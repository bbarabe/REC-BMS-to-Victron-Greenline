#!/usr/bin/env python3
"""Focused production-adapter integration regressions using the coupled bus."""
import contextlib
import json
import struct
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'dbus-recbms'))
import unittest

from solar_priority_plant import CCL, IGNORE, SYSTEM, VEBUS, CoupledSimulation, PlantConfig
from control_inputs import SourceRegistry, TimedMean, LoadServiceEvidence, DemandModel
from rec_policy_adapter import RecPolicyAdapter
from types import SimpleNamespace

class AdapterBoundaryTests(unittest.TestCase):
    @contextlib.contextmanager
    def simulation(self, **kwargs):
        with CoupledSimulation(configure_rec=lambda cfg: setattr(cfg, 'policy_state_path', None),
                               **kwargs) as simulation:
            yield simulation

    def wait_for(self, sim, predicate, timeout_s=1200):
        for _ in range(timeout_s):
            if predicate():
                return
            sim.run(1)
        self.fail('physical scenario did not reach its required state within %ss' % timeout_s)

    def status(self, sim):
        return json.loads(sim.rec.batt['/RecBms/Policy/Status'])

    def set_installed_selection(self, sim, **changes):
        selection = {'/ActiveBatteryService': 'com.victronenergy.battery/200',
                     '/Dc/Battery/BatteryService': sim.battery_name,
                     '/ActiveBmsService': sim.battery_name, '/ActiveBmsInstance': 200}
        selection.update(changes)
        for path, value in selection.items():
            if path not in sim.system.values:
                sim.system.add_path(path, value)
            else:
                sim.system[path] = value

    def test_installed_selection_identity_authorizes_both_policy_adapters(self):
        with self.simulation() as sim:
            self.set_installed_selection(sim)
            sim.run(90)
            self.assertTrue(self.status(sim)['ready'])
            diagnostics = json.loads(sim.solar.sw['/SolarPriority/Diagnostics'])
            self.assertTrue(diagnostics['sources_valid'])
            self.assertTrue(sim.rec.policy_adapter.contract.owned)

    def test_missing_or_conflicting_installed_identity_blocks_both_adapters(self):
        for path, value in (('/ActiveBatteryService', 'com.victronenergy.battery/201'),
                            ('/Dc/Battery/BatteryService', 'com.victronenergy.battery.other'),
                            ('/ActiveBmsService', 'com.victronenergy.battery.other'),
                            ('/ActiveBmsInstance', 201), ('/Dc/Battery/BatteryService', None),
                            ('/ActiveBmsInstance', None)):
            with self.subTest(path=path, value=value), self.simulation() as sim:
                self.set_installed_selection(sim)
                sim.run(40)
                self.set_installed_selection(sim, **{path: value})
                sim.run(10)
                self.assertFalse(self.status(sim)['ready'])
                diagnostics = json.loads(sim.solar.sw['/SolarPriority/Diagnostics'])
                self.assertFalse(diagnostics['sources_valid'])
                self.assertFalse(any(not edge['connected'] for edge in sim.plant.relay_edges))

    def test_prepared_current_cannot_override_reduced_rec_prohibition(self):
        with self.simulation() as sim:
            sim.run(90)
            self.assertTrue(sim.plant.connected)
            self.assertFalse(hasattr(sim.rec.policy_adapter, 'prepared_limits'))
            self.assertGreater(sim.rec.batt[CCL], 0)
            sim.plant.rec_ccl = 0
            sim.run(3)
            self.assertEqual(sim.rec.batt[CCL], 0)

    def test_module_charge_prohibition_survives_policy_regulation(self):
        with self.simulation() as sim:
            sim.run(40)
            self.assertTrue(sim.rec.policy_adapter.contract.owned)
            sim._feed_can()
            sim.rec_module.decode_frame(sim.rec.bms, 0x372, struct.pack('<HHHH', 6, 1, 0, 0))
            sim.rec._tick()
            self.assertEqual(sim.rec.batt[CCL], 0)

    def test_zero_charge_permission_stops_net_charge_with_zero_external_dc(self):
        for prohibition in ('raw_current_zero', 'module_charge_blocked'):
            for shore_available in (False, True):
                with self.subTest(prohibition=prohibition, shore_available=shore_available), self.simulation() as sim:
                    sim.set_load(ac_w=300, dc_w=0)
                    self.wait_for(sim, lambda: not sim.plant.connected)
                    sim.plant.shore_available = shore_available
                    if prohibition == 'raw_current_zero':
                        sim.plant.rec_ccl = 0
                    else:
                        original_feed = sim._feed_can
                        def feed():
                            original_feed()
                            sim.rec_module.decode_frame(sim.rec.bms, 0x372, struct.pack('<HHHH', 6, 1, 0, 0))
                        sim._feed_can = feed
                    # Full CHARGE starts with full REC permission. Verify its
                    # withdrawal promptly, then allow this fixture's 3 s DVCC
                    # cycle and 3 s internal current/PV decay (36 s total). The
                    # physical installation still needs its own response proof.
                    transient_start = len(sim.trace)
                    sim.run(3)
                    self.assertEqual(sim.rec.batt[CCL], 0)
                    sim.run(33)
                    transient_wh = sum(max(0, row['battery_w']) for row in sim.trace[transient_start:]) / 3600
                    self.assertLess(transient_wh, 3.0)
                    start = len(sim.trace)
                    sim.run(60)
                    self.assertLessEqual(max(row['battery_w'] for row in sim.trace[start:]), 1.0)

    def test_off_and_full_policy_paths_obey_raw_charge_voltage_guard(self):
        for enabled, soc, expected_mode in ((False, 60, 'OFF'), (True, 98, 'COMPLETE_FULL'),
                                            (True, 100, 'COMPLETE_FULL')):
            with self.subTest(mode=expected_mode), self.simulation(
                    target=100, enabled=enabled, plant_config=PlantConfig(initial_soc=soc)) as sim:
                sim.set_load(ac_w=300, dc_w=0)
                sim.plant.rec_ccl = 0
                sim.run(320)
                self.assertEqual(sim.rec.policy_adapter.control['mode'], expected_mode)
                self.assertEqual(sim.rec.batt[CCL], 0)
                ceiling = sim.plant.voltage - sim.rec.cfg.voltage_guard_v + .01
                self.assertLessEqual(sim.plant.dvcc.quattro_v, ceiling)
                self.assertLessEqual(sim.plant.dvcc.solar_v, ceiling)
                self.assertIn('charge prohibited', sim.rec.batt['/RecBms/Voltage/Status'])

    def test_restart_with_no_fresh_charger_readback_withholds_charge(self):
        with self.simulation() as sim:
            sim.run(40)
            sim.restart_rec()
            sim._feed_can()
            sim.rec.sp_enabled = None
            sim.rec._tick()
            self.assertEqual(sim.rec.batt[CCL], 0)
            self.assertFalse(self.status(sim)['lease_valid'])
            sim.run(10)
            self.assertTrue(sim.rec._voltage_within_envelope(sim.rec._safe_voltage()))

    def test_negative_current_readback_cannot_prove_transfer_readiness(self):
        with self.simulation(enabled=False) as sim:
            original_read = sim.bus.read
            def read(service, path):
                if service == VEBUS and path == '/BatteryOperationalLimits/MaxChargeCurrent':
                    return -1
                return original_read(service, path)
            sim.bus.read = read
            sim.run(40)
            self.assertFalse(self.status(sim)['ready'])

    def test_missing_or_excess_current_readback_blocks_new_departure(self):
        for readback in (None, 1000):
            with self.subTest(readback=readback):
                with self.simulation() as sim:
                    original_read = sim.bus.read
                    def read(service, path):
                        if service == VEBUS and path == '/BatteryOperationalLimits/MaxChargeCurrent':
                            return readback
                        return original_read(service, path)
                    sim.bus.read = read
                    sim.run(350)
                    self.assertFalse(self.status(sim)['ready'])
                    self.assertFalse(any(not edge['connected'] for edge in sim.plant.relay_edges))

    def test_publisher_before_consumer_does_not_claim_legacy_relay_authority(self):
        with self.simulation() as sim:
            sim.solar_running = False
            sim.run(40)
            self.assertFalse(sim.rec.policy_adapter.contract.owned)
            self.assertFalse(any(write['path'] == IGNORE for write in sim.bus.writes))
            self.assertTrue(sim.rec._sustain_requested('/RecBms/Sustain/Request', 1))

    def test_owned_publisher_rejects_legacy_controls(self):
        with self.simulation() as sim:
            sim.run(40)
            self.assertTrue(sim.rec.policy_adapter.contract.owned)
            self.assertFalse(sim.rec._sustain_requested('/RecBms/Sustain/Request', 1))
            self.assertFalse(sim.rec._boost_requested('/RecBms/SolarBoost/Request', .3))
            self.assertTrue(sim.rec.sustain['active'])  # engine-owned floor remains intact
            self.assertFalse(sim.rec.boost['active'])

    def test_new_consumer_without_protocol_cannot_become_direct_relay_writer(self):
        with self.simulation() as sim:
            sim.bus.invalidate(sim.battery_name, '/RecBms/Policy/Status')
            sim.bus.invalidate(sim.battery_name, '/RecBms/Policy/Version')
            sim.run(90)
            self.assertFalse(sim.rec.policy_adapter.contract.owned)
            self.assertFalse(any(write['path'] == IGNORE for write in sim.bus.writes))
            self.assertTrue(sim.plant.connected)

    def test_old_async_reply_cannot_undo_invalidation_or_source_replacement(self):
        for replace_service in (False, True):
            with self.subTest(replace_service=replace_service):
                with self.simulation() as sim:
                    sim.run(20)
                    adapter = sim.rec.policy_adapter
                    for _ in range(2):
                        sim.clock.elapsed += 1
                        sim.bus.drain()
                    callbacks = []
                    def capture(name, path, interface, method, signature, args,
                                reply_handler, error_handler, **kwargs):
                        callbacks.append((name, reply_handler))
                    sim.bus.call_async = capture
                    sim.clock.elapsed += 1
                    adapter._poll(sim.clock.monotonic())
                    old_reply = next(reply for name, reply in callbacks if name == SYSTEM)
                    old_payload = sim.bus.read(SYSTEM, '/')
                    if replace_service:
                        sim.bus.remove(SYSTEM)
                        sim.clock.elapsed += 1
                        adapter._poll(sim.clock.monotonic())
                        sim.bus.restore(SYSTEM)
                    else:
                        adapter.sources.remove(SYSTEM)
                        old_reply(old_payload)  # discard invalidated request and free its slot
                    callbacks.clear()
                    sim.clock.elapsed += 1
                    adapter._poll(sim.clock.monotonic())
                    new_reply = next(reply for name, reply in callbacks if name == SYSTEM)
                    invalid = dict(old_payload, **{'/Dc/System/Power': None})
                    new_reply(invalid)
                    self.assertIsNone(adapter._value('system', '/Dc/System/Power', sim.clock.monotonic()))
                    old_reply(old_payload)
                    self.assertIsNone(adapter._value('system', '/Dc/System/Power', sim.clock.monotonic()))

    def test_poll_backpressure_bounds_pending_reads_and_preserves_freshness(self):
        with self.simulation() as sim:
            sim.run(20)
            adapter = sim.rec.policy_adapter
            for _ in range(2):
                sim.clock.elapsed += 1
                sim.bus.drain()
            callbacks = []
            def capture(name, path, interface, method, signature, args,
                        reply_handler, error_handler, **kwargs):
                callbacks.append((name, reply_handler, error_handler))
            sim.bus.call_async = capture
            for _ in range(15):
                sim.clock.elapsed += 1
                adapter._poll(sim.clock.monotonic())
            self.assertEqual(len(callbacks), 4)
            self.assertEqual(len(adapter.read_pending), 4)
            self.assertIsNone(adapter._value('system', '/Dc/System/Power', sim.clock.monotonic()))
            name, reply, error = next(c for c in callbacks if c[0] == SYSTEM)
            reply(sim.bus.read(SYSTEM, '/'))
            # A delayed successful read keeps its original request timestamp.
            self.assertIsNone(adapter._value('system', '/Dc/System/Power', sim.clock.monotonic()))
            sim.clock.elapsed += 1
            adapter._poll(sim.clock.monotonic())
            self.assertEqual(len(callbacks), 5)
            callbacks[-1][1](sim.bus.read(SYSTEM, '/'))
            self.assertIsNotNone(adapter._value('system', '/Dc/System/Power', sim.clock.monotonic()))

    def test_late_failed_poll_cannot_invalidate_a_replaced_source(self):
        with self.simulation() as sim:
            sim.run(20)
            adapter = sim.rec.policy_adapter
            for _ in range(2):
                sim.clock.elapsed += 1
                sim.bus.drain()
            callbacks = []
            def capture(name, path, interface, method, signature, args,
                        reply_handler, error_handler, **kwargs):
                callbacks.append((name, reply_handler, error_handler))
            sim.bus.call_async = capture
            sim.clock.elapsed += 1
            adapter._poll(sim.clock.monotonic())
            old_error = next(error for name, reply, error in callbacks if name == SYSTEM)
            old_epoch = adapter.sources.epoch
            adapter.sources.remove(SYSTEM)
            # A replacement gets independent fresh data; the old request's
            # failure belongs to the previous source epoch even before repoll.
            now = sim.clock.monotonic()
            adapter.sources.observe(SYSTEM, '/Dc/System/Power', 123, now)
            self.assertGreater(adapter.sources.epoch, old_epoch)
            old_error(RuntimeError('late old-source failure'))
            self.assertEqual(adapter._value('system', '/Dc/System/Power', now), 123)

    def test_new_target_revokes_same_mode_pending_departure(self):
        with self.simulation(target=60, plant_config=PlantConfig(initial_soc=38)) as sim:
            sim.run(40)
            adapter = sim.rec.policy_adapter
            self.assertEqual(adapter.control['mode'], 'CHARGE')
            self.assertEqual(adapter.contract.request['target_soc'], 60)
            sim.solar_running = False
            adapter.transfer.pending = 1
            adapter.transfer.pending_since = sim.clock.monotonic()
            adapter.transfer.last_command = 1
            sim.set_target(40)
            sim.run(1)
            self.assertEqual(adapter.control['mode'], 'CHARGE')
            self.assertFalse(self.status(sim)['lease_valid'])
            self.assertEqual(adapter.ledger.snapshot()['references']['target_soc'], 40)
            self.assertEqual(adapter.transfer.last_command, 0)
            self.assertTrue(sim.plant.connected)

    def test_command_readback_alone_cannot_authorize_unsettled_departure(self):
        with self.simulation() as sim:
            original_step = sim.rec.voltage_control.step
            def step(*args, **kwargs):
                original_step(*args, **kwargs)
                sim.rec.voltage_control.ready = False
                return False
            sim.rec.voltage_control.step = step
            sim.run(350)
            self.assertFalse(self.status(sim)['ready'])
            self.assertFalse(any(not edge['connected'] for edge in sim.plant.relay_edges))

    def test_stale_quattro_current_cannot_authorize_new_departure(self):
        with self.simulation() as sim:
            adapter = sim.rec.policy_adapter
            original_observe = adapter.sources.observe
            def observe(service, path, value, now, **kwargs):
                if service == VEBUS and path == '/Dc/0/Current':
                    now -= 11
                original_observe(service, path, value, now, **kwargs)
            adapter.sources.observe = observe
            sim.run(350)
            self.assertFalse(self.status(sim)['source_valid'])
            self.assertFalse(any(not edge['connected'] for edge in sim.plant.relay_edges))

    def test_transfer_fault_is_durable_before_an_immediate_restart(self):
        for outcome in ('refusal', 'timeout'):
            with self.subTest(outcome=outcome), self.simulation() as sim, tempfile.TemporaryDirectory() as directory:
                sim.run(250)
                adapter = sim.rec.policy_adapter
                adapter.ledger.path = str(Path(directory) / 'state.json')
                now, wall = sim.clock.monotonic(), sim.clock.time()
                command = adapter.transfer._command(1, now, wall)
                if outcome == 'refusal':
                    sim.bus.refusals[IGNORE] = 1
                    adapter._relay(command, now, wall)
                else:
                    command = adapter.transfer.step('island', now + 31, wall + 31, ready=True, permitted=True)
                    self.assertEqual(command, 0)
                    adapter._relay(command, now + 31, wall + 31)
                saved = json.loads(Path(adapter.ledger.path).read_text())['controller_state']['transfer']
                self.assertGreater(saved['fault_until'], saved['logical_s'])
                self.assertEqual(saved['last_fault']['command'], 1)
                self.assertIn('refused' if outcome == 'refusal' else 'timeout', saved['last_fault']['reason'])

    def begin_shortfall_probe(self, sim):
        # HOLD clips connected PV to DC demand; this deficit fits the existing
        # 20 Wh admission, but actual450 W PV cannot carry the full island load.
        sim.set_load(ac_w=300, dc_w=200)
        sim.set_sun((250, 200))
        self.wait_for(sim, lambda: sim.rec.policy_adapter.probe_event is not None and
                      sim.rec.policy_adapter.probe_event['islanded'])
        self.assertFalse(sim.plant.connected)
        self.assertTrue(sim.rec.policy_adapter.probe_event['islanded'])
        self.assertFalse(sim.rec.policy_adapter.probe_event['completed'])

    def test_expired_consumer_lease_preserves_reference_and_rec_reconnects(self):
        with self.simulation() as sim:
            self.wait_for(sim, lambda: not sim.plant.connected)
            self.assertFalse(sim.plant.connected)
            before = sim.rec.policy_adapter.ledger.snapshot()['references']['high_wh']
            sim.solar_running = False
            sim.run(150)
            self.assertTrue(sim.plant.connected)
            self.assertFalse(self.status(sim)['lease_valid'])
            self.assertEqual(sim.rec.policy_adapter.control['mode'], 'CHARGE')
            after = sim.rec.policy_adapter.ledger.snapshot()['references']['high_wh']
            self.assertGreaterEqual(after, before)

    def test_battery_ledger_requires_new_can_measurement_frames(self):
        with self.simulation() as sim:
            sim.run(40)
            ledger = sim.rec.policy_adapter.ledger
            before = ledger.snapshot()['total']
            sim.missing_can.add(0x356)
            sim.run(5)
            self.assertEqual(ledger.snapshot()['total'], before)
            # Other live frames do not renew V/I or invent held-current energy.
            sim.run(70)
            self.assertEqual(ledger.snapshot()['total'], before)
            sim.missing_can.remove(0x356)
            sim.run(1)
            self.assertEqual(ledger.snapshot()['total'], before)
            sim.run(2)
            measured = ledger.snapshot()['total']
            self.assertGreater(measured['charge_wh'] + measured['discharge_wh'],
                               before['charge_wh'] + before['discharge_wh'])
            self.assertGreater(ledger.snapshot()['gap_count'], 0)

    def test_unrelated_protocol_advertiser_cannot_take_over_relay_ownership(self):
        with self.simulation() as sim:
            sim.rec.cfg.policy_consumer_instance = 999
            sim.run(40)
            self.assertFalse(sim.rec.policy_adapter.contract.owned)
            self.assertEqual(sim.plant.relay_edges, [])

    def test_lost_watchdog_parent_requests_safe_shutdown(self):
        with self.simulation() as sim:
            self.wait_for(sim, lambda: not sim.plant.connected)
            self.assertFalse(sim.plant.connected)
            sim.rec.heartbeat.pulse = lambda: False
            with self.assertRaisesRegex(SystemExit, 'watchdog supervisor disappeared'):
                sim.rec._tick()
            self.assertEqual(sim.rec.batt[CCL], 0)
            self.assertTrue(any(write['path'] == IGNORE and write['value'] == 0
                                for write in sim.bus.writes))

class LoadServicePulseTests(unittest.TestCase):
    def adapter(self):
        adapter = RecPolicyAdapter.__new__(RecPolicyAdapter)
        adapter.control = {}
        adapter.config = SimpleNamespace(stable_admission_s=60, source_gap_s=10, minimum_useful_pv_w=20)
        adapter.load_service = LoadServiceEvidence(60, 10, 20, 10)
        adapter.pv_activity = TimedMean(60)
        adapter.pv_activity_last = None
        return adapter

    def prove_pulsed_service(self, adapter):
        for now in range(65):
            pv, battery = (0, -400) if now % 5 == 4 else (500, 100)
            useful, proven = adapter._update_load_service(now, pv, battery, True)
        self.assertEqual(pv, 0)
        self.assertTrue(useful)
        self.assertTrue(proven)

    def test_balanced_measured_pulses_prove_service_even_on_zero_pv_tick(self):
        self.prove_pulsed_service(self.adapter())

    def test_invalid_sources_and_time_gaps_require_new_continuous_proof(self):
        for failure in ('invalid', 'gap'):
            with self.subTest(failure=failure):
                adapter = self.adapter()
                self.prove_pulsed_service(adapter)
                now = 65 if failure == 'invalid' else 80
                self.assertEqual(adapter._update_load_service(now, 0, 0, failure != 'invalid'), (False, False))
                self.assertEqual(adapter._update_load_service(now + 1, 0, 0, True), (False, False))
                useful, proven = adapter._update_load_service(now + 2, 500, 0, True)
                self.assertFalse(proven)

    def test_sustained_battery_deficit_revokes_proof_despite_recent_pv_activity(self):
        adapter = self.adapter()
        self.prove_pulsed_service(adapter)
        for now in range(65, 91):
            useful, proven = adapter._update_load_service(now, 0, -400, True)
        self.assertTrue(useful)
        self.assertFalse(proven)

class SolarAttributionBoundaryTests(unittest.TestCase):
    def adapter(self):
        adapter = RecPolicyAdapter.__new__(RecPolicyAdapter)
        adapter.driver = SimpleNamespace(cfg=SimpleNamespace(policy_mppt_instances=(278,)))
        adapter.config = SimpleNamespace(positive_reserve_w=20, source_alignment_s=2, current_settle_s=30, source_gap_s=10)
        adapter.control = {'quattro_v': 54, 'solar_v': 55, 'ccl_a': 30, 'actuator': {'settled': True}}
        adapter.attribution_observation = None
        adapter.attribution_island_since = None
        adapter.attribution_last_now = None
        adapter.sources = SourceRegistry(max_age_s=10)
        adapter.sources_names = {'vebus': 'q', 'system': 'system', 'solar278': 'pv'}
        for role, paths in (
                ('vebus', ('/Dc/0/Voltage', '/Dc/0/Current', '/Ac/ActiveIn/ActiveInput',
                           '/BatteryOperationalLimits/MaxChargeVoltage', '/BatteryOperationalLimits/MaxChargeCurrent')),
                ('system', ('/Ac/Consumption/L1/Power', '/Dc/System/Power')),
                ('solar278', ('/Yield/Power', '/Dc/0/Current', '/Dc/0/Voltage', '/Link/ChargeVoltage', '/Link/ChargeCurrent'))):
            for path in paths:
                adapter.sources.observe(adapter.sources_names[role], path, 1, 10)
        actuators = {'quattro_v': 54, 'quattro_current_limit_a': 20, 'quattro_power_w': 0,
                     'solar': [{'valid': True, 'voltage_limit_v': 55, 'current_limit_a': 30}]}
        adapter._solar_attribution(10, 10, True, actuators, True, 54, 1, 100)
        return adapter, actuators

    def test_only_stable_aligned_solar_interval_earns_credit(self):
        adapter, actuators = self.adapter()
        self.assertEqual(adapter._solar_attribution(10, 10, True, actuators, True, 54, 1, 100), 54)

    def test_coherent_solar_tracking_can_earn_credit_without_moving_quattro_or_current(self):
        adapter, actuators = self.adapter()
        adapter.control['actuator'].update(solar_tracking=True, regulation_authority='CURRENT',
                                           mppt_response_ready=True)
        adapter._solar_attribution(10, 10, True, actuators, True, 54, 1, 100)
        adapter.control['solar_v'] = 55.05
        actuators['solar'][0]['voltage_limit_v'] = 55.05
        self.assertEqual(adapter._solar_attribution(10, 10, True, actuators, True, 54, 1, 100), 54)

    def test_solar_tracking_does_not_bypass_current_quattro_or_source_evidence(self):
        for change in ('quattro', 'current', 'flag', 'authority', 'unready', 'stale'):
            with self.subTest(change=change):
                adapter, actuators = self.adapter()
                adapter.control['actuator'].update(solar_tracking=True, regulation_authority='CURRENT',
                                                   mppt_response_ready=True)
                adapter._solar_attribution(10, 10, True, actuators, True, 54, 1, 100)
                adapter.control['solar_v'] = 55.05
                actuators['solar'][0]['voltage_limit_v'] = 55.05
                if change == 'quattro':
                    adapter.control['quattro_v'] = 54.01
                elif change == 'current':
                    adapter.control['ccl_a'] = 31
                elif change == 'flag':
                    adapter.control['actuator']['solar_tracking'] = False
                elif change == 'authority':
                    adapter.control['actuator']['regulation_authority'] = 'VOLTAGE'
                elif change == 'unready':
                    adapter.control['actuator']['mppt_response_ready'] = False
                else:
                    adapter.sources.samples[('pv', '/Yield/Power')]['verified'] = 7
                self.assertIsNone(adapter._solar_attribution(10, 10, True, actuators, True, 54, 1, 100))

    def test_proven_inverter_earns_actual_solar_credit_during_current_regulation(self):
        adapter, actuators = self.adapter()
        adapter.control['actuator']['settled'] = False
        actuators['quattro_power_w'] = -100
        for now in range(11, 42):
            for sample in adapter.sources.samples.values():
                sample['verified'] = now
            adapter.sources.observe('q', '/Ac/ActiveIn/ActiveInput', 240, now)
            adapter.control['ccl_a'] = now % 2
            result = adapter._solar_attribution(now, now, False, actuators, True, 54, 1, 100)
            if now < 41:
                self.assertIsNone(result)
        self.assertEqual(result, 54)
        for sample in adapter.sources.samples.values():
            sample['verified'] = 60
        self.assertIsNone(adapter._solar_attribution(60, 60, False, actuators, True, 54, 1, 100))
        self.assertEqual(adapter.attribution_island_since, 60)
        for sample in adapter.sources.samples.values():
            sample['verified'] = 41
        # Source loss, positive Quattro output or another AC input revokes proof.
        for fault in ('source', 'charger', 'other_ac'):
            with self.subTest(fault=fault):
                old_power = actuators['quattro_power_w']
                if fault == 'charger':
                    actuators['quattro_power_w'] = 1
                elif fault == 'other_ac':
                    adapter.sources.observe('q', '/Ac/ActiveIn/ActiveInput', 1, 41)
                self.assertIsNone(adapter._solar_attribution(
                    41, 41, False, actuators, fault != 'source', 54, 1, 100))
                self.assertIsNone(adapter.attribution_island_since)
                actuators['quattro_power_w'] = old_power
                adapter.sources.observe('q', '/Ac/ActiveIn/ActiveInput', 240, 41)

    def test_burst_source_skew_or_changed_commands_withhold_credit(self):
        for change in ('burst', 'stale_pv', 'command', 'readback', 'feedback', 'unsettled', 'invalid'):
            with self.subTest(change=change):
                adapter, actuators = self.adapter()
                connected, eligible = True, True
                if change == 'burst':
                    actuators['quattro_power_w'] = 31
                elif change == 'stale_pv':
                    adapter.sources.samples[('pv', '/Yield/Power')]['verified'] = 7
                elif change == 'command':
                    adapter.control['solar_v'] = 55.01
                elif change == 'readback':
                    actuators['quattro_current_limit_a'] = 19
                elif change == 'feedback':
                    connected = False
                elif change == 'unsettled':
                    adapter.control['actuator']['settled'] = False
                else:
                    eligible = False
                self.assertIsNone(adapter._solar_attribution(10, 10, connected, actuators, eligible, 54, 1, 100))

class QuattroDcPowerTests(unittest.TestCase):
    """Signed V*I replaces raw /Dc/0/Power, which retains a settled residual.

    Boat evidence 2026-09-12: reported /Dc/0/Power held 25-26 W while measured
    current was 0.0 A at 54.98 V.
    """
    def adapter(self):
        adapter, _ = SolarAttributionBoundaryTests().adapter()
        adapter.demand_model = DemandModel()
        return adapter

    def test_quattro_dc_power_is_signed_voltage_times_current(self):
        adapter = self.adapter()
        adapter.sources.observe('q', '/Dc/0/Voltage', 54.98, 10)
        adapter.sources.observe('q', '/Dc/0/Current', 0.0, 10)
        adapter.sources.observe('q', '/Dc/0/Power', 26, 10)
        actuators = adapter._actuator_inputs(10)
        self.assertEqual(actuators['quattro_power_w'], 0.0)
        self.assertEqual(actuators['quattro_reported_power_w'], 26)
        self.assertTrue(adapter._sources_coherent(10, 10))

        # Islanded and inverting: negative current at similar voltage yields a
        # negative signed V*I, which the demand model turns into a positive
        # inverter DC demand exactly as tick() wires -inverter_power in.
        adapter.sources.observe('q', '/Ac/ActiveIn/ActiveInput', 240, 10)
        adapter.sources.observe('q', '/Dc/0/Current', -3.2, 10)
        power = adapter._quattro_dc_w(10)
        self.assertAlmostEqual(power, -175.936, places=2)
        demand = adapter.demand_model.estimate(
            0, 50, connected=False, inverter_dc_w=-power if power is not None else None)
        self.assertTrue(demand['valid'])
        self.assertEqual(demand['method'], 'measured inverter DC')
        self.assertAlmostEqual(demand['inverter_dc_w'], 175.936, places=2)

    def test_missing_or_stale_current_leaves_power_invalid_never_zero(self):
        for fault in ('missing', 'stale'):
            with self.subTest(fault=fault):
                adapter = self.adapter()
                adapter.sources.observe('q', '/Dc/0/Voltage', 54.98, 10)
                if fault == 'missing':
                    adapter.sources.observe('q', '/Dc/0/Current', None, 10)
                    query_now = 10
                else:
                    adapter.sources.observe('q', '/Dc/0/Current', 0.0, 10)
                    query_now = 25  # beyond the registry's 10s max age
                self.assertIsNone(adapter._quattro_dc_w(query_now))
                actuators = adapter._actuator_inputs(query_now)
                self.assertIsNone(actuators['quattro_power_w'])
                self.assertFalse(adapter._sources_coherent(query_now, query_now))

class VoltageSenseBoundaryTests(unittest.TestCase):
    def adapter(self):
        adapter, _ = SolarAttributionBoundaryTests().adapter()
        for path, value in (('/Connected', 1), ('/MppOperationMode', 1),
                            ('/Dc/0/Voltage', 54.9), ('/Link/VoltageSenseActive', 1),
                            ('/Link/VoltageSense', 55.0)):
            adapter.sources.observe('pv', path, value, 10)
        return adapter

    def test_active_remote_sense_is_part_of_valid_coherent_actuator_evidence(self):
        adapter = self.adapter()
        sample = adapter._actuator_inputs(10)['solar'][0]
        self.assertTrue(sample['valid'])
        self.assertEqual(sample['voltage_sense_v'], 55.0)
        self.assertEqual(sample['voltage_v'], 54.9)
        self.assertTrue(adapter._sources_coherent(10, 10))

    def test_missing_stale_or_invalid_selector_cannot_fall_back_to_local_voltage(self):
        for fault in ('missing', 'stale', 'invalid', 'source_loss'):
            with self.subTest(fault=fault):
                adapter = self.adapter()
                self.assertTrue(adapter._actuator_inputs(10)['solar'][0]['valid'])
                if fault == 'stale':
                    adapter.sources.samples[('pv', '/Link/VoltageSenseActive')]['verified'] = -1
                elif fault == 'source_loss':
                    adapter.sources.remove('pv')
                    for path, value in (('/Connected', 1), ('/MppOperationMode', 1),
                                        ('/Yield/Power', 10), ('/Dc/0/Current', .2),
                                        ('/Dc/0/Voltage', 54.9), ('/Link/ChargeVoltage', 55.3),
                                        ('/Link/ChargeCurrent', 1)):
                        adapter.sources.observe('pv', path, value, 10)
                else:
                    adapter.sources.observe('pv', '/Link/VoltageSenseActive',
                                            None if fault == 'missing' else 7, 10)
                self.assertFalse(adapter._actuator_inputs(10)['solar'][0]['valid'])
                self.assertFalse(adapter._sources_coherent(10, 10))

    def test_active_sense_requires_fresh_aligned_positive_voltage(self):
        for value, stamp in ((None, 10), (0, 10), (-1, 10), (55, -1), (55, 7)):
            with self.subTest(value=value, stamp=stamp):
                adapter = self.adapter()
                adapter.sources.observe('pv', '/Link/VoltageSense', value, stamp)
                sample = adapter._actuator_inputs(10)['solar'][0]
                self.assertFalse(sample['valid'] and adapter._sources_coherent(10, 10))

    def test_fresh_inactive_selector_explicitly_permits_local_voltage(self):
        adapter = self.adapter()
        adapter._actuator_inputs(10)
        adapter.sources.observe('pv', '/Link/VoltageSenseActive', 0, 10)
        adapter.sources.observe('pv', '/Link/VoltageSense', None, 10)
        self.assertTrue(adapter._actuator_inputs(10)['solar'][0]['valid'])
        self.assertTrue(adapter._sources_coherent(10, 10))

    def test_unsupported_remote_sense_does_not_invalidate_local_only_charger(self):
        adapter = self.adapter()
        adapter.sources.samples.pop(('pv', '/Link/VoltageSenseActive'))
        adapter.sources.samples.pop(('pv', '/Link/VoltageSense'))
        self.assertTrue(adapter._actuator_inputs(10)['solar'][0]['valid'])
        self.assertTrue(adapter._sources_coherent(10, 10))

if __name__ == '__main__':
    unittest.main(verbosity=2)
