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
看板 at any time (app/digest.py board), 随时查看 in the title; with nothing to list it says so (EMPTY).

The evening digest's own parts (app/digest.py ``daily``), each left out when the view has none: ``reached`` (本周找你 N 次：问题 a
次、停住 b 次, cards/weekly_usage.py reached_line) and ``proxy`` ({sent, unsent}: 今天代答) as two lines after the three parts,
then ``remember`` (你反复说的: rows {id, text, labels, sources (the labels linked), workspace, state, error}): each its rule in
bold, where it came from and where it would go, and 「记下」 (REMEMBER_KIND, ``op`` save) and 「不用」 (drop) while open; once
decided, what came of it in grey instead."""
from __future__ import annotations

from covey.cards.project_proposal import board_rows
from covey.cards.elements import callback_button, folded_panel, grey, md, menu_item, note, panel_header, section, styled_panel
from covey.cards.overview import clock, dismissed_line, named, stale_line
from covey.cards.weekly_usage import reached_line
from covey.core.text import literal, safe_text

TITLE = "汇总"
SHOWN = 8
TITLE_CHARS, STATE_CHARS = 30, 40
PARTS = (("waiting", "等你"), ("running", "在做"), ("done", "做完"))
ASKED = "随时查看"  # 看板's title: the card the owner asked for, not a timed one.
EMPTY = "{} 时没有要你处理的：没有等你的、没有在做的，{} 之后也没有做完的。"  # A record as of its time (the card stays in the chat).
ICON = "waiting_outlined"


ORIGIN = "digest"  # The stale line's button value's ``from`` (app/overview.py claim hands such a click on).
REMEMBER_KIND = "bridge_digest_remember"  # 「记下」 / 「不用」 on one of 你反复说的: ``id`` of the candidate, ``op`` save or drop.
REMEMBER_TITLE = "你反复说的"
REMEMBER_HINT = ("从今天结束的话题里找的、你在不同话题里说过不止一次的话。点「记下」才记进记忆，以后每一轮都带上；"
                 "不点不生效，开了脱敏的话题不看。")
REMEMBER_DONE = {"saved": "已记下", "declined": "不用了，以后不再提"}


def rows_of(view, key):
    """How many ``key`` has: the 等你 part's old questions too, not the ones taken off from this card ("等你 4" over
    已撤下 3 个 reads as four still waiting), so a redraw after the click lowers the header
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
    older = view.get("older") or 0  # A view saved by an older release has none.
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
        elements.append(md(EMPTY.format(clock(view["slot"]), clock(view["since"]))))
    lines = daily_lines(view)
    if lines:
        elements.append(md("\n".join(lines)))
    elements += remember_elements(view)
    elements += knowledge_elements(view)
    elements += wanted_elements(view)
    if isinstance(view.get("tidy"), dict):
        elements += board_rows([str(name) for name in view["tidy"].get("projects") or ()])
    elements.append(note(f"只列这个{view.get('where') or '私聊'}里的话题，管家不列"))
    title = title_of(view)
    return styled_panel(panel_header(ICON, title, counts(view)), elements, " · ".join(filter(None, [title, counts(view)])))


def daily_lines(view):
    """The evening digest's count lines: 本周找你 and 今天代答; none in a view without them."""
    lines = [reached_line(view["reached"])] if isinstance(view.get("reached"), dict) else []
    proxied = view.get("proxy") if isinstance(view.get("proxy"), dict) else None
    if proxied is not None:
        unsent = int(proxied.get("unsent") or 0)
        lines.append(f"今天代答发出 {int(proxied.get('sent') or 0)} 条" + (f"，{unsent} 条没发出，在菜单「代答」里看" if unsent else ""))
    return lines


def remember_meta(row, *, plain=False):
    """Where a candidate came from and where 「记下」 puts it: ``来源：T12、T15 · 记到工作区 covey`` (``plain``: no Markdown, the
    labels not linked)."""
    labels = [str(label) for label in row.get("labels") or ()]
    where = f"工作区 {row['workspace']}" if row.get("workspace") else "全局"
    if plain:
        sources = "、".join(labels)
    else:
        sources, where = row.get("sources") or "、".join(literal(label) for label in labels), literal(where)
    return (f"来源：{sources} · " if sources else "") + f"记到{where}"


def remember_elements(view):
    rows = view.get("remember") or []
    if not rows:
        return []
    elements = [section(f"{REMEMBER_TITLE}（{len(rows)}）")]
    for row in rows:
        elements.append(md(f"**{literal(row.get('text') or '')}**"))
        meta = remember_meta(row)
        state = row.get("state") or "open"
        if state in REMEMBER_DONE:
            elements.append(note(f"{meta} · {REMEMBER_DONE[state]}"))
            continue
        if row.get("error"):
            meta += f" · ⚠️ 没记下：{literal(row['error'])}"
        value = {"kind": REMEMBER_KIND, "id": row.get("id") or ""}
        elements += [note(meta), {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "auto", "elements": [callback_button("记下", {**value, "op": "save"},
                                                                              style="primary", size="small")]},
            {"tag": "column", "width": "auto", "elements": [callback_button("不用", {**value, "op": "drop"},
                                                                              style="text", size="small")]}]}]
    elements.append(note(REMEMBER_HINT))
    return elements


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
        lines.append(EMPTY.format(clock(view["slot"]), clock(view["since"])))
    lines += daily_lines(view)
    rows = view.get("remember") or []
    if rows:
        lines.append(f"**{REMEMBER_TITLE}（{len(rows)}）**")
        lines += [f"- {row.get('text') or ''}（{remember_meta(row, plain=True)}"
                  + (f" · {REMEMBER_DONE[row['state']]}" if row.get("state") in REMEMBER_DONE else "") + "）" for row in rows]
        lines.append("在卡片上点「记下」才记进记忆，不点不生效。")
    new = knowledge_lines(view)
    if new:
        more = (view.get("knowledge") or {}).get("more") or 0
        lines += [f"**新记的知识（{len(new) + more}）**", *new, *([f"另有 {more} 条"] if more else [])]
    found = wanted_lines(view)
    if found:
        lines += [f"**{WANTED_TITLE}（{len(found)}）**", *found, WANTED_HINT]
    return "\n".join(lines)
