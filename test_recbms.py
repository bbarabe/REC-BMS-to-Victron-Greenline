#!/usr/bin/env python3
"""
test_recbms.py — off-boat checks for dbus-recbms pure logic.

    python test_recbms.py

Loads dbus_recbms.py under the test_stubs stand-ins and exercises the
functions that need no bus -- the solar lead gate and the sustain ratchet --
and the lead as the driver applies it. Exit status is 0 only if everything passes.
"""
from test_stubs import *   # noqa: F401,F403
import types

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

print("\n=== dbus-recbms: the lead in the driver ===")
T = [1_800_000_000.0]
M = [5_000.0]
R.time = types.SimpleNamespace(time=lambda: T[0], monotonic=lambda: M[0])
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 80
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = T[0]
drv = R.RecBmsDriver(cfg)
writes = []
drv._boost_write = lambda v, quiet=False: (writes.append(round(v, 2)), True)[1]


def tick(n=1):
    for _ in range(n):
        T[0] += 1
        M[0] += 1
        drv.bms.update({
            "_lastUpdate": M[0], "socHiRes": 60.0, "soc": 60, "voltage": 56.6,
            "current": 0.0, "temperature": 21.0, "cvl": 62.7, "ccl": 200.0,
            "dcl": 400.0, "dvl": 48.0, "minCellV": 3.70, "maxCellV": 3.75,
            "minCellT": 20.0, "maxCellT": 22.0})
        drv._tick()


b = drv.batt
drv.sp_enabled = True
tick()
target = b["/RecBms/TargetChargeVoltage"]
check("SP on, slider 80: Quattro commanded target - 0.15",
      b["/RecBms/SolarLead"] == 0.15 and b["/Info/MaxChargeVoltage"] == round(target - 0.15, 2)
      and writes[-1] == 0.15, "%s %s" % (b["/RecBms/SolarLead"], writes[-3:]))
drv.sp_enabled = False
del writes[:]
tick(3)
check("SP off: Quattro commanded the true target, offset cleared once",
      b["/RecBms/SolarLead"] == 0 and b["/Info/MaxChargeVoltage"] == target and writes == [0.0],
      "%s %s" % (b["/RecBms/SolarLead"], writes))
drv.sp_enabled = True
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 100
tick()
check("SP on, slider 100: no lead, every charger gets the full target",
      b["/RecBms/SolarLead"] == 0 and b["/Info/MaxChargeVoltage"] == b["/RecBms/TargetChargeVoltage"])

print("\n=== dbus-recbms: sustain ratchet (regression) ===")
F = R.SUSTAIN_FLOOR
r = R.sustain_ratchet(F, 90.0, 92.0, 80.0, 40, 100)
eff = r[1]   # (held unclipped, effective)
check("floor never above slider", eff <= 80.0, str(r))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
