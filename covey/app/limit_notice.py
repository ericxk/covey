"""One notice in the owner's private chat when several topics stopped on the same cause with an automatic retry
planned (app/auto_retry.py): COVEY_LIMIT_NOTICE, on unless 0.

One account's limit (or one server's fault) usually stops several topics at once. Each topic's own task card says
when its retry goes on; this adds the one line at the top level that says it for all of them: "N 个话题同样停在
模型服务限流" with each topic and the time it goes on, the limit's reset when it is known.

- Which: pending automatic retries of kinds in auto_retry.LONG_KINDS (a rate limit, a server fault) of a topic's first
  stop in a chain (a retry that failed again is no new stop: its topic is told already), in the owner's private chat,
  the same Harness and the same kind. Within WINDOW seconds of the first stop of such a group, two topics or more make
  one notice, sent once SETTLE seconds have passed since the last of them, so a burst is one message. A later stop of
  the same group in that window is not told again; a stop after it makes a new window.
- Never held or merged: a stop with no automatic retry (the butler's 停了 relay, app/butler_relay.py) goes out as it
  did, at once and one each, since it waits for the owner; the first stop of a group goes out as before, with its own
  card. Only the notice for the ones that go on by themselves is one line for all.
- Once (rule 6, as app/relay_send.py): ``limit-notices/<group>.json`` says ``sending`` under a file lock no Feishu
  call is made under, before the send, then ``sent``, or ``uncertain``; a window with a record is never sent again,
  uncertain included. Run by the Bridge's outbox loop (app/unified.py OUTBOX_STEPS), every TICK seconds at most. Off:
  nothing is read or sent.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from covey.app import auto_retry
from covey.core import env as cenv
from covey.core.board_read import ended_at
from covey.core.jobs import select_job_records
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json
from covey.core.topics import label_title

SWITCH = "COVEY_LIMIT_NOTICE"
FOLDER = "limit-notices"
WINDOW = 600  # Seconds from the first stop of a group: the stops of that window make one notice.
SETTLE = 45  # Seconds after the last stop of a burst before its notice goes.
TICK = 20  # The Bridge's outbox loop runs ``tick`` every few seconds; the records are looked at this often.
MAX_LISTED = 8
CAUSES = {"rate_limited": "模型服务限流", "server_fault": "模型服务临时故障"}
_last = {}  # state dir -> when ``tick`` last looked.


def enabled() -> bool:
    return str(cenv.get(SWITCH, "1")).strip() != "0"


def first_stop(record) -> bool:
    """Whether ``record``'s turn is the first stop of its chain: no automatic retry of any kind led to it."""
    return not any(int(record.get(spec.count) or 0) for spec in auto_retry.KINDS.values())


def stops(state_dir, chat_id):
    """The pending automatic retries of the owner's chat that a notice may tell of, newest stop last."""
    found = select_job_records(Path(state_dir), lambda r: (
        r.get("auto_retry_state") == "pending" and r.get("chat_id") == chat_id and r.get("topic_id")
        and auto_retry.kind_of(r) in auto_retry.LONG_KINDS and first_stop(r)))
    return sorted(found, key=ended_at)


def group_key(chat_id, backend, kind) -> str:
    return hashlib.sha256(f"{chat_id}|{backend}|{kind}".encode()).hexdigest()[:16]


def path(state_dir, group) -> Path:
    return Path(state_dir) / FOLDER / f"{group}.json"


def notice_text(state_dir, members, backend_title, kind):
    """The notice for ``members`` (job records, in order): the cause, each topic and when it goes on."""
    lines = [f"**{len(members)} 个话题同样停在{CAUSES[kind]}**（{backend_title}），已各自排好自动继续，到点会自己接着做："]
    for record in members[:MAX_LISTED]:
        label, title = label_title(state_dir, record["topic_id"], "")
        name = " ".join(part for part in (label, title) if part) or f"任务 #{record.get('job_id')}"
        at = auto_retry.clock(record["auto_retry_at"])
        reset = record.get("auto_retry_reset_at")
        lines.append(f"- {name} · {at}" + (f"（限流预计 {auto_retry.clock(reset)} 恢复）" if reset else ""))
    if len(members) > MAX_LISTED:
        lines.append(f"- …另有 {len(members) - MAX_LISTED} 个")
    lines.append("要现在继续，在各话题的任务卡上点“现在重试”；不想自动继续，点“停止自动重试”。")
    return "\n".join(lines)


def plan(state_dir, group, members, now):
    """What to do for ``group``'s ``members`` (its pending stops, oldest first): (window anchor, episode key, members
    to tell of) when a notice is to go out now and its window has none yet, else None. Reads under the lock; the
    caller marks and sends."""
    saved = read_object(path(state_dir, group))
    anchor = float(saved.get("anchor") or 0)
    if saved and now < anchor + WINDOW:
        return None  # This window's notice is out or on its way; a later stop of it is not told again.
    fresh = [r for r in members if ended_at(r) > anchor + WINDOW]
    if not fresh:
        return None
    start = ended_at(fresh[0])
    window = [r for r in fresh if ended_at(r) <= start + WINDOW]
    if len({r["topic_id"] for r in window}) < 2 or now < ended_at(window[-1]) + SETTLE:
        return None
    return start, window


def tick(gateway, ctx, *, now=None):
    """An outbox-loop step: the notice for each group of stops that is due. Never raises (the retries do not depend on it)."""
    if not enabled():
        return
    now = time.time() if now is None else now
    if now - _last.get(str(ctx.state_dir), 0) < TICK:
        return
    _last[str(ctx.state_dir)] = now
    try:
        chat = gateway.runtime.access.owner_chat()
        if chat:
            send_due(gateway, ctx, chat, now)
    except Exception as exc:  # noqa: BLE001 - a notice never holds back what the Bridge does next
        gateway.runtime.log(f"limit notice failed: {type(exc).__name__}")


def send_due(gateway, ctx, chat, now):
    state = Path(ctx.state_dir)
    groups = {}
    for record in stops(state, chat):
        groups.setdefault((record.get("backend"), auto_retry.kind_of(record)), []).append(record)
    for (backend, kind), members in groups.items():
        group = group_key(chat, backend, kind)
        with resource_lock("record", state / FOLDER):
            due = plan(state, group, members, now)
            if due is None:
                continue
            anchor, window = due
            # Written before the send, under the lock, no Feishu call in it: a window with a record is never sent again.
            write_json(path(state, group), {"chat_id": chat, "backend": backend, "kind": kind, "anchor": anchor,
                                            "state": "sending", "topics": [r["topic_id"] for r in window],
                                            "jobs": [r.get("job_id") for r in window]})
        app = gateway.apps.get(backend)
        title = app.spec.title if app is not None else str(backend)
        key = gateway.voice.safe_key(f"limit-notice-{group}-{int(anchor)}")
        ok = False
        try:
            ok = bool(gateway.messenger.send_message(ctx.profile, chat, notice_text(state, window, title, kind), key,
                                                     voice=gateway.voice))
        finally:
            with resource_lock("record", state / FOLDER):
                saved = read_object(path(state, group))
                write_json(path(state, group), {**saved, "state": "sent" if ok else "uncertain", "at": now})
        gateway.runtime.log(f"limit notice {'sent' if ok else 'uncertain'} kind={kind} topics={len(window)}")
