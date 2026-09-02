#!/usr/bin/env python3
"""
dbus-recbms — standalone Venus OS driver for the REC-BMS main bank.

Replaces the Node-RED "Virtual BMS" flow (archive/Virtual BMS.json). Runs as a
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
  - CVL from Max Charge slider (40-100% -> base+span mapping on the REC's
    SOC scale, clipped to max_v; the EQ boost may ride above the clip)
  - weekly 1h equalization boost (+0.44V), only while LIVE
  - synthetic alarms (REC-BMS sends no 0x35A frame)

v1.3.0 solar lead: the Quattro's absorption holds +0.05..0.15V ABOVE its
commanded CVL (measured 2026-08-19 with SVS on and the BMS/Quattro/sense
meters agreeing to 10mV), so the driver commands the Quattro solar_lead_v
below the slider/EQ target and raises only the solar chargers back up to it
via the systemcalc SolarVoltageOffset — the MPPTs, which regulate
accurately, finish the top-off at the true target.

v1.4.0 lead verification: systemcalc applies the Debug voltage offsets only
when /Settings/System/AccessLevel > 2 (Superuser) — and it evaluates that
once per process (reify). The D-Bus write succeeds regardless, so v1.3 could
believe a lead was in force while the MPPTs were actually held at target -
lead. The driver now compares com.victronenergy.system
/Control/EffectiveChargeVoltage (the voltage DVCC really sends the MPPTs)
against what it expects; on a sustained mismatch it publishes the FULL
target, refuses boosts, raises /Alarms/InternalFailure to warning and
explains itself in /RecBms/LeadFault. It also pins
/Settings/SystemSetup/BmsInstance to its own instance when that is still on
automatic, so no other battery service can take over the CVL.

v1.5.0 sustain: a request-and-forget control (/RecBms/Sustain/Request, same
shape as the solar boost) that makes the driver read the Max Charge slider as
the PRESENT SOC instead of its set value -- so the chargers hold the bank
where it is rather than moving it. Mode 1 (floor) lets the held SOC ratchet
upward only, mode 2 (ceiling) downward only, and both stay inside the real
slider target. Solar Priority's one-way charge/discharge uses it so that
shore only ever sustains the bank while solar (or the loads) do the moving.
It expires by itself after [sustain] hold_s and dies with the process, so a
dead requester can never leave the charger pinned. The slider value itself
is published as /RecBms/TargetSoc.

v1.3.1 capacity fix: 0x35F bytes 4-5 are the capacity CONFIGURED in the BMS
(1400 Ah), not a firmware version — reading them as one published a bogus
"1400" to /FirmwareVersion. They now feed /RecBms/ConfiguredCapacity, and
/FirmwareVersion has no source. /InstalledCapacity is unchanged: it stays on
0x379 ("BatterySize", 1440 Ah rated), the frame the protocol designates for it.

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
import atexit
import signal
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "1.5.0"
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
        # forward the BMS 0x360 flag to /Info/ChargeRequest — off by
        # default: this REC holds byte 0 at a constant 0xFF (capability
        # flag, not a request), and Venus only uses the path for the
        # GUI "Recharge" state (systemstate.py; dvcc ignores it)
        self.forward_charge_request = \
            str(b.get("forward_charge_request", "false")).lower() == "true"
        # pin /Settings/SystemSetup/BmsInstance to our instance when it is
        # still on automatic (-1); never override an explicit user choice
        self.pin_bms_instance = \
            str(b.get("pin_bms_instance", "true")).lower() != "false"

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
        # piecewise-linear slider% -> CVL breakpoints "pct:volts, pct:volts, ..."
        curve = v.get("curve", "40:54.42, 62.3:56.65, 100:62.70")
        self.cvl_curve = sorted(
            (float(p.split(":")[0]), float(p.split(":")[1]))
            for p in curve.split(",") if p.strip()
        )
        if len(self.cvl_curve) < 2:
            raise ValueError("[cvl] curve needs at least two pct:volts points")
        self.cvl_max = float(v.get("max_v", 61.96))
        self.eq_boost = float(v.get("eq_boost_v", 0.44))
        self.eq_interval_s = float(v.get("eq_interval_days", 7)) * 86400
        self.eq_duration_s = float(v.get("eq_duration_min", 60)) * 60
        # standing Quattro/solar split: command the vebus this far below the
        # target and raise only the MPPTs back to it (0 disables)
        self.solar_lead = max(0.0, min(0.30, float(v.get("solar_lead_v", 0.0))))
        # verify the offset against systemcalc /Control/EffectiveChargeVoltage:
        # a mismatch must persist this long before it counts (DVCC only
        # adjusts every 3 s, so a slider move is briefly inconsistent)
        self.lead_verify_s = float(v.get("lead_verify_s", 10))
        self.lead_fault_alarm = \
            str(v.get("lead_fault_alarm", "true")).lower() != "false"

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

        sb = cp["solarboost"] if cp.has_section("solarboost") else {}
        self.boost_enabled = str(sb.get("enabled", "true")).lower() != "false"
        self.boost_max_v = float(sb.get("max_boost_v", 0.30))
        self.boost_hold_s = float(sb.get("hold_s", 120))
        self.boost_measure_start_s = float(sb.get("measure_start_s", 75))
        self.boost_measure_len_s = float(sb.get("measure_len_s", 30))
        self.boost_cell_max_v = float(sb.get("cell_max_v", 4.05))
        self.boost_cell_min_t = float(sb.get("cell_min_t", 5))
        self.boost_cell_max_t = float(sb.get("cell_max_t", 45))
        self.boost_ceiling_v = float(sb.get("ceiling_v", 62.70))
        self.boost_min_margin_v = float(sb.get("min_margin_v", 0.10))
        self.boost_service = sb.get("target_service", "com.victronenergy.system")
        self.boost_path = sb.get(
            "target_path", "/Debug/BatteryOperationalLimits/SolarVoltageOffset")

        su = cp["sustain"] if cp.has_section("sustain") else {}
        self.sustain_enabled = str(su.get("enabled", "true")).lower() != "false"
        self.sustain_hold_s = float(su.get("hold_s", 120))
        self.sustain_step = max(0.0, float(su.get("step_pct", 1)))


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
        # Victron CAN-BMS "BatteryInfo". Bytes 2-3 are a version pair read as
        # two plain numbers, NOT little-endian (REC labels them the hardware
        # version; independent REC-Q work reads the same bytes as the software
        # version, e.g. 02 06 -> 2.6 — the label is unsettled, the encoding is
        # not). Bytes 4-5 are the capacity CONFIGURED in the BMS, in Ah, and
        # were previously misread here as a firmware version, which published
        # a bogus "1400" to /FirmwareVersion.
        bms["chemistry"] = data[0]
        bms["hwVersion"] = "%d.%d" % (data[2], data[3])
        bms["configuredAh"] = _u16(data, 4)
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
        # Victron CAN-BMS "BatterySize" — the RATED/installed capacity, which
        # is the figure Venus expects at /InstalledCapacity. Constant 1440 on
        # this bank regardless of SOC (the old flow mislabeled it as remaining
        # capacity). Distinct from 0x35F's configured capacity (1400).
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


SUSTAIN_FLOOR = 1      # held SOC may only rise (solar charges, shore holds)
SUSTAIN_CEILING = 2    # held SOC may only fall (loads drain, nothing charges)


def sustain_ratchet(mode, held, soc, slider, lo, hi, step=1.0):
    """(held, effective) for this tick. Pure, so it can be tested off the boat.

    mode      SUSTAIN_FLOOR or SUSTAIN_CEILING
    held      the ratchet so far: the highest (floor) / lowest (ceiling) SOC
              seen since the hold began; carried unclipped so a slider that
              is moved back out of the way restores it
    soc       the bank's present SOC, or None when the BMS is not live
    slider    the real Max Charge slider; the CVL never crosses it
    lo/hi     the slider's range (the CVL curve is only calibrated inside it)
    step      hysteresis: the ratchet moves only once the bank is a full
              step past it. Without it every charger burst that nudged the
              hi-res SOC by a few hundredths would lift the CVL a few
              millivolts and the next burst would start from there.
    effective the SOC the CVL curve is evaluated at
    """
    if soc is not None:
        if mode == SUSTAIN_FLOOR and soc >= held + step:
            held = soc
        elif mode == SUSTAIN_CEILING and soc <= held - step:
            held = soc
    # a floor never asks for more than the owner set; a ceiling never for less
    eff = min(held, slider) if mode == SUSTAIN_FLOOR else max(held, slider)
    return held, max(lo, min(hi, eff))


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
        # Solar-boost state. Reset at startup so a driver restart can never
        # inherit a boost left behind by a crash (the offset itself lives in
        # systemcalc and would otherwise survive us); the standing solar
        # lead, by contrast, is (re)established immediately.
        self.boost = {"active": False, "req_ts": 0.0, "volts": 0.0}
        # Sustain (v1.5.0): read the slider as the present SOC. Never
        # persisted: like the boost it is re-requested by its owner and
        # expires on its own, so a restart always comes up on the real slider.
        self.sustain = {"active": False, "mode": 0, "req_ts": 0.0,
                        "soc": None, "logged_soc": None}
        self.last_target = None
        self._last_offset_warn = 0.0
        # Lead verification (v1.4.0): what DVCC actually sends the MPPTs
        self.eff_cv = None                  # (volts or None, ts) from systemcalc
        self._last_pub_cvl = None           # /Info/MaxChargeVoltage we published
        self._last_offset = 0.0             # offset we last wrote
        self.lead_fault = {"active": False, "since": 0.0, "msg": "",
                           "mismatch_since": 0.0}
        self._check_access_level()
        if cfg.pin_bms_instance:
            self._pin_bms_instance()
        self._boost_write(cfg.solar_lead, quiet=True)
        atexit.register(self._boost_shutdown)
        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(_sig, self._boost_signal)
            except (ValueError, OSError):
                pass

        GLib.timeout_add_seconds(cfg.extv_poll_s, self._poll_ext_voltage)
        GLib.timeout_add_seconds(3, self._poll_effective_cv)   # DVCC cadence
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
        # 0x35F bytes 4-5: the capacity configured in the BMS (1400 Ah here).
        # /InstalledCapacity stays on 0x379 ("BatterySize", 1440 Ah) — that is
        # the frame the Victron CAN-BMS protocol designates for it.
        svc.add_path("/RecBms/ConfiguredCapacity", None,
                     gettextcallback=fmt("Ah", 0))

        # Solar boost: a REQUEST-and-forget control that biases only the solar
        # chargers above the published CVL, so they out-regulate the Quattro
        # and run unthrottled (MppOperationMode 2 = output IS capacity).
        # Write volts to request, 0 to release. It ALWAYS expires by itself --
        # see _service_boost() -- so a requester that dies cannot leave the
        # bank charging high.
        svc.add_path("/RecBms/SolarBoost/Request", 0.0, writeable=True,
                     onchangecallback=self._boost_requested,
                     gettextcallback=v2)
        svc.add_path("/RecBms/SolarBoost/Applied", 0.0, gettextcallback=v2)
        svc.add_path("/RecBms/SolarBoost/Active", 0)
        svc.add_path("/RecBms/SolarBoost/SecondsLeft", 0,
                     gettextcallback=fmt_int("s"))
        svc.add_path("/RecBms/SolarBoost/WindowOpen", 0)
        svc.add_path("/RecBms/SolarBoost/EffectiveChargeVoltage", None,
                     gettextcallback=v2)
        svc.add_path("/RecBms/SolarBoost/Status", "idle")

        # Solar lead (v1.3.0): /Info/MaxChargeVoltage is what the Quattro is
        # commanded (target - lead); the true target and the lead actually in
        # force are published here for the flow and for diagnostics.
        svc.add_path("/RecBms/TargetChargeVoltage", None, gettextcallback=v2)
        svc.add_path("/RecBms/SolarLead", 0.0, gettextcallback=v2)
        # Lead verification (v1.4.0): "" while the systemcalc offset is
        # verified in force; otherwise a human-readable explanation. The
        # Solar Priority flow surfaces it with node.error().
        svc.add_path("/RecBms/LeadFault", "")
        svc.add_path("/RecBms/DvccEffectiveChargeVoltage", None,
                     gettextcallback=v2)

        # The Max Charge slider as this driver reads it each tick (the SOC
        # the CVL curve is evaluated at when nothing overrides it). Published
        # so a client can subscribe instead of scraping the switch service.
        svc.add_path("/RecBms/TargetSoc", None, gettextcallback=fmt("%", 0))

        # Sustain (v1.5.0): a request-and-forget control that makes the
        # driver interpret the slider as the PRESENT SOC, so the chargers
        # hold the bank where it is instead of moving it. Write 1 to hold a
        # floor (the held SOC follows the bank upward only -- solar may raise
        # it, the charger never lowers it), 2 to hold a ceiling (follows
        # downward only -- loads may lower it, nothing raises it), 0 to
        # release. Both stay inside the real slider target. It ALWAYS
        # expires after [sustain] hold_s -- see _service_sustain() -- so a
        # requester that dies cannot leave the bank pinned. Reads back -1
        # while a hold is active (see _service_sustain).
        svc.add_path("/RecBms/Sustain/Request", 0, writeable=True,
                     onchangecallback=self._sustain_requested)
        svc.add_path("/RecBms/Sustain/Active", 0)
        svc.add_path("/RecBms/Sustain/Mode", 0)
        svc.add_path("/RecBms/Sustain/Soc", None, gettextcallback=fmt("%", 1))
        svc.add_path("/RecBms/Sustain/SecondsLeft", 0,
                     gettextcallback=fmt_int("s"))
        svc.add_path("/RecBms/Sustain/Status", "idle")

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

    # ------------------------------------------------------------ solar boost
    # Raising ONLY the solar chargers above the Quattro's regulation point is
    # what makes them produce: with a shared CVL the Quattro wins the tie and
    # squeezes the MPPTs to zero (measured 2026-08-19 -- both chargers sat at
    # 0 W for 17 min while the Quattro pushed 1.3 kW from shore). Venus applies
    # /Debug/BatteryOperationalLimits/SolarVoltageOffset to the solar chargers
    # only (dbus-systemcalc-py delegates/dvcc.py), which is the lever.
    #
    # The BMS driver owns it because the offset is a deliberate excursion above
    # the CVL this same driver publishes -- so the clamp can be checked against
    # live cell data rather than a fixed guess.

    def _boost_write(self, volts, quiet=False):
        """Push the offset into systemcalc. Returns True on success."""
        c = self.cfg
        try:
            obj = shared_bus().get_object(c.boost_service, c.boost_path)
            obj.SetValue(dbus.Double(float(volts)),
                         dbus_interface="com.victronenergy.BusItem")
            return True
        except Exception as e:
            if not quiet:
                log.warning("solar boost: cannot write %s%s: %s",
                            c.boost_service, c.boost_path, e)
            return False

    def _boost_allowed(self, volts):
        """Safety gate, evaluated on request AND on every tick while active."""
        c = self.cfg
        if not c.boost_enabled:
            return False, "disabled in config"
        if self.lead_fault["active"]:
            return False, "solar lead fault (" + self.lead_fault["msg"] + ")"
        if self.sustain["active"] and self.sustain["mode"] == SUSTAIN_CEILING:
            # a boost charges the bank from solar; a ceiling hold exists
            # precisely so that nothing does
            return False, "sustain ceiling active"
        if volts <= 0 or volts > c.boost_max_v:
            return False, "%.2fV outside 0..%.2fV" % (volts, c.boost_max_v)
        bms = self.bms
        if "_lastUpdate" not in bms or                 (time.time() - bms["_lastUpdate"]) > c.live_timeout:
            return False, "BMS not live"
        cmax = bms.get("maxCellV")
        if cmax is None:
            return False, "no cell voltage"
        if cmax >= c.boost_cell_max_v:
            return False, "max cell %.3fV >= %.3fV" % (cmax, c.boost_cell_max_v)
        tmin, tmax = bms.get("minCellT"), bms.get("maxCellT")
        if tmin is None or tmax is None:
            return False, "no cell temperature"
        if tmin < c.boost_cell_min_t or tmax > c.boost_cell_max_t:
            return False, "cell temp %.0f..%.0fC outside %.0f..%.0fC" % (
                tmin, tmax, c.boost_cell_min_t, c.boost_cell_max_t)
        # Gates run against the TRUE target, not the published (lead-lowered)
        # Quattro command — the boosted solar ceiling is target + volts.
        target = self.last_target
        if target is None:
            return False, "no CVL published yet"
        if float(target) + volts > c.boost_ceiling_v:
            return False, "target %.2f + %.2f > ceiling %.2fV" % (
                target, volts, c.boost_ceiling_v)
        # The MPPTs ramp at a rate set by how far the bus sits below their
        # target. Measured 2026-08-19: ~0.15V of margin -> unthrottled in
        # 43-45 s, but only ~0.05V -> 126 s to reach 5 % of the step. With too
        # little margin the measurement window would open on an array that has
        # barely started, and that reading would be recorded as its capacity.
        # Refuse rather than return a number that is wrong and looks real.
        packv = self.batt["/Dc/0/Voltage"]
        if packv is None:
            return False, "no pack voltage"
        margin = (float(target) + volts) - float(packv)
        if margin < c.boost_min_margin_v:
            return False, "margin %.2fV < %.2fV (pack %.2f, target %.2f)" % (
                margin, c.boost_min_margin_v, packv, float(target) + volts)
        return True, ""

    def _boost_requested(self, path, value):
        try:
            volts = float(value)
        except (TypeError, ValueError):
            return False
        if volts <= 0:
            self._boost_clear("released by requester")
            return True
        ok, why = self._boost_allowed(volts)
        if not ok:
            log.warning("solar boost refused (%.2fV): %s", volts, why)
            self.batt["/RecBms/SolarBoost/Status"] = "refused: " + why
            return False
        if not self._boost_write(self.cfg.solar_lead + volts):
            self.batt["/RecBms/SolarBoost/Status"] = "refused: systemcalc write failed"
            return False
        self.boost = {"active": True, "req_ts": time.time(), "volts": volts}
        self.batt["/RecBms/SolarBoost/Applied"] = round(volts, 2)
        self.batt["/RecBms/SolarBoost/Active"] = 1
        self.batt["/RecBms/SolarBoost/Status"] = "ramp"
        log.info("solar boost +%.2fV for %.0fs (measure %.0f..%.0fs)", volts,
                 self.cfg.boost_hold_s, self.cfg.boost_measure_start_s,
                 self.cfg.boost_measure_start_s + self.cfg.boost_measure_len_s)
        return True

    def _boost_clear(self, reason):
        was = self.boost["active"]
        self.boost = {"active": False, "req_ts": 0.0, "volts": 0.0}
        self._boost_write(self.cfg.solar_lead)   # keep the standing lead
        s = self.batt
        s["/RecBms/SolarBoost/Request"] = 0.0
        s["/RecBms/SolarBoost/Applied"] = 0.0
        s["/RecBms/SolarBoost/Active"] = 0
        s["/RecBms/SolarBoost/SecondsLeft"] = 0
        s["/RecBms/SolarBoost/WindowOpen"] = 0
        s["/RecBms/SolarBoost/Status"] = reason
        s["/RecBms/SolarBoost/EffectiveChargeVoltage"] = \
            self.last_target if self.last_target is not None \
            else s["/Info/MaxChargeVoltage"]
        if was:
            log.info("solar boost cleared (%s)", reason)

    # ---------------------------------------------------------- sustain
    # "Interpret the Max Charge slider as the present SOC." The slider ->
    # CVL curve is calibrated on settled holds, so evaluating it at the SOC
    # the bank is AT gives the voltage at which the chargers neither fill
    # nor drain it. The held SOC ratchets in one direction only, so a
    # sustained bank can still be moved the way its owner wants (solar in
    # floor mode, loads in ceiling mode) and never the other way. The BMS
    # driver owns it because the SOC, the curve and the slider all live here.

    def _live_soc(self):
        bms = self.bms
        if "_lastUpdate" not in bms or \
                (time.time() - bms["_lastUpdate"]) > self.cfg.live_timeout:
            return None
        soc = bms["socHiRes"] if bms.get("socHiRes") is not None else bms.get("soc")
        return float(soc) if soc is not None else None

    def _sustain_requested(self, path, value):
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return False
        if mode == 0:
            self._sustain_clear("released by requester")
            return True
        if mode not in (SUSTAIN_FLOOR, SUSTAIN_CEILING):
            return False
        if not self.cfg.sustain_enabled:
            self.batt["/RecBms/Sustain/Status"] = "refused: disabled in config"
            return False
        su = self.sustain
        if su["active"] and su["mode"] == mode:
            # the owner re-asserting its hold: keep the ratchet, refresh expiry
            su["req_ts"] = time.time()
            return True
        soc = self._live_soc()
        if soc is None:
            log.warning("sustain refused (mode %d): BMS not live", mode)
            self.batt["/RecBms/Sustain/Status"] = "refused: BMS not live"
            return False
        self.sustain = {"active": True, "mode": mode, "req_ts": time.time(),
                        "soc": soc, "logged_soc": soc}
        s = self.batt
        s["/RecBms/Sustain/Active"] = 1
        s["/RecBms/Sustain/Mode"] = mode
        s["/RecBms/Sustain/Soc"] = round(soc, 1)
        s["/RecBms/Sustain/Status"] = "floor" if mode == SUSTAIN_FLOOR else "ceiling"
        log.info("sustain %s at %.1f%% (slider read as the present SOC; "
                 "expires in %.0fs unless re-asserted)",
                 "floor" if mode == SUSTAIN_FLOOR else "ceiling", soc,
                 self.cfg.sustain_hold_s)
        return True

    def _sustain_clear(self, reason):
        was = self.sustain["active"]
        held = self.sustain["soc"]
        self.sustain = {"active": False, "mode": 0, "req_ts": 0.0,
                        "soc": None, "logged_soc": None}
        s = self.batt
        s["/RecBms/Sustain/Request"] = 0
        s["/RecBms/Sustain/Active"] = 0
        s["/RecBms/Sustain/Mode"] = 0
        s["/RecBms/Sustain/Soc"] = None
        s["/RecBms/Sustain/SecondsLeft"] = 0
        s["/RecBms/Sustain/Status"] = reason
        if was:
            log.info("sustain cleared at %.1f%% (%s); slider back in force",
                     held if held is not None else -1, reason)

    def _service_sustain(self, now, soc, slider):
        """Runs every tick. Expires the hold, ratchets the held SOC in the
        hold's direction, keeps it inside the real slider target and
        publishes the telemetry. Returns the SOC the CVL curve should be
        evaluated at, or None when the slider itself applies."""
        c = self.cfg
        su = self.sustain
        if not su["active"]:
            return None
        elapsed = now - su["req_ts"]
        if elapsed >= c.sustain_hold_s:
            self._sustain_clear("expired after %.0fs" % c.sustain_hold_s)
            return None
        # velib's SetValue short-circuits a write of the value the path
        # already holds (no callback), so a re-assert of the same mode would
        # never refresh the expiry, and a release (0) would be lost if the
        # path read 0. Reading the request back as -1 while active makes
        # every 0/1/2 write a change; the state lives in /Active and /Mode.
        self.batt["/RecBms/Sustain/Request"] = -1
        su["soc"], held = sustain_ratchet(su["mode"], su["soc"], soc, slider,
                                          c.slider_min, c.slider_max,
                                          c.sustain_step)
        if held != su["logged_soc"]:
            log.info("sustain %s now at %.1f%%",
                     "floor" if su["mode"] == SUSTAIN_FLOOR else "ceiling", held)
            su["logged_soc"] = held
        s = self.batt
        s["/RecBms/Sustain/Soc"] = round(held, 1)
        s["/RecBms/Sustain/SecondsLeft"] = int(c.sustain_hold_s - elapsed)
        return held

    # ------------------------------------------------ lead verification
    def _settings_get(self, path):
        try:
            return self.sbus.call_blocking(
                "com.victronenergy.settings", path, BUSITEM, "GetValue", "",
                [], timeout=2)
        except Exception:
            return None

    def _check_access_level(self):
        """systemcalc applies the Debug voltage offsets only when
        /Settings/System/AccessLevel > 2 (Superuser), evaluated ONCE per
        systemcalc process. Warn at startup; the per-tick verification
        below catches it either way."""
        lvl = self._settings_get("/Settings/System/AccessLevel")
        try:
            lvl = int(lvl)
        except (TypeError, ValueError):
            log.warning("cannot read /Settings/System/AccessLevel")
            return
        # Upstream dvcc.py (master, 2026-08) gates the offsets on level > 2;
        # the boat's firmware applied them at level 2 (verified 2026-08-21),
        # so this is informational — the per-tick verification decides.
        log.info("GX access level %d (upstream dvcc.py applies the Debug "
                 "offsets only above 2; offset verification below is "
                 "authoritative)", lvl)

    def _pin_bms_instance(self):
        """/Settings/SystemSetup/BmsInstance: -1 = automatic (lowest
        instance among battery services publishing /Info/MaxChargeVoltage),
        -255 = no BMS. Pin it to us while it is automatic so no future
        battery service can take over the CVL; never override an explicit
        choice."""
        path = "/Settings/SystemSetup/BmsInstance"
        cur = self._settings_get(path)
        try:
            cur = int(cur)
        except (TypeError, ValueError):
            log.warning("cannot read %s; not pinning", path)
            return
        want = self.batt_instance
        if cur == want:
            return
        if cur == -1:
            try:
                self.sbus.call_blocking(
                    "com.victronenergy.settings", path, BUSITEM, "SetValue",
                    "v", [dbus.Int32(want)], timeout=5)
                log.info("%s: automatic -> pinned to %d", path, want)
            except Exception as e:
                log.warning("%s: could not pin to %d (%s)", path, want, e)
        elif cur == -255:
            log.warning("%s is -255 (BMS control DISABLED): DVCC is not "
                        "passing our limits to any charger", path)
        else:
            log.warning("%s is %d (explicit choice, not us); leaving it",
                        path, cur)

    def _poll_effective_cv(self):
        try:
            raw = self.sbus.call_blocking(
                self.cfg.boost_service, "/Control/EffectiveChargeVoltage",
                BUSITEM, "GetValue", "", [], timeout=2)
            v = float(raw)
            if not (20 <= v <= 80):
                v = None
        except Exception:
            v = None
        self.eff_cv = (v, time.time())
        self.batt["/RecBms/DvccEffectiveChargeVoltage"] = v
        return True

    def _verify_lead(self, now):
        """Compare what DVCC really sends the MPPTs against what we expect
        from the CVL we published and the offset we wrote last tick.
        Returns True while the offset is proven (or cannot be judged),
        False once a mismatch has persisted lead_verify_s."""
        c = self.cfg
        f = self.lead_fault
        pub, off = self._last_pub_cvl, self._last_offset
        if pub is None or off <= 0.005:
            return not f["active"]          # nothing to verify this tick
        v, ts = self.eff_cv if self.eff_cv else (None, 0.0)
        stale = (now - ts) > 15
        applied = v is not None and abs(v - (pub + off)) <= 0.015
        ignored = v is not None and abs(v - pub) <= 0.015
        if applied:
            f["mismatch_since"] = 0.0
            if f["active"]:
                log.info("solar lead: systemcalc offset verified in force "
                         "again (effective %.2fV); fault cleared", v)
                f["active"] = False
                f["msg"] = ""
            return True
        if ignored or v is None or stale:
            if not f["mismatch_since"]:
                f["mismatch_since"] = now
            elif now - f["mismatch_since"] >= c.lead_verify_s and not f["active"]:
                lvl = self._settings_get("/Settings/System/AccessLevel")
                if v is None or stale:
                    msg = ("systemcalc /Control/EffectiveChargeVoltage "
                           "unavailable (DVCC off or systemcalc down?)")
                else:
                    msg = ("systemcalc ignores the solar offset: MPPTs get "
                           "%.2fV, expected %.2fV. GX access level is %s "
                           "(need 3 = Superuser); after raising it run "
                           "'svc -t /service/dbus-systemcalc-py'"
                           % (v, pub + off, lvl))
                f.update(active=True, since=now, msg=msg)
                log.error("SOLAR LEAD FAULT: %s -- publishing the full "
                          "target, boosts refused", msg)
            return not f["active"]
        # neither matches: a transient (slider just moved, DVCC mid-cycle)
        return not f["active"]

    def _service_boost(self, now, target):
        """Runs every tick, after the target CVL for this tick is known.
        Maintains the systemcalc offset = standing lead + any active boost
        (the path is volatile, so it is re-asserted every tick), expires or
        aborts the boost, and publishes the boost telemetry. Returns the
        lead actually in force, so the caller can publish target - lead as
        the Quattro's CVL."""
        c = self.cfg
        b = self.boost
        s = self.batt
        self.last_target = target
        boost_v = 0.0
        if b["active"]:
            elapsed = now - b["req_ts"]
            if elapsed >= c.boost_hold_s:
                self._boost_clear("expired after %.0fs" % c.boost_hold_s)
            else:
                ok, why = self._boost_allowed(b["volts"])
                if not ok:
                    self._boost_clear("aborted: " + why)
                else:
                    boost_v = b["volts"]
                    win_from = c.boost_measure_start_s
                    win_to = win_from + c.boost_measure_len_s
                    window = win_from <= elapsed < win_to
                    s["/RecBms/SolarBoost/SecondsLeft"] = int(c.boost_hold_s - elapsed)
                    s["/RecBms/SolarBoost/WindowOpen"] = 1 if window else 0
                    s["/RecBms/SolarBoost/Status"] = (
                        "measure" if window
                        else ("ramp" if elapsed < win_from else "settling"))
        lead = 0.0
        verified = self._verify_lead(now)
        if not verified and boost_v > 0:
            self._boost_clear("aborted: solar lead fault")
            boost_v = 0.0
        if c.solar_lead > 0 or boost_v > 0:
            # Keep writing the offset even while faulted: if the access
            # level is raised and systemcalc restarted, the next poll sees
            # the offset applied and the fault self-clears.
            if self._boost_write(c.solar_lead + boost_v, quiet=True):
                self._last_offset = c.solar_lead + boost_v
                # While faulted publish the FULL target (lead 0): the MPPT
                # ceiling is never silently lowered by a lead that is not
                # actually in force.
                lead = c.solar_lead if verified else 0.0
            else:
                # Unwritable offset (Debug path — may vanish in a Venus
                # update): publish the FULL target as the CVL so the MPPT
                # ceiling is never silently lowered by a lead that is not
                # actually in force. A boost cannot be honored either.
                if boost_v > 0:
                    self._boost_clear("aborted: systemcalc write failed")
                    boost_v = 0.0
                self._last_offset = 0.0
                if now - self._last_offset_warn > 60:
                    log.warning("solar lead: cannot write systemcalc offset; "
                                "publishing the full target CVL")
                    self._last_offset_warn = now
        else:
            self._last_offset = 0.0
        s["/RecBms/SolarBoost/EffectiveChargeVoltage"] = round(target + boost_v, 2)
        s["/RecBms/LeadFault"] = self.lead_fault["msg"] if self.lead_fault["active"] else ""
        return lead

    def _boost_shutdown(self):
        if self.boost.get("active"):
            log.info("solar boost released on shutdown")
        self._boost_write(0.0, quiet=True)

    def _boost_signal(self, signum, frame):
        self._boost_shutdown()
        raise SystemExit(0)

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
        # Piecewise-linear on the REC's own SOC<->voltage scale (the NMC
        # mid-plateau is flatter than the 62->100% region, so a single slope
        # can't hold both ends), clipped to cvl_max: the published charge
        # voltage never exceeds it — only the equalization boost may ride
        # on top. Breakpoints come from measured ~0A hold equilibria plus
        # the REC's 100% sync point; add new points to [cvl] curve as more
        # holds settle.
        c = self.cfg
        pts = c.cvl_curve
        if slider <= pts[0][0]:
            cvl = pts[0][1]
        elif slider >= pts[-1][0]:
            cvl = pts[-1][1]
        else:
            for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
                if slider <= p1:
                    cvl = v0 + (slider - p0) / (p1 - p0) * (v1 - v0)
                    break
        return min(cvl, c.cvl_max)

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

        # 0x355 bytes 4-5 carry SOC at 0.01% resolution — prefer it
        soc = bms["socHiRes"] if bms.get("socHiRes") is not None else v("soc")

        # ---- CVL control: slider (or sustain) + weekly equalization ----
        slider = float(self.settings["chargeslider"] or c.slider_default)
        # Sustain (v1.5.0): while held, the curve is evaluated at the SOC the
        # bank is at, not at the slider. The BMS' own SOC is used even in
        # fallback (it is the last value the BMS sent; the ratchet simply
        # stops moving), never the safe substitute.
        held = self._service_sustain(now, soc if live else None, slider)
        slider_cvl = self._slider_cvl(held if held is not None else slider)
        eq = self.eq
        eq_last = float(self.settings["eqlast"] or 0)
        # An equalization is a deliberate charge from shore; it waits while
        # a sustain hold is in force (eqlast is untouched, so it stays due).
        eq_eligible = live and held is None
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
                log.warning("equalization aborted (%s)",
                            "sustain hold" if live else "BMS not live")
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
        # The Quattro's absorption holds +0.05..0.15V ABOVE its commanded
        # CVL (measured 2026-08-19: SVS on, BMS/Quattro/sense meters all
        # within 10mV, VebusChargeState=absorption, pack held steady at
        # CVL+0.07V while the tail decayed). The MPPTs regulate accurately,
        # so command the Quattro solar_lead_v BELOW the target and raise
        # only the solar chargers back up to it: the Quattro lands at or
        # under the calibrated equilibrium and solar finishes the top-off.
        target = round(final_cvl, 2)
        lead = self._service_boost(now, target)
        s["/RecBms/TargetChargeVoltage"] = target
        s["/RecBms/TargetSoc"] = slider
        s["/RecBms/SolarLead"] = round(lead, 2)
        s["/Info/MaxChargeVoltage"] = round(target - lead, 2)
        self._last_pub_cvl = round(target - lead, 2)
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
        # Solar lead fault (v1.4.0): warning level on a standard alarm path
        # so the GUI notifies and a VRM alarm rule can mail; detail string
        # in /RecBms/LeadFault
        if c.lead_fault_alarm and self.lead_fault["active"]:
            s["/Alarms/InternalFailure"] = max(s["/Alarms/InternalFailure"], 1)

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
        # /FirmwareVersion has no source: 0x35F bytes 4-5 are capacity, and
        # the only version the BMS sends is the bytes 2-3 pair below.
        if bms.get("hwVersion"):
            s["/HardwareVersion"] = bms["hwVersion"]
        s["/RecBms/ConfiguredCapacity"] = bms.get("configuredAh")

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
