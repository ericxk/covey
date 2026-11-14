"""Schema 2.0 card skeletons and the buttons several cards share.

Key order is part of what CardKit receives, so every builder here keeps the order the cards
were first sent with: config (width_mode, update_multi, summary), then body (padding,
vertical_spacing, elements)."""
from __future__ import annotations

import json


def card(elements, summary=None, *, default_width=False, update_multi=False, padding="", spacing="", header=None):
    """A schema 2.0 card around ``elements``; ``summary`` is the chat-list preview; ``header``, a (template,
    title) title bar, or a whole header as panel_header draws it."""
    config = {}
    if default_width:
        config["width_mode"] = "default"
    if update_multi:
        config["update_multi"] = True
    if summary is not None:
        config["summary"] = {"content": summary}
    body = {}
    if padding:
        body["padding"] = padding
    if spacing:
        body["vertical_spacing"] = spacing
    body["elements"] = elements
    if isinstance(header, dict):
        bar = {"header": header}
    else:
        bar = {"header": {"template": header[0], "title": {"tag": "plain_text", "content": header[1]}}} if header else {}
    return {"schema": "2.0", **({"config": config} if config else {}), **bar, "body": body}


def panel_card(elements, summary, *, spacing=""):
    """A padded default-width card: the look of the launch, picker, switch and notice cards."""
    return card(elements, summary, default_width=True, padding="12px", spacing=spacing)


# The bot menu's panels (用量, 工作区, 代答, 脱敏, 群聊; 2026-11-03 redesign): a white title bar with the
# panel's blue line icon, a grey subtitle and up to three state tags; grey tiles for the figures; rows of
# a line and text buttons as wide as their labels; small grey print last. Icons are Feishu's standard
# icon tokens (open.feishu.cn 卡片图标库).
PANEL_ICONS = {"usage": "insert-chart_outlined", "workspaces": "folder_outlined", "proxy": "chat_outlined",
               "redaction": "safe_outlined", "groups": "group_outlined",
               # The launch card, the cards it becomes and the directory browser shown in its place.
               "launch": "start_outlined", "submitted": "yes_outlined", "preparing": "waiting_outlined",
               "directory": "folder_outlined",
               # The 换配置 card (cards/switch_form.py) and the 回退 card (cards/rewind.py).
               "switch": "replace_outlined", "rewind": "undo_outlined"}


def panel_header(icon, title, subtitle="", tags=(), *, color="blue"):
    """A panel's title bar: ``icon`` a PANEL_ICONS key or token, ``tags`` (text, color) pairs."""
    head = {"title": plain(title)}
    if subtitle:
        head["subtitle"] = plain(subtitle)
    if tags:
        head["text_tag_list"] = [{"tag": "text_tag", "text": plain(text), "color": color} for text, color in tags[:3]]
    head["template"] = "default"
    head["icon"] = {"tag": "standard_icon", "token": PANEL_ICONS.get(icon, icon), "color": color}
    head["padding"] = "10px 14px 0px 14px"
    return head


def styled_panel(head, elements, summary, *, spacing="6px"):
    """A menu panel: ``head`` from panel_header over ``elements``. Tight (2026-11-03, the owner: 再紧凑一些):
    6px between elements, 14px at the sides."""
    return card(elements, summary, default_width=True, update_multi=True, padding="6px 14px 12px 14px",
                spacing=spacing, header=head)


def folded_panel(icon, title, item, top=()):
    """A menu panel folded in place to its title bar and one grey line (the owner, 2026-11-06: 点一下弹出，再点一下
    收回): ``top`` (the 设置 card's tab row) above the line, which says what unfolds it, ``item`` the menu's word for
    it (a quoted 「…」 label, or several)."""
    return styled_panel(panel_header(icon, title), [*top, note(f"已收起 · 再点{item}展开")], f"{title} · 已收起")


def menu_item(label):
    """``label`` as the bot menu's item: 菜单「label」."""
    return f"菜单「{label}」"


def tag(text, color="neutral"):
    """A coloured tag inside a markdown line."""
    return f"<text_tag color='{color}'>{text}</text_tag>"


def grey(text):
    return f"<font color='grey'>{text}</font>"


def tiles(*cells, filled=True, flex="none"):
    """A row of tiles, each (label, value, note): the label and note on one small grey line, the value large and
    bold under it. ``filled``: on grey blocks (the panels); else bare figures (the task card). ``flex``: the row's
    flex_mode on a narrow screen (bisect: two by two)."""
    columns = []
    for label, value, note in cells:
        lines = [md(grey(label + (f" · {note}" if note else "")), size="notation"), md(f"**{value}**", size="heading")]
        look = {"background_style": "grey-50", "padding": "6px 10px 6px 10px"} if filled else {"padding": "0px"}
        columns.append({"tag": "column", "width": "weighted", "weight": 1, **look,
                        "vertical_spacing": "0px", "elements": lines})
    return {"tag": "column_set", "flex_mode": flex, "horizontal_spacing": "6px", "columns": columns}


def section(title, icon=""):
    """A part's bold heading, a grey line icon before it when ``icon``."""
    element = md(f"**{title}**")
    if icon:
        element["icon"] = {"tag": "standard_icon", "token": icon, "color": "grey"}
    return element


def note(text):
    """Small grey print."""
    return md(grey(text), size="notation")


# The buttons of a topic's cards (the task card, 补充回执, 暂存的补充, 重试): bordered and medium, 32 px high on a
# computer and 36 px on the phone, 8 px apart, each as wide as its label (the owner, 2026-11-04 14:05: A 中号线框).
# The small borderless ones were 28 px and only their text: 容易点不到或者误触，特别是手机上. A fixed width cut
# 补充当前任务 to 补充当… in a topic's side panel (a 272 px card). The menu panels keep text_button.
BUTTON_SPACING = "8px"
BUTTON_TYPES = {"": "default", "primary": "primary", "danger": "danger"}


def button_look(tone=""):
    """A topic card button's type and size: ``tone`` "primary" (blue border), "danger" (red) or plain (grey)."""
    return {"type": BUTTON_TYPES[tone], "size": "medium"}


def text_button(label, value, *, tone="", disabled=None):
    """A small borderless button for a row: ``tone`` "primary" (blue), "danger" (red) or plain."""
    return callback_button(label, value, style=f"{tone}_text" if tone else "text", disabled=disabled, size="small")


def text_submit(name, text, *, tone="", disabled=False):
    """text_button for a form: a small borderless submit button named ``name``."""
    return {"tag": "button", "name": name, "form_action_type": "submit", "text": plain(text),
            "type": f"{tone}_text" if tone else "text", "size": "small", "disabled": disabled}


def item(line, *buttons):
    """A list row: ``line`` (an element) takes the width, each button only its label's."""
    columns = [{"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [line]}]
    columns += [{"tag": "column", "width": "auto", "vertical_align": "center", "elements": [button]} for button in buttons]
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "4px", "columns": columns}


def notice_card(text, summary):
    """One padded markdown block: a closed form, or a stand-in where a richer card cannot go."""
    return panel_card([{"tag": "markdown", "content": text}], summary)


def form_button(name, label, *, primary=False, disabled=False):
    """A full-width submit button of a card form; ``name`` routes the click."""
    return {"tag": "button", "name": name, "form_action_type": "submit", "text": {"tag": "plain_text", "content": label},
            "type": "primary_filled" if primary else "default", "width": "fill", "disabled": disabled}


def column_button(label, value):
    """A button (button_look) alone in a weighted column of a column_set."""
    return {"tag": "column", "width": "weighted", "elements": [topic_button(label, value=value)]}


def plain(text):
    """A plain_text object: a button's label, a placeholder, an option's text."""
    return {"tag": "plain_text", "content": text}


def md(content, *, element_id=None, size=None):
    """A markdown element; ``size`` is its text_size (notation for the grey small print)."""
    element = {"tag": "markdown"}
    if element_id:
        element["element_id"] = element_id
    if size:
        element["text_size"] = size
    element["content"] = content
    return element


def callback_button(label, value, *, style=None, width=None, disabled=None, size=None):
    """A button whose click calls back with ``value``; an option left None is left out of the JSON."""
    element = {"tag": "button", "text": plain(label)}
    for key, option in (("type", style), ("width", width), ("disabled", disabled)):
        if option is not None:
            element[key] = option
    element["behaviors"] = [{"type": "callback", "value": value}]
    if size is not None:
        element["size"] = size
    return element


def row(*cells, flex="stretch", align=None):
    """A column_set of (element, weight) cells; weight None makes a column as wide as its element.
    ``flex`` "stretch" stacks the columns on a narrow screen, so no label is cut to "…"; None leaves
    out the flex mode and the spacing."""
    columns = []
    for element, weight in cells:
        column = {"tag": "column", "width": "auto" if weight is None else "weighted"}
        if weight is not None:
            column["weight"] = weight
        if align:
            column["vertical_align"] = align
        column["elements"] = [element]
        columns.append(column)
    return {"tag": "column_set", **({"flex_mode": flex, "horizontal_spacing": "8px"} if flex else {}),
            "columns": columns}


def redaction_tags(redaction):
    """The 敏感词脱敏 tag of a title bar (panel_header's ``tags``): 开 or 关 for a topic whose model can redact
    (``redaction`` True or False), none for another (None). With only 开 shown, an unshown 关 read as not knowing
    (the owner, 2026-11-04 16:45: 连是否敏感都不知道)."""
    return [("敏感词脱敏：开", "green")] if redaction is True else [("敏感词脱敏：关", "neutral")] if redaction is False else []


# Folds: a collapsible_panel the client opens and closes itself, keeping it open or closed across a redraw of the
# same element ID. Two looks, each with the key order its cards were first sent with: ``collapsible``, framed, its
# title and chevron centred; ``fold``, unframed, a markdown title. Both chevrons turn up when open.
def chevron(color="grey"):
    return {"tag": "standard_icon", "token": "down-small-ccm_outlined", "color": color, "size": "16px 16px"}


def collapsible(element_id, title, elements, *, expanded=False, boxed=False, quiet=False, size="normal_v2",
                border=True, icon_position="right", padding=None, spacing=None):
    """A framed fold of ``elements`` under ``title``. ``boxed``: the task card's (本轮请求, 任务详情, 执行过程), white in
    a thin grey rounded border with a markdown title and a grey chevron (the owner's pick of the second colour round,
    "E 白底细边框", 2026-11-04). Else a grey border (none without ``border``) and a plain-text title of ``size``,
    small and grey when ``quiet``: beside what the card says (怎么回答, 产物, a note's rest)."""
    panel = {"tag": "collapsible_panel", "element_id": element_id, "expanded": expanded}
    if boxed:
        panel["border"] = {"color": "grey", "corner_radius": "8px"}
        head = {"tag": "markdown", "content": title}
    else:
        head = {"tag": "plain_text", "content": title, "text_size": "notation" if quiet else size,
                "text_color": "grey" if quiet else "default"}
    panel["header"] = {"title": head, "vertical_align": "center", "icon": chevron("grey" if boxed else ""),
                       "icon_position": icon_position, "icon_expanded_angle": -180}
    if border and not boxed:
        panel["border"] = {"color": "grey", "corner_radius": "5px"}
    panel["vertical_spacing"] = spacing or ("6px" if boxed else "8px")
    panel["padding"] = padding or ("4px 12px 6px 12px" if boxed else "8px 8px 8px 8px")
    panel["elements"] = elements
    return panel


def markdown_panel(element_id, title, content, *, content_id="", size="normal_v2", **look):
    """A framed fold of one markdown text of ``size`` (collapsible's ``look``; the title's size too unless quiet or
    boxed); ``content_id``: the text's own element ID, to replace it alone."""
    text = {"tag": "markdown", **({"element_id": content_id} if content_id else {}), "text_size": size, "content": content}
    return collapsible(element_id, title, [text], size=size, **look)


def fold(element_id, title, elements, *, expanded=False, padding="0px 0px 0px 0px", spacing="4px", title_padding="",
         icon_position="right", background=""):
    """An unframed fold of ``elements`` under ``title``, a markdown element, with a grey chevron: 执行过程's folds of
    earlier operations and records (the chevron after the title, "follow_text": these were first sent with the icon
    last), the launch card's 调整 panel and its settings rows."""
    panel = {"tag": "collapsible_panel", "element_id": element_id, "expanded": expanded,
             **({"background_color": background} if background else {}), "padding": padding, "vertical_spacing": spacing}
    header = {"title": title, **({"padding": title_padding} if title_padding else {})}
    turn = {"icon_position": icon_position, "icon_expanded_angle": -180}
    header.update({**turn, "icon": chevron()} if icon_position == "follow_text" else {"icon": chevron(), **turn})
    panel.update(header=header, elements=elements)
    return panel


def markdown_blocks(lines, limit=3000):
    """``lines`` as small markdown elements of at most ``limit`` bytes of JSON each (a longer line alone): one card
    node for several lines."""
    blocks, block = [], []
    for line in lines:
        if block and json_size("\n".join(block + [line])) > limit:
            blocks.append(block)
            block = []
        block.append(line)
    if block:
        blocks.append(block)
    return [md("\n".join(block), size="notation") for block in blocks]


# What CardKit takes in a card: 200 tagged nodes, every plain_text included (300305), and about 30 KB of JSON.
CARD_NODES, CARD_BYTES = 200, 30000


def element_count(value):
    """The CardKit nodes in ``value``: every dict with a tag, at any depth."""
    if isinstance(value, dict):
        return int("tag" in value) + sum(element_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(element_count(v) for v in value)
    return 0


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def fits(elements, nodes, size):
    """Whether ``elements`` keeps within ``nodes`` CardKit nodes and ``size`` bytes of JSON."""
    return element_count(elements) <= nodes and json_size(elements) <= size


# Walking a card's body: the elements at any depth, an element's ``elements`` before its ``columns`` and theirs (a
# column itself counts as one: an element ID on it is found too).
def walk(elements):
    """(the list holding it, its index there, the element) for every dict in ``elements`` at any depth, parents
    first."""
    stack = [(elements, 0)]
    while stack:
        holder, index = stack.pop()
        if index >= len(holder):
            continue
        stack.append((holder, index + 1))
        item = holder[index]
        if not isinstance(item, dict):
            continue
        yield holder, index, item
        children = [item.get("elements"), item.get("columns")]
        for child in reversed(children):
            if isinstance(child, list):
                stack.append((child, 0))


def find_element(card, element_id):
    """The element of ``card``'s body with ``element_id`` at any depth (the 输出 text sits in the 输入/输出 box), None
    when there is none."""
    return next((item for _, _, item in walk(card.get("body", {}).get("elements", ()))
                 if item.get("element_id") == element_id), None)


def replace_element(built, element_id, element) -> bool:
    """Put ``element`` in place of the one with ``element_id`` in the card ``built`` (at any depth); whether one was
    there."""
    for holder, index, item in walk(((built or {}).get("body") or {}).get("elements") or []):
        if item.get("element_id") == element_id:
            holder[index] = element
            return True
    return False


# The topic cards' buttons (``button_look``) and the row they sit in, each with the key order its cards were sent
# with: a card's JSON is compared as a string before a write (app/cards.py), so a reordered key writes again.
def topic_button(label, *, element_id="", tone="", value=None, behaviors=None):
    """A bordered medium button (``button_look``): ``label``, ``element_id`` when given, and what a click carries:
    ``value`` (the task card's 补充 and 停止 always did) or ``behaviors`` (a callback, an open_url)."""
    button = {"tag": "button", **({"element_id": element_id} if element_id else {}),
              "text": {"tag": "plain_text", "content": label}, **button_look(tone)}
    if value is not None:
        button["value"] = value
    if behaviors is not None:
        button["behaviors"] = behaviors
    return button


def callback(value):
    """A button's ``behaviors`` that send ``value`` back on a click."""
    return [{"type": "callback", "value": value}]


def button_row(buttons, element_id=""):
    """``buttons`` in a row that wraps (flow), each column as wide as its label, so none is cut in a topic's side
    panel; ``element_id`` when given."""
    return {"tag": "column_set", **({"element_id": element_id} if element_id else {}), "flex_mode": "flow",
            "horizontal_spacing": BUTTON_SPACING,
            "columns": [{"tag": "column", "width": "auto", "elements": [button]} for button in buttons]}
