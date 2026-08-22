# CZone: what the open-source projects know

Two projects reverse-engineer the same BEP/CZone proprietary protocol we do,
from the opposite side — both emulate a CZone **module** (a thing that owns
relays), where `dbus-czone` is a **consumer** (a thing that watches and
commands someone else's). Neither is a dependency; this is a record of where
they corroborate `specification.md`, where they add something, and which of
their behaviours would misbehave on this boat's bus.

| | [negrusti/esp32-czone][ez] | [gerryvel/SR-Aktor][sr] |
|---|---|---|
| Target | ESP32-S3, 8 relays | ESP32, 3 relays + web UI |
| RE quality | high — worked from `configuration-tool.exe` | heuristic, tuned to one B&G Vulcan 7 |
| Best for | the `.zcf` format, PGN framing | the PGN 65280 command alphabet |
| Ships a real `.zcf` | no | **yes** |

[ez]: https://github.com/negrusti/esp32-czone
[sr]: https://github.com/gerryvel/SR-Aktor

## What we took

**The `.zcf` format** (negrusti). Verified byte-for-byte against two
independently produced files — this boat's [`48-56.zcf`](48-56.zcf) and
SR-Aktor's `AD CZone System1.zcf` — including both CRC-8s, the `08 ?? 05 0E`
control-section marker, the record layout, the `(dipswitch << 8) | channel`
output address and the `H`-strided loads section. Implemented in
[`zcf_parse.py`](zcf_parse.py); the field semantics are in `specification.md`,
"The CZone configuration file". The two findings that are ours, not theirs: the
commander function byte carries the momentary/latching split, and control-record
field 1 is the same category bitmask PGN 130820 broadcasts.

**The PGN 65280 command alphabet** (SR-Aktor). Their handler documents `F1`
absolute ON, `71`–`7F` toggle press and the release codes, observed on a
Vulcan. The same alphabet appears verbatim from this boat's UC1, and splits the
same way by circuit type. Documented in `specification.md`, "PGN 65280", and
decoded by `nmea_decode.py`.

**The alternate header `0x9913`.** B&G/Navico MFDs send it in place of BEP's
`0x9927` with an identical layout — visible in
[`captures/mfd-boot.log`](captures/mfd-boot.log) from the MFD at src `0x17`.
Anything matching on the header must accept both.

## Where the two agree independently

Worth more than either alone, since they were derived separately:

- **PGN 65291** config-transfer ACK: `hdr | target | status | u16 block |
  moduleAddr | FF` — byte-identical in both.
- **PGN 130816** ZCF transfer: 23-byte header, 200-byte chunks.
- **PGN 130817** status: `hdr | page 0x01 | module address | 3-byte record per
  output`.
- **PGN 65290** as the config claim / transfer-arbitration PGN.
- **Byte 5 of a 65280 frame is the target module address** (its dipswitch), not
  a bank instance and not related to the N2K source address. Our capture agrees:
  always `01`, and this UC1 is dipswitch 1.

They disagree on **PGN 130816 byte 4**: negrusti puts the module address there
(from the configuration tool's own parser), SR-Aktor puts `0x00` and moves the
address to byte 5. negrusti's is the better-sourced claim.

## Behaviours that would misbehave on this bus

Not bugs we need to fix — reasons not to copy these projects' send paths, and
things to recognise if such a device ever appears on the bus:

1. **SR-Aktor broadcasts PGN 127502 as a status announcement**, periodically
   and on every relay change, on bank instance 0. 127502 is a *command* PGN.
2. **SR-Aktor's 65280 handler never filters on the target byte** — it only
   learns from it. Any circuit ID `0x05`–`0x0C` from any source drives its
   relays. On this boat circuits 5, 7, 9 and 10 are actively pressed, so such a
   device would mirror our bilge pump, horn, nav and anchor lights.
3. **SR-Aktor hardcodes `0x1e` in 65284 byte 3** as a "switch summary field
   identifier". The real UC1 puts the **module type** there — ours sends
   `27 99 01 1C ...`, `0x1C` = 28 — and negrusti agrees. It then writes an
   8-bit state where the real frame carries a 32-bit channel bitmap.
4. **PGN 65283 is contested on this bus.** Fourteen sources emit it with
   non-BEP payloads and the UC1 never does; SR-Aktor transmits into it as a
   "switch change ack".
5. **negrusti's Control-1 channel map (`0-3` + `12-15`) does not describe this
   UC1**, which uses channels 0-7 and 12-14 for 11 outputs. Do not infer an
   output count or a channel layout from a module type.

## Still unanswered

Neither project covers the PGN 65299/130820 circuit-description query pair, the
category mask, bank semantics, the UC1's ISO-Request NAK, or the fact that the
query works from the unclaimed source address `0xFE` — all of which are ours,
and are in `specification.md`.

Neither has anything on `f0`/`f2` of a `.zcf` control record (`0x0000` and
`0x0020` on all 14 circuits seen), on the trailing bytes of a commander record,
or on the loads-record header beyond the channel address and name.
