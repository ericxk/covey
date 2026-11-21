"""The 设置 card's 记忆 tab (app/memory_panel.py, COVEY_MEMORY=1): the owner's memory by scope, and what the next turn carries.

One layer under the tab row:

- 下一轮带上什么: a 工作区 row (a settings-list row, cards/launch_config.py setting_row: the workspace whose new topic it
  shows), one line of how many entries and characters an ordinary new topic there carries on the owner's own turn, the room
  each scope used (全局 1,050/1,200 字), a line for each scope that is near its limit or could not carry all (放不下 N 条: the
  dropped count, said here instead of only to the model), and a fold 看原文 with the block itself (app/prompts.py
  memory_block, cut to PREVIEW characters).
- 全局, 工作区 <name>, 专项, 话题: a section each with its entries, the newest first, each one line (你说的 or 它总结的, 常驻,
  where it came from, 这次带不上 for one the next turn leaves out) with 删除 (two steps: 删除, then 确认删除 or 取消) and, for one
  that can be (a principle), 常驻 or 取消常驻. A section shows SHOWN entries and 展开 (共 N 条); the one ``open`` shows a
  page of PAGE with 上一页 / 下一页 / 收起, the others only their heading then.

Each button's value carries its kind, the card's token and the revision drawn. ``view``: revision, notice, workspace, open,
page, confirm (the key of the entry asking 确认删除); ``data``: app/memory_panel.py ``facts``. The lines are cut (TEXT,
WHERE, OTHERS, NAME, at most WORKSPACES workspaces), and a card that would still be too much for CardKit is drawn again
with less (LESS: the block shorter, then one entry a closed section).
"""
from __future__ import annotations

from covey.cards import ui
from covey.cards.elements import (CARD_BYTES, CARD_NODES, element_count, fold, grey, item, json_size, md, note, panel_header,
                                  section, styled_panel, text_button)
from covey.cards.launch_config import setting_row, workspace_setting
from covey.core.text import cut, literal

TITLE = "记忆"
ICON = "member_outlined"
WORKSPACE = "bridge_memory_workspace"  # The 工作区 row's option: the workspace of the preview and the 工作区 section.
OPEN = "bridge_memory_open"  # 展开 / 上一页 / 下一页 / 收起: ``scope`` ('' closes) and ``page``.
DELETE = "bridge_memory_delete"  # 删除: ``key`` of the entry, ``op`` ask, confirm or cancel.
PIN = "bridge_memory_pin"  # 常驻 / 取消常驻: ``key`` of a principle, ``on``.
KINDS = frozenset({WORKSPACE, OPEN, DELETE, PIN})
SHOWN = 2  # Entries a closed section shows.
PAGE = 8  # Entries a page of the open section shows.
PREVIEW = 1800  # Characters of the block 看原文 shows (a card holds 30000 bytes of JSON).
TEXT = 80  # Characters of an entry's line.
NAME = 24  # Characters of a workspace's name on the 工作区 row.
WORKSPACES = 8  # Workspaces the 工作区 row offers, the chosen one first (each option carries its whole name).
WHERE = 20  # Characters of where an entry came from (专项「…」, T12).
OTHERS = 120  # Characters of the line of the other workspaces.
FOOT = ("删掉的下一轮就不带了。标「常驻」的不会因为放不下被挤掉（每组最多 8 条）。全局的只在你自己的一轮带；"
        "工作区的谁在这个工作区开话题都带；代答话题什么都不带。")
EMPTY = "还没有记忆：你在话题里说“以后…”“别再…”这类长期要求，那一轮结束后会记下。"


def entry_line(row) -> str:
    """One entry: who said it, 常驻, where it came from, its text; 这次带不上 when the next turn leaves it out."""
    tags = [grey(row["by"])]
    if row.get("pinned"):
        tags.append("**常驻**")
    if row.get("where"):
        tags.append(grey(literal(cut(row["where"], WHERE))))
    text = literal(cut(row.get("text"), TEXT))
    out = " " + grey("· 这次带不上") if row.get("out") else ""
    return " · ".join(tags) + f"：{text}{out}"


def entry_row(row, view, value):
    key = row["key"]
    if view.get("confirm") == key:
        buttons = [text_button("确认删除", value(DELETE, key=key, op="confirm"), tone="danger"),
                   text_button("取消", value(DELETE, key=key, op="cancel"))]
    else:
        buttons = [text_button("删除", value(DELETE, key=key, op="ask"), tone="danger")]
        if row.get("pinnable"):
            buttons.insert(0, text_button("取消常驻" if row.get("pinned") else "常驻",
                                          value(PIN, key=key, on=not row.get("pinned"))))
    return item(md(entry_line(row)), *buttons)


def preview_elements(preview, token, view, value, limit=PREVIEW):
    """下一轮带上什么: the 工作区 row, the counts, each scope's room and the lines of the ones near or past it, the block."""
    names = list(dict.fromkeys([preview.get("workspace") or "", *(preview.get("workspaces") or [])]))
    names = [name for name in names if name][:WORKSPACES]
    elements = [section("下一轮带上什么")]
    if names:
        setting = workspace_setting("mem_ws", WORKSPACE, preview.get("workspace") or "", names,
                                    labels={name: name if len(name) <= NAME else name[:NAME - 1] + "…" for name in names})
        # The element's ID changes with each draw, so a choice's redraw closes the row (an ID is at most 20 characters).
        elements.append(setting_row(setting, token=token, stamp=f"r{view.get('revision', 1) % 100000}",
                                    value={"revision": view.get("revision", 1)}))
    where = literal(preview.get("workspace") or "")
    lead = f"在工作区 **{where}** 新开话题、你自己的一轮" if where else "新开话题、你自己的一轮"
    elements.append(md(f"{lead}带上 **{preview.get('count', 0)} 条**，约 {preview.get('chars', 0):,} 字"
                       + (f"；另有 {preview['left']} 条放不下" if preview.get("left") else "") + "。"))
    rooms = " ｜ ".join(f"{title} {used:,}/{limit:,} 字" for title, used, limit in preview.get("rooms") or ())
    if rooms:
        elements.append(note(rooms))
    elements += [md(line) for line in preview.get("warnings") or ()]
    text = str(preview.get("text") or "")
    if text:
        shown = text if len(text) <= limit else text[:limit] + "\n…（后面略）"
        elements.append(fold(f"mem_block_r{view.get('revision', 1) % 100000}", md(grey("看原文（模型读到的就是这一块）")),
                             [md("```text\n" + shown.replace("```", "'''") + "\n```")], padding="0px 0px 0px 0px"))
    return elements


def scope_elements(part, view, value, shown_closed=SHOWN):
    """A section: its heading with the count, its rows (SHOWN, a page when open, none while another is open), its buttons."""
    rows, scope = part.get("rows") or [], part["scope"]
    opened = view.get("open") or ""
    heading = section(f"{literal(part['title'])}（{len(rows)}）")
    if not rows:
        return [heading, note(part.get("empty") or "没有")] + ([note(literal(cut(part["others"], OTHERS)))] if part.get("others") else [])
    if opened and opened != scope:
        return [item(heading, text_button("展开", value(OPEN, scope=scope, page=0)))]
    if opened == scope:
        pages = max(1, -(-len(rows) // PAGE))
        page = min(max(int(view.get("page") or 0), 0), pages - 1)
        shown = rows[page * PAGE:(page + 1) * PAGE]
        buttons = []
        if page > 0:
            buttons.append(text_button("上一页", value(OPEN, scope=scope, page=page - 1)))
        if page + 1 < pages:
            buttons.append(text_button("下一页", value(OPEN, scope=scope, page=page + 1)))
        buttons.append(text_button("收起", value(OPEN, scope="", page=0)))
        tail = [item(note(f"第 {page + 1}/{pages} 页"), *buttons)]
    else:
        shown = rows[:shown_closed]
        tail = [item(note(f"另有 {len(rows) - shown_closed} 条"), text_button("展开", value(OPEN, scope=scope, page=0)))] \
            if len(rows) > shown_closed else []
    elements = [heading, *(entry_row(row, view, value) for row in shown), *tail]
    if part.get("others"):
        elements.append(note(literal(cut(part["others"], OTHERS))))
    return elements


# A card that would be too much for CardKit (cards/elements.py CARD_NODES, CARD_BYTES) is drawn again with less: the block
# cut shorter, then one entry a closed section.
LESS = ((PREVIEW, SHOWN), (600, SHOWN), (600, 1))


def memory_card(token, view, data, *, top=()):
    """The tab for ``view`` over ``data`` (app/memory_panel.py facts: preview, parts)."""
    for limit, shown in LESS:
        card = _memory_card(token, view, data, top, limit, shown)
        if element_count(card["body"]["elements"]) <= CARD_NODES and json_size(card) <= CARD_BYTES:
            break
    return card


def _memory_card(token, view, data, top, limit, shown):
    def value(kind, **more):
        return {"kind": kind, "token": token, "revision": view.get("revision", 1), **more}

    elements = [*top]
    if view.get("notice"):
        elements.append(ui.receipt(view["notice"]))
    parts = data.get("parts") or []
    elements += preview_elements(data.get("preview") or {}, token, view, value, limit)
    if not any(part.get("rows") for part in parts):
        elements.append(md(EMPTY))
    for part in parts:
        elements += scope_elements(part, view, value, shown)
    elements.append(note(FOOT))
    total = sum(len(part.get("rows") or ()) for part in parts)
    preview = data.get("preview") or {}
    subtitle = f"共 {total} 条 · 下一轮带 {preview.get('count', 0)} 条"
    return styled_panel(panel_header(ICON, TITLE, subtitle), elements, f"{TITLE} · {subtitle}")
