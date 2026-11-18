"""The shared parts of the 夜航 look (docs/ui.md) as schema 2.0 card elements: the conclusion band with its halo and
orb, the 要你做 bar, the segment bar, a state's dot and pill, a topic row. Words and colours come from core/look.py;
no card here is sent by itself, the cards that move to this look use these parts (docs/ui.md 怎么开工).

- ``conclusion_band``: the visual centre of a card. A small label, the headline, a grey line, then ``below`` (a
  ``todo_bar`` or a ``seg_bar``), on core/look.py BRAND's band colour, inside two rings of its halo colours (the halo:
  Feishu has no shadow, so rings of nested containers stand for it). ``orb_key``: the image key of the orb
  (feishu/orb_images.py), shown at the right of the label and headline; none: no orb.
- ``seg_bar``: how many of each state, as one line of coloured bars as long as their counts, and a legend under it.
- ``topic_row``: a state's dot and word, ``T531 title ›``, a line in words, grey figures; buttons at the right; the
  whole opens ``url``.
"""
from __future__ import annotations

from covey.cards.elements import grey, md
from covey.core import look
from covey.core.text import literal

BAND_PADDING = "10px 12px 10px 12px"
SEG_WIDTH = 24  # Characters of the segment bar: wide enough to read the parts, narrow enough for a topic's side panel.
SEG_CHAR = "━"
ORB_SIZE = "40px 40px"


def font(text, color) -> str:
    return f"<font color='{color}'>{text}</font>"


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


def orb(image_key) -> dict:
    return {"tag": "img", "img_key": image_key, "size": ORB_SIZE, "scale_type": "fit_horizontal", "transparent": True,
            "preview": False, "alt": {"tag": "plain_text", "content": ""}}


def conclusion_band(label, headline, line="", *, below=(), orb_key="", url="") -> dict:
    """The conclusion band: ``label`` (small, the brand's colour), ``headline`` (Markdown, large), ``line`` (grey),
    then ``below``; the orb at the right of the first two when ``orb_key``; inside the halo's two rings."""
    top = [md(font(label, look.BRAND["color"]), size="notation"), md(headline, size="heading")]
    head = side_by_side(top, [orb(orb_key)]) if orb_key else None
    elements = [head] if head else top
    if line:
        elements.append(md(grey(line), size="notation"))
    elements += list(below)
    outer, inner = look.BRAND["halo"]
    band = container(elements, background=look.BRAND["band"], padding=BAND_PADDING, spacing="4px", url=url)
    ring = container([band], background=inner, radius="12px", padding="2px")
    return container([ring], background=outer, radius="14px", padding="3px")


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
