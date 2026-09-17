"""Build a 5-minute grid (local PDT) from stats5.json + raw_modes.json."""
import json, datetime as dt, bisect
TZ = dt.timezone(dt.timedelta(hours=-7))
S = json.load(open('stats5.json'))
R = json.load(open('raw_modes.json'))

def series(name, key='mean'):
    return {r['start'] // 1000: r.get(key) for r in S[name]}

def steps(name):
    rows = sorted((dt.datetime.fromisoformat(s['last_changed']).timestamp(), s['state']) for s in R[name])
    return [r[0] for r in rows], [r[1] for r in rows]

def frac(ts, st, t0, t1, want):
    """fraction of [t0,t1) spent in a state in `want`"""
    i = bisect.bisect_right(ts, t0) - 1
    tot = 0.0
    cur = t0
    while cur < t1:
        state = st[i] if i >= 0 else None
        nxt = ts[i + 1] if i + 1 < len(ts) else 1e18
        end = min(nxt, t1)
        if state in want:
            tot += end - cur
        cur = end
        i += 1
    return tot / (t1 - t0)

def build():
    pv7, pv6 = series('flybridge_solar_pv_yield_power'), series('brow_solar_pv_yield_power')
    v7, v6 = series('flybridge_solar_pv_bus_voltage', 'max'), series('brow_solar_pv_bus_voltage', 'max')
    load, dcl = series('gx_device_consumption_power_l1'), series('gx_device_dc_consumption')
    bp, soc = series('gx_device_dc_battery_power'), series('rec_bms_main_bank_charge')
    bv, bi = series('rec_bms_main_bank_dc_bus_voltage'), series('rec_bms_main_bank_dc_bus_current')
    cvl = series('rec_bms_main_bank_maximum_allowed_charging_voltage')
    qdc, qin = series('quattro_dc_power'), series('quattro_input_power_l1')
    m7, m6 = steps('flybridge_solar_mppt_operation_mode'), steps('brow_solar_mppt_operation_mode')
    ai = steps('gx_device_ac_active_input_source')
    rows = []
    for t in sorted(load):
        rows.append(dict(t=t, pv7=pv7.get(t), pv6=pv6.get(t), v7=v7.get(t), v6=v6.get(t),
                         load=load.get(t), dcl=dcl.get(t), bp=bp.get(t), soc=soc.get(t), bv=bv.get(t),
                         bi=bi.get(t), cvl=cvl.get(t), qdc=qdc.get(t), qin=qin.get(t),
                         a7=frac(*m7, t, t + 300, ('mppt_active',)), a6=frac(*m6, t, t + 300, ('mppt_active',)),
                         off7=frac(*m7, t, t + 300, ('off',)), off6=frac(*m6, t, t + 300, ('off',)),
                         isl=frac(*ai, t, t + 300, ('not_connected',))))
    return rows

if __name__ == '__main__':
    rows = build()
    json.dump(rows, open('grid5.json', 'w'))
    print(len(rows), dt.datetime.fromtimestamp(rows[0]['t'], TZ), dt.datetime.fromtimestamp(rows[-1]['t'], TZ))
    states = set(R['flybridge_solar_mppt_operation_mode'][i]['state'] for i in range(len(R['flybridge_solar_mppt_operation_mode'])))
    print(states, set(s['state'] for s in R['gx_device_ac_active_input_source']))
    days = {}
    for r in rows:
        d = dt.datetime.fromtimestamp(r['t'], TZ).date()
        days.setdefault(d, []).append(r)
    print('day        pvkWh loadkWh dclkWh  daylightSlots unthr7 unthr6 islandH  soc0->soc1')
    for d, rs in sorted(days.items()):
        pv = sum(((r['pv7'] or 0) + (r['pv6'] or 0)) for r in rs) / 12000
        ld = sum((r['load'] or 0) for r in rs) / 12000
        dc = sum((r['dcl'] or 0) for r in rs) / 12000
        dayl = [r for r in rs if (r['v7'] or 0) > 60]
        u7 = sum(r['a7'] for r in dayl) / max(1, len(dayl))
        u6 = sum(r['a6'] for r in dayl) / max(1, len(dayl))
        isl = sum(r['isl'] for r in rs) / 12
        print(d, '%5.2f %6.2f %6.2f %8d %10.2f %6.2f %6.1f   %s -> %s' % (pv, ld, dc, len(dayl), u7, u6, isl, rs[0]['soc'], rs[-1]['soc']))
