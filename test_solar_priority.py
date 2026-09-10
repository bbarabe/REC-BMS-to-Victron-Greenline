#!/usr/bin/env python3
"""
test_solar_priority.py — one-way charge / discharge, off the boat.

    python test_solar_priority.py

Two halves, both against the stand-ins in test_stubs.py:

  dbus-recbms   the Sustain control: /RecBms/Sustain/Request anchors the hold
                to the bank's own voltage (never the curve), gives the MPPTs a
                band above it, steps one way only (a full SOC step, or the
                band absorbed on sun), servos on the coulomb count, stays
                inside the real slider, expires by itself, refuses boosts
                under a ceiling, makes an equalization wait, and holds a
                stale-BMS request pending.

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
check("config: servo and taper tunables", rcfg.sustain_servo_v == 0.02 and rcfg.sustain_servo_s == 30
      and rcfg.sustain_servo_db == 0.1 and rcfg.sustain_servo_up == 0.5 and rcfg.sustain_servo_down == 2.0
      and rcfg.sustain_taper_a == 3 and rcfg.sustain_taper_s == 60 and rcfg.sustain_q_idle_a == 1
      and rcfg.sustain_pv_min_a == 0.5 and rcfg.sustain_band_v == 0.30 and rcfg.sustain_anchor_r == 0.003)

T = [1_800_000_000.0]
R.time = types.SimpleNamespace(time=lambda: T[0])
drv = R.RecBmsDriver(rcfg)
batt = drv.batt
SOC, V, I = [62.0], [56.6], [0.0]
# Solar Priority on, and a systemcalc that honours the offset: the lead is
# in force, so the Quattro is commanded target - lead exactly as on the boat
drv.sp_enabled = True
drv._boost_write = lambda volts, quiet=False: True
LEAD = rcfg.solar_lead
BAND = rcfg.sustain_band_v


def ir(amps):
    """the anchor's correction for the drop across the pack"""
    return -amps * rcfg.sustain_anchor_r


def live():
    drv.bms.update({
        "_lastUpdate": T[0], "socHiRes": SOC[0], "soc": int(SOC[0]),
        "voltage": V[0], "current": I[0], "temperature": 21.0,
        "cvl": 62.7, "ccl": 200.0, "dcl": 400.0, "dvl": 48.0,
        "minCellV": 3.70, "maxCellV": 3.75, "minCellT": 20.0, "maxCellT": 22.0,
    })


def rtick(n=1, soc=None, slider=None, dt=1.0, v=None, i=None, pv=None, keep=True):
    """One or more driver ticks. keep=True re-asserts an active hold every
    tick, the way Solar Priority does every 30 s, so a scenario longer than
    hold_s does not silently expire it; the expiry tests pass keep=False."""
    if soc is not None:
        SOC[0] = soc
    if v is not None:
        V[0] = v
    if i is not None:
        I[0] = i
    if slider is not None:
        FakeBus.store["/Settings/RecBms/ChargeSlider"] = slider
    for _ in range(n):
        T[0] += dt
        if pv is not None:
            drv.pv_current = (pv, T[0])
        live()
        if keep and drv.sustain["active"]:
            batt.write("/RecBms/Sustain/Request", drv.sustain["mode"])
        if drv._last_pub_cvl is not None:
            drv.eff_cv = (drv._last_pub_cvl + drv._last_offset, T[0])
        drv._tick()


def curve(pct):
    return round(drv._slider_cvl(pct), 2)


def r2(x):
    return round(x, 2)


mppt = lambda: batt["/RecBms/TargetChargeVoltage"]      # the MPPT ceiling
quattro = lambda: batt["/Info/MaxChargeVoltage"]        # what the Quattro is commanded
hold = lambda: batt["/RecBms/Sustain/HoldVoltage"]

rtick(n=12, soc=62, slider=80, pv=0.0)
check("slider published as /RecBms/TargetSoc", batt["/RecBms/TargetSoc"] == 80)
check("no hold: CVL from the slider, Quattro a lead under", mppt() == curve(80) and
      quattro() == r2(curve(80) - LEAD), "%s %s" % (mppt(), quattro()))
check("sustain telemetry idle", batt["/RecBms/Sustain/Active"] == 0 and
      batt["/RecBms/Sustain/Status"] == "idle" and hold() is None)

# ---- floor: anchored to the pack voltage, one band of solar headroom ----
check("floor request accepted", batt.write("/RecBms/Sustain/Request", 1))
rtick()
check("floor: Quattro commanded the pack voltage itself", quattro() == 56.6, str(quattro()))
check("floor: MPPTs one band above it", mppt() == r2(56.6 + BAND), str(mppt()))
check("floor telemetry", batt["/RecBms/Sustain/Active"] == 1 and
      batt["/RecBms/Sustain/Mode"] == 1 and batt["/RecBms/Sustain/Soc"] == 62.0 and
      hold() == 56.6 and batt["/RecBms/Sustain/Servo"] == 0.0 and
      batt["/RecBms/Sustain/Status"] == "floor")
rtick(soc=62.9, v=56.7, pv=5.0)
check("floor keeps every bit the sun adds, re-anchors only on a full step",
      hold() == 56.6 and batt["/RecBms/Sustain/Soc"] == 62.9)
rtick(soc=62.95, v=56.7, i=0.5, pv=0.0)
check("...but not a rise made without the sun (Quattro trickle at night)",
      batt["/RecBms/Sustain/Soc"] == 62.9, str(batt["/RecBms/Sustain/Soc"]))
rtick(soc=63.0, v=56.72, i=10.0, pv=12.0)             # solar did it: re-anchor
A1 = r2(56.72 + ir(10.0))
check("floor follows the bank up a full step and re-anchors to the pack voltage less the IR drop",
      batt["/RecBms/Sustain/Soc"] == 63.0 and hold() == A1 and quattro() == A1 and
      mppt() == r2(A1 + BAND), "%s %s %s" % (batt["/RecBms/Sustain/Soc"], hold(), mppt()))
rtick(soc=60, v=56.5, i=-2.0, pv=0.0)
check("floor never follows it down", batt["/RecBms/Sustain/Soc"] == 63.0 and hold() == A1)
rtick(soc=64.0, v=56.9, i=4.0, pv=0.0)                # the Quattro did it (no sun): not counted
check("a rise the Quattro charged is neither held nor anchored",
      batt["/RecBms/Sustain/Soc"] == 63.0 and hold() == A1, "%s %s" % (batt["/RecBms/Sustain/Soc"], hold()))
rtick(slider=62)
check("floor never holds more than the slider", batt["/RecBms/Sustain/Soc"] == 62.0)
check("...and the band closes once the bank is at the slider (standing lead only)", mppt() == hold() and
      quattro() == r2(hold() - LEAD), "%s %s" % (mppt(), quattro()))
rtick(slider=80)
check("...and pops back when the slider rises", batt["/RecBms/Sustain/Soc"] == 63.0 and
      mppt() == r2(hold() + BAND))

# ---- the 2026-09-07 case: a floor far under the slider's minimum ----
batt.write("/RecBms/Sustain/Request", 0); rtick()
rtick(n=2, soc=23.3, slider=60, v=53.9, i=-30.0, pv=0.0)
check("below the curve: floor request accepted", batt.write("/RecBms/Sustain/Request", 1))
rtick()
A0 = r2(53.9 + ir(-30.0))                               # under a 30 A load: 0.09 V under rest
check("below the curve: anchored to the pack (IR-corrected), not clipped to the curve's 40 % edge",
      hold() == A0 and quattro() == A0 and mppt() == r2(A0 + BAND) and
      batt["/RecBms/Sustain/Soc"] == 23.3, "%s %s %s" % (hold(), quattro(), mppt()))
check("below the curve: nothing near curve(40) = %.2f" % curve(40), abs(quattro() - curve(40)) > 0.3)
# the bank settles under the anchor overnight (taken under load): the servo lifts the command
rtick(n=1, soc=23.15, i=-2.0, pv=0.0, dt=31)
check("servo: bank 0.15 % under the held SOC and draining -> command up one step",
      hold() == r2(A0 + 0.02) and batt["/RecBms/Sustain/Servo"] == 0.02, "%s" % hold())
rtick(n=5, dt=31)
check("servo: one step per period", batt["/RecBms/Sustain/Servo"] == 0.12, str(batt["/RecBms/Sustain/Servo"]))
rtick(n=2, dt=10)
check("servo: not inside a period", batt["/RecBms/Sustain/Servo"] == 0.12, str(batt["/RecBms/Sustain/Servo"]))
rtick(n=3, soc=23.15, i=0.0, dt=31)
check("servo: still under the held SOC but no longer draining (Quattro covering) -> holds",
      batt["/RecBms/Sustain/Servo"] == 0.12, str(batt["/RecBms/Sustain/Servo"]))
rtick(n=1, soc=23.3, i=0.5, dt=31)
check("servo: bank back at the held SOC -> holds there", batt["/RecBms/Sustain/Servo"] == 0.12)
rtick(n=1, soc=23.45, i=3.0, pv=0.0, dt=31)
check("servo: the Quattro charging it 0.15 % above -> command down one step",
      batt["/RecBms/Sustain/Servo"] == 0.10, str(batt["/RecBms/Sustain/Servo"]))
rtick(n=1, soc=23.45, i=3.0, pv=4.0, dt=31)
check("servo: same rise on sun (PV covers the current) is left alone, and kept",
      batt["/RecBms/Sustain/Servo"] == 0.10 and batt["/RecBms/Sustain/Soc"] == 23.4,
      "%s %s" % (batt["/RecBms/Sustain/Servo"], batt["/RecBms/Sustain/Soc"]))
rtick(n=1, soc=23.45, i=3.0, pv=None, dt=31)
drv.pv_current = None
rtick(n=1, dt=31)
check("servo: unknown PV current never lowers", batt["/RecBms/Sustain/Servo"] == 0.10)
rtick(n=80, soc=22.0, i=-5.0, pv=0.0, dt=31)
check("servo: bounded upward at servo_max_up_v", batt["/RecBms/Sustain/Servo"] == 0.5 and hold() == r2(A0 + 0.5))
check("servo: MPPT ceiling rides on it", mppt() == r2(hold() + BAND))
rtick(n=200, soc=24.0, i=5.0, pv=0.0, dt=31)
check("servo: bounded downward at the larger servo_max_down_v",
      batt["/RecBms/Sustain/Servo"] == -2.0 and hold() == r2(A0 - 2.0), str(batt["/RecBms/Sustain/Servo"]))

# ---- staircase: the band absorbed on sun steps the hold up ----
batt.write("/RecBms/Sustain/Request", 0); rtick()
rtick(n=2, soc=30.0, slider=60, v=54.40, i=0.0, pv=0.0)
batt.write("/RecBms/Sustain/Request", 1)
rtick()
check("staircase: anchored 54.40, band to %.2f" % r2(54.40 + BAND), hold() == 54.40 and mppt() == r2(54.40 + BAND))
top = mppt()
rtick(n=30, v=top - 0.01, i=20.0, pv=25.0)             # at the ceiling, still absorbing
check("staircase: absorbing at the ceiling is not yet a step", hold() == 54.40)
rtick(n=60, v=top - 0.01, i=0.0, pv=4.0)               # tapered: 59 s elapsed
check("staircase: tapered 59 s: not yet", hold() == 54.40)
rtick(n=1, v=top - 0.01, i=0.0, pv=4.0)                # 60 s
check("staircase: tapered 60 s on sun -> re-anchored at the ceiling",
      hold() == r2(top - 0.01) and mppt() == r2(top - 0.01 + BAND) and quattro() == hold(),
      "%s %s" % (hold(), mppt()))
top2 = mppt()
rtick(n=70, v=top2 - 0.01, i=2.0, pv=0.0)              # same picture at night: the Quattro's overshoot
check("staircase: never steps at night", hold() == r2(top - 0.01))
rtick(n=70, v=top2 - 0.01, i=2.0, pv=0.8)              # a dribble of PV, the Quattro doing the work
check("staircase: never steps while the Quattro is charging", hold() == r2(top - 0.01))
rtick(n=70, v=top2 - 0.10, i=2.0, pv=4.0)              # sun, but under the ceiling
check("staircase: never steps under the ceiling", hold() == r2(top - 0.01))
rtick(n=61, v=top2 - 0.01, i=0.0, pv=4.0)
check("staircase: second step", hold() == r2(top2 - 0.01), str(hold()))
rtick(n=1, soc=31.5, v=hold() + 0.12, i=10.0, pv=20.0)
check("staircase: a full SOC step also re-anchors (IR-corrected)", hold() == r2(top2 - 0.01 + 0.12 + ir(10.0)) and
      batt["/RecBms/Sustain/Soc"] == 31.5, str(hold()))

# EQ due while held: it must wait, not run
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = 0
rtick()
check("equalization waits under a hold", not drv.eq["active"] and
      batt["/RecBms/EqStatus"] == "" and mppt() == r2(hold() + BAND))

# re-assert refreshes the expiry and keeps the anchor
h = hold()
rtick(n=1, dt=100, keep=False)
check("re-assert accepted", batt.write("/RecBms/Sustain/Request", 1))
rtick(n=1, dt=100, keep=False)
check("re-asserted hold survives past the original expiry, anchor kept",
      batt["/RecBms/Sustain/Active"] == 1 and hold() == h)
rtick(n=1, dt=125, keep=False)
check("hold expires on its own", batt["/RecBms/Sustain/Active"] == 0 and
      batt["/RecBms/Sustain/Status"].startswith("expired") and
      batt["/RecBms/Sustain/Request"] == 0 and hold() is None)
check("slider back in force after expiry", mppt() == curve(60) + rcfg.eq_boost
      or mppt() == curve(60), str(mppt()))
check("equalization starts once released", drv.eq["active"])
drv.eq["active"] = False
FakeBus.store["/Settings/RecBms/EqLastCompleted"] = T[0]

# ---- ceiling: anchored, nothing charges above it, Quattro a lead under ----
rtick(n=2, soc=90, slider=70, v=60.3, i=0.0, pv=0.0)
check("ceiling request accepted", batt.write("/RecBms/Sustain/Request", 2))
rtick()
check("ceiling: MPPTs at the pack voltage, Quattro a lead under",
      mppt() == 60.3 and quattro() == r2(60.3 - LEAD) and hold() == 60.3 and
      batt["/RecBms/Sustain/Mode"] == 2 and batt["/RecBms/Sustain/Status"] == "ceiling",
      "%s %s" % (mppt(), quattro()))
rtick(soc=89.3, v=60.2, i=-10.0)
check("ceiling follows the drain, re-anchors only on a full step", hold() == 60.3 and batt["/RecBms/Sustain/Soc"] == 89.3)
rtick(soc=89.0, v=60.15, i=-10.0)
C1 = r2(60.15 + ir(-10.0))
check("ceiling follows the bank down a full step and re-anchors lower (IR-corrected)",
      hold() == C1 and mppt() == C1 and batt["/RecBms/Sustain/Soc"] == 89.0, str(hold()))
rtick(soc=91, v=60.5, i=6.0, pv=8.0)
check("ceiling never follows it up", hold() == C1 and batt["/RecBms/Sustain/Soc"] == 89.0)
rtick(n=1, soc=89.0, v=60.2, i=3.0, pv=0.0, dt=31)
check("ceiling: the Quattro holding it -> command down", batt["/RecBms/Sustain/Servo"] == -0.02 and
      hold() == r2(C1 - 0.02), str(hold()))
rtick(n=1, soc=88.5, v=60.1, i=-4.0, pv=0.0, dt=31)
check("ceiling: a drain is the plan, no servo up", batt["/RecBms/Sustain/Servo"] == -0.02)
rtick(soc=69, v=58.5, i=-4.0)
check("ceiling never goes under the real slider", mppt() >= curve(70) and batt["/RecBms/Sustain/Soc"] == 70.0,
      "%s vs %s" % (mppt(), curve(70)))
rtick(soc=60, v=57.0, i=0.0)
check("ceiling under the slider: MPPTs may charge back up to the slider's point",
      mppt() == curve(70) and hold() == 57.0, "%s vs %s" % (mppt(), curve(70)))
check("boost refused under a ceiling", not batt.write("/RecBms/SolarBoost/Request", 0.2) and
      batt["/RecBms/SolarBoost/Status"] == "refused: sustain ceiling active")

# switching mode re-anchors; release restores the slider
rtick(soc=75, v=58.9, i=0.0)
check("floor request while ceiling held", batt.write("/RecBms/Sustain/Request", 1))
rtick()
check("mode switch re-anchors", batt["/RecBms/Sustain/Mode"] == 1 and
      drv.sustain["soc"] == 75.0 and batt["/RecBms/Sustain/Soc"] == 70.0 and hold() == 58.9 and
      mppt() == 58.9 and quattro() == r2(58.9 - LEAD),
      "floor 75 over slider 70: no band, standing lead -> %s %s" % (mppt(), quattro()))
check("release accepted", batt.write("/RecBms/Sustain/Request", 0))
rtick()
check("released: slider CVL, telemetry cleared", mppt() == curve(70) and
      batt["/RecBms/Sustain/Active"] == 0 and batt["/RecBms/Sustain/Soc"] is None and hold() is None)

# charge current limit while held: PV current + charge_limit_a, MPPT-safe
rtick(n=2, soc=62, slider=80, v=56.6, i=0.0, pv=0.0)
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

# stale BMS: no present SOC to pin -> pending, anchored on the first live tick
drv.bms["_lastUpdate"] = T[0] - 100
check("pending while the BMS is stale", batt.write("/RecBms/Sustain/Request", 1) and
      batt["/RecBms/Sustain/Status"] == "pending: no SOC yet" and batt["/RecBms/Sustain/Active"] == 1)
T[0] += 1; drv._tick()                                  # still stale: slider applies, hold waits
check("pending: slider CVL meanwhile", mppt() == curve(80) and
      batt["/RecBms/Sustain/Soc"] is None and hold() is None,
      "cvl %s vs %s, soc %s, status %s" % (mppt(), curve(80),
                                          batt["/RecBms/Sustain/Soc"], batt["/RecBms/Sustain/Status"]))
# BMS back but only the voltage frame so far: the 50 % safe substitute must not be anchored
T[0] += 1
drv.bms.update({"_lastUpdate": T[0], "socHiRes": None, "soc": None, "voltage": 56.8, "current": 0.0})
drv._tick()
check("pending: a live tick without an SOC frame stays pending (never the 50 % stand-in)",
      batt["/RecBms/Sustain/Status"] == "pending: no SOC yet" and batt["/RecBms/Sustain/Soc"] is None,
      "%s %s" % (batt["/RecBms/Sustain/Status"], batt["/RecBms/Sustain/Soc"]))
rtick(soc=64, v=56.8)                                   # SOC frame in: anchored
check("pending hold starts on the first live tick with an SOC", batt["/RecBms/Sustain/Status"] == "floor" and
      batt["/RecBms/Sustain/Soc"] == 64.0 and hold() == 56.8 and mppt() == r2(56.8 + BAND))
batt.write("/RecBms/Sustain/Request", 0); rtick()
check("bad mode refused", not batt.write("/RecBms/Sustain/Request", 7))

# lead not in force (Solar Priority off): a floor has no band to give
drv.sp_enabled = False
rtick(n=2, soc=62, slider=80, v=56.6, i=0.0, pv=0.0)
batt.write("/RecBms/Sustain/Request", 1); rtick()
check("no lead: floor holds the Quattro AND the MPPTs at the pack voltage",
      mppt() == 56.6 and quattro() == 56.6, "%s %s" % (mppt(), quattro()))
batt.write("/RecBms/Sustain/Request", 0); rtick()
drv.sp_enabled = True

# pure pieces
check("hold: floor up on sun only, never at night or from the Quattro, never down; ceiling down always; None keeps",
      R.sustain_hold(1, 60, 60.9, False, True) == 60.9 and R.sustain_hold(1, 60, 60.9, True, True) == 60 and
      R.sustain_hold(1, 60, 60.9, False, False) == 60 and
      R.sustain_hold(1, 60, 50, False, True) == 60 and R.sustain_hold(2, 60, 59.2, False, False) == 59.2 and
      R.sustain_hold(2, 60, 59.2, True, True) == 59.2 and R.sustain_hold(2, 60, 70, False, True) == 60 and
      R.sustain_hold(1, 60, None, False, True) == 60)
check("servo: floor up only while draining under the line, down only when the Quattro charges above, else hold",
      R.sustain_servo(1, -0.2, False, 0.1, True) == 1 and R.sustain_servo(1, -0.2, False, 0.1, False) == 0 and
      R.sustain_servo(1, -0.2, True, 0.1, True) == 1 and
      R.sustain_servo(1, 0.2, True, 0.1) == -1 and R.sustain_servo(1, 0.2, False, 0.1) == 0 and
      R.sustain_servo(1, 0.05, True, 0.1) == 0 and R.sustain_servo(1, -0.05, False, 0.1, True) == 0)
check("servo: ceiling down whenever the Quattro charges, never up",
      R.sustain_servo(2, 0.0, True, 0.1) == -1 and R.sustain_servo(2, -3.0, True, 0.1) == -1 and
      R.sustain_servo(2, -3.0, False, 0.1) == 0 and R.sustain_servo(2, 3.0, False, 0.1) == 0)
check("shore_charging: battery current over PV current by more than idle",
      R.shore_charging(3.0, 0.0, 1.0) and not R.shore_charging(3.0, 4.0, 1.0) and
      not R.shore_charging(1.0, 0.0, 1.0) and not R.shore_charging(3.0, None, 1.0) and
      not R.shore_charging(None, 0.0, 1.0))

# ================================================================ engine 4.3
print("\n=== solar priority engine: one-way ===")
SP = load(os.path.join(REPO, "dbus-recbms", "solar_priority.py"), "solar_priority")
scfg = SP.Config(os.path.join(REPO, "dbus-recbms", "solar_priority.ini"))
check("config: one-way tunables", scfg.engine["ONEWAY_ENTER_PCT"] == 5 and
      scfg.engine["ONEWAY_EXIT_PCT"] == 1 and scfg.engine["ONEWAY_FULL_PCT"] == 100 and
      scfg.engine["ONEWAY_MIN_SOC"] == 25 and scfg.engine["ONEWAY_DEFICIT_W"] == 50 and
      scfg.engine["ONEWAY_DEFICIT_MS"] == 180000)
check("engine version bumped", SP.ENGINE_VERSION == "4.8")
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
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340)
check("patient: probe", s.state == "probe")
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
s.tick(1, soc=60, target=95, batt_v=56.4)
s.tick(340)
s.tick(100, batt=-120.0, cvl=59.49)              # the whole ramp drains: not a stint to start
check("4.8: probe verdict refuses a draining stint", s.state == "shore" and
      any("probe failed" in tr for tr in s.transitions), str(s.transitions[-1:]))
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

# ---- 4.6: one-way charge leaves shore under min_soc, above oneway_min_soc ----
s = Sim()
s.tick(1, soc=30.0, target=60, batt_v=54.4, cvl=54.69, batt=300.0)
check("4.6: 30 -> 60 engages one-way charge with the floor", s.oneway == "charge" and s.sustain == 1)
s.tick(340)
check("4.6: probe at 30 % (min_soc 40 no longer gates one-way charge)", s.state == "probe", s.state)
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
check("4.6: 27 % still probes (above oneway_min_soc)", s.state == "probe", s.state)
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

print("\n%d passed, %d failed" % (len(ok), len(fail)))
for f in fail:
    print("  FAILED: " + f)
sys.exit(1 if fail else 0)
