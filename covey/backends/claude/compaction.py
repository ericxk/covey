"""Compacting a resumed Claude Code session before a turn: ``/compact`` in a claude process of its own,
then the turn as usual (``backends/claude/runtime.py``).

A turn compacts first when the Bridge asks (``compact_first``: the butler's context passed its threshold) or
when the session's context at the end of its last turn reached ``compact_between_turns`` tokens. Claude
Code's own compaction near the model's window still happens within a turn; this one runs only between
turns, after the previous task ended. It never fails the turn: without a compaction the turn runs on the
session as it was. Claude Code 2.1.310 answers ``/compact`` over stream-json with ``status: compacting``,
a ``compact_boundary`` (``pre_tokens``, ``post_tokens``) and a result with ``local_command: compact``
and no text; the session keeps its ID. ``options["compact_instructions"]`` (the butler's, app/butler.py)
follows the command as Claude Code's ``/compact <instructions>``: what the summary keeps and how long it runs.
``compact_only`` (backends/claude/runtime.py) is the same compaction with no turn after it and nothing on a card: the
butler's while it is idle (app/idle_compaction.py, through the backend's ``compact_session``).
"""

from __future__ import annotations

import time
import uuid

from covey.backends.claude.events import compacted_sizes
from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root
from covey.backends.claude.transcript import json_row
from covey.core.process import device_of
from covey.core.steps import Steps
from covey.core.text import short_count

COMMAND = "/compact"
STEP = "compact-before-turn"
# A 1M-token session's summary is one long model request; past this the turn runs on the session as it is.
COMPACT_SECONDS = 20 * 60
EXIT_SECONDS = 15  # For the CLI to write the session and exit once its input closed, before it is stopped.
TAIL_BYTES = 1 << 20
TAIL_LIMIT = 64 << 20
USAGE_FIELDS = (("inputTokens", "input_tokens"), ("cacheReadInputTokens", "cache_read_input_tokens"),
                ("cacheCreationInputTokens", "cache_creation_input_tokens"), ("outputTokens", "output_tokens"))


def row_context(row):
    """The context a transcript row tells of, or None: a main-session request's input and output, or the
    summary a compaction left (Claude Code's ``postTokens``, without the system prompt and tools)."""
    if row.get("isSidechain"):
        return None
    if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
        meta = row.get("compactMetadata") if isinstance(row.get("compactMetadata"), dict) else {}
        after = meta.get("postTokens")
        return after if type(after) is int and after >= 0 else 0
    message = row.get("message") if row.get("type") == "assistant" else None
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = [usage.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    if not all(type(value) is int and value >= 0 for value in counts):
        return None
    output = usage.get("output_tokens")
    return sum(counts) + (output if type(output) is int and output >= 0 else 0)


def session_context(path):
    """The session's context when its transcript ends: the last row that tells of one, read from the end
    (at most ``TAIL_LIMIT`` bytes); None when none does."""
    size = path.stat().st_size
    window = TAIL_BYTES
    with path.open("rb") as handle:
        while True:
            start = max(0, size - window)
            handle.seek(start)
            lines = handle.read(size - start).split(b"\n")
            if start:
                lines = lines[1:]  # Cut mid-row.
            for line in reversed(lines):
                if b'"assistant"' not in line and b'"compact_boundary"' not in line:
                    continue
                row = json_row(line)
                tokens = row_context(row) if row is not None else None
                if tokens is not None:
                    return tokens
            if not start or window >= TAIL_LIMIT:
                return None
            window *= 4


def reason(ctx, session_id, env):
    """(trigger, context tokens or None) when this turn compacts its session first: ``manual`` when the
    Bridge asked, ``threshold`` when the context reached ``compact_between_turns``; else None."""
    manual, threshold = ctx.options.get("compact_first"), ctx.options.get("compact_between_turns")
    # A turn after ↩️ 回退 resumes at the kept entry in its one process; the next turn compacts.
    if not session_id or not (manual or threshold) or ctx.options.get("resume_at"):
        return None
    try:
        path = ClaudeSessionStore(claude_projects_root(env)).locate(ctx.workdir, session_id)
        tokens = session_context(path) if path else None
    except (OSError, ValueError):
        tokens = None
    if manual:
        return "manual", tokens
    if tokens is not None and tokens >= threshold:
        return "threshold", tokens
    return None


def command(ctx):
    """``/compact``, followed by the instructions the Bridge gave for the summary, if any (on one line)."""
    instructions = ctx.options.get("compact_instructions")
    text = " ".join(instructions.split()) if isinstance(instructions, str) else ""
    return f"{COMMAND} {text}" if text else COMMAND


def lead(trigger, tokens, threshold):
    """Why the turn compacts, before the sizes."""
    if trigger == "manual":
        return "按任务卡上的安排"
    return f"上一轮结束时上下文 {short_count(tokens)} token，超过 {short_count(threshold)}"


def request_usage(row):
    """What the compaction's model request used, from its result row (its usage fields are zero)."""
    models = row.get("model_usage_since_last_result") or row.get("modelUsage")
    totals = {}
    for entry in (models.values() if isinstance(models, dict) else ()):
        for native, field in USAGE_FIELDS:
            value = entry.get(native) if isinstance(entry, dict) else None
            if type(value) is int and value >= 0:
                totals[field] = totals.get(field, 0) + value
    return totals if "output_tokens" in totals else None


def compact_before_turn(request, env, spawn, emit, flush, usage):
    """Compact ``request.session_id`` first when ``reason`` says so; whether it was compacted. ``spawn``
    starts the claude process (``ClaudeProcess`` over ``stream_argv`` for the session). Its step shows on
    the card; its model request counts in ``usage`` (the turn's). A stop or the task's deadline raises
    as in the turn itself."""
    ctx, session_id = request.context, request.session_id
    if device_of(ctx):
        return False  # The session file is on the device; ``reason`` would read this machine's and find none to compact.
    why = reason(ctx, session_id, env)
    if why is None:
        return False
    trigger, tokens = why
    head = lead(trigger, tokens, ctx.options.get("compact_between_turns"))
    steps = Steps(emit)
    steps.put({"public_progress": True, "id": STEP, "name": "context_compaction", "status": "running",
               "detail": head + (f" · 压缩前上下文 {short_count(tokens)} token" if trigger == "manual" and tokens else ""),
               "started_at": time.time()})
    flush()
    meta = outcome = process = None
    try:
        process = spawn()
        process.initialize()
        input_id = str(uuid.uuid4())
        process.send({"type": "user", "uuid": input_id, "session_id": session_id, "parent_tool_use_id": None,
                      "message": {"role": "user", "content": [{"type": "text", "text": command(ctx)}]}})
        deadline = time.monotonic() + COMPACT_SECONDS
        while True:
            if time.monotonic() >= deadline:
                outcome = "超过 20 分钟仍未完成"
                break
            row = process.receive()
            if row is None:
                continue
            if row.get("type") in {"bridge_error", "bridge_eof"}:
                outcome = "Claude Code 进程提前结束"
                break
            if row.get("session_id") not in (None, session_id) or row.get("parent_tool_use_id") is not None:
                continue
            if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                meta = row.get("compact_metadata") if isinstance(row.get("compact_metadata"), dict) else {}
            elif row.get("type") == "result":
                counts = request_usage(row)
                if counts:
                    usage.update("compact:" + input_id, counts, time.time(), context=False)
                if row.get("is_error") or row.get("subtype") != "success":
                    outcome = "Claude Code 报告压缩失败"
                break
        if outcome is None:
            # The CLI ends once its input closes; the session file is complete only then, not at the result.
            process.end_input()
            settle = time.monotonic() + EXIT_SECONDS
            while time.monotonic() < settle:
                row = process.receive()
                if row is not None and row.get("type") in {"bridge_error", "bridge_eof"}:
                    break
    except (OSError, RuntimeError, ValueError):
        outcome = "Claude Code 进程出错"
    finally:
        if process is not None:
            process.close()
        request.observers.process(None)
    done = meta is not None and outcome is None
    steps.finish(STEP, done, time.time(), detail=head + " · " + (
        compacted_sizes(meta) if done else (outcome or "Claude Code 没有压缩") + "，本轮按原上下文继续"))
    flush()
    return done

