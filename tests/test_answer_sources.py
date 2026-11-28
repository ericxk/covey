"""WAVE12 W1-B: who answered a question and whose words a turn carries (契约 C1, C2).

- The Bridge stamps the hand-ons and the continuations of answers (core/turn_source.py ``stamp``: ``relay``, ``lead``,
  ``answer``, ``answer_for``), with one switch back (``STAMPING``).
- Every answer says who gave it (app/answers.py AnswerBy): the question record keeps it (``answer_by``, in the write that
  turns it ``submitting``), a task runner gets it beside the open_id an older one reads, the continuation is stamped from
  it, and only the owner's own (or the butler's on a message of his) is noted as his decision.
- A message of the butler's family answers a question there only when it quotes the question's card (C2's third
  condition, the 1eb6728490 swallow); the words an agent sends are held, never an answer, and say whose they are when they
  go with the owner's answer (P1-6); a question held for the lead keeps them too (P1-7).
- 代发 drafts are offered only to a turn ``owner_send.allowed`` lets draft (RT-2).
"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.world import QuestionFixture, TopicFixture

from covey.app import answers, provenance, runner_ipc
from covey.app.answers import (AnswerBy, RELAYED, RELAYED_HEADS, answer_message, answer_parts, by_fields,
                               continuation_source, from_fields)
from covey.core import turn_source
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.store import update_question

TOKEN = "c" * 16
OWNER = "ou_user"


def checkpoint_question(**fields):
    """A checkpoint's question of one choice, as tests/test_question_submit.py draws one."""
    return {"token": TOKEN, "job_id": 5, "topic_id": "t" * 64, "chat_id": "oc_chat", "status": "pending",
            "blocking": True, "request_id": "checkpoint-om_ask", "created_at": 1,
            "questions": [{"id": "fmt", "header": "格式", "question": "用哪种格式输出？",
                           "options": [{"label": "表格"}, {"label": "列表"}]}], **fields}


class StampTest(unittest.TestCase):
    def test_each_kind_its_party_and_the_one_switch_back(self):
        made = {kind: turn_source.stamp(kind) for kind in turn_source.STAMP_BY}
        self.assertEqual({kind: field["asserted_by"] for kind, field in made.items()},
                         {"relay": "butler", "lead": "lead", "wake": "bridge", "answer": "owner", "answer_for": "lead"})
        self.assertTrue(all(field["recorded_by"] == "bridge" and field["v"] == turn_source.VERSION for field in made.values()))
        his = turn_source.stamp(turn_source.RELAY, asserted_by="owner", origin={"message_id": "om_1"}, via="butler")
        self.assertEqual((his["level"], his["origin"], his["via"]), ("owner", {"message_id": "om_1"}, "butler"))
        self.assertEqual(turn_source.level({"turn_source": his}), "owner")
        self.assertEqual(turn_source.level({"turn_source": made["relay"]}), "agent")
        for wrong in ({"kind_": "owner"}, {"kind_": "collab_ask"}, {"kind_": "relay", "via": "mail"},
                      {"kind_": "relay", "asserted_by": "someone"}):
            with self.subTest(wrong), self.assertRaises(ValueError):
                turn_source.stamp(wrong.pop("kind_"), **wrong)
        with mock.patch.object(turn_source, "STAMPING", False):
            self.assertEqual({kind: turn_source.stamp(kind) for kind in turn_source.STAMP_BY},
                             dict.fromkeys(turn_source.STAMP_BY))

    def test_a_stamped_job_runs_and_does_as_its_row_says(self):
        rows = {
            "relay": (False, False, True, False, True, True), "lead": (False, False, True, False, True, True),
            "wake": (False, False, False, False, True, False), "answer": (True, True, False, False, True, True),
            "answer_for": (False, True, False, False, True, False)}
        for kind, row in rows.items():
            job = {"turn_source": turn_source.stamp(kind)}
            with self.subTest(kind):
                self.assertEqual(turn_source.refusal(job), "")
                behaviour = turn_source.behavior(job)
                self.assertEqual((behaviour.owner_words, behaviour.answers_questions, behaviour.steers, behaviour.read_only,
                                  behaviour.own_delivery, behaviour.owner_send), row)
                self.assertEqual(provenance.synthetic(job), kind != "answer")


class AnswerByTest(unittest.TestCase):
    def test_what_is_kept_of_who_and_read_back(self):
        by = AnswerBy("butler", "tool", open_id=OWNER, message_id="om_9")
        self.assertEqual(by_fields(by), {"party": "butler", "channel": "tool", "message_id": "om_9"})
        self.assertEqual(from_fields(by_fields(by), OWNER), by)
        self.assertEqual(by_fields(OWNER), {})  # An open_id alone: no party known, nothing kept.
        self.assertEqual(from_fields(None, OWNER), AnswerBy("", "", open_id=OWNER))  # A Bridge of before W1-B.
        self.assertEqual(from_fields({"party": "member"}, OWNER), AnswerBy("", "", open_id=OWNER))
        self.assertIsNone(from_fields(None, None))
        self.assertEqual(answers.via_of(AnswerBy("owner", "card")), "card")
        self.assertEqual(answers.via_of(AnswerBy("lead", "tool")), "lead")
        self.assertEqual(answers.via_of(OWNER), "")

    def test_the_continuation_of_each_answer_is_stamped_from_who_gave_it(self):
        def made(**by):
            return continuation_source({"token": TOKEN, "answer_by": by})
        card = made(party="owner", channel="card")
        self.assertEqual((card["kind"], card["asserted_by"], card["origin"], card["via"], card["level"]),
                         ("answer", "owner", {"question": TOKEN}, "card", "owner"))
        message = made(party="owner", channel="message", message_id="om_5")
        self.assertEqual((message["origin"], message["via"]), ({"question": TOKEN, "message_id": "om_5"}, "message"))
        butler = made(party="butler", channel="tool", message_id="om_6")
        self.assertEqual((butler["kind"], butler["asserted_by"], butler["origin"], butler["via"]),
                         ("answer", "owner", {"question": TOKEN, "message_id": "om_6"}, "butler"))
        lead = made(party="lead", channel="tool", message_id="lead-7")
        self.assertEqual((lead["kind"], lead["asserted_by"], lead["origin"], lead["via"], lead["level"]),
                         ("answer_for", "lead", {"question": TOKEN}, "lead", "agent"))
        self.assertIsNone(made(party="butler", channel="tool"))  # The butler answers only on his message.
        self.assertIsNone(made())
        self.assertIsNone(continuation_source({"token": TOKEN}))  # Of no known party: LEGACY, as before.
        self.assertIsNone(continuation_source({"answer_by": {"party": "owner"}}))

    def test_the_bridge_takes_only_an_answers_source_for_its_question_from_a_runner(self):
        good = {"message_id": "answer-" + TOKEN, "turn_source": turn_source.stamp("answer", origin={"question": TOKEN})}
        self.assertEqual(runner_ipc.continuation_source(good), good["turn_source"])
        lead = {"message_id": "answer-" + TOKEN, "turn_source": turn_source.stamp("answer_for", origin={"question": TOKEN})}
        self.assertEqual(runner_ipc.continuation_source(lead)["kind"], "answer_for")
        for name, payload in {
            "none (a runner of before W1-B)": {"message_id": "answer-" + TOKEN},
            "another kind": {"message_id": "answer-" + TOKEN, "turn_source": turn_source.stamp("relay")},
            "another question": {"message_id": "answer-" + "d" * 16, "turn_source": good["turn_source"]},
            "a newer version": {"message_id": "answer-" + TOKEN, "turn_source": {**good["turn_source"], "v": 9}},
            "not an object": {"message_id": "answer-" + TOKEN, "turn_source": "answer"},
        }.items():
            with self.subTest(name):
                self.assertIsNone(runner_ipc.continuation_source(payload))


class OwnerSubmittedTest(unittest.TestCase):
    def setUp(self):
        from covey.app.questions import Questions

        access = SimpleNamespace(is_owner=lambda open_id: open_id == OWNER)
        self.questions = Questions.__new__(Questions)
        self.questions.runtime = SimpleNamespace(access=access)

    def test_only_his_own_answer_or_the_butlers_on_his_message_is_his_decision(self):
        cases = [
            (AnswerBy("owner", "card", open_id=OWNER), True, True),
            (AnswerBy("owner", "web", open_id=OWNER, message_id="web-op-1"), True, True),
            (AnswerBy("owner", "message", open_id=OWNER, message_id="om_1"), True, False),  # His; no rule of the card's.
            (AnswerBy("", "card", open_id="ou_member"), False, True),  # A group member's click: not his.
            (AnswerBy("butler", "tool", open_id=OWNER, message_id="om_2"), True, False),
            (AnswerBy("butler", "tool", open_id=OWNER), False, False),
            (AnswerBy("lead", "tool", message_id="lead-3"), False, False),
            (OWNER, True, True),  # An older runner's open_id: as W0-1.
            (None, False, False),
        ]
        for by, his, rule in cases:
            with self.subTest(by=by):
                self.assertEqual(self.questions.owner_submitted(by), his)
                self.assertEqual(self.questions.owner_submitted(by) and self.questions.owner_clicked(by), his and rule)


class AnswerRecordTest(TopicFixture, unittest.TestCase):
    """A checkpoint's question whose turn ended, answered: the record says who, the continuation carries it."""

    def setUp(self):
        super().setUp()
        self.receive("om_ask", "整理数据")
        self.asking = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(self.asking)
        self.ctx_topic = self.topic("om_ask")
        checkpoint = self.root / "asking-checkpoint.json"
        checkpoint.write_text(json.dumps({"phase": "等你定格式", "requires_user": True}, ensure_ascii=False))
        upsert_job_record(self.ctx_topic.state_dir, self.asking.message_id, status="waiting_user",
                          checkpoint_path=str(checkpoint), job_id=self.asking.job_id,
                          topic_id=self.asking.topic_id, chat_id="oc_chat", backend="claude")
        self.record = checkpoint_question(job_id=self.asking.job_id, topic_id=self.asking.topic_id,
                                          question_message_id="om_qcard", card_id="card_q", job_key=self.asking.message_id)
        self.path = self.ctx_topic.state_dir / "questions" / (TOKEN + ".json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.record, ensure_ascii=False))
        self.app.questions.paint_question_state = mock.Mock()
        self.app.questions.show_answer_notice = mock.Mock()
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        self.noted = []
        patcher = mock.patch("covey.app.project_context.remember_answer",
                             side_effect=lambda *args, **kwargs: self.noted.append(kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads(self.path.read_text())

    def follow_up(self):
        return [job for job in self.app.runtime.jobs.queue.queue if job.message_id == "answer-" + TOKEN]

    def submit(self, by):
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", TOKEN, {"option_0": "1"}, "ev-1", by=by)

    def test_his_click_is_kept_noted_and_carried_on_as_his_answer(self):
        self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": "ev-click", "message_id": "om_qcard", "operator_id": OWNER,
            "action_value": {"kind": "bridge_question", "topic_id": self.record["topic_id"], "token": TOKEN},
            "form_value": {"option_0": "1"}})
        self.assertEqual(self.saved()["answer_by"], {"party": "owner", "channel": "card"})
        self.assertEqual(self.noted[-1]["owners"], True)
        self.assertEqual(self.noted[-1]["settles"], True)
        self.assertEqual(self.noted[-1]["origin"], {"question": TOKEN, "via": "card"})
        [job] = self.follow_up()
        self.assertEqual((job.turn_source["kind"], job.turn_source["origin"], job.turn_source["via"]),
                         ("answer", {"question": TOKEN}, "card"))
        self.assertEqual(find_job_record(self.ctx_topic.state_dir, job.message_id)["turn_source"]["kind"], "answer")

    def test_a_leads_answer_is_never_his_and_goes_on_as_answer_for(self):
        self.submit(AnswerBy("lead", "tool", message_id="lead-r1"))
        self.assertEqual(self.saved()["answer_by"], {"party": "lead", "channel": "tool", "message_id": "lead-r1"})
        self.assertEqual((self.noted[-1]["owners"], self.noted[-1]["settles"]), (False, False))
        [job] = self.follow_up()
        self.assertEqual(job.turn_source["kind"], "answer_for")
        self.assertFalse(turn_source.behavior(job).owner_words)  # Its "以后…" is no memory of his.
        self.assertFalse(provenance.owner_said(job, self.app.runtime.access))

    def test_the_butlers_answer_on_his_message_is_his_with_its_message_but_writes_no_rule(self):
        self.submit(AnswerBy("butler", "tool", open_id=OWNER, message_id="om_said"))
        self.assertEqual((self.noted[-1]["owners"], self.noted[-1]["settles"]), (True, False))
        self.assertEqual(self.noted[-1]["origin"], {"question": TOKEN, "message_id": "om_said", "via": "butler"})
        [job] = self.follow_up()
        self.assertEqual((job.turn_source["kind"], job.turn_source["origin"]),
                         ("answer", {"question": TOKEN, "message_id": "om_said"}))

    def test_an_open_id_alone_goes_on_as_before(self):
        self.submit(OWNER)
        self.assertNotIn("answer_by", self.saved())
        self.assertEqual(self.noted[-1]["owners"], True)  # W0-1: his open_id.
        [job] = self.follow_up()
        self.assertFalse(job.turn_source)  # LEGACY.

    def test_a_relayed_turn_leaves_the_task_waiting_on_its_question(self):
        # WAVE12 C1: app/task_controls.py acknowledge_waiting_answer asks answers_questions (a hand-on answers nothing).
        from covey.app import task_controls

        job = SimpleNamespace(message_id="main-x", chat_id="oc_chat", topic_id=self.asking.topic_id, recovery_context=None,
                              turn_source=turn_source.stamp("relay"))
        controls = self.app.controls
        with mock.patch.object(controls.questions, "take_set_aside"):
            self.assertIsNone(task_controls.TaskControls.acknowledge_waiting_answer(controls, self.ctx_topic, job))
        self.assertEqual(find_job_record(self.ctx_topic.state_dir, self.asking.message_id)["status"], "waiting_user")


class HeldNoteSourceTest(unittest.TestCase):
    """P1-6: the words the butler or a lead sent a waiting topic go with the owner's answer as theirs, never as his."""

    RECORD = {"questions": [{"id": "q", "question": "范围？"}], "supplement": ""}

    def test_each_note_under_whose_it_is(self):
        notes = [{"text": "他自己补充的"}, {"text": "管家转来的话", "by": "butler"}, {"text": "负责人转来的话", "by": "lead"}]
        shown, text, delivered = answer_message(self.RECORD, {"q": {"answers": ["全部"]}}, "", notes)
        self.assertIn("用户另外补充：\n他自己补充的", text)
        self.assertIn(RELAYED_HEADS["butler"] + answers.RELAYED_NOT_HIS + "：\n管家转来的话", text)
        self.assertIn(RELAYED_HEADS["lead"] + answers.RELAYED_NOT_HIS + "：\n负责人转来的话", text)
        self.assertNotIn("用户另外补充：\n管家转来的话", text)
        riders = delivered["q"]["answers"]
        self.assertTrue(any(rider.startswith(RELAYED_HEADS["butler"]) for rider in riders))
        parts = answer_parts(shown, ["范围？"])
        self.assertIn(("note", "他自己补充的"), parts)
        self.assertIn((RELAYED, "管家转来的话"), parts)
        self.assertIn((RELAYED, "负责人转来的话"), parts)

    def test_the_owners_words_of_an_answer_leave_relayed_notes_out(self):
        from covey.app.memory_turn import owner_words

        shown, _, _ = answer_message(self.RECORD, {"q": {"answers": ["全部"]}}, "",
                                     [{"text": "以后都直接部署", "by": "butler"}, {"text": "顺便看日志"}])
        words = owner_words({"user_text": shown})
        self.assertIn("顺便看日志", words)
        self.assertNotIn("以后都直接部署", words)

    def test_an_answer_without_relayed_notes_reads_as_before(self):
        shown, text, _ = answer_message(self.RECORD, {"q": {"answers": ["全部"]}}, "", [{"text": "顺便看日志"}])
        self.assertEqual(text, "用户补充回答：\n范围？\n全部\n\n用户另外补充：\n顺便看日志")


class TopicWordsTest(QuestionFixture, unittest.TestCase):
    """C2's conditions in a topic: an agent's words are held (and say whose), the owner's answer the one shown question."""

    def setUp(self):
        super().setUp()
        self.backend.declare(questions=True, steering=True)

    def notes(self):
        return [json.loads(path.read_text()) for path in (self.ctx_topic.state_dir / "held-notes").glob("*.json")]

    def test_a_hand_on_is_held_with_its_source_never_the_answer(self):
        for kind, by in (("relay", "butler"), ("lead", "lead")):
            with self.subTest(kind), mock.patch.object(self.app.questions, "submit_answer") as submit:
                taken = self.app.interaction.interactive_command(self.ctx_topic, "选第二个", "oc_chat", f"main-{kind}",
                                                                 source=turn_source.stamp(kind))
                self.assertTrue(taken)
                submit.assert_not_called()
        self.assertEqual(sorted(note["by"] for note in self.notes()), ["butler", "lead"])
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")

    def test_his_answer_stamp_is_no_hand_on(self):
        with mock.patch.object(self.app.questions, "submit_answer") as submit:
            self.app.interaction.interactive_command(self.ctx_topic, "全部", "oc_chat", "om_a",
                                                     by=OWNER, source=turn_source.stamp("answer", origin={"question": "x"}))
        submit.assert_called_once()

    def test_a_question_held_for_the_lead_keeps_a_leads_words_for_its_answer(self):
        # P1-7 (Neon F1): the words were refused as a card not delivered ("请先…重新投递") and lost.
        from covey.app import triage as triage_flow

        self.path.write_text(json.dumps({**self.record, "display_pending": True,
                                         "triage": {"state": "held", "at": 1, "deadline": 9e9, "project": "p1"}}))
        self.app.messenger.reply = mock.Mock()
        with mock.patch.object(triage_flow, "ENV", SimpleNamespace(state_dir=self.ctx_topic.state_dir)), \
                mock.patch.object(triage_flow, "owner_spoke") as spoke:
            taken = self.app.interaction.interactive_command(self.ctx_topic, "补一句背景", "oc_chat", "main-lead",
                                                             source=turn_source.stamp("lead"))
        self.assertTrue(taken)
        spoke.assert_not_called()  # An agent's words: the owner did not speak.
        [note] = self.notes()
        self.assertEqual((note["text"], note["by"], note["question_tokens"]), ("补一句背景", "lead", [self.token]))
        said = " ".join(str(call.args[2]) for call in self.app.messenger.reply.call_args_list)
        self.assertNotIn("重新投递", said)

    def test_his_word_that_releases_a_held_question_is_held_not_its_answer(self):
        # C2's second condition: the question goes to him now, and he has not seen it yet.
        from covey.app import triage as triage_flow

        held = {**self.record, "display_pending": True, "triage": {"state": "held", "at": 1, "deadline": 9e9}}
        self.path.write_text(json.dumps(held))

        def released(env, topic, chat):  # As triage.escalate writes it (the store's own write: its cache sees it).
            update_question(self.path, lambda row: {**row, "display_pending": False,
                                                    "triage": {"state": "escalated", "at": 2}})
        with mock.patch.object(triage_flow, "ENV", SimpleNamespace()), \
                mock.patch.object(triage_flow, "owner_spoke", side_effect=released), \
                mock.patch.object(self.app.questions, "submit_answer") as submit:
            taken = self.app.interaction.interactive_command(self.ctx_topic, "全部", "oc_chat", "om_owner", by=OWNER)
        self.assertTrue(taken)
        submit.assert_not_called()
        [note] = self.notes()
        self.assertNotIn("by", note)  # His own words.


class SourcesMatrixTest(QuestionFixture, unittest.TestCase):
    """§5.2 W1-B 必过 3: four sources (the owner, the butler, a lead, a group member) × a question waiting or none."""

    def setUp(self):
        super().setUp()
        self.backend.declare(questions=True, steering=True)

    def outcome(self, mid, by=None, source=None):
        with mock.patch.object(self.app.questions, "submit_answer") as submit, \
                mock.patch.object(self.app.questions, "hold_note") as hold:
            taken = self.app.interaction.interactive_command(self.ctx_topic, "全部", "oc_chat", mid, by=by,
                                                             **({"source": source} if source else {}))
        if submit.called:
            return "answer", submit.call_args.kwargs["by"]
        if hold.called:
            return "held", hold.call_args.kwargs.get("by", "")
        return ("refused" if taken else "turn"), None

    def test_who_may_answer_a_waiting_question_and_who_is_held(self):
        sources = {
            "owner": ({"mid": "om_o", "by": OWNER}, ("answer", "owner")),
            "group member": ({"mid": "om_m", "by": "ou_member"}, ("answer", "")),  # His own topic: an answer, not the owner's.
            "butler": ({"mid": "main-b", "source": turn_source.stamp("relay")}, ("held", "butler")),
            "lead": ({"mid": "main-l", "source": turn_source.stamp("lead")}, ("held", "lead")),
        }
        for name, (call, (kind, party)) in sources.items():
            with self.subTest(name, waiting=True):
                found, by = self.outcome(**call)
                self.assertEqual(found, kind)
                self.assertEqual(by.party if kind == "answer" else by, party)
        update_question(self.path, lambda row: {**row, "status": "cancelled"})  # The store's own write: its cache sees it.
        for name, (call, _) in sources.items():
            with self.subTest(name, waiting=False):
                self.assertEqual(self.outcome(**call)[0], "turn")  # No question waits: a turn of its own, for every source.


class GroundsTest(unittest.TestCase):
    """§5.2 W1-B 必过 8: the lead's grounds take the owner's words the butler handed it (``relayed``), and a current decision
    of another question is no ground of his for this one (MUTUAL_DUP)."""

    def setUp(self):
        import tempfile

        from covey.core.butler import butler_topic_id

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        upsert_job_record(self.state, "om_b", job_id=3, message_id="om_b", topic_id=butler_topic_id("oc_owner"),
                          chat_id="oc_owner", user_text="旧库先别动，用新版", sender_id=OWNER)
        access = SimpleNamespace(owner_turn=lambda chat, sender: sender == OWNER)
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(access=access))
        self.project = {"id": "p1", "chat_id": "oc_owner", "lead_topic_id": "l" * 64, "threads": ["t" * 64],
                        "relayed": [{"message_id": "om_b", "text": "旧库先别动，用新版", "at": 1, "job": "main-x", "steer": False}]}

    def test_the_owners_words_the_butler_handed_on_are_a_ground(self):
        from covey.app import triage as flow

        self.assertEqual(flow.owner_message(self.env, self.project, "om_b")["user_text"], "旧库先别动，用新版")
        self.assertEqual(flow.owner_message(self.env, {**self.project, "relayed": []}, "om_b"), {})  # Not handed on.
        found = flow.grounds(self.env, self.project, ["msg:om_b"], asked="用哪个库版本？")
        self.assertEqual((found[0]["kind"], found[0]["level"]), ("message", "owner"))
        from covey.app.project_lead import owner_word_lines

        with mock.patch("covey.app.project_lead.topic_job_records", return_value=[]):
            self.assertEqual(len(owner_word_lines(self.state, self.project)), 1)
            self.assertIn("msg:om_b", owner_word_lines(self.state, self.project)[0])
            self.assertIn("（管家转来）", owner_word_lines(self.state, self.project)[0])

    def test_a_decision_of_another_question_is_no_ground_alone(self):
        from covey.app import triage as flow

        row = {"id": "d1", "kind": "decision", "question": "部署到哪个机房？", "text": "主人答：部署到哪个机房？→北京"}
        facts = {"valid": "active", "layer": "decision", "applies": "", "level": "owner", "part": "decision"}
        with mock.patch.object(flow, "ground_rows", return_value={"d1": row}), \
                mock.patch.object(flow.core_memory, "describe", return_value=facts):
            with mock.patch("covey.core.memory_search.mutual_similarity", return_value=(0.05, False)):
                with self.assertRaisesRegex(ValueError, "另一道题"):
                    flow.grounds(self.env, self.project, ["d1"], asked="用哪个库版本？")
            with mock.patch("covey.core.memory_search.mutual_similarity", return_value=(0.6, True)):
                self.assertEqual(flow.grounds(self.env, self.project, ["d1"], asked="部署到哪个机房？")[0]["level"], "owner")
            # Not measured (no question asked, or the measure fails): as before.
            self.assertEqual(flow.grounds(self.env, self.project, ["d1"])[0]["level"], "owner")


class TopLevelAnswerTest(QuestionFixture, unittest.TestCase):
    """C2's third condition, the 1eb6728490 swallow: in the butler's family a message answers its question only by quoting
    the question's card (its own, its copy at the top level, or the card of the task that asks)."""

    def setUp(self):
        super().setUp()
        self.backend.declare(questions=True, steering=True)
        self.path.write_text(json.dumps({**self.record, "question_message_id": "om_qcard", "mirror_message_id": "om_qtop",
                                         "at_top": True}))
        from covey.core import butler as contract

        patcher = mock.patch("covey.app.interaction.butler_contract.traits", return_value=contract.BUTLER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_request_quoting_nothing_is_a_turn_of_its_own(self):
        with mock.patch.object(self.app.questions, "submit_answer") as submit:
            taken = self.app.interaction.interactive_command(
                self.ctx_topic, "我看地域和严肃群里有不少人使用 herdr, orca 等方案，你帮我看看还有没有其他方案", "oc_chat", "om_new",
                by=OWNER)
        self.assertFalse(taken)
        submit.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
        self.assertFalse((self.ctx_topic.state_dir / "held-notes").exists())

    def test_quoting_its_card_or_its_copy_answers_it(self):
        for quoted in ("om_qcard", "om_qtop"):
            with self.subTest(quoted), mock.patch.object(self.app.questions, "submit_answer") as submit:
                self.assertTrue(self.app.interaction.interactive_command(self.ctx_topic, "全部", "oc_chat", "om_" + quoted,
                                                                         by=OWNER, quoted=quoted))
                self.assertEqual(submit.call_args.args[2], self.token)
                self.assertEqual(submit.call_args.kwargs["by"].channel, "message")

    def test_quoting_another_message_is_no_answer(self):
        with mock.patch.object(self.app.questions, "submit_answer") as submit:
            self.assertFalse(self.app.interaction.interactive_command(self.ctx_topic, "全部", "oc_chat", "om_x", by=OWNER,
                                                                      quoted="om_somewhere_else"))
        submit.assert_not_called()


class TracksTest(unittest.TestCase):
    """多路并行: the butler's own slot waiting alone is a live track, and only a quote of its card is its answer."""

    def test_the_butlers_slot_waiting_alone_is_seen(self):
        import tempfile

        from covey.app import butler_tracks
        from covey.core import butler_tracks as core

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            chat = "oc_owner"
            upsert_job_record(state, "om_ask", job_id=3, topic_id=core.family_ids(chat)[0], chat_id=chat,
                              status="waiting_user", user_text="HarnessBridge 整体重构放到哪个话题区做？")
            runtime = SimpleNamespace(jobs=SimpleNamespace(topic_jobs=lambda topic, chat: []))
            entries = butler_tracks.live_entries(runtime, state, chat, __import__("time").time())
        self.assertEqual([(entry.live.track, entry.live.phase) for entry in entries], [(0, "paused")])

    def test_an_unquoted_answer_to_the_waiting_track_starts_a_track_of_its_own(self):
        from covey.app.butler_tracks import unasked
        from covey.core import track_route as route

        self.assertEqual(unasked(route.Decision(route.MERGE, 0, "6", "3", answers=True)).action, route.NEW)
        self.assertEqual(unasked(route.Decision(route.MERGE, 0, "judge", "3", answers=True)).action, route.NEW)
        kept = route.Decision(route.MERGE, 0, "4", "3", answers=True)
        self.assertIs(unasked(kept), kept)  # It quotes the track's card.
        supplement = route.Decision(route.MERGE, 1, "8", "4")
        self.assertIs(unasked(supplement), supplement)


class OwnerSendAllowedTest(unittest.TestCase):
    """RT-2: a 代发 draft only from a turn of the owner's own words."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = SimpleNamespace(state_dir=Path(self.tmp.name), topic_id="t" * 64, history_path=Path(self.tmp.name) / "h")
        self.access = SimpleNamespace(owner_turn=lambda chat, sender: sender == OWNER if sender else chat == "oc_owner")

    def allowed(self, **job):
        from covey.app.owner_send import allowed

        return allowed({"chat_id": "oc_owner", "topic_id": "t" * 64, "sender_id": OWNER, **job}, self.ctx, self.access)

    def test_who_may_draft(self):
        cases = {
            "his message": ({}, ""),
            "his answer's continuation": ({"turn_source": turn_source.stamp("answer", origin={"question": TOKEN})}, ""),
            "the butler's hand-on of his message": (
                {"sender_id": "", "turn_source": turn_source.stamp("relay", asserted_by="owner", origin={"message_id": "om_1"})}, ""),
            "a group member's turn": ({"sender_id": "ou_member", "chat_id": "oc_group"}, "not-owner"),
            "a lead's work": ({"turn_source": turn_source.stamp("lead", origin={"topic": "l"})}, "not-his"),
            "the butler's hand-on of nothing of his": ({"turn_source": turn_source.stamp("relay")}, "not-his"),
            "a wake-up": ({"turn_source": turn_source.stamp("wake")}, "source"),
            "a lead's answer for him": ({"turn_source": turn_source.stamp("answer_for")}, "source"),
            "a 协作 ask": ({"turn_source": turn_source.make("collab_ask", asserted_by="topic")}, "source"),
            "a 协作 reply": ({"turn_source": turn_source.make("collab_reply", asserted_by="bridge")}, "not-his"),
            "a turn held to reading": ({"read_only": True}, "read-only"),
        }
        for name, (job, why) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.allowed(**job), why)

    def test_the_butlers_family_and_a_proxy_topic_draft_none(self):
        from covey.core import butler

        self.assertEqual(self.allowed(topic_id=butler.butler_topic_id("oc_owner")), "main-thread")
        with mock.patch("covey.app.owner_send.proxy_topic", return_value=True):
            self.assertEqual(self.allowed(), "proxy-topic")

    def test_a_turn_that_may_not_draft_is_not_told_the_rule(self):
        from covey.app import owner_send

        with mock.patch.object(owner_send, "enabled", return_value=True), \
                mock.patch.object(owner_send.records, "open_lines", return_value=[]), \
                mock.patch.object(owner_send.records, "turn_lines", return_value=[]):
            mine = SimpleNamespace(topic_id="t" * 64, chat_id="oc_owner", sender_id=OWNER, turn_source={}, read_only=False)
            lead = SimpleNamespace(**{**vars(mine), "turn_source": turn_source.stamp("lead")})
            self.assertIn(owner_send.records.RULE, owner_send.turn_note(self.ctx, mine, self.access))
            self.assertEqual(owner_send.turn_note(self.ctx, lead, self.access), "")


class RaceTest(QuestionFixture, unittest.TestCase):
    """§5.4 R2: the owner's click and a lead's project_answer in the same instant: pending→submitting once, the loser told,
    one delivery, ``answer_by`` the winner's."""

    ROUNDS = 200

    def test_a_click_and_a_leads_answer_at_once_settle_one(self):
        from covey.app.answers import AnswerBy

        self.backend.declare(questions=True, steering=True)
        rounds, wins = 0, {"owner": 0, "lead": 0}
        for index in range(self.ROUNDS):
            self.rpc.answer.reset_mock()
            token = f"{index:016x}"
            path = self.ctx_topic.state_dir / "questions" / (token + ".json")
            path.write_text(json.dumps({**self.record, "token": token, "request_id": 17}))
            barrier, results = threading.Barrier(2), {}

            def answer(name, by, fields):
                barrier.wait()
                try:
                    self.app.questions.submit_answer(self.ctx_topic, "oc_chat", token, fields, f"{name}-{index}", by=by)
                    results[name] = "ok"
                except ValueError as exc:
                    results[name] = str(exc)
            threads = [threading.Thread(target=answer, args=("owner", AnswerBy("owner", "card", open_id=OWNER),
                                                             {"option_0": "0"})),
                       threading.Thread(target=answer, args=("lead", AnswerBy("lead", "tool", message_id=f"lead-{index}"),
                                                             {"option_0": "0"}))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            saved = json.loads(path.read_text())
            winners = [name for name, said in results.items() if said == "ok"]
            with self.subTest(index=index):
                self.assertEqual(len(winners), 1, results)
                loser = ({"owner", "lead"} - set(winners)).pop()
                self.assertTrue(any(words in results[loser] for words in ("回答已提交", "已经提交")), results[loser])
                self.assertEqual(saved["status"], "answered")
                self.assertEqual(saved["answer_by"]["party"], winners[0])
                self.assertEqual(self.rpc.answer.call_count, 1)
            wins[winners[0]] += 1
            rounds += 1
        self.assertEqual(rounds, self.ROUNDS)
        print(f"R2 {self.ROUNDS} rounds: owner won {wins['owner']}, lead won {wins['lead']}, 0 settled twice")


if __name__ == "__main__":
    unittest.main()
