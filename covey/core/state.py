#!/usr/bin/env python3
"""Shared local state and workspace helpers for Feishu bridge scripts: private files and directories (and the
private staging folders a file is copied into before it goes out), JSON records written whole, timestamps. Logs are core/logs.py, a topic's history core/history_storage.py, its native
session id core/session_state.py."""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from contextlib import contextmanager
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


def atomic_write_bytes(path: Path, data: bytes, *, prefix: str | None = None, suffix: str = ".tmp",
                       notify: bool = True, remake=None) -> None:
    """``data`` as the whole of ``path``: written to a private temp file next to it, then renamed over it, so a
    reader sees the old file or the new one. The directory must exist (FileNotFoundError otherwise).

    The temp file is ``<prefix>XXXXXXXX<suffix>`` (a unique name per writer, so concurrent writers cannot truncate
    each other's half-written one; ``prefix`` defaults to ``.<name>.``, the dotted ``*.tmp`` form the backup and the
    store skip). A failed write leaves no temp file. ``notify`` tells core/store.py that ``path`` changed, as every
    record write must (the one writer that must not is the store's own index file). ``remake``: called when the
    directory is gone as the temp file is made, before one more try (only then: a directory removed later is not made again).

    This does NOT serialize read-modify-write cycles: a concurrent update can still be lost. Add file locking if
    registry writes ever become frequent."""
    folder = path.parent
    prefix = f".{path.name}." if prefix is None else prefix
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=folder)
    except FileNotFoundError:
        if remake is None:
            raise
        remake()
        fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=folder)
    tmp = Path(tmp_name)
    try:
        if _UMASK is None or _UMASK & PRIVATE_FILE_MODE:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if notify:
        from covey.core.store import invalidate_directory, note_change
        invalidate_directory(folder, path.name)
        note_change(folder, path.name)  # Other processes catch up from this line (core/store.py).


def write_private_text(path: Path, content: str) -> None:
    folder = path.parent
    data = content.encode("utf-8")
    if folder not in _PRIVATE_DIRS:
        ensure_private_dir(folder)
        _PRIVATE_DIRS.add(folder)
    atomic_write_bytes(path, data, remake=lambda: ensure_private_dir(folder))  # Removed since this process made it.


def private_staging(state_dir, name):
    """<state>/<name>, made private (0700) when missing: where a file is copied before it goes to Feishu."""
    staging = state_dir / name
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    return staging


@contextmanager
def private_folder(state_dir, name):
    """A new folder in private_staging(state_dir, name); its files and the folder are removed afterwards."""
    folder = Path(tempfile.mkdtemp(dir=private_staging(state_dir, name)))
    try:
        yield folder
    finally:
        for item in folder.iterdir():
            item.unlink(missing_ok=True)
        folder.rmdir()


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


READ_THREADS = 16  # Files read_each reads at a time.


def read_each(paths, read):
    """``[read(path) for path in paths]``, in that order, READ_THREADS at a time: on NAS a small file's read is mostly
    a wait for the server (718 launch forms one by one took 1.6 s at the Bridge's start, T648). ``read`` must only read
    and be safe on several threads; what it raises is raised here, as the loop would."""
    paths = list(paths)
    if len(paths) < 2:
        return [read(path) for path in paths]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(min(READ_THREADS, len(paths))) as pool:
        return list(pool.map(read, paths))


def keyed_json_path(state_dir, folder, key) -> Path:
    """``<state_dir>/<folder>/<key>.json``, the record of one key in a records folder."""
    return Path(state_dir) / folder / f"{key}.json"


def read_object(path: Path) -> dict:
    """The JSON object in ``path``; {} when the file is missing, cannot be read or holds anything else. Only for reading:
    a write about to change the file reads it with ``read_for_write`` (tests/test_structure.py StateWriteTest)."""
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class Unreadable(OSError):
    """A JSON record that is there but cannot be read (a network disk's passing error, a torn file, JSON of another kind):
    the write about to change it is refused rather than writing the whole file over with only what it adds (T870 Wave 3.5,
    W1-F). An OSError, so a caller that already stops at one stops at this too; ``str`` is a Chinese line for the log."""


_KINDS = {dict: "一个对象", list: "一个列表"}


def read_for_write(path, *, expect=dict):
    """The JSON value in ``path`` a write is about to change: ``expect()`` when there is no file yet; Unreadable when there
    is one that cannot be read or parsed, or holds anything but an ``expect``. Read under the lock the write holds."""
    path = Path(path)
    try:
        value = read_json(path)
    except FileNotFoundError:
        return expect()
    except (OSError, ValueError) as exc:
        try:
            gone = not path.exists()
        except OSError:
            gone = False
        if gone:
            return expect()
        raise Unreadable(f"{path.name} 读不出来，这次不写，免得盖掉原来的内容：{type(exc).__name__}") from exc
    if not isinstance(value, expect):
        raise Unreadable(f"{path.name} 不是{_KINDS.get(expect, expect.__name__)}，这次不写")
    return value


def update_json(path, change, *, default=dict, **dumps):
    """``read_for_write`` (``default`` as its ``expect``), ``change(value)``, ``write_json`` (``dumps`` as its own): the value
    ``change`` returns, or the one it edited in place when it returns None, is written and returned. The caller holds the
    file's lock; Unreadable leaves the file as it was."""
    path = Path(path)
    value = read_for_write(path, expect=default)
    changed = change(value)
    value = value if changed is None else changed
    write_json(path, value, **dumps)
    return value


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
