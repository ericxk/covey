"""The memory watchdog (service/memguard.py) and the Bridge's sending of its alerts (app/memguard_alerts.py).

Only processes named mgt-* are in the rules' scope (only_comms), so the low test thresholds never touch real
work; the rg wrapper is exercised on a copy of an rg binary, never on the installed one.
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from support import exec_tempdir
from support.memguard import GuardCase, alive
from support.processes import leftover_check

from covey.app import memguard_alerts
from covey.app.memguard_alerts import MemguardAlerts
from covey.core.jobs import upsert_job_record
from covey.service import memguard

setUpModule, tearDownModule = leftover_check(timeout=5)
HOLD = ('import sys, time; n = int(sys.argv[1]) << 20; a = bytearray(n); '
        'a[::4096] = b"x" * len(range(0, n, 4096)); time.sleep(float(sys.argv[2]))')
SLEEP = 'import time; time.sleep(60)'


def limits(pid):
    out = {}
    for line in (memguard.read_text(f'/proc/{pid}/limits') or '').splitlines()[1:]:
        out[line[:26].strip()] = line[26:].split()[0] if line[26:].split() else None
    return out


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []



class RuleTests(GuardCase):
    def test_runaway_and_orphaned_searches_are_ended_and_everything_else_only_reported(self):
        config, data = self.config()
        marked = {**os.environ, memguard.TASK_DIR_ENV: '/state/runs/om_task'}
        search = self.start([self.prog('mgt-rg'), '-c', HOLD, '120', '60'], marked)
        big = self.start([self.prog('mgt-bigpy'), '-c', HOLD, '120', '60'])
        orphans = {name: self.spawn(*args) for name, args in (
            ('no_reader', ('pipe', self.prog('mgt-rg'), SLEEP)),
            ('reader', ('reader', self.prog('mgt-rg'), SLEEP)),
            ('find_exec', ('pipe', self.prog('mgt-find'), SLEEP, '-exec')),
            ('file', ('file', self.prog('mgt-rg'), SLEEP, str(self.run_dir / 'orphan-stdout.txt'))),
            ('chain', ('chain', self.prog('mgt-rg'), SLEEP)))}
        time.sleep(1)
        self.assertTrue(all(memguard.proc_stat(o['pid'])['ppid'] == 1 for o in orphans.values()))

        beat = self.run_worker(config, 7)

        self.assertEqual((beat['errors'], beat['kill_enabled']), (0, True), beat.get('last_error'))
        self.assertEqual(search.wait(5), -signal.SIGKILL)
        self.assertIsNone(big.poll())  # Over its alert level, never signalled.
        self.assertFalse(alive(orphans['no_reader']['pid']))
        for name in ('reader', 'find_exec', 'file'):
            self.assertTrue(alive(orphans[name]['pid']), name)
        self.assertTrue(alive(orphans['chain']['pid']) and alive(orphans['chain']['reader']))
        events = read_jsonl(data / 'events.jsonl')
        self.assertEqual(sorted((e['rule'], e['pid']) for e in events if e['kind'] == 'kill'),
                         sorted([('search_rss', search.pid), ('orphan', orphans['no_reader']['pid'])]))
        alerts = {}
        for path in (data / 'outbox').glob('*.json'):
            alert = json.loads(path.read_text())
            alerts.setdefault(alert['rule'], []).append(alert)
        self.assertEqual({rule for rule in alerts}, {'search_rss', 'orphan', 'big_process', 'container', 'long_search'})
        [killed] = alerts['search_rss']
        self.assertEqual((killed['pid'], killed['task_dir']), (search.pid, '/state/runs/om_task'))
        self.assertIn('【内存看门狗】已终止失控搜索进程（killed）', killed['text'])
        self.assertEqual([a['task_dir'] for a in alerts['big_process']], [None])
        self.assertEqual(len(read_jsonl(data / 'alerts.jsonl')), sum(map(len, alerts.values())))
        capture = json.loads(Path(next(e['capture'] for e in events if e['kind'] == 'kill'
                                       and e['pid'] == search.pid)).read_text())
        self.assertEqual(capture['kill']['result'], 'killed')
        self.assertTrue(capture['threads'] and capture['fds']['items'] and capture['ancestors'])
        self.assertNotIn(memguard.TASK_DIR_ENV, json.dumps(capture))  # The environment is not kept.
        self.assertTrue((data / 'status.json').exists())

    def test_without_kill_enabled_a_runaway_search_is_only_reported(self):
        config, data = self.config(kill_enabled=False, container_alert_ratio=2.0)
        search = self.start([self.prog('mgt-rg'), '-c', HOLD, '120', '60'])

        beat = self.run_worker(config, 2)

        self.assertFalse(beat['kill_enabled'])
        self.assertIsNone(search.poll())
        [alert] = [json.loads(p.read_text()) for p in (data / 'outbox').glob('*.json')]
        self.assertTrue(alert['text'].startswith('【内存看门狗】发现失控搜索进程，没有终止（本机没有开启终止）'))
        self.assertEqual([e['result'] for e in read_jsonl(data / 'events.jsonl') if e['kind'] == 'kill'],
                         ['not_enabled'])

    def test_thresholds_scale_with_the_container_unless_set(self):
        cfg = {**memguard.DEFAULTS}
        self.assertEqual(memguard.thresholds(cfg, 230 << 30), (8243, [32972, 65945, 131891]))
        self.assertEqual(memguard.thresholds(cfg, 16 << 30), (1024, [2293, 4587, 9175]))
        self.assertEqual(memguard.thresholds(cfg, 1 << 62), (8192, [32768, 65536, 131072]))  # No limit.
        cfg.update(search_kill_mib=8192, alert_levels_mib=[65536, 32768])
        self.assertEqual(memguard.thresholds(cfg, 16 << 30), (8192, [32768, 65536]))


def plain_rg():
    """An rg binary to copy: Codex's, the binary behind its wrapper when there is one, or rg on PATH."""
    for candidate in (memguard.codex_rg(), shutil.which('rg')):
        if candidate is None:
            continue
        candidate = Path(candidate).resolve()
        if memguard.rg_state(candidate) == 'wrapped':
            candidate = candidate.parent / '.real' / 'rg'
        if memguard.rg_state(candidate) == 'elf':
            return candidate
    return None


@unittest.skipIf(plain_rg() is None, 'no rg binary on this machine')
class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.folder = exec_tempdir()
        self.addCleanup(self.folder.cleanup)
        self.rg = Path(self.folder.name) / 'codex-path' / 'rg'
        self.rg.parent.mkdir()
        shutil.copy2(plain_rg(), self.rg)
        self.files = Path(self.folder.name) / 'files'
        (self.files / 'many').mkdir(parents=True)
        (self.files / 'a.txt').write_text('hello world\n')
        for n in range(50):
            (self.files / 'many' / f'f{n}.txt').write_text('x\n')

    def rg_run(self, *args, **env):
        return subprocess.run([str(self.rg), *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, cwd=self.files,
                              env={**os.environ, **env})

    def held(self, **env):
        """An rg reading its stdin: (its limits, its name), then it is ended."""
        proc = subprocess.Popen([str(self.rg), 'zzz'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env={**os.environ, **env})
        try:
            time.sleep(0.3)
            return limits(proc.pid), (memguard.proc_stat(proc.pid) or {}).get('comm')
        finally:
            proc.stdin.close()
            proc.wait(10)

    def test_the_wrapper_caps_rg_without_changing_how_it_behaves(self):
        self.assertEqual(memguard.rg_install(self.rg, 4096)['result'], 'installed')
        self.assertEqual(memguard.rg_state(self.rg), 'wrapped')
        self.assertIn(b'ripgrep', self.rg_run('--version').stdout)
        self.assertEqual([self.rg_run(*args).returncode for args in (('-l', 'hello', '.'), ('-l', 'nomatch_x', '.'), ('(', '.'))],
                         [0, 1, 2])
        cap, name = self.held()
        self.assertEqual((cap['Max address space'], name), (str(4096 << 20), 'rg'))
        # The host's core_pattern is a pipe, which ignores a zero limit: only one byte stops the dump.
        if Path('/usr/bin/prlimit').exists():
            self.assertEqual(cap['Max core file size'], '1')
        aborted = self.rg_run('--files', '.', RG_GUARD_AS_MIB='64')
        self.assertEqual(aborted.returncode, -signal.SIGABRT)
        self.assertIn(b'memory allocation', aborted.stderr)
        self.assertEqual(self.held(RG_GUARD_AS_MIB='0')[0]['Max address space'], 'unlimited')
        started = time.monotonic()
        waited = subprocess.Popen([str(self.rg), 'zzz'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  env={**os.environ, 'RG_GUARD_TIMEOUT': '1'})
        self.assertEqual(waited.wait(15), 124)
        waited.stdin.close()
        self.assertLess(time.monotonic() - started, 5)

    def test_an_old_wrapper_is_updated_and_uninstall_puts_the_binary_back(self):
        memguard.rg_install(self.rg, 4096)
        self.assertEqual(memguard.rg_install(self.rg, 4096)['result'], 'already_wrapped')
        self.assertEqual(memguard.rg_install(self.rg, 2048)['result'], 'updated')
        self.assertEqual(self.held()[0]['Max address space'], str(2048 << 20))
        self.assertEqual(memguard.rg_uninstall(self.rg)['result'], 'restored')
        self.assertEqual(memguard.rg_state(self.rg), 'elf')
        self.assertFalse((self.rg.parent / '.real').exists())


class AlertTests(unittest.TestCase):
    """The Bridge's side: each alert to its task's topic, or to the owner's private chat."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.state, self.outbox = root / 'harness-cli_x', root / 'guard' / 'outbox'
        self.outbox.mkdir(parents=True)
        config = root / 'memguard.json'
        config.write_text(json.dumps({'owner': memguard.OWNER, 'data_dir': str(self.outbox.parent)}))
        patcher = mock.patch.object(memguard, 'CONFIG', config)
        patcher.start()
        self.addCleanup(patcher.stop)
        upsert_job_record(self.state, 'om_task', message_id='om_task', job_id=42, chat_id='oc_topic_chat',
                          reply_to_message_id='om_root', status='done')
        self.sent, self.log, self.owner = [], [], 'oc_owner'
        messenger = SimpleNamespace(send_message=lambda profile, chat, text, key, reply_to_message_id='', voice=None:
                                    self.sent.append((chat, reply_to_message_id, text, key)) or True)
        runtime = SimpleNamespace(log=self.log.append, access=SimpleNamespace(notify_chat=lambda: self.owner))
        voice = SimpleNamespace(safe_key=lambda value: value[:50])
        self.alerts = MemguardAlerts(runtime, messenger, voice)
        self.ctx = SimpleNamespace(profile='harness', state_dir=self.state)
        self.n = 0

    def alert(self, text, task_dir=None, rule='search_rss'):
        self.n += 1
        path = self.outbox / f'20261104T000000{self.n:06d}Z-1-{self.n}.json'
        path.write_text(json.dumps({'id': path.stem, 'rule': rule, 'text': text, 'task_dir': task_dir}))
        return path

    def test_a_tasks_alert_goes_to_its_topic_and_one_without_a_task_to_the_owner(self):
        self.alert('【内存看门狗】已终止失控搜索进程（killed）', str(self.state / 'runs' / 'om_task'))
        self.alert('【内存看门狗】容器匿名内存已达 81%', rule='container')
        other = self.alert('另一个机器人的任务', '/elsewhere/harness-cli_y/runs/om_other')

        self.alerts.send_pending(self.ctx)

        self.assertEqual([(chat, reply, text) for chat, reply, text, _ in self.sent], [
            ('oc_topic_chat', 'om_root', '【内存看门狗】已终止失控搜索进程（killed）\n任务：#42'),
            ('oc_owner', '', '【内存看门狗】容器匿名内存已达 81%')])
        self.assertEqual(sorted(p.name for p in self.outbox.iterdir()), [other.name])  # Left for its Bridge.
        self.assertTrue(all(key.startswith('memguard-20261104T') for *_, key in self.sent))

    def test_an_alert_waits_for_the_owners_chat_and_old_ones_are_dropped(self):
        self.owner = ''
        waiting = self.alert('等主人私聊')
        self.alerts.send_pending(self.ctx)
        self.assertEqual((self.sent, waiting.exists()), ([], True))
        old = time.time() - memguard_alerts.MAX_AGE - 60
        os.utime(waiting, (old, old))
        (self.outbox / 'broken.json').write_text('{')
        self.alerts.send_pending(self.ctx)
        self.assertEqual(list(self.outbox.iterdir()), [])
        self.assertEqual(self.log, ['memory watchdog alert dropped (expired)', 'memory watchdog alert dropped (unreadable)'])

    def test_at_most_six_an_hour_and_the_next_one_says_how_many_were_not_sent(self):
        for n in range(8):
            self.alert(f'告警 {n}')
        self.alerts.send_pending(self.ctx)
        self.assertEqual(len(self.sent), memguard_alerts.PER_HOUR)
        self.assertEqual(self.alerts.suppressed, 2)
        self.alerts.sent = collections.deque()  # An hour later.
        self.alert('告警 8')
        self.alerts.send_pending(self.ctx)
        self.assertEqual(self.sent[-1][2], '告警 8\n另有 2 条告警因限流没有发送，见看门狗数据目录的 alerts.jsonl。')
        self.assertEqual(list(self.outbox.iterdir()), [])


class CopyTests(unittest.TestCase):
    def test_its_copies_of_core_task_processes_stay_the_same(self):
        # memguard.py runs as one file copied to local disk, without the package, so it keeps its own copy of the
        # search tools and the argv redaction of core/task_processes.py; the two must not drift apart.
        import shlex
        from covey.core import task_processes
        self.assertEqual(set(memguard.SEARCH_NAMES), task_processes.SEARCH_TOOLS)
        self.assertEqual(set(memguard.FIND_ACTIONS), task_processes.FIND_ACTIONS)
        for mine, core in ((memguard.SECRET_KV, task_processes.SECRET_VALUE),
                           (memguard.SECRET_FLAG, task_processes.SECRET_FLAG), (memguard.LONG, task_processes.LONG_RUN)):
            self.assertEqual((mine.pattern, mine.flags), (core.pattern, core.flags))
        argv = ['curl', '--token', 'fake-value', 'API_KEY=fake', 'x' * 45, '--password=fake', 'plain']
        self.assertEqual(' '.join(shlex.quote(arg) for arg in memguard.redact_argv(argv)),
                         task_processes.short_command(argv, limit=10_000))


if __name__ == '__main__':
    unittest.main()
