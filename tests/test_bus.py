"""Tests for BusMaster — validation, scheduling, and broadcast."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spiderwire.bus import BusMaster
from spiderwire.protocol import ModbusTimeoutError, ReadResponse
from spiderwire.registers import (
    ACTUATOR_ADDRS,
    BROADCAST_REG_COUNT,
    BROADCAST_START_REG,
    FAST_ADDRS,
    SCAN_ADDRS,
    SensorHubData,
)


def _hub_regs(addr: int = 0x0A) -> list[int]:
    """28-register sensor-hub frame (mirrors test_registers helper)."""
    regs = [
        addr,
        0xAA00,
        int.from_bytes(b"01", "big"),
        int.from_bytes(b"23", "big"),
        0x1234,
        0x5678,
        0x0400,
        0x0001,
        0,
        0,
    ]
    regs += [0] * 18
    regs[10] = 245   # 24.5 °C
    regs[11] = 612   # 61.2 % RH
    return regs


def _make_transport() -> MagicMock:
    """Mock transport that mimics the RS485Transport surface used by BusMaster."""
    return MagicMock()


class TestPollDevice:
    def test_records_response_and_stats(self):
        tx = _make_transport()
        tx.read_holding_registers.return_value = ReadResponse(
            addr=0x0A, registers=_hub_regs()
        )
        bus = BusMaster(transport=tx)
        result = bus.poll_device(0x0A)
        assert isinstance(result, SensorHubData)
        assert 0x0A in bus.devices
        assert bus.poll_stats[0x0A]["ok"] == 1
        assert bus.last_seen[0x0A] > 0

    def test_timeout_increments_fail_count(self):
        tx = _make_transport()
        tx.read_holding_registers.side_effect = ModbusTimeoutError("silent")
        bus = BusMaster(transport=tx)
        for _ in range(3):
            assert bus.poll_device(0x0A) is None
        assert bus.poll_stats[0x0A]["timeout"] == 3
        # `_fail_count` is internal but its behaviour drives the offline check.
        assert bus._fail_count[0x0A] == 3

    def test_offline_after_failures_drops_device(self):
        tx = _make_transport()
        tx.read_holding_registers.return_value = ReadResponse(
            addr=0x0A, registers=_hub_regs()
        )
        bus = BusMaster(transport=tx, offline_after_failures=2)
        bus.poll_device(0x0A)
        assert 0x0A in bus.devices

        tx.read_holding_registers.side_effect = ModbusTimeoutError("silent")
        bus.poll_device(0x0A)
        bus.poll_device(0x0A)
        # Threshold is "more than" — exactly 2 failures still keeps it.
        assert 0x0A in bus.devices
        bus.poll_device(0x0A)
        assert 0x0A not in bus.devices


class TestSetters:
    def test_set_fan_speed_validates_range(self):
        bus = BusMaster(transport=_make_transport())
        with pytest.raises(ValueError):
            bus.set_fan_speed(0x04, 26)
        with pytest.raises(ValueError):
            bus.set_fan_speed(0x04, -1)
        bus.set_fan_speed(0x04, 0)
        bus.set_fan_speed(0x04, 25)

    def test_set_fan_speed_writes_reg_10(self):
        tx = _make_transport()
        bus = BusMaster(transport=tx)
        bus.set_fan_speed(0x04, 12)
        tx.write_register.assert_called_once_with(0x04, reg=10, value=12)

    def test_set_blower_validates_range(self):
        bus = BusMaster(transport=_make_transport())
        with pytest.raises(ValueError):
            bus.set_blower(0x06, 101)
        with pytest.raises(ValueError):
            bus.set_blower(0x06, -1)
        bus.set_blower(0x06, 0)
        bus.set_blower(0x06, 100)

    def test_set_blower_writes_reg_14(self):
        tx = _make_transport()
        bus = BusMaster(transport=tx)
        bus.set_blower(0x06, 40)
        tx.write_register.assert_called_once_with(0x06, reg=14, value=40)


class TestBroadcastSetpoints:
    def test_default_setpoints_have_oem_constants(self):
        bus = BusMaster(transport=_make_transport())
        assert len(bus.setpoints) == BROADCAST_REG_COUNT
        assert bus.setpoints[1009 - BROADCAST_START_REG] == 7
        assert bus.setpoints[1011 - BROADCAST_START_REG] == 1112

    def test_broadcast_calls_transport_with_payload(self):
        tx = _make_transport()
        bus = BusMaster(transport=tx)
        bus.broadcast_setpoints()
        tx.write_registers.assert_called_once()
        addr, start, payload = tx.write_registers.call_args.args
        assert addr == 0x00
        assert start == BROADCAST_START_REG
        assert payload == bus.setpoints

    def test_broadcast_explicit_values_override_setpoints(self):
        tx = _make_transport()
        bus = BusMaster(transport=tx)
        custom = [42] * BROADCAST_REG_COUNT
        bus.broadcast_setpoints(custom)
        _, _, payload = tx.write_registers.call_args.args
        assert payload == custom

    def test_broadcast_rejects_wrong_length(self):
        bus = BusMaster(transport=_make_transport())
        with pytest.raises(ValueError):
            bus.broadcast_setpoints([0, 0])


class TestTickScheduling:
    def test_first_tick_visits_actuator_fast_and_heartbeat(self):
        tx = _make_transport()
        tx.read_holding_registers.return_value = ReadResponse(
            addr=0x0A, registers=_hub_regs()
        )
        bus = BusMaster(transport=tx, inter_poll_gap=0)
        bus.tick()

        polled = [c.args[0] for c in tx.read_holding_registers.call_args_list]
        # Tier B (actuators) runs before Tier A (fast) on the first tick
        # because all deadlines start at 0.
        assert polled[: len(ACTUATOR_ADDRS)] == ACTUATOR_ADDRS
        assert polled[len(ACTUATOR_ADDRS):][: len(FAST_ADDRS)] == FAST_ADDRS
        # Tier C (scan) also fires on the first tick (deadline 0).
        scan_polled = polled[len(ACTUATOR_ADDRS) + len(FAST_ADDRS):]
        assert scan_polled == SCAN_ADDRS
        # Heartbeat broadcast happened at least once.
        tx.write_registers.assert_called()

    def test_second_tick_skips_actuator_when_interval_not_elapsed(self):
        tx = _make_transport()
        tx.read_holding_registers.return_value = ReadResponse(
            addr=0x0A, registers=_hub_regs()
        )
        bus = BusMaster(
            transport=tx,
            inter_poll_gap=0,
            actuator_interval=10.0,
            scan_interval=10.0,
            heartbeat_interval=10.0,
        )
        bus.tick()
        tx.read_holding_registers.reset_mock()
        bus.tick()

        polled = [c.args[0] for c in tx.read_holding_registers.call_args_list]
        # Only the fast tier should run on the second tick.
        assert polled == FAST_ADDRS

    def test_tick_returns_devices_snapshot(self):
        tx = _make_transport()
        tx.read_holding_registers.return_value = ReadResponse(
            addr=0x0A, registers=_hub_regs()
        )
        bus = BusMaster(transport=tx, inter_poll_gap=0)
        result = bus.tick()
        assert result is bus.devices
