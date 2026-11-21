"""A very long title never takes the status word's place (T604 #51, docs/ui.md 8): the task card's status line starts with
its state's word and holds no title (the title bar has it, cut to 24 characters), and every preview a topic's latest
message gives the topic list says the T number and the status word before any title. How a phone draws them is a real
client's check, not done here."""
from __future__ import annotations

import re
import unittest

from support.cards import sample_job, stream_runtime
from covey.cards import ui
from covey.cards.question_cards import question_card
from covey.cards.review import (answer_card, answer_lead, answer_summary, outcome_line, queued_summary, render_stream_status,
                                running_summary, task_card_summary)
from covey.cards.streaming import build_stream_card, card_title_for_question
from covey.cards.task_status import status_elements
from covey.core import look

LONG = ("把订单库从旧集群迁到新集群，核对每一张表的行数和校验和，然后切换读写流量，最后清理旧集群上所有的临时表和备份"
        * 4)
WORDS = {look.STATES[key].word for key in look.STATES}


def first_word(markdown):
    """The first word a status line's Markdown shows, its tags and marks taken off."""
    text = re.sub(r"<[^>]+>|\*\*|#+ ", "", markdown).replace("●", "").strip()
    return re.split(r"[\s·]", text, maxsplit=1)[0]


class StatusLineTest(unittest.TestCase):
    def test_the_status_line_starts_with_its_word_and_the_title_is_in_the_title_bar_cut(self):
        job = sample_job(title=LONG, user_text=LONG)
        card = build_stream_card(render_stream_status(job, "已入队"), job.title, stream_runtime(), job=job,
                                 include_stop_action=True)
        heading = card["header"]["title"]["content"]
        self.assertTrue(heading.startswith("T70 · #431 · "))
        self.assertLessEqual(len(heading), len("T70 · #431 · ") + 24)
        for line, word in (("在做 · 已运行 12 秒\n现在：核对行数", "在做"), (outcome_line("done") + " · 用时 3分39秒", "有结果"),
                           (outcome_line("failed") + " · 用时 5秒", "停住了"), ("可能卡住 · 已运行 40 分钟", "可能卡住"),
                           (outcome_line("waiting_user"), "等你")):
            for rows, slim, io in ((True, False, True), (False, False, False), (False, True, False)):
                elements = status_elements(line, rows, slim, io)
                self.assertEqual(first_word(elements[0]["content"]), word, (line, rows, slim))
                self.assertNotIn(LONG[:12], str(elements))

    def test_a_topic_row_says_its_state_before_its_title(self):
        row = ui.topic_row("stopped", "T70 " + LONG, "测试没过", url="https://example.invalid/t70")
        line = row["elements"][0]["columns"][0]["elements"][0]["content"]
        self.assertEqual(first_word(line), "停住了")
        self.assertLess(line.index("停住了"), line.index("T70"))


class PreviewTest(unittest.TestCase):
    """What the topic list shows of the topic's latest message: the task card's, the answer card's, the question card's."""

    def title(self):
        return card_title_for_question(LONG, stream_runtime())

    def assertLeads(self, preview, word):
        """The T number (and the task's), then the word, before any of the title: the list cuts the end alone."""
        self.assertTrue(preview.startswith("T70"), preview)
        self.assertIn(word, preview[:16], preview)
        if LONG[:6] in preview:
            self.assertLess(preview.index(word), preview.index(LONG[:6]), preview)

    def test_the_task_cards_previews(self):
        self.assertLessEqual(len(self.title()), 24)
        self.assertLeads(queued_summary("T70", self.title()), "排队")
        self.assertLeads(running_summary("T70", self.title()), "在做")
        self.assertLeads(running_summary("T70", self.title(), phase=LONG), "在做")
        self.assertLeads(running_summary("T70", self.title(), asking=True), "等你")
        self.assertLeads(task_card_summary("T70", "done", outcome_line("done") + " · 用时 3分39秒"), "有结果")
        self.assertLeads(task_card_summary("T70", "failed"), "停住了")
        job = sample_job(title=LONG, user_text=LONG)
        card = build_stream_card(render_stream_status(job, "已入队"), job.title, stream_runtime(), job=job)
        self.assertLeads(card["config"]["summary"]["content"], "排队")

    def test_the_answer_cards_and_the_question_cards(self):
        built = answer_card(LONG, answer_summary("T70", "done", answer_lead(LONG)))
        self.assertLeads(built["config"]["summary"]["content"], "有结果")
        record = {"token": "0123456789abcdef", "status": "pending", "topic_id": "t" * 32, "chat_id": "oc_x", "job_id": 431,
                  "blocking": True, "harness_id": "codex", "binding_revision": 2, "created_at": 1.0,
                  "questions": [{"id": "q", "header": "范围", "question": LONG, "options": []}]}
        summary = question_card(record, topic="T70")["config"]["summary"]["content"]
        self.assertLeads(summary, "等你")


if __name__ == "__main__":
    unittest.main()
