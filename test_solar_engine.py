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
check("config: one-way tunables", scfg.engine["ONEWAY_ENTER_PCT"] == 1 and
      scfg.engine["ONEWAY_EXIT_PCT"] == 0.5 and scfg.engine["ONEWAY_FULL_PCT"] == 100 and
      scfg.engine["ONEWAY_MIN_SOC"] == 25 and scfg.engine["ONEWAY_DEFICIT_W"] == 50 and
      scfg.engine["ONEWAY_DEFICIT_MS"] == 180000)
check("engine version bumped", SP.ENGINE_VERSION == "4.20")
Val = SP.Val


class Sim:
    """Drives Engine.tick with a plant that follows the commands: the
    Quattro's ActiveInput reports 240 (none) once IgnoreAcIn is 1, and
    also whenever there is no shore power at all (shore=False).

    Knobs beyond the engine's own inputs:
      shore          shore power is physically present (default True)
      ac_available   /Ac/State/AcIn<n>Available; "auto" follows `shore`,
                     None is a firmware that publishes no such path
      feed           force ActiveInput (0 = AC in 1, 1 = AC in 2, 240 = none)
      relay_lag      ticks the transfer switch takes to follow IgnoreAcIn
    """

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
                      batt_v=56.6, cvl=56.62, target=None, ac_available="auto")
        self.cmd, self.sustain = 0, 0
        self.relay_lag = 0
        self.cmd_hist = []
        self.cmds, self.sustains, self.boosts = [], [], []
        self.transitions, self.states = [], set()
        self.failures = []        # ticks on which out.probe_failed was raised
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
            # The transfer switch follows IgnoreAcIn only after relay_lag ticks.
            self.cmd_hist.append(self.cmd)
            applied = self.cmd_hist[max(0, len(self.cmd_hist) - 1 - self.relay_lag)]
            shore = v.get("shore", True)
            feed = v.get("feed")
            inp.feed = Val(240 if (applied == 1 or not shore) else 0, n) \
                if feed is None else Val(feed, n)
            avail = v.get("ac_available", "auto")
            if avail == "auto":
                avail = 1.0 if shore else 0.0
            inp.ac_available = None if avail is None else Val(float(avail), n)
            inp.ac_out = Val(v["load"], n)
            inp.voc6, inp.y6, inp.m6 = Val(v["voc"], n), Val(v["pv"], n), Val(v["m"], n)
            inp.voc7, inp.y7, inp.m7 = Val(0.0, n), Val(0.0, n), Val(0, n)
            inp.batt_v, inp.cvl = Val(v["batt_v"], n), Val(v["cvl"], n)
            inp.dc_load = Val(v.get("dc_load", 0.0), n)
            # dbus-recbms' DemandModel defaults: AC / 0.9 + 30 W idle + DC, 30 W uncertainty
            island = lambda ac: ac / 0.9 + 30.0 + v.get("dc_load", 0.0)
            inp.demand_avg = Val(v.get("demand_avg", island(v.get("load_avg", v["load"]))), n)
            inp.demand_slow = Val(v.get("demand_slow", island(v.get("load_slow", v["load"]))), n)
            inp.demand_margin = Val(v.get("demand_margin", 30.0), n)
            inp.quattro_w = Val(v.get("quattro_w", v["batt"] + max(0, v.get("dc_load", 0)) - v["pv"]), n)
            inp.target_soc = Val(v["target"], n) if v["target"] is not None else None
            inp.boost_active = Val(v.get("boost_active", 0), n)
            inp.boost_window = Val(v.get("boost_window", 0), n)
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
            if out.probe_failed:
                self.failures.append(n)
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
# night: PV gone, the loads draw from the bank -> 75 Wh of drawdown -> shore + sustain
s.tick(200, batt=-40.0, pv=0.0, m=0, voc=10.0)
check("charge: -40 W for 200 s is 2 Wh of drawdown, nowhere near the budget", s.state == "solar")
s.tick(240, batt=-1200.0)
check("charge: 75 Wh of drawdown -> shore", s.state == "shore" and s.cmd == 0, s.state)
# Stage A (A1, E03): the floor now goes out on the SAME tick as the shore
# command, so it is in force before the Quattro re-accepts, not after it.
check("charge: floor requested on the same tick as the shore command",
      s.sustain == 1 and s.sustains[-1][0] == s.cmds[-1][0],
      "sustain %s cmd %s" % (s.sustains[-1], s.cmds[-1]))
check("charge: still engaged at 68 %", s.oneway == "charge")
s.tick(1, soc=79.5, batt=0.0)
check("charge: done within EXIT of the target", s.oneway is None and
      any("ONE-WAY charge done (SOC 79.5% at target 80%)" in l for l in s.logs))
# Stage B (plan B2, master D03/SP23): arrival is a handover, not a release.
check("charge: arrival hands straight into the hold, on the arrival tick",
      s.out.sustain == 3 and s.out.charge_intent == "hold" and s.out.oneway == "" and
      s.sustains[-1] == (s.now, 3), "%s %s" % (s.out.charge_intent, s.sustains[-1:]))
check("charge: the hold is at the same target the charge aimed at",
      s.inp.target_soc.v == 80 and s.v["target"] == 80)

# ---- hysteresis and re-targeting ----
s = Sim()
s.tick(1, soc=79.2, target=80)
check("79.2 -> 80 is inside ENTER: normal engine", s.oneway is None)
s.tick(1, soc=78.9)
check("78.9 -> 80 engages", s.oneway == "charge")
s.tick(1, soc=79.4)
check("79.4 -> 80 stays engaged (EXIT is 0.5)", s.oneway == "charge")
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
check("discharge: heater-class load -> suspend on shore", s.state == "suspend" and s.cmd == 0)
check("discharge: ceiling kept through suspend", s.sustain == 2)
s.tick(15, load=300.0, batt=-300.0)
check("discharge: resumes to solar without a boost", s.state == "solar" and s.cmd == 1 and
      s.boosts == [])
s.tick(1, soc=70.9)
check("discharge: 70.9 -> 70 still engaged", s.oneway == "discharge")
s.tick(1, soc=70.4)
check("discharge: done within EXIT of the target", s.oneway is None)
check("discharge: arrival hands straight into the hold, on the arrival tick",
      s.out.sustain == 3 and s.out.charge_intent == "hold" and s.out.oneway == "" and
      s.sustains[-1] == (s.now, 3), "%s %s" % (s.out.charge_intent, s.sustains[-1:]))
check("discharge: the hold is at the same target the descent aimed at",
      s.inp.target_soc.v == 70 and s.v["target"] == 70)
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
prime(s, 200, 150)                               # est 350: clears 250/0.9+30+30=338, not 330/0.9+30+30=427
s.tick(340)
check("need judged against the slower average too", s.state == "shore" and "need 427W" in s.out.status_text,
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
check("4.17: -100 W for 5 min is 8 Wh, the island stays", s.state == "solar", str(s.transitions[-1:]))
s.tick(300, batt=-1200.0)
check("4.17: 75 Wh of drawdown -> shore", any(tr.startswith("-> SHORE (deficit: ") for tr in s.transitions),
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
s.tick(300, batt=-1200.0, cvl=59.49)
check("4.9: a draining stint ends by the deficit exit", s.state == "shore" and
      any("deficit" in tr for tr in s.transitions), str(s.transitions[-1:]))
s = Sim()
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
s.tick(5, load=3000.0, batt=-3000.0)
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

# ---- 4.16: a full target is one-way charge until arrival, then the endgame ----
s = Sim()
s.tick(1, soc=60, target=100)
check("full: 60 -> 100 engages one-way charge (not a shore bulk)", s.oneway == "charge")
check("full: the floor is asked for on shore", s.sustain == 1)
s.tick(335, batt_v=56.4, pv=0.0, m=0, voc=10.0)                     # night: nothing to probe
check("full: shore only sustains while the sun cannot carry the load", s.state == "shore" and s.sustain == 1)
s.tick(1, soc=99.6, batt_v=61.9, cvl=61.96)
check("full: arrival within EXIT of 100 is the endgame: no hold", s.oneway is None and s.sustain == 0
      and s.out.charge_intent == "release" and any("COMPLETE_FULL" in l for l in s.logs), str(s.logs[-1:]))
s.tick(1, soc=99.2)
check("full: inside the band the endgame stands", s.oneway is None and s.sustain == 0)
s.tick(1, soc=98.9)
check("full: more than a point under, one-way charge again with its floor", s.oneway == "charge" and s.sustain == 1)
s = Sim(ONEWAY_FULL_PCT=0)
s.tick(1, soc=60, target=100)
check("full: oneway_full_pct = 0 makes every target an endgame target in the engine alone (the consumer refuses it, #7)",
      s.oneway == "charge" and s.sustain == 1)

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
s.tick(5, load=3000.0, load_avg=300.0, batt=-3000.0, shore=False)   # avg lags: base 300 W
check("09-06: heater load -> suspend", s.state == "suspend" and s.cmd == 0)
check("09-06: no AC came: NO floor in suspend", s.sustain == 0, str(s.sustains[-3:]))
s.tick(200)
check("09-06: still none 200 s in", s.sustain == 0 and s.state == "suspend")
s.tick(1300, load=3000.0, batt=-3000.0)
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
s.tick(5, load=3000.0, batt=-3000.0)
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
check("4.16: a full target is one-way charge, so 35 % may leave shore on the one-way gate",
      s.oneway == "charge" and s.state != "shore", s.state)

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

# ---- 4.20: a boost that covers the need does not wait for its window ----
s = Sim()
s.tick(60, soc=60, target=80, pv=30.0, m=2, voc=75.0, load=1000.0, batt_v=56.4)   # a dim capture: on shore
s.tick(400, pv=5.0, m=1)                                                          # capped, as on the boat
s.tick(10, boost_active=1, pv=200.0, m=2)                                         # the boost lifts the cap; ramping
check("4.20: no capture while the boost is under the need", s.eng.st["cap6"]["w"] < 100, s.eng.st["cap6"])
s.tick(1, pv=1300.0)                                                              # covers the ~1140 W need
check("4.20: capture taken the moment the boost covers the need", abs(s.eng.st["cap6"]["w"] - 1300.0) < 1, s.eng.st["cap6"])
s.tick(40)
check("4.20: departure does not wait for the boost to end", s.state in ("probe", "solar"), s.state)

# ---- 4.19: a current cap makes the yield meaningless; boost on daylight ----
s = Sim()
s.tick(60, soc=60, target=80, pv=30.0, m=2, voc=75.0, load=1000.0, batt_v=56.4)   # a dim capture: no exploratory probe
s.tick(400, pv=5.0, m=1)                                              # then 6 W under a 75 V sky, no cap known
check("4.19: no boost on a trickle when the limit is unknown", s.boosts == [] and s.state == "shore", (str(s.boosts), s.state))
s.inp.ccl_a = SP.Val(0.1, s.now)
s.tick(400, pv=5.0)
check("4.19: boost on a trickle under a 0.1 A charge limit", s.boosts and s.boosts[-1] == s.t["BOOST_V"] and s.state == "shore", (str(s.boosts), s.state))
s = Sim()
s.inp.ccl_a = SP.Val(0.1, s.now)
s.tick(400, soc=60, target=80, pv=0.0, m=0, voc=20.0, batt_v=56.4)   # night under the same cap
check("4.19: no boost at night under a cap", s.boosts == [], str(s.boosts))

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

# ---- issue #1: a missing decision input keeps the objective and its hold ----
def drop(s, name):
    """One engine tick with `name` missing (the Sim refreshes every input)."""
    setattr(s.inp, name, None)
    s.now += 1
    s.out = s.eng.tick(s.now, s.inp)
    if s.out.cmd is not None:
        s.cmd = s.out.cmd
    return s.out

REQUIRED = ("soc", "batt", "load_now", "load_avg", "feed", "ac_out", "quattro_w")
for target, name, hold in ((80, "charge", "floor"), (40, "discharge", "ceiling")):
    s = Sim()
    s.tick(1, soc=60, target=target, batt_v=56.4)
    check("#1 %s: engaged with the %s" % (name, hold), s.oneway == name and s.out.charge_intent == hold)
    for field in REQUIRED:
        s.tick(1)
        out = drop(s, field)
        check("#1 %s: missing %s keeps the objective" % (name, field),
              s.oneway == name and out.oneway == name, "oneway %r" % s.oneway)
        check("#1 %s: missing %s keeps the %s" % (name, field, hold),
              out.charge_intent == hold, out.charge_intent)
        check("#1 %s: missing %s stays on shore, no boost" % (name, field),
              out.transfer_intent == "shore" and out.state == "shore" and out.boost_v is None
              and "No data" in out.status_text, out.status_text)
    s.tick(1)
    check("#1 %s: fresh data resumes without a sticky failure" % name,
          s.oneway == name and "No data" not in s.out.status_text, s.out.status_text)
    check("#1 %s: no 'done' logged for the outage" % name,
          not any("done (no data" in l for l in s.logs), str(s.logs))
# the same loss on solar: the floor is asked for on the tick of the loss,
# before the Quattro can report shore (E02 islanded: 59.34 V / 200 A at return)
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
check("#1 islanded: on solar with the floor released", s.state == "solar" and s.sustain == 0)
out = drop(s, "quattro_w")
check("#1 islanded: loss returns to shore under the floor at once",
      out.transfer_intent == "shore" and out.cmd == 0 and out.charge_intent == "floor"
      and out.sustain == 1 and s.oneway == "charge", "%s %s %s" % (out.transfer_intent, out.charge_intent, out.sustain))
check("#1 islanded: the transition names the loss", out.status_text == "-> SHORE (no data: QuattroDC)" and out.oneway == "charge",
      out.status_text)
s.tick(340)
check("#1 islanded: fresh data leaves for solar again", s.state == "solar" and s.oneway == "charge", s.state)
s.tick(1, soc=79.5, batt=0.0)
check("#1: arrival is still judged once the SOC is fresh", s.oneway is None and s.sustain == 3)
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
out = drop(s, "soc")
check("#1: no SOC keeps the last objective and a '?' in the status",
      s.oneway == "charge" and out.charge_intent == "floor" and out.status_text.startswith("1-WAY CHARGE ?->80% |"),
      out.status_text)
s.inp.enabled = False
out = drop(s, "soc")
check("#1: a disable still releases during an outage", s.oneway is None and out.charge_intent == "release")

# ---- 4.15 (Stage B, SP26): nothing burns a band down any more ----
# Issue #2 refused the ceiling-stall burn while charging one-way (E07: CHARGE
# on solar at 78 % for 80 %, -100 W, pack 59.47 V on a 59.49 V CVL, burned
# after 155 s). Stage B removes the state itself, so ordinary HOLD takes the
# same exits CHARGE does instead of filling a band and spending it.
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
check("#2 stall: CHARGE on solar", s.state == "solar" and s.oneway == "charge")
s.tick(300, soc=78.0, batt=-1200.0, batt_v=59.47, cvl=59.49, pv=200.0)
check("#2 stall: the deficit takes the ordinary return, never burn-down",
      s.state == "shore" and "burndown" not in s.states and
      any(tr.startswith("-> SHORE (deficit: ") for tr in s.transitions),
      "state %s %s" % (s.state, s.transitions[-1:]))
check("#2 stall: still CHARGE, floor back on shore", s.oneway == "charge" and s.sustain == 1)
# the shore-side entries with CHARGE selected: surplus and harvest
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(400, batt_v=57.0, cvl=56.62, batt=0.0, pv=0.0, m=0, voc=10.0)   # above the CVL, quiet, no sun
check("#2 surplus: no burn-down while charging one-way", "burndown" not in s.states and s.state == "shore",
      "%s %s" % (s.state, s.transitions))
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(400, batt_v=56.61, cvl=56.62, batt=0.0, pv=60.0, m=1, load=1000.0)   # band full, need > est
check("#2 harvest: no burn-down while charging one-way", "burndown" not in s.states and s.state == "shore",
      "%s %s" % (s.state, s.transitions))
# the boundary itself, and the suspend resume into a burn-down
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.eng.st.update(state="suspend", suspendPrev="burndown", suspendStart=s.now, suspendBase=300.0,
                lastTransition=s.now, desired=0)
s.tick(15, batt_v=59.47, cvl=59.49, load=300.0)
check("#2 resume: a suspended burn-down resumes to solar under CHARGE",
      s.state == "solar" and "burndown" not in s.states, "%s %s" % (s.state, s.transitions[-1:]))
s = Sim()
s.tick(1, soc=90, target=70, batt_v=60.3, cvl=60.3)
s.tick(335, pv=0.0, m=0, voc=10.0)
s.tick(300, batt=-600.0)
check("#2 discharge: still inverts under no sun", s.state == "solar" and s.oneway == "discharge" and s.cmd == 1)

# the same paths with the bank AT its target, which is where 4.9 harvested,
# burned the surplus and rolled a stalled deficit into a burn-down
s = Sim()
s.tick(1, soc=60, target=60, batt_v=57.0, cvl=56.62, batt=0.0, pv=0.0, m=0, voc=10.0)
check("HOLD: at the target, holding, no one-way objective", s.oneway is None and s.sustain == 3)
s.tick(400)
check("HOLD: bank above the CVL on shore stays on shore, no burn-down",
      s.state == "shore" and "burndown" not in s.states and s.sustain == 3,
      "%s %s" % (s.state, s.transitions))
check("HOLD: the status reports the bank above the CVL with no burn wording",
      s.out.status_text == "SHORE | batt 57.00V > CVL 56.62V", s.out.status_text)
s = Sim()
s.tick(1, soc=60, target=60, batt_v=56.4)
s.tick(400, batt_v=56.61, cvl=56.62, batt=0.0, pv=60.0, m=1, load=1000.0)   # band full, need > est
check("HOLD: a full band on shore no longer harvests",
      s.state == "shore" and "burndown" not in s.states and s.sustain == 3,
      "%s %s" % (s.state, s.transitions))
s = Sim()
s.tick(1, soc=60, batt_v=56.4)                     # no target yet: the normal engine leaves
s.tick(435)
check("HOLD: islanded to start with", s.state == "solar", s.state)
s.tick(1, target=60)
check("HOLD: the hold is asked for while islanded too",
      s.oneway is None and s.sustain == 3 and s.out.charge_intent == "hold")
s.tick(300, batt=-1200.0, batt_v=56.61, cvl=56.62, pv=200.0)   # the E07 stall, at the target
check("HOLD: deficit on solar returns to shore, no burn-down",
      s.state == "shore" and "burndown" not in s.states and
      any(tr.startswith("-> SHORE (deficit: ") for tr in s.transitions),
      "%s %s" % (s.state, s.transitions[-1:]))
check("HOLD: the hold stands on shore", s.sustain == 3)
s = Sim()
s.tick(1, soc=60, target=60, batt_v=56.4)
s.eng.st.update(state="suspend", suspendPrev="burndown", suspendStart=s.now, suspendBase=300.0,
                lastTransition=s.now, desired=0)
s.tick(15, batt_v=56.61, cvl=56.62, load=300.0)
check("HOLD: suspend resumes into solar", s.state == "solar" and "burndown" not in s.states and
      any(tr == "-> SOLAR (resumed after suspend)" for tr in s.transitions),
      "%s %s" % (s.state, s.transitions[-1:]))
check("HOLD: the hold rides through the suspend", s.sustain == 3)

# ---- issue #4: one slider step selects a direction; restart near the destination ----
sel = lambda prev, tgt, soc: SP.select_objective(prev, tgt, soc, s.t["ONEWAY_ENTER_PCT"], s.t["ONEWAY_EXIT_PCT"])
for gap in (3, 5, 6, 1.7):
    check("#4: +%s selects CHARGE" % gap, sel(None, 60 + gap, 60) == "charge")
    check("#4: -%s selects DISCHARGE" % gap, sel(None, 60 - gap, 60) == "discharge")
check("#4: +0.8 is within tolerance: neither", sel(None, 60.8, 60) is None)
check("#4: exactly one point is the tolerance edge: neither", sel(None, 61, 60) is None)
check("#4: retarget across the SOC flips in one step", sel("charge", 50, 60) == "discharge")
check("#4: retarget within tolerance stands down", sel("charge", 60.3, 60) is None)
# noise near the target: hundredths of a percent never alternate the objective
prev, seen = "charge", set()
for i in range(200):
    prev = sel(prev, 80, 79.48 + 0.04 * ((i % 3) - 1))      # 79.44 .. 79.52
    seen.add(prev)
check("#4: noise around the exit band settles to neither and stays", prev is None and seen <= {"charge", None}, str(seen))
prev, seen = None, set()
for i in range(200):
    prev = sel(prev, 80, 78.98 + 0.04 * ((i % 3) - 1))      # 78.94 .. 79.02
    seen.add(prev)
check("#4: noise around the entry band keeps CHARGE once entered", prev == "charge" and None not in seen - {None} and "discharge" not in seen, str(seen))
prev = None
for i in range(200):
    prev = sel(prev, 80, 80 + 0.05 * ((i % 3) - 1))
check("#4: noise at the target never selects either way", prev is None)
# the engine and the consumer's request follow: a single slider step at night
s = Sim()
s.tick(1, soc=62, target=65, batt_v=56.6, cvl=56.62, pv=0.0, m=0, voc=10.0)
check("#4: 62 -> 65 (one slider step) is CHARGE with the floor",
      s.oneway == "charge" and s.out.charge_intent == "floor" and s.out.oneway == "charge")
s = Sim()
s.tick(1, soc=78.3, target=80, batt_v=56.6)
check("#4: restart 1.7 points short selects CHARGE", s.oneway == "charge" and s.out.charge_intent == "floor")
s = Sim()
s.tick(1, soc=81.7, target=80, batt_v=56.6)
check("#4: restart 1.7 points over selects DISCHARGE", s.oneway == "discharge" and s.out.charge_intent == "ceiling")
s = Sim()
s.tick(1, soc=64.6, target=65, batt_v=56.6)
check("#4: within half a point on restart: at target", s.oneway is None and s.out.charge_intent == "hold")

# ---- issue #5: the need is the complete DC-bus demand ----
# E09: 400 W of PV against 300 W AC + 300 W DC passed the old 360 W need
s = Sim()
s.tick(400, soc=60, pv=400.0, m=2, load=300.0, dc_load=300.0, batt_v=56.4)
check("#5 E09: 400 W PV does not clear 300 W AC + 300 W DC",
      s.state == "shore" and "need 693W" in s.out.status_text, s.out.status_text)
check("#5 E09: the need is published", s.out.need_w is not None and abs(s.out.need_w - 693.3) < 1, str(s.out.need_w))
s = Sim()
s.tick(400, soc=60, pv=400.0, m=2, load=300.0, dc_load=50.0, batt_v=56.4)
check("#5 E16: 300 W AC + the 50 W DC baseline needs 443 W, not 360",
      s.state == "shore" and "need 443W" in s.out.status_text, s.out.status_text)
s.tick(400, pv=450.0)
check("#5 E16: 450 W of PV clears it", s.state != "shore", s.state)
# the same need for the probe and the one-way charge direct entry
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(400, pv=400.0, m=2, load=300.0, dc_load=300.0)
check("#5: one-way charge direct entry uses the complete demand too", s.state == "shore", s.state)
s = Sim(ONEWAY_SKIP_PROBE=0)
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(400, pv=400.0, m=2, load=300.0, dc_load=300.0)
check("#5: the probe path too", s.state == "shore" and "probe" not in s.states, s.state)
# missing demand: no elective departure, but one-way discharge still leaves
s = Sim()
s.tick(1, soc=60, batt_v=56.4)
for _ in range(400):
    s.tick(1, pv=1400.0, m=2)
    s.inp.demand_avg = s.inp.demand_slow = s.inp.demand_margin = None
    s.now += 1
    s.out = s.eng.tick(s.now, s.inp)
check("#5: no demand, no departure even in full sun", s.out.state == "shore" and "[no demand]" in s.out.status_text
      and "need ?W" in s.out.status_text and s.out.need_w is None, s.out.status_text)
s = Sim()
s.tick(1, soc=60, target=40, batt_v=56.4)
for _ in range(400):
    s.tick(1, pv=0.0, m=0, voc=10.0)
    s.inp.demand_avg = s.inp.demand_slow = s.inp.demand_margin = None
    s.now += 1
    s.out = s.eng.tick(s.now, s.inp)
    if s.out.cmd is not None:
        s.cmd = s.out.cmd
check("#5: one-way discharge leaves without any demand figure", s.out.state == "solar", s.out.state)

# ---- Stage A1/A2: the floor is in force before the relay closes ----
# E03: at an ordinary closure the pack was 56.41 V against a 59.34 V Quattro
# CVL and 200 A, because the floor waited for ActiveInput to report shore --
# the last thing that happens at a return.
s = Sim()
s.relay_lag = 20                                   # the transfer switch takes 20 s
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)                                        # -> solar
s.tick(95, batt=100.0, cvl=59.49)
check("A1: islanded with the floor released", s.state == "solar" and s.sustain == 0)
for _ in range(400):
    s.tick(1, batt=-1200.0)
    if s.state == "shore":
        break
check("A1: the deficit returns it", s.state == "shore" and s.cmd == 0)
check("A1: shore available, relay not yet closed: the floor goes out on the same tick",
      s.sustain == 1 and s.sustains[-1][0] == s.cmds[-1][0] and s.inp.feed.v == 240,
      "sustain %s cmd %s feed %s" % (s.sustains[-1], s.cmds[-1], s.inp.feed.v))

# availability alone, before ActiveInput moves at all
s = Sim()
s.tick(1, soc=80, target=95, shore=False)
check("A1: shore known absent (available 0): no floor", s.oneway == "charge" and s.sustain == 0)
s.tick(200)
check("A1: still none while it is absent", s.sustain == 0 and s.inp.feed.v == 240)
s.tick(1, ac_available=1)
check("A1: availability 1 asks for the floor before ActiveInput changes",
      s.sustain == 1 and s.sustains[-1][0] == s.now and s.inp.feed.v == 240,
      "sustain %s feed %s" % (s.sustains[-1:], s.inp.feed.v))

# a firmware with no availability path at all: unknown is not absent
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4, ac_available=None)
check("A1 unknown: an accepted input still gets the floor", s.sustain == 1)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
check("A1 unknown: released while islanded", s.state == "solar" and s.sustain == 0)
for _ in range(400):                               # the deficit returns it; no AC comes back
    s.tick(1, batt=-1200.0, shore=False)
    if s.state == "shore":
        break
ret = s.eng.st["lastTransition"]
check("A1 unknown: the return keeps the floor though nothing is accepted",
      s.state == "shore" and s.sustain == 1 and ret == s.now and s.inp.feed.v == 240,
      "sustain %s feed %s" % (s.sustain, s.inp.feed.v))
s.tick(89)
check("A1 unknown: still held through the 90 s grace",
      s.now - ret == 89000 and s.sustain == 1, "%d ms, sustain %s" % (s.now - ret, s.sustain))
s.tick(2)
check("A1 unknown: released once shore has really stayed absent past the grace",
      s.sustain == 0 and s.sustains[-1][1] == 0, str(s.sustains[-2:]))
s.tick(1, ac_available=1)
check("A1 unknown: shore reappears, the floor is asked for at once", s.sustain == 1)

# ---- Stage A2: an accepted input is a charger, whichever input it is ----
# ActiveInput 0 = AC in 1, 1 = AC in 2, 240 = nothing accepted. "Not AC1" is
# not proof of inverter-only operation (master D14, SP56).
s = Sim()
s.tick(1, soc=80, target=95, shore=False, ac_available=0, feed=1)
check("A2: an accepted AC in 2 is a connected charger: floor, though shore reads absent",
      s.oneway == "charge" and s.sustain == 1)
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
check("A2: islanded before the alternate input appears", s.state == "solar")
s.tick(1, feed=1)
check("A2: AC in 2 accepted during solar is the same fault",
      s.state == "shore" and s.eng.st["lockoutUntil"] > s.now and
      "FAULT: AC re-accepted externally" in (s.out.transition or ""), str(s.out.transition))
check("A2: the log names the input",
      any("AC input 1 during solar mode" in l for l in s.logs), str(s.logs[-1:]))

# ---- Stage A3: the probe's clock is the measurement's clock ----
# SP62: the ramp used to start on the request, so a slow transfer spent the
# window on shore and the verdict judged a bank that never left.
s = Sim(ONEWAY_SKIP_PROBE=0)
s.relay_lag = 20
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)                                        # probe at 300 s, relay opens at 321 s
check("A3: probe entered", s.state == "probe")
ramp = s.eng.st["probeRamp"]
check("A3: the ramp clock starts on the confirmed departure, not on the request",
      ramp == s.eng.st["probeStart"] + 21000,
      "ramp %s start %s" % (ramp, s.eng.st["probeStart"]))
s.tick(69)                                         # 90 s after the request, 69 s after departure
check("A3: 90 s after the request it is still ramping", s.state == "probe" and
      "PROBE 1s" in s.out.status_text, s.out.status_text)
ev = s.eng.st["evalBatt"]
check("A3: the evaluation window is the last 15 s of the ramp after departure",
      ev and ev[0][0] == ramp + 75000 and ev[-1][0] == ramp + 89000, str(ev[:1] + ev[-1:]))
s.tick(1)
check("A3: the verdict falls 90 s after departure, 111 s after the request",
      s.state == "solar" and s.now - ramp == 90000, "%s %d" % (s.state, s.now - ramp))

# a transfer that never happens is not a failed solar measurement
s = Sim(ONEWAY_SKIP_PROBE=0)
s.relay_lag = 100000                               # the transfer switch never moves
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
check("A3: probe entered with the relay stuck", s.state == "probe" and s.eng.st["probeRamp"] == 0)
check("A3: status says what it is waiting for",
      s.out.status_text.endswith("PROBE | waiting for transfer"), s.out.status_text)
check("A3: nothing is captured while it waits", s.eng.st["evalBatt"] == [])
s.tick(49)                                         # 90 s after probeStart
check("A3: an unconfirmed transfer returns to shore after the grace",
      s.state == "shore" and "transfer not confirmed in 90 s" in s.transitions[-1],
      str(s.transitions[-1:]))
check("A3: ... with no lockout and no failed probe",
      s.eng.st["lockoutUntil"] == 0 and s.failures == [], str(s.failures))
check("A3: ... but the engine-local cooldown doubles on the return tick",
      s.eng.st["backoffMs"] == 2 * s.t["COOLDOWN_MS"] and s.eng.st["backoffUntil"] > s.now,
      str(s.eng.st["backoffMs"]))
check("A3: ... and the floor is back on shore", s.sustain == 1)

# nor is a bank that is already at its ceiling
s = Sim(ONEWAY_SKIP_PROBE=0)
s.tick(1, soc=60, target=80, batt_v=56.58, cvl=56.62)   # 0.04 V: nothing to ramp into
s.tick(340)
check("A3: the transfer is confirmed but there is no room for the sun",
      s.state == "probe" and s.eng.st["probeRamp"] == 0)
check("A3: status names the two voltages",
      s.out.status_text.endswith("PROBE | waiting for headroom (batt 56.58 V, CVL 56.62 V)"),
      s.out.status_text)
s.tick(50)
check("A3: no headroom returns to shore, not a failed probe",
      s.state == "shore" and "no voltage headroom" in s.transitions[-1] and
      s.eng.st["lockoutUntil"] == 0 and s.failures == [], str(s.transitions[-1:]))

# the ordinary probe is unchanged in outcome
s = Sim(ONEWAY_SKIP_PROBE=0)
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
check("A3: an immediate transfer starts the clock on the next tick",
      s.state == "probe" and s.eng.st["probeRamp"] == s.eng.st["probeStart"] + 1000)
s.tick(51)
check("A3: the ordinary probe still ends in solar, with no failure raised",
      s.state == "solar" and s.failures == [], "%s %s" % (s.state, s.failures))

# ---- issue #8: the failed-probe edge for REC's durable backoff ----
s = Sim(ONEWAY_SKIP_PROBE=0)
s.tick(1, soc=60, target=80, batt_v=56.4, m=1)
s.tick(340)
s.tick(100, batt=-120.0, cvl=59.49)
check("#8: an evaluated probe failure raises exactly one edge",
      s.state == "shore" and s.failures == [s.eng.st["lastTransition"]] and
      any("probe failed" in tr for tr in s.transitions), str(s.failures))
check("#8: and never on a later tick", s.out.probe_failed is False)
s.tick(30)
check("#8: still one edge", len(s.failures) == 1)
s = Sim(ONEWAY_SKIP_PROBE=0)
s.tick(1, soc=60, target=80, batt_v=56.4, m=1)
s.tick(340)
check("#8: probe running before the sun goes", s.state == "probe")
s.tick(70, pv=0.0, m=0)
check("#8: MPPTs that never wake are a failed probe too",
      s.state == "shore" and any("never woke" in tr for tr in s.transitions) and
      s.failures == [s.eng.st["lastTransition"]], str(s.transitions[-1:]))
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(300, batt=-1200.0, cvl=59.49)
check("#8: an ordinary solar deficit return is not a failed probe",
      s.state == "shore" and any("deficit" in tr for tr in s.transitions) and s.failures == [],
      str(s.failures))

# ---- Stage B (plan B2, master D03/SP23): HOLD asks for the hold ----
# 4.9 released sustain at the destination and let the slider, the band and
# the loads settle it between them: a steady -49 W is 1.448 points in 24 h
# (E04). Sustain mode 3 is dbus-recbms' two-sided hold at the rest voltage.
s = Sim()
s.tick(1, soc=60, target=60, batt_v=56.6, cvl=56.62)
check("HOLD: sustain 3 on shore, from the first tick",
      s.oneway is None and s.out.oneway == "" and s.sustain == 3 and
      s.out.charge_intent == "hold" and s.sustains == [(s.now, 3)], str(s.sustains))
n0 = len(s.sustains)
s.tick(5)
check("HOLD: no re-assert inside the 30 s cycle", len(s.sustains) == n0)
s.tick(31)
check("HOLD: re-asserted every ASSERT_MS like the one-way holds",
      len(s.sustains) == n0 + 1 and s.sustain == 3)
s.tick(1, soc=60.4, target=61)
check("HOLD: a slider nudge inside the band keeps the hold",
      s.oneway is None and s.sustain == 3 and s.out.charge_intent == "hold")
s.inp.enabled = False
s.tick(1)
check("HOLD: disabled releases", s.sustain == 0 and s.out.charge_intent == "release")
s.inp.enabled = True
s.tick(1)
check("HOLD: re-enabled holds again", s.sustain == 3)
s.tick(1, target=None)
check("HOLD: no target releases", s.sustain == 0 and s.out.charge_intent == "release")
s = Sim()
s.tick(1, soc=99.6, target=100, batt_v=61.9, cvl=61.96)
check("HOLD: a full-charge target releases (COMPLETE_FULL must, D11/E12)",
      s.oneway is None and s.sustain == 0 and s.out.charge_intent == "release" and
      s.sustains == [], str(s.sustains))
s = Sim()
s.tick(340, soc=60, batt_v=56.4)
check("HOLD: no target at all, nothing written", s.sustains == [] and s.out.charge_intent == "release",
      str(s.sustains))

# ---- 4.17: the island's deficit rule is a drawdown, not a rate ----
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=160.0, cvl=59.49)
check("4.17: on solar with a surplus", s.state == "solar" and s.eng.st["drawdownWh"] == 0.0)
s.tick(130, batt=-1100.0, load=1700.0)                   # the water heater: 1.7 kW for 130 s, sun 600 W
check("4.17: a two-minute heater burst is 40 Wh, the island survives",
      s.state == "solar" and 38 <= s.eng.st["drawdownWh"] <= 41, "%s %.1f" % (s.state, s.eng.st["drawdownWh"]))
check("4.17: the status shows the drawdown", "[drawdown 40/75 Wh]" in s.out.status_text, s.out.status_text)
s.tick(900, batt=160.0, load=300.0)                      # 15 min of 160 W surplus repays it
check("4.17: surplus repays the drawdown down to zero", s.eng.st["drawdownWh"] == 0.0 and s.state == "solar")
s.tick(3600, batt=160.0)
s.tick(130, batt=-1100.0, load=1700.0)
check("4.17: an hour of surplus does not bankroll the next burst: it is 40 Wh again",
      38 <= s.eng.st["drawdownWh"] <= 41, "%.1f" % s.eng.st["drawdownWh"])
for _ in range(4):                                       # bursts separated by 5 s blips of surplus
    s.tick(5, batt=100.0, load=300.0)
    s.tick(60, batt=-1100.0, load=1700.0)
check("4.17: a cloud-edge blip does not wipe the slate: the chain returns",
      s.state == "shore" and any("deficit:" in tr for tr in s.transitions), str(s.transitions[-1:]))
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
s.tick(5600, batt=-49.0)                                 # the E04 drain: 75 Wh after ~92 min
check("4.17: a steady -49 W is bounded at 75 Wh and returns", s.state == "shore" and
      any("deficit: 75 Wh" in tr for tr in s.transitions), str(s.transitions[-1:]))
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
s.tick(10, load=2000.0, batt=-1400.0)
check("4.17: 2 kW is under the suspend threshold", s.state == "solar")
s.tick(200, load=2000.0, batt=-1400.0)                   # 2 kW for 3.5 min: the budget returns it
check("4.17: 2 kW for three minutes is the budget: return", s.state == "shore")
s = Sim()
s.tick(1, soc=60, target=80, batt_v=56.4)
s.tick(340)
s.tick(95, batt=100.0, cvl=59.49)
s.tick(5, load=2600.0, batt=-2000.0)
check("4.17: 2.6 kW suspends within seconds", s.state == "suspend")
s = Sim()
s.tick(1, soc=90, target=70, batt_v=60.3, cvl=60.3)
s.tick(335, pv=0.0, m=0, voc=10.0)
s.tick(3600, batt=-600.0)
check("4.17: DISCHARGE ignores the drawdown: the deficit is the plan", s.state == "solar" and s.oneway == "discharge")

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
if __name__ == '__main__':
    sys.exit(1 if fail else 0)
elif fail:
    raise AssertionError('Solar engine checks failed: ' + ', '.join(fail))
