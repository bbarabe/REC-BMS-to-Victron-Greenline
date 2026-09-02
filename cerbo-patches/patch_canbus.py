#!/usr/bin/env python3
"""Apply configured N2K filters at ingest instead of after decoding.

Pipeline is canbus -> CanboatJs (decode) -> N2kToSignalK (map). n2k-signalk
calls n2kMapper.toDelta(chunk) and only THEN checks isFiltered(), so every
excluded PGN is fully decoded and mapped before being thrown away. This moves
the same predicate into canbus.js onMessage, right after parseCanId(), where
only cheap bit-math has happened. Address claims (60928) are never filtered.

Backups in /data/canbus-patch-backup.  --revert restores.
"""
import os, shutil, subprocess, sys

F = ("/usr/lib/node_modules/signalk-server/node_modules/"
     "@canboat/canboatjs/dist/canbus.js")
BACKUP = "/data/canbus-patch-backup"

INIT_OLD = """        const that = this;
        this.channel = this.socketcan.createRawChannelWithOptions(canDevice, {"""
INIT_NEW = """        const that = this;
        // Ingest-time copy of the configured filters; see onMessage below.
        if (this.options.filters && this.options.filtersEnabled) {
            this.ingestFilters = this.options.filters.filter((f) => {
                return ((f.source && f.source.length) ||
                        (f.pgn && f.pgn.length));
            });
        }
        this.channel = this.socketcan.createRawChannelWithOptions(canDevice, {"""

MSG_OLD = """            const pgn = (0, canId_1.parseCanId)(msg.id);
            if (this.noDataInterval) {
                this.lastDataReceived = Date.now();
            }
            //always send address claims through
"""
MSG_NEW = """            const pgn = (0, canId_1.parseCanId)(msg.id);
            if (this.noDataInterval) {
                this.lastDataReceived = Date.now();
            }
            // Drop filtered PGNs before fast-packet reassembly and decode.
            // n2k-signalk applies this same predicate, but only after
            // toDelta() has already done the work.
            if (pgn.pgn != 60928 &&
                this.ingestFilters &&
                this.ingestFilters.find((f) => {
                    return ((!f.source || f.source.length === 0 ||
                             String(f.source) === String(pgn.src)) &&
                            (!f.pgn || f.pgn.length === 0 ||
                             String(f.pgn) === String(pgn.pgn)));
                })) {
                return;
            }
            //always send address claims through
"""

bp = os.path.join(BACKUP, os.path.basename(F))
if "--revert" in sys.argv:
    if os.path.exists(bp):
        shutil.copy2(bp, F); print("reverted", F)
    else:
        print("NO BACKUP")
    sys.exit()

src = open(F).read()
if MSG_NEW in src:
    sys.exit("ALREADY PATCHED")
for name, old in (("init", INIT_OLD), ("onMessage", MSG_OLD)):
    if src.count(old) != 1:
        sys.exit("ABORT: %s anchor matched %d times" % (name, src.count(old)))

os.makedirs(BACKUP, exist_ok=True)
if not os.path.exists(bp):
    shutil.copy2(F, bp); print("backed up ->", bp)
open(F, "w").write(src.replace(INIT_OLD, INIT_NEW).replace(MSG_OLD, MSG_NEW))
print("patched", F)
r = subprocess.run(["node", "--check", F], capture_output=True, text=True)
print("node --check:", "OK" if r.returncode == 0 else r.stderr[:400])
sys.exit(r.returncode)
