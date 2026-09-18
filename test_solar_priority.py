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

T = [1_800_000_000.0]      # the wall clock: the equalization calendar only
M = [5_000.0]              # the monotonic clock: every duration in the driver
R.time = types.SimpleNamespace(time=lambda: T[0], monotonic=lambda: M[0])
drv = R.RecBmsDriver(rcfg)
batt = drv.batt
SOC = [62.0]


def live():
    drv.bms.update({
        "_lastUpdate": M[0], "socHiRes": SOC[0], "soc": int(SOC[0]),
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
        M[0] += dt
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
check("slider back in force after expiry",
      abs(batt["/RecBms/TargetChargeVoltage"] - (drv._slider_cvl(80) + rcfg.eq_boost)) < 0.006
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
drv.bms["_lastUpdate"] = M[0] - 100
check("refused while the BMS is stale", not batt.write("/RecBms/Sustain/Request", 1) and
      batt["/RecBms/Sustain/Status"] == "refused: BMS not live")
check("bad mode refused", not batt.write("/RecBms/Sustain/Request", 7))

# durations run on the monotonic clock: a wall-clock step (GPS/NTP sync
# after boot) neither keeps a hold alive nor ends it early
live()
check("floor for the clock-step check", batt.write("/RecBms/Sustain/Request", 1))
rtick()
T[0] -= 3600
rtick(n=100)
check("-1 h wall step: hold still in force at 101 s, countdown sane",
      batt["/RecBms/Sustain/Active"] == 1 and 0 < batt["/RecBms/Sustain/SecondsLeft"] <= 120,
      str(batt["/RecBms/Sustain/SecondsLeft"]))
T[0] += 7200
rtick(n=10)
check("+2 h wall step: hold not cut short at 111 s", batt["/RecBms/Sustain/Active"] == 1)
rtick(n=15)
check("hold expires at 120 monotonic seconds", batt["/RecBms/Sustain/Active"] == 0 and
      batt["/RecBms/Sustain/Status"].startswith("expired"))
drv.eq["active"] = False
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = T[0]

# telemetry quantisation: the voltage keeps the BMS's 0.01 V (DVCC hands it to
# the chargers as their sense), the rest moves in steps; control paths untouched
check("config: [publish] voltage at 0.01 V", rcfg.voltage_step == 0.01 and rcfg.current_step == 0.5)
drv.bms.update({"voltage": 56.637, "current": -1.27, "temperature": 21.3})
SOC[0] = 75.26
T[0] += 1; M[0] += 1
drv.bms.update({"_lastUpdate": M[0], "socHiRes": SOC[0]})
drv._tick()
check("voltage published at 0.01 V", batt["/Dc/0/Voltage"] == 56.64, str(batt["/Dc/0/Voltage"]))
check("current in 0.5 A steps", batt["/Dc/0/Current"] == -1.5, str(batt["/Dc/0/Current"]))
check("power in 10 W steps", batt["/Dc/0/Power"] == -70, str(batt["/Dc/0/Power"]))
check("SOC in 0.05 % steps (4.1.0: the HOLD rules read quarter points)", batt["/Soc"] == 75.25, str(batt["/Soc"]))
check("temperature in 0.5 degree steps", batt["/Dc/0/Temperature"] == 21.5, str(batt["/Dc/0/Temperature"]))
check("CVL is never quantised (no Solar Priority here: no gain, every charger on the slider's voltage)",
      batt["/Info/MaxChargeVoltage"] == curve(70) and batt["/RecBms/SolarLead"] == 0)
check("_q: None passes, step 0 is off, no float dust",
      R._q(None, 0.5) is None and R._q(1.2345, 0) == 1.2345 and R._q(0.30000000000000004, 0.1) == 0.3
      and R._q(1234.4, 60) == 1260 and isinstance(R._q(7.2, 1), int))

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
check("config: one-way tunables", scfg.engine["ONEWAY_ENTER_PCT"] == 2 and
      scfg.engine["ONEWAY_EXIT_PCT"] == 0.5)
check("engine version bumped", SP.ENGINE_VERSION == "4.5.2")
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
                      batt_v=56.6, cvl=56.62, target=None, qdc=None, dcl=None, pre=None)
        self.cmd, self.sustain = 0, 0
        self.cmds, self.sustains, self.boosts = [], [], []
        self.boost_cmds, self.boost_until, self.prefers = [], 0, []
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
            inp.load_avg = Val(v["lavg"] if v.get("lavg") is not None else v["load"], n)
            inp.feed = Val(v["feed"] if v.get("feed") is not None else (240 if self.cmd == 1 else 0), n)
            inp.ac_out = Val(v["load"], n)
            inp.voc6, inp.y6, inp.m6 = Val(v["voc"], n), Val(v["pv"], n), Val(v["m"], n)
            inp.voc7, inp.y7, inp.m7 = Val(0.0, n), Val(0.0, n), Val(0, n)
            inp.batt_v, inp.cvl = Val(v["batt_v"], n), Val(v["cvl"], n)
            inp.target_soc = Val(v["target"], n) if v["target"] is not None else None
            inp.q_dc = Val(v["qdc"], n) if v["qdc"] is not None else None
            inp.dc_sys = Val(v["dcl"], n) if v["dcl"] is not None else None
            inp.pre = Val(v["pre"], n) if v["pre"] is not None else None
            # dbus-recbms: a boost runs until released or for 120 s
            inp.boost_active = Val(1 if n < self.boost_until else 0, n)
            out = self.eng.tick(n, inp)
            if out.boost is not None:
                self.boost_cmds.append((n, out.boost))
                self.boost_until = n + 120000 if out.boost > 0 else 0
            self.prefers.append(out.prefer)
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

# ---- 4.3.1: the charger-quiet gate judges the Quattro, not the bank ----
s = Sim()
s.tick(335, soc=50, batt=350.0, qdc=21.0)      # the SUN fills the bank, Quattro at 0 A
check("sun charging the bank at +350 W, Quattro quiet: leaves shore",
      s.state == "probe" and s.cmd == 1, s.state)
s = Sim()
s.tick(30, soc=50, batt=350.0, qdc=21.0)
check("... and the status says whose charge it is", "[chg +350W solar]" in s.out.status_text, s.out.status_text)
s = Sim()
s.tick(335, soc=50, batt=350.0, qdc=600.0)     # the QUATTRO is charging
check("Quattro charging at +600 W: stays on shore", s.state == "shore" and s.cmd == 0, s.state)
check("... tagged as a charger's", "[chg +350W]" in s.out.status_text, s.out.status_text)
s = Sim()
s.tick(335, soc=50, batt=350.0)                # a vebus without /Dc/0/Power
check("no Quattro DC power published: the bank's power decides, as in 4.3", s.state == "shore", s.state)

# ---- charge one-way: 60 % -> 80 %, the 4.3 algorithm (hold_rules = 0) ----
s = Sim(HOLD_RULES=0)
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
check("charge: done within EXIT of the target", s.oneway is None and
      any("ONE-WAY charge done (SOC 79.5% at target 80%)" in l for l in s.logs))
check("hold_rules = 0: the 4.3 engine finishes the last bit from shore", s.sustain == 0 and not s.out.hold)
h = Sim()
h.tick(1, soc=60, target=80)
h.tick(1, soc=79.5)
check("the HOLD rules keep the floor (day/night not known yet): the last half point is the sun's",
      h.oneway is None and h.sustain == 1 and h.out.hold, "sustain %s" % h.sustain)

# ---- hysteresis and re-targeting ----
s = Sim()
s.tick(1, soc=79.2, target=80)
check("79.2 -> 80 is inside ENTER (2): HOLD", s.oneway is None)
s.tick(1, soc=77)
check("77 -> 80 engages: a slider step from the present SOC is a direction", s.oneway == "charge")
s.tick(1, soc=79.3)
check("79.3 -> 80 stays engaged (EXIT is 0.5)", s.oneway == "charge")
b = Sim()
b.tick(1, soc=50.3, target=55)
check("the boat, 2026-09-17: 50.3 -> 55 engages (4.7 points never did at ENTER 5)", b.oneway == "charge")
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
s.tick(5, load=3000.0, batt=-3000.0)
check("discharge: a 3 kW load -> suspend on shore", s.state == "suspend" and s.cmd == 0)
check("discharge: ceiling kept through suspend", s.sustain == 2)
s.tick(15, load=300.0, batt=-300.0)
check("discharge: resumes to solar without a boost", s.state == "solar" and s.cmd == 1 and
      s.boosts == [])
s.tick(1, soc=70.4)
check("discharge: done within EXIT of the target", s.oneway is None and s.sustain == 0)
s.tick(20)
check("discharge done at night: HOLD takes over and brings the boat home (nothing to harvest)",
      s.state == "shore" and s.out.hold and s.transitions[-1] == "-> SHORE (night)",
      "%s %s" % (s.state, s.transitions[-1:]))
s = Sim()
s.tick(1, soc=90, target=70, batt_v=60.3, cvl=60.3, voc=70.0)
s.tick(335, pv=0.0, m=0)
s.tick(300, batt=-600.0)
s.tick(1, soc=70.4)
s.tick(20)
check("discharge done by day: HOLD takes the island over, its backstop starting from here",
      s.state == "solar" and s.out.hold and s.eng.st["socEntry"] == 70.4 and 0 < s.out.deficit_wh < 5,
      "%s entry %s deficit %s" % (s.state, s.eng.st["socEntry"], s.out.deficit_wh))
h = Sim(HOLD_RULES=0)
h.tick(1, soc=90, target=70, batt_v=60.3, cvl=60.3)
h.tick(335, pv=0.0, m=0, voc=10.0)
h.tick(1, soc=70.4, batt=-300.0)
h.tick(120)
check("hold_rules = 0: the 4.3 deficit exit takes over instead", h.state == "shore" and h.cmd == 0, h.state)

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

# ================================================================ engine 4.4
print("\n=== solar priority engine: HOLD rules (4.4) ===")
t0 = dict(SP.ENGINE_DEFAULTS)
st = {"daylight": None, "lightSince": 0, "darkSince": 0}
D = SP.daylight_update
edges = [D(st, 1000 * k, 70.0, t0) for k in range(1, 700)]
check("dawn: once, after DAWN_MS of array voltage", edges.count("dawn") == 1 and edges.index("dawn") == 600 and st["daylight"] is True)
check("a cloud (55 V) changes nothing", D(st, 800000, 55.0, t0) is None and st["daylight"] is True)
check("a short dip under DUSK_V changes nothing", D(st, 801000, 40.0, t0) is None and D(st, 802000, 70.0, t0) is None and st["daylight"] is True)
edges = [D(st, 900000 + 1000 * k, 12.0, t0) for k in range(400)]
check("dusk: once, after DUSK_MS dark", edges.count("dusk") == 1 and edges.index("dusk") == 300 and st["daylight"] is False)
check("no array reporting: day/night kept", D(st, 2000000, None, t0) is None and st["daylight"] is False)
W = SP.prefer_wanted
check("prefer: solar by day, charge now at night", W(True, False, True) == 1 and W(True, False, False) == 0)
check("prefer: the safety charges now even by day", W(True, True, True) == 0)
check("prefer: left alone when off or when day/night is not known", W(False, False, True) is None and W(True, False, None) is None)


def day(sim, **kv):
    """a sunny day on shore, the Quattro reading prefer solar"""
    base = dict(soc=50.0, target=50, voc=70.0, m=2, pre=1, qdc=0.0, load=250.0, dcl=50.0, batt=0.0)
    base.update(kv)
    sim.tick(1, **base)


NEED = 50 + 120 + 250 / 0.95                       # DC loads + idle + AC load / efficiency = 433 W
s = Sim()
day(s, batt=600.0)                                 # 600 W reaching the bank: the arrays deliver 650 W
s.tick(590)
check("HOLD: nothing moves before the day is established", s.state == "shore" and s.out.daylight is None and s.prefers[-1] is None)
s.tick(60)
check("HOLD: sun over the need -> island, directly", s.state == "solar" and s.cmd == 1 and "probe" not in s.states, s.state)
check("HOLD: no boost, no sustain, prefer solar", s.boost_cmds == [] and s.sustain == 0 and s.prefers[-1] == 1)
check("HOLD: the departure states the math",
      any(tr.startswith("-> SOLAR (hold: solar 650W vs need 433W, predicted batt +217W") for tr in s.transitions), str(s.transitions))
check("HOLD: need = DC loads + idle + AC / efficiency", abs(s.eng.st["predW"] - (650 - NEED)) < 1, str(s.eng.st["predW"]))
s.tick(125, batt=200.0, qdc=-400.0)
check("HOLD: prediction checked against the island 2 min on",
      any(l.startswith("prediction check: predicted batt +217W, observed +2") for l in s.logs), str(s.logs[-3:]))

s = Sim(); day(s, batt=250.0); s.tick(700)
check("HOLD: 300 W of sun under 75 % of a 433 W need: stays", s.state == "shore" and s.cmd == 0)
s = Sim(); day(s, batt=300.0, soc=50.1); s.tick(700)
check("HOLD: 350 W is 81 % of the need, but the bank is only 0.1 over: stays", s.state == "shore")
s = Sim(); day(s, batt=300.0, soc=50.3); s.tick(700)
check("HOLD: 81 % of the need with the bank 0.3 over: island, the band pays the rest",
      s.state == "solar" and any("81% of it, bank over target" in tr for tr in s.transitions), str(s.transitions))
s = Sim(); day(s, batt=600.0, qdc=500.0); s.tick(700)
check("HOLD: what the Quattro puts into the bank is not the sun's", s.state == "shore")
s = Sim(); day(s, batt=4000.0, load=3000.0); s.tick(700)
check("HOLD: never leaves under a suspend-class load, even with the sun over the need", s.state == "shore")
s = Sim(); day(s, batt=600.0, feed=240); s.tick(700)
check("HOLD: never leaves while shore itself reads absent", s.state == "shore")
s = Sim(MIN_SOC=50.5); day(s, batt=600.0); s.tick(700)
check("HOLD: never leaves under MIN_SOC", s.state == "shore")
s = Sim(); day(s, batt=600.0, dcl=None); s.tick(700)
check("HOLD: no DC-load reading: still decides (on bank power and the inverter's draw)", s.state == "solar")

# night, and the floor
s = Sim(); day(s, batt=-50.0, voc=10.0, m=0, soc=49.6, pre=0); s.tick(320)
check("night: charge now wanted, no departure", s.out.daylight is False and s.prefers[-1] == 0 and s.state == "shore")
check("night under the target: floor -- shore holds the bank, never raises it", s.sustain == 1)
s.tick(5, pre=1)
check("... whatever the toggle reads: it is the night that asks for it", s.sustain == 1)
s.tick(2, pre=0)
s.tick(5, soc=50.2)
check("night over the target: no floor needed", s.sustain == 0)
s = Sim(); day(s, batt=100.0, soc=49.6, pre=0); s.tick(700)
check("day, but the toggle still reads charge now: the floor stays", s.out.daylight is True and s.sustain == 1)
s.tick(2, pre=1)
check("... released once it reads prefer solar", s.sustain == 0)
s = Sim(); day(s, batt=100.0, soc=49.6, pre=None); s.tick(700)
check("a toggle that cannot be read: the floor stays by day too", s.sustain == 1)

# the island's deficit is an energy
s = Sim(); day(s, batt=600.0); s.tick(700)
budget = 0.5 / 100 * 1440 * 56.6
s.tick(3000, batt=-300.0)
check("island: 250 Wh drawn, still out", s.state == "solar" and abs(s.out.deficit_wh - 250) < 3, str(s.out.deficit_wh))
s.tick(1600, batt=600.0)
check("island: the sun repays it, never under zero", s.out.deficit_wh == 0.0)
s.tick(3000, batt=-300.0)
check("island: a second cloud starts from zero", s.state == "solar" and abs(s.out.deficit_wh - 250) < 3)
s.tick(2000, batt=-300.0)
check("island: %.0f Wh (0.5 %% of the bank) below its best -> shore" % budget,
      s.state == "shore" and any(tr.startswith("-> SHORE (deficit: 4") for tr in s.transitions), str(s.transitions[-1:]))
check("... with a backoff, since no probe proves the next departure", s.eng.st["backoffUntil"] > s.now)
s.tick(100, batt=600.0)
check("the sun is back, but the backoff holds the boat on shore", s.state == "shore" and "[backoff" in s.out.status_text, s.out.status_text)
s.tick(300)
check("... and lets it go once it has run out", s.state == "solar")
check("a HOLD departure does not wipe the backoff it has earned (no probe proved it)",
      s.eng.st["backoffMs"] == 2 * s.t["COOLDOWN_MS"], str(s.eng.st["backoffMs"]))
s = Sim(); day(s, batt=600.0); s.tick(700)
s.tick(6000, batt=-300.0, m=1)
check("island: nothing is exempt -- a drain with an array reading 'limited' still counts", s.state == "shore")
s = Sim(); day(s, batt=600.0); s.tick(700)
s.tick(60, batt=-300.0, soc=49.5)
check("island: no floor while away, even under the target", s.state == "solar" and s.sustain == 0)
s.now += 3600000
s.tick(1)
check("island: a stalled clock integrates five seconds, not the hour", s.out.deficit_wh < 7, str(s.out.deficit_wh))
s.tick(2, soc=47.9)
check("island: the SOC drift backstop still stands", s.state == "shore" and "SOC 47.9%" in s.transitions[-1], str(s.transitions[-1:]))
s = Sim(); day(s, batt=600.0); s.tick(700)
s.tick(400, batt=-300.0, m=0, pv=0.0)
check("island: a dark spell by day (arrays at 0 W, voltage up) rides on the budget",
      s.state == "solar" and s.out.daylight is True and s.out.deficit_wh > 0)
wh0 = s.out.deficit_wh
s.tick(120, load=1700.0, batt=-1700.0)
check("island: the water heater (1.7 kW, 2 min) is carried by the budget, not by a relay",
      s.state == "solar" and s.cmd == 1 and abs(s.out.deficit_wh - wh0 - 57) < 2, "%s %.0f" % (s.state, s.out.deficit_wh - wh0))
s.tick(5, load=3000.0, batt=-3000.0)
check("island: 3 kW -> suspend", s.state == "suspend" and s.cmd == 0)
wh = s.out.deficit_wh
s.tick(15, load=250.0, batt=0.0)
check("island: resumes, the deficit stands and shore time is not counted", s.state == "solar" and abs(s.out.deficit_wh - wh) < 1)

# 4.5.2: night ends the island; a suspend at night ends on shore
s = Sim(); day(s, batt=600.0); s.tick(700)
s.tick(299, batt=-300.0, m=0, voc=10.0, pv=0.0)
check("dusk: the budget carries the taper until night is declared", s.state == "solar" and s.out.daylight is True and s.out.deficit_wh > 0)
b0 = s.eng.st["backoffUntil"]
s.tick(2)
check("night declared: the island ends -- nothing to harvest",
      s.state == "shore" and s.cmd == 0 and s.out.daylight is False and s.transitions[-1] == "-> SHORE (night)", str(s.transitions[-1:]))
check("... without a backoff: dawn must not wait on it", s.eng.st["backoffUntil"] == b0)
check("... and the deficit ledger is cleared with the island", s.out.deficit_wh == 0.0)
s = Sim(); day(s, batt=600.0); s.tick(700)
s.tick(5, load=3000.0, lavg=250.0, batt=-3000.0)      # the 60 s mean is still the base
check("island: 3 kW -> suspend (day)", s.state == "suspend" and s.cmd == 0)
islands = sum(1 for _, c in s.cmds if c == 1)
s.tick(310, voc=10.0, pv=0.0, load=3000.0, batt=0.0, lavg=3000.0)
check("suspend that runs into the night: shore, no island to resume",
      s.state == "shore" and s.cmd == 0 and "night" in s.transitions[-1], str(s.transitions[-1:]))
check("... which moved no relay (the suspend was on shore already)", sum(1 for _, c in s.cmds if c == 1) == islands)
s.tick(60, load=250.0, lavg=None)
check("... and the load dropping afterwards resumes nothing", s.state == "shore" and s.cmd == 0)

# the probe
s = Sim(); day(s, batt=-20.0, m=1, soc=50.9); s.tick(340)
check("limited arrays before the day is known: no probe", s.boost_cmds == [])
s.tick(300)
check("probe: an array limited on shore by day -> the boost lifts the MPPTs' target",
      len(s.boost_cmds) == 1 and s.boost_cmds[0][1] == s.t["BOOST_V"] and s.state == "shore", str(s.boost_cmds))
s.tick(45, batt=700.0, m=2)
check("probe: the sun covers the need -> leaves at once and releases the boost",
      s.state == "solar" and s.boost_cmds[-1][1] == 0 and s.boost_cmds[-1][0] - s.boost_cmds[0][0] <= 60000, str(s.boost_cmds))
s = Sim(); day(s, batt=-20.0, m=1, soc=50.1); s.tick(640)
s.tick(60, batt=150.0, m=2)
check("probe: not limited, output flat, need not met -> released, stays",
      s.state == "shore" and s.boost_cmds[-1][1] == 0 and len(s.boost_cmds) == 2
      and any(l.startswith("hold probe done") for l in s.logs), str(s.boost_cmds))
s.tick(1500, batt=-20.0, m=1)
check("probe: not again inside 30 minutes", len(s.boost_cmds) == 2)
s.tick(400)
check("probe: again after 30 minutes", len(s.boost_cmds) == 3 and s.boost_cmds[-1][1] > 0)
s = Sim(); day(s, batt=-20.0, m=1, soc=50.1); s.tick(640)
for k in range(12):                                 # a tracker waking slowly: reads "tracking", output still rising
    s.tick(10, batt=20.0 + 25 * k, m=2)
check("probe: output still rising -> not cut short (a parked tracker reads 'not limited')",
      [c for c in s.boost_cmds if c[1] == 0] == [], str(s.boost_cmds))
s.tick(15)
check("... dbus-recbms ends the boost at 120 s", any(l.startswith("hold probe over (boost ended)") for l in s.logs))

# a probe whose branch stops running lets go of its boost
s = Sim(); day(s, batt=-20.0, m=1, soc=50.1); s.tick(640)
s.inp.enabled = False
s.tick(1)
check("probe: Solar Priority switched off mid-probe -> boost released at once",
      s.boost_cmds[-1][1] == 0 and s.eng.st["holdProbe"] == 0, str(s.boost_cmds))

# hold_rules = 0 is the 4.3 engine: no safety override, the toggle left alone
h = Sim(HOLD_RULES=0); day(h, soc=24.0, target=40, batt=100.0); h.tick(700)
check("hold_rules = 0: one-way charge keeps its floor under 25 %, the toggle is the owner's",
      h.oneway == "charge" and h.sustain == 1 and set(h.prefers) == {None})

# the safety
s = Sim(); day(s, soc=24.0, target=40, batt=100.0); s.tick(700)
check("safety under 25 %: charge now by day, and no floor in the charger's way",
      s.oneway == "charge" and s.prefers[-1] == 0 and s.sustain == 0)
s.tick(2, soc=26.0)
check("safety holds until 27 %", s.prefers[-1] == 0 and s.sustain == 0)
s.tick(2, soc=27.5, pre=0)                      # the toggle still reads the safety's own "charge now"
check("safety over: prefer solar wanted again, the floor back until the toggle reads it",
      s.prefers[-1] == 1 and s.sustain == 1 and not s.eng.st["ownerCharge"])
s.tick(2, pre=1)
check("... and by day under prefer solar the sun charges with no floor in its way", s.sustain == 0)

# ---- 4.5: charging toward a far target runs the same rules ----
print("\n=== solar priority engine: charging under the HOLD rules (4.5) ===")
s = Sim(); day(s, soc=50.0, target=80, batt=600.0); s.tick(700)
check("charge 50 -> 80: leaves shore when the sun covers the need -- no trial, no boost",
      s.state == "solar" and "probe" not in s.states and s.boost_cmds == [], "%s %s" % (s.state, s.boost_cmds))
check("... the status line still names the direction", s.out.status_text.startswith("1-WAY CHARGE 50->80% | SOLAR"), s.out.status_text)
check("... the floor stood only until the day was established, then prefer solar made it needless",
      s.sustains[-1][1] == 0 and s.sustains[-1][0] - s.sustains[0][0] == 600000 and s.sustain == 0, str(s.sustains[-2:]))
s.tick(200, batt=-65.0)
check("charge: -65 W for 200 s is a cloud, not a reason to come back (4.3 came back after 105 s)", s.state == "solar")
s.tick(6000, batt=-300.0)
check("charge: the deficit budget brings it back", s.state == "shore" and "deficit:" in s.transitions[-1], str(s.transitions[-1:]))
s = Sim(); day(s, soc=50.0, target=80, batt=300.0); s.tick(700)
check("charge: 81 % of the need is not enough under the target -- no deliberate deficit while charging", s.state == "shore")
s = Sim(); day(s, soc=50.0, target=80, batt=-50.0, voc=10.0, m=0, pre=0); s.tick(320)
check("charge at night: floor -- shore holds the bank, the sun does the charging", s.sustain == 1 and s.prefers[-1] == 0)
s = Sim(); day(s, soc=30.0, target=80, batt=600.0); s.tick(700)
check("charge under MIN_SOC: stays on shore, the sun still charges with no floor", s.state == "shore" and s.sustain == 0)

# ---- the owner's Charge now ----
s = Sim(); day(s, soc=50.0, target=80, batt=100.0); s.tick(640)
check("before: prefer solar wanted and read", s.prefers[-1] == 1 and not s.eng.st["ownerCharge"])
s.tick(2, pre=0, batt=2600.0, qdc=2000.0)
check("owner clicks Charge now by day: the toggle is left alone, no floor",
      s.eng.st["ownerCharge"] and s.prefers[-1] is None and s.sustain == 0
      and any(l.startswith("OWNER'S CHARGE NOW") for l in s.logs))
s.tick(400, batt=2600.0, qdc=1500.0)              # the sun alone covers the need: 1150 W vs 433 W
check("... and the boat stays on shore so the charger can work", s.state == "shore" and "[CHARGE NOW: owner]" in s.out.status_text, s.out.status_text)
s.tick(320, voc=10.0, m=0, batt=2000.0, qdc=2000.0)
check("... through the night, still with no floor", s.out.daylight is False and s.sustain == 0 and s.eng.st["ownerCharge"])
s.tick(2, soc=79.96)
check("... until the bank is at the target: then the toggle is ours again", not s.eng.st["ownerCharge"] and s.prefers[-1] == 0)
s = Sim(); day(s, soc=50.0, target=80, batt=100.0); s.tick(640)
s.tick(2, pre=2); s.tick(2, pre=1)
check("owner flips it back to prefer solar: over", not s.eng.st["ownerCharge"] and s.prefers[-1] == 1
      and any(l.startswith("owner's charge now over (toggle back") for l in s.logs))
s = Sim(); day(s, soc=50.0, target=80, batt=600.0); s.tick(700)
s.tick(2, pre=0)
check("clicked while on the island: back to shore for the charger", s.state == "shore" and s.transitions[-1] == "-> SHORE (owner's charge now)")
s = Sim(); day(s, soc=50.0, target=80, batt=100.0, pre=0); s.tick(700)
check("a dawn write that never landed is not the owner: floor kept, prefer solar still wanted",
      not s.eng.st["ownerCharge"] and s.sustain == 1 and s.prefers[-1] == 1)

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
