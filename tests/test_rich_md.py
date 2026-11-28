"""富文本块 (T943, docs/ui.md 富文本块): one reply's Markdown, split into blocks the same way here (core/rich_md.py) and
on the web page (web/client/src/cv/richBlocks.ts, web/scripts/rich-blocks.test.mjs read the same
tests/fixtures/rich/cases.json), drawn as card elements (cards/rich_body.py), as one card Markdown text where a card
holds one (the task card, pages) and as words without the marks (titles, previews, drafts)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from covey.app import prompts
from covey.cards import rich_body
from covey.cards.elements import element_count
from covey.cards.review import answer_card, answer_lead, inline_one_line_code, plain_markdown, render_stream_final, \
    reply_shown
from covey.core import owner_send, rich_md
from covey.core.card_limits import TARGET_NODES

CASES = Path(__file__).resolve().parent / "fixtures" / "rich" / "cases.json"
FENCE = "```"
SAMPLE = ("先说结论：可以上线。\n\n> [!WARNING] 迁移风险\n> 旧卡片不会重画。\n\n<details>\n<summary>完整日志</summary>\n\n"
          + FENCE + "\nline 1\nline 2\n" + FENCE + "\n\n</details>\n\n" + FENCE + "metrics\n通过 | 120/120 | +3\n用时: 4 分\n"
          + FENCE + "\n\n- [ ] 部署\n- [x] 测试")


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


class SharedCasesTest(unittest.TestCase):
    """The cases both sides split: what they found, kind by kind, is the file's (rebuild it only on purpose)."""

    def test_each_case_splits_as_recorded(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            with self.subTest(case["name"]):
                self.assertEqual(case["blocks"], [block.shape() for block in rich_md.blocks(case["text"])])

    def test_a_text_without_blocks_is_one_markdown_block_or_none(self):
        self.assertEqual([], rich_md.blocks(""))
        self.assertEqual(["md"], [block.kind for block in rich_md.blocks("一段\n\n二段")])
        self.assertFalse(rich_md.rich("普通回答，`code` 和 [链接](https://x)"))


class CardMarkdownTest(unittest.TestCase):
    """One card Markdown text: each block in words a card's Markdown shows."""

    def test_blocks_become_words(self):
        shown = rich_md.card_markdown(SAMPLE)
        self.assertIn("> **⚠️ 注意**  迁移风险\n> 旧卡片不会重画。", shown)
        self.assertIn("**完整日志**\n" + FENCE, shown)
        self.assertIn("通过：**120/120**  <font color='grey'>+3</font>", shown)
        self.assertIn("\n<font color='green'>✓</font> 测试", shown)  # No bullet before it: a card drew "• ✓".
        self.assertIn("\n<font color='grey'>○</font> 部署", shown)
        for mark in ("[!WARNING]", "<details>", "<summary>", "</details>", FENCE + "metrics", "[ ]", "[x]"):
            self.assertNotIn(mark, shown)

    def test_no_orange_or_red_words(self):
        # docs/ui.md 2.2: orange says 等你 and 可能卡住 alone, red 停住了 alone.
        for tone in rich_md.TONES:
            shown = rich_md.card_markdown(f"> [!{tone.upper()}] 标题\n> 正文")
            self.assertNotIn("color='orange'", shown)
            self.assertNotIn("color='red'", shown)

    def test_a_text_without_blocks_is_unchanged(self):
        text = "## 结论\n\n普通回答。\n\n" + FENCE + "\n- [ ] 代码里的不动\n" + FENCE
        self.assertEqual(text, rich_md.card_markdown(text))

    def test_callouts_in_a_row_stay_apart_and_nested_tasks_keep_their_depth(self):
        shown = rich_md.card_markdown("> [!NOTE]\n> 一\n> [!TIP]\n> 二\n\n- [x] 父\n  - [ ] 子")
        self.assertIn("> 一\n\n　\n\n> **💡 提示**", shown)
        self.assertIn("\n　<font color='grey'>○</font> 子", shown)

    def test_a_details_left_open_shows_no_tags(self):
        shown = rich_md.card_markdown("<details>\n<summary>写到一半</summary>\n\n还没闭合")
        self.assertNotIn("<details>", shown)
        self.assertIn("**写到一半**", shown)

    def test_the_task_card_body_is_one_text_in_words(self):
        class Job:
            job_id = 7
        shown = render_stream_final(Job(), SAMPLE, bare=True)
        self.assertNotIn("[!WARNING]", shown)
        self.assertIn("**⚠️ 注意**", shown)


class PlainTest(unittest.TestCase):
    """Words without the marks: a title, the chat list's preview, a draft for a person."""

    def test_plain_words(self):
        words = rich_md.plain(SAMPLE)
        self.assertIn("注意：迁移风险\n旧卡片不会重画。", words)
        self.assertIn("通过：120/120（+3）", words)
        self.assertIn("☐ 部署", words)
        for mark in ("[!WARNING]", "<details>", "<summary>", FENCE + "metrics"):
            self.assertNotIn(mark, words)

    def test_lead_and_preview_never_show_marks(self):
        self.assertEqual("注意：先停机", answer_lead("> [!WARNING]\n> 先停机。\n\n后面。"))
        self.assertEqual("先折起来", answer_lead("<details><summary>先折起来</summary>\n明细\n</details>"))
        self.assertNotIn("[!", plain_markdown("> [!NOTE] 标题\n> 正文"))

    def test_a_details_left_open_is_no_lead(self):
        self.assertEqual("写到一半", answer_lead("<details>\n<summary>写到一半</summary>\n\n还没闭合"))

    def test_a_proxy_reply_has_no_marks(self):
        from covey.app.proxy import words_only

        self.assertEqual("提示：明天见", words_only("> [!TIP]\n> 明天见"))
        self.assertEqual("普通的话 - [ ] 不动", words_only("普通的话 - [ ] 不动"))

    def test_a_draft_for_a_person_has_no_marks(self):
        found = owner_send.drafts({owner_send.FIELD: [{"to": "王五", "text": "> [!NOTE] 周五上线\n> 记得看一下",
                                                         "why": "通知"}]})
        self.assertEqual("说明：周五上线\n记得看一下", found[0]["text"])


class CardElementsTest(unittest.TestCase):
    """A reply's blocks as card elements on the cards that hold elements (the answer card, the relay)."""

    def test_each_block_its_element(self):
        drawn = rich_body.reply_elements(SAMPLE, "answer_reply")
        self.assertEqual("answer_reply", drawn[0]["element_id"])
        tags = [element["tag"] for element in drawn]
        self.assertEqual(["markdown", "column_set", "collapsible_panel", "column_set", "markdown"], tags)
        callout, fold, tiles = drawn[1], drawn[2], drawn[3]
        self.assertEqual("grey-50", callout["columns"][0]["background_style"])
        self.assertEqual("**⚠️ 注意**  迁移风险", callout["columns"][0]["elements"][0]["content"])
        self.assertEqual("answer_reply_d0", fold["element_id"])
        self.assertFalse(fold["expanded"])
        self.assertEqual("stretch", tiles["flex_mode"])  # Not bisect: it broke words in a side panel.
        self.assertEqual(2, len(tiles["columns"]))
        for node in walk(drawn):
            self.assertFalse(str(node.get("background_style") or "").startswith(("red", "orange", "carmine")))
            if node.get("element_id"):
                self.assertLessEqual(len(node["element_id"]), 20)

    def test_metrics_go_three_or_four_a_row(self):
        five = rich_body.metrics(rich_md.Block("metrics", "", items=tuple((f"k{n}", str(n), "") for n in range(5))))
        self.assertEqual([3, 2], [len(row["columns"]) for row in five])
        eight = rich_body.metrics(rich_md.Block("metrics", "", items=tuple((f"k{n}", str(n), "") for n in range(8))))
        self.assertEqual([4, 4], [len(row["columns"]) for row in eight])

    def test_a_reply_without_blocks_is_one_markdown_element_as_before(self):
        self.assertEqual([{"tag": "markdown", "element_id": "relay_reply", "content": "普通回答。"}],
                         reply_shown("普通回答。"))

    def test_too_many_blocks_are_one_text(self):
        many = "\n\n".join(f"> [!NOTE] 第 {n} 条\n> 正文" for n in range(60))
        drawn = rich_body.reply_elements(many, "answer_reply")
        self.assertEqual(1, len(drawn))
        self.assertNotIn("[!NOTE]", drawn[0]["content"])
        built = answer_card(many, "T1 有结果")
        self.assertLessEqual(element_count(built), TARGET_NODES)

    def test_a_card_too_large_with_its_blocks_tries_one_text_first(self):
        from covey.cards.pages import fits

        long = "\n\n".join(f"> [!NOTE] 第 {n} 条\n> " + "很长的一句话。" * 30 for n in range(28))
        built = answer_card(long, "T1 有结果")
        self.assertIsNotNone(built)
        self.assertTrue(fits(built))
        self.assertNotIn("[!NOTE]", json.dumps(built, ensure_ascii=False))

    def test_a_details_holds_the_blocks_in_it(self):
        drawn = rich_body.reply_elements("<details>\n<summary>细节</summary>\n\n> [!WARNING] 里面\n> 正文\n\n</details>",
                                         "answer_reply")
        fold = drawn[0]
        self.assertEqual("collapsible_panel", fold["tag"])
        self.assertEqual("column_set", fold["elements"][0]["tag"])

    def test_the_butler_answer_fold_is_words(self):
        from covey.app.butler_reply import reply_fold

        panel = reply_fold("> [!NOTE] 记一下\n> 正文")
        self.assertNotIn("[!NOTE]", json.dumps(panel, ensure_ascii=False))

    def test_label_goes_first(self):
        drawn = rich_body.reply_elements("> [!TIP] 小提示", "mirror_output", label="这一轮输出")
        self.assertEqual("mirror_output", drawn[0]["element_id"])
        self.assertIn("这一轮输出", drawn[0]["content"])


class PipelineTest(unittest.TestCase):
    def test_one_line_metrics_and_diff_keep_their_fence(self):
        self.assertEqual(FENCE + "metrics\n通过 | 1\n" + FENCE, inline_one_line_code(FENCE + "metrics\n通过 | 1\n" + FENCE))
        self.assertEqual(FENCE + "diff\n-a\n" + FENCE, inline_one_line_code(FENCE + "diff\n-a\n" + FENCE))
        self.assertEqual("`ls`", inline_one_line_code(FENCE + "bash\nls\n" + FENCE))

    def test_the_rule_is_in_the_first_message_not_the_reminder(self):
        self.assertIn("[!NOTE]", prompts.RICH_RULE)
        self.assertIn(prompts.RICH_RULE, prompts.constraints("Claude Code"))
        self.assertNotIn("[!NOTE]", prompts.RESUME_REMINDERS)
        self.assertNotIn("[!NOTE]", prompts.proxy_constraints("Claude Code"))


if __name__ == "__main__":
    unittest.main()
