"""服务托管（launchd、systemd 用户服务、auto 选择）和 Harness 探测的测试；命令调用都用假的，不碰真实的 launchctl/systemctl。"""
from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covey.core import harness_probe, protocol
from support import exec_tempdir
from covey.service import doctor, install, launchd


class Calls:
    """假的 subprocess.run：记下命令，按前缀给返回码和输出。"""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, (code, out) in self.answers.items():
            if ' '.join(argv).startswith(prefix):
                return subprocess.CompletedProcess(argv, code, out, '')
        return subprocess.CompletedProcess(argv, 0, '', '')


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.root = Path(self.tmp.name) / 'data' / 'bridge-services'

    def test_plist_round_trips_with_keepalive_log_and_env(self):
        env = {'PATH': '/opt/homebrew/bin:/usr/bin', 'COVEY_DATA_HOME': '/data', 'API_TOKEN': 'never'}
        data = plistlib.loads(plistlib.dumps(launchd.plist_data(self.root, 'com.example.covey', python='/py/python3',
                                                                environ=env)))
        self.assertEqual(data['Label'], 'com.example.covey')
        self.assertIs(data['KeepAlive'], True)
        self.assertIs(data['RunAtLoad'], True)
        self.assertEqual(data['ProgramArguments'][0], '/py/python3')
        self.assertEqual(data['ProgramArguments'][-3:], ['--root', str(self.root), 'run'])
        self.assertEqual(data['StandardOutPath'], str(self.root / 'launchd.log'))
        self.assertEqual(data['EnvironmentVariables'],
                         {'PATH': '/opt/homebrew/bin:/usr/bin', 'COVEY_DATA_HOME': '/data',
                          'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertIs(data['AbandonProcessGroup'], True)

    def test_install_writes_plist_and_bootstraps_after_a_bootout(self):
        run = Calls()
        result = launchd.install(self.root, activate=True, run=run, home=self.home, environ={})
        path = self.home / 'Library/LaunchAgents/com.covey.manager.plist'
        self.assertEqual(result['plist'], str(path))
        self.assertEqual(plistlib.loads(path.read_bytes())['Label'], 'com.covey.manager')
        self.assertFalse(path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        uid = f'gui/{os.getuid()}'
        self.assertEqual([c[1] for c in run.calls], ['bootout', 'bootstrap', 'enable'])
        self.assertEqual(run.calls[1][2:], [uid, str(path)])
        self.assertEqual(run.calls[0][2], f'{uid}/com.covey.manager')

    def test_install_without_activate_only_writes_the_file(self):
        run = Calls()
        self.assertFalse(launchd.install(self.root, run=run, home=self.home)['started'])
        self.assertEqual(run.calls, [])

    def test_status_and_uninstall(self):
        run = Calls({'launchctl print': (0, 'state = running\n')})
        self.assertEqual(launchd.status(run=run, home=self.home)['registered'], False)
        launchd.install(self.root, run=run, home=self.home)
        row = launchd.status(run=run, home=self.home)
        self.assertTrue(row['loaded'] and row['running'])
        row = launchd.status(run=Calls({'launchctl print': (113, '')}), home=self.home)
        self.assertFalse(row['loaded'])
        removed = launchd.uninstall(run=run, home=self.home)
        self.assertTrue(removed['removed'])
        self.assertFalse(Path(removed['plist']).exists())
        self.assertEqual(run.calls[-1][1], 'bootout')


class ModeChoiceTests(unittest.TestCase):
    def hook(self, picked, modes=('ext-mode',)):
        return SimpleNamespace(MODES={m: None for m in modes}, auto_mode=lambda: picked)

    def test_hook_first_then_platform_then_manager(self):
        choose = install.choose_mode
        self.assertEqual(choose(self.hook('ext-mode'), platform='linux', systemd_usable=True)[0], 'ext-mode')
        # 钩子说适用但模式不在表里：不理它。
        self.assertEqual(choose(self.hook('x'), platform='linux', systemd_usable=True)[0], 'systemd')
        self.assertEqual(choose(self.hook(None), platform='linux', systemd_usable=True)[0], 'systemd')
        self.assertEqual(choose(None, platform='linux', systemd_usable=False, cron_usable=False)[0], 'manager')
        self.assertEqual(choose(None, platform='linux', systemd_usable=False, cron_usable=True)[0], 'cron')
        self.assertIn('does not start again after a reboot', choose(None, platform='linux', systemd_usable=False, cron_usable=False)[1])
        with mock.patch('covey.service.install.shutil.which', return_value='/bin/launchctl'):
            self.assertEqual(choose(None, platform='darwin')[0], 'launchd')
        with mock.patch('covey.service.install.shutil.which', return_value=None):
            self.assertEqual(choose(None, platform='darwin')[0], 'manager')

    def test_user_systemd_needs_a_user_manager_that_answers(self):
        with mock.patch('covey.service.install.shutil.which', return_value='/bin/systemctl'):
            self.assertTrue(install.user_systemd_usable(Calls()))
            self.assertFalse(install.user_systemd_usable(Calls({'systemctl --user show-environment': (1, '')})))

            def broken(*a, **k):
                raise subprocess.TimeoutExpired('systemctl', 5)
            self.assertFalse(install.user_systemd_usable(broken))
        with mock.patch('covey.service.install.shutil.which', return_value=None):
            self.assertFalse(install.user_systemd_usable(Calls()))

    def test_linger_hint_only_when_it_is_off(self):
        with mock.patch('covey.service.install.shutil.which', return_value='/bin/loginctl'):
            self.assertEqual(install.linger_hint(Calls({'loginctl': (0, 'no\n')}), user='me'),
                             'sudo loginctl enable-linger me')
            self.assertIsNone(install.linger_hint(Calls({'loginctl': (0, 'yes\n')}), user='me'))
            self.assertIsNone(install.linger_hint(Calls({'loginctl': (1, '')}), user='me'))


class MainAutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'services'

    def run_main(self, *argv, mode_hook=None):
        out = []
        with mock.patch('covey.service.install.extensions.hook', return_value=mode_hook), \
             mock.patch('builtins.print', side_effect=lambda s: out.append(json.loads(s))):
            install.main(['--root', str(self.root), *argv])
        return out[-1]

    def test_auto_falls_back_to_the_manager_and_says_so(self):
        with mock.patch.object(install, 'choose_mode', return_value=('manager', install.MANAGER_NOTE)):
            result = self.run_main('--mode', 'auto')
        self.assertEqual((result['mode'], result['autostart'], result['auto']), ('manager', False, True))
        self.assertIn('reboot', result['note'])
        self.assertEqual(json.loads((self.root.resolve() / 'boot-installation.json').read_text())['mode'], 'manager')

    def test_auto_uses_the_hook_when_it_applies(self):
        seen = []
        hook = SimpleNamespace(MODES={'ext-mode': lambda root, args: seen.append(root) or {'mode': 'ext-mode'}},
                               auto_mode=lambda: 'ext-mode', add_arguments=lambda parser: None)
        with mock.patch.object(install.shutil, 'which', return_value=None):
            result = self.run_main('--mode', 'auto', mode_hook=hook)
        self.assertEqual(result['mode'], 'ext-mode')
        self.assertEqual(seen, [self.root.resolve()])

    def test_auto_picks_launchd_on_macos_and_writes_the_receipt(self):
        with mock.patch.object(install, 'choose_mode', return_value=('launchd', 'macOS')), \
             mock.patch.object(install.launchd, 'install', return_value={'mode': 'launchd', 'label': 'l'}) as put:
            result = self.run_main('--mode', 'auto', '--activate')
        put.assert_called_once_with(self.root.resolve(), 'com.covey.manager', activate=True)
        self.assertEqual(result['auto_reason'], 'macOS')
        self.assertEqual(json.loads((self.root.resolve() / 'boot-installation.json').read_text())['mode'], 'launchd')

    def test_explicit_systemd_without_a_running_systemd_is_still_an_error(self):
        with mock.patch('covey.service.install.Path.is_dir', return_value=False):
            with self.assertRaises(ValueError):
                self.run_main('--mode', 'systemd')

    def test_doctor_reads_the_receipt_of_each_core_mode(self):
        manager = doctor.hosting_report({'mode': 'manager', 'note': 'n'})
        self.assertEqual((manager['status'], manager['autostart']), ('ready', False))
        self.assertIsNone(doctor.hosting_report({'mode': 'ext-mode'}))
        with mock.patch('covey.service.launchd.status', return_value={'registered': True, 'loaded': True}):
            self.assertEqual(doctor.hosting_report({'mode': 'launchd'})['status'], 'ready')
        with mock.patch('covey.service.launchd.status', return_value={'registered': True, 'loaded': False}):
            self.assertEqual(doctor.hosting_report({'mode': 'launchd'})['status'], 'degraded')
        with mock.patch('covey.service.install.systemd_status',
                        return_value={'registered': True, 'enabled': True, 'active': True}):
            self.assertEqual(doctor.hosting_report({'mode': 'systemd-user', 'hint': 'h'})['hint'], 'h')
        self.assertEqual(doctor.boot_report(self.root)['status'], 'unverified')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = exec_tempdir()  # 假的 claude/codex 要能直接执行
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def fake(self, name, body):
        path = self.dir / 'bin'
        path.mkdir(exist_ok=True)
        script = path / name
        script.write_text('#!/bin/sh\n' + body + '\n')
        script.chmod(0o755)
        return {'PATH': str(path) + ':/usr/bin:/bin'}

    def test_classification(self):
        for output, code, reason in [
                ('ok', 0, 'ok'), ('Not logged in · Please run /login', 1, 'not_logged_in'),
                ('401 Unauthorized', 1, 'not_logged_in'),
                ('error sending request for url (https://chatgpt.com): connection refused', 1, 'network'),
                ('getaddrinfo ENOTFOUND api.anthropic.com', 1, 'network'),
                ('something odd', 3, 'other'), ('', 0, 'other')]:
            with self.subTest(output=output):
                self.assertEqual(harness_probe.classify(output, code)['reason'], reason)

    def test_probe_runs_the_real_command_and_maps_failures(self):
        self.assertEqual(harness_probe.probe('claude', env={'PATH': '/nonexistent'})['reason'], 'not_installed')
        env = self.fake('claude', 'echo ok')
        self.assertEqual(harness_probe.probe('claude', env=env)['ok'], True)
        env = self.fake('claude', 'echo "Not logged in" >&2; exit 1')
        self.assertEqual(harness_probe.probe('claude', env=env)['reason'], 'not_logged_in')
        env = self.fake('codex', 'echo "connection refused: proxy" >&2; exit 1')
        self.assertEqual(harness_probe.probe('codex', env=env)['reason'], 'network')
        env = self.fake('codex', 'sleep 5')
        result = harness_probe.probe('codex', env=env, timeout=0.3)
        self.assertEqual((result['ok'], result['reason']), (False, 'timeout'))

    def test_the_commands_are_the_cheap_ones(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'ok', '')
        env = {**self.fake('claude', ''), **self.fake('codex', '')}
        with mock.patch('covey.core.harness_probe.subprocess.run', run):
            harness_probe.probe('claude', env=env)
            harness_probe.probe('codex', env=env)
        self.assertEqual(calls[0][1:3], ['-p', harness_probe.PROMPT])
        self.assertEqual(calls[1][1:3], ['exec', '--skip-git-repo-check'])

    def test_a_harness_without_a_probe_only_needs_its_program(self):
        env = self.fake('dsh', 'exit 9')
        result = harness_probe.probe('dsh', env=env)
        self.assertEqual((result['ok'], result['probed']), (True, False))

    def test_details_are_short_and_have_no_secrets(self):
        result = harness_probe.classify('x\n' * 50 + 'failed token=abcdef123456 sk-ant-' + 'a' * 40, 1)
        self.assertLessEqual(len(result['detail']), 240)
        self.assertNotIn('abcdef123456', result['detail'])
        self.assertNotIn('sk-ant', result['detail'])

    def test_cache_ttl_and_fingerprint(self):
        env = self.fake('claude', 'echo "Not logged in" >&2; exit 1')
        first = harness_probe.cached_probe(self.dir / 'cache', 'claude', env=env)
        self.assertFalse(first['cached'])
        self.assertTrue(harness_probe.cached_probe(self.dir / 'cache', 'claude', env=env)['cached'])
        # 失败的结果 10 分钟后作废，通过的 6 小时。
        later = time.time() + harness_probe.FAIL_TTL + 1
        self.assertIsNone(harness_probe.read_cache(self.dir / 'cache', 'claude', env=env, now=later))
        # 换了代理设置，缓存作废。
        self.assertIsNone(harness_probe.read_cache(self.dir / 'cache', 'claude', env={**env, 'HTTPS_PROXY': 'http://p:1'}))
        env = self.fake('claude', 'echo ok')
        harness_probe.cached_probe(self.dir / 'cache', 'claude', env=env, refresh=True)
        self.assertIsNone(harness_probe.read_cache(self.dir / 'cache', 'claude', env=env,
                                                   now=time.time() + harness_probe.OK_TTL + 1))
        self.assertIsNotNone(harness_probe.read_cache(self.dir / 'cache', 'claude', env=env,
                                                      now=time.time() + harness_probe.FAIL_TTL + 1))

    def test_retry_reason_names_a_network_failure_only_when_the_message_does(self):
        self.assertEqual(protocol.retry_reason('transport_error', 'error sending request for url'), protocol.NETWORK_REASON)
        self.assertEqual(protocol.retry_reason('transport_error', 'Failed to refresh token: 403 Forbidden'),
                         protocol.NETWORK_REASON)
        self.assertEqual(protocol.retry_reason('transport_error', 'upstream said no'), protocol.UNAVAILABLE_REASON)
        self.assertEqual(protocol.retry_reason('rate_limited', 'proxy 429'), protocol.RATE_LIMITED_REASON)


class AvailabilityProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {'COVEY_DATA_HOME': self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        from covey.app import availability
        self.availability = availability
        self.ctx = SimpleNamespace(backend_cmd=['sh'], workdir=Path(self.tmp.name), model='m')

    def cache(self, reason):
        ok = reason == 'ok'
        row = {'ok': ok, 'reason': reason, 'detail': '', 'checked_at': time.time(),
               'fingerprint': harness_probe._fingerprint('codex', ['sh'], self.availability.child_process_env(self.ctx.workdir))}
        (harness_probe.probe_directory()).mkdir(parents=True)
        (harness_probe.probe_directory() / 'codex.json').write_text(json.dumps(row))

    def test_off_by_default_nothing_changes(self):
        self.cache('not_logged_in')
        self.assertEqual(self.availability.local_check('codex', self.ctx)['status'], 'configured')

    def test_on_a_cached_login_failure_makes_the_harness_unavailable_but_a_network_one_does_not(self):
        self.cache('not_logged_in')
        with mock.patch.dict(os.environ, {self.availability.PROBE_ENV: '1'}):
            check = self.availability.local_check('codex', self.ctx)
        self.assertEqual((check['status'], check['reason']), ('unavailable', 'authentication'))
        (harness_probe.probe_directory() / 'codex.json').unlink()
        harness_probe.probe_directory().rmdir()
        self.cache('network')
        with mock.patch.dict(os.environ, {self.availability.PROBE_ENV: '1'}):
            self.assertEqual(self.availability.local_check('codex', self.ctx)['status'], 'configured')

    def test_the_watch_puts_the_probe_into_the_health_snapshot_when_on(self):
        harness = SimpleNamespace(context=self.ctx)
        gateway = SimpleNamespace(harnesses={'codex': harness}, runtime=SimpleNamespace())
        self.ctx.state_dir = Path(self.tmp.name)
        self.cache('network')
        with mock.patch.dict(os.environ, {self.availability.PROBE_ENV: '1'}):
            self.availability.sample(gateway, self.ctx)
        probe = gateway.runtime.availability_health['preflight']['codex']['probe']
        self.assertEqual((probe['ok'], probe['reason'], probe['cached']), (False, 'network', True))
        self.availability.sample(gateway, self.ctx)
        self.assertNotIn('probe', gateway.runtime.availability_health['preflight']['codex'])


if __name__ == '__main__':
    unittest.main()
