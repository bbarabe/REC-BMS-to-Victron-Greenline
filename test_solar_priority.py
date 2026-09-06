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
check("floor: CVL = curve(present SOC + one snap)", batt["/RecBms/TargetChargeVoltage"] == curve(63),
      "%s vs %s" % (batt["/RecBms/TargetChargeVoltage"], curve(63)))
check("floor telemetry", batt["/RecBms/Sustain/Active"] == 1 and
      batt["/RecBms/Sustain/Mode"] == 1 and batt["/RecBms/Sustain/Soc"] == 63.0 and
      batt["/RecBms/Sustain/Status"] == "floor")
rtick(soc=63.9)
check("floor ignores a wobble under one step past the snap",
      batt["/RecBms/TargetChargeVoltage"] == curve(63) and batt["/RecBms/Sustain/Soc"] == 63.0)
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
check("ceiling: CVL = curve(present SOC - one snap)", batt["/RecBms/TargetChargeVoltage"] == curve(89) and
      batt["/RecBms/Sustain/Mode"] == 2 and batt["/RecBms/Sustain/Status"] == "ceiling")
rtick(soc=88.3)
check("ceiling ignores a wobble under one step past the snap", batt["/RecBms/TargetChargeVoltage"] == curve(89))
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
      drv.sustain["soc"] == 76.0 and batt["/RecBms/Sustain/Soc"] == 70.0 and
      batt["/RecBms/TargetChargeVoltage"] == curve(70),
      "floor 75 clipped to slider 70 -> %s" % batt["/RecBms/TargetChargeVoltage"])
check("release accepted", batt.write("/RecBms/Sustain/Request", 0))
rtick()
check("released: slider CVL, telemetry cleared", batt["/RecBms/TargetChargeVoltage"] == curve(70) and
      batt["/RecBms/Sustain/Active"] == 0 and batt["/RecBms/Sustain/Soc"] is None)

# charge current limit while held: PV current + charge_limit_a, MPPT-safe
rtick(soc=62, slider=80)
check("ccl: full REC limit without a hold", batt["/Info/MaxChargeCurrent"] == 200.0)
drv.pv_current = (3.0, T[0])
check("ccl: floor request", batt.write("/RecBms/Sustain/Request", 1))
rtick()
check("ccl: PV 3 A + 5 A while held", batt["/Info/MaxChargeCurrent"] == 8.0 and
      batt["/RecBms/Sustain/ChargeLimit"] == 8.0, str(batt["/Info/MaxChargeCurrent"]))
drv.pv_current = (20.0, T[0])
rtick()
check("ccl: follows PV up", batt["/Info/MaxChargeCurrent"] == 25.0)
drv.pv_current = (None, T[0])
rtick()
check("ccl: not applied when PV current is unknown", batt["/Info/MaxChargeCurrent"] == 200.0 and
      batt["/RecBms/Sustain/ChargeLimit"] is None)
drv.pv_current = (3.0, T[0] - 60)
rtick()
check("ccl: not applied on a stale PV reading", batt["/Info/MaxChargeCurrent"] == 200.0)
drv.pv_current = (3.0, T[0])
drv.boost["active"] = True
rtick()
check("ccl: lifted during a boost", batt["/Info/MaxChargeCurrent"] == 200.0)
drv.boost["active"] = False
batt.write("/RecBms/Sustain/Request", 0)
rtick()
check("ccl: full limit back once released", batt["/Info/MaxChargeCurrent"] == 200.0 and
      batt["/RecBms/Sustain/ChargeLimit"] is None)

# stale BMS: no present SOC to pin -> pending, sampled on the first live tick
drv.bms["_lastUpdate"] = T[0] - 100
check("pending while the BMS is stale", batt.write("/RecBms/Sustain/Request", 1) and
      batt["/RecBms/Sustain/Status"] == "pending: no SOC yet" and batt["/RecBms/Sustain/Active"] == 1)
T[0] += 1; drv._tick()                                  # still stale: slider applies, hold waits
check("pending: slider CVL meanwhile", batt["/RecBms/TargetChargeVoltage"] == curve(80) and
      batt["/RecBms/Sustain/Soc"] is None,
      "cvl %s vs %s, soc %s, status %s" % (batt["/RecBms/TargetChargeVoltage"], curve(80),
                                          batt["/RecBms/Sustain/Soc"], batt["/RecBms/Sustain/Status"]))
rtick(soc=64)                                           # BMS back: sampled and snapped
check("pending hold starts on the first live tick", batt["/RecBms/Sustain/Status"] == "floor" and
      batt["/RecBms/Sustain/Soc"] == 65.0 and batt["/RecBms/TargetChargeVoltage"] == curve(65))
batt.write("/RecBms/Sustain/Request", 0); rtick()
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
      scfg.engine["ONEWAY_EXIT_PCT"] == 1 and scfg.engine["ONEWAY_FULL_PCT"] == 100)
check("engine version bumped", SP.ENGINE_VERSION == "4.5")
Val = SP.Val


class Sim:
    """Drives Engine.tick with a plant that follows the commands: the
    Quattro's ActiveInput reports 240 (none) once IgnoreAcIn is 1, and
    also whenever there is no shore power at all (shore=False)."""

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
            inp.load_avg = Val(v.get("load_avg", v["load"]), n)
            inp.load_slow = Val(v.get("load_slow", v["load"]), n)
            inp.feed = Val(240 if (self.cmd == 1 or not v.get("shore", True)) else 0, n)
            inp.ac_out = Val(v["load"], n)
            inp.voc6, inp.y6, inp.m6 = Val(v["voc"], n), Val(v["pv"], n), Val(v["m"], n)
            inp.voc7, inp.y7, inp.m7 = Val(0.0, n), Val(0.0, n), Val(0, n)
            inp.batt_v, inp.cvl = Val(v["batt_v"], n), Val(v["cvl"], n)
            inp.dc_load = Val(v.get("dc_load", 0.0), n)
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
# night: PV gone, the loads draw from the bank -> ten-minute deficit exit -> shore + sustain
s.tick(200, batt=-200.0, pv=0.0, m=0, voc=10.0)
check("charge: -200 W is inside the one-way tolerance", s.state == "solar")
s.tick(720, batt=-400.0)
check("charge: deficit -> shore", s.state == "shore" and s.cmd == 0, s.state)
check("charge: floor requested one tick after the shore command (once the Quattro reports shore)",
      s.sustain == 1 and s.sustains[-1][0] == s.cmds[-1][0] + 1000,
      "sustain %s cmd %s" % (s.sustains[-1], s.cmds[-1]))
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

# ---- pre-probe checks (4.4) ----
def prime(s, c6, c7, m6=None, m7=None, age=0):
    """Plant captures (aged `age` ms) and fresh model estimates in the engine."""
    ts = s.now - age
    s.eng.st["cap6"] = {"w": c6, "ts": ts} if c6 is not None else None
    s.eng.st["cap7"] = {"w": c7, "ts": ts} if c7 is not None else None
    for k, m in (("mdl6", m6), ("mdl7", m7)):
        s.eng.st[k] = {"voc": 70.0, "vocTs": s.now, "kff": 0.78,
                       "est": ({"w": m, "ts": s.now, "lb": False} if m else None)}

s = Sim()
s.tick(5, pv=60.0, m=1, batt_v=56.4)            # throttled: no capture from the plant itself
prime(s, 100, 80, m6=700, m7=700)
s.tick(1)
check("fresh capture caps the model", abs(s.out.est - 180) < 1, "est %.0f" % s.out.est)
prime(s, 100, 80, m6=700, m7=700, age=16 * 60 * 1000)
s.tick(1)
# MPPT 6's model re-derives ~60 W from the live yield; MPPT 7 keeps the planted 700 W
check("stale capture: the model applies again", 750 < s.out.est < 850, "est %.0f" % s.out.est)

s = Sim()
s.tick(1, pv=60.0, m=1, batt_v=56.4)
prime(s, 300, 20)                                # brow 300/0.35=857, fly 20/0.65=31 -> 0.036
s.tick(340)
check("obstructed array: no probe", s.state == "shore" and "[SHADE]" in s.out.status_text and "bal 0.04" in s.out.status_text,
      s.out.status_text)
prime(s, 300, 400)                               # 857 vs 615 -> 0.72
s.tick(35)
check("balanced arrays: probe goes ahead", s.state == "probe", s.state)
check("probe entry logs cap/mdl/bal", any("-> PROBE (est" in tr and "cap 300+400" in tr and "bal 0.72" in tr for tr in s.transitions),
      str(s.transitions))

s = Sim()
s.tick(1, pv=60.0, m=1, batt_v=56.4, load=250.0, load_slow=330.0)
prime(s, 200, 150)                               # est 350: clears 1.2*250=300, not 1.2*330=396
s.tick(340)
check("need judged against the slower average too", s.state == "shore" and "need 396W" in s.out.status_text,
      s.out.status_text)
s.tick(35, load_slow=250.0)
check("...and clears once the slow average drops", s.state == "probe")

# ---- one-way charge is patient with a deficit, and with a probe ----
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340)
check("patient: probe", s.state == "probe")
s.tick(50, batt=100.0, cvl=59.49, load=350.0)   # avg*1.2 = 420 > est: 4.2 would have quit
check("patient: a creeping load does not end a one-way probe",
      not any("big load" in tr for tr in s.transitions), str(s.transitions))
s.tick(50, batt=-120.0)                          # verdict on the last 15 s: -120 W is inside 200 W
check("patient: probe verdict uses the one-way tolerance", s.state == "solar", s.state)
s.tick(300, batt=-150.0)
check("patient: -150 W for 5 min stays on solar", s.state == "solar")
s.tick(10, batt=-600.0)
check("patient: a 10 s surge does not end it", s.state == "solar")
s.tick(700, batt=-300.0)
check("patient: -300 W ten-minute mean -> shore", any(tr.startswith("-> SHORE (deficit: batt avg -") for tr in s.transitions),
      str(s.transitions))
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
s.tick(5, load=1500.0, batt=-1500.0)
check("patient: heater-class load still suspends", s.state == "suspend")

# ---- solar filling the band is not "the charger charging" ----
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340, pv=345.0, m=2, batt=250.0, load=178.0, dc_load=85.0)   # bank +250 W, all of it solar
check("solar charging the band does not block the probe", s.state == "probe", s.state)
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340, pv=0.0, m=1, batt=250.0, load=178.0)                   # bank +250 W from the Quattro
check("the Quattro charging still does", s.state == "shore" and "[chg +250W]" in s.out.status_text, s.out.status_text)

# ---- 4.5: 100 % means a full charge from every charger, not one-way ----
s = Sim()
s.tick(1, soc=60, target=100)
check("full: 60 -> 100 does not engage one-way", s.oneway is None and s.out.oneway == "")
check("full: no floor ever asked for", s.sustains == [])
s.tick(335, batt_v=56.4)
check("full: the normal engine probes as usual", s.state == "probe")
s.tick(1, target=90)
check("full: 90 engages", s.oneway == "charge")
s.tick(1, target=100)
check("full: back to 100 stands one-way down", s.oneway is None and
      any("done (target 100% is a full charge: every charger at its maximum)" in l for l in s.logs),
      str(s.logs[-1:]))
s = Sim(ONEWAY_FULL_PCT=0)
s.tick(1, soc=60, target=100)
check("full: oneway_full_pct = 0 restores the 4.4 behaviour", s.oneway == "charge")

# ---- 4.5: no floor while the Quattro is not actually on shore ----
s = Sim()
s.tick(1, soc=80, target=95, shore=False)
check("no shore: charge engaged", s.oneway == "charge")
check("no shore: floor never requested on shore state", s.sustain == 0 and 1 not in [x for _, x in s.sustains],
      str(s.sustains))
s.tick(200)
check("no shore: engine sits in NO SHORE?, still no floor",
      s.state == "shore" and "NO SHORE?" in s.out.status_text and s.sustain == 0, s.out.status_text)
s.tick(1, shore=True)
check("shore back: floor requested on the next tick", s.sustain == 1 and s.sustains[-1][0] == s.now)
# the 2026-09-06 case: on solar, heater-class load, shore gone meanwhile
s = Sim()
s.tick(1, soc=80, target=95, batt_v=56.4)
check("09-06: floor while on shore", s.sustain == 1)
s.tick(335)
s.tick(95, batt=100.0, cvl=59.49)
check("09-06: on solar, floor released", s.state == "solar" and s.sustain == 0)
s.tick(5, load=1500.0, load_avg=300.0, batt=-1500.0, shore=False)   # avg lags: base 300 W
check("09-06: heater load -> suspend", s.state == "suspend" and s.cmd == 0)
check("09-06: no AC came: NO floor in suspend", s.sustain == 0, str(s.sustains[-3:]))
s.tick(200)
check("09-06: still none 200 s in", s.sustain == 0 and s.state == "suspend")
s.tick(1300, load=1500.0, batt=-1500.0)
check("09-06: suspend times out to shore, still no floor",
      s.state == "shore" and s.sustain == 0, "state %s sustain %s" % (s.state, s.sustain))
s.tick(1, shore=True)
check("09-06: shore returns: floor follows within a tick", s.sustain == 1)
s.tick(5, shore=False)
check("09-06: shore drops again: floor released", s.sustain == 0)
# and the same suspend with shore present keeps the floor (4.3 behaviour)
s = Sim()
s.tick(1, soc=80, target=95, batt_v=56.4)
s.tick(335)
s.tick(95, batt=100.0, cvl=59.49)
s.tick(5, load=1500.0, batt=-1500.0)
check("shore present: suspend still gets the floor", s.state == "suspend" and s.sustain == 1)

# ---- a re-appeared battery service gets the hold re-asserted at once ----
s = Sim()
s.tick(1, soc=60, target=80)
n0 = len(s.sustains)
s.tick(5)
check("no re-assert inside the 30 s cycle", len(s.sustains) == n0)
s.eng.st["sustainSent"] = None            # what _device_added does for the battery service
s.tick(1)
check("battery service re-appeared: floor re-asserted next tick", len(s.sustains) == n0 + 1 and s.sustain == 1)

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
