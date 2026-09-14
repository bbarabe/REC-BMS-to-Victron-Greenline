"""Acceptance of restored decisions through the retained production boundaries.

The coupled plant is synthetic; these checks are not an on-boat calibration.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'dbus-recbms'))
from energy_accounting import EnergyLedger
from policy_contract import PolicyContract, TransferSupervisor, VERSION


def request(contract, rid=1, intent='connected', sustain=0):
    return dict(version=VERSION, generation=contract.generation, request_id=rid,
                mode='CHARGE', target_soc=80., transfer_intent=intent,
                requested_limits={'boost_v': 0., 'purpose': '', 'sustain': sustain},
                lease_s=30.)


class RestoredBoundaryTests(unittest.TestCase):
    def test_new_generation_revokes_old_request_even_with_durable_ownership(self):
        old = PolicyContract(generation='old')
        self.assertTrue(old.accept(request(old), 100, 80, consumer_ready=True))
        new = PolicyContract(copy.deepcopy(old.state), generation='new')
        self.assertFalse(new.active(101))
        self.assertFalse(new.accept(request(old, 2, 'island'), 101, 80, consumer_ready=True))
        self.assertTrue(new.accept(request(new, 3), 102, 80, consumer_ready=True))
        self.assertFalse(new.active(132))

    def test_invalid_floor_and_boost_cannot_replace_valid_lease(self):
        contract = PolicyContract(generation='test')
        self.assertTrue(contract.accept(request(contract), 100, 80, consumer_ready=True))
        for limits in ({'sustain': 3, 'boost_v': 0, 'purpose': ''},
                       {'sustain': 1, 'boost_v': .31, 'purpose': ''}):
            candidate = request(contract, 2)
            candidate['requested_limits'] = limits
            self.assertFalse(contract.accept(candidate, 101, 80))
            self.assertEqual(contract.accepted_id, 1)

    def test_missing_feedback_aborts_pending_departure_without_spending_another_edge(self):
        relay = TransferSupervisor()
        relay.observe(True, 0, 1000)
        self.assertEqual(relay.step('island', 300, 1300, True, True), 1)
        relay.observe(None, 301, 1301)
        self.assertEqual(relay.step('island', 301, 1301, True, True), 0)
        self.assertEqual(relay.snapshot(301, 1301)['departures_24h'], 1)

    def test_compact_snapshot_never_copies_old_trial_journals(self):
        class DoNotTraverse(list):
            def __deepcopy__(self, memo):
                raise AssertionError('Control snapshot traversed historical trial journal')
        ledger = EnergyLedger()
        ledger.controller_state['supervised_trial_history'] = DoNotTraverse(
            [{'trace': [{'battery_w': 100.0}] * 360}] * 10000)
        before = ledger.snapshot(compact=True)
        for _ in range(100):
            snapshot = ledger.snapshot(compact=True)
            self.assertNotIn('controller_state', snapshot)
            self.assertNotIn('buckets', snapshot)
            self.assertEqual(snapshot['total'], before['total'])
        snapshot['total']['charge_wh'] = 999
        self.assertNotEqual(ledger.snapshot(compact=True)['total']['charge_wh'], 999)


class RestoredPlantTests(unittest.TestCase):
    def simulation(self, **kwargs):
        from solar_priority_plant import CoupledSimulation
        return CoupledSimulation(**kwargs)

    def until(self, sim, predicate, timeout=1200):
        for _ in range(timeout):
            if predicate():
                return
            sim.run(1)
        self.fail('No required transition: ' + repr(sim.trace[-1]))

    def test_uncertain_history_allows_sunny_departure_and_cloud_return(self):
        with self.simulation() as sim:
            ledger = sim.rec.policy_adapter.ledger
            ledger._state['uncertain_until'] = 86400
            ledger._state['uncertainty_reason'] = 'historical unavailable interval'
            self.until(sim, lambda: not sim.plant.connected)
            self.assertTrue(ledger.snapshot(compact=True)['budget']['uncertain'])
            sim.set_sun([0, 0])
            self.until(sim, lambda: sim.plant.connected)
            self.assertTrue(ledger.snapshot(compact=True)['budget']['uncertain'])

    def test_missing_mppt_report_does_not_instantly_return_healthy_island(self):
        from solar_priority_plant import ARRAYS
        with self.simulation() as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.run(100)
            sim.bus.unresponsive.update(ARRAYS)
            sim.run(30)
            self.assertGreater(sim.plant.battery_w, 0)
            self.assertFalse(sim.plant.connected)

    def test_known_unsafe_mppt_voltage_forces_return(self):
        from solar_priority_plant import ARRAYS
        with self.simulation() as sim:
            self.until(sim, lambda: not sim.plant.connected)
            original_read = sim.bus.read
            def read(service, path):
                if service in ARRAYS and path == '/Link/ChargeVoltage':
                    return 99.0
                return original_read(service, path)
            sim.bus.read = read
            sim.run(10)
            self.assertTrue(sim.plant.connected)

    def test_restart_with_missing_mppt_cannot_reuse_pre_restart_voltage_proof(self):
        from solar_priority_plant import ARRAYS, CCL
        with self.simulation() as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.bus.unresponsive.update(ARRAYS)
            sim.restart_rec()
            sim.run(10)
            self.assertTrue(sim.plant.connected)
            self.assertEqual(sim.rec.batt[CCL], 0)

    def test_missing_rec_voltage_forces_return_and_zero_charge_permission(self):
        from solar_priority_plant import CCL
        with self.simulation() as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.missing_can.add(0x356)
            sim.run(sim.rec_cfg.live_timeout + 5)
            self.assertTrue(sim.plant.connected)
            self.assertEqual(sim.rec.batt[CCL], 0)

    def test_raw_charge_prohibition_survives_full_charge_target(self):
        from solar_priority_plant import CCL
        with self.simulation(target=100) as sim:
            sim.run(60)
            sim.plant.rec_ccl = 0
            sim.run(5)
            self.assertEqual(sim.rec.batt[CCL], 0)

    def test_wrong_selected_battery_prevents_departure(self):
        with self.simulation() as sim:
            sim.system['/ActiveBmsService'] = 'com.victronenergy.battery.other'
            sim.run(400)
            self.assertTrue(sim.plant.connected)
            self.assertFalse(sim.plant.relay_edges)

    def test_excess_current_readback_prevents_new_departure(self):
        from solar_priority_plant import VEBUS
        with self.simulation() as sim:
            original_read = sim.bus.read
            def read(service, path):
                if service == VEBUS and path == '/BatteryOperationalLimits/MaxChargeCurrent':
                    return 1000
                return original_read(service, path)
            sim.bus.read = read
            sim.run(400)
            self.assertTrue(sim.plant.connected)
            self.assertFalse(sim.plant.relay_edges)

    def test_night_below_target_uses_floor_and_stays_connected(self):
        with self.simulation(target=80) as sim:
            sim.set_sun([0, 0])
            sim.run(100)
            self.assertTrue(sim.plant.connected)
            self.assertEqual(sim.solar.last_request['requested_limits']['sustain'], 1)

    def test_full_target_releases_floor_and_ceiling(self):
        with self.simulation(target=100) as sim:
            sim.run(100)
            self.assertEqual(sim.solar.last_request['requested_limits']['sustain'], 0)

    def test_missing_quattro_current_on_shore_keeps_charge_and_its_floor(self):
        # E02: night CHARGE at 80 %; losing Quattro DC current used to produce a
        # fresh HOLD/release lease, 59.34 V / 200 A and 4.94 kW into the bank.
        from solar_priority_plant import BASE, CCL, VEBUS
        with self.simulation(target=80) as sim:
            sim.set_sun([0, 0])
            sim.run(100)
            self.assertEqual(sim.solar.last_request['mode'], 'CHARGE')
            sim.bus.invalidate(VEBUS, '/Dc/0/Current')
            start = len(sim.trace)
            sim.run(60)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain'],
                              request['transfer_intent']), ('CHARGE', 1, 'connected'))
            self.assertIn('No data: QuattroDC', sim.solar.sw['/SolarPriority/Status'])
            self.assertLessEqual(sim.rec.batt[CCL], sim.rec.cfg.sustain_ccl_a + 1)
            self.assertLessEqual(sim.rec.batt[BASE], sim.plant.voltage + .1)
            self.assertLess(max(row['battery_w'] for row in sim.trace[start:]), 400)
            sim.bus.invalid.discard((VEBUS, '/Dc/0/Current'))
            sim.run(10)
            self.assertNotIn('No data', sim.solar.sw['/SolarPriority/Status'])
            self.assertEqual(sim.solar.last_request['mode'], 'CHARGE')

    def test_missing_quattro_current_while_islanded_returns_under_the_floor(self):
        from solar_priority_plant import BASE, CCL, VEBUS
        with self.simulation(target=80) as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.run(120)
            self.assertEqual(sim.solar.last_request['requested_limits']['sustain'], 0)
            sim.bus.invalidate(VEBUS, '/Dc/0/Current')
            self.until(sim, lambda: sim.plant.connected, timeout=60)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain'],
                              request['transfer_intent']), ('CHARGE', 1, 'connected'))
            sim.run(30)
            self.assertLessEqual(sim.rec.batt[BASE], sim.plant.voltage + .1)
            self.assertLessEqual(sim.rec.batt[CCL],
                                 sim.system['/Dc/Pv/Current'] + sim.rec.cfg.sustain_ccl_a + 1)
            self.assertLessEqual(sim.plant.q_w, (sim.rec.cfg.sustain_ccl_a + 2) * sim.plant.voltage)

    def ignore_offset(self, sim):
        """systemcalc below Superuser: the offset write succeeds, nothing applies it."""
        original = sim.plant.apply
        sim.plant.apply = lambda kind, value: original(kind, 0.0 if kind == 'offset_v' else value)
        return original

    def test_ignored_solar_offset_from_startup_is_an_actionable_fault(self):
        # E14: CCL stayed 0 for 180 s with Ready 0, an empty LeadFault, no
        # alarm, and SolarLead 0.30 V while the effective offset was 0.
        from solar_priority_plant import CCL
        with self.simulation(target=80) as sim:
            sim.set_sun([0, 0])
            self.ignore_offset(sim)
            sim.run(20)
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], 0.0)
            self.assertEqual(sim.rec.batt['/RecBms/LeadFault'], '')
            sim.run(40)
            fault = sim.rec.batt['/RecBms/LeadFault']
            self.assertIn('systemcalc ignores the solar offset', fault)
            self.assertIn('access level', fault)
            self.assertEqual(sim.rec.batt['/Alarms/InternalFailure'], 1)
            self.assertEqual(sim.rec.batt['/RecBms/Voltage/Ready'], 0)
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], 0.0)
            self.assertEqual(sim.rec.batt[CCL], 0)
            self.assertAlmostEqual(sim.rec.batt['/RecBms/Voltage/RequestedSolar'] -
                                   sim.rec.batt['/RecBms/Voltage/RequestedQuattro'], .3, places=6)
            self.assertFalse(sim.rec._set_boost(.3))
            self.assertIn('solar lead fault', sim.rec.batt['/RecBms/SolarBoost/Status'])
            self.assertIn('ignores the solar offset', sim.solar.inp.lead_fault)

    def test_lost_solar_offset_after_verification_is_reported_and_recovers(self):
        # E15: the safe envelope kept 5 A, but nothing said the band was gone.
        from solar_priority_plant import CCL, OFFSET
        with self.simulation(target=80) as sim:
            sim.set_sun([0, 0])
            sim.run(180)
            self.assertEqual(sim.rec.batt['/RecBms/Voltage/Ready'], 1)
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], .3)
            original = self.ignore_offset(sim)
            sim.plant.apply('offset_v', .3)
            sim.run(15)
            self.assertEqual(sim.rec.batt['/RecBms/LeadFault'], '')
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], .3)
            sim.run(45)
            self.assertIn('systemcalc ignores the solar offset', sim.rec.batt['/RecBms/LeadFault'])
            self.assertEqual(sim.rec.batt['/Alarms/InternalFailure'], 1)
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], 0.0)
            self.assertEqual(sim.rec.batt['/RecBms/Voltage/Ready'], 0)
            # the objective and the envelope stand: CHARGE, floor, PV + 5 A,
            # and the Quattro is still commanded the hold voltage, not a band under it
            self.assertEqual(sim.solar.last_request['mode'], 'CHARGE')
            self.assertEqual(sim.rec.batt[CCL], sim.rec.cfg.sustain_ccl_a)
            hold = sim.rec.batt['/RecBms/Sustain/HoldVoltage']
            self.assertAlmostEqual(sim.rec.batt['/RecBms/Voltage/RequestedQuattro'], hold, delta=.011)
            self.assertAlmostEqual(sim.rec.batt['/RecBms/Voltage/RequestedSolar'], hold + .3, delta=.011)
            # systemcalc restarted at Superuser: the written offset applies
            sim.plant.apply = original
            sim.plant.apply('offset_v', sim.system[OFFSET])
            sim.run(15)
            self.assertEqual(sim.rec.batt['/RecBms/LeadFault'], '')
            self.assertEqual(sim.rec.batt['/Alarms/InternalFailure'], 0)
            self.assertEqual(sim.rec.batt['/RecBms/Voltage/Ready'], 1)
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], .3)

    def test_ordinary_update_delays_are_not_a_fault(self):
        with self.simulation(target=80) as sim:
            sim.set_sun([0, 0])
            unready = 0
            for second in range(600):
                sim.run(1)
                unready = unready + 1 if not sim.rec.batt['/RecBms/Voltage/Ready'] else 0
                self.assertLess(unready, sim.rec.cfg.lead_verify_s)
                self.assertEqual(sim.rec.batt['/RecBms/LeadFault'], '')
                if second == 300:
                    sim.set_target(85)
            sim.set_sun([500, 900])
            self.until(sim, lambda: not sim.plant.connected)
            sim.run(60)
            self.assertEqual(sim.rec.batt['/RecBms/LeadFault'], '')

    def test_stopped_consumer_lease_returns_to_shore(self):
        with self.simulation() as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.stop_solar(stalled=True)
            sim.run(40)
            self.assertTrue(sim.plant.connected)


if __name__ == '__main__':
    unittest.main()
