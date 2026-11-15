"""Indexed JSON records with legacy-compatible reads and isolated invalid files."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from covey.core import timing
from covey.core.locks import resource_lock
from covey.core.statuses import QUESTION_TRANSITIONS

FRESH_SECONDS = 1.0  # A scan older than this is redone whatever the directory says.
# A scan reads this many files at a time. On NAS, 927 job records read one by one took 2.0-2.3 s for a
# new process (a task runner's first query) and 0.65 s eight at a time; their stats 0.2 s and 0.03 s (S35).
SCAN_THREADS = 8
# Writers note each record they write in its directory's journal (note_change, from write_private_text),
# so a store catches up from the names added since its last look instead of listing the directory:
# with ~1000 job records a changed directory's listing took about 100 ms on NFS, the new lines 1-3 ms (S45).
JOURNAL = ".journal"  # Not *.json: Path.glob('*.json') matches dot files too.
JOURNALED = frozenset({"jobs", "questions", "held-notes"})
LIST_SECONDS = 60.0  # A full listing still runs this often: for a write the journal missed (a hand edit).
INDEX = ".index"  # JsonDirectoryStore.save_index; not *.json either.
INDEXED = ('topic_id', 'chat_id', 'job_id', 'status', 'request_id')  # The fields a query can filter on.
# Fields kept beside INDEXED (in .index too) for ``narrow``, a query's cheap test on them before any record is read;
# no index entry is made of them. An .index of a release before them has none: ``narrow`` is then told so (None).
CARRIED = ('updated_at',)
_scope = threading.local()


def note_change(folder, name):
    """A writer's note that it wrote record ``name`` in ``folder``, for the folders that keep a journal.
    One short append (2-4 ms on NAS); if it fails, readers see the write at their next full listing."""
    folder = Path(folder)
    if folder.name not in JOURNALED:
        return
    try:
        fd = os.open(folder / JOURNAL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (name + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def _record_name(name):
    """A record file's name: ``*.json``, not a dot file (a writer's temp file, the journal), no path."""
    return name.endswith('.json') and not name.startswith('.') and '/' not in name


@contextmanager
def one_scan():
    """One message or click: inside, each store is checked as usual on its first query, and later
    queries rescan only when that scan is over FRESH_SECONDS old, not on every change of the
    directory; a record this process writes meanwhile is re-read by name. Another process's write
    shows within FRESH_SECONDS, as it may anyway with coarse directory timestamps. The 870 job
    files cost 0.13-0.2 s a scan on NAS, several times per message before (2026-10-26). Outside
    the block, and on other threads, nothing changes."""
    if getattr(_scope, "checked", None) is not None:
        yield
        return
    _scope.checked = set()
    try:
        yield
    finally:
        _scope.checked = None


class JsonDirectoryStore:
    """One directory of JSON records, known to this process: each record's indexed fields (INDEXED)
    always, the whole record once a query needed it. A new process learns the fields from .index
    (saved by the Bridge, save_index) and the journal since, so it reads only the records it asks
    for; without .index it reads them all, as a long-lived Bridge does anyway."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.signature = None
        self.checked_at = 0.0
        self.records = {}  # name -> the whole record, once read.
        self.fields = {}  # name -> its INDEXED fields, for every record known.
        self.fingerprints = {}
        self.inodes = {}  # name -> inode number of the file the fields came from.
        self.journaled = self.directory.name in JOURNALED
        self.journal = None  # (inode, offset) of the journal read up to; None: list instead.
        self.listed_at = 0.0
        self.seeded = False  # .index tried (_seed).
        self.version, self.saved, self.saved_at = 0, -1, 0.0
        self.index = {}
        self.invalid = set()
        self.refused = set()  # (name, fingerprint) of the records a ``where`` raised on, already logged.
        self.written = set()  # Names this process wrote since they were last read; None: unknown.

    def query(self, where=None, *, log=None, narrow=None, **filters):
        """(path, a copy of the record) per record matching the indexed ``filters`` and, when given,
        ``where(record)``. A filter's value is one value, or a set (frozenset) of them: any of those
        (status=RUNNING). ``where`` sees the cached record (it must not change it), so only the
        records it accepts are copied. A record ``where`` raises on (a field of the wrong type) is
        left out, and ``log`` gets one line for it until the file changes: one bad record must not
        stop a loop over the others. Inside one_scan() a repeated query reuses the scan. Only the
        records the filters select are read, if not read before. ``narrow(fields)``, when given, tests a record's
        INDEXED and CARRIED fields (a dict of those it has and the store knows: one from an .index before CARRIED
        has none of them) and must be true for every record ``where`` would accept; a record it is false for is not
        read at all. It must say True when a field it needs is missing."""
        asked = time.monotonic()
        with self.lock:
            self._look(asked)
            names = set(self.fields)
            for key, value in filters.items():
                if isinstance(value, (set, frozenset)):
                    names &= set().union(*(self.index.get((key, one), set()) for one in value))
                else:
                    names &= self.index.get((key, value), set())
            if narrow is not None:
                names = {name for name in names if self._narrows(narrow, self.fields[name])}
            self._read_whole(names)
            return [(self.directory / name, copy.deepcopy(self.records[name])) for name in sorted(names)
                    if name in self.records and (where is None or self._accepts(where, name, log))]

    def max_field(self, field):
        """The largest whole number in indexed ``field`` (one of INDEXED) over the records, 0 when none:
        from the fields alone, no record is read (the Bridge's first job number at start, repo review R4)."""
        asked = time.monotonic()
        with self.lock:
            self._look(asked)
            return max((int(value) for fields in self.fields.values() if (value := fields.get(field)) is not None
                        and (isinstance(value, int) or value.isdigit())), default=0)

    def _look(self, asked):
        """Catch up for a query asked at ``asked``; inside one_scan() a fresh scan is reused."""
        checked = getattr(_scope, "checked", None)
        now = time.monotonic()
        if now - asked > 0.005:  # Behind another thread's scan of this directory: wait=jobs:850 (S41).
            timing.add("wait", f"{self.directory.name}:{round((now - asked) * 1000)}")
        if (checked is not None and self in checked and self.written is not None
                and now - self.checked_at <= FRESH_SECONDS):
            self._reread(self.written)
        else:
            self._check(now)
            if checked is not None:
                checked.add(self)

    def catch_up(self):
        """Know the directory as it is now, reading no record that has not changed since .index: a new
        process's head start (app/runner.py warm_records)."""
        with self.lock:
            self._check(time.monotonic())

    def _check(self, now):
        """Catch up when the directory changed or the last look is over FRESH_SECONDS old: from the
        journal's new lines when it can (_from_journal), else by listing the directory."""
        if not self.seeded:
            self._seed(now)
        try:
            stat = self.directory.stat()
            signature = (stat.st_ino, stat.st_mtime_ns)
        except FileNotFoundError:
            signature = None
        if signature != self.signature or now - self.checked_at > FRESH_SECONDS:
            self.checked_at = now
            names = self._from_journal(now)
            if names is not None:
                self._reread(names)
                self.written, self.signature = set(), signature
                timing.add("journal", f"{self.directory.name}:{round((time.monotonic() - now) * 1000)}")
                return
            self.written = set()
            self.listed_at, self.journal = now, self._journal_end()
            # A rescan reads only the files whose inode changed: every writer replaces a record whole
            # (write_private_text renames a new file over it), and this NAS never reuses an inode
            # (300 rewrites, S39). On NFS a stat per file cost 0.3-0.9 s for 976 job records, as
            # each one went to the server once the directory had changed; the listing 1.3 ms.
            inodes = self._list()
            records, fingerprints, fields, invalid, changed = {}, {}, {}, set(), []
            for name, inode in inodes.items():
                if self.inodes.get(name) != inode or (name not in self.fields and name not in self.invalid):
                    changed.append(self.directory / name)
                    continue
                if name in self.invalid:
                    invalid.add(name)
                else:
                    fields[name] = self.fields[name]
                if name in self.records:
                    records[name] = self.records[name]
                if name in self.fingerprints:
                    fingerprints[name] = self.fingerprints[name]
            self._load(changed, records, fingerprints, fields, invalid)
            self.records, self.fingerprints, self.fields, self.invalid = records, fingerprints, fields, invalid
            self.signature, self.inodes = signature, inodes
            self._reindex()
            # Which phase of a flow paid for a rescan, and how much: scan=jobs:254 (S39).
            timing.add("scan", f"{self.directory.name}:{round((time.monotonic() - now) * 1000)}")

    def _seed(self, now):
        """Start from .index: every record's fields and inode, and the journal position they reflect.
        The journal since then (or a listing, should it have been replaced) brings them up to date."""
        self.seeded = True
        if not self.journaled or self.fields or self.invalid:
            return
        try:
            saved = json.loads((self.directory / INDEX).read_text())
            position = tuple(saved["journal"])
            entries = {name: (int(inode), dict(fields)) for name, (inode, fields) in saved["entries"].items()
                       if _record_name(name)}
            age = max(0.0, time.time() - float(saved["saved_at"]))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return
        self.fields = {name: fields for name, (_, fields) in entries.items()}
        self.inodes = {name: inode for name, (inode, _) in entries.items()}
        self.journal, self.listed_at = (int(position[0]), int(position[1])), now - age
        self._reindex()

    def save_index(self, every=30.0):
        """Write .index when something changed since the last one, at most every ``every`` seconds; the
        Bridge does, for the processes it starts (_seed). Whether it wrote one."""
        with self.lock:
            now = time.monotonic()
            if (not self.journaled or self.journal is None or self.written is None or self.version == self.saved
                    or now - self.saved_at < every):
                return False
            # The fields as of self.journal, plus this process's writes not yet read back: those are in
            # the journal after it, read again by whoever starts from this.
            saved = {"saved_at": time.time(), "journal": list(self.journal),
                     "entries": {name: [self.inodes[name], fields] for name, fields in self.fields.items()
                                 if name in self.inodes}}
            version = self.version
            self.saved_at = now
        text = json.dumps(saved, ensure_ascii=False, separators=(",", ":"))
        fd, temp = tempfile.mkstemp(prefix=INDEX + ".", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temp, self.directory / INDEX)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
        with self.lock:
            self.saved = max(self.saved, version)
        return True

    def _list(self):
        """Name -> inode number of each record file (_record_name), without a stat per file."""
        try:
            with os.scandir(self.directory) as entries:
                return {e.name: e.inode() for e in entries if _record_name(e.name)}
        except OSError:
            return {}

    def _journal_end(self):
        """(inode, size) of the journal, taken before a listing: what is noted after it is read next."""
        if not self.journaled:
            return None
        try:
            stat = os.stat(self.directory / JOURNAL)
        except OSError:
            return None
        return stat.st_ino, stat.st_size

    def _from_journal(self, now):
        """The records written since the last look, from the journal's new lines and this process's
        own writes; None when the directory must be listed instead: no journal (yet), LIST_SECONDS
        passed, or the journal was replaced or cut."""
        if self.journal is None or self.written is None or now - self.listed_at > LIST_SECONDS:
            return None
        inode, offset = self.journal
        path = self.directory / JOURNAL
        try:
            stat = os.stat(path)
            if stat.st_ino != inode or stat.st_size < offset:
                return None
            data = b""
            if stat.st_size > offset:
                with open(path, "rb") as handle:
                    if os.fstat(handle.fileno()).st_ino != inode:  # Replaced since the stat.
                        return None
                    handle.seek(offset)
                    data = handle.read(stat.st_size - offset)
        except OSError:
            return None
        end = data.rfind(b"\n") + 1  # A line still being appended is read next time, whole.
        self.journal = (inode, offset + end)
        return {name for name in data[:end].decode("utf-8", "replace").splitlines() if _record_name(name)} | self.written

    def _reread(self, names):
        """Re-read only ``names`` (written since they were read); the rest stays."""
        if not names:
            return
        records, fingerprints, fields, invalid = (dict(self.records), dict(self.fingerprints), dict(self.fields),
                                                  set(self.invalid))
        paths = []
        for name in names:
            records.pop(name, None)
            fingerprints.pop(name, None)
            fields.pop(name, None)
            invalid.discard(name)
            self.inodes.pop(name, None)
            path = self.directory / name
            if path.suffix == '.json' and path.exists():  # What the scan's glob would find.
                paths.append(path)
        self._load(paths, records, fingerprints, fields, invalid)
        self.records, self.fingerprints, self.fields, self.invalid = records, fingerprints, fields, invalid
        self.written = set()
        self._reindex()

    def _read_whole(self, names):
        """Read the records among ``names`` known only by their fields (from .index)."""
        missing = [self.directory / name for name in names if name not in self.records and name in self.fields]
        if not missing:
            return
        records, fingerprints, fields, invalid = (dict(self.records), dict(self.fingerprints), dict(self.fields),
                                                  set(self.invalid))
        self._load(missing, records, fingerprints, fields, invalid)
        changed = any(fields.get(path.name) != self.fields.get(path.name) for path in missing)
        self.records, self.fingerprints, self.fields, self.invalid = records, fingerprints, fields, invalid
        if changed:  # Replaced since .index and not yet caught up: the next look reads it again anyway.
            self._reindex()

    def _load(self, paths, records, fingerprints, fields, invalid):
        paths = list(paths)
        if len(paths) > SCAN_THREADS:
            with ThreadPoolExecutor(SCAN_THREADS, thread_name_prefix='store-scan') as pool:
                files = list(pool.map(self._read, paths))
        else:
            files = map(self._read, paths)
        for name, fingerprint, record in files:
            if fingerprint is not None:
                fingerprints[name] = fingerprint
                self.inodes[name] = fingerprint[0]
            if record is None:
                invalid.add(name)
                fields.pop(name, None)
            else:
                records[name] = record
                fields[name] = {field: record[field] for field in INDEXED + CARRIED
                                if isinstance(record.get(field), (str, int))}

    def _read(self, path):
        """(name, fingerprint, record) of one file; the record this store has when the file is unchanged,
        None when it cannot be read or is not an object. Reads only: the scans run it in parallel."""
        try:
            st = path.stat()
        except OSError:
            return path.name, None, None
        fingerprint = (st.st_ino, st.st_mtime_ns, st.st_size)
        if self.fingerprints.get(path.name) == fingerprint:
            return path.name, fingerprint, self.records.get(path.name)
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return path.name, fingerprint, None
        return path.name, fingerprint, value if isinstance(value, dict) else None

    def _reindex(self):
        index = {}
        for name, fields in self.fields.items():
            for field, value in fields.items():
                if field in CARRIED:
                    continue
                index.setdefault((field, value), set()).add(name)
        self.index = index
        self.version += 1
        self.refused = {seen for seen in self.refused if self.fingerprints.get(seen[0]) == seen[1]}

    @staticmethod
    def _narrows(narrow, fields):
        try:
            return bool(narrow(fields))
        except Exception:  # noqa: BLE001 - a test that cannot tell leaves the record to ``where``, as without it
            return True

    def _accepts(self, where, name, log):
        try:
            return where(self.records[name])
        except Exception as exc:  # noqa: BLE001 - a malformed record is skipped, never the whole query
            seen = (name, self.fingerprints.get(name))
            if log is not None and seen not in self.refused:
                self.refused.add(seen)
                log(f"record {self.directory.name}/{name} skipped: {type(exc).__name__}")
            return False


_stores = {}
_guard = threading.Lock()


def save_indexes(state_dir):
    """.index of each record directory this process has looked at (JsonDirectoryStore.save_index): the
    Bridge's, for the task runners it starts."""
    for name in sorted(JOURNALED):
        with _guard:
            store = _stores.get(str((Path(state_dir) / name).absolute()))
        if store is not None:
            store.save_index()


def directory_store(path):
    key = str(Path(path).absolute())
    with _guard:
        if key not in _stores:
            _stores[key] = JsonDirectoryStore(path)
        return _stores[key]


def update_question(path, change, *, expected=None):
    """The question record at ``path`` as ``change`` leaves it, under the record's lock: ``change`` gets a
    copy and returns the record to save, or None to leave it. A status moves only along
    QUESTION_TRANSITIONS. Written only when it changed, with the next revision (a record from before
    revisions stays without one): an untouched file keeps its mtime, which maintenance reads as the
    topic's last activity. The record, or None when its status is not in ``expected`` or ``change``
    left it. Never hold this lock while sending it."""
    from covey.core.state import write_private_text
    path = Path(path)
    with resource_lock('question', path):
        before = json.loads(path.read_text())
        if expected is not None and before.get('status') not in expected:
            return None
        after = change(copy.deepcopy(before))
        if after is None:
            return None
        old, new = before.get('status', 'pending'), after.get('status', 'pending')
        if old != new and new not in QUESTION_TRANSITIONS.get(old, set()):
            raise ValueError(f'无效的问题状态转换：{old} → {new}')
        if after != before:
            if 'revision' in after:
                after['revision'] = int(after['revision']) + 1
            write_private_text(path, json.dumps(after, ensure_ascii=False))
        return after


def transition_question(path, expected=None, **changes):
    """Compare-and-set a durable intent. Never hold this lock while sending it."""
    def change(record):
        if record.get("delivery_state") == "delivered" and changes.get("delivery_state") == "awaiting_ack":
            changes["delivery_state"] = "delivered"
        record.update(changes)
        record['schema_version'] = 1
        record.setdefault('revision', 0)
        return record
    return update_question(path, change, expected=expected)


def invalidate_directory(path, name=None):
    """This process wrote ``name`` in ``path`` (None: something there). The next query rescans,
    except inside one_scan(), where it re-reads ``name`` alone."""
    # Filesystem directory timestamps may be coarser than successive writes.
    with _guard:
        store = _stores.get(str(Path(path).absolute()))
    if store is not None:
        with store.lock:
            store.signature = object()
            if name is None or store.written is None:
                store.written = None
            else:
                store.written.add(name)
