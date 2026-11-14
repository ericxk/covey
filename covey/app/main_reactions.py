"""Reactions on the owner's message for a main-thread turn of the 话题区 layout (core/placement.py Row.quiet, D1
2026-11-08: 提问不开串; docs/main-thread.md 提问不开串). The turn opens no thread of its own, so the message says how it
stands: OnIt once it is queued (app/submission.py, in the Bridge), then, when the turn ends (app/worker.py, in its
task runner), DONE for an answer, Get for work handed on to topics, CrossMark for a failure or an official refusal;
nothing more for a stop or a question (the question card at the top level asks).

The 2026-11-08 probe (misc-tasks PROBE-reaction.md): on the web a reaction makes no unread mark, moves no chat up
and adds no message, only an icon before the chat list's preview. Adding a reaction the bot has already is the same
one again (its reaction_id too), removing one removed already succeeds: the end adds OnIt again to learn its id, then
removes it. A failure is a log line, never the answer's; no record lock is held (rules 1 and 2)."""
from __future__ import annotations

from covey.core import placement

ON_IT, DONE, GOT, FAILED = "OnIt", "DONE", "Get", "CrossMark"


def ending(status, routed, refused=False) -> str:
    """The reaction a turn ending in ``status`` leaves: DONE (Get when it ``routed`` work to topics), CrossMark for a
    failure or an official refusal (``refused``: its status is cancelled, as a stop's), '' for any other end (a stop,
    a question)."""
    if status == "done":
        return GOT if routed else DONE
    return FAILED if status == "failed" or refused else ""


def start(messenger, state_dir, profile, job, log, again=False) -> bool:
    """OnIt on the owner's message of the queued ``job``, when it is a quiet one (core/placement.py ``marked``).
    ``again``: a retry or resume of an earlier turn (app/task_controls.py requeue), whose CrossMark comes off first."""
    if not placement.marked(state_dir, job):
        return False
    message = placement.owner_message(job)
    try:
        if again:
            mark = messenger.react(profile, message, FAILED)  # Its id, as ``finish`` learns OnIt's.
            if mark and not messenger.unreact(profile, message, mark):
                log(f"main thread reaction not removed job=#{job.job_id} emoji={FAILED}")
        if messenger.react(profile, message, ON_IT):
            return True
    except Exception as exc:  # noqa: BLE001 - the turn goes on without it
        log(f"main thread reaction failed job=#{job.job_id}: {type(exc).__name__}")
        return False
    log(f"main thread reaction not added job=#{job.job_id} emoji={ON_IT}")
    return False


def finish(messenger, profile, job, emoji, log) -> bool:
    """OnIt taken off the owner's message of ``job`` and ``emoji`` ('' none) put on; whether ``emoji`` is there now
    (True for '' once OnIt went)."""
    message = placement.owner_message(job)
    try:
        mark = messenger.react(profile, message, ON_IT)  # Its id: the one there already, or a new one.
        if mark and not messenger.unreact(profile, message, mark):
            log(f"main thread reaction not removed job=#{job.job_id} emoji={ON_IT}")
        done = bool(messenger.react(profile, message, emoji)) if emoji else bool(mark)
    except Exception as exc:  # noqa: BLE001 - the answer is the turn's, not its reaction
        log(f"main thread reaction failed job=#{job.job_id}: {type(exc).__name__}")
        return False
    log(f"main thread reaction job=#{job.job_id} emoji={emoji or '-'} ok={done}")
    return done
