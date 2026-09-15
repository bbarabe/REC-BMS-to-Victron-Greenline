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
        for limits in ({'sustain': 4, 'boost_v': 0, 'purpose': ''},
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

    def watch_closure(self, sim):
        """What the plant had actually applied at the physical AC re-accept."""
        closure = {}
        original = sim.plant.update_connection
        def update_connection(cause='source'):
            was = sim.plant.connected
            original(cause)
            if sim.plant.connected and not was and not closure:
                closure.update(time_s=sim.clock.elapsed, quattro_v=sim.plant.dvcc.quattro_v,
                               ccl_a=sim.plant.ccl_a, voltage=sim.plant.voltage,
                               ocv=sim.plant.ocv(), pv_a=sum(sim.plant.pv_w) / sim.plant.voltage,
                               request=dict(sim.solar.last_request or {}),
                               active_input=sim.vebus['/Ac/ActiveIn/ActiveInput'])
        sim.plant.update_connection = update_connection
        return closure

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

    def test_full_target_is_one_way_charge_until_arrival_then_the_endgame(self):
        # Owner, 2026-09-14 (master D12): 100 % does NOT mean shore bulk. At
        # night from 60 % the floor holds and shore only sustains; at 99.7 %
        # the endgame commands both chargers the true full voltage, no lead.
        from solar_priority_plant import BASE, PlantConfig
        with self.simulation(target=100, plant_config=PlantConfig(initial_soc=60)) as sim:
            sim.set_sun([0, 0])
            sim.run(1200)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain']), ('CHARGE', 1))
            self.assertLess(sim.plant.energy.shore_charge_wh, 150)
            self.assertLess(sim.plant.soc, 60.5)
            self.assertAlmostEqual(sim.rec.batt['/RecBms/TargetChargeVoltage'],
                                   sim.rec.batt['/RecBms/Sustain/HoldVoltage'] + sim.rec.cfg.sustain_band_v,
                                   delta=.011)
        with self.simulation(target=100, plant_config=PlantConfig(initial_soc=99.7)) as sim:
            sim.set_sun([0, 0])
            sim.run(100)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain']), ('COMPLETE_FULL', 0))
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Active'], 0)
            full = min(sim.rec.cfg.cvl_max, sim.rec._safe_voltage())
            self.assertAlmostEqual(sim.rec.batt['/RecBms/Voltage/RequestedQuattro'], full, delta=.011)
            self.assertAlmostEqual(sim.rec.batt['/RecBms/Voltage/RequestedSolar'], full, delta=.011)
            self.assertEqual(sim.rec.batt['/RecBms/SolarLead'], 0.0)

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
            closure = self.watch_closure(sim)
            self.until(sim, lambda: sim.plant.connected, timeout=60)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain'],
                              request['transfer_intent']), ('CHARGE', 1, 'connected'))
            sim.run(30)
            self.assertLessEqual(sim.rec.batt[BASE], sim.plant.voltage + .1)
            self.assertLessEqual(sim.rec.batt[CCL],
                                 sim.system['/Dc/Pv/Current'] + sim.rec.cfg.sustain_ccl_a + 1)
            self.assertLessEqual(sim.plant.q_w, (sim.rec.cfg.sustain_ccl_a + 2) * sim.plant.voltage)
            # Stage A: the source-loss return is prepared like any other --
            # the hold and its cap were applied before the AC input was accepted.
            self.assertTrue(closure, 'the return never closed the relay')
            self.assertLessEqual(closure['quattro_v'], max(closure['voltage'], closure['ocv']) + .01)
            self.assertLessEqual(closure['ccl_a'], closure['pv_a'] + sim.rec.cfg.sustain_ccl_a + 1)
            self.assertTrue(json.loads(sim.rec.batt['/RecBms/Policy/Status'])['transfer']['prepared'])

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

    def test_one_slider_step_at_night_charges_one_way_not_from_shore(self):
        # E06: 62 % for 65 %, no sun, 20 min ran as HOLD and put 1.443 kWh
        # into the bank from shore.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=65, plant_config=PlantConfig(initial_soc=62)) as sim:
            sim.set_sun([0, 0])
            sim.run(1200)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain']), ('CHARGE', 1))
            self.assertEqual(sim.rec.policy_adapter.control['mode'], 'CHARGE')
            self.assertLess(sim.plant.energy.shore_charge_wh, 150)
            self.assertLess(sim.plant.soc, 62.5)

    def test_solar_admission_needs_the_complete_dc_bus_demand(self):
        # E09: 400 W of PV left shore against 300 W AC + 300 W DC on a 360 W
        # need; E16: even the 50 W DC baseline needs 443 W with 300 W AC.
        for ac, dc, sun, leaves in ((300, 300, [200, 200], False), (300, 50, [200, 200], False),
                                    (300, 50, [250, 250], True)):
            with self.subTest(ac=ac, dc=dc, pv=sum(sun)), self.simulation(target=80) as sim:
                sim.set_load(ac_w=ac, dc_w=dc)
                sim.set_sun(sun)
                sim.run(900)
                self.assertEqual(not sim.plant.connected, leaves)
                demand = json.loads(sim.rec.batt['/RecBms/Policy/Snapshot'])['demand']
                need = sim.solar.sw['/SolarPriority/NeedW']
                self.assertGreater(need, 360)
                self.assertGreaterEqual(need, demand['admission_w'] - 5)
                if leaves:
                    self.assertIn('vs need', sim.solar.sw['/SolarPriority/LastTransition'])

    def test_missing_dc_demand_admits_no_elective_departure(self):
        from solar_priority_plant import SYSTEM
        with self.simulation(target=80) as sim:
            sim.set_sun([700, 700])
            sim.bus.invalidate(SYSTEM, '/Dc/System/Power')
            sim.run(900)
            self.assertTrue(sim.plant.connected)
            self.assertIsNone(sim.solar.sw['/SolarPriority/NeedW'])
            self.assertIn('[no demand]', sim.solar.sw['/SolarPriority/Status'])

    def test_one_way_discharge_leaves_without_pv_covering_the_loads(self):
        with self.simulation(target=40) as sim:
            sim.set_sun([0, 0])
            self.until(sim, lambda: not sim.plant.connected)
            self.assertEqual(sim.solar.last_request['mode'], 'DISCHARGE')

    def test_stopped_consumer_lease_returns_to_shore(self):
        with self.simulation() as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.stop_solar(stalled=True)
            sim.run(40)
            self.assertTrue(sim.plant.connected)

    def test_failed_probe_marks_the_request_until_rec_acknowledges_it(self):
        # issue #8 / master D16: an evaluated probe failure is the one outcome
        # the engine's own cooldown cannot carry across a restart, so it has to
        # reach REC as a marked request. Here the cloud arrives the moment the
        # transfer completes: 120 W of PV against a 433 W island. The marker
        # rides on every request until REC acknowledges one that carried it,
        # then the ordinary purpose mapping resumes. This tree's REC side does
        # not consume the marker yet, so only what the consumer sends is
        # asserted.
        def keep_the_probe(cfg):
            cfg.engine['ONEWAY_SKIP_PROBE'] = 0
        with self.simulation(target=80, configure_solar=keep_the_probe) as sim:
            purpose = lambda: sim.solar.last_request['requested_limits']['purpose']
            sim.set_sun([260, 260])
            self.until(sim, lambda: sim.solar.sw['/SolarPriority/State'] == 'probe'
                       and not sim.plant.connected)
            sim.set_sun([60, 60])
            self.until(sim, lambda: purpose() == 'failed_probe', timeout=200)
            self.assertIn('probe failed', sim.solar.sw['/SolarPriority/LastTransition'])
            self.until(sim, lambda: purpose() != 'failed_probe', timeout=30)
            self.assertEqual(purpose(), 'solar')
            sim.run(30)
            self.assertEqual(purpose(), 'solar')

    def test_ordinary_return_is_prepared_before_the_relay_closes(self):
        # Stage A end to end (D02/E03): the engine asks for the floor the tick
        # it decides to return, REC anchors the hold at the islanded bank and
        # waits for the exact pair and the current readback, and only then
        # writes the relay -- so with five-second command propagation the
        # below-bank Quattro command and the PV + 5 A cap are already applied
        # when the AC input is accepted. E03 closed at 59.34 V / 200 A.
        from solar_priority_plant import Latency
        with self.simulation(target=80, latency=Latency(base_s=5.0, current_s=5.0)) as sim:
            self.until(sim, lambda: not sim.plant.connected)
            sim.run(60)
            self.assertEqual(sim.solar.last_request['requested_limits']['sustain'], 0)
            closure = self.watch_closure(sim)
            sim.set_sun([0, 0])
            decided = []
            def note():
                if not decided and sim.solar.sw['/SolarPriority/State'] == 'shore':
                    decided.append(sim.clock.elapsed)
                return sim.plant.connected
            self.until(sim, note, timeout=1500)              # 75 Wh at ~380 W takes ~12 min
            self.assertTrue(closure, 'the return never closed the relay')
            self.assertIn('deficit', sim.solar.sw['/SolarPriority/LastTransition'])
            # the floor rode the very request that asked for shore, before any
            # input was accepted
            request = closure['request']
            self.assertEqual(closure['active_input'], 240)
            self.assertEqual((request['mode'], request['transfer_intent'],
                              request['requested_limits']['sustain']), ('CHARGE', 'connected', 1))
            self.assertLessEqual(closure['quattro_v'], max(closure['voltage'], closure['ocv']) + .01)
            self.assertLessEqual(closure['ccl_a'], closure['pv_a'] + sim.rec.cfg.sustain_ccl_a + 1)
            status = json.loads(sim.rec.batt['/RecBms/Policy/Status'])
            self.assertTrue(status['transfer']['prepared'])
            self.assertIsNone(status['transfer']['last_fault'])
            self.assertLessEqual(closure['time_s'] - decided[0],
                                 sim.rec.policy_adapter.config.return_prepare_s + 5)
            sim.run(30)
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Active'], 1)
            self.assertLessEqual(sim.plant.q_w, (sim.rec.cfg.sustain_ccl_a + 2) * sim.plant.voltage)

    def test_absent_and_available_shore_give_distinct_outcomes_on_a_suspend(self):
        # The 2026-09-06 case (a heater-class load on solar, no shore power)
        # against its mirror image. Absent shore: the engine asks for no floor,
        # so the MPPTs keep the slider's ceiling, and REC records 'shore
        # unavailable' with no fault and no lockout. Available shore: the
        # floor is requested while ActiveInput still reads 240 and the return
        # arrives prepared.
        for available in (False, True):
            with self.subTest(shore_available=available), self.simulation(target=80) as sim:
                self.until(sim, lambda: not sim.plant.connected)
                sim.run(30)
                ceiling = sim.rec.batt['/RecBms/TargetChargeVoltage']
                sim.plant.shore_available = available
                closure = self.watch_closure(sim)
                sim.set_load(ac_w=3000)                       # above the 2.5 kW suspend threshold
                self.until(sim, lambda: sim.solar.sw['/SolarPriority/State'] == 'suspend', timeout=60)
                sim.run(45)
                request = sim.solar.last_request
                transfer = json.loads(sim.rec.batt['/RecBms/Policy/Status'])['transfer']
                self.assertEqual(request['transfer_intent'], 'connected')
                if not available:
                    self.assertFalse(sim.plant.connected)
                    self.assertEqual(request['requested_limits']['sustain'], 0)
                    self.assertEqual(sim.rec.batt['/RecBms/Sustain/Active'], 0)
                    self.assertEqual(sim.rec.batt['/RecBms/TargetChargeVoltage'], ceiling)
                    self.assertEqual(transfer['limited_by'], 'shore unavailable')
                    self.assertFalse(transfer['available'])
                    self.assertIsNone(transfer['last_fault'])
                    self.assertEqual(sim.rec.policy_adapter.transfer.durable['fault_until'], 0)
                else:
                    self.assertTrue(closure, 'the suspend never reached shore')
                    self.assertEqual(closure['active_input'], 240)
                    self.assertEqual(closure['request']['requested_limits']['sustain'], 1)
                    self.assertLessEqual(closure['quattro_v'], max(closure['voltage'], closure['ocv']) + .01)
                    self.assertTrue(transfer['prepared'])
                    self.assertEqual(sim.rec.batt['/RecBms/Sustain/Active'], 1)

    def test_hold_at_the_target_asks_for_the_two_sided_hold(self):
        # Stage B (plan B2, master D03/SP23): at the destination the leased
        # request is HOLD with sustain 3 -- dbus-recbms' two-sided hold --
        # where 4.9 released sustain and let the slider, the band and the
        # loads settle it between them (E04: a steady -49 W is 1.448 points
        # in 24 h). This tree's REC does not implement mode 3 yet:
        # _set_sustain(3) simply returns False, so only what the consumer
        # asks for, and that the lease still stands, are asserted here.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=60)) as sim:
            sim.run(100)
            request = sim.solar.last_request
            self.assertEqual((request['mode'], request['requested_limits']['sustain']),
                             ('HOLD', 3))
            self.assertEqual(sim.solar.sw['/SolarPriority/Sustain'], 3)
            self.assertEqual(sim.solar.sw['/SolarPriority/OneWay'], '')
            status = json.loads(sim.rec.batt['/RecBms/Policy/Status'])
            self.assertTrue(status['lease_valid'])
            self.assertEqual(status['rejection'], '')
            self.assertEqual(status['mode'], 'HOLD')

    def movement_pct(self, sim, capacity_ah=1440.0):
        energy = sim.plant.energy
        return 100.0 * (energy.charge_ah + energy.discharge_ah) / capacity_ah

    def test_hold_day_and_night_moves_the_bank_well_under_a_percent(self):
        # E11 (repair plan section 4): the nominal HOLD day, four hours of sun
        # then four of darkness at the target. The old release let the band
        # fill and burn and the Quattro sit 0.15 V under the bank; the hold
        # curtails PV at the destination and covers the loads at night.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=60)) as sim:
            sim.set_load(ac_w=300, dc_w=50)
            sim.set_sun([700, 700])
            sim.run(4 * 3600)
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Mode'], 3)
            self.assertEqual(sim.solar.last_request['mode'], 'HOLD')
            sim.set_sun([0, 0])
            sim.run(4 * 3600)
            movement = self.movement_pct(sim)
            self.assertLess(movement, 0.6, 'combined movement %.3f %%' % movement)
            self.assertAlmostEqual(sim.plant.soc, 60.0, delta=0.5)
            self.assertTrue(sim.plant.connected)
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Mode'], 3)

    def test_hold_on_shore_at_night_does_not_drain(self):
        # E04: a steady small deficit under the old release cost 1.448 points
        # in 24 h because nothing regulated the bank against its destination.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=60)) as sim:
            sim.set_load(ac_w=300, dc_w=50)
            sim.set_sun([0, 0])
            sim.run(3600)
            lowest = highest = sim.plant.soc
            before = sim.plant.energy.charge_ah - sim.plant.energy.discharge_ah
            for _ in range(5):
                sim.run(3600)
                lowest, highest = min(lowest, sim.plant.soc), max(highest, sim.plant.soc)
            net = sim.plant.energy.charge_ah - sim.plant.energy.discharge_ah - before
            self.assertGreaterEqual(lowest, 59.7)
            self.assertLessEqual(highest, 60.3)
            self.assertLess(abs(net), 3.0, 'net %.2f Ah over five hours' % net)
            self.assertAlmostEqual(sim.rec.batt['/RecBms/Voltage/RequestedQuattro'],
                                   sim.rec.batt['/RecBms/Sustain/HoldVoltage'], delta=0.011)

    def test_discharge_through_alternating_sun_does_not_refill(self):
        # E08: 80 % for 60, one hour dark and one hour of 1400 W sun for eight
        # hours put 17.684 Ah back into the bank (1.228 % reverse) because
        # the ceiling answered only the inferred Quattro. Positive current
        # from any source now servos the ceiling down.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=80)) as sim:
            sim.set_load(ac_w=300, dc_w=50)
            sim.set_sun([0, 0])
            self.until(sim, lambda: not sim.plant.connected)
            self.assertEqual(sim.solar.last_request['mode'], 'DISCHARGE')
            for hour in range(8):
                sim.set_sun([0, 0] if hour % 2 == 0 else [700, 700])
                sim.run(3600)
            reverse = sim.plant.energy.charge_ah
            self.assertLess(reverse, 4.0, 'reverse %.3f Ah' % reverse)
            self.assertLess(sim.plant.soc, 79.0)

    def test_arrival_from_charge_hands_into_the_hold(self):
        from solar_priority_plant import PlantConfig
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=58.2)) as sim:
            sim.set_load(ac_w=300, dc_w=50)
            sim.set_sun([700, 700])
            sim.run(5)
            self.assertEqual(sim.solar.last_request['mode'], 'CHARGE')
            self.until(sim, lambda: sim.solar.last_request['mode'] == 'HOLD', timeout=4 * 3600)
            self.assertEqual(sim.solar.last_request['requested_limits']['sustain'], 3)
            arrival = sim.plant.soc
            self.assertAlmostEqual(arrival, 59.5, delta=0.3)
            sim.run(2 * 3600)
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Mode'], 3)
            self.assertEqual(sim.solar.last_request['mode'], 'HOLD')
            self.assertGreaterEqual(sim.plant.soc, 59.6)
            self.assertLessEqual(sim.plant.soc, 60.4)

    def test_arrival_from_discharge_returns_prepared_and_holds(self):
        from solar_priority_plant import PlantConfig
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=61.3)) as sim:
            sim.set_load(ac_w=900, dc_w=50)
            sim.set_sun([0, 0])
            sim.run(5)
            self.assertEqual(sim.solar.last_request['mode'], 'DISCHARGE')
            self.until(sim, lambda: not sim.plant.connected)
            closure = self.watch_closure(sim)
            self.until(sim, lambda: sim.solar.last_request['mode'] == 'HOLD', timeout=4 * 3600)
            self.assertAlmostEqual(sim.plant.soc, 60.5, delta=0.3)
            self.until(sim, lambda: sim.plant.connected, timeout=900)
            self.assertTrue(closure)
            self.assertEqual(closure['request']['requested_limits']['sustain'], 3)
            self.assertLessEqual(closure['quattro_v'], max(closure['voltage'], closure['ocv']) + .01)
            self.assertLessEqual(closure['ccl_a'], closure['pv_a'] + sim.rec.cfg.sustain_ccl_a + 1)
            shore_before = sim.plant.energy.shore_charge_wh
            sim.run(3600)
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Mode'], 3)
            self.assertGreaterEqual(sim.plant.soc, 59.7)
            self.assertLessEqual(sim.plant.soc, 60.8)
            self.assertLess(sim.plant.energy.shore_charge_wh - shore_before, 200)

    def shore_status(self, sim):
        """The shore input REC published, or None before it has said."""
        raw = sim.rec.batt.values.get('/RecBms/Policy/Status')
        return (json.loads(raw) if raw else {}).get('shore_ac_input')

    def ignore_states(self, sim, into):
        """Sample both acknowledged ignore states: a hold left standing on
        the wrong input would be invisible in an end-of-run reading."""
        into.append((sim.vebus['/Ac/State/IgnoreAcIn1'], sim.vebus['/Ac/State/IgnoreAcIn2']))

    def runs(self, values):
        """A sampled sequence collapsed to its distinct consecutive values."""
        return [v for n, v in enumerate(values) if n == 0 or values[n - 1] != v]

    def test_shore_on_ac_input_2_is_resolved_and_controlled(self):
        # The owner rewired shore power from AC in 1 to AC in 2 (2026-09-15).
        # Neither service is configured with the answer: the GX's own AC input
        # types say which input is shore, REC publishes what it resolved, and
        # every relay command goes to that input -- a command written to the
        # other one would strand an ignore on an input nobody is watching.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=80, plant_config=PlantConfig(shore_input=2, initial_soc=60)) as sim:
            states = []
            sim.run(30)
            self.assertEqual(self.shore_status(sim), 2)
            self.assertEqual(sim.vebus['/Ac/ActiveIn/ActiveInput'], 1)
            self.until(sim, lambda: (self.ignore_states(sim, states), not sim.plant.connected)[1])
            self.assertEqual(sim.vebus['/Ac/ActiveIn/ActiveInput'], 240)
            self.assertFalse(sim.plant.relay_edges[-1]['connected'])
            sim.run(30)
            closure = self.watch_closure(sim)
            sim.set_load(ac_w=3000)                       # above the 2.5 kW suspend threshold
            self.until(sim, lambda: sim.solar.sw['/SolarPriority/State'] == 'suspend', timeout=60)
            self.until(sim, lambda: (self.ignore_states(sim, states), sim.plant.connected)[1], timeout=600)
            self.assertTrue(closure, 'the suspend never reached shore')
            self.assertEqual(closure['active_input'], 240)
            self.assertEqual(sim.vebus['/Ac/ActiveIn/ActiveInput'], 1)
            self.assertEqual(sim.vebus['/Ac/ActiveIn/Connected'], 1)
            self.assertTrue(json.loads(sim.rec.batt['/RecBms/Policy/Status'])['transfer']['prepared'])
            # AC in 2 was held and released; AC in 1 is not ours to touch
            self.assertEqual(self.runs([one for one, _ in states]), [0])
            self.assertEqual(self.runs([two for _, two in states]), [0, 1, 0])
            self.assertEqual({write['path'] for write in sim.bus.writes if 'IgnoreAcIn' in write['path']},
                             {'/Ac/Control/IgnoreAcIn2'})
            self.assertEqual([c for c in sim.plant.commands if c['kind'] == 'ignore_wrong_input'], [])

    def test_shore_input_resolves_from_live_evidence_without_gx_types(self):
        # A GX nobody ever configured: both AC input types read 0, so they say
        # nothing at all. The only evidence is the boat sitting accepted on AC
        # in 2, and both services have to read it the same way -- a resolution
        # taken before any of the Quattro's facts arrive is not evidence.
        from solar_priority_plant import PlantConfig
        with self.simulation(target=80, plant_config=PlantConfig(shore_input=2, gx_input_types=(0, 0))) as sim:
            self.assertEqual(sim.vebus['/Ac/ActiveIn/ActiveInput'], 1)
            self.until(sim, lambda: self.shore_status(sim) == 2, timeout=60)
            self.until(sim, lambda: not sim.plant.connected)
            self.assertEqual(sim.vebus['/Ac/State/IgnoreAcIn2'], 1)
            self.assertEqual(sim.vebus['/Ac/State/IgnoreAcIn1'], 0)
            self.assertEqual({write['path'] for write in sim.bus.writes if 'IgnoreAcIn' in write['path']},
                             {'/Ac/Control/IgnoreAcIn2'})
            self.assertEqual([c for c in sim.plant.commands if c['kind'] == 'ignore_wrong_input'], [])

    def prefer_status(self, sim):
        """What REC says about the Quattro's renewable preference, or {}."""
        raw = sim.rec.batt.values.get('/RecBms/Policy/Status')
        return ((json.loads(raw) if raw else {}).get('prefer_renewable') or {})

    def prefer_applied(self, sim):
        """The preference values the plant actually took, in order. Only a
        CHANGE is published and applied, so the list is the edges themselves."""
        return [c['value'] for c in sim.plant.commands if c['kind'] == 'prefer_renewable']

    def test_prefer_renewable_is_solar_by_day_and_charge_now_by_night(self):
        # The boat runs with "Prefer renewable energy" set (2026-09-15): while
        # it is 1 and shore is connected the Quattro does not charge the bank
        # at all -- the charger sits in sustain and the DC loads drain the
        # bank on shore (measured: zero amps for an hour at 0.8 A of drain).
        # REC owns that toggle the way it owns the relay: prefer solar by day,
        # charge now at night, one edge each way, and the night is still the
        # hold's -- the Quattro covers the loads at the hold voltage.
        from solar_priority_plant import PREFER, PlantConfig
        with self.simulation(target=60,
                             plant_config=PlantConfig(prefer_renewable=1, initial_soc=60)) as sim:
            sim.set_load(ac_w=300, dc_w=160)
            sim.set_sun([700, 700])
            sim.run(4 * 3600)
            self.assertEqual(sim.solar.last_request['mode'], 'HOLD')
            self.assertEqual(sim.vebus[PREFER], 1)
            self.assertEqual(sim.plant.q_w, 0.0, 'the charger charged while in sustain')
            self.assertEqual(self.prefer_status(sim).get('wanted'), 1)
            self.assertEqual(self.prefer_applied(sim), [], 'the day was not left preferred')
            # Dusk is the floor's own rule: PV under pv_min_a for dusk_s.
            dark_at = sim.clock.elapsed
            sim.set_sun([0, 0])
            self.until(sim, lambda: sim.vebus[PREFER] == 0,
                       timeout=int(sim.rec.cfg.sustain_dusk_s) + 300)
            status = self.prefer_status(sim)
            self.assertEqual(status.get('wanted'), 0)
            self.assertRegex(status.get('reason', ''), 'dusk|night')
            # The published actual is a read-back, one poll behind the write.
            sim.run(30)
            self.assertEqual(self.prefer_status(sim).get('actual'), 0)
            self.assertEqual(sim.rec.batt.values.get('/RecBms/Policy/Telemetry/PreferRenewable'), 0)
            sim.run(4 * 3600 - (sim.clock.elapsed - dark_at))
            self.assertTrue(sim.plant.connected)
            self.assertEqual(sim.rec.batt['/RecBms/Sustain/Mode'], 3)
            self.assertAlmostEqual(sim.plant.soc, 60.0, delta=0.5)
            # Dawn: PV current back over the dawn threshold for dawn_s.
            light_at = sim.clock.elapsed
            sim.set_sun([700, 700])
            self.until(sim, lambda: sim.vebus[PREFER] == 1, timeout=900)
            self.assertEqual(self.prefer_status(sim).get('wanted'), 1)
            sim.run(3600 - (sim.clock.elapsed - light_at))
            self.assertAlmostEqual(sim.plant.soc, 60.0, delta=0.5)
            # Exactly one edge each way over the whole scenario; the plant
            # started out preferred.
            applied = [1] + self.prefer_applied(sim)
            self.assertEqual([(was, now) for was, now in zip(applied, applied[1:]) if was != now],
                             [(1, 0), (0, 1)])


if __name__ == '__main__':
    unittest.main()
