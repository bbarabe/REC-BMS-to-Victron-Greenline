"""Scalar publication mirrors authoritative documents and clears stale values."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'dbus-recbms'))
from control_inputs import DemandModel, SourceRegistry
from energy_accounting import EnergyLedger
from policy_telemetry import FIELDS, TOTAL_FIELDS, PREFIX, PolicyTelemetry


class Service(dict):
    def add_path(self, path, value):
        if path in self:
            raise AssertionError('duplicate telemetry path: ' + path)
        self[path] = value


class PolicyTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.telemetry = PolicyTelemetry(('system', 'vebus', 'solar278', 'solar279'))
        self.telemetry.register(self.service)
        ledger = EnergyLedger()
        ledger.set_policy('HOLD', 80, soc=80)
        ledger.sample(0, 1000000, 1, 60, solar_surplus_w=60)
        ledger.sample(10, 1000010, 1, 60, solar_surplus_w=60)
        ledger.sample(20, 1000020, -1, 60, solar_surplus_w=0)
        self.ledger = ledger.snapshot()
        self.status = {
            'ready': True, 'lease_valid': True, 'lease_remaining_s': 80,
            'accepted_id': 42, 'rejection': '', 'limited_by': 'REC current limit',
            'limits': {'quattro_v': 57, 'solar_v': 57.3, 'ccl_a': 2},
            'transfer': {'state': 'ISLANDED', 'connected': False, 'pending': None,
                         'departures_24h': 2, 'actual_edges_24h': 3, 'external_edges': 1,
                         'next_departure_s': 300, 'limited_by': ''},
        }
        self.snapshot = {
            'configuration_id': 'test-configuration', 'valid': True,
            'voltage': 60, 'current': -1, 'soc': 80,
            'control': {'mode': 'HOLD', 'protect': False, 'overhead_category': 'buffer',
                        'buffer_floor_soc': 79, 'buffer_ceiling_soc': 80, 'buffer_remaining_wh': .1,
                        'maintenance': {'status': 'deferred', 'reason': 'balance_uncalibrated'}},
            'demand': DemandModel().estimate(500, 60),
            'ledger': self.ledger,
            'maintenance': {'balance_calibrated': False, 'operating_conditions_valid': False,
                            'cells_balanced': False, 'persistent_imbalance': False,
                            'candidate_active': False, 'attempt_outcome': 'interrupted',
                            'last_attempt': 1000020, 'last_qualified_lift': 999000,
                            'last_balance_opportunity': None, 'last_balance_satisfied': None},
        }

    def value(self, suffix):
        return self.service[PREFIX + suffix]

    def test_all_energy_and_budget_scalars_match_ledger_without_netting(self):
        self.telemetry.publish(self.service, self.status, self.snapshot, ledger=self.ledger)
        for suffix, _, keys in TOTAL_FIELDS:
            self.assertEqual(self.value(suffix), self.ledger[keys[0]][keys[1]])
        self.assertGreater(self.value('Ledger/Total/ChargeWh'), 0)
        self.assertGreater(self.value('Ledger/Total/DischargeWh'), 0)
        self.assertEqual(self.value('Ledger/NetWh'), self.ledger['net_wh'])
        self.assertEqual(self.value('References/HighWh'), self.ledger['references']['high_wh'])
        self.assertEqual(self.value('Buffer/CreditWh'), self.ledger['buffer']['credit_wh'])
        self.assertEqual(self.value('Recovery/ChargeDebtAh'), self.ledger['recovery']['charge_ah'])
        self.assertEqual(self.value('Budget24h/RemainingEfc'), self.ledger['budget']['remaining_efc'])
        self.assertEqual(self.value('Budget24h/ReverseRemainingWh'), self.ledger['budget']['reverse_remaining_wh'])

    def test_command_readiness_and_transfer_keep_their_distinct_meanings(self):
        self.telemetry.publish(self.service, self.status, self.snapshot)
        self.assertEqual(self.value('Mode'), 'HOLD')
        self.assertEqual(self.value('Transport'), 'ISLANDED')
        self.assertEqual(self.value('Ready'), 1)
        self.assertEqual(self.value('Connected'), 0)
        self.assertEqual(self.value('LimitedBy'), 'REC current limit')
        self.assertEqual(self.value('Relay/ActualEdges24h'), 3)
        self.assertEqual(self.value('Relay/Departures24h'), 2)

    def test_demand_uncertainty_and_measured_service_do_not_claim_spare_pv(self):
        evidence = {'observed_w': 420, 'capacity_lower_bound_w': 420,
                    'load_service_verified': True, 'confidence': 'load service verified; headroom unknown',
                    'required_dc_w': 450}
        self.telemetry.publish(self.service, self.status, self.snapshot, solar_evidence=evidence)
        self.assertEqual(self.value('Demand/Method'), 'AC conversion model')
        self.assertEqual(self.value('Demand/ExternalMeasured'), 0)
        self.assertEqual(self.value('Demand/UncertaintyW'), self.snapshot['demand']['uncertainty_w'])
        self.assertEqual(self.value('Solar/CapacityLowerBoundW'), 420)
        self.assertEqual(self.value('Solar/LoadServiceVerified'), 1)
        self.assertIn('unknown', self.value('Solar/Confidence'))

    def test_constant_verified_input_age_is_distinct_from_value_change_age(self):
        sources = SourceRegistry(max_age_s=20)
        names = {'system': 'system.service'}
        sources.observe('system.service', '/Dc/System/Power', 60, 0)
        sources.observe('system.service', '/Dc/System/Power', 60, 15)
        self.telemetry.publish(self.service, self.status, self.snapshot,
                               sources=sources, source_names=names, now=20)
        self.assertEqual(self.value('Inputs/system/DcLoadW/Valid'), 1)
        self.assertEqual(self.value('Inputs/system/DcLoadW/AgeSeconds'), 5)
        self.assertEqual(self.value('Inputs/system/DcLoadW/ChangedAgeSeconds'), 20)
        sources.observe('system.service', '/Dc/System/Power', None, 21)
        self.telemetry.publish(self.service, self.status, self.snapshot,
                               sources=sources, source_names=names, now=21)
        self.assertEqual(self.value('Inputs/system/DcLoadW/Valid'), 0)
        sources.observe('system.service', '/Dc/System/Power', 60, 22)
        self.telemetry.publish(self.service, self.status, self.snapshot,
                               sources=sources, source_names=names, now=43)
        self.assertEqual(self.value('Inputs/system/DcLoadW/Valid'), 0)
        self.assertEqual(self.value('Inputs/system/DcLoadW/AgeSeconds'), 21)

    def test_removed_sources_and_missing_documents_clear_old_scalar_values(self):
        sources = SourceRegistry()
        sources.observe('old', '/Yield/Power', 420, 0)
        self.telemetry.publish(self.service, self.status, self.snapshot,
                               sources=sources, source_names={'solar278': 'old'}, now=1)
        self.assertEqual(self.value('Inputs/solar278/PowerW/Valid'), 1)
        self.telemetry.publish(self.service, {}, {}, sources=sources, source_names={}, now=2)
        self.assertIsNone(self.value('Mode'))
        self.assertIsNone(self.value('Demand/IslandW'))
        self.assertIsNone(self.value('Ledger/Total/ChargeWh'))
        self.assertIsNone(self.value('Inputs/solar278/Service'))
        self.assertIsNone(self.value('Inputs/solar278/PowerW/Valid'))

    def test_publication_does_not_mutate_authoritative_documents_or_json_paths(self):
        status, snapshot = copy.deepcopy(self.status), copy.deepcopy(self.snapshot)
        self.service['/RecBms/Policy/Status'] = 'atomic status'
        self.service['/RecBms/Policy/Snapshot'] = 'atomic snapshot'
        self.telemetry.publish(self.service, status, snapshot)
        self.assertEqual(status, self.status)
        self.assertEqual(snapshot, self.snapshot)
        self.assertEqual(self.service['/RecBms/Policy/Status'], 'atomic status')
        self.assertEqual(self.service['/RecBms/Policy/Snapshot'], 'atomic snapshot')
        self.assertEqual(len(set(suffix for suffix, _, _ in FIELDS + TOTAL_FIELDS)), len(FIELDS + TOTAL_FIELDS))


if __name__ == '__main__':
    unittest.main()
