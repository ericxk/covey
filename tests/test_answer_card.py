"""A turn's answer sent on its own goes as a card whose chat-list preview says what the turn came to
(T288, the owner 2026-11-02): the card, its preview, and its send with the Markdown message as fallback."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest import mock

from covey.cards.review import (ANSWER_CARD_BYTES, answer_card, answer_summary, bold_minor_headings,
                                         inline_one_line_code, task_card_summary)
from covey.feishu import transport
from covey.feishu.delivery import safe_idempotency_key, send_message_with_fallback
from covey.feishu.message_request import optimize_markdown
from covey.feishu.transport import last_failure

CARD = answer_card("#### 先确认数据集\n正文", "T304 有结果 · 先确认数据集")


class AnswerCardTests(unittest.TestCase):
    def test_the_card_is_the_answer_with_its_preview(self):
        self.assertEqual(CARD, {"schema": "2.0", "config": {"summary": {"content": "T304 有结果 · 先确认数据集"}},
                                "body": {"elements": [{"tag": "markdown", "content": "#### **先确认数据集**\n正文",
                                                       "text_size": "normal_v2"}]}})

    def test_small_headings_are_bold_as_a_card_shows_only_h1_and_h2_so(self):
        # The 2026-11-02 probe: in a card, ### to ###### looked like the body text; "#### **x**" was bold.
        text = "## 二级\n### 三级 **粗** ###\n```\n### 代码里\n```\n#### C#\n   ##### 缩进\n###\n正文 ### 不是"
        self.assertEqual(bold_minor_headings(text),
                         "## 二级\n### **三级 粗**\n```\n### 代码里\n```\n#### **C#**\n##### **缩进**\n###\n正文 ### 不是")

    def test_the_card_keeps_heading_levels_the_markdown_message_steps_down(self):
        text = "# 结论\n## 细节\n正文"
        self.assertEqual(optimize_markdown(text), "#### 结论\n\n##### 细节\n正文")
        self.assertEqual(optimize_markdown(text, step_headings=False), text)

    def test_an_answer_too_big_for_one_card_message_has_none(self):
        fits = "字" * ((ANSWER_CARD_BYTES - 400) // 3)
        self.assertIsNotNone(answer_card(fits, "✅ T1 · 字"))
        self.assertIsNone(answer_card(fits + "字" * 200, "✅ T1 · 字"))

    def test_a_one_line_code_block_is_inline_code_as_a_card_numbers_a_block_s_lines(self):
        # The 2026-11-02 review read "1 misc-tasks/runs/…/repo-audit-01/" under 记录在：.
        text = "记录在：\n```\nmisc-tasks/runs/a/\n```\n```bash\na\nb\n```\n~~~sh\nrun\n~~~\n```\nhas `tick`\n```\n```\nopen"
        self.assertEqual(inline_one_line_code(text), "记录在：\n`misc-tasks/runs/a/`\n```bash\na\nb\n```\n`run`\n"
                                                     "```\nhas `tick`\n```\n```\nopen")
        self.assertIn("`misc-tasks/runs/a/`", answer_card(text, "✅ T1 · x")["body"]["elements"][0]["content"])

    def test_the_task_card_s_preview_is_the_outcome_and_time_the_answer_card_s_the_lead(self):
        # Both read "✅ T325 · 1102 回流" under the topic in the chat (2026-11-02 review).
        self.assertEqual(task_card_summary("T325", "done", "有结果 · 用时 3 分 39 秒"), "T325 有结果 · 用时 3 分 39 秒")
        self.assertEqual(task_card_summary("#12", "waiting_user"), "#12 等你")
        self.assertEqual(task_card_summary("T3", "failed", "停住了 · 用时 5 秒"), "T3 停住了 · 用时 5 秒")

    def test_the_preview_is_the_outcome_the_topic_and_the_lead(self):
        self.assertEqual(answer_summary("T304", "done", "先确认数据集"), "T304 有结果 · 先确认数据集")
        self.assertEqual(answer_summary("#12", "waiting_user", "选哪个"), "#12 等你 · 选哪个")
        self.assertEqual(answer_summary("T3", "failed", ""), "T3 停住了")
        self.assertEqual(answer_summary("T3", "done", "token=abc123 已换"), "T3 有结果 · token=[已隐藏] 已换")


class CardFirstSendTests(unittest.TestCase):
    """send_message_with_fallback with ``card``: the card first, under its own key; Markdown once Feishu
    refused it outright or Markdown was already tried for the text; nothing else while it may have landed."""

    def setUp(self):
        self.receipts = tempfile.TemporaryDirectory(prefix="answer-card-")
        self.addCleanup(self.receipts.cleanup)
        previous = transport.configure_receipts(self.receipts.name)
        self.addCleanup(transport.configure_receipts, previous)

    def send(self, card_reply, *, message_format="markdown", text="#### 先确认数据集\n正文"):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "--msg-type" in argv:
                return card_reply
            return subprocess.CompletedProcess(argv, 0, '{"data":{"message_id":"om_md"}}', "")

        with mock.patch("covey.feishu.delivery.subprocess.run", side_effect=run), \
                mock.patch("covey.feishu.transport.time.sleep"):
            sent = send_message_with_fallback(
                profile=None, chat_id="oc", text=text, idempotency_key="unit-verified-result-k", card=CARD,
                message_format=message_format, chunk_limit=4000, fallback_chunk_limit=1500, log=lambda _: None,
                fallback_key_prefix="unit", reply_to_message_id="om_root")
        return sent, calls

    @staticmethod
    def flag(argv, name):
        return argv[argv.index(name) + 1]

    def test_the_answer_goes_as_its_card_in_the_topic(self):
        sent, calls = self.send(subprocess.CompletedProcess([], 0, '{"data":{"message_id":"om_card"}}', ""))
        self.assertTrue(sent)
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual((self.flag(argv, "--msg-type"), self.flag(argv, "--message-id")), ("interactive", "om_root"))
        self.assertIn("--reply-in-thread", argv)
        self.assertEqual(json.loads(self.flag(argv, "--content")), CARD)
        self.assertEqual(self.flag(argv, "--idempotency-key"), safe_idempotency_key("unit-verified-result-k-card"))

    def test_a_card_feishu_refuses_outright_goes_as_markdown(self):
        refused = subprocess.CompletedProcess([], 1, "", '{"ok":false,"error":{"type":"api","code":230099,'
                                                        '"message":"Failed to create card content"}}')
        sent, calls = self.send(refused)
        self.assertTrue(sent)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.flag(calls[1], "--markdown"), "#### 先确认数据集\n正文")
        self.assertEqual(self.flag(calls[1], "--idempotency-key"), "unit-verified-result-k")
        # Once Markdown was tried for the text, its retries go straight there (here its receipt answers, as
        # that send landed): never the card again.
        sent, calls = self.send(subprocess.CompletedProcess([], 0, '{"data":{"message_id":"om_card"}}', ""))
        self.assertTrue(sent)
        self.assertEqual(calls, [])

    def test_a_card_that_may_have_landed_is_never_followed_by_markdown(self):
        sent, calls = self.send(subprocess.CompletedProcess([], 1, "", '{"ok":false,"error":{"type":"network"}}'))
        self.assertFalse(sent)
        self.assertTrue(calls)
        self.assertTrue(all("--msg-type" in argv for argv in calls))
        self.assertEqual(last_failure().category, "network")

    def test_an_answer_markdown_was_tried_for_before_cards_stays_markdown(self):
        # A release before answer cards sent this text as Markdown, and that send may have landed.
        receipt = transport._receipt_path("unit-verified-result-k")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("{}")
        sent, calls = self.send(subprocess.CompletedProcess([], 0, '{"data":{"message_id":"om_card"}}', ""))
        self.assertTrue(sent)
        self.assertEqual([("--markdown" in argv) for argv in calls], [True])

    def test_plain_text_format_sends_no_card(self):
        sent, calls = self.send(subprocess.CompletedProcess([], 0, "{}", ""), message_format="text")
        self.assertTrue(sent)
        self.assertEqual([("--text" in argv) for argv in calls], [True])


if __name__ == "__main__":
    unittest.main()
