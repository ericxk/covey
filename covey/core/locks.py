"""Resource-scoped locks. The registry never holds a lock during I/O."""
import fcntl
import os
import re
import threading
import weakref
from pathlib import Path

from covey.core.file_lock import open_lock, runtime_locks_enabled

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()
# Records the bridge and its task runners both rewrite: held across processes too. "answer-cards"
# (app/answer_cards.py, app/saved_send.py keep): a task runner keeps its answer's Outgoing and lists its cards while the
# Bridge writes back an upload's 产物 (refresh); held only around reading and writing that file, never another lock.
SHARED = {"record", "question", "answer-cards"}
# A card's lock orders its writes: each takes its sequence (core/card_sequence.reserve) and goes out under it.
# Once configure_card_locks names the state directory (the Bridge and its task runners at start), a card's lock
# is held across processes too, in <state>/cards/.locks/<card id>.lock: the bot and a task runner writing one
# card in the same second no longer both take "this second + 1" (repo review E4). A process of an older
# release locks a card in that process only, as before.
_cards = None
CARD_ID = re.compile(r"[0-9A-Za-z_-]{1,128}")


def configure_card_locks(state_dir):
    """Hold card locks across processes under ``state_dir`` (None: in this process only); the previous one."""
    global _cards
    previous = _cards.parent if _cards else None
    _cards = Path(state_dir).absolute() / "cards" if state_dir else None
    return previous


class SharedFileLock:
    """A reentrant thread lock that also holds a lock file, so other processes wait as well.

    One object per lock file: on NFS a file lock belongs to the process, not the
    descriptor, so a second descriptor here would neither wait nor keep it held.
    """

    def __init__(self, path):
        self.path = path
        self.local = threading.RLock()
        self.depth = 0
        self.fd = None

    def __enter__(self):
        self.local.acquire()
        if self.depth == 0:
            try:
                self.fd = self._open_locked()
            except BaseException:
                self.local.release()
                raise
        self.depth += 1
        return self

    def _open_locked(self):
        if runtime_locks_enabled():
            fd = open_lock(self.path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return fd
            except BaseException:
                os.close(fd)
                raise
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        while True:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                # Maintenance may have removed the file while this waited: then lock the current one.
                if _same_file(fd, self.path):
                    return fd
            except BaseException:
                os.close(fd)
                raise
            os.close(fd)

    def __exit__(self, *exc):
        try:
            self.depth -= 1
            if self.depth == 0:
                fd, self.fd = self.fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            self.local.release()
        return False


def _same_file(fd, path):
    try:
        current = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def shared_file_lock(path):
    """Lock a record file across threads and processes (a sidecar file in its .locks folder)."""
    path = Path(path).resolve() if runtime_locks_enabled() else Path(path).absolute()
    lockfile = path.parent / ".locks" / (path.name + ".lock")
    if runtime_locks_enabled():
        lockfile = lockfile.resolve()
    identity = ("shared-file", str(lockfile))
    with _guard:
        lock = _locks.get(identity)
        if lock is None:
            lock = SharedFileLock(lockfile)
            _locks[identity] = lock
        return lock


def remove_unused_lock(path):
    """Delete a record's lock file when no thread or process holds it; False when one does.

    Lock files carry no data and are created again on demand. A process waiting on the
    removed file notices once it gets it and locks the new one (``_open_locked``)."""
    if runtime_locks_enabled():
        # A waiter may already have this inode open. Runtime locks live until host
        # shutdown; neither these files nor legacy cloud sidecars are deleted here.
        return False
    lock = shared_file_lock(path)
    if not lock.local.acquire(blocking=False):
        return False
    try:
        if lock.depth:
            return False  # This thread holds it.
        try:
            fd = os.open(lock.path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return True
        try:
            # With no holder in this process, a refused lock is another process's.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _same_file(fd, lock.path):
                os.unlink(lock.path)
        except OSError:
            return False
        finally:
            os.close(fd)
        return True
    finally:
        lock.local.release()


def resource_lock(namespace, key):
    if namespace in SHARED:
        return shared_file_lock(key)
    cards = _cards
    if namespace == "card" and cards is not None and isinstance(key, str) and CARD_ID.fullmatch(key):
        return shared_file_lock(cards / key)
    identity = (namespace, str(key))
    with _guard:
        lock = _locks.get(identity)
        if lock is None:
            lock = threading.RLock()
            _locks[identity] = lock
        return lock


def card_lock(progress):
    return resource_lock('card', getattr(progress, 'card_id', '') or id(progress))


# Workspace config and topic.json rewrites, and the checks that must see every topic at once
# (a submission registering its job, idle maintenance). Held briefly: never across a Feishu call.
LOCK = threading.RLock()


class _TopicLock:
    def __init__(self, lock):
        self.lock = lock

    def __enter__(self):
        if LOCK._is_owned() and not self.lock._is_owned():
            raise RuntimeError("topic lock requested while holding LOCK")
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False


def topic_lock(ctx):
    """One topic's submissions, launch, retry and binding switch, in order. Feishu calls made
    under it delay only this topic. Taken before LOCK, never while holding it."""
    return _TopicLock(resource_lock("topic", str(ctx.history_path.parent.resolve())))
