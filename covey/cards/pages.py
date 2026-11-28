"""A card's text too large for one card message, over as many cards as it takes, each in the look of the card it
stands for (docs/ui.md 5.2 聊天结果卡: a topic's answer card, its relay at the private chat's top level, the output above
a question's copy there): ``paged`` cuts the text (``split``) and draws each part as a card of its own (``drawer``).

Each card opens with the speaker's row as the one card would (cards/ui.py speaker_row: the topic's T number as its
badge, its title, its state, the way into the topic), which of them it is grey in it ("1/3", ``page``); the owner's
words that open (cards/answer_card.py words_fold) under the first one's row; what follows the text (产物, 改动,
输入/输出…) under the last one, else, when the last cannot hold it too, on a card of its own after them
(``Paged.alone``: the caller sends that one, cards/answer_card.py tail_card). A card with no row says which it is on a
grey line first ("分 3 张发 · 1/3").

``fits``: a card one message can carry: at most ANSWER_CARD_BYTES of JSON and at most SENT_BYTES once that JSON is
escaped again, as the request carries it (Feishu takes 30 KB); a CardKit card (``cardkit``) also within what CardKit
renders (cards/card_cost.py). ``split`` cuts after a paragraph, else after a line, else in a line after a sentence's
end, else where the room ends, never inside inline code, a link or a tag; a code block cut in two is closed on one card
and opened again on the next, a table goes on under its header row again. ``answer_pages``: cards/review.py
answer_card's card cut so."""
from __future__ import annotations

import copy
import dataclasses
import json
import re

from covey.cards.elements import card, json_size, md, note
from covey.cards import review
from covey.cards.review import ANSWER_CARD_BYTES, one_card
from covey.cards.ui import BODY_PADDING, BODY_SPACING, banded, labelled, role_of, speaker_row, split_ask
from covey.core import card_limits

# Drawn to in cards/review.py one_card (an answer card falls back to one text by it); read here by name too.
SENT_BYTES, PAGE_NODES = review.SENT_BYTES, review.PAGE_NODES

MARGIN = 600  # Kept free on each card for what ``finish`` adds (its other parts' links) and a longer page mark.
PARAGRAPH = 0.6  # A card is cut after its last paragraph when that leaves it at least this full, else after a line.
SENTENCE = 0.6  # A line is cut after its last sentence end when that leaves the room at least this full.
MIN_ROOM = 2000  # Bytes of text a card takes whatever else it carries, so a text always ends.
TRIES, SHRINK = 4, 0.85  # Cards that do not fit once finished: cut again, each card's room this much smaller.
MANY = 99  # The page mark measured before the count is known: "1/99" is as long as most.


@dataclasses.dataclass(frozen=True)
class Page:
    card: dict
    text: str  # Its part of the text: the Markdown that goes when Feishu refuses the card.


@dataclasses.dataclass(frozen=True)
class Paged:
    pages: tuple
    alone: bool = False  # What follows the text did not fit under the last card: the caller sends it on its own.
    after: tuple = ()  # When ``alone``: what goes atop the caller's card of it (a relay's 这一轮中途的汇报, relay_pages).


sent_size = card_limits.sent_size  # The bytes of a card as a message request carries it (core/card_limits.py).


def fits(built, *, cardkit=False) -> bool:
    """Whether one card message can carry ``built``: ANSWER_CARD_BYTES of JSON and SENT_BYTES as the request carries
    it, PAGE_NODES tagged nodes; ``cardkit``: a card CardKit creates (a question's copy), within its rendered size too
    (cards/card_cost.py)."""
    if not one_card(built):
        return False
    if cardkit:
        from covey.cards.card_cost import CARD_COST_BUDGET, card_cost

        return card_cost(built) <= CARD_COST_BUDGET
    return True


def _cost(text):
    """(bytes once escaped, bytes twice escaped) ``text`` takes inside a card's JSON string."""
    once = json.dumps(text, ensure_ascii=False)[1:-1]
    return len(once.encode("utf-8")), len(json.dumps(once, ensure_ascii=False).encode("utf-8")) - 2


def _plus(a, b):
    return a[0] + b[0], a[1] + b[1]


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_ROW = re.compile(r" {0,3}\|")
_RULE = re.compile(r" {0,3}\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*")
# What a cut in a line never goes inside: inline code, a link or an image, a tag pair, a tag, bold.
_ATOM = re.compile(r"`[^`\n]*`|!?\[[^\]\n]*\]\([^)\n]*\)|<(\w+)\b[^>\n]*>.*?</\1>|<[^>\n]+>|\*\*[^*\n](?:.*?[^*\n])?\*\*")
_ENDS = "。！？!?；;"
_HEADING = re.compile(r" {0,3}(?:#{1,6}\s|\*\*[^*].*\*\*[:：]?\s*$)")  # A heading, or a line of bold alone.


def _contexts(lines):
    """For each line, (the lines a card that starts at it opens with, the line a card that ends just before it closes
    with): inside a code block its opening fence, and that fence's close; in a table's rows its header and rule, and
    nothing."""
    out, fence, header = [], None, None
    for at, line in enumerate(lines):
        if fence:
            out.append(([fence[0]], fence[1]))
        elif header and _ROW.match(line):
            out.append((list(header), ""))
        else:
            out.append(([], ""))
        found = _FENCE.fullmatch(line)
        if fence:
            mark = found.group(1) if found else ""
            if mark and mark[0] == fence[1][0] and len(mark) >= len(fence[1]) and not found.group(2).strip():
                fence = None
        elif found:
            fence, header = (line, found.group(1)), None
        elif header and _ROW.match(line):
            pass
        elif "|" in line and _RULE.fullmatch(line) and at and _ROW.match(lines[at - 1]):
            header = (lines[at - 1], line)
        else:
            header = None
    return out


def _cut(line, room):
    """(head, rest) of ``line`` cut where its escaped bytes reach ``room`` (a pair, as ``_cost``): after its last
    sentence end when that leaves the room SENTENCE full, else where the room ends; never inside what _ATOM matches
    unless that starts the line. ('', ``line``) when not one character fits."""
    used, at, end = (0, 0), 0, 0
    for index, char in enumerate(line):
        cost = _cost(char)
        if used[0] + cost[0] > room[0] or used[1] + cost[1] > room[1]:
            break
        used, at = _plus(used, cost), index + 1
        if char in _ENDS:
            end = at
    else:
        return line, ""
    cut = end if end and end >= at * SENTENCE else at
    for found in _ATOM.finditer(line):
        if found.start() < cut < found.end():
            cut = found.start() or cut
            break
    return line[:cut], line[cut:]


def split(text, room) -> list:
    """``text`` (card Markdown) cut into parts, the n-th (from 1) at most ``room(n)`` of a card's JSON string (a pair:
    bytes once escaped, twice): after a paragraph that leaves a part PARAGRAPH full, else after a line, else inside the
    line (``_cut``); a heading that would end a part starts the next. A part that ends inside a code block closes it,
    the next opens it again; one that starts inside a table's rows has its header and rule first. [``text``] when it
    fits whole."""
    lines = str(text or "").split("\n")
    context = _contexts(lines)
    costs = [_cost(line + "\n") for line in lines]
    parts, at, rest = [], 0, None
    while at < len(lines):
        size = room(len(parts) + 1)
        size = (max(size[0], MIN_ROOM), max(size[1], MIN_ROOM))
        opening = context[at][0]
        while not opening and rest is None and at < len(lines) - 1 and not lines[at].strip():
            at += 1  # A part never starts with blank lines outside a code block.
            opening = context[at][0]
        page, used, mark = list(opening), (0, 0), None
        for line in page:
            used = _plus(used, _cost(line + "\n"))
        while at < len(lines):
            line = lines[at] if rest is None else rest
            cost = costs[at] if rest is None else _cost(line + "\n")
            close = context[at + 1][1] if at + 1 < len(lines) else ""
            need = _plus(_plus(used, cost), _cost(close + "\n") if close else (0, 0))
            if need[0] > size[0] or need[1] > size[1]:
                break
            page.append(line)
            used, rest, at = _plus(used, cost), None, at + 1
            if not line.strip() and not context[at - 1][1]:
                mark = (at, len(page), used)  # After a blank line outside a code block: a paragraph ends.
        if at < len(lines):
            if len(page) == len(opening):  # Not one whole line fits: the line itself is cut.
                line = lines[at] if rest is None else rest
                close = context[at][1]
                left = _plus(used, _plus(_cost("\n"), _cost(close + "\n") if close else (0, 0)))
                head, tail = _cut(line, (size[0] - left[0], size[1] - left[1]))
                if not head:  # Not even one character fits: one all the same, so the text ends.
                    head, tail = line[:1], line[1:]
                page.append(head)
                rest = tail or None
                at += 0 if rest is not None else 1
            elif mark and mark[1] > len(opening) and mark[2][0] >= PARAGRAPH * size[0]:
                at, page = mark[0], page[:mark[1]]
            if rest is None:  # A heading never ends a part: it goes on with what it heads.
                keep = len(page)
                while keep > len(opening) and not page[keep - 1].strip():
                    keep -= 1
                heading = at - (len(page) - keep) - 1  # Its index among the lines.
                if keep > len(opening) + 1 and _HEADING.match(page[keep - 1]) and not context[heading][1]:
                    at, page = heading, page[:keep - 1]
        closing = context[at][1] if at < len(lines) else ""
        while len(page) > len(opening) and not page[-1].strip() and not closing:
            page.pop()  # Nor ends with them.
        if closing:
            page.append(closing)
        if len(page) > len(opening) or not parts:
            parts.append("\n".join(page))
    return parts or [""]


def paged(text, draw, *, finish=None, reserve=False) -> Paged:
    """``text`` (a card's Markdown) over as many cards as it takes: ``draw(part, n, total, last)`` draws the n-th of
    ``total`` with ``part`` its text and, when ``last``, what follows the text under it. Each card goes through
    ``finish`` (card -> card: its other parts' T numbers as links) before it is measured (``fits``). ``reserve``: every
    card keeps room for what follows, so the last always takes it (a one-line tail); else the last takes it when it
    fits there, or ``alone`` says the caller sends it on a card of its own. Cards that do not fit once finished are cut
    again smaller (TRIES); one that never fits (an element of its own larger than a card) goes as drawn, its text then
    as Markdown (feishu/delivery.py)."""
    finish = finish or (lambda built: built)

    def over(n, last):
        built = draw(" ", n, MANY, last)
        return json_size(built), sent_size(built)
    first, other = over(1, reserve), over(2, reserve)
    result = Paged(())
    for attempt in range(TRIES):
        scale = SHRINK ** attempt

        def room(n, scale=scale):
            used = first if n == 1 else other
            return (int((ANSWER_CARD_BYTES - used[0] - MARGIN) * scale), int((SENT_BYTES - used[1] - MARGIN) * scale))
        parts = split(text, room)
        total = len(parts)
        cards = [finish(draw(part, n, total, reserve and n == total)) for n, part in enumerate(parts, 1)]
        alone = not reserve
        if alone:
            last = finish(draw(parts[-1], total, total, True))
            if fits(last):
                cards[-1], alone = last, False
        result = Paged(tuple(Page(built, part) for built, part in zip(cards, parts)), alone)
        if all(fits(built) for built in cards):
            return result
    return result


def tail_apart(result, draw, text="") -> Paged:
    """``result`` (``paged`` without ``reserve``) with what follows the text on a card of its own after the others when
    the last could not hold it (``alone``), numbered with them: ``draw("", n, n, True)``; ``text``, that card's Markdown
    when Feishu refuses it."""
    if not result.alone:
        return result
    total = len(result.pages) + 1
    pages = [Page(draw(page.text, n, total, False), page.text) for n, page in enumerate(result.pages, 1)]
    return Paged((*pages, Page(draw("", total, total, True), text)))


def paged_after(text, draw, more, draw_more, *, reserve=False) -> Paged:
    """``text`` over cards as ``paged`` cuts it with ``draw``, then ``more`` over cards of its own as ``draw_more`` draws
    them (a relay's 这一轮中途的汇报, each card's part of them in a fold: cards/relay_card.py), all numbered together; what
    follows goes under the last of ``more``'s (``reserve`` as ``paged`` takes it; ``alone`` when it did not fit there)."""
    first, rest = paged(text, draw), paged(more, draw_more, reserve=reserve)
    total = len(first.pages) + len(rest.pages)
    cards = [Page(draw(page.text, n, total, False), page.text) for n, page in enumerate(first.pages, 1)]
    for k, page in enumerate(rest.pages, 1):
        last = k == len(rest.pages) and not rest.alone
        cards.append(Page(draw_more(page.text, len(first.pages) + k, total, last), page.text))
    return Paged(tuple(cards), rest.alone)


def drawer(summary, *, row=None, words=(), label="", element_id=None, tail=(), update_multi=False, part_element=None):
    """``draw`` for ``paged``: each part in a speaker's card's look (docs/ui.md 5.3): ``row(page)`` (the speaker's row
    with ``page``, "2/3", in it; None: a card with no row, which says which it is on a grey line first), ``words``
    under the first card's row (the owner's words that open, its head's other blocks), the part (``label`` grey
    before it on the first, as cards/review.py reply_shown; ``element_id`` its ID on each; ``part_element`` (text ->
    element): drawn so instead of as Markdown), ``tail`` under the last; ``summary``, the chat list's preview, with the
    page after it."""
    def draw(part, n, total, last):
        page = f"{n}/{total}" if total > 1 else ""
        head = [row(page)] if row else ([note(f"分 {total} 张发 · {page}")] if page else [])
        text = labelled(part, label) if n == 1 and label else part
        body = ([part_element(text)] if part_element else [md(text, element_id=element_id)]) if part else []
        elements = copy.deepcopy([*head, *(words if n == 1 else ()), *body, *(tail if last else ())])
        look = dict(padding=BODY_PADDING, spacing=BODY_SPACING) if row else {}
        return banded(card(elements, f"{summary}（{page}）" if page else summary, update_multi=update_multi, **look))
    return draw


def finished(finish, words, tail):
    """(``words``, ``tail``) once through ``finish`` (card -> card: their T numbers as links), together on one card that
    is never sent, so the links are made once for all the cards, not again for each (``paged`` measures the cards
    as drawn); as they are without ``finish``."""
    if finish is None:
        return list(words), list(tail)
    parts = copy.deepcopy([*words, *tail])
    done = (finish(card(parts)) or {}).get("body", {}).get("elements") or parts
    return done[:len(words)], done[len(words):]


def answer_pages(text, summary, tail=(), head=(), *, link=None, finish=None) -> Paged:
    """cards/review.py answer_card's card over as many cards as it takes, for an answer too large for one: the same row
    on each (its "你问" block's, or the topic's in its own thread, cards/answer_card.py topic_head), the owner's words
    and the head's other blocks under the first, the answer cut where it fits, ``tail`` under the last, else on a card
    of its own (``alone``). ``link`` (text -> text): the answer's T numbers as links, once, before it is cut;
    ``finish`` (card -> card) the same for the other parts, once (``finished``)."""
    from covey.cards.answer_card import words_fold
    from covey.cards.review import ANSWER_ID, drop_part_heading, reply_body

    meta, rest = split_ask(head)
    from covey.core.rich_md import card_markdown

    body = card_markdown(drop_part_heading(reply_body(text)))  # Pages are markdown: 富文本块 in words.
    body = link(body) if link else body
    quoted = not meta or role_of(meta.get("who")) == "butler"  # The butler's row quotes his words itself.
    words, tail = finished(finish, [*([] if quoted else [part for part in (words_fold(meta),) if part]), *rest], tail)
    row = (lambda page: speaker_row(meta, page=page)) if meta else None
    return paged(body, drawer(summary, row=row, words=words, element_id=ANSWER_ID, tail=tail, update_multi=bool(tail)))
