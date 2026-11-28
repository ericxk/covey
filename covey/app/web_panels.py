"""网页设置页的设备和代答两页 (docs/web.md, COVEY_WEB=1; data contract v15): what app/web_settings.py pushes and changes for the
设置 card's 设备 tab (app/node_panel.py) and the bot menu's 代答 card (app/proxy_panel.py), through those panels' own code.

- 设备 (shown with devices on, COVEY_DEVICES=1, or nodes there; data contract v46 ``rows``): devices.json and the node registry joined
  by alias into one row a machine (``device_rows``: name, 用途, 可写 / 只读, its ssh and whether the last look reached it
  (``devices/<别名>/health.json``), its node's state, the last 体检 (``profile.json`` ``check``)); 添加 (checked first: the 体检 runs off
  the inbox and the device is written only when it passed; one without ssh is only watched), 改, 移除 (the entry out of devices.json,
  its records kept, a node of the same alias revoked) and 体检 (core/device_probe.py ``checkup``: ssh, the shim, hello), each running
  体检's state in <state>/web/device-checks.json (``CHECKS``). The older keys stay for an older page: each node's state as a word the
  page colours (``dot``: ok, tight, down, never; the card's emoji is not a glyph every font has) and its line without the name; the
  joins the page started (``joins``). 添加 is by ssh only: the
  paste way's command carries a one-time join token, which would land in the page's database, so it stays on the card. The ssh
  runs in a thread of its own (up to app/node_panel.py SSH_SECONDS), never in the inbox's: its outcome is written to <state>/web/
  node-joins.json, whose mtime makes the settings item due (``JOINS``). 移除 is the card's (nodes.revoke, the gateway's revoke_now),
  confirmed on the page first; the hub's own node is never removed. 刷新 changes nothing: it only makes the item due now.
- 代答 (a deployment with a sender, app/proxy_send.py ``available``): on or off, the web login's state, what keeps the sender from sending,
  the model rows (cards/proxy_panel.py ``model_rows``, the page may change them as the card's ``pick``), and the whitelist, each contact's
  name, kind and whether it is paused (their names go into the page's database). 开启, 关闭, 暂停, 恢复, 移除 are
  the card's (app/proxy_commands.py); finding and adding contacts and 去登录 stay on the card.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from covey.core.state import Unreadable, read_object, update_json, write_json

JOINS = "node-joins.json"  # <state>/web/: {alias: {state: joining|failed|done, at, message}}.
JOIN_KEPT = 1800  # A join that ended is shown this long.
CHECKS = "device-checks.json"  # <state>/web/: {alias: {state: checking|failed|done, at, message, adding}}: 体检 the page started.
CHECK_STUCK = 300  # A 体检 still "checking" after this long (the Bridge restarted under it) is shown as not finished.
NOTE_MAX, TITLE_MAX, SSH_MAX = 60, 40, 200  # core/devices.py NOTE_MAX; the page's boxes (core/web_sync.py checks the same).
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


def checks_path(state_dir) -> Path:
    return Path(state_dir) / "web" / CHECKS


def read_checks(state_dir, now=None) -> dict:
    """The 体检 the page started that are running, or ended within JOIN_KEPT; one "checking" past CHECK_STUCK reads as failed."""
    now = time.time() if now is None else now
    found = {}
    for alias, row in read_object(checks_path(state_dir)).items():
        if not isinstance(row, dict):
            continue
        age = now - float(row.get("at") or 0)
        if row.get("state") == "checking" and age > CHECK_STUCK:
            row = {**row, "state": "failed", "message": "体检没有结束（Covey 可能重启过），再点一次体检"}
        if row.get("state") == "checking" or age < JOIN_KEPT:
            found[alias] = row
    return found


_CHECK_LOCK = threading.Lock()


def write_check(state_dir, alias, **row) -> None:
    """One 体检's state into CHECKS (read with ``update_json``: a file that cannot be read is left as it is); those that ended long
    ago dropped."""
    now = time.time()

    def change(checks):
        kept = {key: value for key, value in checks.items() if isinstance(value, dict)
                and (value.get("state") == "checking" or now - float(value.get("at") or 0) < JOIN_KEPT)}
        return {**kept, alias: {**row, "at": now}}
    with _CHECK_LOCK:
        try:
            update_json(checks_path(state_dir), change, ensure_ascii=False)
        except Unreadable:
            pass  # The page then shows no running 体检; its outcome is the device's profile.json all the same.


def _ago(at, now) -> str:
    from covey.core.device_probe import age_text
    return age_text(now - float(at)) if isinstance(at, (int, float)) and at else ""


def link_state(state_dir, alias, now) -> dict:
    """Whether the Bridge's last look reached a device over ssh (``devices/<别名>/health.json``, app/devices.py ``probe``)."""
    from covey.core import devices
    from covey.core.devices import OFFLINE_REASONS
    health = read_object(devices.device_dir(state_dir, alias) / "health.json")
    if not isinstance(health.get("at"), (int, float)):
        return {"state": "never", "text": "还没连过"}
    when = _ago(health["at"], now)
    if health.get("ok"):
        return {"state": "ok", "text": f"连得上 · {when}"}
    why = OFFLINE_REASONS.get(str(health.get("error") or ""), str(health.get("error") or "没有答复"))
    return {"state": "down", "text": f"连不上（{why}）· {when}"}


def check_state(state_dir, alias) -> dict | None:
    """The last 体检 or 自检 of a device (``profile.json`` ``check``): {ok, at, text, steps}; None before any."""
    from covey.core import devices
    check = devices.read_profile(state_dir, alias).get("check")
    if not isinstance(check, dict) or not isinstance(check.get("at"), (int, float)):
        return None
    return {"ok": bool(check.get("ok")), "at": check["at"], "text": " ".join(str(check.get("text") or "").split())[:300],
            **({"steps": check["steps"]} if isinstance(check.get("steps"), int) else {})}


def device_rows(state_dir, now=None) -> tuple[list, str]:
    """(the 设备 page's rows, why devices.json cannot be read or ''): the hub's own node first, then every machine of devices.json
    (its ssh entries and the watched ones) and of the node registry, joined by alias, by alias. A row: {alias, title, note, access,
    listed, ssh, runs, link, node, check, checking, local}."""
    from covey.app.node_panel import facts
    from covey.core import devices, nodes
    now = time.time() if now is None else now
    try:
        config = devices.current()
        listed = {**{alias: (device, True) for alias, device in config.devices.items()},
                  **{alias: (device, False) for alias, device in config.watched.items()}}
        error = ""
    except devices.DeviceConfigError as exc:
        listed, error = {}, " ".join(str(exc).split())[:300]
    watched = {row["alias"]: row for row in facts(state_dir, "", now)["rows"]}
    checks = read_checks(state_dir, now)
    rows = []
    for alias in sorted(set(listed) | set(watched), key=lambda a: (a != nodes.LOCAL, a)):
        device, runs = listed.get(alias, (None, False))
        node = watched.get(alias)
        local = alias == nodes.LOCAL and device is None
        title = device.title if device is not None else ("本机" if local else str((node or {}).get("title") or alias))
        if device is None and node is not None and not local and title.endswith(f"（{alias}）"):
            title = title[:-len(f"（{alias}）")]
        shown = node_row(node) if node is not None else None
        check = checks.get(alias)
        rows.append({"alias": alias, "title": title, "note": device.note if device is not None else "",
                     "access": device.access if device is not None else "", "listed": device is not None,
                     "ssh": devices.ssh_text(device) if device is not None else "", "runs": runs,
                     "link": link_state(state_dir, alias, now) if runs else None,
                     "node": {key: shown[key] for key in ("dot", "line", "grey")} if shown else None,
                     "check": check_state(state_dir, alias) if device is not None else None,
                     "checking": {"state": check.get("state"), "message": check.get("message") or ""}
                     if check and not check.get("adding") else None, "local": local})
    return rows, error


def nodes_tab(state_dir, panel, now=None) -> dict:
    """设备: the nodes as the card's tab reads them, and the joins the page started (``items``, ``joins``: an older page's); v46 the rows
    of devices.json and the nodes joined by alias (``rows``), the devices being added (``adding``), whether a device may be added
    here (``can_add_device``: devices on) and a node (``can_add``: the hub listens)."""
    from covey.app.node_panel import facts
    from covey.core import devices
    now = time.time() if now is None else now
    rows = facts(state_dir, "", now)["rows"]
    known = {r["alias"] for r in rows}
    joins = [{"alias": alias, "state": row.get("state"), "message": row.get("message") or ""}
             for alias, row in sorted(read_joins(state_dir).items()) if not (row.get("state") == "done" and alias in known)]
    merged, error = device_rows(state_dir, now)
    listed = {row["alias"] for row in merged if row["listed"]}
    adding = [{"alias": alias, "state": row.get("state"), "message": row.get("message") or ""}
              for alias, row in sorted(read_checks(state_dir, now).items())
              if row.get("adding") and not (row.get("state") == "done" and alias in listed)]
    machines = [row for row in merged if not row["local"]]
    writable = sum(1 for row in machines if row["listed"] and row["access"] == "write")
    online = sum(1 for row in merged if row["node"] and row["node"]["dot"] in ("ok", "tight"))
    parts = [f"{len(machines)} 台设备" if machines else "还没有设备"]
    if any(row["listed"] for row in machines):
        parts.append(f"可写 {writable} 台")
    if any(row["node"] for row in merged):
        parts.append(f"监控在线 {online} 台")
    return {"summary": " · ".join(parts), "items": [node_row(r) for r in rows], "joins": joins,
            "can_add": getattr(getattr(panel, "gateway", None), "tls", None) is not None,
            "rows": merged, "adding": adding, "can_add_device": devices.enabled(), **({"error": error} if error else {})}


# -- 设备 (v46): devices.json changed by the page --------------------------------------------------------------------------------

def _one_line(value, limit, what) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "\n" in value or "\r" in value or len(value.strip()) > limit:
        raise ValueError(f"{what}要是一行、最多 {limit} 个字")
    return " ".join(value.split())


def _access(value, default="read") -> str:
    if value in (None, ""):
        return default
    if value not in ("write", "read"):
        raise ValueError("可写还是只读要是 write 或 read")
    return value


def _entry(alias, *, title="", note="", access="read", ssh=None) -> dict:
    """A devices.json entry the page writes: its title, use and access, and ``ssh`` ((host, options), or None: only watched)."""
    entry = {"title": title or alias, "access": access, **({"note": note} if note else {})}
    if ssh is not None:
        host, options = ssh
        entry["ssh"] = {"host": host, **({"options": list(options)} if options else {})}
    return entry


def _wanted(alias) -> str:
    from covey.core import devices
    alias = str(alias or "").strip().lower()
    if not devices.ALIAS.match(alias):
        raise ValueError("名字只用小写字母、数字和 -，小写字母开头，最多 24 个")
    return alias


def _listed(alias) -> bool:
    from covey.core import devices
    config = devices.current()
    return alias in config.devices or alias in config.watched


def _on():
    from covey.core import devices
    if not devices.enabled():
        raise ValueError("多设备没开（COVEY_DEVICES=1），这里管不了设备名单")


def _start(start, work, name):
    (start or (lambda job: threading.Thread(target=job, name=name, daemon=True).start()))(work)


def device_add(ctx, payload, start=None) -> str:
    """添加: a device of devices.json from the page, ``access`` read unless the page says write. With an ssh address the 体检
    (core/device_probe.py ``checkup``) runs off this thread first and the entry is written only when it passed (its state in
    CHECKS); without one the machine is only watched (its node) and written at once. The receipt."""
    from covey.core import device_probe, devices
    _on()
    alias = _wanted(payload.get("name"))
    title = _one_line(payload.get("title"), TITLE_MAX, "名字")
    note = _one_line(payload.get("note"), NOTE_MAX, "用途")
    access = _access(payload.get("access"))
    typed = _one_line(payload.get("ssh"), SSH_MAX, "ssh 地址")
    try:
        if _listed(alias):
            raise ValueError(f"设备名单里已经有 {alias}")
    except devices.DeviceConfigError as exc:
        raise ValueError(f"devices.json 有问题，先手工改好：{' '.join(str(exc).split())[:200]}") from None
    if (read_checks(ctx.state_dir).get(alias) or {}).get("state") == "checking":
        raise ValueError(f"{alias} 正在体检，等它结束")
    if not typed:
        devices.update(lambda raw: {**raw, "devices": {**(raw.get("devices") or {}),
                                                       alias: _entry(alias, title=title, note=note, access=access)}})
        return f"已加入设备名单：{alias}（没有 ssh：只能看状况，不能执行）"
    ssh = devices.split_ssh(typed)  # ValueError: said as it is.
    entry = _entry(alias, title=title, note=note, access=access, ssh=ssh)
    try:
        current = devices.raw()
        candidate = devices.parse(json.dumps({**(current or {"schema": devices.SCHEMA}),
                                                            "devices": {**(current.get("devices") or {}), alias: entry}}))
    except (devices.DeviceConfigError, devices.Unwritable) as exc:
        raise ValueError(" ".join(str(exc).split())[:300]) from None
    write_check(ctx.state_dir, alias, state="checking", message="", adding=True)

    def work():
        try:
            result = device_probe.checkup(candidate, alias, state_dir=Path(ctx.state_dir))
            if not result.ok:
                write_check(ctx.state_dir, alias, state="failed", message=result.explain().replace("\n", " ")[:300], adding=True)
                return

            def add(raw):
                found = raw.get("devices") or {}
                return raw if alias in found else {**raw, "devices": {**found, alias: entry}}
            devices.update(add)
            write_check(ctx.state_dir, alias, state="done", message=result.explain()[:300], adding=True)
        except Exception as exc:  # noqa: BLE001 - said on the page
            write_check(ctx.state_dir, alias, state="failed", message=f"体检出错：{type(exc).__name__} {str(exc)[:200]}", adding=True)

    _start(start, work, f"web-device-add-{alias}")
    return f"正在体检 {alias}（ssh、设备端小程序、hello），通过了才加进名单，结果会出现在设备页"


def device_set(ctx, payload) -> str:
    """改: a listed device's 名字, 用途, 可写 / 只读 and ssh, those given (an ssh only replaced, never taken away here). The receipt."""
    from covey.core import devices
    _on()
    alias = _wanted(payload.get("alias"))
    changes, words = {}, []
    if payload.get("title") is not None:
        changes["title"] = _one_line(payload.get("title"), TITLE_MAX, "名字") or alias
        words.append("名字")
    if payload.get("note") is not None:
        changes["note"] = _one_line(payload.get("note"), NOTE_MAX, "用途")
        words.append("用途")
    if payload.get("access") is not None:
        changes["access"] = _access(payload.get("access"))
        words.append("可写还是只读")
    ssh = None
    if payload.get("ssh") is not None:
        ssh = devices.split_ssh(_one_line(payload.get("ssh"), SSH_MAX, "ssh 地址"))
        words.append("ssh")
    if not words:
        raise ValueError("没有要改的项")

    def change(raw):
        found = dict(raw.get("devices") or {})
        if alias not in found or not isinstance(found[alias], dict):
            raise ValueError(f"设备名单里没有 {alias}")
        entry = {**found[alias], **changes}
        if not entry.get("note"):
            entry.pop("note", None)
        if ssh is not None:
            entry["ssh"] = {**(entry.get("ssh") if isinstance(entry.get("ssh"), dict) else {}), "host": ssh[0]}
            entry["ssh"].pop("options", None)
            if ssh[1]:
                entry["ssh"]["options"] = list(ssh[1])
        found[alias] = entry
        return {**raw, "devices": found}
    try:
        devices.update(change)
    except devices.Unwritable as exc:
        raise ValueError(" ".join(str(exc).split())[:300]) from None
    return f"已改 {alias}：" + "、".join(words)


def device_remove(ctx, panel, payload) -> str:
    """移除: the device out of devices.json (its records under ``devices/<别名>/`` kept) and a node of the same alias revoked (the
    card's own revoke, told at once when it is connected). The receipt."""
    from covey.core import devices, nodes
    alias = _wanted(payload.get("alias"))
    removed = False

    def change(raw):
        nonlocal removed
        found = dict(raw.get("devices") or {})
        removed = found.pop(alias, None) is not None
        return {**raw, "devices": found} if removed else raw
    try:
        if devices.config_path().exists():
            devices.update(change)
    except devices.Unwritable as exc:
        raise ValueError(" ".join(str(exc).split())[:300]) from None
    revoked = alias != nodes.LOCAL and alias in nodes.active(ctx.state_dir) and nodes.revoke(ctx.state_dir, alias)
    if revoked and panel is not None:
        panel.gateway.revoke_now(alias)
    if not removed and not revoked:
        raise ValueError(f"{alias} 已经不在了")
    words = (["已从设备名单去掉"] if removed else []) + (["撤销了它的监控节点"] if revoked else [])
    return f"{alias}：" + "，".join(words) + ("；它的记录留着" if removed else "")


def device_check(ctx, payload, start=None) -> str:
    """体检 of a listed device with ssh (core/device_probe.py ``checkup``), off this thread; its state in CHECKS, its outcome in the
    device's ``profile.json``. The receipt."""
    from covey.core import device_probe, devices
    _on()
    alias = _wanted(payload.get("alias"))
    try:
        config = devices.current()
    except devices.DeviceConfigError as exc:
        raise ValueError(f"devices.json 有问题：{' '.join(str(exc).split())[:200]}") from None
    if alias in config.watched:
        raise ValueError(f"{alias} 没有 ssh，只能看状况，体检不了")
    if alias not in config.devices:
        raise ValueError(f"设备名单里没有 {alias}")
    if (read_checks(ctx.state_dir).get(alias) or {}).get("state") == "checking":
        return f"{alias} 正在体检"
    write_check(ctx.state_dir, alias, state="checking", message="")

    def work():
        try:
            result = device_probe.checkup(config, alias, state_dir=Path(ctx.state_dir))
            write_check(ctx.state_dir, alias, state="done" if result.ok else "failed",
                        message=result.explain().replace("\n", " ")[:300])
        except Exception as exc:  # noqa: BLE001 - said on the page
            write_check(ctx.state_dir, alias, state="failed", message=f"体检出错：{type(exc).__name__}")

    _start(start, work, f"web-device-check-{alias}")
    return f"正在体检 {alias}（ssh、设备端小程序、hello），结果会出现在设备页"


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
            # Every model row is the page's to change, through the card's ``pick`` (``proxy_change``), not the 设置 rows' EDITABLE.
            "rows": [{**row(each), "editable": True} for each in model_rows(settings, choices, panel.current(settings, choices), limit)],
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
