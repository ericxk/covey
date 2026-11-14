#!/usr/bin/env python3
"""Shared history and chat-card rendering helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from covey.cards.elements import card
from covey.core import turn_words
from covey.core.text import redact

STREAM_PROGRESS_STATUS_MAX_CHARS = 22000


def plain_markdown(value: Any) -> str:
    """Markdown as the words it shows: no code fences, heading, quote or list marks at line starts,
    bold, inline-code backticks or link syntax; secrets redacted as in the process record."""
    text = redact(value).replace("\r\n", "\n")
    text = re.sub(r"(?m)^[ \t]*(?:```+|~~~+)[^\n]*$", " ", text)
    text = re.sub(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d{1,3}[.)][ \t]+)+", "", text)
    text = re.sub(r"!?\[([^\]\n]*)\]\([^)\n]*\)", r"\1", text)
    return text.replace("**", "").replace("`", "")


def compact_text(value: Any, limit: int = 180) -> str:
    """One line of plain text for a card's title: ``plain_markdown`` (T137 read "- **RL数据生产**",
    T130 "```PYTHON export OPENAI_API_KEY=$…"), whitespace runs as one space, cut to ``limit`` with "…"."""
    text = re.sub(r"\s+", " ", plain_markdown(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


LEAD_CHARS = 60
# Headings that name a part, not what it says: an answer's lead is the line after them.
GENERIC_HEADINGS = {"结论", "总结", "小结", "结果", "答案", "回答", "摘要", "概要", "要点", "summary", "tl;dr", "tldr",
                    "conclusion", "answer", "result"}


def answer_lead(value: Any, limit: int = LEAD_CHARS) -> str:
    """A Markdown answer's lead as plain text cut to ``limit`` with "…": its first line outside code,
    whole when it is a heading (or bold alone), else up to its first sentence end; trailing
    punctuation dropped. "" when there is none (only code, tables or nothing)."""
    fence = ""
    for raw in str(value or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        marker = re.match(r"```+|~~~+", line)
        if fence:
            if marker and marker.group(0).startswith(fence):  # Closed by as long a run of the same mark.
                fence = ""
            continue
        if marker:
            fence = marker.group(0)
            continue
        if line.startswith("|") or re.fullmatch(r"[-*_= ]{3,}", line):
            continue
        heading = re.match(r"#{1,6}\s", line) or re.fullmatch(r"\*\*[^*].*\*\*[:：]?", line)
        text = compact_text(line, len(line) + 1)
        if not text:
            continue
        end = None if heading else re.search(r"[。！？]|[.!?](?=\s|$)", text)
        text = (text[:end.start()] if end and end.start() else text).rstrip("。.!！?？:：;；,，、 ")
        if heading and text.lower() in GENERIC_HEADINGS:
            continue  # "## 结论" over the answer: the card already says 结论 (T357 #2746 read 结论 / 结论).
        if text:
            return compact_text(text, limit)
    return ""


def question_preview(value: Any, *, max_lines: int, max_chars: int) -> str:
    text = str(value or "").strip().replace("\r\n", "\n").replace("\r", "\n") or "(空)"
    lines = text.split("\n")
    preview = "\n".join(lines[:max_lines])
    truncated = len(lines) > max_lines or len(preview) > max_chars
    preview = preview[:max_chars].rstrip()
    preview = preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Quote plain text, escaping user Markdown so headings, lists and fences
    # cannot change the surrounding card layout. Hard breaks preserve lines.
    escaped = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", preview)
    # A blank line keeps a full-width space: a bare ">" let Feishu run the lines around it together.
    rendered = "  \n".join(f"> {line}" if line else "> \u3000" for line in escaped.split("\n"))
    if truncated:
        rendered += "\n>\n> …（内容较长，完整内容见原消息）"
    return rendered


def question_display_text(row: dict[str, Any]) -> str:
    if "display_content" in row:
        return str(row["display_content"])
    text = str(row.get("content") or "")
    # Older history rows predate the separate display field. Match the exact
    # generated header and recorded parent ID; leave model history untouched.
    parent_id = str(row.get("parent_id") or "")
    if parent_id:
        text = text.split(f"\n\n回复上下文：\n- parent_id: `{parent_id}`", 1)[0]
    return text


def recent_user_questions(rows: list[dict[str, Any]], current_text: str, limit: int = 3) -> list[str]:
    questions = [
        question_display_text(row).strip()
        for row in rows
        if row.get("role") == "user"
        and str(row.get("content") or "").strip()
    ]
    if questions and questions[-1] == current_text.strip():
        questions = questions[:-1]
    return questions[-limit:]


def render_history(rows: list[dict[str, Any]]) -> str:
    rendered = []
    for row in rows:
        role = row.get("role", "user")
        content = str(row.get("content", "")).strip()
        if content:
            rendered.append(f"{role}: {content}")
    return "\n".join(rendered[-12:])


def render_stream_card_content(body_title: str, body_lines: list[str]) -> str:
    lines = [f"**{body_title}**", *body_lines]
    return "\n".join(lines)


def render_stream_status(
    job: Any,
    status: str,
    events: list[str] | None = None,
    *,
    queue_depth: int = 0,
) -> str:
    lines = [f"📌 阶段：{status}"]
    if queue_depth:
        lines.append(f"队列剩余：约 {queue_depth} 个任务")
    if events:
        lines.extend(["", "📡 执行进度：", *render_stream_progress_lines(events, max_chars=STREAM_PROGRESS_STATUS_MAX_CHARS)])
    return render_stream_card_content("执行进度", lines)


def render_stream_progress_lines(events: list[str], *, max_chars: int) -> list[str]:
    lines = [f"- {event}" for event in events]
    if sum(len(line) + 1 for line in lines) <= max_chars:
        return lines

    head: list[str] = []
    tail: list[str] = []
    head_budget = max(0, max_chars // 3)
    used = 0
    for line in lines:
        cost = len(line) + 1
        if head and used + cost > head_budget:
            break
        if used + cost > max_chars:
            break
        head.append(line)
        used += cost

    placeholder = "- ... 已折叠中间 {omitted} 条进度，避免超过卡片大小上限"
    used += len(placeholder.format(omitted=len(lines))) + 1
    head_count = len(head)
    for line in reversed(lines[head_count:]):
        cost = len(line) + 1
        if tail and used + cost > max_chars:
            break
        if used + cost > max_chars:
            break
        tail.append(line)
        used += cost
    tail.reverse()

    omitted = max(0, len(lines) - len(head) - len(tail))
    if omitted <= 0:
        return head + tail
    return head + [placeholder.format(omitted=omitted)] + tail


def stopped_sentence(job_id: Any) -> str:
    """How a stopped run's result text starts (app/worker.py)."""
    return f"任务 #{job_id} 已停止。"


def render_stream_final(job: Any, final_text: str, *, status: str = "done", answer_elsewhere: bool = False,
                        outcome_shown: bool = False, bare: bool = False) -> str:
    """The result under 任务详情. Its title says how the run ended, unless the status line at the top
    already does (``outcome_shown``): then only what that line lacks stays. An answer keeps its title, except
    on a slim card (``bare``, the main thread's): the answer is the card, its status line under it says ✅."""
    state, title = {
        "failed": ("执行失败", "⚠️ 执行失败"),
        "cancelled": ("已停止", "已停止"),
        "waiting_user": ("等待回答", "需要你回答"),
    }.get(status, ("已完成", "✅ 已完成" if answer_elsewhere else "✅ 答案"))
    if bare and title == "✅ 答案":
        return final_text
    if outcome_shown and title != "✅ 答案":
        if status == "cancelled":
            final_text = final_text.removeprefix(stopped_sentence(getattr(job, "job_id", ""))) or final_text
        return final_text
    return render_stream_card_content(title, [final_text])


outcome_line = turn_words.outcome_line  # The task card's status line once its run ended.


# A card message's request is at most 30 KB, with its content escaped once more: room for that.
ANSWER_CARD_BYTES = 25_000


def answer_summary(label: str, status: str, lead: str) -> str:
    """The chat-list preview of a turn's task card once it closes and of its answer card: the outcome's
    mark, the topic (or task) and the reply's lead, "✅ T298 · lead"; without a lead, the outcome's words."""
    mark, _, words = outcome_line(status).partition(" ")
    return f"{mark} {label} · {redact(lead) if lead else words}"


def bare_answer_summary(status: str, lead: str) -> str:
    """The chat-list preview of the answer card of a main-thread turn that sent no card (app/worker.py
    TurnDelivery.bare): no task number, which the owner never saw (ACCEPT-322: "✅ #3308 · 5"); "✅ 已答：" and the
    reply's lead as its card's status line would say it (app/main_reply.py outcome_words), another outcome's mark
    and words before the lead; without a lead, the outcome's words."""
    mark, _, words = outcome_line(status).partition(" ")
    if not lead:
        return f"{mark} {words}"
    return f"{mark} {'已答' if status == 'done' else words}：{redact(lead)}"


# A topic's row in the topic list shows its latest message's preview, and a task card's title cannot change
# once sent: without these a running topic read the same as a stopped or failed one, only an answer card's
# "✅ T324 · lead" stood out (T314, 2026-11-02). The task card's preview follows its task: queued, running
# (with the checkpoint's phase), waiting for the answer to a model's question, then task_card_summary.
SUMMARY_PHASE_CHARS = 30


def queued_summary(label: str, title: str) -> str:
    """The chat-list preview of a task card while its task waits: "🕒 T298 · 排队中 · " and the card's title."""
    return f"🕒 {label} · 排队中 · {title}"


def running_summary(label: str, title: str, phase: str = "", asking: bool = False) -> str:
    """The chat-list preview of a task card while its task runs: "⏳ T298 · 执行中 · " and the checkpoint's phase,
    else the card's title; "❓ T298 · 需要你回答" while a question of the model waits for the answer."""
    if asking:
        return f"❓ {label} · 需要你回答"
    return f"⏳ {label} · 执行中 · {compact_text(phase, SUMMARY_PHASE_CHARS) if phase else title}"


def task_card_summary(label: str, status: str, line: str = "") -> str:
    """The chat-list preview of a closed task card whose reply went out as its own answer card: the topic and
    the outcome with its time, "T298 · 已完成 · 用时 3 分 39 秒" (``line``: the status line, else the outcome's
    words). The answer card keeps "✅ T298 · lead": with the lead on both, a topic's replies read it twice
    in the chat (2026-11-02 review). Only an outcome other than 已完成 keeps its mark."""
    mark, _, words = (line or outcome_line(status)).partition(" ")
    return f"{label} · {words}" if status == "done" else f"{mark} {label} · {words}"


# A level 3-6 heading, its closing #s apart; up to 3 spaces before it, as Markdown allows.
_MINOR_HEADING = re.compile(r" {0,3}(#{3,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")


def bold_minor_headings(text: str) -> str:
    """``text`` with its level 3-6 headings' words in bold, outside code: a card's Markdown shows only # and ##
    bold and a little larger, ### to ###### as plain body text (the 2026-11-02 probe in 「C队 验收测试」)."""
    lines, fence = [], ""
    for line in text.split("\n"):
        marker = re.match(r" {0,3}(```+|~~~+)", line)
        if fence:
            if marker and marker.group(1).startswith(fence):  # Closed by as long a run of the same mark.
                fence = ""
        elif marker:
            fence = marker.group(1)
        elif heading := _MINOR_HEADING.fullmatch(line):
            words = heading.group(2).replace("**", "").strip()
            line = f"{heading.group(1)} **{words}**" if words else line
        lines.append(line)
    return "\n".join(lines)


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})[^`]*")


def inline_one_line_code(text: str) -> str:
    """``text`` with each fenced block of a single line as inline code on a line of its own: a card numbers
    a code block's lines, and a lone path read "1 misc-tasks/…" (2026-11-02 review). A line with a backtick
    keeps its block."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        opening = _FENCE.fullmatch(lines[i])
        if not opening:
            out.append(lines[i])
            i += 1
            continue
        fence = opening.group(1)
        close = re.compile(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*")
        end = next((j for j in range(i + 1, len(lines)) if close.fullmatch(lines[j])), len(lines))
        code = lines[i + 1].strip() if end == i + 2 < len(lines) else ""
        out.extend([f"`{code}`"] if code and "`" not in code else lines[i:end + 1])
        i = end + 1
    return "\n".join(out)


def answer_card(text: str, summary: str, tail: list | tuple = (), head: list | tuple = ()) -> dict | None:
    """A turn's answer sent on its own, as a card of one Markdown block (``bold_minor_headings``,
    ``inline_one_line_code``): the chat list previews ``summary`` where a Markdown message showed its first
    characters ("#### 先确认…", T288). ``tail``: what follows the answer (cards/answer_card.py tail: 产物 and the
    输入/输出 box), a card that can be updated then (its 产物 once uploaded); ``head``: what goes above it (the
    "你问" block of an answer at the private chat's top level). None when it would not fit one card message; it then
    goes as Markdown."""
    built = card([*head, {"tag": "markdown", "content": bold_minor_headings(inline_one_line_code(text)),
                          "text_size": "normal_v2"}, *tail], summary, update_multi=bool(tail))
    return built if len(json.dumps(built, ensure_ascii=False).encode("utf-8")) <= ANSWER_CARD_BYTES else None
