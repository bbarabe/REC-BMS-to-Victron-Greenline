#!/usr/bin/env python3
"""
dbus-recbms — standalone Venus OS driver for the REC-BMS main bank.

Replaces the Node-RED "Virtual BMS" flow (Virtual BMS.json). Runs as a
daemontools service, starts seconds after D-Bus at boot — independent of
Node-RED / Signal K — and is untouched by Node-RED deploys.

Data path:
    YDNB-07 repackages the REC-BMS 11-bit CAN-BMS frames (0x351..0x404)
    as 29-bit 0x18FF0NNN frames on can0. This driver reads them with a
    raw SocketCAN socket (kernel filter 0x18FF0000/0x1FFFF800 — no
    candump subprocess, no line parsing), decodes them and publishes:
      - com.victronenergy.battery.<suffix>  (the BMS, instance 200)
      - com.victronenergy.switch.<suffix>   (VRM "Max Charge" slider, 220)

Persistence (slider position, equalization schedule, custom name) lives
in localsettings (com.victronenergy.settings) — no state file, no
writable-directory discovery, survives reboots/updates natively.

Behavior ported 1:1 from the Node-RED flow:
  - staged fallback   LIVE -> ALERT -> RESTRICT -> SURVIVAL on CAN loss
  - startup grace     cold boot is not CAN loss (benign STARTUP phase)
  - Quattro /Dc/0/Voltage as independent pack-voltage source when stale
  - CVL from Max Charge slider (40-100% -> base+span mapping)
  - weekly 1h equalization boost (+0.44V), only while LIVE
  - synthetic alarms (REC-BMS sends no 0x35A frame)

Baselines:
  https://github.com/victronenergy/velib_python           (dbusdummyservice.py)
  https://github.com/mr-manuel/venus-os_dbus-mqtt-battery (service structure)
"""

import configparser
import glob
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

VERSION = "1.1.0"
BUSITEM = "com.victronenergy.BusItem"

log = logging.getLogger("dbus-recbms")


# ----------------------------------------------------------------------------
# velib_python (use the copy shipped with Venus OS so the API always matches
# the running localsettings/dbus stack; /data/velib_python wins if present)
# ----------------------------------------------------------------------------
def _find_velib():
    # dbus-systemcalc-py ships the most current velib copy on the device
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


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        c = cp["can"] if cp.has_section("can") else {}
        self.can_iface = c.get("interface", "can0")
        self.can_filter_id = int(str(c.get("filter_id", "0x18FF0000")), 16)
        self.can_filter_mask = int(str(c.get("filter_mask", "0x1FFFF800")), 16)
        self.can_reconnect_s = int(c.get("reconnect_delay_s", 5))

        b = cp["battery"] if cp.has_section("battery") else {}
        self.batt_suffix = b.get("service_suffix", "recbms")
        self.batt_settings_id = b.get("settings_id", "recbms")
        self.batt_instance = int(b.get("instance", 200))
        self.product_name = b.get("product_name", "REC-BMS")
        self.custom_name = b.get("custom_name", "REC-BMS Main Bank")
        self.installed_ah = float(b.get("installed_capacity_ah", 1440))
        self.nr_of_cells = int(b.get("nr_of_cells", 15))
        self.serial_default = b.get("serial", "REC-BMS")
        # forward the BMS 0x360 force-charge flag to /Info/ChargeRequest
        # (DVCC acts on it) — off by default, matching the old flow
        self.forward_charge_request = \
            str(b.get("forward_charge_request", "false")).lower() == "true"

        s = cp["slider"] if cp.has_section("slider") else {}
        self.slider_enabled = str(s.get("enabled", "true")).lower() != "false"
        self.slider_suffix = s.get("service_suffix", "recbms_maxcharge")
        self.slider_settings_id = s.get("settings_id", "recbms_maxcharge")
        self.slider_instance = int(s.get("instance", 220))
        self.slider_name = s.get("custom_name", "Max Charge")
        self.slider_group = s.get("group", "BMS")
        self.slider_unit = s.get("unit", "%")
        self.slider_min = float(s.get("min", 40))
        self.slider_max = float(s.get("max", 100))
        self.slider_step = float(s.get("step", 5))
        self.slider_default = float(s.get("default", 100))

        v = cp["cvl"] if cp.has_section("cvl") else {}
        self.cvl_base = float(v.get("base_v", 54.0))
        self.cvl_span = float(v.get("span_v", 7.96))
        self.eq_boost = float(v.get("eq_boost_v", 0.44))
        self.eq_interval_s = float(v.get("eq_interval_days", 7)) * 86400
        self.eq_duration_s = float(v.get("eq_duration_min", 60)) * 60

        f = cp["fallback"] if cp.has_section("fallback") else {}
        self.live_timeout = float(f.get("live_timeout_s", 60))
        self.alert_timeout = float(f.get("alert_timeout_s", 120))
        self.restrict_timeout = float(f.get("restrict_timeout_s", 300))
        self.startup_grace = float(f.get("startup_grace_s", 180))
        self.alert_dcl = float(f.get("alert_dcl_a", 100))
        self.alert_dvl = float(f.get("alert_dvl_v", 52.0))
        self.restrict_dcl = float(f.get("restrict_dcl_a", 30))
        self.restrict_dvl = float(f.get("restrict_dvl_v", 53.0))
        self.survival_dcl = float(f.get("survival_dcl_a", 15))
        self.survival_dvl = float(f.get("survival_dvl_v", 54.0))
        self.safe_cvl = float(f.get("safe_cvl_v", 62.7))
        self.safe_voltage = float(f.get("safe_voltage_v", 54.0))
        self.safe_soc = float(f.get("safe_soc", 50))

        q = cp["quattro"] if cp.has_section("quattro") else {}
        self.vebus_instance = int(q.get("vebus_instance", 276))
        self.extv_max_age = float(q.get("max_age_s", 30))
        self.extv_poll_s = int(q.get("poll_s", 5))


# ----------------------------------------------------------------------------
# CAN frame decoding (1:1 port of the Node-RED "CAN Frame Decoder")
# ----------------------------------------------------------------------------
MIN_LEN = {
    0x351: 8, 0x355: 4, 0x356: 8, 0x35E: 1, 0x35F: 6, 0x360: 1,
    0x370: 1, 0x371: 1, 0x372: 8, 0x373: 8, 0x374: 1, 0x375: 1,
    0x376: 1, 0x377: 1, 0x379: 2, 0x380: 1, 0x381: 1, 0x404: 1,
}


def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _s16(d, o):
    return struct.unpack_from("<h", d, o)[0]


def _ascii(d):
    return d.split(b"\0")[0].decode("ascii", errors="replace").strip("�")


def decode_frame(bms, canid, data):
    """Update the live BMS state dict from one decoded frame.
    Returns True if the frame refreshed the staleness timestamp."""
    if canid < 0x351 or canid > 0x404 or canid not in MIN_LEN:
        return False
    if len(data) < MIN_LEN[canid]:
        return False  # short/corrupt frame: caller rate-limits the warning

    if canid == 0x351:
        bms["cvl"] = _u16(data, 0) / 10.0
        bms["ccl"] = _u16(data, 2) / 10.0
        bms["dcl"] = _u16(data, 4) / 10.0
        bms["dvl"] = _u16(data, 6) / 10.0
    elif canid == 0x355:
        bms["soc"] = _u16(data, 0)
        bms["soh"] = _u16(data, 2)
        if len(data) >= 6:
            bms["socHiRes"] = _u16(data, 4) / 100.0
    elif canid == 0x356:
        bms["voltage"] = _s16(data, 0) / 100.0
        bms["current"] = _s16(data, 2) / 10.0
        bms["temperature"] = _s16(data, 4) / 10.0
        bms["chargeCycles"] = _u16(data, 6)
    elif canid == 0x35E:
        bms["manufacturer"] = _ascii(data)
    elif canid == 0x35F:
        bms["chemistry"] = data[0]
        bms["hwVersion"] = "%d.%d" % (data[2], data[3])
        bms["fwVersion"] = _u16(data, 4)
    elif canid == 0x360:
        bms["forceCharge"] = data[0] == 0xFF
    elif canid == 0x370:
        bms["productName"] = _ascii(data)
    elif canid == 0x371:
        bms["batteryName"] = _ascii(data)
    elif canid == 0x372:
        bms["modulesOnline"] = _u16(data, 0)
        bms["modulesBlockingCharge"] = _u16(data, 2)
        bms["modulesBlockingDischarge"] = _u16(data, 4)
        bms["modulesOffline"] = _u16(data, 6)
    elif canid == 0x373:
        bms["minCellV"] = _u16(data, 0) / 1000.0
        bms["maxCellV"] = _u16(data, 2) / 1000.0
        bms["minCellT"] = _s16(data, 4) - 273
        bms["maxCellT"] = _s16(data, 6) - 273
    elif canid in (0x374, 0x375, 0x376, 0x377):
        # Identity of the extreme cells (the
        # strings track which module/sensor currently holds the extreme),
        # per the Victron CAN-BMS spec — not static module IDs.
        key = {0x374: "minVCellId", 0x375: "maxVCellId",
               0x376: "minTCellId", 0x377: "maxTCellId"}[canid]
        bms[key] = _ascii(data)
    elif canid == 0x379:
        # Installed (rated) capacity — constant 1440 on this bank regardless
        # of SOC (the old flow mislabeled it as remaining capacity).
        bms["installedAh"] = _u16(data, 0)
    elif canid == 0x380:
        bms["serial"] = _ascii(data)
    elif canid == 0x381:
        bms["alarm381"] = bytes(data)  # not yet decoded (no REC bit map)
    elif canid == 0x404:
        bms["statusByte"] = data[0]

    bms["_lastUpdate"] = time.time()
    return True


# ----------------------------------------------------------------------------
# D-Bus helpers
# ----------------------------------------------------------------------------
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
        svc._recbms_needs_register = True
    else:
        svc = VeDbusService(name, bus=bus)
        svc._recbms_needs_register = False
    return svc


def register_service(svc):
    if getattr(svc, "_recbms_needs_register", False):
        svc.register()


def fmt(unit, digits):
    def cb(path, value):
        if value is None:
            return "---"
        return ("%." + str(digits) + "f%s") % (float(value), unit)
    return cb


def fmt_int(unit=""):
    def cb(path, value):
        return "---" if value is None else "%d%s" % (int(value), unit)
    return cb


# ----------------------------------------------------------------------------
# The driver
# ----------------------------------------------------------------------------
class RecBmsDriver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bms = {}
        self.start_ts = time.time()
        self.phase_name = None          # for change-only logging
        self.eq = {"active": False, "startTime": 0.0}
        self.extv = None                # (volts, ts) from the Quattro
        self.current_ema = None         # ~60s-smoothed current for TimeToFull
        self._vebus_name = None
        self._can_sock = None
        self._can_watch = None
        self._last_short_warn = 0.0
        self._first_frame_logged = False

        self.sbus = shared_bus()
        self._init_settings()
        self._init_battery_service()
        if cfg.slider_enabled:
            self._init_switch_service()

        self._open_can()
        GLib.timeout_add_seconds(cfg.extv_poll_s, self._poll_ext_voltage)
        GLib.timeout_add(1000, self._tick)

    # ------------------------------------------------------------------ setup
    def _init_settings(self):
        c = self.cfg
        supported = {
            "battinstance": [
                "/Settings/Devices/%s/ClassAndVrmInstance" % c.batt_settings_id,
                "battery:%d" % c.batt_instance, 0, 0],
            "chargeslider": [
                "/Settings/RecBms/ChargeSlider", int(c.slider_default),
                int(c.slider_min), int(c.slider_max)],
            "eqlast": ["/Settings/RecBms/EqLastCompleted", 0.0, 0, 0],
            "customname": ["/Settings/RecBms/CustomName", c.custom_name, 0, 0],
        }
        if c.slider_enabled:
            supported["sliderinstance"] = [
                "/Settings/Devices/%s/ClassAndVrmInstance" % c.slider_settings_id,
                "switch:%d" % c.slider_instance, 0, 0]

        self.settings = SettingsDevice(
            self.sbus, supported, self._setting_changed, timeout=120)

        # First install: baseline the EQ schedule so the first equalization
        # runs one interval from now, not immediately (same as the NR flow).
        if not self.settings["eqlast"]:
            self.settings["eqlast"] = time.time()
            log.info("first run: equalization baselined to now")

        self.batt_instance = self._claim_instance(
            "battinstance", "battery", c.batt_settings_id, c.batt_instance)
        self.slider_instance = None
        if c.slider_enabled:
            self.slider_instance = self._claim_instance(
                "sliderinstance", "switch", c.slider_settings_id, c.slider_instance)

    def _claim_instance(self, alias, cls, settings_id, wanted):
        """Parse 'class:NNN' from localsettings; reconverge to the pinned
        number if another allocation grabbed it while the old Node-RED flow
        still held the instance (registry-style self-heal)."""
        granted = self._parse_instance(self.settings[alias], wanted)
        if granted == wanted:
            return granted
        # Is the wanted instance actually in use by a live service?
        in_use = False
        for name in self.sbus.list_names():
            if not str(name).startswith("com.victronenergy.%s." % cls):
                continue
            try:
                di = self.sbus.call_blocking(
                    name, "/DeviceInstance", BUSITEM, "GetValue", "", [],
                    timeout=2)
                if int(di) == wanted:
                    in_use = True
                    break
            except Exception:
                continue
        if in_use:
            log.warning(
                "%s: wanted instance %d is held by a live service; using %d. "
                "Disable the old Node-RED Virtual BMS flow, clean its "
                "settings entries, then restart this driver.",
                settings_id, wanted, granted)
            return granted
        path = "/Settings/Devices/%s/ClassAndVrmInstance" % settings_id
        try:
            self.sbus.call_blocking(
                "com.victronenergy.settings", path, BUSITEM, "SetValue", "v",
                ["%s:%d" % (cls, wanted)], timeout=5)
            log.info("%s: reconverged instance %d -> %d", settings_id, granted, wanted)
            return wanted
        except Exception as e:
            log.warning("%s: could not pin instance %d (%s); using %d",
                        settings_id, wanted, e, granted)
            return granted

    @staticmethod
    def _parse_instance(value, fallback):
        try:
            return int(str(value).split(":")[1])
        except (IndexError, ValueError):
            return fallback

    def _init_battery_service(self):
        c = self.cfg
        svc = new_service("com.victronenergy.battery.%s" % c.batt_suffix,
                          private_bus())
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion",
                     "%s on Python %s" % (VERSION, platform.python_version()))
        svc.add_path("/Mgmt/Connection",
                     "SocketCAN %s (REC-BMS via YDNB-07)" % c.can_iface)
        svc.add_path("/DeviceInstance", self.batt_instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", c.product_name)
        svc.add_path("/CustomName", self.settings["customname"],
                     writeable=True, onchangecallback=self._customname_changed)
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Serial", c.serial_default)
        svc.add_path("/Connected", 1)

        v2, a1, w1, t1, pc = fmt("V", 2), fmt("A", 1), fmt("W", 1), fmt("C", 1), fmt("%", 0)
        svc.add_path("/Dc/0/Voltage", None, gettextcallback=v2)
        svc.add_path("/Dc/0/Current", None, gettextcallback=a1)
        svc.add_path("/Dc/0/Power", None, gettextcallback=w1)
        svc.add_path("/Dc/0/Temperature", None, gettextcallback=t1)
        svc.add_path("/Soc", None, gettextcallback=fmt("%", 1))
        svc.add_path("/Soh", None, gettextcallback=pc)
        svc.add_path("/Capacity", None, gettextcallback=fmt("Ah", 1))
        svc.add_path("/ConsumedAmphours", None, gettextcallback=fmt("Ah", 1))
        svc.add_path("/InstalledCapacity", c.installed_ah,
                     gettextcallback=fmt("Ah", 0))
        svc.add_path("/TimeToGo", None)

        svc.add_path("/Info/MaxChargeVoltage", None, gettextcallback=v2)
        svc.add_path("/Info/MaxChargeCurrent", None, gettextcallback=a1)
        svc.add_path("/Info/MaxDischargeCurrent", None, gettextcallback=a1)
        svc.add_path("/Info/BatteryLowVoltage", None, gettextcallback=v2)
        svc.add_path("/Info/ChargeRequest", 0)

        for p in ("LowVoltage", "HighVoltage", "LowTemperature",
                  "HighTemperature", "LowSoc", "HighChargeCurrent",
                  "HighDischargeCurrent", "CellImbalance", "InternalFailure"):
            svc.add_path("/Alarms/%s" % p, 0)
        svc.add_path("/ErrorCode", 0)

        v3 = fmt("V", 3)
        svc.add_path("/System/MinCellVoltage", None, gettextcallback=v3)
        svc.add_path("/System/MaxCellVoltage", None, gettextcallback=v3)
        svc.add_path("/System/MinVoltageCellId", None)
        svc.add_path("/System/MaxVoltageCellId", None)
        svc.add_path("/System/MinTemperatureCellId", None)
        svc.add_path("/System/MaxTemperatureCellId", None)
        svc.add_path("/System/MinCellTemperature", None, gettextcallback=t1)
        svc.add_path("/System/MaxCellTemperature", None, gettextcallback=t1)
        svc.add_path("/System/NrOfCellsPerBattery", c.nr_of_cells)
        svc.add_path("/System/NrOfBatteries", 1)
        svc.add_path("/System/BatteriesParallel", 1)
        svc.add_path("/System/BatteriesSeries", 1)
        svc.add_path("/System/NrOfModulesOnline", None, gettextcallback=fmt_int())
        svc.add_path("/System/NrOfModulesOffline", None, gettextcallback=fmt_int())
        svc.add_path("/System/NrOfModulesBlockingCharge", None,
                     gettextcallback=fmt_int())
        svc.add_path("/System/NrOfModulesBlockingDischarge", None,
                     gettextcallback=fmt_int())
        svc.add_path("/History/ChargeCycles", None, gettextcallback=fmt_int())

        # Driver diagnostics (non-standard, read-only)
        svc.add_path("/RecBms/Phase", "STARTUP")
        svc.add_path("/RecBms/EqStatus", "")
        svc.add_path("/RecBms/TimeToFull", None,
                     gettextcallback=lambda p, val:
                     "---" if val is None else "%.0fh" % (float(val) / 3600))
        svc.add_path("/RecBms/ForceChargeRequest", 0)

        register_service(svc)
        self.batt = svc
        log.info("registered com.victronenergy.battery.%s instance %d",
                 c.batt_suffix, self.batt_instance)

    def _init_switch_service(self):
        c = self.cfg
        svc = new_service("com.victronenergy.switch.%s" % c.slider_suffix,
                          private_bus())
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion",
                     "%s on Python %s" % (VERSION, platform.python_version()))
        svc.add_path("/Mgmt/Connection", "dbus-recbms")
        svc.add_path("/DeviceInstance", self.slider_instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", "%s slider" % c.slider_name)
        svc.add_path("/CustomName", c.slider_name)
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/Serial", c.slider_settings_id)
        svc.add_path("/Connected", 1)
        svc.add_path("/State", 0x100)  # module state: connected

        o = "/SwitchableOutput/output_1"
        slider = float(self.settings["chargeslider"])
        svc.add_path(o + "/State", 0, writeable=True,
                     onchangecallback=lambda p, v: True)
        svc.add_path(o + "/Status", 0)
        svc.add_path(o + "/Name", "Basic slider")
        svc.add_path(o + "/Dimming", slider, writeable=True,
                     onchangecallback=self._slider_changed,
                     gettextcallback=fmt(c.slider_unit, 0))
        svc.add_path(o + "/Settings/Type", 7,
                     writeable=True, onchangecallback=lambda p, v: v in (7, 7.0))
        svc.add_path(o + "/Settings/ValidTypes", 1 << 7)
        svc.add_path(o + "/Settings/CustomName", c.slider_name)
        svc.add_path(o + "/Settings/Group", c.slider_group)
        svc.add_path(o + "/Settings/ShowUIControl", 1)
        svc.add_path(o + "/Settings/Adjustable", 0)
        svc.add_path(o + "/Settings/DimmingMin", c.slider_min)
        svc.add_path(o + "/Settings/DimmingMax", c.slider_max)
        svc.add_path(o + "/Settings/StepSize", c.slider_step)
        svc.add_path(o + "/Settings/Unit", c.slider_unit)

        register_service(svc)
        self.sw = svc
        log.info("registered com.victronenergy.switch.%s instance %d "
                 "(slider %.0f%%)", c.slider_suffix, self.slider_instance, slider)

    # -------------------------------------------------------------- callbacks
    def _slider_changed(self, path, value):
        c = self.cfg
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        v = max(c.slider_min, min(c.slider_max, v))
        try:
            self.settings["chargeslider"] = int(round(v))
        except Exception:
            log.warning("could not persist slider value %s", v)
        log.info("Max Charge slider -> %.0f%% (CVL %.2fV)", v, self._slider_cvl(v))
        return True

    def _customname_changed(self, path, value):
        try:
            self.settings["customname"] = str(value)
        except Exception:
            pass
        return True

    def _setting_changed(self, setting, old, new):
        # External change (e.g. dbus write to /Settings/RecBms/ChargeSlider):
        # reflect it on the slider so VRM and settings stay in sync.
        if setting == "chargeslider" and self.cfg.slider_enabled:
            try:
                self.sw["/SwitchableOutput/output_1/Dimming"] = float(new)
            except Exception:
                pass

    # -------------------------------------------------------------------- CAN
    def _open_can(self):
        c = self.cfg
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            # Match only the repackaged 29-bit BMS frames, in the kernel.
            # CAN_EFF_FLAG is a NEGATIVE int on 32-bit platforms (armv7l) —
            # mask to u32 or struct 'I' rejects it.
            eff = socket.CAN_EFF_FLAG & 0xFFFFFFFF
            flt = struct.pack("=II",
                              (c.can_filter_id | eff) & 0xFFFFFFFF,
                              (c.can_filter_mask | eff) & 0xFFFFFFFF)
            s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
            s.bind((c.can_iface,))
            s.setblocking(False)
        except OSError as e:
            log.error("cannot open %s (%s), retrying in %ds",
                      c.can_iface, e, c.can_reconnect_s)
            GLib.timeout_add_seconds(c.can_reconnect_s, self._reopen_can)
            return
        self._can_sock = s
        self._can_watch = GLib.io_add_watch(
            s.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP, self._can_readable)
        log.info("listening on %s filter 0x%08X/0x%08X",
                 c.can_iface, c.can_filter_id, c.can_filter_mask)

    def _reopen_can(self):
        self._open_can()
        return False  # one-shot

    def _close_can(self, remove_watch=True):
        # remove_watch=False when called from inside the watch callback:
        # returning False there already removes the source.
        if remove_watch and self._can_watch is not None:
            GLib.source_remove(self._can_watch)
        self._can_watch = None
        if self._can_sock is not None:
            try:
                self._can_sock.close()
            except OSError:
                pass
            self._can_sock = None

    def _can_readable(self, fd, condition):
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            log.error("CAN socket error, reopening in %ds", self.cfg.can_reconnect_s)
            self._close_can(remove_watch=False)
            GLib.timeout_add_seconds(self.cfg.can_reconnect_s, self._reopen_can)
            return False
        try:
            while True:
                frame = self._can_sock.recv(16)
                if len(frame) < 16:
                    continue
                can_id, dlc = struct.unpack_from("=IB", frame)
                data = frame[8:8 + min(dlc, 8)]
                bmsid = (can_id & socket.CAN_EFF_MASK) & 0x7FF
                ok = decode_frame(self.bms, bmsid, data)
                if not ok and bmsid in MIN_LEN and \
                        time.time() - self._last_short_warn > 60:
                    log.warning("dropped short frame 0x%03X (len %d, need %d)",
                                bmsid, len(data), MIN_LEN[bmsid])
                    self._last_short_warn = time.time()
                if ok and not self._first_frame_logged:
                    log.info("first BMS frame decoded %.0fs after start",
                             time.time() - self.start_ts)
                    self._first_frame_logged = True
        except BlockingIOError:
            pass
        except OSError as e:
            log.error("CAN read failed (%s), reopening in %ds",
                      e, self.cfg.can_reconnect_s)
            self._close_can(remove_watch=False)
            GLib.timeout_add_seconds(self.cfg.can_reconnect_s, self._reopen_can)
            return False
        return True

    # ---------------------------------------------- Quattro voltage fallback
    def _poll_ext_voltage(self):
        try:
            name = self._vebus_name or self._find_vebus()
            if name:
                raw = self.sbus.call_blocking(
                    name, "/Dc/0/Voltage", BUSITEM, "GetValue", "", [], timeout=2)
                v = float(raw)
                if 20 <= v <= 80:
                    self.extv = (v, time.time())
                self._vebus_name = name
        except Exception:
            self._vebus_name = None
        return True

    def _find_vebus(self):
        first = None
        for name in self.sbus.list_names():
            n = str(name)
            if not n.startswith("com.victronenergy.vebus."):
                continue
            first = first or n
            try:
                di = self.sbus.call_blocking(
                    n, "/DeviceInstance", BUSITEM, "GetValue", "", [], timeout=2)
                if int(di) == self.cfg.vebus_instance:
                    return n
            except Exception:
                continue
        return first

    # ------------------------------------------------------------------ tick
    def _slider_cvl(self, slider):
        c = self.cfg
        span = c.slider_max - c.slider_min
        return c.cvl_base + (slider - c.slider_min) / span * c.cvl_span

    def _tick(self):
        c = self.cfg
        bms = self.bms
        now = time.time()

        # ---- staged fallback (port of the NR State Assembler) ----
        never_seen = "_lastUpdate" not in bms
        age = now - (self.start_ts if never_seen else bms["_lastUpdate"])
        live = (not never_seen) and age <= c.live_timeout
        startup = never_seen and age <= c.startup_grace

        if live:
            phase, phase_name, fb = 0, "LIVE", None
        elif startup:
            phase, phase_name = 0, "STARTUP"
            fb = (0.0, c.alert_dcl, c.alert_dvl)
        elif age <= c.alert_timeout:
            phase, phase_name = 1, "ALERT"
            fb = (0.0, c.alert_dcl, c.alert_dvl)
        elif age <= c.restrict_timeout:
            phase, phase_name = 2, "RESTRICT"
            fb = (0.0, c.restrict_dcl, c.restrict_dvl)
        else:
            phase, phase_name = 3, "SURVIVAL"
            fb = (0.0, c.survival_dcl, c.survival_dvl)

        safe = {
            "cvl": c.safe_cvl, "voltage": c.safe_voltage, "current": 0.0,
            "temperature": 20.0, "soc": c.safe_soc,
            "minCellV": 3.375, "maxCellV": 3.375, "minCellT": 20.0,
            "maxCellT": 20.0,
        }
        extv_fresh = self.extv is not None and (now - self.extv[1]) <= c.extv_max_age

        def v(key):
            if live:
                return bms[key] if bms.get(key) is not None else safe.get(key)
            if key == "voltage" and extv_fresh:
                return self.extv[0]
            if bms.get(key) is not None:
                return bms[key]
            return safe.get(key)

        # ---- CVL control: slider + weekly equalization ----
        slider = float(self.settings["chargeslider"] or c.slider_default)
        slider_cvl = self._slider_cvl(slider)
        eq = self.eq
        eq_last = float(self.settings["eqlast"] or 0)
        eq_eligible = live
        eq_due = (now - eq_last) >= c.eq_interval_s
        eq_label = ""

        if eq["active"]:
            elapsed = now - eq["startTime"]
            if elapsed >= c.eq_duration_s:
                eq["active"] = False
                self.settings["eqlast"] = now
                log.info("equalization completed")
                final_cvl = slider_cvl
                eq_label = "EQ done"
            elif not eq_eligible:
                eq["active"] = False
                log.warning("equalization aborted (BMS not live)")
                final_cvl = slider_cvl
                eq_label = "EQ aborted"
            else:
                mins_left = int((c.eq_duration_s - elapsed) / 60) + 1
                eq_label = "EQ %dmin left" % mins_left
                final_cvl = slider_cvl + c.eq_boost
        elif eq_eligible and eq_due:
            eq["active"] = True
            eq["startTime"] = now
            log.info("equalization starting (+%.2fV for %.0fmin)",
                     c.eq_boost, c.eq_duration_s / 60)
            eq_label = "EQ starting"
            final_cvl = slider_cvl + c.eq_boost
        else:
            final_cvl = slider_cvl
            if eq_eligible and not eq_due:
                eq_label = "next EQ ~%dh" % round((c.eq_interval_s - (now - eq_last)) / 3600)

        bms_cvl = v("cvl") if live else c.safe_cvl
        final_cvl = min(final_cvl, bms_cvl)

        # ---- resolve outputs ----
        if live:
            ccl, dcl, dvl = v("ccl"), v("dcl"), v("dvl")
        else:
            ccl, dcl, dvl = fb

        volts = v("voltage")
        amps = v("current") if live else 0.0
        # 0x355 bytes 4-5 carry SOC at 0.01% resolution — prefer it
        soc = bms["socHiRes"] if bms.get("socHiRes") is not None else v("soc")
        cell_min, cell_max = v("minCellV"), v("maxCellV")
        cell_min_t, cell_max_t = v("minCellT"), v("maxCellT")

        installed = bms.get("installedAh") or c.installed_ah
        remaining = soc / 100.0 * installed

        ttg = None
        if live and amps < -0.5:
            ttg = min(864000, int(remaining / -amps * 3600))

        # Time to full from ~60s-smoothed current (raw 0.1A steps make the
        # instantaneous figure useless at trickle charge rates)
        if live:
            self.current_ema = amps if self.current_ema is None else \
                self.current_ema + (amps - self.current_ema) / 60.0
        ttf = None
        if live and self.current_ema is not None and self.current_ema > 0.05:
            ttf = int((installed - remaining) / self.current_ema * 3600)

        s = self.batt
        s["/Info/MaxChargeVoltage"] = round(final_cvl, 2)
        s["/Info/MaxChargeCurrent"] = ccl
        s["/Info/MaxDischargeCurrent"] = dcl
        s["/Info/BatteryLowVoltage"] = dvl
        s["/Dc/0/Voltage"] = volts
        s["/Dc/0/Current"] = amps
        s["/Dc/0/Power"] = round(volts * amps, 1)
        s["/Dc/0/Temperature"] = v("temperature")
        s["/Soc"] = round(soc, 2)
        s["/Soh"] = bms.get("soh")
        s["/Capacity"] = round(remaining, 1)
        s["/ConsumedAmphours"] = round(remaining - installed, 1)  # BMV convention: negative
        s["/InstalledCapacity"] = installed
        s["/TimeToGo"] = ttg

        s["/Alarms/LowVoltage"] = (2 if cell_min < 3.00 else 1 if cell_min < 3.30 else 0) if live else 0
        s["/Alarms/HighVoltage"] = (2 if cell_max > 4.25 else 1 if cell_max > 4.20 else 0) if live else 0
        s["/Alarms/LowTemperature"] = (2 if cell_min_t < 0 else 1 if cell_min_t < 5 else 0) if live else 0
        s["/Alarms/HighTemperature"] = (2 if cell_max_t > 50 else 1 if cell_max_t > 45 else 0) if live else 0
        s["/Alarms/LowSoc"] = 2 if soc < 10 else 1 if soc < 20 else 0
        s["/Alarms/HighChargeCurrent"] = (1 if bms.get("modulesBlockingCharge") else 0) if live else 0
        s["/Alarms/HighDischargeCurrent"] = (1 if bms.get("modulesBlockingDischarge") else 0) if live else 0
        delta = cell_max - cell_min
        s["/Alarms/CellImbalance"] = (2 if delta > 0.100 else 1 if delta > 0.050 else 0) if live else 0
        s["/Alarms/InternalFailure"] = 2 if phase >= 1 else (2 if bms.get("modulesOffline") else 0)

        s["/System/MinCellVoltage"] = bms.get("minCellV") if live else None
        s["/System/MaxCellVoltage"] = bms.get("maxCellV") if live else None
        s["/System/MinCellTemperature"] = bms.get("minCellT") if live else None
        s["/System/MaxCellTemperature"] = bms.get("maxCellT") if live else None
        s["/System/MinVoltageCellId"] = bms.get("minVCellId") if live else None
        s["/System/MaxVoltageCellId"] = bms.get("maxVCellId") if live else None
        s["/System/MinTemperatureCellId"] = bms.get("minTCellId") if live else None
        s["/System/MaxTemperatureCellId"] = bms.get("maxTCellId") if live else None
        s["/System/NrOfModulesOnline"] = bms.get("modulesOnline")
        s["/System/NrOfModulesOffline"] = bms.get("modulesOffline")
        s["/System/NrOfModulesBlockingCharge"] = bms.get("modulesBlockingCharge")
        s["/System/NrOfModulesBlockingDischarge"] = bms.get("modulesBlockingDischarge")
        s["/History/ChargeCycles"] = bms.get("chargeCycles")
        if bms.get("serial"):
            s["/Serial"] = bms["serial"]
        if bms.get("fwVersion") is not None:
            s["/FirmwareVersion"] = str(bms["fwVersion"])
        if bms.get("hwVersion"):
            s["/HardwareVersion"] = bms["hwVersion"]

        s["/RecBms/Phase"] = phase_name
        s["/RecBms/EqStatus"] = eq_label
        s["/RecBms/TimeToFull"] = ttf
        force = (1 if bms.get("forceCharge") else 0) if live else 0
        s["/RecBms/ForceChargeRequest"] = force
        if c.forward_charge_request:
            s["/Info/ChargeRequest"] = force

        if phase_name != self.phase_name:
            log.info("phase %s -> %s (age %.0fs, %.1fV %.1fA %s%%, "
                     "CVL %.2fV CCL %sA DCL %sA)",
                     self.phase_name, phase_name, age, volts, amps, soc,
                     final_cvl, ccl, dcl)
            self.phase_name = phase_name
        return True


# ----------------------------------------------------------------------------
def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
    cfg = Config(cfg_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")
    log.info("dbus-recbms v%s starting (velib: %s)", VERSION, _VELIB_DIR)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    RecBmsDriver(cfg)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
