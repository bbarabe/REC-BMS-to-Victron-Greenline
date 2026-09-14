# Solar Priority — small fixes and staged repair plan

2026-09-14. Based on [the dated master review](solar-priority-master-2026-09-14.md) and source at `14e4085a97d32f6070803a79e9189430357a6265`, including the existing local plant/test changes.

**Keep the existing architecture, fix demonstrated mistakes, then tune the battery behavior from measurements. Approximately 1% daily movement is an engineering aim, not a runtime limit.** The owner explicitly accepts occasional 3% or 0.5% days and prefers small, adaptable code.

This instruction supersedes the master's proposals for enforced rolling energy allowances, reservation-based energy admission, and a persistent objective/reference/budget subsystem. It does not invalidate the recorded counterexamples. Unwanted bulk charge, intentional CHARGE burndown, repeated solar refill during DISCHARGE, and avoidable HOLD harvest/burn remain real behavior to fix.

This handoff opens bounded issues and plans larger changes. Production code, configuration and deployment have not been changed.

## 1. Architecture and scope

Keep the existing two services and REC's sole actuator ownership:

- `solar_engine.py`: select objective and transport intentions.
- `solar_priority.py`: supply fresh inputs and submit the existing leased request.
- `rec_policy_adapter.py` / `policy_contract.py`: enforce ownership and order actual transfers.
- `dbus_recbms.py`: apply the existing voltage/current primitives and REC protection.
- `energy_accounting.py`: measure and report signed Ah/Wh and uncertainty.

Use the existing demand model, clock, voltage readbacks, relay history and ledger. Prefer a branch, shared predicate or small field in an existing record when needed. Remove obsolete burn logic or duplicate retry logic as its replacement lands. Reuse saved target/reference information before adding persistence. No new service, general policy framework, energy reservation system, optimizer, or commissioning dashboard is needed.

Keep electrical limits, freshness, leases, relay dwell/rate protection, and protective returns. Those protections address concrete actuator and battery constraints. Approximate cycling aims do not need the same treatment.

Priorities here:

- **P1:** a demonstrated wrong-direction, permissive-command, or misleading-control failure; repair before calling normal Solar Priority behavior reliable.
- **P2:** bounded resilience/configuration repair, commissioning, or tuning.
- None of the findings establishes an observed over-voltage incident on the yacht.

## 2. Bounded GitHub issues

Each issue includes source links pinned to the reviewed commit, a reproduction or code finding, acceptance criteria, and a small scope. Priority is explicit in its title.

| Priority | Issue | Finding | Bounded change |
| --- | --- | --- | --- |
| P1 | [#1](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/1) | D01 | Preserve objective and sustain protection when required decision inputs disappear. |
| P1 | [#2](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/2) | D06 | Guard every CHARGE entry into ceiling-stall burndown. |
| P1 | [#3](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/3) | D04 | Report persistent unapplied commands and truthful requested/applied solar lead. |
| P1 | [#4](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/4) | D05 | Use a small justified target tolerance and arrival hysteresis, including restart. |
| P1 | [#5](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/5) | D08 | Feed the existing complete DC-bus demand into ordinary solar admission. |
| P2 | [#6](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/6) | D10 | Move primitive durations and related timestamps to monotonic time. |
| P2 | [#7](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/7) | D11 | Reject or consistently support full-threshold configuration at load time. |
| P2 | [#8](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/8) | D16, probe portion | Feed actual probe failures into existing durable backoff exactly once. |
| P2 | [#9](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/9) | D18; reporting in D03/D16 | Restore the ini section and remove inactive guarantees from active documentation. |

These are separate reviewable fixes, not nine new mechanisms. Start with [#1](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/1) and [#2](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/2); [#3](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/3) is needed before relying on command readiness during prepared return. Land [#5](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/5) before tuning admission, and [#4](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/4) before judging arrival behavior. [#6](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/6), [#7](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/7) and the ini repair can be developed independently. The probe outcome wiring belongs with the transfer checks below.

## 3. Larger work in stages

### Stage A — establish prepared return in the existing transfer path (P1)

Covers D02, D09's changeover, D14, the transfer timing part of D16, and the relevant D17 fixture limitation. Depends on the missing-data and command-fault fixes.

**A1. Order the commands.** Use the existing `PREPARE_CONNECT` path to install the applicable below-pack Quattro command before an ordinary return, preserve the separately required solar command within REC limits, and wait for the relevant readbacks before issuing the relay command. Keep the existing current brake during this change. Correct shutdown's present relay-first ordering as part of this slice. Publishing first is necessary; it is not evidence that hardware has already applied the command.

**A2. Separate availability from acceptance.** Carry the observed input availability, selected AC input and actual acceptance through the existing adapter/supervisor. An absent shore supply is not a refused relay write. Do not infer inverter-only operation merely because AC1 is inactive; another input can be accepted. Discover the available paths on this firmware before choosing one. Missing availability stays unknown. Do not let an attempted shore return pin solar charging indefinitely when shore is unavailable.

**A3. Bound preparation and observe settling.** Record preparation start, physical acceptance and settling with the existing monotonic clock. Define one bounded wait and a restrictive protective-return fallback so failed preparation cannot stall an urgent return indefinitely. Use signed Quattro V×I and effective commands to distinguish command readiness from actual charger settling. Make shutdown's best-effort ordering and the limits of process termination explicit.

Start the probe's evaluation/ramp clock from confirmed physical departure and usable voltage headroom, rather than the request. A delayed/refused transfer is not a failed solar capacity measurement.

**A4. Measure before changing the brake.** Observe prepared low-CVL return and stable shore holding; compare with recorded unprepared return evidence, using another unprepared trial only if needed. Retain the present `PV current + 5 A` brake until the prepared-return behavior is understood. If needed, keep that same cap only for a short reconnect settling interval. Then compare useful PV ramp and shore charge before removing the continuous sustain cap. Preserve REC current limits throughout.

**Done when:** offline traces prove command-before-relay ordering through propagation delay, source loss, shutdown and refusal; absent shore and alternate input acceptance have distinct outcomes; a boat trace confirms an ordinary prepared return and its settling behavior. No claim that all three regimes share one response curve.

**Stop/rollback:** if prepared CVL does not suppress the observed charge tail, retain the brake and adjust the measured margin or settling duration. Fix the small transfer sequence; do not add a new transfer controller.

### Stage B — make target regulation simple and stable (P1)

Covers the remaining D05 work, D07, ordinary HOLD harvest/burn, progress continuity, and the behavioral part of D03. Depends on Stage A for safe routine return and on the target-selection and complete-demand fixes.

**B1. One objective and one useful reference.** Select CHARGE, DISCHARGE or HOLD from target, measured SOC and a small documented hysteresis. Transport changes and required-input loss must not silently change that objective. Preserve earned progress in the existing reference record where valid. Reconstruct from saved target/reference plus fresh data after restart; a target change deliberately replaces the destination. Avoid serializing the whole engine.

**B2. Make HOLD actually hold.** Use a stable measured-voltage reference with the existing bounded servo/current feedback. Check the final Quattro/solar pair at arrival: requesting today's floor at the target is insufficient if ordinary lead subtraction still moves Quattro below the intended hold. Curtail excess PV once the loads are covered. Remove ordinary HOLD harvest-and-burn and ceiling-stall burn paths once the hold works, along with their now-unused latches/tunables. Retain intentional descent under DISCHARGE and existing protective exits.

**B3. Regulate battery direction directly.** In DISCHARGE, respond to sustained positive native battery current from either PV or Quattro. Reuse the servo's deadband, step and cadence; make the downward voltage reference follow earned descent closely enough to avoid refilling a stale ceiling. In CHARGE, preserve useful PV, support the DC shortfall on shore, and correct persistent draining with the existing servo/deficit response. Arrival from either side should hand into the same HOLD behavior.

Start by tuning the existing residual-current/deficit threshold and response time after removing systematic burn/refill causes. Do not add a daily allowance gate to address a −49 W residual.

**Done when:** small changes and restart select the right direction; darkness/sun transitions produce useful ascent or a downward staircase; ordinary HOLD no longer deliberately fills and burns; arrival is stable without repeated corrective excursions. Inspect signed integrals and relay traces, with the daily percentage interpreted as an approximate operating result.

**Stop/rollback:** if eliminating the CCL brake hurts reconnect behavior, retain Stage A's proven protection. Tune one voltage/current parameter at a time before changing architecture.

### Stage C — settle two operator semantics and calibrate only what is needed (P2)

Covers D12, D13, D15 and D17, with the measurement-duration part of D10 and D09's final tuning.

| Decision/work | Small proposed implementation | Evidence or decision still needed |
| --- | --- | --- |
| 100% target | Keep the full 61.96 V Quattro command, or lower REC limit, through completion and holding. Use the existing objective/command path for any endgame condition. | State whether 100% means immediate shore bulk from any SOC or exceptional shore support only near full. The current default implements the former; the owner's established requirement explicitly covers the endgame. Do not silently change this policy while fixing D11. |
| Lost consumer / operator action | Lease loss retains applicable protection and returns toward shore; a recovered consumer resumes through a fresh valid request. Handle explicit OFF and a changed target as explicit inputs in the same path. | Before implementing, write the expected output for lease loss, target change during loss, OFF, and recovery. Proposal: OFF relinquishes Solar Priority after protected return; target changes discard incompatible old references using fresh data. No new override workflow. |
| Low-target voltage calibration | Add a settled point to the existing curve only where observations show an actual mismatch. | Obtain useful rested/settled observations near 40% and 50% as operation permits; retain uncertainty about historical low-SOC labels. |
| Full endpoint | Record command and REC-reported SOC separately. | A 61.96 V command does not by itself calibrate the SOC counter to 100%. |
| Measurement pulse | Use the existing boost; choose about 90 s if actual headroom/ramp observations support a usable sample. Expire monotonically even if the sample never becomes usable. | Existing 120 s boost with 75–105 s sample window is different from the 90 s island probe. Verify the ramp before shortening it. |
| Capacity/shade | Keep the existing conservative shade rule initially; use fresh sustained production and full demand. | Relax only if observed false rejections materially waste PV; retain the historical adverse-shade cases. |
| Plant response | Add a small prepared-return response profile beside the existing adverse profile when measurements support it. | The current reconnect burst is independent of pre-applied low CVL. It cannot prove that preparation works or validate the full-SOC curve endpoint. |

These are implementation decisions and observation tasks, not reasons to block the independent fixes. Resolve the two operator semantics when their slice is implemented; the plan does not assume an unrequested bulk-charging policy.

**Done when:** chosen semantics are stated in a few sentences with corresponding scenario checks, useful calibration is recorded in the existing configuration, and the plant labels assumed versus measured behavior. Avoid a comprehensive battery-modeling project.

### Stage D — calculate, observe, tune, and simplify (P2)

Covers D03's approximate movement aim, D16's unused energy lifecycle and D18's final documentation.

1. Use the paper calculation below as the initial expectation. Replace its assumed residual current, event counts and tails with observed values.
2. Use existing native REC Ah/Wh and logs for a representative day/night interval under HOLD and the two directional objectives. Reuse observations collected in Stages A–C; 24–48 h traces are useful, not a new continuous test subsystem.
3. Report both charge and discharge. For HOLD, add them; for CHARGE, inspect discharge separately; for DISCHARGE, inspect charge separately. Use the current 1440 Ah reporting basis and show the 1400 Ah sensitivity until calibration warrants changing it.
4. Keep gaps visibly uncertain and reuse existing daily/rolling reporting. Missing accounting history does not lock out useful operation. A 3% day warrants context and, if repeated, tuning; it does not trigger a new control mode.
5. Remove unused energy reservation machinery if call-site and compatibility checks show it is dead. Otherwise label it inactive. Do not connect `reserve`, `close_reservation` or `end_reverse_event` merely to make every existing function active. Preserve native totals and compatible persisted records.

**Done when:** ordinary traces agree reasonably with the physical explanation; persistent bias or unnecessary relay activity has a concrete tuning action; the active docs describe actual behavior. There is no exact-1%-per-day acceptance gate.

## 4. Paper calculation for approximate daily movement

This is a **conditional engineering calculation for the proposed repaired behavior**, not a measured yacht result or proof that today's code achieves it. Assume event contributions are additional to the steady residual, and count their later recovery conservatively in HOLD.

With native current positive into the bank:

```text
Qabs = integral(abs(Ibattery)) dt_hours
movement_percent = 100 * Qabs / C_Ah
CHARGE wrong-way percent = 100 * Qdischarge / C_Ah
DISCHARGE wrong-way percent = 100 * Qcharge / C_Ah
EFC = Qabs / (2 * C_Ah)
```

Thus 1% combined movement is 14.4 Ah at 1440 Ah, or 14 Ah at 1400 Ah. Spread uniformly over 24 h, that is 0.60 A or 0.583 A of mean absolute current. It is about 835 Wh at 58 V; actual Wh uses measured voltage. A 0.01 EFC threshold would represent 2% combined movement.

One plausible nominal-day envelope **to validate**, using 56.4 V solely for the event conversions:

| Contribution | Explicit assumption | Combined Ah, including equal later recovery where applicable |
| --- | --- | --- |
| Settled regulation | Mean absolute residual ≤0.25 A over 24 h | 6.000 |
| Four cloud/load shortfalls | Each 200 W for 3 min, plus equal recharge afterward | 1.418 |
| Two reconnect charge tails | Each 5 A net into the battery for 3 min, plus equal later discharge | 1.000 |
| Four measurements | Each adds 100 W to the battery for 90 s, plus equal later discharge | 0.355 |
| **Total** | Sum of the assumptions above | **8.773 Ah** |

That is **0.609% at 1440 Ah**, or **0.627% at 1400 Ah**. Under those assumptions there is room around a 1% aim without runtime accounting permission. The assumed 0.25 A residual and three-minute reconnect tail are commissioning targets, not values demonstrated by current code. The 5 A is net battery current, not the combined DVCC cap. Event count assumptions are also not new quotas.

Useful sensitivities:

- With the same events, mean absolute residual around **0.48 A** consumes approximately 1% at 1440 Ah. Holding bias matters more than exact calendar-window semantics.
- A constant −49 W at 56.4 V for 24 h is **20.85 Ah / 1.448% discharge**, matching E04's concern. Fix the persistent bias or tune the existing deficit response; its magnitude alone does not justify a daily-budget subsystem.
- Two hypothetical reconnects each adding 100 Wh to the bank, each later discharged, contribute **7.09 Ah**. Substituting those tails for the nominal 1 Ah gives about **1.03%** for this example day. Larger/longer tails can explain 3% days; measure the tail rather than assume the fixture proves it.
- The nominal HOLD replay E11 previously recorded **0.895%** combined movement. It supports plausibility under one fixture profile, not a universal bound.

For CHARGE and DISCHARGE, use the same event arithmetic but count only the wrong-direction leg against the directional aim. Intended ascent/descent is not daily HOLD wobble; subsequent recovery still appears in total throughput.

The practical hypothesis is small: stable holding, no deliberate HOLD burn/refill, correct DC demand and prepared returns should make roughly 1% ordinary movement plausible. If observations miss that expectation, identify the largest Ah contribution and adjust the existing bias, deadband, timing or transfer margin first.

## 5. Full finding disposition

| Master finding | Disposition |
| --- | --- |
| D01 | P1 issue [#1](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/1). |
| D02 | P1 Stage A, including shutdown order. |
| D03 | Persistent drift addressed in Stage B; units in [#9](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/9); approximate wear becomes P2 calculation/monitoring in Stage D. Mandatory cumulative enforcement is superseded. |
| D04 | P1 issue [#3](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/3); physical settling follows in Stage A. |
| D05 | P1 issue [#4](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/4) for selection; Stage B for arrival and continuity. |
| D06 | P1 issue [#2](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/2). |
| D07 | P1 Stage B: battery-current feedback and a ceiling that follows descent. |
| D08 | P1 issue [#5](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/5). |
| D09 | P1 Stage A: preserve the brake, verify preparation, then simplify CCL use; Stage C tuning. |
| D10 | P2 issue [#6](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/6) for clock correctness; Stage C for physical sampling duration. |
| D11 | P2 issue [#7](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/7). |
| D12 | P2 Stage C: state full-target policy before changing it. |
| D13 | P2 Stage C: define lease loss, OFF, target change and recovery in the existing path. |
| D14 | P2 work within Stage A: actual shore availability and alternate-input acceptance. |
| D15 | P2 Stage C: limited, useful calibration. |
| D16 | P2 issue [#8](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/8) for durable probe outcomes; Stage A for probe timing; Stage D for unused accounting lifecycle. |
| D17 | P2 Stages A/C: observed response profiles and explicit fixture limits. |
| D18 | P2 issue [#9](https://github.com/bbarabe/REC-BMS-to-Victron-Greenline/issues/9); keep docs current as stages land. |

## 6. Verification and handoff

On 2026-09-14, the unchanged working-tree baseline was rerun successfully:

- `python3 test_solar_priority.py`: 112 engine assertions and 180 unit/integration tests passed.
- `python3 -m unittest test_solar_priority_plant`: four fixture isolation/lifecycle tests passed.

These passes establish the baseline; they do not close any of the new issues. The E02–E16 numerical replays cited in issues remain the master's previously recorded offline evidence, not new yacht observations.

For each implementation slice, add the smallest meaningful regression for its actual failure, run the relevant existing suite, and inspect command/relay ordering where applicable. Judge physical claims with boat observations. Deploy later through the existing `CLAUDE.md` / `deploy_cerbo.py` procedure, publisher before consumer, preserving live calibration, localsettings and the newest ledger. Use the same observation log for regulation tuning and the paper calculation.
