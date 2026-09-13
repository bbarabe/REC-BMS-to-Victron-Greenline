#!/usr/bin/env python3
"""Independent process supervisor for successful REC control-loop heartbeats.

The supervisor never accesses D-Bus or actuators. It launches exactly one REC
executor, terminates a dead/stalled executor and exits for daemontools to restart
it. The restarted REC executor is responsible for protective shore control.
Default deadlines are provisional commissioning values, not measured recovery.
A running-loop failure takes up to 8 s to detect plus up to 2 s TERM grace,
then service restart, REC startup, fresh input and actuator response time. The
30 s first-heartbeat grace is a separate startup limit. These times must be
measured together against worst-case battery demand; no Wh bound is certified.

    python3 control_watchdog.py -- python3 dbus_recbms.py

Initialize Heartbeat.from_environment() before driver setup, then pulse only
AFTER a complete successful control tick. A False result means the supervisor
has disappeared; the executor must perform its normal protective shutdown.
"""
import argparse
import errno
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time


HEARTBEAT_FD_ENV = 'REC_CONTROL_HEARTBEAT_FD'


def _duration(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('%s must be a positive finite duration' % name)
    return float(value)


class Heartbeat:
    """Nonblocking child-side pipe, disabled for an unwrapped offline driver."""

    def __init__(self, fd=None):
        self.fd = fd
        self.enabled = fd is not None
        if self.enabled:
            if not isinstance(fd, int) or fd < 3 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
                raise ValueError('watchdog descriptor must be a private pipe')
            os.set_inheritable(fd, False)
            os.set_blocking(fd, False)

    @classmethod
    def from_environment(cls, environ=None):
        environ = os.environ if environ is None else environ
        raw = environ.pop(HEARTBEAT_FD_ENV, None)
        if raw is None:
            return cls()
        try:
            fd = int(raw)
        except (ValueError, TypeError):
            raise ValueError('invalid watchdog heartbeat descriptor')
        return cls(fd)

    def pulse(self):
        if not self.enabled:
            return True
        if self.fd is None:
            return False
        try:
            os.write(self.fd, b'.')
            return True
        except OSError as exc:
            # Pending heartbeats already establish progress; control must never
            # block behind a delayed supervisor or a full pipe.
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            if exc.errno in (errno.EPIPE, errno.EBADF):
                self.close()
                return False
            raise

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            self.fd = None


class WatchdogSupervisor:
    """One child process group, monotonic deadlines, bounded TERM→KILL.

    run() must execute in the main thread because it owns signal handlers.
    There is deliberately no internal restart loop: no replacement child can
    overlap the preceding executor. After KILL, reaping must finish before exit;
    a kernel-uninterruptible child requires the platform watchdog, not a second
    executor. Each instance may run only once.
    """

    def __init__(self, command, timeout_s=8.0, startup_grace_s=30.0,
                 kill_grace_s=2.0, poll_s=0.1, environ=None, logger=None):
        if not isinstance(command, (list, tuple)) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError('command must be a nonempty argument list')
        self.command = list(command)
        self.timeout_s = _duration(timeout_s, 'timeout_s')
        self.startup_grace_s = _duration(startup_grace_s, 'startup_grace_s')
        self.kill_grace_s = _duration(kill_grace_s, 'kill_grace_s')
        self.poll_s = _duration(poll_s, 'poll_s')
        self.environ = dict(os.environ if environ is None else environ)
        self.logger = logger or self._log
        self.child = None
        self.reason = ''
        self._signal = None
        self._ran = False

    @staticmethod
    def _log(message):
        print('REC watchdog: ' + message, file=sys.stderr, flush=True)

    def _request_stop(self, signum, frame):
        self._signal = signum

    def _signal_group(self, signum):
        try:
            os.killpg(self.child.pid, signum)
        except ProcessLookupError:
            pass

    def _reap(self):
        if self.child is None:
            return
        self._signal_group(signal.SIGTERM)
        try:
            self.child.wait(timeout=self.kill_grace_s)
        except subprocess.TimeoutExpired:
            self.logger('executor did not stop after TERM; sending KILL')
        finally:
            # Kill any remaining helper descendants as well. They share the
            # executor's isolated process group and may not survive its lifetime.
            self._signal_group(signal.SIGKILL)
        self.child.wait()

    @staticmethod
    def _drain(fd):
        received = False
        while True:
            try:
                block = os.read(fd, 65536)
            except BlockingIOError:
                return received, False
            if not block:
                return received, True
            received = True

    def run(self):
        if self._ran:
            raise RuntimeError('a watchdog instance may launch only one executor')
        self._ran = True
        read_fd, write_fd = os.pipe()
        previous_handlers = {}
        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(read_fd, False)
            selector.register(read_fd, selectors.EVENT_READ)
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, self._request_stop)
            child_env = dict(self.environ)
            child_env[HEARTBEAT_FD_ENV] = str(write_fd)
            try:
                self.child = subprocess.Popen(self.command, env=child_env,
                                              pass_fds=(write_fd,), start_new_session=True)
            except OSError as exc:
                self.reason = 'executor launch failed: %s' % exc
                self.logger(self.reason)
                return 127
            finally:
                os.close(write_fd)
                write_fd = None
            first_deadline = time.monotonic() + self.startup_grace_s
            last_heartbeat = None
            while True:
                if self._signal is not None:
                    self.reason = 'supervisor stopping on signal %d' % self._signal
                    self.logger(self.reason)
                    return 128 + self._signal
                code = self.child.poll()
                if code is not None:
                    self.reason = 'executor exited (%d)' % code
                    self.logger(self.reason)
                    return (128 - code) if code < 0 else (code or 1)
                now = time.monotonic()
                deadline = first_deadline if last_heartbeat is None else last_heartbeat + self.timeout_s
                if now >= deadline:
                    self.reason = 'startup heartbeat timeout' if last_heartbeat is None else 'control heartbeat timeout'
                    self.logger(self.reason)
                    return 124
                events = selector.select(min(self.poll_s, deadline - now))
                if events:
                    received, closed = self._drain(read_fd)
                    if received:
                        last_heartbeat = time.monotonic()
                    if closed:
                        self.reason = 'executor heartbeat pipe closed'
                        self.logger(self.reason)
                        return 1
        finally:
            try:
                self._reap()
            finally:
                selector.close()
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout-s', type=float, default=8.0)
    parser.add_argument('--startup-grace-s', type=float, default=30.0)
    parser.add_argument('--kill-grace-s', type=float, default=2.0)
    parser.add_argument('--poll-s', type=float, default=0.1)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        watchdog = WatchdogSupervisor(command, args.timeout_s, args.startup_grace_s,
                                      args.kill_grace_s, args.poll_s)
    except ValueError as exc:
        parser.error(str(exc))
    return watchdog.run()


if __name__ == '__main__':
    sys.exit(main())
