"""Topic status cards (话题状态卡, cards/status_card.py): what the pinned board (app/pins.py) pins for a topic with a
thread in the owner's private chat, in place of its task or question card, whose top (all the Pin panel shows) is the
round's request, its configuration and its tiles. Each says the
topic's number and title, its state with when and which round, and one small line: this round's request, the question,
the conclusion's lead or why it stopped. The state is a core/look.py STATES key, as the 看板 and 手上的事 say it (``tone``):
等你, 停住了 (failed, interrupted, stopped by someone other than the owner: core/look.py record_tone), 可能卡住, 有结果
(a result not seen yet) or 做完 (seen), 在做, 排队, 已停止 (the owner's own stop). Only where the
placement table says so (core/placement.py Row.status_card: the butler off, COVEY_BUTLER=0): a 话题区's topic is pinned by its turn's own
cards in its thread (app/pins.py aimed), and the status cards sent before stay in their
threads, their records readable (``card_message``), never written again.

The card is a message in the topic's thread (a reply in thread to its root, core/topics.py; registered as the topic's
message), sent the first time the board pins the topic, so clicking the pin opens the whole thread there. The Bridge
alone writes it, from the board's loop (app/overview.py, every TICK): a whole-card PUT when what it says changed, at
most once every WRITE_GAP seconds, its sequence reserved from its record (rule 3).

The record, <state>/status-cards/<topic_id>.json: {schema_version, topic_id, chat_id, card_id, message_id, state:
creating | sent | failed | uncertain, sequence, shown: [title, state, facts, line] last written, written_at; tries, failed_at
once a send surely did not go}. Sending follows rule 1: ``creating`` (with the card's id once CardKit made it) under the
record's lock, the send outside it, then by what became of it (feishu/messenger.py ``last_outcome``): ``sent`` with the
message; ``failed`` when it surely did not go (Feishu refused it, no card was made), sent again under the same key every
RETRY_GAP seconds, at most RETRIES times; ``uncertain`` when it may have gone, never sent again. A topic whose card is not
out keeps its old pin.
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.cards.review import answer_lead
from covey.cards.status_card import status_card
from covey.core.card_sequence import file_card, reserve
from covey.core.checkpoint import checkpoint_steps, read_checkpoint, step_words
from covey.core import progress as turn_progress
from covey.core.jobs import topic_job_records
from covey.core.locks import resource_lock
from covey.core.metrics import run_seconds, timings_of, took_text
from covey.core.butler import traits
from covey.core.state import keyed_json_path, read_object, write_json
from covey.core.text import cut
from covey.core.topics import remember_topic_message, topic_file, topic_label, topic_title
from covey.core import look
from covey.core.board_read import START
from covey.core.turn_words import BLANK_PHASES, START_LINE, request_line
from covey.feishu.messenger import unsent_outcome
from covey.feishu.transport import NOT_SENT

FOLDER = "status-cards"
SCHEMA = 1
WRITE_GAP = 20  # Seconds between two writes of one card.
RETRIES = 3  # Sends of a card that surely did not go, tried again under the same key after the first.
RETRY_GAP = 600  # Seconds from a send that did not go to the next try.
TITLE_CHARS = 30
LINE_CHARS = 80
# core/turn_words.py: no phase of the model's yet (an empty one too), the ended states' words, the request's line.
UNSET_PHASES = BLANK_PHASES


def record_path(state_dir, topic_id) -> Path:
    return keyed_json_path(state_dir, FOLDER, topic_id)


def card_message(state_dir, topic_id) -> str:
    """The message of ``topic_id``'s status card, '' without one sent."""
    message = str(read_object(record_path(state_dir, topic_id)).get("message_id") or "")
    return message if message.startswith("om_") else ""


def retry_due(record, now) -> bool:
    """Whether the card of ``record``, whose last send surely did not go (``failed``), is sent again now: RETRY_GAP seconds
    after it, at most RETRIES times."""
    tries = int(record.get("tries") or 0)
    return tries <= RETRIES and now - float(record.get("failed_at") or 0) >= RETRY_GAP


def when(at, now):
    """"14:25" today, "11-06 22:10" another day; '' unknown."""
    if not isinstance(at, (int, float)) or isinstance(at, bool) or at <= 0:
        return ""
    fmt = "%H:%M" if time.localtime(at)[:3] == time.localtime(now)[:3] else "%m-%d %H:%M"
    return time.strftime(fmt, time.localtime(at))


def title_of(state_dir, topic_id):
    """"T32 · 标题": the topic's title (core/topics.py topic_title) cut to TITLE_CHARS."""
    label = topic_label(Path(state_dir), topic_id)
    title = cut(topic_title(Path(state_dir), topic_id), TITLE_CHARS)
    return " · ".join(part for part in (label, title) if part) or "话题"


def ended(record):
    timings = timings_of(record)
    end = timings.get("execution_finished")
    return end if isinstance(end, (int, float)) and not isinstance(end, bool) else None


def lines(state_dir, chat, row, now):
    """(title, state, facts, line) a board ``row`` (app/pins.py board) shows on its topic's card: ``state`` a core/look.py
    STATES key; ``at`` when its result ended or its run started (as the board has them), ``seen`` when the owner first saw
    a result (None: not yet), ``started`` when the run started."""
    topic, record = row["topic"], row.get("record") or {}
    jobs = topic_job_records(Path(state_dir), topic, chat_id=chat)
    job_id = int(record.get("job_id") or 0)
    turn = sum(1 for job in jobs if 0 < int(job.get("job_id") or 0) <= job_id) or len(jobs) or 1
    title = title_of(state_dir, topic)
    if row["group"] == "waiting" and record.get("status") == START:
        # Its task waits for 开始 on its launch card (core/board_read.py starts): no question, what it waits for.
        facts = " · ".join(part for part in (when(row.get("at"), now), f"第 {turn} 轮") if part)
        return title, "waiting", facts, START_LINE
    if row["group"] == "waiting":
        asked = [q for q in record.get("questions") or [] if isinstance(q, dict)]
        words = next((str(q.get("question") or q.get("header") or "") for q in asked
                      if q.get("question") or q.get("header")), "")
        more = f"（共 {len(asked)} 题）" if len(asked) > 1 else ""
        facts = " · ".join(part for part in (when(record.get("created_at"), now), f"第 {turn} 轮") if part)
        return title, "waiting", facts, cut(f"问题：{words}", LINE_CHARS) + more if words else ""
    if row["group"] == "result":
        end, start = row.get("at"), row.get("started")
        # The turn's 用时 as its task card and answer card say it (core/metrics.py run_seconds, ``ended`` at its
        # execution's end); a record without those timings by the board's times.
        took = run_seconds(record.get("timings"), now=ended(record))
        if took is None or ended(record) is None:
            took = end - start if end and start and end >= start else None
        facts = " · ".join(part for part in (when(end, now), f"{turn} 轮",
                                             f"用时 {took_text(took)}" if took is not None else "") if part)
        tone = look.record_tone(record, seen=row.get("seen") is not None)
        if tone in ("result", "done"):
            lead = answer_lead(record.get("final_text") or "", LINE_CHARS)
            return title, tone, facts, f"结论：{lead}" if lead else ""
        why = cut(record.get("status_message") or "", LINE_CHARS)
        return title, tone, facts, f"原因：{why}" if why else ""
    request = request_line(record.get("user_text"))
    if record.get("status") == "queued":
        return title, "queued", f"第 {turn} 轮", cut(f"这一轮：{request}", LINE_CHARS) if request else ""
    point = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    phase = str(point.get("phase") or "").strip()
    start = row.get("started") or row.get("at")
    # The checkpoint's steps say where the turn is ("第 3/5 步：<title>", core/checkpoint.py step_words) in place of
    # its phase; without steps, the phase as before.
    steps = step_words(checkpoint_steps(point))
    at = cut(steps, 30) if steps else f"阶段：{cut(phase, 30)}" if phase not in UNSET_PHASES else ""
    # Its estimate as a time of day, or that it may be stuck (core/progress.py): the card changes only when they do.
    found = turn_progress.progress(record, checkpoint=point, history=turn_progress.turn_seconds(jobs, exclude=job_id),
                                   now=now)
    # One that may be stuck says so in its tag (可能卡住, orange), not again in its figures.
    stuck = bool(found.get("maybe_stuck"))
    facts = " · ".join(part for part in (f"第 {turn} 轮", f"{when(start, now)} 开始" if when(start, now) else "",
                                         at, *([] if stuck else turn_progress.board_words(found, now, clock=True))) if part)
    return title, "stuck" if stuck else "running", facts, cut(f"这一轮：{request}", LINE_CHARS) if request else ""


class StatusCards:
    def __init__(self, runtime, messenger, voice):
        self.runtime, self.messenger, self.voice = runtime, messenger, voice

    @staticmethod
    def root(state_dir, topic_id, chat):
        """The topic's thread root when it has a thread in ``chat`` (a topic, not the butler), else ''."""
        if not traits(topic_id, chat).has_thread:
            return ""
        data = read_object(topic_file(Path(state_dir), topic_id))
        root = str(data.get("root_id") or "")
        return root if root.startswith("om_") and data.get("chat_id") in (None, "", chat) else ""

    def ensure(self, ctx, chat, rows, now):
        """{topic: its card's message} for ``rows`` (the ones the board pins), each card sent or brought up to date
        first; a topic without one (no thread, its send uncertain) is not in it."""
        found = {}
        if self.voice is None:
            return found
        for row in rows:
            topic = row["topic"]
            root = self.root(ctx.state_dir, topic, chat)
            if not root:
                continue
            try:
                shown = list(lines(ctx.state_dir, chat, row, now))
                message = self._one(ctx, chat, topic, root, shown, now)
            except Exception as exc:  # noqa: BLE001 - its old pin stays
                self.runtime.log(f"status card failed topic={topic[:12]}: {type(exc).__name__}")
                continue
            if message:
                found[topic] = message
        return found

    def _one(self, ctx, chat, topic, root, shown, now):
        path = record_path(ctx.state_dir, topic)
        with resource_lock("status-card", str(path)):
            record = read_object(path)
            if record.get("state") == "uncertain":
                return ""
            if not record.get("message_id") and record.get("state") == "failed" and not retry_due(record, now):
                return ""
            if not record.get("message_id"):
                record = {**record, "schema_version": SCHEMA, "topic_id": topic, "chat_id": chat, "state": "creating",
                          "at": record.get("at") or now}
                write_json(path, record, ensure_ascii=False, indent=1)
                send = True
            else:
                send = False
        if send:
            return self._send(ctx, chat, topic, root, shown, path, record, now)
        if shown != record.get("shown") and now - float(record.get("written_at") or 0) >= WRITE_GAP:
            self._write(ctx, topic, shown, path, record, now)
        return record["message_id"]

    def _send(self, ctx, chat, topic, root, shown, path, record, now):
        def created(card_id):
            with resource_lock("status-card", str(path)):
                write_json(path, {**read_object(path), "card_id": card_id}, ensure_ascii=False, indent=1)
            record["card_id"] = card_id

        card_id, message = self.messenger.create_and_send_card(
            ctx.profile, chat, status_card(*shown), self.voice.key("status-card", topic), voice=self.voice,
            card_id=record.get("card_id", ""), on_created=created, reply_to_message_id=root)
        # No card made: nothing went (feishu/messenger.py says so too, read here for a Messenger that does not).
        how = "" if message else NOT_SENT if not card_id else unsent_outcome(self.messenger)
        with resource_lock("status-card", str(path)):
            saved = {**read_object(path), "card_id": card_id or record.get("card_id", "")}
            if message:
                saved.update(state="sent", message_id=message, shown=shown, written_at=now)
            elif how == NOT_SENT:
                saved.update(state="failed", tries=int(saved.get("tries") or 0) + 1, failed_at=now)
            else:
                saved.update(state="uncertain")
            write_json(path, saved, ensure_ascii=False, indent=1)
        if message:
            remember_topic_message(Path(ctx.state_dir), topic, chat, message)
        self.runtime.log(f"status card {saved['state']} topic={topic[:12]}"
                         + (f" message={message[-8:]}" if message else "")
                         + (f" tries={saved['tries']}" if saved["state"] == "failed" else ""))
        return message

    def _write(self, ctx, topic, shown, path, record, now):
        handle = file_card(path, record, shared=False)
        with handle.lock:
            sequence = reserve(handle)
            result = self.messenger.put_card(ctx.profile, record["card_id"], status_card(*shown), sequence=sequence,
                                             key=f"status-{topic[:16]}-{sequence}", timeout=10)
        if result is None:
            self.runtime.log(f"status card write not confirmed topic={topic[:12]} sequence={sequence}")
            return
        with resource_lock("status-card", str(path)):
            write_json(path, {**read_object(path), "shown": shown, "written_at": now}, ensure_ascii=False, indent=1)
