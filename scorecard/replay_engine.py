#!/usr/bin/env python3
"""Replay recorded days through the REAL Solar Priority engine (HOLD rules).

    python3 scorecard/replay_engine.py <grid5p.json> [curve_error_v [start_soc target_soc]]

With a start under the target it replays a CHARGE (engine 4.5 runs the same
rules at any distance under the target) and reports when the target is reached.

grid5p.json comes from pull_5min.py -> grid.py -> potential.py (Home Assistant
five-minute statistics with the PV potential filled in). A small plant model
answers the engine's commands: the relay, the prefer-renewable toggle, the
dbus-recbms floor and boost, a voltage ceiling one SOC point over the target
with the bank's measured resistance, and an inverter whose losses are NOT the
ones the engine assumes (so the prediction check has something to find).
"""
import json, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_stubs import *   # noqa: F401,F403
dm = types.ModuleType("dbusmonitor"); dm.DbusMonitor = object; sys.modules["dbusmonitor"] = dm
SP = load(os.path.join(REPO, "dbus-recbms", "solar_priority.py"), "solar_priority")
Val = SP.Val

WH_PER_PCT = 1440 * 55.7 / 100.0
V_PER_PCT, R_OHM = 0.078, 0.0017
IDLE_TRUE, EFF_TRUE = 105.0, 0.94          # the plant's inverter (the engine assumes 120 W / 0.95)
CHG_EFF = 0.92
STEP_S = 2
T = 50.0
GAIN_PCT = 1.0


def replay(rows, days, err_v=0.0, tun=None, start=T, target=T):
    t = dict(SP.ENGINE_DEFAULTS); t.update(tun or {})
    now = 1_800_000_000_000
    logs = []
    eng = SP.Engine(t, now, logger=logs.append)
    inp = SP.Inputs(); inp.enabled = True; inp.feed_shore = 1
    T = target
    soc, cmd, pre, floor, boost_until = start, 0, 1, None, 0
    reached = start >= target - 0.5
    lwin = []
    res = {}
    for r in rows:
        if r['day'] not in days:
            continue
        d = res.setdefault(r['day'], dict(pv=0.0, pot=0.0, shore=0.0, dep=0, probes=0, isl_s=0, lo=soc, hi=soc, lim_s=0))
        L = max(0.0, (r['load'] or 0.0) - 100.0 * r['isl'])
        D = r['dcl'] if r['dcl'] is not None and r['dcl'] < 400 else 57.0
        P = r['P']; voc = max(r['v7'] or 0, r['v6'] or 0)
        for _ in range(300 // STEP_S):
            now += STEP_S * 1000
            island = cmd == 1
            at = floor if floor is not None else T
            boost_v = 0.30 if now < boost_until else 0.0
            head_v = (at + GAIN_PCT - soc) * V_PER_PCT + err_v + boost_v
            accept = max(0.0, head_v / R_OHM) * 55.7
            inv = IDLE_TRUE + L / EFF_TRUE
            q_dc = 0.0; shore = 0.0
            if island:
                used = min(P, D + inv + accept)
                q_dc = -inv
                bank = used - D - inv
            else:
                shore = L
                used = min(P, D + accept)
                bank = used - D
                hold_at = at - 0.25 + err_v / V_PER_PCT        # the Quattro regulates ~0.02 V under its CVL
                if pre == 0 and soc < hold_at:
                    q_dc = min(2000.0, (hold_at - soc) * WH_PER_PCT * 3600 / STEP_S) + max(0.0, D - used)
                    bank += q_dc; shore += q_dc / CHG_EFF
            limited = P > used + 5
            soc += bank * STEP_S / 3600.0 / WH_PER_PCT
            d['pv'] += used * STEP_S / 3600; d['pot'] += P * STEP_S / 3600; d['shore'] += shore * STEP_S / 3600
            d['isl_s'] += STEP_S if island else 0
            d['lim_s'] += STEP_S if (limited and not island) else 0
            d['lo'] = min(d['lo'], soc); d['hi'] = max(d['hi'], soc)
            # ---- what the engine sees
            n = now
            load_seen = inv if island else L
            inp.soc = Val(round(soc / 0.05) * 0.05, n)
            inp.batt = Val(round(bank / 10) * 10, n)
            lwin.append(load_seen); del lwin[:-(60 // STEP_S)]      # the driver's 60 s mean
            inp.load_now = Val(load_seen, n); inp.load_avg = Val(sum(lwin) / len(lwin), n); inp.ac_out = Val(load_seen, n)
            inp.feed = Val(240 if island else 1, n)
            mode = 0 if P < 5 else (1 if limited else 2)
            inp.voc6, inp.y6, inp.m6 = Val(voc, n), Val(used * 0.35, n), Val(mode, n)
            inp.voc7, inp.y7, inp.m7 = Val(voc, n), Val(used * 0.65, n), Val(mode, n)
            inp.batt_v = Val(55.61 + (soc - 50) * V_PER_PCT + bank / 55.7 * R_OHM, n)
            inp.cvl = Val(55.61 + (at - 50 + GAIN_PCT) * V_PER_PCT, n)
            inp.target_soc = Val(T, n); inp.q_dc = Val(q_dc, n); inp.dc_sys = Val(D, n)
            inp.pre = Val(pre, n); inp.bank_ah = Val(1440, n)
            inp.boost_active = Val(1 if now < boost_until else 0, n)
            out = eng.tick(n, inp)
            if out.cmd is not None:
                cmd = out.cmd
            if not reached and soc >= target - 0.5:
                reached = True
                logs.append('REACHED %.0f %% on %s at %04.1f h' % (target, r['day'], r['slot'] / 12.0))
            if out.transition:
                logs.append('%s +%05.2fh soc %.2f  %s' % (r['day'], r['slot'] / 12.0, soc, out.transition))
                d['dep'] += out.transition.startswith('-> SOLAR (hold')
                d['susp'] = d.get('susp', 0) + out.transition.startswith('-> SUSPEND')
            if out.sustain is not None:
                floor = min(soc, T) if out.sustain == 1 else None
            if out.boost is not None:
                boost_until = now + 120000 if out.boost > 0 else 0
            if out.prefer is not None:
                pre = out.prefer
            d['probes'] = d.get('probes', 0)
    return res, logs


if __name__ == '__main__':
    rows = json.load(open(sys.argv[1]))
    errs = [float(sys.argv[2])] if len(sys.argv) > 2 else [-0.03, 0.0, 0.02]
    start, target = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) > 4 else (T, T)
    DAYS = ['2026-09-%02d' % n for n in range(9, 17)]
    for err in errs:
        res, logs = replay(rows, DAYS, err, None, start, target)
        print('\n== real engine %s, curve error %+.2f V' % (SP.ENGINE_VERSION, err))
        print('day          PVused  potential   shore  dep probes islandH  limited-on-shore h  SOC lo..hi  variation')
        for day, d in sorted(res.items()):
            print('%s %7.2f %9.2f %8.2f %4d %5d %8.1f %12.1f        %5.2f..%5.2f %7.2f' % (
                day, d['pv'] / 1000, d['pot'] / 1000, d['shore'] / 1000, d['dep'], d['probes'], d['isl_s'] / 3600,
                d['lim_s'] / 3600, d['lo'], d['hi'], d['hi'] - d['lo']))
        n = len(res); s = lambda k: sum(x.get(k, 0) for x in res.values())
        for l in logs:
            if l.startswith('REACHED'):
                print(l)
        print('hold probes: %d, suspends: %d' % (sum(l.startswith('hold probe:') for l in logs), s('susp')))
        print('mean: PV %.2f kWh/d (%.0f%% of potential), shore %.2f, departures %.1f/d, probes %.1f/d, daily SOC variation %.2f %%' % (
            s('pv') / n / 1000, 100 * s('pv') / s('pot'), s('shore') / n / 1000, s('dep') / n, s('probes') / n,
            sum(x['hi'] - x['lo'] for x in res.values()) / n))
        checks = [l for l in logs if l.startswith('prediction check')]
        e = sorted(float(l.split('error ')[1].split('W')[0]) for l in checks)
        if e:
            print('prediction checks: %d, error median %+.0f W, range %+.0f..%+.0f' % (len(e), e[len(e) // 2], e[0], e[-1]))
        back = [l for l in logs if '(deficit:' in l]
        print('returns on the deficit budget: %d; edges logged: %d dawn, %d dusk' % (
            len(back), sum(l.startswith('DAWN') for l in logs), sum(l.startswith('DUSK') for l in logs)))
