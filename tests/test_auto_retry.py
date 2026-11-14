"""Automatic 检查并继续 after a model rate limit, an incomplete turn or a restart (app/auto_retry.py), on the
live Bridge's apps."""
from __future__ import annotations

import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.fake_backends import SID, RecordingBackend
from support.world import UnifiedCase
from support.records import write_topic

from covey.app import auto_retry
from covey.core.result import STATUS_MESSAGES
from covey.app.card_actions import ACTIONS, BOT_ONLY
from covey.core.harness import HarnessCapabilities, RunResult, backend_capabilities
from covey.core.jobs import find_job_record, list_job_records, upsert_job_record
from covey.core.result import ExecutionOutcome, VerifiedRootReply
from covey.core.roster import NAMES
from covey.core.workspaces import read_topic


RATE_LIMITED = ('failed', 'rate_limited')
INCOMPLETE = ('timed_out', 'settle_timeout')  # Claude's main task ended before its subtasks.
INCOMPLETE_STATUS = '本轮执行未全部完成，原生会话不可续接'  # turn_settle._settle_status for it


class Failing(RecordingBackend):
    """Ends every turn as ``ending`` (status, error_code) with no reply, as Claude's stream reports it;
    declares automatic retries as Claude does unless ``retry`` is False. ``reply``: keep a verified reply.
    ``withdrawn``: answer first, then withdraw it, as Claude does when a background task wakes it."""

    def __init__(self, *, retry=True, reply=False, ending=RATE_LIMITED, withdrawn=False):
        super().__init__()
        self.retry, self.reply, self.ending, self.withdrawn = retry, reply, ending, withdrawn

    def capabilities(self, ctx):
        return HarnessCapabilities(rate_limit_retry=self.retry, incomplete_retry=self.retry)

    def run(self, request):
        self.calls.append((request.context, request.prompt, request.session_id))
        done = dict(identity_confirmed=True, cleanup_confirmed=True, protection_confirmed=True)
        if self.withdrawn:
            request.callbacks.verified_reply(VerifiedRootReply("claude", request.inputs.run_id, SID, ("synthetic-user",),
                                                               ("synthetic-root",), "Waiting for the subtasks."))
            request.callbacks.verified_reply(None)
        if self.reply:
            reply = VerifiedRootReply("claude", request.inputs.run_id, SID, ("synthetic-user",), ("synthetic-root",),
                                      "Part of the answer.")
            request.callbacks.verified_reply(reply)
            outcome = ExecutionOutcome(*self.ending, reply_fingerprint=reply.fingerprint, **done)
            return RunResult(reply.text, SID, True, reply, outcome)
        outcome = ExecutionOutcome(*self.ending, **done)
        return RunResult(outcome.failure_reply(), None, True, None, outcome)


class AutoRetryTest(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.claude = self.app.apps['claude']
        self.backend = self.claude.harness.backend = Failing()
        self.claude.cards.update_record_elements = mock.Mock(return_value=True)

    def failed_job(self, mid='task', automatic=0, continued=0, timeout=None):
        """A Claude task in its own topic that ended as the backend's ``ending``: its record."""
        self.receive(mid, 'initial')
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, 'chat', 'claude')
        app, ctx = self.route(mid)
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        job.auto_retry, job.auto_continue = automatic, continued
        if timeout is not None:
            job.timeout = timeout
        app.process_reply_job(job, app.harness.context)
        self.topic_ctx = ctx
        return find_job_record(self.ctx.state_dir, job.message_id)

    def tick(self, record, *, after=1, bridge=None):
        auto_retry.tick(bridge or self.app, self.ctx, now=record['auto_retry_at'] + after)
        return find_job_record(self.ctx.state_dir, record['key'])

    def test_a_tick_copies_only_the_retries_and_the_records_of_their_topics(self):
        record = self.failed_job()
        for number in range(20):
            upsert_job_record(self.ctx.state_dir, f'other-{number}', job_id=1000 + number, topic_id=f'other-{number}',
                              chat_id=record['chat_id'], status='done')
        copied, real = [], copy.deepcopy
        fake = SimpleNamespace(deepcopy=lambda value: copied.append(value.get('key')) or real(value))
        with mock.patch('covey.core.store.copy', fake):
            self.assertEqual(self.tick(record, after=-1)['auto_retry_state'], 'pending')
        self.assertIn(record['key'], copied)
        self.assertEqual([key for key in copied if str(key).startswith('other-')], [])

    def queued(self):
        return list(self.app.runtime.jobs.queue.queue)

    def run_queued(self, ending):
        """Run the queued retry until it ends as ``ending``: (its job, its record)."""
        job = self.app.runtime.jobs.queue.get_nowait()
        self.backend.ending = ending
        self.claude.process_reply_job(job, self.claude.harness.context)
        return job, find_job_record(self.ctx.state_dir, job.message_id)

    def click(self, kind, record, event='click'):
        self.claude.controls.task_action(self.topic_ctx, {
            'chat_id': 'chat', 'event_id': event,
            'action_value': {'kind': kind, 'topic_id': record['topic_id'], 'job_id': record['job_id']}})
        return self.said()[-1]

    def test_a_rate_limited_turn_schedules_one_retry_and_says_when(self):
        before = time.time()
        record = self.failed_job()
        self.assertEqual((record['status'], record['auto_retry_state'], record['auto_retry_attempt']),
                         ('failed', 'pending', 1))
        self.assertGreaterEqual(record['auto_retry_at'], before + 60)
        self.assertLess(record['auto_retry_at'], time.time() + 61)
        self.assertRegex(record['status_message'], r'^模型服务限流，将于 \d\d:\d\d 自动重试（第 1/2 次）$')
        self.assertIn('模型服务限流', record['final_text'])

    def test_the_due_retry_is_queued_once_as_the_button_does(self):
        record = self.failed_job()
        self.assertEqual(self.tick(record, after=-1)['auto_retry_state'], 'pending')  # Not due yet.
        self.assertEqual(self.queued(), [])
        after = self.tick(record)
        self.assertEqual((after['auto_retry_state'], after['status_message']),
                         ('queued', '模型服务限流，已自动重试（第 1/2 次）'))
        [job] = self.queued()
        self.assertEqual((job.auto_retry, job.user_text), (1, 'initial'))
        self.assertEqual(job.binding_revision, record['binding_revision'])  # The topic's binding.
        self.assertEqual(job.recovery_context['previous_status'], 'failed')
        self.assertEqual(after['retried_by'], job.message_id)
        retry = find_job_record(self.ctx.state_dir, job.message_id)
        self.assertEqual((retry['auto_retry'], retry['retry_of'], retry['status_message']),
                         (1, record['key'], '自动重试已入队（第 1/2 次）'))
        self.tick(record, after=600)
        self.assertEqual(len(self.queued()), 1)  # Never a second retry for one failure.

    def test_a_restarted_bridge_fires_a_pending_retry(self):
        record = self.failed_job()
        restarted, _ = self.bridge()
        restarted.messenger.send_message = mock.Mock(return_value=True)
        for app in restarted.apps.values():
            app.cards.create_streaming_reply = mock.Mock(return_value=None)
        # The restarted Bridge has the real Claude backend: it declares the retry.
        self.assertEqual(self.tick(record, bridge=restarted)['auto_retry_state'], 'queued')
        [job] = list(restarted.runtime.jobs.queue.queue)
        self.assertEqual(job.auto_retry, 1)

    def test_the_second_retry_waits_longer_and_there_is_no_third(self):
        before = time.time()
        second = self.failed_job('second', automatic=1)
        self.assertEqual((second['auto_retry_state'], second['auto_retry_attempt']), ('pending', 2))
        self.assertGreaterEqual(second['auto_retry_at'], before + 180)
        self.assertIn('（第 2/2 次）', second['status_message'])
        last = self.failed_job('third', automatic=2)
        self.assertNotIn('auto_retry_state', last)
        self.assertEqual(last['status_message'], STATUS_MESSAGES['rate_limited'])

    def test_checking_now_replaces_the_pending_retry(self):
        record = self.failed_job()
        self.click('bridge_job_retry', record)
        [job] = self.queued()
        self.assertEqual(job.auto_retry, 0)
        self.app.runtime.unregister_reply_job(self.app.runtime.jobs.queue.get_nowait())
        after = self.tick(record, after=-30)
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note']), ('cancelled', 'manual'))
        self.tick(record)
        self.assertEqual(self.queued(), [])
        self.claude.cards.update_record_elements.assert_not_called()  # The click already replaced the card.

    def test_stopping_the_automatic_retry(self):
        record = self.failed_job()
        self.assertIn(f"已停止任务 #{record['job_id']} 的自动重试", self.click(auto_retry.CANCEL_KIND, record))
        after = find_job_record(self.ctx.state_dir, record['key'])
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note'], after['status_message']),
                         ('cancelled', 'stopped', STATUS_MESSAGES['rate_limited']))
        elements = self.claude.cards.update_record_elements.call_args.args[2]
        self.assertEqual(elements[0]['content'], auto_retry.CANCELLED['stopped'])
        self.assertNotIn(auto_retry.CANCEL_KIND, json.dumps(elements[1]))
        self.tick(record)
        self.assertEqual(self.queued(), [])
        self.assertIn('没有待执行的自动重试', self.click(auto_retry.CANCEL_KIND, record, 'click-2'))

    def test_a_new_message_in_the_topic_cancels_it(self):
        record = self.failed_job()
        self.receive('next', 'another request', root='task')
        after = self.tick(record, after=-30)
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note']), ('cancelled', 'new_message'))
        self.assertTrue(all(job.auto_retry == 0 for job in self.queued()))

    def test_a_model_switch_cancels_it(self):
        record = self.failed_job()
        topic = read_topic(self.topic_ctx)
        topic['switches'] = [{'revision': topic['binding_revision'] + 1}]
        topic['binding_revision'] += 1
        write_topic(self.topic_ctx, topic)
        after = self.tick(record)
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note']), ('cancelled', 'model_switch'))
        self.assertEqual(self.queued(), [])

    def test_a_submit_never_repeats_after_an_error_or_a_stop_in_between(self):
        record = self.failed_job()
        with mock.patch.object(self.claude.controls, 'retry_topic', side_effect=RuntimeError('card send')):
            self.assertEqual(self.tick(record)['auto_retry_state'], 'uncertain')
        self.assertEqual(self.tick(record, after=600)['auto_retry_state'], 'uncertain')
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.claude.cards.update_record_elements.call_args.args[2][0]['content'],
                         auto_retry.CANCELLED['uncertain'])
        # A Bridge that stopped between claiming and recording: queued when a retry names it, else uncertain.
        for retry_of, state in ((None, 'uncertain'), (record['key'], 'queued')):
            with self.subTest(state=state):
                upsert_job_record(self.ctx.state_dir, record['key'], auto_retry_state='submitting')
                if retry_of:
                    upsert_job_record(self.ctx.state_dir, 'retry-job', job_id=999, retry_of=retry_of)
                self.assertEqual(self.tick(record)['auto_retry_state'], state)
        self.assertEqual(self.queued(), [])

    def test_no_retry_when_a_reply_was_kept_or_the_harness_does_not_declare_it(self):
        self.claude.harness.backend = Failing(reply=True)
        self.assertNotIn('auto_retry_state', self.failed_job('kept'))
        self.claude.harness.backend = Failing(retry=False)
        self.assertNotIn('auto_retry_state', self.failed_job('undeclared'))
        self.assertFalse(any('auto_retry_state' in r for r in list_job_records(self.ctx.state_dir, limit=0)))
        real, _ = self.bridge()  # Every Harness's own backend; only Claude declares it.
        for name in NAMES:
            with self.subTest(harness=name):
                declared = backend_capabilities(real.harnesses[name].backend, self.ctx)
                self.assertEqual((declared.rate_limit_retry, declared.incomplete_retry), (name == 'claude',) * 2)

    def test_a_turn_whose_answer_was_withdrawn_is_retried(self):
        """Claude answered, a background task woke it, then it was rate-limited or ended incomplete: the
        withdrawn answer was never authorized or sent, so the turn is retried as one with no answer."""
        for ending in (RATE_LIMITED, INCOMPLETE):
            with self.subTest(ending=ending):
                self.claude.harness.backend = Failing(withdrawn=True, ending=ending)
                record = self.failed_job('-'.join(ending))
                self.assertEqual((record['verified_reply_state'], record['result_retry_policy']),
                                 ('invalidated', 'saved_delivery_only'))
                self.assertEqual((record['status'], record['auto_retry_state']), ('failed', 'pending'))
                before = len(self.queued())
                self.assertEqual(self.tick(record)['auto_retry_state'], 'queued')
                self.assertEqual(len(self.queued()), before + 1)

    def test_a_record_already_left_with_a_withdrawn_answer_offers_check_and_continue(self):
        """Such a record from before the fix (#868, #871): its recovery card has 检查并继续, which queues it."""
        from covey.app.outbox import retry_label
        from covey.app.recovery_cards import recovery_card
        self.claude.harness.backend = Failing(withdrawn=True, retry=False)  # No automatic retry for it.
        record = self.failed_job()
        self.assertNotIn('auto_retry_state', record)
        self.assertEqual(retry_label(record), '检查并继续')
        card = json.dumps(recovery_card(record, state_dir=self.ctx.state_dir), ensure_ascii=False)
        self.assertIn('"content": "检查并继续"', card)
        self.assertNotIn('不支持重新投递', card)
        self.assertIn('本话题任务已重新入队', self.click('bridge_job_retry', record))
        [job] = self.queued()
        self.assertEqual((job.user_text, job.recovery_context['previous_status']), ('initial', 'failed'))

    def test_a_retry_whose_card_does_not_go_out_says_so(self):
        """#3560 (2026-11-10 13:46): the retry's card was refused (2200) and only "已重新入队" was said."""
        from covey.feishu.transport import Failure, set_failure
        self.claude.harness.backend = Failing(withdrawn=True, retry=False)
        record = self.failed_job()

        def refused(_ctx, job, _content):
            set_failure(Failure('api_rejected', code='2200', detail='Something went wrong'))
            job.card_missing = True  # As Cards.create_streaming_reply marks it (test_cards TaskCardResendTests).
            return None
        with mock.patch.object(self.claude.cards, 'create_streaming_reply', side_effect=refused):
            said = self.click('bridge_job_retry', record)
        self.assertIn('本话题任务已重新入队', said)
        self.assertIn('任务卡没有发出去（飞书返回 2200 Something went wrong）', said)
        [job] = self.queued()
        self.assertIsNone(job.progress)
        # With no card, its result still goes out on its own, in reply to the topic's root.
        self.claude.harness.backend = RecordingBackend()
        self.app.runtime.jobs.queue.get_nowait()
        self.claude.process_reply_job(job, self.claude.harness.context)
        self.assertEqual(find_job_record(self.ctx.state_dir, job.message_id)['status'], 'done')
        last = self.app.messenger.send_message.call_args
        self.assertEqual(last.args[2], '完成')
        self.assertEqual(last.kwargs.get('reply_to_message_id'), job.reply_to_message_id)

    def test_an_incomplete_turn_is_continued_once(self):
        self.backend.ending = INCOMPLETE
        before = time.time()
        record = self.failed_job()
        self.assertEqual((record['status'], record['auto_retry_state'], record['auto_retry_attempt'],
                          record['auto_retry_kind']), ('failed', 'pending', 1, 'incomplete'))
        self.assertGreaterEqual(record['auto_retry_at'], before + 60)
        self.assertLess(record['auto_retry_at'], time.time() + 61)
        self.assertRegex(record['status_message'], r'^本轮未全部完成，将于 \d\d:\d\d 自动检查并继续（第 1/1 次）$')
        after = self.tick(record)
        self.assertEqual((after['auto_retry_state'], after['status_message']),
                         ('queued', '本轮未全部完成，已自动检查并继续（第 1/1 次）'))
        [job] = self.queued()
        self.assertEqual((job.auto_continue, job.auto_retry, job.user_text), (1, 0, 'initial'))
        retry = find_job_record(self.ctx.state_dir, job.message_id)
        self.assertEqual((retry['auto_continue'], retry.get('auto_retry'), retry['retry_of'], retry['status_message']),
                         (1, None, record['key'], '自动检查并继续已入队（第 1/1 次）'))
        self.tick(record, after=600)
        self.assertEqual(len(self.queued()), 1)  # Never a second retry for one failure.
        # The continued turn ends incomplete again: it is not continued.
        _, again = self.run_queued(INCOMPLETE)
        self.assertNotIn('auto_retry_state', again)
        self.assertEqual(again['status_message'], INCOMPLETE_STATUS)

    def test_a_rate_limit_then_an_incomplete_turn_keep_their_own_counts(self):
        first = self.failed_job()
        self.tick(first)
        job, second = self.run_queued(INCOMPLETE)
        self.assertEqual((job.auto_retry, job.auto_continue), (1, 0))
        self.assertEqual((second['auto_retry_kind'], second['auto_retry_attempt']), ('incomplete', 1))
        self.tick(second)
        job, third = self.run_queued(RATE_LIMITED)
        self.assertEqual((job.auto_retry, job.auto_continue), (1, 1))  # The continue did not reset the count.
        self.assertEqual((third['auto_retry_kind'], third['auto_retry_attempt']), ('rate_limited', 2))
        self.assertIn('（第 2/2 次）', third['status_message'])
        self.tick(third)
        job, last = self.run_queued(INCOMPLETE)
        self.assertEqual((job.auto_retry, job.auto_continue), (2, 1))
        self.assertNotIn('auto_retry_state', last)  # Both counts are used up: at most three in a chain.
        self.assertEqual(self.queued(), [])

    def test_an_incomplete_turn_then_a_rate_limit_keep_their_own_counts(self):
        self.backend.ending = INCOMPLETE
        self.tick(self.failed_job())
        job, second = self.run_queued(RATE_LIMITED)
        self.assertEqual((job.auto_retry, job.auto_continue), (0, 1))
        self.assertEqual((second['auto_retry_kind'], second['auto_retry_attempt']), ('rate_limited', 1))
        self.assertIn('（第 1/2 次）', second['status_message'])
        self.tick(second)
        job, third = self.run_queued(INCOMPLETE)
        self.assertEqual((job.auto_retry, job.auto_continue), (1, 1))
        self.assertNotIn('auto_retry_state', third)
        self.assertEqual(third['status_message'], INCOMPLETE_STATUS)

    def test_stopping_an_incomplete_turns_continue(self):
        self.backend.ending = INCOMPLETE
        record = self.failed_job()
        self.assertIn(f"已停止任务 #{record['job_id']} 的自动重试", self.click(auto_retry.CANCEL_KIND, record))
        after = find_job_record(self.ctx.state_dir, record['key'])
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note'], after['status_message']),
                         ('cancelled', 'stopped', INCOMPLETE_STATUS))
        self.assertEqual(self.claude.cards.update_record_elements.call_args.args[2][0]['content'],
                         auto_retry.CANCELLED['stopped'])
        self.tick(record)
        self.assertEqual(self.queued(), [])

    def test_a_restarted_bridge_continues_an_incomplete_turn(self):
        self.backend.ending = INCOMPLETE
        record = self.failed_job()
        restarted, _ = self.bridge()
        restarted.messenger.send_message = mock.Mock(return_value=True)
        for app in restarted.apps.values():
            app.cards.create_streaming_reply = mock.Mock(return_value=None)
        # The restarted Bridge has the real Claude backend: it declares the continue.
        self.assertEqual(self.tick(record, bridge=restarted)['auto_retry_state'], 'queued')
        [job] = list(restarted.runtime.jobs.queue.queue)
        self.assertEqual((job.auto_retry, job.auto_continue), (0, 1))

    def test_no_continue_under_a_task_timeout_with_a_kept_reply_or_on_other_failures(self):
        self.backend.ending = INCOMPLETE
        self.assertNotIn('auto_retry_state', self.failed_job('timeout', timeout=600))  # It may be what ended it.
        self.claude.harness.backend = Failing(reply=True, ending=INCOMPLETE)
        self.assertNotIn('auto_retry_state', self.failed_job('kept'))  # Partly done: its reply was sent.
        self.claude.harness.backend = Failing(retry=False, ending=INCOMPLETE)
        self.assertNotIn('auto_retry_state', self.failed_job('undeclared'))
        for ending in (('failed', 'root_failed'), ('timed_out', 'timeout'), ('failed', 'authentication_error')):
            with self.subTest(ending=ending):
                self.claude.harness.backend = Failing(ending=ending)
                self.assertNotIn('auto_retry_state', self.failed_job('-'.join(ending)))


    def interrupted(self, mid='task', harness='claude', **fields):
        """A ``harness`` task in its own topic that was running when its Bridge stopped, as a restart leaves it
        (2026-11-02 22:09): its record once the next Bridge's start marked it interrupted."""
        self.receive(mid, 'initial')
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, 'chat', harness)
        app, self.topic_ctx = self.route(mid)
        app.submission.start_workspace(self.topic_ctx, 'chat', self.topic_ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        app.record_job_state(self.topic_ctx, job, 'running', '任务执行中', **fields)
        with mock.patch.object(type(app.controls), 'show_recovery_card', return_value=True) as shown:
            app.notify_stale_jobs(app.harness.context)
        self.shown = shown.call_args.args[1] if shown.called else None
        return find_job_record(self.ctx.state_dir, job.message_id)

    def test_a_restart_continues_an_interrupted_task_once_by_itself(self):
        # T331 #2407 was never continued after the 22:09 restart (owner's choice, T314: 检查并继续 once by itself).
        before = time.time()
        record = self.interrupted()
        self.assertEqual((record['status'], record['interrupted_by'], record['auto_retry_state'], record['auto_retry_kind'],
                          record['auto_retry_attempt']), ('interrupted', 'restart', 'pending', 'restarted', 1))
        self.assertGreaterEqual(record['auto_retry_at'], before + 60)
        self.assertRegex(record['status_message'], r'^本轮被重启打断，将于 \d\d:\d\d 自动检查并继续（第 1/1 次）$')
        self.assertEqual(self.shown['auto_retry_state'], 'pending')  # Its recovery card says when, with 停止自动重试.
        self.assertEqual(self.tick(record, after=-1)['auto_retry_state'], 'pending')
        after = self.tick(record)
        self.assertEqual((after['auto_retry_state'], after['status_message']),
                         ('queued', '本轮被重启打断，已自动检查并继续（第 1/1 次）'))
        [job] = self.queued()
        self.assertEqual((job.auto_resume, job.auto_retry, job.auto_continue, job.recovery_context['previous_status']),
                         (1, 0, 0, 'interrupted'))
        retry = find_job_record(self.ctx.state_dir, job.message_id)
        self.assertEqual((retry['auto_resume'], retry['retry_of']), (1, record['key']))
        # The continued task, interrupted by the next restart, waits for the user: a restart it causes cannot loop.
        self.app.runtime.unregister_reply_job(self.app.runtime.jobs.queue.get_nowait())
        upsert_job_record(self.ctx.state_dir, retry['key'], status='running')
        self.claude.notify_stale_jobs(self.claude.harness.context)
        again = find_job_record(self.ctx.state_dir, retry['key'])
        self.assertEqual((again['status'], again['interrupted_by']), ('interrupted', 'restart'))
        self.assertNotIn('auto_retry_state', again)

    def test_a_codex_task_that_wrote_a_message_and_went_on_continues_too(self):
        # T272 #2630 and #2633 (Codex, 2026-11-03): each message it wrote and worked past is a withdrawn
        # candidate; the restarts left them with neither the automatic continue nor 检查并继续.
        withdrawn = dict(result_retry_policy='saved_delivery_only', verified_reply_state='invalidated',
                         verified_reply_authorized=False)
        record = self.interrupted(harness='codex', **withdrawn)
        self.assertEqual((record['backend'], record['auto_retry_state'], record['auto_retry_kind']),
                         ('codex', 'pending', 'restarted'))
        self.assertEqual(self.tick(record)['auto_retry_state'], 'queued')
        [job] = self.queued()
        self.assertEqual((job.auto_resume, job.recovery_context['previous_status']), (1, 'interrupted'))
        self.app.runtime.unregister_reply_job(self.app.runtime.jobs.queue.get_nowait())
        # One that paused on a native question waits for its answer, as before.
        paused = self.interrupted('paused', harness='codex', native_question_deferred=True, **withdrawn)
        self.assertNotIn('auto_retry_state', paused)

    def test_a_supplement_taken_into_the_task_does_not_cancel_its_continue(self):
        # #2614, #2619, #2621 and #2634 (2026-11-03): a 补充 the task took before the restart has a later job
        # number in the topic, and read as a new message.
        record = self.interrupted()
        upsert_job_record(self.ctx.state_dir, 'supplement', job_id=record['job_id'] + 1, topic_id=record['topic_id'],
                          chat_id=record['chat_id'], status='cancelled', merged_into_job_id=record['job_id'])
        self.assertEqual(self.tick(record)['auto_retry_state'], 'queued')
        self.app.runtime.unregister_reply_job(self.app.runtime.jobs.queue.get_nowait())
        other = self.interrupted('other')
        upsert_job_record(self.ctx.state_dir, 'next', job_id=other['job_id'] + 1, topic_id=other['topic_id'],
                          chat_id=other['chat_id'], status='done')
        after = self.tick(other)
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note']), ('cancelled', 'new_message'))

    def test_a_paused_native_question_marks_its_record_when_saved(self):
        # Before the pause withdraws the candidate, so a runner that dies in between leaves no rerunnable record.
        from covey.core.jobs import BridgeReplyJob
        codex = self.app.apps['codex']
        upsert_job_record(self.ctx.state_dir, 'om-ask', job_id=77, status='running', backend='codex')
        job = BridgeReplyJob(job_id=77, user_text='q', chat_id='chat', message_id='om-ask', timeout=0, topic_id='t')
        job.bridge_run_id = 'run-1'
        rpc = SimpleNamespace(native_blocking_questions=False, reply_state=SimpleNamespace(generation=2),
                              turn_id='turn', thread_id='sid', reject_question=mock.Mock())
        registered = codex.questions.native.register(
            codex.harness.context, job, {'request_id': 'r1', 'isBlocking': True,
                                         'questions': [{'id': 'a', 'question': '选哪个？'}]}, rpc, defer_question=False)
        self.assertTrue(registered[2])  # pause_question
        self.assertIs(find_job_record(self.ctx.state_dir, 'om-ask')['native_question_deferred'], True)

    def test_no_continue_after_a_stop_click_or_with_a_saved_reply_to_deliver(self):
        stopped = self.interrupted('stopped', cancel_requested_by='ou_owner')
        self.assertEqual(stopped['status'], 'interrupted')
        self.assertNotIn('auto_retry_state', stopped)
        saved = self.interrupted('saved', result_retry_policy='saved_delivery_only', verified_reply_state='authorized',
                                 verified_reply_authorized=True, delivery_ok=False, final_text='已完成的回答')
        self.assertNotIn('auto_retry_state', saved)  # 重新投递结果 is the user's to click.

    def test_stopping_a_restart_continue_clears_the_cards_preview(self):
        record = self.interrupted()
        self.claude.cards.set_record_summary = mock.Mock(return_value=True)
        self.assertIn(f"已停止任务 #{record['job_id']} 的自动重试", self.click(auto_retry.CANCEL_KIND, record))
        after = find_job_record(self.ctx.state_dir, record['key'])
        self.assertEqual((after['auto_retry_state'], after['auto_retry_note']), ('cancelled', 'stopped'))
        summary = self.claude.cards.set_record_summary.call_args.args[2]
        self.assertRegex(summary, r'^⚠️ (T\d+|#\d+) · 已中断 · 待检查并继续$')
        self.tick(record)
        self.assertEqual(self.queued(), [])


class AutoRetryCardTest(unittest.TestCase):
    def test_a_pending_retry_shows_its_time_and_a_stop_button(self):
        due = time.time() + 60
        elements = auto_retry.result_elements('topic', 7, {'auto_retry_state': 'pending', 'auto_retry_at': due,
                                                            'auto_retry_attempt': 1})
        self.assertEqual([e['element_id'] for e in elements], ['auto_retry_status', 'result_actions', 'result_actions_note'])
        self.assertIn(auto_retry.status_line(due, 1), elements[0]['content'])
        kinds = [c['elements'][0]['value']['kind'] for c in elements[1]['columns']]
        self.assertEqual(kinds, ['bridge_job_retry', auto_retry.CANCEL_KIND])
        # Without a pending retry the card is as before: the button and its note.
        plain = auto_retry.result_elements('topic', 7, {})
        self.assertEqual([e['element_id'] for e in plain], ['result_actions', 'result_actions_note'])
        self.assertEqual(len(plain[0]['columns']), 1)
        # An incomplete turn's: the status says what the retry does.
        record = {'auto_retry_state': 'pending', 'auto_retry_at': due, 'auto_retry_attempt': 1,
                  'auto_retry_kind': 'incomplete'}
        status = auto_retry.result_elements('topic', 7, record)[0]['content']
        self.assertEqual(status, f"⏳ {auto_retry.status_line(due, 1, 'incomplete')}。现在点“检查并继续”会立即重试；"
                                 "点“停止自动重试”、在本话题发消息或换配置会取消这次自动重试。")
        self.assertIn('自动检查并继续（第 1/1 次）', status)
        # The stop click is the Bridge's: never forwarded to a task runner.
        self.assertIn(auto_retry.CANCEL_KIND, ACTIONS)
        self.assertIn(auto_retry.CANCEL_KIND, BOT_ONLY)

    def test_the_moved_card_says_which_automatic_retry(self):
        from covey.app.recovery_cards import recovery_card
        record = {'job_id': 7, 'topic_id': 'topic'}
        for kind, text in (('', '已转到任务 #8 继续'), ('rate_limited', '已转到任务 #8 继续（限流后自动重试）'),
                           ('incomplete', '已转到任务 #8 继续（未全部完成后自动检查并继续）')):
            with self.subTest(kind=kind):
                header = recovery_card(record, moved_to=8, automatic=kind)['header']
                self.assertEqual((header['template'], header['title']['content']), ('grey', '#7 · ' + text))

    def test_an_interrupted_card_says_when_it_continues_and_previews_it(self):
        from covey.app.recovery_cards import recovery_card
        due = time.time() + 60
        at = time.strftime('%H:%M', time.localtime(due))
        record = {'job_id': 7, 'topic_id': 'topic', 'status': 'interrupted', 'interrupted_by': 'restart', 'kind': 'chat'}
        plain = recovery_card(record)
        self.assertEqual(plain['config']['summary']['content'], '⚠️ #7 · 已中断 · 待检查并继续')
        self.assertIn('recovery_actions', json.dumps(plain))
        pending = recovery_card({**record, 'auto_retry_state': 'pending', 'auto_retry_at': due, 'auto_retry_attempt': 1,
                                 'auto_retry_kind': 'restarted'})
        self.assertEqual(pending['config']['summary']['content'], f'⏳ #7 · 已中断 · {at} 自动检查并继续')
        ids = [e.get('element_id') for e in pending['body']['elements']]
        self.assertEqual(ids[1:4], ['auto_retry_status', 'result_actions', 'result_actions_note'])  # What show repaints.
        self.assertNotIn('recovery_actions', ids)
        self.assertIn(f'本轮被重启打断，将于 {at} 自动检查并继续（第 1/1 次）', pending['body']['elements'][1]['content'])
        self.assertEqual(json.dumps(pending, ensure_ascii=False).count('检查并继续：新开一个任务'), 1)  # The note once.
        moved = recovery_card(record, moved_to=8, automatic='restarted')
        self.assertEqual((moved['header']['title']['content'], moved['config']['summary']['content']),
                         ('#7 · 已转到任务 #8 继续（重启后自动检查并继续）', '#7 · 已转到 #8 继续'))

    def test_the_failed_card_gets_the_retry_controls(self):
        from support.world import build_claude_app
        app = build_claude_app()
        job = SimpleNamespace(message_id='m', topic_id='topic', job_id=7,
                              progress=SimpleNamespace(card_id='card', element_id='answer', sequence=1))
        record = {'auto_retry_state': 'pending', 'auto_retry_at': time.time() + 60, 'auto_retry_attempt': 1}
        app.messenger.write_card = mock.Mock(return_value={'ok': True})
        with mock.patch('covey.app.cards.find_job_record', return_value=record), \
                mock.patch('covey.app.auto_retry.retry_label', return_value='检查并继续'):
            app.cards.finish_result_controls(SimpleNamespace(state_dir=None, profile='p'), job, 'failed')
        body = app.messenger.write_card.call_args.args[4]
        self.assertEqual(json.loads(body['elements']), auto_retry.result_elements('topic', 7, record))


if __name__ == '__main__':
    unittest.main()
