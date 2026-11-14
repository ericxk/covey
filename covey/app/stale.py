"""Close the records daily maintenance found waiting too long (core.stale.stale_records).

A launch card nobody started, a question nobody answered and a task that waited for an answer
are closed the way a click would close them, with STALE_NOTE on the original card, once: the
record moves first, under its lock and only from the waiting state it was found in; the card is
changed after that, outside any record lock. A job registered with this Bridge or with a live
task runner is never closed.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from covey.app.form_cards import LAUNCH_FORM_LOCK, replace_form_card
from covey.app.launch import close_launch_form
from covey.app.questions import question_card_lock
from covey.app.topics import topic_target
from covey.cards.elements import notice_card
from covey.core.jobs import job_path, read_job_record, transition_job_record
from covey.core.locks import resource_lock, topic_lock
from covey.core.stale import STALE_QUESTION_STATES, STALE_NOTE, UNDELIVERED_LAUNCH_NOTE
from covey.core.statuses import OPEN_FORMS, WORKTREE_STATUS
from covey.core.run_dir import run_dir, runner_alive
from covey.core.state import read_json, utc_now_iso
from covey.core.store import directory_store, transition_question
from covey.core.topics import load_topic


def close_stale_records(gateway, ctx, plan):
    """Close the plan's stale questions, then its stale jobs; what was closed, one line each."""
    closed = []

    def isolated(what, close, *args):
        try:
            closed.extend(close(gateway, ctx, *args))
        except Exception as exc:  # noqa: BLE001 - one record never stops the others
            gateway.runtime.log(f"stale record not closed {what}: {type(exc).__name__}")

    for token in plan["stale_questions"]:
        isolated("question", _close_question, token)
    launches = {}
    for key in plan["stale_jobs"]:
        record = read_job_record(job_path(ctx.state_dir, key)) or {}
        if record.get("status") == "waiting_workspace":
            launches.setdefault((record.get("topic_id"), record.get("chat_id")), []).append(key)
        elif record.get("status") == "waiting_user":
            isolated("job", _close_waiting_user, key)
    undelivered = set(plan.get("stale_undelivered", ()))
    for (topic_id, chat_id), keys in launches.items():
        isolated("launch", _close_launch, topic_id, chat_id, keys, undelivered)
    return closed


def _running(gateway, ctx, key):
    """Registered with this Bridge (queued, running, being handed over) or alive in a task runner."""
    return gateway.runtime.jobs.has_key(key) or runner_alive(run_dir(ctx.state_dir, key))


def _topic_context(app, ctx, topic_id, chat_id):
    return topic_target(app, chat_id, load_topic(ctx.state_dir, topic_id))


def _close_question(gateway, ctx, token):
    path = ctx.state_dir / "questions" / (token + ".json")
    record = read_json(path)
    if record.get("job_key") and _running(gateway, ctx, record["job_key"]):
        return []
    job = read_job_record(job_path(ctx.state_dir, record.get("job_key") or "")) or {}
    app = gateway.apps.get(record.get("harness_id") or job.get("backend") or "")
    # As an answer is submitted: the form's lock, then the record's for the change itself.
    with question_card_lock(path):
        updated = transition_question(path, STALE_QUESTION_STATES, status="cancelled", cancel_reason="stale",
                                      cancelled_at=time.time())
        if updated is None:
            return []
        painted = bool(app and updated.get("card_id"))
        if painted:
            app.questions.paint_question_state(_topic_context(app, ctx, updated["topic_id"], updated["chat_id"]), updated)
    gateway.runtime.log(f"stale question closed job=#{updated.get('job_id')} was={record.get('status')} "
                        f"card={'updated' if painted else 'none'}")
    return [f"question of job #{updated.get('job_id')}"]


def _close_waiting_user(gateway, ctx, key):
    """Its open questions were closed above, each with the note on its card. A job that asked
    in its reply alone gets the note in its task card's status line."""
    if _running(gateway, ctx, key):
        return []
    updated = transition_job_record(ctx.state_dir, key, {"waiting_user"}, status="cancelled",
                                    status_message=STALE_NOTE, stale_closed_at=utc_now_iso())
    if updated is None:
        return []
    noted = directory_store(ctx.state_dir / "questions").query(
        where=lambda q: q.get("job_key") == key and q.get("cancel_reason") == "stale")
    card = "question" if noted else "none"
    app = gateway.apps.get(updated.get("harness_id") or updated.get("backend") or "")
    if not noted and app is not None and updated.get("card_id"):
        try:
            target = _topic_context(app, ctx, updated["topic_id"], updated["chat_id"])
            card = "updated" if app.cards.set_record_activity(target, updated, STALE_NOTE) else "unconfirmed"
        except Exception as exc:  # noqa: BLE001 - the record already says it; the card is best effort
            card = type(exc).__name__
    gateway.runtime.log(f"stale job closed job=#{updated.get('job_id')} was=waiting_user card={card}")
    return [f"job #{updated.get('job_id')} waiting_user"]


def _close_launch(gateway, ctx, topic_id, chat_id, keys, undelivered=frozenset()):
    """A topic whose messages all waited a week for 开始任务: close its launch form, as the
    workspace manager's 结束 does, and replace the card with the note."""
    def waiting():
        return {r["key"] for _, r in directory_store(ctx.state_dir / "jobs").query(topic_id=topic_id, chat_id=chat_id)
                if r.get("status") == "waiting_workspace"}

    if not waiting() <= set(keys):
        return []  # A newer message waits on the same card: the topic is in use.
    forms = []
    for path in sorted((ctx.state_dir / "launch-forms").glob("*.json")):
        try:
            form = read_json(path)
        except (OSError, ValueError):
            continue
        if form.get("topic_id") == topic_id and form.get("chat_id") == chat_id:
            if form.get("status") == WORKTREE_STATUS:
                return []  # 开始任务 was clicked and its worktree is being created.
            if form.get("status") in OPEN_FORMS:
                forms.append((path, form))
    closed = []
    for path, form in forms:
        app = gateway.apps.get(form.get("harness_id") or "")
        if app is None:
            continue
        target = _topic_context(app, ctx, topic_id, chat_id)
        # The order a 开始任务 click takes, so the two never interleave.
        with resource_lock(LAUNCH_FORM_LOCK, path), topic_lock(target):
            record = read_json(path)
            jobs = waiting()
            if record.get("status") not in OPEN_FORMS or not jobs <= set(keys):
                continue  # Started meanwhile, or a newer message waits on this card.
            close_launch_form(target, path, record, STALE_NOTE, STALE_NOTE)
            updated = bool(record.get("card_id")) and replace_form_card(
                target, path, record, record, notice_card(STALE_NOTE, "已自动关闭"), gateway.topics.messenger, "stale-launch")
        gateway.runtime.log(f"stale launch closed jobs={len(jobs)} card={'updated' if updated else 'unchanged'}")
        closed.append(f"launch card of {len(jobs)} waiting job(s)")
    if forms:
        return closed
    # No open form (never delivered, or 开始任务 marked it done and is submitting these messages):
    # the records alone, under the topic's lock as that click submits them, so none it queues closes.
    history = ctx.state_dir / "topics" / str(topic_id or "") / "history.jsonl"
    with topic_lock(SimpleNamespace(history_path=history)):
        if not waiting() <= set(keys):
            return closed  # A newer message waits meanwhile.
        for key in keys:
            # Picked by the 1-day rule (stale_records), or marked by publish_launch_cards.
            note = UNDELIVERED_LAUNCH_NOTE if key in undelivered or (read_job_record(
                job_path(ctx.state_dir, key)) or {}).get("launch_delivery_error") else STALE_NOTE
            record = None if _running(gateway, ctx, key) else transition_job_record(
                ctx.state_dir, key, {"waiting_workspace"}, status="cancelled",
                status_message=note, stale_closed_at=utc_now_iso(), never_started=True)
            if record is not None:
                gateway.runtime.log(f"stale job closed job=#{record.get('job_id')} was=waiting_workspace card=none")
                closed.append(f"job #{record.get('job_id')} waiting_workspace")
    return closed
