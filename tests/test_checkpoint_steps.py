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
from covey.cards import question_cards
from covey.cards.relay_card import DONE, relay_card
from covey.core.checkpoint import (ACCEPTANCE_RULE, ASK_KINDS, ASK_RULE, ASK_SHORT, CHECKPOINT_RULES, PLAN_RULE, DELIVERY_PART, DELIVERY_RULE, IMAGES_RULE, MAX_STEPS,
                                   STEPS_RULE, checked, checkpoint_delivery, checkpoint_rules, checkpoint_steps, change_summary,
                                   clock_steps, delivery_commits, images_note, shows_change, step_words)
from covey.core.longtask import TaskMonitor
from covey.core.text import stamp
from covey.core.status_line import Facts, status_summary, step_text
from covey.core.trace import ToolTrace


def steps(*states):
    return [{"title": f"第{index}项", "state": state} for index, state in enumerate(states, 1)]


class RulesTest(unittest.TestCase):
    def test_the_rules_name_both_fields_briefly(self):
        added = STEPS_RULE + DELIVERY_RULE  # The acceptance line (#21) comes after them; AcceptanceTest keeps it short.
        for words in ('"state":"done|doing|todo"', "同时只有一个 doing", "delivery", '"commit"', '"deployed"',
                      '"verified"'):
            self.assertIn(words, added)
        # Only a turn that changed code writes one, its commits in full (the owner, 2026-11-09).
        for words in ("改了代码、有提交时才写 delivery", "完整提交号", "调研、报告、问答的轮次不写 delivery"):
            self.assertIn(words, added)
        self.assertLessEqual(sum(1 for char in added if "一" <= char <= "鿿"), 150)
        # What ``verified`` and ``deployed`` may claim: only what was run, only what took effect.
        for words in ("verified 只写实际跑过的检查和结果，没验证就留空", "别把推断写成已测", "只提了合并请求的留空"):
            self.assertIn(words, added)

    def test_every_turn_carries_a_few_lines_and_the_rest_only_where_it_is_used(self):
        # 5.9 row 7: one paragraph of 1623 characters with a dozen fields mixed, on every turn. Now a head and nine lines
        # (when to write, the fields, when and how to ask, a plan first), and steps, delivery and show_images apart.
        lines = CHECKPOINT_RULES.split("\n")
        self.assertEqual(lines[0], "这是执行约定（检查点）：")
        self.assertLessEqual(len(lines), 10)
        self.assertTrue(all(line.startswith("- ") for line in lines[1:]))
        for words in ("- 何时写：", "- 字段：", "- 什么时候问主人", "- 要用户决策时：", "- 用户怎么答：", "- 主人要的是方案"):
            self.assertEqual(sum(line.startswith(words) for line in lines), 1, words)
        # The JSON is the same: every field it had, under the same name.
        for field in ("phase", "completed", "artifacts", "external_tasks", "next_step", "requires_user", "question",
                      "updated_at", "questions", '"multiple":true', '"header"', '"options"'):
            self.assertIn(field, CHECKPOINT_RULES)
        for part, field in ((STEPS_RULE, "steps"), (DELIVERY_RULE, "delivery"), (IMAGES_RULE, "show_images")):
            self.assertNotIn(field, CHECKPOINT_RULES)
            self.assertIn(field, part)
            self.assertTrue(part.startswith("- ") and "\n" not in part)
        self.assertEqual(checkpoint_rules(), "\n".join((CHECKPOINT_RULES, STEPS_RULE, DELIVERY_PART, IMAGES_RULE)))
        self.assertEqual(checkpoint_rules(steps=False, delivery=False, images=False), CHECKPOINT_RULES)
        self.assertEqual(checkpoint_rules(steps=False, images=False), CHECKPOINT_RULES + "\n" + DELIVERY_PART)
        # Nothing of the old paragraph is lost: each of its sentences' key words is in one of the parts.
        for words in ("原子替换", "写它不算修改工作区文件", "回读核验", "waiting_for_x", "不要把密钥写入检查点", "提交回答",
                      "不能把未回答部分视为同意或授权", "不得声称已有方案或编造内容", "无限等待输入", "保留查询外部任务的方法",
                      "不得重复提交已存在的训练/部署等外部任务", "没有检查点也一样", "发送文件入口", "不代表已经发送",
                      "不要自己用 lark-cli", "urgent", "让本轮保持运行", "只提了合并请求的留空"):
            self.assertIn(words, checkpoint_rules(), words)

    def test_only_what_the_owner_must_decide_is_asked_and_the_rest_is_done_the_recommended_way(self):
        # P0-B: most questions had their 推荐 option picked, and handoffs had to say "never ask". A turn asks only
        # ASK_SHORT's kinds; what can be undone and has a clear best choice is done that way, with how to change it back
        # in the conclusion; what must be asked waits for the end.
        self.assertIn(ASK_RULE, CHECKPOINT_RULES)
        for kind in ASK_SHORT.split("、"):
            self.assertIn(kind, ASK_KINDS, kind)
        for words in (ASK_KINDS + "照常问", "AskUserQuestion", "request_user_input", "能自己查清的事先查，不问。",
                      "能撤回、你有明确推荐的不问", "按推荐直接做", "替主人选了什么", "要改回怎么做", "revert 的提交号",
                      "没替主人做过这类取舍", "结尾也不追加", "要问的留到最后", "合成一次问"):
            self.assertIn(words, ASK_RULE, words)
        # T923 §6: no line to copy as it stands (every answer ended "要改成 Y，回一句就行", a reading one too).
        for words in ("回一句就行", "按 X 做了"):
            self.assertNotIn(words, CHECKPOINT_RULES, words)
        # Asked the old way below it: the form, and 推荐 kept for what is still asked.
        lines = CHECKPOINT_RULES.split("\n")
        self.assertEqual(lines.index(ASK_RULE) + 1, next(i for i, line in enumerate(lines) if line.startswith("- 要用户决策时：")))
        self.assertIn("（推荐）", CHECKPOINT_RULES)

    def test_a_request_for_a_plan_gets_the_whole_plan_before_any_question(self):
        # P0-B: a request for a plan was met with a row of questions on details not decided yet. The plan first, what is
        # open after it.
        self.assertIn(PLAN_RULE, CHECKPOINT_RULES)
        for words in ("先交完整方案，不先发问题卡问细节", "代价和风险", "按最合理的假设写并标明是假设", "登记进 artifacts",
                      "待定项列在方案后面", "不选它的代价", "照常要问的那几类，才写进 questions"):
            self.assertIn(words, PLAN_RULE, words)
        lines = CHECKPOINT_RULES.split("\n")
        self.assertLess(lines.index(PLAN_RULE), next(i for i, line in enumerate(lines) if line.startswith("- 最终回复")))

    def test_a_stuck_turn_says_what_it_tried(self):
        # #14: collie's BLOCKED report says what was tried; a question that only says "选哪个" leaves the owner guessing.
        self.assertIn("卡住了才来问的，在 question 里写清缺什么、已经试过什么、各是什么结果。", CHECKPOINT_RULES)

    def test_a_turn_without_the_image_rule_gets_it_when_the_owner_speaks_of_an_image(self):
        for text in ("帮我登录一下", "二维码发我", "截个图看看", "给我一张 screenshot", "扫码登录 QR code"):
            self.assertEqual(images_note(text), IMAGES_RULE.removeprefix("- "), text)
        for text in ("", None, "看看昨天的日志", "部署到第 331 代"):
            self.assertEqual(images_note(text), "", text)

    def test_the_rules_teach_how_to_mark_the_recommended_option(self):
        # The cards set an option marked （推荐） apart (cards/streaming.py RECOMMENDED); the rules say how to mark one.
        self.assertIn("在 label 末尾加“（推荐）”（一题只标一个）", CHECKPOINT_RULES)
        self.assertIn("不选它的代价", CHECKPOINT_RULES)

    def test_the_rules_name_the_background_line_the_card_shows(self):
        # T604 #14: a question may carry one line of background, which the card writes under it.
        self.assertIn('"context":"一句背景：这题在改什么、为什么现在问"', CHECKPOINT_RULES)
        self.assertEqual(question_cards.question_context({"question": "端口？", "context": "8080 被网关占了"}),
                         "8080 被网关占了")


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
        self.assertEqual(step_words(steps("done", "todo")), "做完 1/2 步")
        self.assertEqual(step_words(steps("done", "done")), "2/2 步都做完了")
        self.assertEqual(change_summary({}), "")
        self.assertEqual(change_summary({"commit": "951327a0c1d2e3f4", "deployed": "第 331 代（10:11 切换）",
                                         "verified": "群里点开看过"}), "951327a · 第 331 代 · 已自检")
        self.assertEqual(change_summary({"commit": "951327a0c1 a6bc148d38", "deployed": "", "verified": ""}),
                         "951327a +1 · 未部署 · 未自检")
        # No room for "+1" in a topic's side panel: the first commit alone, the rest when opened.
        self.assertEqual(change_summary({"commit": "951327a0c1 a6bc148d38", "deployed": "第 331 代", "verified": "x"}),
                         "951327a · 第 331 代 · 已自检")
        self.assertEqual(change_summary({"commit": "", "deployed": "只合进 master，没部署", "verified": "全量测试"}),
                         "未提交 · 只合进 mast… · 已自检")
        long = change_summary({"commit": "bridge/20261108-steps-e4486f73 分支", "deployed": "", "verified": ""})
        self.assertEqual(long, "bridge/2026… · 未部署 · 未自检")
        # What the model wrote is its own check, not an acceptance; words that say it was not checked are 未自检.
        for words in ("尚未在手机上验证", "未验证", "没有跑测试", "待验收", "untested", "Not yet verified", "无", " n/a "):
            self.assertFalse(checked({"verified": words}), words)
            self.assertTrue(change_summary({"commit": "951327a", "verified": words}).endswith("未自检"), words)
        for words in ("pytest 14 passed", "未发现回归，全量通过", "没问题，群里点过", "无报错，全量通过", "在验收群点过发送"):
            self.assertTrue(checked({"verified": words}), words)
        self.assertFalse(checked({}) or checked(None) or checked({"verified": "  "}))
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
            self.assertTrue(first.endswith(" · 阶段：收尾 · 做完 2/3 步"), first)
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
        self.assertEqual(step_text(steps("done", "todo"), {}, 1000), ("做完 1/2 步", False))
        facts = Facts(elapsed=65, quiet=3, alive=True, finished=False, phase="改", last_event="尚无阶段进展",
                      checked="01-02 03:04:05", question="", waiting=False, native_question_active=False,
                      activity=None, traced=False, wall=1000.0, stirred=False, retry={}, epoch=1000.0,
                      long_mode=True, frame=0)
        plain = status_summary(facts)
        self.assertNotIn("步骤", plain)
        self.assertIn("阶段：改\n步骤：第 1/1 步：第1项\n", status_summary(facts._replace(steps="第 1/1 步：第1项")))


class AnswerCardTest(unittest.TestCase):
    inputs = ("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", "repo")
    outputs = ("用时 1分", "**69** token/s")

    change = {"commit": "951327a0c1d2e3f4a5b6 a6bc148d38ce8c230e96e", "deployed": "第 331 代，" + "部" * 300,
              "verified": "10:12 在群里点开看过，" + "验" * 300 + " *客户端*"}

    def fold(self, elements):
        """The 改动 fold among ``elements``, inside the fold of the parts to look up when there is one."""
        inner = [part for element in elements for part in (element.get("elements") or [])
                 if element.get("element_id") == "relay_details"]
        return next(element for element in [*elements, *inner] if element.get("element_id") == CHANGE_ID)

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
        # Under 产物, in the one fold of the parts to look up (cards/answer_card.py details), above the 输入/输出 lines.
        self.assertEqual([element.get("element_id") for element in shown], ["relay_details", "answer_facts"])
        self.assertEqual([part.get("element_id") for part in shown[0]["elements"]], ["answer_artifacts", CHANGE_ID])
        fold = self.fold(shown)
        self.assertEqual(fold["tag"], "collapsible_panel")
        self.assertFalse(fold["expanded"])
        title = fold["header"]["title"]
        self.assertEqual((title["content"], title["text_size"], title["text_color"]),
                         ("改动 · 951327a · 第 331 代 · 已自检", "notation", "grey"))
        rows = [[column["elements"][0]["content"] for column in row["columns"]] for row in fold["elements"]]
        self.assertEqual([label for label, _ in rows], ["<font color='grey'>" + word + "</font>" for word in ("提交", "部署", "自检")])
        self.assertEqual(rows[0][1].split("\n"), ["951327a0c1d2e3f4a5b6", "a6bc148d38ce8c230e96e"])
        # Nothing cut: the whole deployment and acceptance as written, their Markdown not taking effect.
        self.assertEqual(rows[1][1].count("部"), 300)
        self.assertEqual(rows[2][1].count("验"), 300)
        self.assertNotIn("*客户端*", rows[2][1])
        self.assertNotIn("…", json.dumps(fold["elements"], ensure_ascii=False))

    def test_a_change_without_a_deployment_says_so_in_grey(self):
        fold = self.fold(tail(None, self.inputs, self.outputs, change={"commit": "951327a", "deployed": "", "verified": ""}))
        texts = [row["columns"][1]["elements"][0]["content"] for row in fold["elements"]]
        self.assertEqual(texts, ["951327a", "<font color='grey'>未部署</font>", "<font color='grey'>未自检</font>"])

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
        built, body = relay_card("T1", DONE, text="答" * 7900, inputs=self.inputs, outputs=self.outputs, change=huge)
        self.assertIsNone(built)  # Too big with the card's own parts: the reply as Markdown, its parts on their own card after it.
        self.assertTrue(body)
        built, _ = relay_card("T1", DONE, text="答" * 7000, inputs=self.inputs, outputs=self.outputs, change=huge)
        self.assertLessEqual(json_size(built), ANSWER_CARD_BYTES)  # The 改动 line is as long as its summary, not its text.
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

    def test_a_commit_its_workdir_lacks_is_marked_over_the_change(self):
        # #18: the commit is what the model wrote; the Bridge looks it up in the repository of the turn's workdir.
        from support.git import commit_all, init_git_repo, run_git
        from covey.cards.answer_card import CHECK_ID

        with tempfile.TemporaryDirectory() as directory:
            root, point = Path(directory) / "repo", Path(directory) / "cp.json"
            root.mkdir()
            init_git_repo(root)
            (root / "a.txt").write_text("a")
            commit_all(root)
            head = run_git(root, "rev-parse", "HEAD").strip()
            point.write_text(json.dumps({"delivery": {"commit": f"{head} a6bc148d38", "deployed": "第 322 代"}}))
            job = SimpleNamespace(checkpoint_path=str(point), workdir=root)
            changed = answer_cards.change(job)
            self.assertEqual(changed["not_found"], ["a6bc148d38"])
            shown = tail(None, self.inputs, self.outputs, change=changed)
            # Out on the card, never folded (T647 P1-A2: in the fold it went unseen), over the 改动 alone left to look up.
            self.assertEqual([element.get("element_id") for element in shown], [CHECK_ID, CHANGE_ID, "answer_facts"])
            self.assertEqual(shown[0]["content"], "**提交号对不上**<font color='grey'>：写过的仓库里没有 a6bc148</font>")
            rows = self.fold(shown)["elements"]
            self.assertEqual(rows[0]["columns"][1]["elements"][0]["content"].split("\n"),
                             [head, "a6bc148d38 <font color='grey'>（写过的仓库里没有）</font>"])
            relayed = relay_card("T1", DONE, text="好了", inputs=self.inputs, outputs=self.outputs, change=changed)[0]
            self.assertIn("提交号对不上", json.dumps(relayed, ensure_ascii=False))
            # Every commit there: the 改动 as before, no line.
            point.write_text(json.dumps({"delivery": {"commit": head, "deployed": "第 322 代"}}))
            self.assertNotIn("not_found", answer_cards.change(job))
            # Nothing said when it cannot be told: a turn on a device, no workdir, a workdir no repository is in.
            point.write_text(json.dumps({"delivery": {"commit": "a6bc148d38"}}))
            self.assertIn("not_found", answer_cards.change(job))
            for other in (SimpleNamespace(checkpoint_path=str(point), workdir=root, device="box"),
                          SimpleNamespace(checkpoint_path=str(point), workdir=None),
                          {"checkpoint_path": str(point), "workdir": str(Path(directory) / "gone")}):
                self.assertEqual(answer_cards.change(other), {"commit": "a6bc148d38", "deployed": "", "verified": ""})

    def test_a_home_topics_commit_is_looked_up_in_the_repositories_it_writes_to(self):
        """Seam W1 ↔ W2 (T870): a topic that started in the home runs in a directory no repository is in; its commits are
        in the worktree (or the repository written in place) of each repository it writes to (core/diff.py
        missing_in_repos over app/repos_turn.py write_dirs)."""
        from support.git import commit_all, init_git_repo, run_git
        from support.records import write_topic

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home, state, worktree, point = base / "home", base / "state", base / "wt" / "covey", base / "cp.json"
            home.mkdir()
            worktree.mkdir(parents=True)
            init_git_repo(worktree)
            (worktree / "a.txt").write_text("a")
            commit_all(worktree)
            head = run_git(worktree, "rev-parse", "HEAD").strip()
            write_topic(state, topic_id="t" * 16, home_start=True, workdir=str(home), repos=[
                {"name": "covey", "path": str(base / "covey"), "mode": "write", "worktree": str(worktree), "source": "declared"}])
            point.write_text(json.dumps({"delivery": {"commit": f"{head} a6bc148d38"}}))
            job = SimpleNamespace(checkpoint_path=str(point), workdir=home, topic_id="t" * 16)
            self.assertEqual(answer_cards.change(job, state)["not_found"], ["a6bc148d38"])
            point.write_text(json.dumps({"delivery": {"commit": head}}))
            self.assertNotIn("not_found", answer_cards.change(job, state))
            # Without the state directory: the home alone, which cannot tell; nothing said, as before.
            point.write_text(json.dumps({"delivery": {"commit": "a6bc148d38"}}))
            self.assertNotIn("not_found", answer_cards.change(job))
            # A commit made in another repository's main checkout (no worktree of it): found there, not marked.
            checkout = base / "web"
            checkout.mkdir()
            init_git_repo(checkout)
            (checkout / "b.txt").write_text("b")
            commit_all(checkout)
            other = run_git(checkout, "rev-parse", "HEAD").strip()
            write_topic(state, topic_id="t" * 16, home_start=True, workdir=str(home), repos=[
                {"name": "covey", "path": str(base / "covey"), "mode": "write", "worktree": str(worktree), "source": "declared"},
                {"name": "web", "path": str(checkout), "mode": "write", "source": "touched"}])
            point.write_text(json.dumps({"delivery": {"commit": f"{head} {other}"}}))
            self.assertNotIn("not_found", answer_cards.change(job, state))


class AcceptanceTest(unittest.TestCase):
    """#21: 改了什么 / 去哪看 / 怎么验 of a UI change or a turn the owner asked to accept, from the checkpoint."""

    def test_the_rule_asks_for_it_only_where_it_applies(self):
        # Carried with delivery (core/checkpoint.py checkpoint_rules): a turn that changes something writes both.
        self.assertEqual(DELIVERY_PART, DELIVERY_RULE + "\n- " + ACCEPTANCE_RULE)
        self.assertIn(DELIVERY_PART, checkpoint_rules())
        self.assertNotIn(ACCEPTANCE_RULE, checkpoint_rules(delivery=False))
        self.assertNotIn(ACCEPTANCE_RULE, CHECKPOINT_RULES)
        self.assertIn("改了界面或主人说要验收时，另写 acceptance", ACCEPTANCE_RULE)
        for words in ('"what"', '"where"', '"how"', "原样抄", "回复里没说的别编"):
            self.assertIn(words, ACCEPTANCE_RULE)
        self.assertLessEqual(sum(1 for char in ACCEPTANCE_RULE if "一" <= char <= "鿿"), 70)

    def test_it_is_read_and_its_link_must_be_one_the_owner_can_click(self):
        from covey.core.checkpoint import acceptance_link, checkpoint_acceptance

        self.assertEqual(checkpoint_acceptance({}), {})
        self.assertEqual(checkpoint_acceptance({"acceptance": "看板"}), {})
        self.assertEqual(checkpoint_acceptance({"acceptance": {"where": "https://example.invalid/x"}}), {})  # Nothing to check.
        found = checkpoint_acceptance({"acceptance": {"what": "看板的按钮改成蓝色" * 10, "how": "点开看板"}})
        self.assertEqual((len(found["what"]), found["where"], found["how"]), (60, "", "点开看板"))
        self.assertEqual(acceptance_link("预览：https://example.invalid/board，点开看"), "https://example.invalid/board")
        self.assertEqual(acceptance_link("applink.feishu.cn/client/thread/open?x=1"),
                         "https://applink.feishu.cn/client/thread/open?x=1")
        for local in ("http://localhost:8080/board", "本地 http://127.0.0.1:3000 看过", "/tmp/board.html", ""):
            self.assertEqual(acceptance_link(local), "", local)

    def test_the_answer_card_has_three_lines_above_the_criteria(self):
        from covey.cards.answer_card import ACCEPTANCE_ID, CRITERIA_ID

        with tempfile.TemporaryDirectory() as directory:
            topic = Path(directory) / "topics" / "t1" / "topic.json"
            topic.parent.mkdir(parents=True)
            topic.write_text(json.dumps({"criteria": ["按钮是蓝色"]}, ensure_ascii=False))
            point = Path(directory) / "cp.json"
            point.write_text(json.dumps({"acceptance": {"what": "看板按钮改成蓝色", "how": "打开看板，按钮应是蓝色",
                                                        "where": "http://localhost:8080"}}, ensure_ascii=False))
            notes = answer_cards.notes(Path(directory), SimpleNamespace(topic_id="t1", checkpoint_path=str(point)))
            self.assertEqual([element["element_id"] for element in notes], [ACCEPTANCE_ID, CRITERIA_ID])
            rows = notes[0]["columns"][0]["elements"]
            self.assertEqual([[column["elements"][0]["content"] for column in row["columns"]] for row in rows], [
                ["<font color='grey'>改了什么</font>", "看板按钮改成蓝色"],
                ["<font color='grey'>去哪看</font>", "<font color='grey'>只做了本地验证</font>"],
                ["<font color='grey'>怎么验</font>", "打开看板，按钮应是蓝色"]])
            point.write_text(json.dumps({"acceptance": {"what": "看板按钮改成蓝色",
                                                        "where": "https://example.invalid/board"}}, ensure_ascii=False))
            rows = answer_cards.notes(Path(directory), SimpleNamespace(topic_id="", checkpoint_path=str(point)))[0]
            texts = [row["columns"][1]["elements"][0]["content"] for row in rows["columns"][0]["elements"]]
            self.assertEqual(texts[0], "看板按钮改成蓝色")
            self.assertTrue(texts[1].startswith("[https://example") and texts[1].endswith("](https://example.invalid/board)"))
            point.write_text(json.dumps({"delivery": {"commit": "951327a"}}))
            self.assertEqual(answer_cards.notes(Path(directory), SimpleNamespace(topic_id="", checkpoint_path=str(point))), [])


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
            clock = stamp
            self.assertEqual(lines[at + 1:at + 5], [
                f"    1. [做完] 读代码 · {clock(t0)} 开始 · {clock(t0 + 1800)} 做完 · 用时 30 分钟",
                f"    2. [做完] 跳过的 · {clock(t0 + 1800)} 做完",
                f"    3. [在做] 改卡片 · {clock(t0 + 1800)} 开始 · 已 " + lines[at + 3].rsplit("已 ", 1)[1],
                "    4. [待做] 写测试"])
            self.assertEqual(lines[-1], "  检查点改动：提交 a6bc148；部署 未部署；自检 全量测试")
            ended = butler_tools.checkpoint_lines({**record, "status": "done"})
            self.assertEqual(ended[at + 3], f"    3. [在做] 改卡片 · {clock(t0 + 1800)} 开始")
            point.write_text(json.dumps({"phase": "改卡片"}, ensure_ascii=False))
            self.assertEqual(butler_tools.checkpoint_lines({"checkpoint_path": str(point)}), ["  检查点阶段：改卡片"])
            # The checkpoint gone (an older turn's), the record's copy.
            kept = butler_tools.checkpoint_lines({"steps": [{"title": "读代码", "state": "done"}], "status": "done",
                                                "delivery": {"commit": "a6bc148"}})
            self.assertEqual(kept, ["  检查点步骤（1/1 步都做完了）：", "    1. [做完] 读代码",
                                    "  检查点改动：提交 a6bc148；部署 未部署；自检 未自检"])


if __name__ == "__main__":
    unittest.main()
