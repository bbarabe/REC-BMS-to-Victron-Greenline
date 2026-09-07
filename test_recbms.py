#!/usr/bin/env python3
"""
test_recbms.py — off-boat checks for dbus-recbms pure logic.

    python test_recbms.py

Loads dbus_recbms.py under the test_stubs stand-ins and exercises the
functions that need no bus: the solar lead gate (v1.7.0) and the sustain
ratchet. Exit status is 0 only if everything passes.
"""
from test_stubs import *   # noqa: F401,F403

R = load(os.path.join(REPO, "dbus-recbms", "dbus_recbms.py"), "dbus_recbms")
cfg = R.Config(os.path.join(REPO, "dbus-recbms", "config.ini"))

print("\n=== dbus-recbms: solar lead gate ===")
check("config: solar_lead_v 0.15", abs(cfg.solar_lead - 0.15) < 1e-9, str(cfg.solar_lead))
check("config: lead needs Solar Priority", cfg.lead_needs_sp is True)
check("config: lead_full_pct 100", cfg.lead_full_pct == 100, str(cfg.lead_full_pct))

L = R.standing_lead
check("SP on, slider 80: lead in force", L(0.15, 80, 100, True) == 0.15)
check("SP off, slider 80: no lead", L(0.15, 80, 100, False) == 0.0)
check("SP on, slider 100: no lead (full charge)", L(0.15, 100, 100, True) == 0.0)
check("SP off, slider 100: no lead", L(0.15, 100, 100, False) == 0.0)
check("SP unknown (None): no lead", L(0.15, 80, 100, None) == 0.0)
check("SP off but gate disabled in config: lead", L(0.15, 80, 100, False, needs_sp=False) == 0.15)
check("slider 99.9 just under full: lead", L(0.15, 99.9, 100, True) == 0.15)
check("lead 0 in config: never", L(0.0, 80, 100, True) == 0.0)
check("full_pct 95, slider 95: no lead", L(0.15, 95, 95, True) == 0.0)

print("\n=== dbus-recbms: sustain ratchet (regression) ===")
F = R.SUSTAIN_FLOOR
r = R.sustain_ratchet(F, 90.0, 92.0, 80.0, 40, 100)
eff = r[1]   # (held unclipped, effective)
check("floor never above slider", eff <= 80.0, str(r))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
