"""The digest card (app/digest.py): sent at the times the deployer set, at the top level of the owner's private chat.
Three parts, each left out when empty: 等你 (questions waiting, tasks waiting to be started), 在做, 做完 (turns that
ended since the digest before, and every result the owner has not seen, however old: ``older`` of them ended before,
said in the heading; the ones not unseen in grey, marked 已看). A row is the topic's number (a link
to it once app/topic_links.py found one), its title, one line of state or phase and how long; a part shows SHOWN
rows and says how many more there are. Under 等你, the questions waiting over a day are one grey line of topic
numbers, 手上的事's (cards/overview.py stale_line), with its button 不管了 when ``dismissable``; once
clicked, the line is 已撤下 N 个 (dismissed_line), grey, without the button.

``view`` is plain data: ``slot`` and ``since`` (epoch seconds), ``waiting``, ``running`` and ``done`` rows, each
{label (Markdown, maybe a link), title, state, took, seen}, ``older``; ``stale`` rows {topic, label, title}, ``knowledge`` and ``wanted`` (rows {what, why, would}: 我本来想找你的事), ``dismissable``
and ``cleared`` (the labels taken off from this card) — a view saved before them has none. ``asked``: the bot menu's
看板 at any time (app/digest.py board), 随时查看 in the title; with nothing to list it says so (EMPTY)."""
from __future__ import annotations

from covey.cards.project_proposal import board_rows
from covey.cards.elements import folded_panel, grey, md, menu_item, note, panel_header, section, styled_panel
from covey.cards.overview import clock, dismissed_line, named, stale_line
from covey.core.text import safe_text

TITLE = "汇总"
SHOWN = 8
TITLE_CHARS, STATE_CHARS = 30, 40
PARTS = (("waiting", "等你"), ("running", "在做"), ("done", "做完"))
ASKED = "随时查看"  # 看板's title: the card the owner asked for, not a timed one.
EMPTY = "现在没有要你处理的：没有等你的、没有在做的，{} 之后也没有做完的。"
ICON = "waiting_outlined"


ORIGIN = "digest"  # The stale line's button value's ``from`` (app/overview.py claim hands such a click on).


def rows_of(view, key):
    """How many ``key`` has: the 等你 part's old questions too, not the ones taken off from this card (the owner
    read "等你 4" over 已撤下 3 个 as four still waiting, 2026-11-09), so a redraw after the click lowers the header
    and the part's count with the line."""
    extra = len(view.get("stale") or []) if key == "waiting" else 0
    return len(view[key]) + extra


def counts(view):
    return " · ".join(f"{name} {rows_of(view, key)}" for key, name in PARTS if rows_of(view, key))


def tail(view):
    """The 等你 part's last line: its old questions (with the button when ``dismissable``), or the ones taken off."""
    if view.get("stale"):
        return [stale_line(view["stale"], button=bool(view.get("dismissable")), origin=ORIGIN)]
    return [dismissed_line(view["cleared"])] if view.get("cleared") else []


def tail_text(view):
    if view.get("stale"):
        return [f"另有 {len(view['stale'])} 个等了一天以上：{named([row.get('label') for row in view['stale']])}"]
    return [f"已撤下 {len(view['cleared'])} 个：{named(view['cleared'])}"] if view.get("cleared") else []


def line(row, *, plain=False):
    parts = [row.get("label") or "", safe_text(row.get("title") or "", TITLE_CHARS),
             safe_text(row.get("state") or "", STATE_CHARS), row.get("took") or ""]
    text = " · ".join(part for part in parts if part)
    if not row.get("seen"):
        return text
    return f"{text} · 已看" if plain else grey(f"{text} · 已看")


def done_since(view):
    """做完's heading after its count: since when, and how many results not seen ended before (``older``)."""
    older = view.get("older") or 0  # A view saved before 2026-11-10 has none.
    return f" · {clock(view['since'])} 之后结束" + (f"，另有更早没看的 {older} 个" if older else "")


def title_of(view):
    return f"{TITLE} · {ASKED} · {clock(view['slot'])}" if view.get("asked") else f"{TITLE} · {clock(view['slot'])}"


def digest_card(view):
    elements = []
    for key, name in PARTS:
        rows, last = view[key], tail(view) if key == "waiting" else []
        if not rows and not last:
            continue
        heading = f"{name}（{rows_of(view, key)}）" + (done_since(view) if key == "done" else "")
        elements.append(section(heading))
        elements += [md(line(row)) for row in rows[:SHOWN]]
        if len(rows) > SHOWN:
            elements.append(note(f"另有 {len(rows) - SHOWN} 个"))
        elements += last
    if not elements:
        elements.append(md(EMPTY.format(clock(view["since"]))))
    elements += knowledge_elements(view)
    elements += wanted_elements(view)
    if isinstance(view.get("tidy"), dict):
        elements += board_rows([str(name) for name in view["tidy"].get("projects") or ()])
    elements.append(note(f"只列这个{view.get('where') or '私聊'}里的话题，管家不列"))
    title = title_of(view)
    return styled_panel(panel_header(ICON, title, counts(view)), elements, " · ".join(filter(None, [title, counts(view)])))


def knowledge_lines(view):
    """新记的知识's rows (app/digest.py knowledge_view): op · id · group · text; none when the view has no such part
    (a view saved before it, or nothing new)."""
    part = view.get("knowledge") or {}
    return [f"{row['op']} · {row['id']} · {row['where']} · {row['text']}" for row in part.get("rows") or []]


def knowledge_elements(view):
    lines = knowledge_lines(view)
    if not lines:
        return []
    more = (view.get("knowledge") or {}).get("more") or 0
    return [section(f"新记的知识（{len(lines) + more}）"), *(md(text) for text in lines),
            *([note(f"另有 {more} 条")] if more else [])]


WANTED_TITLE = "我本来想找你的事"
WANTED_HINT = "这类该找你，或这类不用，在私聊里告诉我一句，我会记下。"


def wanted_lines(view):
    """我本来想找你的事's rows (app/digest.py wanted_view): what · why · would; none when the view has no such part."""
    return [" · ".join(part for part in (row.get("what"), row.get("why"), f"本来会：{row.get('would')}" if row.get("would") else "") if part)
            for row in view.get("wanted") or []]


def wanted_elements(view):
    lines = wanted_lines(view)
    return [section(f"{WANTED_TITLE}（{len(lines)}）"), *(md(text) for text in lines), note(WANTED_HINT)] if lines else []


def folded_board():
    """看板's card folded in place (a second menu click, app/digest.py fold_board)."""
    return folded_panel("bitablekanban_outlined", "看板", menu_item("看板"))


def digest_text(view):
    """The card in Markdown, for when Feishu refuses the card."""
    lines = [f"**{title_of(view)}**"]
    for key, name in PARTS:
        rows, last = view[key], tail_text(view) if key == "waiting" else []
        if rows or last:
            lines.append(f"**{name}（{rows_of(view, key)}）**")
            lines += [line(row, plain=True) for row in rows[:SHOWN]]
            if len(rows) > SHOWN:
                lines.append(f"另有 {len(rows) - SHOWN} 个")
            lines += last
    if len(lines) == 1:
        lines.append(EMPTY.format(clock(view["since"])))
    new = knowledge_lines(view)
    if new:
        more = (view.get("knowledge") or {}).get("more") or 0
        lines += [f"**新记的知识（{len(new) + more}）**", *new, *([f"另有 {more} 条"] if more else [])]
    found = wanted_lines(view)
    if found:
        lines += [f"**{WANTED_TITLE}（{len(found)}）**", *found, WANTED_HINT]
    return "\n".join(lines)
