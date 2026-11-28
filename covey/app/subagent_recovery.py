"""What a failed or interrupted Claude turn's subagents delivered, for the next turn.

A turn that failed (rate limits included) seals its native session, so the next one, whether 检查并继续, an automatic retry
or the owner's next message, starts a new session and would not know what the subagents of the dead turn had handed in. The
backend reads their transcripts (backends/claude/subagent_report.py, Claude only, not on a device); this module cuts each
last text to ``TEXT_CHARS``, keeps ``INLINE`` of them for the prompt (the unfinished and failed first) and writes the
rest, whole, to ``subagents.md`` in the dead job's run directory. The result is ``recovery_context["subagents"]``
(``{"items", "more", "files"}``; core/checkpoint.py ``subagents_note`` words it) and is carried on when the next turn dies
too. Every failure here is no block, never a refused retry.
"""
from __future__ import annotations

from pathlib import Path

from covey.core.harness import backend_capabilities
from covey.core.run_dir import run_dir

INLINE = 8
TEXT_CHARS = 1500
KEPT = 40  # Entries carried from turn to turn.
FILE = "subagents.md"
FILE_CHARS = 20000  # Per entry in the file.
ENDED = ("failed", "interrupted")
ORDER = {"unfinished": 0, "failed": 1, "finished": 2}


def _cut(text):
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS].rstrip() + "…"


def turn_start(record) -> float:
    """When ``record``'s turn began, in epoch seconds: its backend request (or the record's creation); ValueError when
    it says none."""
    timings = record.get("timings") if isinstance(record.get("timings"), dict) else {}
    start = timings.get("backend_requested") or timings.get("backend_started") or record.get("created_at")
    if not isinstance(start, (int, float)):
        raise ValueError("这一轮没有开始执行的时间")
    return float(start)


def collect(harness, ctx, record):
    """``{"items", "more", "files"}`` of ``record``'s turn (and the dead turns before it that were carried on), or None."""
    try:
        return _collect(harness, ctx, record)
    except Exception:  # noqa: BLE001 - no block, never a refused retry
        return None


def _collect(harness, ctx, record):
    if record.get("status") not in ENDED or not record.get("session_id"):
        return None
    backend = harness.backend
    read = getattr(backend, "subagent_report", None)
    if read is None or not backend_capabilities(backend, ctx).session_files:
        return None
    since = turn_start(record)
    fresh = read(ctx, record["session_id"], since)
    older = (record.get("recovery_context") or {}).get("subagents")
    carried = list(older.get("items") or []) if isinstance(older, dict) else []
    files = [f for f in (older.get("files") or []) if isinstance(f, str)] if isinstance(older, dict) else []
    seen = {item.get("id") for item in fresh if isinstance(item, dict)}
    entries = [item for item in carried if isinstance(item, dict) and item.get("id") not in seen] + list(fresh)
    entries = entries[-KEPT:]
    if not entries:
        return None
    ranked = sorted(range(len(entries)), key=lambda i: (ORDER.get(entries[i].get("end"), 0), -i))
    shown = sorted(ranked[:INLINE])
    rest = [entries[i] for i in sorted(ranked[INLINE:])]
    path = _write(ctx, record, rest) if rest else None
    if path:
        files = [*files[-2:], path]
    items = [{"id": entries[i].get("id", ""), "description": entries[i].get("description", ""),
              "end": entries[i].get("end", "unfinished"), "text": _cut(str(entries[i].get("text") or ""))} for i in shown]
    return {"items": items, "more": len(rest), "files": files}


def _write(ctx, record, rest):
    """The path of the file holding ``rest``, or '' when it could not be written."""
    try:
        directory = run_dir(ctx.state_dir, record["key"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / FILE
        words = {"finished": "做完", "failed": "出错", "unfinished": "没做完"}
        path.write_text("\n\n".join(f"## {e.get('description', '')}（{words.get(e.get('end'), '没做完')}）\n\n"
                                    f"{str(e.get('text') or '（没有文字）')[:FILE_CHARS]}" for e in rest) + "\n",
                        encoding="utf-8")
        return str(Path(path))
    except OSError:
        return ""
