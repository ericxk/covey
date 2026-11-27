"""A topic turn's outcome back at the private chat's top level (docs/butler.md 结论回到顶层), only with the butler
 on (core/butler.py enabled).

The butler hands work to topics (app/butler_topics.py): topic_start and topic_fork open one with a first turn,
topic_send starts a topic's next turn or gives the owner's words to the turn that runs as a supplement. When such a
turn ends, its task runner (app/turn_delivery.py TurnDelivery.deliver, after the task card's last words) relays it, where
core/placement.py's row says (``after_turn``: question_copy, conclusion, stop):

- done: the saved reply whole (rule 5: only the verified reply that was delivered), then the turn's longer 进展说明
  (core/trace.py stage_notes: what a long turn reported on its way, already on its task card), the owner reads the answer
  where he asked, as the topic's answer card has it (cards/answer_card.py: 产物 and the 输入/输出 box, 输出 ending in
  a link to the topic's card), and 换配置 beside its 独立复核 (``with_switch``), whose card opens at the top level. A reply
  too large for one card (CARD_TEXT_BYTES) goes as Markdown messages in order (feishu/delivery.py splits them), its
  产物 and box on a small card after them;
- waiting for an answer: the turn's questions as answerable cards (app/question_display.py mirror) with what the
  turn wrote above them, its longer 进展说明 too; only when no copy could be shown "**T10 需要你回答**", the question, and where to answer;
- stopped or failed with no automatic retry planned: 停住了 with why and what next in core/failure.py's words, and the task card's
  检查并继续 and 看原因 under them, redrawn in place once clicked (app/relay_stops.py).

Every turn of a 话题区 relays, the ones his answers or his messages in
its thread started too (``area_target``: the latest request that opened or handed on to it), and every turn of one the
owner opened on the web page, whose topic card is at the top level too (app/relay_targets.py ``web_root``); any other
topic the butler never touched does not. A project's member topic relays only a turn the owner waits for
(app/relay_targets.py ``awaited_target``: its lead handed it on in a turn the owner was in, or the owner asked for it
quoting one of its cards from outside its thread), its conclusion or its stop, never its question;
the lead's receipt of it says so (app/project_loop.py after_runner), so the lead does not tell it again.

Where it goes (app/relay_targets.py): the butler's card is in the owner's message's thread,
so a message of its own at the top level with a "你问" link block, quoting no card. Which turns came from the butler
 (``relay_target``): a turn whose message id is "main-" +
the request's token, the one main-thread/opened/<token>.json records; one topic_send gave a supplement to (that
record's answer names its job_id, steered); an automatic retry of either (its record's retry_of, the original's
auto_retry_state "queued").

Each job is relayed once (rule 6): main-thread/relays/<job key>.json says ``submitting`` before the send, under a
file lock that no Feishu call is made under (rules 1 and 2), then ``sent`` with the message id, or ``uncertain``,
and the runtime log has one line ``main relay state=…``. A job with a record is never sent again,
uncertain included. The send's key derives from the job's key alone.

A done turn relayed (``relayed``: sent, or written on the card) says so in the butler's snapshot
(app/butler.py snapshot_groups). The message sent is the topic's, and so is a quote of it (remember_topic_message,
route_message: 引用直达), where a waiting turn's question takes the words as its answer. The
butler card a relay quotes stays the butler's: a reply to a message of a threaded topic would open a thread
under it; a quote of it is routed to the topic it alone handed on to.

The parts: where a relay goes (app/relay_targets.py), its card (cards/relay_card.py), sending it once
(app/relay_send.py). This module keeps
the hooks the worker, the Bridge and app/native_questions.py call; callers and tests import the parts' names from
the parts.
"""
from __future__ import annotations

from pathlib import Path

from covey.app import answer_cards, relay_stops, review
from covey.app.artifacts import answer_files
from covey.app.relay_send import (QUESTION_RELAYS, SKIPPED, merge_seconds, messenger_send, record_only, record_unsent,
                                           relay_path, send_merged, send_once as _send_once, take_left as take_left_pending)
from covey.app.relay_targets import areas_of, awaited_target, relay_target
from covey.cards.answer_card import alerts
from covey.cards.relay_card import DONE, STOPPED, WAITING
from covey.cards.topic_actions import beside, relay_switch
from covey.core import butler, placement, turn_source
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import find_job_record, job_field, job_key
from covey.core.state import read_object
from covey.core.store import directory_store
from covey.core.topics import route_message, topic_title
from covey.core.trace import stage_notes, with_stage_notes

STOPPED_STATUSES = frozenset({"failed", "cancelled"})
# What a project member's turn the owner waits for relays (``awaited_target``): its question stays the question flow's
# (app/triage.py may hold it for the lead), the rest is relayed as a topic's of the butler is.
AWAITED = (DONE, STOPPED)


def relayed(state_dir, record) -> bool:
    """Whether the job ``record`` (a job record) ended done with its conclusion sent to the top level."""
    key = record.get("key") or job_key(str(record.get("message_id") or ""), record.get("job_id") or "")
    saved = read_object(relay_path(state_dir, key))
    return saved.get("state") in ("sent", INLINE) and saved.get("outcome") == DONE


INLINE = "inline"  # A relay's state: its conclusion written on the butler's card (an older release).


def inline_at(state_dir, record) -> float | None:
    """When the done conclusion of the job ``record`` was written on a top-level card (INLINE: on the butler's
    card, by an older release), epoch seconds; None for one sent as a message of its own (the topic's
    alias, so its own read says it was seen: core/board_read.py seen_at) or not relayed. Such records stay on disk,
    so the board still takes the owner's top-level read for them."""
    key = record.get("key") or job_key(str(record.get("message_id") or ""), record.get("job_id") or "")
    saved = read_object(relay_path(state_dir, key))
    at = saved.get("sent_at")
    if saved.get("state") != INLINE or saved.get("outcome") != DONE:
        return None
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def outcome_of(turn, delivered, execution_status, retry):
    """What a finished turn relays (DONE, WAITING, STOPPED), or None: a turn whose result did not reach its topic,
    or that an automatic retry (app/auto_retry.py) goes on with."""
    if not delivered:
        return None
    if execution_status == "done" and turn.status == "done" and turn.verified_delivery is not None:
        return DONE
    if execution_status == "waiting_user" and turn.status == "waiting_user":
        return WAITING
    if execution_status in STOPPED_STATUSES and retry is None:
        return STOPPED
    return None


def _question(state_dir, job):
    """The question a waiting turn asks: its pending question record's, else its checkpoint's."""
    rows = directory_store(Path(state_dir) / "questions").query(job_id=job_field(job, "job_id"),
                                                                topic_id=job_field(job, "topic_id"))
    for _, row in sorted(rows, key=lambda pair: pair[1].get("created_at") or 0, reverse=True):
        asked = [q for q in row.get("questions") or [] if isinstance(q, dict)]
        words = [str(q.get("question") or q.get("header") or "").strip() for q in asked]
        if any(words):
            return "\n".join(word for word in words if word)
    point = read_checkpoint(Path(job_field(job, "checkpoint_path"))) if job_field(job, "checkpoint_path") else {}
    asked = [q for q in point.get("questions") or [] if isinstance(q, dict)]
    return str(point.get("question") or "").strip() or "\n".join(
        str(q.get("question") or "").strip() for q in asked if q.get("question"))


def _stages(job) -> list:
    """The turn's longer 进展说明 (core/trace.py stage_notes), from the task's own trace; [] without one."""
    trace = getattr(job, "tool_trace", None)
    return stage_notes(dict(trace.steps)) if trace is not None else []


def _facts(state_dir, job, turn, outcome):
    if outcome == DONE:
        # The saved reply, then what the turn reported on its way (a long turn's stage reports live in its 进展说明).
        return {"text": with_stage_notes(turn.verified_delivery.text, _stages(job))}  # after_turn adds the answer card's parts.
    if outcome == WAITING:
        return {"question": _question(state_dir, job)}
    # Why and what next as the task card says them, never the status line (技术详情 there), and its buttons.
    return relay_stops.facts(state_dir, job)


def relay(state_dir, job, outcome, *, send, profile=None, log=None, merge=True, **facts):
    """Relay ``job``'s ``outcome`` once: ``send(chat, text, key, reply_to, card, on_sent)`` sends it (Messenger's
    send_message; ``key`` is the job's key, the same at every call), ``log`` gets a line of what became of it. The
    relay record's state: None when not relayed, else "sent", "uncertain", or for a job that had a record already,
    what it says (nothing sent); "skipped" for a stop the owner pressed in the 话题区 (D4). ``merge`` False: a 做完了
    goes at once, never waiting for others of its chat (``settle_missing``: no task runner keeps its window)."""
    if not butler.enabled():
        return None
    target = relay_target(state_dir, job)
    if target is None and outcome in AWAITED:
        target = awaited_target(state_dir, job)
    if target is None:
        return None
    key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
    if outcome == WAITING and any((Path(state_dir) / QUESTION_RELAYS).glob(f"{key}--*.json")):
        return "asked"  # Its question went to the top level as it was asked (``relay_question``).
    if outcome == STOPPED and job_field(job, "cancel_requested_by"):
        # The owner pressed 停止 on its card in the 话题区's thread, looking at it: no notice. The
        # record says so; a failure, a refusal, a timeout or an interruption still relays.
        return record_only(state_dir, relay_path(state_dir, key), key, job, outcome, SKIPPED, log, target,
                           why="owner_stop")
    # 做完了 relays that come close together go as one (app/relay_send.py send_merged); 等你 and 停住了 never wait, and nor
    # does one with what the owner must see (a commit not found, the criteria not met: cards/answer_card.py alerts), whose
    # own card says it and takes a reply to its topic (T647 P1-A2).
    seconds = merge_seconds() if merge and outcome == DONE and not alerts(facts.get("change"), facts.get("notes")) else 0
    if seconds:
        title = topic_title(Path(state_dir), str(target.get("topic_id") or job_field(job, "topic_id")))
        return send_merged(state_dir, job, relay_path(state_dir, key), key, target, send, profile=profile, log=log,
                           seconds=seconds, title=title, **facts)
    return _send_once(state_dir, job, relay_path(state_dir, key), key, target, outcome, send, profile=profile, log=log,
                      **facts)


# The end of a turn the task runner's own relay (``after_turn``) never reached, settled by the Bridge (W1-F) ------------

def ended_outcome(record):
    """What ``record``'s turn relays now that its topic has its result (a redelivery, app/redelivery.py): DONE for a done
    one, WAITING for one waiting for the owner, STOPPED for a failed or cancelled one, else None."""
    status = record.get("status")
    if status == "done":
        return DONE
    if status == "waiting_user":
        return WAITING
    return STOPPED if status in STOPPED_STATUSES else None


def _mirrored(state_dir, record) -> bool:
    """Whether a question of ``record``'s turn is copied to the top level already (``mirror_questions``)."""
    rows = directory_store(Path(state_dir) / "questions").query(job_id=record.get("job_id"), topic_id=record.get("topic_id"))
    return any(row.get("mirror_message_id") for _, row in rows)


def settle_missing(state_dir, record, outcome, *, send, profile, log, text=""):
    """The relay of a turn whose end its task runner never relayed (docs/butler.md 结论回到顶层): the Bridge's, from the job
    ``record`` (no turn object here): one left interrupted with nothing to go on by itself (app/task_controls.py left_stopped: app/detached.py
    finish_detached, the Bridge's start), a result redelivered after its first send failed (app/redelivery.py), one the
    outbox gave up sending (app/outbox.py, paused). ``outcome``: DONE with ``text`` the reply just sent to the topic (none:
    nothing relayed, rule 5), WAITING, STOPPED (``text``, when given, its reason; else the task card's, relay_stops.facts).
    The same gates as ``after_turn``: the butler on, the turn's own delivery, a row of the layout that relays it or the owner
    waiting for it (relay_targets.awaited_target); never merged (``relay`` merge False). Each job relays once (rule 6):
    one with a relay record already gets its state, nothing sent (relay_send.send_once), so the runner's own relay and
    this one never both go; a waiting turn whose question is copied up already gets "mirrored". Never raises: a failure
    is a log line and None."""
    try:
        if not record or outcome not in (DONE, WAITING, STOPPED) or not butler.enabled():
            return None
        if not turn_source.behavior(record).own_delivery:
            return None  # A 协作 ask's turn: its collaboration's.
        row = placement.plan(state_dir, record).row
        if not (row.question_copy or row.conclusion or row.stop):
            return None
        if not (row.conclusion or row.stop) and not (outcome in AWAITED and awaited_target(state_dir, record)):
            return None
        if outcome == DONE and not str(text or "").strip():
            return None  # Only the reply that went out is relayed, never one drawn here.
        if outcome == WAITING and _mirrored(state_dir, record):
            return "mirrored"
        if outcome == DONE:
            facts = {"text": text}
        elif outcome == WAITING:
            facts = {"question": _question(state_dir, record)}
        else:
            facts = relay_stops.facts(state_dir, record)
            if text:
                facts["reason"] = text
        state = relay(state_dir, record, outcome, send=send, profile=profile, log=log, merge=False, **facts)
        if state is not None and log is not None:
            log(f"main relay settled outcome={outcome} job=#{record.get('job_id')} state={state}")
        return state
    except Exception as exc:  # noqa: BLE001 - the turn's end stands; only its relay is lost
        if log is not None:
            log(f"main relay settle of job #{record.get('job_id') if isinstance(record, dict) else '?'} failed: "
                f"{type(exc).__name__}")
        return None


def take_left(state_dir, job, *, send, profile, log):
    """A 做完了 relay of ``job`` left PENDING (relay_send.send_merged) by a task runner that has exited: its window's thread
    went with it, so the relays of its chat still pending go now (relay_send.take_left). The Bridge's, once the runner is
    gone (app/detached.py finish_detached, end_stranded). Never raises: the record's state, None for none."""
    try:
        if not butler.available():
            return None
        key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
        return take_left_pending(state_dir, key, send, profile=profile, log=log)
    except Exception as exc:  # noqa: BLE001 - it goes with the chat's next merged relay
        if log is not None:
            log(f"main relay pending of job #{job_field(job, 'job_id')} not taken: {type(exc).__name__}")
        return None


def question_relay_path(state_dir, key, token) -> Path:
    return Path(state_dir) / QUESTION_RELAYS / f"{key}--{token}.json"


def relay_question(state_dir, job, question, *, send, profile=None, log=None):
    """A question a turn the butler started in a topic with its own thread asks while it runs (its question
    record, app/native_questions.py): "**T48 需要你回答**" and the question at the top level, quoting the owner's
    message, as ``relay`` sends a waiting turn's; once per question (main-thread/relays/questions/<job key>--<its
    token>.json, the same rule 1 steps). Not for any other turn. The record's state, or None when nothing is
    relayed."""
    if not butler.enabled():
        return None
    target = relay_target(state_dir, job)
    token = str((question or {}).get("token") or "")
    if target is None or not token:
        return None
    asked = [q for q in question.get("questions") or [] if isinstance(q, dict)]
    words = "\n".join(w for w in (str(q.get("question") or q.get("header") or "").strip() for q in asked) if w)
    if not words:
        return None
    key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
    return _send_once(state_dir, job, question_relay_path(state_dir, key, token), f"{key}--{token}", target, WAITING,
                      send, profile=profile, log=log, question=words)


def after_runner(gateway, job, ctx) -> None:
    """The Bridge, once ``job``'s run has ended (app/detached.py finish_detached; app/unified.py for a run here). A
    butler turn whose card handed on to one topic alone gives the card to it: quoting the card goes there (引用直达).
    Never raises."""
    from covey.app import project_loop, revisits

    project_loop.after_runner(gateway.runtime.log, ctx.state_dir, job)  # A project's task: its tokens, a thread's receipt.
    revisits.after_runner(gateway.runtime.log, ctx.state_dir, job)  # 到点回来看: the topic's next look (COVEY_REVISIT=1).
    try:
        if not butler.available():  # Not only with it on: a card handed on before the owner chose 纯话题 too.
            return
        topic, chat = job_field(job, "topic_id"), job_field(job, "chat_id")
        if not butler.is_butler_topic(topic, chat) or butler.is_lead(topic, chat):
            return  # A project's lead is of the family, but quoting its card stays the lead's (core/projects.py).
        state = Path(ctx.state_dir)
        key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
        areas = areas_of(state, key)
        card = str((find_job_record(state, key) or {}).get("card_message_id") or "")
        if len(areas) == 1 and card.startswith("om_"):
            # Still the butler's message, so a relay quoting it stays at the top level; quoting it goes to
            # the topic.
            route_message(state, str(job_field(job, "chat_id")), card, next(iter(areas)),
                          holder=str(job_field(job, "topic_id")))
    except Exception as exc:  # noqa: BLE001 - quoting the card stays the butler's
        gateway.runtime.log(f"main thread relay settle after job #{job_field(job, 'job_id')} failed: {type(exc).__name__}")


def question_asked(ctx, job, question, *, messenger, voice, log):
    """app/native_questions.py's hook once a running turn's question is saved, before it is shown: ``relay_question``.
    Never raises: a failure is a log line, the question stands."""
    try:
        if not butler.enabled() or not butler.traits_of(job).relays:
            return None
        return relay_question(ctx.state_dir, job, question, send=messenger_send(messenger, ctx.profile, voice),
                              profile=ctx.profile, log=log)
    except Exception as exc:  # noqa: BLE001 - the question is shown all the same
        log(f"main thread question relay of job #{job_field(job, 'job_id')} failed: {type(exc).__name__}")
        return None


def mirror_questions(delivery, *, quiet=False):
    """A waiting 话题区 turn's pending questions shown at the top level too (app/question_display.py mirror), with
    the reply it delivered above them; whether any is there now. ``quiet``: a copy that could not be shown gets no line
    in its place, the 等你 relay that follows says where to answer (``after_turn``)."""
    job, turn, ctx = delivery.job, delivery.turn, delivery.ctx
    output = getattr(delivery, "display_text", None)  # As the topic's card shows it: without the question.
    if output is None:
        output = turn.verified_delivery.text if getattr(turn, "verified_delivery", None) is not None else ""
    output = with_stage_notes(output, _stages(job))
    rows = directory_store(Path(ctx.state_dir) / "questions").query(job_id=job_field(job, "job_id"),
                                                                    topic_id=job_field(job, "topic_id"), status="pending")
    shown = False
    for _, record in rows:
        if record.get("mirror_message_id") or delivery.questions.display.mirror(ctx, record, output, job=job, quiet=quiet):
            shown = True
    return shown


def with_switch(rows, job) -> list:
    """``rows`` (the buttons under a 做完了 relay's 输入/输出 lines: 独立复核, app/review.py) with 换配置 at the end of their row
    (cards/topic_actions.py relay_switch and beside, T669 E1), for a turn with a Harness of a topic that offers it
    (core/butler.py Traits switch_config: not the butler's own nor a lead's); ``rows`` as they are otherwise. Its click opens
    the 换配置 card at the top level, under the relay (app/harness_switch.py); one from before the topic's latest 换配置 is
    refused there (app/binding.py current_binding_card). A merged relay has none: it is several topics'."""
    topic, number, harness = job_field(job, "topic_id"), job_field(job, "job_id"), job_field(job, "harness_id")
    if not (topic and number and harness) or not butler.traits_of(job).switch_config:
        return list(rows or ())
    return beside(rows, relay_switch(topic, number, harness, job_field(job, "binding_revision") or 1))


def _part(log, job, name, make, default):
    """One optional part of a relayed answer (its 输入/输出, 产物): ``make()``, else ``default`` and a log line, the
    relay going without it (as app/answer_delivery.py AnswerParts.answer_tail does for the topic's own answer card)."""
    try:
        return make()
    except Exception as exc:  # noqa: BLE001 - the relay goes without it
        if log is not None:
            log(f"main thread relay of job #{job_field(job, 'job_id')} goes without its {name}: {type(exc).__name__}")
        return default


def after_turn(delivery, delivered, execution_status, *, messenger, profile, voice):
    """app/turn_delivery.py TurnDelivery.deliver's hook once the task card has its last words: the relay of a turn the butler
     started or handed on. Never raises: a failure is a log line, the turn's delivery stands; one before the
    relay's record said ``submitting`` leaves it ``unsent`` (app/relay_send.py record_unsent), never sent later.
    The answer's optional parts each go without when they cannot be made (``_part``)."""
    job, turn = delivery.job, delivery.turn
    log = getattr(getattr(delivery, "runtime", None), "log", None)
    outcome = None
    try:
        if not turn_source.behavior(job).own_delivery:
            return None  # A 协作 ask's turn: its answer is its collaboration's, never the top level's.
        state_dir = delivery.ctx.state_dir
        row = placement.plan(state_dir, job).row
        if not (row.question_copy or row.conclusion or row.stop):
            return None  # Nothing of this turn goes to the top level in this layout (core/placement.py).
        outcome = outcome_of(turn, delivered, execution_status, delivery.retry)
        if outcome is None:
            return None
        # A copy that could not be shown is followed by this layout's 等你 relay, which says where to answer: then the
        # copy's own line would say it twice (T889).
        if outcome == WAITING and row.question_copy and mirror_questions(delivery, quiet=bool(row.conclusion or row.stop)):
            return "mirrored"  # Its questions are at the top level as cards, the output above them.
        if not (row.conclusion or row.stop) and not (outcome in AWAITED and awaited_target(state_dir, job)):
            outcome = None
            return None  # The butler's answer went to the top level itself (app/worker.py); nothing to relay.
        send = messenger_send(messenger, profile, voice)
        facts = _facts(state_dir, job, turn, outcome)
        record = None
        if outcome == DONE:
            # The answer card's parts (app/answer_cards.py), as the topic's answer card has them.
            record = _part(log, job, "record", lambda: find_job_record(
                Path(state_dir), str(job_field(job, "message_id"))) or {}, {})
            facts.update(
                inputs=_part(log, job, "inputs", lambda: answer_cards.inputs(
                    job, delivery.cards.harness, getattr(delivery, "process_snapshot", lambda: None)(),
                    getattr(delivery, "ctx", None)), ()),
                outputs=_part(log, job, "outputs", lambda: answer_cards.outputs(
                    job, getattr(delivery, "process_snapshot", lambda: None)()), ()),
                files=_part(log, job, "files", lambda: answer_files(
                    state_dir, record, job_field(job, "topic_id"), voice) if record else None, None),
                files_status=_part(log, job, "files status", lambda: (answer_cards.ledger_of(
                    state_dir, record, voice) or {}).get("status") if record else None, None))
            changed = _part(log, job, "change", lambda: answer_cards.change(job, state_dir), {})
            if changed:
                facts["change"] = changed  # Only with one: a relay's facts without it are as before.
            noted = _part(log, job, "notes", lambda: answer_cards.notes(state_dir, job), [])
            if noted:
                facts["notes"] = noted  # The completion criteria not met, as the answer card shows them.
            offered = _part(log, job, "buttons", lambda: review.buttons(job), [])
            offered = _part(log, job, "switch", lambda: with_switch(offered, job), offered)
            if offered:
                facts["buttons"] = offered  # 独立复核 (COVEY_REVIEW=1), as on the answer card, and 换配置 beside it.
        state = relay(state_dir, job, outcome, send=send, profile=profile, log=log, **facts)
        if record and facts.get("files") and hasattr(delivery.artifacts, "refresh_answer_cards"):
            # The upload may have ended while the relay went out: its 产物 brought up to date (never raises).
            delivery.artifacts.refresh_answer_cards(delivery.ctx, record)
        return state
    except Exception as exc:  # noqa: BLE001 - the relay never fails the turn's delivery
        delivery.runtime.log(f"main thread relay of job #{job_field(job, 'job_id')} failed: {type(exc).__name__}: {exc}")
        if outcome is not None:
            _unsent(delivery.ctx.state_dir, job, outcome, exc, log)
        return None


def _unsent(state_dir, job, outcome, exc, log):
    """after_turn's failure before the relay's record was written: the record ``unsent`` when it has none."""
    try:
        key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
        record_unsent(state_dir, relay_path(state_dir, key), key, job, outcome, type(exc).__name__, log)
    except Exception:  # noqa: BLE001 - the log line above says it failed
        pass
