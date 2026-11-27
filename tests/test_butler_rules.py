"""管家规则 (app/prompts.py BUTLER_RULES, COVEY_BUTLER_RULES on unless 0, docs/butler.md 管家规则): with the variable at 0
(off, false, no) a butler turn's message is, byte for byte, the one in tests/golden/butler_prompt.txt (taken
from the code before the switch existed, knowledge and history search off); with it on, exactly two lines differ, the
first two of SHARED_RULES, and the knowledge rules still follow them. The tool list is the same: topic_start's description
(core/butler_tool_specs.py) changes only with the variable on, which the Bridge hands on to the MCP server."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from covey.core import env as cenv
from covey.core import butler
from covey.core.butler_tool_specs import BUTLER_RULES_ENV as TOOLS_BUTLER_ENV
from covey.core.butler_tool_specs import START_BUTLER, START_OWN, TOOLS, listed

from covey.app.prompts import (AREA_OPENING, BUTLER_RULES_ENV, BUTLER_RULES, SHARED_RULES, butler_on, butler_message,
                                butler_rules, shared_rules,
                               AREA_RULE, PIN_WORDS, PINNED_OVERVIEW_ENV, QUICKSTART, area_rule)
from covey.core import knowledge

GOLDEN = Path(__file__).resolve().parent / "golden" / "butler_prompt.txt"
# The baseline of the golden message: the switches that became default-on set off, the history command unset.
CLEAN = {BUTLER_RULES_ENV: "0", "LARK_BRIDGE_MAIN_BUTLER": "0", knowledge.ENV: "0", knowledge.HISTORY_ENV: "",
         "LARK_BRIDGE_KNOWLEDGE": "0", "LARK_BRIDGE_HISTORY_CMD": "", PINNED_OVERVIEW_ENV: "0",
         "LARK_BRIDGE_PINNED_OVERVIEW": "0", "COVEY_BUTLER_TRACKS": "0", "COVEY_PROJECTS": "0", "COVEY_MEMORY": "0",
         "COVEY_PROJECT_TIDY": "off"}
# What listed() reads to give TOOLS as they were: the rules, the knowledge tools and the project tools off.
TOOLS_OFF = {BUTLER_RULES_ENV: "0", knowledge.ENV: "0", "COVEY_PROJECTS": "0"}


class ButlerRulesTest(unittest.TestCase):
    def env(self, **values):
        patcher = mock.patch.dict(os.environ, {**CLEAN, **values})
        patcher.start()
        self.addCleanup(patcher.stop)

    def golden(self) -> bytes:
        """The golden message with the path USAGE_RULE names put in: where this checkout's docs/quickstart.md is."""
        return GOLDEN.read_bytes().replace(b"{QUICKSTART}", str(QUICKSTART).encode())

    def test_with_the_switch_off_the_turns_message_is_the_baseline_byte_for_byte(self):
        for value in ("0", "off", "false", "no", " NO "):
            self.env(**{BUTLER_RULES_ENV: value})
            self.assertFalse(butler_on())
            self.assertEqual(butler_message("快照", "消息").encode(), self.golden(), value)
        self.assertEqual(shared_rules(), SHARED_RULES)

    def test_unset_or_empty_the_rules_are_on(self):
        for value in ("", "true", " ", "yes"):
            self.env(**{BUTLER_RULES_ENV: value, "LARK_BRIDGE_MAIN_BUTLER": value})
            self.assertTrue(butler_on(), value)
        self.assertTrue(butler_on({}))
        self.assertTrue(butler_on({"COVEY_X": "0"}))
        self.assertEqual(shared_rules({}), BUTLER_RULES + SHARED_RULES[2:])

    def test_with_the_switch_on_only_the_first_two_shared_rules_differ(self):
        self.env(**{BUTLER_RULES_ENV: "1"})
        self.assertTrue(butler_on())
        old = self.golden().decode().split("\n")
        new = butler_message("快照", "消息").split("\n")
        self.assertEqual(len(old), len(new))
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(changed, [(SHARED_RULES[0], BUTLER_RULES[0]), (SHARED_RULES[1], BUTLER_RULES[1])])
        self.assertEqual(shared_rules(), BUTLER_RULES + SHARED_RULES[2:])

    def test_the_pin_is_named_only_where_one_is_pinned(self):
        # A new install has no 手上的事 pinned: its butler told the owner to look at a Pin that was not there.
        for value in ("0", "off", "false"):
            self.env(**{PINNED_OVERVIEW_ENV: value})
            self.assertNotIn("Pin", butler_rules())
            self.assertEqual(area_rule(), AREA_RULE.replace(PIN_WORDS, ""))
        self.env(**{PINNED_OVERVIEW_ENV: "1"})
        self.assertEqual(area_rule(), AREA_RULE)
        self.assertIn("进度主人看 Pin。被问到时", butler_rules())
        self.assertIn("一行说明；被问到时", area_rule({PINNED_OVERVIEW_ENV: "0"}))
        self.assertEqual(area_rule({}), AREA_RULE)  # No key at all: pinned.

    def test_the_two_ways_of_asking_each_say_when(self):
        # 要主人定的事 goes to the checkpoint (a question card, the turn ends); which topic a message belongs to is
        # asked at once, since the turn still has to hand the owner's message on (app/butler_handoff.py asked).
        asking = AREA_OPENING[2]
        self.assertIn("写进本任务检查点", asking)
        self.assertIn("只有“这句话该交给哪个话题”拿不准时不写检查点", asking)
        self.assertIn("AskUserQuestion 当场问", asking)
        self.assertEqual(1, sum("AskUserQuestion 出一道单选题" in rule for rule in SHARED_RULES))

    def test_the_opening_and_the_last_rule_no_longer_disagree_about_pointing_at_a_card(self):
        self.assertNotIn("不要让主人去别处看", butler_rules())
        self.assertIn("确实要主人动手时，用一句话说点哪里", AREA_OPENING[1])
        self.assertIn("可以点那张任务卡上的「看看」", AREA_RULE)

    def test_the_old_spelling_of_the_variable_works_too(self):
        self.env(LARK_BRIDGE_MAIN_BUTLER="0")
        for name in cenv.spellings(BUTLER_RULES_ENV)[:-1]:
            os.environ.pop(name, None)  # The new spellings, when set, win; patch.dict puts them back afterwards.
        self.assertFalse(butler_on())

    def test_the_butler_rules_are_one_line_each_and_say_what_the_owner_decided(self):
        self.assertEqual(len(BUTLER_RULES), 2)
        rule = BUTLER_RULES[0]
        self.assertTrue(all("\n" not in line for line in BUTLER_RULES))
        self.assertNotIn("一两分钟能做完", rule)
        self.assertNotIn("可能超过一两分钟", BUTLER_RULES[1])  # What goes to a topic area is no longer "over a minute or two".
        self.assertIn("预计超过半分钟的", rule)  # Said once (W0-6): the second line no longer repeats the list.
        self.assertNotIn("预计超过半分钟的", BUTLER_RULES[1])
        self.assertIn("topic_start", BUTLER_RULES[1])
        for words in ("快答", "几秒", "topic_start", "topic_send", "T 号", "你直接做", "拿不准"):
            self.assertIn(words, rule)
        # Its example guessed a time ("大概十分钟"); a time is said only when the snapshot has an estimate (5.9 row 4).
        self.assertNotIn("大概", rule)
        self.assertIn("“放到 T432「标题」里做”；快照里有预计才报时间。", rule)

    def test_the_rules_say_what_an_owner_who_saw_nothing_else_needs(self):
        # 5.9 row 9: the owner read none of the topics; how is the butler's and what the owner's; nothing wakes it later.
        self.env()
        rules = butler_rules()
        for words in ("当他没看过别处", "T 号放在后面的括号里，如“迁移账单（T432）”", "数字和结论直接写进回复",
                      "怎么做由你定", "部署、删除、花钱、对外发送、占机器这几类由主人定", "要问时先说你倾向怎么做",
                      "这一轮结束后你不会自己醒来", "不要说“稍后”“等下”", "派进话题再结束"):
            self.assertIn(words, rules)
        self.assertEqual(rules.count("你不会自己醒来"), 1)
        self.env(**{BUTLER_RULES_ENV: "1"})
        self.assertIn("这一轮结束后你不会自己醒来", butler_rules())

    def test_the_knowledge_rules_still_follow_the_butler_rule(self):
        self.env(**{BUTLER_RULES_ENV: "1", knowledge.ENV: "1"})
        rules = butler_rules().split("\n")
        start = 1 + len(AREA_OPENING)  # After the heading and the opening lines.
        self.assertEqual(rules[start:start + 2], list(BUTLER_RULES))
        self.assertTrue(any("memory_save 记一条" in line for line in rules))  # The knowledge rule, in the memory tools (T870).
        self.assertEqual(rules[0], "[管家规则]")


class ButlerToolsTest(unittest.TestCase):
    """topic_start's description follows the rules (2026-11-15, B1): with COVEY_BUTLER_RULES=1 it no longer says the butler
     does a minute or two's reading and commands itself."""

    def start(self, tools):
        return next(tool for tool in tools if tool["name"] == "topic_start")

    def test_the_variable_is_the_same_one_the_rules_read(self):
        self.assertEqual(TOOLS_BUTLER_ENV, BUTLER_RULES_ENV)

    def test_with_the_switch_off_the_list_is_the_one_before_it_byte_for_byte(self):
        for value in ("0", "off", "false", "no"):
            environ = {**TOOLS_OFF, BUTLER_RULES_ENV: value}
            self.assertIs(listed(environ), TOOLS, value)
            self.assertIn(START_OWN, self.start(listed(environ))["description"])
        for environ in ({}, {BUTLER_RULES_ENV: ""}, {BUTLER_RULES_ENV: "true"}):  # On: unset, empty, anything else.
            self.assertIn(START_BUTLER, self.start(listed(environ))["description"], environ)

    def test_with_the_switch_on_only_topic_starts_description_changes(self):
        on = listed({**TOOLS_OFF, BUTLER_RULES_ENV: "1"})
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
        off = listed({"LARK_BRIDGE_MAIN_BUTLER": "0", "LARK_BRIDGE_KNOWLEDGE": "0", "LARK_BRIDGE_PROJECTS": "0"})
        self.assertIs(off, TOOLS)

    def test_the_bridge_hands_the_variable_to_the_mcp_server_only_when_it_has_it(self):
        def env():
            return butler.mcp_config(Path("/r"), Path("/s"), "job-1", "/usr/bin/python3")["mcpServers"]["bridge"]["env"]
        self.env_patch = mock.patch.dict(os.environ, CLEAN)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name in cenv.spellings("COVEY_BUTLER_RULES"):
            os.environ.pop(name, None)  # Unset: the Bridge has nothing to hand on, the server reads it as on.
        plain = env()
        self.assertNotIn("COVEY_BUTLER_RULES", plain)
        self.assertNotIn("LARK_BRIDGE_MAIN_BUTLER", plain)
        with mock.patch.dict(os.environ, {BUTLER_RULES_ENV: "1"}):
            handed = env()
        self.assertEqual((handed["COVEY_BUTLER_RULES"], handed["LARK_BRIDGE_MAIN_BUTLER"]), ("1", "1"))
        self.assertEqual({k: v for k, v in handed.items() if k not in cenv.spellings("COVEY_BUTLER_RULES")}, plain)
        self.assertIn(START_BUTLER, self.start(listed(handed))["description"])


if __name__ == "__main__":
    unittest.main()
