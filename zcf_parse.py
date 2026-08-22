#!/usr/bin/env python3
"""Parse a CZone configuration file (.zcf) and print the circuit table.

Usage:
  zcf_parse.py <file.zcf> [--json] [--raw]

A .zcf is what the CZone Configuration Tool writes and what an MFD pushes to a
module over PGN 130816. It is the only place the **latching/momentary split**
is recorded -- the UC1 never puts it on the bus (see specification.md, "Switch
type is not on the bus, it is in the .zcf"). This tool exists to derive
`momentary_outputs` for dbus-czone/config.ini from the boat's own configuration
instead of guessing, and to map Venus outputs to physical CZone channels.

Nothing here talks to the bus or the Cerbo; it is a pure file reader.

Format, verified against two independently produced files -- the boat's
48-56.zcf and gerryvel/SR-Aktor's "AD CZone System1.zcf":

  header    [0]=0x06 marker, [1:5]=u32 payload length (= filesize-7),
            [5]=CRC-8 of [0:5], [-1]=CRC-8 of [6:-1].  CRC-8 poly 0x07, init 0.
  control   section found by its `08 ?? 05 0E` marker at offset i; u32 length
            at i-6, u16 record count at i-2, records at i+4.
  record    id(u8) f0(u16) f1(u16) f2(u16) nameLen(u8) name
            cmdLen(u32) commanders  outLen(u32) outCount(u16) outputs[5*n]
            f1 is the category bitmask and matches PGN 130820's trailer
            exactly. f2 was 0x0020 on all 14 circuits seen across both files,
            so it is not the switch type.
  commander 8 bytes: u16 channelAddress, u8, u8 FUNCTION, u8, 3 unknown.
            The function byte carries the switch type: 0x09 momentary,
            0x01/0x03/0x04 latching. A circuit is momentary if any of its
            commanders says so.
  outputs   5 bytes each: u16 channelAddress, u24 packed; level = packed & 0x3FF
            (1000 = 100%).  channelAddress is (dipswitch << 8) | channel.
  loads     [u32 len][u16 count][H][records], each record H bytes with the name
            length in its last byte, then the name. Gives channel labels.
"""
import json
import struct
import sys

MOMENTARY_FN = 0x09
LATCHING_FN = (0x01, 0x03, 0x04)

# Same table as PGN 130820's category mask (specification.md).
CATEGORY = {0x0000: "", 0x0004: "Navigation", 0x0400: "Lighting",
            0x1000: "Pump"}


def crc8(data, crc=0):
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class Zcf:
    def __init__(self, buf):
        self.buf = buf
        self.warnings = []
        self.circuits = []
        self.loads = []
        self._header()
        self._control()
        self._loads()

    def u16(self, o):
        return struct.unpack_from("<H", self.buf, o)[0]

    def u32(self, o):
        return struct.unpack_from("<I", self.buf, o)[0]

    def _warn(self, msg):
        self.warnings.append(msg)

    def _header(self):
        buf = self.buf
        if len(buf) < 8:
            raise ValueError("too short to be a .zcf")
        self.marker = buf[0]
        self.declared_len = self.u32(1)
        self.header_crc_ok = crc8(buf[0:5]) == buf[5]
        self.payload_crc_ok = crc8(buf[6:-1]) == buf[-1]
        if self.marker != 0x06:
            self._warn("unexpected marker 0x%02x (expected 0x06)" % self.marker)
        if self.declared_len != len(buf) - 7:
            self._warn("length field %d != filesize-7 (%d)"
                       % (self.declared_len, len(buf) - 7))
        if not self.header_crc_ok:
            self._warn("header CRC mismatch")
        if not self.payload_crc_ok:
            self._warn("payload CRC mismatch")

    def _commanders(self, blob):
        """u16 count, then one 8-byte record per commander (button)."""
        out = []
        if len(blob) < 2:
            return out
        count = struct.unpack_from("<H", blob, 0)[0]
        for k in range(count):
            o = 2 + k * 8
            if o + 8 > len(blob):
                self._warn("commander blob truncated")
                break
            rec = blob[o:o + 8]
            out.append({"channel_address": struct.unpack_from("<H", rec, 0)[0],
                        "function": rec[3],
                        "raw": rec.hex(" ")})
        return out

    def _control(self):
        buf = self.buf
        for i in range(6, len(buf) - 4):
            if buf[i] != 0x08 or buf[i + 2] != 0x05 or buf[i + 3] != 0x0E:
                continue
            length, count = self.u32(i - 6), self.u16(i - 2)
            end = (i - 2) + length
            if length < 6 or count == 0 or end > len(buf) or end <= i:
                continue
            mark = len(self.warnings)
            try:
                recs = self._control_records(i + 4, end, count)
            except (struct.error, IndexError, ValueError, UnicodeDecodeError):
                del self.warnings[mark:]
                continue
            self.circuits = recs
            self.control_at = i - 6
            return
        raise ValueError("no control-record section found")

    def _control_records(self, o, end, count):
        recs = []
        for _ in range(count):
            cid = self.buf[o]
            fields = [self.u16(o + 1), self.u16(o + 3), self.u16(o + 5)]
            nlen = self.buf[o + 7]
            name = self.buf[o + 8:o + 8 + nlen].decode("latin1")
            co = o + 8 + nlen
            clen = self.u32(co)
            cmds = self._commanders(self.buf[co + 4:co + 4 + clen])
            oo = co + 4 + clen
            olen, ocount = self.u32(oo), self.u16(oo + 4)
            outputs = []
            if olen == 2 + 5 * ocount:
                for j in range(ocount):
                    e = oo + 6 + j * 5
                    addr = self.u16(e)
                    packed = (self.buf[e + 2] | self.buf[e + 3] << 8
                              | self.buf[e + 4] << 16)
                    outputs.append({"channel_address": addr,
                                    "dipswitch": addr >> 8,
                                    "channel": addr & 0xFF,
                                    "level": packed & 0x3FF})
            else:
                # Scenes and other non-output actions reuse this slot with a
                # different body; record the fact rather than guess at it.
                self._warn("circuit %d %r: output block is not a channel list"
                           % (cid, name))
            fns = [c["function"] for c in cmds]
            for fn in fns:
                if fn != MOMENTARY_FN and fn not in LATCHING_FN:
                    self._warn("circuit %d %r: unknown commander function 0x%02x"
                               % (cid, name, fn))
            recs.append({"id": cid, "name": name,
                         "category_mask": fields[1],
                         "category": CATEGORY.get(fields[1],
                                                  "0x%04x?" % fields[1]),
                         "fields": fields, "commanders": cmds,
                         "outputs": outputs,
                         "momentary": MOMENTARY_FN in fns})
            o = oo + 4 + olen
        if o != end:
            raise ValueError("control section consumed to %d, expected %d"
                             % (o, end))
        return recs

    def _loads(self):
        buf = self.buf
        for i in range(6, len(buf) - 8):
            length = self.u32(i)
            if length < 8 or i + 4 + length > len(buf):
                continue
            end = i + 4 + length
            count, h = self.u16(i + 4), buf[i + 6]
            if count == 0 or count > 4000 or not 15 <= h <= 24:
                continue
            o, ok = i + 7, True
            for _ in range(count):
                if o + h > end or o + h + buf[o + h - 1] > end:
                    ok = False
                    break
                o += h + buf[o + h - 1]
            if not (ok and o == end):
                continue
            o = i + 7
            for _ in range(count):
                nlen = buf[o + h - 1]
                addr = self.u16(o)
                self.loads.append({"channel_address": addr,
                                   "dipswitch": addr >> 8,
                                   "channel": addr & 0xFF,
                                   "name": buf[o + h:o + h + nlen].decode("latin1"),
                                   "raw": buf[o:o + h].hex(" ")})
                o += h + nlen
            self.loads_at = i
            return
        self._warn("no loads section found (channel labels unavailable)")

    def bank_table(self):
        """Circuits in bank order.

        The index a PGN 127501 field or a PGN 65299 query uses. Verified on
        this boat against the 130820 name replies: the bank enumerates its
        circuits in ascending circuit-ID order (here IDs 5..15 <-> outputs
        1..11), so a circuit ID is NOT a bank index -- PGN 65280 carries the
        former and 127501/127502 the latter.
        """
        ordered = sorted(self.circuits, key=lambda c: c["id"])
        ids = [c["id"] for c in ordered]
        if ids != list(range(ids[0], ids[0] + len(ids))):
            self._warn("circuit IDs are not contiguous (%s), so the bank order "
                       "below is an assumption -- check it against a 65299 "
                       "name sweep before trusting the output numbers" % ids)
        return ordered


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) != 1:
        print("usage: zcf_parse.py <file.zcf> [--json] [--raw]", file=sys.stderr)
        return 2
    with open(args[0], "rb") as fh:
        z = Zcf(fh.read())
    ordered = z.bank_table()

    if "--json" in flags:
        print(json.dumps({"circuits": ordered, "loads": z.loads,
                          "warnings": z.warnings}, indent=2))
        return 0

    print("%s: %d bytes, header CRC %s, payload CRC %s"
          % (args[0], len(z.buf),
             "ok" if z.header_crc_ok else "BAD",
             "ok" if z.payload_crc_ok else "BAD"))
    loadname = {l["channel_address"]: l["name"] for l in z.loads}
    print()
    print("out  id  circuit                 category    dip/ch  type       load")
    for idx, c in enumerate(ordered, start=1):
        o = c["outputs"][0] if c["outputs"] else None
        print("%-4d %-3d %-23s %-11s %-7s %-10s %s"
              % (idx, c["id"], c["name"][:23], c["category"],
                 "%d/%-2d" % (o["dipswitch"], o["channel"]) if o else "  -  ",
                 "MOMENTARY" if c["momentary"] else "latching",
                 loadname.get(o["channel_address"], "") if o else ""))
        if "--raw" in flags:
            cmds = ", ".join("ch=%04x fn=%02x" % (m["channel_address"],
                                                  m["function"])
                             for m in c["commanders"]) or "(none)"
            print("       fields=%04x %04x %04x  commanders: %s"
                  % (c["fields"][0], c["fields"][1], c["fields"][2], cmds))
    mom = [str(i) for i, c in enumerate(ordered, start=1) if c["momentary"]]
    print()
    print("dbus-czone/config.ini:  momentary_outputs = %s"
          % (", ".join(mom) if mom else "(none)"))
    for w in z.warnings:
        print("warning: %s" % w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
