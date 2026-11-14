"""What a card costs CardKit, which refuses one over its size (code 200860): ``card_cost`` and ``text_cost`` estimate
it, and a task card's body is cut to fit beside the card's other elements (truncate_card_content)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from covey.cards.streaming import CardRuntime


# CardKit refuses a card over its size (code 200860, card over max size), and the size is not the card
# JSON's bytes: each rendered line, paragraph, inline mark and table cell costs as much as 70-600 bytes
# of text, counted over the whole card (a process panel's lines leave fewer for the answer). Measured
# 2026-10-28 with cards created and patched but never sent: 2,190 lines of one number each (9.8 KB)
# were refused, 40,000 characters of Chinese prose (117 KB) were not. #1150 (5,000 numbers, 23.9 KB)
# was refused three times and its card stayed at 执行中. ``card_cost`` estimates that size; at every
# measured refusal (numbers, short Chinese and English lines, paragraphs, lists, bold and code marks,
# tables, headings, code blocks, prose), with the card's other elements, it read at least 358,000;
# 40,000 characters of 40-character Chinese lines were taken at 323,000, so the budget errs short.
CARD_COST_BUDGET = 300_000
LINE_COST, BREAK_COST, MARK_COST, CELL_COST, CODE_LINE_COST = 200, 40, 150, 300, 70
CARD_TOO_BIG = "200860"
_MARKS = re.compile(r"\*\*|`|\]\(|<font|<at ")


def _line_cost(line: str, fenced: bool) -> int:
    """What one line adds to the card's size beyond its bytes."""
    stripped = line.strip()
    if not stripped:
        return BREAK_COST
    if fenced:
        return CODE_LINE_COST
    marks = _MARKS.findall(stripped)
    cost = LINE_COST + MARK_COST * ((marks.count("**") + marks.count("`")) // 2
                                    + sum(mark not in ("**", "`") for mark in marks))
    return cost + (CELL_COST * max(0, stripped.count("|") - 1) if stripped.startswith("|") else 0)


def text_cost(text: str) -> int:
    """How much of CardKit's card size ``text`` takes as markdown: its UTF-8 bytes and its nodes."""
    cost, fenced = len(text.encode("utf-8")), False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            cost += LINE_COST
        else:
            cost += _line_cost(line, fenced)
    return cost


def card_cost(element: Any) -> int:
    """``text_cost`` over a card or element: its JSON, and the nodes of every text in it."""
    def nodes(value):
        if isinstance(value, dict):
            return sum(text_cost(child) - len(child.encode("utf-8")) if key == "content" and isinstance(child, str)
                       else nodes(child) for key, child in value.items())
        return sum(map(nodes, value)) if isinstance(value, list) else 0
    return len(json.dumps(element, ensure_ascii=False).encode("utf-8")) + nodes(element)


def _cut_to_cost(text: str, room: int) -> str:
    """The longest head of ``text`` whose ``text_cost`` fits ``room``, cut after a line (a single line
    longer than that is cut inside it), with a code fence it leaves open closed again."""
    kept, used, fenced = [], 0, False
    for line in text.split("\n"):
        fence = line.lstrip().startswith("```")
        cost = len(line.encode("utf-8")) + 1 + (LINE_COST if fence else _line_cost(line, fenced))
        if used + cost > room:
            if not kept:  # One line over the room: its head, by bytes.
                kept.append(line.encode("utf-8")[:max(0, room - LINE_COST)].decode("utf-8", "ignore"))
            break
        kept.append(line)
        used += cost
        fenced = fenced != fence
    head = "\n".join(kept).rstrip()
    return head + "\n```" if fenced and head else head


def _close_fence(head: str) -> str:
    """``head`` with a code fence it leaves open closed again, its fences found as ``_cut_to_cost`` finds them."""
    fenced = sum(line.lstrip().startswith("```") for line in head.split("\n")) % 2
    return head + "\n```" if fenced and head else head


def truncate_card_content(text: str, runtime: CardRuntime, reserved: int = 0) -> str:
    """``text`` as the task card's body can show it: at most ``card_limit`` characters, and only what the
    card's size leaves once its other elements (``reserved``, by ``card_cost``) are counted. A cut body
    ends at a line (a single line longer than that is cut inside it), a code block it cut closed, with
    ``overflow_suffix`` saying so after it."""
    limit, room = runtime.card_limit, CARD_COST_BUDGET - reserved
    if len(text) <= limit and text_cost(text) <= room:
        return text
    suffix = getattr(runtime, "overflow_suffix", "")
    head = text[: max(0, limit - len(suffix))]
    if len(head) < len(text):  # Cut by characters: at a line, leaving room for the fence that closes a block.
        size = max(0, limit - len(suffix) - len("\n```"))
        end = text.rfind("\n", 0, size + 1)
        line_end = text.find("\n", end + 1)
        if (line_end if line_end >= 0 else len(text)) - end - 1 > size:  # One line over the limit: its head.
            end = size
        head = _close_fence(text[:end].rstrip())
    if text_cost(head) > room - text_cost(suffix):
        head = _cut_to_cost(head, room - text_cost(suffix))
    return head + suffix


def exceeds_stream_card_limit(text: str, runtime: CardRuntime) -> bool:
    """Whether the card cuts ``text`` by characters (``card_limit``). A cut by size depends on the rest
    of the card; the write that made it says so (StreamingReply.cut)."""
    return len(text) > runtime.card_limit
