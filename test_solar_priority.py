#!/usr/bin/env python3
"""Run revised acceptance suites in isolated processes (driver stubs are global)."""
import os
import subprocess
import sys

SUITES = ('test_solar_engine', 'test_rec_safety', 'test_energy_accounting',
          'test_control_policy', 'test_control_config', 'test_transfer_supervisor',
          'test_control_watchdog', 'test_policy_telemetry', 'test_policy_adapter',
          'test_restored_architecture', 'test_rec_engine_boundary')

if __name__ == '__main__':
    failures = []
    for suite in SUITES:
        command = ([sys.executable, suite + '.py'] if suite == 'test_solar_engine' else
                   [sys.executable, '-m', 'unittest', suite])
        result = subprocess.run(command,
                                cwd=os.path.dirname(os.path.abspath(__file__)),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            failures.append(suite)
            print(result.stdout)
        else:
            summary = (next((line for line in result.stdout.splitlines() if 'passed, 0 failed' in line), 'engine assertions passed') if suite == 'test_solar_engine' else next(
                (line for line in result.stdout.splitlines() if line.startswith('Ran ')), 'passed'))
            print('%s: %s' % (suite, summary), flush=True)
    raise SystemExit(bool(failures))
