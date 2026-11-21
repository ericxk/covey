"""The bot menu's 看板: what is going on in the owner's private chat now, in the 夜航 look (docs/ui.md 5.2: 菜单「看板」).

The conclusion band first (cards/ui.py conclusion_band): 要你处理 N 件事 (等你, 停住了 and 可能卡住), the most pressing
topic under it (core/look.py pressing), and how many of each state as a segment bar; 都在掌握中 when nothing asks for him.
Then, most pressing first: 等你 → 停住了 → 有结果没看 → 在做 (可能卡住 in its colour) → 专项 (with 整理专项, the one place it
is) → 做完 and 已停止 (folded). Each topic is a topic row (cards/ui.py topic_row): its state's dot and word, its title
with ›, one line in words (what he is asked to decide, why it stopped, what it is doing now) and a grey line of figures;
the whole row opens it (``url``; a topic without one is not a link). Buttons are real: 去回答 (opens the topic's
question, the row's one primary button), 不管了 (a text button: takes a stopped topic off the board, app/overview.py
DISMISS, ``from`` ORIGIN, app/digest.py redraws this card). A topic of a live project is not listed by itself: its
project's block counts it.

With nothing listed it says which kind of empty it is (ui.empty_words, ``empty_state``): 还没有话题 for an owner who never had
one (``first_use``), 某个时刻的记录 for a card redrawn from its saved view long after it was read (``at``, ``now``),
else 都在掌握中.

A cancelled topic is 停住了 unless the owner stopped it himself (core/look.py owner_stopped; docs/ui.md 3): then it is
已停止, background, in the fold. A row's ``tone`` (app/digest.py collect) says which; a view without it reads by its
status, as before.

``view`` is app/digest.py collect's, with ``url`` on each row and, from board_view, ``projects`` [{name, goal, topics,
waiting, stopped, running}], ``proposals`` (open proposal cards) and ``tidy`` (整理专项 on). 早晚汇总 keeps its own card
(cards/digest.py)."""
from __future__ import annotations

from covey.cards import ui
from covey.cards.elements import collapsible, grey, item, md, note, plain, styled_panel, text_button
from covey.cards.overview import DISMISS, dismissed_line, stale_line
from covey.cards.project_proposal import CREATE_KIND, DECLINE_KIND, START_KIND
from covey.core import look
from covey.core.text import cut, literal as literal_text
from covey.core.turn_words import TASK_CARD_ACT

BOARD_FROM = "board"  # As app/project_tidy.py BOARD_FROM: a proposal decided on the 看板.
SNAPSHOT_AGE = 600  # A card redrawn from its saved view this long after it was read is a record, not now (``empty_state``).
ORIGIN = "digest"  # As cards/digest.py: app/overview.py claim hands a 不管了 on this card to app/digest.py.
ICON = "bitablekanban_outlined"
STOPPED = frozenset({"failed", "cancelled", "interrupted"})
# The five states the 看板 shows, as core/look.py STATES has them: (word, colour, block tint).
LOOKS = {key: (look.STATES[key].word, look.STATES[key].color, look.STATES[key].tint)
         for key in ("waiting", "stopped", "result", "running", "stuck")}
ACT = ("waiting", "stopped", "stuck")  # 要你处理: what the band counts.
# The 看板's own buttons (app/digest.py Digest.claim, before topic routing): 刷新 redraws the card from a fresh view in
# place (a second menu click folds it instead), 记为看过 takes a result off 有结果没看 (core/board_read.py mark_seen).
REFRESH_KIND, SEEN_KIND = "bridge_board_refresh", "bridge_board_seen"
# 取消 on a row of 到点回来看 (app/revisits.py, COVEY_REVISIT=1): that topic's revisit taken back (app/digest.py Digest.claim).
REVISIT_KIND = "bridge_board_revisit_cancel"
ALL_SEEN = "全部记为看过"  # 记为看过 on every result row at once: SEEN_KIND with ``topics``, at most SEEN_MAX.
SEEN_MAX = 30
# 去看板 under an answered question (cards/question_cards.py others_row): a new 看板 card, as the bot menu's item sends one.
OPEN_KIND = "bridge_board_open"
KINDS = frozenset({REFRESH_KIND, SEEN_KIND, REVISIT_KIND, OPEN_KIND})
NO_LINK = "找不到它在私聊里的位置，打不开"  # A row whose topic has no link (app/digest.py board_view).
LEAD = 30  # Characters of the most pressing topic's words on the band.
GOAL_CHARS = 60  # Of a project's or a proposal's goal on its grey line.
SHOWN = 8  # Blocks a group shows; the rest is one line.
DONE_SHOWN = 12  # Rows in the folded 做完.


def button(label, *, tone="", value=None, url=""):
    """A row's button: ``tone`` "primary" (its one main button), "text" (giving up on something: 不管了) or bordered."""
    behaviors = [{"type": "open_url", "default_url": url}] if url else [{"type": "callback", "value": value or {}}]
    return {"tag": "button", "text": plain(label), "type": tone or "default", "size": "small", "behaviors": behaviors}


def heading(key, count):
    """A group's heading: its state's word (有结果没看 for results), how many, its icon."""
    return {"tag": "markdown", "content": f"**{look.group_word(key)}**  <font color='grey'>{count}</font>",
            "text_size": "normal_v2", "icon": {"tag": "standard_icon", "token": look.STATES[key].icon, "color": "grey"}}


def body(left, buttons):
    """The text on the left, the buttons on the right, side by side."""
    columns = [{"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "vertical_spacing": "2px",
                "elements": left}]
    if buttons:
        columns.append({"tag": "column", "width": "auto", "vertical_align": "center", "elements": [
            {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "6px",
             "columns": [{"tag": "column", "width": "auto", "elements": [one]} for one in buttons]}]})
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": columns}


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


def blocks(kind, rows, buttons=lambda row: ()):
    shown = [block("stuck" if kind == "running" and row.get("stuck") else kind, row, buttons(row)) for row in rows[:SHOWN]]
    if len(rows) > SHOWN:
        shown.append(note(f"另有 {len(rows) - SHOWN} 个：" + "、".join(row.get("label") or "话题" for row in rows[SHOWN:])))
    return shown


def tone(row) -> str:
    """The core/look.py STATES key of a row of ``done``: its ``tone`` (app/digest.py collect), else by its status as
    the board read it before (every cancelled one 停住了)."""
    if row.get("tone"):
        return row["tone"]
    if row.get("status") in STOPPED:
        return "stopped"
    return "done" if row.get("seen") else "result"


def groups(view):
    """{waiting, stale, stopped, result, running, seen}: the rows of each group, a live project's topics left out but
    from 等你 (``waiting``, and ``stale`` the ones waiting over a day): what waits for him is listed whoever's it is,
    so the count is the 早晚汇总's and 手上的事's (core/board_read.py waits)."""
    member = {topic for project in view.get("projects") or () for topic in project.get("topics") or ()}

    def mine(rows):
        return [row for row in rows or () if row.get("topic") not in member]
    done = mine(view.get("done"))
    return {"waiting": list(view.get("waiting") or ()), "stale": list(view.get("stale") or ()),
            "running": mine(view.get("running")),
            "stopped": [row for row in done if tone(row) == "stopped"],
            "result": [row for row in done if tone(row) == "result"],
            "seen": [row for row in done if tone(row) == "done"],
            "cancelled": [row for row in done if tone(row) == "cancelled"]}


def waiting_count(found) -> int:
    """How many wait for him: the 等你 rows and the ones waiting over a day (core/board_read.py Waits.count)."""
    return len(found["waiting"]) + len(found.get("stale") or ())


def state_counts(found) -> dict:
    """{state key: how many} in core/look.py PRESSING's order: the band's segment bar and its most pressing."""
    stuck = sum(1 for row in found["running"] if row.get("stuck"))
    return {"waiting": waiting_count(found), "stopped": len(found["stopped"]), "stuck": stuck,
            "result": len(found["result"]), "running": len(found["running"]) - stuck}


def band(found, empty=None):
    """The conclusion band: how many things ask for him, the most pressing topic, every state's count as a bar; the
    whole opens the most pressing topic."""
    counts = state_counts(found)
    asked = sum(counts[key] for key in ACT)
    first = look.pressing(counts)
    rows = ([row for row in found["running"] if row.get("stuck")] if first == "stuck" else
            found["running"] if first == "running" else found.get(first) or
            (found.get("stale") if first == "waiting" else None) or []) if first else []  # Only old 等你: the newest of them.
    lead = rows[0] if rows else None
    line = ""
    if lead:
        words = f"{look.word(first)} · {head_of(lead)}"
        line = "先看：" + (words if len(words) <= LEAD else words[:LEAD - 1] + "…")
    headline = ui.number(asked, "件事") if asked else "**都在掌握中**"
    quiet = "现在没有在等你的事"
    if empty and not asked:  # Not a clear board: no topic yet, or a record from some time ago (ui.empty_words).
        headline, quiet = empty[:2]
    return ui.conclusion_band("要你处理", headline, literal_text(line) if lead and not (empty and not asked) else quiet,
                              below=ui.seg_bar(counts), url=(lead or {}).get("url") or "")


def counts(found, empty=None) -> list:
    """The title bar's tags: (text, color) of the states that have someone, most pressing first, counted as the band
    counts them (state_counts: one that may be stuck is not also 在做)."""
    sizes = state_counts(found)
    tags = [(f"{sizes[key]} {look.word(key)}", look.STATES[key].color) for key in ("waiting", "stopped", "stuck", "running")
            if sizes[key]]
    return tags or [((empty[2], "neutral") if empty else ("都在掌握中", "green"))]


def one_line(text, limit=GOAL_CHARS) -> str:
    """``text`` (a goal the lead or the butler wrote, paragraphs and all) as one line of at most ``limit`` characters,
    escaped: its line breaks are spaces, not the literal \\n literal_text writes for them."""
    return literal_text(cut(text, limit))


def project_block(project):
    """A live project: its name, its goal and how its topics stand."""
    stand = "、".join(f"{n} 个{word}" for n, word in ((project.get("waiting"), "等你"), (project.get("stopped"), "停住"),
                                                      (project.get("running"), "在做")) if n)
    left = [md(f"**{literal_text(project.get('name') or '专项')}**" + (f"　{grey(stand)}" if stand else ""), size="normal_v2")]
    if project.get("goal"):
        left.append(md(grey("目标：" + one_line(project["goal"])), size="notation"))
    return {"tag": "interactive_container", "width": "fill", "padding": "8px 10px 8px 10px", "corner_radius": "8px",
            "background_style": "grey-50", "has_border": True, "border_color": "grey-200", "vertical_spacing": "2px",
            "elements": [body(left, [])], **({"behaviors": [{"type": "open_url", "default_url": project["url"]}]}
                                             if project.get("url") else {})}


def proposal_block(item):
    """A proposal waiting for him: its name, its topics (each a link), its goal, and 建专项 / 不管了 that act on it from
    here (app/project_tidy.py BOARD_FROM). Not one clickable block: its topics are links of their own."""
    topics = " · ".join((f"[{literal_text(row.get('label') or '话题')}]({row['url']})" if row.get("url")
                         else literal_text(row.get("label") or "话题")) + f" {literal_text(row.get('title') or '')}"
                        for row in item.get("rows") or ())
    left = [md(f"<text_tag color='violet'>提议</text_tag> **「{literal_text(item.get('name') or '')}」**", size="normal_v2"),
            md(topics, size="normal_v2")]
    if item.get("goal"):
        left.append(md(grey("目标：" + one_line(item["goal"])), size="notation"))
    value = {"proposal": item.get("id") or "", "from": BOARD_FROM}
    block_ = body(left, [button("建专项", tone="primary", value={"kind": CREATE_KIND, **value}),
                         button("不管了", tone="text", value={"kind": DECLINE_KIND, **value})])
    block_.update(background_style="violet-50", margin="0px")
    return block_


def revisit_blocks(rows):
    """到点回来看 (app/revisits.py board_rows): each topic that asked to be looked at again, when and what, 取消 while it waits;
    one whose turn is queued already says it is being looked at."""
    shown = [{"tag": "markdown", "content": f"**到点回来看**  <font color='grey'>{len(rows)}</font>", "text_size": "normal_v2",
              "icon": {"tag": "standard_icon", "token": "time_outlined", "color": "grey"}}]
    for row in rows[:SHOWN]:
        what = f"：{row['look']}" if row.get("look") else ""
        line = f"正在回来看{what}" if row.get("fired") else f"{row.get('at') or ''} 回来看{what}"
        cancel = [] if row.get("fired") else [button("取消", tone="text", value={
            "kind": REVISIT_KIND, "topic": row.get("topic") or "", "set_at": row.get("set_at")})]
        shown.append(ui.topic_row("running" if row.get("fired") else "queued", head_of(row), line, url=row.get("url") or "",
                                  buttons=buttons_row(cancel)))
    if len(rows) > SHOWN:
        shown.append(note(f"另有 {len(rows) - SHOWN} 个：" + "、".join(row.get("label") or "话题" for row in rows[SHOWN:])))
    return shown


def empty_state(view, found, now=None):
    """ui.empty_words for ``view``: 还没有话题 when it lists nothing and says the owner never had a topic (``first_use``,
    app/digest.py board_view); a record as of its ``stamp`` when ``now`` is more than SNAPSHOT_AGE after it was drawn
    (``at``): the card redrawn from its saved view, 不管了 took a topic off it later. None otherwise."""
    listed = any(found[key] for key in found) or view.get("cleared")
    first = bool(view.get("first_use")) and not listed and not view.get("projects")
    old = bool(now and view.get("at") and now - view["at"] > SNAPSHOT_AGE and view.get("stamp"))
    return ui.empty_words(first=first, when=view.get("stamp") if old else "")


def board_card(view, now=None):
    """The 看板 for ``view``; ``now``: given by a redraw from a saved view (not a fresh read), so that a card whose
    figures are old says so when nothing asks (empty_state)."""
    found = groups(view)
    empty = empty_state(view, found, now)
    elements = [band(found, empty)]
    waiting = found["waiting"]
    if waiting or view.get("stale") or view.get("cleared"):
        elements.append(heading("waiting", waiting_count(found)))
        elements += blocks("waiting", waiting, lambda row: [button(row.get("act") or "去回答", tone="primary", url=row["url"])]
                           if row.get("url") else [])
        if view.get("stale"):
            elements.append(stale_line(view["stale"], button=bool(view.get("dismissable")), origin=ORIGIN))
        elif view.get("cleared"):
            elements.append(dismissed_line(view["cleared"], view.get("cleared_taken"), ORIGIN))
    if found["stopped"]:
        elements.append(heading("stopped", len(found["stopped"])))
        elements += blocks("stopped", found["stopped"], lambda row: task_card_button(row) + ([button("不管了", tone="text", value={
            "kind": DISMISS, "topics": [row["topic"]], "from": ORIGIN})] if view.get("can_dismiss") and row.get("topic") else []))
    if found["result"]:
        topics = [row["topic"] for row in found["result"] if row.get("topic")][:SEEN_MAX]
        # 全部记为看过: two or more results cleared with one click and one redraw.
        elements.append(body([heading("result", len(found["result"]))], [button(
            ALL_SEEN, tone="text", value={"kind": SEEN_KIND, "topics": topics})]) if len(topics) > 1
            else heading("result", len(found["result"])))
        elements += blocks("result", found["result"], lambda row: [button("记为看过", tone="text", value={
            "kind": SEEN_KIND, "topic": row["topic"]})] if row.get("topic") else [])
    if found["running"]:
        elements.append(heading("running", len(found["running"])))
        elements += blocks("running", found["running"], lambda row: task_card_button(row) if row.get("stuck") else [])
    if view.get("revisits"):
        elements += revisit_blocks(view["revisits"])
    if view.get("tidy") is not None or view.get("projects"):
        projects, pending = view.get("projects") or [], view.get("pending") or []
        # One count of each, said the same way as the 整理专项 card and the butler's projects_list: 专项 that exist, and
        # proposals waiting for him, decided right here.
        content = f"**专项**  <font color='grey'>{len(projects)} 个</font>" + (
            f"　<text_tag color='violet'>{len(pending)} 个提议等你定</text_tag>" if pending else "")
        elements.append({"tag": "markdown", "content": content, "text_size": "normal_v2",
                         "icon": {"tag": "standard_icon", "token": "folder_outlined", "color": "grey"}})
        elements += [project_block(project) for project in projects]
        elements += [proposal_block(item) for item in pending[:SHOWN]]
        if view.get("tidy") is not None:
            line = ("「建专项」按提议里的话题和名字建，建好后话题可以移出、名字可以改" if pending else
                    "还没有专项，点右边把最近 7 天动过的话题看一遍" if not projects else
                    "把专项和最近 7 天动过的话题重新看一遍")
            elements.append(body([md(grey(line), size="notation")], [button("整理专项", tone="primary", value={"kind": START_KIND})]))
    if found["seen"] or found["cancelled"]:
        rows = found["seen"] + found["cancelled"]
        lines = [md((f"[{literal_text(row.get('label') or '话题')}]({row['url']})" if row.get("url")
                     else literal_text(row.get("label") or "话题")) + f" {literal_text(row.get('title') or '')} "
                    + grey("· " + " · ".join(part for part in (
                        look.word("cancelled") if tone(row) == "cancelled" else row.get("state"), row.get("took")) if part)))
                 for row in rows[:DONE_SHOWN]]
        if len(rows) > DONE_SHOWN:
            lines.append(note(f"另有 {len(rows) - DONE_SHOWN} 个"))
        since = f"{view['done_since']} 以来" if view.get("done_since") else ""
        title = (f"{since}做完的话题 {len(rows)} 个，都看过了" if not found["cancelled"] else
                 f"{since}做完 {len(found['seen'])} 个 · {look.word('cancelled')} {len(found['cancelled'])} 个")
        elements.append(collapsible("board_done", title, lines, quiet=True))
    if view.get("stamp"):
        # 刷新 redraws it here; a second click on the menu's 看板 folds it (the owner wanted fresh figures, not a fold).
        elements.append(item(note(f"更新于 {view['stamp']}"), text_button("刷新", {"kind": REFRESH_KIND})))
    tags = counts(found, empty)
    head = {"title": plain("看板"), "subtitle": plain("私聊里在推进的事"),
            "text_tag_list": [{"tag": "text_tag", "text": plain(text), "color": color} for text, color in tags[:3]],
            "template": "default", "icon": {"tag": "standard_icon", "token": ICON, "color": "blue"},
            "padding": "10px 14px 0px 14px"}
    summary = "看板 · " + " · ".join(text for text, _ in tags)
    return ui.styled(styled_panel(head, elements, summary, spacing="8px"))

