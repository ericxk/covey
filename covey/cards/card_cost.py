"""What a card costs CardKit, which refuses one over its size (code 200860): ``card_cost`` and ``text_cost`` estimate
it, and a task card's body is cut to fit beside the card's other elements (truncate_card_content)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from covey.core import card_limits

# The measure itself is core/card_limits.py's (the sending side checks cards with it too); its names stay here as before.
CARD_COST_BUDGET = card_limits.CARD_COST_BUDGET
LINE_COST, BREAK_COST, MARK_COST, CELL_COST, CODE_LINE_COST = (card_limits.LINE_COST, card_limits.BREAK_COST,
                                                              card_limits.MARK_COST, card_limits.CELL_COST,
                                                              card_limits.CODE_LINE_COST)
CARD_TOO_BIG = card_limits.SIZE_OVER
_line_cost, text_cost, card_cost = card_limits.line_cost, card_limits.text_cost, card_limits.card_cost

if TYPE_CHECKING:
    from covey.cards.streaming import CardRuntime


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
