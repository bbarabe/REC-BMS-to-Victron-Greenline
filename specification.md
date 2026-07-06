# Boat NMEA System Specification

## Overview

This project contains Node-Red flows and device configurations for a marine vessel's electrical and battery monitoring system running on a **Victron Cerbo GX** (Venus OS). The system integrates multiple CAN bus networks, Signal-K, and Victron's D-Bus to provide unified monitoring and control via VRM (Victron Remote Management).

## Physical Architecture

### CAN Bus Networks

There are two physically separate CAN bus networks, bridged by a Yacht Devices YDNB-07:

- **CAN1 (drive/BMS network)** — 250 kbps, limited devices:
  - REC-BMS (11-bit standard CAN frames): Victron/SMA CAN-BMS protocol at 0x351-0x404
  - Boat electric drive system
  - Yanmar engines
  - No chartplotters, switching, or general NMEA 2000 devices on this bus

- **CAN2 (main NMEA 2000 network / Cerbo GX VE.Can port = `can0`)** — 250 kbps:
  - Cerbo GX (VE.Can port)
  - CZone digital switching
  - Simrad chartplotters/MFDs
  - Battery monitors: device address 16 (`n2k-on-ve.can-socket.16`) provides PGN 127506/127508 battery data
  - All other NMEA 2000 devices
  - Receives forwarded traffic from CAN1 via the YDNB-07 bridge
  - **Important**: The VE.Can port filters out 11-bit CAN frames at the hardware/driver level (sun4i_can). Only 29-bit extended frames reach userspace (`candump`). This is why BMS frames must be repackaged.

### YDNB-07 Bridge (Serial: 00070836, Firmware 1.42)

Configuration file: `YDNB.CFG`

The bridge performs two functions:
1. **NMEA 2000 forwarding**: `FW_CAN1_TO_CAN2=ON` passes all 29-bit traffic transparently
2. **BMS frame repackaging**: 16 explicit `match()` filters catch specific 11-bit BMS CAN IDs, rewrite them as 29-bit extended frames (`0x18FF0NNN`), and send to CAN2. This is necessary because the Cerbo's VE.Can port drops 11-bit frames.

The repackaging scheme: original 11-bit CAN ID `0xNNN` becomes 29-bit CAN ID `0x18FF0NNN` (proprietary broadcast PGN 0xFF00 range, priority 6). The frame type byte at buffer offset 4 is changed from `0xFE` (11-bit) to `0xFF` (29-bit).

Manual: `ydnb07.md` / `ydnb07.pdf`

### Cerbo GX (Venus OS)

- **can0**: VE.Can port, 250 kbps, `sun4i_can` driver
- Runs Node-Red with `@victronenergy/node-red-contrib-victron` v1.7.7 and `@signalk/node-red-embedded` v2.18.1
- Signal-K server decodes NMEA 2000 PGNs into Signal-K paths

## Battery Systems

### Main Battery Bank (REC-BMS via CAN-BMS protocol)

- **Chemistry**: 15S NMC
- **Nominal capacity**: 1440 Ah (~83 kWh at 58V)
- **BMS**: REC-BMS, serial "9M-0485"
- **6 modules**: C8SU1/C7SU1, C14SU2, C13SU4, T1SU3, T1SU4
- **CAN protocol**: Victron/SMA CAN-BMS, 11-bit standard CAN IDs

CAN frame map (all 8-byte DLC unless noted):

| CAN ID | Name | Key fields |
|--------|------|------------|
| 0x351 | Charge/Discharge Limits | CVL, CCL, DCL, DVL (16-bit LE, 0.1V/A scale) |
| 0x355 | SOC/SOH | SOC%, SOH%, hi-res SOC (0.01%) |
| 0x356 | Voltage/Current/Temp | Pack V (0.01V), current (0.1A signed), temp (0.1C), charge cycles (bytes 6-7) |
| 0x35E | Manufacturer | ASCII "REC-BMS" |
| 0x35F | Firmware/Capacity | Chemistry, HW ver, FW ver, capacity |
| 0x360 | Force Charge | Byte 0: 0xFF = force charge requested, 0x00 = normal (not acted upon) |
| 0x372 | Module Status | Byte 0: modules online, Byte 2: blocking charge, Byte 4: blocking discharge, Byte 6: offline |
| 0x373 | Cell Min/Max | Min/max cell voltage (mV), min/max cell temp (K) |
| 0x374-0x377 | Module IDs | ASCII module/sensor labels |
| 0x379 | Capacity | Remaining capacity |
| 0x380 | Serial Number | ASCII serial |
| 0x381 | Unknown | Additional data (not yet decoded) |
| 0x404 | Heartbeat | Keep-alive (DLC=3) |

**Note**: This REC-BMS does **not** send the standard CAN-BMS alarm frame (0x35A). The Virtual BMS flow generates synthetic alarms based on available data (SOC thresholds, cell imbalance from 0x373, module status from 0x372, CAN staleness).

Full decode: `Decoded.md`, raw capture: `BatteryCAN.md`

### 12V Batteries (via NMEA 2000 PGN 127506/127508)

Five 12V batteries monitored by an NMEA 2000 device at address 16. Three are currently integrated; two (port/starboard engine) are not yet connected.

| Signal-K Instance | Name | Available Data | Status |
|-------------------|------|---------------|--------|
| electrical.batteries.1 | House | Voltage, current, SOC, SOH, time remaining | Active |
| electrical.batteries.2 | Bow | Voltage, SOC, SOH, time remaining | Active |
| electrical.batteries.3 | Stern | Voltage, SOC, SOH, time remaining | Active |
| electrical.batteries.4? | Port Engine | TBD | Not yet connected |
| electrical.batteries.5? | Starboard Engine | TBD | Not yet connected |

## Node-Red Flows

### Virtual BMS (`Virtual BMS.json`)

Reads repackaged BMS CAN frames from `can0` via `candump`, decodes them, and publishes to a Victron virtual battery device on D-Bus.

**Data path**: candump can0 (filter `18FF0000:1FFFF800`) -> Line Parser (strips 29-bit wrapper: `rawid & 0x7FF`) -> CAN Frame Decoder -> State Assembler -> Path Filter -> victron-virtual battery

Key features:
- **candump watchdog**: Before every start, `pkill -f '[c]andump can0,18FF0000:1FFFF800'` clears any stale capture process (prevents duplicates after a hard Node-Red restart or manual re-trigger). The pattern matches this flow's exact command line — filter argument included — so manually-run or other-purpose candump processes are never touched. If candump exits for any reason, a watchdog restarts it after 5 seconds (restart count kept in flow context).
- **Chunk-safe line parsing**: exec stdout arrives in chunks, not lines. The parser buffers partial lines across chunks, splits on newlines, and emits one message per CAN frame — no frames are lost when the pipe coalesces multiple lines.
- **Frame length guard**: Each CAN ID has a minimum-length requirement; short/corrupt frames are dropped (with a rate-limited warning, max 1/min) instead of throwing mid-decode. Only successfully decoded frames refresh the staleness timestamp.
- **Staged fallback**: If CAN data goes stale, progressively restricts charge/discharge limits (LIVE ≤60s → ALERT ≤2min → RESTRICT ≤5min → SURVIVAL). When BMS is stale, pack voltage is sourced from the Quattro's `/Dc/0/Voltage` (independent measurement), with fallback to last-known BMS voltage if the Quattro reading is also stale (>30s).
- **CVL control**: "Max Charge" slider (40-100%) exposed on VRM dashboard (BMS group), maps linearly to CVL range (61.96-62.4V at 100%)
- **Weekly equalization**: Every 7 days, adds +0.44V boost to the current slider CVL for 1 hour. Works at any slider position, not just 100%. EQ start is gated on persisted state having been restored; on first install the schedule is baselined so the first EQ runs 7 days later, not immediately.
- **Persistent state** (`virtual-bms-state.json`): The slider position and last-equalization timestamp survive Node-Red restarts, redeploys, and reboots. Node-Red on Venus OS runs as a restricted user that cannot write to `/data` directly, so the startup exec discovers a writable directory (first writable of `$HOME/.node-red`, `$HOME`, `/data` — the Node-Red user dir is always writable since flows are saved there), reports it on the first line of its output, and the flow stores it in flow context (`state_dir`) for all subsequent writes. The "Restore Persisted State" node status shows the directory in use. State is restored 6s after startup (giving the virtual switch time to register on D-Bus, then the slider is pushed back to the VRM switch) and saved on every slider change and EQ completion. If pushing the slider value fails (switch not yet registered), a catch node re-runs the restore up to 5 times at 3s intervals. Saves are blocked until restore has run, so a startup echo can't clobber the file. If the file is missing, defaults are slider 100% and EQ baselined to now.
- **Synthetic alarms** (since BMS does not send 0x35A; all thresholds from 0x373 cell extremes unless noted, and all report 0 when CAN is stale):
  - `/Alarms/LowSoc`: warning at SOC < 20%, alarm at SOC < 10%
  - `/Alarms/CellImbalance`: warning at cell delta > 50mV, alarm at > 100mV
  - `/Alarms/LowVoltage`: warning at min cell < 3.30V, alarm at < 3.00V
  - `/Alarms/HighVoltage`: warning at max cell > 4.20V, alarm at > 4.25V
  - `/Alarms/LowTemperature`: warning at min cell < 5°C, alarm at < 0°C (NMC charging below freezing causes lithium plating)
  - `/Alarms/HighTemperature`: warning at max cell > 45°C, alarm at > 50°C
  - `/Alarms/HighChargeCurrent`: warning when modules are blocking charge (from 0x372)
  - `/Alarms/HighDischargeCurrent`: warning when modules are blocking discharge (from 0x372)
  - `/Alarms/InternalFailure`: alarm on CAN staleness (ALERT phase+) or modules offline (from 0x372)

### CZone Control (`CZoneProxy.json`)

Bidirectional control of CZone digital switching outputs via Victron Virtual Switches. v6 removed Signal-K from the loop entirely — state is read directly from `can0`. (The Signal-K path added 1-2s of latency and, worse, when the Signal-K server wedged the frozen state cache silently blocked all commands until reboot.) v6.0 was rolled back in production because it strobed the lights; v6.1 is the rebuild with the root cause fixed (see instance filter below).

**Bus behavior (measured from candump captures, 2026-07-06):**
- The CZone output interface (src `0x02`) broadcasts PGN 127501 every 2s and within ~10ms of any output change, twice per cycle: once as bank instance 1 and once as instance 10 (identical payloads).
- MFD/keypad commands (src `0x20`) use PGN 127502 **instance 1** with *momentary* semantics: `ON` frames stream at ~250ms while the button is held, one `OFF` on release, and the CZone circuit logic toggles its latched output on the rising edge. Instance-1 127502 is therefore *not* set-state and must not be imitated.
- Bank-10 127502 commands *are* direct set-state: applied in <10ms, confirmed via 127501, and accepted from the unclaimed source address `0xFE`.
- An unidentified device at src `0x0C` broadcasts 127501 **instance 0** ("all OFF") at 5 Hz, with occasional single-frame blips of switch 1 ON. To identify it: `cansend can0 18EA0CFE#00EE00 && candump -td can0,18EEFF0C:1FFFFFFF -n 1`.

Flow features:
- **State monitoring**: `candump can0,01F20D00:03FFFF00` captures PGN 127501 directly — the filter masks the PGN field (CAN ID bits 8-25) so any priority/source matches. Same watchdog pattern as Virtual BMS: pkill stale capture before start, chunk-safe line parser, auto-restart 5s after candump exits. The decoder stores per-switch `{state, ts}` in flow context and pushes to the virtual switches only on change.
- **Instance filter (the v6.0 strobing bug)**: the decoder ignores any 127501 frame whose bank instance byte (data byte 0) is not `0x01`. Without this, the `0x0C` device's 5 Hz instance-0 all-OFF spam flips the tracked state several times a second; the resulting virtual-switch syncs echo out the command side and strobe the physical lights.
- **State request**: ISO Request (PGN 59904) for 127501 is broadcast at startup and every 60s (`cansend can0 18EAFFFE#0DF201`) so state is known right after a deploy even if CZone only transmits on change.
- **Command sending**: `cansend can0` with PGN 127502 instance 10 to set CZone outputs
- **Confirm-and-retry**: every command is registered as pending; a 250ms tick checks whether a 127501 reading *newer than the command* reports the target state. Unconfirmed commands are resent after 500ms (max 3 transmissions); if still unconfirmed after 5s the virtual switch is reverted to the last real CZone state and a warning is logged.
- **Keep-alive** (measured empirically 2026-07-02): CZone reverts a 127502-commanded bank-10 state ~10s after the last command frame — a single fire-and-forget command turns the output on for only a few seconds. Confirmed ONs commanded from VRM are therefore re-asserted in a single 127502 frame every ~1s. A hold is released when the output is observed OFF with no command in flight (keypad or CZone turned it off) or when an OFF is commanded; keypad-lit circuits are never held since CZone latches those natively. Note: holds live in flow context, so a Node-RED redeploy drops them and any VRM-lit circuits turn off ~10s later.
- **Virtual switch caveat**: the `victron-virtual-switch` node does *not* preserve message properties from input to output — a state write echoes out the command side without the `_czoneSync` flag. The engine's fresh-state dedupe is what actually stops these echoes from becoming commands.
- **Revert-echo guard**: a revert write (after a 5s unconfirmed command) also echoes out the command side, and if CZone is unresponsive the fresh-state dedupe can't stop it — state is stale — so it would re-command and re-revert in a ~5s loop. The engine records each revert (`czRecentRevert`) and drops any command that mirrors a revert made in the last 3s.
- **Deploy-echo guard**: on (re)deploy every virtual switch emits its initial OFF out the command side before any 127501 has been decoded, which would otherwise blast OFF commands to all 11 circuits (and kill keypad-lit lights). The engine drops all commands for the first 12s after deploy — by then candump is running and real state has been synced into the switches.
- **Stale-state safety**: the "already ON/OFF" dedupe only applies when the 127501 reading is <10s old — stale state never blocks a command.
- **11 switches**: Bilge1, Bilge2, Horn, Helm, Nav, Anchor, Red, Fly, Underwater, Bow1, Bow2
- **Latching logic**: Lights use on/off toggle; horn and bilge pumps are momentary
- **Feedback-loop protection**: `_czoneSync` flag on virtual-switch writes, plus the fresh-state dedupe in the command engine as second line of defense

### Batteries Forward (`BatteriesForward.json`)

Reads 12V battery data from Signal-K and publishes to 3 Victron virtual battery devices.

- **Signal-K subscribe** nodes for voltage, current (House only), and SOC
- **SOC conversion**: Signal-K ratio (0.0-1.0) multiplied by 100 for Venus OS percentage
- **1s publish tick** with 10-second staleness check
- **3 virtual battery outputs**: House, Bow, Stern (12V preset)

### Solar Priority (`SolarPriority.json`)

Automatically uses solar power instead of shore power when solar production can cover AC loads. Prevents energy waste when the boat is idle with the charge slider set below 100% (which otherwise stops solar production while shore powers all loads).

**Problem**: When the battery sits at the CVL ceiling (e.g., 60% slider), MPPTs produce nothing — so there is no PV power reading to compare against load. The flow solves this by estimating available solar capacity from the panel open-circuit voltage (Voc), which is always readable even when MPPTs are idle.

**Mechanism**: Controls the Quattro's `IgnoreAcIn1` D-Bus path. When solar is estimated to be sufficient, AC input is ignored → Quattro inverts from battery → battery SOC dips below CVL → MPPTs ramp up → solar effectively powers loads. No backfeed risk (AC input is simply disconnected, not reversed).

**Power estimation from Voc**:
- Linear model: `P_est = P_rated × max(0, (Voc - VOC_ZERO) / (VOC_FULL - VOC_ZERO)) × gain`
- `VOC_ZERO` (default 30V): panel voltage below which power is negligible
- `VOC_FULL` (default 80V): panel voltage at full sun
- `P_rated`: total rated panel wattage (configurable via VRM slider, default 500W)
- `gain`: self-tuning correction factor (starts at 1.0, adjusts after each probe)

**Self-tuning**: After each probe, the flow compares the Voc-based estimate against measured PV power and adjusts the gain: `gain = 0.85 × gain + 0.15 × (actual / estimated)`, clamped to [0.3, 3.0]. Over multiple cycles this converges to an accurate mapping for the specific panel installation, accounting for orientation, shading, temperature, and aging.

**Load averaging**: AC consumption is smoothed with a 60-second rolling average to avoid reacting to transient spikes.

**Data inputs**:
- `com.victronenergy.solarcharger` `/Pv/V` — panel open-circuit voltage (V), from one MPPT
- `com.victronenergy.system` `/Dc/Pv/Power` — total DC-coupled PV power (W), sum of all MPPTs
- `com.victronenergy.system` `/Ac/Consumption/L1/Power` — AC consumption phase 1 (W)
- `com.victronenergy.system` `/Dc/Battery/Soc` — battery state of charge (%)

**Control output** (`com.victronenergy.vebus`):
- `/Ac/Control/IgnoreAcIn1` — 0 = accept shore, 1 = ignore shore

**State machine** (three states: `shore`, `probe`, `solar`):
- **Shore → Probe**: estimated PV ≥ avg load × 1.2 AND est ≥ 100W AND SOC ≥ 40% AND cooldown elapsed → disconnect shore
- **Probe (60s)**: MPPTs ramp up; after 60s, evaluate actual PV power
  - **Probe → Solar**: actual PV ≥ avg load × 1.2 → stay on solar (self-tune gain)
  - **Probe → Shore**: actual PV < required → reconnect shore (self-tune gain)
- **Solar → Shore**: avg load > actual PV for 15 continuous seconds, OR SOC < 40%
- **Emergency reconnect**: SOC < 30% → immediate shore from any state, no delay
- **Cooldown**: 5 minutes minimum between any transitions (anti-cycling)
- **Disabled/missing data**: always defaults to shore (safe state)

**VRM dashboard** ("Solar" group):
- "Solar Priority" on/off toggle (disabled by default)
- "PV Capacity" slider (100–2000W, step 50W) — set to total rated panel wattage

**Setup required**: After import, (1) open "PV Voltage" node and select an MPPT solar charger, (2) open "AC Input Control" node and select the Quattro.

## Key Technical Notes

- The Cerbo's VE.Can `can0` port (sun4i_can driver) only passes 29-bit extended CAN frames to userspace. 11-bit standard frames are filtered at the hardware/kernel level. This is why the YDNB-07 must repackage BMS frames.
- Signal-K SOC values are ratios (0.0-1.0), not percentages. Multiply by 100 for Venus OS.
- The YDNB-07 programming language requires `match()` filter bodies on separate lines (no single-line `{ }` blocks).
- The YDNB-07 supports up to 20 `match()` filters. Currently 16 are used for BMS CAN IDs.
- `FW_CAN1_TO_CAN2=ON` only forwards frames that don't match any `match()` filter. Matched frames are handled exclusively by their filter subprogram.
