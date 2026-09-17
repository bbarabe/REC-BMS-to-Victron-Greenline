#!/usr/bin/env python3
"""Re-enable the CAN connection with an aggressive PGN exclude list.

Keeps only what saillogger actually needs off the bus:
  127489  Engine Parameters Dynamic  -> propulsion.*.runTime (engine hours)
  129808  DSC Call                   -> saillogger's safety-critical DSC forward
  126996/59904/60928                 -> product info / requests / address claim
Everything else is excluded at ingest by the patched canbus.js.
Navigation (position/SOG/COG) deliberately comes from the venus D-Bus GPS only,
so there is a single source per path and no $source flapping.
"""
import json, shutil, sys

P = "/data/conf/signalk/settings.json"
BAK = "/data/signalk-settings-backup.json"

EXCLUDE = [
    129540, 127245, 127250, 127251, 127252, 127257, 127237, 129029, 129026,
    129025, 129038, 65350, 65341, 65283, 127501, 128259, 128275, 130822,
    130860, 130311, 130310, 127258, 127506, 127508, 61184, 126992, 130316,
    127505, 127497, 127488, 128267, 130306, 129539, 129044, 127493, 127245,
]

if "--revert" in sys.argv:
    shutil.copy2(BAK, P); print("restored", P); sys.exit()

d = json.load(open(P))
try:
    shutil.copy2(P, BAK); print("backed up ->", BAK)
except Exception as e:
    sys.exit("backup failed: %s" % e)

prov = d["pipedProviders"][0]
sub = prov["pipeElements"][0]["options"]["subOptions"]
prov["enabled"] = True
sub["filtersEnabled"] = True
seen, filters = set(), []
for p in EXCLUDE:
    if p not in seen:
        seen.add(p); filters.append({"source": "", "pgn": str(p)})
sub["filters"] = filters

json.dump(d, open(P, "w"), indent=2)
print("provider enabled:", prov["enabled"])
print("filtersEnabled  :", sub["filtersEnabled"])
print("excluded PGNs   :", len(filters))
