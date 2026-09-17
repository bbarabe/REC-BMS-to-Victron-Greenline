#!/usr/bin/env python3
"""Physical relay acknowledgments are separate from charger settling.

Stage A adds the three facts the supervisor used to conflate: availability,
the Quattro's acknowledgment of the ignore command, and the accepted input.
"""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'dbus-recbms'))
from policy_contract import PolicyContract, TransferSupervisor, resolve_shore_input, prefer_renewable_wanted


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

    def test_ordinary_return_waits_for_the_exact_prepared_pair(self):
        # D02/A1: an islanded ordinary return used to be admitted on general
        # envelope safety, so the relay could close before the below-pack
        # command and the current brake were in force (E03).
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(False, 302, 1302)
        self.assertEqual(supervisor.state, 'ISLANDED')
        self.assertIsNone(self.step(supervisor, 303, 'connected', ready=False))
        self.assertEqual(supervisor.limited_by, 'preparing shore protection')
        self.assertIsNone(supervisor.prepared)
        self.assertEqual(supervisor.snapshot(310, 1310)['prepare_age_s'], 7)
        self.assertEqual(self.step(supervisor, 311, 'connected', ready=True), 0)
        self.assertTrue(supervisor.prepared)
        self.assertIsNone(supervisor.snapshot(311, 1311)['prepare_age_s'])
        self.assertEqual(supervisor.durable['fault_until'], 0)

    def test_bounded_preparation_returns_unprepared_rather_than_stalling(self):
        # A3: one bounded wait, then the return happens anyway. Not a fault:
        # the unverified pair is already reported by dbus-recbms' regulation
        # fault, and REC's own guards still decide the current.
        supervisor = self.connected(prepare_s=30.0)
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(False, 302, 1302)
        for now in (303, 320, 332):
            self.assertIsNone(self.step(supervisor, now, 'connected', ready=False))
            self.assertEqual(supervisor.limited_by, 'preparing shore protection')
        self.assertEqual(self.step(supervisor, 333, 'connected', ready=False), 0)
        self.assertIs(supervisor.prepared, False)
        self.assertEqual(supervisor.limited_by, 'unprepared return after 30s')
        self.assertEqual(supervisor.durable['fault_until'], 0)
        self.assertIsNone(supervisor.durable['last_fault'])

    def test_protective_and_island_intents_never_enter_the_preparation_wait(self):
        for intent, protective in (('protect', False), ('connected', True)):
            with self.subTest(intent=intent, protective=protective):
                supervisor = self.connected()
                self.assertEqual(self.step(supervisor, 300), 1)
                supervisor.observe(False, 302, 1302)
                self.assertEqual(supervisor.step(intent, 303, 1303, ready=False,
                                                 permitted=False, protective=protective), 0)
                self.assertIsNone(supervisor.prepare_since)

    def test_absent_shore_is_an_observation_not_a_refused_relay_write(self):
        # E13/D14: a connect command with persistently disconnected feedback
        # produced a 31 s timeout and a 3600 s lockout; absent shore looks
        # exactly like that and must not take one.
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(False, 302, 1302, available=False, ignore_state=1)
        self.assertEqual(self.step(supervisor, 303, 'connected'), 0)
        for now in (304, 320, 333):
            supervisor.observe(False, now, 1000 + now, available=False, ignore_state=0)
            self.step(supervisor, now, 'connected')
        self.assertEqual(supervisor.limited_by, 'shore unavailable')
        self.assertEqual(supervisor.durable['fault_until'], 0)
        self.assertIsNone(supervisor.durable['last_fault'])
        self.assertIs(supervisor.snapshot(333, 1333)['available'], False)
        # The tick the supply reappears the command is asserted again at once.
        supervisor.observe(False, 334, 1334, available=True, ignore_state=0)
        self.assertEqual(self.step(supervisor, 334, 'connected'), 0)

    def test_feedback_timeout_names_the_stage_that_actually_failed(self):
        # SP63: absent supply, an unacknowledged command and an acknowledged
        # command whose input never arrives are three different diagnoses.
        for ignore_state, reason in ((None, 'relay feedback timeout'),
                                     (0, 'relay command not acknowledged'),
                                     (1, 'relay command acknowledged, AC input not accepted')):
            with self.subTest(ignore_state=ignore_state):
                supervisor = self.connected()
                self.assertEqual(self.step(supervisor, 300), 1)
                supervisor.observe(True, 320, 1320, available=True, ignore_state=ignore_state)
                self.assertEqual(self.step(supervisor, 331), 0)
                self.assertEqual(supervisor.durable['last_fault']['reason'], reason)

    def test_another_accepted_input_is_not_an_island_and_blocks_departure(self):
        # SP56: "not AC1" alone is not proof of inverter-only operation.
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(False, 302, 1302, available=True, ignore_state=1)
        self.assertEqual(supervisor.state, 'ISLANDED')
        supervisor.observe(False, 303, 1303, available=True, ignore_state=1, active_input=1)
        self.assertNotEqual(supervisor.state, 'ISLANDED')
        self.assertEqual(supervisor.departure_reason(304, 1304), 'another AC input accepted')
        self.assertIsNone(self.step(supervisor, 305))
        snapshot = supervisor.snapshot(305, 1305)
        self.assertEqual(snapshot['active_input'], 1)
        self.assertIs(snapshot['connected'], False)

    def test_command_acknowledgment_is_distinct_from_input_acceptance(self):
        # SP62: the command, its acknowledgment and the physical transfer get
        # their own timestamps; only the last one proves a transfer happened.
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(True, 301, 1301, available=True, ignore_state=0, active_input=0)
        self.assertFalse(supervisor.snapshot(301, 1301)['acknowledged'])
        supervisor.observe(True, 302, 1302, available=True, ignore_state=1, active_input=0)
        snapshot = supervisor.snapshot(302, 1302)
        self.assertTrue(snapshot['acknowledged'])
        self.assertEqual(snapshot['acknowledged_at'], 302)
        self.assertEqual(snapshot['command_at'], 300)
        self.assertIsNone(snapshot['accepted_at'])
        self.assertIsNone(snapshot['transition_age_s'])
        supervisor.observe(False, 303, 1303, available=True, ignore_state=1)
        snapshot = supervisor.snapshot(304, 1304)
        self.assertEqual(snapshot['accepted_at'], 303)
        self.assertEqual(snapshot['transition_age_s'], 1)
        self.assertEqual(supervisor.durable['last_departure_s'], 303)

    def test_old_state_files_load_without_the_stage_a_keys(self):
        supervisor = TransferSupervisor({'departures': [10.0], 'edges': [], 'fault_until': 0.0,
                                         'backoff_until': 0.0, 'failures': 2,
                                         'external_edges': 1, 'logical_s': 20.0,
                                         'last_wall_s': 1000.0, 'last_fault': None})
        self.assertEqual(supervisor.durable['probe_failures'], [])
        self.assertIsNone(supervisor.durable['last_departure_s'])
        self.assertIsNone(supervisor.durable['last_failed_departure'])
        self.assertEqual(supervisor.snapshot(0, 1000)['departures_24h'], 1)

    def test_a_command_with_no_vebus_service_is_deferred_not_faulted(self):
        # Boat, 2026-09-15 19:19-19:55 UTC: the VE.Bus service restarted three
        # times while the owner moved shore to AC in 2; each connect issued
        # into the gap was booked as a refused write and an hour's fault
        # lockout, so a boat that had done nothing wrong could not leave shore
        # until 20:55 under a 75 V sky. The write never happened: undo the
        # issue, hand a reserved departure back, assert again next tick.
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        self.assertEqual(len(supervisor.durable['departures']), 1)
        supervisor.command_deferred(1, 300)
        self.assertIsNone(supervisor.pending)
        self.assertEqual(supervisor.durable['departures'], [])
        self.assertEqual(supervisor.durable['fault_until'], 0.0)
        self.assertNotIn('lockout', supervisor.departure_reason(301, 1301))
        self.assertEqual(supervisor.limited_by, 'VE.Bus service absent')
        # The next tick asserts again, without the 30 s re-assert throttle.
        self.assertEqual(self.step(supervisor, 301), 1)
        # A deferred connect behaves the same way: no fault, re-asserted.
        supervisor = self.connected()
        supervisor.observe(False, 10, 1010)
        self.assertEqual(self.step(supervisor, 10, intent='connected', protective=True), 0)
        supervisor.command_deferred(0, 10)
        self.assertEqual(supervisor.durable['fault_until'], 0.0)
        self.assertEqual(self.step(supervisor, 11, intent='connected', protective=True), 0)

    def test_withdrawn_return_leaves_an_island_not_a_running_timer(self):
        # Boat, 2026-09-14 16:06 UTC: a one-tick loss of permission began a
        # prepared return, permission came back, the island intent resumed,
        # but the supervisor stayed PREPARE_CONNECT with its preparation
        # timer running and closed the relay unprepared 33 s later.
        supervisor = self.connected()
        self.assertEqual(self.step(supervisor, 300), 1)
        supervisor.observe(False, 303, 1303)
        self.assertEqual(supervisor.state, 'ISLANDED')
        self.assertIsNone(self.step(supervisor, 310, 'connected', ready=False))
        self.assertEqual(supervisor.state, 'PREPARE_CONNECT')
        self.assertEqual(supervisor.limited_by, 'preparing shore protection')
        for now in (312, 330, 345):
            self.assertIsNone(self.step(supervisor, now, 'island'))
            self.assertEqual(supervisor.state, 'ISLANDED')
            self.assertIsNone(supervisor.prepare_since)
        self.assertIsNone(self.step(supervisor, 346, 'connected', ready=False))
        self.assertEqual(supervisor.prepare_since, 346)
        self.assertIsNone(self.step(supervisor, 360, 'connected', ready=False))
        self.assertEqual(self.step(supervisor, 361, 'connected', ready=True), 0)
        self.assertTrue(supervisor.prepared)

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


class ShoreInputResolverTests(unittest.TestCase):
    """Which Quattro AC input carries shore power (owner, 2026-09-15: shore
    moves from AC in 1 to AC in 2, so nothing may hard-code the input)."""

    def test_a_pinned_input_wins_over_everything(self):
        self.assertEqual(resolve_shore_input(2, (3, 0), 0, (1, 0), 1), (2, 'configured'))
        self.assertEqual(resolve_shore_input(1, (0, 3), 1, (0, 1), 2), (1, 'configured'))

    def test_the_gx_input_types_decide_when_exactly_one_is_grid_or_shore(self):
        self.assertEqual(resolve_shore_input('auto', (0, 3), 240, (None, None), None), (2, 'gx input type'))
        self.assertEqual(resolve_shore_input('auto', (3, 0), 0, (1, 0), None), (1, 'gx input type'))
        self.assertEqual(resolve_shore_input('auto', (2, 1), 0, (1, 1), None), (2, 'gx input type'))   # generator + grid
        # ... and they override an input already settled on: the rewire.
        self.assertEqual(resolve_shore_input('auto', (0, 3), 0, (1, 0), 1), (2, 'gx input type'))

    def test_a_settled_input_is_kept_on_ambiguous_evidence(self):
        # An island reads ActiveInput 240 and no availability: keep it.
        self.assertEqual(resolve_shore_input('auto', (None, None), 240, (0, 0), 2), (2, 'kept'))
        self.assertEqual(resolve_shore_input('auto', (0, 0), 240, (None, None), 1), (1, 'kept'))
        # Both inputs typed shore, or neither: the settled input stands.
        self.assertEqual(resolve_shore_input('auto', (3, 3), 1, (1, 1), 1), (1, 'kept'))

    def test_a_fresh_start_reads_the_quattro(self):
        self.assertEqual(resolve_shore_input('auto', (0, 0), 1, (0, 1), None), (2, 'accepted input'))
        self.assertEqual(resolve_shore_input('auto', (None, None), 0, (1, 0), None), (1, 'accepted input'))
        self.assertEqual(resolve_shore_input('auto', (0, 0), 240, (0, 1), None), (2, 'only input available'))
        self.assertEqual(resolve_shore_input('auto', (0, 0), 240, (1, 1), None), (1, 'default'))
        self.assertEqual(resolve_shore_input('auto', (None, None), None, (None, None), None), (1, 'default'))


class PreferRenewableTests(unittest.TestCase):
    """The Quattro's prefer-renewable toggle, once a day (owner, 2026-09-15)."""

    def test_off_and_discharge(self):
        self.assertEqual(prefer_renewable_wanted('OFF', True, 50.0, 60.0, 1.0, None), (None, 'solar priority off: left alone'))
        self.assertEqual(prefer_renewable_wanted('DISCHARGE', False, 40.0, 30.0, 1.0, 0)[0], 1)
        self.assertEqual(prefer_renewable_wanted('DISCHARGE', True, 40.0, 30.0, 1.0, None)[0], 1)

    def test_day_and_night(self):
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 50.0, 50.0, 1.0, None), (1, 'day: prefer solar'))
        self.assertEqual(prefer_renewable_wanted('CHARGE', False, 50.0, 50.0, 1.0, 1), (0, 'night: charge now'))
        self.assertEqual(prefer_renewable_wanted('HOLD', None, 50.0, 50.0, 1.0, 1)[0], 1)   # unknown: keep
        self.assertEqual(prefer_renewable_wanted('HOLD', None, 50.0, 50.0, 1.0, None)[0], None)

    def test_a_dark_day_charges_once_the_bank_is_a_point_under_and_holds_until_back(self):
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 49.2, 50.0, 1.0, 1)[0], 1)   # within a point: solar
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 48.9, 50.0, 1.0, 1)[0], 0)   # a point under: charge
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 49.5, 50.0, 1.0, 0, 0)[0], 0)   # recovering: still charge
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 50.0, 50.0, 1.0, 0, 0)[0], 1)   # back inside: solar
        self.assertEqual(prefer_renewable_wanted('CHARGE', True, 58.9, 60.0, 1.0, 1)[0], 0)
        self.assertEqual(prefer_renewable_wanted('CHARGE', True, None, 60.0, 1.0, 1)[0], 1) # no SOC: no deficit call

    def test_the_nights_charge_now_does_not_leak_into_the_morning(self):
        # 2026-09-17 dawn: 49.8 % under a 50 % hold, last decision the night's 0.
        # Only a day-made 0 is a deficit to recover from.
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 49.8, 50.0, 1.0, 0, 1), (1, 'day: prefer solar'))
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 49.8, 50.0, 1.0, 0, None), (1, 'day: prefer solar'))
        # A dark day that ended in deficit still recovers the next morning.
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 49.8, 50.0, 1.0, 0, 0)[0], 0)
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 50.0, 50.0, 1.0, 0, 0)[0], 1)
        # A real deficit by day still charges, whatever the night said.
        self.assertEqual(prefer_renewable_wanted('HOLD', True, 48.9, 50.0, 1.0, 0, 1)[0], 0)
        # Unknown daylight still keeps the last decision of any kind.
        self.assertEqual(prefer_renewable_wanted('HOLD', None, 49.8, 50.0, 1.0, 0, 1)[0], 0)


if __name__ == '__main__':
    unittest.main()
