"""名词表防回潮 (docs/glossary.md): the words the owner retired do not come back in what he reads or what the model reads.

Four kinds of text are checked against the retired words (``RETIRED``):

(a) every string of every sample in the card golden (tests/golden/cards.json.xz): the words the cards show;
(b) the rules the butler and a project's lead are given, in full, and the wake-up note, and the notes a topic gets when
    work is handed to it;
(c) every tool description the butler and the lead see, with every optional tool listed;
(d) every string literal in covey/ outside docstrings (replies, notices, prompts, card words), so that a reply written
    inline is covered too. ``ALLOWED`` lists the few literals that have to keep an old word, each with the reason.

Comments and docstrings are not checked: the code keeps its English names until the second batch of the rename.

``PublicDocsTest`` reads what a new user reads (README, CONTRIBUTING and docs/ outside docs/history/) for what only the
owner's own work left there: topic numbers of real topics (examples use one or two digits, T12) and dates of how a rule
came about (git log keeps those). Internal names are scripts/privacy_check.py's, with the deployment's word list.
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

from covey.app import butler_handoff, butler_projects, butler_tracks, project_lead, project_loop
from covey.app import prompts
from covey.core import butler_tool_specs, look, track_ledger

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden" / "cards.json"
PACKED = GOLDEN.with_name("cards.json.xz")

# What is not said any more (docs/glossary.md 名词 · 不再用). ``线程`` covers every sense: the butler, a project's topic and
# the Bridge's own threads, which the docs call 后台线程 and the interface never mentions. ``一路`` and ``多路`` are not words
# of a path (同一路径, 多路径).
# 长期约定, 当前决定, 待核对参考 (the memory's older parts), 记忆中心 and 仓库地图 went with the memory's and the repo map's new words.
RETIRED = re.compile("主线程|线程|话题区|长活|快事|路在做|一路(?!径)|多路(?!径)|Personal Agent|由快事升级|派发活卡片|专项整理|话题摘要|约好回来看|该回来看了"
                     "|长期约定|当前决定|待核对参考|记忆中心|仓库地图")

# The status words docs/glossary.md 状态词 retired (要换掉的说法, core/look.py RETIRED), each as a word of its own: not inside a
# longer one (完成标准, 发送失败, 打断). They are checked where a state is said and the module is clean of them: the global
# cards (T647 P0-1, B8: the 看板, 手上的事, the 专项卡, the status card and the 派发卡), then the conversation's cards and what
# the butler and a turn read of a turn (B8's second batch: the task card, its 执行过程 and status line, the question and
# recovery cards, the answer card, 换配置 and its handoff, the 设置 card's 能做什么 and 记忆), their golden samples and the code
# that draws them. Left out: an error a command or a check gave (git diff 失败, 创建 worktree 失败, agy models 失败, 启动检查失败),
# core/look.py's own RETIRED table, and modules a parallel change owns now (the web page's, the installer's).
STATUS_WORDS = sorted({re.sub(r"[^一-鿿]", "", word) for words in look.RETIRED.values() for word in words} - {""},
                      key=len, reverse=True)
STATUS_RETIRED = re.compile(r"(?<![一-鿿])(" + "|".join(STATUS_WORDS) + r")(?![一-鿿])")
STATUS_MODULES = ("covey/cards/board.py", "covey/cards/overview.py", "covey/cards/project_card.py", "covey/cards/status_card.py",
                  "covey/cards/dispatch_card.py", "covey/app/digest.py", "covey/app/overview.py", "covey/app/pins.py",
                  "covey/app/project_facts.py", "covey/app/status_cards.py", "covey/app/dispatch_cards.py",
                  "covey/core/board_read.py",
                  # B8's second batch.
                  "covey/cards/trace_view.py", "covey/cards/streaming.py", "covey/cards/task_status.py", "covey/core/status_line.py",
                  "covey/core/checkpoint.py", "covey/core/turn_words.py", "covey/core/progress.py", "covey/core/text.py",
                  "covey/core/failure.py", "covey/app/job_dispatch.py", "covey/app/recovery_cards.py", "covey/cards/recovery.py",
                  "covey/app/diag_summary.py", "covey/app/subagent_recovery.py", "covey/app/butler_search.py",
                  "covey/core/butler_mcp.py", "covey/core/track_ledger.py", "covey/core/collab.py", "covey/backends/claude/handoff.py",
                  "covey/cards/question_cards.py", "covey/app/question_display.py", "covey/cards/answer_card.py",
                  "covey/cards/review.py", "covey/app/butler_reply.py", "covey/app/switch_handoff.py", "covey/app/harness_switch.py",
                  "covey/cards/switch_form.py", "covey/app/about_panel.py", "covey/cards/about_panel.py",
                  "covey/app/memory_panel.py", "covey/cards/memory_panel.py", "covey/app/memory_collect.py")
STATUS_SAMPLES = ("digest.board", "overview.", "project.", "status_card.", "task.", "trace.", "main_task.", "question.",
                  "recovery.", "switch.", "settings.about", "settings.memory")

# (path, the literal as it is written, why it keeps the old word)
ALLOWED = (
    ("covey/app/project_facts.py", "由快事升级（你说了",
     "origin_words: a project record written before 2026-11-16 holds the old origin line; it is shown as the new one"),
    ("covey/app/project_facts.py", r"由快事升级（来回第 (\d+) 轮）",
     "origin_words: the same, for the round-count form of the old origin line"),
    ("covey/core/look.py", "约好回来看",
     "RETIRED: the table of words the surfaces no longer say; the web page's test reads it from look.json"),
    ("covey/core/look.py", "该回来看了",
     "RETIRED: the same"),
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


# What a public doc does not say, outside fenced code blocks and inline code: (name, pattern, what to write instead).
NOT_IN_DOCS = (
    ("topic number", re.compile(r"\bT\d{3,}\b"), "an example number of one or two digits (T12), or no number"),
    ("date", re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b"), "the rule as it is now; how it came about is in git log"),
    ("short date", re.compile(r"(?<![\w./:#-])(?:0?[1-9]|1[0-2])-[0-3]\d(?![\w:./%-])"),
     "the rule as it is now (a range of numbers: write 10 到 20)"),
)
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")


def public_docs():
    """The Markdown a new user reads: the READMEs, CONTRIBUTING and docs/ (docs/history/ is the record, not read)."""
    docs = [REPO / name for name in ("README.md", "README.en.md", "CONTRIBUTING.md")]
    return [path for path in docs if path.exists()] + sorted((REPO / "docs").glob("*.md"))


def prose(text):
    """(line, text) of a Markdown file outside fenced code blocks, with inline code taken out."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, INLINE_CODE.sub("", line)


class PublicDocsTest(unittest.TestCase):
    def test_the_patterns_match_what_they_are_for(self):
        def names(text):
            return [name for name, pattern, _ in NOT_IN_DOCS if pattern.search(text)]
        self.assertEqual(names("（T574，主人 2026-11-20）"), ["topic number", "date"])
        self.assertEqual(names("主人 11-19：文字太密"), ["short date"])
        for clean in ("放到 T42「标题」里做", "数据约定 v17", "12:31 的问题", "第 3 轮", "#1084", "cli_a1-20", "3/5 步",
                      "Python 3.9", "1.0.12", "75%-90%"):
            self.assertEqual(names(clean), [], clean)
        self.assertEqual(list(prose("a T574\n```\n[2026-10-26T19:31] log\n```\nb `T574` c")), [(1, "a T574"), (5, "b  c")])

    def test_no_public_doc_carries_topic_numbers_or_dates_of_how_it_came_about(self):
        found = []
        for path in public_docs():
            for number, line in prose(path.read_text(encoding="utf-8")):
                for name, pattern, instead in NOT_IN_DOCS:
                    for match in pattern.finditer(line):
                        found.append(f"{path.relative_to(REPO)}:{number}: {name} {match.group()!r} "
                                     f"in {line[max(0, match.start() - 20):match.end() + 20]!r}; write {instead}")
        self.assertGreater(len(public_docs()), 10)
        self.assertFalse(found, "\n".join(found))


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
                     "由快事升级（你说了长活）", "派发活卡片", "长期约定", "当前决定", "待核对参考", "网页记忆中心", "仓库地图"):
            self.assertIsNotNone(RETIRED.search(word), word)
        for word in ("管家", "话题", "专项的话题", "后台", "同一路径", "还有 3 件事在做", "由管家转成专项", "规矩", "决定", "负责人记的",
                     "网页的「记忆」页", "常用路径"):
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
        for detail in project_lead.STYLE_CHOICES[project_lead.DETAIL]:
            for report in project_lead.STYLE_CHOICES[project_lead.REPORT]:
                self.assertClean([project_lead.lead_rules({"detail": detail, "report": report})], f"lead rules {detail} {report}")
        self.assertClean([project_loop.WAKE_TEXT, *project_lead.REPORT_WORDS.values()], "wake-up note")
        self.assertClean([butler_projects.lead_prompt("迁移账单", "把预算改成两倍", butler_handoff.Asked(said=(("加预算", 0),)))], "lead prompt")

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
        everything = (*butler_tool_specs.TOOLS, *butler_tool_specs.HISTORY_TOOLS,
                      *butler_tool_specs.PROJECT_TOOLS, *butler_tool_specs.TRIAGE_TOOLS, *butler_tool_specs.BUTLER_PROJECT_TOOLS)
        self.assertClean(tool_texts(everything), "all tools")
        self.assertClean(tool_texts(butler_tool_specs.lead_tools()), "lead tools")
        self.assertClean(tool_texts(butler_tool_specs.listed({butler_tool_specs.LEAD_ENV: "p1"})), "lead tools listed")
        self.assertClean(strings([butler_tool_specs.START_PROJECT, butler_tool_specs.SEND_PROJECT]), "project arguments")

    def test_the_butler_reads_the_boards_status_words(self):
        """#48: the words the butler reads are the words it says back. Its snapshot, topics_list and topic_read said
        等你回答 / 有结果未看 / 进行中 and 完成 / 失败 / 中断 while the cards say 等你 / 有结果没看 / 在做 and 做完 / 停住了
        (docs/ui.md 3, core/look.py STATES), so it told the owner "T12 失败了" of a card that said 停住了."""
        from covey.core import look, turn_words
        from covey.app import butler_tools
        self.assertEqual([turn_words.GROUP_NAMES[key] for key in ("waiting", "result", "running")],
                         [look.word("waiting"), look.group_word("result"), look.word("running")])
        self.assertEqual(turn_words.RESULTS, {"done": look.word("done"), "failed": look.word("stopped"),
                                              "interrupted": look.word("stopped"), "cancelled": look.word("cancelled")})
        self.assertEqual(butler_tools.STEP_STATES["done"], look.word("done"))
        retired = ("等你回答", "需要你回答", "有结果未看", "做完没看", "进行中", "已完成", "执行失败", "已中断")
        self.environment(**ALL_ON)
        everything = (*butler_tool_specs.TOOLS, *butler_tool_specs.HISTORY_TOOLS,
                      *butler_tool_specs.PROJECT_TOOLS, *butler_tool_specs.TRIAGE_TOOLS, *butler_tool_specs.BUTLER_PROJECT_TOOLS)
        texts = [*tool_texts(everything), prompts.butler_rules(), project_lead.lead_rules(),
                 *turn_words.GROUP_NAMES.values(), *turn_words.RESULTS.values()]
        found = [(word, text[:40]) for text in texts for word in retired if word in text]
        self.assertEqual(found, [])

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

    def test_the_global_cards_say_no_retired_status_word(self):
        """B8 (T647): the 看板, 手上的事, the 专项卡, the status card and the 派发卡, then the conversation's cards (the task
        card and its 执行过程, the question, recovery and answer cards, 换配置's handoff, 能做什么 and 记忆) say a state in
        core/look.py STATES' words (停住了, 已停止, 做完, 等你, 在做 ...), in what they show (the golden samples) and in the code
        that draws them and what the butler reads of a turn."""
        for word in ("失败", "已完成", "执行失败", "等你回答：推哪几个？", "进行中", "已中断。", "⚠️ 失败 · 后端退出"):
            self.assertIsNotNone(STATUS_RETIRED.search(word), word)
        for word in ("完成标准", "发送失败", "停住了", "已停止", "做完", "等你", "要你定：推哪几个？", "被停止了", "重启打断"):
            self.assertIsNone(STATUS_RETIRED.search(word), word)
        text = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else lzma.decompress(PACKED.read_bytes()).decode("utf-8")
        samples = {name: card for name, card in json.loads(text).items() if name.startswith(STATUS_SAMPLES)}
        self.assertGreater(len(samples), 250)
        found = [f"{name}: {match.group()!r} in {text[max(0, match.start() - 20):match.end() + 20]!r}"
                 for name, card in samples.items() for text in strings(card) for match in STATUS_RETIRED.finditer(text)]
        self.assertEqual(found, [], "golden samples of the global cards with a status word docs/glossary.md retired")
        found = [f"{relative}:{line}: {match.group()!r} in {text[:60]!r}" for relative in STATUS_MODULES
                 for line, text in literals(REPO / relative) for match in STATUS_RETIRED.finditer(text)]
        self.assertEqual(found, [], "string literals of the global cards with a status word docs/glossary.md retired")

    def test_the_glossary_is_in_the_repo_and_the_agents_file_points_to_it(self):
        self.assertTrue((REPO / "docs" / "glossary.md").is_file())
        self.assertIn("docs/glossary.md", (REPO / "AGENTS.md").read_text(encoding="utf-8")[:400])
        self.assertIn("docs/glossary.md", (REPO / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
