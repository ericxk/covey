"""When the owner last saw each topic: <state>/topic-seen.json, a JSON object of topic_id -> the epoch seconds
(float) of the latest read receipt (im.message.message_read_v1) for a message the bot sent in that topic.
app/topic_seen.py writes it from the read events; the pinned topics board reads it to tell a result the owner has
not looked at from one they have. Both are off unless COVEY_PINNED_TOPICS=1 (docs/configuration.md).

A topic keeps its latest time only: a receipt older than the one saved changes nothing. A topic last seen more than
KEEP seconds ago is dropped, and of the rest at most LIMIT, the most recent, so the file stays small. One process
writes it (the Bridge's read event consumer), under one lock; a reader sees the whole file before or after a write.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from covey.core import env as cenv
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

PINNED_TOPICS_ENV = "COVEY_PINNED_TOPICS"
SEEN_FILE = "topic-seen.json"
KEEP = 90 * 86400
LIMIT = 2000


def enabled() -> bool:
    return cenv.get(PINNED_TOPICS_ENV, "").strip() == "1"


def seen_path(state_dir: Path) -> Path:
    return Path(state_dir) / SEEN_FILE


def _seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def read_seen(state_dir: Path) -> dict[str, float]:
    """topic_id -> when the owner last read a bot message there; {} when the file is missing or unreadable.
    An entry that is not a topic and a positive time is left out."""
    return {topic: seconds for topic, value in read_object(seen_path(state_dir)).items()
            if isinstance(topic, str) and topic and (seconds := _seconds(value)) is not None}


def record_reads(state_dir: Path, reads: dict[str, float], *, now: float | None = None) -> dict[str, float]:
    """Merge ``reads`` (topic_id -> read time, epoch seconds), keeping each topic's latest, then drop what KEEP
    and LIMIT leave out. Written only when something changed. The topics whose time moved on, with their time."""
    now = time.time() if now is None else now
    path = seen_path(state_dir)
    with resource_lock("topic-seen", path):
        saved = read_seen(state_dir)
        seen = dict(saved)
        moved = {}
        for topic, value in reads.items():
            seconds = _seconds(value)
            if isinstance(topic, str) and topic and seconds is not None and seconds > seen.get(topic, 0):
                seen[topic] = moved[topic] = seconds
        kept = sorted(((t, s) for t, s in seen.items() if s >= now - KEEP), key=lambda item: item[1],
                      reverse=True)[:LIMIT]
        seen = dict(kept)
        if seen != saved:
            write_json(path, seen)
        return {topic: seconds for topic, seconds in moved.items() if topic in seen}
