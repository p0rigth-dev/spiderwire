"""Register map definitions for SpiderFarmer peripherals.

Register numbers and expected quantities match the protocol analysis.
All register data is big-endian unsigned 16-bit unless noted otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum


class DeviceType(IntEnum):
    """Known device type codes (register 6, high byte)."""
    LIGHT = 0x02
    FAN = 0x03
    SENSOR_HUB = 0x04


# Addresses the OEM master polls (from protocol analysis)
ALL_ADDRS = [
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
    0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x13,
]

# Observed OEM orchestration tiers (see docs/protocol-analysis.md §"Bus Timing").
# The GSS hub does not poll uniformly — it runs four interleaved schedules:
#
#   Tier A  FAST_ADDRS      every ~1 s        primary sensors
#   Tier B  ACTUATOR_ADDRS  every ~2.5 s      light dimmer + blower
#   Tier C  SCAN_ADDRS      every ~7 s        silent-slot discovery
#   Tier D  FC10 broadcast  every ~3.5 s      setpoint heartbeat
#
# Address lists are exactly the order the OEM hub uses.
FAST_ADDRS: list[int] = [0x03, 0x0A]
ACTUATOR_ADDRS: list[int] = [0x04, 0x06]
SCAN_ADDRS: list[int] = [
    0x10, 0x02, 0x01, 0x0C, 0x05, 0x07,
    0x0B, 0x0D, 0x0E, 0x08, 0x0F, 0x13,
]

# Per-address register count the OEM hub reads on every poll, encoding
# the OEM's expected device class at each slot.
DEFAULT_QTY: dict[int, int] = {
    0x01: 16, 0x02: 22, 0x03: 13, 0x04: 24, 0x05: 30,
    0x06: 16, 0x07: 30, 0x08: 21, 0x0A: 28, 0x0B: 30,
    0x0C: 21, 0x0D: 22, 0x0E: 21, 0x0F: 16, 0x10: 28,
    0x13: 23,
}


# ---------------------------------------------------------------------------
# Broadcast write (FC 0x10, addr 0x00)
# ---------------------------------------------------------------------------

BROADCAST_START_REG = 1001
BROADCAST_REG_COUNT = 26


# ---------------------------------------------------------------------------
# Common header (regs 0-9, all devices)
# ---------------------------------------------------------------------------

@dataclass
class DeviceHeader:
    address: int
    magic_byte: int        # should be 0xAA
    fw_version: str
    model_code: int
    serial_frag: int
    device_type: int       # high byte = type, low byte = subtype
    hw_version: int

    @classmethod
    def from_registers(cls, regs: list[int]) -> DeviceHeader:
        addr = regs[0]
        magic = (regs[1] >> 8) & 0xFF
        r2, r3 = regs[2], regs[3]
        try:
            fw_hi = r2.to_bytes(2, "big").decode("ascii")
            fw_lo = r3.to_bytes(2, "big").decode("ascii")
            fw = f"{fw_hi}.{fw_lo}"
        except (UnicodeDecodeError, ValueError):
            fw = f"0x{r2:04X}{r3:04X}"
        return cls(
            address=addr,
            magic_byte=magic,
            fw_version=fw,
            model_code=regs[4],
            serial_frag=regs[5],
            device_type=regs[6],
            hw_version=regs[7],
        )

    @property
    def type_major(self) -> int:
        return (self.device_type >> 8) & 0xFF

    @property
    def type_minor(self) -> int:
        return self.device_type & 0xFF

    @property
    def type_name(self) -> str:
        try:
            return DeviceType(self.type_major).name.lower()
        except ValueError:
            return f"unknown(0x{self.type_major:02X})"


# ---------------------------------------------------------------------------
# Sensor Hub (addr 0x0A, 28 regs)
# ---------------------------------------------------------------------------

@dataclass
class SensorHubData:
    header: DeviceHeader
    air_temp_raw: int          # reg 10, ×10 °C
    air_humidity_raw: int      # reg 11, ×10 %
    soil_temp_raw: int         # reg 12, ×10 °C (signed)
    ppfd: int                  # reg 13, µmol/m²/s (confirmed against app)
    ppfd_secondary: int        # reg 14, co-tracking channel (peak/IR/DLI, TBD)
    calibration: int           # reg 15
    light_enabled: bool        # reg 18
    light_value: int           # reg 19
    zone: int                  # reg 21

    @property
    def air_temp_c(self) -> float:
        return self.air_temp_raw / 10.0

    @property
    def air_humidity_pct(self) -> float:
        return self.air_humidity_raw / 10.0

    @property
    def soil_temp_c(self) -> float | None:
        # -1000 means not connected
        v = _as_signed(self.soil_temp_raw)
        return None if v == -1000 else v / 10.0

    @property
    def vpd_kpa(self) -> float:
        """Vapour Pressure Deficit from temp and humidity."""
        t = self.air_temp_c
        rh = self.air_humidity_pct
        svp = 0.6108 * math.exp(17.27 * t / (t + 237.3))
        return svp * (1 - rh / 100.0)

    @classmethod
    def from_registers(cls, regs: list[int]) -> SensorHubData:
        header = DeviceHeader.from_registers(regs)
        return cls(
            header=header,
            air_temp_raw=regs[10],
            air_humidity_raw=regs[11],
            soil_temp_raw=regs[12],
            ppfd=regs[13],
            ppfd_secondary=regs[14],
            calibration=regs[15],
            light_enabled=bool(regs[18]),
            light_value=regs[19],
            zone=regs[21],
        )

# ---------------------------------------------------------------------------
# CO2 Sensor (addr 0x03, 13 regs)
# ---------------------------------------------------------------------------

@dataclass
class CO2SensorData:
    header: DeviceHeader
    co2_ppm: int  # reg 10

    @classmethod
    def from_registers(cls, regs: list[int]) -> CO2SensorData:
        return cls(
            header=DeviceHeader.from_registers(regs),
            co2_ppm=regs[10],
        )


# ---------------------------------------------------------------------------
# Fan Controller (addr 0x04, 24 regs)
# ---------------------------------------------------------------------------

@dataclass
class FanControllerData:
    """24-register PWM actuator.

    The OEM firmware self-identifies as a fan (`type_major = 0x03`) but
    the same SKU is wired as the **Light 1 dimmer** on this rig
    (`reg 10` runs 0-100 %, confirmed in `docs/device-map.md` and
    `docs/capture-20260418-1152.sal`). Callers that want fan semantics
    read `.speed`; callers that want light semantics read
    `.brightness_pct`. Both return the same underlying value — the
    wiring decides how to interpret it.
    """
    header: DeviceHeader
    speed: int          # reg 10 — raw 0-100 (PWM) or 0-25 (fan)
    enabled: bool       # reg 16

    @property
    def value(self) -> int:
        """Neutral name for `reg 10` — no fan/light assumption."""
        return self.speed

    @property
    def brightness_pct(self) -> int:
        """Reg 10 interpreted as 0-100 % brightness (light-dimmer wiring)."""
        return self.speed

    @classmethod
    def from_registers(cls, regs: list[int]) -> FanControllerData:
        return cls(
            header=DeviceHeader.from_registers(regs),
            speed=regs[10],
            enabled=bool(regs[16]),
        )

# ---------------------------------------------------------------------------
# Blower / ventilation (addr 0x06, 16 regs)
# ---------------------------------------------------------------------------
#
# The OEM app labels this "Light 2" but the 0x06 SKU actually drives the
# **blower / ventilation** — confirmed in `docs/capture-20260418-1452.sal`:
# the user drove the OEM slider 0 → 74 → 60 → 25 → OFF and every move
# produced FC06 writes to `0x06 reg 14` carrying the % directly, while the
# device's own broadcasts echoed the same value back in reg 14 (and set
# reg 12 = 1 whenever the blower was running).

BLOWER_SETPOINT_REG = 14
BLOWER_RUNNING_REG = 12


@dataclass
class BlowerData:
    header: DeviceHeader
    data_regs: list[int]  # regs 10-15

    @property
    def percent(self) -> int:
        """Reg 14 = 0-100 % setpoint."""
        return self.data_regs[BLOWER_SETPOINT_REG - 10]

    @property
    def running(self) -> bool:
        """Reg 12 goes 1 whenever the blower is active."""
        return self.data_regs[BLOWER_RUNNING_REG - 10] != 0

    @classmethod
    def from_registers(cls, regs: list[int]) -> BlowerData:
        return cls(
            header=DeviceHeader.from_registers(regs),
            data_regs=regs[10:16],
        )

# ---------------------------------------------------------------------------
# Union type and auto-detect
# ---------------------------------------------------------------------------

DeviceData = SensorHubData | CO2SensorData | FanControllerData | BlowerData | DeviceHeader


def parse_device_data(regs: list[int]) -> DeviceData:
    """Auto-detect device type from register count and header, return typed data."""
    n = len(regs)
    if n >= 28:
        return SensorHubData.from_registers(regs)
    if n >= 24:
        return FanControllerData.from_registers(regs)
    if n >= 16:
        return BlowerData.from_registers(regs)
    if n >= 13:
        return CO2SensorData.from_registers(regs)
    if n >= 10:
        return DeviceHeader.from_registers(regs)
    raise ValueError(f"Too few registers ({n}) to parse device header")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_signed(val: int) -> int:
    """Interpret a 16-bit unsigned value as signed."""
    return val - 0x10000 if val >= 0x8000 else val
