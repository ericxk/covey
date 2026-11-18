"""名词表防回潮 (docs/glossary.md): the words the owner retired do not come back in what he reads or what the model reads.

Four kinds of text are checked against the retired words (``RETIRED``):

(a) every string of every sample in the card golden (tests/golden/cards.json.xz): the words the cards show;
(b) the rules the butler and a project's lead are given, in full, and the wake-up note, and the notes a topic gets when
    work is handed to it;
(c) every tool description the butler and the lead see, with every optional tool listed;
(d) every string literal in covey/ outside docstrings (replies, notices, prompts, card words), so that a reply written
    inline is covered too. ``ALLOWED`` lists the few literals that have to keep an old word, each with the reason.

Comments and docstrings are not checked: the code keeps its English names until the second batch of the rename.
"""
from __future__ import annotations

import ast
import json
import lzma
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from covey.app import butler_handoff, butler_tracks, project_lead, project_loop, projects
from covey.app import prompts
from covey.core import butler_tool_specs, track_ledger

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden" / "cards.json"
PACKED = GOLDEN.with_name("cards.json.xz")

# What is not said any more (docs/glossary.md 名词 · 不再用). ``线程`` covers every sense: the butler, a project's topic and
# the Bridge's own threads, which the docs call 后台线程 and the interface never mentions. ``一路`` and ``多路`` are not words
# of a path (同一路径, 多路径).
RETIRED = re.compile("主线程|线程|话题区|长活|快事|路在做|一路(?!径)|多路(?!径)|Personal Agent|由快事升级|派发活卡片|专项整理|话题摘要")

# (path, the literal as it is written, why it keeps the old word)
ALLOWED = (
    ("covey/app/project_cards.py", "由快事升级（你说了",
     "origin_words: a project record written before 2026-11-16 holds the old origin line; it is shown as the new one"),
    ("covey/app/project_cards.py", r"由快事升级（来回第 (\d+) 轮）",
     "origin_words: the same, for the round-count form of the old origin line"),
    ("covey/core/track_route.py", "新开一路",
     "ALONE_WORDS: words the owner may type to say a message is its own (matched, never shown); old spellings still count"),
)

# Switches that list every optional tool and rule.
ALL_ON = {"COVEY_BUTLER_RULES": "1", "COVEY_KNOWLEDGE": "1", "COVEY_HISTORY_CMD": "/absolute/history", "COVEY_PROJECTS": "1",
          "COVEY_PROJECT_TIDY": "suggest"}
OLD_NAMES = ("LARK_BRIDGE_MAIN_BUTLER", "LARK_BRIDGE_KNOWLEDGE", "LARK_BRIDGE_HISTORY_CMD", "LARK_BRIDGE_PROJECTS")


def strings(value):
    """Every string inside ``value`` (dict values, list items, nested), keys left out."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from strings(item)


def tool_texts(tools):
    """Every description in the tools: the tool's own and each argument's, name and all."""
    for tool in tools:
        yield tool["name"]
        yield from strings(tool.get("description", ""))
        yield from strings(tool.get("input_schema", {}))


def literals(path: Path):
    """(line, text) of the string literals of a module outside docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


class RetiredWordsTest(unittest.TestCase):
    def assertClean(self, texts, where):
        found = []
        for text in texts:
            match = RETIRED.search(text)
            if match:
                found.append(f"{match.group()!r} in {text[max(0, match.start() - 20):match.end() + 20]!r}")
        self.assertEqual(found, [], f"{where}: words docs/glossary.md retired")

    def environment(self, **values):
        patcher = mock.patch.dict(os.environ, {**dict.fromkeys(OLD_NAMES, ""), **values})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_words_themselves_are_matched(self):
        for word in ("主线程", "线程", "话题区", "长活", "快事", "还有 3 路在做", "并行的一路", "多路并行", "Personal Agent",
                     "由快事升级（你说了长活）", "派发活卡片"):
            self.assertIsNotNone(RETIRED.search(word), word)
        for word in ("管家", "话题", "专项的话题", "后台", "同一路径", "还有 3 件事在做", "由管家转成专项"):
            self.assertIsNone(RETIRED.search(word), word)

    # (a)
    def test_no_card_of_the_golden_shows_a_retired_word(self):
        text = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else lzma.decompress(PACKED.read_bytes()).decode("utf-8")
        samples = json.loads(text)
        self.assertGreater(len(samples), 100)
        for name, card in samples.items():
            self.assertClean(strings(card), f"golden sample {name}")

    # (b)
    def test_the_butlers_rules_in_full_use_no_retired_word(self):
        for butler in ("", "1"):
            for extra in ({}, {"COVEY_KNOWLEDGE": "1", "COVEY_HISTORY_CMD": "/absolute/history"}):
                self.environment(**{"COVEY_BUTLER_RULES": butler, "COVEY_KNOWLEDGE": "", "COVEY_HISTORY_CMD": "", **extra})
                rules = prompts.butler_rules()
                self.assertTrue(rules.startswith("[管家规则]"), rules[:20])
                self.assertClean([rules, prompts.butler_message("快照", "消息")], f"butler rules (butler={butler!r}, {sorted(extra)})")
        self.assertClean([*prompts.AREA_OPENING, *prompts.SHARED_RULES, *prompts.BUTLER_RULES, prompts.AREA_RULE], "rule constants")
        self.assertClean(prompts.knowledge_rules(), "knowledge rules")

    def test_the_leads_rules_and_the_wake_up_note_use_no_retired_word(self):
        self.assertClean([project_lead.lead_rules(), *project_lead.IDENTITY, *project_lead.TOOL_RULES], "lead rules")
        for spawn in project_lead.STYLE_CHOICES[project_lead.SPAWN]:
            for report in project_lead.STYLE_CHOICES[project_lead.REPORT]:
                self.assertClean([project_lead.lead_rules({"spawn": spawn, "report": report})], f"lead rules {spawn} {report}")
        self.assertClean([project_loop.WAKE_TEXT, *project_lead.REPORT_WORDS.values()], "wake-up note")
        self.assertClean([projects.lead_prompt("迁移账单", "把预算改成两倍", butler_handoff.Asked(said=(("加预算", 0),)))], "lead prompt")

    def test_what_a_topic_and_a_parallel_turn_are_told_use_no_retired_word(self):
        owner = butler_handoff.Asked(said=(("看看昨晚的失败", 1793790000.0),))
        lead = butler_handoff.Asked(lead="迁移账单")
        notes = []
        for kind in (butler_handoff.START, butler_handoff.NEXT, butler_handoff.STEER, butler_handoff.FORK):
            for who in (owner, lead):
                notes.append(butler_handoff.note(kind, label="T12", title="查日志", owner=who, source="T3"))
                notes.append(butler_handoff.thread_head(kind, who))
        notes.append(butler_handoff.handoff_prompt(butler_handoff.START, label="T12", title="查日志", task="看看昨晚的失败",
                                                 owner=owner))
        notes += [butler_tracks.FORK_NOTE, track_ledger.HEADER]
        self.assertClean(notes, "notes handed on")

    # (c)
    def test_every_tool_description_the_butler_and_the_lead_see_uses_no_retired_word(self):
        for extra in ({}, ALL_ON):
            listed = butler_tool_specs.listed(dict(extra))
            self.assertClean(tool_texts(listed), f"butler tools {sorted(extra)}")
        everything = (*butler_tool_specs.TOOLS, *butler_tool_specs.KNOWLEDGE_TOOLS, *butler_tool_specs.HISTORY_TOOLS,
                      *butler_tool_specs.PROJECT_TOOLS, *butler_tool_specs.TRIAGE_TOOLS, *butler_tool_specs.BUTLER_PROJECT_TOOLS)
        self.assertClean(tool_texts(everything), "all tools")
        self.assertClean(tool_texts(butler_tool_specs.lead_tools()), "lead tools")
        self.assertClean(tool_texts(butler_tool_specs.listed({butler_tool_specs.LEAD_ENV: "p1"})), "lead tools listed")
        self.assertClean(strings([butler_tool_specs.START_PROJECT, butler_tool_specs.SEND_PROJECT]), "project arguments")

    def test_the_tools_say_when_the_butler_passes_project(self):
        """The two arguments say it in plain words: several topics over days, or the owner's own 开个专项 / 转成专项; else not."""
        for argument in (butler_tool_specs.START_PROJECT, butler_tool_specs.SEND_PROJECT):
            text = argument["description"]
            for phrase in ("几个话题", "开个专项", "转成专项", "拿不准"):
                self.assertIn(phrase, text)

    # (d)
    def test_no_string_in_the_code_shows_a_retired_word_except_the_listed_ones(self):
        found, used = [], set()
        for path in sorted((REPO / "covey").rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            for line, text in literals(path):
                match = RETIRED.search(text)
                if not match:
                    continue
                allowed = [entry for entry in ALLOWED if entry[0] == relative and entry[1] in text]
                if allowed:
                    used.update(entry[:2] for entry in allowed)
                else:
                    found.append(f"{relative}:{line}: {match.group()!r} in {text[:60]!r}")
        self.assertEqual(found, [], "string literals with words docs/glossary.md retired (docstrings and comments are not checked)")
        self.assertEqual({entry[:2] for entry in ALLOWED}, used, "an ALLOWED entry no longer matches anything: remove it")

    def test_the_glossary_is_in_the_repo_and_the_agents_file_points_to_it(self):
        self.assertTrue((REPO / "docs" / "glossary.md").is_file())
        self.assertIn("docs/glossary.md", (REPO / "AGENTS.md").read_text(encoding="utf-8")[:400])
        self.assertIn("docs/glossary.md", (REPO / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
