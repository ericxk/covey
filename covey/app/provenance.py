"""Whether a turn's words are the owner's own (docs/architecture.md 轮次来源): the one answer for the app layer, where who is
served is known (app/access.py), on top of core/turn_source.py, which cannot see it.

- ``owner_said``: the words are the owner's level (core/turn_source.py ``level``: his own message the Bridge took in, or a
  stamped source with his origin), the turn is his (app/access.py ``owner_turn``) and it is no automatic retry.
- ``synthetic``: the turn was made by the Bridge or an agent, not by the owner's message: a hand-on of the butler's or a
  lead's, a lead's wake-up, a lead's answer for him, a turn of 协作.

Every reader of the app layer that asks whether a turn's words are the owner's asks ``owner_said`` (W1-B: no message id
prefix is compared for it elsewhere), and whether a turn was made by the Bridge or an agent asks ``synthetic``.
"""
from __future__ import annotations

from collections.abc import Mapping

from covey.core import turn_source
from covey.core.jobs import job_field

SYNTHETIC = frozenset({turn_source.RELAY, turn_source.LEAD, turn_source.WAKE, turn_source.ANSWER_FOR, turn_source.ASK,
                       turn_source.REPLY})


def retried(item) -> bool:
    """Whether ``item`` (a job or its record) is an automatic retry: of a limit or a fault (app/auto_retry.py counts it), or
    after a safety refusal (app/policy_retry.py). The same test as app/butler_asks.py and app/collab_flow.py ``retried``."""
    from covey.app.auto_retry import COUNTS

    return bool(job_field(item, "policy_retry")) or any(int(job_field(item, name) or 0) > 0 for name in COUNTS)


def owner_said(item, access) -> bool:
    """Whether the job ``item`` (a job or its record) carries the owner's own words: of his level (core/turn_source.py
    ``level``), of a source whose words are his (``behavior``'s ``owner_words``: a relay carries the owner's message in its
    origin, but its text is the butler's), his turn (``access.owner_turn`` of its chat and sender) and no automatic retry
    (``retried``)."""
    if turn_source.level(item) != turn_source.LEVEL_OWNER or retried(item):
        return False
    if not turn_source.behavior(item).owner_words:
        return False
    return bool(access.owner_turn(str(job_field(item, "chat_id") or ""), str(job_field(item, "sender_id") or "")))


def synthetic(source) -> bool:
    """Whether ``source`` (a job, its record, or the ``turn_source`` field itself) is of a kind ``SYNTHETIC`` names. A
    LEGACY job (no field) is not: the owner's messages are not stamped."""
    if isinstance(source, Mapping) and turn_source.FIELD not in source and "kind" in source:
        source = {turn_source.FIELD: source}  # The field itself.
    return turn_source.kind(source) in SYNTHETIC
