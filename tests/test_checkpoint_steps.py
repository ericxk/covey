"""The checkpoint's optional ``steps`` and ``delivery`` (core/checkpoint.py CHECKPOINT_RULES): how the Bridge reads
them, times each step, and says where a turn is on the task card's status line, the answer card, the relay and in
topic_read; without them every one of those is as before."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import make_job

from covey.app import answer_cards, butler_tools
from covey.cards.answer_card import CHANGE_ID, tail
from covey.cards.relay_card import DONE, relay_card
from covey.core.checkpoint import (CHECKPOINT_RULES, MAX_STEPS, checkpoint_delivery, checkpoint_steps,
                                            change_summary, clock_steps, delivery_commits, shows_change,
                                            step_words)
from covey.core.longtask import TaskMonitor
from covey.core.status_line import Facts, status_summary, step_text
from covey.core.trace import ToolTrace


def steps(*states):
    return [{"title": f"第{index}项", "state": state} for index, state in enumerate(states, 1)]


class RulesTest(unittest.TestCase):
    def test_the_rules_name_both_fields_briefly(self):
        added = CHECKPOINT_RULES[CHECKPOINT_RULES.index("长任务另写 steps"):]
        for words in ('"state":"done|doing|todo"', "同时只有一个 doing", "delivery", '"commit"', '"deployed"',
                      '"verified"'):
            self.assertIn(words, added)
        # Only a turn that changed code writes one, its commits in full (the owner, 2026-11-09).
        for words in ("改了代码、有提交时才写 delivery", "完整提交号", "调研、报告、问答的轮次不写 delivery"):
            self.assertIn(words, added)
        self.assertLessEqual(sum(1 for char in added if "一" <= char <= "鿿"), 150)


class ParseTest(unittest.TestCase):
    def test_steps_are_cut_to_what_can_be_shown(self):
        for raw in (None, "a", 3, {"title": "x"}, [1, "x", None, {"state": "doing"}, {"title": ""}, {"title": True}]):
            self.assertEqual(checkpoint_steps({"steps": raw}), [], raw)
        self.assertEqual(checkpoint_steps("not a dict"), [])
        found = checkpoint_steps({"steps": [{"title": "  读\n代码 ", "state": "DONE"}, {"title": "改", "state": "doing"},
                                            {"title": "测", "state": "doing"}, {"title": 7, "state": "later"}]})
        self.assertEqual(found, [{"title": "读 代码", "state": "done"}, {"title": "改", "state": "doing"},
                                 {"title": "测", "state": "todo"}, {"title": "7", "state": "todo"}])
        long = checkpoint_steps({"steps": [{"title": "长" * 40, "state": "todo"}] * 30})
        self.assertEqual(len(long), MAX_STEPS)
        self.assertEqual(long[0]["title"], "长" * 29 + "…")

    def test_delivery_is_three_strings_or_nothing(self):
        for raw in (None, "a6bc148", [], {}, {"commit": "", "deployed": " ", "verified": None}, {"other": "x"}):
            self.assertEqual(checkpoint_delivery({"delivery": raw}), {}, raw)
        self.assertEqual(checkpoint_delivery({"delivery": {"commit": "a6bc148", "deployed": 322, "verified": False}}),
                         {"commit": "a6bc148", "deployed": "322", "verified": ""})
        # Read as written (the 改动 fold shows it all), up to DELIVERY_CHARS.
        self.assertEqual(checkpoint_delivery({"delivery": {"verified": "x" * 300}})["verified"], "x" * 300)
        self.assertEqual(len(checkpoint_delivery({"delivery": {"verified": "x" * 5000}})["verified"]), 2000)
        self.assertEqual(checkpoint_delivery({"delivery": {"commit": ["951327a0c1", 7, None, ""]}})["commit"],
                         "951327a0c1 7")

    def test_commits(self):
        self.assertEqual(delivery_commits({}), [])
        self.assertEqual(delivery_commits({"commit": "951327a0c1d2e3f4、a6bc148, 951327a0c1d2e3f4"}),
                         ["951327a0c1d2e3f4", "a6bc148"])
        self.assertEqual(delivery_commits({"commit": "bridge/20261108-steps-e4486f73 分支"}),
                         ["bridge/20261108-steps-e4486f73 分支"])

    def test_only_a_turn_that_changed_code_shows_its_change(self):
        full = {"commit": "951327a0c1d2", "deployed": "第 331 代", "verified": "全量测试"}
        self.assertTrue(shows_change(full))
        self.assertTrue(shows_change({"commit": "951327a", "deployed": "", "verified": ""}))
        # No commit: only when the Bridge saw the turn change files in its workspace, and the delivery says something.
        report = {"commit": "", "deployed": "", "verified": "报告已发群"}
        self.assertFalse(shows_change(report))
        self.assertTrue(shows_change(report, files_changed=True))
        self.assertFalse(shows_change({}, files_changed=True))
        self.assertFalse(shows_change({"commit": "", "deployed": "", "verified": ""}, files_changed=True))

    def test_words(self):
        self.assertEqual(step_words([]), "")
        self.assertEqual(step_words(steps("done", "done", "doing", "todo", "todo")), "第 3/5 步：第3项")
        self.assertEqual(step_words(steps("done", "todo")), "已完成 1/2 步")
        self.assertEqual(step_words(steps("done", "done")), "2/2 步已完成")
        self.assertEqual(change_summary({}), "")
        self.assertEqual(change_summary({"commit": "951327a0c1d2e3f4", "deployed": "第 331 代（10:11 切换）",
                                         "verified": "群里点开看过"}), "951327a · 第 331 代 · 已验收")
        self.assertEqual(change_summary({"commit": "951327a0c1 a6bc148d38", "deployed": "", "verified": ""}),
                         "951327a +1 · 未部署 · 未验收")
        # No room for "+1" in a topic's side panel: the first commit alone, the rest when opened.
        self.assertEqual(change_summary({"commit": "951327a0c1 a6bc148d38", "deployed": "第 331 代", "verified": "x"}),
                         "951327a · 第 331 代 · 已验收")
        self.assertEqual(change_summary({"commit": "", "deployed": "只合进 master，没部署", "verified": "全量测试"}),
                         "未提交 · 只合进 mast… · 已验收")
        long = change_summary({"commit": "bridge/20261108-steps-e4486f73 分支", "deployed": "", "verified": ""})
        self.assertEqual(long, "bridge/2026… · 未部署 · 未验收")
        for line in (long, change_summary({"commit": "a" * 7, "deployed": "部" * 50, "verified": "x"})):
            self.assertLessEqual(sum(1 if ord(char) < 0x2E80 else 2 for char in line), 30)


class ClockTest(unittest.TestCase):
    def test_doing_then_done(self):
        times = clock_steps(steps("doing", "todo"), {}, 100)
        self.assertEqual(times, {"第1项": {"started": 100}})
        times = clock_steps(steps("done", "doing"), times, 400)
        self.assertEqual(times, {"第1项": {"started": 100, "ended": 400}, "第2项": {"started": 400}})

    def test_the_same_state_again_changes_nothing(self):
        times = clock_steps(steps("done", "doing"), {"第1项": {"started": 100, "ended": 400}, "第2项": {"started": 400}}, 900)
        self.assertEqual(times, {"第1项": {"started": 100, "ended": 400}, "第2项": {"started": 400}})

    def test_a_skipped_step_has_its_end_alone(self):
        times = clock_steps(steps("done", "done", "doing"), {"第1项": {"started": 100}}, 500)
        self.assertEqual(times, {"第1项": {"started": 100, "ended": 500}, "第2项": {"ended": 500}, "第3项": {"started": 500}})

    def test_a_step_taken_back(self):
        before = {"第1项": {"started": 100, "ended": 400}, "第2项": {"started": 400}}
        self.assertEqual(clock_steps(steps("doing", "todo"), before, 600), {"第1项": {"started": 100}})
        # Steps no longer listed and stamps of the wrong type are dropped.
        self.assertEqual(clock_steps([{"title": "新", "state": "doing"}],
                                     {"第1项": {"started": 1}, "新": {"started": "x", "ended": True}}, 700),
                         {"新": {"started": 700}})
        self.assertEqual(clock_steps(steps("doing"), "not a dict", 5), {"第1项": {"started": 5}})


class StatusLineTest(unittest.TestCase):
    T0 = 1_800_000_000

    def monitor(self, point, wall):
        records = []
        job = make_job(tool_trace=ToolTrace(), backend_process=SimpleNamespace(poll=lambda: None))
        updates = []
        monitor = TaskMonitor(job, point, updates.append, lambda **r: records.append(r), lambda *a: True,
                              lambda _: None, lambda: None, clock=lambda: wall[0] - self.T0, wall=lambda: wall[0])
        return monitor, updates, records

    def test_the_step_under_way_and_its_time_stand_for_the_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            point, wall = Path(directory) / "cp.json", [self.T0]
            point.write_text(json.dumps({"phase": "改卡片中", "steps": [
                {"title": "读代码", "state": "done"}, {"title": "改卡片", "state": "doing"}, {"title": "写测试"}],
                "delivery": {"commit": "a6bc148"}}, ensure_ascii=False))
            monitor, updates, records = self.monitor(point, wall)
            monitor.tick()
            wall[0] += 2400
            monitor.tick()
            first = updates[-1].split("\n")[0]
            self.assertTrue(first.endswith(" · 第 2/3 步：改卡片 · 本步 40 分钟"), first)
            self.assertNotIn("阶段", first)
            self.assertEqual(records[-1]["steps"][1], {"title": "改卡片", "state": "doing"})
            self.assertEqual(records[-1]["step_times"], {"读代码": {"ended": self.T0}, "改卡片": {"started": self.T0}})
            self.assertEqual(records[-1]["delivery"], {"commit": "a6bc148", "deployed": "", "verified": ""})
            point.write_text(json.dumps({"phase": "收尾", "steps": [
                {"title": "读代码", "state": "done"}, {"title": "改卡片", "state": "done"}, {"title": "写测试"}]},
                ensure_ascii=False))
            wall[0] += 60
            monitor.tick()
            first = updates[-1].split("\n")[0]
            # 读代码 was done when the plan was first seen (an earlier turn's): one step of this turn's own and no history
            # of the topic, so no estimate (core/progress.py pace).
            self.assertTrue(first.endswith(" · 阶段：收尾 · 已完成 2/3 步"), first)
            self.assertEqual(records[-1]["step_times"]["改卡片"], {"started": self.T0, "ended": self.T0 + 2460})
            self.assertEqual(records[-1]["delivery"]["commit"], "a6bc148")  # Kept once seen.
            point.write_text(json.dumps({"phase": "收尾"}, ensure_ascii=False))
            wall[0] += 60
            monitor.tick()
            self.assertEqual((records[-1]["steps"], records[-1]["step_times"]), ([], {}))  # Emptied, not left stale.

    def test_without_steps_the_line_and_the_record_are_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            point, wall = Path(directory) / "cp.json", [self.T0]
            point.write_text(json.dumps({"phase": "改卡片中", "steps": "三步", "delivery": "a6bc148"}, ensure_ascii=False))
            monitor, updates, records = self.monitor(point, wall)
            monitor.tick()
            self.assertTrue(updates[-1].split("\n")[0].endswith(" · 阶段：改卡片中"))
            self.assertNotIn("步：", updates[-1])
            self.assertFalse({"steps", "step_times", "delivery"} & set(records[-1]))

    def test_step_text_and_the_plain_summary(self):
        self.assertEqual(step_text([], {}, 0), ("", False))
        self.assertEqual(step_text(steps("doing"), {"第1项": {"started": 970}}, 1000), ("第 1/1 步：第1项 · 本步 不到 1 分钟", True))
        self.assertEqual(step_text(steps("doing"), {"第1项": {"started": "x"}}, 1000), ("第 1/1 步：第1项", True))
        self.assertEqual(step_text(steps("doing"), [], 1000), ("第 1/1 步：第1项", True))
        self.assertEqual(step_text(steps("done", "todo"), {}, 1000), ("已完成 1/2 步", False))
        facts = Facts(elapsed=65, quiet=3, alive=True, finished=False, phase="改", last_event="尚无阶段进展",
                      checked="01-02 03:04:05", question="", waiting=False, native_question_active=False,
                      activity=None, traced=False, wall=1000.0, stirred=False, retry={}, epoch=1000.0,
                      long_mode=True, frame=0)
        plain = status_summary(facts)
        self.assertNotIn("步骤", plain)
        self.assertIn("阶段：改\n步骤：第 1/1 步：第1项\n", status_summary(facts._replace(steps="第 1/1 步：第1项")))


class AnswerCardTest(unittest.TestCase):
    inputs = ("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", "脱敏关", "repo")
    outputs = ("用时 1分", "**69** token/s")

    change = {"commit": "951327a0c1d2e3f4a5b6 a6bc148d38ce8c230e96e", "deployed": "第 331 代，" + "部" * 300,
              "verified": "10:12 在群里点开看过，" + "验" * 300 + " *客户端*"}

    def fold(self, elements):
        return next(element for element in elements if element.get("element_id") == CHANGE_ID)

    def test_the_change_is_a_fold_only_with_one(self):
        plain = tail(None, self.inputs, self.outputs)
        self.assertEqual(tail(None, self.inputs, self.outputs, change={}), plain)
        self.assertEqual(tail(None, self.inputs, self.outputs, change=""), plain)
        self.assertEqual(relay_card("T1", DONE, text="好了", inputs=self.inputs, outputs=self.outputs),
                         relay_card("T1", DONE, text="好了", inputs=self.inputs, outputs=self.outputs, change={}))
        self.assertNotIn("改动", json.dumps(plain, ensure_ascii=False))
        self.assertNotIn("交付", json.dumps(plain, ensure_ascii=False))
        files = {"tag": "collapsible_panel", "element_id": "answer_artifacts"}
        shown = tail(files, self.inputs, self.outputs, change=self.change)
        # Under 产物, above the 输入/输出 box.
        self.assertEqual([element.get("element_id") for element in shown], ["answer_artifacts", CHANGE_ID, "answer_facts"])
        fold = shown[1]
        self.assertEqual(fold["tag"], "collapsible_panel")
        self.assertFalse(fold["expanded"])
        title = fold["header"]["title"]
        self.assertEqual((title["content"], title["text_size"], title["text_color"]),
                         ("改动 · 951327a · 第 331 代 · 已验收", "notation", "grey"))
        rows = [[column["elements"][0]["content"] for column in row["columns"]] for row in fold["elements"]]
        self.assertEqual([label for label, _ in rows], ["<font color='grey'>" + word + "</font>" for word in ("提交", "部署", "验收")])
        self.assertEqual(rows[0][1].split("\n"), ["951327a0c1d2e3f4a5b6", "a6bc148d38ce8c230e96e"])
        # Nothing cut: the whole deployment and acceptance as written, their Markdown not taking effect.
        self.assertEqual(rows[1][1].count("部"), 300)
        self.assertEqual(rows[2][1].count("验"), 300)
        self.assertNotIn("*客户端*", rows[2][1])
        self.assertNotIn("…", json.dumps(fold["elements"], ensure_ascii=False))

    def test_a_change_without_a_deployment_says_so_in_grey(self):
        fold = self.fold(tail(None, self.inputs, self.outputs, change={"commit": "951327a", "deployed": "", "verified": ""}))
        texts = [row["columns"][1]["elements"][0]["content"] for row in fold["elements"]]
        self.assertEqual(texts, ["951327a", "<font color='grey'>未部署</font>", "<font color='grey'>未验收</font>"])

    def test_a_string_of_an_older_release_is_one_grey_line(self):
        old = tail(None, self.inputs, self.outputs, change="已提交 a6bc148 · 已部署 第 322 代 · 未验收")
        line = self.fold(old)
        self.assertEqual(line["tag"], "markdown")
        self.assertIn("改动", line["content"])
        self.assertIn("a6bc148", line["content"])
        relayed = relay_card("T1", DONE, text="好了", inputs=self.inputs, outputs=self.outputs,
                             delivery="未提交 · 未部署 · 未验收")
        self.assertIn("改动", json.dumps(relayed[0], ensure_ascii=False))

    def test_a_relay_with_a_long_change_still_fits_a_card(self):
        from covey.cards.elements import json_size
        from covey.cards.review import ANSWER_CARD_BYTES

        huge = {"commit": "951327a", "deployed": "部" * 2000, "verified": "验" * 2000}
        built, body = relay_card("T1", DONE, text="答" * 7000, inputs=self.inputs, outputs=self.outputs, change=huge)
        self.assertIsNone(built)  # Too big with it: the reply as Markdown, its parts on their own card after it.
        self.assertTrue(body)
        built, _ = relay_card("T1", DONE, text="好了", inputs=self.inputs, outputs=self.outputs, change=huge)
        self.assertLessEqual(json_size(built), ANSWER_CARD_BYTES)

    def test_a_jobs_change_from_its_checkpoint_and_its_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            point, diff = Path(directory) / "cp.json", Path(directory) / "diff.json"
            point.write_text(json.dumps({"delivery": {"commit": "a6bc148d38", "deployed": "第 322 代"}}, ensure_ascii=False))
            self.assertEqual(answer_cards.change(SimpleNamespace(checkpoint_path=str(point))),
                             {"commit": "a6bc148d38", "deployed": "第 322 代", "verified": ""})
            self.assertEqual(answer_cards.change({"checkpoint_path": str(point)})["commit"], "a6bc148d38")
            # A report's turn writing a delivery without a commit: no 改动, unless the Bridge saw files change.
            point.write_text(json.dumps({"delivery": {"verified": "报告已发群"}}, ensure_ascii=False))
            self.assertEqual(answer_cards.change(SimpleNamespace(checkpoint_path=str(point))), {})
            self.assertEqual(answer_cards.change(SimpleNamespace(checkpoint_path=str(point),
                                                                 diff_json_path=str(diff))), {})  # Not written.
            diff.write_text("{}")
            self.assertEqual(answer_cards.change(SimpleNamespace(checkpoint_path=str(point), diff_json_path=str(diff))),
                             {"commit": "", "deployed": "", "verified": "报告已发群"})
            point.write_text(json.dumps({"phase": "x"}))
            self.assertEqual(answer_cards.change(SimpleNamespace(checkpoint_path=str(point), diff_json_path=str(diff))), {})
            self.assertEqual(answer_cards.change(SimpleNamespace(checkpoint_path="")), {})


class TopicReadTest(unittest.TestCase):
    def test_each_step_with_its_times_and_the_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            point = Path(directory) / "cp.json"
            point.write_text(json.dumps({"phase": "改卡片", "steps": [
                {"title": "读代码", "state": "done"}, {"title": "跳过的", "state": "done"},
                {"title": "改卡片", "state": "doing"}, {"title": "写测试", "state": "todo"}],
                "delivery": {"commit": "a6bc148", "verified": "全量测试"}}, ensure_ascii=False))
            t0 = 1_800_000_000
            record = {"checkpoint_path": str(point), "status": "running",
                      "step_times": {"读代码": {"started": t0, "ended": t0 + 1800}, "跳过的": {"ended": t0 + 1800},
                                     "改卡片": {"started": t0 + 1800}}}
            lines = butler_tools.checkpoint_lines(record)
            at = lines.index("  检查点步骤（第 3/4 步）：")
            clock = butler_tools.clock
            self.assertEqual(lines[at + 1:at + 5], [
                f"    1. [完成] 读代码 · {clock(t0)} 开始 · {clock(t0 + 1800)} 完成 · 用时 30 分钟",
                f"    2. [完成] 跳过的 · {clock(t0 + 1800)} 完成",
                f"    3. [在做] 改卡片 · {clock(t0 + 1800)} 开始 · 已 " + lines[at + 3].rsplit("已 ", 1)[1],
                "    4. [待做] 写测试"])
            self.assertEqual(lines[-1], "  检查点改动：提交 a6bc148；部署 未部署；验收 全量测试")
            ended = butler_tools.checkpoint_lines({**record, "status": "done"})
            self.assertEqual(ended[at + 3], f"    3. [在做] 改卡片 · {clock(t0 + 1800)} 开始")
            point.write_text(json.dumps({"phase": "改卡片"}, ensure_ascii=False))
            self.assertEqual(butler_tools.checkpoint_lines({"checkpoint_path": str(point)}), ["  检查点阶段：改卡片"])
            # The checkpoint gone (an older turn's), the record's copy.
            kept = butler_tools.checkpoint_lines({"steps": [{"title": "读代码", "state": "done"}], "status": "done",
                                                "delivery": {"commit": "a6bc148"}})
            self.assertEqual(kept, ["  检查点步骤（1/1 步已完成）：", "    1. [完成] 读代码",
                                    "  检查点改动：提交 a6bc148；部署 未部署；验收 未验收"])


if __name__ == "__main__":
    unittest.main()
