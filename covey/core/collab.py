"""多参与者协作的契约 (docs/architecture.md 多参与者协作, COVEY_COLLAB, off by default): who owns the delivery of what the
owner said (``route``), the states of a collaboration and of each ask with their transitions, the first version's limits and
the ids that make a submission idempotent. Pure data and functions: no record is read or written here (the store, the
dispatcher and the cards are later packages'), so the contract is tested alone. Standard library only.

- A participant is a topic id with its role (``Participant``): the butler (the root of its family, core/butler.py), a
  project's lead (core/projects.py) or a topic (an ordinary one or a project's). The caller reads the role from the records;
  there is no registry, and the identity outlives a new model, a compaction or a new native session.
- ``route(entry)``: one owner of the delivery for every way of saying something (the room with no @, one @ or several, a
  topic, a quote, a quote of a collaboration), its helpers, and whether the butler routes once. As before where it was
  defined before: no @ is the butler's, one @ and a quote are direct.
- A collaboration's state goes by COLLAB_TRANSITIONS, each ask's by ASK_TRANSITIONS (rule 4: no other change; an ended
  state never goes back; a late answer is kept and wakes nobody).
- 咨询通道: each (collaboration, helper) runs its asks in a topic of its own (``channel_id``), a temporary fork of the
  helper's session, never in the helper's own topic: the helper's session pointer, history, 回退 marker, records and
  latest delivery stay as they were. The ids of the channels and of 协作's turns are the Bridge's (``CHANNEL_PREFIX``,
  ``MESSAGE_PREFIX``): no Feishu message and no hex topic id looks like one (``is_channel``, ``own_message``).
"""
from __future__ import annotations

import dataclasses
import hashlib

# Participants' roles.
BUTLER, LEAD, TOPIC = "butler", "lead", "topic"
ROLES = frozenset({BUTLER, LEAD, TOPIC})

# Where the owner said it: the room (the web page's 大厅, the private chat's top level in Feishu) or a topic (its page,
# its thread).
ROOM, IN_TOPIC = "room", "topic"

# Where ``route`` sends it: the butler as before (DESK), one participant as before (DIRECT), a collaboration (COLLAB),
# the butler routes it once to the one who owns it (ROUTER), or not at all (REFUSED, with a reason the owner reads).
DESK, DIRECT, COLLAB, ROUTER, REFUSED = "desk", "direct", "collab", "router", "refused"

# The first version's limits (product defaults until the pilot says otherwise).
HELPERS_DEFAULT = 2  # Helpers of a collaboration the owner did not name one by one.
HELPERS_NAMED = 3  # At most, when the owner named them.
ASKS_PER_HELPER = 2  # One ask and one clarification each; a helper asks nobody else.
QUEUE_SECONDS = 600  # An ask waits this long for its receiver's turn to end, then expires.
WINDOW_SECONDS = 1800  # A whole collaboration's window: its asks still open then expire.
TOO_MANY = "一次最多请 {n} 位协助，这次点了 {m} 位：先少点几位"

# A collaboration: open while its asks are out, done once its integrator's turn delivered, cancelled when the owner
# stopped it.
OPEN, DONE, CANCELLED = "open", "done", "cancelled"
COLLAB_TRANSITIONS = {OPEN: {DONE, CANCELLED}, DONE: set(), CANCELLED: set()}

# One ask: 待校验 → 排队 → 已接手 → 已答复 → 已被汇总方读取; or 未接手, 停住了 (it failed), 到期, 已取消.
CHECKING, QUEUED, TAKEN, REPLIED, READ = "checking", "queued", "taken", "replied", "read"
NOT_TAKEN, FAILED, EXPIRED = "not_taken", "failed", "expired"
ASK_TRANSITIONS = {
    CHECKING: {QUEUED, NOT_TAKEN, FAILED, CANCELLED},
    QUEUED: {TAKEN, NOT_TAKEN, FAILED, EXPIRED, CANCELLED},
    TAKEN: {REPLIED, FAILED, EXPIRED, CANCELLED},
    REPLIED: {READ},
    **{state: set() for state in (READ, NOT_TAKEN, FAILED, EXPIRED, CANCELLED)},
}
ASK_ENDED = frozenset(state for state, following in ASK_TRANSITIONS.items() if not following)
ASK_WORDS = {CHECKING: "待校验", QUEUED: "排队", TAKEN: "已接手", REPLIED: "已答复", READ: "已被汇总方读取",
             NOT_TAKEN: "未接手", FAILED: "停住了", EXPIRED: "到期", CANCELLED: "已取消"}

# The lead's receipts of a collaboration in a project (core/projects.py inbox): an answer goes into the inbox and wakes
# nobody; a gap the integrator cannot close itself (a missing item, a conflict, a dependency) wakes the lead.
RECEIPT_ANSWERED, RECEIPT_GAP = "collab_answered", "collab_gap"
RECEIPT_WAKES = {RECEIPT_ANSWERED: False, RECEIPT_GAP: True}


@dataclasses.dataclass(frozen=True)
class Participant:
    key: str  # Its topic id.
    role: str  # BUTLER, LEAD or TOPIC.
    project: str = ""  # Its project's id ('' none): a lead's own, a project topic's.


@dataclasses.dataclass(frozen=True)
class Quote:
    of: Participant  # Whose words the owner quoted.
    collab: str = ""  # The collaboration they belong to ('' none).
    integrator: Participant | None = None  # That collaboration's integrator (the caller reads it off the record).


@dataclasses.dataclass(frozen=True)
class Entry:
    """What the owner said, as the Bridge took it in. ``mentions`` are the participants picked (the page's @ list, a
    card's choice), never parsed out of the text."""

    where: str = ROOM
    here: Participant | None = None  # IN_TOPIC: the topic he spoke in.
    mentions: tuple = ()
    quote: Quote | None = None
    integrator: Participant | None = None  # 由谁汇总, when he chose.


@dataclasses.dataclass(frozen=True)
class Route:
    to: str  # DESK, DIRECT, COLLAB, ROUTER or REFUSED.
    owner: str = ""  # The participant that owns the delivery ('' for DESK, ROUTER, REFUSED).
    helpers: tuple = ()  # The others asked (COLLAB; ROUTER: the ones picked).
    collab: str = ""  # The collaboration the words belong to (a quote of one).
    shown_default: bool = False  # The owner is the first ordinary topic picked: the input box shows it, he may change it.
    reason: str = ""  # REFUSED: why.


def _unique(participants) -> tuple:
    seen, kept = set(), []
    for participant in participants:
        if participant is not None and participant.key not in seen:
            seen.add(participant.key)
            kept.append(participant)
    return tuple(kept)


def _collab(owner, others, *, collab="", shown_default=False) -> Route:
    """COLLAB owned by ``owner`` with ``others`` as its helpers; DIRECT to ``owner`` when nobody else is in it (and the
    words belong to no collaboration already)."""
    helpers = tuple(p.key for p in _unique(others) if p.key != owner.key)
    if len(helpers) > HELPERS_NAMED:
        return Route(REFUSED, reason=TOO_MANY.format(n=HELPERS_NAMED, m=len(helpers)))
    if not helpers and not collab:
        return Route(DESK) if owner.role == BUTLER else Route(DIRECT, owner.key)
    return Route(COLLAB, owner.key, helpers, collab, shown_default)


def route(entry: Entry) -> Route:
    """Who owns the delivery of ``entry`` (docs/architecture.md 多参与者协作 · 示例消息流), first rule that applies:

    1. A quote of a collaboration's words: that collaboration, its integrator owns it; whoever is picked is asked again.
    2. 由谁汇总 chosen: that participant, the others its helpers.
    3. In a topic: the topic owns it; picked others are its helpers (none: the topic alone, as before).
    4. In the room, nobody picked: a quote goes to the one quoted (the butler's words: the butler), else the butler.
    5. One picked: that one, as before (the butler: the butler).
    6. Several picked: a quoted topic owns it; else the first ordinary topic picked (shown, changeable); else the butler if
       picked; else the butler routes it once (leads only).
    More helpers than HELPERS_NAMED: REFUSED, nothing is sent."""
    mentions, quote = _unique(entry.mentions), entry.quote
    if quote is not None and quote.collab:
        if quote.integrator is None:
            raise ValueError("a quote of a collaboration needs its integrator")
        return _collab(quote.integrator, mentions, collab=quote.collab)
    if entry.integrator is not None:
        return _collab(entry.integrator, (entry.here, *mentions))
    if entry.where == IN_TOPIC:
        if entry.here is None:
            raise ValueError("a message in a topic needs the topic")
        return _collab(entry.here, mentions)
    if not mentions:
        if quote is None or quote.of.role == BUTLER:
            return Route(DESK)
        return Route(DIRECT, quote.of.key)
    if len(mentions) == 1:
        return Route(DESK) if mentions[0].role == BUTLER else Route(DIRECT, mentions[0].key)
    if quote is not None and quote.of.role == TOPIC:
        return _collab(quote.of, mentions)
    topics = [p for p in mentions if p.role == TOPIC]
    if topics:
        return _collab(topics[0], mentions, shown_default=True)
    butlers = [p for p in mentions if p.role == BUTLER]
    if butlers:
        return _collab(butlers[0], mentions)
    if len(mentions) > HELPERS_NAMED:
        return Route(REFUSED, reason=TOO_MANY.format(n=HELPERS_NAMED, m=len(mentions)))
    return Route(ROUTER, helpers=tuple(p.key for p in mentions))


def moved(table, current, new) -> str:
    """``new`` when ``table`` lets ``current`` go to it; ValueError otherwise (an ended state never goes back)."""
    if new not in table.get(current, ()):
        raise ValueError(f"{current!r} → {new!r} is not a transition")
    return new


def _digest(*parts) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def collab_id(root_message_id, integrator, helpers) -> str:
    """The id of the collaboration the owner's message ``root_message_id`` starts with these participants: the same
    message, integrator and helpers (in any order) always give the same id, so a second submission finds the first."""
    if not root_message_id or not integrator:
        raise ValueError("a collaboration needs the owner's message and its integrator")
    return _digest("collab", root_message_id, integrator, *sorted(set(helpers)))[:24]


def ask_id(collab, to, number) -> str:
    """The id of ask ``number`` (1 the ask, 2 its clarification) of collaboration ``collab`` to participant ``to``."""
    if not 1 <= int(number) <= ASKS_PER_HELPER:
        raise ValueError(f"at most {ASKS_PER_HELPER} asks to one helper")
    return f"{collab}-{_digest('ask', to)[:8]}-{int(number)}"


# 咨询通道 (app/collab_channel.py): the topic an ask runs in. Its id is no hex topic id (core/projects.py TOPIC_RE,
# app/topics.py target pass it by) and core/butler.py traits reads it as CHANNEL: no T number, not listed, no thread.
CHANNEL_PREFIX = "collab-"
# The message ids of 协作's turns (an ask's, an integrator's continuation): the Bridge's, no Feishu message has one, so
# no reply quotes it (core/topics.py context_for_topic) and no 你问 block shows its words (app/answer_cards.py asked).
MESSAGE_PREFIX = "collab-"
RETRIES = 1  # An ask's turn that ended for a passing cause (a limit, a server fault) is queued again this many times.
# What an answer is grounded on: a fork of the helper's own session, or its topic's record alone (no session to fork).
FORKED, RECORD = "fork", "record"
EVIDENCE_WORDS = {FORKED: "原会话分叉", RECORD: "话题记录（无原会话）"}


def channel_id(collab, helper) -> str:
    """The channel topic of helper ``helper`` in collaboration ``collab``: its ask and its clarification run there."""
    if not collab or not helper:
        raise ValueError("a channel needs its collaboration and its helper")
    return f"{CHANNEL_PREFIX}{collab}-{_digest('channel', helper)[:8]}"


def is_channel(topic_id) -> bool:
    """Whether ``topic_id`` is a 咨询通道's (``channel_id``)."""
    return isinstance(topic_id, str) and topic_id.startswith(CHANNEL_PREFIX)


def own_message(message_id) -> bool:
    """Whether ``message_id`` is one the Bridge made up for a turn of 协作 (``ask_message``, ``reply_message``)."""
    return isinstance(message_id, str) and message_id.startswith(MESSAGE_PREFIX)


def ask_message(ask, attempt=1) -> str:
    """The message id of try ``attempt`` of ask ``ask``'s turn (a try that ended for a passing cause is queued again)."""
    return f"{MESSAGE_PREFIX}{ask}" + (f"-r{int(attempt)}" if int(attempt) > 1 else "")


def reply_message(collab, number, attempt=1) -> str:
    """The message id of try ``attempt`` of the integrator's ``number``-th continuation turn of ``collab`` (one that did not
    end well is queued once more, app/collab_flow.py settle_reply)."""
    return f"{MESSAGE_PREFIX}{collab}-reply-{int(number)}" + (f"-r{int(attempt)}" if int(attempt) > 1 else "")
