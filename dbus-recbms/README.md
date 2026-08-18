# dbus-recbms

Standalone Venus OS driver for the REC-BMS main bank. Replaces the Node-RED
**Virtual BMS** flow (`Virtual BMS.json`).

## Why

The Node-RED implementation had two structural problems:

- **Slow to appear at boot** — Node-RED is embedded in the Signal K server, so
  the virtual battery only registered after Signal K *and* Node-RED were up
  (minutes). This driver is a daemontools service that starts seconds after
  D-Bus, before either of them.
- **Redeploy glitch** — a Node-RED deploy tears down and re-registers the
  virtual devices, which could wedge the D-Bus connection and require a Cerbo
  reboot. This driver is completely independent of Node-RED; deploys can no
  longer touch it. Restarting it is `svc -t /service/dbus-recbms` (~2s outage,
  covered by the startup grace).

## What it does

Reads the YDNB-07-repackaged CAN-BMS frames (29-bit `0x18FF0NNN` on `can0`)
with a raw SocketCAN socket — kernel-level filter, no candump subprocess, no
line parsing, no watchdog needed — and publishes:

| Service | Instance | Content |
|---|---|---|
| `com.victronenergy.battery.recbms` | 200 | the BMS: V/I/P/T, SOC, limits (CVL/CCL/DCL/DVL), synthetic alarms |
| `com.victronenergy.switch.recbms_maxcharge` | 220 | the VRM "Max Charge" slider (40–100%, BMS group) |

Behavior is a 1:1 port of the flow: staged fallback
(LIVE → ALERT → RESTRICT → SURVIVAL), cold-boot startup grace, Quattro
`/Dc/0/Voltage` as independent voltage source when CAN is stale, slider → CVL
mapping (40% → 54.00V, 100% → 61.96V), weekly 1-hour +0.44V equalization
(live-only, aborts on CAN loss, first run baselined to install + 7 days), and
the same alarm thresholds.

Improvements over the flow (the palette's virtual battery rejected these
paths): hi-res `/Soc` (0.01%), `/Soh`, `/Capacity` (remaining = SOC ×
installed), `/ConsumedAmphours`, `/InstalledCapacity` (0x379 — rated, not
remaining), `/TimeToGo`,
`/System/MinCellVoltage`–`MaxCellTemperature`, extreme-cell identity
(`/System/Min|MaxVoltageCellId`, `/System/Min|MaxTemperatureCellId` from
0x374-0x377), module counts from 0x372, `/History/ChargeCycles`, live
`/Serial` + firmware/hardware version, and diagnostics `/RecBms/Phase`,
`/RecBms/EqStatus`, `/RecBms/TimeToFull` (60s-smoothed charge current) and
`/RecBms/ForceChargeRequest` (forwarded to `/Info/ChargeRequest` only when
`forward_charge_request = true`).

Persistence moved from the `virtual-bms-state.json` file hack to
**localsettings**: `/Settings/RecBms/ChargeSlider`, `/Settings/RecBms/EqLastCompleted`,
`/Settings/RecBms/CustomName`. No writable-dir discovery, no restore/retry
dance, survives reboots and firmware updates natively.

All tunables live in [config.ini](config.ini).

## Install / migration

Order matters — free instances 200/220 before the first start so localsettings
grants them cleanly (the driver can self-heal a wrong grant, but this is
cleaner):

1. **Copy to the Cerbo**
   `scp -r dbus-recbms root@<cerbo>:/data/`
2. **Disable the old flow** — in Node-RED, delete (or disable) the *Virtual
   BMS* flow tab and Deploy. This stops candump and frees the two service
   names and instances 200/220.
3. **Clean the old settings entries** — deploy the updated Instance Registry
   (its Virtual BMS rows are removed), run *Enforce + audit now*; the audit
   flags `virtual_bms03a00000000050` / `virtual_bms03a00000000060` as
   orphans, then run the *Remove orphans* inject.
4. **Install**
   `sh /data/dbus-recbms/install.sh`
   (creates `/service/dbus-recbms` and the `/data/rc.local` hook so it
   survives firmware updates)
5. **Verify**
   - `svstat /service/dbus-recbms`
   - `tail -f /var/log/dbus-recbms/current | tai64nlocal` — expect
     "first BMS frame decoded" and "phase None -> LIVE" within seconds
   - `dbus-spy` → both services present, instances 200/220
   - Settings → System setup → **Battery monitor** should still be the
     REC-BMS (selection is stored as class:instance, which is unchanged);
     re-select if needed
   - VRM dashboard: "Max Charge" slider in the BMS group works
6. **Restart Signal K** (`svc -t /service/signalk-server`) so any cached
   paths from the old `virtual_*` service names disappear.

Note: the slider resets to 100% and the equalization schedule re-baselines on
first install (first EQ = install + 7 days). To carry over the old EQ time:
`dbus -y com.victronenergy.settings /Settings/RecBms/EqLastCompleted SetValue <epoch-seconds>`.

## Rollback

`sh /data/dbus-recbms/uninstall.sh`, then re-import the old `Virtual BMS.json`
flow in Node-RED and restore the two rows in the Instance Registry.

## Baselines

- [victronenergy/velib_python](https://github.com/victronenergy/velib_python) —
  official driver template (`dbusdummyservice.py`); the driver imports the
  copy shipped with Venus OS so the API always matches the running firmware
- [mr-manuel/venus-os_dbus-mqtt-battery](https://github.com/mr-manuel/venus-os_dbus-mqtt-battery) —
  service structure and install pattern
- [Venus wiki: howto add a driver](https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus),
  [dbus API](https://github.com/victronenergy/venus/wiki/dbus) — switch
  `/SwitchableOutput` API (slider = Type 7)
