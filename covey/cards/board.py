"""The bot menu's 看板: what is going on in the owner's private chat now, in the 夜航 look (docs/ui.md 5.2: 菜单「看板」).

The conclusion band first (cards/ui.py conclusion_band): 要你处理 N 件事 (等你, 停住了 and 可能卡住: core/board_read.py ACT,
counted by its act_count as 手上的事 counts them), the most pressing topic under it (core/look.py pressing), and how many
of each state as a segment bar; 都在掌握中 when nothing asks for him. Then, most pressing first: 等你 → 停住了 → 可能卡住 →
有结果没看 → 在做 → 专项 (with 整理专项, the one place it is) → 做完 and 已停止 (folded). Each topic is a topic row
(cards/ui.py topic_row): its state's dot and word, its title with ›, one line in words (what he is asked to decide, why
it stopped, what it is doing now) and a grey line of figures; the whole row opens it (``url``; a topic without one is not
a link). Buttons are real: 去回答 (opens the topic's question, the row's one primary button), 去任务卡 (a stopped row's
link), 不管了 (a text button: takes a stopped topic off the board, app/overview.py DISMISS, ``from`` ORIGIN, app/digest.py
redraws this card). The small buttons of T898 are gone: 记为看过, 取消 on 到点回来看, a paused project's 继续 and a stopped row's
检查并继续 (``RETIRED_KINDS``: a card sent before still has them, and a click on one is answered in words, app/digest.py). What asks for
him (等你, 停住了, 可能卡住) is listed whoever's it is; a live project's other topics are not listed by themselves: its
project's block counts them.

A project a guard paused (core/projects.py halted: its token budget, its wake-ups, no progress) is 停住了 in red with
why, counted among 要你处理 (its block opens the project's card, where 继续 is); one the owner stopped is 已停止 (docs/ui.md 2.2, 3).

A proposal of 整理专项 waiting for him (``pending``) is 等你 (docs/ui.md 2.2: 等你定的 are orange): listed under 等你 after
the questions (cards/project_proposal.py waiting_block), with 建专项 and 不管了, and counted among 要你处理 as 手上的事 counts it
(core/board_read.py Asks.proposals).

A topic that stopped has 去任务卡 on its row (the task card's link, where 检查并继续 and 看看 are).

With the web page's address set (core/web_sync.py page_url, COVEY_WEB=1: ``web``) the last line has 打开网页.

With nothing listed it says which kind of empty it is (ui.empty_words, ``empty_state``): 还没有话题 for an owner who never had
one (``first_use``), 某个时刻的记录 for a card redrawn from its saved view long after it was read (``at``, ``now``),
else 都在掌握中.

A cancelled topic is 停住了 unless the owner stopped it himself (core/look.py owner_stopped; docs/ui.md 3): then it is
已停止, background, in the fold. A row's ``tone`` (app/digest.py collect) says which; a view without it reads by its
status, as before.

``view`` is app/digest.py collect's, with ``url`` on each row and, from board_view, ``projects`` [{id, name, goal, topics,
waiting, stopped, stuck, running, halted, cancelled, folded}] (what asks for him first, folded away last: a block each for
the first SHOWN on, the rest and the folded ones a line each in one fold), ``proposals`` (open proposal cards) and ``tidy``
(整理专项 on). 早晚汇总 keeps its own card (cards/digest.py).

Many topics and projects (up to 50 on, folded ones piling up) would pass what CardKit takes in one card (cards/elements.py
CARD_NODES, CARD_BYTES): ``board_card`` then shows fewer rows a group, step by step (FIT_SHOWN), the rest one line each."""
from __future__ import annotations

from covey.cards import ui
from covey.cards.elements import (CARD_BYTES, CARD_NODES, collapsible, fits, grey, item, md, note, plain, styled_panel,
                                  text_button)
from covey.cards.ui import beside as body, link_button, row_button as button, topic_ref
from covey.cards.overview import DISMISS, dismissed_line, stale_line
from covey.cards.project_proposal import START_KIND, waiting_block as proposal_block
from covey.core import board_read, look
from covey.core.text import cut, literal as literal_text
from covey.core.turn_words import TASK_CARD_ACT

SNAPSHOT_AGE = 600  # A card redrawn from its saved view this long after it was read is a record, not now (``empty_state``).
ORIGIN = "digest"  # As cards/digest.py: app/overview.py claim hands a 不管了 on this card to app/digest.py.
ICON = "bitablekanban_outlined"
STOPPED = frozenset({"failed", "cancelled", "interrupted"})
# The five states the 看板 shows, as core/look.py STATES has them: (word, colour, block tint).
LOOKS = {key: (look.STATES[key].word, look.STATES[key].color, look.STATES[key].tint)
         for key in ("waiting", "stopped", "result", "running", "stuck")}
ACT = board_read.ACT  # 要你处理: what the band counts (core/board_read.py act_count), 手上的事's too.
# The 看板's own button (app/digest.py Digest.claim, before topic routing): 刷新 redraws the card from a fresh view in
# place (a second menu click folds it instead).
REFRESH_KIND = "bridge_board_refresh"
# Taken off the rows (T898 界面精简); a card sent before still has them, and app/digest.py answers a click on one in words
# (app/card_actions.py RETIRED_TEXT) and redraws the card: 记为看过, 取消 on a row of 到点回来看, 继续 on a paused project's
# block, 检查并继续 on a stopped row.
SEEN_KIND, REVISIT_KIND = "bridge_board_seen", "bridge_board_revisit_cancel"
PROJECT_GO_KIND, RECOVER_KIND = "bridge_board_project_go", "bridge_board_recover"
RETIRED_KINDS = frozenset({SEEN_KIND, REVISIT_KIND, PROJECT_GO_KIND, RECOVER_KIND})
# 去看板 under an answered question (cards/question_cards.py others_row): a new 看板 card, as the bot menu's item sends one.
OPEN_KIND = "bridge_board_open"
KINDS = frozenset({REFRESH_KIND, OPEN_KIND}) | RETIRED_KINDS
NO_LINK = "找不到它在私聊里的位置，打不开"  # A row whose topic has no link (app/digest.py board_view).
LEAD = 30  # Characters of the most pressing topic's words on the band.
GOAL_CHARS = 60  # Of a project's or a proposal's goal on its grey line.
SHOWN = 8  # Blocks a group shows; the rest is one line.
DONE_SHOWN = 12  # Rows in the folded 做完.
FOLD_SHOWN = 20  # Projects past the blocks, or folded away, listed a line each in their fold; the rest one line.
FIT_SHOWN = (SHOWN, 6, 4, 3, 2, 1)  # Blocks a group shows, tried in turn until the card fits (``board_card``).
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 1500  # Room left under CardKit's limits for what CardKit adds.
WEB = "打开网页"  # The last line's link to the web page (``web``).


def heading(key, count):
    """A group's heading: its state's word (有结果没看 for results), how many, its icon."""
    return {"tag": "markdown", "content": f"**{look.group_word(key)}**  <font color='grey'>{count}</font>",
            "text_size": "normal_v2", "icon": {"tag": "standard_icon", "token": look.STATES[key].icon, "color": "grey"}}


def head_of(row) -> str:
    return " ".join(part for part in (row.get("label"), row.get("title")) if part) or "话题"


def buttons_row(buttons) -> list:
    """A row's buttons side by side, as one element of its right column; [] for none."""
    buttons = [one for one in buttons if one]
    if len(buttons) < 2:
        return buttons
    return [{"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "6px",
             "columns": [{"tag": "column", "width": "auto", "vertical_align": "center", "elements": [one]}
                         for one in buttons]}]


def block(kind, row, buttons=()):
    """One topic as a topic row: its state, ``label title ›``, the line in words, the grey figures; the whole opens
    ``row['url']``. One without a link says why it opens nothing."""
    meta = [*(row.get("meta") or ()), *([NO_LINK] if row.get("topic") and not row.get("url") else [])]
    return ui.topic_row(kind, head_of(row), row.get("line") or "", meta, url=row.get("url") or "",
                        buttons=buttons_row(buttons))


def task_card_button(row):
    """去任务卡: the row's link when it is its task card's (``card``), where 检查并继续 and 看看 are."""
    return [button(TASK_CARD_ACT, tone="primary", url=row["url"])] if row.get("card") and row.get("url") else []


def blocks(kind, rows, buttons=lambda row: (), shown=SHOWN):
    shown_rows = [block(kind, row, buttons(row)) for row in rows[:shown]]
    if len(rows) > shown:
        shown_rows.append(note(f"另有 {len(rows) - shown} 个：" + "、".join(row.get("label") or "话题" for row in rows[shown:])))
    return shown_rows


def tone(row) -> str:
    """The core/look.py STATES key of a row of ``done``: its ``tone`` (app/digest.py collect), else by its status as
    the board read it before (every cancelled one 停住了)."""
    if row.get("tone"):
        return row["tone"]
    if row.get("status") in STOPPED:
        return "stopped"
    return "done" if row.get("seen") else "result"


def groups(view):
    """{waiting, stale, proposals, stopped, stuck, running, result, seen, cancelled}: the rows of each group. What asks
    for him (等你 with ``stale`` the ones waiting over a day and ``proposals`` 整理专项's waiting for his 好, 停住了, 可能卡住:
    ``ACT``) is listed whoever's it is, a live project's topics too, so the count is the 早晚汇总's and 手上的事's
    (core/board_read.py waits, Asks); a live project's other topics are left out (its block counts them)."""
    member = {topic for project in view.get("projects") or () for topic in project.get("topics") or ()}

    def mine(rows):
        return [row for row in rows or () if row.get("topic") not in member]
    done = list(view.get("done") or ())
    running = list(view.get("running") or ())
    return {"waiting": list(view.get("waiting") or ()), "stale": list(view.get("stale") or ()),
            "proposals": list(view.get("pending") or ()),
            "stopped": [row for row in done if tone(row) == "stopped"],
            "stuck": [row for row in running if row.get("stuck")],
            "running": [row for row in mine(running) if not row.get("stuck")],
            "result": [row for row in mine(done) if tone(row) == "result"],
            "seen": [row for row in mine(done) if tone(row) == "done"],
            "cancelled": [row for row in mine(done) if tone(row) == "cancelled"]}


def halted(view) -> list:
    """The projects of ``view`` a guard paused (停住了 · why: app/digest.py projects_view ``halted``)."""
    return [project for project in view.get("projects") or () if project.get("halted")]


def waiting_count(found) -> int:
    """How many wait for him: the 等你 rows, the ones waiting over a day (core/board_read.py Waits.count) and the proposals
    waiting for his 好 (Asks.proposals)."""
    return len(found["waiting"]) + len(found.get("stale") or ()) + len(found.get("proposals") or ())


def state_counts(found, stopped_projects=()) -> dict:
    """{state key: how many} in core/look.py PRESSING's order: the band's segment bar and its most pressing. A project a
    guard paused is 停住了 as a topic that stopped is (core/board_read.py Asks.counts)."""
    return {"waiting": waiting_count(found), "stopped": len(found["stopped"]) + len(stopped_projects),
            "stuck": len(found["stuck"]), "result": len(found["result"]), "running": len(found["running"])}


def project_row(project) -> dict:
    """A project a guard paused as the band's most pressing: 专项「名」, opening its card's topic."""
    return {"label": f"专项「{project.get('name') or ''}」", "url": project.get("url") or ""}


def proposal_lead(item) -> dict:
    """A proposal as the band's most pressing: 提议「名」 (it opens nothing: its own card is elsewhere in the chat)."""
    return {"label": f"提议「{item.get('name') or ''}」"}


def band(found, empty=None, sizes=None, stopped_projects=(), *, label="要你处理", quiet="现在没有在等你的事"):
    """The conclusion band: how many things ask for him (core/board_read.py act_count), the most pressing topic (or
    project, or proposal), every state's count as a bar; the whole opens the most pressing one. ``sizes``: state_counts,
    worked out once by the caller. ``label``, ``quiet``: its small label and its grey line when nothing asks (早晚汇总 says
    when, cards/digest.py)."""
    sizes = sizes if sizes is not None else state_counts(found, stopped_projects)
    asked = board_read.act_count(sizes)
    first = look.pressing(sizes)
    leads = {**found, "stopped": [*found["stopped"], *(project_row(project) for project in stopped_projects)],
             # Only old 等你: the newest of them; only proposals: the newest one.
             "waiting": found["waiting"] or found.get("stale") or [proposal_lead(item) for item in found.get("proposals") or ()]}
    rows = leads.get(first) or [] if first else []
    lead = rows[0] if rows else None
    line = ""
    if lead:
        words = f"{look.word(first)} · {head_of(lead)}"
        line = "先看：" + (words if len(words) <= LEAD else words[:LEAD - 1] + "…")
    headline = ui.number(asked, "件事") if asked else "**都在掌握中**"
    if empty and not asked:  # Not a clear board: no topic yet, or a record from some time ago (ui.empty_words).
        headline, quiet = empty[:2]
    return ui.conclusion_band(label, headline, literal_text(line) if lead and not (empty and not asked) else quiet,
                              below=ui.seg_bar(sizes), url=(lead or {}).get("url") or "")


def counts(found, empty=None, sizes=None):
    """The title bar's tags: (text, color) of the states that have someone, most pressing first, counted as the band
    counts them (state_counts: one that may be stuck is not also 在做)."""
    sizes = sizes if sizes is not None else state_counts(found)
    tags = [(f"{sizes[key]} {look.word(key)}", look.STATES[key].color) for key in ("waiting", "stopped", "stuck", "running")
            if sizes[key]]
    return tags or [((empty[2], "neutral") if empty else ("都在掌握中", "green"))]


def one_line(text, limit=GOAL_CHARS) -> str:
    """``text`` (a goal the lead or the butler wrote, paragraphs and all) as one line of at most ``limit`` characters,
    escaped: its line breaks are spaces, not the literal \\n literal_text writes for them."""
    return literal_text(cut(text, limit))


def stand_words(project) -> str:
    """How a project's topics stand, in the state words: 1 个等你、1 个停住了、2 个在做."""
    return "、".join(f"{n} 个{look.word(key)}" for key, n in (
        ("waiting", project.get("waiting")), ("stopped", project.get("stopped")), ("stuck", project.get("stuck")),
        ("running", project.get("running"))) if n)


def project_state(project) -> str:
    """A project's own state when it is not at work, in its colour: 停住了 · why for a guard's pause, 已停止 for the
    owner's stop; '' otherwise (its topics' words say how it stands)."""
    if project.get("halted"):
        reason = project["halted"] if isinstance(project["halted"], str) else ""
        return ui.state_word("stopped") + (grey(" · " + literal_text(reason)) if reason else "")
    if project.get("cancelled"):
        return ui.state_word("cancelled")
    return ""


def project_block(project):
    """A live project: its name, its own state (``project_state``), its goal and how its topics stand; the whole opens its card."""
    stand, state = stand_words(project), project_state(project)
    left = [md(f"**{literal_text(project.get('name') or '专项')}**" + (f"　{state}" if state else "")
               + (f"　{grey(stand)}" if stand else ""), size="normal_v2")]
    if project.get("goal"):
        left.append(md(grey("目标：" + one_line(project["goal"])), size="notation"))
    return {"tag": "interactive_container", "width": "fill", "padding": "8px 10px 8px 10px", "corner_radius": "8px",
            "background_style": "grey-50", "has_border": True, "border_color": "grey-200", "vertical_spacing": "2px",
            "elements": [body(left, [])], **({"behaviors": [{"type": "open_url", "default_url": project["url"]}]}
                                             if project.get("url") else {})}


def project_line(project):
    """A project past the 看板's blocks, or folded away, in one line: its name (a link to it) and 收起, 停住了 (a guard
    paused it), 已停止 (the owner stopped it) or how its topics stand."""
    name = literal_text(project.get("name") or "专项")
    word = ("收起" if project.get("folded") else look.word("stopped") if project.get("halted")
            else look.word("cancelled") if project.get("cancelled") else stand_words(project) or look.word("running"))
    return md((f"[{name}]({project['url']})" if project.get("url") else name) + " " + grey("· " + word), size="normal_v2")


def revisit_blocks(rows, shown=SHOWN):
    """到点回来看 (app/revisits.py board_rows): each topic that asked to be looked at again, when and what;
    one whose turn is queued already says it is being looked at."""
    drawn = [{"tag": "markdown", "content": f"**Covey 定时回看**  <font color='grey'>{len(rows)}</font>", "text_size": "normal_v2",
              "icon": {"tag": "standard_icon", "token": "time_outlined", "color": "grey"}}]
    for row in rows[:shown]:
        what = f"：{row['look']}" if row.get("look") else ""
        line = f"Covey 正在自动回看{what}" if row.get("fired") else f"Covey 将于 {row.get('at') or ''} 自动回看{what}"
        drawn.append(ui.topic_row("running" if row.get("fired") else "queued", head_of(row), line, url=row.get("url") or ""))
    if len(rows) > shown:
        drawn.append(note(f"另有 {len(rows) - shown} 个：" + "、".join(row.get("label") or "话题" for row in rows[shown:])))
    return drawn


def waiting_part(view, found, shown=SHOWN, *, origin=ORIGIN, answer=True):
    """等你: its heading (every 等我 and proposal, ``waiting_count``), a row each question or task waiting for 开始 with 去回答
    (开始, ``act``) opening its card, the proposals of 整理专项 (``proposal_block``), then the line of the ones waiting over
    a day (its 不管了 while it takes effect) or what 不管了 took off; [] when none of them is there. ``origin``: the
    card's word in the clicks' ``from`` (the 早晚汇总's own); ``answer`` False: the proposals without their 建专项 and 不管了."""
    waiting, proposals = found["waiting"], found.get("proposals") or []
    if not (waiting or proposals or view.get("stale") or view.get("cleared")):
        return []
    elements = [heading("waiting", waiting_count(found))]
    elements += blocks("waiting", waiting, lambda row: [button(row.get("act") or "去回答", tone="primary", url=row["url"])]
                       if row.get("url") else [], shown)
    if proposals:
        elements += [proposal_block(entry, act=answer) for entry in proposals[:shown]]
        if len(proposals) > shown:
            elements.append(note(f"另有 {len(proposals) - shown} 个提议，在私聊里的提议卡上定"))
    if view.get("stale"):
        elements.append(stale_line(view["stale"], button=bool(view.get("dismissable")), origin=origin))
    elif view.get("cleared"):
        elements.append(dismissed_line(view["cleared"], view.get("cleared_taken"), origin))
    return elements


def done_fold(found, since="", shown=SHOWN):
    """做完 and 已停止 folded (the results seen, the owner's own stops): a line each, its T number a link, what it ended as
    and how long it took; ``since`` (a time of day) says from when. [] when there are none."""
    if not (found["seen"] or found["cancelled"]):
        return []
    rows = found["seen"] + found["cancelled"]
    limit = DONE_SHOWN if shown == SHOWN else shown
    lines = [md(topic_ref(row.get("label"), row.get("title"), row.get("url") or "") + " "
                + grey("· " + " · ".join(part for part in (
                    look.word("cancelled") if tone(row) == "cancelled" else row.get("state") or look.word("done"),
                    row.get("took")) if part)))
             for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(note(f"另有 {len(rows) - limit} 个"))
    since = f"{since} 以来" if since else ""
    title = (f"{since}做完的话题 {len(rows)} 个，都看过了" if not found["cancelled"] else
             f"{since}做完 {len(found['seen'])} 个 · {look.word('cancelled')} {len(found['cancelled'])} 个")
    return [collapsible("board_done", title, lines, quiet=True)]


def empty_state(view, found, now=None):
    """ui.empty_words for ``view``: 还没有话题 when it lists nothing and says the owner never had a topic (``first_use``,
    app/digest.py board_view); a record as of its ``stamp`` when ``now`` is more than SNAPSHOT_AGE after it was drawn
    (``at``): the card redrawn from its saved view, 不管了 took a topic off it later. None otherwise."""
    listed = any(found[key] for key in found) or view.get("cleared")
    first = bool(view.get("first_use")) and not listed and not view.get("projects")
    old = bool(now and view.get("at") and now - view["at"] > SNAPSHOT_AGE and view.get("stamp"))
    return ui.empty_words(first=first, when=view.get("stamp") if old else "")


def board_card(view, now=None, notice=""):
    """The 看板 for ``view``; ``now``: given by a redraw from a saved view (not a fresh read), so that a card whose
    figures are old says so when nothing asks (empty_state). ``notice``: what came of the click this redraw follows
    (a stopped row's 检查并继续: app/digest.py Digest.recover), one line under the band; not part of ``view``, so a later
    redraw from it does not say it again. With many rows, fewer a group (FIT_SHOWN) until the card is within what CardKit
    takes (cards/elements.py CARD_NODES, CARD_BYTES); the band counts them all the same."""
    for shown in FIT_SHOWN:
        card = _drawn(view, now, shown, notice)
        if fits(card, NODES, BYTES):
            break
    return card


def _drawn(view, now, shown, notice=""):
    found = groups(view)
    empty = empty_state(view, found, now)
    stopped_projects = halted(view)
    sizes = state_counts(found, stopped_projects)
    elements = [band(found, empty, sizes, stopped_projects)]
    if notice:
        elements.append(ui.receipt(literal_text(notice)))
    elements += waiting_part(view, found, shown)
    if found["stopped"]:
        elements.append(heading("stopped", len(found["stopped"])))
        elements += blocks("stopped", found["stopped"], lambda row: task_card_button(row) + ([button("不管了", tone="text", value={
            "kind": DISMISS, "topics": [row["topic"]], "from": ORIGIN})] if view.get("can_dismiss") and row.get("topic") else []),
            shown)
    if found["stuck"]:  # Right after 停住了 (docs/ui.md 5.1): what may be stuck is not left at the end of 在做.
        elements.append(heading("stuck", len(found["stuck"])))
        elements += blocks("stuck", found["stuck"], task_card_button, shown)
    if found["result"]:
        elements.append(heading("result", len(found["result"])))
        elements += blocks("result", found["result"], shown=shown)
    if found["running"]:
        elements.append(heading("running", len(found["running"])))
        elements += blocks("running", found["running"], shown=shown)
    if view.get("revisits"):
        elements += revisit_blocks(view["revisits"], shown)
    if view.get("tidy") is not None or view.get("projects"):
        projects, pending = view.get("projects") or [], view.get("pending") or []
        # 专项 that exist, counted as the 整理专项 card and the butler's projects_list count them; the proposals waiting for
        # him are 等你, listed under it (``waiting_part``).
        content = f"**专项**  <font color='grey'>{len(projects)} 个</font>"
        elements.append({"tag": "markdown", "content": content, "text_size": "normal_v2",
                         "icon": {"tag": "standard_icon", "token": "folder_outlined", "color": "grey"}})
        # A block each for those on, the first ``shown``; the rest and the ones folded away one line each in a fold, as
        # the list grows (app/project_loop.py: up to MAX_ACTIVE on, and folded ones pile up), at most FOLD_SHOWN of them.
        on = [project for project in projects if not project.get("folded")]
        elements += [project_block(project) for project in on[:shown]]
        rest, folded = on[shown:], [project for project in projects if project.get("folded")]
        if rest or folded:
            title = " · ".join(part for part in (f"另有 {len(rest)} 个专项" if rest else "",
                                                 f"收起的专项 {len(folded)} 个" if folded else "") if part)
            listed = rest + folded
            limit = FOLD_SHOWN if shown == SHOWN else shown * 2
            lines = [project_line(project) for project in listed[:limit]]
            if len(listed) > limit:
                lines.append(note(f"另有 {len(listed) - limit} 个，在私聊里找它们的专项卡"))
            elements.append(collapsible("board_projects", title, lines, quiet=True))
        if view.get("tidy") is not None:
            line = ("等你里的提议点「建专项」，按提议里的话题和名字建，建好后话题可以移出、名字可以改" if pending else
                    "还没有专项，点右边把最近 7 天动过的话题看一遍" if not projects else
                    "把专项和最近 7 天动过的话题重新看一遍")
            elements.append(body([md(grey(line), size="notation")], [button("整理专项", tone="primary", value={"kind": START_KIND})]))
    elements += done_fold(found, view.get("done_since") or "", shown)
    if view.get("stamp"):
        # 刷新 redraws it here; a second click on the menu's 看板 folds it (the owner wanted fresh figures, not a fold).
        # 打开网页 with the web page's address set (``web``).
        elements.append(item(note(f"更新于 {view['stamp']}"), text_button("刷新", {"kind": REFRESH_KIND}),
                             *([link_button(WEB, view["web"])] if view.get("web") else [])))
    tags = counts(found, empty, sizes)
    head = {"title": plain("看板"), "subtitle": plain("私聊里在推进的事"),
            "text_tag_list": [{"tag": "text_tag", "text": plain(text), "color": color} for text, color in tags[:3]],
            "template": "default", "icon": {"tag": "standard_icon", "token": ICON, "color": "blue"},
            "padding": "10px 14px 0px 14px"}
    summary = "看板 · " + " · ".join(text for text, _ in tags)
    return ui.styled(styled_panel(head, elements, summary, spacing="8px"))
