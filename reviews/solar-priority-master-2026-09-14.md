# Solar Priority — master requirements, findings, and evidence

Consolidated 2026-09-14. Reviewed source: working tree at `14e4085`, REC and Solar Priority **3.0.0**, restored engine **4.9**. Includes the existing local plant/test changes and the reconciliation in section 4 of the 85-item review.

**Execution update — owner clarification, 2026-09-14:** simplicity governs the repair work. Approximately 1% daily movement is a calculation-and-monitoring aim; occasional 3% or 0.5% days are acceptable. The [small-fix issues and staged repair plan](solar-priority-repair-plan-2026-09-14.md) now govern implementation scope and priority. Its disposition supersedes mandatory cumulative-budget enforcement, energy-reservation admission, and a persistent objective/reference/budget subsystem proposed below, including related invariant wording and sections 9–10. Electrical protection, requested direction, and the demonstrated defect evidence remain applicable. The plan maps D01–D18, links nine open GitHub issues (five P1, four P2), and includes a conditional paper calculation. No production fix or deployment is implied.

**Solar Priority must preserve the bank's requested SOC direction while using solar for loads and useful charging. The current implementation does not yet guarantee that behavior.** It has substantial command-safety, freshness, ownership, and relay protection, but movement accounting is not a control input, some transitions discard direction protection, and voltage readiness is not equivalent to successful battery regulation.

This is the consolidated reference for the knowledge established in these reviews. It supersedes their conflicting interpretations for future design work; it does not change the production specification files, code, configuration, or deployment. The earlier reviews remain intact as evidence of the analysis. No new yacht observations were made for this document.

## 1. How to read this document

Four kinds of statement are kept distinct:

- **Owner requirement:** the yacht behavior requested on 2026-09-14, including the limits and Quattro regimes described by the owner.
- **Required design consequence:** a rule needed to achieve those requirements, such as integrating movement rather than checking only net SOC.
- **Verified implementation:** behavior established by source inspection or offline execution of the production modules. It is not automatically desired behavior.
- **Reported installation evidence:** observations recorded in repository history. These guide testing; they are not universal hardware laws or new measurements.

The invariant register uses **Owner**, **Derived**, or **Existing** for its basis. Its assessment says whether the current implementation provides it. An invariant is a required property, not a claim that the implementation already satisfies it. **D01–D18** identify findings; **E01–E16** identify evidence cases. Numeric software defaults are listed separately from requirements.

## 2. Required operating contract

The installation has a Quattro, two MPPTs, and a large 15S NMC bank with a limited cycling budget. In the described non-ESS shore configuration, useful PV supply to AC loads generally requires AC1 to be ignored so the Quattro inverts. The controller must not depend on ESS, grid export, or simultaneous shore-connected inversion as its ordinary strategy.

“Max Charge” is a destination for SOC. Battery/REC protection comes first, then the requested direction and movement limits, then solar use and minimum shore consumption. Relay wear constrains elective transfers. If available sources cannot support the loads or shore is absent, a software controller cannot guarantee flat SOC; protective actions and any unavoidable excess movement must be reported.

| Objective | Solar behavior | Shore/Quattro behavior | Battery trajectory |
| --- | --- | --- | --- |
| **HOLD: at target** | Supply loads. Curtail surplus. Occasional bounded headroom measurements may reveal hidden capacity. | Disconnect AC1 when evidence supports solar covering the complete island demand; return when necessary, with relay dwell and movement costs considered. On shore, use a stable calibrated holding command. | Approximately flat: about **1% maximum combined charge plus discharge per day**. |
| **CHARGE: below target** | Expose all safely available PV until approaching the target requires tapering. | Supply AC loads and the DC shortfall. A small positive charge bias is acceptable when needed to prevent drain; minimize discretionary shore charging. | One way up, with about **1% maximum cumulative wrong-direction discharge** over the interval to be specified. |
| **DISCHARGE: above target** | Continue supplying loads. Strong sun creates plateaus; curtail surplus that would materially recharge the bank. | Invert in darkness or insufficient sun to make the desired descent, subject to protections and load capability. | A downward staircase, with about **1% maximum cumulative wrong-direction charge** over the interval to be specified. |
| **Full-charge completion and hold** | Remain inside the electrical envelope. | In the endgame, allow the Quattro the full **61.96 V** command, or the lower applicable limit, and retain that full holding target while 100% remains requested. | Reach and hold full; do not strand the Quattro on a lower sustain floor. |

“Near target” needs a justified measurement tolerance and arrival hysteresis. The present **strictly greater than five SOC points** mode-entry threshold is not part of the owner's requirements. A meaningful one-step target change still requires directional protection.

CHARGE, DISCHARGE, HOLD, and full completion are **battery objectives**. `shore`, `probe`, `solar`, `burndown`, and `suspend` are **transport/operating states**. A transport change must not erase the battery objective, earned progress, or cumulative cost. The name `solar` does not establish that PV actually covers demand.

Solar harvesting is bounded by energy balance. Once loads are covered and the bank is not permitted to charge, surplus must be curtailed unless another legitimate load exists. Repeatedly storing that surplus and burning it later conflicts with ordinary HOLD and the cycle-preservation goal.

## 3. Electrical and accounting definitions

| Quantity | Meaning |
| --- | --- |
| `Vq` | Quattro charge-voltage command; published through `/Info/MaxChargeVoltage` and observed at VE.Bus. |
| `Vs` | Solar charge-voltage command; effective DVCC limit and each MPPT's relevant setpoint must be observed separately. |
| `L = Vs − Vq` | Solar offset/lead relative to the Quattro command. |
| `H = Vs − Vbattery` | Actual solar headroom above measured pack voltage. The owner's approximately **0.15 V** observation refers to this quantity. |
| Absolute voltage ceiling | `min(62.40 V installation ceiling, applicable fresh REC CVL)`. Lower limits always win. Unknown/stale limits invoke restrictive fallback, not continued permission at the installation maximum. |
| Normal/full endpoint | Quattro command at most **61.96 V**, also bounded by the electrical ceiling. A diagnostic solar lift has its own bounded allowance. |
| Positive native current/power | Charging the bank. Negative means discharging. Use native REC voltage and signed current for battery accounting. |

A 0.15 V configured offset does not guarantee 0.15 V headroom above the bank. Normal code often uses `Vq = target − 0.15`, `Vs = target`; a CHARGE floor normally widens its solar band to 0.30 V. Anchoring, standing lead, servo corrections, boosts, and final clamps all affect the actual pair.

Let `C` be the adopted capacity in Ah, `Q+` integrated positive current, and `Q−` the magnitude of integrated negative current, with time expressed in hours:

- Absolute movement: `100 × (Q+ + Q−) / C` percent of capacity.
- CHARGE reverse movement: `100 × Q− / C`.
- DISCHARGE reverse movement: `100 × Q+ / C`.
- This ledger's EFC convention: `(Q+ + Q−) / (2 × C)`.

For the **1440 Ah rated basis**, 1% absolute movement is **14.4 Ah combined**, approximately **835 Wh at 58 V**, and **0.005 EFC**. Conversely, **0.01 EFC is 28.8 Ah combined**, or 2% absolute movement. It could be 14.4 Ah each way, but balanced directions are not required by the formula. Charge and discharge do not cancel for this purpose.

The REC-configured capacity is reported as **1400 Ah**, whereas the driver/ledger presently uses the 1440 Ah installation basis. At 1400 Ah, 1% is 14 Ah. Capacity basis must be explicit and versioned. Wh must use the measured voltage integral; conversion from Ah using 58 V is only an approximation.

Wrong-direction movement, its later recovery, and total throughput are distinct metrics. The current ledger's overhead metric can count reverse movement plus recovery; it must not silently replace the owner's directional allowance. SOC is useful for destination and reference control, but counter resynchronization must not fabricate or erase measured energy.

HOLD explicitly has a daily allowance. Rolling 24 hours versus a calendar day, and the directional allowance's interval, remain design decisions. References may change when the owner changes the destination; already-spent daily movement must not disappear. An allowance per island entry would permit unlimited repeated reversals and is unsuitable.

Source: [native energy and overhead calculations](</workspaces/Boat/Boat NMEA/dbus-recbms/energy_accounting.py:443>), [capacity and control configuration](</workspaces/Boat/Boat NMEA/dbus-recbms/config.ini:299>).

## 4. Installation knowledge and its limits

| Evidence | What it supports | Limit on the conclusion |
| --- | --- | --- |
| Owner: Quattro reconnecting with CVL at/above the bank can surge for minutes; CVL already below the bank can limit the surge; stable connected holding behaves differently. | Prepare the voltage pair before an ordinary reconnect and test the three regimes separately. | Lowering CVL after reconnection does not test pre-applied protection. Quantitative margins and settling time still need installation validation. |
| Repository, 2026-09-02: approximately 0.6–2 kW re-absorb over about 13 minutes; `PV + 5 A` CCL cap reduced shore charging. | The existing cap has historical evidence as a brake. Preserve protection during any control-strategy changeover. | Old “no CVL stops it” wording does not disprove the owner's prepared-low-CVL regime. The current plant's shorter burst profile is not a fit to this whole event. |
| Repository, 2026-08-19: Quattro absorption voltage roughly 0.05–0.15 V above its command; equal setpoints could crowd PV out. | Quattro and MPPT commands need independent treatment and physical readback. | This is regime-specific; it does not justify a standing lead at the full endpoint or a universal voltage/SOC conversion. |
| Two documented settled points: 58.6% at 56.28 V and 62.3% at 56.65 V. | A local slope of about 0.100 V per SOC point over that measured span. | The 40% and 100% curve endpoints are not equivalent measured calibration. Historical 54.42 V was near 30% by counter despite the curve's 40% label; which reference is accurate remains unsettled. |
| Repository: around 0.15 V MPPT headroom reached tracker mode in 43–45 s and 90% of the step in 53 s; around 0.05 V reached only 5% in 126 s. | Headroom and ramp duration matter when measuring capacity. | These are historical response observations, not guaranteed response times for every irradiance/current-limit condition. Mode 2 alone does not prove unrestricted capacity. |
| Repository: Quattro reported `/Dc/0/Power` retained a roughly 25 W residual; voltage × current gave net DC exchange. Direct VE.Bus maximum-charge-current writes were reported ineffective. | Use fresh signed Quattro V×I for engine decisions and verify effective DVCC commands. | Diagnostic path names and successful writes are not evidence of physical authority. |
| Engine 4.4 history: shaded-evening model estimates 374–1381 W versus unthrottled readings 86–296 W; normalized array balance 0.06–0.35 when obstructed. | The shade veto addressed real bad admissions; stale/extrapolated capacity needs conservative treatment. | This does not establish that every fresh aggregate measurement under unequal shade is unusable. Validate any relaxation against changing shade and complete demand. |
| Installation records: DC baseline around 0.9 A/~50 W; array rated shares 0.35/0.65; evening flybridge shade. | Useful scenario parameters and independent-array modeling. | DC load is variable. At 300 W AC plus 50 W DC, the current demand model needs 413 W before uncertainty, exceeding the engine's 360 W admission threshold. |
| Solar offset uses a systemcalc Debug path; documented access-level behavior varies by firmware. | Observe effective voltage and actual charger setpoints, not just the offset property's value. | A successful offset write does not prove DVCC applies it. D04 demonstrates the current reporting/fallback gap. |

Reported bank characteristics include 15S NMC, raw REC CVL around 62.7 V, high-resolution SOC, no usual 0x35A alarm frame, and a constant raw force-charge indication. REC limit/module handling remains authoritative; a constant flag must not be promoted into new charging permission. These are installation records, not assumptions about other REC or Quattro installations.

Sources: [live calibration comments](</workspaces/Boat/Boat NMEA/dbus-recbms/config.ini:70>), [surge observations](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1716>), [shade evidence](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:34>), [access-level history](</workspaces/Boat/Boat NMEA/CLAUDE.md:183>). Historical low-SOC evidence also appears in `git show 386e125:dbus-recbms/README.md`, lines 290–296. The other review's exact 54.40 V/29.6% pair remains a reported observation, not a newly verified settled point.

## 5. Current architecture and behavior

| Component | Current responsibility |
| --- | --- |
| [solar_engine.py](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py>) | Pure five-state operating decisions, one-way overlay, capacity estimates, probe/boost intentions, engine-local timers. |
| [solar_priority.py](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_priority.py>) | Consumer observations, enable/rated-PV settings, engine execution, protocol requests. Switch instance 221. |
| [dbus_recbms.py](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py>) | Native CAN decode, critical-data health, REC protection, voltage envelope, sustain/boost primitives, publication. Battery instance 200; target switch 220. |
| [rec_policy_adapter.py](</workspaces/Boat/Boat NMEA/dbus-recbms/rec_policy_adapter.py>) | Fresh source observations, demand/accounting diagnostics, intent application, protocol and transfer integration. |
| [policy_contract.py](</workspaces/Boat/Boat NMEA/dbus-recbms/policy_contract.py>) | Protocol v2, generation/request IDs/lease, durable relay supervisor and feedback. REC is the actuator owner. |
| [control_inputs.py](</workspaces/Boat/Boat NMEA/dbus-recbms/control_inputs.py>) | Per-source timestamps/epochs, validation, coherent observations, elapsed-time means, complete-demand model. |
| [energy_accounting.py](</workspaces/Boat/Boat NMEA/dbus-recbms/energy_accounting.py>) | Native Ah/Wh, direction/overhead/reference records, gaps, persisted accounting. Normal engine decisions do not consume its energy allowances. |
| [control_watchdog.py](</workspaces/Boat/Boat NMEA/dbus-recbms/control_watchdog.py>) | Detect stalled REC execution, terminate/reap before replacement. It does not write actuators. |
| [solar_priority_plant.py](</workspaces/Boat/Boat NMEA/solar_priority_plant.py>) | Offline coupling of actual controller modules to provisional battery, charger, DVCC, and MPPT response models. |

The retained architecture has useful boundaries: one relay authority, native REC safety, fresh observations, ordered/leased requests, persisted departure reservations, decimal command rounding, and voltage sequencing. These must survive algorithm changes.

Four distinctions explain several defects:

1. **Protocol validity versus decision validity.** A fresh valid lease can carry an inappropriate request when engine inputs are missing; D01 is not a lease-timeout problem.
2. **Electrical envelope versus requested regulation.** Setpoints below the absolute ceiling can still fail to apply the requested lead or suppress reconnect charging. Continuing within the envelope is not proof of the required battery behavior.
3. **SOC reference versus voltage command.** Clipping `held_eff` to the slider does not universally clamp the voltage anchor to the slider curve. A one-point anchor step is not a cumulative reversal limit.
4. **Telemetry versus permission.** A recorded energy budget, reserve function, or DC-bus demand estimate does not govern operation unless the control path consumes it.

Sustain floors follow upward SOC progress attributed to PV; ceilings follow downward SOC progress. Voltage anchors use measured pack voltage with approximately 3 mOhm I×R compensation and slower re-anchoring/servo rules. The ceiling servo currently reacts to inferred Quattro charging, not all positive battery current. Normal HOLD releases the sustain primitive, leaving ordinary slider/lead and harvest/burn behavior active.

The final command boundary clamps voltages and applies charge bans. System effective voltage must match the requested pair for readiness; individual charger readbacks may be lower because of device limits. New departure requires readiness; an islanded return can currently use the looser safe-envelope test. Lower-than-requested setpoints can be electrically safe without proving useful PV headroom or a prepared Quattro reconnect.

### Active defaults and mechanisms — not owner invariants

| Area | Current setting/behavior | Qualification |
| --- | --- | --- |
| Target setting | 40–100%, step 5; persisted in localsettings. | SOC destination; exactly five points does not enter one-way operation. |
| Direction overlay | Enter at delta >5 or <−5; release within 1 point. Full threshold defaults to 100. | Conflicts with meaningful smaller changes; configurable full threshold conflicts with hardcoded consumer mode in D11. |
| Ordinary voltage | 0.15 V standing lead below full; floor solar band 0.30 V; normal cap 61.96 V. | Actual headroom depends on pack and anchor. Installation maximum input is clamped to 62.40; excessive normal `cvl_max` is rejected. |
| Floor/ceiling | 1-point re-anchor step; 0.02 V servo step every 30 s, bounds +0.5/−2.0 V; nominal 120 s hold expiry. | Adapter refreshes retained holds after lost lease. Anchor progress and servo response do not guarantee monotonic physical SOC. |
| Sustain settling | Taper/re-anchor and 300 s dusk rules supplement the one-point step. | Wall-time based; exact anchor conditions matter. Closing the floor band at target still leaves ordinary standing-lead subtraction. |
| Sustain CCL | `min(raw CCL, measured PV current + 5 A)` while held; lifted during boost or absent PV-current reading. | Continuous today; can restrict solar ramps. Removing it requires replacement surge protection. |
| Measurement boost | Maximum +0.30 V, nominal 120 s; sample window 75–105 s; max cell strictly <4.05 V, cell temperatures 5–45°C, pack headroom ≥0.10 V, electrical caps apply. | Distinct from the 90 s island probe. Nominal duration is not a hard elapsed-time bound today. Ceiling holds refuse boosts. |
| Solar admission | `max(100 W, 1.2 × max(60 s, 300 s AC-load means))`; readiness 30 s; quiet Quattro ≤100 W; shade balance ≥0.4 when judged. | AC-only demand is incomplete. DISCHARGE bypasses ordinary solar-sufficiency requirements because descent is intentional. |
| Probe | 90 s ramp; last 15 s evaluation; battery mean above −50 W passes. | Timer starts before physical departure. CHARGE has a direct-entry shortcut; the code's tracking predicate is not strict proof that both arrays are unrestricted. |
| Solar exits | Ordinary deficit: 90 s mean below −50 W with 15 s trigger; CHARGE uses its 180 s mean. Entry-relative SOC drift guard 2 points. | Small persistent deficits pass. CHARGE surge exit is suppressed; its stall branch can enter burndown. DISCHARGE intentionally suppresses ordinary deficit/drift exits. |
| SOC gates | Ordinary minimum 40%, emergency 30%; CHARGE minimum 25% with conditional emergency exception. | Existing safety/operating defaults, not a proof that every admitted interval charges the bank. |
| Large load | Suspend at ≥1 kW for 3 s; resume after 10 s within 200 W of prior baseline; maximum 20 min. | Transfers still use the common supervisor; demand and battery objective remain relevant. |
| Capacity model | Captures smoothed 0.3, trusted 15 min, faded by 90 min; idle Voc EMA 10 min; recent capture outranks model. | Stale evidence, shade, and current-limited tracking need explicit confidence. Day Voc gate 55 V; exploratory gate 65 V. |
| Engine retry | 300 s cooldown; failed-probe backoff doubles to 1 h; fresh evidence may clear backoff; hard lockouts 1 h. | Engine-local state resets on rebuild. Persisted supervisor backoff starts at 900 s but is unwired. |
| Relay supervisor | Connected dwell 300 s; at most 3 departures/hour and 12/day; feedback timeout 30 s; fault lockout 1 h. | Attempts reserved before writes; physical edges separately observed. Budgets age by observed monotonic uptime, not wall-clock jumps or restart downtime. |
| Protocol | v2; current generation/target; increasing request ID; lease range 1–120 s; consumer sends 15 s leases. OFF/COMPLETE_FULL require sustain release. | Rejected requests do not replace a still-valid lease. Expired authority cannot authorize departure. |
| Observation freshness | Consumer 20 s, root reads every 5 s; REC source registry 10 s, root reads every 1 s; independent REC critical groups, 60 s live timeout. | A successful read of unchanged data refreshes it. Cached lookup, unrelated CAN traffic, and late replies from old epochs do not. |
| Pack/permission guard | At pack voltage ≥safe ceiling −0.10 V, or REC charge prohibition, CCL is zero and the command guard removes charging headroom below the pack. | Unknown measurements cannot justify new headroom. Setpoint bounds do not establish instantaneous physical response. |
| REC fallback | Beyond 60 s live freshness: ALERT through 120 s, RESTRICT through 300 s, then SURVIVAL. CCL 0 throughout fallback; DCL 100/30/15 A and DVL 52/53/54 V; startup grace 180 s. | Known lower DCL remains restrictive. Fresh Quattro voltage can substitute for displayed pack voltage during failure, not authorize charging or an anchor. |
| Watchdog | Successful-tick heartbeat; 8 s timeout, 30 s startup allowance; bounded TERM then KILL/reap before replacement. | Separate from REC data-freshness grace. Software scheduling and charger response do not establish an instantaneous physical energy bound. |
| Accounting uncertainty | Sample gaps over 10 s, invalid data, restarts, or persistence failure make relevant history uncertain; the ledger uses a 24 h uncertainty window. | Measured totals are lower bounds after gaps. Current ordinary operation does not use this as an energy veto. |
| Retired mechanisms | Equalization disabled; old ten-second lead verifier unused; energy reservations/reverse-event closure not integrated into ordinary control. | Old configuration comments and architecture claims must not be treated as active behavior. |

Requests contain exactly `version`, `generation`, `request_id`, `mode`, `target_soc`, `transfer_intent`, `requested_limits`, and `lease_s`. Sustain is release/floor/ceiling (0/1/2); an optional boost is an edge, not a continuously renewed target. Unknown fields and incompatible modes are rejected; target agreement is checked within 0.001 SOC point. Ownership handover prevents legacy direct sustain/boost writes after the consumer takes over.

Per-charger validity also tracks voltage-sense selection: once an MPPT has exposed `VoltageSenseActive`, a missing/stale selector cannot silently be treated as a healthy report. Selected battery/BMS identity and instance must be consistent and fresh. These observation rules support, but do not replace, correct handling of missing decision inputs.

Primary references: [engine defaults](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:11>), [sustain and boost configuration](</workspaces/Boat/Boat NMEA/dbus-recbms/config.ini:146>), [relay/accounting configuration](</workspaces/Boat/Boat NMEA/dbus-recbms/config.ini:307>), [voltage checks](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1090>), [transfer readiness](</workspaces/Boat/Boat NMEA/dbus-recbms/rec_policy_adapter.py:453>).

## 6. Consolidated invariant register

“Implemented” below describes reviewed software behavior, not certified physical response. “Partial” means useful mechanisms exist but the end-to-end property is not established. “Gap” means a demonstrated contradiction or missing control. “Decision” marks an unresolved policy choice or calibration, without inventing an answer.

### Safety, topology, and authority

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP01 | REC/battery protection and the requested direction take precedence over discretionary PV harvesting and shore savings. | Owner; Partial — D01–D09. |
| SP02 | Operate within this yacht's two-MPPT, Quattro, shore-connected topology without depending on ESS or export. | Owner; architecture retained. |
| SP03 | Treat Max Charge as a destination, minimizing shore energy except where protection and the defined full-completion policy require it. | Owner; Gap — D05, D12. |
| SP04 | Use consistent signed native battery current/power: positive charge, negative discharge. | Existing; implemented metering foundation. |
| SP05 | Both voltage commands and every intermediate combination obey the installation and applicable REC ceiling in all modes and transitions. | Owner/Existing; command guards implemented; physical overshoot margin remains uncalibrated. |
| SP06 | Ordinary/full Quattro command is at most 61.96 V, with lower REC/installation limits winning; diagnostic solar lift is separately bounded. | Owner/Existing; implemented command caps. |
| SP07 | Charge/discharge permissions and module availability remain authoritative; solar optimization cannot override REC prohibitions. | Existing; implemented boundary. |
| SP08 | Limits, measurements, SOC, cells, temperature, and module groups each require fresh valid REC data. | Existing; implemented independent receipt clocks. |
| SP09 | Only the selected intended REC and native valid measurements establish control references; fallback display substitutions cannot do so. | Existing; implemented boundary; preserve during repairs. |
| SP10 | Base/offset sequencing, delayed writes, and rounding cannot produce a command above the ceiling. | Existing; implemented sequencing and downward rounding. |
| SP11 | REC alone executes voltage/current/relay actuators; the consumer submits leased intentions; competing legacy writers cannot run. | Existing; implemented ownership boundary. |
| SP12 | If source loss makes the requested trajectory physically impossible, protect the bank and record unavoidable deviation rather than claim the movement guarantee held. | Derived; Partial — explicit movement exception handling remains necessary. |

### Voltage authority and measurement

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP13 | Distinguish Quattro CVL, solar CVL, offset, and actual headroom above the bank. | Owner/Derived; Partial — D04 telemetry gap. |
| SP14 | When unrestricted PV is wanted, use about 0.15 V headroom as the installation starting point, subject to caps and observed MPPT response. | Owner; Decision — tune against installed behavior. |
| SP15 | Curtail PV once loads and permitted charging are satisfied; surplus energy is not a reason to create unwanted battery cycles. | Owner/Derived; Gap — D03, D06, D07. |
| SP16 | Ordinary levers are CVL, solar CVL/lead, and Ignore AC1. CCL remains a safety limit and may provide bounded temporary surge damping. | Owner; Gap — D09; staged changeover required. |
| SP17 | Distinguish requested values, write acknowledgment, property readback, effective DVCC voltage, charger setpoints, and physical power response. | Derived; Partial — D02, D04. |
| SP18 | Readiness proves the required command application; a lower safe setpoint or safe absolute envelope alone does not prove solar headroom or reconnect protection. | Derived; Partial — D02, D04. |
| SP19 | Failure to apply a required lead/limit is surfaced truthfully with an actionable fault; requested lead cannot be reported as verified actual lead. | Derived; Gap — D04. |
| SP20 | Safety/operating durations use monotonic elapsed time, including boost, sustain, servo, taper, dusk, and transfer timing. | Derived; Partial — D10. |
| SP21 | A measurement boost is a bounded edge request; renewals do not extend it; sampling follows actual usable headroom/ramp and aims for the owner's roughly 90 s measurement. | Owner/Derived; Partial — D10; distinguish island probe. |
| SP22 | Charge/discharge during probing, ramp, settling, and recovery counts toward the applicable movement allowance. | Derived; accounting exists, control enforcement missing — D03. |

### HOLD

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP23 | Hold an explicit stable battery reference at the destination, with justified sensor tolerance. | Owner/Derived; Gap — ordinary HOLD releases sustain. |
| SP24 | Leave shore only when fresh evidence supports PV covering complete island demand; return when continued operation is unsustainable. | Owner; Partial — D03, D08. |
| SP25 | Elective departure/return choices respect relay wear and the movement cost of both the trial and recovery. | Owner/Derived; relay limits implemented; energy admission missing. |
| SP26 | Ordinary HOLD does not deliberately fill and burn a voltage band as an energy-harvesting cycle. | Owner/Derived; Gap — harvest/stall paths remain. |
| SP27 | HOLD counts both directions and stays near 1% total absolute movement per day; matching initial/final SOC does not erase wear. | Owner; Gap — D03. |
| SP28 | On stable shore, regulate DC support and holding voltage without repeatedly provoking charging transitions. | Owner; Partial — holding calibration and arrival behavior need validation. |

### CHARGE

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP29 | Every meaningful upward target difference selects the upward objective, including a single slider step and restart near target. | Owner/Derived; Gap — D05. |
| SP30 | Use all safely available PV for useful ascent until arrival tapering is necessary; avoid unnecessary solar restriction by shore support. | Owner; Partial — D09, D15. |
| SP31 | Aim for nonnegative battery balance, with only the shore support and small positive bias needed to prevent drain. | Owner; Gap — D01–D03, D05, D08. |
| SP32 | Integrate negative battery movement cumulatively and keep it near the 1% reverse allowance over the defined interval. | Owner; Gap — D03; interval Decision. |
| SP33 | Preserve earned upward progress across transport changes/restarts; a failed island interval cannot silently establish a lower acceptable floor. | Derived; Partial — references recorded but not governing regulation. |
| SP34 | No CHARGE entry path deliberately burns battery energy, including ceiling stall. | Owner/Derived; Gap — D06. |
| SP35 | Missing decision data cannot convert CHARGE into a permissive HOLD/release with a fresh lease. | Derived; Gap — D01. |
| SP36 | Arrive smoothly at target and transition to HOLD without overshoot followed by deliberate corrective descent. | Derived; Partial — D05 and arrival regulation. |

### DISCHARGE

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP37 | Every meaningful downward target change selects the downward objective, including a single slider step. | Owner/Derived; Gap — D05. |
| SP38 | Continue using PV for daytime loads; strong sun produces plateaus, not recharge steps. | Owner; Partial — D07. |
| SP39 | Use inversion during darkness/insufficient sun to descend when permitted; intended discharge alone is not a reason to reconnect. | Owner/Existing; implemented overlay in part; arrival and faults still govern. |
| SP40 | Positive bank energy from either Quattro or PV is reverse movement; source attribution cannot exempt solar recharge. | Owner; Gap in control — D07. |
| SP41 | Bound cumulative positive battery movement near 1% over the defined interval; repeated sub-one-point refills still add up. | Owner; Gap — D03, D07; interval Decision. |
| SP42 | The voltage ceiling follows earned downward progress without permitting refill to a stale higher anchor. | Derived; Gap — D07. |
| SP43 | Stop the descent at the destination and enter HOLD without overshooting and then recharging to undo it. | Owner/Derived; Partial — arrival behavior unproven. |

### Full-charge completion

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP44 | In the full-charge endgame, permit the full Quattro 61.96 V endpoint or the lower electrical limit; standing lead cannot permanently reduce it. | Owner; command behavior implemented. |
| SP45 | Retain the full holding target after completion while 100% remains requested and REC permits it. | Owner; Partial — command exists, explicit completion/hold behavior unresolved. |
| SP46 | Define endgame entry/exit and whether 100% also authorizes immediate shore bulk from any SOC. | Derived; Decision — D12; do not assume the broader exception. |
| SP47 | Configured engine decisions and protocol modes must agree, including full-target sustain restrictions. | Derived; Gap — D11. |
| SP48 | Distinguish commanding 61.96 V from observing/calibrating REC's 100% SOC endpoint. | Existing evidence limit; unverified installation endpoint — D15, D17. |

### Demand and capacity evidence

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP49 | Judge sufficiency in DC-bus watts: AC conversion, inverter idle/losses, external DC demand, and uncertainty. | Derived; Gap — D08. |
| SP50 | Quattro net DC exchange is fresh signed V×I, not reported residual power or subtraction of mixed-age totals. | Existing; implemented engine input. |
| SP51 | Successful per-source observations refresh even constant values; cache reads do not; stale/removed sources and old-epoch replies lose authority. | Existing; implemented input infrastructure. |
| SP52 | Throttled yield is a lower bound; capacity evidence requires applicable limits and ramp state to be understood. | Derived; Partial — tracking mode/capture is not complete proof. |
| SP53 | Treat arrays independently; do not infer a shaded/missing array's production from its partner's rated share. | Derived; Partial — model/extrapolation requires confidence bounds. |
| SP54 | Shading protections distinguish unreliable extrapolation from fresh sustained aggregate production; validate any relaxation against historical bad admissions. | Derived; current conservative veto retained pending evidence. |
| SP55 | Prefer fresh existing capacity evidence and bounded on-shore measurement before spending an elective relay departure. | Owner/Derived; Partial — direct-entry shortcut exists primarily for CHARGE. |

### Transfers, leases, and failure recovery

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP56 | Distinguish Ignore AC1 command, physical acceptance, shore availability, and acceptance of another AC input. | Derived; Partial — D14; “not AC1” alone is not proof of inverter-only operation. |
| SP57 | Before ordinary reconnection, apply an appropriate lower Quattro CVL while preserving required solar CVL inside the envelope. | Owner/Derived; Gap — D02. |
| SP58 | Confirm preparation before closure and retain protection until actual charger settling justifies relaxing it. | Derived; Gap — D02; calibrated settling needed. |
| SP59 | Ordinary departures respect persistent dwell/rate limits across all purposes and restarts; attempts and actual edges are accounted separately. | Existing; implemented supervisor. |
| SP60 | Actual probe outcomes drive persistent backoff; restart or fresh model estimates cannot accidentally erase required protection. | Derived/Existing; Gap — D16. |
| SP61 | Protective returns override elective wear limits; elective admission includes expected return delay and movement cost. | Derived; return priority implemented; energy admission missing. |
| SP62 | Timestamp request, acknowledgment, physical transfer, command application, and ramp separately when judging a probe or return. | Derived; Partial — probe timer starts before physical departure. |
| SP63 | Absent shore, failed command, and delayed physical acceptance receive distinct diagnostics/recovery behavior. | Derived; Gap — D14. |
| SP64 | Requests require current protocol/generation/target, increasing IDs, bounded monotonic lease, and verified ownership; stale authority cannot authorize departure. | Existing; implemented contract. |
| SP65 | Lost authority returns toward shore while retaining applicable battery protection; target changes and operator override during that condition have explicit semantics. | Derived/Existing; Partial/Decision — D13. |
| SP66 | Loss of critical decision evidence removes elective permissions and preserves/tightens the objective; a restrictive fallback cannot be represented as a healthy normal request. | Derived; Gap — D01. |
| SP67 | Optional MPPT report loss alone need not terminate an otherwise proven safe island; known unsafe limits or unverified increases cannot be ignored. | Existing; implemented boundary distinction; preserve while fixing D04. |
| SP68 | A stalled REC executor is terminated and reaped before replacement; the watchdog does not become another actuator writer. | Existing; implemented process boundary. |

### Accounting, operations, and proof

| ID | Required property | Basis; current assessment |
| --- | --- | --- |
| SP69 | Preserve calibration, target/enable settings, newest ledger, and compatible service versions during deployment/rollback. | Existing; documented tooling; engine-local state is still lost. |
| SP70 | Integrate native timestamped current/voltage with separate signs and zero crossings; gaps and SOC resync cannot invent energy. | Existing; implemented ledger foundation. |
| SP71 | Publish explicit Ah/Wh, percentage capacity basis, absolute movement, reverse movement, recovery, and EFC units. | Derived; Partial — D03 and existing ambiguous documentation. |
| SP72 | Daily and directional costs survive mode/transport changes, toggles, and restarts; new target references cannot erase already-spent daily cost. | Derived; Partial — ledger foundation exists, control integration incomplete. |
| SP73 | Unknown history is reported as a lower bound, not proof of spare allowance; recovery after gaps must have an explicit policy. | Derived/Existing; uncertainty recorded; future enforcement must avoid accidental permanent lockout. |
| SP74 | Operational telemetry distinguishes objective, transport, requested/verified/actual limits, protection/fault reason, and movement spent; display consumers do not bypass actuator ownership. | Derived/Existing; Partial — D04 and misleading readiness/settled labels. |
| SP75 | Acceptance checks actual battery integrals and physical actuator response across representative scenarios; passing a state test or simulator endpoint does not certify yacht behavior. | Derived; offline evidence exists, installed trajectory proof outstanding. |

## 7. Consolidated findings

Priority **P1** means a demonstrated path contradicts the battery contract or materially obscures loss of control. **P2** means a resilience/configuration/design gap requiring resolution. These are review priorities, not assertions that the electrical ceiling has been exceeded on the boat.

**D01 — P1: Missing decision input can release the charge floor under a valid lease.**

The engine clears one-way mode on missing inputs. The consumer's protocol-readiness test does not cover every input the engine needs, so it can submit a fresh HOLD request with sustain release. Removing Quattro DC-current telemetry during night CHARGE changed the published command from about 56.42 V/5 A to 59.34 V/200 A; the plant showed 4.94 kW battery charging after 30 s. REC remained valid. This requires objective-preserving missing-data handling, not merely a shorter lease. Sending `protect` must also preserve the intended limits; transfer intent alone is not a substitute for that. **E02.** Sources: [missing-input handling](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:477>), [consumer request mapping](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_priority.py:634>).

**D02 — P1: Reconnect and shutdown apply protection too late.**

CHARGE requests a floor only after actual shore feedback. Ordinary islanded return is admitted on general envelope safety, allowing closure before low-CVL/current protection is established. In the plant, at closure the pack was 56.41 V, Quattro CVL 59.34 V, and CCL 200 A; applied CCL reduction followed about 3 s later and CVL reduction about 5 s later. REC shutdown likewise invokes the relay-return path before zero-current/low-voltage commands. Establish a preparation stage, confirm application, reconnect, and observe settling. Define emergency return separately. **E03.** Sources: [floor request](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:966>), [return readiness](</workspaces/Boat/Boat NMEA/dbus-recbms/rec_policy_adapter.py:471>), [shutdown order](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1828>).

**D03 — P1: Cumulative movement does not govern operation.**

A steady −49 W stays below the 50 W mean-deficit threshold and can lose 1.448 SOC points in 24 h without reaching the two-point exit guard. Ledger budgets and earned references are diagnostic rather than controlling admission/return/regulation. The configured 0.01 EFC also permits twice the literal HOLD absolute-movement allowance, even if wired in. Define the allowance and reserve sufficient cost for response/return before it is exhausted. Do not simply add a per-entry energy counter or reset progress on reconnection. **E04, E11.** Sources: [deficit/exit rules](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:872>), [budget calculation](</workspaces/Boat/Boat NMEA/dbus-recbms/energy_accounting.py:487>).

**D04 — P1: Ignored solar offset can disable charging silently or leave lead telemetry false.**

The old lead verifier is unused. Current handling allows an electrically safe envelope to count as maintained even when the exact pair is not applied. From startup, ignored offset left CCL at 0 A for all sources without a LeadFault or InternalFailure warning. After successful verification, loss of the offset retained 5 A CCL in the replay and reported SolarLead 0.30 V despite actual offset 0 V. Both cases correctly had `Voltage/Ready = 0` and a status string, so the problem is incomplete alarm/semantic handling, not absence of every diagnostic. Distinguish a brief safe update from persistent failure to apply regulation; make the failure actionable while retaining the battery objective. Blindly restoring a full-slider fallback could create unwanted bulk charging. **E14, E15.** Sources: [envelope versus exact application](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1090>), [lead publication](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1815>), [CCL and alarm decisions](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:2133>).

**D05 — P1: The five-point entry gap discards meaningful target direction.**

New ±3 or exactly ±5 point changes select no one-way overlay; restart with fewer than five points remaining does the same. A 62% → 65% night plant run selected HOLD and put 1.443 kWh into the battery from shore over 20 minutes. That is not established as the owner's allowed small anti-drain bias. Replace coarse eligibility with justified tolerance/hysteresis and arrival control. **E05, E06.** Source: [direction selection](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:493>).

**D06 — P1: CHARGE can intentionally enter ceiling-stall burndown.**

Shore-side burn paths exclude one-way charging, but the solar-state stall branch does not. A focused replay retained `oneway = charge` while entering burndown after 105 s at −100 W. Another review reproduced the same path with a different setup/time. Guard every burn entry; this small repair does not replace the cumulative-deficit fix. **E07.** Source: [stall branch](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:932>).

**D07 — P1: DISCHARGE can repeatedly refill from solar to a stale ceiling.**

The SOC reference moves downward, but the voltage anchor lags by a one-point step or other re-anchor conditions. The servo ignores PV-attributed charging. Alternating hourly darkness/sun produced 17.684 Ah positive charge in eight hours, **1.228%** reverse movement, despite net descent and just one shore departure. Responding to positive battery charging from any source is a useful repair direction; noise, anchor timing, response delay, and cumulative allowance still require control. **E08.** Source: [anchor/servo/ceiling logic](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1527>).

**D08 — P1: Departure demand omits DC load and explicit inverter costs.**

The engine uses AC load ×1.2. The adapter computes complete DC-bus demand but does not feed it into that threshold. A replay entered solar at 400 W PV against 300 W AC and 300 W DC, although the model requires about 663 W before uncertainty. Even with only the yacht's reported 50 W DC baseline, 300 W AC implies **413 W** demand and **443 W** with the configured uncertainty, versus the engine's 360 W. The omission is not automatically absorbed by the 20% margin. **E09, E16.** Sources: [engine need](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:567>), [demand model](</workspaces/Boat/Boat NMEA/dbus-recbms/control_inputs.py:167>).

**D09 — P1 design gap: CCL is a continuous sustain actuator; replacement needs a controlled changeover.**

`PV current + 5 A` continuously limits combined charging, can restrict a PV ramp, and disappears during boosts or missing PV-current telemetry. Missing PV telemetry also removes the servo's shore-charge inference. This conflicts with the requested normal actuator policy, but the cap has historical surge-braking evidence. Keep effective protection until prepared-CVL reconnection is validated; then use CCL only for REC safety and any justified bounded damping. No immediate cap removal is implied by this review. Source: [sustain current cap](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1716>).

**D10 — P2: Wall-clock primitive timing defeats bounded measurement duration.**

Boost/sustain expiry and related servo/taper/dusk timers use wall time. A −1 h clock change kept an accepted boost active 132 monotonic seconds later with 3588 s remaining. The 120 s boost/window also differs from the owner's roughly 90 s measurement; the separate island probe is already nominally 90 s. Use monotonic timing and a sampling window tied to actual usable headroom. **E10.** Sources: [request clock](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1311>), [expiry](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1781>).

**D11 — P2: Non-default full threshold produces incompatible protocol requests.**

With `ONEWAY_FULL_PCT = 0` and target 100%, the engine can request CHARGE/floor while the consumer hardcodes `COMPLETE_FULL`, whose contract requires release. The reproduced request is rejected with `disabled/full mode must release sustain`; repeated such requests cannot renew authority. Validate or unify engine/consumer full-mode semantics. **E12.** Sources: [consumer mode](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_priority.py:645>), [contract](</workspaces/Boat/Boat NMEA/dbus-recbms/policy_contract.py:65>).

**D12 — P2 / decision: Full-charge override is broader than the stated endgame exception.**

Default target 100% disables one-way operation from any starting SOC and releases sustain immediately. There is no explicit endgame/completed-state progression; ordinary transport decisions remain available. The full 61.96 V command without standing lead is correct for completion, but immediate maximum shore ascent is an additional policy choice. Define it rather than assuming either interpretation is already agreed. Source: [full override](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_engine.py:500>).

**D13 — P2 / decision: Lost-owner hold retention has undefined operator semantics.**

The adapter refreshes an existing floor/ceiling indefinitely after lease loss and can reconstruct a compatible reference after restart. This avoids sudden permissive slider charging, but the operator's changed target/disable/recovery expectations need definition. The slider is not wholly ignored—it still participates in held-SOC and target calculations—but normal destination movement may be inhibited. Primitive expiry is not an independent dead-owner release. Source: [retained sustain](</workspaces/Boat/Boat NMEA/dbus-recbms/rec_policy_adapter.py:356>).

**D14 — P2: Unavailable shore is classified as failed relay feedback.**

The supervisor does not separately model shore availability. A successful connect command followed by continued disconnected feedback generated a timeout after 31 s and a 3600 s lockout. Distinguish absent shore from refused commands or failed transfer; detect actual available inputs before applying any hold that would unnecessarily constrain PV while still islanded. Firmware/path availability requires verification. **E13.** Source: [feedback timeout](</workspaces/Boat/Boat NMEA/dbus-recbms/policy_contract.py:233>).

**D15 — P2: Voltage/SOC calibration is too limited for universal target guarantees.**

The measured mid-span does not validate the extrapolated 40% point or the 61.96 V → 100% outcome. Low-target HOLD and solar target taper can depend on the curve. Voltage anchoring helps but is affected by loaded/charging voltage and servo behavior. Obtain settled observations and model uncertainty; do not extrapolate a local V/SOC slope into a fixed daily movement claim. Source: [calibration](</workspaces/Boat/Boat NMEA/dbus-recbms/config.ini:70>).

**D16 — P2: Durable failed-probe and energy lifecycle paths are unwired.**

`TransferSupervisor.failed_probe()` has no active outcome caller; engine-local backoff can reset on restart or clear with evidence. Durable dwell and departure quotas still apply. `reserve`, `close_reservation`, and `end_reverse_event` are not integrated into normal operation, so lifecycle telemetry such as reverse-event accumulation can mislead. Connect mechanisms only to an explicit control/accounting design, and identify diagnostics clearly. Sources: [persistent probe backoff](</workspaces/Boat/Boat NMEA/dbus-recbms/policy_contract.py:201>), [energy lifecycle](</workspaces/Boat/Boat NMEA/dbus-recbms/energy_accounting.py:481>).

**D17 — P2 validation gap: The plant cannot certify the owner's reconnect or daily-wear behavior.**

The fixture forces an adversarial reconnect burst even when Quattro CVL is below the pack and assumes a 61.96 V full-SOC endpoint plus provisional response profiles. It demonstrates ordering and logic defects; it cannot establish the benefit of pre-applied low CVL. Preserve the adverse profile and add calibrated distinctions between prepared return, unprepared return, and stable shore holding. The recorded 3.0 release had not yet observed an automatic boat departure/return. Source: [plant assumptions](</workspaces/Boat/Boat NMEA/solar_priority_plant.py:49>), [release evidence](</workspaces/Boat/Boat NMEA/reviews/solar-priority-restoration-2026-09-13.md>).

**D18 — P2: Configuration and documentation describe inactive guarantees.**

`solar_priority.ini` contains only comments and no actual `[engine]` header; uncommenting a suggested key fails parsing. Old claims about ten-second lead fallback, dead-owner release, maintenance, exact charger equality, and enforced energy budgets do not describe the active path. Fix section structure without losing calibration and replace stale claims with active control/diagnostic distinctions. The register above also corrects the false claims that the slider curve caps every held command, every one-way burn path is disabled, or 0.01 EFC equals 1% absolute movement. Sources: [engine ini](</workspaces/Boat/Boat NMEA/dbus-recbms/solar_priority.ini>), [retired lead verifier](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1738>), [voltage-anchor target](</workspaces/Boat/Boat NMEA/dbus-recbms/dbus_recbms.py:1469>).

## 8. Evidence ledger

These are **offline** results obtained during the review/reconciliation work using production modules and the existing fixture. Numerical energy/response results are synthetic unless explicitly identified as historical installation observations in section 4. Consolidating this document did not rerun unchanged suites or create new boat evidence.

| ID | Scenario and setup | Recorded result | Scope of proof |
| --- | --- | --- | --- |
| E01 | `python3 test_solar_priority.py`; separately `python3 -m unittest test_solar_priority_plant`. | 112 engine assertions +180 unit/integration tests passed; four workspace fixture tests passed. | Regression compatibility, not proof of the new owner contract. The separate four-test runner is not included in the main acceptance command. |
| E02 | Night CHARGE target 80%; remove Quattro `/Dc/0/Current` while REC remains valid. | Fresh HOLD/release; command 59.34 V/200 A; 4.94 kW battery charging at +30 s. | Demonstrates permissive missing-input mapping; charging magnitude is fixture-dependent. |
| E03 | CHARGE island loses sun and returns. | At physical return: pack 56.41 V, Quattro CVL 59.34 V, CCL 200 A; reductions arrive approximately +3 s/+5 s. | Confirms ordering gap; timing depends on configured bus/charger response. |
| E04 | Fresh engine inputs, steady −49 W, 24 h at assumed 56.4 V/1440 Ah; targets 60% and 80%, start 60%. | Both remain solar; final SOC 58.552%, loss 1.448 points. | Counterexample independent of the plant's reconnect model. |
| E05 | New target gaps ±3 and exactly ±5 points. | No one-way overlay; sustain release. ±6 enters the corresponding mode. | Direct entry-threshold proof. |
| E06 | Plant starts 62%, target 65%, no sun, 20 min. | HOLD; 1.443 kWh charged into battery; final SOC 63.759%. | Consequence of the mode gap in the fixture. |
| E07 | Engine already CHARGE/solar at 78%, target 80%, −100 W, pack 59.47 V, CVL 59.49 V. | CHARGE → burndown after 105 s. | Reachable stall path; another setup reproduced the same transition later. |
| E08 | Plant starts 80%, target 60%; alternate one hour dark/one hour 1400 W sun for eight hours. | 17.684 Ah charge, 1.228% reverse; one sun interval 78.862% →79.255%; final 78.970%; one departure. | Repeated refill exceeds the directional goal despite net descent. |
| E09 | Engine admission: 400 W PV, 300 W AC, 300 W DC. | Direct solar entry at 300 s, engine need 360 W; complete model need 663 W before uncertainty. | Incomplete admission formula. |
| E10 | Accepted boost, then wall clock moved backward one hour. | Still active at 132 monotonic seconds, reporting 3588 s left. | Nominal expiry is not a monotonic bound. |
| E11 | Nominal HOLD plant day: 12 h sun then 12 h dark, start/target 60%. | 0.895% total movement; final SOC 59.300%. | This case meets the approximate daily allowance; no universal 1.5–4.5% daily claim follows from band width. |
| E12 | Production engine/contract at target 100%; compare full threshold 100 and 0. | Default release accepted; threshold 0 floor rejected. | Configuration/protocol mismatch. |
| E13 | Successful connect command with persistent disconnected feedback. | `relay feedback timeout` at 31 s; lockout for another 3600 s. | Supervisor cannot distinguish this observation from unavailable shore. |
| E14 | Plant ignores applied offset from startup; target 80%, night; run 180 s. | CCL 0 A; Ready 0; LeadFault empty; InternalFailure 0; SolarLead 0.30 V while actual offset 0 V. | Startup inhibition and misleading lead/alarm semantics. |
| E15 | Same setup, first verify offset for 180 s, then force applied offset to zero for 180 s. | CCL retained at 5 A; Ready 0; LeadFault empty; InternalFailure 0; SolarLead 0.30 V while actual offset 0 V. | Safe-envelope continuation does not prove lead application. |
| E16 | Production DemandModel defaults: 300 W AC, 50 W DC. | Island demand 413.33 W; admission with uncertainty 443.33 W; engine threshold 360 W. | Omitted DC/inverter costs matter even with the reported modest DC baseline. |

Existing suites and review replays establish defects and retained boundaries. They do not measure the yacht's 24–48-hour battery trajectory, determine its true usable capacity, or calibrate physical surge/response bounds. The older 1.5–4.5% daily movement estimate describes possible burn scenarios, not a verified universal rate or a proven worst-case bound.

## 9. Design direction and remaining decisions

**Historical design proposals:** use the [repair plan](solar-priority-repair-plan-2026-09-14.md) for the owner's subsequent simplicity constraint and current work scope. In particular, cumulative-energy permission is no longer a required design direction.

The preferred design separates a persistent **battery objective/reference/budget** from **transport state**, with REC retaining final actuator authority. Regulation should use native battery balance and actual actuator response. Mode names, coarse voltage bands, and energy telemetry alone cannot enforce direction.

For an ordinary reconnect, prepare Quattro CVL below the bank as needed, preserve separately required PV headroom within the ceiling, confirm the relevant device commands, reconnect, and hold protection until settling is observed. If shore is absent, avoid a stale floor that needlessly suppresses useful PV. Emergency return has a defined restrictive fallback when normal preparation cannot finish. Treat generic envelope safety, full readiness, and physical settled state as separate facts.

Use cumulative Ah/Wh and uncertainty as actual control inputs. Admission needs an allowance for measurement, delayed detection, transfer, and settling; it cannot spend the full daily budget and only then ask to return. Wrong-direction regulation must act on both solar and shore contributions. Retaining earned references is compatible with updating voltage compensation; it is not permission to reset the energy budget at each anchor.

The following are **proposals/decisions**, not settled owner instructions:

| Decision | Recommended design basis | Still to establish |
| --- | --- | --- |
| HOLD accounting window | Rolling 24-hour control window, with local-calendar reporting separately. | Confirm reporting convention; clock changes must not grant new allowance. |
| Directional allowance | Preserve cumulative cost over a target journey and report rolling 24-hour reverse movement as well. | Choose enforceable interval/limits; a journey reset must not erase daily wear. |
| Capacity basis | Explicit versioned installation basis; retain both raw Ah/Wh and percent. | Rated 1440 Ah versus REC 1400 Ah versus calibrated usable capacity. |
| Target tolerance/arrival | Small tolerance justified by sensor behavior, smooth taper/arrival, persistent direction. | Numeric tolerance, hysteresis, and settling criteria; do not inherit ±5 points. |
| Full-charge exception | Preserve the owner's full Quattro endpoint in completion and hold. | Define endgame entry/completion, and whether 100% authorizes early shore bulk. |
| Lost-owner operation | Preserve protection during loss of authority and expose an explicit operator recovery/override state. | Changed target, disable, prolonged failure, and restart semantics. |
| CCL transition | Retain effective existing braking until lower-CVL preparation is validated; bound any future temporary cap. | Safe pre-CVL margin, cap duration, PV impact, and actual settling criteria. |
| Shading/capacity | Conservative confidence for stale/extrapolated evidence; independent arrays; sustained fresh complete-demand coverage. | Evidence needed to relax the historical imbalance veto. |
| Lead application fault | Distinct actionable fault and truthful applied/requested telemetry, preserving the battery objective. | Response timeout and restrictive fallback; do not blindly restore slider bulk. |
| Gaps and exhausted allowance | Report lower-bound history and preserve protection; allow explicitly defined recovery from uncertainty. | How to resume without pretending unknown history is free or creating an indefinite blanket lockout. |

“Request a floor in HOLD,” “servo the ceiling on any charging,” and “guard CHARGE stall” are useful repair directions. They are not complete proofs: ordinary floor-at-target voltage arithmetic, response lag, noise, cumulative cost, and full completion still need testing. Scope repairs around the required physical behavior.

## 10. Work order and acceptance

**Superseded work order:** the [repair plan](solar-priority-repair-plan-2026-09-14.md) provides the current stages and acceptance scope. Scenarios below remain useful evidence cases; exact daily movement enforcement and exhaustion-based gates are not required.

1. **Close permissive transitions:** D01 missing-input release, D06 CHARGE stall, and D11 incompatible full requests. Preserve existing safety and relay boundaries. Add regressions for the actual failure conditions, including restart/target boundaries.
2. **Make command authority truthful:** D04 offset failure, distinct readiness/settling, and objective-preserving failure handling. Verify ignored offset from startup, lost offset after success, recovery, and brief normal update delays.
3. **Implement prepared return:** D02 plus D14 shore availability, preserving effective surge protection through the changeover. Validate the owner's three Quattro regimes before tuning away the current cap.
4. **Implement directional regulation and cumulative control:** D03, D05, D07–D09; full DC-bus demand, small target changes, earned-reference continuity, target arrival, PV preservation, and absolute/reverse accounting.
5. **Finish timing, lifecycle, and policy:** monotonic primitives, actual probe outcome backoff, lost-owner/full-completion decisions, calibration, and documentation/configuration repair. These are required before claiming the intended operating contract.

| Acceptance family | Required scenarios and observations |
| --- | --- |
| HOLD | Stable shore overnight; strong sun; broken cloud; low steady deficits; repeated measurements; a day with potential harvest/stall triggers. Measure both current directions and relay edges. |
| CHARGE | Meaningful small increases including exactly one slider step; sustained full PV; DC shortfall; load surge; missing Quattro telemetry; restart with <5 points remaining; arrival. Check ascent, reverse integral, and shore contribution. |
| DISCHARGE | Multiple night/day cycles and repeated short shade/sun intervals; ceiling re-anchor boundaries; suspend/return; small target reduction; arrival. Check total positive battery energy, not just final SOC. |
| Full target | Selection from low and high SOC; chosen endgame transition; default/non-default threshold consistency; actual 61.96 V or lower permitted command; REC-reported completion and stable holding. |
| Reconnect | Pre-applied low CVL, unprepared at/above-pack CVL, and stable connected holding; delayed/refused commands; absent shore; external AC re-accept; startup/shutdown. Observe Quattro V×I throughout settling. |
| Electrical protection | Lower REC ceiling, charge/discharge prohibition, module faults, critical-group loss, stale selected battery, asynchronous old offsets and rounding boundaries. Check all commanded limits and actual responses. |
| Evidence/timers | Hidden/throttled PV; unequal shade; one missing MPPT; current-limited tracking; ramp delays; backward/forward clock changes; fixed measurements with successful reads; late replies after source replacement. |
| Accounting/authority | Lease expiry, consumer/REC restart, watchdog replacement, target/enable toggles, history gaps, failed persistence, repeated failed probes, exhausted allowance. No reset may create fictitious spare budget. |

Record objective/target/reference, native REC V/I, raw per-array power/current/mode and effective limits, Quattro signed V×I, effective system voltage, requested and accepted pairs, physical AC feedback/availability, current limits, faults, relay edges, and separate absolute/reverse/recovery integrals. Use appropriate timestamp alignment and retain gaps.

Run the relevant existing offline suites after implementation changes, then targeted reproductions for the repaired paths. Plant coverage should include slow/adverse response profiles as well as calibrated prepared-CVL behavior. Installed acceptance requires observed automatic departure/return and representative 24–48-hour logs using the same accounting definitions.

For deployment, retain one actuator owner and compatible REC/consumer versions; preserve calibration, localsettings, and the newest persistent ledger. Follow [CLAUDE.md](</workspaces/Boat/Boat NMEA/CLAUDE.md>) and [deploy_cerbo.py](</workspaces/Boat/Boat NMEA/deploy_cerbo.py>): shared `./cerbo` session, publisher first, verification of actual shipped versions and commands. Helm status displays should expose the objective and faults without becoming competing actuator writers. These are operational requirements for later work, not actions performed in this review.

## 11. Source and review provenance

This master reconciles the owner's requirements, the current modules linked above, repository hardware history, and these preserved reviews:

- [Original recovered 64-invariant review](</workspaces/Boat/Boat NMEA/reviews/solar-priority-invariants-2026-09-14-codex-64.md>), including its final missing-Quattro-telemetry amendment.
- [85-item review and section 4 reconciliation](</workspaces/Boat/Boat NMEA/reviews/solar-priority-invariants-2026-09-14.md>), including its new ignored-offset scenarios and transition-policy qualifications.
- [Earlier comparison](</workspaces/Boat/Boat NMEA/reviews/solar-priority-review-comparison-64-vs-85-2026-09-14.md>), written before section 4 and therefore historical where it describes disagreements that section 4 later resolved.
- [3.0 release record](</workspaces/Boat/Boat NMEA/reviews/solar-priority-restoration-2026-09-13.md>) and [engine deviations](</workspaces/Boat/Boat NMEA/reviews/solar-engine-baseline-deviations.md>).

Coverage is consolidated rather than counted as a union: the 64 original requirements are represented in SP01–SP75; the other review's hardware, tuning, deployment, and accounting inventory is incorporated in sections 3–5 and 10. Confirmed additional defects are D04, D11, D13–D16. Incorrect guarantees are replaced by explicit implementation assessments. Historical exact hardware numbers remain qualified, and unresolved policy is collected in section 9.

The original three review files and existing plant/test edits were preserved. This document is a review and design reference; it does not claim the 75 invariants are implemented, that the existing regression suite covers the new contract, or that the yacht has passed physical acceptance.
