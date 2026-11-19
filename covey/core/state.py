#!/usr/bin/env python3
"""Shared local state and workspace helpers for Feishu bridge scripts: private files and directories, JSON
records written whole, timestamps. Logs are core/logs.py, a topic's history core/history_storage.py, its native
session id core/session_state.py."""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    try:
        path.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass


def _umask():
    """This process's umask, read without changing it (os.umask would, for every thread); None if unknown."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return None


# Each step of a write is a round trip on the NAS (S39: about 20 ms a write). A directory made private
# once is not made so again by this process, and mkstemp's file is already PRIVATE_FILE_MODE unless
# the umask takes some of it away.
_PRIVATE_DIRS: set = set()
_UMASK = _umask()


def write_private_text(path: Path, content: str) -> None:
    # Unique temp name per writer so concurrent threads (worker vs consumer)
    # writing the same file (e.g. workspaces.json) cannot truncate each other's
    # half-written temp file. This does NOT serialize read-modify-write cycles:
    # a concurrent update can still be lost. Add file locking if registry
    # writes ever become frequent.
    folder = path.parent
    if folder not in _PRIVATE_DIRS:
        ensure_private_dir(folder)
        _PRIVATE_DIRS.add(folder)
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=folder)
    except FileNotFoundError:  # Removed since this process made it.
        ensure_private_dir(folder)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=folder)
    tmp = Path(tmp_name)
    try:
        if _UMASK is None or _UMASK & PRIVATE_FILE_MODE:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    from covey.core.store import invalidate_directory, note_change
    invalidate_directory(folder, path.name)
    note_change(folder, path.name)  # Other processes catch up from this line (core/store.py).


def claim_once(folder: Path, name: str) -> bool:
    """True only for the first claim of ``name`` in ``folder``: an empty marker created
    exclusively, so a later claim, from another process or after a restart, is False."""
    ensure_private_dir(folder)
    try:
        os.close(os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE))
    except FileExistsError:
        return False
    return True


def write_json(path: Path, value, **dumps) -> None:
    """``write_private_text`` of ``json.dumps(value, **dumps)``."""
    write_private_text(path, json.dumps(value, **dumps))


_RAISE = object()


def read_json(path: Path, default=_RAISE):
    """``json.loads(path.read_text())``: its OSError and ValueError reach the caller unchanged, but for a missing
    file when ``default`` is given, which is returned instead."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        if default is _RAISE:
            raise
        return default


def keyed_json_path(state_dir, folder, key) -> Path:
    """``<state_dir>/<folder>/<key>.json``, the record of one key in a records folder."""
    return Path(state_dir) / folder / f"{key}.json"


def read_object(path: Path) -> dict:
    """The JSON object in ``path``; {} when the file is missing, cannot be read or holds anything else."""
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_durable_json(path: Path, value):
    """Written, flushed to disk and recorded in its directory before returning."""
    write_private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def utc_now_iso() -> str:
    """The one timestamp form records use, e.g. 2026-10-24T12:00:00Z; readers also accept +00:00."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_seconds(value, *, aware=False) -> float | None:
    """ISO 8601 time ``value`` (a trailing Z too, which 3.10 rejects) as epoch seconds, one without a time zone
    taken as local time; None when it is not a string or not such a time, or with ``aware`` names no time zone."""
    if not isinstance(value, str):
        return None
    try:
        when = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return None if aware and when.tzinfo is None else when.timestamp()
    except ValueError:
        return None


def canonical_workdir(path: Path) -> Path:
    return path.expanduser().resolve()


DAY = 86400  # Seconds in a day.
