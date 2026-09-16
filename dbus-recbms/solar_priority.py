#!/usr/bin/env python3
"""Solar Priority policy consumer for REC protocol version 2.

The restored five-state engine owns operating decisions and transfer intentions. The REC
publisher owns measured energy, charger regulation and physical relay writes.
D-Bus observations are invalidated explicitly and actively verified even when
healthy measurements remain constant. User enable/rated-PV settings retain
the existing service and device instances.
"""

import atexit
import json
import configparser
import glob
import logging
import math
import os
import platform
import signal
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "3.5.6"
ENGINE_VERSION = "4.24-restored"
BUSITEM = "com.victronenergy.BusItem"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from control_inputs import normalize_bus_snapshot, SourceRegistry, TimedMean, selected_battery_matches
from solar_engine import Engine, Inputs, Val, ENGINE_DEFAULTS
from policy_contract import dumps, VERSION as PROTOCOL_VERSION, resolve_shore_input

log = logging.getLogger("dbus-solarpriority")

# The engine's charge intent as dbus-recbms' sustain mode. Stage B (engine
# 4.15, master D03/SP23): at the destination HOLD asks for the two-sided
# hold (3) rather than releasing the bank to the slider.
SUSTAIN = {'release': 0, 'floor': 1, 'ceiling': 2, 'hold': 3}


# ----------------------------------------------------------------------------
# velib_python (same lookup as dbus_recbms.py)
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
from dbusmonitor import DbusMonitor         # noqa: E402


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
class Config:
    def __init__(self, path):
        # inline ";" comments are used in solar_priority.ini
        cp = configparser.ConfigParser(interpolation=None,
                                       inline_comment_prefixes=(";", "#"))
        cp.read(path)
        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        s = cp["service"] if cp.has_section("service") else {}
        self.suffix = s.get("service_suffix", "solarpriority")
        self.settings_id = s.get("settings_id", "solarpriority")
        self.instance = int(s.get("instance", 221))
        self.group = s.get("group", "BMS")
        self.toggle_name = s.get("toggle_name", "Solar Priority")
        self.slider_name = s.get("slider_name", "PV Capacity")
        self.rated_min = float(s.get("rated_min_w", 100))
        self.rated_max = float(s.get("rated_max_w", 2500))
        self.rated_step = float(s.get("rated_step_w", 50))
        self.rated_default = float(s.get("rated_default_w", 1800))

        i = cp["inputs"] if cp.has_section("inputs") else {}
        self.mppt6_instance = int(i.get("mppt_a_instance", 278))
        self.mppt7_instance = int(i.get("mppt_b_instance", 279))
        self.vebus_instance = int(i.get("vebus_instance", 276))
        self.battery_instance = int(i.get("battery_instance", 200))
        # 3.5.0: 'auto' (default) resolves the shore input at runtime --
        # REC's published resolution first, else the GX's AC input types,
        # else the Quattro's own facts (policy_contract.resolve_shore_input);
        # 1 or 2 pins it.
        raw = str(i.get("shore_ac_input", "auto")).strip().lower()
        self.ac_in = "auto" if raw in ("", "auto") else int(raw)
        if self.ac_in not in ("auto", 1, 2):
            raise ValueError("inputs shore_ac_input must be auto, 1 or 2")
        self.tick_ms = int(i.get("tick_ms", 1000))
        # EstimateW / NeedW are published in steps of this many watts, and a
        # tick's changes go out as one ItemsChanged (2026-09-05: at 0.1 W
        # these two were a bus signal a second each for nothing).
        self.power_step = float(i.get("power_step", 5))

        e = cp['engine'] if cp.has_section('engine') else {}
        self.engine = {k: float(e.get(k.lower(), default)) for k, default in ENGINE_DEFAULTS.items()}
        if any(not math.isfinite(v) or v < 0 for v in self.engine.values()):
            raise ValueError('invalid engine threshold')
        # The protocol maps a target of 100 % to COMPLETE_FULL, which must
        # release sustain (policy_contract), so the engine must stand one-way
        # down at 100 % too: a full threshold of 0 (off) or above 100 had it
        # request a floor there, and every such request was rejected
        # ("disabled/full mode must release sustain", E12). Refuse the
        # configuration before operation rather than run without authority.
        full = self.engine['ONEWAY_FULL_PCT']
        if not 0 < full <= 100:
            raise ValueError('oneway_full_pct must be within 1..100 (got %g): the protocol maps a '
                             '100 %% target to COMPLETE_FULL, which releases sustain' % full)


def _bus(private=False):
    """Use a shared reader connection and a private exported-service connection."""
    factory = dbus.SessionBus if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.SystemBus
    return factory(private=private)


def _q(value, step):
    """Round to the nearest multiple of `step`; None passes through.

    Integer steps give ints, fractional ones a float rounded to the step's
    own number of decimals so 0.1 never comes back as 0.30000000000000004.
    """
    if value is None:
        return None
    n = round(value / step) * step
    if float(step).is_integer():
        return int(round(n))
    return round(n, max(0, -int(math.floor(math.log10(step)))))


def new_service(name, bus):
    import inspect
    if "register" in inspect.signature(VeDbusService.__init__).parameters:
        svc = VeDbusService(name, bus=bus, register=False)
        svc._sp_needs_register = True
    else:
        svc = VeDbusService(name, bus=bus)
        svc._sp_needs_register = False
    return svc


def register_service(svc):
    if getattr(svc, "_sp_needs_register", False):
        svc.register()


# (service class, path) -> (input field, validator)
def _rng(lo, hi):
    return lambda v: lo <= v <= hi


INPUT_MAP = {
    ("solarcharger", "/Pv/V"):            ("voc", _rng(0, 150)),
    ("solarcharger", "/Yield/Power"):     ("y", _rng(0, 3000)),
    ("solarcharger", "/MppOperationMode"): ("m", lambda v: True),
    ("system", "/Ac/Consumption/L1/Power"): ("load_now", _rng(0, 20000)),
    ("system", "/Dc/Battery/Soc"):        ("soc", _rng(0, 100)),
    ("system", "/Dc/Battery/Power"):      ("batt", lambda v: abs(v) <= 30000),
    ("system", "/Dc/Battery/Voltage"):    ("batt_v", _rng(20, 80)),
    ("system", "/Dc/System/Power"):       ("dc_load", _rng(-5000, 5000)),
    ("vebus", "/Ac/ActiveIn/ActiveInput"): ("feed", lambda v: True),
    # Stage A (master D14/SP56): availability is not acceptance. This
    # firmware publishes /Ac/State/AcIn1Available (1 tonight),
    # /Ac/State/AcIn2Available (0), /Ac/State/IgnoreAcIn1|2 and
    # /Ac/ActiveIn/Connected; /Ac/In/1/Connected does not exist on it. Both
    # inputs are kept and the tick derives the engine's ac_available from
    # the resolved shore input (3.5.0); a firmware without the path leaves
    # it None -- unknown, never absent.
    ("vebus", "/Ac/State/AcIn1Available"): ("ac1_available", lambda v: v in (0, 1)),
    ("vebus", "/Ac/State/AcIn2Available"): ("ac2_available", lambda v: v in (0, 1)),
    # The GX's AC input types (0 none, 1 grid, 2 generator, 3 shore): the
    # owner's own statement of which input is shore, read from localsettings.
    ("settings", "/Settings/SystemSetup/AcInput1"): ("ac1_type", lambda v: v in (0, 1, 2, 3)),
    ("settings", "/Settings/SystemSetup/AcInput2"): ("ac2_type", lambda v: v in (0, 1, 2, 3)),
    ("vebus", "/Ac/Out/L1/P"):            ("ac_out", _rng(-20000, 20000)),
    ("battery", "/RecBms/TargetChargeVoltage"): ("cvl", _rng(20, 80)),
    # 4.19: the charge current limit in force; under a small one the yield
    # says nothing about the sun and the boost is gated on daylight alone.
    ("battery", "/Info/MaxChargeCurrent"):      ("ccl_a", _rng(0, 2000)),
    ("battery", "/RecBms/SolarBoost/Active"):   ("boost_active", lambda v: True),
    ("battery", "/RecBms/SolarBoost/WindowOpen"): ("boost_window", lambda v: True),
    ("battery", "/RecBms/SolarBoost/EffectiveChargeVoltage"): ("boost_eff", _rng(20, 80)),
    ("battery", "/RecBms/SolarLead"):     ("lead", _rng(0, 1)),
    # Slider destination remains separate from the acknowledged policy snapshot.
    ("battery", "/RecBms/TargetSoc"):     ("target_soc", _rng(0, 100)),
    ("battery", "/RecBms/Sustain/Active"): ("sustain_active", lambda v: True),
}
WRITE_PATHS = {
    "vebus": ["/Dc/0/Voltage", "/Dc/0/Current", "/Connected"],
    "battery": ["/RecBms/Policy/Request", "/RecBms/Policy/Version",
                "/RecBms/Policy/Generation", "/RecBms/Policy/Status",
                "/RecBms/Policy/Snapshot", "/RecBms/LeadFault"],
    "system": ["/ActiveBatteryService", "/ActiveBmsService", "/Dc/Battery/BatteryService",
               "/ActiveBmsInstance", "/Dc/System/MeasurementType"],
}


class SolarPriorityDriver:
    REQUEST_FAILURES_TO_SHORE = 3   # consecutive refusals before the engine stands down

    def __init__(self, cfg):
        self.cfg = cfg
        self.inp = Inputs()
        self.shore_input = cfg.ac_in if cfg.ac_in in (1, 2) else None
        self.shore_input_reason = "configured" if self.shore_input else ""
        self.engine = Engine(cfg.engine, self._ms(), self._engine_log)
        self.generation = None
        self.request_id = 0
        self.last_limited_by = ""
        self.sources = SourceRegistry()
        self.load_mean = TimedMean(cfg.engine["LOAD_AVG_MS"] / 1000)
        self.load_slow_mean = TimedMean(cfg.engine["LOAD_SLOW_MS"] / 1000)
        # 4.13: the same two means over REC's complete DC-bus island demand
        self.demand_mean = TimedMean(cfg.engine["LOAD_AVG_MS"] / 1000)
        self.demand_slow_mean = TimedMean(cfg.engine["LOAD_SLOW_MS"] / 1000)
        self.field_sources = {}
        self.last_verify = None
        self.read_issued = {}
        self.read_applied = {}
        self.last_request = None
        # issue #8: an evaluated failed probe waiting to reach REC's durable
        # backoff, and the first request id that carried it.
        self.failed_probe = False
        self.failed_probe_id = None
        self.failed_requests = 0
        self.sbus = _bus()

        self._init_settings()
        self.inp.enabled = bool(int(self.settings["enabled"]))
        self.inp.p_rated = float(self.settings["rated"])
        self._init_switch_service()

        dummy = {"code": None, "whenToLog": "configChange", "accessLevel": None}
        tree = {}
        for (cls, path) in INPUT_MAP:
            tree.setdefault("com.victronenergy." + cls, {})[path] = dummy
        for cls, paths in WRITE_PATHS.items():
            for p in paths:
                tree.setdefault("com.victronenergy." + cls, {})[p] = dummy
        for cls in ("solarcharger", "vebus", "battery"):
            tree["com.victronenergy." + cls]["/DeviceInstance"] = dummy
        self.monitor = DbusMonitor(tree, valueChangedCallback=self._value_changed,
                                   deviceAddedCallback=self._device_added,
                                   deviceRemovedCallback=self._device_removed)
        self._seed_inputs()

        # Safe start: command shore (flow: "Safe Start" inject at +3 s)
        GLib.timeout_add_seconds(3, self._safe_start)
        atexit.register(self._shutdown)
        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(_sig, self._on_signal)
            except (ValueError, OSError):
                pass
        GLib.timeout_add(cfg.tick_ms, self._tick)
        log.info("engine v%s, tick %d ms, shore on AC-in %s", ENGINE_VERSION,
                 cfg.tick_ms, cfg.ac_in)

    def _ms(self):
        return int(time.monotonic() * 1000)

    def _resolve_shore_input(self, status):
        """Which AC input is shore this tick, and the engine fields that
        depend on it. REC is the IgnoreAcIn writer, so its published
        resolution wins whenever it is there; otherwise the same rule REC
        applies (policy_contract.resolve_shore_input) runs on this side's
        own readings, so the two never disagree for long."""
        v = lambda f: f.v if f is not None else None
        i = self.inp
        new, reason = resolve_shore_input(
            self.cfg.ac_in, (v(i.ac1_type), v(i.ac2_type)), v(i.feed),
            (v(i.ac1_available), v(i.ac2_available)), self.shore_input)
        rec = status.get('shore_ac_input')
        if self.cfg.ac_in == "auto" and rec in (1, 2):
            new, reason = int(rec), "rec"
        # The fallback is provisional: used this tick, never settled, so the
        # first ticks before the sources arrive cannot pin input 1 for good.
        if reason != "default" and new != self.shore_input:
            log.info("shore AC input %s -> %d (%s)", self.shore_input, new, reason)
            self.shore_input = new
        self.shore_input_reason = reason
        i.ac_available = i.ac1_available if new == 1 else i.ac2_available

    def _engine_log(self, msg):
        if msg.startswith("ERROR "):
            log.error(msg[6:])
        else:
            log.info(msg)

    # ------------------------------------------------------------ settings
    def _init_settings(self):
        c = self.cfg
        supported = {
            "instance": ["/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                         "switch:%d" % c.instance, 0, 0],
            "enabled": ["/Settings/SolarPriority/Enabled", 0, 0, 1],
            "rated": ["/Settings/SolarPriority/RatedPower", int(c.rated_default),
                      int(c.rated_min), int(c.rated_max)],
        }
        self.settings = SettingsDevice(self.sbus, supported, self._setting_changed, timeout=120)
        granted = self._parse_instance(self.settings["instance"], c.instance)
        if granted != c.instance:
            # reconverge like dbus-recbms: only if nobody live holds it
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
                log.warning("instance %d held by a live service (the old Node-RED "
                            "flow?); using %d", c.instance, granted)
            else:
                try:
                    self.sbus.call_blocking(
                        "com.victronenergy.settings",
                        "/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                        BUSITEM, "SetValue", "v", ["switch:%d" % c.instance], timeout=5)
                    # localsettings silently keeps the old number when the
                    # wanted one is held by ANOTHER Devices/* entry (e.g. the
                    # retired flow's virtual_sp_* settings, until the registry
                    # orphan cleanup removes them) — re-read instead of trusting
                    # the write.
                    back = self._parse_instance(self.sbus.call_blocking(
                        "com.victronenergy.settings",
                        "/Settings/Devices/%s/ClassAndVrmInstance" % c.settings_id,
                        BUSITEM, "GetValue", "", [], timeout=5), granted)
                    if back == c.instance:
                        log.info("reconverged instance %d -> %d", granted, c.instance)
                        granted = c.instance
                    else:
                        log.warning("instance %d is reserved by another settings entry "
                                    "(a virtual_* orphan?); localsettings kept %d. "
                                    "Remove the orphan, then svc -t this service.",
                                    c.instance, back)
                        granted = back
                except Exception as e:
                    log.warning("could not pin instance %d (%s); using %d", c.instance, e, granted)
        self.instance = granted

    @staticmethod
    def _parse_instance(value, fallback):
        try:
            return int(str(value).split(":")[1])
        except (IndexError, ValueError):
            return fallback

    def _setting_changed(self, setting, old, new):
        # external write to localsettings: mirror to the switch + engine
        try:
            if setting == "enabled":
                self._set_enabled(bool(int(new)), persist=False)
                self.sw["/SwitchableOutput/output_1/State"] = 1 if self.inp.enabled else 0
            elif setting == "rated":
                self.inp.p_rated = float(new)
                self.sw["/SwitchableOutput/output_2/Dimming"] = float(new)
        except Exception:
            pass

    # ------------------------------------------------------ switch service
    def _init_switch_service(self):
        c = self.cfg
        svc = new_service("com.victronenergy.switch.%s" % c.suffix, _bus(private=True))
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", "%s on Python %s" % (VERSION, platform.python_version()))
        svc.add_path("/Mgmt/Connection", "dbus-solarpriority")
        svc.add_path("/DeviceInstance", self.instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", "Solar Priority")
        svc.add_path("/CustomName", "Solar Priority")
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/Serial", c.settings_id)
        svc.add_path("/Connected", 1)
        svc.add_path("/State", 0x100)

        o1 = "/SwitchableOutput/output_1"
        svc.add_path(o1 + "/State", 1 if self.inp.enabled else 0, writeable=True,
                     onchangecallback=self._toggle_changed)
        svc.add_path(o1 + "/Status", 0)
        svc.add_path(o1 + "/Name", "Enable")
        svc.add_path(o1 + "/Settings/Type", 1, writeable=True,
                     onchangecallback=lambda p, v: v in (1, 1.0))
        svc.add_path(o1 + "/Settings/ValidTypes", 1 << 1)
        svc.add_path(o1 + "/Settings/CustomName", c.toggle_name)
        svc.add_path(o1 + "/Settings/Group", c.group)
        svc.add_path(o1 + "/Settings/ShowUIControl", 1)
        svc.add_path(o1 + "/Settings/Adjustable", 0)

        o2 = "/SwitchableOutput/output_2"
        svc.add_path(o2 + "/State", 0, writeable=True, onchangecallback=lambda p, v: True)
        svc.add_path(o2 + "/Status", 0)
        svc.add_path(o2 + "/Name", "Rated PV (W)")
        svc.add_path(o2 + "/Dimming", self.inp.p_rated, writeable=True,
                     onchangecallback=self._rated_changed,
                     gettextcallback=lambda p, v: "---" if v is None else "%.0fW" % float(v))
        svc.add_path(o2 + "/Settings/Type", 7, writeable=True,
                     onchangecallback=lambda p, v: v in (7, 7.0))
        svc.add_path(o2 + "/Settings/ValidTypes", 1 << 7)
        svc.add_path(o2 + "/Settings/CustomName", c.slider_name)
        svc.add_path(o2 + "/Settings/Group", c.group)
        svc.add_path(o2 + "/Settings/ShowUIControl", 1)
        svc.add_path(o2 + "/Settings/Adjustable", 0)
        svc.add_path(o2 + "/Settings/DimmingMin", c.rated_min)
        svc.add_path(o2 + "/Settings/DimmingMax", c.rated_max)
        svc.add_path(o2 + "/Settings/StepSize", c.rated_step)
        svc.add_path(o2 + "/Settings/Unit", "W")

        # Diagnostics (read-only; what the flow showed as node status)
        svc.add_path("/SolarPriority/ProtocolVersion", PROTOCOL_VERSION)
        svc.add_path("/SolarPriority/Transport", "PREPARE_CONNECT")
        svc.add_path("/SolarPriority/LimitedBy", "")
        svc.add_path("/SolarPriority/Policy", "OFF")
        svc.add_path("/SolarPriority/Diagnostics", "{}")
        svc.add_path("/SolarPriority/State", "shore")
        svc.add_path("/SolarPriority/Status", "")
        svc.add_path("/SolarPriority/StatusFill", "grey")
        svc.add_path("/SolarPriority/LastTransition", "")
        svc.add_path("/SolarPriority/LastTransitionTime", 0)
        svc.add_path("/SolarPriority/EstimateW", 0.0)
        svc.add_path("/SolarPriority/NeedW", 0.0)
        svc.add_path("/SolarPriority/Desired", 0)
        svc.add_path("/SolarPriority/Missing", "")
        svc.add_path("/SolarPriority/EngineVersion", ENGINE_VERSION)
        # one-way charge / discharge (4.3): "", "charge" or "discharge", and
        # the Max Charge target it is judged against (None: dbus-recbms too old)
        svc.add_path("/SolarPriority/OneWay", "")
        svc.add_path("/SolarPriority/TargetSoc", None,
                     gettextcallback=lambda p, v: "---" if v is None else "%.0f%%" % float(v))
        svc.add_path("/SolarPriority/Sustain", 0)

        register_service(svc)
        self.sw = svc
        log.info("registered com.victronenergy.switch.%s instance %d (enabled=%d, rated %.0fW)",
                 c.suffix, self.instance, self.inp.enabled, self.inp.p_rated)

    def _toggle_changed(self, path, value):
        try:
            on = int(value) == 1
        except (TypeError, ValueError):
            return False
        self._set_enabled(on, persist=True)
        return True

    def _set_enabled(self, on, persist):
        was = self.inp.enabled
        self.inp.enabled = on
        if persist:
            try:
                self.settings["enabled"] = 1 if on else 0
            except Exception:
                log.warning("could not persist enable state")
        if on != was:
            if on:
                self.engine.reset_backoff()
            log.info("Solar Priority %s", "ENABLED" if on else "DISABLED")

    def _rated_changed(self, path, value):
        c = self.cfg
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(v) and c.rated_min <= v <= c.rated_max):
            return False
        self.inp.p_rated = v
        try:
            self.settings["rated"] = int(round(v))
        except Exception:
            log.warning("could not persist rated power")
        log.info("PV capacity cap -> %.0fW", v)
        return True

    # --------------------------------------------------------------- inputs
    def _svc(self, cls, instance):
        for name, inst in self.monitor.get_service_list("com.victronenergy." + cls).items():
            if inst == instance:
                return name
        return None

    def _field_for(self, service, path):
        cls = service.split(".")[2]
        spec = INPUT_MAP.get((cls, path))
        if spec is None:
            return None, None
        field, valid = spec
        inst = self.monitor.get_device_instance(service)
        c = self.cfg
        if cls == "solarcharger":
            if inst == c.mppt6_instance:
                field += "6"
            elif inst == c.mppt7_instance:
                field += "7"
            else:
                return None, None
        elif cls == "vebus" and inst != c.vebus_instance:
            return None, None
        elif cls == "battery" and inst != c.battery_instance:
            return None, None
        return field, valid

    def _store(self, service, path, value, now):
        field, validator = self._field_for(service, path)
        if field is not None:
            try:
                number = float(value)
                value = number if math.isfinite(number) and validator(number) else None
            except (TypeError, ValueError):
                value = None
            self.field_sources[field] = (service, path)
            setattr(self.inp, field, Val(value, now) if value is not None else None)
        self.sources.observe(service, path, value, now / 1000.0)

    def _store_fault(self, service, value):
        if self.monitor.get_device_instance(service) != self.cfg.battery_instance:
            return
        txt = "" if value is None else str(value).strip()
        if txt and txt != self.inp.lead_fault:
            log.error("SOLAR LEAD FAULT (dbus-recbms): %s -- boosts refused, harvest disabled", txt)
        elif not txt and self.inp.lead_fault:
            log.info("solar lead restored (dbus-recbms verified the offset again)")
        self.inp.lead_fault = txt

    def _value_changed(self, service, path, options, changes, instance):
        now = self._ms()
        if path == '/RecBms/LeadFault':
            self._store_fault(service, changes.get('Value'))
        self._store(service, path, changes.get('Value'), now)

    def _seed_inputs(self):
        now = self._ms()
        paths = list(INPUT_MAP)
        paths += [(cls, path) for cls, entries in WRITE_PATHS.items() for path in entries]
        for cls, path in paths:
            for name in self.monitor.get_service_list('com.victronenergy.' + cls):
                self._store(name, path, self.monitor.get_value(name, path), now)

    def _device_added(self, service, instance, *args):
        log.info('service appeared: %s (instance %s)', service, instance)
        self.last_verify = None
        if hasattr(self, 'monitor'):
            self._seed_inputs()

    def _device_removed(self, service, instance):
        log.warning('service vanished: %s (instance %s)', service, instance)
        self.sources.remove(service)
        for field, (name, _) in self.field_sources.items():
            if name == service:
                setattr(self.inp, field, None)
        self.engine.st["readySince"] = 0

    def _verify_sources(self, now):
        if self.last_verify is not None and now - self.last_verify < 5:
            return
        self.last_verify = now
        names = set()
        for cls in ('system', 'vebus', 'battery', 'solarcharger'):
            names.update(self.monitor.get_service_list('com.victronenergy.' + cls))
        for name in names:
            epoch = self.sources.generation(name)
            sequence = self.read_issued.get(name, 0) + 1
            self.read_issued[name] = sequence
            def received(values, source=name, expected_epoch=epoch, request_sequence=sequence, requested_at=now):
                if (expected_epoch != self.sources.generation(source) or source not in self.monitor.get_service_list() or
                        request_sequence <= self.read_applied.get(source, 0)):
                    return
                self.read_applied[source] = request_sequence
                observed = int(requested_at * 1000)
                for path, value in normalize_bus_snapshot(values).items():
                    self._store(source, path, value, observed)
            def failed(error, source=name, request_sequence=sequence, expected_epoch=epoch):
                if (expected_epoch == self.sources.generation(source) and
                        request_sequence > self.read_applied.get(source, 0)):
                    self.read_applied[source] = request_sequence
                    self.sources.remove(source)
            try:
                self.sbus.call_async(name, '/', BUSITEM, 'GetValue', '', [],
                                     reply_handler=received, error_handler=failed, timeout=3)
            except Exception as exc:
                failed(exc)

    def _write(self, cls, instance, path, value, what, on_error=None, on_ok=None):
        name = self._svc(cls, instance)
        if name is None:
            if on_error:
                on_error('service missing')
            return False
        def failed(error):
            log.warning('%s: %s', what, error)
            if on_error:
                on_error(error)
        def replied(code):
            if code != 0:
                failed('SetValue returned %s' % code)
            elif on_ok:
                on_ok()
        try:
            self.monitor.set_value_async(name, path, value, reply_handler=replied,
                                         error_handler=failed)
            return True
        except Exception as exc:
            failed(exc)
            return False

    def _safe_start(self):
        # New protocol: claim REC ownership with a connected request on the first tick.
        self.last_verify = None
        return False

    def _shutdown(self):
        if not self.last_request:
            return
        request = dict(self.last_request)
        request['request_id'] += 1
        request['transfer_intent'] = 'protect'
        request['requested_limits'] = {'boost_v': 0.0, 'purpose': '',
                                       'sustain': request['requested_limits'].get('sustain', 0)}
        name = self._svc('battery', self.cfg.battery_instance)
        if name:
            try:
                self.monitor.set_value(name, '/RecBms/Policy/Request', dumps(request))
            except Exception as exc:
                log.warning('shutdown policy request failed: %s', exc)

    def _on_signal(self, signum, frame):
        self._shutdown()
        raise SystemExit(0)

    def _request_failed(self, error):
        # 3.4.1: one refused request is not a reason to abandon the island.
        # A slider move refuses the next request as "target does not match
        # REC slider" for the tick or two before /RecBms/TargetSoc catches
        # up (boat, 2026-09-14 22:18 UTC: 60 -> 50 while islanded forced
        # the engine to shore and closed the relay on the old pair). REC
        # itself keeps a revoked lease's island through its grace and
        # returns prepared on its own if the lease really is gone, so the
        # engine stands down only after several refusals in a row.
        self.last_limited_by = str(error)
        self.failed_requests += 1
        if self.failed_requests >= self.REQUEST_FAILURES_TO_SHORE:
            self.engine.force_shore(self._ms())

    def _request_accepted(self):
        self.failed_requests = 0

    # ----------------------------------------------------------------- tick
    def _tick(self):
        now = time.monotonic()
        self._verify_sources(now)
        battery = self._svc('battery', self.cfg.battery_instance)
        system = ('com.victronenergy.system' if 'com.victronenergy.system' in
                  self.monitor.get_service_list('com.victronenergy.system') else None)
        vebus = self._svc('vebus', self.cfg.vebus_instance)
        def value(service, path):
            return self.sources.get(service, path, now) if service else None
        def document(path):
            try:
                return json.loads(value(battery, path) or '{}')
            except (TypeError, ValueError):
                return {}
        snapshot = document('/RecBms/Policy/Snapshot')
        status = document('/RecBms/Policy/Status')
        target = value(battery, '/RecBms/TargetSoc')
        selected = selected_battery_matches(
            battery, self.cfg.battery_instance,
            value(system, '/ActiveBatteryService'), value(system, '/ActiveBmsService'),
            value(system, '/Dc/Battery/BatteryService'), value(system, '/ActiveBmsInstance'))
        source_valid = (selected and snapshot.get('valid', False) and
                        value(vebus, '/Ac/Out/L1/P') is not None)
        # Refresh every engine field from observed values. A silent signal is
        # healthy after a successful poll; an old cached value cannot stay live.
        for field, (source, path) in self.field_sources.items():
            fresh = value(source, path)
            sample = self.sources.samples.get((source, path), {})
            setattr(self.inp, field, Val(fresh, sample.get('verified', now) * 1000)
                    if fresh is not None else None)
        source_valid = bool(source_valid and target is not None and self.inp.cvl is not None)
        if source_valid:
            # Native REC samples are authoritative, not systemcalc estimates.
            for field, key in (('soc', 'soc'), ('batt_v', 'voltage')):
                number = snapshot.get(key)
                setattr(self.inp, field, Val(number, now * 1000) if number is not None else None)
            voltage, current = snapshot.get('voltage'), snapshot.get('current')
            self.inp.batt = (Val(voltage * current, now * 1000)
                             if voltage is not None and current is not None else None)
        else:
            self.inp.batt = self.inp.soc = None
        qv, qi = value(vebus, '/Dc/0/Voltage'), value(vebus, '/Dc/0/Current')
        self.inp.quattro_w = Val(qv * qi, now * 1000) if qv is not None and qi is not None else None
        ac = value(system, '/Ac/Consumption/L1/Power')
        if ac is not None:
            self.inp.load_avg = Val(self.load_mean.update(now, ac), now * 1000)
            self.inp.load_slow = Val(self.load_slow_mean.update(now, ac), now * 1000)
        else:
            self.inp.load_avg = self.inp.load_slow = None
            self.load_mean.points.clear()
            self.load_slow_mean.points.clear()
        # REC's DemandModel result rides in its snapshot: AC through the
        # inverter (or the measured inverter DC while islanded), the DC
        # loads and its uncertainty allowance. Invalid or stale demand
        # authorizes no elective departure.
        demand = snapshot.get('demand', {}) if source_valid else {}
        island = demand.get('island_w') if demand.get('valid') else None
        if isinstance(island, (int, float)) and math.isfinite(island) and island >= 0:
            self.inp.demand_avg = Val(self.demand_mean.update(now, island), now * 1000)
            self.inp.demand_slow = Val(self.demand_slow_mean.update(now, island), now * 1000)
            self.inp.demand_margin = Val(max(0.0, float(demand.get('uncertainty_w') or 0.0)), now * 1000)
        else:
            self.inp.demand_avg = self.inp.demand_slow = self.inp.demand_margin = None
            self.demand_mean.points.clear()
            self.demand_slow_mean.points.clear()
        self.inp.departure_allowed = status.get('departure_allowed', False)
        self._resolve_shore_input(status if source_valid else {})
        protocol_ready = (status.get('version') == PROTOCOL_VERSION and
                          bool(status.get('generation')) and source_valid)
        if status.get('generation') != self.generation:
            self.generation = status.get('generation')
            self.engine = Engine(self.cfg.engine, now * 1000, self._engine_log)
            self.request_id = 0
            self.failed_probe, self.failed_probe_id = False, None
        try:
            out = self.engine.tick(now * 1000, self.inp)
            if out.probe_failed:
                self.failed_probe, self.failed_probe_id = True, None
            self.last_limited_by = '' if protocol_ready else 'REC protocol or fresh data unavailable'
            if protocol_ready:
                self.request_id = max(self.request_id, status.get('accepted_id', 0)) + 1
                # issue #8 / master D16: an actual failed probe must reach
                # REC's durable backoff, which the engine's own cooldown
                # cannot survive a restart. A single write can be refused, so
                # the marker rides on every request from the failure tick
                # until REC acknowledges one that carried it (REC counts one
                # durable failure per departure). A consumer restart drops
                # the pending marker; the engine-local backoff still stands.
                if (self.failed_probe and self.failed_probe_id is not None
                        and status.get('accepted_id', 0) >= self.failed_probe_id):
                    self.failed_probe, self.failed_probe_id = False, None
                if self.failed_probe and self.failed_probe_id is None:
                    self.failed_probe_id = self.request_id
                # 3.3.0 (master D12): a full target is one-way CHARGE until the
                # bank arrives; COMPLETE_FULL is the endgame at arrival only.
                mode = ('OFF' if not self.inp.enabled else
                        {'charge': 'CHARGE', 'discharge': 'DISCHARGE'}.get(out.oneway) or
                        ('COMPLETE_FULL' if target >= self.cfg.engine['ONEWAY_FULL_PCT'] else 'HOLD'))
                purpose = ('failed_probe' if self.failed_probe else
                           'probe' if out.state == 'probe' else
                           'descent' if out.oneway == 'discharge' else 'solar')
                request = dict(version=PROTOCOL_VERSION, generation=self.generation,
                    request_id=self.request_id, mode=mode, target_soc=float(target),
                    transfer_intent='island' if out.transfer_intent == 'island' else 'connected',
                    requested_limits={'sustain': SUSTAIN[out.charge_intent], 'purpose': purpose},
                    lease_s=15)
                if out.boost_v is not None:
                    request['requested_limits']['boost_v'] = out.boost_v
                self.last_request = request
                self._write('battery', self.cfg.battery_instance, '/RecBms/Policy/Request',
                            dumps(request), 'policy', self._request_failed, self._request_accepted)
            elif self.last_request:
                self._shutdown()
        except Exception as exc:
            log.exception('solar decision failed')
            self._request_failed(str(exc))
            self._shutdown()
            self.sw['/SolarPriority/Status'] = 'decision error; returning to shore'
            return True
        transfer = status.get('transfer', {})
        control = snapshot.get('control', {})
        if out.transition:
            log.info('%s', out.transition)
        with self.sw as service:
            if out.transition:
                service['/SolarPriority/LastTransition'] = out.transition
                service['/SolarPriority/LastTransitionTime'] = int(time.time())
            service['/SolarPriority/State'] = out.state
            service['/SolarPriority/Transport'] = transfer.get('state', 'PREPARE_CONNECT')
            service['/SolarPriority/Status'] = out.status_text
            service['/SolarPriority/StatusFill'] = out.status_fill if source_valid else 'grey'
            service['/SolarPriority/Policy'] = control.get('mode', 'OFF')
            service['/SolarPriority/LimitedBy'] = (self.last_limited_by or
                control.get('limited_by') or transfer.get('limited_by', ''))
            service['/SolarPriority/EstimateW'] = _q(out.est, self.cfg.power_step)
            service['/SolarPriority/NeedW'] = _q(out.need_w, self.cfg.power_step)
            service['/SolarPriority/Desired'] = int(out.transfer_intent == 'island')
            service['/SolarPriority/OneWay'] = out.oneway
            service['/SolarPriority/Sustain'] = SUSTAIN[out.charge_intent]
            service['/SolarPriority/TargetSoc'] = target
            service['/SolarPriority/Diagnostics'] = dumps({'sources_valid': source_valid,
                'protocol_ready': protocol_ready, 'status': status, 'engine_state': out.state})
        return True


# ----------------------------------------------------------------------------
def main():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_priority.ini")
    cfg = Config(cfg_path)
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("dbus-solarpriority v%s starting (velib: %s)", VERSION, _VELIB_DIR)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    SolarPriorityDriver(cfg)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
