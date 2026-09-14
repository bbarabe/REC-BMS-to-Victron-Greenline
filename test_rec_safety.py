#!/usr/bin/env python3
"""REC safety regressions, including asynchronous DVCC application ordering."""
import itertools
import os
import struct
import types
import unittest

from test_stubs import FakeBus, FakeService, REPO, load

R = load(os.path.join(REPO, 'dbus-recbms', 'dbus_recbms.py'), 'rec_safety_driver')


class VoltagePlant:
    def __init__(self, base=56.4, offset=0.3, safe=62.4):
        self.base = self.applied_base = base
        self.offset = self.applied_offset = offset
        self.safe = safe
        self.events = []
        self.unreadable = False
        self.refused = False
        self.readback_lag = False
        self.control = R.VoltageEnvelope(self.publish, self.write, self.read, self.applied)
        self.control.base = base

    def publish(self, base):
        self.events.append(('base', base))
        self.base = base
        # Even before the offset changes, the published pair obeys the cap.
        if not self.unreadable:
            assert base + max(0, self.offset) <= self.safe + 1e-8

    def write(self, offset):
        self.events.append(('offset', offset))
        # The new offset cannot overtake a pending base reduction.
        assert self.applied_base + max(0, offset) <= self.safe + 1e-8
        if self.refused:
            return False
        if not self.readback_lag:
            self.offset = offset
        return True

    def read(self):
        return None if self.unreadable else self.offset

    def applied(self, base, offset):
        return (abs(self.applied_base - base) < 0.001 and
                abs(self.applied_offset - offset) < 0.001)

    def settle(self):
        self.applied_base, self.applied_offset = self.base, self.offset

    def converge(self, quattro, solar):
        for _ in range(8):
            if self.control.step(quattro, solar, self.safe):
                return True
            self.settle()
        return False


def feed_complete(bms, now=100.0, cvl=62.7, current=0.0):
    for canid, data in (
        (0x351, struct.pack('<HHHH', round(cvl * 10), 2000, 4000, 480)),
        (0x355, struct.pack('<HHH', 60, 100, 6001)),
        (0x356, struct.pack('<hhhH', 5660, round(current * 10), 210, 13)),
        (0x372, struct.pack('<HHHH', 8, 0, 0, 0)),
        (0x373, struct.pack('<HHhh', 3700, 3750, 293, 295)),
    ):
        R.decode_frame(bms, canid, data, received_at=now)


class CanValidityTests(unittest.TestCase):
    def test_identity_heartbeat_cannot_refresh_critical_groups(self):
        bms = {}
        feed_complete(bms)
        for frame in (0x35E, 0x35F, 0x360, 0x370, 0x371, 0x374,
                      0x375, 0x376, 0x377, 0x379, 0x380, 0x381, 0x404):
            R.decode_frame(bms, frame, b'12345678', received_at=200)
        self.assertTrue(all(not v['valid'] for v in R.rec_health(bms, 200, 60).values()))
        self.assertNotIn('_lastUpdate', bms)

    def test_constant_valid_samples_and_partial_recovery(self):
        bms = {}
        feed_complete(bms)
        R.decode_frame(bms, 0x351, struct.pack('<HHHH', 627, 2000, 4000, 480), received_at=180)
        health = R.rec_health(bms, 181, 60)
        self.assertTrue(health['Limits']['valid'])
        self.assertFalse(health['Measurements']['valid'])
        feed_complete(bms, 190)
        self.assertTrue(all(v['valid'] for v in R.rec_health(bms, 200, 60).values()))

    def test_short_frame_does_not_refresh_and_short_soc_clears_high_resolution(self):
        bms = {}
        feed_complete(bms)
        self.assertFalse(R.decode_frame(bms, 0x351, b'bad', received_at=200))
        self.assertEqual(bms['_received']['Limits'], 100)
        R.decode_frame(bms, 0x355, struct.pack('<HH', 59, 100), received_at=200)
        self.assertIsNone(bms['socHiRes'])

    def test_invalid_payload_and_future_timestamp_do_not_become_live(self):
        bms = {}
        feed_complete(bms)
        bms['cvl'] = 0
        bms['_received']['Soc'] = 300
        health = R.rec_health(bms, 100, 60)
        self.assertFalse(health['Limits']['valid'])
        self.assertFalse(health['Soc']['valid'])


class EnvelopeTests(unittest.TestCase):
    def test_all_offset_permutations(self):
        for old, new, safe in itertools.product((0, .15, .3, .6), (-.15, 0, .15, .3, .6), (56.7, 62.4)):
            with self.subTest(old=old, new=new, safe=safe):
                plant = VoltagePlant(base=safe - max(0, old), offset=old, safe=safe)
                quattro = safe - max(0, new)
                solar = quattro + new
                self.assertTrue(plant.converge(quattro, solar))
                self.assertLessEqual(plant.base, safe)
                self.assertLessEqual(plant.base + plant.offset, safe + 1e-8)

    def test_reproduced_boost_56_7_to_57_is_clamped(self):
        plant = VoltagePlant(base=56.4, offset=.3, safe=56.7)
        self.assertTrue(plant.converge(56.55, 57.0))
        self.assertAlmostEqual(plant.base + plant.offset, 56.7)

    def test_reduced_rec_limit_reduces_base_before_offset_and_waits(self):
        plant = VoltagePlant(base=61.96, offset=.44, safe=56.7)
        self.assertFalse(plant.control.step(56.7, 56.7, 56.7))
        self.assertEqual(plant.events, [('base', 56.26)])
        plant.settle()
        self.assertFalse(plant.control.step(56.7, 56.7, 56.7))
        self.assertEqual(plant.events[1:], [('offset', 0)])
        plant.settle()
        self.assertFalse(plant.control.step(56.7, 56.7, 56.7))
        self.assertEqual(plant.events[-1], ('base', 56.7))
        plant.settle()
        self.assertTrue(plant.control.step(56.7, 56.7, 56.7))

    def test_limit_reduction_during_offset_application_retains_old_margin(self):
        plant = VoltagePlant(base=61.96, offset=.44)
        self.assertFalse(plant.control.step(62.4, 62.4, 62.4))
        self.assertEqual(plant.offset, 0)
        self.assertEqual(plant.applied_offset, .44)
        plant.safe = 56.7
        self.assertFalse(plant.control.step(56.7, 56.7, 56.7))
        self.assertEqual(plant.base, 56.26)
        self.assertLessEqual(plant.base + plant.applied_offset, 56.7)
        self.assertTrue(plant.converge(56.7, 56.7))

    def test_unknown_offset_never_authorizes_higher_base(self):
        plant = VoltagePlant(base=61.96, offset=.44)
        plant.unreadable = True
        self.assertFalse(plant.control.step(61.96, 61.96, 62.4))
        self.assertEqual(plant.base, 0)
        self.assertEqual(plant.events, [('base', 0)])
        plant.unreadable = False
        self.assertTrue(plant.converge(61.96, 61.96))

    def test_failed_offset_clear_retains_safe_intermediate(self):
        plant = VoltagePlant(base=56.4, offset=.3, safe=56.7)
        plant.refused = True
        for _ in range(4):
            self.assertFalse(plant.control.step(56.7, 56.7, 56.7))
            plant.settle()
        self.assertEqual(plant.base, 56.4)
        self.assertEqual(plant.offset, .3)

    def test_accepted_write_without_matching_readback_is_pending(self):
        plant = VoltagePlant(base=56.4, offset=.3, safe=56.7)
        plant.readback_lag = True
        self.assertFalse(plant.control.step(56.7, 56.7, 56.7))
        self.assertEqual(plant.base, 56.4)
        self.assertIn('readback pending', plant.control.status)

    def test_rounding_cannot_exceed_non_centivolt_limit(self):
        plant = VoltagePlant(base=56, offset=0, safe=56.705)
        self.assertTrue(plant.converge(56.705, 56.705))
        self.assertEqual(plant.base, 56.7)


class RecDriverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.old_time = R.time
        self.now = 1000.0
        R.time = types.SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.now)
        self.cfg = R.Config(os.path.join(REPO, 'dbus-recbms', 'config.ini'))
        self.cfg.policy_state_path = None
        self.driver = R.RecBmsDriver(self.cfg)
        self.driver.settings['eqlast'] = self.now
        self.driver.settings['chargeslider'] = 100
        self.driver.sp_enabled = False
        self.plant = VoltagePlant()
        self.driver.voltage_control = R.VoltageEnvelope(
            lambda base: (self.plant.publish(base), self.driver._publish_voltage_base(base)),
            self.plant.write, self.plant.read, self.plant.applied)
        feed_complete(self.driver.bms, self.now)

    def tearDown(self):
        # Registered shutdown callbacks may run after the test's fake time ends.
        R.atexit.unregister(self.driver._boost_shutdown)
        R.time = self.old_time

    def tick(self):
        self.driver._tick()
        self.plant.settle()

    def test_heartbeat_only_driver_falls_back_without_charging(self):
        for _ in range(3):
            self.tick()
        self.assertEqual(self.driver.batt['/RecBms/Phase'], 'LIVE')
        self.now += 61
        R.decode_frame(self.driver.bms, 0x404, b'\xff', received_at=self.now)
        self.tick()
        self.assertEqual(self.driver.batt['/RecBms/Health/CriticalValid'], 0)
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)
        self.assertLessEqual(self.driver.batt['/RecBms/SafeChargeVoltage'], 54)
        self.assertIsNone(self.driver.batt['/RecBms/Raw/Current'])

    def test_boost_and_sustain_expire_on_monotonic_time_despite_wall_clock_steps(self):
        # E10: after an accepted boost a -1 h wall-clock step left it active
        # at 132 monotonic seconds, reporting 3588 s remaining.
        wall = [self.now]
        R.time = types.SimpleNamespace(time=lambda: wall[0], monotonic=lambda: self.now)
        self.driver.settings['chargeslider'] = 80
        for _ in range(3):
            self.tick()
        self.assertTrue(self.driver._set_boost(.3))
        self.assertTrue(self.driver._set_sustain(1))
        self.assertEqual(self.driver.sustain['servo_ts'], self.now)
        self.assertEqual(self.driver.boost['req_ts'], self.now)
        start = self.now
        for step in (-3600, 7200):
            wall[0] += step
            for _ in range(59):
                self.now += 1
                feed_complete(self.driver.bms, self.now)
                self.tick()
                elapsed = self.now - start
                self.assertEqual(self.driver.batt['/RecBms/SolarBoost/Active'], 1, elapsed)
                self.assertEqual(self.driver.batt['/RecBms/Sustain/Active'], 1, elapsed)
                left = self.driver.batt['/RecBms/SolarBoost/SecondsLeft']
                self.assertTrue(0 <= left <= self.cfg.boost_hold_s - elapsed + 1, (elapsed, left))
                self.assertLessEqual(self.driver.batt['/RecBms/Sustain/SecondsLeft'], self.cfg.sustain_hold_s - elapsed + 1)
        self.now += 3
        feed_complete(self.driver.bms, self.now)
        self.tick()
        self.assertEqual(self.driver.batt['/RecBms/SolarBoost/Active'], 0)
        self.assertIn('expired', self.driver.batt['/RecBms/SolarBoost/Status'])
        self.assertEqual(self.driver.batt['/RecBms/Sustain/Active'], 0)
        self.assertIn('expired', self.driver.batt['/RecBms/Sustain/Status'])

    def test_raw_current_is_unquantized_and_limits_are_external(self):
        feed_complete(self.driver.bms, self.now, current=.1)
        self.tick()
        self.assertEqual(self.driver.batt['/RecBms/Raw/Current'], .1)
        self.assertEqual(self.driver.batt['/RecBms/Raw/ChargeVoltageLimit'], 62.7)
        self.assertEqual(self.driver.batt['/RecBms/SafeChargeVoltage'], 62.4)

    def test_all_modes_obey_installation_cap_and_near_limit_inhibits_charge(self):
        for _ in range(4):
            self.tick()
        self.assertIn('disabled', self.driver.batt['/RecBms/EqStatus'])
        self.assertLessEqual(self.plant.base + self.plant.offset, 62.4)
        self.driver.bms['voltage'] = 62.35
        self.tick()
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)

    def test_missing_modules_prevents_charging_and_boost(self):
        del self.driver.bms['_received']['Modules']
        self.tick()
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)
        self.assertFalse(self.driver._boost_allowed(.3)[0])

    def test_raw_charge_ban_tightens_both_commands_before_offset_change(self):
        self.driver.bms['ccl'] = 0
        self.driver.batt['/Info/MaxChargeCurrent'] = 100
        limit = self.driver.bms['voltage'] - self.cfg.voltage_guard_v
        _, ready = self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
        self.assertFalse(ready)
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)
        self.assertEqual(self.plant.events[0][0], 'base')
        self.assertFalse(any(kind == 'offset' for kind, _ in self.plant.events))
        self.assertLessEqual(self.plant.base + self.plant.offset, limit + 1e-8)
        self.assertLessEqual(self.driver.voltage_control.requested_quattro, limit)
        self.assertLessEqual(self.driver.voltage_control.requested_solar, limit)
        self.assertIn('charge prohibited', self.driver.batt['/RecBms/Voltage/Status'])
        for _ in range(5):
            self.plant.settle()
            self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
        self.assertTrue(self.driver.voltage_control.ready)
        self.assertEqual(self.plant.offset, 0)
        self.assertLessEqual(self.plant.base, limit)

    def test_discretionary_zero_current_is_not_a_raw_charge_ban(self):
        self.driver.batt['/Info/MaxChargeCurrent'] = 0
        self.driver._apply_voltage_commands(56.4, 56.7, 62.4, self.now)
        self.assertEqual(self.driver.charge_guard_reason, '')
        self.assertEqual(self.driver.voltage_control.requested_solar, 56.7)

    def test_charge_ban_preserves_a_lower_fresh_rec_voltage_limit(self):
        self.driver.bms.update(ccl=0, cvl=55.0)
        self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
        self.assertLessEqual(self.plant.base + self.plant.offset, 55.0 + 1e-8)
        self.assertEqual(self.driver.voltage_control.requested_solar, 55.0)

    def test_missing_or_invalid_bank_uses_finite_zero_voltage_fallback(self):
        for invalid in ('stale', 'missing', 'nan'):
            with self.subTest(invalid=invalid):
                feed_complete(self.driver.bms, self.now)
                if invalid == 'stale':
                    self.driver.bms['_received']['Measurements'] = self.now - self.cfg.live_timeout - 1
                elif invalid == 'missing':
                    self.driver.bms.pop('voltage')
                else:
                    self.driver.bms['voltage'] = float('nan')
                self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
                self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)
                self.assertEqual(self.driver.voltage_control.requested_quattro, 0)
                self.assertEqual(self.driver.voltage_control.requested_solar, 0)
                self.assertEqual(self.plant.base, 0)
                self.assertIn('critical REC data unavailable', self.driver.charge_guard_reason)

    def test_failed_offset_write_during_ban_retains_tightened_intermediate(self):
        self.driver.bms['modulesBlockingCharge'] = 1
        self.plant.refused = True
        for _ in range(4):
            self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
            self.plant.settle()
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)
        self.assertFalse(self.driver.voltage_control.ready)
        self.assertLessEqual(self.plant.base + self.plant.offset,
                             self.driver.bms['voltage'] - self.cfg.voltage_guard_v + 1e-8)
        self.assertIn('module charging prohibition', self.driver.charge_guard_reason)
        self.assertIn('offset write refused', self.driver.batt['/RecBms/Voltage/Status'])

    def test_valid_charge_permission_releases_only_the_temporary_voltage_guard(self):
        self.driver.bms['ccl'] = 0
        self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
        self.driver.bms['ccl'] = 200
        self.driver._apply_voltage_commands(61.96, 62.4, 62.4, self.now)
        self.assertEqual(self.driver.charge_guard_reason, '')
        self.assertEqual(self.driver.voltage_control.requested_solar, 62.4)

    def test_driver_rejects_nonzero_setvalue_and_unchanged_readback(self):
        bus = self.driver.sbus
        original = bus.call_blocking
        def refused(service, path, iface, method, sig, args, timeout=None):
            return 1 if method == 'SetValue' else .3
        bus.call_blocking = refused
        self.assertFalse(self.driver._boost_write(0))
        def unchanged(service, path, iface, method, sig, args, timeout=None):
            return 0 if method == 'SetValue' else .3
        bus.call_blocking = unchanged
        self.assertFalse(self.driver._boost_write(0))
        bus.call_blocking = original

    def voltage_feedback(self, value):
        FakeBus.names.pop(self.cfg.boost_service, None)
        FakeBus.names.pop('com.victronenergy.solarcharger.safetytest', None)
        system = FakeService(self.cfg.boost_service)
        system.add_path('/Control/EffectiveChargeVoltage', value)
        charger = FakeService('com.victronenergy.solarcharger.safetytest')
        charger.add_path('/Connected', 1)
        charger.add_path('/Link/ChargeVoltage', value)
        return system, charger

    def test_safe_pending_servo_retains_current_but_unknown_offset_inhibits(self):
        self.voltage_feedback(56.7)
        self.driver.batt['/Info/MaxChargeCurrent'] = 10
        _, ready = self.driver._apply_voltage_commands(56.5, 56.8, 62.4, self.now)
        self.assertFalse(ready)
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 10)
        self.plant.unreadable = True
        self.driver._apply_voltage_commands(56.5, 56.8, 62.4, self.now)
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)

    def test_reduced_live_ceiling_inhibits_current_until_old_effective_limit_is_safe(self):
        self.voltage_feedback(56.7)
        self.driver.batt['/Info/MaxChargeCurrent'] = 10
        self.plant.safe = 56.6
        self.driver._apply_voltage_commands(56.6, 56.6, 56.6, self.now)
        self.assertEqual(self.driver.batt['/Info/MaxChargeCurrent'], 0)

    def test_readback_tolerance_cannot_authorize_above_hard_ceiling(self):
        self.voltage_feedback(62.41)
        self.assertFalse(self.driver._voltage_applied(61.96, .44))

    def test_charger_feedback_required_even_when_dvcc_matches(self):
        system = FakeService(self.cfg.boost_service)
        system.add_path('/Control/EffectiveChargeVoltage', 56.7)
        charger = FakeService('com.victronenergy.solarcharger.safetytest')
        charger.add_path('/Connected', 1)
        charger.add_path('/Link/ChargeVoltage', 57)
        self.assertFalse(self.driver._voltage_applied(56.4, .3))
        charger['/Link/ChargeVoltage'] = 56.7
        self.assertTrue(self.driver._voltage_applied(56.4, .3))
        charger['/Link/ChargeVoltage'] = None
        self.assertFalse(self.driver._voltage_applied(56.4, .3))


if __name__ == '__main__':
    unittest.main(verbosity=2)
