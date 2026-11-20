"""话题要点 (docs/projects.md 整理专项, COVEY_PROJECT_TIDY): a few lines each topic keeps of what it is about at heart, written by
the topic's own model at the end of every turn (the checkpoint's ``topic_gist``, core/checkpoint.py checkpoint_gist) or, for a
topic from before, once by a read-only call when 整理专项 first meets it (app/project_sorter.py backfill).

The record, <state>/topics/<topic_id>/gist.json: {schema_version, topic_id, essence (本质), goal (现在的目标), involves (涉及),
related (相关话题: T numbers), changed (和上次比变了什么), updated_at, source (``turn`` or ``backfill``), job (the job key it came
from), essence_at (when the essence last changed), essences (the last ESSENCES_KEPT of them, each {text, at})}; a topic its
backfill could not read keeps ``unreadable`` {reason, at, job} instead and is tried again only after a newer turn.

``save`` says whether the essence changed (``moved``): an essence that reads differently enough from the one before
(``same_essence``: the same words, or close) is the moment of a real-time 整理专项 (app/project_tidy.py). The first essence
a topic has is not a change. Standard library and core only; no model, no Feishu call."""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

SCHEMA = 1
NAME = "gist.json"
FIELDS = ("essence", "goal", "involves", "related", "changed")
WORDS = {"essence": "本质", "goal": "现在的目标", "involves": "涉及", "related": "相关话题", "changed": "和上次比变了什么"}
LIMITS = {"essence": 80, "goal": 160, "involves": 160, "changed": 160}
RELATED_MAX = 8
ESSENCES_KEPT = 5
SAME = 0.6  # Two essences whose character pairs overlap this much (Jaccard) are the same essence reworded.
LABEL = re.compile(r"T\d{1,6}")
_KEYS = {**{word: field for field, word in WORDS.items()}, "目标": "goal", "相关": "related", "变化": "changed",
         "变了什么": "changed"}
_LINE = re.compile(r"^\s*[-*·]?\s*([^:：]{1,12})\s*[:：]\s*(.*)$")


def path(state_dir, topic_id) -> Path:
    return Path(state_dir) / "topics" / str(topic_id) / NAME


def _words(value, limit) -> str:
    if isinstance(value, (list, tuple)):
        value = "、".join(str(item) for item in value if isinstance(item, (str, int)) and not isinstance(item, bool))
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _related(value) -> list:
    if isinstance(value, str):
        found = LABEL.findall(value)
    elif isinstance(value, (list, tuple)):
        found = [match for item in value if isinstance(item, (str, int)) and not isinstance(item, bool)
                 for match in (LABEL.findall(str(item)) or ([f"T{item}"] if str(item).isdigit() else []))]
    else:
        found = []
    return list(dict.fromkeys(found))[:RELATED_MAX]


def parse(raw) -> dict:
    """``raw`` (the checkpoint's ``topic_gist``, or a model's answer) as {essence, goal, involves, related, changed}; {} when it
    has no essence. Takes an object with the English keys or the Chinese words (本质 …), or text of "本质：…" lines."""
    if isinstance(raw, str):
        lines = {}
        for line in raw.splitlines():
            found = _LINE.match(line)
            if found and _KEYS.get(found.group(1).strip()):
                lines[_KEYS[found.group(1).strip()]] = found.group(2)
        raw = lines
    if not isinstance(raw, dict):
        return {}
    data = {_KEYS.get(key, key): value for key, value in raw.items() if isinstance(key, str)}
    gist = {field: _unlabelled(_words(data.get(field), LIMITS[field]), field) for field in LIMITS}
    gist["related"] = _related(data.get("related"))
    return gist if gist["essence"] else {}


def _unlabelled(text, field) -> str:
    """``text`` without its field's own word in front ("本质：…" as the model may copy it from the template)."""
    for word in (WORDS[field], *(key for key, name in _KEYS.items() if name == field)):
        for mark in ("：", ":"):
            if text.startswith(word + mark):
                return text[len(word) + 1:].strip()
    return text


def read(state_dir, topic_id) -> dict:
    """The record ({} without one); its gist fields are there when it has an essence."""
    if not topic_id or "/" in str(topic_id):
        return {}
    return read_object(path(state_dir, topic_id))


def gist_of(record) -> dict:
    """The five fields of a record ({} when it has no essence)."""
    if not isinstance(record, dict) or not record.get("essence"):
        return {}
    return {field: record.get(field) or ([] if field == "related" else "") for field in FIELDS}


def _pairs(text) -> set:
    text = re.sub(r"[\s，。、；：:,.;!！?？（）()「」“”\"'《》<>\[\]-]+", "", str(text or "").lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} or ({text} if text else set())


def same_essence(one, other) -> bool:
    """Whether two essences say the same thing: equal once spaces and punctuation are gone, or their character pairs
    overlap SAME or more."""
    a, b = _pairs(one), _pairs(other)
    if not a or not b:
        return a == b
    return a == b or len(a & b) / len(a | b) >= SAME


def save(state_dir, topic_id, gist, *, source="turn", job="", now=None) -> tuple:
    """(the record written, ``moved``: the essence changed from an earlier one). ``gist`` as ``parse`` gives it; nothing is
    written for one without an essence ((record, False)). The same gist again moves only ``updated_at``."""
    gist = parse(gist)
    now = time.time() if now is None else now
    target = path(state_dir, topic_id)
    if not gist or not gist.get("essence"):
        return read(state_dir, topic_id), False
    with resource_lock("record", str(target)):
        before = read_object(target)
        old = str(before.get("essence") or "")
        moved = bool(old) and not same_essence(old, gist["essence"])
        essences = [item for item in before.get("essences") or () if isinstance(item, dict)]
        if not old or moved:
            essences = [*essences, {"text": gist["essence"], "at": now}][-ESSENCES_KEPT:]
        record = {"schema_version": SCHEMA, "topic_id": str(topic_id),
                  **{field: gist.get(field) or ([] if field == "related" else "") for field in FIELDS},
                  "updated_at": now, "source": source, "job": str(job or ""),
                  "essence_at": now if (moved or not old) else before.get("essence_at") or now, "essences": essences}
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, record, ensure_ascii=False, indent=1)
    return record, moved


def mark_unreadable(state_dir, topic_id, reason, *, job="", now=None) -> dict:
    """A topic the backfill could not read (``reason``: why, e.g. the model refused it): kept so it is not asked again
    until a newer turn (``job`` the newest job key it had). A topic with a gist keeps it."""
    target = path(state_dir, topic_id)
    with resource_lock("record", str(target)):
        record = read_object(target)
        record.update(schema_version=SCHEMA, topic_id=str(topic_id),
                      unreadable={"reason": str(reason or "")[:40], "at": time.time() if now is None else now,
                                  "job": str(job or "")})
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, record, ensure_ascii=False, indent=1)
    return record


def lines(gist) -> str:
    """The gist as the five lines a topic's model and the owner read ('' without an essence)."""
    gist = gist_of(gist) or (gist if isinstance(gist, dict) and gist.get("essence") else {})
    if not gist:
        return ""
    shown = []
    for field in FIELDS:
        value = gist.get(field)
        value = "、".join(value) if isinstance(value, list) else str(value or "")
        if value:
            shown.append(f"{WORDS[field]}：{value}")
    return "\n".join(shown)
