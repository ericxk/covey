"""The shared parts of the 夜航 look (docs/ui.md) as schema 2.0 card elements: the conclusion band, the 要你做 bar, the segment bar, a state's dot and pill, a topic row. Words and colours come from core/look.py;
no card here is sent by itself, the cards that move to this look use these parts (docs/ui.md 怎么开工).

- ``conclusion_band``: the visual centre of a card. A small label, the headline, a grey line, then ``below`` (a
  ``todo_bar`` or a ``seg_bar``), each a row of its own on a background a step lighter than the row above (core/look.py
  BAND: a gradient in steps too fine to see), inside one thin line. Its colours are the card's own: a card with a band
  goes through ``styled``, which puts them in its ``config.style.color``.
- ``seg_bar``: how many of each state, as one line of coloured bars as long as their counts, and a legend under it.
- ``topic_row``: a state's dot and word, ``T531 title ›``, a line in words, grey figures; buttons at the right; the
  whole opens ``url``.
"""
from __future__ import annotations

from covey.cards.elements import grey, md
from covey.core import look
from covey.core.text import literal

EDGE, GAP = "10px", "3px"  # A band row's padding at the band's top and bottom edge, and between two rows.
SIDE = "14px"
SEG_WIDTH = 24  # Characters of the segment bar: wide enough to read the parts, narrow enough for a topic's side panel.
SEG_CHAR = "━"


def font(text, color) -> str:
    return f"<font color='{color}'>{text}</font>"


def speaker(who) -> str:
    """说话人 (docs/ui.md 第 4 节 ``speaker``): who speaks, bold in the brand colour; '' for no one. What follows it
    on the line (what it does, when, ›) is grey, as the caller writes it."""
    return font("**" + literal(" ".join(str(who or "").split())) + "**", look.BRAND["color"]) if who else ""


def dot(key) -> str:
    """A state's dot, in its colour."""
    return font("●", look.STATES[key].color)


def state_word(key, *, heading=False) -> str:
    """A state's dot and its word in bold, both in its colour: how a row says its state."""
    found = look.STATES[key]
    return f"{dot(key)} {font('**' + (look.group_word(key) if heading else found.word) + '**', found.color)}"


def state_pill(key) -> str:
    """A state's word as a coloured tag (cards/board.py pill's look)."""
    found = look.STATES[key]
    return f"<text_tag color='{found.color}'>{found.word}</text_tag>"


def container(elements, *, background="", border="", radius="10px", padding="0px", spacing="2px", url="") -> dict:
    box = {"tag": "interactive_container", "width": "fill", "padding": padding, "corner_radius": radius,
           "vertical_spacing": spacing}
    if background:
        box["background_style"] = background
    if border:
        box["has_border"] = True
        box["border_color"] = border
    else:
        box["has_border"] = False
    box["elements"] = elements
    if url:
        box["behaviors"] = [{"type": "open_url", "default_url": url}]
    return box


def side_by_side(left, right, *, align="top") -> dict:
    """``left`` (elements) filling the width, ``right`` (elements) as wide as it needs, side by side."""
    columns = [{"tag": "column", "width": "weighted", "weight": 1, "vertical_align": align, "vertical_spacing": "2px",
                "elements": left}]
    if right:
        columns.append({"tag": "column", "width": "auto", "vertical_align": align, "elements": right})
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": columns}


def conclusion_band(label, headline, line="", *, below=(), url="") -> dict:
    """The conclusion band: ``label`` (small, the brand's colour), ``headline`` (Markdown, large), ``line`` (grey),
    then ``below`` (elements, one row together), each row a step lighter than the one above, inside one thin line.
    The card it is on goes through ``styled``."""
    rows = [[md(font(label, look.BRAND["color"]), size="notation")], [md(headline, size="heading")]]
    if line:
        rows.append([md(grey(line), size="notation")])
    if below:
        rows.append(list(below))
    rows = rows[:look.BAND["rows"]]
    last = len(rows) - 1
    boxes = [container(row, background=f"band-{n}", radius="0px", spacing="4px",
                       padding=f"{EDGE if n == 0 else GAP} {SIDE} {EDGE if n == last else GAP} {SIDE}")
             for n, row in enumerate(rows)]
    return container(boxes, border="band-line", radius="10px", spacing="0px", url=url)


def styled(card) -> dict:
    """``card`` (cards/elements.py card) with the colours of the band in its ``config.style.color``, beside any it has."""
    config = card.setdefault("config", {})
    style = config.setdefault("style", {})
    style["color"] = {**style.get("color", {}), **look.band_colors()}
    return card


def number(value, unit) -> str:
    """A headline figure: the number in the brand's colour, its unit after it (``2 件事``)."""
    return f"{font('**' + str(value) + '**', look.BRAND['color'])} **{unit}**"


def todo_bar(text, *, label="要你做") -> dict:
    """The one thing asked of the owner, on white inside the band: ``label`` in the waiting colour, then ``text``."""
    return container([md(f"{font('**' + label + '**', look.STATES['waiting'].color)}　{literal(text)}", size="notation")],
                     background="bg-white", radius="8px", padding="6px 8px 6px 8px")


def seg_bar(counts) -> list:
    """``counts`` ({state key: how many}, in the order given) as a bar of coloured parts and a legend line; [] when
    nothing counts. Each part is at least one character long."""
    parts = [(key, int(n)) for key, n in counts.items() if int(n or 0) > 0]
    if not parts:
        return []
    total = sum(n for _, n in parts)
    widths = [max(1, round(SEG_WIDTH * n / total)) for _, n in parts]
    widths[widths.index(max(widths))] += SEG_WIDTH - sum(widths)  # The longest part takes the rounding.
    bar = "".join(font(SEG_CHAR * width, look.STATES[key].color) for (key, _), width in zip(parts, widths))
    legend = "　".join(f"{dot(key)} {grey(f'{n} {look.STATES[key].word}')}" for key, n in parts)
    return [md(bar, size="notation"), md(legend, size="notation")]


def topic_row(key, head, line="", figures=(), *, url="", buttons=()) -> dict:
    """One topic: ``key``'s dot and word, ``head`` (``T531 title``) and › when it opens ``url``, ``line`` in words,
    ``figures`` grey; ``buttons`` (elements) at the right."""
    left = [md(f"{state_word(key)} **{literal(head)}**" + (f"　{grey('›')}" if url else ""), size="normal_v2")]
    if line:
        left.append(md(literal(line), size="normal_v2"))
    shown = " ｜ ".join(literal(part) for part in figures if part)
    if shown:
        left.append(md(grey(shown), size="notation"))
    return container([side_by_side(left, list(buttons), align="center")], padding="6px 4px 6px 4px", radius="8px",
                     url=url)


DONE_MARK = "✓"  # The mark the setting cards' notices started with before docs/ui.md 5.2.
PROBLEM_MARK = "⚠️"  # A notice of a change that did not happen, or of something to fix first.


def receipt(text) -> dict:
    """What a setting card says after a change (docs/ui.md 5.2: 保存后用一句灰字回执): one grey line in small print, the
    ✓ it used to start with left off. One that starts with ⚠️ (the change did not happen, or something comes first)
    says so in the body's size and colour."""
    text = str(text or "").strip()
    if text.startswith(PROBLEM_MARK):
        return md(text)
    if text.startswith(DONE_MARK):
        text = text[len(DONE_MARK):].lstrip()
    return md(grey(text), size="notation")
