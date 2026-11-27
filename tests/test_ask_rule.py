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
    "要不要删掉旧的 worktree？", "回滚到上一版吗？", "推到 stable 通道吗？", "要通知同事王钰吗？", "结果发到群里吗？",
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


if __name__ == "__main__":
    unittest.main()
