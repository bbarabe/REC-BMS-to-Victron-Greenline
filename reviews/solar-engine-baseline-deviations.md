# Restored engine: baseline and deliberate corrections

The decision code in `dbus-recbms/solar_engine.py` comes from commit
`386e125`, `dbus-recbms/solar_priority.py`, engine 4.9. It retains the five
states, one-way target overlay, SOC gates, probe and cooldown durations,
capacity capture/model, burndown, heater suspend, and full-charge behavior.
The 98 engine assertions from the committed root `test_solar_priority.py`
are restored in `test_solar_engine.py`; the old REC driver tests are not
ported because those tested an obsolete direct sustain implementation.

Deliberate differences:

- The module has no D-Bus imports, I/O, relay writes or configuration parser.
  `Engine(tunables, now_ms, logger=None).tick(now_ms, Inputs)` is pure apart
  from its own state and optional logger. Times must use the same monotonic
  millisecond clock. Backward/nonfinite engine time is rejected.
- `Val(value, timestamp)` means the time of a successful observation,
  including observations whose value stayed constant. Missing, nonfinite,
  future-dated, or 20-second-old inputs are unavailable. The adapter must
  preserve source observation times instead of timestamping cache reads.
  Fresh system/VE.Bus/REC observations remain necessary; missing optional
  MPPT values do not independently force an island exit.
- `Inputs.quattro_w` is mandatory fresh signed Quattro DC voltage times
  current. The engine no longer reconstructs Quattro power from battery,
  DC load and PV readings. The baseline test plant explicitly synthesizes
  this meter for its old scenarios; additional assertions exercise the
  case where the real meter disagrees with that reconstruction.
- Battery and probe averages are weighted by elapsed time. Battery warmup
  is four seconds for the short mean and 59 seconds for the long mean,
  preserving the committed five/60-sample thresholds at 1 Hz. Windows retain
  their boundary observation and never integrate a sample over a gap longer
  than source freshness. Battery windows clear when power is unavailable
  and are bounded to 4096 observations even if a caller spins.
- `Inputs.departure_allowed` defaults true for standalone compatibility.
  Production supplies REC transfer readiness. When false, the engine holds
  shore or suspend without starting a probe clock, while retaining its
  floor/ceiling request. It does not block a return to shore or transitions
  that occur while already islanded. A waiting admission starts immediately
  when readiness is restored and the original engine conditions still hold.
- Outputs retain the baseline edge fields `cmd`, `sustain` and `boost`.
  They additionally expose persistent `transfer_intent` (`shore`/`island`),
  `charge_intent` (`release`/`floor`/`ceiling`), and `reason`.
  `boost_v` is an alias of the **edge** `boost`: `None` means no new request,
  zero releases, positive volts request a publisher-bounded pulse. Lease
  refresh must not turn that pulse into a continuously renewed boost.

Validation: `python3 test_solar_engine.py` passes 112 assertions: 98 restored
baseline assertions plus 14 admission, metering, freshness, timing and boost
checks. No boat deployment was performed for this extraction.
