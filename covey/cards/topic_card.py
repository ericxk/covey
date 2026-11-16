"""The topic card (话题卡): the root message of a topic the butler opens (app/butler_topics.py, docs/butler.md).
Sent at the top of the owner's private chat, then replaced whole once with the topic's number. Compact, no
buttons, in the menu panels' style: the title bar names the topic ("T410 · 标题"), one small grey line its
configuration. Nothing else since 2026-11-06 (the owner): the butler's reason and "进展看话题里的任务卡" said
what the butler's card and the topic itself say."""
from __future__ import annotations

from covey.core.text import literal
from covey.cards.elements import note, panel_header, styled_panel


def heading(label, title):
    """"T412 · 标题"; only the title before the topic has its number."""
    return f"{label} · {title}" if label else title


def topic_card(title, *, label="", why="", config=()):
    """``label`` the topic's number (T412, '' before it has one), ``config`` the parts of the configuration line
    (Harness, model, budget, workspace, run mode), shown literally: no Markdown or Feishu tag of theirs takes
    effect. ``why``, the butler's one line on its choice, is no longer shown (its record keeps it)."""
    head = heading(label, title)
    line = " · ".join(literal(part) for part in config if part)
    return styled_panel(panel_header("launch", head), [note(line)] if line else [], head)
