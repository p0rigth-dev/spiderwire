"""Tests for register parsing and dataclass decoders."""

from __future__ import annotations

import math

import pytest

from spiderwire.registers import (
    BlowerData,
    CO2SensorData,
    DeviceHeader,
    DeviceType,
    FanControllerData,
    SensorHubData,
    _as_signed,
    parse_device_data,
)


def _header(addr: int = 0x0A) -> list[int]:
    """Build a 10-register device header.

    reg 0  = addr
    reg 1  = magic 0xAA in high byte
    reg 2,3 = ASCII "01" "23" → fw "01.23"
    reg 4  = model code
    reg 5  = serial fragment
    reg 6  = device type (high byte = major)
    reg 7  = hw version
    reg 8,9 = padding
    """
    return [
        addr,
        0xAA00,
        int.from_bytes(b"01", "big"),
        int.from_bytes(b"23", "big"),
        0x1234,
        0x5678,
        (DeviceType.SENSOR_HUB << 8) | 0x01,
        0x0001,
        0,
        0,
    ]


class TestDeviceHeader:
    def test_basic_fields(self):
        h = DeviceHeader.from_registers(_header(0x0A))
        assert h.address == 0x0A
        assert h.magic_byte == 0xAA
        assert h.fw_version == "01.23"
        assert h.model_code == 0x1234
        assert h.serial_frag == 0x5678
        assert h.type_major == int(DeviceType.SENSOR_HUB)
        assert h.type_name == "sensor_hub"

    def test_unknown_type_name(self):
        regs = _header()
        regs[6] = 0xEE00  # major byte 0xEE — not in DeviceType.
        h = DeviceHeader.from_registers(regs)
        assert h.type_name == "unknown(0xEE)"

    def test_non_ascii_fw_falls_back_to_hex(self):
        regs = _header()
        regs[2] = 0xFF00  # not ASCII
        regs[3] = 0x00FF
        h = DeviceHeader.from_registers(regs)
        assert h.fw_version == "0xFF0000FF"


class TestSensorHubData:
    def _hub_regs(self) -> list[int]:
        regs = _header(0x0A) + [0] * 18  # need 28 total
        # Override hub-specific slots.
        regs[10] = 245       # 24.5 °C
        regs[11] = 612       # 61.2 % RH
        regs[12] = 218       # 21.8 °C soil
        regs[13] = 850       # PPFD
        regs[14] = 12        # secondary
        regs[15] = 0
        regs[18] = 1         # light enabled
        regs[19] = 75        # light value
        regs[21] = 2         # zone
        return regs

    def test_decode(self):
        d = SensorHubData.from_registers(self._hub_regs())
        assert d.air_temp_c == pytest.approx(24.5)
        assert d.air_humidity_pct == pytest.approx(61.2)
        assert d.soil_temp_c == pytest.approx(21.8)
        assert d.ppfd == 850
        assert d.light_enabled is True
        assert d.light_value == 75
        assert d.zone == 2

    def test_soil_disconnected_returns_none(self):
        regs = self._hub_regs()
        # -1000 sentinel encoded as 16-bit two's complement.
        regs[12] = (-1000) & 0xFFFF
        d = SensorHubData.from_registers(regs)
        assert d.soil_temp_c is None

    def test_vpd_kpa_matches_tetens(self):
        d = SensorHubData.from_registers(self._hub_regs())
        # Reference VPD at 24.5 °C / 61.2 % RH using Tetens.
        t, rh = 24.5, 61.2
        svp = 0.6108 * math.exp(17.27 * t / (t + 237.3))
        expected = svp * (1 - rh / 100.0)
        assert d.vpd_kpa == pytest.approx(expected, rel=1e-6)


class TestParseDeviceDataDispatch:
    def test_sensor_hub_28_regs(self):
        regs = _header(0x0A) + [0] * 18
        assert isinstance(parse_device_data(regs), SensorHubData)

    def test_fan_controller_24_regs(self):
        regs = _header(0x04) + [0] * 14
        regs[10] = 50  # speed/brightness
        regs[16] = 1   # enabled
        d = parse_device_data(regs)
        assert isinstance(d, FanControllerData)
        assert d.speed == 50
        assert d.value == 50
        assert d.brightness_pct == 50
        assert d.enabled is True

    def test_blower_16_regs(self):
        regs = _header(0x06) + [0] * 6
        regs[14] = 40  # setpoint %
        regs[12] = 1   # running
        d = parse_device_data(regs)
        assert isinstance(d, BlowerData)
        assert d.percent == 40
        assert d.running is True

    def test_co2_13_regs(self):
        regs = _header(0x03) + [0] * 3
        regs[10] = 850
        d = parse_device_data(regs)
        assert isinstance(d, CO2SensorData)
        assert d.co2_ppm == 850

    def test_header_only_10_regs(self):
        d = parse_device_data(_header(0x0A))
        assert isinstance(d, DeviceHeader)

    def test_too_few_regs_raises(self):
        with pytest.raises(ValueError):
            parse_device_data([0] * 5)


class TestSigned:
    def test_zero(self):
        assert _as_signed(0) == 0

    def test_positive(self):
        assert _as_signed(0x7FFF) == 32767

    def test_negative_one(self):
        assert _as_signed(0xFFFF) == -1

    def test_min(self):
        assert _as_signed(0x8000) == -32768
