"""专项卡 (docs/projects.md 专项卡), when COVEY_PROJECTS=1: the one card of a project at the chat's top level, a message of its
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
- A project folded away for being idle (state_reason ``idle``, app/project_loop.py ``rest``) is drawn as one line (``fold``, 收起 · N 天没有新的事),
  before the 14 days are looked at; the card is not closed, so once the project is active again the next look draws it whole.
- A finished project the owner speaks in again (app/project_loop.py ``unfinish``) gets its card back: ``unfinish`` clears
  ``closed`` and ``digest``, so the next look draws the one line whole again in place, with the next sequence.
- With COVEY_PROJECT_ASK=1 it also sends and settles the pause card (``ask``): a project a guard paused (``pause_ask`` in its
  record, app/project_loop.py) gets a card of its own where the project's card is, with 继续 / 收尾 / 停; once the owner chose
  (app/project_clicks.py ``ask_choice`` records it) or the project moved on some other way, that card is written once more as one line.
- What the card says is read by app/project_facts.py (``facts``), what its buttons run is app/project_clicks.py (app/card_actions.py:
  ``controls.project_stop`` etc.).

The record fields this reads and nobody here writes (later packages fill them; an empty one is not drawn): ``lead_note`` ({text, at} or a string; P2-2), ``tokens_used`` (an int; P2-2), ``conclusion_message_id`` (the
lead's report on the project's end; P2-2), ``triage`` (a list of lines or {at, text} rows; P2-4's 分流 package),
``projects/<id>/memory.json`` (core/project_memory.py: {"entries": [...]}, or a list, of strings or {id, text}) and ``projects/<id>/library/``
(its files' names)."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app.card_publish import digest_of, publish, safely, titles_of
from covey.app.project_facts import facts, pending_question
from covey.cards.project_card import ask_card, asked_card, idle_card, moved_card, project_card, settled_card
from covey.core import projects
from covey.core.card_sequence import CardHandle, reserve
from covey.core.jobs import job_path, read_job_record
from covey.core.statuses import ENDED
from covey.core.loops import every
from covey.core.topics import remember_topic_message, route_message


TICK = 1.0  # Seconds between two looks at the live records.
MIN_GAP = 2.5  # Seconds between two writes of one card (CardKit allows 10 a second).
BACKOFF = 60.0  # The longest wait after a failed write (2, 4, 8 ... seconds).
LINK_RETRY = 30.0  # Seconds before a link that could not be read is tried again (one read of Feishu per tick at most).
FLIP_AFTER = 12 * 86400  # A card this old is sent again: CardKit stops taking writes at 14 days.
MOVE_TRIES = 3  # Writes of "已移到新卡" tried on the old card before it is recalled.
CONCLUSION_GRACE = 600.0  # A project done this long without a conclusion message is shrunk without its link.
ANSWER_WAIT = 30.0  # How long after its finishing turn ended the card waits for that turn's answer id (written a moment later).
CLOSED_WORDS = {"stopped": "专项已停住", "downgraded": "专项已降为普通话题", "merged": "专项已并入另一个专项"}


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
    return safely(env, lambda: _created(env, record), f"project card failed project={str(record.get('id'))[:8]}")


def _created(env, record) -> str:
    state, pid = Path(env.state_dir), str(record["id"])
    saved = projects.read_record(state, pid) or record
    if projects.card_message(saved):
        return projects.card_message(saved)
    card = project_card(facts(state, saved, time.time(), titles_of(env.topics)))
    pending = (saved.get("card") or {}).get("pending_card_id") or ""

    def store(card_id, message, digest):
        projects.update_card(state, pid, card_id=card_id, message_id=message, sequence=0, created_at=time.time(),
                             digest=digest, wrote_at=time.time(), pending_card_id=None, closed=None)
    return publish(
        env, state, saved["chat_id"], card, env.voice.key("project-card", pid + "-0"), card_id=pending,
        on_created=lambda cid: projects.update_card(state, pid, pending_card_id=cid), record=store,
        topic=str(saved.get("lead_topic_id") or ""), route_chat=str(saved.get("chat_id") or ""),
        not_sent=f"project card not sent project={pid[:8]}", sent=f"project card sent project={pid[:8]} message=")


def _route(state, record, message) -> None:
    """A click on the card and a quote of it go to the project's lead (``route_message``)."""
    lead, chat = str(record.get("lead_topic_id") or ""), str(record.get("chat_id") or "")
    remember_topic_message(state, lead, chat, message)
    route_message(state, chat, message, lead, holder=lead)


def finishing_answer(state_dir, record) -> tuple[str, bool]:
    """(message id, ended) of the lead's turn that finished the project (``state_job``, app/project_tools.py
    project_finish): the answer it sent at the chat's top level, once that turn has ended, and whether it has ended. A turn
    that ended with no answer of its own (a quiet wake-up turn's reply is not sent) gives ('', True): the card shrinks at
    once instead of waiting CONCLUSION_GRACE for a message that never comes (10 minutes)."""
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
        seen["done_seen"] = 0.0  # Opened again after a finish (app/project_loop.py unfinish): the next finish waits its own grace.
        seen.pop("answer_wait", None)
        if state == projects.CANCELLED:
            return self.settle(ctx, record, card, seen, now, reason=CLOSED_WORDS[
                projects.MERGED if record.get("state_reason") == projects.MERGED else projects.DOWNGRADED])
        if projects.folded(record):
            return self.fold(ctx, record, card, seen, now)
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

    def fold(self, ctx, record, card, seen, now) -> None:
        """A project folded away for being idle (``projects.folded``): its card is one line, written once (a card that is not changed
        is not written again); the card is not closed, so the next look after the project opens again draws it whole."""
        idle = record.get("idle") if isinstance(record.get("idle"), dict) else {}
        days = idle.get("days") if isinstance(idle.get("days"), (int, float)) and not isinstance(idle.get("days"), bool) \
            and idle["days"] > 0 else projects.idle_days()
        built = idle_card(record.get("name"), days)
        digest = digest_of(built)
        if digest == seen["digest"] or now - seen["wrote"] < MIN_GAP:
            return
        if self.write(ctx, record, card, seen, built, now):
            projects.update_card(ctx.state_dir, str(record["id"]), digest=digest, wrote_at=now)
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
        built = settled_card(record.get("name"), url=url, reason=reason)
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
        built = moved_card(record.get("name"), url)

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
