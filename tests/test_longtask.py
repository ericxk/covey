from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import make_job
from covey.core.checkpoint import audit_recovery
from covey.core.longtask import SEED_PHASE, TaskMonitor
from covey.core.status_line import (Facts, status_summary, status_text, BACKEND_ALIVE, COLLECTING, STARTING, THINKING,
                                             WAITING_FOR_MODEL)


class LongTaskTest(unittest.TestCase):
    def test_eight_hour_clock_changes_cadence_and_never_invents_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0.0]
            updates = []
            records = []
            closed = []
            job = make_job(backend_process=SimpleNamespace(poll=lambda: None))
            monitor = TaskMonitor(
                job,
                Path(directory) / "checkpoint.json",
                updates.append,
                lambda **r: records.append(r),
                lambda *a: True,
                lambda _: None,
                lambda: closed.append(True),
                clock=lambda: now[0],
            )
            for seconds in [0, 2, 4, 180, 195, 210, 240, 8 * 3600]:
                now[0] = seconds
                monitor.tick()
            self.assertEqual(len(closed), 1)
            self.assertEqual(len(updates), 6)
            self.assertIn("8 小时 0 分钟", updates[-1])
            # Eight hours without any progress: it may be stuck (core/progress.py), the card's 看看 under it.
            self.assertIn("超过 30 分钟没有动静，可能卡住了", updates[-1])
            self.assertIn(BACKEND_ALIVE, updates[-1])
            self.assertNotIn("%", updates[-1])
            self.assertTrue(records[-1]["process_alive"])

    def test_checkpoint_question_uses_card_without_notice_and_heartbeat_is_not_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cp.json"
            now = [0]
            notices = []
            records = []
            cp = {
                "phase": "等待训练",
                "external_tasks": ["train-123"],
                "requires_user": True,
                "question": "选择哪个数据集？",
            }
            path.write_text(json.dumps(cp))
            monitor = TaskMonitor(
                make_job(),
                path,
                lambda _: True,
                lambda **r: records.append(r),
                lambda text, key: notices.append(text) or True,
                lambda _: None,
                lambda: None,
                clock=lambda: now[0],
            )
            monitor.tick()
            now[0] = 100
            path.write_text(json.dumps({**cp, "updated_at": "new-time"}))
            monitor.tick()
            self.assertEqual(len(notices), 0)
            self.assertEqual(records[-1]["last_progress_age_seconds"], 100)
            self.assertIsNone(records[-1]["process_alive"])

    def test_recovery_audits_artifacts_without_relaunching_external_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "done.bin").write_bytes(b"output")
            report = audit_recovery(
                {
                    "checkpoint": {
                        "artifacts": ["done.bin", "missing.bin", "/etc/passwd", "~$预算表.docx"],
                        "external_tasks": ["train-123"],
                    }
                },
                root,
            )
            self.assertTrue(report["artifacts"][0]["exists"])
            self.assertFalse(report["artifacts"][1]["exists"])
            self.assertEqual(report["artifacts"][2]["state"], "outside_workspace_not_inspected")
            # A "~name" of no user (T480, 2026-11-11: pathlib raised RuntimeError) is no file, not a failed recovery.
            self.assertEqual(report["artifacts"][3], {"path": "~$预算表.docx", "exists": False})
            self.assertEqual(report["external_tasks_require_verification"], ["train-123"])


class RunningStatusTest(unittest.TestCase):
    def monitor(self, job, updates):
        return TaskMonitor(job, Path(tempfile.mkdtemp()) / "cp.json", updates.append, lambda **r: None,
                           lambda *a: True, lambda _: None, lambda: None, clock=lambda: 125.0)

    def test_the_card_preview_hears_of_a_new_phase_and_of_a_question_waiting_for_its_answer(self):
        # T314 2026-11-02: the topic list showed a running topic as it showed a stopped one (its card's title).
        states = []
        job = make_job(backend_process=SimpleNamespace(poll=lambda: None))
        monitor = TaskMonitor(job, Path(tempfile.mkdtemp()) / "cp.json", lambda _: None, lambda **r: None,
                              lambda *a: True, lambda _: None, lambda: None, clock=lambda: 125.0,
                              state=lambda phase, asking: states.append((phase, asking)))
        monitor.tick()
        monitor.checkpoint.write_text(json.dumps({"phase": SEED_PHASE, "updated_at": "1"}))
        monitor.tick()  # The Bridge's own phase is no phase: not told again.
        monitor.checkpoint.write_text(json.dumps({"phase": "改代码", "updated_at": "2"}))
        monitor.tick()
        monitor.checkpoint.write_text(json.dumps({"phase": "改代码", "requires_user": True, "question": "选哪个？",
                                                  "updated_at": "3"}))
        monitor.tick()  # A checkpoint question: the turn is ending, the task still runs.
        job.native_question_active = True
        monitor.tick()
        self.assertEqual(states, [("", False), ("改代码", False), ("改代码", True)])

    def test_once_stopped_the_monitor_leaves_the_status_line_to_the_outcome(self):
        updates = []
        monitor = self.monitor(make_job(backend_process=None), updates)
        monitor.start()
        monitor.stop()
        written = len(updates)  # Its own first tick, if the thread got to it; stopping writes nothing.
        monitor.tick()  # A late progress event or retry note on another thread.
        self.assertEqual(len(updates), written)
        self.assertNotIn("", updates)

    def test_status_shows_the_current_step_and_last_progress_not_process_liveness(self):
        import time
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "运行全量测试 · python3 -m unittest",
                              "started_at": time.time() - 130})
        job = make_job(tool_trace=trace, backend_process=SimpleNamespace(poll=lambda: None))
        updates = []
        monitor = self.monitor(job, updates)
        monitor.started = 0.0
        monitor.tick()
        self.assertIn("已运行 2分05秒", updates[-1])
        self.assertIn("现在：", updates[-1])
        self.assertIn("运行全量测试", updates[-1])
        # A command still running is what the turn waits for, in minutes: never the command line itself.
        self.assertIn(" · 在等一条命令跑完 · 已 2 分钟", updates[-1])
        self.assertNotIn(BACKEND_ALIVE, updates[-1])
        job.backend_process = SimpleNamespace(poll=lambda: 1)
        monitor.last_ui = float("-inf")  # The card refreshes at most every 2 s.
        monitor.tick()
        self.assertIn("后端进程已退出", updates[-1])

    def test_a_backend_that_exited_normally_reads_as_collecting_the_result(self):
        # S27 #843 (MASO): "现在：等待模型开始输出 · 最近进展 13 秒前 / 后端进程已退出，正在收尾。" just before ✅ 已完成.
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("first_token", {"ttft_seconds": 1.68, "ttft_source": "maso_native"})
        job = make_job(tool_trace=trace, backend_process=SimpleNamespace(poll=lambda: 0))
        updates = []
        self.monitor(job, updates).tick()
        self.assertTrue(updates[-1].startswith("在做 · 已运行"))
        self.assertTrue(updates[-1].endswith("\n现在：" + COLLECTING))
        self.assertNotIn("已退出", updates[-1])
        self.assertNotIn("等待模型开始输出", updates[-1])

    def test_the_phase_a_turn_starts_with_is_not_shown_beside_the_current_step(self):
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "sleep 90", "started_at": 100.0})
        job = make_job(tool_trace=trace, backend_process=SimpleNamespace(poll=lambda: None))
        path = Path(tempfile.mkdtemp()) / "cp.json"
        path.write_text(json.dumps({"phase": SEED_PHASE}, ensure_ascii=False))
        updates = []
        monitor = TaskMonitor(job, path, updates.append, lambda **r: None, lambda *a: True, lambda _: None,
                              lambda: None, clock=lambda: 125.0)
        monitor.started = 0.0
        monitor.tick()
        self.assertIn("现在：", updates[-1])
        self.assertNotIn("阶段", updates[-1])  # It said 阶段：准备处理本轮请求 beside 现在：执行命令 (S25).
        path.write_text(json.dumps({"phase": "跑测试"}, ensure_ascii=False))
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("阶段：跑测试", updates[-1])

    def test_status_without_steps_says_the_model_has_not_started(self):
        from covey.core.trace import ToolTrace
        job = make_job(tool_trace=ToolTrace())
        updates = []
        self.monitor(job, updates).tick()
        self.assertIn("现在：已提交给模型，等待首个输出", updates[-1])
        self.assertNotIn("等待模型开始输出", updates[-1])  # It read as if nothing were happening (S28 #850).

    def test_a_reasoning_start_says_the_model_is_thinking(self):
        # S28 #850 (Codex · GPT-6.1-Sol, max) reasoned for 29 s without streaming any text.
        from covey.app.interaction import Interaction
        from covey.core.trace import ToolTrace
        job = make_job(tool_trace=ToolTrace(), backend_process=SimpleNamespace(poll=lambda: None))
        updates, wall = [], [1000.0]
        monitor = TaskMonitor(job, Path(tempfile.mkdtemp()) / "cp.json", updates.append, lambda **r: None,
                              lambda *a: True, lambda _: None, lambda: None, clock=lambda: 125.0, wall=lambda: wall[0])
        job.activity_monitor = monitor
        Interaction._dispatch_structured_event(SimpleNamespace(), SimpleNamespace(), job, "model_reasoning", {}, None)
        wall[0] += 80
        monitor.tick()
        self.assertIn("现在：模型思考中 · 1 分钟前在思考", updates[-1])
        self.assertEqual(job.tool_trace.steps, {})  # Nothing for 执行过程 to show.
        # Codex's TTFT can be that reasoning start (S30): still only thinking.
        self.assertTrue(job.tool_trace.accept("first_token", {"ttft_seconds": 25.0, "ttft_source": "codex_item"}))
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("现在：模型思考中", updates[-1])
        monitor.output()  # Then streamed output: no longer only thinking.
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertTrue(updates[-1].endswith("现在：" + THINKING), updates[-1])  # Progress under 10 s has no age.

    def test_output_without_steps_says_the_model_is_at_work(self):
        # MASO reports a first token and token usage but no steps; Claude or Codex answering without a tool
        # streams answer text. Each means the model has started.
        from covey.core.trace import ToolTrace
        for event, data in (("first_token", {"ttft_seconds": 1.68, "ttft_source": "maso_native"}),
                            ("token_usage", {"output_tokens": 41}), (None, None)):
            with self.subTest(event):
                trace = ToolTrace()
                job = make_job(tool_trace=trace, backend_process=SimpleNamespace(poll=lambda: None))
                updates = []
                monitor = self.monitor(job, updates)
                if event:
                    self.assertTrue(trace.accept(event, data))
                else:
                    monitor.output()
                monitor.tick()
                self.assertIn("现在：" + THINKING, updates[-1])

    def test_a_backend_still_setting_up_says_so_until_its_model_request(self):
        # S29 #863 (MASO): "已提交给模型" for about 8 s before the SDK made its first model call.
        from covey.core.trace import ToolTrace
        job = make_job(tool_trace=ToolTrace(), backend_process=SimpleNamespace(poll=lambda: None))
        updates, wall = [], [1000.0]
        monitor = TaskMonitor(job, Path(tempfile.mkdtemp()) / "cp.json", updates.append, lambda **r: None,
                              lambda *a: True, lambda _: None, lambda: None, clock=lambda: 125.0,
                              wall=lambda: wall[0], starting=STARTING.format("MASO"))
        monitor.tick()
        self.assertIn("\n现在：正在启动 MASO…", updates[-1])
        monitor.request()
        wall[0] += 3
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertTrue(updates[-1].endswith("\n现在：" + WAITING_FOR_MODEL), updates[-1])
        wall[0] += 9
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertTrue(updates[-1].endswith("\n现在：" + WAITING_FOR_MODEL), updates[-1])  # Under a minute: no age.
        wall[0] += 60
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("\n现在：" + WAITING_FOR_MODEL + " · 1 分钟前在等模型回复", updates[-1])
        monitor.output()  # Output after it as before.
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("\n现在：" + THINKING, updates[-1])

    def test_streamed_output_is_not_held_back_by_a_missing_model_request(self):
        from covey.core.trace import ToolTrace
        job = make_job(tool_trace=ToolTrace(), backend_process=SimpleNamespace(poll=lambda: None))
        updates = []
        monitor = TaskMonitor(job, Path(tempfile.mkdtemp()) / "cp.json", updates.append, lambda **r: None,
                              lambda *a: True, lambda _: None, lambda: None, clock=lambda: 125.0,
                              starting=STARTING.format("MASO"))
        monitor.output()
        monitor.tick()
        self.assertIn("\n现在：" + THINKING, updates[-1])

    def test_the_backend_s_model_request_reaches_only_the_status_line(self):
        from unittest import mock
        from support import make_context
        from support.fake_backends import FakeBackend
        from support.world import build_claude_app
        from covey.core.harness import RunResult
        from covey.core.jobs import BridgeReplyJob

        class Backend(FakeBackend):
            def run(self, request):
                request.callbacks.event("model_request", {"id": "root", "received_at": 1.0}, None)
                return RunResult("ok", None)

        app = build_claude_app(Backend())
        job = BridgeReplyJob(job_id=1, user_text="x", chat_id="oc", message_id="om1", timeout=0, history_snapshot=[])
        job.activity_monitor = mock.Mock()
        with mock.patch.object(app.interaction, "structured_harness_event") as interaction:
            app.runs.generate_reply_for_worker(make_context(Path(tempfile.mkdtemp())), job, None, None)
        job.activity_monitor.request.assert_called_once_with()
        interaction.assert_not_called()


class SubtaskProgressTest(unittest.TestCase):
    """Sub-task steps are progress on the main card; a live process alone is not."""

    T0 = 1_800_000_000.0

    def monitor(self, trace, wall, records=None, alive=True):
        job = make_job(tool_trace=trace, backend_process=SimpleNamespace(poll=lambda: None if alive else 1))
        updates = []
        monitor = TaskMonitor(job, Path(tempfile.mkdtemp()) / "cp.json", updates.append,
                              lambda **r: records is not None and records.append(r), lambda *a: True,
                              lambda _: None, lambda: None, clock=lambda: 0.0, wall=lambda: wall[0])
        return monitor, updates

    def subtask_trace(self):
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("tool", {"id": "a1", "name": "agent", "detail": "Agent", "status": "running", "started_at": self.T0})
        trace.accept("agent_state", {"id": "a1", "name": "S5 进展修复", "status": "running", "started_at": self.T0,
                                     "parent_id": None, "background": False})
        return trace

    def child_tool(self, trace, key, detail, at, status="running"):
        trace.accept("tool", {"id": key, "name": "bash", "detail": detail, "status": status, "child_id": "a1",
                              "started_at": at, "ended_at": None if status == "running" else at})

    def test_child_tool_start_and_finish_move_the_clock_and_name_the_action(self):
        trace = self.subtask_trace()
        command = "运行问题卡测试 · cd /repo && python3 -B -m unittest discover -s tests -p 'test_questions.py'"
        self.child_tool(trace, "c1", command, self.T0 + 7080)
        wall, records = [self.T0 + 7200], []
        monitor, updates = self.monitor(trace, wall, records)
        monitor.tick()
        # T182 #1128 wrapped the sub-task's name, its time, the age and its last step over three lines:
        # now its latest step and the age only.
        self.assertTrue(updates[-1].endswith("\n现在：子任务正在运行测试：运行问题卡测试 · 在等一条命令跑完 · 已 2 分钟"), updates[-1])
        self.assertNotRegex(updates[-1], r"\d+ 小时")
        self.assertNotIn("unittest", updates[-1])
        self.assertNotIn("较长时间没有新进展", updates[-1])
        self.assertEqual(records[-1]["last_progress_age_seconds"], 120)
        self.child_tool(trace, "c1", command, self.T0 + 7190, status="done")
        wall[0], monitor.last_ui = self.T0 + 7200, float("-inf")
        monitor.tick()
        self.assertTrue(updates[-1].endswith("现在：子任务运行测试结束：运行问题卡测试"), updates[-1])  # 10 s: no age.
        self.child_tool(trace, "c2", "git commit -qm 'fix: progress'", self.T0 + 7195)
        trace.accept("progress_note", {"id": "n1", "text": "Tests pass; committing.", "child_id": "a1",
                                       "received_at": self.T0 + 7194})
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("现在：子任务正在提交代码", updates[-1])
        self.assertNotIn("最近进展", updates[-1])
        self.assertNotIn("fix: progress", updates[-1])

    def test_a_running_sub_task_is_named_without_its_running_time(self):
        # Its time is on its own entry in 执行过程; the line keeps what runs now and the age only.
        trace = self.subtask_trace()
        wall = [self.T0 + 7200]
        monitor, updates = self.monitor(trace, wall)
        monitor.output()  # The newest progress is the main task's: the line names what runs now.
        monitor.tick()
        self.assertTrue(updates[-1].endswith("\n现在：子任务「S5 进展修复」运行中"), updates[-1])

    def test_a_child_only_active_hour_never_reads_as_an_hour_without_progress(self):
        trace = self.subtask_trace()
        wall = [self.T0]
        monitor, updates = self.monitor(trace, wall)
        for minute in range(0, 95, 5):
            self.child_tool(trace, f"c{minute}", f"Step {minute} · ls", self.T0 + minute * 60)
            wall[0], monitor.last_ui = self.T0 + minute * 60 + 30, float("-inf")
            monitor.tick()
            self.assertIn("现在：子任务正在查看代码：Step", updates[-1])
            self.assertNotIn("分钟前", updates[-1])  # 30 s ago each time.
            self.assertNotIn("可能卡住了", updates[-1])
            self.assertNotIn("较长时间没有新进展", updates[-1])

    def test_a_live_process_without_progress_says_only_that_it_is_alive(self):
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("tool", {"id": "b", "name": "bash", "status": "running", "started_at": self.T0,
                              "detail": "运行全量测试 · python3 -m unittest"})
        wall, records = [self.T0 + 3700], []
        monitor, updates = self.monitor(trace, wall, records)
        monitor.tick()
        # The model's purpose without the command (2026-11-02 review); the kind still comes from the command.
        # The command still runs: the turn waits for it, however long (blue 在做, no 可能卡住, no 看看).
        self.assertIn("现在：执行命令 · 运行全量测试 · 在等一条命令跑完 · 已 1 小时 1 分钟", updates[-1])
        self.assertNotIn("可能卡住", updates[-1])
        self.assertNotIn("进程仍在运行", updates[-1])
        self.assertEqual(records[-1]["command_since"], int(self.T0))
        self.assertEqual(records[-1]["last_activity"], "在跑测试")
        self.assertEqual(records[-1]["last_activity_at"], int(self.T0))
        self.assertEqual(records[-1]["last_progress_age_seconds"], 3700)
        monitor.output()  # Streamed answer text is progress too.
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertNotIn("最近进展", updates[-1])  # Under 10 s.
        self.assertNotIn("进程仍在运行", updates[-1])
        exited, exited_updates = self.monitor(trace, wall, alive=False)  # An exited process is not "alive".
        exited.tick()
        self.assertNotIn("进程仍在运行", exited_updates[-1])
        self.assertIn("后端进程已退出", exited_updates[-1])

    def test_nested_and_legacy_subtask_records_still_describe_their_progress(self):
        from covey.core.trace import ToolTrace
        from covey.core.status_line import trace_progress
        # Records written before trace files embed tool_steps / child_steps / agents.
        trace = ToolTrace()
        trace.steps = {"n1": {"title": "进展说明", "detail": "先读取目录结构", "status": "note", "started": self.T0},
                       "agent-c1": {"title": "子任务", "detail": "核对配置", "status": "done", "started": self.T0 + 1,
                                    "agent_id": "c1"}}
        trace.agents = {"c1": {"name": "核对配置", "status": "completed", "started_at": self.T0 + 1,
                               "ended_at": self.T0 + 10, "background": False, "parent_id": None}}
        child = trace.children["c1"] = ToolTrace()
        child.steps = {"m1": {"title": "进展说明", "detail": "配置一致", "status": "note", "started": self.T0 + 2}}
        restored = ToolTrace.restore(trace.snapshot())
        for each in (trace, restored):
            self.assertEqual(trace_progress(each, self.T0 + 100),
                             (THINKING, self.T0 + 10, "子任务「核对配置」已完成"))
        nested = self.subtask_trace()
        nested.accept("tool", {"id": "a2", "name": "agent", "detail": "Agent", "status": "running", "child_id": "a1",
                               "started_at": self.T0 + 5})
        nested.accept("agent_state", {"id": "a2", "name": "读日志", "status": "running", "started_at": self.T0 + 5,
                                      "parent_id": "a1", "background": False})
        nested.accept("tool", {"id": "r1", "name": "read", "detail": "/repo/harness\\_bridge/core/longtask.py",
                               "status": "running", "child_id": "a2", "started_at": self.T0 + 9})
        self.assertEqual(trace_progress(nested, self.T0 + 20)[2], "子任务「读日志」正在查看代码 longtask.py")

    def test_claude_child_rows_reach_the_main_card(self):
        import time
        from covey.backends.claude.events import ClaudeEventAdapter
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        adapter = ClaudeEventAdapter(trace.accept)
        adapter.accept({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_a", "name": "Agent",
                        "input": {"description": "S5 进展修复", "prompt": "..."}}]}})
        adapter.accept({"type": "assistant", "parent_tool_use_id": "toolu_a", "message": {"content": [
            {"type": "tool_use", "id": "toolu_b", "name": "Bash",
             "input": {"command": "python3 -B -m unittest tests.test_questions", "description": "运行问题卡测试"}}]}})
        monitor, updates = self.monitor(trace, [time.time() + 3])
        monitor.tick()
        self.assertIn("现在：子任务正在运行测试：运行问题卡测试", updates[-1])
        adapter.accept({"type": "user", "parent_tool_use_id": "toolu_a", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_b", "content": "OK"}]}})
        monitor.last_ui = float("-inf")
        monitor.tick()
        self.assertIn("子任务运行测试结束：运行问题卡测试", updates[-1])


class StatusTextTest(unittest.TestCase):
    """The status line from what one tick saw (Facts), without a monitor, a clock or a checkpoint file."""

    def facts(self, **changes):
        seen = dict(elapsed=65, quiet=3, alive=True, finished=False, phase="处理本轮请求", last_event="尚无阶段进展",
                    checked="01-02 03:04:05", question="", waiting=False, native_question_active=False,
                    activity=None, traced=False, wall=1000.0, stirred=False, retry={}, epoch=1000.0,
                    long_mode=False, frame=0)
        return Facts(**{**seen, **changes})

    def test_a_short_task_without_a_trace_shows_its_last_event_or_an_ellipsis(self):
        self.assertEqual(status_text(self.facts()), "正在处理本轮消息 ·")
        self.assertEqual(status_text(self.facts(frame=4)), "正在处理本轮消息 · ·")
        self.assertEqual(status_text(self.facts(last_event="阶段记录已更新：读代码")), "阶段记录已更新：读代码")
        # Its record has the full summary all the same; a long task's line is that summary.
        plain = ("在做 · 已运行 1分05秒\n阶段：处理本轮请求\n最近进展：0 分钟前 · 尚无阶段进展\n"
                 "最近检查：01-02 03:04:05 · " + BACKEND_ALIVE)
        self.assertEqual(status_summary(self.facts()), plain)
        self.assertEqual(status_text(self.facts(long_mode=True)), plain)

    def test_a_retry_then_a_question_then_the_trace(self):
        traced = dict(traced=True, activity=(THINKING, None, None))
        self.assertEqual(status_text(self.facts(**traced)), "在做 · 已运行 1分05秒\n现在：" + THINKING)
        retry = {"attempt": 2, "next": 1_030_000, "reason": "429"}
        self.assertEqual(status_text(self.facts(retry=retry, **traced)),
                         "在做 · 已运行 1分05秒 · 模型请求重试第 2 次\n本次模型请求尚未成功，约 30 秒后再次尝试。\n"
                         "上游返回：429\n可使用停止按钮结束等待。")
        asked = dict(waiting=True, question="用哪个数据集？")
        self.assertEqual(status_text(self.facts(retry=retry, native_question_active=True, **asked, **traced)),
                         "等你\n要你定：用哪个数据集？")
        self.assertEqual(status_text(self.facts(retry=retry, **asked, **traced)),
                         "正在整理本轮结果，完成后将展示需要确认的内容。")

    def test_the_same_facts_give_the_same_line(self):
        facts = self.facts(traced=True, activity=("运行命令", 990.0, "子任务运行命令结束"), quiet=12, stirred=True)
        self.assertEqual(status_text(facts), status_text(facts))
        self.assertEqual(status_text(facts), "在做 · 已运行 1分05秒\n现在：子任务运行命令结束 · 最近进展 12 秒前")
