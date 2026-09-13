#!/usr/bin/env python3
"""Contract, transfer supervisor, source-validity and demand regressions."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'dbus-recbms'))
from control_inputs import normalize_bus_snapshot, DemandModel, SourceRegistry, TimedMean, selected_battery_matches
from policy_contract import PolicyContract, TransferSupervisor, VERSION


def request(contract, rid=1, intent='connected'):
    return dict(version=VERSION, generation=contract.generation, request_id=rid,
                mode='CHARGE', target_soc=80., transfer_intent=intent,
                requested_limits={'boost_v': 0., 'purpose': '', 'sustain': 1}, lease_s=30.)


class ContractTests(unittest.TestCase):
    def test_handover_generation_reorder_and_expiry(self):
        contract = PolicyContract(generation='first')
        self.assertFalse(contract.accept(request(contract), 100, 80))
        self.assertTrue(contract.accept(request(contract), 100, 80, consumer_ready=True))
        self.assertFalse(contract.accept(request(contract), 101, 80))
        self.assertTrue(contract.accept(request(contract, 3), 101, 80))
        self.assertFalse(contract.accept(request(contract, 2), 102, 80))
        self.assertEqual(contract.accepted_id, 3)
        self.assertTrue(contract.active(130.999))
        self.assertFalse(contract.active(131))
        restarted = PolicyContract(contract.state, generation='second')
        self.assertTrue(restarted.owned)
        self.assertFalse(restarted.active(132))
        self.assertFalse(restarted.accept(request(contract, 4), 132, 80, consumer_ready=True))

    def test_rejected_payload_cannot_replace_valid_lease(self):
        contract = PolicyContract(generation='test')
        self.assertTrue(contract.accept(request(contract), 100, 80, consumer_ready=True))
        for change in ({'target_soc': 81}, {'request_id': True}, {'lease_s': float('nan')},
                       {'requested_limits': {'boost_v': .31}}, {'extra': 'unsupported'}):
            candidate = request(contract, 2)
            candidate.update(change)
            self.assertFalse(contract.accept(candidate, 101, 80))
            self.assertEqual(contract.accepted_id, 1)
            self.assertTrue(contract.active(101))


class SupervisorTests(unittest.TestCase):
    def connected(self):
        supervisor = TransferSupervisor(timeout_s=15)
        supervisor.observe(True, 0, 1000)
        return supervisor

    def test_preparation_and_dwell_before_one_reserved_departure(self):
        s = self.connected()
        self.assertIsNone(s.step('island', 299, 1299, ready=True, permitted=True))
        self.assertIsNone(s.step('prepare_island', 300, 1300, ready=True, permitted=True))
        self.assertEqual(s.state, 'PREPARE_ISLAND')
        self.assertEqual(s.step('island', 301, 1301, ready=True, permitted=True), 1)
        for now in (302, 303, 310):
            self.assertIsNone(s.step('island', now, 1000 + now, ready=True, permitted=True))
        self.assertEqual(s.snapshot(310, 1310)['departures_24h'], 1)
        s.observe(False, 311, 1311)
        self.assertEqual(s.state, 'ISLANDED')
        self.assertEqual(s.snapshot(311, 1311)['actual_edges_24h'], 1)
        self.assertEqual(s.snapshot(311, 1311)['external_edges'], 0)

    def test_new_connected_intent_cancels_pending_disconnect(self):
        s = self.connected()
        self.assertEqual(s.step('island', 300, 1300, True, True), 1)
        self.assertEqual(s.step('connected', 301, 1301, True, True), 0)
        self.assertEqual(s.pending, 0)

    def test_revoked_preparation_or_budget_cancels_pending_disconnect(self):
        for ready, permitted in ((False, True), (True, False)):
            with self.subTest(ready=ready, permitted=permitted):
                s = self.connected()
                s.step('island', 300, 1300, True, True)
                self.assertEqual(s.step('island', 301, 1301, ready, permitted), 0)

    def test_refusal_latches_across_restart_and_protection_ignores_dwell(self):
        s = self.connected()
        s.step('island', 300, 1300, True, True)
        s.command_result(1, 1, 300, 1300)
        self.assertEqual(s.step('protect', 301, 1301, False, False), 0)
        resumed = TransferSupervisor(copy.deepcopy(s.durable))
        resumed.observe(True, 0, 9000000)
        self.assertIn('fault', resumed.departure_reason(300, 9000300))
        self.assertEqual(resumed.snapshot(300, 9000300)['departures_24h'], 1)

    def test_wall_clock_jump_cannot_clear_departure_budget_or_probe_backoff(self):
        s = self.connected()
        s.step('island', 300, 1300, True, True)
        s.failed_probe(1300)
        self.assertEqual(s.snapshot(301, 10000000)['departures_24h'], 1)
        self.assertIn('backoff', s.departure_reason(301, 10000000))
        self.assertEqual(s.snapshot(302, -10000000)['departures_24h'], 1)
        self.assertIn('backoff', s.departure_reason(302, -10000000))

    def test_monotonic_elapsed_ages_budget_and_backwards_sample_grants_nothing(self):
        s = self.connected()
        s.step('island', 300, 1300, True, True)
        s.failed_probe(1300)
        s.snapshot(200, 1400)
        self.assertEqual(s.durable['logical_s'], 300)
        self.assertIn('backoff', s.departure_reason(301, 1401))
        self.assertEqual(s.durable['logical_s'], 301)
        self.assertEqual(s.snapshot(86700, 1402)['departures_24h'], 0)

    def test_missing_feedback_invalidates_departure_and_dwell(self):
        s = self.connected()
        s.observe(None, 400, 1400)
        self.assertIsNone(s.feedback)
        self.assertEqual(s.step('island', 401, 1401, True, True), 0)
        s.observe(True, 402, 1402)
        self.assertIn('dwell', s.departure_reason(403, 1403))

    def test_missing_feedback_cancels_departure_already_in_flight(self):
        s = self.connected()
        s.step('island', 300, 1300, True, True)
        s.observe(None, 301, 1301)
        self.assertEqual(s.step('island', 301, 1301, True, True), 0)
        self.assertEqual(s.snapshot(301, 1301)['departures_24h'], 1)

    def test_feedback_timeout_reconnects_and_does_not_reserve_more_departures(self):
        s = self.connected()
        s.step('island', 300, 1300, True, True)
        self.assertEqual(s.step('island', 316, 1316, True, True), 0)
        self.assertEqual(s.snapshot(316, 1316)['departures_24h'], 1)
        self.assertIn('fault', s.departure_reason(317, 1317))

    def test_protective_reconnect_retries_timed_out_write_without_30s_delay(self):
        s = self.connected()
        s.observe(False, 1, 1001)
        self.assertEqual(s.step('protect', 2, 1002, False, False), 0)
        self.assertEqual(s.step('protect', 18, 1018, False, False), 0)
        self.assertEqual(s.snapshot(18, 1018)['departures_24h'], 0)


class InputAndDemandTests(unittest.TestCase):
    def test_constant_fresh_values_invalidations_removals_and_identity(self):
        sources = SourceRegistry(max_age_s=20)
        sources.observe('old', '/load', 100, 0)
        sources.observe('old', '/load', 100, 19)
        self.assertEqual(sources.get('old', '/load', 21), 100)
        self.assertEqual(sources.samples[('old', '/load')]['changed'], 0)
        sources.observe('old', '/load', None, 22)
        self.assertIsNone(sources.get('old', '/load', 22))
        sources.observe('old', '/load', 100, 23)
        sources.remove('old')
        self.assertIsNone(sources.get('old', '/load', 24))
        sources.observe('new', '/load', 200, 24)
        self.assertEqual(sources.get('new', '/load', 24), 200)
        self.assertIsNone(sources.get('old', '/load', 24))
        self.assertIsNone(sources.get('new', '/load', 45))

    def test_optional_service_removal_preserves_other_source_generation(self):
        sources = SourceRegistry()
        sources.observe('rec', '/Soc', 60, 10)
        sources.observe('mppt', '/Yield/Power', 1000, 10)
        rec_generation = sources.generation('rec')
        mppt_generation = sources.generation('mppt')
        sources.remove('mppt')
        self.assertEqual(sources.generation('rec'), rec_generation)
        self.assertNotEqual(sources.generation('mppt'), mppt_generation)
        self.assertEqual(sources.get('rec', '/Soc', 11), 60)
        self.assertIsNone(sources.get('mppt', '/Yield/Power', 11))

    def test_elapsed_weighted_mean_quiet_and_changing_load(self):
        mean = TimedMean(60)
        self.assertEqual(mean.update(0, 100), 100)
        self.assertEqual(mean.update(30, 200), 100)
        self.assertEqual(mean.update(60, 200), 150)
        self.assertEqual(mean.update(120, 200), 200)

    def test_measured_inverter_demand_does_not_double_count_idle(self):
        model = DemandModel(efficiency=.9, idle_w=30, uncertainty_w=30)
        estimated = model.estimate(900, 500, connected=False)
        direct = model.estimate(900, 500, connected=False, inverter_dc_w=1030)
        self.assertEqual(estimated['island_w'], 1530)
        self.assertEqual(direct['island_w'], 1530)
        self.assertEqual(direct['method'], 'measured inverter DC')
        self.assertEqual(direct['admission_w'], 1560)

    def test_measured_dc_compensation_applied_exactly_once(self):
        model = DemandModel()
        for dc in (0, 500, 1000):
            estimated = model.estimate(900, dc, measured_dc=False)
            measured = model.estimate(900, dc, measured_dc=True)
            ea = model.allocate(estimated, 50, 100, 0, 200)
            ma = model.allocate(measured, 50, 100, 0, 200)
            self.assertAlmostEqual(ea['ccl_a'], ma['ccl_a'] + dc / 50)
            self.assertEqual(ea['support_w'], max(0, dc + 20 - 100))

    def test_invalid_pv_never_releases_shore_guard_and_current_cap_is_authoritative(self):
        model = DemandModel()
        demand = model.estimate(900, 1000)
        missing = model.allocate(demand, 50, None, -1000, 200)
        self.assertLess(missing['ccl_a'], 25)
        limited = model.allocate(demand, 50, 0, -1000, 5)
        self.assertEqual(limited['ccl_a'], 5)
        self.assertIn('REC', limited['limited_by'])
        self.assertEqual(model.allocate(demand, None, 100, 0, 200)['ccl_a'], 0)
        self.assertFalse(model.estimate(900, None)['valid'])
        self.assertFalse(model.estimate(900, 5001)['valid'])


class BusSnapshotTests(unittest.TestCase):
    def test_root_getvalue_relative_keys_and_empty_invalid_values(self):
        self.assertEqual(normalize_bus_snapshot({'Dc/0/Power': 0, 'Soc': [],
                                                'ActiveBmsService': 'rec'}),
                         {'/Dc/0/Power': 0, '/Soc': None, '/ActiveBmsService': 'rec'})

    def test_getitems_wrappers_and_invalid_responses(self):
        self.assertEqual(normalize_bus_snapshot({'/Dc/0/Power': {'Value': [], 'Text': '---'},
                                                '/Soc': {'Value': 80}}),
                         {'/Dc/0/Power': None, '/Soc': 80})
        self.assertEqual(normalize_bus_snapshot(None), {})


class BatterySelectionTests(unittest.TestCase):
    SERVICE = 'com.victronenergy.battery.recbms'
    STABLE = 'com.victronenergy.battery/200'

    def matches(self, **changes):
        observations = dict(active_battery_service=self.STABLE, active_bms_service=self.SERVICE,
                            battery_service=self.SERVICE, active_bms_instance=200)
        observations.update(changes)
        return selected_battery_matches(self.SERVICE, 200, **observations)

    def test_installed_stable_identity_and_direct_name_are_supported(self):
        self.assertTrue(self.matches())
        self.assertTrue(self.matches(active_battery_service=self.SERVICE))
        self.assertTrue(self.matches(active_battery_service=self.SERVICE,
                                     battery_service=None, active_bms_instance=None))

    def test_wrong_unknown_and_inconsistent_identity_is_rejected(self):
        changes = ({'active_battery_service': 'com.victronenergy.battery/201'},
                   {'active_battery_service': None}, {'active_bms_service': None},
                   {'battery_service': None}, {'active_bms_instance': None},
                   {'battery_service': 'com.victronenergy.battery.other'},
                   {'active_bms_service': 'com.victronenergy.battery.other'},
                   {'active_bms_instance': 201}, {'active_bms_instance': '200'},
                   {'active_bms_instance': float('nan')},
                   {'active_battery_service': self.SERVICE, 'active_bms_instance': 201})
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(self.matches(**change))
        self.assertFalse(selected_battery_matches(None, 200, self.STABLE, self.SERVICE,
                                                  self.SERVICE, 200))

    def test_each_stable_identity_observation_must_remain_fresh(self):
        observations = dict(active_battery_service=self.STABLE, active_bms_service=self.SERVICE,
                            battery_service=self.SERVICE, active_bms_instance=200)
        for stale in observations:
            registry = SourceRegistry(max_age_s=10)
            for path, value in observations.items():
                registry.observe('system', path, value, 0 if path == stale else 11)
            fresh = {path: registry.get('system', path, 11) for path in observations}
            with self.subTest(stale=stale):
                self.assertFalse(selected_battery_matches(self.SERVICE, 200, **fresh))


if __name__ == '__main__':
    unittest.main()
