"""Sending what goes back to the butler's top level (app/butler_relay.py): each relay once (``send_once``, rule 6:
its card made first, then main-thread/relays/<job key>.json says ``submitting`` under a file lock no Feishu call is
made under, rules 1 and 2, then ``sent`` with the message id, or ``uncertain``; a job with a record is never sent
again), ``unsent`` for one that failed before any Feishu call (``record_unsent``: kept and logged, never sent later),
the one way a relay goes out through the Messenger (``messenger_send``), its T numbers as links (``linked``), and
what each message sent says about whose it is (``remember_sent``)."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import answer_cards
from covey.app.relay_targets import ask_for, turn_card
from covey.cards.answer_card import tail as answer_tail, tail_card
from covey.cards.relay_card import WAITING as WAITING_OUTCOME, relay_card, summary
from covey.core import butler
from covey.core.jobs import job_field
from covey.core.locks import resource_lock
from covey.core.state import keyed_json_path, read_object, write_json
from covey.core.topics import remember_topic_message, route_message

RELAYS = Path("main-thread") / "relays"
QUESTION_RELAYS = RELAYS / "questions"  # A question relayed as it was asked (app/butler_relay.py relay_question).
UNSENT = "unsent"  # Failed before it reached any Feishu call: the record says why; nothing sends it later.
SKIPPED = "skipped"  # Not sent by rule (``why``: owner_stop, the owner pressed 停止 himself, app/butler_relay.py relay).


def relay_path(state_dir, key) -> Path:
    return keyed_json_path(state_dir, RELAYS, key)


def lock(state_dir):
    """The relay records' file lock: held for reads and writes of them, never for a send (rules 1 and 2)."""
    return resource_lock("record", Path(state_dir) / RELAYS)


def messenger_send(messenger, profile, voice):
    """send(chat, text, key, reply_to, built, on_sent) -> whether it was confirmed: one relay message, ``built`` its
    card (None: the Markdown ``text`` alone), its idempotency key from ``key``."""
    def send(chat, text, key, reply_to, built, on_sent):
        return messenger.send_message(profile, chat, text, voice.key("main-relay", key), reply_to_message_id=reply_to,
                                      voice=voice, card=built, on_sent=on_sent)
    return send


def remember_sent(state_dir, chat_id, topic_id, messages, *, skip_errors=False) -> None:
    """Each relay message sent is the topic ``topic_id``'s (the butler's without one), and a quote of it goes to
    that topic (core/topics.py context_for_topic), whose waiting question takes it as the answer. ``skip_errors``:
    a message whose alias cannot be written is left as it is, the next ones still are."""
    for message in messages:
        try:
            remember_topic_message(Path(state_dir), topic_id or butler.butler_topic_id(chat_id), chat_id, message)
            if topic_id:
                route_message(Path(state_dir), chat_id, message, topic_id)
        except OSError:
            if not skip_errors:
                raise


def linked(state_dir, chat_id, built, text, profile=None, cards=None):
    """The relay's card and text with each T number a link to its topic (app/topic_links.py): the owner taps
    "T404" to open it (2026-11-07 02:00); ``cards``: {topic id: the card its T number opens}, the relayed turn's
    (2026-11-08: 点 T439 要到那张答案卡上). Unchanged when no link can be told."""
    from covey.app.topic_links import linkify

    def link(words):
        return linkify(words, state_dir, chat_id, profile=profile, cards=cards)
    def walk(elements):
        for element in elements or ():
            if not isinstance(element, dict):
                continue
            if element.get("tag") == "markdown":
                element["content"] = link(element.get("content") or "")
            elif element.get("tag") == "column_set":  # The 输入/输出 box (cards/answer_card.py), not 产物's names.
                for column in element.get("columns") or ():
                    walk(column.get("elements") if isinstance(column, dict) else ())
            elif element.get("tag") == "column":
                walk(element.get("elements"))
    if isinstance(built, dict):
        walk((built.get("body") or {}).get("elements"))
    return built, link(text)


def dispatch_note(state_dir, target, outcome, state, message_id="", reason="") -> None:
    """The 派发卡 of the work this relay concludes (app/dispatch_cards.py ``relayed``) is told the relay went out
    ("sent", ``message_id`` its conclusion) or was skipped, so it can shrink to a line; a relay that waits on the owner
    says nothing, and one that did not go out (uncertain, unsent) is not told: that card stays as it is. Only with
    COVEY_DISPATCH_CARD=1."""
    from covey.app import dispatch_cards

    if outcome != WAITING_OUTCOME and dispatch_cards.enabled():
        dispatch_cards.relayed(state_dir, target, outcome, state, message_id, reason)


def record_unsent(state_dir, path, key, job, outcome, error, log=None, target=None):
    """A relay that failed before its record said ``submitting`` (its card could not be made, or what it needs could
    not be read): the record ``unsent`` with ``error``, when there is none yet, and one log line; nothing is sent, now
    or later (an old release's ``send_once`` reading it returns its state too). The record's state."""
    return record_only(state_dir, path, key, job, outcome, UNSENT, log, target, error=str(error))


def record_only(state_dir, path, key, job, outcome, state, log=None, target=None, **why):
    """The record at ``path`` written ``state`` with ``why`` (UNSENT, SKIPPED), when there is none yet, under the lock,
    and one log line ``main relay state=…``; nothing is sent. The record's state."""
    with lock(state_dir):
        saved = read_object(path)
        if saved:
            return saved.get("state") or "uncertain"
        record = {"state": state, "outcome": outcome, "job_key": key, "job_id": job_field(job, "job_id"),
                  "topic_id": job_field(job, "topic_id"), "chat_id": (target or {}).get("chat_id")
                  or job_field(job, "chat_id"), "kind": (target or {}).get("kind") or "", **why, "at": time.time()}
        write_json(path, record, ensure_ascii=False, indent=1)
    if state == SKIPPED and target:
        dispatch_note(state_dir, target, outcome, state, reason=why.get("why", ""))
    if log is not None:
        log(f"main relay state={state} outcome={outcome} job={str(key)[-10:]} kind={record['kind'] or '-'}"
            + "".join(f" {name}={value}" for name, value in why.items()))
    return state


def send_once(state_dir, job, path, key, target, outcome, send, profile=None, log=None, **facts):
    """app/butler_relay.py ``relay``'s steps for the record at ``path``, its send's key from ``key``: its card made, the
    record ``submitting`` under the lock, the send outside it, then ``sent`` or ``uncertain``, which ``log`` (the
    runtime's log, None: none) gets as one line ``main relay state=…``; the record's state. A card that cannot be made
    leaves the record ``unsent`` (``record_unsent``): ``submitting`` only ever says it is about to go or went."""
    try:
        card = turn_card(state_dir, job, outcome)
        # What the topic did with the owner's words, in the speaker's sentence (cards/answer_card.py says).
        facts = {**facts, "ask": ask_for(state_dir, job, target, profile, at=card, log=log, doing=outcome)}
        reply_to = ""  # A message of its own at the top level, quoting no card.
        built, text = relay_card(target["label"], outcome, **facts)
        topic = str(target.get("topic_id") or job_field(job, "topic_id"))
        links = {topic: card}
        built, text = linked(state_dir, target["chat_id"], built, text, profile, cards=links)
    except Exception as exc:  # noqa: BLE001 - nothing went out: the record says so, the turn's delivery stands
        return record_unsent(state_dir, path, key, job, outcome, type(exc).__name__, log, target)
    with lock(state_dir):
        saved = read_object(path)
        if saved:
            return saved.get("state") or "uncertain"
        record = {"state": "submitting", "outcome": outcome, "job_key": key, "job_id": job_field(job, "job_id"),
                  "topic_id": job_field(job, "topic_id"), "chat_id": target["chat_id"], "reply_to": target["reply_to"],
                  "kind": target["kind"], "request": target["token"], "at": time.time()}
        write_json(path, record, ensure_ascii=False, indent=1)
    sent = []
    try:
        ok = send(target["chat_id"], text, key, reply_to, built, sent.append)
        error = "" if ok else "not confirmed"
        if ok and built is None and facts.get("inputs") is not None:
            # A reply too long for one card went as Markdown: its 产物 and 输入/输出 follow on a small card.
            tail_built, _ = linked(state_dir, target["chat_id"], tail_card(
                [*([facts["ask"]] if facts.get("ask") else []),
                 *answer_tail(facts.get("files"), facts["inputs"], facts.get("outputs") or (), link=target["label"],
                              change=facts.get("change") or facts.get("delivery"))],
                summary(target["label"], "产物与详情", ""), "上面是这一轮的回答，产物和详情在这里"), "", profile, cards=links)
            tails = []
            if send(target["chat_id"], f"{target['label']} 这一轮的产物和详情", key + "-tail", reply_to,
                    tail_built, tails.append) and tails:
                built = tail_built
                sent += tails
    except Exception as exc:  # noqa: BLE001 - it may have gone out: never sent again
        ok, error = False, type(exc).__name__
    if ok and sent and built is not None and facts.get("files"):
        # Its 产物 brought up to date once uploaded (app/answer_cards.py refresh).
        answer_cards.register(state_dir, key, "relay", sent[-1], built, facts.get("files_status"))
    record.update(state="sent" if ok else "uncertain", message_id=sent[0] if sent else "", error=error,
                  sent_at=time.time())
    with lock(state_dir):
        write_json(path, record, ensure_ascii=False, indent=1)
    if ok and sent:
        dispatch_note(state_dir, target, outcome, "sent", sent[0])
    # The topic's messages: the owner's quote of one goes to that topic, whose waiting question takes it as the answer.
    remember_sent(state_dir, target["chat_id"], target.get("topic_id"), sent, skip_errors=True)
    if log is not None:
        log(f"main relay state={record['state']} outcome={outcome} job={str(key)[-10:]} kind={target['kind']}"
            f" message={record['message_id'][-8:]}" + (f" error={error}" if error else ""))
    return record["state"]
