"""A reply's 富文本块 as card elements (core/rich_md.py, docs/ui.md 富文本块): its Markdown runs as markdown elements
(task marks as ✓ and ○), a callout a grey block with its kind's mark and word over its body, a details a closed fold
under its summary, metrics a row of tiles (stacked on a narrow screen). The first element carries the reply's element
ID. A reply whose elements would take more than NODE_SHARE of a card's nodes is one markdown element
(``rich_md.card_markdown``), as on the cards that hold one text (the task card, the pages of a long answer)."""
from __future__ import annotations

from covey.cards.elements import collapsible, element_count, grey, md
from covey.core import rich_md
from covey.core.rich_md import CALLOUT, DETAILS, METRICS

NODE_SHARE = 120  # Of a card's 195 (core/card_limits.py TARGET_NODES): the rest is the card's row, 产物 and buttons.
TILES_ROW = 4  # Metrics abreast at most; five or six go three a row.
BLOCK_LOOK = {"background_style": "grey-50", "padding": "8px 12px 8px 12px", "vertical_spacing": "4px"}
# The folds of a reply's details: <reply's element ID, cut>_d<n>, within CardKit's 20 characters; app/relay_send.py
# links the T numbers in them as in the reply.
FOLD_MARK = "_d"


def fold_id(element_id, n) -> str:
    suffix = f"{FOLD_MARK}{n}"
    return (element_id or "rich")[: 20 - len(suffix)] + suffix


def callout(block, polish=str) -> dict:
    """A callout on a grey block (never a red or orange one: docs/ui.md 2.2): its mark and kind in bold with its title,
    its body under it."""
    lines = [md(rich_md.callout_head(block))]
    if block.body.strip():  # ``polish``: its small headings bold, as a reply's (a quote's lines missed reply_body).
        lines.append(md(polish(rich_md.card_markdown(block.body))))
    column = {"tag": "column", "width": "weighted", "weight": 1, **BLOCK_LOOK, "elements": lines}
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "0px", "columns": [column]}


def metrics(block) -> list:
    """Metrics as rows of tiles: the label small and grey, the value large and bold, the change under it in grey (a
    rise is not always good: docs/ui.md 1 颜色只说状态). ``stretch`` stacks them on a narrow screen (``bisect`` broke
    words in a topic's side panel: docs/cards.md 设置卡配置页的格子版式)."""
    items = list(block.items)
    per = TILES_ROW if len(items) <= TILES_ROW or len(items) > 6 else 3
    rows = []
    for start in range(0, len(items), per):
        columns = []
        for label, value, delta in items[start:start + per]:
            lines = [md(grey(label), size="notation"), md(f"**{value}**", size="heading")]
            if delta:
                lines.append(md(grey(delta), size="notation"))
            columns.append({"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50",
                            "padding": "6px 10px 6px 10px", "vertical_spacing": "0px", "elements": lines})
        rows.append({"tag": "column_set", "flex_mode": "stretch", "horizontal_spacing": "6px", "columns": columns})
    return rows


def elements(text, element_id="", polish=str) -> list:
    """``text``'s elements (see the module); [] for an empty text."""
    out, folds = [], 0
    for block in rich_md.blocks(text):
        if block.kind == CALLOUT:
            out.append(callout(block, polish))
        elif block.kind == DETAILS:
            inner = elements(block.body, polish=polish) or [md(" ")]
            out.append(collapsible(fold_id(element_id, folds), rich_md.plain(block.title) or rich_md.DETAILS_WORD,
                                   inner, expanded=block.open))
            folds += 1
        elif block.kind == METRICS:
            out.extend(metrics(block))
        else:
            out.append(md(rich_md.task_marks(block.source)))
    return out


def reply_elements(text, element_id="", label="", polish=str) -> list:
    """The elements of a reply's ``text`` (as a card shows it: cards/review.py reply_body), ``label`` grey before it,
    the first element carrying ``element_id``: one markdown element when ``text`` holds no block or its blocks would
    take too many of the card's nodes."""
    from covey.cards.ui import labelled

    if rich_md.rich(text):
        drawn = elements(text, element_id, polish)
        if label:
            drawn.insert(0, md(grey(label)))
        if drawn and element_count(drawn) <= NODE_SHARE:
            if element_id and "element_id" not in drawn[0]:
                first = dict(drawn[0])
                drawn[0] = {"tag": first.pop("tag"), "element_id": element_id, **first}
            return drawn
    return [md(labelled(rich_md.card_markdown(text), label, text), element_id=element_id or None)]
