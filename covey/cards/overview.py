"""The 手上的事 card (app/overview.py), pinned in the owner's private chat with the bot and redrawn whole as its
tasks change, in the 看板's 夜航 look (cards/board.py, docs/ui.md 5.2): the conclusion band (要你处理 N 件事, the most
pressing topic, a segment bar of each state's count; 都在掌握中 when nothing asks), then the questions waiting for the
owner, the tasks running and the ones that ended in the last few minutes, each a topic row (cards/ui.py topic_row: its
state's dot and word, its T number and title, one line in words, grey figures; a question's row opens its card and has
去回答). Times are times of day (``when``), not durations: the card changes with its content, not with the clock (a row's
等了 18 小时 5 分 would rewrite it every minute). Questions waiting over STALE seconds are one grey line of topic numbers
(such ones can wait for days).

``view`` is plain data from app/overview.py: ``waiting`` rows (label, title, ask, since, url), newest first, ``running``
rows (label, title, harness, workspace, status, started, phase, progress: its estimate as a time of day or 可能卡住了) and ``ended`` rows (label, title, status, at); times
are epoch seconds, ``started`` None while the task waits in the queue.

With the pinned board on (app/pins.py) ``view`` also has ``board``: ``counts`` (group -> how many), ``pinned`` (how
many of them have their pin; ``unseen``: how many results the owner has not seen, 有结果未看), the rows without a
pin under ``waiting``, ``result`` (as ``ended`` rows, ``seen`` marked 已看) and
``running``, and ``stale``, the questions waiting too long to be on it. The card is then the board's index: the
band with the counts and how many are pinned, only the rows without a pin. Its line of old questions has the button
不管了 (DISMISS, its value the topics listed), and ``dismissed``, the T numbers of the topics the owner
took off the board, is one line at the bottom."""
from __future__ import annotations

import datetime
import time

from covey.cards import ui
from covey.cards.elements import CARD_BYTES, CARD_NODES, fits, item, note, notice_card, plain, styled_panel, text_button
from covey.core import look, turn_words
from covey.core.text import cut, literal as literal_text

TITLE = "手上的事"
ICON = "waiting_outlined"
SHOWN = 8  # Rows a part shows; the rest is one line under them.
FIT_SHOWN = (SHOWN, 6, 4, 3, 2, 1)  # Rows a part shows, tried in turn until the card fits (``overview_card``).
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 1500  # Room left under CardKit's limits for what CardKit adds.
TITLE_CHARS, DETAIL_CHARS = 30, 80
STALE = 86400  # A question waiting this long is listed by its topic number only.
LABELS = 10  # Topic numbers that line names.
DISMISS = "bridge_overview_dismiss"  # 不管了 (这些都不用回答了 in older releases) (app/overview.py claim).
DISMISS_MAX = 50  # Topics its value names at most.


def when(seconds, now):
    """The local time of ``seconds`` as seen at ``now``: 14:05 the same day, 昨天 22:10 the day before, 11-03 09:30
    before that."""
    then, today = time.localtime(seconds), time.localtime(now)
    days = (datetime.date(*today[:3]) - datetime.date(*then[:3])).days
    if days == 0:
        return time.strftime("%H:%M", then)
    return time.strftime("昨天 %H:%M" if days == 1 else "%m-%d %H:%M", then)


def clock(seconds):
    return time.strftime("%H:%M", time.localtime(seconds))


# How an ended task's row reads, as the 看板 says its state (core/look.py STATES): a result not seen, one seen, a stop.
ENDED_TONES = {"done": "result", "continued": "result", "failed": "stopped", "interrupted": "stopped",
               "cancelled": "cancelled"}
STUCK_WORDS = "可能卡住了"  # core/progress.py board_words: such a running row is drawn 可能卡住.
ACT = ("waiting", "stopped", "stuck")  # 要你处理: what the band counts, as cards/board.py ACT.
LEAD = 30  # Characters of the most pressing topic's words on the band, as cards/board.py LEAD.


def linked(row, bold=False) -> str:
    """A row's label as a link to the card it is acted on when it has one (``url``: a 等我's, core/board_read.py waits)."""
    label = row.get("label") or ""
    if not row.get("url") or label.startswith("["):
        return label
    return f"[**{label or '话题'}**]({row['url']})" if bold else f"[{label or '话题'}]({row['url']})"


def named_row(row, **fields):
    """A row of the view as the 看板's topic row takes one (cards/board.py block): its T number, its title cut to
    TITLE_CHARS, its line cut to DETAIL_CHARS."""
    line = fields.pop("line", "")
    return {"label": row.get("label") or "", "title": cut(row.get("title") or "", TITLE_CHARS),
            "line": cut(line, DETAIL_CHARS) if line else "", **fields}


def waiting_row(row, now):
    """A question as a 看板 row: what it asks, since when; it opens the card it is answered on."""
    return named_row(row, url=row.get("url") or "", line=row.get("ask") or "",
                     meta=[f"{when(row['since'], now)} 问的"] if row.get("since") else [])


def running_kind(row) -> str:
    """在做, or 可能卡住 (its estimate says so); one in the queue is 在做 with 排队中, as on the 看板."""
    return "stuck" if STUCK_WORDS in (row.get("progress") or ()) else "running"


def running_row(row, now):
    """A running (or queued) task as a 看板 row: its phase, then since when, its estimate, what it runs with."""
    queued = row.get("status") == "queued" or not row.get("started")
    time_part = "排队中" if queued else f"{when(row['started'], now)} 开始"
    line = "正在停止" if row.get("status") == "cancel_requested" else row.get("phase") or ""
    meta = [time_part, *(part for part in row.get("progress") or () if part != STUCK_WORDS), row.get("harness") or "",
            row.get("workspace") or ""]
    return named_row(row, line=line, meta=meta)


def ended_kind(row) -> str:
    kind = ENDED_TONES.get(row.get("status"), "result")
    return "done" if kind == "result" and row.get("seen") else kind


def ended_row(row, now):
    """A task that ended a moment ago as a 看板 row: when, and 已看 for a board result seen within its KEEP_SEEN."""
    return named_row(row, meta=[when(row["at"], now), "已看" if row.get("seen") else ""])


def heading(word, count, icon):
    """A group's heading as the 看板 draws one (cards/board.py heading): its word, how many, its icon."""
    return {"tag": "markdown", "content": f"**{word}**  <font color='grey'>{count}</font>", "text_size": "normal_v2",
            "icon": {"tag": "standard_icon", "token": icon, "color": "grey"}}


def head_of(row) -> str:
    return " ".join(part for part in (row.get("label"), row.get("title")) if part) or "话题"


def answer_button(url):
    """去回答 (cards/board.py button's look): the row's one primary button, opening the card the question is on."""
    return {"tag": "button", "text": plain("去回答"), "type": "primary", "size": "small",
            "behaviors": [{"type": "open_url", "default_url": url}]}


def rows_part(rows, draw, now, kind, shown=SHOWN):
    """The rows of a group as topic rows (cards/ui.py topic_row, as cards/board.py block draws them), ``shown`` of
    them, the rest one line; ``kind``: each row's state key."""
    elements = []
    for row in rows[:shown]:
        drawn = draw(row, now)
        url = drawn.get("url") or ""
        elements.append(ui.topic_row(kind(row), head_of(drawn), drawn.get("line") or "", drawn.get("meta") or (),
                                     url=url, buttons=[answer_button(url)] if kind(row) == "waiting" and url else []))
    if len(rows) > shown:
        elements.append(note(f"还有 {len(rows) - shown} 件没列出"))
    return elements


def _waiting_part(rows, now, count=None, word=None, shown=SHOWN):
    """等你: the recent questions as rows, the ones waiting over STALE as one line of their topic numbers."""
    recent = [row for row in rows if not row.get("since") or now - row["since"] < STALE]
    old = [row for row in rows if row not in recent]
    elements = [heading(word or look.group_word("waiting"), len(rows) if count is None else count,
                        look.STATES["waiting"].icon)]
    elements += rows_part(recent, waiting_row, now, lambda row: "waiting", shown)
    if old:
        elements.append(stale_line(old))
    return elements


def sizes(waiting, running, ended):
    """{state key: how many} in core/look.py PRESSING's order, as the 看板 counts them (cards/board.py state_counts)."""
    stuck = sum(running_kind(row) == "stuck" for row in running)
    kinds = [ended_kind(row) for row in ended]
    return {"waiting": len(waiting), "stopped": kinds.count("stopped"), "stuck": stuck, "result": kinds.count("result"),
            "running": len(running) - stuck}


def band(found, now, quiet, empty=None):
    """The conclusion band (cards/board.py band): how many things ask for him, the most pressing topic, every state's
    count as a bar; ``quiet``, its grey line when nothing asks."""
    lead = {"waiting": [waiting_row(row, now) for row in found["waiting"]],
            "stopped": [ended_row(row, now) for row in found["ended"] if ended_kind(row) == "stopped"],
            "stuck": [running_row(row, now) for row in found["running"] if running_kind(row) == "stuck"],
            "result": [ended_row(row, now) for row in found["ended"] if ended_kind(row) == "result"],
            "running": [running_row(row, now) for row in found["running"] if running_kind(row) != "stuck"]}
    counts = sizes(found["waiting"], found["running"], found["ended"])
    asked = sum(counts[key] for key in ACT)
    first = look.pressing(counts)
    row = (lead.get(first) or [None])[0] if first else None
    line = quiet
    if row is not None and first in ACT:
        words = f"{look.word(first)} · {head_of(row)}"
        line = literal_text("先看：" + (words if len(words) <= LEAD else words[:LEAD - 1] + "…"))
    headline = ui.number(asked, "件事") if asked else empty[0] if empty else "**都在掌握中**"
    if empty and not asked:
        line = empty[1]
    return ui.conclusion_band("要你处理", headline, line, below=ui.seg_bar(counts),
                              url=(row or {}).get("url") or "" if first in ACT else ""), counts


def tags(counts, empty=None):
    """The title bar's tags: (text, colour) of the states that have someone, most pressing first (cards/board.py counts)."""
    found = [(f"{counts[key]} {look.word(key)}", look.STATES[key].color) for key in ("waiting", "stopped", "stuck", "running")
             if counts.get(key)]
    return found or [(empty[2], "neutral") if empty else ("都在掌握中", "green")]


def head(subtitle, found):
    return {"title": plain(TITLE), "subtitle": plain(subtitle),
            "text_tag_list": [{"tag": "text_tag", "text": plain(text), "color": color} for text, color in found[:3]],
            "template": "default", "icon": {"tag": "standard_icon", "token": ICON, "color": "blue"},
            "padding": "10px 14px 0px 14px"}


def counts_words(view):
    """The chat list's preview: how many wait and run."""
    parts = [f"{turn_words.OVERVIEW_GROUPS['waiting']} {len(view['waiting'])}" if view["waiting"] else "",
             f"{turn_words.OVERVIEW_GROUPS['running']} {len(view['running'])}" if view["running"] else ""]
    return " · ".join(part for part in parts if part) or "没有在做的任务"


def board_counts(board):
    counts = board["counts"]
    unseen = counts.get("unseen", counts["result"])  # A view saved by an older release has no unseen count.
    return (f"{turn_words.OVERVIEW_GROUPS['waiting']} {counts['waiting']} · {turn_words.OVERVIEW_GROUPS['running']} "
            f"{counts['running']} · 有结果 {counts['result']}"
            + (f"（未看 {unseen}）" if counts["result"] else ""))


def named(labels):
    labels = [label or "?" for label in labels]
    return "、".join(labels[:LABELS]) + ("等" if len(labels) > LABELS else "")


def stale_line(rows, button=False, origin=""):
    """The questions waiting over STALE as one line, here and on the 早晚汇总 card (cards/digest.py); ``button``:
    with 不管了, which names these topics; ``origin``, the card it is on when not this one (its value's
    ``from``: app/overview.py claim hands the click on, app/digest.py redraws that card)."""
    line = note(f"另有 {len(rows)} 个等了一天以上：{named([linked(row) for row in rows])}")
    topics = list(dict.fromkeys(row["topic"] for row in rows if row.get("topic")))[:DISMISS_MAX]
    if not button or not topics:
        return line
    value = {"kind": DISMISS, "topics": topics, **({"from": origin} if origin else {})}
    return item(line, text_button("不管了", value))


def dismissed_line(labels):
    """What the line of ``stale_line`` becomes on a card that is not redrawn from the board (the 早晚汇总's) once
    its button took ``labels``' topics off: grey, no button."""
    return note(f"已撤下 {len(labels)} 个：{named(labels)}")


def _board(board, now, shown=SHOWN, empty=None):
    """The index under the pinned board: the band with the counts and how many are pinned, the rest by group."""
    counts = board["counts"]
    sized = {"waiting": counts["waiting"], "result": counts.get("unseen", counts["result"]), "running": counts["running"]}
    asked = counts["waiting"]
    # How many are pinned: the title bar says it.
    line = ("在 Pin 页里点话题的卡片，直接打开那个话题" if board["pinned"]
            else "现在没有在等你的事" if not asked else "")
    if empty and not asked:
        line = empty[1]
    elements = [ui.conclusion_band("要你处理", ui.number(asked, "件事") if asked else empty[0] if empty else "**都在掌握中**", line,
                                   below=ui.seg_bar(sized))]
    # Top to bottom as the Pin board (app/pins.py GROUPS).
    if board["waiting"]:
        elements += _waiting_part(board["waiting"], now, word=f"{look.group_word('waiting')} · 未 Pin", shown=shown)
    if board["running"]:
        elements.append(heading(f"{look.word('running')} · 未 Pin", len(board["running"]), look.STATES["running"].icon))
        elements += rows_part(board["running"], running_row, now, running_kind, shown)
    if board["result"]:
        elements.append(heading(f"{look.word('result')} · 未 Pin", len(board["result"]), look.STATES["result"].icon))
        elements += rows_part(board["result"], ended_row, now, ended_kind, shown)
    if board["stale"]:
        elements.append(stale_line(board["stale"], button=True))
    if board.get("dismissed"):
        elements.append(note(f"你撤下的 {len(board['dismissed'])} 个：{named(board['dismissed'])}"))
    return elements, tags({"waiting": asked, "running": counts["running"]}, empty)


def overview_card(view, now, stamp="", where="私聊"):
    """The card for ``view`` at ``now``; ``stamp``, the 更新于 time ('' leaves the line out: what app/overview.py
    compares to tell whether the card changed); ``where``, the chat's word (私聊, or 群 for a personal group's, app/group_boards.py).
    A topic row costs more than the line it was: fewer rows a group (FIT_SHOWN) until the card is within what CardKit
    takes (cards/elements.py CARD_NODES, CARD_BYTES), the rest one line each."""
    for shown in FIT_SHOWN:
        card = _drawn(view, now, stamp, where, shown)
        if fits(card, NODES, BYTES):
            break
    return card


def _drawn(view, now, stamp, where, shown):
    empty = ui.empty_words(first=bool(view.get("first_use")))  # Only 还没有话题: this card is redrawn, never a record.
    if "board" in view:
        board = view["board"]
        elements, found = _board(board, now, shown, empty)
        subtitle = f"Pin 着 {board['pinned']} 个话题" if board["pinned"] else f"{where}里在推进的事"
        summary = f"{TITLE} · {board_counts(board)}"
    else:
        top, counts = band(view, now, "现在没有在做的任务，也没有在等你的事", empty)
        elements, found = [top], tags(counts, empty)
        if view["waiting"]:
            elements += _waiting_part(view["waiting"], now, shown=shown)
        if view["running"]:
            elements.append(heading(look.word("running"), len(view["running"]), look.STATES["running"].icon))
            elements += rows_part(view["running"], running_row, now, running_kind, shown)
        if view["ended"]:
            elements.append(heading("刚结束", len(view["ended"]), "yes_outlined"))
            elements += rows_part(view["ended"], ended_row, now, ended_kind, shown)
        subtitle = f"{where}里在推进的事"
        summary = f"{TITLE} · {counts_words(view)}"
    if stamp:
        elements.append(note(f"更新于 {stamp} · 只列这个{where}里的任务"))
    return ui.styled(styled_panel(head(subtitle, found), elements, summary, spacing="8px"))


def retired_card():
    """What a card that was replaced by a newer one says: it no longer changes."""
    return notice_card(f"这张“{TITLE}”已不再更新，最新的在置顶（Pin）里。",
                       f"{TITLE} · 已不再更新")
