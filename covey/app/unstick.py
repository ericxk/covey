"""A butler's or a lead's turn whose model request hangs no longer holds up what the owner said after it (T784).

The butler takes the owner's next words as a supplement of its running turn (app/butler.py supplement), which Claude Code
reads only between two tool calls; with 多路并行 a busy track keeps a new message queued behind its turn. A turn whose
model request hung held them as long as it hung (an hour once), and nothing said what they waited for. Each tick of the
turn's task monitor (core/longtask.py ``watch``) now calls ``check``. While the turn gives no sign of life (no progress
of its own, not waiting on a command, a question, a retry or a queue for a seat) and something of the owner's waits
behind it (an unread supplement, ``core/deliveries.py unread_of``; a queued message of the same topic):

- after NOTICE_SECONDS, each waiting message gets one line under it that says which turn it waits for and for how long
  it has been quiet (``waiting_words``);
- after ``hung_seconds`` (COVEY_BUTLER_HUNG_MINUTES, 10 by default; 0 turns this off) the turn is stopped by the Bridge:
  ``job.hung_stop`` and its stop flag, so it ends failed with HUNG_STATUS (app/worker.py), 检查并继续 offered, never as the
  owner's stop. What waited then runs as after any end: an unread supplement as a turn of its own (app/steering.py
  requeue_lost), a queued message as the topic's next turn.

A topic's turn is never stopped here: its card has 停止 and 看看. The disk is looked at no more than every LOOK_EVERY
seconds, and only once the turn has been quiet NOTICE_SECONDS.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from covey.core import env
from covey.core.butler import traits_of
from covey.core.deliveries import unread_of
from covey.core.jobs import job_key
from covey.core.result import STATUS_MESSAGES
from covey.core.run_dir import QUEUED
from covey.core.text import cut, stamp

SWITCH = "COVEY_BUTLER_HUNG_MINUTES"
HUNG_MINUTES = 10
NOTICE_SECONDS = 180
LOOK_EVERY = 30
HUNG_STATUS = STATUS_MESSAGES["request_hung"]  # core/failure.py names it request_hung from these words.


def hung_seconds() -> float:
    """COVEY_BUTLER_HUNG_MINUTES (HUNG_MINUTES unset or not a whole number of 0 to 1440) in seconds; 0: never stopped."""
    raw = str(env.get(SWITCH, "") or "").strip()
    try:
        minutes = int(raw) if raw else HUNG_MINUTES
    except ValueError:
        minutes = HUNG_MINUTES
    return float((minutes if 0 <= minutes <= 1440 else HUNG_MINUTES) * 60)


def quiet_for(facts, now) -> float | None:
    """How long the turn of ``facts`` (core/status_line.py Facts) has given no sign of life, None while it may not be
    hung: it waits on a command, the owner's answer, a retry of its request or a seat, or its process is gone."""
    found = facts.progress or {}
    if (facts.waiting or facts.retry or getattr(facts, "queue", None) or found.get("command")
            or facts.alive is False):
        return None
    at = found.get("last_activity_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return max(0.0, now - at)


def queued_behind(state_dir, job) -> list:
    """The jobs queued in ``job``'s topic after it (their queued copies, app/detached.py), each {message_id, job_id,
    user_text}; read only."""
    found = []
    try:
        paths = sorted((Path(state_dir) / QUEUED).glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            queued = json.loads(path.read_text()).get("job") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if (isinstance(queued, dict) and queued.get("topic_id") == job.topic_id
                and str(queued.get("job_id")) != str(job.job_id) and queued.get("message_id")):
            found.append({"message_id": str(queued["message_id"]), "job_id": queued.get("job_id"),
                          "chat_id": queued.get("chat_id") or job.chat_id})
    return found


def waiting_behind(state_dir, job) -> list:
    """What of the owner's waits behind ``job``'s turn: its unread supplements and the topic's queued messages, each
    {message_id, chat_id}."""
    since = (getattr(job, "timings", None) or {}).get("backend_requested", 0)
    supplements = [{"message_id": str(record.get("mid")), "chat_id": job.chat_id}
                   for record in unread_of(state_dir, job_key(job.message_id, job.job_id), since)
                   if record.get("mid") and not record.get("internal")]  # Not the Bridge's own reminder (W2-A).
    return supplements + queued_behind(state_dir, job)


def waiting_words(job, quiet) -> str:
    """The line under a message that waits behind ``job``'s quiet turn."""
    asked = cut(getattr(job, "user_text", ""), 24)
    when = stamp(getattr(job, "created_at", None) or (getattr(job, "timings", None) or {}).get("submitted"), "%H:%M")
    which = f"#{job.job_id}" + (f"（{when} 的「{asked}」）" if asked and when != "—" else f"（「{asked}」）" if asked else "")
    limit = hung_seconds()
    after = (f"到 {int(limit // 60)} 分钟仍没动静会停下它，先跑这句" if limit and quiet < limit else "")
    return f"这句话在等前一轮 {which}：它已 {int(quiet // 60)} 分钟没有动静，可能卡在模型请求上" + (f"；{after}。" if after else "。")


def check(ctx, job, facts, *, reply=None, log=None, now=None) -> bool:
    """One tick of ``job``'s turn (see the module): tell what waits behind a quiet turn, once a message, and stop a hung
    one. ``reply(ctx, chat_id, message_id, text)`` puts a line under a message. Whether the turn was stopped."""
    if not traits_of(job).lists_supplements or job.cancel_event.is_set() or getattr(job, "hung_stop", None):
        return False
    now = time.time() if now is None else now
    quiet = quiet_for(facts, now)
    if quiet is None or quiet < NOTICE_SECONDS or now - getattr(job, "hung_looked", 0.0) < LOOK_EVERY:
        return False
    job.hung_looked = now
    waiting = waiting_behind(ctx.state_dir, job)
    if not waiting:
        return False
    told = getattr(job, "hung_told", None)
    if told is None:
        told = job.hung_told = set()
    for item in waiting:
        if item["message_id"] in told or reply is None:
            continue
        told.add(item["message_id"])
        try:
            reply(ctx, item["chat_id"], item["message_id"], waiting_words(job, quiet))
        except Exception as exc:  # noqa: BLE001 - only the line is lost
            if log is not None:
                log(f"hung turn notice not sent job=#{job.job_id}: {type(exc).__name__}")
    limit = hung_seconds()
    if not limit or quiet < limit:
        return False
    job.hung_stop = {"quiet": int(quiet), "waiting": len(waiting)}
    if log is not None:
        log(f"hung turn stopped job=#{job.job_id} quiet_s={int(quiet)} waiting={len(waiting)}")
    job.cancel_event.set()
    return True


def stopped_text(job) -> str:
    """The stopped turn's status line and words (app/worker.py)."""
    found = getattr(job, "hung_stop", None) or {}
    minutes = max(1, int(found.get("quiet", 0)) // 60)
    waiting = int(found.get("waiting") or 0)
    then = f"，好让你之后发的 {waiting} 条消息先跑" if waiting else ""
    return f"{HUNG_STATUS}（{minutes} 分钟没有动静）{then}；可以点检查并继续接着做。"
