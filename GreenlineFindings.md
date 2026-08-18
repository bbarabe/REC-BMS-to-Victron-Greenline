# Greenline 6GK — can0 reverse-engineering findings

## Live-run decode 2026-08-17 (dock, in gear on lines) — the big session

Five synchronised captures (raw `can0` + OP BOX tag poll on one clock, 8 Hz tags / ~720 CAN
frames per second), with the owner operating one drive at a time, plus **two photographs of the
MFD hybrid page** taken during the runs as ground truth, plus **the HMI project's own tag
bindings** (saved page `rd202.html`). Between them these resolve every major open item — and
overturn one previously "verified" decode.

| run | state | what it pinned |
|---|---|---|
| `base0` | both drives off, pack charging +6.2 A | baseline; `AV[13]/[14]` non-zero, `AV[11]/[12]` zero |
| `runA`/`runB` | **STBD only**, reverse, 430 rpm | side assignment; `AV[11]/[12]` non-zero, `AV[13]/[14]` zero |
| `runC` | **PORT only**, forward, 430 rpm, throttle 0 % | mirror image → side assignment proven |
| `runD` | **PORT** blipped to 491 rpm, throttle 5 % | torque scale, shutdown transient |
| `runE` | **PORT** throttle sweep 0→8.1 %, 422–524 rpm, `AccPedalPos1` polled alongside CAN | **throttle field**, DC-current scale, rated torque |

Running **one motor at a time** is what made this decisive: every port/starboard ambiguity
collapses when the other drive is at zero.

---

## CORRECTION — `0x48x` float 1 is motor TORQUE in N·m, not power ×100 W

The earlier "Motor power, ×100 W" decode is **wrong**. Direct comparison against the MFD:

| capture | `0x48A` f1 | rpm | T·ω | display "MOTOR POWER" | f1 as ×100 W |
|---|---|---|---|---|---|
| runC | 44.79 | 430 | **2.017 kW** | **2.0 kW** ✓ | 4.48 kW ✗ (2.2× high) |
| runD | 49.79 | 491 | **2.56 kW** | **2.6 kW** ✓ | 4.98 kW ✗ (1.9× high) |

Cross-checked independently against the HCU torque-percent field (below): torque N·m ÷ torque %
gives a **rated torque of 155.3 / 156.9 / 157.4 N·m** across three runs — consistent to ~1 %,
which a wrong interpretation would not produce. The runE sweep (422–524 rpm, torque 22–37 %) confirms
it: regressing N·m against torque % gives r = 0.9913 and per-bucket ratios of 153–158, so **rated torque
≈ 155 N·m**.

**Why July looked right:** at 1004 rpm, ω/100 = 1.051, so torque-in-N·m and power-in-hundreds-of-watts
differ by only ~5 % — inside the noise of reading a display. July's single operating point could not
discriminate. At 430–491 rpm they differ by >2×, and torque wins outright. The July residual
(f1·ω = 8.15 kW vs displayed 7.6–7.8 kW) is a real ~6 % discrepancy worth re-checking at high rpm,
but it does not rescue the ×100 W reading.

**Motor power is therefore not transmitted directly** — the display computes it. Use `T·ω` (mechanical)
or `V × I_dc` (electrical); the MFD's "MOTOR POWER" tracks the electrical product.

### Consequence: `GreenlineEDriveFlow.json` — corrected 2026-08-17

The flow had carried the old decode (`s.power = f1 * 100`), publishing `Dc/0/Power` ~2.2× high at
430 rpm (4478 W vs ~2017 W) and a `Dc/0/Current` derived from it. The error scales as `100/ω`, so it
nearly vanished near 1000 rpm — which is why it went unnoticed since July.

**Now fixed in the repo** (not yet deployed). The rewrite also stops deriving current from power and
instead uses the *measured* DC current from `61452` w2, so the DC triplet is internally consistent
(`P = V × I`) and matches the MFD.

Replaying the real `runE` capture through the new parse function reproduces the OP BOX's own numbers:

| quantity | flow output | PLC tag |
|---|---|---|
| peak motor current | 56.48 A | 56.6 A |
| peak motor power | 3148 W | 3.11 kW |
| peak throttle | 8.08 % | 8.082 % |
| peak torque | 36 % | 36 % |
| direction fwd / rev / neutral | 2 / 1 / 0 | FWD / REV / NEUTRAL |
| both drives at rest | 0.07 A, 4 W | ~0 |

**Deployed and verified 2026-08-17.** The owner redeployed and confirmed the readings now match the
Greenline app. Independently checked on D-Bus afterwards: with the drives powered down both devices
report `Connected = 0` (the staleness tick working as intended) and `Dc/0/Current` 0.07 A /
`Dc/0/Power` 2 W at standstill, against −0.8 A / −19 W from the old decode at the same standstill.

Other changes: a 2 s tick inject drives `Connected → 0` after 6 s of CAN silence (previously the
values froze forever — the drives were still publishing a stale 22.7 V hours after shutdown); the
candump filter gained PGNs 61451/61452/65363; and a third output carries torque, throttle and phase
current, which have no Victron dbus path.

---

## Electric motor data — CANopen TPDOs

Each frame = two IEEE-754 little-endian floats. `A` = port (node 0x0A), `B` = starboard (0x0B).

| CAN ID | Bytes 0–3 (float 1) | Bytes 4–7 (float 2) |
|---|---|---|
| `0x18A/18B` | status byte, DLC 1: `00` = stopped, `01` = running (confirmed both ways this session) | — |
| `0x28A/28B` | **HCU / controller-region temp °C** | **Electric motor temp °C** |
| `0x38A/38B` | **Motor voltage V** | **Motor phase current, RMS A** (display shows peak = ×√2) |
| `0x48A/48B` | **Motor torque, N·m, SIGNED** (+ motoring, − generating) — *see correction above* | **Motor RPM** (stays positive in reverse) |
| `0x305` | all zeros | |
| `0x307` | `12 34 56 78 "VIC"` constant — ID/heartbeat | |

### `0x28x` temperatures — now separated by their dynamics

The load step separates the two sensors cleanly (idle → loaded → still loaded):

- **float 1 = controller/HCU-region**: starboard `18.72 → 22.58 → 22.41` — jumps ~4 °C within seconds
  of load and plateaus (small thermal mass).
- **float 2 = electric motor winding**: starboard `11.03 → 11.65 → 13.70` — slow monotonic rise
  (large thermal mass). **Exact display match**: port f2 `20.39` vs displayed `20 °C`, starboard f2
  `11.10` vs displayed `11 °C`.

Port (never ran in runA/runB) stayed flat throughout — confirming the response is load-driven, not ambient.

float 1's absolute value matches the displayed *HCU MOSFET temp* exactly on an **idle** drive
(19.35 vs 19 °C) but reads ~3 °C above both displayed HCU temps on a **loaded** drive, and the
displayed MOSFET value did not move while f1 climbed 23.1 → 24.3. So f1 is a controller-side sensor
that is **not** the one displayed. Exact display mapping unresolved.

---

## HCU frames — src `0x64` = STARBOARD, `0x65` = PORT

**Side assignment is now proven, not inferred.** In runC (port only) `14F00B65` showed w2 = 3437–3443
and w0 ≠ idle while `14F00B64` sat at exactly w0 = 32000 / w2 = 0; runA/runB showed the exact mirror.

| PGN (hex ID) | Field map |
|---|---|
| 61451 `14F00Bxx` | **byte 1 = motor torque %, offset −125** (J1939 percent-torque convention); byte 0 = 0. **w2 = motor RPM × 8**. w4 = 1100 constant (1080 in July) — constant within a session, changes between sessions; unexplained |
| 61452 `14F00Cxx` | **w4 = motor phase current peak × 20** (5595→271 A, 6219→311 A ⇒ 20.6 / 20.0 counts/A ✓); **w2 = motor DC current**: `A = 0.024918 × w2 − 800.41` (r = 0.9994 over −30…−57 A) ⇒ **40.1 counts/A, zero at w2 ≈ 32122** — i.e. essentially 0.025 A/count. w0 also tracks load (r = −0.9993, ~13.8 counts/A, opposite sign) — a second load quantity, identity unresolved |
| 61453 `14F00Dxx` | three slow temps; **port and starboard read nearly identically and barely respond to load** — does not match the displayed HCU temps under either ×1/256 (≈36.5 °C) or ×1/512 (≈18.3 °C) scaling. Unresolved; likely shared/ambient sensors |
| 65243 `14FEDBxx` | slow counters — w2 = 35/37, w0 = 14080 (stbd) / 1280 (port), static this session |

### Torque % — verified twice against the display

| run | `14F00Bxx` byte 1 | −125 ⇒ torque % | display "MOTOR TORQUE" |
|---|---|---|---|
| runC (port) | 153–154 | 28–29 % | **28 %** ✓ |
| runD (port) | 157 | 32 % | **32 %** ✓ |
| runA (stbd) | 150 | 25 % | — |

At zero torque w0 = **exactly 32000** (= 125 × 256), which is what fixes the −125 offset.

---

## Throttle position — SOLVED: PGN 65363 `0x18FF53xx`, bytes 1–2

**`throttle_percent = u16le(bytes 1–2) × 0.107759`**  (= raw ÷ **9.280**, zero intercept)

Measured in `runE` — a slow throttle sweep on the port drive with `AccPedalPos1` polled at 8 Hz on the
same clock as the CAN capture. Restricting to *settled plateaus* (both the tag and the CAN field
unchanged for ≥1 s), the ratio is **exactly 9.280 at every plateau**, with a fitted intercept of 0.0000:

| raw u16 | `AccPedalPos1` % | ratio |
|---|---|---|
| 0 | 0.000 | — |
| 12 | 1.293 | 9.280 |
| 30 | 3.233 | 9.280 |
| 44 | 4.741 | 9.280 |
| 75 | 8.082 | 9.280 |

Overall Pearson r = **+0.9908** across all 282 polls (degraded only by the lag below), and it is the
**only** field on can0 correlating with throttle above r ≥ 0.95.

**The CAN field leads the OP BOX tag by ~0.2–0.4 s** — at t = 8.6 s the raw was already 27 while
`AccPedalPos1` still read 0.0, and on the way down the raw dropped first. So the PLC's tag is a
*filtered copy* of this CAN field, not an independent source. That direction of causality is what
identifies the CAN field as the origin.

Byte 0 is the drive index (`00` port / `01` starboard), matching the `0x8D`/`0x8E` sources.

Only 0–8.1 % was exercised, so the **nominal full-scale is unconfirmed**: extrapolating, 100 % would be
raw ≈ 928. The field is 16-bit, so there is no range problem — byte 2 simply stays 0 below ~27.5 %.

> **Correction to the first pass of this session.** An earlier three-state scan flagged this exact field
> and then wrongly dismissed it, on the argument that 9.8 counts/% would overflow a *byte* past ~26 %.
> That reasoning was invalid — the field is a 16-bit little-endian word at bytes 1–2, not a byte. The
> accompanying "commanded rpm ÷ 10" hypothesis (49 → 490 rpm) was a coincidence of the single operating
> point available at the time and is **withdrawn**; the sweep shows no proportionality to rpm
> (raw/rpm wanders 0.02–0.15) while throttle is exact.

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

## Gear — PGN 127493, neutral value confirmed

`09F2058D` (port) / `09F2058E` (stbd), **byte 1** = standard N2K gear enum:

| value | gear | evidence |
|---|---|---|
| `0xFC` | **Forward** | runC port, display "THROTTLE STATE **FWD**" ✓ |
| `0xFD` | **Neutral** | base0 both, runC starboard, display "THROTTLE STATE **NEUTRAL**" ✓ |
| `0xFE` | **Reverse** | runA/runB starboard ✓ |

Neutral was the last untested value — **now confirmed against the display**, closing that open item.
The OP BOX equivalent is `EngineST_HMI[n]/ClutchState/AV`: **2.0 = Neutral, 3.0 = Forward** (both observed).

---

## OP BOX — `PStD1/AV[1..20]`, corrected

`AV[11..14]` were previously guessed as motor RPM and controller temp. **They are neither** — they are
the battery time-remaining pair, and only one pair is populated at a time depending on current sign.

Three independent confirmations:

1. **Arithmetic.** `hours + minutes/60`, multiplied by pack current, recovers the expected amp-hours:
   discharging (runA) → 715 Ah vs SOC×capacity = **713 Ah**; charging (base0) → 668–680 Ah vs
   (100−SOC)×capacity = **685 Ah**.
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

Recovered from the HMI project's own `datalinks` rather than by guessing names:

```
EngineST_HMI[1..2]/AccPedalPos1/AV        throttle position % (= PGN 65363 raw / 9.280, lagged ~0.3 s)
EngineST_HMI[1..2]/ClutchState/AV         gear: 2.0 = NEUTRAL, 3.0 = FORWARD (observed)
EngineST_HMI[1..2]/EngineSpeed/AV         DIESEL rpm -- reads 0 with the e-motor running, NOT motor rpm
EngineST_HMI[1..2]/EnginePower/AV         motor power kW (1.67 -> 3.11 over the runE sweep)
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
reach userspace on `can0` (see the 2026-08-17 re-baseline note below).

## Diesel side — standard NMEA2000 from 0x8D/0x8E

- PGN 127488: **diesel RPM ×0.25**, instance 0 = port, 1 = stbd
- PGN 127489: coolant temp, starter battery V, **engine hours** — now 236 h port / 233 h stbd
  (208.1/209.2 h in July)
- PGN 127493 transmission (gear, above), 65363 proprietary (speed demand, above)

---

## Re-baseline 2026-08-17 (motors idle at dock) — two earlier corrections

1. **The 11-bit CANopen e-drive frames ARE present on `can0`.** `0x18A/B`, `0x28A/B`, `0x38A/B`,
   `0x48A/B`, `0x305`, `0x307` all stream at 10 Hz. The VE.Can port does **not** blanket-filter 11-bit
   frames the way the spec's "Key Technical Notes" implies — at least this SFF range reaches userspace.
2. **The REC-BMS main bank is on `can0`** as PGN **65283 (0xFF03)**, src `0x51`–`0x81` (repackaged
   `0x351`–`0x381` via YDNB). The July note "48 V propulsion BMS not seen on can0" is resolved — that
   bank is the REC-BMS, and `dbus-recbms` decodes it.

## Confirmed by the earlier charging session

- Torque sign: negative floats in `0x48x` f1 while generating ✓
- Side assignment: CANopen 0x0A = port, 0x0B = stbd ✓ (re-confirmed this session)

---

## Still open

1. **`65363` full-scale calibration** — the throttle scale (÷9.280) is exact over 0–8.1 %, but full travel
   was never exercised; 100 % extrapolates to raw ≈ 928, unverified.
2. **`61452` w0** — tracks load at ~13.8 counts/A with sign opposite to w2; a second load quantity
   (demand vs actual?), identity unresolved.
3. **`61453`** — does not match the displayed HCU temps under any obvious scaling.
4. **`0x28x` float 1** — real controller-side sensor, but not the one the MFD shows.
5. **`61451` w4 = 1100** — constant within a session, 1080 in July. Unexplained.
6. **`AV[10]`, `AV[19]`** — still constant 0.
7. **High-rpm re-check of the torque scale** to resolve the residual ~6 % on the July numbers. Everything
   this session was 422–524 rpm; the July session was ~1000 rpm.
8. **Reverse-direction torque sign** — all runE data is forward; starboard reverse showed positive torque
   and positive rpm, so direction still comes only from 127493.

### Procedure that worked (reuse it)

Capture raw `can0` and OP BOX tags **on one clock** from the Cerbo, run **one drive at a time**, and
photograph the MFD during the run. The tooling is a self-triggering capture (arm it, then operate the
drive whenever ready — no need to race a timer) plus a brute-force correlator that scores every
byte/u16/f32 field of every CAN ID against every moving tag. Trying to synchronise the owner to a
countdown wasted a run; the trigger approach did not.

Beware a **monotonic-drift confound**: at a steady rpm hold, motor warming makes power drift slowly, and
every slowly-drifting nav frame on the bus correlates with it at r > 0.95. Only a *varying* input
(a throttle ramp, or a start/stop transition) produces trustworthy correlations.

`socket.CAN_EFF_FLAG` is negative on armv7l — mask `& 0xFFFFFFFF` before `struct.pack`, or just use the
literal `0x80000000` when testing received IDs.

## Security note

The root password had **not** been rotated as of 2026-08-17 — the previously shared one still worked. Worth changing
(Settings → General → Set root password, or `passwd` via SSH). The OP BOX `loginDefaultUser` endpoint
also still grants an admin session with no password.
