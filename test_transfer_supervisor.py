#!/usr/bin/env python3
"""Physical relay acknowledgments are separate from charger settling."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'dbus-recbms'))
from policy_contract import PolicyContract, TransferSupervisor


class TransferBoundaryTests(unittest.TestCase):
    def connected(self, **kwargs):
        supervisor = TransferSupervisor(**kwargs)
        supervisor.observe(True, 0, 1000)
        return supervisor

    def step(self, supervisor, now, intent='island', **kwargs):
        return supervisor.step(intent, now, 1000 + now,
                               ready=kwargs.get('ready', True),
                               permitted=kwargs.get('permitted', True),
                               protective=kwargs.get('protective', False))

    def test_seventeen_second_physical_feedback_does_not_fault(self):
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        self.assertIsNone(self.step(supervisor, 316))
        supervisor.observe(False, 317, 1317)
        self.assertIsNone(self.step(supervisor, 317))
        self.assertEqual(supervisor.state, 'ISLANDED')
        self.assertEqual(supervisor.durable['fault_until'], 0)
        self.assertEqual(len(supervisor.durable['departures']), 1)

    def test_timeout_records_actual_pending_command_and_returns_immediately(self):
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        self.assertEqual(self.step(supervisor, 331), 0)
        fault = supervisor.snapshot(331, 1331)['last_fault']
        self.assertEqual(fault['reason'], 'relay feedback timeout')
        self.assertEqual(fault['command'], 1)
        self.assertEqual(fault['pending_age_s'], 31)
        self.assertTrue(fault['feedback_connected'])
        self.assertIsNone(self.step(supervisor, 332, 'protect'))

    def test_confirmed_protection_is_not_reissued_every_tick(self):
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 1, 'protect', ready=False), 0)
        supervisor.command_result(0, 0, 1, 1001)
        supervisor.observe(True, 2, 1002)
        for now in (2, 3, 30, 100):
            self.assertIsNone(self.step(supervisor, now, 'protect', ready=False))
        self.assertEqual(supervisor.durable['edges'], [])

    def test_pending_departure_cancel_is_immediate_even_during_unsettled_output(self):
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        self.assertEqual(self.step(supervisor, 301, 'protect', ready=False, permitted=False), 0)
        self.assertIsNone(self.step(supervisor, 302, 'protect', ready=False, permitted=False))
        self.assertEqual(len(supervisor.durable['departures']), 1)

    def test_refused_or_timed_out_protective_command_retries(self):
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 1, 'protect'), 0)
        supervisor.command_result(0, 1, 1, 1001)
        self.assertEqual(self.step(supervisor, 2, 'protect'), 0)
        self.assertEqual(self.step(supervisor, 33, 'protect'), 0)
        self.assertEqual(supervisor.snapshot(33, 1033)['last_fault']['command'], 0)

    def test_island_protection_does_not_wait_for_charger_settling(self):
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(False, 317, 1317)
        self.assertEqual(self.step(supervisor, 318, ready=False, protective=True), 0)

    def test_recovered_read_outage_retains_fault_lockout(self):
        supervisor = self.connected()
        supervisor.observe(None, 1, 1001)
        self.step(supervisor, 1, 'protect')
        self.step(supervisor, 32, 'protect')
        supervisor.observe(True, 33, 1033)
        self.assertEqual(supervisor.departure_reason(334, 1334), 'transfer fault lockout')
        self.assertGreater(supervisor.durable['fault_until'], supervisor.durable['logical_s'])

    def test_target_change_revokes_request_even_when_policy_mode_is_unchanged(self):
        contract = PolicyContract(generation='test')
        request = {'version': 2, 'generation': 'test', 'request_id': 1,
                   'mode': 'CHARGE', 'target_soc': 60, 'transfer_intent': 'island',
                   'requested_limits': {'sustain': 0}, 'lease_s': 120}
        self.assertTrue(contract.accept(request, 0, 60, consumer_ready=True))
        self.assertTrue(contract.active(1, target_soc=60))
        self.assertFalse(contract.active(1, target_soc=40))
        request.update(request_id=2, target_soc=40)
        self.assertTrue(contract.accept(request, 2, 40))
        self.assertTrue(contract.active(2, target_soc=40))


if __name__ == '__main__':
    unittest.main()
