"""Offline refusal replay, old runner process trees, durable notices and exact correlation."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from support.world import TopicFixture, wait_for
from covey.core import run_dir as rd
from covey.app import policy_guard, runner_spawn
from covey.backends.claude.policy import policy_signal
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.policy_feed import digest, match_task
from covey.core.policy_process import stop_tree
from covey.core.process import process_identity
from covey.core.state import write_json, read_json
from covey.core.session_guard import session_is_blocked, claim_native_session, finish_native_session
from covey.app.outbox import retry_label
from covey.feishu.transport import Failure, set_failure

# A policy source is any read-only adapter named in policy-source.json: given the cursor on stdin, it prints one
# {cursor, events, health} page. This one prints the page saved at argv[1].
FAKE_SOURCE = 'import sys; sys.stdin.read(); sys.stdout.write(open(sys.argv[1]).read())'


def event(kind, category, child='', evidence='server_audit'):
    return {'kind': kind, 'category': category, 'source_model': '', 'target_model': '',
            'scope': 'subagent' if child else 'main', 'child_id': child, 'response_id': '', 'evidence': evidence}


def observation(session='session', child='', kind='refusal', evidence='server_audit', ident='audit:run:3'):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {'id': ident, 'session': digest(session), 'request_time': now, 'time': now,
            'event': event(kind, 'frontier_llm', child, evidence=evidence)}


class PolicySourceTests(unittest.TestCase):
    def test_error_envelope_only_not_quoted_prose(self):
        err = {'type': 'error', 'error': {'type': 'invalid_request_error', 'code': 'frontier_llm_policy',
                                          'message': 'fixture'}}
        row = {'type': 'assistant', 'isApiErrorMessage': True, 'message': {'content': [
            {'type': 'text', 'text': 'API Error: 400 ' + json.dumps(err)}]}}
        self.assertEqual(policy_signal(row)['category'], 'frontier_llm')
        self.assertEqual(policy_signal(row)['evidence'], 'http_refusal')
        self.assertIsNone(policy_signal({**row, 'isApiErrorMessage': False}))
        self.assertIsNone(policy_signal({**row, 'isReplay': True}))
        row['message']['content'][0]['text'] = 'API Error: 400 ' + json.dumps({'type': 'error', 'error': {'message': 'frontier_llm'}})
        self.assertIsNone(policy_signal(row))

    def test_a_retired_gateways_refusal_wording_is_not_a_signal(self):
        """The removed Magpie gateway wrote its refusals as prose in the message; only explicit fields count now."""
        err = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
            'claude-opus-5-5 refused this request (safety filter: refusal (category frontier_llm: fixture)) via Claude Code'}}
        row = {'type': 'assistant', 'isApiErrorMessage': True, 'message': {'content': [
            {'type': 'text', 'text': 'API Error: 400 ' + json.dumps(err)}]}}
        self.assertIsNone(policy_signal(row))

    def test_old_task_ambiguity_future_and_resumed_interval(self):
        row = observation()
        record = {'backend': 'claude', 'status': 'running', 'session_id': 'session', 'created_at': '2026-01-01T00:00:00Z'}
        self.assertIs(match_task(row, [record], time.time())[0], record)
        self.assertIsNone(match_task(row, [record, dict(record)], time.time())[0])
        self.assertIsNone(match_task(row, [{**record, 'status': 'done'}], time.time())[0])
        self.assertIsNone(match_task(row, [{**record, 'created_at': '2099-01-01T00:00:00Z'}], time.time())[0])
        self.assertIsNone(match_task(row, [{**record, 'timings': {'backend_started': time.time()+60}}], time.time())[0])
        self.assertIsNone(match_task({**row, 'time': '2099-01-01T00:00:00Z'}, [record], time.time())[0])

    def test_a_page_is_kept_once_and_its_cursor_goes_back_to_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = {'cursor': {'offset': 7}, 'events': [observation()], 'health': {'unmapped': 0}}
            page = root / 'page.json'
            page.write_text(json.dumps(packet))
            seen = root / 'seen.json'
            command = [sys.executable, '-B', '-c',
                       'import sys; open(sys.argv[2], "w").write(sys.stdin.read()); '
                       'sys.stdout.write(open(sys.argv[1]).read())', str(page), str(seen)]
            write_json(root / 'policy-source.json', {'command': command})
            self.assertTrue(policy_guard.fetch(root))
            self.assertEqual(json.loads(seen.read_text()), {})
            self.assertTrue(policy_guard.fetch(root))
            self.assertEqual(json.loads(seen.read_text()), {'offset': 7})
            self.assertEqual(len(list((root/'policy-inbox').glob('*.json'))), 1)
            self.assertEqual(read_json(root / 'policy-source-state.json')['health'], {'unmapped': 0})
            page.write_text(json.dumps({**packet, 'events': [{**observation(), 'prompt': 'PRIVATE'}]}))
            with self.assertRaisesRegex(ValueError, 'invalid_policy_source_packet'):
                policy_guard.fetch(root)
            self.assertEqual(read_json(root / 'policy-source-state.json')['cursor'], {'offset': 7})

    def test_source_killed_by_signal_or_at_shutdown_is_not_a_failure(self):
        import threading
        for command, stopping, failed in ((['/bin/sh', '-c', 'kill -TERM $$'], False, False),
                                          (['/bin/sh', '-c', 'kill -INT $$'], False, False),
                                          (['/bin/sh', '-c', 'kill -HUP $$'], False, False),
                                          (['/bin/sh', '-c', 'kill -KILL $$'], False, True),
                                          (['/bin/sh', '-c', 'kill -SEGV $$'], False, True),
                                          (['/bin/sh', '-c', 'kill -KILL $$'], True, False),
                                          (['/bin/sh', '-c', 'exit 1'], True, False),
                                          (['/bin/sh', '-c', 'exit 1'], False, True)):
            with tempfile.TemporaryDirectory() as tmp:
                write_json(Path(tmp) / 'policy-source.json', {'command': command})
                runtime = SimpleNamespace(shutdown=threading.Event(), log=mock.Mock())
                if stopping:
                    runtime.shutdown.set()
                policy_guard.tick(SimpleNamespace(apps={}, runtime=runtime), SimpleNamespace(state_dir=Path(tmp)))
                state = Path(tmp) / 'policy-source-state.json'
                self.assertEqual(state.exists(), failed, command)
                self.assertEqual(runtime.log.called, failed, command)


class OldRunnerPolicyTests(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.harness.context = self.ctx
        self.gateway = SimpleNamespace(apps={'claude': self.app}, runtime=self.app.runtime)
        self.key = 'old-job'
        self.directory = rd.run_dir(self.ctx.state_dir, self.key)
        self.directory.mkdir(parents=True)
        ready = self.directory/'children.json'
        code = '''import subprocess,sys,time,os,json
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300)'],start_new_session=True)
open(sys.argv[2],'w').write(json.dumps({'child':child.pid}))
time.sleep(300)
'''
        self.proc = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.directory), str(ready)], start_new_session=True)
        self.addCleanup(self.cleanup_process)
        wait_for(self, lambda: ready.exists() and ready.read_text().endswith('}'))  # Created first, then written.
        child = json.loads(ready.read_text())['child']
        self.child_identity = process_identity(child)
        runner_spawn.register_runner(self.directory, self.proc.pid, Path('/old-release'), 77, self.key, 'claude')
        self.record = upsert_job_record(self.ctx.state_dir, self.key, job_id=77,
            backend='claude', status='running', created_at='2026-01-01T00:00:00Z', session_id='session',
            topic_id='topic', chat_id='oc_chat', reply_to_message_id='root', workdir=str(self.root),
            history_path=str(self.root/'history'), session_path=str(self.root/'session'))
        self.app.controls.repaint_recovery_card = mock.Mock(return_value=True)

    def cleanup_process(self):
        if self.proc.poll() is None:
            stop_tree(process_identity(self.proc.pid))
        self.proc.wait(timeout=5)

    def test_old_runner_stops_whole_tree_notice_once_and_late_success_cannot_win(self):
        claim_native_session(self.ctx, 'claude', 'session', 'old-run')
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(300)'], start_new_session=True)
        self.addCleanup(lambda: (unrelated.kill(), unrelated.wait()))
        row = observation(child=digest('child'))
        self.assertEqual(policy_guard.claim(self.ctx.state_dir, row, self.record), 'stop_requested')
        path = policy_guard.intent_path(self.ctx.state_dir, self.key)
        original_read = Path.read_text
        def stale_child_list(p, *args, **kwargs):
            value = original_read(p, *args, **kwargs)
            if str(p).startswith(f'/proc/{self.proc.pid}/task/') and p.name == 'children':
                return value + ' ' + str(unrelated.pid)
            return value
        with mock.patch.object(Path, 'read_text', stale_child_list):
            policy_guard.finish(self.gateway, self.ctx, path)
        self.proc.wait(timeout=5)
        self.assertIsNone(process_identity(self.child_identity['pid']))
        self.assertIsNone(unrelated.poll())
        record = find_job_record(self.ctx.state_dir, self.key)
        self.assertEqual(record['status'], 'cancelled')
        self.assertTrue(record['delivery_ok'])
        self.assertEqual(retry_label(record), '')
        self.assertIn('frontier_llm', record['final_text'])
        self.assertIn('SubAgent', record['final_text'])
        self.assertEqual(self.app.messenger.send_message.call_count, 1)
        self.assertEqual(policy_guard.claim(self.ctx.state_dir, row, record), 'already_stopped')
        policy_guard.finish(self.gateway, self.ctx, path)
        self.assertEqual(self.app.messenger.send_message.call_count, 1)
        latest = upsert_job_record(self.ctx.state_dir, self.key, status='done', final_text='false success', native_resume_allowed=True)
        self.assertEqual(latest['status'], 'cancelled')
        self.assertNotEqual(latest['final_text'], 'false success')
        self.assertFalse(latest['native_resume_allowed'])
        self.assertTrue(session_is_blocked(self.ctx, 'claude', 'session'))
        self.assertFalse(finish_native_session(self.ctx, 'claude', 'session', 'old-run', True))
        self.assertTrue(session_is_blocked(self.ctx, 'claude', 'session'))

    def test_notice_retry_after_restart_never_reexecutes_model(self):
        def temporary_failure(*a, **kw):
            set_failure(Failure('rate_limited', retryable=True, retry_after=1))
            return False
        self.app.messenger.send_message.side_effect = temporary_failure
        row = observation()
        policy_guard.claim(self.ctx.state_dir, row, self.record)
        path = policy_guard.intent_path(self.ctx.state_dir, self.key)
        policy_guard.finish(self.gateway, self.ctx, path)
        saved = read_json(path)
        self.assertEqual(saved['notice'], 'pending')
        saved['next_attempt'] = 0
        write_json(path, saved)
        self.app.messenger.send_message.side_effect = None
        self.app.messenger.send_message.return_value = True
        policy_guard.finish(self.gateway, self.ctx, path)
        self.assertEqual(read_json(path)['notice'], 'sent')
        self.assertEqual(len(self.backend.calls), 0)

    def test_pid_identity_mismatch_does_not_signal(self):
        identity = {**process_identity(self.proc.pid), 'start_ticks': '0'}
        self.assertFalse(stop_tree(identity)['confirmed'])
        self.assertIsNone(self.proc.poll())

    def test_a_stat_that_stops_reading_while_freezing_still_stops_the_tree(self):
        # Only FileNotFoundError was caught there: a stat that no longer decodes (a process may rename itself to a
        # name cut inside a UTF-8 character) raised out of stop_tree and left the whole tree stopped, never killed.
        stat, original_read = f"/proc/{self.child_identity['pid']}/stat", Path.read_text

        def undecodable_once_stopped(p, *args, **kwargs):
            value = original_read(p, *args, **kwargs)
            if str(p) == stat and value.rsplit(')', 1)[1].split()[0] == 'T':
                raise UnicodeDecodeError('utf-8', b'\xe6\xb5', 0, 2, 'unexpected end of data')
            return value
        with mock.patch.object(Path, 'read_text', undecodable_once_stopped):
            result = stop_tree(process_identity(self.proc.pid))
        self.assertTrue(result['confirmed'], result)
        self.proc.wait(timeout=5)
        wait_for(self, lambda: process_identity(self.child_identity['pid']) is None)

    def replay_feed(self, audit):
        root = self.root / 'remote'
        root.mkdir()
        row = (observation(child=digest('child'), ident='audit:r:2') if audit
               else observation(evidence='http_refusal', ident='http:1:0'))
        (root / 'page.json').write_text(json.dumps({'cursor': {'n': 1}, 'events': [row], 'health': {}}))
        write_json(self.ctx.state_dir/'policy-source.json',
                   {'command': [sys.executable, '-B', '-c', FAKE_SOURCE, str(root / 'page.json')]})
        policy_guard.tick(self.gateway, self.ctx)
        self.proc.wait(timeout=5)
        record = find_job_record(self.ctx.state_dir, self.key)
        self.assertEqual(record['policy_stop']['category'], 'frontier_llm')
        self.assertTrue(record['delivery_ok'])
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['policy_stop']['scope'], 'subagent' if audit else 'main')
        policy_guard.tick(self.gateway, self.ctx)
        self.assertEqual(self.app.messenger.send_message.call_count, 1)
        self.assertEqual(len(self.backend.calls), 0)

    def test_end_to_end_http400_replay(self):
        self.replay_feed(False)

    def test_end_to_end_server_audit_subagent_replay(self):
        self.replay_feed(True)

    def test_crash_after_kill_reconciles_without_signalling_new_pid(self):
        policy_guard.claim(self.ctx.state_dir, observation(), self.record)
        path = policy_guard.intent_path(self.ctx.state_dir, self.key)
        saved = read_json(path)
        def checkpoint(rows):
            saved['cleanup'] = {'processes': rows}
            write_json(path, saved)
        stop_tree(saved['runner'], before_kill=checkpoint)
        self.proc.wait(timeout=5)
        policy_guard.finish(self.gateway, self.ctx, path)
        self.assertTrue(read_json(path)['cleanup']['confirmed'])
        self.assertEqual(read_json(path)['notice'], 'sent')
