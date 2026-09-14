"""Source validity and topology-aware DC demand, shared by control adapters.

A value-change signal and a successful read are both observations. Neither a
cached monitor lookup nor traffic from a different source renews validity.
"""
import math


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_bus_snapshot(values):
    """Canonicalize BusItem root GetValue and GetItems representations.

    velib root GetValue uses relative keys and empty arrays for invalid values;
    GetItems uses absolute keys with Value/Text wrappers. Keep this boundary
    shared so a bus encoding difference cannot leave one owner using old data.
    """
    result = {}
    if not isinstance(values, dict):
        return result
    for path, value in values.items():
        path = '/' + str(path).lstrip('/')
        if isinstance(value, dict):
            value = value.get('Value')
        if isinstance(value, (list, tuple)) and not value:
            value = None
        result[path] = value
    return result


def selected_battery_matches(service, instance, active_battery_service, active_bms_service,
                             battery_service=None, active_bms_instance=None):
    """Match fresh systemcalc selection evidence to one concrete battery/BMS.

    ActiveBatteryService may be a bus name or a stable service-type/instance
    key. The latter needs the concrete battery service and BMS instance as
    corroboration. Callers supply fresh observations; absent values stay None.
    """
    battery_type = 'com.victronenergy.battery'
    if (not isinstance(service, str) or not service.startswith(battery_type + '.') or
            isinstance(instance, bool) or not isinstance(instance, (int, float)) or
            not math.isfinite(instance) or instance < 0 or int(instance) != instance):
        return False
    if active_bms_service != service:
        return False
    if battery_service is not None and battery_service != service:
        return False
    if active_bms_instance is not None and (
            isinstance(active_bms_instance, bool) or
            not isinstance(active_bms_instance, (int, float)) or active_bms_instance != instance):
        return False
    if active_battery_service == service:
        return True
    return (active_battery_service == '%s/%d' % (battery_type, instance) and
            battery_service == service and active_bms_instance == instance)


class SourceRegistry:
    def __init__(self, max_age_s=20.0):
        self.max_age_s = max_age_s
        self.samples = {}
        self.epoch = 0
        self.service_epochs = {}

    def observe(self, service, path, value, now, validator=None):
        key = (service, path)
        previous = self.samples.get(key)
        valid = value is not None and (validator is None or validator(value))
        changed = previous is None or previous['value'] != value
        self.samples[key] = {
            'value': value if valid else None, 'valid': valid,
            'verified': now, 'changed': now if changed else previous['changed'],
            'epoch': self.epoch,
        }

    def generation(self, service):
        return self.service_epochs.get(service, 0)

    def remove(self, service):
        self.epoch += 1
        self.service_epochs[service] = self.generation(service) + 1
        for (name, _), sample in self.samples.items():
            if name == service:
                sample['valid'] = False
                sample['value'] = None

    def get(self, service, path, now):
        sample = self.samples.get((service, path))
        if sample is None or not sample['valid']:
            return None
        age = now - sample['verified']
        return sample['value'] if 0 <= age <= self.max_age_s else None


class TimedMean:
    """Piecewise-constant, elapsed-time-weighted mean including quiet loads."""
    def __init__(self, duration_s):
        self.duration_s = duration_s
        self.points = []

    def update(self, now, value):
        if self.points and now < self.points[-1][0]:
            self.points.clear()
        if self.points and now == self.points[-1][0]:
            self.points[-1] = (now, value)
        else:
            self.points.append((now, value))
        cutoff = now - self.duration_s
        while len(self.points) > 1 and self.points[1][0] <= cutoff:
            self.points.pop(0)
        start = max(cutoff, self.points[0][0])
        if now <= start:
            return value
        total = 0.0
        for i, (ts, sample) in enumerate(self.points[:-1]):
            end = self.points[i + 1][0]
            total += sample * max(0.0, end - max(start, ts))
        return total / (now - start)


class LoadServiceEvidence:
    """Prove average delivered demand while tolerating bounded actuator pulses.

    A complete admission window is required. After proof, a shorter sustained
    deficit revokes it; missing observations never bridge either window.
    Physical protection and raw Wh limits remain the REC controller's job.
    """
    def __init__(self, duration_s=60.0, response_s=10.0, tolerance_w=20.0, gap_s=10.0):
        self.duration_s, self.response_s = duration_s, response_s
        self.tolerance_w, self.gap_s = tolerance_w, gap_s
        self.reset()

    def reset(self):
        self.mean = TimedMean(self.duration_s)
        self.short_mean = TimedMean(self.response_s)
        self.since = self.last = self.lost_since = None
        self.verified = False

    def update(self, now, power_w, eligible=True, response_in_progress=False):
        if (not eligible or finite(power_w) is None or
                (self.last is not None and not 0 <= now - self.last <= self.gap_s)):
            self.reset()
            return False
        self.last = now
        self.since = now if self.since is None else self.since
        mean = self.mean.update(now, power_w)
        short = self.short_mean.update(now, power_w)
        if self.verified and short < -self.tolerance_w:
            self.lost_since = now if self.lost_since is None else self.lost_since
            if now - self.lost_since >= self.response_s and not response_in_progress:
                self.reset()
        else:
            self.lost_since = None
            if now - self.since >= self.duration_s and mean >= -self.tolerance_w:
                self.verified = True
        return self.verified


class DemandModel:
    def __init__(self, efficiency=0.90, idle_w=30.0, uncertainty_w=30.0,
                 max_external_w=5000.0):
        if not 0 < efficiency <= 1 or idle_w < 0 or uncertainty_w < 0:
            raise ValueError('invalid inverter demand parameters')
        self.efficiency = efficiency
        self.idle_w = idle_w
        self.uncertainty_w = uncertainty_w
        self.max_external_w = max_external_w

    def estimate(self, ac_w, external_dc_w, measured_dc=False,
                 connected=True, inverter_dc_w=None):
        ac_w, external_dc_w = finite(ac_w), finite(external_dc_w)
        if ac_w is None or ac_w < 0:
            return {'valid': False, 'limited_by': 'AC demand unavailable'}
        if external_dc_w is None or external_dc_w > self.max_external_w:
            return {'valid': False, 'limited_by': 'DC demand unavailable or implausible'}
        # systemcalc's DC-system estimate is battery minus PV minus VE.Bus;
        # on a sunny bus it dips a few watts under zero between samples.
        # That is metering skew, not a demand to refuse: on the boat
        # (2026-09-14 16:06 UTC) every such dip made the REC's permission
        # flicker and started a return from a healthy island. Clamp it.
        external_dc_w = max(0.0, external_dc_w)
        direct = finite(inverter_dc_w)
        # Direct VE.Bus DC demand already includes conversion and idle loss.
        if not connected and direct is not None and direct >= 0:
            inverter = direct
            method = 'measured inverter DC'
        else:
            inverter = ac_w / self.efficiency + self.idle_w
            method = 'AC conversion model'
        return {
            'valid': True, 'island_w': inverter + external_dc_w,
            'admission_w': inverter + external_dc_w + self.uncertainty_w,
            'connected_dc_w': external_dc_w, 'inverter_dc_w': inverter,
            'external_dc_w': external_dc_w, 'measured_dc': bool(measured_dc),
            'uncertainty_w': self.uncertainty_w, 'method': method,
            'limited_by': '',
        }

    @staticmethod
    def allocate(demand, voltage_v, pv_w, battery_power_w, rec_ccl_a,
                 reserve_w=20.0, ramp_a=0.5, complete=False):
        """Map a battery-bus budget to DVCC's combined current allowance.

        DVCC adds *measured* DC demand itself. Estimated demand must be in
        the published allowance. PV uncertainty never releases the REC cap.
        The bounded ramp reserve also bounds incidental Quattro allowance;
        delivered battery power must still close the regulation loop.
        """
        voltage_v, rec_ccl_a = finite(voltage_v), finite(rec_ccl_a)
        if voltage_v is None or voltage_v <= 0 or rec_ccl_a is None:
            return {'ccl_a': 0.0, 'support_w': 0.0, 'limited_by': 'BMS limits unavailable'}
        if complete:
            return {'ccl_a': max(0.0, rec_ccl_a), 'support_w': None, 'limited_by': ''}
        if not demand.get('valid'):
            return {'ccl_a': 0.0, 'support_w': 0.0,
                    'limited_by': demand.get('limited_by', 'demand unavailable')}
        pv = finite(pv_w)
        # A missing array is not a permission to invent solar headroom.
        known_pv = max(0.0, pv or 0.0)
        external = demand['external_dc_w']
        support = max(0.0, external + reserve_w - known_pv)
        allowance_w = known_pv + support
        if demand['measured_dc']:
            allowance_w = max(0.0, allowance_w - external)
        allowance = max(0.0, allowance_w / voltage_v + ramp_a)
        return {
            'ccl_a': min(max(0.0, rec_ccl_a), allowance),
            'support_w': support,
            'limited_by': ('REC current limit' if allowance > rec_ccl_a else
                           'PV data unavailable; bounded support' if pv is None else ''),
        }
