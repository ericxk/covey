"""The welcome card (docs/usage.md 第一次使用): sent once per new deployment to the owner's private chat, its examples
taken as his messages. It says what this machine has and how a message is answered; each of its three examples starts
a task as if the owner had typed it. Off: COVEY_WELCOME=0 (no record, no thread, nothing sent).

- Once (core/welcome.py ``settle``, at the Bridge's start before any event, app/unified.py main): a new deployment's
  record is pending; one with a job, a topic or the owner's private chat from before is skipped and never gets it.
- When: the ``welcome`` thread, while the record is pending, FIRST seconds after the start (the event consumers are up,
  the availability watch has looked once). To the owner's private chat when it is known (Access.owner_chat), else to
  his open_id: that message opens the chat, which is saved as the bot menu's first click saves it
  (Access.remember_owner_chat). A send Feishu refused is tried again: at once on the chat once the owner's first
  message names it (looked for every POLL seconds), else every RETRY seconds, TRIES times each way in one process.
  Rule 1: SENDING is written before the message goes out, outside the lock; a send that may have landed (no answer)
  is UNCERTAIN and never sent again. A click on it says it did: that card is taken as the one sent.
- What: ``view`` (cards/welcome.py): the Harnesses this Bridge enables and those its availability watch found
  unavailable, the default workspace (and, while it holds no project yet, how to add one: core/welcome.py
  ``bare_workspace``), and with the butler a word on its reactions; without it why not (Claude Code not
  enabled, COVEY_BUTLER=0, Claude Code not started) and how to get it, and that each message opens a
  topic with its launch card.
- The examples (``EXAMPLES`` with the butler, ``TOPIC_EXAMPLES`` without it: ``view`` picks by ``butler_state``, the card keeps
  the words it was sent with): a topic that ends in an answer, a topic whose reply is a choice (a question card), and
  what waits for him (the butler's snapshot and the 看板; without it an ordinary question, the card's grey line says 看板).
- A click on 发这句 (cards/welcome.py KIND, bot_only: it starts a task; the owner's alone, Access.is_owner): once per
  event (claim_once) and once per example (``tried``: a failed one may be clicked again). Acknowledged at once; then
  the card is drawn again with the example marked (beside), and the example goes into the chat as a plain text
  message of the bot's at the top level, which is taken as the owner's own message (``hear``: app/card_routing.py
  handle_event, the entry of a Feishu message: access, the duplicate check on its id, the extensions, the butler,
  else a new topic with its launch card in that message's thread). So it is answered as a typed one is: the butler's
  reactions on it and its topics under it, or the launch card. Its words are the example's alone, so
  app/source_watch.py reading the message back finds them unchanged.
"""
from __future__ import annotations

import hashlib
import json
import platform
import time

from covey.app import butler_harness
from covey.app.butler import HARNESS
from covey.app.card_lanes import after_ack
from covey.cards import welcome as cards
from covey.core import butler as contract
from covey.core import butler_settings
from covey.core import welcome
from covey.core.card_sequence import file_card, reserve
from covey.core.harness import backend_capabilities
from covey.core.process_env import env_flag
from covey.core.roster import NAMES
from covey.core.state import claim_once, utc_now_iso
from covey.core.workspaces import load_config
from covey.feishu.cardkit import extract_card_id
from covey.feishu.events import REPLY_CONTEXT
from covey.feishu.transport import last_failure

SWITCH = "COVEY_WELCOME"
FIRST = 10  # Seconds after the start before the first send.
POLL = 5  # How often a pending card looks for the owner's private chat (his first message saves it).
RETRY = 600  # Seconds between two sends Feishu refused the same way.
TRIES = 6  # Sends one process makes each way (the chat, the open_id); the next start tries again.
INBOUND = "inbound-messages"  # app/card_routing.py INBOUND_DIR: one marker per click taken in.
# Three read-only examples that take him round: a topic that ends in an answer, a topic whose natural reply is a choice
# (its question card; picking changes nothing), and what waits for him. With the butler (EXAMPLES): "开个话题，" makes
# the first two topics (it answers in place what takes a minute or less) and the third is its snapshot's and the 看板.
# Without it (TOPIC_EXAMPLES) every message is a topic: the third is an ordinary question, and the card's grey line
# points to the 看板.
OPEN = "开个话题，"
_TOPICS = ("把默认工作区过一遍，用三句话说清它是做什么的", "读一下默认工作区，列三个我可以先了解的部分，问我先看哪个")
EXAMPLES = (OPEN + _TOPICS[0], OPEN + _TOPICS[1], "现在有哪些事在等我？菜单「看板」怎么看")
TOPIC_EXAMPLES = (*_TOPICS, "Covey 能帮我做什么？举三个例子")
REASONS = {"authentication": "没登录", "executable_missing": "找不到命令"}  # app/availability.py local_check reasons.


def enabled() -> bool:
    return env_flag(SWITCH, True)


def harnesses(apps, runtime) -> tuple[list, list]:
    """(the titles of the Harnesses this Bridge enables, in the roster's order; [title, why] of those its availability
    watch found unavailable). Read here and now: no Feishu call."""
    names = [name for name in NAMES if name in apps]
    preflight = (getattr(runtime, "availability_health", None) or {}).get("preflight") or {}
    down = [[apps[name].spec.title, REASONS[check["reason"]]] for name in names for check in [preflight.get(name) or {}]
            if check.get("status") == "unavailable" and check.get("reason") in REASONS]
    return [apps[name].spec.title for name in names], down


def butler_state(apps, harness=HARNESS) -> str:
    """'' when the owner's top-level messages go to the butler now, else why not (cards/welcome.py BUTLER_WORDS);
    ``apps``: the Bridge's Harness apps by name; ``harness``: the butler's (app/butler_harness.py name_of: the 设置 card's
    choice, else Claude Code while it can run it, else the first enabled Harness that can). The 设置 card's 能做什么 says the
    same (app/about_panel.py)."""
    if not contract.available():
        return cards.OFF
    app = apps.get(harness)
    if app is None or not backend_capabilities(app.backend, app.harness.context).bridge_tools:
        return cards.NO_CLAUDE
    try:
        app.harness.refuse_while_unavailable()
    except RuntimeError:
        return cards.DOWN
    return ""


def drawn(record) -> dict:
    """The card's view as it is to be drawn now: the one it was sent with, the examples tried and the notice."""
    return {**(record.get("view") or {}), "tried": record.get("tried") or {}, "notice": record.get("notice") or ""}


class Welcome:
    """``hear(event, ctx)``: the Bridge's entry of a Feishu message (app/card_routing.py handle_event)."""

    def __init__(self, runtime, messenger, voice, topics, hear) -> None:
        self.runtime, self.messenger, self.voice, self.topics, self.hear = runtime, messenger, voice, topics, hear

    # The record and the thread ----------------------------------------------------------------------------------

    def settle(self, ctx) -> None:
        """At the Bridge's start, before any event: the record written once (core/welcome.py settle)."""
        if not enabled():
            return
        try:
            state = welcome.settle(ctx.state_dir)
        except OSError as exc:
            self.runtime.log(f"welcome card not settled: {type(exc).__name__}")
            return
        if state in (welcome.PENDING, welcome.UNCERTAIN):
            self.runtime.log(f"welcome card {state}")

    def threads(self):
        """The ``welcome`` thread while the card is still to be sent, unless COVEY_WELCOME=0 (listed after ``settle``)."""
        state = self.runtime.state_dir
        if not enabled() or state is None or welcome.read(state).get("state") != welcome.PENDING:
            return ()
        return (("welcome", self.run),)

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(FIRST):
            return
        tries, last, due = {"chat": 0, "user": 0}, "", 0.0
        while welcome.read(ctx.state_dir).get("state") == welcome.PENDING:
            chat = self.runtime.access.owner_chat()
            via = "chat" if chat else "user"
            if self.runtime.owner_open_id and tries[via] < TRIES and (via != last or time.monotonic() >= due):
                if self.send(ctx, chat) != welcome.PENDING:
                    break
                tries[via] += 1
                last, due = via, time.monotonic() + RETRY
            if self.runtime.shutdown.wait(POLL):
                return
        self.runtime.shutdown.wait()  # Sent, or never to be: supervised would only start it again.

    # Sending ---------------------------------------------------------------------------------------------------

    def send(self, ctx, chat) -> str:
        """One send of the card: to ``chat``, else to the owner's open_id. The record's state after it."""
        owner, state = self.runtime.owner_open_id or "", welcome.read(ctx.state_dir).get("state")
        if not owner or state != welcome.PENDING:
            return state or welcome.PENDING
        view = self.view(ctx)
        card_id = extract_card_id(self.messenger.create_card(ctx.profile, cards.welcome_card(view)) or {})
        if not card_id:
            self.runtime.log("welcome card not created")
            return welcome.PENDING
        via = "chat" if chat else "user"

        def intent(record):
            if record.get("state") != welcome.PENDING:
                return None
            return {**record, "state": welcome.SENDING, "via": via, "tries": int(record.get("tries") or 0) + 1,
                    "card_id": card_id, "view": view, "chat_id": chat, "sending_at": utc_now_iso()}
        record = welcome.update(ctx.state_dir, intent)
        if record.get("state") != welcome.SENDING or record.get("card_id") != card_id:
            return record.get("state") or welcome.PENDING
        key = self.voice.safe_key(f"welcome-{via}-{record['tries']}-{record.get('decided_at') or ''}")
        if chat:
            message_id, _ = self.messenger.send_card(ctx.profile, chat, card_id, key, voice=self.voice)
        else:
            message_id, chat = self.messenger.send_card_to_user(ctx.profile, owner, card_id, key)
        failure = last_failure()
        if message_id and not chat:
            chat = self.chat_of(ctx, message_id)
        if message_id:
            state, fields = welcome.SENT, {"message_id": message_id, "chat_id": chat, "sequence": 0, "sent_at": utc_now_iso()}
        elif failure is not None and not failure.ambiguous:
            state, fields = welcome.PENDING, {"card_id": ""}  # Refused: it did not land, the next send makes a new card.
        else:
            state, fields = welcome.UNCERTAIN, {"uncertain_at": utc_now_iso()}
        welcome.update(ctx.state_dir, lambda saved: {**saved, "state": state, **fields})
        if message_id and chat:
            self.runtime.access.remember_owner_chat(chat)
        why = f" category={failure.category} code={failure.code or '-'}" if failure is not None and not message_id else ""
        self.runtime.log(f"welcome card {state} via={via} try={record['tries']}{why}")
        return state

    def chat_of(self, ctx, message_id) -> str:
        """The chat of the message ``message_id`` the bot sent, read back; '' when unread (app/bot_menu.py
        open_owner_chat does the same)."""
        from covey.feishu.message_read import fetch_message_detail, message_item
        item = message_item(fetch_message_detail(message_id, ctx.profile), message_id) or {}
        return str(item.get("chat_id") or "")

    def view(self, ctx) -> dict:
        """What the card shows (cards/welcome.py), read here and now: no Feishu call."""
        titles, down = harnesses(self.topics.apps, self.runtime)
        try:
            config = load_config(ctx.state_dir, ctx.default_workdir)
            workspace = str(config["default"])
            workdir = str(config["workspaces"][workspace])
        except (OSError, ValueError, KeyError, TypeError):
            workspace = workdir = ""
        butler = self.butler_state(ctx.state_dir)
        return {"host": platform.node(), "harnesses": titles, "down": down,
                "workspace": workspace, "workdir": workdir, "butler": butler,
                "examples": list(TOPIC_EXAMPLES if butler else EXAMPLES),
                **({"bare_workspace": True} if workdir and welcome.bare_workspace(workdir) else {})}

    def butler_state(self, state_dir) -> str:
        """'' when the owner's top-level messages go to the butler now, else why not (``butler_state``, on the
        Harness it runs on: app/butler_harness.py)."""
        return butler_state(self.topics.apps, butler_harness.name_of(butler_settings.load(state_dir), self.topics.apps))

    # A click ---------------------------------------------------------------------------------------------------

    def matches(self, raw) -> bool:
        return isinstance(raw, dict) and raw.get("kind") == cards.KIND

    def claim(self, ctx, event, raw, owner):
        """A click on 发这句 (app/unified.py PANELS), else None: the owner's only; a click delivered again is a
        ``duplicate``, one on a card other than the one sent ``welcome_stale``, one on an example tried already
        ``welcome_again``; nothing is sent for those."""
        if not self.matches(raw):
            return None
        if not owner:
            return "owner_only"
        event_id = str(event.get("event_id") or "")
        if not event_id or not claim_once(ctx.state_dir / INBOUND,
                                          hashlib.sha256(("welcome:" + event_id).encode()).hexdigest()):
            return "duplicate"
        message, chat = str(event.get("message_id") or ""), str(event.get("chat_id") or "")
        try:
            index = int(raw.get("example"))
        except (TypeError, ValueError):
            index = -1
        found = {"outcome": "welcome_stale"}

        def mark(record):
            if record.get("state") == welcome.UNCERTAIN and record.get("card_id") and not record.get("message_id") and message:
                # The send that may have landed did: a refused one never did, so no other card carries this kind.
                record = {**record, "state": welcome.SENT, "message_id": message, "sequence": int(record.get("sequence") or 0)}
            examples = (record.get("view") or {}).get("examples") or []
            if record.get("state") != welcome.SENT or not message or record.get("message_id") != message \
                    or not 0 <= index < len(examples):
                return None
            tried = dict(record.get("tried") or {})
            if (tried.get(str(index)) or {}).get("state") in cards.TRIED:
                found["outcome"] = "welcome_again"
                return None
            tried[str(index)] = {"state": cards.STARTING, "event": event_id[-40:], "at": utc_now_iso()}
            found.update(outcome="welcome_try", text=str(examples[index]), chat=str(record.get("chat_id") or chat))
            return {**record, "tried": tried, "notice": "", "chat_id": record.get("chat_id") or chat}
        welcome.update(ctx.state_dir, mark)
        if found["outcome"] == "welcome_try":
            after_ack(lambda: self.redraw(ctx), beside=True)
            after_ack(lambda: self.start(ctx, index, found["text"], found["chat"], event_id))
        return found["outcome"]

    def start(self, ctx, index, text, chat, event_id) -> None:
        """The example ``text`` into the chat as a text message of the bot's, taken as the owner's own (``hear``); what
        came of it on the record, and the card drawn again when it did not go out."""
        message_id = self.messenger.send_text(ctx.profile, chat, text, self.voice.safe_key(f"welcome-try-{event_id}"))
        failure = last_failure()
        state = (cards.SENT if message_id else cards.FAILED if failure is not None and not failure.ambiguous
                 else cards.UNCERTAIN)
        notice = {cards.FAILED: f"「{text}」没发出去，再点一次。",
                  cards.UNCERTAIN: f"「{text}」可能没发出去：聊天里没看到的话，直接打这句话发出来。"}.get(state, "")

        def done(record):
            tried = dict(record.get("tried") or {})
            tried[str(index)] = {**(tried.get(str(index)) or {}), "state": state,
                                 **({"message_id": message_id} if message_id else {})}
            return {**record, "tried": tried, **({"notice": notice} if notice else {})}
        welcome.update(ctx.state_dir, done)
        self.runtime.log(f"welcome example {index} {state}")
        if not message_id:
            self.redraw(ctx)
            return
        event = {"message_id": message_id, "chat_id": chat, "chat_type": "p2p", "sender_id": self.runtime.owner_open_id or "",
                 "sender_type": "user", "message_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False),
                 REPLY_CONTEXT: True, "create_time": str(int(time.time() * 1000))}
        try:
            self.hear(event, ctx)
        except Exception as exc:  # noqa: BLE001 - the message is in the chat; typed again it is answered
            self.runtime.log(f"welcome example {index} not taken in: {type(exc).__name__}")

    def redraw(self, ctx) -> bool:
        """The card drawn again from its record: one CardKit write, its sequence reserved from the record under the
        card's lock (rule 3), the record's lock held only for that. Whether CardKit confirmed it."""
        state = ctx.state_dir
        record = welcome.read(state)
        if record.get("state") != welcome.SENT or not record.get("card_id"):
            return False
        path = welcome.path(state)
        with file_card(path, record, shared=False).lock:
            with welcome.lock(state):
                record = welcome.read(state)
                sequence = reserve(file_card(path, record, shared=False))
            result = self.messenger.put_card(ctx.profile, record["card_id"], cards.welcome_card(drawn(record)),
                                             sequence=sequence, key=f"welcome-{record['card_id'][-16:]}-{sequence}",
                                             timeout=10)
        self.runtime.log(f"welcome card redrawn ok={result is not None}")
        return result is not None
