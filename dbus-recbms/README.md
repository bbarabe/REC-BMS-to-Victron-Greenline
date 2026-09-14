# REC-BMS and Solar Priority 3.0

Solar Priority restores the committed engine 4.9 decisions inside the improved REC safety and observation boundary. The consumer owns operating decisions; REC owns charger commands and the shore relay.

## Operating behavior

| State | Behavior |
| --- | --- |
| `shore` | Wait for sustained evidence of usable solar or surplus battery voltage. |
| `probe` | Disconnect shore for the legacy 90-second MPPT ramp; evaluate battery power. |
| `solar` | Supply loads from solar/battery; return for sustained deficit, reserve, or faults. |
| `burndown` | Use harvested surplus without requiring sun; preserve the one-shot rearm latch. |
| `suspend` | Temporarily reconnect for a large load, then resume when it clears. |

One-way charging engages when the target is more than one SOC point above the battery and exits within half a point (engine 4.12; one function, `select_objective`, decides at startup, on a target change and every tick). On actual shore, REC maintains the legacy sustain floor; off shore it releases that floor so solar can charge. Both arrays already tracking with sufficient measured capacity can enter solar directly. The three-minute mean below −50 W governs its deficit return. One-way discharge uses a ceiling and allows intentional battery discharge toward the target. A 100% target releases the one-way restraint and permits charging from all chargers within REC limits.

The committed thresholds are in `solar_priority.ini [engine]`. The solar need for a probe or a direct entry is REC's complete DC-bus island demand (`[control]` inverter efficiency, idle and uncertainty, plus the DC loads) from its policy snapshot, smoothed by the consumer; without fresh valid demand no elective departure is admitted, while one-way discharge still leaves (engine 4.13). Readiness is 30 seconds, probe ramp 90 seconds, engine cooldown five minutes. REC additionally preserves physical relay dwell, rate and fault guards. The engine waits for REC departure readiness before starting a probe timer. Full baseline deviations are [documented here](../reviews/solar-engine-baseline-deviations.md).

## Safety and metering

Fresh raw REC limits, voltage/current, SOC, cells, temperatures and module permissions remain authoritative. Missing critical data cannot grant charge or departure permission. Final commands and intermediate voltage-base/solar-offset combinations remain within the REC and installation envelope. Voltage readback is required for a new departure; missing MPPT reports during an otherwise healthy island do not independently force shore. Observed unsafe limits still do.

A voltage pair that stays unverified for `lead_verify_s` is a regulation fault (3.0.1): `/RecBms/LeadFault` names the cause, InternalFailure warns, boosts are refused, `/RecBms/SolarLead` reads 0 and the commands stand — there is no fallback to the full slider. Quattro net DC power is fresh voltage × signed current. Its reported `/Dc/0/Power` is retained as diagnostic data, not used as net DC truth. Successful bounded asynchronous reads refresh unchanged observations. Missing, removed and stale sources lose validity. Source discovery is cached; historical ledger buckets and journals are excluded from control snapshots.

`energy_accounting.py` retains timestamped native Ah/Wh, separate charge/discharge counters, gaps, references and historical reporting. Uncertain history stays uncertain. Old energy allowances, return-energy calibration and trial status do not authorize or veto normal operation; the reservation and reverse-event lifecycle (`reserve`, `close_reservation`, `end_reverse_event`) has no caller in 3.0 and its telemetry is not a per-event guarantee. Its EFC is (charge Ah + discharge Ah) / (2 × capacity): 0.01 EFC is 28.8 Ah combined at the 1440 Ah basis, i.e. 2 % combined movement; 1 % combined movement is 14.4 Ah (14 Ah at REC's 1400 Ah). About 1 % of movement a day is a paper calculation and a monitoring target, not a runtime limit. No commissioning arm command exists in the active architecture.

## Services and protocol

- `dbus_recbms.py`, battery instance 200 and Max Charge switch 220: raw CAN, battery protection, sustain/boost primitives and publication.
- `rec_policy_adapter.py`: fresh observations, accounting, intent lease and the sole policy relay writer.
- `solar_engine.py`: pure five-state decision engine, independent of D-Bus.
- `solar_priority.py`, switch instance 221: enable/rated-PV settings, observations and engine request transport. Instance 222 stays retired.
- `policy_contract.py`: protocol v2, generation, ordered request IDs, bounded lease, relay feedback, dwell and departure limits.
- `control_watchdog.py`: successful-tick heartbeat; terminate/reap a stuck REC child before daemontools restarts it. The watchdog never writes actuators.

`/RecBms/Policy/Request` contains `version`, `generation`, `request_id`, `mode`, `target_soc`, `transfer_intent`, `requested_limits` and `lease_s`. Limits carry sustain 0/1/2 (release/floor/ceiling), purpose and an optional bounded boost edge. Lease renewal does not renew a boost. Mode reports the engine's choice; REC does not select another mode. A generation change invalidates old requests. An expired consumer lease forces shore and retains existing sustain protection while recovering: REC's adapter keeps refreshing a retained hold, so the primitive's own 120 s expiry is not a dead-owner release (what a changed target, OFF or a recovered consumer mean during that loss is an open decision, master review D13).

`/RecBms/Policy/Status`, `/Snapshot` and `/Telemetry/*` expose current authority, limits, source validity, relay state and measured energy. `/SolarPriority/*` exposes all five engine states, one-way mode, desired transfer and transition reasons.

## Configuration, verification and release

`config.ini` retains live battery calibration, `[sustain]`, `[solarboost]`, `[policy]` storage/topology and `[control]` metering/relay parameters. Named retired 2.x control keys are ignored for upgrade compatibility. `solar_priority.ini [engine]` restores legacy decision tunables. Existing localsettings and the latest `solar-control-state.json` must be preserved.

Run `python3 test_solar_priority.py` for the isolated engine, boundary, safety and coupled-plant suites; `python3 -m unittest test_deploy_cerbo` checks deployment tooling. Synthetic plant tests demonstrate controller behavior, not installed calibration.

Follow [CLAUDE.md](../CLAUDE.md): use only the shared `./cerbo` connection and `deploy_cerbo.py`. Back up the complete current pair before the protocol change, ship REC first, then the consumer. Roll back the complete compatible pair while retaining the newest ledger and localsettings. Never run a legacy direct relay writer with the REC policy executor.

Before ordinary operation is accepted on the boat, verify both versions, voltage/current commands, CPU/control-loop latency and CAN counter deltas, then observe actual automatic departure and return. A subsequent 24–48-hour log review evaluates everyday behavior. The [restoration report](../reviews/solar-priority-restoration-2026-09-13.md) records offline and installed verification separately.
