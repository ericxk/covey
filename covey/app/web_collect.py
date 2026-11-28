"""网页工作台同步的收集 (app/web_sync.py): ``Collector`` reads the records of the owner's chat and makes the items (topics, turns, questions, projects)
the page is sent. Reading only: it sends nothing and holds no lock (rule 1, AGENTS.md). The reading rules are in app/web_sync.py's docstring."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import threading
import time
from pathlib import Path

from covey.app import owner_ledger, owner_send, pins, project_lead, project_loop, revisits as revisit_rules, topic_links, web_images, web_pairing, web_room, web_said, web_switch, web_turns
from covey.app.project_facts import rebound
from covey.core import board_read, collab, collab_records, look, project_actions, project_checklist, project_library, project_memory, projects, revisits, store, turn_source, turn_words, web_sync
from covey.core import owner_send as owner_send_records
from covey.core import memory as core_memory
from covey.core import progress as turn_progress
from covey.core.history_storage import history_lines
from covey.core.butler import butler_topic_id
from covey.core.checkpoint import read_checkpoint, upload_ledger
from covey.core.jobs import job_key, select_job_records, topic_job_records
from covey.core.model_names import model_name
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import ENDED, OPEN_QUESTIONS, RUNNING
from covey.core.store import directory_store
from covey.core.topics import topic_file
from covey.core.trace_store import load_trace

PASS_SLICE = 25  # Topics of a full pass looked at per step of the pass thread (a pass is many steps, never one long look).
TOPIC_DAYS = 30  # A full look takes the topics with a job written within this many days.
# A topic the pass leaves unbuilt because its inputs did not change (Collector.unchanged) is built again all the same at the latest
# this long after its last build, for what its inputs cannot say (``limit``: each topic's own, between half of it and all of it): an
# upload's ledger 重新上传 rewrote, an image another topic sent up, one gone with its message after KEEP_DAYS.
REBUILD_SECONDS = 1800.0
FOLDER_SECONDS = 60.0  # A folder the pass reads whole (Collector.folder) is listed again at least this often, changed or not.
MENU_SECONDS = 60.0  # How long the model menu a topic's ``config`` reads is kept (Collector.menu).
# The quick look reads the board again at least this often (``Shared.board_at``), whatever its indexes say: 可能卡住 comes with time,
# and a proposal of 整理专项, a project a guard paused or a result seen in Feishu touch none of the indexes it reads. A pass reads it
# when it starts, but on the network disk a pass can take many minutes (its steps stuck 60 s and more: ``web sync pass stuck``).
BOARD_SECONDS = 60.0
TURNS_KEPT = 100  # The newest turns of a topic that are pushed.
STATE_WORDS = {"done": "做完", "waiting_user": "等你", "cancelled": "已停止", "failed": "失败", "interrupted": "失败"}
BUTLER_TITLE = "管家"
LEAD_TITLE = "负责人"
EXTRA_TURNS = 20  # The newest turns of a topic that get their process and plan; every kept turn gets its figures (each read from its trace file once).
# A turn that ended more than this long ago goes without its process (``steps``): most of the page's database was the process of
# old turns (37.6 of 61 MB of its turns, 2026-11-27), and the page is opened for what is going on now. Its plan, figures and words stay.
STEPS_SECONDS = 7 * 86400
FIGURES_KEPT = 8192  # Ended turns whose figures are remembered (a few numbers each): all kept turns of all topics a full look builds.
# The caches go oldest first, a quarter at a time, never all at once (``Shared.keep``): a cleared cache read every topic's
# history and trace files again in the next steps (2026-11-23 T673: 512 ended turns' extras were cleared every 26 topics).
HISTORY_KEPT = 512  # Topics whose history lines are kept (Collector.rows).
EXTRA_KEPT = 1024  # Ended turns whose process and plan are kept (Collector.ended_extras): up to 80 steps each, so not every one a pass reads.
FILES_GOING, FILES_SETTLED = 15.0, 300.0  # Seconds a turn's 产物 are kept before they are read again: while one goes up, and after.
RECEIPTS_KEPT = 200  # The newest receipts of a project's inbox that are looked at.
EDIT_PREFIX = "edit-"  # The message id of the owner's edit sent into a running task (app/source_watch.py): edit-<message id>-<stamp>.
UNSTARTED_WORDS = "没开工，已取消"  # The line under the owner's words whose task was cancelled before it started (its record's reason after it).
# The line under the owner's words whose task waits for 开始 on its launch card (``start_turn``); the page puts 等你： before it.
START_WORDS = "在启动卡上点开始，这一轮才会开工"
LIVE = ("running", "cancel_requested")  # The statuses whose turn has a topic.data progress.
REPLY_MESSAGE = re.compile(r"collab-([0-9a-f]{24})-reply-\d+(?:-r\d+)?")  # core/collab.py reply_message: the integrator's continuation turn.
CONTINUED_WORDS = "（协作的答复到齐，接着汇总）"  # What the page shows of the long prompt the Bridge wrote for that turn.
NOTES_SHOWN = 10  # The newest milestones and decisions a project item carries (its 进度 tab on the page, docs/web.md 专项条目).
CHECKLIST_SHOWN = ("id", "criterion", "topic", "needs", "evidence", "gap", "state", "at")  # What an item of the 目标检查表 carries.
ACTION_SHOWN = ("id", "title", "note", "items", "topics", "state", "at")  # What an action of the 简报 carries.
GAP_TEXT = 200  # Characters of a 协作 gap a project item carries.
NEXT_STEP, WAITING_TEXT = 120, 160  # Characters of a running topic's next step and of what it waits for the owner to answer.


def board_marks(running, asking, starting=None) -> frozenset:
    """What the board's 要你处理 turns on, as the quick look's indexes read it: each task running (its topic, number and status), each
    question pending (its token, and whether it ``asks``: shown, its card sent) and each task waiting for 开始 (its topic, key and
    card). The quick look reads the board again when these change, not only when the topics among them do: a topic whose task runs,
    asks the owner and runs on once the owner answered on its card is among them all along. 2026-11-29 T978: the last two questions
    answered on their cards by 01:02:31 and none pending, the page said 1 件事要你处理 at 01:04 and drew no topic under 等你."""
    return frozenset([
        *(("job", str(record.get("topic_id") or ""), str(record.get("job_id") or ""), str(record.get("status") or ""))
          for record in running),
        *(("question", str(row.get("topic_id") or ""), str(row.get("token") or ""), board_read.asks(row)) for row in asking),
        *(("start", str(topic), str(wait.record.get("key") or ""), wait.card) for topic, wait in (starting or {}).items())])


def estimate_of(job, turn) -> None:
    """``turn`` takes ``estimate_seconds``, the first estimate of its whole 用时 the task monitor kept on the job record
    (core/longtask.py), or loses it: a turn whose task never had one has none."""
    value = job.get("estimate_seconds")
    if type(value) is int and value > 0:
        turn["estimate_seconds"] = value
    else:
        turn.pop("estimate_seconds", None)



def steps_expired(turn, now) -> bool:
    """Whether an ended turn's process is no longer pushed (STEPS_SECONDS after it ended); a turn without an end (running, waiting)
    keeps it, as does any turn while ``now`` is not known."""
    ended = turn.get("ended_at") or 0
    return bool(ended) and bool(now) and now - float(ended) > STEPS_SECONDS


class Shared:
    """What the two collectors of one WebSync share (the quick look's, ``collect_hot``, and the pass's, ``collect_pass``, each used by
    one thread) and what a collector made again after a stuck read keeps (WebSync.start_over): the caches of what ended turns and
    histories read, so a new collector does not read every topic's files again; the board's reading; which topics the page has.
    A left-behind thread that comes back late may still write a cache entry: each is keyed by what it was read from (a turn's
    record time, a file's stamp), so a late one is right or read again, never wrong. Compound changes take ``lock``, no read
    of a file or a request under it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seq = itertools.count(1)  # Each topic's read takes the next (``next_seq``): the newer read of a topic has the higher.
        self.history = {}  # topic id -> ((mtime_ns, size), rows)
        self.extra = {}  # (job key, updated_at) -> the optional fields of an ended turn (web_turns.extras).
        self.figures = {}  # (job key, updated_at) -> the figures of an ended turn (web_turns.ended_figures).
        self.files = {}  # (job key, updated_at) -> (when read, the turn's 产物) (``artifacts``, data contract v32).
        self.receipts = {}  # project id -> {receipt file name: its row} (thread_end only).
        self.numbers = {}  # topic id -> its T number once it has one (a number is never changed): a project's rows name it.
        self.project_rows = {}  # project id -> (what it was built from, its item) (Collector.project_items).
        self.job_seqs = {}  # topic id -> {job id: seq of its answer turn}, of a topic in a collaboration (the evidence's seq).
        self.states = {}  # topic id -> its state word, from its last build (a project's 等你 reads it).
        self.unpaired = {}  # topic id -> its answer turns with no task record, as last logged (count_unpaired).
        self.unseen = set()
        self.board = None  # topic id -> its group on the board (``board_groups``); None without the board (COVEY_PINNED_TOPICS unset).
        self.asks = None  # 要你处理 as every card counts it ({waiting, stopped, stuck}, core/board_read.py Asks.counts), data contract v34.
        self.board_at = -math.inf  # ``now`` of the last look that read the board (Collector.read_board), the quick look's or the pass's.
        self.pushed_topics, self.pass_seen, self.pushed_collabs = set(), set(), set()
        # topic id -> the left-behind thread stuck reading it, or a tuple of them (a quick look's, then a touch's or a reread's, app/web_sync.py):
        # looks leave the topic out while one of them lives (``hold``, ``alive_stuck``).
        self.stuck = {}
        self.dirty = set()  # The topics the next quick look builds whatever the indexes say (Collector.touch).
        self.unbuilt = {}  # topic id -> the error its build raised, as last logged (Collector.build_each).
        # topic id -> (its inputs as the pass's last build of it read them, ((type, key), rev) of each item that build made, when it
        # is built again whatever its inputs say): Collector.unchanged. Only the pass's thread writes it.
        self.prints = {}
        # The room's record files as last read (app/web_room.py Files): a collector made again, or the touch's, reads only what changed.
        self.room_files = web_room.Files()

    def next_seq(self) -> int:
        return next(self.seq)

    def recall(self, cache, key):
        """``cache[key]``, made the newest, or None."""
        with self.lock:
            value = cache.pop(key, None)
            if value is not None:
                cache[key] = value
            return value

    def keep(self, cache, key, value, limit) -> None:
        """``cache[key] = value``; over ``limit`` the oldest quarter goes."""
        with self.lock:
            cache.pop(key, None)
            cache[key] = value
            if len(cache) > limit:
                for old in list(itertools.islice(cache, max(1, len(cache) // 4))):
                    cache.pop(old, None)

    def built(self, topics) -> None:
        """``topics`` were built and their items handed to the push: the page has them, and the pass under way has seen them."""
        names = {("topic", topic) for topic in topics}
        with self.lock:
            self.pushed_topics |= names
            self.pass_seen |= names

    def alive_stuck(self) -> set:
        """The topics whose read is stuck in a thread still alive."""
        with self.lock:
            for topic in list(self.stuck):
                self.keep_alive(topic)
            return set(self.stuck)

    def hold(self, topic, thread) -> None:
        """``thread`` is stuck reading ``topic``: the topic is left out while it lives, or one stuck on it before (not replaced)."""
        with self.lock:
            threads = self.keep_alive(topic)
            if thread not in threads:
                threads += (thread,)
            self.stuck[topic] = threads if len(threads) > 1 else threads[0]

    def keep_alive(self, topic) -> tuple:
        """Under ``lock``: the threads still stuck on ``topic``, the dead ones dropped (the topic too when none lives)."""
        held = self.stuck.get(topic)
        threads = () if held is None else held if isinstance(held, tuple) else (held,)
        alive = tuple(thread for thread in threads if thread.is_alive())
        if not alive:
            self.stuck.pop(topic, None)
        elif len(alive) != len(threads):
            self.stuck[topic] = alive if len(alive) > 1 else alive[0]
        return alive


class Collector:
    """The items of the owner's chat from the records (see the module's reading rules). One thread uses it: the quick look's
    (``collect_hot``) or the pass's (``collect_pass``); WebSync.push_once runs one of each in turn. What two collectors share is
    ``shared``."""

    def __init__(self, env, stuck=None, log=None, images=None, shared=None) -> None:
        self.env = env
        self.shared = Shared() if shared is None else shared
        if stuck is not None:
            self.shared.stuck = stuck
        self.images = images  # web_images.WebImages (data contract v19); None: turns carry no images (their words lose local paths all the same).
        self.now = 0.0  # The time of the look under way.
        self.backfill = images is not None and not images.backfilled  # The first pass looks at every topic once (their images).
        self.backfilling, self.backfill_extra = False, set()  # The pass under way is that one; the topics only it looks at.
        self.left = set()  # The topics the last look left out because their read is stuck.
        self.at, self.thread = "", None  # The topic being built now, and the thread that looks.
        self.doing = ""  # What the look is at, for the log line of one left behind (WebSync.loop): indexes, board, topic, projects...
        self.retired = False  # Left behind (WebSync.start_over_pass): a pass it ends late changes nothing the new one keeps.
        self.hot = set()  # The topics the last quick look found running, asking or waiting for 开始.
        self.marks = frozenset()  # What the board's 要你处理 turns on, as the last quick look read its indexes (``board_marks``).
        # The full pass under way: the topics still to look at, and whether the last step ended it.
        self.pass_left, self.pass_done = [], False
        self.urgent = 0  # How many of the last look's items, at its head, are the hot topics' (WebSync.push_once sends them first).
        self.groups = []  # The last look's items by whose they are: [(topic or "project:<id>", seq of its read, items)] (WebSync).
        self.projects_due = True  # The next quick look builds every project again, not only the ones that changed (a pass started).
        self.history, self.extra, self.figures, self.files = self.shared.history, self.shared.extra, self.shared.figures, self.shared.files
        self.receipts, self.job_seqs, self.states, self.unpaired = self.shared.receipts, self.shared.job_seqs, self.shared.states, self.shared.unpaired
        self.leads = {}  # lead topic id -> its project's record (COVEY_PROJECTS=1 only), from this look.
        self.projects = {}  # project id -> its record, from this look (a member topic's ``joined_at``).
        self.log = log  # The bridge.log writer (WebSync's runtime.log); None: nothing logged.
        # Whom the butler's turns handed on to and which topic turns went to the private chat (data contract v23).
        self.room = web_room.Room(self.shared.room_files)
        self.origins = None  # web_said.Origins of the owner's chat: who wrote each hand-on, read once.
        self.moved = 0.0  # Monotonic time of the last progress of a look and its push: a topic built, a batch sent (WebSync.loop's stuck check).
        self.collab_cache, self.collab_primed = {}, False  # name -> ((mtime_ns, size), record) of <state>/collab/ (read_collabs).
        self.collab_now, self.collab_helpers, self.collab_topics = {}, {}, set()  # This look's: id -> record (the chat's), helper topic -> records, topics in any.
        self.menu_at, self.menu_models = -MENU_SECONDS, {}  # The model menu (available_models) a topic's ``config`` reads, read again after MENU_SECONDS.
        self.due = math.inf  # When the items of the topic being built change by themselves (``files_due``): it is built again by then.
        # What this look of the pass read for every topic's inputs (``look_inputs``): its share of them (a hash), the 到点回来看 by topic
        # and the 代发 drafts by topic (``folder``: path -> (the folder's stamp, when listed, what was made of it)).
        self.look_rev, self.later, self.drafts, self.folders = "", {}, {}, {}

    # What the two collectors share, as attributes of each.
    stuck = property(lambda self: self.shared.stuck, lambda self, value: setattr(self.shared, "stuck", value))
    unseen = property(lambda self: self.shared.unseen, lambda self, value: setattr(self.shared, "unseen", value))
    board = property(lambda self: self.shared.board, lambda self, value: setattr(self.shared, "board", value))
    asks = property(lambda self: self.shared.asks, lambda self, value: setattr(self.shared, "asks", value))
    pushed_topics = property(lambda self: self.shared.pushed_topics, lambda self, value: setattr(self.shared, "pushed_topics", value))
    pass_seen = property(lambda self: self.shared.pass_seen, lambda self, value: setattr(self.shared, "pass_seen", value))
    pushed_collabs = property(lambda self: self.shared.pushed_collabs, lambda self, value: setattr(self.shared, "pushed_collabs", value))

    def menu(self) -> dict:
        """The launch card's model menu (app/launch_models.py available_models), read at most every MENU_SECONDS; {} when it cannot be read
        (a topic's ``config`` then names its own model as it is)."""
        now = time.monotonic()
        if now - self.menu_at >= MENU_SECONDS:
            self.menu_at = now
            try:
                from covey.app.launch_models import available_models
                from covey.app.web_launch import base_app
                self.menu_models = available_models(self.env.topics, base_app(self.env)[1])
            except Exception:  # noqa: BLE001 - a push never fails for it
                self.menu_models = {}
        return self.menu_models

    dirty = property(lambda self: self.shared.dirty)

    def touch(self, topic) -> None:
        """The page acted on ``topic`` (or a collaboration of it changed): the next quick look builds it whatever the indexes say."""
        with self.shared.lock:
            self.shared.dirty.add(str(topic))

    def mark(self, doing) -> None:
        """Progress: the look is at ``doing`` now (WebSync.loop's stuck check reads ``moved``, its log line ``doing``)."""
        self.doing, self.moved = doing, time.monotonic()

    def look_begins(self, chat, now) -> Path:
        """What every look reads first: who said each hand-on, the private chat's room, the collaborations. The state directory."""
        state = Path(self.env.state_dir)
        self.thread = threading.current_thread()
        self.now = now
        self.mark("room and collaborations")
        if self.origins is None or self.origins.chat != str(chat):
            self.origins = web_said.Origins(state, chat)
        self.room.refresh(state)
        self.read_collabs(state, chat)
        return state

    def read_projects(self, state, chat) -> tuple:
        """(the chat's project records, the conversations not on the board: the butler's and the projects' leads)."""
        records = projects.records(state, chat)
        self.leads = {str(record.get("lead_topic_id")): record for record in records if record.get("lead_topic_id")} if projects.enabled() else {}
        self.projects = {str(record.get("id")): record for record in records}
        return records, [butler_topic_id(chat), *self.leads]

    def read_board(self, state, chat, now) -> None:
        """The board's reading (app/pins.py): which results the owner has not seen, each topic's group, 要你处理."""
        read = board_read.read(state, chat, now, keep=pins.kept(state, chat) if pins.laid() else ())
        looked = pins.PinnedTopics(None, None)
        looked.look(state, chat, now, read, board=pins.laid())
        self.unseen = set(looked.unseen)
        asked = self.asked(state, chat, now, looked)
        self.board = board_groups(looked.rows, looked.dismissed, asked) if pins.laid() else None
        self.asks = dict(asked.counts()) if asked is not None else None
        self.board_due, self.shared.board_at = False, now

    board_due = True  # The quick look reads the board before its first build (a pass reads it when it starts).

    def build(self, state, chat, topic, now, sent=None) -> tuple:
        """(seq, items) of ``topic``: its records read now, after the seq is taken (``Shared.next_seq``). ``sent`` (the pass's: what the
        page has, (type, key) -> rev): its inputs are read first (``inputs``), before anything the build reads, and when they are those of
        its last build and the page has what that build made (``unchanged``), nothing is built: (seq, None)."""
        seq = self.shared.next_seq()
        self.doing = f"topic {topic[:12]}"
        records = sorted(topic_job_records(state, topic, chat_id=chat), key=board_read.job_number)
        self.at = topic
        inputs = self.inputs(state, chat, topic, records) if sent is not None else None
        if sent is not None and self.unchanged(topic, inputs, now, sent):
            self.moved = time.monotonic()
            return seq, None
        self.due = math.inf
        items = self.topic_items(state, chat, topic, records, now)
        if sent is not None:
            self.keep_inputs(topic, inputs, items, now)
        self.moved = time.monotonic()
        return seq, items

    def build_each(self, state, chat, topics, now, sent=None, always=()) -> tuple:
        """([(topic, seq, items)], the topics whose build raised, the topics left unbuilt) of ``topics``, one at a time. One that raises
        (a record of a shape the build does not know, a read that failed) is left out of this look and logged once per topic and error;
        the others go on. A busy index (core/store.py Busy) ends the look: nothing of it is sent. ``sent``: the pass's (``build``); the
        topics in ``always`` are built whatever their inputs (the butler's and the leads' conversations read other topics' files)."""
        groups, failed, kept = [], [], []
        for topic in topics:
            try:
                seq, mine = self.build(state, chat, topic, now, None if topic in always else sent)
            except store.Busy:
                raise
            except Exception as exc:  # noqa: BLE001 - one topic must not hold up the others
                failed.append(topic)
                why = f"{type(exc).__name__}: {str(exc)[:80]}"
                if self.shared.unbuilt.get(topic) != why:
                    self.shared.unbuilt[topic] = why
                    if self.log is not None:
                        self.log(f"web sync topic {topic[:12]} not built: {why}")
                continue
            if mine is None:
                kept.append(topic)
                continue
            self.shared.unbuilt.pop(topic, None)
            groups.append((topic, seq, mine))
        self.at = ""
        return groups, failed, kept

    def collect_hot(self, chat, now) -> list:
        """The quick look (WebSync.push_hot, every push_seconds and when this process wrote a job or a question): the topics that
        run, ask or wait for 开始 (three index queries), the ones that stopped a moment ago or the page just acted on, the butler's
        and a lead's conversation while one of those, then the projects (built again only when one changed, every one after a pass
        started) and the collaborations. All its items are urgent; ``groups`` says whose each is and when it was read."""
        state = self.look_begins(chat, now)
        out = self.shared.alive_stuck()
        back = self.left - out  # A stuck read that came back: its topic is looked at again now.
        self.mark("indexes")
        running = board_read.running_jobs(state, chat)
        asking = board_read.pending_questions(state, chat)
        starting = board_read.starts(state, chat)
        records, family = self.read_projects(state, chat)
        hot = {row.get("topic_id") for row in (*running, *asking) if row.get("topic_id")}
        hot |= set(starting)  # Waiting for 开始 on a launch card: 等我 as a question is.
        live, family_marks = self.family_live(state, chat, family)
        hot |= live  # A turn of one runs or asks (the board does not list them).
        marks = board_marks(running, asking, starting) | family_marks
        with self.shared.lock:
            asked = set(self.shared.dirty)  # Taken off once this look went through: one given up leaves them to the next.
        touched = asked | back
        # Not only when topics come and go: one that runs, asks and runs on (answered on its card) stays hot all along (``board_marks``).
        if (self.board_due or hot != self.hot or marks != self.marks or touched
                or now - self.shared.board_at >= BOARD_SECONDS):
            self.mark("board")
            self.read_board(state, chat, now)
        quick = [topic for topic in hot | self.hot | touched if topic not in family and topic not in out]
        looked = [topic for topic in family if (topic in hot or topic in self.hot or topic in touched) and topic not in out]
        groups, _, _ = self.build_each(state, chat, [*quick, *looked], now)
        self.shared.built([topic for topic, _, mine in groups if topic in quick or mine])
        self.mark("projects")
        seq = self.shared.next_seq()
        made = self.project_items(state, chat, records, force=self.projects_due) + self.collab_items()
        self.projects_due = False
        groups += [(f"{each['type']}:{each['key']}", seq, [each]) for each in made]
        # The look went through: what it was asked to build is done with. A topic whose build raised was logged; it is built again when
        # it runs, asks or is touched again, and by every pass (kept in the hot set it would read the board again at every look).
        self.hot, self.left, self.marks = hot, out, marks
        with self.shared.lock:
            self.shared.pushed_collabs |= {each["key"] for each in made if each["type"] == "collab"}
            self.shared.dirty -= asked
        self.groups = groups
        items = [each for _, _, mine in groups for each in mine]
        self.urgent = len(items)
        self.mark("")
        return items

    def collect_touched(self, chat, topics, now, skip=()) -> list:
        """A look of its own at ``topics``, the ones the page just acted on (WebSync.touch_once): those alone are built, with the
        board's reading the last quick look left in ``shared`` (it reads the board again soon after: Collector.touch). One whose read
        is stuck in a left-behind quick look or pass is read again here all the same: a call hung on the network disk is that call's,
        a new read of the same files comes back at once (2026-11-23 T673), and the page waits for what it just did (2026-11-26 T837:
        an answer to T732 reached the page 3.5 minutes late, behind a quick look stuck on it). Only those in ``skip``, the ones a
        touch's own read hangs on, are left to the quick look. ``groups`` says whose each item is and when it was read."""
        state = self.look_begins(chat, now)
        self.mark("projects")
        self.read_projects(state, chat)
        groups, _, _ = self.build_each(state, chat, [topic for topic in topics if topic not in skip], now)
        self.shared.built([topic for topic, _, _ in groups])
        self.groups = groups
        self.mark("")
        return [each for _, _, mine in groups for each in mine]

    def collect_pass(self, chat, now, full, skip=(), sent=None) -> tuple:
        """One step of the full pass (WebSync.pass_once, its own thread: a slow read here holds up no quick look). When ``full`` and
        none is under way a pass starts: the board read again, every topic with a job written in the last TOPIC_DAYS days, the
        butler's and the leads' conversations. Each step looks at the next PASS_SLICE topics; those in ``skip`` (the quick look builds
        them every step) are left to it. With ``sent`` (what the page has, (type, key) -> rev) a topic whose inputs are those of its
        last build is left unbuilt when the page has what that build made (``unchanged``): it is seen all the same, never deleted.
        (items, deleted): ``deleted`` once the pass ended, the topics and collaborations pushed before that it did not see. Nothing
        to do: ([], [])."""
        start = full and not self.pass_left  # A new pass only once the last one has ended (``full``: one is due).
        self.pass_done, self.groups, self.urgent = False, [], 0
        if not start and not self.pass_left:
            return [], []
        state = self.look_begins(chat, now)
        out = self.shared.alive_stuck()
        self.left = out
        skip = set(skip)
        self.mark("indexes")
        records, family = self.read_projects(state, chat)
        if sent is not None:
            self.look_inputs(state)
        if start:
            self.mark("board")
            self.read_board(state, chat, now)
            backfill, self.backfill = self.backfill, False  # Every topic once, not only the recent ones (images of older turns).
            self.backfilling = backfill
            jobs = board_read.topic_jobs(state, chat)
            self.pass_left = [topic for topic, records_ in jobs.items() if topic in skip or self.recent(records_, now)]
            # The topics only that pass looks at: once it ends they are forgotten as pushed, so the next pass does not delete them.
            self.backfill_extra = {("topic", topic) for topic in jobs if topic not in self.pass_left} if backfill else set()
            self.pass_left += [key for _, key in sorted(self.backfill_extra)]
            with self.shared.lock:
                self.shared.pass_seen = set()
        slice_, left = [], set()
        while self.pass_left and len(slice_) < PASS_SLICE:
            topic = self.pass_left.pop(0)
            if topic in skip or topic in out:
                left.add(topic)
            else:
                slice_.append(topic)
        looked = [topic for topic in family if topic not in out and topic not in skip] if start else []
        groups, failed, kept = self.build_each(state, chat, [*slice_, *looked], now, sent=sent, always=set(family))
        # Built, or left unbuilt as the page has it: the page has them, and this pass has seen them.
        self.shared.built([topic for topic, _, mine in groups if topic in slice_ or mine] + kept)
        with self.shared.lock:  # Left to the quick look, stuck, or its build raised: still on the page, not deleted at the pass's end.
            self.shared.pass_seen |= {("topic", topic) for topic in left | set(failed)}
        self.pass_done = not self.pass_left
        deleted = []
        if self.pass_done and not self.retired:
            with self.shared.lock:
                # A topic left out (its read stuck) is still on the page, and so is one the quick look builds: not deleted.
                seen = self.shared.pass_seen | {("topic", topic) for topic in out | skip}
                deleted += [{"type": kind, "key": key} for kind, key in sorted(self.shared.pushed_topics - seen)]
                self.shared.pushed_topics = seen - self.backfill_extra
                for topic in [topic for topic in self.shared.prints if ("topic", topic) not in self.shared.pushed_topics]:
                    del self.shared.prints[topic]  # Deleted, or only a backfill looked at it: its next build is a whole one.
                current = set(self.collab_now)
                deleted += [{"type": "collab", "key": key} for key in sorted(self.shared.pushed_collabs - current)]
                self.shared.pushed_collabs = current
            if self.backfilling:
                self.backfilling, self.backfill_extra = False, set()
                self.images.mark_backfilled(now)
        self.groups = groups
        self.mark("")
        return [each for _, _, mine in groups for each in mine], deleted

    # -- what a topic is built from (the pass leaves one unbuilt whose inputs did not change) -------------------------------------

    def look_inputs(self, state) -> None:
        """What this look of the pass reads once for every topic's ``inputs``: the switches a build reads and the model menu (its share,
        ``look_rev``), the 到点回来看 by topic and the 代发 drafts by topic (``folder``: each folder listed again only when it changed)."""
        from covey.core import project_proposals

        self.look_rev = web_sync.rev([projects.enabled(), project_proposals.enabled(), revisits.enabled(), owner_send.enabled(),
                                      turn_source.enabled(), pins.laid(), self.images is not None, self.menu()])
        self.later = self.folder(Path(state) / revisits.FOLDER, stamps_by_name) if revisits.enabled() else {}
        self.drafts = self.folder(owner_send_records.folder(state), drafts_by_topic) if owner_send.enabled() else {}

    def folder(self, path, make) -> dict:
        """``make(the folder's *.json files)``, made again only when the folder changed or FOLDER_SECONDS after it was last made (every
        writer of these renames a whole new file in, so the folder's stamp moves with each write; the time is for a stamp that did not)."""
        stamp = file_stamp(path)
        kept = self.folders.get(str(path))
        if kept is not None and kept[0] == stamp and self.now - kept[1] < FOLDER_SECONDS:
            return kept[2]
        try:
            files = sorted(Path(path).glob("*.json")) if stamp is not None else []
        except OSError:
            files = []
        made = make(files)
        self.folders[str(path)] = (stamp, self.now, made)
        return made

    def inputs(self, state, chat, topic, records):
        """What ``topic``'s items are built from (``topic_items``), as cheaply as it can be read: its job and question records as the
        stores hold them (no file read), a stat of each file of its own the build reads (history.jsonl, topic.json, gist.json), and what
        this look read already: its board group and whether its result is unseen, the projects it joined and when, the collaborations it
        is in, the relays of its turns, its 到点回来看 and 代发 drafts, the look's share (``look_inputs``). A hash; None while its items
        change by themselves (a task not ended: queued, running, waiting for an answer or for 开始; a question still open): such a topic
        is built every time. Read before anything the build reads, so a change in between is a change at the next look."""
        if any(record.get("status") not in ENDED for record in records):
            return None
        questions = [row for _, row in directory_store(state / "questions").query(chat_id=chat, topic_id=topic)]
        if any(row.get("status", "pending") in OPEN_QUESTIONS for row in questions):
            return None
        from covey.core import project_proposals, topic_gist

        files = [file_stamp(state / "topics" / topic / "history.jsonl"), file_stamp(topic_file(state, topic))]
        if project_proposals.enabled():
            files.append(file_stamp(topic_gist.path(state, topic)))
        involved = sorted((cid, web_sync.rev(record)) for cid, record in self.collab_now.items()
                          if topic in parties(record)) if topic in self.collab_topics else []
        joined = sorted((pid, projects.joined_at(record, topic)) for pid, record in self.projects.items() if projects.joined_at(record, topic))
        return web_sync.rev([records, questions, files, involved, joined, [self.room.relayed_at(record) for record in records],
                             self.board.get(topic, "") if self.board is not None else None, topic in self.unseen,
                             self.later.get(topic), self.drafts.get(topic), self.look_rev])

    def unchanged(self, topic, inputs, now, sent) -> bool:
        """Whether the pass leaves ``topic`` unbuilt: its ``inputs`` (not None) are those of its last build, that build is not due again
        (``limit``, or sooner: 产物 still going up, ``files_due``), and the page has every item it made (``sent``). One
        whose items never got there (left behind, refused, still waiting to go) or of which a quick look sent another reading since is
        built again, and so is every topic once ``sent`` starts empty (a new day, a new process)."""
        kept = self.shared.prints.get(topic)
        return (inputs is not None and kept is not None and kept[0] == inputs and now < kept[2]
                and all(sent.get(name) == rev for name, rev in kept[1]))

    def keep_inputs(self, topic, inputs, items, now) -> None:
        """The pass built ``topic`` from ``inputs`` into ``items``: kept for ``unchanged``, with when it is due again (``limit``, or the
        build's own ``due``). Nothing of a topic whose items change by themselves (``inputs`` None), and nothing from a collector left
        behind (one that comes back late read what is old)."""
        if self.retired:
            return
        with self.shared.lock:
            if inputs is None:
                self.shared.prints.pop(topic, None)
            else:
                self.shared.prints[topic] = (inputs, tuple(((each["type"], each["key"]), each["rev"]) for each in items),
                                             min(self.due, now + limit(topic)))

    # -- collaborations (COVEY_COLLAB=1) ---------------------------------------------------------------------------------------

    def read_collabs(self, state, chat) -> None:
        """This look's collaborations of ``chat`` (``collab_now``) from <state>/collab/, a file read again only when it changed (its
        inode too: a rewrite within one tick of the clock keeps its mtime and often its size); a topic in one whose record changed is
        built on the next look (``touch``: its marks and its 已协助 line). Without COVEY_COLLAB nothing is read and nothing is kept."""
        if not turn_source.enabled():
            self.collab_cache, self.collab_primed = {}, False
            self.collab_now, self.collab_helpers, self.collab_topics = {}, {}, set()
            self.job_seqs.clear()
            return
        try:
            paths = sorted((Path(state) / collab_records.FOLDER).glob("*.json"))
        except OSError:
            paths = []
        seen, mine, changed = {}, {}, []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp, kept = (stat.st_ino, stat.st_mtime_ns, stat.st_size), self.collab_cache.get(path.stem)
            if kept is not None and kept[0] == stamp:
                record = kept[1]
            else:
                record = collab_records.read(state, path.stem)
                if record and self.collab_primed:
                    changed.append(record)
            seen[path.stem] = (stamp, record)
            if record and record.get("chat_id") == chat:
                mine[str(record["id"])] = record
        self.collab_cache, self.collab_primed, self.collab_now = seen, True, mine
        self.collab_helpers, self.collab_topics = {}, set()
        for record in sorted(mine.values(), key=lambda each: float(each.get("created_at") or 0)):
            self.collab_topics |= parties(record)
            for helper in dict.fromkeys(str(ask.get("to")) for ask in record.get("asks") or ()):
                self.collab_helpers.setdefault(helper, []).append(record)
        for topic in [topic for topic in list(self.job_seqs) if topic not in self.collab_topics]:
            self.job_seqs.pop(topic, None)
        for record in changed:
            for topic in parties(record):
                self.touch(topic)

    def collab_items(self) -> list:
        """One ``collab`` item (docs/web.md 协作与三层记忆) for each collaboration of the chat, the oldest first; none without COVEY_COLLAB."""
        return [web_sync.item("collab", cid, self.collab_data(record)) for cid, record in self.collab_now.items()]

    def collab_data(self, record) -> dict:
        """A collaboration as the page reads it: ``collab_records.view`` with the owner's words and the job of its root, each ask's ``from``
        (the integrator when it has none), its answer with the evidence level in words and, where the helper's topic shows which of its
        turns the answer stands on, that turn's ``seq`` (``job_seqs``: the task the evidence names), and a late answer's words."""
        view = collab_records.view(record)
        integrator = str((view.get("integrator") or {}).get("key") or "")
        raw = {str(ask.get("id")): ask for ask in record.get("asks") or ()}
        asks = []
        for each in view["asks"]:
            ask, to = raw.get(str(each["id"])) or {}, str(each.get("to") or "")
            late = ask.get("late") if isinstance(ask.get("late"), dict) else {}
            asks.append({"id": str(each["id"]), "from": str(each.get("from") or integrator), "to": to, "number": 2 if each.get("number") == 2 else 1,
                         "state": str(each.get("state") or ""), "words": str(each.get("words") or ""), "reason": str(each.get("reason") or ""),
                         "text": str(each.get("text") or ""), "reply": self.collab_reply(ask, to),
                         "late": {"text": web_sync.clip(late["text"]), "at": int(late.get("at") or 0)} if late.get("text") else None})
        root = record.get("root") or {}
        job = str(root.get("job_key") or root.get("job_id") or "")
        return {"key": view["id"], "root": {"message_id": str(root.get("message_id") or ""), **({"job": job} if job else {}),
                                            "words": str(root.get("words") or "")},
                "integrator": integrator, "helpers": [str(row.get("key")) for row in view["helpers"] if row.get("key")],
                "state": str(view.get("state") or ""), "goal_rev": str(view.get("goal_rev") or ""),
                "created_at": int(view.get("created_at") or 0), "updated_at": int(view.get("updated_at") or 0), "asks": asks}

    def collab_reply(self, ask, helper) -> dict | None:
        """The answer of ``ask`` (a record's): {text, at, topic_key, seq?, evidence}; None until it has one."""
        reply = ask.get("reply") if isinstance(ask.get("reply"), dict) else {}
        if not reply.get("text"):
            return None
        grounds = reply.get("evidence") if isinstance(reply.get("evidence"), dict) and reply.get("evidence") else ask.get("evidence")
        grounds = grounds if isinstance(grounds, dict) else {}
        words = collab.EVIDENCE_WORDS.get(grounds.get("level"), "")
        job = str(grounds.get("job_id") or (ask.get("base") or {}).get("job_id") or reply.get("job_id") or "")
        seq = (self.job_seqs.get(helper) or {}).get(job)
        out = {"text": web_sync.clip(reply["text"]), "at": int(reply.get("at") or 0), "topic_key": helper,
               "evidence": [{"text": words, "topic_key": helper, **({"seq": seq} if seq else {})}] if words else []}
        if seq and words:
            out["seq"] = seq
        return out

    def collab_of(self, message_id) -> str:
        """The collaboration whose integrator's continuation turn has the message id ``message_id`` ('' for any other)."""
        found = REPLY_MESSAGE.fullmatch(str(message_id or ""))
        return found[1] if found and found[1] in self.collab_now else ""

    def mark_collab(self, data, message_id, *, prompt=False) -> None:
        """``data``, the integrator's continuation turn (its owner line, ``prompt``, or its answer), takes ``collab`` {key, role}; the
        owner line shows ``CONTINUED_WORDS`` for the long prompt the Bridge wrote."""
        found = self.collab_of(message_id)
        if not found:
            return
        data["collab"] = {"key": found, "role": "integrator"}
        if prompt:
            data["body"] = data["display"] = CONTINUED_WORDS

    def helper_rows(self, topic, turns, last) -> list:
        """[(collaboration id, turn)]: a line of role event in the topic of a helper of each collaboration, 已协助 T3（看这次协作） (正在协助
        while an ask waits or runs, 没能协助 when none was answered), that changes nothing else of the topic. Its seq is the latest turn
        started by then and never past ``last`` (the page drops what is: data contract v12), its place under the key
        ``<topic>:collab-<id>`` where no turn of the topic has one."""
        out = []
        for record in self.collab_helpers.get(topic) or ():
            asks = [ask for ask in record.get("asks") or () if ask.get("to") == topic and ask.get("state") != collab.NOT_TAKEN]
            if not asks or not turns or not last:
                continue
            label = (record.get("integrator") or {}).get("label") or "汇总方"
            live = any(ask.get("state") in (collab.CHECKING, collab.QUEUED, collab.TAKEN) for ask in asks)
            helped = any(ask.get("state") in (collab.REPLIED, collab.READ) for ask in asks)
            at = float(record.get("created_at") or 0) if live else max(float(ask.get("updated_at") or 0) for ask in asks)
            before = [turn["seq"] for turn in turns if (turn.get("started_at") or 0) <= at]
            seq = min(max(before, default=min(turn["seq"] for turn in turns)), last)
            words = f"正在协助 {label}" if live else f"已协助 {label}" if helped else f"没能协助 {label}"
            out.append((str(record["id"]), {"topic_key": topic, "seq": seq, "role": "event", "body": f"{words}（看这次协作）",
                                            "status": "running" if live else "done", "job_id": "", "started_at": int(at),
                                            "ended_at": 0 if live else int(at), "artifacts": [],
                                            "collab": {"key": str(record["id"]), "role": "helper"}}))
        return out

    @staticmethod
    def family_live(state, chat, topics) -> tuple:
        """(which of ``topics`` has a task running or a question waiting, the ``board_marks`` of those tasks and questions): one index
        query of the running tasks, one of the questions each."""
        wanted = set(topics)
        running = select_job_records(state, lambda record: record.get("topic_id") in wanted, chat_id=chat, status=frozenset(RUNNING))
        store = directory_store(state / "questions")
        asking = [row for topic in wanted for _, row in store.query(chat_id=chat, topic_id=topic, status="pending")]
        return {row.get("topic_id") for row in (*running, *asking)} & wanted, board_marks(running, asking)

    @staticmethod
    def recent(records, now) -> bool:
        return any((epoch_seconds(record.get("updated_at")) or now) >= now - TOPIC_DAYS * 86400 for record in records)

    # -- topics ---------------------------------------------------------------------------------------------------------

    def topic_items(self, state, chat, topic, records, now) -> list:
        butler = topic == butler_topic_id(chat)
        project = self.leads.get(topic)  # The record of the project this topic leads (COVEY_PROJECTS=1).
        if not records and not ((butler or project) and self.rows(state, topic)):
            return []  # A topic with no task has nothing the page shows (and a name the page made up is not looked up).
        asking = sorted(directory_store(state / "questions").query(chat_id=chat, topic_id=topic), key=lambda pair: pair[1].get("created_at") or 0)
        questions = [row for _, row in asking]
        latest = next((record for record in reversed(records) if not board_read.unrun(record)), None)
        # 等我 as every reader counts it (core/board_read.py waits): a question that asks, else a task waiting for 开始
        # on a launch card that can still start it (``form``, its record).
        form = board_read.launch_form(state, topic, chat) if latest and latest.get("status") == board_read.START else {}
        starts = bool(form)
        word = self.word(records, latest, questions, starts)
        self.states[topic] = word
        label, title = ("", BUTLER_TITLE) if butler else ("", LEAD_TITLE) if project else board_read.topic_label(state, topic)
        data = read_object(topic_file(state, topic))
        found = None if butler else projects.topic_project(state, topic)
        turns, count = self.turns(state, topic, records, latest, word, project, now, questions, form=form, butler=butler)
        self.put_ran_with(turns, records, data)
        number = int(label[1:]) if label[1:].isdigit() else 0
        updated = max((epoch_seconds(record.get("updated_at")) or 0 for record in records), default=0)
        ran = [record for record in records if not board_read.unrun(record)]
        # Waiting for 开始, it runs with what its launch card has chosen, not the binding it was given to wait with (the
        # default Harness and workspace of a reply that opened it, 2026-11-21 T622: Codex and the default workspace on the
        # page, Claude Code · Opus 5.5 · covey on the card). Else its binding's Harness, workspace and model (bound_model).
        bound = bound_model(data, latest)
        choice = launch_choice(form) or {"workspace": str(data.get("workspace_name") or ""), "harness": str(data.get("harness_id") or ""),
                                         "model": bound, "model_name": model_name(bound)}
        body = {
            "number": number, "title": title, "project_key": (project["id"] if project else found.id if found else ""), "state": word,
            "workspace": choice["workspace"], "harness": choice["harness"],
            "model": choice["model"], "model_name": choice["model_name"], "updated_at": updated, "unseen": topic in self.unseen and not butler and not project,
            "kind": "butler" if butler else "lead" if project else "topic", "round": len(ran)}
        if ran:
            body["started_at"] = board_read.started_at(ran[0])
        body.update(web_turns.topic_fields(data))  # v43: the repositories it uses and whether it started in the home.
        context = next((turn["context"] for turn in reversed(turns) if turn.get("context")), None)
        if context:
            body["context"] = context
        live = next((record for record in reversed(records) if record.get("status") in LIVE), None)
        if live is not None:  # Its running turn's progress (core/progress.py), from the record the task monitor keeps.
            body["progress"] = {**turn_progress.progress(live, now=now or time.time(), history=turn_progress.turn_seconds(
                records, exclude=live.get("job_id"))), **checkpoint_fields(live)}
        body["tone"] = self.tone(records, latest, questions, body["unseen"], body.get("progress"), starts)
        if self.board is not None and not (butler or project):
            body["board"] = self.board.get(topic, "")
        # What it runs with, for 换配置 on the page (data contract v21, app/web_switch.py; a project's lead's too, v36); none while
        # it waits for 开始, its launch card's to choose (a switch now would bind it to what the card is about to replace).
        if not (butler or starts):
            config = web_switch.topic_config(data, self.menu())
            if config:
                body["config"] = config
        gist = self.gist(state, topic) if not (butler or project) else None
        if gist:
            body["gist"] = gist
        if not (butler or project):  # Data contract v34: what comes back to it at a time it set, and what waits for the owner to send.
            body.update(later_and_drafts(state, topic))
        # The page drops this topic's turns past it (data contract v12): a turn pushed as running at count + 1 whose answer came in
        # at a lower seq (the answer's row and a stale "running" row of the same task) was never replaced,
        # so the 专项报告 counted that task's tokens twice and drew it as still running.
        body["last_seq"] = max((turn["seq"] for turn in turns), default=0)
        # When a member topic came into its project (data contract v13, core/projects.py add_member): the 专项报告 counts its turns
        # from then, as the 专项卡's token does.
        joined = projects.joined_at(self.projects.get(found.id) or {}, topic) if found is not None and project is None else None
        if joined:
            body["joined_at"] = joined
        out = [web_sync.item("topic", topic, body)]
        out += [web_sync.item("turn", f"{topic}:{turn['seq']}", turn) for turn in turns]
        if not butler:
            out += [web_sync.item("turn", f"{topic}:collab-{cid}", turn) for cid, turn in self.helper_rows(topic, turns, body["last_seq"])]
        for row in questions:
            made = web_pairing.question(row, topic, count, body["project_key"], number, records, questions)
            if made:
                out.append(web_sync.item("question", row.get("token"), made))
        return out

    def put_ran_with(self, turns, records, data) -> None:
        """Each answer's ``ran_with`` (data contract v27, web_turns.ran_with) from its task's record: the page writes it under the answer."""
        by_job = {str(record.get("job_id") or ""): record for record in records if record.get("job_id")}
        menu = None
        for turn in turns:
            job = by_job.get(str(turn.get("job_id") or "")) if turn.get("role") == "agent" else None
            if job is None:
                continue
            menu = self.menu() if menu is None else menu
            ran = web_turns.ran_with(job, menu, data)
            if ran:
                turn["ran_with"] = ran

    @staticmethod
    def asked(state, chat, now, looked):
        """要你处理 now as every card counts it (core/board_read.py Asks, what the 看板 and 手上的事 say: 等你 with the butler's and
        the leads' questions and 整理专项's proposals, 停住了 with the projects a guard paused, 可能卡住), from the look just taken
        (``looked``: app/pins.py PinnedTopics, its topics taken off left out); None when it cannot be read. Its ``counts`` are
        the page's numbers (data contract v34 ``asks``) and its 停住了 the topics ``board_groups`` says it of."""
        try:
            return owner_ledger.of(state, chat, now, looked, proposals=True).asks  # 等主人的账 (app/owner_ledger.py).
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def gist(state, topic):
        """The topic's 话题要点 (core/topic_gist.py) as the page shows it, COVEY_PROJECT_TIDY on: {essence, goal, involves, related,
        changed, updated_at}; None without one."""
        from covey.core import project_proposals, topic_gist

        if not project_proposals.enabled():
            return None
        record = topic_gist.read(state, topic)
        found = topic_gist.gist_of(record)
        return {**found, "updated_at": record.get("updated_at") or 0} if found else None

    @staticmethod
    def word(records, latest, questions, starts=False) -> str:
        """The topic's state word. 等你 for 等我 (core/board_read.py waits: a question that ``asks``, or ``starts``, its
        task waiting for 开始 on a launch card); running is what RUNNING says; a ``continued`` turn's outcome is its
        follow-up's, so the turn before it speaks; any other status (one this does not know) is not running: 空闲."""
        if starts or any(board_read.asks(row) for row in questions):
            return "等你"
        if any(record.get("status") in RUNNING for record in records):
            return "在做"
        if latest is not None and latest.get("status") == "continued":
            latest = next((record for record in reversed(records) if record.get("status") in STATE_WORDS), None)
        if latest is not None and board_read.set_aside(latest, questions):
            latest = {**latest, "status": "done"}  # Its question set aside (不处理): a turn that ended, no 等你.
        if latest is None:
            return "已停止" if never_started(records) else "空闲"
        return STATE_WORDS.get(latest.get("status"), "空闲")

    @staticmethod
    def tone(records, latest, questions, unseen, progress, starts=False) -> str:
        """The topic's state as a core/look.py STATES key (data contract v5 ``tone``): the page colours by it, and says
        its word. As ``word`` reads the records, and more: 可能卡住 for a running turn that may be stuck, 有结果 for an
        ended one not seen (``unseen``) and 做完 once seen, 停住了 for a failed one and for one stopped by anyone but the
        owner (core/look.py owner_stopped), 已停止 for his own."""
        if starts or any(board_read.asks(row) for row in questions):  # 等我, as ``word``.
            return "waiting"
        live = next((record for record in reversed(records) if record.get("status") in RUNNING), None)
        if live is not None:
            return look.record_tone(live, stuck=bool((progress or {}).get("maybe_stuck")))
        if latest is not None and latest.get("status") == "continued":
            latest = next((record for record in reversed(records) if record.get("status") in STATE_WORDS), None)
        if latest is not None and board_read.set_aside(latest, questions):
            latest = {**latest, "status": "done"}  # As ``word``.
        if latest is None and never_started(records):
            return "cancelled"  # Nothing ever ran and the last task was cancelled before it started: 已停止, not 空闲.
        if latest is None or latest.get("status") not in STATE_WORDS:
            return "idle"
        return look.record_tone(latest, seen=not unseen)

    def rows(self, state, topic) -> list:
        """The user and assistant lines of the topic's history, from a cache that is read again when the file changed."""
        path = state / "topics" / topic / "history.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return []
        stamp = (stat.st_mtime_ns, stat.st_size)
        kept = self.shared.recall(self.history, topic)
        if kept and kept[0] == stamp:
            return kept[1]
        rows = []
        for line in history_lines(path):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("role") in ("user", "assistant"):
                rows.append(row)
        self.shared.keep(self.history, topic, (stamp, rows), HISTORY_KEPT)
        return rows

    def turns(self, state, topic, records, latest, word, project=None, now=0.0, questions=(), form=None, butler=False) -> tuple:
        """([turn data with seq], number of turns in the history) for ``topic``: the newest TURNS_KEPT, and one more for the task that runs
        (its progress notes as the body), waits (for an answer, or for 开始 on the launch card ``form``: ``start_turn``) or ended without an
        answer. The newest EXTRA_TURNS answers of a task also carry its process, plan and figures (core web_turns.py). A project's lead
        (``project``, its record) has its own reading: ``lead_turns``."""
        rows = self.rows(state, topic)
        wakes = {}
        if project is not None:
            wakes = web_pairing.lead_starts(state, project, records)
        answered = web_pairing.answered(questions, records)
        entries = (self.lead_entries(state, topic, project, rows, records, wakes, answered, questions) if project is not None
                   else self.entries(rows, records, answered, questions, topic=topic))
        total = len(entries)
        self.count_unpaired(topic, entries)
        before = web_pairing.retried(records, entries)
        out = []
        seqs = {} if topic in self.collab_topics else None  # The seq of each answer turn by its task, for the evidence of a collaboration.
        if seqs is not None:
            self.job_seqs[topic] = seqs
        for seq, (at, data, job) in enumerate(entries, 1):
            if seqs is not None and job is not None and data.get("role") == "agent":
                seqs[str(job.get("job_id") or "")] = seq
            if seq <= total - TURNS_KEPT:
                continue
            turn = {"topic_key": topic, "seq": seq, **data}
            if job is not None and data.get("role") == "agent":
                turn["job_id"] = str(job.get("job_id") or "")
                web_pairing.put_images(turn, self.images.shown(job, topic, data.get("started_at"), self.now) if self.images else [])
                files = self.artifacts(state, job)
                if files:
                    turn["artifacts"] = files
            if job is not None:
                extra = self.ended_extras(state, job) if seq > total - EXTRA_TURNS else self.ended_figures(state, job)
                if "steps" in extra and steps_expired(turn, self.now):
                    extra = {key: value for key, value in extra.items() if key != "steps"}  # Not the cached dict: others read it.
                turn.update(extra)
                self.fold_usage(state, turn, before.get(id(job)) or ())
                # The memory notes are written after the turn has ended (app/memory_collect.py), maybe within the same
                # second as its end: read them from the record every time, not from the cache.
                notes = web_turns.notes_of(job)
                if notes:
                    turn["notes"] = notes
                else:
                    turn.pop("notes", None)
            if job is not None:
                # Why it did not succeed and what next, with what the page's buttons do (web_turns.failure_of). Not of a task
                # still running: its answer line is written once its result has gone out and before its record says so
                # (app/worker.py deliver), and a look in between read it as 没发出去.
                fail = web_turns.failure_of(job) if job.get("status") not in RUNNING else None
                if fail:
                    turn["failure"] = fail
                    if turn.get("role") == "agent":
                        # The line the history has is the stop's own words (or an answer that never went out), not an answer
                        # (data contract v10): an 官方拒绝 is no finished turn with the stop message as its conclusion.
                        turn["status"] = "failed"
                else:
                    turn.pop("failure", None)
            if job is not None:
                estimate_of(job, turn)
            if job is not None and wakes.get(job_key(job.get("message_id"), job.get("job_id"))):
                turn["started_topics"] = wakes[job_key(job.get("message_id"), job.get("job_id"))]
            if job is not None and data.get("role") == "agent":
                self.room_fields(state, turn, job, butler=butler, lead=project is not None)
            out.append(turn)
        job = next((record for record in reversed(records) if record.get("status") in RUNNING or (
            record.get("status") == "waiting_user" and not board_read.set_aside(record, questions))), None)
        if (job is not None and job.get("status") in RUNNING and rows and rows[-1].get("role") != "user"
                and str(rows[-1].get("job_id") or "") == str(job.get("job_id"))):
            job = None  # Its answer is the last line already, its record not yet ended: no second, running turn of it.
        if job is not None:
            out.append(self.live_turn(state, topic, total + 1, job, wakes, now, butler=butler))
            self.mark_collab(out[-1], job.get("message_id"))
        elif latest is not None and latest.get("status") in ("failed", "interrupted", "cancelled") and (not rows or rows[-1].get("role") == "user"):
            at = epoch_seconds(latest.get("updated_at")) or 0
            ended = {"topic_key": topic, "seq": total + 1, "role": "agent", "body": web_sync.clip(latest.get("status_message") or ""),
                     "status": "stopped" if latest.get("status") == "cancelled" else "failed", "job_id": str(latest.get("job_id") or ""),
                     "started_at": board_read.started_at(latest), "ended_at": at, "artifacts": []}
            fail = web_turns.failure_of(latest)
            if fail:  # A stop the owner did not make (官方拒绝, a guard) did not succeed either (data contract v10).
                ended["failure"], ended["status"] = fail, "failed"
            self.mark_collab(ended, latest.get("message_id"))
            out.append(ended)
        elif records and records[-1].get("status") == "cancelled" and records[-1].get("never_started") and rows \
                and rows[-1].get("role") == "user":
            # The owner's last words were never run: their task waited for 开始 (or in the queue) and was cancelled. Without a
            # line for it the page drew 排队中… under them for good (launch cards closed by a stop, say).
            unstarted = records[-1]
            why = str(unstarted.get("status_message") or "").strip()
            out.append({"topic_key": topic, "seq": total + 1, "role": "agent", "status": "stopped",
                        "body": web_sync.clip(UNSTARTED_WORDS + (f"（{why}）" if why else "")),
                        "job_id": str(unstarted.get("job_id") or ""), "started_at": 0,
                        "ended_at": epoch_seconds(unstarted.get("updated_at")) or 0, "artifacts": []})
        elif form and latest is not None and (not rows or rows[-1].get("role") == "user"):
            out.append(self.start_turn(state, topic, total + 1, latest, form))
        return out, total

    @staticmethod
    def start_turn(state, topic, seq, job, form) -> dict:
        """The turn of the owner's words whose task ``job`` waits for 开始 on the launch card ``form`` (core/board_read.py launch_form):
        等你 (``status`` waiting), what to do, what 开始 would run them with, and a link that opens the card in Feishu when one is
        known (app/topic_links.py, from its file only: the push reads no message from Feishu). 2026-11-21 T622: without it the page
        drew 排队中… under the words, said 等你 with nothing to act on, and (the topic's ``round`` counting the task) numbered them a
        round past it, under 再早的 1 轮网页上没有载入."""
        choice = launch_choice(form)
        runs = " · ".join(part for part in (choice["label"], f"思考预算 {choice['effort']}" if choice["effort"] else "",
                                            f"工作区 {choice['workspace']}" if choice["workspace"] else "") if part)
        link = start_link(state, topic, form.get("message_id"))
        lines = [START_WORDS + "。"]
        if runs:
            lines.append(f"点了开始会用：{runs}（卡上还能改）。")
        lines.append(f"[去飞书点开始]({link})" if link else "启动卡在飞书里这个话题的话题串中。")
        return {"topic_key": topic, "seq": seq, "role": "agent", "status": "waiting", "body": web_sync.clip("\n\n".join(lines)),
                "job_id": str(job.get("job_id") or ""), "started_at": board_read.started_at(job), "ended_at": 0, "artifacts": []}

    def entries(self, rows, records, answered=None, questions=(), topic="") -> list:
        """[(time, turn data, job record or None)] of a topic's history lines; the job only on an assistant line. ``answered``
        (``answered``): the turns that answered a question card get its ``answers``. An owner line says who said it and
        what the page shows of it (``said``)."""
        out, by_message = [], {}
        for row, job, asked in web_pairing.pair(rows, records, questions):
            at = epoch_seconds(row.get("time")) or 0
            owner = row.get("role") == "user"
            if owner:
                row = self.owner_row(row, topic, at, job)
            body = row.get("display_content") if owner and row.get("display_content") else row.get("content")
            data = {"role": "owner" if owner else "agent", "body": web_sync.clip(body), "status": "done", "job_id": "",
                    "started_at": at, "ended_at": at, "artifacts": []}
            if owner:
                self.said(data, row, asked)
                web_pairing.put_images(data, row.get("images"))
                self.merge_edit(data, str(row.get("message_id") or ""), by_message)
                self.mark_collab(data, row.get("message_id"), prompt=True)
            elif job is not None:
                self.mark_collab(data, job.get("message_id"))
            web_pairing.put_answers(data, answered, job, owner)
            out.append((at, data, None if owner else job))
        return out

    @staticmethod
    def merge_edit(data, message_id, by_message) -> None:
        """The owner's edit of a message sent into the running task as a line of its own (app/source_watch.py, message id
        ``edit-<message id>-<stamp>``) goes onto the line of the message it edits: that one shows the edited text, marked edited
        (its ``body`` stays the original), and the edit's own line is ``merged`` (the page leaves it out). Without the first line
        among these (older than the turns kept) the edit stays a line of its own. Data contract v19 (not both, the second as
        改过)."""
        if message_id.startswith(EDIT_PREFIX) and data.get("edited"):
            first = by_message.get(message_id[len(EDIT_PREFIX):].rsplit("-", 1)[0])
            if first is not None:
                first["display"], first["edited"] = data["display"], True
                data["merged"] = True
                return
        if message_id:
            by_message[message_id] = data

    def owner_row(self, row, topic, at, job=None) -> dict:
        """The owner's history line ``row`` as the page gets it: its words without local paths (web_images.scrub: the attachment
        block becomes its "附件：…" line) and, under ``images``, the refs of the images it came with (data contract v19). A line the
        butler or a lead handed on keeps only the owner's words and their "附件：…" line; its images are listed in the prompt of the
        task it started (``job``, paired by its message id), under "收到的附件"."""
        content = str(row.get("content") or "")
        clean = {**row, "content": web_images.scrub(content)}
        if row.get("display_content"):
            clean["display_content"] = web_images.scrub(row["display_content"])
        clean["images"] = []
        if self.images is not None:
            clean["images"] = self.images.sent(content, topic, at, self.now)
            own = job is not None and row.get("message_id") and str(job.get("message_id") or "") == str(row.get("message_id"))
            if not clean["images"] and own and "附件：" in content:
                clean["images"] = self.images.sent(web_images.listed(job.get("prompt_text")), topic, at, self.now)
        return clean

    def said(self, data, row, asked) -> None:
        """``data`` (an owner line's turn) takes web_said's fields (data contract v11): ``by`` always, ``display`` clipped as the
        body is; the answers of the question record ``asked`` when it has them (``put_answers`` may put them again)."""
        found = web_said.said(row, asked, self.origins)
        if found.get("display"):
            found["display"] = web_sync.clip(found["display"])
        if asked is not None and web_pairing.answer_rows(asked):
            found["answers"] = web_pairing.answer_rows(asked)
        data.update(found)

    # -- a project's lead ---------------------------------------------------------------------------------------------------

    def lead_entries(self, state, topic, project, rows, records, wakes, answered=None, questions=()) -> list:
        """The lead's conversation: the owner's words and the lead's answers, as ``entries``; a wake-up turn (the Bridge's, not the owner's
        words) is left out, with its answer unless the lead meant it for the owner (project_loop.reported), and what it started is a line of
        the page's own (role event); so is a member's end (the inbox's ``thread_end`` receipt). All in the order they happened."""
        out, shown, waking = [], set(), False
        running = {job_key(job.get("message_id"), job.get("job_id")) for job in records if job.get("status") in RUNNING or job.get("status") == "waiting_user"}
        reported = self.reported_by(state, topic, project)
        for row, job, asked in web_pairing.pair(rows, records, questions):
            at = epoch_seconds(row.get("time")) or 0
            if row.get("role") == "user":
                waking = projects.is_wake(row.get("message_id"))
                if not waking:
                    row = self.owner_row(row, topic, at, job)
                    data = {"role": "owner", "body": web_sync.clip(row.get("display_content") or row.get("content")), "status": "done",
                            "job_id": "", "started_at": at, "ended_at": at, "artifacts": []}
                    self.said(data, row, asked)
                    web_pairing.put_images(data, row.get("images"))
                    web_pairing.put_answers(data, answered, job, True)
                    self.mark_collab(data, row.get("message_id"), prompt=True)
                    out.append((at, data, None))
                continue
            key = job_key(job.get("message_id"), job.get("job_id")) if job is not None else ""
            if waking and (job is None or projects.woken(job) and not reported(key)):
                waking = False
                continue
            waking = False
            if key:
                shown.add(key)
            data = {"role": "agent", "body": web_sync.clip(row.get("content")), "status": "done", "job_id": "", "started_at": at,
                    "ended_at": at, "artifacts": []}
            web_pairing.put_answers(data, answered, job, False)
            if job is not None:
                self.mark_collab(data, job.get("message_id"))
            out.append((at, data, job))
        for key, started in wakes.items():  # What a wake-up turn nobody sees started.
            if key in shown or key in running or not started:
                continue
            at = next((board_read.started_at(job) for job in records if job_key(job.get("message_id"), job.get("job_id")) == key), 0)
            words = "、".join((f"T{each['number']} " if each["number"] else "") + each["title"] for each in started)
            out.append((at, {"role": "event", "body": "负责人派出了 " + words, "status": "done", "job_id": "", "started_at": at, "ended_at": at,
                             "artifacts": [], "started_topics": started}, None))
        for at, words in self.member_ends(state, project):
            out.append((at, {"role": "event", "body": words, "status": "done", "job_id": "", "started_at": at, "ended_at": at, "artifacts": []}, None))
        out.sort(key=lambda entry: entry[0])
        return out

    @staticmethod
    def reported_by(state, topic, project):
        """project_loop.reported for the turns of the lead topic ``topic``, its project's record read once a build: ``project`` (this look's,
        read_projects) when the topic says it leads that project, else read as reported reads it. Asked once a wake-up turn, reported read
        the 120 KB record again each time: 195 reads of it for one build of a busy lead, the build that answering its question waits for
        (2026-11-28 T959)."""
        found = projects.topic_project(state, str(topic or ""))
        if found is None:
            return lambda key: False
        record = project if str(project.get("id") or "") == found.id else projects.read_record(state, found.id)
        return lambda key: bool(project_loop.report_rows(state, found.id, record, key))

    def member_ends(self, state, project) -> list:
        """[(time, "T34 做完了")] of the project's inbox: only the receipts of a member's end, which the owner reads as they are."""
        folder = project_lead.inbox_dir(state, project["id"])
        try:
            names = sorted(path.name for path in folder.glob("*.json"))[-RECEIPTS_KEPT:]
        except OSError:
            return []
        seen = self.receipts.setdefault(project["id"], {})
        for name in names:
            if name not in seen:
                row = read_object(folder / name)
                seen[name] = row if row.get("kind") == "thread_end" else {}
        for name in set(seen) - set(names):
            seen.pop(name, None)
        found = []
        for name in names:
            row = seen.get(name) or {}
            words = web_turns.event_words(row.get("text"))
            if words:
                found.append((float(row.get("at") or 0), words))
        return found

    # -- a running turn -----------------------------------------------------------------------------------------------------

    def count_unpaired(self, topic, entries) -> None:
        """Logs (bridge.log, once each time the number changes) how many of the topic's answer turns have no task record: none should
        (pair), and one that has none reaches the page with no token, process or plan."""
        answers = [job for _, data, job in entries if data.get("role") == "agent"]
        missing = sum(1 for job in answers if job is None)
        if self.unpaired.get(topic, 0) != missing:
            self.unpaired[topic] = missing
            if self.log is not None:
                self.log(f"web sync topic {topic[:12]}: {missing} of {len(answers)} answer turns have no task record")

    def fold_usage(self, state, turn, before) -> None:
        """``turn``'s ``usage`` takes the tokens of the tasks ``before`` it (``retried``): what its answer cost in all."""
        for job in before:
            usage = self.ended_figures(state, job).get("usage")
            if not usage:
                continue
            mine = dict(turn.get("usage") or {"input": 0, "output": 0, "cache_read": 0, "total": 0})
            for name in ("input", "output", "cache_read", "total"):
                mine[name] = int(mine.get(name) or 0) + int(usage.get(name) or 0)
            turn["usage"] = mine

    def ended_figures(self, state, job) -> dict:
        """The figures alone (usage, seconds, speed, context) of an ended turn older than the newest EXTRA_TURNS, read once."""
        key = (job_key(job.get("message_id"), job.get("job_id")), str(job.get("updated_at")))
        found = self.shared.recall(self.figures, key)
        if found is None:
            found = web_turns.ended_figures(state, job)
            self.shared.keep(self.figures, key, found, FIGURES_KEPT)
        return found

    def artifacts(self, state, job) -> list:
        """The 产物 of an answer turn (web_turns.artifacts_of, data contract v32), read again only while they can still change: after
        FILES_GOING seconds while one goes up; once settled, FILES_SETTLED after, only when its upload's ledger changed (重新上传 on a
        card rewrites the ledger, not the job's record; the files and what may be published of them are the job's, which has ended:
        2026-11-23 T678, the paths of every 产物 resolved again every 300 s were 197 thousand lstats a pass of the live topics); a
        turn without any is read once."""
        key = (job_key(job.get("message_id"), job.get("job_id")), str(job.get("updated_at")))
        now = self.now or time.time()
        hit = self.shared.recall(self.files, key)
        if hit is not None:
            at, items, ledger = hit
            going = any(item.get("uploading") for item in items)
            if not items or now - at < (FILES_GOING if going else FILES_SETTLED):
                self.files_due(at, items)
                return items
            if not going and ledger is not None and ledger == ledger_rev(state, job):
                self.shared.keep(self.files, key, (now, items, ledger), FIGURES_KEPT)
                return items
        items = web_turns.artifacts_of(state, job, now)
        # Read after them: an upload that changes the ledger in between went through "uploading", which is read again whole.
        self.shared.keep(self.files, key, (now, items, ledger_rev(state, job) if items else None), FIGURES_KEPT)
        self.files_due(now, items)
        return items

    def files_due(self, at, items) -> None:
        """The topic being built is built again (``due``) when these 产物, read at ``at``, are read again while one of them goes up
        (FILES_GOING after): its link comes with the upload's ledger, not with the job's record. One that could not go up waits for its
        topic's ``limit`` (重新上传 is a click on a card; 154 of the live 2471 ledgers keep an error for good: a file too large, a
        configuration path)."""
        if any(item.get("uploading") for item in items):
            self.due = min(self.due, at + FILES_GOING)

    def ended_extras(self, state, job) -> dict:
        """The process, plan and figures of an ended turn, read once (they do not change once it has ended)."""
        key = (job_key(job.get("message_id"), job.get("job_id")), str(job.get("updated_at")))
        found = self.shared.recall(self.extra, key)
        if found is None:
            found = web_turns.extras(state, job, False)
            self.shared.keep(self.extra, key, found, EXTRA_KEPT)
        return found

    def room_fields(self, state, turn, job, *, butler=False, lead=False) -> None:
        """The private chat's fields of an answer turn (data contract v23, app/web_room.py): a butler turn's ``handed``, an ordinary topic
        turn's ``relayed_at``; neither on a lead's (its answers are at the top level already)."""
        if butler:
            handed = self.room.handed(state, job)
            if handed:
                turn["handed"] = handed
        elif not lead:
            at = self.room.relayed_at(job)
            if at:
                turn["relayed_at"] = int(at)

    def live_turn(self, state, topic, seq, job, wakes=None, now=0.0, butler=False) -> dict:
        notes, snapshot = [], None
        try:
            snapshot = load_trace(state, job) or {}
            steps = snapshot.get("steps") or {}
            for key in snapshot.get("step_order") or list(steps):
                step = steps.get(key) or {}
                if step.get("status") == "note" and step.get("title") == "进展说明" and step.get("detail"):
                    notes.append(str(step["detail"]))
        except (OSError, ValueError, KeyError, TypeError):
            notes = []
        turn = {"topic_key": topic, "seq": seq, "role": "agent", "body": web_sync.clip("\n\n".join(notes)),
                "status": "waiting" if job.get("status") == "waiting_user" else "running", "job_id": str(job.get("job_id") or ""),
                "started_at": board_read.started_at(job), "ended_at": 0, "artifacts": []}
        turn.update(web_turns.extras(state, job, job.get("status") in RUNNING, now or time.time(), snapshot))
        web_pairing.put_images(turn, self.images.shown(job, topic, turn["started_at"], now or time.time()) if self.images else [])
        estimate_of(job, turn)
        started = (wakes or {}).get(job_key(job.get("message_id"), job.get("job_id")))
        if started:
            turn["started_topics"] = started
        if butler:  # What it handed on so far (a running butler turn: its line under the owner's words, before it ends).
            self.room_fields(state, turn, job, butler=True)
        return turn

    # -- projects -------------------------------------------------------------------------------------------------------

    def project_items(self, state, chat, records=None, force=False) -> list:
        """An item per project. One is built again only when what it is built from changed (its record, its state word, the files of
        its folder: memory, meta, library), or with ``force`` (a pass started): a quick look every second reads no project's memory."""
        out = []
        for record in projects.records(state, chat) if records is None else records:
            pid = record["id"]
            members = [topic for topic in record.get("threads") or () if isinstance(topic, str)]
            lead = str(record.get("lead_topic_id") or "")
            word, why = self.project_state(record, [lead, *members])
            basis = (web_sync.rev(record), word, why, project_stamp(state, str(pid)))
            kept = self.shared.project_rows.get(pid)
            if kept is not None and kept[0] == basis and not force:
                out.append(kept[1])
                continue
            try:
                entries = project_memory.entries(state, record)
                # The read a member topic's turn takes (app/project_context.py memory_view).
                view = core_memory.project_view(entries, pid)
                carried = {item["ref"] for item in view["kept"]}
                def number(topic):
                    return self.topic_number(state, topic)
                memory = [self.memory_row(row, pid, carried, number) for row in entries if not row["deleted"]]
                library = [self.library_row(state, row, number) for row in project_library.entries(state, pid)
                           if not row.get("purged") and row.get("source") != "result"]  # Whole replies: the lead's (C7).
                version = view["version"]
            except Exception:  # noqa: BLE001 - the rest of the project is still worth pushing
                memory, library, version = [], [], ""
            item = web_sync.item("project", pid, {
                "title": str(record.get("name") or ""), "goal": project_lead.lead_goal(record), "status": word, "status_reason": why,
                "lead_topic_key": lead, "topic_keys": members, "memory": memory, "memory_version": version, "library": library,
                "started_at": web_pairing.moment(record.get("created_at")), "updated_at": web_pairing.moment(record.get("updated_at")),
                **progress_fields(record, project_lead.inbox_items(state, str(pid)) if turn_source.enabled() else ())})
            with self.shared.lock:
                self.shared.project_rows[pid] = (basis, item)
            out.append(item)
        return out

    def topic_number(self, state, topic) -> int:
        """The T number of ``topic`` (0 when it has none yet), read once it has one: a number is never changed, and a project's
        memory and library name the same topics in every row (18 thousand reads of topic.json in two passes, 2026-11-23 T673)."""
        if not topic:
            return 0
        found = self.shared.numbers.get(topic)
        if found:
            return found
        label = board_read.topic_label(state, topic)[0]
        found = int(label[1:]) if label[1:].isdigit() else 0
        if found:
            self.shared.numbers[topic] = found
        return found

    @staticmethod
    def memory_row(row, project_id="", carried=(), number=None) -> dict:
        """One entry of a project's memory as core/memory.py reads it for a turn (docs/web.md 三层记忆): its words; who said it
        and who wrote it down (``asserted_by``, ``recorded_by``), its ``level`` (core/turn_source.py provenance) and the label
        ``said_by`` (你说的 for what the owner said, 它总结的 for the rest), ``by`` owner or lead; its ``layer`` (rule, decision,
        history; the style line is a rule of kind style) and the ``part`` a turn carries it in (rule, decision, reference 待核对参考;
        none for history), ``applies``, ``valid``, ``superseded_by``; its ``ref`` (as a turn's memory_ids name it), whether a
        member topic's next turn carries it (``carried``), and where it came from (``origin``: {at} and, when the entry knows,
        ``message_id`` or ``question`` with its ``item`` and ``index``, ``topic`` and its ``topic_number``, ``job``, the owner's
        words ``quote``). ``number``: a topic's T number, for ``topic_number``."""
        facts = core_memory.describe(row)
        out = {"id": str(row.get("id") or ""), "text": row["text"], "kind": row["kind"], "source": row["source"], "at": row["at"]}
        out.update(said_by=core_memory.OWNER_WORDS[core_memory.OWNER if facts["asserted_by"] == "owner" else core_memory.LEAD],
                   by=core_memory.OWNER if facts["asserted_by"] == "owner" else core_memory.LEAD, ref=f"project:{project_id}:{out['id']}",
                   **{key: facts[key] for key in ("asserted_by", "recorded_by", "level", "layer", "part", "applies", "valid",
                                                  "superseded_by")})
        if row["kind"] == "style":
            out["part"] = None
        out["carried"] = out["ref"] in carried
        origin = {"at": row["at"]} if row["at"] else {}
        origin.update({key: value for key, value in facts["origin"].items() if value})
        if origin.get("topic") and number is not None and number(origin["topic"]):
            origin["topic_number"] = number(origin["topic"])
        if origin:
            out["origin"] = origin
        return out

    @staticmethod
    def library_row(state, row, number=None) -> dict:
        """One entry of a project's library as the page shows it; ``number``: a topic's T number (read from its topic.json without)."""
        out = {"name": row["name"], "link": row["ref"], "at": row["at"], "kind": web_turns.library_kind(row)}
        if row.get("size"):
            out["size"] = row["size"]
        if row.get("topic_id"):
            if number is not None:
                out["topic_number"] = number(row["topic_id"])
            else:
                label = board_read.topic_label(state, row["topic_id"])[0]
                out["topic_number"] = int(label[1:]) if label[1:].isdigit() else 0
        return out

    def project_state(self, record, topics) -> tuple:
        state = record.get("state")
        if state == projects.DONE:
            return "做完", "再说话会重开"
        if state == projects.CANCELLED:
            return "已取消", ""
        if state == projects.PAUSED:
            if record.get("state_reason") == projects.IDLE:
                return "收起", "没有新的事"
            return "停住", projects.REASON_WORDS.get(record.get("state_reason"), str(record.get("state_reason") or ""))
        if any(self.states.get(topic) == "等你" for topic in topics):
            return "等你", "有问题等你回答"
        return "在做", ""


def file_stamp(path):
    """(inode, mtime, size) of the file or folder at ``path``, None when there is none: every writer of a record replaces it whole (the
    inode moves), history.jsonl grows."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def limit(topic) -> float:
    """How long after its last build the pass builds ``topic`` again whatever its inputs say: REBUILD_SECONDS less up to half of it, by
    the topic's id, so the topics of a big chat built in one pass do not all come due in the same pass again."""
    share = int(hashlib.sha1(str(topic).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return REBUILD_SECONDS * (1 - share / 2)


def ledger_rev(state, job):
    """A hash of the upload ledger of ``job``'s 产物 (core/checkpoint.py upload_ledger: one file), None when it cannot be read."""
    try:
        return web_sync.rev(upload_ledger(state, job["key"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def stamps_by_name(files) -> dict:
    """{file stem: its stamp}: <state>/revisits/<topic id>.json by topic (core/revisits.py path)."""
    return {file.stem: file_stamp(file) for file in files}


def drafts_by_topic(files) -> dict:
    """{topic id: a hash of its drafts' records} of <state>/owner-send/ (core/owner_send.py), every status: what ``later_and_drafts`` shows of
    them changes only with one of these."""
    found = {}
    for file in files:
        record = read_object(file)
        found.setdefault(str(record.get("topic_id") or ""), []).append([file.name, record])
    return {topic: web_sync.rev(rows) for topic, rows in found.items()}


def parties(record) -> set:
    """The topics of a collaboration record: its integrator's and those it asked (Collector.read_collabs)."""
    return {str((record.get("integrator") or {}).get("key") or ""), *(str(ask.get("to")) for ask in record.get("asks") or ())} - {""}


def project_stamp(state, pid) -> tuple:
    """What a project's memory, its meta and its library, and with 协作 on its inbox (the gaps are read from it), are read from, as stats
    (each written whole and renamed over, so a change is a new inode; the inbox is a folder, which a receipt added or taken changes): a
    quick look builds the project again only when one changed (Collector.project_items)."""
    out = []
    for path in (project_memory.memory_path(state, pid), project_memory.meta_path(state, pid), project_library.index_path(state, pid),
                 *((project_lead.inbox_dir(state, pid),) if turn_source.enabled() else ())):
        try:
            stat = path.stat()
        except OSError:
            out.append(())
            continue
        out.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
    return tuple(out)


def progress_fields(record, receipts=()) -> dict:
    """What a project's 简报 and 进度 tabs are drawn from (docs/web.md 专项条目): its 目标检查表 as ``checklist`` (core/project_checklist.py
    items; ``topic`` and ``topics`` are topic ids, the page's topic keys; ``title``, ``waiting_on``, ``done_at`` and ``topics`` only on an
    item that has them), its ``background`` (the lead's), its ``actions`` (core/project_actions.py, the dropped too), the 协作 gaps not
    dealt with as ``gaps`` ({id, thread, text, at} of ``receipts``, the inbox's), the newest NOTES_SHOWN of its milestone and finish notes
    as ``milestones`` and of its decision log as ``decisions``, each {id, at, kind, text}. A field the record has nothing for is left out, so
    a page from before reads the item as it did; the item is built again when the record or the inbox changes (Collector.project_items)."""
    out = {}
    checklist = [{**{key: item[key] for key in CHECKLIST_SHOWN}, **optional_fields(item)} for item in project_checklist.items(record)]
    if checklist:
        out["checklist"] = checklist
    background = project_lead.lead_background(record)
    if background:
        out["background"] = background
    actions = [{key: action[key] for key in ACTION_SHOWN} for action in project_actions.items(record)]
    if actions:
        out["actions"] = actions
    gaps = [{"id": gap["id"], "thread": gap["thread"], "text": gap["text"][:GAP_TEXT], "at": gap["at"]}
            for gap in project_checklist.gaps(record, receipts)]
    if gaps:
        out["gaps"] = gaps
    for field, source, kinds in (("milestones", "notes", ("milestone", "finish")), ("decisions", "decisions", None)):
        rows = [note_row(row) for row in record.get(source) or ()
                if isinstance(row, dict) and str(row.get("text") or "").strip() and (kinds is None or row.get("kind") in kinds)]
        if rows:
            out[field] = rows[-NOTES_SHOWN:]
    return out


def optional_fields(item) -> dict:
    """What an item of the checklist says besides CHECKLIST_SHOWN, only where it says it: ``title``, ``waiting_on`` (written: ``owner``,
    or '' for not the owner's), ``done_at`` (not 0), ``topics`` (the other topics on it)."""
    return {key: item[key] for key in ("title", "waiting_on", "done_at", "topics") if item[key] or key == "waiting_on" and item[key] is not None}


def checkpoint_fields(record) -> dict:
    """What a running topic's checkpoint (the file ``record`` names) says that its ``progress`` carries besides core/progress.py's: its
    ``next_step`` (NEXT_STEP characters; not the seed's, not "无"), and ``waiting``, the ``question`` it asks the owner while its
    ``requires_user`` is true (WAITING_TEXT). Each only when there is one (docs/web.md 进度)."""
    path = str(record.get("checkpoint_path") or "")
    found = read_checkpoint(Path(path)) if path else {}
    step = " ".join(str(found.get("next_step") or "").split())
    asked = " ".join(str(found.get("question") or "").split())
    out = {}
    if step and step != turn_words.SEED_NEXT and not re.fullmatch(r"(无|没有|暂无)[。.]?", step):
        out["next_step"] = step[:NEXT_STEP]
    if found.get("requires_user") is True and asked:
        out["waiting"] = asked[:WAITING_TEXT]
    return out


def note_row(row) -> dict:
    """A note of a project record (app/project_lead.py entry) as the page reads it: {id, at, kind, text}."""
    at = row.get("at")
    return {"id": str(row.get("id") or ""), "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else 0,
            "kind": str(row.get("kind") or "note"), "text": str(row["text"])}


def launch_choice(form) -> dict:
    """What the launch form ``form`` (core/board_read.py launch_form) would start its topic with now, from the selects it recorded
    (app/launch.py form_draft): {harness, model, model_name, label, effort, workspace}; {} without a form."""
    if not form:
        return {}
    key = str(form.get("selected_model") or "")
    entry = (form.get("models") or {}).get(key)
    entry = entry if isinstance(entry, dict) else {}
    harness, _, model = key.partition(":")
    harness, model = str(entry.get("harness") or harness or form.get("harness_id") or ""), str(entry.get("model") or model)
    defaults = form.get("form_defaults") if isinstance(form.get("form_defaults"), dict) else {}
    return {"harness": harness, "model": model, "model_name": str(entry.get("display_name") or "") or model_name(model),
            "label": str(entry.get("label") or ""), "effort": str(form.get("selected_effort") or ""),
            "workspace": str(defaults.get("path") or defaults.get("workspace") or "")}


def bound_model(topic, latest) -> str:
    """The model ``topic`` (its topic.json) runs its next turn on, named on the page with the Harness and workspace it is
    bound to (the chat box's 工作区 · Harness · 模型 line, the topic page's head): its binding's; for a topic.json that names
    none (older topics), the one its ``latest`` task ran on, unless a 换配置 bound it anew since (app/project_facts.py
    rebound). 2026-11-24 T724: a topic opened on Claude Code · opus, its turn stopped, switched to Antigravity · Gemini 4
    Neon, and the page said Antigravity · Opus until its next turn ran: the binding's Harness, the stopped task's model."""
    model = str(topic.get("model") or "")
    if model or latest is None or rebound(topic, latest):
        return model
    return str(latest.get("model") or "")


def start_link(state, topic, card) -> str:
    """The applink that opens ``topic``'s launch card ``card`` in Feishu (app/topic_links.py link_for), from topic-links.json only
    (手上的事 keeps it there, app/overview.py link): '' when it is not there, as the push reads no message from Feishu."""
    def unread(message_id, profile):
        raise LookupError(message_id)
    try:
        return topic_links.link_for(state, topic, card=str(card or ""), fetch=unread)
    except Exception:  # noqa: BLE001 - no link, the words say where the card is
        return ""


def never_started(records) -> bool:
    """Whether a topic's last task was cancelled before it started (a launch card closed by a stop, one taken out of the queue)."""
    return bool(records) and records[-1].get("status") == "cancelled" and bool(records[-1].get("never_started"))


def later_and_drafts(state, topic) -> dict:
    """A topic's ``revisit`` {at, what, set_at, running}: its 定时回看 (core/revisits.py, COVEY_REVISIT=1, state pending or, ``running``, fired: Covey's turn is out), and its
    ``owner_send``: the 代发 drafts that wait for the owner on their card in Feishu (core/owner_send.py drafted, or unsent and to be
    sent again; COVEY_OWNER_SEND, with a 代答 sender), each {id, to, state, preview, created_at}, newest first, at most
    DRAFTS_SHOWN. The page shows them and sends nobody anything: a draft goes out only from its card. {} for neither."""
    out = {}
    if revisits.enabled():
        found = revisits.read(state, topic)
        # An ordinary topic's only (a record left from before the topic joined a project is not shown; the Bridge's thread removes it).
        if (found.get("state") in (revisits.PENDING, revisits.FIRED) and isinstance(found.get("due_at"), (int, float))
                and revisit_rules.ordinary(state, topic, str(found.get("chat_id") or ""))):
            out["revisit"] = {"at": round(found["due_at"]), "what": " ".join(str(found.get("look") or "").split())[:200],
                              "set_at": found.get("set_at"), "running": found.get("state") == revisits.FIRED}
    if owner_send.enabled():
        drafts = [row for row in owner_send_records.topic_records(state, topic) if row.get("status") in DRAFT_WAITS]
        if drafts:
            out["owner_send"] = [{
                "id": str(row.get("id") or ""), "to": str(row.get("to_label") or row.get("to") or "")[:60],
                "state": "waiting" if row.get("status") == "drafted" else "failed",
                "preview": " ".join(str(row.get("text") or "").split())[:120],
                "created_at": round(row["created_at"]) if isinstance(row.get("created_at"), (int, float)) else None,
            } for row in drafts[:DRAFTS_SHOWN]]
    return out


DRAFT_WAITS = ("drafted", "unsent")  # A draft that waits for the owner's 确认 on its card (app/owner_send.py decide).
DRAFTS_SHOWN = 5


def board_groups(rows, dismissed, asks=None) -> dict:
    """topic id -> its group on the board (app/pins.py PinnedTopics.look ``rows``, without the topics the owner took off,
    ``dismissed``), as a core/look.py STATES key: waiting, running, stuck, or a result's (stopped, result, done, cancelled).
    A project's thread is its own topic here (the board's row is its project's cell, the record says which thread). The page
    lists 要你处理 by this (data contract v6 ``board``), so a stopped topic the board no longer lists (seen KEEP_SEEN ago,
    taken off with 不管了) is not 停住了 there either: a topic stopped and seen at once must not come back on the page as
    停住了 while the board has let it go.

    停住了 is exactly what ``asks`` (core/board_read.py Asks, whose ``counts`` are the page's numbers) counts: each of its topics,
    though the board's row is its project's cell showing another thread or it ended before the board's rules; and a stopped
    result it leaves out (a project's member its lead is woken for) is on the page as one the board let go, not 停住了.
    2026-11-24 T734: the page said 9 停住了 and drew none, nine members of one project behind their project's cell."""
    stopped = asks.stopped if asks is not None else None
    out = {}
    for row in rows:
        if row["topic"] in dismissed:
            continue
        record = row.get("record") or {}
        topic = str(record.get("topic_id") or row["topic"])
        if row["group"] == "waiting":
            out[topic] = "waiting"
        elif row["group"] == "running":
            out[topic] = "running"
        else:
            tone = look.record_tone(record, seen=row.get("seen") is not None)
            if tone != "stopped" or stopped is None or topic in stopped:
                out[topic] = tone
    for topic in stopped or ():
        out[str(topic)] = "stopped"
    return out

