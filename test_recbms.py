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

# the offset persists inside systemcalc: a clear that fails must be retried
drv.sp_enabled = True
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 80
tick(2)
ok_writes = [True]
drv._boost_write = lambda v, quiet=False: (writes.append(round(v, 2)), ok_writes[0])[1]
drv.sp_enabled = False
ok_writes[0] = False
del writes[:]
tick(3)
check("a failed offset clear is retried every tick", writes == [0.0, 0.0, 0.0], str(writes))
ok_writes[0] = True
tick(3)
check("... until it lands, then no more", writes == [0.0, 0.0, 0.0, 0.0], str(writes))

# a lead fault cannot outlive the lead it was about
drv.sp_enabled = True
tick(2)
drv.lead_fault.update(active=True, msg="test fault", mismatch_since=M[0] - 5)
drv.sp_enabled = False
tick(2)
check("no lead wanted: the fault and its half-run timer are cleared",
      not drv.lead_fault["active"] and not drv.lead_fault["mismatch_since"] and b["/RecBms/LeadFault"] == "")

# the Solar Priority setting: absent = off, a failed read keeps the last answer
class _Gone(Exception):
    def get_dbus_name(self):
        return "org.freedesktop.DBus.Error.UnknownObject"


real_call = drv.sbus.call_blocking
drv.sp_enabled = True
drv.sbus.call_blocking = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("busy"))
drv._poll_solar_priority()
check("a failed read keeps Solar Priority on", drv.sp_enabled is True)
drv.sbus.call_blocking = lambda *a, **k: (_ for _ in ()).throw(_Gone())
drv._poll_solar_priority()
check("a setting that does not exist reads as off", drv.sp_enabled is False)
drv.sbus.call_blocking = real_call
FakeBus.store["/Settings/SolarPriority/Enabled"] = 1
drv._poll_solar_priority()
check("the setting is read from localsettings", drv.sp_enabled is True)

check("_q: any step keeps its own decimals",
      R._q(0.74, 0.25) == 0.75 and R._q(0.26, 0.25) == 0.25 and R._q(7.4, 2.5) == 7.5 and R._q(-1.27, 0.5) == -1.5)

# a tick that raises must not end the timer (GLib drops a callback that raises)
inner = drv._tick_inner
drv._tick_inner = lambda: 1 / 0
check("a tick that raises keeps the timer", drv._tick() is True)
drv._tick_inner = inner

print("\n=== dbus-recbms: sustain ratchet (regression) ===")
F = R.SUSTAIN_FLOOR
r = R.sustain_ratchet(F, 90.0, 92.0, 80.0, 40, 100)
eff = r[1]   # (held unclipped, effective)
check("floor never above slider", eff <= 80.0, str(r))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
