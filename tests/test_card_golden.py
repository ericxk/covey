"""Card JSON goldens: a fixed set of cards and card parts drawn by the real builders, with the clock frozen and
the time zone fixed, compared byte for byte with tests/golden/cards.json (keys sorted).

Any difference means the JSON the Bridge sends for a card changed, so rule 9's client check applies
(docs/cards.md 卡片的客户端限制). After a deliberate change rewrite the file and commit it with the change:

    GOLDEN_CARDS=update python3 -B -m unittest discover -s tests -p test_card_golden.py

The samples carry only fake open_ids, chat and message IDs and /absolute/... paths.
"""

from __future__ import annotations


import json
import lzma
import re
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # support, also when run as tests.test_card_golden

from support.cards import TOPIC, redaction_facts, sample_job, status_block, stream_runtime  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "cards.json"
PACKED = GOLDEN.with_name("cards.json.xz")  # The public export packs the golden file (scripts/export_core.py).
FROZEN = 1793790000.0  # 2026-11-04 19:00 at UTC+8
ZONE = "CST-8"  # POSIX form: no tz database needed
WORK = Path("/absolute/work")
TOKEN = "0123456789abcdef"


def question_record(status="pending", **fields):
    record = {"token": TOKEN, "status": status, "topic_id": TOPIC, "chat_id": "oc_fake_chat", "job_id": 431,
              "blocking": True, "harness_id": "codex", "binding_revision": 2, "created_at": FROZEN - 60,
              "questions": [{"id": "fmt", "header": "格式", "question": "用哪种格式输出？",
                             "options": [{"label": "表格", "description": "Markdown 表格"},
                                         {"label": "列表", "description": "无序列表"}]},
                            {"id": "note", "header": "补充", "question": "还有别的要求吗？"}],
              "answers": {"fmt": {"answers": ["表格"]}, "note": {"answers": ["无"]}}}
    record.update(fields)
    return record


CHOICES = {
    "codex:gpt-6-astra": {"harness": "codex", "title": "Codex", "provider": "openai", "provider_title": "Codex 自带",
                          "display_name": "GPT-6-Astra", "label": "Codex · GPT-6-Astra",
                          "levels": ["low", "high", "xhigh", "max"]},
    "claude:opus": {"harness": "claude", "title": "Claude", "provider": "anthropic", "provider_title": "Claude 自带",
                    "display_name": "Opus 5.5", "redaction": True, "label": "Claude · Opus 5.5",
                    "levels": ["high", "xhigh", "max"]},
    "maso:preset": {"harness": "maso", "title": "MASO", "provider": "maso", "provider_title": "MASO",
                    "display_name": "Preset", "label": "MASO · Preset", "levels": ["high"],
                    "work_modes": ["focus", "adaptive"]},
    "dsh:ali-deepseek-v4.1-flash": {"harness": "dsh", "title": "DeepSeek Harness", "provider": "acme",
                                    "provider_title": "Acme", "display_name": "DeepSeek V4.1 Flash",
                                    "label": "DeepSeek Harness · flash", "levels": ["low", "high", "max"]},
}


def status_line_sample(quiet, progress, background=()):
    """The task card's status line (core/status_line.py status_text) of a turn running a command, quiet for ``quiet`` seconds, with
    ``progress`` (core/progress.py) fields over a quiet one's, and the ``background`` tasks' descriptions the reply left open."""
    from covey.core.status_line import Facts, status_text
    found = {"phase": "", "plan_done": None, "plan_total": None, "last_activity_at": 1000, "last_activity": "在跑评测",
             "eta_seconds": None, "slow_step": None, "maybe_stuck": False, **progress}
    facts = Facts(elapsed=quiet + 60, quiet=quiet, alive=True, finished=False, phase="跑评测", last_event="", checked="11-05 14:03:09",
                  question="", waiting=False, native_question_active=False,
                  activity=("执行命令 · 跑评测", 1000, None), traced=True, wall=1000 + quiet, stirred=True, retry=None,
                  epoch=1000.0 + quiet, long_mode=True, frame=0, progress=found, stuck_after=1800, background=background)
    return status_text(facts)


def task_cards():
    from covey.cards.review import (answer_card, answer_lead, answer_summary, outcome_line, recent_user_questions,
                                             render_history, render_stream_final, render_stream_status, running_summary,
                                             stopped_sentence, task_card_summary)
    from covey.cards.card_cost import exceeds_stream_card_limit
    from covey.cards.streaming import build_stream_card, card_details_text, task_details_panel, usage_lines
    from covey.cards.task_figures import figure_cells, figures_element
    from covey.cards.task_status import look_element, status_elements

    from covey.cards.answer_card import output_lines, output_parts, running_figures
    from covey.cards.task_figures import io_box, io_lines

    run = stream_runtime()
    snapshot = {"started_at": FROZEN - 100, "ended_at": FROZEN, "input_tokens": 120000, "output_tokens": 3400,
                "cache_read_tokens": 90000, "cache_write_tokens": 1000, "measured_output_tokens": 3000,
                "generation_seconds": 40.0, "ttft_seconds": 1.5, "ttft_source": "stream", "steps": {}}
    running = {"started_at": FROZEN - 73, "input_tokens": 570000, "output_tokens": 9000,
               "measured_output_tokens": 4800, "generation_seconds": 70.0, "context_tokens": 86000,
               "context_window": 1000000, "steps": {}}
    cards = {
        "running": build_stream_card("正在处理", "任务", run, job=sample_job(), include_stop_action=True),
        "running_essential": build_stream_card("正在处理", "任务", run, job=sample_job(), include_stop_action=True,
                                               essential=True),
        "plain": build_stream_card("正在处理", "任务", run, job=sample_job()),
        "no_job": build_stream_card("直接回答", "任务", run),
        "figures": figures_element(snapshot, 100),
        "figures_empty": figures_element(),
        "figures_compact": figures_element(snapshot, 100, compact=True),  # A slim card's 详情 (one line).
        "figure_cells": figure_cells(snapshot, 100),
        "usage_lines": usage_lines(snapshot),
        "task_details": task_details_panel(sample_job(), snapshot),
        "task_details_redacting": task_details_panel(sample_job(), {**snapshot, "redaction": {
            "terms": 1, "hits": 2, "requests": 3}}),
        # A running turn's progress (core/progress.py, cards/streaming.py progress_lines): 阶段, 步骤, 最近动静 at a time
        # of day and the estimate with when it was made; and one that may be stuck.
        "task_details_progress": task_details_panel(sample_job(turn_progress={
            "phase": "改卡片", "plan_done": 3, "plan_total": 5, "last_activity_at": FROZEN - 120,
            "last_activity": "在跑测试", "eta_seconds": 2400, "maybe_stuck": False, "at": FROZEN}), running),
        "task_details_stuck": task_details_panel(sample_job(turn_progress={
            "phase": "", "plan_done": None, "plan_total": None, "last_activity_at": FROZEN - 2400,
            "last_activity": "在跑测试", "eta_seconds": None, "maybe_stuck": True, "at": FROZEN}), running),
        # The newest step a command still running (core/progress.py ``command``): 任务详情 says the turn waits for it.
        "task_details_command": task_details_panel(sample_job(turn_progress={
            "phase": "", "plan_done": None, "plan_total": None, "last_activity_at": FROZEN - 2400,
            "last_activity": "在跑评测", "eta_seconds": None, "maybe_stuck": False, "at": FROZEN,
            "command": {"since": FROZEN - 2400, "seconds": 2400, "long": True}}), running),
        # The task card's status line (core/status_line.py): a turn quiet 40 minutes, with a command still running (在做, in
        # blue, waiting for it), the same for a command of five minutes, and a turn as quiet with none running (可能卡住).
        "status_line_command": status_line_sample(2400, {"command": {"since": 0, "seconds": 2400, "long": True}}),
        "status_line_command_short": status_line_sample(300, {"command": {"since": 0, "seconds": 300, "long": False}}),
        "status_line_stuck": status_line_sample(2400, {"maybe_stuck": True}),
        # The reply is out and background tasks keep the task open: one grey line under 现在：, the descriptions named (two or
        # three), the rest counted, only the count when none is known.
        "status_line_background": status_line_sample(120, {}, ("跑全量测试", "等 CI")),
        "status_line_background_many": status_line_sample(120, {}, ("跑全量测试", "等 CI", "读日志", "整理结果", "")),
        "status_line_background_count": status_line_sample(120, {}, ("", "")),
        # 看看 under the status line of a turn that may be stuck (Cards.show_stuck).
        "look_button": look_element(sample_job()),
        "status_block": status_block("⏳ 执行中 · 已运行 1 分\n第二行", figures_element(snapshot)),
        # The 输入/输出 box (the "E" sample, 2026-11-08): running (开始 · 已运行 / token · token/s / 上下文), on a slim
        # card (no 已运行), and ended (用时 · token/s / token · 缓存命中).
        "io_box_running": io_box(sample_job(), io_lines(running_figures(running, FROZEN - 73), "1分13秒")),
        "io_box_slim": io_box(sample_job(), io_lines(running_figures(running, FROZEN - 73), slim=True)),
        "io_box_ended": io_box(sample_job(), output_lines(output_parts(snapshot, 100))),
        "io_box_unknown": io_box(sample_job(input_parts=None, model_label="", workspace_label="")),
        "answer_card": answer_card("# 结论\n\n第一段 **加粗**\n\n### 小标题\n\n- 一\n- 二\n\n```py\nprint(1)\n```\n",
                                   "T70 有结果 · 第一段 加粗"),
        "status_text": render_stream_status(sample_job(), "排队中", ["⚠️ 提示", "前面还有 1 个任务"], queue_depth=2),
        **answer_tail_cards(),
        **queued_cards(run),
    }
    for status in ("done", "failed", "cancelled", "waiting_user"):
        cards[f"final_text_{status}"] = render_stream_final(sample_job(), "任务 #431 的回答正文", status=status)
    cards["final_text_done_elsewhere"] = render_stream_final(sample_job(), "任务 #431 的回答正文", answer_elsewhere=True)
    # A body over the card's limit: cut by characters (an open code block closed again), then by size (tables).
    by_chars = "开头一段。\n\n```py\n" + "".join(f"print('{i:04d}' + '{'a' * 88}')\n" for i in range(300)) + "```\n"
    by_size = "| a | b | c | d | e | f | g |\n" * 200
    cards["over_limit_by_chars"] = build_stream_card(by_chars, "任务", run, job=sample_job())
    cards["over_limit_by_size"] = build_stream_card(by_size, "任务", run, job=sample_job())
    cards["over_limit_flags"] = [exceeds_stream_card_limit(text, run) for text in (by_chars, by_size)]
    cards["details_text"] = card_details_text(cards["running"])
    cards["status_elements"] = {"line": status_elements("⏳ 执行中", False),
                                "rows": status_elements("⏳ 执行中 · 已运行 1 分\n阶段：改代码", True)}
    lead = answer_lead("# 结论：重试改成指数退避\n\n正文")
    cards["texts"] = {
        "answer_lead": lead,
        "answer_lead_sentence": answer_lead("改好了。还跑了测试"),
        "answer_lead_code_only": answer_lead("```\ncode\n```"),
        "answer_summary": {status: answer_summary("T70", status, lead) for status in ("done", "failed")},
        "answer_summary_no_lead": answer_summary("T70", "cancelled", ""),
        "outcome_line": {status: outcome_line(status) for status in ("done", "failed", "cancelled", "waiting_user")},
        "running_summary": [running_summary("T70", "任务"), running_summary("T70", "任务", phase="改代码" * 20),
                            running_summary("T70", "任务", asking=True)],
        "task_card_summary": [task_card_summary("T70", "done", "有结果 · 用时 3 分 39 秒"),
                              task_card_summary("T70", "failed")],
        "stopped_sentence": stopped_sentence(431),
        "history": render_history([{"role": "user", "content": "第一问"}, {"role": "assistant", "content": "第一答"},
                                   {"role": "user", "content": "  "}]),
        "recent_questions": recent_user_questions([{"role": "user", "content": f"问题 {i}"} for i in range(5)],
                                                  "问题 4"),
    }
    return cards


def trace_panels():
    """执行过程 as each state of a task draws it: the panel put on the card, and the CardKit writes from the
    previous state's panel."""
    from covey.backends.gateway.catalog import retry_notice
    from covey.backends.gateway.relay import Busy
    from support import acme as acme_gateway
    from covey.core.trace import ToolTrace, finish_record_trace, task_trace
    from covey.cards.trace_updates import incremental_actions
    from support.cards import figures_line, split_panel, trace_panel

    out = {}

    def shots(name, trace, states):
        previous = None
        for label, step in states:
            step(trace)
            panel = split_panel(trace_panel(trace, trace.ended_at is None))
            entry = {"panel": panel, "figures_line": figures_line(trace.snapshot())}
            if previous is not None:
                entry["actions"] = incremental_actions(previous, panel)
            out[f"{name}.{label}"] = entry
            previous = panel

    def tool(i, status="done", *, child=None, name="bash", detail=None):
        data = {"id": f"t{i}", "name": name, "status": status, "started_at": FROZEN - 60 + i,
                "detail": f"ls -la dir{i}" if detail is None else detail}
        if status != "running":
            data.update(ended_at=data["started_at"] + 1.5, exit_code=0 if status == "done" else 1)
        if child:
            data["child_id"] = child
        return data

    def accept(*events):
        return lambda trace: [trace.accept(kind, data) for kind, data in events]

    def finish(stopped=False):
        return lambda trace: trace.finish(stopped=stopped)

    usage = ("token_usage", {"output_tokens": 3400, "input_tokens": 120000, "cache_read_tokens": 90000,
                             "cache_write_tokens": 1000, "measured_output_tokens": 3000, "generation_seconds": 40.0,
                             "context_tokens": 623456, "context_window": 1000000})
    shots("running", task_trace(), [
        ("empty", accept()),
        ("first_tool", accept(("tool", tool(1, "running")))),
        ("note", accept(("tool", tool(1)), ("progress_note", {"id": "n1", "text": "先看目录，再改代码。**重点**：`a.py`",
                                                            "received_at": FROZEN - 50}))),
        ("more", accept(*[("tool", tool(i, "done" if i % 3 else "failed",
                                        name=("bash", "read", "edit", "web_search")[i % 4], detail=f"/absolute/work/f{i}.py"))
                          for i in range(2, 9)],
                        ("tool", tool(9, "running")), usage,
                        ("first_token", {"ttft_seconds": 1.25, "ttft_source": "stream"}),
                        ("redaction", {"terms": 3, "hits": 5, "requests": 2}))),
        ("finished", finish()),
    ])
    shots("stopped", task_trace(), [
        ("running", accept(("tool", tool(1, "running")), ("progress_note", {"id": "n", "text": "处理中"}))),
        ("stopped", finish(stopped=True)),
    ])
    shots("subtasks", task_trace(), [
        ("spawned", accept(("progress_note", {"id": "intro", "text": "检查项目"}),
                           ("agent_state", {"id": "c1", "name": "检查配置", "status": "running", "started_at": FROZEN - 40,
                                            "description": "读取配置"}),
                           ("tool", tool(1, "running", child="c1")),
                           ("agent_state", {"id": "c2", "name": "核对", "status": "pendingInit",
                                            "started_at": FROZEN - 30, "parent_id": "c1"}),
                           ("tool", tool(2, child="c2")),
                           ("progress_note", {"id": "c1n", "text": "子任务的说明。" * 30, "child_id": "c1",
                                              "title": "子任务说明"}))),
        ("child_done", accept(("tool", tool(1, child="c1")),
                              ("agent_state", {"id": "c1", "name": "检查配置", "status": "completed",
                                               "started_at": FROZEN - 40, "ended_at": FROZEN - 5, "summary": "配置无误"}),
                              ("subtask_usage", {"subtasks": [{"id": "c1", "input_tokens": 100, "output_tokens": 10}]}),
                              usage)),
        ("finished", finish()),
    ])
    shots("compacting", task_trace(), [
        ("running", accept(("tool", {"id": "compact-before-turn", "name": "context_compaction", "status": "running",
                                     "started_at": FROZEN - 50, "public_progress": True,
                                     "detail": "按任务卡上的安排 · 压缩前上下文 62.3 万 token"}))),
        ("done", accept(("tool", {"id": "compact-before-turn", "name": "context_compaction", "status": "done",
                                  "started_at": FROZEN - 50, "ended_at": FROZEN - 2,
                                  "detail": "按任务卡上的安排 · 压缩前 62.3 万 token → 摘要 9,800 token"}),
                        ("tool", tool(1)), ("tool", tool(2)))),
        ("finished", finish()),
    ])

    def busy(trace):
        notify = retry_notice(SimpleNamespace(emit=trace.accept), acme_gateway())
        notify(Busy("retrying", 7, 20, 1, 7, 20))
        notify(Busy("retrying", 2, 16, 1, 2, 16, "sub", limited=2))
        trace.accept("tool", tool(1, "running", child="sub"))
        trace.accept("agent_state", {"id": "sub", "name": "核对", "status": "running", "started_at": FROZEN - 9})
        notify(Busy("recovered", 30, 80, 1, 30, 80))
        notify(Busy("retrying", 3, 15, 2, 33, 95, limited=1))
        trace.accept("tool", tool(2, "running"))
    shots("busy", task_trace(), [("retrying", busy), ("finished", finish())])

    def many(trace):
        for i in range(60):
            trace.accept("tool", tool(i, detail=f"grep -rn pattern{i} src/ | head -{i}" * (1 + i % 5)))
            if i % 7 == 0:
                trace.accept("progress_note", {"id": f"n{i}", "text": f"第 {i} 步说明 " * (20 + i * 3),
                                               "received_at": FROZEN - 60 + i})
    shots("long", task_trace(), [("running", many), ("finished", finish())])
    # Over the panel's budget: one note in chunks, then more 进展说明 than fit, so the oldest fold to their first
    # sentences (older_fold) and the fold's own oldest lines are left out.
    shots("huge_note", task_trace(), [
        ("note", accept(("progress_note", {"id": "big", "text": ("长说明 " * 400 + "\n\n") * 12}))),
        ("finished", finish())])

    def notes(trace):
        for i in range(120):
            if i % 20 == 0:
                trace.accept("tool", tool(i))
            trace.accept("progress_note", {"id": f"m{i}", "text": f"第 {i} 段说明" + "首句很长没有句号" * 10 + "。"
                                           + "细节 " * 40, "received_at": FROZEN - 300 + i})
    shots("over_budget", task_trace(), [("running", notes), ("finished", finish())])
    checkpoint = task_trace()
    checkpoint.checkpoint = "/absolute/state/checkpoints/431.json"
    shots("checkpoint", checkpoint, [("written", accept(
        ("tool", tool(1, detail="记下当前进度 · cat > /absolute/state/checkpoints/431.json <<'EOF'\n{}\nEOF")),
        ("tool", tool(2, name="write", detail="/absolute/state/checkpoints/431.json"))))])
    shots("empty", task_trace(), [("finished", finish())])
    # A record restored after a restart, then ended or stopped.
    for status in ("cancelled", "done"):
        trace = task_trace()
        trace.accept("tool", tool(1, "running"))
        restored = ToolTrace.restore(json.loads(json.dumps(trace.snapshot())))
        finish_record_trace(restored, {"status": status, "elapsed_seconds": 30})
        out[f"restored_{status}"] = {"panel": split_panel(trace_panel(restored, True))}
    return out


def launch_cards():
    from covey.cards.launch_config import launch_form_card
    from covey.cards.launch_receipt import build_preparing_card, build_receipt_card

    def form(model, **kwargs):
        # The home start: no workspace, run mode or 其他目录… (what names a workspace is an older card's, ``legacy``).
        values = dict(model=model, choices=CHOICES, effort="xhigh", topic_tag="T70")
        return launch_form_card("a" * 24, **{**values, **kwargs})

    def legacy(model, **kwargs):
        return form(model, **{"workspace": "project", "names": ["project", "notes"], "run_mode": "worktree", **kwargs})

    return {
        "codex": form("codex:gpt-6-astra"),
        "codex_effort_open": form("codex:gpt-6-astra", expanded=True, page="effort"),
        "codex_notice": form("codex:gpt-6-astra", notice="⚠️ 提示"),
        # COVEY_DEVICES=1 with devices (core/devices.py): one 执行设备 row, 本机 first; with a device chosen the summary says so.
        "codex_device": form("codex:gpt-6-astra", device_options=[("gpu1", "测试机 gpu1"), ("gpu2", "gpu2")]),
        "codex_device_chosen": form("codex:gpt-6-astra", expanded=True, page="device", device="gpu1",
                                    device_options=[("gpu1", "测试机 gpu1"), ("gpu2", "gpu2")]),
        # A card that names a workspace: a butler's worktree launch, a card sent before the home start.
        "codex_legacy": legacy("codex:gpt-6-astra"),
        "codex_legacy_notice": legacy("codex:gpt-6-astra", notice="⚠️ 提示", run_mode="original"),
        "claude_redacting": form("claude:opus", redaction="on"),
        # 脱敏 on and a model that cannot (T647 P0-8): said above the buttons, 不脱敏开始 confirmed in the client.
        "codex_unredacted": form("codex:gpt-6-astra", redaction="on"),
        "maso_expanded": form("maso:preset", expanded=True, work_mode="adaptive"),
        "receipt": build_receipt_card({"model_label": "Codex · GPT-6-Astra", "effort": "xhigh",
                                       "workdir": str(WORK), "topic": "T70", "run_mode": "worktree",
                                       "branch": "bridge/t70"}),
        "preparing": build_preparing_card({"label": "Codex · GPT-6-Astra", "effort": "xhigh",
                                           "workspace": "project", "topic": "T70"}),
    }


def sample_question():
    """One question whose options carry samples as the Bridge keeps them (core/ui_samples.py sample): two button rows,
    and a third too large to draw."""
    from covey.core import ui_samples

    def row(*buttons):
        return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [button]} for button in buttons]}

    def button(label, kind, look="default"):
        return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": look, "width": "fill",
                "name": kind, "behaviors": [{"type": "callback", "value": {"kind": kind, "token": TOKEN}}]}
    same = ui_samples.sample({"schema": "2.0", "body": {"elements": [
        row(button("提交回答", "bridge_question", "primary_filled"), button("不处理", "bridge_question_set_aside"))]}}, 0)
    text = ui_samples.sample([button("提交回答", "bridge_question", "primary_filled"),
                              {**button("不处理", "bridge_question_set_aside", "text"), "width": "default"}], 1)
    large = ui_samples.sample([{"tag": "markdown", "content": f"第 {n} 行"} for n in range(ui_samples.SAMPLE_NODES + 1)], 2)
    return [{"id": "look", "header": "按钮", "question": "不处理按钮用哪一版？", "multiple": False, "options": [
        {"label": "A 一样宽（推荐）", "description": "和提交回答并排、一样宽的边框按钮", "sample": same},
        {"label": "B 文字按钮", "description": "提交回答下面一个文字按钮", "sample": text},
        {"label": "C 很长的一版", "sample": large}]}]


def question_cards():
    from covey.cards.question_cards import (TOP_COPY, answer_notice_card, folded_refusal, mirror_card,
                                                     question_card, question_element, question_feedback_card)

    cards = {f"card_{status}": question_card(question_record(status, continuation_job_id=432), "T70")
             for status in ("pending", "answered", "cancelled", "superseded", "expired", "submitting", "uncertain")}
    cards["card_answered_running"] = question_card(question_record("answered", delivery_state="running"))
    # In its topic's thread (app/question_display.py standalone_card): the topic's row on top, as every card of a topic
    # has it (the owner, 2026-11-22), the first question right under it (T666); answered, the row's tag says so.
    cards["card_in_thread"] = question_card(question_record(), "T70", "改看板的 Pin 目标")
    cards["card_in_thread_answered"] = question_card(question_record("answered", continuation_job_id=432), "T70",
                                                     "改看板的 Pin 目标")
    # 还有 N 件等你 under a card the owner settled (``others_waiting``, never saved): with 去看板, and without it.
    cards["card_answered_others"] = question_card(question_record(
        "answered", continuation_job_id=432, others_waiting={"n": 3, "board": True}), "T70")
    cards["card_set_aside_others"] = question_card(question_record(
        "set_aside", response_kind="set_aside", answers={}, others_waiting={"n": 1, "board": False}), "T70")
    cards["card_folded"] = question_card(question_record("superseded", superseded_reason="newer_question",
                                                         superseded_by_job=432, refusal="这组问题已收起"))
    cards["card_multi_select"] = question_card(question_record(
        questions=[{"id": "q", "header": "选择", "question": "选哪个？", "multiSelect": True,
                    "options": [{"label": "甲（推荐）"}, {"label": "乙"}]}], answers={}))
    cards["element_on_task_card"] = question_element(question_record(), "tag", headed=True)
    # On a task card as the Bridge puts it there (app/cards.py set_task_element): no row above it, its first line says it.
    cards["element_on_task_card_plain"] = question_element(question_record(answers={}))
    # One short question without a header in its thread: in bold where the band that said it was, never left out (T666).
    cards["card_in_thread_one"] = question_card(question_record(questions=[{"id": "go", "question": "现在部署吗？"}],
                                                                answers={}), "T70", "改看板的 Pin 目标")
    # The same short question asked before (app/question_dedup.py, T870 Wave 3.5): its grey line 上次你答过 under the bold
    # words, then the third same-class question the card adds (``extra_questions``); the record's questions untouched.
    cards["card_in_thread_one_dedup"] = question_card(question_record(
        questions=[{"id": "go", "question": "现在合进主干吗？", "options": [{"label": "合进去"}, {"label": "先不合"}]}],
        answers={}, dedup={"go": {"ref": "decision:a1b2c3d4e5f6:d1", "code": "D7K2Q", "answer": "合进去",
                                  "question": "这次的改动现在合进主干吗？", "line": "上次你答过：合进去（T12 · 11-26）"}},
        extra_questions=[{"id": "__memory_class", "header": "以后", "question": "以后这类（合进主干）还要问你吗？",
                          "options": [{"label": "还要问", "description": "照常问；30 天内不再问这一句"},
                                      {"label": "以后按这次的做", "description": "记成一条规矩：以后同类的题按这次的回答直接做"}],
                          "class": {"key": "a1b2c3d4e5f6:merge:c29f6324", "category": "merge"}}]), "T70", "改看板的 Pin 目标")
    # Only the grey line (no third of its class): the one short question in bold, the grey line its own line under it.
    cards["card_in_thread_one_seen"] = question_card(question_record(
        questions=[{"id": "go", "question": "现在合进主干吗？", "options": [{"label": "合进去"}, {"label": "先不合"}]}],
        answers={}, dedup={"go": {"ref": "decision:a1b2c3d4e5f6:d1", "code": "D7K2Q", "answer": "合进去",
                                  "question": "这次的改动现在合进主干吗？", "line": "上次你答过：合进去（T12 · 11-26）"}}),
        "T70", "改看板的 Pin 目标")
    # A question its turn did not wait for, the turn over: that grey line over it, the only line above the question.
    cards["card_in_thread_not_waiting"] = question_card(question_record(
        blocking=False, task_finished=True, answers={},
        questions=[{"id": "note", "header": "补充", "question": "还有别的要求吗？"}]), "T70", "改看板的 Pin 目标")
    # A question with one line of background (core/checkpoint.py CHECKPOINT_RULES ``context``, T604 #14): grey under it.
    cards["card_context"] = question_card(question_record(questions=[
        {"id": "port", "header": "端口", "question": "新服务用哪个端口？", "context": "看板要从外网打开，8080 已被网关占用",
         "options": [{"label": "8443（推荐）", "description": "不用改网关"}, {"label": "9090"}]},
        {"id": "note", "header": "补充", "question": "还有别的要求吗？"}], answers={}), "T70")
    # A question the project's lead handed back (app/triage.py escalate): its why; with background and a recommendation (T604 #14).
    cards["card_escalated"] = question_card(question_record(answers={}, triage={
        "state": "escalated", "why": "记忆里没有这个版本的依据", "by": "lead"}), "T70")
    cards["card_escalated_recommend"] = question_card(question_record(answers={}, triage={
        "state": "escalated", "why": "要不要停服务得你定", "by": "lead", "context": "导出会锁表约 5 分钟",
        "recommend": "今晚 23 点", "if_not": "白天停会挡住下单"}), "T70")
    # 不处理 (T604 #15): set aside, with 撤销; then taken by the topic's next turn (#432), no 撤销 left.
    skipped = {"fmt": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}, "note": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}}
    cards["card_set_aside"] = question_card(question_record("set_aside", response_kind="set_aside", answers=skipped), "T70")
    cards["card_set_aside_taken"] = question_card(question_record(
        "answered", response_kind="set_aside", answers=skipped, delivery_state="delivered", continuation_job_id=432), "T70")
    # A record with a 先问一句 from before T631, which took it off the card: an open form like any, the old field unread.
    cards["card_asked_first"] = question_card(question_record(answers={}, asked_first={
        "at": FROZEN - 30, "text": "表格和列表在手机上哪个好看？", "event": "e"}), "T70")
    # 提交回答 refused (app/interaction.py refuse_on_card): the card redrawn with the refusal under it and what the click
    # sent kept in its fields (cards/question_cards.py DRAFT); the dropdown's choice, the answer box and 补充说明.
    cards["card_refused_draft"] = question_card(question_record(answers={}, refusal="回答还没提交：还有 1 题没回答：…", draft={
        "option_0": "1", "answer_0": "都不合适，先出两版给我挑", "supplement": "顺便说下为什么问"}), "T70")
    # One question with options at the private chat's top level: a button per option (cards/question_cards.py picks).
    cards["card_top_pick"] = question_card({**question_record(questions=question_record()["questions"][:1], answers={}),
                                            TOP_COPY: True}, "T70")
    # Its buttons by how wide their labels are (2026-11-24 T746): short ones in one row as above; too many for a row in
    # rows, as many to a row as fit; with a long one, each option a row of its own as wide as the card.
    cards["card_top_pick_rows"] = question_card({**question_record(answers={}, questions=[{
        "id": "way", "question": "先做哪些？", "options": [{"label": "两项都做（推荐）"}, {"label": "只做任务进程预热"},
                                                          {"label": "只做配置变更"}, {"label": "先不做"}]}]), TOP_COPY: True}, "T70")
    cards["card_top_pick_long"] = question_card({**question_record(answers={}, questions=[{
        "id": "run", "question": "接下来怎么跑？", "options": [{"label": "保留草稿，在新版组上继续跑完第 1 轮再决定（推荐）"},
                                                            {"label": "回退到基线，重新从第 1 轮开始跑"}]}]), TOP_COPY: True}, "T70")
    # A 话题区's question at the top level (app/question_display.py mirror): the turn's output above the form.
    output = "## 两种做法\n\n- A：改看板，`pins.py`\n- B：改卡片 <x>"
    cards["mirror_pending"] = mirror_card(question_record(), "T436", output)
    cards["mirror_answered"] = mirror_card(question_record("answered", continuation_job_id=432), "T436", output)
    cards["mirror_no_output"] = mirror_card(question_record(), "T436")
    # The asking turn's 产物 under the output, as its answer card in the topic shows them (2026-11-09).
    from covey.cards.answer_card import ARTIFACTS_ID
    from covey.cards.elements import markdown_panel
    files = markdown_panel(ARTIFACTS_ID, "产物 2 个 · a.png 等", "- [a.png](https://example.invalid/a) · 12 KB\n"
                           "- [b.png](https://example.invalid/b) · 3 KB", quiet=True)
    cards["mirror_with_files"] = mirror_card(question_record(), "T436", output, files=files)
    cards["mirror_files_no_output"] = mirror_card(question_record("answered", continuation_job_id=432), "T436",
                                                  files=files)
    # The owner's case (2026-11-23, T666): one question with a header and options at the top level, under his words and
    # the turn's output: a rule, then the question and a button per option, 补充说明, 提交回答, how to answer folded last;
    # no band saying the row's 等你 and the header again, no rules between the output and the question.
    from covey.cards.answer_card import ask_block
    pick = question_record(answers={}, questions=[{"id": "way", "header": "先改哪处", "question": "接下来先做哪一个？",
                                                   "options": [{"label": "A 先改卡片（推荐）", "description": "改动小，今晚能验"},
                                                               {"label": "B 先改看板", "description": "要动排序"}]}])
    cards["mirror_pick_asked"] = mirror_card(pick, "T436", output, ask=ask_block(
        "问答卡这一部分是不是也可以优化一下", at="00:20", who="T436 精简问题卡", doing="waiting",
        url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&openThreadId=omt_436"))
    # 样张 (core/ui_samples.py, docs/ui.md 12): a question whose options carry samples of a card, drawn above the question,
    # each under its option's label and description; one too large says why; answered, the samples are gone.
    samples = sample_question()
    cards["card_samples"] = question_card(question_record(answers={}, questions=samples), "T70", "问题卡的按钮")
    cards["card_samples_answered"] = question_card(question_record(
        "answered", continuation_job_id=432, questions=samples, answers={"look": {"answers": ["A 一样宽（推荐）"]}}),
        "T70", "问题卡的按钮")
    cards["mirror_samples"] = mirror_card(question_record(answers={}, questions=samples), "T436", output)
    cards["feedback"] = question_feedback_card(question_record())
    cards["answer_notice"] = answer_notice_card("已提交回答")
    cards["folded_refusal"] = folded_refusal(question_record("superseded", superseded_by_job=432))
    return cards


def proxy_cards():
    from covey.cards.proxy_panel import expired_card, proxy_panel_card
    from covey.cards.proxy_resend import resend_card

    settings = {"enabled": True, "group_chat_id": "oc_fake_proxy", "harness": "codex", "model": "gpt-6-astra",
                "effort": "xhigh", "user_profile": "me", "web_python": "/absolute/python", "workspace": "project"}
    person = {"key": "p2p:ou_fake_z", "kind": "p2p", "target": "ou_fake_z", "name": "张三"}
    cards = {
        "panel": proxy_panel_card(
            "f" * 24, {"revision": 2, "notice": "✓ 已加入白名单", "query": "张三",
                       "found": {"query": "张三", "candidates": [{**person, "detail": "数据", "email": "z@example.com"}]}},
            settings=settings,
            contacts=[{**person, "paused": False},
                      {"key": "group:oc_fake_g", "kind": "group", "target": "oc_fake_g", "name": "项目群", "paused": True}],
            batches=[{"contact": person["key"]}], choices=CHOICES, current=("codex:gpt-6-astra", "xhigh")),
        "panel_lookup": proxy_panel_card(
            "f" * 24, {"revision": 3, "query": "张三、王五 Covey", "page": 1, "lookup": {
                "id": "abcd1234", "text": "张三、王五 Covey", "left": 0, "items": [
                    {"query": "张三", "state": "unique", "candidates": [{**person, "detail": "数据", "email": ""}]},
                    {"query": "Covey", "state": "unique", "candidates": [
                        {"key": "group:oc_fake_c", "kind": "group", "target": "oc_fake_c", "name": "Covey", "detail": "群",
                         "email": "", "members": 12}]},
                    {"query": "王五", "state": "choices", "more": 0, "candidates": [
                        {"key": f"p2p:ou_fake_w{i}", "kind": "p2p", "target": f"ou_fake_w{i}", "name": "王五",
                         "detail": "部门", "email": ""} for i in range(2)]},
                    {"query": "没这个人", "state": "none", "candidates": []}]},
                "members": {"id": "beef0001", "chat_id": "oc_fake_c", "name": "Covey", "total": 12, "bots": 1, "skipped": 2,
                            "add": [{"open_id": f"ou_fake_m{i}", "name": f"成员{i}"} for i in range(10)],
                            "partial": False}},
            settings=settings,
            contacts=[{"key": f"p2p:ou_fake_{i}", "kind": "p2p", "target": f"ou_fake_{i}", "name": f"联系人{i}",
                       "paused": False} for i in range(12)] + [
                {"key": f"p2p:ou_fake_s{i}", "kind": "p2p", "target": f"ou_fake_s{i}", "name": f"成员{i}",
                 "paused": i == 0, "source": {"chat_id": "oc_fake_s", "name": "项目群"}} for i in range(5)],
            batches=[], choices=CHOICES, current=("codex:gpt-6-astra", "xhigh"), busy="⏳ 正在查找…"),
        "panel_group_open": proxy_panel_card(
            "f" * 24, {"revision": 4, "source_open": "oc_fake_s"}, settings=settings,
            contacts=[{**person, "paused": False}] + [
                {"key": f"p2p:ou_fake_s{i}", "kind": "p2p", "target": f"ou_fake_s{i}", "name": f"成员{i}",
                 "paused": i == 0, "source": {"chat_id": "oc_fake_s", "name": "项目群"}} for i in range(3)],
            batches=[{"contact": "p2p:ou_fake_s1"}], choices=CHOICES, current=("codex:gpt-6-astra", "xhigh")),
        # 最近 (core/proxy.py recent): a reply drafted and not sent, a send not known to have gone out, one the owner
        # answered himself; a contact removed since is named so.
        "panel_recent": proxy_panel_card(
            "f" * 24, {"revision": 5}, settings=settings, contacts=[{**person, "paused": False}], batches=[],
            choices=CHOICES, current=("codex:gpt-6-astra", "xhigh"),
            recent=[{"kind": "drafted", "contact": person["key"], "at": FROZEN - 120},
                    {"kind": "unsure", "contact": "p2p:ou_fake_gone", "at": FROZEN - 600},
                    {"kind": "answered", "contact": person["key"], "at": FROZEN - 2 * 86400}]),
        "expired": expired_card(),
    }
    for state in ("failed", "resending", "sent", "uncertain", "dropped"):
        cards[f"resend_{state}"] = resend_card({"state": state, "notice": "⚠️ 没有重新发送：代答已关闭。"}, {
            "id": "0" * 32, "reply": "收到\n**这两条** 挺到位的", "reason": "网页端未登录", "messages": []}, "张三")
    # The 代发草稿 card (cards/owner_send.py) waiting for the owner, with 先问一句 under the box (T604 #13), and sent.
    from covey.cards.owner_send import owner_send_card, top_card
    draft = {"id": "d" * 32, "ref": "zs", "status": "drafted", "topic_label": "T9「通知」", "why": "把开好的 key 交给他",
             "text": "key 开好了，在共享盘的 keys 目录", "recipient": {"name": "张三", "department": "算法", "kind": "p2p"}}
    cards["owner_send_drafted"] = owner_send_card(draft, {"state": "drafted"})
    # The small card at the chat's top level that says a draft waits in its topic (app/owner_send.py top_line).
    cards["owner_send_top"] = top_card("T9「通知」", "张三", "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x")
    cards["owner_send_unsent"] = owner_send_card({**draft, "status": "unsent", "reason": "网页端未登录"}, {"state": "unsent"})
    # A draft with a blank left in it (T604 #41): the card says so; and one whose text the card cannot edit.
    blank = {**draft, "text": "会议改到【日期】，请确认"}
    cards["owner_send_blank"] = owner_send_card(blank, {"state": "drafted"})
    cards["owner_send_blank_refused"] = owner_send_card(blank, {"state": "drafted", "notice": "⚠️ 正文里还有没填的占位符（【日期】），没有发送。请在框里改掉再点发送。"})
    cards["owner_send_blank_read_only"] = owner_send_card({**blank, "text": blank["text"] + " a@example.com"}, {"state": "drafted"})
    cards["owner_send_sent"] = owner_send_card({**draft, "status": "sent", "sent_at_text": "18:59"}, {"state": "sent"})
    return cards


def queued_cards(run):
    """A topic's queued card with a task before it (app/submission.py queued_line, queue_hint; cards/streaming.py
    steer_label): its first line says where the message goes and its 补充 the task it sends it into (现在补给 #430);
    one being sent into that task as it arrived says so until its receipt (steering_line); one that stayed queued
    says why in one line; a Harness that takes no supplements (dsh) has no 补充.
    """
    from covey.app.submission import queue_hint, queued_line, steering_line
    from covey.cards.review import render_stream_status
    from covey.cards.streaming import build_stream_card
    from covey.core.harness import HarnessCapabilities

    queued = sample_job(steer_target_job_id=430, user_text="等一下，别改那个文件")
    plain = sample_job(steer_target_job_id=430, user_text="等一下，别改那个文件",
                       harness_capabilities=HarnessCapabilities())
    stage = "排队中 · 排在 #430 后面 · #430 已运行 5分（18:55 开始）"
    return {
        "queued_behind": build_stream_card(render_stream_status(queued, stage, [queue_hint(queued)]), "任务", run,
                                           job=queued, include_stop_action=True, status=queued_line(queued)),
        "queued_steering": build_stream_card(render_stream_status(queued, stage), "任务", run, job=queued,
                                             include_stop_action=True, status=steering_line(queued)),
        "queued_refused_text": render_stream_status(queued, "排队中", [
            "⚠️ 没有补充到 #430：任务正在收尾，不能再补充", queue_hint(queued)]),
        "queued_behind_no_steer": build_stream_card(render_stream_status(plain, stage, [queue_hint(plain)]), "任务",
                                                    run, job=plain, include_stop_action=True,
                                                    status=queued_line(plain)),
    }


ARRIVAL_SOURCE = "来源：#431 在做时发到话题里的消息，自动作为补充交给它，不另开一轮。"


def steer_cards():
    from covey.cards.held_note import feedback_chooser, note_card
    from covey.cards.steer_form import closed_card, latest_request, steer_form
    from covey.cards.steer_receipts import receipt_card, stage_line

    receipt = {"status": "sent", "job_id": 431, "text": "再补充一点\n第二行", "saved_at": FROZEN - 20,
               "sent_at": FROZEN - 10, "topic_id": TOPIC, "token": "c" * 64}
    # Each state's wording and buttons (撤回, 检查送达状态, 重新排队执行).
    receipts = {
        "submitting": {"status": "submitting", "sent_at": None},
        "held": {"status": "held", "held_at": FROZEN - 15, "sent_at": None, "error": "任务刚开始"},
        "uncertain": {"status": "uncertain", "error": "飞书超时"},
        "sent_with_reads": {"reads": True},
        # Unread in a task whose command can be stopped: 已等 N 秒 and 打断它，马上读; then what the stop did.
        "unread_stops": {"reads": True, "stops": True, "receipt": {"kind": "job"}},
        "interrupted": {"reads": True, "stops": True, "interrupt_at": FROZEN - 3, "interrupt_by": "click",
                        "interrupt": "已停掉正在跑的命令，模型马上读到", "interrupted": "sleep 600"},
        "interrupt_kept": {"reads": True, "stops": True, "interrupt_at": FROZEN - 3, "interrupt_by": "auto",
                           "interrupt": "正在跑的是推送、部署这类命令，不打断；它结束后模型读到"},
        "unread_long": {"reads": True, "stops": True, "sent_at": FROZEN - 200},
        "read": {"status": "read", "read_at": FROZEN - 5, "reads": True},
        "unread_requeue": {"target_ended": True, "ended_at": FROZEN - 2, "requeue": True},
        "unread": {"target_ended": True, "ended_at": FROZEN - 2},
        "refused_requeue": {"status": "refused", "sent_at": None, "error": "任务已结束", "requeue": True},
        "withdrawn": {"status": "withdrawn", "sent_at": None, "withdrawn_at": FROZEN - 5, "requeue": True},
        "merged": {"merged": True},
        "requeue_uncertain": {"status": "refused", "sent_at": None, "requeue": True, "requeue_state": "uncertain"},
        "requeued": {"status": "refused", "sent_at": None, "requeue": True, "requeued_job_id": 433},
        # Queued again by a click once its task ended unread: a line saying where it went, no band (T651).
        "unread_requeued": {"target_ended": True, "ended_at": FROZEN - 2, "requeue": True, "requeue_state": "queued",
                            "requeued_job_id": 433},
    }
    arrival = {"receipt": {"kind": "job", "key": "om_fake_next", "job_id": 432, "text": "再补充一点\n第二行",
                           "auto": True},
               "requeue": {"text": "再补充一点\n第二行", "prompt": "再补充一点\n第二行", "sender_id": "ou_fake_owner"}}
    arrivals = {
        "arrival": {},
        "arrival_read": {"status": "read", "read_at": FROZEN - 5, "reads": True},
        "arrival_held": {"status": "held", "held_at": FROZEN - 15, "sent_at": None, "error": "任务刚开始"},
        "arrival_uncertain": {"status": "uncertain", "error": "补充已转交任务进程，暂未确认送达；不会重复发送"},
        "arrival_requeuing": {"reads": True, "target_ended": True, "ended_at": FROZEN - 2, "requeue_next": True},
        "arrival_requeued": {"reads": True, "target_ended": True, "ended_at": FROZEN - 2, "requeue_next": True,
                             "requeue_state": "queued", "requeued_job_id": 433},
        "arrival_refused_requeued": {"status": "refused", "sent_at": None, "held_at": FROZEN - 15,
                                     "error": "任务已经在收尾，这条补充没有发出", "requeue_next": True,
                                     "requeue_state": "queued", "requeued_job_id": 433},
        # Its task was stopped: not queued by itself, 重新排队 offered.
        "arrival_stopped": {"reads": True, "target_ended": True, "ended_at": FROZEN - 2},
    }
    return {
        "form": steer_form(431, "T70 · #431", {"kind": "bridge_steer_submit", "job_id": 431}),
        "form_refused": steer_form(431, "T70 · #431", {"kind": "bridge_steer_submit", "job_id": 431}, text="已写",
                                   notice="⚠️ 没有送达"),
        "closed": closed_card("任务已结束", "T70 · #431 · 已结束", unsent="没发出的补充"),
        "receipt": receipt_card(receipt, source="来源：#432"),
        # A topic's message that went into #431 as it arrived (core/deliveries.py on_arrival): into #431, no round of
        # its own; held, or never read, it becomes the next round by itself (on its way, then queued as #433).
        **{f"receipt_{name}": receipt_card({**receipt, **arrival, **fields}, source=ARRIVAL_SOURCE)
           for name, fields in arrivals.items()},
        **{f"receipt_{name}": receipt_card({**receipt, **fields}) for name, fields in receipts.items()},
        "stage_lines": {name: stage_line({**receipt, **fields}) for name, fields in receipts.items()},
        "latest_request": latest_request("📝 **最新补充** 19:00 · 再补充一点"),
        "feedback_chooser": feedback_chooser(TOPIC, "d" * 24, [question_record(), question_record(token="e" * 16)]),
        "held_note": note_card({"status": "held", "job_id": 431, "text": "这条先暂存", "topic_id": TOPIC,
                                "token": "d" * 24}),
        "held_note_moved": note_card({"status": "held", "text": "这条先暂存", "topic_id": TOPIC, "token": "d" * 24,
                                      "waiting": [{"job_id": 432, "created_at": FROZEN - 20, "count": 2}],
                                      "folded": [{"job_id": 431, "created_at": FROZEN - 30, "count": 1}]}),
    }


def recovery_cards():
    from covey.app.auto_retry import failed_controls
    from covey.app.recovery_cards import recovery_card
    from covey.cards.recovery import result_actions, result_note, retry_status, stopped_card

    record = {"job_id": 431, "topic_id": TOPIC, "status": "interrupted", "message_id": "om_fake_task",
              "harness_id": "codex", "binding_revision": 2, "user_text": "继续"}
    return {
        "interrupted": recovery_card(record),
        "interrupted_continued": recovery_card(record, continued=True),
        # T650: a task runner that never started, its exception in 技术详情 (core/failure.py start_failed).
        "start_failed": recovery_card({**record, "status": "failed", "runner_start_failed": True,
                                       "runner_start_error": "BlockingIOError: Resource temporarily unavailable",
                                       "status_message": "task runner could not be started; the job did not run"}),
        "saved_answer": recovery_card({**record, "result_retry_policy": "saved_delivery_only",
                                       "verified_reply_authorized": True, "verified_reply_state": "authorized"}),
        # The owner's case (2026-11-23, T666: 这种UI还没改呢): a turn the rate limit stopped at once, moved on to its
        # automatic retry. The task card's row says how it went on, one grey line that nothing is asked of him; no band
        # with the task's and the retry's numbers, no 尚无记录, no 检查并继续 note for a button the card does not have.
        "moved_rate_limited": recovery_card({**record, "status": "failed", "execution_status": "failed",
                                             "failure_code": "rate_limited"}, moved_to=432, automatic="rate_limited"),
        "stopped": stopped_card("任务 #431 已停止。", retry=("检查并继续", TOPIC, 431),
                                actions=[result_actions(TOPIC, 431, "重新执行")], header="T70 · #431",
                                summary="⏹ T70 · #431"),
        "result_actions_auto_retry": result_actions(TOPIC, 431, "检查并继续", stop_kind="bridge_auto_retry_stop"),
        # A failed task card's 技术详情 and next steps (core/failure.py): 检查并继续 and 查原因; a policy stop's 拆小再试.
        "failed_controls": failed_controls(TOPIC, 431, {**record, "status": "failed", "execution_status": "failed",
                                                        "failure_code": "root_failed", "final_text": "本轮任务未全部完成。主任务后续执行失败。"}),
        # #36: a used-up quota's 换个模型 and 换个 Harness (app/auto_retry.py other_harness), both the 换配置 click.
        "failed_controls_quota": failed_controls(TOPIC, 431, {**record, "status": "failed", "execution_status": "failed",
                                                              "failure_code": "quota_exhausted"}, other=True),
        "failed_controls_policy": failed_controls(TOPIC, 431, {**record, "status": "cancelled", "final_text": "本轮已停止。",
                                                               "policy_stop": {"kind": "refusal", "category": "cyber", "scope": "main",
                                                                               "child_id": "", "response_id": "", "source_model": "",
                                                                               "target_model": "", "evidence": "refusal_stop"}}),
        # A failed task card while its automatic retry waits (app/auto_retry.py result_elements): a rate limit with no
        # reset known, with the reset Claude Code named, and a passing server fault with its count.
        "failed_controls_limit_pending": failed_controls(TOPIC, 431, {
            **record, "status": "failed", "execution_status": "failed", "failure_code": "rate_limited",
            "auto_retry_state": "pending", "auto_retry_kind": "rate_limited", "auto_retry_attempt": 3,
            "auto_retry_at": FROZEN + 600}),
        "failed_controls_limit_reset_pending": failed_controls(TOPIC, 431, {
            **record, "status": "failed", "execution_status": "failed", "failure_code": "rate_limited",
            "auto_retry_state": "pending", "auto_retry_kind": "rate_limited", "auto_retry_attempt": 1,
            "auto_retry_at": FROZEN + 3660, "auto_retry_reset_at": FROZEN + 3600}),
        "failed_controls_fault_pending": failed_controls(TOPIC, 431, {
            **record, "status": "failed", "execution_status": "failed", "failure_code": "server_fault",
            "auto_retry_state": "pending", "auto_retry_kind": "server_fault", "auto_retry_attempt": 1,
            "auto_retry_at": FROZEN + 30}, other=True),
        "failed_controls_fault": failed_controls(TOPIC, 431, {**record, "status": "failed", "execution_status": "failed",
                                                              "failure_code": "server_fault"}, other=True),
        "retry_status": retry_status("自动重试 · 10:00"),
        "result_note": result_note("说明"),
    }


def rewind_cards():
    from covey.cards.rewind import done_card, rewind_card

    form = {"token": "c" * 24, "topic": "T70", "job_id": 12, "user_text": "把 a.py 里的重试改成指数退避",
            "workdir": str(WORK), "point": {"keep": "k", "checkpoint": True, "file_count": 2, "files": [
                (str(WORK / "a.py"), "restore"), (str(WORK / "notes.md"), "delete")]}}
    return {
        "card": rewind_card(form),
        "card_no_files": rewind_card({**form, "point": {"keep": "k", "checkpoint": False, "files": [],
                                                         "file_count": 0}}),
        "done": done_card({**form, "done": {"files": 0, "keep": "k"}}),
        "done_files": done_card({**form, "done": {"files": 2, "keep": "k"}}),
    }


def switch_cards():
    from covey.cards.elements import notice_card
    from covey.cards.switch_form import build_switch_card, handoff_block
    from covey.cards.topic_actions import switch_panel

    old = {"token": "b" * 24, "topic": "T70", "workspace": "project",
           "current": {"label": "Codex · GPT-6-Astra", "effort": "xhigh"},
           "selected_model": "codex:gpt-6-astra", "selected_effort": "xhigh", "config_revision": 2,
           "models": {"codex:gpt-6-astra": {"label": "Codex · GPT-6-Astra", "levels": ["low", "high", "xhigh", "max"]},
                      "dsh:ali-deepseek-v4.1-flash": {"label": "DeepSeek Harness · flash",
                                                      "levels": ["low", "high", "max"]}}}
    form = {**old, "models": {key: CHOICES[key] for key in ("codex:gpt-6-astra", "claude:opus",
                                                            "dsh:ali-deepseek-v4.1-flash")},
            "current": {"label": "Codex · GPT-6-Astra", "effort": "xhigh", "key": "codex:gpt-6-astra",
                        "budget": "xhigh", "workspace": "project", "run_mode": "worktree", "redaction": False},
            "no_place": True, "selected_redaction": "off"}
    # A card sent before the home start: the workspace and run mode rows, and the topic's place in its first line.
    legacy = {key: value for key, value in form.items() if key != "no_place"}
    legacy.update(workspaces=["project", "notes"], selected_workspace="project", selected_run_mode="worktree")
    return {
        "card": build_switch_card(form),
        "card_changed": build_switch_card({**form, "selected_model": "dsh:ali-deepseek-v4.1-flash",
                                           "selected_effort": "low"}),
        "card_legacy": build_switch_card(legacy),
        "card_legacy_changed": build_switch_card({**legacy, "selected_model": "dsh:ali-deepseek-v4.1-flash",
                                                  "selected_effort": "low", "selected_run_mode": "original"}),
        "card_old_record": build_switch_card(old),
        "card_redacting": build_switch_card({**form, "selected_model": "claude:opus", "selected_redaction": "off",
                                             "current": {**form["current"], "redaction": True}}),
        "card_refused": build_switch_card({**form, "notice": "**⚠️ 还没有换（20:08）**：本话题还有任务在排队或执行"}),
        # T669: opened from a relay card at the private chat's top level: there, its subtitle naming the topic and where a
        # reply goes (app/harness_switch.py).
        "card_top": build_switch_card({**form, "top_level": True}),
        # T700: a project's lead's, opened from its 专项卡: only the models a lead can run on, no workspace or run mode
        # rows, the project named, what the lead's next turn gets; and while the lead has a turn going.
        "card_lead": build_switch_card({**form, "models": {key: CHOICES[key] for key in ("codex:gpt-6-astra", "claude:opus")},
                                        "lead": {"project_id": "a1b2c3d4e5f6", "name": "迁移订单库"}}),
        "card_lead_busy": build_switch_card({**form, "models": {key: CHOICES[key] for key in ("codex:gpt-6-astra", "claude:opus")},
                                             "selected_model": "claude:opus", "selected_effort": "high", "active_task": True,
                                             "lead": {"project_id": "a1b2c3d4e5f6", "name": "迁移订单库"}}),
        "closed": notice_card("**🔁 已换配置**", "已换配置"),
        "panel_on_task_card": switch_panel(TOPIC, 431, "codex", 2),
        # T647 E1: the 交接 block above the first task card's answer after a switch (app/switch_handoff.py).
        "handoff_waiting": handoff_block("Claude Code · claude-opus（high）", "Codex · gpt-6-astra（xhigh）", state="waiting"),
        "handoff_summary": handoff_block("Claude Code · claude-opus（high）", "Codex · gpt-6-astra（xhigh）",
                                         summary="## 目标\n把看板改成彩色块。\n\n## 当前状态\n浅色做完，深色没做。", rounds=12, kept=1),
        "handoff_none": handoff_block("Codex · gpt-6-astra（xhigh）", "Claude Code · claude-opus（high）", rounds=2, kept=2,
                                      why="原来的 Harness 不会写交接摘要"),
        "handoff_stopped": handoff_block("Claude Code · claude-opus（high）", "Codex · gpt-6-astra（xhigh）", state="stopped"),
    }


def topic_panels():
    from covey.cards.artifact_panels import artifact_panel
    from covey.cards.topic_actions import topic_panel

    return {
        "topic": topic_panel(TOPIC, 431, ("claude", 2), rewind=True),
        "artifacts": artifact_panel([str(WORK / "result.md"), str(WORK / "diff.md")], {"job_id": 431}, TOPIC),
    }


def usage_cards():
    from covey.cards.weekly_usage import usage_card, usage_text

    summary = {"since": FROZEN - 7 * 86400, "until": FROZEN, "days": 7, "tasks": 200, "topics": 3, "untracked": 1,
               "tokens": {"input_tokens": 330000000, "cache_read_tokens": 270000000, "output_tokens": 3100000},
               "outcomes": {"done": 3, "continued": 195, "failed": 1, "active": 1},
               "ended": {"done": 197, "stopped": 1, "cancelled": 1, "running": 1},
               "daily": [{"day": f"10-{28 + i}" if i < 4 else f"11-0{i - 3}", "tasks": 25,
                          "tokens": [0, 4100000, 12000000, 3300000, 9800000, 1500000, 2200000, 300000][i]}
                         for i in range(8)],
               "harnesses": [{"title": "Codex", "tasks": 197, "tokens": 30300000, "input": 30200000, "cache": 0,
                              "untracked": 1},
                             {"title": "Claude", "tasks": 3, "tokens": 33000000, "input": 300000000,
                              "cache": 270000000, "untracked": 0}]}
    empty = {**summary, "tasks": 0, "topics": 0, "untracked": 0, "outcomes": {}, "ended": {}, "daily": [], "harnesses": [],
             "tokens": {"input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0}}
    # 本周找你 (T604 #1): the week's questions and stops under the band, counted only.
    reached = {**summary, "reached": {"questions": 12, "stopped": 1}}
    return {"card": usage_card(summary), "card_empty": usage_card(empty), "text": usage_text(summary),
            "card_reached": usage_card(reached), "text_reached": usage_text(reached)}


def free_groups():
    """Four groups for the 免@ cards: 你和选中的人 (2 picked, one gone from the group), 所有人 while stopped, the proxy's
    and an external one with 只有你."""
    return [{"chat_id": "oc_fake_group1", "active": True, "external": False, "free_talk": "some",
             "free_talk_members": ["ou_fake_m3", "ou_fake_gone1"]},
            {"chat_id": "oc_fake_group2", "active": True, "external": False},
            {"chat_id": "oc_fake_group3", "active": False, "external": False, "free_talk": "all",
             "deactivated_at": "2026-11-09T00:00:00Z"},
            {"chat_id": "oc_fake_group6", "active": False, "external": True, "free_talk": "owner"}]


def waiting_groups():
    """Three groups for the 群聊 card: one active, one waiting for the owner's word (its card went out), one stopped."""
    return [{"chat_id": "oc_fake_group1", "active": True, "external": False},
            {"chat_id": "oc_fake_group7", "active": False, "external": False, "pending_at": "2026-11-04T10:00:00Z",
             "pending_by": "ou_fake_owner", "asked_at": "2026-11-04T10:00:01Z", "asked_message": "om_fake_ask"},
            {"chat_id": "oc_fake_group5", "active": False, "external": False, "deactivated_at": "2026-11-04T09:00:00Z",
             "deactivated_by": "ou_fake_owner"}]


def group_ask_cards():
    """The confirmation card the owner's first @ in a group sends to his private chat (cards/group_manager.py ask_card):
    waiting for his word, after 启用, after 先不启用, a click on a card decided since, the group's record gone."""
    from covey.cards.group_manager import ASKING, DECIDED, MISSING, OFF, ON, ask_card

    view = {"chat": "oc_fake_group7", "name": "项目群"}
    return {"asking": ask_card({**view, "state": ASKING}),
            # The deployment's members' default is read-only (COVEY_GROUP_MEMBERS=read): what members could do says so.
            "asking_read": ask_card({**view, "state": ASKING, "members": "read"}),
            "enabled_read": ask_card({**view, "state": ON, "members": "read"}),
            "enabled": ask_card({**view, "state": ON}),
            "declined": ask_card({**view, "state": OFF}),
            "decided": ask_card({**view, "state": ON, "notice": DECIDED}),
            "gone": ask_card({**view, "state": MISSING})}


def menu_panels():
    from covey.cards.directory import directory_card
    from covey.cards.group_manager import group_manager_card
    from covey.cards.group_manager import folded_card as groups_folded
    from covey.cards.proxy_panel import folded_card as proxy_folded
    from covey.cards.weekly_usage import folded_card as usage_folded
    from covey.cards.workspace_manager import closed_card, manager_card
    from covey.cards.workspace_manager import folded_card as workspaces_folded

    groups = [{"chat_id": f"oc_fake_group{i}", "active": i % 3 != 0, "external": None if i == 3 else i == 6}
              for i in range(12)]
    names = {f"oc_fake_group{i}": f"群{i}" for i in range(10)}
    config = {"default": "w0", "workspaces": {f"w{i}": f"/absolute/w{i}" for i in range(14)},
              "recent": {"w1": FROZEN - 60}}
    view = {"revision": 3, "notice": "✓ 已从列表移除 1 项，磁盘目录未删除", "details": "**old** · /absolute/old"}
    return {
        "groups": group_manager_card("a" * 24, {"revision": 1, "page": 0}, groups, names),
        "groups_page_2": group_manager_card("a" * 24, {"revision": 1, "page": 1, "notice": "✓ 已停用"}, groups, names),
        "groups_empty": group_manager_card("a" * 24, {"revision": 1}, [], {}),
        # 免@ offered (COVEY_GROUP_ALL_MESSAGES=1): a tag and a 免@ button a row, and a group's 免@ page.
        "groups_free": group_manager_card("a" * 24, {"revision": 1, "page": 0}, free_groups(), names,
                                          free={"blocked": {"oc_fake_group2"}}),
        "groups_free_page": group_manager_card(
            "a" * 24, {"revision": 2, "free": "oc_fake_group1", "notice": "✓ 群「群1」免@ 改成：你和选中的人",
                       "members": [{"id": f"ou_fake_m{i}", "name": f"成员{i}"} for i in range(10)]},
            free_groups(), names, free={"blocked": set()}),
        "groups_free_unread": group_manager_card(
            "a" * 24, {"revision": 2, "free": "oc_fake_group1", "members": None, "member_page": 0},
            free_groups(), names, free={"blocked": set()}),
        "groups_free_external": group_manager_card(
            "a" * 24, {"revision": 2, "free": "oc_fake_group6", "members": []}, free_groups(), names,
            free={"blocked": set()}),
        "groups_free_proxy": group_manager_card(
            "a" * 24, {"revision": 2, "free": "oc_fake_group2", "members": []}, free_groups(), names,
            free={"blocked": {"oc_fake_group2"}}),
        # A group waiting for the owner's word (his first @ there, app/access.py pending): first, 启用 and 先不启用; with
        # 免@ offered its row has three buttons.
        "groups_pending": group_manager_card("a" * 24, {"revision": 1, "page": 0}, waiting_groups(), names),
        "groups_pending_free": group_manager_card("a" * 24, {"revision": 1, "page": 0}, waiting_groups(), names,
                                                  free={"blocked": set()}),
        # 群成员只读: one active group set read-only on the card (its tag, 取消只读), one not (设为只读); with the
        # deployment's default read-only, every active group not chosen for is read-only too.
        "groups_read": group_manager_card("a" * 24, {"revision": 2, "page": 0,
                                                     "notice": "✓ 群「群1」设为只读：群成员开的任务只能读文件和搜索"},
                                          [{**waiting_groups()[0], "member_mode": "read"}, *waiting_groups()[1:],
                                           {"chat_id": "oc_fake_group2", "active": True, "external": False}], names),
        "groups_read_default": group_manager_card("a" * 24, {"revision": 1, "page": 0},
                                                  [{"chat_id": "oc_fake_group1", "active": True, "external": False},
                                                   {"chat_id": "oc_fake_group2", "active": True, "external": False,
                                                    "member_mode": "full"}], names, members="read"),
        "workspaces": manager_card("a" * 24, view, config, {"/absolute/w2": False, "/absolute/w3": None},
                                   standalone=True),
        "workspaces_from_launch": manager_card("a" * 24, view, config, {}, standalone=False),
        "workspaces_closed": closed_card("**工作区管理已结束**"),
        "directory": directory_card("e" * 24, {"path": str(WORK), "revision": 1,
                                               "entries": [{"name": f"child-{i}", "link": False} for i in range(30)]}),
        # A second menu click on the newest card folds it in place (app/bot_menu.py).
        "folded_usage": usage_folded(),
        "folded_workspaces": workspaces_folded(),
        "folded_proxy": proxy_folded(),
        "folded_groups": groups_folded(),
    }


def answer_tail_cards():
    """The answer card with its 产物 and 输入/输出 (cards/answer_card.py, 2026-11-08), its relay at the top level and
    the small card after an answer sent as a file."""
    from covey.cards.relay_card import DONE, STOPPED, WAITING, merged_card, relay_card
    from covey.app import relay_stops
    from covey.cards.elements import replace_element
    from covey.core.failure import failure_view

    def stopped_relay(**fields):
        """停住了 at the top level as app/relay_stops.py facts draws it: core/failure.py's words of the record, its buttons."""
        record = {"job_id": 439, "topic_id": TOPIC, "status": "failed", "execution_status": "failed", **fields}
        view = failure_view(record)
        area = relay_stops.buttons(record, view)
        return relay_card("T439", STOPPED, reason=view["title"], buttons=[area] if area else [])[0]
    from covey.cards.answer_card import (ARTIFACTS_ID, acceptance_element, criteria_element, input_parts, memory_element,
                                         output_parts, tail, tail_card)
    from covey.cards.elements import markdown_panel
    from covey.cards.review import answer_card
    from covey.cards.topic_actions import alone, relay_switch, topic_panel

    files = markdown_panel(ARTIFACTS_ID, "产物 2 个 · REPORT.md 等", "- [REPORT.md](https://example.invalid/a) · 12 KB\n"
                           "- [本轮改动.diff](https://example.invalid/b) · 3 KB", quiet=True)
    inputs = input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", True, "demo-app")
    outputs = output_parts({"input_tokens": 750_000, "output_tokens": 10_000, "cache_read_tokens": 690_000,
                            "measured_output_tokens": 8_200, "generation_seconds": 200}, 760)
    text = "**结论**：按钮已经改好。\n\n- 去掉了侧聊\n- 旧卡点了回一句"
    from covey.cards.answer_card import ask_block
    from covey.cards.question_cards import mirror_card

    ask = ask_block("这个答案区的卡片能不能和私聊区的卡片样式一样？或者说，私聊区相比答案区只多一个跳转链接。", at="12:31",
                    url="https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x")
    from covey.cards.answer_card import answer_block
    # An answer's turn: the "你答" fold, folded, opening the whole answer (2026-11-09: two questions, one answer of two
    # long lines, 补充说明).
    answered = answer_block([("question", "7 种风格里选哪一种（A 极简 / B 杂志 / C 手绘 / D 水彩 / E 像素 / F 线稿 / G 拼贴）？"),
                             ("answer", "我重新给你一张图，你把这个图的风格和第 3 种结合起来，颜色用暖色，字体用 **粗一点** 的；"
                              "人物比例按原图，背景留白多一些，不要 <b>阴影</b>。\n- 先出两张小样给我挑"),
                             ("question", "S3（先读后答）按 S3-PLAN.md 的草稿执行到哪一步？"), ("answer", "全流程（推荐）"),
                             ("heading", "补充说明"), ("note", "做完后发我截图\n两张就够")],
                            "7 种风格里选哪一种 → 我重新给你一张图；S3 → 全流程（推荐）", at="14:43")
    from covey.cards.answer_card import topic_head
    head = topic_head("T443", "改看板的 Pin 目标", "done")  # Atop a 话题区's answer card in its thread (D3): the topic's row.
    # A turn that changed code (app/answer_cards.py change): its checkpoint's delivery as written.
    handed = {"commit": "a6bc148d38ce8c230e96e", "deployed": "第 322 代（10:11 切换，旧进程已退出）",
              "verified": "全量测试通过；在「Covey 验收测试」点开折叠看过"}
    from covey.cards.answer_card import handoff_card, hook_card
    # 2026-11-10 17:59: the line a topic's thread gets from topic_send opens the owner's message; the hook under his
    # message opens each topic it went to (one without a link: no ›).
    handoff = handoff_card("管家转来你 17:54 的消息", "这块代码提交到github上吧\n附件：图片 1 张",
                           "https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=1379")
    hook = hook_card([("T453「同步 GitHub」", "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x"
                                            "&open_thread_id=omt_x&thread_position=7"), ("T454「看板」", "")])
    # The 派发卡 (cards/dispatch_card.py): the live card in each state, and the lines it shrinks to.
    from covey.cards import dispatch_card as dc
    from covey.cards.dispatch_card import Dispatch, dispatch_card, settled_card
    thread_url = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x"
    work = Dispatch(token="d" * 16, topic_id="topic-fake", request="把这块代码提交到 GitHub 上吧，推之前先问我推哪些", status=dc.RUNNING,
                    label="T453", title="比对公司主干和 GitHub 快照", who="T453 比对公司主干和 GitHub 快照",
                    workspace="covey", started="12:41", took="2分00秒",
                    body="第 2/4 步：列出待推提交 · 本步 1 分钟", job_id="7", url=thread_url, ask_at="12:31",
                    ask_url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=76",
                    inputs=("Claude Code", "Sonnet 5.5", "思考 high", "covey"),
                    outputs=("**开始** 12:41　**已运行** 2分00秒",))
    asked_words = "把这块代码提交到 GitHub 上吧"
    return {"handoff_line": handoff, "main_hook": hook,
            "dispatch_running": dispatch_card(work),
            "dispatch_running_no_job": dispatch_card(Dispatch(**{**work.__dict__, "job_id": "", "url": ""})),
            "dispatch_waiting": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.WAITING,
                                                          "body": "要你定：推哪几个提交？"})),
            "dispatch_done": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.DONE, "body": "做完了",
                                                       "outputs": ("**用时** 2分00秒",)})),
            "dispatch_failed": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.FAILED, "body": "后端退出",
                                                         "outputs": ("**用时** 2分00秒",)})),
            "dispatch_stopped": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.STOPPED, "body": "已停止"})),
            "dispatch_line_done": settled_card(dc.DONE, asked_words, url=thread_url, label="T453"),
            "dispatch_line_failed": settled_card(dc.FAILED, asked_words, reason="后端退出", label="T453"),
            "dispatch_line_stopped": settled_card(dc.STOPPED, asked_words, label="T453"),
            # In the thread of the owner's question (topic_send's, T636): no "你问" block, the topic heads it.
            "dispatch_thread_running": dispatch_card(Dispatch(**{**work.__dict__, "threaded": True})),
            "dispatch_thread_waiting": dispatch_card(Dispatch(**{**work.__dict__, "threaded": True, "status": dc.WAITING,
                                                                 "body": "要你定：推哪几个提交？"})),
            "dispatch_thread_line_done": settled_card(dc.DONE, asked_words, url=thread_url, label="T453", threaded=True,
                                                      title="比对公司主干和 GitHub 快照"),
            "answer_card_tail": answer_card(text, "✅ T439 · 结论", tail(files, inputs, outputs)),
            # COVEY_MEMORY=1: the memory lines at the bottom (cards/answer_card.py memory_element, app/memory_collect.py).
            "answer_card_memory": answer_card(text, "✅ T439 · 结论", [*tail(files, inputs, outputs), memory_element([
                {"kind": "remembered", "text": "选型类的事不用先问侧重点", "scope": "管这个专项", "entry_id": "a1b2c3"},
                {"kind": "voided", "text": "选型前先问侧重点", "scope": "管这个专项", "entry_id": "d4e5f6"},
                {"kind": "undone", "text": "产物放 out/", "scope": "管这个话题", "entry_id": "0a1b2c"}], "tf0001", 439, url="")]),
            # With the web page on, 改 opens that entry on the page's 记忆 (?code=, a URL button); a note with a title says the title.
            "answer_card_memory_web": answer_card(text, "✅ T439 · 结论", [*tail(files, inputs, outputs), memory_element([
                {"kind": "remembered", "text": "选型类的事不用先问侧重点，直接开工", "title": "选型不先问侧重点", "code": "R4K2M",
                 "scope": "管这个专项", "entry_id": "a1b2c3"}], "tf0001", 439, url="https://covey.example.test/memory")]),
            # A line of 管全局 (the 升为全局 button is gone: moving to a larger scope is the web page's).
            "answer_card_memory_global": answer_card(text, "✅ T439 · 结论", [*tail(files, inputs, outputs), memory_element([
                {"kind": "remembered", "text": "回答先给结论", "scope": "管全局", "entry_id": "p1a2b3c"},
                {"kind": "remembered", "text": "项目代号用简称", "scope": "管这个话题", "entry_id": "0a1b2d", "redacted": True}],
                "tf0001", 439, url="")]),
            # The two kinds of 待审核 on the card (T908 M3b): a rule held here that may be 全局 (one line, 采纳 and 不对), one that
            # grants (待审核, 采纳 and 不对), and the lead's rule of its turn (负责人记下了, 不对).
            "answer_card_memory_review": answer_card(text, "✅ T439 · 结论", [*tail(files, inputs, outputs), memory_element([
                {"kind": "remembered", "text": "以后回复都用中文", "code": "R7M2Q", "scope": "管这个专项", "entry_id": "a1b2c3",
                 "review": "review:learn:9f8e7d"},
                {"kind": "pending", "text": "以后改完直接部署到本机", "scope": "涉及要你点头的事，采纳后才生效", "entry_id": "review:learn:1a2b3c"},
                {"kind": "remembered", "by": "lead", "text": "发版先灰度再全量", "code": "R3K8W", "scope": "管这个专项",
                 "entry_id": "d4e5f6"}], "tf0001", 439, url="https://covey.example.test/memory")]),
            "answer_card_thread": answer_card(text, "✅ T443 · 结论", tail(files, inputs, outputs), [head]),
            "answer_card_thread_stopped": answer_card(text, "⏹ T443 · 结论", tail(None, inputs, outputs),
                                                      [topic_head("T443", "改看板的 Pin 目标", "cancelled")]),
            # A long answer in its thread (the owner, 2026-11-22: 话题里的卡片和私聊区对齐; 不折叠，直接展示全文): the topic's
            # row, the answer whole, the parts to look up in one fold, the lines.
            "answer_card_thread_long": answer_card(
                "看板的颜色换好了：浅色那套和深色那套都换了。" + "原因是深色模式下旧颜色的对比度不够。" * 40,
                "T443 有结果 · 结论", tail(files, inputs, outputs, change=handed, notes=[acceptance_element(
                    {"what": "看板的按钮改成蓝色", "where": "", "how": "打开看板，点任一按钮"})]), [head]),
            # 追问只发答案卡 (docs/cards.md): a follow-up that only answered sent no task card; its 换配置 and 回退 go
            # under its answer (app/answer_delivery.py topic_controls).
            "answer_card_quick": answer_card(
                text, "T443 有结果 · 结论", [*tail(None, inputs, outputs), topic_panel(TOPIC, 443, ("claude", 2), rewind=True)],
                [head]),
            # The relay with what the owner answered as one line that opens, the row a link into the topic (the owner,
            # 2026-11-22: 回带卡对应上我的问题；进话题做到卡头上).
            "answer_relay_said": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, ask=ask_block(
                "格式 → 表格", verb="你答", at="14:22", url="https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x"
                "&open_thread_id=omt_x", who="T439 改看板的 Pin 目标", doing="done",
                parts=[("question", "用哪种格式输出？"), ("answer", "表格")]))[0],
            "relay_waiting": relay_card("T439", WAITING, question="用哪种格式输出？表格还是列表，表格方便对比、列表方便手机上看")[0],
            # 做完了 relays that came close together, as one (app/relay_send.py send_merged).
            "relay_merged": merged_card([{"label": "T439", "title": "改看板的 Pin 目标", "lead": "按钮已经改好",
                                          "url": thread_url},
                                         {"label": "T441", "title": "查一下昨晚的日志", "lead": "没有异常"}])[0],
            # T650: the record's words through core/failure.py (they were the status line, and no next step), the task card's
            # 检查并继续 and its follow-up under them; once clicked, the line of what it started in their place.
            "relay_stopped": stopped_relay(failure_code="timeout", status_message="本轮任务已超时。任务执行时间已到。"),
            "relay_stopped_unfinished": stopped_relay(failure_code="root_failed", status_message="任务异常（RuntimeError）"),
            "relay_stopped_limit": stopped_relay(failure_code="rate_limited"),
            "relay_stopped_quota": stopped_relay(failure_code="quota_exhausted"),
            "relay_stopped_started": (lambda built: replace_element(built, relay_stops.AREA_ID, relay_stops.line(
                "↪ 已接着做 · 任务 #440")) and built)(stopped_relay(failure_code="timeout")),
            # T669: a record with a binding offers 换个模型 too (its 换配置 card opens at the top level); once switched there,
            # the line of it in the buttons' place.
            "relay_stopped_limit_switch": stopped_relay(failure_code="rate_limited", harness_id="codex", binding_revision=2),
            "relay_stopped_quota_switch": stopped_relay(failure_code="quota_exhausted", harness_id="codex", binding_revision=2),
            "relay_stopped_switched": (lambda built: replace_element(built, relay_stops.AREA_ID, relay_stops.line(
                relay_stops.SWITCHED.format("：Claude · Opus 5.5"))) and built)(
                stopped_relay(failure_code="quota_exhausted", harness_id="codex", binding_revision=2)),
            # T669 E1: a 做完了 relay's 换配置, in a row of its own.
            "answer_relay_switch": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                              buttons=alone(relay_switch(TOPIC, 439, "codex", 2)))[0],
            "answer_card_asked": answer_card(text, "✅ 结论", tail(files, inputs, outputs), [ask]),
            "answer_relay_asked": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                             ask=ask)[0],
            "question_copy_asked": mirror_card(question_record(), "T439", "先说结论：两种都行。", ask=ask),
            # A project's topic asks at the top level: who asks, no words of the owner's (the lead handed it the work).
            "question_copy_member": mirror_card(question_record(), "T546", "设置卡已按你选的改法改完。",
                                                ask=ask_block("", who="T546 设置的使用逻辑梳理", doing="member")),
            # T658: its row opens the topic's thread at the question; settled, it says what the topic does (not 关于你的问题).
            "question_copy_member_linked": mirror_card(question_record(), "T546", "设置卡已按你选的改法改完。", ask=ask_block(
                "", who="T546 设置的使用逻辑梳理", doing="member",
                url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&openThreadId=omt_546")),
            "question_copy_member_settled": mirror_card(
                question_record("answered", continuation_job_id=432, delivery_state="running"), "T546", "设置卡已按你选的改法改完。",
                ask=ask_block("", who="T546 设置的使用逻辑梳理", doing="member",
                              url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&openThreadId=omt_546")),
            "answer_card_answered": answer_card(text, "✅ 结论", tail(files, inputs, outputs), [answered]),
            "answer_relay_answered": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                                ask=answered)[0],
            "question_copy_answered": mirror_card(question_record(), "T439", "先说结论：两种都行。", ask=answered),
            "answer_relay_tail": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files)[0],
            # Who speaks (docs/ui.md 5.3 speaker): a topic's relay, the butler's answer, a lead's wake-up turn (no 你问).
            "answer_relay_speaker": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                               ask=ask_block("看板改成彩色块", at="12:31", url="https://applink.feishu.cn/t/439",
                                                             who="T439 改看板的颜色", doing="done"))[0],
            # A long reply with a table and a change, folded under its conclusion (cards/relay_card.py reply_fold), the title bar
            # of its topic, the small line with the way back and into the topic; the same for a lead; the butler's below.
            "answer_relay_long": relay_card(
                "T439", DONE, text="**结论**：按钮已经改好。\n\n" + "\n".join(f"- 第 {n} 处：改了名字和颜色，旧卡点了回一句" for n in range(12))
                + "\n\n| 项 | 结果 |\n| --- | --- |\n| 单测 | 通过 |\n| 真机 | 待验收 |", inputs=inputs, outputs=outputs,
                files=files, change=handed, url=thread_url,
                ask=ask_block("看板改成彩色块", at="12:31", url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=1",
                              who="T439 改看板的颜色", doing="done", avatar="img_v3_fake_topic"))[0],
            "answer_card_lead_speaker": answer_card(text, "专项「发布」· 负责人 · 结论", tail(None, inputs, outputs), [
                ask_block("发布的事怎么样了", at="12:31", url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=2",
                          who="专项「发布」· 负责人", doing="answer", avatar="img_v3_fake_lead")]),
            # The question first, the turn's output folded under it, its way back at the end (T439 asks, the owner not answered).
            "question_copy_waiting": mirror_card(
                question_record(), "T439", "先说结论：两种都行。\n\n" + "\n".join(f"- 第 {n} 处：改了名字和颜色" for n in range(8)),
                ask=ask_block("选哪种样式", at="09:12", who="T439 改看板的颜色", doing="waiting", avatar="img_v3_fake_topic",
                              url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=3"), files=files),
            "question_copy_settled": mirror_card(
                question_record("answered", continuation_job_id=432, delivery_state="running"), "T439",
                "先说结论：两种都行。", ask=ask_block("选哪种样式", at="09:12", who="T439 改看板的颜色", doing="waiting",
                                                  url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=3")),
            # T638 犹抱琵琶半遮面: one long question answered, its opening in view and the rest going on to the answer; a
            # long reply of prose, its first lines in view, the rest folded.
            "question_copy_settled_one": mirror_card(
                {**question_record("answered", continuation_job_id=432, delivery_state="running"),
                 "questions": [{"id": "q1", "header": "评分", "question": "按新办法重排：每页把三组草图和参考图放在一起，"
                                "先看每张草图的配色对不对，再看各组之间是否一致，最后写进 report.md。用新的评分表来判吗？",
                                "options": [{"label": "用新评分表"}, {"label": "用旧评分表"}]}],
                 "answers": {"q1": {"answers": ["用新评分表（推荐）"]}}}, "T52",
                "先说结论：三组草图都重看了一遍。" * 12, ask=ask_block("选哪种评法", at="01:49", who="T52 整理设计稿", doing="waiting",
                                                       url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=4")),
            "answer_relay_prose": relay_card(
                "T53", DONE, text="看板的颜色还没换成：浅色那套换好了，深色那套还没有。" + "原因是深色模式下旧颜色的对比度不够。" * 12,
                inputs=inputs, outputs=outputs, url=thread_url,
                ask=ask_block("换看板颜色", at="01:41", who="T53 统一看板和卡片的配色", doing="done"))[0],
            # 11:02: everything under the answer but 输入/输出 in one fold whose title names the parts.
            "answer_relay_details": relay_card(
                "T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files, change=handed, url=thread_url,
                notes=[acceptance_element({"what": "看板的按钮改成蓝色", "where": "", "how": "打开看板，点任一按钮"})])[0],
            "answer_card_butler": answer_card(text, "✅ 结论", tail(files, inputs, outputs),
                                              [ask_block("看板改成彩色块，字大一点，颜色用暖色，再把图例挪到右边去", at="12:31",
                                                         who="管家", avatar="img_v3_fake_butler")]),
            # The speaker's sentence by what it did (cards/answer_card.py says, 样卡 B): a relay waiting, a stopped one.
            "ask_speaker_waiting": ask_block("选哪种样式", verb="你答", at="09:12", url="https://applink.feishu.cn/t/439",
                                             who="T439 改看板的颜色", doing="waiting"),
            "ask_speaker_stopped": ask_block("看板改成彩色块", at="09:12", who="T439 改看板的颜色", doing="stopped"),
            "answer_card_lead": answer_card(text, "✅ 结论", tail(files, inputs, outputs),
                                            [ask_block("", at="12:40", who="专项「Covey 体验」· 负责人", doing="report")]),
            "answer_tail_card": tail_card(tail(files, inputs, outputs), "T439 · 产物与详情",
                                          "上面是这一轮的回答，产物和详情在这里"),
            # A turn that changed code: its 改动 folded under 产物 (cards/answer_card.py change_element).
            "answer_card_delivery": answer_card(text, "✅ T443 · 结论", tail(files, inputs, outputs, change=handed),
                                                [head]),
            "answer_relay_delivery": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                                change=handed)[0],
            # #22: the topic's completion criteria its turn did not meet (core/criteria.py unmet), above the box.
            "answer_card_criteria": answer_card(text, "✅ T443 · 结论", tail(
                files, inputs, outputs, change=handed, notes=[criteria_element([
                    ("没满足", "合进 main 并推送"), ("没查", "在手机上点开看过，按钮颜色对")])]), [head]),
            # #21: 改了什么 / 去哪看 / 怎么验 of a UI change (the checkpoint's acceptance), with a link and without one.
            "answer_card_acceptance": answer_card(text, "✅ T443 · 结论", tail(
                files, inputs, outputs, change=handed, notes=[acceptance_element({
                    "what": "看板的按钮改成蓝色，点了回一句", "where": "https://example.invalid/board",
                    "how": "打开看板，点任一按钮：按钮是蓝色，点完卡上回“收到”"})]), [head]),
            "answer_card_acceptance_local": answer_card(text, "✅ T443 · 结论", tail(
                files, inputs, outputs, notes=[acceptance_element({"what": "看板的按钮改成蓝色", "where": "",
                                                                   "how": "本地起页面看过按钮颜色"})]), [head]),
            # #18: a commit the repository of the turn's workdir lacks (app/answer_cards.py absent_commits), marked.
            "answer_card_delivery_mismatch": answer_card(text, "✅ T443 · 结论", tail(
                files, inputs, outputs, change={**handed, "not_found": ["a6bc148d38ce8c230e96e"]}), [head]),
            # A release before 2026-11-09 kept the line as a string: one grey 改动 line.
            "answer_relay_delivery_string": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs,
                                                       delivery="已提交 a6bc148 · 已部署 第 322 代 · 未验收")[0]}


def main_topic_cards():
    from covey.cards.topic_card import topic_card

    config = ("Claude Code", "Opus 5.5", "思考预算 high", "工作区 work", "原目录")
    return {"unnumbered": topic_card("查昨晚的失败日志", why="日志在这个仓库，只读", config=config),
            "numbered": topic_card("查昨晚的失败日志", label="T412", why="日志在这个仓库，只读", config=config),
            "bare": topic_card("查昨晚的失败日志", label="T412")}


def main_task_cards():
    """The butler's slim task card (core/butler.py Traits.slim_card) as it is sent, and as its writes
    leave it: queued and held, running, a direct answer, a turn that opened and handed over topics, stopped with
    the supplements it took, failed with its 重试 row. The writes are drawn by the builders the Bridge writes with."""
    from covey.app.butler_reply import DROPPED, reply_fold, routed_body
    from covey.cards.elements import md
    from covey.cards.recovery import result_actions
    from covey.cards.review import render_stream_final, render_stream_status
    from covey.cards.streaming import ACTION_ROW_ELEMENT_ID, SLIM_DETAILS_ID, build_stream_card
    from covey.cards.task_figures import IO_BOX_ID
    from covey.cards.task_status import status_elements
    from covey.core import butler

    run = stream_runtime()
    snapshot = {"started_at": FROZEN - 13, "ended_at": FROZEN, "input_tokens": 52000, "output_tokens": 900,
                "measured_output_tokens": 800, "generation_seconds": 10.0, "steps": {}}

    def job():
        # Who speaks, as app/cards.py speaker_facts gives it when the card is made: its first row (T667).
        return sample_job(topic_id=butler.butler_topic_id("chat"), topic_label="", harness_id="claude",
                          user_text="帮我看看昨晚的失败日志", title="",
                          card_speaker={"who": "管家", "avatar": "img_v3_fake_butler"})

    def ended(body, status, *, under=(), inside=()):
        card = build_stream_card(" ", "任务", run, job=job(), include_stop_action=True)
        elements = card["body"]["elements"]
        at = {element.get("element_id"): index for index, element in enumerate(elements)}
        elements[at["answer"]]["content"] = body
        elements[at["activity"]] = status_elements(status, False, True)[0]
        details = elements[at[SLIM_DETAILS_ID]]["elements"]
        details[:] = [ended_box(job(), snapshot, 13) if e.get("element_id") == IO_BOX_ID else e
                      for e in details if e.get("element_id") != ACTION_ROW_ELEMENT_ID]
        details[1:1] = list(inside)  # After the 输入/输出 box (task_card.body_anchor).
        elements[at["answer"] + 1:at["answer"] + 1] = list(under)  # Right under the body (task_card.under_body).
        return card

    with mock.patch.dict(os.environ, {butler.ENV: "1"}):
        lines = ["↪ 开了 **T410**「查昨晚的失败日志」· work · 只读", "↪ 转给 **T388**「迁移数据」：顺便把日志也看一下"]
        bare = SimpleNamespace(job_id=431, progress=SimpleNamespace(slim=True))
        return {
            "queued": build_stream_card(render_stream_status(job(), "已入队"), "任务", run, job=job(),
                                        include_stop_action=True),
            "held": build_stream_card(render_stream_status(job(), butler.HOLD_STATUS), "任务", run, job=job(),
                                      include_stop_action=True),
            "running_status": status_elements("执行中 · 已运行 6 秒 · 阶段：读日志\n当前：Read bridge.log", False, True),
            "done": ended(render_stream_final(bare, "昨晚失败是**磁盘满**。", bare=True), "有结果 · 用时 13 秒"),
            "routed": ended(render_stream_final(bare, routed_body(lines), bare=True),
                            "有结果 · 用时 13 秒", inside=[reply_fold("开了 T410，转给 T388。")]),
            "stopped": ended("已保存的结果和记录保留在私聊里。", "⏹ 已停止 · 用时 13 秒",
                             under=[md("**这一轮停了，期间你发的 1 条补充也一起停了**，不会自动重发；要的话改一改再发：\n"
                                       "- 14:36 · 看看这两个样本", element_id=DROPPED)]),
            "failed": ended("任务 #431 处理出错（RuntimeError），未满足结果发布条件。", "停住了 · 用时 13 秒",
                            under=[result_actions(butler.butler_topic_id("chat"), 431, "检查并继续")]),
        }


def ended_box(job, snapshot, took):
    """The 输入/输出 box of ``job``'s card once its run ended (app/task_details.py _io_figures writes its 输出)."""
    from covey.cards.answer_card import output_lines, output_parts
    from covey.cards.task_figures import io_box
    return io_box(job, output_lines(output_parts(snapshot, took)))


def status_cards_samples():
    """The topic status card the pinned board pins (cards/status_card.py), one per state: a core/look.py STATES key, its
    word and colour as the 看板 and 手上的事 say it (P0-1, T647)."""
    from covey.cards.status_card import status_card
    return {
        "running": status_card("T419 · 讨论 Pin 看板是否对前台话题关闭", "running", "第 9 轮 · 14:25 开始 · 阶段：看图",
                               "这一轮：这个 pin 显示上也有问题"),
        "waiting": status_card("T404 · 排查群晖 Btrfs", "waiting", "14:12 · 第 3 轮", "问题：把第三版说明发给 Codex？"),
        # A result not seen yet (有结果, green), and once seen (做完, grey).
        "done": status_card("T432 · 调研 Mac mini <雷电5>", "result", "13:01 · 2 轮 · 用时 5 分 49 秒", "结论：没找到 *完全* 对口的实测"),
        "seen": status_card("T432 · 调研 Mac mini <雷电5>", "done", "13:01 · 2 轮 · 用时 5 分 49 秒", "结论：没找到 *完全* 对口的实测"),
        # 停住了 (red: failed, interrupted, stopped by the lead) and the owner's own stop (已停止, grey).
        "stopped": status_card("T433 · 迁库", "stopped", "13:05 · 3 轮 · 用时 9 分 2 秒", "原因：任务异常（transport_error）"),
        "cancelled": status_card("T434 · 查日志", "cancelled", "13:07 · 1 轮 · 用时 40 秒", "原因：用户停止了任务"),
        "bare": status_card("T7", "queued", "第 1 轮", ""),
        # The checkpoint's steps in place of its phase (app/status_cards.py lines).
        "running_steps": status_card("T445 · 检查点里的步骤与交付进度", "running", "第 2 轮 · 14:25 开始 · 第 3/5 步：改卡片",
                                     "这一轮：长任务看得出走到第几步"),
        # With its estimate as a time of day (core/progress.py finish_words), and one that may be stuck (可能卡住 in its tag).
        "running_estimate": status_card("T484 · 进度和预计时间", "running",
                                        "第 3 轮 · 14:25 开始 · 第 3/5 步：改卡片 · 预计 15:20 左右做完", "这一轮：加上进度"),
        "running_stuck": status_card("T484 · 进度和预计时间", "stuck", "第 3 轮 · 14:25 开始 · 阶段：跑全量", "这一轮：加上进度"),
        # A long command still running: the row says the turn waits for it, in the running colour (core/progress.py board_words).
        "running_command": status_card("T484 · 进度和预计时间", "running", "第 3 轮 · 14:25 开始 · 阶段：跑全量 · 在等一条命令跑完",
                                       "这一轮：加上进度"),
    }


# As app/launch_models.py available_models names them: each with its provider (core/providers.py).
CLAUDE_OWN = {"provider": "anthropic", "provider_title": "Claude 自带"}
BUTLER_CHOICES = {"claude:claude-opus-5-5": {"harness": "claude", "model": "claude-opus-5-5", "title": "Claude Code",
                                          "display_name": "Opus 5.5", "levels": ["low", "high", "xhigh", "max"],
                                          "redaction": True, **CLAUDE_OWN},
               "claude:claude-sonnet-5-5": {"harness": "claude", "model": "claude-sonnet-5-5", "title": "Claude Code",
                                            "display_name": "Sonnet 5.5", "levels": ["low", "high"], "redaction": True,
                                            **CLAUDE_OWN},
               "codex:gpt-6": {"harness": "codex", "model": "gpt-6", "title": "Codex", "display_name": "GPT-6",
                               "levels": ["low", "medium", "high", "xhigh"], "provider": "openai",
                               "provider_title": "Codex 自带"},
               "codex:acme/gpt-6-astra": {"harness": "codex", "model": "acme/gpt-6-astra", "title": "Codex",
                                            "provider": "acme", "provider_title": "Acme",
                                            "display_name": "GPT-6 Astra", "levels": ["high"]}}
# The same menu as the Bridge marks it (app/launch_models.py): Claude Code's and Codex's models take the Bridge's tools.
LEAD_CHOICES = {key: {**entry, "bridge_tools": True} for key, entry in BUTLER_CHOICES.items()}


def butler_panels():
    from covey.cards.butler_panel import butler_panel_card
    from covey.core.butler_settings import Settings, TopicDefaults

    names = ["bridge", "docs", "scratch"]
    changed = Settings(enabled=False, model="claude-opus-5-5", effort="high", compact_tokens=300_000, redaction=True,
                       topic_defaults=TopicDefaults(harness="codex", model="gpt-6", effort="xhigh", run_mode="worktree",
                                                    workspace="bridge"),
                       workspace_notes=(("bridge", "Harness Bridge 本体"), ("docs", "文档")))
    follows = {"t_harness": "跟随上次：Claude Code · Opus · max", "t_model": "跟随上次：Opus · max",
               "t_effort": "Bridge 默认：high"}
    return {"default": butler_panel_card("b" * 24, {"revision": 1}, Settings(), BUTLER_CHOICES, names),
            # Empty defaults say what they resolve to now (app/butler_panel.py follows).
            "follows": butler_panel_card("b" * 24, {"revision": 2}, Settings(), BUTLER_CHOICES, names,
                                              follows=follows),
            "follows_model": butler_panel_card(
                "b" * 24, {"revision": 3}, Settings(topic_defaults=TopicDefaults(harness="claude")), BUTLER_CHOICES,
                names, follows=follows),
            "changed": butler_panel_card("b" * 24, {"revision": 4, "notice": "⚠️ 只有机器人主人能改管家的设置。"},
                                              changed, BUTLER_CHOICES, names)}


def doctor_report(**service):
    """service/doctor.py diagnose's report of service k1 (and one more in the root), for the 诊断 tab's samples."""
    row = {"id": "k1", "desired_state": "running", "status": "ready", "alive": True, "generation": 463,
           "bridge": {"status": "ready", "remote_connection": {"status": "receiving"}},
           "events": {"status": "ready", "owned_required_consumers": True, "dropped": 0}, **service}
    return {"status": "ready", "runtime_status": "ready", "boot": {"status": "ready", "mode": "systemd-user"},
            "manager": {"lag": 0.42}, "services": [row, {"id": "k2", "desired_state": "running", "status": "ready"}],
            "harness_probe": {"claude": {"ok": True, "reason": "ok", "checked_at": FROZEN - 3600},
                              "codex": {"ok": False, "reason": "network", "checked_at": FROZEN - 120,
                                        "advice": "连不上模型服务（网络或代理问题）：检查出口，必要时设置 HTTPS_PROXY 后重新注册服务"}}}


def doctor_trouble():
    """doctor_report with every part that needs a look: no Manager, the connection stalled, events dropped, a permission
    variable kept, start at boot broken, no probe yet."""
    service = {"status": "degraded", "permission_env": ["LARK_BRIDGE_CODEX_SANDBOX=read-only"],
               "bridge": {"status": "degraded", "remote_connection": {"status": "stalled"}},
               "events": {"status": "degraded", "owned_required_consumers": False, "dropped": 2}}
    return {**doctor_report(**service), "manager": None, "boot": {"status": "degraded", "mode": "cron"}, "harness_probe": {}}


def settings_cards():
    """The 设置 card on each tab (cards/settings_hub.py): the panel's card with the tab row on top; the butler's
    two pages, 新话题 and 管家 (its question before 每条直接开一个话题, and in the mode 纯话题; no 工作区 or 运行方式 row, v43), 仓库
    (the home and the repositories, read only: with the web page's link, without it, and with none), 脱敏 and 诊断. 代答 is an older card's
    tab; a record on the retired 工作区 or 其他 folds as 仓库."""
    from covey.app.doctor_panel import summarize
    from covey.cards.doctor_card import doctor_card
    from covey.cards.group_manager import group_manager_card
    from covey.cards.butler_panel import butler_panel_card
    from covey.cards.proxy_panel import proxy_panel_card
    from covey.cards.redaction_panel import redaction_card
    from covey.cards.settings_hub import folded_hub, tab_row
    from covey.cards.workspace_manager import repos_card
    from covey.core.butler_settings import AssistChoice, LeadModel, Settings

    tabs = ["config", "repos", "redaction", "groups"]
    ASSIST_ROW = {"options": [("auto", "自动（跟随管家）"), ("claude", "Claude Code"), ("codex", "Codex"),
                              ("none", "不用模型（只按规则）")],
                  "current": "auto", "words": "现在：跟随管家，Claude Code · 当前登录账号的模型，分流、整理专项、记忆自己长都用它。"}
    ASSIST_NAMED = {**ASSIST_ROW, "current": "codex", "words": "现在：Codex · gpt-6 · high，分流、整理专项、记忆自己长都用它。"}

    def row(tab):
        return [tab_row(tab, "c" * 24, tabs)]

    settings = {"enabled": True, "group_chat_id": "oc_fake_proxy", "harness": "codex", "model": "gpt-6-astra",
                "effort": "xhigh", "user_profile": "me", "web_python": "/absolute/python", "workspace": "project"}
    person = {"key": "p2p:ou_fake_z", "kind": "p2p", "target": "ou_fake_z", "name": "张三", "paused": False}
    groups = [{"chat_id": f"oc_fake_group{i}", "active": i % 3 != 0, "external": False} for i in range(4)]
    view = {"revision": 1, "hub": True}
    repos = [{"name": "bridge", "status": "active", "note": "Bridge 的代码"}, {"name": "docs", "status": "readonly", "note": ""},
             {"name": "legacy", "status": "frozen", "note": "只读归档"}]

    return {
        # 配置 (T619): the butler's settings on one page, a section open at a time (新话题 first; 管家 from the butler's
        # tool).
        "config": butler_panel_card("c" * 24, view, Settings(), BUTLER_CHOICES, ["bridge", "docs"], top=row("config"),
                                    page="config"),
        "config_butler": butler_panel_card("c" * 24, {**view, "open": "butler"}, Settings(), BUTLER_CHOICES,
                                           ["bridge", "docs"], top=row("config"), page="config"),
        # T700: 专项负责人 with COVEY_PROJECTS=1, following the butler or a model chosen for it; its Harness row lists the
        # Harnesses whose models take the Bridge's tools.
        "config_lead": butler_panel_card("c" * 24, {**view, "open": "lead"}, Settings(), LEAD_CHOICES,
                                         ["bridge", "docs"], top=row("config"), page="config", lead_on=True),
        "config_lead_chosen": butler_panel_card(
            "c" * 24, {**view, "open": "lead"},
            Settings(lead=LeadModel(harness="codex", model="gpt-6", effort="high")),
            LEAD_CHOICES, ["bridge", "docs"], top=row("config"), page="config", lead_on=True),
        # T884: 辅助模型, a section of its own (the owner's choice A), shown with the butler off too.
        "config_assist": butler_panel_card("c" * 24, {**view, "open": "assist"}, Settings(), BUTLER_CHOICES,
                                           ["bridge", "docs"], top=row("config"), page="config", assist=ASSIST_ROW),
        # T924: one setting for every background call; a Harness named there with its 供应商, 模型 and 思考预算 as 专项负责人's.
        "config_assist_named": butler_panel_card(
            "c" * 24, {**view, "open": "assist"}, Settings(assist=AssistChoice(harness="codex", model="gpt-6", effort="high")),
            LEAD_CHOICES, ["bridge", "docs"], top=row("config"), page="config", assist=ASSIST_NAMED),
        "config_assist_closed": butler_panel_card("c" * 24, view, Settings(enabled=False), BUTLER_CHOICES,
                                                  ["bridge", "docs"], top=row("config"), page="config", assist=ASSIST_ROW,
                                                  butler_on=False, butler_allowed=False),
        # U4 (T898, the owner's choice B): the rows nobody changes in their section's 更多, open after a click on one.
        "config_butler_more": butler_panel_card("c" * 24, {**view, "open": "butler", "more": True}, Settings(), BUTLER_CHOICES,
                                                ["bridge", "docs"], top=row("config"), page="config"),
        "config_confirm": butler_panel_card("c" * 24, {**view, "confirm": True}, Settings(), BUTLER_CHOICES,
                                            ["bridge", "docs"], top=row("config"), page="config"),
        "config_topics": butler_panel_card("c" * 24, {**view, "open": "butler"}, Settings(enabled=False), BUTLER_CHOICES,
                                           ["bridge", "docs"], top=row("config"), page="config"),
        # A record still on a retired page (管家, 新话题, 模型) is drawn as 配置.
        "main_retired": butler_panel_card("c" * 24, view, Settings(), BUTLER_CHOICES, ["bridge", "docs"],
                                          top=row("main"), page="main"),
        # 仓库 (cards/workspace_manager.py repos_card): the home, the count and the web page's link; without the page the
        # repositories with their states and 用途; with none.
        "repos": repos_card("c" * 24, view, home="/absolute/home", items=repos, top=row("repos")),
        "repos_web": repos_card("c" * 24, view, home="/absolute/home", items=repos, link="https://web.example/settings/repos",
                                top=row("repos")),
        "repos_empty": repos_card("c" * 24, view, home="/absolute/home", items=[], top=row("repos")),
        "repos_notice": repos_card("c" * 24, {**view, "notice": "⚠️ 只有机器人主人能改设置。"}, home="/absolute/home", items=repos[:1],
                                   top=row("repos")),
        # 脱敏 (cards/redaction_panel.py): no term, only its first character and length.
        "redaction": redaction_card("c" * 24, {**view, "notice": "已删除「机··（4 字）」。", "undo": True},
                                    redaction_facts(), top=row("redaction")),
        "redaction_found": redaction_card("c" * 24, {**view, "found": ["b" * 12]}, redaction_facts(),
                                          top=row("redaction")),
        "redaction_pages": redaction_card("c" * 24, {**view, "at": 1}, redaction_facts(rows=12), top=row("redaction")),
        # The 开关 fold's probe, the upstream box and a try-out open (no form may sit in a fold).
        "redaction_folds": redaction_card(
            "c" * 24, {**view, "open": ["try", "upstream"], "probe": {"upstream": "https://api.example.com",
                                                                     "source": "默认"},
                       "preview": {"redacted": "我们的 RXFAKE000 上线了\n第二行", "count": 1, "terms": 1,
                                   "restore": True, "same": True}},
            redaction_facts(upstream="https://gw.example.com/v1"), top=row("redaction")),
        "redaction_probing": redaction_card("c" * 24, {**view, "probing": FROZEN}, redaction_facts(),
                                            top=row("redaction")),
        # A view of the 2026-11-13 layout (page, edit): its card is drawn in the new one.
        "redaction_old_view": redaction_card("c" * 24, {**view, "page": "records", "edit": "b" * 12},
                                             redaction_facts(), top=row("redaction")),
        "redaction_idle": redaction_card("c" * 24, view, redaction_facts(rows=0), top=row("redaction")),
        "redaction_unreadable": redaction_card("c" * 24, view, {**redaction_facts(), "error": "词表文件读不了或有错。"},
                                               top=row("redaction")),
        "proxy": proxy_panel_card("c" * 24, view, settings=settings, contacts=[person], batches=[], choices=CHOICES,
                                  current=("codex:gpt-6-astra", "xhigh"), top=row("proxy")),
        "groups": group_manager_card("c" * 24, {**view, "page": 0}, groups, {"oc_fake_group0": "群0"},
                                     top=row("groups")),
        # Folded by a click on the tab shown or the menu's 设置: the panel's title bar and the tab row.
        "config_folded": folded_hub("config", "c" * 24, tabs),
        "main_folded": folded_hub("main", "c" * 24, tabs),  # A record still on 管家 folds as 配置.
        "proxy_folded": folded_hub("proxy", "c" * 24, tabs),
        "groups_folded_no_main": folded_hub("groups", "c" * 24, [t for t in tabs if t != "main"]),
        "repos_folded": folded_hub("repos", "c" * 24, tabs),
        "workspace_folded": folded_hub("workspace", "c" * 24, tabs),  # A record still on the retired 工作区 folds as 仓库.
        # A record still on the retired 其他 folds as 仓库.
        "other_folded": folded_hub("other", "c" * 24, tabs),
        **memory_tab_cards(), **about_tab_cards(),
        # 诊断 (cards/doctor_card.py): service/doctor.py's report in words, read-only; one where every part needs a look.
        "doctor": doctor_card("c" * 24, view, summarize(doctor_report(), "k1", FROZEN - 300),
                              top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        # The switches the service has set (core/switches.py), a row of its own after the service's.
        "doctor_switches": doctor_card("c" * 24, view, summarize(doctor_report(switches=[
            ["COVEY_BUTLER_TRACKS", "1"], ["COVEY_DIGEST_TIMES", "09:30,21:30"], ["COVEY_WEB_CONFIG", "已设"]]), "k1", FROZEN - 300),
                                       top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        # Claude Code keeps few days of sessions (core/claude_retention.py): to know, with the fix; and fine at 90 days or more.
        "doctor_cleanup_low": doctor_card("c" * 24, view, summarize(doctor_report(claude_cleanup={
            "state": "missing", "days": 30, "path": "/absolute/claude/settings.json",
            "hint": "Claude Code 只保留最近 30 天的会话，更早的话题不能接着做、也不能回退一步；想留久一点，请自己在 "
                    "/absolute/claude/settings.json 里加一行 \"cleanupPeriodDays\": 3650（Covey 不会替你改）"}), "k1", FROZEN - 300),
                                          top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_cleanup_ok": doctor_card("c" * 24, view, summarize(doctor_report(claude_cleanup={
            "state": "ok", "days": 3650, "path": "/absolute/claude/settings.json"}), "k1", FROZEN - 300),
                                         top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_problems": doctor_card("c" * 24, view, summarize(doctor_trouble(), "k1", FROZEN - 60),
                                       top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_failed": doctor_card("c" * 24, view, {"checked_at": FROZEN, "error": "OSError"},
                                     top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_folded": folded_hub("doctor", "c" * 24, tabs + ["doctor"]),
    }


FULL_TABS = ["config", "repos", "redaction", "groups", "memory", "nodes", "doctor"]


def memory_tab_cards():
    """The 设置 card's 记忆 tab (cards/memory_panel.py, COVEY_MEMORY=1): one line of counts (全局 · 专项 · 话题 · 待审核), 待审核 each
    with what it is and what 采纳 and 不要 do, and without the web page the rules to 收起, a topic's too (one asking 确认收起);
    with the web page 打开网页 and no rules; no memory at all."""
    from covey.cards.memory_panel import memory_card
    from covey.cards.settings_hub import tab_row

    counts = {"rule": 5, "decision": 3, "note": 8, "pending": 4}
    scopes = [{"key": "global", "title": "全局", "counts": {"rule": 2, "decision": 0, "note": 3, "pending": 1}},
              {"key": "topics", "title": "话题", "counts": {"rule": 1, "decision": 0, "note": 2, "pending": 0}},
              {"key": "p:acc0unt1", "title": "账号额度运行台", "counts": {"rule": 2, "decision": 3, "note": 3, "pending": 3}}]
    review = [{"key": "review:learn:a1b2c3", "type": "learn", "title": "回答先给结论", "text": "你多次说过：回答先给结论，细节放后面",
               "suggest": {"kind": "rule", "scope": "global"}},
              {"key": "review:lead:d4e5f6", "type": "lead", "title": "额度低于 10% 先停新话题", "text": "负责人总结：额度低于 10% 先停新话题",
               "suggest": {"kind": "rule", "scope": "project", "project_key": "acc0unt1"}},
              {"key": "review:migrate:a0b1c2", "type": "migrate", "title": "发版前跑全量", "text": "发版前跑全量",
               "suggest": {"kind": "rule", "scope": "project", "project_key": "acc0unt1"}}]
    rules = [{"key": "global:p00000c", "code": "R3K9M", "title": "回答先给结论", "scope": "global", "over_cap": False, "project_key": ""},
             {"key": "global:p0000f0", "code": "R7T2A", "title": "测试通过就合并进 main 并推送", "scope": "global", "over_cap": True,
              "project_key": ""},
             {"key": "project:acc0unt1:a1b2c3", "code": "R5H8Q", "title": "额度低于 10% 先停新话题", "scope": "project", "over_cap": False,
              "project_key": "acc0unt1"},
             {"key": "topic:t0a1:r1b2c3", "code": "R2N6W", "title": "这个话题只读 demo-repo", "scope": "topic", "over_cap": False,
              "project_key": "", "topic_number": 12}]
    base = {"counts": counts, "rules_by": {"global": 2, "project": 2, "topic": 1}, "scopes": scopes, "review": review, "rules": rules}
    web = {**base, "url": "https://covey.example.test/memory"}
    local = {**base, "url": ""}
    empty = {"counts": {"rule": 0, "decision": 0, "note": 0, "pending": 0}, "rules_by": {}, "scopes": scopes[:2], "review": [],
             "rules": [], "url": ""}
    view = {"revision": 3, "hub": True, "token": "c" * 24, "review_page": 0, "rules_page": 0}
    row = [tab_row("memory", "c" * 24, FULL_TABS)]
    return {"memory": memory_card("c" * 24, view, local, top=row),
            "memory_open": memory_card("c" * 24, {**view, "confirm": "global:p0000f0", "notice": "已采纳：回答先给结论"}, local, top=row),
            "memory_web": memory_card("c" * 24, view, web, top=row),
            "memory_empty": memory_card("c" * 24, view, empty, top=row)}


def about_tab_cards():
    """The 设置 card's 能做什么 part under the 诊断 report (cards/about_panel.py; T604 #62, T647 D6, T898), read only: three
    groups by what the owner does with them, each line what it does for him first, on with its switch, off grey with how to
    get it; without Claude Code there is no butler and it says how to get one."""
    from covey.app.about_panel import FEATURES, GROUPS
    from covey.cards.about_panel import about_elements
    from covey.cards.doctor_card import doctor_card
    from covey.cards.settings_hub import tab_row
    from covey.cards.welcome import BUTLER_WORDS, NO_CLAUDE

    off = {"早晚汇总": "设 COVEY_DIGEST_TIMES=09:30,21:30（按北京时间）", "手上的事": "设 COVEY_PINNED_OVERVIEW=1",
           "同时做几件事": "设 COVEY_BUTLER_TRACKS=1（要有管家）", "派发卡": "设 COVEY_DISPATCH_CARD=1",
           "代答": "要一个带代答发送器的扩展（核心没有），再设 COVEY_PROXY_PROFILE",
           "新话题带上最近话题的要点": "设 COVEY_WORKSPACE_GISTS=3（话题要点要整理专项开着才会写）",
           "网页工作台": "要一个配套的网页应用，再设 COVEY_WEB=1 和 COVEY_WEB_CONFIG", "历史搜索": "设 COVEY_HISTORY_CMD=搜索命令的绝对路径",
           "设备与机器状况": "设 COVEY_NODE_LISTEN=设备能连进来的监听地址"}
    details = {"敏感词脱敏": ("词表在「脱敏」，话题开了才替换", ""), "管家": ("", "COVEY_BUTLER"), "专项": ("", "COVEY_PROJECTS"),
               "看板": ("", "COVEY_BOARD"), "记忆": ("", "COVEY_MEMORY、COVEY_KNOWLEDGE"),
               "负责人自己往下推": ("", "COVEY_PROJECT_AUTOWAKE 等四个"), "限流后自动继续": ("", "COVEY_AUTO_RETRY_LONG")}
    features = [{"name": name, "group": group, "good": good, "on": name not in off,
                 "line": off.get(name) or details.get(name, ("", ""))[0], "switch": details.get(name, ("", ""))[1]}
                for name, group, good, _ in FEATURES]
    view = {"host": "devbox", "harnesses": ["Claude Code", "Codex"], "down": [["Codex", "没登录"]],
            "groups": [list(group) for group in GROUPS], "features": features, "extensions": [], "state_dir": "/absolute/state"}
    why, how = BUTLER_WORDS[NO_CLAUDE]
    no_butler = [{**row, "on": False, "line": f"{why}；{how}"} if row["name"] in ("管家", "回退一步") else row for row in features]
    top, data = [tab_row("doctor", "c" * 24, FULL_TABS)], {"checked_at": FROZEN, "error": "OSError"}
    return {"doctor_about": doctor_card("c" * 24, {"token": "c" * 24, "hub": True}, data, top=top,
                                        below=about_elements(view, heading=True)),
            "doctor_about_no_butler": doctor_card("c" * 24, {"token": "c" * 24, "hub": True}, data, top=top, below=about_elements(
                {**view, "harnesses": ["Codex"], "down": [], "features": no_butler, "extensions": ["covey_internal"]}, heading=True))}


def overview_cards():
    from covey.cards.overview import overview_card

    # The board's index (P0-1, P0-2, T647): 要你处理 as the 看板 counts it (core/board_read.py Asks), 停住了 a part of its own,
    # 可能卡住 right after it, the owner's own stop 已停止 apart from 有结果.
    board = {"counts": {"waiting": 2, "stopped": 2, "stuck": 1, "result": 2, "running": 10}, "pinned": 10,
             "waiting": [{"label": "T396", "title": "你为啥没有pin？", "ask": "置顶汇总卡", "since": FROZEN - 180}],
             "stopped": [{"label": "T394", "title": "群晖公钥", "status": "failed", "at": FROZEN - 600, "tone": "stopped",
                          "line": "测试没过：test_web_sync 2 个失败；停在：跑全量", "url": "https://applink.feishu.cn/t/394"},
                         {"label": "专项「迁库」", "title": "", "topic": "lead", "project": "a1b2c3d4e5f6", "tone": "stopped",
                          "at": FROZEN - 1200, "line": "token 用完了 · 在看板上点「继续」接着做"}],
             "result": [{"label": "T392", "title": "看板", "status": "done", "at": FROZEN - 800, "seen": False,
                         "tone": "result", "line": "改好了"}],
             "cancelled": [{"label": "T393", "title": "日志", "status": "cancelled", "at": FROZEN - 900, "seen": True,
                            "tone": "cancelled", "line": "被停止了"}],
             "running": [{"label": "T391", "title": "logid", "harness": "Codex", "workspace": "DataLab",
                          "status": "queued", "started": None, "phase": "", "stuck": False},
                         {"label": "T390", "title": "重构", "harness": "Claude Code", "workspace": "demo-app",
                          "status": "running", "started": FROZEN - 3900, "phase": "跑全量",
                          "progress": ["预计 19:40 左右做完"], "stuck": False},
                         {"label": "T389", "title": "跑评测", "harness": "Codex", "workspace": "DataLab",
                          "status": "running", "started": FROZEN - 7200, "phase": "跑评测", "progress": ["可能卡住了"],
                          "stuck": True}],
             "stale": [{"label": f"T{n}", "title": "旧", "topic": f"topic-{n}", "ask": "补充信息",
                        "since": FROZEN - 2 * 86400} for n in (380, 381)],
             "dismissed": ["T401", "T402"]}
    undo = {**board, "undo": {"topic-401": FROZEN - 60, "topic-402": FROZEN - 60}}  # 撤销最近一次 beside the line.
    empty = {"counts": {"waiting": 0, "result": 0, "running": 0}, "pinned": 0, "waiting": [], "result": [],
             "running": [], "stale": []}
    view = {"waiting": [], "running": [], "ended": []}
    # The butler's question waits under 等你 too, under its speaker (core/board_read.py top_questions).
    butler_waiting = {"waiting": [{"label": "管家", "title": "", "ask": "现在部署吗？", "since": FROZEN - 60},
                                  {"label": "T396", "title": "你为啥没有pin？", "ask": "置顶汇总卡", "since": FROZEN - 180}],
                      "running": [], "ended": []}
    # 刚结束 rows say why a task stopped or what its result was (core/board_read.py ended_line); the owner's own stop 已停止.
    ended = {**view, "ended": [{"label": "T394", "title": "群晖公钥", "status": "cancelled", "at": FROZEN - 600,
                                "tone": "cancelled", "line": "被停止了"},
                               {"label": "T395", "title": "重复的卡", "status": "done", "at": FROZEN - 300, "tone": "result",
                                "line": "已经改好了，测试都过。" + "很长" * 40},
                               {"label": "T396", "title": "日志", "status": "done", "at": FROZEN - 200, "seen": True}]}
    # Without the board: what stopped and what may be stuck, each a part of its own after 等你 (P0-1, P0-2, B4), and the
    # band's grey line does not say nothing runs while something does (P0-10).
    asking = {**view, "stopped": [{"label": "T397", "title": "迁库", "status": "failed", "at": FROZEN - 900, "tone": "stopped",
                                   "line": "这一轮没有成功；可以接着做，或先看原因；停在：跑全量",
                                   "url": "https://applink.feishu.cn/t/397"}],
              "running": [{"label": "T398", "title": "跑评测", "harness": "Codex", "workspace": "DataLab", "status": "running",
                           "started": FROZEN - 7200, "phase": "跑评测", "progress": ["可能卡住了"], "stuck": True},
                          {"label": "T399", "title": "写文档", "harness": "Claude Code", "workspace": "covey",
                           "status": "running", "started": FROZEN - 600, "phase": "改 README", "stuck": False}]}
    calm = {**view, "running": asking["running"][1:]}
    # Every row opens what it is about (T657 B5): a running task's and a result's card too; 整理专项's proposals waiting
    # are 等你, counted and listed as the 看板 lists them (B3); the web page's link on the last line (B9).
    proposals = [{"id": "b84c9a1e00000000", "name": "提示词优化", "goal": "各环节新提示词验证通过",
                  "rows": [{"topic": "h", "label": "T488", "title": ""}, {"topic": "i", "label": "T505", "title": ""}]}]
    linked = {**view, "proposals": proposals, "web": "https://example.invalid/app/covey",
              "running": [{**asking["running"][1], "url": "https://applink.feishu.cn/t/399"}],
              "ended": [{**ended["ended"][1], "url": "https://applink.feishu.cn/t/395"}]}
    return {"pinned_board": overview_card({**view, "board": board}, FROZEN, "14:40"),
            "overview_linked": overview_card(linked, FROZEN, "14:40"),
            "pinned_board_proposals": overview_card({**view, "web": "https://example.invalid/app/covey", "board": {
                **board, "counts": {**board["counts"], "waiting": 3}, "proposals": proposals}}, FROZEN, "14:40"),
            "overview_ended": overview_card(ended, FROZEN, "14:40"),
            "pinned_board_undo": overview_card({**view, "board": undo}, FROZEN, "14:40"),
            "overview_butler_waiting": overview_card(butler_waiting, FROZEN, "14:40"),
            "pinned_board_empty": overview_card({**view, "board": empty}, FROZEN, "14:40"),
            # The owner never had a topic (T604 #53): both modes say what to do first.
            "overview_first_use": overview_card({**view, "first_use": True}, FROZEN, "14:40"),
            "pinned_board_first_use": overview_card({**view, "board": empty, "first_use": True}, FROZEN, "14:40"),
            "overview_stopped_stuck": overview_card(asking, FROZEN, "14:40"),
            "overview_running_only": overview_card(calm, FROZEN, "14:40")}


def digest_cards():
    from covey.cards.board import board_card
    from covey.cards.digest import digest_card, folded_board

    def old_row(label, title, state, took, seen=False):  # As an older release saved a view (its link in its label).
        return {"label": label, "title": title, "state": state, "took": took, "seen": seen}

    def row(label, title, state, took, line, meta, url="", **fields):  # As app/digest.py collect and tick give it now.
        return {"topic": label.lower(), "label": label, "title": title, "state": state, "took": took, "line": line,
                "meta": meta, "url": url, **fields}

    view = {"slot": FROZEN, "since": FROZEN - 9.5 * 3600,
            "waiting": [row("T396", "你为啥没有pin？", "等你：置顶汇总卡", "等了 3分00秒", "要你定：置顶汇总卡", ["等了 3 分钟"],
                            "https://applink.feishu.cn/t/396", kind="question", card="om_q396"),
                        row("T397", "新话题", "等你开工", "等了 40秒", "等你在启动卡上点开始", ["等了 1 分钟"],
                            "https://applink.feishu.cn/t/397", kind="start", card="om_launch397", act="去开始")],
            "running": [row("T390", "重构", "跑全量 · 预计还要约 40 分钟", "已运行 1小时5分", "现在：跑全量",
                            ["4/7 步", "预计还要约 40 分钟", "2 分钟前有动静", "已跑 1 小时 5 分钟"], "https://applink.feishu.cn/t/390",
                            card="om_t390", stuck=False)],
            "done": [row("T394", "群晖公钥", "停住了", "用时 2分10秒", "这一轮没有成功；可以接着做，或先看原因",
                         ["20:31 停", "下一步：在任务卡上点「检查并继续」"], "https://applink.feishu.cn/t/394c", card="om_t394",
                         status="failed", tone="stopped", seen=False),
                     row("T393", "日志", "做完", "用时 45秒", "日志切到新目录了", ["用时 1 分钟"], "https://applink.feishu.cn/t/393",
                         status="done", tone="done", seen=True)]
                    + [row(f"T{n}", "旧", "做完", "用时 1秒", "做好了", ["用时 不到 1 分钟"], status="done", tone="result",
                           seen=False) for n in range(380, 388)]}
    # A view an older release saved (这些都不用回答了 and 记下 redraw the card from it): drawn the same way.
    legacy = {"slot": FROZEN, "since": FROZEN - 9.5 * 3600,
              "waiting": [old_row("[T396](https://applink.feishu.cn/client/thread/open?open_chat_id=oc_fake_chat)",
                                  "你为啥没有pin？", "等你：置顶汇总卡", "等了 3分00秒")],
              "running": [old_row("T390", "重构", "跑全量 · 预计还要约 40 分钟", "已运行 1小时5分")],
              "done": [old_row("T394", "群晖公钥", "停住了", "用时 2分10秒"), old_row("T393", "日志", "做完", "用时 45秒", seen=True)]}
    from covey.app.digest import cleared

    old = {**view, "dismissable": True, "done": view["done"][:2],
           "stale": [{"topic": f"t{n}", "label": f"T{n}", "title": "旧问题"} for n in (370, 371, 372)]}
    return {"three_parts": digest_card(view),
            "legacy_view": digest_card(legacy),
            # 要你处理 as the 看板 counts it (T657 B1, B3): a project a guard paused under 停住了, a proposal under 等你 with no
            # buttons (this card is a record of its time); the web page's address on the last line.
            "asks": digest_card({**view, "waiting": view["waiting"][:1], "running": [], "done": view["done"][:2],
                                 "projects": [{"id": "a1b2c3d4e5f6", "name": "把订单库迁到新集群", "goal": "", "topics": [],
                                               "halted": "token 用完了", "url": "https://applink.feishu.cn/t/540"}],
                                 "pending": [{"id": "b84c9a1e00000000", "name": "提示词优化", "goal": "各环节新提示词验证通过",
                                              "why": "", "rows": [{"topic": "h", "label": "T488", "title": "修数据包",
                                                                   "url": "https://applink.feishu.cn/t/488"}]}],
                                 "web": "https://example.invalid/app/covey"}),
            "done_only": digest_card({**view, "waiting": [], "running": [], "done": view["done"][:1]}),
            # 记忆：待审核 N 条 with the link to the page's 记忆, 待审核 on its top (app/digest.py review_view).
            "memory_review": digest_card({**view, "waiting": [], "running": [], "done": view["done"][:1],
                                          "review": {"pending": 3, "url": "https://covey.example.test/memory"}}),
            # Results not seen that ended before the digest before are listed too, the heading says how many.
            "older_unseen": digest_card({**view, "waiting": [], "running": [], "done": view["done"][:2], "older": 1}),
            "old_questions": digest_card(old),
            "after_dismiss": digest_card(cleared(old, {"t370", "t371", "t372"})),
            # 撤销 on the line it left (``cleared_taken``: what it puts back); a view saved before it has no button.
            "after_dismiss_undo": digest_card(cleared(old, {"t370", "t371", "t372"}, FROZEN + 60)),
            # The bot menu's 看板 (app/digest.py board): the same card at any time, and with nothing to list.
            "asked": digest_card({**old, "asked": True}),
            "asked_empty": digest_card({"slot": FROZEN, "since": FROZEN - 3 * 3600, "waiting": [], "stale": [],
                                        "running": [], "done": [], "asked": True}),
            "asked_folded": folded_board(),
            # The 看板 (cards/board.py, the owner's 样张 A, 2026-11-18): a coloured block a topic, by what it asks of him.
            "board": board_card({
                "waiting": [{"topic": "a", "label": "T505", "title": "优化 Bootstrap 下一环节", "url": "https://applink.feishu.cn/t/505",
                             "line": "要你定：Prompt 问题清单已出，等你定 6 件事", "meta": ["等了 45 分钟"]}],
                "stale": [{"topic": "s", "label": "T370", "title": "旧问题"}], "dismissable": True, "can_dismiss": True,
                "running": [{"topic": "b", "label": "T488", "title": "修 064 采购审批数据包", "url": "https://applink.feishu.cn/t/488",
                             "line": "现在：梳理 Service 还剩哪些要收尾", "meta": ["4/7 步", "预计还要约 35 分钟", "2 分钟前有动静"]},
                            {"topic": "c", "label": "T519", "title": "从追问记录盘点 Covey 痛点", "url": "https://applink.feishu.cn/t/519c",
                             "card": "om_t519", "line": "现在：在审看板", "meta": ["35 分钟前有动静"], "stuck": True}],
                "done": [{"topic": "d", "label": "T525", "title": "修 Covey开发机重启后自启", "url": "https://applink.feishu.cn/t/525c",
                          "card": "om_t525", "status": "failed", "line": "这一轮没有成功；可以接着做，或先看原因；停在：等你选插件形态",
                          "meta": ["20:31 停", "下一步：在任务卡上点「检查并继续」"], "seen": False},
                         {"topic": "e", "label": "T528", "title": "提炼两群 AI 高手的认知", "status": "done", "line": "炼化完了",
                          "url": "https://applink.feishu.cn/t/528",
                          "meta": [], "seen": False, "state": "做完", "took": "用时 19分22秒"},
                         {"topic": "f", "label": "T527", "title": "修停止慢和任务卡重复", "status": "done", "seen": True,
                          "state": "做完", "took": "用时 11分26秒"}],
                "projects": [{"name": "数据包生产链路提示词优化", "goal": "各环节新提示词验证通过并合入", "topics": ["g"],
                              "waiting": 1, "running": 0, "stopped": 0, "url": "https://applink.feishu.cn/t/600"}],
                "proposals": 1, "tidy": {}, "stamp": "20:59", "done_since": "09:30",
                "pending": [{"id": "b84c9a1e00000000", "name": "数据包生产链路提示词优化", "goal": "各环节新提示词验证通过并合入 !248",
                             "why": "", "rows": [{"topic": "h", "label": "T488", "title": "修 064 采购审批数据包",
                                                  "url": "https://applink.feishu.cn/t/488"},
                                                 {"topic": "i", "label": "T505", "title": "优化 Bootstrap 下一环节", "url": ""}]}]}),
            # Many projects (T639: up to 50 on, folded ones piling up): the first 8 on a block each, the rest and the
            # folded ones a line each in one fold.
            "board_many_projects": board_card({
                "waiting": [], "stale": [], "running": [], "done": [], "stamp": "08:00",
                "projects": [*({"name": f"专项 {n}", "goal": "目标" if n < 2 else "", "topics": [f"t{n}"], "waiting": int(n == 0),
                                "running": 1, "stopped": 0, "url": f"https://applink.feishu.cn/t/{700 + n}"} for n in range(10)),
                             {"name": "旧的上线收尾", "goal": "", "topics": ["u"], "waiting": 0, "running": 0, "stopped": 0,
                              "folded": True, "url": "https://applink.feishu.cn/t/650"}]}),
            "board_empty": board_card({"waiting": [], "stale": [], "running": [], "done": [], "stamp": "08:00"}),
            # Nothing to list, told apart (T604 #53): the owner never had a topic; a record from 08:00 drawn later.
            "board_first_use": board_card({"waiting": [], "stale": [], "running": [], "done": [], "stamp": "08:00",
                                           "at": FROZEN, "first_use": True}),
            "board_record": board_card({"waiting": [], "stale": [], "running": [], "done": [], "stamp": "08:00",
                                        "at": FROZEN, "cleared": ["T370"]}, FROZEN + 3 * 3600),
            "board_dismissed_undo": board_card({"waiting": [], "stale": [], "running": [], "done": [], "stamp": "08:00",
                                                "at": FROZEN, "cleared": ["T370", "T371"],
                                                "cleared_taken": {"t370": FROZEN + 60, "t371": FROZEN + 60}}, FROZEN + 120),
            "board_butler_waiting": board_card({"waiting": [{"topic": "m", "label": "管家", "title": "", "url": "",
                                                             "line": "要你定：现在部署吗？", "meta": ["等了 1分"]}],
                                                "stale": [], "running": [], "done": [], "stamp": "08:00"}),
            # Who stopped it (docs/ui.md 3): the lead's stop is 停住了, the owner's own 已停止 in the fold; a result seen.
            "board_stops": board_card({"waiting": [], "stale": [], "running": [], "stamp": "08:00", "done_since": "07:00",
                                       "done": [{"topic": "j", "label": "T531", "title": "统一 UI 规范", "status": "cancelled",
                                                 "tone": "stopped", "line": "专项负责人停了它；停在：等 T538", "meta": ["07:40 停"],
                                                 "url": "https://applink.feishu.cn/t/531c", "card": "om_t531"},
                                                {"topic": "k", "label": "T532", "title": "自动归入", "status": "cancelled",
                                                 "tone": "cancelled", "state": "已取消", "took": "用时 2分0秒"},
                                                {"topic": "l", "label": "T533", "title": "看板", "status": "done",
                                                 "tone": "done", "state": "做完", "took": "用时 9分0秒", "seen": True}]}),
            # A stopped row has 去任务卡 (its 检查并继续 button was taken off, T898; an old view's ``recover`` is ignored).
            # The web page's link (B9).
            "board_recover": board_card({"waiting": [], "stale": [], "running": [], "stamp": "08:00", "can_dismiss": True,
                                         "web": "https://example.invalid/app/covey",
                                         "done": [{"topic": "c" * 16, "label": "T551", "title": "跑全量", "status": "failed",
                                                   "tone": "stopped", "line": "这一轮没有成功；可以接着做，或先看原因",
                                                   "meta": ["07:40 停"], "url": "https://applink.feishu.cn/t/551c", "card": "om_t551",
                                                   "recover": {"action": "continue", "label": "检查并继续", "job": 41}},
                                                  {"topic": "d" * 16, "label": "T552", "title": "发结论", "status": "failed",
                                                   "tone": "stopped", "line": "结果没能发出去；可以重新投递",
                                                   "meta": ["07:41 停"], "url": "https://applink.feishu.cn/t/552c", "card": "om_t552",
                                                   "recover": {"action": "continue", "label": "重新投递结果", "job": 42}},
                                                  {"topic": "e" * 16, "label": "T553", "title": "限流", "status": "failed",
                                                   "tone": "stopped", "line": "上游限流，08:10 自动重试",
                                                   "meta": ["07:42 停"], "url": "https://applink.feishu.cn/t/553c", "card": "om_t553"}]},
                                        notice="↪ T551 已接着做 · 任务 #43"),
            # A project a guard paused (P0-3, T647): 停住了 and why in red, 继续 on its block, counted among 要你处理; one the
            # owner stopped 已停止; a stopped topic of a live project is listed under 停住了 too (it asks for him).
            "board_halted_project": board_card({"waiting": [], "stale": [], "running": [], "stamp": "08:00",
                                                "done": [{"topic": "pm", "label": "T541", "title": "改写大表", "status": "failed",
                                                          "tone": "stopped", "line": "这一轮没有成功；可以接着做，或先看原因",
                                                          "url": "https://applink.feishu.cn/t/541c", "card": "om_t541"}],
                                                "projects": [{"id": "a1b2c3d4e5f6", "name": "把订单库迁到新集群", "goal": "周末前切完",
                                                              "topics": ["pl", "pm"], "waiting": 0, "stopped": 1, "stuck": 0,
                                                              "running": 0, "halted": "token 用完了",
                                                              "url": "https://applink.feishu.cn/t/540"},
                                                             {"id": "b2c3d4e5f6a7", "name": "整理周报", "goal": "", "topics": ["pw"],
                                                              "waiting": 0, "stopped": 0, "stuck": 0, "running": 0,
                                                              "cancelled": True, "url": "https://applink.feishu.cn/t/542"}]}),
            # Many topics and projects at once (P0-10): fewer rows a group until the card is within what CardKit takes.
            "board_crowded": board_card({
                "waiting": [{"topic": f"w{n}", "label": f"T{700 + n}", "title": "等你定的事" * 3, "url": f"https://applink.feishu.cn/t/w{n}",
                             "line": "要你定：" + "选哪一个方案" * 5, "meta": ["等了 5 分钟"]} for n in range(9)],
                "stale": [{"topic": f"s{n}", "label": f"T{600 + n}", "title": "旧问题"} for n in range(3)], "dismissable": True,
                "can_dismiss": True,
                "running": [{"topic": f"r{n}", "label": f"T{720 + n}", "title": "在做的事" * 3, "url": f"https://applink.feishu.cn/t/r{n}",
                             "card": f"om_r{n}", "line": "现在：" + "跑全量测试" * 5, "meta": ["3/7 步", "2 分钟前有动静"],
                             "stuck": n % 2 == 0} for n in range(18)],
                "done": [{"topic": f"d{n}", "label": f"T{740 + n}", "title": "结束的事" * 3, "url": f"https://applink.feishu.cn/t/d{n}",
                          "card": f"om_d{n}", "status": ("failed", "done", "done", "cancelled")[n % 4],
                          "tone": ("stopped", "result", "done", "cancelled")[n % 4], "line": "这一轮没有成功；可以接着做，或先看原因",
                          "meta": ["20:31 停"], "state": "做完", "took": "用时 3分0秒"} for n in range(36)],
                "projects": [*({"id": f"{n:012x}", "name": f"专项 {n}", "goal": "目标" * 10, "topics": [f"pt{n}"], "waiting": 1,
                                "stopped": 1, "stuck": 0, "running": 1, "url": f"https://applink.feishu.cn/t/p{n}",
                                **({"halted": "很久没有动静"} if n % 3 == 0 else {})} for n in range(14)),
                             *({"id": f"f{n:011x}", "name": f"收起的专项 {n}", "goal": "", "topics": [f"pf{n}"], "waiting": 0,
                                "stopped": 0, "stuck": 0, "running": 0, "folded": True, "url": f"https://applink.feishu.cn/t/f{n}"}
                               for n in range(30))],
                "tidy": {}, "stamp": "20:59", "done_since": "09:30"}),
            # 到点回来看 (COVEY_REVISIT=1, app/revisits.py): a topic waiting for its time (its 取消 button was taken off, T898), one being looked at now.
            "board_revisits": board_card({"waiting": [], "stale": [], "done": [], "stamp": "08:00",
                                          "running": [{"topic": "r", "label": "T42", "title": "跑全量", "line": "现在：在跑测试",
                                                       "url": "https://applink.feishu.cn/t/42", "meta": ["3/5 步"]}],
                                          "revisits": [{"topic": "w", "label": "T40", "title": "盯训练", "at": "15:30",
                                                        "look": "看 loss 降到 0.3 没有", "set_at": 1795000000.5,
                                                        "url": "https://applink.feishu.cn/t/40", "fired": False},
                                                       {"topic": "x", "label": "T41", "title": "等 CI", "at": "08:00",
                                                        "look": "", "set_at": 1795000001.0, "url": "", "fired": True}]}),
            # 整理专项 on (cards/project_proposal.py board_rows): the projects and the 整理专项 button.
            "tidy": digest_card({**view, "tidy": {"projects": ["账号额度运行台", "DataLab Prompt 优化"]}}),
            "tidy_none": digest_card({**view, "tidy": {"projects": []}}),
            **evening_digest_cards(view)}


def evening_digest_cards(view):
    """The evening digest's own parts (app/digest.py daily; T604 #1, #45): 本周找你 and 今天代答 after the three parts, then
    整理记忆 with 撤销 and the 待审核 line; with only 整理记忆 (the card is sent for it alone), after 撤销, and its Markdown.
    A view of before (``remember``, ``wanted``, rules in 整理记忆) is drawn without them."""
    from covey.cards.digest import digest_card, digest_text

    tidied = {"run": "t0a1b2c3d", "away": {"idle": 1, "done": 8, "foreign": 2}, "undone": False}
    evening = {**view, "reached": {"questions": 9, "stopped": 3}, "proxy": {"sent": 5, "unsent": 1}, "memory_tidy": tidied,
               "review": {"pending": 3, "url": "https://covey.example.com/memory"}}
    alone = {"slot": FROZEN, "since": FROZEN - 9.5 * 3600, "waiting": [], "stale": [], "running": [], "done": [],
             "reached": {"questions": 0, "stopped": 0}, "memory_tidy": tidied}
    undone = {**alone, "memory_tidy": {**tidied, "undone": True}}
    older = {**evening, "remember": [{"id": "c0a1b2c3d", "text": "回答先给结论", "labels": ["T396"], "workspace": "", "state": "open"}],
             "wanted": [{"what": "T396 等了你 3小时0分", "why": "你一直没回", "would": "主动来问你"}]}
    return {"evening": digest_card(evening), "evening_tidy_alone": digest_card(alone), "evening_tidy_undone": digest_card(undone),
            "evening_of_before": digest_card(older), "evening_text": {"text": digest_text(evening)}}


def app_setup_cards():
    """飞书应用待补 (cards/app_setup.py): the offer, with and without reviewed scopes and a full menu, the run's
    steps with the QR prompt, the result after a published run and after one stopped at a step."""
    from covey.app.app_setup import need_dict
    from covey.cards.app_setup import offer_card, progress_card, result_card, skipped_card, unavailable_card
    from covey.core import app_manifest

    needs = [need for need in app_manifest.required({"COVEY_DIGEST_TIMES": "09:30",
                                                      "COVEY_GROUP_ALL_MESSAGES": "1"}, bottom_menu=True)
             if need.name in ("application.bot.menu_v6", "im:message", "drive:file:upload", "weekly_usage",
                              "bridge_settings", "digest_board", "im:message.group_msg")]
    offer = [need_dict(n) for n in needs if not n.review]
    review = [need_dict(n) for n in needs if n.review]
    view = {"offer": offer, "review": [], "full": [], "version": "1.0.11", "digest": "0123456789abcdef"}
    steps = {"login": "done", "read": "done", "manifest": "done", "menu": "doing"}
    return {"offer": offer_card(view),
            "offer_review_full": offer_card({**view, "review": review, "full": [offer[-1]]}),
            "progress_qr": progress_card({**view, "steps": {"login": "doing"}, "qr": True}),
            "progress_steps": progress_card({**view, "steps": steps}),
            "published": result_card({**view, "steps": {**steps, "menu": "done", "version": "done", "check": "done"},
                                      "still": [], "new_version": "1.0.12"}),
            "failed": result_card({**view, "steps": {**steps, "menu": "failed"}, "still": [offer[-1]["id"]],
                                   "failed": {"words": "写入菜单", "detail": "后台接口 robot/update_changed 报错 9499 Bad Request",
                                              "changed": ["manifest"]}}),
            "skipped": skipped_card(view),
            "unavailable": unavailable_card(view, "没有装了 Playwright 的 Python（COVEY_PLAYWRIGHT_PYTHON）")}


def welcome_cards():
    """The welcome card (cards/welcome.py): with the butler; without it (Claude Code not enabled, a Harness not logged
    in); and after a click, one example sent and one that did not go out, with its line."""
    from covey.app.welcome import EXAMPLES, TOPIC_EXAMPLES
    from covey.cards.welcome import FAILED, NO_CLAUDE, SENT, welcome_card

    view = {"host": "devbox", "harnesses": ["Codex", "Claude Code"], "down": [], "workspace": "work",
            "workdir": "/absolute/work", "butler": "", "examples": list(EXAMPLES)}
    return {"butler": welcome_card(view),
            "no_butler": welcome_card({**view, "harnesses": ["Codex", "Pi"], "down": [["Pi", "没登录"]], "butler": NO_CLAUDE,
                                      "examples": list(TOPIC_EXAMPLES)}),
            "tried": welcome_card({**view, "tried": {"0": {"state": SENT}, "2": {"state": FAILED}},
                                   "notice": f"「{EXAMPLES[2]}」没发出去，再点一次。"})}


def web_login_cards():
    """网页登录确认 (cards/web_login.py): the request waiting, with the page's refusal said, decided both ways, void (the page took nothing)
    and past its five minutes."""
    from covey.cards.web_login import expired_card, pending_card, result_card, void_card

    view = {"request_id": "req-0123456789", "code": "4827", "browser": "Chrome 130", "os": "macOS 15", "ip": "203.0.113.7",
            "at": FROZEN - 30}
    return {"pending": pending_card(view), "pending_failed": pending_card(view, "http 401"),
            "approved": result_card(view, True, FROZEN), "denied": result_card(view, False, FROZEN),
            "void": void_card(view, True), "expired": expired_card(view)}


def project_cards():
    """The 专项卡 (cards/project_card.py): the live card in each state, and the lines it shrinks to."""
    from covey.cards import project_card as pc

    link = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x"
    ask = "https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=76"
    threads = (pc.Member("a1b2c3" + "0" * 10, pc.RUNNING, "T501", "拆分 SQL 迁移脚本", "第 2/5 步：改写第 3 张表", "12分00秒", link),
               pc.Member("b2c3d4" + "0" * 10, pc.DONE, "T502", "梳理旧表结构", "做完了", "8分00秒", link),
               pc.Member("c3d4e5" + "0" * 10, pc.WAITING, "T503", "决定分区键", "要你定：按日期还是按租户分区？", "3分00秒", link))
    work = pc.Project(
        project_id="a1b2c3d4e5f6", lead_topic="f" * 32, name="把订单库迁到新集群", status=pc.RUNNING,
        members=threads[:2], elapsed="2小时3分", tokens="33万", lead_state="在做 · 第 3/6 步：核对迁移结果",
        lead_note="先迁小表，大表等周末窗口", inputs=("Claude Code", "Opus 5.5", "思考 high"),
        outputs=("**开始** 09:12　**已运行** 2小时3分", "**token** 33万"), ask_text="把订单库迁到新集群，周末前要有结果",
        ask_at="09:10", ask_url=ask)
    full = pc.Project(**{**work.__dict__, "triage": ("09:40 答了：用哪个库版本？", "10:05 转给你：能不能停服务？"),
                         "memory": (("m1", "迁移窗口在周六凌晨"), ("m2", "旧集群只读到周日")), "memory_counts": (2, 1),
                         "memory_url": "https://covey.example.test/memory?scope=p%3Aa1b2c3d4e5f6",
                         "library": (("迁移方案.md",), ("表清单.csv",))})
    many = tuple(pc.Member(f"{index:02x}" * 3 + "0" * 10, pc.DONE if index % 3 else pc.RUNNING, f"T{600 + index}", f"话题 {index}",
                           "做完了", "5分00秒") for index in range(11))
    # A member that failed (停住了, open, why), one the owner stopped (已停止), one that may be stuck (P0-1, T647).
    ended = (pc.Member("d4e5f6" + "0" * 10, pc.STOPPED, "T504", "改写大表", "这一轮没有成功；可以接着做，或先看原因；停在：第 3 张表",
                       "20分00秒", link),
             pc.Member("e5f6a7" + "0" * 10, pc.CANCELLED, "T505", "导出旧数据", "被停止了", "1分00秒", link),
             pc.Member("f6a7b8" + "0" * 10, pc.STUCK, "T506", "核对行数", "第 1/2 步：逐表比对", "40分00秒", link))
    return {"running": pc.project_card(work),
            "running_full": pc.project_card(full),
            # The lead's speaker row with its picture (T657 B2), what waits for the owner as a band opening the question card.
            "waiting": pc.project_card(pc.Project(**{**full.__dict__, "status": pc.WAITING, "members": threads,
                                                     "need": "按日期还是按租户分区？", "need_from": "T503", "need_url": ask,
                                                     "avatar": "img_v3_fake_lead"})),
            # A guard's pause: 停住了 and why, red (P0-3); the owner's own stop: 已停止, grey.
            "paused": pc.project_card(pc.Project(**{**work.__dict__, "status": pc.STOPPED, "reason": "token 用完了",
                                                    "lead_state": "停住了 · token 用完了"})),
            "stopped_by_owner": pc.project_card(pc.Project(**{**work.__dict__, "status": pc.CANCELLED, "lead_state": "待命"})),
            "members_ended": pc.project_card(pc.Project(**{**work.__dict__, "members": threads[:1] + ended})),
            "many_threads": pc.project_card(pc.Project(**{**full.__dict__, "members": many})),
            "line_done": pc.settled_card("把订单库迁到新集群", url=link),
            "line_done_no_link": pc.settled_card("把订单库迁到新集群"),
            "line_stopped": pc.settled_card("把订单库迁到新集群", reason="专项已降为普通话题"),
            "line_moved": pc.moved_card("把订单库迁到新集群", link),
            "upgraded": pc.project_card(pc.Project(**{**work.__dict__, "members": threads[:1],
                                                      "origin_note": "由管家转成专项"})),
            "line_merged": pc.merged_card("把订单库迁到新集群", link),
            # Nothing to do for COVEY_PROJECT_IDLE_DAYS: the project folds away into one line until new work opens it again.
            "line_idle": pc.idle_card("把订单库迁到新集群", 7),
            # COVEY_PROJECT_TRIAGE=1: a row of an answer the lead gave is a plain line (its 不对 button was taken off, T898).
            "running_triage": pc.project_card(pc.Project(**{**full.__dict__, "triage": (
                ("09:40 答了：T501 · 用哪个库版本？ → 新版", "c" * 16), "10:05 转给你：T503 · 能不能停服务？（碰到硬闸）")})),
            "ask_tokens": pc.ask_card("把订单库迁到新集群", "token 用到上限 1000 万", project_id="a1b2c3d4e5f6", lead_topic="f" * 32,
                                      note="在做的 2 个话题已请它们停下。"),
            "ask_wakes": pc.ask_card("把订单库迁到新集群", "负责人连续叫醒 30 次没有你的消息", project_id="a1b2c3d4e5f6",
                                     lead_topic="f" * 32, note="在做的话题不受影响，还在继续做。"),
            "ask_stalled": pc.ask_card("把订单库迁到新集群", "62 分钟没有进展", project_id="a1b2c3d4e5f6", lead_topic="f" * 32),
            "line_asked_continue": pc.asked_card("把订单库迁到新集群", "token 用到上限 1000 万", "continue"),
            "line_asked_finish": pc.asked_card("把订单库迁到新集群", "62 分钟没有进展", "finish"),
            "line_asked_stop": pc.asked_card("把订单库迁到新集群", "负责人连续叫醒 30 次没有你的消息", "stop"),
            "line_asked_elsewhere": pc.asked_card("把订单库迁到新集群", "62 分钟没有进展")}


def tidy_cards():
    """整理专项's cards (cards/project_proposal.py): the proposal card (the first one, the one asking 以后直接建, decided), its
    rename form, the 整理专项 card while it reads and with its result, and the 已归进 line on a topic's cards."""
    from covey.cards import project_proposal as tc
    from covey.cards.status_card import status_card
    from covey.cards.topic_card import topic_card

    view = {"proposal": "0123456789abcdef", "labels": [("T488", "修 064 采购审批数据包"), ("T505", "优化 Bootstrap 下一环节")],
            "name": "DataLab Prompt 优化", "goal": "新旧 Prompt 对率可比", "why": "两个话题都在改 Prompt", "first": True,
            "accepted": 0, "ask_auto": False, "state": "open"}
    rows = [{"proposal": "1" * 16, "line": "新建「推理网关稳定」：T484、T485、T499（都在这个网关上）", "state": "open"},
            {"proposal": "2" * 16, "line": "「Covey 工作台与体验」改名「Covey 工作台」", "state": "done", "note": "已改名「Covey 工作台」"},
            {"proposal": "3" * 16, "line": "合并「回流周报」「回流汇报」", "state": "declined"}]
    done = {"run": "20261118093000-abc123", "state": "done", "did": ["已把 T512 归进「DataLab Prompt 优化」",
                                                                      "已收起「回流周报」：本周的已交，有新的事会自动展开"],
            "rows": rows, "skipped": ["3 个话题读不了，没参与这次整理"], "scope": ""}
    joined = tc.joined_element("DataLab Prompt 优化", "f" * 32, "a1b2c3d4e5f6")
    rows = [{"label": "T488", "title": "修 064 采购审批数据包", "link": "https://applink.feishu.cn/client/thread/open?t=488",
             "essence": "优化 DataLab 的 Prompt，让数据包一次跑对", "now": "v1.1 和旧版各跑 3 遍的对跑在跑，预计 19:35 跑完。",
             "days": 7.2, "turns": 93},
            {"label": "T505", "title": "优化 Bootstrap 下一环节", "link": "", "essence": "", "now": "", "days": 0.4, "turns": 12}]
    one = [{"label": "T252", "title": "阅读这个session，继续我们未完成的事情", "link": "https://applink.feishu.cn/client/thread/open?t=252",
            "essence": "把账号额度运行台做成能一眼看清各账号额度和可用性的页面", "now": "第 7 期上线：额度读数改成按账号分组。",
            "days": 19.0, "turns": 118}]
    choices = [{"topic": "a" * 64, "label": "T488", "title": "修 064 采购审批数据包", "chosen": True},
               {"topic": "b" * 64, "label": "T505", "title": "优化 Bootstrap 下一环节", "chosen": True},
               {"topic": "c" * 64, "label": "T523", "title": "T488、T505 验证加速方案", "chosen": False}]
    view = {**view, "choices": choices}
    cards_ = {"proposal_rows": tc.proposal_card({**view, "rows": rows}),
              "proposal_one_topic": tc.proposal_card({**view, "rows": one, "first": False, "name": "账号额度运行台",
                                                      "goal": "额度看得准、异常即时置顶", "why": "一直在迭代同一个运行台"})}
    return {**cards_, "proposal_first": tc.proposal_card(view),
            "proposal_ask_auto": tc.proposal_card({**view, "first": False, "ask_auto": True, "accepted": 5}),
            "proposal_declined": tc.proposal_card({**view, "state": "declined", "lesson": True,
                                                   "note": "不建了：这几个话题不会再这样提，等又多出相关的话题才会再提。"}),
            "proposal_done": tc.proposal_card({**view, "state": "done", "note": "已建专项「DataLab Prompt 优化」：专项卡在聊天区，负责人接着跟进。"}),
            "rename_form": tc.rename_form(view),
            "tidy_reading": tc.tidy_card({"run": "r", "state": "running", "progress": {"done": 12, "total": 40, "current": "T488"}}),
            "tidy_done": tc.tidy_card(done),
            # 2026-11-18: each new project on its own proposal card, rows Markdown with every topic linked, no 全部照办, the
            # topics not read named with why.
            "tidy_pending": tc.tidy_card({"run": "r", "state": "done", "did": [], "rows": [],
                                          "pending": ["数据包生产链路提示词优化", "Covey 网页工作台完善"]}),
            "tidy_done_v2": tc.tidy_card({
                "run": "20261118200000-def456", "state": "done", "did": [], "proposed": ["Golden Localize 提示词优化", "提议卡与整理专项"],
                "rows": [{"proposal": "4" * 16, "md": True, "state": "open",
                          "line": "把 [T518](https://applink.feishu.cn/client/thread/open?t=518) 升级测试机的运行时 移出「Covey 产品完善」（不是在完善 Covey）"}],
                "skipped": ["2 个话题读不了，没参与这次整理：[未编号话题](https://applink.feishu.cn/client/thread/open?t=1) 看一下 maso 的日志（内容太长，一次读不完）；"
                            "[T301](https://applink.feishu.cn/client/thread/open?t=301) 对齐评测（官方内容安全拦了）"],
                "skipped_md": True}),
            "tidy_nothing": tc.tidy_card({"run": "r", "state": "done", "scope": "Covey"}),
            "tidy_failed": tc.tidy_card({"run": "r", "state": "failed", "why": "判断超时了"}),
            # T884 (the owner's choice A): the reason through which Harness, what to do next and its buttons by the kind.
            "tidy_failed_login": tc.tidy_card({"run": "r", "state": "failed", "why": "Codex 登录失效或没有权限",
                                               "why_code": "authentication_error", "via": "codex", "model": "gpt-6-astra"}),
            "tidy_failed_refused": tc.tidy_card({"run": "r", "state": "failed", "why": "Claude Code 被官方内容安全拦了",
                                                 "why_code": "policy_refusal", "via": "claude", "model": "sonnet"}),
            "tidy_failed_timeout": tc.tidy_card({"run": "r", "state": "failed", "why": "Codex 判断超时了",
                                                 "why_code": "timeout", "via": "codex", "retry": True}),
            "tidy_failed_none": tc.tidy_card({"run": "r", "state": "failed", "why": "辅助模型选了不用模型",
                                              "why_code": "none"}),
            "leave_answer": tc.lesson_card("已把这个话题移出专项「DataLab Prompt 优化」，以后它不会再被自动归进专项。",
                                           {"kind": tc.LESSON_KIND, "topic": "f" * 32, "project": "a1b2c3d4e5f6"}),
            "joined_line": tc.joined_card("DataLab Prompt 优化", "f" * 32, "a1b2c3d4e5f6"),
            "topic_card_joined": topic_card("修 064 采购审批数据包", label="T488", config=("Claude Code", "Opus 5.5"), joined=joined),
            "status_card_joined": status_card("T488 · 修 064 采购审批数据包", "做完", "13:01 · 2 轮", "结论：改 Prompt",
                                              ["DataLab Prompt 优化", "f" * 32, "a1b2c3d4e5f6"])}


def node_cards_samples():
    from covey.cards.node_cards import alert_card, alert_text, cleared_card, machine_lines
    from covey.cards.node_panel import node_panel_card
    from covey.cards.settings_hub import tab_row

    top = [{"comm": "rg", "rss": 160 << 30, "cpu": 98.5, "task": "k1"}, {"comm": "python3", "rss": 3 << 30}]
    mem = {"rule": "mem", "kind": "raise", "at": FROZEN - 300, "pct": 93.5, "used": 215 << 30, "limit": 230 << 30,
           "top": top}
    tasks = {"k1": "T512 · 任务 #4664"}
    latest = {"cpu": {"pct": 12.0, "cores": 56.0}, "mem": {"pct": 41.0, "used": 94 << 30, "limit": 230 << 30},
              "disk": [{"path": "/", "pct": 87.0}], "gpu": [{"util": 90}, {"util": 3}]}
    rows = [{"alias": "local", "title": "本机", "local": True,
             "line": machine_lines("本机", {"connected": True, "heard": FROZEN}, latest, online=True)[0], "grey": "内存 94.0 GiB / 230.0 GiB · 56 核"},
            {"alias": "gpu1", "title": "训练机（gpu1）", "local": False,
             "line": machine_lines("训练机（gpu1）", {"heard": FROZEN - 2220}, latest, online=False)[0], "grey": "最后：CPU 12% · 内存 41%"}]
    data = {"address": "[2001:db8::1]:10603", "rows": rows}
    view = {"token": "d" * 24, "revision": 3, "hub": True}
    row = [tab_row("nodes", "d" * 24, ["repos", "nodes", "redaction"])]
    return {
        "alert_mem": alert_card(mem, "本机", tasks),
        "alert_oom_text": alert_text({"rule": "oom", "kind": "once", "at": FROZEN, "count": 2, "pct": 97.0, "top": top},
                                     "gpu1", tasks),
        "alert_disk": alert_card({"rule": "disk:/", "kind": "raise", "at": FROZEN, "path": "/", "pct": 90.0,
                                  "free": 49 << 30, "growth": [{"secs": 3600, "bytes": 2 << 30}, {"secs": 86400, "bytes": 45 << 30}],
                                  "writers": [{"comm": "pytest", "bytes": 6 << 30, "secs": 60, "task": "k1"}],
                                  "dirs": {"at": FROZEN - 600, "partial": False, "items": [
                                      {"path": "/tmp", "bytes": 207 << 30, "files": 39, "shape": "datalab-artifacts-*.tar"},
                                      {"path": "/tmp/claude-1000", "bytes": 43 << 30}]}}, "本机", tasks),
        "alert_write": alert_card({"rule": "write:/absolute/data", "kind": "raise", "at": FROZEN, "path": "/absolute/data",
                                   "error": "Disk quota exceeded"}, "本机", {}),
        "cleared_mem": cleared_card(mem, "本机", tasks, FROZEN + 1500),
        "panel": node_panel_card("d" * 24, view, data, top=row),
        "panel_adding": node_panel_card("d" * 24, {**view, "adding": {"alias": "gpu2", "until": "19:30",
                                        "command": "curl -fsSk --noproxy '*' --pinnedpubkey 'sha256//AAAA' "
                                                   "'https://[2001:db8::1]:10603/join/TOKEN' | python3 -"}}, data, top=row),
        "panel_confirm": node_panel_card("d" * 24, {**view, "confirm": "gpu1", "notice": "⏳ 正在经 ssh 登上 gpu2 安装节点…"},
                                         data, top=row),
        "panel_empty": node_panel_card("d" * 24, view, {"address": "", "rows": []}, top=row),
    }


def paged_cards():
    """A text too large for one card over several (cards/pages.py, T663), each opening with its card's row, "1/2" in it:
    an answer in its topic's thread and at the top level, a relay, a question copy's output, and the small card after an
    answer sent as a file. Drawn with a card's room made small, so each sample is a few cards of a few KB; the cards
    are cut the same way at the real room."""
    from covey.cards import pages
    from covey.cards.answer_card import ask_block, figure, input_parts, output_parts, tail, tail_card, topic_head
    from covey.cards.question_cards import output_pages
    from covey.cards.relay_card import relay_pages

    text = "## 结论\n看板改好了，颜色换成暖色。\n\n" + "\n\n".join(
        f"### 第 {n} 步\n" + "改了看板的配色和图例的位置，跑了全量测试。" * 18 for n in range(1, 4)) + (
        "\n\n```py\n" + "".join(f"print({n})\n" for n in range(40)) + "```")
    files = {"tag": "collapsible_panel", "element_id": "answer_artifacts", "expanded": False,
             "header": {"title": {"tag": "markdown", "content": "产物 1 个 · REPORT.md"}}, "elements": []}
    inputs = input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", None, "covey")
    outputs = output_parts({"usage": {"input_tokens": 120000, "cache_read_input_tokens": 90000, "output_tokens": 3000}}, 100)
    block = ask_block("看板改成彩色块，字大一点", at="12:31", url="https://applink.feishu.cn/t/439", who="T439 改看板的颜色",
                      doing="done")
    asked = ask_block("看板要不要改成暖色？", at="12:31", url="https://applink.feishu.cn/t/439", who="T439 改看板的颜色",
                      doing="waiting")
    small = {"ANSWER_CARD_BYTES": 4200, "SENT_BYTES": 5000, "MIN_ROOM": 800}
    with mock.patch.multiple(pages, **small):
        def cards(cut):
            return {"cards": [page.card for page in cut.pages], "texts": [page.text for page in cut.pages],
                    "alone": cut.alone}
        return {
            "answer_in_thread": cards(pages.answer_pages(text, "T439 有结果 · 看板改好了", tail(files, inputs, outputs),
                                                         [topic_head("T439", "改看板的颜色", "done")])),
            "answer_at_top": cards(pages.answer_pages(text, "T439 有结果 · 看板改好了", tail(files, inputs, outputs),
                                                      [block])),
            "answer_no_row": cards(pages.answer_pages(text, "#431 有结果 · 看板改好了", tail(None, inputs, outputs))),
            "relay": cards(relay_pages("T439", text=text, ask=block, url="https://applink.feishu.cn/t/439/card",
                                       inputs=inputs, outputs=[figure("用时", "1分40秒")], files=files)),
            "question_output": cards(output_pages(text, "T439", asked)),
            "tail_after_file": tail_card([block, *tail(files, inputs, outputs)], "T439 · 产物与详情",
                                         "回答较长，作为文件发在上面；产物和详情在这里", words=True),
        }


GROUPS = {"task": task_cards, "trace": trace_panels, "launch": launch_cards, "question": question_cards,
          "proxy": proxy_cards, "steer": steer_cards, "recovery": recovery_cards, "rewind": rewind_cards,
          "switch": switch_cards, "topic": topic_panels,
          "weekly_usage": usage_cards, "menu": menu_panels, "main_topic": main_topic_cards, "main_task": main_task_cards, "status_card": status_cards_samples,
          "main_thread": butler_panels, "settings": settings_cards,
          "overview": overview_cards, "digest": digest_cards, "app_setup": app_setup_cards, "welcome": welcome_cards, "web_login": web_login_cards,
          "project": project_cards, "tidy": tidy_cards, "nodes": node_cards_samples, "group_ask": group_ask_cards,
          "pages": paged_cards}


def render():
    """Every sample as ``group.name`` -> its JSON, drawn with time.time() and the local time at FROZEN in UTC+8."""
    real_localtime = time.localtime
    zone = os.environ.get("TZ")
    os.environ["TZ"] = ZONE
    time.tzset()
    try:
        with mock.patch("time.time", return_value=FROZEN), \
                mock.patch("time.localtime", lambda seconds=None: real_localtime(FROZEN if seconds is None else seconds)):
            return {f"{group}.{name}": value for group, build in GROUPS.items() for name, value in build().items()}
    finally:
        if zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = zone
        time.tzset()


def encode(samples):
    return json.dumps(samples, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def first_difference(old, new, at=""):
    """The path of the first place ``old`` and ``new`` differ, or None."""
    if type(old) is not type(new):
        return at or "/"
    if isinstance(old, dict):
        for key in sorted(set(old) | set(new)):
            if key not in old or key not in new:
                return f"{at}/{key} ({'added' if key not in old else 'removed'})"
            found = first_difference(old[key], new[key], f"{at}/{key}")
            if found:
                return found
        return None
    if isinstance(old, list):
        for i, (a, b) in enumerate(zip(old, new)):
            found = first_difference(a, b, f"{at}[{i}]")
            if found:
                return found
        return None if len(old) == len(new) else f"{at} (length {len(old)} -> {len(new)})"
    return None if old == new else at or "/"


def changes(old, new):
    """One line per sample added, removed or changed, with where it first differs."""
    lines = [f"added: {name}" for name in sorted(set(new) - set(old))]
    lines += [f"removed: {name}" for name in sorted(set(old) - set(new))]
    lines += [f"changed: {name} at {first_difference(old[name], new[name])}" for name in sorted(set(old) & set(new))
              if encode(old[name]) != encode(new[name])]
    return lines


class CardGoldenTests(unittest.TestCase):
    def test_cards_match_the_golden_file(self):
        text = encode(render())
        if os.environ.get("GOLDEN_CARDS") == "update":
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(text, encoding="utf-8")
            return
        self.assertTrue(GOLDEN.exists() or PACKED.exists(), f"{GOLDEN} is missing: GOLDEN_CARDS=update writes it")
        saved = (GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists()
                 else lzma.decompress(PACKED.read_bytes()).decode("utf-8"))
        if saved == text:
            return
        lines = changes(json.loads(saved), json.loads(text)) or ["the samples are equal but the bytes are not"]
        self.fail("Card JSON differs from tests/golden/cards.json:\n  " + "\n  ".join(lines) +
                  "\nIf the change is meant, rewrite the file (GOLDEN_CARDS=update, docs/testing.md) and check the "
                  "cards in a real Feishu client (docs/cards.md 卡片的客户端限制).")

    def test_no_card_names_two_components_alike(self):
        # Feishu refuses such a card (230099, ErrCode 11310); accept15's 移出 answer named its form and its button alike.
        def names(node, found):
            if isinstance(node, dict):
                if "tag" in node and isinstance(node.get("name"), str):
                    found.append(node["name"])
                for value in node.values():
                    names(value, found)
            elif isinstance(node, list):
                for value in node:
                    names(value, found)
            return found
        twice = {sample: sorted({name for name in found if found.count(name) > 1})
                 for sample, card in render().items() for found in [names(card, [])]}
        self.assertEqual({sample: dup for sample, dup in twice.items() if dup}, {})

    def test_button_labels_follow_the_ui_rules(self):
        """docs/ui.md 6: a button's label has no emoji (a standard_icon is the icon) and none of the words the table
        replaced (算了, 这些先不补, a lone 停 or 删, 不发); T575 cleaned up the last ones."""
        emoji = re.compile("[\U0001F000-\U0001FFFF\u2190-\u21FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\u203A]")
        replaced = {"算了", "暂不考虑", "这些先不补", "停", "删", "不发"}

        def labels(node, found):
            if isinstance(node, dict):
                text = node.get("text")
                if node.get("tag") == "button" and isinstance(text, dict) and isinstance(text.get("content"), str):
                    found.append(text["content"])
                for value in node.values():
                    labels(value, found)
            elif isinstance(node, list):
                for value in node:
                    labels(value, found)
            return found
        bad = {sample: sorted({label for label in found if emoji.search(label) or label.strip() in replaced})
               for sample, card in render().items() for found in [labels(card, [])]}
        self.assertEqual({sample: found for sample, found in bad.items() if found}, {})

    def test_rendering_twice_gives_the_same_bytes(self):
        self.assertEqual(encode(render()), encode(render()))

    def test_differences_are_named_by_sample_and_path(self):
        old = {"a.x": {"body": [{"text": "停止"}]}, "a.gone": {}}
        new = {"a.x": {"body": [{"text": "停 止"}]}, "a.new": {}}
        self.assertEqual(changes(old, new), ["added: a.new", "removed: a.gone", "changed: a.x at /body[0]/text"])


if __name__ == "__main__":
    unittest.main()
