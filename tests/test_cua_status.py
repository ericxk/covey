"""Regressions from real Lark desktop observations; all external calls stubbed."""
import json
import unittest
from unittest import mock

from support import requires_extension
from covey.app.launch import remember_launch_card, SUBMIT_PREFIX
from covey.core.jobs import find_job_record
from covey.app.task_card import StreamingReply
from covey.core.trace import ToolTrace
from support.cards import current_token, trace_panel
from support.codex_questions import CodexNativeQuestionCase
from support.fake_lark import FakeCardKit
from support.world import LaunchCase


class SubmittedFormTests(LaunchCase):

    def ready(self):
        self.receive('task', 'hello')
        card, event = self.form('task')
        _, ctx = self.route('task')
        remember_launch_card(ctx, card, 'launch-card', event['message_id'], sequence=4)
        self.forms = ctx.state_dir / 'launch-forms'
        return event, event['action_name'][len(SUBMIT_PREFIX):]

    def record(self, token):
        """The form the card shows now; choosing the model replaced the first one."""
        return json.loads((self.forms / (current_token(self.forms.parent, token) + '.json')).read_text())

    @requires_extension()
    def test_submit_replaces_form_once_and_does_not_reenqueue(self):
        event, token = self.ready()
        with FakeCardKit({'ok': True}) as lark:
            self.submit(event)
            self.submit({**event, 'event_id': 'second-click'})
        self.assertEqual(len(lark.calls), 1)
        payload = lark.last.data
        card = json.loads(payload['card']['data'])
        self.assertEqual(payload['sequence'], 7)  # 4, then the model's and the budget's redraws, then this receipt.
        self.assertIn('已提交', str(card))
        self.assertIn('maso model', str(card))
        self.assertNotIn('select_static', str(card))
        self.assertNotIn('form_action_type', str(card))
        self.assertTrue(self.record(token)['receipt_published'])
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)

    @requires_extension()
    def test_failed_receipt_retries_with_new_sequence_without_model_rerun(self):
        event, token = self.ready()
        with FakeCardKit(side_effect=[None, {'ok': True}]) as lark:
            self.submit(event)
            self.assertEqual(self.record(token)['status'], 'done')
            self.submit({**event, 'event_id': 'retry-click'})
        self.assertEqual([call.data['sequence'] for call in lark.calls], [7, 8])  # After two redraws, as above.
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(self.notices(), [])

    def test_exception_in_receipt_never_reports_task_submission_failed(self):
        event, _ = self.ready()
        with FakeCardKit(side_effect=OSError('offline')):
            self.submit(event)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(self.notices(), [])

    def test_invalid_source_does_not_update_card(self):
        event, _ = self.ready()
        with FakeCardKit() as lark:
            self.submit({**event, 'operator_id': 'foreign'})
        self.assertEqual(lark.calls, [])
        self.assertTrue(self.app.runtime.jobs.queue.empty())


class DeliveryStatusTests(CodexNativeQuestionCase):

    def test_sent_label_follows_confirmed_delivery(self):
        c = self.prepare_case(send_ok=True)
        c.job.progress = StreamingReply('card', 'message')
        with mock.patch.object(c.app.cards, 'update_job_streaming_reply', return_value=True) as update:
            c.app.process_reply_job(c.job, c.ctx)
        sent = [call for call in update.call_args_list if call.kwargs.get('idempotency_prefix', '').endswith('-result-delivered')]
        self.assertEqual(len(sent), 1)
        # The card says what came of the turn: the lead of the verified reply that was sent (its heading).
        self.assertEqual(sent[0].args[2], '**↓ Codex verified report**')
        self.assertIn('# Codex verified report', [e['text'] for e in c.tx.events if e['kind'] == 'text'][0])
        # The status line at the top says 有结果; the receipt below doesn't say it again (S28).
        self.assertEqual(c.job.progress.outcome, '有结果')
        self.assertNotIn('已完成', sent[0].args[2])
        self.assertFalse(sent[0].kwargs['allow_replacement'])
        self.assertTrue(find_job_record(c.ctx.state_dir, c.job.message_id)['delivery_ok'])
        self.assertEqual(c.backend_run.call_count, 1)

    def test_real_stream_update_remains_enabled_until_delivery_receipt(self):
        c = self.prepare_case(send_ok=True)
        c.job.progress = StreamingReply('card', 'message')
        # Force the asynchronous close to win any race immediately. The real
        # update helper must still persist the sent label before this runs.
        def close(ctx, progress, ops):
            progress.disabled = True
        with mock.patch('covey.app.worker.close_streaming_reply_async', side_effect=close) as closer:
            c.app.process_reply_job(c.job, c.ctx)
        self.assertEqual(c.job.progress.last_content, '**↓ Codex verified report**')
        # The chat list's preview of the closed card names the outcome and time: the answer card sent beside it has the
        # lead (T288; both had it, 2026-11-02 review).
        self.assertRegex(c.job.progress.summary, r'^(T\d+|#\d+) 有结果 · 用时 \d+秒$')
        self.assertTrue(c.job.progress.disabled)
        closer.assert_called_once()
        self.assertTrue(find_job_record(c.ctx.state_dir, c.job.message_id)['delivery_ok'])

    def test_the_lead_comes_only_from_the_reply_once_it_was_sent(self):
        c = self.prepare_case(send_ok=True)
        c.job.progress = StreamingReply('card', 'message')
        writes = []
        with mock.patch.object(c.app.cards, 'update_job_streaming_reply',
                               side_effect=lambda *a, **k: writes.append((a[2], len(c.tx.events))) or True):
            c.app.process_reply_job(c.job, c.ctx)
        sent_at = next(i for i, e in enumerate(c.tx.events) if e['kind'] == 'text') + 1
        leads = [(text, seen) for text, seen in writes if 'Codex verified report' in text]
        self.assertEqual(len(leads), 1)
        self.assertGreaterEqual(leads[0][1], sent_at)  # Written after the reply went out, never before.
        self.assertNotIn('直接回复本话题', ''.join(text for text, _ in writes))

    def test_a_reply_sent_as_a_file_keeps_the_fixed_sentence(self):
        # Over 20,000 bytes the verified reply goes as a file: no words of it on the card.
        with mock.patch('support.codex_chain.ROOT_TEXT', '# 很长的报告\n\n' + '正文。' * 4000):
            c = self.prepare_case(send_ok=True)
            c.job.progress = StreamingReply('card', 'message')
            with mock.patch.object(c.app.cards, 'update_job_streaming_reply', return_value=True) as update:
                c.app.process_reply_job(c.job, c.ctx)
        # The file, then the small card with its 输入/输出 (cards/answer_card.py tail_card, 2026-11-08).
        self.assertEqual([e['kind'] for e in c.tx.events if e['kind'] in ('text', 'file')], ['file', 'text'])
        self.assertTrue([e for e in c.tx.events if e['kind'] == 'text'][0]['key'].endswith('-tail'))
        sent = [call.args[2] for call in update.call_args_list
                if call.kwargs.get('idempotency_prefix', '').endswith('-result-delivered')]
        self.assertEqual(sent, ['回答已单独发送在下方。'])

    def test_failed_delivery_never_claims_sent(self):
        c = self.prepare_case(send_ok=False)
        c.job.progress = StreamingReply('card', 'message')
        with mock.patch.object(c.app.cards, 'update_job_streaming_reply', return_value=True) as update:
            c.app.process_reply_job(c.job, c.ctx)
        self.assertFalse(any(call.kwargs.get('idempotency_prefix', '').endswith('-result-delivered') for call in update.call_args_list))
        self.assertFalse(any('Codex verified report' in call.args[2] for call in update.call_args_list))  # Unsent.
        self.assertFalse(find_job_record(c.ctx.state_dir, c.job.message_id)['delivery_ok'])

    def test_partial_result_sent_does_not_claim_execution_succeeded(self):
        c = self.prepare_case(partial=True, send_ok=True)
        c.job.progress = StreamingReply('card', 'message')
        with mock.patch.object(c.app.cards, 'update_job_streaming_reply', return_value=True) as update:
            c.app.process_reply_job(c.job, c.ctx)
        sent = [call for call in update.call_args_list
                if call.kwargs.get('idempotency_prefix', '').endswith('-result-delivered')]
        self.assertEqual(len(sent), 1)
        self.assertIn('本轮未全部完成', sent[0].args[2])
        self.assertNotIn('✅', sent[0].args[2])
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)['execution_status'], 'failed')

    def test_question_receipt_requires_confirmed_controls(self):
        c = self.prepare(form_ok=False)
        c.job.progress = StreamingReply('card', 'message')
        with mock.patch.object(c.app.cards, 'update_job_streaming_reply', return_value=True) as update:
            c.app.process_reply_job(c.job, c.ctx)
        self.assertFalse(any(call.kwargs.get('idempotency_prefix', '').endswith('-result-delivered')
                             for call in update.call_args_list))
        self.assertFalse(find_job_record(c.ctx.state_dir, c.job.message_id)['question_delivery_ok'])

    def test_receipt_exception_does_not_undo_delivery_or_reexecute(self):
        c = self.prepare_case(send_ok=True)
        c.job.progress = StreamingReply('card', 'message')
        def update(*args, **kwargs):
            if kwargs.get('idempotency_prefix', '').endswith('-result-delivered'):
                raise OSError('card offline')
            return True
        with mock.patch.object(c.app.cards, 'update_job_streaming_reply', side_effect=update):
            c.app.process_reply_job(c.job, c.ctx)
        record = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertTrue(record['delivery_ok'])
        self.assertEqual(record['status'], 'done')
        self.assertEqual(c.backend_run.call_count, 1)


class EmptyTraceTests(unittest.TestCase):
    def test_running_and_finished_empty_trace_are_distinct(self):
        trace = ToolTrace()
        self.assertIn('尚无可展示', str(trace_panel(trace, True)))
        trace.finish()
        panel = trace_panel(trace, False)
        self.assertIn('执行已结束', str(panel))
        self.assertNotIn('等待执行事件', str(panel))
        self.assertNotIn('第 1/1 页', str(panel))
        self.assertFalse(panel['expanded'])


if __name__ == '__main__':
    unittest.main()
