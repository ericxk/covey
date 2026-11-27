"""照常要问主人的五类事 in one place (core/ask_kinds.py, WAVE12 C3): the names core/checkpoint.py gives every prompt are
its own, character for character; ``classify`` says what app/question_dedup.py's five classes say; ``question_kind``
lets a declared kind raise a question to the owner's and never lower one its words make his."""
from __future__ import annotations

import sys
import unittest

from support import REPO
from support.source_imports import load_time_imports

# What core/checkpoint.py wrote out before the five moved (b4d687c4): the prompts must not change by a character.
SHORT_BEFORE = "部署、删除、花钱、对外发送、占机器"
LONG_BEFORE = ("部署上线和发布、删除和其他撤不回的操作（覆盖别人的改动、改线上数据、强推）、花钱、"
               "对外发送（以主人身份发消息、发到别人看得到的地方）、占机器（长时间占 GPU 或集群、起长期运行的服务、大量下载或写盘）")
TEXTS = (
    "部署到现网", "Proceed with deployment now?", "redeploy the service", "推 stable 通道", "release 一个新版本", "发版",
    "删除旧分支", "删掉 worktree", "rm -rf build", "git push --force", "drop table", "回滚到上一代", "覆盖别人的改动",
    "买 Pro 套餐", "开通付费版", "预算多少", "通知同事一声", "以主人身份发消息", "发邮件给他", "群里说一下",
    "申 8 卡跑 12 小时训练任务", "起常驻服务", "占用 GPU", "大量下载数据集", "长期运行的守护",
    "合进主干", "选哪个样张", "补一个测试", "选方案 A", "perform the form", "dropdown 的样式", "用 grep 查", "",
    "先删除旧分支再部署", "部署后通知同事",
)


class NamesTest(unittest.TestCase):
    def test_the_short_and_the_long_names_are_the_ones_every_prompt_gave_before(self):
        from covey.core import ask_kinds, checkpoint

        self.assertEqual(ask_kinds.SHORT, SHORT_BEFORE)
        self.assertEqual(ask_kinds.LONG, LONG_BEFORE)
        self.assertEqual((checkpoint.ASK_SHORT, checkpoint.ASK_KINDS), (SHORT_BEFORE, LONG_BEFORE))
        self.assertEqual([kind.key for kind in ask_kinds.KINDS], ["deploy", "delete", "spend", "send", "occupy"])
        self.assertEqual([kind.short for kind in ask_kinds.KINDS], SHORT_BEFORE.split("、"))

    def test_the_kinds_of_a_question(self):
        from covey.core import ask_kinds

        self.assertEqual(ask_kinds.QUESTION_KINDS,
                         ("deploy", "delete", "spend", "send", "occupy", "owner_asked", "blocked", "other"))
        self.assertEqual(ask_kinds.HIGH_RISK, {"deploy", "delete", "spend", "send", "occupy"})
        self.assertEqual(ask_kinds.MUST_OWNER, {"deploy", "delete", "spend", "send", "occupy", "owner_asked"})
        self.assertEqual(set(ask_kinds.ORDER), ask_kinds.HIGH_RISK)

    def test_it_needs_nothing_but_the_standard_library(self):
        path = REPO / "covey" / "core" / "ask_kinds.py"
        self.assertEqual([m for m, _ in load_time_imports(path) if m.split(".")[0] not in sys.stdlib_module_names
                          and m != "__future__"], [])


class ClassifyTest(unittest.TestCase):
    def test_it_says_what_the_five_classes_of_question_dedup_say(self):
        from covey.app import question_dedup
        from covey.core import ask_kinds

        for text in TEXTS:
            with self.subTest(text=text):
                said = question_dedup.category(text) if text else "other"
                self.assertEqual(ask_kinds.classify(text), said if said in ask_kinds.HIGH_RISK else "")

    def test_english_stems_match_longer_words_and_short_words_only_whole(self):
        from covey.core.ask_kinds import classify

        self.assertEqual([classify(t) for t in ("Redeployment", "a RELEASE", "rm -rf /x", "drop it", "forced push")],
                         ["deploy", "deploy", "delete", "delete", "delete"])
        self.assertEqual([classify(t) for t in ("perform", "dropdown", "", None, 3)], ["", "", "", "", ""])
        self.assertEqual(classify("先删除旧分支再部署"), "delete")  # Deleting first, as app/question_dedup.py orders them.


class WordsTest(unittest.TestCase):
    """P1-1 / F3 (Wave 0 review): the words of both lists before (app/question_dedup.py's classes, core/project_gates.py's
    gate) in one, English verbs in their longer forms, and the plain words that are part of an ordinary one left out."""

    def test_english_verbs_in_their_longer_forms_and_beside_chinese(self):
        from covey.core.ask_kinds import classify

        cases = {"Proceed with deployment now?": "deploy", "deploying it": "deploy", "deployed yet?": "deploy",
                 "redeploy": "deploy", "published?": "deploy", "released?": "deploy", "要deploy吗": "deploy",
                 "Deploy to production?": "deploy", "deletion of the cache": "delete", "deleting logs": "delete",
                 "a rollback": "delete", "roll back": "delete", "git reset --hard": "delete", "git branch -D old": "delete",
                 "dropped the table": "delete", "purchase a plan": "spend", "send an email": "send", "notify them": "send",
                 "scale up to 4": "occupy", "nohup ./run": "occupy"}
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify(text), kind)

    def test_plain_words_that_are_part_of_an_ordinary_one_say_nothing(self):
        from covey.core.ask_kinds import classify

        for text in ("排序用 stable sort 还是快排？", "覆盖检查加在第几步？", "通知卡片的样式用哪种？", "买家的头像放哪？", "常驻内存多大？",
                     "执行 tailscale up 吗？", "enforce the rule?", "perform the form", "dropdown 的样式", "format 用哪种？",
                     "建议先做中小模型服务的评测方案？", "推荐 stable 排序吗？"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "")

    def test_words_in_gives_each_word_as_written_with_its_kind(self):
        from covey.core.ask_kinds import words_in

        self.assertEqual(words_in("Deploy 后通知同事"), [("deploy", "deploy"), ("send", "通知")])
        self.assertEqual(words_in("要删除吗")[0], ("delete", "删除"))  # The word before the single 删.
        self.assertEqual(words_in(""), [])
        self.assertEqual(words_in(None), [])

    def test_question_dedup_and_the_gate_read_these_words(self):
        from covey.app import question_dedup
        from covey.core import ask_kinds

        self.assertIs(question_dedup.HIGH_RISK, ask_kinds.HIGH_RISK)
        self.assertEqual([(name, pattern) for name, pattern in question_dedup.CATEGORIES[:5]],
                         [(key, ask_kinds.PATTERNS[key]) for key in ask_kinds.ORDER])


class QuestionKindTest(unittest.TestCase):
    def test_its_words_raise_it_whatever_it_declares(self):
        from covey.core.ask_kinds import question_kind

        for declared in ("blocked", "other", "owner_asked", "", "spend", None):
            with self.subTest(declared=declared):
                self.assertEqual(question_kind({"question": "要不要部署到现网？", "kind": declared}), "deploy")
        cases = (
            ({"header": "清理", "question": "怎么处理？", "kind": "other"}, "delete"),  # The header.
            ({"question": "选哪个？", "options": [{"label": "A", "description": "清掉旧的 worktree"}], "kind": "blocked"}, "delete"),
            ({"question": "选哪个？", "options": ["A", "通知同事"], "kind": "blocked"}, "send"),  # An option that is a string.
            ({"question": "选哪个？", "context": "这一步要占用 GPU 一整天", "kind": "owner_asked"}, "occupy"),
        )
        for question, kind in cases:
            with self.subTest(question=question):
                self.assertEqual(question_kind(question), kind)

    def test_a_high_risk_question_declared_blocked_is_raised(self):
        """§5.2 W1-A item 8 and RT-5 ①: a question that deletes and deploys, declared ``blocked``, is the owner's."""
        from covey.core.ask_kinds import MUST_OWNER, question_kind

        for text in ("把生产服务删掉并重新部署吗？", "现在删掉旧服务并上线新版本吗？"):
            with self.subTest(text=text):
                kind = question_kind({"kind": "blocked", "question": text})
                self.assertIn(kind, {"delete", "deploy"})
                self.assertIn(kind, MUST_OWNER)
        self.assertEqual(question_kind({"kind": "other", "question": "选哪个？", "context": "选 A 会把线上数据覆盖掉"}), "delete")

    def test_without_such_words_the_declared_kind_counts_and_anything_else_is_other(self):
        from covey.core.ask_kinds import question_kind

        self.assertEqual(question_kind({"question": "选哪个方案？", "kind": "owner_asked"}), "owner_asked")
        self.assertEqual(question_kind({"question": "选哪个方案？", "kind": " blocked "}), "blocked")
        self.assertEqual(question_kind({"question": "选哪个方案？", "kind": "deploy"}), "deploy")  # Declared up: kept.
        for question in ({"question": "选哪个方案？"}, {"question": "选哪个方案？", "kind": "urgent"}, {"kind": 3}, {}, None, 7):
            with self.subTest(question=question):
                self.assertEqual(question_kind(question), "other")
        self.assertEqual(question_kind("要不要强推？"), "delete")  # A question that is its text.


if __name__ == "__main__":
    unittest.main()
