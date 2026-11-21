"""早晚汇总: at the times of day COVEY_DIGEST_TIMES lists (HH:MM, comma separated, Asia/Shanghai), one card
at the top level of the owner's private chat listing what waits for the owner, what runs and what ended since the
digest before (cards/digest.py). Off unless the variable holds a valid time (docs/usage.md 早晚汇总): without
it there is no thread.

What it lists is read from the records, as the pinned board reads them (core/board_read.py read: the indexes of
questions/ and jobs/, no record lock, no Feishu call, the butler's topics left out), never from a model:
- 等你: 等我 as every reader counts it (core/board_read.py waits): each topic's newest question shown, the butler's
  and the leads' too, else its task waiting for 开始 on a launch card, asked within QUESTION_AGE (one day); those
  waiting longer are not listed one by one but as one line of topic numbers (``stale``), as on 手上的事. Each row
  keeps the card it is acted on (``card``): its T number, and its button on the 看板, open that card;
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

看板 (the bot menu's BOARD_KEY, with a time set or COVEY_BOARD=1, core/app_manifest.py board_switch: with neither, no
such item) is the same card at any time, 随时查看 · HH:MM in its title (the view's ``asked``): what waits and runs now, and what ended since the
last digest time (``last_slot``, ``board_since``; without the digest, the last day). It reads digest.json and never writes it: the times and their
sends go on as before. Nothing to list, the card says so (a click wants an answer). Its record is
<state>/BOARD_FOLDER/<token>.json (app/menu_panel.py), the view in ``digest``; the bot menu keeps one such card and a
second click on the newest folds and unfolds it in place (app/bot_menu.py), unfolded with a fresh view. Its
这些都不用回答了 is handed on as the timed card's (``dismissed``), the card redrawn from the record's view.
"""
from __future__ import annotations

import datetime
import re
import time
from concurrent import futures as concurrent_futures

from covey.core import env as cenv
from covey.app.overview import dismissable
from covey.app.pins import board_results, kept, laid, record_path
from covey.app import revisits, topic_links
from covey.app.topic_links import link_for, linkify
from covey.app.form_cards import settle
from covey.app.menu_panel import MenuPanel
from covey.app.card_lanes import after_ack
from covey.cards.board import KINDS as BOARD_KINDS, REVISIT_KIND, SEEN_KIND, SEEN_MAX, board_card
from covey.cards.digest import ORIGIN, digest_card, digest_text, folded_board
from covey.core import app_manifest, board_read, knowledge, look, patrol, projects, turn_words
from covey.core import project_proposals
from covey.core import progress as turn_progress
from covey.core.board_read import ended_at, order, started_at, unrun
from covey.core.failure import LABELS as FAILURE_LABELS, failure_view
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
LINK_READS = 6  # The 看板's links read at once (``Digest.urls``), as topic_links reads a text's T numbers.
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


def configured(log=lambda _: None):
    """The digest times COVEY_DIGEST_TIMES lists (``parse_times``), [] when unset: the digest is off."""
    raw = cenv.get(ENV, "").strip()
    return parse_times(raw, log) if raw else []


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


def clock_words(at, now) -> str:
    """When ``at`` was, as the 看板 says it: "20:31" today, "昨天 02:40", else "11-16 09:05"."""
    then, today = datetime.datetime.fromtimestamp(at, ZONE), datetime.datetime.fromtimestamp(now, ZONE).date()
    if then.date() == today:
        return then.strftime("%H:%M")
    if then.date() == today - datetime.timedelta(days=1):
        return "昨天 " + then.strftime("%H:%M")
    return then.strftime("%m-%d %H:%M")


LINE_CHARS = 60  # Of a result's first line on the 看板.


def first_line(text) -> str:
    """A result's first line in words, its Markdown marks off, cut to LINE_CHARS."""
    for line in str(text or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+|[*`]", "", line).strip()
        if len(line) >= 4:
            return line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1] + "…"
    return ""


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
    took, seen, topic}, a 等你 row's ``kind`` and ``card`` too, ``older`` (the 做完 rows of results not seen that ended
    before ``since``), and ``stale``, 等我 waiting over QUESTION_AGE (core/board_read.py waits); ``board``: 手上的事
    and the board are on (overview.dismissable), the topics taken off left out and the stale line's button shown."""
    # One read of the indexes (core/board_read.py): with the board, the one its look judges too; the jobs judged
    # here are those read without the board's ``keep``.
    read = board_read.read(state, chat, now, keep=kept(state, chat) if laid() else ())
    unseen, off = board_results(state, chat, now, read)  # 有结果未看, as the board has it (core/board_read.py).
    off = off if board else {}
    jobs = [record for record in read.jobs if board_read.fresh(record, now) and record.get("topic_id") not in off]
    # 等你: 等我 as every reader counts it (core/board_read.py waits): the questions, the butler's and the leads' too,
    # and the tasks waiting for 开始, each with the card it is acted on; those waiting over QUESTION_AGE one line.
    found = board_read.waits(state, chat, now, off=off)

    def named(topic):
        """(label, title) of ``topic``: the butler or a lead asking at the top level by its speaker, no T number."""
        if not board_read.listed(topic, chat):
            return board_read.speaker_of(state, topic, chat) or "管家", ""
        return topic_label(state, topic)

    stale = [{"topic": wait.topic, "label": label, "title": title, "card": wait.card}
             for wait in found.stale for label, title in [named(wait.topic)]]

    def row(topic, state_words, took, seen_it=False):
        label, title = named(topic)
        return {"topic": topic, "label": label, "title": title, "state": state_words, "took": took, "seen": seen_it}

    newest = {}
    for record in read.jobs:
        topic = record.get("topic_id") or ""
        if topic and (topic not in newest or order(record) > order(newest[topic])):
            newest[topic] = record

    def phase_of(record) -> str:
        phase = " ".join(str((record or {}).get("phase") or "").split())
        return "" if phase in turn_words.UNSET_PHASES else phase

    waiting = []
    for wait in found.live:
        topic, record = wait.topic, wait.record
        took = f"等了 {took_text(now - wait.since)}"
        waited = f"等了 {turn_words.wait_words(now - wait.since)}"  # The 看板's grey line (docs/ui.md 7).
        if wait.kind == "start":
            waiting.append({**row(topic, turn_words.DIGEST_START, took), "kind": "start", "card": wait.card,
                            "line": turn_words.START_LINE, "meta": [waited]})
            continue
        ask = turn_words.ask_words(record)
        waiting.append({**row(topic, f"{turn_words.BOARD['waiting']}：{ask}" if ask else turn_words.BOARD["waiting"], took),
                        "kind": "question", "card": wait.card,
                        # The 看板's words (cards/board.py): what he is asked to decide, then how long it waited.
                        "line": f"要你定：{phase_of(newest.get(topic))}" if phase_of(newest.get(topic)) else
                        (f"等你回答：{ask}" if ask else "等你回答"), "meta": [waited]})
    busy = {wait.topic for wait in found.live}

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
        # The estimate or 可能卡住了 after the phase (core/progress.py board_words), the topic's earlier turns as read.
        found = turn_progress.progress(record, now=now, history=turn_progress.turn_seconds(
            [job for job in jobs if job.get("topic_id") == topic], exclude=record.get("job_id")))
        figures = turn_progress.board_words(found, now)
        words = " · ".join([words, *figures])
        ran = run_seconds(record.get("timings"), now=now) if status != "queued" else None
        # Steps as words, not a bar (docs/ui.md 4 progress: 话题行里不放，用“5/8 步”的文字代替).
        plan = (f"{found['plan_done']}/{found['plan_total']} 步"
                if found.get("plan_total") and found.get("plan_done") is not None else "")
        moved = found.get("last_activity_at")
        running_rows.append({**row(topic, words, f"已运行 {took_text(ran)}" if ran is not None else ""),
                             "line": f"现在：{phase_of(record)}" if phase_of(record) else
                             ("正在停止" if status == "cancel_requested" else turn_words.BOARD["queued" if status == "queued" else "running"]),
                             "meta": [plan, *(figure for figure in figures if "卡住" not in figure),
                                      f"{turn_progress.ago_words(now - moved)}有动静" if moved else "",
                                      f"已跑 {turn_words.wait_words(ran)}" if ran is not None else ""],
                             # Its task card: the 看板's row of one that may be stuck opens it (看看 is there).
                             "card": str(record.get("card_message_id") or ""),
                             "stuck": bool(found.get("maybe_stuck"))})
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
        status = record["status"]
        line, meta, card = "", [f"用时 {turn_words.wait_words(took)}" if took is not None else ""], ""
        if status in ("failed", "cancelled", "interrupted"):  # 停住了 on the 看板: why, where it stopped, what next.
            failed = failure_view(record, now)
            why = (failed["title"] if failed else
                   turn_words.STOPPED_BY.get(str(record.get("cancel_requested_by") or ""), "被停止了"))
            line = why + (f"；停在：{phase_of(record)}" if phase_of(record) else "")
            acts = (failed or {}).get("actions") or []
            step = FAILURE_LABELS.get(acts[0], "") if acts else ""
            meta = [f"{clock_words(ended_at(record), now)} 停", f"下一步：在任务卡上点「{step}」" if step else ""]
            card = str(record.get("card_message_id") or "")  # The 看板's row opens its task card (its buttons are there).
        elif status == "done":
            line = first_line(record.get("final_text"))
        done.append({**row(topic, turn_words.STATES.get(status, status), f"用时 {took_text(took)}" if took is not None else "",
                           topic not in unseen), "status": status, "line": line, "meta": meta, "card": card,
                     # 停住了, 已停止 (the owner's own stop), 有结果 or 做完 (core/look.py; cards/board.py groups by it).
                     "tone": look.record_tone(record, seen=topic not in unseen)})
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


def taken_off(view, topics):
    """A 看板's ``view`` once ``topics`` were taken off (这些都不用回答了, or a stopped topic's 不管了): their stale rows
    under ``cleared`` and their blocks gone; None when none of them is on it."""
    on = any(row.get("topic") in topics for key in ("stale", "waiting", "running", "done") for row in view.get(key) or ())
    if not on:
        return None
    view = cleared(view, topics) or dict(view)
    return {**view, **{key: [row for row in view.get(key) or () if row.get("topic") not in topics]
                       for key in ("waiting", "running", "done")}}


class Digest:
    """``chat``: the chat it is sent to, '' the owner's private chat (Access.owner_chat); a personal group has one of
    its own (app/group_boards.py), its record its own (app/pins.py record_path) and
    ``name`` its thread's."""
    origin = ORIGIN  # What its card's 这些都不用回答了 says it is from (Overview.handed's key).

    def __init__(self, runtime, messenger, voice, *, chat="", name="digest") -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.fixed, self.name = chat, name
        self.others = {}  # chat_id -> a personal group's Digest (app/group_boards.py): its 看板's clicks are its own.
        self.link = lambda ctx, chat, text, cards=None: linkify(text, ctx.state_dir, chat, profile=ctx.profile, cards=cards)
        self.menu = MenuPanel(self, BOARD_KIND, BOARD_FOLDER, BOARD_KIND)

    def menu_items(self):
        """The bot menu's 看板, with a digest time set or COVEY_BOARD=1 (app/unified.py menu)."""
        return {BOARD_KEY: self.board} if self.board_on() else {}

    def menu_folds(self):
        """A second 看板 on its card, the newest in the chat (app/bot_menu.py)."""
        return {BOARD_KEY: self.fold_board} if self.board_on() else {}

    def board_on(self) -> bool:
        """Whether the bot menu has 看板: the digest's times, or the board on its own (core/app_manifest.py)."""
        return app_manifest.board_switch() or bool(self.times())

    def board_view(self, ctx, chat, now=None):
        """看板's view now (``asked``): ``chat``'s digest.json only read. Feishu calls (the T links), so outside any
        lock."""
        now = time.time() if now is None else now
        last = read_object(record_path(ctx.state_dir, chat, RECORD)).get("last_slot")
        view = collect(ctx.state_dir, chat, now, board_since(self.times(), now, last), now, board=dismissable())
        view.pop("wanted", None)  # 我本来想找你的事 is the 早晚汇总's, not the 看板's.
        # Each block of the 看板 opens its topic (cards/board.py): the topic's applink, read outside any lock.
        # A 等你 row's opens the card it is acted on (core/board_read.py waits): the question card, the launch card.
        # The old ones' labels link through their ``url`` (cards/overview.py stale_line), plain for the band to lead with.
        # All of them read at once (``urls``): one by one, eight topics whose cards were not kept took four seconds.
        rows = [row for key in ("waiting", "running", "done", "stale") for row in view[key]]
        urls = self.urls(ctx, [(row.get("topic") or "", row.get("card") or "") for row in rows])
        for row in rows:
            row["url"] = urls.get((row.get("topic") or "", row.get("card") or ""), "")
            if row.get("kind") == "start":
                row["act"] = turn_words.START_ACT
        return {**view, "asked": True, "stamp": clock_words(now, now), "can_dismiss": dismissable(),
                "done_since": clock_words(view["since"], now) if view.get("since") else "",
                **self.where(chat), **self.tidy(ctx, chat),
                **self.projects_view(ctx, chat, view), **self.revisit_view(ctx, chat, now)}

    def revisit_view(self, ctx, chat, now):
        """到点回来看's rows on the 看板 (app/revisits.py board_rows), each with its topic's link; {} without any (the switch
        off: the view is as before)."""
        rows = revisits.board_rows(ctx.state_dir, chat, lambda at: clock_words(at, now))
        if not rows:
            return {}
        urls = self.urls(ctx, [(row["topic"], "") for row in rows])
        for row in rows:
            row["url"] = urls.get((row["topic"], ""), "")
        return {"revisits": rows}

    def urls(self, ctx, pairs) -> dict:
        """{(topic, card): ``url``} for each pair with a topic, read in parallel (a Feishu read each when the link is
        not kept, app/topic_links.py) within topic_links.BUDGET seconds; one not read by then has ''. Outside any lock."""
        wanted = list(dict.fromkeys((topic, card) for topic, card in pairs if topic))
        if len(wanted) < 2:
            return {pair: self.url(ctx, *pair) for pair in wanted}
        pool = concurrent_futures.ThreadPoolExecutor(max_workers=min(LINK_READS, len(wanted)),
                                                     thread_name_prefix="board-link")
        try:
            reads = {pool.submit(self.url, ctx, *pair): pair for pair in wanted}
            done, _ = concurrent_futures.wait(reads, timeout=topic_links.BUDGET)
            return {reads[read]: read.result() for read in done if not read.exception()}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def url(self, ctx, topic, card="") -> str:
        try:
            return link_for(ctx.state_dir, topic, card=card, profile=getattr(ctx, "profile", None))
        except Exception:  # noqa: BLE001 - a block that opens nothing, as a topic without a link
            return ""

    def projects_view(self, ctx, chat, view):
        """The 看板's 专项 part (cards/board.py): each live project of ``chat`` with how its topics stand in ``view``,
        and how many proposal cards wait (整理专项 on); nothing in a group's 看板 or with 专项 off."""
        if self.where(chat) or not projects.enabled():
            return {}
        rows = {key: {row.get("topic") for row in view.get(key) or ()} for key in ("waiting", "running")}
        stopped = {row.get("topic") for row in view.get("done") or () if row.get("tone") == "stopped"}
        found = []
        for record in projects.records(ctx.state_dir, chat):
            if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
                continue
            topics = [topic for topic in (record.get("lead_topic_id"), *(record.get("threads") or ())) if topic]
            found.append({"name": record.get("name") or "", "goal": record.get("goal") or "", "topics": topics,
                          "waiting": len(rows["waiting"] & set(topics)), "running": len(rows["running"] & set(topics)),
                          "stopped": len(stopped & set(topics)), "lead": record.get("lead_topic_id") or ""})
        # The proposals waiting for him, each with its topics, to decide right on the 看板, not by
        # scrolling up the chat. One of a single topic is no project any more (app/project_sorter.py MIN_CREATE): left out.
        pending = []
        if project_proposals.enabled():
            for record in project_proposals.waiting(ctx.state_dir):  # The same list as projects_list and 整理专项's 还在等你定.
                topics = list(dict.fromkeys(record.get("topics") or ()))
                rows = []
                for topic in topics:
                    label, title = label_title(ctx.state_dir, topic)
                    rows.append({"topic": topic, "label": label, "title": title})
                pending.append({"id": record["id"], "name": record.get("name") or "",
                                "goal": record.get("goal") or "", "why": record.get("why") or "", "rows": rows})
        urls = self.urls(ctx, [(entry["lead"], "") for entry in found]
                         + [(row["topic"], "") for entry in pending for row in entry["rows"]])
        for entry in found:
            entry["url"] = urls.get((entry.pop("lead"), ""), "")
        for entry in pending:
            for row in entry["rows"]:
                row["url"] = urls.get((row["topic"], ""), "")
        return {"projects": found, "proposals": len(pending), "pending": pending}

    def tidy(self, ctx, chat):
        """The 专项 part (cards/project_proposal.py board_rows): with 整理专项 on, in the owner's private chat (a group's
        看板 has none: 整理专项 sorts the private chat's topics)."""
        if not project_proposals.enabled() or self.where(chat):
            return {}
        return {"tidy": {}}

    def where(self, chat):
        """The card's word for ``chat`` when it is a group with a butler (cards/digest.py), else nothing."""
        return {"where": "群"} if chat in getattr(self.runtime.access, "agent_groups", ()) else {}

    def path(self, ctx):
        return record_path(ctx.state_dir, self.fixed, RECORD)

    def board(self, ctx, chat_id, source, shown=None):
        """The bot menu's 看板: a new card in ``chat_id`` (the owner's private chat) for the click ``source``."""
        self.menu.open(ctx, chat_id, source, lambda token, record: board_card(record["digest"]), BOARD_FAILED,
                       fields=lambda: {"digest": self.board_view(ctx, chat_id)}, shown=shown)
        self.runtime.log(f"digest board sent event={source[-10:]}")

    def fold_board(self, ctx, chat_id, source, message_id):
        """看板 again on its card, the newest in the chat: folded, or unfolded with a fresh view (MenuPanel toggle)."""
        return self.menu.toggle(ctx, chat_id, source, message_id, lambda token, record: board_card(record["digest"]),
                                lambda token, record: folded_board(),
                                fields=lambda: {"digest": self.board_view(ctx, chat_id)})

    @staticmethod
    def matches(raw):
        return isinstance(raw, dict) and raw.get("kind") in BOARD_KINDS

    def claim(self, ctx, event, raw, owner):
        """刷新 or 记为看过 on a 看板 card (cards/board.py KINDS; app/unified.py PANELS), else None: the owner's only; the
        card redrawn from a fresh view once the click is acknowledged."""
        if not self.matches(raw):
            return None
        kind = raw["kind"]
        other = self.others.get(str(event.get("chat_id") or ""))
        if other is not None:
            return other.claim(ctx, event, raw, owner)
        if not owner:
            return "owner_only"
        message = str(event.get("message_id") or "")
        if kind == REVISIT_KIND:
            # 取消 on a row of 到点回来看: that revisit (its ``set_at``: a newer one the topic asked for since stays).
            topic, set_at = raw.get("topic"), raw.get("set_at")
            if not isinstance(topic, str) or not topic or isinstance(set_at, bool) or not isinstance(set_at, (int, float)):
                return "board_revisit_ignored"
            gone = revisits.records.cancel(ctx.state_dir, topic, set_at)
            self.runtime.log(f"revisit cancelled topic={topic[-8:]} gone={gone}")
        elif kind == SEEN_KIND:
            # One row's 记为看过 names its ``topic``; 全部记为看过 names them all (``topics``, cards/board.py SEEN_MAX).
            chat = self.fixed or self.runtime.access.notify_chat()
            topics = raw.get("topics") if isinstance(raw.get("topics"), list) else [raw.get("topic")]
            topics = [topic for topic in topics[:SEEN_MAX] if isinstance(topic, str) and topic]
            if not topics or not chat:
                return "board_seen_ignored"
            now = time.time()
            for topic in topics:
                fresh = board_read.mark_seen(ctx.state_dir, chat, topic, now)
                self.runtime.log(f"digest board seen topic={topic[-8:]} fresh={fresh}")
        if message:
            after_ack(lambda: self.board_refresh(ctx, message))
        return {SEEN_KIND: "board_seen", REVISIT_KIND: "board_revisit_cancel"}.get(kind, "board_refresh")

    def board_refresh(self, ctx, message_id):
        """A 看板 card redrawn from a fresh view, after a proposal on it was decided (app/project_tidy.py ``handed``),
        or its 刷新 or 记为看过 was clicked (``claim``)."""
        chat = self.fixed or self.runtime.access.notify_chat()
        folder = self.menu.directory(ctx)
        try:
            paths = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime)[-BOARD_SCANNED:]
        except OSError:
            return False
        for path in reversed(paths):
            if read_object(path).get("message_id") != message_id or not chat:
                continue
            view = self.board_view(ctx, chat)  # Feishu reads for the links: outside the lock.
            with self.menu.lock(path):
                record = read_object(path)
                if record.get("message_id") != message_id or not isinstance((record.get("view") or {}).get("revision"), int):
                    return True
                changed = {**record, "digest": view, "view": {**record["view"], "revision": record["view"]["revision"] + 1}}
                confirmed = settle(ctx, path, record, changed, board_card(view), self.messenger, BOARD_KIND)
            self.runtime.log(f"digest board refreshed message={message_id[-8:]} confirmed={bool(confirmed)}")
            return True
        return False

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
                view = taken_off(record.get("digest"), set(topics)) if isinstance(record.get("digest"), dict) else None
                if view is None or record.get("message_id") != message_id or not isinstance(
                        (record.get("view") or {}).get("revision"), int):
                    return True
                changed = {**record, "digest": view, "view": {**record["view"], "revision": record["view"]["revision"] + 1}}
                confirmed = settle(ctx, path, record, changed, board_card(view), self.messenger, BOARD_KIND)
            self.runtime.log(f"digest board redrawn message={message_id[-8:]} confirmed={confirmed}")
            return True
        return False

    def times(self):
        return configured(self.runtime.log)

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
                **self.where(chat)}  # 整理专项 is the 看板's alone.
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
        """Each row's T number as a link to its topic, where one is found (app/topic_links.py): a 等我 row's to the
        card it is acted on (its ``card``)."""
        labels = [row["label"] for row in rows if row["label"]]
        if not labels:
            return
        cards = {row["topic"]: row["card"] for row in rows if row.get("topic") and row.get("card")}
        done = iter(self.link(ctx, chat, "\n".join(labels), cards or None).split("\n"))
        for row in rows:
            if row["label"]:
                label = next(done, row["label"])
                if label == row["label"] and row.get("card") and not board_read.listed(row.get("topic") or "", chat):
                    # The butler or a lead, no T number: its name opens the card at the top level.
                    url = self.url(ctx, row["topic"], row["card"])
                    label = f"[{label}]({url})" if url else label
                row["label"] = label

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
