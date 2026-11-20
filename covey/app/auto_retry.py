"""Automatic 检查并继续 after a turn that ended without a reply as one of KINDS: a model rate limit
(on Harnesses that declare ``rate_limit_retry``), an incomplete end (``incomplete_retry``), or a restart
that interrupted it (any Harness: RESTARTED, plan_restart).

A rate-limited turn is retried up to twice in a row, an incomplete or interrupted one once, ``delays``
seconds after each failure. Each kind counts its own retries along a chain (the job's ``auto_retry``,
``auto_continue`` and ``auto_resume``; a retry's job carries them on), so they add up and never
multiply. The worker that delivered the failure, or the Bridge that marked the task interrupted (a
restart found it unfinished, or its task runner got a SIGTERM), writes the schedule into that job's
record (``auto_retry_state`` "pending", ``auto_retry_at``, ``auto_retry_attempt``, ``auto_retry_kind``;
none means rate-limited);
the Bridge's outbox loop runs ``tick``, which fires it through TaskControls.retry_topic, the
path of the button, so a restart keeps it. The state then moves on, under the record lock, to
"submitting" and "queued", or to "cancelled" (``auto_retry_note`` says why), or to "uncertain"
when a submit raised or a Bridge stopped during it: an uncertain one never runs again (AUTO_RETRY_TRANSITIONS).
Clicking 检查并继续 or 停止自动重试, a newer job in the topic, or a model switch cancels it.
Older Bridges and task runners ignore these keys; one of 74023f9 runs an incomplete one as its first
rate-limit retry.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from pathlib import Path
import time
from typing import NamedTuple

from covey.app.outbox import RETRY_NOTE, retry_label
from covey.cards.recovery import (FOLLOW_UP_NOTES, FOLLOW_UPS, SWITCH_MODEL, failure_details, interrupted_summary,
                                  result_actions, result_note, retry_status, switch_route)
from covey.core import failure
from covey.core.harness import backend_capabilities
from covey.core.jobs import find_job_record, job_key, select_job_records, update_job_record_if
from covey.core.result import INCOMPLETE_STATUS, STATUS_MESSAGES
from covey.core.saved_results import has_saved_reply
from covey.core.topics import context_for_topic, read_topic, topic_label

CANCEL_KIND = "bridge_job_retry_cancel"
RATE_LIMITED, INCOMPLETE, RESTARTED = "rate_limited", "incomplete", "restarted"


@dataclasses.dataclass(frozen=True)
class Kind:
    """A failure retried automatically, and how its card and record say so."""

    outcome: tuple  # The (status, error_code) of the turn's ExecutionOutcome.
    capability: str  # The HarnessCapabilities flag of the Harnesses it applies to; "" for every Harness.
    count: str  # The job field counting this kind's retries along a chain.
    delays: tuple  # Seconds before the first, the second, ... retry of a chain.
    reason: str  # What its status lines start with.
    action: str  # What the retry does, as they say it.
    failed_status: str  # The record's status line with no retry pending: turn_settle._settle_status's (core/result.py).
    moved: str  # How the failed task's card says its task went on.


KINDS = {
    RATE_LIMITED: Kind(("failed", "rate_limited"), "rate_limit_retry", "auto_retry", (60, 180), "模型服务限流",
                       "自动重试", STATUS_MESSAGES["rate_limited"], "限流后自动重试"),
    # Claude's main task ended while subtasks or later work had not: 10 of 31 failures in 9 days (2026-10).
    INCOMPLETE: Kind(("timed_out", "settle_timeout"), "incomplete_retry", "auto_continue", (60,), "本轮未全部完成",
                     "自动检查并继续", INCOMPLETE_STATUS, "未全部完成后自动检查并继续"),
    # A restart (the 2026-11-02 22:09 container restart left T331 #2407 unfinished) or a SIGTERM to the task
    # runner ended the turn: once, after the Bridge is up again (owner's choice, T314 2026-11-02).
    RESTARTED: Kind(("interrupted", "restart"), "", "auto_resume", (60,), "本轮被重启打断", "自动检查并继续",
                    "interrupted by a restart or SIGTERM before this job reached a terminal state", "重启后自动检查并继续"),
}
CANCELLED = {
    "stopped": "自动重试已停止。需要时点“检查并继续”。",
    "new_message": "本话题有了新任务，自动重试已取消。",
    "model_switch": "本话题已换配置，自动重试已取消。",
    "unavailable": "自动重试没有执行：本话题或它的 Harness 已不可用。",
    "refused": "自动重试没有执行。需要时点“检查并继续”。",
    "uncertain": "自动重试没有确认是否已提交，不会再自动重试；请先查看本话题，需要时再点“检查并继续”。",
}


# Where a scheduled retry's auto_retry_state ("pending": plan with worker's schedule_fields, schedule_restart) may
# go, each step through advance under the record lock. The one other change: a policy stop cancels it from any
# state (worker, policy_guard, core/statuses.py merge_job_record).
AUTO_RETRY_TRANSITIONS = {
    "pending": ("submitting", "cancelled"),
    "submitting": ("queued", "uncertain", "cancelled"),
}


class Planned(NamedTuple):
    """The automatic retry a finished turn gets."""

    due_at: float
    attempt: int
    kind: str = RATE_LIMITED


def pending(record):
    return record.get("auto_retry_state") == "pending"


def kind_of(record):
    """The kind of ``record``'s automatic retry; records from before kinds were rate-limited."""
    # compat(84ad62a): a record from before kinds has none, and was rate-limited; retire when no job record with
    # an automatic retry lacks auto_retry_kind (compat_report: auto-retry-kind)
    kind = record.get("auto_retry_kind")
    return kind if kind in KINDS else RATE_LIMITED


def applies(app, ctx, kind):
    """Whether ``kind`` retries the tasks of ``app``'s Harness (in ``ctx``)."""
    flag = KINDS[kind].capability
    return not flag or bool(getattr(backend_capabilities(app.backend, ctx), flag))


def status_line(due_at, attempt, kind=RATE_LIMITED):
    """The record's status line while a retry is pending; the topic notice says it too."""
    at = _dt.datetime.fromtimestamp(due_at).astimezone().strftime("%H:%M")
    spec = KINDS[kind]
    return f"{spec.reason}，将于 {at} {spec.action}（第 {attempt}/{len(spec.delays)} 次）"


def queued_line(kind, attempt):
    """The retry's own task card while it is queued."""
    spec = KINDS[kind]
    return f"{spec.action}已入队（第 {attempt}/{len(spec.delays)} 次）"


def plan(harness, ctx, job, turn, now=None):
    """The Planned retry this finished turn gets, or None: a turn that ended as one of KINDS with no
    reply, on a Harness that declares the kind, that 检查并继续 would queue again, at most
    len(delays) times of its kind in a chain. A turn under a task timeout is not continued after an
    incomplete end: the timeout may be what ended it."""
    outcome = turn.outcome
    if outcome is not None and getattr(outcome, "policy_event", None) is not None:
        return None  # Even a cleanup/transport failure cannot erase a latched policy stop.
    kind = next((name for name, spec in KINDS.items()
                 if outcome is not None and (outcome.status, outcome.error_code) == spec.outcome), None)
    if (kind is None or turn.verified_delivery is not None or not job.topic_id
            or job.cancel_event.is_set() or (kind == INCOMPLETE and (job.timeout or 0) > 0)):
        return None
    spec = KINDS[kind]
    done = int(getattr(job, spec.count) or 0)
    if done >= len(spec.delays) or not applies(harness, ctx, kind):
        return None
    record = find_job_record(ctx.state_dir, job_key(job.message_id, job.job_id)) or {}
    if (has_saved_reply(ctx, record) or record.get("result_retry_policy") == "native_answer_once"
            or record.get("paused_question_delivery") is not None):
        return None  # 检查并继续 would redeliver or refuse it, not queue it again.
    return Planned((time.time() if now is None else now) + spec.delays[done], done + 1, kind)


def plan_restart(record, now=None):
    """The Planned retry of ``record``, a chat task a restart left interrupted (``interrupted_by`` "restart" or
    "sigterm", core/jobs.py interrupted_fields), or None: one that 检查并继续 would queue again (not a stop the
    user asked for, an official refusal or a saved reply to deliver), once along a chain."""
    spec = KINDS[RESTARTED]
    if (record.get("status") != "interrupted" or record.get("interrupted_by") not in ("restart", "sigterm")
            or record.get("kind") != "chat" or not record.get("topic_id") or record.get("cancel_requested_by")
            or record.get("retried_by") or record.get("auto_retry_state") in ("pending", "submitting")
            or retry_label(record) != "检查并继续"):
        return None
    done = int(record.get(spec.count) or 0)
    if done >= len(spec.delays):
        return None  # Itself the automatic retry after a restart: a second restart leaves it to the user.
    return Planned((time.time() if now is None else now) + spec.delays[done], done + 1, RESTARTED)


def advance(state_dir, key, old, new, **fields):
    """Move job ``key``'s automatic retry from ``old`` to ``new`` (AUTO_RETRY_TRANSITIONS) with ``fields``, under
    its record lock: the record, or None when it is no longer ``old`` (then nothing is written)."""
    if new not in AUTO_RETRY_TRANSITIONS[old]:
        raise ValueError(f"auto retry {old} -> {new}")
    return update_job_record_if(state_dir, key, lambda r: r.get("auto_retry_state") == old,
                                auto_retry_state=new, **fields)


def schedule_restart(ctx, record, now=None):
    """Schedule ``record``'s automatic 检查并继续 when a restart interrupted it (plan_restart): the record as it
    is then, unchanged when none applies."""
    planned = plan_restart(record, now)
    if planned is None:
        return record
    current = update_job_record_if(ctx.state_dir, record["key"], lambda r: plan_restart(r, now) is not None,
                                   **schedule_fields(planned),
                                   status_message=status_line(planned.due_at, planned.attempt, RESTARTED))
    return current or record


def schedule_fields(planned):
    """The record fields that schedule ``planned``; {} for None."""
    if planned is None:
        return {}
    return {"auto_retry_state": "pending", "auto_retry_at": planned.due_at, "auto_retry_attempt": planned.attempt,
            "auto_retry_kind": planned.kind, "auto_retry_note": ""}


def chain_counts(record, attempt):
    """The retry counts of the job that retries ``record``: none after a click (``attempt`` 0); after
    the automatic retry, ``attempt`` for its kind, and the other kind's count as ``record``'s job had it."""
    if not attempt:
        return {spec.count: 0 for spec in KINDS.values()}
    counts = {spec.count: int(record.get(spec.count) or 0) for spec in KINDS.values()}
    counts[KINDS[kind_of(record)].count] = attempt
    return counts


def next_steps(record):
    """(the label of the 检查并继续 click on ``record``'s failed task card, its follow-ups): what core/failure.py
    failure_view offers. A rate limit's click reads 现在重试; 看原因 and 拆小再试 start turns of their own."""
    view = failure.failure_view(record) if record else None
    actions = (view or {}).get("actions", [])
    label = failure.LABELS[failure.RETRY] if failure.RETRY in actions else "检查并继续"
    return label, tuple(name for name in actions if name in FOLLOW_UPS
                        or (name == SWITCH_MODEL and record and switch_route(record)))


def actions(topic_id, job_id, can_stop, record=None):
    """The 检查并继续 row of a failed task card (``next_steps`` of ``record``); a pending automatic retry adds
    停止自动重试."""
    label, follow = next_steps(record)
    return result_actions(topic_id, job_id, label, stop_kind=CANCEL_KIND if can_stop else "", follow_ups=follow,
                          route=switch_route(record or {}))


def result_note_text(label, follow):
    note = RETRY_NOTE if label == "检查并继续" else label + "：" + RETRY_NOTE.split("：", 1)[1]
    return "\n".join([note, *(FOLLOW_UP_NOTES[name] for name in follow)])


def result_elements(topic_id, job_id, record):
    """What a failed task card shows after its answer when 检查并继续 applies: a pending
    automatic retry's status, the buttons and the note."""
    label, follow = next_steps(record)
    elements = [actions(topic_id, job_id, pending(record), record), result_note(result_note_text(label, follow))]
    if pending(record):
        kind = kind_of(record)
        how = "，做法同“检查并继续”" if kind == RATE_LIMITED else ""
        elements.insert(0, retry_status(
            f"⏳ {status_line(record['auto_retry_at'], record['auto_retry_attempt'], kind)}{how}。"
            f"现在点“{label}”会立即重试；点“停止自动重试”、在本话题发消息或换配置会取消这次自动重试。"))
    return elements


def show(app, ctx, record, text):
    """Say on the failed task card what became of its retry; only 检查并继续 stays. An interrupted task's card
    (RESTARTED) said in its chat-list preview when it would continue: that goes too."""
    try:
        ok = app.cards.update_record_elements(ctx, record, [retry_status(text), actions(record["topic_id"],
                                                                                   record["job_id"], False, record)])
        if kind_of(record) == RESTARTED:
            name = topic_label(ctx.state_dir, record.get("topic_id", "")) or f"#{record.get('job_id')}"
            ok = app.cards.set_record_summary(ctx, record, interrupted_summary(name, "检查并继续")) and ok
        if not ok:
            app.runtime.log(f"auto retry card update for job #{record.get('job_id')} not confirmed")
    except Exception as exc:  # noqa: BLE001 - the record already says it; the card is best effort
        app.runtime.log(f"auto retry card update failed: {type(exc).__name__}")


def cancel(app, ctx, record, reason):
    """Cancel ``record``'s pending retry; whether this call cancelled it. A manual 检查并继续
    already replaced the card; other reasons are shown on it. ``app`` (its cards and log) is None
    for a Harness no longer in the roster."""
    current = advance(ctx.state_dir, record["key"], "pending", "cancelled", auto_retry_note=reason,
                      status_message=KINDS[kind_of(record)].failed_status)
    if current is None or app is None:
        return current is not None
    app.runtime.log(f"auto retry of job #{record.get('job_id')} cancelled: {reason}")
    if reason != "manual":
        show(app, ctx, current, CANCELLED[reason])
    return True


def tick(gateway, ctx, *, now=None):
    """Settle every scheduled retry: resolve one a stopped Bridge left mid-submit, cancel one whose
    topic moved on, fire one that is due. Runs every few seconds in the Bridge's outbox loop."""
    now = time.time() if now is None else now
    retries = select_job_records(ctx.state_dir, lambda r: r.get("auto_retry_state") in ("submitting", "pending"),
                                 log=gateway.runtime.log)
    # What the checks read of the other records, copied once before any change: the jobs that name a
    # retry (retry_of) and the jobs in a pending retry's topic. Not every record (~0.3 s each tick).
    keys = [r["key"] for r in retries if r.get("auto_retry_state") == "submitting"]
    topics = [(r.get("topic_id"), r.get("chat_id")) for r in retries if pending(r)]
    records = select_job_records(ctx.state_dir, lambda r: any(r.get("retry_of") == key for key in keys) or any(
        r.get("topic_id") == topic and r.get("chat_id") == chat for topic, chat in topics),
        log=gateway.runtime.log) if retries else []
    for record in retries:
        if record.get("auto_retry_state") == "submitting":
            # Never fired twice: a job that names this one went out, or it is uncertain.
            queued = bool(record.get("retried_by")) or any(r.get("retry_of") == record["key"] for r in records)
            advance(ctx.state_dir, record["key"], "submitting", "queued" if queued else "uncertain")
        elif pending(record):
            if gateway.runtime.jobs.settling(record.get("message_id")):
                continue  # Its task runner has not exited yet.
            settle(gateway, ctx, record, records, now)


def settle(gateway, ctx, record, records, now):
    owner = gateway.apps.get(record.get("backend"))
    if owner is None or not applies(owner, owner.harness.context, kind_of(record)):
        cancel(owner, ctx, record, "unavailable")
        return
    reason, target = checked_target(gateway, owner, record, records, due=record.get("auto_retry_at", 0) <= now)
    if reason:
        cancel(owner, ctx, record, reason)
    elif target is not None:
        fire(owner, ctx, target, record)


def checked_target(gateway, owner, record, records, *, due=True):
    """(why ``record``'s retry must not run or '', the topic's context to run it in when ``due`` and it may run, else
    None). Also for a retry after a safety refusal (app/policy_retry.py)."""
    base, target = owner.harness.context, None
    try:
        topic = read_topic(dataclasses.replace(base, history_path=Path(record["history_path"])))
        reason = blocker(gateway, record, records, topic)
        if not reason and due:
            # The topic's context, as a click on its card builds it.
            # The butler's topic has no root: the job's own message routes to it.
            event = {"chat_id": record["chat_id"], "message_id": topic["root_id"] or record["message_id"],
                     "root_id": topic["root_id"]}
            target = owner.models.configured_context(context_for_topic(base, event, owner.store.sessions_dir))
            if target.topic_id != record["topic_id"]:
                reason = "unavailable"
    except (OSError, ValueError, KeyError, TypeError):
        reason = "unavailable"
    return reason, (None if reason else target)


def blocker(gateway, record, records, topic):
    """Why ``record``'s pending retry must not run, or ''."""
    from covey.app.binding import stale_job_card

    if record.get("retried_by"):
        return "manual"
    if topic.get("chat_id") != record.get("chat_id") or topic.get("harness_id") != record.get("backend") \
            or stale_job_card(topic, record):
        return "model_switch"
    # A 补充 taken into this task (steering.py, merged_into_job_id) is no new task: four such cancelled the
    # continue after the 2026-11-03 restarts (#2614, #2619, #2621, #2634).
    same_topic = [r for r in records if r.get("topic_id") == record["topic_id"] and r.get("chat_id") == record["chat_id"]
                  and str(r.get("merged_into_job_id")) != str(record["job_id"])]
    if any(int(r.get("job_id") or 0) > int(record["job_id"]) for r in same_topic):
        return "new_message"
    if gateway.runtime.jobs.topic_jobs(record["topic_id"], record["chat_id"]):
        return "new_message"
    return ""


def fire(app, ctx, target, record):
    """Claim ``record``'s retry, then queue it as the 检查并继续 button does; never sent twice."""
    key, attempt, spec = record["key"], int(record.get("auto_retry_attempt") or 1), KINDS[kind_of(record)]
    if advance(ctx.state_dir, key, "pending", "submitting") is None:
        return
    try:
        app.controls.retry_topic(target, record["chat_id"], f"auto-retry-{record['job_id']}-{attempt}",
                                 job_id=record["job_id"], automatic=attempt)
    except Exception as exc:  # noqa: BLE001 - its card may have gone out: never retried automatically
        app.runtime.log(f"auto retry of job #{record['job_id']} uncertain: {type(exc).__name__}")
        current = advance(ctx.state_dir, key, "submitting", "uncertain", auto_retry_note=type(exc).__name__)
        if current is not None:
            show(app, ctx, current, CANCELLED["uncertain"])
        return
    current = find_job_record(ctx.state_dir, key) or {}
    queued = bool(current.get("retried_by"))
    current = advance(ctx.state_dir, key, "submitting", "queued" if queued else "cancelled",
                      auto_retry_note="" if queued else "refused",
                      status_message=(f"{spec.reason}，已{spec.action}（第 {attempt}/{len(spec.delays)} 次）"
                                      if queued else spec.failed_status))
    app.runtime.log(f"auto retry of job #{record['job_id']} {'queued' if queued else 'refused'} (attempt {attempt})")
    if not queued and current is not None:
        show(app, ctx, current, CANCELLED["refused"])


def failed_controls(topic_id, job_id, record) -> list:
    """The elements a failed task card gets after its answer (app/cards.py Cards.finish_result_controls inserts
    them): 技术详情, the words its body said before its one-line reason (app/turn_delivery.py card_text); its retry
    button as ``retry_label`` names it, with a pending automatic retry's status and 停止自动重试 when that is
    检查并继续 (``result_elements``), and the follow-ups core/failure.py offers (看原因, 拆小再试; a policy stop
    gets 拆小再试 alone); [] when the record offers none."""
    view = failure.failure_view(record) if record else None
    # Only where the body says the reason instead (app/turn_delivery.py card_text, which wrote failure_code).
    details = ([failure_details(str(record["final_text"]))]
               if view and record.get("failure_code") and record.get("execution_status") == "failed"
               and not record.get("policy_stop") and record.get("final_text") else [])
    label = retry_label(record)
    if label == "检查并继续":
        return details + result_elements(topic_id, job_id, record)
    follow = next_steps(record)[1]
    if not label and not follow:
        return details
    return details + [result_actions(topic_id, job_id, label, follow_ups=follow, route=switch_route(record))]

