"""Deterministic, offline coupling of the actual REC and Solar Priority drivers.

This is an acceptance-test fixture, never an installation calibration. The model
uses a resistive OCV battery, finite PV/charger ramps, configurable DC compensation
and independently delayed charger controls with burst/taper response. Installed
DVCC current allocation is represented; charger timing profiles are provisional.
It does not model cell electrochemistry, AC transients, balancing or an independent
hardware watchdog.
Positive battery current/power means charging. All energy is integrated on the
DC bus; shore Wh includes AC pass-through and charger conversion loss.

The asynchronous bus separates request acceptance, actuator application and
monitor readback. No production controller is replaced by a simulator policy.
"""
from dataclasses import asdict, dataclass, field
from contextlib import ExitStack
from unittest.mock import patch
import heapq
import itertools
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import types

import test_stubs as stubs

ROOT = Path(__file__).resolve().parent
SYSTEM = "com.victronenergy.system"
VEBUS = "com.victronenergy.vebus.plant"
ARRAYS = ("com.victronenergy.solarcharger.plant_a",
          "com.victronenergy.solarcharger.plant_b")
OFFSET = "/Debug/BatteryOperationalLimits/SolarVoltageOffset"
SETTINGS = "com.victronenergy.settings"
# The GX's own statement of what each AC input is (0 none, 1 grid, 2
# generator, 3 shore power), which is where a resolver looks first.
GX_INPUT = "/Settings/SystemSetup/AcInput%d"
CONTROL = "/Ac/Control/IgnoreAcIn%d"
STATE = "/Ac/State/IgnoreAcIn%d"
AVAILABLE = "/Ac/State/AcIn%dAvailable"
IGNORE = CONTROL % 1
BASE = "/Info/MaxChargeVoltage"
CCL = "/Info/MaxChargeCurrent"


@dataclass
class Latency:
    request_s: float = 0.2
    reply_s: float = 0.1
    readback_s: float = 0.5
    base_s: float = 1.0
    offset_s: float = 0.5
    current_s: float = 1.0
    relay_s: float = 2.0


@dataclass
class PlantConfig:
    capacity_ah: float = 1440.0
    initial_soc: float = 60.0
    resistance_ohm: float = 0.003
    # An explicit synthetic endpoint, not evidence of the installed REC endpoint.
    ocv_points: tuple = ((0, 50), (40, 54.42), (62.3, 56.65), (100, 61.96))
    inverter_efficiency: float = 0.90
    charger_efficiency: float = 0.93
    inverter_idle_w: float = 40.0
    quattro_max_w: float = 5000.0
    shore_max_w: float = 7000.0
    # Which physical Quattro AC input shore power arrives on (1 or 2); the
    # other input is the alternate supply. The owner rewired shore from AC in
    # 1 to AC in 2 on 2026-09-15, and no service is told which it is.
    shore_input: int = 1
    # The GX's two AC input types, seeded to agree with shore_input (shore
    # power on the shore input, nothing on the other). An explicit (type1,
    # type2) pair overrides that: (0, 0) is a GX nobody ever configured.
    gx_input_types: tuple = None
    pv_ramp_w_s: float = 100.0
    quattro_ramp_w_s: float = 200.0
    quattro_voltage_bias_v: float = 0.08
    reconnect_surge_w: float = 1200.0
    reconnect_surge_s: float = 150.0
    quattro_burst_hold_s: float = 5.0
    quattro_current_response_s: float = 3.0
    quattro_voltage_response_s: float = 3.0
    quattro_charge_needed_hysteresis_v: float = 0.02
    pv_voltage_response_s: float = 1.0
    # Historical rise anchors, not a calibrated transfer function. The smooth
    # interpolation and all downward-response/offset variants are synthetic.
    mppt_response_profile: str = "historical_headroom"
    mppt_fall_response_s: float = 3.0
    mppt_response_scales: tuple = (1.0, 1.0)
    mppt_voltage_sense_offsets_v: tuple = (0.0, 0.0)
    mppt_headroom_loss_v: tuple = (0.0, 0.0)
    # Source-derived DVCC cadence and LPF coefficients; physical response above
    # is an explicit provisional profile, not installed calibration.
    dvcc_sample_s: float = 1.0
    dvcc_adjust_s: float = 3.0
    pv_filter_s: float = 20.0 / (2 * math.pi)
    vebus_filter_s: float = 30.0 / (2 * math.pi)
    array_current_limits_a: tuple = (20.0, 35.0)
    polarization_ohm: float = 0.0
    polarization_response_s: float = 120.0
    dc_measurement: str = "measured"
    dvcc_compensation: bool = True
    # Firmware-dependent: fixture default explicitly adds negative VE.Bus DC
    # draw to PV allowance. Scenarios can test firmware without that behavior.
    dvcc_inverter_compensation: bool = True
    dvcc_quantization: bool = True
    initial_base_v: float = 54.0
    initial_offset_v: float = 0.0
    # Reported VE.Bus /Dc/0/Power is approximately AC-in minus AC-out, not net
    # DC power: it retained a 25-26 W residual at 0.0 A measured current on the
    # boat (2026-09-12). /Dc/0/Current and /Dc/0/Voltage stay the coherent
    # truth; only the reported power field carries this fixed disagreement.
    vebus_reported_overhead_w: float = 25.0


@dataclass
class Energy:
    shore_wh: float = 0.0
    shore_charge_wh: float = 0.0
    pv_wh: float = 0.0
    pv_available_wh: float = 0.0
    pv_curtailed_wh: float = 0.0
    quattro_burst_wh: float = 0.0
    solar_below_bank_s: float = 0.0
    ac_load_wh: float = 0.0
    dc_load_wh: float = 0.0
    conversion_wh: float = 0.0
    charge_wh: float = 0.0
    discharge_wh: float = 0.0
    charge_ah: float = 0.0
    discharge_ah: float = 0.0
    reverse_wh: float = 0.0
    max_balance_error_w: float = 0.0


class Clock:
    def __init__(self, epoch=1_800_000_000.0):
        self.epoch, self.elapsed = float(epoch), 0.0

    def time(self):
        return self.epoch + self.elapsed

    def monotonic(self):
        return self.elapsed


class DelayedBus:
    """One deterministic bus with independently faultable writes and sources."""
    def __init__(self, clock, latency):
        self.clock, self.latency = clock, latency
        self.services, self.monitors = {}, []
        self.events, self.serial = [], itertools.count()
        self.refusals, self.ineffective = {}, set()
        self.invalid, self.removed, self.unresponsive = set(), set(), set()
        self.writes, self.on_publication = [], None
        self.settings = stubs.FakeBus.store

    def later(self, delay, fn):
        heapq.heappush(self.events, (self.clock.elapsed + delay, next(self.serial), fn))

    def drain(self):
        while self.events and self.events[0][0] <= self.clock.elapsed + 1e-9:
            _, _, fn = heapq.heappop(self.events)
            fn()

    def register(self, service):
        self.services[service.name] = service
        stubs.FakeBus.names[service.name] = service

    def publish(self, name, path, value):
        if self.on_publication:
            self.on_publication(name, path, value)
        if name in self.removed or name in self.unresponsive or (name, path) in self.invalid:
            return
        for monitor in self.monitors:
            self.later(self.latency.readback_s,
                       lambda m=monitor: m.receive(name, path, value))

    def read(self, name, path):
        if name in self.unresponsive:
            raise TimeoutError("service unresponsive: " + name)
        if name in self.removed:
            raise RuntimeError("service unavailable: " + name)
        if (name, path) in self.invalid:
            return None
        if name == "com.victronenergy.settings":
            return self.settings.get(path)
        service = self.services.get(name)
        if service is None:
            raise RuntimeError("service unavailable: " + name)
        if path == "/":
            # VeDbusTreeExport.GetValue uses paths relative to the requested
            # tree root; D-Bus represents an invalid scalar as an empty array.
            # https://raw.githubusercontent.com/victronenergy/velib_python/master/vedbus.py
            result = {}
            for key in service.values:
                value = self.read(name, key)
                result[key.lstrip("/")] = [] if value is None else value
            return result
        return service.values.get(path)

    def write(self, name, path, value, asynchronous=False, reply_handler=None,
              error_handler=None):
        event = {"requested_s": self.clock.elapsed, "service": name,
                 "path": path, "value": value, "code": None}
        self.writes.append(event)

        def accept():
            try:
                if name in self.unresponsive:
                    raise TimeoutError("service unresponsive: " + name)
                if name in self.removed or name not in self.services:
                    raise RuntimeError("service unavailable: " + name)
                code = self.refusals.get((name, path), self.refusals.get(path, 0))
                service = self.services[name]
                if not code and (name, path) not in self.ineffective and path not in self.ineffective:
                    code = 0 if service.accept(path, value) else 2
                event.update(accepted_s=self.clock.elapsed, code=code)
                if reply_handler:
                    self.later(self.latency.reply_s, lambda: reply_handler(code))
                return code
            except Exception as exc:
                event.update(accepted_s=self.clock.elapsed, error=str(exc))
                if asynchronous and error_handler:
                    self.later(self.latency.reply_s, lambda err=exc: error_handler(err))
                    return None
                raise
        if asynchronous:
            self.later(self.latency.request_s, accept)
            return None
        return accept()

    def invalidate(self, name, path):
        self.invalid.add((name, path))
        for monitor in self.monitors:
            monitor.receive(name, path, None)

    def remove(self, name):
        self.removed.add(name)
        for monitor in self.monitors:
            if monitor.removed:
                monitor.removed(name, monitor.get_device_instance(name))
            for key in list(monitor.cache):
                if key[0] == name:
                    del monitor.cache[key]

    def restore(self, name):
        self.removed.discard(name)
        self.unresponsive.discard(name)
        for monitor in self.monitors:
            if monitor.added:
                monitor.added(name, monitor.get_device_instance(name))
        for path, value in self.services[name].values.items():
            self.publish(name, path, value)

    def list_names(self):
        return [name for name in self.services if name not in self.removed]

    def add_signal_receiver(self, *args, **kwargs):
        return None

    def get_object(self, name, path):
        return types.SimpleNamespace(
            SetValue=lambda value, **kwargs: self.write(name, path, value),
            GetValue=lambda **kwargs: self.read(name, path))

    def call_async(self, name, path, interface, method, signature, args,
                   reply_handler, error_handler, timeout=None, **kwargs):
        def deliver():
            try:
                result = self.call_blocking(name, path, interface, method, signature, args, timeout)
                self.later(self.latency.reply_s, lambda: reply_handler(result))
            except Exception as exc:
                self.later(self.latency.reply_s, lambda err=exc: error_handler(err))
        self.later(self.latency.request_s, deliver)

    def call_blocking(self, name, path, interface, method, signature, args,
                      timeout=None):
        if method == "GetValue":
            return self.read(name, path)
        if name == "com.victronenergy.settings":
            if method == "SetValue":
                self.settings[path] = args[0]
                # The store answers GetValue; the paths localsettings also
                # publishes as a service have to reach the monitors as well.
                service = self.services.get(name)
                if service is not None and path in service.values:
                    service[path] = args[0]
                return 0
            if method == "AddSetting":
                group, leaf, default = args[:3]
                self.settings.setdefault("/Settings/%s/%s" % (group.strip("/"), leaf), default)
                return 0
            if method == "RemoveSettings":
                return 0
        if method == "SetValue":
            return self.write(name, path, args[0])
        raise RuntimeError("unsupported bus method " + method)


class PlantService(stubs.FakeService):
    def __init__(self, name, bus=None, register=True):
        self.name, self.bus = name, bus
        self.values, self.cbs = {}, {}
        self.registered = False
        if register:
            self.register()

    def register(self):
        self.registered = True
        self.bus.register(self)

    def __setitem__(self, path, value):
        if path not in self.values:
            raise AssertionError("set of unknown path " + path)
        old = self.values[path]
        self.values[path] = value
        if old != value and self.registered:
            self.bus.publish(self.name, path, value)

    def accept(self, path, value):
        if path not in self.values or path not in self.cbs:
            return False
        if self.values[path] == value:
            return True
        callback = self.cbs[path]
        if callback is None or callback(path, value):
            self[path] = value
            return True
        return False

    def __del__(self):
        pass  # lifetime belongs to the simulation, not global FakeBus.names


class DelayedMonitor:
    def __init__(self, bus, tree, valueChangedCallback=None,
                 deviceAddedCallback=None, deviceRemovedCallback=None, **kwargs):
        self.bus, self.tree = bus, tree
        self.changed, self.added, self.removed = (
            valueChangedCallback, deviceAddedCallback, deviceRemovedCallback)
        self.cache = {(name, path): value for name, service in bus.services.items()
                      for path, value in service.values.items()}
        bus.monitors.append(self)

    def close(self):
        """Retire subscriptions, including deliveries already in the event queue."""
        if self in self.bus.monitors:
            self.bus.monitors.remove(self)

    def receive(self, name, path, value):
        if self not in self.bus.monitors:
            return
        if name in self.bus.removed or (name, path) in self.bus.invalid and value is not None:
            return
        key = (name, path)
        old = self.cache.get(key)
        self.cache[key] = value
        if old != value and self.changed:
            self.changed(name, path, {}, {"Value": value}, self.get_device_instance(name))

    def get_service_list(self, prefix=None):
        return {name: self.get_device_instance(name) for name in self.bus.list_names()
                if prefix is None or name == prefix or name.startswith(prefix + ".")}

    def get_device_instance(self, name):
        service = self.bus.services.get(name)
        return service.values.get("/DeviceInstance", 0) if service else None

    def get_value(self, name, path, default=None):
        if name in self.bus.removed or (name, path) in self.bus.invalid:
            return default
        return self.cache.get((name, path), default)

    def seen(self, name, path):
        return name in self.bus.services and name not in self.bus.removed and (name, path) in self.cache

    def set_value_async(self, name, path, value, reply_handler=None, error_handler=None):
        return self.bus.write(name, path, value, True, reply_handler, error_handler)

    def set_value(self, name, path, value):
        return self.bus.write(name, path, value)


class DvccAllocation:
    """Installed two-MPPT current distribution, sampled independently of policy.

    dvcc.py updates PV/VE.Bus filters every second and adjusts every three.
    The measured external-DC filter is evaluated only on adjustment ticks.
    No ESS, alternator, RS inverter or manufacturer quirk is assumed here.
    """
    def __init__(self, config):
        self.cfg = config
        self.pv_a = [0.0 for _ in config.array_current_limits_a]
        self.limits_a = list(config.array_current_limits_a)
        self.vebus_a = self.external_a = self.quattro_a = 0.0
        self.quattro_v = config.initial_base_v
        self.solar_v = config.initial_base_v + config.initial_offset_v
        self.next_sample_s = self.next_adjust_s = 0.0
        self.adjustments = []

    def update(self, now, voltage, pv_w, vebus_w, external_w, ccl_a,
               quattro_v=None, solar_v=None):
        c = self.cfg
        if now + 1e-9 < self.next_sample_s:
            return
        self.next_sample_s = now + c.dvcc_sample_s
        for n, power in enumerate(pv_w):
            self.pv_a[n] += (power / voltage - self.pv_a[n]) * min(1.0, 1 / c.pv_filter_s)
        # Installed Multi.update_values limits inverter compensation to the
        # aggregate solar capacity before filtering the VE.Bus current.
        vebus_a = max(vebus_w / voltage, -sum(c.array_current_limits_a))
        self.vebus_a += (vebus_a - self.vebus_a) * min(1.0, 1 / c.vebus_filter_s)
        if now + 1e-9 < self.next_adjust_s:
            return
        self.next_adjust_s = now + c.dvcc_adjust_s
        # Both charger voltage targets are sent below the same installed
        # ADJUST gate as current allocation, never on each publisher change.
        if quattro_v is not None:
            self.quattro_v = quattro_v
        if solar_v is not None:
            self.solar_v = solar_v
        total = max(0.0, ccl_a)
        if c.dvcc_compensation and c.dc_measurement == 'measured' and total > 0:
            self.external_a += (external_w / voltage - self.external_a) * min(1.0, 1 / c.pv_filter_s)
            total += self.external_a
            if c.dvcc_quantization:
                total = round(total, 1)
        solar = total
        if c.dvcc_compensation and c.dvcc_inverter_compensation and self.vebus_a < 0:
            solar -= self.vebus_a
            if c.dvcc_quantization:
                solar = math.ceil(solar)
        self._distribute(solar)
        remaining = total - sum(self.pv_a)
        self.quattro_a = max(0.0, round(remaining) if c.dvcc_quantization else remaining)
        self.adjustments.append({'time_s': now, 'solar_limits_a': list(self.limits_a),
                                 'quattro_limit_a': self.quattro_a,
                                 'quattro_voltage_v': self.quattro_v,
                                 'solar_voltage_v': self.solar_v})

    def _distribute(self, requested):
        capacities = self.cfg.array_current_limits_a
        capacity = sum(capacities)
        if requested > capacity * .95:
            self.limits_a = list(capacities)
            return
        actual = min(sum(self.pv_a), sum(self.limits_a))
        if capacity <= actual:
            return
        ratio = (requested - actual) / (capacity - actual)
        spillover = 0.0
        for n, maximum in enumerate(capacities):
            limit = max(0.0, self.pv_a[n] + ratio * (maximum - self.pv_a[n]) + spillover)
            rounded = round(limit, 1) if self.cfg.dvcc_quantization else limit
            spillover = limit - rounded
            self.limits_a[n] = min(maximum, rounded)


class QuattroResponse:
    """Provisional charge-needed/reconnect burst followed by finite taper.

    Every physical reconnection starts a new response even below its CVL. A
    pending startup begins when current is permitted. Command readbacks are
    separate from the internal voltage/current response and delivered power.
    Profiles describe an adversarial envelope, not a calibrated charger.
    """
    def __init__(self, config):
        self.cfg = config
        self.voltage_v = config.initial_base_v
        self.current_limit_a = self.power_w = 0.0
        self.started_s = None
        self.pending_start = True
        self.charge_needed = False
        self.events = []
        self.reason = 'startup'

    @staticmethod
    def respond(value, target, dt, response_s):
        return target if response_s <= 0 else target + (value - target) * math.exp(-dt / response_s)

    def reconnect(self):
        self.pending_start = True
        self.reason = 'reconnect'

    def advance(self, now, dt, connected, voltage_v, limit_a, terminal_v,
                voltage_power_w, hardware_cap_w):
        c = self.cfg
        self.voltage_v = self.respond(self.voltage_v, voltage_v, dt, c.quattro_voltage_response_s)
        self.current_limit_a = self.respond(self.current_limit_a, max(0.0, limit_a), dt,
                                            c.quattro_current_response_s)
        if not connected:
            self.power_w = 0.0
            self.charge_needed = False
            return 0.0
        needs_charge = self.voltage_v + c.quattro_voltage_bias_v > terminal_v + c.quattro_charge_needed_hysteresis_v
        if self.current_limit_a > .01 and (self.pending_start or (needs_charge and not self.charge_needed)):
            self.started_s = now
            self.events.append({'time_s': now, 'reason': self.reason if self.pending_start else 'charge needed'})
            self.pending_start = False
        # Hysteresis prevents minor voltage ripple from continually restarting
        # the burst. A reconnect always starts its own response.
        if needs_charge:
            self.charge_needed = True
        elif terminal_v > self.voltage_v + c.quattro_voltage_bias_v + c.quattro_charge_needed_hysteresis_v:
            self.charge_needed = False
        age = math.inf if self.started_s is None else now - self.started_s
        if age < c.reconnect_surge_s:
            span = max(.001, c.reconnect_surge_s - c.quattro_burst_hold_s)
            taper = max(0.0, 1 - max(0.0, age - c.quattro_burst_hold_s) / span)
            voltage_power_w = max(voltage_power_w, c.reconnect_surge_w * taper)
        cap = min(hardware_cap_w, self.current_limit_a * terminal_v)
        target = max(0.0, min(cap, voltage_power_w))
        self.power_w = ChargerPlant.ramp(self.power_w, target, c.quattro_ramp_w_s, dt)
        # Hardware/AC bounds are immediate; a new requested DVCC limit is not.
        self.power_w = min(self.power_w, hardware_cap_w)
        return self.power_w


class MpptResponse:
    """Separate physical MPPT output from the acknowledged DVCC command.

    Historical anchors are 90% after53s at0.15V headroom and only5% after126s
    at0.05V. A first-order response at each anchor and logarithmic interpolation
    between them are an explicit synthetic family, not measured firmware math.
    Below/above the anchors the rise time is clamped; actual voltage permission
    still independently constrains output. Downward response has no measured
    calibration and therefore remains a separate configurable sensitivity.
    """
    HIGH_HEADROOM_V, HIGH_T90_S = .15, 53.0
    LOW_HEADROOM_V, LOW_T05_S = .05, 126.0

    def __init__(self, config, index):
        if config.mppt_response_profile not in ('fast', 'historical_headroom'):
            raise ValueError('unknown MPPT response profile')
        self.config, self.index = config, index
        self.power_w = 0.0
        self.target_w = self.headroom_v = 0.0
        self.response_s = 0.0
        self.direction = 'stationary'
        if config.mppt_fall_response_s < 0 or config.mppt_response_scales[index] <= 0:
            raise ValueError('MPPT response times must be nonnegative and scales positive')

    @classmethod
    def rise_time_constant(cls, headroom_v):
        slow = cls.LOW_T05_S / -math.log(.95)
        fast = cls.HIGH_T90_S / math.log(10)
        fraction = max(0.0, min(1.0, (headroom_v - cls.LOW_HEADROOM_V) /
                               (cls.HIGH_HEADROOM_V - cls.LOW_HEADROOM_V)))
        return math.exp(math.log(slow) + fraction * (math.log(fast) - math.log(slow)))

    def advance(self, dt, target_w, sunlight_w, headroom_v, ramp_w_s):
        self.target_w = max(0.0, target_w)
        self.headroom_v = headroom_v
        self.direction = ('rising' if self.target_w > self.power_w + 1e-9 else
                          'falling' if self.target_w < self.power_w - 1e-9 else 'stationary')
        if self.config.mppt_response_profile == 'fast':
            self.response_s = 0.0
        elif self.direction == 'rising':
            self.response_s = self.rise_time_constant(headroom_v) * self.config.mppt_response_scales[self.index]
        else:
            self.response_s = self.config.mppt_fall_response_s * self.config.mppt_response_scales[self.index]
        response = QuattroResponse.respond(self.power_w, self.target_w, dt, self.response_s)
        self.power_w = max(0.0, min(sunlight_w,
            ChargerPlant.ramp(self.power_w, response, ramp_w_s, dt)))
        return self.power_w

    def snapshot(self):
        return {'power_w': self.power_w, 'target_w': self.target_w,
                'headroom_v': self.headroom_v, 'response_s': self.response_s,
                'direction': self.direction}


class ChargerPlant:
    def __init__(self, config, clock):
        self.cfg, self.clock = config, clock
        self.soc = config.initial_soc
        self.polarization_v = 0.0
        self.voltage = self.ocv()
        self.current = self.battery_w = 0.0
        self.ac_w, self.dc_w = 300.0, 60.0
        self.sun_w, self.pv_w = [500.0, 900.0], [0.0, 0.0]
        self.q_w = self.smoothed_pv_w = 0.0
        self.q_permission_a = 0.0
        self.base_v, self.offset_v = config.initial_base_v, config.initial_offset_v
        self.pv_voltage_v = self.solar_v
        self.ccl_a, self.connected, self.shore_available = 0.0, True, True
        # Shore is on one physical input, the alternate on the other: which is
        # which is the installation's fact, never the controller's assumption.
        if config.shore_input not in (1, 2):
            raise ValueError("shore_input must be 1 or 2")
        self.shore_input = int(config.shore_input)
        self.alternate_input = 3 - self.shore_input
        # A second AC input the Quattro may accept while shore is ignored or
        # absent: "not shore" is not proof of inverter-only operation (SP56).
        self.alternate_available = False
        self.on_alternate = False
        self.ignore = 0
        self.rec_cvl, self.rec_ccl, self.rec_dcl = 62.7, 200.0, 400.0
        self.energy, self.relay_edges, self.commands = Energy(), [], []
        self.policy_mode = ''
        self.elapsed_s = 0.0
        self.dvcc = DvccAllocation(config)
        self.quattro = QuattroResponse(config)
        self.mppts = [MpptResponse(config, n) for n in range(len(config.array_current_limits_a))]

    def ocv(self):
        points = self.cfg.ocv_points
        if self.soc <= points[0][0]:
            return points[0][1]
        for (s0, v0), (s1, v1) in zip(points, points[1:]):
            if self.soc <= s1:
                return v0 + (v1 - v0) * (self.soc - s0) / (s1 - s0)
        return points[-1][1]

    @property
    def solar_v(self):
        return self.base_v + self.offset_v

    def apply(self, kind, value):
        if kind == 'ignore':
            self.ignore = int(value)
            self.update_connection('command')
        else:
            setattr(self, kind, float(value))
            self.commands.append({'time_s': self.clock.elapsed, 'kind': kind,
                                  'base_v': self.base_v, 'offset_v': self.offset_v,
                                  'solar_v': self.solar_v, 'rec_cvl_v': self.rec_cvl})

    def update_connection(self, cause='source'):
        shore = not self.ignore and self.shore_available
        connected = bool(shore or self.alternate_available)
        # The alternate input carries the boat only while shore cannot.
        self.on_alternate = connected and not shore
        if connected != self.connected:
            self.connected = connected
            self.relay_edges.append({'time_s': self.clock.elapsed,
                                     'connected': connected, 'cause': cause})
            if connected:
                self.quattro.reconnect()

    @staticmethod
    def ramp(old, target, rate, dt):
        return old + max(-rate * dt, min(rate * dt, target - old))

    def advance(self, dt):
        # Standalone plant tests and coupled bus stepping share the same physical
        # integration resolution and independent DVCC timer boundaries.
        while dt > 1e-9:
            step = min(.25, dt)
            self._advance(step)
            self.elapsed_s += step
            dt -= step

    def _advance(self, dt):
        c, e = self.cfg, self.energy
        ocv = self.ocv() + self.polarization_v
        inverter_w = 0.0 if self.connected else self.ac_w / c.inverter_efficiency + c.inverter_idle_w
        demand = self.dc_w + inverter_w
        self.dvcc.update(self.elapsed_s, self.voltage, self.pv_w, self.q_w - inverter_w,
                         self.dc_w, self.ccl_a, self.base_v, self.solar_v)
        self.q_permission_a = self.dvcc.quattro_a
        self.smoothed_pv_w = sum(self.dvcc.pv_a) * self.voltage
        shore_capacity = max(0.0, c.shore_max_w - self.ac_w) * c.charger_efficiency
        hardware_cap = min(c.quattro_max_w, shore_capacity)
        q_voltage = self.quattro.voltage_v + c.quattro_voltage_bias_v
        q_voltage_w = q_voltage * (q_voltage - ocv) / c.resistance_ohm + demand - sum(self.pv_w)
        self.q_w = self.quattro.advance(self.elapsed_s, dt, self.connected, self.dvcc.quattro_v,
            self.q_permission_a, self.voltage, q_voltage_w, hardware_cap)
        self.pv_voltage_v = QuattroResponse.respond(self.pv_voltage_v, self.dvcc.solar_v, dt,
                                                    c.pv_voltage_response_s)
        # All sources share the terminal; each MPPT also has its own sense and
        # response history. A current reduction does not instantly settle output.
        previous_pv = list(self.pv_w)
        available_targets = [min(sun, maximum * self.voltage)
                             for sun, maximum in zip(self.sun_w, self.dvcc.limits_a)]
        potential = sum(available_targets)
        for n, potential_w in enumerate(available_targets):
            sense = self.voltage + c.mppt_voltage_sense_offsets_v[n]
            effective_target = (self.pv_voltage_v - c.mppt_voltage_sense_offsets_v[n]
                                - c.mppt_headroom_loss_v[n])
            total_voltage_limit = max(0.0, effective_target * (effective_target - ocv) /
                                      c.resistance_ohm + demand - self.q_w)
            share = potential_w / potential if potential else 0.0
            if c.mppt_response_profile == 'fast':
                # Preserve the archived arithmetic fixture as an explicit mode.
                target = min(potential, total_voltage_limit) * share
            else:
                # The other source's actual contribution occupies the same bus.
                other_power = sum(previous_pv) - previous_pv[n]
                target = min(potential_w, max(0.0, total_voltage_limit - other_power))
            headroom = self.pv_voltage_v - sense - c.mppt_headroom_loss_v[n]
            self.pv_w[n] = self.mppts[n].advance(dt, target, self.sun_w[n], headroom,
                                               c.pv_ramp_w_s * max(share, .1))
        pv = sum(self.pv_w)
        self.battery_w = pv + self.q_w - demand
        discriminant = max(0.0, ocv * ocv + 4 * c.resistance_ohm * self.battery_w)
        self.current = 2 * self.battery_w / (ocv + math.sqrt(discriminant))
        self.voltage = ocv + self.current * c.resistance_ohm
        self.polarization_v = QuattroResponse.respond(self.polarization_v,
            self.current * c.polarization_ohm, dt, c.polarization_response_s)
        dh = dt / 3600.0
        self.soc = max(0.0, min(100.0, self.soc + self.current * dh / c.capacity_ah * 100))
        shore = (self.ac_w + self.q_w / c.charger_efficiency) if self.connected else 0.0
        conversion = self.q_w * (1 / c.charger_efficiency - 1) + inverter_w - (0 if self.connected else self.ac_w)
        e.shore_wh += shore * dh
        e.shore_charge_wh += self.q_w / c.charger_efficiency * dh
        e.pv_wh += pv * dh
        e.pv_available_wh += sum(self.sun_w) * dh
        e.pv_curtailed_wh += max(0.0, sum(self.sun_w) - pv) * dh
        if self.quattro.started_s is not None and self.elapsed_s - self.quattro.started_s < c.reconnect_surge_s:
            e.quattro_burst_wh += self.q_w * dh
        if sum(self.sun_w) > 0 and self.dvcc.solar_v < self.voltage:
            e.solar_below_bank_s += dt
        e.ac_load_wh += self.ac_w * dh
        e.dc_load_wh += self.dc_w * dh
        e.conversion_wh += conversion * dh
        e.charge_wh += max(0.0, self.battery_w) * dh
        e.discharge_wh += max(0.0, -self.battery_w) * dh
        e.charge_ah += max(0.0, self.current) * dh
        e.discharge_ah += max(0.0, -self.current) * dh
        if self.policy_mode == 'CHARGE':
            e.reverse_wh += max(0.0, -self.battery_w) * dh
        elif self.policy_mode == 'DISCHARGE':
            e.reverse_wh += max(0.0, self.battery_w) * dh
        error = shore + pv - self.ac_w - self.dc_w - conversion - self.battery_w
        e.max_balance_error_w = max(e.max_balance_error_w, abs(error))


class CoupledSimulation:
    """Owns driver processes, bus, time and plant; one live simulation per process.

    Pass module paths from an isolated baseline checkout to replay old behavior.
    It is intentionally impossible to identify a simulation pass as boat data.
    """
    def __init__(self, plant_config=None, latency=None, target=80, enabled=True,
                 rec_path=None, solar_path=None, configure_rec=None, configure_solar=None):
        self.clock, self.latency = Clock(), latency or Latency()
        self.plant = ChargerPlant(plant_config or PlantConfig(), self.clock)
        stubs.FakeBus.store, stubs.FakeBus.names = {}, {}
        stubs.GLib.timers = []
        self.bus = DelayedBus(self.clock, self.latency)
        self.tempdir = tempfile.TemporaryDirectory(prefix="solar-priority-plant-")
        self._patches = ExitStack()
        self.bus_factory_calls = []
        self.rec_running = self.solar_running = True
        self.missing_can = set()
        self.trace = []
        self._next_tick = 1.0
        types = self.plant.cfg.gx_input_types
        if types is None:
            types = tuple(3 if n == self.plant.shore_input else 0 for n in (1, 2))
        self.bus.settings.update({"/Settings/RecBms/ChargeSlider": target,
            "/Settings/RecBms/EqLastCompleted": self.clock.time(),
            "/Settings/SolarPriority/Enabled": int(enabled),
            GX_INPUT % 1: int(types[0]), GX_INPUT % 2: int(types[1]),
            "/Settings/System/AccessLevel": 3, "/Settings/SystemSetup/BmsInstance": 200})
        self._make_sources()
        try:
            self._start_drivers(rec_path, solar_path, configure_rec, configure_solar)
        except BaseException:
            self.close()
            raise

    def _start_drivers(self, rec_path, solar_path, configure_rec, configure_solar):
        # Patch dependency factories rather than inventing driver helpers. Missing
        # startup functions/imports must fail exactly as they would on the boat.
        for name in ('SystemBus', 'SessionBus'):
            def connect(private=False, factory=name):
                self.bus_factory_calls.append({'factory': factory, 'private': bool(private)})
                return self.bus
            self._patches.enter_context(patch.object(stubs.dbus, name, connect))
        # These are public dbus-python types absent from the minimal shared stub.
        for name, value in (('Double', float), ('Int32', int)):
            self._patches.enter_context(patch.object(stubs.dbus, name, value, create=True))
        previous_monitor = sys.modules.get('dbusmonitor')
        monitor_module = types.ModuleType("dbusmonitor")
        monitor_module.DbusMonitor = lambda tree, **kw: DelayedMonitor(self.bus, tree, **kw)
        sys.modules["dbusmonitor"] = monitor_module
        def restore_monitor():
            if sys.modules.get('dbusmonitor') is monitor_module:
                if previous_monitor is None:
                    sys.modules.pop('dbusmonitor', None)
                else:
                    sys.modules['dbusmonitor'] = previous_monitor
        self._patches.callback(restore_monitor)
        driver_dir = ROOT / "dbus-recbms"
        paths = (Path(rec_path or driver_dir / "dbus_recbms.py"),
                 Path(solar_path or driver_dir / "solar_priority.py"))
        # Each driver gets its checkout's complete helper graph, even when a
        # previous simulation (or another test) has imported the same names.
        helper_names = {path.stem for directory in {driver_dir, *(p.parent for p in paths)}
                        for path in directory.glob("*.py")}
        self.rec_module, rec_helpers = self._load_driver(paths[0], 'rec', helper_names)
        self.solar_module, solar_helpers = self._load_driver(paths[1], 'solar', helper_names)
        for module in (self.rec_module, self.solar_module):
            replacements = {
                'time': self.clock, 'VeDbusService': PlantService,
                'atexit': types.SimpleNamespace(register=lambda *a: None),
                'signal': types.SimpleNamespace(SIGTERM=15, SIGINT=2, signal=lambda *a: None),
            }
            for name, value in replacements.items():
                self._patches.enter_context(patch.object(module, name, value))
        # Imported adapter modules own their clocks too; replacing only the driver
        # module would silently make 600 simulated seconds look like milliseconds.
        process_ids = itertools.count(1)
        for helpers in (rec_helpers, solar_helpers):
            adapter_module = helpers.get("rec_policy_adapter")
            if adapter_module is not None:
                self._patches.enter_context(patch.object(adapter_module, "time", self.clock))
            contract_module = helpers.get("policy_contract")
            if contract_module is not None:
                self._patches.enter_context(patch.object(contract_module, 'uuid',
                    types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(
                        hex="offline-generation-%08d" % next(process_ids)))))
        self.bus.on_publication = self._actuator_publication
        rec_config_dir = Path(rec_path).parent if rec_path else driver_dir
        solar_config_dir = Path(solar_path).parent if solar_path else driver_dir
        self.rec_cfg = self.rec_module.Config(str(rec_config_dir / "config.ini"))
        # Persistent controller state must never escape the test directory.
        for attr in ("policy_state_path", "state_path", "ledger_path"):
            setattr(self.rec_cfg, attr, str(Path(self.tempdir.name) / (attr + ".json")))
        if configure_rec:
            configure_rec(self.rec_cfg)
        self.rec = self.rec_module.RecBmsDriver(self.rec_cfg)
        self.battery_name = self.rec.batt.name
        self.select_rec_battery()
        self._feed_can()
        self.rec._poll_effective_cv()
        self.rec._tick()
        self.solar_cfg = self.solar_module.Config(str(solar_config_dir / "solar_priority.ini"))
        if configure_solar:
            configure_solar(self.solar_cfg)
        before_solar = len(self.bus_factory_calls)
        self.solar = self.solar_module.SolarPriorityDriver(self.solar_cfg)
        self.solar_bus_calls = self.bus_factory_calls[before_solar:]
        self.publish_measurements()
        self.bus.drain()

    def _load_driver(self, path, role, helper_names):
        saved_modules = {name: sys.modules[name] for name in helper_names if name in sys.modules}
        saved_path = sys.path[:]
        try:
            for name in helper_names:
                sys.modules.pop(name, None)
            sys.path.insert(0, str(path.resolve().parent))
            module = stubs.load(str(path), "coupled_%s_%s" % (role, id(self)))
            helpers = {name: sys.modules[name] for name in helper_names if name in sys.modules}
            return module, helpers
        finally:
            for name in helper_names:
                sys.modules.pop(name, None)
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path

    def _source(self, name, instance, values, writable=()):
        service = PlantService(name, self.bus)
        service.add_path("/DeviceInstance", instance)
        for path, value in values.items():
            service.add_path(path, value, writeable=path in writable,
                             onchangecallback=lambda path, value: True)
        return service

    def _make_sources(self):
        self.system = self._source(SYSTEM, 0, {OFFSET: self.plant.offset_v,
            "/Control/EffectiveChargeVoltage": self.plant.solar_v,
            "/Dc/Pv/Current": 0.0, "/Dc/Pv/Power": 0.0,
            "/Ac/Consumption/L1/Power": self.plant.ac_w,
            "/Dc/Battery/Soc": self.plant.soc, "/Dc/Battery/Power": 0.0,
            "/Dc/Battery/Voltage": self.plant.voltage, "/Dc/System/Power": self.plant.dc_w,
            "/Dc/System/MeasurementType": 1 if self.plant.cfg.dc_measurement == "measured" else 0,
            "/ActiveBatteryService": None, "/Dc/Battery/BatteryService": None,
            "/ActiveBmsService": None, "/ActiveBmsInstance": None,
            "/Control/Dvcc": 1}, writable=(OFFSET,))
        # /Ac/In/1/Connected does NOT exist on this Quattro's firmware; the
        # availability and acknowledged-ignore state do (read 2026-09-14 on
        # vebus 276: AcIn1Available 1, AcIn2Available 0, IgnoreAcIn1 0,
        # IgnoreAcIn2 0, ActiveIn/Connected 1, ActiveInput 0).
        shore, alternate = self.plant.shore_input, self.plant.alternate_input
        self.vebus = self._source(VEBUS, 276, {CONTROL % 1: 0, "/Connected": 1,
            CONTROL % 2: 0, "/Ac/ActiveIn/ActiveInput": shore - 1,
            "/Ac/ActiveIn/Connected": 1, "/Ac/NumberOfAcInputs": 2,
            AVAILABLE % shore: 1, AVAILABLE % alternate: 0,
            STATE % 1: 0, STATE % 2: 0,
            "/Ac/Out/L1/P": self.plant.ac_w, "/Ac/In/1/CurrentLimit": 32.0,
            "/Dc/0/Voltage": self.plant.voltage, "/Dc/0/Current": 0.0,
            "/Dc/0/Power": 0.0, "/BatteryOperationalLimits/MaxChargeCurrent": 0.0,
            "/BatteryOperationalLimits/MaxChargeVoltage": self.plant.base_v},
            writable=(CONTROL % 1, CONTROL % 2))
        # localsettings is a service like any other here: the GX's AC input
        # types are read over the bus AND watched on a DbusMonitor tree, so
        # the same two values the store answers GetValue with are published.
        self.gx_settings = self._source(SETTINGS, 0,
            {GX_INPUT % n: self.bus.settings[GX_INPUT % n] for n in (1, 2)})
        self.arrays = [self._source(name, instance, {"/Pv/V": 70.0,
            "/Yield/Power": 0.0, "/MppOperationMode": 2, "/Dc/0/Current": 0.0,
            "/Dc/0/Voltage": self.plant.voltage, "/Link/ChargeVoltage": self.plant.solar_v,
            "/Link/ChargeCurrent": 0.0, "/Connected": 1,
            "/Settings/ChargeCurrentLimit": self.plant.cfg.array_current_limits_a[n],
            "/Link/VoltageSense": self.plant.voltage + self.plant.cfg.mppt_voltage_sense_offsets_v[n],
            "/Link/VoltageSenseActive": 1, "/Link/NetworkMode": 13, "/Link/NetworkStatus": 0,
            "/State": 252, "/ErrorCode": 0})
            for n, (name, instance) in enumerate(zip(ARRAYS, (278, 279)))]

    def select_rec_battery(self):
        """Publish the installed systemcalc instance selector and concrete identity."""
        instance = int(self.rec.batt['/DeviceInstance'])
        for path, value in {
                '/ActiveBatteryService': 'com.victronenergy.battery/%s' % instance,
                '/Dc/Battery/BatteryService': self.rec.batt.name,
                '/ActiveBmsService': self.rec.batt.name,
                '/ActiveBmsInstance': instance}.items():
            self.system[path] = value
        self.bus.settings['/Settings/SystemSetup/BmsInstance'] = instance

    def _actuator_publication(self, name, path, value):
        if value is None:
            return
        mapping = {BASE: ("base_v", self.latency.base_s), CCL: ("ccl_a", self.latency.current_s)}
        if name.startswith("com.victronenergy.battery.") and path in mapping:
            kind, delay = mapping[path]
        elif name == SYSTEM and path == OFFSET:
            kind, delay = "offset_v", self.latency.offset_s
        elif name == VEBUS and path == CONTROL % self.plant.shore_input:
            kind, delay = "ignore", self.latency.relay_s
        elif name == VEBUS and path in (CONTROL % 1, CONTROL % 2):
            # A command written to the input shore is NOT on cannot move the
            # transfer switch: it would only strand an ignore on an input
            # nobody is watching. Recorded so a test can prove it never
            # happened.
            self.plant.commands.append({'time_s': self.clock.elapsed,
                'kind': 'ignore_wrong_input', 'path': path, 'value': value})
            return
        else:
            return
        self.bus.later(delay, lambda: self.plant.apply(kind, value))

    def _feed_can(self):
        p = self.plant
        frames = {
            0x351: struct.pack("<HHHH", round(p.rec_cvl * 10), round(p.rec_ccl * 10), round(p.rec_dcl * 10), 480),
            0x355: struct.pack("<HHH", int(p.soc), 100, round(p.soc * 100)),
            0x356: struct.pack("<hhhH", round(p.voltage * 100), round(p.current * 10), 210, 0),
            0x373: struct.pack("<HHhh", round(p.voltage / 15 * 1000) - 2,
                                round(p.voltage / 15 * 1000) + 2, 293, 295),
            0x372: struct.pack("<HHHH", 6, 0, 0, 0),
            0x379: struct.pack("<H", round(p.cfg.capacity_ah)),
            0x35E: b"REC-BMS", 0x404: b"\x01\x00\x00",
        }
        for canid, payload in frames.items():
            if canid not in self.missing_can:
                self.rec_module.decode_frame(self.rec.bms, canid, payload)

    def publish_measurements(self):
        p = self.plant
        # Availability changes reach the Quattro on its own, not only when a
        # relay command is written.
        p.update_connection('source')
        for path, value in {"/Control/EffectiveChargeVoltage": p.dvcc.solar_v,
            "/Dc/Pv/Current": sum(p.pv_w) / p.voltage, "/Dc/Pv/Power": sum(p.pv_w),
            "/Ac/Consumption/L1/Power": p.ac_w, "/Dc/Battery/Soc": p.soc,
            "/Dc/Battery/Power": p.battery_w, "/Dc/Battery/Voltage": p.voltage,
            "/Dc/System/Power": p.dc_w}.items():
            self.system[path] = value
        inverter_w = 0 if p.connected else p.ac_w / p.cfg.inverter_efficiency + p.cfg.inverter_idle_w
        net_dc_w = p.q_w - inverter_w
        overhead = p.cfg.vebus_reported_overhead_w
        # The reported field disagrees with V*I the way the real Quattro does:
        # on shore it retains a positive residual (AC-in minus AC-out); while
        # inverting, conversion/idle losses draw extra DC beyond the net figure.
        reported_dc_w = net_dc_w + overhead if p.connected else net_dc_w - overhead
        shore, alternate = p.shore_input, p.alternate_input
        for path, value in {
            "/Ac/ActiveIn/ActiveInput": ((alternate if p.on_alternate else shore) - 1)
                                        if p.connected else 240,
            "/Ac/ActiveIn/Connected": int(p.connected),
            AVAILABLE % shore: int(p.shore_available),
            AVAILABLE % alternate: int(p.alternate_available),
            # The acknowledged ignore state, which this fixture applies only
            # after relay_s -- never the value just written. Only the shore
            # input is ever held: nothing ignores the alternate.
            STATE % shore: int(p.ignore), STATE % alternate: 0,
            "/Ac/Out/L1/P": p.ac_w, "/Dc/0/Voltage": p.voltage,
            "/Dc/0/Power": reported_dc_w,
            "/Dc/0/Current": net_dc_w / p.voltage,
            "/BatteryOperationalLimits/MaxChargeVoltage": p.dvcc.quattro_v,
            "/BatteryOperationalLimits/MaxChargeCurrent": p.q_permission_a}.items():
            self.vebus[path] = value
        for n, service in enumerate(self.arrays):
            limited = p.pv_w[n] < p.sun_w[n] - 5
            # Venus solar-charger schema:0 off,1 voltage/current limited,2
            # maximum available input. Darkness is observed off, while a slow
            # positive-input response remains limited; zero watts alone is not off.
            # https://github.com/victronenergy/venus/wiki/dbus#solar-chargers
            tracking = 0 if p.sun_w[n] <= 0 else 1 if limited else 2
            for path, value in {"/Yield/Power": p.pv_w[n], "/Pv/V": 70 if p.sun_w[n] else 10,
                "/MppOperationMode": tracking, "/Dc/0/Current": p.pv_w[n] / p.voltage,
                "/Dc/0/Voltage": p.voltage, "/Link/ChargeVoltage": p.dvcc.solar_v,
                "/Link/ChargeCurrent": p.dvcc.limits_a[n],
                "/Link/VoltageSense": p.voltage + p.cfg.mppt_voltage_sense_offsets_v[n],
                "/Link/VoltageSenseActive": 1}.items():
                service[path] = value

    def step(self, dt=1.0):
        end = self.clock.elapsed + dt
        while self.clock.elapsed < end - 1e-9:
            next_event = self.bus.events[0][0] if self.bus.events else end
            boundary = min(end, self._next_tick, max(self.clock.elapsed, next_event), self.clock.elapsed + .25)
            duration = boundary - self.clock.elapsed
            if duration > 1e-9:
                self.plant.advance(duration)
                self.clock.elapsed = boundary
            self.bus.drain()
            if self.clock.elapsed >= self._next_tick - 1e-9:
                self.publish_measurements()
                if self.rec_running:
                    self._feed_can()
                    self.rec._poll_effective_cv()
                    self.rec._poll_ext_voltage()
                    self.rec._tick()
                if self.solar_running:
                    self.solar._tick()
                self._record()
                self._next_tick += 1.0
        return self

    def run(self, seconds):
        return self.step(float(seconds))

    def _record(self):
        status_raw = self.rec.batt.values.get("/RecBms/Policy/Status")
        try:
            status = json.loads(status_raw) if status_raw else {}
        except (ValueError, TypeError):
            status = {}
        legacy_mode = self.solar.sw.values.get("/SolarPriority/OneWay") or ""
        request = getattr(self.solar, "last_request", None) or {}
        self.plant.policy_mode = status.get("mode", legacy_mode).upper()
        control = getattr(getattr(self.rec, 'policy_adapter', None), 'control', {})
        actuator = control.get('actuator', {})
        self.trace.append({"time_s": self.clock.elapsed, "soc": self.plant.soc,
            "battery_w": self.plant.battery_w, "battery_a": self.plant.current,
            "dc_load_w": self.plant.dc_w, "ac_load_w": self.plant.ac_w,
            "voltage_v": self.plant.voltage, "pv_w": sum(self.plant.pv_w),
            "sun_available_w": sum(self.plant.sun_w),
            "battery_net_wh": self.plant.energy.charge_wh - self.plant.energy.discharge_wh,
            "battery_charge_wh": self.plant.energy.charge_wh,
            "battery_discharge_wh": self.plant.energy.discharge_wh,
            "policy_intent": request.get("transfer_intent"),
            "policy_purpose": request.get("requested_limits", {}).get("purpose"),
            "quattro_w": self.plant.q_w, "connected": self.plant.connected,
            "mppt_response": [mppt.snapshot() for mppt in self.plant.mppts],
            "regulation_authority": actuator.get('regulation_authority'),
            "regulation_reason": actuator.get('regulation_reason'),
            "regulation_limits": {key: control.get(key) for key in ('ccl_a', 'quattro_v', 'solar_v')},
            "mppt_evidence_state": actuator.get('mppt_response_state'),
            "policy_protect": control.get('protect'),
            "base_v": self.plant.dvcc.quattro_v, "solar_v": self.plant.dvcc.solar_v,
            "requested_base_v": self.plant.base_v, "requested_solar_v": self.plant.solar_v,
            "ccl_a": self.plant.ccl_a, "offset_v": self.plant.offset_v,
            "quattro_current_limit_a": self.plant.q_permission_a,
            "quattro_applied_current_limit_a": self.plant.quattro.current_limit_a,
            "quattro_internal_voltage_v": self.plant.quattro.voltage_v,
            "quattro_response_age_s": (None if self.plant.quattro.started_s is None else
                self.plant.elapsed_s - self.plant.quattro.started_s),
            "prepared_limits": (dict(self.rec.policy_adapter.prepared_limits)
                if getattr(getattr(self.rec, 'policy_adapter', None), 'prepared_limits', None) else None),
            "prepared": status.get("transfer", {}).get("prepared"),
            "prepare_age_s": status.get("transfer", {}).get("prepare_age_s"),
            "actuator_state": actuator.get('state'),
            "state": self.solar.sw.values.get("/SolarPriority/State"), "policy": status})

    def set_load(self, ac_w=None, dc_w=None):
        if ac_w is not None:
            self.plant.ac_w = float(ac_w)
        if dc_w is not None:
            self.plant.dc_w = float(dc_w)

    def set_sun(self, array_w):
        self.plant.sun_w = list(map(float, array_w))

    def set_target(self, target):
        self.rec.settings["chargeslider"] = target

    def set_enabled(self, enabled):
        self.solar._set_enabled(bool(enabled), persist=True)

    def stop_rec(self, stalled=False):
        """Kill or stall the whole publisher, including its D-Bus callbacks."""
        self.rec_running = False
        names = [self.rec.batt.name]
        if hasattr(self.rec, 'sw'):
            names.append(self.rec.sw.name)
        for name in names:
            if stalled:
                self.bus.unresponsive.add(name)
            else:
                self.bus.remove(name)

    def stop_solar(self, stalled=False):
        self.solar_running = False
        self.solar.monitor.close()
        if stalled:
            self.bus.unresponsive.add(self.solar.sw.name)
        else:
            self.bus.remove(self.solar.sw.name)

    def restart_rec(self, planned=True):
        """Restart the actual publisher, preserving only durable controller state.

        A known-history offline run acquires a temporary persistence file before
        a planned restart. A crash never performs that final checkpoint.
        """
        old = self.rec
        if planned and hasattr(old, 'policy_adapter'):
            if old.policy_adapter.ledger.path is None:
                self.rec_cfg.policy_state_path = str(Path(self.tempdir.name) / 'restart-state.json')
                old.policy_adapter.ledger.path = self.rec_cfg.policy_state_path
            old.policy_adapter.shutdown()
        names = [old.batt.name]
        if hasattr(old, 'sw'):
            names.append(old.sw.name)
        for name in names:
            self.bus.remove(name)
        old._close_can()
        self.rec = self.rec_module.RecBmsDriver(self.rec_cfg)
        self.battery_name = self.rec.batt.name
        for name in names:
            self.bus.restore(name)
        self.rec_running = True
        return self.rec

    def restart_solar(self, planned=True):
        old = self.solar
        if planned:
            old._shutdown()
        self.bus.remove(old.sw.name)
        old.monitor.close()
        self.solar = self.solar_module.SolarPriorityDriver(self.solar_cfg)
        self.bus.restore(self.solar.sw.name)
        self.solar_running = True
        return self.solar

    def summary(self):
        raw_snapshot = self.rec.batt.values.get("/RecBms/Policy/Snapshot")
        try:
            snapshot = json.loads(raw_snapshot) if raw_snapshot else {}
        except (ValueError, TypeError):
            snapshot = {}
        ledger = snapshot.get("ledger", {})
        result = asdict(self.plant.energy)
        result.update(evidence="offline deterministic fixture", elapsed_s=self.clock.elapsed,
            model_parameters=asdict(self.plant.cfg), latency_s=asdict(self.latency),
            final_soc=self.plant.soc, final_voltage_v=self.plant.voltage,
            observed_soc_min=min((point['soc'] for point in self.trace), default=self.plant.soc),
            observed_soc_max=max((point['soc'] for point in self.trace), default=self.plant.soc),
            final_base_v=self.plant.dvcc.quattro_v, final_solar_v=self.plant.dvcc.solar_v,
            requested_base_v=self.plant.base_v, requested_solar_v=self.plant.solar_v,
            actual_relay_edges=list(self.plant.relay_edges),
            charger_response_events=list(self.plant.quattro.events),
            mppt_response=[mppt.snapshot() for mppt in self.plant.mppts],
            all_use_efc=(self.plant.energy.charge_ah + self.plant.energy.discharge_ah)
                        / (2 * self.plant.cfg.capacity_ah),
            policy_status=self.rec.batt.values.get("/RecBms/Policy/Status"),
            overhead_efc=ledger.get("overhead", {}).get("efc"),
            held_references=ledger.get("references"),
            controller_snapshot=snapshot)
        return result

    def close(self):
        rec = getattr(self, 'rec', None)
        if rec is not None:
            rec._close_can()
        self.bus.events.clear()
        stubs.GLib.timers.clear()
        stubs.FakeBus.names = {}
        # Remove loaded driver modules and restore dependencies even when a
        # constructor failed before both driver instances existed.
        for prefix in ('rec', 'solar'):
            sys.modules.pop('coupled_%s_%s' % (prefix, id(self)), None)
        self._patches.close()
        self.tempdir.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
