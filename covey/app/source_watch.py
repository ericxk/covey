"""The owner recalled or edited a message the Bridge acts on: its task stops, or takes the new words (docs/architecture.md
撤回与编辑), unless COVEY_SOURCE_WATCH=0.

No event says either (core/source_book.py), so each message the owner sends that becomes a task or a supplement is
read back every WATCH_SECONDS while its task lives, and for GRACE_SECONDS after it ended:

- **recalled** (``deleted``): its task stops as its card's 停止 would (queued: never runs; running: stopped), the card
  saying 原消息已撤回. A supplement it was goes after as another one: ignore it. A turn already ended keeps its answer,
  but what it 记下了 (memory lines, knowledge entries, the entries of core/memstore.py it wrote) is taken back. Each topic the butler opened, forked or handed work
  to from that turn gets a line in its thread, 来源消息已撤回, with 停止 while a task of it still goes (rather than
  stopping them).
- **edited** (``updated``, a newer ``update_time`` and words that differ from the ones it had: a thread started under
  a message marks it updated too, its words untouched): the new words go to its task as a supplement (app/steering.py
  deliver_steer): a queued task takes them into its request before it starts, a running one reads them after its
  current step. A task that has ended, or waits for an answer, gets a line in its thread instead: the turn went by the
  old words, and nothing runs again by itself.

- **downstream** (``turn_source.downstream``, ``finished_downstream``): the jobs whose 轮次来源 has the message as its
  ``origin.message_id`` (the butler's ``relay`` of the owner's words to a topic): a recall marks their topics as above, an
  edit goes to the one running as a supplement and is said in a line in the thread of one that ended. The chain stops at
  the project's lead: what the lead hands on (``lead``) has no message of the owner's in its origin, so the lead decides
  what its members hear.
- **answered or held** (``Watched.kind`` ``answer``, ``held``, noted by app/interaction.py ``note_kind``): a message
  taken as the answer to a waiting question gets a line when recalled, 已作为回答提交, the recall changes nothing; one kept
  as a held note (app/held_notes.py) is withdrawn as its card's 撤回 does, or said to have gone with the answer. An edit of
  either is not told.

Only the owner's messages (``om_`` ids: not the web page's), and nothing in a task runner. Every call is outside the
locks; a read that fails is tried at the next round. The book is kept in ``<state>/source-book.json``
(core/source_book.py), and each thing done for a message is claimed there before it is done (``SourceBook.claim``,
``Watched.stamp``), so a restart replays the round without telling anything twice.
"""
from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path

from covey.core import turn_source
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
ANSWERED_LINE = "你撤回了这条消息，但它已经作为回答提交（或随回答交给了模型），撤回不影响它。"
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


def handed_to(state_dir, job) -> dict:
    """What an edit's supplement into ``job`` adds to its ``requeue`` (core/deliveries.py handed): an ordinary topic's or a
    project's lead's turn that never reads it queues it as that topic's next turn (``topic``; a lead by its alias,
    ``address``: its topic.json has no root message), not as the butler's own (WAVE12 W2-B). {} for the butler's: its
    turn of its own is the owner's message itself, as before."""
    from covey.core import butler, projects

    if butler.is_lead(job.topic_id, job.chat_id):
        found = projects.topic_project(Path(state_dir), job.topic_id)
        return {"topic": True, "address": projects.LEAD_ALIAS_PREFIX + str(found.id)} if found else {}
    return {} if butler.traits_of(job).lists_supplements else {"topic": True}


def steer_target(runtime, state_dir, chat_id, message_id):
    """(job id, topic id) of the live task of ``chat_id`` the message went into as a supplement; None without one. A
    topic's message that went in as it arrived (app/steering.py steer_on_arrival) is the delivery of its queued card."""
    from covey.app.steering import QUEUED_STEER
    from covey.core.deliveries import delivery_path, read_delivery

    with runtime.jobs.lock:
        jobs = [job for job in runtime.jobs.by_id.values() if job.chat_id == chat_id]
    for job in sorted(jobs, key=lambda job: -int(job.job_id)):
        if delivery_path(state_dir, job.topic_id, message_id).exists():
            return int(job.job_id), job.topic_id
        record = read_delivery(delivery_path(state_dir, job.topic_id, QUEUED_STEER + message_id))
        if record and record.get("job_id") == job.job_id:
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

    runtime.sources.attach(ctx.state_dir)
    runtime.sources.note(Watched(message_id, chat_id, app.spec.name, str(ctx.topic_id or ""), stamp,
                                 text=str(text or "")[:4000], words=message_words(text), steer=steer,
                                 kind="steer" if steer else "task"))


def note_kind(runtime, app_name, ctx, message_id, chat_id, sender, text, kind) -> None:
    """The owner's message ``message_id`` was taken in as the answer to a waiting question (``kind`` ``answer``) or kept as a
    held note (``held``), by the Harness ``app_name`` (app/interaction.py): watched for its recall."""
    if (not enabled() or runtime.in_runner or not str(message_id).startswith("om_")
            or not runtime.access.is_owner(sender)):
        return
    from covey.core.attachments import message_words

    runtime.sources.attach(ctx.state_dir)
    runtime.sources.note(Watched(message_id, chat_id, app_name, str(ctx.topic_id or ""), "", text=str(text or "")[:4000],
                                 words=message_words(text), kind=kind))


class SourceWatch:
    """The feature: its thread, and what a recall or an edit does. ``topics``: the Bridge's Topics (its apps)."""

    def __init__(self, runtime, topics, messenger, voice) -> None:
        self.runtime, self.topics, self.messenger, self.voice = runtime, topics, messenger, voice

    def threads(self):
        return (("source-watch", self.run),) if enabled() else ()

    def run(self, ctx) -> None:
        self.runtime.sources.attach(ctx.state_dir)  # What a restart left: read back from the first round on.
        every(self.runtime, WATCH_SECONDS, self.round, args=(ctx,), isolate="source-watch")

    # -- the rounds ----------------------------------------------------------------------------------------------

    def round(self, ctx, now=None) -> None:
        from covey.core.jobs import running_job_records
        from covey.feishu.message_read import fetch_message_detail

        now = time.time() if now is None else now
        book = self.runtime.sources
        book.attach(ctx.state_dir)
        running = {turn_source.origin(record).get("message_id") for record in running_job_records(Path(ctx.state_dir))}
        for item in book.snapshot():
            if now - item.added > MAX_AGE_SECONDS:
                book.drop(item.message_id)
                continue
            if self.task_of(item) is not None or item.message_id in running or self.note_open(ctx, item):
                if item.ended:
                    item.ended = 0.0
                    book.save()
            elif not item.ended:
                item.ended = now
                book.save()
            elif now - item.ended > GRACE_SECONDS:
                book.drop(item.message_id)
                continue
            what, words, stamp = read_back(fetch_message_detail(item.message_id, ctx.profile), item.message_id, item.stamp)
            try:
                if what == "recalled":
                    self.runtime.log(f"source recalled message={item.message_id[-10:]}")
                    try:
                        if book.claim(item, "recall", "all"):  # Claimed first: a restart in the middle never says it twice.
                            self.recalled(ctx, item)
                    finally:
                        book.drop(item.message_id)
                elif what == "edited" and same_words(words, item.words):
                    item.stamp = stamp  # Touched (a thread under it), not edited: nothing to tell its task.
                    book.save()
                    self.runtime.log(f"source touched message={item.message_id[-10:]} words unchanged")
                elif what == "edited":
                    item.stamp, item.words = stamp, words
                    book.save()
                    self.runtime.log(f"source edited message={item.message_id[-10:]}")
                    self.edited(ctx, item, words)
            except Exception as exc:  # noqa: BLE001 - one message's failure leaves the others to their round
                self.runtime.log(f"source {what} failed message={item.message_id[-10:]}: {type(exc).__name__}")

    def note_open(self, ctx, item) -> bool:
        """Whether ``item`` is a held note still held (or kept for the next round): it is read back while it is."""
        if item.kind != "held":
            return False
        from covey.core.state import keyed_json_path, read_object

        token = hashlib.sha256(f"{item.topic_id}-{item.message_id}".encode()).hexdigest()[:32]
        return read_object(keyed_json_path(ctx.state_dir, "held-notes", token)).get("status") in ("held", "next_round")

    @staticmethod
    def base_of(app, ctx):
        """``app``'s own context (its Harness's), else the round's."""
        return getattr(app.harness, "context", None) or ctx

    def app_of(self, item):
        apps = getattr(self.topics, "apps", {}) or {}
        return apps.get(item.app) or next(iter(apps.values()), None)

    def task_of(self, item):
        """The live task ``item`` is: its own (keyed by the message) or the one it went into as a supplement."""
        if item.kind in ("answer", "held"):
            return None
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
        if item.kind in ("answer", "held"):
            return self.recalled_reply(app, base, item)
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

    def recalled_reply(self, app, base, item) -> None:
        """The recall of a message that was taken as an answer (said so in a line: it stays) or kept as a held note
        (withdrawn, unless it went with an answer already)."""
        status = ""
        if item.kind == "held":
            target = dataclasses.replace(base, topic_id=item.topic_id)
            status = app.questions.notes.withdraw_message(target, item.message_id)
            self.runtime.log(f"source recall held note message={item.message_id[-10:]} status={status or 'none'}")
            if status in ("", "withdrawn"):
                return
        self.say(base, item.chat_id, item.topic_id, ANSWERED_LINE, "source-answered", item.message_id[-16:])

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
        dropped += self.take_back_entries(target, record, key)
        if lines or dropped:
            self.runtime.log(f"source recall took back memory={lines} knowledge={dropped} job=#{record.get('job_id')}")

    def take_back_entries(self, target, record, key) -> int:
        """The entries of core/memstore.py the recalled turn ``key`` wrote (its origin's ``job``), not one the owner wrote
        himself: voided (a 墓碑, as 不对 leaves it). Read in the global memory, the turn's topic and its project only."""
        if not key:
            return 0
        from covey.core import memstore, projects

        state, topic = Path(target.state_dir), str(record.get("topic_id") or "")
        taken = 0
        try:
            found = projects.topic_project(state, topic) if topic else None
            pool = memstore.entries(state, scope="global", statuses=("active", "pending"))
            if topic:
                pool += memstore.entries(state, scope="topic", owner_key=topic, statuses=("active", "pending"))
            if found is not None:
                pool += memstore.entries(state, scope="project", owner_key=found.id, statuses=("active", "pending"))
            for entry in pool:
                origin = entry.origin or {}
                if entry.by in ("owner", "web", "import") or key not in (origin.get("job"), origin.get("job_key")):
                    continue
                memstore.void(state, entry.ref, who="owner", because={"message_id": str(record.get("message_id") or "")})
                taken += 1
        except Exception as exc:  # noqa: BLE001 - the rest of the recall goes on
            self.runtime.log(f"source recall memory entries failed: {type(exc).__name__}")
        return taken

    def mark_opened(self, app, base, item, record) -> None:
        """Each topic the butler's turn ``record`` (None: it left none) opened, forked or handed work to, and each topic of
        a job downstream of the message (turn_source.downstream): 来源消息已撤回 in its thread, with 停止 while one of its
        tasks goes."""
        from covey.cards.source_note import RECALLED_LINE, recalled_card
        from covey.core import butler_opened
        from covey.core.jobs import RECALLED
        from covey.core.state import read_object
        from covey.core.topics import topic_file

        state = Path(base.state_dir)
        topics = {}  # topic -> the chat it is in
        for _, opened in butler_opened.by_job(state, str(record.get("key") or "")) if record else ():
            topic = butler_opened.topic_of(opened)
            if butler_opened.answered(opened) and topic:
                topics.setdefault(topic, item.chat_id)
        running, ended = self.downstream_of(state, item)
        for found in (*running, *ended):
            if found.get("topic_id") and found["topic_id"] != item.topic_id:
                topics.setdefault(str(found["topic_id"]), str(found.get("chat_id") or item.chat_id))
        for topic, chat in topics.items():
            root = str(read_object(topic_file(state, topic)).get("root_id") or "")
            if not root.startswith("om_") or not self.runtime.sources.claim(item, "recall-mark", topic):
                continue
            live = self.runtime.jobs.open_in_topic(topic, chat)
            job = live[-1] if live else None
            card = recalled_card(job.job_id if job else None, job.message_id if job else "", RECALLED)
            key = self.voice.safe_key(self.voice.key("source-recalled", f"{item.message_id[-16:]}-{topic[:16]}"))
            self.messenger.send_message(base.profile, chat, RECALLED_LINE, key, reply_to_message_id=root,
                                        voice=self.voice, card=card)
            self.runtime.log(f"source recall marked topic={topic[:12]} live={len(live)}")

    @staticmethod
    def downstream_of(state, item):
        """(running, finished) job records whose 轮次来源 names the message ``item`` as its origin, other than its own task.
        A lead's hand-on is not among them: it names a topic, not the message."""
        gone = lambda records: [r for r in records if r.get("message_id") != item.message_id]  # noqa: E731
        if item.kind != "task":
            return [], []
        return (gone(turn_source.downstream(state, item.message_id)),
                gone(turn_source.finished_downstream(state, item.message_id)))

    # -- an edit -------------------------------------------------------------------------------------------------

    def edited(self, ctx, item, words) -> None:
        app = self.app_of(item)
        if app is None or not str(words or "").strip() or item.kind in ("answer", "held"):
            return
        base = self.base_of(app, ctx)
        job = self.task_of(item)
        took = job is not None and self.supplement(app, base, job, edited_text(words), f"edit-{item.message_id}-{item.stamp}")
        item.text = str(words)[:4000]
        if not took:
            self.tell_edited(base, item, words)
        self.edited_downstream(ctx, app, base, item, words)

    def edited_downstream(self, ctx, app, base, item, words) -> None:
        """The edit to each topic downstream of the message (one thing per topic, claimed first): the running job takes it as
        a supplement; a job that ended, or refuses one, gets a line in its thread."""
        running, ended = self.downstream_of(Path(base.state_dir), item)
        action, seen = f"edit-{item.stamp}", set()
        for record in (*running, *ended):
            topic = str(record.get("topic_id") or "")
            if not topic or topic in seen or topic == item.topic_id:
                continue
            seen.add(topic)
            if not self.runtime.sources.claim(item, action, topic):
                continue
            job = self.runtime.jobs.find(int(record.get("job_id") or 0)) if record in running else None
            target = self.app_of_job(job, app)
            took = (job is not None and job.topic_id == topic and not self.ended(job)
                    and self.supplement(target, self.base_of(target, ctx), job, edited_text(words),
                                        f"edit-{item.message_id}-{item.stamp}"))
            if not took:
                text = (f"你改了那条消息：「{preview(words)}」。交到这个话题的那一轮是按改之前的内容做的，没有重做；"
                        "要按新内容做，直接再说一句。")
                self.say(base, str(record.get("chat_id") or item.chat_id), topic, text, "source-edited",
                         f"{item.message_id[-16:]}-{item.stamp}-{topic[:16]}")

    def app_of_job(self, job, fallback):
        """The Harness app a job runs on, ``fallback`` when it is not one of this Bridge's."""
        from covey.core.jobs import harness_of

        apps = getattr(self.topics, "apps", {}) or {}
        return apps.get(harness_of(job)) or fallback if job is not None else fallback

    def tell_edited(self, base, item, words) -> None:
        """The line of an edit no task takes: in the topic's thread, or at the top level of the butler's chat."""
        text = (f"你改了这条消息：「{preview(words)}」。这一轮是按改之前的内容做的，没有重做；"
                "要按新内容做，直接再说一句。")
        self.say(base, item.chat_id, item.topic_id, text, "source-edited", f"{item.message_id[-16:]}-{item.stamp}")

    def say(self, base, chat_id, topic_id, text, name, suffix) -> None:
        """``text`` in the thread of the topic ``topic_id`` (at the top level of the chat for the butler's), once by the key ``name`` and ``suffix`` make."""
        from covey.core.butler import is_butler_topic
        from covey.core.state import read_object
        from covey.core.topics import topic_file

        root = ""
        if topic_id and not is_butler_topic(topic_id, chat_id):
            root = str(read_object(topic_file(Path(base.state_dir), topic_id)).get("root_id") or "")
        self.messenger.send_message(base.profile, chat_id, text, self.voice.safe_key(self.voice.key(name, suffix)),
                                    reply_to_message_id=root if root.startswith("om_") else "", voice=self.voice)

    def supplement(self, app, base, job, text, mid) -> bool:
        """``text`` into ``job`` as a supplement of its own (``mid``); whether it went or may have (rule 6: never again)."""
        from covey.core.jobs import context_for_job
        from covey.core.steer import SteerRefused, SteerUncertain

        target = context_for_job(dataclasses.replace(base, topic_id=job.topic_id), job)
        source = turn_source.of(job)  # Should the turn never read it, it is queued as the turn would have been (steering requeue).
        requeue = {"text": text, "prompt": text, **({"source": dict(source)} if isinstance(source, dict) and source else {}),
                   **handed_to(base.state_dir, job)}
        try:
            app.steering.deliver_steer(target, job, text, mid, requeue=requeue)
            return True
        except SteerUncertain:
            self.runtime.log(f"source supplement uncertain job=#{job.job_id}")
            return True
        except SteerRefused as exc:
            self.runtime.log(f"source supplement refused job=#{job.job_id}: {exc}")
            return False
