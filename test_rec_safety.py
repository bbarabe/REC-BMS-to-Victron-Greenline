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
        # Past the boost's own expiry (240 s since 2026-09-15), on monotonic time.
        self.now += max(3, self.cfg.boost_hold_s - (self.now - start) + 1)
        feed_complete(self.driver.bms, self.now)
        self.tick()
        self.assertEqual(self.driver.batt['/RecBms/SolarBoost/Active'], 0)
        self.assertIn('expired', self.driver.batt['/RecBms/SolarBoost/Status'])
        self.assertEqual(self.driver.batt['/RecBms/Sustain/Active'], 0)
        self.assertIn('expired', self.driver.batt['/RecBms/Sustain/Status'])

    def test_two_sided_hold_anchors_like_a_floor_and_holds_the_slider(self):
        # Stage B/B2: ordinary HOLD used to release sustain, leaving the
        # slider curve with the standing 0.15 V lead under it -- the Quattro
        # below the bank, a steady -49 W, 1.448 points in 24 h (E04/D03).
        # Mode 3 anchors on the measured bank like a floor, reports the
        # DESTINATION as its held SOC, and makes the lead in force its own
        # band so the Quattro is commanded the hold voltage itself.
        self.driver.settings['chargeslider'] = 80
        self.driver.sp_enabled = True
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        self.tick()
        batt = self.driver.batt
        self.assertEqual(batt['/RecBms/Sustain/Mode'], 3)
        self.assertEqual(batt['/RecBms/Sustain/Status'], 'hold')
        self.assertEqual(batt['/RecBms/Sustain/Soc'], 80)
        hold_v = batt['/RecBms/Sustain/HoldVoltage']
        self.assertAlmostEqual(hold_v, 56.6, places=2)
        # 60.01 % is well under the 80 % destination: the sun gets its band.
        self.assertAlmostEqual(self.driver.lead_v, self.cfg.sustain_band_v, places=3)
        self.assertAlmostEqual(batt['/RecBms/TargetChargeVoltage'],
                               hold_v + self.cfg.sustain_band_v, places=2)
        self.assertAlmostEqual(self.plant.base, hold_v, places=2)
        # At the destination (3.6.0) the band closes: the lead in force is
        # nothing, every charger is commanded the hold voltage and the bank
        # is regulated by that voltage. The charge limit stays the floor's
        # PV + charge_limit_a -- a real constraint, never the near-zero
        # figure that had DVCC switch an MPPT off (boat, 2026-09-15).
        self.driver.settings['chargeslider'] = 60
        self.tick()
        self.assertEqual(self.driver.lead_v, 0.0)
        hold_v = batt['/RecBms/Sustain/HoldVoltage']
        self.assertAlmostEqual(batt['/RecBms/TargetChargeVoltage'], hold_v, places=2)
        self.assertAlmostEqual(self.plant.base, hold_v, places=2)
        self.assertEqual(batt['/RecBms/Sustain/Soc'], 60)
        # the new pair verifies on the next ticks; the limit is then the floor's
        self.tick()
        self.tick()
        self.assertGreaterEqual(batt['/Info/MaxChargeCurrent'], self.cfg.sustain_ccl_a)
        self.assertGreaterEqual(batt['/RecBms/Sustain/ChargeLimit'] or self.cfg.sustain_ccl_a,
                                self.cfg.sustain_ccl_a)

    def test_a_hold_arriving_at_its_destination_folds_its_servo_into_a_fresh_anchor(self):
        # Boat, 2026-09-14 22:25-23:40 UTC: the first HOLD (50 %) was taken
        # at 49.1 % on shore, the servo wound +0.50 V lifting the bank, and
        # at 49.9 % the Quattro sat 0.45 V above a bank it may no longer
        # fill, restrained only by the current limit -- 2 A for minutes on
        # a 0 A limit, and 6 A into the bank the moment a solar boost
        # lifted it. 3.3.1: arriving re-anchors on the bank's rest voltage
        # and folds the servo, once per arrival, from either side.
        self.driver.settings['chargeslider'] = 50
        self.driver.sp_enabled = True
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        su = self.driver.sustain
        r = self.cfg.sustain_anchor_r
        self.driver._sustain_anchor(49.1, 55.55 + 12.0 * r, 12.0, 'test')
        self.assertAlmostEqual(su['anchor_v'], 55.55, places=2)

        def drive(soc, volts, amps):
            self.driver._set_sustain(3)                    # the owner re-asserts its lease
            return self.driver._service_sustain(self.now, soc, 50.0, volts, amps)

        for _ in range(30):                                # the lift: 0.02 V per period
            self.now += self.cfg.sustain_servo_s
            drive(49.1, 55.6, 12.0)
        self.assertAlmostEqual(su['servo_v'], self.cfg.sustain_servo_up, places=3)
        self.assertAlmostEqual(su['anchor_v'], 55.55, places=2)
        # Still a deadband short of the destination: nothing folds yet.
        self.now += 1
        drive(49.8, 55.6, 12.0)
        self.assertAlmostEqual(su['servo_v'], self.cfg.sustain_servo_up, places=3)
        self.assertAlmostEqual(su['anchor_v'], 55.55, places=2)
        # Arrival from below at 5 A: the hold IS the bank's rest voltage,
        # the servo is gone, the Quattro is commanded onto the bank.
        self.now += 1
        target = drive(49.9, 55.6, 5.0)
        self.assertAlmostEqual(su['anchor_v'], 55.6 - 5.0 * r, delta=.006)
        self.assertEqual(su['servo_v'], 0.0)
        self.assertAlmostEqual(self.driver.batt['/RecBms/Sustain/HoldVoltage'], su['anchor_v'], places=2)
        self.assertAlmostEqual(target, su['anchor_v'] + self.driver._sustain_band(
            getattr(self.driver, 'band_available', False), 49.9, 50.0), places=2)
        # Inside the band the anchor stands whatever the bank reads.
        folded = su['anchor_v']
        for soc, volts in ((50.0, 55.7), (49.95, 55.5), (50.05, 55.8)):
            self.now += 1
            drive(soc, volts, 0.0)
            self.assertAlmostEqual(su['anchor_v'], folded, places=2)
            self.assertEqual(su['servo_v'], 0.0)
        # The band's edge (3.3.2): the bank drains a hair under, the servo
        # steps up while it drains and stops once it sits still; the few
        # hundredths it keeps to cover the loads are never folded when the
        # bank touches 49.9 % again -- folding them re-anchored the hold
        # every 30 s all night on the boat.
        for _ in range(3):
            self.now += self.cfg.sustain_servo_s
            drive(49.89, 55.6, -0.9)
        self.assertAlmostEqual(su['servo_v'], 3 * self.cfg.sustain_servo_v, places=3)
        self.now += self.cfg.sustain_servo_s
        drive(49.89, 55.6, 0.0)                            # covered: still
        self.assertAlmostEqual(su['servo_v'], 3 * self.cfg.sustain_servo_v, places=3)
        for soc in (49.9, 49.89, 49.9, 50.0):
            self.now += 1
            drive(soc, 55.6, 0.0)
            self.assertAlmostEqual(su['servo_v'], 3 * self.cfg.sustain_servo_v, places=3)
            self.assertAlmostEqual(su['anchor_v'], folded, places=2)
        su['servo_v'] = 0.0
        # Arrival from above with the servo wound down folds the same way;
        # a bank still outside the band does not.
        su['servo_v'] = -0.3
        self.now += 1
        drive(50.3, 55.8, -1.0)
        self.assertAlmostEqual(su['servo_v'], -0.3, places=3)
        self.assertAlmostEqual(su['anchor_v'], folded, places=2)
        self.now += 1
        drive(50.1, 55.8, -1.0)
        self.assertAlmostEqual(su['anchor_v'], 55.8 + 1.0 * r, delta=.006)
        self.assertEqual(su['servo_v'], 0.0)
        # A floor never folds: its servo is not an arrival mechanism.
        self.assertTrue(self.driver._set_sustain(0))
        self.driver.settings['chargeslider'] = 60
        self.assertTrue(self.driver._set_sustain(1))
        su = self.driver.sustain                            # a release makes a fresh state
        self.driver._sustain_anchor(49.1, 55.55, 0.0, 'test')
        su['servo_v'] = 0.3
        self.now += 1
        self.driver._set_sustain(1)
        self.driver._service_sustain(self.now, 49.1, 60.0, 55.6, 0.0)
        self.assertAlmostEqual(su['servo_v'], 0.3, places=3)
        self.assertAlmostEqual(su['anchor_v'], 55.55, places=2)

    def test_a_hold_closes_its_band_on_arrival_and_reopens_it_two_deadbands_under(self):
        # 3.6.0 (Victron: CVL is the top-of-charge control, CCL a genuine
        # current constraint). Under its destination a hold gives the sun
        # the band and the floor's cap; arriving at target - deadband
        # closes the band, re-anchors on the bank's rest voltage whatever
        # the servo carries, and from there the hold voltage regulates.
        # The latch reopens only two deadbands under, so a bank dithering
        # on the edge never flips the MPPT ceiling by band_v.
        self.driver.settings['chargeslider'] = 50
        self.driver.sp_enabled = True
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        su = self.driver.sustain
        r = self.cfg.sustain_anchor_r
        self.driver._sustain_anchor(49.5, 55.5, 0.0, 'test')
        self.assertIsNone(su['at_dest'])

        def drive(soc, volts, amps):
            self.now += 1
            self.driver._set_sustain(3)
            return self.driver._service_sustain(self.now, soc, 50.0, volts, amps)

        # Under the destination: the band is open, the sun fills at the cap.
        target = drive(49.5, 55.5, 8.0)
        self.assertIs(su['at_dest'], False)
        self.assertAlmostEqual(target, su['anchor_v'] + self.cfg.sustain_band_v, places=2)
        self.assertAlmostEqual(self.driver._sustain_band(True, 49.5, 50.0),
                               self.cfg.sustain_band_v, places=3)
        drive(49.85, 55.55, 8.0)                            # a deadband and a half under: still open
        self.assertIs(su['at_dest'], False)
        self.assertAlmostEqual(su['anchor_v'], 55.5, places=2)
        # Arrival at 49.9 %: the band closes and the hold IS the bank's
        # rest voltage now, even with no servo to fold.
        target = drive(49.9, 55.6, 8.0)
        self.assertIs(su['at_dest'], True)
        self.assertAlmostEqual(su['anchor_v'], 55.6 - 8.0 * r, delta=.006)
        self.assertEqual(su['servo_v'], 0.0)
        self.assertFalse(su['arrived'])
        self.assertAlmostEqual(target, su['anchor_v'], places=2)
        self.assertEqual(self.driver._sustain_band(True, 49.9, 50.0), 0.0)
        folded = su['anchor_v']
        # Dithering on the edge neither reopens the band nor re-anchors.
        for soc in (49.89, 49.9, 49.85, 49.95, 49.81, 50.2):
            target = drive(soc, 55.6, 0.0)
            self.assertIs(su['at_dest'], True, soc)
            self.assertAlmostEqual(su['anchor_v'], folded, places=2)
            self.assertAlmostEqual(target, folded + su['servo_v'], places=2)
        # Two deadbands under: the band reopens so the sun can finish again.
        target = drive(49.79, 55.55, -1.0)
        self.assertIs(su['at_dest'], False)
        self.assertAlmostEqual(target, folded + su['servo_v'] + self.cfg.sustain_band_v, places=2)
        # ... and the next arrival folds again.
        drive(49.9, 55.62, 4.0)
        self.assertIs(su['at_dest'], True)
        self.assertAlmostEqual(su['anchor_v'], 55.62 - 4.0 * r, delta=.006)
        # A hold taken at or above its destination is there from the start:
        # no band, and no "arrival" to fold.
        self.assertTrue(self.driver._set_sustain(0))
        self.assertTrue(self.driver._set_sustain(3))
        su = self.driver.sustain
        self.driver._sustain_anchor(50.3, 55.7, 0.0, 'test')
        target = drive(50.3, 55.75, 0.0)
        self.assertIs(su['at_dest'], True)
        self.assertFalse(su['arrived'])
        self.assertAlmostEqual(su['anchor_v'], 55.7, places=2)
        self.assertAlmostEqual(target, 55.7, places=2)

    def test_a_hold_servo_answers_a_fill_above_target_from_any_source(self):
        # 3.6.0: at the destination the hold regulates by voltage, so the
        # sun CAN fill the bank there when the hold voltage sits over it
        # (the Quattro's bias over its command does the same at night); a
        # fill above the target is the sign that it does, and the servo
        # steps the voltage down whoever is filling. A bank sitting still
        # or draining above the target is left to the loads.
        self.driver.settings['chargeslider'] = 50
        self.driver.sp_enabled = True
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        su = self.driver.sustain
        self.driver._sustain_anchor(50.0, 55.6, 0.0, 'test')

        def period(soc, amps):
            self.now += self.cfg.sustain_servo_s
            self.driver._set_sustain(3)
            self.driver._service_sustain(self.now, soc, 50.0, 55.6, amps)

        step = self.cfg.sustain_servo_v
        for _ in range(3):
            period(50.2, 2.0)                               # PV filling above target: down
        self.assertAlmostEqual(su['servo_v'], -3 * step, places=3)
        period(50.2, -1.0)                                  # draining above: left to the loads
        period(50.2, 0.0)                                   # sitting above: left alone
        self.assertAlmostEqual(su['servo_v'], -3 * step, places=3)
        period(50.05, 0.0)                                  # inside the deadband: nothing
        self.assertAlmostEqual(su['servo_v'], -3 * step, places=3)
        for _ in range(2):
            period(49.85, -1.0)                             # draining under: up, as the floor
        self.assertAlmostEqual(su['servo_v'], -1 * step, places=3)
        period(49.85, 0.0)                                  # covered: still
        period(49.85, 0.5)                                  # the sun finishing: still
        self.assertAlmostEqual(su['servo_v'], -1 * step, places=3)

    def test_a_hold_stands_still_while_a_boost_measures(self):
        # Boat, 2026-09-15 20:50 UTC, the first boost on 3.4.1: the lifted
        # limit let the arrays fill the held bank at 6 A for two minutes
        # and the hold's regulation answered that fill as if it were the
        # world's doing. A boost is our own measurement: the servo may not
        # regulate on it (3.4.2; the current trim that also stood still
        # then is gone in 3.6.0). It resumes, on a fresh period, once the
        # boost ends.
        self.driver.settings['chargeslider'] = 50
        self.driver.sp_enabled = True
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        su = self.driver.sustain
        self.driver._sustain_anchor(50.0, 55.6, 0.0, 'test')

        def period(soc, amps):
            self.now += self.cfg.sustain_servo_s
            self.driver._set_sustain(3)
            self.driver._service_sustain(self.now, soc, 50.0, 55.6, amps)

        self.driver.boost['active'] = True
        for _ in range(4):
            period(50.2, 6.0)                               # filling above target under a boost
        self.assertEqual(su['servo_v'], 0.0)
        self.driver.boost['active'] = False
        period(50.2, 6.0)                                   # the same fill, boost over: the servo answers
        self.assertAlmostEqual(su['servo_v'], -self.cfg.sustain_servo_v, places=3)

    def test_a_re_asserted_hold_keeps_its_anchor_and_refreshes_expiry(self):
        self.driver.settings['chargeslider'] = 80
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        self.tick()
        anchor = self.driver.sustain['anchor_v']
        self.now += 60
        feed_complete(self.driver.bms, self.now, current=-20)
        self.assertTrue(self.driver._set_sustain(3))
        self.tick()
        self.assertEqual(self.driver.sustain['anchor_v'], anchor)
        self.assertEqual(self.driver.batt['/RecBms/Sustain/SecondsLeft'],
                         int(self.cfg.sustain_hold_s))

    def test_a_boost_is_allowed_under_a_hold_and_refused_under_a_ceiling(self):
        self.driver.settings['chargeslider'] = 80
        self.tick()
        self.assertTrue(self.driver._set_sustain(3))
        self.tick()
        self.assertTrue(self.driver._boost_allowed(.3)[0])
        self.assertTrue(self.driver._set_sustain(2))
        self.tick()
        self.assertFalse(self.driver._boost_allowed(.3)[0])

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


class SustainPrimitiveTests(unittest.TestCase):
    """Stage B: the two-sided hold (mode 3) and the ceiling's filling step."""
    FLOOR, CEILING, HOLD = R.SUSTAIN_FLOOR, R.SUSTAIN_CEILING, R.SUSTAIN_HOLD

    def test_a_hold_holds_the_destination_and_ratchets_nowhere(self):
        # SP23: the hold's reference is the target, not what the bank
        # reached, so sustain_hold hands the held value straight back and
        # _service_sustain judges the bank against the slider itself.
        for soc in (62.0, 58.0, None):
            for charging in (False, True):
                for sun in (False, True):
                    self.assertEqual(
                        R.sustain_hold(self.HOLD, 60.0, soc, charging, sun), 60.0,
                        (soc, charging, sun))

    def test_hold_servo_answers_a_drain_up_and_anything_above_target_down(self):
        db = .1
        servo = lambda err, charging, draining, filling=False: R.sustain_servo(
            self.HOLD, err, charging, db, draining, filling)
        # Clearly under the destination: up, draining or not -- the Quattro
        # is capped at charge_limit_a under a hold, so the step fills gently.
        self.assertEqual(servo(-.5, False, True), 1)
        self.assertEqual(servo(-.5, False, False), 1)
        self.assertEqual(servo(-.5, False, False, True), 1)
        self.assertEqual(servo(-.21, False, False), 1)
        # Within two deadbands of it the hold servos like the floor (3.3.2):
        # up while the bank drains, still once the Quattro covers the loads.
        # Winding on regardless put +0.48 V on a bank 0.02 % under its band.
        self.assertEqual(servo(-.15, False, True), 1)
        self.assertEqual(servo(-.15, False, False), 0)
        self.assertEqual(servo(-.15, True, False), 0)
        self.assertEqual(servo(-.11, False, False, True), 0)
        # Above it (3.6.0): down while anything fills it, the Quattro or the
        # sun -- the hold regulates by voltage at its destination and a fill
        # means the hold voltage sits over the bank. A bank sitting still or
        # draining above is left to the loads, never answered by starving
        # the MPPTs of voltage.
        self.assertEqual(servo(.5, True, False), -1)
        self.assertEqual(servo(.5, False, False, True), -1)
        self.assertEqual(servo(.5, False, False), 0)
        self.assertEqual(servo(.5, False, True), 0)
        # Inside the deadband nothing moves either way.
        self.assertEqual(servo(.05, False, True), 0)
        self.assertEqual(servo(-.05, True, True), 0)

    def test_ceiling_steps_down_on_solar_filling_but_not_on_a_plateau(self):
        # E08/D07: alternating an hour of darkness and an hour of 1400 W sun
        # for eight hours put 17.684 Ah back into a bank meant to descend,
        # 1.228 % reverse, because the servo only answered the inferred
        # Quattro. +0.5 A with PV flowing is reverse movement whoever made it.
        servo = lambda charging, draining, filling: R.sustain_servo(
            self.CEILING, -.5, charging, .1, draining, filling)
        self.assertEqual(servo(False, False, True), -1)    # +0.5 A on PV
        self.assertEqual(servo(False, False, False), 0)    # 0 A sunny plateau
        self.assertEqual(servo(False, True, False), 0)     # -0.5 A, the plan
        self.assertEqual(servo(True, False, False), -1)    # the Quattro, as before

    def test_filling_changes_nothing_for_a_floor(self):
        floor = lambda err, charging, draining, filling: R.sustain_servo(
            self.FLOOR, err, charging, .1, draining, filling)
        self.assertEqual(floor(-.5, False, True, False), 1)
        self.assertEqual(floor(.5, False, False, True), 0)   # solar is not fought
        self.assertEqual(floor(.5, True, False, True), -1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
