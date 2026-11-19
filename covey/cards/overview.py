"""The 手上的事 card (app/overview.py), pinned in the owner's private chat with the bot and redrawn whole as its
tasks change: the questions waiting for the owner first, then the tasks running, then the ones that ended in the
last few minutes. Each row is the topic's number and first message, and under it in grey since when and where it is
(the question, the checkpoint's phase), then what it runs with. Times are times of day (``when``), not durations:
the card changes with its content, not with the clock (until 2026-11-06 a row's 等了 18 小时 5 分 rewrote it every
minute). Questions waiting over STALE seconds are one grey line of topic numbers (on 2026-11-05 seven of the private
chat's eight had waited 21 hours to five days).

``view`` is plain data from app/overview.py: ``waiting`` rows (label, title, ask, since), newest first, ``running``
rows (label, title, harness, workspace, status, started, phase, progress: its estimate as a time of day or 可能卡住了) and ``ended`` rows (label, title, status, at); times
are epoch seconds, ``started`` None while the task waits in the queue.

With the pinned board on (app/pins.py) ``view`` also has ``board``: ``counts`` (group -> how many), ``pinned`` (how
many of them have their pin; ``unseen``: how many results the owner has not seen, 有结果未看), the rows without a
pin under ``waiting``, ``result`` (as ``ended`` rows, ``seen`` marked 已看) and
``running``, and ``stale``, the questions waiting too long to be on it. The card is then the board's index: the
counts, the pinned ones as one line, only the rows without a pin. Its line of old questions has the button
这些都不用回答了 (DISMISS, its value the topics listed), and ``dismissed``, the T numbers of the topics the owner
took off the board, is one line at the bottom."""
from __future__ import annotations

import datetime
import time

from covey.cards.elements import (grey, item, md, note, notice_card, panel_header, section, styled_panel,
                                           text_button)
from covey.core import turn_words
from covey.core.text import safe_text

TITLE = "手上的事"
SHOWN = 8  # Rows a part shows; the rest is one line under them.
TITLE_CHARS, DETAIL_CHARS = 30, 80
STALE = 86400  # A question waiting this long is listed by its topic number only.
LABELS = 10  # Topic numbers that line names.
DISMISS = "bridge_overview_dismiss"  # 这些都不用回答了 (app/overview.py claim).
DISMISS_MAX = 50  # Topics its value names at most.
# How a task ended, as 刚结束 says it.
OUTCOMES = turn_words.OVERVIEW_OUTCOMES


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


def _row(label, title, detail):
    shown = label if label.startswith("[") else f"**{label}**" if label else ""  # A link (``linked``) is bold inside.
    head = " ".join(part for part in (shown, safe_text(title, TITLE_CHARS)) if part)
    return md(f"{head}\n{grey(safe_text(detail, DETAIL_CHARS))}" if detail else head)


def linked(row, bold=False) -> str:
    """A row's label as a link to the card it is acted on when it has one (``url``: a 等我's, core/board_read.py waits)."""
    label = row.get("label") or ""
    if not row.get("url") or label.startswith("["):
        return label
    return f"[**{label or '话题'}**]({row['url']})" if bold else f"[{label or '话题'}]({row['url']})"


def _waiting(row, now):
    return _row(linked(row, bold=True), row["title"], " · ".join(part for part in (
        f"{when(row['since'], now)} 问的" if row.get("since") else "", row.get("ask") or "") if part))


def _running(row, now):
    if row.get("status") == "queued" or not row.get("started"):
        time_part = "排队中"
    else:
        time_part = f"{when(row['started'], now)} 开始"
    stopping = "正在停止" if row.get("status") == "cancel_requested" else ""
    parts = (time_part, stopping, row.get("phase") or "", *(row.get("progress") or ()), row.get("harness") or "",
             row.get("workspace") or "")
    return _row(row["label"], row["title"], " · ".join(part for part in parts if part))


def _ended(row, now):
    seen = " · 已看" if row.get("seen") else ""  # A board result seen within its KEEP_SEEN (app/pins.py).
    return _row(row["label"], row["title"],
                f"{OUTCOMES.get(row['status'], row['status'])} · {when(row['at'], now)}{seen}")


def _part(title, rows, draw, now, count=None):
    elements = [section(f"{title}（{len(rows) if count is None else count}）")]
    elements += [draw(row, now) for row in rows[:SHOWN]]
    if len(rows) > SHOWN:
        elements.append(note(f"还有 {len(rows) - SHOWN} 件没列出"))
    return elements


def _waiting_part(rows, now):
    """等你回答: the recent questions as rows, the ones waiting over STALE as one line of their topic numbers."""
    recent = [row for row in rows if not row.get("since") or now - row["since"] < STALE]
    old = [row for row in rows if row not in recent]
    elements = _part(turn_words.OVERVIEW_GROUPS["waiting"], recent, _waiting, now, count=len(rows))
    if old:
        elements.append(stale_line(old))
    return elements


def counts(view):
    """The title bar's subtitle and the chat list's preview: how many wait and run."""
    parts = [f"等你回答 {len(view['waiting'])}" if view["waiting"] else "",
             f"进行中 {len(view['running'])}" if view["running"] else ""]
    return " · ".join(part for part in parts if part) or "没有进行中的任务"


def board_counts(board):
    counts = board["counts"]
    unseen = counts.get("unseen", counts["result"])  # A view saved before 2026-11-10 has no unseen count.
    return (f"等你回答 {counts['waiting']} · 进行中 {counts['running']} · 有结果 {counts['result']}"
            + (f"（未看 {unseen}）" if counts["result"] else ""))


def named(labels):
    labels = [label or "?" for label in labels]
    return "、".join(labels[:LABELS]) + ("等" if len(labels) > LABELS else "")


def stale_line(rows, button=False, origin=""):
    """The questions waiting over STALE as one line, here and on the 早晚汇总 card (cards/digest.py); ``button``:
    with 这些都不用回答了, which names these topics; ``origin``, the card it is on when not this one (its value's
    ``from``: app/overview.py claim hands the click on, app/digest.py redraws that card)."""
    line = note(f"另有 {len(rows)} 个等了一天以上：{named([linked(row) for row in rows])}")
    topics = list(dict.fromkeys(row["topic"] for row in rows if row.get("topic")))[:DISMISS_MAX]
    if not button or not topics:
        return line
    value = {"kind": DISMISS, "topics": topics, **({"from": origin} if origin else {})}
    return item(line, text_button("这些都不用回答了", value))


def dismissed_line(labels):
    """What the line of ``stale_line`` becomes on a card that is not redrawn from the board (the 早晚汇总's) once
    its button took ``labels``' topics off: grey, no button."""
    return note(f"已撤下 {len(labels)} 个：{named(labels)}")


def _board(board, now):
    """The index under the pinned board: counts, how many are pinned, the rest by group."""
    elements = [md(board_counts(board))]
    if board["pinned"]:
        elements.append(note("在 Pin 页里点话题的卡片，直接打开那个话题"))  # How many: the header says it.
    # Top to bottom as the Pin board (app/pins.py GROUPS, the owner's D2, 2026-11-08).
    for key, draw in (("waiting", _waiting), ("running", _running), ("result", _ended)):
        title = turn_words.OVERVIEW_GROUPS[key]
        if board[key]:
            elements += _part(f"{title} · 未 Pin", board[key], draw, now)
    if board["stale"]:
        elements.append(stale_line(board["stale"], button=True))
    if board.get("dismissed"):
        elements.append(note(f"你撤下的 {len(board['dismissed'])} 个：{named(board['dismissed'])}"))
    return elements


def overview_card(view, now, stamp="", where="私聊"):
    """The card for ``view`` at ``now``; ``stamp``, the 更新于 time ('' leaves the line out: what app/overview.py
    compares to tell whether the card changed); ``where``, the chat's word (私聊, or 群 for a personal group's, app/group_boards.py)."""
    if "board" in view:
        board = view["board"]
        elements = _board(board, now)
        if stamp:
            elements.append(note(f"更新于 {stamp} · 只列这个{where}里的任务"))
        head = panel_header("waiting_outlined", TITLE, f"Pin 着 {board['pinned']} 个话题" if board["pinned"]
                            else board_counts(board))
        return styled_panel(head, elements, f"{TITLE} · {board_counts(board)}")
    elements = []
    if view["waiting"]:
        elements += _waiting_part(view["waiting"], now)
    if view["running"]:
        elements += _part("进行中", view["running"], _running, now)
    if view["ended"]:
        elements += _part("刚结束", view["ended"], _ended, now)
    if not elements:
        elements.append(md(grey("现在没有进行中的任务，也没有等你回答的问题。")))
    if stamp:
        elements.append(note(f"更新于 {stamp} · 只列这个{where}里的任务"))
    head = panel_header("waiting_outlined", TITLE, counts(view))
    return styled_panel(head, elements, f"{TITLE} · {counts(view)}")


def retired_card():
    """What a card that was replaced by a newer one says: it no longer changes."""
    return notice_card(f"这张“{TITLE}”已不再更新，最新的在置顶（Pin）里。",
                       f"{TITLE} · 已不再更新")
