"""What follows an answer, and messages held while questions wait.

The answer saved before the click's ack with the continuation in the follow-up lane, held
notes (card, buttons, a click during the follow-up), a restart during the follow-up and
resuming from a saved answer.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.cards import text_of
from support.world import TopicFixture, acknowledging_lanes, wait_for

from covey.app.questions import recover_question_delivery
from covey.app.card_lanes import CardLanes
from covey.cards.held_note import ANSWER_REQUIRED, note_card
from covey.app.answers import SKIPPED, TITLE_CHARS, UNANSWERED_NOTE
from covey.cards.question_cards import SKIP_ALL_KIND, question_element
from covey.core.jobs import upsert_job_record
from covey.core.state import write_private_text


class HeldNoteCardLookTest(unittest.TestCase):
    """P4 of the UI review: a held note says what it waits for; sending it as feedback comes first."""

    def test_a_held_note_names_the_questions_it_waits_for_and_offers_feedback_first(self):
        # T182: the note did not say it waited for #1106's two questions; the owner sent it again 18 min later.
        from covey.app.held_notes import waiting_for
        from covey.cards.held_note import FEEDBACK_LABEL
        asked = time.mktime(time.localtime()[:3] + (20, 24, 0, 0, 0, -1))
        pending = [{"job_id": 1106, "created_at": asked, "questions": [{}, {}]},
                   {"job_id": 1106, "created_at": asked + 60, "questions": [{}]},
                   {"job_id": 1084, "created_at": asked, "questions": [{}]}]
        note = {"token": "n" * 32, "topic_id": "t", "status": "held", "text": "感觉意思不大",
                "waiting": waiting_for(pending)}
        card = note_card(note)
        self.assertEqual(card["body"]["elements"][0]["content"],
                         "📝 **这条消息还没发给模型**：#1106（20:24）的 3 道题、#1084（20:24）的提问还没答，提交回答时会一起带上")
        row = card["body"]["elements"][-1]
        self.assertEqual(row["flex_mode"], "flow")  # Wraps in the side panel instead of cutting the labels.
        buttons = [c["elements"][0] for c in row["columns"]]
        self.assertEqual([(b["text"]["content"], b["value"]["verb"], b["type"]) for b in buttons],
                         [(FEEDBACK_LABEL, "feedback", "primary"), ("留作下一轮", "next", "default"),
                          ("撤回", "withdraw", "default")])
        self.assertEqual({b["value"]["kind"] for b in buttons}, {"bridge_note"})
        later = note_card({**note, "status": "next_round"})
        self.assertIn("↪ **留作下一轮**：#1106（20:24）的 3 道题、#1084（20:24）的提问回答后单独排队执行", text_of(later))
        self.assertEqual([c["elements"][0]["value"]["verb"] for c in later["body"]["elements"][-1]["columns"]],
                         ["feedback", "attach", "withdraw"])
        # A note held before it kept what it waits for.
        self.assertIn("话题里的问题还没答", text_of(note_card({k: v for k, v in note.items() if k != "waiting"})))


class AnswerFollowUpFixture(TopicFixture):
    """A finished task's question, answered by clicks through card lanes."""

    def setUp(self):
        super().setUp()
        self.receive("om_q", "task")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        self.ctx_topic = self.topic("om_q")
        upsert_job_record(self.ctx_topic.state_dir, job.message_id, status="waiting_user", model="m", effort="e")
        self.token = "c" * 16
        self.path = self.ctx_topic.state_dir / "questions" / (self.token + ".json")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "token": self.token, "job_id": job.job_id, "topic_id": job.topic_id, "chat_id": "oc_chat",
            "status": "pending", "blocking": True, "request_id": "r", "created_at": 1,
            "questions": [{"id": "q", "question": "范围？", "header": "范围", "options": []}]}))
        patcher = mock.patch("covey.core.model_settings.strict_recorded_model_settings", return_value=("m", "e"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.painted = []
        self.app.questions.paint_question_state = lambda ctx, record: self.painted.append(record["status"])
        self.shutdown = threading.Event()
        self.addCleanup(self.shutdown.set)
        self.lane_logs = []
        self.lanes = CardLanes(self.app.handle_card_action_event, lambda event, ctx: "topic:q", self.lane_logs.append,
                               self.shutdown)

    def click(self, event_id):
        self.lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": event_id, "action_value": {
            "kind": "bridge_question", "topic_id": self.ctx_topic.topic_id, "token": self.token},
            "form_value": {"answer_0": "全部"}}, self.ctx_topic)

    def said(self):
        return [c.args[2] for c in self.app.messenger.send_message.call_args_list]

    def wait(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end and not predicate():
            time.sleep(0.01)
        self.assertTrue(predicate())

    def record(self):
        return json.loads(self.path.read_text())


class AnswerFollowUpTest(AnswerFollowUpFixture, unittest.TestCase):
    """A 提交回答 click for a finished task is acknowledged once the answer is saved and its card
    says so; the continuation task's card follows in the topic's follow-up lane (S19 review item 6:
    the click waited ~3.4 s for that card)."""

    def test_the_answer_is_saved_before_the_ack_and_a_repeat_click_meanwhile_is_refused(self):
        queuing, release, seen = threading.Event(), threading.Event(), []

        def enqueue(*args, **kwargs):
            seen.append(self.record()["status"])
            queuing.set()
            release.wait(5)
            return SimpleNamespace(job_id=91)
        self.app.questions.enqueue_continuation = mock.Mock(side_effect=enqueue)
        self.click("first")
        self.assertTrue(queuing.wait(5))
        try:
            saved = self.record()
            self.assertEqual((saved["status"], saved["answers"]["q"]["answers"]), ("submitting", ["全部"]))
            self.assertEqual(self.painted, ["submitting"])  # The card said so before the click returned.
            self.click("again")  # Handled while the continuation is still being queued.
            self.wait(lambda: any("正在创建后续任务" in text for text in self.said()))
        finally:
            release.set()
        self.wait(lambda: self.record()["status"] == "answered")
        self.assertEqual((self.record()["continuation_job_id"], self.record()["delivery_state"]), (91, "queued"))
        self.assertEqual(self.app.questions.enqueue_continuation.call_count, 1)
        self.assertEqual(seen, ["submitting"])
        self.wait(lambda: self.painted[-1] == "answered")
        self.wait(lambda: any(line.startswith("card follow-up kind=bridge_question") for line in self.lane_logs))

    def test_a_continuation_that_cannot_be_queued_is_unconfirmed_and_told(self):
        self.app.questions.enqueue_continuation = mock.Mock(return_value=None)
        self.click("first")
        self.wait(lambda: self.record()["status"] == "uncertain")
        self.wait(lambda: any(text.startswith("提交回答未完成：回答已保存，但后续任务未能入队") for text in self.said()))
        self.click("again")
        self.wait(lambda: any("送达状态未确认" in text for text in self.said()))
        self.assertEqual(self.app.questions.enqueue_continuation.call_count, 1)


class SkipAllFollowUpTest(AnswerFollowUpFixture, unittest.TestCase):
    def test_the_follow_up_is_titled_by_the_supplement(self):
        # T191 #1183 read "/workspace 键 → 不回答" over the owner's new instruction.
        self.app.questions.enqueue_continuation = mock.Mock(return_value=SimpleNamespace(job_id=92))
        self.lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": "skip", "action_value": {
            "kind": SKIP_ALL_KIND, "topic_id": self.ctx_topic.topic_id, "token": self.token},
            "form_value": {"answer_0": "全部", "supplement": "你帮我整理到一个飞书文档里，我好去跟季谦同步一下"}},
            self.ctx_topic)
        self.wait(lambda: self.record()["status"] == "answered")
        call = self.app.questions.enqueue_continuation.call_args
        self.assertEqual(call.kwargs["title"], "补充：你帮我整理到一个飞书文档里，我好去跟季谦同步一下")
        self.assertIn("范围？\n" + SKIPPED, call.args[1])
        self.assertIn("用户" + UNANSWERED_NOTE + "\n你帮我整理到一个飞书文档里", call.args[1])


class HeldNoteFixture(AnswerFollowUpFixture):
    """The finished task's question has two parts, and a message sent meanwhile is held."""

    def setUp(self):
        super().setUp()
        self.backend.declare(questions=True, steering=True)
        record = self.record()
        record["questions"].append({"id": "q2", "question": "顺序？", "header": "顺序", "options": []})
        self.path.write_text(json.dumps(record))  # Two questions: a message sent now is held.
        self.app.messenger.publish_card = mock.Mock(side_effect=lambda *a, on_sent=None, **k: (
            on_sent and on_sent("note-card"), True)[-1])
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        self.app.interaction.interactive_command(self.ctx_topic, "另外记得看日志", "oc_chat", "m1")
        self.note_token = self.note()["token"]
        self.enqueued = []

    def note(self):
        note, = (json.loads(p.read_text()) for p in (self.ctx_topic.state_dir / "held-notes").glob("*.json"))
        return note

    def answer(self):
        self.lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": "first", "action_value": {
            "kind": "bridge_question", "topic_id": self.ctx_topic.topic_id, "token": self.token},
            "form_value": {"answer_0": "全部", "answer_1": "按建议"}}, self.ctx_topic)

    def note_click(self, verb):
        self.lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": "note-" + verb, "action_value": {
            "kind": "bridge_note", "topic_id": self.ctx_topic.topic_id, "token": self.note_token, "verb": verb}},
            self.ctx_topic)

    def hold_enqueue(self):
        """Each continuation is recorded; the answer's waits in enqueue until the returned event is set."""
        queuing, release = threading.Event(), threading.Event()

        def enqueue(ctx, text, chat, mid, *args, **kwargs):
            self.enqueued.append((mid, "另外记得看日志" in text))
            if mid.startswith("answer-"):
                queuing.set()
                release.wait(30)  # Set by the test, or at its cleanup.
            return SimpleNamespace(job_id=90 + len(self.enqueued))
        self.app.questions.enqueue_continuation = mock.Mock(side_effect=enqueue)
        self.addCleanup(release.set)
        return queuing, release

    def hold_follow_up(self):
        """The answer's follow-up waits before it starts until the returned event is set."""
        _, release = self.hold_enqueue()
        release.set()
        started, go = threading.Event(), threading.Event()
        real = self.app.questions._queue_continuation

        def held(*args, **kwargs):
            started.set()
            go.wait(30)
            return real(*args, **kwargs)
        self.app.questions._queue_continuation = held
        self.addCleanup(go.set)
        return started, go

    def settled(self):
        self.wait(lambda: self.record()["status"] == "answered")
        self.wait(lambda: not self.lanes.lanes)


class HeldNoteDuringFollowUpTest(HeldNoteFixture, unittest.TestCase):
    """S21 review item 1 (regression of ffb60a0): a click on a held note between the 提交回答 ack and
    its follow-up sent the note twice (留作下一轮 queued it while the follow-up carried it too) or after
    it was withdrawn. A held note now goes out once or not at all, whatever the interleaving."""

    def test_next_round_while_the_answer_is_queued_sends_the_note_once_with_it(self):
        queuing, release = self.hold_enqueue()
        self.answer()
        self.assertTrue(queuing.wait(5))
        self.note_click("next")  # The follow-up has taken the note: the click shows that.
        self.wait(lambda: self.app.cards.repaint_browse_card.called)
        self.assertEqual(self.note()["status"], "attached")
        release.set()
        self.settled()
        self.assertEqual(self.enqueued, [("answer-" + self.token, True)])
        self.wait(lambda: self.note().get("job_id") == 91)
        self.assertEqual(self.note()["status"], "attached")

    def test_withdraw_while_the_answer_is_queued_is_refused_by_showing_the_note_sent(self):
        queuing, release = self.hold_enqueue()
        self.answer()
        self.assertTrue(queuing.wait(5))
        self.note_click("withdraw")
        self.wait(lambda: self.app.cards.repaint_browse_card.called)
        self.assertEqual(self.note()["status"], "attached")  # Never "withdrawn" for a note that is going.
        self.assertIn("已随回答一起发送", text_of(self.app.cards.repaint_browse_card.call_args.args[4]))
        release.set()
        self.settled()
        self.assertEqual(self.enqueued, [("answer-" + self.token, True)])

    def test_next_round_before_the_follow_up_runs_the_note_once_after_the_answer(self):
        started, go = self.hold_follow_up()
        self.answer()
        self.assertTrue(started.wait(5))
        self.note_click("next")
        self.wait(lambda: self.note()["status"] == "next_round")  # The answer is in flight: not queued yet.
        self.assertEqual(self.enqueued, [])
        go.set()
        self.settled()
        self.wait(lambda: self.note()["status"] == "queued")
        self.assertEqual(self.enqueued, [("answer-" + self.token, False), ("note-" + self.note_token, True)])
        self.assertEqual(self.note()["job_id"], 92)

    def test_withdraw_before_the_follow_up_keeps_the_note_out_of_the_answer(self):
        started, go = self.hold_follow_up()
        self.answer()
        self.assertTrue(started.wait(5))
        self.note_click("withdraw")
        self.wait(lambda: self.note()["status"] == "withdrawn")
        go.set()
        self.settled()
        self.assertEqual(self.enqueued, [("answer-" + self.token, False)])
        self.assertEqual(self.note()["status"], "withdrawn")

    def test_feedback_from_a_note_while_an_answer_is_sent_is_refused(self):
        started, go = self.hold_follow_up()
        self.answer()
        self.assertTrue(started.wait(5))
        self.note_click("feedback")
        self.wait(lambda: any("回答正在发送，稍后再操作这条补充" in text for text in self.said()))
        go.set()
        self.settled()
        self.assertEqual(self.enqueued, [("answer-" + self.token, True)])

    def test_a_note_taken_by_an_unconfirmed_answer_stops_offering_withdraw(self):
        self.app.questions.enqueue_continuation = mock.Mock(side_effect=OSError("disk"))
        self.answer()
        self.wait(lambda: self.record()["status"] == "uncertain")
        self.wait(lambda: self.app.cards.repaint_browse_card.called)
        card = self.app.cards.repaint_browse_card.call_args.args[4]
        self.assertIn("送达状态见问题卡片", text_of(card))
        self.assertNotIn("撤回", json.dumps(card, ensure_ascii=False))
        self.note_click("withdraw")  # Too late to take back what may have gone.
        self.wait(lambda: self.app.cards.repaint_browse_card.call_count >= 2)
        self.assertEqual(self.note()["status"], "attached")


class RestartDuringFollowUpTest(HeldNoteFixture, unittest.TestCase):
    """S21 review item 3: a restart between the 提交回答 ack and its follow-up left 正在发送 on the
    question card, with no task behind it. Startup repaints what the record says now."""

    def submitting(self):
        record = self.record()
        record.update(status="submitting", answers={"q": {"answers": ["全部"]}, "q2": {"answers": ["按建议"]}})
        write_private_text(self.path, json.dumps(record))

    def test_a_restart_before_the_continuation_shows_the_answer_unconfirmed(self):
        self.submitting()
        self.app.questions.notes.change_note(self.ctx_topic, self.note_token, status="attached", answer_token=self.token)
        recover_question_delivery(self.ctx_topic, [self.app.questions], self.app.runtime.log)
        self.assertEqual((self.record()["status"], self.painted), ("uncertain", ["uncertain"]))
        shown = json.dumps(question_element(self.record()), ensure_ascii=False)
        self.assertIn("送达未确认，请先核查，不会自动重复发送", shown)
        self.assertIn("核查已保存回答并继续", shown)
        card = self.app.cards.repaint_browse_card.call_args.args[4]  # The note it took says so too.
        self.assertIn("送达状态见问题卡片", text_of(card))
        self.assertNotIn("撤回", json.dumps(card, ensure_ascii=False))

    def test_a_restart_after_the_continuation_was_queued_shows_it(self):
        self.submitting()
        upsert_job_record(self.ctx_topic.state_dir, "answer-" + self.token, job_id=95, message_id="answer-" + self.token,
                          topic_id=self.ctx_topic.topic_id, chat_id="oc_chat", status="queued")
        recover_question_delivery(self.ctx_topic, [self.app.questions], self.app.runtime.log)
        self.assertEqual((self.record()["status"], self.record()["continuation_job_id"]), ("answered", 95))
        self.assertEqual(self.painted, ["answered"])

    def test_records_settled_before_the_restart_are_not_repainted(self):
        recover_question_delivery(self.ctx_topic, [self.app.questions], self.app.runtime.log)
        self.assertEqual(self.painted, [])


class ResumeSavedAnswerTest(HeldNoteFixture, unittest.TestCase):
    """核查已保存回答并继续 on an answer whose continuation is unconfirmed."""

    def resume(self):
        action = {"kind": "bridge_question_resume", "topic_id": self.ctx_topic.topic_id, "token": self.token}
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "action_value": action,
                                                                 "event_id": "recover"})

    def test_a_note_taken_by_an_answer_that_queued_nothing_goes_with_its_resubmission(self):
        texts = []

        def enqueue(ctx, text, chat, mid, *args, **kwargs):
            texts.append(text)
            return None if len(texts) == 1 else SimpleNamespace(job_id=95)  # 1st: the model setting unconfirmed.
        self.app.questions.enqueue_continuation = mock.Mock(side_effect=enqueue)
        self.answer()
        self.wait(lambda: self.record()["status"] == "uncertain")
        self.wait(lambda: not self.lanes.lanes)
        self.assertEqual((self.note()["status"], self.note().get("job_id")), ("attached", None))
        self.assertIn("另外记得看日志", texts[0])
        self.resume()  # The owner fixed the model setting.
        self.wait(lambda: self.record()["status"] == "answered")
        self.assertEqual(len(texts), 2)
        self.assertIn("另外记得看日志", texts[1])  # The note went with the only continuation queued.
        self.assertEqual(self.note().get("job_id"), 95)

    def test_an_answer_whose_continuation_was_queued_after_all_is_not_queued_again(self):
        """The task runner stopped waiting for the Bridge's receipt, but the Bridge did queue it."""
        record = self.record()
        record.update(status="uncertain", answers={"q": {"answers": ["全部"]}, "q2": {"answers": ["按建议"]}})
        write_private_text(self.path, json.dumps(record))
        self.app.questions.notes.change_note(self.ctx_topic, self.note_token, status="attached", answer_token=self.token)
        upsert_job_record(self.ctx_topic.state_dir, "answer-" + self.token, job_id=95, message_id="answer-" + self.token,
                          topic_id=self.ctx_topic.topic_id, chat_id="oc_chat", status="queued")
        self.app.questions.enqueue_continuation = mock.Mock()
        self.resume()
        self.app.questions.enqueue_continuation.assert_not_called()
        self.assertEqual((self.record()["status"], self.record()["continuation_job_id"]), ("answered", 95))
        self.assertEqual(self.painted[-1], "answered")
        self.assertEqual(self.note().get("job_id"), 95)


class HeldNoteTest(TopicFixture, unittest.TestCase):
    """A message sent while several questions wait goes with the first answer, or after it."""

    def setUp(self):
        super().setUp()
        self.receive("om_q", "task")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.job_id = job.job_id
        self.ctx_topic = self.topic("om_q")
        upsert_job_record(self.ctx_topic.state_dir, job.message_id, status="waiting_user", model="m", effort="e")
        folder = self.ctx_topic.state_dir / "questions"
        folder.mkdir(parents=True)
        self.tokens = []
        for i, question in enumerate(("范围？", "顺序？")):
            token = f"{i:x}" * 16
            self.tokens.append(token)
            (folder / (token + ".json")).write_text(json.dumps({
                "token": token, "job_id": job.job_id, "topic_id": job.topic_id, "chat_id": "oc_chat",
                "status": "pending", "blocking": True, "request_id": f"r{i}", "created_at": i,
                "questions": [{"id": "q", "question": question, "header": question, "options": []}]}))
        self.cards = []
        self.app.messenger.publish_card = mock.Mock(side_effect=lambda *a, on_sent=None, **k: (
            self.cards.append(a[3]), on_sent and on_sent(f"note-card-{len(self.cards)}"), True)[-1])
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        self.app.questions.enqueue_continuation = mock.Mock(side_effect=lambda *a, **k: SimpleNamespace(job_id=90 + len(
            self.app.questions.enqueue_continuation.call_args_list)))
        patcher = mock.patch("covey.core.model_settings.strict_recorded_model_settings", return_value=("m", "e"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend.declare(questions=True, steering=True)

    def say(self, mid, text):
        self.app.interaction.interactive_command(self.ctx_topic, text, "oc_chat", mid)

    def answer(self, index, text):
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.tokens[index], {"answer_0": text}, f"answer-{index}")

    def notes(self):
        return {r["message_id"]: r for r in (json.loads(p.read_text()) for p in
                                             (self.ctx_topic.state_dir / "held-notes").glob("*.json"))}

    def test_a_held_message_goes_with_the_first_answer_and_a_next_round_one_after_the_last(self):
        self.say("m1", "另外记得看日志")
        self.say("m2", "下一轮再做性能对比")
        self.assertIn("这条消息还没发给模型", text_of(self.cards[-1]))
        self.assertIn(ANSWER_REQUIRED, text_of(self.cards[-1]))
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        token = self.notes()["m2"]["token"]
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "next", "action_value": {
            "kind": "bridge_note", "topic_id": self.ctx_topic.topic_id, "token": token, "verb": "next"}})
        self.assertEqual({k: v["status"] for k, v in self.notes().items()}, {"m1": "held", "m2": "next_round"})
        self.answer(0, "全部")
        text = self.app.questions.enqueue_continuation.call_args.args[1]
        self.assertIn("全部", text)
        self.assertIn("用户另外补充：\n另外记得看日志", text)
        self.assertNotIn("性能对比", text)
        self.assertEqual(self.app.questions.enqueue_continuation.call_args.kwargs["title"], "范围？ → 全部")
        self.assertEqual(self.notes()["m1"]["status"], "attached")
        self.assertEqual(self.notes()["m2"]["status"], "next_round")  # A question still waits.
        self.answer(1, "按建议")
        calls = self.app.questions.enqueue_continuation.call_args_list
        self.assertIn("按建议", calls[-2].args[1])
        self.assertEqual(calls[-1].args[1:4], ("下一轮再做性能对比", "oc_chat", "note-" + token))  # Queued after it.
        self.assertEqual(self.notes()["m2"]["status"], "queued")

    def test_failed_note_card_keeps_the_form_requirement_in_the_text_fallback(self):
        self.app.messenger.publish_card = mock.Mock(return_value=False)
        self.say("m1", "都按建议")
        text = self.app.messenger.send_message.call_args.args[2]
        self.assertIn("这条消息还没发给模型", text)
        self.assertIn(ANSWER_REQUIRED, text)
        self.app.questions.enqueue_continuation.assert_not_called()
        self.assertEqual(self.notes()["m1"]["status"], "held")

    def test_feedback_selects_one_question_group_and_preserves_the_full_message(self):
        text = "问题前提不对\n" * 180
        self.say("m1", text)
        note = self.notes()["m1"]
        action = {"token": note["token"], "verb": "feedback"}
        self.app.questions.note_action(self.ctx_topic, "oc_chat", action, "choose-question")
        self.app.questions.enqueue_continuation.assert_not_called()
        card = self.cards[-1]
        self.assertIn("请选择要反馈的一组问题", text_of(card))
        buttons = [e for e in card["body"]["elements"] if e["tag"] == "button"]
        self.assertEqual(len(buttons), 2)
        target = buttons[1]["behaviors"][0]["value"]
        self.app.questions.note_action(self.ctx_topic, "oc_chat", target, "submit-feedback")
        self.app.questions.enqueue_continuation.assert_called_once()
        call = self.app.questions.enqueue_continuation.call_args
        self.assertIn(text.strip(), call.args[1])
        self.assertIn("不表示同意或授权", call.args[1])
        self.assertTrue(call.kwargs["title"].startswith("问题反馈：问题前提不对"))  # The card title stays short.
        self.assertLessEqual(len(call.kwargs["title"]), TITLE_CHARS)
        records = [json.loads((self.ctx_topic.state_dir / "questions" / (token + ".json")).read_text())
                   for token in self.tokens]
        self.assertEqual([r["status"] for r in records], ["pending", "answered"])
        self.assertEqual(records[1]["response_kind"], "feedback")
        self.assertEqual(self.notes()["m1"]["response_kind"], "feedback")

    def test_next_round_note_changed_to_feedback_is_not_queued_again(self):
        self.say("m1", "这些问题不需要回答")
        note = self.notes()["m1"]
        self.app.questions.notes.update_note(self.ctx_topic, note["token"], status="next_round")
        self.app.questions.note_action(self.ctx_topic, "oc_chat", {
            "token": note["token"], "verb": "feedback", "question_token": self.tokens[0]}, "feedback")
        self.assertEqual(self.notes()["m1"]["status"], "attached")
        self.answer(1, "先检查")
        self.assertEqual(self.app.questions.enqueue_continuation.call_count, 2)  # Feedback and the remaining answer.

    def test_a_live_answer_carries_the_note_so_the_model_replies_once(self):
        # #262: the note used to follow a native answer as a supplement, and Claude answered twice.
        control = SimpleNamespace(answer=mock.Mock(), steer=mock.Mock(), user_requests={"r0", "r1"},
                                  control_lock=threading.RLock())
        checkpoint = self.root / "cp.json"
        checkpoint.write_text("{}")
        live = mock.MagicMock(job_id=self.job_id, topic_id=self.ctx_topic.topic_id, chat_id="oc_chat",
                              backend_control=control, cancel_event=threading.Event(), checkpoint_path=str(checkpoint))
        self.app.runtime.jobs.live_job = mock.Mock(return_value=live)
        self.say("m1", "另外记得看日志")
        self.answer(0, "全部")
        control.answer.assert_called_once_with("r0", {"q": {"answers": ["全部", "另外补充：另外记得看日志"]}})
        control.steer.assert_not_called()
        self.app.questions.enqueue_continuation.assert_not_called()
        self.assertEqual(self.notes()["m1"]["status"], "attached")
        self.assertIn("已随回答一起提交", text_of(self.app.cards.repaint_browse_card.call_args.args[4]))

    def test_a_missing_answer_shows_on_one_card_that_turns_into_the_success(self):
        def press(event_id, fields):
            self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": event_id, "message_id": "",
                "action_value": {"kind": "bridge_question", "topic_id": self.ctx_topic.topic_id, "token": self.tokens[0]},
                "form_value": fields})
        press("e1", {})
        self.assertIn("回答还没提交", text_of(self.cards[-1]))
        self.assertIn("还有 1 题没回答：范围？", text_of(self.cards[-1]))
        published = len(self.cards)
        press("e2", {"answer_0": ""})  # Still missing: the same card is repainted, no second one.
        self.assertEqual(len(self.cards), published)
        source = self.app.cards.repaint_browse_card.call_args.args[3]
        self.assertEqual(source, f"note-card-{published}")
        press("e3", {"answer_0": "全部"})
        self.assertEqual(self.app.cards.repaint_browse_card.call_args.args[3], source)
        done = text_of(self.app.cards.repaint_browse_card.call_args.args[4])
        self.assertIn("✓ **回答已提交**", done)
        self.assertNotIn("漏答", done)  # The earlier refusal may have been a conflict, not a missing answer.
        sent = " ".join(str(c) for c in self.app.messenger.send_message.call_args_list)
        self.assertNotIn("没回答", sent)  # No stray error text left behind.

    def test_withdrawn_notes_are_never_sent(self):
        self.say("m1", "算了")
        token = self.notes()["m1"]["token"]
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "w", "action_value": {
            "kind": "bridge_note", "topic_id": self.ctx_topic.topic_id, "token": token, "verb": "withdraw"}})
        self.answer(0, "全部")
        self.assertNotIn("算了", self.app.questions.enqueue_continuation.call_args.args[1])
        self.assertEqual(self.notes()["m1"]["status"], "withdrawn")

    def test_next_round_with_nothing_left_to_answer_is_acknowledged_before_its_task_card(self):
        # REVIEW2 item 9: the note's task card was made inside the click (1.8 s).
        self.say("m1", "下一轮再做性能对比")
        for token in self.tokens:  # Both were answered since: nothing is left to answer.
            path = self.ctx_topic.state_dir / "questions" / (token + ".json")
            write_private_text(path, json.dumps({**json.loads(path.read_text()), "status": "answered"}))
        queuing, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.app.questions.enqueue_continuation = mock.Mock(side_effect=lambda *a, **k: (
            queuing.set(), release.wait(30), SimpleNamespace(job_id=95))[-1])
        lanes, acknowledged = acknowledging_lanes(self, self.app.handle_card_action_event)
        lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": "next", "action_value": {
            "kind": "bridge_note", "topic_id": self.ctx_topic.topic_id, "token": self.notes()["m1"]["token"],
            "verb": "next"}}, self.ctx_topic)
        self.assertTrue(queuing.wait(5))
        wait_for(self, lambda: acknowledged == ["next"])
        self.assertEqual(self.notes()["m1"]["status"], "queued")  # Taken: a second click cannot queue it again.
        release.set()
        wait_for(self, lambda: self.notes()["m1"].get("job_id") == 95)
        self.assertEqual(self.app.questions.enqueue_continuation.call_count, 1)
