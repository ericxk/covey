"""独立复核 was taken out (T937, docs/legacy.md): an older card's buttons are answered in one line and open nothing."""
from __future__ import annotations

import unittest
from unittest import mock

from support.world import UnifiedCase
from covey.app.card_actions import ACTIONS, REVIEW_KINDS, REVIEW_RETIRED, moved_card_text


class ReviewRetiredTest(unittest.TestCase):
    def test_its_kinds_are_gone_and_an_older_cards_click_gets_the_notice(self):
        self.assertEqual(REVIEW_KINDS[0], "bridge_review_open")  # The answer card's and the relay's button.
        for kind in REVIEW_KINDS:
            with self.subTest(kind):
                self.assertNotIn(kind, ACTIONS)
                self.assertEqual(moved_card_text({"action_value": {"kind": kind, "topic_id": "t" * 64, "job_id": 7}}),
                                 REVIEW_RETIRED)
        self.assertEqual(moved_card_text({"action_value": {"kind": "bridge_main_lead_harness"}}), "")


class ReviewClickTest(UnifiedCase):
    def test_an_older_answer_card_or_settings_card_click_says_so_and_opens_nothing(self):
        self.app.messenger.reply = mock.Mock(return_value=True)
        self.app.messenger.create_and_send_card = mock.Mock(return_value=("card", "om_new"))
        clicks = ({"kind": "bridge_review_open", "topic_id": "t" * 64, "job_id": 7},
                  {"kind": "bridge_main_review_harness", "token": "c" * 24, "revision": 3, "option": "codex"})
        for n, value in enumerate(clicks):
            event = {"operator_id": "owner", "chat_id": "chat", "message_id": f"om_card{n}", "event_id": f"e{n}",
                     "action_value": value}
            self.assertEqual(self.app._handle_card_action_event(event, self.ctx), "extension_missing")
        self.assertEqual(self.app.messenger.reply.call_count, 2)
        self.assertEqual({call.args[2] for call in self.app.messenger.reply.call_args_list}, {REVIEW_RETIRED})
        self.app.messenger.create_and_send_card.assert_not_called()  # No topic card: no topic opened.
        self.assertFalse((self.ctx.state_dir / "reviews").exists())
        self.assertFalse(hasattr(self.app, "review"))


if __name__ == "__main__":
    unittest.main()
