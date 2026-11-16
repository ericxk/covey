#!/usr/bin/env python3
"""Shared durable job-state helpers for the Feishu bridge scripts."""

from __future__ import annotations

import json
import re
import threading
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from covey.core.state import ensure_private_dir, utc_now_iso, write_private_text
from covey.core.locks import resource_lock
from covey.core.metrics import durations
from covey.core.statuses import NOT_ENDED, RUNNING, merge_job_record
from covey.core.store import directory_store

JOB_STOP_ACTION_KIND = "bridge_job_stop"
# Timing stages (core/metrics.py) marked once a backend was asked to run the job.
RUN_STAGES = ("backend_requested", "backend_started")


@dataclass
class RunState:
    """Ephemeral controls are never serialized into durable job records."""
    backend_control: Any = field(default=None, repr=False)
    trace_lock: Any = field(default_factory=threading.RLock, repr=False)
    answer_finalized: bool = False


EXTRA = {"extra": True}


def _state(default=None, *, factory=None):
    """State a job gains while it is queued, run and shown, declared so no reader needs a default.
    Before it was declared it was set ad hoc, so handoff and queued files carry it in "extras", and
    only once it differs from this default (`app.detached.dump_job`)."""
    if factory is not None:
        return field(default_factory=factory, init=False, repr=False, metadata=EXTRA)
    return field(default=default, init=False, repr=False, metadata=EXTRA)


@dataclass
class BridgeReplyJob:
    runtime: RunState = field(default_factory=RunState, init=False, repr=False)
    job_id: int
    user_text: str
    chat_id: str
    message_id: str
    timeout: int
    model: str = ""
    effort: str = ""
    prompt_text: str | None = None
    kind: str = "chat"
    workspace_name: str | None = None
    workdir: Path | None = None
    history_path: Path | None = None
    session_path: Path | None = None
    sessions_dir: Path | None = None
    session_id: str | None = None
    diff_artifact_path: str = ""
    diff_json_path: str = ""
    recent_questions: list[str] = field(default_factory=list)
    # History as seen at enqueue time. The worker must build prompts from this
    # snapshot, not from a fresh read: with a queue backlog, re-reading would
    # leak later messages into an earlier job's prompt.
    history_snapshot: list[dict[str, Any]] | None = None
    progress: Any | None = None
    stream_replacements: int = 0
    last_card_error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    cancel_requested_at: str = ""
    cancel_requested_by: str = ""
    # How many other tracks of the butler's went on when the stop came (app/task_controls.py): the stopped text says it.
    stop_others: int = _state(0)
    topic_id: str = ""
    reply_to_message_id: str = ""
    checkpoint_path: str = ""
    recovery_context: dict | None = None
    auto_retry: int = 0  # The n-th automatic retry after a rate limit in a chain (app/auto_retry.py); 0 for others.
    auto_continue: int = 0  # The same after an incomplete turn; the chain carries both counts.
    auto_resume: int = 0  # The same after a restart interrupted the turn (auto_retry.RESTARTED).
    backend_process: Any = field(default=None, repr=False)
    execution_lock: Any = field(default_factory=threading.Lock, repr=False)
    execution_started: bool = False
    cancelled_before_start: bool = False
    model_settings_snapshot: bool = False
    harness_id: str = ""
    binding_revision: int = 0
    timings: dict = field(default_factory=dict)
    title: str = ""  # Card title when the request text is not a good one (an answer carrying its questions).
    # Bound when the queued card is created; never retarget an old card to an older task.
    # None is a legacy/unbound job; 0 means there was no preceding task.
    steer_target_job_id: int | None = None
    # The device the job runs on and the directory on it (core/devices.py; BridgeContext has the same two): '' on this
    # machine. Saved, and in a record, only when set, so a record without them reads as before and an old release
    # reading one that has them fails on the stand-in ``workdir`` (which never exists) rather than running here.
    device: str = _state("")
    remote_workdir: str = _state("")
    # Submission and execution.
    harness_capabilities: Any = _state()
    bridge_run_id: str | None = _state()
    detached_run_dir: Any = _state()
    early_runner: Any = _state()  # Started before its handoff (app/detached.py prestart_runner); never saved.
    runner_relaunched: bool = _state(False)
    # Not before this time.time() (core/scheduler.py): a butler turn waiting for the owner's words
    # (app/butler.py). Never saved: a restart starts it.
    start_held_until: float = _state(0.0)
    # The owner's words that joined such a turn's request while it waited (app/steering.py merge_before_start), one
    # per line: its "你问" block shows them, not the forward or link that started it (app/answer_cards.py asked).
    asked_text: str = _state("")
    # A butler turn of the 话题区 layout whose card is held back until it has run butler.CARD_AFTER seconds
    # (core/placement.py Row.quiet; app/cards.py Cards.reveal); False once it has one, or for any other job. Saved: the
    # task runner sends the card.
    card_deferred: bool = _state(False)
    # The owner's message a retry or resume of such a turn reacts on and links in its 你问 block (app/task_controls.py
    # requeue): its own message id is the retry's, not a Feishu one. '' for every other job. Saved: its task runner reacts.
    reaction_message: str = _state("")
    # A retry on an older model after an official safety refusal (app/policy_retry.py): {"of": the refused job's key,
    # "category", "model"}; its subagents run on that model too (context_for_job), and it never retries again. {} for
    # every other job. Saved: its task runner reads it.
    policy_retry: dict = _state(factory=dict)
    interrupted: bool = _state(False)  # Its task runner took a SIGTERM (app/runner.py interrupt), not a stop.
    result_backend: str = _state("")
    last_backend_control: Any = _state()
    backend_retry: dict = _state(factory=dict)
    # What the link to the device says of this turn's process (core/device_link.py details(): state, seconds down, the
    # patience, ...), read by the activity monitor each tick (core/longtask.py) and saved in the record only for a turn on
    # a device (as ``device_state``). {} on this machine.
    device_state: dict = _state(factory=dict)
    native_session_claims: set = _state(factory=set)
    verified_reply: Any = _state()
    verified_result_key: str | None = _state()
    # Questions.
    native_question_active: bool = _state(False)
    native_question_deferred: bool = _state(False)
    paused_question_delivery: Any = _state()
    checkpoint_question_deferred: bool = _state(False)
    checkpoint_question_error: str = _state("")
    checkpoint_question_registration_pending: bool = _state(False)
    # Supplements held until the task can take them.
    held_lock: Any = _state(factory=threading.RLock)
    held_steers: dict = _state(factory=dict)
    # The task card did not go out (app/cards.py create_streaming_reply): its task sends it again while it runs
    # (Cards.resend_missing), ``card_resends`` times so far, not before ``card_resend_after`` (time.monotonic()).
    card_missing: bool = _state(False)
    card_resends: int = _state(0)
    card_resend_after: float = _state(0.0)
    # A stopped or refused butler turn's card listed its supplements (app/butler_reply.py show_dropped).
    supplements_listed: bool = _state(False)
    held_thread: Any = _state()
    steer_forms: list = _state(factory=list)
    # The task card and its process view.
    topic_label: str = _state("")
    model_label: str = _state("")  # The model as the model selects name it (Claude · Sonnet 5.5); "" when not listed.
    card_updates: Any = _state()
    progress_body_cleared: bool = _state(False)
    body_clear_pending: bool = _state(False)  # The first status line write clears the body too (Cards).
    action_controls_removed: bool = _state(False)
    artifact_controls_attached: bool = _state(False)
    topic_controls_attached: bool = _state(False)  # 换配置 and 回退 (app/switch_handoff.py).
    switch_controls_attached: bool = _state(False)
    export_controls_attached: bool = _state(False)
    interactive_card_id: str | None = _state()
    interactive_elements: set = _state(factory=set)
    activity_monitor: Any = _state()
    tool_trace: Any = _state()
    agents: dict = _state(factory=dict)
    trace_card_updates: Any = _state()
    trace_step_count: int | None = _state()
    trace_rendered_card: Any = _state()
    trace_rendered_panel: Any = _state()
    # Who sent the message that started it (the owner or a group member); "" for any other job.
    sender_id: str = ""
    # The work mode it was submitted with (core/defaults.WORK_MODES); "" for the default, as before modes.
    work_mode: str = ""

    @property
    def backend_control(self):
        return self.runtime.backend_control

    @backend_control.setter
    def backend_control(self, value):
        self.runtime.backend_control = value

    @property
    def trace_lock(self):
        return self.runtime.trace_lock

    @trace_lock.setter
    def trace_lock(self, value):
        self.runtime.trace_lock = value

    @property
    def answer_finalized(self):
        return self.runtime.answer_finalized

    @answer_finalized.setter
    def answer_finalized(self, value):
        self.runtime.answer_finalized = value


def carry_device(job, ctx):
    """``job`` runs where ``ctx`` does: its device and the directory on it (nothing for a context on this machine, so a
    job there is built, saved and read as before devices existed). Returns ``job``."""
    if getattr(ctx, "device", ""):
        job.device, job.remote_workdir = ctx.device, ctx.remote_workdir
    return job


def context_for_job(ctx, job):
    """The context ``job`` runs and reports in: its topic, workspace, session files and the model
    settings it was submitted with, over the Harness's base ``ctx``."""
    return dataclasses.replace(
        ctx,
        model=job.model if job.model_settings_snapshot else (job.model or ctx.model),
        effort=job.effort if job.model_settings_snapshot or job.model else ctx.effort,
        work_mode=job.work_mode if job.model_settings_snapshot or job.model else ctx.work_mode,
        topic_id=job.topic_id,
        reply_to_message_id=job.reply_to_message_id,
        workspace_name=job.workspace_name or ctx.workspace_name,
        workdir=job.workdir or ctx.workdir,
        **({"device": getattr(job, "device", ""), "remote_workdir": getattr(job, "remote_workdir", "")}
           if job.workdir else {}),
        history_path=job.history_path or ctx.history_path,
        session_path=job.session_path or ctx.session_path,
        sessions_dir=job.sessions_dir or ctx.sessions_dir,
        options=_job_options(ctx, job),
    )


def _job_options(ctx, job):
    options = ctx.options
    if job.topic_id and job.binding_revision:
        # Controls drawn for this job (换配置, a question's card) name the binding it ran under,
        # not the app's default revision 1, which a switched topic would refuse as stale.
        options = {**options, "binding_revision": job.binding_revision}
    model = (getattr(job, "policy_retry", None) or {}).get("model")
    if model:
        # A retry after a safety refusal: its subagents run on its older model, not CLAUDE_CODE_SUBAGENT_MODEL's.
        options = {**options, "subagent_model": model}
    return options


def saved_job(record, chat_id, topic_id, reply_to_message_id, **fields):
    """The finished job of `record`, rebuilt only to redeliver what it saved; it never runs."""
    job = BridgeReplyJob(job_id=record["job_id"], user_text="", chat_id=chat_id, message_id=record["key"], timeout=0,
                         topic_id=topic_id, reply_to_message_id=reply_to_message_id)
    for name, value in fields.items():
        setattr(job, name, value)
    return job


def safe_slug(value: str, fallback: str = "job") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip(".-")
    return (slug or fallback)[:140]


def _lock_for_path(path: Path) -> threading.RLock:
    return resource_lock("record", path.absolute())


def jobs_dir(state_dir: Path) -> Path:
    path = state_dir / "jobs"
    ensure_private_dir(path)
    return path


def job_key(message_id: str, job_id: int | str) -> str:
    return safe_slug(message_id or str(job_id))


def job_stop_action_value(job: Any) -> dict[str, str]:
    return {
        "kind": JOB_STOP_ACTION_KIND,
        "job_id": str(getattr(job, "job_id", "") or ""),
        "message_id": str(getattr(job, "message_id", "") or ""),
    }


def parse_job_stop_action_value(raw: Any) -> dict[str, str] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict) or raw.get("kind") != JOB_STOP_ACTION_KIND:
        return None
    job_id = str(raw.get("job_id") or "").strip()
    message_id = str(raw.get("message_id") or "").strip()
    if not job_id.isdigit() or int(job_id) <= 0:
        return None
    if len(message_id) > 180:
        return None
    action = {"kind": JOB_STOP_ACTION_KIND, "job_id": job_id, "message_id": message_id}
    others = str(raw.get("others") or "").strip()
    if others.isdigit() and int(others) > 0:
        action["others"] = others  # A stop word of the butler's tracks: how many other tracks are still going.
    return action


def job_file(state_dir: Path, key: str) -> Path:
    """Where ``key``'s record is, computed only; ``job_path`` also creates jobs/ (private) for a writer."""
    return Path(state_dir) / "jobs" / f"{safe_slug(key)}.json"


def job_path(state_dir: Path, key: str) -> Path:
    jobs_dir(state_dir)
    return job_file(state_dir, key)


def read_job_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # Not JSON, or not UTF-8 (UnicodeDecodeError).
        return None
    return payload if isinstance(payload, dict) else None


def upsert_job_record(state_dir: Path, key: str, **fields: Any) -> dict[str, Any]:
    path = job_path(state_dir, key)
    with _lock_for_path(path):
        current = read_job_record(path) or {}
        now = utc_now_iso()
        record = merge_job_record(current, {**fields, "key": key, "updated_at": now})
        if 'timings' in fields:
            record['timings'] = {**fields['timings'], **current.get('timings', {})}
            record['latency_ms'] = durations(record['timings'])
        record.setdefault("created_at", now)
        write_private_text(path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return record


# Fields no code reads back once a job has ended (S41): the history its prompt was built from (40% of
# jobs/ by bytes; read only from a record waiting for a workspace), its recent questions, its recovery audit;
# and fields no release writes or reads any more (repo review R2: 10-28 none of the live releases named them),
# left by the diff documents, the topic-only recovery card, the narrative card's trial and an input-token note.
FINISHED_UNREAD_FIELDS = ("history_snapshot", "recent_questions", "recovery_audit", "diff_doc_error", "diff_doc_id",
                          "diff_doc_url", "diff_status", "topic_only", "card_style", "input_tokens_source")


def slim_finished_job_record(state_dir: Path, key: str) -> bool:
    """Drop FINISHED_UNREAD_FIELDS from the record of a job that has ended, under its lock. Not an
    update: updated_at and every other field stay as they were. Whether the record was rewritten."""
    path = job_path(state_dir, key)
    with _lock_for_path(path):
        current = read_job_record(path)
        if (current is None or not current.get("status") or current["status"] in NOT_ENDED
                or not any(name in current for name in FINISHED_UNREAD_FIELDS)):
            return False
        slim = {name: value for name, value in current.items() if name not in FINISHED_UNREAD_FIELDS}
        write_private_text(path, json.dumps(slim, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return True


def update_job_record_if(state_dir: Path, key: str, check: Any, **fields: Any) -> dict[str, Any] | None:
    """upsert_job_record only when ``check`` accepts the current record, under its lock: the
    updated record, or None when there is no record or ``check`` refused it."""
    path = job_path(state_dir, key)
    with _lock_for_path(path):
        current = read_job_record(path)
        if current is None or not check(current):
            return None
        return upsert_job_record(state_dir, key, **fields)


def transition_job_record(state_dir: Path, key: str, expected, **fields: Any) -> dict[str, Any] | None:
    """``upsert_job_record`` only while the record's status is one of ``expected``; None otherwise."""
    return update_job_record_if(state_dir, key, lambda current: current.get("status") in expected, **fields)


def record_bridge_job_state(
    *,
    state_dir: Path,
    backend: str,
    job: Any,
    status: str,
    status_message: str = "",
    workspace_name: str = "",
    workdir: Path | str | None = None,
    history_path: Path | str | None = None,
    session_path: Path | str | None = None,
    sessions_dir: Path | str | None = None,
    **fields: Any,
) -> None:
    progress = getattr(job, "progress", None)
    job_id = getattr(job, "job_id", "")
    message_id = str(getattr(job, "message_id", "") or "")
    payload: dict[str, Any] = {
        "timings": dict(getattr(job, 'timings', {})),
        "model": getattr(job, "model", ""),
        "effort": getattr(job, "effort", ""),
        "model_settings_snapshot": bool(getattr(job, "model_settings_snapshot", False)),
        "backend": backend,
        "harness_id": getattr(job, "harness_id", "") or backend,
        "binding_revision": getattr(job, "binding_revision", 0),
        "job_id": job_id,
        "message_id": message_id,
        "chat_id": str(getattr(job, "chat_id", "") or ""),
        "topic_id": getattr(job, "topic_id", ""),
        "reply_to_message_id": getattr(job, "reply_to_message_id", ""),
        "user_text": str(getattr(job, "user_text", "") or ""),
        "checkpoint_path": getattr(job, "checkpoint_path", ""),
        "recovery_context": getattr(job, "recovery_context", None),
        "prompt_text": str(getattr(job, "prompt_text", "") or ""),
        "steer_target_job_id": getattr(job, "steer_target_job_id", None),
        "kind": str(getattr(job, "kind", "") or "chat"),
        "workspace_name": str(getattr(job, "workspace_name", "") or workspace_name),
        "workdir": str(getattr(job, "workdir", None) or workdir or ""),
        "status": status,
        "status_message": status_message,
        "card_id": str(getattr(progress, "card_id", "") or "") if progress else "",
        "card_message_id": str(getattr(progress, "message_id", "") or "") if progress else "",
        "last_card_error": str(getattr(job, "last_card_error", "") or ""),
        "session_id": str(getattr(job, "session_id", "") or ""),
        "diff_artifact_path": str(getattr(job, "diff_artifact_path", "") or ""),
        "diff_json_path": str(getattr(job, "diff_json_path", "") or ""),
        "history_path": str(getattr(job, "history_path", None) or history_path or ""),
        "session_path": str(getattr(job, "session_path", None) or session_path or ""),
        "stream_replacements": int(getattr(job, "stream_replacements", 0) or 0),
        "cancel_requested_at": str(getattr(job, "cancel_requested_at", "") or ""),
        "cancel_requested_by": str(getattr(job, "cancel_requested_by", "") or ""),
    }
    if progress:
        # A running total over the job's cards; with no card left, the last saved total stays.
        payload["card_update_failures"] = int(getattr(progress, "failures_total", 0) or 0)
    recent_questions = getattr(job, "recent_questions", None)
    if recent_questions:
        payload["recent_questions"] = list(recent_questions)
    if getattr(job, "sender_id", ""):
        payload["sender_id"] = job.sender_id
    if getattr(job, "work_mode", ""):
        payload["work_mode"] = job.work_mode
    if getattr(job, "device", ""):
        payload["device"], payload["remote_workdir"] = job.device, str(getattr(job, "remote_workdir", "") or "")
    if getattr(job, "policy_retry", None):
        payload["policy_retry"] = dict(job.policy_retry)  # Never retried again (app/policy_retry.py eligible).
    resolved_sessions_dir = getattr(job, "sessions_dir", None) or sessions_dir
    if resolved_sessions_dir:
        payload["sessions_dir"] = str(resolved_sessions_dir)
    if status == "cancelled" and getattr(job, "cancelled_before_start", False):
        payload["never_started"] = True  # Cancelled while queued, or merged into another task.
    payload.update(fields)
    upsert_job_record(state_dir, job_key(message_id, job_id), **payload)


def never_started(record: dict[str, Any]) -> bool:
    """A job no backend ran: a launch card's message (waiting, or closed without 开始任务), or one
    cancelled while queued or merged into another task as a supplement. Its ``backend`` is only the
    route it waited on (a launch card's is the default Harness), so per-backend counts of tasks and
    outcomes leave it out. The Bridge writes ``never_started`` when it closes such a job; an older
    record counts when it has no run stage, execution status or session."""
    if record.get("status") not in {"waiting_workspace", "cancelled"}:
        return False
    if record.get("never_started") is True:
        return True
    # A waiting launch card's record has no never_started yet (the Bridge writes it when it closes one), so it is
    # told by what it lacks, like a cancelled record closed before the field (c9b9e73).
    timings = record.get("timings")
    ran = isinstance(timings, dict) and any(stage in timings for stage in RUN_STAGES)
    return not (ran or record.get("execution_status") or record.get("session_id"))


def list_job_records(state_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    records = select_job_records(state_dir, None)
    return records[:limit] if limit > 0 else records


def select_job_records(state_dir: Path, where, *, log=None, narrow=None, **filters) -> list[dict[str, Any]]:
    """The job records ``where(record)`` accepts (None: all), newest first as list_job_records orders
    them. ``where`` sees the cached record and must not change it; only the accepted ones are copied,
    so a loop that runs every few seconds does not copy all of them (live 774: ~0.3 s, 2026-10-25).
    A record ``where`` raises on is left out; ``log`` hears of it once (JsonDirectoryStore.query).
    ``filters`` narrow by indexed fields first (status=RUNNING): only those records are read. ``narrow`` also, by a
    test of the indexed and carried fields (core/store.py CARRIED: ``updated_at``) that ``where`` implies."""
    records = [record for _, record in directory_store(jobs_dir(state_dir)).query(where, log=log, narrow=narrow,
                                                                                    **filters)]
    records.sort(key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""),
                                  int(item.get("job_id") or 0)), reverse=True)
    return records


def running_job_records(state_dir: Path) -> list[dict[str, Any]]:
    """The jobs in RUNNING, newest first; only those records are read (repo review R4: at start
    and in a deploy's status every record was, 2.6 s for 1185 on NFS)."""
    return select_job_records(state_dir, None, status=frozenset(RUNNING))


def max_job_id(state_dir: Path) -> int:
    """The largest job number of any record, 0 without one; from the index, no record is read."""
    return directory_store(jobs_dir(state_dir)).max_field("job_id")


def topic_job_records(state_dir: Path, topic_id: str, **filters) -> list[dict[str, Any]]:
    """One topic's job records, narrowed by indexed fields (chat_id, job_id, status). Copies only
    those: a full list copies every record, about 0.3 s for the live 774 (2026-10-25)."""
    return [record for _, record in directory_store(jobs_dir(state_dir)).query(topic_id=topic_id, **filters)]


def find_job_record(state_dir: Path, ref: str) -> dict[str, Any] | None:
    ref = ref.strip()
    if not ref:
        return None
    normalized = ref[1:] if ref.startswith("#") else ref
    direct = read_job_record(job_path(state_dir, normalized))
    if direct and normalized in {str(direct.get("key", "")), str(direct.get("message_id", "")), str(direct.get("job_id", ""))}:
        return direct
    if normalized.isdigit():
        records = directory_store(jobs_dir(state_dir)).query(job_id=int(normalized))
        if not records:
            records = directory_store(jobs_dir(state_dir)).query(job_id=normalized)
        return records[0][1] if records else None

    return None


# The status line a task runner leaves on a turn a SIGTERM ended (app/worker.py leave_interrupted).
SIGTERM_STATUS = "任务进程收到 SIGTERM，后端已停止，等待标为中断"
# The same for a turn whose device stayed away past the patience (app/worker.py leave_interrupted): ``interrupted_by`` becomes
# "device_lost", which app/auto_retry.py does not continue by itself (the device may be gone for good).
DEVICE_LOST_STATUS = "设备失联超过耐心值，后端已放弃，等待标为中断"


def interrupted_fields(record: dict[str, Any], status_message: str, cause: str = "") -> dict[str, Any]:
    """The fields that mark ``record``, a job whose runner ended before it finished, interrupted. A runner
    may have ended while it sent "任务 #N 已停止": until 2026-11-02 it read a SIGTERM (a container's
    shutdown sends every process one) as the stop button. That text is the Bridge's, not a result: it is
    dropped, so the recovery card offers 检查并继续 instead of 重新投递结果, which resent it and left the task
    stopped for good (T331 #2407). Kept when the user did stop the task, or for an official refusal.
    ``cause``: "restart" (found unfinished when a Bridge started) or "sigterm" (its runner got one), the
    interruptions app/auto_retry.py continues once by itself; "" for a runner that ended otherwise."""
    fields = {"status": "interrupted", "interrupted_at": utc_now_iso(), "status_message": status_message,
              **({"interrupted_by": cause} if cause else {})}
    if (record.get("execution_status") == "cancelled" and record.get("delivery_ok") is False
            and record.get("verified_reply_authorized") is not True and not record.get("cancel_requested_by")
            and not record.get("policy_stop") and not record.get("policy_guard_requested")):
        fields.update(final_text=None, delivery_ok=None)
    return fields


def recover_stale_jobs(state_dir: Path, backend: str, keep=frozenset()) -> list[dict[str, Any]]:
    """Mark jobs a previous process left unfinished as interrupted; `keep` names jobs still running elsewhere."""
    recovered: list[dict[str, Any]] = []
    for record in running_job_records(state_dir):
        if record.get("backend") != backend or record.get("key") in keep:
            continue
        if str(record.get("status") or "") not in RUNNING:
            continue
        key = str(record.get("key") or "")
        if not key:
            continue
        updated = upsert_job_record(state_dir, key, **interrupted_fields(
            record, "bridge restarted before this job reached a terminal state", "restart"))
        recovered.append(updated)
    return recovered


def context_for_record(base: Any, record: dict[str, Any], reply_to: str | None = None) -> Any:
    """``base`` (a Harness's context) for the topic of the job record ``record``: its topic, working directory,
    history and session paths. ``reply_to``: the message replies go under, else the record's
    ``reply_to_message_id`` (a KeyError when it has none). Raises KeyError for a record without those fields."""
    return dataclasses.replace(
        base, topic_id=record["topic_id"],
        reply_to_message_id=record["reply_to_message_id"] if reply_to is None else reply_to,
        workdir=Path(record["workdir"]), history_path=Path(record["history_path"]),
        session_path=Path(record["session_path"]),
        device=str(record.get("device") or ""), remote_workdir=str(record.get("remote_workdir") or ""))


def harness_of(job_or_record: Any, fallback: str = "") -> str:
    """The Harness a job (``harness_id``) or a job record (``backend``) ran on; ``fallback`` when it names none.
    The fields keep their names (rule 10)."""
    if isinstance(job_or_record, dict):
        return str(job_or_record.get("backend") or "") or fallback
    return getattr(job_or_record, "harness_id", "") or fallback


def job_field(job, name):
    """``name`` of ``job``, a job or its record (a mapping): '' for None or a missing one."""
    from collections.abc import Mapping

    value = job.get(name) if isinstance(job, Mapping) else getattr(job, name, None)
    return "" if value is None else value
