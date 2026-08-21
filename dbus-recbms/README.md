# dbus-recbms

Standalone Venus OS driver for the REC-BMS main bank. Replaces the Node-RED
**Virtual BMS** flow (`archive/Virtual BMS.json`).

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
mapping piecewise-calibrated to the REC's own SOC scale from measured hold
equilibria (40% → 54.42V, 60% → 56.42V, knee at 62.3% → 56.65V,
100% → 62.70V, clipped to 61.96V), weekly 1-hour +0.44V equalization
(live-only, aborts on CAN loss, first run baselined to install + 7 days), and
the same alarm thresholds.

Improvements over the flow (the palette's virtual battery rejected these
paths): hi-res `/Soc` (0.01%), `/Soh`, `/Capacity` (remaining = SOC ×
installed), `/ConsumedAmphours`, `/InstalledCapacity` (0x379 `BatterySize` —
rated, not remaining), `/TimeToGo`,
`/System/MinCellVoltage`–`MaxCellTemperature`, extreme-cell identity
(`/System/Min|MaxVoltageCellId`, `/System/Min|MaxTemperatureCellId` from
0x374-0x377), module counts from 0x372, `/History/ChargeCycles`, live
`/Serial` + `/HardwareVersion`, and diagnostics `/RecBms/Phase`,
`/RecBms/ConfiguredCapacity`,
`/RecBms/EqStatus`, `/RecBms/TimeToFull` (60s-smoothed charge current) and
`/RecBms/ForceChargeRequest` (forwarded to `/Info/ChargeRequest` only when
`forward_charge_request = true` — keep this off: this REC holds 0x360 byte 0
at a constant `0xFF`, i.e. the flag carries no state. Consumers on current
Venus firmware: `systemstate.py` (GUI/VRM "Recharge" state), the gui-v2
battery-parameters page (display), and `hub4control` — the ESS controller,
which forces grid recharging on a charge request. Without an ESS assistant
in the Quattro (this boat: none, no `/Hub4/*` paths) hub4control idles, so
forwarding would only freeze the displayed state today — but it would mean
permanent forced grid charging if ESS were ever configured. `dvcc.py` and
`mk2-dbus` never read the path).

Persistence moved from the `virtual-bms-state.json` file hack to
**localsettings**: `/Settings/RecBms/ChargeSlider`, `/Settings/RecBms/EqLastCompleted`,
`/Settings/RecBms/CustomName`. No writable-dir discovery, no restore/retry
dance, survives reboots and firmware updates natively.

All tunables live in [config.ini](config.ini).

## The two capacity figures (v1.3.1)

The BMS broadcasts capacity twice, and they do not agree:

| Frame | Victron name | This bank | Published as |
|---|---|---|---|
| `0x379` bytes 0-1 | `BatterySize` — rated / installed | **1440 Ah** | `/InstalledCapacity` |
| `0x35F` bytes 4-5 | `BatteryInfo` — capacity configured in the BMS | **1400 Ah** | `/RecBms/ConfiguredCapacity` |

`/InstalledCapacity` stays on `0x379` because that is the frame the Victron
CAN-bus BMS protocol designates for it, and it is what `/Capacity`
(SOC × installed), `/ConsumedAmphours` and `/TimeToGo` are derived from.

**Fixed in v1.3.1**: `0x35F` bytes 4-5 were being read as a firmware version,
so `/FirmwareVersion` published a meaningless **`1400`**. That path now has no
source and stays empty — the only version the BMS sends is the bytes 2-3 pair,
published as `/HardwareVersion` (`2.9` here). REC's own documentation labels
those two bytes the hardware version; independent work on the REC-Q reads the
identical encoding as the *software* version, so the label is unsettled while
the encoding (two plain bytes, `major.minor`, **not** little-endian) is not.

If `/TimeToGo` ever needs to match the BMS's own arithmetic rather than the
rated figure, the number to switch `/InstalledCapacity` to is the 1400 in
`/RecBms/ConfiguredCapacity` — the REC computes its SOC against that.

## Solar boost (v1.2.0)

A request-and-forget control that lets a caller measure what the panels can
actually produce **without dropping shore power**.

The problem it solves: DVCC hands the same charge voltage to the Quattro and
to both MPPTs. At an identical setpoint the stiffer source wins, and a
shore-fed Quattro is far stiffer than an MPPT — so the solar chargers get
squeezed to zero. Measured 2026-08-19: both chargers sat at 0 W for 17
minutes while the Quattro pushed up to 1.3 kW from shore, on a sunny morning.
Lowering the Quattro does not help — it ignores
`/BatteryOperationalLimits/MaxChargeVoltage` in that range. Raising *only* the
solar chargers does, via the offset `dbus-systemcalc-py` applies to them alone
(`delegates/dvcc.py`).

While boosted the MPPTs run unthrottled (`MppOperationMode == 2`), which is
exactly the condition where **their output IS their capacity** — a direct
measurement, no probing, no AC transfer, no battery cycling.

| path | |
|---|---|
| `/RecBms/SolarBoost/Request` | **writeable** — volts to request, 0 to release |
| `/RecBms/SolarBoost/Applied` | volts actually in force |
| `/RecBms/SolarBoost/Active` | 0/1 |
| `/RecBms/SolarBoost/SecondsLeft` | countdown to automatic expiry |
| `/RecBms/SolarBoost/WindowOpen` | 1 while the caller should be sampling |
| `/RecBms/SolarBoost/EffectiveChargeVoltage` | CVL + boost — compare against this, not the CVL |
| `/RecBms/SolarBoost/Status` | `idle` / `ramp` / `measure` / `settling` / refusal reason |

**It is never sticky.** The boost expires by itself after `hold_s` (120 s), and
the driver additionally clears the offset at startup, on `SIGTERM`/`SIGINT`,
and via `atexit` — so a requester that dies, or a driver that is killed and
restarted, cannot leave the bank charging high. It is also re-asserted every
tick, because the systemcalc path is volatile and would otherwise be lost to a
systemcalc restart.

Timing (`measure_start_s = 75`, `measure_len_s = 30`) comes from a measured
step response: with ~0.15 V of margin both chargers reached `MppOperationMode
2` in **43–45 s** and 90 % of the step in **53 s**. 75 s leaves ~20 s of slack.

Every request is gated on live cell data, re-checked on every tick while
active — max cell voltage, cell temperature band, `CVL + boost` against a hard
ceiling, and a **minimum margin over the present pack voltage**. That last one
matters: the MPPTs ramp at a rate set by how far the bus sits below their
target, and at only ~0.05 V of margin they took **126 s to reach 5 %** of the
step. Boosting with too little margin would open the measurement window on an
array that had barely started and record the result as its capacity, so the
driver refuses instead of returning a number that is wrong but looks real.

> `SolarVoltageOffset` is a `Debug` path — unsupported, and it may move or
> vanish in a Venus update. The driver logs a warning and refuses the request
> if the write fails, rather than reporting a boost it did not apply.

Since v1.3.0 the boost's ceiling and margin gates run against the **true
target** (not the lead-lowered Quattro command), and the offset actually
written is `solar_lead_v + boost` while a boost is active.

## Solar lead (v1.3.0)

The Quattro does not hold the CVL it is given: in absorption it regulates
**+0.05–0.15V above** the commanded voltage. Measured 2026-08-19 with SVS on
and the BMS, the Quattro's own meter and `/BatterySense/Voltage` all agreeing
within 10mV — `VebusChargeState` said *absorption* while the pack was held
rock-steady at CVL+0.07V with the tail current decaying, so the bias is in
its regulation target, not in any measurement. Every calibrated slider
equilibrium was therefore overshot, which re-armed the flow's surplus logic
and ate the solar-boost margin. The MPPTs, by contrast, regulate accurately.

`[cvl] solar_lead_v` (0.15V here; built-in default 0 = off) splits the two:
the published `/Info/MaxChargeVoltage` — what DVCC hands the Quattro — is
**target − lead**, and the driver holds the systemcalc `SolarVoltageOffset`
at the lead so the solar chargers still see the **true target**.
Consequences:

- The Quattro lands at or just below the calibrated equilibrium (its bias is
  smaller than the lead), so it can no longer overcharge past the target.
- The last stretch of any charge is finished by the MPPTs at the true
  target, sun permitting; with no sun the pack settles up to `lead − bias`
  below target, which storage mode does not care about.
- Below the target only solar has charging headroom, so on shore the MPPTs
  run unthrottled (`MppOperationMode 2`) whenever the pack sits under
  target — passive solar priority plus continuous free capacity readings.
- `/RecBms/TargetChargeVoltage` publishes the true target (the Solar
  Priority flow keys its surplus/burn-down logic on it);
  `/RecBms/SolarLead` publishes the lead in force;
  `EffectiveChargeVoltage` = target + boost, as before.
- Fail-safe: if the offset path is unwritable, the driver logs a warning
  (rate-limited) and publishes the **full target** as the CVL — a missing
  lead can only restore v1.2 behavior, never lower the solar ceiling.
- The GUI/VRM DVCC charge-voltage readout shows the lowered Quattro
  command, not the target. The weekly EQ boost rides on the target as
  before; with no sun during the EQ hour the pack tops out `lead` low.

Deploy order: install this driver version **before** deploying the flow
revision that reads `/RecBms/TargetChargeVoltage`.

## Lead verification (v1.4.0)

Reading Victron's `dbus-systemcalc-py` (`delegates/dvcc.py`) showed the one
real hole in v1.3: both `/Debug/BatteryOperationalLimits/*VoltageOffset`
paths are applied **only while `/Settings/System/AccessLevel > 2`
(Superuser)**, and systemcalc evaluates that once per process (`@reify`).
The D-Bus write succeeds at any access level, so a driver that only checks
the write could believe a lead was in force while the MPPTs were actually
held at `target − lead` — the whole bank would settle ~0.15 V low, boosts
would do nothing, and harvest would never arm. (ESS does not have this
problem because its own +0.1 V MPPT bias is hard-coded in dvcc.py, gated on
the ESS assistant rather than on access level; the Debug offsets are the
only lever outside ESS.)

v1.4.0 therefore checks ground truth every 3 s:
`com.victronenergy.system /Control/EffectiveChargeVoltage` is the voltage
DVCC actually sends the solar chargers. The driver expects it to equal the
`/Info/MaxChargeVoltage` it published plus the offset it wrote; if instead
it equals the published CVL alone (offset ignored), or is unavailable, for
`[cvl] lead_verify_s` (10 s) it declares a **lead fault**:

- `/Info/MaxChargeVoltage` = full target (lead 0) — same fail-safe as an
  unwritable offset: v1.2 behavior, never a lowered solar ceiling;
- `/RecBms/SolarLead` = 0, boosts refused/aborted
  (`/RecBms/SolarBoost/Status` says why);
- `/RecBms/LeadFault` carries a human-readable explanation including the
  current access level and the fix;
- `/Alarms/InternalFailure` is raised to **warning** (1) while faulted
  (`lead_fault_alarm = false` to disable) — a standard battery alarm, so the
  GUI notifies and a VRM alarm rule on it can send mail;
- `/RecBms/DvccEffectiveChargeVoltage` mirrors the systemcalc value for
  diagnosis.

The offset keeps being written while faulted, so the fault clears by itself
once it is seen applied (the MPPTs then see `target + lead` for one tick plus
one DVCC cycle, ≤ 4 s, before the lead is re-established — harmless, and
still under `ceiling_v` even during EQ). Fix: Settings → General → Access level =
**Superuser**, then `svc -t /service/dbus-systemcalc-py` (the reify). The
driver also logs the access level at startup. Note: the boat's current
firmware applied the offset at access level 2 (verified 2026-08-21 on
deploy), so the gate is version-dependent — which is exactly why the driver
checks the effective voltage instead of the setting.

Also new in v1.4.0: `[battery] pin_bms_instance` writes
`/Settings/SystemSetup/BmsInstance = 200` while that setting is still on
automatic (−1), so no later battery service can be auto-selected as the
DVCC BMS. An explicit choice is never overridden; −255 (BMS control
disabled) is logged as a warning.

Things dvcc.py does that are worth knowing:

- DVCC adjusts every **3 s**; a boost request is applied 0–3 s after the
  write (`measure_start_s = 75` already allows for it).
- The DVCC "limit managed battery charge voltage" setting is applied
  *before* the solar offset, so it does **not** cap a boost — this driver's
  `ceiling_v` is the only ceiling on the MPPTs.
- If this driver dies (not CAN loss — the service itself), systemcalc stops
  writing to the MPPTs and they raise error #67 after ~60 s and stop;
  daemontools restarts the driver in ~1 s, so this only matters in a crash
  loop.
- CCL 0 (the driver's fallback phases) drives the MPPT `/Link/ChargeCurrent`
  to 0 as well — solar is off during a CAN outage on shore; when inverting
  the MPPTs still get 0 + inverter draw, so they keep carrying loads.

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

`sh /data/dbus-recbms/uninstall.sh`, then re-import the old `archive/Virtual BMS.json`
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
