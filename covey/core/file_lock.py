"""Explicit host-local lock files; durable paths never move. See docs/runtime-locks.md."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat

import covey.core.env as cenv

LOCK_ROOT_ENV = "COVEY_LOCK_ROOT"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def runtime_locks_enabled():
    """An explicitly empty setting is invalid, not permission to use cloud locks."""
    return cenv.has(LOCK_ROOT_ENV)


def _private(info, *, directory=False):
    kind, mode = (stat.S_ISDIR, 0o700) if directory else (stat.S_ISREG, 0o600)
    if (not kind(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != mode or (not directory and info.st_nlink != 1)):
        raise ValueError("runtime locks require owner-private directories (0700) and regular files (0600)")


def _open_root():
    root = Path(cenv.get(LOCK_ROOT_ENV))
    if not root.is_absolute() or ".." in root.parts or len(root.parts) < 2:
        raise ValueError("COVEY_LOCK_ROOT must be an absolute private runtime directory")
    fd = os.open(root.anchor, DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(root.parts[1:], 1):
            # Writable ancestors could allow another user to replace the lock directory.
            # Sticky shared parents such as /dev/shm protect entries owned by this user.
            parent = os.fstat(fd)
            if (parent.st_uid not in {0, os.geteuid()}
                    or (parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX)):
                raise ValueError("unsafe runtime lock directory ancestor")
            if index == len(root.parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(os.fstat(fd), directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_lock(path, *, create=True):
    """Open a lock descriptor, without acquiring it. The caller must close it.

    Runtime files are always created, including after reboot. ``create=False``
    still requires the durable parent directory to exist, and never creates it.
    """
    path = Path(path)
    if not runtime_locks_enabled():
        return os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0), 0o600)
    if not create and not stat.S_ISDIR(path.parent.stat().st_mode):
        raise NotADirectoryError(str(path.parent))
    name = hashlib.sha256(os.fsencode(path.resolve())).hexdigest() + ".lock"
    root = _open_root()
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=root)
        try:
            _private(os.fstat(fd))
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(root)


@contextmanager
def file_lock(path, *, operation=fcntl.LOCK_EX, create=True, source=None):
    """Hold flock for this scope. ``source`` preserves locking a data fd by default."""
    borrowed = source is not None and not runtime_locks_enabled()
    fd = source.fileno() if borrowed else open_lock(path, create=create)
    try:
        fcntl.flock(fd, operation)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if not borrowed:
            os.close(fd)
