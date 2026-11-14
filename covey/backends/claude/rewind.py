"""↩️ 回退 on Claude Code: drop the last turn of a session and put back the files it changed.

Claude Code keeps a session's messages as a chain in ``<projects>/<cwd>/<session>.jsonl``, each
entry naming its parent. ``--resume <session> --resume-session-at <entry>`` (print mode) resumes with
the chain up to that entry only; the turn's messages then hang under it, so a later plain resume
follows them and the dropped turn stays in the file unseen (2.1.310, verified 2026-11-03). The entry
kept is the parent of the dropped turn's prompt, whose uuid the runtime chose when it sent it
(``prompt_read``, backends/claude/control.py).

Files: with FILE_CHECKPOINTS_ENV set (``ClaudeBackend.child_env``), a print-mode turn backs up each
file its edit tools change before the first change, per prompt: ``file-history-snapshot`` rows name
the backups (``<config>/file-history/<session>/``), ``file-history-delta`` rows the files first
tracked during the turn, which did not exist before it unless they have a backup. ``--rewind-files
<prompt uuid>`` puts every tracked file back as it was when that prompt came and exits. Files changed
by shell commands are not tracked, and a tracked file changed since by anything else goes back too.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root

FILE_CHECKPOINTS_ENV = "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"
TAIL_BYTES = 1 << 20
REWIND_SECONDS = 120
FILES_SHOWN = 20  # Files rewind_point lists; the count says how many more.


def _tail_rows(path, prompt_uuid):
    """The transcript's rows from the entry ``prompt_uuid`` to the end, parsed; [] when it has no such entry.
    Read from the end in growing pieces: a long session's file is hundreds of megabytes and the latest turn
    is at its end (the rows after the prompt name it too, as their parent or their snapshot's message)."""
    size, span, needle = path.stat().st_size, TAIL_BYTES, prompt_uuid.encode()
    while True:
        start = max(0, size - span)
        with path.open("rb") as stream:
            stream.seek(start)
            lines = stream.read().split(b"\n")
        if start:
            lines = lines[1:]  # Cut mid-line.
        rows, found = [], False
        for line in lines:
            if not found and needle not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            found = found or row.get("uuid") == prompt_uuid
            if found:
                rows.append(row)
        if found or not start:
            return rows
        span *= 4


def _changes(rows, prompt_uuid, backups, workdir):
    """[(path, "restore" | "delete")] the file rewind to ``prompt_uuid`` would make now: each tracked file
    whose backup differs from it, each file first tracked in the turn (no backup) that exists."""
    tracked = {}
    for row in rows:
        if row.get("type") == "file-history-snapshot" and row.get("messageId") == prompt_uuid:
            for name, backup in ((row.get("snapshot") or {}).get("trackedFileBackups") or {}).items():
                tracked.setdefault(name, backup)
        elif row.get("type") == "file-history-delta" and row.get("snapshotMessageId") == prompt_uuid:
            tracked.setdefault(row.get("trackingPath"), row.get("backup") or {})
    changes = []
    for name, backup in tracked.items():
        if not isinstance(name, str) or not name or not isinstance(backup, dict):
            continue
        folder = backup.get("realParentDir")
        path = Path(folder) / Path(name).name if folder else Path(workdir) / name
        stored = backup.get("backupFileName")
        try:
            if not stored:
                if path.exists():
                    changes.append((str(path), "delete"))
            elif not path.exists() or path.read_bytes() != (backups / stored).read_bytes():
                changes.append((str(path), "restore"))
        except OSError:
            changes.append((str(path), "restore"))
    return sorted(changes)


def rewind_point(ctx, session_id, prompt_uuid, env):
    """Where dropping the turn that ``prompt_uuid`` began leaves session ``session_id``: {"keep": the entry
    the next turn resumes at ('' when that prompt began the session), "checkpoint": whether the turn's
    files can be put back, "files": the first FILES_SHOWN changes that would make, "file_count"}.
    ValueError when the session or the prompt is not there."""
    path = ClaudeSessionStore(claude_projects_root(env)).locate(ctx.workdir, session_id)
    if path is None:
        raise ValueError("找不到本话题的 Claude Code 会话记录")
    rows = _tail_rows(path, prompt_uuid)
    prompt = next((row for row in rows if row.get("uuid") == prompt_uuid), None)
    if prompt is None or prompt.get("type") != "user":
        raise ValueError("Claude Code 的会话记录里找不到这一轮的提问")
    backups = claude_projects_root(env).parent / "file-history" / session_id
    checkpoint = any(row.get("type") == "file-history-snapshot" and row.get("messageId") == prompt_uuid for row in rows)
    changes = _changes(rows, prompt_uuid, backups, ctx.workdir) if checkpoint else []
    return {"keep": str(prompt.get("parentUuid") or ""), "checkpoint": checkpoint,
            "files": changes[:FILES_SHOWN], "file_count": len(changes)}


def rewind_files(backend, ctx, session_id, prompt_uuid, env):
    """Put the files back as they were when ``prompt_uuid`` came (``--rewind-files``); its words, or
    ValueError with Claude Code's when it refused."""
    argv = backend.wrap_argv([*ctx.backend_cmd, "-p", "--resume", session_id, "--rewind-files", prompt_uuid], ctx)
    try:
        done = subprocess.run(argv, cwd=str(ctx.workdir), env={**env, FILE_CHECKPOINTS_ENV: "1"},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=REWIND_SECONDS,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Claude Code 没能改回文件（{type(exc).__name__}）") from None
    if done.returncode:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ValueError("Claude Code 没能改回文件：" + (detail[-1][:200] if detail else f"退出码 {done.returncode}"))
    return (done.stdout or "").strip()
