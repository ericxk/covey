"""Whether a turn's words are the owner's own (app/provenance.py, WAVE12 C1), and who answered a question (app/answers.py
AnswerBy, WAVE12 C2): read the same way as the places that ask it today, and taken by app/questions.py submit_answer
beside an open_id, as before."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.core import turn_source
from covey.core.jobs import BridgeReplyJob

OWNER, MEMBER, OWNER_CHAT = "ou_owner", "ou_member", "oc_owner"


class Access:
    """app/access.py owner_turn as it reads: the owner's sender, or any turn of his private chat without a sender."""

    def owner_turn(self, chat_id, sender_id):
        return sender_id == OWNER if sender_id else chat_id == OWNER_CHAT


def job(message_id="om_1", chat="oc_group", sender=OWNER, source=None, **fields):
    return {"message_id": message_id, "chat_id": chat, "sender_id": sender, **({"turn_source": source} if source else {}),
            **fields}


def source(kind_, asserted_by, origin=None):
    return turn_source.make(kind_, asserted_by=asserted_by, origin=origin)


class OwnerSaidTest(unittest.TestCase):
    def test_the_owners_own_message_from_feishu_or_the_page_is_his(self):
        from covey.app.provenance import owner_said

        for item in (job("om_1"), job("web-17-ab"), job("om_1", chat=OWNER_CHAT, sender=""),
                     job("om_2", source=source("owner", "owner", {"message_id": "om_2"})),
                     job("answer-0123", source=source("answer", "owner", {"question": "0123456789abcdef"})),
                     BridgeReplyJob(job_id=1, user_text="", chat_id="oc_group", message_id="om_1", timeout=0, sender_id=OWNER)):
            with self.subTest(item=item):
                self.assertTrue(owner_said(item, Access()))

    def test_anybody_elses_words_a_hand_on_without_his_message_and_a_retry_are_not(self):
        from covey.app.provenance import owner_said

        cases = {
            "a member's message": job("om_1", sender=MEMBER),
            "no sender outside his chat": job("om_1", sender=""),
            "an answer continuation, unstamped": job("answer-0123"),
            "the butler's hand-on": job("main-4", source=source("relay", "butler")),
            # Its origin names his message, but its words are the butler's (BEHAVIOR owner_words, W1-B).
            "the butler's hand-on of his message": job("relay-3", source=source("relay", "owner", {"message_id": "om_2"})),
            "a lead's work": job("lead-5", source=source("lead", "lead", {"topic": "t1"})),
            "a wake-up": job("wake-6", source=source("wake", "bridge")),
            "a lead's answer for him": job("answer-0123", source=source("answer_for", "lead", {"question": "0123456789abcdef"})),
            "a newer release's kind": job("om_1", source={"v": 1, "kind": "lead_wake", "asserted_by": "owner",
                                                          "recorded_by": "bridge", "origin": {"message_id": "om_1"}}),
            "an automatic retry": job("om_1", auto_retry=1),
            "a retry after a refusal": job("om_1", policy_retry={"from": 3}),
        }
        for name, item in cases.items():
            with self.subTest(name):
                self.assertFalse(owner_said(item, Access()))

    def test_it_says_what_the_two_direct_checks_of_today_say_of_a_legacy_job(self):
        from covey.app import butler_asks, collab_flow
        from covey.app.provenance import owner_said

        for item in (job("om_1"), job("web-1"), job("om_1", sender=MEMBER), job("om_1", auto_retry=2), job("main-3"),
                     job("om_1", chat=OWNER_CHAT, sender=""), job("collab-c1-1", source=source("collab_ask", "topic"))):
            with self.subTest(item=item):
                self.assertEqual(owner_said(item, Access()), collab_flow.direct(item, Access()))
                self.assertEqual(owner_said(item, Access()), butler_asks.direct(item, Access()))


TOKEN = "0123456789abcdef"
ANSWER_OF = source("answer", "owner", {"question": TOKEN})
ANSWER_FOR_OF = source("answer_for", "lead", {"question": TOKEN})
RELAY_OF = source("relay", "owner", {"message_id": "om_2"})
LEAD_OF = source("lead", "lead", {"topic": "t1"})


class ReadersTest(unittest.TestCase):
    """The places that told the owner's words by a message id prefix ask the source of a stamped job and go on as before for one
    without (W1-B): a hand-on carrying his message id is not his words, his own answer is."""

    def test_collab_flow_direct_and_owner_rows_follow_the_source(self):
        from covey.app import collab_flow

        for name, item, expected in (("relay with his message", job("main-1", source=RELAY_OF), False),
                                     ("relay carrying om_ as its id", job("om_9", source=RELAY_OF), False),
                                     ("lead", job("om_9", source=LEAD_OF), False),
                                     ("answer_for", job("answer-" + TOKEN, source=ANSWER_FOR_OF), False),
                                     ("his answer", job("answer-" + TOKEN, source=ANSWER_OF), True),
                                     ("his message, unstamped", job("om_1"), True),
                                     ("an answer id, unstamped", job("answer-" + TOKEN), False)):
            with self.subTest(name):
                self.assertEqual(collab_flow.direct(item, Access()), expected)
        with tempfile.TemporaryDirectory() as tmp:
            from covey.core.jobs import upsert_job_record

            for number, (message, made) in enumerate((("om_1", None), ("om_2", RELAY_OF), ("om_3", LEAD_OF),
                                                      ("answer-" + TOKEN, ANSWER_OF)), start=1):
                upsert_job_record(Path(tmp), f"k{number}", job_id=number, status="done", chat_id="oc", topic_id="t1",
                                  message_id=message, **({"turn_source": made} if made else {}))
            rows = collab_flow.owner_rows(tmp, "t1", "oc", 0)
            self.assertEqual([row["job_id"] for row in rows], [1, 4])

    def test_butler_asks_direct_counts_his_stamped_answer_and_not_a_leads_for_him(self):
        from covey.app import butler_asks

        for name, item, expected in (("his answer", job("answer-" + TOKEN, source=ANSWER_OF), True),
                                     ("a lead's answer for him", job("answer-" + TOKEN, source=ANSWER_FOR_OF), False),
                                     ("relay", job("om_9", source=RELAY_OF), False),
                                     ("a retried answer", job("answer-" + TOKEN, source=ANSWER_OF, auto_retry=1), False)):
            with self.subTest(name):
                self.assertEqual(butler_asks.direct(item, Access()), expected)
        self.assertIs(butler_asks.retried, __import__("covey.app.provenance", fromlist=["x"]).retried)

    def test_memory_takes_no_candidate_from_a_hand_on_and_one_from_his_answer(self):
        from covey.app.memory_candidates import not_owner_reason

        self.assertEqual(not_owner_reason({"message_id": "om_9", "turn_source": RELAY_OF}, text="以后都这样"), "source")
        self.assertEqual(not_owner_reason({"message_id": "om_9", "turn_source": LEAD_OF}, text="以后都这样"), "source")
        self.assertEqual(not_owner_reason({"message_id": "answer-" + TOKEN, "turn_source": ANSWER_FOR_OF}, text="以后都这样"),
                         "source")
        self.assertEqual(not_owner_reason({"message_id": "answer-" + TOKEN, "turn_source": ANSWER_OF}, text="以后都这样"), "")
        self.assertEqual(not_owner_reason({"message_id": "main-x"}, text="以后都这样"), "bridge-id")  # Unstamped: as before.

    def test_memory_collect_and_the_fix_rule_skip_a_hand_on_carrying_his_message(self):
        from covey.app import memory_collect

        runtime = SimpleNamespace(access=Access(), log=lambda *_: None)
        ctx = SimpleNamespace(state_dir=Path("/absolute/state"), topic_id="t5")
        record = {"status": "done", "chat_id": OWNER_CHAT, "topic_id": "t5", "user_text": "以后都要带基线，记一下",
                  "message_id": "om_9", "sender_id": ""}
        for made in (RELAY_OF, LEAD_OF, ANSWER_FOR_OF):
            self.assertEqual(memory_collect.reason_to_ignore(runtime, ctx, SimpleNamespace(), {**record, "turn_source": made}),
                             "not-owner-words")
        reason = memory_collect.reason_to_ignore(runtime, ctx, SimpleNamespace(), {**record, "turn_source": ANSWER_OF})
        self.assertNotIn(reason, ("not-owner-words", "not-owner", "handed-on"))

    def test_memory_learn_finds_the_question_of_his_stamped_answer_only(self):
        from covey.app import memory_learn
        from covey.core.state import write_json

        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "questions" / (TOKEN + ".json"),
                       {"questions": [{"question": "用哪个集群", "options": [{"label": "A", "description": "旧"}]}]})
            card = [{"question": "用哪个集群", "options": ["A 旧"]}]
            self.assertEqual(memory_learn.asked_card(tmp, {"message_id": "answer-" + TOKEN}), card)  # Unstamped: its id.
            self.assertEqual(memory_learn.asked_card(tmp, {"message_id": "x-1", "turn_source": ANSWER_OF}), card)
            self.assertEqual(memory_learn.asked_card(tmp, {"message_id": "answer-" + TOKEN, "turn_source": ANSWER_FOR_OF}), [])
            self.assertEqual(memory_learn.asked_card(tmp, {"message_id": "answer-" + TOKEN, "turn_source": RELAY_OF}), [])

    def test_the_page_reads_a_hand_on_by_its_source_and_an_older_one_as_before(self):
        from covey.app import web_said
        from covey.core.jobs import upsert_job_record

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            for key, made in (("main-r1", RELAY_OF), ("main-r2", source("relay", "butler")),
                              ("main-r3", turn_source.make("relay", asserted_by="owner", origin={"message_id": "om_2"},
                                                           via="lead")),
                              ("main-l1", LEAD_OF)):
                upsert_job_record(state, key, job_id=1, status="done", chat_id="oc", message_id=key, turn_source=made)
            origins = web_said.Origins(state, "oc")
            self.assertEqual(origins.of("main-r1"), ("owner", "butler"))
            self.assertEqual(origins.of("main-r2"), ("butler", ""))
            self.assertEqual(origins.of("main-r3"), ("owner", "lead"))
            self.assertEqual(origins.of("main-l1"), ("lead", ""))
            self.assertEqual(origins.of("main-gone"), ("owner", ""))  # No record, no source: as before.


class SyntheticTest(unittest.TestCase):
    def test_the_bridges_and_the_agents_turns_are_synthetic_and_the_owners_are_not(self):
        from covey.app.provenance import SYNTHETIC, synthetic

        self.assertEqual(SYNTHETIC, {"relay", "lead", "wake", "answer_for", "collab_ask", "collab_reply"})
        for kind_ in sorted(SYNTHETIC):
            made = source(kind_, "bridge")
            with self.subTest(kind=kind_):
                self.assertTrue(synthetic(made))  # The field itself,
                self.assertTrue(synthetic({"turn_source": made}))  # a record,
                self.assertTrue(synthetic(SimpleNamespace(turn_source=made)))  # a job.
        for item in (source("owner", "owner", {"message_id": "om_1"}), source("answer", "owner", {"question": "q"}),
                     source("handover", "owner", {"message_id": "om_1"}), {"message_id": "om_1"}, {}, None,
                     BridgeReplyJob(job_id=1, user_text="", chat_id="c", message_id="om_1", timeout=0)):
            with self.subTest(item=item):
                self.assertFalse(synthetic(item))


class AnswerByTest(unittest.TestCase):
    def test_who_answered_and_how(self):
        from covey.app.answers import AnswerBy, open_id_of

        by = AnswerBy(party=turn_source.PARTY_OWNER, channel="card", open_id=OWNER)
        self.assertEqual((by.party, by.channel, by.open_id, by.message_id), ("owner", "card", OWNER, ""))
        with self.assertRaises(Exception):
            by.party = "lead"  # Frozen.
        self.assertEqual([open_id_of(b) for b in (by, AnswerBy("owner", "web", message_id="web-1"), OWNER, "", None)],
                         [OWNER, None, OWNER, "", None])

    def test_submit_answer_hands_the_runner_the_open_id_and_who_and_keeps_an_answer_by(self):
        from covey.app import questions
        from covey.app.answers import AnswerBy

        seen = []
        fake = SimpleNamespace(runtime=None, _took=lambda ctx, top: None,
                               _submit_answer=lambda *args, by=None, **kwargs: seen.append(("bridge", by)))
        with mock.patch.object(questions, "forward_answer",
                               side_effect=lambda *a, by=None, answer_by=None: seen.append(("runner", by, answer_by))):
            for by in (OWNER, AnswerBy("owner", "card", open_id=OWNER), AnswerBy("owner", "web", message_id="web-1"), None):
                questions.Questions.submit_answer(fake, SimpleNamespace(), "oc_chat", "t" * 16, {}, "e1", by=by)
        # The runner's ``by`` is the open_id as before (a runner of an older release reads only it), ``answer_by`` who.
        self.assertEqual(seen, [("runner", OWNER, {}), ("bridge", AnswerBy("", "", open_id=OWNER)),
                                ("runner", OWNER, {"party": "owner", "channel": "card"}),
                                ("bridge", AnswerBy("owner", "card", open_id=OWNER)),
                                ("runner", None, {"party": "owner", "channel": "web", "message_id": "web-1"}),
                                ("bridge", AnswerBy("owner", "web", message_id="web-1")),
                                ("runner", None, {}), ("bridge", None)])

if __name__ == "__main__":
    unittest.main()
