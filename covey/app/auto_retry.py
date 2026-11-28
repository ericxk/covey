"""Automatic 检查并继续 after a turn that ended without a reply as one of KINDS: a model rate limit
(on Harnesses that declare ``rate_limit_retry``), an incomplete end (``incomplete_retry``), or a restart
that interrupted it (any Harness: RESTARTED, plan_restart).

A rate-limited turn is retried up to six times (60 s to an hour apart, about 2-3 hours in all), one a passing
server fault (SERVER_FAULT: 529, 5xx, a dropped connection) three times (30 s to 5 minutes), an incomplete or
interrupted one once, ``delays`` seconds after each failure plus 0-30 s of jitter (so topics stopped together do not
retry together). When Claude Code said when a limit resets (core/limit_reset.py, within MAX_RESET_WAIT) the retry is
at the reset plus a minute instead. A spent quota (QUOTA: a gateway's accounts all spent, every agy account of the
model's group spent) is retried only at its known reset, within MAX_RESET_WAIT, a minute after it (T831); one further
off, or with none known, is not retried: its card says when it is back (a known one is told of when it comes,
app/quota_return.py). Each kind counts its own retries along a chain (the job's ``auto_retry``,
``auto_fault``, ``auto_continue`` and ``auto_resume``; a retry's job carries them on), so they add up and never
multiply. A retry turn that made progress (``made_progress``) starts the chain's counts again, so a long round is not
cut off by an old count; MAX_IDLE retries in a row at a reset with no progress stop the chain (the failure card
stays). COVEY_AUTO_RETRY_LONG=0 gives back the old ladder (two tries, no fault kind, no reset, no jitter). The worker that delivered the failure, or the Bridge that marked the task interrupted (a
restart found it unfinished, or its task runner got a SIGTERM), writes the schedule into that job's
record (``auto_retry_state`` "pending", ``auto_retry_at``, ``auto_retry_attempt``, ``auto_retry_kind``;
none means rate-limited; ``auto_retry_reset_at``, ``auto_retry_progress``, ``auto_retry_idle``);
the Bridge's outbox loop runs ``tick``, which fires it through TaskControls.retry_topic, the
path of the button, so a restart keeps it. The state then moves on, under the record lock, to
"submitting" and "queued", or to "cancelled" (``auto_retry_note`` says why), or to "uncertain"
when a submit raised or a Bridge stopped during it: an uncertain one never runs again (AUTO_RETRY_TRANSITIONS).
Clicking 检查并继续 or 停止自动重试, a newer job in the topic, or a model switch cancels it.
Older Bridges and task runners ignore these keys; one of 74023f9 runs an incomplete one as its first
rate-limit retry, and one that does not know ``server_fault`` shows such a failure as an unfinished one.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from pathlib import Path
import random
import time
from typing import NamedTuple

from covey.app.outbox import REDELIVER_LABEL, REDELIVER_NOTE, RETRY_NOTE, retry_label
from covey.cards.recovery import (FOLLOW_UP_NOTES, FOLLOW_UPS, OTHER_HARNESS, REPORT_NOTE, SWITCH_MODEL,
                                  failure_details, interrupted_summary, result_actions, result_note, retry_status, switch_route)
from covey.core import env as cenv
from covey.core import failure, turn_source
from covey.core.harness import backend_capabilities
from covey.core.jobs import find_job_record, job_key, select_job_records, update_job_record_if
from covey.core.metrics import run_seconds
from covey.core.result import INCOMPLETE_STATUS, STATUS_MESSAGES
from covey.core.saved_results import has_saved_reply
from covey.core.topics import context_for_topic, read_topic, topic_label

CANCEL_KIND = "bridge_job_retry_cancel"
RATE_LIMITED, INCOMPLETE, RESTARTED, SERVER_FAULT = "rate_limited", "incomplete", "restarted", "server_fault"
QUOTA = "quota_exhausted"
SWITCH = "COVEY_AUTO_RETRY_LONG"
JITTER = 30  # Seconds at most added to every scheduled retry.
RESET_AFTER = 60  # A retry at a limit's reset goes this long after it (and the jitter).
MAX_RESET_WAIT = 6 * 3600  # A reset further off than this is not waited for on the clock: the ladder is.
MAX_IDLE = 3  # Retries at a reset in a row that made no progress, after which the chain stops.
PROGRESS_STEPS, PROGRESS_SECONDS = 3, 60  # What a retry turn did to have made progress: tool steps, and that it ran.
LONG_KINDS = (RATE_LIMITED, SERVER_FAULT)  # The kinds a retry that made progress starts over.
NOTICED = (*LONG_KINDS, QUOTA)  # The kinds app/limit_notice.py tells of together.
AT_RESET = (RATE_LIMITED, QUOTA)  # The kinds a known reset times; QUOTA is retried only at one.
IDLE = "auto_idle"  # The job field counting the retries in a row that made no progress.


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
    counted: bool = True  # Whether its status lines say "第 n/N 次" (not for a ladder a retry that made progress starts over).
    legacy: tuple = ()  # Its delays with COVEY_AUTO_RETRY_LONG=0, () for a kind that did not exist then.


KINDS = {
    RATE_LIMITED: Kind(("failed", "rate_limited"), "rate_limit_retry", "auto_retry", (60, 180, 600, 1800, 3600, 3600),
                       "模型服务限流", "自动重试", STATUS_MESSAGES["rate_limited"], "限流后自动重试", False, (60, 180)),
    # A 529, a 5xx or a dropped connection the Harness's own retries gave up on: gone in minutes, if at all.
    SERVER_FAULT: Kind(("failed", "server_fault"), "fault_retry", "auto_fault", (30, 120, 300), "模型服务临时故障",
                       "自动重试", STATUS_MESSAGES["server_fault"], "临时故障后自动重试"),
    # A spent quota with a known reset within MAX_RESET_WAIT: at the reset, at most three times a chain (T831).
    QUOTA: Kind(("failed", "quota_exhausted"), "", "auto_quota", (0, 0, 0), "模型额度用完", "自动接着做",
                STATUS_MESSAGES["quota_exhausted"], "额度恢复后自动接着做", False),
    # Claude's main task ended while subtasks or later work had not: 10 of 31 failures in 9 days (2026-10).
    INCOMPLETE: Kind(("timed_out", "settle_timeout"), "incomplete_retry", "auto_continue", (60,), "本轮未全部完成",
                     "自动检查并继续", INCOMPLETE_STATUS, "未全部完成后自动检查并继续"),
    # A restart (a container restart leaves a turn unfinished) or a SIGTERM to the task
    # runner ended the turn: once, after the Bridge is up again.
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
    """The automatic retry a finished turn gets. ``reset_at``: the limit's reset it waits for (0: none known);
    ``progress``: the turn made progress, so the chain's counts start over; ``idle``: retries in a row at a reset
    that made none, this turn's too."""

    due_at: float
    attempt: int
    kind: str = RATE_LIMITED
    reset_at: float = 0.0
    progress: bool = False
    idle: int = 0


def long_ladder() -> bool:
    """Whether the long ladder, the fault kind, the resets and the jitter are on (COVEY_AUTO_RETRY_LONG, on unless 0)."""
    return str(cenv.get(SWITCH, "1")).strip() != "0"


def delays_of(kind):
    """Seconds before the first, the second, ... retry of ``kind``'s chain."""
    spec = KINDS[kind]
    return spec.delays if long_ladder() or not spec.legacy else spec.legacy


def jitter() -> float:
    return random.uniform(0, JITTER) if long_ladder() else 0.0


def count_words(kind, attempt):
    """"（第 1/3 次）" for a kind that counts, else ""."""
    spec = KINDS[kind]
    return f"（第 {attempt}/{len(delays_of(kind))} 次）" if spec.counted or not long_ladder() else ""


def made_progress(job, now=None) -> bool:
    """Whether the turn of ``job`` did real work before it ended: at least PROGRESS_STEPS tool steps and PROGRESS_SECONDS
    of running. A turn the limit refused at once, or one stuck in a server's own retries, has neither."""
    trace = getattr(job, "tool_trace", None)
    steps = getattr(trace, "total", 0)
    ran = run_seconds(getattr(job, "timings", None), None, now) or 0
    return isinstance(steps, int) and steps >= PROGRESS_STEPS and ran >= PROGRESS_SECONDS


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
    if kind in (SERVER_FAULT, QUOTA) and not long_ladder():
        return False
    flag = KINDS[kind].capability
    return not flag or bool(getattr(backend_capabilities(app.backend, ctx), flag))


def clock(at):
    return _dt.datetime.fromtimestamp(at).astimezone().strftime("%H:%M")


def status_line(due_at, attempt, kind=RATE_LIMITED, reset_at=0.0, *_):
    """The record's status line while a retry is pending; the topic notice says it too. ``reset_at``: the limit's
    reset, when it is known. (More arguments are Planned's other fields.)"""
    spec = KINDS[kind]
    when = f"，预计 {clock(reset_at)} 恢复" if reset_at else ""
    return f"{spec.reason}{when}，将于 {clock(due_at)} {spec.action}{count_words(kind, attempt)}"


def queued_line(kind, attempt):
    """The retry's own task card while it is queued."""
    return f"{KINDS[kind].action}已入队{count_words(kind, attempt)}"


def plan(harness, ctx, job, turn, now=None):
    """The Planned retry this finished turn gets, or None: a turn that ended as one of KINDS with no
    reply, on a Harness that declares the kind, that 检查并继续 would queue again, at most
    len(delays) times of its kind in a chain. A turn under a task timeout is not continued after an
    incomplete end: the timeout may be what ended it."""
    outcome = turn.outcome
    if outcome is not None and getattr(outcome, "policy_event", None) is not None:
        return None  # Even a cleanup/transport failure cannot erase a latched policy stop.
    if not turn_source.behavior(job).own_delivery:
        return None  # A 协作 ask's turn: its collaboration record queues it again (app/collab_flow.py).
    kind = next((name for name, spec in KINDS.items()
                 if outcome is not None and (outcome.status, outcome.error_code) == spec.outcome), None)
    if (kind is None or turn.verified_delivery is not None or not job.topic_id
            or job.cancel_event.is_set() or (kind == INCOMPLETE and (job.timeout or 0) > 0)):
        return None
    now = time.time() if now is None else now
    if not applies(harness, ctx, kind):
        return None
    spec = KINDS[kind]
    # This turn was itself an automatic retry (of any kind): did it get anywhere?
    retried = any(int(getattr(job, s.count, 0) or 0) for s in KINDS.values())
    progress = kind in LONG_KINDS and long_ladder() and retried and made_progress(job, now)
    done = 0 if progress else int(getattr(job, spec.count, 0) or 0)
    idle = 0 if progress or not retried else int(getattr(job, IDLE, 0) or 0) + 1
    ladder = delays_of(kind)
    if done >= len(ladder):
        return None
    record = find_job_record(ctx.state_dir, job_key(job.message_id, job.job_id)) or {}
    if (has_saved_reply(ctx, record) or record.get("result_retry_policy") == "native_answer_once"
            or record.get("paused_question_delivery") is not None):
        return None  # 检查并继续 would redeliver or refuse it, not queue it again.
    reset = limit_reset(turn, now) if kind in AT_RESET and long_ladder() else 0.0
    if kind == QUOTA and not reset:
        return None  # A spent quota with no reset known, or one further off than MAX_RESET_WAIT: its card says when.
    if reset and idle >= MAX_IDLE:
        return None  # MAX_IDLE retries at a reset that went nowhere: the failure card stays.
    due = (max(reset, now) + RESET_AFTER if reset else now + ladder[done]) + jitter()
    return Planned(due, done + 1, kind, reset, progress, idle)


def limit_reset(turn, now) -> float:
    """When the limit that ended ``turn`` resets, for a retry at that time: 0 when it said none, or it is more than
    MAX_RESET_WAIT off (the ladder then)."""
    said = getattr(turn, "limit", None)
    reset = said.get("reset_at") if isinstance(said, dict) else None
    if isinstance(reset, bool) or not isinstance(reset, (int, float)) or not 0 < reset - now <= MAX_RESET_WAIT:
        return 0.0
    return float(reset)  # A reset already past is no time to wait for: the ladder's first rung.


def plan_restart(record, now=None):
    """The Planned retry of ``record``, a chat task a restart left interrupted (``interrupted_by`` "restart" or
    "sigterm", core/jobs.py interrupted_fields), or None: one that 检查并继续 would queue again (not a stop the
    user asked for, an official refusal or a saved reply to deliver), once along a chain."""
    spec = KINDS[RESTARTED]
    if not turn_source.behavior(record).own_delivery:
        return None  # A 协作 ask's turn: its collaboration record queues it again (app/collab_flow.py).
    if (record.get("status") != "interrupted" or record.get("interrupted_by") not in ("restart", "sigterm")
            or record.get("kind") != "chat" or not record.get("topic_id") or record.get("cancel_requested_by")
            or record.get("retried_by") or record.get("auto_retry_state") in ("pending", "submitting")
            or retry_label(record) != "检查并继续"):
        return None
    done = int(record.get(spec.count) or 0)
    if done >= len(spec.delays):
        return None  # Itself the automatic retry after a restart: a second restart leaves it to the user.
    return Planned((time.time() if now is None else now) + spec.delays[done] + jitter(), done + 1, RESTARTED)


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
            "auto_retry_kind": planned.kind, "auto_retry_note": "",
            # Only when set: a record that has none reads as before.
            **({"auto_retry_reset_at": planned.reset_at} if planned.reset_at else {}),
            **({"auto_retry_progress": True} if planned.progress else {}),
            **({"auto_retry_idle": planned.idle} if planned.idle else {})}


COUNTS = tuple(spec.count for spec in KINDS.values()) + (IDLE,)
# The counts a ReplyJob takes as arguments; the others are declared state (core/jobs.py), which an older release's job
# files and runners leave alone.
INIT_COUNTS = ("auto_retry", "auto_continue", "auto_resume")


def chain_counts(record, attempt):
    """The retry counts of the job that retries ``record``: none after a click (``attempt`` 0); after
    the automatic retry, ``attempt`` for its kind, and the other kind's count as ``record``'s job had it."""
    if not attempt:
        return {name: 0 for name in COUNTS}
    # A turn that made progress started the chain over (Planned.progress): the other kinds' counts too.
    counts = {spec.count: 0 if record.get("auto_retry_progress") else int(record.get(spec.count) or 0)
              for spec in KINDS.values()}
    counts[KINDS[kind_of(record)].count] = attempt
    counts[IDLE] = int(record.get("auto_retry_idle") or 0)
    return counts


# A failure another Harness may get past now: a limit of the one the topic is on (#36).
OTHER_CODES = frozenset({"rate_limited", "quota_exhausted", "server_fault"})


def other_harness(state_dir, record, topics) -> bool:
    """Whether ``record``'s failed task card offers 换个 Harness (cards/recovery.py OTHER_HARNESS, #36): it failed on a
    limit (OTHER_CODES), it can be switched (``switch_route``) and another Harness is enabled (``topics.apps``). Which one, and whether it can take a task now,
    is decided on the click (app/harness_switch.py); nothing switches by itself."""
    if not record or failure.failure_code(record) not in OTHER_CODES or not switch_route(record):
        return False
    return any(name != record.get("harness_id") for name in (getattr(topics, "apps", None) or {}))


def next_steps(record, other=False):
    """(the label of the 检查并继续 click on ``record``'s failed task card, its follow-ups): what core/failure.py
    failure_view offers. A rate limit's click reads 现在重试; 看原因 and 拆小再试 start turns of their own. ``other``
    (``other_harness``): 换个 Harness after 换个模型."""
    view = failure.failure_view(record) if record else None
    actions = (view or {}).get("actions", [])
    label = failure.LABELS[failure.RETRY] if failure.RETRY in actions else "检查并继续"
    follow = tuple(name for name in actions if name in FOLLOW_UPS
                   or (name == SWITCH_MODEL and record and switch_route(record)))
    return label, follow + ((OTHER_HARNESS,) if other and SWITCH_MODEL in follow else ())


def actions(topic_id, job_id, can_stop, record=None, other=False):
    """The 检查并继续 row of a failed task card (``next_steps`` of ``record``); a pending automatic retry adds
    停止自动重试."""
    label, follow = next_steps(record, other)
    return result_actions(topic_id, job_id, label, stop_kind=CANCEL_KIND if can_stop else "", follow_ups=follow,
                          route=switch_route(record or {}))


def result_note_text(label, follow, pending_kind=""):
    """What the buttons of a failed or stopped task card do, a line each, in the fold under them (``button_note``): its
    retry ``label`` (检查并继续, 现在重试, 重新投递结果), then ``follow`` (看原因 with 复制诊断 after it, 拆小再试, 换个模型
    …); while a retry of ``pending_kind`` waits, first when it runs and what cancels it."""
    note = (REDELIVER_NOTE if label == REDELIVER_LABEL else RETRY_NOTE if label == "检查并继续"
            else label + "：" + RETRY_NOTE.split("：", 1)[1] if label else "")
    # 复制诊断 comes with 看原因 (cards/recovery.py result_actions): its line after 看原因's.
    lines = [note, *(line for name in follow
                     for line in (FOLLOW_UP_NOTES[name], *((REPORT_NOTE,) if name == "diagnose" else ())))]
    if pending_kind:
        how = "，做法同“检查并继续”" if pending_kind in NOTICED else ""
        lines.insert(0, f"到点自动重试{how}。现在点“{label}”会立即重试；点“停止自动重试”、在本话题发消息或换配置会取消"
                        "这次自动重试。")
    return "\n".join(line for line in lines if line)


def note_gist(label, pending_retry=False):
    """The one line ``button_note`` shows of what the buttons do: the retry's in a few words."""
    if pending_retry:
        return f"怎么继续：到点自动重试，不想等就点“{label}”"
    if label == REDELIVER_LABEL:
        return f"怎么继续：点“{label}”，只发已保存的结果，不重跑"
    return f"怎么继续：点“{label}”接着做，会先核查做过的" if label else "这几个按钮做什么"


def button_note(label, follow=(), pending_kind=""):
    """Under a failed or stopped task card's buttons, what they do: one grey line that opens on a line each
    (cards/recovery.py result_note; the owner, 2026-11-23: those lines of print sat under every such card, and a
    stopped card said 检查并继续's in its body too). ``pending_kind``: the kind of a retry that waits."""
    return result_note(result_note_text(label, follow, pending_kind), note_gist(label, bool(pending_kind)))


def result_elements(topic_id, job_id, record, other=False):
    """What a failed task card shows after its answer when 检查并继续 applies: a pending automatic retry's status (when
    it runs, the rest in the fold), the buttons and what they do (``button_note``)."""
    label, follow = next_steps(record, other)
    kind = kind_of(record) if pending(record) else ""
    elements = [actions(topic_id, job_id, bool(kind), record, other), button_note(label, follow, kind)]
    if kind:
        elements.insert(0, retry_status(
            f"⏳ {status_line(record['auto_retry_at'], record['auto_retry_attempt'], kind, record.get('auto_retry_reset_at') or 0)}"))
    return elements


def show(app, ctx, record, text):
    """Say on the failed task card what became of its retry; only 检查并继续 stays. An interrupted task's card
    (RESTARTED) said in its chat-list preview when it would continue: that goes too."""
    try:
        other = other_harness(ctx.state_dir, record, getattr(app, "topics", None))
        label, follow = next_steps(record, other)  # The fold under the row no longer says when it would run.
        ok = app.cards.update_record_elements(ctx, record, [retry_status(text), actions(record["topic_id"],
                                                                                   record["job_id"], False, record, other),
                                                            button_note(label, follow)])
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
    target = None
    try:
        topic = record_topic(owner, record)
        reason = blocker(gateway, record, records, topic)
        if not reason and due:
            target = retry_context(owner, record, topic)
            if target.topic_id != record["topic_id"]:
                reason = "unavailable"
    except (OSError, ValueError, KeyError, TypeError):
        reason = "unavailable"
    return reason, (None if reason else target)


def record_topic(owner, record) -> dict:
    """The topic.json of ``record``'s topic, read through ``owner``'s context (its history path's)."""
    return read_topic(dataclasses.replace(owner.harness.context, history_path=Path(record["history_path"])))


def retry_context(owner, record, topic):
    """The context ``record``'s task runs again in, as a click on its card builds it: its topic's root message, else (the
    butler's topic has none) the job's own message, which routes to it; also the web page's ``recover`` (app/web_ops.py)."""
    event = {"chat_id": record["chat_id"], "message_id": topic["root_id"] or record["message_id"], "root_id": topic["root_id"]}
    return owner.models.configured_context(context_for_topic(owner.harness.context, event, owner.store.sessions_dir))


def blocker(gateway, record, records, topic):
    """Why ``record``'s pending retry must not run, or ''."""
    from covey.app.binding import stale_job_card

    if record.get("retried_by"):
        return "manual"
    if topic.get("chat_id") != record.get("chat_id") or topic.get("harness_id") != record.get("backend") \
            or stale_job_card(topic, record):
        return "model_switch"
    # A 补充 taken into this task (steering.py, merged_into_job_id) is no new task: such ones once cancelled the
    # continue after a restart.
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
                      status_message=(f"{spec.reason}，已{spec.action}{count_words(kind_of(record), attempt)}"
                                      if queued else spec.failed_status))
    app.runtime.log(f"auto retry of job #{record['job_id']} {'queued' if queued else 'refused'} (attempt {attempt})")
    if not queued and current is not None:
        show(app, ctx, current, CANCELLED["refused"])


def failed_controls(topic_id, job_id, record, other=False) -> list:
    """The elements a failed task card gets after its answer (app/cards.py Cards.finish_result_controls inserts
    them): 技术详情, the words its body said before its one-line reason (app/turn_delivery.py card_text); its retry
    button as ``retry_label`` names it, with a pending automatic retry's status and 停止自动重试 when that is
    检查并继续 (``result_elements``), and the follow-ups core/failure.py offers (看原因, 拆小再试; a policy stop
    gets 拆小再试 alone; ``other``, other_harness: 换个 Harness too); [] when the record offers none."""
    view = failure.failure_view(record) if record else None
    # Only where the body says the reason instead (app/turn_delivery.py card_text, which wrote failure_code).
    details = ([failure_details(str(record["final_text"]))]
               if view and record.get("failure_code") and record.get("execution_status") == "failed"
               and not record.get("policy_stop") and record.get("final_text") else [])
    label = retry_label(record)
    if label == "检查并继续":
        return details + result_elements(topic_id, job_id, record, other)
    follow = next_steps(record, other)[1]
    if not label and not follow:
        return details
    return details + [result_actions(topic_id, job_id, label, follow_ups=follow, route=switch_route(record))]

