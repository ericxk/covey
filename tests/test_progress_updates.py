"""Shared public progress aggregation and refresh behavior; no live bridge I/O."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import json
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import make_job
from support.records import saved, state_dir
from support.cards import trace_panel
from support.world import build_claude_app
from support import acme as acme_gateway
from covey.cards.trace_view import CHILD_NO_EVENTS, CHILD_STEPS_BELOW, TraceView


class PublicProgressUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.app = build_claude_app()
        self.app.cards.queue_trace = mock.Mock()
        self.ctx = SimpleNamespace(topic_id="topic", state_dir=Path("unused-test-state"))
        self.job = make_job(progress=SimpleNamespace(card_id="card_test"))

    def emit(self, kind, data):
        self.app.interaction.structured_harness_event(self.ctx, self.job, kind, data, None)

    def test_duplicate_tools_and_replayed_starts_do_not_schedule_refreshes(self):
        start = {"id": "tool", "name": "read", "status": "running", "started_at": 10,
                 "detail": "src/example.py"}
        self.emit("tool", start)
        self.emit("tool", copy.deepcopy(start))
        self.assertEqual(self.app.cards.queue_trace.call_count, 1)
        done = {**start, "status": "done", "ended_at": 11}
        self.emit("tool", done)
        self.emit("tool", copy.deepcopy(done))
        self.emit("tool", start)
        self.assertEqual(self.app.cards.queue_trace.call_count, 2)
        self.assertEqual(self.job.tool_trace.steps["tool"]["status"], "done")
        self.assertFalse(self.app.cards.queue_trace.call_args.args[2])

    def test_stable_note_updates_replace_content_and_duplicates_do_not_refresh(self):
        note = {"id": "current-action", "text": "正在检查文件", "received_at": 10, "urgent": False}
        self.emit("progress_note", note)
        self.app.cards.queue_trace.assert_called_once_with(self.ctx, self.job, False)
        self.emit("progress_note", {**note, "received_at": 11})
        self.assertEqual(self.app.cards.queue_trace.call_count, 1)
        self.emit("progress_note", {**note, "text": "正在核对结果", "received_at": 12})
        self.assertEqual(self.app.cards.queue_trace.call_count, 2)
        self.assertEqual(self.job.tool_trace.total, 1)
        self.assertEqual(self.job.tool_trace.steps["current-action"]["detail"], "正在核对结果")

    def test_acme_waits_keep_their_count_in_the_trace_and_only_the_main_tasks_take_the_status_line(self):
        from covey.backends.gateway.catalog import retry_notice
        from covey.backends.gateway.relay import Busy
        notify = retry_notice(SimpleNamespace(emit=self.emit), acme_gateway())
        notify(Busy("retrying", 7, 20, 1, 7, 20))
        self.assertEqual(set(self.job.backend_retry), {"attempt", "reason", "since"})
        self.assertEqual(self.job.backend_retry["attempt"], 7)
        # A sub-task's wait and its work leave the main task's retry on the status line.
        notify(Busy("retrying", 2, 16, 1, 2, 16, "sub"))
        self.emit("tool", {"id": "t", "name": "bash", "status": "running", "started_at": 10, "child_id": "sub"})
        self.assertEqual(self.job.backend_retry["attempt"], 7)
        self.assertEqual(self.job.tool_trace.children["sub"].steps["acme-busy-1"]["detail"],
                         "Acme 繁忙（429），正在第 2 次重试，已持续 16 秒")
        self.emit("agent_state", {"id": "sub", "name": "核对", "status": "running", "started_at": 9})
        self.assertRegex(json.dumps(trace_panel(self.job.tool_trace, True), ensure_ascii=False),
                         r'"content": "子任务 · 核对 · \d\d:\d\d · Acme 繁忙（429），正在第 2 次重试')
        notify(Busy("recovered", 30, 80, 1, 30, 80))
        self.assertEqual(self.job.backend_retry, {})
        self.assertFalse(self.app.cards.queue_trace.call_args.args[2])  # The count alone is not urgent.
        self.assertEqual(self.job.tool_trace.steps["acme-busy-1"]["detail"],
                         "Acme 繁忙（429），重试 30 次后恢复，持续 1 分 20 秒")
        notify(Busy("retrying", 3, 15, 2, 33, 95))
        self.emit("tool", {"id": "m", "name": "bash", "status": "running", "started_at": 11})
        self.assertEqual(self.job.backend_retry, {})  # The main task's own work ends its retry too.
        # Each run the card heard of is a line of its own, set apart from the operations.
        rendered = json.dumps(trace_panel(self.job.tool_trace, True), ensure_ascii=False)
        self.assertRegex(rendered, r'"content": "\d\d:\d\d · Acme 繁忙（429），重试 30 次后恢复，持续 1 分 20 秒"')
        self.assertIn("· Acme 繁忙（429），正在第 3 次重试，已持续 15 秒；本轮共繁忙 2 次，重试 33 次，累计 1 分 35 秒", rendered)

    def test_existing_note_priority_and_explicit_urgent_notes_remain_immediate(self):
        self.emit("progress_note", {"id": "codex-note", "text": "Public commentary"})
        self.app.cards.queue_trace.assert_called_with(self.ctx, self.job, True)
        self.emit("progress_note", {"id": "blocked", "text": "需要用户确认", "urgent": True})
        self.app.cards.queue_trace.assert_called_with(self.ctx, self.job, True)
        self.emit("tool", {"id": "failure", "name": "bash", "status": "failed", "started_at": 10,
                           "ended_at": 11, "exit_code": 1, "detail": "验证命令"})
        self.app.cards.queue_trace.assert_called_with(self.ctx, self.job, True)

    def test_identical_agent_states_and_late_running_cannot_refresh_or_reopen(self):
        start = {"id": "child", "name": "子任务", "status": "running", "started_at": 10}
        self.emit("agent_state", start)
        self.emit("agent_state", copy.deepcopy(start))
        self.assertEqual(self.app.cards.queue_trace.call_count, 1)
        done = {**start, "status": "completed", "ended_at": 12}
        self.emit("agent_state", done)
        saved = self.job.tool_trace.snapshot()
        self.emit("agent_state", copy.deepcopy(done))
        self.emit("agent_state", start)
        self.assertEqual(self.app.cards.queue_trace.call_count, 2)
        self.assertEqual(self.job.tool_trace.snapshot(), saved)
        self.assertEqual(self.job.agents["child"]["status"], "completed")

    def test_nested_agent_state_deduplication_keeps_parent_links(self):
        parent = {"id": "parent", "name": "父子任务", "status": "running", "started_at": 10}
        child = {"id": "child", "name": "下级子任务", "status": "running", "started_at": 11,
                 "parent_id": "parent"}
        self.emit("agent_state", parent)
        self.emit("agent_state", child)
        self.emit("agent_state", copy.deepcopy(child))
        self.assertEqual(self.app.cards.queue_trace.call_count, 2)
        trace = self.job.tool_trace
        self.assertNotIn("child", trace.agents)
        self.assertIn("child", trace.children["parent"].agents)
        self.assertIs(trace.children["parent"].children["child"], trace.children["child"])
        self.emit("tool", {"id": "child-tool", "name": "read", "detail": "src/child.py", "status": "done",
                           "started_at": 12, "ended_at": 13, "child_id": "child"})
        self.assertIn("child-tool", trace.children["parent"].children["child"].steps)

    def test_child_without_public_steps_retains_no_events_message(self):
        self.emit("agent_state", {"id": "child", "name": "子任务", "status": "running", "started_at": 10})
        panel = TraceView(self.job.tool_trace.snapshot()).agent_element("child")
        self.assertIn(CHILD_NO_EVENTS, str(panel))
        self.assertNotIn(CHILD_STEPS_BELOW, str(panel))

    def test_child_with_public_steps_points_to_existing_records(self):
        self.emit("agent_state", {"id": "child", "name": "子任务", "status": "running", "started_at": 10})
        self.emit("tool", {"id": "child-read", "name": "read", "detail": "src/example.py", "status": "done",
                           "started_at": 11, "ended_at": 12, "child_id": "child"})
        panel = TraceView(self.job.tool_trace.snapshot()).agent_element("child")
        self.assertIn(CHILD_STEPS_BELOW, str(panel))
        self.assertNotIn(CHILD_NO_EVENTS, str(panel))
        self.assertIn("src/example.py", str(trace_panel(self.job.tool_trace)))

    def test_public_progress_opt_in_roundtrips_without_changing_old_snapshots(self):
        from covey.core.trace import ToolTrace
        event = {"id": "tool", "name": "read", "status": "running", "started_at": 10, "detail": "src/example.py"}
        self.emit("tool", event)
        snapshot = self.job.tool_trace.snapshot()
        self.assertNotIn("public_progress", snapshot)
        self.emit("tool", {**event, "public_progress": True})
        self.assertEqual(self.app.cards.queue_trace.call_count, 2)
        restored = ToolTrace.restore(self.job.tool_trace.snapshot())
        self.assertTrue(restored.public_progress)
        # Every task's panel ends with its newest operations where they ran (2026-11-04 15:07): the opt-in adds
        # no summary of its own.
        panel = json.dumps(trace_panel(restored), ensure_ascii=False)
        self.assertNotIn("public_progress", panel)
        self.assertNotIn("最近操作", panel)
        self.assertIn("src/example.py", panel)
        self.emit("tool", {**event, "public_progress": True})
        self.assertEqual(self.app.cards.queue_trace.call_count, 2)

    def test_a_long_run_folds_all_but_its_last_three_and_what_still_runs_and_the_record_keeps_all(self):
        for index in range(5):
            self.emit("tool", {"id": f"done-{index}", "name": "bash", "status": "done", "started_at": index,
                               "ended_at": index + 1, "exit_code": 0, "detail": f"check-{index}", "public_progress": True})
            detail = f"current-{index}.py" if index != 4 else "current-4.py/" + "a" * 180 + "（后台运行）"
            self.emit("tool", {"id": f"active-{index}", "name": "read", "status": "running", "started_at": 10 + index,
                               "detail": detail, "public_progress": True})
        trace = self.job.tool_trace
        panel = trace_panel(trace)
        recent = panel["elements"][-1]["content"]
        for text in ("current-0.py", "current-3.py", "current-4.py", "后台运行"):  # Every running operation shows.
            self.assertIn(text, recent)
        self.assertNotIn("check-4", recent)  # Ended among the earlier ones: in the fold.
        fold = panel["elements"][0]
        self.assertEqual(fold["header"]["title"]["content"], "<font color='grey'>收起了更早的 5 项操作：执行命令 5</font>")
        self.assertIn("check-4", json.dumps(fold, ensure_ascii=False))
        self.assertLess(len(recent), 1500)
        self.assertEqual(len(trace.steps), 10)
        original_detail = trace.steps["active-4"]["detail"]
        trace.finish()
        finished = json.dumps(trace_panel(trace, False), ensure_ascii=False)
        self.assertNotIn("最后几项操作", finished)
        self.assertIn("结束状态未确认", finished)
        self.assertEqual(len(trace.steps), 10)  # The record keeps every operation.
        self.assertEqual(trace.steps["active-4"]["detail"], original_detail)

    def test_an_operation_s_detail_drops_its_repeated_title_without_rewriting_history(self):
        events = [
            {"id": "bash", "name": "bash", "status": "done", "started_at": 1, "ended_at": 2,
             "exit_code": 0, "detail": "执行命令：Run tests."},
            {"id": "read", "name": "read", "status": "running", "started_at": 3,
             "detail": "读取文件：docs/sample.md"},
            {"id": "unknown", "name": "tool", "status": "running", "started_at": 4, "detail": "tool"},
        ]
        for event in events:
            self.emit("tool", {**event, "public_progress": True})
        recent = trace_panel(self.job.tool_trace)["elements"][-1]["content"]
        self.assertIn("Run tests.", recent)
        self.assertIn("docs/sample.md", recent)
        self.assertIn("**调用工具**", recent)
        self.assertNotIn("退出码 0", recent)  # ✓ says it (2026-11-04 17:38).
        self.assertNotIn("执行命令：", recent)
        self.assertNotIn("读取文件：", recent)
        self.assertNotIn("tool", recent)
        self.assertEqual(self.job.tool_trace.steps["bash"]["detail"], "执行命令：Run tests.")
        self.assertEqual(self.job.tool_trace.steps["read"]["detail"], "读取文件：docs/sample.md")

    def test_real_trace_queue_coalesces_pending_updates_and_survives_card_failure(self):
        del self.app.cards.queue_trace
        self.app.runtime.log_sink = mock.Mock()
        entered, release = threading.Event(), threading.Event()
        panels = []

        def update(ctx, job, element):
            panels.append(element)
            if len(panels) == 1:
                entered.set()
                release.wait(3)
                raise OSError("Synthetic card update failure")
            return True

        self.app.cards.set_task_element = mock.Mock(side_effect=update)
        self.ctx.state_dir = state_dir(self)  # The trace is saved and the record written as in the Bridge.
        try:
            self.emit("progress_note", {"id": "current-action", "text": "开始检查", "urgent": True})
            self.assertTrue(entered.wait(2))
            for index in range(20):
                self.emit("progress_note", {"id": "current-action", "text": f"Public phase {index}", "urgent": False})
            self.emit("tool", {"id": "check", "name": "bash", "detail": "验证命令", "status": "failed",
                               "started_at": 10, "ended_at": 11, "exit_code": 1})
            self.assertEqual(len(panels), 1)
        finally:
            release.set()
            self.assertTrue(self.app.cards.trace_updates(self.job).close(3))
            self.assertTrue(self.job.trace_card_updates.close(3))
        self.assertEqual(len(panels), 2)
        self.assertIn("Public phase 19", str(panels[-1]))
        self.assertIn("退出码 1", str(panels[-1]))
        self.assertEqual(self.job.tool_trace.total, 2)
        self.assertEqual(saved(self.ctx.state_dir, self.job.message_id)["tool_step_count"], 2)
        self.app.runtime.log_sink.assert_called_once()

    def test_finalized_answer_does_not_accept_late_answer_updates(self):
        self.job.answer_finalized = True
        self.app.cards.trace_updates = mock.Mock()
        self.emit("answer_snapshot", {"text": "late answer"})
        self.app.cards.trace_updates.assert_not_called()
        self.emit("progress_note", {"id": "public-result", "text": "工具检查已结束", "urgent": False})
        self.app.cards.queue_trace.assert_called_once_with(self.ctx, self.job, False)


if __name__ == "__main__":
    unittest.main()
