#!/usr/bin/env python3
"""Watchdog tests use harmless short-lived child processes, never drivers."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
MODULE = ROOT / 'dbus-recbms' / 'control_watchdog.py'
SPEC = importlib.util.spec_from_file_location('control_watchdog', MODULE)
W = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(W)
IMPORT = 'import sys; sys.path.insert(0, %r); from control_watchdog import Heartbeat; ' % str(MODULE.parent)


class HeartbeatTests(unittest.TestCase):
    def test_disabled_helper_allows_direct_offline_execution(self):
        heartbeat = W.Heartbeat.from_environment({})
        self.assertFalse(heartbeat.enabled)
        self.assertTrue(heartbeat.pulse())

    def test_nonblocking_pipe_pulse_and_supervisor_loss(self):
        read_fd, write_fd = os.pipe()
        heartbeat = None
        try:
            environ = {W.HEARTBEAT_FD_ENV: str(write_fd)}
            heartbeat = W.Heartbeat.from_environment(environ)
            self.assertNotIn(W.HEARTBEAT_FD_ENV, environ)
            self.assertFalse(os.get_inheritable(write_fd))
            self.assertFalse(os.get_blocking(write_fd))
            self.assertTrue(heartbeat.pulse())
            self.assertEqual(os.read(read_fd, 1), b'.')
            os.close(read_fd)
            read_fd = None
            self.assertFalse(heartbeat.pulse())
            self.assertFalse(heartbeat.pulse())
        finally:
            if heartbeat is not None:
                heartbeat.close()
            else:
                os.close(write_fd)
            if read_fd is not None:
                os.close(read_fd)

    def test_full_pipe_never_blocks_control(self):
        read_fd, write_fd = os.pipe()
        heartbeat = W.Heartbeat(write_fd)
        try:
            try:
                while True:
                    os.write(write_fd, b'x' * 65536)
            except BlockingIOError:
                pass
            started = time.monotonic()
            self.assertTrue(heartbeat.pulse())
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            heartbeat.close()
            os.close(read_fd)

    def test_exec_grandchild_cannot_inherit_heartbeat_even_with_close_fds_false(self):
        read_fd, write_fd = os.pipe()
        heartbeat = W.Heartbeat(write_fd)
        code = 'import os,sys\ntry: os.fstat(int(sys.argv[1]))\nexcept OSError: sys.exit(0)\nsys.exit(1)'
        try:
            child = subprocess.run([sys.executable, '-c', code, str(write_fd)], close_fds=False)
            self.assertEqual(child.returncode, 0)
        finally:
            heartbeat.close()
            os.close(read_fd)

    def test_bad_descriptor_is_not_silently_disabled(self):
        for raw in ('not-an-fd', '-1', '1'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                W.Heartbeat.from_environment({W.HEARTBEAT_FD_ENV: raw})


class WatchdogProcessTests(unittest.TestCase):
    def launch(self, body, startup=0.6, timeout=0.2, grace=0.1):
        return subprocess.Popen([sys.executable, str(MODULE), '--timeout-s', str(timeout),
                                 '--startup-grace-s', str(startup), '--kill-grace-s', str(grace),
                                 '--poll-s', '0.01', '--', sys.executable, '-c', IMPORT + body],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def collect(self, process):
        try:
            return process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            self.fail('watchdog failed to finish its bounded test')

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def wait_file(self, path, process):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                self.fail('watchdog exited before child startup')
            time.sleep(0.01)
        self.fail('child did not start')

    def test_startup_timeout_reaps_silent_executor(self):
        process = self.launch('import os,time; print(os.getpid(), flush=True); time.sleep(60)', startup=0.2)
        stdout, stderr = self.collect(process)
        self.assertEqual(process.returncode, 124)
        self.assertIn('startup heartbeat timeout', stderr)
        self.assert_gone(int(stdout.strip()))

    def test_successful_tick_then_stall_uses_short_running_deadline(self):
        process = self.launch('import os,time; h=Heartbeat.from_environment(); print(os.getpid(), flush=True); h.pulse(); time.sleep(60)', startup=2)
        started = time.monotonic()
        stdout, stderr = self.collect(process)
        self.assertEqual(process.returncode, 124)
        self.assertIn('control heartbeat timeout', stderr)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_gone(int(stdout.strip()))

    def test_setup_grace_then_continuous_ticks_are_not_a_stall(self):
        body = 'import time; h=Heartbeat.from_environment(); time.sleep(.25)\nfor i in range(10):\n h.pulse(); time.sleep(.03)\n'
        process = self.launch(body, startup=0.8, timeout=0.15)
        _, stderr = self.collect(process)
        self.assertEqual(process.returncode, 1)  # A vanished executor is a restart request, even with exit 0.
        self.assertNotIn('timeout', stderr)

    def test_executor_failure_produces_nonzero_supervisor_exit(self):
        process = self.launch('raise SystemExit(7)')
        self.collect(process)
        self.assertNotEqual(process.returncode, 0)
        self.assertNotEqual(process.returncode, 124)

    def test_clean_service_stop_terminates_child_and_reaps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready'
            stopped = Path(directory) / 'stopped'
            body = ('import os,time,signal; from pathlib import Path; h=Heartbeat.from_environment()\n'
                    'def stop(sig, frame):\n Path(%r).write_text("stopped"); raise SystemExit(0)\n'
                    'signal.signal(signal.SIGTERM,stop)\nPath(%r).write_text(str(os.getpid()))\n'
                    'while True:\n h.pulse(); time.sleep(.02)\n') % (str(stopped), str(ready))
            process = self.launch(body, startup=1)
            self.wait_file(ready, process)
            process.terminate()
            self.collect(process)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM)
            self.assertTrue(stopped.exists())
            self.assert_gone(int(ready.read_text()))

    def test_stalled_child_ignoring_term_is_killed_and_reaped(self):
        body = ('import os,time,signal; h=Heartbeat.from_environment(); '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                'print(os.getpid(),flush=True); h.pulse(); time.sleep(60)')
        process = self.launch(body)
        stdout, stderr = self.collect(process)
        self.assertEqual(process.returncode, 124)
        self.assertIn('sending KILL', stderr)
        self.assert_gone(int(stdout.strip()))

    def test_closed_pipe_is_immediate_failure_not_startup_grace(self):
        process = self.launch('import os,time; h=Heartbeat.from_environment(); print(os.getpid(),flush=True); h.close(); time.sleep(60)', startup=2)
        stdout, stderr = self.collect(process)
        self.assertEqual(process.returncode, 1)
        self.assertIn('pipe closed', stderr)
        self.assert_gone(int(stdout.strip()))

    def test_only_one_child_per_supervisor_and_no_parent_fd_leak(self):
        before = len(os.listdir('/proc/self/fd'))
        supervisor = W.WatchdogSupervisor([sys.executable, '-c', 'raise SystemExit(0)'], logger=lambda text: None)
        self.assertNotEqual(supervisor.run(), 0)
        with self.assertRaises(RuntimeError):
            supervisor.run()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        self.assertIsNotNone(supervisor.child.returncode)

    def test_failed_launch_closes_all_pipe_descriptors(self):
        before = len(os.listdir('/proc/self/fd'))
        supervisor = W.WatchdogSupervisor(['/this-executable-does-not-exist'], logger=lambda text: None)
        self.assertEqual(supervisor.run(), 127)
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
