"""Where a turn's words go in the private chat (docs/butler.md 放置表): one table, the layout by the role.

The layout is the deployment's and the owner's (``layout``): "topics" with the butler off (core/butler.py
``enabled``), else "area" (the 话题区). The role is the topic's: the butler ("butler", BUTLER_OFF too) or a topic
("topic") by core/butler.py Traits, and with COVEY_PROJECTS on a project's lead ("lead", LEAD_OFF too: Traits) or
one of its members ("project_member": topic.json's ``project.role``, core/projects.py, read by a stat and kept until
the file changes, so a topic upgraded into a project is one from its next ``plan`` on). The front-desk layout and role (desk_topics) were retired
and their topics frozen (core/migrate.py desk-retire).

Each cell names a place: TOP a new message at the chat's top level quoting no card (a "你问" block on it when the row
says ``ask_block``), THREAD the turn's context's reply target (its thread), NONE nowhere. The call sites read the cells;
what only the turn tells (whether it opened topics, how long it took, whether it fits a card) stays theirs.

Standard library and core only: cards/streaming.py may read it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from covey.core import butler, projects, web_sync
from covey.core.jobs import job_field
from covey.core.butler import Traits

TOP, THREAD, CARD, NONE = "top", "thread", "card", ""
AREA, TOPICS = "area", "topics"  # The layouts.
BUTLER_ROLE, TOPIC_ROLE, LEAD_ROLE, MEMBER_ROLE = "butler", "topic", "lead", "project_member"
ROUTES = "routes"  # When an answer is its task card's body (``answer_on_card``).
# Traits.name → role: the butler's topic is the butler whether its session runs (BUTLER) or not (BUTLER_OFF).
ROLES = {"butler": BUTLER_ROLE, "butler_off": BUTLER_ROLE, "topic": TOPIC_ROLE, "lead": LEAD_ROLE, "lead_off": LEAD_ROLE}
# MEMBER_ROLE is no Traits row's: a member is a topic (TOPIC) by its Traits, a project's by its topic.json (``plan``).


@dataclass(frozen=True)
class Row:
    """Where each of a turn's words goes, for one layout and role."""

    # A reply to the owner's message at the top level (True) or in its thread (False); None: Traits.top_level
    # (app/butler.py Butler.configure).
    reply_top_level: bool | None
    answer: str  # The verified reply sent on its own, not on the task card (app/turn_delivery.py TurnDelivery.send).
    # When the reply is the task card's body (app/butler_reply.py answers_in_place): ROUTES only when the turn opened
    # or handed on to topics; "" never (Traits.answers_in_place False).
    answer_on_card: str
    conclusion: str  # A finished turn's relay (app/butler_relay.py after_turn); NONE none.
    stop: str  # A stopped or failed turn's relay; NONE none (the butler's card lists its supplements).
    question_copy: str  # A waiting turn's question cards copied to the top level (app/question_display.py mirror).
    ask_block: bool  # A new top-level message carries the "你问" link to the message it answers.
    # Its status card is pinned on the board when it has a thread (app/status_cards.py); else its turn's own card:
    # the question card, the task card, a 话题区's answer card in its thread once done (app/pins.py ``aimed``).
    status_card: bool
    # The answer card sent on its own in the turn's thread leads with the turn's status, T number and title, and its
    # task card then keeps no 结论 lead (app/turn_delivery.py TurnDelivery ``answer_head``), in the owner's
    # private chat only (TurnDelivery ``headed``: a group's topic has this row too, its cards as before): its answer card
    # is the turn's result and the board's Pin.
    answer_head: bool = False
    # The butler's turn opens no thread of its own: its task card goes out only once
    # it has run butler.CARD_AFTER seconds (app/cards.py Cards.reveal), the owner's message gets a reaction
    # instead (app/butler_reactions.py: OnIt, then DONE, Get or CrossMark), a turn that only handed work on to topics
    # sends no answer of its own, a failed or refused one a short top-level notice, its question cards go to the top
    # level (an answer's continuation, ``quiet``, too: no thread under the question card, no reaction), and
    # its "你问" links open the owner's message in the chat, not a thread (app/answer_cards.py ask_link chat_place).
    quiet: bool = False
    # Its task card (the process card): THREAD in the turn's context's reply target (a quiet turn's once it has run
    # butler.CARD_AFTER seconds, app/cards.py Cards.reveal); NONE never, whatever started the turn, and every turn of
    # such a row is quiet (``quiet``): what reaches the owner is the answer, a report or a turn that did not end
    # well, each a message of its own (``answer``, app/quiet_delivery.py).
    task_card: str = THREAD


_TOPICS = Row(reply_top_level=False, answer=THREAD, answer_on_card=NONE, conclusion=NONE, stop=NONE,
              question_copy=NONE, ask_block=False, status_card=True)
TABLE = {
    (AREA, BUTLER_ROLE): Row(reply_top_level=False, answer=TOP, answer_on_card=ROUTES, conclusion=NONE,
                           stop=NONE, question_copy=TOP, ask_block=True, status_card=False, quiet=True),
    (AREA, TOPIC_ROLE): Row(reply_top_level=None, answer=THREAD, answer_on_card=NONE, conclusion=TOP,
                            stop=TOP, question_copy=TOP, ask_block=True, status_card=False, answer_head=True),
    # A project's lead (docs/architecture.md 专项): hidden, no thread of its own, quiet like the butler; its words
    # go to the top level only in a turn the owner started (a wake-up turn's are dropped unless it reported, phase 2's
    # app/project_lead.py), nothing relayed, no question card copy (what the owner must answer comes by the question
    # card of the thread's turn), and no task card in any turn, the butler's project_send and the owner's own
    # message too (the owner, 2026-11-22: 负责人一律不往私聊顶层吐过程卡): its 专项卡 says what it does.
    (AREA, LEAD_ROLE): Row(reply_top_level=False, answer=TOP, answer_on_card=NONE, conclusion=NONE,
                           stop=NONE, question_copy=NONE, ask_block=True, status_card=False, quiet=True,
                           task_card=NONE),
    # A project's thread: a topic whose answer stays in its thread; its conclusion and stop do not go back to the top
    # level (they go to the lead's inbox, phase 2), and only a question that reaches the owner is copied there.
    (AREA, MEMBER_ROLE): Row(reply_top_level=None, answer=THREAD, answer_on_card=NONE, conclusion=NONE,
                             stop=NONE, question_copy=TOP, ask_block=True, status_card=False, answer_head=True),
    # The butler off: every reply in its thread, nothing relayed (BUTLER_OFF and TOPIC are the only rows then; a
    # project needs the butler, so its rows here are for what is left of one when it is turned off).
    (TOPICS, BUTLER_ROLE): _TOPICS,
    (TOPICS, TOPIC_ROLE): _TOPICS,
    (TOPICS, LEAD_ROLE): _TOPICS,
    (TOPICS, MEMBER_ROLE): _TOPICS,
}


@dataclass(frozen=True)
class Placement:
    traits: Traits
    layout: str
    role: str
    row: Row

    def top_level(self) -> bool:
        """Whether a reply to the owner's message stays at the top level (``Row.reply_top_level``)."""
        return self.traits.top_level if self.row.reply_top_level is None else self.row.reply_top_level


def layout() -> str:
    """TOPICS with the butler off (COVEY_BUTLER=0, core/butler.py enabled), else AREA."""
    return AREA if butler.enabled() else TOPICS


def _topic_of(of) -> str:
    """The topic id ``of`` names ('' for a Traits row, which names none)."""
    if isinstance(of, Mapping):
        topic = of.get("topic_id")
    else:
        topic = None if isinstance(of, Traits) else getattr(of, "topic_id", "")
    return topic if isinstance(topic, str) else ""


def plan(state_dir, of) -> Placement:
    """The placement of ``of`` (as core/butler.py traits_of takes it: a job, a record, topic.json or a Traits
    row) in the layout now (``layout``). The role is its Traits row's, and for a topic with COVEY_PROJECTS on, MEMBER_ROLE
    when its topic.json says it is a project's thread (``state_dir`` is read for that alone; a Traits row names no
    topic, so a caller that wants the thread's role passes the job, the record or {"topic_id": ..., "chat_id": ...})."""
    traits = butler.traits_of(of)
    where = layout()
    role = ROLES.get(traits.name, TOPIC_ROLE)
    if role == TOPIC_ROLE and state_dir and projects.enabled() and projects.is_member_topic(state_dir, _topic_of(of)):
        role = MEMBER_ROLE
    return Placement(traits, where, role, TABLE[(where, role)])


# An answer's continuation: the turn the owner's answer to a question card queues (app/questions.py
# _queue_continuation), its job's message id this and the question's token.
CONTINUATION = "answer-"


def owner_message(job) -> str:
    """The owner's message ``job`` (a job, or its record) answers: its ``reaction_message`` (a quiet turn's retry or
    resume, app/task_controls.py requeue), else its own message id. Never raises."""
    try:
        return str(job_field(job, "reaction_message") or job_field(job, "message_id") or "")
    except Exception:  # noqa: BLE001 - none
        return ""


def quiet(state_dir, job) -> bool:
    """Whether ``job`` (a job, or its record) is a quiet turn of the 话题区 layout (``Row.quiet``): every turn of a
    row whose task card never goes out (``Row.task_card`` NONE: a project's lead, whatever started it, a wake-up, the
    owner's message, his words on the web page, the butler's project_send "main-…", an answer's continuation or a
    retry of one), and the butler's on one of the owner's own messages (a Feishu message id, ``owner_message``), his
    words on the web page or an answer's continuation (``CONTINUATION``: answering the question card at the top level
    opens no thread under it either); a turn the butler's tools started in its topic is not. Never raises: False when
    it cannot be told."""
    try:
        row = plan(state_dir, job).row
        return row.quiet and (row.task_card == NONE or owner_message(job).startswith(
            ("om_", CONTINUATION, projects.WAKE_PREFIX, web_sync.PREFIX)))
    except Exception:  # noqa: BLE001 - as before: the card at once
        return False


def cardless(state_dir, job) -> bool:
    """Whether ``job`` (a job, or its record) never shows a task card (``Row.task_card`` NONE: a project's lead in the
    话题区 layout), whatever its record says it had (a card an older release sent). Never raises: False when it
    cannot be told."""
    try:
        return plan(state_dir, job).row.task_card == NONE
    except Exception:  # noqa: BLE001 - as before: its card
        return False


def marked(state_dir, job) -> bool:
    """Whether ``job`` is ``quiet`` and one of the owner's own messages, which takes the turn's reactions
    (app/butler_reactions.py): an answer's continuation has no message of the owner's, its question card says how it
    goes (app/question_display.py update_continuation). Never raises."""
    return owner_message(job).startswith("om_") and quiet(state_dir, job)
