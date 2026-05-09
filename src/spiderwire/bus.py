"""Bus master — device discovery, polling, and control.

Emulates the OEM GSS controller's tiered polling pattern (see
`docs/protocol-analysis.md`):

  Tier A (fast)     0x03, 0x0A                  every ~1.0 s
  Tier B (actuator) 0x04, 0x06                  every ~2.5 s
  Tier C (scan)     12 silent slots             every ~7.0 s
  Tier D (heartbeat) FC 0x10 broadcast → 0x00   every ~3.5 s

One "tick" walks Tier A and advances the slow tiers when their deadline
elapses. Call `tick()` on a ~1 Hz cadence (or let `poll_loop()` do it).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .protocol import CRCError, ModbusError, ModbusTimeoutError
from .registers import (
    ACTUATOR_ADDRS,
    ALL_ADDRS,
    BLOWER_SETPOINT_REG,
    BROADCAST_REG_COUNT,
    BROADCAST_START_REG,
    DEFAULT_QTY,
    FAST_ADDRS,
    SCAN_ADDRS,
    DeviceData,
    parse_device_data,
)
from .transport import RS485Transport

log = logging.getLogger(__name__)

# Defaults chosen to match the OEM hub's observed cadence.
DEFAULT_FAST_INTERVAL = 1.0
DEFAULT_ACTUATOR_INTERVAL = 2.5
DEFAULT_SCAN_INTERVAL = 7.0
DEFAULT_HEARTBEAT_INTERVAL = 3.5


@dataclass
class BusMaster:
    transport: RS485Transport
    devices: dict[int, DeviceData] = field(default_factory=dict)
    # Setpoints broadcast in the master heartbeat (reg 1001, 26 regs).
    # Mutate at runtime to change what peripherals receive.
    # reg 1009 = 7 and reg 1011 = 1112 are the OEM's observed defaults.
    setpoints: list[int] = field(
        default_factory=lambda: _default_setpoints()
    )

    # Tiered orchestration config — mirrors the OEM GSS hub.
    fast_addrs: list[int] = field(default_factory=lambda: list(FAST_ADDRS))
    actuator_addrs: list[int] = field(default_factory=lambda: list(ACTUATOR_ADDRS))
    scan_addrs: list[int] = field(default_factory=lambda: list(SCAN_ADDRS))
    actuator_interval: float = DEFAULT_ACTUATOR_INTERVAL
    scan_interval: float = DEFAULT_SCAN_INTERVAL
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    # Settle time between consecutive polls within a tier. Without this
    # gap the *second* device in each pair (0x06 in actuator, 0x0A in
    # fast) reliably times out — observed `to=` >> `ok=` for both, while
    # `0x03` and `0x04` (the *first* polls) never timed out. `scan()`
    # already uses 50 ms; tick() needs the same. Cheap insurance — at
    # 4 polls/cycle this adds <0.2 s of overhead per tick.
    inter_poll_gap: float = 0.05
    # Drop a device from `self.devices` only after this many consecutive
    # failed polls. The OEM bus is bursty: 0x0A in particular goes silent
    # for 30+ s at a time but then resumes. Keep stale data visible so
    # callers (CLI, HA) can show "last seen Ns ago" instead of flapping.
    offline_after_failures: int = 30

    _fail_count: dict[int, int] = field(default_factory=dict)
    # Monotonic timestamp of last successful poll per addr — drives the
    # "polled Ns ago" / staleness display.
    last_seen: dict[int, float] = field(default_factory=dict)
    # Per-addr poll statistics — lets callers distinguish "slave is silent"
    # (timeout) from "slave replied but we mangled it" (crc). The
    # difference points at different bus problems.
    poll_stats: dict[int, dict[str, int]] = field(default_factory=dict)
    _next_actuator: float = 0.0
    _next_scan: float = 0.0
    _next_heartbeat: float = 0.0

    def scan(
        self,
        addrs: list[int] | None = None,
        inter_poll: float = 0.05,
        heartbeat_interval: float = 0.0,
    ) -> dict[int, DeviceData]:
        """Poll every address once (discovery walk).

        Unlike `tick()`, this always hits every slot — use it for the
        `gss-ctrl scan` command or a one-shot inventory. `inter_poll`
        gives the RS-485 adapter a beat between transactions so slow
        peripherals respond cleanly. `heartbeat_interval > 0` interleaves
        broadcasts so peripherals don't drop into master-missing
        fail-safe during a long (full-range) scan.
        """
        targets = addrs or ALL_ADDRS
        next_heartbeat = float("inf")
        if heartbeat_interval > 0:
            try:
                self.broadcast_setpoints()
            except Exception:
                log.exception("Pre-scan heartbeat failed")
            next_heartbeat = time.monotonic() + heartbeat_interval

        for i, addr in enumerate(targets):
            if time.monotonic() >= next_heartbeat:
                try:
                    self.broadcast_setpoints()
                except Exception:
                    log.exception("In-scan heartbeat failed")
                next_heartbeat = time.monotonic() + heartbeat_interval
            self.poll_device(addr)
            if inter_poll > 0 and i < len(targets) - 1:
                time.sleep(inter_poll)
        return self.devices

    def poll_device(self, addr: int) -> DeviceData | None:
        qty = DEFAULT_QTY.get(addr, 16)
        stats = self.poll_stats.setdefault(
            addr, {"ok": 0, "timeout": 0, "crc": 0, "framing": 0, "other": 0}
        )
        try:
            resp = self.transport.read_holding_registers(addr, start_reg=0, qty=qty)
            data = parse_device_data(resp.registers)
            self.devices[addr] = data
            self.last_seen[addr] = time.monotonic()
            self._fail_count[addr] = 0
            stats["ok"] += 1
            log.info("Dev 0x%02X responded (%d regs)", addr, qty)
            return data
        except ModbusTimeoutError:
            stats["timeout"] += 1
            self._fail_count[addr] = self._fail_count.get(addr, 0) + 1
            if addr in self.devices and self._fail_count[addr] > self.offline_after_failures:
                log.warning("Dev 0x%02X went offline", addr)
                del self.devices[addr]
            log.debug("Dev 0x%02X timeout (silent)", addr)
            return None
        except CRCError as e:
            # Slave did transmit bytes but we couldn't validate them — this
            # is the fingerprint of bus-integrity / turnaround issues, not
            # a missing slave. Keep loud so it shows up in stats.
            stats["crc"] += 1
            log.warning("Dev 0x%02X CRC fail: %s", addr, e)
            return None
        except ModbusError as e:
            # Frame parsed cleanly (CRC valid) but isn't the response we
            # expected — typically because another master (the OEM GSS hub)
            # is also driving the bus and we latched onto its traffic.
            # Expected on a 2-master bus; log gracefully without a trace.
            stats["framing"] += 1
            log.warning("Dev 0x%02X framing: %s", addr, e)
            return None
        except Exception:
            stats["other"] += 1
            log.exception("Error polling dev 0x%02X", addr)
            return None

    # ----- Control helpers -----

    def set_fan_speed(self, addr: int, speed: int) -> None:
        """Write fan speed (0-25) to the fan controller."""
        if not 0 <= speed <= 25:
            raise ValueError(f"Fan speed must be 0-25, got {speed}")
        self.transport.write_register(addr, reg=10, value=speed)
        log.info("Dev 0x%02X fan speed → %d", addr, speed)

    def set_fan_enable(self, addr: int, enable: bool) -> None:
        self.transport.write_register(addr, reg=16, value=int(enable))
        log.info("Dev 0x%02X fan enable → %s", addr, enable)

    def set_light_enable(self, addr: int, enable: bool) -> None:
        """Write light enable flag (reg 18 on sensor hub)."""
        self.transport.write_register(addr, reg=18, value=int(enable))
        log.info("Dev 0x%02X light enable → %s", addr, enable)

    def set_blower(self, addr: int, percent: int) -> None:
        """Set blower / ventilation % via FC06 → reg 14.

        Device 0x06 (the "Light 2" in the OEM UI) echoes this write, so
        we wait for the standard Modbus response. 0 turns the blower off;
        the OEM UI enforces a 25 % floor above 0 but the device itself
        accepts any 0-100 value.
        """
        if not 0 <= percent <= 100:
            raise ValueError(f"Blower percent must be 0-100, got {percent}")
        self.transport.write_register(addr, reg=BLOWER_SETPOINT_REG, value=percent)
        log.info("Dev 0x%02X blower → %d %%", addr, percent)

    def broadcast_setpoints(self, values: list[int] | None = None) -> None:
        """Send FC 0x10 broadcast to addr 0x00 (all devices).

        The OEM master broadcasts 26 registers starting at 1001. If
        `values` is None, the current `self.setpoints` is sent.
        """
        payload = self.setpoints if values is None else values
        if len(payload) != BROADCAST_REG_COUNT:
            raise ValueError(
                f"Expected {BROADCAST_REG_COUNT} values, got {len(payload)}"
            )
        self.transport.write_registers(0x00, BROADCAST_START_REG, payload)
        log.debug("Broadcast %d regs @ %d", BROADCAST_REG_COUNT, BROADCAST_START_REG)

    # ----- Tiered orchestration -----

    def tick(self) -> dict[int, DeviceData]:
        """Run one orchestration cycle matching the OEM GSS hub.

        * Tier B (actuators) runs first when its deadline hits — the OEM
          hub slots it in right before the fast pair.
        * Tier A (fast sensors) always runs.
        * Tier D (heartbeat) fires between fast pairs, never mid-request.
        * Tier C (silent-slot scan) runs last; ~1.7 s burst of timeouts
          every ~7 s keeps the bus mostly free for sensors.

        Call at ~1 Hz (see `poll_loop` for the outer scheduler). Returns
        the current `self.devices` snapshot.
        """
        now = time.monotonic()

        if now >= self._next_actuator:
            self._poll_with_gap(self.actuator_addrs)
            self._next_actuator = now + self.actuator_interval

        self._poll_with_gap(self.fast_addrs)

        if time.monotonic() >= self._next_heartbeat:
            try:
                self.broadcast_setpoints()
            except Exception:
                log.exception("Heartbeat broadcast failed")
            self._next_heartbeat = time.monotonic() + self.heartbeat_interval

        if time.monotonic() >= self._next_scan:
            self._poll_with_gap(self.scan_addrs)
            self._next_scan = time.monotonic() + self.scan_interval

        return self.devices

    def _poll_with_gap(self, addrs: list[int]) -> None:
        """Poll `addrs` in order with `self.inter_poll_gap` between them.
        See `inter_poll_gap` field for why the gap matters."""
        for i, addr in enumerate(addrs):
            if i and self.inter_poll_gap > 0:
                time.sleep(self.inter_poll_gap)
            self.poll_device(addr)

    def poll_loop(
        self,
        interval: float = DEFAULT_FAST_INTERVAL,
        callback=None,
    ) -> None:
        """Run `tick()` in a loop at `interval` seconds (default 1 s).

        Adjust tier cadence via `self.actuator_interval`,
        `self.scan_interval`, `self.heartbeat_interval`. Calls
        `callback(devices)` after each tick. Runs until KeyboardInterrupt.
        """
        try:
            while True:
                cycle_start = time.monotonic()
                self.tick()
                if callback:
                    callback(self.devices)
                sleep_for = interval - (time.monotonic() - cycle_start)
                if sleep_for > 0:
                    time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Poll loop stopped")


def _default_setpoints() -> list[int]:
    """Empty setpoint block with the OEM's observed constants pre-filled.

    reg 1009 = 7 (mode/state flag), reg 1011 = 1112 (setpoint). Both are
    constant across all captures; peripherals appear to expect them.
    """
    values = [0] * BROADCAST_REG_COUNT
    values[1009 - BROADCAST_START_REG] = 7
    values[1011 - BROADCAST_START_REG] = 1112
    return values
