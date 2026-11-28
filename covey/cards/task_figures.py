"""The task card's figures: the tiles (开始 or 用时, token, 速度, 上下文; figures_element) and the grey 输入/输出 box
(io_box), the same box as the answer card's (cards/answer_card.py)."""

from __future__ import annotations

import json
from typing import Any

from covey.cards import answer_card
from covey.cards.elements import grey, md, tiles
from covey.cards.facts import context_text, speed as facts_speed, start_text, token_total, usage_figures
from covey.core.metrics import run_seconds, took_text


# The task card's dashboard (the "A 仪表盘" sample): the status line and the tiles of figures
# (开始 or 用时, token, 速度, 上下文) by their own ID, written with the process panel and 任务详情.
STATUS_BLOCK_ID = "status_block"
FIGURES_ELEMENT_ID = "task_figures"
# The task card's grey box of 输入 and 输出 (the "E" sample; cards/answer_card.py box): under the status
# line, a line each; its 输出 text is FIGURES_ELEMENT_ID, a Markdown element, so an older release writes its one
# line of figures there (StreamingReply.io_box). A slim card has it first in its 详情.
IO_BOX_ID = "task_io"


def figure_cells(snapshot: dict | None = None, took: float | None = None) -> list[tuple[str, str, str]]:
    """The tiles' (label, value, note): 用时 (``took``, else the process record's start to its end; while it runs,
    开始 and the time it began), the turn's tokens in and out, the main task's output speed (``speed_cell``),
    and the context the next turn resumes with as a share of the model's
    window (its size alone when the window is not known). "—" when a figure is not known: a Harness that
    reports no context, a run without a process record."""
    snapshot = snapshot or {}
    running = took is None and snapshot.get("ended_at") is None and type(snapshot.get("started_at")) in (int, float)
    if took is None:
        took = run_seconds(None, snapshot)
    figures = usage_figures(snapshot)
    tokens = token_total(figures["shown"]) or "—"
    context = context_text(figures["context"], figures["window"]) or "—"
    if running:
        # Running: when it began. A 用时 here moved only with the steps (16秒 under 已运行 1 分 01 秒)
        # and said what the status line's 已运行 says.
        return [("开始", start_text(snapshot["started_at"]), ""),
                ("token", tokens, ""), speed_cell(snapshot), ("上下文", context, "")]
    return [("用时", "—" if took is None else took_text(took), ""), ("token", tokens, ""), speed_cell(snapshot), ("上下文", context, "")]


def speed_cell(snapshot):
    """The main task's output speed when the backend timed its generation, a tile of its own: small grey under
    the token tile it was easy to miss."""
    speed = facts_speed(snapshot)
    return ("速度", "—" if speed is None else f"{speed:.0f}", "token/s")


def figures_element(snapshot: dict | None = None, took: float | None = None, *, compact: bool = False) -> dict[str, Any]:
    """The four tiles beside the status (FIGURES_ELEMENT_ID): in a row, two by two on a narrow screen (a topic's
    side panel, about 300 px, wrapped four in a row; flex_mode bisect). Unfilled, as the rest of the card.
    ``compact`` (a slim card's 详情): one line of small print,
    "开始 14:25 · token 76万 · 速度 14 token/s · 上下文 76万", the labels grey and the figures bold."""
    cells = figure_cells(snapshot, took)
    if compact:
        line = " · ".join(f"{grey(label)} **{value}**" + (f" {grey(note)}" if note else "") for label, value, note in cells)
        return md(line, element_id=FIGURES_ELEMENT_ID, size="notation")
    return {"element_id": FIGURES_ELEMENT_ID, **tiles(*cells, filled=False, flex="bisect")}


def compact_figures(figures_text: str) -> bool:
    """Whether a card's figures as it was sent (``figures_text``, the element's JSON) are the one-line kind: a card
    keeps the kind it was sent with."""
    try:
        return json.loads(figures_text or "{}").get("tag") == "markdown"
    except (ValueError, AttributeError):
        return False


def job_inputs(job: Any) -> list[str]:
    """The 输入 parts of ``job``'s card: those app/cards.py worked out when the card was first made
    (``job.input_parts``, cards/answer_card.py input_parts, the answer card's), else from what the job says."""
    parts = getattr(job, "input_parts", None)
    if isinstance(parts, list) and parts:
        return [str(part) for part in parts]
    model = getattr(job, "model_label", "") or getattr(job, "model", "") or ""
    return answer_card.input_parts("", "", str(model).rsplit(" · ", 1)[-1], getattr(job, "effort", "") or "",
                                   getattr(job, "workspace_label", "") or "")


def io_lines(pairs=(), ran: str = "", *, slim: bool = False) -> list[str]:
    """A running turn's 输出 line from its parts ``pairs`` (cards/answer_card.py running_figures) and 已运行 ``ran``:
    开始 ｜ 已运行 ｜ token ｜ token/s ｜ 上下文; on a ``slim`` card (its 已运行 on its status line) without 已运行."""
    if slim:
        return answer_card.output_lines([part for _, part in answer_card.with_ran(pairs, "")],
                                        answer_card.SLIM_RUNNING_LINES)
    return answer_card.output_lines([part for _, part in answer_card.with_ran(pairs, ran)], answer_card.RUNNING_LINES)


def io_figures(lines) -> dict[str, Any]:
    """The 输出 text of the 输入/输出 box (FIGURES_ELEMENT_ID) with ``lines``, as the box draws it."""
    return answer_card.box(IO_BOX_ID, (), lines, FIGURES_ELEMENT_ID)["columns"][0]["elements"][0]["columns"][1][
        "elements"][0]


def io_box(job: Any, outputs=()) -> dict[str, Any]:
    """The task card's 输入/输出 box (IO_BOX_ID): ``job``'s 输入 (``job_inputs``) and the 输出 lines ``outputs``."""
    return answer_card.box(IO_BOX_ID, answer_card.input_lines(job_inputs(job)), list(outputs), FIGURES_ELEMENT_ID)
