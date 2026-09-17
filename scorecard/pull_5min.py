import json, ha, sys
ha.init()
ENTS = """flybridge_solar_pv_yield_power brow_solar_pv_yield_power flybridge_solar_pv_bus_voltage brow_solar_pv_bus_voltage
gx_device_consumption_power_l1 gx_device_dc_consumption gx_device_dc_battery_power gx_device_pv_power
rec_bms_main_bank_charge rec_bms_main_bank_dc_bus_voltage rec_bms_main_bank_dc_bus_current
rec_bms_main_bank_maximum_allowed_charging_voltage quattro_dc_power quattro_input_power_l1 quattro_output_power_l1""".split()
out = {}
for e in ENTS:
    rows, off = [], 0
    while True:
        r = ha.call("ha_get_history", entity_ids="sensor." + e, source="statistics",
                    start_time="2026-09-06T00:00:00-07:00", period="5minute", limit=1000, offset=off,
                    statistic_types=["mean", "min", "max"])
        d = r.get("data", r)
        ents = d.get("entities") or []
        if not ents:
            break
        en = ents[0]
        rows += en.get("statistics", [])
        if en.get("has_more"):
            off = en["next_offset"]
        else:
            break
    out[e] = rows
    print(e, len(rows), rows[0] if rows else None, file=sys.stderr)
json.dump(out, open("stats5.json", "w"))
