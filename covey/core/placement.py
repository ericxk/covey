"""Where a turn's words go in the private chat (docs/main-thread.md 放置表): one table, the layout by the role.

The layout is the deployment's and the owner's (``layout``): "topics" with the main thread off (core/main_thread.py
``enabled``), else "area" (the 话题区). The role is the topic's (core/main_thread.py Traits): the main thread ("main",
MAIN_OFF too) or a topic ("topic"). The front-desk layout and role (desk_topics, 11-07) were retired on 2026-11-09
and their topics frozen on 2026-11-10 (core/migrate.py desk-retire).

Each cell names a place: TOP a new message at the chat's top level quoting no card (a "你问" block on it when the row
says ``ask_block``), THREAD the turn's context's reply target (its thread), NONE nowhere. The call sites read the cells;
what only the turn tells (whether it opened topics, how long it took, whether it fits a card) stays theirs.

Standard library and core only: cards/streaming.py may read it.
"""
from __future__ import annotations

from dataclasses import dataclass

from covey.core import main_thread
from covey.core.jobs import job_field
from covey.core.main_thread import Traits

TOP, THREAD, CARD, NONE = "top", "thread", "card", ""
AREA, TOPICS = "area", "topics"  # The layouts.
MAIN_ROLE, TOPIC_ROLE = "main", "topic"
ROUTES = "routes"  # When an answer is its task card's body (``answer_on_card``).
# Traits.name → role: the main thread's topic is the main thread whether its session runs (MAIN) or not (MAIN_OFF).
ROLES = {"main": MAIN_ROLE, "main_off": MAIN_ROLE, "topic": TOPIC_ROLE}


@dataclass(frozen=True)
class Row:
    """Where each of a turn's words goes, for one layout and role."""

    # A reply to the owner's message at the top level (True) or in its thread (False); None: Traits.top_level
    # (app/main_thread.py MainThread.configure).
    reply_top_level: bool | None
    answer: str  # The verified reply sent on its own, not on the task card (app/worker.py TurnDelivery.send).
    # When the reply is the task card's body (app/main_reply.py answers_in_place): ROUTES only when the turn opened
    # or handed on to topics; "" never (Traits.answers_in_place False).
    answer_on_card: str
    conclusion: str  # A finished turn's relay (app/main_relay.py after_turn); NONE none.
    stop: str  # A stopped or failed turn's relay; NONE none (the main thread's card lists its supplements).
    question_copy: str  # A waiting turn's question cards copied to the top level (app/question_display.py mirror).
    ask_block: bool  # A new top-level message carries the "你问" link to the message it answers.
    # Its status card is pinned on the board when it has a thread (app/status_cards.py); else its turn's own card:
    # the question card, the task card, a 话题区's answer card in its thread once done (app/pins.py ``aimed``).
    status_card: bool
    # The answer card sent on its own in the turn's thread leads with the turn's status, T number and title, and its
    # task card then keeps no 结论 lead (app/worker.py TurnDelivery ``answer_head``, D3 2026-11-08), in the owner's
    # private chat only (TurnDelivery ``headed``: a group's topic has this row too, its cards as before): its answer card
    # is the turn's result and the board's Pin.
    answer_head: bool = False
    # The main thread's turn opens no thread of its own (D1, 2026-11-08: 提问不开串): its task card goes out only once
    # it has run main_thread.CARD_AFTER seconds (app/cards.py Cards.reveal), the owner's message gets a reaction
    # instead (app/main_reactions.py: OnIt, then DONE, Get or CrossMark), a turn that only handed work on to topics
    # sends no answer of its own, a failed or refused one a short top-level notice, its question cards go to the top
    # level (an answer's continuation, ``quiet``, too: no thread under the question card, no reaction), and
    # its "你问" links open the owner's message in the chat, not a thread (app/answer_cards.py ask_link chat_place).
    quiet: bool = False


_TOPICS = Row(reply_top_level=False, answer=THREAD, answer_on_card=NONE, conclusion=NONE, stop=NONE,
              question_copy=NONE, ask_block=False, status_card=True)
TABLE = {
    (AREA, MAIN_ROLE): Row(reply_top_level=False, answer=TOP, answer_on_card=ROUTES, conclusion=NONE,
                           stop=NONE, question_copy=TOP, ask_block=True, status_card=False, quiet=True),
    (AREA, TOPIC_ROLE): Row(reply_top_level=None, answer=THREAD, answer_on_card=NONE, conclusion=TOP,
                            stop=TOP, question_copy=TOP, ask_block=True, status_card=False, answer_head=True),
    # The main thread off: every reply in its thread, nothing relayed (MAIN_OFF and TOPIC are the only rows then).
    (TOPICS, MAIN_ROLE): _TOPICS,
    (TOPICS, TOPIC_ROLE): _TOPICS,
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
    """TOPICS with the main thread off, else AREA: the mode as core/main_thread.py reads it (set up per process)."""
    return AREA if main_thread.enabled() else TOPICS


def plan(state_dir, of) -> Placement:
    """The placement of ``of`` (as core/main_thread.py traits_of takes it: a job, a record, topic.json or a Traits
    row) in the layout now (``layout``; ``state_dir`` is not read, kept for the callers)."""
    traits = main_thread.traits_of(of)
    where = layout()
    role = ROLES.get(traits.name, TOPIC_ROLE)
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
    """Whether ``job`` (a job, or its record) is a main-thread turn of the 话题区 layout (``Row.quiet``), and one
    of the owner's own messages (a Feishu message id, ``owner_message``) or an answer's continuation (``CONTINUATION``: answering the
    question card at the top level opens no thread under it either); a main-thread tool's turn is not. Never raises:
    False when it cannot be told."""
    try:
        return owner_message(job).startswith(("om_", CONTINUATION)) and plan(state_dir, job).row.quiet
    except Exception:  # noqa: BLE001 - as before: the card at once
        return False


def marked(state_dir, job) -> bool:
    """Whether ``job`` is ``quiet`` and one of the owner's own messages, which takes the turn's reactions
    (app/main_reactions.py): an answer's continuation has no message of the owner's, its question card says how it
    goes (app/question_display.py update_continuation). Never raises."""
    return owner_message(job).startswith("om_") and quiet(state_dir, job)
