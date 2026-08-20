# REC-BMS raw frame capture

One representative frame per CAN id, captured on **CAN1** (the drive/BMS bus)
with 11-bit standard ids — i.e. *before* the YDNB-07 repackages them as
`0x18FF0NNN` for the Cerbo. Timestamps are relative to the start of the
capture; ids repeat on their own cycles, so the ordering here is not
meaningful.

Column format: `time RX <bus> <id> <dlc> <data bytes> <interval ms>`.

Field-by-field decode: `Decoded.md`. Authoritative frame map:
`specification.md`. The 16 ids the bridge forwards are listed in `YDNB.CFG`.

```
00:08:21.136 RX 0      351 8 73 02 C0 12 C0 12 FA 01    151
00:08:19.173 RX 0      355 8 47 00 62 00 1F 1C 00 00     51
00:08:19.324 RX 0      356 8 89 16 35 00 8E 00 00 00     49
00:08:19.626 RX 0      35e 8 52 45 43 2D 42 4D 53 00     51
00:08:19.928 RX 0      35f 8 00 00 02 09 78 05 00 00     51
00:08:20.079 RX 0      360 8 FF 00 00 00 00 00 00 00     52
00:08:20.230 RX 0      372 8 06 00 00 00 00 00 00 00     52
00:08:20.381 RX 0      373 8 03 0F 09 0F 1D 01 1F 01     52
00:08:20.532 RX 0      374 8 43 37 53 55 31 00 00 00     52
00:08:20.683 RX 0      375 8 43 31 34 53 55 32 00 00     52
00:08:20.834 RX 0      376 8 54 31 53 55 34 00 00 00     52
00:08:20.985 RX 0      377 8 54 31 53 55 33 00 00 00     52
00:08:21.287 RX 0      379 8 A0 05 00 00 00 00 00 00     52
00:08:21.438 RX 0      380 8 39 4D 2D 30 34 38 35 00     51
00:08:18.870 RX 0      381 8 00 00 00 00 00 00 00 00     51
00:08:19.474 RX 0      404 3 00 00 00 __ __ __ __ __     52
```
