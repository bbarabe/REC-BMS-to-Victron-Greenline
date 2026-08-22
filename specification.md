# Boat NMEA System Specification

## Overview

This project contains five standalone Python D-Bus drivers (`dbus-recbms/`, which ships both `dbus-recbms` and `dbus-solarpriority`, plus `dbus-czone/`, `dbus-batteries/` and `dbus-edrive/`) and device configurations for a marine vessel's electrical and battery monitoring system running on a **Victron Cerbo GX** (Venus OS). The system integrates multiple CAN bus networks and Victron's D-Bus to provide unified monitoring and control via VRM (Victron Remote Management).

Every function used to be a Node-Red flow; as of 2026-08-21 none is. The retired flows are in [`archive/`](archive/) for rollback only, and nothing on the boat depends on Signal K or Node-RED any more — the drivers come up seconds after D-Bus at boot and survive Node-RED deploys and Venus firmware updates.

## Physical Architecture

### CAN Bus Networks

There are two physically separate CAN bus networks, bridged by a Yacht Devices YDNB-07:

- **CAN1 (drive/BMS network)** — 250 kbps, limited devices:
  - REC-BMS (11-bit standard CAN frames): Victron/SMA CAN-BMS protocol at 0x351-0x404
  - Greenline 6GK electric drive system — CANopen TPDOs (11-bit) plus HCU J1939 frames; decoded by `dbus-edrive/`, wire format in `GreenlineFindings.md`
  - Yanmar engines
  - No chartplotters, switching, or general NMEA 2000 devices on this bus

- **CAN2 (main NMEA 2000 network / Cerbo GX VE.Can port = `can0`)** — 250 kbps.
  Node inventory:

  | Addr | Device (from Product Information) |
  |------|-----------------------------------|
  | `0x02` | **CZone UC1** (BEP `80-911-0120-00`) — the switching interface |
  | `0x17` | Simrad **NSX-7 MFD** (fw 2.3.190); `0x16` Navigator, `0x14` iGPS, `0x07` Pilot Controller — same unit, 4 logical nodes |
  | `0x0C` | **NAC-3 Autopilot** — also the 5 Hz PGN 127501 instance-0 "all OFF" spammer; `0x00` NMEA0183, `0x03` virtual rudder, `0x04` pilot controller, `0x0D` rudder feedback |
  | `0x05` | OP50 · `0x06` NAP44 Autopilot Controller · `0x1D` RF25 rudder feedback |
  | `0x0E` `0x18` | GS25 compass / antenna · `0x15` Precision-9 compass |
  | `0x23` | Airmar DST810 depth/speed/temp · `0x1C` CORTEX VHF/AIS |
  | `0x10` | **SIMARINE SN01 SiCOM** N2K gateway — the PGN 127506/127508 battery data source |
  | `0x01` | Sentinel Boat Monitor BM-40 · `0x21` Fusion MS-RA770 ("Galley") |
  | `0x39` | YDNB-07 bridge · `0xE3` Cerbo GX · `0x64` Signal K |

  - **There is only one CZone node on the bus** (`0x02`). Frames that appear to come from
    source addresses `0x51`-`0x81` carrying "proprietary PGN 65283" are **not devices at all**:
    they are the YDNB-07's repackaged REC-BMS frames. The bridge rewrites 11-bit id `0xNNN`
    as `0x18FF0NNN`, so the original CAN id's low byte lands where a source address would be
    (`0x351` → apparent src `0x51`) and the PGN decodes as 65283. They decode correctly
    with the REC-BMS frame map below. Any bus survey of CAN2 must exclude
    `0x18FF0000/0x1FFFF800` or it will invent phantom nodes.
  - Battery monitors: device address 16 (`n2k-on-ve.can-socket.16`) provides PGN 127506/127508 battery data
  - All other NMEA 2000 devices
  - Receives forwarded traffic from CAN1 via the YDNB-07 bridge
  - **Important**: the VE.Can port (sun4i_can) does not pass the REC-BMS 11-bit
    frames `0x351`-`0x404` to userspace, which is why the YDNB-07 must repackage
    them as 29-bit. It is *not* a blanket 11-bit filter: the drives' CANopen
    TPDOs `0x18x`/`0x28x`/`0x38x`/`0x48x` do arrive and `dbus-edrive` reads them
    directly. Check any new 11-bit id with `candump` rather than assuming.

### YDNB-07 Bridge (Firmware 1.42)

Configuration file: `YDNB.CFG`

The bridge performs two functions:
1. **NMEA 2000 forwarding**: `FW_CAN1_TO_CAN2=ON` passes all 29-bit traffic transparently
2. **BMS frame repackaging**: 16 explicit `match()` filters catch specific 11-bit BMS CAN IDs, rewrite them as 29-bit extended frames (`0x18FF0NNN`), and send to CAN2. This is necessary because the Cerbo's VE.Can port drops 11-bit frames.

The repackaging scheme: original 11-bit CAN ID `0xNNN` becomes 29-bit CAN ID `0x18FF0NNN` (proprietary broadcast PGN 0xFF00 range, priority 6). The frame type byte at buffer offset 4 is changed from `0xFE` (11-bit) to `0xFF` (29-bit).

Manual: `ydnb07.md` (transcription of the Yacht Devices YDNB-07 manual, firmware 1.42)

### Cerbo GX (Venus OS)

- **can0**: VE.Can port, 250 kbps, `sun4i_can` driver
- Runs Node-Red with `@victronenergy/node-red-contrib-victron` v1.7.18 and `@signalk/node-red-embedded` v2.18.1 — Node-Red is *embedded in the Signal K server*, not a service of its own, so restarting Node-Red means `svc -t /service/signalk-server`
- Signal-K server decodes NMEA 2000 PGNs into Signal-K paths

## CZone Digital Switching

**Bus behavior**

CZone exposes its switching over three PGNs, plus a proprietary query pair used
for discovery. All of it is broadcast, so a passive listener on `can0` sees
everything.

- **PGN 127501 Binary Switch Bank Status** — the CZone UC1 (src `0x02`)
  broadcasts every 2 s and within ~10 ms of any output change. 8 bytes: byte 0
  is the bank instance, then 28 two-bit switch fields across bytes 1-7. A field
  reads `0`/`1` for a configured circuit and `3` for one that is not configured.
- **PGN 127502 Switch Bank Control** — how a circuit is operated. The MFD uses
  CAN id `0DF20E17` (priority 3); the same frame from any source address is
  accepted, including the unclaimed `0xFE`.
- **PGN 130820 / 65299** — the CZone circuit description reply and its request
  (see below).

**Interpretation of the 127502 bits is set by the circuit's own CZone
configuration, not by the sender** — the MFD, the keypads and a driver are all
treated identically. `00` is never a command in its own right:

- **Latching circuits** (lights): `01` is a button *press* and **toggles** the
  latched output; `00` is the button release and is ignored. The same `01`
  frame sent twice therefore turns a light on and then off.
- **Momentary circuits** (bilge pumps, horn): the output **follows the bit** —
  `01` = on, `00` = off, no toggling.

CZone applies any of these in 8-15 ms.

**Bank instances.** A CZone configuration can define more than one bank over
the same physical outputs, each with its own switch types. Every bank the UC1
serves appears as its own PGN 127501 instance. Bank 1 is the user latch shared
by the MFD and the keypads and is the one to drive.

The physical output is the **OR** of the banks: a second bank can force an
output on, but its `00` cannot clear a latch set on bank 1. A control path
built on a secondary bank can therefore only ever add, never switch off
something latched at a keypad — which makes it unsuitable as the sole control
channel. Bank 1 has no such limitation.

- Another device on the bus (the Navico autopilot, src `0x0C`) broadcasts PGN
  127501 **instance 0** at 5 Hz with every field zeroed. Two consequences: the
  tracked state must never be taken from it, and — because all 28 fields read
  as configured — it looks like a 28-circuit bank to anything sizing a bank
  from the status frame. Reject any instance that is not the UC1's.

### CZone circuit discovery

A CZone bank describes itself over the bus, so nothing about it needs to be
hardcoded — not the bank instance, not the circuit count, not the names.

**Bank list and bank size come from PGN 127501.** The bank instances the UC1
broadcasts are the bank list, and the number of two-bit fields that are not `3`
("unavailable") is the circuit count:

```
bank 1   payload 01 00 00 C0 FF FF FF FF
         fields  0000000000033333333333333333
                 └─ 11 configured ─┘└──── 17 unavailable ────┘
```

A bank appears and disappears here: configure a second bank and the UC1 starts
broadcasting its instance; delete it and the instance stops.

**Names and categories come from a query/reply pair.**

*Request — PGN 65299*, single frame, broadcast, CAN id `1CFF13xx` (priority 7):

```
27 99 | 01 | circuit | bank | tag | 80 | 00
  │      │      │       │     └── correlation tag; BIT 7 MUST BE CLEAR
  │      │      │       └── bank instance
  │      │      └── 0-based circuit index
  │      └── request type: circuit description
  └── mfg 295 BEP Marine, industry group 4
```

*Reply — PGN 130820*, fast-packet, broadcast by the UC1, 2-13 ms later:

```
27 99 | 01 | tag | 80 | <NUL-terminated name> | 00 00 00 00 | CC CC | 20 00 00
             │                                                └── category mask
             └── echoes the request's correlation tag
```

The 9-byte trailer is constant except bytes 4-5, a little-endian category
bitmask:

| Mask | Bit | CZone category |
|------|-----|----------------|
| `0x0000` | — | (none) |
| `0x0004` | 2 | Navigation |
| `0x0400` | 10 | Lighting |
| `0x1000` | 12 | Pump |

The same masks appear in the `.zcf` control records, so the two sources
corroborate each other — see "The CZone configuration file" below.

Behaviours that matter when implementing this:

- **The query works from the unclaimed source address `0xFE`.** No address
  claim, no product-information handshake, no membership in the UC1's node list.
- **Bit 7 of the correlation tag is a flag, not part of the tag.** Set it and
  the UC1 answers with a well-formed but *empty* record
  `27 99 01 <tag & 0x7F> 80 00`, which is indistinguishable from "no such
  circuit". Always mask the tag to 7 bits.
- **An empty name means the circuit does not exist**, so a bank can be sized by
  enumerating until the replies come back empty.
- Replies are broadcast, so a listener sees the answers to every node's queries.
- Other manufacturers also use PGN 130820 with their own header; check the
  first two bytes for `27 99` before decoding.

**The UC1 does not answer ISO Requests.** It replies only to 60928 (address
claim) and 126996 (product information); requests for 130820, 126998 or 127501
are refused with an ISO Acknowledgement of `1` (NAK). Discovery must use the
PGN 65299 query above, and there is no point requesting 127501 — it is
broadcast every 2 s regardless.

### Switch type is not on the bus — it is in the `.zcf`

The latching/momentary distinction is absent from every message the UC1 sends.
It is not in the PGN 130820 record (whose trailer is fully accounted for by the
category mask), not in any of the UC1's other proprietary PGNs, and no field
broadcast on the bus separates the momentary circuits from the latching ones.

Note that the **category is not a proxy for it**: the horn is categorised
*Navigation* alongside the nav and anchor lights but is momentary, while the
bilge pumps are momentary under *Pump*.

Nothing driving a circuit over PGN 127502 needs the information: the MFD, the
keypads and a driver all send the same press-and-release and let the UC1's
circuit logic decide. (The keypads' *own* PGN 65280 traffic does distinguish
the two — see below — but that is a report of which kind of button was
pressed, not a description of the circuit, and it only appears when somebody
presses one.)

It *is* recorded in the CZone configuration file, one level below the bus: see
"The CZone configuration file" below. `zcf_parse.py` reads it straight out of
[`48-56.zcf`](48-56.zcf) and prints the `momentary_outputs` line to paste into
`config.ini`. That file is not on the bus, though — it lives on a laptop or
inside the MFD — so a driver still cannot discover the split at runtime.

Consequence for a driver: the type has to be configured rather than discovered.
`dbus-czone` keeps it in Venus's own per-output `Settings/Type`, seeded from
`config.ini` and editable in the GUI, so a change to the CZone configuration
does not require a code change. What the `.zcf` buys is that the seed is now
*derived from the boat's own configuration* instead of guessed.

### The CZone configuration file (`.zcf`)

[`48-56.zcf`](48-56.zcf) is this boat's CZone configuration as written by the
CZone Configuration Tool. Read it with:

```sh
python zcf_parse.py 48-56.zcf --raw
```

The format was reverse-engineered by [negrusti/esp32-czone][ez] and is
documented in `zcf_parse.py`'s docstring. Every structural claim in it was
re-verified here against two independently produced files — ours and the one
shipped with [gerryvel/SR-Aktor][sr] — including both CRC-8s (poly 0x07), the
`08 ?? 05 0E` control-section marker, the record layout and the loads section.

Three fields matter:

- **Commander byte 3 is the switch type.** `0x09` momentary, `0x01`/`0x03`/
  `0x04` latching. It sits in the *commander* (button) record, not the circuit
  — which matches how the UC1 behaves: the type is a property of the press,
  and the circuit logic applies it. On this boat it splits 11/11 in agreement
  with `momentary_outputs`.
- **Control-record field 1 is the category bitmask**, the same value PGN 130820
  puts in its trailer. Confirmed on all 11 circuits.
- **The output entry gives the physical channel**, as
  `(dipswitch << 8) | channel`.

Field 0 was `0x0000` and field 2 `0x0020` on all 14 circuits across both files,
momentary ones included, so neither is the type.

[ez]: https://github.com/negrusti/esp32-czone
[sr]: https://github.com/gerryvel/SR-Aktor

**This boat's bank** (dipswitch 1, all 11 circuits on bank instance 1):

| Output | Circuit ID | Channel | Name | Category | Type |
|---|---|---|---|---|---|
| 1 | 5 | 12 | Bilge Pump FW | Pump | momentary |
| 2 | 6 | 13 | Bilge Pump AFT | Pump | momentary |
| 3 | 7 | 14 | Horn | Navigation | momentary |
| 4 | 8 | 0 | Illumination Helm | Lighting | latching |
| 5 | 9 | 2 | Navigation Lights | Navigation | latching |
| 6 | 10 | 3 | Anchor Light | Navigation | latching |
| 7 | 11 | 4 | Red Light | Lighting | latching |
| 8 | 12 | 1 | Illumination Fly | (none) | latching |
| 9 | 13 | 5 | Underwater Light | Lighting | latching |
| 10 | 14 | 6 | Bow light | Lighting | latching |
| 11 | 15 | 7 | Bow IR light | Lighting | latching |

**A circuit ID is not a bank index.** The bank enumerates its circuits in
ascending circuit-ID order, and this configuration's IDs start at 5 — so
output *N* on PGN 127501/127502 and in a PGN 65299 query is circuit ID *N+4*.
PGN 65280 (below) carries the **circuit ID**; everything else carries the bank
index. Reading one as the other silently names the wrong circuit.

The channel map also rules out any assumption that a CZone module lays its
outputs out in one contiguous block: this UC1 uses channels 0-7 and 12-14.

### PGN 65280 — what the keypads and the MFD actually send

Not needed to drive a circuit (`dbus-czone` uses 127502), but it is the traffic
a listener sees when someone presses a physical button, and it is the one place
on the bus where the switch type shows through.

```
27 99 | 05 00 | 00 | 01 | F1 | 00
  |       |      |    |    |    +-- flags; 0x08 on B&G marks the toggle family
  |       |      |    |    +------- command byte, see below
  |       |      |    +------------ target module address (dipswitch)
  |       |      +----------------- varies; 0/1/2 seen
  |       +------------------------ u16 CIRCUIT ID (not the bank index)
  +-------------------------------- mfg 295 BEP Marine, industry group 4
```

Two command families, and they follow the circuit's type:

| Command | Meaning | Seen on |
|---|---|---|
| `F1` (also `01`, `11`) | absolute ON | momentary circuits |
| `42` | release of the above | momentary circuits |
| `71`, `72` | toggle press; the MFD alternates the two | latching circuits |
| `40`, `62` | release of a toggle press | latching circuits |
| `91` | unexplained; 12 frames on the horn | — |

In [`captures/czone.log`](captures/czone.log) circuits 5 and 7 (Bilge Pump FW,
Horn — both momentary) use `F1`/`42`, while circuits 9 and 10 (Navigation
Lights, Anchor Light — both latching) use `71`/`72` with `40`/`62`. The same
split is documented independently in [SR-Aktor][sr] from a B&G Vulcan. It is a
usable passive check on a circuit's type, but only for circuits somebody
actually presses while you are capturing.

The `id - 4` output mapping is confirmed on the wire, not just from the file:
each `71`/`72` on circuit 10 is followed 1 ms later by a PGN 127501 change on
**Anchor**, output 6. `nmea_decode.py` decodes this PGN and does the mapping.

**The header is not always `27 99`.** B&G/Navico MFDs use `13 99` for the same
layout — in [`captures/mfd-boot.log`](captures/mfd-boot.log) the MFD at src
`0x17` sends `08FF0017 # 13 99 05 00 00 00 00 00`. Match on either.

## Battery Systems

### Main Battery Bank (REC-BMS via CAN-BMS protocol)

- **Chemistry**: 15S NMC
- **Capacity**: 1440 Ah rated (~83 kWh at 58V), 1400 Ah configured in the BMS — the pack reports both, on 0x379 and 0x35F respectively
- **BMS**: REC-BMS
- **6 modules**: C8SU1/C7SU1, C14SU2, C13SU4, T1SU3, T1SU4
- **CAN protocol**: Victron/SMA CAN-BMS, 11-bit standard CAN IDs

CAN frame map (all 8-byte DLC unless noted):

| CAN ID | Name | Key fields |
|--------|------|------------|
| 0x351 | Charge/Discharge Limits | CVL, CCL, DCL, DVL (16-bit LE, 0.1V/A scale) |
| 0x355 | SOC/SOH | SOC%, SOH%, hi-res SOC (0.01%) |
| 0x356 | Voltage/Current/Temp | Pack V (0.01V), current (0.1A signed), temp (0.1C), charge cycles (bytes 6-7) |
| 0x35E | Manufacturer | ASCII "REC-BMS" |
| 0x35F | `BatteryInfo` | Chemistry id (byte 0); version pair (bytes 2-3, two plain bytes `major.minor` -> `/HardwareVersion` 2.9); **capacity configured in the BMS** (bytes 4-5 LE, 1400 Ah -> `/RecBms/ConfiguredCapacity`). Bytes 4-5 are **not** a firmware version — reading them as one published a bogus 1400 to `/FirmwareVersion` before driver v1.3.1 |
| 0x360 | Force Charge | Byte 0: 0xFF nominally = force charge requested — but this REC asserts 0xFF **permanently** (observed at 65% SOC, balanced cells, at rest), so it is a static capability flag carrying no state. Not forwarded to Venus (`forward_charge_request = false`); on current firmware `/Info/ChargeRequest` is consumed by systemstate.py (GUI "Recharge" state), gui-v2 (display row) and hub4control (ESS forced grid recharge — inert here: no ESS assistant in the Quattro). DVCC and mk2-dbus ignore it, so the Quattro itself never sees it. |
| 0x372 | Module Status | Byte 0: modules online, Byte 2: blocking charge, Byte 4: blocking discharge, Byte 6: offline |
| 0x373 | Cell Min/Max | Min/max cell voltage (mV), min/max cell temp (K) |
| 0x374-0x377 | Extreme-cell IDs | ASCII labels of the cells/sensors currently holding the extremes: 0x374 min-V cell, 0x375 max-V cell, 0x376 min-T sensor, 0x377 max-T sensor. The strings track the extremes; they are not static module IDs. |
| 0x379 | `BatterySize` | Installed (**rated**) capacity, 1440 Ah — constant regardless of SOC, not remaining capacity. The protocol's designated source for `/InstalledCapacity`, so `/Capacity`, `/ConsumedAmphours` and `/TimeToGo` derive from it; distinct from 0x35F's 1400 Ah configured capacity |
| 0x380 | Serial Number | ASCII serial |
| 0x381 | Unknown | Additional data (not yet decoded) |
| 0x404 | Heartbeat | Keep-alive (DLC=3) |

**Note**: This REC-BMS does **not** send the standard CAN-BMS alarm frame (0x35A). `dbus-recbms` generates synthetic alarms based on available data (SOC thresholds, cell imbalance from 0x373, module status from 0x372, CAN staleness).

The table above is the authoritative decode. `Decoded.md` holds the field-by-field working (scales, worked examples); `BatteryCAN.md` is a raw one-frame-per-id capture.

### 12V Batteries

Three 12V batteries are monitored by an NMEA 2000 device at address 16 (PGN 127506/127508). The two engine batteries are not on that monitor — their voltage comes from the Yanmar gateways' PGN 127489 (Engine Parameters, Dynamic) *Alternator Potential* field (0.01V resolution, ~2 Hz). The gateways are powered from the **ignition**, not the engine: ignition on with the engine stopped still gives a reading, but with the ignition off 127489 is absent from the bus entirely and the engine batteries do not exist as far as the Cerbo is concerned (confirmed 2026-08-21). 127489 carries no SOC or current.

| Signal-K Path | Name | Source | Available Data |
|---------------|------|--------|---------------|
| electrical.batteries.1 | House | Addr 16, PGN 127506/127508 | Voltage, current, SOC, SOH, time remaining |
| electrical.batteries.2 | Bow | Addr 16, PGN 127506/127508 | Voltage, SOC, SOH, time remaining |
| electrical.batteries.3 | Stern | Addr 16, PGN 127506/127508 | Voltage, SOC, SOH, time remaining |
| propulsion.port.alternatorVoltage | Port Engine | Src 141 (0x8D), PGN 127489, engine instance 0 | Voltage only |
| propulsion.starboard.alternatorVoltage | Starboard Engine | Src 142 (0x8E), PGN 127489, engine instance 1 | Voltage only |

The 127489 messages also carry engine hours and engine temperature; oil temp, coolant pressure, and fuel pressure are not populated (0xFFFF) by these gateways.

## REC-BMS Driver (`dbus-recbms/`)

Standalone Python D-Bus service that replaces the Node-RED Virtual BMS flow. Runs under daemontools (`/service/dbus-recbms`, installed via `install.sh`, survives firmware updates through `/data/rc.local`), starts seconds after D-Bus at boot — independent of Signal K / Node-RED (which took minutes to bring the virtual battery up) — and is untouched by Node-RED deploys (which could wedge the D-Bus connection and force a reboot).

**Data path**: raw SocketCAN socket on `can0` (kernel filter `0x18FF0000/0x1FFFF800`, 29-bit only) -> `decode_frame()` (strips wrapper: `id & 0x7FF`) -> 1s publish tick -> `com.victronenergy.battery.recbms` (instance 200) + `com.victronenergy.switch.recbms_maxcharge` (VRM "Max Charge" slider, instance 220).

Behavior is a 1:1 port of the flow (staged fallback, startup grace, Quattro voltage fallback, slider→CVL, weekly EQ, synthetic alarms — all documented below; thresholds/timings live in `dbus-recbms/config.ini`). Differences from the flow:

- No candump subprocess, watchdog, pkill, or chunked line parsing — the kernel filters and delivers frames directly.
- Persistence moved from the `virtual-bms-state.json` writable-dir hack to localsettings (`/Settings/RecBms/ChargeSlider`, `/Settings/RecBms/EqLastCompleted`, `/Settings/RecBms/CustomName`) — no restore/retry dance, saves are synchronous.
- Instance pinning is built in: the driver seeds `/Settings/Devices/recbms|recbms_maxcharge/ClassAndVrmInstance` and reconverges to 200/220 at startup if localsettings granted something else while the wanted instance is free (registry-style self-heal). The settings ids deliberately have no `virtual_` prefix so the Node-RED palette's auto-cleanup never touches them.
- Publishes paths the virtual battery node rejected: hi-res `/Soc` (0.01% from 0x355 bytes 4-5), `/Soh`, `/Capacity` (remaining = SOC × installed), `/ConsumedAmphours` (negative, BMV convention), `/InstalledCapacity` (from 0x379), `/TimeToGo`, `/System/MinCellVoltage`–`MaxCellTemperature` (cell extremes from 0x373), `/System/Min|MaxVoltageCellId` + `/System/Min|MaxTemperatureCellId` (extreme-cell identity from 0x374-0x377), module counts from 0x372, `/History/ChargeCycles`, live `/Serial` + FW/HW version, plus diagnostics: `/RecBms/Phase`, `/RecBms/EqStatus`, `/RecBms/TimeToFull` (from 60s-smoothed charge current) and `/RecBms/ForceChargeRequest` (0x360; forwarded to `/Info/ChargeRequest` only if `forward_charge_request = true` in config).

- **Solar boost (v1.2.0)**: writeable `/RecBms/SolarBoost/Request` raises only the solar chargers above the Quattro's regulation point (via the systemcalc `SolarVoltageOffset`, which applies to MPPTs only), so they run unthrottled and their output can be measured as capacity without dropping shore power. Gated every tick on live cell data (max cell 4.05V, temps 5–45°C, ceiling 62.70V, ≥0.10V margin over pack voltage); hard 120s expiry, cleared at startup/shutdown — never sticky.
- **Lead verification (v1.4.0)**: systemcalc applies the Debug voltage offsets only while the GX access level is **Superuser** (`/Settings/System/AccessLevel > 2`, evaluated once per systemcalc process), and the D-Bus write succeeds regardless. The driver therefore compares `com.victronenergy.system /Control/EffectiveChargeVoltage` (what DVCC really sends the MPPTs) with published CVL + written offset every 3 s; a 10 s mismatch is a **lead fault**: full target published (lead 0), boosts refused, `/RecBms/LeadFault` explains (access level + fix), `/Alarms/InternalFailure` raised to warning so the GUI notifies and VRM can mail. Self-clears once the offset is seen applied (raise access level, `svc -t /service/dbus-systemcalc-py`). Also pins `/Settings/SystemSetup/BmsInstance` to 200 while it is on automatic. See `dbus-recbms/README.md` for the other dvcc.py facts (3 s cadence, user voltage cap does not cap a boost, driver death → MPPT error #67).
- **Solar lead (v1.3.0)**: the Quattro's absorption holds +0.05–0.15V *above* its commanded CVL (measured 2026-08-19: SVS on, BMS/Quattro/sense meters within 10mV, `VebusChargeState` = absorption — the bias is in its regulation, not a sense error). The driver therefore publishes `/Info/MaxChargeVoltage` = target − `solar_lead_v` (0.15V) and holds `SolarVoltageOffset` at the lead, so only the MPPTs see the true target: the Quattro lands at or just under the calibrated equilibrium and the accurately-regulating MPPTs finish the top-off. True target on `/RecBms/TargetChargeVoltage`, lead in force on `/RecBms/SolarLead`; if the offset write fails the driver publishes the full target (fail-safe to pre-lead behavior). Side effect: below target only solar has charging headroom on shore.

Install/migration/rollback procedure: `dbus-recbms/README.md`.

## CZone Driver (`dbus-czone/`)

Standalone Python D-Bus service that replaces the Node-RED CZone flow. Runs
under daemontools (`/service/dbus-czone`, installed via `install.sh`, survives
firmware updates through `/data/rc.local`), starts seconds after D-Bus at boot
independently of Signal K / Node-RED, and is untouched by Node-RED deploys.

**Publishes one multi-output switch bank**, the way Venus models the GX IO
extender (`/opt/victronenergy/dbus-switch/dbus-switch.py`):
`com.victronenergy.switch.czone` (instance 224) with
`/SwitchableOutput/output_1..output_N`. The whole bank costs a single device
instance instead of the eleven the flow consumed, and VRM renders it as one unit.

**Data path**: raw SocketCAN socket on `can0` (kernel filters for PGN 127501 and
130820, 29-bit only) — no `candump` subprocess, no watchdog, no line parsing.

**Startup** (~5 s): listen to PGN 127501 for the bank list and circuit count,
then query each circuit for its name and category over PGN 65299/130820 (both
described under CZone Digital Switching above). Names become the output names,
categories become the Venus groups. The discovered table is cached in
localsettings (`/Settings/CZone/Circuits`) and used if the bus is silent because
CZone is powered down.

**Control**: PGN 127502 on the bank-1 instance. Latching circuits get a press
plus a release 120 ms later, and only when a fresh 127501 reading disagrees with
the target — a press toggles, so a command on stale state is refused rather than
guessed. Momentary circuits get direct level control. A retry is issued only when
a status frame newer than the last send still disagrees, at most once, because
re-pressing a latching circuit would toggle it back; an unconfirmed command
reverts the switch to the real CZone state after 9 s.

**No keep-alive and no echo guard.** CZone latches a bank-1 press natively, and
the driver owns both sides of its D-Bus service — its own updates never re-enter
as commands, unlike the virtual-switch nodes in the flow.

The momentary/latching split is not broadcast by CZone (see "Switch type is not
on the bus"), so it lives in Venus's per-output `Settings/Type` — seeded from
`dbus-czone/config.ini`, persisted, and editable in the GUI. The seed itself is
derived from the boat's `.zcf` with `zcf_parse.py`, not guessed.

Install/migration/rollback procedure: `dbus-czone/README.md`.

## 12V Battery Driver (`dbus-batteries/`)

Standalone Python D-Bus service that replaces the Node-RED Batteries Forward
flow. Runs under daemontools (`/service/dbus-batteries`, installed via
`install.sh`, survives firmware updates through `/data/rc.local`), starts
seconds after D-Bus at boot, and is untouched by Node-RED deploys.

**Data path**: raw SocketCAN socket on `can0` (kernel filters for PGN 127508,
127506 and 127489, 29-bit only) -> one `com.victronenergy.battery.n2kbat_<name>`
per forwarded source. The flow read Signal K paths instead, so the batteries
only appeared once the whole Signal K stack was up and its N2K decoder had
converged.

| Key | Battery | Instance | Source |
|---|---|---|---|
| `bat1` | House | 201 | SiCOM addr `0x10`, PGN 127508 + 127506 |
| `bat2` | Bow | 202 | same |
| `bat3` | Stern | 203 | same |
| `alt0` | Port Engine | 204 | Yanmar gateway src `0x8D`, PGN 127489, engine instance 0 |
| `alt1` | Starboard Engine | 205 | Yanmar gateway src `0x8E`, PGN 127489, engine instance 1 |

Instances carried over unchanged from the retired registry, so VRM history and
Signal K paths survive the migration. Field decode:

| PGN | Layout |
|---|---|
| 127508 Battery Status (single frame) | instance \| V (0.01 V, u16) \| I (0.1 A, s16) \| temperature (0.01 K, u16) \| SID |
| 127506 DC Detailed Status (fast packet) | SID \| instance \| DC type \| SOC % \| SOH % \| time remaining (min, u16) \| ripple |
| 127489 Engine Parameters, Dynamic (fast packet) | instance \| oil P \| oil T \| coolant T \| **alternator potential** (0.01 V, s16) \| … |

127508 and 127506 are verified against `captures/mfd-boot.log` and confirmed
live on the boat (House 14.15 V / −0.2 A / SOC 100, Bow 13.76 V, Stern 13.65 V).
127489's alternator field is per the NMEA 2000 layout and is **still
unverified**: it has never appeared in a capture in this repo, and at first
deploy the Yanmar ignition was off, so the gateways were unpowered. Reserved codes
(`0xFFFF`, `0x7FFF`) decode to *no value*, never to 655.35 V.

A DC source gets `/Dc/0/Voltage|Current|Power|Temperature`, `/Soc`, `/Soh`,
`/TimeToGo` and — when a capacity is configured — `/InstalledCapacity`,
`/Capacity` and `/ConsumedAmphours` (negative, BMV convention). An alternator
source gets `/Dc/0/Voltage` only: 127489 carries no current and no SOC, and a
never-updated `/Soc` showing a plausible number is worse than no `/Soc` at all
(the flow's `default_values: false`, done properly).

Staleness is two-stage: after `stale_after_s` (10 s) `/Connected` drops to 0 and
every measurement is **blanked** — a frozen 12.6 V on a start battery reads
exactly like a healthy one — and after `unpublish_after_s` (300 s) the battery
leaves D-Bus entirely. A source never seen is never published. That is why the
engine batteries are normally absent: the Yanmar gateways are unpowered with the
ignition off, so 127489 is not on the bus at all, and a permanently disconnected
VRM device is worse than none. The source keeps its settings, its name and its
**reserved instance** and returns on the same number one frame after ignition.

**Which batteries are forwarded is data, not code.** Every source the bus
mentions is catalogued under a key derived from the wire (`bat<n>` for N2K
battery instance *n*, `alt<n>` for engine instance *n*) — never from a list
position. A catalogued but disabled source costs nothing: no instance, no
service, no VRM entry. Two equivalent control surfaces, both persisting to the
same localsettings values and both applied immediately (the battery service is
created or torn down in place, no restart):

- `com.victronenergy.settings` — `/Settings/N2kBatteries/<key>/Enabled`,
  `/Instance`, `/CustomName`, `/ServiceSuffix`, `/Capacity`, plus
  `/Settings/N2kBatteries/Catalog` (JSON) and `/NextInstance`.
- `com.victronenergy.n2kbatteries` — `/Sources/<key>/Enabled`,
  `/DeviceInstance`, `/CustomName`, `/Capacity` (all writable) alongside
  read-only `/Kind`, `/N2kInstance`, `/SourceAddress`, `/Available`,
  `/Published`, `/Age`, `/Fields`, and `/Catalog` for a one-read render of the
  whole picker. `Enabled` is the standing choice, `Published` is the bus's
  answer.

This is the surface a forked Victron HTML5 MFD app is meant to drive. Only the
*stable* description is persisted; liveness lives on `/Available` and `/Age`, so
a present battery does not cause a localsettings write every second. Settings
written by something else are picked up from `PropertiesChanged`, with a 30 s
reconcile as backstop.

`config.ini` values under `[sources]` are **first-run defaults only** — once a
setting exists, the user's choice wins, the same contract as `dbus-czone`'s
per-output `Type`. A battery that turns up and is not in `config.ini` is
catalogued and left disabled (`auto_enable_new = false`); enabling it draws the
next number from `instance_pool` (206-209), monotonically and never reused.

Install/migration/rollback: `dbus-batteries/README.md`. Migration needs
`migrate.sh` on the boat to delete `/Settings/Devices/virtual_bat<N>_virtual`,
or localsettings will not hand 201-205 back.

## E-Drive Driver (`dbus-edrive/`)

Standalone Python D-Bus service that replaces the Node-RED Greenline E-Drive
flow. Runs under daemontools (`/service/dbus-edrive`), starts seconds after
D-Bus at boot, and is untouched by Node-RED deploys. Publishes the two drives as
`com.victronenergy.motordrive.edrive_port` (instance 210) and `edrive_stbd`
(211). **Read-only**: the socket is bound with a receive filter set and the
driver never transmits, so it cannot influence the drive system.

**Data path**: raw SocketCAN socket on `can0` — 8 filter entries for the 11-bit
CANopen ids plus 5 for the J1939/N2K PGNs. No `candump` subprocess, no `pkill`,
no watchdog, no 5 s restart loop, no chunk-safe line parsing.

Both frame families are needed. The **CANopen TPDOs** (11-bit `0x18x`/`0x28x`/
`0x38x`/`0x48x`, `A` = port node `0x0A`, `B` = starboard `0x0B`) carry IEEE
floats at full resolution; the **HCU J1939 frames** (`61451`/`61452`/`61453`)
carry quantized values, but two quantities have no float source at all. Note the
source-address inversion: on the J1939 frames `0x64` is **starboard** and `0x65`
is **port**. All three mappings live in `config.ini`, one `[drive.<key>]`
section per motor.

| CAN id / PGN | Content |
|---|---|
| `0x18x` | status byte, `01` = running |
| `0x28x` | float 1 = MOSFET temp °C, float 2 = drive (motor) temp °C |
| `0x38x` | float 1 = motor voltage V, float 2 = phase current RMS A |
| `0x48x` | float 1 = **motor torque N·m**, float 2 = motor RPM |
| 61451 `0x14F00Bxx` | byte 1 − 125 = torque % |
| 61452 `0x14F00Cxx` | w2 = DC current, w4 ÷ 20 = phase current peak A |
| 61453 `0x14F00Dxx` | w0/w1/w2 × 0.03125 − 273 = MOSFET / drive / MCU-HCU temp °C |
| 65363 `0x18FF53xx` | byte 0 = instance, bytes 1–2 LE ÷ 9.280 = throttle % |
| 127493 | byte 0 = instance, byte 1 bits 0–1 = gear (`0xFC` Fwd, `0xFD` Neutral, `0xFE` Rev) |

**`0x48x` float 1 is torque, not power × 100 W.** The drives do not transmit
motor power at all — the MFD computes it — so `/Dc/0/Power` is derived as
voltage × current instead. Details and the calibration in `GreenlineFindings.md`.

**Temperatures**: all three the MFD shows per drive are published (solved
2026-08-18, see `edrive_temps.py` for a live read). The Victron `motordrive`
class has exactly **nine** paths, so the mapping is forced: the MOSFET is what
the coolant loop cools, so it feeds `/Coolant/Temperature`; `/Motor/Temperature`
is the drive/motor temp; `/Controller/Temperature` is the MCU/HCU board, which
only 61453 w2 reports and only in whole degrees. 61453 is quantized to 1 °C, so
a sensor sitting on an integer boundary dithers ±1 °C frame to frame — exactly
what the display shows.

| motordrive path | Source |
|---|---|
| `/Dc/0/Voltage` | `0x38x` f1 |
| `/Dc/0/Current` | 61452 w2 (measured), else T·ω ÷ V |
| `/Dc/0/Power` | voltage × current (derived) |
| `/Motor/RPM` | `0x48x` f2, clamped to 0 below 1 rpm |
| `/Motor/Direction` | 127493 gear |
| `/Motor/Temperature` | `0x28x` f2 (drive/motor), else 61453 w1 |
| `/Controller/Temperature` | 61453 w2 (MCU/HCU) |
| `/Coolant/Temperature` | `0x28x` f1 (MOSFET), else 61453 w0 |
| `/Connected` | frame staleness, per drive (6 s) |

The calibrated fits — DC current `800.409 − 0.024918 × w2`, throttle
`raw ÷ 9.280`, phase peak `w4 ÷ 20` — live in `config.ini`, never in the code.
The throttle scale is exact over the 0–8.1 % that was exercised; full travel
extrapolates to raw ≈ 928 and is **unverified**.

Everything with no `motordrive` path is published under `/EDrive/` on the same
service — `TorqueNm`, `TorquePercent`, `ThrottlePercent`, `PhaseCurrentRms`,
`PhaseCurrentPeak`, `MechanicalPower`, `MosfetTemperature`, `Running`,
`StatusByte`. In the flow this telemetry could only reach a debug node; on D-Bus
it is visible to Signal K, MQTT and the HTML5 app like anything else.

Staleness is per drive, so one drive powered down does not blank the other, and
only the drives' own frames count: 127493 (gear) and 65363 (throttle) come from
the Yanmar gateway, which keeps broadcasting with the hybrid system down. The
flow treated them as a heartbeat, so a drive that had been off for hours still
read `/Connected = 1` with every measurement blank. With the hybrid system off
both services now stay registered with `/Connected = 0` and blank values — the
expected dock state, not a fault.

Install/migration/rollback: `dbus-edrive/README.md`. Migration needs
`migrate.sh` on the boat to delete `/Settings/Devices/virtual_gl6gk_port|stbd`,
or localsettings will not hand 210/211 back.


## Solar Priority Driver (`dbus-recbms/solar_priority.py`) — DEPLOYED 2026-08-21

The retired flow (algorithm described under **Retired Node-RED flows** below), ported line-for-line to a standalone daemontools service (`/service/dbus-solarpriority`) that ships in the `dbus-recbms/` package but stays separate (own config `solar_priority.ini`, own log, own D-Bus service `com.victronenergy.switch.solarpriority` instance 221 carrying the toggle and the PV-capacity slider as two outputs). Inputs via velib `DbusMonitor` (last-known-good + heartbeat liveness, as in the flow); outputs `IgnoreAcIn1` and the dbus-recbms boost request over D-Bus. Differences from the flow are deliberate and listed in `dbus-recbms/README.md`: `IgnoreAcIn1` forced to 0 on exit/SIGTERM/exception, FAULT/emergency lockouts are hard 1 h holds (the flow's evidence release cleared them), transitions logged durably. The flow and the driver must never run together (both write `IgnoreAcIn1`); migration steps in the README. `SolarPriority.json` is now in `archive/` and instance 222 is retired.

## Device Instance Allocation

Every device on the boat is published by a standalone driver, and each
driver pins the numbers it owns through
`/Settings/Devices/<id>/ClassAndVrmInstance` at startup, reconverging if
localsettings granted something else. The settings ids deliberately have
**no** `virtual_` prefix, so the Node-RED palette's auto-cleanup can never
remove them.

system-wide view):

| Instance | Class | Service `com.victronenergy.…` | Device | Owner |
|----------|-------|-------------------------------|--------|-------|
| 200 | battery | `battery.recbms` | REC-BMS Main Bank | `dbus-recbms` |
| 201 | battery | `battery.n2kbat_house` | House 12V | `dbus-batteries` |
| 202 | battery | `battery.n2kbat_bow` | Bow 12V | `dbus-batteries` |
| 203 | battery | `battery.n2kbat_stern` | Stern 12V | `dbus-batteries` |
| 204 | battery | `battery.n2kbat_porteng` | Port Engine 12V | `dbus-batteries` |
| 205 | battery | `battery.n2kbat_stbdeng` | Starboard Engine 12V | `dbus-batteries` |
| 206–209 | battery | — | spare pool for newly discovered N2K batteries | `dbus-batteries` |
| 210 | motordrive | `motordrive.edrive_port` | Port E-Motor | `dbus-edrive` |
| 211 | motordrive | `motordrive.edrive_stbd` | Starboard E-Motor | `dbus-edrive` |
| 220 | switch | `switch.recbms_maxcharge` | Max Charge Slider | `dbus-recbms` |
| 221 | switch | `switch.solarpriority` | Solar Priority toggle + PV Capacity slider | `dbus-solarpriority` |
| 222 | — | *(retired)* | was `virtual_sp_rated_switch`, folded into 221 output_2 | — |
| 224 | switch | `switch.czone` | CZone switch bank (all circuits, one device) | `dbus-czone` |
| 225–235 | — | *(retired)* | was `virtual_cz_vs_sw1`…`sw11`, replaced by the bank at 224 | — |
| 236–255 | — | — | spare | — |

Reserved block **200–255**, far above anything Venus auto-assigns (native
devices allocate from 0 upward): 200–209 batteries, 210–219 motor drives,
220–239 switches, 240–255 spare.

**localsettings will not let two `/Settings/Devices/*` entries claim one
instance, and it refuses the write silently** — `SetValue` reports success and
keeps the old number (confirmed on the boat 2026-08-21, with the retired
Node-RED `virtual_*` entries still in place: every driver restart logged
`reconverged 206 -> 201` and nothing ever stuck). A driver must therefore
re-read after pinning, and a flow's entries must be removed before its
replacement can claim the same numbers. `RemoveSettings` takes the **leaf**
path (`Devices/<id>/ClassAndVrmInstance`); handed the group (`Devices/<id>`) it
returns `-1` for every entry and changes nothing, also silently.

**Never reuse a number, even after deleting a device.** VRM and Signal K key
their history off the instance, so a recycled number silently merges two
different devices' histories. 222 and 225–235 are retired, not spare. The rule
outlived the registry: `dbus-batteries` allocates from its pool monotonically
through `/Settings/N2kBatteries/NextInstance`, and nothing anywhere derives an
instance from an array index, discovery order, a loop counter, a timestamp or a
hash.

**Verification** (`verify_pinning.py`): snapshots the service→instance map,
`virtual_*` settings entries and the Signal K model (tanks/electrical/
propulsion), restarts Node-RED three times, and fails if any snapshot gains a
service, changes an instance, or grows a new Signal K path. Still useful after a
Venus firmware update, but its Node-RED restart loop no longer proves much — the
drivers do not restart with Node-RED. Restarting the *drivers* is now the
meaningful test.


## Retired Node-RED flows (`archive/`)

Every one of them was replaced by a standalone driver above, and each is kept
only for rollback. **None may run alongside its driver** — see
`archive/README.md` for the collisions and the rollback procedure. What follows
describes them as they were on their retirement date; every fix and calibration
since lives in the drivers.

With the Instance Registry retired there are **no active Node-RED flows left**:
nothing on the boat depends on Signal K or Node-RED any more.

### Batteries Forward (`archive/BatteriesForward.json`) — RETIRED

Subscribed to Signal K `electrical.batteries.1..3.*` and
`propulsion.{port,starboard}.alternatorVoltage`, assembled them in flow context,
and published five `victron-virtual` batteries on a 1 s tick with a 10 s
staleness check. SOC arrived as a Signal K ratio and was multiplied by 100.
Replaced by [`dbus-batteries/`](../dbus-batteries/), which reads the same three
PGNs directly off `can0` and makes the choice of which batteries to forward a
runtime setting rather than five hardcoded nodes.

### Greenline E-Drive (`archive/GreenlineEDriveFlow.json`) — RETIRED

Ran `candump -L can0,<13 filters>` as an exec-node subprocess, parsed the
chunked stdout, and published two `victron-virtual` motordrives; a watchdog
`pkill`ed any stale capture and restarted 5 s after candump exited. The wire
decode it implemented is unchanged and now lives in
[`dbus-edrive/`](../dbus-edrive/), which reads the bus with kernel filters and
starts no subprocess.


### Virtual BMS (`archive/Virtual BMS.json`) — RETIRED

**Replaced by the standalone `dbus-recbms/` driver (see above). Kept in the repo for rollback only — do not deploy alongside the driver (they claim the same instances 200/220).**

Reads repackaged BMS CAN frames from `can0` via `candump`, decodes them, and publishes to a Victron virtual battery device on D-Bus.

**Data path**: candump can0 (filter `18FF0000:1FFFF800`) -> Line Parser (strips 29-bit wrapper: `rawid & 0x7FF`) -> CAN Frame Decoder -> State Assembler -> Path Filter -> victron-virtual battery

Key features (all ported to the driver):
- **candump watchdog**: Before every start, `pkill -f '[c]andump can0,18FF0000:1FFFF800'` clears any stale capture process (prevents duplicates after a hard Node-Red restart or manual re-trigger). The pattern matches this flow's exact command line — filter argument included — so manually-run or other-purpose candump processes are never touched. If candump exits for any reason, a watchdog restarts it after 5 seconds (restart count kept in flow context).
- **Chunk-safe line parsing**: exec stdout arrives in chunks, not lines. The parser buffers partial lines across chunks, splits on newlines, and emits one message per CAN frame — no frames are lost when the pipe coalesces multiple lines.
- **Frame length guard**: Each CAN ID has a minimum-length requirement; short/corrupt frames are dropped (with a rate-limited warning, max 1/min) instead of throwing mid-decode. Only successfully decoded frames refresh the staleness timestamp.
- **Staged fallback**: If CAN data goes stale, progressively restricts charge/discharge limits (LIVE ≤60s → ALERT ≤2min → RESTRICT ≤5min → SURVIVAL). When BMS is stale, pack voltage is sourced from the Quattro's `/Dc/0/Voltage` (independent measurement), with fallback to last-known BMS voltage if the Quattro reading is also stale (>30s).
- **Startup grace (cold-boot fix)**: A cold boot is not treated as CAN loss. Until the first frame ever decodes, the staleness clock is anchored to flow start (not epoch 0) and the flow holds a benign STARTUP phase for up to 3 minutes: CCL 0 (no blind charging), DCL 100A (inverter keeps running), DVL 52.0V (below resting pack voltage, so no low-battery cutoff), all alarms 0. Previously the first publish tick computed `age = now − 0`, jumped straight to SURVIVAL, and pushed DVL 54.0V — above the pack's resting ~53V — which tripped the Quattro's low-battery shutdown and raised a low-voltage alarm on every boot off shore power. If the grace window expires with still no frame, the normal ALERT → RESTRICT → SURVIVAL escalation proceeds from flow start. The virtual battery node's `default_values` is also off, so the service no longer flashes 48V/50% in the seconds before the first publish tick.
- **CVL control**: "Max Charge" slider (40-100%) exposed on VRM dashboard (BMS group), maps **piecewise-linearly** to CVL **on the REC's own SOC scale** ([cvl] `curve` breakpoints in config.ini) and is clipped at 61.96V. Calibrated 2026-08-19 from three measured points: a settled hold at 58.6% ↔ 56.28V, a days-long ~0A hold at 62.3% ↔ 56.65V, and the REC's 100% sync point (raw 0x351 CVL, 62.70V). The two mid points both lie exactly on 0.100 V/% — the NMC mid-plateau is flatter than the 62→100% region (0.1605 V/%) — so the map has a knee at 62.3%: 40% → 54.42V, 60% → 56.42V, 62.3% → 56.65V, 100% → 62.70V (clip engages above ~95%; values ≥62.3% are unchanged from the earlier single-slope line). NMC steepens again below ~50%, so low slider values may settle a little high until a hold is measured there; new breakpoints can be appended to the curve as holds settle. The original 54.00→61.96V line sat inside the REC's scale at both ends (its "40%" ≈ 46% real, its "60%" held ≈ 62.5% real — the storage-mode mismatch the slider was built to prevent). The clip is user policy: never push the Quattro above 61.96V; only the EQ boost may ride on top (max 62.40V, still under the REC's 62.7V limit which is `min()`'d in regardless).
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

### CZone Control (`archive/CZoneProxy.json`) — RETIRED

**Replaced by the standalone `dbus-czone/` driver (see above). Kept in the repo
for rollback only — do not deploy it alongside the driver: both drive the same
CZone circuits, and two writers on one bank will fight.**

Reads PGN 127501 from `can0` with a `candump` subprocess and publishes eleven
separate `victron-virtual-switch` devices (instances 225-235), one per circuit;
commands go out as PGN 127502. Circuit names, the circuit count and the
momentary/latching split are all hardcoded in the flow. The driver replaces all
of that with discovery and a single multi-output bank.

### Solar Priority (`archive/SolarPriority.json`) — RETIRED, the algorithm description below still applies to the driver

Automatically powers AC loads from solar instead of shore power when the panels can carry them. Designed for storage mode: the battery is held at a target SOC by the Max Charge slider and the goal is **zero energy cycled through the battery** — battery power is therefore the primary control signal, not PV-vs-load comparison. Charging in solar mode is allowed (surplus charges the pack until DVCC/CVL caps it at the target).

**v3 (2026-08-18)** was rebuilt against live bus measurements on the boat; v2's Voc→power linear model and self-tuning gain were dropped (panel Voc is nearly flat across the daylight irradiance range — ~72V idle in both dim and bright sun — so it carries almost no capacity signal, and the gain would have conflated morning sun-angle with capacity error). **v3.4 (2026-08-19)** added the adaptive panel model, the night/dawn probe gates and the charger-quiet probe gate after the first overnight run exposed hourly stale-Voc probes, a dawn probe storm, and the probes resetting the Prefer-Renewable-Energy initial charge cycle. **v3.5 (2026-08-19)** made probes **boost-assisted**: in the steady state the pack sits at the target voltage, which is also the MPPT ceiling, so a bare probe drops shore with ~0V of margin and the chargers ramp glacially — measured 2026-08-20: 25W at 60s into a probe vs 600W in ~60s under a 0.30V boost, in identical full sun. Each probe requests the dbus-recbms solar boost at entry and releases it on exit (probe success, failure, or any return to shore). The handover leaves the pack slightly above the target — the boost charged it — so the MPPTs throttle until the surplus drains into the loads; that drain phase and a 90-second settle window after solar entry are exempt from the deficit/surge exits (SOC and fault exits stay live). **v3.6 (2026-08-20)** made the solar→shore deficit exit judge the **90s rolling mean** of battery power instead of the instantaneous sign: hovering at the target the MPPTs regulate voltage with a margin-limited response and throttle-hunt around the demand (measured: 1W from a 350W-capable array at 0.01V of margin), so instantaneous discharge readings flip sign even when the array carries the load on average. The 400W surge exit stays instantaneous.

**v4.0 (2026-08-20)** adds two behaviors and finishes the averaging pass:

- **Harvest-and-burn**: when capacity < load (shaded array, haze, evenings), solar is no longer wasted at the ceiling. On shore the solar lead gives the MPPTs headroom to fill the 0.15V band below the target; when the band is full (battV within `HARVEST_ARM_V` = 0.02V of target, charger quiet, daylight) the flow enters the existing burn-down state and feeds the stored solar into the loads, then lets the sun refill. Harvest re-arms only after battV dipped `REFILL_RESET_V` = 0.10V under target and climbed back **without material charger power** — during a solar climb PV roughly matches the battery charge power, while a Quattro recharge has near-zero PV — so shore energy is never round-tripped through the inverter and nothing can arm at night. Each burn cycles ~1.2 kWh (~10–15 full-cycle-equivalents/year: negligible NMC wear). Probing (direct carry, no round-trip loss) takes precedence whenever capacity clears the bar.
- **Suspend**: a heater-class load (≥ `SUSPEND_LOAD_W` = 1kW for 3s) reconnects shore immediately from solar or burn-down — no cooldown, no backoff — and the flow resumes the prior state ~10s after the load returns to its pre-suspend 60s average (+200W; the baseline is snapshotted when the big load first appears). A suspend older than 20 minutes becomes a normal shore transition.
- The probe's big-load kill now judges the 60s load average (instantaneous 4s flickers killed two probes on 2026-08-20; only a step >1.5× the estimate counts directly), the burn-down calm handover uses the 90s battery-power mean (gated 45s after entry), and the probe-assist boost is kept through the solar handover so the MPPTs stay margined while the drain begins.

**v4.2 (2026-08-20)** — **lead verification hooks**: subscribes to the driver's `/RecBms/LeadFault` and `/RecBms/SolarLead` (v1.4.0); a fault is surfaced with `node.error()` and harvest-and-burn is disabled while the lead in force is 0.

**v4.1 (2026-08-20)** — **ceiling-stall burn**. Observed at 12:30 PT in full sun: solar mode refills the band, the pack parks *at* the target, the MPPTs sit at zero margin and carry 2–37W of a 300W load, the 90s deficit mean trips, shore comes back, backoff, boosted probe, solar, repeat — a 9-minute loop (six boosts in 50 minutes) that never re-armed harvest, because the refill tracker needs battV to reach target − 0.10V and the deficit exit fires at ~target − 0.03V. A solar-mode deficit with the band still full (battV within `STALL_BURN_MIN_V` = 0.05V of the target, daylight, shore present, SOC ≥ floor) now rolls into **burn-down instead of a shore exit**, with no backoff escalation: the loads open margin under the chargers, the averaged calm handover returns to solar, and burn-down's bottom exit (target − 0.15V) still falls back to probe/shore if the sun really has gone. The genuine sunset/cloud deficit — battV already sagging under the target — keeps its shore exit.

**Capacity is measured, not modelled**: `/MppOperationMode == 2` means a charger's tracker is active (unthrottled), so its `/Yield/Power` *is* its current capacity. Captured per charger (light 0.3 blend when refreshed within 60s), fully trusted for 15 min, faded linearly to zero by 90 min. The chargers bulk-charge unthrottled for hours most mornings (`TimeInBulk` 7–14 h/day observed), and a failed probe is itself a measurement, so the estimate is usually backed by evidence.

**Adaptive panel model (v3.4)**: fills the throttled-time blind spot. While a charger is throttled (battery at the CVL ceiling) but passing even a trickle — the DC loads provide ~45W continuously under Prefer Renewable sustain — the panel voltage's distance below its open-circuit voltage reveals how much current the panel *could* source. Single-diode approximation per MPPT: `I = Isc·(1 − exp((V − Voc)/a))` with effective string thermal voltage `a = 3.5V`, so `Isc = (P/V)/ratio` and `capacity ≈ kff·Voc·Isc`. The Voc reference is a 10-min EMA from idle moments (tracks temperature drift); `kff` (fill-factor-ish, default 0.78) is calibrated whenever a charger runs unthrottled — **every probe adapts the model**. Near Voc the exponential is razor thin, so shallow-draw estimates (`ratio < 0.3`, or clamped at draw×20 / ratio floor 0.02) count only as **lower bounds** — good enough to justify a probe, never to skip one; a deep-draw estimate is trusted, and a trusted-dim reading on either array (scaled by array share 0.35/0.65) suppresses blind exploratory probes — both arrays see the same sky. Estimates live 5 min; nothing is persisted (Voc re-learns in minutes, kff in one calibration).

Estimate = `min(P_rated, max(live PV, Σ per-charger max(faded capture, model)))`, gated to zero at night: max panel Voc < 55V **or both chargers report `MppOperationMode` 0** (the Voc reading is last-known-good on a change-only bus, so a sleeping charger's daytime voltage would otherwise stick all night — the cause of v3.3's hourly overnight exploratory probes). An **exploratory probe** (Voc ≥ 65V, no capture, no model signal) is additionally blocked while Voc is still climbing (> 1V per 10-min window) — during the dawn ramp the panels reach Voc long before they can deliver, and v3.3's Voc-rise backoff release turned that into a probe storm every ~5 min.

**Change-only publication (measured)**: Venus services signal values only when they change — `/Dc/Battery/Power`, `/Soc`, `/MppOperationMode`, `/ActiveInput` can be silent for minutes to hours (battery power sat at −17W with zero signals in 60s). Silence means *unchanged*, not *stale*: all inputs are last-known-good, and **liveness is tracked per service** via paths that provably jitter ~1/s — system: `/Ac/Consumption/L1/Power` (57 signals/min measured), vebus: `/Ac/Out/L1/P`, each MPPT: its `/Yield/Power` while producing. A dead system or vebus heartbeat (>20s silent) forces shore; a silent MPPT just stops capacity captures (correct at night, safe on failure — battery power remains the safety signal).

**Mechanism**: Controls the Quattro's `IgnoreAcIn1` D-Bus path. When solar can carry the load, AC input is ignored → Quattro inverts from battery → battery dips below CVL → MPPTs wake and ramp → solar powers the loads. No backfeed risk (AC input is disconnected, not reversed). While enabled, the flow owns this path and re-asserts the desired value every 30s (survives vebus restarts and external writes); while disabled it never writes after the initial return to shore, so manual control stays possible. **Verified live 2026-08-18**: write accepted, `ActiveInput` → 240 and `/State` → 9 (inverting) within 2s; reconnect equally fast; measured capacity during the test: 390W + 730W ≈ 1120W (late-afternoon, partly cloudy).

**Data inputs** (each stored as `{value, timestamp}`):
- `com.victronenergy.solarcharger/278` + `/279` (MPPT 100/20 and 150/35): `/Pv/V` (daylight gate), `/Yield/Power` (capacity + heartbeat), `/MppOperationMode` (throttle state)
- `com.victronenergy.system`: `/Ac/Consumption/L1/Power` (instant + 60s time-based rolling average; system heartbeat), `/Dc/Battery/Soc`, `/Dc/Battery/Power` (the truth signal; also sampled once per tick into a 90s rolling mean), `/Dc/Battery/Voltage` (against the target: surplus, burn-down exit, harvest band, ceiling stall)
- `com.victronenergy.vebus/276`: `/Ac/ActiveIn/ActiveInput` (command feedback: detects an ineffective `IgnoreAcIn1` write and an absent shore connection), `/Ac/Out/L1/P` (vebus heartbeat)
- `com.victronenergy.battery/200` (dbus-recbms ≥ 1.4.0, optional): `/RecBms/LeadFault` (text; non-empty → `node.error()` in the "Notify Lead Fault" node) and `/RecBms/SolarLead` (lead in force; 0 disables harvest-and-burn — no band to harvest). Absent on an older driver the flow behaves as v4.1.
- `com.victronenergy.battery/200` (dbus-recbms ≥ 1.3.0): `/RecBms/TargetChargeVoltage` (the true charge target — **not** `/Info/MaxChargeVoltage`, which since v1.3.0 is the solar-lead-lowered Quattro command), and the solar-boost feedback trio `/RecBms/SolarBoost/Active`, `/SolarBoost/WindowOpen` (only sample capacity once the array has settled), `/SolarBoost/EffectiveChargeVoltage` (compare `battV` against this, so a boost the flow asked for is never read as a surplus)

**Control outputs**: `com.victronenergy.vebus/276` `/Ac/Control/IgnoreAcIn1` — 0 = accept shore, 1 = ignore shore. Shore is assumed on AC input 1 (see the flow's SETUP comment for AC-in-2 systems). And `com.victronenergy.battery/200` `/RecBms/SolarBoost/Request` — volts of measurement boost, 0 to release; the driver clamps, gates and expires it, so the flow can only ever ask.

**State machine** (`shore` → `probe`/`burndown` → `solar`, plus `suspend` from any inverting state):
- **Shore → Burn-down**: battV > CVL + 0.05V (pack holds surplus — the Max Charge target was lowered, or an external over-charge) AND the charger is quiet (battP ≤ +100W) AND not latched AND SOC ≥ 40% AND shore present, sustained 30s, gated by cooldown only (no backoff — this is not solar-dependent) → disconnect shore and feed the loads from the surplus, **no sun required**. The MPPTs wake on their own once battV crosses under the CVL. Exits: battery no longer discharging for 15s → hand over to `solar` seamlessly; battV ≤ CVL − 0.15V sustained 30s (well past the ~0.08V/500W load sag measured, so the exit cannot flap) → `probe` if the estimate clears the bar (no relay cycling), else reconnect shore. **Loop protection** (added after observing a burn→recharge→burn cycle live on 2026-08-18: the Quattro overshoots the CVL while re-absorbing after shore returns — 56.78V at +471W vs CVL 56.65V — which kept re-arming the trigger; SOC moved a net −0.4% over ~4h while shore re-imported every burn): the charger-quiet entry condition, plus a **one-shot latch** — a completed burn-down blocks further burns until the CVL changes (slider moved, >0.02V) or battV exceeds CVL + 0.25V (genuine overcharge, above the measured overshoot band). Inputs: `/Dc/Battery/Voltage` (system) and `/RecBms/TargetChargeVoltage` (battery/200) — the slider/EQ target; since driver v1.3.0, `/Info/MaxChargeVoltage` is the Quattro's solar-lead-lowered command, not the voltage the pack converges to.
- **Shore → Probe**: estimate ≥ max(100W, avg load × 1.2) (or exploratory) AND SOC ≥ 40% AND shore present AND battV ≤ CVL + 0.05V AND the charger quiet (battP ≤ +100W — disconnecting AC mid-charge resets the Quattro's charge cycle, and with Prefer Renewable Energy that cycle must complete once before sustain activates; v3.3's overnight probes kept resetting it, which is why the feature never went Active), all sustained for 30s (no probing on a sun glint), gated by cooldown + backoff → disconnect shore. The CVL condition encodes MPPT physics: while the pack sits above the DVCC charge voltage, the chargers back off to zero and a probe is guaranteed to fail — observed live 2026-08-18 (battV 56.78V vs CVL 56.65V kept both chargers asleep through a full probe); that situation is handled by burn-down instead. The gate is skipped if either voltage input is absent.
- **Probe (90s ramp)**: the MPPTs take **~40s to wake** after the shore drop (measured — the pack barely sags below CVL) and reach full power by ~60s; since v3.5 the probe requests the solar boost at entry so the ramp is not margin-starved when the pack already sits at the target; PV and battery power are averaged over the final 15s. Aborts early if a load bigger than the estimate appears (3s sustained), or if no charger is tracking by 60s (`MPPTs never woke` — pack still above CVL). At ramp end:
  - **→ Solar**: battery not discharging (> −50W avg) — solar covers *everything* including DC loads and inverter losses
  - **→ Shore**: battery discharging → the ramp's capacity captures recorded the real capacity (MPPTs were provably unthrottled), exponential backoff
  - **→ Shore (FAULT)**: feedback still shows AC-in 1 → `IgnoreAcIn1` ineffective → 1h lockout
- **Solar → Shore** (all safety exits bypass cooldown):
  - Big load: battery discharge > 400W sustained 3s → immediate reconnect (uses instant readings, not the average)
  - Deficit: 90s rolling-mean battery discharge > 50W, sustained 15s (clouds, evening); if battV is still within 0.05V of the target in daylight with shore present this is a ceiling stall and rolls into burn-down instead of shore (v4.1) — the mean, not the instantaneous value, because at the ceiling the MPPTs throttle-hunt around the demand (v3.6); suspended while the pack drains boost surplus above the target and for the first 90s after solar entry (v3.5)
  - SOC drift: SOC falls 2% below its value at solar entry, or below 40%
  - Emergency: SOC < 30% from any state → immediate shore + 1h lockout
  - Fault: feedback shows AC re-accepted externally despite re-asserts → shore + 1h lockout
- **Anti-cycling**: 5 min cooldown between transitions; failed probes back off exponentially 5→10→20→40→60 min (capped). Because a failed probe stores the measured capacity *and calibrates the panel model*, the estimate itself blocks re-probing until conditions genuinely improve. Backoff is released early only on capacity evidence (measured capture or model estimate) clearing the bar, a ≥ 30 min stable solar stint, or a re-enable. (v3.3's "Voc rose 3V since the failure" release was removed — it re-fired continuously through the dawn Voc ramp.)
- **Disabled / missing data**: always returns to shore (safe state). A `catch` node forces shore on any decision-engine exception; a safe-start inject writes 0 at startup.

**Persistence** (`solar-priority-state.json`, same writable-dir discovery as the old Virtual BMS): enable state and PV capacity survive Node-RED restarts, redeploys, and reboots — after a marina power cut the flow resumes on its own. Restore runs ~6s after startup and pushes values back to the VRM switches as plain payloads (the virtual switch expands them to `State`/`Dimming`; catch + retry ×5); saves are blocked until restore has run so startup echoes can't clobber the file. Capacity measurements are deliberately not persisted (stale after a restart; re-learned within minutes).

**VRM dashboard** ("Solar" group): "Solar Priority" on/off toggle (type 1), "PV Capacity" slider (100–2500W, step 50W, default 1800W ≈ the array's observed 7-day peak of ~1750W; only a sanity ceiling on the estimate).

**Setup required**: After import, (1) verify the six solar-charger input nodes point at the two MPPTs (pre-filled `solarcharger/278` and `/279`), (2) verify "AC Input Control", "AC Input Feedback" and "AC Out Power" point at the Quattro (pre-filled `vebus/276`), (3) verify the four `victron-input-system` nodes auto-discovered "Venus system" (AC Load, Battery SOC, Battery Power, Battery Voltage), (4) verify the four `battery/200` nodes ("BMS Target CVL" plus the three solar-boost paths) — **deploy `dbus-recbms` ≥ 1.3.0 before this flow**, or `/RecBms/TargetChargeVoltage` will not exist.

### Instance Registry (`archive/InstanceRegistry.json`) — RETIRED 2026-08-21

Pinned every virtual device the flows published to a fixed dbus service name
**and** a fixed VRM/device instance, so redeploys and restarts could never mint
new instances. Before it, instances were assigned dynamically (localsettings
auto-allocation in registration order), which littered the Signal K data model
with orphan paths (four generations of tank paths `tanks.freshWater.81/83/85/89`
+ matching `wasteWater.82/84/86/90` from one sender, dead GPS services
`vi1_uc1479909` / `vi2_uc1548484`).

**Why instances drifted**: the victron palette derives the dbus service name
from the Node-RED node ID (`com.victronenergy.<class>.virtual_<nodeId>`) and
proposes a default instance; localsettings then auto-assigns the next free
number in registration order. Any regenerated node ID (copy/paste re-import,
flow rebuild) or reset settings entry minted a brand-new service + instance —
and Signal K keys its paths off both, so each generation left an orphan.

**Why it could be retired**: no `virtual_*` device is published any more. Every
device on the boat now comes from a standalone driver, and each driver pins the
numbers it owns through `/Settings/Devices/<id>/ClassAndVrmInstance` at startup,
reconverging if localsettings granted something else. The settings ids
deliberately have **no** `virtual_` prefix, so the palette's auto-cleanup can
never remove them — the failure mode the registry was built to police cannot
occur without a registry to police it.

The allocation table those rules produced is now maintained under
**Device Instance Allocation** above.

## Key Technical Notes

- **11-bit visibility on `can0` is partial, not all-or-nothing.** The REC-BMS
  frames at `0x351`-`0x404` do **not** reach userspace on the Cerbo's VE.Can
  port (sun4i_can), which is why the YDNB-07 repackages them as 29-bit
  `0x18FF0NNN`. The Greenline drives' CANopen TPDOs at `0x18x`/`0x28x`/`0x38x`/
  `0x48x` **do** arrive, at 10 Hz, and `dbus-edrive` reads them directly with an
  SFF receive filter — an earlier blanket "11-bit frames are filtered" note here
  was wrong (`GreenlineFindings.md`, "can0 visibility notes"). Assume nothing
  about a new 11-bit id: check with `candump` before designing around it.
- Signal-K SOC values are ratios (0.0-1.0), not percentages. Multiply by 100 for Venus OS.
- The YDNB-07 programming language requires `match()` filter bodies on separate lines (no single-line `{ }` blocks).
- The YDNB-07 supports up to 20 `match()` filters. Currently 16 are used for BMS CAN IDs.
- `FW_CAN1_TO_CAN2=ON` only forwards frames that don't match any `match()` filter. Matched frames are handled exclusively by their filter subprogram.
