# REC-BMS-to-Victron-Greenline

Personal integration of a REC BMS, CZone digital switching, the 12 V house
batteries and a Greenline hybrid drive into a Cerbo GX (Venus OS).

All of it runs as standalone D-Bus drivers. Every function used to be a Node-RED
flow; as of 2026-08-21 none is, so nothing on the boat depends on Signal K or
Node-RED any more.

| | |
|---|---|
| [`dbus-recbms/`](dbus-recbms/) | REC-BMS main bank → `com.victronenergy.battery` plus a VRM "Max Charge" slider. Ships `dbus-solarpriority` too: harvest-and-burn solar priority on `com.victronenergy.switch` |
| [`dbus-czone/`](dbus-czone/) | CZone switching → one multi-output `com.victronenergy.switch` bank, circuits discovered from the bus |
| [`dbus-batteries/`](dbus-batteries/) | 12 V batteries from N2K PGN 127508/127506/127489 → `com.victronenergy.battery`, with which batteries to forward chosen at runtime over D-Bus |
| [`dbus-edrive/`](dbus-edrive/) | Greenline 6GK drives → two `com.victronenergy.motordrive`, read-only |

Deploy with `python deploy_cerbo.py <name>` (one SSH session, config guard,
backups, verification — it encodes the rules in [`CLAUDE.md`](CLAUDE.md)).
`python test_drivers.py` runs `dbus-batteries` and `dbus-edrive` off the boat
against stubbed D-Bus and CAN.

Superseded flows live in [`archive/`](archive/) — kept for rollback only, never
deployed alongside the drivers that replaced them.

**Start with [`specification.md`](specification.md)** — CAN topology, the CZone
and REC-BMS protocols, and every driver are documented there.

Other references: [`CZoneEcosystem.md`](CZoneEcosystem.md) (what the
open-source CZone projects know, and what would misbehave on this bus),
[`GreenlineFindings.md`](GreenlineFindings.md) (hybrid drive
CAN decode), [`Decoded.md`](Decoded.md) and [`BatteryCAN.md`](BatteryCAN.md)
(BMS frames), [`ydnb07.md`](ydnb07.md) + [`YDNB.CFG`](YDNB.CFG) (CAN bridge).

Helper scripts: `cerbo_ssh.py` (read-only command runner), `nmea_capture.py` /
`nmea_decode.py` (bus capture and switching-event decode), `zcf_parse.py`
(read a CZone `.zcf`: circuits, channels, categories, momentary/latching),
`verify_pinning.py`
(device-instance regression check), `edrive_temps.py` (live e-drive MOSFET /
MCU-HCU / drive temperatures). All take the host from `CERBO_HOST` and the
password from `CERBO_PASS`.
