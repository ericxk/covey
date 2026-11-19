"""What the 设置 card's 脱敏 tab shows of the relays' records (app/redaction_panel.py): a term's restore tag and the
counts the relays recorded (core/redaction.py ``recent_turns``) by term and by topic. Reads only.
"""
from __future__ import annotations

import collections
import re
import threading
import time
from pathlib import Path

from covey.core.jobs import find_job_record
from covey.core.redaction import recent_turns
from covey.core.topics import load_topic, topic_file

MAX_PREVIEW = 20000
RECENT = 50  # Turns ``stats_view`` lists.
_TOPIC_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
TOPICS_KEPT = 2000  # topic_info's cache: (state dir, topic id) -> (topic.json's mtime, what it read).
_topics, _topics_lock = {}, threading.Lock()


def restore_tag(config, redactor, term):
    if not config["restore"]:
        return "不还原"
    return "还原" if redactor.restorable(term) else "不还原"


def term_hits(rows, since=0.0):
    hits = collections.Counter()
    for row in rows:
        if float(row.get("ts") or 0) < since or not isinstance(row.get("hits"), dict):
            continue
        for term, count in row["hits"].items():
            if isinstance(count, int):
                hits[term] += count
    return hits


def topic_info(state_dir, topic_id):
    """A topic's number, title and switch, from its topic.json and first job; {} when unreadable. Kept while topic.json's
    mtime stays (its first job's words never change): the 脱敏 tab draws the topics of the last 500 turns on each click,
    and reading them took 0.3 s on the NAS."""
    if not _TOPIC_ID.fullmatch(topic_id):
        return {}
    key = (str(state_dir), topic_id)
    try:
        stamp = topic_file(Path(state_dir), topic_id).stat().st_mtime_ns
    except OSError:
        return {}
    with _topics_lock:
        kept = _topics.get(key)
    if kept and kept[0] == stamp:
        return dict(kept[1])
    info = _topic_info(state_dir, topic_id)
    if info:
        with _topics_lock:
            if len(_topics) >= TOPICS_KEPT:
                _topics.clear()
            _topics[key] = (stamp, dict(info))
    return info


def _topic_info(state_dir, topic_id):
    try:
        topic = load_topic(Path(state_dir), topic_id)
    except (OSError, ValueError):
        return {}
    if not isinstance(topic, dict):
        return {}
    title = ""
    try:
        job = find_job_record(Path(state_dir), str(topic.get("root_id") or ""))
        title = str((job or {}).get("user_text") or "").strip().splitlines()[0][:40]
    except (OSError, ValueError, IndexError):
        pass
    number = topic.get("topic_number")
    return {"number": number if type(number) is int else None, "title": title,
            "workspace": str(topic.get("workspace_name") or ""), "redaction": topic.get("redaction") is True}


def stats_view(state_dir, now=None):
    """Hits per term (all recorded, and the last 7 days), the latest turns, and the topics with redaction
    on among those the turns name (no scan of topics/)."""
    rows = recent_turns(state_dir)
    now = time.time() if now is None else now
    hits, week = term_hits(rows), term_hits(rows, now - 7 * 86400)
    topics = {}
    for row in rows:
        topic_id = str(row.get("topic_id") or "")
        if not topic_id:
            continue
        seen = topics.setdefault(topic_id, {"turns": 0, "hits": 0, "last": 0.0})
        seen["turns"] += 1
        seen["hits"] += sum(n for n in (row.get("hits") or {}).values() if isinstance(n, int)) \
            if isinstance(row.get("hits"), dict) else 0
        seen["last"] = max(seen["last"], float(row.get("ts") or 0))
    info = {topic_id: topic_info(state_dir, topic_id) for topic_id in topics}
    turns = []
    for row in reversed(rows[-RECENT:]):
        topic_id = str(row.get("topic_id") or "")
        counts = row.get("hits") if isinstance(row.get("hits"), dict) else {}
        turns.append({"ts": row.get("ts"), "topic_id": topic_id, "number": info.get(topic_id, {}).get("number"),
                      "title": info.get(topic_id, {}).get("title", ""), "harness": row.get("harness", ""),
                      "model": row.get("model", ""), "requests": row.get("requests", 0),
                      "replaced": sum(n for n in counts.values() if isinstance(n, int)), "hits": counts})
    on = [{"topic_id": topic_id, **info[topic_id], **seen} for topic_id, seen in topics.items()
          if info[topic_id].get("redaction")]
    on.sort(key=lambda t: t["last"], reverse=True)
    return {"terms": [{"term": term, "hits": count, "week": week.get(term, 0)} for term, count in hits.most_common()],
            "week_hits": sum(week.values()), "turn_count": len(rows), "turns": turns, "topics": on}
