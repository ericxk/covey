"""Card JSON goldens: a fixed set of cards and card parts drawn by the real builders, with the clock frozen and
the time zone fixed, compared byte for byte with tests/golden/cards.json (keys sorted).

Any difference means the JSON the Bridge sends for a card changed, so rule 9's client check applies
(docs/cards.md 卡片的客户端限制). After a deliberate change rewrite the file and commit it with the change:

    GOLDEN_CARDS=update python3 -B -m unittest discover -s tests -p test_card_golden.py

The samples carry only fake open_ids, chat and message IDs and /absolute/... paths.
"""

from __future__ import annotations

import dataclasses

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
                                   "✅ T70 · #431 · 已完成"),
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
        "task_card_summary": [task_card_summary("T70", "done", "✅ 已完成 · 用时 3 分 39 秒"),
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
        values = dict(model=model, choices=CHOICES, effort="xhigh", workspace="project",
                      names=["project", "notes"], run_mode="worktree", topic_tag="T70")
        return launch_form_card("a" * 24, **{**values, **kwargs})

    return {
        "codex": form("codex:gpt-6-astra"),
        "codex_effort_open": form("codex:gpt-6-astra", expanded=True, page="effort"),
        "codex_notice": form("codex:gpt-6-astra", notice="⚠️ 提示", run_mode="original"),
        "claude_redacting": form("claude:opus", redaction="on"),
        "maso_expanded": form("maso:preset", expanded=True, work_mode="adaptive"),
        "receipt": build_receipt_card({"model_label": "Codex · GPT-6-Astra", "effort": "xhigh",
                                       "workdir": str(WORK), "topic": "T70", "run_mode": "worktree",
                                       "branch": "bridge/t70"}),
        "preparing": build_preparing_card({"label": "Codex · GPT-6-Astra", "effort": "xhigh",
                                           "workspace": "project", "topic": "T70"}),
    }


def question_cards():
    from covey.cards.question_cards import (TOP_COPY, answer_notice_card, folded_refusal, mirror_card,
                                                     question_card, question_element, question_feedback_card)

    cards = {f"card_{status}": question_card(question_record(status, continuation_job_id=432), "T70")
             for status in ("pending", "answered", "cancelled", "superseded", "expired", "submitting", "uncertain")}
    cards["card_answered_running"] = question_card(question_record("answered", delivery_state="running"))
    cards["card_folded"] = question_card(question_record("superseded", superseded_reason="newer_question",
                                                         superseded_by_job=432, refusal="这组问题已收起"))
    cards["card_multi_select"] = question_card(question_record(
        questions=[{"id": "q", "header": "选择", "question": "选哪个？", "multiSelect": True,
                    "options": [{"label": "甲（推荐）"}, {"label": "乙"}]}], answers={}))
    cards["element_on_task_card"] = question_element(question_record(), "tag", headed=True)
    # A question with one line of background (core/checkpoint.py CHECKPOINT_RULES ``context``, T604 #14): grey under it.
    cards["card_context"] = question_card(question_record(questions=[
        {"id": "port", "header": "端口", "question": "新服务用哪个端口？", "context": "看板要从外网打开，8080 已被网关占用",
         "options": [{"label": "8443（推荐）", "description": "不用改网关"}, {"label": "9090"}]},
        {"id": "note", "header": "补充", "question": "还有别的要求吗？"}], answers={}), "T70")
    # 不处理 (T604 #15): set aside, with 撤销; then taken by the topic's next turn (#432), no 撤销 left.
    skipped = {"fmt": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}, "note": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}}
    cards["card_set_aside"] = question_card(question_record("set_aside", response_kind="set_aside", answers=skipped), "T70")
    cards["card_set_aside_taken"] = question_card(question_record(
        "answered", response_kind="set_aside", answers=skipped, delivery_state="delivered", continuation_job_id=432), "T70")
    # 先问一句 (T604 #13): the open form says what was asked first; the topic answers it, the form stays.
    cards["card_asked_first"] = question_card(question_record(answers={}, asked_first={
        "at": FROZEN - 30, "text": "表格和列表在手机上哪个好看？", "event": "e"}), "T70")
    # One question with options at the private chat's top level: a button per option (cards/question_cards.py picks).
    cards["card_top_pick"] = question_card({**question_record(questions=question_record()["questions"][:1], answers={}),
                                            TOP_COPY: True}, "T70")
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
    cards["feedback"] = question_feedback_card(question_record())
    cards["answer_notice"] = answer_notice_card("已提交回答")
    cards["folded_refusal"] = folded_refusal(question_record("superseded", superseded_by_job=432))
    # 以后这类按推荐走 (app/standing_answers.py): the button under a form whose every question has one recommended
    # option, and its own card: the sentence with 记下 / 改一下 / 算了, the box to change it, what became of it.
    from covey.cards.question_cards import standing_card
    with mock.patch.dict(os.environ, {"COVEY_STANDING_ANSWERS": "1"}):  # Off unset; ./install writes 1.
        cards["card_standing_offer"] = question_card(question_record(
            questions=[{"id": "q", "header": "合并", "question": "测试过了，要合并进 main 并推送吗？",
                        "options": [{"label": "合并并推送（推荐）"}, {"label": "先不合并"}]}], answers={}), "T70")
    standing = {"token": "e" * 24, "workspace": "covey", "picks": ["合并并推送"],
                "text": "covey 工作区：测试通过就合并进 main 并推送；没有在跑的任务时直接部署，不用再问"}
    for status in ("ready", "editing", "saved", "cancelled"):
        cards[f"standing_{status}"] = standing_card({**standing, "status": status, "answered": True})
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
    from covey.cards.owner_send import owner_send_card
    draft = {"id": "d" * 32, "ref": "zs", "status": "drafted", "topic_label": "T9「通知」", "why": "把开好的 key 交给他",
             "text": "key 开好了，在共享盘的 keys 目录", "recipient": {"name": "张三", "department": "算法", "kind": "p2p"}}
    cards["owner_send_drafted"] = owner_send_card(draft, {"state": "drafted"})
    cards["owner_send_unsent"] = owner_send_card({**draft, "status": "unsent", "reason": "网页端未登录"}, {"state": "unsent"})
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
        "saved_answer": recovery_card({**record, "result_retry_policy": "saved_delivery_only",
                                       "verified_reply_authorized": True, "verified_reply_state": "authorized"}),
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


def turn_export_panels():
    from covey.cards.turn_export import export_panel

    return {
        "offered": export_panel(TOPIC, 431),
        "building": export_panel(TOPIC, 431, {"status": "building", "at": FROZEN - 30}),
        "failed": export_panel(TOPIC, 431, {"status": "failed", "error": "上传失败", "at": FROZEN - 30}),
        "done": export_panel(TOPIC, 431, {"status": "done", "url": "https://example.com/file/x", "at": FROZEN - 30,
                                          "name": "T70-#431-请求与结果.json.xz", "requests": 12, "subagents": 2,
                                          "size": 85000, "raw_size": 9000000}),
    }


def switch_cards():
    from covey.cards.elements import notice_card
    from covey.cards.switch_form import build_switch_card
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
            "workspaces": ["project", "notes"], "selected_workspace": "project", "selected_run_mode": "worktree",
            "selected_redaction": "off"}
    return {
        "card": build_switch_card(form),
        "card_changed": build_switch_card({**form, "selected_model": "dsh:ali-deepseek-v4.1-flash",
                                           "selected_effort": "low", "selected_run_mode": "original"}),
        "card_old_record": build_switch_card(old),
        "card_redacting": build_switch_card({**form, "selected_model": "claude:opus", "selected_redaction": "off",
                                             "current": {**form["current"], "redaction": True}}),
        "card_refused": build_switch_card({**form, "notice": "**⚠️ 还没有换（20:08）**：本话题还有任务在排队或执行"}),
        "closed": notice_card("**🔁 已换配置**", "已换配置"),
        "panel_on_task_card": switch_panel(TOPIC, 431, "codex", 2),
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
    from covey.cards.relay_card import DONE, STOPPED, WAITING, relay_card
    from covey.cards.answer_card import (ARTIFACTS_ID, acceptance_element, criteria_element, input_parts, memory_element,
                                         output_parts, review_element, tail, tail_card)
    from covey.cards.elements import markdown_panel
    from covey.cards.review import answer_band, answer_card

    files = markdown_panel(ARTIFACTS_ID, "产物 2 个 · REPORT.md 等", "- [REPORT.md](https://example.invalid/a) · 12 KB\n"
                           "- [本轮改动.diff](https://example.invalid/b) · 3 KB", quiet=True)
    inputs = input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", True, "algo-agent-devskills")
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
    from covey.cards.answer_card import head_element
    head = head_element("✅ 做完", "T443", "改看板的 Pin 目标")  # Atop a 话题区's answer card in its thread (D3).
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
                                                          "body": "等你回答：推哪几个提交？"})),
            "dispatch_done": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.DONE, "body": "已完成",
                                                       "outputs": ("**用时** 2分00秒",)})),
            "dispatch_failed": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.FAILED, "body": "后端退出",
                                                         "outputs": ("**用时** 2分00秒",)})),
            "dispatch_stopped": dispatch_card(Dispatch(**{**work.__dict__, "status": dc.STOPPED, "body": "已停止"})),
            "dispatch_line_done": settled_card(dc.DONE, asked_words, url=thread_url, label="T453"),
            "dispatch_line_failed": settled_card(dc.FAILED, asked_words, reason="后端退出", label="T453"),
            "dispatch_line_stopped": settled_card(dc.STOPPED, asked_words, label="T453"),
            "answer_card_tail": answer_card(text, "✅ T439 · 结论", tail(files, inputs, outputs)),
            # COVEY_MEMORY=1: the memory lines at the bottom (cards/answer_card.py memory_element, app/memory_collect.py).
            "answer_card_memory": answer_card(text, "✅ T439 · 结论", [*tail(files, inputs, outputs), memory_element([
                {"kind": "remembered", "text": "选型类的事不用先问侧重点", "scope": "管这个专项", "entry_id": "a1b2c3"},
                {"kind": "voided", "text": "选型前先问侧重点", "scope": "管这个专项", "entry_id": "d4e5f6"},
                {"kind": "undone", "text": "产物放 out/", "scope": "管这个话题", "entry_id": "0a1b2c"}], "tf0001", 439)]),
            "answer_card_thread": answer_card(text, "✅ T443 · 结论", tail(files, inputs, outputs), [head]),
            "answer_card_thread_stopped": answer_card(text, "⏹ T443 · 结论", tail(None, inputs, outputs),
                                                      [head_element("⏹ 已停止", "T443", "改看板的 Pin 目标")]),
            # Atop a 话题区's answer card in its thread since docs/ui.md 5.2: the conclusion band (app/saved_send.py
            # status_head).
            "answer_card_thread_band": answer_card(text, "T443 有结果 · 结论", tail(files, inputs, outputs),
                                                   [answer_band("done", "T443", "改看板的 Pin 目标", "结论：一和二都做完了")]),
            "answer_card_thread_band_stopped": answer_card(
                text, "T443 已停止 · 结论", tail(None, inputs, outputs),
                [answer_band("cancelled", "T443", "改看板的 Pin 目标", "停在了第二步")]),
            "relay_waiting": relay_card("T439", WAITING, question="用哪种格式输出？表格还是列表，表格方便对比、列表方便手机上看")[0],
            "relay_stopped": relay_card("T439", STOPPED, reason="到了任务的时间上限，这一轮停在这里；可以接着做，或拆小再试")[0],
            "answer_card_asked": answer_card(text, "✅ 结论", tail(files, inputs, outputs), [ask]),
            "answer_relay_asked": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                             ask=ask)[0],
            "question_copy_asked": mirror_card(question_record(), "T439", "先说结论：两种都行。", ask=ask),
            # A project's topic asks at the top level: who asks, no words of the owner's (the lead handed it the work).
            "question_copy_member": mirror_card(question_record(), "T546", "设置卡已按你选的改法改完。",
                                                ask=ask_block("", who="T546 设置的使用逻辑梳理", doing="member")),
            "answer_card_answered": answer_card(text, "✅ 结论", tail(files, inputs, outputs), [answered]),
            "answer_relay_answered": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                                ask=answered)[0],
            "question_copy_answered": mirror_card(question_record(), "T439", "先说结论：两种都行。", ask=answered),
            "answer_relay_tail": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files)[0],
            # Who speaks (docs/ui.md 5.3 speaker): a topic's relay, the butler's answer, a lead's wake-up turn (no 你问).
            "answer_relay_speaker": relay_card("T439", DONE, text=text, inputs=inputs, outputs=outputs, files=files,
                                               ask=ask_block("看板改成彩色块", at="12:31", url="https://applink.feishu.cn/t/439",
                                                             who="T439 改看板的颜色", doing="done"))[0],
            "answer_card_butler": answer_card(text, "✅ 结论", tail(files, inputs, outputs),
                                              [ask_block("看板改成彩色块", at="12:31", who="管家")]),
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
            # #20: 独立复核 under the box of a turn that changed code (COVEY_REVIEW=1, app/review.py).
            "answer_card_review": answer_card(text, "✅ T443 · 结论", tail(
                files, inputs, outputs, change=handed, buttons=[review_element("tf0443", 2906)]), [head]),
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
    from covey.cards.streaming import ACTION_ROW_ELEMENT_ID, build_stream_card
    from covey.cards.task_figures import IO_BOX_ID
    from covey.cards.task_status import status_elements
    from covey.core import butler

    run = stream_runtime()
    snapshot = {"started_at": FROZEN - 13, "ended_at": FROZEN, "input_tokens": 52000, "output_tokens": 900,
                "measured_output_tokens": 800, "generation_seconds": 10.0, "steps": {}}

    def job():
        return sample_job(topic_id=butler.butler_topic_id("chat"), topic_label="", harness_id="claude",
                          user_text="帮我看看昨晚的失败日志", title="")

    def ended(body, status, *, under=(), inside=()):
        card = build_stream_card(" ", "任务", run, job=job(), include_stop_action=True)
        elements = card["body"]["elements"]
        elements[0]["content"] = body
        elements[1] = status_elements(status, False, True)[0]
        details = elements[2]["elements"]
        details[:] = [ended_box(job(), snapshot, 13) if e.get("element_id") == IO_BOX_ID else e
                      for e in details if e.get("element_id") != ACTION_ROW_ELEMENT_ID]
        details[1:1] = list(inside)  # After the 输入/输出 box (task_card.body_anchor).
        elements[1:1] = list(under)  # Right under the body (task_card.under_body).
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
            "done": ended(render_stream_final(bare, "昨晚失败是**磁盘满**。", bare=True), "✅ 已完成 · 用时 13 秒"),
            "routed": ended(render_stream_final(bare, routed_body(lines), bare=True),
                            "✅ 已完成 · 用时 13 秒", inside=[reply_fold("开了 T410，转给 T388。")]),
            "stopped": ended("已保存的结果和记录保留在私聊里。", "⏹ 已停止 · 用时 13 秒",
                             under=[md("**这一轮停了，期间你发的 1 条补充也一起停了**，不会自动重发；要的话改一改再发：\n"
                                       "- 14:36 · 看看这两个样本", element_id=DROPPED)]),
            "failed": ended("任务 #431 处理出错（RuntimeError），未满足结果发布条件。", "⚠️ 执行失败 · 用时 13 秒",
                            under=[result_actions(butler.butler_topic_id("chat"), 431, "检查并继续")]),
        }


def ended_box(job, snapshot, took):
    """The 输入/输出 box of ``job``'s card once its run ended (app/task_details.py _io_figures writes its 输出)."""
    from covey.cards.answer_card import output_lines, output_parts
    from covey.cards.task_figures import io_box
    return io_box(job, output_lines(output_parts(snapshot, took)))


def status_cards_samples():
    """The topic status card the pinned board pins (cards/status_card.py), one per state."""
    from covey.cards.status_card import status_card
    return {
        "running": status_card("T419 · 讨论 Pin 看板是否对前台话题关闭", "在做", "第 9 轮 · 14:25 开始 · 阶段：看图",
                               "这一轮：这个 pin 显示上也有问题"),
        "waiting": status_card("T404 · 排查群晖 Btrfs", "等你回答", "14:12 · 第 3 轮", "问题：把第三版说明发给 Codex？"),
        "done": status_card("T432 · 调研 Mac mini <雷电5>", "做完", "13:01 · 2 轮 · 用时 5 分 49 秒", "结论：没找到 *完全* 对口的实测"),
        "bare": status_card("T7", "排队", "第 1 轮", ""),
        # The checkpoint's steps in place of its phase (app/status_cards.py lines).
        "running_steps": status_card("T445 · 检查点里的步骤与交付进度", "在做", "第 2 轮 · 14:25 开始 · 第 3/5 步：改卡片",
                                     "这一轮：长任务看得出走到第几步"),
        # With its estimate as a time of day (core/progress.py finish_words), and one that may be stuck.
        "running_estimate": status_card("T484 · 进度和预计时间", "在做",
                                        "第 3 轮 · 14:25 开始 · 第 3/5 步：改卡片 · 预计 15:20 左右做完", "这一轮：加上进度"),
        "running_stuck": status_card("T484 · 进度和预计时间", "在做", "第 3 轮 · 14:25 开始 · 阶段：跑全量 · 可能卡住了",
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


def butler_panels():
    from covey.cards.butler_panel import butler_panel_card
    from covey.core.butler_settings import Settings, TopicDefaults

    names = ["bridge", "docs", "scratch"]
    changed = Settings(enabled=False, model="claude-opus-5-5", effort="high", compact_tokens=300_000, redaction=True,
                       topic_defaults=TopicDefaults(harness="codex", model="gpt-6", effort="xhigh", run_mode="worktree",
                                                    workspace="bridge"),
                       start=("readonly", "requested"), trusted=("bridge|edit", "docs|read"),
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
    two pages, 新话题 (with 记住的工作区 under 直接开工 too) and 管家 (its question before 每条直接开一个话题, and in the
    mode 纯话题), 工作区 (with the butler's 用途, being edited, and without), 脱敏 and 诊断. 代答 is an older card's tab."""
    from covey.app.doctor_panel import summarize
    from covey.cards.doctor_card import doctor_card
    from covey.cards.group_manager import group_manager_card
    from covey.cards.butler_panel import WORKSPACE_NOTE, butler_panel_card, notes_button
    from covey.cards.elements import note
    from covey.cards.proxy_panel import proxy_panel_card
    from covey.cards.redaction_panel import redaction_card
    from covey.cards.settings_hub import folded_hub, tab_row
    from covey.cards.workspace_manager import manager_card
    from covey.core.butler_settings import Settings

    tabs = ["topic", "main", "workspace", "redaction", "groups"]

    def row(tab):
        return [tab_row(tab, "c" * 24, tabs)]

    settings = {"enabled": True, "group_chat_id": "oc_fake_proxy", "harness": "codex", "model": "gpt-6-astra",
                "effort": "xhigh", "user_profile": "me", "web_python": "/absolute/python", "workspace": "project"}
    person = {"key": "p2p:ou_fake_z", "kind": "p2p", "target": "ou_fake_z", "name": "张三", "paused": False}
    groups = [{"chat_id": f"oc_fake_group{i}", "active": i % 3 != 0, "external": False} for i in range(4)]
    view = {"revision": 1, "hub": True}
    config = {"default": "bridge", "workspaces": {"bridge": "/absolute/bridge", "docs": "/absolute/docs"}, "recent": {}}
    kept = Settings(trusted=("bridge|read", "docs|write"), workspace_notes=(("bridge", "Bridge 的代码"),))

    def value(kind, **more):
        return {"kind": kind, "token": "c" * 24, "revision": 1, **more}

    return {
        **{page: butler_panel_card("c" * 24, view, Settings(), BUTLER_CHOICES, ["bridge", "docs"],
                                        top=row(page), page=page) for page in ("main", "topic")},
        "topic_trusted": butler_panel_card("c" * 24, view, dataclasses.replace(kept, start=("readonly", "trusted")),
                                           BUTLER_CHOICES, ["bridge", "docs"], top=row("topic"), page="topic"),
        "main_confirm": butler_panel_card("c" * 24, {**view, "confirm": True}, Settings(), BUTLER_CHOICES,
                                          ["bridge", "docs"], top=row("main"), page="main"),
        "main_topics": butler_panel_card("c" * 24, view, Settings(enabled=False), BUTLER_CHOICES, ["bridge", "docs"],
                                         top=row("main"), page="main"),
        "workspace": manager_card("c" * 24, view, config, {}, standalone=True, hub=True, top=row("workspace"),
                                  notes=(dict(kept.workspace_notes), lambda names: notes_button(value, names)),
                                  after=[note(WORKSPACE_NOTE)]),
        "workspace_notes": manager_card("c" * 24, {**view, "notes_edit": True}, config, {}, standalone=True, hub=True,
                                        top=row("workspace"),
                                        notes=(dict(kept.workspace_notes), lambda names: notes_button(value, names)),
                                        after=[note(WORKSPACE_NOTE)]),
        "workspace_no_main": manager_card("c" * 24, view, config, {}, standalone=True, hub=True,
                                          top=[tab_row("workspace", "c" * 24, [t for t in tabs if t != "main"])]),
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
        "main_folded": folded_hub("main", "c" * 24, tabs),
        "topic_folded": folded_hub("topic", "c" * 24, tabs),
        "proxy_folded": folded_hub("proxy", "c" * 24, tabs),
        "groups_folded_no_main": folded_hub("groups", "c" * 24, [t for t in tabs if t != "main"]),
        "workspace_folded": folded_hub("workspace", "c" * 24, tabs),
        # A record still on the retired 其他 folds as 工作区.
        "other_folded": folded_hub("other", "c" * 24, tabs),
        **memory_tab_cards(), **about_tab_cards(),
        # 诊断 (cards/doctor_card.py): service/doctor.py's report in words, read-only; one where every part needs a look.
        "doctor": doctor_card("c" * 24, view, summarize(doctor_report(), "k1", FROZEN - 300),
                              top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_problems": doctor_card("c" * 24, view, summarize(doctor_trouble(), "k1", FROZEN - 60),
                                       top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_failed": doctor_card("c" * 24, view, {"checked_at": FROZEN, "error": "OSError"},
                                     top=[tab_row("doctor", "c" * 24, tabs + ["doctor"])]),
        "doctor_folded": folded_hub("doctor", "c" * 24, tabs + ["doctor"]),
    }


FULL_TABS = ["topic", "main", "workspace", "redaction", "groups", "memory", "nodes", "about"]


def memory_tab_cards():
    """The 设置 card's 记忆 tab (cards/memory_panel.py, COVEY_MEMORY=1; T604 #29, #27): the next turn's block for the chosen
    workspace with each scope's room (one past it: 放不下 2 条), the entries by scope (常驻, 这次带不上, 它总结的, a topic's), a
    section open on its page with an entry asking 确认删除, and no memory at all."""
    from covey.app.prompts import memory_block
    from covey.cards.memory_panel import memory_card
    from covey.cards.settings_hub import tab_row

    def entry(key, text, *, by="你说的", pinned=False, out=False, where="", at=0):
        return {"key": key, "text": text, "by": by, "pinned": pinned, "pinnable": key.startswith("principle:"),
                "where": where, "out": out, "at": at}
    kept = [{"n": "M12", "scope": "global", "by": "owner", "text": "回答先给结论，细节放后面"},
            {"n": "M240", "scope": "global", "by": "owner", "text": "测试通过就合并进 main 并推送，不用再问"},
            {"n": "M31", "scope": "workspace", "by": "owner", "text": "决定：卡片 JSON 变了要真实客户端验收"}]
    data = {"preview": {"workspace": "bridge", "workspaces": ["bridge", "docs"], "count": 3, "chars": 1132, "left": 2,
                        "rooms": [["全局", 1100, 1200], ["工作区 bridge", 32, 800]],
                        "warnings": ["⚠️ 全局放不下 2 条，下一轮不带它们（最早的先放不下）：要一直带上的点「常驻」，用不着的删掉。"],
                        "text": memory_block(kept, 2)},
            "parts": [{"scope": "global", "title": "全局", "rows": [
                          entry("principle:p00000c", "回答先给结论，细节放后面", pinned=True),
                          entry("principle:p0000f0", "测试通过就合并进 main 并推送，不用再问"),
                          entry("knowledge:k000001", "坑：NAS 上别递归扫整个工作区", out=True),
                          entry("principle:p000001", "周报用表格", out=True)]},
                      {"scope": "workspace", "title": "工作区 bridge", "rows": [
                          entry("knowledge:k00001f", "决定：卡片 JSON 变了要真实客户端验收")],
                       "others": "其他工作区：docs 3 条（上面的工作区一行换着看）"},
                      {"scope": "project", "title": "专项", "rows": [
                          entry("project:acc0unt1:a1b2c3", "额度低于 10% 先停新话题", by="它总结的", where="专项「账号额度运行台」")]},
                      {"scope": "topic", "title": "话题", "rows": [
                          entry(f"topic:{'ab' * 8}:d4e5f6", "这个话题不要再问部署", where="T412")]}]}
    view = {"revision": 3, "hub": True, "token": "c" * 24, "workspace": "bridge", "open": "", "page": 0}
    row = [tab_row("memory", "c" * 24, FULL_TABS)]
    empty = {"preview": {"workspace": "bridge", "workspaces": ["bridge"], "count": 0, "chars": 0, "left": 0,
                         "rooms": [["全局", 0, 1200], ["工作区 bridge", 0, 800]], "warnings": [], "text": ""},
             "parts": [{"scope": "global", "title": "全局", "rows": []},
                       {"scope": "workspace", "title": "工作区 bridge", "rows": []},
                       {"scope": "project", "title": "专项", "rows": [], "empty": "专项的记忆没开（COVEY_PROJECT_MEMORY=1）"},
                       {"scope": "topic", "title": "话题", "rows": []}]}
    return {"memory": memory_card("c" * 24, view, data, top=row),
            "memory_open": memory_card("c" * 24, {**view, "open": "global", "confirm": "principle:p0000f0",
                                                  "notice": "已标常驻：回答先给结论，细节放后面"}, data, top=row),
            "memory_empty": memory_card("c" * 24, view, empty, top=row)}


def about_tab_cards():
    """The 设置 card's 能做什么 tab (cards/about_panel.py; T604 #62), read only: what this deployment has on and how to get
    the rest; without Claude Code there is no butler and it says how to get one."""
    from covey.cards.about_panel import about_card
    from covey.cards.settings_hub import folded_hub, tab_row
    from covey.cards.welcome import BUTLER_WORDS, NO_CLAUDE

    features = [
        {"name": "管家", "on": True, "line": "私聊顶层的话先交给管家，能答的直接答，要做的开话题", "switch": "COVEY_BUTLER"},
        {"name": "早晚汇总和看板", "on": True, "line": "每天 09:30、21:30 在私聊发汇总卡；菜单有「看板」", "switch": "COVEY_DIGEST_TIMES"},
        {"name": "专项", "on": True, "line": "要分几个话题、跨几天做的事开成专项，有负责人跟进", "switch": "COVEY_PROJECTS"},
        {"name": "记忆", "on": False, "line": "设 COVEY_MEMORY=1", "switch": "COVEY_MEMORY"},
        {"name": "新话题带上最近话题的要点", "on": False, "line": "设 COVEY_WORKSPACE_GISTS=3（话题要点要整理专项开着才会写）",
         "switch": "COVEY_WORKSPACE_GISTS"},
        {"name": "代答", "on": False, "line": "要一个带代答发送器的扩展（核心没有），再设 COVEY_PROXY_PROFILE",
         "switch": "COVEY_PROXY_PROFILE"}]
    view = {"host": "devbox", "harnesses": ["Claude Code", "Codex"], "down": [["Codex", "没登录"]], "features": features,
            "extensions": []}
    why, how = BUTLER_WORDS[NO_CLAUDE]
    no_butler = [{**features[0], "on": False, "line": f"{why}；{how}"}, *features[1:]]
    return {"about": about_card("c" * 24, view, top=[tab_row("about", "c" * 24, FULL_TABS)]),
            "about_no_butler": about_card("c" * 24, {**view, "harnesses": ["Codex"], "down": [], "features": no_butler,
                                                     "extensions": ["covey_internal"], "notice": "⚠️ 仅机器人主人可以看这一页。"},
                                          top=[tab_row("about", "c" * 24, [t for t in FULL_TABS if t != "main"])]),
            "about_folded": folded_hub("about", "c" * 24, FULL_TABS)}


def overview_cards():
    from covey.cards.overview import overview_card

    board = {"counts": {"waiting": 2, "result": 3, "running": 11, "unseen": 2}, "pinned": 10,
             "waiting": [{"label": "T396", "title": "你为啥没有pin？", "ask": "置顶汇总卡", "since": FROZEN - 180}],
             "result": [{"label": "T394", "title": "群晖公钥", "status": "failed", "at": FROZEN - 600, "seen": False},
                        {"label": "T393", "title": "日志", "status": "cancelled", "at": FROZEN - 900, "seen": True}],
             "running": [{"label": "T391", "title": "logid", "harness": "Codex", "workspace": "DataLab",
                          "status": "queued", "started": None, "phase": ""},
                         {"label": "T390", "title": "重构", "harness": "Claude Code", "workspace": "algo-agent-devskills",
                          "status": "running", "started": FROZEN - 3900, "phase": "跑全量",
                          "progress": ["预计 19:40 左右做完"]}],
             "stale": [{"label": f"T{n}", "title": "旧", "topic": f"topic-{n}", "ask": "补充信息",
                        "since": FROZEN - 2 * 86400} for n in (380, 381)],
             "dismissed": ["T401", "T402"]}
    empty = {"counts": {"waiting": 0, "result": 0, "running": 0}, "pinned": 0, "waiting": [], "result": [],
             "running": [], "stale": []}
    view = {"waiting": [], "running": [], "ended": []}
    # The butler's question waits under 等你 too, under its speaker (core/board_read.py top_questions).
    butler_waiting = {"waiting": [{"label": "管家", "title": "", "ask": "现在部署吗？", "since": FROZEN - 60},
                                  {"label": "T396", "title": "你为啥没有pin？", "ask": "置顶汇总卡", "since": FROZEN - 180}],
                      "running": [], "ended": []}
    return {"pinned_board": overview_card({**view, "board": board}, FROZEN, "14:40"),
            "overview_butler_waiting": overview_card(butler_waiting, FROZEN, "14:40"),
            "pinned_board_empty": overview_card({**view, "board": empty}, FROZEN, "14:40")}


def digest_cards():
    from covey.cards.board import board_card
    from covey.cards.digest import digest_card, folded_board

    def row(label, title, state, took, seen=False):
        return {"label": label, "title": title, "state": state, "took": took, "seen": seen}

    view = {"slot": FROZEN, "since": FROZEN - 9.5 * 3600,
            "waiting": [row("[T396](https://applink.feishu.cn/client/thread/open?open_chat_id=oc_fake_chat)",
                            "你为啥没有pin？", "等你回答：置顶汇总卡", "等了 3分00秒"),
                        row("T397", "新话题", "等你开工", "等了 40秒")],
            "running": [row("T390", "重构", "跑全量 · 预计还要约 40 分钟", "已运行 1小时5分")],
            "done": [row("T394", "群晖公钥", "失败", "用时 2分10秒"),
                     row("T393", "日志", "做完", "用时 45秒", seen=True)]
                    + [row(f"T{n}", "旧", "做完", "用时 1秒") for n in range(380, 388)]}
    from covey.app.digest import cleared

    old = {**view, "dismissable": True, "done": view["done"][:2],
           "stale": [{"topic": f"t{n}", "label": f"T{n}", "title": "旧问题"} for n in (370, 371, 372)]}
    return {"three_parts": digest_card(view),
            "done_only": digest_card({**view, "waiting": [], "running": [], "done": view["done"][:1]}),
            # Results not seen that ended before the digest before are listed too, the heading says how many.
            "older_unseen": digest_card({**view, "waiting": [], "running": [], "done": view["done"][:2], "older": 1}),
            "old_questions": digest_card(old),
            "after_dismiss": digest_card(cleared(old, {"t370", "t371", "t372"})),
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
            "board_empty": board_card({"waiting": [], "stale": [], "running": [], "done": [], "stamp": "08:00"}),
            "board_butler_waiting": board_card({"waiting": [{"topic": "m", "label": "管家", "title": "", "url": "",
                                                             "line": "等你回答：现在部署吗？", "meta": ["等了 1分"]}],
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
            # 到点回来看 (COVEY_REVISIT=1, app/revisits.py): a topic waiting for its time with 取消, one being looked at now.
            "board_revisits": board_card({"waiting": [], "stale": [], "done": [], "stamp": "08:00",
                                          "running": [{"topic": "r", "label": "T42", "title": "跑全量", "line": "现在：在跑测试",
                                                       "url": "https://applink.feishu.cn/t/42", "meta": ["3/5 步"]}],
                                          "revisits": [{"topic": "w", "label": "T40", "title": "盯训练", "at": "15:30",
                                                        "look": "看 loss 降到 0.3 没有", "set_at": 1795000000.5,
                                                        "url": "https://applink.feishu.cn/t/40", "fired": False},
                                                       {"topic": "x", "label": "T41", "title": "等 CI", "at": "08:00",
                                                        "look": "", "set_at": 1795000001.0, "url": "", "fired": True}]}),
            # 我本来想找你的事 (COVEY_PATROL=shadow): what the butler would have acted on, under the other parts.
            # 整理专项 on (cards/project_proposal.py board_rows): the projects and the 整理专项 button.
            "tidy": digest_card({**view, "tidy": {"projects": ["账号额度运行台", "DataLab Prompt 优化"]}}),
            "tidy_none": digest_card({**view, "tidy": {"projects": []}}),
            "wanted": digest_card({**view, "wanted": [
                {"what": "T396 你为啥没有pin？：有个问题等了你 3小时0分", "why": "你一直没回，像是卡住了", "would": "主动来问你"},
                {"what": "专项「重构」有事没人动，已经 1天0小时 没有进展", "why": "收件箱有回执没人读", "would": "叫醒负责人看一眼"}]}),
            **evening_digest_cards(view)}


def evening_digest_cards(view):
    """The evening digest's own parts (app/digest.py daily; T604 #1, #45, #28): 本周找你 and 今天代答 after the three parts, then
    你反复说的 with an open candidate (its 记下 / 不用), one written, one set aside and one 记下 refused; with only a
    candidate (the card is sent for it alone); and its Markdown."""
    from covey.cards.digest import digest_card, digest_text

    remember = [{"id": "c0a1b2c3d", "text": "回答先给结论，细节放后面", "labels": ["T396", "T401"],
                 "sources": "[T396](https://applink.feishu.cn/client/thread/open?open_chat_id=oc_fake_chat)、T401",
                 "workspace": "covey", "state": "open"},
                {"id": "c1a2b3c4d", "text": "测试没跑全量不说通过", "labels": ["T402", "T405"], "workspace": "", "state": "saved"},
                {"id": "c2a3b4c5d", "text": "周报用表格", "labels": ["T403", "T404"], "workspace": "", "state": "declined"},
                {"id": "c3a4b5c6d", "text": "别用表情", "labels": ["T406", "T407"], "workspace": "", "state": "open",
                 "error": "这一组的记忆已有 30 条，先在菜单「设置」→「记忆」里删掉不用的"}]
    evening = {**view, "reached": {"questions": 9, "stopped": 3}, "proxy": {"sent": 5, "unsent": 1}, "remember": remember}
    alone = {"slot": FROZEN, "since": FROZEN - 9.5 * 3600, "waiting": [], "stale": [], "running": [], "done": [],
             "reached": {"questions": 0, "stopped": 0}, "remember": remember[:1]}
    return {"evening": digest_card(evening), "evening_candidate_alone": digest_card(alone),
            "evening_text": {"text": digest_text(evening)}}


def app_setup_cards():
    """飞书应用待补 (cards/app_setup.py): the offer, with and without reviewed scopes and a full menu, the run's
    steps with the QR prompt, the result after a published run and after one stopped at a step."""
    from covey.app.app_setup import need_dict
    from covey.cards.app_setup import offer_card, progress_card, result_card, skipped_card, unavailable_card
    from covey.core import app_manifest

    needs = [need for need in app_manifest.required({"COVEY_DIGEST_TIMES": "09:30",
                                                      "COVEY_GROUP_ALL_MESSAGES": "1"})
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
    from covey.app.welcome import EXAMPLES
    from covey.cards.welcome import FAILED, NO_CLAUDE, SENT, welcome_card

    view = {"host": "devbox", "harnesses": ["Codex", "Claude Code"], "down": [], "workspace": "work",
            "workdir": "/absolute/work", "butler": "", "examples": list(EXAMPLES)}
    return {"butler": welcome_card(view),
            "no_butler": welcome_card({**view, "harnesses": ["Codex", "Pi"], "down": [["Pi", "没登录"]], "butler": NO_CLAUDE}),
            "tried": welcome_card({**view, "tried": {"0": {"state": SENT}, "2": {"state": FAILED}},
                                   "notice": f"「{EXAMPLES[2]}」没发出去，再点一次。"})}


def project_cards():
    """The 专项卡 (cards/project_card.py): the live card in each state, and the lines it shrinks to."""
    from covey.cards import project_card as pc, triage_card

    link = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x"
    ask = "https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=76"
    threads = (pc.Member("a1b2c3" + "0" * 10, pc.RUNNING, "T501", "拆分 SQL 迁移脚本", "第 2/5 步：改写第 3 张表", "12分00秒", link),
               pc.Member("b2c3d4" + "0" * 10, pc.DONE, "T502", "梳理旧表结构", "已完成", "8分00秒", link),
               pc.Member("c3d4e5" + "0" * 10, pc.WAITING, "T503", "决定分区键", "等你回答：按日期还是按租户分区？", "3分00秒", link))
    work = pc.Project(
        project_id="a1b2c3d4e5f6", lead_topic="f" * 32, name="把订单库迁到新集群", status=pc.RUNNING,
        members=threads[:2], elapsed="2小时3分", tokens="33万", lead_state="在做 · 第 3/6 步：核对迁移结果",
        lead_note="先迁小表，大表等周末窗口", inputs=("Claude Code", "Opus 5.5", "思考 high"),
        outputs=("**开始** 09:12　**已运行** 2小时3分", "**token** 33万"), ask_text="把订单库迁到新集群，周末前要有结果",
        ask_at="09:10", ask_url=ask)
    full = pc.Project(**{**work.__dict__, "triage": ("09:40 答了：用哪个库版本？", "10:05 转给你：能不能停服务？"),
                         "memory": (("m1", "迁移窗口在周六凌晨"), ("m2", "旧集群只读到周日")),
                         "library": (("迁移方案.md",), ("表清单.csv",))})
    many = tuple(pc.Member(f"{index:02x}" * 3 + "0" * 10, pc.DONE if index % 3 else pc.RUNNING, f"T{600 + index}", f"话题 {index}",
                           "已完成", "5分00秒") for index in range(11))
    return {"running": pc.project_card(work),
            "running_full": pc.project_card(full),
            "waiting": pc.project_card(pc.Project(**{**full.__dict__, "status": pc.WAITING, "members": threads,
                                                     "need": "T503 按日期还是按租户分区？", "need_url": ask})),
            "paused": pc.project_card(pc.Project(**{**work.__dict__, "status": pc.PAUSED})),
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
            # COVEY_PROJECT_TRIAGE=1: a row of an answer the lead gave has the 不对 button, and the form it opens.
            "running_triage": pc.project_card(pc.Project(**{**full.__dict__, "triage": (
                ("09:40 答了：T501 · 用哪个库版本？ → 新版", "c" * 16), "10:05 转给你：T503 · 能不能停服务？（碰到硬闸）")})),
            "triage_fix_form": triage_card.fix_form("T501 · 用哪个库版本？ → 新版", {"project_id": "a1b2c3d4e5f6", "topic_id": "f" * 32,
                                                                              "token": "c" * 16}),
            "triage_fixed": triage_card.fixed_card("T501 · 用哪个库版本？ → 新版 → 这类库版本的事要等我看过再定"),
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
    row = [tab_row("nodes", "d" * 24, ["workspace", "nodes", "redaction"])]
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


GROUPS = {"task": task_cards, "trace": trace_panels, "launch": launch_cards, "question": question_cards,
          "proxy": proxy_cards, "steer": steer_cards, "recovery": recovery_cards, "rewind": rewind_cards,
          "turn_export": turn_export_panels, "switch": switch_cards, "topic": topic_panels,
          "weekly_usage": usage_cards, "menu": menu_panels, "main_topic": main_topic_cards, "main_task": main_task_cards, "status_card": status_cards_samples,
          "main_thread": butler_panels, "settings": settings_cards,
          "overview": overview_cards, "digest": digest_cards, "app_setup": app_setup_cards, "welcome": welcome_cards,
          "project": project_cards, "tidy": tidy_cards, "nodes": node_cards_samples, "group_ask": group_ask_cards}


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
