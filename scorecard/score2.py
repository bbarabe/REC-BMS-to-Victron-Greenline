import json, datetime as dt, collections
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Los_Angeles")
R = json.load(open("raw_states.json")); rows = json.load(open("scorecard.json"))
S = json.load(open("stats.json"))["day"]
homepv = {dt.datetime.fromtimestamp(r["start"]/1000, TZ).date(): r["mean"]*24/1000 for r in S["solark_pv_power"] if r.get("mean") is not None}
def parse(s): return dt.datetime.fromisoformat(s)
def series(e): return sorted((parse(s.get("last_changed") or s["last_updated"]), s["state"]) for s in R[e])
def hours_in(e, want, h0=9, h1=17):
    out = collections.defaultdict(float); ser = series(e)
    for (t0, s0), (t1, _) in zip(ser, ser[1:] + [(dt.datetime.now(TZ), None)]):
        if s0 != want: continue
        a = t0.astimezone(TZ)
        while a < t1:
            d = a.date(); lo = dt.datetime.combine(d, dt.time(h0), TZ); hi = dt.datetime.combine(d, dt.time(h1), TZ)
            b = min(t1, dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0), TZ))
            ov = (min(b, hi) - max(a, lo)).total_seconds()
            if ov > 0: out[d] += ov/3600
            a = b
    return out
lim_f = hours_in("flybridge_solar_mppt_operation_mode", "voltage_current_limited")
lim_b = hours_in("brow_solar_mppt_operation_mode", "voltage_current_limited")
isl_day = hours_in("gx_device_ac_active_input_source", "not_connected")
dep = collections.defaultdict(int)
ser = series("gx_device_ac_active_input_source")
for (t0, s0), (t1, s1) in zip(ser, ser[1:]):
    if s1 == "not_connected" and s0 not in ("unavailable", "unknown"): dep[t1.astimezone(TZ).date()] += 1
print("date  | PV kWh | home kWh | shore kWh | AC+DC load | inverted | bank move % | SOC first>last | departures | island h 9-17 | MPPT limited h 9-17 (fly/brow)")
for r in rows:
    d = dt.date.fromisoformat(r["date"])
    if d.isoformat() in ("2026-09-05", "2026-09-06", "2026-09-07"): tag = " (cruising)"
    else: tag = ""
    print(f"{d:%m-%d} | {r['pv']:5.1f} | {homepv.get(d,0):6.1f} | {r['shore']:6.1f} | {r['acload']+r['dcload']:6.1f} | {r['inv']:5.1f} | {r['move']:6.1f} | {r['soc0'] or 0:5.1f}>{r['soc1'] or 0:5.1f} | {dep.get(d,'-') if d>=dt.date(2026,9,8) else '-':>3} | {isl_day.get(d,0):4.1f} | {lim_f.get(d,0):4.1f}/{lim_b.get(d,0):4.1f}{tag}")
