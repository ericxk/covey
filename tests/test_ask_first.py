"""先问一句 (T604 #13): on a question card and on a 代发草稿 card the owner asks the topic something first. It reaches the
topic the way its messages do (its next turn, or into the task it runs), the card stays open and nothing counts as an
answer: the question stays pending, the task waiting for it waits on (app/questions.py ask_first, app/owner_send.py ask)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.cards import text_of, walk_dicts
from support.world import TopicFixture

from covey.app.answers import ASK_PREFIX, ASK_START, ask_id
from covey.app.owner_send import ASK_EMPTY as DRAFT_ASK_EMPTY, ASK_GONE, ASK_START as DRAFT_ASK_START, OwnerSend
from covey.cards.owner_send import ASK, ASK_FIELD, owner_send_card
from covey.cards.question_cards import (ASK_EMPTY, ASK_HELD, ASK_KIND, ASK_LABEL, ASK_SETTLED, ASKED, SKIP_ALL_LABEL,
                                        how_to_answer, question_card, question_element)
from covey.core import owner_send as drafts
from covey.core.jobs import find_job_record, upsert_job_record

WORDS = "B 是什么意思？会动线上吗"


def checkpoint_question(**fields):
    """A checkpoint's question of task #5 in topic ``t``*64, as the golden samples draw it."""
    return {"token": "c" * 16, "job_id": 5, "topic_id": "t" * 64, "chat_id": "oc_chat", "status": "pending",
            "blocking": True, "request_id": "checkpoint-om_ask", "created_at": 1,
            "questions": [{"id": "fmt", "header": "格式", "question": "用哪种格式输出？",
                           "options": [{"label": "表格"}, {"label": "列表"}]},
                          {"id": "note", "header": "补充", "question": "还有别的要求吗？"}], **fields}


def ask_row(form):
    """The buttons of the row under 提交回答 and 只发补充说明 (cards/question_cards.py _extras_row), or None."""
    rows = [e for e in form["elements"] if e.get("tag") == "column_set" and e.get("flex_mode") == "flow"
            and any(c["elements"][0].get("name") == "ask_first" for c in e["columns"])]
    return [c["elements"][0] for c in rows[0]["columns"]] if rows else None


class AskCardTest(unittest.TestCase):
    def test_an_open_form_offers_it_under_submit_and_says_what_it_does(self):
        form = question_element(checkpoint_question())
        names = [e.get("name") for e in walk_dicts(form) if e.get("tag") == "button"]
        # 提交回答 and 只发补充说明 keep their row; 不处理 is beside 先问一句 (test_set_aside).
        self.assertEqual(names, ["submit", "skip_all", "ask_first", "set_aside"])
        ask = ask_row(form)[0]
        self.assertEqual((ask["text"]["content"], ask["form_action_type"], ask["type"]), (ASK_LABEL, "submit", "default"))
        self.assertEqual(ask["behaviors"][0]["value"], {"kind": ASK_KIND, "topic_id": "t" * 64, "token": "c" * 16})
        self.assertIn(f"对题目有疑问：写在“补充说明”，点“{ASK_LABEL}”", how_to_answer(checkpoint_question()))
        single = checkpoint_question(questions=checkpoint_question()["questions"][:1])  # Option buttons: the same.
        self.assertTrue(ask_row(question_element(single)))
        self.assertIn(ASK_LABEL, how_to_answer(single))

    def test_not_where_a_turn_is_held_on_the_question_nor_the_butlers(self):
        from covey.core import butler
        cases = {"Claude's own question": checkpoint_question(native_live_question=True),
                 "Codex's paused question": checkpoint_question(native_paused_question=True)}
        for name, record in cases.items():
            with self.subTest(name):
                self.assertIsNone(ask_row(question_element(record)))
                self.assertNotIn(ASK_LABEL, how_to_answer(record))
        with mock.patch.object(butler, "is_butler_topic", return_value=True):
            self.assertIsNone(ask_row(question_element(checkpoint_question())))
        # Settled: no form, no button.
        self.assertNotIn(ASK_LABEL, text_of(question_card(checkpoint_question(status="answered", answers={}))))

    def test_the_form_says_what_was_asked_first(self):
        record = checkpoint_question(**{ASKED: {"at": 1793790000.0, "text": WORDS + "\n第二行", "event": "e"}})
        texts = [e.get("content", "") for e in question_element(record)["elements"] if e.get("tag") == "markdown"]
        line = next(text for text in texts if "你先问了一句" in text)
        self.assertIn("你先问了一句「B 是什么意思？会动线上吗 第二行」，回答在话题里；这张卡还开着，看完再答。", line)
        self.assertIn("<font color='grey'>", line)
        self.assertNotIn("你先问了一句", text_of(question_element(checkpoint_question())))


class AskQuestionTest(TopicFixture, unittest.TestCase):
    """A checkpoint's question whose turn ended (task #N waiting_user): 先问一句 queues the topic's next turn."""

    def setUp(self):
        super().setUp()
        self.receive("om_ask", "整理数据")
        self.asking = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(self.asking)  # Its turn ended: it waits for the answer, no task runs.
        self.ctx_topic = self.topic("om_ask")
        self.checkpoint = self.root / "asking-checkpoint.json"
        self.checkpoint.write_text(json.dumps({"phase": "等你定格式", "requires_user": True, "question": "用哪种格式输出？"},
                                              ensure_ascii=False))
        upsert_job_record(self.ctx_topic.state_dir, self.asking.message_id, status="waiting_user",
                          checkpoint_path=str(self.checkpoint), job_id=self.asking.job_id,
                          topic_id=self.asking.topic_id, chat_id="oc_chat", backend="claude")
        self.record = checkpoint_question(job_id=self.asking.job_id, topic_id=self.asking.topic_id,
                                          question_message_id="om_qcard", card_id="card_q")
        self.path = self.ctx_topic.state_dir / "questions" / (self.record["token"] + ".json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.record, ensure_ascii=False))
        self.painted = []
        self.app.questions.paint_question_state = lambda ctx, record: self.painted.append(record)
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)

    def click(self, supplement=WORDS, event="ev-ask", message="om_qcard"):
        return self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": event, "message_id": message, "operator_id": "ou_user",
            "action_value": {"kind": ASK_KIND, "topic_id": self.record["topic_id"], "token": self.record["token"]},
            "form_value": {"supplement": supplement, "option_0": "0"}})

    def queued(self):
        return [job for job in self.app.runtime.jobs.queue.queue if job.message_id.startswith(ASK_PREFIX)]

    def saved(self):
        return json.loads(self.path.read_text())

    def test_the_question_goes_as_the_next_turn_and_the_card_stays_open(self):
        self.click()
        [job] = self.queued()
        self.assertEqual(job.message_id, ask_id(self.record["token"], "ev-ask"))
        self.assertEqual(job.user_text, WORDS)  # Its card shows what the owner wrote.
        self.assertEqual(job.prompt_text, f"{ASK_START}\n你的问题：\n- 用哪种格式输出？\n- 还有别的要求吗？\n他问：\n{WORDS}")
        self.assertEqual(job.title, "先问一句：" + WORDS)
        self.assertEqual(job.sender_id, "ou_user")
        # It goes on from the asking task's checkpoint, as an answer's continuation would, without being one.
        self.assertEqual(job.recovery_context["phase"], "等你定格式")
        # Nothing answered: the question stays pending, its card open with what was asked, its task waiting.
        saved = self.saved()
        self.assertEqual((saved["status"], saved.get("answers")), ("pending", None))
        self.assertEqual((saved[ASKED]["text"], saved[ASKED]["event"]), (WORDS, "ev-ask"))
        self.assertEqual([record["status"] for record in self.painted], ["pending"])
        self.assertIn(ASKED, self.painted[0])
        asking = find_job_record(self.ctx_topic.state_dir, self.asking.message_id)
        self.assertEqual(asking["status"], "waiting_user")
        self.assertEqual(self.app.questions.pending_questions(self.ctx_topic, "oc_chat")[0]["token"], self.record["token"])
        history = self.ctx_topic.history_path.read_text()
        self.assertIn(ASK_START, history)

    def test_the_answer_after_it_continues_the_task_that_waited(self):
        self.click()
        [ask] = self.queued()
        self.app.runtime.jobs.unregister(ask)  # The ask's turn has run.
        self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": "ev-answer", "message_id": "om_qcard",
            "action_value": {"kind": "bridge_question", "topic_id": self.record["topic_id"], "token": self.record["token"]},
            "form_value": {"option_0": "0", "answer_1": "没有"}})
        answer = next(job for job in self.app.runtime.jobs.queue.queue if job.message_id == "answer-" + self.record["token"])
        self.assertIn("用户补充回答：", answer.prompt_text)
        self.assertEqual(self.saved()["status"], "answered")
        asking = find_job_record(self.ctx_topic.state_dir, self.asking.message_id)
        self.assertEqual((asking["status"], asking["resumed_by"]), ("continued", answer.message_id))

    def test_the_same_click_again_asks_once(self):
        self.click()
        self.click()
        self.assertEqual(len(self.queued()), 1)
        self.click(event="ev-again", supplement="再问一句")
        self.assertEqual(len(self.queued()), 2)
        self.assertEqual(self.saved()[ASKED]["text"], "再问一句")  # The card shows the last one.

    def test_an_empty_supplement_is_refused_on_the_card(self):
        for supplement in ("", "  \n "):
            with self.subTest(repr(supplement)):
                self.click(supplement=supplement)
                _, chat, _, source, card = self.app.cards.repaint_browse_card.call_args.args
                self.assertEqual((chat, source), ("oc_chat", "om_qcard"))
                self.assertIn("⚠️ " + ASK_EMPTY, text_of(card))
                self.assertIn('"tag": "form"', json.dumps(card))  # Still open: written and asked again.
        self.assertEqual(self.queued(), [])
        self.assertNotIn(ASKED, self.saved())
        # Under a form on a task card, which is not redrawn alone: a line in the topic.
        self.click(supplement="", message="om_task_card")
        self.assertIn(ASK_EMPTY, self.app.messenger.send_message.call_args.args[2])
        self.assertEqual(self.queued(), [])

    def test_a_question_settled_or_held_asks_nothing(self):
        for fields, refusal in (({"status": "answered", "answers": {}}, ASK_SETTLED),
                                ({"status": "cancelled"}, ASK_SETTLED),
                                ({"native_live_question": True}, ASK_HELD)):
            with self.subTest(fields):
                self.path.write_text(json.dumps({**self.record, **fields}, ensure_ascii=False))
                with self.assertRaisesRegex(ValueError, refusal):
                    self.click()
                self.assertEqual(self.queued(), [])
        self.assertIn(SKIP_ALL_LABEL, ASK_HELD)


class AskRunningTaskTest(TopicFixture, unittest.TestCase):
    """A question that does not block (其他工作继续), its task still running: 先问一句 goes into that task, as a message
    of the topic does (COVEY_TOPIC_STEER), and the question stays open."""

    def setUp(self):
        super().setUp()
        self.backend.declare(steering=True)
        self.receive("om_run", "整理数据")
        self.running = self.app.runtime.jobs.queue.get_nowait()
        self.running.execution_started = True
        self.control = SimpleNamespace(steer=mock.Mock(return_value=None), supports_steer=True)
        self.running.backend_control = self.control
        self.ctx_topic = self.topic("om_run")
        self.app.harness.context = self.ctx
        self.app.cards.close_streaming_reply = mock.Mock()
        self.app.cards.replace_record_card = mock.Mock(return_value=True)
        self.app.cards.update_job_streaming_reply = mock.Mock(return_value=True)
        self.app.cards.update_card_activity = mock.Mock(return_value=True)
        self.app.cards.set_task_element = mock.Mock(return_value=True)
        self.app.questions.paint_question_state = mock.Mock()
        self.record = checkpoint_question(job_id=self.running.job_id, topic_id=self.running.topic_id, blocking=False,
                                          request_id="ask-1")
        path = self.ctx_topic.state_dir / "questions" / (self.record["token"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.record, ensure_ascii=False))
        self.path = path

    def test_it_goes_into_the_running_task(self):
        self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": "ev-ask", "message_id": "om_task_card", "operator_id": "ou_user",
            "action_value": {"kind": ASK_KIND, "topic_id": self.record["topic_id"], "token": self.record["token"]},
            "form_value": {"supplement": WORDS}})
        mid = ask_id(self.record["token"], "ev-ask")
        text, delivery = self.control.steer.call_args.args
        self.assertEqual(delivery, "queued-steer-" + mid)
        self.assertTrue(text.startswith(ASK_START) and text.endswith("他问：\n" + WORDS))
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
        self.assertEqual(find_job_record(self.ctx_topic.state_dir, mid)["merged_into_job_id"], self.running.job_id)


class AcknowledgeTest(TopicFixture, unittest.TestCase):
    """A turn queued while a task waits for its answer goes on from it (TaskControls.acknowledge_waiting_answer); a
    先问一句's turn does not: the question it asks about still waits."""

    def test_an_ask_turn_leaves_the_waiting_task_as_it_is(self):
        self.receive("om_wait", "整理数据")
        waiting = self.app.runtime.jobs.queue.get_nowait()
        ctx = self.topic("om_wait")
        checkpoint = self.root / "cp.json"
        checkpoint.write_text('{"requires_user": true}')
        upsert_job_record(ctx.state_dir, waiting.message_id, status="waiting_user", checkpoint_path=str(checkpoint),
                          job_id=waiting.job_id, topic_id=waiting.topic_id, chat_id="oc_chat", backend="claude")
        ask = SimpleNamespace(message_id=ASK_PREFIX + "0" * 16, chat_id="oc_chat", job_id=9, recovery_context=None)
        self.assertIsNone(self.app.controls.acknowledge_waiting_answer(ctx, ask))
        self.assertEqual(find_job_record(ctx.state_dir, waiting.message_id)["status"], "waiting_user")
        self.assertIsNone(ask.recovery_context)
        other = SimpleNamespace(message_id="om_next", chat_id="oc_chat", job_id=10, recovery_context=None)
        self.app.controls.acknowledge_waiting_answer(ctx, other)
        self.assertEqual(find_job_record(ctx.state_dir, waiting.message_id)["status"], "continued")
        self.assertEqual(other.recovery_context, {"requires_user": True})


class AskDraftTest(unittest.TestCase):
    """先问一句 on a 代发草稿 card: the line under the draft's box goes to the topic that drafted it; the draft stays as it
    is, unsent, and its card is not redrawn (what the owner changed in the box stays)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.ctx = SimpleNamespace(state_dir=self.state, profile="bot")
        self.topic_app = SimpleNamespace(questions=SimpleNamespace(queue_ask=mock.Mock(return_value=SimpleNamespace(job_id=12))))
        self.target = SimpleNamespace(topic_id="t" * 64)
        self.topics = SimpleNamespace(target=mock.Mock(return_value=(self.topic_app, self.target, {})))
        self.messenger = SimpleNamespace(send_message=mock.Mock(return_value="om_line"))
        self.proxy = SimpleNamespace(topics=self.topics, messenger=self.messenger,
                                     voice=SimpleNamespace(key=lambda *parts: "-".join(parts)),
                                     runtime=SimpleNamespace(log=lambda line: None))
        self.owner = OwnerSend(self.proxy)
        self.owner.publish = mock.Mock(return_value=True)
        self.checkpoint = self.state / "drafting.json"
        self.checkpoint.write_text('{"phase": "草稿写好了"}')
        upsert_job_record(self.state, "om_job1", status="done", checkpoint_path=str(self.checkpoint), job_id=9,
                          topic_id="t" * 64, chat_id="oc_owner")
        self.record = {"id": "d" * 32, "ref": "zs", "status": "drafted", "job_key": "om_job1", "job_id": 9,
                       "topic_id": "t" * 64, "chat_id": "oc_owner", "reply_to": "om_thread", "to": {"email": "z@example.com"},
                       "text": "AK 开好了", "why": "交付", "recipient": {"name": "张三", "open_id": "ou_zs"},
                       "to_label": "张三", "card_id": "card_d", "message_id": "om_draft", "view": {"state": "drafted"},
                       "history": [["drafted", 1, ""]]}
        drafts.write(self.state, self.record)

    def click(self, words=WORDS, owner=True, event="ev-draft"):
        value = {"kind": ASK, "draft": self.record["id"]}
        return self.owner.click(self.ctx, {"message_id": "om_draft", "operator_id": "ou_me", "event_id": event,
                                           "action_value": value, "form_value": {ASK_FIELD: words, "text": "改过的正文"}},
                                value, owner)

    def test_the_card_offers_it_while_the_draft_waits(self):
        card = owner_send_card(self.record, {"state": "drafted"})
        buttons = [e for e in walk_dicts(card) if e.get("tag") == "button"]
        self.assertEqual([b["name"] for b in buttons], ["owner_send_go", "owner_send_drop", "owner_send_ask"])
        self.assertEqual((buttons[2]["form_action_type"], buttons[2]["behaviors"][0]["value"]),
                         ("submit", {"kind": ASK, "draft": self.record["id"]}))
        self.assertTrue([e for e in walk_dicts(card) if e.get("tag") == "input" and e.get("name") == ASK_FIELD])
        for state in ("sent", "declined", "sending", "choose", "superseded"):
            with self.subTest(state):
                self.assertNotIn("owner_send_ask", json.dumps(owner_send_card({**self.record, "status": state},
                                                                              {"state": state})))

    def test_the_question_goes_to_the_drafting_topic_and_the_draft_stays(self):
        self.assertEqual(self.click(), "owner_send_ask")
        self.topics.target.assert_called_once_with(self.state, "t" * 64, "oc_owner")
        call = self.topic_app.questions.queue_ask.call_args
        target, chat, shown, text, mid = call.args
        self.assertEqual((target, chat, shown, mid), (self.target, "oc_owner", WORDS, ask_id(self.record["id"], "ev-draft")))
        self.assertEqual(text, f"{DRAFT_ASK_START}\n草稿 zs，发给 张三。\n他问：\n{WORDS}")
        self.assertNotIn("AK 开好了", text)  # Not the text, which may hold a key.
        self.assertEqual((call.kwargs["title"], call.kwargs["sender"]), ("先问一句：" + WORDS, "ou_me"))
        self.assertEqual(call.kwargs["prior"], {"phase": "草稿写好了"})
        saved = drafts.read(self.state, self.record["id"])
        self.assertEqual((saved["status"], saved["text"]), ("drafted", "AK 开好了"))
        self.owner.publish.assert_not_called()  # Not redrawn: the box keeps what the owner changed.
        self.messenger.send_message.assert_not_called()

    def test_nothing_asked_is_said_in_the_topic(self):
        self.assertEqual(self.click(words="  "), "owner_send_ask_empty")
        self.assertEqual(self.messenger.send_message.call_args.args[2], DRAFT_ASK_EMPTY)
        self.assertEqual(self.messenger.send_message.call_args.kwargs["reply_to_message_id"], "om_thread")
        self.topics.target.return_value = None  # The topic gone, or on a Harness no longer here.
        self.assertEqual(self.click(event="ev-2"), "owner_send_ask")
        self.assertEqual(self.messenger.send_message.call_args.args[2], ASK_GONE)
        self.topic_app.questions.queue_ask.assert_not_called()

    def test_only_the_owner_and_only_while_it_waits(self):
        self.assertEqual(self.click(owner=False), "owner_only")
        drafts.write(self.state, {**self.record, "status": "sent"})
        self.assertEqual(self.click(), "owner_send_stale")
        self.topic_app.questions.queue_ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
