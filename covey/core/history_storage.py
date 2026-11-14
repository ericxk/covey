"""A topic's history: appending a turn's rows (append_history), reading the latest ones and rounds
(recent_history, recent_rounds), dropping and restoring the last round for a rewind, with per-history
isolation, newest-first reads and an exclusive maintenance gate."""
from __future__ import annotations

import datetime as _dt
import functools
import gzip
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from covey.core.locks import resource_lock
from covey.core.logs import append_private_text
from covey.core.state import write_private_text


class MaintenanceGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @contextmanager
    def shared(self):
        with self.condition:
            self.condition.wait_for(lambda: not self.writer and not self.waiting_writers)
            self.readers += 1
        try:
            yield
        finally:
            with self.condition:
                self.readers -= 1
                self.condition.notify_all()

    def __enter__(self):
        with self.condition:
            self.waiting_writers += 1
            self.condition.wait_for(lambda: not self.writer and not self.readers)
            self.waiting_writers -= 1
            self.writer = True
        return self

    def __exit__(self, *args):
        with self.condition:
            self.writer = False
            self.condition.notify_all()


HISTORY_LOCK = MaintenanceGate()


def history_locked(fn):
    @functools.wraps(fn)
    def wrapped(path, *args, **kwargs):
        with HISTORY_LOCK.shared(), resource_lock('history', Path(path).absolute()):
            return fn(path, *args, **kwargs)
    return wrapped


def history_files(path):
    archive = path.parent / (path.name + '.archive')
    files = {}
    if archive.exists():
        for p in archive.iterdir():
            if p.is_file() and not p.is_symlink() and (p.name.endswith('.jsonl') or p.name.endswith('.jsonl.gz')):
                key = p.name.removesuffix('.gz')
                if key not in files or p.suffix == '.gz':
                    files[key] = p
    return [*(files[k] for k in sorted(files)), path]


def reverse_lines(path):
    if path.suffix == '.gz':
        # Only reached when more rows are needed than newer segments contain.
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            yield from reversed(f.readlines())
        return
    with path.open('rb') as f:
        offset = f.seek(0, 2)
        tail = b''
        while offset:
            length = min(offset, 8192)
            offset -= length
            f.seek(offset)
            chunks = (f.read(length) + tail).split(b'\n')
            tail = chunks[0]
            for chunk in reversed(chunks[1:]):
                if chunk:
                    yield chunk.decode('utf-8', errors='replace')
        if tail:
            yield tail.decode('utf-8', errors='replace')


def history_lines(path: Path, newest_first=False):
    with HISTORY_LOCK.shared(), resource_lock('history', path.absolute()):
        files = history_files(path)
        for p in reversed(files) if newest_first else files:
            if not p.exists():
                continue
            if newest_first:
                yield from reverse_lines(p)
            else:
                opener = gzip.open if p.suffix == '.gz' else open
                with opener(p, 'rt', encoding='utf-8', errors='replace') as handle:
                    yield from handle


@history_locked
def append_history(path: Path, role: str, content: str, event: dict[str, Any] | None = None) -> None:
    record: dict[str, Any] = {
        "time": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "role": role,
        "content": content,
    }
    if event:
        for key in (
            "display_content",
            "chat_id",
            "chat_type",
            "message_id",
            "event_id",
            "sender_id",
            "sender_type",
            "message_type",
            "root_id",
            "parent_id",
            "thread_id",
            "create_time",
            "update_time",
            "timestamp",
            "type",
            "mentions",
            "sender",
            "user_agent",
        ):
            value = event.get(key)
            if value not in (None, "", [], {}):
                record[key] = value
    append_private_text(path, json.dumps(record, ensure_ascii=False) + "\n")


def recent_history(path: Path, limit: int = 12) -> list[dict[str, Any]]:
    rows = []
    try:
        # Explicit close releases the per-history lock even on an early break.
        from contextlib import closing
        with closing(history_lines(path, newest_first=True)) as lines:
            for line in lines:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"session_reset", "session_resume"}:
                    break
                rows.append(item)
                if limit > 0 and len(rows) >= limit:
                    break
    except OSError:
        return []
    return list(reversed(rows))


def recent_rounds(path: Path, rounds: int) -> list[dict[str, Any]]:
    """The last `rounds` rounds (each ends with an assistant reply), plus any newer user rows."""
    rows, replies = [], 0
    try:
        from contextlib import closing
        with closing(history_lines(path, newest_first=True)) as lines:
            for line in lines:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("role") == "assistant":
                    if replies == rounds:
                        break
                    replies += 1
                rows.append(item)
    except OSError:
        return []
    return list(reversed(rows))


@history_locked
def drop_last_round(path: Path) -> list[str]:
    """Take the last round (its user rows and the replies after them) out of the topic history, for ↩️ 回退;
    the lines taken, to put back with restore_rounds. Only the current file: a round is never split by a rotation
    that happened while it ran, and the older rounds stay where they are."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return []

    def role(line):
        try:
            item = json.loads(line)
        except ValueError:
            return None
        if not isinstance(item, dict) or item.get("type") in {"session_reset", "session_resume"}:
            return None
        return item.get("role")
    cut = len(lines)
    while cut and role(lines[cut - 1]) == "assistant":
        cut -= 1
    while cut and role(lines[cut - 1]) == "user":
        cut -= 1
    dropped = lines[cut:]
    if dropped:
        write_private_text(path, "".join(lines[:cut]))
    return dropped


@history_locked
def restore_rounds(path: Path, lines: list[str]) -> None:
    """Put back what drop_last_round took (撤销回退)."""
    if lines:
        append_private_text(path, "".join(lines))
