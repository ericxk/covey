"""The topic's status card (话题状态卡, app/status_cards.py): what the pinned board pins for a topic with a thread, so
the Pin panel, which shows a card's top alone and lets nothing on it be clicked, says what the topic is, where it is
and what came of it. Three lines and no title bar (sample A): the topic's
number and title in bold, its state as a coloured tag with when and which round, then one small line (this round's
request, the question, the conclusion or why it stopped). No buttons: clicking the pin opens the topic's thread."""
from __future__ import annotations

from covey.core.text import literal
from covey.cards.elements import card, md

STATE_COLORS = {"在做": "blue", "排队": "neutral", "等你回答": "orange", "做完": "green", "失败": "red", "中断": "red",
                "已停止": "neutral"}


def status_tag(state):
    """The state as a tag of its colour."""
    return f"<text_tag color='{STATE_COLORS.get(state, 'neutral')}'>{state}</text_tag>"


def status_card(title, state, facts, line, joined=None):
    """``title`` "T32 · 调研…"; ``state`` a STATE_COLORS key; ``facts`` the rest of its line ("13:01 · 2 轮 · 用时 5 分
    49 秒"); ``line`` the small last line, '' for none. Plain text in each: no Markdown of theirs takes effect.
    ``joined``: [project name, topic id, project id] of a topic 整理专项 put into a project: 已归进「X」 · 移出 last
    (cards/project_proposal.py joined_element; clickable in the thread, not in the Pin panel)."""
    elements = [md(f"**{_shown(title)}**"), md(" ".join(part for part in (status_tag(state), _shown(facts)) if part))]
    if line:
        elements.append(md(_shown(line), size="notation"))
    if joined:
        from covey.cards.project_proposal import joined_element
        elements.append(joined_element(*joined))
    return card(elements, f"{_one_line(title)} · {state}", default_width=True, update_multi=True,
                padding="8px 12px 8px 12px", spacing="4px")


def _one_line(text):
    return " ".join(str(text or "").split())


def _shown(text):
    """``text`` on one line, shown as it is: no Markdown or Feishu tag of its takes effect."""
    return literal(_one_line(text))
