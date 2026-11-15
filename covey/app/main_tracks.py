"""多路并行 at run time (docs/main-thread.md 多路并行; COVEY_MAIN_TRACKS=1 and the main thread on): where a message of the
owner's goes when the main thread's family (core/main_tracks.py) has live tasks, and what a track's turn starts from.

- ``Tracks.handle``, called by app/main_thread.py handle_event before its usual path: with no live task in the family
  it answers False and that path runs as it always did; else it builds the router's input (core/track_route.py Message:
  the live tasks, what the message quotes), asks the router, asks the small model (app/track_judge.py) when the
  rules cannot tell (no lock held), and acts: ``stop`` (the quoted or newest track's task, no task of its own),
  ``merge`` (a supplement to that track's task, or its answer when it waits for one) and ``new`` (``pick_slot``,
  ``enter``: the slot's topic.json, the message's alias moved to it, then the usual submission).
- A merge the task refuses (it ended, or cannot take supplements) becomes a new track instead of queueing behind it
  (``MainThread.supplement`` asks ``fresh``).
- ``pick_slot``: slot 0 when it is free, else the lowest free slot 1..SLOTS; at COVEY_MAIN_TRACKS_MAX live tasks, the
  slot with the fewest tasks under way, to queue behind it (the topic's FIFO; no new state, the scheduler is not touched).
- ``fork_options`` and ``parallel_note``, in the task runner of a track's turn (app/main_thread.py main_turn): the session
  to fork and where, and what the other tracks are doing now. ``release_pointer``, when the turn ended
  (app/worker.py): the track forgets its session unless it waits for the owner's answer.

The reactions on the owner's message (app/main_reactions.py) hang on ``Tracks.noted``: 排队中 for a message that queues
behind a track, 并进 for one that joined a task and, when he quotes it with 单独答, off again. The stop's reply says how many
other tracks are still going (``Tracks.stop``).
"""
from __future__ import annotations

import dataclasses
import functools
import json
import time
from pathlib import Path

from covey.app import main_reactions, runner_ipc, track_judge
from covey.core import main_tracks as core
from covey.core import main_thread as contract
from covey.core import track_route as route
from covey.core.deliveries import delivery_path, read_delivery
from covey.core.harness import backend_capabilities
from covey.core.history_storage import recent_history
from covey.core.jobs import JOB_STOP_ACTION_KIND
from covey.core.redaction import topic_redaction
from covey.core.session_state import capture_session_state, compare_and_set_session, read_session_id
from covey.core.state import epoch_seconds, read_object
from covey.core.store import directory_store
from covey.core.topics import message_topic, move_topic_message, topic_file
from covey.app.runtime import job_stage

WAITING_FRESH = 6 * 3600  # A task waiting for the owner's answer this long still holds its track, and is routed to.
NOTE_LINE = 60  # Characters of a track's request in the parallel note.
NOTE_ROWS = 6
BARE_SECONDS = contract.HOLD_SECONDS  # The words that follow a forward or a file this soon join it (no router).
FORK_NOTE = (
    "这一轮是管家并行的一路：主人同时在别的路上还有事，那些路各自回答，不归你。上面会话里没有答完的提问（如果有）"
    "也不归你，只处理下面这条新消息。")


@dataclasses.dataclass
class Entry:
    """A live task of the family: what the router sees (``live``) and the registry's job (None: it waits for an answer
    in a task that left the registry, a record only)."""
    live: route.LiveJob
    job: object = None
    ids: tuple = ()  # The messages that stand for it: the owner's, its card, the reply target.


class Tracks:
    """The router's side of the main thread (app/main_thread.py ``MainThread.tracks``).

    - ``main``: the MainThread (keep_track_topic, hold_for), the owner of this object.
    """

    def __init__(self, main) -> None:
        self.main = main
        self.runtime = main.runtime
        self.bare = {}  # chat -> (slot, at): where the last forward or file went, for the words right after it.
        self.last_owner = {}  # job id -> when the owner last wrote to its track.

    @staticmethod
    def enabled() -> bool:
        """COVEY_MAIN_TRACKS=1, and the main thread on in the mode Personal Agent (the switch alone does not check it,
        core/main_tracks.py enabled)."""
        return core.enabled() and contract.enabled()

    # -- what is live ------------------------------------------------------------------------------------------------

    def entries(self, ctx, chat, now) -> list:
        """The family's tasks that have not ended: those in this process's registry (queued, starting, running, paused),
        and the records of tasks waiting for the owner's answer that left it (``WAITING_FRESH``; those of the main
        thread's own slot only while something else is live). A task that is no more than a record has ``job`` None."""
        state, found = ctx.state_dir, []
        ids = core.family_ids(chat)
        for slot, topic in enumerate(ids):
            for job in self.runtime.jobs.topic_jobs(topic, chat):
                stage = job_stage(job)
                if stage == "ended":
                    continue
                started = float((getattr(job, "timings", None) or {}).get("received") or 0)
                found.append(Entry(route.LiveJob(
                    track=slot, job_id=str(job.job_id), phase=stage, request=str(job.user_text or ""), started_at=started,
                    last_owner_at=max(started, self.last_owner.get(job.job_id, 0)), number=int(job.job_id)), job,
                    tuple(filter(None, (job.message_id, getattr(job.progress, "message_id", ""), job.reply_to_message_id)))))
        have = {entry.live.job_id for entry in found}
        records = []
        for slot, topic in enumerate(ids):
            if slot == 0 and not found:
                continue
            for _, record in directory_store(state / "jobs").query(topic_id=topic, chat_id=chat, status="waiting_user"):
                at = epoch_seconds(record.get("updated_at") or record.get("created_at"))
                if at is None or now - at > WAITING_FRESH or str(record.get("job_id")) in have:
                    continue
                records.append(Entry(route.LiveJob(
                    track=slot, job_id=str(record.get("job_id")), phase=route.PAUSED, request=str(record.get("user_text") or ""),
                    started_at=at, last_owner_at=at, number=int(record.get("job_id") or 0)), None,
                    tuple(filter(None, (record.get("key"), record.get("card_message_id"), record.get("reply_to_message_id"))))))
        return found + records

    # -- the router's input --------------------------------------------------------------------------------------------

    def quote(self, ctx, event, chat, entries) -> route.Quote:
        """What the message quotes, from the topic aliases, the live tasks' messages and the supplement delivery
        records (nothing is asked of Feishu). A message of the owner's that triggered a live task counts as that task's
        card (the router's rule 4); one merged into a task is ``merged``; any other message of the family or of a topic
        that went is ``ended``; what no alias knows is ``unknown``."""
        parent = quoted_id(event)
        if not parent:
            return route.Quote()
        state = ctx.state_dir
        for slot, topic in enumerate(core.family_ids(chat)):
            saved = read_delivery(delivery_path(state, topic, parent))
            if saved and saved.get("job_id") is not None:
                return route.Quote(route.Q_MERGED, slot, str(saved["job_id"]), str(saved.get("text") or ""))
        for entry in entries:
            if parent in entry.ids:
                return route.Quote(route.Q_LIVE, entry.live.track, entry.live.job_id)
        topic = message_topic(state, chat, parent)
        if not topic:
            return route.Quote(route.Q_UNKNOWN)
        slot = core.track_of(topic, chat)
        waiting = [entry for entry in entries if entry.live.track == slot and entry.live.phase == route.PAUSED]
        if waiting:  # A question card of a track that asks (it has no id of its own here): the answer.
            return route.Quote(route.Q_LIVE, slot, waiting[0].live.job_id)
        return route.Quote(route.Q_ENDED, slot)

    def decide(self, ctx, app, event, chat, text, bare, entries, now) -> route.Decision:
        """The router's decision, the small model asked when the rules say so (never under a lock) and the live tasks
        read again after it."""
        message = route.Message(
            text=text, live=tuple(entry.live for entry in entries), quote=self.quote(ctx, event, chat, entries),
            now=now, bare=bare, judge=track_judge.mode() != "off")
        decision = route.route(message)
        if decision.why == "9" and decision.action in (route.ASK_JUDGE, route.NEW) and not bare and text.strip():
            held = self.bare.get(chat)  # Words right after a forward, a file or a link: they go where it went.
            if held and now - held[1] <= BARE_SECONDS and not quoted_id(event):
                target = self.newest_of(entries, held[0])
                if target is not None:
                    return route.Decision(route.MERGE, held[0], "bare", target.live.job_id)
        if decision.action != route.ASK_JUDGE:
            return decision
        redacted = False
        try:
            redacted = topic_redaction(ctx.state_dir, core.root_topic_id(chat))
        except ValueError:
            redacted = True
        verdict = track_judge.judge(message, redacted=redacted, backend=app.harness.backend, ctx=ctx, log=self.runtime.log)
        if verdict.route == "merge" and verdict.target is not None:
            fresh = self.entries(ctx, chat, now)  # The model took a while: the task may have ended meanwhile.
            target = self.newest_of(fresh, verdict.target)
            if target is not None:
                return route.Decision(route.MERGE, verdict.target, "judge", target.live.job_id,
                                      answers=target.live.phase == route.PAUSED)
        return route.Decision(route.NEW, None, "judge")

    @staticmethod
    def newest_of(entries, slot):
        rows = [entry for entry in entries if entry.live.track == slot]
        return max(rows, key=lambda entry: (entry.live.started_at, entry.live.number), default=None)

    # -- the slots ---------------------------------------------------------------------------------------------------

    def pick_slot(self, ctx, chat, entries) -> tuple:
        """(slot, free): where a new track starts. Slot 0 when it is free, else the lowest free slot 1..SLOTS; ``free``
        False means it queues behind that slot's tasks. At COVEY_MAIN_TRACKS_MAX tasks under way (waiting ones are not),
        or with no slot free, the slot with the fewest tasks under way (the lowest on a tie)."""
        under_way = {}
        held = set()
        for entry in entries:
            slot = entry.live.track
            held.add(slot)
            if entry.live.phase != route.PAUSED:
                under_way[slot] = under_way.get(slot, 0) + 1
        free = [slot for slot in range(core.SLOTS + 1) if slot not in held]
        if sum(under_way.values()) < core.limit() and free:
            return free[0], True
        pool = list(under_way) or list(range(core.SLOTS + 1))
        return min(pool, key=lambda slot: (under_way.get(slot, 0), slot)), False

    def enter(self, ctx, app, chat, message, slot, *, free=True):
        """The context of ``message`` as a turn of ``slot``: the slot's topic.json kept up to date, a free track's session
        pointer cleared (an old one a waiting question left is never resumed by a new request), the message's alias moved
        to the slot. The event is read from the alias alone (a quoted message's own chain is not the slot's)."""
        topic = core.track_topic_id(chat, slot)
        if slot:
            self.main.keep_track_topic(ctx, app, chat, slot)
            if free:
                clear_pointer(app.harness, ctx.state_dir, topic)
        move_topic_message(ctx.state_dir, topic, chat, message)
        return app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})

    # -- the actions ---------------------------------------------------------------------------------------------------

    def handle(self, ctx, app, event, chat, message, *, bare, text) -> bool:
        """True when the message was handled here; False: no task of the family is live, the usual path (slot 0) runs."""
        started = time.monotonic()
        now = time.time()
        entries = self.entries(ctx, chat, now)
        if not entries:
            self.remember_bare(chat, 0, bare, now)
            return False
        decision = self.decide(ctx, app, event, chat, text, bare, entries, now)
        self.runtime.log(f"track route={decision.action} why={decision.why} ms={round((time.monotonic() - started) * 1000)} "
                         f"live={len(entries)}")
        if decision.action == route.LEGACY:
            self.remember_bare(chat, 0, bare, now)
            return False
        if decision.action == route.STOP:
            self.stop(ctx, app, chat, message, decision, entries)
        elif decision.action == route.MERGE:
            self.merge(ctx, app, event, chat, message, decision, entries, bare, now)
        else:
            self.start(ctx, app, event, chat, message, decision, bare, now)
        return True

    def stop(self, ctx, app, chat, message, decision, entries) -> None:
        """The stop word's task stops (app/task_controls.py: a queued one is cancelled, a running one asked to stop); the
        message itself starts nothing and reaches no model. A task in a live task runner is stopped there, by the click
        on its card's 停止 forwarded to the runner as the button does (app/runner_ipc.py ``forward_card_action``, as
        app/proxy.py ``stop_job``): the runner owns the job, and a stop only set in this Bridge's copy of it reached
        nobody (2026-11-15: both tasks ran their 90 s on and sent their answers)."""
        entry = next((entry for entry in entries if entry.live.job_id == decision.job_id), None)
        topic = core.track_topic_id(chat, entry.live.track if entry else decision.target or 0)
        move_topic_message(ctx.state_dir, topic, chat, message)
        stop_ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        going = {other.live.track for other in entries
                 if other.live.job_id != decision.job_id and other.live.phase != route.PAUSED}
        others = len(going - {entry.live.track} if entry else going)
        job = entry.job if entry else None
        action = {"kind": JOB_STOP_ACTION_KIND, "job_id": decision.job_id, "message_id": job.message_id if job else ""}
        if others > 0:
            action["others"] = str(others)  # The runner's reply says it (task_controls.py ``stop_action``).
        click = {"chat_id": chat, "message_id": message, "event_id": f"track-stop-{message}",
                 "operator_id": self.runtime.owner_open_id or "", "action_value": action}
        if job is not None and runner_ipc.forward_card_action(self.runtime, stop_ctx, click, action):
            self.runtime.log(f"track stop job=#{decision.job_id} sent to its task runner")
            return
        app.controls.cancel_reply_job_from_card(stop_ctx, action, chat, message, others=others)

    def merge(self, ctx, app, event, chat, message, decision, entries, bare, now) -> None:
        """A supplement to the task (or, when it waits for one, its answer: the usual continuation). The task that ended
        meanwhile, or refuses it, makes it a track of its own (``fresh``)."""
        entry = next((entry for entry in entries if entry.live.job_id == decision.job_id), None)
        slot = decision.target if decision.target is not None else 0
        step_ctx = self.enter(ctx, app, chat, message, slot, free=False)
        if decision.job_id.isdigit():
            self.last_owner[int(decision.job_id)] = now
        self.remember_bare(chat, slot, bare, now)
        job = None if decision.answers or entry is None else entry.job
        supplement = functools.partial(self.main.supplement, app, chat, bare=bare, target=job, fresh=(
            None if decision.answers or job is None else lambda: self.fresh(ctx, app, chat, message, bare, now)))
        app.handle_event(event, step_ctx, supplement=supplement, routed=True)

    def start(self, ctx, app, event, chat, message, decision, bare, now) -> None:
        """A request of its own on a track."""
        entries = self.entries(ctx, chat, now)
        slot, free = self.pick_slot(ctx, chat, entries)
        step_ctx = self.enter(ctx, app, chat, message, slot, free=free)
        self.remember_bare(chat, slot, bare, now)
        if not free:
            self.noted("queued", chat, message, ctx=ctx, app=app)
        if decision.text:  # Rule 3: it was merged into a task before; the owner wants it answered on its own.
            self.noted("alone", chat, quoted_id(event), ctx=ctx, app=app)
            event = {**event, "message_type": "text", "content": json.dumps({"text": decision.text}, ensure_ascii=False)}
        app.handle_event(event, step_ctx, hold=self.main.hold_for(chat, bare) if slot == 0 else 0, routed=True)

    def fresh(self, ctx, app, chat, message, bare, now):
        """The context of ``message`` as a track of its own, for a supplement that was refused: ``MainThread.supplement``
        gives it back to the submission. Slots read again: the task that refused may have ended."""
        slot, free = self.pick_slot(ctx, chat, self.entries(ctx, chat, now))
        step_ctx = self.enter(ctx, app, chat, message, slot, free=free)
        self.remember_bare(chat, slot, bare, now)
        if not free:
            self.noted("queued", chat, message, ctx=ctx, app=app)
        return step_ctx

    def remember_bare(self, chat, slot, bare, now) -> None:
        if bare:
            self.bare[chat] = (slot, now)
        elif chat in self.bare and now - self.bare[chat][1] > BARE_SECONDS:
            self.bare.pop(chat, None)

    def noted(self, kind, chat, message, *, ctx=None, app=None) -> None:
        """The reactions on the owner's ``message`` (app/main_reactions.py), none of them held up by a failure and none
        under a lock: ``kind`` "queued" (it waits behind a track's tasks: 排队中), "merged" (it joined a task: 并进, which
        stays) or "alone" (``message`` is a merged one the owner quoted to have it answered on its own: 并进 off). Without
        ``ctx`` and ``app`` (where the state and the messenger are), or with the switch off, nothing."""
        if ctx is None or app is None or not message or not self.enabled():
            return
        messenger, log = app.messenger, self.runtime.log
        if kind == "queued":
            main_reactions.queue(messenger, ctx.state_dir, ctx.profile, chat, message, log)
        elif kind == "merged":
            main_reactions.merge(messenger, ctx.state_dir, ctx.profile, chat, message, log)
        elif kind == "alone":
            main_reactions.split(messenger, ctx.profile, message, log)


# -- a track's turn (in the task runner) -------------------------------------------------------------------------------

def quoted_id(event) -> str:
    """The message ``event`` quotes. The event is the consumer's, before ``enrich_incoming_event``: a lark-cli that names
    the reply context (1.0.69 and later, feishu/events.py REPLY_CONTEXT) gives ``reply_to`` and no ``parent_id`` at all,
    so reading ``parent_id`` alone saw no quote and a "停止" quoting a track stopped the newest one (2026-11-15)."""
    return str(event.get("parent_id") or event.get("reply_to") or "")


def clear_pointer(harness, state_dir, topic_id) -> None:
    """Forget the native session of the topic ``topic_id`` (only the pointer; the transcript stays)."""
    path = topic_file(state_dir, topic_id).parent / harness.spec.session_filename
    pattern = harness.spec.session_pattern
    revision, session = capture_session_state(path, pattern)
    if session:
        compare_and_set_session(path, pattern, revision, session, None)


def fork_options(ctx, harness, chat, log) -> dict | None:
    """The options of a track's first run: ``fork_session`` the main thread's session (slot 0's pointer) and, when a turn
    of slot 0 is under way, ``fork_at`` the entry before its question, so no half of it is carried over (the spike:
    docs/main-thread.md 多路并行). None: no fork can be made (slot 0 has no session, another workspace, the backend
    cannot, the question's place is not found): the turn starts on a new session with the conversation (``context_pack``)."""
    state = ctx.state_dir
    root = core.root_topic_id(chat)
    folder = topic_file(state, root).parent
    try:
        if not backend_capabilities(harness.backend, ctx).native_fork:
            return None
        if str(read_object(folder / "topic.json").get("workdir") or "") != str(ctx.workdir):
            return None  # A session belongs to a directory.
        session = read_session_id(folder / harness.spec.session_filename, harness.spec.session_pattern)
        if not session or (ctx.sessions_dir and not (Path(ctx.sessions_dir) / f"{session}.jsonl").is_file()):
            return None
        under_way = [record for _, record in directory_store(state / "jobs").query(
            topic_id=root, chat_id=chat, status=frozenset({"running", "cancel_requested"}))]
        record = max(under_way, key=lambda row: int(row.get("job_id") or 0), default=None)
        if record is None or not record.get("prompt_uuid") or record.get("prompt_session") != session:
            return {"fork_session": session}  # Nothing under way, or its question is not in the file yet: all of it.
        if not backend_capabilities(harness.backend, ctx).rewind:
            return None
        keep = str(harness.backend.rewind_point(ctx, session, record["prompt_uuid"]).get("keep") or "")
        return {"fork_session": session, "fork_at": keep} if keep else None
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        log(f"track fork not possible: {type(exc).__name__}")
        return None


def context_pack(ctx, chat) -> list:
    """The conversation a track on a new session starts from instead of a fork: the main thread's recent history
    (core/history_storage.py recent_history)."""
    return recent_history(topic_file(ctx.state_dir, core.root_topic_id(chat)).parent / "history.jsonl")


def parallel_note(state_dir, chat, topic_id, now=None) -> str:
    """What the family's other tracks are doing now, one line each, from the job records ("" when none): the note at the
    head of a track's first message (B-SPEC 4.2; the ledger's block of what they answered comes with it)."""
    now = time.time() if now is None else now
    rows = []
    for slot, topic in enumerate(core.family_ids(chat)):
        if topic == topic_id:
            continue
        for _, record in directory_store(Path(state_dir) / "jobs").query(
                topic_id=topic, chat_id=chat, status=frozenset({"queued", "running"})):
            request = " ".join(str(record.get("user_text") or "").split())
            rows.append((int(record.get("job_id") or 0), f"- #{record.get('job_id')}「{request[:NOTE_LINE]}"
                         f"{'…' if len(request) > NOTE_LINE else ''}」· {'在排队' if record.get('status') == 'queued' else '在做'}"))
    if not rows:
        return FORK_NOTE
    lines = [text for _, text in sorted(rows)[:NOTE_ROWS]]
    return FORK_NOTE + "\n此刻另几路在做：\n" + "\n".join(lines)


def release_pointer(ctx, job, harness, runtime) -> None:
    """A track's turn ended: the track forgets its session so the next request forks the main thread's again, unless it
    waits for the owner's answer (the answer's turn continues it). A no-op for every other topic."""
    if not core.track_of(job.topic_id, job.chat_id):
        return
    try:
        with runtime.session_lock:
            clear_pointer(harness, ctx.state_dir, job.topic_id)
    except (OSError, ValueError) as exc:
        runtime.log(f"track session pointer not cleared job=#{job.job_id}: {type(exc).__name__}")
