"""A topic's title has one source (C9/V13, 2026-11-08): core/topics.py topic_title. T442 read "验收:睡 70 秒…" on its
Pin and status card and the owner's own words in 手上的事 and the butler's tools."""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path

from covey.app import butler_reply, butler_tools, status_cards
from covey.app.overview import Overview
from covey.core.topics import shown_title, topic_title
from support.records import write_topic


class TopicTitleTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="topic-title-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))

    def history(self, topic_id, *rows):
        path = self.state / "topics" / topic_id / "history.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))

    def every_title(self, topic_id):
        env = types.SimpleNamespace(state_dir=self.state)
        overview = types.SimpleNamespace(topics={})
        return {"status card": status_cards.title_of(self.state, topic_id).split(" · ", 1)[-1],
                "main card": butler_reply._topic_title(self.state, topic_id),
                "main tools": butler_tools.title(env, topic_id),
                "手上的事": Overview.topic(overview, self.state, topic_id)[1]}

    def test_the_butler_s_title_first_line_with_chinese_punctuation(self):
        write_topic(self.state, {"topic_number": 442, "chat_id": "oc_dm",
                                 "origin": {"title": "验收:睡 70 秒并写 REPORT-2.md,登记产物\n第二行"}}, topic_id="t442")
        self.history("t442", {"role": "user", "content": "【验收 T419 答案卡第二次】另开一个话题区：先运行…"})
        title = "验收：睡 70 秒并写 REPORT-2.md，登记产物"
        self.assertEqual(topic_title(self.state, "t442"), title)
        self.assertEqual(set(self.every_title("t442").values()), {title})

    def test_without_one_the_first_user_line_that_says_something(self):
        write_topic(self.state, {"topic_number": 7, "chat_id": "oc_dm"}, topic_id="t7")
        self.history("t7", {"role": "assistant", "content": "不是这条"},
                     {"role": "user", "content": "用户补充回答：\n  看一下   日志:为什么\n第二行"})
        self.assertEqual(set(self.every_title("t7").values()), {"看一下 日志：为什么"})

    def test_kept_until_topic_json_changes(self):
        path = write_topic(self.state, {"topic_number": 8, "chat_id": "oc_dm", "origin": {"title": "旧标题"}},
                           topic_id="t8")
        self.assertEqual(topic_title(self.state, "t8"), "旧标题")
        write_topic(self.state, {"topic_number": 8, "chat_id": "oc_dm", "origin": {"title": "改过的新标题"}},
                    topic_id="t8")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
        self.assertEqual(topic_title(self.state, "t8"), "改过的新标题")
        self.assertEqual(topic_title(self.state, "missing"), "")

    def test_the_task_cards_title_bar_and_previews_write_it_the_same(self):
        # ACCEPT-322 problem 2: "T446 · #3310 · 验收T444:睡75秒报时间" on the task card and its Pin preview, the
        # full-width colon on the answer card and in the hand-off.
        from covey.cards.streaming import build_stream_card, card_title_for_question
        from support.cards import sample_job, stream_runtime
        write_topic(self.state, {"topic_number": 446, "chat_id": "oc_dm", "origin": {"title": "验收T444:睡75秒报时间"}},
                    topic_id="t446")
        title = topic_title(self.state, "t446")
        self.assertEqual(title, "验收T444：睡75秒报时间")
        runtime = stream_runtime()
        job = sample_job(job_id=3310, topic_label="T446", title="验收T444:睡75秒报时间", execution_started=True)
        card = build_stream_card("执行中", job.title, runtime, job=job)
        self.assertEqual(card["header"]["title"]["content"], f"T446 · #3310 · {title}")
        self.assertIn(title, card["config"]["summary"]["content"])
        self.assertEqual(card_title_for_question(job.title, runtime), title)  # The running preview's.
        self.assertEqual(card_title_for_question("13:15 a,b", runtime), "13:15 a,b")

    def test_punctuation_only_beside_chinese(self):
        self.assertEqual([shown_title(text) for text in ("13:15 a,b", "https://x.y/z", "好, 那就", "T419: 修正")],
                         ["13:15 a,b", "https://x.y/z", "好，那就", "T419：修正"])


if __name__ == "__main__":
    unittest.main()
