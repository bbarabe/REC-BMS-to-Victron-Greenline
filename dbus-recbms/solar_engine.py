"""Pure Solar Priority decision engine, restored from 386e125 (engine 4.9).

All times are monotonic milliseconds. No I/O or relay ownership lives here.
The adapter must supply observed values, physical AC feedback and measured
Quattro DC power. See reviews/solar-engine-baseline-deviations.md.
"""
import math

ENGINE_VERSION = "4.12"

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
    # 4.12 (issue #4): 4.3's ENTER of 5 let a whole slider step (5 points),
    # a 3-point change and a restart with under 5 points to go all pass as
    # HOLD, and HOLD lets the shore charger fill the gap (62 -> 65 % at
    # night: 1.44 kWh from shore in 20 min, E06). One full SOC point is the
    # step dbus-recbms itself treats as real movement (sustain step_pct);
    # half a point is five times the servo's 0.1 % deadband and fifty times
    # the hi-res SOC resolution, well clear of the hundredths the Quattro's
    # bursts nudge it by. Direction flips only through the whole band.
    "ONEWAY_ENTER_PCT": 1, "ONEWAY_EXIT_PCT": 0.5,
    # pre-probe checks (4.4), from the evening of 2026-09-01 in Home
    # Assistant: five probes on model estimates of 374-1381 W while every
    # unthrottled reading after 17:42 was 86-296 W; the arrays' balance
    # against their rated shares was 0.06-0.35 whenever the flybridge was
    # obstructed and 0.68-0.75 when it was not; three probes ended on the
    # load rising past the 60 s average.
    "CAP_TRUMPS_MDL_MS": 900000, "SHADE_BALANCE_MIN": 0.4, "LOAD_SLOW_MS": 300000,
    # one-way charge: how much deficit solar may run before shore is
    # reconnected. 4.3 allowed -200 W for ten minutes because every
    # reconnect then cost a 0.6-2 kW Quattro re-absorb (2026-09-02); the
    # sustain charge-current cap (dbus-recbms 1.5+) has removed that cost,
    # and on 2026-09-10 the tolerance let the bank drain for two hours of
    # thin sun while it was supposed to be CHARGING. 4.8: a three-minute
    # mean below -50 W reconnects; on shore the floor holds the bank flat
    # and every watt of sun still goes in through the solar band.
    "ONEWAY_DEFICIT_W": 50, "ONEWAY_DEFICIT_MS": 180000,
    # 4.5: a target at or above this is a request for a FULL charge from
    # every charger at its maximum, so one-way charge never engages there
    # (2026-09-06: at 100 % the floor held the Quattro at the present SOC
    # and only solar could move the bank -- it could never get full). 0 = off.
    "ONEWAY_FULL_PCT": 100,
    # 4.6: the SOC floor for LEAVING shore and staying on solar while
    # charging one-way. MIN_SOC (40) and SOC_EMERGENCY (30) protect a bank
    # that is being inverted into; charging one-way on solar the bank is
    # by definition being charged, the deficit exit is patient but bounded,
    # and 30 % of 1440 Ah is 430 Ah of reserve -- yet at 30 % on 2026-09-09
    # the engine sat on shore in full sun because of the 40 % gate. Below
    # this the emergency lockout fires as before, sun or not; above it the
    # lockout is skipped only while the bank's ten-minute mean is positive
    # (the sun IS carrying it).
    "ONEWAY_MIN_SOC": 25,
    # 4.7: a measurement boost needs this much PV actually flowing. On the
    # evening of 2026-09-09 eight boosts fired at fifteen-minute intervals
    # on a marina-light open-circuit voltage with zero yield; each lifts
    # dbus-recbms' sustain charge-current cap for two minutes for nothing.
    "BOOST_MIN_PV_W": 20,
    # 4.9: on the sustain floor the MPPTs sit one solar band above the
    # bank and run unthrottled, so the capture IS the capacity and a probe
    # proves nothing (2026-09-10: one probe, est 572 W vs need 529 W, both
    # arrays in tracker mode, accepted on -133 W). With both arrays
    # unthrottled and fresh captures, one-way charge goes straight to
    # solar like one-way discharge does; the three-minute deficit exit
    # stands guard. 0 keeps the probe.
    "ONEWAY_SKIP_PROBE": 1,
}


def time_mean(samples, now, window_ms, max_hold_ms):
    """Integrate left-held observations, excluding stale intervals."""
    area = duration = 0.0
    start = now - window_ms
    for idx, (stamp, value) in enumerate(samples):
        end = samples[idx + 1][0] if idx + 1 < len(samples) else now
        dt = max(0.0, min(end, now, stamp + max_hold_ms) - max(stamp, start))
        area += value * dt
        duration += dt
    return (area / duration if duration else samples[-1][1], duration)


def rolling_mean(samples, now, observation, window_ms, warmup_ms, max_hold_ms):
    if observation is None:
        samples.clear()
        return None
    if samples and now == samples[-1][0]:
        samples[-1] = (now, observation.v)
    else:
        samples.append((now, observation.v))
    # Keep the observation that spans the window's left boundary.
    while len(samples) > 1 and samples[1][0] <= now - window_ms:
        del samples[0]
    # Production runs at 1 Hz. Bound memory if an erroneous caller spins.
    if len(samples) > 4096:
        del samples[:len(samples) - 4096]
    mean, duration = time_mean(samples, now, window_ms, max_hold_ms)
    return mean if duration >= warmup_ms else None


class Val:
    """A value and its last successful observation time, in monotonic milliseconds."""
    __slots__ = ("v", "ts")

    def __init__(self, v, ts):
        self.v = v
        self.ts = ts


class Inputs:
    """What the engine sees each tick. All Val or None (missing)."""
    FIELDS = ("soc", "batt", "load_now", "load_avg", "load_slow", "feed", "ac_out",
              "voc6", "voc7", "y6", "y7", "m6", "m7", "batt_v", "cvl",
              "boost_active", "boost_window", "boost_eff", "lead",
              "target_soc", "sustain_active", "dc_load", "quattro_w")

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, None)
        self.enabled = False
        self.departure_allowed = True
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


def select_objective(previous, target, soc, enter_pct, exit_pct):
    """CHARGE, DISCHARGE or neither, from the destination and the SOC alone.

    One rule for a fresh start, a target change and every tick since: engage
    once the target is more than enter_pct from the SOC, stand down within
    exit_pct of it, and never flip direction without passing through the
    band between (a retarget across the SOC does, deliberately, in one
    tick). enter_pct 0 turns one-way operation off.
    """
    delta = target - soc
    if previous == "charge" and delta <= exit_pct:
        previous = None
    elif previous == "discharge" and delta >= -exit_pct:
        previous = None
    if enter_pct > 0:
        if delta > enter_pct:
            return "charge"
        if delta < -enter_pct:
            return "discharge"
    return previous


class Engine:
    def __init__(self, tunables, now_ms, logger=None):
        self.t = dict(ENGINE_DEFAULTS)
        self.t.update(tunables)
        self.st = fresh_state(now_ms, self.t)
        self.log = logger or (lambda msg: None)
        self._now = now_ms

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
        if not math.isfinite(now) or now < self._now:
            raise ValueError("Engine time must be finite and monotonic")
        self._now = now
        t = self.t
        st = self.st
        out = Outputs()
        # Observation freshness applies to constant values too. Never refresh
        # a timestamp merely because the consumer ticks.
        fresh = Inputs()
        for name in Inputs.FIELDS:
            item = getattr(inp, name, None)
            if (item is not None and isinstance(item.v, (int, float))
                    and math.isfinite(item.v) and math.isfinite(item.ts)
                    and 0 <= now - item.ts < t["HB_STALE_MS"]):
                setattr(fresh, name, item)
        for name in ("enabled", "lead_fault", "p_rated", "feed_shore", "departure_allowed"):
            setattr(fresh, name, getattr(inp, name))
        inp = fresh

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

        # Elapsed-time means: bursts of callbacks must not outweigh slow
        # observations. Four/59 seconds preserve the old 5/60 1-Hz warmup.
        battMean = rolling_mean(st["battWin"], now, batt,
                                t["DEFICIT_AVG_MS"], 4000, t["HB_STALE_MS"])
        battMeanLong = rolling_mean(st["battWinLong"], now, batt,
                                    t["ONEWAY_DEFICIT_MS"], 59000,
                                    t["HB_STALE_MS"])

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
            if st["state"] in ("shore", "suspend") and not inp.departure_allowed:
                status[:] = ["yellow", "SHORE | waiting for transfer readiness"]
                return
            if owc:
                # A burn-down spends the band into the loads: a step backward
                # while the bank is meant to be charging. The shore-side
                # entries already exclude it; this is the boundary (issue #2).
                self.log("ERROR burn-down refused while charging one-way (%s)" % reason)
                return
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
            if st["state"] in ("shore", "suspend") and not inp.departure_allowed:
                status[:] = ["yellow", "SHORE | waiting for transfer readiness"]
                return
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
            if st["state"] in ("shore", "suspend") and not inp.departure_allowed:
                status[:] = ["yellow", "SHORE | waiting for transfer readiness"]
                return
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
        if inp.quattro_w is None:
            missing.append("QuattroDC")
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
        # The objective is judged on the target and the SOC alone. A missing
        # transport input (Quattro DC, load, ActiveIn, a heartbeat) sends the
        # state machine to shore below; it is not arrival, not a disable and
        # not a new target, so the objective and its hold stand until fresh
        # data can judge them again. Clearing it here handed the shore
        # charger the full slider under a fresh HOLD/release lease (E02:
        # 56.42 V / 5 A became 59.34 V / 200 A). With no SOC at all the
        # last objective stands as well.
        if not enabled or tgt is None or full:
            oneway = None
        elif soc is not None:
            oneway = select_objective(oneway, tgt.v, soc.v,
                                      t["ONEWAY_ENTER_PCT"], t["ONEWAY_EXIT_PCT"])
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
        # 4.6: the SOC gate for leaving / staying off shore
        minSoc = t["ONEWAY_MIN_SOC"] if owc else t["MIN_SOC"]
        battMeanAny = battMeanLong if battMeanLong is not None else (
            battMean if battMean is not None else (batt.v if batt is not None else 0.0))
        sunCarrying = owc and soc is not None and soc.v >= minSoc and battMeanAny > 0

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

        elif soc.v < t["SOC_EMERGENCY"] and st["state"] != "shore" and not sunCarrying:
            lockout()
            toShore("EMERGENCY SOC %.1f%%" % soc.v)
            status[0] = "red"

        else:
            discharge = -batt.v
            sinceTrans = now - st["lastTransition"]
            loadSlow = inp.load_slow
            loadJudge = max(loadAvg.v, loadSlow.v) if loadSlow is not None else loadAvg.v
            needW = max(t["MIN_EST_W"], loadJudge * t["SOLAR_MARGIN"])
            # Signed measured Quattro DC voltage * current is authoritative.
            # Missing metering is never reconstructed from mixed-age totals.
            quattroW = inp.quattro_w.v

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
                surplus = (aboveCvl and soc.v >= minSoc and not shoreMissing
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
                    ready = (not shoreMissing and soc.v >= minSoc
                             and quattroW <= t["SURPLUS_QUIET_W"])
                else:
                    # Charging one-way: aboveCvl is judged against the
                    # sustain CVL (pinned at the SOC), which a freshly
                    # solar-charged bank sits above; the probe runs against
                    # the real target once sustain is released, so it is no
                    # reason to wait.
                    ready = (not shoreMissing and (not aboveCvl or owc)
                             and soc.v >= minSoc and not shaded
                             and quattroW <= t["SURPLUS_QUIET_W"] and (est >= needW or explore))
                if ready:
                    if not st["readySince"]:
                        st["readySince"] = now
                else:
                    st["readySince"] = 0

                harvestArmed = ((st["refillReady"] is True and not st["refillShoreFed"]) or
                                (st["refillReady"] is None and evidence >= t["HARVEST_MIN_EVID"]))
                harvest = (harvestArmed and leadOn and dayOk and not shoreMissing and not aboveCvl
                           and soc.v >= minSoc and batt.v <= t["SURPLUS_QUIET_W"]
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
                # 4.9: no boost while the arrays already run unthrottled --
                # no producing array at its ceiling (mode 1) and at least
                # one in tracker mode (2) with a fresh capture. The live
                # capture is the measurement, and every boost lifts the
                # floor's charge-current cap for two minutes.
                def cap_fresh(cap):
                    return cap is not None and (now - cap["ts"]) <= t["CAP_FRESH_MS"]
                throttled_any = any(m is not None and m.v == 1 for m in (m6, m7))
                live_any = any(m is not None and m.v == 2 and cap_fresh(cp)
                               for m, cp in ((m6, st["cap6"]), (m7, st["cap7"])))
                unthrottled = live_any and not throttled_any
                if (not boosting and dayOk and vocMax >= t["VOC_DAY_V"] and not vocRising
                        and pvNow >= t["BOOST_MIN_PV_W"] and not unthrottled
                        and not aboveCvl and not shoreMissing and soc.v >= minSoc
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
                elif (owc and t["ONEWAY_SKIP_PROBE"] and unthrottled
                      and est >= needW and ready and gateOk and (now - st["readySince"]) >= t["READY_MS"]):
                    # 4.9: the arrays run unthrottled on the floor's band and
                    # the captures are fresh -- there is nothing a probe
                    # could measure that the capture has not (the balance
                    # gate in `ready` has already passed)
                    enter_solar("one-way charge on a live capture: %.0f+%.0fW vs need %.0fW, bal %s" % (
                        faded(st["cap6"]), faded(st["cap7"]), needW,
                        "%.2f" % balance if balance is not None else "-"))
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
                        st["evalPv"].append((now, pvNow))
                        st["evalBatt"].append((now, batt.v))
                    status[0] = "yellow"
                    status[1] = "PROBE %ds | PV %.0fW batt %.0fW load %.0fW" % (
                        math.ceil((t["RAMP_MS"] - elapsed) / 1000), pvNow, batt.v, loadNow.v)
                else:
                    pvAvg = time_mean(st["evalPv"], now, t["EVAL_MS"], t["HB_STALE_MS"])[0] if st["evalPv"] else pvNow
                    battAvg = time_mean(st["evalBatt"], now, t["EVAL_MS"], t["HB_STALE_MS"])[0] if st["evalBatt"] else batt.v
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
                    if now - st["resumeStart"] >= t["RESUME_MS"] and inp.departure_allowed:
                        st["resumeStart"] = 0
                        st["desired"] = 1
                        st["lastTransition"] = now
                        st["surgeStart"] = 0
                        st["loadExceedStart"] = 0
                        if (st["suspendPrev"] == "burndown" and not owc and battV is not None
                                and cvl is not None and battV.v > cvl.v - t["BURN_EXIT_DROP_V"]):
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
                            loadNow.v, max(0, math.ceil((t["RESUME_MS"] - (now - st["resumeStart"])) / 1000)))
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
                elif soc.v < minSoc:
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
                      and dayOk and soc.v >= minSoc and not owc
                      and battV is not None and cvl is not None
                      and battV.v >= cvl.v - t["STALL_BURN_MIN_V"]):
                    # Ceiling stall (v4.1): burn the band, no backoff. Never
                    # while charging one-way (issue #2, E07: CHARGE at 78 %
                    # for 80 % burned the band at -100 W); that deficit takes
                    # the ordinary return below and the floor on shore.
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
            status[1] = "%s %s->%.0f%% | %s" % (
                "1-WAY CHARGE" if owc else "1-WAY DISCHARGE",
                "%.0f" % soc.v if soc is not None else "?", tgt.v, status[1])

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
        # With a required input missing the engine is heading for shore on
        # data it cannot judge: the floor is then wanted regardless of what
        # the Quattro reports (or fails to report), so that it is in force
        # before the charger is, not one tick after it (issue #1).
        onShore = feed is not None and feed.v == FEED_SHORE
        if owc:
            want = (SUSTAIN_FLOOR if st["state"] in ("shore", "suspend") and (onShore or missing)
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
        out.transfer_intent = "island" if st["desired"] else "shore"
        out.charge_intent = {0: "release", 1: "floor", 2: "ceiling"}[want]
        # A boost is a bounded edge request, never a continuously renewed
        # desired level. Publisher owns its expiry and reports active state.
        out.boost_v = out.boost
        out.reason = out.transition or out.status_text
        return out


