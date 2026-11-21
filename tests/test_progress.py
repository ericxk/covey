"""A running turn's progress (core/progress.py, docs/reference.md 任务卡): the one pure function every reader shares, the
words they show it with, the task monitor that keeps it on the record and tells the card when the turn may be stuck
(core/longtask.py), the 看看 button (Cards.show_stuck) and its read-only look (app/stuck_check.py), and the board's readers
(the pinned status cards, 手上的事, 早晚汇总, the butler's snapshot) that write its phase and estimate, never a command."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_job

from covey.core import progress
from covey.core.longtask import TaskMonitor

NOW = 1_800_000_000.0
STEPS = [{"title": "读代码", "state": "done"}, {"title": "改卡片", "state": "done"}, {"title": "跑测试", "state": "doing"},
         {"title": "提交", "state": "todo"}, {"title": "部署", "state": "todo"}]


# When the Bridge saw each step start and end (core/checkpoint.py clock_steps): the plan first seen 1200 s ago with 读代码
# doing, two steps done in 1080 s (540 s each), 跑测试 on for 120 s since.
TIMES = {"读代码": {"started": NOW - 1200, "ended": NOW - 660}, "改卡片": {"ended": NOW - 120},
         "跑测试": {"started": NOW - 120}}


def running(**fields):
    return {"status": "running", "phase": "修进度", "steps": STEPS, "step_times": TIMES,
            "timings": {"backend_requested": NOW - 1200}, "last_activity_at": NOW - 120, "last_activity": "在跑测试",
            **fields}


class ProgressTest(unittest.TestCase):
    def test_the_eight_fields_from_the_record(self):
        found = progress.progress(running(), now=NOW, stuck_after=1800)
        self.assertEqual(sorted(found), sorted(progress.FIELDS))
        self.assertEqual((found["phase"], found["plan_done"], found["plan_total"]), ("修进度", 2, 5))
        self.assertEqual((found["last_activity_at"], found["last_activity"]), (int(NOW - 120), "在跑测试"))
        self.assertEqual(found["eta_seconds"], 1500)  # 540 s a step, 3 left, less the 120 s 跑测试 has had.
        self.assertIsNone(found["slow_step"])
        self.assertFalse(found["maybe_stuck"])

    def test_the_estimate_is_put_towards_the_topics_usual_turn(self):
        found = progress.progress(running(), now=NOW, history=[600, 3000, 2400], stuck_after=1800)
        self.assertEqual(found["eta_seconds"], 1320)  # Halfway between 1500 and the median's 2400 - 1200, to the minute.
        # A turn already longer than the usual one: the steps alone.
        self.assertEqual(progress.progress(running(), now=NOW, history=[300, 400, 500])["eta_seconds"], 1500)

    def test_no_estimate_rather_than_a_wrong_one(self):
        ONE = {"读代码": {"started": NOW - 1200, "ended": NOW - 300}, "跑测试": {"started": NOW - 300}}
        cases = {"no steps": running(steps=[]), "none done": running(steps=[{"title": "a", "state": "doing"}]),
                 "all done": running(steps=[{"title": "a", "state": "done"}]),
                 "its first minute": running(timings={"backend_requested": NOW - 30}),
                 "queued": running(status="queued"),
                 "one step done and no history": running(steps=STEPS[:1] + STEPS[2:], step_times=ONE),
                 "no times of the steps (an older task runner)": running(step_times=None)}
        for name, record in cases.items():
            with self.subTest(name):
                self.assertIsNone(progress.progress(record, now=NOW)["eta_seconds"])
        one = running(steps=STEPS[:1] + STEPS[2:], step_times=ONE)
        self.assertIsNotNone(progress.progress(one, now=NOW, history=[900, 1300, 2000])["eta_seconds"])

    def test_the_estimate_goes_down_while_a_step_takes_long_and_then_says_the_step_instead(self):
        # T488, 11-19 16:58 to 17:14: one step for 40 minutes, and the old estimate (time run / steps done * steps left)
        # grew from 43 minutes to 1 hour 5 minutes as it waited.
        seen = [progress.progress(running(), now=NOW + wait, stuck_after=7200) for wait in range(0, 3600, 60)]
        etas = [found["eta_seconds"] for found in seen if found["eta_seconds"] is not None]
        self.assertEqual(etas, sorted(etas, reverse=True))
        self.assertEqual((etas[0], etas[-1]), (1500, 1080))  # Down by a minute a minute until 跑测试 took its 540 s.
        slow = seen[30]["slow_step"]  # Half an hour later: no figure, the step and the average one instead.
        self.assertIsNone(seen[30]["eta_seconds"])
        self.assertEqual(slow, {"number": 3, "since": int(NOW - 120), "seconds": 1920, "usual": 540})
        self.assertEqual(progress.slow_words(seen[30]), "第 3 步已做 32 分钟，此前平均每步约 9 分钟")
        self.assertEqual(progress.slow_words(seen[30], clock=lambda at: "16:33"), "第 3 步 16:33 开始，此前平均每步约 9 分钟")
        self.assertEqual(progress.board_words(seen[30], NOW, clock=True), ["第 3 步比此前几步慢"])
        self.assertEqual(progress.slow_words(seen[0]), "")
        # Once 跑测试 is done the pace counts it: three steps in 3000 s, two left.
        done = progress.progress(running(steps=[dict(step, state="done") if step["title"] == "跑测试" else step
                                                for step in STEPS],
                                         step_times={**TIMES, "跑测试": {"started": NOW - 120, "ended": NOW + 1800}}),
                                 now=NOW + 1800)
        self.assertEqual((done["eta_seconds"], done["slow_step"]), (2000 // 60 * 60, None))

    def test_steps_done_when_the_plan_was_first_seen_are_not_this_turns_pace(self):
        # A continued topic's checkpoint: two steps done in an earlier turn, seen done at once 3 minutes in.
        carried = {"读代码": {"ended": NOW - 1020}, "改卡片": {"ended": NOW - 1020}, "跑测试": {"started": NOW - 1020}}
        found = progress.progress(running(step_times=carried), now=NOW, history=[900, 1300, 2000])
        self.assertEqual((found["eta_seconds"], found["slow_step"]), (None, None))
        # The first step this turn did is its pace: 跑测试 in 600 s, 2 left, history enough for one step.
        steps = [dict(step, state="done") if step["title"] == "跑测试" else step for step in STEPS]
        later = {**carried, "跑测试": {"started": NOW - 1020, "ended": NOW - 420}}
        found = progress.progress(running(steps=steps, step_times=later), now=NOW, history=[900, 1000, 1100])
        self.assertEqual(found["eta_seconds"], 780)  # 600 * 2 - 420; the usual turn is shorter than this one so far.

    def test_the_checkpoint_wins_over_the_record_and_a_seed_phase_is_none(self):
        found = progress.progress(running(), checkpoint={"phase": "准备处理本轮请求", "steps": STEPS[:2]}, now=NOW)
        self.assertEqual((found["phase"], found["plan_done"], found["plan_total"]), ("", 2, 2))

    def test_the_trace_names_the_last_activity_in_words_never_a_command(self):
        trace = {"steps": {"a": {"title": "读取文件", "detail": "/repo/covey/app/runs.py", "status": "done",
                                 "started": NOW - 300, "ended": NOW - 290},
                           "b": {"title": "执行命令", "detail": "跑单测 · python3 -B -m unittest tests.test_x", "status": "running",
                                 "started": NOW - 60}}}
        found = progress.progress(running(last_activity_at=NOW - 500), trace=trace, now=NOW)
        self.assertEqual((found["last_activity_at"], found["last_activity"]), (int(NOW - 60), "在跑测试"))
        self.assertNotIn("unittest", str(found))

    def test_a_record_of_an_older_runner_reads_its_health_check(self):
        record = {"status": "running", "health_checked_at": "2027-01-15T08:00:00+00:00", "last_progress_age_seconds": 2000}
        now = 1_800_000_000.0  # 2027-01-15T08:00:00Z.
        found = progress.progress(record, now=now, stuck_after=1800)
        self.assertEqual((found["last_activity_at"], found["last_activity"], found["maybe_stuck"]), (int(now - 2000), "", True))

    def test_maybe_stuck_after_the_threshold_only_while_it_runs_and_waits_for_nothing(self):
        quiet = running(last_activity_at=NOW - 1900)
        self.assertTrue(progress.progress(quiet, now=NOW, stuck_after=1800)["maybe_stuck"])
        self.assertFalse(progress.progress(quiet, now=NOW, stuck_after=1800, waiting=True)["maybe_stuck"])
        for status in ("queued", "cancel_requested", "done"):
            self.assertFalse(progress.progress(running(status=status, last_activity_at=NOW - 9000), now=NOW)["maybe_stuck"])
        unknown = {"status": "running", "timings": {"backend_requested": NOW - 9000}}  # Nothing the monitor wrote.
        self.assertFalse(progress.progress(unknown, now=NOW)["maybe_stuck"])

    def test_the_threshold_comes_from_the_environment(self):
        for value, minutes in (("", 30), ("5", 5), ("0", 30), ("abc", 30), ("2000", 30), ("1440", 1440)):
            with self.subTest(value):
                self.assertEqual(progress.stuck_seconds({progress.STUCK_ENV: value}), minutes * 60)

    def test_the_earlier_turns_are_the_done_ones_but_this(self):
        records = [{"job_id": 1, "status": "done", "timings": {"backend_requested": 0, "execution_finished": 600}},
                   {"job_id": 2, "status": "failed", "timings": {"backend_requested": 0, "execution_finished": 60}},
                   {"job_id": 3, "status": "done", "timings": {"backend_requested": 0}},  # Never finished.
                   {"job_id": 4, "status": "done", "timings": {"backend_requested": 100, "execution_finished": 1300}}]
        self.assertEqual(progress.turn_seconds(records), [600, 1200])
        self.assertEqual(progress.turn_seconds(records, exclude=4), [600])


class WordsTest(unittest.TestCase):
    def test_a_step_in_a_few_words(self):
        cases = [({"title": "执行命令", "detail": "python3 -m pytest -q", "status": "running"}, "在跑测试"),
                 ({"title": "执行命令", "detail": "git commit -m x", "status": "done"}, "提交了代码"),
                 ({"title": "执行命令", "detail": "装依赖 · pip install x", "status": "done"}, "跑了命令：装依赖"),
                 ({"title": "执行命令", "detail": "make build", "status": "failed"}, "跑命令失败"),
                 ({"title": "读取文件", "detail": "/a/b/covey/app/runs.py", "status": "running"}, "在读 app/runs.py"),
                 ({"title": "修改文件", "detail": "/a/cards/streaming.py, /a/x.py", "status": "done"}, "改了 cards/streaming.py"),
                 ({"title": "搜索网页", "status": "running"}, "在查网页"),
                 ({"title": "进展说明", "detail": "x", "status": "note"}, "更新了进展说明"),
                 ({"title": "tool", "status": "done"}, "调用了工具")]
        for step, words in cases:
            with self.subTest(words):
                self.assertEqual(progress.activity_words(step), words)

    def test_a_sub_tasks_step_names_it(self):
        trace = {"steps": {}, "agents": {"a1": {"name": "标注第 1 块", "status": "running", "started_at": NOW - 900}},
                 "children": {"a1": {"steps": {"c": {"title": "执行命令", "detail": "pytest", "status": "running",
                                                     "started": NOW - 100}}}}}
        self.assertEqual(progress.newest_activity(trace, NOW), (NOW - 100, "子任务「标注第 1 块」在跑测试"))

    def test_times_are_to_the_minute(self):
        self.assertEqual([progress.ago_words(s) for s in (5, 59, 60, 150, 3600, 3900)],
                         ["刚刚", "刚刚", "1 分钟前", "2 分钟前", "1 小时前", "1 小时 5 分钟前"])
        self.assertEqual([progress.eta_words(s) for s in (None, 30, 420, 2400, 2520, 4700)],
                         ["", "预计还要约 1 分钟", "预计还要约 7 分钟", "预计还要约 40 分钟", "预计还要约 40 分钟",
                          "预计还要约 1 小时 20 分钟"])
        self.assertEqual(progress.stuck_words(1800), "超过 30 分钟没有动静，可能卡住了")
        found = progress.progress(running(), now=NOW)
        self.assertEqual(progress.activity_line(found, NOW), "2 分钟前在跑测试")

    def test_a_board_says_the_estimate_as_a_time_of_day_or_that_it_may_be_stuck(self):
        with mock.patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
            time.tzset()
            try:
                at = time.mktime((2026, 11, 18, 14, 0, 0, 0, 0, -1))
                self.assertEqual(progress.finish_words({"eta_seconds": 2400}, at), "预计 14:40 左右做完")
                self.assertEqual(progress.finish_words({"eta_seconds": 2520}, at), "预计 14:40 左右做完")  # To five minutes.
                self.assertEqual(progress.finish_words({"eta_seconds": 12 * 3600}, at), "预计明天 02:00 左右做完")
                self.assertEqual(progress.board_words({"eta_seconds": 2400, "maybe_stuck": False}, at, clock=True),
                                 ["预计 14:40 左右做完"])
                self.assertEqual(progress.board_words({"eta_seconds": 2400, "maybe_stuck": True}, at), ["可能卡住了"])
                self.assertEqual(progress.board_words({"eta_seconds": None, "maybe_stuck": False}, at), [])
            finally:
                time.tzset()


class MonitorTest(unittest.TestCase):
    def monitor(self, wall, *, history=(), stuck=None, records=None, updates=None):
        from covey.core.trace import ToolTrace
        job = make_job(tool_trace=ToolTrace(), backend_process=SimpleNamespace(poll=lambda: None),
                       timings={"backend_requested": NOW - 1200})
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [path.unlink() for path in folder.glob("*")] and None)
        checkpoint = folder / "cp.json"
        checkpoint.write_text('{"phase": "修进度", "steps": %s}' % __import__("json").dumps(STEPS, ensure_ascii=False))
        monitor = TaskMonitor(job, checkpoint, (updates if updates is not None else []).append,
                              lambda **fields: records is not None and records.append(fields), lambda *a: True,
                              lambda _: None, lambda: None, clock=lambda: wall[0], wall=lambda: wall[0], history=history,
                              stuck=stuck)
        return job, monitor

    def walk(self, monitor, wall):
        """The turn's plan as the model writes it: 读代码 doing at NOW, done at NOW + 300, 改卡片 done at NOW + 600, the
        monitor ticking at each; then at NOW + 720 跑测试 has had 120 s of a 300 s pace, three steps left."""
        import json
        for at, done in ((NOW, 0), (NOW + 300, 1), (NOW + 600, 2)):
            wall[0] = at
            steps = [dict(step, state="done" if index < done else "doing" if index == done else "todo")
                     for index, step in enumerate(STEPS)]
            monitor.checkpoint.write_text(json.dumps({"phase": "修进度", "steps": steps}, ensure_ascii=False))
            monitor.tick()
        wall[0] = NOW + 720
        monitor.last_ui = float("-inf")
        monitor.tick()

    def test_the_status_line_says_the_estimate_and_the_record_keeps_the_last_activity(self):
        wall, records, updates = [NOW], [], []
        job, monitor = self.monitor(wall, records=records, updates=updates)
        job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "跑单测 · pytest -q",
                                       "started_at": NOW + 600})
        self.walk(monitor, wall)
        self.assertIn(" · 预计还要约 15 分钟", updates[-1])  # 300 s a step, 3 left, less 120 s: 13 to five.
        self.assertIn(" · 在等一条命令跑完 · 已 2 分钟", updates[-1])  # A command still running is waited for, not "quiet".
        self.assertNotIn("pytest", updates[-1].split("当前：")[0])
        self.assertEqual((records[-1]["last_activity"], records[-1]["last_activity_at"]), ("在跑测试", int(NOW + 600)))
        self.assertEqual(job.turn_progress["plan_total"], 5)
        # 跑测试 longer than the 300 s pace: the line says the average step beside its own 本步, no figure.
        wall[0] = NOW + 1200
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("本步 10 分钟 · 此前平均每步约 5 分钟", updates[-1])
        self.assertNotIn("预计", updates[-1])
        self.assertEqual(job.turn_progress["slow_step"]["number"], 3)

    def test_the_record_keeps_the_turns_first_estimate_of_its_whole_time(self):
        # 预计 vs 实际 on the web page: run so far + the first eta (1800 s, made as 改卡片 ended: 300 s a step, 3 left), not a later one.
        wall, records = [NOW], []
        _, monitor = self.monitor(wall, records=records)
        self.walk(monitor, wall)
        self.assertEqual([fields.get("estimate_seconds") for fields in records], [None, None, 2700, 2700])

    def test_a_turn_with_no_estimate_yet_writes_none(self):
        records = []
        with mock.patch.object(progress, "estimate", return_value=(None, None)):
            _, monitor = self.monitor([NOW], records=records)
            monitor.tick()
        self.assertNotIn("estimate_seconds", records[-1])

    def test_a_command_purpose_the_line_names_already_is_not_said_twice(self):
        # accept15: "当前：执行命令 · 前台睡眠 300 秒 · 2 分钟前在跑命令：前台睡眠 300 秒".
        wall, updates = [NOW], []
        job, monitor = self.monitor(wall, updates=updates)
        job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "running",
                                       "detail": "前台睡眠 300 秒 · python3 -c 'import time; time.sleep(300)'",
                                       "started_at": NOW})
        monitor.tick()
        wall[0] = NOW + 150
        monitor.last_ui = float("-inf")
        monitor.tick()
        line = updates[-1].split("现在：")[1].split("\n")[0]
        self.assertEqual(line.count("前台睡眠 300 秒"), 1, line)
        self.assertTrue(line.endswith(" · 在等一条命令跑完 · 已 2 分钟"), line)

    def test_the_card_is_told_once_when_it_may_be_stuck_and_once_when_it_moves_again(self):
        wall, told, updates = [NOW], [], []
        with mock.patch.dict(os.environ, {progress.STUCK_ENV: "2"}):
            job, monitor = self.monitor(wall, stuck=told.append, updates=updates)
        monitor.tick()
        for seconds in (60, 119, 121, 150):
            wall[0] = NOW + seconds
            monitor.last_ui = float("-inf")
            monitor.tick()
        self.assertEqual(told, [True])
        self.assertIn("超过 2 分钟没有动静，可能卡住了", updates[-1])
        monitor.output()
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertEqual(told, [True, False])
        self.assertNotIn("可能卡住了", updates[-1])


class CardsTest(unittest.TestCase):
    def cards(self, writes, ok=True):
        from covey.app.cards import Cards
        cards = Cards.__new__(Cards)
        cards._write = lambda ctx, job, method, part, body, name: writes.append((method, part, body)) or ({} if ok else None)
        return cards

    def test_look_goes_under_the_status_line_and_comes_off_when_the_turn_moves_or_ends(self):
        from covey.app.task_card import StreamingReply
        from covey.cards.task_status import LOOK_ID, LOOK_KIND, STATUS_MORE_ID
        writes = []
        cards = self.cards(writes)
        job = make_job(topic_id="t" * 32, job_id=7)
        job.progress = StreamingReply("card_1", "om_card", status_rows=True)
        self.assertTrue(cards.show_stuck(None, job, True))
        self.assertFalse(cards.show_stuck(None, job, True))  # Shown already: no second write.
        method, part, body = writes[0]
        self.assertEqual((method, part, body["type"], body["target_element_id"]), ("POST", "elements", "insert_after", STATUS_MORE_ID))
        button = __import__("json").loads(body["elements"])[0]["columns"][0]["elements"][0]
        self.assertEqual(button["value"], {"kind": LOOK_KIND, "topic_id": "t" * 32, "job_id": 7})
        self.assertTrue(cards.show_stuck(None, job, False))
        self.assertEqual(writes[-1][:2], ("DELETE", f"elements/{LOOK_ID}"))
        self.assertFalse(job.progress.look_shown)

    def test_not_put_on_a_card_stopping_and_a_refused_write_is_not_counted_as_shown(self):
        from covey.app.task_card import StreamingReply
        writes = []
        job = make_job(topic_id="t" * 32, job_id=7)
        job.progress = StreamingReply("card_1", "om_card")
        job.cancel_requested_at = time.time()
        self.assertFalse(self.cards(writes).show_stuck(None, job, True))
        self.assertEqual(writes, [])
        job.cancel_requested_at = None
        self.assertFalse(self.cards(writes, ok=False).show_stuck(None, job, True))
        self.assertFalse(job.progress.look_shown)
        self.assertEqual(writes[0][2]["target_element_id"], "activity")  # A card without the two-part line.

    def test_the_kind_is_the_bridges_and_starts_no_task(self):
        from covey.app.card_actions import ACTIONS, BOT_ONLY
        from covey.cards.task_status import LOOK_KIND
        self.assertIn(LOOK_KIND, BOT_ONLY)  # Never forwarded to the task runner, which may be the stuck one.
        self.assertEqual(ACTIONS[LOOK_KIND].handler, "stuck_check.look_action")


class LookTest(unittest.TestCase):
    def found(self, processes, *, runner=True, detached=True):
        return {"progress": progress.progress(running(), now=NOW), "runner": runner, "detached": detached,
                "processes": processes, "log_quiet": 90.0, "log": ["x"], "steps": [{"at": int(NOW - 120), "did": "在跑测试"}]}

    def test_the_rules_sentence(self):
        from covey.app import stuck_check
        cases = [
            (self.found([], runner=False), "任务进程已经不在了"),
            (self.found([{"name": "claude", "state": "S", "depth": 1, "busy": False},
                         {"name": "pytest", "state": "R", "depth": 2, "busy": True}]), "还在干活：pytest 正在用 CPU"),
            (self.found([{"name": "claude", "state": "S", "depth": 1, "busy": False},
                         {"name": "sleep", "state": "S", "depth": 2, "busy": False}]), "在等命令 sleep 返回"),
            (self.found([{"name": "claude", "state": "S", "depth": 1, "busy": False}]), "像是在等模型回复"),
            (self.found([{"name": "claude", "state": "R", "depth": 1, "busy": True}]), "后端进程在用 CPU"),
            (self.found([], runner=False, detached=False), "这台机器上看不到"),
        ]
        for found, words in cases:
            with self.subTest(words):
                sentence = stuck_check.verdict(found, NOW)
                self.assertIn(words, sentence)
                self.assertIn("2 分钟前在跑测试", sentence)
        text = stuck_check.reply_text(cases[1][0], "还在跑", 7, NOW)
        self.assertTrue(text.startswith("🔍 看了一下任务 #7：还在跑\n- 阶段：修进度（步骤 2/5）"), text)
        self.assertIn("- 下面的进程：claude（空闲）、pytest（在用 CPU）", text)

    def test_which_processes_use_the_cpu(self):
        from covey.app import stuck_check
        busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
        idle = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: [proc.kill() or proc.wait() for proc in (busy, idle)])
        rows = {row["name"]: row for row in stuck_check.processes(os.getpid(), sample=0.5)}
        self.assertTrue(rows[Path(sys.executable).name[:15]]["busy"] if Path(sys.executable).name[:15] in rows
                        else any(row["busy"] for row in rows.values()))
        self.assertFalse(rows["sleep"]["busy"])
        self.assertEqual(rows["sleep"]["depth"], 1)

    def app(self, record, replies):
        from covey.app.stuck_check import StuckCheck
        harness = SimpleNamespace(voice=SimpleNamespace(key=lambda name, event: f"{name}-{event}"),
                                  topic_record=lambda ctx, chat, job_id: record, spec=SimpleNamespace(name="codex"))
        messenger = SimpleNamespace(reply=lambda ctx, chat, text, key, voice: replies.append((chat, text, key)))
        return StuckCheck(harness, runtime=SimpleNamespace(log=lambda line: None), messenger=messenger)

    def test_the_small_model_words_it_on_any_harness_unless_the_topic_redacts(self):
        # #17: a Codex topic's 看看 is worded by Haiku through this Bridge's Claude Harness; without one, or for a topic
        # that redacts, the rules' sentence stands.
        from covey.app import stuck_check
        from covey.app.stuck_check import StuckCheck
        from covey.core.redaction import TOPIC_KEY
        from covey.core.state import write_json

        state = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        write_json(state / "topics" / "t1" / "topic.json", {"topic_id": "t1"})
        claude = SimpleNamespace(harness=SimpleNamespace(backend="claude-backend",
                                                         context=SimpleNamespace(backend_cmd=["claude"], state_dir=state)))
        codex = SimpleNamespace(spec=SimpleNamespace(name="codex"), backend="codex-backend", voice=None)
        ctx = SimpleNamespace(state_dir=state, backend_cmd=["codex"])
        record = {"topic_id": "t1"}
        with mock.patch.object(stuck_check, "ask_model", return_value="还在跑测试") as ask:
            check = StuckCheck(codex, runtime=None, messenger=None, topics=SimpleNamespace(apps={"claude": claude}))
            self.assertEqual(check.worded(ctx, record, {}, "规则"), "还在跑测试")
            self.assertEqual(ask.call_args.args[2:], ("claude-backend", claude.harness.context))
            alone = StuckCheck(codex, runtime=None, messenger=None, topics=SimpleNamespace(apps={}))
            self.assertEqual(alone.worded(ctx, record, {}, "规则"), "")  # No Claude Harness here.
            own = StuckCheck(SimpleNamespace(spec=SimpleNamespace(name="claude"), backend="own", voice=None),
                             runtime=None, messenger=None)
            self.assertEqual(own.worded(ctx, record, {}, "规则"), "还在跑测试")  # A Claude topic asks its own, as before.
            self.assertEqual(ask.call_args.args[2:], ("own", ctx))
            write_json(state / "topics" / "t1" / "topic.json", {"topic_id": "t1", TOPIC_KEY: True})
            calls = ask.call_count
            self.assertEqual(check.worded(ctx, record, {}, "规则"), "")  # It redacts: nothing goes to a model.
            self.assertEqual(ask.call_count, calls)

    def test_a_click_on_an_ended_turn_says_so_and_a_running_one_is_looked_at_once(self):
        from covey.app import stuck_check
        replies = []
        ctx = SimpleNamespace(topic_id="t" * 32, state_dir=Path(tempfile.mkdtemp()))
        event = {"chat_id": "oc_chat", "event_id": "ev1",
                 "action_value": {"kind": "bridge_job_look", "topic_id": "t" * 32, "job_id": 7}}
        self.assertTrue(self.app({"job_id": 7, "status": "done", "key": "k7"}, replies).look_action(ctx, event))
        self.assertIn("已经结束了", replies[-1][1])
        app = self.app({"job_id": 7, "status": "running", "key": "k7", "topic_id": "t" * 32}, replies)
        started = []
        with mock.patch.object(stuck_check.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: started.append(kw))):
            app.look_action(ctx, event)
            app.look_action(ctx, event)  # A second click while it looks adds nothing.
        self.assertEqual(len(started), 1)
        app.looking.clear()
        app.look(ctx, "oc_chat", {"job_id": 7, "status": "running", "key": "k7", "topic_id": "t" * 32}, "ev1")
        self.assertTrue(replies[-1][1].startswith("🔍 看了一下任务 #7："), replies[-1])
        self.assertEqual(replies[-1][2], "stuck-look-ev1")


class RunningCommandTest(unittest.TestCase):
    """A turn whose newest step is a command still running waits for it: never 可能卡住, however long (the quiet-turn rule
    stays for every other turn)."""

    def trace(self, *, status="running", at=NOW - 2400, name="bash"):
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("tool", {"id": "c", "name": name, "status": status, "detail": "跑评测 · python3 eval.py", "started_at": at,
                              "ended_at": None if status == "running" else at + 5})
        return trace

    def test_a_running_command_is_never_stuck_and_says_how_long_it_has_run(self):
        record = running(last_activity_at=NOW - 2400, command_since=NOW - 2400)
        found = progress.progress(record, now=NOW, stuck_after=1800)
        self.assertFalse(found["maybe_stuck"])
        self.assertEqual(found["command"], {"since": int(NOW - 2400), "seconds": 2400, "long": True})
        self.assertEqual(progress.command_words(found), "在等一条命令跑完 · 已 40 分钟")
        self.assertEqual(progress.board_words(found, NOW), ["在等一条命令跑完"])
        # A turn quiet as long with no command running keeps the 30-minute rule.
        quiet = progress.progress(running(last_activity_at=NOW - 2400), now=NOW, stuck_after=1800)
        self.assertTrue(quiet["maybe_stuck"])
        self.assertNotIn("command", quiet)
        self.assertEqual(progress.board_words(quiet, NOW), ["可能卡住了"])

    def test_a_short_command_is_not_on_the_board_and_a_trace_gives_the_same(self):
        found = progress.progress(running(), now=NOW, trace=self.trace(at=NOW - 300), stuck_after=1800)
        self.assertEqual(found["command"], {"since": int(NOW - 300), "seconds": 300, "long": False})
        self.assertNotIn(progress.COMMAND_WAIT, progress.board_words(found, NOW))
        long = progress.progress(running(), now=NOW, trace=self.trace(), stuck_after=1800)
        self.assertEqual((long["maybe_stuck"], long["command"]["seconds"]), (False, 2400))

    def test_only_the_newest_step_being_a_running_command_counts(self):
        self.assertEqual(progress.running_command(self.trace(), NOW), NOW - 2400)
        self.assertIsNone(progress.running_command(self.trace(name="read"), NOW))
        self.assertIsNone(progress.running_command(self.trace(status="done"), NOW))
        trace = self.trace()
        trace.accept("tool", {"id": "r", "name": "read", "status": "done", "detail": "app/runs.py", "started_at": NOW - 100,
                              "ended_at": NOW - 90})  # Newer: the model moved on while the command goes on.
        self.assertIsNone(progress.running_command(trace, NOW))
        ended = self.trace()
        ended.finish()  # The turn ended: the step reads unconfirmed, nothing waits.
        self.assertIsNone(progress.running_command(ended, NOW))

    def test_the_hold_is_bounded_by_what_the_bridge_knows(self):
        held = dict(last_activity_at=NOW - 7200, command_since=NOW - 7200)
        self.assertIn("command", progress.progress(running(**held), now=NOW, stuck_after=1800))
        # The backend process has exited but the command's end was never recorded: it cannot hold the turn for ever.
        gone = progress.progress(running(process_alive=False, **held), now=NOW, stuck_after=1800)
        self.assertTrue(gone["maybe_stuck"])
        self.assertNotIn("command", gone)
        # The task runner stopped writing its record: no one vouches for the command.
        stale = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - progress.COMMAND_FRESH - 60))
        self.assertTrue(progress.progress(running(health_checked_at=stale, **held), now=NOW, stuck_after=1800)["maybe_stuck"])
        fresh = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 30))
        self.assertFalse(progress.progress(running(health_checked_at=fresh, process_alive=True, **held), now=NOW,
                                           stuck_after=1800)["maybe_stuck"])
        for fields in ({"status": "queued"}, {"status": "cancel_requested"}):
            self.assertNotIn("command", progress.progress(running(**held, **fields), now=NOW))
        self.assertNotIn("command", progress.progress(running(**held), now=NOW, waiting=True))

    def test_every_reader_says_the_same(self):
        from covey.app.butler import progress_words
        from covey.cards.overview import running_kind
        from covey.cards.streaming import progress_lines
        from covey.core import look
        record = running(last_activity_at=NOW - 2400, command_since=NOW - 2400)
        found = progress.progress(record, now=NOW)
        self.assertEqual(progress_words(record, [], NOW), ["在等一条命令跑完 · 已 40 分钟"])
        row = {"progress": progress.board_words(found, NOW, clock=True)}
        self.assertEqual(running_kind(row), "running")  # The 看板 row and 手上的事 row: blue 在做, not 可能卡住.
        self.assertIn("在等一条命令跑完 · 已 40 分钟", progress_lines({**found, "at": NOW}))
        self.assertEqual(look.record_tone(record, stuck=found["maybe_stuck"]), "running")


class CommandMonitorTest(unittest.TestCase):
    monitor = MonitorTest.monitor

    def test_the_record_keeps_the_command_while_it_runs_and_clears_it_once(self):
        wall, records, updates, told = [NOW], [], [], []
        job, monitor = self.monitor(wall, records=records, updates=updates, stuck=told.append)
        job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "跑评测 · python3 eval.py",
                                       "started_at": NOW})
        for seconds in (0, 600, 1900, 2400):
            wall[0] = NOW + seconds
            monitor.last_ui = float("-inf")
            monitor.tick()
        self.assertEqual(told, [])  # No 看看 button: it never may be stuck.
        self.assertEqual(records[-1]["command_since"], int(NOW))
        self.assertIn("\n现在：执行命令 · 跑评测 · 在等一条命令跑完 · 已 40 分钟", updates[-1])
        self.assertNotIn("可能卡住", updates[-1])
        self.assertNotIn("较长时间没有新进展", updates[-1])
        job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "done", "detail": "跑评测 · python3 eval.py",
                                       "started_at": NOW, "ended_at": NOW + 2401})
        wall[0] = NOW + 2410
        monitor.last_ui = float("-inf")
        monitor.tick()
        monitor.tick()
        self.assertEqual([r.get("command_since", "absent") for r in records[-2:]], [None, "absent"])  # Written empty once.
        self.assertNotIn("在等一条命令跑完", updates[-1])

    def test_a_command_after_the_backend_exited_is_not_waited_for(self):
        wall, updates = [NOW], []
        job, monitor = self.monitor(wall, updates=updates)
        job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "跑评测 · python3 eval.py",
                                       "started_at": NOW})
        monitor.tick()
        job.backend_process = SimpleNamespace(poll=lambda: 1)
        wall[0] = NOW + 2400
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertNotIn("在等一条命令跑完", updates[-1])
        self.assertIn("可能卡住", updates[-1])

    def test_a_turn_without_a_command_keeps_the_thirty_minute_rule(self):
        wall, told = [NOW], []
        job, monitor = self.monitor(wall, stuck=told.append)
        job.tool_trace.accept("tool", {"id": "a", "name": "read", "status": "done", "detail": "app/runs.py",
                                       "started_at": NOW, "ended_at": NOW + 1})
        monitor.tick()
        wall[0] = NOW + 2400
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertEqual(told, [True])


class BackgroundTasksTest(unittest.TestCase):
    """The reply is out but background tasks keep the turn open: the status line says what it still waits for."""

    def trace(self, *, commands=(), agents=(), foreground=None):
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        for index, detail in enumerate(commands):
            trace.accept("tool", {"id": f"b{index}", "name": "background", "status": "running", "detail": detail,
                                  "started_at": NOW - 600})
        for index, name in enumerate(agents):
            trace.accept("agent_state", {"id": f"a{index}", "name": name, "status": "running", "started_at": NOW - 500,
                                         "parent_id": None, "background": True})
        if foreground:
            trace.accept("tool", {"id": "f", "name": foreground, "status": "running", "detail": "x", "started_at": NOW - 5})
        return trace

    def test_commands_and_agents_are_described_by_the_models_words_never_the_command(self):
        names = progress.background_tasks(self.trace(commands=["跑全量测试 · python3 -m unittest -q", "pytest -q"],
                                                     agents=["等 CI 结果"]))
        self.assertEqual(sorted(names), ["等 CI 结果", "跑全量测试", "跑测试"])  # No purpose: the kind of command.
        self.assertNotIn("unittest", " ".join(names))

    def test_a_foreground_step_or_a_sub_task_the_model_waits_for_means_no_reply_is_out(self):
        self.assertEqual(progress.background_tasks(self.trace(commands=["跑测试 · pytest"], foreground="bash")), [])
        trace = self.trace(commands=["跑测试 · pytest"])
        trace.accept("agent_state", {"id": "w", "name": "读日志", "status": "running", "started_at": NOW, "parent_id": None,
                                     "background": False})
        self.assertEqual(progress.background_tasks(trace), [])
        ended = self.trace(commands=["跑测试 · pytest"])
        ended.finish()
        self.assertEqual(progress.background_tasks(ended), [])

    def test_the_words_name_two_or_three_cut_short_then_count_the_rest(self):
        said = progress.background_words
        self.assertEqual(said([]), "")
        self.assertEqual(said(["", ""]), "还在等后台 2 个")
        self.assertEqual(said(["跑全量测试", "等 CI"]), "还在等后台 2 个：跑全量测试、等 CI")
        self.assertEqual(said(["A", "B", "C"]), "还在等后台 3 个：A、B、C")
        self.assertEqual(said(["A", "B", "C", "D", "E"]), "还在等后台：A、B、C 等 5 个")
        self.assertEqual(said(["A", "", "C"]), "还在等后台：A、C 等 3 个")

    def monitor_lines(self, trace, wall=NOW):
        from covey.core.status_line import status_text
        job, monitor = MonitorTest.monitor(self, [wall])
        job.tool_trace = trace
        facts, _ = monitor.facts(monitor.clock())
        return facts, status_text(facts)

    def test_the_status_line_says_it_in_grey_under_the_now_line_and_drops_it_when_they_end(self):
        trace = self.trace(commands=["跑全量测试 · python3 -m unittest"], agents=["等 CI 结果"])
        facts, text = self.monitor_lines(trace)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("在做 · 已运行"))  # The running colour; never orange for this.
        self.assertTrue(lines[1].startswith("现在："))
        self.assertEqual(lines[2], "还在等后台 2 个：等 CI 结果、跑全量测试")
        self.assertEqual(len(lines), 3)
        from covey.cards.task_status import status_markdown
        self.assertIn("<font color='grey'>还在等后台", status_markdown(text).replace('"', "'"))
        trace.accept("tool", {"id": "b0", "name": "background", "status": "done", "detail": "跑全量测试 · x",
                              "started_at": NOW - 600, "ended_at": NOW})
        trace.accept("agent_state", {"id": "a0", "name": "等 CI 结果", "status": "completed", "started_at": NOW - 500,
                                     "ended_at": NOW, "parent_id": None, "background": True})
        self.assertNotIn("还在等后台", self.monitor_lines(trace)[1])

    def test_descriptions_are_escaped_and_cut_for_the_card(self):
        trace = self.trace(agents=["**很长很长很长很长很长很长很长很长很长很长很长的描述** `x`"])
        text = self.monitor_lines(trace)[1].split("\n")[-1]
        self.assertTrue(text.startswith("还在等后台 1 个："), text)
        self.assertNotIn("**", text)
        self.assertLessEqual(len(text), 80)


class BoardTest(unittest.TestCase):
    def test_the_butler_snapshot_says_the_three_things(self):
        from covey.app.butler import progress_words
        self.assertEqual(progress_words(running(), [], NOW), ["最近动静 2 分钟前在跑测试", "预计还要约 25 分钟"])
        self.assertEqual(progress_words(running(), [], NOW + 1200)[1], "第 3 步已做 22 分钟，此前平均每步约 9 分钟")
        with mock.patch.dict(os.environ, {progress.STUCK_ENV: "1"}):
            self.assertEqual(progress_words(running(), [], NOW), ["可能卡住了（超过 1 分钟没有动静）"])
        self.assertEqual(progress_words(running(status="queued"), [], NOW), [])

    def test_the_status_card_and_the_overview_row_say_the_estimate_as_a_time_of_day(self):
        from covey.app import overview, status_cards
        state = Path(tempfile.mkdtemp())
        record = running(topic_id="t" * 32, job_id=3, user_text="做一下", timings={"backend_requested": time.time() - 1200},
                         last_activity_at=time.time() - 60)
        _, _, facts, _ = status_cards.lines(state, "oc_chat", {"topic": "t" * 32, "group": "running", "record": record},
                                            time.time())
        self.assertRegex(facts, r"预计(明天)? ?\d\d:\d\d 左右做完")  # 预计明天 00:10 near midnight (failed at 23:42 before)
        row = overview.Overview.__new__(overview.Overview)
        row.history, row.now, row.topics, row.titles = {}, time.time(), {}, {}
        self.assertTrue(row.running_row(state, record)["progress"][0].endswith("左右做完"))


if __name__ == "__main__":
    unittest.main()
