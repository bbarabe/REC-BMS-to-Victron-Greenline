#!/usr/bin/env python3
"""
dbus-batteries — standalone Venus OS driver for the NMEA 2000 12V batteries.

Replaces the Node-RED "Batteries Forward" flow (archive/BatteriesForward.json).
Runs as a daemontools service, starts seconds after D-Bus at boot — independent
of Signal K / Node-RED — and is untouched by Node-RED deploys.

The flow read Signal K paths (`electrical.batteries.N.*`,
`propulsion.*.alternatorVoltage`), so the batteries only appeared once the whole
Signal K stack was up and its N2K decoder had converged. This driver reads the
same three PGNs straight off `can0`:

    127508  Battery Status         single frame   instance, V, I, temperature
    127506  DC Detailed Status     fast packet    instance, SOC, SOH, time left
    127489  Engine Parameters Dyn  fast packet    engine instance, alternator V

and publishes one `com.victronenergy.battery.<suffix>` per forwarded source.

WHICH batteries are forwarded is not hardcoded. Every source seen on the bus is
catalogued, and the forward/don't-forward decision, the VRM instance, the name
and the capacity live in localsettings — readable and writeable over D-Bus, so a
GUI (the forked Victron HTML5 MFD app) can manage the set without touching this
file. Two equivalent control surfaces, both persisting to the same settings:

    com.victronenergy.settings      /Settings/N2kBatteries/<key>/Enabled ...
    com.victronenergy.n2kbatteries  /Sources/<key>/Enabled ... plus /Catalog

Enabling or disabling a source takes effect immediately: the battery service is
created or torn down in place, no restart.

Source keys come from the wire, never from a list index:
    bat<n>   NMEA 2000 battery / DC instance <n>   (127508 + 127506)
    alt<n>   engine instance <n> alternator        (127489)

Baselines:
  https://github.com/victronenergy/velib_python   (dbusdummyservice.py)
  /opt/victronenergy/dbus-systemcalc-py           (battery service paths)
"""

import configparser
import glob
import json
import logging
import os
import platform
import socket
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "1.2.0"
BUSITEM = "com.victronenergy.BusItem"
SETTINGS_SVC = "com.victronenergy.settings"
SETTINGS_IF = "com.victronenergy.Settings"

log = logging.getLogger("dbus-batteries")


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


# ============================================================================
# NMEA 2000
# ============================================================================
PGN_BATTERY_STATUS = 127508      # 0x1F214, single frame, ~1 Hz per instance
PGN_DC_DETAILED = 127506         # 0x1F212, fast packet, ~0.5 Hz per instance
PGN_ENGINE_DYNAMIC = 127489      # 0x1F201, fast packet, ~2 Hz per engine

# N2K reserved codes: the top two values of every field mean "out of range"
# and "not available". Treating 0xFFFF as a reading is how a dead sender ends
# up displayed as 655.35 V.
U16_ERR = 0xFFFE
S16_ERR = 32766

DC_TYPE_BATTERY = 0


def pgn_of(cid):
    """29-bit CAN id -> PGN. All three PGNs here are PDU2 (PF >= 240)."""
    pf = (cid >> 16) & 0xFF
    return (cid >> 8) & 0x3FF00 if pf < 240 else (cid >> 8) & 0x3FFFF


def _u16(buf, off):
    v = buf[off] | (buf[off + 1] << 8)
    return None if v >= U16_ERR else v


def _s16(buf, off):
    v = buf[off] | (buf[off + 1] << 8)
    if v >= 0x8000:
        v -= 0x10000
    return None if v >= S16_ERR else v


def _u8(buf, off):
    v = buf[off]
    return None if v >= 0xFE else v


def decode_battery_status(data):
    """PGN 127508 -> dict. Layout: instance | V (0.01 V, u16) |
    I (0.1 A, s16) | temperature (0.01 K, u16) | SID.

    Verified against captures/mfd-boot.log:
        15F21410 # 01 8C 05 03 00 FF FF FF  -> instance 1, 14.20 V, +0.3 A
        15F21410 # 02 64 05 FF 7F FF FF FF  -> instance 2, 13.80 V, no current
    """
    if len(data) < 7:
        return None
    volt = _u16(data, 1)
    cur = _s16(data, 3)
    temp = _u16(data, 5)
    return {
        "instance": data[0],
        "voltage": None if volt is None else volt * 0.01,
        "current": None if cur is None else cur * 0.1,
        "temperature": None if temp is None else temp * 0.01 - 273.15,
    }


def decode_dc_detailed(payload):
    """PGN 127506 -> dict. Layout: SID | instance | DC type | SOC % | SOH % |
    time remaining (min, u16) | ripple (0.01 V, u16).

    Verified against captures/mfd-boot.log (reassembled fast packet):
        00 01 00 64 64 00 00 00 00 -> instance 1, battery, SOC 100 %, SOH 100 %
    """
    if len(payload) < 7:
        return None
    soc = _u8(payload, 3)
    soh = _u8(payload, 4)
    ttg = _u16(payload, 5)
    return {
        "instance": payload[1],
        "dctype": payload[2],
        # SOC/SOH are 1 %/bit; above 100 is a broken sender, not data
        "soc": soc if soc is not None and soc <= 100 else None,
        "soh": soh if soh is not None and soh <= 100 else None,
        # This monitor sends 0 minutes when it has nothing to estimate (seen
        # at SOC 100 %). That is "no estimate", not "empty now".
        "timetogo": ttg * 60 if ttg else None,
    }


def decode_engine_dynamic(payload):
    """PGN 127489 -> dict. Only the alternator potential is used: the engine
    batteries are not on the N2K battery monitor, and the Yanmar gateways send
    this at ~2 Hz whether or not the engine is running.

    Layout: instance | oil P | oil T | coolant T | alternator V (0.01 V, s16).
    """
    if len(payload) < 9:
        return None
    alt = _s16(payload, 7)
    return {
        "instance": payload[0],
        "voltage": None if alt is None else alt * 0.01,
    }


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
def _kv_map(raw, cast=str):
    """'bat1: House, alt0: Port Engine' -> {'bat1': 'House', ...}.

    Values may contain spaces; only the first colon splits.
    """
    out = {}
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        k, v = item.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        try:
            out[k] = cast(v)
        except ValueError:
            log.warning("config: ignoring unparseable entry %r", item)
    return out


def _key_list(raw):
    return [t for t in str(raw or "").replace(",", " ").split() if t]


def _flag(raw, default=False):
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)

        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        c = cp["can"] if cp.has_section("can") else {}
        self.can_iface = c.get("interface", "can0")
        self.can_reconnect_s = int(c.get("reconnect_delay_s", 5))

        p = cp["publish"] if cp.has_section("publish") else {}
        self.publish_ms = int(p.get("rate_ms", 1000))
        self.stale_after_s = float(p.get("stale_after_s", 10))
        self.unpublish_after_s = float(p.get("unpublish_after_s", 300))
        self.product_name = p.get("product_name", "N2K battery")

        s = cp["sources"] if cp.has_section("sources") else {}
        self.enabled_default = _key_list(s.get("enabled", ""))
        self.instances = _kv_map(s.get("instances", ""), int)
        self.names = _kv_map(s.get("names", ""))
        self.suffixes = _kv_map(s.get("suffixes", ""))
        self.capacities = _kv_map(s.get("capacities", ""), int)
        self.auto_enable_new = _flag(s.get("auto_enable_new"), False)
        self.max_sources = int(s.get("max_sources", 32))
        lo, _, hi = str(s.get("instance_pool", "206-209")).partition("-")
        self.pool_lo = int(lo)
        self.pool_hi = int(hi or lo)
        self.settings_prefix = s.get("settings_prefix", "n2kbat_")

        m = cp["manager"] if cp.has_section("manager") else {}
        self.manager_enabled = _flag(m.get("enabled"), True)
        self.manager_service = m.get("service", "com.victronenergy.n2kbatteries")
        self.settings_group = m.get("settings_group", "N2kBatteries")
        self.reconcile_s = int(m.get("reconcile_s", 30))


# ============================================================================
# D-Bus helpers (same pattern as dbus-recbms / dbus-czone)
# ============================================================================
def private_bus():
    # Each VeDbusService needs its own connection: every service exports a
    # root ("/") item, which collides on a shared connection.
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus(private=True)
    return dbus.SystemBus(private=True)


def shared_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


def new_service(name, bus):
    """VeDbusService across velib versions: prefer deferred registration.
    Feature-detect instead of try/except — a half-constructed VeDbusService
    logs AttributeError noise from its __del__ on older velib."""
    import inspect
    if "register" in inspect.signature(VeDbusService.__init__).parameters:
        svc = VeDbusService(name, bus=bus, register=False)
        svc._n2kbat_needs_register = True
    else:
        svc = VeDbusService(name, bus=bus)
        svc._n2kbat_needs_register = False
    return svc


def register_service(svc):
    if getattr(svc, "_n2kbat_needs_register", False):
        svc.register()


def drop_service(svc, bus):
    """Take a service off the bus. Closing the private connection is what
    actually releases the well-known name; __del__ is called first, while the
    connection is still alive, so velib can tidy its own objects quietly."""
    try:
        if hasattr(svc, "__del__"):
            svc.__del__()
    except Exception:
        pass
    try:
        bus.close()
    except Exception:
        pass


def fmt(unit, digits):
    def cb(path, value):
        if value is None:
            return "---"
        return ("%." + str(digits) + "f%s") % (float(value), unit)
    return cb


class Settings:
    """Thin localsettings client.

    SettingsDevice wants its whole path list at construction time, but the set
    of sources is discovered from the bus, so paths are created as they are
    found. AddSetting / GetValue / SetValue are the stable Venus API underneath
    it and are all this needs.
    """

    def __init__(self, bus, group):
        self.bus = bus
        self.group = group.strip("/")

    def path(self, name):
        return "/Settings/%s/%s" % (self.group, name.strip("/"))

    def add(self, name, default, itemtype, minimum=0, maximum=0):
        """Create the setting if absent (never clobbers), return its value."""
        name = name.strip("/")
        group, _, leaf = ("%s/%s" % (self.group, name)).rpartition("/")
        try:
            self.bus.call_blocking(
                SETTINGS_SVC, "/Settings", SETTINGS_IF, "AddSetting",
                "ssvsii", [group, leaf, default, itemtype, minimum, maximum],
                timeout=10)
        except Exception as e:
            log.warning("AddSetting %s failed: %s", self.path(name), e)
        return self.get(name, default)

    def get(self, name, fallback=None):
        try:
            return self.bus.call_blocking(SETTINGS_SVC, self.path(name), BUSITEM,
                                          "GetValue", "", [], timeout=10)
        except Exception:
            return fallback

    def set(self, name, value):
        try:
            self.bus.call_blocking(SETTINGS_SVC, self.path(name), BUSITEM,
                                   "SetValue", "v", [value], timeout=10)
            return True
        except Exception as e:
            log.warning("SetValue %s failed: %s", self.path(name), e)
            return False


# ============================================================================
# One battery source
# ============================================================================
class Source:
    """A battery the N2K bus can tell us about, forwarded or not."""

    KIND_DC = "dc"                # 127508 + 127506 (the SiCOM monitor)
    KIND_ALT = "alternator"       # 127489 alternator potential only

    stale_after_s = 10.0          # set from config at startup

    def __init__(self, key, kind, n2k_instance):
        self.key = key
        self.kind = kind
        self.n2k_instance = n2k_instance
        self.src_addr = None

        # live measurements
        self.voltage = None
        self.current = None
        self.temperature = None
        self.soc = None
        self.soh = None
        self.timetogo = None
        self.last_seen = 0.0
        self.fields = set()       # which quantities this source has ever sent

        # configuration (persisted in localsettings; config.ini only seeds it)
        self.enabled = False
        self.instance = None
        self.name = key
        self.suffix = key
        self.capacity = 0
        self.settings_created = False

        # publication
        self.svc = None
        self.bus = None
        self.service_name = None
        self.paths = set()
        self.connected = None

    @property
    def fresh(self):
        return (self.last_seen > 0 and
                (time.time() - self.last_seen) <= self.stale_after_s)

    def forget(self):
        """Drop the measurements, keep the identity.

        Blanking the D-Bus paths is not enough: if this source comes back
        sending only SOME of its PGNs — 127508 without 127506, say — the
        untouched fields would republish a reading from before the outage.
        """
        self.voltage = self.current = self.temperature = None
        self.soc = self.soh = self.timetogo = None

    def age(self):
        return None if not self.last_seen else round(time.time() - self.last_seen, 1)

    def describe(self):
        """The stable half of the catalog — no timestamps, so it can be
        persisted to localsettings without a write every second."""
        return {
            "key": self.key,
            "kind": self.kind,
            "n2k": self.n2k_instance,
            "src": self.src_addr,
            "fields": sorted(self.fields),
            "enabled": 1 if self.enabled else 0,
            "instance": self.instance,
            "name": self.name,
            "suffix": self.suffix,
            "capacity": self.capacity,
            "published": 1 if self.svc is not None else 0,
        }


# ============================================================================
# The driver
# ============================================================================
class BatteryDriver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.start_ts = time.time()
        self.sbus = shared_bus()
        self.settings = Settings(self.sbus, cfg.settings_group)
        self.sources = {}          # key -> Source
        self.fp = FastPacket()
        self.manager = None
        self.manager_bus = None
        self.manager_paths = set()
        self._sock = None
        self._watch = None
        self._catalog_json = None
        self._cap_logged = False

        Source.stale_after_s = cfg.stale_after_s

        self._open_can(blocking=False)
        if self._sock is None:
            raise SystemExit("cannot open %s" % cfg.can_iface)

        self._load_catalog()
        self._seed_from_config()
        self._init_manager()

        for src in sorted(self.sources.values(), key=lambda x: x.key):
            self._ensure_pinned(src)
            self._apply_enabled(src)
        self._refresh_manager()

        self._watch = GLib.io_add_watch(
            self._sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
            self._can_readable)
        GLib.timeout_add(cfg.publish_ms, self._tick)
        GLib.timeout_add_seconds(cfg.reconcile_s, self._reconcile)
        self._watch_settings()

    # ------------------------------------------------------------------ CAN
    def _open_can(self, blocking=False):
        c = self.cfg
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            # CAN_EFF_FLAG is a NEGATIVE int on 32-bit platforms (armv7l) —
            # mask to u32 or struct 'I' rejects it.
            eff = socket.CAN_EFF_FLAG & 0xFFFFFFFF
            flt = b""
            for pgn in (PGN_BATTERY_STATUS, PGN_DC_DETAILED, PGN_ENGINE_DYNAMIC):
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
        log.info("listening on %s for PGN %d, %d and %d", c.can_iface,
                 PGN_BATTERY_STATUS, PGN_DC_DETAILED, PGN_ENGINE_DYNAMIC)

    def _reopen_can(self):
        self._open_can()
        if self._sock is not None:
            self._watch = GLib.io_add_watch(
                self._sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self._can_readable)
        return False

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

    @staticmethod
    def _parse_frame(frame):
        cid, dlc = struct.unpack_from("=IB", frame)
        cid &= socket.CAN_EFF_MASK
        return cid, frame[8:8 + min(dlc, 8)]

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
                self._on_frame(pgn_of(cid), cid & 0xFF, data)
        except BlockingIOError:
            pass
        except OSError as e:
            log.error("CAN read failed (%s), reopening in %ds",
                      e, self.cfg.can_reconnect_s)
            self._close_can(remove_watch=False)
            GLib.timeout_add_seconds(self.cfg.can_reconnect_s, self._reopen_can)
            return False
        return True

    def _on_frame(self, pgn, src_addr, data):
        now = time.time()
        if pgn == PGN_BATTERY_STATUS:
            d = decode_battery_status(data)
            if d is None:
                return
            s = self._source("bat%d" % d["instance"], Source.KIND_DC, d["instance"])
            s.src_addr = src_addr
            for f in ("voltage", "current", "temperature"):
                if d[f] is not None:
                    setattr(s, f, d[f])
                    s.fields.add(f)
            s.last_seen = now
            return

        payload = self.fp.push((pgn, src_addr), data)
        if payload is None:
            return

        if pgn == PGN_DC_DETAILED:
            d = decode_dc_detailed(payload)
            if d is None or d["dctype"] != DC_TYPE_BATTERY:
                return
            s = self._source("bat%d" % d["instance"], Source.KIND_DC, d["instance"])
            s.src_addr = src_addr
            for f in ("soc", "soh", "timetogo"):
                if d[f] is not None:
                    setattr(s, f, d[f])
                    s.fields.add(f)
            s.last_seen = now

        elif pgn == PGN_ENGINE_DYNAMIC:
            d = decode_engine_dynamic(payload)
            if d is None or d["voltage"] is None:
                return
            s = self._source("alt%d" % d["instance"], Source.KIND_ALT, d["instance"])
            s.src_addr = src_addr
            s.voltage = d["voltage"]
            s.fields.add("voltage")
            s.last_seen = now

    # -------------------------------------------------------------- sources
    def _source(self, key, kind, n2k_instance):
        s = self.sources.get(key)
        if s is not None:
            return s
        # Discovery is driven by an instance byte off the wire, so a garbled
        # frame that survives the kernel filter would mint a source — and its
        # settings, and its catalog entry — every time it arrives. Cap it.
        if len(self.sources) >= self.cfg.max_sources:
            if not self._cap_logged:
                self._cap_logged = True
                log.error("refusing to catalogue more than %d sources; %s and "
                          "anything after it is ignored. Raise max_sources in "
                          "config.ini if the bus really has that many.",
                          self.cfg.max_sources, key)
            return Source(key, kind, n2k_instance)     # scratch, not retained
        s = Source(key, kind, n2k_instance)
        self.sources[key] = s
        self._load_source_settings(s)
        log.info("new source %s (%s, N2K instance %d): %s", key, kind, n2k_instance,
                 "forwarding as device instance %s" % s.instance if s.enabled
                 else "not forwarded — enable via %s"
                      % self.settings.path("%s/Enabled" % key))
        self._add_manager_paths(s)
        self._ensure_pinned(s)
        self._apply_enabled(s)
        return s

    def _seed_from_config(self):
        """Sources named in config.ini exist from boot even on a silent bus.

        A battery configured to be forwarded should show up in VRM as a
        disconnected device rather than vanish, and the HTML5 app needs the
        catalog to list it before the engines are switched on.
        """
        keys = list(self.cfg.enabled_default)
        for extra in (self.cfg.instances, self.cfg.names,
                      self.cfg.suffixes, self.cfg.capacities):
            for k in extra:
                if k not in keys:
                    keys.append(k)
        for key in keys:
            if key in self.sources:
                continue
            kind, n2k = self._parse_key(key)
            if kind is None:
                log.warning("config: %r is not a valid source key "
                            "(expected bat<n> or alt<n>)", key)
                continue
            s = Source(key, kind, n2k)
            self.sources[key] = s
            self._load_source_settings(s)

    @staticmethod
    def _parse_key(key):
        for prefix, kind in (("bat", Source.KIND_DC), ("alt", Source.KIND_ALT)):
            if key.startswith(prefix) and key[len(prefix):].isdigit():
                return kind, int(key[len(prefix):])
        return None, None

    # ------------------------------------------------------------- settings
    def _load_source_settings(self, s):
        """Create this source's settings on first sight, then read them back.

        config.ini values are FIRST-RUN defaults only: once a setting exists,
        whatever the user or the app last chose wins — the same contract as
        dbus-czone's per-output Type.
        """
        c = self.cfg
        if not s.settings_created:
            defaults = [
                ("Enabled", 1 if (s.key in c.enabled_default or c.auto_enable_new)
                 else 0, "i", 0, 1),
                ("CustomName", c.names.get(s.key, s.key), "s", 0, 0),
                ("ServiceSuffix", c.suffixes.get(s.key, s.key), "s", 0, 0),
                ("Capacity", c.capacities.get(s.key, 0), "i", 0, 100000),
                ("Instance", c.instances.get(s.key, 0), "i", 0, 255),
            ]
            for name, default, itype, lo, hi in defaults:
                self.settings.add("%s/%s" % (s.key, name), default, itype, lo, hi)
            s.settings_created = True

        s.enabled = bool(int(self.settings.get("%s/Enabled" % s.key, 0) or 0))
        s.name = str(self.settings.get("%s/CustomName" % s.key, s.key))
        s.suffix = str(self.settings.get("%s/ServiceSuffix" % s.key, s.key))
        s.capacity = int(self.settings.get("%s/Capacity" % s.key, 0) or 0)
        inst = int(self.settings.get("%s/Instance" % s.key, 0) or 0)
        if inst == 0:
            # Reuse what this source already holds rather than drawing again:
            # a settings write that silently failed would otherwise consume a
            # pool number on every reconcile.
            inst = s.instance or self._allocate_instance()
            if inst:
                self.settings.set("%s/Instance" % s.key, inst)
        s.instance = inst or None

    def _allocate_instance(self):
        """Next never-used number from the spare pool.

        Monotonic on purpose: the never-reuse rule exists because Signal K and
        VRM key their history off the instance, so a recycled number silently
        merges two different batteries' histories.
        """
        c = self.cfg
        nxt = int(self.settings.add("NextInstance", c.pool_lo, "i", 0, 255)
                  or c.pool_lo)
        nxt = max(nxt, c.pool_lo)
        if nxt > c.pool_hi:
            log.error("instance pool %d-%d exhausted; assign one by hand at %s",
                      c.pool_lo, c.pool_hi, self.settings.path("<key>/Instance"))
            return 0
        self.settings.set("NextInstance", nxt + 1)
        return nxt

    def _watch_settings(self):
        """React immediately when something else writes our settings.

        The manager service's own paths short-circuit to the callbacks below,
        so this is for a client that writes localsettings directly — the Venus
        MQTT bridge exposes settings, so that is the path of least resistance
        for a web app. The periodic reconcile is the backstop.
        """
        prefix = "/Settings/%s/" % self.cfg.settings_group

        def handler(_changes, path=None, **_kw):
            if path and str(path).startswith(prefix):
                GLib.idle_add(self._reconcile_once)

        try:
            self.sbus.add_signal_receiver(
                handler, dbus_interface=BUSITEM, signal_name="PropertiesChanged",
                bus_name=SETTINGS_SVC, path_keyword="path")
        except Exception as e:
            log.warning("cannot watch settings changes (%s); relying on the "
                        "%ds reconcile", e, self.cfg.reconcile_s)

    def _reconcile_once(self):
        self._reconcile()
        return False

    def _reconcile(self):
        """Re-read every source's settings and apply whatever changed."""
        for s in list(self.sources.values()):
            before = (s.enabled, s.instance, s.name, s.suffix, s.capacity)
            self._load_source_settings(s)
            after = (s.enabled, s.instance, s.name, s.suffix, s.capacity)
            if after[:2] != before[:2] or s.suffix != before[3]:
                self._apply_enabled(s, restart=True)
            elif s.svc is not None:
                if s.name != before[2]:
                    s.svc["/CustomName"] = s.name
                if s.capacity != before[4]:
                    self._publish(s, force=True)
        self._refresh_manager()
        return True

    # ------------------------------------------------ battery service (per source)
    def _should_publish(self, s):
        """Enabled is the user's standing choice; presence is the bus's answer.

        A battery is only on D-Bus while its data is actually arriving. A
        source that has never been seen, or has been silent for
        `unpublish_after_s`, is dropped from D-Bus entirely rather than left in
        VRM as a permanently disconnected device — the engine batteries are
        invisible whenever the Yanmar ignition is off, which is most of the
        time. It keeps its settings, its instance and its catalog entry, and
        comes straight back on the next frame.

        `unpublish_after_s = 0` restores the old always-published behaviour.
        """
        if not s.enabled or not s.instance:
            return False
        if self.cfg.unpublish_after_s <= 0:
            return True
        if not s.last_seen:
            return False
        return (time.time() - s.last_seen) <= self.cfg.unpublish_after_s

    def _apply_enabled(self, s, restart=False):
        want = self._should_publish(s)
        if want and (s.svc is None or restart):
            had = s.svc is not None
            self._stop_service(s)
            self._start_service(s)
            if not had:
                log.info("%s: back on the bus", s.key)
        elif not want and s.svc is not None:
            self._stop_service(s)
            log.info("%s: %s", s.key,
                     "stopped forwarding" if not s.enabled or not s.instance
                     else "silent for %.0fs — removed from D-Bus"
                          % self.cfg.unpublish_after_s)
        elif s.enabled and not s.instance:
            log.error("%s: enabled but has no device instance", s.key)

    def _ensure_pinned(self, s):
        """Reserve this source's device instance in localsettings.

        Called for every ENABLED source, whether or not it is on the bus right
        now. A source that is enabled but silent (the engine batteries with the
        ignition off) still owns its number — otherwise localsettings is free
        to hand it to the next device that asks, and the battery comes back on
        a different instance with VRM history split in two.
        """
        if not s.enabled or not s.instance:
            return
        settings_id = self.cfg.settings_prefix + s.suffix
        granted = self._claim_instance(settings_id, s.instance)
        if granted != s.instance:
            s.instance = granted
            self.settings.set("%s/Instance" % s.key, granted)

    def _start_service(self, s):
        c = self.cfg
        settings_id = c.settings_prefix + s.suffix
        instance = self._claim_instance(settings_id, s.instance)
        name = "com.victronenergy.battery.%s" % settings_id
        try:
            bus = private_bus()
            svc = new_service(name, bus)
            self._build_battery_paths(svc, s, instance)
            register_service(svc)
        except Exception as e:
            log.error("%s: cannot publish %s (%s)", s.key, name, e)
            try:
                bus.close()
            except Exception:
                pass
            return

        s.bus, s.svc, s.service_name = bus, svc, name
        s.connected = None
        if instance != s.instance:
            s.instance = instance
            self.settings.set("%s/Instance" % s.key, instance)
        log.info("%s: forwarding as %s (instance %d, %r)",
                 s.key, name, instance, s.name)
        self._publish(s, force=True)

    def _build_battery_paths(self, svc, s, instance):
        c = self.cfg
        s.paths = set()

        def add(path, value, **kw):
            svc.add_path(path, value, **kw)
            s.paths.add(path)

        add("/Mgmt/ProcessName", __file__)
        add("/Mgmt/ProcessVersion",
            "%s on Python %s" % (VERSION, platform.python_version()))
        add("/Mgmt/Connection",
            "%s N2K %s instance %d" % (
                c.can_iface,
                "engine" if s.kind == Source.KIND_ALT else "battery",
                s.n2k_instance))
        add("/DeviceInstance", instance)
        add("/ProductId", 0xFFFF)
        add("/ProductName", c.product_name)
        add("/CustomName", s.name, writeable=True,
            onchangecallback=self._make_name_cb(s))
        add("/FirmwareVersion", None)
        add("/HardwareVersion", None)
        add("/Serial", "%s%d" % (s.kind[:3], s.n2k_instance))
        add("/Connected", 0)

        # Only the paths this source can actually fill. The engine batteries
        # are voltage-only (PGN 127489 carries no current and no SOC), and a
        # never-updated /Soc showing a plausible number is worse than no /Soc
        # at all — that is why the flow set default_values:false for them.
        add("/Dc/0/Voltage", None, gettextcallback=fmt("V", 2))
        if s.kind == Source.KIND_DC:
            add("/Dc/0/Current", None, gettextcallback=fmt("A", 1))
            add("/Dc/0/Power", None, gettextcallback=fmt("W", 0))
            add("/Dc/0/Temperature", None, gettextcallback=fmt("°C", 1))
            add("/Soc", None, gettextcallback=fmt("%", 0))
            add("/Soh", None, gettextcallback=fmt("%", 0))
            add("/TimeToGo", None)
            add("/InstalledCapacity", s.capacity or None,
                gettextcallback=fmt("Ah", 0))
            add("/Capacity", None, gettextcallback=fmt("Ah", 1))
            add("/ConsumedAmphours", None, gettextcallback=fmt("Ah", 1))

        # Where this device came from, for a UI that wants to explain itself
        add("/N2k/SourceKey", s.key)
        add("/N2k/Instance", s.n2k_instance)
        add("/N2k/SourceAddress", s.src_addr)

    def _stop_service(self, s):
        if s.svc is None:
            return
        drop_service(s.svc, s.bus)
        s.svc = s.bus = s.service_name = None
        s.paths = set()
        s.connected = None

    def _claim_instance(self, settings_id, wanted):
        """Pin /Settings/Devices/<id>/ClassAndVrmInstance, reconverging if
        localsettings granted something else (registry-style self-heal).

        The settings id deliberately has no `virtual_` prefix so the Node-RED
        palette's auto-cleanup never touches it.
        """
        path = "/Settings/Devices/%s/ClassAndVrmInstance" % settings_id
        try:
            self.sbus.call_blocking(
                SETTINGS_SVC, "/Settings", SETTINGS_IF, "AddSetting", "ssvsii",
                ["Devices/%s" % settings_id, "ClassAndVrmInstance",
                 "battery:%d" % wanted, "s", 0, 0], timeout=10)
        except Exception as e:
            log.warning("%s: AddSetting failed (%s)", settings_id, e)
        granted = wanted
        try:
            granted = int(str(self.sbus.call_blocking(
                SETTINGS_SVC, path, BUSITEM, "GetValue", "", [],
                timeout=10)).split(":")[1])
        except Exception:
            pass
        if granted == wanted:
            return granted
        if self._instance_in_use(wanted):
            log.warning("%s: instance %d is held by a live battery service; "
                        "using %d instead. Retire the old Node-RED device and "
                        "remove its /Settings/Devices/virtual_* entry "
                        "(see migrate.sh), then restart this driver.",
                        settings_id, wanted, granted)
            return granted
        try:
            self.sbus.call_blocking(SETTINGS_SVC, path, BUSITEM, "SetValue", "v",
                                    ["battery:%d" % wanted], timeout=10)
        except Exception as e:
            log.warning("%s: could not pin instance %d (%s); using %d",
                        settings_id, wanted, e, granted)
            return granted
        # SetValue reports success even when localsettings refuses the value:
        # it will not let two /Settings/Devices entries claim one instance, and
        # quietly keeps the old number. Re-read, or we publish an instance that
        # localsettings does not agree we own. (Seen on the boat 2026-08-21
        # with the retired Node-RED virtual_* entries still in place.)
        after = wanted
        try:
            after = int(str(self.sbus.call_blocking(
                SETTINGS_SVC, path, BUSITEM, "GetValue", "", [],
                timeout=10)).split(":")[1])
        except Exception:
            pass
        if after != wanted:
            log.warning("%s: localsettings refused instance %d and kept %d — "
                        "something else still claims it. Look for a stale "
                        "/Settings/Devices/* entry (see migrate.sh).",
                        settings_id, wanted, after)
            return after
        log.info("%s: reconverged instance %d -> %d",
                 settings_id, granted, wanted)
        return wanted

    def _instance_in_use(self, wanted):
        mine = {s.service_name for s in self.sources.values() if s.service_name}
        for name in self.sbus.list_names():
            name = str(name)
            if not name.startswith("com.victronenergy.battery.") or name in mine:
                continue
            try:
                di = self.sbus.call_blocking(name, "/DeviceInstance", BUSITEM,
                                             "GetValue", "", [], timeout=2)
            except Exception:
                continue
            try:
                if int(di) == wanted:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _make_name_cb(self, s):
        def cb(_path, value):
            s.name = str(value)
            self.settings.set("%s/CustomName" % s.key, s.name)
            return True
        return cb

    # ------------------------------------------------------------- publish
    def _tick(self):
        for s in self.sources.values():
            # presence first: this is what adds a battery that has started
            # talking and removes one that has gone quiet
            if self._should_publish(s) != (s.svc is not None):
                self._apply_enabled(s)
            self._publish(s)
        self._refresh_manager()
        return True

    def _publish(self, s, force=False):
        if s.svc is None:
            return
        fresh = s.fresh
        if s.connected != fresh or force:
            if s.connected and not fresh:
                log.warning("%s: no N2K data for %.0fs — disconnected",
                            s.key, time.time() - s.last_seen)
                s.forget()
            s.connected = fresh
            s.svc["/Connected"] = 1 if fresh else 0

        # Installed capacity is configuration, not a measurement: it stays
        # visible while the battery is offline, and it must track a change made
        # through the settings without waiting for the source to come back.
        installed = s.capacity or None
        if "/InstalledCapacity" in s.paths:
            s.svc["/InstalledCapacity"] = installed

        if not fresh:
            # blank rather than freeze: a stale voltage on a start battery
            # reads exactly like a healthy one
            for p in ("/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                      "/Dc/0/Temperature", "/Soc", "/Soh", "/TimeToGo",
                      "/Capacity", "/ConsumedAmphours"):
                if p in s.paths:
                    s.svc[p] = None
            return

        s.svc["/Dc/0/Voltage"] = None if s.voltage is None else round(s.voltage, 2)
        if s.kind != Source.KIND_DC:
            return

        s.svc["/Dc/0/Current"] = None if s.current is None else round(s.current, 1)
        s.svc["/Dc/0/Power"] = (None if s.voltage is None or s.current is None
                                else round(s.voltage * s.current, 1))
        s.svc["/Dc/0/Temperature"] = (None if s.temperature is None
                                      else round(s.temperature, 1))
        s.svc["/Soc"] = s.soc
        s.svc["/Soh"] = s.soh
        s.svc["/TimeToGo"] = s.timetogo

        if installed and s.soc is not None:
            remaining = installed * s.soc / 100.0
            s.svc["/Capacity"] = round(remaining, 1)
            # BMV convention: consumed amp hours are negative
            s.svc["/ConsumedAmphours"] = -round(installed - remaining, 1)
        else:
            s.svc["/Capacity"] = None
            s.svc["/ConsumedAmphours"] = None

    # ------------------------------------------------------------- manager
    def _init_manager(self):
        c = self.cfg
        if not c.manager_enabled:
            return
        try:
            bus = private_bus()
            svc = new_service(c.manager_service, bus)
        except Exception as e:
            log.error("cannot create %s (%s); localsettings remains the "
                      "control surface", c.manager_service, e)
            return

        def add(path, value, **kw):
            svc.add_path(path, value, **kw)
            self.manager_paths.add(path)

        add("/Mgmt/ProcessName", __file__)
        add("/Mgmt/ProcessVersion",
            "%s on Python %s" % (VERSION, platform.python_version()))
        add("/Mgmt/Connection", c.can_iface)
        add("/DeviceInstance", 0)
        add("/ProductId", 0xFFFF)
        add("/ProductName", "N2K battery forwarder")
        add("/Connected", 1)
        add("/SourceCount", 0)
        add("/EnabledCount", 0)
        # The whole catalog in one string, for a client that would rather read
        # once than subscribe to a tree whose shape it does not know yet.
        add("/Catalog", "[]")
        add("/SettingsGroup", "/Settings/%s" % c.settings_group)

        self.manager, self.manager_bus = svc, bus
        for s in sorted(self.sources.values(), key=lambda x: x.key):
            self._add_manager_paths(s)
        try:
            register_service(svc)
        except Exception as e:
            log.error("cannot register %s (%s)", c.manager_service, e)
            self.manager = None
            drop_service(svc, bus)
            return
        log.info("registered %s (%d source(s))", c.manager_service, len(self.sources))

    def _add_manager_paths(self, s):
        svc = self.manager
        base = "/Sources/%s" % s.key
        if svc is None or base + "/Enabled" in self.manager_paths:
            return

        def add(leaf, value, **kw):
            svc.add_path(base + leaf, value, **kw)
            self.manager_paths.add(base + leaf)

        add("/Enabled", 1 if s.enabled else 0, writeable=True,
            onchangecallback=self._make_enable_cb(s))
        add("/Kind", s.kind)
        add("/N2kInstance", s.n2k_instance)
        add("/SourceAddress", s.src_addr)
        add("/Available", 0)
        add("/Age", None)
        add("/Published", 0)
        add("/Fields", ",".join(sorted(s.fields)))
        add("/DeviceInstance", s.instance or 0, writeable=True,
            onchangecallback=self._make_setting_cb(s, "Instance", int))
        add("/CustomName", s.name, writeable=True,
            onchangecallback=self._make_setting_cb(s, "CustomName", str))
        add("/Capacity", s.capacity, writeable=True,
            onchangecallback=self._make_setting_cb(s, "Capacity", int))

    def _make_enable_cb(self, s):
        def cb(_path, value):
            try:
                want = bool(int(value))
            except (TypeError, ValueError):
                return False
            if want != s.enabled:
                self.settings.set("%s/Enabled" % s.key, 1 if want else 0)
                s.enabled = want
                log.info("%s: %s by D-Bus write", s.key,
                         "enabled" if want else "disabled")
                if want:
                    self._ensure_pinned(s)
                GLib.idle_add(self._apply_enabled_idle, s)
            return True
        return cb

    def _apply_enabled_idle(self, s):
        # Deferred: tearing a VeDbusService down from inside a D-Bus callback
        # would close the connection the pending reply still needs.
        self._apply_enabled(s)
        self._refresh_manager()
        return False

    def _make_setting_cb(self, s, setting, cast):
        def cb(_path, value):
            try:
                v = cast(value)
            except (TypeError, ValueError):
                return False
            if setting == "Instance" and v and v != s.instance \
                    and self._instance_in_use(v):
                log.warning("%s: device instance %d is already in use", s.key, v)
                return False
            self.settings.set("%s/%s" % (s.key, setting), v)
            GLib.idle_add(self._reconcile_once)
            return True
        return cb

    def _refresh_manager(self):
        blob = json.dumps([s.describe() for s in
                           sorted(self.sources.values(), key=lambda x: x.key)],
                          separators=(",", ":"))
        if blob != self._catalog_json:
            # Only the stable description is persisted, so liveness (which
            # changes every tick) never turns into a localsettings write.
            self._catalog_json = blob
            self.settings.set("Catalog", blob)

        svc = self.manager
        if svc is None:
            return
        if svc["/Catalog"] != blob:
            svc["/Catalog"] = blob
        svc["/SourceCount"] = len(self.sources)
        svc["/EnabledCount"] = sum(1 for s in self.sources.values() if s.enabled)
        for s in self.sources.values():
            base = "/Sources/%s" % s.key
            if base + "/Enabled" not in self.manager_paths:
                continue
            svc[base + "/Enabled"] = 1 if s.enabled else 0
            svc[base + "/Available"] = 1 if s.fresh else 0
            svc[base + "/Published"] = 1 if s.svc is not None else 0
            svc[base + "/Age"] = s.age()
            svc[base + "/SourceAddress"] = s.src_addr
            svc[base + "/Fields"] = ",".join(sorted(s.fields))
            svc[base + "/DeviceInstance"] = s.instance or 0
            svc[base + "/CustomName"] = s.name
            svc[base + "/Capacity"] = s.capacity

    def _load_catalog(self):
        """Re-create last run's sources before a single frame arrives, so a
        battery whose bus is quiet at boot still shows up (disconnected)
        instead of disappearing from VRM and from the app's list."""
        raw = self.settings.add("Catalog", "[]", "s")
        try:
            entries = json.loads(str(raw or "[]"))
        except ValueError:
            log.warning("cached catalog unreadable; starting empty")
            return
        for e in entries:
            key = e.get("key")
            if not key or key in self.sources:
                continue
            kind, n2k = self._parse_key(key)
            if kind is None:
                continue
            s = Source(key, kind, n2k)
            s.fields = set(e.get("fields") or [])
            s.src_addr = e.get("src")
            self.sources[key] = s
            self._load_source_settings(s)
        if entries:
            log.info("catalog: %d source(s) restored from settings", len(entries))


def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
    cfg = Config(cfg_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")
    log.info("dbus-batteries v%s starting (velib: %s)", VERSION, _VELIB_DIR)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    BatteryDriver(cfg)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
