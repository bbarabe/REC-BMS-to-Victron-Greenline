#!/usr/bin/env python3
"""
dbus-edrive — standalone Venus OS driver for the Greenline 6GK electric drives.

Replaces the Node-RED "Greenline E-Drive" flow (archive/GreenlineEDriveFlow.json).
Runs as a daemontools service, starts seconds after D-Bus at boot — independent
of Signal K / Node-RED — and is untouched by Node-RED deploys.

Strictly read-only: the driver never transmits, so it cannot influence the drive
system. The CAN socket is bound with a receive filter set and nothing is ever
written to it.

**Data path**: raw SocketCAN socket on `can0` (kernel filters, no `candump`
subprocess, no watchdog, no pkill, no chunked line parsing) -> per-drive state
-> two `com.victronenergy.motordrive.<suffix>` services.

Two frame families are needed, and the wire format is documented in
GreenlineFindings.md:

  * CANopen TPDOs, 11-bit ids, node 0x0A = PORT and 0x0B = STARBOARD.
    IEEE floats at full resolution, ~10 Hz.
        0x18x  status byte, 01 = running
        0x28x  f1 = MOSFET temp °C, f2 = drive (motor) temp °C
        0x38x  f1 = motor voltage V, f2 = phase current RMS A
        0x48x  f1 = motor TORQUE N·m (not power), f2 = motor RPM
  * J1939 / NMEA 2000, 29-bit. Quantized, but two quantities have no float
    source at all. Note the source-address inversion on the HCU frames:
    0x64 is STARBOARD and 0x65 is PORT.
        61451  byte1 − 125 = torque %
        61452  w2 = DC current (calibrated fit), w4 ÷ 20 = phase peak A
        61453  w0/w1/w2 × 0.03125 − 273 = MOSFET / drive / MCU-HCU temp °C
        65363  byte0 = instance, bytes1-2 LE ÷ 9.280 = throttle %
        127493 byte0 = instance, byte1 bits0-1 = gear

The Victron `motordrive` class has exactly nine data paths, so the temperature
mapping is forced: the MOSFET is what the coolant loop cools and feeds
/Coolant/Temperature, /Motor/Temperature is the drive/motor sensor, and
/Controller/Temperature is the MCU/HCU board that only 61453 w2 reports.

Everything the drives send that has no Victron path — torque, throttle, phase
currents, mechanical power, the raw status byte — is published under /EDrive/ on
the same service. In the flow that telemetry only reached a debug node; here it
is on D-Bus, so Signal K, MQTT and the HTML5 app can all read it.

Baselines:
  https://github.com/victronenergy/velib_python   (dbusdummyservice.py)
"""

import configparser
import glob
import logging
import math
import os
import platform
import socket
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "1.1.0"

log = logging.getLogger("dbus-edrive")


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

BUSITEM = "com.victronenergy.BusItem"
SETTINGS_SVC = "com.victronenergy.settings"


# ============================================================================
# Protocol
# ============================================================================
# CANopen TPDO function codes: the 11-bit id is (code | node),
# e.g. node 0x0A -> 0x18A / 0x28A / 0x38A / 0x48A
TPDO_STATUS = 0x180          # 0x18x
TPDO_TEMPS = 0x280           # 0x28x
TPDO_VOLTAGE = 0x380         # 0x38x
TPDO_TORQUE = 0x480          # 0x48x
TPDO_CODES = (TPDO_STATUS, TPDO_TEMPS, TPDO_VOLTAGE, TPDO_TORQUE)

PGN_HCU_TORQUE = 61451       # 0xF00B
PGN_HCU_CURRENT = 61452      # 0xF00C
PGN_HCU_TEMPS = 61453        # 0xF00D
PGN_THROTTLE = 65363         # 0xFF53, proprietary
PGN_TRANSMISSION = 127493    # 0x1F205

J1939_PERCENT_OFFSET = 125   # 61451 byte 1 is percent torque, offset −125

# Venus /Motor/Direction enum
DIR_NEUTRAL, DIR_REVERSE, DIR_FORWARD = 0, 1, 2
# 127493 gear field: 0 = forward, 1 = neutral, 2 = reverse, 3 = unavailable
GEAR_TO_DIRECTION = {0: DIR_FORWARD, 1: DIR_NEUTRAL, 2: DIR_REVERSE}

STATUS_RUNNING = 1


def pgn_of(cid):
    """29-bit CAN id -> PGN. Every PGN used here is PDU2 (PF >= 240)."""
    pf = (cid >> 16) & 0xFF
    return (cid >> 8) & 0x3FF00 if pf < 240 else (cid >> 8) & 0x3FFFF


def _u16(buf, off):
    return buf[off] | (buf[off + 1] << 8)


def _f32(buf, off):
    return struct.unpack_from("<f", buf, off)[0]


# ============================================================================
# Config
# ============================================================================
def _q(value, step):
    """Round to the nearest multiple of `step`; None passes through.

    Integer steps give ints (D-Bus 'i'), fractional ones a float rounded to
    the step's own number of decimals so 0.1 never comes back as
    0.30000000000000004.
    """
    if value is None:
        return None
    n = round(value / step) * step
    if float(step).is_integer():
        return int(round(n))
    return round(n, max(0, -int(math.floor(math.log10(step)))))


def _flag(raw, default=False):
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class DriveConfig:
    """One drive's identity on the three buses it appears on."""

    def __init__(self, key, section):
        self.key = key
        self.canopen_node = int(str(section.get("canopen_node", "0x0A")), 16)
        self.hcu_source = int(str(section.get("hcu_source", "0x65")), 16)
        self.n2k_instance = int(section.get("n2k_instance", 0))
        self.instance = int(section.get("instance", 210))
        self.suffix = section.get("service_suffix", key)
        self.settings_id = section.get("settings_id", "edrive_" + key)
        self.custom_name = section.get("custom_name", key.capitalize() + " E-Motor")


class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        self._cp = cp

        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        c = cp["can"] if cp.has_section("can") else {}
        self.can_iface = c.get("interface", "can0")
        self.can_reconnect_s = int(c.get("reconnect_delay_s", 5))

        p = cp["publish"] if cp.has_section("publish") else {}
        self.publish_ms = int(p.get("rate_ms", 1000))
        self.stale_after_s = float(p.get("stale_after_s", 6))
        self.product_name = p.get("product_name", "Greenline 6GK E-Drive")
        self.publish_extra = _flag(p.get("publish_extra"), True)
        self.rpm_deadband = float(p.get("rpm_deadband", 1.0))
        # Quantisation steps. A value is only put on D-Bus when it has moved
        # to a different step, so the second-decimal jitter of an idle drive
        # (61.83 V, 0.07 A, 0.02 Nm ...) no longer costs a bus signal a
        # second per path. Rounding is done here, once, at publish time; the
        # decoded floats stay exact for the fallbacks that derive from them.
        self.voltage_step = float(p.get("voltage_step", 0.1))
        self.current_step = float(p.get("current_step", 0.5))
        self.power_step = float(p.get("power_step", 10))
        self.temperature_step = float(p.get("temperature_step", 0.5))
        self.torque_step = float(p.get("torque_step", 0.5))
        self.throttle_step = float(p.get("throttle_step", 0.5))
        self.phase_current_step = float(p.get("phase_current_step", 1))
        # Standstill deadbands, same idea as rpm_deadband: a stopped drive
        # reports a few tenths of a Nm and an amp or so of noise, which the
        # steps above still let through as a change every tick.
        self.torque_deadband = float(p.get("torque_deadband", 1.0))
        self.current_deadband = float(p.get("current_deadband", 1.0))

        k = cp["calibration"] if cp.has_section("calibration") else {}
        # 61452 w2 -> motor DC current, fit r=0.9994 against the MFD's MOTOR
        # CURRENT readout. Sign flipped so positive = motoring (drawing from
        # the pack). See GreenlineFindings.md.
        self.dc_current_offset = float(k.get("dc_current_offset", 800.409))
        self.dc_current_slope = float(k.get("dc_current_slope", -0.024918))
        # 65363 bytes 1-2 -> throttle %, exact over the 0-8.1 % that was
        # exercised; full travel extrapolates to raw ~928 and is unverified.
        self.throttle_divisor = float(k.get("throttle_divisor", 9.280))
        self.phase_peak_divisor = float(k.get("phase_peak_divisor", 20.0))

        self.drives = []
        for name in cp.sections():
            if name.startswith("drive."):
                self.drives.append(DriveConfig(name.split(".", 1)[1], cp[name]))
        if not self.drives:
            raise SystemExit("config.ini defines no [drive.<key>] section")


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
        svc._edrive_needs_register = True
    else:
        svc = VeDbusService(name, bus=bus)
        svc._edrive_needs_register = False
    return svc


def register_service(svc):
    if getattr(svc, "_edrive_needs_register", False):
        svc.register()


def fmt(unit, digits):
    def cb(path, value):
        if value is None:
            return "---"
        return ("%." + str(digits) + "f%s") % (float(value), unit)
    return cb


DIRECTION_TEXT = {DIR_NEUTRAL: "Neutral", DIR_REVERSE: "Reverse",
                  DIR_FORWARD: "Forward"}


def fmt_direction(_path, value):
    return DIRECTION_TEXT.get(value, "---")


# ============================================================================
# One drive
# ============================================================================
class Drive:
    def __init__(self, dcfg):
        self.cfg = dcfg
        self.key = dcfg.key
        self.instance = dcfg.instance
        self.name = dcfg.custom_name
        self.svc = None
        self.bus = None
        self.connected = None
        self.last_seen = 0.0

        # CANopen TPDO floats
        self.status = None
        self.mosfet_temp = None
        self.motor_temp = None
        self.voltage = None
        self.phase_irms = None
        self.torque = None            # N·m
        self.rpm = None

        # J1939 / N2K
        self.torque_pct = None
        self.dc_current = None
        self.phase_ipk = None
        self.mosfet_temp_1c = None    # 61453 w0, quantized to 1 °C
        self.drive_temp_1c = None     # 61453 w1
        self.mcu_temp = None          # 61453 w2 — the only source for this one
        self.throttle = None
        self.direction = None

    def fresh(self, stale_after_s):
        return (self.last_seen > 0 and
                (time.time() - self.last_seen) <= stale_after_s)

    def forget(self):
        """Drop the measurements, keep the identity.

        Blanking the D-Bus paths is not enough: the drives come back one frame
        family at a time, so an untouched field — the MCU temperature, which
        only 61453 carries — would republish a reading from before the outage.
        """
        self.status = self.mosfet_temp = self.motor_temp = None
        self.voltage = self.phase_irms = self.torque = self.rpm = None
        self.torque_pct = self.dc_current = self.phase_ipk = None
        self.mosfet_temp_1c = self.drive_temp_1c = self.mcu_temp = None
        self.throttle = self.direction = None


# ============================================================================
# The driver
# ============================================================================
class EDriveDriver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.start_ts = time.time()
        self.sbus = shared_bus()
        self._sock = None
        self._watch = None
        self._first_frame_logged = False

        self.drives = [Drive(d) for d in cfg.drives]
        self.by_node = {d.cfg.canopen_node: d for d in self.drives}
        self.by_hcu = {d.cfg.hcu_source: d for d in self.drives}
        self.by_n2k = {d.cfg.n2k_instance: d for d in self.drives}
        for name, table in (("CANopen node", self.by_node),
                            ("HCU source address", self.by_hcu),
                            ("N2K engine instance", self.by_n2k)):
            if len(table) != len(self.drives):
                raise SystemExit("config.ini: two drives share a %s" % name)

        self._open_can()
        if self._sock is None:
            raise SystemExit("cannot open %s" % cfg.can_iface)

        self._init_settings()
        for d in self.drives:
            self._init_service(d)

        self._watch = GLib.io_add_watch(
            self._sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
            self._can_readable)
        GLib.timeout_add(cfg.publish_ms, self._tick)

    # ------------------------------------------------------------------ CAN
    def _can_filters(self):
        """Receive filters for exactly the ids we decode.

        11-bit and 29-bit frames need different entries. Putting CAN_EFF_FLAG
        in the mask with the bit clear in the id is what restricts an entry to
        SFF frames — without it the kernel would also hand us 29-bit frames
        whose low 11 bits happen to match.
        """
        eff = socket.CAN_EFF_FLAG & 0xFFFFFFFF
        nodes = sorted(self.by_node)
        flt = b""
        for code in TPDO_CODES:
            for node in nodes:
                flt += struct.pack("=II", (code | node) & 0xFFFFFFFF,
                                   (0x7FF | eff) & 0xFFFFFFFF)
        for pgn in (PGN_HCU_TORQUE, PGN_HCU_CURRENT, PGN_HCU_TEMPS,
                    PGN_THROTTLE, PGN_TRANSMISSION):
            flt += struct.pack("=II", ((pgn << 8) | eff) & 0xFFFFFFFF,
                               (0x03FFFF00 | eff) & 0xFFFFFFFF)
        return flt

    def _open_can(self):
        c = self.cfg
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                         self._can_filters())
            s.bind((c.can_iface,))
            s.setblocking(False)
        except OSError as e:
            log.error("cannot open %s (%s), retrying in %ds",
                      c.can_iface, e, c.can_reconnect_s)
            GLib.timeout_add_seconds(c.can_reconnect_s, self._reopen_can)
            return
        self._sock = s
        log.info("listening on %s (read-only) for %d CANopen ids and 5 PGNs",
                 c.can_iface, len(TPDO_CODES) * len(self.by_node))

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
        extended = bool(cid & (socket.CAN_EFF_FLAG & 0xFFFFFFFF))
        cid &= (socket.CAN_EFF_MASK if extended else 0x7FF)
        return cid, extended, frame[8:8 + min(dlc, 8)]

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
                cid, extended, data = self._parse_frame(frame)
                if extended:
                    self._on_j1939(pgn_of(cid), cid & 0xFF, data)
                else:
                    self._on_canopen(cid, data)
        except BlockingIOError:
            pass
        except OSError as e:
            log.error("CAN read failed (%s), reopening in %ds",
                      e, self.cfg.can_reconnect_s)
            self._close_can(remove_watch=False)
            GLib.timeout_add_seconds(self.cfg.can_reconnect_s, self._reopen_can)
            return False
        return True

    def _seen(self, d):
        d.last_seen = time.time()
        if not self._first_frame_logged:
            self._first_frame_logged = True
            log.info("first drive frame %.1fs after start", d.last_seen - self.start_ts)

    # --------------------------------------------------------- CANopen TPDO
    def _on_canopen(self, cid, data):
        d = self.by_node.get(cid & 0x7F)
        if d is None:
            return
        code = cid & 0x780
        if code == TPDO_STATUS:
            if data:
                d.status = data[0]
                self._seen(d)
            return
        if len(data) < 8:
            return
        f1, f2 = _f32(data, 0), _f32(data, 4)
        if code == TPDO_TEMPS:
            # f1 = MOSFET, f2 = drive (motor). Confirmed against the MFD on
            # both drives; the display truncates them to whole degrees.
            d.mosfet_temp, d.motor_temp = f1, f2
        elif code == TPDO_VOLTAGE:
            d.voltage, d.phase_irms = f1, f2
        elif code == TPDO_TORQUE:
            # f1 is TORQUE in N·m, NOT power × 100 W: T·ω matched the MFD
            # (2.017 vs 2.0 kW, 2.56 vs 2.6) where ×100 W was 2.2× high.
            d.torque, d.rpm = f1, f2
        else:
            return
        self._seen(d)

    # ------------------------------------------------------ J1939 / NMEA2000
    def _on_j1939(self, pgn, src_addr, data):
        c = self.cfg
        if pgn in (PGN_HCU_TORQUE, PGN_HCU_CURRENT, PGN_HCU_TEMPS):
            # HCU frames carry no instance byte, so the side comes from the
            # source address — and it is inverted: 0x64 = STARBOARD, 0x65 = PORT.
            d = self.by_hcu.get(src_addr)
            if d is None:
                return
            if pgn == PGN_HCU_TORQUE and len(data) >= 2:
                d.torque_pct = data[1] - J1939_PERCENT_OFFSET
            elif pgn == PGN_HCU_CURRENT and len(data) >= 6:
                d.dc_current = c.dc_current_offset + c.dc_current_slope * _u16(data, 2)
                d.phase_ipk = _u16(data, 4) / c.phase_peak_divisor
            elif pgn == PGN_HCU_TEMPS and len(data) >= 6:
                # Standard J1939 encoding, 0.03125 K/bit with a 273 K offset.
                # Word order is NOT the display order: w0 MOSFET, w1 drive,
                # w2 MCU/HCU — w2 is the only source for the controller board.
                d.mosfet_temp_1c = _u16(data, 0) * 0.03125 - 273
                d.drive_temp_1c = _u16(data, 2) * 0.03125 - 273
                d.mcu_temp = _u16(data, 4) * 0.03125 - 273
            else:
                return
            self._seen(d)
            return

        # 65363 and 127493 are matched by PGN and instance byte, never by
        # source address: N2K addresses renumber on address claim.
        #
        # Neither counts as liveness. Both come from the Yanmar gateway, not
        # from the drives, and the gateway keeps broadcasting with the hybrid
        # system powered down — treating them as a heartbeat (as the flow did)
        # would hold /Connected at 1 on a drive that has been off for hours,
        # with every real measurement blank.
        if pgn == PGN_TRANSMISSION:
            if len(data) < 2:
                return
            d = self.by_n2k.get(data[0])
            if d is None:
                return
            gear = data[1] & 0x03
            if gear in GEAR_TO_DIRECTION:
                d.direction = GEAR_TO_DIRECTION[gear]
        elif pgn == PGN_THROTTLE:
            if len(data) < 3:
                return
            d = self.by_n2k.get(data[0])
            if d is None:
                return
            d.throttle = _u16(data, 1) / c.throttle_divisor

    # ------------------------------------------------------------- settings
    def _init_settings(self):
        supported = {}
        for d in self.drives:
            supported["inst_" + d.key] = [
                "/Settings/Devices/%s/ClassAndVrmInstance" % d.cfg.settings_id,
                "motordrive:%d" % d.cfg.instance, 0, 0]
            supported["name_" + d.key] = [
                "/Settings/EDrive/%s/CustomName" % d.key, d.cfg.custom_name, 0, 0]
        self.settings = SettingsDevice(self.sbus, supported,
                                       self._setting_changed, timeout=120)
        for d in self.drives:
            d.instance = self._claim_instance(d)
            d.name = str(self.settings["name_" + d.key])

    def _claim_instance(self, d):
        """Pin /Settings/Devices/<id>/ClassAndVrmInstance, reconverging if
        localsettings granted something else (registry-style self-heal).

        The settings id deliberately has no `virtual_` prefix so the Node-RED
        palette's auto-cleanup never touches it.
        """
        wanted = d.cfg.instance
        try:
            granted = int(str(self.settings["inst_" + d.key]).split(":")[1])
        except (IndexError, ValueError):
            granted = wanted
        if granted == wanted:
            return granted
        in_use = False
        for name in self.sbus.list_names():
            if not str(name).startswith("com.victronenergy.motordrive."):
                continue
            try:
                di = self.sbus.call_blocking(name, "/DeviceInstance", BUSITEM,
                                             "GetValue", "", [], timeout=2)
                if int(di) == wanted:
                    in_use = True
                    break
            except Exception:
                continue
        if in_use:
            log.warning("%s: instance %d is held by a live motordrive service; "
                        "using %d instead. Retire the Node-RED flow and remove "
                        "its /Settings/Devices/virtual_gl6gk_* entries "
                        "(see migrate.sh), then restart this driver.",
                        d.cfg.settings_id, wanted, granted)
            return granted
        path = "/Settings/Devices/%s/ClassAndVrmInstance" % d.cfg.settings_id
        try:
            self.sbus.call_blocking(SETTINGS_SVC, path, BUSITEM, "SetValue", "v",
                                    ["motordrive:%d" % wanted], timeout=5)
            log.info("%s: reconverged instance %d -> %d",
                     d.cfg.settings_id, granted, wanted)
            return wanted
        except Exception as e:
            log.warning("%s: could not pin instance %d (%s); using %d",
                        d.cfg.settings_id, wanted, e, granted)
            return granted

    def _setting_changed(self, name, _old, new):
        if not name.startswith("name_"):
            return
        key = name[5:]
        for d in self.drives:
            if d.key == key and d.svc is not None:
                d.name = str(new)
                d.svc["/CustomName"] = d.name

    # -------------------------------------------------------------- service
    def _init_service(self, d):
        c = self.cfg
        name = "com.victronenergy.motordrive.%s" % d.cfg.suffix
        bus = private_bus()
        svc = new_service(name, bus)
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion",
                     "%s on Python %s" % (VERSION, platform.python_version()))
        svc.add_path("/Mgmt/Connection",
                     "%s CANopen node 0x%02X / HCU 0x%02X" %
                     (c.can_iface, d.cfg.canopen_node, d.cfg.hcu_source))
        svc.add_path("/DeviceInstance", d.instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", c.product_name)
        svc.add_path("/CustomName", d.name, writeable=True,
                     onchangecallback=self._make_name_cb(d))
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Serial", d.cfg.settings_id)
        svc.add_path("/Connected", 0)

        # The nine motordrive data paths
        svc.add_path("/Dc/0/Voltage", None, gettextcallback=fmt("V", 2))
        svc.add_path("/Dc/0/Current", None, gettextcallback=fmt("A", 2))
        svc.add_path("/Dc/0/Power", None, gettextcallback=fmt("W", 0))
        svc.add_path("/Motor/RPM", None, gettextcallback=fmt("rpm", 0))
        svc.add_path("/Motor/Direction", None, gettextcallback=fmt_direction)
        svc.add_path("/Motor/Temperature", None, gettextcallback=fmt("°C", 1))
        svc.add_path("/Controller/Temperature", None, gettextcallback=fmt("°C", 1))
        svc.add_path("/Coolant/Temperature", None, gettextcallback=fmt("°C", 1))

        if c.publish_extra:
            # Telemetry the motordrive class has no path for. The flow could
            # only drop this on a debug node; on D-Bus it reaches Signal K,
            # MQTT and the HTML5 app like anything else.
            svc.add_path("/EDrive/TorqueNm", None, gettextcallback=fmt("Nm", 2))
            svc.add_path("/EDrive/TorquePercent", None, gettextcallback=fmt("%", 0))
            svc.add_path("/EDrive/ThrottlePercent", None, gettextcallback=fmt("%", 1))
            svc.add_path("/EDrive/PhaseCurrentRms", None, gettextcallback=fmt("A", 1))
            svc.add_path("/EDrive/PhaseCurrentPeak", None, gettextcallback=fmt("A", 1))
            svc.add_path("/EDrive/MechanicalPower", None, gettextcallback=fmt("W", 0))
            svc.add_path("/EDrive/MosfetTemperature", None,
                         gettextcallback=fmt("°C", 1))
            svc.add_path("/EDrive/Running", None)
            svc.add_path("/EDrive/StatusByte", None)

        register_service(svc)
        d.svc = svc
        d.bus = bus
        log.info("registered %s (instance %d, %s): CANopen node 0x%02X, "
                 "HCU src 0x%02X, N2K engine instance %d",
                 name, d.instance, d.name, d.cfg.canopen_node,
                 d.cfg.hcu_source, d.cfg.n2k_instance)

    def _make_name_cb(self, d):
        def cb(_path, value):
            d.name = str(value)
            try:
                self.settings["name_" + d.key] = d.name
            except Exception:
                return False
            return True
        return cb

    # ------------------------------------------------------------- publish
    def _tick(self):
        for d in self.drives:
            self._publish(d)
        return True

    def _publish(self, d):
        """One ItemsChanged per drive per tick, carrying only what moved.

        velib's context manager collects every change made inside the block
        and emits a single ItemsChanged when it closes, instead of one
        PropertiesChanged per path. With eleven paths jittering every second
        that was ~9 signals/s per drive, each fanned out to the seven or so
        listeners on this Cerbo (systemcalc, vrmlogger, the GUI, Signal K,
        flashmq ...) — measured 2026-09-05 as three quarters of all bus
        traffic and enough load to trip the watchdog. Quantised values
        (see Config) mean an idle drive usually changes nothing at all.
        """
        c = self.cfg
        svc = d.svc
        if svc is None:
            return
        fresh = d.fresh(c.stale_after_s)
        with svc as s:
            if d.connected != fresh:
                if d.connected and not fresh:
                    log.info("%s: no CAN frames for %.0fs — disconnected",
                             d.key, time.time() - d.last_seen)
                    d.forget()
                elif fresh:
                    log.info("%s: connected", d.key)
                d.connected = fresh
                s["/Connected"] = 1 if fresh else 0

            if not fresh:
                for p in ("/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                          "/Motor/RPM", "/Motor/Direction", "/Motor/Temperature",
                          "/Controller/Temperature", "/Coolant/Temperature"):
                    s[p] = None
                if c.publish_extra:
                    for p in ("/EDrive/TorqueNm", "/EDrive/TorquePercent",
                              "/EDrive/ThrottlePercent", "/EDrive/PhaseCurrentRms",
                              "/EDrive/PhaseCurrentPeak", "/EDrive/MechanicalPower",
                              "/EDrive/MosfetTemperature", "/EDrive/Running",
                              "/EDrive/StatusByte"):
                        s[p] = None
                return

            # clamp the tiny signed float noise around standstill so VRM never
            # shows -1 rpm on a drive that is not turning; the clamped values
            # feed everything derived below, so a stopped drive is exactly 0
            rpm = d.rpm
            if rpm is not None and abs(rpm) < c.rpm_deadband:
                rpm = 0
            torque = d.torque
            if torque is not None and abs(torque) < c.torque_deadband:
                torque = 0.0
            if rpm is not None:
                s["/Motor/RPM"] = int(round(rpm))
            s["/Dc/0/Voltage"] = _q(d.voltage, c.voltage_step)

            # Prefer the measured DC current from 61452. Fall back to
            # mechanical power / V (ignores drive losses, so a few % optimistic)
            # when the HCU frame is missing.
            amps = d.dc_current
            if amps is None and torque is not None and rpm is not None \
                    and d.voltage is not None and d.voltage > 1:
                amps = (torque * rpm * math.pi / 30) / d.voltage
            if amps is not None and abs(amps) < c.current_deadband:
                amps = 0.0
            s["/Dc/0/Current"] = _q(amps, c.current_step)
            if amps is not None and d.voltage is not None and d.voltage > 1:
                s["/Dc/0/Power"] = _q(d.voltage * amps, c.power_step)
            else:
                s["/Dc/0/Power"] = None

            # Three sensors, three paths. Prefer the 0x28x floats over the 1 °C
            # 61453 words wherever both carry the same sensor.
            t_motor = d.motor_temp if d.motor_temp is not None else d.drive_temp_1c
            t_mosfet = (d.mosfet_temp if d.mosfet_temp is not None
                        else d.mosfet_temp_1c)
            s["/Motor/Temperature"] = _q(t_motor, c.temperature_step)
            s["/Controller/Temperature"] = _q(d.mcu_temp, c.temperature_step)
            s["/Coolant/Temperature"] = _q(t_mosfet, c.temperature_step)
            s["/Motor/Direction"] = d.direction

            if not c.publish_extra:
                return
            irms, ipk = d.phase_irms, d.phase_ipk
            if irms is not None and abs(irms) < c.current_deadband:
                irms = 0.0
            if ipk is not None and abs(ipk) < c.current_deadband:
                ipk = 0.0
            s["/EDrive/TorqueNm"] = _q(torque, c.torque_step)
            s["/EDrive/TorquePercent"] = d.torque_pct
            s["/EDrive/ThrottlePercent"] = _q(d.throttle, c.throttle_step)
            s["/EDrive/PhaseCurrentRms"] = _q(irms, c.phase_current_step)
            s["/EDrive/PhaseCurrentPeak"] = _q(ipk, c.phase_current_step)
            s["/EDrive/MechanicalPower"] = (
                None if torque is None or rpm is None
                else _q(torque * rpm * math.pi / 30, c.power_step))
            s["/EDrive/MosfetTemperature"] = _q(t_mosfet, c.temperature_step)
            s["/EDrive/Running"] = (None if d.status is None
                                    else int(d.status == STATUS_RUNNING))
            s["/EDrive/StatusByte"] = d.status

def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
    cfg = Config(cfg_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")
    log.info("dbus-edrive v%s starting (velib: %s)", VERSION, _VELIB_DIR)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    EDriveDriver(cfg)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
