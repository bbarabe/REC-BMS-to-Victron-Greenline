#!/usr/bin/env python3
"""
dbus-solarpriority — standalone Venus OS driver for Solar Priority.

Replaces the Node-RED "Solar Priority" flow (SolarPriority.json, Decision
Engine v4.2). Ships in the dbus-recbms package (same folder, same installer)
but is a separate daemontools service with its own config, log and D-Bus
service; it talks to dbus-recbms only over D-Bus — the same contract the
flow used — so the BMS driver needs no changes.

What it does (see README.md "Solar Priority driver"):
  - powers AC loads from solar instead of shore by driving the Quattro's
    /Ac/Control/IgnoreAcIn1: shore -> probe (90 s MPPT ramp) -> solar ->
    shore; shore -> burndown (surplus or harvested lead band) -> solar |
    probe | shore; solar/burndown -> suspend (heater on shore) -> resumed
  - capacity is MEASURED (MppOperationMode 2 => /Yield/Power is capacity),
    refreshed without dropping shore via the dbus-recbms solar boost, and
    estimated while throttled by the adaptive single-diode panel model
  - battery power is the health signal; the deficit exit judges the 90 s
    rolling mean (MPPT throttle-hunting at the ceiling flips the sign)
  - a VRM toggle ("Solar Priority") and slider ("PV Capacity") on
    com.victronenergy.switch.solarpriority, persisted in localsettings
  - one-way charge / discharge (engine 4.3): when the Max Charge target is
    more than ONEWAY_ENTER_PCT away from the SOC, the bank is only ever
    moved in that direction. Charging: solar does all the charging (the
    normal shore -> probe -> solar cycle), and whenever shore is connected
    dbus-recbms is asked to SUSTAIN the bank (read the slider as the present
    SOC, floor mode) so the Quattro holds instead of charging. Discharging:
    the bank is sustained as a ceiling the whole time (nothing may charge
    it), shore is left as soon as the gates allow, and the loads drain the
    bank day and night while solar covers what it can. Ends within
    ONEWAY_EXIT_PCT of the target; the normal engine finishes the last bit.
    Two limits (4.5): a target at or above ONEWAY_FULL_PCT (100 %) means a
    full charge from every charger at its maximum, so one-way charge stays
    off; and the floor is only asked for while the Quattro is actually on
    shore (ActiveInput = the shore input) -- with no AC available the
    Quattro inverts regardless, and a floor would only throttle solar.

Differences from the flow (all deliberate):
  - inputs come from a velib DbusMonitor (signal-driven cache). Values stay
    last-known-good exactly like the flow; service liveness is still judged
    on the jittering heartbeat paths AND on the service being present.
  - IgnoreAcIn1 is forced back to 0 on SIGTERM / exit — a dead flow could
    leave the Quattro inverting indefinitely; a dead driver cannot.
  - every transition is logged to /var/log/dbus-solarpriority (durable),
    not just a debug sidebar.
  - a FAULT / emergency-SOC lockout is a hard 1 h hold (lockoutUntil): the
    flow's evidence-based early release also cleared lockouts, so in strong
    sun a locked-out engine re-probed every cooldown.
  - state machine lives in the Engine class (pure, testable): tick(now, inp)
    -> outputs. Thresholds are in solar_priority.ini [engine]; the defaults
    are the flow's values.

Baselines: dbus_recbms.py (service structure), velib_python dbusmonitor.
"""

import atexit
import configparser
import glob
import logging
import math
import os
import platform
import signal
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "1.4.0"
ENGINE_VERSION = "4.5"
BUSITEM = "com.victronenergy.BusItem"

log = logging.getLogger("dbus-solarpriority")


# ----------------------------------------------------------------------------
# velib_python (same lookup as dbus_recbms.py)
# ----------------------------------------------------------------------------
def _find_velib():
    candidates = ["/data/velib_python",
                  "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"]
    candidates += sorted(glob.glob("/opt/victronenergy/*/ext/velib_python"))
    candidates.append(os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "vedbus.py")):
            sys.path.insert(1, c)
            return c
    raise RuntimeError("velib_python not found (looked in %s)" % candidates)


_VELIB_DIR = _find_velib()
from vedbus import VeDbusService            # noqa: E402
from settingsdevice import SettingsDevice   # noqa: E402
from dbusmonitor import DbusMonitor         # noqa: E402


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
# Engine tunables: name -> (default, unit). Defaults are the flow's values
# (Decision Engine v4.2); the ini overrides by the same names, lower-case.
ENGINE_DEFAULTS = {
    "SOLAR_MARGIN": 1.2, "MIN_EST_W": 100, "READY_MS": 30000, "RAMP_MS": 90000,
    "EVAL_MS": 15000, "DISCHARGE_TOL_W": 50, "LOAD_EXCEED_MS": 15000,
    "SOLAR_SETTLE_MS": 90000, "DEFICIT_AVG_MS": 90000, "SURGE_W": 400,
    "SURGE_MS": 3000, "COOLDOWN_MS": 300000, "BACKOFF_MAX_MS": 3600000,
    "STABLE_MS": 1800000, "MIN_SOC": 40, "SOC_EMERGENCY": 30, "SOC_DRIFT_MAX": 2,
    "HB_STALE_MS": 20000, "ASSERT_MS": 30000, "FEEDBACK_GRACE_MS": 90000,
    "CAP_SMOOTH": 0.3, "CAP_FRESH_MS": 900000, "CAP_ZERO_MS": 5400000,
    "VOC_DAY_V": 55, "VOC_EXPLORE_V": 65, "CVL_MARGIN_V": 0.05, "WAKE_MS": 60000,
    "BURN_EXIT_DROP_V": 0.15, "BURN_EXIT_MS": 30000, "BURN_CALM_MS": 15000,
    "SURPLUS_QUIET_W": 100, "BOOST_V": 0.30, "BOOST_INTERVAL_MS": 900000,
    "BOOST_RETRY_MS": 180000, "BURN_REARM_V": 0.25, "HARVEST_ARM_V": 0.02,
    "REFILL_RESET_V": 0.10, "HARVEST_MIN_EVID": 100, "STALL_BURN_MIN_V": 0.05,
    "SUSPEND_LOAD_W": 1000, "SUSPEND_MS": 3000, "SUSPEND_MAX_MS": 1200000,
    "RESUME_DELTA_W": 200, "RESUME_MS": 10000,
    "MDL_A_V": 3.5, "MDL_VOC_IDLE_W": 3, "MDL_VOC_TAU_MS": 600000, "MDL_MIN_W": 10,
    "MDL_CAL_MIN_W": 30, "MDL_KFF_DEF": 0.78, "MDL_KFF_ALPHA": 0.05,
    "MDL_RATIO_MIN": 0.02, "MDL_RATIO_CONF": 0.3, "MDL_MAX_MULT": 20,
    "MDL_FRESH_MS": 300000, "MDL_SHARE6": 0.35, "MDL_SHARE7": 0.65, "VOC_RISE_V": 1.0,
    "BURN_CALM_GATE_MS": 45000, "LOAD_AVG_MS": 60000,
    # one-way charge / discharge (4.3): engage when the Max Charge target is
    # further than ENTER from the SOC, stand down within EXIT of it. 0 = off.
    "ONEWAY_ENTER_PCT": 5, "ONEWAY_EXIT_PCT": 1,
    # pre-probe checks (4.4), from the evening of 2026-09-01 in Home
    # Assistant: five probes on model estimates of 374-1381 W while every
    # unthrottled reading after 17:42 was 86-296 W; the arrays' balance
    # against their rated shares was 0.06-0.35 whenever the flybridge was
    # obstructed and 0.68-0.75 when it was not; three probes ended on the
    # load rising past the 60 s average.
    "CAP_TRUMPS_MDL_MS": 900000, "SHADE_BALANCE_MIN": 0.4, "LOAD_SLOW_MS": 300000,
    # one-way charge: how much deficit solar may run before shore is
    # reconnected. Measured 2026-09-02: the 4.2 rule (-50 W over 90 s) left
    # solar over a one-minute -51 W dip, and every reconnect made the
    # Quattro re-absorb at 0.6-2 kW for ten-plus minutes (>100 Wh of shore
    # into the bank) -- far more than the dip. On a bank this size a small
    # deficit is nothing; a reconnect is not.
    "ONEWAY_DEFICIT_W": 200, "ONEWAY_DEFICIT_MS": 600000,
    # 4.5: a target at or above this is a request for a FULL charge from
    # every charger at its maximum, so one-way charge never engages there
    # (2026-09-06: at 100 % the floor held the Quattro at the present SOC
    # and only solar could move the bank -- it could never get full). 0 = off.
    "ONEWAY_FULL_PCT": 100,
}


class Config:
    def __init__(self, path):
        # inline ";" comments are used in solar_priority.ini
        cp = configparser.ConfigParser(interpolation=None,
                                       inline_comment_prefixes=(";", "#"))
        cp.read(path)
        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        s = cp["service"] if cp.has_section("service") else {}
        self.suffix = s.get("service_suffix", "solarpriority")
        self.settings_id = s.get("settings_id", "solarpriority")
        self.instance = int(s.get("instance", 221))
        self.group = s.get("group", "BMS")
        self.toggle_name = s.get("toggle_name", "Solar Priority")
        self.slider_name = s.get("slider_name", "PV Capacity")
        self.rated_min = float(s.get("rated_min_w", 100))
        self.rated_max = float(s.get("rated_max_w", 2500))
        self.rated_step = float(s.get("rated_step_w", 50))
        self.rated_default = float(s.get("rated_default_w", 1800))

        i = cp["inputs"] if cp.has_section("inputs") else {}
        self.mppt6_instance = int(i.get("mppt_a_instance", 278))
        self.mppt7_instance = int(i.get("mppt_b_instance", 279))
        self.vebus_instance = int(i.get("vebus_instance", 276))
        self.battery_instance = int(i.get("battery_instance", 200))
        self.ac_in = int(i.get("shore_ac_input", 1))     # 1 or 2
        self.tick_ms = int(i.get("tick_ms", 1000))
        # EstimateW / NeedW are published in steps of this many watts, and a
        # tick's changes go out as one ItemsChanged (2026-09-05: at 0.1 W
        # these two were a bus signal a second each for nothing).
        self.power_step = float(i.get("power_step", 5))

        e = cp["engine"] if cp.has_section("engine") else {}
        self.engine = {}
        for k, dflt in ENGINE_DEFAULTS.items():
            raw = e.get(k.lower(), None)
            self.engine[k] = float(raw) if raw is not None else dflt


# ----------------------------------------------------------------------------
# Engine — 1:1 port of the flow's Decision Engine v4.2 (times in ms)
# ----------------------------------------------------------------------------
class Val:
    """A last-known-good input: value + the time it last CHANGED."""
    __slots__ = ("v", "ts")

    def __init__(self, v, ts):
        self.v = v
        self.ts = ts


class Inputs:
    """What the engine sees each tick. All Val or None (missing)."""
    FIELDS = ("soc", "batt", "load_now", "load_avg", "load_slow", "feed", "ac_out",
              "voc6", "voc7", "y6", "y7", "m6", "m7", "batt_v", "cvl",
              "boost_active", "boost_window", "boost_eff", "lead",
              "target_soc", "sustain_active", "dc_load")

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, None)
        self.enabled = False
        self.lead_fault = ""
        self.p_rated = 1800.0
        self.feed_shore = 0      # ActiveInput value meaning "shore present"


class Outputs:
    def __init__(self):
        self.cmd = None          # 0/1 to write to IgnoreAcIn, or None
        self.transition = None   # text, or None
        self.boost = None        # volts to request (0 = release), or None
        self.sustain = None      # 0/1/2 to write to /RecBms/Sustain/Request, or None
        self.oneway = ""         # "", "charge" or "discharge"
        self.status_fill = "grey"
        self.status_text = ""
        self.est = 0.0
        self.need_w = 0.0
        self.state = "shore"


def fresh_state(now, t):
    return {
        "state": "shore", "desired": 0, "lastSent": None, "lastAssert": 0,
        "lastTransition": now, "probeStart": 0, "probeEst": 0,
        "evalPv": [], "evalBatt": [],
        "readySince": 0, "loadExceedStart": 0, "surgeStart": 0,
        "backoffMs": t["COOLDOWN_MS"], "backoffUntil": 0,
        "socEntry": 0, "solarSince": 0, "cap6": None, "cap7": None,
        "burnReadySince": 0, "burnExitStart": 0, "burnCalmStart": 0,
        "burnDoneCvl": None,
        "refillReady": None, "refillShoreFed": False, "harvestSince": 0,
        "suspendTrigStart": 0, "suspendStart": 0, "suspendBase": 0,
        "resumeStart": 0, "suspendPrev": None,
        "battWin": [], "battWinLong": [], "mdl6": None, "mdl7": None, "vocRef": None,
        "lastBoostTs": 0, "lockoutUntil": 0,
        "oneway": None, "sustainSent": 0, "sustainAssert": 0,
    }


SUSTAIN_OFF, SUSTAIN_FLOOR, SUSTAIN_CEILING = 0, 1, 2   # dbus-recbms modes


class Engine:
    def __init__(self, tunables, now_ms, logger=None):
        self.t = dict(tunables)
        self.st = fresh_state(now_ms, self.t)
        self.log = logger or (lambda msg: None)

    # ---- helpers -----------------------------------------------------
    def reset_backoff(self):
        """Fresh enable clears any failed-probe lockout (flow: Store Enable)."""
        self.st["backoffMs"] = self.t["COOLDOWN_MS"]
        self.st["backoffUntil"] = 0
        self.st["lockoutUntil"] = 0

    def force_shore(self, now):
        """Equivalent of the flow's catch node: force shore after an error."""
        st = self.st
        st["state"] = "shore"
        st["desired"] = 0
        st["lastSent"] = 0
        st["lastAssert"] = now

    # ---- the tick -----------------------------------------------------
    def tick(self, now, inp):
        t = self.t
        st = self.st
        out = Outputs()

        enabled = inp.enabled
        soc, batt = inp.soc, inp.batt
        loadNow, loadAvg = inp.load_now, inp.load_avg
        feed, acOut = inp.feed, inp.ac_out
        voc6, voc7, y6, y7, m6, m7 = inp.voc6, inp.voc7, inp.y6, inp.y7, inp.m6, inp.m7
        battV, cvl = inp.batt_v, inp.cvl
        boostAct, boostWin, boostEff = inp.boost_active, inp.boost_window, inp.boost_eff
        boosting = boostAct is not None and boostAct.v == 1
        leadV = inp.lead
        leadOn = leadV is None or leadV.v > 0.005
        leadFault = inp.lead_fault or ""
        windowOpen = boostWin is not None and boostWin.v == 1
        FEED_SHORE = inp.feed_shore

        pRated = inp.p_rated
        if not (isinstance(pRated, (int, float)) and math.isfinite(pRated)
                and 100 <= pRated <= 2500):
            pRated = 1800.0

        HB = t["HB_STALE_MS"]
        sysAlive = loadNow is not None and (now - loadNow.ts) < HB
        vebusAlive = acOut is not None and (now - acOut.ts) < HB

        # ---- Rolling battery-power mean (v3.6) ----
        if batt is not None:
            bw = st["battWin"]
            bw.append((now, batt.v))
            while bw and bw[0][0] < now - t["DEFICIT_AVG_MS"]:
                bw.pop(0)
            if len(bw) > 150:
                del bw[:len(bw) - 150]
        battMean = None
        if len(st["battWin"]) >= 5:
            battMean = sum(w for _, w in st["battWin"]) / len(st["battWin"])
        # 4.4: the long mean for one-way charge (cleared on entering solar,
        # so it never carries the shore stint's charging)
        if batt is not None:
            bl = st["battWinLong"]
            bl.append((now, batt.v))
            while bl and bl[0][0] < now - t["ONEWAY_DEFICIT_MS"]:
                bl.pop(0)
            if len(bl) > 900:
                del bl[:len(bl) - 900]
        battMeanLong = None
        if len(st["battWinLong"]) >= 60:
            battMeanLong = sum(w for _, w in st["battWinLong"]) / len(st["battWinLong"])

        # ---- Capacity capture ----
        def capture(cap, mode, yld):
            if mode is None or mode.v != 2:
                return cap
            if yld is None or (now - yld.ts) >= HB:
                return cap
            if cap is not None and (now - cap["ts"]) < 60000:
                w = cap["w"] * (1 - t["CAP_SMOOTH"]) + yld.v * t["CAP_SMOOTH"]
            else:
                w = yld.v
            return {"w": w, "ts": now}

        if not boosting or windowOpen:
            st["cap6"] = capture(st["cap6"], m6, y6)
            st["cap7"] = capture(st["cap7"], m7, y7)

        def faded(cap):
            if cap is None:
                return 0.0
            age = now - cap["ts"]
            if age <= t["CAP_FRESH_MS"]:
                return cap["w"]
            if age >= t["CAP_ZERO_MS"]:
                return 0.0
            return cap["w"] * (1 - (age - t["CAP_FRESH_MS"]) / (t["CAP_ZERO_MS"] - t["CAP_FRESH_MS"]))

        # ---- Adaptive panel model (v3.4) ----
        for k in ("mdl6", "mdl7"):
            if st[k] is None:
                st[k] = {"voc": None, "vocTs": 0, "kff": t["MDL_KFF_DEF"], "est": None}

        def modelTick(mdl, pv, yld, mode):
            v = pv.v if pv is not None else None
            if v is not None and v > t["VOC_DAY_V"] and yld is not None and yld.v < t["MDL_VOC_IDLE_W"]:
                if mdl["voc"] is None:
                    mdl["voc"] = v
                else:
                    dt = min(now - mdl["vocTs"], 60000)
                    mdl["voc"] += (v - mdl["voc"]) * min(1.0, dt / t["MDL_VOC_TAU_MS"])
                mdl["vocTs"] = now
            live = yld is not None and (now - yld.ts) < HB
            if (mdl["voc"] is None or v is None or not live or yld.v < t["MDL_MIN_W"]
                    or v < t["VOC_DAY_V"] or v > mdl["voc"] + 1):
                return
            ratioRaw = 1 - math.exp((v - mdl["voc"]) / t["MDL_A_V"])
            isc = (yld.v / v) / max(ratioRaw, t["MDL_RATIO_MIN"])
            if mode is not None and mode.v == 2:
                if yld.v >= t["MDL_CAL_MIN_W"] and ratioRaw > 0.05:
                    kObs = yld.v / (mdl["voc"] * isc)
                    if math.isfinite(kObs) and 0.3 < kObs < 1.2:
                        kObs = min(max(kObs, 0.4), 0.95)
                        mdl["kff"] += (kObs - mdl["kff"]) * t["MDL_KFF_ALPHA"]
                mdl["est"] = None
                return
            cRaw = mdl["kff"] * mdl["voc"] * isc
            lb = ratioRaw < t["MDL_RATIO_CONF"] or cRaw > yld.v * t["MDL_MAX_MULT"]
            c = max(min(cRaw, yld.v * t["MDL_MAX_MULT"]), yld.v)
            mdl["est"] = {"w": min(c, pRated), "ts": now, "lb": lb}

        modelTick(st["mdl6"], voc6, y6, m6)
        modelTick(st["mdl7"], voc7, y7, m7)

        def mdlVal(mdl):
            e = mdl["est"]
            return e if (e is not None and (now - e["ts"]) <= t["MDL_FRESH_MS"]) else None

        me6, me7 = mdlVal(st["mdl6"]), mdlVal(st["mdl7"])
        modelSum = (me6["w"] if me6 else 0) + (me7["w"] if me7 else 0)
        plantConf = None
        if me6 and not me6["lb"]:
            plantConf = me6["w"] / t["MDL_SHARE6"]
        if me7 and not me7["lb"]:
            plantConf = max(plantConf or 0, me7["w"] / t["MDL_SHARE7"])

        pvNow = (y6.v if y6 else 0) + (y7.v if y7 else 0)
        capSum = faded(st["cap6"]) + faded(st["cap7"])
        vocMax = max(voc6.v if voc6 else 0, voc7.v if voc7 else 0)
        dayOk = (m6 is not None and m6.v > 0) or (m7 is not None and m7.v > 0)
        # 4.4: an unthrottled reading IS the capacity. While one is younger
        # than CAP_TRUMPS_MDL_MS the model may not outbid it -- the model's
        # job is to estimate while throttled, not to argue with a measurement.
        def evid(cap, me):
            c = faded(cap)
            if cap is not None and (now - cap["ts"]) <= t["CAP_TRUMPS_MDL_MS"]:
                return c
            return max(c, me["w"] if me else 0)
        evidence = evid(st["cap6"], me6) + evid(st["cap7"], me7)
        est = min(pRated, max(pvNow, evidence)) if (dayOk and vocMax >= t["VOC_DAY_V"]) else 0.0
        # 4.4: array balance. Both chargers get the same voltage, so
        # throttling and sun angle move them together (within their tilt);
        # an obstruction on one string does not. Judged on fresh unthrottled
        # captures against the rated shares; None when there is no fresh pair.
        balance = None
        c6, c7 = st["cap6"], st["cap7"]
        if (c6 is not None and c7 is not None and (now - c6["ts"]) <= t["CAP_FRESH_MS"]
                and (now - c7["ts"]) <= t["CAP_FRESH_MS"]):
            p6, p7 = c6["w"] / t["MDL_SHARE6"], c7["w"] / t["MDL_SHARE7"]
            balance = (min(p6, p7) / max(p6, p7)) if max(p6, p7) > 0 else None
        shaded = balance is not None and balance < t["SHADE_BALANCE_MIN"]
        if st["vocRef"] is None or now - st["vocRef"]["ts"] >= t["MDL_VOC_TAU_MS"]:
            st["vocRef"] = {"v": vocMax, "ts": now}
        vocRising = vocMax > st["vocRef"]["v"] + t["VOC_RISE_V"]

        effCvl = boostEff.v if boostEff is not None else (cvl.v if cvl is not None else None)
        aboveCvl = (battV is not None and effCvl is not None) and (battV.v > effCvl + t["CVL_MARGIN_V"])

        # Harvest refill tracker (v4.0)
        if battV is not None and cvl is not None:
            if battV.v <= cvl.v - t["REFILL_RESET_V"]:
                st["refillReady"] = True
                st["refillShoreFed"] = False
            elif (st["refillReady"] is True and battV.v < cvl.v - t["HARVEST_ARM_V"]
                  and batt is not None and batt.v > t["SURPLUS_QUIET_W"]
                  and pvNow < batt.v * 0.6):
                st["refillShoreFed"] = True

        transition = [None]
        boostMsg = [None]
        status = ["grey", ""]

        def toShore(reason):
            st["desired"] = 0
            st["state"] = "shore"
            st["lastTransition"] = now
            st["readySince"] = 0
            st["loadExceedStart"] = 0
            st["surgeStart"] = 0
            st["probeStart"] = 0
            st["evalPv"] = []
            st["evalBatt"] = []
            st["burnReadySince"] = 0
            st["burnExitStart"] = 0
            st["burnCalmStart"] = 0
            st["harvestSince"] = 0
            st["suspendTrigStart"] = 0
            st["resumeStart"] = 0
            boostMsg[0] = 0
            transition[0] = "-> SHORE (" + reason + ")"
            status[1] = transition[0]

        def escalateBackoff():
            st["backoffUntil"] = now + st["backoffMs"]
            st["backoffMs"] = min(st["backoffMs"] * 2, t["BACKOFF_MAX_MS"])

        def lockout():
            st["backoffMs"] = t["BACKOFF_MAX_MS"]
            st["backoffUntil"] = now + t["BACKOFF_MAX_MS"]
            # Driver difference: the flow's early-release rule (capacity
            # evidence clears backoffUntil) also cleared FAULT / emergency
            # lockouts, so in strong sun a locked-out engine re-probed every
            # cooldown. A lockout is a hard hold until it expires or the
            # user re-enables.
            st["lockoutUntil"] = now + t["BACKOFF_MAX_MS"]

        def enter_burndown(reason, clear_harvest):
            st["state"] = "burndown"
            st["desired"] = 1
            st["lastTransition"] = now
            st["readySince"] = 0
            st["burnReadySince"] = 0
            st["burnExitStart"] = 0
            st["burnCalmStart"] = 0
            st["surgeStart"] = 0
            st["refillReady"] = False
            if clear_harvest:
                st["harvestSince"] = 0
            transition[0] = "-> BURNDOWN (" + reason + ")"
            status[0] = "green"
            status[1] = transition[0]

        def enter_probe(reason):
            st["state"] = "probe"
            st["desired"] = 1
            st["probeStart"] = now
            st["probeEst"] = max(est, needW)
            st["evalPv"] = []
            st["evalBatt"] = []
            st["lastTransition"] = now
            st["surgeStart"] = 0
            st["readySince"] = 0
            st["burnExitStart"] = 0
            st["burnCalmStart"] = 0
            st["lastBoostTs"] = now
            boostMsg[0] = t["BOOST_V"]       # probe assist (v3.5)
            transition[0] = "-> PROBE (" + reason + ")"
            status[0] = "yellow"
            status[1] = transition[0]

        def enter_solar(reason):
            st["state"] = "solar"
            st["desired"] = 1
            st["battWinLong"] = []
            st["socEntry"] = soc.v
            st["solarSince"] = now
            st["lastTransition"] = now
            st["loadExceedStart"] = 0
            st["surgeStart"] = 0
            st["burnExitStart"] = 0
            st["burnCalmStart"] = 0
            st["backoffMs"] = t["COOLDOWN_MS"]
            st["backoffUntil"] = 0
            transition[0] = "-> SOLAR (" + reason + ")"
            status[0] = "green"
            status[1] = transition[0]

        def enter_suspend(prev):
            st["state"] = "suspend"
            st["suspendPrev"] = prev
            st["desired"] = 0
            st["suspendStart"] = now
            st["lastTransition"] = now
            st["suspendTrigStart"] = 0
            st["resumeStart"] = 0
            st["loadExceedStart"] = 0
            st["surgeStart"] = 0
            boostMsg[0] = 0
            transition[0] = "-> SUSPEND (load %.0fW, base %.0fW)" % (loadNow.v, st["suspendBase"])
            status[0] = "blue"
            status[1] = transition[0]

        missing = []
        if soc is None:
            missing.append("SOC")
        if batt is None:
            missing.append("Batt")
        if loadNow is None or loadAvg is None:
            missing.append("Load")
        if feed is None:
            missing.append("ActiveIn")
        if not sysAlive:
            missing.append("system-hb")
        if not vebusAlive:
            missing.append("vebus-hb")

        # ---- One-way charge / discharge (4.3) ----
        # The Max Charge slider is a destination. While it is far from the
        # SOC the bank is only allowed to move toward it: the shore charger
        # never charges (dbus-recbms sustains the bank instead), solar does
        # the charging; or nothing charges and the loads do the draining.
        tgt = inp.target_soc
        oneway = st["oneway"]
        # 4.5: at (or above) ONEWAY_FULL_PCT the slider asks for a full
        # charge from everything -- the Quattro at its full CVL too, not a
        # floor -- so the feature stands aside.
        full = (tgt is not None and t["ONEWAY_FULL_PCT"] > 0
                and tgt.v >= t["ONEWAY_FULL_PCT"])
        if not enabled or missing or tgt is None or full:
            oneway = None
        else:
            delta = tgt.v - soc.v
            if oneway == "charge" and delta <= t["ONEWAY_EXIT_PCT"]:
                oneway = None
            elif oneway == "discharge" and delta >= -t["ONEWAY_EXIT_PCT"]:
                oneway = None
            if t["ONEWAY_ENTER_PCT"] > 0:
                if delta > t["ONEWAY_ENTER_PCT"]:
                    oneway = "charge"
                elif delta < -t["ONEWAY_ENTER_PCT"]:
                    oneway = "discharge"
        if oneway != st["oneway"]:
            if oneway == "charge":
                self.log("ONE-WAY CHARGE %.1f%% -> %.0f%%: solar charges, shore only sustains"
                         % (soc.v, tgt.v))
            elif oneway == "discharge":
                self.log("ONE-WAY DISCHARGE %.1f%% -> %.0f%%: loads drain, nothing charges"
                         % (soc.v, tgt.v))
            elif st["oneway"] is not None:
                if not enabled:
                    why = "disabled"
                elif missing:
                    why = "no data: " + ",".join(missing)
                elif tgt is None:
                    why = "no Max Charge target"
                elif full:
                    why = "target %.0f%% is a full charge: every charger at its maximum" % tgt.v
                else:
                    why = "SOC %.1f%% at target %.0f%%" % (soc.v, tgt.v)
                self.log("ONE-WAY %s done (%s); normal engine resumes" % (st["oneway"], why))
            st["oneway"] = oneway
        owc = oneway == "charge"
        owd = oneway == "discharge"

        needW = 0.0
        if not enabled:
            if st["state"] != "shore":
                toShore("disabled")
            else:
                status[1] = "DISABLED"
            status[0] = "grey"

        elif missing:
            if st["state"] != "shore":
                toShore("no data: " + ",".join(missing))
            else:
                status[1] = "No data: " + ", ".join(missing)
            status[0] = "red"

        elif soc.v < t["SOC_EMERGENCY"] and st["state"] != "shore":
            lockout()
            toShore("EMERGENCY SOC %.1f%%" % soc.v)
            status[0] = "red"

        else:
            discharge = -batt.v
            sinceTrans = now - st["lastTransition"]
            loadSlow = inp.load_slow
            loadJudge = max(loadAvg.v, loadSlow.v) if loadSlow is not None else loadAvg.v
            needW = max(t["MIN_EST_W"], loadJudge * t["SOLAR_MARGIN"])
            # 4.4: "charger quiet" must mean the QUATTRO is quiet. Battery
            # power alone cannot tell shore charging from solar filling the
            # lead band, and under a one-way floor that band is wide enough
            # for solar to keep the bank at +250 W for hours -- which held the
            # engine on shore in full sun (2026-09-02 10:08). The Quattro's
            # DC output is battery power plus DC loads minus PV.
            dcLoad = inp.dc_load.v if inp.dc_load is not None else 0.0
            quattroW = batt.v + max(0.0, dcLoad) - pvNow

            if st["state"] == "shore":
                shoreMissing = (feed.v == 240 and sinceTrans > t["FEEDBACK_GRACE_MS"])

                if st["backoffUntil"] > now:
                    if evidence >= needW:
                        st["backoffUntil"] = 0
                        st["backoffMs"] = t["COOLDOWN_MS"]

                explore = (dayOk and not vocRising and vocMax >= t["VOC_EXPLORE_V"]
                           and capSum <= 0 and not (plantConf is not None and plantConf < needW))

                if st["burnDoneCvl"] is not None and cvl is not None:
                    if (abs(cvl.v - st["burnDoneCvl"]) > 0.02 or
                            (battV is not None and battV.v > cvl.v + t["BURN_REARM_V"])):
                        st["burnDoneCvl"] = None
                latched = st["burnDoneCvl"] is not None
                # One-way: no burn-downs. A burn-down spends the band above
                # the CVL into the loads; while charging one-way that is a
                # step backward, and while discharging the loads drain the
                # bank anyway (solar state, no deficit exit).
                surplus = (aboveCvl and soc.v >= t["MIN_SOC"] and not shoreMissing
                           and not latched and batt.v <= t["SURPLUS_QUIET_W"]
                           and not owc and not owd)
                if surplus:
                    if not st["burnReadySince"]:
                        st["burnReadySince"] = now
                else:
                    st["burnReadySince"] = 0

                if owd:
                    # Discharging: leave shore as soon as the charger is quiet
                    # (the sustain ceiling makes it so). Solar need not cover
                    # the load -- the deficit IS the plan.
                    ready = (not shoreMissing and soc.v >= t["MIN_SOC"]
                             and quattroW <= t["SURPLUS_QUIET_W"])
                else:
                    # Charging one-way: aboveCvl is judged against the
                    # sustain CVL (pinned at the SOC), which a freshly
                    # solar-charged bank sits above; the probe runs against
                    # the real target once sustain is released, so it is no
                    # reason to wait.
                    ready = (not shoreMissing and (not aboveCvl or owc)
                             and soc.v >= t["MIN_SOC"] and not shaded
                             and quattroW <= t["SURPLUS_QUIET_W"] and (est >= needW or explore))
                if ready:
                    if not st["readySince"]:
                        st["readySince"] = now
                else:
                    st["readySince"] = 0

                harvestArmed = ((st["refillReady"] is True and not st["refillShoreFed"]) or
                                (st["refillReady"] is None and evidence >= t["HARVEST_MIN_EVID"]))
                harvest = (harvestArmed and leadOn and dayOk and not shoreMissing and not aboveCvl
                           and soc.v >= t["MIN_SOC"] and batt.v <= t["SURPLUS_QUIET_W"]
                           and battV is not None and cvl is not None
                           and battV.v >= cvl.v - t["HARVEST_ARM_V"]
                           and not owc and not owd)
                if harvest:
                    if not st["harvestSince"]:
                        st["harvestSince"] = now
                else:
                    st["harvestSince"] = 0

                # Measurement boost (not gated on cooldown/backoff). Never
                # while discharging one-way: a boost charges from solar, and
                # dbus-recbms would refuse it under a sustain ceiling anyway.
                if (not boosting and dayOk and vocMax >= t["VOC_DAY_V"] and not vocRising
                        and not aboveCvl and not shoreMissing and soc.v >= t["MIN_SOC"]
                        and quattroW <= t["SURPLUS_QUIET_W"] and not owd
                        and (now - st["lastBoostTs"]) >=
                        (t["BOOST_RETRY_MS"] if capSum <= 0 else t["BOOST_INTERVAL_MS"])):
                    st["lastBoostTs"] = now
                    boostMsg[0] = t["BOOST_V"]

                gateOk = (sinceTrans >= t["COOLDOWN_MS"] and now >= st["backoffUntil"]
                          and now >= st["lockoutUntil"])

                if surplus and sinceTrans >= t["COOLDOWN_MS"] and (now - st["burnReadySince"]) >= t["READY_MS"]:
                    enter_burndown("batt %.2fV > CVL %.2fV" % (battV.v, cvl.v), False)
                elif owd and ready and gateOk and (now - st["readySince"]) >= t["READY_MS"]:
                    # No probe: nothing to prove, the loads may run the bank
                    # down. The AC-control fault check lives in solar too.
                    enter_solar("one-way discharge %.1f%% -> %.0f%%" % (soc.v, tgt.v))
                elif ready and gateOk and (now - st["readySince"]) >= t["READY_MS"]:
                    enter_probe(("est %.0fW" % est if est >= needW else "exploratory") +
                                " vs load %.0fW need %.0fW" % (loadJudge, needW) +
                                " | cap %.0f+%.0f mdl %.0f+%.0f PV %.0fW Voc %.0fV mode %s/%s bal %s" % (
                                    faded(st["cap6"]), faded(st["cap7"]),
                                    me6["w"] if me6 else 0, me7["w"] if me7 else 0, pvNow, vocMax,
                                    int(m6.v) if m6 else "-", int(m7.v) if m7 else "-",
                                    "%.2f" % balance if balance is not None else "-"))
                elif harvest and sinceTrans >= t["COOLDOWN_MS"] and (now - st["harvestSince"]) >= t["READY_MS"]:
                    enter_burndown("harvest: band full at %.2fV" % battV.v, True)
                elif surplus:
                    status[0] = "blue"
                    status[1] = "SHORE | SURPLUS batt %.2fV > CVL %.2fV" % (battV.v, cvl.v)
                    if sinceTrans < t["COOLDOWN_MS"]:
                        status[1] += " [cd %ds]" % math.ceil((t["COOLDOWN_MS"] - sinceTrans) / 1000)
                    else:
                        status[1] += " [burn in %ds]" % math.ceil((t["READY_MS"] - (now - st["burnReadySince"])) / 1000)
                elif aboveCvl:
                    status[0] = "blue"
                    status[1] = "SHORE | batt %.2fV > CVL %.2fV " % (battV.v, cvl.v) + (
                        "[burned - re-arms on CVL change]" if latched else
                        ("[charger active +%.0fW]" % batt.v if batt.v > t["SURPLUS_QUIET_W"] else "[SOC/shore gate]"))
                else:
                    status[0] = "red" if shoreMissing else "blue"
                    s = ("NO SHORE? | " if shoreMissing else "SHORE | ") + \
                        "est %.0fW need %.0fW" % (est, needW)
                    if capSum > 0:
                        s += " cap %.0f" % capSum
                    if modelSum > 0:
                        s += " mdl %.0f" % modelSum + ("+" if ((me6 and me6["lb"]) or (me7 and me7["lb"])) else "")
                    if explore:
                        s += " (explore-ok)"
                    if plantConf is not None and plantConf < needW:
                        s += " (dim)"
                    if batt.v > t["SURPLUS_QUIET_W"]:
                        s += " [chg +%.0fW%s]" % (batt.v, "" if quattroW > t["SURPLUS_QUIET_W"] else " solar")
                    if harvestArmed:
                        s += " [hv-armed]" if leadOn else " [hv-off: no lead]"
                    if leadFault:
                        s += " [LEAD FAULT]"
                    if balance is not None:
                        s += " bal %.2f" % balance + (" [SHADE]" if shaded else "")
                    s += " Voc %.0fV SOC %.0f%%" % (vocMax, soc.v)
                    if now < st["lockoutUntil"]:
                        s += " [LOCKOUT %dm]" % math.ceil((st["lockoutUntil"] - now) / 60000)
                    elif now < st["backoffUntil"]:
                        s += " [backoff %dm]" % math.ceil((st["backoffUntil"] - now) / 60000)
                    elif sinceTrans < t["COOLDOWN_MS"]:
                        s += " [cd %ds]" % math.ceil((t["COOLDOWN_MS"] - sinceTrans) / 1000)
                    elif ready:
                        s += " [confirm %ds]" % math.ceil((t["READY_MS"] - (now - st["readySince"])) / 1000)
                    status[1] = s

            elif st["state"] == "probe":
                elapsed = now - st["probeStart"]
                bigLoad = loadNow.v > st["probeEst"] * 1.5
                if not owc:
                    bigLoad = bigLoad or loadAvg.v * t["SOLAR_MARGIN"] > st["probeEst"]
                if bigLoad:
                    if not st["surgeStart"]:
                        st["surgeStart"] = now
                else:
                    st["surgeStart"] = 0

                if st["surgeStart"] and now - st["surgeStart"] >= t["SURGE_MS"]:
                    toShore("big load during probe (%.0fW, avg %.0fW vs est %.0fW) | PV %.0fW bal %s" % (
                        loadNow.v, loadAvg.v, st["probeEst"], pvNow,
                        "%.2f" % balance if balance is not None else "-"))
                    status[0] = "blue"
                elif (elapsed >= t["WAKE_MS"] and pvNow < 10
                      and not (m6 and m6.v == 2) and not (m7 and m7.v == 2)):
                    escalateBackoff()
                    toShore("probe: MPPTs never woke (batt %sV, CVL %sV)" % (
                        "%.2f" % battV.v if battV else "?", "%.2f" % cvl.v if cvl else "?"))
                    status[0] = "blue"
                elif elapsed < t["RAMP_MS"]:
                    if elapsed >= t["RAMP_MS"] - t["EVAL_MS"]:
                        st["evalPv"].append(pvNow)
                        st["evalBatt"].append(batt.v)
                    status[0] = "yellow"
                    status[1] = "PROBE %ds | PV %.0fW batt %.0fW load %.0fW" % (
                        math.ceil((t["RAMP_MS"] - elapsed) / 1000), pvNow, batt.v, loadNow.v)
                else:
                    pvAvg = sum(st["evalPv"]) / len(st["evalPv"]) if st["evalPv"] else pvNow
                    battAvg = sum(st["evalBatt"]) / len(st["evalBatt"]) if st["evalBatt"] else batt.v
                    if feed.v == FEED_SHORE:
                        self.log("ERROR IgnoreAcIn has no effect - check the vebus instance / firmware")
                        lockout()
                        toShore("FAULT: AC control ineffective")
                        status[0] = "red"
                    elif battAvg > -(t["ONEWAY_DEFICIT_W"] if owc else t["DISCHARGE_TOL_W"]):
                        enter_solar("PV %.0fW, batt %.0fW" % (pvAvg, battAvg))
                    else:
                        escalateBackoff()
                        toShore("probe failed: PV %.0fW, batt %.0fW | y %.0f+%.0f Voc %.0fV bal %s" % (
                            pvAvg, battAvg, y6.v if y6 else 0, y7.v if y7 else 0, vocMax,
                            "%.2f" % balance if balance is not None else "-"))
                        status[0] = "blue"

            elif st["state"] == "burndown":
                if loadNow.v >= t["SUSPEND_LOAD_W"]:
                    if not st["suspendTrigStart"]:
                        st["suspendTrigStart"] = now
                        st["suspendBase"] = loadAvg.v
                else:
                    st["suspendTrigStart"] = 0

                if st["suspendTrigStart"] and now - st["suspendTrigStart"] >= t["SUSPEND_MS"]:
                    enter_suspend("burndown")
                elif feed.v == FEED_SHORE and sinceTrans > t["FEEDBACK_GRACE_MS"]:
                    self.log("ERROR Quattro re-accepted AC during burn-down - standing down")
                    lockout()
                    toShore("FAULT: AC re-accepted externally")
                    status[0] = "red"
                else:
                    calmP = battMean if battMean is not None else batt.v
                    if sinceTrans > t["BURN_CALM_GATE_MS"] and calmP > -t["DISCHARGE_TOL_W"]:
                        if not st["burnCalmStart"]:
                            st["burnCalmStart"] = now
                    else:
                        st["burnCalmStart"] = 0
                    burnDone = (battV is not None and cvl is not None
                                and battV.v <= cvl.v - t["BURN_EXIT_DROP_V"])
                    if burnDone:
                        if not st["burnExitStart"]:
                            st["burnExitStart"] = now
                    else:
                        st["burnExitStart"] = 0

                    if st["burnCalmStart"] and now - st["burnCalmStart"] >= t["BURN_CALM_MS"]:
                        enter_solar("burn-down handover, PV %.0fW" % pvNow)
                    elif st["burnExitStart"] and now - st["burnExitStart"] >= t["BURN_EXIT_MS"]:
                        st["burnDoneCvl"] = cvl.v
                        if est >= needW:
                            enter_probe("after burn-down, est %.0fW" % est)
                        else:
                            toShore("burn-down complete (batt %.2fV)" % battV.v)
                            status[0] = "blue"
                    else:
                        status[0] = "green"
                        status[1] = "BURN | batt %.0fW, %sV -> %sV, PV %.0fW load %.0fW SOC %.1f%%" % (
                            batt.v, "%.2f" % battV.v if battV is not None else "?",
                            "%.2f" % (cvl.v - t["BURN_EXIT_DROP_V"]) if cvl is not None else "?",
                            pvNow, loadNow.v, soc.v)

            elif st["state"] == "suspend":
                if now - st["suspendStart"] >= t["SUSPEND_MAX_MS"]:
                    toShore("suspend timeout after %dmin" % round(t["SUSPEND_MAX_MS"] / 60000))
                    status[0] = "blue"
                elif loadNow.v <= st["suspendBase"] + t["RESUME_DELTA_W"]:
                    if not st["resumeStart"]:
                        st["resumeStart"] = now
                    if now - st["resumeStart"] >= t["RESUME_MS"]:
                        st["resumeStart"] = 0
                        st["desired"] = 1
                        st["lastTransition"] = now
                        st["surgeStart"] = 0
                        st["loadExceedStart"] = 0
                        if (st["suspendPrev"] == "burndown" and battV is not None and cvl is not None
                                and battV.v > cvl.v - t["BURN_EXIT_DROP_V"]):
                            st["state"] = "burndown"
                            st["burnExitStart"] = 0
                            st["burnCalmStart"] = 0
                            transition[0] = "-> BURNDOWN (resumed after suspend)"
                        else:
                            st["state"] = "solar"
                            st["solarSince"] = now
                            st["socEntry"] = soc.v
                            if not owd:
                                st["lastBoostTs"] = now
                                boostMsg[0] = t["BOOST_V"]     # re-ramp assist
                            transition[0] = "-> SOLAR (resumed after suspend)"
                        status[0] = "green"
                        status[1] = transition[0]
                    else:
                        status[0] = "yellow"
                        status[1] = "SUSPEND | load %.0fW back at base - resume in %ds" % (
                            loadNow.v, math.ceil((t["RESUME_MS"] - (now - st["resumeStart"])) / 1000))
                else:
                    st["resumeStart"] = 0
                    status[0] = "blue"
                    status[1] = "SUSPEND | load %.0fW (base %.0fW) - timeout in %dmin" % (
                        loadNow.v, st["suspendBase"],
                        math.ceil((t["SUSPEND_MAX_MS"] - (now - st["suspendStart"])) / 60000))

            else:  # ---- SOLAR ----
                if st["backoffMs"] != t["COOLDOWN_MS"] and now - st["solarSince"] >= t["STABLE_MS"]:
                    st["backoffMs"] = t["COOLDOWN_MS"]

                draining = battV is not None and effCvl is not None and battV.v > effCvl + 0.01
                settling = draining or (now - st["solarSince"]) < t["SOLAR_SETTLE_MS"]

                if owc:
                    # one-way charge: the ten-minute mean against the
                    # one-way tolerance; a surge alone never ends it
                    # (heater-class loads still suspend)
                    tol = t["ONEWAY_DEFICIT_W"]
                    dischargeAvg = -battMeanLong if battMeanLong is not None else -float(t["ONEWAY_DEFICIT_W"])
                else:
                    tol = t["DISCHARGE_TOL_W"]
                    dischargeAvg = -battMean if battMean is not None else discharge
                if dischargeAvg > tol and not settling:
                    if not st["loadExceedStart"]:
                        st["loadExceedStart"] = now
                else:
                    st["loadExceedStart"] = 0
                if discharge > t["SURGE_W"] and not draining and not owc:
                    if not st["surgeStart"]:
                        st["surgeStart"] = now
                else:
                    st["surgeStart"] = 0

                if loadNow.v >= t["SUSPEND_LOAD_W"]:
                    if not st["suspendTrigStart"]:
                        st["suspendTrigStart"] = now
                        st["suspendBase"] = loadAvg.v
                else:
                    st["suspendTrigStart"] = 0

                # Note: the flow's ceiling-stall condition also tested
                # `!shoreMissing`, but that variable is only assigned in the
                # shore branch (JS `var` hoisting) so it was always undefined
                # here = no gate. ActiveInput is 240 while inverting anyway, so
                # shore presence cannot be judged in this state; the port
                # keeps the flow's actual behaviour (no gate).
                if st["suspendTrigStart"] and now - st["suspendTrigStart"] >= t["SUSPEND_MS"]:
                    enter_suspend("solar")
                elif feed.v == FEED_SHORE and sinceTrans > t["FEEDBACK_GRACE_MS"]:
                    self.log("ERROR Quattro re-accepted AC during solar mode - standing down")
                    lockout()
                    toShore("FAULT: AC re-accepted externally")
                    status[0] = "red"
                elif soc.v < t["MIN_SOC"]:
                    escalateBackoff()
                    toShore("SOC %.1f%% (entry %.1f%%)" % (soc.v, st["socEntry"]))
                    status[0] = "blue"
                elif owd:
                    # Discharging one-way: a deficit, a surge or SOC drift is
                    # the bank doing exactly what was asked. Only the floor,
                    # a heater-class load (suspend) and a fault end this.
                    st["loadExceedStart"] = 0
                    st["surgeStart"] = 0
                    status[0] = "green"
                    status[1] = "DRAIN | PV %.0fW batt %s%.0fW load %.0fW SOC %.1f%% -> %.0f%%" % (
                        pvNow, "+" if batt.v >= 0 else "", batt.v, loadNow.v, soc.v, tgt.v)
                elif soc.v < st["socEntry"] - t["SOC_DRIFT_MAX"]:
                    escalateBackoff()
                    toShore("SOC %.1f%% (entry %.1f%%)" % (soc.v, st["socEntry"]))
                    status[0] = "blue"
                elif st["surgeStart"] and now - st["surgeStart"] >= t["SURGE_MS"]:
                    toShore("big load: batt -%.0fW, load %.0fW" % (discharge, loadNow.v))
                    status[0] = "blue"
                elif (st["loadExceedStart"] and now - st["loadExceedStart"] >= t["LOAD_EXCEED_MS"]
                      and dayOk and soc.v >= t["MIN_SOC"]
                      and battV is not None and cvl is not None
                      and battV.v >= cvl.v - t["STALL_BURN_MIN_V"]):
                    # Ceiling stall (v4.1): burn the band, no backoff
                    st["loadExceedStart"] = 0
                    enter_burndown("ceiling stall: batt avg -%.0fW at %.2fV, PV %.0fW" % (
                        dischargeAvg, battV.v, pvNow), True)
                elif st["loadExceedStart"] and now - st["loadExceedStart"] >= t["LOAD_EXCEED_MS"]:
                    escalateBackoff()
                    toShore("deficit: batt avg -%.0fW (now %.0fW)" % (dischargeAvg, batt.v))
                    status[0] = "blue"
                elif st["loadExceedStart"] or st["surgeStart"]:
                    left = (math.ceil((t["SURGE_MS"] - (now - st["surgeStart"])) / 1000) if st["surgeStart"]
                            else math.ceil((t["LOAD_EXCEED_MS"] - (now - st["loadExceedStart"])) / 1000))
                    status[0] = "yellow"
                    status[1] = "SOLAR! batt avg -%.0fW (now %.0fW) PV %.0fW load %.0fW [shore in %ds]" % (
                        dischargeAvg, batt.v, pvNow, loadNow.v, left)
                else:
                    status[0] = "green"
                    status[1] = ("SOLAR drain +%.2fV | PV " % (battV.v - effCvl) if draining else "SOLAR | PV ") + \
                        "%.0fW batt %s%.0fW load %.0fW SOC %.1f%%" % (
                            pvNow, "+" if batt.v >= 0 else "", batt.v, loadNow.v, soc.v)

        if oneway and status[1] and not status[1].startswith("->"):
            status[1] = "%s %.0f->%.0f%% | %s" % (
                "1-WAY CHARGE" if owc else "1-WAY DISCHARGE", soc.v, tgt.v, status[1])

        # ---- Command emission ----
        if st["lastSent"] != st["desired"] or (enabled and now - st["lastAssert"] >= t["ASSERT_MS"]):
            st["lastSent"] = st["desired"]
            st["lastAssert"] = now
            out.cmd = st["desired"]

        # Sustain: charging one-way, the bank is held only while the charger
        # is connected (shore, suspend) -- solar must be free to charge the
        # rest of the time. Discharging, it is a ceiling the whole time.
        # Re-asserted every ASSERT_MS: dbus-recbms expires it on its own.
        # 4.5: "connected" means the Quattro reports the shore input as its
        # active input, not merely that the engine asked for it. With no AC
        # available (2026-09-06: a 1 kW load on solar, no shore power) the
        # Quattro keeps inverting whatever it is told, and a floor then does
        # nothing but pin the CVL at the present SOC and stop solar charging.
        onShore = feed is not None and feed.v == FEED_SHORE
        if owc:
            want = (SUSTAIN_FLOOR if st["state"] in ("shore", "suspend") and onShore
                    else SUSTAIN_OFF)
        elif owd:
            want = SUSTAIN_CEILING
        else:
            want = SUSTAIN_OFF
        if st["sustainSent"] != want or (want and now - st["sustainAssert"] >= t["ASSERT_MS"]):
            st["sustainSent"] = want
            st["sustainAssert"] = now
            out.sustain = want

        out.transition = transition[0]
        out.boost = boostMsg[0]
        out.oneway = oneway or ""
        out.status_fill, out.status_text = status
        out.est = est
        out.need_w = needW
        out.state = st["state"]
        return out


# ----------------------------------------------------------------------------
# D-Bus glue
# ----------------------------------------------------------------------------
def private_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus(private=True)
    return dbus.SystemBus(private=True)


def shared_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


def _q(value, step):
    """Round to the nearest multiple of `step`; None passes through.

    Integer steps give ints, fractional ones a float rounded to the step's
    own number of decimals so 0.1 never comes back as 0.30000000000000004.
    """
    if value is None:
        return None
    n = round(value / step) * step
    if float(step).is_integer():
        return int(round(n))
    return round(n, max(0, -int(math.floor(math.log10(step)))))


def new_service(name, bus):
    import inspect
    if "register" in inspect.signature(VeDbusService.__init__).parameters:
        svc = VeDbusService(name, bus=bus, register=False)
        svc._sp_needs_register = True
    else:
        svc = VeDbusService(name, bus=bus)
        svc._sp_needs_register = False
    return svc


def register_service(svc):
    if getattr(svc, "_sp_needs_register", False):
        svc.register()


# (service class, path) -> (input field, validator, is_heartbeat)
def _rng(lo, hi):
    return lambda v: lo <= v <= hi


INPUT_MAP = {
    ("solarcharger", "/Pv/V"):            ("voc", _rng(0, 150)),
    ("solarcharger", "/Yield/Power"):     ("y", _rng(0, 3000)),
    ("solarcharger", "/MppOperationMode"): ("m", lambda v: True),
    ("system", "/Ac/Consumption/L1/Power"): ("load_now", _rng(0, 20000)),
    ("system", "/Dc/Battery/Soc"):        ("soc", _rng(0, 100)),
    ("system", "/Dc/Battery/Power"):      ("batt", lambda v: abs(v) <= 30000),
    ("system", "/Dc/Battery/Voltage"):    ("batt_v", _rng(20, 80)),
    ("system", "/Dc/System/Power"):       ("dc_load", _rng(-5000, 5000)),
    ("vebus", "/Ac/ActiveIn/ActiveInput"): ("feed", lambda v: True),
    ("vebus", "/Ac/Out/L1/P"):            ("ac_out", _rng(-20000, 20000)),
    ("battery", "/RecBms/TargetChargeVoltage"): ("cvl", _rng(20, 80)),
    ("battery", "/RecBms/SolarBoost/Active"):   ("boost_active", lambda v: True),
    ("battery", "/RecBms/SolarBoost/WindowOpen"): ("boost_window", lambda v: True),
    ("battery", "/RecBms/SolarBoost/EffectiveChargeVoltage"): ("boost_eff", _rng(20, 80)),
    ("battery", "/RecBms/SolarLead"):     ("lead", _rng(0, 1)),
    # dbus-recbms >= 1.5.0; absent on older builds, which simply leaves
    # one-way mode off (target_soc stays None)
    ("battery", "/RecBms/TargetSoc"):     ("target_soc", _rng(0, 100)),
    ("battery", "/RecBms/Sustain/Active"): ("sustain_active", lambda v: True),
}
WRITE_PATHS = {
    "vebus": ["/Ac/Control/IgnoreAcIn1", "/Ac/Control/IgnoreAcIn2"],
    "battery": ["/RecBms/SolarBoost/Request", "/RecBms/LeadFault",
                "/RecBms/Sustain/Request"],
}


class SolarPriorityDriver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.now0 = time.time()
        self.inp = Inputs()
        self.inp.feed_shore = 0 if cfg.ac_in == 1 else 1
        self.ignore_path = "/Ac/Control/IgnoreAcIn%d" % cfg.ac_in
        self.load_window = []
        self.load_window_slow = []
        self.last_status = None
        self.engine = Engine(cfg.engine, self._ms(), logger=self._engine_log)
        self.sbus = shared_bus()

        self._init_settings()
        self.inp.enabled = bool(int(self.settings["enabled"]))
        self.inp.p_rated = float(self.settings["rated"])
        self._init_switch_service()

        dummy = {"code": None, "whenToLog": "configChange", "accessLevel": None}
        tree = {}
        for (cls, path) in INPUT_MAP:
            tree.setdefault("com.victronenergy." + cls, {})[path] = dummy
        for cls, paths in WRITE_PATHS.items():
            for p in paths:
                tree.setdefault("com.victronenergy." + cls, {})[p] = dummy
        for cls in ("solarcharger", "vebus", "battery"):
            tree["com.victronenergy." + cls]["/DeviceInstance"] = dummy
        self.monitor = DbusMonitor(tree, valueChangedCallback=self._value_changed,
                                   deviceAddedCallback=self._device_added,
                                   deviceRemovedCallback=self._device_removed)
        self._seed_inputs()

        # Safe start: command shore (flow: "Safe Start" inject at +3 s)
        GLib.timeout_add_seconds(3, self._safe_start)
        atexit.register(self._shutdown)
        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(_sig, self._on_signal)
            except (ValueError, OSError):
                pass
        GLib.timeout_add(cfg.tick_ms, self._tick)
        log.info("engine v%s, tick %d ms, shore on AC-in %d", ENGINE_VERSION,
                 cfg.tick_ms, cfg.ac_in)

    def _ms(self):
        return int(time.time() * 1000)

    def _engine_log(self, msg):
        if msg.startswith("ERROR "):
            log.error(msg[6:])
        else:
            log.info(msg)

    # ------------------------------------------------------------ settings
    def _init_settings(self):
        c = self.cfg
        supported = {
            "instance": ["/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                         "switch:%d" % c.instance, 0, 0],
            "enabled": ["/Settings/SolarPriority/Enabled", 0, 0, 1],
            "rated": ["/Settings/SolarPriority/RatedPower", int(c.rated_default),
                      int(c.rated_min), int(c.rated_max)],
        }
        self.settings = SettingsDevice(self.sbus, supported, self._setting_changed, timeout=120)
        granted = self._parse_instance(self.settings["instance"], c.instance)
        if granted != c.instance:
            # reconverge like dbus-recbms: only if nobody live holds it
            in_use = False
            for name in self.sbus.list_names():
                if not str(name).startswith("com.victronenergy.switch."):
                    continue
                try:
                    di = self.sbus.call_blocking(name, "/DeviceInstance", BUSITEM,
                                                 "GetValue", "", [], timeout=2)
                    if int(di) == c.instance:
                        in_use = True
                        break
                except Exception:
                    continue
            if in_use:
                log.warning("instance %d held by a live service (the old Node-RED "
                            "flow?); using %d", c.instance, granted)
            else:
                try:
                    self.sbus.call_blocking(
                        "com.victronenergy.settings",
                        "/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                        BUSITEM, "SetValue", "v", ["switch:%d" % c.instance], timeout=5)
                    # localsettings silently keeps the old number when the
                    # wanted one is held by ANOTHER Devices/* entry (e.g. the
                    # retired flow's virtual_sp_* settings, until the registry
                    # orphan cleanup removes them) — re-read instead of trusting
                    # the write.
                    back = self._parse_instance(self.sbus.call_blocking(
                        "com.victronenergy.settings",
                        "/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                        BUSITEM, "GetValue", "", [], timeout=5), granted)
                    if back == c.instance:
                        log.info("reconverged instance %d -> %d", granted, c.instance)
                        granted = c.instance
                    else:
                        log.warning("instance %d is reserved by another settings entry "
                                    "(a virtual_* orphan?); localsettings kept %d. "
                                    "Remove the orphan, then svc -t this service.",
                                    c.instance, back)
                        granted = back
                except Exception as e:
                    log.warning("could not pin instance %d (%s); using %d", c.instance, e, granted)
        self.instance = granted

    @staticmethod
    def _parse_instance(value, fallback):
        try:
            return int(str(value).split(":")[1])
        except (IndexError, ValueError):
            return fallback

    def _setting_changed(self, setting, old, new):
        # external write to localsettings: mirror to the switch + engine
        try:
            if setting == "enabled":
                self._set_enabled(bool(int(new)), persist=False)
                self.sw["/SwitchableOutput/output_1/State"] = 1 if self.inp.enabled else 0
            elif setting == "rated":
                self.inp.p_rated = float(new)
                self.sw["/SwitchableOutput/output_2/Dimming"] = float(new)
        except Exception:
            pass

    # ------------------------------------------------------ switch service
    def _init_switch_service(self):
        c = self.cfg
        svc = new_service("com.victronenergy.switch.%s" % c.suffix, private_bus())
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", "%s on Python %s" % (VERSION, platform.python_version()))
        svc.add_path("/Mgmt/Connection", "dbus-solarpriority")
        svc.add_path("/DeviceInstance", self.instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", "Solar Priority")
        svc.add_path("/CustomName", "Solar Priority")
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/Serial", c.settings_id)
        svc.add_path("/Connected", 1)
        svc.add_path("/State", 0x100)

        o1 = "/SwitchableOutput/output_1"
        svc.add_path(o1 + "/State", 1 if self.inp.enabled else 0, writeable=True,
                     onchangecallback=self._toggle_changed)
        svc.add_path(o1 + "/Status", 0)
        svc.add_path(o1 + "/Name", "Enable")
        svc.add_path(o1 + "/Settings/Type", 1, writeable=True,
                     onchangecallback=lambda p, v: v in (1, 1.0))
        svc.add_path(o1 + "/Settings/ValidTypes", 1 << 1)
        svc.add_path(o1 + "/Settings/CustomName", c.toggle_name)
        svc.add_path(o1 + "/Settings/Group", c.group)
        svc.add_path(o1 + "/Settings/ShowUIControl", 1)
        svc.add_path(o1 + "/Settings/Adjustable", 0)

        o2 = "/SwitchableOutput/output_2"
        svc.add_path(o2 + "/State", 0, writeable=True, onchangecallback=lambda p, v: True)
        svc.add_path(o2 + "/Status", 0)
        svc.add_path(o2 + "/Name", "Rated PV (W)")
        svc.add_path(o2 + "/Dimming", self.inp.p_rated, writeable=True,
                     onchangecallback=self._rated_changed,
                     gettextcallback=lambda p, v: "---" if v is None else "%.0fW" % float(v))
        svc.add_path(o2 + "/Settings/Type", 7, writeable=True,
                     onchangecallback=lambda p, v: v in (7, 7.0))
        svc.add_path(o2 + "/Settings/ValidTypes", 1 << 7)
        svc.add_path(o2 + "/Settings/CustomName", c.slider_name)
        svc.add_path(o2 + "/Settings/Group", c.group)
        svc.add_path(o2 + "/Settings/ShowUIControl", 1)
        svc.add_path(o2 + "/Settings/Adjustable", 0)
        svc.add_path(o2 + "/Settings/DimmingMin", c.rated_min)
        svc.add_path(o2 + "/Settings/DimmingMax", c.rated_max)
        svc.add_path(o2 + "/Settings/StepSize", c.rated_step)
        svc.add_path(o2 + "/Settings/Unit", "W")

        # Diagnostics (read-only; what the flow showed as node status)
        svc.add_path("/SolarPriority/State", "shore")
        svc.add_path("/SolarPriority/Status", "")
        svc.add_path("/SolarPriority/StatusFill", "grey")
        svc.add_path("/SolarPriority/LastTransition", "")
        svc.add_path("/SolarPriority/LastTransitionTime", 0)
        svc.add_path("/SolarPriority/EstimateW", 0.0)
        svc.add_path("/SolarPriority/NeedW", 0.0)
        svc.add_path("/SolarPriority/Desired", 0)
        svc.add_path("/SolarPriority/Missing", "")
        svc.add_path("/SolarPriority/EngineVersion", ENGINE_VERSION)
        # one-way charge / discharge (4.3): "", "charge" or "discharge", and
        # the Max Charge target it is judged against (None: dbus-recbms too old)
        svc.add_path("/SolarPriority/OneWay", "")
        svc.add_path("/SolarPriority/TargetSoc", None,
                     gettextcallback=lambda p, v: "---" if v is None else "%.0f%%" % float(v))
        svc.add_path("/SolarPriority/Sustain", 0)

        register_service(svc)
        self.sw = svc
        log.info("registered com.victronenergy.switch.%s instance %d (enabled=%d, rated %.0fW)",
                 c.suffix, self.instance, self.inp.enabled, self.inp.p_rated)

    def _toggle_changed(self, path, value):
        try:
            on = int(value) == 1
        except (TypeError, ValueError):
            return False
        self._set_enabled(on, persist=True)
        return True

    def _set_enabled(self, on, persist):
        was = self.inp.enabled
        self.inp.enabled = on
        if on and not was:
            self.engine.reset_backoff()
        if persist:
            try:
                self.settings["enabled"] = 1 if on else 0
            except Exception:
                log.warning("could not persist enable state")
        if on != was:
            log.info("Solar Priority %s", "ENABLED" if on else "DISABLED")

    def _rated_changed(self, path, value):
        c = self.cfg
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(v) and c.rated_min <= v <= c.rated_max):
            return False
        self.inp.p_rated = v
        try:
            self.settings["rated"] = int(round(v))
        except Exception:
            log.warning("could not persist rated power")
        log.info("PV capacity cap -> %.0fW", v)
        return True

    # --------------------------------------------------------------- inputs
    def _svc(self, cls, instance):
        for name, inst in self.monitor.get_service_list("com.victronenergy." + cls).items():
            if inst == instance:
                return name
        return None

    def _field_for(self, service, path):
        cls = service.split(".")[2]
        spec = INPUT_MAP.get((cls, path))
        if spec is None:
            return None, None
        field, valid = spec
        inst = self.monitor.get_device_instance(service)
        c = self.cfg
        if cls == "solarcharger":
            if inst == c.mppt6_instance:
                field += "6"
            elif inst == c.mppt7_instance:
                field += "7"
            else:
                return None, None
        elif cls == "vebus" and inst != c.vebus_instance:
            return None, None
        elif cls == "battery" and inst != c.battery_instance:
            return None, None
        return field, valid

    def _store(self, service, path, value, now):
        field, valid = self._field_for(service, path)
        if field is None or value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(v) or not valid(v):
            return
        setattr(self.inp, field, Val(v, now))
        if field == "load_now":
            # Time-based 60 s rolling window (flow: "Store AC Load")
            w = self.load_window
            w.append((now, v))
            cutoff = now - self.cfg.engine["LOAD_AVG_MS"]
            while w and w[0][0] < cutoff:
                w.pop(0)
            if len(w) > 300:
                del w[:len(w) - 300]
            self.inp.load_avg = Val(sum(x for _, x in w) / len(w), now)
            # 4.4: a slower running average for the pre-probe need
            ws = self.load_window_slow
            ws.append((now, v))
            cutoff = now - self.cfg.engine["LOAD_SLOW_MS"]
            while ws and ws[0][0] < cutoff:
                ws.pop(0)
            if len(ws) > 1500:
                del ws[:len(ws) - 1500]
            self.inp.load_slow = Val(sum(x for _, x in ws) / len(ws), now)

    def _store_fault(self, service, value):
        if self.monitor.get_device_instance(service) != self.cfg.battery_instance:
            return
        txt = "" if value is None else str(value).strip()
        if txt and txt != self.inp.lead_fault:
            log.error("SOLAR LEAD FAULT (dbus-recbms): %s -- boosts refused, harvest disabled", txt)
        elif not txt and self.inp.lead_fault:
            log.info("solar lead restored (dbus-recbms verified the offset again)")
        self.inp.lead_fault = txt

    def _value_changed(self, service, path, options, changes, instance):
        now = self._ms()
        if path == "/RecBms/LeadFault":
            self._store_fault(service, changes.get("Value"))
            return
        self._store(service, path, changes.get("Value"), now)

    def _seed_inputs(self):
        """Startup: take current values as last-known-good (ts = now)."""
        now = self._ms()
        for (cls, path) in INPUT_MAP:
            for name in self.monitor.get_service_list("com.victronenergy." + cls):
                self._store(name, path, self.monitor.get_value(name, path), now)
        b = self._svc("battery", self.cfg.battery_instance)
        if b:
            self._store_fault(b, self.monitor.get_value(b, "/RecBms/LeadFault"))

    def _device_added(self, service, instance, *args):
        log.info("service appeared: %s (instance %s)", service, instance)
        now = self._ms()
        cls = service.split(".")[2]
        for (c2, path) in INPUT_MAP:
            if c2 == cls:
                self._store(service, path, self.monitor.get_value(service, path), now)
        if cls == "battery" and instance == self.cfg.battery_instance:
            # A restarted dbus-recbms comes up on the plain slider: re-assert
            # the sustain on the next tick rather than at the 30 s cycle.
            self.engine.st["sustainSent"] = None

    def _device_removed(self, service, instance):
        log.warning("service vanished: %s (instance %s)", service, instance)
        # Values stay last-known-good; the heartbeat check takes it from here.

    # -------------------------------------------------------------- outputs
    def _write(self, cls, instance, path, value, what, on_error=None):
        name = self._svc(cls, instance)
        if name is None:
            log.warning("%s: no %s service with instance %d", what, cls, instance)
            return False

        def failed(e):
            log.warning("%s: write %s%s failed: %s", what, name, path, e)
            if on_error is not None:
                on_error(e)
        try:
            self.monitor.set_value_async(name, path, value, error_handler=failed)
            return True
        except Exception as e:
            log.warning("%s: write %s%s failed: %s", what, name, path, e)
            return False

    def _sustain_failed(self, e):
        # dbus-recbms refused or is absent: ask again next tick, not in 30 s
        self.engine.st["sustainSent"] = None

    def _safe_start(self):
        self._write("vebus", self.cfg.vebus_instance, self.ignore_path, 0, "safe start")
        return False

    def _shutdown(self):
        # A dead driver must never leave the Quattro inverting.
        try:
            name = self._svc("vebus", self.cfg.vebus_instance)
            if name:
                self.monitor.set_value(name, self.ignore_path, 0)
            b = self._svc("battery", self.cfg.battery_instance)
            if b:
                self.monitor.set_value(b, "/RecBms/SolarBoost/Request", 0.0)
            # The sustain hold is deliberately NOT released here: it expires
            # in dbus-recbms on its own (120 s) if we really die, and a
            # planned restart re-asserts it within seconds. Releasing it put
            # the full slider CVL on the Quattro for the restart gap, and it
            # answered with a 1.4 kW re-absorb burst (seen 2026-09-02).
            log.info("shutdown: IgnoreAcIn%d=0, boost released (sustain left to expire)",
                     self.cfg.ac_in)
        except Exception as e:
            log.warning("shutdown write failed: %s", e)

    def _on_signal(self, signum, frame):
        self._shutdown()
        raise SystemExit(0)

    # ----------------------------------------------------------------- tick
    def _tick(self):
        now = self._ms()
        try:
            out = self.engine.tick(now, self.inp)
        except Exception:
            log.exception("decision error - forcing shore")
            self.engine.force_shore(now)
            self._write("vebus", self.cfg.vebus_instance, self.ignore_path, 0, "error->shore")
            self.sw["/SolarPriority/Status"] = "error -> shore (see log)"
            self.sw["/SolarPriority/StatusFill"] = "red"
            return True

        if out.cmd is not None:
            self._write("vebus", self.cfg.vebus_instance, self.ignore_path, int(out.cmd), "AC control")
        # sustain before boost: a probe releases the hold and asks for a
        # boost in the same tick, and the boost is gated on the target
        if out.sustain is not None:
            if not self._write("battery", self.cfg.battery_instance, "/RecBms/Sustain/Request",
                               int(out.sustain), "sustain", on_error=self._sustain_failed):
                self._sustain_failed(None)
        if out.boost is not None:
            self._write("battery", self.cfg.battery_instance, "/RecBms/SolarBoost/Request",
                        float(out.boost), "boost")
        if out.transition:
            log.info("%s", out.transition)

        # one ItemsChanged per tick, carrying only what moved
        with self.sw as s:
            if out.transition:
                s["/SolarPriority/LastTransition"] = out.transition
                s["/SolarPriority/LastTransitionTime"] = int(now / 1000)
            s["/SolarPriority/State"] = out.state
            if out.status_text != self.last_status:
                s["/SolarPriority/Status"] = out.status_text
                self.last_status = out.status_text
            s["/SolarPriority/StatusFill"] = out.status_fill
            s["/SolarPriority/EstimateW"] = _q(out.est, self.cfg.power_step)
            s["/SolarPriority/NeedW"] = _q(out.need_w, self.cfg.power_step)
            s["/SolarPriority/Desired"] = int(self.engine.st["desired"])
            s["/SolarPriority/OneWay"] = out.oneway
            s["/SolarPriority/TargetSoc"] = (self.inp.target_soc.v
                                             if self.inp.target_soc else None)
            s["/SolarPriority/Sustain"] = int(self.engine.st["sustainSent"] or 0)
            s["/SwitchableOutput/output_1/State"] = 1 if self.inp.enabled else 0
        return True


# ----------------------------------------------------------------------------
def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_priority.ini")
    cfg = Config(cfg_path)
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("dbus-solarpriority v%s starting (velib: %s)", VERSION, _VELIB_DIR)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    SolarPriorityDriver(cfg)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
