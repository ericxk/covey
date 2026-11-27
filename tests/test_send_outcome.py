"""What became of a send (WAVE12 C10, T904): feishu/transport.py ``outcome`` (SENT, NOT_SENT, UNCERTAIN) and
feishu/messenger.py ``Messenger.last_outcome``, which the status cards, the limit notice, the relays and 批量换配置 read to
tell a send that surely did not go (the same key may go again) from one that may have gone (never sent again)."""
from __future__ import annotations

import subprocess
import threading
import unittest
from unittest import mock

from support.fake_lark import FakeCardKit
from support.world import TopicFixture

from covey.feishu import messenger as messenger_module, transport
from covey.feishu.messenger import Messenger, unsent_outcome
from covey.feishu.transport import NOT_SENT, SENT, UNCERTAIN, Failure, classify, outcome

CARD = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "x"}]}}
VOICE = messenger_module.Voice("t", "COVEY_TEST")


def failing(failure, answer=None):
    """A stand-in for a Feishu call that leaves ``failure`` where the transport does and answers ``answer``."""
    def call(*args, **kwargs):
        transport.set_failure(failure)
        return answer
    return call


class OutcomeTest(unittest.TestCase):
    def test_each_kind_of_failure_is_sent_not_sent_or_uncertain(self):
        rows = {
            "confirmed": (True, None, SENT),
            "no failure known": (False, None, UNCERTAIN),
            "timeout": (False, classify(exception=subprocess.TimeoutExpired("lark-cli", 30)), UNCERTAIN),
            "network": (False, classify({"error": {"type": "network"}}), UNCERTAIN),
            "unconfirmed": (False, classify({"ok": False}), UNCERTAIN),
            "an earlier try still unknown": (False, Failure("reconciliation_required"), UNCERTAIN),
            "rate limited": (False, classify({"error": {"type": "rate_limit"}}), NOT_SENT),
            "refused content": (False, classify({"error": {"subtype": "invalid_content"}}), NOT_SENT),
            "card over its size": (False, classify({"error": {"type": "api", "code": "300305"}}), NOT_SENT),
            "audit": (False, classify({"error": {"code": "230028"}}), NOT_SENT),
            "authentication": (False, classify({"error": {"type": "auth"}}), NOT_SENT),
            "the CLI did not run": (False, classify(exception=OSError("no lark-cli")), NOT_SENT),
            "the key used for other content": (False, Failure("idempotency_conflict"), NOT_SENT),
            "any ambiguous one": (False, Failure("api_rejected", ambiguous=True), UNCERTAIN),
        }
        for name, (ok, failure, expected) in rows.items():
            with self.subTest(name):
                self.assertEqual(outcome(ok, failure), expected)
        self.assertEqual(outcome(True, Failure("rate_limited")), SENT)  # Confirmed wins over a stale failure.


class MessengerOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.messenger = Messenger(lambda line: None)
        transport.set_failure(None)
        self.addCleanup(transport.set_failure, None)

    def test_before_any_send_and_for_one_that_raised_it_is_uncertain(self):
        self.assertEqual(self.messenger.last_outcome(), UNCERTAIN)
        with FakeCardKit(side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.messenger.create_and_send_card("p", "oc_chat", CARD, "k1", voice=VOICE)
        self.assertEqual(self.messenger.last_outcome(), UNCERTAIN)

    def test_no_card_made_is_not_sent_whatever_the_create_failed_on(self):
        for failure in (Failure("api_rejected", code="300305"), Failure("network_timeout", True, True)):
            with self.subTest(failure.category), FakeCardKit(side_effect=failing(failure)) as lark:
                self.assertEqual(self.messenger.create_and_send_card("p", "oc_chat", CARD, "k1", voice=VOICE), ("", ""))
                lark.send.assert_not_called()
            self.assertEqual(self.messenger.last_outcome(), NOT_SENT)

    def test_a_card_made_and_its_message_refused_timed_out_or_sent(self):
        rows = (("refused", failing(Failure("rate_limited", True), ("", False)), NOT_SENT),
                ("timed out", failing(Failure("network_timeout", True, True), ("", False)), UNCERTAIN),
                ("answered without a message id", failing(None, ("", False)), UNCERTAIN),
                ("sent", failing(None, ("om_sent", False)), SENT))
        for name, send, expected in rows:
            with self.subTest(name), FakeCardKit({"data": {"card_id": "card-1"}}, sent_effect=send):
                card_id, message = self.messenger.create_and_send_card("p", "oc_chat", CARD, "k1", voice=VOICE)
            self.assertEqual(card_id, "card-1")
            self.assertEqual(self.messenger.last_outcome(), expected)

    def test_send_message_says_what_its_last_chunk_met(self):
        rows = ((False, Failure("content"), NOT_SENT), (False, Failure("unconfirmed", True, True), UNCERTAIN),
                (True, None, SENT))
        for ok, failure, expected in rows:
            with self.subTest(expected), mock.patch.object(messenger_module, "send_message_with_fallback",
                                                           side_effect=failing(failure, ok)):
                self.assertEqual(self.messenger.send_message("p", "oc_chat", "hi", "k2", voice=VOICE), ok)
            self.assertEqual(self.messenger.last_outcome(), expected)
            self.assertEqual(unsent_outcome(self.messenger), NOT_SENT if expected == NOT_SENT else UNCERTAIN)

    def test_it_is_each_threads_own_and_each_messengers_own(self):
        with mock.patch.object(messenger_module, "send_message_with_fallback",
                               side_effect=failing(Failure("content"), False)):
            self.messenger.send_message("p", "oc_chat", "hi", "k2", voice=VOICE)
        seen = []
        thread = threading.Thread(target=lambda: seen.append(self.messenger.last_outcome()))
        thread.start()
        thread.join()
        self.assertEqual((self.messenger.last_outcome(), seen), (NOT_SENT, [UNCERTAIN]))
        self.assertEqual(Messenger(lambda line: None).last_outcome(), UNCERTAIN)

    def test_a_stand_in_that_cannot_say_is_uncertain(self):
        self.assertEqual(unsent_outcome(object()), UNCERTAIN)
        self.assertEqual(unsent_outcome(mock.Mock()), UNCERTAIN)  # A Mock answers anything: not NOT_SENT.


class PublishOutcomeTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.receive("om_a", "first")
        self.target = self.topic("om_a")

    def test_the_line_sent_in_place_of_a_card_is_not_the_cards_outcome(self):
        """publish_card sends one line where a card could not go (T889): what the caller reads is the card's."""
        with FakeCardKit(side_effect=failing(Failure("api_rejected", code="300305"))), \
                mock.patch.object(messenger_module, "send_message_with_fallback", side_effect=failing(None, True)):
            self.assertFalse(self.app.messenger.publish_card(self.target, "oc_chat", "page-1", CARD,
                                                             voice=self.app.voice))
        self.assertEqual(self.app.messenger.last_outcome(), NOT_SENT)
        with FakeCardKit({"data": {"card_id": "card-x"}}, sent_effect=failing(Failure("network_timeout", True, True),
                                                                              ("", False))), \
                mock.patch.object(messenger_module, "send_message_with_fallback", side_effect=failing(None, True)):
            self.assertFalse(self.app.messenger.publish_card(self.target, "oc_chat", "page-2", CARD,
                                                             voice=self.app.voice))
        self.assertEqual(self.app.messenger.last_outcome(), UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
