"""REC actuation boundaries for leased legacy-engine intentions."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'dbus-recbms'))
from policy_contract import PolicyContract, VERSION
from rec_policy_adapter import RecPolicyAdapter


class IntentLeaseTests(unittest.TestCase):
    def test_restart_reconstructs_floor_only_for_matching_target_and_fresh_shore(self):
        # Stage B: a lost lease under HOLD reconstructs the two-sided hold
        # (3) the way CHARGE reconstructs its floor -- on the same accepted
        # input, for the same destination. Releasing it instead would put
        # the slider curve back with the standing lead under it (E04/D03).
        for mode, feed, target, expected in (
                ('CHARGE', 0, 80., 1), ('CHARGE', 240, 80., 0),
                ('CHARGE', None, 80., 0), ('CHARGE', 0, 60., 0),
                ('HOLD', 0, 80., 3), ('HOLD', 240, 80., 0),
                ('HOLD', 0, 60., 0),
                ('DISCHARGE', 240, 80., 2)):
            with self.subTest(mode=mode, feed=feed, target=target):
                calls = []
                adapter = object.__new__(RecPolicyAdapter)
                adapter.driver = SimpleNamespace(sustain={'active': False, 'mode': 0},
                    boost={'active': False}, cfg=SimpleNamespace(policy_ac_input=1),
                    _set_sustain=calls.append, _set_boost=lambda value: None)
                adapter.clock = SimpleNamespace(monotonic=lambda: 100.)
                adapter.contract = PolicyContract({'owned': True,
                    'policy': {'mode': mode, 'target_soc': 80.}})
                adapter._value = lambda *args: feed
                adapter.prepare_intents(target)
                self.assertEqual(calls, [expected])

    def test_lost_owner_operator_actions(self):
        # Stage C / master D13: a retained hold is released once the target
        # moves during the loss and the bank is back on an accepted input; a
        # return still in progress keeps it. OFF keeps a hold through the
        # protected return and releases it once accepted.
        def adapter_with(hold_mode, connected, lease=None):
            calls = []
            adapter = object.__new__(RecPolicyAdapter)
            adapter.driver = SimpleNamespace(sustain={'active': True, 'mode': hold_mode},
                boost={'active': False}, cfg=SimpleNamespace(policy_ac_input=1),
                _set_sustain=calls.append, _set_boost=lambda value: None)
            adapter.clock = SimpleNamespace(monotonic=lambda: 100.)
            adapter.transfer = SimpleNamespace(feedback=connected, available=True, limited_by='')
            adapter.contract = PolicyContract({'owned': True,
                'policy': {'mode': 'CHARGE', 'target_soc': 80.}}, generation='g')
            if lease:
                self.assertTrue(adapter.contract.accept(dict(version=VERSION, generation='g',
                    request_id=1, mode=lease, target_soc=80., transfer_intent='connected',
                    requested_limits={'sustain': 0, 'purpose': ''}, lease_s=15.), 100., 80.,
                    consumer_ready=True))
            adapter._value = lambda *args: 0 if connected else 240
            return adapter, calls
        # target moved during the loss, on shore: released
        adapter, calls = adapter_with(1, True)
        adapter.prepare_intents(70.)
        self.assertEqual(calls, [0])
        # target moved during the loss, still returning: kept
        adapter, calls = adapter_with(1, False)
        adapter.prepare_intents(70.)
        self.assertEqual(calls, [1])
        # same target during the loss: kept
        adapter, calls = adapter_with(3, True)
        adapter.prepare_intents(80.)
        self.assertEqual(calls, [3])
        # OFF while returning keeps the hold; OFF on shore releases it
        adapter, calls = adapter_with(1, False, lease='OFF')
        adapter.prepare_intents(80.)
        self.assertEqual(calls, [1])
        adapter, calls = adapter_with(1, True, lease='OFF')
        adapter.prepare_intents(80.)
        self.assertEqual(calls, [0])

    def test_heartbeat_does_not_extend_boost_or_reanchor_floor(self):
        now = [100.0]
        calls = []
        driver = SimpleNamespace(sustain={'mode': 1, 'active': True}, boost={'active': True},
            _set_sustain=lambda value: calls.append(('sustain', value)),
            _set_boost=lambda value: calls.append(('boost', value)))
        adapter = object.__new__(RecPolicyAdapter)
        adapter.driver = driver
        adapter.clock = SimpleNamespace(monotonic=lambda: now[0])
        adapter.contract = PolicyContract(generation='generation')
        adapter.last_boost_id = None
        def accept(rid, limits):
            self.assertTrue(adapter.contract.accept(dict(version=VERSION, generation='generation',
                request_id=rid, mode='CHARGE', target_soc=80., transfer_intent='connected',
                requested_limits=limits, lease_s=15.), now[0], 80., consumer_ready=True))
        accept(1, {'sustain': 1, 'boost_v': .3})
        adapter.prepare_intents(80.)
        adapter.prepare_intents(80.)
        now[0] += 5
        accept(2, {'sustain': 1})
        adapter.prepare_intents(80.)
        self.assertEqual([value for name, value in calls if name == 'boost'], [.3])
        self.assertEqual([value for name, value in calls if name == 'sustain'], [1, 1, 1])
        now[0] += 16
        adapter.prepare_intents(80.)
        self.assertEqual(calls[-1], ('boost', 0.))
        self.assertIn(('sustain', 1), calls[-2:])


class IslandEnvelopeTests(unittest.TestCase):
    def test_missing_array_does_not_hide_unsafe_limit_or_authorize_an_increase(self):
        from solar_priority_plant import ARRAYS, CoupledSimulation
        with CoupledSimulation() as sim:
            for _ in range(1800):
                sim.run(1)
                if not sim.plant.connected:
                    break
            self.assertFalse(sim.plant.connected)
            sim.run(30)
            driver = sim.rec
            safe = driver._safe_voltage()
            original = driver._read_number
            def missing_one(name, path):
                if name == ARRAYS[0]:
                    return None
                return original(name, path)
            with patch.object(driver, '_read_number', side_effect=missing_one):
                self.assertTrue(driver._voltage_within_envelope(safe))
                with patch.object(driver.voltage_control, 'requested_solar', safe + .1):
                    self.assertFalse(driver._voltage_within_envelope(safe))
                with patch.object(driver, '_last_verified_voltage', None):
                    self.assertFalse(driver._voltage_within_envelope(safe))
            def unsafe_other(name, path):
                if name == ARRAYS[0]:
                    return None
                if name == ARRAYS[1] and path == '/Link/ChargeVoltage':
                    return safe + 1
                return original(name, path)
            with patch.object(driver, '_read_number', side_effect=unsafe_other):
                self.assertFalse(driver._voltage_within_envelope(safe))


if __name__ == '__main__':
    unittest.main()
