# REC-BMS-to-Victron-Greenline

Personal integration of a REC BMS, CZone digital switching and a Greenline
hybrid drive into a Cerbo GX (Venus OS).

Two standalone D-Bus drivers do the work that Node-RED flows used to:

| | |
|---|---|
| [`dbus-recbms/`](dbus-recbms/) | REC-BMS main bank → `com.victronenergy.battery` plus a VRM "Max Charge" slider |
| [`dbus-czone/`](dbus-czone/) | CZone switching → one multi-output `com.victronenergy.switch` bank, circuits discovered from the bus |

The remaining Node-RED flows (12 V batteries, Greenline e-drive, solar priority,
device-instance registry) are the `*.json` files at the top level.

Superseded flows live in [`archive/`](archive/) — kept for rollback only, never
deployed alongside the drivers that replaced them.

**Start with [`specification.md`](specification.md)** — CAN topology, the CZone
and REC-BMS protocols, the drivers and every flow are documented there.

Other references: [`GreenlineFindings.md`](GreenlineFindings.md) (hybrid drive
CAN decode), [`Decoded.md`](Decoded.md) and [`BatteryCAN.md`](BatteryCAN.md)
(BMS frames), [`ydnb07.md`](ydnb07.md) + [`YDNB.CFG`](YDNB.CFG) (CAN bridge).

Helper scripts: `cerbo_ssh.py` (read-only command runner), `nmea_capture.py` /
`nmea_decode.py` (bus capture and switching-event decode), `verify_pinning.py`
(device-instance regression check), `edrive_temps.py` (live e-drive MOSFET /
MCU-HCU / drive temperatures). All take the host from `CERBO_HOST` and the
password from `CERBO_PASS`.
