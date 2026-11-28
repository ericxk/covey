"""敏感词脱敏 was taken out (T947, docs/legacy.md): an older card's buttons are answered in one line and change nothing,
and an older topic's ``redaction`` is read by nothing."""
from __future__ import annotations

import unittest
from unittest import mock

from support.world import UnifiedCase
from covey.app.card_actions import ACTIONS, REDACTION_KINDS, REDACTION_RETIRED, moved_card_text
from covey.core.harness import HarnessCapabilities


class RedactionRetiredTest(unittest.TestCase):
    def test_its_kinds_are_gone_and_an_older_cards_click_gets_the_notice(self):
        self.assertIn("bridge_redaction_add", REDACTION_KINDS)  # The 脱敏 tab's.
        self.assertIn("bridge_dashboard_rotate", REDACTION_KINDS)  # The retired web dashboard's 换口令 on it.
        self.assertIn("bridge_main_topic_redaction", REDACTION_KINDS)  # The 新话题 row of 配置.
        for kind in REDACTION_KINDS:
            with self.subTest(kind):
                self.assertNotIn(kind, ACTIONS)
                self.assertEqual(moved_card_text({"action_value": {"kind": kind, "token": "c" * 24}}), REDACTION_RETIRED)
        self.assertFalse(hasattr(HarnessCapabilities(), "redaction"))


class RedactionClickTest(UnifiedCase):
    def test_an_older_settings_card_click_says_so_and_changes_nothing(self):
        self.app.messenger.reply = mock.Mock(return_value=True)
        clicks = ({"kind": "bridge_redaction_add", "token": "c" * 24, "revision": 2},
                  {"kind": "bridge_dashboard_rotate", "token": "c" * 24},
                  {"kind": "bridge_main_topic_redaction", "token": "c" * 24, "revision": 3, "option": "on"})
        for n, value in enumerate(clicks):
            event = {"operator_id": "owner", "chat_id": "chat", "message_id": f"om_card{n}", "event_id": f"e{n}",
                     "action_value": value}
            self.assertEqual(self.app._handle_card_action_event(event, self.ctx), "extension_missing")
        self.assertEqual({call.args[2] for call in self.app.messenger.reply.call_args_list}, {REDACTION_RETIRED})
        self.assertEqual(self.app.messenger.reply.call_count, 3)
        self.assertFalse((self.ctx.state_dir / "redaction").exists())  # Nothing written there.
        self.assertFalse((self.ctx.state_dir / "main-thread.json").exists())  # No setting changed.
        self.assertFalse(hasattr(self.app, "redaction_panel"))


if __name__ == "__main__":
    unittest.main()
