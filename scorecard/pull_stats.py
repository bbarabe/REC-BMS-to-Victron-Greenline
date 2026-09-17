import json, ha, sys
ha.init()
ENTS = """flybridge_solar_total_yield brow_solar_total_yield
quattro_energy_from_ac_in_1_to_ac_out quattro_energy_from_ac_in_1_to_inverter
quattro_energy_from_ac_in_2_to_ac_out quattro_energy_from_ac_in_2_to_inverter
quattro_energy_from_inverter_to_ac_out quattro_energy_from_out_to_inverter
gx_device_dc_battery_charge_energy gx_device_dc_battery_discharge_energy gx_device_pv_energy
rec_bms_main_bank_charge rec_bms_main_bank_dc_bus_current rec_bms_main_bank_dc_bus_voltage rec_bms_main_bank_power
rec_bms_main_bank_consumed_amp_hours rec_bms_main_bank_maximum_allowed_charging_voltage rec_bms_main_bank_maximum_allowed_charge_current
gx_device_consumption_power_l1 gx_device_dc_consumption gx_device_pv_power gx_device_dc_battery_power
quattro_input_power_l1 quattro_output_power_l1 quattro_dc_power quattro_dc_current
flybridge_solar_pv_yield_power brow_solar_pv_yield_power flybridge_solar_pv_bus_voltage brow_solar_pv_bus_voltage
solark_pv_power solark_energy_total sun_solar_elevation""".split()
out = {}
for period in ("day", "hour"):
    for e in ENTS:
        eid = "sensor." + e
        rows, off = [], 0
        while True:
            r = ha.call("ha_get_history", entity_ids=eid, source="statistics",
                        start_time="2026-08-30T00:00:00-07:00", period=period, limit=1000, offset=off)
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
        out.setdefault(period, {})[e] = rows
        print(period, e, len(rows), file=sys.stderr)
json.dump(out, open("stats.json", "w"))
