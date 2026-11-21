"""不处理 (T604 #15) on a question card: every question 不回答此题 with nothing added, nothing sent now, the question off
the 等你 lists (手上的事, 看板, the web page); 撤销 opens the form again until the topic's next turn takes it as its
answer (app/questions.py set_aside, restore, take_set_aside)."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from support.cards import text_of, walk_dicts
from support.world import TopicFixture

from covey.app import binding, web_pairing
from covey.app.answers import SET_ASIDE_START, SKIPPED
from covey.app.web_collect import Collector
from covey.cards.question_cards import (RESTORE_KIND, RESTORE_LABEL, SET_ASIDE, SET_ASIDE_ANSWER, SET_ASIDE_HELD,
                                        SET_ASIDE_KIND, SET_ASIDE_LABEL, SET_ASIDE_SETTLED, how_to_answer, question_card,
                                        question_element)
from covey.core import board_read
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.statuses import QUESTION_TRANSITIONS
from covey.core.store import update_question

from test_ask_first import ask_row, checkpoint_question


class SetAsideCardTest(unittest.TestCase):
    def test_the_form_offers_it_beside_ask_first_as_a_text_button(self):
        form = question_element(checkpoint_question())
        ask, aside = ask_row(form)
        self.assertEqual((aside["text"]["content"], aside["type"], aside["name"]), (SET_ASIDE_LABEL, "text", "set_aside"))
        self.assertNotIn("form_action_type", aside)  # It sends no field.
        self.assertEqual(aside["behaviors"][0]["value"], {"kind": SET_ASIDE_KIND, "topic_id": "t" * 64, "token": "c" * 16})
        self.assertIn(f"先不管这组题：点“{SET_ASIDE_LABEL}”", how_to_answer(checkpoint_question()))
        self.assertNotIn(SET_ASIDE_LABEL, how_to_answer(checkpoint_question(native_live_question=True)))

    def test_a_question_set_aside_says_so_and_offers_its_undo_until_a_turn_takes_it(self):
        aside = checkpoint_question(status=SET_ASIDE, response_kind=SET_ASIDE,
                                    answers={"fmt": {"answers": [SKIPPED]}, "note": {"answers": [SKIPPED]}})
        card = question_card(aside, "T70")
        self.assertEqual(card["config"]["summary"]["content"], "❓ #5 的 2 道题 · 你选了不处理 · 话题下一轮开始前可以撤销")
        self.assertNotIn('"tag": "form"', json.dumps(card))
        undo = next(e for e in walk_dicts(card) if e.get("tag") == "button")
        self.assertEqual((undo["text"]["content"], undo["behaviors"][0]["value"]),
                         (RESTORE_LABEL, {"kind": RESTORE_KIND, "topic_id": "t" * 64, "token": "c" * 16}))
        self.assertIn("你选了不处理：每题记为不回答（不表示同意或授权）。从“等你”里撤下了", text_of(card))
        panels = lambda drawn: [e["expanded"] for e in walk_dicts(drawn) if e.get("tag") == "collapsible_panel"]  # noqa: E731
        self.assertEqual(panels(card), [True])  # Open: 撤销 at hand.
        taken = question_card({**aside, "status": "answered", "delivery_state": "delivered", "continuation_job_id": 9}, "T70")
        self.assertEqual(taken["config"]["summary"]["content"], "❓ #5 的 2 道题 · 你选了不处理 → 随 #9 告诉了模型")
        self.assertFalse([e for e in walk_dicts(taken) if e.get("tag") == "button"])
        self.assertEqual(panels(taken), [False])
        self.assertIn("已随话题的下一轮告诉模型", text_of(taken))

    def test_its_status_moves_only_along_the_table(self):
        self.assertIn(SET_ASIDE, QUESTION_TRANSITIONS["pending"])
        self.assertEqual(QUESTION_TRANSITIONS[SET_ASIDE], {"pending", "answered", "cancelled"})


class SetAsideTest(TopicFixture, unittest.TestCase):
    """A checkpoint's question of task #N, its turn ended (waiting_user), its card in the topic."""

    def setUp(self):
        super().setUp()
        self.receive("om_ask", "整理数据")
        self.asking = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(self.asking)
        self.ctx_topic = self.topic("om_ask")
        checkpoint = self.root / "asking-checkpoint.json"
        checkpoint.write_text('{"requires_user": true, "question": "用哪种格式输出？"}')
        upsert_job_record(self.ctx_topic.state_dir, self.asking.message_id, status="waiting_user",
                          checkpoint_path=str(checkpoint), job_id=self.asking.job_id, topic_id=self.asking.topic_id,
                          chat_id="oc_chat", backend="claude")
        self.record = checkpoint_question(job_id=self.asking.job_id, topic_id=self.asking.topic_id,
                                          job_key=self.asking.message_id, question_message_id="om_qcard", card_id="card_q")
        self.path = self.ctx_topic.state_dir / "questions" / (self.record["token"] + ".json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.record, ensure_ascii=False))
        self.painted = []
        self.app.questions.paint_question_state = lambda ctx, record: self.painted.append(record["status"])
        self.enterContext(mock.patch("covey.app.questions.background.start", side_effect=lambda _, fn, *a: fn(*a)))

    def click(self, kind=SET_ASIDE_KIND, event="ev-aside"):
        return self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": event, "message_id": "om_qcard", "operator_id": "ou_user",
            "action_value": {"kind": kind, "topic_id": self.record["topic_id"], "token": self.record["token"]}})

    def saved(self):
        return json.loads(self.path.read_text())

    def waiting(self):
        """How many things wait for the owner, as 手上的事, the 看板 and the web page count them (core/board_read.py waits)."""
        return board_read.waits(self.ctx_topic.state_dir, "oc_chat", 2).count()

    def say(self, mid, text):
        self.receive(mid, text, root="om_ask")
        return next(job for job in self.app.runtime.jobs.queue.queue if job.message_id == mid)

    def test_it_leaves_waiting_and_nothing_is_sent(self):
        self.assertEqual(self.waiting(), 1)
        self.click()
        saved = self.saved()
        self.assertEqual((saved["status"], saved["response_kind"], saved["set_aside_by"]), (SET_ASIDE, SET_ASIDE, "ou_user"))
        self.assertEqual(saved["answers"], {"fmt": {"answers": [SKIPPED]}, "note": {"answers": [SKIPPED]}})
        self.assertIsInstance(saved["answered_at"], float)  # When it stopped waiting (the web page's closed_at).
        self.assertEqual(self.painted, [SET_ASIDE])
        self.assertEqual(self.waiting(), 0)
        self.assertEqual(self.app.questions.pending_questions(self.ctx_topic, "oc_chat"), [])
        self.assertTrue(self.app.runtime.jobs.queue.empty())  # Nothing sent, no turn.
        self.assertEqual(find_job_record(self.ctx_topic.state_dir, self.asking.message_id)["status"], "waiting_user")
        self.click()  # The same click again: nothing changes.
        self.assertEqual(self.painted, [SET_ASIDE])

    def test_undo_opens_the_form_again(self):
        self.click()
        self.click(RESTORE_KIND, "ev-undo")
        saved = self.saved()
        self.assertEqual(saved["status"], "pending")
        for key in ("answers", "response_kind", "set_aside_at", "answered_at"):
            self.assertNotIn(key, saved)
        self.assertEqual(self.painted, [SET_ASIDE, "pending"])
        self.assertEqual(self.waiting(), 1)
        self.assertEqual(len(self.app.questions.pending_questions(self.ctx_topic, "oc_chat")), 1)

    def test_the_topics_next_turn_takes_it_as_its_answer_and_undo_ends(self):
        self.click()
        job = self.say("om_next", "先做别的：把报告发我")
        self.assertTrue(job.prompt_text.startswith(SET_ASIDE_START + "\n用户补充回答：\n用哪种格式输出？\n" + SKIPPED))
        self.assertIn("还有别的要求吗？\n" + SKIPPED, job.prompt_text)
        self.assertTrue(job.prompt_text.endswith("先做别的：把报告发我"))
        self.assertEqual(job.user_text, "先做别的：把报告发我")  # Its card shows what the owner wrote.
        saved = self.saved()
        self.assertEqual((saved["status"], saved["response_kind"], saved["continuation_job_id"]),
                         ("answered", SET_ASIDE, job.job_id))
        self.assertEqual(self.painted, [SET_ASIDE, "answered"])
        self.assertIn(SET_ASIDE_START, self.ctx_topic.history_path.read_text())
        with self.assertRaisesRegex(ValueError, f"随 #{job.job_id} 告诉了模型，撤不回了"):
            self.click(RESTORE_KIND, "ev-late")
        # Once: the turn after it takes nothing.
        self.app.runtime.jobs.unregister(job)
        later = self.say("om_later", "再来")
        self.assertNotIn(SET_ASIDE_START, later.prompt_text or later.user_text)

    def test_the_notes_held_for_it_go_with_that_turn(self):
        self.app.questions.hold_note(self.ctx_topic, "oc_chat", "顺便看下格式", "om_note", [self.record])
        [note] = self.app.questions.notes.held_notes(self.ctx_topic, "oc_chat", "held")
        self.click()
        job = self.say("om_next", "继续")
        self.assertIn("用户另外补充：\n顺便看下格式", job.prompt_text)
        settled = json.loads(self.app.questions.notes.note_path(self.ctx_topic, note["token"]).read_text())
        self.assertEqual((settled["status"], settled["job_id"]), ("attached", job.job_id))

    def test_a_question_settled_or_held_is_not_set_aside(self):
        for fields, refusal in (({"status": "answered", "answers": {}}, SET_ASIDE_SETTLED),
                                ({"native_live_question": True}, SET_ASIDE_HELD),
                                ({"native_paused_question": True}, SET_ASIDE_HELD)):
            with self.subTest(fields):
                self.path.write_text(json.dumps({**self.record, **fields}, ensure_ascii=False))
                with self.assertRaisesRegex(ValueError, refusal):
                    self.click()
                self.assertNotEqual(self.saved()["status"], SET_ASIDE)

    def test_a_stale_form_cannot_answer_it_without_its_undo(self):
        self.click()
        with self.assertRaisesRegex(ValueError, SET_ASIDE_ANSWER):
            self.app.interaction.interaction_action(self.ctx_topic, {
                "chat_id": "oc_chat", "event_id": "ev-answer", "message_id": "om_qcard",
                "action_value": {"kind": "bridge_question", "topic_id": self.record["topic_id"], "token": self.record["token"]},
                "form_value": {"option_0": "0", "answer_1": "没有"}})
        self.assertEqual(self.saved()["status"], SET_ASIDE)

    def test_a_new_binding_cancels_it_with_the_open_questions(self):
        self.click()
        binding.retire_open_questions(SimpleNamespace(runtime=self.app.runtime), self.app, self.ctx_topic, "oc_chat")
        saved = self.saved()
        self.assertEqual((saved["status"], saved["cancel_reason"]), ("cancelled", "switched"))
        self.assertIsNone(update_question(self.path, lambda r: {**r, "status": "pending"}, expected={SET_ASIDE}))

    def test_the_web_page_reads_its_task_as_ended(self):
        questions = [self.record]
        job = find_job_record(self.ctx_topic.state_dir, self.asking.message_id)
        self.assertEqual(Collector.word([job], job, questions), "等你")
        self.click()
        questions = [self.saved()]
        self.assertTrue(board_read.set_aside(job, questions))
        self.assertEqual(Collector.word([job], job, questions), "做完")
        self.assertNotEqual(Collector.tone([job], job, questions, True, None), "waiting")
        item = web_pairing.question(self.saved(), "t" * 64, 1)
        self.assertEqual((item["state"], item["closed_by"], item["turn_key"]), ("answered", "skipped", ""))
        self.assertFalse(board_read.set_aside({**job, "status": "done"}, questions))


if __name__ == "__main__":
    unittest.main()
