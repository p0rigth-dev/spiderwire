# SpiderWire

Open Modbus RTU library and `gss-ctrl` CLI for the **SpiderFarmer GSS**
peripheral bus (inline fans, CO₂ sensor, sensor hub, light driver). The
OEM GSS hub is a Modbus RTU master over a proprietary RJ12 wire layout;
SpiderWire replaces that hub so you can drive the bus from any host —
locally, no cloud account.

Two surfaces, one library:

| Surface | Role | What ships with it |
|---|---|---|
| `spiderwire` Python package | Transport, register map, tiered bus master | `bus.py`, `protocol.py`, `registers.py`, `transport.py` |
| `gss-ctrl` CLI | Test / ops / manual control over USB-RS485 | `gss-ctrl scan / poll / read / write / fan / blower / light` |

Companion repo: **[`spiderfarmer-ha`](https://github.com/1am/spiderfarmer-ha)** — a
Home Assistant integration that pulls SpiderWire in as a git submodule
and exposes the bus as HA entities.

- [PCB preview](https://www.youtube.com/watch?v=0Yn37gflFO0)

## Install

```bash
git clone https://github.com/1am/spiderwire.git
cd spiderwire
make install                          # uv sync — Python 3.13 + pyserial
```

## CLI quickstart

```bash
make scan    PORT=/dev/ttyUSB0        # discover devices on the bus
make poll    PORT=/dev/ttyUSB0        # master mode: tiered poll + heartbeat
make read    PORT=... ADDR=0x0A QTY=28
make fan     PORT=... ADDR=0x04 SPEED=15
make light   PORT=... PCT=50
make blower  PORT=... PCT=40
```

Without the OEM GSS hub on the bus, **`gss-ctrl poll`** takes over
master duties: tiered polling (~1 s fast, ~2.5 s actuators, ~7 s scan)
plus the setpoint heartbeat broadcast (~3.5 s), matching the OEM
cadence peripherals expect.

Full CLI: `gss-ctrl --help`. Commands: `scan`, `poll`, `read`, `write`,
`fan`, `blower`, `light`.

## Library use

```python
from spiderwire.transport import RS485Transport
from spiderwire.bus import BusMaster

with RS485Transport("/dev/ttyUSB0", baud=115200) as tx:
    bus = BusMaster(tx)
    for snapshot in bus.tick_forever(interval=1.0, heartbeat=3.5):
        print(snapshot)
```

See `bus.py` for the tiered scheduler, `registers.py` for the per-device
register map, and `transport.py` for the RS-485 framer.

## Layout

```
spiderwire/
├── spiderwire/                  Python package (lib)
│   ├── bus.py                   tiered master + heartbeat scheduler
│   ├── protocol.py              Modbus RTU framing + CRC
│   ├── registers.py             per-device register map + decoders
│   └── transport.py             RS-485 framer over pyserial
├── gss_ctrl_pc/                 gss-ctrl CLI (stand-in for the OEM master)
├── docs/                        protocol + device map reference
├── pyproject.toml               builds the `spiderwire` wheel + `gss-ctrl` script
└── Makefile                     dev shortcuts
```

## Docs

- [`docs/device-map.md`](docs/device-map.md) — per-device register map
  for every address observed on the bus.
- [`docs/protocol-analysis.md`](docs/protocol-analysis.md) — protocol
  reference: physical layer, function codes, tiered polling, heartbeat.
- [`docs/hw-notes.md`](docs/hw-notes.md) — OEM hub board notes and RJ12
  pinout.

## License

Copyright (C) 2026 1AM

[GNU General Public License v3.0 or later](LICENSE) — free for any
use, including commercial, with one strong condition: any product or
distribution that includes this code (modified or unmodified) must
itself be released under GPLv3, with full source code available to its
users.

That means a vendor — including SpiderFarmer or any successor — cannot
ship this code inside a closed-source product. They have two options:

1. Open-source their integration / firmware under GPLv3, with
   attribution preserved, **or**
2. Obtain a separate commercial license from me.

## Disclaimer

This software is provided "as is", without warranty of any kind, express
or implied, including but not limited to the warranties of
merchantability, fitness for a particular purpose, and non-infringement.

This project interacts with mains-powered grow equipment over an RS-485
bus. Incorrect wiring, miswired connectors, unsupported devices, or
misuse of the protocol can damage hardware, void manufacturer warranties,
cause fire, or result in personal injury. You are solely responsible for
verifying the correctness of your wiring, your device configuration, and
the commands you send.

In no event shall the author or contributors be liable for any direct,
indirect, incidental, special, exemplary, or consequential damages —
including but not limited to damage to equipment, crops, property, or
persons — arising from the use of, or inability to use, this software.

Use at your own risk.
