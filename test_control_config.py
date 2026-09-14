#!/usr/bin/env python3
"""Invalid configuration must fail before policy or actuator construction."""
import os
import sys
import tempfile
import unittest

from test_stubs import REPO, load

sys.path.insert(0, os.path.join(REPO, 'dbus-recbms'))
from rec_control_config import ControlConfig

REC = load(os.path.join(REPO, 'dbus-recbms', 'dbus_recbms.py'), 'rec_config_validation')
# The consumer imports velib's dbusmonitor at module level; only its Config is under test here.
sys.modules.setdefault('dbusmonitor', type(sys)('dbusmonitor'))
sys.modules['dbusmonitor'].DbusMonitor = object
SP = load(os.path.join(REPO, 'dbus-recbms', 'solar_priority.py'), 'solar_config_validation')
class ControlConfigurationTests(unittest.TestCase):
    def test_default_relay_limits_and_source_alignment(self):
        config = ControlConfig()
        self.assertEqual(config.connected_dwell_s, 300)
        self.assertEqual(config.hourly_departures, 3)
        self.assertEqual(config.daily_departures, 12)
        self.assertLessEqual(config.source_alignment_s, config.source_gap_s)
        # Stage A's bounded preparation wait before an ordinary return.
        self.assertEqual(config.return_prepare_s, 30)
        self.assertEqual(ControlConfig({'return_prepare_s': 45}).return_prepare_s, 45)

    def test_shipped_control_section_carries_the_return_preparation_bound(self):
        import configparser
        shipped = configparser.ConfigParser(interpolation=None)
        shipped.read(os.path.join(REPO, 'dbus-recbms', 'config.ini'))
        values = dict(shipped['control'])
        self.assertEqual(float(values['return_prepare_s']), 30)
        self.assertFalse(set(values) - set(ControlConfig.DEFAULTS) -
                         set(ControlConfig.RETIRED_OPTIONS))
        self.assertEqual(ControlConfig(values).return_prepare_s, 30)

    def test_required_controls_cannot_be_blank_or_nonfinite(self):
        for key in ControlConfig.DEFAULTS:
            for value in (None, '', '  ', float('nan'), float('inf'), -float('inf'), True):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    ControlConfig({key: value})

    def test_timing_and_physical_transfer_guards(self):
        for key in ControlConfig.DEFAULTS:
            if key.endswith('_s'):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    ControlConfig({key: 0})
        for values in ({'connected_dwell_s': 299}, {'failed_probe_backoff_s': 899},
                       {'hourly_departures': 4}, {'daily_departures': 13},
                       {'hourly_departures': 1.5}, {'daily_departures': 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ControlConfig(values)

    def test_invalid_topology_and_unknown_controls_fail(self):
        for values in ({'inverter_efficiency': 1.01}, {'inverter_efficiency': 0},
                       {'source_alignment_s': 11}, {'inverter_idle_w': -1},
                       {'unknown': 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ControlConfig(values)

    def test_retired_policy_keys_do_not_restore_operational_vetoes(self):
        config = ControlConfig({'return_energy_bound_wh': '', 'waterline_loss_wh': 1,
                                'minimum_soc': 99, 'terminal_voltage_v': 61.96})
        self.assertFalse(hasattr(config, 'return_energy_bound_wh'))
        self.assertFalse(hasattr(config, 'minimum_soc'))
        self.assertEqual(config.connected_dwell_s, 300)


class DriverConfigurationTests(unittest.TestCase):
    def parse(self, contents=''):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'config.ini')
            with open(path, 'w') as stream:
                stream.write(contents)
            return REC.Config(path)

    def test_shipped_configuration_preserves_verified_installation_values(self):
        import configparser
        import json
        from pathlib import Path
        expected = json.loads((Path(REPO) / 'test_fixtures/recbms-preserved-calibration.json').read_text())
        actual = configparser.ConfigParser(interpolation=None)
        actual.read(os.path.join(REPO, 'dbus-recbms', 'config.ini'))
        for section, values in expected.items():
            self.assertTrue(actual.has_section(section), section)
            self.assertEqual(dict(actual[section]), values, section)
        self.assertTrue(actual.has_section('control'))

    def test_defaults_and_shipped_config_pass(self):
        self.assertEqual(self.parse().installation_max_v, 62.4)
        self.assertEqual(REC.Config(os.path.join(REPO, 'dbus-recbms', 'config.ini')).live_timeout, 60)
        self.assertEqual(self.parse('[cvl]\ninstallation_max_v=63\n').installation_max_v, 62.4)
        with self.assertRaisesRegex(ValueError, 'normal CVL'):
            self.parse('[cvl]\nmax_v=62.4\n')

    def test_nonfinite_values_fail_before_minimum_or_maximum_clamps(self):
        settings = [('cvl', 'installation_max_v'), ('cvl', 'voltage_guard_v'),
                    ('cvl', 'solar_lead_v'), ('solarboost', 'hold_s'),
                    ('fallback', 'live_timeout_s'), ('fallback', 'safe_cvl_v'),
                    ('sustain', 'step_pct'), ('sustain', 'servo_period_s'),
                    ('battery', 'installed_capacity_ah'), ('publish', 'voltage_step')]
        for section, key in settings:
            for value in ('nan', 'inf', '-inf'):
                with self.subTest(section=section, key=key, value=value), self.assertRaises(ValueError):
                    self.parse('[%s]\n%s=%s\n' % (section, key, value))

    def test_nonpositive_cadence_duration_and_capacity_fail(self):
        settings = [('can', 'reconnect_delay_s'), ('quattro', 'poll_s'),
                    ('fallback', 'live_timeout_s'),
                    ('solarboost', 'hold_s'), ('sustain', 'servo_period_s'),
                    ('battery', 'installed_capacity_ah')]
        for section, key in settings:
            for value in ('0', '-1'):
                with self.subTest(section=section, key=key, value=value), self.assertRaises(ValueError):
                    self.parse('[%s]\n%s=%s\n' % (section, key, value))

    def test_curve_points_are_finite_distinct_and_ordered(self):
        for curve in ('40:54,nan:60', '40:54,100:inf', '40:54,40:55',
                      '40:55,100:54', '40:54,101:60', '40:0,100:60',
                      '40:54:55,100:60', '40:54,100'):
            with self.subTest(curve=curve), self.assertRaises(ValueError):
                self.parse('[cvl]\ncurve=%s\n' % curve)

    def test_invalid_slider_source_and_fallback_ranges_fail(self):
        for contents in ('[slider]\nmin=100\n', '[slider]\ndefault=20\n',
                         '[policy]\nmppt_instances=278,278\n',
                         '[fallback]\nlive_timeout_s=150\nalert_timeout_s=120\n'):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                self.parse(contents)


class ConsumerConfigurationTests(unittest.TestCase):
    def parse(self, contents=''):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'solar_priority.ini')
            with open(path, 'w') as stream:
                stream.write(contents)
            return SP.Config(path)

    def test_full_threshold_must_agree_with_the_protocol_full_mode(self):
        # E12: threshold 0 and target 100 % had the engine ask for a floor
        # under COMPLETE_FULL, which the contract rejects on every request.
        self.assertEqual(self.parse().engine['ONEWAY_FULL_PCT'], 100)
        self.assertEqual(self.parse('[engine]\noneway_full_pct = 100\n').engine['ONEWAY_FULL_PCT'], 100)
        self.assertEqual(self.parse('[engine]\noneway_full_pct = 95\n').engine['ONEWAY_FULL_PCT'], 95)
        for value in ('0', '101', '100.5'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'oneway_full_pct'):
                self.parse('[engine]\noneway_full_pct = %s\n' % value)

    def test_shipped_engine_ini_and_uncommented_examples_parse_and_apply(self):
        # The shipped ini had no [engine] header: uncommenting any example made
        # ConfigParser reject the file (master review D18).
        import re
        path = os.path.join(REPO, 'dbus-recbms', 'solar_priority.ini')
        text = open(path, encoding='utf-8').read()
        self.assertEqual(SP.Config(path).engine, {k: float(v) for k, v in SP.ENGINE_DEFAULTS.items()})
        examples = dict(re.findall(r'^;(\w+) = ([\d.]+)$', text, re.M))
        self.assertEqual(set(examples), {k.lower() for k in SP.ENGINE_DEFAULTS})
        for key, default in SP.ENGINE_DEFAULTS.items():
            self.assertEqual(float(examples[key.lower()]), float(default), key)
        for key, value in (('oneway_enter_pct', '3'), ('solar_margin', '1.2'), ('cooldown_ms', '600000')):
            uncommented = text.replace(';%s = %s' % (key, examples[key]), '%s = %s' % (key, value))
            self.assertNotEqual(uncommented, text)
            with self.subTest(key=key):
                self.assertEqual(self.parse(uncommented).engine[key.upper()], float(value))

    def test_accepted_full_configuration_never_requests_a_floor_under_complete_full(self):
        # Issue #7, restated for 4.16 (master D12): toward a full target the
        # consumer sends CHARGE with the floor; only the arrival maps to
        # COMPLETE_FULL, and that request must carry release.
        from policy_contract import PolicyContract, VERSION
        for full in (100, 95):
            for soc, expected_mode, expected_intent in ((60., 'CHARGE', 'floor'),
                                                        (99.6, 'COMPLETE_FULL', 'release')):
                engine = SP.Engine(dict(SP.ENGINE_DEFAULTS, ONEWAY_FULL_PCT=full), 0)
                inputs = SP.Inputs()
                inputs.enabled = True
                for name in ('soc', 'batt', 'load_now', 'load_avg', 'load_slow', 'feed', 'ac_out',
                             'batt_v', 'cvl', 'quattro_w', 'demand_avg', 'demand_slow', 'demand_margin'):
                    setattr(inputs, name, SP.Val({'soc': soc, 'feed': 0., 'batt_v': 56.4, 'cvl': 56.42}.get(name, 100.), 1000))
                inputs.target_soc = SP.Val(100., 1000)
                out = engine.tick(1000, inputs)
                # the consumer's mapping (solar_priority.py): one-way first, the endgame at arrival
                mode = ({'charge': 'CHARGE', 'discharge': 'DISCHARGE'}.get(out.oneway) or
                        ('COMPLETE_FULL' if 100. >= full else 'HOLD'))
                contract = PolicyContract(generation='g')
                request = dict(version=VERSION, generation='g', request_id=1, mode=mode,
                               target_soc=100., transfer_intent='connected', lease_s=15.,
                               requested_limits={'sustain': {'release': 0, 'floor': 1, 'ceiling': 2, 'hold': 3}[out.charge_intent]})
                with self.subTest(full=full, soc=soc):
                    self.assertEqual((mode, out.charge_intent), (expected_mode, expected_intent))
                    self.assertTrue(contract.accept(request, 0, 100., consumer_ready=True), contract.rejection)


if __name__ == '__main__':
    unittest.main()
