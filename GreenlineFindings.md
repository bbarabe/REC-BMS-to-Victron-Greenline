# Greenline 6GK — can0 reverse-engineering findings

Captured passively with `candump` on the Cerbo GX (can0, 250 kbit/s NMEA2000 backbone), 2026-07-07.
Verified against the live MFD hybrid page in TWO states:
1. MOTOR mode: both drives 1004 rpm, 54 V, 7.6/7.8 kW propulsion
2. GENER. mode (charging): diesels 1011/1056 rpm, motors generating 0.8/3.8 kW, BMS +71.2 A

## What's on the bus

| Source | Protocol | Role |
|---|---|---|
| 11-bit IDs `x8A`/`x8B` | CANopen-style TPDOs | **Electric drives** — node 0x0A = PORT, 0x0B = STARBOARD, ~10 Hz |
| 29-bit src `0x64`/`0x65` | J1939-style | HCU (hybrid control unit) port / starboard |
| 29-bit src `0x8D`/`0x8E` | NMEA2000 (mfg code 172) | Engine gateway — **diesel** engine instances 0/1 |
| 29-bit src `0x77/0x78/0x79` | J1939 TSC1 (PGN 0) → dest 0x07 | Throttle/control keep-alives (constant while cruising) |
| src `0x10` | NMEA2000 | Battery monitor — 12 V house/start batteries only (inst 2: 14.12 V, inst 3: 13.97 V) |
| Others (0x02–0x23, 0x51–0x81) | NMEA2000 | Nav gear (Simrad/Navico heading, GPS, AIS, autopilot, rudder…) |

**The 48 V propulsion BMS (SOC 36 %, 54.3 V, −281 A) was NOT seen on can0.** It likely feeds the MFD via the HCU frames (see 61452 below) or lives on another CAN segment (the Cerbo's second port, can1, is currently DOWN).

## Electric motor data — CANopen TPDOs (the good stuff)

Each frame = two IEEE-754 little-endian floats. `A` = port (node 0x0A), `B` = starboard (0x0B).

| CAN ID | Bytes 0–3 (float 1) | Bytes 4–7 (float 2) | Verified against display |
|---|---|---|---|
| `0x18A/18B` | status byte `01` = running (unchanged in MOTOR and GENER. modes) | — | — |
| `0x28A/28B` | controller-region temp °C (33.1/37.8 charging vs 41.8/42.8 motoring — tracks between MOSFET and DC temps, exact sensor unclear) | **Electric motor temp °C** | 54.2/51.5→"54/51" ✓; 49.3/47.9→"50/48" ✓ |
| `0x38A/38B` | **Motor voltage V** | **Motor phase current, RMS A** (display shows peak = ×√2) | 54.1→"54 V" ✓; 342.3×√2=484.1→"484" ✓; 40.6×√2=57.4→"57" ✓; 188.4×√2=266.5→"266" ✓ |
| `0x48A/48B` | **Motor power, ×100 W, SIGNED** (+ = motoring, − = generating/charging) | **Motor RPM** (stays positive while generating) | +77.5→7.75 kW vs "7.6" ✓; −9.2→−0.92 kW vs "0.8" gen ✓; −42.7→−4.27 kW vs "3.8" gen ✓ | 1004→"1004" ✓; 1012/1057→"1012/1058" ✓ |
| `0x305` | all zeros | | |
| `0x307` | `12 34 56 78 "VIC"` constant — ID/heartbeat | | |

Derived: motor DC current = power / voltage → 7748/54.1 = 143 A ≈ display "140 A" ✓.
Power magnitude reads ~10 % above the display in GENER. mode (0.92 vs 0.8, 4.27 vs 3.8) — display may show battery-side power after losses.

## HCU frames (src 0x64 = STARBOARD, 0x65 = PORT — confirmed via asymmetric RPM 1056/1012)

| PGN (hex ID) | Field map (LE 16-bit words) |
|---|---|
| 61451 `14F00Bxx` | **w2 = motor RPM × 8** (8455→1056.9 ✓, 8097→1012.2 ✓); w4 = 1080 constant; w0 = load-dependent, NOT voltage (44452 motoring → 33421–39000 charging), unknown |
| 61452 `14F00Cxx` | **w4 = motor phase current peak × 20** (9382→469.1 ✓, 5325→266.3 ✓, 1146→57.3 ✓ — exact across both sessions); w2 correlates linearly with per-motor DC current (scale ≈ 39/A, offset ≈ 31900, unconfirmed); w0 unknown |
| 61453 `14F00Dxx` | three stable temps ×1/256 (≈38.1–40.2 °C both sessions) — does NOT track displayed MOSFET/DC temps; possibly coolant/other sensors |
| 65243 `14FEDBxx` | slow counters — w2 = 27–29 (BMS max temp 28.4 °C?), w0 steps in ×256 units, unconfirmed |

## Diesel side — standard NMEA2000 from 0x8D/0x8E

- PGN 127488: **diesel RPM ×0.25**, instance 0 = port, 1 = stbd (0 in MOTOR mode; 1012/1056 in GENER. mode → display "1011/1056" ✓)
- PGN 127489: coolant temp 25 °C, starter battery 13.15/12.95 V, **engine hours 208.1 h / 209.2 h** → display "208/209 h" ✓
- PGN 127493 transmission, 65363 proprietary (0x01E0 = 480 ≈ phase-current limit?)

## Confirmed by the charging session

- Power sign: negative floats in `0x48x` f1 while generating ✓
- Power scale ×100 W ✓ (two very different loads)
- Side assignment: CANopen 0x0A = port, 0x0B = stbd; HCU 0x64 = stbd, 0x65 = port
- 48 V BMS (SOC 32 %, +71.2 A) still absent from can0 — not in any frame that tracked SOC across the 36 %→32 % change

## Reverse-gear session (motors ~550 rpm, ~0 A)

- RPM float stays **positive** in reverse; `0x18x` status stays `01` — direction is NOT in the CANopen frames.
- **Direction found in NMEA2000 PGN 127493** (`09F2058D` port / `09F2058E` stbd): byte 1 bits 0–1 = 0 Forward (`0xFC`, seen in both FWD sessions), 2 Reverse (`0xFE`, seen in REV) — standard N2K gear enum. The flow now maps this to Venus `Motor/Direction`.

## Still to verify

1. `0x28x` float 1 sensor identity; HCU 61451 w0 and 61452 w0/w2 exact scales.
2. Throttle position / torque %: not yet found on can0 (TSC1 frames from 0x77–0x79 stayed constant across 16–49 % throttle — they're keep-alives, not levers).
3. Neutral gear value in 127493 (expect `0xFD` → 1 = Neutral, untested).

## Security note

You shared the root password in this chat — consider changing it (Settings → General → Set root password, or `passwd` via SSH).
