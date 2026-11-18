"""A running turn's progress (core/progress.py, docs/usage.md 任务卡上的进度): the one pure function every reader shares, the
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


def running(**fields):
    return {"status": "running", "phase": "修进度", "steps": STEPS, "timings": {"backend_requested": NOW - 1200},
            "last_activity_at": NOW - 120, "last_activity": "在跑测试", **fields}


class ProgressTest(unittest.TestCase):
    def test_the_seven_fields_from_the_record(self):
        found = progress.progress(running(), now=NOW, stuck_after=1800)
        self.assertEqual(sorted(found), sorted(progress.FIELDS))
        self.assertEqual((found["phase"], found["plan_done"], found["plan_total"]), ("修进度", 2, 5))
        self.assertEqual((found["last_activity_at"], found["last_activity"]), (int(NOW - 120), "在跑测试"))
        self.assertEqual(found["eta_seconds"], 1800)  # 1200 s for 2 steps, 3 left.
        self.assertFalse(found["maybe_stuck"])

    def test_the_estimate_is_put_towards_the_topics_usual_turn(self):
        found = progress.progress(running(), now=NOW, history=[600, 3000, 2400], stuck_after=1800)
        self.assertEqual(found["eta_seconds"], 1500)  # Halfway between 1800 and the median's 2400 - 1200.
        # A turn already longer than the usual one: the steps alone.
        self.assertEqual(progress.progress(running(), now=NOW, history=[300, 400, 500])["eta_seconds"], 1800)

    def test_no_estimate_rather_than_a_wrong_one(self):
        cases = {"no steps": running(steps=[]), "none done": running(steps=[{"title": "a", "state": "doing"}]),
                 "all done": running(steps=[{"title": "a", "state": "done"}]),
                 "its first minute": running(timings={"backend_requested": NOW - 30}),
                 "queued": running(status="queued"),
                 "one step done and no history": running(steps=[{"title": "a", "state": "done"}, {"title": "b", "state": "doing"}])}
        for name, record in cases.items():
            with self.subTest(name):
                self.assertIsNone(progress.progress(record, now=NOW)["eta_seconds"])
        one = running(steps=[{"title": "a", "state": "done"}, {"title": "b", "state": "doing"}])
        self.assertIsNotNone(progress.progress(one, now=NOW, history=[900, 1300, 2000])["eta_seconds"])

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

    def test_the_status_line_says_the_estimate_and_the_record_keeps_the_last_activity(self):
        wall, records, updates = [NOW], [], []
        job, monitor = self.monitor(wall, records=records, updates=updates)
        job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "跑单测 · pytest -q",
                                       "started_at": NOW})
        monitor.tick()
        wall[0] = NOW + 150
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn(" · 预计还要约 ", updates[-1])
        self.assertIn(" · 2 分钟前在跑测试", updates[-1])
        self.assertNotIn("pytest", updates[-1].split("当前：")[0])
        self.assertEqual((records[-1]["last_activity"], records[-1]["last_activity_at"]), ("在跑测试", int(NOW)))
        self.assertEqual(job.turn_progress["plan_total"], 5)

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
        line = updates[-1].split("当前：")[1].split("\n")[0]
        self.assertEqual(line.count("前台睡眠 300 秒"), 1, line)
        self.assertTrue(line.endswith(" · 2 分钟前"), line)

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


class BoardTest(unittest.TestCase):
    def test_the_butler_snapshot_says_the_three_things(self):
        from covey.app.butler import progress_words
        self.assertEqual(progress_words(running(), [], NOW), ["最近动静 2 分钟前在跑测试", "预计还要约 30 分钟"])
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
        self.assertIn("预计 ", facts)
        self.assertIn(" 左右做完", facts)
        row = overview.Overview.__new__(overview.Overview)
        row.history, row.now, row.topics, row.titles = {}, time.time(), {}, {}
        self.assertTrue(row.running_row(state, record)["progress"][0].endswith("左右做完"))


if __name__ == "__main__":
    unittest.main()
