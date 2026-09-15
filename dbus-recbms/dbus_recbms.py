#!/usr/bin/env python3
"""REC-BMS CAN publisher and Solar Priority safety/actuator owner.

YDNB-07 forwards 11-bit REC frames as 0x18FF0NNN on SocketCAN. This driver
publishes battery instance 200 and Max Charge slider instance 220, validates
CAN groups independently, and clamps both charger commands to fresh REC and
installation limits through verified base/offset transitions.

The v2 policy adapter owns measured energy references, durable wear/relay
budgets, battery-power regulation and the only new-protocol IgnoreAcIn writer.
Solar Priority sends acknowledged, expiring intents. Legacy sustain/boost
callbacks remain available only before the publisher-first ownership handover.
Maintenance requires calibrated evidence; there is no calendar-only charge.

Localsettings stores UI settings. The versioned atomic energy ledger stores
control history separately. The service wrapper supervises completed REC ticks
so a stalled executor can be restarted. Installation timing, physical voltage
margin, REC completion and balancing remain commissioning requirements; see
SOLAR_PRIORITY_FACTS.md and SOLAR_PRIORITY_ROLLOUT.md.
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
import atexit
import signal
import dbus.mainloop.glib
from gi.repository import GLib

VERSION = "3.5.0"
BUSITEM = "com.victronenergy.BusItem"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rec_policy_adapter import RecPolicyAdapter
from control_watchdog import Heartbeat

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
    @staticmethod
    def _number(value):
        """Reject nonfinite input before clamps can accidentally conceal it."""
        try:
            if isinstance(value, bool):
                raise ValueError()
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('configuration value must be numeric: %r' % (value,))
        if not math.isfinite(number):
            raise ValueError('configuration value must be finite: %r' % (value,))
        return number

    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        g = cp["general"] if cp.has_section("general") else {}
        self.log_level = str(g.get("log_level", "INFO")).upper()

        policy = cp['policy'] if cp.has_section('policy') else {}
        self.policy_state_path = os.path.join(os.path.dirname(os.path.abspath(path)),
                                             policy.get('state_file', 'solar-control-state.json'))
        self.capacity_version = policy.get('capacity_version', 'rated-1440')
        self.policy_calendar_timezone = policy.get('calendar_timezone', 'UTC')
        # 3.4.0: 'auto' (the default) resolves the shore input at runtime
        # from the GX's AC input types, then the Quattro's own facts
        # (policy_contract.resolve_shore_input); 1 or 2 pins it.
        raw = str(policy.get('shore_ac_input', 'auto')).strip().lower()
        self.policy_ac_input = 'auto' if raw in ('', 'auto') else int(raw)
        self.policy_consumer_suffix = policy.get('consumer_service_suffix', 'solarpriority')
        self.policy_consumer_instance = int(policy.get('consumer_instance', 221))
        if self.policy_ac_input not in ('auto', 1, 2):
            raise ValueError('policy shore_ac_input must be auto, 1 or 2')
        # 3.5.0: the Quattro's "Prefer renewable energy" toggle, managed
        # daily (policy_contract.prefer_renewable_wanted): 'auto' manages
        # it, 'off' never touches it.
        self.policy_prefer_renewable = str(policy.get('prefer_renewable', 'auto')).strip().lower()
        if self.policy_prefer_renewable not in ('auto', 'off'):
            raise ValueError('policy prefer_renewable must be auto or off')
        self.policy_dawn_voc_v = max(0.0, self._number(policy.get('dawn_voc_v', 60)))
        self.policy_dusk_voc_v = max(0.0, self._number(policy.get('dusk_voc_v', 50)))
        self.policy_dawn_s = max(0.0, self._number(policy.get('dawn_s', 600)))
        self.policy_day_deficit_pct = max(0.0, self._number(policy.get('day_deficit_pct', 1.0)))
        self.policy_mppt_instances = tuple(int(v.strip()) for v in policy.get('mppt_instances', '278,279').split(','))
        self.policy_parameters = dict(cp['control']) if cp.has_section('control') else {}
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
        self.installed_ah = self._number(b.get("installed_capacity_ah", 1440))
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
        self.slider_min = self._number(s.get("min", 40))
        self.slider_max = self._number(s.get("max", 100))
        self.slider_step = self._number(s.get("step", 5))
        self.slider_default = self._number(s.get("default", 100))

        v = cp["cvl"] if cp.has_section("cvl") else {}
        # piecewise-linear slider% -> CVL breakpoints "pct:volts, pct:volts, ..."
        curve = v.get("curve", "40:54.42, 62.3:56.65, 100:62.70")
        points = [point.split(':') for point in curve.split(',') if point.strip()]
        if any(len(point) != 2 for point in points):
            raise ValueError('[cvl] curve points must be pct:volts pairs')
        self.cvl_curve = sorted((self._number(pct), self._number(volts)) for pct, volts in points)
        if len(self.cvl_curve) < 2:
            raise ValueError("[cvl] curve needs at least two pct:volts points")
        self.cvl_max = self._number(v.get("max_v", 61.96))
        self.installation_max_v = min(62.40, self._number(v.get("installation_max_v", 62.40)))
        self.voltage_guard_v = max(0.0, self._number(v.get("voltage_guard_v", 0.10)))
        # standing Quattro/solar split: command the vebus this far below the
        # target and raise only the MPPTs back to it (0 disables)
        self.solar_lead = max(0.0, min(0.30, self._number(v.get("solar_lead_v", 0.0))))
        # v1.7.0: the lead only while Solar Priority is enabled (its
        # /Settings/SolarPriority/Enabled), and never at/above this slider
        # position -- a full charge is every charger commanded the target
        self.lead_needs_sp = \
            str(v.get("lead_needs_solar_priority", "true")).lower() != "false"
        self.lead_full_pct = self._number(v.get("lead_full_pct", 100))
        # verify the offset against systemcalc /Control/EffectiveChargeVoltage:
        # a mismatch must persist this long before it counts (DVCC only
        # adjusts every 3 s, so a slider move is briefly inconsistent)
        self.lead_verify_s = self._number(v.get("lead_verify_s", 30))
        self.lead_fault_alarm = \
            str(v.get("lead_fault_alarm", "true")).lower() != "false"

        # [publish] — telemetry quantisation. A value only goes on D-Bus when
        # it moves to a different step, and every tick's changes go out as ONE
        # ItemsChanged. Control paths (CVL/CCL/DCL, targets, lead, alarms,
        # sustain/boost) are published at full resolution regardless.
        pb = cp["publish"] if cp.has_section("publish") else {}
        self.voltage_step = self._number(pb.get("voltage_step", 0.05))
        self.current_step = self._number(pb.get("current_step", 0.5))
        self.power_step = self._number(pb.get("power_step", 10))
        self.temperature_step = self._number(pb.get("temperature_step", 0.5))
        self.soc_step = self._number(pb.get("soc_step", 0.1))
        self.ah_step = self._number(pb.get("ah_step", 1))
        self.time_step = self._number(pb.get("time_step", 60))

        f = cp["fallback"] if cp.has_section("fallback") else {}
        self.live_timeout = self._number(f.get("live_timeout_s", 60))
        self.alert_timeout = self._number(f.get("alert_timeout_s", 120))
        self.restrict_timeout = self._number(f.get("restrict_timeout_s", 300))
        self.startup_grace = self._number(f.get("startup_grace_s", 180))
        self.alert_dcl = self._number(f.get("alert_dcl_a", 100))
        self.alert_dvl = self._number(f.get("alert_dvl_v", 52.0))
        self.restrict_dcl = self._number(f.get("restrict_dcl_a", 30))
        self.restrict_dvl = self._number(f.get("restrict_dvl_v", 53.0))
        self.survival_dcl = self._number(f.get("survival_dcl_a", 15))
        self.survival_dvl = self._number(f.get("survival_dvl_v", 54.0))
        # Unknown limits never authorize charging, including old 62.7 V configs.
        self.safe_cvl = min(54.0, self._number(f.get("safe_cvl_v", 54.0)))
        self.safe_voltage = self._number(f.get("safe_voltage_v", 54.0))
        self.safe_soc = self._number(f.get("safe_soc", 50))

        q = cp["quattro"] if cp.has_section("quattro") else {}
        self.vebus_instance = int(q.get("vebus_instance", 276))
        self.extv_max_age = self._number(q.get("max_age_s", 30))
        self.extv_poll_s = int(q.get("poll_s", 5))

        sb = cp["solarboost"] if cp.has_section("solarboost") else {}
        self.boost_enabled = str(sb.get("enabled", "true")).lower() != "false"
        self.boost_max_v = self._number(sb.get("max_boost_v", 0.30))
        self.boost_hold_s = self._number(sb.get("hold_s", 120))
        self.boost_measure_start_s = self._number(sb.get("measure_start_s", 75))
        self.boost_measure_len_s = self._number(sb.get("measure_len_s", 30))
        self.boost_cell_max_v = self._number(sb.get("cell_max_v", 4.05))
        self.boost_cell_min_t = self._number(sb.get("cell_min_t", 5))
        self.boost_cell_max_t = self._number(sb.get("cell_max_t", 45))
        self.boost_ceiling_v = self._number(sb.get("ceiling_v", 62.70))
        self.boost_min_margin_v = self._number(sb.get("min_margin_v", 0.10))
        self.boost_service = sb.get("target_service", "com.victronenergy.system")
        self.boost_path = sb.get(
            "target_path", "/Debug/BatteryOperationalLimits/SolarVoltageOffset")

        su = cp["sustain"] if cp.has_section("sustain") else {}
        self.sustain_enabled = str(su.get("enabled", "true")).lower() != "false"
        self.sustain_hold_s = self._number(su.get("hold_s", 120))
        self.sustain_step = max(0.0, self._number(su.get("step_pct", 1)))
        self.sustain_ccl_a = max(0.0, self._number(su.get("charge_limit_a", 5)))
        # v1.8.0: the voltage anchor's SOC servo and the band-absorbed step
        self.sustain_servo_v = max(0.0, self._number(su.get("servo_step_v", 0.02)))
        self.sustain_servo_s = self._number(su.get("servo_period_s", 30))
        self.sustain_servo_db = max(0.0, self._number(su.get("servo_deadband_pct", 0.1)))
        self.sustain_servo_up = max(0.0, self._number(su.get("servo_max_up_v", 0.5)))
        self.sustain_servo_down = max(0.0, self._number(su.get("servo_max_down_v", 2.0)))
        # 3.3.2: a two-sided hold folds its servo on arrival only when it
        # carries at least this much -- a real lift or descent, not the
        # few hundredths that cover the loads at the band's edge.
        self.sustain_arrival_fold_v = max(0.0, self._number(su.get("arrival_fold_v", 0.25)))
        self.sustain_band_v = max(0.0, min(1.0, self._number(su.get("band_v", 0.30))))
        self.sustain_anchor_r = max(0.0, self._number(su.get("anchor_ir_mohm", 3))) / 1000.0
        self.sustain_taper_a = max(0.0, self._number(su.get("taper_a", 3)))
        self.sustain_taper_s = max(0.0, self._number(su.get("taper_s", 60)))
        self.sustain_taper_margin = max(0.0, self._number(su.get("taper_margin_v", 0.03)))
        self.sustain_q_idle_a = max(0.0, self._number(su.get("quattro_idle_a", 1)))
        self.sustain_pv_min_a = max(0.0, self._number(su.get("pv_min_a", 0.5)))
        # v1.8.3: what counts as the bank draining (the DC loads alone are
        # ~0.9 A on this boat), and the hold voltage's feed-forward per
        # percent of held SOC between re-anchors
        self.sustain_drain_a = max(0.0, self._number(su.get("servo_drain_a", 0.3)))
        # v3.2.0: the hold's current trim (see _service_sustain), a step per
        # servo period on the destination's charge limit, bounded either way
        self.sustain_trim_a = max(0.0, self._number(su.get("trim_step_a", 0.5)))
        self.sustain_trim_max_a = max(0.0, self._number(su.get("trim_max_a", 3.0)))
        # v1.8.4: the dusk snap -- PV current under pv_min_a for this long,
        # after a day of sun, re-anchors the hold to the bank's own voltage
        self.sustain_dusk_s = max(0.0, self._number(su.get("dusk_s", 300)))

        # Unit conversions (minutes/days) must remain finite as well.
        for key, value in vars(self).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('configuration parameter overflow: ' + key)
        positive = (
            'can_reconnect_s', 'installed_ah', 'nr_of_cells', 'slider_step',
            'cvl_max', 'installation_max_v',
            'lead_verify_s', 'live_timeout', 'alert_timeout', 'restrict_timeout',
            'startup_grace', 'safe_cvl', 'safe_voltage', 'extv_max_age', 'extv_poll_s',
            'boost_hold_s', 'boost_measure_start_s', 'boost_measure_len_s',
            'sustain_hold_s', 'sustain_servo_s', 'sustain_taper_s', 'sustain_dusk_s')
        for key in positive:
            if getattr(self, key) <= 0:
                raise ValueError('configuration parameter must be positive: ' + key)
        if self.cvl_max > 61.96:
            raise ValueError('normal CVL may not exceed 61.96 V')
        if not 0 <= self.slider_min < self.slider_max <= 100 or not self.slider_min <= self.slider_default <= self.slider_max:
            raise ValueError('invalid slider bounds or default')
        if not 0 <= self.safe_soc <= 100 or not 0 <= self.lead_full_pct <= 100:
            raise ValueError('configured SOC must be within 0..100')
        if not 0 < self.live_timeout <= self.alert_timeout <= self.restrict_timeout:
            raise ValueError('fallback timeouts must be ordered')
        if any(not 0 <= pct <= 100 or volts <= 0 for pct, volts in self.cvl_curve):
            raise ValueError('invalid CVL curve point')
        if any(right[0] <= left[0] or right[1] < left[1]
               for left, right in zip(self.cvl_curve, self.cvl_curve[1:])):
            raise ValueError('CVL curve requires unique SOC points and nondecreasing voltage')
        if (not self.policy_mppt_instances or len(set(self.policy_mppt_instances)) != len(self.policy_mppt_instances)
                or any(instance < 0 for instance in self.policy_mppt_instances)):
            raise ValueError('policy MPPT instances must be distinct nonnegative numbers')



# ----------------------------------------------------------------------------
# CAN frame decoding (1:1 port of the Node-RED "CAN Frame Decoder")
# ----------------------------------------------------------------------------
MIN_LEN = {
    0x351: 8, 0x355: 4, 0x356: 8, 0x35E: 1, 0x35F: 6, 0x360: 1,
    0x370: 1, 0x371: 1, 0x372: 8, 0x373: 8, 0x374: 1, 0x375: 1,
    0x376: 1, 0x377: 1, 0x379: 2, 0x380: 1, 0x381: 1, 0x404: 1,
}



# Cell and temperature extrema share a frame, but have distinct consumers.
CAN_GROUPS = {
    0x351: ("Limits",), 0x355: ("Soc",), 0x356: ("Measurements",),
    0x372: ("Modules",), 0x373: ("Cells", "Temperature"),
}
CRITICAL_GROUPS = ("Limits", "Measurements", "Soc", "Cells", "Temperature", "Modules")
GROUP_FIELDS = {
    "Limits": ("cvl", "ccl", "dcl", "dvl"),
    "Measurements": ("voltage", "current", "temperature"),
    "Soc": ("soc",), "Cells": ("minCellV", "maxCellV"),
    "Temperature": ("minCellT", "maxCellT"),
    "Modules": ("modulesOnline", "modulesBlockingCharge", "modulesBlockingDischarge", "modulesOffline"),
}


def rec_health(bms, now, timeout):
    """Return independent receipt ages/validity, never numeric-change ages."""
    received = bms.get("_received", {})
    health = {}
    for group in CRITICAL_GROUPS:
        stamp = received.get(group)
        age = None if stamp is None else max(0.0, now - stamp)
        values = [bms.get(key) for key in GROUP_FIELDS[group]]
        valid = (age is not None and stamp <= now and age <= timeout and
                 all(isinstance(v, (int, float)) and math.isfinite(v) for v in values))
        if valid:
            if group == "Limits":
                valid = (0 < bms["cvl"] <= 80 and 0 <= bms["ccl"] <= 5000 and
                         0 <= bms["dcl"] <= 5000 and 0 < bms["dvl"] <= bms["cvl"])
            elif group == "Measurements":
                valid = 20 <= bms["voltage"] <= 80 and abs(bms["current"]) <= 5000
            elif group == "Soc":
                soc = bms.get("socHiRes") if bms.get("socHiRes") is not None else bms["soc"]
                valid = math.isfinite(soc) and 0 <= soc <= 100
            elif group == "Cells":
                valid = 0 < bms["minCellV"] <= bms["maxCellV"] <= 6
            elif group == "Temperature":
                valid = -60 <= bms["minCellT"] <= bms["maxCellT"] <= 120
            elif group == "Modules":
                valid = all(0 <= v <= 65535 for v in values)
        health[group] = {"age": age, "valid": bool(valid)}
    return health


def voltage_floor(value):
    """Round down so decimal publication cannot cross a hard cap."""
    return math.floor(max(0.0, value) * 100 + 1e-8) / 100.0


class VoltageEnvelope:
    """Sequence DVCC base/offset updates through a verified safe base.

    Callbacks publish the base immediately, write/read the offset, and verify
    downstream application. A failed or missing readback inhibits charging.
    """
    def __init__(self, publish_base, write_offset, read_offset, applied):
        self.publish_base, self.write_offset = publish_base, write_offset
        self.read_offset, self.applied = read_offset, applied
        self.base, self.offset = None, None
        self.ready, self.status = False, "unverified startup"
        self.requested_quattro, self.requested_solar = None, None
        self.pending_offset = None
        self.pending_offset_max = 0.0

    def _publish(self, base):
        base = voltage_floor(base)
        if self.base != base:
            self.publish_base(base)
            self.base = base

    def step(self, quattro, solar, safe):
        self.ready = False
        self.requested_quattro = voltage_floor(min(quattro, safe))
        self.requested_solar = voltage_floor(min(solar, safe))
        new_offset = round(self.requested_solar - self.requested_quattro, 2)
        observed = self.read_offset()
        if observed is None or not math.isfinite(observed):
            self.offset = None
            self._publish(0.0)
            self.status = "offset unknown; charging inhibited"
            return False
        self.offset = observed
        # A confirmed Debug-property write can precede downstream application.
        # Retain the safe base until the changed offset is effective as well.
        if self.pending_offset is not None:
            if abs(observed - self.pending_offset) <= 0.001:
                if not self.applied(self.base, observed):
                    self._publish(min(self.base, safe - max(0.0, observed, self.pending_offset_max)))
                    self.status = "waiting for effective offset readback"
                    return False
            self.pending_offset = None
        intermediate = voltage_floor(min(self.requested_quattro,
                                          safe - max(0.0, observed, new_offset)))
        if self.base is None or self.base > intermediate + 0.001:
            self._publish(intermediate)
        if abs(observed - new_offset) > 0.001:
            if not self.applied(self.base, observed):
                self.status = "waiting for safe intermediate voltage"
                return False
            old_offset = observed
            if not self.write_offset(new_offset):
                self.status = "offset write refused; retaining safe base"
                return False
            observed = self.read_offset()
            if observed is None or abs(observed - new_offset) > 0.001:
                self.status = "offset readback pending; retaining safe base"
                return False
            self.offset = observed
            if not self.applied(self.base, observed):
                self.pending_offset = observed
                self.pending_offset_max = max(0.0, old_offset, observed)
                self.status = "waiting for effective offset readback"
                return False
        self._publish(self.requested_quattro)
        self.ready = self.applied(self.base, self.offset)
        self.status = "verified" if self.ready else "waiting for charger voltage readback"
        return self.ready


def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _s16(d, o):
    return struct.unpack_from("<h", d, o)[0]


def _ascii(d):
    return d.split(b"\0")[0].decode("ascii", errors="replace").strip("�")


def decode_frame(bms, canid, data, received_at=None):
    """Decode a frame, refreshing only groups it carries (monotonic time)."""
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
        bms["socHiRes"] = _u16(data, 4) / 100.0 if len(data) >= 6 else None
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

    received_at = time.monotonic() if received_at is None else received_at
    received = bms.setdefault("_received", {})
    for group in CAN_GROUPS.get(canid, ()):
        received[group] = received_at
    bms["_lastFrame"] = received_at
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


SUSTAIN_OFF = 0        # released: the slider's own curve applies
SUSTAIN_FLOOR = 1      # held SOC may only rise (solar charges, shore holds)
SUSTAIN_CEILING = 2    # held SOC may only fall (loads drain, nothing charges)
SUSTAIN_HOLD = 3       # two-sided: the bank is kept AT the slider's target
SUSTAIN_HOLD_MIN_A = 0.1  # the least a hold publishes at its destination
SUSTAIN_NAMES = {SUSTAIN_FLOOR: "floor", SUSTAIN_CEILING: "ceiling",
                 SUSTAIN_HOLD: "hold"}


def sustain_name(mode):
    return SUSTAIN_NAMES.get(mode, "off")


def standing_lead(lead_v, slider, full_pct, sp_enabled, needs_sp=True):
    """The solar lead in force this tick (v1.7.0). The lead is a Solar
    Priority tool: it keeps the Quattro under the target so the MPPTs have
    headroom on shore. Without Solar Priority, or with the Max Charge slider
    at full_pct, the Quattro gets the true target and the offset is dropped:
    the owner's endgame is 61.96 V for BOTH chargers, not 61.96 less a lead
    (2026-09-14). That is the standing lead only; a floor's or hold's band
    is decided from the mechanism's availability (_sustain_band), so a
    one-way charge toward a full target still gets its solar band."""
    if lead_v <= 0:
        return 0.0
    if slider >= full_pct:
        return 0.0
    if needs_sp and not sp_enabled:
        return 0.0
    return float(lead_v)


def sustain_hold(mode, held, soc, charging, sun):
    """The held SOC after this tick: a one-way ratchet with no hysteresis.

    A floor follows the bank upward -- every hundredth of a percent the
    sun adds is kept -- but only on sun (PV current flowing) and not while
    the shore charger is the one adding. Without the sun condition the
    Quattro's trickle under the idle threshold ratcheted the floor up 0.2 %
    over the night of 2026-09-09; with it, a rise at night is the Quattro's
    and the servo answers it instead. A ceiling follows the bank downward,
    always: a drain is the plan. No SOC (BMS not live) keeps the hold.

    A two-sided hold (mode 3, v3.2.0) ratchets nowhere: it holds the
    DESTINATION, not what the bank happens to reach, so the held value
    comes back unchanged and _service_sustain judges the bank against the
    slider itself. Moving the slider inside a HOLD simply moves the
    reference; the engine changes mode when the change is a large one.
    Pure, so it can be tested off the boat.
    """
    if soc is None:
        return held
    if mode == SUSTAIN_FLOOR and soc > held and sun and not charging:
        return soc
    if mode == SUSTAIN_CEILING and soc < held:
        return soc
    return held


def sustain_servo(mode, err, charging, deadband, draining=True, filling=False):
    """Which way to move the hold voltage this servo period: +1 up, -1
    down, 0 leave it. err is SOC minus the (slider-bounded) held SOC in
    percent; charging says whether the shore charger is pushing current
    into the bank (see shore_charging); draining says whether current is
    still leaving the bank; filling says whether ANY source is putting
    current into it.

    Floor: the bank more than a deadband UNDER the held SOC AND still
    draining is a sag the Quattro is not covering -> up. Once the bank
    sits still, the Quattro is covering the loads and the command is
    where it needs to be: hold, even a hair under the held SOC. (Pushing
    on until the bank was back above the line wound the command up 0.5 V
    ahead of the Quattro's slow response on 2026-09-09 and overshot by
    0.25 %.) The Quattro charging it more than a deadband ABOVE -> down.
    Solar raising it, or a wobble inside the band, is left alone.

    Ceiling: nothing may charge, so a bank being FILLED is always -> down,
    and a drain is the plan. Answering only the inferred Quattro (v1.8.x)
    let the sun put 17.684 Ah back into a descending bank over eight
    alternating hours, 1.228 % reverse (E08, master D07/SP40): a positive
    battery current from any source is reverse movement. Zero current --
    a sunny plateau with PV carrying the loads at the ceiling -- is not
    filling and is left alone (SP38).

    Hold (mode 3, v3.2.0): two-sided. Under its destination and still
    draining -> up, exactly as the floor. Above it -> down whatever is
    responsible: the sun, the Quattro, or simply a bank the loads should
    be allowed to bring down at night. (The old release left the slider
    curve with the 0.15 V standing lead under it, the Quattro sat below
    the bank and a steady -49 W drained 1.448 points in 24 h: E04/D03.)
    A bank still under its destination that solar is raising is left
    alone -- finishing the last bit is the band's job.

    3.3.2: the unconditional lift ("up whenever under, draining or not")
    applies only while the bank is clearly under -- more than two
    deadbands. Within two deadbands of the destination the hold servos
    like the floor: up while draining, still once the Quattro covers the
    loads. Winding on regardless there put +0.48 V on a bank 0.02 %
    under its band (boat, 2026-09-15 dawn), and the arrival fold then
    took the whole offset away, loads included, so the bank drained
    again: a 30-minute limit cycle at the band's edge.
    """
    if mode == SUSTAIN_FLOOR:
        if err < -deadband and draining:
            return 1
        if err > deadband and charging:
            return -1
        return 0
    if mode == SUSTAIN_HOLD:
        # Up whenever the bank sits under its destination, draining or not:
        # under the hold the Quattro is capped at charge_limit_a below the
        # target (the floor's brake), so a step up fills gently instead of
        # winding the command ahead of a 200 A charger (the v1.8.1 case).
        # Down only when the Quattro is the one filling it above -- the sun
        # cannot, the current limit sees to that, and a bank the loads
        # should bring down is not answered by starving every charger of
        # voltage (a hold servoed under the bank left the MPPTs at 0 W for
        # four sunny hours in the fixture, 2026-09-14).
        if err < -deadband:
            return 1 if (draining or err < -2 * deadband) else 0
        if err > deadband and charging:
            return -1
        return 0
    if mode == SUSTAIN_CEILING and (charging or filling):
        return -1
    return 0


def shore_charging(batt_a, pv_a, idle_a):
    """Is the shore charger pushing current into the bank? With no DC
    current meter, systemcalc's DC-system estimate makes the Quattro's net
    contribution to the bank exactly battery current minus PV current, so
    the question needs neither the vebus service nor the load figure. An
    unknown PV current answers False (no servo-down; the sustain charge
    current limit bounds what the Quattro can do meanwhile)."""
    if batt_a is None or pv_a is None:
        return False
    return (batt_a - pv_a) > idle_a


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
        self.heartbeat = Heartbeat.from_environment()
        self.cfg = cfg
        self.bms = {}
        # Every duration in this driver -- boost and sustain expiry, servo
        # spacing, taper and dusk intervals, sample freshness, the
        # regulation fault -- is measured on the monotonic clock (3.0.2,
        # issue #6): a -1 h wall-clock step once kept an accepted boost
        # alive with 3588 s to go (E10). Wall time is for calendar records
        # (eqlast) and telemetry only.
        self.start_mono = time.monotonic()
        self.phase_name = None          # for change-only logging
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
        self.sustain = self._sustain_idle()
        self.last_target = None
        self._last_offset_warn = 0.0
        self.eff_cv = None                  # (volts or None, ts) from systemcalc
        self.pv_current = None              # (amps or None, ts) from systemcalc
        self.charge_guard_reason = ""
        self.applied_reason = ""            # why _voltage_applied last said no
        self.voltage_control = VoltageEnvelope(
            self._publish_voltage_base, self._boost_write,
            self._read_solar_offset, self._voltage_applied)
        self.sp_enabled = None              # /Settings/SolarPriority/Enabled, polled
        self.lead_v = 0.0                   # standing lead in force this tick
        self._lead_logged = None
        # Regulation fault (issue #3): the requested voltage pair has gone
        # unverified for lead_verify_s. Boosts are refused, the lead reads
        # 0, /RecBms/LeadFault says why and InternalFailure warns.
        self.lead_fault = {"active": False, "since": 0.0, "msg": "",
                           "mismatch_since": 0.0}
        self._check_access_level()
        if cfg.pin_bms_instance:
            self._pin_bms_instance()
        self.batt["/Info/MaxChargeCurrent"] = 0.0
        self.policy_adapter = RecPolicyAdapter(self, clock=time)
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
        svc.add_path("/RecBms/Health/Source", "can:%s:%08x/%08x" % (
            c.can_iface, c.can_filter_id, c.can_filter_mask))
        svc.add_path("/RecBms/Health/CriticalValid", 0)
        for group in CRITICAL_GROUPS:
            svc.add_path("/RecBms/Health/%s/Age" % group, None)
            svc.add_path("/RecBms/Health/%s/Valid" % group, 0)
        for name in ("Voltage", "Current", "Soc", "ChargeVoltageLimit",
                     "ChargeCurrentLimit", "DischargeCurrentLimit"):
            svc.add_path("/RecBms/Raw/" + name, None)
        svc.add_path("/RecBms/SafeChargeVoltage", c.safe_cvl)
        for name in ("RequestedQuattro", "RequestedSolar", "AcceptedQuattro", "AcceptedSolar"):
            svc.add_path("/RecBms/Voltage/" + name, None)
        svc.add_path("/RecBms/Voltage/Ready", 0)
        svc.add_path("/RecBms/Voltage/Status", "unverified startup")


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
        # downward only -- loads may lower it, nothing raises it), 3 to hold
        # the slider's own target from both sides (v3.2.0: the held SOC is
        # the destination, the servo answers a drain and a fill alike and
        # the MPPT band closes at the target), 0 to release. All stay inside
        # the real slider target. It ALWAYS expires after [sustain] hold_s
        # -- see _service_sustain() -- so a requester that dies cannot leave
        # the bank pinned. Reads back -1 while a hold is active.
        svc.add_path("/RecBms/Sustain/Request", 0, writeable=True,
                     onchangecallback=self._sustain_requested)
        svc.add_path("/RecBms/Sustain/Active", 0)
        svc.add_path("/RecBms/Sustain/Mode", 0)
        svc.add_path("/RecBms/Sustain/Soc", None, gettextcallback=fmt("%", 1))
        svc.add_path("/RecBms/Sustain/SecondsLeft", 0,
                     gettextcallback=fmt_int("s"))
        svc.add_path("/RecBms/Sustain/Status", "idle")
        # the charge current limit in force for the hold (None: not capped)
        svc.add_path("/RecBms/Sustain/ChargeLimit", None, gettextcallback=a1)
        # v1.8.0: the voltage the hold sits at (the Quattro's command under
        # a floor or a two-sided hold, the MPPT ceiling under a ceiling) and
        # the SOC servo's correction included in it
        svc.add_path("/RecBms/Sustain/HoldVoltage", None, gettextcallback=v2)
        svc.add_path("/RecBms/Sustain/Servo", 0.0, gettextcallback=v2)
        svc.add_path("/RecBms/Sustain/TrimA", 0.0, gettextcallback=fmt("A", 1))

        register_service(svc)
        self.batt = svc
        # Everything publishes through _pub. Outside a tick it IS the
        # service (immediate PropertiesChanged, for the rare event writes);
        # inside a tick it is velib's batching context, so the ~40 paths a
        # tick touches go out as one ItemsChanged instead of one signal each.
        self._pub = svc
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

    def _read_number(self, service, path):
        # Voltage guards consume the same bounded, timestamped root samples as
        # policy. Re-reading every charger synchronously in every guard blocks
        # the event loop which must deliver those samples. Missing/stale cache
        # entries remain unavailable; writes and offset acknowledgement stay
        # synchronous and are never satisfied by a cached readback.
        adapter = getattr(self, 'policy_adapter', None)
        if (adapter is not None and adapter.contract.owned and path in (
                '/Connected', '/Link/ChargeVoltage',
                '/BatteryOperationalLimits/MaxChargeVoltage', '/Control/EffectiveChargeVoltage')
                and service in adapter.sources_names.values()):
            value = adapter.sources.get(service, path, time.monotonic())
            try:
                value = float(value)
                return value if math.isfinite(value) else None
            except (TypeError, ValueError):
                return None
        try:
            value = self.sbus.call_blocking(service, path, BUSITEM,
                                           "GetValue", "", [], timeout=2)
            value = float(value)
            return value if math.isfinite(value) else None
        except Exception:
            return None

    def _read_solar_offset(self):
        return self._read_number(self.cfg.boost_service, self.cfg.boost_path)

    def _boost_write(self, volts, quiet=False):
        """Accept only SetValue success AND actual offset readback."""
        try:
            result = self.sbus.call_blocking(
                self.cfg.boost_service, self.cfg.boost_path, BUSITEM,
                "SetValue", "v", [float(volts)], timeout=2)
            actual = self._read_solar_offset()
            return (result == 0 and actual is not None and
                    abs(actual - volts) <= 0.001)
        except Exception as exc:
            if not quiet:
                log.warning("solar offset write failed: %s", exc)
            return False

    def _publish_voltage_base(self, volts):
        # Deliberately outside the telemetry batch: offset writes must never
        # overtake a buffered reduction of the battery's base CVL.
        self.batt["/Info/MaxChargeVoltage"] = volts

    def _voltage_applied(self, base, offset):
        """Fresh DVCC and charger setpoints must confirm the safe pair.

        Lower charger limits are safe (a device can impose its own cap); the
        systemcalc effective limit must acknowledge the requested base/offset.
        Missing control paths are a commissioning/availability fault.
        """
        effective = self._read_number(self.cfg.boost_service,
                                      "/Control/EffectiveChargeVoltage")
        safe = self._safe_voltage()
        self.applied_reason = ""
        if effective is None:
            self.applied_reason = "systemcalc /Control/EffectiveChargeVoltage unavailable (DVCC off or systemcalc down?)"
            return False
        if effective > safe + 1e-8 or abs(effective - (base + offset)) > 0.015:
            if offset > 0.005 and abs(effective - base) <= 0.015:
                # the base went through and the offset did not: the access
                # level gate (see _check_access_level)
                self.applied_reason = ("systemcalc ignores the solar offset: MPPTs get %.2fV, "
                                       "expected %.2fV" % (effective, base + offset))
            else:
                self.applied_reason = "DVCC effective %.2fV, expected %.2fV" % (effective, base + offset)
            return False
        seen = False
        for name in self.sbus.list_names():
            name = str(name)
            if name.startswith("com.victronenergy.solarcharger."):
                path, ceiling = "/Link/ChargeVoltage", base + offset
            elif name.startswith("com.victronenergy.vebus."):
                path, ceiling = "/BatteryOperationalLimits/MaxChargeVoltage", base
            else:
                continue
            connected = self._read_number(name, "/Connected")
            if connected == 0:
                continue
            if connected != 1:
                self.applied_reason = "%s: /Connected unknown" % name
                return False
            actual = self._read_number(name, path)
            if actual is None:
                self.applied_reason = "%s: %s readback missing" % (name, path)
                return False
            if actual < 0 or actual > min(ceiling + 0.015, safe + 1e-8):
                self.applied_reason = "%s: %s at %.2fV, above %.2fV" % (name, path, actual, ceiling)
                return False
            seen = True
        if not seen:
            self.applied_reason = "no charger on the bus"
        return seen

    def _voltage_within_envelope(self, safe):
        """Charging can continue during a safe servo update; transfer readiness
        still requires the precise requested protection to be acknowledged."""
        if self.voltage_control.offset is None:
            return False
        effective = self._read_number(self.cfg.boost_service, '/Control/EffectiveChargeVoltage')
        if effective is None or not 0 <= effective <= safe + 0.001:
            return False
        adapter = getattr(self, 'policy_adapter', None)
        islanded = bool(adapter is not None and adapter.transfer.feedback is False)
        # An unchanged, previously accepted envelope survives loss of an MPPT
        # report while islanded. New departures still require fresh exact
        # readbacks. Known unsafe observations and requested increases fail. Reductions
        # may wait for readback while the previous safe pair remains in force.
        controller = self.voltage_control
        verified = getattr(self, '_last_verified_voltage', None)
        cached_solar_safe = bool(islanded and verified is not None and controller.base is not None and
            controller.offset is not None and controller.requested_solar is not None and
            controller.requested_solar <= controller.base + controller.offset + .015 and
            0 <= controller.base + controller.offset <= min(safe, verified[1]) + .001 and
            self._health()['Measurements']['valid'] and
            0 < self.bms.get('voltage', 0) <= safe + .001)
        seen = False
        for name in self.sbus.list_names():
            name = str(name)
            solar = name.startswith('com.victronenergy.solarcharger.')
            if solar:
                path = '/Link/ChargeVoltage'
            elif name.startswith('com.victronenergy.vebus.'):
                path = '/BatteryOperationalLimits/MaxChargeVoltage'
            else:
                continue
            connected = self._read_number(name, '/Connected')
            if connected == 0:
                continue
            value = self._read_number(name, path)
            if value is not None and not 0 <= value <= safe + .001:
                return False
            if connected != 1 or value is None:
                if solar and cached_solar_safe:
                    continue
                return False
            seen = True
        return seen

    def _health(self, now=None):
        return rec_health(self.bms, time.monotonic() if now is None else now,
                          self.cfg.live_timeout)

    def _safe_voltage(self, health=None):
        health = self._health() if health is None else health
        rec_limit = self.bms["cvl"] if health["Limits"]["valid"] else self.cfg.safe_cvl
        # A last known lower REC limit remains restrictive through an outage.
        previous = self.bms.get("cvl")
        if isinstance(previous, (int, float)) and math.isfinite(previous) and previous > 0:
            rec_limit = min(rec_limit, previous)
        return min(self.cfg.installation_max_v, rec_limit)

    def _charge_voltage_ceiling(self, safe, charge_permission=None):
        """A charge ban removes voltage headroom independently of DVCC CCL.

        Generic DVCC still adds rounded inverter compensation at CCL=0. This
        final command guard therefore sacrifices PV load service during a ban.
        The existing voltage-guard margin is provisional, not a new SOC/CVL
        calibration or a claim of instantaneous physical charger response.
        """
        health = self._health()
        safe = min(safe, self._safe_voltage(health))
        bms = self.bms
        if not all(item['valid'] for item in health.values()):
            reason = 'critical REC data unavailable'
        elif bms['ccl'] <= 0:
            reason = 'REC charge current limit is zero'
        elif (not bms['modulesOnline'] or bms['modulesBlockingCharge'] or bms['modulesOffline']):
            reason = 'REC module charging prohibition'
        elif bms['voltage'] >= safe - self.cfg.voltage_guard_v:
            reason = 'pack voltage guard'
        elif charge_permission is not None and charge_permission <= 0:
            reason = 'charging explicitly inhibited'
        else:
            return safe, ''
        # At least one command quantum below a fresh terminal measurement;
        # an unavailable bank cannot justify any nonzero voltage headroom.
        ceiling = (max(0.0, bms['voltage'] - max(self.cfg.voltage_guard_v, .01))
                   if health['Measurements']['valid'] else 0.0)
        return min(safe, voltage_floor(ceiling)), 'charge prohibited: ' + reason

    def _apply_voltage_commands(self, quattro, solar, safe, now, charge_permission=None):
        """Apply clamped individual commands; return (base, ready).

        A final raw-REC charge guard applies in every mode and during fallback.
        Readiness is command acknowledgment; physical response has its own delay.
        """
        safe, self.charge_guard_reason = self._charge_voltage_ceiling(safe, charge_permission)
        if self.charge_guard_reason:
            quattro, solar = min(quattro, safe), min(solar, safe)
            self.batt["/Info/MaxChargeCurrent"] = 0.0
        controller = self.voltage_control
        ready = controller.step(quattro, solar, safe)
        s = self._pub
        s["/RecBms/Voltage/RequestedQuattro"] = controller.requested_quattro
        s["/RecBms/Voltage/RequestedSolar"] = controller.requested_solar
        s["/RecBms/Voltage/AcceptedQuattro"] = controller.base
        s["/RecBms/Voltage/AcceptedSolar"] = (None if controller.offset is None
                                                  else controller.base + controller.offset)
        s["/RecBms/Voltage/Ready"] = int(ready)
        s["/RecBms/Voltage/Status"] = ((self.charge_guard_reason + "; ") if self.charge_guard_reason else "") + controller.status
        if not ready and not self._voltage_within_envelope(safe):
            self.batt["/Info/MaxChargeCurrent"] = 0.0
        return controller.base, ready

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
        health = self._health()
        if not all(item["valid"] for item in health.values()):
            return False, "critical BMS data unavailable"
        if bms["ccl"] <= 0 or not bms["modulesOnline"] or bms["modulesBlockingCharge"] or bms["modulesOffline"]:
            return False, "REC prohibits charging"
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
        ceiling = min(c.boost_ceiling_v, self._safe_voltage(health))
        if float(target) + volts > ceiling:
            return False, "target %.2f + %.2f > ceiling %.2fV" % (
                target, volts, ceiling)
        # The MPPTs ramp at a rate set by how far the bus sits below their
        # target. Measured 2026-08-19: ~0.15V of margin -> unthrottled in
        # 43-45 s, but only ~0.05V -> 126 s to reach 5 % of the step. With too
        # little margin the measurement window would open on an array that has
        # barely started, and that reading would be recorded as its capacity.
        # Refuse rather than return a number that is wrong and looks real.
        packv = bms.get("voltage")
        if packv is None:
            return False, "no pack voltage"
        margin = (float(target) + volts) - float(packv)
        if margin < c.boost_min_margin_v:
            return False, "margin %.2fV < %.2fV (pack %.2f, target %.2f)" % (
                margin, c.boost_min_margin_v, packv, float(target) + volts)
        return True, ""

    def _boost_requested(self, path, value):
        if hasattr(self, 'policy_adapter') and self.policy_adapter.contract.owned:
            return False
        return self._set_boost(value)

    def _set_boost(self, value):
        """Internal actuator primitive; protocol owns authorization."""
        try:
            volts = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(volts):
            return False
        if volts <= 0:
            self._boost_clear("released by requester")
            return True
        ok, why = self._boost_allowed(volts)
        if not ok:
            log.warning("solar boost refused (%.2fV): %s", volts, why)
            self._pub["/RecBms/SolarBoost/Status"] = "refused: " + why
            return False
        self.boost = {"active": True, "req_ts": time.monotonic(), "volts": volts}
        self._pub["/RecBms/SolarBoost/Applied"] = round(volts, 2)
        self._pub["/RecBms/SolarBoost/Active"] = 1
        self._pub["/RecBms/SolarBoost/Status"] = "ramp"
        log.info("solar boost +%.2fV for %.0fs (measure %.0f..%.0fs)", volts,
                 self.cfg.boost_hold_s, self.cfg.boost_measure_start_s,
                 self.cfg.boost_measure_start_s + self.cfg.boost_measure_len_s)
        return True

    def _boost_clear(self, reason):
        was = self.boost["active"]
        self.boost = {"active": False, "req_ts": 0.0, "volts": 0.0}
        s = self._pub
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

    @staticmethod
    def _sustain_idle():
        return {"active": False, "mode": 0, "req_ts": 0.0, "soc": None,
                "anchor_v": None, "anchor_soc": None, "servo_v": 0.0,
                "servo_ts": 0.0, "taper_since": 0.0, "sun_seen": False,
                "dark_since": 0.0, "logged_soc": None,
                "logged_servo": 0.0, "trim_a": 0.0}

    def _live_soc(self):
        if not self._health()["Soc"]["valid"]:
            return None
        soc = self.bms.get("socHiRes")
        return float(self.bms["soc"] if soc is None else soc)

    def _live_volts(self):
        return self.bms["voltage"] if self._health()["Measurements"]["valid"] else None

    def _fresh_pv(self, now):
        """PV current from systemcalc, or None when unknown or stale (the
        poll runs at DVCC's 3 s cadence)."""
        a, ts = self.pv_current if self.pv_current else (None, 0.0)
        if a is None or (now - ts) > 15:
            return None
        return a

    def _sustain_requested(self, path, value):
        if hasattr(self, 'policy_adapter') and self.policy_adapter.contract.owned:
            return False
        return self._set_sustain(value)

    def _set_sustain(self, value):
        """Internal actuator primitive; protocol owns authorization."""
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return False
        if mode == 0:
            self._sustain_clear("released by requester")
            return True
        if mode not in (SUSTAIN_FLOOR, SUSTAIN_CEILING, SUSTAIN_HOLD):
            return False
        if not self.cfg.sustain_enabled:
            self._pub["/RecBms/Sustain/Status"] = "refused: disabled in config"
            return False
        su = self.sustain
        if su["active"] and su["mode"] == mode:
            # the owner re-asserting its hold: keep the anchor, refresh expiry
            su["req_ts"] = time.monotonic()
            return True
        soc, volts = self._live_soc(), self._live_volts()
        self.sustain = self._sustain_idle()
        self.sustain.update(active=True, mode=mode, req_ts=time.monotonic())
        if soc is None or volts is None:
            # No SOC yet (a restart's first second, or a stale BMS): take the
            # request as PENDING and anchor on the first live tick.
            # Refusing it here left the full slider CVL on the Quattro for
            # the engine's 30 s re-assert cycle after every dbus-recbms
            # restart (2026-09-02). It expires like any hold if the BMS
            # never comes back.
            s = self._pub
            s["/RecBms/Sustain/Active"] = 1
            s["/RecBms/Sustain/Mode"] = mode
            s["/RecBms/Sustain/Soc"] = None
            s["/RecBms/Sustain/HoldVoltage"] = None
            s["/RecBms/Sustain/Servo"] = 0.0
            s["/RecBms/Sustain/Status"] = "pending: no SOC yet"
            log.info("sustain %s requested before the BMS is live; pending",
                     sustain_name(mode))
            return True
        self._sustain_anchor(soc, volts, self.bms.get("current"), "requested")
        return True

    def _rest_volts(self, volts, amps):
        """The pack voltage less the drop across its resistance: what it
        would read with no current flowing. anchor_ir_mohm is a rough
        figure and only needs to be; the SOC servo takes out the rest."""
        if amps is None:
            return float(volts)
        return float(volts) - float(amps) * self.cfg.sustain_anchor_r

    def _sustain_anchor(self, soc, volts, amps, why):
        """Start the hold where the bank IS: the held SOC is the present
        SOC, the hold voltage the present pack voltage (v1.8.0), less the
        drop across the pack's resistance so a bank being charged when the
        hold arrives does not anchor high, nor one under load low. The
        curve is not consulted -- it is calibrated on settled holds well
        inside the slider's range, and a hold outside that range clipped
        to the curve's edge held the bank a full 10 % away from where it
        was (2026-09-07). Whatever error remains, the SOC servo takes it
        out within minutes. A two-sided hold (mode 3) anchors exactly the
        same way -- present rest voltage, I x R compensated -- and its
        servo then works both ways from there."""
        su = self.sustain
        name = sustain_name(su["mode"])
        su["soc"] = su["logged_soc"] = su["anchor_soc"] = soc
        su["anchor_v"] = round(self._rest_volts(volts, amps), 2)
        su["servo_v"] = su["logged_servo"] = 0.0
        su["servo_ts"] = time.monotonic()
        su["taper_since"] = 0.0
        s = self._pub
        s["/RecBms/Sustain/Active"] = 1
        s["/RecBms/Sustain/Mode"] = su["mode"]
        s["/RecBms/Sustain/Soc"] = round(soc, 1)
        s["/RecBms/Sustain/HoldVoltage"] = su["anchor_v"]
        s["/RecBms/Sustain/Servo"] = 0.0
        s["/RecBms/Sustain/Status"] = name
        log.info("sustain %s at %.1f%% anchored %.2fV (%s; expires in %.0fs "
                 "unless re-asserted)", name, soc,
                 su["anchor_v"], why, self.cfg.sustain_hold_s)

    def _sustain_reanchor(self, volts, amps, why):
        """The bank moved a full step the hold's way: the hold voltage
        follows it -- a floor up and never down, a ceiling down and never
        up -- and the servo starts afresh from the new anchor. A two-sided
        hold (mode 3) never re-anchors on a step: it holds a destination,
        not wherever the bank got to, and its own servo is the correction.
        It re-anchors once on ARRIVAL at that destination (3.3.1, see
        _service_sustain): the bank's rest voltage there is the hold, and
        the servo it wound getting there is folded away."""
        su = self.sustain
        name = sustain_name(su["mode"])
        hold_v = su["anchor_v"] + su["servo_v"]
        volts = self._rest_volts(volts, amps)
        new = (max(hold_v, volts) if su["mode"] == SUSTAIN_FLOOR else
               min(hold_v, volts) if su["mode"] == SUSTAIN_CEILING else volts)
        su["anchor_v"] = round(new, 2)
        su["anchor_soc"] = su["soc"]
        su["servo_v"] = su["logged_servo"] = 0.0
        su["taper_since"] = 0.0
        log.info("sustain %s re-anchored %.2fV -> %.2fV (%s)",
                 name, hold_v, su["anchor_v"], why)

    def _sustain_band(self, available, soc, slider):
        """The MPPTs' headroom over the hold voltage this tick, in volts.

        One helper, because _tick_inner decides the lead in force and
        _service_sustain the charge target out of the same figure and they
        must not disagree. A floor gives the sun band_v while the bank is
        under the slider. A two-sided hold gives it always: PV has to be
        able to reach the loads at the destination, and the surplus is
        curtailed by the charge CURRENT limit there (_sustain_ccl), not by
        starving the MPPTs of voltage (SP23/SP28, repair plan B2). A
        ceiling never has one. `available` says whether the lead
        mechanism (systemcalc's solar offset, a Solar Priority tool) can be
        used at all; with it unavailable there is no way to give only the
        MPPTs headroom, so there is none. The standing lead's own full-
        target rule does not apply here: a one-way charge toward 100 %
        keeps its band, and only the arrival (COMPLETE_FULL, no hold) puts
        both chargers on the true target (owner, 2026-09-14)."""
        su = self.sustain
        c = self.cfg
        if not su["active"] or not available:
            return 0.0
        if su["mode"] == SUSTAIN_FLOOR:
            return c.sustain_band_v if (soc is None or soc < slider) else 0.0
        if su["mode"] == SUSTAIN_HOLD:
            # Always: PV must be able to flow to the loads at the
            # destination, so the MPPTs keep their headroom and the CHARGE
            # CURRENT limit is what curtails the surplus (_sustain_ccl).
            return c.sustain_band_v
        return 0.0

    def _sustain_target(self, band):
        """The charge voltage a hold asks for: the MPPT ceiling. A floor
        leaves the solar chargers `band` (one solar lead) above the hold
        voltage so the sun can raise the bank; a ceiling puts them at it,
        so nothing charges above where the bank was. The Quattro is
        commanded a lead under the target as always, which lands it ON the
        hold voltage under a floor and a lead under it under a ceiling.
        Under a two-sided hold the lead in force IS this band (0 or
        band_v), so the Quattro lands on the hold voltage either way."""
        su = self.sustain
        hold_v = round(su["anchor_v"] + su["servo_v"], 2)
        return round(min(hold_v + band, self.cfg.cvl_max), 2)

    def _sustain_clear(self, reason):
        was = self.sustain["active"]
        held = self.sustain["soc"]
        self.sustain = self._sustain_idle()
        s = self._pub
        s["/RecBms/Sustain/Request"] = 0
        s["/RecBms/Sustain/Active"] = 0
        s["/RecBms/Sustain/Mode"] = 0
        s["/RecBms/Sustain/Soc"] = None
        s["/RecBms/Sustain/HoldVoltage"] = None
        s["/RecBms/Sustain/Servo"] = 0.0
        s["/RecBms/Sustain/SecondsLeft"] = 0
        s["/RecBms/Sustain/Status"] = reason
        if was:
            log.info("sustain cleared at %.1f%% (%s); slider back in force",
                     held if held is not None else -1, reason)

    def _service_sustain(self, now, soc, slider, volts, amps):
        """Runs every tick. Expires the hold, ratchets the held SOC and
        re-anchors the hold voltage in the hold's own direction, servos
        it on the coulomb count and publishes the telemetry. Returns the
        charge voltage the hold asks for (see _sustain_target), or None
        when the slider itself applies. soc/volts/amps are None when the
        BMS is not live: the hold then stands still."""
        c = self.cfg
        su = self.sustain
        if not su["active"]:
            return None
        elapsed = now - su["req_ts"]
        if elapsed >= c.sustain_hold_s:
            self._sustain_clear("expired after %.0fs" % c.sustain_hold_s)
            return None
        floor = su["mode"] == SUSTAIN_FLOOR
        hold = su["mode"] == SUSTAIN_HOLD
        name = sustain_name(su["mode"])
        if su["soc"] is None:
            # pending: nothing to hold until the bank is known
            if soc is None or volts is None:
                self._pub["/RecBms/Sustain/SecondsLeft"] = int(c.sustain_hold_s - elapsed)
                return None
            self._sustain_anchor(soc, volts, amps, "was pending")
        # velib's SetValue short-circuits a write of the value the path
        # already holds (no callback), so a re-assert of the same mode would
        # never refresh the expiry, and a release (0) would be lost if the
        # path read 0. Reading the request back as -1 while active makes
        # every 0/1/2/3 write a change; the state lives in /Active and /Mode.
        self._pub["/RecBms/Sustain/Request"] = -1

        pv_a = self._fresh_pv(now)
        charging = shore_charging(amps, pv_a, c.sustain_q_idle_a)
        # v1.8.3: the DC loads alone drain this bank at ~0.9 A, under the
        # 1 A "Quattro charging" threshold 1.8.1 reused here, so a whole
        # night's drain went unseen. Draining has its own bar, and filling
        # -- current INTO the bank from any source, the sun included
        # (E08/D07) -- is the same bar the other way.
        draining = amps is not None and amps < -c.sustain_drain_a
        filling = amps is not None and amps > c.sustain_drain_a
        # A two-sided hold holds the DESTINATION: the slider is the
        # reference every tick, whatever the bank has reached (v3.2.0).
        held_eff = slider if hold else su["soc"]
        if soc is not None:
            sun = pv_a is not None and pv_a >= c.sustain_pv_min_a
            su["soc"] = sustain_hold(su["mode"], su["soc"], soc, charging, sun)
            # The held SOC has moved a full step the hold's way since the
            # hold voltage was last anchored: the voltage follows it (a
            # floor only ever rises this way on sun, see sustain_hold). A
            # two-sided hold ratchets nowhere, so it never re-anchors.
            if not hold:
                moved = (su["soc"] - su["anchor_soc"]) if floor else (su["anchor_soc"] - su["soc"])
                if moved >= c.sustain_step and volts is not None:
                    self._sustain_reanchor(volts, amps, "SOC %+.1f%% since the anchor, now %.1f%%"
                                           % (moved if floor else -moved, su["soc"]))
                # a floor never holds more than the owner set, a ceiling never
                # less: the servo judges the bank against the bounded value
                held_eff = min(su["soc"], slider) if floor else max(su["soc"], slider)
            if now - su["servo_ts"] >= c.sustain_servo_s and self.boost["active"]:
                # 3.4.2: a solar boost is a MEASUREMENT -- the charge limit
                # is lifted on purpose and the bank fills on purpose for two
                # minutes. Regulating on that would be regulating on our own
                # experiment: the boat's first boost on 3.4.1 (2026-09-15
                # 20:50 UTC) wound the trim from 0 to -2.0 A because the
                # unthrottled arrays filled the held bank, and the moment
                # the boost ended the limit fell back onto its 0.1 A floor,
                # which is exactly the starvation the boost was meant to
                # cure. The servo and the trim stand still while a boost
                # runs; the period restarts when it ends.
                su["servo_ts"] = now
            elif now - su["servo_ts"] >= c.sustain_servo_s:
                su["servo_ts"] = now
                d = sustain_servo(su["mode"], soc - held_eff, charging,
                                  c.sustain_servo_db, draining, filling)
                if d:
                    su["servo_v"] = max(-c.sustain_servo_down, min(
                        c.sustain_servo_up, su["servo_v"] + d * c.sustain_servo_v))
                if hold:
                    # The current trim: the destination's charge limit is
                    # the DC support DVCC does not add itself, but the GX
                    # rounds its allocation (whole amps for the Quattro, a
                    # ceiling for the MPPTs' inverter compensation), so the
                    # bank creeps a fraction of an amp either way. A voltage
                    # step is 0.02 V / 3 mOhm = several amps, far too coarse
                    # for that; a half-amp trim on the published limit, at
                    # the servo's own cadence, is not. Down while the bank
                    # is filling above its destination, up while it drains
                    # under it; a bank above target that is not being filled
                    # is left to the loads.
                    err = soc - held_eff
                    if err > c.sustain_servo_db and filling:
                        su["trim_a"] = max(-c.sustain_trim_max_a, su["trim_a"] - c.sustain_trim_a)
                    elif err < 0 and draining:
                        su["trim_a"] = min(c.sustain_trim_max_a, su["trim_a"] + c.sustain_trim_a)
                    # 3.4.1: a trim outlives its cause otherwise. The boat's
                    # re-accept surge of 2026-09-15 filled the bank to
                    # 50.2 %, the trim wound to -2.5 A, and with the bank
                    # then draining (not filling) nothing ever unwound it:
                    # the cap sat on its 0.1 A floor all afternoon, the
                    # MPPTs made 6 W under a 75 V sky, the DC loads came
                    # out of the bank on shore, and the engine could not
                    # even measure the arrays. A trim relaxes toward zero
                    # once the bank stops doing what it was trimmed for.
                    elif su["trim_a"] < 0 and not filling:
                        su["trim_a"] = min(0.0, su["trim_a"] + c.sustain_trim_a)
                    elif su["trim_a"] > 0 and not draining:
                        su["trim_a"] = max(0.0, su["trim_a"] - c.sustain_trim_a)
            if (hold and volts is not None and abs(su["servo_v"]) >= c.sustain_arrival_fold_v > 0
                    and held_eff - c.sustain_servo_db <= soc <= held_eff + c.sustain_servo_db):
                # Arrival snap (3.3.1). A hold that lifts (or lowers) the
                # bank to its destination itself winds the servo the whole
                # way -- the boat's first HOLD, 2026-09-14: taken at 49.1 %
                # for 50 %, +0.50 V in twelve minutes, and at 49.9 % the
                # Quattro sat 0.45 V above a bank it may no longer fill.
                # The current limit is the only thing restraining it there,
                # a 0 A limit the Quattro honours slowly (2 A for minutes),
                # and every solar boost lifts that limit: 6 A into a held
                # bank per boost. Arriving is a measurement like taking the
                # hold: the voltage becomes the bank's present rest voltage
                # and the servo starts again from nothing, so the Quattro
                # is ON the bank with no headroom to spend, boost or not.
                # Only a servo that carries a real lift or descent
                # (arrival_fold_v, 0.25 V) is folded: the few hundredths
                # the servo keeps at the band's edge to cover the loads
                # are not an arrival, and folding them re-anchored the
                # hold every 30 s all night while the bank sat on the
                # edge (boat, 2026-09-15 13:32-14:08 UTC).
                self._sustain_reanchor(volts, amps, "arrived at %.1f%% for %.1f%%: bank %.2fV at %.1fA, servo %+.2fV folded"
                                       % (soc, held_eff, volts, amps if amps is not None else 0.0, su["servo_v"]))
        # The solar band (see _sustain_band): under a floor the MPPTs get
        # band_v of headroom above the hold voltage while the bank is under
        # the slider; under a two-sided hold always, the current limit
        # doing the curtailing at the destination. The tick set
        # the lead in force from the same helper, so the Quattro is
        # commanded target - band = the hold voltage itself. The band is
        # asked for even while the offset goes unapplied (issue #3):
        # dropping it alone put the Quattro a band UNDER the hold and let
        # the bank drain; with the offset ignored the chargers simply all
        # get the hold voltage, which is the restrictive outcome, and the
        # fault reports it. A ceiling has no band, but lets the bank charge
        # back up to the slider's own point if it is under it.
        band = self._sustain_band(getattr(self, 'band_available', False), soc, slider)
        target = self._sustain_target(band)
        if not floor and not hold and soc is not None and soc < slider:
            target = max(target, round(self._slider_cvl(slider), 2))
        # Dusk snap (v1.8.4). A hold's voltage is only ever set from a
        # measurement: when the hold is taken, on a full SOC step, when the
        # band is absorbed -- and here, when the sun goes. A sunny afternoon
        # raises the held SOC without a full step, and the Quattro's command
        # then sits under the bank all night (2026-09-10: 0.13 V under, a
        # steady 50 W out, 1.5 % lost). Once PV current has been under
        # pv_min_a for dusk_s after a day of sun, the hold snaps to the
        # bank's present voltage (less the drop across the pack): a floor
        # never down, a ceiling never up. One snap per night; the servo
        # below is only the backstop after it. A two-sided hold has no
        # snap and no band-absorbed step: those are one-way staircase
        # mechanisms, and its servo answers both directions already.
        if not hold and soc is not None and volts is not None:
            sun_now = pv_a is not None and pv_a >= c.sustain_pv_min_a
            if sun_now:
                su["sun_seen"] = True
                su["dark_since"] = 0.0
            elif su["sun_seen"]:
                if not su["dark_since"]:
                    su["dark_since"] = now
                elif now - su["dark_since"] >= c.sustain_dusk_s:
                    su["sun_seen"] = False
                    su["dark_since"] = 0.0
                    self._sustain_reanchor(volts, amps, "dusk: PV gone %.0fs, bank %.2fV at %.1fA"
                                           % (c.sustain_dusk_s, volts, amps if amps is not None else 0.0))
                    target = self._sustain_target(band)
        # Band absorbed: the MPPTs sit at their ceiling, the charge current
        # has tapered, the sun (not the Quattro) is what holds it there.
        # The bank has genuinely taken the band, so the staircase steps up
        # by one more band -- without waiting for a full SOC step, which a
        # steep part of the curve might never yield inside one band.
        if floor and band > 0 and soc is not None and volts is not None and amps is not None:
            at_top = (volts >= target - c.sustain_taper_margin
                      and 0 <= amps <= c.sustain_taper_a
                      and pv_a is not None and pv_a >= c.sustain_pv_min_a
                      and not charging)
            if not at_top:
                su["taper_since"] = 0.0
            elif not su["taper_since"]:
                su["taper_since"] = now
            elif now - su["taper_since"] >= c.sustain_taper_s:
                self._sustain_reanchor(volts, amps, "band absorbed: %.2fV at %.1fA, PV %.1fA"
                                       % (volts, amps, pv_a))
                target = self._sustain_target(band)
        if abs(held_eff - su["logged_soc"]) >= c.sustain_step:
            log.info("sustain %s now at %.1f%%", name, held_eff)
            su["logged_soc"] = held_eff
        if abs(su["servo_v"] - su["logged_servo"]) >= 0.05 - 1e-9:
            log.info("sustain %s servo %+.2fV: bank %.2f%% vs held %.1f%% (%s)",
                     name, su["servo_v"], soc if soc is not None else -1, held_eff,
                     "Quattro charging" if charging else
                     "filling" if filling else "draining")
            su["logged_servo"] = su["servo_v"]
        s = self._pub
        s["/RecBms/Sustain/Soc"] = round(held_eff, 1)
        s["/RecBms/Sustain/HoldVoltage"] = round(su["anchor_v"] + su["servo_v"], 2)
        s["/RecBms/Sustain/Servo"] = round(su["servo_v"], 2)
        s["/RecBms/Sustain/TrimA"] = round(su["trim_a"], 1)
        s["/RecBms/Sustain/SecondsLeft"] = int(c.sustain_hold_s - elapsed)
        return target

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
        self.eff_cv = (v, time.monotonic())
        self._pub["/RecBms/DvccEffectiveChargeVoltage"] = v
        # v1.7.0: is Solar Priority on? Its setting; absent (driver not
        # installed) reads as off, and then no lead is applied.
        sp = self._settings_get("/Settings/SolarPriority/Enabled")
        try:
            self.sp_enabled = bool(int(sp)) if sp is not None else None
        except (TypeError, ValueError):
            self.sp_enabled = None
        # PV current for the sustain charge limit (same 3 s cadence as DVCC)
        try:
            raw = self.sbus.call_blocking(
                self.cfg.boost_service, "/Dc/Pv/Current", BUSITEM, "GetValue", "", [], timeout=2)
            a = float(raw)
            if not (0 <= a <= 500):
                a = None
        except Exception:
            a = None
        self.pv_current = (a, time.monotonic())
        return True

    def _sustain_ccl(self, now, held, ccl, soc=None, slider=None):
        """The charge current limit to publish while a hold is in force.

        DVCC hands the MPPTs the whole BMS limit (plus DC loads) and the
        Quattro only what remains after their smoothed current. Publishing
        "present PV current + charge_limit_a" therefore leaves every MPPT a
        few amps above what it already makes -- tracker active, free to ramp
        a few amps per DVCC cycle -- while the Quattro can never bulk. That
        is what stops the 0.6-2 kW re-absorb it does on every AC re-accept
        (measured 2026-09-02, 13 min and >100 Wh of shore per reconnect),
        which no CVL can, since it happens with the pack above its command.
        Lifted during a solar boost (the measurement needs the MPPTs truly
        unthrottled) and whenever PV current cannot be read."""
        c = self.cfg
        if held is None or c.sustain_ccl_a <= 0 or self.boost["active"]:
            return ccl, None
        su = self.sustain
        if (su["mode"] == SUSTAIN_HOLD and soc is not None and slider is not None
                and soc >= slider - c.sustain_servo_db):
            # At the destination the surplus is curtailed by CURRENT, not
            # by voltage (repair plan B2: "curtail excess PV once the loads
            # are covered"). DVCC hands the chargers this limit plus what it
            # compensates itself -- the inverter's draw while islanded, and
            # the DC system only when it is metered -- so the published
            # figure is the DC load the GX does not add (about 1 A here:
            # /Dc/System/MeasurementType 0, 58 W read 2026-09-14), or 0 A
            # when it does. The MPPTs then carry exactly the loads and the
            # Quattro gets whatever they cannot, into the loads and not the
            # bank: no fill to burn back, no re-accept surge at all. With
            # the demand unknown the floor's own cap stands in.
            support = self._dc_support_a()
            if support is not None:
                # Never exactly 0: DVCC adds its DC compensation only to a
                # positive limit, and 0 would leave the MPPTs unable to
                # serve even the DC loads (fixture, 2026-09-14). A tenth of
                # an amp keeps the compensation alive.
                cap = max(SUSTAIN_HOLD_MIN_A, support + su["trim_a"])
                return min(ccl, cap), round(cap, 1)
        a = self._fresh_pv(now)
        if a is None:
            return ccl, None
        cap = a + c.sustain_ccl_a
        return min(ccl, cap), round(cap, 1)

    def _dc_support_a(self):
        """The DC load current DVCC will not add to the chargers' allowance
        by itself: the policy adapter's demand model says whether the DC
        system is metered (then DVCC adds it and this is 0) or estimated
        (then it is the estimate over the pack voltage). None without a
        valid demand or pack voltage."""
        adapter = getattr(self, "policy_adapter", None)
        demand = (getattr(adapter, "last_snapshot", None) or {}).get("demand") or {}
        volts = self.bms.get("voltage")
        if not demand.get("valid") or not isinstance(volts, (int, float)) or volts <= 0:
            return None
        if demand.get("measured_dc"):
            return 0.0
        return max(0.0, float(demand.get("external_dc_w") or 0.0)) / float(volts)

    def _regulation_fault(self, now, ready):
        """A voltage pair that stays unapplied is a fault, not a wait.

        The envelope controller reports readiness every tick, and an ordinary
        update -- a servo step, a slider move, a boost edge -- is not ready
        for a few seconds while the base, the offset and the chargers'
        readbacks catch up (7 s in the offline plant). A mismatch that
        persists lead_verify_s is raised on the existing surface
        (/RecBms/LeadFault, InternalFailure warning, boosts refused, lead
        reported 0) and clears the tick the pair is verified again. The
        commands themselves are not changed: no full-slider fallback.
        """
        f = self.lead_fault
        if ready:
            f["mismatch_since"] = 0.0
            if f["active"]:
                log.info("voltage commands verified again; regulation fault cleared")
                f.update(active=False, msg="")
            return
        if not f["mismatch_since"]:
            f["mismatch_since"] = now
            return
        if f["active"] or now - f["mismatch_since"] < self.cfg.lead_verify_s:
            return
        control = self.voltage_control
        why = self.applied_reason or control.status
        if why.startswith("systemcalc ignores the solar offset"):
            why += (". GX access level is %s (need 3 = Superuser); after raising it "
                    "run 'svc -t /service/dbus-systemcalc-py'"
                    % self._settings_get("/Settings/System/AccessLevel"))
        msg = "unapplied for %.0fs: %s [%s; requested %s/%s V, accepted %s/%s V]" % (
            now - f["mismatch_since"], why, control.status,
            control.requested_quattro, control.requested_solar, control.base,
            None if control.offset is None or control.base is None else round(control.base + control.offset, 2))
        f.update(active=True, since=now, msg=msg)
        log.error("REGULATION FAULT: %s -- boosts refused, lead reported 0", msg)

    def _service_boost(self, now, target):
        """Runs every tick, after the target CVL for this tick is known.
        Maintains the systemcalc offset = standing lead + any active boost
        (the path is volatile, so it is re-asserted every tick), expires or
        aborts the boost, and publishes the boost telemetry. Returns the
        lead actually in force, so the caller can publish target - lead as
        the Quattro's CVL."""
        c = self.cfg
        b = self.boost
        s = self._pub
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
        lead = self.lead_v
        if not all(item["valid"] for item in self._health().values()):
            lead, boost_v = 0.0, 0.0
        safe = self._safe_voltage()
        base, ready = self._apply_voltage_commands(
            target - lead, target + boost_v, safe, now)
        accepted = self.voltage_control
        effective = None if accepted.offset is None else base + accepted.offset
        s["/RecBms/SolarBoost/EffectiveChargeVoltage"] = effective
        s["/RecBms/SolarBoost/Applied"] = max(0.0, (effective or target) - target) if ready else 0.0
        s["/RecBms/SolarBoost/WindowOpen"] = int(bool(s["/RecBms/SolarBoost/WindowOpen"]) and ready)
        if ready and accepted.offset is not None:
            self._last_verified_voltage = (base, base + accepted.offset)
        self._regulation_fault(now, ready)
        s["/RecBms/LeadFault"] = self.lead_fault["msg"] if self.lead_fault["active"] else ""
        # The lead in force is the verified one. Through an ordinary update
        # the last verified pair still stands on the chargers; a pair that
        # was never verified, or has gone unapplied past the fault, is no
        # lead at all (E14/E15 reported 0.30 V with an effective offset of 0).
        verified = ready or (getattr(self, "_last_verified_voltage", None) is not None
                             and not self.lead_fault["active"])
        return min(lead, max(0.0, target - base)) if verified else 0.0

    def _boost_shutdown(self):
        """Protection first, relay second -- and only on an orderly exit.

        D02/A1: this used to hand the relay back to shore BEFORE zeroing the
        current and lowering the commands, so the Quattro could re-accept AC
        while still holding the old pair (E03: pack 56.41 V against CVL
        59.34 V and CCL 200 A at closure). CCL 0 and the safe voltage pair go
        out first, then the return, then the ledger checkpoint.

        This is best effort on SIGTERM/SIGINT/atexit only. SIGKILL, a crash or
        power loss skip it entirely, and once this process dies the battery
        service disappears from D-Bus with every BMS limit DVCC distributes
        from it, so the chargers fall back to their own settings. Publishing a
        command here is not evidence that the hardware applied it.
        """
        if self.boost.get("active"):
            log.info("solar boost released on shutdown")
        self.batt["/Info/MaxChargeCurrent"] = 0.0
        self._apply_voltage_commands(self.cfg.safe_cvl, self.cfg.safe_cvl,
                                     self._safe_voltage(), time.monotonic(), charge_permission=0.0)
        if hasattr(self, 'policy_adapter'):
            self.policy_adapter.shutdown()

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
                        time.monotonic() - self._last_short_warn > 60:
                    log.warning("dropped short frame 0x%03X (len %d, need %d)",
                                bmsid, len(data), MIN_LEN[bmsid])
                    self._last_short_warn = time.monotonic()
                if ok and not self._first_frame_logged:
                    log.info("first BMS frame decoded %.0fs after start",
                             time.monotonic() - self.start_mono)
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
                    self.extv = (v, time.monotonic())
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
        with self.batt as ctx:
            self._pub = ctx
            try:
                result = self._tick_inner()
            finally:
                self._pub = self.batt
        if not self.heartbeat.pulse():
            self._boost_shutdown()
            raise SystemExit('REC watchdog supervisor disappeared')
        return result

    def _tick_inner(self):
        c = self.cfg
        bms = self.bms
        now = time.monotonic()      # elapsed-time basis for every primitive below

        # Critical receipt clocks are independent. A serial/heartbeat frame
        # cannot keep stale limits, shunt samples or cell protections LIVE.
        health = self._health()
        received = bms.get("_received", {})
        never_seen = not received
        ages = [item["age"] if item["age"] is not None else
                max(0.0, time.monotonic() - self.start_mono)
                for item in health.values()]
        age = max(ages)
        live = all(item["valid"] for item in health.values())
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

        volts = v("voltage")
        amps = v("current") if live else 0.0

        # ---- CVL control: slider (or sustain) + weekly equalization ----
        slider = float(self.settings["chargeslider"] or c.slider_default)
        self.policy_adapter.prepare_intents(slider)
        # The standing solar lead (v1.7.0) is decided first: a sustain hold
        # uses it as the MPPTs' headroom over the hold voltage. Only the
        # BMS's own SOC judges the band, never the safe substitute.
        bms_soc = bms.get("socHiRes") if bms.get("socHiRes") is not None else bms.get("soc")
        live_soc = float(bms_soc) if (live and bms_soc is not None) else None
        self.lead_v = standing_lead(c.solar_lead, slider, c.lead_full_pct,
                                    self.sp_enabled, c.lead_needs_sp)
        # Under a floor the lead IS the MPPTs' solar band over the hold
        # voltage (the Quattro sits on the hold voltage either way), and a
        # band the size of the standing lead throttled the sun most of a
        # simulated day: widen it to band_v while the floor holds. Under a
        # two-sided hold the lead IS the band exactly, band_v or nothing:
        # leaving the standing 0.15 V in force at the destination would put
        # the Quattro that much UNDER the hold -- the very thing repair
        # plan B2 warns about, and what drained 1.448 points in 24 h (E04).
        self.band_available = bool(c.solar_lead > 0 and (not c.lead_needs_sp or self.sp_enabled))
        band = self._sustain_band(self.band_available, live_soc, slider)
        if self.sustain["active"] and self.sustain["mode"] == SUSTAIN_HOLD:
            self.lead_v = band
        else:
            self.lead_v = max(self.lead_v, band)
        if self._lead_logged != self.lead_v:
            log.info("solar lead %.2fV -> %.2fV (Solar Priority %s, "
                     "slider %.0f%%, full at %.0f%%)",
                     self._lead_logged or 0.0, self.lead_v,
                     "on" if self.sp_enabled else "off", slider,
                     c.lead_full_pct)
            self._lead_logged = self.lead_v
        # Sustain (v1.5.0, voltage-anchored since v1.8.0): while held, the
        # charge voltage comes from the hold, not from the slider's curve.
        # In fallback the hold stands still (no SOC, no voltage to judge).
        # Only the BMS's OWN figures go in, never the safe substitutes: a
        # pending floor anchored on the first live tick after a restart,
        # before the SOC frame had arrived, took the 50 % stand-in as the
        # held SOC (2026-09-10 17:01 UTC, bank at 36 %).
        held = self._service_sustain(
            now, live_soc, slider,
            bms["voltage"] if (live and bms.get("voltage") is not None) else None,
            bms["current"] if (live and bms.get("current") is not None) else None)
        slider_cvl = held if held is not None else self._slider_cvl(slider)
        final_cvl = slider_cvl
        eq_label = 'disabled: engine controls full-charge target'

        safe_voltage = self._safe_voltage(health)
        final_cvl = min(final_cvl, safe_voltage)
        voltage_guard = (health["Measurements"]["valid"] and
                         bms["voltage"] >= safe_voltage - c.voltage_guard_v)
        if voltage_guard or not live:
            self.lead_v = 0.0
            if self.boost["active"]:
                self._boost_clear("pack voltage guard" if voltage_guard else "critical BMS data unavailable")

        # ---- resolve outputs ----
        if live:
            ccl, dcl, dvl = v("ccl"), v("dcl"), v("dvl")
            ccl, applied = self._sustain_ccl(now, held, ccl, live_soc, slider)
            self._pub["/RecBms/Sustain/ChargeLimit"] = applied
        else:
            ccl, dcl, dvl = fb
            # Retain a lower known prohibition through partial-data outages.
            if bms.get("dcl") is not None:
                dcl = min(dcl, max(0.0, bms["dcl"]))
        no_modules = bms.get("modulesOnline") == 0
        if voltage_guard or no_modules or bms.get("modulesBlockingCharge") or bms.get("modulesOffline"):
            ccl = 0.0
        if no_modules or bms.get("modulesBlockingDischarge") or bms.get("modulesOffline"):
            dcl = 0.0

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

        s = self._pub
        # The Quattro's absorption holds +0.05..0.15V ABOVE its commanded
        # CVL (measured 2026-08-19: SVS on, BMS/Quattro/sense meters all
        # within 10mV, VebusChargeState=absorption, pack held steady at
        # CVL+0.07V while the tail decayed). The MPPTs regulate accurately,
        # so command the Quattro solar_lead_v BELOW the target and raise
        # only the solar chargers back up to it: the Quattro lands at or
        # under the calibrated equilibrium and solar finishes the top-off.
        target = voltage_floor(final_cvl)
        lead = self._service_boost(now, target)
        s["/RecBms/TargetChargeVoltage"] = target
        s["/RecBms/TargetSoc"] = slider
        s["/RecBms/SolarLead"] = round(lead, 2)
        s["/Info/MaxChargeCurrent"] = (ccl if self.voltage_control.ready or
            (getattr(self, '_last_verified_voltage', None) is not None and
             self._voltage_within_envelope(safe_voltage)) else 0.0)
        s["/Info/MaxDischargeCurrent"] = dcl
        s["/Info/BatteryLowVoltage"] = dvl
        s["/Dc/0/Voltage"] = _q(volts, c.voltage_step)
        s["/Dc/0/Current"] = _q(amps, c.current_step)
        s["/Dc/0/Power"] = _q(volts * amps, c.power_step)
        s["/Dc/0/Temperature"] = _q(v("temperature"), c.temperature_step)
        s["/Soc"] = _q(soc, c.soc_step)
        s["/Soh"] = bms.get("soh")
        s["/Capacity"] = _q(remaining, c.ah_step)
        s["/ConsumedAmphours"] = _q(remaining - installed, c.ah_step)  # BMV convention: negative
        s["/InstalledCapacity"] = installed
        s["/TimeToGo"] = _q(ttg, c.time_step)

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

        s["/RecBms/Health/CriticalValid"] = int(live)
        for group, item in health.items():
            s["/RecBms/Health/%s/Age" % group] = item["age"]
            s["/RecBms/Health/%s/Valid" % group] = int(item["valid"])
        raw_fields = {
            "Voltage": ("voltage", "Measurements"),
            "Current": ("current", "Measurements"), "Soc": ("soc", "Soc"),
            "ChargeVoltageLimit": ("cvl", "Limits"),
            "ChargeCurrentLimit": ("ccl", "Limits"),
            "DischargeCurrentLimit": ("dcl", "Limits"),
        }
        for name, (key, group) in raw_fields.items():
            raw = (bms.get("socHiRes") if key == "soc" and bms.get("socHiRes") is not None
                   else bms.get(key))
            s["/RecBms/Raw/" + name] = raw if health[group]["valid"] else None
        s["/RecBms/SafeChargeVoltage"] = safe_voltage
        s["/RecBms/Phase"] = phase_name
        policy_control = self.policy_adapter.tick(
            live, slider, soc, volts, amps, self._slider_cvl(slider), safe_voltage,
            ccl)
        s["/RecBms/EqStatus"] = eq_label
        s["/RecBms/TimeToFull"] = _q(ttf, c.time_step)
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
