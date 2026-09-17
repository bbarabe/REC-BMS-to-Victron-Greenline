"""PV potential per 5-min slot: measured where the tracker ran free, else clear-sky envelope x the day's clearness."""
import json, datetime as dt, statistics as st
TZ = dt.timezone(dt.timedelta(hours=-7))

def build():
    rows = json.load(open('grid5.json'))
    for r in rows:
        d = dt.datetime.fromtimestamp(r['t'], TZ)
        r['day'] = d.date().isoformat(); r['slot'] = d.hour * 12 + d.minute // 5
    out = {}
    for arr, pk, ak in (('7', 'pv7', 'a7'), ('6', 'pv6', 'a6')):
        env = [0.0] * 288
        for r in rows:
            if r[ak] >= 0.9 and r[pk] is not None:
                env[r['slot']] = max(env[r['slot']], r[pk])
        env = [max(env[max(0, i - 1):i + 2]) for i in range(288)]          # widen by one slot
        env = [st.mean(env[max(0, i - 2):i + 3]) for i in range(288)]      # smooth
        days = {}
        for r in rows:
            days.setdefault(r['day'], []).append(r)
        for day, rs in days.items():
            known = [(r['slot'], min(1.0, r[pk] / env[r['slot']])) for r in rs
                     if r[ak] >= 0.9 and r[pk] is not None and env[r['slot']] > 30]
            for r in rs:
                p = r[pk] or 0.0
                if r[ak] >= 0.9 or env[r['slot']] <= 0:
                    r['pot' + arr], r['est' + arr] = p, 0
                    continue
                before = [k for k in known if k[0] < r['slot']]
                after = [k for k in known if k[0] > r['slot']]
                if before and after:
                    (s0, k0), (s1, k1) = before[-1], after[0]
                    k = k0 + (k1 - k0) * (r['slot'] - s0) / (s1 - s0)
                elif before or after:
                    k = (before[-1] if before else after[0])[1]
                else:
                    k = None
                r['pot' + arr] = max(p, env[r['slot']] * k) if k is not None else p
                r['est' + arr] = 1
    for r in rows:
        r['P'] = r['pot7'] + r['pot6']
        r['Pmeas'] = (r['pv7'] or 0) + (r['pv6'] or 0)
    return rows

if __name__ == '__main__':
    rows = build()
    json.dump(rows, open('grid5p.json', 'w'))
    days = {}
    for r in rows:
        days.setdefault(r['day'], []).append(r)
    print('day         measured  potential  est.share  peakP')
    for d, rs in sorted(days.items()):
        m = sum(r['Pmeas'] for r in rs) / 12000; p = sum(r['P'] for r in rs) / 12000
        e = sum(r['P'] for r in rs if r['est7'] or r['est6']) / 12000
        print(d, '%8.2f %9.2f %9.0f%% %7.0f' % (m, p, 100 * e / p if p else 0, max(r['P'] for r in rs)))
