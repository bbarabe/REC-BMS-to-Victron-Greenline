# Greenline 6GK — CAN bus reference

Decode of the hybrid drive traffic on `can0`: the CANopen TPDOs from the two
electric drives, the J1939-style HCU frames, the NMEA 2000 diesel side, and the
OP BOX PLC tag dictionary. Scalings below are cross-checked against the MFD
hybrid page and the HMI project's own tag bindings.

## `0x48x` float 1 is motor torque (N·m), not power

Float 1 of `0x48A/0x48B` is **motor torque in N·m, signed** (+ motoring,
− generating). It is not power. Mechanical power is `T·ω`; the MFD's
"MOTOR POWER" tracks the electrical product `V × I_dc`.

**Motor power is not transmitted directly** — the display computes it. Derive it
from `T·ω` or from `V × I_dc`.

`torque N·m ÷ torque %` (the HCU percent-torque field below) gives a **rated
torque of ≈155 N·m**, consistent to ~1 % across runs; regressing N·m against
torque % over a 422–524 rpm sweep gives r = 0.9913.

Note for anyone re-checking near 1000 rpm: at ~1000 rpm `ω/100 ≈ 1.05`, so a
torque-in-N·m reading and a power-in-hundreds-of-watts reading differ by only
~5 % and a single operating point cannot tell them apart. At 430–491 rpm they
differ by more than 2×. There is a residual ~6 % discrepancy in the high-rpm
numbers that is still open (see Unresolved).

### `GreenlineEDriveFlow.json`

The flow publishes both drives as Victron `motordrive` devices. It takes DC
current from the *measured* `61452` w2 rather than deriving it from power, so
the DC triplet is internally consistent (`P = V × I`) and matches the MFD.

A 2 s tick drives `Connected → 0` after 6 s of CAN silence, otherwise the values
freeze indefinitely — the drives keep publishing a stale voltage long after
shutdown. The capture filter covers PGNs 61451/61452/65363, and a third output
carries torque, throttle and phase current, which have no Victron D-Bus path.

---

## Electric motor data — CANopen TPDOs

Each frame = two IEEE-754 little-endian floats. `A` = port (node 0x0A), `B` = starboard (0x0B).

| CAN ID | Bytes 0–3 (float 1) | Bytes 4–7 (float 2) |
|---|---|---|
| `0x18A/18B` | status byte, DLC 1: `00` = stopped, `01` = running | — |
| `0x28A/28B` | **HCU / controller-region temp °C** | **Electric motor temp °C** |
| `0x38A/38B` | **Motor voltage V** | **Motor phase current, RMS A** (display shows peak = ×√2) |
| `0x48A/48B` | **Motor torque, N·m, SIGNED** (+ motoring, − generating) | **Motor RPM** (stays positive in reverse) |
| `0x305` | all zeros | |
| `0x307` | `12 34 56 78 "VIC"` constant — ID/heartbeat | |

### `0x28x` temperatures

The two sensors are told apart by their dynamics under a load step:

- **float 1 = controller/HCU-region**: starboard `18.72 → 22.58 → 22.41` — jumps ~4 °C within seconds
  of load and plateaus (small thermal mass).
- **float 2 = electric motor winding**: starboard `11.03 → 11.65 → 13.70` — slow monotonic rise
  (large thermal mass). **Exact display match**: port f2 `20.39` vs displayed `20 °C`, starboard f2
  `11.10` vs displayed `11 °C`.

float 1's absolute value matches the displayed *HCU MOSFET temp* exactly on an **idle** drive
(19.35 vs 19 °C) but reads ~3 °C above both displayed HCU temps on a **loaded** drive, and the
displayed MOSFET value did not move while f1 climbed 23.1 → 24.3. So f1 is a controller-side sensor
that is **not** the one displayed. Exact display mapping unresolved.

---

## HCU frames — src `0x64` = STARBOARD, `0x65` = PORT

| PGN (hex ID) | Field map |
|---|---|
| 61451 `14F00Bxx` | **byte 1 = motor torque %, offset −125** (J1939 percent-torque convention); byte 0 = 0. **w2 = motor RPM × 8**. w4 = 1100 constant — constant while running, changes between power cycles; unexplained |
| 61452 `14F00Cxx` | **w4 = motor phase current peak × 20** (5595→271 A, 6219→311 A ⇒ 20.6 / 20.0 counts/A ✓); **w2 = motor DC current**: `A = 0.024918 × w2 − 800.41` (r = 0.9994 over −30…−57 A) ⇒ **40.1 counts/A, zero at w2 ≈ 32122** — i.e. essentially 0.025 A/count. w0 also tracks load (r = −0.9993, ~13.8 counts/A, opposite sign) — a second load quantity, identity unresolved |
| 61453 `14F00Dxx` | three slow temps; **port and starboard read nearly identically and barely respond to load** — does not match the displayed HCU temps under either ×1/256 (≈36.5 °C) or ×1/512 (≈18.3 °C) scaling. Unresolved; likely shared/ambient sensors |
| 65243 `14FEDBxx` | slow counters — w2 = 35/37, w0 = 14080 (stbd) / 1280 (port), static |

### Torque %

Byte 1 of `14F00Bxx` minus 125 is the torque percentage the MFD displays
(e.g. byte 153 → 28 %, byte 157 → 32 %). At zero torque w0 = **exactly 32000** (= 125 × 256), which is what fixes the −125 offset.

---

## Throttle position — PGN 65363 `0x18FF53xx`, bytes 1–2

**`throttle_percent = u16le(bytes 1–2) × 0.107759`**  (= raw ÷ **9.280**, zero intercept)

The ratio is exactly 9.280 at every settled plateau, with a zero intercept:

| raw u16 | `AccPedalPos1` % | ratio |
|---|---|---|
| 0 | 0.000 | — |
| 12 | 1.293 | 9.280 |
| 30 | 3.233 | 9.280 |
| 44 | 4.741 | 9.280 |
| 75 | 8.082 | 9.280 |

It is the only field on `can0` that tracks throttle. **The CAN field leads the OP
BOX `AccPedalPos1` tag by ~0.2–0.4 s** in both directions, so the PLC tag is a
filtered copy of this CAN field rather than an independent source.

Byte 0 is the drive index (`00` port / `01` starboard), matching the `0x8D`/`0x8E` sources.

The scale is established over 0–8.1 %; the nominal full scale is unconfirmed, but
extrapolates to raw ≈ 928 at 100 %. The field is 16-bit, so there is no range
problem — byte 2 simply stays 0 below ~27.5 %.

Note: this field is a 16-bit little-endian word at bytes 1–2, not a byte. It has
no proportionality to rpm (raw/rpm wanders 0.02–0.15).

### OP BOX equivalent

```
Application/GVL_ExportHMI/EngineST_HMI[1]/AccPedalPos1/AV   (port)
Application/GVL_ExportHMI/EngineST_HMI[2]/AccPedalPos1/AV   (starboard)
```

`AccPedalPos1` is the J1939 SPN-91 name (Accelerator Pedal Position 1). Use the CAN field if you want
it un-lagged; use the tag if you want the same number the MFD shows.

The TSC1 keep-alives are confirmed **not** throttle: `0x777`/`0x779` are bit-identical in every state;
`0x778` is instance-multiplexed (byte 0 = 0 port / 1 starboard) with byte 1 = `02` idle → `04` engaged —
a discrete engaged flag, identical in forward and reverse, with no proportional content.

---

## Gear — PGN 127493

`09F2058D` (port) / `09F2058E` (stbd), **byte 1** = standard N2K gear enum:

| value | gear |
|---|---|
| `0xFC` | **Forward** |
| `0xFD` | **Neutral** |
| `0xFE` | **Reverse** |

All three values match the display's "THROTTLE STATE" field.
The OP BOX equivalent is `EngineST_HMI[n]/ClutchState/AV`: **2.0 = Neutral, 3.0 = Forward** (both observed).

---

## OP BOX — `PStD1/AV[1..20]`

`AV[11..14]` were previously guessed as motor RPM and controller temp. **They are neither** — they are
the battery time-remaining pair, and only one pair is populated at a time depending on current sign.

Three independent checks agree:

1. **Arithmetic.** `hours + minutes/60`, multiplied by pack current, recovers the expected amp-hours,
   both discharging (715 Ah vs SOC×capacity = 713 Ah) and charging (668–680 Ah vs
   (100−SOC)×capacity = 685 Ah).
2. **The display.** Bottom bar read "TIME TO EMPTY **24h60m**" while `AV[11]=24`, `AV[12]≈60`; later
   "**22h 3m**" while `AV[11]=22`, `AV[12]=3`.
3. **The HMI project bindings** (`rd202.html`): the "TIME TO FULL" label is immediately followed by the
   `AV[13]` and `AV[14]` datalinks, and "TIME TO EMPTY" by `AV[11]` and `AV[12]`.

| idx | quantity | idx | quantity |
|----|----------|----|----------|
| AV[1] | SOC % | AV[11] | **time to empty — hours** (0 while charging) |
| AV[2] | pack voltage V | AV[12] | **time to empty — minutes** |
| AV[3] | current A (− = discharge) | AV[13] | **time to full — hours** (0 while discharging) |
| AV[4] | max cell V | AV[14] | **time to full — minutes** |
| AV[5] | min cell V | AV[15] | CVL V (62.7) |
| AV[6] | temperature °C | AV[16] | DVL V (50.6) |
| AV[7] | power kW (\|V·I\|) | AV[17] | CCL A (480) |
| AV[8] | SOH % | AV[18] | DCL A (480) |
| AV[9] | ~1398 — nominal/usable Ah | AV[19] | 0 — unknown flag |
| AV[10] | 0 — unknown flag | AV[20] | **SOC % again** — drives the battery bargraph (0–100 widget) |

The `AV[1..8]` block maps 1:1 onto the MFD's centre "BMS" panel. A brute-force correlation of every
CAN field against every moving tag independently rediscovered the battery mappings from raw data —
`AV[3]` ← `0x356` byte 2 ×0.1 A, `AV[4]`/`AV[5]` ← `0x373` cell mV, `AV[7]` ← V·I — which is what
validated the whole correlation pipeline before it was trusted on the motor side.

### The rest of the OP BOX tag dictionary

From the HMI project's own `datalinks`:

```
EngineST_HMI[1..2]/AccPedalPos1/AV        throttle position % (= PGN 65363 raw / 9.280, lagged ~0.3 s)
EngineST_HMI[1..2]/ClutchState/AV         gear: 2.0 = NEUTRAL, 3.0 = FORWARD (observed)
EngineST_HMI[1..2]/EngineSpeed/AV         DIESEL rpm -- reads 0 with the e-motor running, NOT motor rpm
EngineST_HMI[1..2]/EnginePower/AV         motor power kW
EngineST_HMI[1..2]/EngineMode/AV          1.0 = MOTOR
EngineST_HMI[1..2]/BattCurrent/AV         motor DC current A = the MFD's "MOTOR CURRENT"
EngineST_HMI[1..2]/EngineCoolTemp/AV      coolant temp (0.0 at dock)
EngineST_HMI[1..2]/EngineRevolutions/AV   hours / revolutions
PEng1/AV[1]/AV (21.0)   PNav1/AV[1..2]/AV   Engine_ErrorCode_HMI[1]
VehicleMode_HMI / _Port_HMI / _Stbd_HMI    NrOfEngines_HMI (2)
FullCapacity_HMI (220)  MeasureUnitChoice_HMI  DTE_HMI/AV  MainAlarm_HMI
```

**How to get the project files** (read-only, this is what a browser does):

```
GET /cgi/getProperties?n=1&it1=projectRelativePath   ->  workspace/rd202
GET /rd202/web/web/pages/...                          ->  page JS with "datalinks" bindings
```

Note the served path is `/rd202/...`, **not** `/workspace/rd202/...` — the latter 404s. The
`_webmain.html` HTML5 client is not installed, so the pages must be fetched directly (or saved from a
browser already viewing the HMI, which is how `rd202.html` was obtained).

---

## What's on the bus

| Source | Protocol | Role |
|---|---|---|
| 11-bit IDs `x8A`/`x8B` | CANopen-style TPDOs | **Electric drives** — node 0x0A = PORT, 0x0B = STARBOARD, ~10 Hz |
| 29-bit src `0x64`/`0x65` | J1939-style | HCU — **0x64 = STARBOARD, 0x65 = PORT** (proven, single-motor runs) |
| 29-bit src `0x8D`/`0x8E` | NMEA2000 (mfg code 172) | Engine gateway — diesel instances 0/1; also carries 127493 gear and 65363 speed demand |
| 29-bit src `0x77/0x78/0x79` | J1939 TSC1 (PGN 0) → dest 0x07 | Keep-alives + engaged flag; no proportional throttle |
| src `0x51`–`0x81` | NMEA2000 65283 (0xFF03) | REC-BMS repackaged by the YDNB-07 (`0x351`→src `0x51` …) |
| src `0x10` | NMEA2000 | 12 V house/start battery monitor |
| Others (0x02–0x23) | NMEA2000 | Nav gear (Simrad/Navico heading, GPS, AIS, autopilot, rudder…) |

232 distinct CAN IDs at ~720 frames/s. Both the 11-bit CANopen range and the repackaged BMS frames
reach userspace on `can0` (see the note on `can0` visibility below).

## Diesel side — standard NMEA2000 from 0x8D/0x8E

- PGN 127488: **diesel RPM ×0.25**, instance 0 = port, 1 = stbd
- PGN 127489: coolant temp, starter battery V, **engine hours**
- PGN 127493 transmission (gear, above), 65363 proprietary (speed demand, above)

---

## `can0` visibility notes

1. **The 11-bit CANopen e-drive frames are present on `can0`.** `0x18A/B`,
   `0x28A/B`, `0x38A/B`, `0x48A/B`, `0x305`, `0x307` all stream at 10 Hz. The
   VE.Can port does **not** blanket-filter 11-bit frames the way the main
   specification's "Key Technical Notes" implies — at least this SFF range
   reaches userspace.
2. **The REC-BMS main bank is on `can0`** as PGN **65283 (0xFF03)**, apparent src
   `0x51`–`0x81` (the YDNB-07 repackaging of `0x351`–`0x381`). `dbus-recbms`
   decodes it.
3. Torque sign: floats in `0x48x` f1 go negative while generating.
4. Side assignment: CANopen node `0x0A` = port, `0x0B` = starboard.

---

## Unresolved

1. **`65363` full-scale calibration** — the throttle scale (÷9.280) is exact over 0–8.1 %, but full travel
   was never exercised; 100 % extrapolates to raw ≈ 928, unverified.
2. **`61452` w0** — tracks load at ~13.8 counts/A with sign opposite to w2; a second load quantity
   (demand vs actual?), identity unresolved.
3. **`61453`** — does not match the displayed HCU temps under any obvious scaling.
4. **`0x28x` float 1** — real controller-side sensor, but not the one the MFD shows.
5. **`61451` w4 = 1100** — constant while running, differs between power cycles.
   Unexplained.
6. **`AV[10]`, `AV[19]`** — still constant 0.
7. **High-rpm re-check of the torque scale** to resolve the residual ~6 % in the
   high-rpm numbers. The torque work was done at 422–524 rpm.
8. **Reverse-direction torque sign** — reverse shows positive torque and positive
   rpm, so direction comes only from 127493.

## Implementation notes

`socket.CAN_EFF_FLAG` is negative on armv7l — mask `& 0xFFFFFFFF` before
`struct.pack`, or use the literal `0x80000000` when testing received IDs.
