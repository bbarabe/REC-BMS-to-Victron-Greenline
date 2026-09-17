import json, sys, datetime as dt
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Los_Angeles")
H = json.load(open("stats.json"))["hour"]
def m(e, k="mean"): return {r["start"]: r.get(k) for r in H[e]}
fly, brow, home = m("flybridge_solar_pv_yield_power"), m("brow_solar_pv_yield_power"), m("solark_pv_power")
load, qin, bi, soc, cvl, bv = m("gx_device_consumption_power_l1"), m("quattro_input_power_l1"), m("rec_bms_main_bank_dc_bus_current"), m("rec_bms_main_bank_charge"), m("rec_bms_main_bank_maximum_allowed_charging_voltage"), m("rec_bms_main_bank_dc_bus_voltage")
for ds in sys.argv[1:]:
    d = dt.date.fromisoformat(ds)
    print(f"\n{ds}  hr | PV fly brow | home kW | x1000 | load W | shore W | bank A | SOC   | CVL    Vbat")
    for t in sorted(fly):
        lt = dt.datetime.fromtimestamp(t/1000, TZ)
        if lt.date() != d or not 6 <= lt.hour <= 19: continue
        pv = (fly.get(t) or 0) + (brow.get(t) or 0); hm = home.get(t) or 0
        print(f"           {lt.hour:02d} | {pv:4.0f} {fly.get(t) or 0:4.0f} {brow.get(t) or 0:4.0f} | {hm/1000:5.1f}  | {1000*pv/hm if hm>500 else 0:5.0f} | {load.get(t) or 0:6.0f} | {qin.get(t) or 0:6.0f}  | {bi.get(t) or 0:6.1f} | {soc.get(t) or 0:5.1f} | {cvl.get(t) or 0:5.2f} {bv.get(t) or 0:6.2f}")
