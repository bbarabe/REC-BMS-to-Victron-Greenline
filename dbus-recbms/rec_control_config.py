"""REC metering and relay limits; no charge-mode selection or energy vetoes.

Retired configuration keys are ignored only to allow publisher-first upgrade
with the boat's existing config.ini. They do not affect operation.
"""
import math


class ControlConfig:
    DEFAULTS = {'positive_reserve_w': 20.0, 'reverse_power_w': 20.0, 'reverse_response_s': 10.0, 'reverse_event_wh': 20.0, 'reverse_day_wh': 100.0, 'stable_admission_s': 60.0, 'minimum_useful_pv_w': 20.0, 'source_gap_s': 10.0, 'ordinary_overhead_efc': 0.01, 'connected_dwell_s': 300.0, 'transfer_timeout_s': 30.0, 'return_prepare_s': 30.0, 'source_alignment_s': 2.0, 'current_settle_s': 30.0, 'hourly_departures': 3, 'daily_departures': 12, 'failed_probe_backoff_s': 900.0, 'inverter_efficiency': 0.9, 'inverter_idle_w': 30.0, 'demand_uncertainty_w': 30.0}
    RETIRED_OPTIONS = ('normal_max_v', 'minimum_soc', 'selection_tolerance_pct', 'selection_entry_tolerance_pct', 'buffer_width_pct', 'maximum_buffer_pct', 'discharge_bias_w', 'transfer_margin_s', 'probe_duration_s', 'full_entry_soc', 'full_entry_s', 'full_bulk_soc', 'full_bulk_s', 'anchor_resistance_ohm', 'solar_lead_v', 'terminal_voltage_v', 'terminal_stall_s', 'terminal_taper_a', 'current_resolution_a', 'maximum_overhead_efc', 'charger_settle_s', 'charger_stable_s', 'mppt_settle_s', 'waterline_deficit_s', 'waterline_loss_wh', 'waterline_hysteresis_v', 'current_ramp_a', 'return_energy_bound_wh', 'support_max_correction_v', 'support_step_v', 'support_observation_s')

    def __init__(self, values=None):
        values = values or {}
        unknown = set(values) - set(self.DEFAULTS) - set(self.RETIRED_OPTIONS)
        if unknown:
            raise ValueError('unknown control parameters: ' + ', '.join(sorted(unknown)))
        for name, default in self.DEFAULTS.items():
            value = values.get(name, default)
            if isinstance(value, bool):
                raise ValueError('invalid control parameter: ' + name)
            try:
                value = float(value)
            except (ValueError, TypeError, OverflowError):
                raise ValueError('invalid control parameter: ' + name)
            if not math.isfinite(value) or value < 0 or (name.endswith('_s') and value <= 0):
                raise ValueError('invalid control parameter: ' + name)
            setattr(self, name, value)
        if not 0 < self.inverter_efficiency <= 1:
            raise ValueError('invalid inverter efficiency')
        if self.source_alignment_s > self.source_gap_s:
            raise ValueError('source alignment exceeds freshness')
        if self.connected_dwell_s < 300 or self.failed_probe_backoff_s < 900:
            raise ValueError('relay dwell/backoff below installation limits')
        for name, maximum in (('hourly_departures', 3), ('daily_departures', 12)):
            value = getattr(self, name)
            if not value.is_integer() or not 1 <= value <= maximum:
                raise ValueError('invalid relay departure limit: ' + name)
