"""What each subagent of a Claude turn delivered, from ``<session>/subagents/agent-*.jsonl`` and its ``.meta.json``.

For a turn that ended badly (app/subagent_recovery.py): the next turn starts a new session, so what the dead one's
subagents handed in would be lost. Per subagent: its description, its end (``finished``: Claude Code's meta says so;
``failed``: finished, but its last row is an API error; else ``unfinished``) and its last assistant text, whole.
Claude Code's own format, read as little as possible: a missing directory, a file or row that does not parse gives
fewer subagents or none, never an error.
"""
from __future__ import annotations

import json
from pathlib import Path

from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root
from covey.backends.claude.transcript import json_row
from covey.core.turn_export_rows import MARGIN

FINISHED, FAILED, UNFINISHED = "finished", "failed", "unfinished"
MAX_AGENTS = 60  # Newest files only: a turn with more is not a turn to rebuild from.
MAX_BYTES = 64 * 1024 * 1024  # A transcript larger than this is skipped.


def _meta(path):
    try:
        meta = json.loads(path.with_name(path.name[:-len(".jsonl")] + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _read(path):
    """(last assistant text, whether the last assistant row is an API error) of one transcript."""
    text, error = "", False
    with path.open("rb") as handle:
        for line in handle:
            if b'"assistant"' not in line:
                continue
            row = json_row(line)
            if row is None or row.get("type") != "assistant":
                continue
            error = row.get("isApiErrorMessage") is True
            content = (row.get("message") or {}).get("content") if isinstance(row.get("message"), dict) else None
            parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"] \
                if isinstance(content, list) else []
            joined = "\n".join(p for p in parts if isinstance(p, str) and p.strip()).strip()
            if joined and not error:
                text = joined
    return text, error


def read(directory, since):
    """The subagents of ``directory`` (a session's ``subagents`` folder) whose file changed since ``since``, oldest
    first: ``{"id", "description", "end", "text"}`` each. [] for anything unreadable."""
    try:
        directory = Path(directory)
        paths = [p for p in directory.glob("agent-*.jsonl") if p.stat().st_mtime >= since - MARGIN]
        paths = sorted(paths, key=lambda p: p.stat().st_mtime)[-MAX_AGENTS:]
    except (OSError, TypeError, ValueError):
        return []
    found = []
    for path in paths:
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            meta = _meta(path)
            text, error = _read(path)
        except (OSError, ValueError):
            continue
        end = UNFINISHED if meta.get("finished") is not True else FAILED if error else FINISHED
        ident = meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) and meta["toolUseId"] else path.stem
        description = meta.get("description") if isinstance(meta.get("description"), str) else ""
        found.append({"id": ident, "description": description.strip()[:120] or "子 Agent", "end": end, "text": text})
    return found


def report(ctx, session_id, since, env):
    """The subagents of the Claude session ``session_id`` of this turn, [] when its records are not found."""
    try:
        path = ClaudeSessionStore(claude_projects_root(env)).locate(ctx.workdir, session_id)
        if path is None:
            return []
        return read(path.parent / session_id / "subagents", since)
    except Exception:  # noqa: BLE001 - Claude Code's own format: whatever it is, no block
        return []
