from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import make_job
from support.fake_lark import FakeCardKit
from support.final_delivery import FinalDeliveryHarness, make_reply_job
from support.git import commit_all, init_git_repo
from covey.core.harness import RunResult
from covey.cards.ui import state_word
from covey.cards.review import render_stream_final, render_stream_progress_lines
from covey.app.task_card import StreamingCard
from covey.cards.streaming import STOP_ACTION_ELEMENT_ID, build_stream_card, task_details_panel
from covey.app.worker import format_received_progress_event


class ChatFinalDeliveryTest(unittest.TestCase):
    """Regression: a review job must send its full final text exactly once."""

    def test_progress_event_records_received_time_without_delta(self) -> None:
        first = format_received_progress_event("我开始读实际 diff", 1000.0)

        self.assertIn("我开始读实际 diff", first)
        self.assertNotIn("距上次", first)

    def test_chat_final_card_omits_inline_progress_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = make_reply_job("chat", root=root, with_progress=False)
            rendered = render_stream_final(job, "任务 #1:\n\n最终完成")

            self.assertNotIn("执行进度：", rendered)
            self.assertNotIn("07-06 01:12:00：我开始读实际 diff", rendered)
            self.assertNotIn("07-06 01:12:04：看到新卡片缓存", rendered)
            self.assertIn("**答案**", rendered)
            self.assertNotIn("✅ 最终答案", rendered)
            self.assertNotIn("该session最近历史问题", rendered)
            self.assertNotIn("**最近历史问题（最多3个）**", rendered)
            self.assertIn("最终完成", rendered)

    def test_failed_card_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = make_reply_job("chat", root=Path(tmp), with_progress=False)
            rendered = render_stream_final(job, "调用 Codex 失败", status="failed")
            self.assertIn("停住了", rendered)
            self.assertNotIn("✅", rendered)
            self.assertNotIn("已完成", rendered)
            self.assertNotIn("有结果", rendered)

    def test_progress_lines_do_not_drop_history_by_fixed_count(self) -> None:
        events = [f"07-06 01:12:{idx:02d}：进度 {idx}" for idx in range(40)]
        lines = render_stream_progress_lines(events, max_chars=10000)
        text = "\n".join(lines)

        self.assertIn("进度 0", text)
        self.assertIn("进度 39", text)
        self.assertNotIn("已折叠中间", text)

    def test_progress_lines_fold_middle_when_character_budget_is_exceeded(self) -> None:
        events = [f"07-06 01:12:{idx:02d}：{'x' * 80}" for idx in range(40)]
        lines = render_stream_progress_lines(events, max_chars=900)
        text = "\n".join(lines)

        self.assertIn("07-06 01:12:00", text)
        self.assertIn("07-06 01:12:39", text)
        self.assertIn("已折叠中间", text)

    def test_stream_card_prints_updates_in_one_large_step(self) -> None:
        runtime = StreamingCard(
            idempotency_prefix="unit",
            default_title="Unit",
            element_id="answer",
            progress_factory=lambda **kwargs: kwargs,
            log=lambda _message: None,
            safe_idempotency_key=lambda value: value,
            card_limit=10000,
            update_interval=0.0,
            replacement_limit=0,
            send_text_message=lambda *_args: True,
            compact_text=lambda text, _limit: str(text),
            messenger=None,
            send_card=lambda *_args, **_kwargs: (None, False),
        )
        card = build_stream_card("处理中", "question", runtime)

        config = card["config"]["streaming_config"]
        self.assertEqual(config["print_frequency_ms"]["default"], 10)
        self.assertEqual(config["print_step"]["default"], 10000)

    def test_stream_card_omits_process_panel_until_final(self) -> None:
        runtime = StreamingCard(
            idempotency_prefix="unit",
            default_title="Unit",
            element_id="answer",
            progress_factory=lambda **kwargs: kwargs,
            log=lambda _message: None,
            safe_idempotency_key=lambda value: value,
            card_limit=10000,
            update_interval=0.0,
            replacement_limit=0,
            send_text_message=lambda *_args: True,
            compact_text=lambda text, _limit: str(text),
            messenger=None,
            send_card=lambda *_args, **_kwargs: (None, False),
        )
        card = build_stream_card("处理中", "question", runtime)

        self.assertEqual(len(card["body"]["elements"]), 1)
        self.assertEqual(card["body"]["elements"][0]["element_id"], "answer")

    def test_stream_card_includes_stop_button_for_running_chat_job(self) -> None:
        runtime = StreamingCard(
            idempotency_prefix="unit",
            default_title="Unit",
            element_id="answer",
            progress_factory=lambda **kwargs: kwargs,
            log=lambda _message: None,
            safe_idempotency_key=lambda value: value,
            card_limit=10000,
            update_interval=0.0,
            replacement_limit=0,
            send_text_message=lambda *_args: True,
            compact_text=lambda text, _limit: str(text),
            messenger=None,
            send_card=lambda *_args, **_kwargs: (None, False),
        )
        job = SimpleNamespace(job_id=7, message_id="om_job", kind="chat", user_text="question")
        card = build_stream_card("处理中", "question", runtime, job=job, include_stop_action=True)

        elements = card["body"]["elements"]
        # The status line at the top, the 输入/输出 box under it with 输出 by the tiles' old ID (the "E" sample,
        # 2026-11-08), the buttons at the bottom (the A 仪表盘 sample, 2026-11-04), in a row that wraps.
        self.assertEqual([e["element_id"] for e in elements[:3]], ["activity", "activity_more", "task_io"])
        self.assertEqual(elements[2]["columns"][0]["elements"][1]["columns"][1]["elements"][0]["element_id"],
                         "task_figures")
        row = elements[-1]
        self.assertEqual(row["element_id"], "action_row")
        self.assertEqual(row["flex_mode"], "flow")
        self.assertTrue(all(column["width"] == "auto" for column in row["columns"]))
        button = row["columns"][-1]["elements"][0]  # Ending the task is the last, separate action.
        self.assertEqual(button["element_id"], STOP_ACTION_ELEMENT_ID)
        self.assertEqual(button["text"]["content"], "停止")
        # Bordered in red and medium (the owner, 2026-11-04 14:05: A 中号线框). No confirm: on a running card,
        # redrawn every few seconds, the client dropped 确定 (gen 257's real 停止 never reached the Bridge).
        self.assertEqual((button["type"], button["size"]), ("danger", "medium"))
        self.assertNotIn("confirm", button)
        self.assertEqual(row["horizontal_spacing"], "8px")
        self.assertEqual(button["value"]["kind"], "bridge_job_stop")
        self.assertEqual(button["value"]["job_id"], "7")

    def test_the_other_cards_of_a_topic_have_the_task_card_s_bordered_medium_buttons(self) -> None:
        # The owner, 2026-11-04 14:05 (A 中号线框): the topic's cards share one button look, the menu panels keep theirs.
        from support.cards import walk_dicts
        from covey.cards.held_note import note_card
        from covey.cards.recovery import result_actions
        from covey.cards.rewind import rewind_button
        from covey.cards.steer_receipts import receipt_card
        from covey.cards.turn_export import export_panel
        from covey.cards.elements import text_button

        record = {"status": "held", "topic_id": "t", "token": "x", "job_id": 1, "text": "补一句", "waiting": []}
        cards = {"download": export_panel("t", 1), "rewind": rewind_button("t", 1, "claude", 2),
                 "receipt": receipt_card(record), "held note": note_card(record),
                 "retry": result_actions("t", 1, "检查并继续", stop_kind="k")}
        for name, element in cards.items():
            with self.subTest(name):
                buttons = [e for e in walk_dicts(element) if e.get("tag") == "button"]
                self.assertTrue(buttons)
                self.assertEqual({b["size"] for b in buttons}, {"medium"})
                self.assertTrue({b["type"] for b in buttons} <= {"default", "primary"})
        rows = [e for e in walk_dicts(cards["receipt"]) if e.get("tag") == "column_set" and e.get("columns")]
        self.assertEqual((rows[-1]["flex_mode"], rows[-1]["horizontal_spacing"]), ("flow", "8px"))
        self.assertEqual((text_button("移除", {})["type"], text_button("移除", {})["size"]), ("text", "small"))

    def test_the_title_bar_says_whether_a_redacting_harness_redacts(self) -> None:
        # The owner, 2026-11-04 17:38: 这个卡片也看不出是否开启敏感过滤.
        from covey.cards.elements import redaction_tags
        self.assertEqual(redaction_tags(True), [("敏感词脱敏：开", "green")])
        self.assertEqual(redaction_tags(False), [("敏感词脱敏：关", "neutral")])
        self.assertEqual(redaction_tags(None), [])  # Another Harness, or a job from before.
        runtime = StreamingCard(idempotency_prefix="unit", default_title="Unit", element_id="answer",
                                progress_factory=lambda **kwargs: kwargs, log=lambda _message: None,
                                safe_idempotency_key=lambda value: value, card_limit=10000, update_interval=0.0,
                                replacement_limit=0, send_text_message=lambda *_args: True,
                                compact_text=lambda text, _limit: str(text), messenger=None,
                                send_card=lambda *_args, **_kwargs: (None, False))
        job = make_job(job_id=7, model="opus", effort="max")
        job.redaction = False
        card = build_stream_card("", "统计行数", runtime, job=job)
        # Since the 输入/输出 box (the "E" sample, 2026-11-08) its 输入 says it, not a tag the side panel cut.
        self.assertNotIn("text_tag_list", card["header"])
        box = next(e for e in card["body"]["elements"] if e.get("element_id") == "task_io")
        self.assertIn("脱敏关", box["columns"][0]["elements"][0]["columns"][1]["elements"][0]["content"].replace("\u2060", ""))

    def test_the_task_card_has_the_dashboard_look_with_the_ids_it_always_had(self) -> None:
        # The A 仪表盘 sample the owner picked (2026-11-04).
        from covey.cards.task_status import status_markdown
        runtime = StreamingCard(idempotency_prefix="unit", default_title="Unit", element_id="answer",
                                progress_factory=lambda **kwargs: kwargs, log=lambda _message: None,
                                safe_idempotency_key=lambda value: value, card_limit=10000, update_interval=0.0,
                                replacement_limit=0, send_text_message=lambda *_args: True,
                                compact_text=lambda text, _limit: str(text), messenger=None,
                                send_card=lambda *_args, **_kwargs: (None, False))
        job = SimpleNamespace(job_id=7, message_id="om_job", kind="chat", user_text="统计行数", topic_id="t1",
                              topic_label="T372", model_label="Claude · Opus 5", effort="max",
                              workspace_label="algo-agent-devskills", harness_id="claude", binding_revision=2,
                              harness_capabilities=SimpleNamespace(steering=True))
        card = build_stream_card("", "统计行数", runtime, job=job, include_stop_action=True)
        header = card["header"]
        self.assertEqual((header["template"], header["icon"]["token"], header["icon"]["color"]), ("default", "robot_outlined", "blue"))
        self.assertEqual(header["title"]["content"], "T372 · #7 · 统计行数")
        # The title bar is the T number, task and title alone (the "E" sample, 2026-11-08: a subtitle was cut in a
        # topic's side panel); what the task runs with is the box's 输入.
        self.assertEqual(set(header), {"title", "template", "icon", "padding"})
        ids = [e.get("element_id") for e in card["body"]["elements"]]
        # No 任务详情 while it has nothing to say (2026-11-09): the box has the figures.
        self.assertEqual(ids, ["activity", "activity_more", "task_io", "answer", "current_question", "action_row"])
        # The status line, the rest of it under it (empty till there is some), then the grey box: 输入 in three
        # lines (its hyphens no-break ones), 输出 by the tiles' old ID ("—" till the run has figures).
        self.assertEqual(card["body"]["elements"][0]["content"], "## 等待处理")
        self.assertEqual((card["body"]["elements"][1]["content"], card["body"]["elements"][1]["text_size"]), ("", "normal_v2"))
        box = card["body"]["elements"][2]["columns"][0]
        self.assertEqual(box["background_style"], "grey-50")
        texts = [row["columns"][1]["elements"][0] for row in box["elements"]]
        self.assertEqual([row["columns"][0]["elements"][0]["content"] for row in box["elements"]],
                         ["<font color='grey'>输入</font>", "<font color='grey'>输出</font>"])
        shown = texts[0]["content"].replace("\u2060", "").replace("\u00a0", " ")
        # One line, its parts apart by a grey bar (the owner, 2026-11-09 13:48).
        self.assertTrue(shown.startswith("<font color='grey'>") and "\n" not in shown, shown)
        self.assertEqual(re.sub(r"<[^>]*>", "", shown), "— ｜ — ｜ Opus 5 ｜ 思考 max ｜ 脱敏关 ｜ algo\u2011agent\u2011devskills")
        self.assertEqual((texts[1]["element_id"], texts[1]["content"]), ("task_figures", "<font color='grey'>—</font>"))
        for panel in card["body"]["elements"][4:5]:  # White, in a thin grey border (the "E 白底细边框" pick).
            self.assertNotIn("background_color", panel)
            self.assertEqual(panel["border"], {"color": "grey", "corner_radius": "8px"})
        row = card["body"]["elements"][-1]
        buttons = [column["elements"][0] for column in row["columns"]]
        # 💬 侧聊 sat between 补充 and 换配置 until 2026-11-08.
        self.assertEqual([b["element_id"] for b in buttons], ["steer_action", "running_switch", "job_stop_action"])
        self.assertEqual([(b["type"], b["size"]) for b in buttons],
                         [("primary", "medium"), ("default", "medium"), ("danger", "medium")])
        self.assertFalse([b for b in buttons if "confirm" in b])
        self.assertEqual(buttons[0]["value"], {"kind": "bridge_steer", "topic_id": "t1", "job_id": 7})
        self.assertEqual(buttons[-1]["value"]["kind"], "bridge_job_stop")
        self.assertEqual((row["flex_mode"], {c["width"] for c in row["columns"]}), ("flow", {"auto"}))
        essential = build_stream_card("", "统计行数", runtime, job=job, include_stop_action=True, essential=True)
        self.assertEqual([c["elements"][0]["element_id"] for c in essential["body"]["elements"][-1]["columns"]], ["job_stop_action"])
        # The status line: its status a heading, the details and what runs now small and grey under it (beside the
        # heading they were drawn large and wrapped in the side panel, 2026-11-04).
        self.assertEqual(status_markdown("在做 · 已运行 48 秒\n现在：执行命令"),
                         f"## {state_word('running')}\n<font color='grey'>已运行 48 秒</font>\n<font color='grey'>现在：执行命令</font>")
        self.assertEqual(status_markdown(status_markdown("有结果")), f"## {state_word('result')}")
        self.assertEqual(status_markdown(""), " ")

    def test_a_status_line_splits_into_the_status_beside_the_tiles_and_the_rest_under_them(self) -> None:
        # The "A 一行仪表" pick: the status with how long it ran or when it ended beside the tiles, the rest as wide
        # as the card (the owner, 2026-11-04 20:21: 执行中下面的文案感觉占的面积比较小，右侧留白比较多).
        from covey.app.cards import STOPPING
        from covey.cards.task_status import status_elements, status_rows
        self.assertEqual(status_rows("在做 · 已运行 6 分 19 秒 · 阶段：合入主干\n现在：模型思考或生成回复中"),
                         ("在做 · 已运行 6 分 19 秒", "阶段：合入主干\n现在：模型思考或生成回复中"))
        self.assertEqual(status_rows("有结果 · 20:01 结束"), ("有结果 · 20:01 结束", ""))
        self.assertEqual(status_rows(STOPPING), ("正在停止…", "已收到停止请求，正在中断后台任务。"))
        self.assertEqual(status_rows("在做 · 已运行 2 分 05 秒 · 模型请求重试第 3 次\n本次模型请求尚未成功。"),
                         ("在做 · 已运行 2 分 05 秒", "模型请求重试第 3 次\n本次模型请求尚未成功。"))
        for whole in ("等待处理", "↪ 已回答 → #2806", f"## {state_word('result')}", ""):
            self.assertEqual(status_rows(whole), (whole, ""))
        top, more = status_elements("在做 · 已运行 5 秒 · 阶段：读代码\n现在：执行命令", True)
        self.assertEqual((top["element_id"], top["content"]),
                         ("activity", f"## {state_word('running')}\n<font color='grey'>已运行 5 秒</font>"))
        self.assertEqual((more["element_id"], more["content"]), ("activity_more", "阶段：读代码\n<font color='grey'>现在：执行命令</font>"))
        self.assertEqual(status_elements("有结果 · 20:01 结束", True)[1]["content"], "")
        # A card sent before: the whole line in its one element, as it always was.
        self.assertEqual([e["element_id"] for e in status_elements(STOPPING, False)], ["activity"])

    def test_task_details_does_not_repeat_the_model_and_budget(self) -> None:
        # 任务详情 said 模型 and 思考预算 the card says elsewhere (2026-11-04 15:07: 冗余的文字有点多).
        job = make_job(job_id=7, model="gpt56_sol_reasoning_xhigh", effort="")
        job.model_label, job.effort = "Codex · GPT-6-Astra", "max"
        self.assertIsNone(task_details_panel(job))  # Nothing to say (2026-11-09): none of 模型, 思考预算, 任务编号, 超时.
        job.timeout = 600
        self.assertIn("超时：600s", task_details_panel(job)["elements"][0]["content"])  # Only when one is set.

    def test_job_buttons_are_removed_once(self) -> None:
        from support.world import build_claude_app
        cards = build_claude_app().cards
        job = make_job(message_id="om_job", topic_id="topic",
                              progress=SimpleNamespace(card_id="card-once", sequence=1, message_id="om_job"))
        with FakeCardKit() as lark:
            # Stopping removes the row; the job's end then must not delete it again.
            self.assertTrue(cards.delete_stop_action(SimpleNamespace(profile="unit"), job))
            self.assertTrue(cards.delete_stop_action(SimpleNamespace(profile="unit"), job))
        self.assertEqual(len(lark.calls), 1)
        self.assertTrue(lark.last.path.endswith("/elements/action_row"))



    def test_chat_overflow_still_sends_full_result_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=True)
            job = make_reply_job("chat", root=root, with_progress=True)
            harness.run(job)

            self.assertEqual(len(harness.sent), 1)
            self.assertIn("-result-full-", harness.sent[0][0])

    def test_a_result_the_card_cut_to_its_size_is_sent_in_full_too(self) -> None:
        # Under the character limit, but over the card's size (#1150): the card shows its head only.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False, cut=True)
            harness.run(make_reply_job("chat", root=root, with_progress=True))
            self.assertEqual(len(harness.sent), 1)
            self.assertIn("-result-full-", harness.sent[0][0])

    def test_chat_final_updates_before_closing_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            job = make_reply_job("chat", root=root, with_progress=True)
            harness.run(job)

            final_update_index = harness.card_events.index(("update", "unit-final"))
            self.assertGreater(harness.card_events.index(("close", "")), final_update_index)
            self.assertIn(("unit-final", False), harness.allow_replacements)
            self.assertTrue(harness.close_finished.wait(timeout=1))
            self.assertLess(harness.card_events.index(("trace", "")), harness.card_events.index(("close", "")))

    def outcomes(self, harness):
        return [content for kind, content in harness.card_events if kind == "outcome"]

    def test_the_outcome_is_at_the_top_before_the_answer_is_drawn(self) -> None:
        # S26 client run: the status line went blank about a second before the answer appeared lower down.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            harness.run(make_reply_job("chat", root=root, with_progress=True))
            self.assertEqual(self.outcomes(harness), ["有结果"])
            self.assertLess(harness.card_events.index(("outcome", "有结果")),
                            harness.card_events.index(("update", "unit-final")))

    def test_a_stopped_run_shows_stopped_at_the_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            job = make_reply_job("chat", root=root, with_progress=True)
            job.cancel_event.set()  # Stopped before it started: the top said 正在停止… until now.
            harness.run(job)
            self.assertEqual(self.outcomes(harness), ["已停止"])

    def test_a_backend_with_structured_events_has_a_trace_and_a_timer_from_the_start(self) -> None:
        # S28 #849 (MASO): until its first event the top said only "MASO 已开始处理", with no 已运行.
        from covey.core.harness import HarnessCapabilities
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            harness.app.harness.backend = SimpleNamespace(
                capabilities=lambda _ctx: HarnessCapabilities())
            lines, traced = [], []
            harness.app.cards.update_card_activity = lambda _ctx, _job, content: lines.append(content)

            def generate_reply(_ctx, job, _session_id, progress):
                progress("MASO 已开始处理")
                traced.append(job.tool_trace is not None)
                if job.tool_trace is not None:  # Its steps that write this file read 更新检查点 (S30).
                    self.assertEqual(job.tool_trace.checkpoint, job.checkpoint_path)
                job.activity_monitor.last_ui = float("-inf")  # Not held back by the 2 s card refresh.
                job.activity_monitor.tick()
                return RunResult("最终回复", None)

            harness.app.runs.generate_reply_for_worker = generate_reply
            harness.run(make_reply_job("chat", root=root, with_progress=True))
            # Every turn has a trace, whatever the backend declares (all of them report structured events).
            self.assertEqual(traced, [True])
            self.assertTrue(lines[-1].startswith("在做 · 已运行 "), lines[-1])
            self.assertIn("\n现在：已提交给模型，等待首个输出", lines[-1])

    def test_the_backend_starts_before_any_card_write_and_the_monitor_before_the_git_snapshot(self) -> None:
        # S33: a task runner's first card write held the backend's start for 3 s. S34: 执行中 came a
        # write later (2 s), and the monitor that shows it started after the git snapshot (0.2-0.35 s).
        from unittest import mock
        from covey.app import worker
        from covey.core.harness import HarnessCapabilities
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            harness.app.harness.backend = SimpleNamespace(
                capabilities=lambda _ctx: HarnessCapabilities())
            order, at_start = [], []

            def generate_reply(_ctx, job, _session_id, progress):
                order.append("backend")
                at_start.append((list(harness.card_updates), job.body_clear_pending))
                progress("调用工具")  # Asks for no second clear.
                return RunResult("最终回复", None)
            harness.app.runs.generate_reply_for_worker = generate_reply
            start = worker.TaskMonitor.start
            with mock.patch.object(worker.TaskMonitor, "start", autospec=True,
                                   side_effect=lambda monitor: (order.append("monitor"), start(monitor))[1]), \
                 mock.patch.object(worker, "capture_job_diff_snapshot",
                                   side_effect=lambda _workdir: order.append("snapshot")):
                harness.run(make_reply_job("chat", root=root, with_progress=True))
            self.assertEqual(order, ["monitor", "snapshot", "backend"])
            # The first status line write takes the queued text away (every turn has a trace).
            self.assertEqual(at_start, [([], True)])
            self.assertEqual(harness.card_updates[-1], "final:最终回复")

    def test_a_backend_that_reports_its_model_request_starts_as_starting(self) -> None:
        # S29 #863 (MASO): "已提交给模型" for about 8 s before the SDK's first model call.
        from covey.core.harness import HarnessCapabilities
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            harness.app.harness.spec.prompt_agent_name = "MASO"
            harness.app.harness.backend = SimpleNamespace(
                capabilities=lambda _ctx: HarnessCapabilities(model_requests=True))
            lines = []
            harness.app.cards.update_card_activity = lambda _ctx, _job, content: lines.append(content)

            def generate_reply(_ctx, job, _session_id, _progress):
                for request in (False, True):
                    if request:
                        job.activity_monitor.request()
                    job.activity_monitor.last_ui = float("-inf")
                    job.activity_monitor.tick()
                return RunResult("最终回复", None)

            harness.app.runs.generate_reply_for_worker = generate_reply
            harness.run(make_reply_job("chat", root=root, with_progress=True))
            self.assertIn("\n现在：正在启动 MASO…", lines[-2])
            self.assertIn("\n现在：已提交给模型，等待首个输出", lines[-1])

    def test_the_final_trace_knows_whether_the_task_was_stopped(self) -> None:
        from covey.core.harness import HarnessCapabilities, JobCancelled
        for stop in (True, False):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
                harness.app.harness.backend = SimpleNamespace(
                    capabilities=lambda _ctx: HarnessCapabilities())
                calls = []
                harness.app.cards.finalize_trace = lambda _ctx, _job, **kwargs: calls.append(kwargs) or True

                def generate_reply(*_args):
                    if stop:
                        raise JobCancelled("任务已停止")
                    return RunResult("最终回复", None)

                harness.app.runs.generate_reply_for_worker = generate_reply
                harness.run(make_reply_job("chat", root=root, with_progress=True))
                self.assertEqual(calls, [{"stopped": stop}])

    def test_a_stopped_task_says_its_records_stay_and_nothing_more(self) -> None:
        # Repo review U10: P1 took "直接回复本话题即可继续" off the finished card; the stopped card still said it.
        from covey.core.harness import JobCancelled
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)
            states = []
            harness.app.record_job_state = lambda _ctx, _job, status, _message, **fields: states.append((status, fields))

            def stopped(*_args):
                raise JobCancelled("任务已停止")
            harness.app.runs.generate_reply_for_worker = stopped
            job = make_reply_job("chat", root=root, with_progress=True)
            harness.run(job)
            said = [fields["final_text"] for status, fields in states if "final_text" in fields]
            self.assertEqual(said[-1], f"任务 #{job.job_id} 已停止。已保存的结果和记录保留在本话题。")

    def test_a_failed_delivery_corrects_the_outcome_before_the_card_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=False, overflow=False)
            harness.app.messenger = SimpleNamespace(reply=lambda *_args, **_kwargs: False)
            harness.run(make_reply_job("chat", root=root, with_progress=True))
            self.assertTrue(harness.close_finished.wait(timeout=1))
            self.assertEqual(self.outcomes(harness), ["有结果", "停住了"])
            self.assertLess(harness.card_events.index(("outcome", "停住了")), harness.card_events.index(("close", "")))
            # The top first: the failure label drawn after it need not say 停住了 again (cards.render_stream_final).
            self.assertLess(harness.card_events.index(("outcome", "停住了")),
                            harness.card_events.index(("update", "unit-delivery-failed")))

    def test_cancelled_job_stops_before_generating_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)

            def generate_reply(*_args):
                raise AssertionError("cancelled job must not call backend")

            harness.app.runs.generate_reply_for_worker = generate_reply
            job = make_reply_job("chat", root=root, with_progress=True)
            job.cancel_event.set()
            harness.run(job)

            self.assertTrue(harness.card_updates)
            self.assertIn("任务 #1 已停止", harness.card_updates[-1])
            self.assertEqual(harness.sent, [])

    def test_cancelled_job_does_not_deliver_reply_returned_during_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)

            def generate_reply(_ctx, job, _session_id, _progress):
                job.cancel_event.set()
                return RunResult("旧答案不应展示", "sess_new")

            harness.app.runs.generate_reply_for_worker = generate_reply
            job = make_reply_job("chat", root=root, with_progress=True)
            harness.run(job)

            self.assertTrue(harness.card_updates)
            self.assertIn("任务 #1 已停止", harness.card_updates[-1])
            self.assertNotIn("旧答案不应展示", harness.card_updates[-1])

    def test_chat_final_includes_workspace_diff_summary_without_diff_panels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            commit_all(root)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False)

            def generate_reply(_ctx, _job, _session_id, progress) -> RunResult:
                assert progress is not None
                progress("我会修改 app.py", False)
                (root / "app.py").write_text("new\n", encoding="utf-8")
                return RunResult("已修改 app.py", None)

            harness.app.runs.generate_reply_for_worker = generate_reply
            job = make_reply_job("chat", root=root, with_progress=True)
            harness.run(job)

            self.assertTrue(harness.card_updates)
            final_update = harness.card_updates[-1]
            self.assertIn("本次修改 Diff", final_update)
            self.assertIn("M app.py", final_update)
            self.assertNotIn("```diff", final_update)
            self.assertTrue(Path(job.diff_artifact_path).exists())

    def test_chat_final_does_not_block_on_streaming_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=True, overflow=False, close_delay=0.2)
            job = make_reply_job("chat", root=root, with_progress=True)
            started = time.monotonic()
            harness.run(job)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.15)
            self.assertIn(("update", "unit-final"), harness.card_events)
            self.assertNotIn(("close", ""), harness.card_events)
            self.assertTrue(harness.close_finished.wait(timeout=1))


    def test_chat_without_card_sends_plain_result_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = FinalDeliveryHarness(root, card_update_ok=False, overflow=False)
            job = make_reply_job("chat", root=root, with_progress=False)
            harness.run(job)

            self.assertEqual(len(harness.sent), 1)
            self.assertIn("unit-result-om_msg", harness.sent[0][0])


if __name__ == "__main__":
    unittest.main()


class FailureTraceTest(unittest.TestCase):
    """T480/T481 (2026-11-11): turns failed at their start with only "任务异常（RuntimeError）" in the log."""

    def test_a_failed_turn_logs_where_it_was_raised(self):
        from types import SimpleNamespace
        from covey.app.worker import TRACE_CHARS, failure_trace

        def deep_down():
            raise RuntimeError("dictionary changed size during iteration")
        try:
            deep_down()
        except RuntimeError as exc:
            line = failure_trace(exc, SimpleNamespace(job_id=3780))
        self.assertTrue(line.startswith("job #3780 failed with RuntimeError: Traceback"), line)
        self.assertIn("in deep_down", line)
        self.assertIn("dictionary changed size during iteration", line)
        try:
            raise ValueError("x" * (TRACE_CHARS * 2))
        except ValueError as exc:
            long = failure_trace(exc, SimpleNamespace(job_id=1))
        self.assertLess(len(long), TRACE_CHARS + 100)
        self.assertTrue(long.endswith("x"))
