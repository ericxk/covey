"""专项卡 (docs/butler.md 专项卡), when COVEY_PROJECTS=1: the one card of a project at the chat's top level, a message of its
own (cards/project_card.py), kept up to date and shrunk to one line when the project is done.

- ``ProjectCards`` (a feature object of the Bridge: ``threads()`` gives the refresh thread, none with the switch off): each
  second it looks at the projects that are on, reads the lead's and the members' job records and questions (no Feishu call, no
  lock) and draws the card; when what it would say differs from what it last wrote and at least ``MIN_GAP`` seconds have
  passed it writes the whole card (one CardKit write, the sequence ``reserve`` hands out first and never again, rule 3; a
  failed write backs off and goes on with the next sequence). It is the one writer of every project card: sending it
  (``created``), taking over a 派发卡 the upgrade replaces (``adopt_card``), the refresh, the 14 days, the one line.
- The sequence is kept in the record's ``card`` ({card_id, message_id, sequence, ...}), saved by a small update of that field
  alone (the lead's runs and the upgrade write the rest of the record at the same time).
- 14 days (CardKit stops taking writes to a card then): from ``FLIP_AFTER`` (12 days) of its age the card is sent again as a
  new message; the board's pin (app/pins.py ``aimed`` reads the record) follows, the new message routes to the lead, the old
  card becomes "已移到新卡 ›" and, when that cannot be written ``MOVE_TRIES`` times, is recalled.
- With COVEY_PROJECT_ASK=1 it also sends and settles the pause card (``ask``): a project a guard paused (``pause_ask`` in its
  record, app/project_loop.py) gets a card of its own where the project's card is, with 继续 / 收尾 / 停; once the owner chose
  (``ask_choice`` records it) or the project moved on some other way, that card is written once more as one line.
- The click helpers at the end are what the card's buttons run (app/card_actions.py: ``controls.project_stop`` etc.): they
  check the click belongs to the project's lead topic, the topic the card's message routes to.

The record fields this reads and nobody here writes (later packages fill them; an empty one is not drawn): ``direction``
(a text, or {name, source} with COVEY_DIRECTIONS=1: core/directions.py), ``lead_note`` ({text, at} or a string; P2-2), ``tokens_used`` (an int; P2-2), ``conclusion_message_id`` (the
lead's report on the project's end; P2-2), ``triage`` (a list of lines or {at, text} rows; P2-4's 分流 package),
``projects/<id>/memory.json`` (a list, or {"entries": [...]}, of strings or {id, text}) and ``projects/<id>/library/``
(its files' names)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

from covey.cards.project_card import (ASK_KINDS, DONE, PAUSED, RUNNING, WAITING, Project, Member, ask_card, asked_card,
                                      moved_card, project_card, settled_card)
from covey.app import project_loop
from covey.cards import answer_card
from covey.cards.facts import tile_count
from covey.core import directions, projects
from covey.core.card_sequence import CardHandle, reserve
from covey.core.jobs import job_path, read_job_record, topic_job_records
from covey.core.statuses import ENDED
from covey.core import dispatch_records as drecords
from covey.core.locks import resource_lock, topic_lock
from covey.core.loops import every
from covey.core.metrics import run_seconds, timings_of
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.store import directory_store
from covey.core.topics import (clear_topic_project, move_topic_message, remember_topic_message, route_message, topic_file,
                               topic_label, topic_title)

TICK = 1.0  # Seconds between two looks at the live records.
MIN_GAP = 2.5  # Seconds between two writes of one card (CardKit allows 10 a second).
BACKOFF = 60.0  # The longest wait after a failed write (2, 4, 8 ... seconds).
LINK_RETRY = 30.0  # Seconds before a link that could not be read is tried again (one read of Feishu per tick at most).
FLIP_AFTER = 12 * 86400  # A card this old is sent again: CardKit stops taking writes at 14 days.
MOVE_TRIES = 3  # Writes of "已移到新卡" tried on the old card before it is recalled.
CONCLUSION_GRACE = 600.0  # A project done this long without a conclusion message is shrunk without its link.
ANSWER_WAIT = 30.0  # How long after its finishing turn ended the card waits for that turn's answer id (written a moment later).
MEMBER_CAP = 40  # Member topics read for one card (the card draws MAX_FOLDS).
LIST_CAP = 50  # Rows read of the memory and the library.
NO_LEAD_JOB = "负责人现在没有在做的一轮。要对它说话，引用这张专项卡直接回复就行。"
CLOSED_WORDS = {"stopped": "专项已停住", "downgraded": "专项已降为普通话题"}
DOWNGRADED = "downgraded"  # The record's state_reason of a project the owner turned back (its state is cancelled).


def digest_of(card) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def words(text, limit=200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def clock(epoch) -> str:
    return time.strftime("%H:%M", time.localtime(float(epoch))) if epoch else ""


# What the records say -----------------------------------------------------------------------------------------------

def newest_job(state_dir, topic_id):
    from covey.app.dispatch_cards import job_number

    return max(topic_job_records(Path(state_dir), str(topic_id)), key=job_number, default=None)


def pending_question(state_dir, topic_ids):
    """(topic id, question message id) of the newest question shown and not answered, among ``topic_ids``; ('', '') for none."""
    best = None
    for topic in topic_ids:
        for _, row in directory_store(Path(state_dir) / "questions").query(topic_id=topic, status="pending"):
            if row.get("display_pending") or not row.get("question_message_id"):
                continue
            if best is None or (row.get("created_at") or 0) > (best[0] or 0):
                best = (row.get("created_at"), topic, str(row["question_message_id"]))
    return (best[1], best[2]) if best else ("", "")


def member_view(state_dir, topic_id, now, url="") -> Member:
    from covey.app import dispatch_cards as dc

    job = newest_job(state_dir, topic_id)
    status, body = dc.status_of(state_dir, {"topic_id": topic_id, "starts": True}, job, now)
    ended = status not in (dc.RUNNING, dc.WAITING)
    at = (epoch_seconds((job or {}).get("updated_at")) or now) if ended else now
    seconds = run_seconds(timings_of(job or {}), None, at) if job is not None else None
    word = DONE if ended else (WAITING if status == dc.WAITING else RUNNING)
    if status in (dc.FAILED, dc.STOPPED):
        body = ("失败：" if status == dc.FAILED else "已停止：") + body
    return Member(topic_id=topic_id, status=word, label=topic_label(Path(state_dir), topic_id),
                  title=topic_title(Path(state_dir), topic_id), body=words(body), url=url,
                  took=dc.quantize(seconds) if seconds is not None else "")


def lead_state(state_dir, record, now) -> str:
    from covey.app import dispatch_cards as dc

    job = newest_job(state_dir, record.get("lead_topic_id") or "")
    if job is None:
        return "待命"
    status, body = dc.status_of(state_dir, {"topic_id": record.get("lead_topic_id"), "starts": True}, job, now)
    if status in (dc.RUNNING, dc.WAITING):
        return ("等你 · " if status == dc.WAITING else "") + words(body, 80)
    return "待命"


def direction_of(state_dir, record) -> str:
    """The direction the project's cards draw: a text an older record holds as it is; {name, source} (COVEY_DIRECTIONS=1) only once
    its name is active (core/directions.py ``shown``)."""
    value = record.get("direction")
    return value if isinstance(value, str) else directions.shown(state_dir, value)


def lead_note(record) -> str:
    note = record.get("lead_note")
    text = note.get("text") if isinstance(note, dict) else note
    return words(text, 160) if isinstance(text, str) else ""


def triage_lines(record) -> tuple:
    rows = record.get("triage") if isinstance(record.get("triage"), list) else []
    lines = []
    for row in reversed(rows[-LIST_CAP:]):
        text = row.get("text") if isinstance(row, dict) else row
        if isinstance(text, str) and text.strip():
            at = clock(row.get("at")) if isinstance(row, dict) and isinstance(row.get("at"), (int, float)) else ""
            lines.append((at + " " if at else "") + words(text, 120))
    return tuple(lines)


def memory_rows(state_dir, project_id) -> tuple:
    """(entry id, text) of the project's memory (projects/<id>/memory.json), the newest first."""
    data = _json(projects.folder(state_dir) / project_id / "memory.json")
    entries = data.get("entries") if isinstance(data, dict) else data
    rows = []
    for index, entry in enumerate(entries if isinstance(entries, list) else ()):
        text = entry.get("text") if isinstance(entry, dict) else entry
        if isinstance(text, str) and text.strip():
            entry_id = entry.get("id") if isinstance(entry, dict) and entry.get("id") else index
            rows.append((str(entry_id), words(text, 120)))
    return tuple(reversed(rows[-LIST_CAP:]))


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def library_rows(state_dir, project_id) -> tuple:
    """(file name,) of the files in projects/<id>/library/ (one level, no links), by name."""
    folder = projects.folder(state_dir) / project_id / "library"
    names = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                    names.append(entry.name)
                if len(names) >= LIST_CAP:
                    break
    except OSError:
        return ()
    return tuple((name,) for name in sorted(names))


def facts(state_dir, record, now, titles=None, links=None) -> Project:
    """What the card of ``record`` says now. ``links``: {"need": url of the question card, "threads": {topic id: url}}."""
    from covey.app import dispatch_cards as dc

    links = links or {}
    pid, lead = str(record.get("id") or ""), str(record.get("lead_topic_id") or "")
    topics = [topic for topic in (record.get("threads") or ()) if isinstance(topic, str)][:MEMBER_CAP]
    members = tuple(member_view(state_dir, topic, now, (links.get("threads") or {}).get(topic, "")) for topic in topics)
    waiting = next((member for member in members if member.status == WAITING), None)
    state = record.get("state")
    if state == projects.DONE:
        status = DONE
    elif state == projects.PAUSED:
        status = PAUSED
    else:
        status = WAITING if waiting or lead_state(state_dir, record, now).startswith("等你") else RUNNING
    need = ""
    if status == WAITING:
        need = waiting.body.removeprefix("等你回答：") if waiting else lead_state(state_dir, record, now).removeprefix("等你 · ")
        need = " · ".join(part for part in (waiting.label if waiting else "", need) if part)
    began = float(record.get("created_at") or now)
    end = now if status != DONE else float(record.get("updated_at") or now)
    elapsed = dc.quantize(max(0.0, end - began))
    used = record.get("tokens_used")
    tokens = tile_count(used) if type(used) is int and used > 0 else ""
    job = newest_job(state_dir, lead) or (newest_job(state_dir, topics[0]) if topics else None) or {}
    harness = job.get("harness_id") or job.get("backend") or ""
    model = str(job.get("model") or "").rsplit(" · ", 1)[-1]
    effort = str(job.get("effort") or "").strip()
    inputs = tuple(part for part in ((titles or {}).get(harness, harness), model,
                                     f"{answer_card.BUDGET_WORD} {effort}" if effort else "") if part)
    parts = [answer_card.figure("开始", clock(began)), answer_card.figure("已运行" if status != DONE else "用时", elapsed)]
    if tokens:
        parts.append(answer_card.figure("token", tokens))
    ask = record.get("ask") if isinstance(record.get("ask"), dict) else {}
    return Project(project_id=pid, lead_topic=lead, name=str(record.get("name") or ""), status=status,
                   direction=direction_of(state_dir, record),
                   need=need, need_url=str(links.get("need") or ""), members=members, elapsed=elapsed, tokens=tokens,
                   lead_state=projects.reason_words(record) or lead_state(state_dir, record, now), lead_note=lead_note(record),
                   triage=triage_lines(record), memory=memory_rows(state_dir, pid), library=library_rows(state_dir, pid),
                   inputs=inputs, outputs=tuple(answer_card.output_lines(parts, answer_card.RUNNING_LINES)),
                   ask_text=str(ask.get("text") or ""), ask_at=str(ask.get("at") or ""), ask_url=str(ask.get("url") or ""),
                   origin_note=origin_words((record.get("origin") or {}).get("note") if isinstance(record.get("origin"), dict) else ""))


def origin_words(note) -> str:
    """The origin line of the card as the owner reads it today: a record written before 2026-11-16 holds the older
    wording (由快事升级……), which is shown as the new one; the record itself is not rewritten."""
    note = str(note or "")
    if note.startswith("由快事升级（你说了"):
        return "由管家转成专项"
    rounds = re.fullmatch(r"由快事升级（来回第 (\d+) 轮）", note)
    return f"来回 {rounds.group(1)} 轮后转成专项" if rounds else note


def titles_of(topics) -> dict:
    try:
        return {name: app.harness.spec.title for name, app in topics.apps.items()}
    except AttributeError:
        return {}


# Sending, taking over --------------------------------------------------------------------------------------------------

def adopt_card(state_dir, project_id, *, card_id, message_id, sequence=0, created_at=None) -> dict:
    """The card of a message that already exists (the 派发卡 an upgrade makes the project's) is the project's card from now
    on: the next look of ``ProjectCards`` replaces it whole (the header, the tags and every element id change; CardKit
    takes a whole-card PUT over any card of the same message). ``sequence`` is the last one used on it (a 派发卡 record's
    ``sequence``): the writes go on from it. The caller (the upgrade, P2-3) has settled the dispatch record first, so
    ``DispatchCards`` writes it no more, and routed the message to the lead (``route_message``)."""
    return projects.update_card(state_dir, project_id, card_id=str(card_id), message_id=str(message_id),
                                sequence=int(sequence or 0), created_at=float(created_at or time.time()),
                                digest="", wrote_at=0.0, closed=None, old=None, pending_card_id=None)


def created(env, record) -> str:
    """Send the project's card as a message of its own at the chat's top level (not a reply: it would go into a thread) and
    keep it in the record; the message id, '' when it did not go out. Once: a record with a card is left as it is.
    Never raises. ``env`` as app/dispatch_cards.py ``created`` takes it."""
    if not projects.enabled():
        return ""
    try:
        return _created(env, record)
    except Exception as exc:  # noqa: BLE001 - the project is open; only its card is missing
        env.runtime.log(f"project card failed project={str(record.get('id'))[:8]}: {type(exc).__name__}")
        return ""


def _created(env, record) -> str:
    state, pid = Path(env.state_dir), str(record["id"])
    saved = projects.read_record(state, pid) or record
    if projects.card_message(saved):
        return projects.card_message(saved)
    card = project_card(facts(state, saved, time.time(), titles_of(env.topics)))
    pending = (saved.get("card") or {}).get("pending_card_id") or ""
    card_id, message = env.messenger.create_and_send_card(
        env.profile, saved["chat_id"], card, env.voice.key("project-card", pid + "-0"), voice=env.voice, card_id=pending,
        on_created=lambda cid: projects.update_card(state, pid, pending_card_id=cid))
    if not message:
        env.runtime.log(f"project card not sent project={pid[:8]}")
        return ""
    projects.update_card(state, pid, card_id=card_id, message_id=message, sequence=0, created_at=time.time(),
                         digest=digest_of(card), wrote_at=time.time(), pending_card_id=None, closed=None)
    _route(state, saved, message)
    env.runtime.log(f"project card sent project={pid[:8]} message={message[-8:]}")
    return message


def _route(state, record, message) -> None:
    """A click on the card and a quote of it go to the project's lead (``route_message``)."""
    lead, chat = str(record.get("lead_topic_id") or ""), str(record.get("chat_id") or "")
    remember_topic_message(state, lead, chat, message)
    route_message(state, chat, message, lead, holder=lead)


def finishing_answer(state_dir, record) -> tuple[str, bool]:
    """(message id, ended) of the lead's turn that finished the project (``state_job``, app/project_tools.py
    project_finish): the answer it sent at the chat's top level, once that turn has ended, and whether it has ended. A turn
    that ended with no answer of its own (a quiet wake-up turn's reply is not sent) gives ('', True): the card shrinks at
    once instead of waiting CONCLUSION_GRACE for a message that never comes (accept7, 2026-11-16: 10 minutes)."""
    key = str(record.get("state_job") or "")
    job = (read_job_record(job_path(state_dir, key)) or {}) if key else {}
    if not job or job.get("status") not in ENDED:
        return "", False
    for message in (job.get("answer_message_id"), job.get("card_message_id")):
        if str(message or "").startswith("om_"):
            return str(message), True
    return "", True


def update_ask(state_dir, project_id, **fields) -> dict:
    """``update`` of the card in the record's ``pause_ask`` alone (``card_id``, ``message_id``, ``sequence``, ``closed``): the
    fields merged into it, one given as None removed; nothing when the record has no ``pause_ask``."""
    def change(record):
        ask = record.get("pause_ask")
        if not isinstance(ask, dict):
            return
        card = ask.get("card") if isinstance(ask.get("card"), dict) else {}
        card = {**{"card_id": "", "message_id": "", "sequence": 0}, **card}
        for key, value in fields.items():
            if value is None:
                card.pop(key, None)
            else:
                card[key] = value
        ask["card"] = card
    return projects.update(state_dir, project_id, change)


def ask_note(ask) -> str:
    """What the pause card says of the topics that were at work: a ``tokens`` pause asked them to stop, a ``wakes`` one left them."""
    if ask.get("reason") == "tokens":
        return f"在做的 {int(ask['halted'])} 个话题已请它们停下。" if type(ask.get("halted")) is int and ask["halted"] > 0 else ""
    return "在做的话题不受影响，还在继续做。" if ask.get("reason") == "wakes" else ""


class ProjectCards:
    """The Bridge's feature object (app/unified.py PARTS ``project_cards``): the refresh thread, off unless COVEY_PROJECTS=1,
    and nothing else (no menu item, no click it claims: the buttons route as the lead topic's)."""
    name = "project_cards"

    def __init__(self, runtime, messenger, voice, titles=None) -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.titles = titles or {}
        self.sched: dict = {}  # project id -> {digest, wrote, next, failures, links, link_at, done_seen, old_next}

    def threads(self):
        return ((self.name, self.run),) if projects.enabled() else ()

    def run(self, ctx) -> None:
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def tick(self, ctx, now=None) -> None:
        """One look at every project that has a card: draw what changed, shrink what ended, send again what is old."""
        if not projects.enabled():
            return
        now = time.time() if now is None else now
        for record in projects.records(ctx.state_dir):
            if isinstance(record.get("pause_ask"), dict):
                try:
                    self.ask(ctx, record, now)
                except Exception as exc:  # noqa: BLE001 - the project's own card still refreshes
                    self.runtime.log(f"project pause card failed project={str(record.get('id'))[:8]}: {type(exc).__name__}")
            card = record.get("card") if isinstance(record.get("card"), dict) else {}
            if not card.get("card_id") or not card.get("message_id"):
                continue  # Still being sent (created), or no card yet.
            try:
                self.refresh(ctx, record, card, now)
            except Exception as exc:  # noqa: BLE001 - the others still refresh
                self.runtime.log(f"project card refresh failed project={str(record.get('id'))[:8]}: {type(exc).__name__}")

    def refresh(self, ctx, record, card, now) -> None:
        pid = str(record["id"])
        seen = self.sched.setdefault(pid, {"digest": str(card.get("digest") or ""), "wrote": float(card.get("wrote_at") or 0),
                                           "next": 0.0, "failures": 0, "links": {}, "link_at": 0.0, "done_seen": 0.0,
                                           "old_next": 0.0})
        if card.get("old"):
            self.retire_old(ctx, record, card, seen, now)
        if card.get("closed") or now < seen["next"]:
            return
        state = record.get("state")
        if state == projects.DONE:
            return self.settle(ctx, record, card, seen, now)
        if state == projects.CANCELLED:
            return self.settle(ctx, record, card, seen, now, reason=CLOSED_WORDS["downgraded"])
        if now - float(card.get("created_at") or record.get("created_at") or now) > FLIP_AFTER:
            return self.flip(ctx, record, card, seen, now)
        view = facts(ctx.state_dir, record, now, self.titles, self.links(ctx, record, seen, now))
        built = project_card(view)
        digest = digest_of(built)
        if digest == seen["digest"] or now - seen["wrote"] < MIN_GAP:
            return
        if self.write(ctx, record, card, seen, built, now):
            projects.update_card(ctx.state_dir, pid, digest=digest, wrote_at=now)
            seen["digest"] = digest

    def ask(self, ctx, record, now) -> None:
        """The pause card of a project a guard paused (COVEY_PROJECT_ASK=1): sent while the pause stands and the owner has not
        chosen, written as one line once he has or the project moved on; each at most once, a failed write backs off."""
        ask = record["pause_ask"]
        card = ask.get("card") if isinstance(ask.get("card"), dict) else {}
        pid = str(record["id"])
        seen = self.sched.setdefault(pid + "/ask", {"next": 0.0, "failures": 0})
        if card.get("closed") or now < seen["next"]:
            return
        standing = (record.get("state") == projects.PAUSED and record.get("state_reason") == ask.get("reason")
                    and not ask.get("choice"))
        if not card.get("message_id"):
            return self.send_ask(ctx, record, ask, card, seen, now) if standing else update_ask(ctx.state_dir, pid, closed=True)
        if not standing:
            self.close_ask(ctx, record, ask, card, seen, now)

    def send_ask(self, ctx, record, ask, card, seen, now) -> None:
        pid, state = str(record["id"]), ctx.state_dir
        built = ask_card(record.get("name"), str(ask.get("why") or ""), project_id=pid,
                         lead_topic=str(record.get("lead_topic_id") or ""), note=ask_note(ask))
        key = self.voice.key("project-ask", f"{pid}-{int(float(ask.get('at') or 0) * 1000)}")
        card_id, message = self.messenger.create_and_send_card(
            ctx.profile, str(record.get("chat_id") or ""), built, key, voice=self.voice, card_id=card.get("pending_card_id") or "",
            on_created=lambda cid: update_ask(state, pid, pending_card_id=cid))
        if not message:
            seen["failures"] += 1
            seen["next"] = now + min(BACKOFF, 2.0 ** seen["failures"])
            self.runtime.log(f"project pause card not sent project={pid[:8]}")
            return
        update_ask(state, pid, card_id=card_id, message_id=message, sequence=0, pending_card_id=None)
        _route(Path(state), record, message)
        self.runtime.log(f"project pause card sent project={pid[:8]} reason={ask.get('reason')} message={message[-8:]}")

    def close_ask(self, ctx, record, ask, card, seen, now) -> None:
        pid = str(record["id"])
        built = asked_card(record.get("name"), str(ask.get("why") or ""), str(ask.get("choice") or ""))

        def save(sequence):
            update_ask(ctx.state_dir, pid, sequence=sequence)
        if self.put(ctx, pid, card["card_id"], card.get("sequence"), save, built, "a"):
            update_ask(ctx.state_dir, pid, closed=True)
            return
        seen["failures"] += 1
        seen["next"] = now + min(BACKOFF, 2.0 ** seen["failures"])
        self.runtime.log(f"project pause card not settled project={pid[:8]} failures={seen['failures']}")

    def links(self, ctx, record, seen, now) -> dict:
        """The links the card names: the open question card and each thread's thread, each read from Feishu once (app/topic_links.py
        keeps what it read); a read that failed is tried again after LINK_RETRY, and one read a tick at most."""
        found = seen["links"]
        if now < seen["link_at"]:
            return found
        from covey.app.topic_links import message_link, place_of, topic_thread_link

        topics = [topic for topic in (record.get("threads") or ()) if isinstance(topic, str)]
        lead = str(record.get("lead_topic_id") or "")
        try:
            topic, message = pending_question(ctx.state_dir, [*topics, lead])
            if message and found.get("need_for") != message:
                seen["link_at"] = now + LINK_RETRY
                where = place_of(ctx.state_dir, topic, message, ctx.profile)
                found["need"] = message_link(record.get("chat_id"), where["position"]) if where.get("position") else ""
                found["need_for"] = message if found["need"] else ""
            elif not message:
                found["need"], found["need_for"] = "", ""
            thread_links = found.setdefault("threads", {})
            missing = next((topic for topic in topics if not thread_links.get(topic)), "")
            if missing and now >= seen["link_at"]:
                seen["link_at"] = now + LINK_RETRY
                thread_links[missing] = topic_thread_link(ctx.state_dir, missing, profile=ctx.profile)[0] or ""
        except Exception as exc:  # noqa: BLE001 - the card without its links
            seen["link_at"] = now + LINK_RETRY
            self.runtime.log(f"project card link failed: {type(exc).__name__}")
        return found

    def put(self, ctx, project_id, card_id, sequence, save, built, label) -> bool:
        """One whole-card write: the sequence reserved first (saved before the request goes out), never reused."""
        handle = CardHandle(card_id, save, sequence, shared=False)
        with handle.lock:
            number = reserve(handle)
            result = self.messenger.put_card(ctx.profile, card_id, built, sequence=number,
                                             key=f"project-card-{project_id[:12]}-{label}-{number}", timeout=3)
        return result is not None

    def write(self, ctx, record, card, seen, built, now) -> bool:
        pid = str(record["id"])

        def save(sequence):
            projects.update_card(ctx.state_dir, pid, sequence=sequence)
        if self.put(ctx, pid, card["card_id"], card.get("sequence"), save, built, "w"):
            seen["failures"], seen["wrote"], seen["next"] = 0, now, 0.0
            return True
        seen["failures"] += 1
        seen["next"] = now + min(BACKOFF, 2.0 ** seen["failures"])
        self.runtime.log(f"project card not written project={pid[:8]} failures={seen['failures']}")
        return False

    def settle(self, ctx, record, card, seen, now, reason="") -> None:
        """The project is done (or it is not one any more): the card becomes one line. Done waits for the lead's conclusion
        message (``conclusion_message_id``) for CONCLUSION_GRACE seconds, so the line can open it."""
        pid, url = str(record["id"]), ""
        conclusion = str(record.get("conclusion_message_id") or "")
        if not reason:
            seen["done_seen"] = seen["done_seen"] or now
            ended = False
            if not conclusion:
                conclusion, ended = finishing_answer(ctx.state_dir, record)
                if ended and not conclusion:
                    # The turn's answer id is written a moment after its status (accept8: the line came out without the
                    # link it then had); one that never comes (a quiet wake-up turn) is given up after ANSWER_WAIT.
                    seen["answer_wait"] = seen.get("answer_wait") or now
                    ended = now - seen["answer_wait"] >= ANSWER_WAIT
            if not conclusion and not ended and now - seen["done_seen"] < CONCLUSION_GRACE:
                return
            if conclusion:
                url = self.message_url(ctx, record, conclusion)
        built = settled_card(record.get("name"), url=url, reason=reason, direction=direction_of(ctx.state_dir, record))
        if self.write(ctx, record, card, seen, built, now):
            projects.update_card(ctx.state_dir, pid, closed=True, settled_at=now, digest=digest_of(built))

    def message_url(self, ctx, record, message_id) -> str:
        """The link that opens ``message_id`` at its place in the chat; '' when it cannot be read."""
        from covey.app.topic_links import message_link, place_of

        try:
            where = place_of(ctx.state_dir, str(record.get("lead_topic_id") or ""), message_id, ctx.profile)
        except Exception:  # noqa: BLE001 - the line without its link
            return ""
        return message_link(record.get("chat_id"), where["position"]) if where.get("position") else ""

    def flip(self, ctx, record, card, seen, now) -> None:
        """The card is FLIP_AFTER old: a new card is sent (a message of its own; the key holds the generation, so a try after
        one whose outcome was unknown posts nothing new), the record points at it, the old one is shrunk by ``retire_old``."""
        pid, state, chat = str(record["id"]), ctx.state_dir, str(record.get("chat_id") or "")
        generation = int(card.get("generation") or 0) + 1
        built = project_card(facts(state, record, now, self.titles, self.links(ctx, record, seen, now)))
        card_id, message = self.messenger.create_and_send_card(
            ctx.profile, chat, built, self.voice.key("project-card", f"{pid}-{generation}"), voice=self.voice,
            card_id=card.get("pending_card_id") or "",
            on_created=lambda cid: projects.update_card(state, pid, pending_card_id=cid))
        if not message:
            seen["failures"] += 1
            seen["next"] = now + min(BACKOFF, 2.0 ** seen["failures"])
            self.runtime.log(f"project card not sent again project={pid[:8]}")
            return
        old = {"card_id": card["card_id"], "message_id": card["message_id"], "sequence": int(card.get("sequence") or 0),
               "tries": 0}
        # One write of the record: the pin (app/pins.py aimed) and the clicks (the alias below) follow the new card.
        projects.update_card(state, pid, card_id=card_id, message_id=message, sequence=0, created_at=now,
                             generation=generation, pending_card_id=None, digest=digest_of(built), wrote_at=now, old=old)
        _route(Path(state), record, message)
        seen.update(digest=digest_of(built), wrote=now, failures=0, next=0.0, old_next=0.0)
        self.runtime.log(f"project card sent again project={pid[:8]} generation={generation}")

    def retire_old(self, ctx, record, card, seen, now) -> None:
        """The card that was sent again becomes "已移到新卡 ›"; one that cannot be written MOVE_TRIES times is recalled
        (撤回) instead, so two live-looking cards never stay."""
        if now < seen["old_next"]:
            return
        pid, old = str(record["id"]), dict(card["old"])
        url = self.message_url(ctx, record, str(card.get("message_id") or ""))
        built = moved_card(record.get("name"), url, direction=direction_of(ctx.state_dir, record))

        def save(sequence):
            projects.update_card(ctx.state_dir, pid, old={**old, "sequence": sequence})
        if self.put(ctx, pid, old["card_id"], old.get("sequence"), save, built, "m"):
            projects.update_card(ctx.state_dir, pid, old=None)
            return
        old["tries"] = int(old.get("tries") or 0) + 1
        if old["tries"] >= MOVE_TRIES:
            self.messenger.recall(ctx.profile, old["message_id"])
            projects.update_card(ctx.state_dir, pid, old=None)
            self.runtime.log(f"project card old recalled project={pid[:8]}")
            return
        projects.update_card(ctx.state_dir, pid, old=old)
        seen["old_next"] = now + 2.0 ** (old["tries"] + 1)


# The buttons (app/card_actions.py: controls.project_*, steering.project_steer) -----------------------------------------

def topic_ctx(state_dir, topic_id):
    """What ``topic_lock`` needs of a topic: where its history is, which is the topic's directory."""
    return SimpleNamespace(history_path=topic_file(Path(state_dir), topic_id).parent / "history.jsonl")


def downgrade(host, ctx, record) -> str:
    """不要专项: the line said to the owner. The record is cancelled first (``ProjectCards`` then shrinks the card to "⏹ 专项已降为
    普通话题"); the lead's queued and running tasks are stopped; the threads (and the topic the project grew from) lose their
    ``project`` field; the card's message, and the lines of the 派发卡 it took in, go back to the topic the project grew from
    (a direct project's, to its first thread; none when it has none). Every step is safe to repeat, so a click on a project that
    was turned back half way finishes it. ``host`` is the component the click came to (its runtime, messenger and voice)."""
    state, pid = Path(ctx.state_dir), str(record["id"])
    now = projects.read_record(state, pid) or record
    name = words(now.get("name"), 30) or "专项"
    if now.get("state") == projects.DONE:
        return f"专项「{name}」已经做完，没有要降的。"
    if now.get("state") == projects.CANCELLED and now.get("state_reason") != DOWNGRADED:
        return f"专项「{name}」已经取消了。"
    if now.get("state") != projects.CANCELLED:
        try:
            now = projects.set_state(state, pid, projects.CANCELLED, DOWNGRADED)
        except ValueError:  # Done, or cancelled, since it was read.
            return f"专项「{name}」刚刚已经结束了，没有要降的。"
    chat, lead = str(now.get("chat_id") or ""), str(now.get("lead_topic_id") or "")
    stopped = stop_lead(host, ctx, lead, chat, pid)
    origin = now.get("origin") if isinstance(now.get("origin"), dict) else {}
    topics = [topic for topic in dict.fromkeys([origin.get("topic_id"), *(now.get("threads") or ())])
              if isinstance(topic, str) and topic]
    plain = [topic for topic in topics if release(state, topic, pid)]
    back = origin.get("topic_id") or next(iter(topics), "")
    if back:
        messages = {str((now.get("card") or {}).get("message_id") or "")}
        for path in drecords.live_paths(state):
            saved = read_object(path)
            if saved.get("project_id") == pid:
                messages.add(str(saved.get("card_message_id") or ""))
        card = str((now.get("card") or {}).get("message_id") or "")
        for message in sorted(messages - {""}):
            if message == card:
                move_topic_message(state, back, chat, message)  # Its clicks too: the upgrade made the card the lead's.
            route_message(state, chat, message, back, holder=back)
    label = topic_label(state, back) if back else ""
    return (f"已把专项「{name}」降回普通话题：已请负责人的 {stopped} 个任务停下，{len(plain)} 个话题变回普通话题"
            + (f"；之后引用这张卡回复，会回到 {label or '原话题'}" if back else "；它没有话题，引用这张卡回复也没有话题可回") + "。")


def release(state, topic, pid) -> bool:
    """``topic`` is an ordinary topic again (its ``project`` field of project ``pid`` is gone); whether it is one now. A topic of
    another project, or whose topic.json is gone, is left as it is."""
    found = projects.topic_project(state, topic)
    if found is None:
        return True
    if found.id != pid:
        return False
    try:
        with topic_lock(topic_ctx(state, topic)):
            clear_topic_project(state, topic)
    except OSError:
        return False
    return True


STOP_ALIAS = "project-stop-"  # The Bridge's own message a forwarded stop of a project's task names: an alias of its topic.


def stop_task(host, ctx, topic, chat, job, event_id) -> None:
    """Ask ``job`` of ``topic`` to stop as a click on its card's 停止 does. A job in a live task runner is stopped there, by
    that click forwarded to the runner (app/runner_ipc.py forward_card_action, as app/butler_tracks.py and app/proxy.py
    stop_job do): the runner owns the job, and a stop set only in this Bridge's copy of it reaches nobody (accept8,
    2026-11-16: the threads' commands ran on to their end). The click names an alias of the topic's own (``STOP_ALIAS``),
    so the runner routes it to that topic whatever card the job has (a lead's quiet turn has none). Any other job (queued,
    or in this Bridge) is stopped here."""
    from covey.app import runner_ipc
    from covey.core.jobs import JOB_STOP_ACTION_KIND

    target = dataclasses.replace(ctx, topic_id=topic)
    message = STOP_ALIAS + str(topic)
    move_topic_message(ctx.state_dir, topic, chat, message)
    action = {"kind": JOB_STOP_ACTION_KIND, "job_id": str(job.job_id), "message_id": job.message_id}
    click = {"chat_id": chat, "message_id": message, "event_id": event_id,
             "operator_id": host.runtime.owner_open_id or "", "action_value": action}
    if runner_ipc.forward_card_action(host.runtime, target, click, action):
        host.runtime.log(f"project stop job=#{job.job_id} sent to its task runner")
        return
    host.cancel_reply_job_from_card(target, action, chat, event_id)


def stop_tasks(host, ctx, topics, chat, event_id) -> int:
    """Stop the tasks of ``topics`` that are queued or running (``stop_task``, the event id of each is ``event_id`` and its
    job number); how many."""
    stopped = 0
    for topic in topics:
        for job in host.runtime.jobs.open_in_topic(topic, chat):
            stop_task(host, ctx, topic, chat, job, f"{event_id}-{job.job_id}")
            stopped += 1
    return stopped


def stop_lead(host, ctx, lead, chat, pid) -> int:
    """Stop the lead's tasks that are queued or running (as a stop on a task's own card does, ``stop_task``); how many."""
    stopped = 0
    for job in host.runtime.jobs.open_in_topic(lead, chat):
        stop_task(host, ctx, lead, chat, job, f"project-downgrade-{pid}-{job.job_id}")
        stopped += 1
    return stopped


def click(ctx, event):
    """(the click's value, the project's record) of a click on a project card: the value's topic must be the one the click
    routed to (the lead's), and the project one that is there, in this chat."""
    from covey.app.card_actions import action_value

    raw = action_value(event)
    if raw.get("topic_id") != ctx.topic_id:
        raise ValueError("操作不属于当前话题")
    record = projects.read_record(ctx.state_dir, str(raw.get("project_id") or ""))
    chat = str(event.get("chat_id") or "")
    if not record or record.get("lead_topic_id") != ctx.topic_id or (chat and record.get("chat_id") != chat):
        raise ValueError("这个专项已经不在了")
    return raw, record


def say(host, ctx, event, text, name) -> bool:
    event_id = str(event.get("event_id") or event.get("message_id") or "")
    host.messenger.reply(ctx, str(event.get("chat_id") or ""), text, host.voice.key(name, event_id), voice=host.voice)
    return True


def steer_job(host, ctx, event) -> str:
    """补充: the job of the lead's that the supplement goes to (its running turn), '' when the lead has none."""
    from covey.app.dispatch_cards import click_job

    raw, record = click(ctx, event)
    return click_job(host.runtime, ctx.state_dir, ctx.topic_id, str(event.get("chat_id") or ""))


def stop_project(host, ctx, event) -> bool:
    """停止专项: the project is 停住 (``PAUSED``) and every task of its lead and its threads that is queued or running is
    stopped, as a stop on its own card would. A project that is not on says so."""
    raw, record = click(ctx, event)
    name = words(record.get("name"), 30) or "专项"
    def idle():
        return say(host, ctx, event, f"专项「{name}」已经是{'做完' if record.get('state') == projects.DONE else '停住'}的，没有要停的。",
                   "project-stop-idle")
    if record.get("state") != projects.ACTIVE:
        return idle()
    try:
        projects.set_state(ctx.state_dir, record["id"], projects.PAUSED, "主人停止")
    except ValueError:  # Another click or the lead moved it first.
        return idle()
    stopped = stop_tasks(host, ctx, [record["lead_topic_id"], *(record.get("threads") or ())], str(event.get("chat_id") or ""),
                         str(event.get("event_id") or event.get("message_id")))
    return say(host, ctx, event, f"已停住专项「{name}」，已请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停）。要继续，引用这张卡回复。", "project-stop")


ASK_REFUSED = {"none": "专项「{name}」现在没有要定的停住。", "answered": "专项「{name}」停住后的选择已经做过了。",
               "stale": "这是专项「{name}」之前一次停住的卡，已经过期了。",
               "moved": "专项「{name}」已经不是因为这个停住了，没有要定的。"}


def ask_choice(host, ctx, event) -> bool:
    """继续, 收尾 or 停 on the pause card (COVEY_PROJECT_ASK=1): the choice is kept in the record under its lock first
    (``project_loop.answer_pause``; ``ProjectCards`` then writes the card as one line and the project card follows the state).
    继续 and 收尾 leave the lead a receipt that wakes it; 停 stops the tasks of the lead and the member topics as 停止专项 does."""
    raw, record = click(ctx, event)
    choice = next((name for name, kind in ASK_KINDS.items() if kind == raw.get("kind")), "")
    if not choice:
        raise ValueError("不认识的按钮")
    pid, name = str(record["id"]), words(record.get("name"), 30) or "专项"
    outcome = project_loop.answer_pause(ctx.state_dir, pid, choice, str(event.get("message_id") or ""))
    if outcome != "ok":
        return say(host, ctx, event, ASK_REFUSED[outcome].format(name=name), "project-ask-" + outcome)
    ask = (projects.read_record(ctx.state_dir, pid) or {}).get("pause_ask") or {}
    if choice != "stop":
        project_loop.pause_receipt(ctx.state_dir, pid, choice, ask)
        return True
    stopped = stop_tasks(host, ctx, [record["lead_topic_id"], *(record.get("threads") or ())], str(event.get("chat_id") or ""),
                         str(event.get("event_id") or event.get("message_id")))
    return say(host, ctx, event, f"已停住专项「{name}」，已请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停）。要继续，引用这张卡回复。",
               "project-ask-stop")


def downgrade_project(host, ctx, event) -> bool:
    raw, record = click(ctx, event)
    return say(host, ctx, event, downgrade(host, ctx, record), "project-down")


def delete_memory(host, ctx, event) -> bool:
    """删 on a row of 专项记忆: the entry leaves projects/<id>/memory.json."""
    raw, record = click(ctx, event)
    path = projects.folder(ctx.state_dir) / record["id"] / "memory.json"
    entry = str(raw.get("entry") or "")
    with resource_lock("record", str(path)):
        data = _json(path)
        entries = data.get("entries") if isinstance(data, dict) else data
        entries = entries if isinstance(entries, list) else []
        kept = [row for index, row in enumerate(entries)
                if str(row.get("id") if isinstance(row, dict) and row.get("id") else index) != entry]
        if len(kept) == len(entries):
            return say(host, ctx, event, "这条记忆已经不在了。", "project-mem-gone")
        write_json(path, {**data, "entries": kept} if isinstance(data, dict) else kept, ensure_ascii=False, indent=1)
    return say(host, ctx, event, "已删掉这条专项记忆。", "project-mem-del")


def delete_library(host, ctx, event) -> bool:
    """删 on a row of 资料库: the file leaves projects/<id>/library/ (a plain name inside it, nothing else)."""
    raw, record = click(ctx, event)
    name = str(raw.get("name") or "")
    path = projects.folder(ctx.state_dir) / record["id"] / "library" / name
    if not name or "/" in name or name.startswith(".") or not path.is_file() or path.is_symlink():
        return say(host, ctx, event, "这份资料已经不在了。", "project-lib-gone")
    path.unlink()
    return say(host, ctx, event, f"已删掉资料「{words(name, 40)}」。", "project-lib-del")
