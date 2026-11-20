"""The owner recalled or edited a message the Bridge acts on: its task stops, or takes the new words (docs/architecture.md
撤回与编辑), unless COVEY_SOURCE_WATCH=0.

No event says either (core/source_book.py), so each message the owner sends that becomes a task or a supplement is
read back every WATCH_SECONDS while its task lives, and for GRACE_SECONDS after it ended:

- **recalled** (``deleted``): its task stops as its card's 停止 would (queued: never runs; running: stopped), the card
  saying 原消息已撤回. A supplement it was goes after as another one: ignore it. A turn already ended keeps its answer,
  but what it 记下了 (memory lines, knowledge entries) is taken back. Each topic the butler opened, forked or handed work
  to from that turn gets a line in its thread, 来源消息已撤回, with 停止 while a task of it still goes (rather than
  stopping them).
- **edited** (``updated``, a newer ``update_time`` and words that differ from the ones it had: a thread started under
  a message marks it updated too, its words untouched): the new words go to its task as a supplement (app/steering.py
  deliver_steer): a queued task takes them into its request before it starts, a running one reads them after its
  current step. A task that has ended, or waits for an answer, gets a line in its thread instead: the turn went by the
  old words, and nothing runs again by itself.

Only the owner's messages (``om_`` ids: not the web page's), and nothing in a task runner. Every call is outside the
locks; a read that fails is tried at the next round, a recall or an edit acted on once (``Watched.stamp``).
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.core.loops import every
from covey.core.process_env import env_flag
from covey.core.source_book import GRACE_SECONDS, MAX_AGE_SECONDS, Watched

SWITCH = "COVEY_SOURCE_WATCH"
WATCH_SECONDS = 15
PREVIEW = 80  # Characters of the message quoted in a supplement or a line.
RECALL_ERRORS = ("230011", "recall", "withdrawn")  # Feishu's refusal of a recalled message, should it refuse to read one.


def enabled() -> bool:
    return env_flag(SWITCH, True)


def preview(text) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:PREVIEW] + ("…" if len(flat) > PREVIEW else "")


EDIT_HEAD = "【主人编辑了刚才的消息】修改后的全文如下，以它为准；按原文已经做了的部分，需要时调整："
RECALL_HEAD = "【主人撤回了刚才那条补充】"  # The web page reads both off a topic's line (app/web_said.py).


def edited_text(text) -> str:
    """The supplement an edit becomes."""
    return (EDIT_HEAD + "\n\n" + str(text or "").strip())


def recalled_text(text) -> str:
    """The supplement the recall of a supplement becomes."""
    return f"{RECALL_HEAD}不要再按它做：「{preview(text)}」"


def same_words(one, other) -> bool:
    """Whether two versions of a message say the same, whitespace aside. Feishu marks a message ``updated`` with a newer
    update_time for more than an edit: a thread started under it (the butler's topic hung under the owner's question)
    does too, its words untouched. So only words that differ make an edit."""
    return "".join(str(one or "").split()) == "".join(str(other or "").split())


def read_back(detail, message_id, stamp):
    """(what became of the message, its words now, its update_time) from Feishu's answer ``detail``: "recalled",
    "edited" (an update_time other than ``stamp``), "same", or "unknown" when the answer says nothing (a failed read)."""
    from covey.feishu.forward import message_text
    from covey.feishu.message_read import message_item

    if not isinstance(detail, dict) or detail.get("ok") is False:
        error = str((detail or {}).get("error") or "") if isinstance(detail, dict) else ""
        return ("recalled" if any(word in error.lower() for word in RECALL_ERRORS) else "unknown"), "", ""
    item = message_item(detail, message_id)
    if not item:
        return "unknown", "", ""
    if item.get("deleted") is True:
        return "recalled", "", ""
    updated = str(item.get("update_time") or "")
    if item.get("updated") is True and updated and updated != stamp:
        return "edited", message_text(item), updated
    return "same", "", ""


def steer_target(runtime, state_dir, chat_id, message_id):
    """(job id, topic id) of the live task of ``chat_id`` the message went into as a supplement; None without one."""
    from covey.core.deliveries import delivery_path

    with runtime.jobs.lock:
        jobs = [job for job in runtime.jobs.by_id.values() if job.chat_id == chat_id]
    for job in sorted(jobs, key=lambda job: -int(job.job_id)):
        if delivery_path(state_dir, job.topic_id, message_id).exists():
            return int(job.job_id), job.topic_id
    return None


def note(app, ctx, event, message_id, chat_id, sender, text, *, steered=False) -> None:
    """The owner's message ``message_id``, just taken in by ``app`` as a task or (``steered``) a supplement, watched."""
    runtime = app.runtime
    if (not enabled() or runtime.in_runner or not str(message_id).startswith("om_")
            or not runtime.access.is_owner(sender)):
        return
    steer = steer_target(runtime, ctx.state_dir, chat_id, message_id) if steered else None
    if steered and steer is None:
        return
    stamp = str(event.get("update_time") or event.get("create_time") or "")
    from covey.core.attachments import message_words

    runtime.sources.note(Watched(message_id, chat_id, app.spec.name, str(ctx.topic_id or ""), stamp,
                                 text=str(text or "")[:4000], words=message_words(text), steer=steer))


class SourceWatch:
    """The feature: its thread, and what a recall or an edit does. ``topics``: the Bridge's Topics (its apps)."""

    def __init__(self, runtime, topics, messenger, voice) -> None:
        self.runtime, self.topics, self.messenger, self.voice = runtime, topics, messenger, voice

    def threads(self):
        return (("source-watch", self.run),) if enabled() else ()

    def run(self, ctx) -> None:
        every(self.runtime, WATCH_SECONDS, self.round, args=(ctx,), isolate="source-watch")

    # -- the rounds ----------------------------------------------------------------------------------------------

    def round(self, ctx, now=None) -> None:
        from covey.feishu.message_read import fetch_message_detail

        now = time.time() if now is None else now
        book = self.runtime.sources
        for item in book.snapshot():
            if now - item.added > MAX_AGE_SECONDS:
                book.drop(item.message_id)
                continue
            if self.task_of(item) is not None:
                item.ended = 0.0
            elif not item.ended:
                item.ended = now
            elif now - item.ended > GRACE_SECONDS:
                book.drop(item.message_id)
                continue
            what, words, stamp = read_back(fetch_message_detail(item.message_id, ctx.profile), item.message_id, item.stamp)
            try:
                if what == "recalled":
                    book.drop(item.message_id)
                    self.runtime.log(f"source recalled message={item.message_id[-10:]}")
                    self.recalled(ctx, item)
                elif what == "edited" and same_words(words, item.words):
                    item.stamp = stamp  # Touched (a thread under it), not edited: nothing to tell its task.
                    self.runtime.log(f"source touched message={item.message_id[-10:]} words unchanged")
                elif what == "edited":
                    item.stamp, item.words = stamp, words
                    self.runtime.log(f"source edited message={item.message_id[-10:]}")
                    self.edited(ctx, item, words)
            except Exception as exc:  # noqa: BLE001 - one message's failure leaves the others to their round
                self.runtime.log(f"source {what} failed message={item.message_id[-10:]}: {type(exc).__name__}")

    @staticmethod
    def base_of(app, ctx):
        """``app``'s own context (its Harness's), else the round's."""
        return getattr(app.harness, "context", None) or ctx

    def app_of(self, item):
        apps = getattr(self.topics, "apps", {}) or {}
        return apps.get(item.app) or next(iter(apps.values()), None)

    def task_of(self, item):
        """The live task ``item`` is: its own (keyed by the message) or the one it went into as a supplement."""
        if item.steer is not None:
            job = self.runtime.jobs.find(item.steer[0])
            return job if job is not None and job.topic_id == item.steer[1] and not self.ended(job) else None
        job = self.runtime.jobs.find(0, item.message_id)
        return job if job is not None and not self.ended(job) else None

    @staticmethod
    def ended(job) -> bool:
        from covey.app.runtime import job_stage

        return job_stage(job) == "ended"

    # -- a recall ------------------------------------------------------------------------------------------------

    def recalled(self, ctx, item) -> None:
        from covey.app.project_clicks import stop_task
        from covey.core.jobs import RECALLED, find_job_record

        app = self.app_of(item)
        if app is None:
            return
        base = self.base_of(app, ctx)
        job = self.task_of(item)
        if item.steer is not None:
            if job is not None:
                self.supplement(app, base, job, recalled_text(item.text), f"recall-{item.message_id}")
            return
        if job is not None:
            stop_task(app.controls, base, job.topic_id, job.chat_id, job, f"recall-{item.message_id}", by=RECALLED)
        record = find_job_record(Path(base.state_dir), item.message_id)
        if record:
            self.take_back(app, base, record)
            self.mark_opened(app, base, item, record)

    def take_back(self, app, base, record) -> None:
        """What the turn ``record`` 记下了, taken back: its memory lines (as 不对 does) and its knowledge entries."""
        from covey.app import memory_collect
        from covey.core import knowledge

        target = dataclasses.replace(base, topic_id=str(record.get("topic_id") or base.topic_id))
        try:
            lines = memory_collect.void_turn(self.runtime, self.messenger, target, record)
        except Exception as exc:  # noqa: BLE001 - the rest of the recall goes on
            lines = 0
            self.runtime.log(f"source recall memory failed: {type(exc).__name__}")
        key, dropped = str(record.get("key") or ""), 0
        for entry in knowledge.entries(target.state_dir) if key else ():
            if entry.get("by") == "topic" and (entry.get("source") or {}).get("job_key") == key:
                try:
                    knowledge.drop(target.state_dir, entry["id"], "主人撤回了记下它的那条消息",
                                   source={"job_key": key}, by="topic")
                    dropped += 1
                except ValueError:
                    pass
        if lines or dropped:
            self.runtime.log(f"source recall took back memory={lines} knowledge={dropped} job=#{record.get('job_id')}")

    def mark_opened(self, app, base, item, record) -> None:
        """Each topic the butler's turn ``record`` opened, forked or handed work to: 来源消息已撤回 in its thread, with 停止
        while one of its tasks goes."""
        from covey.cards.source_note import RECALLED_LINE, recalled_card
        from covey.core import butler_opened
        from covey.core.jobs import RECALLED
        from covey.core.state import read_object
        from covey.core.topics import topic_file

        state = Path(base.state_dir)
        topics = []
        for _, opened in butler_opened.by_job(state, str(record.get("key") or "")):
            topic = butler_opened.topic_of(opened)
            if butler_opened.answered(opened) and topic and topic not in topics:
                topics.append(topic)
        for topic in topics:
            root = str(read_object(topic_file(state, topic)).get("root_id") or "")
            if not root.startswith("om_"):
                continue
            live = self.runtime.jobs.open_in_topic(topic, item.chat_id)
            job = live[-1] if live else None
            card = recalled_card(job.job_id if job else None, job.message_id if job else "", RECALLED)
            key = self.voice.safe_key(self.voice.key("source-recalled", f"{item.message_id[-16:]}-{topic[:16]}"))
            self.messenger.send_message(base.profile, item.chat_id, RECALLED_LINE, key, reply_to_message_id=root,
                                        voice=self.voice, card=card)
            self.runtime.log(f"source recall marked topic={topic[:12]} live={len(live)}")

    # -- an edit -------------------------------------------------------------------------------------------------

    def edited(self, ctx, item, words) -> None:
        app = self.app_of(item)
        if app is None or not str(words or "").strip():
            return
        base = self.base_of(app, ctx)
        job = self.task_of(item)
        took = job is not None and self.supplement(app, base, job, edited_text(words), f"edit-{item.message_id}-{item.stamp}")
        item.text = str(words)[:4000]
        if not took:
            self.tell_edited(base, item, words)

    def tell_edited(self, base, item, words) -> None:
        """The line of an edit no task takes: in the topic's thread, or at the top level of the butler's chat."""
        from covey.core.butler import is_butler_topic
        from covey.core.state import read_object
        from covey.core.topics import topic_file

        root = ""
        if item.topic_id and not is_butler_topic(item.topic_id, item.chat_id):
            root = str(read_object(topic_file(Path(base.state_dir), item.topic_id)).get("root_id") or "")
        text = (f"你改了这条消息：「{preview(words)}」。这一轮是按改之前的内容做的，没有重做；"
                "要按新内容做，直接再说一句。")
        key = self.voice.safe_key(self.voice.key("source-edited", f"{item.message_id[-16:]}-{item.stamp}"))
        self.messenger.send_message(base.profile, item.chat_id, text, key,
                                    reply_to_message_id=root if root.startswith("om_") else "", voice=self.voice)

    def supplement(self, app, base, job, text, mid) -> bool:
        """``text`` into ``job`` as a supplement of its own (``mid``); whether it went or may have (rule 6: never again)."""
        from covey.core.jobs import context_for_job
        from covey.core.steer import SteerRefused, SteerUncertain

        target = context_for_job(dataclasses.replace(base, topic_id=job.topic_id), job)
        try:
            app.steering.deliver_steer(target, job, text, mid)
            return True
        except SteerUncertain:
            self.runtime.log(f"source supplement uncertain job=#{job.job_id}")
            return True
        except SteerRefused as exc:
            self.runtime.log(f"source supplement refused job=#{job.job_id}: {exc}")
            return False
