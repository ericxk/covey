"""Logs of a state directory: appending private lines, rotating a live log by size (rotate_live, keep_log) and
a bounded line log (line_log)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from covey.core.file_lock import file_lock
from covey.core.state import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE, ensure_private_dir


# bridge.log and the Manager's logs go by size alone, never at a start (repo review E3: rotated at each of
# about 14 starts a day, 4 copies held 10 hours). bridge.log grew about 2 MB a day in 2026-10: 10 files of
# 10 MiB hold weeks of it, and still 3 days at 30 MB a day.
LOG_BYTES = 10 * 1024 * 1024


LOG_KEEP = 9


def keep_log(path: Path, stop: threading.Event, every=60):
    """Rotate ``path`` by size (``rotate_live``) now and every ``every`` seconds until ``stop`` is set."""
    while True:
        try:
            rotate_live(path, LOG_BYTES, LOG_KEEP)
        except OSError:
            pass  # The next check tries again; a log never stops its writer.
        if stop.wait(every):
            return


def rotate_live(path: Path, max_bytes=10 * 1024 * 1024, keep=4):
    """Bound redirected logs without replacing the inode held by adopted children.

    Like logrotate copytruncate, a small concurrent-write window can lose log
    lines. Durable task/results records never use this best-effort log path.
    """
    if not path.is_file() or path.stat().st_size < max_bytes:
        return False
    with path.open('r+b') as source, file_lock(path, source=source):
        if os.fstat(source.fileno()).st_size < max_bytes:
            return False
        for number in range(keep, 1, -1):
            previous = Path(str(path) + f'.{number - 1}')
            if previous.exists():
                previous.replace(Path(str(path) + f'.{number}'))
        destination = Path(str(path) + '.1')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # Snapshot a bounded prefix; an active writer cannot keep the copy alive.
        remaining = os.fstat(source.fileno()).st_size
        with os.fdopen(fd, 'wb') as target:
            while remaining:
                block = source.read(min(remaining, 1024 * 1024))
                if not block:
                    break
                target.write(block)
                remaining -= len(block)
        source.truncate(0)
    return True


def append_private_text(path: Path, content: str) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(content)


def line_log(path: Path, *, max_bytes, keep=1, live=False):
    """A ``log(line)`` that appends ``line`` to ``path`` (0600) after the local time; an OSError is dropped, as a
    log never stops its writer. Past ``max_bytes`` the file goes to ``path.1`` (``keep`` older files): renamed
    once it is larger, or with ``live``, for a file another process may hold open, copied and truncated once it
    is as large (``rotate_live``)."""
    path = Path(path)

    def log(line):
        try:
            if live:
                rotate_live(path, max_bytes=max_bytes, keep=keep)
                append_private_text(path, time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
                return
            path.parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > max_bytes:
                for number in range(keep, 1, -1):
                    previous = Path(f"{path}.{number - 1}")
                    if previous.exists():
                        os.replace(previous, f"{path}.{number}")
                os.replace(path, f"{path}.1")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except OSError:
            pass
    return log
