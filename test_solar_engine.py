#!/usr/bin/env python3
"""Committed engine 4.9 behavior tests, with no D-Bus or driver stubs."""
import sys
import types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "dbus-recbms"))
import solar_engine as SP
ok, fail = [], []
def check(name, condition, detail=""):
    (ok if condition else fail).append(name)
    if not condition:
        print("FAIL:", name, detail)

# ================================================================ engine 4.3
print("\n=== solar priority engine: one-way ===")
scfg = types.SimpleNamespace(engine=SP.ENGINE_DEFAULTS)
check("config: one-way tunables", scfg.engine["ONEWAY_ENTER_PCT"] == 5 and
      scfg.engine["ONEWAY_EXIT_PCT"] == 1 and scfg.engine["ONEWAY_FULL_PCT"] == 100 and
      scfg.engine["ONEWAY_MIN_SOC"] == 25 and scfg.engine["ONEWAY_DEFICIT_W"] == 50 and
      scfg.engine["ONEWAY_DEFICIT_MS"] == 180000)
check("engine version bumped", SP.ENGINE_VERSION == "4.9")
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
            inp.quattro_w = Val(v.get("quattro_w", v["batt"] + max(0, v.get("dc_load", 0)) - v["pv"]), n)
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
check("charge: aboveCvl neither burns down nor blocks leaving shore; unthrottled arrays -> straight to solar",
      s.state == "solar" and s.cmd == 1 and "probe" not in s.states and
      any(tr.startswith("-> SOLAR (one-way charge on a live capture") for tr in s.transitions),
      "state %s %s" % (s.state, s.transitions[-1:]))
check("charge: sustain released while shore is off", s.sustain == 0 and s.sustains[-1][1] == 0)
check("charge: no probe boost, no measurement boost on unthrottled arrays", s.boosts == [], str(s.boosts))
s.tick(95, batt=100.0, cvl=59.49)       # hold released: the real target is back
check("charge: still on solar", s.state == "solar" and s.sustain == 0)
s.tick(60, soc=68.0)
check("charge: solar stays while the bank fills", s.state == "solar" and s.oneway == "charge")
# night: PV gone, the loads draw from the bank -> three-minute deficit exit -> shore + sustain
s.tick(200, batt=-40.0, pv=0.0, m=0, voc=10.0)
check("charge: -40 W is inside the one-way tolerance", s.state == "solar")
s.tick(240, batt=-100.0)
check("charge: -100 W three-minute mean -> shore", s.state == "shore" and s.cmd == 0, s.state)
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
s.tick(1, soc=60, target=95, batt_v=56.4, m=1)   # throttled arrays: the probe still runs
s.tick(340)
check("patient: probe when an array is throttled", s.state == "probe", s.state)
s.tick(50, batt=100.0, cvl=59.49, load=350.0)   # avg*1.2 = 420 > est: 4.2 would have quit
check("patient: a creeping load does not end a one-way probe",
      not any("big load" in tr for tr in s.transitions), str(s.transitions))
s.tick(50, batt=-40.0)                           # verdict on the last 15 s: -40 W is inside 50 W
check("patient: probe verdict uses the one-way tolerance", s.state == "solar", s.state)
s.tick(300, batt=-40.0)
check("patient: -40 W for 5 min stays on solar", s.state == "solar")
s.tick(10, batt=-600.0)
check("patient: a 10 s surge does not end it", s.state == "solar")
s.tick(300, batt=-100.0)
check("4.8: -100 W three-minute mean -> shore", any(tr.startswith("-> SHORE (deficit: batt avg -") for tr in s.transitions),
      str(s.transitions))
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4, m=1)
s.tick(340)
s.tick(100, batt=-120.0, cvl=59.49)              # the whole ramp drains: not a stint to start
check("4.8: probe verdict refuses a draining stint", s.state == "shore" and
      any("probe failed" in tr for tr in s.transitions), str(s.transitions[-1:]))
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4)         # unthrottled: straight in, the deficit exit guards
s.tick(340)
check("4.9: straight to solar", s.state == "solar" and "probe" not in s.states)
s.tick(300, batt=-120.0, cvl=59.49)
check("4.9: a draining stint ends by the deficit exit", s.state == "shore" and
      any("deficit" in tr for tr in s.transitions), str(s.transitions[-1:]))
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
check("solar charging the band does not block leaving shore", s.state == "solar", s.state)
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

# ---- 4.6: one-way charge leaves shore under min_soc, above oneway_min_soc ----
s = Sim()
s.tick(1, soc=30.0, target=60, batt_v=54.4, cvl=54.69, batt=300.0)
check("4.6: 30 -> 60 engages one-way charge with the floor", s.oneway == "charge" and s.sustain == 1)
s.tick(340)
check("4.6: leaves shore at 30 % (min_soc 40 no longer gates one-way charge)", s.state == "solar", s.state)
s.tick(95, batt=100.0, cvl=56.42)
check("4.6: on solar at 30 %, floor released, real target", s.state == "solar" and s.sustain == 0)
s.tick(120, soc=29.0, batt=150.0)
check("4.6: 29 % while the sun carries the bank: no emergency", s.state == "solar" and
      s.eng.st["lockoutUntil"] == 0, s.state)
s.tick(700, soc=28.5, batt=-100.0)               # mean turns negative under 30 %
check("4.6: 28.5 % with the bank draining: emergency lockout", s.state == "shore" and
      s.eng.st["lockoutUntil"] > s.now and any("EMERGENCY SOC" in tr for tr in s.transitions), str(s.transitions[-1:]))
s = Sim()
s.tick(1, soc=27.0, target=60, batt_v=54.2, cvl=54.5, batt=300.0)
s.tick(340)
check("4.6: 27 % still leaves shore (above oneway_min_soc)", s.state == "solar", s.state)
s.tick(95, batt=100.0, cvl=56.42)
s.tick(1, soc=24.9, batt=150.0)
check("4.6: under oneway_min_soc the emergency lockout fires even with the sun carrying the bank",
      s.state == "shore" and s.eng.st["lockoutUntil"] > s.now and "EMERGENCY SOC 24.9%" in (s.out.transition or ""),
      str(s.out.transition))
s = Sim()
s.tick(1, soc=24.0, target=60, batt_v=54.0, cvl=54.3, batt=300.0)
s.tick(340)
check("4.6: 24 % does not leave shore", s.state == "shore", s.state)
s = Sim()
s.tick(341, soc=30.0, batt_v=54.4, cvl=54.69)     # no target: the normal engine keeps min_soc 40
check("4.6: without one-way, 30 % still stays on shore", s.state == "shore", s.state)
s = Sim()
s.tick(1, soc=35.0, target=100, batt_v=54.9, cvl=55.2)
s.tick(340)
check("4.6: a full charge (100 %) is not one-way and keeps min_soc", s.state == "shore", s.state)

# ---- 4.9: no measurement boost while both arrays are unthrottled ----
s = Sim()
s.tick(400, soc=60, pv=500.0, m=2, load=1000.0, batt_v=56.4)   # on shore (need 1200 > est), tracker active
check("4.9: no measurement boost while the arrays are unthrottled", s.boosts == [] and s.state == "shore", str(s.boosts))
s.tick(400, m=1)
check("4.9: measurement boost once an array is throttled", s.boosts and s.boosts[-1] == s.t["BOOST_V"] and s.state == "shore", str(s.boosts))
s = Sim(ONEWAY_SKIP_PROBE=0)
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
check("4.9: oneway_skip_probe = 0 keeps the probe", s.state == "probe", s.state)

# ---- 4.7: no measurement boost without real PV ----
s = Sim()
s.tick(400, soc=60, target=80, pv=0.0, m=1, voc=62.0, batt_v=56.4)   # dusk: Voc up, yield nil
check("4.7: no boost on an open-circuit voltage with no yield", s.boosts == [], str(s.boosts))
s.tick(400, pv=60.0)
check("4.7: boost once PV is flowing", s.boosts and s.boosts[-1] == s.t["BOOST_V"], str(s.boosts))

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

# Current integration corrections, separate from committed behavior assertions.
s = Sim(ONEWAY_SKIP_PROBE=0)
s.inp.departure_allowed = False
s.tick(700, target=80)
check("readiness veto keeps shore with floor", s.state == "shore" and s.out.charge_intent == "floor")
check("readiness veto does not start probe clock", s.eng.st["probeStart"] == 0)
s.inp.departure_allowed = True
s.tick()
check("readiness release admits waiting probe", s.state == "probe")
check("boost is an edge", s.out.boost_v == s.t["BOOST_V"])
s.tick()
check("boost is not perpetually renewed", s.out.boost_v is None)
s.tick(95)
s.inp.batt = Val(-300, s.now - 20000)
s.now += 1
out = s.eng.tick(s.now, s.inp)
check("stale REC power returns shore", out.transfer_intent == "shore" and "Batt" in out.reason)
s = Sim()
s.tick(700, target=40, quattro_w=150, pv=0, m=0, voc=10)
check("measured Quattro charging blocks departure despite reconstructed quiet", s.state == "shore")
s.tick(35, quattro_w=0)
check("measured Quattro quiet allows night drain", s.state == "solar")
s.inp.quattro_w = None
s.now += 1
out = s.eng.tick(s.now, s.inp)
check("missing measured Quattro returns shore", out.transfer_intent == "shore" and "QuattroDC" in out.reason)
mean, duration = SP.time_mean([(0, 100), (9000, -100), (9500, -100)], 10000, 10000, 20000)
check("irregular sampling uses elapsed time", mean == 80 and duration == 10000)
mean, duration = SP.time_mean([(0, 100), (100000, -100)], 101000, 180000, 20000)
check("stale gaps are not integrated", duration == 21000)
try:
    s.eng.tick(s.now - 1, s.inp)
    check("clock reversal rejected", False)
except ValueError:
    check("clock reversal rejected", True)

# Losing optional MPPT telemetry never invents a battery deficit.
s = Sim()
s.tick(500, target=80, batt=900, quattro_w=0)
check("missing MPPT fixture establishes solar", s.state == "solar")
for _ in range(35):
    s.tick(1, batt=900)
    for name in ('y6', 'y7', 'm6', 'm7', 'voc6', 'voc7'):
        setattr(s.inp, name, None)
    s.now += 1
    s.out = s.eng.tick(s.now, s.inp)
check("missing MPPT with healthy bank stays solar", s.out.state == "solar" and s.out.transfer_intent == "island")

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
if __name__ == '__main__':
    sys.exit(1 if fail else 0)
elif fail:
    raise AssertionError('Solar engine checks failed: ' + ', '.join(fail))
