from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import covey.feishu.transport as transport
from covey.service.manager import rotate_live
from covey.feishu.connections import BusUnresponsive, tcp_receive_age, watch
from covey.feishu.delivery import send_message_with_fallback
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.metrics import durations
from covey.app.outbox import REDELIVER_LABEL, record_attempt, retry_label


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        previous = transport.configure_receipts(self.state)
        self.addCleanup(transport.configure_receipts, previous)
        self.addCleanup(transport.set_failure, None)

    def success(self):
        return subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"message_id":"om_result"}}', '')

    def test_uncertain_write_restarts_with_same_payload_and_deduplicates_forever(self):
        command = ['lark-cli', 'message', 'BODY_SENTINEL']
        with mock.patch.object(transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('cli', 1)), \
             mock.patch.object(transport.time, 'sleep'):
            self.assertIsNone(transport.call(command, uuid='stable'))
        receipt = next((self.state / 'delivery-receipts').glob('*.json'))
        self.assertNotIn('BODY_SENTINEL', receipt.read_text())
        transport.configure_receipts(self.state)  # A fresh process reopens the durable receipt.
        with mock.patch.object(transport.subprocess, 'run', return_value=self.success()) as send:
            self.assertIsNotNone(transport.call(command, uuid='stable'))
            self.assertIsNotNone(transport.call(command, uuid='stable'))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], command)
        logs = []
        self.assertIsNone(transport.call(command + ['changed'], uuid='stable', log=logs.append))
        self.assertEqual(transport.last_failure().category, 'idempotency_conflict')
        # 21:53:17 on 10-28: a send failed in 2 ms and the log said nothing of why. One line, never the body.
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].startswith('delivery failed category=idempotency_conflict code=- op=lark-cli'), logs)
        self.assertNotIn('BODY_SENTINEL', logs[0])

    def test_expired_ambiguous_write_requires_reconciliation_without_sending(self):
        with mock.patch.object(transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('cli', 1)), \
             mock.patch.object(transport.time, 'sleep'):
            transport.call(['send'], uuid='old')
        path = next((self.state / 'delivery-receipts').glob('*.json'))
        data = json.loads(path.read_text())
        data['uncertain_since'] -= 3601
        path.write_text(json.dumps(data))
        logs = []
        with mock.patch.object(transport.subprocess, 'run') as send:
            self.assertIsNone(transport.call(['send'], uuid='old', log=logs.append))
            send.assert_not_called()
        self.assertEqual(transport.last_failure().category, 'reconciliation_required')
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].startswith('delivery failed category=reconciliation_required code=- op='), logs)

    def test_auth_never_retries_and_rate_limit_respects_delay(self):
        for error, expected in [({'type': 'authentication'}, 'authentication'),
                                 ({'type': 'api', 'code': 99991400, 'retry_after': 60}, 'rate_limited')]:
            with self.subTest(error=error), mock.patch.object(transport.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 1, '', json.dumps({'ok': False, 'error': error}))) as send:
                self.assertIsNone(transport.call(['send', expected], uuid=expected))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(transport.last_failure().category, expected)
        record_attempt(self.state, 'job', transport.last_failure(), now=100)
        self.assertGreaterEqual(find_job_record(self.state, 'job')['outbox_next_at'], 160)

    def test_network_failure_never_switches_format_or_key(self):
        with mock.patch.object(transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('cli', 1)) as send, \
             mock.patch.object(transport.time, 'sleep'):
            sent = send_message_with_fallback(profile='test', chat_id='chat', text='x' * 100,
                idempotency_key='same', message_format='markdown', chunk_limit=1000, fallback_chunk_limit=10,
                log=lambda _: None, fallback_key_prefix='test', reply_to_message_id='root')
        self.assertFalse(sent)
        self.assertEqual(send.call_count, 3)
        self.assertTrue(all(c.args == send.call_args.args for c in send.call_args_list))
        self.assertIn('--markdown', send.call_args.args[0])

    def test_unverified_and_delivered_records_offer_no_replay_button(self):
        # Withdrawn after it was authorized: nothing to send, and the turn is not run again either.
        record = {'result_retry_policy': 'saved_delivery_only', 'verified_reply_state': 'invalidated',
                  'verified_authorization': 'a' * 64, 'backend': 'claude'}
        self.assertEqual(retry_label(record), '')
        # Never authorized on Codex: run again (T272 #2633 had no button), unless it paused on a native question.
        codex = {'result_retry_policy': 'saved_delivery_only', 'verified_reply_state': 'invalidated', 'backend': 'codex'}
        self.assertEqual(retry_label(codex), '检查并继续')
        self.assertEqual(retry_label({**codex, 'native_question_deferred': True}), '')
        self.assertEqual(retry_label({**codex, 'paused_question_delivery': {'generation': 2}}), '重新投递问题')
        self.assertEqual(retry_label({**codex, 'backend': 'claude', 'native_question_deferred': True}), '检查并继续')
        record.update(verified_reply_state='authorized', verified_reply_authorized=True)
        self.assertEqual(retry_label(record), REDELIVER_LABEL)
        record['delivery_ok'] = True
        self.assertEqual(retry_label(record), '')

    def test_an_undelivered_saved_reply_is_offered_as_redelivery(self):
        # retry_topic resends final_text for these without running the task; the button says so.
        from covey.app.recovery_cards import recovery_card
        record = {'job_id': 5, 'topic_id': 't', 'status': 'failed', 'delivery_ok': False, 'final_text': '答案'}
        self.assertEqual(retry_label(record), REDELIVER_LABEL)
        card = json.dumps(recovery_card(record), ensure_ascii=False)
        self.assertIn(REDELIVER_LABEL, card)
        self.assertIn('不重新执行任务', card)
        self.assertNotIn('新开一个任务', card)
        self.assertEqual(retry_label({**record, 'delivery_ok': True}), '检查并继续')
        stopped = {**record, 'checkpoint_question_deferred': True, 'checkpoint_question_stopped': True}
        self.assertEqual(retry_label(stopped), '检查并继续')


class HealthAndMetricsTests(unittest.TestCase):
    def test_sdk_remote_failure_does_not_fall_back_to_cli(self):
        from covey.feishu.cardkit import cardkit_api
        failure = subprocess.CompletedProcess([], 1, '', '{"ok":false,"error":{"type":"network"}}')
        sdk = mock.Mock(return_value=failure)
        with mock.patch.dict(os.environ, {'COVEY_CARD_TRANSPORT': 'sdk'}), \
             mock.patch('covey.feishu.sdk.runner', return_value=sdk), \
             mock.patch('covey.feishu.transport.subprocess.run') as cli, \
             mock.patch('covey.feishu.transport.time.sleep'):
            self.assertIsNone(cardkit_api('named', 'PUT', '/open-apis/cardkit/v1/cards/c',
                                         {'uuid': 'stable-update'}, log=lambda _: None))
        self.assertEqual(sdk.call_count, 3)
        cli.assert_not_called()

    def test_sdk_local_unavailable_falls_back_before_any_request(self):
        from covey.feishu.cardkit import cardkit_api
        with mock.patch.dict(os.environ, {'COVEY_CARD_TRANSPORT': 'sdk'}), \
             mock.patch('covey.feishu.sdk.runner', side_effect=ValueError('unsupported provider')), \
             mock.patch('covey.feishu.transport.subprocess.run', return_value=
                        subprocess.CompletedProcess([], 0, '{"ok":true}', '')) as cli:
            self.assertIsNotNone(cardkit_api('named', 'PUT', '/open-apis/cardkit/v1/cards/c',
                                            {'uuid': 'stable-update'}, log=lambda _: None))
        cli.assert_called_once()

    def test_sdk_credential_that_does_not_decrypt_is_unavailable_not_fatal(self):
        import importlib.util
        if not all(importlib.util.find_spec(name) for name in ('lark_oapi', 'cryptography')):
            self.skipTest('optional Feishu SDK not installed')
        from covey.feishu import sdk
        with warnings.catch_warnings():  # lark_oapi's own import warns (pkg_resources, utcfromtimestamp).
            warnings.simplefilter("ignore")
            importlib.import_module("lark_oapi")
        # lark_oapi.ws.client opens an event loop at import and never closes it.
        loop = getattr(sys.modules.get("lark_oapi.ws.client"), "loop", None)
        if loop is not None and not loop.is_running():
            self.addCleanup(loop.close)
        with tempfile.TemporaryDirectory() as tmp:
            home, data = Path(tmp) / 'home', Path(tmp) / 'data' / 'lark-cli'
            (home / '.lark-cli').mkdir(parents=True)
            data.mkdir(parents=True)
            files = {
                home / '.lark-cli/config.json': json.dumps({'apps': [{'name': 'named', 'appId': 'cli_abc', 'brand': 'feishu',
                    'appSecret': {'source': 'keychain', 'id': 'appsecret:cli_abc'}}]}).encode(),
                data / 'appsecret_cli_abc.enc': os.urandom(40),
                data / 'master.key': os.urandom(32),
            }
            for path, body in files.items():
                path.write_bytes(body)
                path.chmod(0o600)
            with mock.patch.dict(os.environ, {'HOME': str(home), 'LARKSUITE_CLI_DATA_DIR': str(data.parent)}), \
                 mock.patch.object(sdk.Path, 'home', return_value=home):
                with self.assertRaisesRegex(ValueError, 'unreadable'):
                    sdk.client('named')

    def test_live_rotation_keeps_open_writer_and_bounds_retained_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bridge.log'
            with path.open('ab', buffering=0) as writer:
                inode = path.stat().st_ino
                for index in range(7):
                    writer.write((str(index) * 200).encode())
                    self.assertTrue(rotate_live(path, 100, keep=2))
                    self.assertEqual(path.stat().st_ino, inode)
                writer.write(b'after-rotation')
            self.assertEqual(path.read_text(), 'after-rotation')
            self.assertEqual(Path(str(path) + '.1').read_text(), '6' * 200)
            self.assertEqual(len(list(Path(tmp).glob('bridge.log.*'))), 2)

    def test_tcp_transport_uses_only_selected_pid_and_receive_age(self):
        text = 'ESTAB 0 0 a b users:(("lark-cli",pid=12,fd=3))\n cubic lastrcv:650000\n'
        text += 'ESTAB 0 0 a b users:(("other",pid=123,fd=3))\n cubic lastrcv:1\n'
        self.assertEqual(tcp_receive_age(text, 12), 650)
        self.assertIsNone(tcp_receive_age(text, 9))

    def watch_samples(self, samples, uptime=None):
        stop = mock.Mock()
        stop.is_set.side_effect = [False] * len(samples) + [True]
        app = SimpleNamespace(runtime=SimpleNamespace(shutdown=stop, log=lambda _: None, connection_health={}))
        clock = mock.patch('covey.feishu.connections.time.monotonic', return_value=uptime) if uptime is not None \
            else contextlib.nullcontext()
        with mock.patch('covey.feishu.connections.bus_sample', side_effect=samples), \
             mock.patch('covey.feishu.connections.is_owned', return_value=True), \
             mock.patch('covey.feishu.connections.stop_bus') as kill, clock:
            watch(app.runtime, SimpleNamespace(profile='test'))
        return app, kill

    def test_a_hung_bus_is_replaced_even_just_after_the_host_booted(self):
        # time.monotonic() is the uptime: a host booted a minute ago (a fresh CI runner) must not look like
        # "restarted a moment ago".
        sample = {'status': 'receiving', 'process': {'pid': 12}, 'exclusive': True}
        _, kill = self.watch_samples([sample, BusUnresponsive(), BusUnresponsive(), BusUnresponsive()], uptime=60.0)
        kill.assert_called_once_with({'pid': 12})

    def test_quiet_tcp_receiving_bus_never_restarts(self):
        sample = {'status': 'receiving', 'process': {'pid': 12}, 'exclusive': True}
        _, kill = self.watch_samples([sample] * 5)
        kill.assert_not_called()

    def test_hung_bus_recovers_only_after_verified_exclusive_ownership(self):
        sample = {'status': 'receiving', 'process': {'pid': 12}, 'exclusive': True}
        app, kill = self.watch_samples([sample, BusUnresponsive(), BusUnresponsive(), BusUnresponsive()])
        kill.assert_called_once_with({'pid': 12})
        self.assertEqual(app.runtime.connection_health['recovery'], 'verified_bus_replaced')
        _, kill = self.watch_samples([BusUnresponsive()] * 4)
        kill.assert_not_called()

    def test_cli_orphan_is_unresponsive_even_with_exit_code_zero(self):
        from covey.feishu.connections import bus_sample
        payload = {'apps': [{'status': 'orphan', 'running': False, 'pid': 12}]}
        with mock.patch('covey.feishu.connections.subprocess.run', return_value=
                        subprocess.CompletedProcess([], 0, json.dumps(payload), '')), \
             mock.patch('covey.feishu.connections.process_identity', return_value={'pid': 12}):
            with self.assertRaises(BusUnresponsive) as caught:
                bus_sample('test', 100)
            self.assertEqual(caught.exception.identity, {'pid': 12})

    def test_a_malformed_event_status_is_unavailable_in_the_watchdog_and_the_doctor(self):
        from covey.feishu.connections import bus_sample
        from covey.service.doctor import event_report
        for payload in ([], {'apps': {}}, {'apps': ['x']}, {'apps': [{'consumers': [1]}]}):
            reply = subprocess.CompletedProcess([], 0, json.dumps(payload), '')
            with mock.patch('covey.feishu.connections.subprocess.run', return_value=reply):
                with self.assertRaises(ValueError):
                    bus_sample('test', 100)
                self.assertEqual(event_report('test'), {'status': 'unavailable'})
        failed = subprocess.CompletedProcess([], 4, '', 'no daemon')
        with mock.patch('covey.feishu.connections.subprocess.run', return_value=failed):
            self.assertEqual(event_report('test'), {'status': 'unavailable', 'exit_code': 4})

    def test_changed_orphan_identity_cannot_authorize_restarting_it(self):
        sample = {'status': 'receiving', 'process': {'pid': 12}, 'exclusive': True}
        _, kill = self.watch_samples([sample] + [BusUnresponsive({'pid': 13})] * 4)
        kill.assert_not_called()

    def test_shared_bus_never_restarted(self):
        sample = {'status': 'stalled', 'process': {'pid': 12}, 'exclusive': False}
        _, kill = self.watch_samples([sample] * 5)
        kill.assert_not_called()

    def test_timings_survive_reload_and_missing_old_stages_are_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            upsert_job_record(state, 'new', timings={'received': 1, 'submitted': 11, 'queued': 12})
            row = upsert_job_record(state, 'new', timings={'received': 999, 'dispatched': 14, 'delivered': 30})
            self.assertEqual(row['timings']['received'], 1)
            self.assertEqual(row['latency_ms']['queue_ms'], 2000)
            self.assertNotIn('first_output_ms', row['latency_ms'])
            self.assertEqual(durations({'received': 1}), {})


class TurnFromExceptionTest(unittest.TestCase):
    """worker._turn_from_exception: what the turn an exception ended becomes."""

    def test_each_exception_maps_to_its_status(self):
        from covey.app.worker import _turn_from_exception
        from covey.core.process import JobCancelled
        job = SimpleNamespace(job_id=7, interrupted=False, topic_id="", chat_id="")
        timeout = subprocess.TimeoutExpired("x", 1)
        self.assertEqual(_turn_from_exception(timeout, job, False)["status_message"], "任务超时")
        self.assertEqual(_turn_from_exception(JobCancelled("x"), job, False)["status"], "cancelled")
        self.assertEqual(_turn_from_exception(ValueError("x"), job, True),
                         {"status": "failed", "status_message": "任务异常（ValueError）",
                          "final_text": "任务 #7 处理出错（ValueError），未满足结果发布条件。"})
        job.interrupted = True  # A SIGTERM before the backend finished: interrupted, a timeout still fails.
        self.assertEqual(_turn_from_exception(ValueError("x"), job, False), {"status": "interrupted"})
        self.assertEqual(_turn_from_exception(JobCancelled("x"), job, False), {"status": "interrupted"})
        self.assertEqual(_turn_from_exception(JobCancelled("x"), job, True)["status"], "cancelled")
        self.assertEqual(_turn_from_exception(timeout, job, False)["status"], "failed")

    def test_a_backend_that_fails_as_a_stop_ends_it_is_stopped(self):
        # A 停止 SIGTERMs a backend whose turn's thread is held (app/task_controls.py hard_stop_later): whatever it
        # raises as it goes, the turn is stopped, not failed; once it finished (settling), a failure is still one.
        import threading
        from covey.app.worker import _turn_from_exception
        job = SimpleNamespace(job_id=7, interrupted=False, topic_id="", chat_id="", cancel_event=threading.Event())
        self.assertEqual(_turn_from_exception(RuntimeError("closed"), job, False)["status"], "failed")
        job.cancel_event.set()
        self.assertEqual(_turn_from_exception(RuntimeError("closed"), job, False)["status"], "cancelled")
        self.assertEqual(_turn_from_exception(ValueError("x"), job, True)["status"], "failed")
        self.assertEqual(_turn_from_exception(subprocess.TimeoutExpired("x", 1), job, False)["status"], "failed")


if __name__ == '__main__':
    unittest.main()
