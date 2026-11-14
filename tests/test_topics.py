from __future__ import annotations

import dataclasses
import json
import subprocess
import unittest
import uuid
from unittest import mock


from covey.feishu.cardkit import send_card_message
from covey.feishu.delivery import send_message_with_fallback
from covey.core.jobs import find_job_record
from covey.core.history_storage import recent_history
from covey.core.topics import context_for_topic, remember_topic_message
from support.fake_lark import FakeCardKit
from support.sessions import write_session_id
from support.world import TopicFixture, build_claude_app


class TopicTest(TopicFixture, unittest.TestCase):
    def test_repeated_callback_topic_resolution_does_not_rewrite_aliases(self):
        topic = self.topic('root')
        with mock.patch('covey.core.topics.write_private_text') as write:
            for _ in range(3):
                self.assertEqual(self.topic('root').topic_id, topic.topic_id)
            write.assert_not_called()

    def test_job_lookup_is_scoped_to_topic_chat_backend_and_id(self):
        topic = self.topic('root')
        record = dict(backend='claude', kind='chat', job_id=10)
        with mock.patch('covey.app.harnesses.directory_store') as store:
            store.return_value.query.return_value=[(None, record)]
            self.assertEqual(self.app.harness.topic_record(topic, 'chat', 10), record)
            store.return_value.query.assert_called_once_with(topic_id=topic.topic_id, chat_id='chat', job_id=10)
            record['backend']='codex'
            self.assertIsNone(self.app.harness.topic_record(topic, 'chat', 10))
            # A job of the removed 豆包 Harness is Claude's: the buttons on its card still find it.
            record['backend']='doubao'
            self.assertEqual(self.app.harness.topic_record(topic, 'chat', 10), record)
            self.assertEqual(self.app.harness.topic_records(topic, 'chat'), [record])

    def test_interleaved_topics_resume_their_own_session_and_history(self):
        write_session_id(self.ctx.session_path, str(uuid.uuid4()))
        self.receive("om_a", "主题甲")
        a = self.work()
        self.receive("om_b", "主题乙")
        b = self.work()
        self.assertIsNone(self.backend.calls[0][2])
        self.assertIsNone(self.backend.calls[1][2])
        self.assertNotEqual(a.session_id, b.session_id)
        self.receive("om_a2", "甲的追问", root="om_a", thread="omt_a")
        a2 = self.work()
        self.assertEqual(a.session_id, a2.session_id)
        self.assertEqual(a.session_path, a2.session_path)
        self.assertNotIn("主题乙", self.backend.calls[-1][1])
        self.assertEqual(a2.reply_to_message_id, "om_a")
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs["reply_to_message_id"], "om_a")
        data = find_job_record(self.ctx.state_dir, "om_a2")
        self.assertEqual(data["topic_id"], a.topic_id)
        self.assertEqual(data["reply_to_message_id"], "om_a")



    def test_binding_survives_restart_and_workspace_switch(self):
        a = self.topic("om_a")
        sid = str(uuid.uuid4())
        write_session_id(a.session_path, sid)
        changed = dataclasses.replace(self.ctx, workspace_name="other", workdir=self.root / "other")
        again = context_for_topic(changed, {"chat_id": "oc_chat", "message_id": "om_next", "root_id": "om_a"},
                                  lambda workdir: workdir)
        self.assertEqual(again.workdir, a.workdir)
        self.assertEqual(again.session_path, a.session_path)
        self.assertEqual(build_claude_app().sessions.read_session_id(again.session_path), sid)

    def test_chat_isolation_and_thread_alias(self):
        a = self.topic("om_a", thread_id="omt_a")
        followup = self.topic("om_b", thread_id="omt_a")
        self.assertEqual(a.session_path, followup.session_path)
        other_chat = self.topic("om_b", chat="oc_other", thread_id="omt_a")
        self.assertNotEqual(a.session_path, other_chat.session_path)
        root_and_thread = self.topic("om_c", root_id="om_a", thread_id="omt_a")
        self.assertEqual(root_and_thread.session_path, a.session_path)
        root_only = self.topic("om_d", root_id="om_a")
        self.assertEqual(root_only.session_path, a.session_path)

    def test_queued_jobs_keep_topic_paths(self):
        self.receive("om_a", "甲")
        self.receive("om_b", "乙")
        self.receive("om_a2", "甲追问", root="om_a")
        jobs = [self.app.runtime.jobs.queue.get_nowait() for _ in range(3)]
        self.assertEqual(jobs[0].session_path, jobs[2].session_path)
        self.assertNotEqual(jobs[0].session_path, jobs[1].session_path)
        self.assertNotIn("甲追问", json.dumps(jobs[0].history_snapshot, ensure_ascii=False))
        self.assertNotIn("乙", json.dumps(recent_history(jobs[2].history_path), ensure_ascii=False))






    def test_card_callback_restores_topic_without_network(self):
        ctx = self.topic("om_a")
        remember_topic_message(ctx.state_dir, ctx.topic_id, "oc_chat", "om_card")
        self.app.runtime.owner_open_id = "ou"
        with mock.patch("covey.feishu.message_read.fetch_message_detail") as fetch, \
             mock.patch.object(self.app, "unclaimed_card_action") as handle:
            self.app.handle_card_action_event({"operator_id": "ou", "chat_id": "oc_chat",
                                               "message_id": "om_card"}, self.ctx)
        fetch.assert_not_called()
        self.assertEqual(handle.call_args.args[1].topic_id, ctx.topic_id)
        self.assertEqual(handle.call_args.args[1].session_path, ctx.session_path)

    def test_stream_replacement_keeps_original_root(self):
        from covey.core.jobs import BridgeReplyJob
        from covey.app.task_card import StreamingCard

        job = BridgeReplyJob(1, "question", "oc", "om_in", 0,
                             reply_to_message_id="om_root", topic_id="topic")
        with mock.patch.object(StreamingCard, "create", return_value=None) as create:
            dataclasses.replace(self.app.cards.stream, send_text_message=self.app.messenger.send_message).replace_card(
                None, job, "answer", "failed", "unit")
        replacement = create.call_args.args[1]
        self.assertEqual(replacement.reply_to_message_id, "om_root")
        self.assertEqual(replacement.topic_id, "topic")
        self.assertNotEqual(replacement.message_id, job.message_id)
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs["reply_to_message_id"], "om_root")

    def test_stale_job_notice_returns_to_original_topic(self):
        with FakeCardKit(None) as lark:
            self.receive("om_a", "甲")
            self.app.runtime.jobs.queue.get_nowait()
            self.app.notify_stale_jobs(self.ctx)
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs["reply_to_message_id"], "om_a")
        self.assertEqual([(call.method, call.path) for call in lark.calls], [("POST", "/open-apis/cardkit/v1/cards")])
        lark.send.assert_not_called()


class TopicDeliveryTest(unittest.TestCase):
    def test_markdown_failure_and_split_fallback_never_escape_topic(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2 if "--markdown" in argv else 0,
                "{}" if "--markdown" not in argv else '',
                '{"ok":false,"error":{"type":"validation","param":"--markdown"}}' if "--markdown" in argv else '')
        with mock.patch("covey.feishu.delivery.subprocess.run", side_effect=run) as call:
            result = send_message_with_fallback(
                profile=None, chat_id="oc", text="long message " * 10, idempotency_key="key",
                message_format="markdown", chunk_limit=40, fallback_chunk_limit=15,
                log=lambda _: None, fallback_key_prefix="unit", reply_to_message_id="om_root",
            )
        self.assertTrue(result)
        self.assertGreater(len(call.call_args_list), 2)
        for c in call.call_args_list:
            argv = c.args[0]
            self.assertIn("+messages-reply", argv)
            self.assertIn("--reply-in-thread", argv)
            self.assertNotIn("+messages-send", argv)
            self.assertEqual(argv[argv.index("--message-id") + 1], "om_root")

    def test_split_fallback_stops_at_the_first_failed_piece_and_keeps_its_failure(self):
        from covey.feishu.transport import last_failure
        sent, tried = [], set()
        def run(argv, **kwargs):
            text = argv[argv.index("--text") + 1] if "--text" in argv else ""
            tried.add(text)
            if "--markdown" in argv or len(text) > 15:
                return subprocess.CompletedProcess(argv, 2, "", '{"ok":false,"error":{"type":"validation","param":"--text"}}')
            if len(sent) == 1:
                return subprocess.CompletedProcess(argv, 2, "", '{"ok":false,"error":{"type":"network"}}')
            sent.append(text)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        with mock.patch("covey.feishu.delivery.subprocess.run", side_effect=run):
            result = send_message_with_fallback(
                profile=None, chat_id="oc", text="long message " * 3, idempotency_key="key",
                message_format="markdown", chunk_limit=40, fallback_chunk_limit=15,
                log=lambda _: None, fallback_key_prefix="unit", reply_to_message_id="om_root",
            )
        self.assertFalse(result)
        self.assertEqual(sent, ["long message lo"])
        self.assertEqual(tried, {"", "long message long message long message ", "long message lo", "ng message long"})
        self.assertEqual(last_failure().category, "network")

    AUDIT = ('{"ok":false,"error":{"type":"api","code":230028,'
             '"message":"The messages do NOT pass the audit, ext=contain sensitive data: EMAIL_ADDRESS"}}')

    def audit_send(self, text, refuse):
        """send_message_with_fallback of ``text`` where Feishu's audit refuses what ``refuse`` says: (result, [(text, key)])."""
        tried = []
        def run(argv, **kwargs):
            body = argv[argv.index("--markdown") + 1]
            tried.append((body, argv[argv.index("--idempotency-key") + 1]))
            if refuse(body):
                return subprocess.CompletedProcess(argv, 1, "", self.AUDIT)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        with mock.patch("covey.feishu.delivery.subprocess.run", side_effect=run):
            result = send_message_with_fallback(
                profile=None, chat_id="oc", text=text, idempotency_key="key",
                message_format="markdown", chunk_limit=4000, fallback_chunk_limit=1500,
                log=lambda _: None, fallback_key_prefix="unit", reply_to_message_id="om_root",
            )
        return result, tried

    def test_a_reply_feishu_audit_refuses_for_an_email_is_sent_again_masked(self):
        # T252 (2026-10-31): 230028 refused #1812's reply for git's author email; every resend was the same text.
        from covey.feishu.delivery import MASKED_NOTE
        from covey.feishu.transport import last_failure
        text = "作者邮箱是 `alice@example.com`，仓库 git@github.com:me/repo.git，电话 13812345678"
        result, tried = self.audit_send(text, lambda body: "alice@" in body)
        self.assertTrue(result)
        self.assertEqual(len(tried), 2)
        masked, key = tried[1]
        self.assertNotEqual(key, tried[0][1])
        self.assertEqual(masked, "作者邮箱是 `…@example.com`，仓库 …@github.com:me/repo.git，电话 13812345678"
                                 "\n\n" + MASKED_NOTE)  # The audit named emails only: the phone number stays.
        self.assertIsNone(last_failure())

    def test_a_reply_still_refused_masked_keeps_the_audit_failure(self):
        from covey.feishu.transport import last_failure
        result, tried = self.audit_send("mail me: a.b@example.com", lambda body: True)
        self.assertFalse(result)
        self.assertEqual(len(tried), 2)
        self.assertEqual(last_failure().category, "audit_rejected")
        result, tried = self.audit_send("nothing to mask here", lambda body: True)
        self.assertFalse(result)
        self.assertEqual(len(tried), 1)  # The same text again would be refused again.
        self.assertEqual((last_failure().category, last_failure().code), ("audit_rejected", "230028"))

    def test_masking_takes_what_the_audit_names_or_every_kind(self):
        from covey.feishu.delivery import mask_sensitive
        text = "x@example.com +86 13912345678 1793438301110 v1.13912345678 @types/node"
        self.assertEqual(mask_sensitive(text, "contain sensitive data: PHONE_NUMBER"),
                         "x@example.com 139****5678 1793438301110 v1.13912345678 @types/node")
        self.assertEqual(mask_sensitive(text, "The messages do NOT pass the audit"),
                         "…@example.com 139****5678 1793438301110 v1.13912345678 @types/node")

    def test_streaming_card_send_is_a_topic_reply(self):
        response = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"message_id":"om_card"}}', "")
        with mock.patch("covey.feishu.transport.subprocess.run", return_value=response) as call:
            message_id, _ = send_card_message(None, "oc", "card_1", "key", log=lambda _: None,
                                               reply_to_message_id="om_root")
        self.assertEqual(message_id, "om_card")
        argv = call.call_args.args[0]
        self.assertIn("+messages-reply", argv)
        self.assertIn("--reply-in-thread", argv)
        self.assertEqual(argv[argv.index("--message-id") + 1], "om_root")

    def test_legacy_non_topic_send_remains_available(self):
        response = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"message_id":"om_card"}}', "")
        with mock.patch("covey.feishu.transport.subprocess.run", return_value=response) as call:
            send_card_message(None, "oc", "card_1", "key", log=lambda _: None)
        self.assertIn("+messages-send", call.call_args.args[0])
        self.assertNotIn("--reply-in-thread", call.call_args.args[0])
