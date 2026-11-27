"""提问口径 (W0-6, T892 第 4 节): when a turn asks the owner is said once (core/checkpoint.py ASK_RULE), with which memory may
let it go on without asking (ASK_GRANT); every list of the five kinds a prompt gives is ASK_SHORT or ASK_KINDS; the hard gate
of a project's questions (core/project_gates.py) stops the questions of the five kinds the review found it let through; the
short codes the prompts give as examples are codes (core/memstore.py CODE_RE); Codex's developer instructions no longer
call request_user_input a question asked while the work goes on; the butler's rules give the dispatch threshold once."""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from covey.app import config_basis, project_lead, prompts
from covey.backends.codex.appserver import CODEX_INSTRUCTIONS
from covey.core import butler_tool_specs, checkpoint, memory_search, memstore, owner_send, project_gates
from covey.core.checkpoint import ASK_GRANT, ASK_KINDS, ASK_RULE, ASK_SHORT

# T892 D5 (Fable) and section 1.1 row 17: questions the owner must decide that the gate held for the lead, one of each kind
# the review named (deleting a worktree, rolling back, pushing to stable, telling a colleague, posting to a group, buying a
# plan, a training job, a service that keeps running, a forced push) and the rest of ASK_KINDS.
OWNERS = (
    "要不要删掉旧的 worktree？", "回滚到上一版吗？", "推到 stable 通道吗？", "要通知同事张三吗？", "结果发到群里吗？",
    "要不要买 Pro 套餐？", "现在提交训练任务吗？", "要起一个常驻服务盯着吗？", "要强推覆盖远端分支吗？", "要覆盖别人的改动吗？",
    "直接改线上数据吗？", "以主人身份发消息给对方吗？", "要长时间占 GPU 跑一晚吗？", "要大量下载 500GB 数据集吗？",
)
# Questions of the kind a lead answers: none of them may hit (the words of the new list said whole, the act named).
ORDINARY = (
    "worktree 建在哪个目录？", "排序用 stable sort 还是快排？", "测试用例放哪个文件？", "通知卡片的样式用哪种？", "要不要加失败告警？",
    "日志级别用 info 还是 debug？", "这个分支名叫什么？", "下载进度条怎么显示？", "改成二选一的问题卡吗？", "要不要先读 README？",
)


def asked(text):
    return [{"question": text, "options": [{"label": "是"}, {"label": "否"}]}]


class GateWordsTest(unittest.TestCase):
    def test_every_question_of_the_five_kinds_the_review_found_hits(self):
        self.assertEqual(len(OWNERS), 14)
        missed = [text for text in OWNERS if not project_gates.hit(asked(text))]
        self.assertEqual(missed, [])

    def test_the_words_hit_in_an_options_description_too(self):
        question = [{"question": "怎么处理旧目录？", "options": [{"label": "A", "description": "清掉旧的 worktree"}]}]
        self.assertTrue(project_gates.hit(question))

    def test_questions_that_ask_none_of_them_pass(self):
        hits = {text: project_gates.hits(asked(text)) for text in ORDINARY}
        self.assertEqual({text: found for text, found in hits.items() if found}, {})


# The acceptance list's 14 questions (WAVE12 §5.1 W0-6), as the Wave 0 review wrote them twice (wave0-verify-fable.md, P1-1:
# 10 of 14 hit then), and its 3 that ask none of the five, likewise.
SPEC_OWNERS = (
    "要不要删掉旧的 worktree", "旧分支删了还是留着", "要不要回滚到上一版", "重启 Bridge 服务吗", "推到现网吗", "推 stable 通道吗",
    "Proceed with deployment now?", "通知张三升级吗", "结果发到群里吗", "给他发邮件吗", "买 Pro 套餐吗", "提交训练任务（8 卡 × 12 小时）吗",
    "起一个常驻服务吗", "强推覆盖远端分支吗",
    "要不要删掉旧的 worktree？", "要删旧分支吗？", "回滚到上一版吗？", "现在重启 Bridge 吗？", "要推到现网吗？", "推到 stable 通道吗？",
    "要通知同事张三吗？", "要发邮件给对方吗？", "要买 Pro 套餐吗？", "要申请 8 卡×12h 训练吗？", "要起一个常驻服务吗？", "要强推覆盖远端分支吗？",
)
SPEC_ORDINARY = ("用哪个方案", "测试要不要补", "标题用哪种写法", "选方案 A 还是 B？", "要不要补测试？", "起个标题？")


class SpecQuestionsTest(unittest.TestCase):
    def test_the_fourteen_questions_hit_both_ways_they_were_written(self):
        from covey.core import ask_kinds

        self.assertEqual(len(SPEC_OWNERS), 26)  # The second writing shares two of the first word for word.
        self.assertEqual([text for text in SPEC_OWNERS if not project_gates.hit(asked(text))], [])
        self.assertEqual([text for text in SPEC_OWNERS if ask_kinds.question_kind(asked(text)[0]) not in ask_kinds.HIGH_RISK], [])

    def test_the_three_that_ask_none_of_them_pass(self):
        self.assertEqual({text: project_gates.hits(asked(text)) for text in SPEC_ORDINARY if project_gates.hits(asked(text))}, {})

    def test_the_gate_reads_a_questions_context(self):
        question = {"question": "选哪个？", "context": "选 A 要把旧服务删掉", "options": [{"label": "A"}, {"label": "B"}]}
        self.assertIn("选 A 要把旧服务删掉", project_gates.texts([question]))
        self.assertTrue(project_gates.hit([question]))

    def test_a_question_that_declares_a_kind_of_the_owners_hits_with_no_word(self):
        for kind, word in (("deploy", "部署"), ("send", "对外发送"), ("owner_asked", project_gates.DECLARED["owner_asked"])):
            with self.subTest(kind=kind):
                self.assertEqual(project_gates.hit([{"question": "选哪个？", "kind": kind, "options": [{"label": "A"}]}]), word)
        for kind in ("blocked", "other", "", "urgent"):
            with self.subTest(kind=kind):
                self.assertEqual(project_gates.hit([{"question": "选哪个？", "kind": kind, "options": [{"label": "A"}]}]), "")
        # Raised, never lowered: words of the five hit whatever the question declares.
        self.assertEqual(project_gates.hit([{"question": "要删除旧分支吗？", "kind": "blocked", "options": [{"label": "是"}]}]), "删除")

    def test_what_is_not_of_the_five_stays_in_the_gate(self):
        for text, word in (("要额度吗？", "额度"), ("把密钥写进去吗？", "密钥"), ("用 sudo 装吗？", "sudo"), ("现在申请配额吗？", "配额")):
            with self.subTest(text=text):
                self.assertIn(word, project_gates.hits(asked(text)))
        from covey.core import ask_kinds

        self.assertEqual(ask_kinds.classify("用 sudo 装吗？"), "")


# The prompts a turn, the butler or a lead reads: every list of the five kinds in them.
def prompt_texts(environ=None):
    with mock.patch.dict(os.environ, environ or {}):
        texts = [checkpoint.checkpoint_rules(), prompts.butler_rules(), prompts.RULES_GUIDE, *prompts.MEMORY_RULES,
                 *project_lead.TOOL_RULES, *project_lead.TRIAGE_RULES, CODEX_INSTRUCTIONS, owner_send.RULE]
        texts += [tool["description"] for tool in (*butler_tool_specs.TOOLS, *butler_tool_specs.PROJECT_TOOLS)]
    return texts


class FiveKindsOneSourceTest(unittest.TestCase):
    def test_every_list_of_the_five_kinds_is_ask_short_or_ask_kinds(self):
        for environ in ({}, {"COVEY_BUTLER_RULES": "1", "COVEY_PROJECTS": "1"}):
            for text in prompt_texts(environ):
                rest = text.replace(ASK_KINDS, "").replace(ASK_SHORT, "")
                for word in ("花钱", "占机器", "删东西"):
                    self.assertNotIn(word, rest, text[:80])

    def test_the_memory_guide_names_all_five(self):
        self.assertIn(ASK_SHORT, prompts.RULES_GUIDE)
        self.assertIn("占机器", prompts.RULES_GUIDE)

    def test_the_rule_says_which_memory_lets_a_turn_go_on_without_asking(self):
        self.assertIn(ASK_GRANT, ASK_RULE)
        self.assertIn(ASK_RULE, checkpoint.checkpoint_rules())
        for words in ("主人亲口立的", "只放行它写明的那一类事", "删除和其他撤不回的操作永远照常问", "标“负责人记的”的规矩",
                      "自动学到的", f"不能放行{ASK_SHORT}", "报一句"):
            self.assertIn(words, ASK_GRANT)
        # Before the rest of the rule: what may be done without asking comes after what must be asked.
        self.assertLess(ASK_RULE.index(ASK_KINDS), ASK_RULE.index(ASK_GRANT))


# A code as the prompts show one: R, D or N, a digit and more of the code's letters (R7K2M, R6M64).
EXAMPLE = re.compile(r"(?<![0-9A-Za-z_])[RDN][0-9][0-9A-Z]{2,6}(?![0-9A-Za-z_])")


class ExampleCodesTest(unittest.TestCase):
    def test_every_example_code_is_a_code(self):
        texts = [prompts.MEMORY_FIX_RULE, prompts.RULES_GUIDE, prompts.RULES_SEARCH, config_basis.MISSING,
                 *(tool["description"] for tool in (*butler_tool_specs.TOOLS, *butler_tool_specs.PROJECT_TOOLS)),
                 Path(memory_search.__file__).read_text(encoding="utf-8")]  # Its lookup command's help.
        found = {code for text in texts for code in EXAMPLE.findall(text)}
        self.assertLessEqual({"R7K2M"}, found)
        self.assertEqual({code for code in found if not memstore.CODE_RE.match(code)}, set())


class CodexInstructionsTest(unittest.TestCase):
    def test_request_user_input_is_not_called_a_question_asked_while_working(self):
        for words in ("边做边问", "非阻塞"):
            self.assertNotIn(words, CODEX_INSTRUCTIONS)
        self.assertIn("停下这一轮", CODEX_INSTRUCTIONS)
        self.assertIn("什么时候问主人", CODEX_INSTRUCTIONS)
        self.assertIn("合成一次问", CODEX_INSTRUCTIONS)


class CodexInstructionsByLimitTest(unittest.TestCase):
    """W2-A: Codex's developer instructions by the turn's TOOL_LIMIT (backends/codex/appserver.py codex_instructions)."""

    def instructions(self, limit=None, **options):
        from types import SimpleNamespace
        from covey.backends.codex.appserver import codex_instructions
        from covey.core.harness import TOOL_LIMIT
        return codex_instructions(SimpleNamespace(options={**({TOOL_LIMIT: limit} if limit else {}), **options}))

    def test_an_ordinary_turn_has_them_all(self):
        from covey.core.harness import PROXY
        self.assertEqual(self.instructions(), CODEX_INSTRUCTIONS)
        self.assertEqual(self.instructions(PROXY), CODEX_INSTRUCTIONS)

    def test_a_consult_turn_asks_nobody(self):
        from covey.backends.codex.appserver import SUBAGENTS
        from covey.core.harness import CONSULT
        found = self.instructions(CONSULT)
        self.assertEqual(found, SUBAGENTS)
        for words in ("request_user_input", "检查点", "问主人", "问题卡"):
            self.assertNotIn(words, found)

    def test_a_lead_asks_with_its_bridge_tool_and_has_no_checkpoint_contract(self):
        from covey.backends.codex.appserver import LEAD_ASK_TOOL
        from covey.core.harness import LEAD
        found = self.instructions(LEAD)
        self.assertIn(LEAD_ASK_TOOL.strip(), found)
        self.assertIn("不写检查点", found)
        for words in ("requires_user", "按检查点约定"):
            self.assertNotIn(words, found)
        self.assertTrue("project_ask" in butler_tool_specs.PROJECT_NAMES or "project_note" in found)

    def test_a_members_read_only_turn_writes_no_checkpoint(self):
        from covey.core.harness import READ
        found = self.instructions(READ)
        self.assertIn("检查点不用写", found)
        self.assertNotIn("requires_user", found)

    def test_a_look_turn_says_where_it_can_write(self):
        from covey.backends.codex.appserver import LOOK_HELD_WRITE, LOOK_NO_WRITE
        from covey.core.harness import LOOK, LOOK_ROOTS
        held = self.instructions(LOOK, **{LOOK_ROOTS: {"file": "/x/cp.json", "dir": "/x/out", "held": True}})
        self.assertIn(LOOK_HELD_WRITE, held)
        self.assertIn("requires_user", held)
        words = self.instructions(LOOK, **{LOOK_ROOTS: {"file": "/x/cp.json", "dir": "/x/out", "held": False}})
        self.assertIn(LOOK_NO_WRITE, words)
        self.assertNotIn("requires_user", words)


class WordingTest(unittest.TestCase):
    def test_the_dispatch_threshold_is_said_once_in_each_mode(self):
        self.assertIn(butler_tool_specs.DISPATCH_BUTLER, prompts.BUTLER_RULES[0])
        self.assertNotIn("预计超过半分钟", prompts.BUTLER_RULES[1])
        self.assertIn(butler_tool_specs.DISPATCH_BUTLER, butler_tool_specs.START_BUTLER)
        self.assertIn(butler_tool_specs.DISPATCH_OWN, prompts.SHARED_RULES[1])
        self.assertIn(butler_tool_specs.DISPATCH_OWN, butler_tool_specs.START_OWN)

    def test_the_review_leftovers_are_gone(self):
        self.assertNotIn("评审结果", prompts.RESUME_REMINDERS)
        self.assertNotIn("评审结果", prompts.constraints("claude", "权限模式：bypass"))
        self.assertNotIn("下面列出", owner_send.RULE)
        self.assertIn("不要再起一张一样的", owner_send.OPEN_HEAD)

    def test_the_wait_line_names_the_short_wait_and_says_revisit_only_where_one_is_kept(self):
        # W2-B: a wait past SHORT_WAIT_MINUTES is a revisit only in an ordinary topic with 到点回来看 on (app/runs.py waits_of),
        # the lead's next turn in a project's thread; no other turn is told of either.
        line = f"预计 {prompts.SHORT_WAIT_MINUTES} 分钟内出结果"
        ordinary = prompts.constraints("claude", waits=prompts.WAIT_REVISIT)
        member = prompts.constraints("claude", waits=prompts.WAIT_LEAD)
        other = prompts.constraints("claude")
        for rules in (ordinary, member, other):
            self.assertIn(line, rules)
        self.assertIn("revisit", ordinary)
        self.assertNotIn("revisit", member)
        self.assertIn("交给负责人下一轮", member)
        self.assertNotIn("revisit", other)
        self.assertNotIn("专项", other)
        self.assertNotIn("负责人", other)

    def test_a_topics_wait_follows_the_revisit_switch_and_its_project(self):
        from types import SimpleNamespace
        from covey.app import revisits
        from covey.app.runs import Runs
        ctx, job = SimpleNamespace(state_dir="/x", topic_id="t1"), SimpleNamespace(topic_id="t1", chat_id="oc")
        with mock.patch.object(revisits, "ordinary", return_value=True), \
                mock.patch.object(revisits.records, "enabled", return_value=True):
            self.assertEqual(Runs.waits_of(ctx, job), prompts.WAIT_REVISIT)
        with mock.patch.object(revisits, "ordinary", return_value=True), \
                mock.patch.object(revisits.records, "enabled", return_value=False):
            self.assertEqual(Runs.waits_of(ctx, job), "")
        with mock.patch.object(revisits, "ordinary", return_value=False), \
                mock.patch("covey.core.projects.topic_project", return_value=SimpleNamespace(id="p1")), \
                mock.patch("covey.app.runs.is_butler_topic", return_value=False):
            self.assertEqual(Runs.waits_of(ctx, job), prompts.WAIT_LEAD)
        self.assertEqual(Runs.waits_of(ctx, None), "")


if __name__ == "__main__":
    unittest.main()
