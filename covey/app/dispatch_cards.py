"""派发活卡片 (docs/main-thread.md 派发活卡片), when COVEY_MAIN_DISPATCH_CARD=1: the card the main thread's topic_start,
topic_send and topic_fork leave at the chat's top level, quoting the owner's message, saying how the job it handed to a
topic is going, kept up to date until the conclusion comes back and the card shrinks to one line (cards/dispatch_card.py).

- ``created`` (called by app/main_topics.py once a topic took the work): sends the card, records it
  (core/dispatch_records.py), and makes a quote of it go to the topic (``route_message``). Never raises: the topic is
  open and its work queued already.
- ``DispatchCards`` (a feature object of the Bridge: ``threads()`` gives the refresh thread): each second it looks at the
  records still live, reads the topic's job records (no Feishu call, no lock) and draws the card; when what it would say
  differs from what it last wrote and at least ``MIN_GAP`` seconds have passed it writes the whole card (one CardKit
  write, the sequence ``reserve`` hands out first and never again, rule 3; a failed write backs off and goes on with
  the next sequence). It is the one writer of the card after ``created``.
- ``relayed`` (called by app/relay_send.py in the task runner, B8): the topic's conclusion went out or was skipped.
  It only leaves a small note (core/dispatch_records.py ``mark_relayed``); the thread reads it, writes the one-line card
  and settles the record (live -> settled, rule 4). A relay that did not go out (uncertain, unsent) leaves no note, so
  the card stays as it is for the owner to see; a card whose job ended and got no note for ``TERMINAL_GRACE`` seconds
  settles as it is (no more refreshing)."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from covey.cards import answer_card
from covey.cards.dispatch_card import (DONE, FAILED, RUNNING, STOPPED, WAITING, Dispatch, dispatch_card, settled_card)
from covey.core import dispatch_records as records
from covey.core import env as cenv
from covey.core.card_sequence import file_card, reserve
from covey.core.checkpoint import checkpoint_steps
from covey.core.jobs import find_job_record, topic_job_records
from covey.core.locks import resource_lock
from covey.core.loops import every
from covey.core.metrics import run_seconds, timings_of, took_text
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.status_line import step_text
from covey.core.topics import remember_topic_message, route_message, topic_label, topic_title
from covey.core.turn_words import UNSET_PHASES, ask_words, request_line

ENV = "COVEY_MAIN_DISPATCH_CARD"
TICK = 1.0  # Seconds between two looks at the live records.
MIN_GAP = 2.5  # Seconds between two writes of one card (CardKit allows 10 a second; v3's finding: one in 2 to 3 is enough).
BACKOFF = 60.0  # The longest wait after a failed write (2, 4, 8 ... seconds).
LINK_RETRY = 30.0  # Seconds before a thread link that could not be read is tried again (one read of Feishu per tick at most).
TERMINAL_GRACE = 600.0  # A job ended this long with no conclusion note: its card settles as it is.
EXPIRE = 3 * 86400  # A record still live this long after it began is settled without a write.
BODY_CHARS = 300


def enabled() -> bool:
    return cenv.get(ENV, "").strip() == "1"


def words(text, limit=BODY_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def quantize(seconds) -> str:
    """A time as the card says it, steady enough that a running card is not redrawn every second: by 10 seconds up to
    two minutes, by the minute after."""
    seconds = max(0, int(seconds))
    return took_text(seconds // 10 * 10 if seconds < 120 else seconds // 60 * 60)


def clock(epoch) -> str:
    return time.strftime("%H:%M", time.localtime(float(epoch))) if epoch else ""


# What the job records say -------------------------------------------------------------------------------------------

def job_number(job) -> int:
    try:
        return int(job.get("job_id") or 0)
    except (TypeError, ValueError):
        return 0


def current_job(state_dir, record) -> dict | None:
    """The job the card speaks of: the topic's newest, from the job the dispatch started or joined (``mid``) on (an answer
    to a question starts a new one in the same topic); None while the topic has none yet."""
    first = find_job_record(Path(state_dir), str(record.get("mid") or "")) if record.get("mid") else None
    if first is None:
        return None
    later = [job for job in topic_job_records(Path(state_dir), str(record.get("topic_id") or ""))
             if job_number(job) >= job_number(first)]
    return max(later, key=job_number, default=first)


def question_words(state_dir, topic_id) -> str:
    from covey.core.store import directory_store

    rows = [row for _, row in directory_store(Path(state_dir) / "questions").query(topic_id=topic_id, status="pending")
            if not row.get("display_pending")]
    rows.sort(key=lambda row: row.get("created_at") or 0, reverse=True)
    return ask_words(rows[0]) if rows else ""


def status_of(state_dir, record, job, now) -> tuple[str, str]:
    """(the card's state word, what the fold says) of the dispatch ``record`` whose job is ``job``."""
    if job is None:
        return (RUNNING, "排队中，还没开始") if record.get("starts") else (WAITING, "等你在话题里点启动卡上的开始")
    status, message = str(job.get("status") or ""), words(job.get("status_message"))
    if status == "waiting_workspace":
        return WAITING, "等你在话题里点启动卡上的开始"
    if status == "waiting_user":
        asked = question_words(state_dir, record.get("topic_id"))
        return WAITING, "等你回答：" + asked if asked else message or "等你回答"
    if status == "done":
        return DONE, message or "已完成"
    if status in ("failed", "interrupted"):
        return FAILED, message or ("执行失败" if status == "failed" else "任务中断")
    if status == "cancelled":
        return STOPPED, message or "已停止"
    # The step under way, as the main thread's snapshot says it (app/main_thread.py snapshot_groups), else the phase.
    if status == "cancel_requested":
        return RUNNING, "正在停止…"
    step, doing = step_text(checkpoint_steps({"steps": job.get("steps")}), job.get("step_times"), now)
    phase = str(job.get("phase") or "")
    phase = "" if phase in UNSET_PHASES else phase
    return RUNNING, step if doing else ("阶段：" + phase if phase else message or "处理中")


def figures(state_dir, record, job, status, now, took, titles) -> tuple[tuple, tuple]:
    """(输入 parts, 输出 parts) of the card's box, from the job record alone."""
    job = job or {}
    harness = job.get("harness_id") or job.get("backend") or ""
    model = str(job.get("model") or "").rsplit(" · ", 1)[-1]
    effort = str(job.get("effort") or "").strip()
    workspace = str(job.get("workspace_name") or "")
    inputs = [part for part in (titles.get(harness, harness), model, f"{answer_card.BUDGET_WORD} {effort}" if effort else "",
                                workspace) if part]
    if status in (RUNNING, WAITING):
        parts = [answer_card.figure("开始", clock(record.get("started_at")))] + (
            [answer_card.figure("已运行", took)] if took else [])
        lines = answer_card.RUNNING_LINES
    else:
        parts = [answer_card.figure("用时", took)] if took else []
        lines = answer_card.ENDED_LINES
    tokens = job.get("context_tokens")
    if type(tokens) is int and tokens > 0:
        parts.append(answer_card.figure("上下文", f"{tokens // 1000}k"))
    return tuple(inputs), tuple(answer_card.output_lines(parts, lines))


def facts(state_dir, record, now, titles, url="") -> Dispatch:
    """What the card of ``record`` says now."""
    topic = str(record.get("topic_id") or "")
    job = current_job(state_dir, record)
    status, body = status_of(state_dir, record, job, now)
    # A job that ended is timed to when it did (its record's last write), not to now: its card stops changing.
    at = (epoch_seconds((job or {}).get("updated_at")) or now) if status in (DONE, FAILED, STOPPED) else now
    seconds = run_seconds(timings_of(job or {}), None, at)
    if seconds is None:
        seconds = at - float(record.get("started_at") or at)
    took = quantize(seconds) if job is not None or status != WAITING else ""
    inputs, outputs = figures(state_dir, record, job, status, now, took, titles)
    return Dispatch(token=str(record.get("token") or ""), topic_id=topic, request=str(record.get("request") or ""),
                    status=status, label=topic_label(Path(state_dir), topic), title=topic_title(Path(state_dir), topic),
                    workspace=str((job or {}).get("workspace_name") or ""), started=clock(record.get("started_at")),
                    took=took, body=body, inputs=inputs, outputs=outputs,
                    job_id=str(job_number(job)) if job is not None and job_number(job) else "", url=url)


def titles_of(topics) -> dict:
    """Harness name -> its title (the card's 输入 line), from the Harness apps."""
    try:
        return {name: app.harness.spec.title for name, app in topics.apps.items()}
    except AttributeError:
        return {}


def digest_of(card) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


# Sending the card ---------------------------------------------------------------------------------------------------

def request_words(state_dir, job_key) -> str:
    """The owner's words the main-thread job ``job_key`` started from, one line."""
    record = find_job_record(Path(state_dir), str(job_key or "")) or {}
    return request_line(record.get("user_text")) or str(record.get("title") or "")


def created(env, request, topic_id, job_key="", *, token, kind="start", mid="", starts=True) -> str:
    """Send the card of the work ``request`` (a main-thread tool call) handed to topic ``topic_id``; the record's token,
    '' when there is none. ``job_key``: the main-thread job (default the request's); ``mid``: the key of the topic's job
    the card follows (default the first turn's, "main-" + ``token``, the request's: app/main_topics.py request_token). Once a request: a second call finds
    the card sent. Does nothing with the switch off, or when the owner's message is not known. Never raises."""
    if not enabled():
        return ""
    try:
        return _created(env, request, topic_id, job_key or request.job_key, token, kind, mid, starts)
    except Exception as exc:  # noqa: BLE001 - the topic is open and its work queued: only the card is missing
        env.runtime.log(f"dispatch card failed topic={str(topic_id)[:12]}: {type(exc).__name__}")
        return ""


def _created(env, request, topic_id, job_key, token, kind, mid, starts) -> str:
    from covey.app.relay_targets import main_job_owner_message

    chat, state = env.chat(), Path(env.state_dir)
    owner = main_job_owner_message(state, job_key)
    if not chat or not owner or not topic_id:
        return ""
    path = records.record_path(state, token)
    record = read_object(path)
    if record.get("card_message_id"):
        return token  # The same request again: the card is there.
    if not record:
        record = records.new_record(token=token, chat_id=chat, topic_id=topic_id, job_key=job_key,
                                    mid=mid or "main-" + token, kind=kind, owner_message=owner,
                                    request=request_words(state, job_key), starts=starts,
                                    rounds=records.rounds_for(state, owner, topic_id))
        write_json(path, record, ensure_ascii=False, indent=1)
    card = dispatch_card(facts(state, record, time.time(), titles_of(env.topics)))

    def saved(card_id):
        record["card_id"] = card_id  # Kept before the send: a second try sends this same card.
        write_json(path, record, ensure_ascii=False, indent=1)
    card_id, message = env.messenger.create_and_send_card(
        env.profile, chat, card, env.voice.key("main-dispatch", token), voice=env.voice, card_id=record.get("card_id", ""),
        on_created=saved, reply_to_message_id=owner, in_thread=False)
    if not message:
        env.runtime.log(f"dispatch card not sent topic={str(topic_id)[:12]}")
        return ""
    record.update(card_id=card_id, card_message_id=message, digest=digest_of(card), wrote_at=time.time())
    write_json(path, record, ensure_ascii=False, indent=1)
    # A quote of the card goes to the topic (the owner's reply to it is a word to the job), as the hook's does.
    remember_topic_message(state, topic_id, chat, message)
    route_message(state, chat, message, topic_id, holder=topic_id)
    env.runtime.log(f"dispatch card sent topic={str(topic_id)[:12]} kind={kind} message={message[-8:]}")
    return token


def relayed(state_dir, target, outcome, state, message_id="", reason="") -> None:
    """The relay of ``target`` (app/relay_targets.py) ended in ``state``, "sent" (``message_id`` the conclusion) or
    "skipped"; leaves the note the refresh thread settles the card with (B8). Called in the task runner: only a note, no
    Feishu call. Does nothing with the switch off. Never raises."""
    if not enabled():
        return
    try:
        records.mark_relayed(state_dir, str((target or {}).get("token") or ""), outcome=outcome, state=state,
                             message_id=message_id, reason=reason)
    except Exception:  # noqa: BLE001 - the relay went out; the card settles on its own
        return


class DispatchCards:
    """The Bridge's feature object (app/unified.py PARTS ``dispatch_cards``): the refresh thread, off unless the switch
    is on, and nothing else (no menu item, no click it claims: the buttons route as a topic's)."""
    name = "dispatch_cards"

    def __init__(self, runtime, messenger, voice, titles=None) -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.titles = titles or {}
        self.done: set = set()  # Tokens whose record is settled: not read again.
        self.sched: dict = {}  # token -> {digest, wrote, next, failures, url, link_at}

    def threads(self):
        return ((self.name, self.run),) if enabled() else ()

    def run(self, ctx) -> None:
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def tick(self, ctx, now=None) -> None:
        """One look at every live record: draw what changed, settle what ended."""
        now = time.time() if now is None else now
        for path in records.live_paths(ctx.state_dir):
            if path.stem in self.done:
                continue
            record = read_object(path)
            if not record or not record.get("card_message_id") or not record.get("card_id"):
                continue  # Still being sent (created), or not a record.
            if record.get("state") == records.SETTLED:
                self.done.add(path.stem)
                continue
            try:
                self.refresh(ctx, path, record, now)
            except Exception as exc:  # noqa: BLE001 - the others still refresh
                self.runtime.log(f"dispatch card refresh failed token={path.stem[:8]}: {type(exc).__name__}")

    def refresh(self, ctx, path, record, now) -> None:
        token = str(record.get("token") or path.stem)
        seen = self.sched.setdefault(token, {"digest": str(record.get("digest") or ""), "wrote": float(record.get("wrote_at") or 0),
                                             "next": 0.0, "failures": 0, "url": str(record.get("thread_url") or ""),
                                             "link_at": 0.0})
        if now < seen["next"]:
            return
        if now - float(record.get("started_at") or now) > EXPIRE:
            return self.finish(path, record, "expired")
        note = records.relay_note(ctx.state_dir, token)
        if note:
            return self.settle(ctx, path, record, seen, note, now)
        url = self.thread_url(ctx, record, seen, now)
        view = facts(ctx.state_dir, record, now, self.titles, url)
        card = dispatch_card(view)
        digest = digest_of(card)
        if digest == seen["digest"]:
            self.ended_long_ago(path, record, view, seen, now)
            return
        if now - seen["wrote"] < MIN_GAP:
            return
        if self.write(ctx, path, record, seen, card, now):
            record.update(digest=digest, thread_url=url)
            seen["digest"] = digest
            write_json(path, record, ensure_ascii=False, indent=1)

    def ended_long_ago(self, path, record, view, seen, now) -> None:
        """A card of a job that ended, drawn as it ended and unchanged since, whose conclusion note never came (the relay
        did not go out, or its turn is none the main thread relays): settled as it is after TERMINAL_GRACE."""
        if view.status not in (DONE, FAILED, STOPPED):
            seen.pop("ended", None)
            return
        seen.setdefault("ended", now)
        if now - seen["ended"] > TERMINAL_GRACE:
            self.finish(path, record, "ended")

    def thread_url(self, ctx, record, seen, now) -> str:
        """The link that opens the topic's thread, read from Feishu once (app/topic_links.py keeps what it read); a read that
        failed is tried again after LINK_RETRY, and one read a tick at most."""
        if seen["url"] or now < seen["link_at"]:
            return seen["url"]
        seen["link_at"] = now + LINK_RETRY
        from covey.app.topic_links import topic_thread_link

        url, _ = topic_thread_link(ctx.state_dir, record["topic_id"], profile=ctx.profile)
        seen["url"] = url or ""
        return seen["url"]

    def write(self, ctx, path, record, seen, card, now) -> bool:
        """One whole-card write: the sequence reserved first (saved in the record before the request goes out), never
        reused; false, with the next try pushed back, when CardKit did not confirm."""
        handle = file_card(path, record, shared=False)
        with handle.lock:
            sequence = reserve(handle)
            result = self.messenger.put_card(ctx.profile, record["card_id"], card, sequence=sequence,
                                             key=f"main-dispatch-{str(record.get('token'))[:20]}-{sequence}", timeout=3)
        if result is None:
            seen["failures"] += 1
            seen["next"] = now + min(BACKOFF, 2.0 ** seen["failures"])
            self.runtime.log(f"dispatch card not written token={str(record.get('token'))[:8]} failures={seen['failures']}")
            return False
        seen["failures"], seen["wrote"], seen["next"] = 0, now, 0.0
        return True

    def settle(self, ctx, path, record, seen, note, now) -> None:
        """B8: the conclusion went out (or the owner stopped the job himself): the card becomes one line, then the record
        settles. A write that fails is tried again; the record stays live until it is."""
        job = current_job(ctx.state_dir, record)
        status, reason = status_of(ctx.state_dir, record, job, now)
        if note.get("state") == "skipped":
            status = STOPPED
        elif note.get("outcome") == "done" or status == DONE:
            status = DONE
        elif status not in (FAILED, STOPPED):
            status = FAILED
        url = ""
        if status == DONE and note.get("message_id"):
            url = self.message_url(ctx, record, note["message_id"])
        label = topic_label(Path(ctx.state_dir), str(record.get("topic_id") or ""))
        card = settled_card(status, record.get("request"), url=url, reason=words(reason, 60) if status != DONE else "",
                            label=label)
        if self.write(ctx, path, record, seen, card, now):
            self.finish(path, record, "relay", digest=digest_of(card))

    def message_url(self, ctx, record, message_id) -> str:
        """The link that opens the conclusion message at its place in the chat; '' when it cannot be read."""
        from covey.app.topic_links import message_link, place_of

        try:
            where = place_of(ctx.state_dir, str(record.get("topic_id") or ""), message_id, ctx.profile)
        except Exception:  # noqa: BLE001 - the line without its link
            return ""
        return message_link(record.get("chat_id"), where["position"]) if where.get("position") else ""

    def finish(self, path, record, why, **fields) -> None:
        """The record live -> settled (rule 4), kept so; never refreshed again."""
        with resource_lock("record", str(path)):
            record = read_object(path) or record
            if record.get("state") == records.SETTLED:
                self.done.add(path.stem)
                return
            records.transition(record, records.SETTLED)
            record.update(settled_at=time.time(), settled_by=why, **fields)
            write_json(path, record, ensure_ascii=False, indent=1)
        self.done.add(path.stem)
        self.runtime.log(f"dispatch card settled token={path.stem[:8]} by={why}")
