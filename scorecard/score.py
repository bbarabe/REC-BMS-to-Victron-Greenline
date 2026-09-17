import json, datetime as dt, collections
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Los_Angeles")
S = json.load(open("stats.json"))
R = json.load(open("raw_states.json"))
D, H = S["day"], S["hour"]
def day(ms): return dt.datetime.fromtimestamp(ms/1000, TZ).date()
def dmap(e, k):
    return {day(r["start"]): r.get(k) for r in D[e] if r.get(k) is not None}
def chg(*es):
    out = collections.defaultdict(float)
    for e in es:
        for d, v in dmap(e, "change").items(): out[d] += v
    return out
pv = chg("flybridge_solar_total_yield", "brow_solar_total_yield")
shore_out = chg("quattro_energy_from_ac_in_1_to_ac_out", "quattro_energy_from_ac_in_2_to_ac_out")
shore_chg = chg("quattro_energy_from_ac_in_1_to_inverter", "quattro_energy_from_ac_in_2_to_inverter")
inv = chg("quattro_energy_from_inverter_to_ac_out")
bchg = chg("gx_device_dc_battery_charge_energy"); bdis = chg("gx_device_dc_battery_discharge_energy")
home = chg("solark_energy_total")
homepv = dmap("solark_pv_power", "mean")
load = dmap("gx_device_consumption_power_l1", "mean"); dcl = dmap("gx_device_dc_consumption", "mean")
qin = dmap("quattro_input_power_l1", "mean")
smin = dmap("rec_bms_main_bank_charge", "min"); smax = dmap("rec_bms_main_bank_charge", "max")
cvl = dmap("rec_bms_main_bank_maximum_allowed_charging_voltage", "mean")
# hourly: SOC first/last, throughput lower bound from hourly mean current, daylight PV-zero hours
hs = collections.defaultdict(list)
for r in H["rec_bms_main_bank_charge"]:
    if r.get("mean") is not None: hs[day(r["start"])].append((r["start"], r["mean"]))
ah_pos = collections.defaultdict(float); ah_neg = collections.defaultdict(float)
for r in H["rec_bms_main_bank_dc_bus_current"]:
    m = r.get("mean")
    if m is None: continue
    (ah_pos if m > 0 else ah_neg)[day(r["start"])] += abs(m)
elev = {r["start"]: r["mean"] for r in H["sun_solar_elevation"] if r.get("mean") is not None}
pvh = collections.defaultdict(float)
for e in ("flybridge_solar_pv_yield_power", "brow_solar_pv_yield_power"):
    for r in H[e]:
        if r.get("mean") is not None: pvh[r["start"]] += r["mean"]
dead = collections.defaultdict(int); dayl = collections.defaultdict(int)
for t, el in elev.items():
    if el > 20 and t in pvh:
        dayl[day(t)] += 1
        if pvh[t] < 30: dead[day(t)] += 1
# relay: transitions and islanded hours from raw
def parse(s): return dt.datetime.fromisoformat(s)
src = sorted(((parse(s.get("last_changed") or s["last_updated"]), s["state"]) for s in R["gx_device_ac_active_input_source"]))
trans = collections.defaultdict(int); isl = collections.defaultdict(float)
for (t0, s0), (t1, s1) in zip(src, src[1:] + [(dt.datetime.now(TZ), None)]):
    if s0 == "not_connected":
        a = t0
        while a < t1:
            nxt = min(t1, dt.datetime.combine(a.astimezone(TZ).date() + dt.timedelta(days=1), dt.time(0), TZ))
            isl[a.astimezone(TZ).date()] += (nxt - a).total_seconds()/3600; a = nxt
    if s1 is not None and s0 != s1 and "unavailable" not in (s0, s1) and "unknown" not in (s0, s1):
        trans[t1.astimezone(TZ).date()] += 1
KWH = 1440*55.5/1000
print("date   | PV   home  PV/home| shore  ofwhich | invert | AC ld DC ld | batt+  batt-  move%  | hrly Ah+ Ah- | SOC first>last (min-max)  | CVL  | xfers isl h | dead/daylight h")
print("       | kWh  kWh   x1000  | kWh    charger | kWh    | kWh   kWh   | kWh    kWh          |              |                           | mean |             |")
rows = []
for d in sorted(pv):
    if d < dt.date(2026, 9, 1): continue
    s = sorted(hs.get(d, []))
    first, last = (s[0][1], s[-1][1]) if s else (None, None)
    sh = shore_out[d] + shore_chg[d]
    mv = 100*(bchg[d]+bdis[d])/KWH
    hp = home.get(d, 0)
    row = dict(date=str(d), pv=pv[d], home=hp, shore=sh, shore_chg=shore_chg[d], inv=inv[d], acload=load.get(d,0)*24/1000,
               dcload=dcl.get(d,0)*24/1000, bchg=bchg[d], bdis=bdis[d], move=mv, ahp=ah_pos[d], ahn=ah_neg[d], soc0=first, soc1=last,
               smin=smin.get(d), smax=smax.get(d), cvl=cvl.get(d), xf=trans.get(d), isl=isl.get(d), dead=dead[d], dayl=dayl[d], qin=qin.get(d,0)*24/1000)
    rows.append(row)
    print(f"{d:%m-%d}  | {pv[d]:4.1f} {hp:5.1f} {1000*pv[d]/hp if hp else 0:6.1f} | {sh:5.1f}  {shore_chg[d]:5.1f}   | {inv[d]:5.1f}  | {row['acload']:5.1f} {row['dcload']:5.1f} | {bchg[d]:5.2f} {bdis[d]:6.2f} {mv:6.1f}  | {ah_pos[d]:6.1f} {ah_neg[d]:6.1f} | {first or 0:5.1f} > {last or 0:5.1f} ({smin.get(d) or 0:5.1f}-{smax.get(d) or 0:5.1f}) | {cvl.get(d) or 0:5.2f}| {str(trans.get(d,'-')):>4} {isl.get(d,0):5.1f} | {dead[d]:2d}/{dayl[d]:2d}")
json.dump(rows, open("scorecard.json", "w"), default=str)
