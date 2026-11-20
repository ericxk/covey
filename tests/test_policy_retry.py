"""One retry on an older model after an official safety refusal of cyber or bio (app/policy_retry.py), on the live
Bridge's apps: both stop paths, the retry job, and the cards' words."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from support.fake_backends import RecordingBackend
from support.world import UnifiedCase

from covey.app import policy_guard, runner_ipc
from covey.backends.claude import argv as claude_argv, subagents
from covey.app.unified import OUTBOX_STEPS
from covey.app.turn_delivery import TurnDelivery
try:
    from covey.app import policy_retry
except ImportError:  # A release before it: the behaviour the assertions check is what fails.
    policy_retry = None
from covey.core import butler, run_dir
from covey.core.harness import RunResult
from covey.core.jobs import context_for_job, find_job_record, upsert_job_record
from covey.core.model_policy import POLICY_RECOVERY, retry_recovery
from covey.core.result import ExecutionOutcome
from covey.core.state import write_json
from covey.core.workspaces import read_topic


def event(category='cyber', kind='refusal'):
    return {'kind': kind, 'category': category, 'source_model': '', 'target_model': '', 'scope': 'main',
            'child_id': '', 'response_id': '', 'evidence': 'refusal_stop' if kind == 'refusal' else 'fallback_block'}


class Refusing(RecordingBackend):
    """Stops every turn on an official refusal of ``category`` seen in its stream, as Claude's runtime does;
    ``category`` None ends the turn as a plain failure."""

    def __init__(self, category='cyber', kind='refusal'):
        super().__init__()
        self.category, self.kind = category, kind

    def run(self, request):
        self.calls.append((request.context, request.prompt, request.session_id))
        done = dict(identity_confirmed=True, cleanup_confirmed=True, protection_confirmed=True)
        if self.category is None:
            outcome = ExecutionOutcome('failed', 'process_exit', **done)
        else:
            outcome = ExecutionOutcome('cancelled', 'policy_' + self.kind, policy_event=event(self.category, self.kind),
                                       **done)
        # A new session each turn: a refused one is blocked for good.
        return RunResult(outcome.failure_reply(), str(uuid.uuid4()), True, None, outcome)


class PolicyRetryTest(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.claude = self.app.apps['claude']
        self.backend = self.claude.harness.backend = Refusing()
        self.claude.cards.replace_record_card = mock.Mock(return_value=True)
        self.claude.cards.set_record_activity = mock.Mock(return_value=True)
        self.claude.cards.update_record_elements = mock.Mock(return_value=True)

    def stopped(self, mid='task', category='cyber', kind='refusal'):
        """A Claude task in its own topic stopped by a refusal of ``category``: its record."""
        self.backend.category, self.backend.kind = category, kind
        self.receive(mid, 'scan my own server for open ports')
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, 'chat', 'claude')
        app, ctx = self.route(mid)
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        app.process_reply_job(job, app.harness.context)
        self.topic_ctx = ctx
        return find_job_record(self.ctx.state_dir, job.message_id)

    def tick(self, record):
        for step in OUTBOX_STEPS:  # As the Bridge's outbox loop runs it.
            step(self.app, self.ctx)
        return find_job_record(self.ctx.state_dir, record['key'])

    def queued(self):
        return list(self.app.runtime.jobs.queue.queue)

    def run_queued(self, category):
        job = self.app.runtime.jobs.queue.get_nowait()
        self.backend.category = category
        self.claude.process_reply_job(job, self.claude.harness.context)
        return job, find_job_record(self.ctx.state_dir, job.message_id)

    def test_it_runs_in_the_outbox_loop(self):
        self.assertIn(policy_retry.tick, OUTBOX_STEPS)

    def test_a_cyber_refusal_is_retried_once_on_opus_4_8(self):
        record = self.stopped()
        topic_model = read_topic(self.topic_ctx).get('model')
        self.assertEqual((record['status'], record.get('policy_retry_state'), record.get('policy_retry_model')),
                         ('cancelled', 'pending', 'claude-opus-4-8'))
        self.assertIn('将因安全拒绝（cyber）换 Opus 4.8 在新会话里自动重试一次本轮请求', record['final_text'])
        self.assertNotIn('不会自动重试', record['final_text'])
        self.assertTrue(record['status_message'].endswith('将换 Opus 4.8 重试。'))
        self.assertEqual(self.queued(), [])  # Nothing until the outbox loop.
        after = self.tick(record)
        self.assertEqual(after['policy_retry_state'], 'queued')
        [job] = self.queued()
        self.assertEqual((job.model, job.user_text, job.recovery_context), ('claude-opus-4-8', record['user_text'], None))
        self.assertEqual(job.policy_retry, {'of': record['key'], 'category': 'cyber', 'model': 'claude-opus-4-8'})
        self.assertEqual(context_for_job(self.claude.harness.context, job).options['subagent_model'], 'claude-opus-4-8')
        retry = find_job_record(self.ctx.state_dir, job.message_id)
        self.assertEqual((retry['status_message'], retry['retry_of'], retry['model']),
                         ('已因安全拒绝（cyber）换 Opus 4.8 重试', record['key'], 'claude-opus-4-8'))
        self.assertEqual(after['retried_by'], job.message_id)
        moved = json.dumps(self.claude.cards.replace_record_card.call_args.args[2], ensure_ascii=False)
        self.assertIn(f"已转到任务 &#35;{job.job_id} 继续（已因安全拒绝（cyber）换 Opus 4&#46;8 重试）", moved)
        self.assertEqual(read_topic(self.topic_ctx).get('model'), topic_model)  # The binding stays.
        self.tick(record)
        self.assertEqual(len(self.queued()), 1)
        # The retry starts a new session, without the refused turn in its recent conversation.
        _, retried = self.run_queued(None)
        context, prompt, session = self.backend.calls[-1]
        self.assertIsNone(session)
        self.assertEqual(context.model, 'claude-opus-4-8')
        self.assertNotIn('换 Opus 4.8 在新会话里', prompt)
        self.assertEqual(prompt.count('scan my own server for open ports'), 1)

    def test_the_retrys_input_box_names_the_older_model(self):
        self.tick(self.stopped())
        [job] = self.queued()
        self.claude.cards.card_facts(context_for_job(self.claude.harness.context, job), job)
        # The fake backend lists no models: the box names the id (the live catalog's Opus 4.8).
        self.assertEqual(job.input_parts[:3], ['Claude Code', 'Claude 自带', 'claude-opus-4-8'])

    def test_a_bio_refusal_is_retried_on_opus_5(self):
        record = self.stopped(category='bio')
        self.assertEqual(record['policy_retry_model'], 'claude-opus-5')
        self.tick(record)
        [job] = self.queued()
        self.assertEqual(job.model, 'claude-opus-5')
        self.assertIn('已因安全拒绝（bio）换 Opus 5 重试',
                      find_job_record(self.ctx.state_dir, job.message_id)['status_message'])

    def test_other_categories_and_fallbacks_only_stop(self):
        for mid, category, kind in (('frontier', 'frontier_llm', 'refusal'), ('fallback', 'cyber', 'fallback'),
                                    ('unspecified', 'unspecified', 'refusal')):
            with self.subTest(category=category, kind=kind):
                record = self.stopped(mid, category, kind)
                self.assertEqual(record['status'], 'cancelled')
                self.assertNotIn('policy_retry_state', record)
                self.assertIn('不会自动重试', record['final_text'])
                self.tick(record)
                self.assertEqual(self.queued(), [])

    def test_a_refused_retry_is_not_retried_again(self):
        for first, again in (('cyber', 'bio'), ('bio', 'cyber'), ('cyber', 'cyber')):
            with self.subTest(first=first, again=again):
                record = self.stopped(f'{first}-{again}', first)
                self.tick(record)
                job, retried = self.run_queued(again)
                self.assertEqual(retried['status'], 'cancelled')
                self.assertNotIn('policy_retry_state', retried)
                self.assertIn('不会自动重试', retried['final_text'])
                self.tick(retried)
                self.assertEqual(self.queued(), [])
                # Nor through the policy guard's path.
                self.assertIsNone(policy_retry.schedule(self.ctx.state_dir, retried['key'], event(again)))

    def test_duplicate_stop_events_schedule_one_retry(self):
        record = self.stopped()
        for _ in range(3):
            self.assertEqual(policy_retry.schedule(self.ctx.state_dir, record['key'], event()),
                             ('claude-opus-4-8', 'Opus 4.8'))
        # The policy guard's evidence of the same stop, after the worker's.
        intent = policy_guard.intent_path(self.ctx.state_dir, record['key'])
        write_json(intent, {'key': record['key'], 'job_id': record['job_id'], 'phase': 'stopped', 'notice': 'sent',
                            'attempts': 1, 'next_attempt': 0, 'card_updated': True, 'session_blocked': True,
                            'questions_closed': True, 'observation': {'id': 'audit:1', 'event': event()}})
        gateway = SimpleNamespace(apps=self.app.apps, runtime=self.app.runtime)
        policy_guard.finish(gateway, self.ctx, intent)
        policy_guard.finish(gateway, self.ctx, intent)
        self.tick(record)
        self.tick(record)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(find_job_record(self.ctx.state_dir, record['key'])['policy_retry_state'], 'queued')

    def test_the_policy_guards_stop_retries_too(self):
        # The runner saw nothing: the guard stops it from the policy feed's evidence while it runs.
        base = self.stopped('earlier', category=None)
        record = upsert_job_record(self.ctx.state_dir, 'guarded', **{
            **{k: v for k, v in base.items() if k not in ('key', 'revision', 'schema_version')},
            'message_id': 'guarded', 'job_id': base['job_id'] + 1, 'status': 'running', 'status_message': '执行中',
            'final_text': '', 'execution_status': ''})
        intent = policy_guard.intent_path(self.ctx.state_dir, record['key'])
        write_json(intent, {'key': record['key'], 'job_id': record['job_id'], 'phase': 'stopped', 'notice': 'pending',
                            'attempts': 0, 'next_attempt': 0, 'card_updated': False, 'fallback_retry': True,
                            'observation': {'id': 'audit:2', 'event': event('bio')}})
        gateway = SimpleNamespace(apps=self.app.apps, runtime=self.app.runtime)
        policy_guard.finish(gateway, self.ctx, intent)
        after = find_job_record(self.ctx.state_dir, record['key'])
        self.assertEqual((after['status'], after['policy_retry_state']), ('cancelled', 'pending'))
        self.assertIn('换 Opus 5 在新会话里自动重试一次', after['final_text'])
        self.assertEqual(after['status_message'], '官方拒绝／降级，任务已停止；将换 Opus 5 重试。')
        notice = self.app.messenger.send_message.call_args_list[-1]
        self.assertIn('换 Opus 5 在新会话里自动重试一次', json.dumps(notice.args, ensure_ascii=False, default=str))
        self.tick(record)
        [job] = self.queued()
        self.assertEqual(job.model, 'claude-opus-5')

    def guarded(self, mid, *, phase='stopped', notice='pending', marker=True, category='cyber'):
        """A running Claude task the policy guard claimed (``marker``: by this release) and stopped on a refusal of
        ``category``: (its record, its intent's path, the guard's gateway)."""
        base = self.stopped(mid + '-earlier', category=None)
        record = upsert_job_record(self.ctx.state_dir, mid, **{
            **{k: v for k, v in base.items() if k not in ('key', 'revision', 'schema_version')},
            'message_id': mid, 'job_id': base['job_id'] + 1, 'status': 'running', 'status_message': '执行中',
            'final_text': '', 'execution_status': '', 'policy_guard_requested': True})
        intent = policy_guard.intent_path(self.ctx.state_dir, record['key'])
        write_json(intent, {'key': record['key'], 'job_id': record['job_id'], 'phase': phase, 'notice': notice,
                            'attempts': 0, 'next_attempt': 0, 'card_updated': False,
                            'observation': {'id': 'audit:' + mid, 'event': event(category)},
                            **({'fallback_retry': True} if marker else {})})
        return record, intent, SimpleNamespace(apps=self.app.apps, runtime=self.app.runtime)

    def test_the_retrys_claude_runs_its_subagents_on_the_older_model(self):
        self.tick(self.stopped())
        self.run_queued(None)
        (refused, *_), (retried, *_) = self.backend.calls
        light = {subagents.MODEL_ENV: subagents.LIGHT}  # As the Bridge's own environment might have it.
        self.assertEqual(claude_argv.turn_env(refused, {})[subagents.MODEL_ENV], subagents.LIGHT)
        self.assertEqual(claude_argv.turn_env(retried, light)[subagents.MODEL_ENV], 'claude-opus-4-8')
        # No Haiku Explore in the retry: the built-in one follows the model above; the refused turn had it.
        wrapper = SimpleNamespace(wrap_argv=lambda argv, ctx: argv)
        self.assertIn('--agents', claude_argv.stream_argv(wrapper, refused))
        self.assertNotIn('--agents', claude_argv.stream_argv(wrapper, retried))

    def test_a_cancelled_retry_of_the_guards_stop_takes_its_promise_back(self):
        # Its notice failed for now; the topic moves on before the retry runs.
        self.app.messenger.send_message.return_value = False
        self.claude.controls.repaint_recovery_card = mock.Mock(return_value=True)
        record, intent, gateway = self.guarded('promised')
        with mock.patch.object(policy_guard, 'last_failure', return_value=SimpleNamespace(retryable=True,
                                                                                          retry_after=0)):
            policy_guard.finish(gateway, self.ctx, intent)
        first_text, first_key = self.app.messenger.send_message.call_args.args[2:4]
        self.assertIn('将因安全拒绝', first_text)
        before = find_job_record(self.ctx.state_dir, record['key'])
        self.assertEqual((before['policy_retry_state'], before['policy_notice_state']), ('pending', 'pending'))
        self.receive('newer', 'another request', root='promised-earlier')
        after = self.tick(record)
        self.assertEqual((after['policy_retry_state'], after['policy_retry_note']), ('cancelled', 'new_message'))
        self.assertTrue(after['status_message'].endswith('没有换模型重试：本话题有了新任务。'), after['status_message'])
        self.assertNotIn('将换', after['status_message'])
        self.assertTrue(after['final_text'].endswith(butler.worded(POLICY_RECOVERY, after)), after['final_text'])
        # Its recovery card is drawn again from the record, without the promise in its body.
        repainted = self.claude.controls.repaint_recovery_card.call_args.args[1]
        self.assertEqual((repainted['key'], repainted['final_text']), (record['key'], after['final_text']))
        # The guard's pin keeps them against a late write, and its next notice says the same.
        late = upsert_job_record(self.ctx.state_dir, record['key'], status_message='执行中', final_text='late')
        self.assertEqual((late['status_message'], late['final_text']), (after['status_message'], after['final_text']))
        self.app.messenger.send_message.return_value = True
        write_json(intent, {**json.loads(Path(intent).read_text()), 'next_attempt': 0})
        policy_guard.finish(gateway, self.ctx, intent)
        notice = json.dumps(self.app.messenger.send_message.call_args.args, ensure_ascii=False, default=str)
        self.assertIn('不会自动重试', notice)
        self.assertNotIn('将因安全拒绝', notice)
        # The first text surely did not land: the new one goes under a key of its own (a key never carries two
        # texts; the transport would refuse it as idempotency_conflict and the notice would be lost).
        self.assertNotEqual(self.app.messenger.send_message.call_args.args[3], first_key)
        self.assertEqual(json.loads(Path(intent).read_text())['notice'], 'sent')
        final = find_job_record(self.ctx.state_dir, record['key'])
        self.assertEqual((final['status_message'], final['final_text'], final['policy_retry_state']),
                         (after['status_message'], after['final_text'], 'cancelled'))

    def test_a_notice_that_may_have_landed_is_sent_again_as_it_was(self):
        # Its first notice may have landed (an ambiguous failure); the retry is then taken back.
        self.app.messenger.send_message.return_value = False
        self.claude.controls.repaint_recovery_card = mock.Mock(return_value=True)
        record, intent, gateway = self.guarded('maybe')
        with mock.patch.object(policy_guard, 'last_failure', return_value=SimpleNamespace(
                retryable=True, ambiguous=True, retry_after=0)):
            policy_guard.finish(gateway, self.ctx, intent)
        first = self.app.messenger.send_message.call_args.args[2:4]
        self.receive('later', 'another request', root='maybe-earlier')
        self.assertEqual(self.tick(record)['policy_retry_state'], 'cancelled')
        self.app.messenger.send_message.return_value = True
        write_json(intent, {**json.loads(Path(intent).read_text()), 'next_attempt': 0})
        policy_guard.finish(gateway, self.ctx, intent)
        # Only the same text under the same key, which the transport reconciles: never a second notice.
        self.assertEqual(self.app.messenger.send_message.call_args.args[2:4], first)

    def test_a_stop_recorded_before_this_release_is_never_retried(self):
        # An older release claimed and stopped it; its notice is paused, so the guard's tick finishes it again.
        record, intent, gateway = self.guarded('older', notice='paused', marker=False)
        policy_guard.tick(gateway, self.ctx)
        policy_guard.tick(gateway, self.ctx)
        after = self.tick(record)
        self.assertNotIn('policy_retry_state', after)
        self.assertIn('不会自动重试', after['final_text'])
        self.assertNotIn('将换', after['status_message'])
        self.assertEqual(self.queued(), [])

    def test_this_releases_claim_marks_its_intent(self):
        record = upsert_job_record(self.ctx.state_dir, 'claimed', job_id=900, message_id='claimed', backend='claude',
                                   status='running')
        with mock.patch.object(policy_guard, 'runner_info', return_value={'key': 'claimed', 'job_id': 900}), \
                mock.patch.object(policy_guard, 'runner_alive', return_value=True):
            self.assertEqual(policy_guard.claim(self.ctx.state_dir, {'id': 'audit:c', 'event': event()}, record),
                             'stop_requested')
        self.assertTrue(json.loads(policy_guard.intent_path(self.ctx.state_dir, 'claimed').read_text())['fallback_retry'])

    def test_no_retry_while_the_guard_reports_unconfirmed_cleanup(self):
        # The task runner saw its own tree end and scheduled the retry; the guard, which claimed it too, could not
        # confirm the cleanup of the tree it stopped.
        record = self.stopped('unconfirmed')
        self.assertEqual(record['policy_retry_state'], 'pending')
        upsert_job_record(self.ctx.state_dir, record['key'], policy_guard_requested=True)
        intent = policy_guard.intent_path(self.ctx.state_dir, record['key'])
        write_json(intent, {'key': record['key'], 'job_id': record['job_id'], 'phase': 'cleanup_unconfirmed',
                            'notice': 'pending', 'attempts': 0, 'next_attempt': 0, 'card_updated': False,
                            'fallback_retry': True, 'observation': {'id': 'audit:u', 'event': event()}})
        self.assertEqual(self.tick(record)['policy_retry_state'], 'pending')  # The guard has not finished yet.
        policy_guard.finish(SimpleNamespace(apps=self.app.apps, runtime=self.app.runtime), self.ctx, intent)
        self.assertEqual(find_job_record(self.ctx.state_dir, record['key'])['policy_guard_cleanup'], 'unconfirmed')
        after = self.tick(record)
        self.assertEqual((after['policy_retry_state'], after['policy_retry_note']), ('cancelled', 'cleanup'))
        self.assertEqual(self.claude.cards.set_record_activity.call_args.args[2], '没有换模型重试：本轮的进程清理待确认。')
        self.assertEqual(self.queued(), [])

    def test_no_retry_after_the_owners_stop_or_when_the_topic_moved_on(self):
        record = self.stopped()
        self.assertIsNone(policy_retry.eligible({**record, 'policy_retry_state': None, 'cancel_requested_by': 'card'},
                                                event()))
        self.receive('next', 'another request', root='task')
        after = self.tick(record)
        self.assertEqual((after['policy_retry_state'], after['policy_retry_note']), ('cancelled', 'new_message'))
        self.assertEqual(self.claude.cards.set_record_activity.call_args.args[2], '没有换模型重试：本话题有了新任务。')
        # Status cards, 手上的事 and Pin read the status line; a 重新投递 sends the text: no retry promised.
        self.assertTrue(after['status_message'].endswith('没有换模型重试：本话题有了新任务。'), after['status_message'])
        self.assertNotIn('将换', after['status_message'])
        self.assertIn('不会自动重试', after['final_text'])
        self.assertNotIn('将因安全拒绝', after['final_text'])
        self.assertTrue(all(not job.policy_retry for job in self.queued()))

    def test_a_continuation_of_the_retry_keeps_its_mark_and_never_retries(self):
        # The retry hits a rate limit; its automatic retry is refused for the other category: no second retry.
        self.backend.declare(rate_limit_retry=True)
        record = self.stopped('chain', 'cyber')
        self.tick(record)
        [job] = self.queued()
        self.backend.run = mock.Mock(side_effect=lambda request: RunResult(
            'limited', str(uuid.uuid4()), True, None, ExecutionOutcome(
                'failed', 'rate_limited', identity_confirmed=True, cleanup_confirmed=True, protection_confirmed=True)))
        self.app.runtime.jobs.queue.get_nowait()
        self.claude.process_reply_job(job, self.claude.harness.context)
        limited = find_job_record(self.ctx.state_dir, job.message_id)
        self.assertEqual(limited.get('auto_retry_state'), 'pending')
        upsert_job_record(self.ctx.state_dir, limited['key'], auto_retry_at=0)
        self.tick(limited)
        [again] = self.queued()
        self.assertEqual((again.model, again.policy_retry), ('claude-opus-4-8', job.policy_retry))
        self.assertEqual(context_for_job(self.claude.harness.context, again).options['subagent_model'],
                         'claude-opus-4-8')
        del self.backend.run
        _, refused = self.run_queued('bio')
        self.assertEqual(refused['status'], 'cancelled')
        self.assertNotIn('policy_retry_state', refused)
        self.assertIn('不会自动重试', refused['final_text'])
        self.tick(refused)
        self.assertEqual(self.queued(), [])
        # 检查并继续 on it carries the mark too.
        self.claude.controls.requeue(self.topic_ctx, refused['chat_id'], 'by-hand', refused)
        [by_hand] = self.queued()
        self.assertEqual((by_hand.model, by_hand.policy_retry), ('claude-opus-4-8', job.policy_retry))

    def test_the_retry_waits_for_the_guards_stop_to_finish(self):
        # The guard claimed the task; its finish has not blocked the session, repainted the card, sent the notice.
        record = self.stopped('guard-order')
        upsert_job_record(self.ctx.state_dir, record['key'], policy_guard_requested=True)
        self.assertEqual(self.tick(record)['policy_retry_state'], 'pending')
        self.assertEqual(self.queued(), [])
        # Its notice went, its card's repaint has not (yet): the retry's card would come first.
        upsert_job_record(self.ctx.state_dir, record['key'], policy_notice_state='sent', policy_notice_at=time.time())
        self.assertEqual(self.tick(record)['policy_retry_state'], 'pending')
        upsert_job_record(self.ctx.state_dir, record['key'], policy_card_updated=True)
        self.assertEqual(self.tick(record)['policy_retry_state'], 'queued')
        self.assertEqual(len(self.queued()), 1)

    def test_the_guards_later_repaint_keeps_the_retrys_header(self):
        # The guard's first repaint fails (CardKit), its notice goes. The retry waits for the repaint, at most
        # CARD_WAIT, then moves the card; the guard's next round does not draw it back from the record.
        self.claude.cards.replace_record_card.side_effect = [False, True, True]
        record, intent, gateway = self.guarded('repaint')
        upsert_job_record(self.ctx.state_dir, record['key'], card_id='card-repaint')
        policy_guard.finish(gateway, self.ctx, intent)
        self.assertEqual((self.tick(record)['policy_retry_state'], self.queued()), ('pending', []))
        upsert_job_record(self.ctx.state_dir, record['key'], policy_notice_at=time.time() - policy_retry.CARD_WAIT)
        self.assertEqual(self.tick(record)['policy_retry_state'], 'queued')
        [job] = self.queued()
        self.assertFalse(json.loads(Path(intent).read_text())['card_updated'])
        policy_guard.finish(gateway, self.ctx, intent)  # As its tick does while card_updated is False.
        self.assertEqual(self.claude.cards.replace_record_card.call_count, 2)
        last = json.dumps(self.claude.cards.replace_record_card.call_args.args[2], ensure_ascii=False)
        self.assertIn(f'已转到任务 &#35;{job.job_id} 继续（已因安全拒绝（cyber）换 Opus 4&#46;8 重试）', last)
        self.assertNotIn('检查并继续', last)
        self.assertTrue(json.loads(Path(intent).read_text())['card_updated'])
        self.assertTrue(find_job_record(self.ctx.state_dir, record['key'])['policy_card_updated'])

    def test_a_stopped_bridges_unconfirmed_submit_says_so_on_the_card(self):
        # The Bridge stopped between claiming the submit and queueing it; nothing was queued.
        record = self.stopped()
        policy_retry.advance(self.ctx.state_dir, record['key'], 'pending', 'submitting')
        after = self.tick(record)
        self.assertEqual(after['policy_retry_state'], 'uncertain')
        self.assertEqual(self.claude.cards.set_record_activity.call_args.args[2], policy_retry.CANCELLED['uncertain'])
        self.assertNotIn('将换', after['status_message'])
        self.assertEqual(self.queued(), [])

    def test_a_cancelled_retry_takes_its_promise_off_the_cards_body(self):
        with mock.patch.object(TurnDelivery, 'send', return_value=(True, True)):  # The task card took the result.
            record = self.stopped()
        promise = butler.worded(retry_recovery('cyber', 'Opus 4.8'), record)
        self.assertIn(promise, record['policy_retry_card'])  # The result as the task card shows it.
        self.receive('next', 'another request', root='task')
        after = self.tick(record)
        self.assertEqual(after['policy_retry_state'], 'cancelled')
        [[_, painted, [element]], _] = self.claude.cards.update_record_elements.call_args
        self.assertEqual((painted['key'], element['element_id']), (record['key'], 'answer'))
        self.assertNotIn(promise, element['content'])
        self.assertEqual(element['content'], record['policy_retry_card'].replace(
            promise, butler.worded(POLICY_RECOVERY, record)))
        self.assertEqual(self.claude.cards.set_record_activity.call_args.args[2], '没有换模型重试：本话题有了新任务。')

    def test_an_answer_continuation_of_the_retry_keeps_its_mark(self):
        # The retry asked a question and ended; the owner answers: the continuation goes on as the retry.
        self.tick(self.stopped('asked', 'cyber'))
        job, retried = self.run_queued(None)
        token = 'a' * 16
        path = self.topic_ctx.state_dir / 'questions' / (token + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'token': token, 'job_id': job.job_id, 'topic_id': job.topic_id, 'chat_id': retried['chat_id'],
            'status': 'pending', 'blocking': True, 'request_id': 'r', 'created_at': 1,
            'questions': [{'id': 'q', 'question': '范围？', 'header': '范围', 'options': []}]}))
        self.claude.questions.submit_answer(self.topic_ctx, retried['chat_id'], token, {'answer_0': '全部'}, 'ev')
        [continuation] = self.queued()
        self.assertEqual((continuation.model, continuation.policy_retry), ('claude-opus-4-8', job.policy_retry))
        self.assertEqual(context_for_job(self.claude.harness.context, continuation).options['subagent_model'],
                         'claude-opus-4-8')
        _, refused = self.run_queued('bio')
        self.assertEqual(refused['status'], 'cancelled')
        self.assertNotIn('policy_retry_state', refused)
        self.assertIn('不会自动重试', refused['final_text'])
        self.tick(refused)
        self.assertEqual(self.queued(), [])

    def test_a_stop_whose_notice_was_not_sent_is_retried_too(self):
        # TurnDelivery records a stop it could not send as failed; its execution_status stays cancelled.
        with mock.patch.object(TurnDelivery, 'send', return_value=(False, False)):
            record = self.stopped('undelivered')
        self.assertEqual((record['status'], record['execution_status']), ('failed', 'cancelled'))
        after = self.tick(record)
        self.assertEqual(after['policy_retry_state'], 'queued')
        [job] = self.queued()
        self.assertEqual(job.model, 'claude-opus-4-8')

    def test_a_task_runners_continuation_request_carries_the_mark(self):
        # An answer taken by the retry's task runner: the Bridge queues its continuation from the runner's request.
        self.tick(self.stopped('runner-asked'))
        job, retried = self.run_queued(None)
        directory = self.ctx.state_dir / 'runner-requests'
        (directory / run_dir.REQUESTS).mkdir(parents=True)
        ctx = dataclasses.replace(self.topic_ctx, model=job.model, effort=job.effort)
        gateway = SimpleNamespace(apps=self.app.apps, runtime=self.app.runtime)
        with mock.patch.object(self.claude.questions.runtime, 'runner', runner_ipc.RunnerLink(directory)), \
                mock.patch.object(runner_ipc, 'await_reply', return_value={'ok': True, 'job_id': 0}):
            self.claude.questions.enqueue_continuation(ctx, 'answer', retried['chat_id'], 'answer-runner', None,
                                                       policy_retry=job.policy_retry)
        # A runner from before it sends none: its continuation is queued all the same, unmarked.
        run_dir.post(directory / run_dir.REQUESTS, {
            'kind': 'enqueue_answer', 'harness': 'claude', 'chat_id': retried['chat_id'], 'topic_id': job.topic_id,
            'message_id': 'answer-older', 'text': 'answer', 'model': job.model, 'effort': job.effort,
            'recovery_context': None, 'deadline': time.time() + 30, 'title': '', 'display': ''})
        runner_ipc.serve_requests(gateway, None, job, self.ctx, directory)
        marked, older = self.queued()
        self.assertEqual((marked.message_id, marked.model, marked.policy_retry),
                         ('answer-runner', 'claude-opus-4-8', job.policy_retry))
        self.assertEqual((older.message_id, older.policy_retry), ('answer-older', {}))

    def test_a_submit_that_raised_is_never_repeated(self):
        record = self.stopped()
        with mock.patch.object(self.claude.controls, 'requeue', side_effect=RuntimeError('card send')):
            self.assertEqual(self.tick(record)['policy_retry_state'], 'uncertain')
        self.assertEqual(self.tick(record)['policy_retry_state'], 'uncertain')
        self.assertEqual(self.queued(), [])
        with self.assertRaises(ValueError):
            policy_retry.advance(self.ctx.state_dir, record['key'], 'pending', 'queued')


class ButlerStopTest(unittest.TestCase):
    """A butler turn is not retried: its opened topics, hand-offs and 代答 would run again, without memory of
    them. It only stops, as before."""

    def test_a_butler_refusal_only_stops(self):
        with mock.patch.dict(os.environ, {butler.ENV: '1'}), tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            topic = butler.butler_topic_id('oc_main')
            upsert_job_record(state, 'main-1', job_id=1, message_id='main-1', chat_id='oc_main', topic_id=topic,
                              backend='claude', kind='chat', model='opus', status='running')
            job = SimpleNamespace(message_id='main-1', job_id=1, chat_id='oc_main', topic_id=topic, policy_retry={},
                                  cancel_event=threading.Event())
            usual = ExecutionOutcome('cancelled', 'policy_refusal', True, True, True, policy_event=event())
            said = butler.worded(usual.failure_reply(), job)
            turn = SimpleNamespace(outcome=usual, final_text=said, status_message=usual.summary())
            self.assertIsNone(policy_retry.at_stop(SimpleNamespace(state_dir=state), job, turn))
            self.assertEqual((turn.final_text, turn.status_message), (said, usual.summary()))
            self.assertIn('不会自动重试', turn.final_text)
            self.assertNotIn('policy_retry_state', find_job_record(state, 'main-1'))
            # Nor through the policy guard's path.
            self.assertIsNone(policy_retry.schedule(state, 'main-1', event()))
            self.assertNotIn('policy_retry_state', find_job_record(state, 'main-1'))
