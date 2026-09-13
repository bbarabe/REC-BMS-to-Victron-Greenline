# Solar Priority 3.0 release — 2026-09-13

REC and Solar Priority 3.0.0 were deployed with protocol v2 and restored engine 4.9. Enable remained on and the target remained 60%. All 19 shipped files were hash-verified at deployment. Current behavior and module responsibilities are in the [driver README](../dbus-recbms/README.md); intentional baseline changes are in [engine deviations](solar-engine-baseline-deviations.md).

Verification passed: 112 engine assertions, 180 unit/integration tests, eight deployment-tool tests. Coupled tests cover ordinary solar entry despite uncertain history, deficit return, full target, night floor, source outages, charge prohibition, restart and bounded snapshot work.

Final boat sample: REC valid, voltage commands ready, shore connected, SOC 44.31%, battery 55.2 V / −0.4 A. Available solar was approximately 32 W against 386 W needed. Automatic solar departure and return on this release had **not yet been observed on the boat**; normal operation remained enabled for daylight follow-up.

Over eight seconds, CPU was 52.2% busy and CAN received 2,595 packets, with no dropped/overflow/missed counter increments and two RX errors. Baseline was 47.1% busy with one RX error; these samples do not establish the error cause. Snapshot reply took 0.011 seconds; no Cerbo reboot occurred.

The initial configuration cleanup accidentally matched a section name inside a comment and removed later sections. The verified pre-deployment backup restored every retained calibration value, and corrected configuration was deployed before final verification. The full suite passed again. A [calibration fixture](../test_fixtures/recbms-preserved-calibration.json) now checks section and value preservation so defaults cannot mask this error.

Rollback package on the boat: `/data/backup/solar-priority-restoration-20260913/previous-pair.tar.gz`, SHA-256 `4a6006c137eeec47b2c9272f6741220fc3abbff425407fd533992b8aa5ec47f8`. Roll back a compatible REC/consumer pair together while preserving the newest ledger and localsettings.

Obsolete architecture documents and generated deployment/test artifacts were deleted from the workspace. No temporary on-boat monitor remains running; normal service logs and native accounting continue.
