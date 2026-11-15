"""管家规则 (app/prompts.py BUTLER_RULES, COVEY_MAIN_BUTLER=1, docs/main-thread.md 管家规则): with the variable unset
(or anything but 1) a main-thread turn's message is, byte for byte, the one in tests/golden/main_thread_prompt.txt (taken
from the code before the switch existed, knowledge and history search off); with it on, exactly one line differs, the
first of SHARED_RULES, and the knowledge rules still follow it."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from covey.app.prompts import (BUTLER_ENV, BUTLER_RULES, SHARED_RULES, butler_on, main_thread_message,
                                main_thread_rules, shared_rules)
from covey.core import knowledge

GOLDEN = Path(__file__).resolve().parent / "golden" / "main_thread_prompt.txt"
CLEAN = {BUTLER_ENV: "", "LARK_BRIDGE_MAIN_BUTLER": "", knowledge.ENV: "", knowledge.HISTORY_ENV: "",
         "LARK_BRIDGE_KNOWLEDGE": "", "LARK_BRIDGE_HISTORY_CMD": ""}


class ButlerRulesTest(unittest.TestCase):
    def env(self, **values):
        patcher = mock.patch.dict(os.environ, {**CLEAN, **values})
        patcher.start()
        self.addCleanup(patcher.stop)

    def golden(self) -> bytes:
        return GOLDEN.read_bytes()

    def test_with_the_switch_off_the_turns_message_is_the_baseline_byte_for_byte(self):
        for value in ("", "0", "true", " ", "yes"):
            self.env(**{BUTLER_ENV: value})
            self.assertFalse(butler_on())
            self.assertEqual(main_thread_message("快照", "消息").encode(), self.golden(), value)
        self.assertEqual(shared_rules(), SHARED_RULES)

    def test_with_the_switch_on_only_the_first_shared_rule_differs(self):
        self.env(**{BUTLER_ENV: "1"})
        self.assertTrue(butler_on())
        old = self.golden().decode().split("\n")
        new = main_thread_message("快照", "消息").split("\n")
        self.assertEqual(len(old), len(new))
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(changed, [(SHARED_RULES[0], BUTLER_RULES[0])])
        self.assertEqual(shared_rules(), BUTLER_RULES + SHARED_RULES[1:])

    def test_the_old_spelling_of_the_variable_works_too(self):
        self.env(LARK_BRIDGE_MAIN_BUTLER="1")
        del os.environ[BUTLER_ENV]  # The new spelling, when set, wins; patch.dict puts it back afterwards.
        self.assertTrue(butler_on())

    def test_the_butler_rule_is_one_line_and_says_what_the_owner_decided(self):
        self.assertEqual(len(BUTLER_RULES), 1)
        rule = BUTLER_RULES[0]
        self.assertNotIn("\n", rule)
        self.assertNotIn("一两分钟能做完", rule)
        for words in ("快答", "几秒", "topic_start", "topic_send", "T 号", "大概多久", "你直接做", "拿不准"):
            self.assertIn(words, rule)

    def test_the_knowledge_rules_still_follow_the_butler_rule(self):
        self.env(**{BUTLER_ENV: "1", knowledge.ENV: "1"})
        rules = main_thread_rules().split("\n")
        self.assertEqual(rules[3], BUTLER_RULES[0])
        self.assertTrue(any("knowledge_save" in line for line in rules))
        self.assertEqual(rules[0], "[主线程规则]")


if __name__ == "__main__":
    unittest.main()
