"""The bot menu's 看板: what is going on in the owner's private chat now, one coloured block a topic (the owner,
2026-11-18 21:09/21:20: 文字只通过蓝色来代表能点击，感觉差点意思; 样张 A 彩色块).

From the top, what asks the most of him first: 等你 (orange) → 停住了 (red) → 有结果没看 (green) → 在做 (blue; 可能卡住
orange) → 专项 (grey, with 整理专项, the one place it is) → 做完 (folded). Each topic is a framed block the whole of which
opens it (``url``; a topic without one is not a link), a status pill, its title with ›, one line in words (what he is
asked to decide, why it stopped, what it is doing now) and a grey line of figures (how long it waited, when it stopped,
its steps, how long is left, when it last moved). Buttons are real, bordered: 去回答 (opens the topic's question),
不管了 (takes a stopped topic off the board: app/overview.py DISMISS, ``from`` ORIGIN, app/digest.py redraws this
card). A topic of a live project is not listed by itself: its project's block counts it.

``view`` is app/digest.py collect's, with ``url`` on each row and, from board_view, ``projects`` [{name, goal, topics,
waiting, stopped, running}], ``proposals`` (open proposal cards) and ``tidy`` (整理专项 on). 早晚汇总 keeps its own card
(cards/digest.py)."""
from __future__ import annotations

from covey.cards.elements import collapsible, grey, md, note, plain, styled_panel
from covey.cards.overview import DISMISS, dismissed_line, stale_line
from covey.cards.project_proposal import CREATE_KIND, DECLINE_KIND, START_KIND
from covey.core.text import literal as literal_text

BOARD_FROM = "board"  # As app/project_tidy.py BOARD_FROM: a proposal decided on the 看板.
ORIGIN = "digest"  # As cards/digest.py: app/overview.py claim hands a 不管了 on this card to app/digest.py.
ICON = "bitablekanban_outlined"
STOPPED = frozenset({"failed", "cancelled", "interrupted"})
LOOKS = {"waiting": ("等你", "orange", "orange-50"), "stopped": ("停住了", "red", "red-50"),
         "result": ("有结果", "green", "green-50"), "running": ("在做", "blue", "blue-50"),
         "stuck": ("可能卡住", "orange", "orange-50")}
GROUPS = (("waiting", "等你", "time_outlined"), ("stopped", "停住了", "warning_outlined"),
          ("result", "有结果没看", "yes_outlined"), ("running", "在做", "loading_outlined"))
SHOWN = 8  # Blocks a group shows; the rest is one line.
DONE_SHOWN = 12  # Rows in the folded 做完.


def pill(kind) -> str:
    text, color, _ = LOOKS[kind]
    return f"<text_tag color='{color}'>{text}</text_tag>"


def button(label, *, tone="", value=None, url=""):
    behaviors = [{"type": "open_url", "default_url": url}] if url else [{"type": "callback", "value": value or {}}]
    return {"tag": "button", "text": plain(label), "type": tone or "default", "size": "small", "behaviors": behaviors}


def heading(title, count, icon):
    return {"tag": "markdown", "content": f"**{title}**  <font color='grey'>{count}</font>", "text_size": "normal_v2",
            "icon": {"tag": "standard_icon", "token": icon, "color": "grey"}}


def body(left, buttons):
    """The text on the left, the buttons on the right, side by side."""
    columns = [{"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "vertical_spacing": "2px",
                "elements": left}]
    if buttons:
        columns.append({"tag": "column", "width": "auto", "vertical_align": "center", "elements": [
            {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "6px",
             "columns": [{"tag": "column", "width": "auto", "elements": [one]} for one in buttons]}]})
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": columns}


def block(kind, row, buttons=()):
    """One topic: its pill, ``label title ›``, the line in words, the grey figures; the whole opens ``row['url']``."""
    _, _, tint = LOOKS[kind]
    url = row.get("url") or ""
    head = " ".join(part for part in (row.get("label"), row.get("title")) if part) or "话题"
    left = [md(f"{pill(kind)} **{literal_text(head)}**" + ("　<font color='grey'>›</font>" if url else ""), size="normal_v2")]
    if row.get("line"):
        left.append(md(literal_text(row["line"]), size="normal_v2"))
    meta = " · ".join(part for part in row.get("meta") or () if part)
    if meta:
        left.append(md(grey(literal_text(meta)), size="notation"))
    box = {"tag": "interactive_container", "width": "fill", "padding": "8px 10px 8px 10px", "corner_radius": "8px",
           "background_style": tint, "has_border": True, "border_color": "grey-200", "vertical_spacing": "2px",
           "elements": [body(left, list(buttons))]}
    if url:
        box["behaviors"] = [{"type": "open_url", "default_url": url}]
    return box


def blocks(kind, rows, buttons=lambda row: ()):
    shown = [block("stuck" if kind == "running" and row.get("stuck") else kind, row, buttons(row)) for row in rows[:SHOWN]]
    if len(rows) > SHOWN:
        shown.append(note(f"另有 {len(rows) - SHOWN} 个：" + "、".join(row.get("label") or "话题" for row in rows[SHOWN:])))
    return shown


def groups(view):
    """{waiting, stopped, result, running, seen}: the rows of each group, a live project's topics left out."""
    member = {topic for project in view.get("projects") or () for topic in project.get("topics") or ()}

    def mine(rows):
        return [row for row in rows or () if row.get("topic") not in member]
    done = mine(view.get("done"))
    return {"waiting": mine(view.get("waiting")), "running": mine(view.get("running")),
            "stopped": [row for row in done if row.get("status") in STOPPED],
            "result": [row for row in done if row.get("status") not in STOPPED and not row.get("seen")],
            "seen": [row for row in done if row.get("status") not in STOPPED and row.get("seen")]}


def counts(found) -> list:
    """The title bar's tags: (text, color) of the groups that have someone, most pressing first."""
    tags = [(f"{len(found[key])} {LOOKS[key][0]}", LOOKS[key][1]) for key in ("waiting", "stopped", "running") if found[key]]
    return tags or [("都在掌握中", "green")]


def project_block(project):
    """A live project: its name, its goal and how its topics stand."""
    stand = "、".join(f"{n} 个{word}" for n, word in ((project.get("waiting"), "等你"), (project.get("stopped"), "停住"),
                                                      (project.get("running"), "在做")) if n)
    left = [md(f"**{literal_text(project.get('name') or '专项')}**" + (f"　{grey(stand)}" if stand else ""), size="normal_v2")]
    if project.get("goal"):
        left.append(md(grey("目标：" + literal_text(project["goal"])), size="notation"))
    return {"tag": "interactive_container", "width": "fill", "padding": "8px 10px 8px 10px", "corner_radius": "8px",
            "background_style": "grey-50", "has_border": True, "border_color": "grey-200", "vertical_spacing": "2px",
            "elements": [body(left, [])], **({"behaviors": [{"type": "open_url", "default_url": project["url"]}]}
                                             if project.get("url") else {})}


def proposal_block(item):
    """A proposal waiting for him: its name, its topics (each a link), its goal, and 建专项 / 暂不考虑 that act on it from
    here (app/project_tidy.py BOARD_FROM). Not one clickable block: its topics are links of their own."""
    topics = " · ".join((f"[{literal_text(row.get('label') or '话题')}]({row['url']})" if row.get("url")
                         else literal_text(row.get("label") or "话题")) + f" {literal_text(row.get('title') or '')}"
                        for row in item.get("rows") or ())
    left = [md(f"<text_tag color='violet'>提议</text_tag> **「{literal_text(item.get('name') or '')}」**", size="normal_v2"),
            md(topics, size="normal_v2")]
    if item.get("goal"):
        left.append(md(grey("目标：" + literal_text(item["goal"])), size="notation"))
    value = {"proposal": item.get("id") or "", "from": BOARD_FROM}
    block_ = body(left, [button("建专项", tone="primary", value={"kind": CREATE_KIND, **value}),
                         button("暂不考虑", value={"kind": DECLINE_KIND, **value})])
    block_.update(background_style="violet-50", margin="0px")
    return block_


def board_card(view):
    found = groups(view)
    elements = []
    waiting = found["waiting"]
    if waiting or view.get("stale") or view.get("cleared"):
        elements.append(heading("等你", len(waiting), "time_outlined"))
        elements += blocks("waiting", waiting, lambda row: [button("去回答", tone="primary", url=row["url"])] if row.get("url") else [])
        if view.get("stale"):
            elements.append(stale_line(view["stale"], button=bool(view.get("dismissable")), origin=ORIGIN))
        elif view.get("cleared"):
            elements.append(dismissed_line(view["cleared"]))
    if found["stopped"]:
        elements.append(heading("停住了", len(found["stopped"]), "warning_outlined"))
        elements += blocks("stopped", found["stopped"], lambda row: [button("不管了", value={
            "kind": DISMISS, "topics": [row["topic"]], "from": ORIGIN})] if view.get("can_dismiss") and row.get("topic") else [])
    if found["result"]:
        elements.append(heading("有结果没看", len(found["result"]), "yes_outlined"))
        elements += blocks("result", found["result"])
    if found["running"]:
        elements.append(heading("在做", len(found["running"]), "loading_outlined"))
        elements += blocks("running", found["running"])
    if view.get("tidy") is not None or view.get("projects"):
        projects, pending = view.get("projects") or [], view.get("pending") or []
        # One count of each, said the same way as the 整理专项 card and the butler's projects_list: 专项 that exist, and
        # proposals waiting for him, decided right here (the owner, 2026-11-18 23:17: 怎么可能没有专项呢).
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
    if found["seen"]:
        rows = found["seen"]
        lines = [md(f"{literal_text(row.get('label') or '话题')} {literal_text(row.get('title') or '')} "
                    + grey("· " + " · ".join(part for part in (row.get("state"), row.get("took")) if part)))
                 for row in rows[:DONE_SHOWN]]
        if len(rows) > DONE_SHOWN:
            lines.append(note(f"另有 {len(rows) - DONE_SHOWN} 个"))
        since = f"{view['done_since']} 以来" if view.get("done_since") else ""
        elements.append(collapsible("board_done", f"{since}做完的话题 {len(rows)} 个，都看过了", lines, quiet=True))
    if not elements:
        elements.append(md(grey("现在没有在等你、在做或停住的事。")))
    if view.get("stamp"):
        elements.append(note(f"更新于 {view['stamp']}"))
    tags = counts(found)
    head = {"title": plain("看板"), "subtitle": plain("私聊里在推进的事"),
            "text_tag_list": [{"tag": "text_tag", "text": plain(text), "color": color} for text, color in tags[:3]],
            "template": "default", "icon": {"tag": "standard_icon", "token": ICON, "color": "blue"},
            "padding": "10px 14px 0px 14px"}
    summary = "看板 · " + " · ".join(text for text, _ in tags)
    return styled_panel(head, elements, summary, spacing="8px")

