"""The task card's status line: the status and its time styled (status_markdown, the slim card's too), and its
elements, in two parts on a card that has the row of tiles (status_elements)."""

from __future__ import annotations

import re
from typing import Any

from covey.cards.elements import button_row, grey, topic_button
from covey.cards.ui import state_word
from covey.core.turn_words import TONES


ACTIVITY_ELEMENT_ID = "activity"
# What the status line says besides the status and its time (阶段, 当前, why it stops), under the row of the
# status and the tiles and as wide as the card (the "A 一行仪表" sample). A card
# sent before has no such element: its status line says it all (StreamingReply.status_rows).
STATUS_MORE_ID = "activity_more"
# The 看看 button under the status line of a turn that may be stuck (core/progress.py maybe_stuck; Cards.show_stuck): a
# read-only look at its processes, log and latest output (app/stuck_check.py), its kind LOOK_KIND in app/card_actions.py.
LOOK_ID = "stuck_look"
LOOK_KIND = "bridge_job_look"
LOOK_LABEL = "看看"
RAN = "已运行 "
RAN_MINUTES = 3  # From this many minutes on, 已运行 says minutes alone: the line is written once a minute then.


def marked(head: str) -> str:
    """A status line's first word (core/turn_words.py TONES) as its state's dot and word in its colour (cards/ui.py
    state_word); any other words as they are (an older card's ✅ 已完成, a device's line)."""
    return state_word(TONES[head]) if head in TONES else head


def status_markdown(text: str) -> str:
    """The status line as the card shows it, from the plain text its producers write (core/longtask.py
    TaskMonitor, cards/review.py outcome_line, STOPPING; a job record keeps the plain text): the first
    line's status as a ``##`` heading, which a card's Markdown draws bold and a little larger (cards/review.py
    bold_minor_headings); its words after the first " · " and the lines under it (what
    the task does now) small and grey below it: beside the heading they were drawn as large and wrapped in a
    topic's side panel (a client check). The status word (在做, 有结果, 停住了 …) is its state's dot and
    word in its colour (``marked``). Text already styled is left as it is."""
    text = str(text or "")
    if not text.strip() or text.startswith("## "):
        return text or " "
    first, _, rest = text.partition("\n")
    head, _, details = first.partition(" · ")
    return "\n".join(["## " + marked(head)] + [grey(line) for line in [details, *rest.split("\n")] if line.strip()])


def activity_element(content: str = "等待处理") -> dict[str, Any]:
    """The task card's status line (``activity``), styled by ``status_markdown``."""
    return {"tag": "markdown", "element_id": ACTIVITY_ELEMENT_ID, "text_size": "notation",
            "content": status_markdown(content)}


def slim_status_markdown(text: str) -> str:
    """The slim card's status line (SLIM_DETAILS_ID's card) from the plain text its producers write, on one line:
    the status and its time (``status_rows``) in the card's text, the rest grey after " · ", its lines joined
    with " · " too (在做 · 已运行 6 秒 · 现在：…; 有结果 · 用时 13 秒), its status word ``marked``. No ``##`` heading.
    Text already styled is left as it is."""
    text = str(text or "")
    if not text.strip() or text.startswith("## "):
        return text or " "
    first, _, more = text.partition("\n")
    head, *segments = first.split(" · ")
    head = marked(head)
    timed = [part for part in segments
             if part.startswith(("已运行 ", "用时 ")) or re.search(r"\d{1,2}:\d{2} 结束$", part)]
    rest = [part.strip() for part in [*(part for part in segments if part not in timed), *more.split("\n")]
            if part.strip()]
    return " · ".join([head, *timed]) + (" · " + grey(" · ".join(rest)) if rest else "")


def slim_activity_element(content: str = "等待处理") -> dict[str, Any]:
    """The slim card's status line (``activity``, the same ID), styled by ``slim_status_markdown``."""
    return {"tag": "markdown", "element_id": ACTIVITY_ELEMENT_ID, "text_size": "normal_v2",
            "content": slim_status_markdown(content)}


def slim_start(content: str) -> tuple[str, str]:
    """A slim card's first (status line, body) from the body it is created with: a queued card's body
    (cards/review.py render_stream_status: **执行进度**, 📌 阶段：…, its notes) goes in the status line, its
    stage first and its notes after it; any other body stays the body."""
    lines = [line for line in str(content or "").split("\n") if line.strip()]
    if not lines or lines[0] != "**执行进度**" or len(lines) < 2 or not lines[1].startswith("📌 阶段："):
        return "等待处理", content
    stage = lines[1].removeprefix("📌 阶段：")
    notes = [line.removeprefix("- ").strip() for line in lines[2:] if line.strip() != "📡 执行进度："]
    return "\n".join(["⏳ " + stage, *notes]), ""


def status_rows(text: str, io: bool = False) -> tuple[str, str]:
    """``text``, a status line as its producers write it, split for a card with STATUS_MORE_ID: the status with
    how long it has run or when it ended (在做 · 已运行 6 分 19 秒, 有结果 · 20:01 结束), beside the tiles;
    the rest (阶段, 当前, why it stops, a retry's attempt and reason), under them. Both as plain text. On a card
    with the 输入/输出 box (``io``) 已运行 is in 输出 (``ran_of``), on neither part."""
    text = str(text or "")
    if text.startswith("## "):
        return text, ""
    first, _, rest = text.partition("\n")
    head, *segments = first.split(" · ")
    if io:
        segments = [part for part in segments if not part.startswith(RAN)]
    beside = [part for part in segments if part.startswith(RAN) or part.endswith(" 结束")]
    below = [part for part in segments if part not in beside]
    lines = [" · ".join(below), *rest.split("\n")]
    return " · ".join([head, *beside]), "\n".join(line for line in lines if line.strip())


def ran_of(text: str) -> str:
    """How long the run of the status line ``text`` has gone (its "已运行 …" part), as the 输出 box shows it: minutes
    alone from RAN_MINUTES on ("12分", not "12分05秒" a minute long); "" without one."""
    first = str(text or "").partition("\n")[0]
    return ran_shown(next((part[len(RAN):].strip() for part in first.split(" · ")[1:] if part.startswith(RAN)), ""))


def ran_shown(ran: str) -> str:
    """``ran``, an 已运行 as core/metrics.py took_text writes it, as the 输出 box shows it (``ran_of``)."""
    found = re.fullmatch(r"(\d+)分\d+秒", ran)
    return f"{found.group(1)}分" if found and int(found.group(1)) >= RAN_MINUTES else ran


def more_element(text: str = "") -> dict[str, Any]:
    """STATUS_MORE_ID with the rest of a status line (``status_rows``): its first line as the card's text, the
    lines under it grey; empty when there is none (CardKit takes "" and draws nothing)."""
    first, *rest = text.split("\n") if text.strip() else [""]
    content = "\n".join([first, *(grey(line) for line in rest)])
    return {"tag": "markdown", "element_id": STATUS_MORE_ID, "text_size": "normal_v2", "content": content}


def status_elements(content: str, rows: bool, slim: bool = False, io: bool = False) -> list[dict[str, Any]]:
    """What a write of the status line ``content`` puts on a card: the line alone (``activity_element``; on a slim
    card, ``slim``, ``slim_activity_element``), or on a card with STATUS_MORE_ID (``rows``) its two parts
    (``status_rows``; with the 输入/输出 box, ``io``, without 已运行)."""
    if slim:
        return [slim_activity_element(content)]
    if not rows:
        return [activity_element(content)]
    top, more = status_rows(content, io)
    return [activity_element(top), more_element(more)]


def look_element(job: Any) -> dict[str, Any]:
    """The 看看 button (LOOK_ID) of ``job``'s task card, in a row of its own under the status line."""
    value = {"kind": LOOK_KIND, "topic_id": getattr(job, "topic_id", "") or "", "job_id": getattr(job, "job_id", 0)}
    return button_row([topic_button(LOOK_LABEL, element_id="stuck_look_button", value=value)], LOOK_ID)
