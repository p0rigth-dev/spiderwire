"""Thread-safe RS-485 serial transport for Modbus RTU.

Frame reads are deterministic: we know every response size up front
from the function code, so we read exactly that many bytes from the
port. This is the fix for the USB-RS485 adapter's 28-register CRC
failures — the old `wait-for-silence-then-break` heuristic truncated
long frames when the OS delivered bytes in bursts slower than the
inter-frame gap. `docs/capture-20260418-1135.sal` confirms the wire
carries a clean 61-byte response for qty=28 (CRC OK).

Half-duplex TX echo (cheap USB-RS485 dongles without automatic DE)
is supported via `echo=True`. Default is off, matching the DE-driven
adapters currently in use.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import serial

from .protocol import (
    FunctionCode,
    ModbusError,
    ModbusTimeoutError,
    ReadResponse,
    WriteMultipleResponse,
    WriteResponse,
    build_read_holding,
    build_write_multiple,
    build_write_single,
    parse_response,
)

EXCEPTION_FLAG = FunctionCode.EXCEPTION_FLAG

log = logging.getLogger(__name__)

# Inter-frame silence: Modbus RTU spec says 3.5 char times.
# At 115200 baud, one char = 11 bits → 95.5 µs, so 3.5 chars ≈ 334 µs.
# We use a slightly longer gap for safety.
INTER_FRAME_GAP = 0.001  # 1 ms

# After a `wait_for_response=False` write, give the slave long enough
# to finish any (potential) echo TX *and* internal processing before
# we send the next frame. An 8-byte FC06 echo at 115200 baud is ~0.76 ms;
# cheap dimmers also take a couple of ms to latch the new setpoint. 5 ms
# is well under the OEM's observed 3–6 s pacing between FC06 writes to
# 0x04, so this adds no user-visible latency to `pulse` / HA.
POST_BLIND_WRITE_QUIET = 0.005  # 5 ms


class RS485Transport:
    """Low-level Modbus RTU master over a USB-RS485 serial adapter.

    All public methods are thread-safe (guarded by an internal lock).
    Supports use as a context manager.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 0.3,
        echo: bool = False,
    ):
        self._lock = threading.Lock()
        self._timeout = timeout
        self._echo = echo
        self._ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )
        log.info("Opened %s @ %d baud (echo=%s)", port, baudrate, echo)

    @property
    def is_open(self) -> bool:
        return self._ser.is_open

    def close(self) -> None:
        with self._lock:
            self._ser.close()
            log.info("Closed serial port")

    def __enter__(self) -> RS485Transport:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- raw I/O -----

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes within the port timeout or raise.

        pyserial's `read(n)` blocks up to the port's configured
        timeout (set once at construction) and returns up to n bytes,
        so a single call is enough — no per-byte bookkeeping.
        """
        data = self._ser.read(n)
        if len(data) < n:
            raise ModbusTimeoutError(f"Got {len(data)}/{n} bytes before timeout")
        return bytes(data)

    def _send(self, tx: bytes) -> None:
        """Write a request and hold the required 3.5-char silence gap.

        Without the post-TX sleep the slave can miss frame-end
        detection and never reply (Modbus RTU spec). Empirically,
        dropping the gap caused every unicast read/write to time out.

        We short-circuit if the port has been closed under us — that
        happens during HA reload, when `close()` lands between two
        `_poll_with_gap` iterations. Returning a clean
        `ModbusTimeoutError` (caught one level up by `poll_device`)
        keeps the tail of that in-flight tick from spamming
        SerialException tracebacks for every remaining scan-tier address.
        """
        if not self._ser.is_open:
            raise ModbusTimeoutError("Serial port is closed")
        log.debug("TX [%d]: %s", len(tx), tx.hex(" "))
        self._ser.reset_input_buffer()
        self._ser.write(tx)
        self._ser.flush()
        time.sleep(INTER_FRAME_GAP)

    def _transact(
        self, tx: bytes, total_len_fn: Callable[[bytes], int]
    ) -> bytes:
        """Send `tx` and return the slave's raw response (addr..crc).

        `total_len_fn(head)` returns the full expected response size
        once `head` contains the first 3 bytes of the frame (enough to
        branch on the exception bit in FC).
        """
        self._send(tx)
        if self._echo:
            self._recv_exact(len(tx))
        head = self._recv_exact(3)
        total = total_len_fn(head)
        raw = head + (self._recv_exact(total - 3) if total > 3 else b"")
        log.debug("RX [%d]: %s", len(raw), raw.hex(" "))
        return raw

    # ----- Modbus operations -----

    def read_holding_registers(
        self, addr: int, start_reg: int = 0, qty: int = 1
    ) -> ReadResponse:
        """FC 0x03: normal reply = 5 + 2*qty bytes; exception = 5 bytes."""
        req = build_read_holding(addr, start_reg, qty)
        with self._lock:
            raw = self._transact(
                req, lambda h: 5 if (h[1] & EXCEPTION_FLAG) else 5 + qty * 2
            )
        resp = parse_response(raw)
        if not isinstance(resp, ReadResponse):
            raise ModbusError(f"Expected ReadResponse, got {type(resp).__name__}")
        return resp

    def write_register(
        self,
        addr: int,
        reg: int,
        value: int,
        wait_for_response: bool = True,
    ) -> WriteResponse | None:
        """FC 0x06: reply is the 8-byte echo of the request.

        Some GSS actuators (notably whatever's physically at `0x04` on
        the current rig — a light dimmer) act on the write but never
        send the Modbus echo back, confirmed in
        `docs/capture-20260418-1152.sal`. Set `wait_for_response=False`
        for those targets so the caller doesn't stall on a 300 ms
        timeout per write — that path returns ``None`` since there's
        nothing to echo back.
        """
        req = build_write_single(addr, reg, value)
        with self._lock:
            if not wait_for_response:
                # Go through _send so we honour the 3.5-char inter-frame
                # silence Modbus RTU requires. Without it, the next TX on
                # the bus lands inside the previous frame's silence
                # window and every listening slave (including unrelated
                # echo-capable ones like 0x06) discards it — that was
                # the ~50% timeout we saw under `pulse`.
                self._send(req)
                # Give the slave a short tail window to finish
                # processing (and transmitting an echo, if it does)
                # before we let the next TX hit the bus. 0x04 is spec'd
                # silent but this is cheap insurance.
                time.sleep(POST_BLIND_WRITE_QUIET)
                # Drain whatever's sitting in the RX buffer (any echo
                # the slave sent) so it can't poison the next read.
                if self._ser.in_waiting:
                    self._ser.reset_input_buffer()
                return None
            raw = self._transact(req, lambda h: 5 if (h[1] & EXCEPTION_FLAG) else 8)
        resp = parse_response(raw)
        if not isinstance(resp, WriteResponse):
            raise ModbusError(f"Expected WriteResponse, got {type(resp).__name__}")
        return resp

    def write_registers(
        self, addr: int, start_reg: int, values: list[int]
    ) -> WriteMultipleResponse:
        """FC 0x10: 8-byte reply, or no reply for broadcast (addr 0)."""
        req = build_write_multiple(addr, start_reg, values)
        with self._lock:
            if addr == 0:
                self._send(req)
                if self._echo:
                    try:
                        self._recv_exact(len(req))
                    except ModbusTimeoutError:
                        pass
                time.sleep(INTER_FRAME_GAP * 4)
                return WriteMultipleResponse(
                    addr=0, start_reg=start_reg, qty=len(values)
                )
            raw = self._transact(req, lambda h: 5 if (h[1] & EXCEPTION_FLAG) else 8)
        resp = parse_response(raw)
        if not isinstance(resp, WriteMultipleResponse):
            raise ModbusError(
                f"Expected WriteMultipleResponse, got {type(resp).__name__}"
            )
        return resp
