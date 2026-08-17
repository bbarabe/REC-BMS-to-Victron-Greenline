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

### 12V Batteries

Three 12V batteries are monitored by an NMEA 2000 device at address 16 (PGN 127506/127508). The two engine batteries are not on that monitor — their voltage comes from the Yanmar gateways' PGN 127489 (Engine Parameters, Dynamic) *Alternator Potential* field (0.01V resolution, ~2 Hz, transmitted even with engines off; confirmed against known battery voltages 2026-07-07). 127489 carries no SOC or current.

| Signal-K Path | Name | Source | Available Data |
|---------------|------|--------|---------------|
| electrical.batteries.1 | House | Addr 16, PGN 127506/127508 | Voltage, current, SOC, SOH, time remaining |
| electrical.batteries.2 | Bow | Addr 16, PGN 127506/127508 | Voltage, SOC, SOH, time remaining |
| electrical.batteries.3 | Stern | Addr 16, PGN 127506/127508 | Voltage, SOC, SOH, time remaining |
| propulsion.port.alternatorVoltage | Port Engine | Src 141 (0x8D), PGN 127489, engine instance 0 | Voltage only |
| propulsion.starboard.alternatorVoltage | Starboard Engine | Src 142 (0x8E), PGN 127489, engine instance 1 | Voltage only |

The 127489 messages also carry engine hours (~208h port / ~209h starboard as of 2026-07-07) and engine temperature; oil temp, coolant pressure, and fuel pressure are not populated (0xFFFF) by these gateways.

## Node-Red Flows

### Virtual BMS (`Virtual BMS.json`)

Reads repackaged BMS CAN frames from `can0` via `candump`, decodes them, and publishes to a Victron virtual battery device on D-Bus.

**Data path**: candump can0 (filter `18FF0000:1FFFF800`) -> Line Parser (strips 29-bit wrapper: `rawid & 0x7FF`) -> CAN Frame Decoder -> State Assembler -> Path Filter -> victron-virtual battery

Key features:
- **candump watchdog**: Before every start, `pkill -f '[c]andump can0,18FF0000:1FFFF800'` clears any stale capture process (prevents duplicates after a hard Node-Red restart or manual re-trigger). The pattern matches this flow's exact command line — filter argument included — so manually-run or other-purpose candump processes are never touched. If candump exits for any reason, a watchdog restarts it after 5 seconds (restart count kept in flow context).
- **Chunk-safe line parsing**: exec stdout arrives in chunks, not lines. The parser buffers partial lines across chunks, splits on newlines, and emits one message per CAN frame — no frames are lost when the pipe coalesces multiple lines.
- **Frame length guard**: Each CAN ID has a minimum-length requirement; short/corrupt frames are dropped (with a rate-limited warning, max 1/min) instead of throwing mid-decode. Only successfully decoded frames refresh the staleness timestamp.
- **Staged fallback**: If CAN data goes stale, progressively restricts charge/discharge limits (LIVE ≤60s → ALERT ≤2min → RESTRICT ≤5min → SURVIVAL). When BMS is stale, pack voltage is sourced from the Quattro's `/Dc/0/Voltage` (independent measurement), with fallback to last-known BMS voltage if the Quattro reading is also stale (>30s).
- **Startup grace (cold-boot fix)**: A cold boot is not treated as CAN loss. Until the first frame ever decodes, the staleness clock is anchored to flow start (not epoch 0) and the flow holds a benign STARTUP phase for up to 3 minutes: CCL 0 (no blind charging), DCL 100A (inverter keeps running), DVL 52.0V (below resting pack voltage, so no low-battery cutoff), all alarms 0. Previously the first publish tick computed `age = now − 0`, jumped straight to SURVIVAL, and pushed DVL 54.0V — above the pack's resting ~53V — which tripped the Quattro's low-battery shutdown and raised a low-voltage alarm on every boot off shore power. If the grace window expires with still no frame, the normal ALERT → RESTRICT → SURVIVAL escalation proceeds from flow start. The virtual battery node's `default_values` is also off, so the service no longer flashes 48V/50% in the seconds before the first publish tick.
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

Reads 12V battery data from Signal-K and publishes to 5 Victron virtual battery devices.

- **Signal-K subscribe** nodes for voltage, current (House only), and SOC
- **SOC conversion**: Signal-K ratio (0.0-1.0) multiplied by 100 for Venus OS percentage
- **1s publish tick** with 10-second staleness check
- **5 virtual battery outputs** (12V preset): House, Bow, Stern (voltage/SOC from `electrical.batteries.*`), Port Engine and Starboard Engine (voltage only, from `propulsion.*.alternatorVoltage` / PGN 127489)
- Engine batteries use `default_values: false` so the never-published SOC/current paths stay blank instead of showing a fake 50%; their assemblers also drop non-numeric payloads so a null Signal-K delta can't publish 0V

### Solar Priority (`SolarPriority.json`)

Automatically powers AC loads from solar instead of shore power when the panels can carry them. Designed for storage mode: the battery is held at ~60% SOC by the Max Charge slider and the goal is **zero energy cycled through the battery** — battery power is therefore the primary control signal, not PV-vs-load comparison.

**Problem**: When the battery sits at the CVL ceiling (e.g., 60% slider), MPPTs produce nothing — so there is no PV power reading to compare against load. The flow estimates available solar capacity from the panel open-circuit voltage (Voc), which is readable even when MPPTs are idle.

**Mechanism**: Controls the Quattro's `IgnoreAcIn1` D-Bus path. When solar is estimated sufficient, AC input is ignored → Quattro inverts from battery → battery dips below CVL → MPPTs ramp up → solar powers the loads. No backfeed risk (AC input is disconnected, not reversed). While enabled, the flow owns this path and re-asserts the desired value every 30s (survives vebus restarts and external writes); while disabled it never writes after the initial return to shore, so manual control stays possible.

**Power estimation**:
- Idle MPPT (PV < 30W): `P_est = P_rated × clamp((Voc - 30V) / (80V - 30V)) × gain` — `/Pv/V` reads true open-circuit voltage
- Producing MPPT: `/Pv/V` reads Vmp (lower than Voc), so the estimate becomes `max(actual PV power, Voc model)` — actual production is a live lower bound on capacity
- `P_rated`: total rated panel wattage (VRM slider, default 500W); `gain`: self-tuning correction factor

**Self-tuning (throttle-aware)**: A throttled MPPT (battery at CVL ceiling) produces exactly the load, not its capacity — naively comparing that to the estimate would spiral the gain down. The gain is therefore only tuned **down** after a *failed* probe (battery discharging at ramp end ⇒ MPPTs were provably unthrottled ⇒ measured PV *is* the real capacity), and only tuned **up** when measured PV exceeds the estimate (lower-bound evidence). Update: `gain ×= 0.85 + 0.15 × (actual / estimated)`, clamped to [0.3, 3.0]. Persisted across reboots.

**Data inputs** (each stored as `{value, timestamp}`; any critical input staler than 30s forces shore):
- `com.victronenergy.solarcharger` `/Pv/V` — panel voltage, from one MPPT
- `com.victronenergy.system` `/Dc/Pv/Power` — total DC-coupled PV power (W)
- `com.victronenergy.system` `/Ac/Consumption/L1/Power` — AC consumption (instant + 60s *time-based* rolling average)
- `com.victronenergy.system` `/Dc/Battery/Soc` — battery SOC (%)
- `com.victronenergy.system` `/Dc/Battery/Power` — battery power (W, + charging / − discharging) — the truth signal
- `com.victronenergy.vebus` `/Ac/ActiveIn/ActiveInput` — command feedback (0 = AC-in 1, 240 = inverting); optional but detects an ineffective `IgnoreAcIn1` write (wrong service selected — otherwise the flow would fake-enter solar mode while still on shore) and an absent shore connection (probing is blocked when there is no fallback)

**Control output** (`com.victronenergy.vebus`): `/Ac/Control/IgnoreAcIn1` — 0 = accept shore, 1 = ignore shore. Shore is assumed on AC input 1 (see the flow's SETUP comment for AC-in-2 systems).

**State machine** (`shore` → `probe` → `solar`):
- **Shore → Probe**: estimate ≥ avg load × 1.2 AND ≥ 100W AND SOC ≥ 40% AND shore present, all sustained for 30s (no probing on a sun glint), gated by cooldown + backoff → disconnect shore
- **Probe (60s ramp)**: PV and battery power are averaged over the final 15s (a passing cloud at the evaluation instant can't fail an otherwise good probe). Aborts early if a load bigger than the estimate appears (3s sustained). At ramp end:
  - **→ Solar**: battery not discharging (> −50W avg) — solar covers *everything* including DC loads and inverter losses
  - **→ Shore**: battery discharging → gain tuned down (reliable measurement), exponential backoff
  - **→ Shore (FAULT)**: feedback still shows AC-in 1 → `IgnoreAcIn1` ineffective → 1h lockout, no gain pollution
- **Solar → Shore** (all safety exits bypass cooldown):
  - Big load: battery discharge > 400W sustained 3s → immediate reconnect (uses instant readings, not the average)
  - Deficit: battery discharge > 50W sustained 15s (clouds, evening)
  - SOC drift: SOC falls 2% below its value at solar entry, or below 40%
  - Emergency: SOC < 30% from any state → immediate shore + 1h lockout
  - Fault: feedback shows AC re-accepted externally despite re-asserts → shore + 1h lockout
- **Anti-cycling**: 5 min cooldown between transitions; failed probes back off exponentially 5→10→20→40→60 min (capped). Backoff is released early when Voc rises ≥ 5V above its value at the last failure (sky materially brighter), after a ≥ 30 min stable solar stint, or on re-enable.
- **Disabled / missing / stale data**: always returns to shore (safe state). A `catch` node forces shore on any decision-engine exception; a safe-start inject writes 0 at startup.

**Persistence** (`solar-priority-state.json`, same writable-dir discovery as Virtual BMS): enable state, PV capacity, and learned gain survive Node-RED restarts, redeploys, and reboots — after a marina power cut the flow resumes on its own. Restore runs ~6s after startup and pushes values back to the VRM switches (catch + retry ×5); saves are blocked until restore has run so startup echoes can't clobber the file.

**VRM dashboard** ("Solar" group): "Solar Priority" on/off toggle, "PV Capacity" slider (100–2000W, step 50W).

**Setup required**: After import, (1) open "PV Voltage" and select an MPPT solar charger, (2) verify "AC Input Control" and "AC Input Feedback" point at the Quattro (pre-filled with `vebus/276`), (3) verify the four `victron-input-system` nodes.

### Instance Registry (`InstanceRegistry.json`)

Pins every virtual device the flows publish to a fixed dbus service name **and** a fixed VRM/device instance, so redeploys and restarts can never mint new instances. Previously instances were assigned dynamically (localsettings auto-allocation in registration order), which littered the Signal K data model with orphan paths (e.g. four generations of tank paths `tanks.freshWater.81/83/85/89` + matching `wasteWater.82/84/86/90` from one sender, and dead GPS services `vi1_uc1479909` / `vi2_uc1548484`).

**Why instances drifted**: the victron palette derives the dbus service name from the Node-RED node ID (`com.victronenergy.<class>.virtual_<nodeId>`) and proposes a default instance; localsettings then auto-assigns the next free number in registration order. Any regenerated node ID (copy/paste re-import, flow rebuild) or reset settings entry mints a brand-new service + instance — and Signal K keys its paths off both, so each generation leaves an orphan.

**Design**:
- The function node **"Instance Registry (single source of truth)"** holds the *only* table mapping each virtual device (node ID → service name, class, source identity) to a hand-assigned instance. Nothing anywhere may derive an instance from an array index, discovery order, a loop counter, a timestamp, or a hash.
- Instances come from the reserved block **200–255**, far above anything Venus auto-assigns (native devices allocate from 0 upward): 200–209 batteries, 210–219 motor drives, 220–239 switches, 240–255 spare. Never reuse a number, even after deleting a device.
- **Idempotent enforcement**: on startup (10s inject) and via the manual "Enforce + audit now" inject, the flow pre-seeds `/Settings/Devices/virtual_<id>/ClassAndVrmInstance` (AddSetting keeps an existing value, so re-runs never clobber), pins it with SetValue when it differs, then compares the live service's `/DeviceInstance`. Existing services are updated in place — the service name never changes, so a redeploy can never create a second copy. A `RECONVERGE` warning means the service registered before the pin landed (first-ever deploy only); one more deploy converges it.
- **Audit + cleanup**: the audit lists `virtual_*` settings entries that are neither in the registry nor live (orphans from dead services); a separate manual inject removes them via `RemoveSettings`. Live-but-unregistered services (e.g. an older flow version still deployed, like CZone v5) are only warned about and never removed. After removing orphans, restart Signal K (`svc -t /service/signalk-server`) so its cached dead paths disappear.

**Device instance allocation** (authoritative copy lives in the registry node — keep in sync):

| Instance | Class | Service `com.victronenergy.…` | Device | Source identity |
|----------|-------|-------------------------------|--------|-----------------|
| 200 | battery | `battery.virtual_bms03a00000000050` | REC-BMS Main Bank | REC-BMS 9M-0485, CAN-BMS 0x351–0x404 via YDNB-07 |
| 201 | battery | `battery.virtual_bat1_virtual` | House 12V | N2K addr 16, PGN 127506/127508 |
| 202 | battery | `battery.virtual_bat2_virtual` | Bow 12V | N2K addr 16, PGN 127506/127508 |
| 203 | battery | `battery.virtual_bat3_virtual` | Stern 12V | N2K addr 16, PGN 127506/127508 |
| 204 | battery | `battery.virtual_bat4_virtual` | Port Engine 12V | PGN 127489 src 141, engine instance 0 |
| 205 | battery | `battery.virtual_bat5_virtual` | Starboard Engine 12V | PGN 127489 src 142, engine instance 1 |
| 210 | motordrive | `motordrive.virtual_gl6gk_port` | Port E-Motor | CANopen node 0x0A, PGN 127493 instance 0 |
| 211 | motordrive | `motordrive.virtual_gl6gk_stbd` | Starboard E-Motor | CANopen node 0x0B, PGN 127493 instance 1 |
| 220 | switch | `switch.virtual_bms03a00000000060` | Max Charge Slider | VRM control only |
| 221 | switch | `switch.virtual_sp_switch_001` | Solar Priority Toggle | VRM control only |
| 222 | switch | `switch.virtual_sp_rated_switch` | PV Capacity Slider | VRM control only |
| 225–235 | switch | `switch.virtual_cz_vs_sw1`…`sw11` | CZone Bilge1, Bilge2, Horn, Helm, Nav, Anchor, Red, Fly, Underwater, Bow1, Bow2 | CZone PGN 127501/127502 |
| 236–255 | — | — | spare | — |

**Rules for adding a device**: hand-write a stable, dot-free node ID (never let Node-RED generate one); add a registry row using the next free number in the correct block; deploy. Never let a virtual device register without a registry row.

**Verification** (`verify_pinning.py`): snapshots the service→instance map, `virtual_*` settings entries and the Signal K model (tanks/electrical/propulsion), restarts Node-RED three times, and fails if any snapshot gains a service, changes an instance, or grows a new Signal K path. Password via `CERBO_PASS` env var or prompt. Run it after every change to the registry or to flows containing virtual devices.

## Key Technical Notes

- The Cerbo's VE.Can `can0` port (sun4i_can driver) only passes 29-bit extended CAN frames to userspace. 11-bit standard frames are filtered at the hardware/kernel level. This is why the YDNB-07 must repackage BMS frames.
- Signal-K SOC values are ratios (0.0-1.0), not percentages. Multiply by 100 for Venus OS.
- The YDNB-07 programming language requires `match()` filter bodies on separate lines (no single-line `{ }` blocks).
- The YDNB-07 supports up to 20 `match()` filters. Currently 16 are used for BMS CAN IDs.
- `FW_CAN1_TO_CAN2=ON` only forwards frames that don't match any `match()` filter. Matched frames are handled exclusively by their filter subprogram.
