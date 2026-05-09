# SpiderFarmer GSS Modbus Protocol Analysis

Reverse-engineered protocol reference for the SpiderFarmer GSS bus.
Captured over several hours of Saleae logic traces on a running rig;
the live register layout ships in
[`spiderwire/registers.py`](../src/spiderwire/registers.py)
and [`docs/device-map.md`](./device-map.md).

## Physical Layer

| Parameter | Value |
|-----------|-------|
| Interface | RS-485 (differential pair) |
| Baud rate | **115200** |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Byte order | Big-endian (register data) |
| CRC | Modbus CRC-16 (polynomial 0xA001, init 0xFFFF) |

### RJ12 pinout (see `docs/hw-notes.md` for wire colours)

| RJ12 pin | Signal |
|----------|--------|
| RS-485 A | black |
| RS-485 B | yellow |
| +12 V    | white / blue |
| GND      | green |

## Protocol

Standard **Modbus RTU**. Only three function codes observed:

| FC | Name | Direction | Usage |
|----|------|-----------|-------|
| 0x03 | Read Holding Registers | master → slave → master | Poll sensors / identity |
| 0x06 | Write Single Register | master → slave | Set individual setpoints |
| 0x10 | Write Multiple Registers | master → broadcast (0x00) | Heartbeat / setpoint broadcast |

## Bus Architecture

One bus master (the GSS controller MCU, no Modbus address) polls
peripherals on a shared RS-485 bus. Discovered devices and the
OEM-expected register count per address slot are captured in
[`registers.DEFAULT_QTY`](../src/spiderwire/registers.py)
and summarised in [`device-map.md`](./device-map.md).

## Register Map

See [`device-map.md`](./device-map.md) for the per-device tables
(header, sensor hub, CO₂ sensor, light dimmer, blower). Highlights:

- Every device exposes the same 10-register header (addr, magic byte
  `0xAA`, firmware version, model, serial fragment, type/subtype,
  hardware revision).
- Sensor hub (`0x0A`) holds air T/RH/soil T/PPFD and the Light 1
  enable flag (reg 18). FC06 writes to reg 18 are accepted but
  **never echoed** on this firmware — fire blind, then re-read to
  confirm.
- Light dimmer (`0x04`) takes 0-100 % brightness on reg 10 via FC06
  and **never echoes** — writes must be fired blind.
- Blower (`0x06`, OEM UI "Light 2") takes 0-100 % on reg 14 via FC06
  and **does echo**.

## Bus Timing & Polling Pattern

The OEM hub does **not** poll every slot uniformly. Four independent
schedules interleave, all reimplemented in
[`BusMaster.tick()`](../src/spiderwire/bus.py):

| Tier | Addresses / Action | Period |
|------|---------------------|--------|
| A — Fast sensors | `0x03` (CO₂), `0x0A` (hub) | **~1.0 s** |
| B — Actuators | `0x04` (dimmer), `0x06` (blower) | **~2.5 s** |
| C — Silent-slot scan | 12 unpopulated addresses | **~7.0 s** |
| D — Broadcast heartbeat | FC `0x10` → `0x00`, 26 regs @ 1001 | **~3.5 s** |

Tier B is prepended to Tier A on cycles where its deadline elapses;
Tier D fires between fast pairs, never mid-request; Tier C is a single
~1.7 s burst of 12 × 140 ms timeouts every ~7 s.

### Example wire sequence (one full tick)

```
0x04 → 0x06 → 0x03 → 0x0A        (Tier B due, then Tier A)
0x03 → 0x0A                       (Tier A only, subsequent ticks)
0x00 FC10 broadcast                (Tier D, every ~3.5 s)
0x10 → 0x02 → 0x01 → …            (Tier C, every ~7 s)
```

## Broadcast Heartbeat (Tier D)

FC `0x10` to addr `0x00`, writing 26 registers starting at reg 1001.
Observed payload is constant across captures:

| Register | Value | Interpretation |
|----------|-------|---------------|
| 1001-1008 | 0 | Reserved |
| 1009 | 7 | Mode/state flag |
| 1010 | 0 | Reserved |
| 1011 | 1088-1112 | Setpoint (CO₂? still unresolved) |
| 1012-1026 | 0 | Reserved |

Peripherals drop into a master-missing fail-safe if the heartbeat
stops, so the broadcast has to keep running even when nothing changes
(`BusMaster` fires it every `heartbeat_interval`, default 3.5 s).

## Cold-Boot Behaviour (no master on the bus)

When the master is powered but silent, peripherals emit unsolicited
FC `0x03`-**response**-formatted beacons on their assigned Modbus
address. Byte-for-byte identical to what they'd return to a real read
at reg 0, at a fixed cadence:

- Sensor hub (`0x0A`) every ~1–1.5 s
- Blower (`0x06`) every ~2–3 s (last CRC byte often gets a framing
  error and is stripped)

Once the master starts transmitting (either polls or the Tier-D
broadcast), beacons stop and peripherals switch to pure request /
response mode. There is **no pairing / enrol / init** function code:
"pairing" in the OEM app is just the master starting to write
device-specific enable bits (hub reg 18 = 1, dimmer reg 16 = 1, …) via
ordinary FC 0x06 writes.

Practical consequence: a clean master can simply power on and start
polling — no discovery handshake needed.

## Fan Speed Control (24-register actuator on 0x04)

The OEM master writes to `0x04` reg 10 via FC 0x06, ramping the fan
(or light, depending on wiring) from 11 → 25 in steps every ~5 s,
then resetting to 0 and restarting from 11. Below ~11 stalls the
motor on a true duct fan; on this rig the same SKU is wired as the
main grow-light dimmer and the value is a direct 0-100 % brightness
(see `device-map.md` §0x04 for the wiring discussion).

Reg 16 is an enable latch that must be `1` for reg 10 writes to take
effect. The OEM leaves it set between sessions; `gss-ctrl light` and
the HA integration re-arm it at startup (blind — the dimmer never
echoes).
