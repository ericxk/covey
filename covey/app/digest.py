"""早晚汇总: at the times of day COVEY_DIGEST_TIMES lists (HH:MM, comma separated, Asia/Shanghai), one card
at the top level of the owner's private chat listing what waits for the owner, what runs and what ended since the
digest before (cards/digest.py). Off unless the variable holds a valid time (docs/operations.md 早晚汇总): without
it there is no thread.

What it lists is read from the records, as the pinned board reads them (core/board_read.py read: the indexes of
questions/ and jobs/, no record lock, no Feishu call, the butler's topics left out), never from a model:
- 等你: each topic's newest pending question asked within pins.QUESTION_AGE (one day, as 手上的事 and the board
  have it), and the tasks waiting to be started (waiting_workspace, never old: 手上的事 has no such rule for them);
  the newest questions asked before that are not listed one by one but as one line of topic numbers (``stale``),
  as on 手上的事;
- 在做: each other topic's running job (board_read.order), the latest started first;
- 做完: each other topic's latest turn (not one cancelled before it ran: board_read.unrun) that ended after the
  digest before (``since``) and by this one's time, and every result the owner has not seen however old (有结果未看,
  pins.board_results: the board's rule, core/board_read.py unseen; ``older`` counts those that ended before
  ``since``), the latest first; one not 有结果未看 is marked 已看.
With 手上的事 and the board on (overview.dismissable), the topics the owner took off the board (pins.taken_off: the
board's own reading of its record) are in none of it, and the line of old questions has 手上的事's button
这些都不用回答了 (cards/overview.py stale_line, its value's ``from`` "digest"). Its click is 手上的事's
(app/overview.py claim: the owner's only, the topics taken off, 手上的事 redrawn), which hands it on once
acknowledged (Overview.handed, app/unified.py): ``dismissed`` redraws this card from the view saved when it was
sent, the line turned grey, 已撤下 N 个, no button (Messenger.patch_card, outside the lock). A card sent before views
were saved is left as it is. All empty, the line of old questions too, nothing is sent (the time counts as done);
only that line, the card is sent: it is what the owner has to deal with.

<state>/digest.json keeps the last time sent (``last_slot``, epoch seconds), the send under way (``pending``:
the time, its view, card and Markdown, its idempotency key, tries) and the last SENT_KEPT cards sent (``sent``:
message id -> {slot, view}). A time is sent once (``due``): while it is the
latest past, not sent yet and at most CATCH_UP seconds ago, so a Bridge stopped over the time and back within the
hour sends it once, later it is skipped. The card is built and saved before it is sent, outside the lock (rule 1);
a send not confirmed is tried again RETRY seconds later with the same key and card, at most SEND_TRIES times
(Feishu's idempotency window and feishu/transport.py's receipts post nothing twice), then given up.

看板 (the bot menu's BOARD_KEY, only with a time set: a deployment without the digest has no such item) is the same
card at any time, 随时查看 · HH:MM in its title (the view's ``asked``): what waits and runs now, and what ended since the
last digest time (``last_slot``, ``board_since``). It reads digest.json and never writes it: the times and their
sends go on as before. Nothing to list, the card says so (a click wants an answer). Its record is
<state>/BOARD_FOLDER/<token>.json (app/menu_panel.py), the view in ``digest``; the bot menu keeps one such card and a
second click on the newest folds and unfolds it in place (app/bot_menu.py), unfolded with a fresh view. Its
这些都不用回答了 is handed on as the timed card's (``dismissed``), the card redrawn from the record's view.
"""
from __future__ import annotations

import datetime
import re
import time

from covey.core import env as cenv
from covey.app.overview import dismissable
from covey.app.pins import QUESTION_AGE, board_results, kept, laid, record_path
from covey.app.topic_links import linkify
from covey.app.form_cards import settle
from covey.app.menu_panel import MenuPanel
from covey.cards.digest import ORIGIN, digest_card, digest_text, folded_board
from covey.core import board_read, knowledge, patrol, projects, turn_words
from covey.core.board_read import ended_at, order, started_at, unrun
from covey.core.locks import resource_lock
from covey.core.loops import every
from covey.core.metrics import run_seconds, took_text
from covey.core.state import read_object, write_json
from covey.core.statuses import RUNNING, TERMINAL
from covey.core.topics import label_title

ENV = "COVEY_DIGEST_TIMES"
RECORD = "digest.json"
ZONE = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Shanghai, no summer time; no tz database needed.
TICK = 30
CATCH_UP = 3600
RETRY = 60
SEND_TRIES = 3
SENT_KEPT = 6  # Cards whose view is kept for 这些都不用回答了: three days at two a day.
LOOKBACK = 3 * 86400  # 做完 reaches back this far at most (board_read.read reads jobs written within it).
TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
BOARD_KEY = "digest_board"  # The bot menu's 看板 (docs/usage.md 机器人菜单).
BOARD_KIND = "digest-board"
BOARD_FOLDER = "digest-boards"  # Its cards' records (app/menu_panel.py).
BOARD_FAILED = "看板没能发出去，请稍后再点机器人菜单「看板」。"
BOARD_SCANNED = 20  # The newest board records ``dismissed`` looks a card up in.


def parse_times(raw, log=lambda _: None):
    """The minutes after midnight that ``raw`` lists, sorted and each once; a part that is not HH:MM is logged."""
    minutes = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        match = TIME.fullmatch(part)
        if match is None:
            log(f"digest time ignored: {part!r} is not HH:MM")
            continue
        minutes.add(int(match.group(1)) * 60 + int(match.group(2)))
    return sorted(minutes)


def slots(times, now):
    """The digest times (epoch seconds) at or before ``now``, the latest last, over the last three days."""
    today = datetime.datetime.fromtimestamp(now, ZONE).date()
    found = []
    for back in (3, 2, 1, 0):
        day = today - datetime.timedelta(days=back)
        for minute in times:
            at = datetime.datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=ZONE).timestamp()
            if at <= now:
                found.append(at)
    return found


def due(times, now, last):
    """The time to send now, or None: the latest past one, if not sent (``last``) and at most CATCH_UP ago."""
    past = slots(times, now)
    if not past:
        return None
    slot = past[-1]
    if isinstance(last, (int, float)) and last >= slot:
        return None
    return slot if now - slot <= CATCH_UP else None


def since_of(times, slot, last):
    """Where 做完 starts for ``slot``: the digest time before it, or the last one sent when that is earlier (times
    were missed), never more than LOOKBACK back."""
    before = [at for at in slots(times, slot) if at < slot]
    start = before[-1] if before else slot - 86400
    if isinstance(last, (int, float)) and last < start:
        start = last
    return max(start, slot - LOOKBACK)


def board_since(times, now, last):
    """Where 看板's 做完 starts at ``now``: the last digest time done (``last``, digest.json's last_slot), else the latest
    past time, never more than LOOKBACK back."""
    past = slots(times, now)
    start = last if isinstance(last, (int, float)) else past[-1] if past else now - 86400
    return max(start, now - LOOKBACK)


def topic_label(state, topic_id):
    """(T number, the topic's title), '' for a topic without a number yet."""
    return label_title(state, topic_id, "（话题未知）")


def collect(state, chat, slot, since, now, *, board=False):
    """The three parts' rows for ``chat`` (``view`` of cards/digest.py without links): {label, title, state,
    took, seen, topic}, ``older`` (the 做完 rows of results not seen that ended before ``since``), and ``stale``,
    the topics whose newest question waits over QUESTION_AGE; ``board``: 手上的事
    and the board are on (overview.dismissable), the topics taken off left out and the stale line's button shown."""
    # One read of the indexes (core/board_read.py): with the board, the one its look judges too; the jobs judged
    # here are those read without the board's ``keep``.
    read = board_read.read(state, chat, now, keep=kept(state, chat) if laid() else ())
    unseen, off = board_results(state, chat, now, read)  # 有结果未看, as the board has it (core/board_read.py).
    off = off if board else {}
    questions = [record for record in read.questions if record.get("topic_id") not in off]
    jobs = [record for record in read.jobs if board_read.fresh(record, now) and record.get("topic_id") not in off]
    asked, stale = {}, []
    for record in questions:
        topic = record.get("topic_id") or ""
        if topic and (record.get("created_at") or 0) > (asked.get(topic, {}).get("created_at") or -1):
            asked[topic] = record
    for topic, record in sorted(asked.items(), key=lambda item: item[1].get("created_at") or 0, reverse=True):
        if now - (record.get("created_at") or now) > QUESTION_AGE:  # As pins.board and Overview.board split them.
            label, title = topic_label(state, topic)
            stale.append({"topic": topic, "label": label, "title": title})
    asked = {topic: record for topic, record in asked.items() if topic not in {row["topic"] for row in stale}}

    def row(topic, state_words, took, seen_it=False):
        label, title = topic_label(state, topic)
        return {"topic": topic, "label": label, "title": title, "state": state_words, "took": took, "seen": seen_it}

    waiting = []
    for topic, record in sorted(asked.items(), key=lambda item: item[1].get("created_at") or 0, reverse=True):
        ask = turn_words.ask_words(record)
        waited = now - (record.get("created_at") or now)
        waiting.append(row(topic, f"{turn_words.BOARD['waiting']}：{ask}" if ask else turn_words.BOARD["waiting"],
                           f"等了 {took_text(waited)}"))
    starting = {}
    for record in jobs:
        topic = record.get("topic_id") or ""
        if topic and topic not in asked and record.get("status") == "waiting_workspace":
            starting.setdefault(topic, record)  # Newest first already.
    for topic, record in starting.items():
        waiting.append(row(topic, turn_words.DIGEST_START, f"等了 {took_text(now - started_at(record))}"))
    busy = set(asked) | set(starting)

    running = {}
    for record in jobs:
        topic = record.get("topic_id") or ""
        if topic and topic not in busy and record.get("status") in RUNNING and (
                topic not in running or order(record) > order(running[topic])):
            running[topic] = record
    running_rows = []
    for topic, record in sorted(running.items(), key=lambda item: started_at(item[1]), reverse=True):
        phase = str(record.get("phase") or "")
        status = record.get("status")
        if status == "cancel_requested":
            words = "正在停止"
        elif phase and phase not in turn_words.UNSET_PHASES:
            words = phase
        else:
            words = turn_words.BOARD["queued" if status == "queued" else "running"]
        ran = run_seconds(record.get("timings"), now=now) if status != "queued" else None
        running_rows.append(row(topic, words, f"已运行 {took_text(ran)}" if ran is not None else ""))
    busy |= set(running)

    ended = {}
    for record in jobs:
        topic = record.get("topic_id") or ""
        at = ended_at(record)
        if (topic and topic not in busy and record.get("status") in TERMINAL and not unrun(record)
                and since < at <= slot and int(record.get("job_id") or 0) > int(ended.get(topic, {}).get("job_id") or 0)):
            ended[topic] = record
    # Every result the owner has not seen is listed, however long ago it ended (the board's 有结果未看: its own
    # record, the one ``unseen`` has); the others only when they ended since the digest before. 已看: not unseen.
    unseen = {topic: record for topic, record in unseen.items() if ended_at(record) <= slot}  # As of its time.
    older = sum(ended_at(record) <= since for record in unseen.values())
    ended.update(unseen)
    done = []
    for topic, record in sorted(ended.items(), key=lambda item: ended_at(item[1]), reverse=True):
        took = run_seconds(record.get("timings"), now=ended_at(record))
        done.append(row(topic, turn_words.STATES.get(record["status"], record["status"]),
                        f"用时 {took_text(took)}" if took is not None else "", topic not in unseen))
    view = {"slot": slot, "since": since, "waiting": waiting, "stale": stale, "running": running_rows, "done": done,
            "older": older, "dismissable": bool(board and stale)}
    new, wanted = knowledge_view(state, since, slot), wanted_view(state, chat, since, slot)
    return {**view, **({"knowledge": new} if new else {}), **({"wanted": wanted} if wanted else {})}


KNOWLEDGE_SHOWN = 10  # Rows of 新记的知识; the rest is one line.
KNOWLEDGE_TEXT = 40  # Characters of an entry's text on its row.
KNOWLEDGE_OPS = {"add": "新增", "update": "改写", "drop": "作废"}


def knowledge_view(state, since, slot):
    """The 新记的知识 part (core/knowledge.py changes, COVEY_KNOWLEDGE=1): {"rows": [{op, id, where, text}],
    "more"} of the entries added, rewritten or dropped after ``since`` up to ``slot``, the newest first; {} when
    there are none (or the entries are off): the card then has no such part. It never makes a card be sent."""
    if not knowledge.enabled():
        return {}
    found = knowledge.changes(state, since, slot)
    rows = [{"op": KNOWLEDGE_OPS[change["op"]], "id": change["id"],
             "where": "通用" if change["scope"] == knowledge.OWNER else change["workspace"],
             "text": change["text"][:KNOWLEDGE_TEXT] + ("…" if len(change["text"]) > KNOWLEDGE_TEXT else "")}
            for change in found[:KNOWLEDGE_SHOWN]]
    return {"rows": rows, "more": len(found) - len(rows)} if rows else {}


WANTED_SHOWN = 5  # Rows of 我本来想找你的事; the rest is left out.


def wanted_view(state, chat, since, slot):
    """The 我本来想找你的事 part (core/patrol.py decisions, COVEY_PATROL=shadow): rows {what, why, would} of what the butler
    would have acted on in ``chat`` (verdict ``handle``) decided after ``since`` up to ``slot``, the newest first, an
    object once, at most WANTED_SHOWN; [] when there are none (or the patrol is off): the card then has no such part. It
    never makes a card be sent."""
    if not patrol.enabled():
        return []
    rows, seen = [], set()
    for row in reversed(patrol.rows(state, "decisions", since, slot)):
        if row.get("verdict") != "handle" or row.get("chat") != chat or row.get("key") in seen:
            continue
        seen.add(row.get("key"))
        would = patrol.ACTION_WORDS.get(row.get("action"), patrol.ACTION_WORDS["none"])
        name = str(projects.read_record(state, row["project"]).get("name") or "") if row.get("action") == "join" and row.get("project") else ""
        rows.append({"what": str(row.get("what") or ""), "why": str(row.get("why") or ""),
                     "would": f"{would}「{name}」" if name else would})
    return rows[:WANTED_SHOWN]


def cleared(view, topics):
    """``view`` once 这些都不用回答了 took ``topics`` off: their stale rows gone, their labels under ``cleared``;
    None when none of its stale rows is theirs (the card says so already)."""
    taken = [row for row in view.get("stale") or [] if row.get("topic") in topics]
    if not taken:
        return None
    return {**view, "stale": [row for row in view.get("stale") or [] if row not in taken],
            "cleared": [*(view.get("cleared") or []), *(row.get("label") or "" for row in taken)]}


class Digest:
    """``chat``: the chat it is sent to, '' the owner's private chat (Access.owner_chat); a personal group has one of
    its own (app/group_boards.py), its record its own (app/pins.py record_path) and
    ``name`` its thread's."""
    origin = ORIGIN  # What its card's 这些都不用回答了 says it is from (Overview.handed's key).

    def __init__(self, runtime, messenger, voice, *, chat="", name="digest") -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.fixed, self.name = chat, name
        self.link = lambda ctx, chat, text: linkify(text, ctx.state_dir, chat, profile=ctx.profile)
        self.menu = MenuPanel(self, BOARD_KIND, BOARD_FOLDER, BOARD_KIND)

    def menu_items(self):
        """The bot menu's 看板, only with a digest time set (app/unified.py menu)."""
        return {BOARD_KEY: self.board} if self.times() else {}

    def menu_folds(self):
        """A second 看板 on its card, the newest in the chat (app/bot_menu.py)."""
        return {BOARD_KEY: self.fold_board} if self.times() else {}

    def board_view(self, ctx, chat, now=None):
        """看板's view now (``asked``): ``chat``'s digest.json only read. Feishu calls (the T links), so outside any
        lock."""
        now = time.time() if now is None else now
        last = read_object(record_path(ctx.state_dir, chat, RECORD)).get("last_slot")
        view = collect(ctx.state_dir, chat, now, board_since(self.times(), now, last), now, board=dismissable())
        for key in ("waiting", "stale", "running", "done"):
            self.linked(ctx, chat, view[key])
        return {**view, "asked": True, **self.where(chat)}

    def where(self, chat):
        """The card's word for ``chat`` when it is a group with a butler (cards/digest.py), else nothing."""
        return {"where": "群"} if chat in getattr(self.runtime.access, "agent_groups", ()) else {}

    def path(self, ctx):
        return record_path(ctx.state_dir, self.fixed, RECORD)

    def board(self, ctx, chat_id, source, shown=None):
        """The bot menu's 看板: a new card in ``chat_id`` (the owner's private chat) for the click ``source``."""
        self.menu.open(ctx, chat_id, source, lambda token, record: digest_card(record["digest"]), BOARD_FAILED,
                       fields=lambda: {"digest": self.board_view(ctx, chat_id)}, shown=shown)
        self.runtime.log(f"digest board sent event={source[-10:]}")

    def fold_board(self, ctx, chat_id, source, message_id):
        """看板 again on its card, the newest in the chat: folded, or unfolded with a fresh view (MenuPanel toggle)."""
        return self.menu.toggle(ctx, chat_id, source, message_id, lambda token, record: digest_card(record["digest"]),
                                lambda token, record: folded_board(),
                                fields=lambda: {"digest": self.board_view(ctx, chat_id)})

    def board_dismissed(self, ctx, message_id, topics):
        """``dismissed`` on a 看板 card: its record's view redrawn with those topics taken off; whether it was one."""
        folder = self.menu.directory(ctx)
        try:
            paths = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime)[-BOARD_SCANNED:]
        except OSError:
            return False
        for path in reversed(paths):
            if read_object(path).get("message_id") != message_id:
                continue
            with self.menu.lock(path):
                record = read_object(path)
                view = cleared(record.get("digest"), set(topics)) if isinstance(record.get("digest"), dict) else None
                if view is None or record.get("message_id") != message_id or not isinstance(
                        (record.get("view") or {}).get("revision"), int):
                    return True
                changed = {**record, "digest": view, "view": {**record["view"], "revision": record["view"]["revision"] + 1}}
                confirmed = settle(ctx, path, record, changed, digest_card(view), self.messenger, BOARD_KIND)
            self.runtime.log(f"digest board redrawn message={message_id[-8:]} confirmed={confirmed}")
            return True
        return False

    def times(self):
        raw = cenv.get(ENV, "").strip()
        return parse_times(raw, self.runtime.log) if raw else []

    def threads(self):
        """Its worker, only when COVEY_DIGEST_TIMES lists a time (app/unified.py worker_targets)."""
        times = self.times()
        return ((self.name, lambda ctx: every(self.runtime, TICK, self.tick, args=(ctx, times), isolate="digest")),
                ) if times else ()

    def save(self, ctx, **fields):
        path = self.path(ctx)
        with resource_lock("digest", str(path)):
            saved = read_object(path)
            write_json(path, {**saved, "schema_version": 1, **fields}, ensure_ascii=False)

    def tick(self, ctx, times, now=None):
        now = time.time() if now is None else now
        record = read_object(self.path(ctx))
        last = record.get("last_slot")
        slot = due(times, now, last)
        if slot is None:
            return
        pending = record.get("pending") if isinstance(record.get("pending"), dict) else {}
        if pending.get("slot") == slot:
            if now - float(pending.get("tried_at") or 0) >= RETRY:
                self.send(ctx, slot, pending, now)
            return
        chat = self.fixed or self.runtime.access.notify_chat()
        if not chat:
            return  # No private chat with the owner known yet: tried again until the time is CATCH_UP old.
        view = {**collect(ctx.state_dir, chat, slot, since_of(times, slot, last), now, board=dismissable()),
                **self.where(chat)}
        if not (view["waiting"] or view["stale"] or view["running"] or view["done"]):
            self.save(ctx, last_slot=slot, pending=None)
            self.runtime.log("digest not sent: nothing to list")
            return
        for key in ("waiting", "stale", "running", "done"):
            self.linked(ctx, chat, view[key])
        named = f"{self.fixed}-{int(slot)}" if self.fixed else str(int(slot))  # A group's send is its own.
        pending = {"slot": slot, "chat_id": chat, "key": self.voice.safe_key(self.voice.key("digest", named)),
                   "view": view, "card": digest_card(view), "text": digest_text(view), "tries": 0, "tried_at": 0}
        self.save(ctx, pending=pending)
        self.send(ctx, slot, pending, now)

    def linked(self, ctx, chat, rows):
        """Each row's T number as a link to its topic, where one is found (app/topic_links.py)."""
        labels = [row["label"] for row in rows if row["label"]]
        if not labels:
            return
        done = iter(self.link(ctx, chat, "\n".join(labels)).split("\n"))
        for row in rows:
            if row["label"]:
                row["label"] = next(done, row["label"])

    def send(self, ctx, slot, pending, now):
        pending = {**pending, "tries": int(pending.get("tries") or 0) + 1, "tried_at": now}
        self.save(ctx, pending=pending)  # The try counts even if the process ends during it.
        cards = []
        if self.messenger.send_message(ctx.profile, pending["chat_id"], pending["text"], pending["key"],
                                       voice=self.voice, card=pending["card"], on_card=cards.append):
            message = cards[0] if cards else ""
            self.save(ctx, last_slot=slot, sent_at=time.time(), message_id=message, pending=None,
                      sent=self.kept(ctx, message, slot, pending.get("view")))
            self.runtime.log(f"digest sent slot={time.strftime('%H:%M', time.localtime(slot))} "
                             f"card={'yes' if cards else 'no'}")
        elif pending["tries"] >= SEND_TRIES:
            self.save(ctx, last_slot=slot, pending=None)
            self.runtime.log(f"digest not sent after {pending['tries']} tries; given up")
        else:
            self.runtime.log(f"digest not sent try={pending['tries']}; tried again in {RETRY} s")

    def kept(self, ctx, message, slot, view):
        """The record's ``sent`` with this card's view, the last SENT_KEPT cards' (no view: an older pending's)."""
        sent = read_object(self.path(ctx)).get("sent")
        sent = {key: value for key, value in sent.items() if isinstance(value, dict)} if isinstance(sent, dict) else {}
        if message and isinstance(view, dict):
            sent[message] = {"slot": slot, "view": view}
        return dict(sorted(sent.items(), key=lambda item: float(item[1].get("slot") or 0))[-SENT_KEPT:])

    def dismissed(self, ctx, message_id, topics):
        """After a click on this card's 这些都不用回答了 (Overview.handed, once acknowledged): the card redrawn
        from its saved view with that line grey, 已撤下 N 个, no button; the view saved once Feishu took it. A card
        without a saved view (sent before they were, or no longer kept) is left as it is."""
        path = self.path(ctx)
        with resource_lock("digest", str(path)):
            sent = read_object(path).get("sent")
        entry = sent.get(message_id) if isinstance(sent, dict) else None
        view = entry.get("view") if isinstance(entry, dict) else None
        if entry is None and self.board_dismissed(ctx, message_id, topics):
            return
        view = cleared(view, set(topics)) if isinstance(view, dict) else None
        if view is None:
            self.runtime.log(f"digest card not redrawn message={message_id[-8:]}: no view of those topics kept")
            return
        if not self.messenger.patch_card(ctx.profile, message_id, digest_card(view)):
            self.runtime.log(f"digest card redraw failed message={message_id[-8:]}")
            return
        with resource_lock("digest", str(path)):
            record = read_object(path)
            sent = record.get("sent") if isinstance(record.get("sent"), dict) else {}
            if isinstance(sent.get(message_id), dict):
                write_json(path, {**record, "sent": {**sent, message_id: {**sent[message_id], "view": view}}},
                           ensure_ascii=False)
        self.runtime.log(f"digest card redrawn message={message_id[-8:]} cleared={len(view['cleared'])}")
