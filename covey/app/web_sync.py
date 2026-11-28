"""网页工作台同步 (docs/web.md, COVEY_WEB=1 and COVEY_WEB_CONFIG): pushes the state of the owner's projects, topics, turns and
questions to the web page (网页应用) and fetches what the page sent, which app/web_ops.py does. Off by default: without the switch
there is no thread and no request.

- ``WebSync`` (app/unified.py PARTS ``web_sync``, a feature object: ``threads()``, none without the switch): five supervised threads.
  ``web-push`` (the quick looks, ``push_hot``) builds the hot items (``Collector.collect_hot``, app/web_collect.py), keeps those whose ``rev``
  (core/web_sync.py) differs from what was sent, and posts them to /sync/upsert in batches, then a batch of what the pass read; ``web-pass``
  (``pass_once``) reads the full pass a slice a step into that backlog; ``web-inbox`` asks /sync/inbox, writes each operation's seq and op_id
  into the ledger before it is done and hands it to its lane (``OpLanes``: a topic's in order, topics side by side), which does it
  (``Ops.run``), acknowledges it at /sync/ack at once and then runs what it left for after the ack (app/card_lanes.py ``after_ack``);
  ``web-touch`` (``touch_once``) builds what an operation touched and pushes it at once, not at the quick look's turn; ``web-reread``
  (``reread_once``) reads again, one at a time and within bounds, a topic the quick look leaves out while a read of it is stuck on the
  network disk (2026-11-26 T837). A failed request backs off (2, 4 ... 30 seconds) and touches no other thread.
- What is read, and how often: every ``push_seconds``, and at once when this process wrote a job or a question (core/store.py listen: an
  answer on a card), only the running tasks, the pending questions and the tasks waiting for 开始 on a launch card (three index queries, the
  records they name; core/board_read.py starts), plus the topics that stopped running a moment ago or that the page just acted on, and the
  projects that changed; FULL_SECONDS after the last pass ended a full pass of all topics of the last TOPIC_DAYS days, PASS_SLICE of them a
  step in its own thread (2026-11-23 T673: in the one push thread a pass's read stuck on the network disk held the hot topics up a minute at
  a time), the deletions once it ends. The pass builds only a topic whose inputs changed since its last build or whose items the page does
  not have, and each at the latest REBUILD_SECONDS after its last build (app/web_collect.py Collector.inputs, unchanged; 2026-11-23 T678:
  every pass built the 598 live topics again, 44-83 s and up to 277 thousand file calls warm). Each topic's read has a seq: a read older
  than what the page has of a topic is never sent after it.
  A topic's state and "做完没看" are the board's reading (core/board_read.py, app/pins.py
  board_results), not a scan of every job record each second. The butler's conversation (core/butler.py butler_topic_id; the other tracks
  of the family and the leads are not) is one more topic item, ``kind`` "butler", number 0, title 管家, no project, never unseen: its
  turns are its history's (the owner's top-level words, the butler's answers), looked at while its task runs, after an operation of the page
  touched it and in the full look. A running turn's body is its progress notes as the trace file has them, each time it changes.
- A project's lead (COVEY_PROJECTS=1) is looked at the same way and is a topic of ``kind`` "lead" (title 负责人, number 0, its project's id as
  ``project_key``): the owner's words and its answers, a wake-up turn left out (its reply only when the lead meant it for the owner), and
  a line of the page's own (role event) for a member's end and for what a silent wake-up started; ``started_topics`` says which turn started
  which member. A turn's process, plan and figures (app/web_turns.py) and a question's times ride on the items; the memory and library rows
  say who wrote them (data contract v3).
- The ``settings`` items of their own, each built when its few stats moved: 整理专项 (push_tidy), the 记忆 tab of v37 (push_memory), the
  记忆's ``memory:*`` keys of v43/v45 (push_memory_center: one stat of the memory's sentinel; a key gone taken back), 版本与更新
  (push_upgrade) and 账号与额度 (push_quota).
- The key is read from the config's key_file and appears in no log line and no record.

Rule 1 (AGENTS.md): no lock is held across a request; records are read, not locked."""
from __future__ import annotations

import collections
import functools
import itertools
import json
import threading
import time
from pathlib import Path

from covey.app import card_lanes, self_upgrade, web_images, web_memory, web_quota
from covey.app.web_collect import Collector, Shared
from covey.app.web_ops import Ops
from covey.app.web_push_client import Client, WebError
from covey.core import avatars, store, web_sync
from covey.core.loops import Stuck, Worker
from covey.core.state import epoch_seconds, read_object, write_json

FULL_SECONDS = web_sync.FULL_SECONDS  # A full pass starts this long after the last one ended (its own thread reads it a slice a step).
SENT_FILE = "sent.json"  # <state>/web/: what the page took (key -> rev), so a restart sends only what changed since.
SENT_SAVE_SECONDS = 30.0  # It is written at most this often, and when a pass ends.
SENT_KEEP_SECONDS = 86400.0  # Once a day ``sent`` starts empty again, so everything goes once (a page whose database was emptied fills again).
MAX_BACKOFF = 30
STUCK_SECONDS = 60  # A step with no progress in this long is left behind (an NFS read can hold its thread for good); the next runs in a new thread.
# The quick look's own limit: what runs, asks or just ended waits no longer than this for a read stuck on the network disk
# (2026-11-23 T673: an open of a running topic's topic.json held its thread over 2 minutes while other processes read the file in 5 ms).
HOT_STUCK_SECONDS = 15
HOT_WAIT = 5.0  # Seconds a quick look waits for a store's lock (core/store.py bounded) before it gives its step up: another thread's scan may be stuck.
HOT_GAP = 0.25  # The least time between two quick looks, however often this process writes a record.
PASS_PAUSE = 1.0  # Seconds between two steps of a pass (each a slice of PASS_SLICE topics), and between two checks whether one is due.
UPGRADE_PAUSE = 60.0  # Seconds between two looks of the web-upgrade thread whether a check of a newer version is due (app/self_upgrade.py due).
BUSY_EVERY = 60  # A quick look given up for a busy store is logged the first time, then every this many.
MAX_LEFT = 3  # Left-behind steps still stuck: with this many no new thread is started until one of them ends, or LEFT_WAIT has passed.
LEFT_WAIT = 300  # Seconds waited at MAX_LEFT; then the next step runs in a new thread anyway (an open stuck for good must not stop the push).
IMAGES_WAIT = 300  # Seconds images are held back after the page did not take one (push_images).
CENTER_WAIT, CENTER_WAIT_MAX = 60.0, 1800.0  # The memory center's keys after a refused post: tried again this long after, doubled each time.
# After the page's change was pushed (a quick build, before its ack) the pass builds it all again no sooner than this: the page's
# next change within the undo bar's 5 s (an undo) does not find a full build holding the lock (T870 Wave 3.5).
CENTER_REST = 20.0
HELD_LOG_SECONDS = 60.0  # A push that finds its lock held says so at most this often an item (WebSync.lock_held).
CENTER_LOCK_WAIT = 5.0  # A push before the page's ack waits this long for a build the pass is making, then goes without.
INBOX_WAIT = 8  # Seconds a page that knows ``wait`` holds an empty /sync/inbox (it answers ``waited``); within the client's TIMEOUT.
OP_WAIT = 5.0  # Seconds an inbox step waits for the operations it handed to their lanes before it asks again.
PACE_SECONDS = 60.0  # A quick look's, a touch's and a reread's step times are logged once this often (Pace).
# 换线程重读 (web-reread, ``reread_once``): a topic a look left out because its read hangs on the network disk is read again in a thread
# of its own, so a running topic does not stand still on the page for the minutes such a read can last (2026-11-26 T837: T732 from 18:17:05
# to 18:20:43). Bounded so that it cannot pile threads up or hold the other threads: one reread at a time; REREAD_AFTER seconds after the
# topic was left out; then every REREAD_GAP while the quick look would look at it (it runs, asks, just ended, the page acted on it) and once
# more when it stops being so; a topic whose reread hung too waits REREAD_BACKOFF, doubled each time up to REREAD_BACKOFF_MAX, and is not
# read again while that reread lives; with REREAD_MAX_LEFT rereads still stuck none is started.
REREAD_PAUSE = 1.0
REREAD_AFTER = 5.0
REREAD_GAP = 10.0
REREAD_BACKOFF = 60.0
REREAD_BACKOFF_MAX = 600.0
REREAD_MAX_LEFT = 2
PACED = ("push",)  # The loops whose step times are logged by loop(); a touch logs its own (from its wake, not its wait).


class Pace:
    """How long the steps of a loop took, one line a minute: ``web sync push steps=N p50=…ms p90=…ms max=…ms`` (2026-11-25 T765:
    the quick look meant to run every second ran about every 3 s, and nothing said so)."""

    def __init__(self, log, every=PACE_SECONDS, clock=time.monotonic) -> None:
        self.log, self.every, self.clock = log, every, clock
        self.seconds, self.since = {}, {}

    def note(self, label, seconds) -> None:
        now = self.clock()
        self.seconds.setdefault(label, []).append(seconds)
        since = self.since.setdefault(label, now)
        if now - since >= self.every:
            got = sorted(self.seconds.pop(label))
            self.since[label] = now
            at = lambda share: got[min(len(got) - 1, int(len(got) * share))] * 1000  # noqa: E731
            self.log(f"web sync {label} steps={len(got)} p50={at(0.5):.0f}ms p90={at(0.9):.0f}ms max={got[-1] * 1000:.0f}ms")


class OpLanes:
    """The page's operations in order within a lane (a topic, a project, the chat) and side by side across lanes: a thread for each
    lane that has work, gone once it has none (one stuck on the network disk holds its lane, not the others). ``put`` gives the
    Event set once that work has run."""

    def __init__(self, log) -> None:
        self.log = log
        self.lock = threading.Lock()
        self.lanes = {}  # lane -> deque of (work, done); present while its thread runs
        self.count = itertools.count(1)

    def put(self, lane, work) -> threading.Event:
        done = threading.Event()
        with self.lock:
            start = lane not in self.lanes
            if start:
                self.lanes[lane] = collections.deque()
            self.lanes[lane].append((work, done))
        if start:
            threading.Thread(target=self.drain, args=(lane,), name=f"web-op-{next(self.count)}", daemon=True).start()
        return done

    def drain(self, lane) -> None:
        while True:
            with self.lock:
                items = self.lanes[lane]
                if not items:
                    del self.lanes[lane]
                    return
                work, done = items[0]
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - one operation must not end its lane
                self.log(f"web op lane error: {type(exc).__name__}")
            finally:
                done.set()
                with self.lock:
                    items.popleft()


class WebSync:
    """The feature object (``threads()``). ``runtime``: app/runtime.py Runtime; ``env``: app/butler_env.py ButlerEnv; ``hear``: app/unified.py
    web_message, how the owner's words to the butler get in (None: none can); ``login``: app/web_login.py WebLogin, which the ``login`` operation hands its card to (None: it is ``failed``)."""
    name = "web-sync"

    def __init__(self, runtime, env, hear=None, login=None) -> None:
        self.runtime, self.env = runtime, env
        self.client = Client()
        self.shared = Shared()  # The caches and what the page has, shared by the two collectors and kept by a new one (start_over).
        self.images = None  # web_images.WebImages, made once the state directory is known (images_ready)
        self.collector = Collector(env, None, runtime.log, shared=self.shared)  # The quick looks' (push_hot).
        self.passer = Collector(env, None, runtime.log, shared=self.shared)  # The pass's (pass_once).
        # What the page just acted on, built and pushed in a thread of its own (touch_once) instead of waiting for the quick look: that one
        # builds every running topic, about 3 s a look on the dev machine and now and then a minute on a stuck read (2026-11-25 T765).
        self.toucher = Collector(env, None, runtime.log, shared=self.shared)
        self.touch_due, self.touch_lock, self.touch_event = set(), threading.Lock(), threading.Event()
        # topic -> the thread of a touch's read stuck on it (start_over_touch): touches leave that topic to the quick look while the thread
        # lives (one stuck in a quick look or the pass a touch reads again: Collector.collect_touched); and touches given up in a row
        # because a store was busy, logged as the quick look's are.
        self.touch_hung, self.touch_busy = {}, 0
        # web-reread (reread_once): its collector, each stuck topic's reread state (``rereads``: topic -> left, next, at, hot, hangs, reads), the threads of rereads that hung (topic ->
        # thread), whether it holds back at REREAD_MAX_LEFT, rereads given up in a row for a busy store; and when each topic was left out
        # first and by whom (leave_out), for the line that says when its stuck read came back.
        self.rereader = Collector(env, None, runtime.log, shared=self.shared)
        self.rereads, self.reread_hung, self.reread_capped, self.reread_busy = {}, {}, False, 0
        self.stuck_since, self.stuck_lock = {}, threading.Lock()
        self.post_lock = threading.Lock()  # Choosing what is fresh and posting it, one thread at a time (push_hot, touch_once).
        self.images_ready()
        self.ops = Ops(env, hear, fetch=self.client.request, login=login)
        self.sent = {}  # (type, key) -> the rev pushed last.
        # (type, key) -> (item, whose, seq): what a pass read and the page has not got, the newest read of each; sent a batch a quick look.
        self.backlog, self.backlog_lock = {}, threading.Lock()
        # whose (a topic id, "project:<id>", "collab:<id>") -> the seq of its newest read handed to the page: an older read of it is
        # never sent after it (a pass that read a topic before the owner's answer must not put 等你 back on the page).
        self.fresh = {}
        self.deletions = []  # [(seq, [{type, key}])] of the passes that ended, for the next quick look to send.
        self.pass_ended = False  # A pass ended since the last quick look: that one posts, an empty batch too, so 最后同步 moves on.
        self.wake = threading.Event()  # Set when this process wrote a job or a question (core/store.py listen) or the page acted: look now.
        self.busy = 0  # Quick looks given up in a row because a store was busy.
        # A push that found its lock held and went away (``lock_held``): name -> when (wall clock), until its next turn gives the item a
        # full push (``catch_up``); name -> when its line was last logged (monotonic), at most one a minute an item.
        self.held, self.held_logged = {}, {}
        self.sent_loaded, self.sent_saved, self.sent_changed = False, 0.0, False  # <state>/web/sent.json (load_sent, save_sent).
        self.sent_since = None  # When ``sent`` last started empty (wall clock).
        self.last_full = 0.0
        self.settings_due, self.settings_stamps = True, None  # The 设置 item: built on a full look, after the page changed one, or when its files did.
        self.settings_lock = threading.Lock()  # push_settings runs in both threads.
        self.tidy_stamps, self.tidy_lock, self.tidy_failed = None, threading.Lock(), 0  # The 整理专项 item (push_tidy).
        self.memory_stamps, self.memory_lock, self.memory_failed = None, threading.Lock(), 0  # The 记忆 item (push_memory).
        # The memory center's keys (push_memory_center, data contract v43): the sentinel last built at, when (monotonic), failures in a
        # row and no try before (monotonic) while the page refuses them; the builder, made once the state directory is known.
        self.center_stamps, self.center_built, self.center_lock, self.center_failed = None, None, threading.Lock(), 0
        self.center_wait, self.memory_center = 0.0, None
        self.center_rest, self.center_broken = 0.0, 0  # No full build before (monotonic); builds that raised in a row.
        self.upgrade_stamps, self.upgrade_lock, self.upgrade_failed = None, threading.Lock(), 0  # The 版本与更新 item (push_upgrade).
        self.quota_stamps, self.quota_lock, self.quota_failed = None, threading.Lock(), 0  # The 账号与额度 item (push_quota).
        self.asks_failed, self.asks_tried, self.asks_full = 0, None, False  # The 要你处理 item (push_asks): failures in a row, the rev tried last, a pass began.
        self.ledger = None
        self.op_lanes = OpLanes(runtime.log)  # The page's operations (inbox_once, do_op).
        self.inflight, self.inflight_lock = {}, threading.Lock()  # op_id -> monotonic time it was handed to its lane, until it is acknowledged.
        self.inbox_waited = False  # The page held the last /sync/inbox (it knows ``wait``): the next one goes at once.
        self.pace = Pace(runtime.log)  # The quick looks' and the touches' step times, logged once a minute.
        self.down = {}  # thread name -> failures in a row.
        self.images_wait, self.images_down = 0.0, 0  # push_images: no sending before this time; failures in a row.

    @property
    def stuck(self) -> dict:
        return self.shared.stuck

    def threads(self):
        if not web_sync.enabled():
            return ()
        upgrader = self.ops.self_upgrade
        # 版本与更新 (v39): its own thread, only with an upgrader set up (a check fetches two repositories, up to STATUS_SECONDS).
        upgrading = (("web-upgrade", lambda ctx: self.loop("upgrade", self.upgrade_once, lambda: UPGRADE_PAUSE,
                                                           stuck=self_upgrade.STATUS_SECONDS + 60)),) \
            if upgrader is not None and upgrader.on() else ()
        return upgrading + (("web-push", lambda ctx: self.run_push()),
                ("web-pass", lambda ctx: self.loop("pass", self.pass_once, lambda: PASS_PAUSE, self.start_over_pass,
                                                   lambda: self.passer.moved, where=lambda: self.passer.doing)),
                ("web-inbox", lambda ctx: self.loop("inbox", self.inbox_once, self.inbox_pause, again=True)),
                ("web-touch", lambda ctx: self.loop("touch", self.touch_once, lambda: 0, self.start_over_touch, lambda: self.toucher.moved,
                                                    stuck=HOT_STUCK_SECONDS, where=lambda: self.toucher.doing)),
                ("web-reread", lambda ctx: self.loop("reread", self.reread_once, lambda: REREAD_PAUSE, self.start_over_reread,
                                                     lambda: self.rereader.moved, stuck=HOT_STUCK_SECONDS, where=lambda: self.rereader.doing)))

    def inbox_pause(self) -> float:
        """Seconds between two asks of the inbox: none after one the page held (it waits for the next operation itself), else poll_seconds."""
        return 0 if self.inbox_waited else self.client.known("poll_seconds", web_sync.POLL_SECONDS)

    def run_push(self) -> None:
        """The quick looks' thread: every push_seconds, and at once (HOT_GAP apart at most) when this process wrote a job or a question."""
        store.listen(self.heard)
        try:
            self.loop("push", self.push_hot, lambda: self.client.known("push_seconds", web_sync.PUSH_SECONDS), self.start_over_hot,
                      lambda: self.collector.moved, stuck=HOT_STUCK_SECONDS, wake=self.wake, where=lambda: self.collector.doing)
        finally:
            store.unlisten(self.heard)

    def heard(self, folder, name) -> None:
        """core/store.py listen: this process wrote record ``name`` in ``folder`` (a card's answer, a job's start or end)."""
        if folder in ("jobs", "questions"):
            self.wake.set()

    def loop(self, label, step, seconds, reset=None, moved=None, again=False, stuck=None, wake=None, where=None) -> None:
        """``step`` every ``seconds()``, in a Worker; a failure is logged (the first, then every tenth) and waited out 2, 4 ... MAX_BACKOFF
        seconds. A step with no progress in ``stuck`` seconds (STUCK_SECONDS without; ``moved()``: the monotonic time of its last; without
        it, the step's start) is logged and left behind: ``reset()`` drops what it was building and the next step runs in a new Worker
        (none while MAX_LEFT left-behind ones are still stuck). With ``again``, a step that returned something (the inbox's new operations)
        is followed by the next at once: the page's next click is waiting. With ``wake`` (an Event), setting it ends the wait between two
        steps that went well early, HOT_GAP after the last at the least. ``where()``: what a step left behind was at, for its log line."""
        limit = STUCK_SECONDS if stuck is None else stuck
        worker, left = Worker(f"web-{label}-step"), []
        while not self.runtime.shutdown.is_set():
            backing = False
            try:
                began = time.monotonic()
                got = worker.call(step, limit, moved)
                if label in PACED:
                    self.pace.note(label, time.monotonic() - began)
                if self.down.pop(label, 0):
                    self.runtime.log(f"web sync {label} recovered")
                wait = 0 if again and got else seconds()
            except Stuck:
                left = [each for each in (*left, worker) if each.thread.is_alive()]
                at = f", at: {where() or 'its end'}" if where is not None else ""
                self.runtime.log(f"web sync {label} stuck: a step has not moved in {limit} s{at}; left behind ({len(left)} so far)")
                began = time.monotonic()
                while len(left) >= MAX_LEFT and time.monotonic() - began < LEFT_WAIT and not self.runtime.shutdown.wait(
                        min(limit, LEFT_WAIT)):
                    left = [each for each in left if each.thread.is_alive()]
                if len(left) >= MAX_LEFT:
                    self.runtime.log(f"web sync {label}: {len(left)} left-behind steps still stuck after {LEFT_WAIT} s; going on in a new thread")
                if reset is not None:
                    reset()
                worker, wait = Worker(f"web-{label}-step"), 0
            except Exception as exc:  # noqa: BLE001 - a failing page must not stop anything else
                failed = self.down[label] = self.down.get(label, 0) + 1
                if failed == 1 or failed % 10 == 0:
                    self.runtime.log(f"web sync {label} failed ({failed}): {type(exc).__name__}: {self.why(exc)}")
                wait, backing = min(MAX_BACKOFF, 2 ** min(failed, 5)), True
            if wake is None or backing:
                if self.runtime.shutdown.wait(wait):
                    break
                continue
            ended = time.monotonic()
            wake.wait(wait)
            wake.clear()
            rest = HOT_GAP - (time.monotonic() - ended)
            if self.runtime.shutdown.wait(rest) if rest > 0 else self.runtime.shutdown.is_set():
                break
        worker.steps.put(None)

    @staticmethod
    def why(exc) -> str:
        return str(exc)[:80] if isinstance(exc, (WebError, ValueError)) else ""

    # -- push -----------------------------------------------------------------------------------------------------------

    def leave_out(self, old, label) -> None:
        """The topic ``old`` (a collector left behind) was reading when it got stuck is left out of every look while its thread lives,
        so the next look does not get stuck on it again (an NFS open of its topic.json that never returned, say). A touch reads such a
        topic all the same (Collector.collect_touched), and web-reread reads it again (reread_once): one stuck on it too is held with the
        threads stuck there before (Shared.hold), and the topic stays out until all of them have ended."""
        if old.at and old.thread is not None and old.thread.is_alive():
            self.shared.hold(old.at, old.thread)
            with self.stuck_lock:
                self.stuck_since.setdefault(old.at, (label, time.monotonic()))
            self.runtime.log(f"web sync {label} leaves topic {old.at[:12]} out while its read is stuck")

    def start_over_hot(self) -> None:
        """After a quick look was left behind: a new collector for the quick looks. The caches stay (``Shared``): nothing is read again
        that has not changed, and no pass starts for it. The left-behind look writes only to the old collector and sends nothing (one
        that comes back an hour late must not put a topic back to 在做 on the page)."""
        old = self.collector
        self.leave_out(old, "push")
        fresh = Collector(self.env, None, self.runtime.log, self.images, shared=self.shared)
        fresh.hot, fresh.left, fresh.projects_due = set(old.hot), set(old.left), old.projects_due
        self.collector = fresh

    def start_over_pass(self) -> None:
        """After a step of the pass was left behind: a new collector for the pass, which starts a new one at once (the topics of the
        slice it was building were taken off the old pass: a pass that ended without them would delete them from the page). The caches
        stay; the quick looks go on all the while in their own thread."""
        old = self.passer
        old.retired = True
        self.leave_out(old, "pass")
        fresh = Collector(self.env, None, self.runtime.log, self.images, shared=self.shared)
        fresh.backfill = old.backfill or old.backfilling
        self.passer, self.last_full = fresh, 0.0

    def start_over(self) -> None:
        """Both collectors made again (start_over_hot, start_over_pass): ``push_once``'s, which does a step of each in one thread."""
        self.start_over_hot()
        self.start_over_pass()

    def push_once(self, now=None) -> int:
        """A step of the pass and a quick look, one after the other in this thread (the threads run them each in its own): how many
        items went out. Raises WebError when the page did not take a batch (the batches before it count)."""
        self.pass_once(now)
        count = self.push_hot(now)
        self.save_due(now)
        return count

    def push_hot(self, now=None) -> int:
        """One quick look (Collector.collect_hot) and one push: its items that changed, then a batch of what the pass read (the backlog,
        each an item not older than what the page has of its topic), then the deletions of a pass that ended (an empty batch when there
        is nothing else, so the page's 最后同步 moves on in a quiet minute), then 要你处理. How many items went out. A store whose lock
        another thread holds HOT_WAIT seconds (a scan stuck on the network disk) gives the step up: nothing is sent, the next look tries
        again. Raises WebError when the page did not take a batch (the batches before it count)."""
        chat = self.runtime.access.owner_chat()
        if not chat or not self.env.state_dir:
            return 0
        if not self.sent_loaded:
            self.load_sent(now)
        self.images_ready()
        collector, sent = self.collector, self.sent
        now = time.time() if now is None else now
        try:
            with store.bounded(HOT_WAIT):
                items = collector.collect_hot(chat, now)
        except store.Busy as exc:
            self.busy += 1
            if self.busy == 1 or self.busy % BUSY_EVERY == 0:
                self.runtime.log(f"web sync push waits: the {exc} index is held by a read that has not ended ({self.busy} looks given up)")
            return 0
        if self.busy:
            self.runtime.log(f"web sync push goes on: the index is free again after {self.busy} looks given up")
            self.busy = 0
        if collector is not self.collector:
            return 0  # Left behind while it looked: what it read is old.
        collector.mark("posting")
        # Keep avatar allocation outside the read-only Collector and finish its lock before any HTTP call.
        # Feishu and the web share this ledger, including collision handling and assignments already made.
        for each in items:
            if each["type"] == "project" and "avatar" not in each["data"]:
                name = avatars.lead_name(self.env.state_dir, chat, each["key"])
                if name:
                    each["data"]["avatar"] = name
                    each["rev"] = web_sync.rev(each["data"])
        with self.post_lock:  # Held while its batches are posted: the touch thread posts between two looks, not inside one.
            return self.post_hot(collector, sent)

    def post_hot(self, collector, sent) -> int:
        """push_hot's posting, under ``post_lock``: the look's fresh items, a batch of the backlog, the deletions, then 要你处理."""
        step = self.client.batch_bytes()  # What a quick look sends of the pass's items (the backlog) after its own: one batch.
        with self.backlog_lock:
            urgent = self.fresh_items(collector.groups)
            later, size = [], 0
            for name, (each, whose, seq) in list(self.backlog.items()):
                if seq < self.fresh.get(whose, 0) or sent.get(name) == each["rev"]:
                    del self.backlog[name]  # A newer read of its topic went out, or the page has it already.
                    continue
                weight = len(json.dumps(each, ensure_ascii=False).encode())
                if later and size + weight > step:
                    break
                later.append(each)
                size += weight
                self.fresh[whose] = max(self.fresh.get(whose, 0), seq)
            deletions, self.deletions = self.deletions, []
            ended, self.pass_ended = self.pass_ended, False
        count, posted = 0, False
        try:
            for batch in web_sync.batches(urgent + later, step):
                if collector is not self.collector:
                    return count  # Left behind (start_over) while it looked: what it read is old, the page would go back to it.
                self.client.request("POST", "/sync/upsert", {"items": batch, "deleted": []})
                collector.moved = time.monotonic()  # The first push of a big chat is many batches: each one taken is progress.
                with self.backlog_lock:
                    for each in batch:
                        name = (each["type"], each["key"])
                        sent[name] = each["rev"]
                        if (self.backlog.get(name) or (None,))[0] is each:
                            del self.backlog[name]
                count += len(batch)
                self.sent_changed = True
            if collector is not self.collector:
                return count
            # A topic a pass did not see but a quick look built since (an old topic written to again) stays.
            deleted = [each for seq, rows in deletions for each in rows
                       if self.fresh.get(each["key"] if each["type"] == "topic" else f"{each['type']}:{each['key']}", 0) < seq]
            if deleted or (ended and not count):
                self.client.request("POST", "/sync/upsert", {"items": [], "deleted": deleted})
            posted = True
        finally:
            if not posted:  # Failed or left behind: the page took none of them, the next quick look sends the deletions.
                with self.backlog_lock:
                    self.deletions[:0] = deletions
                    self.pass_ended = self.pass_ended or ended
        if deleted or (ended and not count):
            gone = {each["key"] for each in deleted if each["type"] == "topic"}
            with self.backlog_lock:
                for each in deleted:
                    sent.pop((each["type"], each["key"]), None)
                for name, (each, whose, _) in list(self.backlog.items()):  # Nothing of a topic deleted goes after it.
                    if whose in gone or each["key"] in gone or str((each.get("data") or {}).get("topic_key") or "") in gone:
                        del self.backlog[name]
            self.sent_changed = True
        if collector is self.collector:
            full, self.asks_full = self.asks_full, False
            self.push_asks(collector, full)
            collector.mark("")
        return count

    def fresh_items(self, groups) -> list:
        """Under ``backlog_lock``: of ``groups`` [(whose, seq, items)], the items the page should get. A read older than the newest
        the page has of its topic is left out whole (the quick look's read from before an answer must not follow the touch's read
        after it); the others are marked the newest, taken off the backlog, and kept when the page does not have their rev."""
        out = []
        for whose, seq, mine in groups:
            if seq < self.fresh.get(whose, 0):
                continue
            self.fresh[whose] = seq
            for each in mine:
                name = (each["type"], each["key"])
                self.backlog.pop(name, None)
                if self.sent.get(name) != each["rev"]:
                    out.append(each)
        return out

    def touch_soon(self, topics) -> None:
        """The page acted on ``topics``: the touch thread builds and pushes them at once (touch_once)."""
        with self.touch_lock:
            self.touch_due |= {str(topic) for topic in topics if topic}
        self.touch_event.set()

    def touch_once(self, now=None) -> int:
        """Waits up to a second for topics the page acted on (touch_soon), builds just those (Collector.collect_touched: the board's
        reading the last quick look left) and posts them; how many items went out. Each topic's read has its seq as every read has.
        A topic whose read is stuck in a left-behind quick look or pass is read again here (the page waits for what it just did); one
        a touch's own read hangs on (``touch_hung``) is left to the quick look while that read lasts, and said so. A busy store or a
        batch the page did not take puts the topics back for the next touch, and is logged: a touch never drops one silently."""
        if not self.touch_event.wait(1.0):
            return 0
        self.touch_event.clear()
        woke = time.monotonic()
        with self.touch_lock:
            topics, self.touch_due = self.touch_due, set()
        chat = self.runtime.access.owner_chat()
        if not topics or not chat or not self.env.state_dir:
            return 0
        if not self.sent_loaded:
            return 0  # The quick look loads what the page has first; it builds these too (Collector.touch).
        hung = self.touch_hung_now()
        for topic in sorted(set(topics) & hung):
            self.runtime.log(f"web sync touch leaves topic {topic[:12]} to the quick look: a touch's read of it is stuck")
        toucher = self.toucher
        toucher.touching = set(topics)
        try:
            with store.bounded(HOT_WAIT):
                toucher.collect_touched(chat, sorted(topics), time.time() if now is None else now, skip=hung)
        except store.Busy as exc:
            self.touch_busy += 1
            if self.touch_busy == 1 or self.touch_busy % BUSY_EVERY == 0:
                self.runtime.log(f"web sync touch waits: the {exc} index is held by a read that has not ended ({self.touch_busy} touches given up)")
            self.touch_soon(topics)  # The quick look builds them all the same; this one tries again.
            return 0
        if self.touch_busy:
            self.runtime.log(f"web sync touch goes on: the index is free again after {self.touch_busy} touches given up")
            self.touch_busy = 0
        if toucher is not self.toucher:
            return 0  # Left behind while it looked: what it read is old.
        toucher.mark("posting")
        try:
            count = self.post_fresh(toucher)
        except WebError:
            self.touch_soon(set(topics) - hung)  # Read again and sent once the page takes batches again (the loop waits it out).
            raise
        toucher.touching = set()
        toucher.mark("")
        self.pace.note("touch", time.monotonic() - woke)
        return count

    def touch_hung_now(self) -> set:
        """The topics a touch's read is stuck on (start_over_touch), while the thread of that read lives."""
        for topic, thread in list(self.touch_hung.items()):
            if not thread.is_alive():
                self.touch_hung.pop(topic, None)
        return set(self.touch_hung)

    def start_over_touch(self) -> None:
        """After a touch was left behind (a read stuck on the network disk): a new collector for it, the topics it had taken back in
        the queue but the one it is stuck on (``touch_hung``: the quick look builds that one when its read comes back)."""
        old = self.toucher
        self.leave_out(old, "touch")
        if old.at and old.thread is not None and old.thread.is_alive():
            self.touch_hung[old.at] = old.thread
        self.toucher = Collector(self.env, None, self.runtime.log, self.images, shared=self.shared)
        again = set(getattr(old, "touching", ()) or ()) - self.touch_hung_now()
        if again:
            self.touch_soon(again)

    def post_fresh(self, collector) -> int:
        """Posts the items of ``collector``'s last look (its ``groups``) the page should get (``fresh_items``), under ``post_lock``: how many
        went out. Raises WebError when the page did not take a batch (the batches before it count)."""
        count = 0
        with self.post_lock:
            with self.backlog_lock:
                items = self.fresh_items(collector.groups)
            for batch in web_sync.batches(items, self.client.batch_bytes()):
                self.client.request("POST", "/sync/upsert", {"items": batch, "deleted": []})
                collector.moved = time.monotonic()
                with self.backlog_lock:
                    for each in batch:
                        self.sent[(each["type"], each["key"])] = each["rev"]
                count += len(batch)
                self.sent_changed = True
        return count

    # -- reread (换线程重读) ----------------------------------------------------------------------------------------------

    def reread_once(self, now=None) -> int:
        """One step of web-reread: notes which topics are left out for a stuck read now (and says when one's read came back), then reads
        again the one most due, if any (``reread_due``), with the board's reading the last quick look left (Collector.collect_touched, as
        a touch) and posts it; how many items went out. Bounded as REREAD_* says; a reread that hangs too is left behind by the loop
        (start_over_reread)."""
        chat = self.runtime.access.owner_chat()
        if not chat or not self.env.state_dir or not self.sent_loaded:
            return 0
        clock = time.monotonic()
        stuck = self.shared.alive_stuck()
        self.note_stuck(stuck, clock)
        hung = self.reread_hung_now()
        if len(hung) >= REREAD_MAX_LEFT:
            if not self.reread_capped:
                self.reread_capped = True
                self.runtime.log(f"web sync reread waits: {len(hung)} rereads still stuck, none is started until one of them ends")
            return 0
        if self.reread_capped:
            self.reread_capped = False
            self.runtime.log("web sync reread goes on: a stuck reread ended")
        with self.shared.lock:
            hot = set(self.collector.hot) | set(self.shared.dirty)
        topic = self.reread_due(clock, hot, hung | self.touch_hung_now())
        if not topic:
            return 0
        state = self.rereads[topic]
        state.update(began=clock, hot=topic in hot)
        rereader = self.rereader
        rereader.rereading = topic
        try:
            return self.reread_topic(rereader, chat, topic, state, clock, now)
        finally:
            rereader.rereading = ""

    def reread_topic(self, rereader, chat, topic, state, clock, now) -> int:
        """reread_once's read of ``topic`` and its post: how many items went out."""
        try:
            with store.bounded(HOT_WAIT):
                rereader.collect_touched(chat, [topic], time.time() if now is None else now)
        except store.Busy as exc:
            state["next"] = clock + REREAD_GAP
            self.reread_busy += 1
            if self.reread_busy == 1 or self.reread_busy % BUSY_EVERY == 0:
                self.runtime.log(f"web sync reread waits: the {exc} index is held by a read that has not ended ({self.reread_busy} rereads given up)")
            return 0
        if self.reread_busy:
            self.runtime.log(f"web sync reread goes on: the index is free again after {self.reread_busy} rereads given up")
            self.reread_busy = 0
        if rereader is not self.rereader:
            return 0  # Left behind while it read: what it read is old.
        rereader.mark("posting")
        try:
            count = self.post_fresh(rereader)
        except WebError:
            state["next"] = time.monotonic() + REREAD_GAP
            raise
        took = time.monotonic() - clock
        state.update(at=clock, next=clock + REREAD_GAP, hangs=0, reads=state["reads"] + 1)
        if state["reads"] == 1:
            self.runtime.log(f"web sync reread topic {topic[:12]}: read again in {took * 1000:.0f} ms, {count} items sent, "
                             f"while a read of it stuck since {clock - state['left']:.0f} s holds it out of the quick looks")
        rereader.mark("")
        self.pace.note("reread", took)
        return count

    def reread_due(self, clock, hot, hung) -> str:
        """The stuck topic to read again now, or "": not one a touch's or a reread's read hangs on (``hung``); REREAD_AFTER after it was
        left out the first time; then while the quick look would look at it (``hot``) every REREAD_GAP; once more when it stopped being
        hot since the last reread; after a hung one, not before its backoff. The most overdue first."""
        due = []
        for topic, state in self.rereads.items():
            if topic in hung or clock < state["next"]:
                continue
            if state["at"] is not None and topic not in hot and not state["hot"]:
                continue  # Read again since it went quiet: nothing more to fetch until it runs or is acted on again.
            due.append((state["next"], topic))
        return min(due)[1] if due else ""

    def note_stuck(self, stuck, clock) -> None:
        """Keeps ``rereads`` to the topics left out now (``stuck``); a topic no longer left out had its stuck read come back: logged
        with how long it held the topic out and how often it was read again meanwhile."""
        with self.stuck_lock:
            for topic in list(self.stuck_since):
                if topic not in stuck:
                    label, since = self.stuck_since.pop(topic)
                    reads = (self.rereads.get(topic) or {}).get("reads", 0)
                    self.runtime.log(f"web sync {label} read of topic {topic[:12]} came back after {clock - since:.0f} s; "
                                     f"read again {reads} times meanwhile")
            since = dict(self.stuck_since)
        for topic in list(self.rereads):
            if topic not in stuck:
                del self.rereads[topic]
        for topic in stuck:
            if topic not in self.rereads:
                left = since.get(topic, ("", clock))[1]
                self.rereads[topic] = {"left": left, "next": left + REREAD_AFTER, "at": None, "hot": False, "hangs": 0, "reads": 0,
                                       "began": None}

    def reread_hung_now(self) -> set:
        """The topics a reread's read is stuck on (start_over_reread), while the thread of that read lives."""
        for topic, thread in list(self.reread_hung.items()):
            if not thread.is_alive():
                self.reread_hung.pop(topic, None)
        return set(self.reread_hung)

    def start_over_reread(self) -> None:
        """After a reread was left behind (its read hung too): a new collector for web-reread. The topic is not read again while that read
        lives (``reread_hung``, at most REREAD_MAX_LEFT of them before rereads stop) nor before its backoff, doubled each time."""
        old = self.rereader
        topic = getattr(old, "rereading", "") or old.at
        self.leave_out(old, "reread")
        if topic and old.thread is not None and old.thread.is_alive():
            self.reread_hung[topic] = old.thread
        state = self.rereads.get(topic)
        if state is not None:
            state["hangs"] += 1
            wait = min(REREAD_BACKOFF_MAX, REREAD_BACKOFF * 2 ** (state["hangs"] - 1))
            state["next"] = time.monotonic() + wait
            self.runtime.log(f"web sync reread of topic {topic[:12]} hung too: not read again while it hangs nor for {wait:.0f} s "
                             f"({len(self.reread_hung_now())} rereads still stuck)")
        self.rereader = Collector(self.env, None, self.runtime.log, self.images, shared=self.shared)

    def pass_once(self, now=None) -> int:
        """One step of the full pass (Collector.collect_pass), in its own thread: when one is due (FULL_SECONDS after the last ended) it
        starts, with the 设置 and 整理专项 items built again; each step reads PASS_SLICE topics into the backlog, which the
        quick looks send a batch at a time after their own items; one that ended leaves its deletions to the next quick look. Then some
        images (push_images) and, now and then, what the page has (save_sent). How many items it put in the backlog."""
        chat = self.runtime.access.owner_chat()
        if not chat or not self.env.state_dir:
            return 0
        if not self.sent_loaded:
            self.load_sent(now)
        self.images_ready()
        passer, sent = self.passer, self.sent
        now = time.time() if now is None else now
        full = not passer.pass_left and now - self.last_full >= FULL_SECONDS  # A full pass is due and none is under way.
        if full and (self.sent_since is None or now - self.sent_since >= SENT_KEEP_SECONDS):
            if self.sent_since is not None:
                sent.clear()  # A day on: everything once more, a step's worth at a time behind the hot topics.
                self.sent_changed = True
            self.sent_since = now
        # ``sent``: a topic whose inputs did not change is left unbuilt when the page has what its last build made (Collector.unchanged);
        # emptied (a new day above, a new process) it has nothing, and every topic is built again. One scan of each index a second
        # (core/store.py one_scan), not one per topic: the step queries the jobs and the questions of each of its PASS_SLICE topics.
        with store.one_scan():
            items, deleted = passer.collect_pass(chat, now, full, skip=self.collector.hot | self.collector.dirty, sent=sent)
        if passer is not self.passer:
            return 0  # Left behind while it looked: what it read is old, and must not wait in the backlog either.
        put = 0
        with self.backlog_lock:
            for whose, seq, mine in passer.groups:
                if seq < self.fresh.get(whose, 0):
                    continue  # A quick look sent a newer read of it already.
                for each in mine:
                    name = (each["type"], each["key"])
                    if sent.get(name) != each["rev"]:
                        self.backlog[name] = (each, whose, seq)
                        put += 1
                    else:
                        self.backlog.pop(name, None)
            if passer.pass_done:
                self.deletions.append((self.shared.next_seq(), deleted))
                self.pass_ended = True
                self.last_full = max(self.last_full, now)
        if full:
            self.collector.projects_due, self.asks_full = True, True  # Every project built again, 要你处理 sent again if it was refused.
            self.last_full = now
        # The 设置 and 整理专项 items: built when their files changed (a few stats) or a pass starts. Here, in the thread whose stuck
        # step holds up nothing else, not in the inbox's each poll (2026-11-23 T673: the inbox stuck 60 s on those stats).
        passer.mark("settings")
        self.push_settings(chat, now, full=full)
        passer.mark("tidy")
        self.push_tidy(now, full=full)
        passer.mark("memory")
        self.push_memory(full=full)
        passer.mark("memory_center")
        self.push_memory_center(full=full)
        passer.mark("upgrade")
        self.push_upgrade(full=full)
        passer.mark("quota")
        self.push_quota(full=full)
        passer.mark("images")
        self.push_images(passer, now)
        passer.mark("saving")
        self.save_due(now)
        passer.mark("")
        return put

    def save_due(self, now=None) -> None:
        """``save_sent`` when what the page has changed, at most every SENT_SAVE_SECONDS, and when a pass ended."""
        if self.sent_changed and (self.passer.pass_done or time.monotonic() - self.sent_saved >= SENT_SAVE_SECONDS):
            self.save_sent(now)

    def images_ready(self) -> None:
        """The images' ledger, once the state directory is known: the unified Bridge builds this object before build_context fills
        the butler env's state directory in."""
        if self.images is not None or not self.env.state_dir:
            return
        self.images = web_images.WebImages(self.env.state_dir, self.runtime.log)
        # The touch's and the reread's too: a turn they build carries its images as the quick look's does, or the page would lose them
        # between two pushes of the same topic.
        self.collector.images = self.passer.images = self.toucher.images = self.rereader.images = self.images
        self.passer.backfill = not self.images.backfilled

    def push_images(self, collector, now) -> None:
        """Some of the images the turns asked for (web_images.WebImages.upload), from the pass's thread; their topics are built again
        on the next quick look, now with them. A page that does not take them (an older page without /sync/image, a refusal) only holds
        the images back IMAGES_WAIT seconds: the items go on as before."""
        if self.images is None or now < self.images_wait:
            return
        try:
            topics = self.images.upload(self.client, now, lambda: setattr(collector, "moved", time.monotonic()))
        except WebError as exc:
            self.images_down += 1
            if self.images_down == 1 or self.images_down % 10 == 0:
                self.runtime.log(f"web sync images failed ({self.images_down}): {self.why(exc)}")
            self.images_wait = now + IMAGES_WAIT
            return
        self.images_down = 0
        for topic in topics:
            self.collector.touch(topic)
        if topics:
            self.wake.set()

    def sent_path(self) -> Path:
        return Path(self.env.state_dir) / web_sync.FOLDER / SENT_FILE

    def load_sent(self, now=None) -> None:
        """What the page took before this process (save_sent), when it was saved for the same page less than SENT_KEEP_SECONDS ago:
        a restart sends only what changed since, not every item again. Anything else (none, another page, too old, unreadable):
        nothing, so everything goes once, as before."""
        self.sent_loaded = True
        now = time.time() if now is None else now
        try:
            base = self.client.settings().base_url
        except ValueError:
            return
        data = read_object(self.sent_path())
        since = data.get("since")
        rows = data.get("sent")
        if data.get("base_url") != base or not isinstance(since, (int, float)) or not 0 <= now - since < SENT_KEEP_SECONDS or not isinstance(rows, list):
            return
        self.sent_since = since
        for row in rows:
            if isinstance(row, list) and len(row) == 3 and all(isinstance(part, str) for part in row):
                self.sent.setdefault((row[0], row[1]), row[2])

    def save_sent(self, now=None) -> None:
        """``sent`` to <state>/web/sent.json for the next process (load_sent); a failure to write is only a slower next start."""
        self.sent_saved, self.sent_changed = time.monotonic(), False
        try:
            base = self.client.settings().base_url
            rows = [[kind, key, rev] for (kind, key), rev in list(self.sent.items())]
            path = self.sent_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"base_url": base, "since": self.sent_since, "saved_at": time.time() if now is None else now, "sent": rows},
                       ensure_ascii=False)
        except (OSError, ValueError):
            pass

    def lock_held(self, name) -> int:
        """A push's non-blocking lock was taken: remember it (``held[name]``, the first time since the last catch-up) so the item's next
        turn pushes it in full, and log it once a minute an item. Always 0 (what the push returns)."""
        since = self.held.setdefault(name, time.time())
        clock = time.monotonic()
        last = self.held_logged.get(name)
        if last is None or clock - last >= HELD_LOG_SECONDS or clock < last:
            self.held_logged[name] = clock
            self.runtime.log(f"web sync {name} skipped: its lock is held (since {time.strftime('%H:%M:%S', time.localtime(since))})")
        return 0

    def catch_up(self, name, full) -> bool:
        """``full``, or True when this item's earlier push found its lock held (``lock_held``); the mark is cleared. Called with the
        lock held, so a push that is skipped again keeps it."""
        return self.held.pop(name, None) is not None or full

    def settings_items(self, chat, now, full) -> list:
        """The ``settings`` item (app/web_settings.py) when it is due: on a full look, after the page changed a setting, when main-thread.json
        or the workspace list or the term list changed (three stats); else none. Never raises (a 设置 that cannot be read stops nothing)."""
        if getattr(self.env, "settings", None) is None:
            return []
        builder = self.ops.web_settings
        try:
            stamps = builder.stamps()
            if not (full or self.settings_due or stamps != self.settings_stamps):
                return []
            self.settings_due, self.settings_stamps = False, stamps
            items = [web_sync.item("settings", web_sync.SETTINGS_KEY, builder.snapshot(chat, now))]
            if full:  # 本周用量 as the card counts it (data contract v15): once a full look, its rev changes as tasks run.
                items.append(web_sync.item("settings", web_sync.USAGE_KEY, builder.usage(now)))
            return items
        except Exception as exc:  # noqa: BLE001 - logged; the next look tries again
            self.runtime.log(f"web settings not built: {type(exc).__name__}")
            return []

    def push_settings(self, chat, now, full=False) -> int:
        """The ``settings`` item posted on its own when it is due and changed (``settings_items``): from the pass thread each step (a few
        stats of its files: a change on the card is on the page a second later, however long the quick looks take) and on a full pass;
        and from the inbox thread right after the page changed a setting, so it is on the page before the operation's ack. One at a
        time (``settings_lock``); a turn that finds the lock held leaves a mark (``lock_held``) and the next one pushes in full. How many went (0 or 1);
        a failed post is logged and tried on the next look."""
        if not self.settings_lock.acquire(blocking=False):
            # The other thread posts it, or a look left behind still holds it (a read on the network disk that hangs): the item is due
            # again, and the next turn pushes it in full.
            self.settings_due = True
            return self.lock_held("settings")
        try:
            full = self.catch_up("settings", full)
            due = [each for each in self.settings_items(chat, now, full)
                   if self.sent.get((each["type"], each["key"])) != each["rev"]]
            if not due:
                return 0
            try:
                self.client.request("POST", "/sync/upsert", {"items": due, "deleted": []})
            except Exception as exc:  # noqa: BLE001 - the inbox goes on; the settings are due again
                self.settings_due = True
                self.runtime.log(f"web settings not pushed: {type(exc).__name__}: {self.why(exc)}")
                return 0
            for each in due:
                self.sent[(each["type"], each["key"])] = each["rev"]
            return len(due)
        finally:
            self.settings_lock.release()

    def push_asks(self, collector, full=False) -> int:
        """The ``settings`` item ``asks`` (data contract v34): 要你处理 as every card counts it ({waiting, stopped, stuck},
        core/board_read.py Asks.counts, the collector's last look), posted on its own right after the step's topics, when it
        changed. A page older than v34 refuses the key: that is logged (the first time, then every tenth) and the same counts are
        tried again only on a full look, never holding up the topics, which went in their own request. How many went (0 or 1)."""
        if collector.asks is None:
            return 0
        each = web_sync.item("settings", web_sync.ASKS_KEY, collector.asks)
        name = (each["type"], each["key"])
        if self.sent.get(name) == each["rev"] or (self.asks_tried == each["rev"] and not full):
            return 0
        self.asks_tried = each["rev"]
        try:
            self.client.request("POST", "/sync/upsert", {"items": [each], "deleted": []})
        except Exception as exc:  # noqa: BLE001 - logged; a change or the next full look tries again
            self.asks_failed += 1
            if self.asks_failed == 1 or self.asks_failed % 10 == 0:
                self.runtime.log(f"web asks not pushed ({self.asks_failed}): {type(exc).__name__}: {self.why(exc)}")
            return 0
        self.asks_failed = 0
        self.sent[name] = each["rev"]
        self.sent_changed = True
        return 1

    def push_tidy(self, now, full=False) -> int:
        """The ``settings`` item ``tidy`` (整理专项's suggestions, app/project_tidy.py web_view, data contract v16), posted on its own: when
        the runs or the proposals changed (a stat of two folders, from the pass thread each step), on a full pass, and from the inbox
        thread after the page decided a suggestion. A page older than v16 refuses the key: that is logged (the first time, then every
        tenth) and tried again only on the next full pass or change, and never holds up the settings, which go in a request of their
        own. How many went (0 or 1)."""
        tidy = getattr(self.env, "tidy", None)
        if tidy is None or not getattr(self.env, "state_dir", None):
            return 0
        if not self.tidy_lock.acquire(blocking=False):
            return self.lock_held("tidy")
        try:
            full = self.catch_up("tidy", full)
            stamps = tidy.web_stamps()
            if not full and stamps == self.tidy_stamps:
                return 0
            self.tidy_stamps = stamps
            data = tidy.web_view(now)
            if data is None:
                return 0
            each = web_sync.item("settings", web_sync.TIDY_KEY, data)
            if self.sent.get((each["type"], each["key"])) == each["rev"]:
                return 0
            try:
                self.client.request("POST", "/sync/upsert", {"items": [each], "deleted": []})
            except Exception as exc:  # noqa: BLE001 - logged; the next full look or change tries again
                self.tidy_failed += 1
                if self.tidy_failed == 1 or self.tidy_failed % 10 == 0:
                    self.runtime.log(f"web tidy not pushed ({self.tidy_failed}): {type(exc).__name__}: {self.why(exc)}")
                return 0
            self.tidy_failed = 0
            self.sent[(each["type"], each["key"])] = each["rev"]
            return 1
        except Exception as exc:  # noqa: BLE001 - a 整理专项 that cannot be read stops nothing
            self.runtime.log(f"web tidy not built: {type(exc).__name__}")
            return 0
        finally:
            self.tidy_lock.release()

    def default_workdir(self):
        """The default Harness's default workdir (the workspace list's fallback), None when it cannot be told."""
        try:
            return self.ops.web_settings.context().default_workdir
        except Exception:  # noqa: BLE001 - the list in workspaces.json is read all the same
            return None

    def push_memory(self, full=False) -> int:
        """The ``settings`` item ``memory`` (the page's 记忆 tab, app/web_memory.py view, data contract v37), posted on its own as
        ``push_tidy`` is: when a file of the memory changed (a few stats, from the pass thread each step), on a full pass, and from
        the inbox thread after the page changed the memory, before the ack. With COVEY_MEMORY off it is ``{on: false}``, so a page
        that had the tab drops it, and nothing at all when the page never had one. A page older than v37 refuses the key: logged the first time and every tenth. How many went."""
        state = getattr(self.env, "state_dir", None)
        if not state:
            return 0
        if not self.memory_lock.acquire(blocking=False):
            return self.lock_held("memory")
        try:
            full = self.catch_up("memory", full)
            on = web_memory.enabled()
            if not on and ("settings", web_sync.MEMORY_KEY) not in self.sent:
                return 0  # Never pushed (the memory was never on here): nothing on the page to take back.
            stamps = (on, web_memory.stamps(state)) if on else (False,)
            if not full and stamps == self.memory_stamps:
                return 0
            self.memory_stamps = stamps
            data = web_memory.view(state, self.default_workdir()) if on else {"on": False}
            each = web_sync.item("settings", web_sync.MEMORY_KEY, data)
            if self.sent.get((each["type"], each["key"])) == each["rev"]:
                return 0
            try:
                self.client.request("POST", "/sync/upsert", {"items": [each], "deleted": []})
            except Exception as exc:  # noqa: BLE001 - logged; the next full look or change tries again
                self.memory_failed += 1
                if self.memory_failed == 1 or self.memory_failed % 10 == 0:
                    self.runtime.log(f"web memory not pushed ({self.memory_failed}): {type(exc).__name__}: {self.why(exc)}")
                return 0
            self.memory_failed = 0
            self.sent[(each["type"], each["key"])] = each["rev"]
            return 1
        except Exception as exc:  # noqa: BLE001 - a memory that cannot be read stops nothing
            self.runtime.log(f"web memory not built: {type(exc).__name__}")
            return 0
        finally:
            self.memory_lock.release()

    def push_memory_center(self, full=False, force=False) -> int:
        """The memory center's ``settings`` items (data contract v43, app/web_memory.py Center): every ``memory:*`` key whose rev
        the page does not have, the data keys first and ``memory:index`` (whose ``revs`` name theirs) last, in requests of their own
        after the 记忆 item's; a key gone since (a project cancelled, fewer import pages) taken back with a ``deleted`` row. Built
        when the memory's one sentinel moved (``stamps``: one stat, from the pass thread each step), on a full pass at most every
        REBUILD_SECONDS, and ``force``d from the inbox thread after the page changed the memory, before the ack. With COVEY_MEMORY
        off: ``memory:index`` says so and the rest is taken back; nothing at all when the page never had any. A page older than
        v43 refuses the keys: logged the first time and every tenth, tried again later and later (at most CENTER_WAIT_MAX).
        How many items went."""
        state = getattr(self.env, "state_dir", None)
        if not state:
            return 0
        if not (self.center_lock.acquire(timeout=CENTER_LOCK_WAIT) if force else self.center_lock.acquire(blocking=False)):
            return 0 if force else self.lock_held("memory center")  # A forced push waited for it: that is as it was.
        try:
            caught = self.catch_up("memory center", False)  # A pass skipped at the lock: built now, whatever REBUILD_SECONDS says.
            on = web_memory.enabled()
            had = [key for kind, key in list(self.sent) if kind == "settings" and web_sync.MEMORY_KEY_RE.match(key)]
            if not on and not had:
                return 0
            stamps = (on, web_memory.stamps(state)) if on else (False,)
            clock = time.monotonic()
            due = force or caught or self.center_stamps is None or stamps != self.center_stamps or \
                (full and clock - (self.center_built or 0) >= web_memory.REBUILD_SECONDS)
            if not due or (not force and clock < max(self.center_wait, self.center_rest)):
                return 0
            if on:
                if self.memory_center is None:
                    self.memory_center = web_memory.Center(state)
                built = self.memory_center.build(quick=force)  # Before the page's ack when forced.
            else:
                built = {web_memory.INDEX_KEY: web_memory.off_index()}
            # After a forced one a pass builds it again, off the page's ack and CENTER_REST later.
            self.center_stamps, self.center_built, self.center_broken = (None if force and on else stamps), clock, 0
            if force and on:
                self.center_rest = clock + CENTER_REST
            items = [web_sync.item("settings", key, data) for key, data in built.items()]
            index = [each for each in items if each["key"] == web_memory.INDEX_KEY]
            fresh = [each for each in items if each["key"] != web_memory.INDEX_KEY and self.sent.get(("settings", each["key"])) != each["rev"]]
            index = [each for each in index if self.sent.get(("settings", each["key"])) != each["rev"]]
            gone = [{"type": "settings", "key": key} for key in had if key not in built]
            if not fresh and not index and not gone:
                return 0
            try:
                for batch in web_sync.batches(fresh, self.client.batch_bytes()):
                    self.client.request("POST", "/sync/upsert", {"items": batch, "deleted": []})
                    for each in batch:
                        self.sent[("settings", each["key"])] = each["rev"]
                    self.sent_changed = True
                self.client.request("POST", "/sync/upsert", {"items": index, "deleted": gone})
            except Exception as exc:  # noqa: BLE001 - logged; tried again later (the keys not sent stay due)
                self.center_failed += 1
                self.center_stamps = None
                self.center_wait = clock + min(CENTER_WAIT * 2 ** (self.center_failed - 1), CENTER_WAIT_MAX)
                if self.center_failed == 1 or self.center_failed % 10 == 0:
                    self.runtime.log(f"web memory center not pushed ({self.center_failed}): {type(exc).__name__}: {self.why(exc)}")
                return 0
            self.center_failed, self.center_wait = 0, 0.0
            for each in index:
                self.sent[("settings", each["key"])] = each["rev"]
            for each in gone:
                self.sent.pop(("settings", each["key"]), None)
            self.sent_changed = True
            return len(fresh) + len(index) + len(gone)
        except Exception as exc:  # noqa: BLE001 - a memory that cannot be read stops nothing; tried again later and later
            self.center_broken += 1
            self.center_wait = time.monotonic() + min(CENTER_WAIT * 2 ** (self.center_broken - 1), CENTER_WAIT_MAX)
            if self.center_broken == 1 or self.center_broken % 10 == 0:
                self.runtime.log(f"web memory center not built ({self.center_broken}): {type(exc).__name__}")
            return 0
        finally:
            self.center_lock.release()

    def upgrade_once(self) -> int:
        """The web-upgrade thread's step: a check of a newer version when one is due (app/self_upgrade.py due), then the item."""
        upgrader = self.ops.self_upgrade
        if upgrader is None:
            return 0
        if upgrader.due():
            upgrader.check()
        return self.push_upgrade()

    def push_upgrade(self, full=False) -> int:
        """The ``settings`` item ``upgrade`` (版本与更新, app/self_upgrade.py view, data contract v39), posted on its own as
        ``push_memory`` is: when its two files or the upgrade's process changed (a few stats, from the pass thread each step and the
        web-upgrade thread), on a full pass, and before the ack of an ``upgrade`` operation. Nothing without an upgrader set up. A page
        older than v39 refuses the key: logged the first time and every tenth. How many went."""
        upgrader = self.ops.self_upgrade
        if upgrader is None or not upgrader.on():
            return 0
        if not self.upgrade_lock.acquire(blocking=False):
            return self.lock_held("upgrade")
        try:
            full = self.catch_up("upgrade", full)
            stamps = upgrader.stamps()
            if not full and stamps == self.upgrade_stamps:
                return 0
            self.upgrade_stamps = stamps
            each = web_sync.item("settings", web_sync.UPGRADE_KEY, upgrader.view())
            if self.sent.get((each["type"], each["key"])) == each["rev"]:
                return 0
            try:
                self.client.request("POST", "/sync/upsert", {"items": [each], "deleted": []})
            except Exception as exc:  # noqa: BLE001 - logged; the next change or full look tries again
                self.upgrade_failed += 1
                self.upgrade_stamps = None
                if self.upgrade_failed == 1 or self.upgrade_failed % 10 == 0:
                    self.runtime.log(f"web upgrade not pushed ({self.upgrade_failed}): {type(exc).__name__}: {self.why(exc)}")
                return 0
            self.upgrade_failed = 0
            self.sent[(each["type"], each["key"])] = each["rev"]
            return 1
        except Exception as exc:  # noqa: BLE001 - a status that cannot be read stops nothing
            self.runtime.log(f"web upgrade not built: {type(exc).__name__}")
            return 0
        finally:
            self.upgrade_lock.release()

    def push_quota(self, full=False) -> int:
        """The ``settings`` item ``quota`` (账号与额度, app/web_quota.py view, data contract v42), posted on its own as ``push_upgrade``
        is: when a quota snapshot or the refresh record changed (a few stats, from the pass thread each step), on a full pass, and
        before the ack of a ``quota_refresh`` operation. Nothing while there is no source and never was one; a source gone after one
        was pushed pushes ``sources: []``, so the page drops its tab. A page older than v42 refuses the key: logged the first time
        and every tenth. How many went."""
        state = getattr(self.env, "state_dir", None)
        if not state:
            return 0
        if not self.quota_lock.acquire(blocking=False):
            return self.lock_held("quota")
        try:
            full = self.catch_up("quota", full)
            stamps = web_quota.stamps(state)
            if not full and stamps == self.quota_stamps:
                return 0
            self.quota_stamps = stamps
            data = web_quota.view(state)
            if not data["sources"] and ("settings", web_sync.QUOTA_KEY) not in self.sent:
                return 0
            each = web_sync.item("settings", web_sync.QUOTA_KEY, {**data, "at": 0})  # ``at`` moves each time: not part of the rev.
            if self.sent.get((each["type"], each["key"])) == each["rev"]:
                return 0
            each["data"]["at"] = data["at"]
            try:
                self.client.request("POST", "/sync/upsert", {"items": [each], "deleted": []})
            except Exception as exc:  # noqa: BLE001 - logged; the next change or full look tries again
                self.quota_failed += 1
                self.quota_stamps = None
                if self.quota_failed == 1 or self.quota_failed % 10 == 0:
                    self.runtime.log(f"web quota not pushed ({self.quota_failed}): {type(exc).__name__}: {self.why(exc)}")
                return 0
            self.quota_failed = 0
            self.sent[(each["type"], each["key"])] = each["rev"]
            return 1
        except Exception as exc:  # noqa: BLE001 - snapshots that cannot be read stop nothing
            self.runtime.log(f"web quota not built: {type(exc).__name__}")
            return 0
        finally:
            self.quota_lock.release()

    # -- inbox ----------------------------------------------------------------------------------------------------------

    def inbox_once(self) -> int:
        """Fetch the page's operations and hand each to its lane (``OpLanes``: in order within a topic, side by side across topics),
        which does it, acknowledges it at once and only then runs what it left for after the acknowledgement (``after_ack``: the
        new task's card, the notices) and pushes what it touched (``touch_soon``). This step waits up to OP_WAIT seconds for what it
        handed out; one still running after that goes on in its lane and acknowledges itself. How many were new.

        The ledger has each before it is done (``take``): one of a step left behind that is still running is skipped (``inflight``)
        and acknowledged by that step when it ends, with its real outcome; one taken and never finished by this process (a restart)
        is failed as uncertain and never done again. With ``wait`` (INBOX_WAIT) a page that knows it (``waited`` in its answer,
        data contract: docs/web.md 更快的取数) holds the request until an operation comes; an older page answers at once, and the
        next ask waits poll_seconds as before."""
        if not self.env.state_dir:
            return 0
        if self.ledger is None:
            self.ledger = web_sync.Ledger(self.env.state_dir)
        ledger = self.ledger
        data = self.client.request("GET", f"/sync/inbox?after={ledger.after()}&limit=100&wait={INBOX_WAIT:g}")
        fetched = time.time()
        late, handed, fresh, busy = [], [], 0, False
        for row in data.get("items") or ():
            op_id, seq = str(row.get("op_id") or ""), row.get("seq")
            if not op_id or not isinstance(seq, int) or isinstance(seq, bool):
                continue
            with self.inflight_lock:
                if op_id in self.inflight:
                    busy = True
                    continue  # Being done (a lane, or a step left behind): it is acknowledged when it ends.
                known = ledger.get(op_id)
                if known is None:
                    ledger.take(op_id, seq)
                    self.inflight[op_id] = time.monotonic()
            if known is None:
                fresh += 1
                handed.append(self.op_lanes.put(self.lane_of(row), functools.partial(self.do_op, row, fetched)))
                continue
            if known.get("state") == "received":
                ledger.finish(op_id, "failed", "这条执行到一半没有结果（Covey 重启或卡住了），结果不确定，没有重发")
            saved = ledger.get(op_id)
            late.append({"seq": seq, "op_id": op_id, "state": saved["state"], "message": saved["message"]})
        # Held by the page until an operation came: ask again at once. Not when it answered because of one still being done here (it is
        # new on the page until its ack): that would ask without a pause until it ends.
        self.inbox_waited = data.get("waited") is True and not busy
        if late:  # Done before and not acknowledged (the page did not take the ack), or failed as uncertain just now.
            self.client.request("POST", "/sync/ack", {"results": late})
            ledger.acked([each["op_id"] for each in late])
        deadline = time.monotonic() + OP_WAIT
        for done in handed:
            if not done.wait(max(0.0, deadline - time.monotonic())):
                break
        return fresh

    @staticmethod
    def lane_of(row) -> str:
        """The lane of an operation: its topic's, its project's, ``upgrade``'s own (its check takes a minute: no setting waits for it),
        else the one of the chat-wide ones (settings, launch, tidy, memory)."""
        if row.get("kind") == "upgrade":
            return "upgrade"
        if row.get("topic_key"):
            return "topic:" + str(row["topic_key"])
        if row.get("project_key"):
            return "project:" + str(row["project_key"])
        return "chat"

    def do_op(self, row, fetched) -> None:
        """One operation in its lane: done, written down, acknowledged, then its follow-ups and the push of what it touched.
        A content-free line says where its seconds went (``wait``: from the page's row to this fetch, by the two clocks)."""
        ledger, op_id, seq, kind = self.ledger, str(row["op_id"]), row["seq"], str(row.get("kind") or "")
        began = time.monotonic()
        queued = (began - self.inflight.get(op_id, began)) * 1000
        try:
            with card_lanes.collecting() as later:
                state, message = self.ops.run(row)
            ledger.finish(op_id, state, message)
            ran = time.monotonic()
            touched = [str(row["topic_key"])] if row.get("topic_key") else []
            opened = self.ops.opened_topic(row)
            if opened:  # Else the page waits a minute for the topic it has just opened.
                touched.append(opened)
            for topic in touched:
                self.collector.touch(topic)
            self.wake.set()  # The quick look builds what the operation touched now, not at its next tick.
            if kind in ("settings", "launch", "tidy_decide", "memory_edit", "upgrade", "quota_refresh"):
                self.before_ack(kind)  # The page reads the new settings, the tidy list, the memory when it sees done.
            pushed = time.monotonic()
            try:
                self.client.request("POST", "/sync/ack", {"results": [{"seq": seq, "op_id": op_id, "state": state, "message": ledger.get(op_id)["message"]}]})
                ledger.acked([op_id])
            except WebError as exc:  # The page hands the row out again: the next step acknowledges it from the ledger.
                self.runtime.log(f"web op {kind} seq={seq} ack failed: {self.why(exc) or type(exc).__name__}")
            acked = time.monotonic()
            written = epoch_seconds(row.get("created_at")) if row.get("created_at") else None
            wait = f"wait={max(0.0, fetched - written) * 1000:.0f}ms " if written else ""
            self.runtime.log(f"web op {kind} seq={seq} {wait}queue={queued:.0f}ms run={(ran - began) * 1000:.0f}ms "
                             f"push={(pushed - ran) * 1000:.0f}ms ack={(acked - pushed) * 1000:.0f}ms")
        finally:
            with self.inflight_lock:
                self.inflight.pop(op_id, None)
        if touched:
            self.touch_soon(touched)  # What the page acts on is on it now, before the follow-ups' cards (an answer: its question closes).
        if later:
            card_lanes.run_follow_ups(later, self.runtime.log)
            if touched:
                self.touch_soon(touched)  # And what they made (the answer's new turn).

    def before_ack(self, kind) -> None:
        """What the page must read when it sees done: the settings and the tidy list for a change of either (a launch changes what the
        next one preselects), the memory for its tab."""
        chat = self.runtime.access.owner_chat()
        if not chat:
            return
        if kind == "upgrade":
            self.push_upgrade(full=True)  # The page reads what the check found, or that the upgrade is under way, when it sees done.
            return
        if kind == "quota_refresh":
            self.push_quota(full=True)  # The page reads that the reading is under way (or the snapshots as they are: it just did) when it sees done.
            return
        if kind in ("settings", "launch"):
            self.settings_due = True
        self.push_settings(chat, time.time())
        if kind == "tidy_decide":
            self.tidy_stamps = None
        if kind == "memory_edit":
            self.memory_stamps = None
        self.push_tidy(time.time())
        self.push_memory()
        if kind == "memory_edit":
            self.push_memory_center(force=True)  # The page reads the center as the change left it when it sees done.
