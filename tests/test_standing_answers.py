"""以后这类按推荐走 (C2, 2026-11-18): the button under a question card, the principle the owner confirms, the answer."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from covey.app import standing_answers
from covey.app.prompts import ASK_AFTER_PRINCIPLES, MEMORY_RULES, PRINCIPLES_HEAD
from covey.cards import question_cards
from covey.cards.question_cards import (STANDING_CANCEL, STANDING_EDIT, STANDING_FIELD, STANDING_KIND, STANDING_SAVE,
                                        question_card, recommended)
from covey.core import principles
from covey.core.state import read_json, write_json

TOPIC, CHAT, QTOKEN = "a" * 32, "oc_chat", "0123456789abcdef"


def question(**fields):
    return {"token": QTOKEN, "topic_id": TOPIC, "chat_id": CHAT, "job_id": 41, "status": "pending", "blocking": True,
            "questions": [{"id": "q1", "header": "合并", "question": "测试过了，要合并进 main 并推送吗？",
                           "options": [{"label": "合并并推送（推荐）"}, {"label": "先不合并"}]}], **fields}


class CardTest(unittest.TestCase):
    def test_the_button_is_offered_only_where_every_question_has_one_recommended_option(self):
        self.assertEqual(recommended(question()["questions"]), [(0, 0)])
        two = [{"question": "x", "options": [{"label": "A (Recommended)"}, {"label": "B（推荐）"}]}]
        none = [{"question": "x", "options": [{"label": "A"}, {"label": "B"}]}]
        free = [{"question": "x"}]
        multiple = [{"question": "x", "multiple": True, "options": [{"label": "A（推荐）"}, {"label": "B"}]}]
        for questions in (two, none, free, multiple, []):
            self.assertIsNone(recommended(questions), questions)
        text = json.dumps(question_card(question()), ensure_ascii=False)
        self.assertIn(STANDING_KIND, text)
        form = question_card(question())["body"]["elements"][0]
        self.assertNotIn(STANDING_KIND, json.dumps(form))  # Outside the form: it submits nothing.
        self.assertNotIn(STANDING_KIND, json.dumps(question_card(question(questions=none)), ensure_ascii=False))
        self.assertNotIn(STANDING_KIND, json.dumps(question_card(question(status="answered")), ensure_ascii=False))
        with mock.patch.dict(os.environ, {question_cards.STANDING_ENV: "0"}):
            self.assertNotIn(STANDING_KIND, json.dumps(question_card(question()), ensure_ascii=False))

    def test_the_rules_say_a_principle_s_go_ahead_is_not_asked_again(self):
        self.assertIn(ASK_AFTER_PRINCIPLES, PRINCIPLES_HEAD)
        self.assertTrue(any("不要再问" in rule for rule in MEMORY_RULES))


class FlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        write_json(self.state / "questions" / (QTOKEN + ".json"), question())
        self.messenger = mock.Mock()
        self.messenger.create_and_send_card.return_value = ("card_s", "om_standing")
        self.messenger.put_card.return_value = {"ok": True}
        self.app = SimpleNamespace(messenger=self.messenger, voice=None, key=lambda *parts: "-".join(parts),
                                   questions=mock.Mock())
        self.target = SimpleNamespace(topic_id=TOPIC, workspace_name="covey", reply_to_message_id="om_root",
                                      state_dir=self.state, profile="p")
        runtime = SimpleNamespace(card_queues=[], log=mock.Mock(), shutdown=threading.Event())
        self.topics = SimpleNamespace(runtime=runtime, messenger=self.messenger, apps={},
                                      target=lambda state, topic, chat: (self.app, self.target, {}) if topic == TOPIC else None)
        self.standing = standing_answers.StandingAnswers(self.topics)
        self.standing.submit = lambda busy, handler, *args: handler(*args)  # On this thread.
        self.ctx = SimpleNamespace(state_dir=self.state, profile="p")

    def click(self, kind, token, **extra):
        event = {"chat_id": CHAT, "event_id": "ev-" + kind, "message_id": extra.pop("message_id", "om_q"),
                 "action_value": {"kind": kind, "token": token, "topic_id": TOPIC}, **extra}
        return self.standing.claim(self.ctx, event, event["action_value"], True)

    def opened(self):
        self.assertEqual(self.click(STANDING_KIND, QTOKEN), "standing_answer")
        path = next((self.state / "standing-forms").glob("*.json"))
        return path, read_json(path)

    def test_only_the_owner_s_click_counts(self):
        event = {"chat_id": CHAT, "action_value": {"kind": STANDING_KIND, "token": QTOKEN, "topic_id": TOPIC}}
        self.assertEqual(self.standing.claim(self.ctx, event, event["action_value"], False), "owner_only")
        self.assertIsNone(self.standing.claim(self.ctx, event, {"kind": "bridge_question"}, True))
        # A topic that no longer routes (its Harness retired): topic routing says so, not this.
        other = {"chat_id": CHAT, "action_value": {"kind": STANDING_KIND, "token": QTOKEN, "topic_id": "b" * 32}}
        self.assertIsNone(self.standing.claim(self.ctx, other, other["action_value"], True))

    def test_the_sentence_is_shown_first_and_only_saved_with_the_answer_on_its_record(self):
        path, form = self.opened()
        self.assertEqual(form["status"], "ready")
        self.assertTrue(form["text"].startswith("covey 工作区："), form["text"])  # No Claude here: the fallback sentence.
        sent = json.dumps(self.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False)
        for label in ("记下", "改一下", "算了", form["text"]):
            self.assertIn(label, sent)
        self.assertEqual(principles.entries(self.state), [])
        self.app.questions.submit_answer.assert_not_called()
        self.assertEqual(self.click(STANDING_KIND, QTOKEN), "standing_answer")  # A second click: still one card.
        self.assertEqual(self.messenger.create_and_send_card.call_count, 1)
        self.click(STANDING_EDIT, form["token"], message_id="om_standing")
        edited = read_json(path)
        self.assertEqual(edited["status"], "editing")
        self.assertIn('"default_value"', json.dumps(self.messenger.put_card.call_args.args[2]))
        typed = "covey 工作区：测试通过就合并进 main 并推送；没有在跑的任务时直接部署，不用再问"
        self.click(STANDING_SAVE, form["token"], message_id="om_standing", form_value={STANDING_FIELD: typed})
        saved = read_json(path)
        self.assertEqual((saved["status"], saved["answered"]), ("saved", True))
        rows = principles.entries(self.state)
        self.assertEqual([(row["scope"], row["workspace"], row["text"]) for row in rows], [("workspace", "covey", typed)])
        _, chat, token, fields, key = self.app.questions.submit_answer.call_args.args
        self.assertEqual((chat, token, key), (CHAT, QTOKEN, "standing-" + form["token"]))
        self.assertEqual(fields["option_0"], "0")  # The recommended option, as the form would send it.
        self.assertIn("已记下", json.dumps(self.messenger.put_card.call_args.args[2], ensure_ascii=False))
        self.click(STANDING_SAVE, form["token"], message_id="om_standing")  # Saved already: nothing more.
        self.assertEqual(self.app.questions.submit_answer.call_count, 1)

    def test_cancel_records_nothing_and_a_click_on_another_card_is_dropped(self):
        path, form = self.opened()
        self.click(STANDING_SAVE, form["token"], message_id="om_other")
        self.assertEqual(read_json(path)["status"], "ready")
        self.click(STANDING_CANCEL, form["token"], message_id="om_standing")
        self.assertEqual(read_json(path)["status"], "cancelled")
        self.assertEqual(principles.entries(self.state), [])
        self.app.questions.submit_answer.assert_not_called()

    def test_an_answered_question_keeps_the_principle_and_is_not_answered_again(self):
        path, form = self.opened()
        write_json(self.state / "questions" / (QTOKEN + ".json"), question(status="answered"))
        self.click(STANDING_SAVE, form["token"], message_id="om_standing")
        self.assertEqual((read_json(path)["status"], read_json(path)["answered"]), ("saved", False))
        self.assertEqual(len(principles.entries(self.state)), 1)
        self.app.questions.submit_answer.assert_not_called()

    def test_a_question_that_cannot_be_offered_gets_one_line_why(self):
        write_json(self.state / "questions" / (QTOKEN + ".json"), question(status="answered"))
        self.click(STANDING_KIND, QTOKEN)
        self.assertIn("已经答过", self.messenger.reply.call_args.args[2])
        self.assertFalse((self.state / "standing-forms").exists())


class DraftTest(unittest.TestCase):
    def test_the_small_model_s_sentence_else_the_fallback(self):
        record = question()
        picks = standing_answers.picks_of(record)
        backend = SimpleNamespace(wrap_argv=lambda argv, ctx: argv, child_env=lambda ctx: {})
        ctx = SimpleNamespace(backend_cmd=["claude"])
        with tempfile.TemporaryDirectory() as tmp:
            answer = json.dumps({"result": '{"principle":"covey 工作区：测试通过就合并进 main 并推送，不用再问"}'})
            with mock.patch.object(standing_answers.track_judge, "run", return_value=answer) as run:
                text = standing_answers.draft(tmp, TOPIC, "covey", record, picks, backend=backend, ctx=ctx)
            self.assertEqual(text, "covey 工作区：测试通过就合并进 main 并推送，不用再问")
            sent = json.loads(run.call_args.args[1])
            self.assertEqual(sent["questions"][0]["recommended"], "合并并推送")
            with mock.patch.object(standing_answers.redaction, "topic_redaction", return_value=True), \
                    mock.patch.object(standing_answers.track_judge, "run") as run:
                text = standing_answers.draft(tmp, TOPIC, "covey", record, picks, backend=backend, ctx=ctx)
            run.assert_not_called()  # A topic that redacts sends the model nothing.
            self.assertEqual(text, "covey 工作区：遇到“合并”这类问题，直接按推荐的“合并并推送”做，不用再问")
            with mock.patch.object(standing_answers.track_judge, "run", return_value="not json"):
                self.assertTrue(standing_answers.draft(tmp, TOPIC, "covey", record, picks, backend=backend,
                                                       ctx=ctx).startswith("covey 工作区：遇到"))


if __name__ == "__main__":
    unittest.main()
