"""The owner's messages the Bridge still acts on, kept so a recall or an edit of one is noticed (app/source_watch.py).

Feishu's event bus as lark-cli serves it has no recall event and no edit event (lark-cli 1.0.98 knows neither
im.message.recalled_v1 nor an edit), but a message read back says both: ``deleted`` once it was recalled, ``updated``
and a newer ``update_time`` once it was edited. So each message the owner sends that becomes a task or a supplement
is noted here when it is taken in (app/app.py handle_event), and app/source_watch.py reads it back while that task
lives and for GRACE_SECONDS after.

In memory only: a restart forgets them, and the messages of the tasks it adopts are not read back.
"""
from __future__ import annotations

import dataclasses
import threading
import time

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
    steer: tuple | None = None
    added: float = dataclasses.field(default_factory=time.time)
    ended: float = 0.0  # When its task was first seen ended; 0 while it lives.


class SourceBook:
    """The watched messages by message id; ``lock`` guards ``items``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, Watched] = {}

    def note(self, watched: Watched) -> None:
        with self.lock:
            self.items[watched.message_id] = watched
            if len(self.items) > MAX_WATCHED:
                for old in sorted(self.items.values(), key=lambda item: item.added)[:len(self.items) - MAX_WATCHED]:
                    self.items.pop(old.message_id, None)

    def drop(self, message_id: str) -> None:
        with self.lock:
            self.items.pop(message_id, None)

    def get(self, message_id: str) -> Watched | None:
        with self.lock:
            return self.items.get(message_id)

    def snapshot(self) -> list[Watched]:
        with self.lock:
            return list(self.items.values())

    def __len__(self) -> int:
        with self.lock:
            return len(self.items)
