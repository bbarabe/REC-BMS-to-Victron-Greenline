import json, ha, sys
ha.init()
ENTS = sys.argv[2:]
out = {}
for e in ENTS:
    eid = "sensor." + e
    rows, off = [], 0
    while True:
        r = ha.call("ha_get_history", entity_ids=eid, source="history",
                    start_time="2026-09-06T00:00:00-07:00", limit=1000, offset=off,
                    significant_changes_only=False)
        d = r.get("data", r)
        ents = d.get("entities") or []
        if not ents: break
        en = ents[0]
        rows += en.get("states", [])
        if en.get("has_more"): off = en["next_offset"]
        else: break
        if off > 400000: break
    out[e] = rows
    ts = [s.get("last_changed") or s.get("last_updated") for s in rows]
    print(e, len(rows), min(ts) if ts else None, max(ts) if ts else None, file=sys.stderr)
json.dump(out, open(sys.argv[1], "w"))
