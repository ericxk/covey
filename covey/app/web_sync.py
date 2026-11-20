"""网页工作台同步 (docs/butler.md 网页工作台, COVEY_WEB=1 and COVEY_WEB_CONFIG): pushes the state of the owner's projects, topics, turns and
questions to the web page (the 妙搭 app) and fetches what the page sent, which app/web_ops.py does. Off by default: without the switch
there is no thread and no request.

- ``WebSync`` (app/unified.py PARTS ``web_sync``, a feature object: ``threads()``, none without the switch): two supervised threads. ``web-push``
  builds the items (``Collector``, app/web_collect.py), keeps those whose ``rev`` (core/web_sync.py) differs from what was sent, and posts them to
  /sync/upsert in batches; ``web-inbox`` asks /sync/inbox, writes each operation's seq and op_id into the ledger before it is done, does it
  (``Ops.run``) and acknowledges it at /sync/ack. A failed request backs off (2, 4 ... 30 seconds) and touches no other thread.
- What is read, and how often: every ``push_seconds`` only the running tasks and the pending questions (two index queries, the records they
  name), plus the topics that stopped running a moment ago or that the page just acted on; every FULL_SECONDS a full pass of all topics
  of the last TOPIC_DAYS days, PASS_SLICE of them a step before the hot ones (a pass of hundreds of topics is many steps, not one look of
  minutes in which nothing else goes out), the projects, and the deletions once the pass ends. A topic's state and "做完没看" are the board's reading (core/board_read.py, app/pins.py
  board_results), not a scan of every job record each second. The butler's conversation (core/butler.py butler_topic_id; the other tracks
  of the family and the leads are not) is one more topic item, ``kind`` "butler", number 0, title 管家, no project, never unseen: its
  turns are its history's (the owner's top-level words, the butler's answers), looked at while its task runs, after an operation of the page
  touched it and in the full look. A running turn's body is its progress notes as the trace file has them, each time it changes.
- A project's lead (COVEY_PROJECTS=1) is looked at the same way and is a topic of ``kind`` "lead" (title 负责人, number 0, its project's id as
  ``project_key``): the owner's words and its answers, a wake-up turn left out (its reply only when the lead meant it for the owner), and
  a line of the page's own (role event) for a member's end and for what a silent wake-up started; ``started_topics`` says which turn started
  which member. A turn's process, plan and figures (app/web_turns.py) and a question's times ride on the items; the memory and library rows
  say who wrote them (data contract v3).
- The key is read from the config's key_file and appears in no log line and no record.

Rule 1 (AGENTS.md): no lock is held across a request; records are read, not locked."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from covey.app import web_images
from covey.app.web_collect import Collector
from covey.app.web_ops import Ops
from covey.app.web_push_client import Client, Stuck, WebError, Worker
from covey.core import web_sync
from covey.core.state import read_object, write_json

FULL_SECONDS = web_sync.FULL_SECONDS
STEP_BYTES = web_sync.BATCH_BYTES  # What a step sends of the pass's items (the backlog) after the urgent ones: one batch.
SENT_FILE = "sent.json"  # <state>/web/: what the page took (key -> rev), so a restart sends only what changed since.
SENT_SAVE_SECONDS = 30.0  # It is written at most this often, and when a pass ends.
SENT_KEEP_SECONDS = 86400.0  # Once a day ``sent`` starts empty again, so everything goes once (a page whose database was emptied fills again).
MAX_BACKOFF = 30
STUCK_SECONDS = 60  # A step with no progress in this long is left behind (an NFS read can hold its thread for good); the next runs in a new thread.
MAX_LEFT = 3  # Left-behind steps still stuck: with this many no new thread is started until one of them ends, or LEFT_WAIT has passed.
LEFT_WAIT = 300  # Seconds waited at MAX_LEFT; then the next step runs in a new thread anyway (an open stuck for good must not stop the push).
IMAGES_WAIT = 300  # Seconds images are held back after the page did not take one (push_images).


class WebSync:
    """The feature object (``threads()``). ``runtime``: app/runtime.py Runtime; ``env``: app/butler_env.py ButlerEnv; ``hear``: app/unified.py
    web_message, how the owner's words to the butler get in (None: none can)."""
    name = "web-sync"

    def __init__(self, runtime, env, hear=None) -> None:
        self.runtime, self.env = runtime, env
        self.client = Client()
        self.stuck = {}  # topic id -> the left-behind thread stuck reading it: looks leave the topic out while that thread lives.
        self.images = None  # web_images.WebImages, made once the state directory is known (images_ready)
        self.collector = Collector(env, self.stuck, runtime.log)
        self.images_ready()
        self.ops = Ops(env, hear)
        self.sent = {}  # (type, key) -> the rev pushed last.
        self.backlog = {}  # (type, key) -> an item of a pass not sent yet, the newest read of it (push_once).
        self.sent_loaded, self.sent_saved, self.sent_changed = False, 0.0, False  # <state>/web/sent.json (load_sent, save_sent).
        self.sent_since = None  # When ``sent`` last started empty (wall clock).
        self.last_full = 0.0
        self.settings_due, self.settings_stamps = True, None  # The 设置 item: built on a full look, after the page changed one, or when its files did.
        self.settings_lock = threading.Lock()  # push_settings runs in both threads.
        self.tidy_stamps, self.tidy_lock, self.tidy_failed = None, threading.Lock(), 0  # The 整理专项 item (push_tidy).
        self.ledger = None
        self.down = {}  # thread name -> failures in a row.
        self.images_wait, self.images_down = 0.0, 0  # push_images: no sending before this time; failures in a row.

    def threads(self):
        if not web_sync.enabled():
            return ()
        return (("web-push", lambda ctx: self.loop("push", self.push_once, lambda: self.client.settings().push_seconds, self.start_over,
                                                   lambda: self.collector.moved)),
                ("web-inbox", lambda ctx: self.loop("inbox", self.inbox_once, lambda: self.client.settings().poll_seconds)))

    def loop(self, label, step, seconds, reset=None, moved=None) -> None:
        """``step`` every ``seconds()``, in a Worker; a failure is logged (the first, then every tenth) and waited out 2, 4 ... MAX_BACKOFF
        seconds. A step with no progress in STUCK_SECONDS (``moved()``: the monotonic time of its last; without it, the step's start) is
        logged and left behind: ``reset()`` drops what it was building and the next step runs in a new Worker (none while MAX_LEFT
        left-behind ones are still stuck)."""
        worker, left = Worker(f"web-{label}-step"), []
        while not self.runtime.shutdown.is_set():
            try:
                worker.call(step, STUCK_SECONDS, moved)
                if self.down.pop(label, 0):
                    self.runtime.log(f"web sync {label} recovered")
                wait = seconds()
            except Stuck:
                left = [each for each in (*left, worker) if each.thread.is_alive()]
                self.runtime.log(f"web sync {label} stuck: a step has not moved in {STUCK_SECONDS} s; left behind ({len(left)} so far)")
                began = time.monotonic()
                while len(left) >= MAX_LEFT and time.monotonic() - began < LEFT_WAIT and not self.runtime.shutdown.wait(
                        min(STUCK_SECONDS, LEFT_WAIT)):
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
                wait = min(MAX_BACKOFF, 2 ** min(failed, 5))
            if self.runtime.shutdown.wait(wait):
                break
        worker.steps.put(None)

    @staticmethod
    def why(exc) -> str:
        return str(exc)[:80] if isinstance(exc, (WebError, ValueError)) else ""

    # -- push -----------------------------------------------------------------------------------------------------------

    def start_over(self) -> None:
        """After a look was left behind: a new collector, so the next look starts a full pass. The left-behind
        look keeps writing only to the old ones and sends nothing (2026-11-19 14:36 one that came back an hour late put T542 back to 在做
        on the page). The topic it was stuck reading (an NFS open of its topic.json that never returned,
        2026-11-19) is left out of the looks while that thread lives, so the next look does not get stuck on it again."""
        old = self.collector
        stuck = {topic: thread for topic, thread in self.stuck.items() if thread.is_alive()}
        if old.at and old.thread is not None and old.thread.is_alive():
            stuck[old.at] = old.thread
            self.runtime.log(f"web sync push leaves topic {old.at[:12]} out while its read is stuck")
        self.stuck = stuck
        # What the page took stays known (``sent``): only what changed goes again, not every item (2026-11-19 23:52: a stuck read
        # made the next look push all ~7000 items again, minutes in which nothing new went out).
        self.collector, self.last_full = Collector(self.env, stuck, self.runtime.log, self.images), 0.0
        self.backlog = {}  # Read by the old one: the new pass reads them again.

    def push_once(self, now=None) -> int:
        """One look and one push; how many items went out. Raises WebError when the page did not take a batch (the batches before it count).
        A full look that has nothing to send still posts an empty batch, so the page's 最后同步 moves on in a quiet minute."""
        chat = self.runtime.access.owner_chat()
        if not chat or not self.env.state_dir:
            return 0
        if not self.sent_loaded:
            self.load_sent(now)
        self.images_ready()
        collector, sent = self.collector, self.sent
        now = time.time() if now is None else now
        full = not collector.pass_left and now - self.last_full >= FULL_SECONDS  # A full pass is due and none is under way.
        if full and (self.sent_since is None or now - self.sent_since >= SENT_KEEP_SECONDS):
            if self.sent_since is not None:
                sent.clear()  # A day on: everything once more, a step's worth at a time behind the hot topics.
                self.sent_changed = True
            self.sent_since = now
        items, deleted = collector.collect(chat, now, full)
        if collector is not self.collector:
            return 0  # Left behind while it looked: what it read is old, and must not wait in the backlog either.
        if full:
            self.push_settings(chat, now, full=True)
            self.push_tidy(now, full=True)
        # The hot topics' changes go first, whole, every step; the pass's wait in the backlog and go a batch a step after them, so
        # an answer or a start shows within seconds even while a restart's first pass is still sending (2026-11-20 00:31-00:53: four
        # restarts, each first pass ~20 MB over ~5 minutes, starts and ends 55-73 s late on the page).
        urgent = [each for each in items[:collector.urgent] if sent.get((each["type"], each["key"])) != each["rev"]]
        for each in urgent:
            self.backlog.pop((each["type"], each["key"]), None)
        for each in items[collector.urgent:]:
            name = (each["type"], each["key"])
            if sent.get(name) != each["rev"]:
                self.backlog[name] = each
            else:
                self.backlog.pop(name, None)
        later, size = [], 0
        for name, each in list(self.backlog.items()):
            weight = len(json.dumps(each, ensure_ascii=False).encode())
            if later and size + weight > STEP_BYTES:
                break
            later.append(each)
            size += weight
        count = 0
        for batch in web_sync.batches(urgent + later):
            if collector is not self.collector:
                return count  # Left behind (start_over) while it looked: what it read is old, the page would go back to it.
            self.client.request("POST", "/sync/upsert", {"items": batch, "deleted": []})
            collector.moved = time.monotonic()  # The first push of a big chat is many batches: each one taken is progress.
            for each in batch:
                name = (each["type"], each["key"])
                sent[name] = each["rev"]
                if self.backlog.get(name) is each:
                    del self.backlog[name]
            count += len(batch)
            self.sent_changed = True
        if collector is not self.collector:
            return count
        if deleted or (collector.pass_done and not count):
            self.client.request("POST", "/sync/upsert", {"items": [], "deleted": deleted})
            gone = {each["key"] for each in deleted if each["type"] == "topic"}
            for each in deleted:
                sent.pop((each["type"], each["key"]), None)
            for name, each in list(self.backlog.items()):  # Nothing of a topic deleted goes after it.
                if each["key"] in gone or str((each.get("data") or {}).get("topic_key") or "") in gone:
                    del self.backlog[name]
            self.sent_changed = True
        if collector is self.collector:
            self.push_images(collector, now)
        if full and collector is self.collector:
            self.last_full = now
        if self.sent_changed and (collector.pass_done or time.monotonic() - self.sent_saved >= SENT_SAVE_SECONDS):
            self.save_sent(now)
        return count

    def images_ready(self) -> None:
        """The images' ledger, once the state directory is known: the unified Bridge builds this object before build_context fills
        the butler env's state directory in (2026-11-20, generation 452 pushed no image because of it)."""
        if self.images is not None or not self.env.state_dir:
            return
        self.images = web_images.WebImages(self.env.state_dir, self.runtime.log)
        self.collector.images = self.images
        self.collector.backfill = not self.images.backfilled

    def push_images(self, collector, now) -> None:
        """Some of the images the turns asked for (web_images.WebImages.upload); their topics are built again on the next look, now with
        them. A page that does not take them (an older page without /sync/image, a refusal) only holds the images back IMAGES_WAIT
        seconds: the items go on as before."""
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
            collector.touch(topic)

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
        """The ``settings`` item posted on its own when it is due and changed (``settings_items``): from the inbox thread each second, so a
        change the page made is on the page before its ack, and a change on the card a second later, however long the push thread's full look
        takes (minutes in a big chat on a network disk: 2026-11-19 T554, the page's change came back after 3 min 40 s); and from the push
        thread's full look. One at a time (``settings_lock``). How many went (0 or 1); a failed post is logged and tried on the next look."""
        if not self.settings_lock.acquire(blocking=False):
            return 0  # The other thread posts it, or a look left behind still holds it (a read on the network disk that hangs).
        try:
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

    def push_tidy(self, now, full=False) -> int:
        """The ``settings`` item ``tidy`` (整理专项's suggestions, app/project_tidy.py web_view, data contract v16), posted on its own: when
        the runs or the proposals changed (a stat of two folders, from the inbox thread each second) and on a full look. A page older than
        v16 refuses the key: that is logged (the first time, then every tenth) and tried again only on the next full look or change, and
        never holds up the settings, which go in a request of their own. How many went (0 or 1)."""
        tidy = getattr(self.env, "tidy", None)
        if tidy is None or not getattr(self.env, "state_dir", None) or not self.tidy_lock.acquire(blocking=False):
            return 0
        try:
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

    # -- inbox ----------------------------------------------------------------------------------------------------------

    def inbox_once(self) -> int:
        """Fetch, do and acknowledge the page's operations; how many were new."""
        if not self.env.state_dir:
            return 0
        if self.ledger is None:
            self.ledger = web_sync.Ledger(self.env.state_dir)
        ledger = self.ledger
        data = self.client.request("GET", f"/sync/inbox?after={ledger.after()}&limit=100")
        results, fresh = [], 0
        for row in data.get("items") or ():
            op_id, seq = str(row.get("op_id") or ""), row.get("seq")
            if not op_id or not isinstance(seq, int) or isinstance(seq, bool):
                continue
            known = ledger.get(op_id)
            if known is None:
                fresh += 1
                ledger.take(op_id, seq)
                state, message = self.ops.run(row)
                ledger.finish(op_id, state, message)
                if row.get("topic_key"):
                    self.collector.touch(row["topic_key"])
                opened = self.ops.opened_topic(row)
                if opened:  # 2026-11-20 live: the page waited a minute for the topic it had just opened.
                    self.collector.touch(opened)
                if row.get("kind") in ("settings", "launch"):  # A launch changes what the next one preselects.
                    self.settings_due = True  # Pushed below, before the ack: the page reads the new settings when it sees done.
                if row.get("kind") == "tidy_decide":
                    self.tidy_stamps = None  # Pushed below, before the ack, as the settings are.
            elif known.get("state") == "received":
                ledger.finish(op_id, "failed", "这条执行到一半没有结果（Covey 重启或卡住了），结果不确定，没有重发")
            saved = ledger.get(op_id)
            results.append({"seq": seq, "op_id": op_id, "state": saved["state"], "message": saved["message"]})
        chat = self.runtime.access.owner_chat()
        if chat:
            self.push_settings(chat, time.time())
            self.push_tidy(time.time())
        if results:
            self.client.request("POST", "/sync/ack", {"results": results})
            ledger.acked([each["op_id"] for each in results])
        return fresh
