# privacy: allow-file 测托管平台的开机守护与 PID 1 识别（MERLIN_JOB_ID、merlin-pid1 模式），这些名字是平台接口
from __future__ import annotations

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
from covey.service.manager import Manager, instance_lock, is_owned, process_identity, read_json, register, runtime_environment, service_key, set_desired, terminate_owned
from covey.service.boot import eligible, manager_health, mounted, watch
from covey.service.install import main as install_main, systemd_unit


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'old-state'
        self.state.mkdir()
        self.manager_root = self.root / 'services'
        self.manager = Manager(self.manager_root, base_delay=0, max_delay=0)
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.manager.children.values():
            identity = process_identity(child.pid)
            if identity:
                terminate_owned(identity, timeout=1)
            child.wait(timeout=5)

    def register(self, code='import time; time.sleep(60)', profile='one', health_timeout=None):
        return register(self.manager_root, self.state, [sys.executable, '-B', '-c', code],
                        workdir=self.root, profile=profile, health_timeout=health_timeout)

    def status(self):
        return read_json(self.manager_root / 'status' / (service_key(self.state) + '.json'), {})

    def test_crash_restarts_beyond_old_failure_limit_without_touching_sessions(self):
        session = self.state / 'codex_session_id'
        session.write_text('original-session')
        manifest = self.register()
        pids = set()
        for _ in range(7):
            self.manager.reconcile(manifest)
            status = self.status()
            self.assertEqual(status['status'], 'running')
            pids.add(status['process']['pid'])
            os.kill(status['process']['pid'], signal.SIGKILL)
            self.manager.children[manifest['id']].wait(timeout=5)
            self.manager.reconcile(manifest)
            self.assertEqual(self.status()['status'], 'backoff')
        self.assertEqual(len(pids), 7)
        self.assertEqual(session.read_text(), 'original-session')

    def test_one_malformed_manifest_never_stops_the_others(self):
        import contextlib
        import io
        manifest = self.register()
        broken = self.manager_root / 'services' / '0-broken.json'
        broken.write_text('{"desired_state": "running"}')
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.manager.reconcile_all()
            self.manager.reconcile_all()
        self.assertEqual([line.split('] ', 1)[1] for line in said.getvalue().splitlines() if '0-broken' in line],
                         ["service 0-broken: 'id'"])  # Once, not at every pass (repo review E3).
        self.assertEqual(self.status()['status'], 'running')
        self.assertEqual(read_json(self.manager_root / 'errors' / broken.name)['error'], "'id'")
        self.assertTrue(is_owned(self.status()['process']))
        self.assertIn(manifest['id'], self.manager.children)

    def test_a_service_error_is_written_once_and_cleared_when_it_ends(self):
        # It was rewritten with fsync at every 2 s pass while it lasted and never removed, so the same error days
        # later went unlogged.
        import contextlib
        import io
        from covey.service.manager import save
        manifest = self.register()
        path = self.manager_root / 'services' / (manifest['id'] + '.json')
        error = self.manager_root / 'errors' / path.name
        said = io.StringIO()
        with contextlib.redirect_stdout(said), mock.patch('covey.service.manager.save', wraps=save) as saved:
            for _ in range(2):
                with mock.patch.object(Manager, 'reconcile', side_effect=RuntimeError('state has a live unmanaged PID')):
                    self.manager.reconcile_all()
                self.assertEqual(read_json(error)['error'], 'state has a live unmanaged PID')
            self.assertEqual([call.args[0] for call in saved.call_args_list], [error])
            self.manager.reconcile_all()
            self.assertFalse(error.exists())
            self.assertEqual(self.status()['status'], 'running')
            with mock.patch.object(Manager, 'reconcile', side_effect=RuntimeError('state has a live unmanaged PID')):
                self.manager.reconcile_all()
        self.assertEqual([line.split('] ', 1)[1] for line in said.getvalue().splitlines()
                          if 'unmanaged' in line], [f"service {manifest['id'][:8]}: state has a live unmanaged PID"] * 2)

    def test_stop_is_durable_and_start_reenables(self):
        manifest = self.register()
        self.manager.reconcile(manifest)
        identity = self.status()['process']
        set_desired(self.manager_root, self.state, 'stopped')
        path = self.manager_root / 'services' / (manifest['id'] + '.json')
        self.manager.reconcile(read_json(path))
        self.assertFalse(is_owned(identity))
        replacement = Manager(self.manager_root)
        replacement.reconcile(read_json(path))
        self.assertEqual(self.status()['status'], 'stopped')
        set_desired(self.manager_root, self.state, 'running')
        self.manager.reconcile(read_json(path))
        self.assertTrue(is_owned(self.status()['process']))

    def test_manager_restart_adopts_live_process(self):
        manifest = self.register()
        self.manager.reconcile(manifest)
        identity = self.status()['process']
        Manager(self.manager_root).reconcile(manifest)
        self.assertEqual(self.status()['process'], identity)
        self.assertTrue(is_owned(identity))

    def test_unresponsive_health_restarts_without_changing_session(self):
        session = self.state / 'codex_session_id'
        session.write_text('preserved')
        manifest = self.register()
        manifest['health_timeout'] = .01
        self.manager.reconcile(manifest)
        identity = self.status()['process']
        self.manager.reconcile(manifest)
        time.sleep(.02)
        import contextlib
        import io
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.manager.reconcile(manifest)
        self.assertIn(f"pid={identity['pid']}: no health response for 0s, replacing it", said.getvalue())
        self.assertFalse(is_owned(identity))
        self.assertEqual(self.status()['status'], 'unresponsive')
        self.manager.reconcile(manifest)
        self.manager.reconcile(manifest)
        self.assertNotEqual(self.status()['process'], identity)
        self.assertEqual(session.read_text(), 'preserved')

    def test_degraded_subscriptions_do_not_kill_running_jobs(self):
        manifest = self.register()
        manifest['health_timeout'] = .01
        self.manager.reconcile(manifest)
        identity = self.status()['process']
        with mock.patch('covey.service.manager.query', return_value={'process': identity, 'status': 'degraded'}):
            self.manager.reconcile(manifest)
            time.sleep(.02)
            self.manager.reconcile(manifest)
        self.assertEqual(self.status()['process'], identity)
        self.assertTrue(is_owned(identity))
        self.assertEqual(self.status()['health']['status'], 'degraded')

    def test_live_unmanaged_pid_is_not_killed(self):
        manifest = self.register()
        (self.state / 'bridge.pid').write_text(str(os.getpid()))
        with self.assertRaisesRegex(RuntimeError, 'unmanaged'):
            self.manager.reconcile(manifest)
        self.assertIsNotNone(process_identity(os.getpid()))

    def test_profile_conflict_and_secret_environment_rejected(self):
        self.register()
        with self.assertRaisesRegex(ValueError, 'profile'):
            register(self.manager_root, self.root / 'other', [sys.executable, '-c', 'pass'], workdir=self.root, profile='one')
        with self.assertRaisesRegex(ValueError, 'non-secret'):
            register(self.manager_root, self.state, [sys.executable, '-c', 'pass'], workdir=self.root,
                     environment={'API_KEY': 'never-persist'})

    def test_manager_health_answers_only_its_own_user(self):
        import socket
        address = '\0lark-bridge-test-manager-health-' + str(os.getpid())
        server = socket.socket(socket.AF_UNIX)
        server.bind(address)
        thread = threading.Thread(target=self.manager.health_server, args=(server,), daemon=True)
        thread.start()
        self.addCleanup(server.close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.manager.stop.set)

        def read():
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(address)
                return client.recv(4096)

        self.assertEqual(json.loads(read())['process'], process_identity(os.getpid()))
        with mock.patch('covey.service.boot.os.getuid', return_value=os.getuid() + 1):
            self.assertEqual(read(), b'')  # Another user's peer gets nothing.

    def test_boot_asks_the_manager_where_and_as_core_process_does(self):
        # service/boot.py does not import the package (a NAS outage must not stop it), so it writes the Manager's
        # address and the health query again; they must stay core/process.py's.
        import socket
        from covey.core import process
        from covey.service import boot
        root = str(Path(self.manager_root).resolve())
        with mock.patch.object(boot, '_ask', return_value=None) as ask:
            boot.manager_health(root)
        ask.assert_called_once_with(process.socket_address('manager-' + process.service_key(Path(root))))
        name = 'test-boot-ask-' + str(os.getpid())
        server = socket.socket(socket.AF_UNIX)
        server.bind(process.socket_address(name))
        server.listen(2)
        self.addCleanup(server.close)

        def answer():
            for _ in range(2):
                connection, _ = server.accept()
                with connection:
                    connection.sendall(b'{"process": "same"}')

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self.assertEqual(boot._ask(process.socket_address(name)), {'process': 'same'})
        self.assertEqual(process.query(name), {'process': 'same'})
        thread.join(3)

    def test_identity_rejects_reused_pid(self):
        identity = process_identity(os.getpid())
        identity['start_ticks'] = str(int(identity['start_ticks']) - 1)
        self.assertFalse(is_owned(identity))

    def test_bridge_settings_persist_and_secrets_and_manager_variables_do_not(self):
        environ = {'LARK_MASO_BRIDGE_PYTHON': '/nas/maso/venv/bin/python', 'LARK_MASO_BRIDGE_END_WAIT': '600',
                   'LARK_DSH_BRIDGE_CMD': '/opt/dsh', 'NEEKO_HOME': '/nas/neeko',  # Neeko Next's, retired.
                   'LARK_CLAUDE_BRIDGE_TRANSPORT': 'stream-json', 'LARK_BRIDGE_CODEX_SANDBOX': 'workspace-write',
                   'COVEY_MANAGED': '1', 'COVEY_REQUIRED_MOUNT': '/nas', 'COVEY_SERVICE_ROOT': '/nas/s',
                   'COVEY_APP_SECRET': 'never', 'COVEY_API_KEY': 'never', 'OPENCODE_DB_PATH': '/x', 'XDG_DATA_HOME': '/x',
                   'LLMGW_SERVICE_SECRET': 'never', 'ACME_API_KEY': 'never', 'ACME_API_KEY_FILE': '/x',
                   'DSH_HOME': '/nas/dsh', 'HOME': '/home/user',
                   'COVEY_TASK_DIR': '/nas/state/runs/om_x'}  # A task's marker: launched from a task's shell.
        self.assertEqual(set(runtime_environment(environ)), {
            'LARK_MASO_BRIDGE_PYTHON', 'LARK_MASO_BRIDGE_END_WAIT', 'LARK_DSH_BRIDGE_CMD',
            'LARK_CLAUDE_BRIDGE_TRANSPORT', 'LARK_BRIDGE_CODEX_SANDBOX', 'DSH_HOME'})
        with self.assertRaises(ValueError):
            register(self.manager_root, self.state, [sys.executable, '-c', 'pass'], workdir=self.root,
                     environment={'COVEY_APP_SECRET': 'never'})
        with self.assertRaises(ValueError):
            register(self.manager_root, self.state, [sys.executable, '-c', 'pass'], workdir=self.root,
                     environment={'LARK_MASO_BRIDGE_PYTHON': 'a\nb'})

    def test_systemd_stops_only_the_manager(self):
        # The Bridge and task runners are the Manager's children in the unit's cgroup: stopping or
        # restarting the unit must leave them for the next Manager to adopt.
        unit = systemd_unit(self.manager_root)
        self.assertIn("KillMode=process\n", unit)
        self.assertNotIn("control-group", unit)

    def test_proxy_settings_survive_a_clean_boot_environment(self):
        proxies = {'http_proxy': 'http://proxy.example:8118', 'HTTPS_PROXY': 'http://proxy.example:8118',
                   'no_proxy': 'localhost,.internal,127.0.0.1', 'NO_PROXY': 'localhost,.internal,127.0.0.1'}
        settings = runtime_environment({**proxies, 'API_KEY': 'never-persist', 'CODEX_THREAD_ID': 'unrelated'})
        observed = self.root / 'observed.json'
        script = ('import json,os,pathlib; pathlib.Path(' + repr(str(observed)) + ').write_text('
                  'json.dumps({k:os.environ.get(k) for k in ' + repr([*proxies, 'API_KEY', 'CODEX_THREAD_ID']) + '}))')
        manifest = register(self.manager_root, self.state, [sys.executable, '-B', '-c', script],
                            workdir=self.root, environment=settings)
        manifest = read_json(self.manager_root / 'services' / (manifest['id'] + '.json'))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.manager.reconcile(manifest)
        self.assertEqual(self.manager.children[manifest['id']].wait(timeout=5), 0)
        result = read_json(observed)
        self.assertEqual(result, {**proxies, 'API_KEY': None, 'CODEX_THREAD_ID': None})

    def test_credential_bearing_proxy_urls_are_not_saved_or_echoed(self):
        for value in ('http://user:PRIVATE_SENTINEL@proxy:8118',
                      'http://proxy:8118/?token=PRIVATE_SENTINEL',
                      'http://proxy:8118/#PRIVATE_SENTINEL', 'http://proxy:bad',
                      'http://proxy:8118\nPRIVATE_SENTINEL'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as error:
                    register(self.manager_root, self.state, [sys.executable, '-c', 'pass'],
                             workdir=self.root, environment={'HTTPS_PROXY': value})
                self.assertNotIn('PRIVATE_SENTINEL', str(error.exception))
        self.assertFalse((self.manager_root / 'services').exists())

    def test_instance_lock_and_boot_gate(self):
        sock = instance_lock('test-' + service_key(self.root))
        self.addCleanup(sock.close)
        self.assertIsNone(instance_lock('test-' + service_key(self.root)))
        cfg = {'enabled': True, 'devbox_id': 'test', 'pid1_entrypoint': '/platform/init.py'}
        self.assertTrue(eligible(cfg, 1, ['/platform/init.py'], {'MERLIN_JOB_ID': 'test'}))
        for pid, argv, env in [(42, ['/platform/init.py'], {'MERLIN_JOB_ID': 'test'}),
                               (1, ['/other.py'], {'MERLIN_JOB_ID': 'test'}),
                               (1, ['/platform/init.py'], {'MERLIN_JOB_ID': 'other'})]:
            self.assertFalse(eligible(cfg, pid, argv, env))

    def test_configuration_change_restarts_exact_service(self):
        old = self.register(health_timeout=120)
        self.manager.reconcile(old)
        identity = self.status()['process']
        new = self.register()
        self.assertEqual(new['health_timeout'], 120)
        self.manager.reconcile(new)
        self.assertFalse(is_owned(identity))
        self.assertNotEqual(self.status()['process'], identity)

    def test_boot_watchdog_restarts_real_manager_and_adopts_service(self):
        self.register()
        env = dict(x.split(b'=', 1) for x in Path('/proc/1/environ').read_bytes().split(b'\0') if b'=' in x)
        config = self.root / 'boot.json'
        config.write_text(json.dumps({'enabled': True, 'devbox_id': env.get(b'MERLIN_JOB_ID', b'').decode(),
            'root': str(self.manager_root), 'mountpoint': '/', 'watchdog_seconds': 30,
            'command': [sys.executable, '-B', str(Path(__file__).parents[1] / 'bin/covey_service.py'),
                        '--root', str(self.manager_root), 'run', '--interval', '.1']}))
        stop = threading.Event()
        watcher = threading.Thread(target=watch, args=(config,), kwargs={'stop': stop, 'interval': .05})
        watcher.start()
        def wait_for(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(.05)
            self.fail('watchdog recovery timed out')
        try:
            first = wait_for(lambda: manager_health(str(self.manager_root)))['process']
            # Its output goes to its own log on the mount, which the Manager rotates.
            log = self.manager_root / 'manager.log'
            self.assertEqual(Path(os.readlink(f"/proc/{first['pid']}/fd/2")).resolve(), log.resolve())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            service = wait_for(lambda: self.status().get('process'))
            os.kill(first['pid'], signal.SIGKILL)
            second = wait_for(lambda: (h if (h := manager_health(str(self.manager_root)))
                                      and h['process'] != first else None))['process']
            self.assertNotEqual(second, first)
            self.assertTrue(is_owned(service))
            self.assertEqual(self.status()['process'], service)
            set_desired(self.manager_root, self.state, 'stopped')
            wait_for(lambda: self.status().get('status') == 'stopped')
            self.assertFalse(is_owned(service))
            # Repo review E3: manager.log said nothing; each Manager and what it did to the service is a line.
            said = log.read_text()
            release = Path(__file__).resolve().parents[1].name
            for line in (f"manager started pid={first['pid']} release={release}",
                         f"manager started pid={second['pid']} release={release}",
                         f"started pid={service['pid']} generation=1", f"pid={service['pid']}: stopping"):
                self.assertIn(line, said)
        finally:
            stop.set()
            watcher.join(timeout=5)
            health = manager_health(str(self.manager_root))
            if health:
                terminate_owned(health['process'], timeout=2)
            identity = self.status().get('process')
            if identity:
                terminate_owned(identity, timeout=1)

    def test_the_manager_does_not_load_the_app_stack(self):
        # An import error anywhere in the app must not keep the supervising Manager from starting.
        loaded = subprocess.run([sys.executable, '-B', '-c', 'import sys, covey.service.manager; '
                                 'print(sorted(m for m in sys.modules if m.startswith("covey.app")))'],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
        self.assertEqual(loaded.stdout.strip(), '[]', loaded.stderr)

    def test_checking_a_running_service_does_not_load_the_app_stack(self):
        # The health check imported app/health.py at every pass: a release whose app stack fails to import stopped
        # the Manager that started or upgraded under it.
        manifest = self.register()
        self.manager.reconcile(manifest)
        with mock.patch.dict(sys.modules, {'covey.app': None, 'covey.app.health': None}):
            self.manager.reconcile(manifest)
        self.assertEqual((self.status()['status'], self.status()['health']), ('running', {'status': 'unavailable'}))

    def test_a_manager_log_that_cannot_be_opened_still_starts_the_manager(self):
        from covey.service.boot import manager_log
        blocked = self.root / 'file'
        blocked.write_text('')
        self.assertEqual(manager_log(str(blocked / 'services')), subprocess.DEVNULL)

    def test_the_manager_appends_its_own_output_to_its_log(self):
        log, repo = self.root / 'services' / 'manager.log', Path(__file__).resolve().parents[1]
        script = ('import sys; from pathlib import Path; from covey.service.manager import log_to; '
                  'log_to(Path(sys.argv[1])); print("out " + sys.argv[2], flush=True); '
                  'print("err " + sys.argv[2], file=sys.stderr, flush=True)')

        def child(target, label, output):
            return subprocess.run([sys.executable, '-B', '-c', script, str(target), label], cwd=repo, timeout=60,
                                  stdin=subprocess.DEVNULL, stdout=output, stderr=output)

        self.assertEqual(child(log, 'elsewhere', subprocess.DEVNULL).returncode, 0)  # An old watchdog's Manager.
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        with log.open('a') as already:  # ensure_manager and the new watchdog: no line twice.
            self.assertEqual(child(log, 'already', already).returncode, 0)
        self.assertEqual(log.read_text().splitlines(), ['out elsewhere', 'err elsewhere', 'out already', 'err already'])
        blocked = self.root / 'file'
        blocked.write_text('')
        kept = child(blocked / 'manager.log', 'kept', subprocess.PIPE)
        self.assertEqual((kept.returncode, kept.stdout, kept.stderr), (0, b'out kept\n', b'err kept\n'))

    def test_a_start_never_rotates_the_bridge_log_and_the_manager_says_what_it_did(self):
        # Repo review E3: the Manager rotated bridge.log at each of about 14 starts a day (4 copies: 10 hours),
        # and manager.log stayed empty: it never wrote a line.
        import contextlib
        import io
        manifest = self.register()
        console = self.state / 'bridge-console.log'
        self.assertEqual(manifest['log'], str(console))  # An older Manager rotates this one at each start.
        bridge_log = self.state / 'bridge.log'
        bridge_log.write_text('earlier lines\n')
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.manager.reconcile(manifest)
            first = self.status()['process']
            os.kill(first['pid'], signal.SIGKILL)
            self.manager.children[manifest['id']].wait(timeout=5)
            self.manager.reconcile(manifest)  # Backoff, then started again.
            self.manager.reconcile(manifest)
            second = self.status()['process']
            set_desired(self.manager_root, self.state, 'running', restart=True)
            self.manager.reconcile(read_json(self.manager_root / 'services' / (manifest['id'] + '.json')))
            third = self.status()['process']
        self.assertEqual(bridge_log.read_text(), 'earlier lines\n')
        self.assertEqual(sorted(p.name for p in self.state.glob('bridge*')), ['bridge-console.log', 'bridge.log'])
        key = manifest['id'][:8]
        lines = [line.split('] ', 1)[1] for line in said.getvalue().splitlines()]
        self.assertEqual(lines, [
            f'service {key} started pid={first["pid"]} generation=1 release=-',
            f'service {key} pid={first["pid"]} exited rc=-9 (failure 1); starting again in 0s',
            f'service {key} started pid={second["pid"]} generation=1 release=-',
            f'service {key} pid={second["pid"]}: generation 1 -> 2, replacing it',
            f'service {key} started pid={third["pid"]} generation=2 release=-'])
        self.assertRegex(said.getvalue().splitlines()[0], r'^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\] ')

    def test_the_bridge_appends_its_own_log_and_says_its_generation_and_release(self):
        # Repo review E3: a Bridge run by a Manager writes <state>/bridge.log itself and rotates it by size; what the
        # Manager gave it as output keeps only what came before. Its first line names the generation and code.
        repo = Path(__file__).resolve().parents[1]
        work, services = self.root / 'work', self.root / 'services'
        work.mkdir()
        state = self.state.resolve()
        (services / 'services').mkdir(parents=True)
        (services / 'services' / (service_key(state) + '.json')).write_text(json.dumps({'generation': 7}))
        (state / 'bridge.log').write_text('earlier lines\n')
        script = ('import sys, threading; from pathlib import Path; from unittest import mock; sys.path.insert(0, sys.argv[1]); '
                  'from covey.app.unified import UnifiedBridgeApp; from covey.core import locks; '
                  'app = UnifiedBridgeApp(script_path=Path(sys.argv[1]) / "bin/covey_bridge.py"); '
                  'stop = mock.patch.object(app, "init_target", side_effect=lambda ctx: sys.exit(f"card locks in {locks._cards}; "'
                  '"log kept by size: " + str(any(t.name == "bridge-log" for t in threading.enumerate())))); '
                  'direct = mock.patch("covey.feishu.sdk.lark_cli_direct"); '
                  'stop.start(); direct.start(); app.main(sys.argv[2:])')
        env = {**os.environ, 'COVEY_MANAGED': '1', 'COVEY_SERVICE_ROOT': str(services)}
        with (state / 'bridge-console.log').open('a') as console:
            subprocess.run([sys.executable, '-B', '-c', script, str(repo), '--state-dir',
                            str(state), '--workdir', str(work), '--profile', 'unit'],
                           cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=console, stderr=console, timeout=120)
        log = (state / 'bridge.log').read_text()
        self.assertTrue(log.startswith('earlier lines\n'), log[:200])
        self.assertRegex(log, rf'\] bridge started pid=\d+ generation=7 release={repo.name}\n')
        # Its last words, after the Bridge took its log.
        self.assertIn(f'card locks in {state / "cards"}; log kept by size: True', log)
        self.assertNotIn('bridge started', (state / 'bridge-console.log').read_text())

    def test_a_manager_whose_output_is_gone_still_replaces_the_bridge(self):
        from covey.service import manager
        manifest = self.register()
        self.manager.reconcile(manifest)
        first = self.status()['process']
        set_desired(self.manager_root, self.state, 'running', restart=True)
        with mock.patch.object(manager, 'print', side_effect=BrokenPipeError(32, 'Broken pipe'), create=True):
            self.manager.reconcile(read_json(self.manager_root / 'services' / (manifest['id'] + '.json')))
        self.assertFalse(is_owned(first))
        self.assertNotEqual(self.status()['process'], first)

    def test_the_bridge_log_is_rotated_by_size_into_ten_files(self):
        from covey.core import logs
        log = self.state / 'bridge.log'
        stop = threading.Event()
        stop.set()  # One check, then it returns.
        with mock.patch.object(logs, 'LOG_BYTES', 100):
            for n in range(12):
                log.write_text(str(n % 10) * 150)
                logs.keep_log(log, stop)
        self.assertEqual(sorted(p.name for p in self.state.glob('bridge.log.*')),
                         [f'bridge.log.{n}' for n in range(1, 10)])
        self.assertEqual((self.state / 'bridge.log.1').read_text(), '1' * 150)
        self.assertEqual(logs.LOG_KEEP, 9)

    def test_run_logs_before_the_manager_starts(self):
        from covey.service import manager
        order = []
        with mock.patch.object(manager, 'log_to', side_effect=lambda path: order.append(path)), \
                mock.patch.object(Manager, 'run', side_effect=lambda: order.append('run') or 0):
            self.assertEqual(manager.main(['--root', str(self.root / 'services'), 'run']), 0)
        self.assertEqual(order, [(self.root / 'services').resolve() / 'manager.log', 'run'])

    def test_boot_mount_gate_does_not_touch_unmounted_root(self):
        with mock.patch.object(Path, 'read_text', return_value='1 0 0:1 / / rw - overlay overlay rw\n'):
            self.assertFalse(mounted('/missing/data/services', '/missing/data'))
            self.assertFalse(mounted('/different/data', '/missing/data'))
            self.assertTrue(mounted('/data/services', '/'))

    @requires_extension()
    def test_installer_root_follows_service_root_unless_given(self):
        # Manager and doctor default to COVEY_SERVICE_ROOT; the installer must write the
        # boot registration and its receipt under the same root, and an explicit --root still wins.
        chosen = []
        def install(root, activate=False):
            chosen.append(root)
            return {'root': str(root)}
        with mock.patch('covey_internal.hosting.install_pid1', side_effect=install), \
             mock.patch.dict(os.environ, {'COVEY_SERVICE_ROOT': str(self.manager_root)}), \
             mock.patch('builtins.print'):
            with mock.patch.object(sys, 'argv', ['covey_service_install.py', '--mode', 'merlin-pid1']):
                install_main()
            with mock.patch.object(sys, 'argv', ['covey_service_install.py', '--mode', 'merlin-pid1',
                                                 '--root', str(self.root / 'explicit')]):
                install_main()
        self.assertEqual(chosen, [self.manager_root.resolve(), (self.root / 'explicit').resolve()])

    @requires_extension()
    def test_installed_hook_only_starts_thread_in_matching_pid1(self):
        from covey_internal.hosting import install_pid1
        entry = '/opt/tiger/arnold/arnold_entrypoint/exec/my_init.py'  # privacy: allow 扩展的 hosting 按平台 PID 1 入口路径认环境
        probe = subprocess.CompletedProcess([], 0, stdout='/ nfs4\n')
        with mock.patch('covey_internal.hosting.pid1_context', return_value=(['python', entry], 'test-devbox')), \
             mock.patch('covey_internal.hosting.subprocess.run', return_value=probe), \
             mock.patch('covey_internal.hosting.site.ENABLE_USER_SITE', True), \
             mock.patch('covey_internal.hosting.sys.executable', str(Path('/proc/1/exe').resolve())):
            receipt = install_pid1(self.manager_root, site_dir=self.root / 'site', config_dir=self.root / 'config')
        hook = Path(receipt['hook']).read_text()
        with mock.patch('threading.Thread') as thread:
            exec(hook, {})
            thread.assert_not_called()
        with mock.patch('os.getpid', return_value=1), mock.patch.object(sys, 'argv', [entry]), \
             mock.patch.dict(os.environ, {'MERLIN_JOB_ID': 'test-devbox'}), mock.patch('threading.Thread') as thread:
            exec(hook, {})
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        # install_pid1(activate=True) starts the installed file itself as a standalone watchdog.
        usage = subprocess.run([sys.executable, '-B', str(self.root / 'site' / 'lark_bridge_boot_hook.py'), '--help'],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(usage.returncode, 0, usage.stderr[-500:])
        self.assertTrue(usage.stdout.startswith('usage: lark_bridge_boot_hook.py'), usage.stdout[:200])
        self.assertFalse(receipt['reboot_verified'])
        self.assertFalse(receipt['active_pid1_hook'])


if __name__ == '__main__':
    unittest.main()
