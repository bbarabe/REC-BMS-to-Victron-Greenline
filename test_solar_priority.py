#!/usr/bin/env python3
"""
test_solar_priority.py — one-way charge / discharge, off the boat.

    python test_solar_priority.py

Two halves, both against the stand-ins in test_stubs.py:

  dbus-recbms   the Sustain control: /RecBms/Sustain/Request pins the CVL at
                the PRESENT SOC, ratchets one way only, stays inside the real
                slider, expires by itself, refuses boosts under a ceiling,
                makes an equalization wait, and refuses when the BMS is stale.

  engine 4.3    the Engine class driven tick by tick through a charge day
                (shore/sustain -> probe -> solar -> deficit -> shore/sustain
                -> target reached) and a discharge (sustain ceiling, straight
                to solar, no deficit/surge/drift exits, heater suspend and
                resume, target reached, SOC floor), plus the off switches.

Exit status is 0 only if everything passes.
"""
import types

from test_stubs import *   # noqa: F401,F403

# solar_priority.py imports velib's DbusMonitor; only the Engine is exercised
dm = types.ModuleType("dbusmonitor")
dm.DbusMonitor = object
sys.modules["dbusmonitor"] = dm

# ============================================================ dbus-recbms
print("\n=== dbus-recbms: sustain ===")
R = load(os.path.join(REPO, "dbus-recbms", "dbus_recbms.py"), "dbus_recbms")
rcfg = R.Config(os.path.join(REPO, "dbus-recbms", "config.ini"))
check("config: [sustain] parsed", rcfg.sustain_enabled and rcfg.sustain_hold_s == 120)

T = [1_800_000_000.0]
R.time = types.SimpleNamespace(time=lambda: T[0])
drv = R.RecBmsDriver(rcfg)
batt = drv.batt
SOC = [62.0]


def live():
    drv.bms.update({
        "_lastUpdate": T[0], "socHiRes": SOC[0], "soc": int(SOC[0]),
        "voltage": 56.6, "current": 0.0, "temperature": 21.0,
        "cvl": 62.7, "ccl": 200.0, "dcl": 400.0, "dvl": 48.0,
        "minCellV": 3.70, "maxCellV": 3.75, "minCellT": 20.0, "maxCellT": 22.0,
    })


def rtick(n=1, soc=None, slider=None, dt=1.0):
    if soc is not None:
        SOC[0] = soc
    if slider is not None:
        FakeBus.store["/Settings/RecBms/ChargeSlider"] = slider
    for _ in range(n):
        T[0] += dt
        live()
        drv._tick()


def curve(pct):
    return round(drv._slider_cvl(pct), 2)


rtick(soc=62, slider=80)
check("slider published as /RecBms/TargetSoc", batt["/RecBms/TargetSoc"] == 80)
check("no hold: CVL from the slider", batt["/RecBms/TargetChargeVoltage"] == curve(80))
check("sustain telemetry idle", batt["/RecBms/Sustain/Active"] == 0 and
      batt["/RecBms/Sustain/Status"] == "idle")

check("floor request accepted", batt.write("/RecBms/Sustain/Request", 1))
rtick()
check("floor: CVL = curve(present SOC)", batt["/RecBms/TargetChargeVoltage"] == curve(62),
      "%s vs %s" % (batt["/RecBms/TargetChargeVoltage"], curve(62)))
check("floor telemetry", batt["/RecBms/Sustain/Active"] == 1 and
      batt["/RecBms/Sustain/Mode"] == 1 and batt["/RecBms/Sustain/Soc"] == 62.0 and
      batt["/RecBms/Sustain/Status"] == "floor")
rtick(soc=62.6)
check("floor ignores a wobble under one step", batt["/RecBms/TargetChargeVoltage"] == curve(62) and
      batt["/RecBms/Sustain/Soc"] == 62.0)
rtick(soc=65)
check("floor follows the bank up a full step", batt["/RecBms/TargetChargeVoltage"] == curve(65))
rtick(soc=60)
check("floor never follows it down", batt["/RecBms/TargetChargeVoltage"] == curve(65))
rtick(slider=64)
check("floor stays under the real slider", batt["/RecBms/TargetChargeVoltage"] == curve(64))
rtick(slider=80)
check("...and pops back when the slider rises", batt["/RecBms/TargetChargeVoltage"] == curve(65))

# EQ due while held: it must wait, not run
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = 0
rtick()
check("equalization waits under a hold", not drv.eq["active"] and
      batt["/RecBms/EqStatus"] == "" and batt["/RecBms/TargetChargeVoltage"] == curve(65))

# re-assert refreshes the expiry and keeps the ratchet
rtick(n=1, dt=100)
check("re-assert accepted", batt.write("/RecBms/Sustain/Request", 1))
rtick(n=1, dt=100)
check("re-asserted hold survives past the original expiry",
      batt["/RecBms/Sustain/Active"] == 1 and batt["/RecBms/TargetChargeVoltage"] == curve(65))
rtick(n=1, dt=125)
check("hold expires on its own", batt["/RecBms/Sustain/Active"] == 0 and
      batt["/RecBms/Sustain/Status"].startswith("expired") and
      batt["/RecBms/Sustain/Request"] == 0)
check("slider back in force after expiry", batt["/RecBms/TargetChargeVoltage"] == curve(80) + rcfg.eq_boost
      or batt["/RecBms/TargetChargeVoltage"] == curve(80), str(batt["/RecBms/TargetChargeVoltage"]))
check("equalization starts once released", drv.eq["active"])
drv.eq["active"] = False
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = T[0]

# ceiling
rtick(soc=90, slider=70)
check("ceiling request accepted", batt.write("/RecBms/Sustain/Request", 2))
rtick()
check("ceiling: CVL = curve(present SOC)", batt["/RecBms/TargetChargeVoltage"] == curve(90) and
      batt["/RecBms/Sustain/Mode"] == 2 and batt["/RecBms/Sustain/Status"] == "ceiling")
rtick(soc=89.3)
check("ceiling ignores a wobble under one step", batt["/RecBms/TargetChargeVoltage"] == curve(90))
rtick(soc=85)
check("ceiling follows the bank down a full step", batt["/RecBms/TargetChargeVoltage"] == curve(85))
rtick(soc=88)
check("ceiling never follows it up", batt["/RecBms/TargetChargeVoltage"] == curve(85))
rtick(soc=69)
check("ceiling never goes under the real slider", batt["/RecBms/TargetChargeVoltage"] == curve(70))
check("boost refused under a ceiling", not batt.write("/RecBms/SolarBoost/Request", 0.2) and
      batt["/RecBms/SolarBoost/Status"] == "refused: sustain ceiling active")

# switching mode re-samples; release restores the slider
rtick(soc=75)
check("floor request while ceiling held", batt.write("/RecBms/Sustain/Request", 1))
rtick()
check("mode switch re-samples the SOC", batt["/RecBms/Sustain/Mode"] == 1 and
      drv.sustain["soc"] == 75.0 and batt["/RecBms/Sustain/Soc"] == 70.0 and
      batt["/RecBms/TargetChargeVoltage"] == curve(70),
      "floor 75 clipped to slider 70 -> %s" % batt["/RecBms/TargetChargeVoltage"])
check("release accepted", batt.write("/RecBms/Sustain/Request", 0))
rtick()
check("released: slider CVL, telemetry cleared", batt["/RecBms/TargetChargeVoltage"] == curve(70) and
      batt["/RecBms/Sustain/Active"] == 0 and batt["/RecBms/Sustain/Soc"] is None)

# stale BMS: no present SOC to pin, so refuse
drv.bms["_lastUpdate"] = T[0] - 100
check("refused while the BMS is stale", not batt.write("/RecBms/Sustain/Request", 1) and
      batt["/RecBms/Sustain/Status"] == "refused: BMS not live")
check("bad mode refused", not batt.write("/RecBms/Sustain/Request", 7))

# pure ratchet corner: no SOC (fallback) keeps the last hold, still clipped
check("ratchet: floor without SOC keeps and clips",
      R.sustain_ratchet(1, 65, None, 60, 40, 100) == (65, 60) and
      R.sustain_ratchet(2, 65, None, 70, 40, 100) == (65, 70) and
      R.sustain_ratchet(1, 30, 35, 80, 40, 100) == (35, 40) and
      R.sustain_ratchet(1, 60, 60.9, 80, 40, 100) == (60, 60) and
      R.sustain_ratchet(1, 60, 61.0, 80, 40, 100) == (61, 61) and
      R.sustain_ratchet(2, 60, 59.2, 40, 40, 100) == (60, 60))

# ================================================================ engine 4.3
print("\n=== solar priority engine: one-way ===")
SP = load(os.path.join(REPO, "dbus-recbms", "solar_priority.py"), "solar_priority")
scfg = SP.Config(os.path.join(REPO, "dbus-recbms", "solar_priority.ini"))
check("config: one-way tunables", scfg.engine["ONEWAY_ENTER_PCT"] == 5 and
      scfg.engine["ONEWAY_EXIT_PCT"] == 1)
check("engine version bumped", SP.ENGINE_VERSION == "4.3")
Val = SP.Val


class Sim:
    """Drives Engine.tick with a plant that follows the commands: the
    Quattro's ActiveInput reports 240 (none) once IgnoreAcIn is 1."""

    def __init__(self, **tun):
        self.t = dict(SP.ENGINE_DEFAULTS)
        self.t.update(tun)
        self.now = 1_800_000_000_000
        self.logs = []
        self.eng = SP.Engine(self.t, self.now, logger=self.logs.append)
        self.inp = SP.Inputs()
        self.inp.enabled = True
        self.inp.feed_shore = 0
        self.v = dict(soc=60.0, batt=0.0, load=300.0, pv=500.0, m=2, voc=60.0,
                      batt_v=56.6, cvl=56.62, target=None)
        self.cmd, self.sustain = 0, 0
        self.cmds, self.sustains, self.boosts = [], [], []
        self.transitions, self.states = [], set()
        self.out = None

    @property
    def state(self):
        return self.eng.st["state"]

    @property
    def oneway(self):
        return self.eng.st["oneway"]

    def tick(self, secs=1, **kv):
        self.v.update(kv)
        for _ in range(secs):
            self.now += 1000
            n, v, inp = self.now, self.v, self.inp
            inp.soc = Val(v["soc"], n)
            inp.batt = Val(v["batt"], n)
            inp.load_now = Val(v["load"], n)
            inp.load_avg = Val(v["load"], n)
            inp.feed = Val(240 if self.cmd == 1 else 0, n)
            inp.ac_out = Val(v["load"], n)
            inp.voc6, inp.y6, inp.m6 = Val(v["voc"], n), Val(v["pv"], n), Val(v["m"], n)
            inp.voc7, inp.y7, inp.m7 = Val(0.0, n), Val(0.0, n), Val(0, n)
            inp.batt_v, inp.cvl = Val(v["batt_v"], n), Val(v["cvl"], n)
            inp.target_soc = Val(v["target"], n) if v["target"] is not None else None
            out = self.eng.tick(n, inp)
            if out.cmd is not None:
                self.cmd = out.cmd
                self.cmds.append((n, out.cmd))
            if out.sustain is not None:
                self.sustain = out.sustain
                self.sustains.append((n, out.sustain))
            if out.boost:
                self.boosts.append(out.boost)
            if out.transition:
                self.transitions.append(out.transition)
            self.states.add(out.state)
            self.out = out
        return self.out


# ---- no target published (old dbus-recbms): the 4.2 engine, unchanged ----
s = Sim()
s.tick(335, soc=60)
check("no target: normal probe path", s.state == "probe" and s.oneway is None)
check("no target: sustain never written", s.sustains == [])

# ---- charge one-way: 60 % -> 80 % ----
s = Sim()
# a freshly solar-charged bank sits above the sustain CVL (surplus in 4.2 terms)
s.tick(1, soc=60, target=80, batt_v=56.9, cvl=56.62)
check("charge: engaged", s.oneway == "charge" and s.out.oneway == "charge")
check("charge: sustain floor while on shore", s.sustain == 1 and s.sustains[-1][1] == 1)
check("charge: status says so", s.out.status_text.startswith("1-WAY CHARGE 60->80% |"),
      s.out.status_text)
check("charge: engagement logged", any(l.startswith("ONE-WAY CHARGE 60.0% -> 80%") for l in s.logs))
n0 = len(s.sustains)
s.tick(31)
check("charge: floor re-asserted every ASSERT_MS", len(s.sustains) == n0 + 1 and s.sustain == 1)
s.tick(304)
check("charge: aboveCvl neither burns down nor blocks the probe",
      s.state == "probe" and s.cmd == 1, "state %s" % s.state)
check("charge: sustain released while shore is off", s.sustain == 0 and s.sustains[-1][1] == 0)
check("charge: probe boost requested", s.boosts and s.boosts[-1] == s.t["BOOST_V"])
s.tick(95, batt=100.0, cvl=59.49)       # hold released: the real target is back
check("charge: probe -> solar", s.state == "solar" and s.sustain == 0)
s.tick(60, soc=68.0)
check("charge: solar stays while the bank fills", s.state == "solar" and s.oneway == "charge")
# night: PV gone, the loads draw from the bank -> deficit exit -> shore + sustain
s.tick(200, batt=-200.0, pv=0.0, m=0, voc=10.0)
check("charge: deficit -> shore", s.state == "shore" and s.cmd == 0, s.state)
check("charge: floor requested with the shore command",
      s.sustain == 1 and s.sustains[-1][0] == s.cmds[-1][0])
check("charge: still engaged at 68 %", s.oneway == "charge")
s.tick(1, soc=79.5, batt=0.0)
check("charge: done within EXIT of the target", s.oneway is None and s.sustain == 0 and
      any("ONE-WAY charge done (SOC 79.5% at target 80%)" in l for l in s.logs))

# ---- hysteresis and re-targeting ----
s = Sim()
s.tick(1, soc=77, target=80)
check("77 -> 80 is inside ENTER: normal engine", s.oneway is None)
s.tick(1, soc=74)
check("74 -> 80 engages", s.oneway == "charge")
s.tick(1, soc=78)
check("78 -> 80 stays engaged (EXIT is 1)", s.oneway == "charge")
s.tick(1, target=50)
check("slider moved the other way: flips to discharge", s.oneway == "discharge" and s.sustain == 2)
s.inp.enabled = False
s.tick(1)
check("disabled: one-way off, sustain released", s.oneway is None and s.sustain == 0 and
      any("done (disabled)" in l for l in s.logs))
s.inp.enabled = True
s.tick(1)
check("re-enabled: engages again", s.oneway == "discharge")
s.tick(1, target=None)
check("target vanished: one-way off", s.oneway is None and s.sustain == 0 and
      any("done (no Max Charge target)" in l for l in s.logs))

# ---- discharge one-way: 90 % -> 70 % ----
s = Sim()
s.tick(1, soc=90, target=70, batt_v=60.3, cvl=60.3)
check("discharge: engaged, ceiling from the first tick", s.oneway == "discharge" and s.sustain == 2)
check("discharge: status", s.out.status_text.startswith("1-WAY DISCHARGE 90->70% |"), s.out.status_text)
s.tick(335, pv=0.0, m=0, voc=10.0)          # no sun at all: irrelevant, leave anyway
check("discharge: shore -> solar directly, no probe", s.state == "solar" and s.cmd == 1 and
      "probe" not in s.states, "state %s, seen %s" % (s.state, sorted(s.states)))
check("discharge: transition names it",
      any(tr == "-> SOLAR (one-way discharge 90.0% -> 70%)" for tr in s.transitions),
      str(s.transitions))
check("discharge: no boost ever requested", s.boosts == [])
s.tick(300, batt=-600.0)
check("discharge: deficit and surge do not end it", s.state == "solar" and s.cmd == 1)
check("discharge: DRAIN status", "DRAIN | PV 0W batt -600W" in s.out.status_text, s.out.status_text)
s.tick(30, soc=84.0)
check("discharge: SOC drift does not end it", s.state == "solar")
s.tick(5, load=1500.0, batt=-1500.0)
check("discharge: heater-class load -> suspend on shore", s.state == "suspend" and s.cmd == 0)
check("discharge: ceiling kept through suspend", s.sustain == 2)
s.tick(15, load=300.0, batt=-300.0)
check("discharge: resumes to solar without a boost", s.state == "solar" and s.cmd == 1 and
      s.boosts == [])
s.tick(1, soc=70.9)
check("discharge: done within EXIT of the target", s.oneway is None and s.sustain == 0)
s.tick(20)
check("discharge done: the normal deficit exit takes over", s.state == "shore" and s.cmd == 0)

# ---- the SOC floor still wins ----
s = Sim()
s.tick(1, soc=47, target=40)
check("47 -> 40 engages discharge", s.oneway == "discharge")
s.tick(335, pv=0.0, m=0, voc=10.0)
check("floor test: draining", s.state == "solar")
s.tick(1, soc=39.5, batt=-300.0)
check("MIN_SOC ends the drain regardless", s.state == "shore" and s.cmd == 0 and
      "SOC 39.5%" in (s.out.transition or ""))

# ---- emergency SOC lockout still applies while discharging ----
s = Sim(MIN_SOC=25)
s.tick(1, soc=47, target=40)
s.tick(335, pv=0.0, m=0, voc=10.0)
s.tick(1, soc=29.0, batt=-300.0)
check("emergency SOC -> shore + lockout", s.state == "shore" and s.eng.st["lockoutUntil"] > s.now)

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
