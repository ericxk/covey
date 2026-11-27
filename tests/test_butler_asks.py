"""话题转交管家 (app/butler_asks.py, docs/architecture.md 话题转交管家): the owner's own turn of a topic carries the rule, a
turn that wrote ``ask_butler`` is handed to the butler once (a message at the top level, then a queued butler turn with
the turn source ``handover``), and what cannot be handed on is said in the topic."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import write_topic
from test_butler import BUTLER as MAIN_BUTLER, CHAT as MAIN_CHAT, ButlerUnifiedCase

from covey.app import butler_asks as asks
from covey.app import detached, prompts
from covey.app.app import BridgeApp
from covey.core import butler, projects, turn_source
from covey.core.jobs import BridgeReplyJob, find_job_record, upsert_job_record
from covey.core.state import read_object
from covey.core.topics import message_topic, topic_file

CHAT, OWNER, TOPIC = "oc_owner", "ou_owner", "a1" * 16


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {projects.ENV: "1"}))
        self.enterContext(mock.patch.object(butler, "enabled", return_value=True))
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, topic_number=12, title="迁移订单库")
        self.logs = []
        self.access = SimpleNamespace(owner_turn=lambda chat, sender: sender in ("", OWNER) and chat == CHAT,
                                      agent_chat=lambda chat, group=None: chat == CHAT,
                                      spaces=SimpleNamespace(personal_groups=()))
        base = SimpleNamespace(profile="prof", state_dir=self.state)
        root = butler.butler_topic_id(CHAT)
        self.enqueue = mock.Mock(side_effect=lambda ctx, text, chat, message, **kwargs: SimpleNamespace(message_id=message))
        self.butler_app = SimpleNamespace(
            harness=SimpleNamespace(context=base),
            submission=SimpleNamespace(
                event_context=lambda context, event: SimpleNamespace(
                    topic_id=root, history_path=self.state / "topics" / root / "history.jsonl"),
                enqueue_chat_job=self.enqueue))
        self.app_for = mock.Mock(return_value=self.butler_app)
        self.send_text = mock.Mock(return_value="om_handed")
        self.send_message = mock.Mock(return_value="om_note")
        self.gateway = SimpleNamespace(
            runtime=SimpleNamespace(access=self.access, log=self.logs.append, owner_open_id=OWNER, in_runner=False),
            topics=SimpleNamespace(apps={"claude": SimpleNamespace(harness=SimpleNamespace(context=base))}, default="claude"),
            butler=SimpleNamespace(app_for=self.app_for, keep_topic=lambda ctx, app, chat: "claude-butler"),
            messenger=SimpleNamespace(send_text=self.send_text, send_message=self.send_message),
            voice=SimpleNamespace(safe_key=lambda key: key, key=lambda *parts: "-".join(map(str, parts))))
        self.ctx = SimpleNamespace(state_dir=self.state, profile="prof")
        self.enterContext(mock.patch.object(asks, "context_for_job", return_value=SimpleNamespace(
            profile="prof", reply_to_message_id="om_thread")))

    def turn(self, key="om_turn1", ask=None, **fields):
        """A finished turn of the topic whose checkpoint has ``ask`` as its ask_butler; the job the Bridge has of it."""
        checkpoint = self.state / f"{key}.cp.json"
        checkpoint.write_text(json.dumps({"phase": "完成", **({asks.FIELD: ask} if ask is not None else {})}, ensure_ascii=False))
        upsert_job_record(self.state, key, **{"status": "done", "verified_reply_authorized": True, "job_id": 7,
                                              "topic_id": TOPIC, "chat_id": CHAT, "message_id": key, "sender_id": OWNER,
                                              "user_text": "开个专项推进这件事", "checkpoint_path": str(checkpoint), **fields})
        return SimpleNamespace(message_id=key, job_id=7, topic_id=TOPIC, chat_id=CHAT)


class RuleTest(Case):
    def offered(self, message_id="om_1", sender=OWNER, topic=TOPIC, chat=CHAT, **fields):
        job = {"message_id": message_id, "chat_id": chat, "sender_id": sender, "topic_id": topic, **fields}
        return asks.offered(self.access, self.state, job)

    def test_the_owners_own_turn_of_a_topic_carries_the_rule(self):
        self.assertTrue(self.offered())
        self.assertTrue(self.offered("web-op1"))  # His words from the web page.
        self.assertTrue(self.offered(sender=""))  # A record without a sender, in his private chat.

    def test_hand_ons_wake_ups_answers_retries_and_other_peoples_turns_do_not(self):
        for message in ("main-abc", "lead-p1-3", "revisit-a1a1-1-0", "answer-q1"):  # q1: no question on record.
            self.assertFalse(self.offered(message), message)
        self.assertFalse(self.offered(sender="ou_colleague"))
        self.assertFalse(self.offered(chat="oc_somewhere"))  # No butler in that chat.
        self.assertFalse(self.offered(auto_retry=1))  # An automatic retry (app/auto_retry.py COUNTS).
        self.assertFalse(self.offered(policy_retry={"of": "k"}))
        ask = turn_source.make(turn_source.ASK, asserted_by="topic", collab="c1")
        self.assertFalse(self.offered(turn_source=ask))  # A 协作 turn.

    def question(self, token, **fields):
        path = self.state / "questions" / f"{token}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"token": token, "topic_id": TOPIC, "chat_id": CHAT, "status": "answered", **fields}))

    def test_the_owners_answer_on_the_question_card_is_his_own_words(self):
        self.question("qcard", answer_event="evt-click-1")  # His click on the card.
        self.question("qweb", answer_event="web-op9")  # His answer from the web page.
        self.question("qsaid", answer_event="om_said")  # A message of the topic taken as the answer.
        for token in ("qcard", "qweb", "qsaid"):
            self.assertTrue(self.offered(f"answer-{token}"), token)
        self.assertFalse(self.offered("answer-qcard", sender="ou_colleague"))
        self.assertFalse(self.offered("answer-qcard", auto_retry=1))

    def test_an_answer_the_butler_or_a_lead_gave_for_him_is_not(self):
        self.question("qbutler", answer_event="main-req1")  # The butler's question_answer.
        self.question("qlead", answer_event="lead-req2", triage={"state": "answered_by_lead"})  # A lead's project_answer.
        self.question("qlead2", answer_event="evt", triage={"state": "answered_by_lead"})
        for token in ("qbutler", "qlead", "qlead2", "../qcard", ".hidden"):
            self.assertFalse(self.offered(f"answer-{token}"), token)

    def test_the_butlers_family_and_a_proxy_topic_do_not(self):
        self.assertFalse(self.offered(topic=butler.butler_topic_id(CHAT)))
        self.assertFalse(self.offered(topic=projects.lead_topic_id(CHAT, "p1p1p1")))
        write_topic(self.state, topic_id="b2" * 16, chat_id=CHAT, proxy={"contact": "x"})
        self.assertFalse(self.offered(topic="b2" * 16))

    def test_off_without_projects_or_the_butler(self):
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertFalse(self.offered())
        with mock.patch.object(butler, "enabled", return_value=False):
            self.assertFalse(self.offered())

    def test_the_rule_is_put_after_the_checkpoint_file_of_the_owners_turn_only(self):
        from covey.app.runs import Runs

        runs = SimpleNamespace(runtime=SimpleNamespace(access=self.access))
        ctx = SimpleNamespace(state_dir=self.state, topic_id=TOPIC)
        job = SimpleNamespace(topic_id="", chat_id=CHAT, message_id="om_1", sender_id=OWNER)
        self.assertEqual(Runs._ask_note(runs, ctx, job), "\n\n" + asks.RULE)  # The context's topic when the job has none.
        self.assertEqual(Runs._ask_note(runs, ctx, SimpleNamespace(**{**vars(job), "message_id": "main-tok"})), "")

    def test_the_butlers_rule_reads_the_handed_message(self):
        self.assertIn(prompts.ASKED_RULE, prompts.project_rules())
        self.assertIn(asks.HEAD.format(label="T12"), prompts.ASKED_RULE)
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertEqual(prompts.project_rules(), ())

    def test_the_checkpoint_field_is_read_as_written_and_cut(self):
        self.assertEqual(asks.wanted({asks.FIELD: "开专项「X」"}), {"ask": "开专项「X」", "why": ""})
        self.assertEqual(asks.wanted({asks.FIELD: {"ask": "  开专项\n「X」 ", "why": "话题开不了"}}),
                         {"ask": "开专项 「X」", "why": "话题开不了"})
        self.assertEqual(len(asks.wanted({asks.FIELD: {"ask": "长" * 900}})["ask"]), asks.ASK_CHARS)
        for raw in (None, "", {"why": "x"}, ["开专项"], {"ask": "  "}):
            self.assertIsNone(asks.wanted({asks.FIELD: raw}), raw)
        self.assertIsNone(asks.wanted({}))


class HandOnTest(Case):
    ASK = {"ask": "开专项「迁移订单库」，按启动包推进，把 T12 收进去", "why": "话题开不了专项"}
    TEXT = "↪ T12 转给管家\n你在 T12 说：开个专项推进这件事\nT12 的理解：开专项「迁移订单库」，按启动包推进，把 T12 收进去"

    def test_a_turn_with_an_ask_is_a_message_at_the_top_level_and_a_queued_handover_turn_of_the_butler(self):
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        (profile, chat, text, key), _ = self.send_text.call_args
        self.assertEqual((profile, chat, text), ("prof", CHAT, self.TEXT))
        (target, shown, chat, message), kwargs = self.enqueue.call_args
        self.assertEqual((shown, chat, message, kwargs["prompt_content"], kwargs["sender_id"]),
                         (self.TEXT, CHAT, "om_handed", self.TEXT, OWNER))
        source = kwargs["source"]
        self.assertEqual((source["kind"], source["asserted_by"], source["recorded_by"], source["level"]),
                         (turn_source.HANDOVER, "owner", "bridge", "owner"))
        self.assertEqual(source["origin"], {"topic": TOPIC, "message_id": "om_turn1", "chat": CHAT})
        self.assertEqual(turn_source.refusal({"turn_source": source}), "")  # This release runs it.
        self.assertFalse(turn_source.behavior({"turn_source": source}).steers)  # Queued, never a 补充.
        self.assertEqual(message_topic(self.state, CHAT, "om_handed"), butler.butler_topic_id(CHAT))
        self.assertIn("om_handed", target.history_path.read_text(encoding="utf-8"))
        record = read_object(asks.record_path(self.state, "om_turn1"))
        self.assertEqual((record["state"], record["message_id"], record["butler_job"], record["why"]),
                         ("sent", "om_handed", "om_handed", "话题开不了专项"))
        self.send_message.assert_not_called()

    def test_the_butlers_model_reads_the_owners_message_whole_its_message_and_card_the_start(self):
        # W0-3 (T892 P0-2): the butler got 200 characters of the owner's message, a forward as "[合并转发 2 条]".
        whole = "合并转发消息，共 2 条记录。\n\n1. 张三：订单服务 502 了\n2. 李四：先看网关日志"
        asks.collect(self.gateway, self.turn(ask=self.ASK, user_text="[合并转发 2 条]", prompt_text=whole), self.ctx)
        text = self.send_text.call_args.args[2]
        self.assertNotIn("订单服务 502", text)
        (target, shown, chat, message), kwargs = self.enqueue.call_args
        self.assertEqual(shown, text)
        self.assertTrue(kwargs["prompt_content"].startswith(text + "\n\n"), kwargs["prompt_content"])
        self.assertIn("> 1. 张三：订单服务 502 了\n> 2. 李四：先看网关日志", kwargs["prompt_content"])

    def test_words_no_more_than_the_message_says_and_a_turn_not_on_his_message_add_nothing(self):
        asks.collect(self.gateway, self.turn(ask=self.ASK, prompt_text="开个专项推进这件事"), self.ctx)
        self.assertEqual(self.enqueue.call_args.kwargs["prompt_content"], self.TEXT)
        self.assertEqual(asks.whole_words({"message_id": "answer-q7", "prompt_text": "用户补充回答：很长的一段"}, "x"), "")

    def test_an_answer_on_the_question_card_is_handed_on_with_the_question_as_its_origin(self):
        questions = self.state / "questions"
        questions.mkdir(parents=True, exist_ok=True)
        (questions / "q7.json").write_text(json.dumps({"token": "q7", "topic_id": TOPIC, "chat_id": CHAT,
                                                       "status": "answered", "answer_event": "evt-click"}))
        asks.collect(self.gateway, self.turn("answer-q7", ask=self.ASK, user_text="用户补充回答：\n怎么推进\n交给管家"),
                     self.ctx)
        self.assertIn("你在 T12 说：用户补充回答： 怎么推进 交给管家", self.send_text.call_args.args[2])
        source = self.enqueue.call_args.kwargs["source"]
        self.assertEqual(source["origin"], {"topic": TOPIC, "message_id": "answer-q7", "chat": CHAT, "question": "q7"})
        self.assertEqual(source["level"], "owner")
        self.assertEqual(read_object(asks.record_path(self.state, "answer-q7"))["state"], "sent")

    def test_the_owners_words_come_from_the_topics_history_by_the_message_id(self):
        from covey.core.history_storage import append_history
        from covey.core.topics import topic_file

        history = topic_file(self.state, TOPIC).parent / "history.jsonl"
        append_history(history, "user", "把这个开成专项吧\n附件：", {"message_id": "om_turn1"})
        append_history(history, "assistant", "好，交给管家", {"message_id": "om_turn1"})
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        self.assertIn("你在 T12 说：把这个开成专项吧", self.send_text.call_args.args[2])

    def test_a_turn_read_twice_and_a_later_turn_with_the_same_words_hand_on_once(self):
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        asks.collect(self.gateway, self.turn("om_turn2", ask=self.ASK), self.ctx)  # Its checkpoint copied.
        self.assertEqual(self.send_text.call_count, 1)
        self.assertEqual(self.enqueue.call_count, 1)
        self.assertTrue(any("same words" in line for line in self.logs))
        asks.collect(self.gateway, self.turn("om_turn3", ask={"ask": "另开一个话题查 Y"}), self.ctx)
        self.assertEqual(self.send_text.call_count, 2)

    def test_what_is_not_handed_on(self):
        cases = {"om_failed": {"status": "failed"}, "om_unauthorized": {"verified_reply_authorized": False},
                 "main-tok": {}, "om_colleague": {"sender_id": "ou_colleague"}, "answer-q1": {}}
        for key, fields in cases.items():
            asks.collect(self.gateway, self.turn(key, ask=self.ASK, **fields), self.ctx)
        asks.collect(self.gateway, self.turn("om_none"), self.ctx)  # No ask_butler.
        self.send_text.assert_not_called()
        self.enqueue.assert_not_called()
        self.send_message.assert_not_called()
        self.assertEqual(sum("butler ask ignored" in line for line in self.logs), 5)

    def test_an_automatic_retrys_ask_is_not_handed_on_and_its_thread_says_so(self):
        asks.collect(self.gateway, self.turn("om_retry", ask=self.ASK, auto_retry=1, retry_of="om_turn0"), self.ctx)
        self.send_text.assert_not_called()
        (profile, chat, text, key), kwargs = self.send_message.call_args
        self.assertIn("这一轮是自动重试", text)
        self.assertEqual(kwargs["reply_to_message_id"], "om_thread")

    def test_no_butler_to_take_it_is_said_in_the_topic_and_a_later_turn_may_hand_it_on(self):
        self.app_for.return_value = None
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        self.send_text.assert_not_called()
        (profile, chat, text, key), kwargs = self.send_message.call_args
        self.assertEqual((chat, kwargs["reply_to_message_id"]), (CHAT, "om_thread"))
        self.assertIn("没能交给管家：管家现在没有开", text)
        self.assertEqual(read_object(asks.record_path(self.state, "om_turn1"))["state"], "no_butler")
        self.app_for.return_value = self.butler_app  # The owner turned the butler on and asked again.
        asks.collect(self.gateway, self.turn("om_turn2", ask=self.ASK), self.ctx)
        self.assertEqual(self.send_text.call_count, 1)

    def test_a_message_that_did_not_go_out_is_not_sent_again_and_is_said(self):
        self.send_text.return_value = ""
        for ambiguous, word in ((False, "failed"), (True, "uncertain")):
            with self.subTest(word=word), mock.patch("covey.feishu.transport.last_failure",
                                                     return_value=SimpleNamespace(ambiguous=ambiguous)):
                key = f"om_{word}"
                asks.collect(self.gateway, self.turn(key, ask={"ask": f"开专项 {word}"}), self.ctx)
                asks.collect(self.gateway, self.turn(key, ask={"ask": f"开专项 {word}"}), self.ctx)
                self.assertEqual(read_object(asks.record_path(self.state, key))["state"], word)
        self.assertEqual(self.send_text.call_count, 2)  # Once each, never again.
        self.enqueue.assert_not_called()
        texts = [call.args[2] for call in self.send_message.call_args_list]
        self.assertIn("消息没发出去", texts[0])
        self.assertIn("可能没发出去：私聊里没看到“↪ T12 转给管家”", texts[1])

    def test_a_butler_turn_that_was_not_queued_is_said_and_not_handed_again(self):
        self.enqueue.side_effect = None
        self.enqueue.return_value = None
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        self.assertEqual(read_object(asks.record_path(self.state, "om_turn1"))["state"], "not_queued")
        self.assertIn("管家的这一轮没排上", self.send_message.call_args.args[2])
        asks.collect(self.gateway, self.turn("om_turn2", ask=self.ASK), self.ctx)  # The message is there: not sent twice.
        self.assertEqual(self.send_text.call_count, 1)

    def test_a_butler_turn_whose_queueing_raised_is_said_and_never_left_sent(self):
        # T688: a task runner's enqueue_chat_job raised (任务进程不新建任务); collect swallowed it, the record stayed
        # "sent" with no butler turn and nothing said, and the same words were then taken as handed on for 7 days.
        self.enqueue.side_effect = RuntimeError("任务进程不新建任务")
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        record = read_object(asks.record_path(self.state, "om_turn1"))
        self.assertEqual((record["state"], record["message_id"]), ("not_queued", "om_handed"))
        self.assertNotIn("butler_job", record)
        self.assertIn("管家的这一轮没排上", self.send_message.call_args.args[2])
        self.assertEqual(self.send_message.call_args.kwargs["reply_to_message_id"], "om_thread")
        self.assertTrue(any("butler ask raised" in line and "RuntimeError" in line for line in self.logs))
        asks.collect(self.gateway, self.turn("om_turn2", ask=self.ASK), self.ctx)  # The message is there: not again.
        self.assertEqual(self.send_text.call_count, 1)

    def test_a_step_that_raised_before_or_in_the_send_is_said_as_what_it_left(self):
        self.app_for.side_effect = OSError("topics unreadable")  # Nothing sent: a later turn may hand it on.
        asks.collect(self.gateway, self.turn("om_a", ask={"ask": "开专项 A"}), self.ctx)
        self.assertEqual(read_object(asks.record_path(self.state, "om_a"))["state"], "failed")
        self.assertIn("消息没发出去", self.send_message.call_args.args[2])
        self.send_text.assert_not_called()
        self.app_for.side_effect = None
        self.send_text.side_effect = TimeoutError("lark-cli")  # It may have gone: never sent again (rule 1).
        asks.collect(self.gateway, self.turn("om_b", ask={"ask": "开专项 B"}), self.ctx)
        self.assertEqual(read_object(asks.record_path(self.state, "om_b"))["state"], "uncertain")
        self.assertIn("可能没发出去", self.send_message.call_args.args[2])
        self.send_text.side_effect = None
        asks.collect(self.gateway, self.turn("om_a2", ask={"ask": "开专项 A"}), self.ctx)
        asks.collect(self.gateway, self.turn("om_b2", ask={"ask": "开专项 B"}), self.ctx)
        self.assertEqual([call.args[2].splitlines()[-1] for call in self.send_text.call_args_list[1:]],
                         ["T12 的理解：开专项 A"])  # A again, B not.
        self.enqueue.assert_called_once()

    def test_a_task_runner_hands_nothing_on_and_its_bridge_does_once(self):
        # T688: a runner's process_reply_job reached collect too, sent "↪ T12 转给管家", could not queue the butler's
        # turn and left the record "sent"; its Bridge then skipped it as handed on.
        self.gateway.runtime.in_runner = True
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        self.send_text.assert_not_called()
        self.enqueue.assert_not_called()
        self.send_message.assert_not_called()
        self.assertFalse((self.state / asks.FOLDER).exists())
        self.gateway.runtime.in_runner = False  # Its Bridge, once the runner has exited.
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        self.assertEqual((self.send_text.call_count, self.enqueue.call_count), (1, 1))
        self.assertEqual(read_object(asks.record_path(self.state, "om_turn1"))["butler_job"], "om_handed")

    def test_nothing_at_all_with_the_switch_off(self):
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        self.send_text.assert_not_called()
        self.assertFalse((self.state / asks.FOLDER).exists())

    def test_a_later_turn_may_hand_on_the_same_words_after_the_days(self):
        asks.collect(self.gateway, self.turn(ask=self.ASK), self.ctx)
        with mock.patch.object(asks.time, "time", return_value=time.time() + (asks.SAME_DAYS + 1) * 86400):
            asks.collect(self.gateway, self.turn("om_turn2", ask=self.ASK), self.ctx)
        self.assertEqual(self.send_text.call_count, 2)


class TaskRunnerTest(ButlerUnifiedCase):
    """T688 through the Bridge's own parts: a topic's turn that wrote ask_butler ran in a task runner (its
    process_reply_job reaches the turn's end as a turn run in the Bridge does, runtime.detached() being False there);
    then its Bridge ends it once the runner has exited (app/detached.py after_job, as finish_detached calls it)."""

    TOPIC = "c3" * 16

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {projects.ENV: "1"}))
        self.app.runtime.access.remember_owner_chat(MAIN_CHAT)
        self.app.messenger.send_text = mock.Mock(return_value="om_handed")
        write_topic(self.state, topic_id=self.TOPIC, chat_id=MAIN_CHAT, harness_id="claude", binding_revision=1,
                    topic_number=12, title="迁移订单库", workspace_locked=True)
        checkpoint = self.state / "om_turn.cp.json"
        checkpoint.write_text(json.dumps({asks.FIELD: HandOnTest.ASK}, ensure_ascii=False))
        upsert_job_record(self.state, "om_turn", status="done", verified_reply_authorized=True, job_id=7,
                          topic_id=self.TOPIC, chat_id=MAIN_CHAT, message_id="om_turn", sender_id="owner",
                          user_text="开个专项推进这件事", checkpoint_path=str(checkpoint), backend="claude")
        self.job = BridgeReplyJob(job_id=7, user_text="开个专项推进这件事", chat_id=MAIN_CHAT, message_id="om_turn",
                                  timeout=60, topic_id=self.TOPIC, sender_id="owner",
                                  history_path=topic_file(self.state, self.TOPIC).parent / "history.jsonl")
        self.job.harness_id = "claude"

    def run_turn(self, runner):
        """The job's process_reply_job, its turn already ended (done, the checkpoint written): ``runner`` is the
        runtime's link to its Bridge, None in the Bridge itself."""
        self.app.runtime.runner = runner
        try:
            with mock.patch("covey.app.job_dispatch.check_or_raise"), \
                    mock.patch.object(BridgeApp, "process_reply_job", autospec=True) as turn:
                self.app.process_reply_job(self.job, self.ctx)
            turn.assert_called_once()
        finally:
            self.app.runtime.runner = None

    def test_the_runner_hands_nothing_on_and_leaves_no_queued_job_and_its_bridge_hands_it_on_once(self):
        self.run_turn(object())
        # Live on 2026-11-23 (T682's turn #6205): "↪ T682 转给管家" went out from the runner, its job #1 (the runner's
        # own count) was recorded queued and refused, the record said "sent", and the next deploy waited on job #1.
        self.app.messenger.send_text.assert_not_called()
        self.assertEqual(read_object(asks.record_path(self.state, "om_turn")), {})
        self.assertIsNone(find_job_record(self.state, "om_handed"))
        self.assertEqual(detached.restart_blockers(self.state), [])
        self.assertEqual(self.butler_jobs(), [])
        detached.after_job(self.app, self.app.apps["claude"], self.job, self.ctx)  # finish_detached's, in the Bridge.
        (profile, chat, text, key), _ = self.app.messenger.send_text.call_args
        self.assertEqual((chat, text.splitlines()[0]), (MAIN_CHAT, "↪ T12 转给管家"))
        (butler_job,) = self.butler_jobs()
        self.assertEqual((butler_job.message_id, butler_job.topic_id), ("om_handed", MAIN_BUTLER))
        self.assertEqual(turn_source.kind(butler_job), turn_source.HANDOVER)
        self.assertEqual(find_job_record(self.state, "om_handed")["status"], "queued")
        record = read_object(asks.record_path(self.state, "om_turn"))
        self.assertEqual((record["state"], record["butler_job"]), ("sent", "om_handed"))
        detached.after_job(self.app, self.app.apps["claude"], self.job, self.ctx)  # Read twice: handed on once.
        self.assertEqual((self.app.messenger.send_text.call_count, len(self.butler_jobs())), (1, 1))

    def test_a_turn_run_in_the_bridge_itself_is_handed_on_at_its_end_as_before(self):
        self.run_turn(None)  # No task runners (a test app): process_reply_job ends the turn here.
        self.assertEqual(self.app.messenger.send_text.call_count, 1)
        self.assertEqual([job.message_id for job in self.butler_jobs()], ["om_handed"])


if __name__ == "__main__":
    unittest.main()
