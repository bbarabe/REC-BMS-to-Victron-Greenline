Here's the full decode. These are REC-BMS CAN frames following the Victron/SMA CAN-BMS protocol on an 11-bit CAN bus.

0x351 — Charge/Discharge Limits
73 02 C0 12 C0 12 FA 01 (all 16-bit LE, unsigned)
BytesFieldRawDecoded0–1Charge Voltage Limit (CVL)0x0273 = 62762.7 V (→ 4.18 V/cell × 15S)2–3Charge Current Limit (CCL)0x12C0 = 4800480.0 A4–5Discharge Current Limit (DCL)0x12C0 = 4800480.0 A6–7Discharge Voltage Limit (DVL)0x01FA = 50650.6 V (→ 3.37 V/cell × 15S)
This looks like a 15S NMC pack. 62.7 V / 15 = 4.18 V/cell charge target; 50.6 V / 15 = 3.37 V/cell low cutoff.

0x355 — State of Charge / Health
47 00 62 00 1F 1C 00 00
BytesFieldRawDecoded0–1SOC0x0047 = 7171%2–3SOH0x0062 = 9898%4–5Hi-res SOC (0.01%)0x1C1F = 719971.99%6–7Reserved0x0000—

0x356 — Voltage / Current / Temperature
89 16 35 00 8E 00 00 00
BytesFieldRawDecoded0–1Pack voltage (0.01 V)0x1689 = 576957.69 V (3.846 V/cell)2–3Pack current (0.1 A, signed)0x0035 = 535.3 A (charging)4–5Temperature (0.1 °C)0x008E = 14214.2 °C6–7Reserved0x0000—

0x35E — BMS Name (ASCII)
52 45 43 2D 42 4D 53 00 → "REC-BMS"

0x35F — Firmware / Capacity
00 00 02 09 78 05 00 00
BytesLikely fieldRawDecoded0–1Product/chemistry ID0x000002–3Firmware version02, 09v2.9 (or 9.2 depending on order)4–5Nominal capacity (Ah)0x0578 = 14001400 Ah (or 140 Ah if 0.1 Ah scale)6–7Reserved0x0000—
If your bank is ~140 Ah nominal, it's 0.1 Ah resolution. If it's actually a large marine bank, 1400 Ah could be literal.

0x360 — Alarms / Warnings
FF 00 00 00 00 00 00 00
Standard protocol uses 2-bit fields per alarm (00=OK, 01=Warn, 10=Alarm). Byte 0 = 0xFF is unusual — in REC-BMS this likely means all-supported / capability flags rather than active alarms, since everything else in this snapshot looks healthy. Bytes 1–7 all 0x00 = no active alarms or warnings.

0x372 — Module Count
06 00 00 00 00 00 00 00 → 6 modules connected to the BMS.

0x373 — Cell Voltage & Temperature Extremes
03 0F 09 0F 1D 01 1F 01
BytesFieldRawDecoded0–1Min cell voltage (mV)0x0F03 = 38433.843 V2–3Max cell voltage (mV)0x0F09 = 38493.849 V4–5Min cell temperature (K)0x011D = 28512 °C6–7Max cell temperature (K)0x011F = 28714 °C
Only 6 mV cell imbalance — very well balanced pack. Temp spread is just 2 °C.

0x374–0x377 — Module/String Identifiers (ASCII)
CAN IDHexString0x37443 37 53 55 31"C7SU1"0x37543 31 34 53 55 32"C14SU2"0x37654 31 53 55 34"T1SU4"0x37754 31 53 55 33"T1SU3"
These are module/sensor labels — "C" likely cell-voltage units, "T" temperature sensors, "SU" sub-unit number.

0x379 — Available Energy or Capacity Remaining
A0 05 00 00 00 00 00 00
0x05A0 = 1440. Likely remaining capacity — 1440 Wh, or 1440 × 0.1 Ah = 144 Ah remaining (which would be ~72% of a 200 Ah bank, aligning with 71.99% SOC and suggesting nominal capacity is ~200 Ah).

0x380 — Serial Number (ASCII)
39 4D 2D 30 34 38 35 00 → "9M-0485"

0x381 — Status / Reserved
00 00 00 00 00 00 00 00 → All zeros — no active status flags.

0x404 — Heartbeat / Keep-alive
00 00 00 (DLC=3) → Likely a BMS heartbeat or Victron-compatible keep-alive frame. All zeros = normal/OK.

Summary snapshot: 15S NMC bank at 57.69 V / 5.3 A charge / 71.99% SOC / 98% SOH / 14 °C, 6 modules, 6 mV cell delta, no alarms. The pack is in excellent shape.It is a 1440ah battery bank. it's big.10:34 AMReconciled capacity specifications for substantial marine battery systemReconciled capacity specifications for substantial marine battery systemThat's a serious bank — 1440 Ah at ~58 V is roughly 83 kWh. Makes sense for the boat.
So correcting my earlier decode: 0x35F bytes 4–5 (1400 Ah) is likely the usable capacity, and 0x379 (1440) is the nominal capacity. At 71.99% SOC that's about 1037 Ah / ~60 kWh available. Plenty of reserve.