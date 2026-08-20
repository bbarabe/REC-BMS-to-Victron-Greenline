# REC-BMS CAN frame decode — working notes

Field-by-field working behind the frame map in `specification.md`
("Main Battery Bank"), which is the authoritative version. This file exists for
the scales and the worked examples; where the two disagree, the specification
wins. Frames as captured in `BatteryCAN.md`.

The BMS speaks the Victron/SMA CAN-BMS protocol on 11-bit standard CAN ids. It
sits on CAN1, so every id below reaches the Cerbo repackaged as `0x18FF0NNN`
by the YDNB-07 (see `YDNB.CFG`). All multi-byte fields are little-endian.

Pack: **15S NMC, 1440 Ah nominal (~83 kWh at 58 V), 6 modules.**

## 0x351 — Charge/discharge limits

`73 02 C0 12 C0 12 FA 01`

| Bytes | Field | Raw | Decoded |
|---|---|---|---|
| 0–1 | Charge voltage limit (CVL) | `0x0273` = 627 | 62.7 V (4.18 V/cell) |
| 2–3 | Charge current limit (CCL) | `0x12C0` = 4800 | 480.0 A |
| 4–5 | Discharge current limit (DCL) | `0x12C0` = 4800 | 480.0 A |
| 6–7 | Discharge voltage limit (DVL) | `0x01FA` = 506 | 50.6 V (3.37 V/cell) |

0.1 V / 0.1 A scale throughout. The 62.7 V CVL is the REC's own 100 % sync
point and the hard ceiling `dbus-recbms` clamps against.

## 0x355 — State of charge / health

`47 00 62 00 1F 1C 00 00`

| Bytes | Field | Raw | Decoded |
|---|---|---|---|
| 0–1 | SOC % | `0x0047` | 71 % |
| 2–3 | SOH % | `0x0062` | 98 % |
| 4–5 | Hi-res SOC (0.01 %) | `0x1C1F` = 7199 | 71.99 % |
| 6–7 | reserved | `0x0000` | — |

The driver publishes the hi-res field: at low current it can hold the same
value for minutes, which matters for the change-only consumers downstream.

## 0x356 — Voltage / current / temperature

`89 16 35 00 8E 00 00 00`

| Bytes | Field | Raw | Decoded |
|---|---|---|---|
| 0–1 | Pack voltage (0.01 V) | `0x1689` = 5769 | 57.69 V (3.846 V/cell) |
| 2–3 | Pack current (0.1 A, **signed**) | `0x0035` = 53 | +5.3 A (charging) |
| 4–5 | Temperature (0.1 °C) | `0x008E` = 142 | 14.2 °C |
| 6–7 | Charge cycles | `0x0000` | 0 |

Bytes 6–7 are not reserved: they carry the charge-cycle count, published as
`/History/ChargeCycles`.

## 0x35E / 0x380 — Identity (ASCII)

| Id | Bytes | String |
|---|---|---|
| 0x35E | `52 45 43 2D 42 4D 53 00` | `REC-BMS` (manufacturer) |
| 0x380 | `39 4D 2D 30 34 38 35 00` | BMS serial |

## 0x35F — Chemistry / version / configured capacity

`00 00 02 09 78 05 00 00` — Victron CAN-BMS `BatteryInfo`.

| Bytes | Field | Raw | Decoded |
|---|---|---|---|
| 0–1 | chemistry / product id | `0x0000` | — |
| 2–3 | version pair, **two plain bytes** (`major.minor`, not LE) | `02`, `09` | `2.9` -> `/HardwareVersion` |
| 4–5 | **capacity configured in the BMS** (Ah, LE) | `0x0578` | **1400 Ah** -> `/RecBms/ConfiguredCapacity` |
| 6–7 | reserved | `0x0000` | — |

Bytes 4–5 are capacity, **not** a firmware version. Driver versions before
1.3.1 read them as one and published a meaningless `1400` to
`/FirmwareVersion`; that path now has no source at all, because this frame's
only version field is the bytes 2–3 pair.

The `major.minor` encoding of bytes 2–3 is confirmed by a REC-Q capture
elsewhere reading `02 06` as version 2.6 — the same two-plain-bytes form, not
little-endian. Its *label* is not confirmed: REC's own documentation calls it
the hardware version (which is what this driver publishes it as), while that
same independent work reads it as the software version. Nothing depends on it
either way.

## 0x360 — Force charge flag

`FF 00 00 00 00 00 00 00`

Byte 0 nominally signals "force charge requested". **This REC asserts `0xFF`
permanently** — observed at 65 % SOC, balanced cells, at rest — so it is a
static capability flag carrying no state, and forwarding it to Venus would
assert a charge request forever. `dbus-recbms` keeps
`forward_charge_request = false` and exposes the raw byte at
`/RecBms/ForceChargeRequest` instead; see `dbus-recbms/config.ini` for which
Venus consumers read `/Info/ChargeRequest` and why it matters if ESS is ever
configured. Bytes 1–7 are zero.

## 0x372 — Module status

`06 00 00 00 00 00 00 00`

| Offset | Field (u16 LE) | Raw |
|---|---|---|
| 0 | modules online | 6 |
| 2 | modules blocking charge | 0 |
| 4 | modules blocking discharge | 0 |
| 6 | modules offline | 0 |

Bytes 2/4/6 drive the synthetic `HighChargeCurrent` / `HighDischargeCurrent` /
`InternalFailure` alarms.

## 0x373 — Cell extremes

`03 0F 09 0F 1D 01 1F 01`

| Bytes | Field | Raw | Decoded |
|---|---|---|---|
| 0–1 | Min cell voltage (mV) | `0x0F03` = 3843 | 3.843 V |
| 2–3 | Max cell voltage (mV) | `0x0F09` = 3849 | 3.849 V |
| 4–5 | Min cell temperature (K) | `0x011D` = 285 | 12 °C |
| 6–7 | Max cell temperature (K) | `0x011F` = 287 | 14 °C |

Temperatures are whole kelvin, so °C = raw − 273. This snapshot is a 6 mV cell
delta over a 2 °C spread. Every synthetic voltage and temperature alarm in the
driver is derived from this frame, and the solar-boost safety gate re-checks it
on every tick.

## 0x374–0x377 — Extreme-cell identity (ASCII)

| Id | Bytes | String | Meaning |
|---|---|---|---|
| 0x374 | `43 37 53 55 31` | `C7SU1` | cell currently holding min voltage |
| 0x375 | `43 31 34 53 55 32` | `C14SU2` | cell currently holding max voltage |
| 0x376 | `54 31 53 55 34` | `T1SU4` | sensor holding min temperature |
| 0x377 | `54 31 53 55 33` | `T1SU3` | sensor holding max temperature |

`C<n>` = cell-voltage tap, `T<n>` = temperature sensor, `SU<n>` = sub-unit
(module). These **track the extremes** — the strings change as cells move — so
they are not static module ids. Published as `/System/Min|MaxVoltageCellId`
and `/System/Min|MaxTemperatureCellId`.

## 0x379 — Installed capacity

`A0 05 00 00 00 00 00 00` -> `0x05A0` = **1440 Ah rated/installed**.

Victron CAN-BMS `BatterySize`. This is the frame the protocol designates for
installed capacity, so it is what feeds `/InstalledCapacity` and therefore
`/Capacity` (SOC × installed), `/ConsumedAmphours` and `/TimeToGo`. Constant
regardless of SOC.

**The bank reports capacity twice and the two disagree**: 1440 Ah rated here,
1400 Ah configured in 0x35F. The REC computes its own SOC against the
configured figure, and the Greenline OP BOX shows ~1398 Ah as nominal/usable
(`GreenlineFindings.md`, `AV[9]`), so 1400 is the more physical number — but
`/InstalledCapacity` follows the protocol, not the smaller figure. Both are on
the bus; see `dbus-recbms/README.md`.

It is *not* remaining capacity — the early reading of "144 Ah remaining"
happened to sit near 72 % of a guessed 200 Ah bank, which made a wrong decode
look self-consistent.

## 0x381 — Undecoded

`00 00 00 00 00 00 00 00` — all zeros in every capture so far, so there is
nothing to decode against. Not used.

## 0x404 — Heartbeat

`00 00 00` (DLC = 3) — keep-alive. Only its arrival matters; the driver's
staleness clock is fed by any successfully decoded frame, not this one alone.

## Not sent by this BMS

**0x35A, the standard CAN-BMS alarm frame, is absent.** Every alarm Venus sees
is synthesised by `dbus-recbms` from 0x373 (cell extremes), 0x355 (SOC),
0x372 (module status) and CAN staleness. Thresholds are listed in the
specification and configured in `dbus-recbms/config.ini`.
