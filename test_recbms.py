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
import tempfile
_tmpdir = tempfile.mkdtemp(prefix="recbms-test-")
cfg.energy_file = os.path.join(_tmpdir, "energy.json")

print("\n=== dbus-recbms: the energy ledger (4.2.0) ===")
check("config: current published at the BMS's 0.1 A, ledger file and hourly log set",
      cfg.current_step == 0.1 and cfg.energy_log_hourly and cfg.energy_file.endswith("energy.json"))
E = R.EnergyLedger
e = E(None)
w0 = 1_800_000_000.0
for i in range(360):                       # +10 A at 56.6 V for 6 min
    e.add(566.0, 1.0, w0 + i)
check("6 min at +566 W: 56.6 Wh in, nothing out", abs(e.charged_wh - 56.6) < 0.01 and e.discharged_wh == 0.0,
      "%.2f %.2f" % (e.charged_wh, e.discharged_wh))
for i in range(180):                       # -20 A for 3 min
    e.add(-1132.0, 1.0, w0 + 360 + i)
check("3 min at -1132 W: 56.6 Wh out, the in side untouched",
      abs(e.discharged_wh - 56.6) < 0.01 and abs(e.charged_wh - 56.6) < 0.01)
check("24 h window carries both", tuple(round(x, 1) for x in e.last24h(w0 + 540)) == (56.6, 56.6), str(e.last24h(w0 + 540)))
x = E(None); x.add(566.0, 5.0, w0)
check("the driver clips a stalled clock to 5 s: 5 s at 566 W is 0.79 Wh", abs(x.charged_wh - 0.786) < 0.001, str(x.charged_wh))
closed = e.add(0.0, 1.0, w0 + 3600 * 2)
check("an hour rolling over returns the closed hour's (in, out)", closed is not None and tuple(round(x, 1) for x in closed) == (56.6, 56.6), str(closed))
check("25 h later the window is empty, the lifetime figures stand",
      e.last24h(w0 + 25 * 3600) == (0.0, 0.0) and abs(e.charged_wh - 56.6) < 0.01)
e.add(0.0, 1.0, w0 + 25 * 3600)
check("... and the old bins are pruned", all(k > (w0 + 25 * 3600) // 300 - 288 for k in e.bins), str(sorted(e.bins)[:3]))
e = E(cfg.energy_file)
e.add(1000.0, 1.0, w0); e.add(1000.0, 1.0, w0 + 300); e.add(-500.0, 1.0, w0 + 600)   # two bin rolls -> saved
e.save()
e2 = E(cfg.energy_file)
check("a restart carries on from the file (to the Wh)", abs(e2.charged_wh - e.charged_wh) < 1e-3 and abs(e2.discharged_wh - e.discharged_wh) < 1e-3
      and all(abs(a - b) < 1e-3 for a, b in zip(e2.last24h(w0 + 600), e.last24h(w0 + 600))), "%s %s" % (e2.charged_wh, e2.last24h(w0 + 600)))
os.remove(cfg.energy_file)
with open(cfg.energy_file, "w") as f:
    f.write("{not json")
check("an unreadable file starts from zero, no crash", E(cfg.energy_file).charged_wh == 0.0)
os.remove(cfg.energy_file)

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

check("config: solar gain 1 % above the target (4.1.0), SOC in 0.05 % steps",
      cfg.solar_gain_pct == 1.0 and cfg.solar_gain_max_v == 0.30 and cfg.soc_step == 0.05,
      "%s %s %s" % (cfg.solar_gain_pct, cfg.solar_gain_max_v, cfg.soc_step))

print("\n=== dbus-recbms: the lead in the driver (solar_gain_pct = 0) ===")
cfg.solar_gain_pct = 0.0          # the lead below the target: the 4.0.0 behaviour
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

# 62.40 V is the ceiling on every charge voltage, whoever asks
check("config: one ceiling, 62.40 V, for the CVL and the boost",
      cfg.ceiling_v == 62.40 and cfg.boost_ceiling_v == 62.40)
check("slider 100: CVL 61.96, no lead, no offset",
      b["/RecBms/TargetChargeVoltage"] == 61.96 and b["/Info/MaxChargeVoltage"] == 61.96 and
      b["/RecBms/SolarLead"] == 0 and drv._last_offset == 0.0)
drv.eq.update(active=True, startTime=M[0])
tick()
check("equalization at 100 %: 61.96 + 0.44 lands on the ceiling, not over",
      b["/RecBms/TargetChargeVoltage"] == 62.40 and b["/Info/MaxChargeVoltage"] == 62.40,
      str(b["/RecBms/TargetChargeVoltage"]))
ok_, why = drv._boost_allowed(0.30)
check("a solar boost on top of it is refused by the ceiling", not ok_ and "ceiling" in why, why)
drv.cfg.eq_boost = 1.0
tick()
check("a larger equalization boost is clamped to the ceiling", b["/RecBms/TargetChargeVoltage"] == 62.40,
      str(b["/RecBms/TargetChargeVoltage"]))
drv.cfg.eq_boost = cfg.eq_boost
drv.eq["active"] = False
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = T[0]
tick()
ok_, why = drv._boost_allowed(0.30)
check("slider 100, no equalization: a 0.30 V boost fits under the ceiling (62.26)", ok_, why)
drv.cfg.boost_max_v = 0.50
ok_, why = drv._boost_allowed(0.50)
check("... a 0.50 V one does not (62.46)", not ok_ and "ceiling" in why, why)
drv.cfg.boost_max_v = cfg.boost_max_v
drv.boost = {"active": True, "req_ts": M[0], "volts": 0.30}
drv._boost_allowed = lambda volts: (True, "")        # the gate out of the way:
drv.cfg.ceiling_v = 62.10                            # ... the output clamp alone
del writes[:]
tick()
check("the offset written for a boost is clamped at the point of output",
      writes and abs(writes[-1] - 0.14) < 1e-9 and b["/RecBms/SolarBoost/EffectiveChargeVoltage"] == 62.10,
      "%s %s" % (writes[-3:], b["/RecBms/SolarBoost/EffectiveChargeVoltage"]))
del drv._boost_allowed
drv.cfg.ceiling_v = 62.40
drv._boost_clear("test done")


def _cfg_with(boost_ceiling):
    import tempfile
    text = open(os.path.join(REPO, "dbus-recbms", "config.ini")).read()
    head, tail = text.split("[solarboost]", 1)
    tail = tail.replace("ceiling_v = 62.40", "ceiling_v = %s" % boost_ceiling, 1)
    with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
        f.write(head + "[solarboost]" + tail)
    try:
        return R.Config(f.name)
    finally:
        os.unlink(f.name)


check("a boost-only ceiling can be lower, never higher",
      _cfg_with("62.70").boost_ceiling_v == 62.40 and _cfg_with("62.00").boost_ceiling_v == 62.00)

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

print("\n=== dbus-recbms: solar gain (4.1.0) ===")
G = R.solar_gain_v
line = lambda pct: 50.0 + 0.08 * pct                       # a straight test curve
check("gain: the curve's rise over the band", G(line, 50, 1.0, 0.30) == 0.08)
check("gain: capped at max_v", G(line, 50, 5.0, 0.30) == 0.30)
check("gain 0: none", G(line, 50, 0.0, 0.30) == 0.0)
check("gain: a clipped curve gives none", G(lambda p: 61.96, 99.5, 1.0, 0.30) == 0.0)

drv.cfg.solar_gain_pct = 1.0
drv.sp_enabled = True
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 50
drv._boost_write = lambda v, quiet=False: (writes.append(round(v, 2)), True)[1]
drv.eff_cv = None
del writes[:]
tick(2)
q50 = round(drv._slider_cvl(50), 2)
g50 = round(drv._slider_cvl(51) - drv._slider_cvl(50), 2)
check("slider 50: the Quattro gets the slider's own voltage",
      b["/Info/MaxChargeVoltage"] == q50, "%s vs %s" % (b["/Info/MaxChargeVoltage"], q50))
check("... and the MPPTs one SOC point more, through the offset",
      b["/RecBms/TargetChargeVoltage"] == round(q50 + g50, 2) and b["/RecBms/SolarLead"] == g50
      and writes[-1] == g50 and 0.05 <= g50 <= 0.10,
      "%s %s %s" % (b["/RecBms/TargetChargeVoltage"], b["/RecBms/SolarLead"], writes[-2:]))
drv.sp_enabled = False
tick(2)
check("Solar Priority off: no gain, every charger at the slider's voltage",
      b["/RecBms/SolarLead"] == 0 and b["/Info/MaxChargeVoltage"] == q50 and b["/RecBms/TargetChargeVoltage"] == q50)
drv.sp_enabled = True
tick(2)
drv.lead_fault.update(active=True, msg="test fault", mismatch_since=0.0)
drv._verify_lead = lambda now: False
tick()
check("offset not in force: the Quattro stays on its own figure, never the MPPTs' target",
      b["/Info/MaxChargeVoltage"] == q50 and b["/RecBms/TargetChargeVoltage"] == q50 and b["/RecBms/SolarLead"] == 0,
      "%s %s" % (b["/Info/MaxChargeVoltage"], b["/RecBms/TargetChargeVoltage"]))
del drv._verify_lead
drv.lead_fault.update(active=False, msg="", mismatch_since=0.0)
# a sustain floor holds the Quattro at the present SOC; the sun keeps its band above it
drv.sustain.update(active=True, mode=R.SUSTAIN_FLOOR, soc=45.0, req_ts=M[0], logged_soc=None)
for _ in range(2):
    T[0] += 1; M[0] += 1
    drv.bms.update({"_lastUpdate": M[0], "socHiRes": 45.0, "soc": 45})
    drv._tick()
q45 = round(drv._slider_cvl(45), 2)
check("floor at 45 % under a 50 % slider: Quattro at 45 %'s voltage, MPPTs a point above it",
      b["/Info/MaxChargeVoltage"] == q45 and b["/RecBms/TargetChargeVoltage"] > q45
      and b["/RecBms/TargetChargeVoltage"] < q50 + 0.001,
      "%s %s" % (b["/Info/MaxChargeVoltage"], b["/RecBms/TargetChargeVoltage"]))
drv.sustain.update(active=True, mode=R.SUSTAIN_CEILING, soc=55.0, req_ts=M[0], logged_soc=None)
for _ in range(2):
    T[0] += 1; M[0] += 1
    drv.bms.update({"_lastUpdate": M[0], "socHiRes": 55.0, "soc": 55})
    drv._tick()
check("a sustain ceiling gets no gain: nothing charges, the sun included",
      b["/RecBms/SolarLead"] == 0 and b["/RecBms/TargetChargeVoltage"] == b["/Info/MaxChargeVoltage"] == round(drv._slider_cvl(55), 2),
      "%s %s" % (b["/RecBms/TargetChargeVoltage"], b["/Info/MaxChargeVoltage"]))
drv._sustain_clear("test done")
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 100
tick(2)
check("slider 100: no gain, 61.96 for everyone",
      b["/RecBms/SolarLead"] == 0 and b["/Info/MaxChargeVoltage"] == 61.96 and b["/RecBms/TargetChargeVoltage"] == 61.96)
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 80
tick(2)
check("slider 80: the steeper curve gives a larger gain (0.17 V)", b["/RecBms/SolarLead"] == 0.17, str(b["/RecBms/SolarLead"]))
drv.cfg.ceiling_v = round(drv._slider_cvl(80), 2) + 0.03
tick(2)
check("the ceiling bounds the MPPTs' target, gain included",
      b["/RecBms/TargetChargeVoltage"] == drv.cfg.ceiling_v and b["/RecBms/SolarLead"] == 0.03,
      "%s %s" % (b["/RecBms/TargetChargeVoltage"], b["/RecBms/SolarLead"]))
drv.cfg.ceiling_v = 62.40

# the BMS's own CVL bounds the MPPTs too: the gain, and a boost on top of it
FakeBus.store["/Settings/RecBms/ChargeSlider"] = 50


def tick_bms_cvl(cvl, n=2):
    for _ in range(n):
        T[0] += 1; M[0] += 1
        drv.bms.update({"_lastUpdate": M[0], "socHiRes": 50.0, "soc": 50, "cvl": cvl, "voltage": 55.5})
        drv._tick()


tick_bms_cvl(round(q50 + 0.04, 2))
check("BMS CVL 0.04 V over the slider's voltage: the gain is cut to it",
      b["/Info/MaxChargeVoltage"] == q50 and b["/RecBms/SolarLead"] == 0.04, str(b["/RecBms/SolarLead"]))
ok_, why = drv._boost_allowed(0.30)
check("... and a boost over it is refused", not ok_ and "BMS" in why, why)
drv.boost = {"active": True, "req_ts": M[0], "volts": 0.30}
drv._boost_allowed = lambda volts: (True, "")        # the gate out of the way: the output clamp alone
del writes[:]
tick_bms_cvl(round(q50 + 0.04, 2), 1)
check("... and clamped at the point of output: the MPPTs are never sent more than the BMS allows",
      writes[-1] == 0.04 and b["/RecBms/SolarBoost/EffectiveChargeVoltage"] == round(q50 + 0.04, 2),
      "%s %s" % (writes[-2:], b["/RecBms/SolarBoost/EffectiveChargeVoltage"]))
del drv._boost_allowed
drv._boost_clear("test done")
tick_bms_cvl(62.7)
drv.boost = {"active": True, "req_ts": M[0], "volts": 0.30}
del writes[:]
tick_bms_cvl(62.7, 1)
check("a boost rides on top of the gain: offset = gain + boost, the Quattro untouched",
      writes[-1] == round(g50 + 0.30, 2) and b["/Info/MaxChargeVoltage"] == q50
      and b["/RecBms/SolarBoost/EffectiveChargeVoltage"] == round(q50 + g50 + 0.30, 2),
      "%s %s" % (writes[-2:], b["/RecBms/SolarBoost/EffectiveChargeVoltage"]))
drv._boost_clear("test done")
drv._verify_lead = lambda now: False
tick_bms_cvl(62.7, 1)
check("offset not in force: the boost gate and the engine are told the MPPTs' real target",
      drv.last_target == q50 and b["/RecBms/SolarBoost/EffectiveChargeVoltage"] == q50,
      "%s %s" % (drv.last_target, b["/RecBms/SolarBoost/EffectiveChargeVoltage"]))
del drv._verify_lead
drv.lead_fault.update(active=False, msg="", mismatch_since=0.0)

print("\n=== dbus-recbms: the ledger in the driver ===")
c_in, c_out = b["/History/ChargedEnergy"] or 0, b["/History/DischargedEnergy"] or 0
for _ in range(60):
    T[0] += 1
    M[0] += 1
    drv.bms.update({"_lastUpdate": M[0], "current": 10.0, "voltage": 56.6})
    drv._tick()
check("driver: 60 s at +10 A / 56.6 V -> +0.009 kWh on /History/ChargedEnergy and the 24 h path, nothing out",
      abs(b["/History/ChargedEnergy"] - c_in - 0.009) < 0.0015 and b["/History/DischargedEnergy"] == c_out
      and abs(b["/RecBms/Energy/Charged24h"] - 0.009) < 0.0015,
      "%s %s %s" % (b["/History/ChargedEnergy"], b["/History/DischargedEnergy"], b["/RecBms/Energy/Charged24h"]))
M[0] += 3600                     # a stalled tick: an hour passes at once
T[0] += 3600
drv.bms.update({"_lastUpdate": M[0], "current": 10.0, "voltage": 56.6})
drv._tick()
check("driver: a stalled clock integrates five seconds of it, not the hour",
      abs(b["/History/ChargedEnergy"] - c_in - 0.010) < 0.0015, str(b["/History/ChargedEnergy"]))
drv.bms.update({"current": 0.0})

print("\n=== dbus-recbms: sustain ratchet (regression) ===")
F = R.SUSTAIN_FLOOR
r = R.sustain_ratchet(F, 90.0, 92.0, 80.0, 40, 100)
eff = r[1]   # (held unclipped, effective)
check("floor never above slider", eff <= 80.0, str(r))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
