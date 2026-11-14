"""How a finished turn ended, settled before anything is delivered (rule 5's core): ``settle_turn`` checks the
backend's execution facts, authorizes a verified public reply, protects or records the native session and sets the
``TurnResult``'s status and text. app/worker.py runs it and delivers the result (TurnDelivery)."""
from __future__ import annotations

import dataclasses
from dataclasses import replace
from pathlib import Path
from typing import Any

from covey.app.availability import observe
from covey.core.checkpoint import read_checkpoint
from covey.core.harness import JobCancelled
from covey.core.jobs import harness_of
from covey.core.main_thread import traits_of, worded
from covey.core.result import INCOMPLETE_STATUS, STATUS_MESSAGES
from covey.core.saved_results import (authorize_verified_reply, mark_paused_question_delivery,
                                               observe_verified_reply)


@dataclasses.dataclass
class TurnResult:
    """How one turn ended, settled before its result is delivered."""

    status: str = "done"
    status_message: str = "任务完成"
    final_text: str = ""
    allow_resume: bool = False
    session_finalized: bool = False
    verified_delivery: Any = None
    verified_outcome: Any = None
    outcome: Any = None  # The backend's ExecutionOutcome as settled, when it gave one.


def settle_turn(turn, result, ctx, job, *, sessions, session_id, revision, runtime, prefix):
    """Settle how the turn ended from the backend's ``result``: check its execution facts, authorize
    a verified public reply, protect or record the native session, and set ``turn``'s status and
    text. Raises what the job's handlers turn into a failed or stopped turn; ``turn`` keeps
    whether the session was already finalized."""
    reply, is_error, outcome, claimed_roots = _turn_facts(turn, result, ctx, job, session_id=session_id,
                                                          runtime=runtime, prefix=prefix)
    if result.public_reply is not None and outcome is not None:
        outcome, reply = _authorize_public_reply(turn, result, ctx, job, outcome, reply, sessions=sessions,
                                                 session_id=session_id, revision=revision)
    else:
        _finalize_without_public_reply(turn, result, ctx, job, reply, claimed_roots, sessions=sessions,
                                       session_id=session_id, revision=revision)
    _settle_status(turn, result, job, outcome, reply, is_error)


def _turn_facts(turn, result, ctx, job, *, session_id, runtime, prefix):
    """Check the backend's execution facts and whether its native session may be resumed (sets
    ``turn.allow_resume``). Returns (reply, is_error, outcome, claimed root sessions)."""
    reply, new_session_id, is_error = result.text, result.session_id, result.is_error
    job.session_id = new_session_id or session_id
    outcome = result.outcome
    try:
        observe(ctx, harness_of(job, job.result_backend or prefix),
                job.model, status='failed' if is_error else 'responded',
                error=outcome.error_code if outcome else ('root_failed' if is_error else ''))
    except OSError:
        runtime.log('backend availability observation could not be saved')
    if result.public_reply is not None and outcome is None:
        raise ValueError("公开回复缺少独立执行事实")
    # A SIGTERM after the backend finished (``finished_before``) leaves the turn as it ended.
    if job.cancel_event.is_set() and not job.interrupted:
        if outcome is None or result.public_reply is None:
            raise JobCancelled("任务已停止")
        outcome = replace(outcome, status="cancelled", error_code="cancelled", resume_allowed=False)
    turn.allow_resume = (not is_error) if outcome is None else outcome.resume_allowed
    if outcome is not None and turn.allow_resume and (is_error or result.public_reply is None
            or not outcome.reply_fingerprint or outcome.reply_fingerprint != result.public_reply.fingerprint
            or result.public_reply.session_id != new_session_id):
        raise ValueError("原生续接缺少匹配的已验证根回复")
    claimed_roots = set(job.native_session_claims)
    claimed_roots.update(sid for sid in (session_id, new_session_id) if sid)
    if turn.allow_resume and len(claimed_roots) > 1:
        turn.allow_resume = False
        if outcome is not None:
            outcome = replace(outcome, status="failed", error_code="session_guard", resume_allowed=False)
        else:
            is_error = True
            reply = "本轮出现多个未确认的根会话，已禁止续接。"
    return reply, is_error, outcome, claimed_roots


def _authorize_public_reply(turn, result, ctx, job, outcome, reply, *, sessions, session_id, revision):
    """Authorize the backend's public reply and protect, then record, its native session.
    Returns (outcome, reply): the reply is the verified candidate's text once authorized."""
    new_session_id = result.session_id
    candidate = result.public_reply
    if not job.verified_result_key:
        observe_verified_reply(ctx, job, candidate.backend, candidate)
    # Keep the native identity blocked until the exact current candidate
    # has passed the persistent generation and authorization checks.
    protected = sessions.finalize_session_from_worker(ctx, job, session_id, new_session_id, revision, allow_resume=False,
                                                     clear_pointer=not turn.allow_resume)
    if not protected:
        outcome = replace(outcome, protection_confirmed=False, resume_allowed=False)
    authorized = authorize_verified_reply(ctx, job, candidate.backend, candidate, outcome)
    if not authorized:
        sessions.finalize_session_from_worker(ctx, job, session_id, new_session_id, revision, allow_resume=False)
        turn.session_finalized = True
        turn.allow_resume = False
        if outcome.status == "completed":
            raise ValueError("公开回复未满足最终发布条件")
    else:
        if turn.allow_resume:
            protected = sessions.finalize_session_from_worker(ctx, job, session_id, new_session_id, revision, allow_resume=True)
            if not protected:
                observe_verified_reply(ctx, job, candidate.backend, None)
                raise ValueError("原生会话保护未确认")
            sessions.record_session_from_worker(ctx, session_id, new_session_id, revision)
        turn.verified_delivery, turn.verified_outcome = candidate, outcome
        reply = candidate.text
        turn.session_finalized = True
    return outcome, reply


def _finalize_without_public_reply(turn, result, ctx, job, reply, claimed_roots, *, sessions, session_id, revision):
    """Protect or record the native session of a turn without a public reply to authorize; a paused
    native question keeps its session blocked for the answer."""
    new_session_id = result.session_id
    if turn.allow_resume and job.native_question_deferred:
        if len(claimed_roots) != 1 or not sessions.finalize_session_from_worker(ctx, job, session_id, new_session_id, revision, allow_resume=False):
            raise ValueError("原生问答会话保护未确认")
        mark_paused_question_delivery(ctx, job, job.result_backend, new_session_id,
                                      job.last_backend_control, reply)
        turn.allow_resume = False  # Only a confirmed answer may transfer this blocked SID.
    protected = sessions.finalize_session_from_worker(ctx, job, session_id, new_session_id, revision,
                                                     allow_resume=turn.allow_resume)
    if turn.allow_resume and not protected:
        raise ValueError("原生会话保护未确认")
    turn.session_finalized = True
    if turn.allow_resume and protected:
        sessions.record_session_from_worker(ctx, session_id, new_session_id, revision)


def _settle_status(turn, result, job, outcome, reply, is_error):
    """Set ``turn``'s reply, status and final text from how the turn ended."""
    main = traits_of(job)  # The Bridge's own texts below say the main thread's words (worded).
    if outcome is not None and result.public_reply is None:
        reply = worded(outcome.failure_reply(result.failure_detail), main)
    if result.public_reply is not None and turn.verified_delivery is None:
        reply = "本轮未完成，公开回复仅已保全，尚未满足发送条件。原生会话不可续接。"
    turn.outcome = outcome
    if outcome is not None:
        turn.status = outcome.job_status
        turn.status_message = "任务完成" if turn.status == "done" else STATUS_MESSAGES.get(
            outcome.error_code, INCOMPLETE_STATUS)
        if outcome.policy_event is not None:
            turn.status_message = outcome.summary()
        if turn.verified_delivery is None and turn.status == "done":
            turn.status, turn.status_message = "failed", "未取得可交付的完整公开回复"
        turn.final_text = reply
    else:
        if is_error:
            turn.status, turn.status_message = "failed", "后端调用失败"
        turn.final_text = reply if not is_error else f"⚠️ {reply}"
    if turn.status == "done" and (turn.allow_resume or job.paused_question_delivery is not None) and read_checkpoint(Path(job.checkpoint_path)).get("requires_user") is True:
        turn.status, turn.status_message = "waiting_user", worded("等待你在本话题回答", main)
