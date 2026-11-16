"""One retry on an older model after an official safety refusal (docs/policy-stop.md 换旧模型重试一次).

A Claude turn stopped by a refusal of a category in core/model_policy.py FALLBACK_MODELS (cyber → Opus 4.8,
bio → Opus 5) runs again once: the same request as a new job on that model, in a new native session (the refused
one is blocked), its subagents on the same model (``subagent_model``, core/jobs.py context_for_job). The topic's
binding stays: its next message runs on the topic's model. Fallbacks, other categories, a turn the owner stopped, a
butler turn (its topics opened, hand-offs and 代答 would run again without memory of them) and the retry itself
(its record's ``policy_retry``, which its continuations carry too) are not retried.

Both stop paths decide it the same way once the stop is final, through ``schedule``: the worker that delivers a
stop it saw in the stream (``at_stop``), and the policy guard after it stopped a task it claimed (app/policy_guard.py
finish; only a claim of this release, its intent's ``fallback_retry``: a stop recorded before is never retried).
``schedule`` writes ``policy_retry_state`` "pending" under the record lock only on a record that has none, so
duplicate stop events schedule one retry. The Bridge's outbox loop runs ``tick``, which queues it through
TaskControls.requeue once the task runner has exited, or cancels it (a newer job in the topic, a model switch:
auto_retry.blocker), along POLICY_RETRY_TRANSITIONS: never submitted twice, an uncertain submit is not retried.
A retry waits for the policy guard's stop of the same task and is cancelled when that left the process cleanup
unconfirmed. A cancelled retry takes its promise back from the card's body and status line
(``settle``). A separate field from auto_retry_state, which every policy stop sets to "cancelled" (core/statuses.py
merge_job_record).
"""
from __future__ import annotations

import time

from covey.app import auto_retry
from covey.cards.streaming import answer_element
from covey.core import butler
from covey.core.jobs import (context_for_record, find_job_record, job_key, select_job_records,
                                      update_job_record_if)
from covey.core.locks import topic_lock
from covey.core.butler import worded
from covey.core.model_policy import FALLBACK_MODELS, POLICY_RECOVERY, fallback_model, retry_recovery

# Where a retry's policy_retry_state ("pending", written by schedule) may go, each step through advance under the
# record lock.
POLICY_RETRY_TRANSITIONS = {
    "pending": ("submitting", "cancelled"),
    "submitting": ("queued", "uncertain", "cancelled"),
}
CANCELLED = {
    "new_message": "没有换模型重试：本话题有了新任务。",
    "model_switch": "没有换模型重试：本话题已换配置。",
    "manual": "没有换模型重试：本轮已经重新执行过。",
    "unavailable": "没有换模型重试：本话题或它的 Harness 已不可用。",
    "refused": "没有换模型重试：重试没能入队。",
    "uncertain": "换模型重试没有确认是否已提交，不会再自动重试；请先查看本话题。",
    "cleanup": "没有换模型重试：本轮的进程清理待确认。",
}
# How long a retry waits for the policy guard's repaint of the stopped card once its notice was tried
# (``guard_done``): a card CardKit keeps refusing does not hold the retry back for good.
CARD_WAIT = 60
# Where the task card shows its result (app/cards.py STREAM_CARD_ELEMENT_ID).
BODY_ELEMENT_ID = "answer"


def eligible(record, event):
    """The (model, name) ``record``'s stop on ``event`` is retried on, or None: a Claude chat turn in a topic
    other than the butler's, refused for a category FALLBACK_MODELS names, not already scheduled, not itself
    such a retry, not stopped by the owner, not retried otherwise, not already on that model or another provider's."""
    found = fallback_model(event)
    model = str(record.get("model") or "")
    if (found is None or record.get("backend") != "claude" or (record.get("kind") or "chat") != "chat"
            or not record.get("topic_id") or butler.is_butler_topic(record.get("topic_id"), record.get("chat_id"))
            or record.get("policy_retry") or record.get("policy_retry_state")
            or record.get("cancel_requested_by") or record.get("retried_by") or "/" in model or model == found[0]):
        return None
    return found


def planned(record):
    """The (model, name) of ``record``'s scheduled retry, or None."""
    model = record.get("policy_retry_model")
    if not record.get("policy_retry_state") or not model:
        return None
    return model, next((name for found, name in FALLBACK_MODELS.values() if found == model), model)


def schedule(state_dir, key, event):
    """Schedule job ``key``'s retry after its stop on ``event`` when it is eligible: the (model, name) its record
    has scheduled, by this call or an earlier one, else None. Idempotent."""
    found = fallback_model(event)
    if found is not None:
        current = update_job_record_if(state_dir, key, lambda r: eligible(r, event) is not None,
                                       policy_retry_state="pending", policy_retry_model=found[0],
                                       policy_retry_category=event["category"], policy_retry_note="")
        if current is not None:
            return planned(current)
    return planned(find_job_record(state_dir, key) or {})


def will_retry(found):
    """The stopped turn's status line goes on so while its retry is scheduled."""
    return f"将换 {found[1]} 重试。"


def stop_text(summary, event, found, of):
    """The stopped turn's text: ``summary`` and what comes next (a retry on ``found`` or POLICY_RECOVERY)."""
    return worded(summary + (retry_recovery(event["category"], found[1]) if found else POLICY_RECOVERY), of)


def at_stop(ctx, job, turn):
    """In the worker, before a turn stopped by a refusal in its stream is delivered: schedule its retry and say so
    in its text and status line. A turn the owner stopped, or whose cleanup is unconfirmed, gets none."""
    outcome = turn.outcome
    event = getattr(outcome, "policy_event", None)
    if (event is None or job.cancel_event.is_set() or job.policy_retry or not outcome.cleanup_confirmed
            or fallback_model(event) is None):
        return None
    found = schedule(ctx.state_dir, job_key(job.message_id, job.job_id), event)
    if found is None:
        return None
    usual = worded(POLICY_RECOVERY, job)
    if turn.final_text.endswith(usual):
        turn.final_text = turn.final_text[:-len(usual)]
    turn.final_text += worded(retry_recovery(event["category"], found[1]), job)
    turn.status_message = f"{turn.status_message}{will_retry(found)}"
    return found


def advance(state_dir, key, old, new, **fields):
    """Move job ``key``'s retry from ``old`` to ``new`` (POLICY_RETRY_TRANSITIONS) with ``fields``, under its record
    lock: the record, or None when it is no longer ``old``."""
    if new not in POLICY_RETRY_TRANSITIONS[old]:
        raise ValueError(f"policy retry {old} -> {new}")
    return update_job_record_if(state_dir, key, lambda r: r.get("policy_retry_state") == old,
                                policy_retry_state=new, **fields)


def unpromise(state_dir, record, reason):
    """``record``'s retry did not run (``reason``, CANCELLED): its status line, which status cards, 手上的事 and Pin
    read, no longer says one comes, nor the text a 重新投递 sends (POLICY_RECOVERY again, unless ``uncertain``: it may
    have been queued). Only while neither changed since (compare and set); the record, as it is now."""
    found = planned(record) or ("", "")
    status, text = str(record.get("status_message") or ""), str(record.get("final_text") or "")
    fields, promise = {}, will_retry(found)
    if status.endswith(promise):
        fields["status_message"] = status[:-len(promise)] + CANCELLED[reason]
    retry_text = worded(retry_recovery(record.get("policy_retry_category") or "", found[1]), record)
    if reason != "uncertain" and text.endswith(retry_text):
        fields["final_text"] = text[:-len(retry_text)] + worded(POLICY_RECOVERY, record)
    if not fields:
        return record
    # policy_retry_unpromised: a stop the policy guard finalized keeps its words otherwise (core/statuses.py).
    return update_job_record_if(state_dir, record["key"], lambda r: (r.get("status_message"), r.get("final_text")) == (
        record.get("status_message"), record.get("final_text")), policy_retry_unpromised=reason, **fields) or record


def stopped(record):
    """Whether ``record`` is a policy stop: cancelled, or failed only because its stop's notice was not sent
    (TurnDelivery: execution_status stays cancelled)."""
    return bool(record.get("policy_stop")) and (record.get("status") == "cancelled" or (
        record.get("status") == "failed" and record.get("execution_status") == "cancelled"))


def guard_done(record, now=None):
    """Whether the policy guard, when it stopped ``record``'s task (its claim set ``policy_guard_requested``), has
    blocked its session, sent or tried its notice (app/policy_guard.py finish records ``policy_notice_state`` and
    ``policy_notice_at`` last, after ``policy_guard_cleanup``) and repainted its card (``policy_card_updated``;
    none comes after a cleanup it could not confirm), or CARD_WAIT passed since that notice: the retry's card,
    which says where the task went on, comes after them. The guard repaints no card a retry has replaced."""
    if not record.get("policy_guard_requested"):
        return True
    if not record.get("policy_notice_state"):
        return False
    return (bool(record.get("policy_card_updated")) or record.get("policy_guard_cleanup") == "unconfirmed"
            or (time.time() if now is None else now) - float(record.get("policy_notice_at") or 0) >= CARD_WAIT)


def show(app, ctx, record, text):
    """Say on the stopped task's card what became of its retry (its status line); best effort."""
    try:
        if not app.cards.set_record_activity(ctx, record, text):
            app.runtime.log(f"policy retry card update for job #{record.get('job_id')} not confirmed")
    except Exception as exc:  # noqa: BLE001 - the record already says it
        app.runtime.log(f"policy retry card update failed: {type(exc).__name__}")


def repaint_body(app, ctx, record):
    """The stopped task's card without the retry ``record``'s text no longer promises (``unpromise``): the recovery
    card the policy guard drew, redrawn from the record, or the task card's result, as its delivery painted it
    (``policy_retry_card``, app/worker.py TurnDelivery) with the promise taken out; best effort, through Cards."""
    found = planned(record) or ("", "")
    promise = worded(retry_recovery(record.get("policy_retry_category") or "", found[1]), record)
    try:
        if record.get("policy_guard_finalized"):
            target = context_for_record(app.harness.context, record, record.get("reply_to_message_id") or "")
            ok = app.controls.repaint_recovery_card(target, record)
        elif promise in str(record.get("policy_retry_card") or ""):
            body = record["policy_retry_card"].replace(promise, worded(POLICY_RECOVERY, record), 1)
            ok = app.cards.update_record_elements(ctx, record, [answer_element(BODY_ELEMENT_ID, body)])
        else:
            return
        if not ok:
            app.runtime.log(f"policy retry card body for job #{record.get('job_id')} not confirmed")
    except Exception as exc:  # noqa: BLE001 - the record already says it
        app.runtime.log(f"policy retry card body update failed: {type(exc).__name__}")


def settle(app, ctx, record, reason):
    """``record``'s retry did not run (``reason``, CANCELLED): the record and its card say so, the card's body and
    status line both (an uncertain one may have been queued: its body stays)."""
    current = unpromise(ctx.state_dir, record, reason)
    if app is None:
        return
    if reason != "uncertain" and current.get("policy_retry_unpromised"):
        repaint_body(app, ctx, current)
    show(app, ctx, current, CANCELLED[reason])


def cancel(app, ctx, record, reason):
    current = advance(ctx.state_dir, record["key"], "pending", "cancelled", policy_retry_note=reason)
    if current is not None and app is not None:
        app.runtime.log(f"policy retry of job #{record.get('job_id')} cancelled: {reason}")
    if current is not None:
        settle(app, ctx, current, reason)


def tick(gateway, ctx):
    """Settle every scheduled retry: resolve one a stopped Bridge left mid-submit, cancel one whose topic moved on,
    queue one whose stopped task has ended. Runs every few seconds in the Bridge's outbox loop."""
    retries = select_job_records(ctx.state_dir, lambda r: r.get("policy_retry_state") in ("submitting", "pending"),
                                 log=gateway.runtime.log)
    if not retries:
        return
    keys = [r["key"] for r in retries if r.get("policy_retry_state") == "submitting"]
    topics = [(r.get("topic_id"), r.get("chat_id")) for r in retries if r.get("policy_retry_state") == "pending"]
    records = select_job_records(ctx.state_dir, lambda r: any(r.get("retry_of") == key for key in keys) or any(
        r.get("topic_id") == topic and r.get("chat_id") == chat for topic, chat in topics), log=gateway.runtime.log)
    for record in retries:
        if record.get("policy_retry_state") == "submitting":
            queued = bool(record.get("retried_by")) or any(r.get("retry_of") == record["key"] for r in records)
            current = advance(ctx.state_dir, record["key"], "submitting", "queued" if queued else "uncertain")
            if current is not None and not queued:  # As fire's exception: the card stops saying one comes.
                settle(gateway.apps.get(record.get("backend")), ctx, current, "uncertain")
            continue
        if (gateway.runtime.jobs.settling(record.get("message_id")) or record.get("status") in ("running", "queued")
                or not guard_done(record)):
            continue  # Its task runner, or the policy guard, has not finished the stop yet.
        owner = gateway.apps.get(record.get("backend"))
        if record.get("policy_guard_cleanup") == "unconfirmed":
            # The task runner saw its own tree end (at_stop), the policy guard's stop of it did not confirm it.
            cancel(owner, ctx, record, "cleanup")
            continue
        if owner is None or not stopped(record):
            cancel(owner, ctx, record, "unavailable")
            continue
        reason, target = auto_retry.checked_target(gateway, owner, record, records)
        if reason:
            cancel(owner, ctx, record, reason)
        else:
            fire(owner, ctx, target, record)


def fire(app, ctx, target, record):
    """Claim ``record``'s retry, then queue it on its older model; never sent twice."""
    key, (model, name) = record["key"], planned(record)
    if advance(ctx.state_dir, key, "pending", "submitting") is None:
        return
    fallback = {"of": key, "category": record.get("policy_retry_category") or "", "model": model, "name": name}
    try:
        with topic_lock(target):  # As 检查并继续: one retry at a time in the topic.
            if app.runtime.jobs.topic_jobs(target.topic_id, record["chat_id"]):
                current = advance(ctx.state_dir, key, "submitting", "cancelled", policy_retry_note="new_message")
                if current is not None:
                    settle(app, ctx, current, "new_message")
                return
            app.controls.requeue(target, record["chat_id"], f"policy-retry-{record['job_id']}", record,
                                 fallback=fallback)
    except Exception as exc:  # noqa: BLE001 - its card may have gone out: never retried automatically
        app.runtime.log(f"policy retry of job #{record['job_id']} uncertain: {type(exc).__name__}")
        current = advance(ctx.state_dir, key, "submitting", "uncertain", policy_retry_note=type(exc).__name__)
        if current is not None:
            settle(app, ctx, current, "uncertain")
        return
    queued = bool((find_job_record(ctx.state_dir, key) or {}).get("retried_by"))
    current = advance(ctx.state_dir, key, "submitting", "queued" if queued else "cancelled",
                      policy_retry_note="" if queued else "refused")
    app.runtime.log(f"policy retry of job #{record['job_id']} {'queued on ' + model if queued else 'refused'}")
    if not queued and current is not None:
        settle(app, ctx, current, "refused")
