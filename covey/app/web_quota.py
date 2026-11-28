"""账号与额度 on the web page: the ``settings`` item ``quota`` the page's 账号与额度 tab reads, and the page's one operation
``quota_refresh`` (刷新; docs/web.md 账号与额度, data contract v42, W1b). The item is built from the quota snapshots under ``<state>/quota/``
(core/quota.py Store) alone; nothing is asked of a source here.

- ``view(state_dir, now)``: one entry a source ``sources[]`` ({id, kind, title, link?, collected_at, ttl, stale_after, read_at, failing,
  why, usable, total, next_available_at, low, dry, accounts[]}; ``why``: what the last read that failed said, '' when it did not), each account {name, login?, state, available_at, pools[]}, each pool
  {key, label, active, state, available_at, windows[{kind, label, left, resets_at, checked_at, ceiling?}]}. ``left`` is what is
  left (1 - used), null when not read. Only whitelisted fields; their texts pass core/quota.py ``scrub``, but ``login`` (agy's
  Google e-mail) does not. The page does the rest of the time-dependent words (a reading too old, a window already reset).
- agy's two group snapshots (``agy.gemini``, ``agy.3p``) are one source ``agy``, one row an account: an account's pools are the
  groups it ran a turn in the last 7 days (the entry's ``recent``), else the groups anyone did, else Gemini; two pools only when
  both were used.
- ``low`` (the source as a whole is at its bottom: no account that can be used has 20% left in every window) and ``dry`` (no account
  can be used, and some will be again by itself) say when the page's sidebar shows its capsule and the Bridge tells the owner
  (app/quota_notice.py); ``relogin`` lists the accounts that need a sign-in again. A source that is not agy and not a gateway
  has ``expand`` (the page opens it): an account needs a sign-in or has nothing left.
- ``Refresher.ask``: the page's 刷新. The Bridge reads every source at once (core/quota_sources.py ``refresh`` with ``force``) and
  asks agy's /usage of every account (backends/agy/accounts.py ``probe_accounts``, which is the one thing that asks a spare account)
  in a thread of its own, so the operation does not wait for it; at most one a COOLDOWN (a second click only says it just did).
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from pathlib import Path

from covey.backends.agy import accounts as agy_accounts
from covey.core import quiet_hours, quota
from covey.core.state import ensure_private_dir, read_json, write_json

COOLDOWN = 300  # Seconds between two readings the page asked for.
BUSY_MAX = 900  # A reading begun longer ago than this is not still going (the process died).
TIGHT = 0.2  # A window with less than this left is red on the page, and a source with no more than this everywhere is at its bottom.
AGY_STALE = 3600  # A spare account's /usage read is taken at a switch: older than this is grey on the page, whatever the ttl.
REFRESH_FILE = "refresh.json"
WHY_CHARS = 60  # A failed read's words on the page.
FIRST = agy_accounts.GROUPS[0]


def _scrub(text, limit) -> str:
    text = quota.scrub(text, limit)
    return "" if text == quota.CUT else text


def window_view(window) -> dict:
    used = window.get("used")
    shown = {"kind": window.get("kind") if window.get("kind") in quota.WINDOW_KINDS else "other",
             "label": _scrub(window.get("label"), quota.LABEL_CHARS),
             "left": None if used is None else round(1 - float(used), 4)}
    for key in ("resets_at", "checked_at", "ceiling"):
        if window.get(key) is not None:
            shown[key] = window[key]
    return shown


def pool_view(key, label, row) -> dict:
    return {"key": key, "label": label, "active": row.get("active") is True, "state": row.get("state"),
            "available_at": row.get("available_at"), "windows": [window_view(w) for w in row.get("windows") or ()]}


def account_view(row, pools) -> dict:
    """``row``: the first snapshot row of the account; ``pools``: its pools as ``pool_view`` made them."""
    shown = {"name": _scrub(row.get("alias"), quota.ALIAS_CHARS) or "?", "state": row.get("state"),
             "available_at": row.get("available_at"), "pools": pools}
    if quota.login_ok(row.get("login")):
        shown["login"] = row["login"]
    return shown


def label_of(name, shown) -> str:
    """What the page calls an account: the e-mail it signed in with, else its name."""
    return shown.get("login") or shown.get("name") or name


def bottom(rows) -> tuple:
    """(low, dry, back): of the accounts' (state, windows' left, available_at): the source is ``low`` when no account in a state
    to be used has TIGHT or more left in every window it has read, ``dry`` when no account can be used though some come back by
    themselves (``back``: the earliest), ``low`` too then."""
    if not rows:
        return False, False, None
    usable = [r for r in rows if r[0] == quota.OK]
    roomy = [r for r in usable if all(left is None or left >= TIGHT for left in r[1])]
    coming = [r[2] for r in rows if r[0] in quota.WAITING and r[2]]
    low = not roomy and any(r[0] in (quota.OK, *quota.WAITING) for r in rows)
    dry = not usable and any(r[0] in quota.WAITING for r in rows)
    return low, dry, min(coming) if dry and coming else None


def need_login(rows) -> list:
    return [r for r in rows if r.get("state") == quota.LOGIN]


def agy_source(entries) -> dict | None:
    """The one source ``agy`` of the group snapshots in ``entries`` (see the module docstring)."""
    groups = {}
    for entry in entries:
        group = entry.get("group") if entry.get("group") in agy_accounts.GROUPS else None
        if group and isinstance(entry.get("snapshot"), dict):
            groups[group] = entry
    if not groups:
        return None
    order = [g for g in agy_accounts.GROUPS if g in groups]
    first = groups[order[0]]["snapshot"]
    names = [a["alias"] for a in first["accounts"]]
    recent = {g: set(groups[g].get("recent") or ()) for g in order}
    anyone = [g for g in order if recent[g]]
    rows = {g: {a["alias"]: a for a in groups[g]["snapshot"]["accounts"]} for g in order}
    accounts = []
    for index, name in enumerate(names):
        mine = [g for g in order if name in recent[g]] or anyone or order[:1]
        pools = []
        for g in mine:
            row = rows[g].get(name)
            if row is not None:
                pools.append(pool_view(g, agy_accounts.GROUP_LABELS.get(g, g), row))
        shown = account_view(first["accounts"][index], pools)
        states = {p["state"] for p in pools}
        for worst in (quota.LOGIN, quota.PAUSED):  # One sign-in or one pause is the account's, in every group.
            if worst in states:
                shown["state"] = worst
                break
        else:
            shown["state"] = quota.OK
        accounts.append(shown)
    low = dry = False
    back = []
    for g in order:  # Every account takes its turn in a group, drawn for it or not: the group is at its bottom as a whole.
        if not any(p["key"] == g for a in accounts for p in a["pools"]):
            continue
        was_low, was_dry, at = bottom([(r["state"], [1 - w["used"] for w in r.get("windows") or () if w.get("used") is not None],
                                        r.get("available_at")) for r in groups[g]["snapshot"]["accounts"]])
        low, dry = low or was_low, dry or was_dry
        if at:
            back.append(at)
    counted = quota.pool(first)
    ents = [groups[g] for g in order]
    return {"id": "agy", "kind": "agy", "title": "agy", "collected_at": min(e["snapshot"]["collected_at"] for e in ents),
            "ttl": first.get("ttl") or quota.MIN_TTL, "stale_after": AGY_STALE,
            "read_at": max(float(e.get("read_at") or 0) for e in ents), "failing": False, "why": "",
            "usable": counted["usable"], "total": counted["total"],
            "next_available_at": min(back) if dry and back else None, "low": low, "dry": dry, "expand": True,
            "accounts": accounts}


def plain_source(entry) -> dict:
    snapshot = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else None
    title = _scrub(entry.get("title") or (snapshot or {}).get("title") or entry.get("source"), quota.TITLE_CHARS)
    shown = {"id": entry["source"], "kind": entry.get("kind") if entry.get("kind") in quota.SOURCE_KINDS else "command",
             "title": title or entry["source"], "read_at": float(entry.get("read_at") or 0),
             "failing": bool(entry.get("error")), "why": _scrub(entry.get("error"), WHY_CHARS) if entry.get("error") else "",
             "collected_at": (snapshot or {}).get("collected_at") or 0,
             "ttl": (snapshot or {}).get("ttl") or quota.MIN_TTL}
    shown["stale_after"] = shown["ttl"]
    if snapshot is None:
        return {**shown, "usable": 0, "total": 0, "next_available_at": None, "low": False, "dry": False, "expand": False,
                "accounts": []}
    accounts = [account_view(row, [pool_view("", "", row)]) for row in snapshot["accounts"]]
    counted = quota.pool(snapshot)
    low, _, _ = bottom([(row["state"], [1 - w["used"] for w in row.get("windows") or () if w.get("used") is not None],
                         row.get("available_at")) for row in snapshot["accounts"]])
    dry = counted["total"] > 0 and counted["usable"] == 0 and not quota.none_signed_in(snapshot)
    if quota.link_ok(snapshot.get("link")):
        shown["link"] = snapshot["link"]
    expand = shown["kind"] not in ("agy", "gateway") and any(
        row["state"] in (quota.LOGIN, quota.SPENT) for row in snapshot["accounts"])
    return {**shown, "usable": counted["usable"], "total": counted["total"],
            "next_available_at": counted["next_available_at"] if dry else None, "low": low or dry, "dry": dry,
            "expand": expand, "accounts": accounts}


def refresh_state(state_dir, now) -> dict:
    """{at, busy, cooldown}: when the page's last 刷新 began, whether its reading is still going, the cooldown."""
    try:
        saved = read_json(Path(state_dir) / quota.FOLDER / REFRESH_FILE, {})
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    at, done = float(saved.get("at") or 0), float(saved.get("done_at") or 0)
    return {"at": at, "busy": bool(at) and done < at and now - at < BUSY_MAX, "cooldown": COOLDOWN}


def view(state_dir, now=None) -> dict:
    """The ``quota`` item (see the module docstring); ``sources`` is empty with nothing to show."""
    now = time.time() if now is None else now
    # A gateway without the address is not there; one that answered once and is absent in an older record still is.
    entries = [e for e in quota.Store(state_dir).entries() if not e.get("absent") or isinstance(e.get("snapshot"), dict)]
    sources = [s for s in [agy_source([e for e in entries if e.get("kind") == "agy"])] if s is not None]
    sources += [plain_source(e) for e in entries if e.get("kind") != "agy"]
    relogin = [{"source": s["title"], "name": label_of("?", a)} for s in sources for a in s["accounts"]
               if a["state"] == quota.LOGIN]
    for source in sources:
        source["relogin"] = [label_of("?", a) for a in source["accounts"] if a["state"] == quota.LOGIN]
    return {"at": now, "read_at": max([s["read_at"] for s in sources], default=0), "relogin": relogin, "sources": sources,
            "refresh": refresh_state(state_dir, now)}


def stamps(state_dir) -> tuple:
    """What a change of the item shows in: each source file's and the refresh record's modification time (a few stats)."""
    folder, found = Path(state_dir) / quota.FOLDER, []
    for name in (REFRESH_FILE, *sorted(p.name for p in _files(folder))):
        try:
            found.append((name, (folder / name).stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(found)


def _files(folder):
    try:
        return [p for p in folder.glob("*.json") if p.name not in ("notices.json", REFRESH_FILE)]
    except OSError:
        return []


def clock(now) -> str:
    return datetime.datetime.fromtimestamp(now, quiet_hours.ZONE).strftime("%H:%M")


def agy_command(apps) -> list:
    """agy's command of the Bridge's agy Harness (``apps``: name -> app); [] without one."""
    ctx = getattr(getattr(getattr((apps or {}).get("agy"), "harness", None), "context", None), "backend_cmd", None)
    return list(ctx) if ctx else []


class Refresher:
    """The page's 刷新 (see the module docstring). ``command()``: agy's command ([] without it); ``log(line)``."""

    def __init__(self, state_dir, command=lambda: [], log=lambda text: None, environ=None, clock_=time.time) -> None:
        self.state_dir, self.command, self.log, self.environ, self.clock = state_dir, command, log, environ, clock_
        self.lock = threading.Lock()
        self.thread = None

    @property
    def path(self) -> Path:
        return Path(self.state_dir) / quota.FOLDER / REFRESH_FILE

    def ask(self, now=None) -> str:
        """Begin a reading of every source, unless one began less than COOLDOWN ago; what to tell the page."""
        now = self.clock() if now is None else now
        with self.lock:
            last = refresh_state(self.state_dir, now)
            if last["busy"]:
                return f"正在重新读取（{clock(last['at'])} 开始），读完页面会更新"
            if last["at"] and now - last["at"] < COOLDOWN:
                return f"刚查过（{clock(last['at'])}）：{clock(last['at'] + COOLDOWN)} 之后再点才会重新读取"
            ensure_private_dir(self.path.parent)
            write_json(self.path, {"at": now, "done_at": 0})
            self.thread = threading.Thread(target=self.work, args=(now,), name="quota-refresh", daemon=True)
            self.thread.start()
        return "开始重新读取所有来源和每个号的额度，读完页面会更新"

    def work(self, began) -> None:
        """The reading itself, in its thread; never raises (what could not be read stays as the last time left it)."""
        from covey.core import quota_sources

        environ = os.environ if self.environ is None else self.environ
        try:
            if quota_sources.enabled(environ):
                found, _problem = quota_sources.sources(environ)
                quota_sources.refresh(self.state_dir, found, force=True)
                command = self.command()
                if command and agy_accounts.configured(environ):
                    agy_accounts.probe_accounts(self.state_dir, command, dict(environ), which="all")
                agy_accounts.snapshots(self.state_dir, environ)
        except Exception as exc:  # noqa: BLE001 - the page's reading stops here, the Bridge goes on
            self.log(f"quota refresh failed: {type(exc).__name__}")
        finally:
            try:
                write_json(self.path, {"at": began, "done_at": self.clock()})
            except OSError:
                pass
