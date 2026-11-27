"""Where a topic turn's outcome goes back to at the butler's top level (app/butler_relay.py, docs/butler.md
结论回到顶层): which turns came from the butler (``relay_target``: a request's opened record, a supplement it
gave, or an automatic retry of either; ``area_target``: the latest request that touched a 话题区, or the owner's own
launch from the web page, ``web_root``), the message a relay quotes (``relay_quote``, ``butler_job_owner_message``), its
"你问" block (``ask_for``), the card its T number opens (``turn_card``), and a project member's turn the owner waits for
(``awaited_target``: one the lead handed on while he was there, or one he asked for himself from outside the topic's
thread, ``asked_outside``). A hand-on can come from a turn that was handed on itself
(a lead's turn the butler's project_send started, ``_handed_up``): the quote and the "你问" block are then the ones of
the turn the owner spoke in, up that chain. Reads records only: nothing here sends or writes."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from covey.app import answer_cards
from covey.cards.relay_card import WAITING
from covey.core import butler_opened, placement, projects, turn_source
from covey.core.jobs import find_job_record, job_field, job_key, topic_job_records
from covey.core.store import directory_store
from covey.core.topics import load_topic, topic_label, topic_origin

PREFIX = "main-"  # A butler tool's synthetic message id (app/butler_topics.py).
RETRY_HOPS = 5
# Jobs ``_handed_up`` reads up a chain of hand-ons: a project member's turn is two hand-ons below the owner's message (the
# butler's project_send to the lead, the lead's project_topic_start), the rest is room for an end to a loop of records.
HAND_HOPS = 4


def _token_of(request) -> str:
    """The name of a request in record files and synthetic message ids (core/butler_opened.py request_token)."""
    return butler_opened.request_token(SimpleNamespace(id=request))


def _origin_record(state_dir, topic_id, token=""):
    """(token, kind, a record shaped as the request ledger's) of the butler's request that opened or forked ``topic_id``, from
    its topic.json ``origin`` ({kind "main", request, job_key, at, ``message_id``: the owner's message the butler's turn
    answered, kept since W2-B}), the ledger gone or not (main-thread/opened/ is cleared after 7 days, core/maintenance_rules.py);
    ``token``: only if it is that request's. None for a topic without one (a web one is ``web_root``'s) or one that names no butler job."""
    origin = topic_origin(state_dir, topic_id)
    request, job = str(origin.get("request") or ""), str(origin.get("job_key") or "")
    if origin.get("kind") != "main" or not request or not job:
        return None
    mine = _token_of(request)
    if token and token != mine:
        return None
    kind = butler_opened.FORK if origin.get("fork_of") else butler_opened.START
    return mine, kind, {"job_key": job, "topic_id": topic_id, "at": origin.get("at") or 0,
                        "message_id": str(origin.get("message_id") or "")}


def _quote_of(state_dir, record) -> str:
    """What a relay of a turn ``record`` (a request's, as the ledger or ``_origin_record`` gives it) quotes: its butler job's
    card or the owner's message (``relay_quote``), else the message id the origin kept ('' without one)."""
    quote = relay_quote(state_dir, record.get("job_key"))
    if quote:
        return quote
    kept = str(record.get("message_id") or "")
    return kept if kept.startswith("om_") else ""


def _opened(state_dir, message_id, job_id, topic_id):
    """(token, kind, the request's record) the job came from, or None: for a turn the butler started or forked the topic's
    own origin first (``_origin_record``: it outlives the ledger), then main-thread/opened/'s record; a job
    topic_send supplemented more than once: the latest owner message."""
    if message_id.startswith(PREFIX):
        token = message_id[len(PREFIX):]
        found = _origin_record(state_dir, topic_id, token)
        if found is not None:
            return found
        record = butler_opened.by_token(state_dir, token)
        if record:
            return token, butler_opened.kind_of(record), record
    found = butler_opened.steered(state_dir, job_id, topic_id)
    return None if found is None else (found[0], "steer", found[1])


def _origin_job(state_dir, job):
    """The job's (message_id, job_id): its own, or the one its automatic retry chain started from."""
    message_id, job_id = str(job_field(job, "message_id")), job_field(job, "job_id")
    for _ in range(RETRY_HOPS):
        record = find_job_record(Path(state_dir), job_key(message_id, job_id)) or {}
        previous = find_job_record(Path(state_dir), str(record.get("retry_of") or "")) if record.get("retry_of") else None
        if not previous or previous.get("auto_retry_state") != "queued":
            break
        message_id, job_id = str(previous.get("message_id") or ""), previous.get("job_id") or ""
    return message_id, job_id


def origin_message(state_dir, job) -> str:
    """The message id the turn ``job`` came from: its own, or the one its automatic retry chain started from
    (``_origin_job``)."""
    return _origin_job(state_dir, job)[0]


def _owner_message(job) -> str:
    """The Feishu message the job record ``job`` answers: its reply_to_message_id, else its own message id; '' for
    neither an ``om_`` one."""
    reply_to = str(job.get("reply_to_message_id") or "")
    if not reply_to.startswith("om_"):
        reply_to = str(job.get("message_id") or "")
    return reply_to if reply_to.startswith("om_") else ""


def butler_job_owner_message(state_dir, butler_job_key) -> str:
    """The owner's top-level message the butler job ``butler_job_key`` answers (the one its card quotes); ''
    when its record is gone or names no Feishu message. That job's own only, never one up its hand-ons
    (``relay_quote`` follows them): a lead's turn the butler handed on answers no message of the owner's itself. Whatever a
    lead's turn answers, the topics it opens hang under none (app/butler_topics.py place) and what it hands them is its
    own words (app/butler_topics.py handed, app/web_said.py); the conclusions of what it handed on while the owner was there
    still come back quoting his message (``awaited_target``)."""
    return _owner_message(find_job_record(Path(state_dir), str(butler_job_key or "")) or {})


def _handed_up(state_dir, key):
    """The jobs from the job ``key`` up the hand-ons it came from, each as (key, record): a job whose message id (that of
    its automatic retry chain's first, ``_origin_job``) is a hand-on's, "main-<token>", came from the job its
    main-thread/opened record names (``job_key``), as a lead's turn the butler's project_send started came from the
    butler's turn that took the owner's message (T812, 2026-11-26: T806, handed on by such a turn, relayed nothing).
    Stops at a job no hand-on started (the owner's own turn, a lead's wake-up), one whose record is gone or is of
    another chat, one read already, and after HAND_HOPS jobs."""
    seen, chat, key = set(), "", str(key or "")
    while key and key not in seen and len(seen) < HAND_HOPS:
        seen.add(key)
        record = find_job_record(Path(state_dir), key)
        if not record:
            return
        where = str(record.get("chat_id") or "")
        if chat and where and where != chat:
            return
        chat = chat or where
        yield key, record
        message_id = str(record.get("message_id") or "")
        if not message_id.startswith(PREFIX) and record.get("retry_of"):
            message_id = _origin_job(state_dir, record)[0]
        if not message_id.startswith(PREFIX):
            return
        token = message_id[len(PREFIX):]
        key = str(butler_opened.by_token(state_dir, token).get("job_key") or "")
        if not key:  # The ledger cleared: the topic's own origin names it (``_origin_record``).
            found = _origin_record(state_dir, record.get("topic_id"), token)
            key = str(found[2]["job_key"]) if found else ""


def relay_quote(state_dir, butler_job_key) -> str:
    """What a relay of the butler job ``butler_job_key``'s hand-on quotes: its card at the top level (``relay``),
    else the owner's message it answers; for a job with neither that was handed on itself (a lead's turn the butler's
    project_send started: no card of its own, message id "main-<token>"), the same of the job it came from, up the
    chain (``_handed_up``). '' when none is known: never for a chain that starts at a wake-up of the lead's, but for the
    first turn of a project the owner started or upgraded himself (its ``wake`` source carries his message, WAVE12 C5:
    what it hands out comes back quoting that message, W2-C)."""
    for _, record in _handed_up(state_dir, butler_job_key):
        card = str(record.get("card_message_id") or "")
        if card.startswith("om_"):
            return card
        owner = _owner_message(record) or _started_by(record)
        if owner:
            return owner
    return ""


def _started_by(job) -> str:
    """The owner's Feishu message a lead's wake-up ``job`` carries in its source (app/project_loop.py started_by), '' for
    none."""
    found = turn_source.origin(job).get("message_id", "") if projects.wake_turn(job) else ""
    return found if found.startswith("om_") else ""


def _asker(state_dir, key):
    """The record of the job whose words a relay of the hand-on of the job ``key`` quotes in its "你问" block: the top of
    ``_handed_up``, the turn the owner spoke in (for a lead's turn the butler's project_send started, the butler's turn
    with his message, not the words the butler handed the lead); None when ``key``'s record is gone."""
    found = None
    for found in _handed_up(state_dir, key):
        pass
    return found[1] if found else None


def areas_of(state_dir, butler_job_key) -> set:
    """The topics the butler job ``butler_job_key`` opened, forked or handed words to (main-thread/opened/), done."""
    found = set()
    for _, record in butler_opened.by_job(state_dir, butler_job_key) if butler_job_key else ():
        topic = butler_opened.topic_of(record)
        if topic and butler_opened.answered(record):
            found.add(str(topic))
    return found


def relays(state_dir, topic_id, chat) -> bool:
    """Whether the turns of ``topic_id`` relay at all: its row of core/placement.py has a conclusion. A topic's does;
    none with the butler off."""
    return placement.plan(state_dir, {"topic_id": topic_id, "chat_id": chat}).row.conclusion != placement.NONE


def web_root(state_dir, topic_id, chat=""):
    """(the request, the topic card, its chat) of a topic the owner opened on the web page in ``chat``
    (app/web_launch.py: topic.json's ``origin.kind`` "web", ``origin.request`` "web-<op id>"), its topic card at the top
    level as a topic the butler opens at once has; None for any other topic, or one whose topic.json cannot be read.
    Reads topic.json without a lock (rule 2)."""
    try:
        data = load_topic(Path(state_dir), str(topic_id))
    except (OSError, ValueError):
        return None
    origin = data.get("origin") if isinstance(data, dict) else None
    root = str(data.get("root_id") or "") if isinstance(data, dict) else ""
    if not isinstance(origin, dict) or origin.get("kind") != "web" or not root.startswith("om_"):
        return None
    opened_in = str(data.get("chat_id") or "")
    if chat and opened_in != str(chat):
        return None
    return str(origin.get("request") or ""), root, opened_in


def area_target(state_dir, topic_id, chat=""):
    """Where a turn of the 话题区 ``topic_id`` relays when no request of the butler started it: the latest one
    (main-thread/opened/) that opened, forked or handed on to it, as ``relay_target`` gives it, kind "follow"; a topic
    the owner opened on the web page that no request touched since (``web_root``) as its own, quoting its topic card
    (T762, 2026-11-25: T753, opened there, relayed none of its five turns nor copied its three questions); None for a
    topic no request touched."""
    if not topic_id or not relays(state_dir, topic_id, chat):
        return None
    best = butler_opened.latest_for_topic(state_dir, topic_id)
    if best is None:
        # The ledger has nothing of it (cleared after 7 days): the request that opened it, from its origin.
        started = _origin_record(state_dir, topic_id)
        reply_to = _quote_of(state_dir, started[2]) if started else ""
        if reply_to:
            token, _, record = started
            return {"chat_id": str(chat), "reply_to": reply_to,
                    "label": topic_label(Path(state_dir), topic_id) or "话题", "token": token, "kind": "follow",
                    "topic_id": topic_id, "main_key": record["job_key"]}
        web = web_root(state_dir, topic_id, chat)
        if web is None:
            return None
        request, root, opened_in = web
        return {"chat_id": opened_in or str(chat), "reply_to": root, "label": topic_label(Path(state_dir), topic_id)
                or "话题", "token": request, "kind": "follow", "topic_id": topic_id, "main_key": ""}
    reply_to = relay_quote(state_dir, best[1].get("job_key"))
    if not reply_to:
        return None
    return {"chat_id": str(best[1].get("chat_id") or chat), "reply_to": reply_to,
            "label": topic_label(Path(state_dir), topic_id) or "话题", "token": best[0], "kind": "follow",
            "topic_id": topic_id, "main_key": str(best[1].get("job_key") or "")}


def ask_for(state_dir, job, target, profile=None, *, at="", log=None, doing="about"):
    """The "你问"/"你答" block of the topic turn ``job`` its relay or question copy carries (app/answer_cards.py
    asked): the words of the owner's message the butler took it from (its job record, target ``main_key``, or the one
    that job was handed on from, ``_asker``), or,
    for a turn the owner started in the topic (kind "follow"), the turn's own words; None when it cannot be told.
    Never raises. Whatever the kind, the block opens the topic's thread (target ``topic_id``),
    at ``at`` (the relay's turn card or question card, ``turn_card``) or the turn's own card when
    they are in it: a second topic one message opened has its own root, the topic card (S6), so its own thread."""
    try:
        record = None
        if target.get("kind") != "follow" and target.get("main_key"):
            record = _asker(state_dir, target["main_key"])
        from covey.app.answer_cards import turn_messages

        return answer_cards.asked(state_dir, record or job, profile=profile, log=log, doing=doing,
                                  topic_id=str(target.get("topic_id") or job_field(job, "topic_id") or ""),
                                  at=[m for m in (at, *turn_messages(job)) if m])
    except Exception:  # noqa: BLE001 - the relay goes without it
        return None


def relay_target(state_dir, job):
    """Where a topic turn the butler started or handed on relays to: {chat_id, reply_to (the butler card,
    ``relay_quote``), label (its T number), token (the request's), kind (start, send, steer, fork), topic_id, main_key
    (the butler job's key)}; None for any other turn, the butler's own, or one whose butler job is not
    known."""
    topic_id, chat = str(job_field(job, "topic_id")), str(job_field(job, "chat_id"))
    if not relays(state_dir, topic_id, chat):
        return None
    message_id, job_id = _origin_job(state_dir, job)
    found = _opened(state_dir, message_id, job_id, topic_id)
    if found is None:
        return area_target(state_dir, topic_id, chat)
    token, kind, record = found
    reply_to = _quote_of(state_dir, record)
    if not reply_to:
        return None
    label = topic_label(Path(state_dir), topic_id) or "话题"
    return {"chat_id": str(record.get("chat_id") or chat), "reply_to": reply_to, "label": label, "token": token,
            "kind": kind, "topic_id": topic_id, "main_key": str(record.get("job_key") or "")}


def awaited_target(state_dir, job):
    """Where a project member's turn the owner waits for relays its conclusion or its stop (docs/projects.md 主人在等的结果):
    the lead handed it on (project_topic_start, project_topic_send or the supplement it gave, main-thread/opened/) in a turn
    of the lead the owner was in, one that was not a wake-up or one his words went into (``projects.woken``); as ``relay_target`` gives it, quoting
    the lead's card, else what the turn the owner spoke in has (``relay_quote``: the butler's card of a project_send).
    Else a turn he asked for himself from outside the topic's thread (``asked_outside``). None for any other turn: a
    member's row relays nothing of its own (core/placement.py), what a wake-up turn handed out is the lead's to tell (its
    style), and what he says in the topic's own thread is answered there."""
    topic_id, chat = str(job_field(job, "topic_id")), str(job_field(job, "chat_id"))
    if not projects.enabled() or not projects.is_member_topic(state_dir, topic_id):
        return None
    return _lead_handed(state_dir, job, topic_id, chat) or asked_outside(state_dir, job, topic_id, chat)


def _lead_handed(state_dir, job, topic_id, chat):
    """``awaited_target`` of a member's turn the lead handed on in a turn of his the owner was in; None for any other."""
    message_id, job_id = _origin_job(state_dir, job)
    found = _opened(state_dir, message_id, job_id, topic_id)
    if found is None:
        return None
    token, kind, record = found
    lead_key = str(record.get("job_key") or "")
    lead_job = find_job_record(Path(state_dir), lead_key) or {}
    lead_topic = str(lead_job.get("topic_id") or "")
    if (not lead_topic or projects.woken(lead_job)
            or not projects.is_lead_topic(state_dir, lead_topic, str(record.get("chat_id") or chat) or None)):
        return None
    reply_to = relay_quote(state_dir, lead_key)
    if not reply_to:
        return None
    return {"chat_id": str(record.get("chat_id") or chat), "reply_to": reply_to,
            "label": topic_label(Path(state_dir), topic_id) or "话题", "token": token, "kind": kind,
            "topic_id": topic_id, "main_key": lead_key}


def asked_outside(state_dir, job, topic_id, chat):
    """``awaited_target`` of a member's turn the owner asked for from outside the topic's thread (T812, 2026-11-26: he
    quoted T824's relay at the top level and asked again, #7397 answered in T824's thread, its message got DONE, and the
    top level heard nothing): his message quoting a card of the topic there (引用直达) started it (its ``top_message``,
    app/butler_reactions.py, or that of the turn its automatic retry chain started from), kind "follow", whose "你问" is the
    turn's own words; or such a message went into it as a supplement and the task took it (``_steered_outside``), kind
    "steer", whose "你问" is that message's. Either quotes his message. None for a turn he started in the thread, as the
    member's row has it."""
    label = topic_label(Path(state_dir), topic_id) or "话题"
    top = _top_of(state_dir, job)
    if top:
        return {"chat_id": chat, "reply_to": top, "label": label, "token": "", "kind": "follow", "topic_id": topic_id,
                "main_key": ""}
    steered = _steered_outside(state_dir, job, topic_id, chat)
    if steered is None:
        return None
    return {"chat_id": chat, "reply_to": str(steered.get("top_message")), "label": label, "token": "", "kind": "steer",
            "topic_id": topic_id, "main_key": str(steered.get("key") or "")}


def _top_of(state_dir, job) -> str:
    """``job``'s ``top_message`` (a person's message quoted from outside its topic's thread), else that of the turn its
    automatic retry chain started from (``_origin_job``); ''."""
    from covey.app.butler_reactions import top_of

    top = top_of(job)
    if top:
        return top
    message_id, job_id = _origin_job(state_dir, job)
    if message_id == str(job_field(job, "message_id")):
        return ""
    return top_of(find_job_record(Path(state_dir), job_key(message_id, job_id)) or {})


def _steered_outside(state_dir, job, topic_id, chat):
    """The record of the latest message quoted from outside the topic's thread that went into the running task ``job``
    as a supplement (app/steering.py steer_queued_job: its own turn cancelled, ``merged_into_job_id``, its ``top_message``
    kept) and that the task took: its delivery sent or read, not one that waits unread or that the task ended without
    (core/deliveries.py ``unread``, ``target_ended``; such a one becomes a turn of its own with the same
    ``top_message``). None for none."""
    from covey.app.butler_reactions import top_of
    from covey.core.deliveries import QUEUED_STEER, delivery_path, read_delivery, unread

    job_id = str(job_field(job, "job_id"))
    if not job_id:
        return None
    found = None
    for record in topic_job_records(Path(state_dir), topic_id, chat_id=chat, status="cancelled"):
        if str(record.get("merged_into_job_id") or "") != job_id or not top_of(record):
            continue
        delivery = read_delivery(delivery_path(Path(state_dir), topic_id, QUEUED_STEER + str(record.get("message_id") or "")))
        if (not isinstance(delivery, dict) or delivery.get("status") not in ("sent", "read") or unread(delivery)
                or delivery.get("target_ended")):
            continue
        if found is None or int(record.get("job_id") or 0) > int(found.get("job_id") or 0):
            found = record
    return found


def turn_card(state_dir, job, outcome) -> str:
    """The card of ``job``'s topic that its relay's T number opens: the question card a waiting turn asks on (its
    newest question record's question_message_id), else the turn's task card."""
    if outcome == WAITING:
        rows = directory_store(Path(state_dir) / "questions").query(job_id=job_field(job, "job_id"),
                                                                    topic_id=job_field(job, "topic_id"))
        for _, row in sorted(rows, key=lambda pair: pair[1].get("created_at") or 0, reverse=True):
            if str(row.get("question_message_id") or "").startswith("om_"):
                return str(row["question_message_id"])
    return str(job_field(job, "card_message_id") or "")
