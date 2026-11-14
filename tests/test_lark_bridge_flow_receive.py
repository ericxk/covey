"""Receive-flow regressions for topics whose slash commands are closed."""
from __future__ import annotations

import unittest

from covey.core.jobs import context_for_job
from covey.core.harness import RunResult
from support.fake_lark import FakeCardKit
from support.world import TopicFixture


class ReceiveFlowTest(TopicFixture, unittest.TestCase):
    def test_non_target_sender_cannot_enqueue_or_create_topic(self):
        self.app.handle_event({'chat_id':'oc', 'sender_id':'other', 'message_id':'om', 'content':'hello'}, self.ctx, fetch_message_details=False)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertFalse((self.ctx.state_dir/'topics').exists())

    def test_old_slash_commands_are_plain_messages_and_reset_nothing(self):
        import uuid

        from support.sessions import write_session_id
        a = self.topic('om_a')
        sid = str(uuid.uuid4())
        write_session_id(a.session_path, sid)
        for command in ['/new', '/sessions', '/review current', '/goal target', '/watch all', '/workspace use other']:
            self.receive('om_cmd'+command.split()[0][1:], command, root='om_a')
        self.assertEqual(self.app.sessions.read_session_id(a.session_path), sid)
        self.assertIn('/workspace use other', a.history_path.read_text())  # Text for the model, nothing more.
        self.assertFalse(self.app.runtime.shutdown.is_set())

    def test_stop_only_cancels_current_topic_queue(self):
        self.receive('om_a', 'A')
        self.receive('om_b', 'B')
        self.receive('om_a2', 'A2', root='om_a')
        self.receive('om_stop', '/stop', root='om_a')
        self.assertFalse(any(j.cancel_event.is_set() for j in self.app.runtime.jobs.queue.queue))
        self.assertFalse(self.app.runtime.shutdown.is_set())

    def test_retry_is_topic_scoped(self):
        self.receive('om_a', 'A')
        job = self.app.runtime.jobs.queue.get_nowait()
        ctx = context_for_job(self.ctx, job)
        self.app.record_job_state(ctx, job, 'failed', 'failed-A')
        self.app.runtime.unregister_reply_job(job)
        b = self.topic('om_b')
        self.assertIn('没有可重试', self.app.controls.retry_topic(b, 'oc_chat', 'om_retry_b'))
        self.app.controls.retry_topic(ctx, 'oc_chat', 'om_retry_a')
        retry = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(retry.topic_id, job.topic_id)
        self.assertEqual(retry.reply_to_message_id, 'om_a')

    def test_delivery_retry_never_reruns_backend(self):
        self.receive('om_a', 'A')
        job = self.app.runtime.jobs.queue.get_nowait()
        ctx = context_for_job(self.ctx, job)
        self.app.record_job_state(ctx, job, 'failed', '发送失败', final_text='已完成', execution_status='done', delivery_ok=False)
        self.app.runtime.unregister_reply_job(job)
        self.app.controls.retry_topic(ctx, 'oc_chat', 'om_retry')
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertFalse(self.backend.calls)
        self.assertEqual(self.app.messenger.send_message.call_args.args[2], '已完成')

    def test_old_card_cannot_cancel_reused_job_number(self):
        self.receive('om_new', 'new task')
        job = next(iter(self.app.runtime.jobs.queue.queue))
        ctx = context_for_job(self.ctx, job)
        self.app.controls.cancel_reply_job_from_card(ctx, {'job_id': str(job.job_id), 'message_id': 'om_old'}, 'oc_chat', 'om_card_old')
        self.assertFalse(job.cancel_event.is_set())

    def test_card_cannot_cancel_another_topic(self):
        self.receive('om_a', 'A')
        job = next(iter(self.app.runtime.jobs.queue.queue))
        self.app.controls.cancel_reply_job_from_card(self.topic('om_b'), {'job_id': str(job.job_id), 'message_id': job.message_id}, 'oc_chat', 'om_card_b')
        self.assertFalse(job.cancel_event.is_set())

    def test_retry_selects_newest_failure_when_timestamps_tie(self):
        from covey.core.jobs import job_key, upsert_job_record
        self.receive('om_a', 'first')
        self.receive('om_a2', 'second', root='om_a')
        for _ in range(2):
            job = self.app.runtime.jobs.queue.get_nowait()
            ctx = context_for_job(self.ctx, job)
            self.app.record_job_state(ctx, job, 'failed')
            self.app.runtime.unregister_reply_job(job)
            upsert_job_record(ctx.state_dir, job_key(job.message_id, job.job_id), status='failed')
        self.app.controls.retry_topic(ctx, 'oc_chat', 'om_retry')
        retry = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(retry.user_text, 'second')

    def test_reply_context_is_internal_not_card_question(self):
        from covey.core.history_storage import recent_history
        event = {'chat_id': 'oc_chat', 'sender_id': 'ou_user', 'message_id': 'om_followup',
                 'message_type': 'text', 'content': '{"text":"你这里是多轮对话么？"}',
                 'root_id': 'om_root', 'parent_id': 'om_root', 'thread_id': 'omt_root'}
        self.app.handle_event(event, self.ctx, fetch_message_details=False)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(job.user_text, '你这里是多轮对话么？')
        self.assertIn('话题起始消息（背景参考）', job.prompt_text)
        self.assertEqual(job.reply_to_message_id, 'om_root')
        history = recent_history(job.history_path)
        self.assertEqual(history[-1]['display_content'], job.user_text)
        self.app.runtime.unregister_reply_job(job)
        self.receive('om_followup2', '继续说', root='om_root')
        following = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(following.recent_questions, ['你这里是多轮对话么？'])
        self.app.record_job_state(context_for_job(self.ctx, job), job, 'failed')
        self.app.runtime.unregister_reply_job(following)
        self.app.controls.retry_topic(context_for_job(self.ctx, job), 'oc_chat', 'om_retry')
        retried = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(retried.user_text, job.user_text)
        self.assertEqual(retried.prompt_text, job.prompt_text)

    def test_legacy_reply_history_displays_only_original_question(self):
        from covey.cards.review import recent_user_questions
        rows = [{'role': 'user', 'parent_id': 'om_root',
                 'content': '你这里是多轮对话么？\n\n回复上下文：\n- parent_id: `om_root`\n被回复消息：测试'}]
        self.assertEqual(recent_user_questions(rows, '继续'), ['你这里是多轮对话么？'])

    def test_checkpoint_input_and_early_session_survive_until_followup(self):
        lark = FakeCardKit({"data": {"card_id": "card-question"}}, sent=("om-question", "")).start(self)
        import json
        import uuid
        from pathlib import Path
        from covey.core.jobs import find_job_record
        sid=str(uuid.uuid4())
        prompts=[]
        def run(request):
            ctx,prompt,session_id=request.context,request.prompt,request.session_id
            prompts.append(prompt)
            request.callbacks.session(sid)
            # Observing a native SID claims it; only successful finalization
            # activates the topic pointer. On the second turn it is already bound.
            self.assertEqual(self.app.sessions.read_session_id(ctx.session_path), sid if session_id else None)
            if len(prompts)==1:
                Path(request.inputs.checkpoint_path).write_text(json.dumps({
                    'phase':'准备训练','external_tasks':['train-123'],
                    'requires_user':True,'question':'选择哪个数据集？'}))
                return RunResult('需要选择数据集',sid)
            self.assertEqual(session_id,sid)
            return RunResult('已继续执行',sid)
        self.backend.run=run
        self.receive('om_a','开始训练')
        first=self.work()
        self.assertEqual(find_job_record(self.ctx.state_dir,first.message_id)['status'],'waiting_user')
        self.assertIn('选择哪个数据集',str(lark.api.call_args))
        self.receive('om_a2','使用数据集A',root='om_a')
        self.work()
        self.assertIn('train-123',prompts[-1])
        self.assertIn('禁止直接重复提交',prompts[-1])
        self.assertEqual(find_job_record(self.ctx.state_dir,first.message_id)['status'],'continued')
        self.assertEqual(len(lark.calls), 1)
        self.assertEqual((lark.last.method, lark.last.path), ("POST", "/open-apis/cardkit/v1/cards"))
        lark.send.assert_called_once()
