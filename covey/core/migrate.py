"""One-time migrations of old records, so the code that reads records needs no compat branch for them.

``migrate`` runs at the Bridge's start before any event is handled (app/unified.py) and in maintenance
``--apply`` (core/maintenance.py ``maintain``), so a deployer who skips releases is migrated before any reader
runs. Each step is marked done in <state>/migrations.json once every record it touches is done; from then on
it is one read of that file. Until then it runs again from the top: each file is written whole (an atomic
replace) under the lock its writer holds, and only when it is still missing, so a run cut short resumes where
it stopped. Nobody sees a change: the records keep what older releases read.

- embedded-trace: a job record from before traces/ (ff57531) embeds its process snapshot (``trace_snapshot``;
  ``tool_steps``, ``agents`` and ``child_steps`` were only ever written beside it). It goes to
  traces/<key>.jsonl (trace_store.save_embedded) unless load_trace finds one there; load_trace then reads
  only the file. No release since ff57531 writes ``trace_snapshot`` into a record.
- desk-retire: a front-desk topic (前台话题: one the main thread opened with main-thread.json's desk_topics on,
  11-07, registered by an empty file <state>/main-thread/desk/<topic_id>) is frozen: its topic.json gets
  ``retired: "desk"`` (core/topics.py RETIRED_DESK), every field it had kept, and nothing goes into it from then on
  (core/topics.py retired_topic). The registry stays as it is; no release since 2026-11-09 adds to it.
"""
# compat(ff57531): the one place that reads a job record's embedded process snapshot; retire when every job record
# that embeds one has its traces/ file in every deployment (compat_report: embedded-trace)
from __future__ import annotations

import time
from pathlib import Path

from covey.core.jobs import read_job_record
from covey.core.locks import LOCK, resource_lock
from covey.core.state import read_object, write_json
from covey.core.trace_store import has_trace, save_embedded

MIGRATIONS = "migrations.json"
EMBEDDED_TRACE = "embedded-trace"
DESK_RETIRE = "desk-retire"
DESK_REGISTRY = Path("main-thread") / "desk"  # One empty file per front-desk topic id (releases of 11-07 to 11-09).
RETIRED, RETIRED_DESK = "retired", "desk"  # topic.json's key and value (core/topics.py RETIRED_DESK).
FAILED_KEYS = 20  # Keys named in the summary line per step.


def done(state_dir: Path) -> dict:
    """The steps marked done in ``state_dir`` (name -> what it did)."""
    return {name: value for name, value in read_object(Path(state_dir) / MIGRATIONS).items()
            if name in STEPS and isinstance(value, dict) and value.get("done")}


def _mark(state_dir: Path, name: str, value: dict) -> None:
    path = Path(state_dir) / MIGRATIONS
    with resource_lock("migrations", str(path.absolute())):
        write_json(path, {**read_object(path), name: value}, ensure_ascii=False, indent=2, sort_keys=True)


def _embedded(state_dir: Path):
    """(key, snapshot) of each job record that embeds a process snapshot."""
    folder = Path(state_dir) / "jobs"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        record = read_job_record(path) if not path.name.startswith(".") else None
        snapshot = (record or {}).get("trace_snapshot")
        if snapshot and isinstance(snapshot, dict) and record.get("key"):
            yield str(record["key"]), snapshot


def embedded_traces(state_dir: Path) -> tuple[int, list[str]]:
    """Write each embedded process snapshot to its file: (files written, keys of the records it could not
    write). A record it could not write shows an empty process panel until a later run writes it, so the
    summary line names it; one that fails for any reason (a full disk, a snapshot of an odd shape) does not
    stop the Bridge's start."""
    written, failed = 0, []
    for key, snapshot in _embedded(state_dir):
        try:
            written += save_embedded(Path(state_dir), key, snapshot)
        except Exception:
            failed.append(key)
    return written, failed


def pending(state_dir: Path) -> dict:
    """What the steps not marked done would still do (step -> records), reading only: maintenance --dry-run."""
    state_dir = Path(state_dir)
    finished = done(state_dir)
    return {name: still(state_dir) for name, (_, still) in STEPS.items() if name not in finished}


def _unwritten(state_dir: Path) -> int:
    """The embedded snapshots without their traces/ file yet."""
    return sum(1 for key, _ in _embedded(state_dir) if not _has_file(state_dir, key))


def _has_file(state_dir: Path, key: str) -> bool:
    try:
        return has_trace(state_dir, key)
    except OSError:
        return False


def _desk_topics(state_dir: Path):
    """(topic id, its topic.json path) of each registered front-desk topic whose topic.json is there."""
    folder = Path(state_dir) / DESK_REGISTRY
    for entry in sorted(folder.iterdir()) if folder.is_dir() else ():
        name = entry.name
        if name.startswith(".") or "/" in name or not entry.is_file():
            continue
        path = Path(state_dir) / "topics" / name / "topic.json"
        if path.is_file():
            yield name, path


def retire_desk_topics(state_dir: Path) -> tuple[int, list[str]]:
    """Mark each registered front-desk topic's topic.json retired (``RETIRED``), once: (files written, ids of the
    topics it could not write). One already marked, or whose topic.json is gone or unreadable (no route reaches
    it), is left alone."""
    written, failed = 0, []
    for topic_id, path in _desk_topics(state_dir):
        try:
            with LOCK:  # The lock topic.json's writers hold (core/topics.py bind_topic).
                data = read_object(path)
                if not data or data.get(RETIRED) == RETIRED_DESK:
                    continue
                write_json(path, {**data, RETIRED: RETIRED_DESK}, ensure_ascii=False, indent=2)
            written += 1
        except Exception:
            failed.append(topic_id)
    return written, failed


def _desk_left(state_dir: Path) -> int:
    """The registered front-desk topics not marked retired yet."""
    return sum(1 for _, path in _desk_topics(state_dir)
               if (data := read_object(path)) and data.get(RETIRED) != RETIRED_DESK)


def migrate(state_dir: Path, log=None) -> dict:
    """Run the steps not marked done in ``state_dir``; what each did. ``log`` gets one summary line when one ran,
    naming the first records a step could not write."""
    state_dir = Path(state_dir)
    finished = done(state_dir)
    summary, unwritten = {}, {}
    started = time.monotonic()
    for name, (run, _) in STEPS.items():
        if name in finished:
            continue
        written, failed = run(state_dir)
        summary[name] = {"written": written, "failed": len(failed)}
        if failed:
            unwritten[name] = failed
        else:
            _mark(state_dir, name, {"done": True, "at": int(time.time()), "written": written})
    if summary and log is not None:
        log("record migrations " + " ".join(f"{name}:" + ",".join(f"{key}={value}" for key, value in facts.items())
                                             for name, facts in summary.items())
            + f" elapsed_ms={(time.monotonic() - started) * 1000:.0f}"
            + "".join(f" {name}:failed_keys=" + ",".join(keys[:FAILED_KEYS])
                      + (f",+{len(keys) - FAILED_KEYS}" if len(keys) > FAILED_KEYS else "")
                      for name, keys in unwritten.items()))
    return summary


# The steps in the order they run: name -> (what it does: files written and the keys of the records it could not
# write; what it would still do: records, reading only). A new step is one more row.
STEPS = {EMBEDDED_TRACE: (embedded_traces, _unwritten), DESK_RETIRE: (retire_desk_topics, _desk_left)}
