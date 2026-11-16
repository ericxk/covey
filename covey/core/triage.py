"""问题分流的记录和开关 (docs/butler.md 专项的问题分流, COVEY_PROJECT_TRIAGE=1): what a question record says about being held for a
project's lead, and the switch. A member topic's choice question can be kept from the owner for ``DEADLINE`` seconds while the
lead answers it from the project's memory or hands it back; the record's ``triage`` field is the whole of it, and nothing else
about the question changes (its ``status`` stays ``pending``, so nothing that reads questions sees a new state).

``triage`` = {state, at, deadline, project, receipt, why, by, answering}. ``state`` moves only along TRIAGE_TRANSITIONS
(AGENTS.md rule 4): held → answered_by_lead | escalated, both final. Pure; the questions' own file is written by
app/triage.py, under the question lock.

Switch off, no record has the field and every function here says "not held"."""
from __future__ import annotations

from covey.core import env as cenv, projects

ENV = "COVEY_PROJECT_TRIAGE"  # ``1`` (and COVEY_PROJECTS=1): on; anything else, unset too: off.
DEADLINE = 180  # Seconds a question is held for the lead before it goes to the owner.
ANSWERING = 90  # Seconds an answer the lead is sending is left alone by the timeout.
HELD, ANSWERED, ESCALATED = "held", "answered_by_lead", "escalated"
TRIAGE_TRANSITIONS = {HELD: {ANSWERED, ESCALATED}, ANSWERED: set(), ESCALATED: set()}
STATES = frozenset(TRIAGE_TRANSITIONS)


def enabled() -> bool:
    """Whether COVEY_PROJECTS=1 and COVEY_PROJECT_TRIAGE=1 (read on every call)."""
    return projects.enabled() and str(cenv.get(ENV, "")).strip() == "1"


def of(record) -> dict:
    """The record's ``triage`` field, {} when it has none (or an invalid one)."""
    found = record.get("triage") if isinstance(record, dict) else None
    return found if isinstance(found, dict) and found.get("state") in STATES else {}


def state(record) -> str:
    return of(record).get("state", "")


def held(record) -> bool:
    """Whether the question is kept from the owner for the lead: pending, and held."""
    return isinstance(record, dict) and record.get("status", "pending") == "pending" and state(record) == HELD


def expired(record, now) -> bool:
    """Whether a held question has waited past its deadline (and no answer of the lead's is on its way)."""
    found = of(record)
    if found.get("state") != HELD:
        return False
    if now - float(found.get("answering") or 0) < ANSWERING and found.get("answering"):
        return False
    return now >= float(found.get("deadline") or 0)


def moved(record, new, **fields) -> dict:
    """``record`` with its triage state ``new`` and ``fields`` added; ValueError for a move TRIAGE_TRANSITIONS does not have."""
    found = of(record)
    if new not in TRIAGE_TRANSITIONS.get(found.get("state", ""), ()):
        raise ValueError(f"无效的分流状态转换：{found.get('state')} → {new}")
    return {**record, "triage": {**found, **fields, "state": new}}
