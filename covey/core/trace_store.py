"""Process records on disk: one append-only file per task, outside the job record.

Each line holds only what changed since the line before it: steps (and child
steps) that were added or updated, and summary fields that changed. Replaying
the lines rebuilds the snapshot, so a task that runs for days appends a few
hundred bytes per update instead of rewriting a growing record. The writer
compacts the file to one full line when it holds several times the live data.
Daily maintenance gzips the file of a task finished long ago (``compress_trace``);
``load_trace`` reads either form.

Records written before this file existed embed a ``trace_snapshot``: core/migrate.py writes it to
the task's file once (``save_embedded``), so readers look only at the file.
"""
from __future__ import annotations

import gzip
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import write_private_text
from covey.core.logs import append_private_text
from covey.core.jobs import job_file

COMPACT_RATIO = 4
COMPACT_MIN_BYTES = 256 * 1024

_written: dict[str, dict] = {}
_guard = threading.Lock()


def trace_path(state_dir: Path, key: str) -> Path:
    return Path(state_dir) / "traces" / (job_file(state_dir, key).stem + ".jsonl")


def _flatten(snapshot: dict, prefix: tuple = ()) -> dict:
    flat = {}
    for name, value in snapshot.items():
        if name == "steps":
            for sid, step in (value or {}).items():
                flat[prefix + ("s", sid)] = step
        elif name == "children":
            for cid, child in (value or {}).items():
                flat.update(_flatten(child, prefix + ("c", cid)))
        elif name != "step_order":  # Derived from the order steps first appear.
            flat[prefix + ("f", name)] = value
    return flat


def _unflatten(flat: dict) -> dict:
    root: dict = {}
    for path, value in flat.items():
        node = root
        rest = list(path)
        while rest[0] == "c":
            node = node.setdefault("children", {}).setdefault(rest[1], {})
            rest = rest[2:]
        if rest[0] == "s":
            node.setdefault("steps", {})[rest[1]] = value
        else:
            node[rest[1]] = value

    def finish(node):
        node.setdefault("steps", {})
        node["step_order"] = list(node["steps"])
        for child in node.get("children", {}).values():
            finish(child)
        return node
    return finish(root)


def _encode(flat: dict) -> dict:
    return {json.dumps(path, ensure_ascii=False): json.dumps(value, ensure_ascii=False, sort_keys=True)
            for path, value in flat.items()}


def _gzipped(path: Path) -> Path:
    return path.with_name(path.name + ".gz")


def _replay(path: Path) -> dict:
    flat: dict = {}
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        else:
            lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, EOFError):
        return {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # A torn last line from a crash; earlier lines stay valid.
        if entry.get("full"):
            flat = {}
        for key, value in entry.get("set", []):
            flat[tuple(key)] = value
        for key in entry.get("del", []):
            flat.pop(tuple(key), None)
    return flat


def save_trace(state_dir: Path, key: str, snapshot: dict) -> None:
    """Append what changed since the last save of this task's process record."""
    path = trace_path(state_dir, key)
    with resource_lock("trace-store", str(path)):
        flat = _flatten(snapshot)
        encoded = _encode(flat)
        with _guard:
            previous = _written.get(str(path))
        full = previous is None
        if full and path.exists():
            # Another process (an earlier bridge or task runner) wrote it: continue from the file.
            previous = _encode(_replay(path))
            full = False
        previous = previous or {}
        changed = [[list(item), value] for (item, value), k in zip(flat.items(), encoded)
                   if previous.get(k) != encoded[k]]
        removed = [json.loads(k) for k in previous if k not in encoded]
        if changed or removed or full:
            line = {"v": 1, "set": changed, **({"del": removed} if removed else {}), **({"full": True} if full else {})}
            append_private_text(path, json.dumps(line, ensure_ascii=False) + "\n")
        with _guard:
            _written[str(path)] = encoded
        try:
            size = path.stat().st_size
        except OSError:
            return
        live = sum(len(k) + len(v) for k, v in encoded.items())
        if size > COMPACT_MIN_BYTES and size > COMPACT_RATIO * live:
            write_private_text(path, json.dumps({"v": 1, "full": True,
                "set": [[list(k), v] for k, v in flat.items()]}, ensure_ascii=False) + "\n")


def _saved(path: Path) -> dict:
    """What the task's file holds, plain or gzipped; {} without one."""
    for candidate in (path, _gzipped(path)):
        if candidate.exists():
            flat = _replay(candidate)
            if flat:
                return flat
    return {}


def load_trace(state_dir: Path, record: dict) -> dict | None:
    """The task's process snapshot: its own file (gzipped once the task is long finished)."""
    flat = _saved(trace_path(state_dir, str(record["key"]))) if record.get("key") else {}
    return _unflatten(flat) if flat else None


def has_trace(state_dir: Path, key: str) -> bool:
    """Whether load_trace finds the process snapshot of task ``key`` in its file."""
    return bool(_saved(trace_path(state_dir, key)))


def _in_step_order(snapshot: dict) -> dict:
    """``snapshot`` with its steps (and each child's) in the order ToolTrace.restore takes them: those its
    step_order names, then the rest. The file keeps only the steps' order."""
    steps = snapshot.get("steps") or {}
    ordered = {key: steps[key] for key in snapshot.get("step_order", list(steps)) if key in steps}
    ordered.update({key: value for key, value in steps.items() if key not in ordered})
    return {**snapshot, "steps": ordered,
            "children": {cid: _in_step_order(child) for cid, child in (snapshot.get("children") or {}).items()}}


def save_embedded(state_dir: Path, key: str, snapshot: dict) -> bool:
    """Write the process snapshot an older job record embeds (its ``trace_snapshot``) to task ``key``'s file
    as one full line, when load_trace finds none there (core/migrate.py). Under the writer's lock, replaced
    whole; the record keeps its copy, so an older release reads the same. Whether it wrote the file."""
    path = trace_path(state_dir, key)
    with resource_lock("trace-store", str(path)):
        if _saved(path):
            return False
        flat = _flatten(_in_step_order(snapshot))
        write_private_text(path, json.dumps({"v": 1, "full": True, "set": [[list(k), v] for k, v in flat.items()]},
                                            ensure_ascii=False) + "\n")
        forget(state_dir, key)
        return True


def compress_trace(state_dir: Path, key: str, *, older_than: float, finished) -> int:
    """Replace a finished task's ``traces/<key>.jsonl`` by ``<key>.jsonl.gz``; the bytes saved, 0
    when skipped. Under the writer's lock, and only while the file is unchanged since
    ``older_than`` and ``finished()`` (its task is not active, no task runner of it alive) holds.
    The gzip is synced and renamed into place before the plain file goes, so a reader always
    finds one complete form. Another process appending meanwhile keeps the plain file."""
    path = trace_path(state_dir, key)
    with resource_lock("trace-store", str(path)):
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(before.st_mode) or before.st_mtime >= older_than or not finished():
            return 0
        target = _gzipped(path)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
                    out.write(path.read_bytes())
                raw.flush()
                os.fsync(raw.fileno())
            after = os.lstat(path)
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                return 0
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
        path.unlink()
        forget(state_dir, key)  # A later save here starts a new file with a full line.
        return before.st_size - target.stat().st_size


def forget(state_dir: Path, key: str) -> None:
    """Drop the in-memory view, e.g. when another process takes over writing."""
    with _guard:
        _written.pop(str(trace_path(state_dir, key)), None)

