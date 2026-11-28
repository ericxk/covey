#!/usr/bin/env python3
"""Shared history and chat-card rendering helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from covey.cards.elements import CARD_NODES, card, element_count, json_size, md
from covey.cards.ui import BODY_PADDING, BODY_SPACING, banded, labelled, role_of, speaker_row, split_ask
from covey.core import card_limits, rich_md, turn_words
from covey.core.text import redact

STREAM_PROGRESS_STATUS_MAX_CHARS = 22000


def plain_markdown(value: Any) -> str:
    """Markdown as the words it shows: no 富文本块 marks (core/rich_md.py plain), code fences, heading, quote or list
    marks at line starts, bold, inline-code backticks or link syntax; secrets redacted as in the process record."""
    text = rich_md.plain(redact(value).replace("\r\n", "\n"))
    text = re.sub(r"(?m)^[ \t]*(?:```+|~~~+)[^\n]*$", " ", text)
    text = re.sub(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d{1,3}[.)][ \t]+)+", "", text)
    text = re.sub(r"!?\[([^\]\n]*)\]\([^)\n]*\)", r"\1", text)
    return text.replace("**", "").replace("`", "")


def compact_text(value: Any, limit: int = 180) -> str:
    """One line of plain text for a card's title: ``plain_markdown`` (not "- **数据生产**" or
    "```PYTHON export OPENAI_API_KEY=$…"), whitespace runs as one space, cut to ``limit`` with "…"."""
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
    for raw in rich_md.plain(str(value or "").replace("\r\n", "\n")).split("\n"):
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
            continue  # "## 结论" over the answer: the card already says 结论 (not 结论 / 结论).
        if text:
            return compact_text(text, limit)
    return ""


LEAD_ENDS = "。.!！?？:：;；,，、 "
GENERIC_LABEL = re.compile(r"^(?:%s)[:：]\s*" % "|".join(sorted(map(re.escape, GENERIC_HEADINGS), key=len, reverse=True)),
                           re.IGNORECASE)


# 露一截 (cards/ui.py peek): how much of a long text's opening stays in view.
# The owner, 2026-11-22 on a phone: two lines (twenty-odd characters) were too little to tell what a reply says, "起码几百字".
# 11:02 the same day: more of the answer in view, the details under it folded (the answer is what the card is for).
PEEK_WIDTH = 1000  # Display width (a CJK character two): about 500 汉字, a dozen lines on a computer.
PEEK_LINES = 20  # Lines at most: short lines (a list) fill the height before the width.
PEEK_FILL = 0.5  # An opening narrower than this share of the width takes the next line in too, cut to fit.
PEEK_CUT = 0.4  # A line is cut at a sentence end, else a pause, no nearer its start than this share of the room left.
# A line that never goes into an opening: a table's row, a code fence, a rule, an image.
_NOT_OPENING = re.compile(r" {0,3}(?:\||```|~~~|<hr|!\[|[-*_](?:\s*[-*_]){2,}\s*$)")
_HEADING_LINE = re.compile(r" {0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*")
_BOLD_LINE = re.compile(r"\*\*[^*].*\*\*[:：]?")
# What a cut never goes inside: inline code, bold, a link, a tag pair, a tag.
_ATOM = re.compile(r"`[^`\n]*`|\*\*[^*\n](?:.*?[^*\n])?\*\*|!?\[[^\]\n]*\]\([^)\n]*\)|<(\w+)\b[^>\n]*>.*?</\1>|<[^>\n]+>")
_ENDS, _PAUSES = "。！？!?；;", "，、：,:"
_ORDERED = re.compile(r" {0,3}(\d{1,9})[.)]\s")
_BLOCK_START = re.compile(r" {0,3}(?:[-*+]\s|\d{1,9}[.)]\s|>|#{1,6}\s)")


def _width(text) -> int:
    return sum(2 if ord(char) > 0x2e7f else 1 for char in str(text))


def _cut(line, room):
    """(head, tail) of ``line`` cut where its words reach ``room`` display width: after the last sentence end that is
    not too near its start (PEEK_CUT), else after the last pause, then "…", else where the room ends, then "…";
    never inside what ``_ATOM`` matches. ('', ``line``) when not even its first part fits."""
    atoms = {found.start(): found.end() for found in _ATOM.finditer(line)}
    used, at, end, pause = 0, 0, None, None
    while at < len(line):
        stop = atoms.get(at, at + 1)
        part = line[at:stop]
        wide = _width(plain_markdown(part) if stop - at > 1 else part)
        if used + wide > room:
            break
        used, at = used + wide, stop
        if part[-1] in _ENDS or (part[-1] == "." and (at == len(line) or line[at] == " ")):
            end = (at, used)
        elif part[-1] in _PAUSES:
            pause = (at, used)
    if at >= len(line):
        return line, ""
    if end and end[1] >= room * PEEK_CUT:
        return line[:end[0]], line[end[0]:].lstrip()
    if pause and pause[1] >= room * PEEK_CUT:
        return line[:pause[0]], line[pause[0]:].lstrip()  # No "…": the fold's title stays as it is once open.
    return (line[:at].rstrip(), line[at:].lstrip()) if at else ("", line)


def drop_part_heading(text) -> str:
    """``text`` (Markdown) without a first line that only names a part ("## 结论", "**总结**") when words follow it: a
    card that shows the text's first lines as they are needs no heading over them (it read as a large 结论 row)."""
    text = str(text or "")
    lines = text.split("\n")
    filled = [n for n, line in enumerate(lines) if line.strip()]
    if len(filled) < 2:
        return text
    first = lines[filled[0]].strip()
    heading = _HEADING_LINE.fullmatch(first) or _BOLD_LINE.fullmatch(first)
    if heading and compact_text(first, len(first) + 1).rstrip(LEAD_ENDS).lower() in GENERIC_HEADINGS:
        return "\n".join(lines[filled[0] + 1:]).lstrip("\n")
    return text


def opening(text, width=PEEK_WIDTH):
    """(opening, rest) of a Markdown text for a peek (cards/ui.py ``peek``): its first lines of words, no wider in all
    than ``width`` and at most PEEK_LINES, and what follows them, so that the two read as the whole text once the fold
    is opened. A heading that only names a part ("## 结论") is left out, another heading is bold when it comes first
    and starts the rest after words; list items and quotes come as they are, but not past a blank line after words; a
    table, code or a rule ends the opening, and a numbered list is not split after words before it. An opening under PEEK_FILL of ``width`` takes the next line in too, cut
    (``_cut``) where it fills it: at a sentence end, else at a pause or where the room ends, with "…". ('', ``text``)
    when the text does not start with words (a table, code); (``text``, '') when it all fits."""
    lines = drop_part_heading(str(text or "").replace("\r\n", "\n")).split("\n")
    at = 0
    taken, used, rest = [], 0, []
    while at < len(lines):
        line = lines[at]
        if not line.strip():
            if taken and used >= width * PEEK_FILL:
                break
            if taken:
                taken.append("")  # The opening is Markdown of its own: its paragraphs stay apart.
            at += 1
            continue
        if _NOT_OPENING.match(line) or len(taken) >= PEEK_LINES:
            break
        shown = line.rstrip()
        wide = _width(plain_markdown(shown))
        if used + wide <= width:
            taken.append(shown)
            used += wide
            at += 1
            continue
        if not taken or used < width * PEEK_FILL:
            head, tail = _cut(shown, width - used)
            if head:
                taken.append(head)
                rest = [tail] if tail else []
                at += 1
        break
    if not taken:
        return "", str(text or "")
    rest = "\n".join([*rest, *lines[at:]]).strip("\n")
    # A numbered list is not split after words before it: the rest would start at "3." and may be numbered anew.
    following = _ORDERED.match(rest)
    if following and int(following.group(1)) > 1:
        start = len(taken)
        while start > 0 and _ORDERED.match(taken[start - 1]):
            start -= 1
        if 0 < start < len(taken):
            taken, rest = taken[:start], "\n".join(taken[start:]) + "\n" + rest
    return "\n".join(taken).strip("\n"), rest


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


ANSWER = "答案"  # The title of an answer on the card its run streamed it on.


def stopped_sentence(job_id: Any) -> str:
    """How a stopped run's result text starts (app/worker.py)."""
    return f"任务 #{job_id} 已停止。"


def render_stream_final(job: Any, final_text: str, *, status: str = "done", answer_elsewhere: bool = False,
                        outcome_shown: bool = False, bare: bool = False) -> str:
    """The result under 任务详情. Its title says how the run ended, unless the status line at the top
    already does (``outcome_shown``): then only what that line lacks stays. An answer keeps its title, except
    on a slim card (``bare``, the butler's): the answer is the card, its status line under it says 有结果."""
    title = {"failed": "停住了", "cancelled": "已停止", "waiting_user": "等你"}.get(
        status, "有结果" if answer_elsewhere else ANSWER)
    final_text = rich_md.card_markdown(final_text)  # One markdown element: its 富文本块 in words (core/rich_md.py).
    if bare and title == ANSWER:
        return final_text
    if outcome_shown and title != ANSWER:
        if status == "cancelled":
            final_text = final_text.removeprefix(stopped_sentence(getattr(job, "job_id", ""))) or final_text
        return final_text
    return render_stream_card_content(title, [final_text])


outcome_line = turn_words.outcome_line  # The task card's status line once its run ended.


# A card message's request is at most 30 KB, with its content escaped once more: room for that.
ANSWER_CARD_BYTES = 25_000
SENT_BYTES = 29_000  # A card's JSON escaped once more, as a message request carries it, under Feishu's 30 KB.
PAGE_NODES = CARD_NODES - 10  # Tagged nodes a card may have, a few kept free (CardKit refuses one over 200: 300305).


def one_card(built) -> bool:
    """Whether one card message can carry ``built``: ANSWER_CARD_BYTES of JSON, SENT_BYTES as the request carries it and
    PAGE_NODES tagged nodes (cards/pages.py fits, which adds CardKit's rendered size for a card it creates)."""
    return (isinstance(built, dict) and json_size(built) <= ANSWER_CARD_BYTES
            and card_limits.sent_size(built) <= SENT_BYTES and element_count(built) <= PAGE_NODES)


def answer_summary(label: str, status: str, lead: str) -> str:
    """The chat-list preview of a turn's task card once it closes and of its answer card: the topic (or task), the
    outcome's word and the reply's lead, "T28 有结果 · lead"; without a lead, the outcome's word alone."""
    return f"{label} {outcome_line(status)}" + (f" · {redact(lead)}" if lead else "")


def bare_answer_summary(status: str, lead: str) -> str:
    """The chat-list preview of the answer card of a butler turn that sent no card (app/worker.py
    TurnDelivery.bare): no task number, which the owner never saw; "已答：" and the
    reply's lead as its card's status line would say it (app/butler_reply.py outcome_words), another outcome's word
    before the lead; without a lead, the outcome's word."""
    words = outcome_line(status)
    if not lead:
        return words
    return f"{'已答' if status == 'done' else words}：{redact(lead)}"


# A topic's row in the topic list shows its latest message's preview, and a task card's title cannot change
# once sent: without these a running topic read the same as a stopped or failed one, only an answer card's
# "✅ T24 · lead" stood out. The task card's preview follows its task: queued, running
# (with the checkpoint's phase), waiting for the answer to a model's question, then task_card_summary.
SUMMARY_PHASE_CHARS = 30


def queued_summary(label: str, title: str) -> str:
    """The chat-list preview of a task card while its task waits: "T28 排队 · " and the card's title."""
    return f"{label} 排队 · {title}"


def running_summary(label: str, title: str, phase: str = "", asking: bool = False) -> str:
    """The chat-list preview of a task card while its task runs: "T28 在做 · " and the checkpoint's phase, else the
    card's title; "T28 等你" while a question of the model waits for the answer."""
    if asking:
        return f"{label} {turn_words.BOARD['waiting']}"
    return f"{label} {turn_words.RUNNING} · {compact_text(phase, SUMMARY_PHASE_CHARS) if phase else title}"


def task_card_summary(label: str, status: str, line: str = "") -> str:
    """The chat-list preview of a closed task card whose reply went out as its own answer card: the topic and
    the outcome with its time, "T28 有结果 · 用时 3 分 39 秒" (``line``: the status line, else the outcome's
    words). The answer card keeps "T28 有结果 · lead": with the lead on both, a topic's replies read it twice
    in the chat."""
    return f"{label} {line or outcome_line(status)}"


# A level 3-6 heading, its closing #s apart; up to 3 spaces before it, as Markdown allows.
_MINOR_HEADING = re.compile(r" {0,3}(#{3,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")


def bold_minor_headings(text: str) -> str:
    """``text`` with its level 3-6 headings' words in bold, outside code: a card's Markdown shows only # and ##
    bold and a little larger, ### to ###### as plain body text (as probed in a test group)."""
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


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})([^`]*)")
# Blocks a single line of which means more than its code: a metric (core/rich_md.py) and a one-line diff.
KEEP_FENCED = ("metrics", "diff", "patch")


def inline_one_line_code(text: str) -> str:
    """``text`` with each fenced block of a single line as inline code on a line of its own: a card numbers
    a code block's lines, and a lone path read "1 runs/…". A line with a backtick
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
        kept = (opening.group(2).strip().split() or [""])[0].lower() in KEEP_FENCED
        code = lines[i + 1].strip() if end == i + 2 < len(lines) and not kept else ""
        out.extend([f"`{code}`"] if code and "`" not in code else lines[i:end + 1])
        i = end + 1
    return "\n".join(out)


FOLD_ID = "relay_reply"
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def reply_body(text) -> str:
    """A turn's reply as a card shows it: its line ends as \\n, one-line code inline, its small headings bold."""
    return bold_minor_headings(inline_one_line_code(str(text or "").replace("\r\n", "\n").strip()))


def reply_size(text):
    """(lines, characters, tables) of a reply's Markdown: its non-blank lines, its characters without spaces, and its
    tables (a line of dashes under a header row)."""
    body = str(text or "")
    lines = [line for line in body.splitlines() if line.strip()]
    tables = sum(1 for line in lines if "|" in line and "-" in line and TABLE_RULE.match(line))
    return len(lines), len("".join(body.split())), tables


def reply_shown(text, element_id=FOLD_ID, *, label="", closable=False, rich=True) -> list:
    """A reply (Markdown) whole on its card, ``label`` grey before it ("这一轮输出"): the owner, 2026-11-22, after the
    folds he tried (露一截 with a line under it, then the answer itself opening with a round arrow): 不折叠，直接展示全文.
    A reply too large for a card goes as Markdown before it (the callers' CARD_TEXT_BYTES and ANSWER_CARD_BYTES). One
    element in a list, or two when the reply ends with what the turn reported on its way (core/trace.py with_stage_notes):
    those go in a closed fold after it, titled with their size (the owner, 2026-11-25: 这一轮中途的汇报 太长，默认折叠，点一下
    展开). ``closable``: a click on the fold's text closes it too (``stages_fold``), on a card the Bridge keeps and patches
    (the relay's, app/fold_close.py); elsewhere (a question's copy, which CardKit holds) only its title closes it.
    Its 富文本块 as their elements (cards/rich_body.py), or with ``rich`` False in one markdown element's words (a card
    that would hold too many nodes with them)."""
    from covey.cards.rich_body import reply_elements

    main, stages = split_stages(text)
    shown = (reply_elements(main, element_id, label, polish=bold_minor_headings) if rich else
             [md(labelled(rich_md.card_markdown(main), label, main), element_id=element_id)])
    if stages:
        shown.append(stages_fold(stages, element_id + (STAGES_CLOSABLE + "0" if closable else STAGES_SUFFIX), closable))
    return shown


STAGES_SUFFIX = "_stages"
# A fold whose text closes it is <element_id>_s<n>: each close draws it under the next n, since the client keeps a fold
# open or closed by its ID (docs/cards.md 启动卡和换配置卡), and an element ID is at most 20 characters (CardKit).
STAGES_CLOSABLE = "_s"
FOLD_CLOSE_KIND = "bridge_fold_close"  # A click on the text of a fold of 这一轮中途的汇报: app/fold_close.py closes it.


def stages_fold(stages, fold_id, closable=False) -> dict:
    """这一轮中途的汇报 closed under its size. ``closable``: its text is one button (FOLD_CLOSE_KIND, naming ``fold_id``)
    whose click closes it where the owner has read to, not at the title he scrolled past (the owner, 2026-11-25: 随便点
    一下这个过程项的文字，它自己就能折起来)."""
    from covey.cards.elements import collapsible

    chars = len("".join(stages.split()))
    text = md(stages)
    if closable:
        text = {"tag": "interactive_container", "width": "fill", "padding": "0px 0px 0px 0px", "has_border": False,
                "background_style": "default", "elements": [text],
                "behaviors": [{"type": "callback", "value": {"kind": FOLD_CLOSE_KIND, "fold": fold_id}}]}
    title = f"这一轮中途的汇报 · 约 {max(10, round(chars / 10) * 10)} 字（点开看" + ("，点正文收起）" if closable else "）")
    return collapsible(fold_id, title, [text], quiet=True, spacing="6px")


def split_stages(text) -> tuple:
    """(reply, stage notes) of a text that ends with STAGE_HEADING's section; (text, '') without one."""
    from covey.core.trace import STAGE_HEADING

    text = str(text or "")
    found = re.search(r"(?:\n\s*---\s*\n|^)\s*" + re.escape(STAGE_HEADING) + r"[ \t]*\n", text)
    if not found:
        return text, ""
    stages = text[found.end():].strip()
    return (text[:found.start()].rstrip(), stages) if stages else (text, "")


ANSWER_ID = "answer_reply"  # The answer on its card, as the relay's is FOLD_ID.


def answer_card(text: str, summary: str, tail: list | tuple = (), head: list | tuple = (), *, rich=True) -> dict | None:
    """A turn's answer sent on its own, as card Markdown (``bold_minor_headings``, ``inline_one_line_code``), in the
    look the relay has (docs/ui.md 5.2 聊天结果卡): the chat list previews ``summary`` where a Markdown message showed
    its first characters ("#### 先确认…"). ``head``: what goes above it, a block that becomes the card's row
    (cards/ui.py speaker_row: the "你问" block at the private chat's top level, the topic's row in its own thread,
    cards/answer_card.py topic_head), a click on it opening what the block links to; a speaker whose row does not
    quote the owner (a topic, a lead) has his words under it as one line that opens (cards/answer_card.py
    ``words_fold``). The answer whole (``reply_shown``), a leading "## 结论" left out. ``tail``: what follows the answer (cards/answer_card.py tail: 产物 and the 输入/输出 lines), a card
    that can be updated then (its 产物 once uploaded). None when it would not fit one card message; it then goes as
    Markdown."""
    from covey.cards.answer_card import words_fold

    headed, given = bool(head), head
    meta, head = split_ask(head)  # The "你问" block goes into the speaker's row (cards/ui.py speaker_row).
    body = drop_part_heading(reply_body(text))
    answer = reply_shown(body, ANSWER_ID, rich=rich) if body.strip() else (
        [] if headed else [{"tag": "markdown", "content": "", "text_size": "normal_v2"}])
    elements = [*head, *answer, *tail]
    if meta:
        quoted = role_of(meta.get("who")) == "butler"  # The butler's row quotes his words itself.
        words = [] if quoted else [part for part in (words_fold(meta),) if part]
        built = banded(card([speaker_row(meta), *words, *elements], summary, update_multi=bool(tail),
                            padding=BODY_PADDING, spacing=BODY_SPACING))
    else:
        built = banded(card(elements, summary, update_multi=bool(tail)))
    if rich and rich_md.rich(body) and not one_card(built):
        return answer_card(text, summary, tail, given, rich=False)  # Its blocks in words, one markdown element, first.
    return built if len(json.dumps(built, ensure_ascii=False).encode("utf-8")) <= ANSWER_CARD_BYTES else None
