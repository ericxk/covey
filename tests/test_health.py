# privacy: allow-file 测托管平台的开机守护健康检查（MERLIN_JOB_ID 读自 PID 1 环境）
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import requires_extension
from covey.service.manager import instance_lock, service_key
from covey.service.boot import boot_health, supervise, watch
from covey.feishu.events import consume_event_stream
from covey.core.process import query
from covey.service.boot import serve
from covey.feishu.events import ConsumerHealth, consumer


class HealthTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail('condition timed out')

    def test_health_protocol_is_bounded_and_read_only(self):
        name = 'test-health-' + str(os.getpid())
        sock = instance_lock(name)
        stop = threading.Event()
        thread = threading.Thread(target=serve, args=(sock, lambda: {'status': 'ready'}, stop))
        thread.start()
        try:
            # serve() starts listening in its own thread: a query made before that is refused, not answered.
            self.assertEqual(self.wait_for(lambda: query(name)), {'status': 'ready'})
            self.assertIsNone(query(name + '-missing', timeout=.01))
        finally:
            stop.set()
            thread.join(2)
            sock.close()

    def test_quiet_ready_consumer_is_healthy_and_errors_are_redacted(self):
        health = ConsumerHealth('test')
        health.starting(mock.Mock(pid=12, poll=lambda: None))
        health.stderr('[event] ready event_key=another')
        self.assertFalse(health.snapshot()['ready'])
        health.stderr('[event] ready event_key=test')
        health.started -= 86400
        self.assertTrue(health.snapshot()['ready'])
        self.assertIsNone(health.snapshot()['last_event_age_seconds'])
        health.stderr('{"error":{"type":"auth","message":"secret-token"}}')
        snapshot = health.snapshot()
        self.assertFalse(snapshot['ready'])
        self.assertEqual(snapshot['error'], 'auth')
        self.assertNotIn('secret-token', json.dumps(snapshot))

    def run_consumer(self, code, *, timeout=.3):
        key = 'test-' + self.id()
        stop = threading.Event()
        processes, events = [], []
        thread = threading.Thread(target=consume_event_stream, kwargs={
            'argv': [sys.executable, '-B', '-u', '-c', code, 'consume', key],
            'start_log': '', 'exit_label': key, 'shutdown': stop,
            'set_process': lambda proc: processes.append(proc) if proc else None,
            'handle_event': events.append, 'log': lambda _: None, 'restart_delay': .01,
            'ready_timeout': timeout, 'handler_error_label': 'test'})
        thread.start()

        def cleanup():
            stop.set()
            thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(proc.poll() is not None for proc in processes))
        self.addCleanup(cleanup)
        return key, processes, events

    def test_stuck_startup_replaces_only_consumer(self):
        key, processes, _ = self.run_consumer('import time; time.sleep(60)')
        self.wait_for(lambda: len(processes) >= 2)
        self.assertIsNotNone(processes[0].poll())
        self.assertGreater(consumer(key).snapshot()['restarts'], 0)

    def test_killed_consumer_recovers_and_silence_does_not_restart(self):
        code = "import sys,time; print('[event] ready event_key='+sys.argv[-1],file=sys.stderr,flush=True); time.sleep(60)"
        key, processes, _ = self.run_consumer(code)
        self.wait_for(lambda: consumer(key).snapshot()['ready'])
        time.sleep(.7)
        self.assertEqual(len(processes), 1)
        os.kill(processes[0].pid, signal.SIGKILL)
        self.wait_for(lambda: len(processes) == 2 and consumer(key).snapshot()['ready'])

    def test_bad_events_do_not_kill_reader_and_timing_is_collected(self):
        code = ("import sys,time; print('[event] ready event_key='+sys.argv[-1],file=sys.stderr,flush=True); "
                "print('bad-json'); print('[]'); print('{\"type\":\"test\"}',flush=True); time.sleep(60)")
        key, _, events = self.run_consumer(code)
        self.wait_for(lambda: len(events) == 1)
        self.assertEqual(consumer(key).snapshot()['handler_ms']['samples'], 1)

    def test_boot_watchdog_exposes_waiting_mount_without_nas_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'boot.json'
            config.write_text(json.dumps({'enabled': True, 'devbox_id': 'test-job',
                                          'root': '/missing/services', 'mountpoint': '/missing'}))
            stop = threading.Event()
            # The host's PID 1 may be another user's (CI runners): its environment is not what is under test.
            patch = mock.patch('covey.service.boot.hosting_job', return_value='test-job')
            patch.start()
            self.addCleanup(patch.stop)
            thread = threading.Thread(target=watch, args=(config,), kwargs={'stop': stop, 'interval': .02})
            thread.start()
            try:
                report = self.wait_for(lambda: (h if (h := boot_health(config)) and h['state'] == 'waiting_mount' else None))
                self.assertEqual(report['mode'], 'standalone')
                self.assertLess(report['lag'], 1)
            finally:
                stop.set()
                thread.join(3)

    def test_unreadable_init_environment_leaves_the_host_unknown_and_the_watchdog_retrying(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'boot.json'
            config.write_text(json.dumps({'enabled': True, 'devbox_id': 'test-job', 'root': tmp, 'mountpoint': '/'}))
            stop = threading.Event()
            with mock.patch('covey.service.boot.hosting_job', side_effect=PermissionError(13, 'denied')):
                thread = threading.Thread(target=watch, args=(config,), kwargs={'stop': stop, 'interval': .02})
                thread.start()
                try:
                    report = self.wait_for(lambda: (h if (h := boot_health(config)) and h['state'] == 'retrying' else None))
                    self.assertEqual(report['error'], 'PermissionError')
                finally:
                    stop.set()
                    thread.join(3)

    def test_outer_loop_survives_unexpected_watchdog_exception(self):
        with mock.patch('covey.service.boot.watch', side_effect=[RuntimeError('fault'), None]) as run:
            supervise('/unused', interval=.001)
        self.assertEqual(run.call_count, 2)

    def test_unresponsive_manager_uses_only_previously_verified_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'boot.json'
            config.write_text(json.dumps({'enabled': True, 'devbox_id': 'test-job',
                                          'root': tmp, 'mountpoint': '/', 'watchdog_seconds': 0}))
            stop = threading.Event()
            identity = {'pid': 987654321, 'start_ticks': '123', 'boot_id': 'test'}
            with mock.patch('covey.service.boot.manager_health', side_effect=[{'process': identity, 'lag': 0}, None]), \
                 mock.patch('covey.service.boot.process_matches', return_value=True), \
                 mock.patch('covey.service.boot.hosting_job', return_value='test-job'), \
                 mock.patch('covey.service.boot.os.kill', side_effect=lambda *_: stop.set()) as kill, \
                 mock.patch('covey.service.boot.subprocess.Popen') as spawn:
                watch(config, stop=stop, interval=.001)
            kill.assert_called_once_with(identity['pid'], signal.SIGTERM)
            spawn.assert_not_called()

    @requires_extension()
    def test_installed_hook_runs_in_fresh_pid_namespace(self):
        """Real PID 1 and installed .pth dispatch; not a host reboot claim."""
        from covey_internal.hosting import install_pid1
        probe = subprocess.run(['unshare', '--user', '--map-root-user', '--pid', '--fork',
                                sys.executable, '-B', '-c', 'import os; assert os.getpid()==1'],
                               capture_output=True, timeout=10)
        if probe.returncode:
            self.skipTest('unprivileged PID namespace unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = '/opt/tiger/arnold/arnold_entrypoint/exec/my_init.py'  # privacy: allow 扩展的 hosting 按平台 PID 1 入口路径认环境
            try:
                raw = Path('/proc/1/environ').read_bytes().split(b'\0')
            except OSError:
                self.skipTest("PID 1's environment is not readable here")
            job = next((x.split(b'=', 1)[1].decode() for x in raw if x.startswith(b'MERLIN_JOB_ID=')), '')
            if not job:
                self.skipTest('Merlin job identity unavailable')
            with mock.patch('covey_internal.hosting.pid1_context', return_value=(['python', entry], job)), \
                 mock.patch('covey_internal.hosting.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '/ nfs4\n')), \
                 mock.patch('covey_internal.hosting.sys.executable', str(Path('/proc/1/exe').resolve())):
                receipt = install_pid1(root / 'manager', site_dir=root / 'site', config_dir=root / 'config')
            config_path = Path(receipt['config'])
            cfg = json.loads(config_path.read_text())
            marker = root / 'manager-started'
            cfg['command'] = [sys.executable, '-B', '-c',
                              'from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text("started"); time.sleep(30)', str(marker)]
            config_path.write_text(json.dumps(cfg))
            script = ('import os,sys,site,time,json; sys.argv=[sys.argv[1]]; site.addsitedir(os.environ["TEST_SITE"]); '
                      'sys.path.insert(0,os.environ["TEST_SITE"]); from lark_bridge_boot_hook import boot_health; '
                      'from pathlib import Path\n'
                      'for i in range(100):\n'
                      ' h=boot_health(os.environ["TEST_CONFIG"])\n'
                      ' if h and h["mode"]=="pid1" and Path(os.environ["TEST_MARKER"]).exists(): break\n'
                      ' time.sleep(.05)\n'
                      'else: raise SystemExit("PID1 hook did not activate")\n'
                      'print(json.dumps({"mode":h["mode"],"pid":os.getpid()}))')
            for _ in range(2):
                marker.unlink(missing_ok=True)
                result = subprocess.run(['unshare', '--user', '--map-root-user', '--pid', '--fork',
                                         sys.executable, '-B', '-c', script, entry], capture_output=True, text=True, timeout=15,
                                        env={**os.environ, 'MERLIN_JOB_ID': job, 'TEST_SITE': str(root / 'site'),
                                             'TEST_CONFIG': str(config_path), 'TEST_MARKER': str(marker)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'mode': 'pid1', 'pid': 1})
            self.assertFalse(receipt['reboot_verified'])

    def test_doctor_rejects_process_only_and_wrong_generation(self):
        from covey.service.doctor import diagnose
        from covey.service.manager import process_identity, register, save

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            manifest = register(root, state, [sys.executable, '-c', 'pass'], workdir=root)
            identity = process_identity(os.getpid())
            save(root / 'status' / (service_key(state) + '.json'), {'process': identity, 'generation': 0})
            with mock.patch('covey.service.doctor.manager_health', return_value={'process': identity, 'lag': 0}), \
                 mock.patch('covey.service.doctor.query', return_value={'process': identity, 'status': 'ready'}):
                report = diagnose(root, check_events=False)
                self.assertEqual(report['runtime_status'], 'degraded')
                save(root / 'status' / (service_key(state) + '.json'), {'process': identity, 'generation': manifest['generation']})
                report = diagnose(root, check_events=False)
                self.assertEqual(report['runtime_status'], 'ready')
                self.assertEqual(report['status'], 'degraded')  # no outer supervisor


    def test_doctor_fails_on_a_permission_variable_kept_in_the_manifest(self):
        # LARK_BRIDGE_CODEX_SANDBOX and LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS are no longer read
        # (core/ignored_env.py): a manifest keeping one that restricted runs looser than registered.
        from covey.service import doctor
        from covey.service.manager import process_identity, register, save

        identity = process_identity(os.getpid())
        cases = (({}, []), ({'LARK_BRIDGE_CODEX_SANDBOX': 'danger-full-access',
                             'LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS': '1', 'LARK_CLAUDE_BRIDGE_MODEL': 'x'}, []),
                 ({'LARK_BRIDGE_CODEX_SANDBOX': 'workspace-write'}, ['LARK_BRIDGE_CODEX_SANDBOX=workspace-write']),
                 ({'LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS': 'no'}, ['LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS=no']))
        for environment, flagged in cases:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / 'state'
                manifest = register(root, state, [sys.executable, '-c', 'pass'], workdir=root, environment=environment)
                save(root / 'status' / (service_key(state) + '.json'),
                     {'process': identity, 'generation': manifest['generation']})
                with mock.patch.object(doctor, 'manager_health', return_value={'process': identity, 'lag': 0}), \
                        mock.patch.object(doctor, 'query', return_value={'process': identity, 'status': 'ready'}), \
                        mock.patch.object(sys, 'argv', ['covey_doctor.py', '--root', str(root), '--skip-events',
                                                        '--runtime-only']), \
                        mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                        mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                    code = doctor.main()
                row = json.loads(out.getvalue())['services'][0]
                self.assertEqual([line.split(' ', 1)[0] for line in row.get('permission_env', [])], flagged)
                self.assertEqual((code, row['status']), (2, 'degraded') if flagged else (0, 'ready'))
                for name in flagged:  # What to do, on stderr too.
                    self.assertIn(f"service {row['id']}: {name} is no longer read", err.getvalue())
                    self.assertIn('unset it and put --', err.getvalue())
                self.assertEqual(bool(err.getvalue()), bool(flagged))

    def test_a_stopped_service_s_permission_variable_fails_the_full_report_only(self):
        # r4 review C1: --runtime-only (the deploy's post-check) counts only running services' permission variables,
        # as the deploy's pre-check does; a stopped service's is said on stderr and fails the full report.
        from covey.service import doctor
        from covey.service.manager import process_identity, register, save

        identity = process_identity(os.getpid())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            manifest = register(root, state, [sys.executable, '-c', 'pass'], workdir=root)
            save(root / 'status' / (service_key(state) + '.json'),
                 {'process': identity, 'generation': manifest['generation']})
            old = register(root, root / 'old-state', [sys.executable, '-c', 'pass'], workdir=root, profile='old',
                           environment={'LARK_BRIDGE_CODEX_SANDBOX': 'workspace-write'})
            path = root / 'services' / (old['id'] + '.json')
            path.write_text(json.dumps({**json.loads(path.read_text()), 'desired_state': 'stopped'}))
            for argv, expected in ((['--runtime-only'], 0), ([], 2)):
                with self.subTest(argv=argv), \
                        mock.patch.object(doctor, 'manager_health', return_value={'process': identity, 'lag': 0}), \
                        mock.patch.object(doctor, 'query', return_value={'process': identity, 'status': 'ready'}), \
                        mock.patch.object(doctor, 'boot_report', return_value={'status': 'ready'}), \
                        mock.patch.object(sys, 'argv', ['covey_doctor.py', '--root', str(root), '--skip-events',
                                                        *argv]), \
                        mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                        mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                    code = doctor.main()
                report = json.loads(out.getvalue())
                rows = {row['id']: row for row in report['services']}
                self.assertEqual((code, report['runtime_status'], report['status']), (expected, 'ready', 'degraded'))
                self.assertEqual((rows[manifest['id']]['status'], rows[old['id']]['status']), ('ready', 'stopped'))
                self.assertEqual([line.split(' ', 1)[0] for line in rows[old['id']]['permission_env']],
                                 ['LARK_BRIDGE_CODEX_SANDBOX=workspace-write'])
                self.assertIn(f"service {old['id']}: LARK_BRIDGE_CODEX_SANDBOX=workspace-write is no longer read",
                              err.getvalue())


if __name__ == '__main__':
    unittest.main()
