"""A topic's completion criteria (完成标准): 1 to MAX_ITEMS short lines saying when its work is done, kept in its
topic.json (KEY). The butler gives them when it opens the topic (topic_start ``criteria``) and changes them when the
owner says so (topic_send ``criteria``, app/butler_topics.py); a Q&A topic carries none. Each turn of a topic that has
them is told them (``note``, after its checkpoint file, app/runs.py) and marks each in its checkpoint (``marks``:
满足 / 没满足 / 没查); its answer card shows only the ones not met (``unmet``, cards/answer_card.py criteria_element).
A topic without criteria, and every turn of it, is as before."""
from __future__ import annotations

import json
import re
from pathlib import Path

from covey.core.state import read_object

KEY = "criteria"
MAX_ITEMS = 4
ITEM_CHARS = 80
MET, UNMET, UNCHECKED = "满足", "没满足", "没查"
STATES = (MET, UNMET, UNCHECKED)
EVIDENCE_CHARS = 120
MARKER = re.compile(r"^(?:[-•·*]|\d{1,2}[.、)）])\s*")  # A list's own marker before a line ("1. ", "- ").

RULE = ("完成标准（这件事做到这些才算完，这一轮结束前逐条核对）：\n{items}\n"
        '在检查点写 criteria：[{{"item":"标准原文","state":"满足|没满足|没查","evidence":"一句依据：跑了什么、看到什么"}}]，'
        "每条一项；没实际查过的写“没查”，别把推断写成满足。")


def _line(value, limit) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def clean(raw) -> list | None:
    """A call's ``criteria`` as at most MAX_ITEMS lines of at most ITEM_CHARS each, empty and repeated ones left out:
    a list of texts, or one text a line each. None when it is neither (the call gave none)."""
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        return None
    items = []
    for value in raw:
        line = MARKER.sub("", _line(value, ITEM_CHARS)).strip()
        if line and line not in items:
            items.append(line)
    return items[:MAX_ITEMS]


def of_topic(topic) -> list:
    """The criteria of a topic.json's data ``topic``; [] for none."""
    found = clean((topic or {}).get(KEY)) if isinstance(topic, dict) else None
    return found or []


def read(state_dir, topic_id) -> list:
    """The criteria of the topic ``topic_id``; [] for none or a topic.json that cannot be read."""
    if not topic_id or "/" in str(topic_id) or str(topic_id).startswith("."):
        return []
    try:
        return of_topic(read_object(Path(state_dir) / "topics" / str(topic_id) / "topic.json"))
    except (OSError, ValueError, TypeError):
        return []


def note(items) -> str:
    """The lines each turn of a topic with ``items`` is told, '' without."""
    if not items:
        return ""
    return RULE.format(items="\n".join(f"{index}. {item}" for index, item in enumerate(items, 1)))


def marks(cp) -> list:
    """The checkpoint's ``criteria`` as [(item, state, evidence)], an unknown state read as 没查; [] without."""
    raw = cp.get(KEY) if isinstance(cp, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    found = []
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, dict):
            continue
        state = _line(entry.get("state"), 8)
        found.append((_line(entry.get("item"), ITEM_CHARS), state if state in STATES else UNCHECKED,
                      _line(entry.get("evidence"), EVIDENCE_CHARS)))
    return found


def unmet(items, cp) -> list:
    """[(state, item)] of each of the topic's ``items`` its turn's checkpoint ``cp`` does not mark 满足: 没满足, or 没查
    for one it marks so or not at all. A mark names its item by its words; when none of them does and there are as many
    marks as items, they go in order."""
    found = marks(cp)
    by_words = {item: state for item, state, _ in found if item}
    in_order = len(found) == len(items) and not any(item in by_words for item in items)
    rows = []
    for index, item in enumerate(items):
        state = found[index][1] if in_order else by_words.get(item, UNCHECKED)
        if state != MET:
            rows.append((state, item))
    return rows
