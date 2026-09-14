# Stage A on the boat — 2026-09-14

Boat observations for the [repair plan](solar-priority-repair-plan-2026-09-14.md) Stage A "done when" and A4. All figures are from the read-only MQTT trace recorder (`solar_watch.py`, one row per second; the raw files are `captures/solar-trace-2026-09-14-*.jsonl`, kept out of git). Times UTC; the boat is at UTC−7. Target 60 %, one-way CHARGE from 46 %; slider curve at 60 % = 56.42 V, Quattro standing lead 0.15 V, floor band 0.30 V, sustain cap PV current + 5 A.

## Timeline

| Time | Code | Event |
| --- | --- | --- |
| 05:34–05:37 | 3.1.0 | Stage A pair deployed. |
| 14:46–14:58 | 3.2.0, 3.3.0 | Stage B pair, then the full-target semantics. |
| 16:05:57 | 3.3.0 | First automatic departure ever recorded on 3.x: direct entry on a live capture (98 + 244 W vs need 335 W); relay open and acknowledged 16:06:05. |
| 16:06:08 | 3.3.0 | REC permission blinked for one tick (systemcalc's estimated `/Dc/System/Power` under 0 W → demand "implausible"). The non-urgent return began a prepared wait; permission came back at 16:06:17 but the supervisor stayed PREPARE_CONNECT with the timer running. |
| 16:06:42 | 3.3.0 | **Unprepared** closure on the slider pair (56.27 / 56.42 V, CCL 480 A). Engine lockout 1 h ("AC re-accepted externally"); floor back at 16:07:27. |
| 16:17 | 3.3.0+f32ec16 | Fix deployed: negative DC estimate clamped, 10 s grace on a non-urgent loss of permission, a withdrawn return re-affirms the island. REC restart cleared the lockout. |
| 16:46:38 | fixed | Second departure; ISLANDED at 16:46:40, no wobble. |
| 16:52:32 | fixed | 1.75 kW load → SUSPEND; floor requested on the same tick; pair verified 16:52:38 (Quattro command 55.40 V = the hold, CCL 12 A); **prepared** closure 16:52:41. |
| 16:57:52 | fixed | Resume → third departure; ISLANDED 16:57:54. |
| 17:01:10 | fixed | CHARGE deficit rule (three-minute mean under −50 W, PV ~390 W vs need 445 W); **prepared** closure 17:01:17 (Quattro command 55.42 V = the hold). |

## The two closures, first ten minutes

| | Unprepared 16:06:42 | Prepared 16:52:41 |
| --- | --- | --- |
| Quattro command at the edge | 56.27 V (slider − lead), CCL 480 A | 55.40 V (the hold), CCL 11 A (PV + 5) |
| Pack at the edge | 55.40 V | 55.35 V |
| Quattro DC peak | **3 856 W at +49 s** | **394 W** (6.4 A at the edge) |
| Quattro energy | 54 Wh | 24 Wh (5 min on shore, then islanded again) |
| Battery charge, all sources | 88 Wh | 54 Wh (mostly the sun through the band: PV 6–8 A) |
| Quattro ≤ 3 A first at | +520 s | +148 s |

The unprepared case is master D02 / E03 measured on the boat: the Quattro bulked at its full permission until the floor arrived 45 s later, then decayed under the cap over eight minutes. The prepared case never exceeded the cap: the Quattro started at the cap plus the DC loads (6.4 A) with its command already on the hold, and was under 3 A within two and a half minutes.

## What this settles, and what it does not

- **Stage A done-when, boat part: met.** Two ordinary prepared returns (a suspend and a deficit) closed with the below-bank pair and the current cap in force, acknowledged and accepted in that order, and settled without a bulk burst. Departures under the fixed code entered ISLANDED cleanly three times.
- **A4 brake question: keep the cap.** Both prepared closures had the `PV + 5 A` cap active, so the prepared CVL alone is *not* shown to suppress the tail; the observed tail (6.4 A decaying to under 3 A in 148 s) is what cap plus prepared command give together. A trial without the cap is a separate, deliberate measurement; nothing here justifies removing it. If anything the data supports the plan's alternative of keeping the cap for a settling interval of a few minutes, to be judged in Stage D with more returns.
- **The flicker was pre-existing.** `Demand/Valid` dropped for single ticks under 3.0–3.2 as well; there it produced immediate protective returns. Whether earlier "departures" in the telemetry (4 in the 24 h before deployment) were such flickers is not established.
- **Not yet observed:** a dusk return with no sun, a DISCHARGE or HOLD return, arrival at the target, and the Quattro's behaviour with the cap lifted.
