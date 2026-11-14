"""The web dashboard's link card (app/dashboard.py). It goes out when the dashboard's address moves, and for 换口令 on an
older link card: what happened, 打开看板 (the link, which carries the token) and 换口令 (ROTATE_KIND: a new link under a
new token), then the notes in small print. The 设置 card's 脱敏 tab has the link in its last part
(cards/redaction_panel.py), where 换口令's value carries that card's token."""
from __future__ import annotations

from covey.cards.elements import (callback_button, folded_panel, md, menu_item, note, panel_header, plain, row,
                                           styled_panel)

ROTATE_KIND = "bridge_dashboard_rotate"  # 换口令 on the link's card (app/card_actions.py ACTIONS).
TITLE = "脱敏看板"


SUBTITLE = "敏感词的命中统计、话题开关和词表"


def link_card(lead, link, notes, *, listening=True):
    """``lead`` may be empty; the title bar says whether the dashboard listens."""
    head = panel_header("redaction", TITLE, SUBTITLE,
                        [("看板在线", "green")] if listening else [("看板没有在监听", "red")])
    open_page = {"tag": "button", "text": plain("打开看板"), "type": "primary_filled",
                 "behaviors": [{"type": "open_url", "default_url": link}]}
    rotate = callback_button("换口令", {"kind": ROTATE_KIND})
    elements = [*([md(lead)] if lead else []), row((open_page, None), (rotate, None), flex=None),
                note("\n".join(notes))]
    return styled_panel(head, elements, TITLE)


def folded_card():
    """The link's card folded in place (app/dashboard.py, a second menu click): the link is off the card."""
    return folded_panel("redaction", TITLE, menu_item("脱敏"))
