#!/usr/bin/env python3
"""Offline accounting/evidence regressions: python3 test_energy_accounting.py."""
import importlib.util
import builtins
import datetime
import os
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('energy_accounting', os.path.join(
    os.path.dirname(__file__), 'dbus-recbms', 'energy_accounting.py'))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)


class EnergyAccountingTests(unittest.TestCase):
    def ledger(self, **kwargs):
        return E.EnergyLedger(max_gap_s=2000, **kwargs)

    def cycle(self, ledger, reservation_id=None):
        ledger.set_policy('HOLD', 80, 80)
        # Two exact triangular profiles integrate to 14.4 Ah per leg.
        for now, current in ((0, 0), (1800, 28.8), (3600, 0), (5400, -28.8), (7200, 0)):
            ledger.sample(now, 1700000000 + now, current, 58,
                          overhead_category='buffer', reservation_id=reservation_id)

    def test_closed_cycle_matches_design_without_net_cancellation(self):
        ledger = self.ledger()
        self.cycle(ledger)
        state = ledger.snapshot()
        for section in ('total', 'overhead'):
            for key, expected in (('charge_ah', 14.4), ('discharge_ah', 14.4),
                                  ('charge_wh', 835.2), ('discharge_wh', 835.2), ('efc', 0.01)):
                self.assertAlmostEqual(state[section][key], expected)
        self.assertAlmostEqual(state['net_ah'], 0)
        self.assertAlmostEqual(state['budget']['spent_efc'], 0.01)

    def test_zero_crossing_avoids_endpoint_rectification(self):
        self.assertEqual(E._integrate_signed(2, -2, 1), (0.5, 0.5))
        self.assertEqual(E._integrate_signed(-2, 2, 1), (0.5, 0.5))
        self.assertEqual(E._integrate_signed(0, 0, 1), (0, 0))

    def test_requested_charge_and_descent_not_discretionary(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 80, 77)
        ledger.sample(0, 100, 10, 58, soc=77)
        ledger.sample(1800, 1900, 10, 58, soc=78)
        self.assertEqual(ledger.snapshot()['total']['charge_ah'], 5)
        self.assertEqual(ledger.snapshot()['overhead']['efc'], 0)
        ledger.set_policy('DISCHARGE', 70, 78)
        ledger.sample(1801, 1901, -10, 58)
        before = ledger.snapshot()['overhead']['efc']
        ledger.sample(3601, 3701, -10, 58, soc=77)
        self.assertLess(ledger.snapshot()['overhead']['efc'] - before, 0.000001)
        self.assertGreater(ledger.snapshot()['total']['discharge_ah'], 5)

    def test_quantized_soc_and_duplicate_sample_create_no_energy(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 80, 77)
        ledger.sample(0, 100, 0, 58, soc=77)
        ledger.sample(0, 100, 1000, 58, soc=99)
        ledger.sample(1800, 1900, 0, 58, soc=76)
        self.assertEqual(ledger.snapshot()['total'], E._totals())
        self.assertEqual(ledger.snapshot()['references']['high_soc'], 77)

    def test_reserve_wh_and_entire_return_before_admission(self):
        ledger = self.ledger()
        self.assertFalse(ledger.reserve('big', 'buffer', 1, 1, 100000, 100000))
        self.assertTrue(ledger.reserve('cycle', 'buffer', 14.4, 14.4, 835.2, 835.2))
        self.assertAlmostEqual(ledger.budget()['remaining_efc'], 0.01)
        self.assertFalse(ledger.reserve('other', 'maintenance', 15, 15, 870, 870))
        self.assertTrue(ledger.reserve('cycle', 'buffer', 14.4, 14.4, 835.2, 835.2))
        self.assertFalse(ledger.reserve('cycle', 'buffer', 1, 1, 58, 58))
        self.cycle(ledger, 'cycle')
        self.assertAlmostEqual(ledger.budget()['remaining_efc'], 0.01)
        ledger.close_reservation('cycle', completed=True)
        self.assertAlmostEqual(ledger.budget()['remaining_efc'], 0.01)

    def test_interrupted_reservation_debits_without_inventing_measurement(self):
        ledger = self.ledger()
        ledger.reserve('cycle', 'maintenance', 14.4, 14.4, 835.2, 835.2)
        ledger.close_reservation('cycle')
        self.assertAlmostEqual(ledger.budget()['spent_efc'], 0.01)
        self.assertAlmostEqual(ledger.budget()['spent_wh'], 1670.4)
        self.assertEqual(ledger.snapshot()['total']['efc'], 0)
        self.assertEqual(ledger.snapshot()['overhead']['efc'], 0)

    def test_midnight_wall_jumps_toggle_and_retarget_preserve_spend(self):
        ledger = self.ledger()
        self.cycle(ledger)
        before = ledger.budget()['spent_efc']
        for now, wall, mode, target in ((7201, 86401, 'OFF', 80),
                                       (7202, 86400000, 'CHARGE', 90),
                                       (7203, -500, 'DISCHARGE', 70)):
            ledger.set_policy(mode, target)
            ledger.sample(now, wall, 0, 58)
            self.assertAlmostEqual(ledger.budget()['spent_efc'], before)

    def test_rolling_budget_needs_full_observed_window(self):
        ledger = self.ledger()
        ledger.reserve('cycle', 'maintenance', 14.4, 14.4, 835.2, 835.2)
        ledger.close_reservation('cycle')
        for now in range(0, 86401, 1800):
            ledger.sample(now, now, 0, 58)
        self.assertGreater(ledger.budget()['spent_efc'], 0)
        ledger.sample(86500, 86500, 0, 58)
        self.assertEqual(ledger.budget()['spent_efc'], 0)

    def test_gap_invalidity_withholds_credit_without_integrating(self):
        ledger = E.EnergyLedger()
        ledger.set_policy('HOLD', 80)
        ledger.sample(0, 100, 10, 58, solar_surplus_w=580)
        ledger.sample(10, 110, 10, 58, solar_surplus_w=580)
        before = ledger.snapshot()['total'].copy()
        self.assertGreater(ledger.snapshot()['buffer']['credit_wh'], 0)
        ledger.sample(1000, 1100, 10, 58)
        self.assertEqual(ledger.snapshot()['total'], before)
        self.assertTrue(ledger.budget()['uncertain'])
        self.assertEqual(ledger.snapshot()['buffer']['credit_wh'], 0)
        ledger.sample(1001, 1101, float('nan'), 58)
        self.assertFalse(ledger.snapshot()['complete_history'])
        self.assertFalse(ledger.reserve('blocked', 'buffer', 1, 1, 58, 58))

    def test_directional_reference_and_controller_state_survive_restart(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 80, 77)
        ledger.sample(0, 100, 10, 58)
        ledger.sample(1800, 1900, 10, 58)
        high = ledger.snapshot()['references']['high_wh']
        ledger.set_policy('OFF', 80)
        ledger.set_policy('CHARGE', 80)
        self.assertEqual(ledger.snapshot()['references']['high_wh'], high)
        with tempfile.TemporaryDirectory() as directory:
            ledger.path = os.path.join(directory, 'ledger.json')
            ledger.controller_state['full_phase'] = 'COMPLETE_FULL'
            self.assertTrue(ledger.save())
            restored = self.ledger(path=ledger.path)
            self.assertEqual(restored.snapshot()['references']['high_wh'], high)
            self.assertEqual(restored.controller_state['full_phase'], 'COMPLETE_FULL')
            self.assertTrue(restored.budget()['uncertain'])
            restored.sample(0, 999999, 20, 58)
            self.assertEqual(restored.snapshot()['total'], ledger.snapshot()['total'])

    def test_capacity_version_does_not_rewrite_history(self):
        ledger = self.ledger()
        self.cycle(ledger)
        with tempfile.TemporaryDirectory() as directory:
            ledger.path = os.path.join(directory, 'ledger.json')
            ledger.save()
            with self.assertRaises(ValueError):
                self.ledger(path=ledger.path, capacity_ah=1400)
            changed = self.ledger(path=ledger.path, capacity_ah=1400,
                                  capacity_version='calibrated-1400')
            self.assertAlmostEqual(changed.snapshot()['total']['efc'], 0.01)
            changed.sample(0, 100, 14, 58)
            changed.sample(1800, 1900, 14, 58)
            self.assertAlmostEqual(changed.snapshot()['total']['efc'], 0.0125)
            self.assertEqual(len(changed.snapshot()['capacities']), 2)

    def test_atomic_failure_preserves_previous_file_and_refuses_actuation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'ledger.json')
            ledger = self.ledger()
            ledger.path = path
            self.assertTrue(ledger.save())
            with open(path) as handle:
                prior = handle.read()
            with mock.patch.object(E.os, 'replace', side_effect=OSError('simulated failure')):
                self.assertFalse(ledger.reserve('cycle', 'buffer', 1, 1, 58, 58))
            with open(path) as handle:
                self.assertEqual(handle.read(), prior)
            self.assertTrue(ledger.budget()['uncertain'])
            self.assertEqual(os.listdir(directory), ['ledger.json'])

    def test_missing_corrupt_unknown_version_history_is_conservative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'ledger.json')
            self.assertTrue(self.ledger(path=path).budget()['uncertain'])
            for payload in ('{', '{"schema_version":900}', '{"schema_version":1,"total":{}}'):
                with open(path, 'w') as handle:
                    handle.write(payload)
                self.assertTrue(self.ledger(path=path).budget()['uncertain'])

    def test_solar_credit_requires_verified_net_pv_and_discharge_consumes_it(self):
        ledger = self.ledger()
        ledger.set_policy('HOLD', 80)
        ledger.sample(0, 100, 10, 58)
        ledger.sample(1800, 1900, 10, 58)
        self.assertEqual(ledger.snapshot()['buffer']['credit_wh'], 0)
        self.assertGreater(ledger.snapshot()['overhead']['charge_wh'], 0)
        ledger.sample(1801, 1901, 10, 58, solar_surplus_w=290)
        ledger.sample(3601, 3701, 10, 58, solar_surplus_w=290)
        self.assertAlmostEqual(ledger.snapshot()['buffer']['credit_wh'], 145)
        ledger.sample(3602, 3702, -10, 58)
        ledger.sample(5402, 5502, -10, 58)
        self.assertEqual(ledger.snapshot()['buffer']['credit_wh'], 0)

    def test_reverse_microdeficit_and_corrective_return_both_cost(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 80)
        ledger.sample(0, 100, -40 / 58, 58)
        ledger.sample(1800, 1900, -40 / 58, 58)
        state = ledger.snapshot()
        self.assertAlmostEqual(state['reverse']['event_wh'], 20)
        self.assertEqual(state['reverse']['event_remaining_wh'], 0)
        self.assertAlmostEqual(state['budget']['reverse_wh'], 20)
        ledger.sample(1801, 1901, 40 / 58, 58)
        ledger.sample(3601, 3701, 40 / 58, 58)
        self.assertGreaterEqual(ledger.snapshot()['overhead']['charge_wh'], 20)
        self.assertGreaterEqual(ledger.snapshot()['reverse']['event_wh'], 20)
        with mock.patch.object(ledger, 'save', wraps=ledger.save) as save:
            ledger.end_reverse_event()
            for _ in range(60):
                ledger.end_reverse_event()
            self.assertEqual(save.call_count, 1, 'unchanged recovery must not rewrite flash each tick')
        self.assertEqual(ledger.snapshot()['reverse']['event_wh'], 0)
        self.assertGreaterEqual(ledger.budget()['reverse_wh'], 20)

    def test_fractional_probe_cost_rounding_does_not_hide_last_allowance(self):
        ledger = self.ledger()
        ledger._state['buckets'].append(dict(E._totals(), end_s=60, reverse_wh=80.00000000000001))
        self.assertTrue(ledger.reserve('last', 'probe', 20 / 58, 20 / 58, 20, 20))
        self.assertFalse(ledger.reserve('excess', 'probe', .001 / 58, .001 / 58, .001, .001))

    def test_probe_reservations_share_event_and_aggregate_budgets(self):
        ledger = self.ledger()
        self.assertFalse(ledger.reserve('large', 'probe', 21 / 58, 21 / 58, 21, 21))
        for index in range(5):
            self.assertTrue(ledger.reserve(str(index), 'probe', 20 / 58, 20 / 58, 20, 20))
        self.assertFalse(ledger.reserve('sixth', 'probe', 1 / 58, 1 / 58, 1, 1))


    def test_restart_abandons_leases_but_retains_and_ages_reserved_cost(self):
        ledger = self.ledger()
        ledger.reserve('probe', 'probe', 20 / 58, 20 / 58, 20, 20)
        with tempfile.TemporaryDirectory() as directory:
            ledger.path = os.path.join(directory, 'ledger.json')
            ledger.save()
            restored = self.ledger(path=ledger.path)
            self.assertEqual(restored.snapshot()['reservations'], {})
            self.assertAlmostEqual(restored.budget()['reverse_wh'], 20)
            self.assertGreater(restored.budget()['spent_efc'], 0)
            for now in range(0, 88201, 1800):
                restored.sample(now, now, 0, 58)
            self.assertFalse(restored.budget()['uncertain'])
            self.assertEqual(restored.budget()['spent_efc'], 0)
            self.assertEqual(restored.budget()['reverse_wh'], 0)

    def test_hold_probe_discharge_counts_against_reverse_subbudget(self):
        ledger = self.ledger()
        ledger.set_policy('HOLD', 80)
        ledger.reserve('probe', 'probe', 20 / 58, 20 / 58, 20, 20)
        ledger.sample(0, 100, -40 / 58, 58, reservation_id='probe')
        ledger.sample(1800, 1900, -40 / 58, 58, reservation_id='probe')
        self.assertAlmostEqual(ledger.budget()['reverse_wh'], 20)
        ledger.close_reservation('probe', completed=False)
        self.assertAlmostEqual(ledger.budget()['reverse_wh'], 20)

    def test_low_water_never_rises_and_off_still_records_total_use(self):
        ledger = self.ledger()
        ledger.set_policy('DISCHARGE', 70, 80)
        ledger.sample(0, 100, -10, 58)
        ledger.sample(1800, 1900, -10, 58, soc=79)
        low = ledger.snapshot()['references']['low_wh']
        ledger.sample(1801, 1901, 10, 58, soc=79.5)
        ledger.sample(3601, 3701, 10, 58, soc=80)
        self.assertEqual(ledger.snapshot()['references']['low_wh'], low)
        ledger.set_policy('OFF', 70)
        before = ledger.snapshot()['total']['charge_ah']
        ledger.sample(5401, 5501, 10, 58)
        self.assertAlmostEqual(ledger.snapshot()['total']['charge_ah'] - before, 5)

    def test_late_can_receipt_does_not_rewind_observation_clock_or_lose_energy(self):
        ledger = self.ledger()
        ledger.sample(0, 100, 10, 58)
        ledger.observe_time(.9, 100.9)
        self.assertTrue(ledger.sample(.5, 101, 10, 58)['valid'])
        ledger.observe_time(1.1, 101.1)
        self.assertTrue(ledger.sample(1.0, 101.2, 10, 58)['valid'])
        state = ledger.snapshot()
        self.assertAlmostEqual(state['total']['charge_ah'], 10 / 3600)
        self.assertEqual(state['gap_count'], 0)
        self.assertAlmostEqual(state['logical_s'], 1.1)
        self.assertFalse(state['budget']['uncertain'])

    def test_out_of_order_measurement_ignored_but_observer_reset_invalidates(self):
        ledger = self.ledger()
        ledger.sample(0, 100, 10, 58)
        ledger.sample(1, 101, 10, 58)
        self.assertFalse(ledger.sample(.5, 102, 500, 58)['valid'])
        ledger.sample(2, 103, 10, 58)
        self.assertAlmostEqual(ledger.snapshot()['total']['charge_ah'], 20 / 3600)
        self.assertEqual(ledger.snapshot()['gap_count'], 0)
        ledger.observe_time(1, 104)
        self.assertTrue(ledger.budget()['uncertain'])
        self.assertFalse(ledger.sample(1.5, 105, 10, 58)['valid'])

    def test_reverse_flag_counts_actual_return_not_unrelated_requested_progress(self):
        for mode, sign in (('CHARGE', 1), ('DISCHARGE', -1)):
            with self.subTest(mode=mode):
                ledger = self.ledger()
                ledger.set_policy(mode, 40, 40)
                for now, current in ((0, -sign), (10, -sign), (11, sign), (31, sign)):
                    ledger.sample(now, 100 + now, current, 58, overhead_category='reverse')
                state = ledger.snapshot()
                wrong = 'discharge' if sign > 0 else 'charge'
                useful = 'charge' if sign > 0 else 'discharge'
                self.assertAlmostEqual(state['overhead'][wrong + '_ah'], state['total'][wrong + '_ah'])
                self.assertAlmostEqual(state['overhead'][useful + '_ah'], state['total'][wrong + '_ah'])
                self.assertGreater(state['total'][useful + '_ah'], state['overhead'][useful + '_ah'])
                self.assertEqual(state['recovery'][useful + '_ah'], 0)

    def test_zero_crossing_recovery_respects_order_inside_the_interval(self):
        for first, last, expected_charge, expected_debt in ((-2, 2, .25, 0), (2, -2, 0, .25)):
            with self.subTest(first=first):
                ledger = self.ledger()
                ledger.set_policy('CHARGE', 40, 39)
                ledger.sample(0, 100, first, 58)
                ledger.sample(1800, 1900, last, 58)
                state = ledger.snapshot()
                self.assertAlmostEqual(state['overhead']['charge_ah'], expected_charge)
                self.assertAlmostEqual(state['overhead']['discharge_ah'], .25)
                self.assertAlmostEqual(state['recovery']['charge_ah'], expected_debt)

    def test_maintenance_return_remains_overhead_across_target_change_and_restart(self):
        ledger = self.ledger()
        ledger.set_policy('HOLD', 60, 60)
        ledger.sample(0, 100, 2, 58, overhead_category='maintenance')
        ledger.sample(1800, 1900, 2, 58, overhead_category='maintenance')
        self.assertAlmostEqual(ledger.snapshot()['recovery']['discharge_ah'], 1)
        with tempfile.TemporaryDirectory() as directory:
            ledger.path = os.path.join(directory, 'ledger.json')
            ledger.save()
            restored = self.ledger(path=ledger.path)
            restored.set_policy('DISCHARGE', 40, 60)
            restored.sample(0, 2000, -4, 58)
            restored.sample(1800, 3800, -4, 58)
            state = restored.snapshot()
            self.assertAlmostEqual(state['total']['discharge_ah'], 2)
            self.assertAlmostEqual(state['overhead']['discharge_ah'], 1)
            self.assertEqual(state['recovery']['discharge_ah'], 0)

    def test_new_reverse_reservation_cannot_reset_an_open_event_allowance(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 40, 39)
        ledger.sample(0, 100, -20 / 58, 58)
        ledger.sample(1800, 1900, -20 / 58, 58)
        self.assertFalse(ledger.reserve('too_large', 'probe', 20 / 58, 20 / 58, 20, 20))
        self.assertTrue(ledger.reserve('remaining', 'probe', 10 / 58, 10 / 58, 10, 10))
        ledger.end_reverse_event()
        self.assertTrue(ledger.reserve('new_event', 'probe', 20 / 58, 20 / 58, 20, 20))
        self.assertAlmostEqual(ledger.budget()['reverse_wh'], 10)

    def test_charge_reference_needs_explicit_permission_and_solar_at_both_endpoints(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 40, 39)
        ledger.sample(0, 100, 2, 58, solar_surplus_w=116)
        ledger.sample(10, 110, 2, 58, solar_surplus_w=116, advance_charge_reference=True)
        self.assertEqual(ledger.snapshot()['references']['high_wh'], 0)
        ledger.sample(20, 120, 2, 58, solar_surplus_w=116, advance_charge_reference=True)
        earned = 116 * 10 / 3600
        self.assertAlmostEqual(ledger.snapshot()['references']['high_wh'], earned)
        ledger.sample(30, 130, 20, 58, advance_charge_reference=True)
        ledger.sample(40, 140, 20, 58, advance_charge_reference=True)
        self.assertAlmostEqual(ledger.snapshot()['references']['high_wh'], earned)
        self.assertGreater(ledger.snapshot()['net_wh'], earned)

    def test_shore_burst_and_soc_jump_cannot_become_solar_earned_high_water(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 40, 39)
        ledger.sample(0, 100, 20, 58, soc=39)
        ledger.sample(1800, 1900, 20, 58, soc=41)
        ledger.sample(1801, 1901, 20, 58, soc=41, solar_surplus_w=58,
                      advance_charge_reference=True)
        ledger.sample(3601, 3701, 20, 58, soc=41, solar_surplus_w=58,
                      advance_charge_reference=True)
        state = ledger.snapshot()
        self.assertAlmostEqual(state['references']['high_wh'], 29)
        self.assertAlmostEqual(state['references']['high_ah'], .5)
        self.assertAlmostEqual(state['references']['high_soc'], 39 + .5 * 100 / 1440)
        self.assertGreater(state['net_wh'], 1000)

    def test_solar_recovery_below_charge_floor_does_not_advance_it(self):
        ledger = self.ledger()
        ledger.set_policy('CHARGE', 40, 39)
        ledger.sample(0, 100, -10, 58)
        ledger.sample(1800, 1900, -10, 58)
        ledger.sample(1801, 1901, 1, 58, solar_surplus_w=58,
                      advance_charge_reference=True)
        ledger.sample(3601, 3701, 1, 58, solar_surplus_w=58,
                      advance_charge_reference=True)
        self.assertLess(ledger.snapshot()['net_wh'], 0)
        self.assertEqual(ledger.snapshot()['references']['high_wh'], 0)

    def test_calendar_timezone_is_explicit_and_separate_from_rolling_budget(self):
        zone = datetime.timezone(datetime.timedelta(hours=2))
        ledger = self.ledger(calendar_timezone=zone)
        ledger.sample(0, 23 * 3600, 10, 58)
        ledger.sample(1800, 23.5 * 3600, 10, 58)
        days = ledger.snapshot()['calendar_days']
        self.assertEqual(list(days), ['1970-01-02'])
        self.assertEqual(days['1970-01-02']['total']['charge_ah'], 5)
        self.assertEqual(days['1970-01-02']['timezone'], 'UTC+02:00')


    def test_explicit_utc_matches_default_calendar_and_budget(self):
        implicit, explicit = self.ledger(), self.ledger(calendar_timezone='UTC')
        wall = datetime.datetime(2026, 9, 11, 23, 59, 58,
                                 tzinfo=datetime.timezone.utc).timestamp()
        for ledger in (implicit, explicit):
            for now in range(3):
                ledger.sample(now, wall + now, 10, 58, overhead_category='buffer')
        self.assertEqual(implicit.snapshot(), explicit.snapshot())
        self.assertEqual(set(explicit.snapshot()['calendar_days']),
                         {'2026-09-11', '2026-09-12'})

    def test_iana_local_midnight_groups_energy_without_resetting_budget(self):
        ledger = self.ledger(calendar_timezone='America/Los_Angeles')
        utc = self.ledger(calendar_timezone='UTC')
        wall = datetime.datetime(2026, 9, 12, 6, 59, 58,
                                 tzinfo=datetime.timezone.utc).timestamp()
        for active in (ledger, utc):
            for now in range(3):
                active.sample(now, wall + now, 10, 58, overhead_category='buffer')
        days = ledger.snapshot()['calendar_days']
        self.assertEqual(set(days), {'2026-09-11', '2026-09-12'})
        for day in days.values():
            self.assertEqual(day['timezone'], 'America/Los_Angeles')
            self.assertAlmostEqual(day['total']['charge_ah'], 10 / 3600)
        self.assertEqual(set(utc.snapshot()['calendar_days']), {'2026-09-12'})
        self.assertEqual(ledger.budget(), utc.budget())
        self.assertEqual(ledger.snapshot()['total'], utc.snapshot()['total'])

    def test_iana_dst_transitions_neither_duplicate_nor_omit_energy(self):
        # Spring skips an hour; autumn repeats it. Both are two measured seconds.
        for transition in ((2026, 3, 8, 9, 59, 59),
                           (2026, 11, 1, 8, 59, 59)):
            with self.subTest(transition=transition):
                ledger = self.ledger(calendar_timezone='America/Los_Angeles')
                utc = self.ledger(calendar_timezone='UTC')
                wall = datetime.datetime(*transition,
                                         tzinfo=datetime.timezone.utc).timestamp()
                for active in (ledger, utc):
                    for now in range(3):
                        active.sample(now, wall + now, 10, 58,
                                      overhead_category='buffer')
                days = ledger.snapshot()['calendar_days']
                self.assertEqual(len(days), 1)
                day = next(iter(days.values()))
                self.assertAlmostEqual(day['total']['charge_ah'], 20 / 3600)
                self.assertEqual(ledger.budget(), utc.budget())
                self.assertEqual(ledger.snapshot()['total'], utc.snapshot()['total'])

    def test_invalid_or_unavailable_iana_zone_refuses_but_utc_needs_no_database(self):
        with self.assertRaisesRegex(ValueError, 'valid IANA zone'):
            self.ledger(calendar_timezone='Mars/Olympus_Mons')
        real_import = builtins.__import__

        def without_zoneinfo(name, *args, **kwargs):
            if name == 'zoneinfo':
                raise ImportError('simulated older platform')
            return real_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=without_zoneinfo):
            self.assertEqual(self.ledger(calendar_timezone='UTC').calendar_timezone,
                             datetime.timezone.utc)
            with self.assertRaisesRegex(ValueError, 'zoneinfo database'):
                self.ledger(calendar_timezone='America/Los_Angeles')

    def test_reopened_zone_change_keeps_old_day_and_rolling_spend(self):
        ledger = self.ledger(calendar_timezone='UTC')
        wall = datetime.datetime(2026, 9, 11, 12,
                                 tzinfo=datetime.timezone.utc).timestamp()
        ledger.sample(0, wall, 10, 58, overhead_category='buffer')
        ledger.sample(2, wall + 2, 10, 58, overhead_category='buffer')
        old_day = ledger.snapshot()['calendar_days']['2026-09-11']
        old_spent = ledger.budget()['spent_efc']
        with tempfile.TemporaryDirectory() as directory:
            ledger.path = os.path.join(directory, 'ledger.json')
            self.assertTrue(ledger.save())
            restored = self.ledger(path=ledger.path,
                                   calendar_timezone='America/Los_Angeles')
            self.assertAlmostEqual(restored.budget()['spent_efc'], old_spent)
            self.assertTrue(restored.budget()['uncertain'])
            self.assertEqual(restored.budget()['remaining_efc'], 0)
            restored.sample(0, wall + 2, 10, 58, overhead_category='buffer')
            restored.sample(2, wall + 4, 10, 58, overhead_category='buffer')
            days = restored.snapshot()['calendar_days']
            self.assertEqual(set(days), {'2026-09-11',
                                        '2026-09-11@America/Los_Angeles'})
            self.assertEqual(days['2026-09-11'], old_day)
            new_day = days['2026-09-11@America/Los_Angeles']
            self.assertEqual(new_day['timezone'], 'America/Los_Angeles')
            self.assertAlmostEqual(new_day['total']['charge_ah'], 20 / 3600)
            self.assertAlmostEqual(restored.budget()['spent_efc'], old_spent * 2)
            self.assertAlmostEqual(restored.snapshot()['total']['charge_ah'], 40 / 3600)
            self.assertTrue(restored.budget()['uncertain'])
            self.assertTrue(restored.save())
            reopened = self.ledger(path=ledger.path,
                                   calendar_timezone='America/Los_Angeles')
            self.assertEqual(reopened.snapshot()['calendar_days'], days)
            self.assertAlmostEqual(reopened.budget()['spent_efc'], old_spent * 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
