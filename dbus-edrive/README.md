# dbus-edrive

Standalone Venus OS driver for the two Greenline 6GK electric drives. Replaces
the Node-RED "Greenline E-Drive" flow (`archive/GreenlineEDriveFlow.json`).

Runs as a daemontools service, comes up seconds after D-Bus at boot,
independent of Node-RED / Signal K, and is untouched by Node-RED deploys.

**Strictly read-only.** The driver binds a receive filter set on `can0` and
never writes a frame, so it cannot influence the drive system.

## What it publishes

```
com.victronenergy.motordrive.edrive_port   (device instance 210)
com.victronenergy.motordrive.edrive_stbd   (device instance 211)
```

Instances carried over unchanged from `InstanceRegistry.json`, so VRM history
survives the migration.

The Victron `motordrive` class has exactly **nine** data paths:

```
/Dc/0/Voltage  /Dc/0/Current  /Dc/0/Power
/Motor/RPM  /Motor/Direction
/Motor/Temperature  /Controller/Temperature  /Coolant/Temperature
```

Everything else the drives send is published under `/EDrive/` on the same
service — in the flow this telemetry could only reach a debug node; on D-Bus it
is visible to Signal K, MQTT and the HTML5 app like anything else:

```
/EDrive/TorqueNm            motor torque, N·m
/EDrive/TorquePercent       J1939 percent torque (61451)
/EDrive/ThrottlePercent     65363, see the calibration caveat below
/EDrive/PhaseCurrentRms     0x38x float 2
/EDrive/PhaseCurrentPeak    61452 w4 ÷ 20
/EDrive/MechanicalPower     T·ω, W
/EDrive/MosfetTemperature   same sensor as /Coolant/Temperature, unmapped name
/EDrive/Running             status byte == 1
/EDrive/StatusByte          raw 0x18x byte 0
```

Set `publish_extra = false` in `config.ini` to drop the `/EDrive/` tree.

## The wire

Full working in `GreenlineFindings.md`; this is the summary the code
implements.

**CANopen TPDOs, 11-bit ids**, node `0x0A` = PORT and `0x0B` = STARBOARD, IEEE
floats at full resolution, ~10 Hz:

| id | float 1 | float 2 |
|---|---|---|
| `0x18x` | status byte (`01` = running) | — |
| `0x28x` | MOSFET temp °C | drive (motor) temp °C |
| `0x38x` | motor voltage V | phase current RMS A |
| `0x48x` | motor **torque N·m** | motor RPM |

**J1939 / NMEA 2000, 29-bit**, quantized — but two quantities have no float
source at all:

| PGN | Content |
|---|---|
| 61451 | byte 1 − 125 = torque % |
| 61452 | w2 = DC current (calibrated fit), w4 ÷ 20 = phase peak A |
| 61453 | w0/w1/w2 × 0.03125 − 273 = MOSFET / drive / MCU-HCU temp °C |
| 65363 | byte 0 = instance, bytes 1-2 LE ÷ 9.280 = throttle % |
| 127493 | byte 0 = instance, byte 1 bits 0-1 = gear |

Three traps, all encoded in `config.ini` rather than in the code:

* **The side mapping is inverted between frame families.** CANopen node `0x0A`
  is port, but J1939 source `0x64` is **starboard** and `0x65` is port. Proven
  on single-motor runs. Do not "fix" it.
* **`0x48x` float 1 is torque, not power × 100 W.** The drives do not transmit
  motor power at all — the MFD computes it — so `/Dc/0/Power` is derived as
  voltage × current.
* **127493 and 65363 are matched by PGN and instance byte, never by source
  address**, because N2K addresses renumber on address claim.

### Temperatures

All three the MFD shows, per drive. The nine-path budget forces the mapping:

| Sensor | Path | Source |
|---|---|---|
| drive / motor winding | `/Motor/Temperature` | `0x28x` f2, else 61453 w1 |
| MCU / HCU controller board | `/Controller/Temperature` | 61453 w2 **only** |
| MOSFET | `/Coolant/Temperature` | `0x28x` f1, else 61453 w0 |

The MOSFET is the coolant-cooled element, so it is the closest thing the drives
report to a coolant-loop reading. 61453 is quantized to 1 °C, so a sensor
sitting on an integer boundary dithers ±1 °C frame to frame — exactly what the
display does. The `0x28x` floats are preferred wherever both carry the same
sensor.

### DC current

`/Dc/0/Current` prefers the measured value from 61452 w2:

```
amps = dc_current_offset + dc_current_slope * w2      (fit r = 0.9994 vs the MFD)
```

Sign inverted on purpose so positive = motoring. If the HCU frame is missing
the driver falls back to mechanical power ÷ voltage, which ignores drive losses
and is a few percent optimistic.

Both coefficients live in `config.ini` — re-fit them there, never in the code.

**Unverified**: the throttle scale (÷ 9.280) is exact over the 0-8.1 % that was
exercised; full travel extrapolates to raw ≈ 928 and has never been tested.

## Differences from the flow

- No `candump` subprocess, no `pkill`, no watchdog, no 5 s restart loop, no
  chunk-safe line parsing. The kernel filters and delivers frames directly
  (8 CANopen id entries + 5 PGN entries).
- Instance pinning is built in: the driver seeds and reconverges
  `/Settings/Devices/edrive_port|edrive_stbd/ClassAndVrmInstance`. The settings
  ids deliberately have no `virtual_` prefix, so the Node-RED palette's
  auto-cleanup never touches them.
- `/CustomName` is writable and persisted in `/Settings/EDrive/<key>/CustomName`.
- The `/EDrive/` telemetry tree is new.
- Staleness is per drive, not for the pair: one drive powered down does not
  blank the other.
- **Only the drives' own frames count as liveness.** 127493 (gear) and 65363
  (throttle) come from the Yanmar gateway, which keeps broadcasting with the
  hybrid system powered down. The flow treated them as a heartbeat, so a drive
  that had been off for hours still read `/Connected = 1` with every real
  measurement blank; here they update their values without refreshing the
  staleness clock.

## Install

First install only:

```sh
scp -r dbus-edrive root@$CERBO_HOST:/data/
ssh root@$CERBO_HOST '/data/dbus-edrive/install.sh'
```

or `python deploy_cerbo.py edrive --install` from the repo, which encodes the
one-session / config-guard rules in `CLAUDE.md`.

Afterwards a plain `python deploy_cerbo.py edrive` uploads what changed and
restarts the service.

## Migration from the flow

1. Node-RED UI: disable the **Greenline E-Drive** tab, Deploy.
   (Flows are deployed by hand — see `CLAUDE.md`.)
2. `ssh root@$CERBO_HOST '/data/dbus-edrive/migrate.sh'` — refuses to run while
   a flow service is still on the bus, kills the flow's leftover `candump`, and
   removes `/Settings/Devices/virtual_gl6gk_port|stbd`.
3. `svc -t /service/dbus-edrive`.

Rollback: `uninstall.sh`, then re-import `archive/GreenlineEDriveFlow.json` and
`archive/InstanceRegistry.json`. The flow and this driver must **never** run
together — they would both claim 210/211.

## Verify

```sh
svstat /service/dbus-edrive
grep -m1 '^VERSION' /data/dbus-edrive/dbus_edrive.py
tail -f /var/log/dbus-edrive/current | tai64nlocal
dbus -y com.victronenergy.motordrive.edrive_port / GetValue
```

With the hybrid system powered down both services stay registered with
`/Connected = 0` and blank values — that is the expected dock state, not a
fault. `python edrive_temps.py` remains the live cross-check against the MFD.
