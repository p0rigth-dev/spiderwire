"""CLI entry point for ``gss-ctrl``.

Stand-in for the OEM SpiderFarmer GSS hub on the RS-485 bus: tiered
polling plus setpoint heartbeat broadcasts so peripherals stay out of
their master-missing fail-safe state. Shares ``spiderwire`` with the
Home Assistant integration — same bus logic, different surface.

Usage
-----
    gss-ctrl /dev/ttyUSB0 scan
    gss-ctrl /dev/ttyUSB0 poll [--fast 0x03,0x0A] [--interval 1.0]
                               [--actuator-interval 2.5] [--scan-interval 7.0]
                               [--heartbeat 3.5]
    gss-ctrl /dev/ttyUSB0 read <addr>[,addr...] [qty]
    gss-ctrl /dev/ttyUSB0 write <addr> <reg> <value>
    gss-ctrl /dev/ttyUSB0 fan <addr> <speed>              # 0-25
    gss-ctrl /dev/ttyUSB0 light <percent>                 # 0-100
    gss-ctrl /dev/ttyUSB0 blower [<addr>] <percent>       # 0-100, addr default 0x06
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from spiderwire.bus import (
    DEFAULT_ACTUATOR_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    BusMaster,
)
from spiderwire.protocol import ModbusTimeoutError
from spiderwire.registers import (
    BlowerData,
    CO2SensorData,
    FanControllerData,
    SensorHubData,
)
from spiderwire.transport import RS485Transport

log = logging.getLogger(__name__)

# Match BusMaster.scan's default inter_poll: USB-RS485 adapters need a
# beat between slave transactions or later reads often time out.
_READ_ADDR_GAP_S = 0.05

# Default addresses for the light command. The "Light 1" fixture lives
# on the sensor hub (0x0A, reg 18 enable), brightness on the dimmer at
# 0x04 reg 10 (blind FC06 — the dimmer never echoes).
DEFAULT_HUB_ADDR = 0x0A
DEFAULT_DIMMER_ADDR = 0x04
HUB_LIGHT_ENABLE_REG = 18
DIMMER_ENABLE_REG = 16
DIMMER_BRIGHTNESS_REG = 10


# The OEM ships the same 24-reg SKU as either a fan or a light dimmer,
# and the same 16-reg SKU as either a light or a blower. The device's
# self-reported `type_major` byte therefore lies for 0x04 and 0x06 on
# this rig (see docs/device-map.md). Label by data class — that's
# already the wiring-aware role we resolved when parsing.
_ROLE_BY_TYPE = {
    FanControllerData: "light",
    BlowerData: "blower",
    # 0x03 self-IDs as type=FAN but is the CO₂ sensor — see
    # docs/protocol-analysis.md "Tier A — Fast sensors".
    CO2SensorData: "co2_sensor",
}


def _role_label(data) -> str:
    return _ROLE_BY_TYPE.get(type(data), data.header.type_name)


def _freshness_line(addr: int, bus: BusMaster | None) -> str | None:
    """Diagnostic line: how recently we polled the device, plus tally of
    ok / timeout / crc replies. Lets the user spot a slave that's silently
    falling behind (rising `to=`) vs. one with bus-integrity issues
    (rising `crc=`)."""
    if bus is None:
        return None
    last = bus.last_seen.get(addr)
    stats = bus.poll_stats.get(addr) or {}
    if last is None and not stats:
        return None
    age = f"{time.monotonic() - last:.1f}s ago" if last is not None else "never"
    return (
        f"         polled {age}  "
        f"(ok={stats.get('ok', 0)} to={stats.get('timeout', 0)} "
        f"crc={stats.get('crc', 0)})"
    )


def _format_device(addr: int, data, bus: BusMaster | None = None) -> str:
    lines = [
        f"  [{addr:#04x}] {_role_label(data)}  "
        f"model={data.header.model_code:#06x}  "
        f"fw={data.header.fw_version}  hw={data.header.hw_version:#06x}"
    ]

    if isinstance(data, SensorHubData):
        soil = f"{data.soil_temp_c:.1f}°C" if data.soil_temp_c is not None else "--"
        lines.append(
            f"         temp={data.air_temp_c:.1f}°C  rh={data.air_humidity_pct:.1f}%  "
            f"vpd={data.vpd_kpa:.2f}kPa  soil={soil}"
        )
        lines.append(
            f"         light={'ON' if data.light_enabled else 'OFF'}  "
            f"val={data.light_value}  zone={data.zone}"
        )
    elif isinstance(data, CO2SensorData):
        lines.append(f"         co2={data.co2_ppm} ppm")
    elif isinstance(data, FanControllerData):
        lines.append(
            f"         brightness={data.value}/100  "
            f"enabled={'ON' if data.enabled else 'OFF'}"
        )
    elif isinstance(data, BlowerData):
        lines.append(
            f"         setpoint={data.percent}%  "
            f"{'RUNNING' if data.running else 'idle'}"
        )

    fresh = _freshness_line(addr, bus)
    if fresh is not None:
        lines.append(fresh)

    return "\n".join(lines)


def _print_devices(devices: dict, bus: BusMaster | None = None) -> None:
    if not devices:
        print("  (no devices responding)")
        return
    for addr in sorted(devices):
        print(_format_device(addr, devices[addr], bus))


def _parse_addr_range(spec: str) -> list[int]:
    """Parse ``START-END`` (inclusive, hex or dec) into a list of addresses."""
    if "-" not in spec:
        raise ValueError(f"Range must be START-END, got {spec!r}")
    start_s, end_s = spec.split("-", 1)
    start, end = int(start_s, 0), int(end_s, 0)
    if not 0 <= start <= end <= 0xFF:
        raise ValueError(f"Range {spec!r} outside 0x00-0xFF or inverted")
    return list(range(start, end + 1))


def cmd_scan(bus: BusMaster, args: argparse.Namespace) -> None:
    addrs = _parse_addr_range(args.range) if args.range else None
    if addrs is not None:
        # Skip broadcast addr 0x00 — reads to it are meaningless and some
        # adapters treat it specially.
        addrs = [a for a in addrs if a != 0]
        print(
            f"Scanning bus (range {args.range}, {len(addrs)} addrs, "
            f"heartbeat {args.heartbeat}s)..."
        )
    else:
        print(f"Scanning bus (heartbeat {args.heartbeat}s)...")
    devices = bus.scan(
        addrs=addrs,
        inter_poll=args.scan_gap,
        heartbeat_interval=args.heartbeat,
    )
    print(f"\n{len(devices)} device(s) found:\n")
    _print_devices(devices, bus)


def cmd_poll(bus: BusMaster, args: argparse.Namespace) -> None:
    if args.fast:
        bus.fast_addrs = [int(a, 0) for a in args.fast.split(",")]
    bus.actuator_interval = args.actuator_interval
    bus.scan_interval = args.scan_interval
    bus.heartbeat_interval = args.heartbeat

    hb = "off" if args.heartbeat <= 0 else f"{args.heartbeat}s"
    print(
        f"Master mode (tiered, matches OEM GSS hub):\n"
        f"  fast     {bus.fast_addrs}  every {args.interval}s\n"
        f"  actuator {bus.actuator_addrs}  every {args.actuator_interval}s\n"
        f"  scan     {len(bus.scan_addrs)} silent slots  every {args.scan_interval}s\n"
        f"  heartbeat broadcast  {hb}\n"
        f"(Ctrl+C to stop)\n"
    )

    def on_cycle(devices):
        sys.stdout.write("\033[2J\033[H")
        print(f"--- GSS Bus  ({len(devices)} online) ---\n")
        _print_devices(devices, bus)
        sys.stdout.flush()

    bus.poll_loop(interval=args.interval, callback=on_cycle)


def cmd_read(bus: BusMaster, args: argparse.Namespace) -> None:
    addrs = [int(a.strip(), 0) for a in args.device_addr.split(",") if a.strip()]
    qty = int(args.qty) if args.qty else 16
    for j, addr in enumerate(addrs):
        if j:
            time.sleep(_READ_ADDR_GAP_S)
            print()
        try:
            resp = bus.transport.read_holding_registers(addr, start_reg=0, qty=qty)
        except ModbusTimeoutError:
            print(f"Device {addr:#04x}: no response (timeout)")
            continue
        print(f"Device {addr:#04x}  ({qty} registers):")
        for i, v in enumerate(resp.registers):
            print(f"  reg[{i:2d}] = {v:5d}  (0x{v:04X})")


def cmd_write(bus: BusMaster, args: argparse.Namespace) -> None:
    addr = int(args.device_addr, 0)
    reg = int(args.reg, 0)
    value = int(args.value, 0)
    resp = bus.transport.write_register(addr, reg, value)
    print(f"OK: dev={resp.addr:#04x} reg={resp.reg} value={resp.value}")


def cmd_fan(bus: BusMaster, args: argparse.Namespace) -> None:
    addr = int(args.device_addr, 0)
    speed = int(args.speed)
    bus.set_fan_speed(addr, speed)
    print(f"Fan {addr:#04x} speed → {speed}")


_WAKE_ATTEMPTS = 4
_WAKE_GAP_S = 0.1

# Settle window between back-to-back blind FC06s to the dimmer at 0x04.
# `POST_BLIND_WRITE_QUIET` (5 ms) covers tail TX + adapter quiet, but the
# dimmer itself needs longer to latch reg 16 before reg 10 takes effect —
# without this the second write silently no-ops and the light stays at 0
# after a previous turn-off (observed manually).
_DIMMER_LATCH_SETTLE_S = 0.05


def _broadcast_wake(bus: BusMaster, count: int = 3) -> None:
    """Broadcast a few heartbeats to silence cold-boot beacons.

    For all-blind-write commands like `cmd_light` we can't probe the
    target's FC06 echo, so we rely on a short broadcast burst to switch
    slaves out of beacon mode (see `docs/protocol-analysis.md`
    "Cold-Boot Behaviour"). 3 broadcasts spaced by `_WAKE_GAP_S`
    reliably win against the hub's ~1.5 s beacon cadence.
    """
    for _ in range(count):
        try:
            bus.broadcast_setpoints()
        except Exception:
            log.exception("Wake broadcast failed")
        time.sleep(_WAKE_GAP_S)


def _wake_bus(bus: BusMaster, target_addr: int) -> None:
    """Pull `target_addr` out of cold-boot beacon mode.

    With no master on the bus, slaves emit unsolicited FC03-format
    beacons every ~1–3 s instead of listening (see
    `docs/protocol-analysis.md` "Cold-Boot Behaviour"). A single
    broadcast usually silences them, but our frame race-loses against
    any in-flight beacon, so we loop: broadcast → poll target → bail
    out as soon as the target answers cleanly.
    """
    for attempt in range(1, _WAKE_ATTEMPTS + 1):
        try:
            bus.broadcast_setpoints()
        except Exception:
            log.exception("Wake broadcast failed (attempt %d)", attempt)
        if bus.poll_device(target_addr) is not None:
            return
        time.sleep(_WAKE_GAP_S)
    log.warning(
        "Slave 0x%02X never answered after %d wake attempts; "
        "proceeding with the write anyway",
        target_addr, _WAKE_ATTEMPTS,
    )


def _retry_on_timeout(action, label: str, attempts: int = 3) -> None:
    """Run `action()`, retrying on `ModbusTimeoutError`. Re-raises the
    last timeout if all attempts fail."""
    last: ModbusTimeoutError | None = None
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except ModbusTimeoutError as e:
            last = e
            log.warning("%s timed out (attempt %d/%d)", label, attempt, attempts)
            time.sleep(_WAKE_GAP_S)
    assert last is not None
    raise last


def cmd_blower(bus: BusMaster, args: argparse.Namespace) -> None:
    """Set blower / ventilation % (FC06 → reg 14 on 0x06).

    The OEM app labels this "Light 2" but the hardware is the blower.
    """
    addr = int(args.device_addr, 0)
    pct = int(args.percent)
    _wake_bus(bus, addr)
    _retry_on_timeout(lambda: bus.set_blower(addr, pct), f"blower {addr:#04x}")
    print(f"Blower {addr:#04x} → {pct}%")


def cmd_light(bus: BusMaster, args: argparse.Namespace) -> None:
    """Set Light 1 brightness 0-100 % (hub enable + dimmer brightness).

    Both the hub at ``0x0A`` and the dimmer at ``0x04`` accept FC06 but
    **neither echoes** on this firmware, so all writes are fired blind
    and the next poll surfaces the new state. To keep the writes from
    landing on a slave that's still in cold-boot beacon mode, we
    broadcast a few heartbeats first.

    Sequence on turn-on: dimmer enable → settle → dimmer brightness →
    hub gate. Hub is opened *last* so the dimmer is already at the
    right setpoint when light gating goes on (otherwise the light
    flashes the previous brightness for a frame).
    """
    hub_addr = int(args.hub, 0)
    dimmer_addr = int(args.dimmer, 0)
    pct = int(args.percent)
    if not 0 <= pct <= 100:
        raise SystemExit(f"percent must be 0-100 (got {pct})")

    _broadcast_wake(bus)

    if pct == 0:
        bus.transport.write_register(
            hub_addr, HUB_LIGHT_ENABLE_REG, 0, wait_for_response=False
        )
        bus.transport.write_register(
            dimmer_addr, DIMMER_BRIGHTNESS_REG, 0, wait_for_response=False
        )
        print(f"Light off (hub {hub_addr:#04x} reg 18 ← 0, dimmer {dimmer_addr:#04x} reg 10 ← 0)")
        return

    bus.transport.write_register(
        dimmer_addr, DIMMER_ENABLE_REG, 1, wait_for_response=False
    )
    time.sleep(_DIMMER_LATCH_SETTLE_S)
    bus.transport.write_register(
        dimmer_addr, DIMMER_BRIGHTNESS_REG, pct, wait_for_response=False
    )
    bus.transport.write_register(
        hub_addr, HUB_LIGHT_ENABLE_REG, 1, wait_for_response=False
    )
    print(f"Light → {pct}%  (hub {hub_addr:#04x} enable=1, dimmer {dimmer_addr:#04x} reg 10={pct})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gss-ctrl",
        description="SpiderFarmer GSS peripheral controller",
    )
    p.add_argument("port", help="Serial port (e.g. /dev/ttyUSB0)")
    p.add_argument("-b", "--baud", type=int, default=115200)
    p.add_argument(
        "-t", "--timeout", type=float, default=0.3,
        help="Per-request serial timeout in seconds (default 0.3)",
    )
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Scan bus for devices")
    scan_p.add_argument(
        "--scan-gap", type=float, default=0.05,
        help="Seconds between address polls (default 0.05)",
    )
    scan_p.add_argument(
        "--range",
        help="Address range START-END (inclusive, hex ok), e.g. 0x00-0xff. "
        "Default: OEM-polled addresses only.",
    )
    scan_p.add_argument(
        "--heartbeat", type=float, default=2.0,
        help="Seconds between setpoint broadcasts during scan; 0 disables "
        "(default 2.0 — matches the OEM hub)",
    )

    poll_p = sub.add_parser(
        "poll", help="Act as the GSS master: tiered polling + heartbeat broadcast",
    )
    poll_p.add_argument(
        "--fast",
        help="Comma-separated hex addresses polled every cycle "
        "(default: 0x03,0x0A — the OEM's primary sensors)",
    )
    poll_p.add_argument(
        "--interval", type=float, default=DEFAULT_FAST_INTERVAL,
        help=f"Seconds between fast-tier cycles (default {DEFAULT_FAST_INTERVAL})",
    )
    poll_p.add_argument(
        "--actuator-interval", type=float, default=DEFAULT_ACTUATOR_INTERVAL,
        help=f"Seconds between actuator-tier polls (default {DEFAULT_ACTUATOR_INTERVAL})",
    )
    poll_p.add_argument(
        "--scan-interval", type=float, default=DEFAULT_SCAN_INTERVAL,
        help=f"Seconds between silent-slot discovery scans (default {DEFAULT_SCAN_INTERVAL})",
    )
    poll_p.add_argument(
        "--heartbeat", type=float, default=DEFAULT_HEARTBEAT_INTERVAL,
        help=f"Seconds between setpoint broadcasts; 0 to disable "
        f"(default {DEFAULT_HEARTBEAT_INTERVAL})",
    )

    read_p = sub.add_parser("read", help="Read registers from one or more devices")
    read_p.add_argument(
        "device_addr",
        help="Device address(es), comma-separated hex (e.g. 0x0A or 0x03,0x0A)",
    )
    read_p.add_argument("qty", nargs="?", help="Number of registers (default 16)")

    write_p = sub.add_parser("write", help="Write a single register (FC06)")
    write_p.add_argument("device_addr")
    write_p.add_argument("reg", help="Register number")
    write_p.add_argument("value", help="Value to write")

    fan_p = sub.add_parser("fan", help="Set fan speed (0-25)")
    fan_p.add_argument("device_addr")
    fan_p.add_argument("speed", help="Speed value 0-25")

    blower_p = sub.add_parser(
        "blower",
        help="Set ventilation blower %% (FC06 → reg 14; default addr 0x06)",
    )
    blower_p.add_argument("device_addr", nargs="?", default="0x06")
    blower_p.add_argument("percent", help="Percent value 0-100")

    light_p = sub.add_parser(
        "light",
        help="Set Light 1 brightness 0-100 %% (hub enable + dimmer brightness)",
    )
    light_p.add_argument("percent", help="Brightness 0-100 (0 = off)")
    light_p.add_argument(
        "--hub", default=hex(DEFAULT_HUB_ADDR),
        help=f"Sensor hub address (default {hex(DEFAULT_HUB_ADDR)})",
    )
    light_p.add_argument(
        "--dimmer", default=hex(DEFAULT_DIMMER_ADDR),
        help=f"Dimmer address (default {hex(DEFAULT_DIMMER_ADDR)})",
    )

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    handlers = {
        "scan": cmd_scan,
        "poll": cmd_poll,
        "read": cmd_read,
        "write": cmd_write,
        "fan": cmd_fan,
        "blower": cmd_blower,
        "light": cmd_light,
    }

    with RS485Transport(args.port, baudrate=args.baud, timeout=args.timeout) as transport:
        bus = BusMaster(transport=transport)
        handlers[args.command](bus, args)


if __name__ == "__main__":
    main()
