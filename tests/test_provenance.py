"""Whether a turn's words are the owner's own (app/provenance.py, WAVE12 C1), and who answered a question (app/answers.py
AnswerBy, WAVE12 C2): read the same way as the places that ask it today, and taken by app/questions.py submit_answer
beside an open_id, as before."""
from __future__ import annotations

import unittest
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
                     job("relay-3", source=source("relay", "owner", {"message_id": "om_2"})),
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

    def test_submit_answer_reads_an_answer_bys_open_id_where_it_read_the_string(self):
        from covey.app import questions
        from covey.app.answers import AnswerBy

        seen = []
        fake = SimpleNamespace(runtime=None, _took=lambda ctx, top: None,
                               _submit_answer=lambda *args, by=None, **kwargs: seen.append(("bridge", by)))
        with mock.patch.object(questions, "forward_answer", side_effect=lambda *a, by=None: seen.append(("runner", by))):
            for by in (OWNER, AnswerBy("owner", "card", open_id=OWNER), AnswerBy("owner", "web", message_id="web-1"), None):
                questions.Questions.submit_answer(fake, SimpleNamespace(), "oc_chat", "t" * 16, {}, "e1", by=by)
        self.assertEqual(seen, [("runner", OWNER), ("bridge", OWNER), ("runner", OWNER), ("bridge", OWNER),
                                ("runner", None), ("bridge", None), ("runner", None), ("bridge", None)])


if __name__ == "__main__":
    unittest.main()
