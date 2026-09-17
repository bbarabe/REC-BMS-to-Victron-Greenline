"""Back-test, revision 2: voltage ceiling with the bank's resistance and a curve error, probe when the arrays are limited."""
import json
WH_PER_PCT = 1440 * 55.7 / 100.0
V_PER_PCT = 0.078                  # curve slope at 50..58 %
R_OHM = 0.0017                     # measured: +0.03 V at 19 A
IDLE_W, EFF = 90.0, 0.95           # inverter model
CHG_EFF = 0.92

def run(rows, T=50.0, up=1.0, mid=0.25, ret=0.5, frac=0.75, err_v=0.0, probe_min=30, r2=True,
        rule_min=True, rule_max=False, band_full=False, cooldown_min=5, days=None):
    soc = T; E = T; island = False
    light = None; light_run = dark_run = 0
    since = 999; last_probe = -999; minute = 0
    res = {}
    for r in rows:
        if days and r['day'] not in days:
            continue
        d = res.setdefault(r['day'], dict(pv=0.0, pot=0.0, shore=0.0, dep=0, isl_min=0, lo=soc, hi=soc,
                                          night_chg=0.0, lost=0.0, probes=0, lim_min=0))
        L = max(0.0, (r['load'] or 0.0) - 100.0 * r['isl'])
        D = r['dcl'] if r['dcl'] is not None and r['dcl'] < 400 else 57.0
        P = r['P']
        vmax = max(r['v7'] or 0, r['v6'] or 0)
        for _ in range(5):
            minute += 1
            if vmax >= 60: light_run += 1; dark_run = 0
            elif vmax < 50: dark_run += 1; light_run = 0
            edge = None
            if light_run >= 10 and light is not True: light, edge = True, 'dawn'
            if dark_run >= 5 and light is not False: light, edge = False, 'dusk'
            if edge == 'dawn': E = max(soc, T) if rule_max else T
            if edge == 'dusk' and not island: E = min(soc, T) if rule_min else T
            since += 1
            need = D + IDLE_W + L / EFF
            # what the voltage ceiling lets into the bank: (ceiling - rest voltage) / R
            head_v = (E + up - soc) * V_PER_PCT + err_v
            accept = max(0.0, head_v / R_OHM) * 55.7
            if island:
                used = min(P, need + accept)
                bank = used - need; shore = 0.0
                if L >= 1000: island = False; since = 999
                elif soc <= E - ret and bank < -20:
                    island = False; since = 0
                    if light is False: E = min(soc, T) if rule_min else T
            else:
                if light:
                    used = min(P, D + accept)
                    bank = used - D; shore = L
                    limited = P > used + 5
                    if limited: d['lost'] += (P - used) / 60.0; d['lim_min'] += 1
                    seen = used
                    if limited and minute - last_probe >= probe_min and L < 1000:
                        last_probe = minute; seen = P; d['probes'] += 1      # momentary gain: the arrays show what they have
                    go = False
                    if L < 1000 and since >= cooldown_min:
                        if seen >= need: go = True
                        elif r2 and seen >= frac * need and soc >= E + mid: go = True
                        elif band_full and soc >= E + up: go = True
                    if go: island = True; since = 0; d['dep'] += 1
                else:
                    used = min(P, D)
                    hold = E + err_v / V_PER_PCT - 0.25            # the Quattro regulates ~0.02 V under its CVL
                    if soc < 25 or soc < hold:
                        chg = min(2000.0, (hold - soc) * WH_PER_PCT * 60)
                        bank = chg; shore = L + (chg + D - used) / CHG_EFF; d['night_chg'] += chg / 60.0
                    else:
                        bank = used - D; shore = L
            soc += bank / 60.0 / WH_PER_PCT
            d['pv'] += used / 60.0; d['pot'] += P / 60.0; d['shore'] += shore / 60.0
            d['isl_min'] += 1 if island else 0
            d['lo'] = min(d['lo'], soc); d['hi'] = max(d['hi'], soc)
    return res

def table(name, res, show=False):
    if show:
        print('\n== %s\nday          PVused  potential  shore   dep probes islandH  limited-on-shore h  SOC lo..hi' % name)
        for day, d in sorted(res.items()):
            print('%s %7.2f %9.2f %7.2f %5d %5d %7.1f %12.1f        %5.2f..%5.2f' % (day, d['pv']/1000, d['pot']/1000, d['shore']/1000,
                  d['dep'], d['probes'], d['isl_min']/60, d['lim_min']/60, d['lo'], d['hi']))
    n = len(res); s = lambda k: sum(d[k] for d in res.values())
    print('%-46s PV %.2f kWh/d (%2.0f%%)  shore %.2f  dep %.1f/d  probes %.1f/d  lost on shore %.2f  SOC %.2f..%.2f' % (
        name, s('pv')/n/1000, 100*s('pv')/s('pot'), s('shore')/n/1000, s('dep')/n, s('probes')/n, s('lost')/n/1000,
        min(d['lo'] for d in res.values()), max(d['hi'] for d in res.values())))

if __name__ == '__main__':
    rows = json.load(open('grid5p.json'))
    DAYS = ['2026-09-%02d' % n for n in range(9, 17)]
    for err in (-0.03, 0.0, 0.02):
        print('curve error %+.2f V' % err)
        table('  revised: +1 / -0.5, 75%% rule, probe 30 min', run(rows, days=DAYS, err_v=err))
        table('  same, probe every 15 min', run(rows, days=DAYS, err_v=err, probe_min=15))
        table('  same, no probe at all', run(rows, days=DAYS, err_v=err, probe_min=10**9))
        table('  same, without the 75%% rule', run(rows, days=DAYS, err_v=err, r2=False))
        table('  +0.5 / -0.25 (first draft), probe 30 min', run(rows, days=DAYS, err_v=err, up=0.5, ret=0.25))
    table('revised, by day', run(rows, days=DAYS), show=True)
    table('revised with max() at dawn', run(rows, days=DAYS, rule_max=True))
    table('revised without min() at dusk', run(rows, days=DAYS, rule_min=False))
