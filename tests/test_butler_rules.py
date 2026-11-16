"""管家规则 (app/prompts.py BUTLER_RULES, COVEY_MAIN_BUTLER=1, docs/main-thread.md 管家规则): with the variable unset
(or anything but 1) a main-thread turn's message is, byte for byte, the one in tests/golden/main_thread_prompt.txt (taken
from the code before the switch existed, knowledge and history search off); with it on, exactly two lines differ, the
first two of SHARED_RULES, and the knowledge rules still follow them. The tool list is the same: topic_start's description
(core/main_tool_specs.py) changes only with the variable on, which the Bridge hands on to the MCP server."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from covey.core import main_thread
from covey.core.main_tool_specs import BUTLER_ENV as TOOLS_BUTLER_ENV
from covey.core.main_tool_specs import START_BUTLER, START_OWN, TOOLS, listed

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

    def test_with_the_switch_on_only_the_first_two_shared_rules_differ(self):
        self.env(**{BUTLER_ENV: "1"})
        self.assertTrue(butler_on())
        old = self.golden().decode().split("\n")
        new = main_thread_message("快照", "消息").split("\n")
        self.assertEqual(len(old), len(new))
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(changed, [(SHARED_RULES[0], BUTLER_RULES[0]), (SHARED_RULES[1], BUTLER_RULES[1])])
        self.assertEqual(shared_rules(), BUTLER_RULES + SHARED_RULES[2:])

    def test_the_old_spelling_of_the_variable_works_too(self):
        self.env(LARK_BRIDGE_MAIN_BUTLER="1")
        del os.environ[BUTLER_ENV]  # The new spelling, when set, wins; patch.dict puts it back afterwards.
        self.assertTrue(butler_on())

    def test_the_butler_rules_are_one_line_each_and_say_what_the_owner_decided(self):
        self.assertEqual(len(BUTLER_RULES), 2)
        rule = BUTLER_RULES[0]
        self.assertTrue(all("\n" not in line for line in BUTLER_RULES))
        self.assertNotIn("一两分钟能做完", rule)
        self.assertNotIn("可能超过一两分钟", BUTLER_RULES[1])  # What goes to a topic area is no longer "over a minute or two".
        self.assertIn("预计超过半分钟的", BUTLER_RULES[1])
        self.assertIn("topic_start", BUTLER_RULES[1])
        for words in ("快答", "几秒", "topic_start", "topic_send", "T 号", "大概多久", "你直接做", "拿不准"):
            self.assertIn(words, rule)

    def test_the_knowledge_rules_still_follow_the_butler_rule(self):
        self.env(**{BUTLER_ENV: "1", knowledge.ENV: "1"})
        rules = main_thread_rules().split("\n")
        self.assertEqual(rules[3:5], list(BUTLER_RULES))
        self.assertTrue(any("knowledge_save" in line for line in rules))
        self.assertEqual(rules[0], "[管家规则]")


class ButlerToolsTest(unittest.TestCase):
    """topic_start's description follows the rules (2026-11-15, B1): with COVEY_MAIN_BUTLER=1 it no longer says the main
    thread does a minute or two's reading and commands itself."""

    def start(self, tools):
        return next(tool for tool in tools if tool["name"] == "topic_start")

    def test_the_variable_is_the_same_one_the_rules_read(self):
        self.assertEqual(TOOLS_BUTLER_ENV, BUTLER_ENV)

    def test_with_the_switch_off_the_list_is_the_one_before_it_byte_for_byte(self):
        for value in (None, "", "0", "true", "yes"):
            environ = {} if value is None else {BUTLER_ENV: value}
            self.assertIs(listed(environ), TOOLS, value)
            self.assertIn(START_OWN, self.start(listed(environ))["description"])

    def test_with_the_switch_on_only_topic_starts_description_changes(self):
        on = listed({BUTLER_ENV: "1"})
        self.assertEqual([tool["name"] for tool in on], [tool["name"] for tool in TOOLS])
        changed = [name for name, a, b in ((a["name"], a, b) for a, b in zip(TOOLS, on)) if a != b]
        self.assertEqual(changed, ["topic_start"])
        description = self.start(on)["description"]
        self.assertNotIn("一两分钟", description)
        self.assertIn(START_BUTLER, description)
        self.assertEqual(description, self.start(TOOLS)["description"].replace(START_OWN, START_BUTLER))
        self.assertEqual(self.start(on)["input_schema"], self.start(TOOLS)["input_schema"])

    def test_the_old_spelling_and_the_knowledge_tools_go_with_it(self):
        on = listed({"LARK_BRIDGE_MAIN_BUTLER": "1", "COVEY_KNOWLEDGE": "1"})
        self.assertNotIn("一两分钟", self.start(on)["description"])
        self.assertGreater(len(on), len(TOOLS))

    def test_the_bridge_hands_the_variable_to_the_mcp_server_only_when_it_has_it(self):
        def env():
            return main_thread.mcp_config(Path("/r"), Path("/s"), "job-1", "/usr/bin/python3")["mcpServers"]["bridge"]["env"]
        self.env_patch = mock.patch.dict(os.environ, CLEAN)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        plain = env()
        self.assertNotIn("COVEY_MAIN_BUTLER", plain)
        self.assertNotIn("LARK_BRIDGE_MAIN_BUTLER", plain)
        with mock.patch.dict(os.environ, {BUTLER_ENV: "1"}):
            handed = env()
        self.assertEqual((handed["COVEY_MAIN_BUTLER"], handed["LARK_BRIDGE_MAIN_BUTLER"]), ("1", "1"))
        self.assertEqual({k: v for k, v in handed.items() if "BUTLER" not in k}, plain)
        self.assertIn(START_BUTLER, self.start(listed(handed))["description"])


if __name__ == "__main__":
    unittest.main()
