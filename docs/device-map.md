# Device Map — Current Bus State

Snapshot of what actually lives on the RS-485 bus as captured by the
`gss-ctrl` CLI (`gss-ctrl <serial> scan` / `read`, or `make scan` /
`make read` from the repo root). Example port:
`/dev/cu.usbserial-130` (macOS).
See [`protocol-analysis.md`](./protocol-analysis.md) for the full
protocol write-up and historical observations; this file is a concise,
per-device reference grouped by role.

**Code:** parsing, tier lists, and `DEFAULT_QTY` live in
[`registers.py`](../src/spiderwire/registers.py);
orchestration in
[`bus.py`](../src/spiderwire/bus.py).
The Home Assistant custom component
[`custom_components/spiderfarmer/`](https://github.com/1am/spiderfarmer-ha/tree/main/custom_components/spiderfarmer/)
drives the same `BusMaster` + `RS485Transport` stack (see below).

## Summary

| Addr | Role                  | Device                                                                                                     | Header type (reg 6) | Model (reg 4) | FW (reg 2–3)          | HW (reg 7) | Reliable register qty                                       |
| ---- | --------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------- | ------------- | --------------------- | ---------- | ----------------------------------------------------------- |
| 0x03 | **Sensor**            | CO₂ sensor                                                                                                 | `0x0308`            | `0x0330`      | `"05.12"` (ASCII)     | `0x0202`   | 13                                                          |
| 0x0A | **Sensor** + actuator | Sensor hub (air T/RH, soil T, PPFD); `reg 19` is the *reported* light value, not a setpoint                | `0x0400`            | `0x0330`      | `"05.12"` (ASCII)     | `0x0202`   | 28                                                          |
| 0x04 | **Actuator**          | **Light dimmer (primary)** — accepts FC06 → reg 10 in **percent (0-100)**, never echoes writes             | `0x0301`            | `0x0426`      | `0xF784C96D` (binary) | `0x0105`   | 24 — silent to FC03 in current rig, but acts on FC06 writes |
| 0x06 | **Actuator**          | **Blower / ventilation** (OEM UI calls it *"Light 2"*) — FC06 → **reg 14** in percent (0-100), *does* echo | `0x0204`            | `0x043D`      | `0xF784C90F` (binary) | `0x0102`   | 16                                                          |

`DeviceHeader.type_name` in `registers.py` (`LIGHT=0x02`, `FAN=0x03`,
`SENSOR_HUB=0x04`) maps **only the high byte** of register 6. That is
why the CLI prints `fan` for the CO₂ sensor at `0x03` (header type high
byte is `0x03`). The subtype in the low byte is what actually
distinguishes CO₂ (`0x08`) from the duct fan (`0x01`).

## Tier placement (OEM orchestration)

`BusMaster.tick()` (see `bus.py`) interleaves the same four schedules the
OEM GSS hub uses (see `protocol-analysis.md` §"Bus Timing"). On each call,
actuators run **when their ~2.5 s deadline has elapsed** (before the fast
pair), the fast sensor pair **always** runs, then heartbeat / silent scan
fire on their own timers:

| Tier                    | Cadence                        | Addresses / action                                                       |
| ----------------------- | ------------------------------ | ------------------------------------------------------------------------ |
| B — actuators           | ~2.5 s                         | `0x04`, `0x06` (FC03 read when due)                                      |
| A — fast sensors        | every tick (~1.0 s outer loop) | `0x03`, `0x0A`                                                           |
| D — heartbeat broadcast | ~3.5 s                         | FC 0x10 → 0x00, 26 regs @ 1001                                           |
| C — silent-slot scan    | ~7.0 s                         | `0x10, 0x02, 0x01, 0x0C, 0x05, 0x07, 0x0B, 0x0D, 0x0E, 0x08, 0x0F, 0x13` |

Moving a device between tiers is editing `FAST_ADDRS`,
`ACTUATOR_ADDRS`, or `SCAN_ADDRS` in `spiderwire/registers.py` (or
mutating `bus.fast_addrs` / `actuator_addrs` / `scan_addrs` at runtime).

### Home Assistant (`spiderfarmer`)

The integration's coordinator calls `BusMaster.tick()` once per HA
update (`DEFAULT_SCAN_INTERVAL` = 1 s in `custom_components/spiderfarmer/const.py`), so the Python
master tracks the OEM cadence internally.

| HA platform | What appears                                                                                                                                                                                    | Bus mapping                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Sensor**  | Air temp, humidity, VPD, soil temp, PPFD on the hub; CO₂ ppm on `0x03`                                                                                                                          | Typed `SensorHubData` / `CO2SensorData` from coordinator data; unique ids `sf_0x0a_<key>` / `sf_0x03_co2` (hex lower case).                                                                                                                                                                                                                                                                    |
| **Light**   | One **Light 1** entity, attached to the **dimmer device (`0x04`)** so the user sees it under "Light Driver" rather than under the sensor hub                                                    | State from hub regs **18** (on/off) and **19** (reported %). `turn_on` writes hub reg 18 → 1, dimmer `0x04` reg **16** → 1 and reg **10** → % — **all FC06 blind**, neither the hub nor the dimmer echoes writes on this firmware. `turn_off` writes hub reg 18 → 0 and dimmer reg 10 → 0. Unique id stays `sf_0x0a_light1` (legacy, hub-scoped) so existing installs don't lose their entity. |
| **Fan**     | **Blower** for each `BlowerData` (today: `0x06`)                                                                                                                                                | HA fan entity with `translation_key` blower; unique id `sf_0x06_blower`. Writes FC06 → reg **14** (0–100 %), waits for echo.                                                                                                                                                                                                                                                                   |
| **Number**  | **Light brightness** (under "Light Driver") and **Blower speed** (under "Blower") — direct 0–100 % sliders visible on the dashboard / tile cards, complementing the on/off light + fan entities | Brightness writes the same dimmer reg 10 (with reg 16 = 1 latch) as the light entity but does **not** touch the hub gate. Blower speed writes blower reg 14, identical to the fan entity's `set_percentage`. Unique ids `sf_<dimmer>_brightness` / `sf_<blower>_blower_percent`.                                                                                                               |

All four platforms use a **dynamic discovery** loop (`setup_dynamic_entities` in `entity.py`): entities are added both at first refresh **and** on subsequent coordinator updates as new addresses appear. This is what makes the CO₂ sensor at `0x03` show up reliably even when it misses the very first poll burst — without it, anything not on the bus during HA's startup tick would stay invisible until a full reload.

Device names in the HA UI are derived from the *parsed* device class
(`SensorHubData` → "Sensor Hub", `CO2SensorData` → "CO₂ Sensor",
`FanControllerData` → "Light Driver", `BlowerData` → "Blower" — see
`_ROLE_NAMES` in `entity.py`), **not** from `DeviceHeader.type_name`.
The OEM SKUs on this rig all mis-identify themselves in register 6
(0x03 CO₂ reports `fan`, 0x04 dimmer reports `fan`, 0x06 blower reports
`light`); using `type_name` for the device label is what produced the
"Blower device with a CO₂ sensor inside" misnaming on first install.

There is no separate switch platform: light enable is folded into the
light entity. Legacy `BusMaster` helpers (`set_fan_speed` /
`set_fan_enable` on reg 10 / 16) remain for a true fan-class actuator if
one is wired at `0x04` on another rig.

## Common Header (all devices, regs 0–9)

| Reg | Meaning                         | Notes                                                                                |
| --- | ------------------------------- | ------------------------------------------------------------------------------------ |
| 0   | Self-reported Modbus address    | matches polling addr                                                                 |
| 1   | `0xAA` magic byte << 8 \| addr  | e.g. `0xAA0A` for `0x0A`                                                             |
| 2–3 | Firmware version                | ASCII `"HH.LL"` on SF-made units, binary `0xF784xxxx` on OEM‑branded LED/fan drivers |
| 4   | Model / product code            |                                                                                      |
| 5   | Serial number fragment          |                                                                                      |
| 6   | Device type (hi) : subtype (lo) |                                                                                      |
| 7   | Hardware version                |                                                                                      |
| 8–9 | Reserved                        | always `0x0000`                                                                      |

---

## Sensors

### `0x03` — CO₂ sensor  *(13 registers)*

Example FC03 read (`qty` ≥ 13; values illustrative):

| Reg    | Hex      | Decimal | Interpretation                     |
| ------ | -------- | ------- | ---------------------------------- |
| 0      | `0x0003` | 3       | self-addr                          |
| 1      | `0xAA03` | 43523   | magic + addr                       |
| 2      | `0x3035` | —       | FW hi = `"05"`                     |
| 3      | `0x3132` | —       | FW lo = `"12"`                     |
| 4      | `0x0330` | 816     | model                              |
| 5      | `0x7518` | 29976   | serial frag                        |
| 6      | `0x0308` | 776     | type / subtype (sensor-class, CO₂) |
| 7      | `0x0202` | 514     | hw                                 |
| 8–9    | `0x0000` | 0       | reserved                           |
| **10** | `0x01B5` | **437** | **CO₂ concentration [ppm]**        |
| 11–12  | `0x0000` | 0       | reserved (outside the 13-reg spec) |

The scan uses `DEFAULT_QTY[0x03]=13`, the response parses to `CO2SensorData`, and `co2_ppm = reg[10]`. Observed range across recent runs: **430–466 ppm**, consistent with an occupied indoor room. The value is confirmed real sensor data (not stale, not from another device).

### `0x0A` — Sensor hub (air T / RH / soil T / PPFD) + Light 1 enable *(28 registers)*

Full 28-register reads work reliably since `RS485Transport` switched
to deterministic, fixed-size frame reads (`spiderwire.transport`);
the old silence-timing heuristic truncated long frames. The
authoritative snapshot below comes from
`docs/capture-20260418-1135.sal` (Saleae, Session 9), where the OEM
GSS hub pulls the full 61-byte response with CRC OK.

| Reg    | Hex                | Decimal        | Interpretation                                                                                                                                                                                                                                                   |
| ------ | ------------------ | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0–9    | —                  | —              | header (self-addr `0x000A`, magic `0xAA0A`, FW `"05.12"`, model `0x0330`, type `0x0400`, hw `0x0202`)                                                                                                                                                            |
| **10** | `0x00F5`           | **245**        | **Air temperature × 10 → 24.5 °C**                                                                                                                                                                                                                               |
| **11** | `0x01B0`           | **432**        | **Air humidity × 10 → 43.2 %RH**                                                                                                                                                                                                                                 |
| **12** | `0xFC18`           | −1000 (signed) | **Soil temperature × 10** — `-1000` means probe not connected (`soil_temp_c = None`)                                                                                                                                                                             |
| **13** | `0x0146`           | **326**        | **PPFD in µmol/m²/s** (matches in-app display 325–326)                                                                                                                                                                                                           |
| 14     | `0x0163`           | 355            | co-tracking light channel — peak / IR / DLI TBD                                                                                                                                                                                                                  |
| 15     | `0x23F8`           | 9208           | constant — calibration / device config                                                                                                                                                                                                                           |
| 16–17  | `0x0000`           | 0              | reserved                                                                                                                                                                                                                                                         |
| **18** | `0x0001`           | 1              | **Light 1 enable flag** — written by master via **blind** FC 0x06 (the hub never echoes the write — `gss-ctrl light` and HA both timed out waiting for an echo before we switched to blind writes; reads of reg 18 still confirm the new value on the next poll) |
| **19** | `0x0064`           | 100            | **Light 1 *reported* value** (read-only status echo of the dimmer at `0x04`; writing it does not change brightness — confirmed absent from all FC06 traffic in `capture-20260418-1152.sal`)                                                                      |
| 20–21  | `0x0023`, `0x000A` | 35, 10         | zone / group (reg 21 matches device addr = 10)                                                                                                                                                                                                                   |
| 22–27  | `0x0000`           | 0              | reserved / status flags                                                                                                                                                                                                                                          |

`BusMaster.set_light_enable(addr=0x0A, enable)` writes reg 18. VPD is
computed client-side from reg 10 + reg 11 via the Tetens SVP formula in
`SensorHubData.vpd_kpa`. PPFD from reg 13 is exposed as a sensor entity
with translation key `ppfd` on the hub device in Home Assistant.

---

## Actuators

### `0x04` — Light dimmer *(24 registers)*

Header type `0x0301` reads as "fan subtype" via `registers.py`, but the actual device wired at `0x04` in the current rig is the **main grow-light dimmer** — confirmed in `docs/capture-20260418-1152.sal`: every time the user moved the OEM app's brightness slider (0 → 53 → 30 → 53 → 0 %), the hub emitted FC06 writes to `0x04 reg 10` carrying the percent value directly. Whether this is a re-flashed fan MCU or the OEM assigning the fan product code to their light driver is unresolved; treat `reg 10` here as a 0-100 % brightness setpoint.

| Reg    | Interpretation                                                 | Observed                                                                                                                                                                                                                                                                                                                                                           |
| ------ | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0–9    | header                                                         | type `0x0301`, model `0x0426`, FW `0xF784C96D`, hw `0x0105`                                                                                                                                                                                                                                                                                                        |
| **10** | **Brightness 0-100 %** (direct percent)                        | written by master via FC 0x06; OEM writes it blind (see below)                                                                                                                                                                                                                                                                                                     |
| 11–15  | reserved                                                       | `0`                                                                                                                                                                                                                                                                                                                                                                |
| **16** | **Enable flag** — must be `1` for reg 10 writes to take effect | Throughout `capture-20260418-1152.sal` the FC03 responses show `reg[16] = 1`; the OEM sets it during an earlier session and the value sticks. If you start a fresh master without setting this bit, reg 10 writes land on the dimmer but produce no visible change. `gss-ctrl light` (and the HA light entity) writes `reg 16 ← 1` blind alongside the brightness. |
| 17–23  | reserved                                                       | `0`                                                                                                                                                                                                                                                                                                                                                                |

**No Modbus echo on FC06 writes.** The device acts on the command but never sends the 8-byte echo back — `gss-ctrl light` and the HA integration use `write_register(..., wait_for_response=False)` for this address so they don't stall 300 ms per write. FC03 reads also time out most of the time in the current rig.

The legacy fan helpers — `BusMaster.set_fan_speed(addr, 0..25)` / `set_fan_enable(addr, bool)` — still target `reg 10` / `reg 16`. If you genuinely have a duct fan wired here (different rig), speed is 0-25 and speeds below ~11 stall the motor; see `protocol-analysis.md` §"Fan Speed Control".

### `0x06` — Blower / ventilation *(16 registers)*

The OEM app labels this "Light 2" but the 0x06 SKU is physically wired
to the blower / ventilation fan. Confirmed in
`docs/capture-20260418-1452.sal`: the user drove the OEM slider
**0 → 74 → 60 → 25 → OFF** and every change produced an FC06 write to
`0x06 reg 14` carrying the percent directly; reg 10 stays `0` the entire
capture. The device's own 16-register broadcast echoes the live
setpoint back in reg 14 and latches reg 12 to 1 whenever the blower is
running.

| Reg    | Interpretation       | Notes                                                       |
| ------ | -------------------- | ----------------------------------------------------------- |
| 0–9    | header               | type `0x0204`, model `0x043D`, FW `0xF784C90F`, hw `0x0102` |
| 10     | unused               | `0x0000` throughout every capture                           |
| 11     | paired flag          | `0x0001` once the controller has linked, latches            |
| **12** | **running flag**     | `1` whenever the blower is actively driven, `0` when off    |
| 13     | reserved             | `0x0000`                                                    |
| **14** | **Blower % (0-100)** | master writes via FC 0x06; slave echoes                     |
| 15     | reserved             | `0x0000`                                                    |

`BusMaster.set_blower(addr, percent)` issues `write_register` on reg 14
(`BLOWER_SETPOINT_REG` in `registers.py`) and waits for the standard FC06
response echo. The OEM UI enforces a 25 % floor above 0 (it refuses to
slide lower without going to OFF), but the device itself accepts any
0–100 value — `set_blower` and the HA blower entity allow lower values
if desired.

---

## Addresses polled but silent

The OEM master polls `0x01, 0x02, 0x05, 0x07, 0x08, 0x0B–0x10, 0x13` every poll cycle. None of these respond in the current rig. The per-address register-count table in `registers.py` (`DEFAULT_QTY`) encodes the OEM's expected device class at each slot; nothing is physically installed there today.

> During `gss-ctrl poll`, the top-of-screen "N online" counter sometimes shows phantom devices at those addresses with data identical to `0x03`'s. Those are garbled frames that happened to pass CRC — not real devices. Only the clean `scan` output above should be trusted for device discovery.

## Broadcast heartbeat (master → 0x00)

FC `0x10` @ register 1001, 26 words. `BusMaster.setpoints` holds the
live payload; `broadcast_setpoints()` ships it, and `tick()` fires one
every `heartbeat_interval` seconds (default 3.5 s, matching the OEM
hub). Peripherals enter a master-missing fail-safe if the heartbeat
stops. See `protocol-analysis.md` §"Broadcast Writes" for the
per-register layout; default non-zero values are `reg[1009]=7` and
`reg[1011]=1112` (both observed constant in every capture).
