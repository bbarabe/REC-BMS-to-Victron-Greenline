#!/usr/bin/env python3
"""
dbus-czone — standalone Venus OS driver for the CZone switch bank.

Replaces the Node-RED "CZone Control" flow (archive/CZoneProxy.json). Runs as a
daemontools service, starts seconds after D-Bus at boot — independent of
Node-RED / Signal K — and is untouched by Node-RED deploys.

Publishes ONE multi-output switch bank, the way Venus models the GX IO
extender (/opt/victronenergy/dbus-switch/dbus-switch.py):

    com.victronenergy.switch.czone
        /SwitchableOutput/output_1 .. output_N

rather than N separate "virtual switch" devices, so the whole bank costs a
single device instance and VRM renders it as one unit.

Auto-discovery (wire format documented in specification.md,
"CZone circuit discovery"):

  1. Listen to PGN 127501. The bank instances the CZone UC1 broadcasts ARE
     the bank list, and the number of 2-bit fields that are not 3
     ("unavailable") IS the circuit count. Nothing needs hardcoding.
  2. For each circuit, send the CZone query PGN 65299 and read the name and
     category out of the PGN 130820 reply. ~1 s for a whole bank.

Control uses PGN 127502 on the bank-1 instance — the user latch that the
MFD and the keypads share — and mirrors exactly what the MFD sends:

  * LATCHING circuits: 01 is a button press and TOGGLES the latch; 00 is
    the release and is ignored. Sent as press + release.
  * MOMENTARY circuits: the output FOLLOWS the bit. 01 = on, 00 = off.

That distinction is not published by CZone — it is absent from every
message the UC1 sends — so it lives in Venus's own per-output
`Settings/Type`: seeded from config.ini, persisted in localsettings, and
changeable by the user in the GUI without touching this code.

Baselines:
  https://github.com/victronenergy/velib_python   (dbusdummyservice.py)
  /opt/victronenergy/dbus-switch/dbus-switch.py   (multi-output switch bank)
"""

import configparser
import glob
import json
import logging
import os
import platform
import select
import socket
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "1.0.0"
BUSITEM = "com.victronenergy.BusItem"

log = logging.getLogger("dbus-czone")


# ----------------------------------------------------------------------------
# velib_python (use the copy shipped with Venus OS so the API always matches
# the running localsettings/dbus stack; /data/velib_python wins if present)
# ----------------------------------------------------------------------------
def _find_velib():
    candidates = ["/data/velib_python",
                  "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"]
    candidates += sorted(glob.glob("/opt/victronenergy/*/ext/velib_python"))
    candidates.append(os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "vedbus.py")):
            sys.path.insert(1, c)
            return c
    raise RuntimeError("velib_python not found (looked in %s)" % candidates)


_VELIB_DIR = _find_velib()
from vedbus import VeDbusService            # noqa: E402
from settingsdevice import SettingsDevice   # noqa: E402


# ============================================================================
# CZone / NMEA 2000 protocol
# ============================================================================
PGN_STATUS = 127501          # Binary Switch Bank Status   (UC1 -> everyone)
PGN_CONTROL = 127502         # Switch Bank Control         (us  -> UC1)
PGN_CFG_REPLY = 130820       # CZone circuit description   (UC1 -> everyone)
PGN_CFG_QUERY = 65299        # CZone circuit query         (us  -> UC1)

MFG_BEP = 0x9927             # little-endian: mfg 295 (BEP Marine), industry 4

# Venus switch output types (dbus-switch.py)
TYPE_MOMENTARY = 0
TYPE_TOGGLE = 1

FIELDS_PER_FRAME = 28        # 2 bits per switch across data bytes 1..7
UNAVAILABLE = 3


def can_id(pgn, src, priority):
    """Build a 29-bit PDU2 (broadcast) CAN id."""
    return (priority << 26) | (pgn << 8) | src


def pgn_of(cid):
    pf = (cid >> 16) & 0xFF
    return (cid >> 8) & 0x3FF00 if pf < 240 else (cid >> 8) & 0x3FFFF


def decode_status(data):
    """PGN 127501 -> (bank_instance, [state per circuit], n_configured).

    Circuit states are 0/1; 3 means the circuit is not configured, which is
    how the bank size is discovered.
    """
    if len(data) < 8:
        return None, [], 0
    states = []
    for i in range(FIELDS_PER_FRAME):
        byte = data[i // 4 + 1]
        states.append((byte >> ((i % 4) * 2)) & 0x03)
    n = sum(1 for v in states if v != UNAVAILABLE)
    return data[0], states, n


def build_control(bank, circuit, bit, src=0xFE, priority=3):
    """PGN 127502 frame setting one circuit's 2-bit field, all others 'no change'.

    Mirrors the MFD byte for byte (it uses priority 3, CAN id 0DF20E17); only
    the source address differs. 01 = pressed/on, 00 = released/off,
    11 = leave alone.
    """
    data = bytearray([bank & 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
    idx = circuit // 4 + 1
    pos = (circuit % 4) * 2
    data[idx] = (data[idx] & (~(0x03 << pos) & 0xFF)) | ((bit & 0x03) << pos)
    return can_id(PGN_CONTROL, src, priority), bytes(data)


def build_query(circuit, bank, tag, src=0xFE, priority=7):
    """PGN 65299 'describe circuit' request, as the NSX-7 sends it.

    `tag` is echoed in the reply. BIT 7 MUST BE CLEAR: with it set the UC1
    answers with a well-formed but empty record, which is indistinguishable
    from "no such circuit".
    """
    tag &= 0x7F
    data = bytes([MFG_BEP & 0xFF, MFG_BEP >> 8, 0x01,
                  circuit & 0xFF, bank & 0xFF, tag, 0x80, 0x00])
    return can_id(PGN_CFG_QUERY, src, priority), data


CATEGORIES = {0x0000: "", 0x0004: "Navigation", 0x0400: "Lighting", 0x1000: "Pump"}


def decode_cfg_reply(payload):
    """PGN 130820 -> (tag, name, category) or None.

    Layout: 27 99 | 01 | tag | 80 | <NUL-terminated name> | 4 zero bytes |
            category mask (LE u16) | 20 00 00
    An empty name means the circuit does not exist.
    """
    if len(payload) < 6:
        return None
    if payload[0] | (payload[1] << 8) != MFG_BEP or payload[2] != 0x01:
        return None
    tag = payload[3]
    body = payload[5:]                      # skip the 0x80 marker
    name = body.split(b"\x00")[0].decode("ascii", "replace")
    tail = body[len(name) + 1:]
    cat = ""
    if len(tail) >= 6:
        cat = CATEGORIES.get(tail[4] | (tail[5] << 8), "")
    return tag, name, cat


class FastPacket:
    """Reassemble NMEA 2000 fast-packets (byte0 = (seq << 5) | frame_index)."""

    def __init__(self):
        self._open = {}

    def push(self, key, data):
        if len(data) < 2:
            return None
        seq, idx = data[0] >> 5, data[0] & 0x1F
        k = (key, seq)
        if idx == 0:
            self._open[k] = {"total": data[1], "buf": bytearray(data[2:]), "next": 1}
        else:
            rec = self._open.get(k)
            if rec is None or rec["next"] != idx:
                self._open.pop(k, None)
                return None
            rec["buf"] += data[1:]
            rec["next"] += 1
        rec = self._open.get(k)
        if rec and len(rec["buf"]) >= rec["total"]:
            out = bytes(rec["buf"][:rec["total"]])
            del self._open[k]
            return out
        return None


# ============================================================================
# Config
# ============================================================================
class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        c = cp["can"] if cp.has_section("can") else {}
        self.can_iface = c.get("interface", "can0")
        self.src_address = int(str(c.get("source_address", "0xFE")), 16)
        self.can_reconnect_s = int(c.get("reconnect_delay_s", 5))

        d = cp["discovery"] if cp.has_section("discovery") else {}
        self.listen_s = float(d.get("status_listen_s", 8))
        self.query_timeout_s = float(d.get("query_timeout_s", 1.0))
        self.query_retries = int(d.get("query_retries", 3))
        self.prefer_bank = int(d.get("prefer_bank", 1))

        s = cp["switch"] if cp.has_section("switch") else {}
        self.suffix = s.get("service_suffix", "czone")
        self.settings_id = s.get("settings_id", "czone")
        self.instance = int(s.get("instance", 224))
        self.product_name = s.get("product_name", "CZone switch bank")
        self.custom_name = s.get("custom_name", "CZone")
        self.group_prefix = s.get("group_prefix", "CZone")
        self.momentary_outputs = set()
        for tok in str(s.get("momentary_outputs", "")).replace(",", " ").split():
            try:
                self.momentary_outputs.add(int(tok))
            except ValueError:
                pass

        t = cp["control"] if cp.has_section("control") else {}
        self.release_delay_ms = int(t.get("release_delay_ms", 120))
        self.retry_after_s = float(t.get("retry_after_s", 2.5))
        self.max_sends = int(t.get("max_sends", 2))
        self.give_up_s = float(t.get("give_up_s", 9))
        self.stale_state_s = float(t.get("stale_state_s", 6))


# ============================================================================
# D-Bus helpers (identical pattern to dbus-recbms)
# ============================================================================
def private_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus(private=True)
    return dbus.SystemBus(private=True)


def shared_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


def new_service(name, bus):
    import inspect
    if "register" in inspect.signature(VeDbusService.__init__).parameters:
        svc = VeDbusService(name, bus=bus, register=False)
        svc._czone_needs_register = True
    else:
        svc = VeDbusService(name, bus=bus)
        svc._czone_needs_register = False
    return svc


def register_service(svc):
    if getattr(svc, "_czone_needs_register", False):
        svc.register()


# ============================================================================
# The driver
# ============================================================================
class Circuit:
    __slots__ = ("index", "name", "category", "momentary",
                 "state", "state_ts", "pending")

    def __init__(self, index, name, category, momentary):
        self.index = index
        self.name = name
        self.category = category
        self.momentary = momentary
        self.state = None            # last state reported by CZone (0/1)
        self.state_ts = 0.0
        self.pending = None          # {target, cmd_ts, last_send, sends}


class CZoneDriver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.start_ts = time.time()
        self.sbus = shared_bus()
        # set before SettingsDevice exists: its change callback can fire
        # during construction, before _init_service has run
        self.svc = None
        self.bank = cfg.prefer_bank
        self.circuits = []
        self.fp = FastPacket()
        self._sock = None
        self._watch = None
        self._first_status_logged = False

        self._open_can(blocking=True)
        if self._sock is None:
            raise SystemExit("cannot open %s" % cfg.can_iface)

        table = self._discover()
        self._init_settings(table)
        self._init_service()

        self._sock.setblocking(False)
        self._watch = GLib.io_add_watch(
            self._sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
            self._can_readable)
        GLib.timeout_add(250, self._tick)

    # ------------------------------------------------------------------ CAN
    def _open_can(self, blocking=False):
        c = self.cfg
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            # CAN_EFF_FLAG is a NEGATIVE int on 32-bit platforms (armv7l) —
            # mask to u32 or struct 'I' rejects it.
            eff = socket.CAN_EFF_FLAG & 0xFFFFFFFF
            flt = b""
            for pgn in (PGN_STATUS, PGN_CFG_REPLY):
                flt += struct.pack("=II",
                                   ((pgn << 8) | eff) & 0xFFFFFFFF,
                                   (0x03FFFF00 | eff) & 0xFFFFFFFF)
            s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
            s.bind((c.can_iface,))
            s.setblocking(blocking)
        except OSError as e:
            log.error("cannot open %s (%s), retrying in %ds",
                      c.can_iface, e, c.can_reconnect_s)
            GLib.timeout_add_seconds(c.can_reconnect_s, self._reopen_can)
            return
        self._sock = s
        log.info("listening on %s for PGN %d and %d",
                 c.can_iface, PGN_STATUS, PGN_CFG_REPLY)

    def _reopen_can(self):
        self._open_can()
        if self._sock is not None:
            self._sock.setblocking(False)
            self._watch = GLib.io_add_watch(
                self._sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self._can_readable)
        return False

    def _send(self, cid, data):
        if self._sock is None:
            return False
        frame = struct.pack("=IB3x8s",
                            (cid | (socket.CAN_EFF_FLAG & 0xFFFFFFFF)) & 0xFFFFFFFF,
                            len(data), data.ljust(8, b"\x00"))
        try:
            self._sock.send(frame)
            return True
        except OSError as e:
            log.warning("CAN send failed: %s", e)
            return False

    @staticmethod
    def _parse_frame(frame):
        cid, dlc = struct.unpack_from("=IB", frame)
        cid &= socket.CAN_EFF_MASK
        return cid, frame[8:8 + min(dlc, 8)]

    # ------------------------------------------------------------ discovery
    def _discover(self):
        """Return [(index, name, category)] for the chosen bank.

        Phase 1 is passive and tells us which banks exist and how big they
        are; phase 2 asks the UC1 for the names. Falls back to the cached
        table if the bus is silent (CZone powered down).
        """
        c = self.cfg
        banks = {}
        deadline = time.time() + c.listen_s
        settle_at = time.time() + min(3.0, c.listen_s)
        log.info("discovery: listening up to %.0fs for PGN %d ...", c.listen_s, PGN_STATUS)
        while time.time() < deadline:
            frame = self._recv_until(deadline)
            if frame is None:
                break
            cid, data = frame
            if pgn_of(cid) != PGN_STATUS:
                continue
            inst, _states, n = decode_status(data)
            if inst is None or n == 0:
                continue
            # The Navico autopilot spams instance 0 with every field zeroed,
            # which reads as a 28-circuit bank. Never trust a full-width bank.
            if n >= FIELDS_PER_FRAME:
                continue
            src = cid & 0xFF
            banks.setdefault((src, inst), n)
            # The UC1 emits every bank within a millisecond or two of the
            # others, so once the preferred bank has appeared and the bus
            # has had a moment to settle there is nothing more to wait for.
            if inst == c.prefer_bank and time.time() >= settle_at:
                break

        if not banks:
            log.warning("discovery: no CZone status frames seen")
            return self._cached_table()

        for (src, inst), n in sorted(banks.items()):
            log.info("discovery: bank instance %d from src 0x%02X, %d circuits",
                     inst, src, n)
        chosen = None
        for (src, inst), n in sorted(banks.items()):
            if inst == c.prefer_bank:
                chosen = (src, inst, n)
        if chosen is None:
            src, inst = sorted(banks)[0]
            chosen = (src, inst, banks[(src, inst)])
            log.warning("discovery: bank %d not present, using bank %d",
                        c.prefer_bank, inst)
        self.bank = chosen[1]
        count = chosen[2]

        names = self._query_names(count)
        if not names:
            log.warning("discovery: no query replies; falling back to cache")
            return self._cached_table()
        return names

    def _recv_until(self, deadline):
        timeout = deadline - time.time()
        if timeout <= 0:
            return None
        r, _, _ = select.select([self._sock], [], [], timeout)
        if not r:
            return None
        try:
            return self._parse_frame(self._sock.recv(16))
        except OSError:
            return None

    def _query_names(self, count):
        """Ask the UC1 to describe circuits 0..count-1 on the chosen bank."""
        c = self.cfg
        found = {}
        for attempt in range(c.query_retries):
            missing = [i for i in range(count) if i not in found]
            if not missing:
                break
            for i in missing:
                cid, data = build_query(i, self.bank, i, src=c.src_address)
                self._send(cid, data)
                deadline = time.time() + c.query_timeout_s
                while time.time() < deadline:
                    frame = self._recv_until(deadline)
                    if frame is None:
                        break
                    fcid, fdata = frame
                    if pgn_of(fcid) != PGN_CFG_REPLY:
                        continue
                    payload = self.fp.push(fcid & 0xFF, fdata)
                    if payload is None:
                        continue
                    dec = decode_cfg_reply(payload)
                    if dec is None:
                        continue
                    tag, name, cat = dec
                    if tag == (i & 0x7F):
                        if name:
                            found[i] = (name, cat)
                        else:
                            # empty name = circuit does not exist
                            found[i] = ("", "")
                        break
            if attempt and missing:
                log.info("discovery: retry %d for %d circuit(s)", attempt, len(missing))
        table = [(i, found[i][0], found[i][1])
                 for i in sorted(found) if found[i][0]]
        log.info("discovery: %d/%d circuits named", len(table), count)
        for i, name, cat in table:
            log.info("   circuit %-2d %-22s %s", i, name, cat or "(no category)")
        return table

    def _cached_table(self):
        raw = None
        try:
            raw = self.sbus.call_blocking(
                "com.victronenergy.settings", "/Settings/CZone/Circuits",
                BUSITEM, "GetValue", "", [], timeout=5)
        except Exception:
            pass
        if not raw:
            return []
        try:
            data = json.loads(str(raw))
            table = [(int(e["i"]), e["name"], e.get("cat", "")) for e in data["circuits"]]
            self.bank = int(data.get("bank", self.cfg.prefer_bank))
            log.warning("discovery: using cached table (%d circuits, bank %d)",
                        len(table), self.bank)
            return table
        except Exception as e:
            log.error("cached table unreadable: %s", e)
            return []

    def _save_cache(self):
        blob = json.dumps({
            "bank": self.bank,
            "circuits": [{"i": c.index, "name": c.name, "cat": c.category,
                          "type": TYPE_MOMENTARY if c.momentary else TYPE_TOGGLE}
                         for c in self.circuits]})
        try:
            self.sbus.call_blocking(
                "com.victronenergy.settings", "/Settings/CZone/Circuits",
                BUSITEM, "SetValue", "v", [blob], timeout=5)
        except Exception as e:
            log.warning("could not persist circuit table: %s", e)

    # ------------------------------------------------------------- settings
    def _init_settings(self, table):
        c = self.cfg
        if not table:
            # daemontools restarts us; pause first so a powered-down CZone
            # does not turn into a restart loop.
            log.error("no circuits discovered and no cached table — is "
                      "CZone powered? retrying in 30s")
            time.sleep(30)
            raise SystemExit(1)

        cached_types = {}
        try:
            raw = self.sbus.call_blocking(
                "com.victronenergy.settings", "/Settings/CZone/Circuits",
                BUSITEM, "GetValue", "", [], timeout=5)
            if raw:
                for e in json.loads(str(raw)).get("circuits", []):
                    cached_types[int(e["i"])] = int(e.get("type", TYPE_TOGGLE))
        except Exception:
            pass

        supported = {
            "instance": [
                "/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                "switch:%d" % c.instance, 0, 0],
            "circuits": ["/Settings/CZone/Circuits", "", 0, 0],
            "customname": ["/Settings/CZone/CustomName", c.custom_name, 0, 0],
        }
        self.settings = SettingsDevice(self.sbus, supported,
                                       self._setting_changed, timeout=120)
        self.instance = self._claim_instance()

        for idx, name, cat in table:
            out_no = idx + 1
            if idx in cached_types:
                momentary = cached_types[idx] == TYPE_MOMENTARY
            else:
                momentary = out_no in c.momentary_outputs
            self.circuits.append(Circuit(idx, name, cat, momentary))
        log.info("%d circuits on bank %d (%d momentary)",
                 len(self.circuits), self.bank,
                 sum(1 for x in self.circuits if x.momentary))

    def _claim_instance(self):
        c = self.cfg
        try:
            granted = int(str(self.settings["instance"]).split(":")[1])
        except (IndexError, ValueError):
            granted = c.instance
        if granted == c.instance:
            return granted
        in_use = False
        for name in self.sbus.list_names():
            if not str(name).startswith("com.victronenergy.switch."):
                continue
            try:
                di = self.sbus.call_blocking(name, "/DeviceInstance", BUSITEM,
                                             "GetValue", "", [], timeout=2)
                if int(di) == c.instance:
                    in_use = True
                    break
            except Exception:
                continue
        if in_use:
            log.warning("instance %d held by a live service; using %d. Disable the "
                        "Node-RED CZone flow and clean its virtual_cz_vs_* settings.",
                        c.instance, granted)
            return granted
        try:
            self.sbus.call_blocking(
                "com.victronenergy.settings",
                "/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                BUSITEM, "SetValue", "v", ["switch:%d" % c.instance], timeout=5)
            log.info("reconverged instance %d -> %d", granted, c.instance)
            return c.instance
        except Exception as e:
            log.warning("could not pin instance %d (%s); using %d",
                        c.instance, e, granted)
            return granted

    def _setting_changed(self, name, old, new):
        if name == "customname" and self.svc is not None:
            self.svc["/CustomName"] = new

    # -------------------------------------------------------------- service
    def _init_service(self):
        c = self.cfg
        svc = new_service("com.victronenergy.switch.%s" % c.suffix, private_bus())
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion",
                     "%s on Python %s" % (VERSION, platform.python_version()))
        svc.add_path("/Mgmt/Connection", "%s CZone bank %d" % (c.can_iface, self.bank))
        svc.add_path("/DeviceInstance", self.instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", c.product_name)
        svc.add_path("/CustomName", self.settings["customname"], writeable=True,
                     onchangecallback=self._customname_changed)
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Serial", c.settings_id)
        svc.add_path("/Connected", 1)
        svc.add_path("/State", 0x100)             # module state: connected

        valid = (1 << TYPE_MOMENTARY) | (1 << TYPE_TOGGLE)
        for circ in self.circuits:
            o = "/SwitchableOutput/output_%d" % (circ.index + 1)
            svc.add_path(o + "/Name", circ.name)
            svc.add_path(o + "/State", 0, writeable=True,
                         onchangecallback=self._make_state_cb(circ))
            svc.add_path(o + "/Status", 0)
            svc.add_path(o + "/Settings/Type",
                         TYPE_MOMENTARY if circ.momentary else TYPE_TOGGLE,
                         writeable=True, onchangecallback=self._make_type_cb(circ))
            svc.add_path(o + "/Settings/ValidTypes", valid)
            svc.add_path(o + "/Settings/CustomName", circ.name, writeable=True,
                         onchangecallback=lambda p, v: True)
            svc.add_path(o + "/Settings/Group",
                         ("%s %s" % (c.group_prefix, circ.category)).strip())
            svc.add_path(o + "/Settings/ShowUIControl", 1)

        register_service(svc)
        self.svc = svc
        self._save_cache()
        log.info("registered com.victronenergy.switch.%s instance %d, %d outputs",
                 c.suffix, self.instance, len(self.circuits))

    def _customname_changed(self, path, value):
        try:
            self.settings["customname"] = str(value)
        except Exception:
            return False
        return True

    def _make_type_cb(self, circ):
        def cb(path, value):
            try:
                v = int(value)
            except (TypeError, ValueError):
                return False
            if v not in (TYPE_MOMENTARY, TYPE_TOGGLE):
                return False
            circ.momentary = (v == TYPE_MOMENTARY)
            log.info("%s -> %s", circ.name,
                     "momentary" if circ.momentary else "latching")
            GLib.idle_add(self._save_cache_once)
            return True
        return cb

    def _save_cache_once(self):
        self._save_cache()
        return False

    def _make_state_cb(self, circ):
        def cb(path, value):
            return self._command(circ, 1 if value else 0)
        return cb

    # -------------------------------------------------------- control engine
    def _command(self, circ, target):
        """A user (VRM/GUI) wrote State. Returns True to accept the write.

        There is no echo problem here: this callback only ever fires for an
        EXTERNAL write. Updates we make from CAN go through the item's local
        value and never re-enter as commands.
        """
        c = self.cfg
        now = time.time()

        if circ.momentary:
            # Level control: the output follows the bit. Never a press/release
            # pair — the release IS the off command.
            cid, data = build_control(self.bank, circ.index, target,
                                      src=c.src_address)
            self._send(cid, data)
            circ.pending = {"target": target, "cmd_ts": now,
                            "last_send": now, "sends": 1}
            return True

        # Latching: a press TOGGLES, so we must know the current state, and we
        # only press when it disagrees with what was asked for.
        if circ.state is None or now - circ.state_ts > c.stale_state_s:
            log.warning("%s: refusing — no fresh PGN %d state (capture dead?)",
                        circ.name, PGN_STATUS)
            return False
        if circ.state == target:
            return True                      # already there; nothing to send
        self._press(circ)
        circ.pending = {"target": target, "cmd_ts": now,
                        "last_send": now, "sends": 1}
        return True

    def _press(self, circ):
        c = self.cfg
        cid, data = build_control(self.bank, circ.index, 1, src=c.src_address)
        self._send(cid, data)
        # The release re-arms the press detector. Latching circuits ignore it,
        # but the MFD always sends one, so mirror the MFD exactly.
        GLib.timeout_add(c.release_delay_ms, self._release, circ)

    def _release(self, circ):
        cid, data = build_control(self.bank, circ.index, 0, src=self.cfg.src_address)
        self._send(cid, data)
        return False

    def _tick(self):
        """Confirm / retry / revert, every 250 ms.

        Re-pressing a latching circuit TOGGLES it, so a retry needs positive
        evidence the last send was lost: a status frame newer than it that
        still disagrees. The retry floor keeps send latency from faking that.
        """
        c = self.cfg
        now = time.time()
        for circ in self.circuits:
            p = circ.pending
            if p is None:
                continue
            if circ.state is not None and circ.state_ts >= p["cmd_ts"] \
                    and circ.state == p["target"]:
                circ.pending = None
                continue
            if now - p["cmd_ts"] > c.give_up_s:
                circ.pending = None
                log.warning("%s: %s unconfirmed after %.0fs (%d send%s) — reverting",
                            circ.name, "ON" if p["target"] else "OFF",
                            c.give_up_s, p["sends"], "" if p["sends"] == 1 else "s")
                self._publish_state(circ)
                continue
            if circ.state is not None and circ.state_ts > p["last_send"] \
                    and circ.state != p["target"] \
                    and now - p["last_send"] >= c.retry_after_s \
                    and p["sends"] < c.max_sends:
                log.info("%s: re-sending %s", circ.name,
                         "ON" if p["target"] else "OFF")
                if circ.momentary:
                    cid, data = build_control(self.bank, circ.index, p["target"],
                                              src=c.src_address)
                    self._send(cid, data)
                else:
                    self._press(circ)
                p["last_send"] = now
                p["sends"] += 1
        return True

    # ----------------------------------------------------------- CAN inbound
    def _can_readable(self, fd, condition):
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            log.error("CAN socket error, reopening in %ds", self.cfg.can_reconnect_s)
            self._close_can(remove_watch=False)
            GLib.timeout_add_seconds(self.cfg.can_reconnect_s, self._reopen_can)
            return False
        try:
            while True:
                frame = self._sock.recv(16)
                if len(frame) < 16:
                    continue
                cid, data = self._parse_frame(frame)
                pgn = pgn_of(cid)
                if pgn == PGN_STATUS:
                    self._on_status(data)
                elif pgn == PGN_CFG_REPLY:
                    self.fp.push(cid & 0xFF, data)      # keep the assembler fed
        except BlockingIOError:
            pass
        except OSError as e:
            log.error("CAN read failed (%s), reopening in %ds",
                      e, self.cfg.can_reconnect_s)
            self._close_can(remove_watch=False)
            GLib.timeout_add_seconds(self.cfg.can_reconnect_s, self._reopen_can)
            return False
        return True

    def _close_can(self, remove_watch=True):
        if remove_watch and self._watch is not None:
            GLib.source_remove(self._watch)
        self._watch = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _on_status(self, data):
        inst, states, n = decode_status(data)
        if inst != self.bank or n >= FIELDS_PER_FRAME:
            # Not our bank — or the autopilot's instance-0 spam, which reports
            # every field as configured and would look like a 28-circuit bank.
            return
        now = time.time()
        if not self._first_status_logged:
            log.info("first CZone status %.1fs after start", now - self.start_ts)
            self._first_status_logged = True
        for circ in self.circuits:
            v = states[circ.index] if circ.index < len(states) else UNAVAILABLE
            if v == UNAVAILABLE:
                continue
            changed = circ.state != v
            circ.state = v
            circ.state_ts = now
            if changed:
                self._publish_state(circ)

    def _publish_state(self, circ):
        if self.svc is None or circ.state is None:
            return
        o = "/SwitchableOutput/output_%d" % (circ.index + 1)
        self.svc[o + "/State"] = circ.state
        self.svc[o + "/Status"] = 9 if circ.state else 0


def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
    cfg = Config(cfg_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")
    log.info("dbus-czone v%s starting (velib: %s)", VERSION, _VELIB_DIR)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    CZoneDriver(cfg)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
