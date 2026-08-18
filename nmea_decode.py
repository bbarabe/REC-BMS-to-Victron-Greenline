#!/usr/bin/env python3
"""Decode a candump capture into a CZone/NMEA-2000 switching event timeline.

Usage:
  nmea_decode.py <capture.log> [--from "HH:MM:SS"] [--to "HH:MM:SS"]
                 [--all]        also list every proprietary frame that changed
                 [--raw PGN]    dump every frame of one PGN (decimal)

Reports, in time order:
  * PGN 127502 Switch Bank Control  — every frame (who commanded what)
  * PGN 127501 Switch Bank Status   — only when the decoded state changes,
                                      tracked per (source, bank instance)
  * CZone/BEP proprietary frames whose payload changed (candidate command path)
Source-address names come from the address-claim survey of this bus.
"""
import sys
import re
import collections

SRC = {
    0x00: "Navico internetwork", 0x01: "mfg917 internetwork",
    0x02: "CZone Output Interface (BEP)", 0x03: "Navico rudder",
    0x04: "Navico steering", 0x05: "Simrad steering", 0x06: "Simrad steering",
    0x07: "Simrad steering", 0x0C: "Navico AUTOPILOT", 0x0D: "Navico rudder",
    0x0E: "Simrad steering/drive", 0x10: "battery monitor",
    0x14: "Simrad nav", 0x15: "Navico nav", 0x16: "Simrad nav",
    0x17: "Simrad DISPLAY (MFD)", 0x18: "Simrad nav", 0x1C: "AIS",
    0x1D: "Simrad instrument", 0x21: "Fusion stereo", 0x23: "Airmar GPS",
    0x39: "YDNB-07 bridge", 0x64: "mfg999 internetwork",
    0xE3: "Victron Cerbo GX", 0xFE: "*** Node-RED (null address) ***",
}

SWNAME = {1: "Bilge1", 2: "Bilge2", 3: "Horn", 4: "Helm", 5: "Nav",
          6: "Anchor", 7: "Red", 8: "Fly", 9: "Underwater", 10: "Bow1",
          11: "Bow2"}

VAL = {0: "OFF", 1: "ON", 2: "err", 3: "--"}

LINE = re.compile(r"\((.*?)\)\s+can\d+\s+([0-9A-Fa-f]+)\s+\[(\d+)\]\s+([0-9A-Fa-f ]+)")


def pgn_of(canid):
    pf = (canid >> 16) & 0xFF
    if pf < 240:                      # PDU1: destination-specific
        return (canid >> 8) & 0x3FF00
    return (canid >> 8) & 0x3FFFF


def decode_bank(data):
    """PGN 127501/127502 payload -> (instance, {sw: 2-bit value})."""
    sw = {}
    for i in range(1, 29):
        byte = (i - 1) // 4 + 1
        if byte >= len(data):
            break
        sw[i] = (data[byte] >> (((i - 1) % 4) * 2)) & 0x03
    return data[0], sw


def fmt_bank(sw, only_known=True):
    hi = max(SWNAME) if only_known else 28
    return " ".join("%s=%s" % (SWNAME.get(i, i), VAL[sw[i]])
                    for i in range(1, hi + 1) if i in sw and sw[i] != 3)


def main():
    args = sys.argv[1:]
    path = args[0]
    show_all = "--all" in args
    raw_pgn = None
    if "--raw" in args:
        raw_pgn = int(args[args.index("--raw") + 1])
    t_from = args[args.index("--from") + 1] if "--from" in args else None
    t_to = args[args.index("--to") + 1] if "--to" in args else None

    last501 = {}          # (src, instance) -> sw dict
    lastprop = {}         # (src, pgn) -> payload hex
    counts = collections.Counter()

    for line in open(path, encoding="utf-8", errors="replace"):
        if line.startswith("### MARK"):
            print("\n" + line.rstrip() + "\n")
            continue
        m = LINE.match(line.strip())
        if not m:
            continue
        ts, idhex, dlc, payload = m.groups()
        clock = ts.split(" ")[-1][:12]
        if t_from and clock < t_from:
            continue
        if t_to and clock > t_to:
            continue
        canid = int(idhex, 16)
        src = canid & 0xFF
        pgn = pgn_of(canid)
        prio = (canid >> 26) & 7
        data = bytes(int(h, 16) for h in payload.split())
        who = SRC.get(src, "src 0x%02X" % src)
        counts[(pgn, src)] += 1

        if raw_pgn is not None:
            if pgn == raw_pgn:
                print("%s  %-30s PGN %-6d prio%d  %s" % (clock, who, pgn, prio, payload.strip()))
            continue

        if pgn == 127502 and len(data) >= 2:
            inst, sw = decode_bank(data)
            cmds = " ".join("%s->%s" % (SWNAME.get(i, i), VAL[sw[i]])
                            for i in sorted(sw) if sw[i] != 3)
            print("%s  CMD  127502 from %-30s bank=%-3d %s   [%s]"
                  % (clock, who, inst, cmds or "(no change bits)", payload.strip()))

        elif pgn == 127501 and len(data) >= 2:
            inst, sw = decode_bank(data)
            key = (src, inst)
            if last501.get(key) != sw:
                delta = ""
                if key in last501:
                    ch = [(i, last501[key].get(i), sw[i]) for i in sw
                          if last501[key].get(i) != sw[i]]
                    delta = "  CHANGED: " + ", ".join(
                        "%s %s->%s" % (SWNAME.get(i, i), VAL.get(o, o), VAL[n])
                        for i, o, n in ch)
                last501[key] = sw
                print("%s  STAT 127501 from %-30s bank=%-3d %s%s"
                      % (clock, who, inst, fmt_bank(sw), delta))

        elif show_all and (130816 <= pgn <= 131071 or 65280 <= pgn <= 65535):
            # Fast-packet byte 0 is (sequence << 5) | frame_index and ticks on
            # every transmission, so key on the frame index and compare the
            # payload without it — otherwise every frame looks "changed".
            key = (src, pgn, data[0] & 0x1F)
            body = payload.strip()[3:]
            if lastprop.get(key) != body:
                first = key not in lastprop
                lastprop[key] = body
                if not first:
                    print("%s  PROP %-6d from %-30s f%-2d %s"
                          % (clock, pgn, who, data[0] & 0x1F, body))


    print("\n===== frame counts (pgn, src) =====")
    for (pgn, src), n in counts.most_common(25):
        print("  PGN %-7d src 0x%02X %-32s %d" % (pgn, src, SRC.get(src, ""), n))


if __name__ == "__main__":
    main()
