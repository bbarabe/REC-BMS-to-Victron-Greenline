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
class ControlConfigurationTests(unittest.TestCase):
    def test_default_relay_limits_and_source_alignment(self):
        config = ControlConfig()
        self.assertEqual(config.connected_dwell_s, 300)
        self.assertEqual(config.hourly_departures, 3)
        self.assertEqual(config.daily_departures, 12)
        self.assertLessEqual(config.source_alignment_s, config.source_gap_s)

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


if __name__ == '__main__':
    unittest.main()
