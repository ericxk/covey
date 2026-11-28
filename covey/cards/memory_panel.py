"""The 设置 card's 记忆 tab (app/memory_panel.py, COVEY_MEMORY=1): the memory's counts, the 待审核 to decide, and the way to the web.

Feishu keeps three things of the memory: looking at the numbers, the light handling of 待审核 (采纳 or 不要, one item at a time),
and the way to the web page's 记忆 (managing it all is there: editing, moving to another scope, 删). One layer under the tab row:

- one line of counts, the header's subtitle: ``规矩：全局 N · 专项 N · 话题 N · 待审核 N`` (``counts_line``);
- 待审核: the oldest items, each its title, one grey line of what kind it is and what each button does (``review_words``) and
  采纳 / 不要, a page of REVIEW_PAGE with 上一页 / 下一页; with the web page on (``url``) one button 打开网页 (a URL button: it
  carries no kind);
- without the web page: the titles of the rules of 全局, the projects and the topics (short code, title, where; 只带标题 marked),
  each with 收起 (two steps: 收起, then 确认收起 or 取消; core/memstore.py archive), a page of RULE_PAGE.

The buttons keep the kinds the tab had (the closed set of kinds is frozen, tests/golden/concepts.json): ``ACCEPT`` is
``bridge_memory_pin`` (it was 常驻), ``DELETE`` is 不要 on a 待审核 item (``key`` ``review:…``, ``op`` reject) and 收起 on a rule
(``op`` ask, confirm or cancel), ``OPEN`` pages. ``WORKSPACE`` and ``PROMOTE`` (and 常驻 as the old ``PIN``) stay in KINDS only to
take the click of a card drawn before: they change nothing and say so (app/memory_panel.py).

Each button's value carries its kind, the card's token and the revision drawn. ``view``: revision, notice, review_page,
rules_page, confirm (the key of the rule asking 确认收起); ``data``: app/memory_panel.py ``facts``. A card that would be too much
for CardKit (cards/elements.py CARD_NODES, CARD_BYTES) is drawn again with fewer rows.
"""
from __future__ import annotations

from covey.cards import ui
from covey.cards.elements import (CARD_BYTES, CARD_NODES, element_count, grey, item, json_size, md, note, panel_header, plain,
                                  section, styled_panel, text_button)
from covey.core import web_sync
from covey.core.text import cut, literal

TITLE = "记忆"
ICON = "member_outlined"
WORKSPACE = "bridge_memory_workspace"  # Offline: the 工作区 row of the older tab. A click says so and changes nothing.
OPEN = "bridge_memory_open"  # 上一页 / 下一页: ``scope`` (review or rules) and ``page``.
DELETE = "bridge_memory_delete"  # 不要 (``key`` review:…, ``op`` reject) and 收起 (``key`` of a rule, ``op`` ask, confirm or cancel).
ACCEPT = "bridge_memory_pin"  # 采纳 a 待审核 item (``key`` review:…, ``op`` accept): the kind that was 常驻, kept so no kind is new.
PIN = ACCEPT  # What the older tab's 常驻 carried (``key`` of a principle, ``on``): offline, answered with a line.
PROMOTE = "bridge_memory_to_global"  # Offline: 升为全局 of the older tab.
KINDS = frozenset({WORKSPACE, OPEN, DELETE, ACCEPT, PROMOTE})
REVIEW = "review"  # OPEN's ``scope`` of the 待审核 list.
RULES = "rules"  # OPEN's ``scope`` of the rules list (without the web page).
REVIEW_PAGE = 5  # Items of 待审核 a page shows.
RULE_PAGE = 8  # Rules a page shows.
TITLE_CHARS = 30  # Characters of an item's or a rule's title.
TEXT = 60  # Characters of an item's line of text.
URL_LABEL = "打开网页"
URL_NOTE = "看全部、改、删、换管哪儿，在网页的「记忆」"
OFFLINE = "这个选项已下线：记忆按范围管，改动到网页的「记忆」"
EMPTY = "还没有记忆：你在话题里说“以后…”“别再…”这类长期要求，那一轮结束后会记下。"


def center_url(code="", scope="") -> str:
    """The web page's 记忆 (app/web_memory.py center_url, the same rule: a card may not import app/): ``page_url`` + /memory,
    with ``?code=<short code>`` opening that entry or ``?scope=<global|topics|p:id>`` that group; '' without the web page."""
    from urllib.parse import quote

    url = web_sync.page_url()
    if not url or "?" in url or "#" in url:
        return url
    query = f"?code={quote(code)}" if code else f"?scope={quote(scope)}" if scope else ""
    return url.rstrip("/") + "/memory" + query


def url_button(label, url):
    """A small borderless button that opens ``url`` (it carries no kind: a click never reaches the Bridge)."""
    return {"tag": "button", "text": plain(label), "type": "text", "size": "small",
            "behaviors": [{"type": "open_url", "default_url": url}]}


def counts_line(data) -> str:
    """``规矩：全局 N · 专项 N · 话题 N · 待审核 N`` of app/memory_panel.py facts (the rules in use by scope, the open items)."""
    by = data.get("rules_by") or {}
    pending = (data.get("counts") or {}).get("pending", 0)
    return (f"规矩：全局 {by.get('global', 0)} · 专项 {by.get('project', 0)} · 话题 {by.get('topic', 0)} · "
            f"待审核 {pending}")


# What an item of 待审核 is and what 采纳 / 不要 do to it (core/memory_review.py decide): 不要 always leaves things as they are.
# The web page's 待审核 says the same (web/client/src/cv/memoryCenter.ts REVIEW_WORDS).
WIDER_WORDS = "推断要管全局 · 现在先在说的地方照做；采纳就挪到全局，不要就留在原处"
GRANT_WORDS = "涉及要你点头的事 · 采纳后才生效，能放行它写明的那类事；不要就不记"
REVIEW_WORDS = {
    ("learn", "global"): "推断要管全局 · 还没生效；采纳后每一轮都照做，不要就不记",
    ("learn", ""): "自动学到的 · 还没生效；采纳后在这里照做，不要就不记",
    ("migrate", "project"): "迁移带过来的负责人约束 · 采纳算你定的，不要就照旧是负责人记的",
    ("migrate", ""): "旧版工作区里的一条 · 采纳后管全局，不要就不带过来",
    ("conflict", ""): "和已有的一条说法不同 · 采纳换成新的说法，不要就留原来的",
}
REVIEW_OTHER = "采纳后生效，不要就保持原样"


def review_words(row) -> str:
    """The grey line of an item of 待审核: what kind it is and what 采纳 and 不要 do. A learned rule held where it was said
    (``items``) is WIDER_WORDS, one that names what only the owner says yes to (``suggest.grant``) GRANT_WORDS, the rest by
    type and scope (REVIEW_WORDS)."""
    kind = str(row.get("type") or "")
    suggest = row.get("suggest") if isinstance(row.get("suggest"), dict) else {}
    scope = str(suggest.get("scope") or "")
    if kind == "learn" and suggest.get("grant"):
        return GRANT_WORDS
    if kind == "learn" and row.get("items"):
        return WIDER_WORDS
    return REVIEW_WORDS.get((kind, scope)) or REVIEW_WORDS.get((kind, "")) or REVIEW_OTHER


def pager(scope, page, pages, value):
    buttons = []
    if page > 0:
        buttons.append(text_button("上一页", value(OPEN, scope=scope, page=page - 1)))
    if page + 1 < pages:
        buttons.append(text_button("下一页", value(OPEN, scope=scope, page=page + 1)))
    return item(note(f"第 {page + 1}/{pages} 页"), *buttons)


def page_of(rows, view, field, size):
    pages = max(1, -(-len(rows) // size))
    page = min(max(int(view.get(field) or 0), 0), pages - 1)
    return rows[page * size:(page + 1) * size], page, pages


def review_elements(review, view, value, data, size=REVIEW_PAGE):
    """待审核: the oldest items, each with what it is and what the buttons do (``review_words``), 采纳 and 不要."""
    elements = [section(f"待审核（{len(review)}）")]
    if not review:
        return [*elements, note("没有待审核的")]
    shown, page, pages = page_of(review, view, "review_page", size)
    for row in shown:
        key = str(row.get("key") or "")
        line = f"**{literal(cut(row.get('title') or row.get('text') or '', TITLE_CHARS))}**"
        text = literal(cut(row.get("text") or "", TEXT))
        if text and text != literal(cut(row.get("title") or "", TITLE_CHARS)):
            line += "\n" + text
        line += "\n" + grey(review_words(row))
        elements.append(item(md(line, size="normal_v2"), text_button("采纳", value(ACCEPT, key=key, op="accept")),
                             text_button("不要", value(DELETE, key=key, op="reject"))))
    if pages > 1:
        elements.append(pager(REVIEW, page, pages, value))
    return elements


def rule_line(row, titles) -> str:
    if row.get("scope") == "topic":
        where = f"话题 T{row['topic_number']}" if type(row.get("topic_number")) is int else "话题"
    else:
        where = "全局" if row.get("scope") == "global" else titles.get(f"p:{row.get('project_key')}", "专项")
    over = " " + grey("· 只带标题") if row.get("over_cap") else ""
    # No short code before it (T969): the owner reads the title; the code is the tools' and the page's.
    return f"{literal(cut(row.get('title') or '', TITLE_CHARS))} {grey('· ' + literal(cut(where, 12)))}{over}"


def rule_elements(rules, scopes, view, value, size=RULE_PAGE):
    """Without the web page: the titles of the rules in use (全局, the projects', the topics'), each 收起 (two steps)."""
    elements = [section(f"规矩（{len(rules)}）")]
    if not rules:
        return [*elements, note("没有")]
    titles = {str(row.get("key")): str(row.get("title") or "") for row in scopes or ()}
    shown, page, pages = page_of(rules, view, "rules_page", size)
    for row in shown:
        key = str(row.get("key") or "")
        if view.get("confirm") == key:
            buttons = [text_button("确认收起", value(DELETE, key=key, op="confirm"), tone="danger"),
                       text_button("取消", value(DELETE, key=key, op="cancel"))]
        else:
            buttons = [text_button("收起", value(DELETE, key=key, op="ask"))]
        elements.append(item(md(rule_line(row, titles), size="normal_v2"), *buttons))
    if pages > 1:
        elements.append(pager(RULES, page, pages, value))
    return elements


# A card too much for CardKit is drawn again with fewer rows: (items of 待审核, rules).
LESS = ((REVIEW_PAGE, RULE_PAGE), (3, 4), (2, 2))


def memory_card(token, view, data, *, top=()):
    """The tab for ``view`` over ``data`` (app/memory_panel.py facts)."""
    for review, rules in LESS:
        card = _memory_card(token, view, data, top, review, rules)
        if element_count(card["body"]["elements"]) <= CARD_NODES and json_size(card) <= CARD_BYTES:
            break
    return card


def _memory_card(token, view, data, top, review_size, rule_size):
    def value(kind, **more):
        return {"kind": kind, "token": token, "revision": view.get("revision", 1), **more}

    elements = [*top]
    if view.get("notice"):
        elements.append(ui.receipt(view["notice"]))
    counts = data.get("counts") or {}
    if data.get("error"):
        elements.append(md(literal(data["error"])))
    elif not any(counts.get(name) for name in ("rule", "decision", "note", "pending")):
        elements.append(md(EMPTY))
    subtitle = counts_line(data)
    if data.get("url"):
        elements.append(item(note(URL_NOTE), url_button(URL_LABEL, data["url"])))
    elements += review_elements(data.get("review") or [], view, value, data, review_size)
    if not data.get("url"):
        elements += rule_elements(data.get("rules") or [], data.get("scopes"), view, value, rule_size)
    return styled_panel(panel_header(ICON, TITLE, subtitle), elements, f"{TITLE} · {subtitle}")
