"""The words and rows 整理专项 draws a topic with: its T number, title and link (the link reads Feishu), a proposal card's rows of
topics, the 整理专项 card's rows (docs/projects.md 整理专项). Takes no record lock, writes nothing; ``link`` is the one thing
that asks Feishu, so the tests stand in for it here."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import project_sorter as sorter
from covey.app import tidy_facts as facts
from covey.app import topic_links
from covey.cards import project_proposal as cards
from covey.core import topic_gist
from covey.core.text import cut
from covey.core.topics import label_title


class TopicWords:
    """Topics as the cards word them. ``runtime``: the owner's chat (``runtime.access``). ``link``: a topic's applink for a card
    (``link(state, topic, profile=...)``)."""

    def __init__(self, runtime, link=topic_links.link_for) -> None:
        self.runtime = runtime
        self.link = link

    def topic_rows(self, ctx, chat, topics) -> list:
        """The proposal card's rows (cards/project_proposal.py topic_lines): the T number and its link, the title, the
        本质 of its 话题要点, the days it has gone on and the owner's turns. The links read Feishu: outside every lock."""
        state, now, rows = Path(ctx.state_dir), time.time(), []
        for topic in topics:
            label, title = label_title(state, topic)
            found = sorter.material(state, chat, topic, now)
            try:
                link = self.link(state, topic, profile=getattr(ctx, "profile", None)) if label else ""
            except Exception:  # noqa: BLE001 - a T number without its link, as before
                link = ""
            essence = str(topic_gist.read(state, topic).get("essence") or found.gist.get("essence") or "")
            rows.append({"label": label, "title": title, "link": link, "essence": essence,
                         "now": facts.where_now(state, chat, topic) or facts.latest(found.conclusions),
                         "days": round(found.days, 1), "turns": found.turns})
        return rows

    def entries(self, ctx, topics) -> list:
        """Every topic as {key, label, title, link}: its T number (a topic without one, an old one or a Harness's own, is named by
        what the owner said first), its title and the link that opens it ('' when there is none)."""
        state, found = Path(ctx.state_dir), []
        for topic in topics:
            label, title = label_title(state, topic)
            if not label:
                said = sorter.material(state, facts.owner_chat(self.runtime), topic, time.time()).owner
                label, title = "未编号话题", title or (cut(said[0], 24) if said else "")
            try:
                link = self.link(state, topic, profile=getattr(ctx, "profile", None))
            except Exception:  # noqa: BLE001 - a T number without its link
                link = ""
            found.append({"key": topic, "label": label, "title": title or "", "link": link or ""})
        return found

    def linked(self, ctx, topics) -> str:
        """Every topic as Markdown, its T number a link that opens it and its title after it."""
        words = []
        for entry in self.entries(ctx, topics):
            shown = f"[{entry['label']}]({entry['link']})" if entry["link"] else entry["label"]
            words.append(f"{shown} {cards.literal_text(entry['title'])}" if entry["title"] else shown)
        return "、".join(words)

    def line(self, state, record, ctx=None) -> str:
        """One row of the 整理专项 card (app/tidy_facts.py ``line``): Markdown with every topic linked when ``ctx`` is given."""
        return facts.line(state, record, self.linked(ctx, record.get("topics") or ()) if ctx is not None else None)

    def row_item(self, ctx, record) -> dict:
        """The data of a row of the 整理专项 card (app/tidy_facts.py ``row_item``) with its topics' entries."""
        return facts.row_item(Path(ctx.state_dir), record, self.entries(ctx, record.get("topics") or ()))
