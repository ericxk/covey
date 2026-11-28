from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app.cards import STOPPING
from covey.app.task_card import StreamingReply
from covey.app.recovery_cards import recovery_card
from covey.cards.review import stopped_sentence
from covey.cards.task_status import status_markdown
from covey.cards.ui import state_pill
from covey.core.harness import RunResult
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.history_storage import append_history
from support.cards import walk_dicts
from support.fake_lark import FakeCardKit
from support.world import TopicFixture, acknowledging_lanes, wait_for
from support.final_delivery import FinalDeliveryHarness, make_reply_job


class TaskControlsTest(TopicFixture, unittest.TestCase):
    def test_cancel_one_queued_job_leaves_other_jobs_and_session_alone(self):
        self.receive("om_a", "one")
        self.receive("om_a2", "two", root="om_a")
        self.receive("om_b", "other topic")
        jobs = list(self.app.runtime.jobs.queue.queue)
        ctx = self.topic("om_a")
        self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(jobs[1].job_id)}, "oc_chat", "cancel")
        self.assertTrue(jobs[1].cancelled_before_start)
        self.assertFalse(jobs[0].cancel_event.is_set())
        self.assertFalse(jobs[2].cancel_event.is_set())
        self.assertNotIn(jobs[1].job_id, self.app.runtime.jobs.by_id)
        self.app.process_reply_job(jobs[1], ctx)
        record = next(r for r in self.app.harness.topic_records(ctx, "oc_chat") if r["job_id"] == jobs[1].job_id)
        self.assertEqual((record["status"], record.get("never_started")), ("cancelled", True))
        self.assertEqual(len(self.backend.calls), 0)

    def running_with_card(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        job.execution_started = True
        job.progress = StreamingReply("card-a", "om_card", sequence=1)
        return job, self.topic("om_a")

    def test_a_stop_ends_the_backend_itself_when_the_turn_does_not(self):
        # T525 #4664 (2026-11-18): the turn's thread waited minutes on a job record's lock (a stalled disk) and never
        # looked at the stop; Claude ran on. The stop SIGTERMs the backend's group once HARD_STOP_AFTER has passed.
        import signal
        import subprocess
        from covey.app import task_controls
        job, ctx = self.running_with_card()
        job.backend_process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: job.backend_process.poll() is None and job.backend_process.kill())
        with FakeCardKit(), mock.patch.object(task_controls, "HARD_STOP_AFTER", 0.05):
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
            self.assertEqual(job.backend_process.wait(timeout=10), -signal.SIGTERM)
        self.assertTrue(job.cancel_event.is_set())
        self.assertTrue(any("SIGTERM job=#" in line for line in self.logs), self.logs)

    def test_a_backend_that_ended_gets_no_signal(self):
        from covey.app import task_controls
        process = mock.Mock(**{"poll.return_value": 0})
        with mock.patch.object(task_controls, "signal_group") as signal_group:
            timer = task_controls.hard_stop_later(SimpleNamespace(job_id=1, backend_process=process), self.logs.append, 0.01)
            timer.join(5)
        signal_group.assert_not_called()
        self.assertIsNone(task_controls.hard_stop_later(SimpleNamespace(job_id=1, backend_process=None), self.logs.append))

    def test_stop_changes_the_top_of_the_card_in_its_first_write(self):
        job, ctx = self.running_with_card()
        with FakeCardKit() as lark:
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
        first = lark.calls[0]
        self.assertEqual((first.method, first.path), ("POST", "/open-apis/cardkit/v1/cards/card-a/batch_update"))
        status, buttons = json.loads(first.data["actions"])
        self.assertEqual(status["action"], "update_element")
        self.assertEqual(json.loads(status["params"]["element"])["content"], status_markdown(STOPPING))
        self.assertEqual(buttons, {"action": "delete_elements", "params": {"element_ids": ["action_row"]}})
        self.assertTrue(job.action_controls_removed)
        # Closing the controls afterwards does not delete the row again.
        self.assertFalse([c for c in lark.calls if c.method == "DELETE"])
        sequences = [c.data["sequence"] for c in lark.calls]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_a_card_without_the_action_row_still_shows_stopping_first(self):
        job, ctx = self.running_with_card()
        with FakeCardKit(side_effect=[None] + [{"code": 0}] * 20) as lark:  # Old card: the batch is refused.
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
        self.assertTrue(lark.calls[1].path.endswith("/elements/activity"))
        self.assertEqual(json.loads(lark.calls[1].data["partial_element"])["content"], status_markdown(STOPPING))
        self.assertTrue(lark.calls[2].path.endswith("/elements/action_row"))
        self.assertFalse([c for c in lark.calls if "📌 阶段" in json.dumps(c.data, ensure_ascii=False)])

    def test_a_stop_shown_at_the_top_leaves_the_body_alone(self):
        # S29 client run: "📌 阶段：正在停止" stayed in the body for about 1 s after "⏹ 已停止".
        job, ctx = self.running_with_card()
        with FakeCardKit() as lark:
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
        self.assertEqual(len(lark.calls), 1)  # The batch alone: status line and buttons.
        self.assertTrue(job.cancel_event.is_set())

    def test_a_card_without_the_status_line_says_stopping_in_its_body(self):
        job, ctx = self.running_with_card()
        with FakeCardKit(side_effect=[None, None] + [{"code": 0}] * 20) as lark:  # No batch, no status line.
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
        body = [c for c in lark.calls if "📌 阶段：正在停止" in json.dumps(c.data, ensure_ascii=False)]
        self.assertEqual(len(body), 1)
        self.assertTrue(job.action_controls_removed)

    def test_after_a_stop_click_the_monitor_only_clears_the_status_line(self):
        job, ctx = self.running_with_card()
        job.cancel_requested_at = "2026-10-26T12:00:00Z"
        with FakeCardKit() as lark:
            self.assertFalse(self.app.cards.update_card_activity(ctx, job, "执行中 · 已运行 1 分 36 秒"))
            self.assertEqual(lark.calls, [])
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, ""))
        self.assertEqual(len(lark.calls), 1)

    def test_the_first_status_line_takes_the_queued_text_from_the_body_in_the_same_write(self):
        # S34 client run: 执行中 came 2 s after the write that cleared the queued text.
        job, ctx = self.running_with_card()
        job.body_clear_pending = True
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, "执行中 · 已运行 0 秒"))
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, "执行中 · 已运行 2 秒"))
        first, second = lark.calls
        self.assertEqual(first.path, "/open-apis/cardkit/v1/cards/card-a/batch_update")
        status, body = (json.loads(a["params"]["element"]) for a in json.loads(first.data["actions"]))
        self.assertEqual((status["element_id"], status["content"]), ("activity", status_markdown("执行中 · 已运行 0 秒")))
        self.assertEqual((body["element_id"], body["content"]), ("answer", " "))
        self.assertTrue(second.path.endswith("/elements/activity"))
        self.assertEqual((job.body_clear_pending, job.progress.last_content), (False, " "))

    def test_a_refused_first_batch_clears_the_body_then_writes_the_status_line(self):
        job, ctx = self.running_with_card()
        job.body_clear_pending = True
        with FakeCardKit(side_effect=[None] + [{"code": 0}] * 5) as lark:
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, "执行中"))
        self.assertEqual([c.path.rsplit("/", 1)[-1] for c in lark.calls], ["batch_update", "answer", "activity"])

    def test_the_queued_text_is_never_cleared_over_an_answer(self):
        from covey.app.interaction import _show_answer_snapshot
        job, ctx = self.running_with_card()
        job.body_clear_pending = True
        with FakeCardKit() as lark:
            _show_answer_snapshot(self.app.cards, ctx, job, {"text": "回答开头"})
            self.app.cards.trace_updates(job).close()
            self.app.cards.update_card_activity(ctx, job, "执行中")
            job.body_clear_pending, job.answer_finalized = True, True  # The final answer is drawn.
            self.app.cards.update_card_activity(ctx, job, "")
        self.assertEqual([c.path.rsplit("/", 1)[-1] for c in lark.calls], ["answer", "activity", "activity"])
        self.assertEqual(json.loads(lark.calls[0].data["partial_element"])["content"], "回答开头")

    def test_an_empty_answer_so_far_leaves_the_body_blank(self):
        # T182 #1128: Claude's empty snapshot at each tool start wrote 正在处理… under a status line
        # that already said 执行中.
        from covey.app.interaction import _show_answer_snapshot
        job, ctx = self.running_with_card()
        with FakeCardKit() as lark:
            _show_answer_snapshot(self.app.cards, ctx, job, {"text": ""})
            self.app.cards.trace_updates(job).close()
        self.assertEqual([json.loads(c.data["partial_element"])["content"] for c in lark.calls], [" "])

    def test_a_streamed_answer_over_the_cards_size_still_lets_the_run_end_on_the_card(self):
        # #1150 (T202): 5,000 numbers streamed, PATCH …/elements/answer refused three times with 200860
        # card over max size, the card disabled and the top stuck at 执行中 · 已运行 33 秒.
        from covey.app.interaction import _show_answer_snapshot
        from covey.feishu.transport import Failure, set_failure
        job, ctx = self.running_with_card()

        def api(profile, method, path, data, **kwargs):
            # CardKit took 2,188 lines of one number each and refused 2,220 (the B2-0 probe).
            if path.endswith("/elements/answer") and json.loads(data["partial_element"])["content"].count("\n") > 2100:
                set_failure(Failure("api_rejected", code="200860", detail="ErrMsg: card over max size;"))
                return None
            return {"code": 0}
        with FakeCardKit(side_effect=api) as lark:
            for end in (2000, 3500, 4200, 5000):
                _show_answer_snapshot(self.app.cards, ctx, job, {"text": "".join(f"{i}\n" for i in range(1, end + 1))})
                self.app.cards.trace_updates(job).close()
                job.card_updates = None
            self.assertFalse(job.progress.disabled)
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
        self.assertTrue(self.app.cards.body_cut(job))
        status, _ = json.loads(lark.calls[-1].data["actions"])
        self.assertEqual(json.loads(status["params"]["element"])["content"], status_markdown("有结果"))

    def test_a_card_disabled_by_failed_writes_still_gets_its_outcome_once(self):
        job, ctx = self.running_with_card()
        job.progress.disabled = job.progress.failed_off = True  # Three failed body writes.
        job.progress.others = {"action_row": 900, "activity": 200}
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            self.assertTrue(self.app.cards.update_streaming_reply(ctx, job.progress, "↓ 结论", force=True))
        self.assertEqual([c.path.rsplit("/", 1)[-1] for c in lark.calls], ["batch_update", "answer"])
        self.assertNotIn("action_row", job.progress.others)  # The row went: the body's room counts it no more.
        # A card closed or replaced stays as it is.
        job, ctx = self.running_with_card()
        job.progress.disabled = True
        with FakeCardKit() as lark:
            self.assertFalse(self.app.cards.show_outcome(ctx, job, "有结果"))
        self.assertEqual(lark.calls, [])

    def test_the_outcome_says_how_long_the_run_took(self):
        # T182 #1111: ✅ 已完成 alone at the top; its 1 h 47 min only as 总耗时 6460.3 秒 lower down.
        with FakeCardKit() as lark:
            job, ctx = self.drawn_trace()
            job.tool_trace.started_at -= 6460.3
            job.timings["backend_requested"] = job.tool_trace.started_at  # The trace starts at the request (worker).
            drawn = len(lark.calls)
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果", timed=True))
            self.assertTrue(job.trace_card_updates.close())
        status = json.loads(json.loads(lark.calls[drawn].data["actions"])[0]["params"]["element"])
        self.assertEqual(status["content"], status_markdown("有结果 · 用时 1小时47分"))
        # The outcome alone is kept: the result below still leaves it to the status line.
        self.assertEqual(job.progress.outcome, "有结果")
        self.assertEqual(self.app.cards.render_stream_final(job, "↓ 结论", status="done", answer_elsewhere=True), "↓ 结论")
        job, ctx = self.running_with_card()  # Without a trace: from the backend request to the end of the run.
        job.timings = {"backend_requested": 1000.0, "execution_finished": 1125.0}
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "已停止", stopped=True, timed=True))
        status = json.loads(json.loads(lark.calls[0].data["actions"])[0]["params"]["element"])
        self.assertEqual(status["content"], status_markdown("已停止 · 用时 2分05秒"))
        job, ctx = self.running_with_card()  # Never reached the backend: the outcome alone.
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "停住了", timed=True))
        self.assertEqual(json.loads(json.loads(lark.calls[0].data["actions"])[0]["params"]["element"])["content"],
                         status_markdown("停住了"))

    def dashboard_trace(self):
        """drawn_trace on a card with the dashboard look (since 2026-11-04): its figure tiles under the status."""
        from covey.core.trace import task_trace
        job, ctx = self.running_with_card()
        job.progress.dashboard = True
        job.tool_trace = task_trace()
        self.app.cards.queue_trace(ctx, job, urgent=True)
        wait_for(self, lambda: job.trace_rendered_panel is not None)
        return job, ctx

    @staticmethod
    def tiles(batch):
        """The (label, value) of the task_figures tiles a batch_update puts, or None."""
        for action in json.loads(batch.data.get("actions") or "[]"):
            if action["params"].get("element_id") == "task_figures":
                element = json.loads(action["params"]["element"])
                return [(re.sub(r"<[^>]+>", "", column["elements"][0]["content"]), column["elements"][1]["content"].strip("*"))
                        for column in element["columns"]]
        return None

    def test_a_tile_count_is_short_enough_for_a_third_of_the_side_panel(self):
        # short_count's "9.01 万" wrapped in a tile of a topic's side panel (2026-11-04 client check).
        from covey.cards.facts import tile_count
        self.assertEqual([tile_count(n) for n in (8123, 10000, 41900, 99499, 99500, 329638, 99995000, 141137149, 2 * 10 ** 9)],
                         ['8,123', '1.0万', '4.2万', '9.9万', '10万', '33万', '1.0亿', '1.4亿', '20亿'])

    def test_a_dashboard_card_s_tiles_go_with_its_process_panel_and_its_end(self):
        # The A 仪表盘 sample (2026-11-04): 用时 / token / 速度 / 上下文 by the status line, in the batches that
        # carry the process panel and 任务详情, and in the outcome's.
        with FakeCardKit() as lark:
            job, ctx = self.dashboard_trace()
            trace = job.tool_trace
            trace.started_at -= 48
            trace.input_tokens, trace.output_tokens = 39712, 2184
            trace.context_tokens, trace.context_window = 39700, 1000000
            trace.accept("tool", {"id": "ls", "name": "bash", "status": "running", "detail": "ls", "started_at": time.time()})
            first = [self.tiles(c) for c in lark.calls if c.path.endswith("batch_update") and self.tiles(c)]
            self.assertEqual(first[-1][1:], [("token", "—"), ("速度 · token/s", "—"), ("上下文", "—")])  # Drawn with the panel: none yet.
            self.app.cards.queue_trace(ctx, job, urgent=True)
            wait_for(self, lambda: len([c for c in lark.calls if c.path.endswith("batch_update") and self.tiles(c)]) > len(first))
            update = [c for c in lark.calls if c.path.endswith("batch_update") and self.tiles(c)][-1]
            running = self.tiles(update)
            self.assertEqual(running[1:], [("token", "4.2万"), ("速度 · token/s", "—"), ("上下文", "4%")])
            # With the panel's change, in one batch (trace-update), not on its own.
            self.assertTrue(any(a["params"].get("element_id") != "task_figures" for a in json.loads(update.data["actions"])))
            # Running, the first tile says when it began: the status line's 已运行 is kept current, a 用时 here
            # moved only with the steps (2026-11-04 15:07).
            self.assertEqual(running[0][0], "开始")
            drawn = len(lark.calls)
            trace.generation_seconds, trace.measured_output_tokens = 26.0, 2184
            trace.started_at -= 60  # A minute on: the end's 用时 differs from the running tile's.
            job.timings["backend_requested"] = trace.started_at
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果", timed=True))
            self.assertTrue(job.trace_card_updates.close())
        end = lark.calls[drawn]
        status = json.loads(json.loads(end.data["actions"])[0]["params"]["element"])
        # The end time under the outcome; its time is the 用时 tile's, its speed 任务详情's (2026-11-04 15:07).
        self.assertRegex(status["content"], "^" + re.escape(state_pill("result")) + r" <font color='grey'>\d\d:\d\d 结束</font>$")
        self.assertEqual(self.tiles(end)[0], ("用时", "1分48秒"))  # Without spaces: a side panel wrapped "1 分 20 秒".
        # The speed in its own tile, as large as the others (the owner, 2026-11-04 20:21: token/s的数字也比较小).
        self.assertEqual(self.tiles(end)[1:], [("token", "4.2万"), ("速度 · token/s", "84"), ("上下文", "4%")])
        self.assertIn("用时", job.progress.timed_outcome)  # The chat list's preview keeps it.
        # A Harness without a context figure: "—", never a guess.
        from covey.cards.task_figures import figure_cells
        self.assertEqual(figure_cells({"started_at": 1, "ended_at": 3})[1:],
                         [("token", "—", ""), ("速度", "—", "token/s"), ("上下文", "—", "")])

    def test_a_new_card_has_its_status_line_in_two_parts(self):
        # The "A 一行仪表" pick (the owner, 2026-11-04 21:47): the card says so to every later write.
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        with FakeCardKit(return_value={"code": 0, "data": {"card_id": "card-new"}}) as lark:
            progress = self.app.cards.stream.create(None, job, "排队中")
        self.assertEqual((progress.card_id, progress.dashboard, progress.status_rows, progress.more_text),
                         ("card-new", True, True, ""))
        self.assertIn("activity_more", json.dumps(lark.calls[0].created))

    def test_a_card_in_two_parts_writes_the_status_beside_the_tiles_and_the_rest_under_them(self):
        # A tick that changes only 已运行 writes the status alone, as on any card; a new 阶段 or 当前 writes both,
        # in one batch.
        job, ctx = self.running_with_card()
        job.progress.dashboard = job.progress.status_rows = True
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, "在做 · 已运行 5 秒 · 阶段：读代码\n现在：执行命令"))
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, "在做 · 已运行 6 秒 · 阶段：读代码\n现在：执行命令"))
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, "在做 · 已运行 7 秒 · 阶段：读代码\n现在：模型思考"))
        both, tick, changed = lark.calls
        self.assertTrue(both.path.endswith("/batch_update"))
        written = {a["params"]["element_id"]: json.loads(a["params"]["element"])["content"]
                   for a in json.loads(both.data["actions"])}
        self.assertEqual(written, {"activity": f"{state_pill('running')} <font color='grey'>已运行 5 秒</font>",
                                   "activity_more": "阶段：读代码\n<font color='grey'>现在：执行命令</font>"})
        self.assertTrue(tick.path.endswith("/elements/activity"))
        self.assertEqual(json.loads(tick.data["partial_element"])["content"], f"{state_pill('running')} <font color='grey'>已运行 6 秒</font>")
        self.assertTrue(changed.path.endswith("/batch_update"))
        self.assertEqual(job.progress.more_text, "阶段：读代码\n现在：模型思考")
        # Stopping: 正在停止… beside the tiles and why under them, with the buttons' removal in the same batch.
        with FakeCardKit() as lark:
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
        actions = json.loads(lark.calls[0].data["actions"])
        self.assertEqual([(a["action"], a["params"].get("element_id")) for a in actions],
                         [("update_element", "activity"), ("update_element", "activity_more"), ("delete_elements", None)])
        self.assertEqual(json.loads(actions[1]["params"]["element"])["content"], "已收到停止请求，正在中断后台任务。")

    @staticmethod
    def outputs(call):
        """The 输出 text (task_figures) a write puts on a card with the 输入/输出 box, without markup, or None."""
        def plain(text):
            return re.sub(r"<[^>]+>|\*\*", "", text).replace("\u2060", "").replace("\u00a0", " ")
        if call.path.endswith("/elements/task_figures"):
            return plain(json.loads(call.data["partial_element"])["content"])
        for action in json.loads(call.data.get("actions") or "[]"):
            if action["params"].get("element_id") == "task_figures":
                return plain(json.loads(action["params"]["element"])["content"])
        return None

    def test_an_io_card_has_its_running_time_in_its_outputs_and_writes_once_a_tick(self):
        # The "E" sample (2026-11-08, DESIGN-cardio §4): 已运行 moved from the status line to 输出; a tick that
        # changes only it writes 输出 alone, one write as before; from 3 minutes on it says minutes alone.
        from covey.cards.answer_card import figure
        job, ctx = self.running_with_card()
        progress = job.progress
        progress.dashboard = progress.status_rows = progress.io_box = True
        progress.figures_parts = [["开始", figure("开始", "14:14")], ["token", figure("token", "58万")]]
        line = "在做 · 已运行 {} · 阶段：读代码\n现在：执行命令"
        with FakeCardKit() as lark:
            for ran in ("5秒", "6秒", "3分05秒", "3分09秒", "3分09秒"):
                self.assertTrue(self.app.cards.update_card_activity(ctx, job, line.format(ran)))
            self.assertTrue(self.app.cards.update_card_activity(ctx, job, line.format("3分10秒").replace("执行命令", "Read")))
        first, tick, minutes, changed = lark.calls  # 3分09秒 is 3分 again: nothing written.
        self.assertTrue(first.path.endswith("/batch_update"))
        written = {a["params"]["element_id"]: json.loads(a["params"]["element"])["content"]
                   for a in json.loads(first.data["actions"])}
        self.assertEqual(written["activity"], state_pill("running"))  # Without 已运行: it is in 输出.
        self.assertEqual(written["activity_more"], "阶段：读代码\n<font color='grey'>现在：执行命令</font>")
        self.assertEqual(self.outputs(first), "开始 14:14 ｜ 已运行 5秒 ｜ token 58万")  # One line (2026-11-09).
        self.assertTrue(tick.path.endswith("/elements/task_figures"))
        self.assertEqual(self.outputs(tick), "开始 14:14 ｜ 已运行 6秒 ｜ token 58万")
        self.assertEqual(self.outputs(minutes), "开始 14:14 ｜ 已运行 3分 ｜ token 58万")
        self.assertTrue(changed.path.endswith("/batch_update"))  # 现在 changed: the line under the status, alone.
        self.assertEqual([a["params"]["element_id"] for a in json.loads(changed.data["actions"])], ["activity_more"])
        self.assertEqual(progress.top_text, "在做")

    def test_an_io_card_s_outputs_go_with_its_process_panel_and_its_end(self):
        with FakeCardKit() as lark:
            job, ctx = self.running_with_card()
            job.progress.dashboard = job.progress.status_rows = job.progress.io_box = True
            from covey.core.trace import task_trace
            job.tool_trace = trace = task_trace()
            trace.started_at -= 48
            job.timings["backend_requested"] = trace.started_at
            trace.input_tokens, trace.output_tokens = 39712, 2184
            trace.context_tokens, trace.context_window = 39700, 1000000
            trace.accept("tool", {"id": "ls", "name": "bash", "status": "running", "detail": "ls", "started_at": time.time()})
            self.app.cards.queue_trace(ctx, job, urgent=True)
            wait_for(self, lambda: job.trace_rendered_panel is not None)
            running = [self.outputs(c) for c in lark.calls if c.path.endswith("batch_update") and self.outputs(c)]
            self.assertRegex(running[-1], r"^开始 \d\d:\d\d ｜ 已运行 4\d秒 ｜ token 4.2万 ｜ 上下文 4%$")
            self.assertEqual([label for label, _ in job.progress.figures_parts], ["开始", "token", "上下文"])
            drawn = len(lark.calls)
            trace.generation_seconds, trace.measured_output_tokens = 26.0, 2184
            trace.cache_read_tokens = 30000
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果", timed=True))
            self.assertTrue(job.trace_card_updates.close())
        end = lark.calls[drawn]
        status = json.loads(json.loads(end.data["actions"])[0]["params"]["element"])
        self.assertRegex(status["content"], "^" + re.escape(state_pill("result")) + r" <font color='grey'>\d\d:\d\d 结束</font>$")
        # As its answer card's: 用时 ｜ token/s ｜ token ｜ 缓存命中, one line.
        self.assertRegex(self.outputs(end), r"^用时 4\d秒 ｜ 84 token/s ｜ token 4.2万 ｜ 缓存命中 76%$")

    def io_card(self, job):
        job.progress = StreamingReply("card-q", "om_card", sequence=1, dashboard=True, status_rows=True, io_box=True,
                                      figures_text=json.dumps({"tag": "markdown", "element_id": "task_figures"}))
        return job

    def test_a_cancelled_queued_io_card_s_outputs_say_no_running_time(self):
        # B3 review: a card that never ran said 开始 20:16 · 已运行 3分 in 输出 (run_start falls back to queued).
        self.receive("om_a", "one")
        job = self.io_card(self.app.runtime.jobs.queue.queue[0])
        job.timings["queued"] = time.time() - 200
        with FakeCardKit() as lark:
            self.app.controls.cancel_reply_job_from_card(self.topic("om_a"), {"job_id": str(job.job_id)}, "oc_chat", "cancel")
        written = [text for text in map(self.outputs, lark.calls) if text is not None]
        self.assertTrue(written)
        self.assertFalse([text for text in written if "已运行" in text or "开始" in text], written)
        self.assertEqual(written[-1], "—")

    def test_a_timed_outcome_without_a_run_or_a_record_says_no_running_time(self):
        job, ctx = self.running_with_card()
        self.io_card(job)
        job.timings["dispatched"] = time.time() - 90
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "❌ 失败", timed=True))
        written = [text for text in map(self.outputs, lark.calls) if text is not None]
        self.assertEqual(written, ["—"])

    def test_the_end_of_a_card_in_two_parts_empties_the_line_under_the_tiles(self):
        with FakeCardKit() as lark:
            job, ctx = self.dashboard_trace()
            job.progress.status_rows, job.progress.more_text = True, "现在：执行命令"
            drawn = len(lark.calls)
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果", timed=True))
            self.assertTrue(job.trace_card_updates.close())
        actions = json.loads(lark.calls[drawn].data["actions"])
        status, more = (json.loads(a["params"]["element"]) for a in actions[:2])
        self.assertRegex(status["content"], "^" + re.escape(state_pill("result")) + r" <font color='grey'>\d\d:\d\d 结束</font>$")
        self.assertEqual((more["element_id"], more["content"]), ("activity_more", ""))
        self.assertEqual(actions[2], {"action": "delete_elements", "params": {"element_ids": ["action_row"]}})
        self.assertEqual(job.progress.more_text, "")

    def test_a_dashboard_card_keeps_its_body_and_its_finished_buttons_go_after_its_details(self):
        with FakeCardKit() as lark:
            job, ctx = self.dashboard_trace()
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            self.assertTrue(job.trace_card_updates.close())
            written = len(lark.calls)
            self.assertTrue(self.app.cards.lead_first(ctx, job))  # The body is under the tiles already.
            self.assertEqual(len(lark.calls), written)
            self.assertFalse(job.progress.lead_first)
            self.app.switch.attach_topic_controls(ctx, job)
        insert = lark.calls[-1]
        self.assertEqual((insert.data["type"], insert.data["target_element_id"]), ("insert_after", "task_details"))
        buttons = [e for e in walk_dicts(json.loads(insert.data["elements"])) if e.get("tag") == "button"]
        self.assertTrue(buttons)
        # Bordered and small, 8 px apart (the owner, 2026-11-04 14:05: A 线框, text ones were hard to hit; 2026-11-22:
        # 换配置和回退窄一点小一点).
        self.assertTrue(all(b["type"] == "default" and b["size"] == "small" for b in buttons))
        rows = [e for e in walk_dicts(json.loads(insert.data["elements"])) if e.get("flex_mode") == "flow"]
        self.assertTrue(rows)
        self.assertEqual({r["horizontal_spacing"] for r in rows}, {"8px"})

    def test_a_finished_turn_s_buttons_go_under_its_process_panel(self):
        # T182 #1111: the buttons (then 🔀 💬 🔁) sat between the result and its artifacts and figures.
        with FakeCardKit() as lark:
            job, ctx = self.drawn_trace()
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            self.assertTrue(job.trace_card_updates.close())
            self.app.switch.attach_topic_controls(ctx, job)
        insert = lark.calls[-1]
        self.assertEqual((insert.method, insert.data["type"], insert.data["target_element_id"]),
                         ("POST", "insert_after", "process_panel"))
        self.assertEqual(json.loads(insert.data["elements"])[0]["element_id"], "fork_actions")
        job, ctx = self.running_with_card()  # A card with no process panel drawn keeps them under the body.
        with FakeCardKit() as lark:
            self.app.switch.attach_topic_controls(ctx, job)
        self.assertEqual(lark.calls[-1].data["target_element_id"], "answer")

    def test_the_run_s_end_puts_its_outcome_where_the_status_was_and_drops_the_buttons_at_once(self):
        # S26 client run: the top of the card went blank for about a second before the answer came.
        job, ctx = self.running_with_card()
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            self.app.steering.close_task_controls(ctx, job)
        self.assertEqual(lark.calls[0].path, "/open-apis/cardkit/v1/cards/card-a/batch_update")
        status, buttons = json.loads(lark.calls[0].data["actions"])
        self.assertEqual(json.loads(status["params"]["element"])["content"], status_markdown("有结果"))
        self.assertEqual(buttons, {"action": "delete_elements", "params": {"element_ids": ["action_row"]}})
        self.assertFalse([c for c in lark.calls if c.method == "DELETE"])  # Closing the controls keeps it.

    def test_after_a_stop_the_outcome_replaces_stopping(self):
        job, ctx = self.running_with_card()
        with FakeCardKit() as lark:
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
            stopping = len(lark.calls)
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "已停止"))  # Not a monitor tick: not held back.
        end = lark.calls[stopping:]
        self.assertEqual([c.path.rsplit("/", 1)[-1] for c in end], ["activity"])
        self.assertEqual(json.loads(end[0].data["partial_element"])["content"], status_markdown("已停止"))
        sequences = [c.data["sequence"] for c in lark.calls]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_an_old_card_without_the_action_row_still_gets_its_outcome(self):
        job, ctx = self.running_with_card()
        with FakeCardKit(side_effect=[None] + [{"code": 0}] * 5) as lark:  # Old card: the batch is refused.
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
        self.assertTrue(lark.calls[1].path.endswith("/elements/activity"))
        self.assertEqual(json.loads(lark.calls[1].data["partial_element"])["content"], status_markdown("有结果"))
        self.assertFalse(job.action_controls_removed)  # close_task_controls removes its buttons as before.

    def drawn_trace(self):
        """A running job whose card shows its process panel waiting for progress."""
        from covey.core.trace import task_trace
        job, ctx = self.running_with_card()
        job.tool_trace = task_trace()
        self.app.cards.queue_trace(ctx, job, urgent=True)
        wait_for(self, lambda: job.trace_rendered_panel is not None)
        return job, ctx

    def test_the_outcome_ends_the_process_panel_in_the_same_write(self):
        # S31 K5: ✅ 已完成 over 执行过程 · 等待进展 … 正在处理 for 0.24 s, until the result's write.
        with FakeCardKit() as lark:
            job, ctx = self.drawn_trace()
            self.assertIn("等待进展", json.dumps(lark.calls[-1].data, ensure_ascii=False))
            drawn = len(lark.calls)
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            self.assertTrue(self.app.cards.finalize_trace(ctx, job))
        end = lark.calls[drawn:]
        status, buttons, *panel = json.loads(end[0].data["actions"])
        self.assertEqual(json.loads(status["params"]["element"])["content"], status_markdown("有结果"))
        self.assertEqual(buttons, {"action": "delete_elements", "params": {"element_ids": ["action_row"]}})
        panel = json.dumps(panel, ensure_ascii=False)
        self.assertIn("无执行记录", panel)
        self.assertNotIn("正在处理", panel)
        # The status line has the time, and a turn without records has nothing to unfold (2026-11-02 review).
        self.assertNotIn("用时", panel)
        self.assertNotIn("查看过程记录", panel)
        # The result's finalize_trace finds the panel ended: only 任务详情 would be left to write, and with nothing
        # to say (the 输入/输出 box has the figures since 2026-11-09) it is not.
        self.assertEqual([c.path.rsplit("/", 1)[-1] for c in end[1:]], [])
        sequences = [c.data["sequence"] for c in lark.calls]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_after_a_stop_the_process_panel_ends_before_the_outcome(self):
        with FakeCardKit() as lark:
            job, ctx = self.drawn_trace()
            self.app.controls.cancel_reply_job_from_card(ctx, {"job_id": str(job.job_id)}, "oc_chat", "stop")
            drawn = len(lark.calls)
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "已停止", stopped=True))
            end = lark.calls[drawn:]
            self.assertTrue(self.app.cards.finalize_trace(ctx, job, stopped=True))
        self.assertEqual([c.path.rsplit("/", 1)[-1] for c in end], ["batch_update", "activity"])
        self.assertIn("无执行记录", json.dumps(end[0].data, ensure_ascii=False))
        self.assertTrue(job.tool_trace.stopped)

    def test_a_trace_update_read_before_the_end_does_not_draw_over_it(self):
        with FakeCardKit() as lark:
            job, ctx = self.drawn_trace()
            running = job.tool_trace.snapshot()
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            drawn = len(lark.calls)
            job.tool_trace.snapshot = lambda: running  # As a flush that read the trace before it ended.
            self.app.cards.queue_trace(ctx, job, urgent=True)
            self.assertTrue(job.trace_card_updates.close())
        self.assertEqual(lark.calls[drawn:], [])

    USAGE = {"input_tokens": 1_234_567, "output_tokens": 23_456, "cache_read_tokens": 1_100_000,
             "cache_write_tokens": 100_000, "measured_output_tokens": 23_456, "generation_seconds": 200.0}

    def flush(self, ctx, job):
        """The process panel's next write, waited for."""
        self.app.cards.queue_trace(ctx, job, urgent=True)
        self.assertTrue(job.trace_card_updates.close())
        job.trace_card_updates = None

    def test_task_details_never_go_on_a_card_and_one_sent_before_keeps_its_words(self):
        # The owner, 2026-11-22: 任务详情就没必要了 (the card keeps 本轮请求 and 执行过程). A turn with words for it (a
        # timeout, its figures) writes none, running or ended, and a card that had one (sent before)
        # keeps what it shows: no write of 任务详情, none refused.
        from covey.core.trace import task_trace
        for live in (False, True):
            with self.subTest(live=live):
                job, ctx = self.running_with_card()
                job.timeout = 600
                job.progress.details_panel = live
                job.progress.live_details, job.progress.details_text = live, "token 用量：输入 1 · 输出 1 token" if live else ""
                job.tool_trace = task_trace()
                job.tool_trace.accept("token_usage", self.USAGE)
                with FakeCardKit() as lark:
                    self.flush(ctx, job)
                    job.tool_trace.accept("tool", {"id": "a", "name": "bash", "status": "running", "detail": "ls",
                                                   "started_at": 100.0})
                    self.flush(ctx, job)
                    self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
                    self.assertTrue(self.app.cards.finalize_trace(ctx, job))
                self.assertTrue(lark.calls)  # The process panel and the outcome went.
                self.assertFalse([c for c in lark.calls if "task_details" in json.dumps(c.data)])
                self.assertEqual(job.progress.update_failures, 0)

    def test_a_running_card_previews_its_phase_and_question_and_its_close_the_outcome(self):
        # T314 2026-11-02: the topic list shows a topic's latest message's preview; a running task card's was its
        # title, as a stopped or failed one's.
        job, ctx = self.running_with_card()
        label = self.app.cards.summary_label(job)
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_running_summary(ctx, job, "改代码"))
            self.assertTrue(self.app.cards.show_running_summary(ctx, job, "改代码"))  # Unchanged: not written again.
            self.assertTrue(self.app.cards.show_running_summary(ctx, job, "改代码", asking=True))
        self.assertTrue(all((c.method, c.path.rsplit("/", 1)[-1]) == ("PATCH", "settings") for c in lark.calls))
        self.assertEqual([json.loads(c.data["settings"])["config"] for c in lark.calls],
                         [{"summary": {"content": f"{label} 在做 · 改代码"}},
                          {"summary": {"content": f"{label} 等你"}}])
        with FakeCardKit() as lark:
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "停住了"))
            self.app.cards.stream.close(ctx.profile, job.progress)
        config = json.loads(lark.calls[-1].data["settings"])["config"]
        self.assertEqual(config, {"streaming_mode": False, "summary": {"content": f"{label} 停住了"}})

    NOTE ="回答已单独发送在下方；直接回复本话题即可继续。"
    NEXT = "已保存的结果和记录保留在本话题。"

    def test_the_result_below_leaves_the_outcome_to_the_status_line(self):
        # S27 client run: "✅ 已完成" at the top, and again over "回答已单独发送在下方…" lower down.
        job, ctx = self.running_with_card()
        render = self.app.cards.render_stream_final
        with FakeCardKit():
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
        self.assertEqual(render(job, self.NOTE, status="done", answer_elsewhere=True), self.NOTE)
        self.assertEqual(render(job, "好", status="done"), "**答案**\n好")  # An answer keeps its title.
        self.assertIn("停住了", render(job, "出错", status="failed"))  # Not what the top says.
        with FakeCardKit():
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "已停止"))
        # "已停止 · 任务 #844 已停止。…" said it twice more; what the top lacks stays.
        self.assertEqual(render(job, stopped_sentence(job.job_id) + self.NEXT, status="cancelled"), self.NEXT)
        for status, outcome in (("failed", "停住了"), ("waiting_user", "等你")):
            with FakeCardKit():
                self.assertTrue(self.app.cards.show_outcome(ctx, job, outcome))
            self.assertEqual(render(job, "正文", status=status), "正文")

    def test_without_the_outcome_at_the_top_the_result_still_says_it(self):
        job, ctx = self.running_with_card()
        render = self.app.cards.render_stream_final
        with FakeCardKit(None) as lark:  # An old card without the status line, or CardKit refused both writes.
            self.assertFalse(self.app.cards.show_outcome(ctx, job, "已停止"))
        self.assertEqual(len(lark.calls), 2)
        stopped = stopped_sentence(job.job_id) + self.NEXT
        self.assertEqual(render(job, stopped, status="cancelled"), "**已停止**\n" + stopped)
        with FakeCardKit():
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
            self.app.cards.update_card_activity(ctx, job, "")  # Any later status-line write.
        self.assertIn("有结果", render(job, self.NOTE, status="done", answer_elsewhere=True))
        with FakeCardKit():
            self.assertTrue(self.app.cards.show_outcome(ctx, job, "有结果"))
        job.progress = StreamingReply("card-b", "om_card_b", sequence=1)  # A replacement card has its own line.
        self.assertIn("有结果", render(job, self.NOTE, status="done", answer_elsewhere=True))

    def test_cancelling_a_queued_task_says_so_at_the_top_first(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.queue[0]
        job.progress = StreamingReply("card-q", "om_card", sequence=1)
        with FakeCardKit() as lark:
            self.app.controls.cancel_reply_job_from_card(self.topic("om_a"), {"job_id": str(job.job_id)}, "oc_chat", "cancel")
        writes = [c.path.rsplit("/", 1)[-1] for c in lark.calls]
        self.assertLess(writes.index("batch_update"), writes.index("answer"))  # The top before the body.
        status, _ = json.loads(lark.calls[writes.index("batch_update")].data["actions"])
        self.assertEqual(json.loads(status["params"]["element"])["content"], status_markdown("⏹ 已取消排队"))
        # No later write blanks it, and the row is not deleted twice.
        self.assertFalse([c for c in lark.calls if c.path.endswith("/elements/activity") or c.method == "DELETE"])

    def test_an_answered_question_task_says_so_in_its_status_line_and_keeps_what_it_said(self):
        # Repo review U1: the body ("↓ <the reply's lead>") became "已收到回答 · 后续任务 #6 继续处理。结果见下方任务卡。"
        # (the card below was the next turn's) and the status line lost the time; a task whose questions were
        # not registered lost its whole card to the recovery card.
        ctx = self.topic("om_q")
        questions = ctx.state_dir / "questions"
        questions.mkdir(parents=True, exist_ok=True)
        follow_up = make_reply_job("chat", root=self.root, with_progress=False)
        follow_up.job_id, follow_up.message_id, follow_up.topic_id = 6, "om_answer", ctx.topic_id
        for registered in (True, False):
            with self.subTest(registered=registered):
                key = f"om_q_{registered}"
                # A card from before the 用时 tile (DASHBOARD_SINCE): its status line keeps 用时.
                upsert_job_record(ctx.state_dir, key, topic_id=ctx.topic_id, chat_id="oc_chat", job_id=5, kind="chat",
                                  backend="claude", status="waiting_user", card_id="card-q",
                                  card_message_id="om_card_q", elapsed_seconds=142, created_at="2026-11-02T00:00:00Z")
                if registered:
                    (questions / ("b" * 16 + ".json")).write_text(json.dumps({
                        "token": "b" * 16, "job_id": 5, "topic_id": ctx.topic_id, "chat_id": "oc_chat",
                        "status": "answered"}))
                with FakeCardKit() as lark:
                    paint = self.app.controls.acknowledge_waiting_answer(ctx, follow_up)
                    self.assertEqual(lark.calls, [])  # S37: the card waits until the continuation is queued.
                    paint()
                self.assertEqual([(c.method, c.path.rsplit("/", 1)[-1]) for c in lark.calls], [("PATCH", "activity")])
                self.assertEqual(json.loads(lark.calls[0].data["partial_element"])["content"],
                                 status_markdown("↪ 已回答 → #6 · 用时 2分22秒"))  # It said ❓ 需要你回答 · 用时 2 分 22 秒.
                self.assertEqual(find_job_record(ctx.state_dir, key)["status"], "continued")
                (questions / ("b" * 16 + ".json")).unlink(missing_ok=True)
        with FakeCardKit(side_effect=[None, {"code": 0}]) as lark:  # A card without the status line.
            paint()
        self.assertEqual([(c.method, c.path.rsplit("/", 1)[-1]) for c in lark.calls],
                         [("PATCH", "activity"), ("PUT", "answer")])
        self.assertEqual(json.loads(lark.calls[1].data["element"])["content"], "↪ 已回答 → #6 · 用时 2分22秒")
        self.assertLess(lark.calls[0].data["sequence"], lark.calls[1].data["sequence"])

    def test_a_card_with_the_time_tile_does_not_say_its_time_again_once_answered(self):
        # The owner, 2026-11-04 17:54 (T386): ↪ 已回答 → #2794 · 用时 13 分 24 秒 under a 用时 13分24秒 tile.
        from covey.app.recovery_cards import DASHBOARD_SINCE, answered_line
        record = {"job_id": 5, "elapsed_seconds": 804, "created_at": "2026-11-04T09:00:00Z"}
        self.assertGreater(record["created_at"], DASHBOARD_SINCE)
        self.assertEqual(answered_line(record, 6, self.root), "↪ 已回答 → #6")
        self.assertEqual(answered_line(record, 6, self.root, answered=False), "↪ #6 已接着开始，这组问题没有回答")

    def test_a_message_queued_before_the_task_asked_does_not_answer_its_questions(self):
        # Repo review U8, T206 #1178: the next message, queued while #1178 ran, started once it asked; the whole
        # card became "已收到回答 · 继续处理中" (no title, request or details) over questions no one answered.
        ctx = self.topic("om_q")
        checkpoint = self.root / "asked.json"
        checkpoint.write_text(json.dumps({"requires_user": True, "question": "哪个数据集？"}))
        upsert_job_record(ctx.state_dir, "om_q", topic_id=ctx.topic_id, chat_id="oc_chat", job_id=5, kind="chat",
                          backend="claude", status="waiting_user", card_id="card-q", card_message_id="om_card_q",
                          checkpoint_path=str(checkpoint), elapsed_seconds=61, created_at="2026-11-02T00:00:00Z")
        job = make_reply_job("chat", root=self.root, with_progress=False)
        job.job_id, job.message_id, job.topic_id, job.chat_id = 6, "om_next", ctx.topic_id, "oc_chat"
        job.checkpoint_path, job.recovery_context = str(self.root / "next.json"), None
        with FakeCardKit() as lark, mock.patch.object(self.app.cards, "replace_record_card") as replace:
            self.app.runs._checkpoint_note(ctx, job)
        replace.assert_not_called()
        self.assertEqual([(c.method, c.path.rsplit("/", 1)[-1]) for c in lark.calls], [("PATCH", "activity")])
        self.assertEqual(json.loads(lark.calls[0].data["partial_element"])["content"],
                         status_markdown("↪ #6 已接着开始，这组问题没有回答 · 用时 1分01秒"))
        record = find_job_record(ctx.state_dir, "om_q")
        self.assertEqual((record["status"], record["status_message"]), ("continued", "#6 已接着开始，这组问题没有回答"))
        self.assertEqual(job.recovery_context["question"], "哪个数据集？")

    def test_an_answer_s_continuation_is_queued_before_the_answered_card_is_repainted(self):
        ctx = self.topic("om_q")
        upsert_job_record(ctx.state_dir, "om_q", topic_id=ctx.topic_id, chat_id="oc_chat", job_id=5, kind="chat",
                          backend="codex", status="waiting_user", card_id="card-q", card_message_id="om_card_q")
        order = []

        def paint(job):
            order.append(("painted", self.app.runtime.jobs.queue.qsize()))
            job.progress = None  # Its task runner took the job, and its card, meanwhile.
        self.app.controls.acknowledge_waiting_answer = lambda ctx, job: lambda: paint(job)
        self.app.cards.create_streaming_reply = lambda ctx, job, content: StreamingReply("card-c", "om_card_c")
        self.receive("om_answer", "回答", root="om_q")
        wait_for(self, lambda: order)
        self.assertEqual(order, [("painted", 1)])
        # S37 client run: with the card gone by then, "任务已入队…" went out beside the task card.
        sent = [c.args[2] for c in self.app.messenger.send_message.call_args_list]
        self.assertEqual([text for text in sent if "已入队" in text], [])

    def test_the_answered_card_is_repainted_beside_what_follows_the_queueing(self):
        # S48: the answer's question card waited 470 ms for this repaint; now they go at once.
        ctx = self.topic("om_q")
        upsert_job_record(ctx.state_dir, "om_q", topic_id=ctx.topic_id, chat_id="oc_chat", job_id=5, kind="chat",
                          backend="codex", status="waiting_user", card_id="card-q", card_message_id="om_card_q")
        release, painted = threading.Event(), []
        self.app.controls.acknowledge_waiting_answer = lambda ctx, job: lambda: painted.append(
            (threading.current_thread().name, release.wait(5)))
        self.app.cards.create_streaming_reply = lambda ctx, job, content: StreamingReply("card-c", "om_card_c")
        self.receive("om_answer", "回答", root="om_q")  # Back while the repaint still waits.
        release.set()
        wait_for(self, lambda: painted)
        self.assertEqual(painted, [("card-updates", True)])
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)

    def test_retry_button_targets_exact_job_and_is_idempotent(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        ctx = self.topic("om_a")
        self.app.record_job_state(ctx, job, "interrupted", "restart")
        event = {
            "chat_id": "oc_chat",
            "event_id": "retry",
            "action_value": {"kind": "bridge_job_retry", "job_id": job.job_id, "topic_id": ctx.topic_id},
        }
        ctx.history_path.write_text("")  # The topic's history as the retry reads it: two questions.
        for text in ("earlier", "one"):
            append_history(ctx.history_path, "user", text)
        self.app.controls.task_action(ctx, event)
        self.app.controls.task_action(ctx, event)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(self.app.runtime.jobs.queue.queue[0].user_text, "one")
        self.assertIsNotNone(self.app.runtime.jobs.queue.queue[0].recovery_context)
        self.assertEqual(["earlier"], self.app.runtime.jobs.queue.queue[0].recent_questions)

    def test_a_retry_click_is_acknowledged_before_its_task_card_and_a_repeat_is_refused(self):
        # REVIEW2 item 9: the new task card, the old card's update and the reply took 3.5 s in the click.
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        ctx = self.topic("om_a")
        self.app.record_job_state(ctx, job, "interrupted", "restart")
        making, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.app.cards.create_streaming_reply = mock.Mock(side_effect=lambda *a: (making.set(), release.wait(30), None)[-1])
        lanes, acknowledged = acknowledging_lanes(self, self.app.handle_card_action_event)

        def said():
            return " ".join(str(c.args[2]) for c in self.app.messenger.send_message.call_args_list)
        for event_id in ("retry", "again"):
            lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": event_id, "action_value": {
                "kind": "bridge_job_retry", "job_id": job.job_id, "topic_id": ctx.topic_id}}, ctx)
        self.assertTrue(making.wait(5))
        wait_for(self, lambda: acknowledged == ["retry", "again"])  # Both, while the task card is being made.
        release.set()
        wait_for(self, lambda: "本话题任务已重新入队" in said() and "本话题还有运行或排队的任务" in said())
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)

    def test_retried_card_says_what_retry_does_and_which_task_continues(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        ctx = self.topic("om_a")
        self.app.record_job_state(ctx, job, "interrupted", "restart")
        record = next(r for r in self.app.harness.topic_records(ctx, "oc_chat") if r["job_id"] == job.job_id)
        # What 检查并继续 does: one grey line under the buttons that opens on it, not in the card's body (T666).
        note, = [e for e in recovery_card(record)["body"]["elements"] if e.get("element_id") == "result_actions_note"]
        self.assertIn("检查并继续：新开一个任务", note["elements"][0]["content"])
        self.assertIn("怎么继续：点“检查并继续”接着做", note["header"]["title"]["content"])
        event = {"chat_id": "oc_chat", "event_id": "retry",
                 "action_value": {"kind": "bridge_job_retry", "job_id": job.job_id, "topic_id": ctx.topic_id}}
        with mock.patch.object(type(self.app.cards), "replace_record_card") as replace:
            self.app.controls.task_action(ctx, event)
        new = self.app.runtime.jobs.queue.queue[0]
        card = replace.call_args.args[2]
        text = card["body"]["elements"][1]["content"]
        self.assertNotIn("header", card)
        # Its row says it went on, one grey line that nothing is asked; not the number of the task it went to (T666).
        self.assertIn("↪ 已重新开始", json.dumps(card["body"]["elements"][0], ensure_ascii=False))
        self.assertEqual(text, "<font color='grey'>不用处理，接着做的进展在后面。</font>")
        self.assertNotIn(f"&#35;{new.job_id}", json.dumps(card, ensure_ascii=False))
        self.assertNotIn("恢复已入队", text)

    def stopped_while_sending(self, **fields):
        """Job #1 whose runner ended while it sent "任务 #1 已停止", as T331 #2407's did: (its context, the
        notice, its record once the restarted Bridge marked it interrupted)."""
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        for name, value in fields.items():
            setattr(job, name, value)
        ctx = self.topic("om_a")
        notice = stopped_sentence(job.job_id) + "已保存的结果和记录保留在本话题。"
        self.app.record_job_state(ctx, job, "running", "正在发送本轮结果", final_text=notice, execution_status="cancelled",
                                  delivery_ok=False, result_retry_policy="saved_delivery_only",
                                  verified_reply_state="invalidated", verified_reply_authorized=False)
        with mock.patch.object(type(self.app.controls), "show_recovery_card", return_value=True) as shown:
            self.app.notify_stale_jobs(ctx)
        return ctx, notice, shown.call_args.args[1]

    def test_an_unsent_stop_notice_is_no_result_and_the_interrupted_task_continues(self):
        # T331 #2407: its runner read the container's SIGTERM as 停止. The card offered only 重新投递结果,
        # which resent the notice and ended the task as stopped; the next click found nothing to retry.
        from covey.app.outbox import retry_label
        ctx, notice, record = self.stopped_while_sending()
        self.assertEqual((record["status"], record["final_text"], retry_label(record)), ("interrupted", None, "检查并继续"))
        self.app.controls.task_action(ctx, {"chat_id": "oc_chat", "event_id": "retry", "action_value": {
            "kind": "bridge_job_retry", "job_id": record["job_id"], "topic_id": ctx.topic_id}})
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(self.app.runtime.jobs.queue.queue[0].recovery_context["previous_status"], "interrupted")
        self.assertNotIn(notice, [c.args[2] for c in self.app.messenger.send_message.call_args_list])

    def test_a_stop_the_user_asked_for_keeps_its_unsent_notice(self):
        from covey.app.outbox import retry_label
        _, notice, record = self.stopped_while_sending(cancel_requested_by="card")
        self.assertEqual((record["status"], record["final_text"], retry_label(record)),
                         ("interrupted", notice, "重新投递结果"))

    def test_retry_button_rejects_different_topic(self):
        self.receive("om_a", "one")
        self.receive("om_b", "two")
        before = self.app.runtime.jobs.queue.qsize()
        self.app.controls.task_action(
            self.topic("om_a"),
            {
                "chat_id": "oc_chat",
                "action_value": {"kind": "bridge_job_retry", "job_id": 1, "topic_id": self.topic("om_b").topic_id},
            },
        )
        self.assertEqual(before, self.app.runtime.jobs.queue.qsize())

    def test_recovery_card_contains_checkpoint_and_action(self):
        path = self.root / "checkpoint.json"
        path.write_text(json.dumps({"phase": "训练", "completed": ["数据准备"], "next_step": "查询 train-1"}))
        card = recovery_card({"job_id": 7, "topic_id": "topic", "checkpoint_path": str(path)})
        # The task card's row, why in bold, then where it stood (T666: no band over them).
        head, lead, body, buttons = card["body"]["elements"][:4]
        self.assertEqual((head["element_id"], lead["content"].split("\n")[0]), ("task_head", "**这一轮被打断了**"))
        self.assertEqual(body["content"], "停在：训练\n做完的：数据准备\n下一步：查询 train-1")
        button = buttons["columns"][0]["elements"][0]
        self.assertEqual(button["value"]["job_id"], 7)
        for continued in (False, True):
            view = recovery_card({"job_id": 7, "topic_id": "topic"}, continued=continued)
            self.assertNotIn("bridge_browse", json.dumps(view))  # retired history browser

    def test_recovery_keeps_trace_and_frozen_duration(self):
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.started_at = 1000
        trace.note('note', '已核对文件')
        record = {'job_id':7,'topic_id':'topic','trace_snapshot':trace.snapshot(),'elapsed_seconds':42}
        for continued in (False,True):
            card = recovery_card(record,continued=continued)
            panel = next(e for e in card['body']['elements'] if e.get('element_id')=='process_panel')
            # In the title of 执行过程 (since 2026-11-04 the figures line is inside it).
            self.assertIn('用时 42秒',next(e for e in walk_dicts(panel) if e.get('element_id')=='trace_details')['header']['title']['content'])
            self.assertIn('已核对文件',str(panel))
            self.assertNotIn('history_actions',str(card))
        # Moved on to an automatic retry it keeps what it did (T666); with no step, no 无执行记录 panel: its time on the row.
        moved = recovery_card(record, moved_to=8, automatic='rate_limited')
        self.assertIn('已核对文件', str(next(e for e in moved['body']['elements'] if e.get('element_id') == 'process_panel')))
        empty = {**record, 'trace_snapshot': ToolTrace().snapshot(), 'status': 'interrupted'}
        bare = recovery_card(empty)
        self.assertFalse([e for e in bare['body']['elements'] if e.get('element_id') == 'process_panel'])
        self.assertIn('停住了</text_tag> <font color=\'grey\'>用时 42秒</font>', json.dumps(bare['body']['elements'][0], ensure_ascii=False))
        self.assertFalse([e for e in recovery_card({**empty}, moved_to=8)['body']['elements'] if e.get('element_id') == 'process_panel'])

    def test_followup_clears_waiting_state_before_execution(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.unregister_reply_job(job)
        ctx = self.topic("om_a")
        cp = self.root / "checkpoint.json"
        cp.write_text(json.dumps({"requires_user": True, "question": "哪个数据集？"}))
        job.checkpoint_path = str(cp)
        self.app.record_job_state(ctx, job, "waiting_user", "wait", final_text="哪个数据集？")
        with mock.patch.object(self.app.cards, "replace_record_card", return_value=True) as update:
            self.receive("om_a2", "数据集 A", root="om_a")
        update.assert_not_called()  # U1: the asking task's card keeps what it said; this one has no card to paint.
        records = self.app.harness.topic_records(ctx, "oc_chat")
        self.assertTrue(any(r["status"] == "continued" for r in records))
        queued = next(r for r in records if r["status"] == "queued")
        self.assertEqual(queued["recovery_context"]["question"], "哪个数据集？")


class TurnBodyTest(unittest.TestCase):
    """The task card's body once a turn's reply was sent apart from it: ↓ and the reply's lead (P1)."""

    def body(self, status, text, **kwargs):
        return self.finish(status, text, **kwargs)[0].update_job_streaming_reply.call_args.args[2]

    def finish(self, status, text, *, proxy=False, question=False, topic_label="", taken=True, timed=""):
        """(cards, job) once the turn's card was finished."""
        from covey.app.cards import Cards
        from covey.app.turn_delivery import TurnDelivery
        cards = mock.Mock()
        cards.render_stream_final = lambda job, note, **kwargs: note
        # The preview is computed in Cards alone (rule 3): the real method, on this mock.
        cards.set_lead_summary.side_effect = lambda job, status: Cards.set_lead_summary(
            SimpleNamespace(summary_label=Cards.summary_label), job, status)
        cards.update_job_streaming_reply.return_value = taken
        reply = SimpleNamespace(text=text) if text is not None else None
        turn = SimpleNamespace(status=status, verified_delivery=reply, verified_outcome=SimpleNamespace(notice=lambda: ""))
        job = SimpleNamespace(progress=SimpleNamespace(summary="", timed_outcome=timed), job_id=7, topic_label=topic_label)
        delivery = TurnDelivery(job, SimpleNamespace(), turn, prefix="p", cards=cards, runtime=mock.Mock(),
                                artifacts=None, questions=None, steering=None, record_job_state=mock.Mock(),
                                send_text=mock.Mock())
        with mock.patch("covey.app.turn_delivery.close_streaming_reply_async"), \
                mock.patch("covey.app.turn_delivery.proxy_topic", return_value=proxy):
            delivery.finish_card(True, status, {}, reply is not None, question)
        return cards, job

    def test_a_dashboard_card_has_its_lead_under_a_bold_conclusion(self):
        # The A 仪表盘 sample: 「结论」 over the lead, which is under the status line and the tiles already.
        from covey.app.turn_delivery import TurnDelivery
        turn = SimpleNamespace(status="done", verified_delivery=SimpleNamespace(text="## 迁移有两种做法\n先说第一种。"),
                               verified_outcome=SimpleNamespace(notice=lambda: ""))
        job = SimpleNamespace(progress=SimpleNamespace(dashboard=True), job_id=7)
        delivery = TurnDelivery(job, SimpleNamespace(), turn, prefix="p", cards=mock.Mock(), runtime=mock.Mock(),
                                artifacts=None, questions=None, steering=None, record_job_state=mock.Mock(),
                                send_text=mock.Mock())
        with mock.patch("covey.app.turn_delivery.proxy_topic", return_value=False):
            self.assertEqual(delivery.delivered_note(), ("**结论**\n迁移有两种做法", "迁移有两种做法"))

    def test_a_turn_that_asked_leads_with_what_it_said_too(self):
        # Repo review U1: 163 of 328 turns in the owner's chats asked, and their cards read only
        # "正文和问题控件已送达，请按问题卡片提示回答。", then "已收到回答 · …结果见下方任务卡。".
        self.assertEqual(self.body("waiting_user", "## 迁移有两种做法\n先说第一种。"), "**↓ 迁移有两种做法**")
        self.assertEqual(self.body("done", "## 迁移有两种做法"), "**↓ 迁移有两种做法**")
        # A native question without words of its own: the fixed sentence, true once it is answered too.
        self.assertEqual(self.body("waiting_user", None, question=True), "正文和问题已单独发送在下方。")
        self.assertEqual(self.body("done", "x" * 30000), "回答已单独发送在下方。")  # Sent as a file.

    def test_a_proxy_turn_that_sends_nothing_says_so_in_words(self):
        # Repo review U5: the card read "↓ [不回复]" and "↓ [无需回复]".
        self.assertEqual(self.body("done", "[不回复]\n这事要你定。\n建议回复：我看下", proxy=True),
                         "↓ 不代你回复：原因见下方这一轮的回答")
        self.assertEqual(self.body("done", "[无需回复] 对方只是说好的", proxy=True), "↓ 不用回：原因见下方这一轮的回答")
        self.assertEqual(self.body("done", "好的，明天给你", proxy=True), "**↓ 好的，明天给你**")
        self.assertNotIn("不代你回复", self.body("done", "[不回复] 不是代答话题"))  # Only in a 代答 topic.

    def test_the_lead_moves_to_the_top_and_names_the_card_in_the_chat_list(self):
        # T288: the lead was under 本轮请求 and 任务详情, and the chat list showed only the question.
        # The list names the outcome and time: the answer card sent beside it has the lead, which read twice under the
        # topic in the chat (2026-11-02 review).
        cards, job = self.finish("done", "## 界面验收结果（只读）\n细节……", topic_label="T298",
                                 timed="有结果 · 用时 3 分 39 秒")
        cards.lead_first.assert_called_once()
        cards.set_lead_summary.assert_called_once_with(job, "done")  # Computed in Cards alone (rule 3).
        self.assertEqual(job.progress.summary, "T298 有结果 · 用时 3 分 39 秒")
        cards, job = self.finish("waiting_user", "先确认*数据集*。还有别的。")
        self.assertEqual(cards.update_job_streaming_reply.call_args.args[2], "**↓ 先确认\\*数据集\\***")
        self.assertEqual(job.progress.summary, "#7 等你")
        # A fixed sentence is not a lead: the card keeps its order and the list its preview.
        for status, text, kwargs in [("waiting_user", None, {"question": True}), ("done", "x" * 30000, {}),
                                     ("done", "[不回复]\n这事要你定。", {"proxy": True})]:
            cards, job = self.finish(status, text, **kwargs)
            cards.lead_first.assert_not_called()
            self.assertEqual(job.progress.summary, "")
        # A body the card did not take is not moved; the list still says what came of the turn.
        cards, job = self.finish("done", "## 好了", taken=False)
        cards.lead_first.assert_not_called()
        self.assertEqual(job.progress.summary, "#7 有结果")


class RedeliveryKeyTest(unittest.TestCase):
    """重新投递结果 on a result that is not a verified reply (a failure text) whose send timed out."""

    def setUp(self):
        from support import make_context
        from support.world import build_claude_app
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = build_claude_app()
        self.addCleanup(self.app.runtime.shutdown.set)
        self.ctx = make_context(Path(tmp.name), topic_id="topic-1")
        upsert_job_record(self.ctx.state_dir, "job-k", job_id=5, chat_id="chat", topic_id="topic-1", backend="claude",
                          harness_id="claude", kind="chat", status="failed", execution_status="failed",
                          final_text="⚠️ 后端调用失败：partial text", delivery_ok=False,
                          delivery_error="network_timeout", message_id="job-k")

    def clicks(self, *events, ambiguous=False):
        from covey.feishu.transport import Failure, set_failure
        keys, files = [], []

        def reply(ctx, chat, text, key, **kwargs):
            keys.append(key)
            if ambiguous:
                set_failure(Failure("network_timeout", True, True))  # Possibly delivered.
            return False
        with mock.patch.object(self.app.controls.messenger, "reply", side_effect=reply), \
                mock.patch.object(self.app.controls.artifacts, "send_result_file",
                                  side_effect=lambda *a: files.append(a) or False):
            said = [self.app.controls.retry_topic(self.ctx, "chat", event, job_id=5) for event in events]
        return keys, files, said

    def test_every_click_sends_the_same_message_under_one_key(self):
        from covey.app.outbox import REDELIVER_LABEL, retry_label
        self.assertEqual(retry_label(find_job_record(self.ctx.state_dir, "job-k")), REDELIVER_LABEL)
        keys, _, said = self.clicks("ev-click-1", "ev-click-2")
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 1, keys)
        self.assertEqual(said, ["结果仍未能发送，请稍后重试。"] * 2)

    def test_a_send_that_may_have_landed_is_not_sent_again_as_a_file_and_says_so(self):
        from covey.app.redelivery import REDELIVERY_UNCONFIRMED
        keys, files, said = self.clicks("ev-click-1", ambiguous=True)
        self.assertEqual((len(keys), files, said), (1, [], [REDELIVERY_UNCONFIRMED]))


class QuestionDeliveryTest(unittest.TestCase):
    def test_question_once_in_final_card_without_extra_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            job = make_reply_job("chat", root=root, with_progress=True)

            def generate(ctx, job, session, progress):
                Path(job.checkpoint_path).write_text(json.dumps({"requires_user": True, "question": "用哪个数据集？"}))
                return RunResult("用哪个数据集？", None)

            harness.app.runs.generate_reply_for_worker = generate
            harness.run(job)
            self.assertFalse(harness.sent)
            self.assertEqual(harness.card_updates[-1].count("用哪个数据集？"), 1)
