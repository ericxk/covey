"""The owner's messages the Bridge still acts on, kept so a recall or an edit of one is noticed (app/source_watch.py).

Feishu's event bus as lark-cli serves it has no recall event and no edit event (lark-cli 1.0.98 knows neither
im.message.recalled_v1 nor an edit), but a message read back says both: ``deleted`` once it was recalled, ``updated``
and a newer ``update_time`` once it was edited. So each message the owner sends that becomes a task or a supplement
is noted here when it is taken in (app/app.py handle_event), and app/source_watch.py reads it back while that task
lives and for GRACE_SECONDS after.

Kept in ``<state>/source-book.json`` (``SourceBook.attach``, only while COVEY_SOURCE_WATCH is on) for MAX_AGE_SECONDS, so a
restart goes on reading them back; what was already done about one (``Watched.acted``, ``stamp``) is kept with it, so a
recall or an edit is not told twice after a restart (rule 6: claimed in the book first, then done).
"""
from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import read_object, update_json

FILE = "source-book.json"
VERSION = 1
KINDS = ("task", "steer", "answer", "held")  # What the message became: a task of its own, a supplement, an answer, a held note.

GRACE_SECONDS = 600  # After the message's task ended: a recall or an edit then still voids or notes its turn.
MAX_AGE_SECONDS = 24 * 3600  # Feishu lets a message be recalled or edited within a day at most.
MAX_WATCHED = 200  # The oldest go first past it.


@dataclasses.dataclass
class Watched:
    """One message: ``app`` the Harness that took it, ``topic_id`` the topic it went to, ``stamp`` its update_time
    when it was taken in (or as it was last read back), ``steer`` (job id, topic id) of the running task it went
    into as a supplement, else None (it became a task of its own, keyed by the message)."""
    message_id: str
    chat_id: str
    app: str
    topic_id: str
    stamp: str
    text: str = ""
    words: str = ""  # What the owner wrote of it, as last known: an edit is a read-back whose words differ (``same_words``).
    steer: tuple | None = None
    kind: str = "task"  # One of KINDS. A "steer" has ``steer`` set; an "answer" or a "held" note has no task of its own.
    acted: dict = dataclasses.field(default_factory=dict)  # What was done for it already: action -> who it reached (job ids, topics).
    added: float = dataclasses.field(default_factory=time.time)
    ended: float = 0.0  # When its task was first seen ended; 0 while it lives.


def _watched(row) -> Watched | None:
    """A ``Watched`` from its saved form; None for a row that is not one."""
    if not isinstance(row, dict) or not str(row.get("message_id") or ""):
        return None
    known = {field.name for field in dataclasses.fields(Watched)}
    values = {key: value for key, value in row.items() if key in known}
    try:
        steer = values.get("steer")
        values["steer"] = (int(steer[0]), str(steer[1])) if steer else None
        values["acted"] = values["acted"] if isinstance(values.get("acted"), dict) else {}
        values["added"] = float(values.get("added") or time.time())
        values["ended"] = float(values.get("ended") or 0.0)
        if values.get("kind") not in KINDS:
            values["kind"] = "steer" if values["steer"] else "task"
        return Watched(**values)
    except (TypeError, ValueError, IndexError):
        return None


class SourceBook:
    """The watched messages by message id; ``lock`` guards ``items``. With a ``path`` (``attach``) every change is written
    there too, under the lock and without a network call."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, Watched] = {}
        self.path: Path | None = None

    def attach(self, state_dir) -> None:
        """Keep the book in ``<state_dir>/source-book.json`` from now on, reading back what is there (a message of this
        process's own wins; one past MAX_AGE_SECONDS is not taken). Once; a second call for the same file does nothing."""
        path = Path(state_dir) / FILE
        with self.lock:
            if self.path == path:
                return
            self.path = path
            now = time.time()
            rows = read_object(path).get("items")
            for row in (rows.values() if isinstance(rows, dict) else ()):
                item = _watched(row)
                if item is not None and now - item.added <= MAX_AGE_SECONDS:
                    self.items.setdefault(item.message_id, item)

    def _write(self) -> None:
        """The book as the file, the lock held. A file that cannot be read is left as it was (Unreadable): the book goes on
        in memory."""
        if self.path is None:
            return
        rows = {item.message_id: dataclasses.asdict(item) for item in self.items.values()}
        try:
            with resource_lock("source-book", str(self.path)):
                update_json(self.path, lambda value: {"version": VERSION, "items": rows}, ensure_ascii=False)
        except OSError:
            pass

    def save(self) -> None:
        """Write the book now, after a message's fields were changed in place."""
        with self.lock:
            self._write()

    def note(self, watched: Watched) -> None:
        with self.lock:
            self.items[watched.message_id] = watched
            if len(self.items) > MAX_WATCHED:
                for old in sorted(self.items.values(), key=lambda item: item.added)[:len(self.items) - MAX_WATCHED]:
                    self.items.pop(old.message_id, None)
            self._write()

    def drop(self, message_id: str) -> None:
        with self.lock:
            if self.items.pop(message_id, None) is not None:
                self._write()

    def claim(self, item: Watched, action: str, who) -> bool:
        """Whether ``who`` (a job id or a topic) is not yet in ``item.acted[action]``; if so it is put there and the book
        written, before the caller does the thing (rule 6: a restart in between leaves it undone, never done twice)."""
        who = str(who)
        with self.lock:
            done = item.acted.setdefault(action, [])
            if who in done:
                return False
            done.append(who)
            self._write()
            return True

    def get(self, message_id: str) -> Watched | None:
        with self.lock:
            return self.items.get(message_id)

    def snapshot(self) -> list[Watched]:
        with self.lock:
            return list(self.items.values())

    def __len__(self) -> int:
        with self.lock:
            return len(self.items)
