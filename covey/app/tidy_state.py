"""What the parts of 整理专项 share and change while it runs: whether a whole 整理 is running (``busy``), how far its reading is
(``progress``), the chat's topic count read lately (``counted``) and what app/unified.py hands in (``handed``). One object made by
app/project_tidy.py ProjectTidy and given to each part that reads or changes any of it; nothing else is shared between the parts."""
from __future__ import annotations

import threading

from covey.core.butler import is_butler_topic
from covey.core.jobs import jobs_dir
from covey.core.store import directory_store

COUNT_KEPT = 600.0  # The chat's topic count is read again after this.


class TidyState:
    """The mutable state of one ProjectTidy. Rebound as a whole (``progress``, ``counted``), so the parts hold this object and
    read the field each time, never a copy of it."""

    def __init__(self) -> None:
        self.handed = {}  # BOARD_FROM: the 看板 redrawn after a proposal decided on it (app/unified.py fills it).
        self.busy = threading.Lock()  # Held by a whole 整理 (app/tidy_run.py ``start``, ``guarded``).
        self.progress = {"done": 0, "total": 0}  # The reading of a whole 整理: topics read, of how many.
        self.counted = (0.0, 0)  # (when, how many topics the chat has) of the last ``topic_count``.

    def topic_count(self, state, chat, now) -> int:
        """How many topics the chat has had (the butler's family left out), read once every COUNT_KEPT seconds."""
        at, count = self.counted
        if now - at < COUNT_KEPT and at:
            return count
        topics = {record.get("topic_id") for _, record in directory_store(jobs_dir(state)).query(chat_id=chat)}
        count = sum(1 for topic in topics if isinstance(topic, str) and topic and not is_butler_topic(topic, chat))
        self.counted = (now, count)
        return count
