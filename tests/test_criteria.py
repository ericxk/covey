"""A topic's completion criteria (core/criteria.py, #22): what topic_start and topic_send keep in its topic.json, what
each of its turns is told, what its checkpoint marks and the ones not met on its answer card and relay; a topic
without criteria is as before."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_butler_topics import ButlerTopicsCase

from covey.app import answer_cards
from covey.app.runs import Runs
from covey.cards.answer_card import CRITERIA_ID, tail
from covey.cards.relay_card import DONE, relay_card
from covey.core import criteria
from covey.core.state import write_json
from covey.core.topics import load_topic

ITEMS = ["全量测试通过", "合进 main 并推送", "在手机上点开看过"]


def topic_dir(state, topic, **data):
    path = Path(state) / "topics" / topic / "topic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"topic_id": topic, **data})
    return path


class ParseTest(unittest.TestCase):
    def test_a_calls_criteria_are_at_most_four_short_lines(self):
        self.assertEqual(criteria.clean(["1. 全量测试通过", "- 合进 main", "3 个文件都改好", "", "全量测试通过",
                                         "x" * 200, "五", "六"]),
                         ["全量测试通过", "合进 main", "3 个文件都改好", "x" * 79 + "…"])
        self.assertEqual(criteria.clean("全量测试通过\n2、合进 main\n"), ["全量测试通过", "合进 main"])
        self.assertEqual(criteria.clean([]), [])  # Given empty: none (topic_send takes them away).
        self.assertIsNone(criteria.clean(None))
        self.assertIsNone(criteria.clean({"a": 1}))

    def test_each_one_not_marked_met_is_left(self):
        cp = {"criteria": [{"item": "全量测试通过", "state": "满足", "evidence": "pytest 14 passed"},
                           {"item": "合进 main 并推送", "state": "没满足"}]}
        self.assertEqual(criteria.unmet(ITEMS, cp), [("没满足", "合进 main 并推送"), ("没查", "在手机上点开看过")])
        # Words the model changed: in order when there are as many marks as items.
        moved = {"criteria": [{"item": "测试", "state": "满足"}, {"item": "合并", "state": "满足"},
                              {"item": "手机", "state": "未知"}]}
        self.assertEqual(criteria.unmet(ITEMS, moved), [("没查", "在手机上点开看过")])
        self.assertEqual(criteria.unmet(ITEMS, {}), [("没查", item) for item in ITEMS])
        self.assertEqual(criteria.unmet(ITEMS, {"criteria": [{"item": item, "state": "满足"} for item in ITEMS]}), [])
        self.assertEqual(criteria.marks({"criteria": "不是列表"}), [])

    def test_the_turns_note(self):
        note = criteria.note(ITEMS[:2])
        self.assertIn("1. 全量测试通过\n2. 合进 main 并推送", note)
        self.assertIn('"state":"满足|没满足|没查"', note)
        self.assertEqual(criteria.note([]), "")


class TurnTest(unittest.TestCase):
    def test_only_a_topic_with_criteria_is_told_them(self):
        with tempfile.TemporaryDirectory() as directory:
            topic_dir(directory, "t1", criteria=ITEMS[:2])
            topic_dir(directory, "t2")
            ctx = SimpleNamespace(state_dir=Path(directory), topic_id="t1")
            note = Runs._criteria_note(ctx, SimpleNamespace(topic_id="t1"))
            self.assertEqual(note, "\n\n" + criteria.note(ITEMS[:2]))
            self.assertEqual(Runs._criteria_note(ctx, SimpleNamespace(topic_id="t2")), "")
            self.assertEqual(Runs._criteria_note(ctx, SimpleNamespace(topic_id="gone")), "")


class CardTest(unittest.TestCase):
    inputs = ("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", "脱敏关", "repo")

    def test_the_answer_card_shows_those_not_met_between_the_change_and_the_box(self):
        with tempfile.TemporaryDirectory() as directory:
            topic_dir(directory, "t1", criteria=ITEMS)
            point = Path(directory) / "cp.json"
            point.write_text(json.dumps({"criteria": [{"item": ITEMS[0], "state": "满足"},
                                                      {"item": ITEMS[1], "state": "没满足"}]}, ensure_ascii=False))
            job = SimpleNamespace(topic_id="t1", checkpoint_path=str(point))
            notes = answer_cards.notes(Path(directory), job)
            shown = tail(None, self.inputs, ("用时 1分",), change={"commit": "951327a"}, notes=notes)
            self.assertEqual([element.get("element_id") for element in shown], ["answer_change", CRITERIA_ID, "answer_facts"])
            row = shown[1]["columns"]
            self.assertEqual(row[0]["elements"][0]["content"], "<font color='grey'>完成标准</font>")
            self.assertEqual(row[1]["elements"][0]["content"].split("\n"), [
                "<font color='orange'>没满足</font><font color='grey'>：</font>合进 main 并推送",
                "<font color='grey'>没查</font><font color='grey'>：</font>在手机上点开看过"])
            relayed = relay_card("T1", DONE, text="好了", inputs=self.inputs, notes=notes)[0]
            self.assertIn(CRITERIA_ID, json.dumps(relayed))
            # Every one met, or a topic without criteria: nothing, the card as before.
            point.write_text(json.dumps({"criteria": [{"item": item, "state": "满足"} for item in ITEMS]}))
            self.assertEqual(answer_cards.notes(Path(directory), job), [])
            topic_dir(directory, "t2")
            self.assertEqual(answer_cards.notes(Path(directory), SimpleNamespace(topic_id="t2", checkpoint_path=str(point))), [])
            self.assertEqual(tail(None, self.inputs, ("用时 1分",), notes=[]), tail(None, self.inputs, ("用时 1分",)))


class ButlerToolTest(ButlerTopicsCase):
    def test_topic_start_keeps_them_and_topic_send_changes_them(self):
        answer = self.start("c1", criteria=["1. 全量测试通过", "合进 main 并推送"])
        self.assertTrue(answer["ok"], answer)
        topic = answer["data"]["topic_id"]
        self.assertEqual(load_topic(self.ctx.state_dir, topic)["criteria"], ["全量测试通过", "合进 main 并推送"])
        self.assertNotIn("criteria", load_topic(self.ctx.state_dir, self.start("c2")["data"]["topic_id"]))
        label = answer["data"]["label"]
        sent = self.call("topic_send", "c3", topic=label, text="标准改一下", criteria=["部署到第 464 代"])
        self.assertEqual(sent["data"]["criteria"], ["部署到第 464 代"])
        self.assertEqual(load_topic(self.ctx.state_dir, topic)["criteria"], ["部署到第 464 代"])
        self.call("topic_send", "c4", topic=label, text="不用标准了", criteria=[])
        self.assertNotIn("criteria", load_topic(self.ctx.state_dir, topic))
        self.call("topic_send", "c5", topic=label, text="接着做")  # None given: left as it is.
        self.assertNotIn("criteria", load_topic(self.ctx.state_dir, topic))


if __name__ == "__main__":
    unittest.main()
