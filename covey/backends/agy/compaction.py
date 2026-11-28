"""Compacting an agy conversation between turns: ``/compact`` in an agy process of its own.

agy runs ``/compact`` itself when its slash commands are on (a turn runs with ``--disable-slash-commands``, so this
process runs without it): one model request of type ``checkpoint`` that summarizes the conversation, shown as a
``step_update`` of step_type ``checkpoint`` with that request's usage, then a ``result`` with an empty response; the
conversation keeps its ID (seen on 1.5.16). The words after ``/compact`` are not instructions for the summary: agy
compacts and then has its model answer them as a message, so the command goes alone and ``compact_instructions`` is
not used. On a conversation too short to compact, agy hands ``/compact`` to its model as a message instead: no
``checkpoint`` step, so no compaction.

The process runs with the turn's own argv and environment (model, budget, home, the butler's tools): its request
then starts with the same prompt as the turn before, which agy's server still has in its prompt cache for a few
minutes after a turn (about 5 on Neon High, well under one on Neon XHigh). The butler's idle compaction
(app/idle_compaction.py, ``compact_session``) waits ``COMPACT_IDLE_SECONDS`` for that reason; ``compact_first`` (a
request the idle compaction did not take in time) runs it before the turn, its step on the turn's card.
"""
from __future__ import annotations

import json
import subprocess
import time

from covey.core.home import external_cwd
from covey.core.process import device_of, stream_subprocess_lines
from covey.core.text import short_count

COMMAND = "/compact"
SLASH_OFF = "--disable-slash-commands"
STEP = "compact-before-turn"
COMPACT_SECONDS = 15 * 60  # A 500K-token conversation's summary is one model request; past this the turn goes on as it is.
# After a butler turn, how long the topic stays idle before its compaction starts (app/idle_compaction.py): the owner's
# quick follow-ups (about a third of them come within 3 minutes) go first, and the compaction still finds the turn's
# prompt in Neon High's cache (a hit at 180 s, a miss at 330 s). Measured on the owner's butler, T612.
COMPACT_IDLE_SECONDS = 180.0


def compact_argv(argv):
    """A turn's argv with agy's slash commands on."""
    return [arg for arg in argv if arg != SLASH_OFF]


class CompactRows:
    """What a ``/compact`` process printed: its conversation and the ``checkpoint`` step's usage."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.sid = None
        self.usage = None  # The checkpoint step's usage once it is DONE ({} when it said none).

    def on_line(self, line):
        try:
            row = json.loads(line)
        except ValueError:
            return
        if not isinstance(row, dict):
            return
        if row.get("event") == "init":
            init = row.get("init") if isinstance(row.get("init"), dict) else {}
            value = row.get("conversation_id") or init.get("conversation_id")
            self.sid = value if isinstance(value, str) else None
            return
        step = row.get("step_update")
        if row.get("event") != "step_update" or not isinstance(step, dict):
            return
        if step.get("conversation_id") not in (None, self.session_id):
            return
        if step.get("step_type") == "checkpoint" and step.get("state") == "DONE":
            usage = step.get("usage")
            self.usage = usage if isinstance(usage, dict) else {}

    @property
    def done(self):
        return self.usage is not None and self.sid == self.session_id


def compact(ctx, argv, env, session_id, *, cancel_event=None, process_observer=None):
    """``/compact`` on ``session_id``: ``(done, usage)``, the usage of its checkpoint request or None. ``argv`` is the
    turn's (``--conversation session_id`` in it). A stop raises JobCancelled; a process past COMPACT_SECONDS is
    stopped and is no compaction."""
    rows = CompactRows(session_id)
    line = json.dumps({"event": "user", "message": {"content": COMMAND}}) + "\n"
    try:
        rc, _ = stream_subprocess_lines(compact_argv(argv), stdin_text=line, timeout=COMPACT_SECONDS,
                                        on_line=rows.on_line, env=env, cwd=external_cwd(ctx), cancel_event=cancel_event,
                                        process_observer=process_observer, ctx=ctx)
    except subprocess.TimeoutExpired:
        return False, None
    return rc == 0 and rows.done, rows.usage


def context_of(usage):
    """The context a checkpoint request read (its input and cache reads), or None."""
    if not isinstance(usage, dict):
        return None
    counts = [usage.get(key) for key in ("input_tokens", "cache_read_tokens")]
    return sum(counts) if all(type(value) is int and value >= 0 for value in counts) else None


def compact_before_turn(ctx, argv, env, session_id, steps, *, add_usage, cancel_event=None, process_observer=None):
    """``compact_first``: the conversation compacted before the turn, its step on the card; whether it was. It never
    fails the turn: without a compaction the turn runs on the conversation as it was."""
    if not ctx.options.get("compact_first") or not session_id or device_of(ctx):
        return False
    head = "上一轮结束时上下文超过管家的压缩阈值"
    steps.start(STEP, "context_compaction", head, time.time(), public_progress=True)
    done, usage = compact(ctx, argv, env, session_id, cancel_event=cancel_event, process_observer=process_observer)
    if usage:
        add_usage(usage)
    before = context_of(usage)
    steps.finish(STEP, done, time.time(), detail=head + " · " + (
        (f"压缩前上下文 {short_count(before)} token" if before else "已压缩") if done
        else "agy 没有压缩，本轮按原上下文继续"))
    return done
