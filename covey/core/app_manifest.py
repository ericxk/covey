"""What the Feishu app behind a Bridge must have, and what its online version lacks (docs/feishu-app.md 应用要有什么).

``required(env)`` lists the app's needs from this deployment's switches: the bot, its events and callback, the
scopes the Bridge's own calls use, and the bot menu's items. ``missing(env, online)`` compares them with the
online version as feishu/app_config.py reads it through the official API (no login). app/app_setup.py checks
at each start and offers the owner to add what is missing (feishu/app_console.py, after a QR login);
bin/covey_app_setup.py prints the same check for the install and deploy scripts.

A scope that needs the tenant admin's review is never added unless asked (``review``): listed apart on
the card, unticked. Nothing is ever removed: the app's other scopes, events and menu items stay.
Standard library only.
"""
from __future__ import annotations

import dataclasses
import base64
import gzip
import hashlib
import json
import os

from covey.core import env as cenv

MESSAGE_EVENT = "im.message.receive_v1"
MENU_EVENT = "application.bot.menu_v6"
READ_EVENT = "im.message.message_read_v1"
CARD_CALLBACK = "card.action.trigger"
MENU_LIMIT = 5  # Feishu's floating menu holds at most five top-level items.
MENU_DISPLAY = 3  # 悬浮菜单 (bot_menu_display_strategy).


@dataclasses.dataclass(frozen=True)
class Need:
    """One thing the app must have: ``kind`` scope, event, callback, bot, menu_enable or menu; ``name`` the
    scope, event key, callback or menu event key; ``words`` what it is for, as the card says it."""
    kind: str
    name: str
    words: str
    review: bool = False  # A scope the tenant admin reviews: added only when the owner asks.
    label: str = ""  # A menu item's name.
    icon: str = ""  # A menu item's built-in icon token (飞书线性图标).

    @property
    def id(self):
        return f"{self.kind}:{self.name}"


# The scopes the Bridge's own calls use, with what each is for. The CLI app template (lark-cli config init --new)
# brings most of them; im:message (recall, read events) and the Drive ones (产物上传) it does not.
SCOPES = (
    ("im:message.p2p_msg:readonly", "收私聊消息"),
    ("im:message.group_at_msg:readonly", "收群里 @ 机器人的消息"),
    ("im:message:readonly", "读消息和引用"),
    ("im:message:send_as_bot", "发消息"),
    ("im:message", "撤回消息"),
    ("im:message:update", "改卡片"),
    ("cardkit:card:write", "任务卡实时更新"),
    ("im:resource", "收发图片和文件"),
    ("im:chat:read", "读群信息"),
    ("im:message.reactions:write_only", "在你的消息上加表情"),
    ("drive:file:upload", "产物上传云空间"),
    ("space:folder:create", "产物上传云空间"),
    ("docs:permission.member:create", "产物文件夹给你权限"),
    ("application:application:self_manage", "检查应用配置"),
)
PIN_SCOPES = (("im:message.pins:read", "置顶看板"), ("im:message.pins:write_only", "置顶看板"))
GROUP_ALL_SCOPES = (("im:chat.members:read", "群聊免@ 选人"),)
GROUP_ALL_REVIEW = ("im:message.group_msg", "群聊免@ 收群里全部消息")

# The bot menu (docs/usage.md 机器人菜单: 看板 · 用量 · 代答 · 设置). 看板 with COVEY_BOARD=1 or the digest (``board``),
# 代答 only where an enabled extension has the proxy_sender hook (``proxy_sender``).
MENU = (("weekly_usage", "用量", "insert-chart_outlined"), ("digest_board", "看板", "bitablekanban_outlined"),
        ("proxy_settings", "代答", "robot_outlined"), ("bridge_settings", "设置", "setting_outlined"))
# Older items that still work (app/bot_menu.py KEYS): counted toward the limit, never replaced or removed.
OLD_MENU_KEYS = ("manage_workspaces", "redaction_dashboard", "manage_groups", "main_thread_settings")


def proxy_sender():
    """Whether an enabled extension has the proxy_sender hook (read from the manifests, no module imported);
    False when they cannot be read."""
    try:
        from covey.core import extensions
        return any("proxy_sender" in extension.hooks for extension in extensions.manifests())
    except Exception:
        return False


def switch(env, name):
    return str(cenv.get(name, "", env)).strip() == "1"


def board_switch(env=None) -> bool:
    """COVEY_BOARD=1: the bot menu has 看板 without 早晚汇总 too (app/digest.py menu_items; ./install writes it). Unset
    or 0, 看板 comes with the digest alone, as before the switch."""
    return str(cenv.get("COVEY_BOARD", "", os.environ if env is None else env)).strip() == "1"


def board(env=None) -> bool:
    """Whether the app's menu needs 看板: COVEY_BOARD=1 or a COVEY_DIGEST_TIMES set."""
    env = os.environ if env is None else env
    return board_switch(env) or bool(str(cenv.get("COVEY_DIGEST_TIMES", "", env)).strip())


def required(env=None):
    """This deployment's needs, in the card's order: bot, events, callback, scopes, menu."""
    env = os.environ if env is None else env
    needs = [Need("bot", "bot", "机器人能力"),
             Need("event", MESSAGE_EVENT, "收消息"),
             Need("event", MENU_EVENT, "机器人菜单的点击"),
             Need("callback", CARD_CALLBACK, "卡片按钮的点击")]
    pinned = switch(env, "COVEY_PINNED_TOPICS")
    if pinned:
        needs.append(Need("event", READ_EVENT, "话题看过没有（已读）"))
    scopes = list(SCOPES)
    if pinned or switch(env, "COVEY_PINNED_OVERVIEW"):
        scopes += PIN_SCOPES
    group_all = switch(env, "COVEY_GROUP_ALL_MESSAGES")
    if group_all:
        scopes += GROUP_ALL_SCOPES
    needs += [Need("scope", name, words) for name, words in scopes]
    if group_all:
        needs.append(Need("scope", GROUP_ALL_REVIEW[0], GROUP_ALL_REVIEW[1], review=True))
    needs.append(Need("menu_enable", "menu", "自定义菜单"))
    shown = board(env)
    for key, label, icon in MENU:
        if (key != "digest_board" or shown) and (key != "proxy_settings" or proxy_sender()):
            needs.append(Need("menu", key, f"菜单「{label}」", label=label, icon=icon))
    return needs


def has(online, need):
    if need.kind == "bot":
        return bool(online.get("bot"))
    if need.kind == "menu_enable":
        return bool(online.get("menu_enable"))
    field = {"scope": "scopes", "event": "events", "callback": "callbacks", "menu": "menu_keys"}[need.kind]
    return need.name in (online.get(field) or ())


def missing(env, online):
    """(lacking, full): the needs ``online`` (feishu/app_config.py online_config) lacks, and of those the menu items
    that would not fit in the menu's MENU_LIMIT next to the items it has."""
    lacking = [need for need in required(env) if not has(online, need)]
    room = MENU_LIMIT - len(online.get("menu_keys") or ())
    full = []
    for need in lacking:
        if need.kind == "menu":
            if room > 0:
                room -= 1
            else:
                full.append(need)
    return lacking, full


def offered(lacking, full, *, review=False):
    """What one 扫码自动补齐 adds: ``lacking`` but the menu items with no room, and the reviewed scopes unless
    ``review``."""
    skip = {need.id for need in full}
    return [need for need in lacking if need.id not in skip and (review or not need.review)]


def digest(needs):
    """A short stable name of a set of needs: the card is offered once per set (app/app_setup.py)."""
    return hashlib.sha256("\n".join(sorted(need.id for need in needs)).encode()).hexdigest()[:16]


def plan(needs, *, remark):
    """The console's work (feishu/app_console.py): the additions as JSON, and the version's 更新说明."""
    return {"scopes": [n.name for n in needs if n.kind == "scope"],
            "events": [n.name for n in needs if n.kind == "event"],
            "callbacks": [n.name for n in needs if n.kind == "callback"],
            "bot": any(n.kind == "bot" for n in needs),
            "menu_enable": any(n.kind in ("menu_enable", "menu") for n in needs),
            "menu": [{"key": n.name, "label": n.label, "icon": n.icon} for n in needs if n.kind == "menu"],
            "menu_display": MENU_DISPLAY,
            "remark": remark}


def remark(needs):
    """The version's 更新说明: what the Bridge added, in words."""
    parts = []
    menus = [n.label for n in needs if n.kind == "menu"]
    if menus:
        parts.append("菜单 " + "、".join(menus))
    scopes = len([n for n in needs if n.kind == "scope"])
    if scopes:
        parts.append(f"{scopes} 项权限")
    events = [n for n in needs if n.kind in ("event", "callback")]
    if events:
        parts.append(f"{len(events)} 项事件与回调")
    if any(n.kind == "bot" for n in needs):
        parts.append("机器人能力")
    return "Harness Bridge 自动补齐：" + ("；".join(parts) if parts else "配置")


def joined(needs):
    """The needs' words for one line, each said once (two scopes serve 产物上传云空间)."""
    return "、".join(dict.fromkeys(n.words for n in needs))


LAUNCHER = "https://open.feishu.cn/page/launcher"


def launcher_links(app_id, needs):
    """The one-tap links lark-cli itself prints for what the app's console still lacks (``event consume`` says "subscribe these
    event types by scanning: <link>"): opened on the phone as the app's owner, each adds the events or the callbacks. Same
    shape lark-cli 1.0.98 uses: ``addons`` is the URL-safe base64 of the gzipped JSON. [(words, url)]: one link for the
    events and callbacks together (its page lists both, one confirm adds both); menu items and scopes have no such link."""
    def link(addons):
        raw = gzip.compress(json.dumps(addons, separators=(",", ":")).encode(), mtime=0)
        return f"{LAUNCHER}?clientID={app_id}&addons={base64.urlsafe_b64encode(raw).decode().rstrip('=')}"

    events = [n for n in needs if n.kind == "event"]
    callbacks = [n for n in needs if n.kind == "callback"]
    addons, words = {}, []
    if events:
        addons["events"] = {"items": {"tenant": [n.name for n in events], "user": []}}
        words.append("事件订阅：" + "、".join(n.words for n in events))
    if callbacks:
        addons["callbacks"] = {"items": [n.name for n in callbacks]}
        words.append("回调订阅：" + "、".join(n.words for n in callbacks))
    return [("；".join(words), link(addons))] if addons else []
