"""A turn's figures from its process snapshot (core/trace.py), worked out in one place for the task card's tiles
(cards/task_figures.py figure_cells, speed_cell) and the answer card's 输出 line (cards/answer_card.py output_parts):
its tokens (the subtasks' with the main task's when reported, ``turn_usage``), the main task's output speed, and the
context the next turn resumes with. Raw figures only: each card keeps its own test of what it shows."""
from __future__ import annotations

import time

from covey.core.text import share
from covey.core.trace import turn_usage
from covey.core.usage import rate


def tile_count(value):
    """A count as a tile (and the 输出 box) shows it, without a space and with one decimal at most: 8,123, 9.0万, 33万, 1.4亿. A third of a topic's
    side panel wrapped short_count's "9.01 万" (the 2026-11-04 client check); 任务详情 keeps the exact count."""
    if value < 10000:
        return f"{value:,}"
    unit, size = ("亿", 10 ** 8) if value >= 99995000 else ("万", 10 ** 4)
    amount = value / size
    return f"{amount:.{1 if amount < 9.95 else 0}f}{unit}"


def speed(snapshot):
    """The main task's output tokens per second when the backend timed its generation, else None."""
    snapshot = snapshot or {}
    return rate(snapshot.get("measured_output_tokens"), snapshot.get("generation_seconds"))


def usage_figures(snapshot):
    """{shown: the token counts the cards show (the turn's total when subtasks reported, else the main task's; {}
    without a snapshot), speed, context, window (the snapshot's context_tokens and context_window), cache_read}."""
    snapshot = snapshot or {}
    shown = {}
    if snapshot:
        usage = turn_usage(snapshot)
        shown = usage["total"] if usage["count"] else usage["main"]
    return {"shown": shown, "speed": speed(snapshot), "context": snapshot.get("context_tokens"),
            "window": snapshot.get("context_window"), "cache_read": shown.get("cache_read_tokens")}


def token_total(shown, *, ints_only=False):
    """``shown``'s (usage_figures) input and output tokens summed as a tile writes them (``tile_count``), None without
    either. A count counts when it is not None (the task card's tiles); ``ints_only``: when it is an int (the 输出
    box, cards/answer_card.py). turn_usage hands both only ints, so they show the same."""
    counts = [value for value in (shown.get("input_tokens"), shown.get("output_tokens"))
              if (type(value) is int if ints_only else value is not None)]
    return tile_count(sum(counts)) if counts else None


def context_text(context, window):
    """The context the next turn resumes with as the cards show it: a share of the model's ``window`` (its size
    alone when the window is not known or smaller); None when ``context`` is not a positive int."""
    if type(context) is int and context > 0:
        return share(context, window) if type(window) is int and window >= context else tile_count(context)
    return None


def start_text(stamp):
    """When a turn began, as 开始 shows it: HH:MM."""
    return time.strftime("%H:%M", time.localtime(stamp))
