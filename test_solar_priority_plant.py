"""Regression checks for isolation and lifecycle in the offline plant fixture."""
import shutil
import sys
import tempfile
from pathlib import Path
import unittest

from solar_priority_plant import (
    ROOT, Clock, CoupledSimulation, DelayedBus, DelayedMonitor, Latency, PlantService,
)


class SimulationIsolationTests(unittest.TestCase):
    def test_alternate_checkout_uses_its_own_transitive_helpers(self):
        original_path = sys.path[:]
        with CoupledSimulation() as sim:
            original_adapter = sim.rec_module.RecPolicyAdapter
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / 'driver'
            shutil.copytree(ROOT / 'dbus-recbms', checkout,
                            ignore=shutil.ignore_patterns('__pycache__'))
            for file, cls in (('rec_policy_adapter.py', 'RecPolicyAdapter'),
                              ('energy_accounting.py', 'EnergyLedger'),
                              ('solar_engine.py', 'Engine')):
                with (checkout / file).open('a') as stream:
                    stream.write('\n%s.checkout_marker = "alternate"\n' % cls)
            with CoupledSimulation(rec_path=checkout / 'dbus_recbms.py',
                                   solar_path=checkout / 'solar_priority.py') as sim:
                self.assertEqual(sim.rec_module.RecPolicyAdapter.checkout_marker, 'alternate')
                self.assertEqual(sim.rec.policy_adapter.ledger.checkout_marker, 'alternate')
                self.assertEqual(sim.solar_module.Engine.checkout_marker, 'alternate')
                self.assertIsNot(sim.rec_module.RecPolicyAdapter, original_adapter)
                sim.run(2)
        with CoupledSimulation() as sim:
            self.assertFalse(hasattr(sim.rec_module.RecPolicyAdapter, 'checkout_marker'))
            self.assertFalse(hasattr(sim.solar_module.Engine, 'checkout_marker'))
        self.assertEqual(sys.path, original_path)

    def test_failed_import_restores_existing_helper_and_search_path(self):
        original_path = sys.path[:]
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / 'fixture_helper.py').write_text('value = 1\n')
            (checkout / 'broken.py').write_text('import fixture_helper\nraise RuntimeError("broken")\n')
            sentinel = object()
            previous = sys.modules.get('fixture_helper')
            sys.modules['fixture_helper'] = sentinel
            try:
                with self.assertRaisesRegex(RuntimeError, 'broken'):
                    CoupledSimulation(rec_path=checkout / 'broken.py')
                self.assertIs(sys.modules['fixture_helper'], sentinel)
                self.assertEqual(sys.path, original_path)
            finally:
                if previous is None:
                    sys.modules.pop('fixture_helper', None)
                else:
                    sys.modules['fixture_helper'] = previous


class MonitorLifecycleTests(unittest.TestCase):
    def test_retired_monitor_drops_queued_delivery_but_live_monitor_receives_it(self):
        clock = Clock()
        bus = DelayedBus(clock, Latency())
        service = PlantService('fixture', bus)
        service.add_path('/Value', 0)
        old_updates, live_updates = [], []
        old = DelayedMonitor(bus, {}, valueChangedCallback=lambda *args: old_updates.append(args))
        DelayedMonitor(bus, {}, valueChangedCallback=lambda *args: live_updates.append(args))
        service['/Value'] = 1
        old.close()
        old.close()  # Stop followed by restart must be harmless.
        clock.elapsed = 1
        bus.drain()
        self.assertEqual(old_updates, [])
        self.assertEqual(len(live_updates), 1)
        self.assertEqual(old.get_value('fixture', '/Value'), 0)

    def test_stop_and_restart_retire_monitor_with_pending_publications(self):
        with CoupledSimulation() as sim:
            old = sim.solar.monitor
            updates = []
            old.changed = lambda *args: updates.append(args)
            sim.system['/Dc/Pv/Power'] = 123
            sim.stop_solar()
            sim.restart_solar(planned=False)
            sim.run(2)
            self.assertEqual(updates, [])
            self.assertNotIn(old, sim.bus.monitors)
            self.assertIn(sim.solar.monitor, sim.bus.monitors)


if __name__ == '__main__':
    unittest.main()
