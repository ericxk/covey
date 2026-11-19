"""网页设置页的设备和代答两页 (docs/butler.md 网页工作台, COVEY_WEB=1; data contract v15): what app/web_settings.py pushes and changes for the
设置 card's 设备 tab (app/node_panel.py) and the bot menu's 代答 card (app/proxy_panel.py), through those panels' own code.

- 设备 (COVEY_NODE_LISTEN set): each node's state as a word the page colours (``dot``: ok, tight, down, never; the card's emoji is not a
  glyph every font has) and its line without the name; the joins the page started (``joins``). 添加 is by ssh only (the owner, 2026-11-19
  T554): the paste way's command carries a one-time join token, which would land in the page's database, so it stays on the card. The ssh
  runs in a thread of its own (up to app/node_panel.py SSH_SECONDS), never in the inbox's: its outcome is written to <state>/web/
  node-joins.json, whose mtime makes the settings item due (``JOINS``). 移除 is the card's (nodes.revoke, the gateway's revoke_now),
  confirmed on the page first; the hub's own node is never removed. 刷新 changes nothing: it only makes the item due now.
- 代答 (a deployment with a sender, app/proxy_send.py ``available``): on or off, the web login's state, what keeps the sender from sending,
  the model rows (cards/proxy_panel.py ``model_rows``, the page may change them as the card's ``pick``), and the whitelist, each contact's
  name, kind and whether it is paused (the owner, 2026-11-19 T554: their names go into the page's database). 开启, 关闭, 暂停, 恢复, 移除 are
  the card's (app/proxy_commands.py); finding and adding contacts and 去登录 stay on the card.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from covey.core.state import read_object, write_json

JOINS = "node-joins.json"  # <state>/web/: {alias: {state: joining|failed|done, at, message}}.
JOIN_KEPT = 1800  # A join that ended is shown this long.
DOTS = {"🟢": "ok", "🟠": "tight", "🔴": "down", "⚪": "never"}


def joins_path(state_dir) -> Path:
    return Path(state_dir) / "web" / JOINS


def read_joins(state_dir, now=None) -> dict:
    now = time.time() if now is None else now
    joins = read_object(joins_path(state_dir))
    return {alias: row for alias, row in joins.items()
            if isinstance(row, dict) and (row.get("state") == "joining" or now - float(row.get("at") or 0) < JOIN_KEPT)}


_JOIN_LOCK = threading.Lock()


def write_join(state_dir, alias, **row) -> None:
    with _JOIN_LOCK:
        joins = read_joins(state_dir)
        joins[alias] = {**row, "at": time.time()}
        write_json(joins_path(state_dir), joins, ensure_ascii=False)


def node_row(row) -> dict:
    """A row of app/node_panel.py ``facts`` as the page draws it: the dot's word and the line without its emoji and name."""
    line = str(row.get("line") or "")
    emoji, _, rest = line.partition(" ")
    title = str(row.get("title") or "")
    rest = rest[len(title):].lstrip(" ·") if rest.startswith(title) else rest
    return {"alias": row["alias"], "title": title, "dot": DOTS.get(emoji, "never"), "line": rest, "grey": row.get("grey") or "",
            "local": bool(row.get("local"))}


def nodes_tab(state_dir, panel) -> dict:
    """设备: the nodes as the card's tab reads them, and the joins the page started."""
    from covey.app.node_panel import facts
    rows = facts(state_dir, "")["rows"]
    known = {r["alias"] for r in rows}
    joins = [{"alias": alias, "state": row.get("state"), "message": row.get("message") or ""}
             for alias, row in sorted(read_joins(state_dir).items()) if not (row.get("state") == "done" and alias in known)]
    return {"summary": f"{len(rows)} 台设备" if rows else "还没有设备", "items": [node_row(r) for r in rows], "joins": joins,
            "can_add": getattr(getattr(panel, "gateway", None), "tls", None) is not None}


def node_add(ctx, panel, payload, start=None) -> str:
    """添加 by ssh: checked as the card checks it, the join opened, the ssh run off this thread; the receipt says it started."""
    from covey.app.node_gateway import paste_command
    from covey.app.node_panel import ssh_args
    from covey.core import nodes
    gateway = panel.gateway
    if gateway.tls is None:
        raise ValueError("Hub 还没开始监听节点，稍后再试")
    alias = str(payload.get("name") or "").strip().lower()
    title = str(payload.get("title") or "").strip()[:40]
    typed = str(payload.get("ssh") or "").strip()
    if not typed:
        raise ValueError("网页上只能经 ssh 添加；不填 ssh 地址的方式请到飞书设置卡上拿命令")
    args = ssh_args(typed)  # ValueError: said as it is.
    token = nodes.open_join(ctx.state_dir, alias, title or alias, via="ssh")
    command = paste_command(gateway.address, gateway.tls.spki, token)
    write_join(ctx.state_dir, alias, state="joining", message="")

    def join():
        try:
            notice = panel.ssh_join(alias, args, command)
        except Exception as exc:  # noqa: BLE001 - said on the page
            notice = f"⚠️ 经 ssh 安装 {alias} 出错：{type(exc).__name__}"
        ok = notice.startswith("✅")
        write_join(ctx.state_dir, alias, state="done" if ok else "failed", message=notice.lstrip("✅⚠️ ").strip())

    (start or (lambda work: threading.Thread(target=work, name=f"web-node-join-{alias}", daemon=True).start()))(join)
    from covey.app.node_panel import SSH_SECONDS
    return f"正在经 ssh 登上 {alias} 安装节点（最多 {SSH_SECONDS} 秒），结果会出现在设备页"


def node_remove(ctx, panel, payload) -> str:
    """移除, the card's own: the node revoked and told at once when it is connected."""
    from covey.core import nodes
    alias = str(payload.get("alias") or "")
    if not alias or alias == nodes.LOCAL:
        raise ValueError("本机不能移除")
    if not nodes.revoke(ctx.state_dir, alias):
        raise ValueError(f"{alias} 已经不在了")
    panel.gateway.revoke_now(alias)
    return f"已移除 {alias}：它在线时马上卸载自己，不在线时下次连上来再卸载"


# -- 代答 ------------------------------------------------------------------------------------------------------------------------

def proxy_tab(ctx, panel, row, limit) -> dict:
    """代答, as the bot menu's card reads it: no contact's messages, only who is on the whitelist."""
    from covey.app.launch_models import available_models
    from covey.cards.proxy_panel import kind_label, model_rows
    from covey.core import proxy as records
    proxy = panel.proxy
    settings = proxy.settings(ctx)
    choices = available_models(proxy.topics, ctx)
    seen = proxy.web_login.login_seen
    problems = ([] if settings.get("user_profile") else ["代答还读不到你的消息：部署没配好你的飞书身份（COVEY_PROXY_PROFILE）"]) \
        + list(proxy.web.problems(ctx, settings) or ())
    contacts = records.read_contacts(ctx.state_dir)
    groups = {}
    for contact in contacts:
        source = records.source_of(contact)
        if source and contact.get("source_name"):
            groups[source] = contact["source_name"]
    enabled = bool(settings.get("enabled"))
    paused = sum(1 for c in contacts if c.get("paused"))
    return {"summary": " · ".join(["开着" if enabled else "关着", f"白名单 {len(contacts)} 个"] + ([f"暂停 {paused} 个"] if paused else [])),
            "enabled": enabled, "group": settings.get("group_name") or "", "login_ok": bool(enabled and seen and seen[0]),
            "problems": problems,
            "rows": [row(each, True) for each in model_rows(settings, choices, panel.current(settings, choices), limit)],
            "contacts": [{"key": c["key"], "name": c.get("name") or "", "kind": kind_label(c), "paused": bool(c.get("paused")),
                          "from": groups.get(records.source_of(c) or "", "")} for c in contacts]}


def proxy_change(ctx, panel, payload) -> str:
    """开启, 关闭, a model row, 暂停, 恢复, 移除: the card's own (app/proxy_panel.py, app/proxy_commands.py)."""
    from covey.cards import proxy_panel as cards
    proxy = panel.proxy
    op = str(payload.get("op") or "")
    try:
        if op == "enable":
            if proxy.settings(ctx).get("enabled"):
                return "代答已经是开启的"
            return proxy.commands.cmd_enable(ctx, "", "", "")
        if op == "disable":
            if not proxy.settings(ctx).get("enabled"):
                return "代答已经是关闭的"
            return proxy.commands.cmd_disable(ctx, "", "", "")
        if op == "row":
            kind = payload.get("kind")
            if kind not in (cards.HARNESS, cards.PROVIDER, cards.TIER_MODEL, cards.TIER_BUDGET):
                raise ValueError("没有这一行")
            words = panel.pick(ctx, kind, payload.get("option"))
            if words.startswith("⚠️"):
                raise ValueError(words.removeprefix("⚠️").strip())
            return words.removeprefix("✓").strip()
        if op in ("pause", "resume", "remove"):
            from covey.core import proxy as records
            key = str(payload.get("contact") or "")
            contact = next((c for c in records.read_contacts(ctx.state_dir) if c["key"] == key), None)
            if contact is None:
                raise ValueError("白名单里已经没有这个人了")
            if op == "remove":
                return proxy.commands.cmd_remove(ctx, key, "", "")
            if bool(contact.get("paused")) == (op == "pause"):
                return f"{contact.get('name') or '这个人'} 已经是{'暂停' if op == 'pause' else '运行中'}的了"
            return proxy.commands.set_paused(ctx, key, op == "pause")
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - the card says the same: 未完成
        raise ValueError(f"没有完成：{exc}") from None
    raise ValueError("不认识的代答操作")
