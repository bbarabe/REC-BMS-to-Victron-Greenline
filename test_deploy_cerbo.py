"""Offline deployment transport tests; no SSH or physical commands."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import deploy_cerbo as deploy


class DeploymentClientTests(unittest.TestCase):
    def test_client_uses_shared_cli_and_close_preserves_daemon(self):
        with patch.object(deploy.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'ready\n', '')
            client = deploy.Cerbo('fixture', 'test-only')
            client.run('read-only-command', timeout=15)
            calls = run.call_args_list
            self.assertEqual(calls[0].args[0], [str(Path(deploy.REPO) / 'cerbo'), 'up'])
            self.assertEqual(calls[1].args[0][1:], ['run', 'read-only-command', '15'])
            self.assertEqual(calls[1].kwargs['env']['CERBO_PASS'], 'test-only')
            client.close()
            self.assertNotIn('CERBO_PASS', client.environ)
            self.assertEqual(run.call_count, 2)

    def test_refused_daemon_operation_aborts_without_retry(self):
        with patch.object(deploy.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 1, '', 'backoff active')
            with self.assertRaisesRegex(RuntimeError, 'backoff active'):
                deploy.Cerbo('fixture', 'test-only')
            self.assertEqual(run.call_count, 1)

    def test_failed_backup_command_cannot_be_treated_as_success(self):
        with patch.object(deploy.subprocess, 'run') as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, '', ''),
                               subprocess.CompletedProcess([], 7, '', 'copy failed')]
            client = deploy.Cerbo('fixture', 'test-only')
            with self.assertRaisesRegex(RuntimeError, 'exit 7'):
                client.run('backup-command')
            self.assertEqual(run.call_count, 2)

    def test_upload_normalizes_temporary_copy_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy.Cerbo, '_call') as call:
            source = Path(folder) / 'script.py'
            source.write_bytes(b'#!/usr/bin/python3\r\nprint(1)\r\n')
            staged = []
            def invoke(*args):
                if args[0] == 'put':
                    staged.append(Path(args[1]))
                    self.assertEqual(Path(args[1]).read_bytes(), b'#!/usr/bin/python3\nprint(1)\n')
                return '', ''
            call.side_effect = invoke
            client = deploy.Cerbo('fixture', 'test-only')
            client.put(str(source), '/data/example/script.py')
            self.assertIn(b'\r\n', source.read_bytes())
            self.assertFalse(staged[0].exists())
            self.assertEqual(call.call_args.args, ('run', 'chmod 755 /data/example/script.py', '60'))

    def test_failed_upload_cleans_temporary_file_and_does_not_chmod(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy.Cerbo, '_call') as call:
            source = Path(folder) / 'script.py'
            source.write_text('pass\n')
            staged = []
            def invoke(*args):
                if args[0] == 'put':
                    staged.append(Path(args[1]))
                    raise RuntimeError('transfer failed')
                return '', ''
            call.side_effect = invoke
            client = deploy.Cerbo('fixture', 'test-only')
            with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
                client.put(str(source), '/data/example/script.py')
            self.assertFalse(staged[0].exists())
            self.assertFalse(any('chmod' in str(c.args) for c in call.call_args_list))


class CerboCliTests(unittest.TestCase):
    def invoke(self, response, *arguments):
        operations = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                operations.append(request)
                self.wfile.write((json.dumps(response(request)) + '\n').encode())
        with tempfile.TemporaryDirectory() as folder:
            sock = str(Path(folder) / 'cerbo-fixture.sock')
            with socketserver.ThreadingUnixStreamServer(sock, Handler) as server:
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                thread.start()
                try:
                    result = subprocess.run([str(Path(deploy.REPO) / 'cerbo'), *arguments],
                        env=dict(os.environ, CERBO_HOST='fixture', CERBO_PASS='test-only', TMPDIR=folder),
                        capture_output=True, text=True, timeout=5)
                finally:
                    server.shutdown()
                    thread.join(timeout=2)
        return result, operations

    def test_remote_nonzero_exit_propagates_even_when_rpc_succeeded(self):
        def response(request):
            if request['op'] == 'ping': return {'ok': True, 'alive': True}
            return {'ok': True, 'out': 'partial result\n', 'err': 'command failed\n', 'rc': 7}
        result, operations = self.invoke(response, 'run', 'fixture-command')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, 'partial result\n')
        self.assertIn('command failed', result.stderr)
        self.assertEqual([r['op'] for r in operations], ['ping', 'run'])

    def test_dead_transport_is_reported_down_without_ssh_attempt(self):
        result, operations = self.invoke(lambda request: {'ok': True, 'alive': False}, 'status')
        self.assertIn('session DOWN', result.stdout)
        self.assertEqual([r['op'] for r in operations], ['ping'])

    def test_successful_command_reuses_existing_daemon(self):
        def response(request):
            if request['op'] == 'ping': return {'ok': True, 'alive': True}
            return {'ok': True, 'out': 'verified\n', 'rc': 0}
        result, operations = self.invoke(response, 'run', 'fixture-command')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'verified\n')
        self.assertEqual([r['op'] for r in operations], ['ping', 'run'])


class PackageDefinitionTests(unittest.TestCase):
    def test_every_listed_file_exists_in_the_repo(self):
        for name, pkg in deploy.PACKAGES.items():
            for rel in pkg['files'] + [pkg['version_file']]:
                self.assertTrue((Path(deploy.REPO) / pkg['dir'] / rel).is_file(),
                                '%s lists %s, which is not in %s/' % (name, rel, pkg['dir']))
            self.assertLessEqual(set(pkg['configs']), set(pkg['files']), name)


class VerifyTests(unittest.TestCase):
    class Box:
        def __init__(self, process_version='', command_output=''):
            self.process_version, self.command_output = process_version, command_output
        def run(self, cmd, timeout=60):
            return (self.command_output if cmd.startswith('wget') else ''), ''
        def dbus_get(self, service, path):
            return self.process_version if path == '/Mgmt/ProcessVersion' else '0'

    def verify(self, package, box, expect):
        with patch('builtins.print'):
            return deploy.verify(box, deploy.PACKAGES[package], '/data/fixture', expect=expect)

    def test_running_process_must_report_the_shipped_version(self):
        self.assertTrue(self.verify('czone', self.Box("'1.2.3 on Python 3.12.1'"), '1.2.3'))
        self.assertFalse(self.verify('czone', self.Box("'1.2.2 on Python 3.12.1'"), '1.2.3'))
        self.assertFalse(self.verify('czone', self.Box("'1.2.30 on Python 3.12.1'"), '1.2.3'))
        self.assertTrue(self.verify('czone', self.Box("'1.2.2 on Python 3.12.1'"), None))

    def test_package_without_process_version_is_verified_by_its_status_output(self):
        self.assertTrue(self.verify('camerarelay', self.Box(command_output='{"version": "0.5.0", "uptime_s": 3}'), '0.5.0'))
        self.assertFalse(self.verify('camerarelay', self.Box(command_output='{"version": "0.4.0", "uptime_s": 3}'), '0.5.0'))
        self.assertFalse(self.verify('camerarelay', self.Box(command_output='{"version": "10.5.0"}'), '0.5.0'))


if __name__ == '__main__':
    unittest.main()
