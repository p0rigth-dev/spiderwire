"""Modbus RTU frame encoding/decoding and CRC-16 for SpiderFarmer bus."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum


class FunctionCode(IntEnum):
    READ_HOLDING_REGISTERS = 0x03
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_REGISTERS = 0x10

    # Exception flag (ORed with the original FC in error responses)
    EXCEPTION_FLAG = 0x80


class ModbusError(Exception):
    pass


class CRCError(ModbusError):
    pass


class ModbusTimeoutError(ModbusError):
    pass


class ExceptionResponse(ModbusError):
    def __init__(self, fc: int, code: int):
        self.fc = fc
        self.code = code
        super().__init__(f"Modbus exception FC=0x{fc:02X} code={code}")


# ---------------------------------------------------------------------------
# CRC-16/Modbus  (polynomial 0xA001, init 0xFFFF)
# ---------------------------------------------------------------------------

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc(frame: bytes) -> bytes:
    c = crc16(frame)
    return frame + struct.pack("<H", c)


def check_crc(frame: bytes) -> bytes:
    """Validate CRC and return payload (frame minus trailing 2-byte CRC)."""
    if len(frame) < 4:
        raise CRCError(f"Frame too short ({len(frame)} bytes)")
    payload, crc_rx = frame[:-2], struct.unpack("<H", frame[-2:])[0]
    if crc16(payload) != crc_rx:
        raise CRCError(f"CRC mismatch (got 0x{crc_rx:04X}, expected 0x{crc16(payload):04X})")
    return payload


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------

def build_read_holding(addr: int, start_reg: int, qty: int) -> bytes:
    """FC 0x03 — Read Holding Registers request."""
    frame = struct.pack(">BBHH", addr, FunctionCode.READ_HOLDING_REGISTERS, start_reg, qty)
    return append_crc(frame)


def build_write_single(addr: int, reg: int, value: int) -> bytes:
    """FC 0x06 — Write Single Register request."""
    frame = struct.pack(">BBHH", addr, FunctionCode.WRITE_SINGLE_REGISTER, reg, value)
    return append_crc(frame)


def build_write_multiple(addr: int, start_reg: int, values: list[int]) -> bytes:
    """FC 0x10 — Write Multiple Registers request."""
    qty = len(values)
    byte_count = qty * 2
    frame = struct.pack(
        f">BBHHB{qty}H",
        addr, FunctionCode.WRITE_MULTIPLE_REGISTERS,
        start_reg, qty, byte_count,
        *values,
    )
    return append_crc(frame)


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

@dataclass
class ReadResponse:
    addr: int
    registers: list[int]


@dataclass
class WriteResponse:
    addr: int
    reg: int
    value: int


@dataclass
class WriteMultipleResponse:
    addr: int
    start_reg: int
    qty: int


def parse_response(raw: bytes) -> ReadResponse | WriteResponse | WriteMultipleResponse:
    """Parse a validated Modbus RTU response frame (with CRC)."""
    payload = check_crc(raw)
    addr, fc = payload[0], payload[1]

    if fc & FunctionCode.EXCEPTION_FLAG:
        raise ExceptionResponse(fc & 0x7F, payload[2])

    if fc == FunctionCode.READ_HOLDING_REGISTERS:
        byte_count = payload[2]
        if len(payload) < 3 + byte_count:
            raise ModbusError(
                f"FC03 payload too short: byte_count={byte_count} but only "
                f"{len(payload) - 3} data bytes present"
            )
        n = byte_count // 2
        regs = list(struct.unpack(f">{n}H", payload[3:3 + byte_count]))
        return ReadResponse(addr=addr, registers=regs)

    if fc == FunctionCode.WRITE_SINGLE_REGISTER:
        reg, value = struct.unpack(">HH", payload[2:6])
        return WriteResponse(addr=addr, reg=reg, value=value)

    if fc == FunctionCode.WRITE_MULTIPLE_REGISTERS:
        start_reg, qty = struct.unpack(">HH", payload[2:6])
        return WriteMultipleResponse(addr=addr, start_reg=start_reg, qty=qty)

    raise ModbusError(f"Unknown function code 0x{fc:02X}")
