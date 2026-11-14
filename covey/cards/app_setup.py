"""The 飞书应用待补 card (app/app_setup.py): what the app lacks, then the run's steps, then what it added.

One card in the owner's private chat, redrawn whole (``update_multi``) as the run goes: ``offer_card`` with
扫码自动补齐 (and 连同需审核的一起申请 when a reviewed scope is missing) and 这些先不补; ``progress_card`` while the
browser works; ``result_card`` after the official read-back; ``skipped_card`` after 这些先不补.
"""
from __future__ import annotations

from covey.cards.elements import item, md, note, panel_header, section, styled_panel, tag, text_button

ICON = "setting_outlined"
TITLE = "飞书应用待补"
START = "bridge_app_setup"  # 扫码自动补齐 (``review``: with the reviewed scopes)
SKIP = "bridge_app_setup_skip"  # 这些先不补
GROUPS = (("bot", "机器人"), ("event", "事件与回调"), ("callback", "事件与回调"), ("scope", "权限"),
          ("menu_enable", "菜单"), ("menu", "菜单"))
STEP_WORDS = (("login", "扫码登录开发者后台"), ("read", "读取后台配置"), ("manifest", "写入权限与事件"),
              ("menu", "写入菜单"), ("version", "发布新版本"), ("check", "核对线上版本"))
MARKS = {"done": "✅", "doing": "⏳", "skipped": "➖", "failed": "❌", "todo": "▫️"}


def need_line(need):
    """One need in words: what it is for, then its name in grey when that is not the same."""
    name = need["name"]
    if need["kind"] in ("menu", "bot", "menu_enable"):
        return need["words"]
    return f"{need['words']} <font color='grey'>{name}</font>"


def groups(needs):
    """``needs`` (dicts: kind, name, words) as (heading, lines) in GROUPS' order, a heading once."""
    found = {}
    for kind, heading in GROUPS:
        found.setdefault(heading, []).extend(need_line(n) for n in needs if n["kind"] == kind)
    return [(heading, rows) for heading, rows in found.items() if rows]


def grouped(needs):
    """The groups as Markdown text (the card's fallback)."""
    return "\n".join(f"**{heading}**\n" + "\n".join(f"- {row}" for row in rows) for heading, rows in groups(needs))


def grouped_md(needs):
    """The groups as card elements, one markdown block each: in the client a line after a list item joins that
    item (a heading after a list read 「…member:create菜单」, 2026-11-09), so no list is followed by more text."""
    return [md(f"**{heading}**\n" + "\n".join(f"- {row}" for row in rows)) for heading, rows in groups(needs)]


def subtitle(view):
    version = view.get("version")
    return " · ".join(filter(None, [f"线上版本 {version}" if version else "", view.get("app_name", "")]))


def offer_card(view):
    """What the online version lacks and the two (or three) buttons."""
    offer, review, full = view["offer"], view.get("review") or [], view.get("full") or []
    elements = [md(f"这个机器人的飞书应用缺 {len(offer)} 项，缺了对应的功能用不了。扫码登录开发者后台后，"
                   "Bridge 用浏览器补上并发布新版本，再回读核对。")]
    if view.get("blind"):
        elements.append(note("现在看不到线上配置：先补上「检查应用配置」这一项，其余各项补完后再查。"))
    elements += grouped_md(offer)
    if review:
        elements.append(section("需要管理员审核（默认不申请）"))
        elements.append(md("\n".join(f"- {need_line(n)}" for n in review)))
    if full:
        elements.append(note("菜单已满 5 项，放不下：" + "、".join(n["words"] for n in full) + "。要换下哪一项请在后台自己改。"))
    buttons = [text_button("扫码自动补齐", {"kind": START, "review": False}, tone="primary")]
    if review:
        buttons.append(text_button("连同需审核的一起申请", {"kind": START, "review": True}))
    buttons.append(text_button("这些先不补", {"kind": SKIP}))
    elements.append(item(md(" "), *buttons))
    elements.append(note("有代答的网页端登录时直接用它，不用扫码；登录只在这一次用，完了就删。"))
    return styled_panel(panel_header(ICON, TITLE, subtitle(view), [(f"缺 {len(offer)} 项", "orange")]), elements,
                        f"{TITLE} · 缺 {len(offer)} 项")


def offer_text(view):
    """The offer as Markdown, for when Feishu refuses the card."""
    return (f"**{TITLE}**：飞书应用缺 {len(view['offer'])} 项。\n" + grouped(view["offer"])
            + "\n卡片发不出来，请照 docs/install.md「配置飞书应用」在开发者后台补上。")


def step_lines(steps):
    return "\n".join(f"{MARKS.get(steps.get(key, 'todo'), '▫️')} {words}" for key, words in STEP_WORDS)


def progress_card(view):
    """The run: its steps, the QR's prompt while one is shown, a page between the QR and the console."""
    elements = [md(step_lines(view.get("steps") or {}))]
    if view.get("qr"):
        elements.append(md("请用**飞书手机端**扫这张卡下面的二维码，登录开发者后台（二维码过期会自动换新的）。"))
    if view.get("page"):
        elements.append(note("登录页显示：" + view["page"][:200]))
    elements.append(note("在补：" + "、".join(n["words"] for n in view["offer"][:8])
                         + (f" 等 {len(view['offer'])} 项" if len(view["offer"]) > 8 else "")))
    return styled_panel(panel_header(ICON, TITLE, subtitle(view), [("补齐中", "blue")]), elements, f"{TITLE} · 补齐中")


def result_card(view):
    """After the read-back: each need ✅ or ❌, where it stopped, the version."""
    ok = not view.get("failed") and not view.get("still")
    elements = []
    if view.get("failed"):
        failed = view["failed"]
        elements.append(md(f"❌ 卡在「{failed['words']}」：{failed['detail']}"))
        if failed.get("changed"):
            elements.append(note("这之前已经改了：" + "、".join(CHANGED.get(c, c) for c in failed["changed"])
                                 + "；没发布的改动下次补齐时一起发布。"))
    elements.append(md(step_lines(view.get("steps") or {})))
    added = [n for n in view["offer"] if n["id"] not in set(view.get("still") or ())]
    if added:
        elements.append(section(f"已生效（{len(added)}）"))
        elements += grouped_md(added)
    still = [n for n in view["offer"] if n["id"] in set(view.get("still") or ())]
    if still:
        elements.append(section(f"线上版本仍然缺（{len(still)}）"))
        elements += grouped_md(still)
    if view.get("reviewing"):
        elements.append(md(f"版本 {view.get('new_version')} 已提交，等租户管理员审核通过后生效。"))
    elif view.get("new_version"):
        elements.append(note(f"版本 {view['new_version']} 已发布；菜单约 5 分钟内出现在私聊里。"))
    if not ok and not view.get("reviewing"):
        elements.append(item(md(" "), text_button("重新扫码补齐", {"kind": START, "review": bool(view.get("with_review"))},
                                                  tone="primary")))
    title = "飞书应用已补齐" if ok else "飞书应用没补完"
    tags = [("已补齐", "green")] if ok else [("审核中", "blue")] if view.get("reviewing") else [("没补完", "red")]
    return styled_panel(panel_header(ICON, title, subtitle(view), tags), elements, title)


CHANGED = {"manifest": "权限与事件", "menu": "菜单"}


def result_text(view):
    ok = not view.get("failed") and not view.get("still")
    head = "飞书应用已补齐" if ok else "飞书应用没补完"
    detail = f"：卡在「{view['failed']['words']}」，{view['failed']['detail']}" if view.get("failed") else ""
    return f"**{head}**{detail}"


def skipped_card(view):
    elements = [*grouped_md(view["offer"]),
                note("这次先不补。缺的内容变了会再提醒；要补时等下一张卡，或照 docs/install.md 在后台自己改。")]
    return styled_panel(panel_header(ICON, TITLE, subtitle(view), [("先不补", "neutral")]), elements, f"{TITLE} · 先不补")


def unavailable_card(view, reason):
    """No browser to drive: the list stays, with what to do by hand."""
    elements = [md(f"这台机器上没法自动补：{reason}"), *grouped_md(view["offer"]),
                note("照 docs/install.md「配置飞书应用」在开发者后台补上并发布；装好浏览器环境后（见 docs/operations.md "
                     "「自动补齐飞书应用」）下次部署会再提醒。"),
                md(tag("待手动补", "orange"))]
    return styled_panel(panel_header(ICON, TITLE, subtitle(view), [("没法自动补", "orange")]), elements, TITLE)
