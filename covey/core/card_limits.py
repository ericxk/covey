"""What one Feishu card may hold, measured the same way wherever it is drawn or sent: its tagged nodes, its JSON bytes and
the size CardKit renders it at (``card_cost``), and ``check``, the one test every card write passes before it goes
(feishu/messenger.py). Standard library only: cards/ (which draws the cards) and feishu/ (which sends them) both read it.

Feishu's limits, and the targets the cards are drawn to (T889, a little kept free):

- Tagged nodes: at most CARD_NODES (200, every ``plain_text`` counted) on any card, a card message or a CardKit card;
  CardKit refuses more with 300305 (NODES_OVER). Drawn to TARGET_NODES (195).
- JSON bytes: a card message (an IM ``messages patch`` or send carrying the card itself) at most CARD_BYTES (30 KB); Feishu
  refuses more (SIZE_OVER). Drawn to TARGET_BYTES (29 KB). A CardKit card has no such bound: it is refused (200860) by the
  size it renders at, not by its bytes (40,000 characters of Chinese prose, 117 KB, were taken; a task card's body is up
  to 28,000 characters), so its bytes are never refused here.
- Rendered size: a CardKit card within CARD_COST_BUDGET by ``card_cost``, an estimate that errs short (every measured
  refusal read at least 358,000): over it is only logged, as is anything over a target.

``check`` refuses only what Feishu surely refuses (WAVE12 §6.1 #9, the first week: the hard limits; the targets are logged).
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

CARD_NODES, CARD_BYTES = 200, 30_000  # Feishu's hard limits (scripts/cardkit_check.py LIMITS).
TARGET_NODES, TARGET_BYTES = 195, 29_000  # What the cards are drawn to (cards/question_cards.py, T889).
NODES_OVER, SIZE_OVER = "300305", "200860"  # Feishu's codes for a card over them (feishu/transport.py CODE_REASONS).

# CardKit refuses a card over its size (code 200860, card over max size), and the size is not the card
# JSON's bytes: each rendered line, paragraph, inline mark and table cell costs as much as 70-600 bytes
# of text, counted over the whole card (a process panel's lines leave fewer for the answer). Measured
# with cards created and patched but never sent: 2,190 lines of one number each (9.8 KB)
# were refused, 40,000 characters of Chinese prose (117 KB) were not. A body of 5,000 numbers (23.9 KB)
# was refused three times and its card stayed at 执行中. ``card_cost`` estimates that size; at every
# measured refusal (numbers, short Chinese and English lines, paragraphs, lists, bold and code marks,
# tables, headings, code blocks, prose), with the card's other elements, it read at least 358,000;
# 40,000 characters of 40-character Chinese lines were taken at 323,000, so the budget errs short.
CARD_COST_BUDGET = 300_000
LINE_COST, BREAK_COST, MARK_COST, CELL_COST, CODE_LINE_COST = 200, 40, 150, 300, 70
_MARKS = re.compile(r"\*\*|`|\]\(|<font|<at ")


def element_count(value) -> int:
    """The CardKit nodes in ``value``: every dict with a tag, at any depth."""
    if isinstance(value, dict):
        return int("tag" in value) + sum(element_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(element_count(v) for v in value)
    return 0


def json_size(value) -> int:
    """The UTF-8 bytes of ``value``'s JSON as the cards are written (``ensure_ascii=False``)."""
    return len(json.dumps(value, ensure_ascii=False).encode())


def sent_size(built) -> int:
    """The bytes of ``built`` as a message request carries it: its JSON, escaped again as a string."""
    return len(json.dumps(json.dumps(built, ensure_ascii=False), ensure_ascii=False).encode("utf-8"))


def fits(elements, nodes, size) -> bool:
    """Whether ``elements`` keeps within ``nodes`` CardKit nodes and ``size`` bytes of JSON."""
    return element_count(elements) <= nodes and json_size(elements) <= size


def line_cost(line: str, fenced: bool) -> int:
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
            cost += line_cost(line, fenced)
    return cost


def card_cost(element: Any) -> int:
    """``text_cost`` over a card or element: its JSON, and the nodes of every text in it."""
    def nodes(value):
        if isinstance(value, dict):
            return sum(text_cost(child) - len(child.encode("utf-8")) if key == "content" and isinstance(child, str)
                       else nodes(child) for key, child in value.items())
        return sum(map(nodes, value)) if isinstance(value, list) else 0
    return len(json.dumps(element, ensure_ascii=False).encode("utf-8")) + nodes(element)


@dataclasses.dataclass(frozen=True)
class Over:
    """A card ``check`` refuses: Feishu's ``code`` for it (NODES_OVER, SIZE_OVER), what was measured (``nodes``, ``bytes``)
    and the ``limit`` it passed."""
    code: str
    nodes: int
    bytes: int
    limit: int

    def words(self) -> str:
        """A short English note for the log and the failure's detail (never the card's content)."""
        what = f"{self.nodes} nodes" if self.code == NODES_OVER else f"{self.bytes} bytes"
        return f"checked before sending: {what} over {self.limit}"


def check(card, *, message=False, nodes=CARD_NODES, size=CARD_BYTES) -> Over | None:
    """Whether ``card`` (a whole card, or the part one CardKit write carries) is over what Feishu takes: more than ``nodes``
    tagged nodes, or for a card message (``message``) more than ``size`` bytes of JSON. None when it is within them. A
    test may pass the targets (TARGET_NODES, TARGET_BYTES) as the limits. Never raises."""
    try:
        count = element_count(card)
        if count > nodes:
            return Over(NODES_OVER, count, 0, nodes)
        if message:
            used = json_size(card)
            if used > size:
                return Over(SIZE_OVER, count, used, size)
    except (TypeError, ValueError, RecursionError):
        return None
    return None


def over_target(card, *, message=False, cost=True) -> str:
    """What of ``card`` is over the drawn targets though within the hard limits, for a log line ('' none): its nodes over
    TARGET_NODES; a card message's bytes over TARGET_BYTES, a whole CardKit card's rendered size over CARD_COST_BUDGET
    (``cost`` False: the part one write carries, whose text the task card already cuts to fit). Never raises."""
    try:
        count, parts = element_count(card), []
        if count > TARGET_NODES:
            parts.append(f"nodes={count}")
        if message:
            used = json_size(card)
            if used > TARGET_BYTES:
                parts.append(f"bytes={used}")
        elif cost:
            rendered = card_cost(card)
            if rendered > CARD_COST_BUDGET:
                parts.append(f"cost={rendered}")
        return " ".join(parts)
    except (TypeError, ValueError, RecursionError):
        return ""
