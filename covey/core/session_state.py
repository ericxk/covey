"""A topic's native session id in its state file, read (read_session_id) and replaced only from the revision
it was read at (capture_session_state, compare_and_set_session), under one process-wide lock."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Pattern

from covey.core.file_lock import file_lock
from covey.core.state import PRIVATE_DIR_MODE, write_private_text


def read_session_id(path: Path, pattern: Pattern[str]) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value if pattern.fullmatch(value) else None


_SESSION_STATE_LOCK = threading.RLock()


def _session_revision_path(path):
    return path.with_name(path.name + ".revision")


def _session_revision(path):
    revision = _session_revision_path(path)
    if not revision.exists():
        return 0
    value = revision.read_text().strip()
    if not value.isdecimal():
        raise ValueError("会话版本记录无效")
    return int(value)


@contextmanager
def _locked_session_state(path):
    with _SESSION_STATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        if path.is_symlink() or _session_revision_path(path).is_symlink():
            raise ValueError("会话状态路径无效")
        with file_lock(path.with_name(path.name + ".lock")):
            yield


def capture_session_state(path: Path, pattern: Pattern[str]):
    with _locked_session_state(path):
        return _session_revision(path), read_session_id(path, pattern)


def _write_session_locked(path, session_id):
    write_private_text(_session_revision_path(path), str(_session_revision(path) + 1))
    write_private_text(path, session_id)


def _clear_session_locked(path, pattern):
    old = read_session_id(path, pattern)
    # A reset of an already empty topic still changes its generation.
    write_private_text(_session_revision_path(path), str(_session_revision(path) + 1))
    path.unlink(missing_ok=True)
    return old


def compare_and_set_session(path, pattern, revision, old_session_id, new_session_id):
    with _locked_session_state(path):
        if type(revision) is not int or (_session_revision(path), read_session_id(path, pattern)) != (revision, old_session_id):
            return False
        if new_session_id is None:
            _clear_session_locked(path, pattern)
            return True
        if not pattern.fullmatch(new_session_id):
            raise ValueError("会话标识无效")
        _write_session_locked(path, new_session_id)
        return True
