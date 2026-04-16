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
- Runs Node-Red with `@victronenergy/node-red-contrib-victron` v1.6.60 and `@signalk/node-red-embedded` v2.18.1
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
- **Staged fallback**: If CAN data goes stale, progressively restricts charge/discharge limits (LIVE -> ALERT 30s -> RESTRICT 5min -> SURVIVAL)
- **CVL control**: "Max Charge" slider (40-100%) exposed on VRM dashboard (BMS group), maps linearly to CVL range (61.96-62.4V at 100%)
- **Weekly equalization**: Every 7 days, adds +0.44V boost to the current slider CVL for 1 hour. Works at any slider position, not just 100%.
- **Synthetic alarms** (since BMS does not send 0x35A):
  - `/Alarms/LowSoc`: warning at SOC < 20%, alarm at SOC < 10%
  - `/Alarms/HighVoltage` / `/Alarms/LowVoltage`: alarm when cell delta > 100mV (imbalance)
  - `/Alarms/HighChargeCurrent`: warning when modules are blocking charge (from 0x372)
  - `/Alarms/HighDischargeCurrent`: warning when modules are blocking discharge (from 0x372)
  - `/Alarms/InternalFailure`: alarm on CAN staleness (ALERT phase+) or modules offline (from 0x372)

### CZone Control (`CZoneProxy.json`)

Bidirectional control of CZone digital switching outputs via Victron Virtual Switches.

- **State monitoring**: Signal-K subscribe to `electrical.switches.bank.1.{1-11}.state` (PGN 127501, CZone instance 1)
- **Command sending**: `cansend can0` with PGN 127502 instance 10 to toggle CZone outputs
- **11 switches**: Bilge1, Bilge2, Horn, Helm, Nav, Anchor, Red, Fly, Underwater, Bow1, Bow2
- **Latching logic**: Lights use on/off toggle; horn and bilge pumps are momentary
- **2-second suppression**: After sending a command, ignores state feedback to prevent loops

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
