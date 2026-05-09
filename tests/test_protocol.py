"""Pure-Python tests for the Modbus RTU protocol layer."""

from __future__ import annotations

import pytest

from spiderwire.protocol import (
    CRCError,
    ExceptionResponse,
    ModbusError,
    ReadResponse,
    WriteMultipleResponse,
    WriteResponse,
    append_crc,
    build_read_holding,
    build_write_multiple,
    build_write_single,
    check_crc,
    crc16,
    parse_response,
)


class TestCRC16:
    def test_known_vector_empty(self):
        # CRC-16/Modbus of empty input is the init value 0xFFFF.
        assert crc16(b"") == 0xFFFF

    def test_known_vector_read_request(self):
        # `01 03 00 00 00 01` → CRC 0x840A (low byte first on the wire).
        assert crc16(bytes.fromhex("0103000000 01".replace(" ", ""))) == 0x0A84

    def test_round_trip(self):
        frame = bytes.fromhex("0A030001")
        out = append_crc(frame)
        assert check_crc(out) == frame

    def test_check_crc_too_short(self):
        with pytest.raises(CRCError):
            check_crc(b"\x01\x02\x03")

    def test_check_crc_mismatch(self):
        good = append_crc(b"\x01\x03\x00")
        tampered = good[:-1] + bytes([good[-1] ^ 0xFF])
        with pytest.raises(CRCError):
            check_crc(tampered)


class TestRequestBuilders:
    def test_read_holding(self):
        # FC03 read 28 regs starting at 0 from slave 0x0A.
        # Expected layload bytes (without CRC): 0A 03 00 00 00 1C
        out = build_read_holding(addr=0x0A, start_reg=0, qty=28)
        assert out[:6] == bytes.fromhex("0A030000001C")
        assert len(out) == 8  # 6 payload + 2 CRC
        # CRC must validate.
        assert check_crc(out) == out[:-2]

    def test_write_single(self):
        # FC06 write reg 10 = 50 on slave 0x04.
        out = build_write_single(addr=0x04, reg=10, value=50)
        assert out[:6] == bytes.fromhex("0406000A0032")
        assert len(out) == 8
        assert check_crc(out) == out[:-2]

    def test_write_multiple(self):
        # FC10 broadcast: addr 0x00, start 1001 (0x03E9), 2 regs.
        values = [0x1111, 0x2222]
        out = build_write_multiple(addr=0x00, start_reg=1001, values=values)
        # addr fc start_hi start_lo qty_hi qty_lo bc data...
        assert out[:7] == bytes.fromhex("001003E90002 04".replace(" ", ""))
        assert out[7:11] == bytes.fromhex("11112222")
        assert check_crc(out) == out[:-2]


class TestParseResponse:
    def test_read_response(self):
        # Slave 0x0A replies with 2 regs: [0x000A, 0xAA00].
        body = bytes.fromhex("0A0304000AAA00")
        raw = append_crc(body)
        resp = parse_response(raw)
        assert isinstance(resp, ReadResponse)
        assert resp.addr == 0x0A
        assert resp.registers == [0x000A, 0xAA00]

    def test_write_single_response(self):
        body = bytes.fromhex("0406000A0032")
        raw = append_crc(body)
        resp = parse_response(raw)
        assert isinstance(resp, WriteResponse)
        assert resp.addr == 0x04
        assert resp.reg == 10
        assert resp.value == 50

    def test_write_multiple_response(self):
        # Echo of the broadcast header: addr 00, fc 10, start 1001, qty 2.
        body = bytes.fromhex("001003E90002")
        raw = append_crc(body)
        resp = parse_response(raw)
        assert isinstance(resp, WriteMultipleResponse)
        assert resp.start_reg == 1001
        assert resp.qty == 2

    def test_exception_response(self):
        # FC03 with the exception bit set, code 0x02 (Illegal Data Address).
        body = bytes.fromhex("0A8302")
        raw = append_crc(body)
        with pytest.raises(ExceptionResponse) as ei:
            parse_response(raw)
        assert ei.value.fc == 0x03
        assert ei.value.code == 0x02

    def test_unknown_function_code(self):
        body = bytes.fromhex("0A7700")  # FC 0x77 — not implemented.
        raw = append_crc(body)
        with pytest.raises(ModbusError):
            parse_response(raw)
