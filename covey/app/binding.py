"""A topic's binding (Harness, model, budget, work mode, place and 敏感词脱敏) and what may change it.

switch_topic rebinds a topic under topic_lock, then LOCK: 🔁 换配置 (app/harness_switch.py) and 代答's
model setting (app/proxy.py) call it. busy_reason says why a topic cannot change now (also for ↩️ 回退),
retire_open_questions voids the questions and waits of the old binding, and stale_job_card and
current_binding_card tell a card of an earlier binding, which may be viewed but never start a job.
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.app.launch import launch_in_progress
from covey.cards.switch_form import UNCHANGED
from covey.cards.topic_actions import SWITCH_OPEN
from covey.core.checkpoint import release_user_wait
from covey.core.harness import backend_capabilities
from covey.core.jobs import upsert_job_record
from covey.core.statuses import RUNNING
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.core.state import read_json, write_json
from covey.core.session_state import capture_session_state, compare_and_set_session
from covey.core.store import directory_store, transition_question
from covey.core.locks import LOCK, topic_lock
from covey.core.topics import read_topic
from covey.core.workspaces import touch_workspace, worktree_root

BUSY = frozenset(RUNNING | {"waiting_workspace"})
# Card actions that start a job; refused on cards from before the latest switch.
JOB_STARTING = frozenset({"bridge_job_retry", "bridge_steer_requeue", SWITCH_OPEN})
REDACTION_ONLY = "敏感词脱敏开着时只能换到能脱敏的模型（Claude）；要换别的 Harness，先在卡上把脱敏关掉"
STALE_CARD = "话题已换过配置，这张卡属于更换前的任务；请在最新的任务卡上操作，或直接在话题里发消息。"


def latest_switch_revision(topic):
    switches = topic.get("switches") or []
    return int(switches[-1].get("revision") or 0) if switches else 0


def stale_job_card(topic, record):
    """A job from before the latest switch may be viewed, never continued or forked."""
    latest = latest_switch_revision(topic)
    return bool(latest) and int(record.get("binding_revision") or 0) < latest


def current_binding_card(state_dir, topic, raw, chat):
    """Whether a 换配置 button belongs to the topic's current binding.

    Finished task cards drawn before the job context carried its revision say revision 1;
    the job record still names the binding the task ran under."""
    binding = (topic.get("harness_id"), topic.get("binding_revision", 1))
    if (raw.get("harness_id"), raw.get("binding_revision")) == binding:
        return True
    try:
        job_id = int(raw.get("job_id"))
    except (TypeError, ValueError):
        return False
    record = next((r for _, r in directory_store(state_dir / "jobs").query(
        topic_id=topic.get("topic_id"), chat_id=chat, job_id=job_id)), None)
    return record is not None and (record.get("harness_id"), record.get("binding_revision")) == binding


def native_transcript(app, workdir, session_id):
    """Where the Harness keeps one native session, or None; best effort, never raises."""
    locate = getattr(app.store, "transcript", None)
    if not session_id or not callable(locate):
        return None
    try:
        return locate(workdir, session_id)
    except (OSError, ValueError):
        return None


def active_task(topics, state_dir, topic_id):
    """A task of the topic is queued, running, stopping or waiting for its worktree."""
    if any(record.get("status") in BUSY for _, record in directory_store(state_dir / "jobs").query(topic_id=topic_id)):
        return True
    return topics.runtime.jobs.in_topic(topic_id)


def busy_reason(topics, ctx, chat, action="确认更换"):
    """Why this topic cannot change its binding (or ``action`` it) now, or ''."""
    if launch_in_progress(ctx):
        return "本话题正在创建独立 worktree，完成后再" + action
    if active_task(topics, ctx.state_dir, ctx.topic_id):
        return "本话题还有任务在排队或执行，请先在任务卡上停止，停止完成后再" + action
    for _, record in directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id):
        if record.get("status") in {"submitting", "uncertain"}:
            return "本话题有回答正在提交或送达未确认，先在问题卡上查看提交状态"
    app = topics.apps.get(read_topic(ctx).get("harness_id"))
    if app is not None and app.questions.undelivered_checkpoint_jobs(ctx, chat):
        return "本话题的结果或问题还没送达，先在任务卡上重新投递"
    return ""


def _clear_pointer(app, path):
    with app.runtime.session_lock:
        revision, sid = capture_session_state(path, app.spec.session_pattern)
        if sid:
            compare_and_set_session(path, app.spec.session_pattern, revision, sid, None)


REWIND = "rewind-next.json"  # In the topic's folder: the entry its next turn resumes at (↩️ 回退, app/rewind.py).


def marker_path(ctx):
    return ctx.history_path.parent / REWIND


def read_marker(ctx):
    """The topic's pending ↩️ 回退 (app/rewind.py), or None; here because a switch reads it too."""
    try:
        value = read_json(marker_path(ctx))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("token") else None


def run_mode_of(workdir):
    """"worktree" for a topic in a Bridge worktree (core/workspaces.py worktree_root), else "direct"."""
    try:
        Path(workdir).resolve().relative_to(worktree_root().resolve())
        return "worktree"
    except (ValueError, OSError):
        return "direct"


def switch_topic(topics, ctx, chat, form, name, model, effort, work_mode="", *, place=None, redaction=None):
    """Rebind the topic to `name`/`model`/`effort` in `work_mode` ("" the default) and, with ``place``
    ({workspace, workdir, run_mode, branch}), in another directory; returns (outgoing, incoming) bindings.
    A new work mode or place alone is a switch too: like any, it starts a fresh session. ``redaction``
    (True/False, None to keep it) alone only sets the topic's 敏感词脱敏, the session going on: returns None."""
    topic = read_topic(ctx)
    old_app, new_app = topics.apps[topic["harness_id"]], topics.apps[name]
    topic_dir = ctx.history_path.parent
    old_sid = old_app.sessions.read_session_id(topic_dir / old_app.spec.session_filename) or ""
    # A glob on the NAS: outside LOCK. A device's transcript is on the device: none here.
    transcript = None if getattr(ctx, "device", "") else native_transcript(old_app, ctx.workdir, old_sid)
    with topic_lock(ctx), LOCK:  # The topic lock waits out a submission or retry still sending its card.
        topic = read_topic(ctx)
        if (topic.get("harness_id") != form["harness_id"]
                or topic.get("binding_revision", 1) != form["binding_revision"]):
            raise ValueError("话题配置已变化，请在最新的任务卡上重新打开换配置")
        reason = busy_reason(topics, ctx, chat)
        if reason:
            raise ValueError(reason)
        redacting = topic.get(REDACTION_KEY) is True
        redaction = redacting if redaction is None else redaction
        rebind = (place is not None or (name, model, effort, work_mode) != (
            topic["harness_id"], topic.get("model", ""), topic.get("effort", ""), topic.get("work_mode", "")))
        if not rebind and redaction == redacting:
            raise ValueError(UNCHANGED)
        # A Harness that cannot redact would send the terms out.
        if redaction and not backend_capabilities(new_app.backend, new_app.harness.context).redaction:
            raise ValueError(REDACTION_ONLY)
        if not rebind:
            # Only 敏感词脱敏: each request of a turn goes through the relay whole, the earlier rounds too, so
            # the session goes on; turned on, it covers the turns from here (the earlier ones went out as they were).
            set_redaction(topic, redaction)
            write_json(topic_dir / "topic.json", topic, ensure_ascii=False, indent=2)
            topics.runtime.log(f"redaction {'on' if redaction else 'off'} topic={ctx.topic_id[:12]} by a switch")
            return None
        if (old_app.sessions.read_session_id(topic_dir / old_app.spec.session_filename) or "") != old_sid:
            raise ValueError("会话状态正在变化，请稍后重试")
        here = {"workspace": topic.get("workspace_name", ""), "workdir": str(topic.get("workdir") or ""),
                "run_mode": run_mode_of(topic.get("workdir") or "")}
        outgoing = {"harness_id": topic["harness_id"], "title": old_app.spec.title, "model": topic.get("model", ""),
                    "effort": topic.get("effort", ""), "work_mode": topic.get("work_mode", ""), "session_id": old_sid,
                    "transcript": str(transcript) if transcript else "",
                    # Whether its turns went through the term relay: so does the handoff summary (app/switch_handoff.py).
                    "redaction": redacting, **here}
        marker = read_marker(ctx)
        if old_sid and marker is not None and marker.get("session_id") == old_sid:
            # A ↩️ 回退 the next turn was to take: the session's tip still has the dropped round, so no summary
            # is made from it (app/switch_handoff.py); the entry kept is noted.
            outgoing["rewind_keep"] = str(marker.get("keep") or "")
        incoming = {"harness_id": name, "title": new_app.spec.title, "model": model, "effort": effort,
                    "work_mode": work_mode, **(place or here)}
        # A pointer the new Harness kept from an earlier binding (or this one) would
        # resume a session that missed the turns in between: it always starts fresh.
        _clear_pointer(new_app, topic_dir / new_app.spec.session_filename)
        revision = int(topic.get("binding_revision", 1)) + 1
        topic.update(harness_id=name, model=model, effort=effort, model_settings_snapshot=True,
                     binding_revision=revision, harness_locked=True, workspace_locked=True)
        if place:
            topic.update(workspace_name=place["workspace"], workdir=place["workdir"])
        if work_mode:
            topic["work_mode"] = work_mode
        else:
            topic.pop("work_mode", None)
        set_redaction(topic, redaction)
        topic.setdefault("switches", []).append({"at": time.time(), "revision": revision, "job_id": form.get("job_id"),
                                                 "from": outgoing, "to": incoming})
        write_json(topic_dir / "topic.json", topic, ensure_ascii=False, indent=2)
    if place:
        touch_workspace(ctx.state_dir, ctx.default_workdir, place["workspace"])
    topics.runtime.log(f"switched topic={ctx.topic_id[:12]} {outgoing['harness_id']} -> {name} revision={revision}"
                       + (f" run_mode={place['run_mode']}" if place else ""))
    retire_open_questions(topics, old_app, ctx, chat)
    return outgoing, incoming


def set_redaction(topic, on):
    if on:
        topic[REDACTION_KEY] = True
    else:
        topic.pop(REDACTION_KEY, None)


def retire_open_questions(topics, old_app, ctx, chat, *, reason="switched", message="话题已更换配置，由新模型接着处理",
                          job_status="continued"):
    """Questions of the old binding (or of a turn ↩️ 回退 dropped, ``reason`` "rewound") are void: cancel them
    and end the jobs waiting on them (``job_status``: "continued", the turn goes on elsewhere; "cancelled" when it is stopped,
    as a project's lead does for the owner)."""
    for path, record in directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id, chat_id=chat):
        if record.get("status") != "pending":
            continue
        updated = transition_question(path, {"pending"}, status="cancelled", cancel_reason=reason)
        if updated:
            try:
                old_app.questions.paint_question_state(ctx, updated)
            except Exception as exc:  # noqa: BLE001 - the switch already happened
                topics.runtime.log(f"switch question repaint failed: {type(exc).__name__}")
    for _, record in directory_store(ctx.state_dir / "jobs").query(topic_id=ctx.topic_id, chat_id=chat):
        if record.get("status") != "waiting_user" or not record.get("key"):
            continue
        upsert_job_record(ctx.state_dir, record["key"], status=job_status, status_message=message)
        if record.get("checkpoint_path"):
            try:
                release_user_wait(Path(record["checkpoint_path"]))
            except (OSError, ValueError):
                pass
