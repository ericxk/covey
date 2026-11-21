"""What the butler noticed and would have done (docs/butler.md 我本来想找你的事): records and numbers, no judging.

Off unless COVEY_PATROL is ``shadow`` (``on`` is kept for a later release and means ``shadow`` now; anything else, unset
too, is off): then app/patrol.py looks at the records every minute with rules, asks the small model every half hour about
what is new, and writes down what it would do. It sends nothing, wakes nobody and changes no project; the digest lists
the decisions it would have acted on (app/digest.py).

<state>/patrol/::

    state.json                      # ``seen`` (object key -> {sig, at, last}: what was recorded and for which state of
                                    # it), ``pending`` (candidates the small model has not judged), ``calls`` ({day, n})
    candidates/<YYYY-MM-DD>.jsonl   # one line per candidate recorded: {id, at, kind, key, sig, chat, topic, project,
                                    # title, what, wait} (``what`` is one line of words for the owner)
    decisions/<YYYY-MM-DD>.jsonl    # one line per decision: {id, at, candidate, kind, key, chat, title, what, verdict,
                                    # why, action}; verdict drop|note|handle, action ask|join|tidy|wake|none

A candidate is recorded once per object while its ``sig`` (the state it was found in) stays the same, and again when the
sig changes or the object left the rules' view for a day. The files of a day older than RETAIN_DAYS go with the
maintenance (core/maintenance_rules.py PatrolRecords). Only the one patrol thread writes; readers take no lock.
Standard library only, with core/env.py and core/state.py.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from covey.core import env as cenv
from covey.core.state import read_object, write_json

ENV = "COVEY_PATROL"
FOLDER = "patrol"
STATE = "state.json"
RETAIN_DAYS = 14
GONE = 86400  # An object not found for this long is forgotten: found again, it is new.
ZONE = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Shanghai, as the digest's days.

KINDS = ("ask", "unseen", "fails", "stuck", "group", "join", "project_idle", "project_messy", "deferred", "unrelayed")
# Recorded in candidates/ and never put to the small model (#55: watched for a week first, rules only).
RECORD_ONLY = frozenset({"deferred", "unrelayed"})
VERDICTS = ("drop", "note", "handle")
ACTIONS = ("ask", "join", "tidy", "wake", "none")

ACTION_WORDS = {"ask": "主动来问你", "join": "归进专项", "tidy": "建议收起或拆开这个专项", "wake": "叫醒负责人看一眼",
                "none": "先记着，不打扰你"}  # What a decision's action says in the digest.

# name: (default, smallest, largest); the variable is named in ``raw``. Minutes, so that a trial can wait for minutes.
NUMBERS = {
    "ask_minutes": (120, 1, 10080),  # A question waits this long for the owner.
    "unseen_minutes": (360, 1, 10080),  # A finished result stays unseen this long.
    "stall_minutes": (180, 1, 10080),  # A running task's record stays unchanged this long.
    "fails": (2, 2, 20),  # Turns of one topic failed in a row.
    "project_minutes": (720, 1, 43200),  # A project with work waiting does not move this long.
    "relay_minutes": (10, 1, 1440),  # A topic's result whose relay to the top level is not confirmed sent this long.
    "judge_minutes": (30, 1, 1440),  # Between two calls of the small model.
    "tick_seconds": (60, 5, 3600),  # Between two looks of the rules.
    "daily_calls": (48, 1, 1000),  # Calls of the small model a day.
    "judge_ms": (30000, 1000, 120000),  # One call's hard limit.
}


def raw(name, environ=None):
    """The text of the variable behind tunable ``name``."""
    return {
        "ask_minutes": cenv.get("COVEY_PATROL_ASK_MINUTES", "", environ),
        "unseen_minutes": cenv.get("COVEY_PATROL_UNSEEN_MINUTES", "", environ),
        "stall_minutes": cenv.get("COVEY_PATROL_STALL_MINUTES", "", environ),
        "fails": cenv.get("COVEY_PATROL_FAILS", "", environ),
        "project_minutes": cenv.get("COVEY_PATROL_PROJECT_MINUTES", "", environ),
        "relay_minutes": cenv.get("COVEY_PATROL_RELAY_MINUTES", "", environ),
        "judge_minutes": cenv.get("COVEY_PATROL_JUDGE_MINUTES", "", environ),
        "tick_seconds": cenv.get("COVEY_PATROL_TICK_SECONDS", "", environ),
        "daily_calls": cenv.get("COVEY_PATROL_DAILY_CALLS", "", environ),
        "judge_ms": cenv.get("COVEY_PATROL_JUDGE_MS", "", environ),
    }[name]


def mode(environ=None) -> str:
    """``shadow`` for COVEY_PATROL=shadow or on, else ``off``."""
    value = str(cenv.get(ENV, "", environ) or "").strip().lower()
    return "shadow" if value in ("shadow", "on") else "off"


def enabled(environ=None) -> bool:
    return mode(environ) == "shadow"


def number(name, environ=None):
    """The tunable ``name`` (NUMBERS), clamped; its default when unset or not a number."""
    default, low, high = NUMBERS[name]
    text = str(raw(name, environ) or "").strip()
    try:
        value = type(default)(float(text)) if text else default
    except ValueError:
        return default
    return max(low, min(high, value))


def folder(state_dir) -> Path:
    return Path(state_dir) / FOLDER


def day_of(now) -> str:
    return datetime.datetime.fromtimestamp(now, ZONE).strftime("%Y-%m-%d")


def load(state_dir) -> dict:
    """state.json as {seen, pending, calls} (empty parts for a missing or bad file)."""
    data = read_object(folder(state_dir) / STATE)
    seen, pending, calls = data.get("seen"), data.get("pending"), data.get("calls")
    return {"seen": seen if isinstance(seen, dict) else {},
            "pending": [row for row in pending if isinstance(row, dict)] if isinstance(pending, list) else [],
            "calls": calls if isinstance(calls, dict) else {}}


def save(state_dir, data) -> None:
    write_json(folder(state_dir) / STATE, {"schema_version": 1, **data}, ensure_ascii=False)


def append(state_dir, part, now, row) -> None:
    """One line on today's file of ``part`` (candidates or decisions)."""
    path = folder(state_dir) / part / f"{day_of(now)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def rows(state_dir, part, since, until) -> list:
    """The lines of ``part`` written after ``since`` up to ``until`` (their ``at``), oldest first; a line that is not an
    object is skipped. Reads the files of the days between only."""
    found = []
    day = datetime.datetime.fromtimestamp(since, ZONE).date()
    last = datetime.datetime.fromtimestamp(until, ZONE).date()
    while day <= last:
        path = folder(state_dir) / part / f"{day.isoformat()}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("at"), (int, float)) and since < value["at"] <= until:
                found.append(value)
        day += datetime.timedelta(days=1)
    return sorted(found, key=lambda row: row["at"])


def calls_today(state, now) -> int:
    calls = state["calls"]
    return int(calls.get("n") or 0) if calls.get("day") == day_of(now) else 0
