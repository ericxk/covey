"""Reactions on the owner's message for a butler turn when its topics hang under his question (core/placement.py Row.quiet;
docs/butler.md 提问不开串). The turn opens no thread of its own, so the message says how it
stands: OnIt once it is queued (app/submission.py, in the Bridge), then, when the turn ends (app/worker.py, in its
task runner), DONE for an answer, Get for work handed on to topics, CrossMark for a failure or an official refusal;
nothing more for a stop or a question (the question card at the top level asks).

As probed: on the web a reaction makes no unread mark, moves no chat up
and adds no message, only an icon before the chat list's preview. Adding a reaction the bot has already is the same
one again (its reaction_id too), removing one removed already succeeds: the end adds OnIt again to learn its id, then
removes it. A failure is a log line, never the answer's; no record lock is held (rules 1 and 2).

多路并行 (COVEY_BUTLER_TRACKS=1, app/butler_tracks.py ``Tracks.noted``) adds two: QUEUED (OneSecond) on a message that waits
behind a track's tasks, in the Bridge, instead of OnIt; the task process takes it off and puts OnIt on when the turn
starts (``begin``: the ids come from Messenger.reaction_ids, so no state is kept between the two processes), and MERGED
(THUMBSUP) on a message that joined a running task, which stays until the owner quotes it with 单独答 (``split``). Feishu
takes OneSecond and THUMBSUP, not WAIT. With the switch off none
of them is called."""
from __future__ import annotations

import time

from covey.core import butler_tracks, placement
from covey.core.topics import message_topic

ON_IT, DONE, GOT, FAILED = "OnIt", "DONE", "Get", "CrossMark"
QUEUED, MERGED = "OneSecond", "THUMBSUP"
HELD_SECONDS = 600
HELD: dict = {}  # message id -> when QUEUED went on it in this process: ``start`` leaves OnIt for ``begin``.


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
    if not again and HELD.pop(message, None) is not None:
        return True  # 排队中 is on it: OnIt comes when the turn starts (``begin``).
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


# -- 多路并行 ----------------------------------------------------------------------------------------------------------

def owner_quiet(state_dir, chat, message) -> bool:
    """Whether ``message`` is one of the owner's own that a butler turn of the 话题区 layout answers (as
    ``placement.marked`` says of a job), from the topic its alias names. Never raises."""
    try:
        topic = message_topic(state_dir, chat, message)
        return message.startswith("om_") and bool(topic) and placement.plan(
            state_dir, {"topic_id": topic, "chat_id": chat}).row.quiet
    except Exception:  # noqa: BLE001 - no reaction
        return False


def put(messenger, profile, message, emoji, what, log) -> bool:
    """``emoji`` on ``message``; whether it is there. A failure is a log line."""
    try:
        if messenger.react(profile, message, emoji):
            return True
    except Exception as exc:  # noqa: BLE001 - the turn goes on without it
        log(f"main thread reaction failed {what} message={message[-10:]}: {type(exc).__name__}")
        return False
    log(f"main thread reaction not added {what} message={message[-10:]} emoji={emoji}")
    return False


def take_off(messenger, profile, message, emoji, what, log) -> int | None:
    """The bot's ``emoji`` reactions taken off ``message``: how many went, None when they could not be listed (logged)."""
    try:
        ids = messenger.reaction_ids(profile, message, emoji)
        if ids is None:
            log(f"main thread reactions not listed {what} message={message[-10:]} emoji={emoji}")
            return None
        gone = 0
        for reaction in ids:
            if messenger.unreact(profile, message, reaction):
                gone += 1
            else:
                log(f"main thread reaction not removed {what} message={message[-10:]} emoji={emoji}")
        return gone
    except Exception as exc:  # noqa: BLE001 - the turn goes on without it
        log(f"main thread reaction failed {what} message={message[-10:]}: {type(exc).__name__}")
        return None


def queue(messenger, state_dir, profile, chat, message, log) -> bool:
    """QUEUED on the owner's ``message``, which waits behind a track's tasks (Tracks.noted); ``start`` then leaves OnIt
    to ``begin``. False when it is not a message a quiet turn answers or the reaction is not on."""
    if not owner_quiet(state_dir, chat, message):
        return False
    now = time.time()
    for old in [key for key, at in HELD.items() if now - at > HELD_SECONDS]:
        HELD.pop(old, None)  # Never queued after all: nothing came to take it.
    if not put(messenger, profile, message, QUEUED, "queued", log):
        return False
    HELD[message] = now
    return True


def merge(messenger, state_dir, profile, chat, message, log) -> bool:
    """MERGED on the owner's ``message`` that joined a running task (Tracks.noted); it stays."""
    return owner_quiet(state_dir, chat, message) and put(messenger, profile, message, MERGED, "merged", log)


def split(messenger, profile, message, log) -> bool:
    """MERGED off the owner's ``message`` that he quoted to have it answered on its own (Tracks.noted "alone"); False
    when it could not be taken off."""
    return take_off(messenger, profile, message, MERGED, "alone", log) is not None


def begin(messenger, state_dir, profile, job, log) -> bool:
    """In the task process, as the turn of ``job`` starts: QUEUED off its message and OnIt on, when it had QUEUED (or
    when that cannot be told: OnIt again is the same reaction). With the switch off, or a message that never queued, no
    reaction is added (OnIt went on when it was queued). Whether OnIt was put on."""
    if not butler_tracks.enabled() or not placement.marked(state_dir, job):
        return False
    message = placement.owner_message(job)
    gone = take_off(messenger, profile, message, QUEUED, f"start job=#{job.job_id}", log)
    return gone != 0 and put(messenger, profile, message, ON_IT, f"start job=#{job.job_id}", log)  # None: unknown


def unqueue(messenger, state_dir, profile, job, log) -> None:
    """QUEUED off the message of a job cancelled before it started (its ``begin`` never comes)."""
    if butler_tracks.enabled() and placement.marked(state_dir, job):
        take_off(messenger, profile, placement.owner_message(job), QUEUED, f"cancel job=#{job.job_id}", log)
