"""Sending what goes back to the butler's top level (app/butler_relay.py): each relay once (``send_once``, rule 6:
its card made first, then main-thread/relays/<job key>.json says ``submitting`` under a file lock no Feishu call is
made under, rules 1 and 2, then ``sent`` with the message id, or ``uncertain``; a job with a record is never sent
again; a 停住了 one keeps its card as sent, ``card``, for its buttons' clicks to redraw, app/relay_stops.py),
``unsent`` for one that failed before any Feishu call (``record_unsent``: kept and logged, never sent later),
the one way a relay goes out through the Messenger (``messenger_send``), its T numbers as links (``linked``), and
what each message sent says about whose it is (``remember_sent``). ``send_merged``: a 做完了 relay that may wait
COVEY_RELAY_MERGE_SECONDS for others of its chat (PENDING, listed in WAITING) and go out with them as one card."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import answer_cards, fold_close
from covey.app.relay_targets import ask_for, turn_card
from covey.cards.answer_card import tail as answer_tail, tail_card
from covey.cards.pages import Paged, fits
from covey.cards.relay_card import (DONE as DONE_OUTCOME, HINT_ID, STOPPED as STOPPED_OUTCOME, WAITING as WAITING_OUTCOME,
                                    merged_card, relay_card, relay_pages, reply_hint, summary)
from covey.cards.review import FOLD_ID, answer_lead
from covey.core import background, butler
from covey.core.failure import HEADLINE_CHARS
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


# The folds whose words are the answer's or the owner's (a long answer's opening is its title): their T numbers link.
# Not 产物's (file names) and not the details' fold.
LINKED_FOLDS = (FOLD_ID, "answer_reply", "mirror_output", "answer_words")


def linked(state_dir, chat_id, built, text, profile=None, cards=None, skip=()):
    """The relay's card and text with each T number a link to its topic (app/topic_links.py): the owner taps
    "T44" to open it; ``cards``: {topic id: the card its T number opens}, the relayed turn's; ``skip``: topic ids left
    plain. The answer's fold is linked, its opening (the fold's title) too; the card's row is not (a click on it opens
    the topic already), nor its last grey line (HINT_ID, where a reply goes). Unchanged when no link can be told."""
    from covey.app.topic_links import linkify
    from covey.cards.ui import ROW_ID

    def link(words):
        return linkify(words, state_dir, chat_id, profile=profile, cards=cards, skip=skip)
    def walk(elements):
        for element in elements or ():
            if not isinstance(element, dict) or element.get("element_id") in (ROW_ID, HINT_ID):
                continue
            if element.get("tag") == "markdown":
                element["content"] = link(element.get("content") or "")
            elif element.get("tag") == "column_set":  # The 输入/输出 box (cards/answer_card.py), not 产物's names.
                for column in element.get("columns") or ():
                    walk(column.get("elements") if isinstance(column, dict) else ())
            elif element.get("tag") == "column":
                walk(element.get("elements"))
            elif element.get("tag") == "collapsible_panel" and element.get("element_id") in LINKED_FOLDS:
                title = (element.get("header") or {}).get("title")
                if isinstance(title, dict) and title.get("tag") == "markdown":
                    title["content"] = link(title.get("content") or "")
                walk(element.get("elements"))
    if isinstance(built, dict):
        walk((built.get("body") or {}).get("elements"))
    return built, link(text)


def topic_url(state_dir, topic, card="", profile=None) -> str:
    """The link that opens ``topic`` at ``card`` (app/topic_links.py link_for), the relay's "进话题 ›"; '' when it cannot
    be told. Never raises."""
    from covey.app.topic_links import link_for

    try:
        return link_for(state_dir, topic, card=card, profile=profile)
    except Exception:  # noqa: BLE001 - the card without that link
        return ""


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
        made = _made(state_dir, job, target, outcome, profile, log, facts)
    except Exception as exc:  # noqa: BLE001 - nothing went out: the record says so, the turn's delivery stands
        return record_unsent(state_dir, path, key, job, outcome, type(exc).__name__, log, target)
    with lock(state_dir):
        saved = read_object(path)
        if saved:
            return saved.get("state") or "uncertain"
        record = _record(job, key, target, outcome, "submitting")
        write_json(path, record, ensure_ascii=False, indent=1)
    return _send(state_dir, [(path, record)], key, target, outcome, send, made, profile, log)


def _made(state_dir, job, target, outcome, profile, log, facts):
    """(card, text, links, facts) of ``job``'s relay: its card with its T numbers as links (``links``: {topic: the card
    each opens}), or, a 做完了 reply too large for one card once linked, its cards (cards/pages.py Paged,
    cards/relay_card.py relay_pages; ``send_pages``), and ``facts`` with the "你问" block."""
    card = turn_card(state_dir, job, outcome)
    # What the topic did with the owner's words, in the speaker's sentence (cards/answer_card.py says).
    facts = {**facts, "ask": ask_for(state_dir, job, target, profile, at=card, log=log, doing=outcome)}
    topic = str(target.get("topic_id") or job_field(job, "topic_id"))
    picture = answer_cards.avatar(state_dir, topic, str(target.get("chat_id") or ""), profile, log)  # Its call, outside the lock.
    url = topic_url(state_dir, topic, card, profile)
    built, text = relay_card(target["label"], outcome, url=url, avatar=picture, **facts)
    links, chat = {topic: card}, target["chat_id"]
    built, text = linked(state_dir, chat, built, text, profile, cards=links)
    if outcome == DONE_OUTCOME and not fits(built):
        # Too large for one card, or grown past it by its links: on several, each with the relay's row (never Markdown
        # with no row, the owner 2026-11-22); each card's room measured with its links in.
        built = relay_pages(target["label"], url=url, avatar=picture,
                            link=lambda words: linked(state_dir, chat, None, words, profile, cards=links)[1],
                            finish=lambda page: linked(state_dir, chat, page, "", profile, cards=links)[0], **facts)
    return built, text, links, facts


def send_pages(chat, key, reply_to, send, paged, sent):
    """The cards of a relay too large for one (``_made``, cards/pages.py Paged) in order, each a message of its own: the
    first under ``key`` (as one card would go), the n-th under ``key``-p<n>, each its part of the reply as Markdown when
    its card is refused (feishu/delivery.py); ``sent`` gets their message ids. (whether every one was confirmed, the card
    that carries what follows the reply: the last, None when it goes on a card of its own, ``alone``.)"""
    for n, page in enumerate(paged.pages, 1):
        if not send(chat, page.text, key if n == 1 else f"{key}-p{n}", reply_to, page.card, sent.append):
            return False, None
    return True, None if paged.alone else paged.pages[-1].card


def _record(job, key, target, outcome, state):
    return {"state": state, "outcome": outcome, "job_key": key, "job_id": job_field(job, "job_id"),
            "topic_id": job_field(job, "topic_id"), "chat_id": target["chat_id"], "reply_to": target["reply_to"],
            "kind": target["kind"], "request": target["token"], "at": time.time()}


def _send(state_dir, taken, key, target, outcome, send, made, profile, log):
    """The send of the records ``taken`` ([(path, record)], each ``submitting`` already): one relay with ``made``
    (``_made``), or several merged as one (``send_merged``, ``made`` then the merged card's), outside the lock; then each
    ``sent`` or ``uncertain`` with that message. The first record's state."""
    built, text, links, facts = made
    merged = len(taken) > 1
    reply_to = ""  # A message of its own at the top level, quoting no card.
    sent = []
    cut = built if isinstance(built, Paged) else None
    after = list(cut.after) if cut else []  # Its 这一轮中途的汇报, when the cards left it to the small one.
    try:
        if isinstance(built, Paged):  # A reply too large for one card, on several (``send_pages``).
            ok, built = send_pages(target["chat_id"], key, reply_to, send, built, sent)
        else:
            ok = send(target["chat_id"], text, key, reply_to, built, sent.append)
        error = "" if ok else "not confirmed"
        if ok and built is None and (facts.get("inputs") is not None or after):
            # Cards that could not hold its 产物 and 输入/输出 too: they follow on a small card, with the same row, its
            # 这一轮中途的汇报 folded first (cards/relay_card.py relay_pages).
            tail_built, _ = linked(state_dir, target["chat_id"], tail_card(
                [*([facts["ask"]] if facts.get("ask") else []), *after,
                 *answer_tail(facts.get("files"), facts.get("inputs"), facts.get("outputs") or (), link=target["label"],
                              change=facts.get("change") or facts.get("delivery"), notes=facts.get("notes") or (),
                              buttons=facts.get("buttons") or ()), reply_hint(target["label"])],
                summary(target["label"], "产物与详情", ""), "上面是这一轮的回答，产物和详情在这里"), "", profile, cards=links)
            tails = []
            if send(target["chat_id"], f"{target['label']} 这一轮的产物和详情", key + "-tail", reply_to,
                    tail_built, tails.append) and tails:
                built = tail_built
                sent += tails
    except Exception as exc:  # noqa: BLE001 - it may have gone out: never sent again
        ok, error = False, type(exc).__name__
    # Its 这一轮中途的汇报 closes on a click on its text, on whichever of its cards they went.
    for page, message_id in zip(cut.pages if cut else (), sent):
        fold_close.remember(state_dir, message_id, page.card, key)
    if ok and sent and built is not None:
        fold_close.remember(state_dir, sent[-1], built, key)
    if ok and sent and built is not None and facts.get("files"):
        # Its 产物 brought up to date once uploaded (app/answer_cards.py refresh).
        answer_cards.register(state_dir, key, "relay", sent[-1], built, facts.get("files_status"))
    keys = [record["job_key"] for _, record in taken]
    for _, record in taken:
        record.update(state="sent" if ok else "uncertain", message_id=sent[0] if sent else "", error=error,
                      sent_at=time.time())
        if merged:
            record["merged"] = keys  # The relays that went out as this one message.
        if ok and sent and outcome == STOPPED_OUTCOME and isinstance(built, dict):
            record["card"] = built  # A click on its buttons redraws it in place (app/relay_stops.py settle).
    with lock(state_dir):
        for path, record in taken:
            write_json(path, record, ensure_ascii=False, indent=1)
    if ok and sent:
        for _, record in taken:
            dispatch_note(state_dir, record.pop("target", None) or target, outcome, "sent", sent[0])
    # The topic's messages: the owner's quote of one goes to that topic, whose waiting question takes it as the answer;
    # a merged one is several topics', so a quote of it stays at the top level (the butler's).
    remember_sent(state_dir, target["chat_id"], "" if merged else target.get("topic_id"), sent, skip_errors=True)
    record = taken[0][1]
    if log is not None:
        log(f"main relay state={record['state']} outcome={outcome} job={str(key)[-10:]} kind={target['kind']}"
            f" message={record['message_id'][-8:]}" + (f" merged={len(taken)}" if merged else "")
            + (f" error={error}" if error else ""))
    return record["state"]


# 做完了 relays that come close together go as one (COVEY_RELAY_MERGE_SECONDS) --------------------------------------------

MERGE_ENV = "COVEY_RELAY_MERGE_SECONDS"
MERGE_SECONDS = 20
MERGE_MAX = 45  # Within what a task runner waits for its background work after its result (app/runner.py UPDATE_GRACE).
PENDING = "pending"  # A 做完了 relay waiting for the others of its chat (``send_merged``): nothing sent, nothing to resend.
WAITING = RELAYS / "waiting.json"  # {chat id: [the PENDING records' keys, in the order they came]}, under ``lock``.
AT_WORK = frozenset({"queued", "running", "cancel_requested"})


def pause(seconds) -> None:
    """The window a pending relay waits (``send_merged``), on its own thread."""
    time.sleep(seconds)


def merge_seconds(environ=None) -> int:
    """COVEY_RELAY_MERGE_SECONDS: how long a 做完了 relay waits for others of its chat while other tasks there are at work;
    MERGE_SECONDS when unset or not a whole number from 0 to MERGE_MAX; 0 merges none (each at once, as before)."""
    from covey.core import env as cenv

    raw = str(cenv.get(MERGE_ENV, "", environ) or "").strip()
    try:
        seconds = int(raw)
    except ValueError:
        return MERGE_SECONDS
    return seconds if 0 <= seconds <= MERGE_MAX else MERGE_SECONDS


def others_at_work(state_dir, job, chat) -> bool:
    """Whether a task of ``chat`` in a topic other than ``job``'s own is queued or running: its 做完了 may come within
    seconds. The butler's own turns relay nothing; a failure to read says no (the relay goes at once, as before)."""
    from covey.core.jobs import jobs_dir, job_key
    from covey.core.store import directory_store

    mine, own_topic = job_key(str(job_field(job, "message_id")), job_field(job, "job_id")), job_field(job, "topic_id")
    butler_topic = butler.butler_topic_id(chat)
    try:
        for _, record in directory_store(jobs_dir(Path(state_dir))).query(chat_id=chat, status=AT_WORK):
            topic = record.get("topic_id")
            if record.get("key") != mine and topic and topic not in (own_topic, butler_topic):
                return True
    except Exception:  # noqa: BLE001 - none known: at once
        return False
    return False


def file_count(state_dir, job) -> int:
    """How many 产物 the turn ``job`` has (app/artifact_policy.py drive_paths, its 产物 panel's): what the merged card's row
    says of them; 0 when its record cannot be read."""
    from covey.app.artifact_policy import drive_paths
    from covey.core.jobs import find_job_record

    try:
        record = find_job_record(Path(state_dir), str(job_field(job, "message_id"))) or {}
        return len(drive_paths(record)) if record else 0
    except Exception:  # noqa: BLE001 - the row without the count
        return 0


def send_merged(state_dir, job, path, key, target, send, profile=None, log=None, *, seconds, title="", **facts):
    """A 做完了 relay (DONE) that may go out with others of its chat (docs/butler.md 结论回到顶层): ``send_once``'s, sent at
    once as it sends it when no other task of the chat is at work (``others_at_work``) and no relay there waits; else its
    record PENDING (in WAITING), and ``seconds`` later every relay of the chat still pending goes as one message
    (``take``): one alone as its own card, several as one 「N 个话题做完了」 card (cards/relay_card.py merged_card), each
    record then ``submitting`` and ``sent`` or ``uncertain`` with that message. A record is taken from PENDING once,
    under the lock, so each relay still goes once (rule 6). ``title``: the topic's, on the merged card. The state."""
    outcome = DONE_OUTCOME
    try:
        made = _made(state_dir, job, target, outcome, profile, log, facts)
    except Exception as exc:  # noqa: BLE001 - nothing went out: the record says so, the turn's delivery stands
        return record_unsent(state_dir, path, key, job, outcome, type(exc).__name__, log, target)
    chat = target["chat_id"]
    busy = others_at_work(state_dir, job, chat)  # A read of the job records: not under the relay lock.
    files = file_count(state_dir, job) if facts.get("files") else 0  # Its 产物, said on the merged card's row.
    with lock(state_dir):
        saved = read_object(path)
        if saved:
            return saved.get("state") or "uncertain"
        waiting = read_object(Path(state_dir) / WAITING)
        queued = [other for other in waiting.get(chat) or () if other != key]
        alone = not busy and not queued
        record = _record(job, key, target, outcome, "submitting" if alone else PENDING)
        if not alone:
            record.update(target=target, item={"label": target["label"], "title": title,
                                               "lead": answer_lead(facts.get("text") or "", HEADLINE_CHARS),
                                               "topic_id": target.get("topic_id") or job_field(job, "topic_id"),
                                               "card": made[2].get(str(target.get("topic_id") or job_field(job, "topic_id")), ""),
                                               **({"files": files} if files else {})})
            waiting[chat] = [*queued, key]
            write_json(Path(state_dir) / WAITING, waiting, ensure_ascii=False, indent=1)
        write_json(path, record, ensure_ascii=False, indent=1)
    if alone:
        return _send(state_dir, [(path, record)], key, target, outcome, send, made, profile, log)
    if log is not None:
        log(f"main relay state={PENDING} outcome={outcome} job={str(key)[-10:]} kind={target['kind']} wait={seconds}s")

    def later():
        pause(seconds)
        try:
            take(state_dir, chat, path, key, target, send, made, profile, log)
        except Exception as exc:  # noqa: BLE001 - the records say what went; one still pending goes with the next
            if log is not None:
                log(f"main relay merge failed job={str(key)[-10:]}: {type(exc).__name__}")
    background.start(background.RELAY, later)
    return PENDING


def merged_fit(state_dir, chat, rows, links, profile=None):
    """(card, text) of the 做完了 relays ``rows`` as one card (cards/relay_card.py merged_card), its T numbers links
    (``linked``; ``links``, each topic's card): as many rows as one card holds (cards/pages.py fits), the rest on one grey
    line under them. Many at once passed a card's bytes from about 19 rows and CardKit's 200 nodes from 29 (T889)."""
    shown = len(rows)
    while True:
        built, text = merged_card(rows[:shown], more=rows[shown:])
        built, text = linked(state_dir, chat, built, text, profile, cards=links)
        if shown <= 1 or fits(built):
            return built, text
        shown -= 1


def take(state_dir, chat, path, key, target, send, made, profile=None, log=None):
    """The relays of ``chat`` still PENDING, the one at ``path`` first, taken as one send (``send_merged``): nothing
    when that one went with another's already. The state of the one at ``path``."""
    with lock(state_dir):
        own = read_object(path)
        if own.get("state") != PENDING:
            return own.get("state") or "uncertain"
        waiting = read_object(Path(state_dir) / WAITING)
        taken = []
        for other in dict.fromkeys([key, *(waiting.get(chat) or ())]):
            other_path = relay_path(state_dir, other)
            record = read_object(other_path)
            if record.get("state") == PENDING and record.get("chat_id") == chat:
                record.update(state="submitting", batch=key)
                write_json(other_path, record, ensure_ascii=False, indent=1)
                taken.append((other_path, record))
        gone = {record["job_key"] for _, record in taken}
        waiting[chat] = [other for other in waiting.get(chat) or () if other not in gone
                         and read_object(relay_path(state_dir, other)).get("state") == PENDING]
        if not waiting[chat]:
            waiting.pop(chat)
        write_json(Path(state_dir) / WAITING, waiting, ensure_ascii=False, indent=1)
    if len(taken) > 1:
        items = [record.get("item") or {"label": record.get("job_key", "")} for _, record in taken]
        links = {item["topic_id"]: item.get("card") or "" for item in items if item.get("topic_id")}
        from covey.app.topic_links import links_for

        urls = links_for(state_dir, links, cards=links, profile=profile)  # Each row opens its topic.
        # No picture: each row has its topic's T number as its badge (cards/ui.py topic_badge).
        rows = [{**item, "url": urls.get(item.get("topic_id") or "", "")} for item in items]
        built, text = merged_fit(state_dir, chat, rows, links, profile)
        made = (built, text, links, {})
        key = key + "-merged"
    return _send(state_dir, taken, key, target, DONE_OUTCOME, send, made, profile, log)
