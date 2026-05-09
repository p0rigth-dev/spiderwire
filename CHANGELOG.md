# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-05-09

Initial release.

### Added

- `spiderwire` library: Modbus RTU protocol (`protocol`), RS-485 transport
  (`transport`), per-device register map and decoders (`registers`), and
  tiered bus master with setpoint heartbeat (`bus`).
- `gss-ctrl` CLI: `scan`, `poll`, `read`, `write`, `fan`, `blower`,
  `light` commands. Acts as a stand-in master for the OEM SpiderFarmer
  GSS hub on a USB-RS485 adapter.
- Reference docs: `docs/protocol-analysis.md`, `docs/device-map.md`,
  `docs/hw-notes.md`.

[Unreleased]: https://github.com/1am/spiderwire/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/1am/spiderwire/releases/tag/v0.1.0
