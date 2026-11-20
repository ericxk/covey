"""网页工作台同步 (docs/butler.md 网页工作台, COVEY_WEB=1 and COVEY_WEB_CONFIG): pushes the state of the owner's projects, topics, turns and
questions to the web page (the 妙搭 app) and fetches what the page sent, which app/web_ops.py does. Off by default: without the switch
there is no thread and no request.

- ``WebSync`` (app/unified.py PARTS ``web_sync``, a feature object: ``threads()``, none without the switch): two supervised threads. ``web-push``
  builds the items (``Collector``), keeps those whose ``rev`` (core/web_sync.py) differs from what was sent, and posts them to
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
import queue
import threading
import time
from pathlib import Path

from covey.app import pins, project_lead, project_loop, web_images, web_said, web_turns
from covey.app.answers import SKIPPED
from covey.app.web_ops import Ops
from covey.core import board_read, failure, http_upstream, look, project_library, project_memory, projects, triage, web_sync
from covey.core import progress as turn_progress
from covey.core.history_storage import history_lines
from covey.core.butler import butler_topic_id
from covey.core.jobs import job_key, select_job_records, topic_job_records
from covey.core.model_names import model_name
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
from covey.core.topics import topic_file
from covey.core.trace_store import load_trace

FULL_SECONDS = web_sync.FULL_SECONDS
STEP_BYTES = web_sync.BATCH_BYTES  # What a step sends of the pass's items (the backlog) after the urgent ones: one batch.
SENT_FILE = "sent.json"  # <state>/web/: what the page took (key -> rev), so a restart sends only what changed since.
SENT_SAVE_SECONDS = 30.0  # It is written at most this often, and when a pass ends.
SENT_KEEP_SECONDS = 86400.0  # Once a day ``sent`` starts empty again, so everything goes once (a page whose database was emptied fills again).
PASS_SLICE = 25  # Topics of a full pass looked at per step, after the hot ones: a pass is many steps, never one long look.
TOPIC_DAYS = 30  # A full look takes the topics with a job written within this many days.
TURNS_KEPT = 100  # The newest turns of a topic that are pushed.
MAX_BACKOFF = 30
TIMEOUT = 20
STUCK_SECONDS = 60  # A step with no progress in this long is left behind (an NFS read can hold its thread for good); the next runs in a new thread.
MAX_LEFT = 3  # Left-behind steps still stuck: with this many no new thread is started until one of them ends, or LEFT_WAIT has passed.
LEFT_WAIT = 300  # Seconds waited at MAX_LEFT; then the next step runs in a new thread anyway (an open stuck for good must not stop the push).
STATE_WORDS = {"done": "做完", "waiting_user": "等你", "cancelled": "已停止", "failed": "失败", "interrupted": "失败"}
BUTLER_TITLE = "管家"
LEAD_TITLE = "负责人"
EXTRA_TURNS = 20  # The newest turns of a topic that get their process and plan; every kept turn gets its figures (each read from its trace file once).
FIGURES_KEPT = 8192  # Ended turns whose figures are remembered (a few numbers each): all kept turns of all topics a full look builds.
PAIR_SLACK = 300  # Seconds after a task's record was last written that an answer line with no other task may still be its own (pair).
RECEIPTS_KEPT = 200  # The newest receipts of a project's inbox that are looked at.
QUESTION_STATES = {"pending": "open", "cancelled": "cancelled", "superseded": "cancelled"}
NOT_ANSWERED = "没答"  # turn.data answers' choice of a question the owner chose not to answer (data contract v4).
CANCEL_REASONS = ("stale", "switched", "rewound", "lead_stop", "web_stop")  # A cancelled question's cancel_reason, pushed as its closed_by.
UNSTARTED_WORDS = "没开工，已取消"  # The line under the owner's words whose task was cancelled before it started (its record's reason after it).
IMAGES_WAIT = 300  # Seconds images are held back after the page did not take one (push_images).
LIVE = ("running", "cancel_requested")  # The statuses whose turn has a topic.data progress.


def moment(value) -> float:
    """``value`` (epoch seconds or an ISO time) as epoch seconds; 0 when it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return epoch_seconds(value) or 0.0


def estimate_of(job, turn) -> None:
    """``turn`` takes ``estimate_seconds``, the first estimate of its whole 用时 the task monitor kept on the job record
    (core/longtask.py), or loses it: a turn whose task never had one has none."""
    value = job.get("estimate_seconds")
    if type(value) is int and value > 0:
        turn["estimate_seconds"] = value
    else:
        turn.pop("estimate_seconds", None)


class WebError(Exception):
    """A request the page did not take: its HTTP status, or the connection."""

    def __init__(self, text, status=0) -> None:
        super().__init__(text)
        self.status = status


class Stuck(Exception):
    """A step that did not end within its time."""


class Worker:
    """A thread that runs the steps it is handed, one at a time. A step stuck in a system call is left to it: the caller hands the
    next steps to a new Worker, and this thread ends when the stuck step does."""

    def __init__(self, name) -> None:
        self.steps, self.ends = queue.Queue(), queue.Queue()
        self.thread = threading.Thread(target=self.run, name=name, daemon=True)
        self.thread.start()

    def run(self) -> None:
        while True:
            step = self.steps.get()
            if step is None:
                return
            try:
                self.ends.put((step(), None))
            except Exception as exc:  # noqa: BLE001 - raised in the caller's thread
                self.ends.put((None, exc))

    def call(self, step, seconds, moved=None):
        """``step``'s value (its exception is raised here). Stuck when it has not ended ``seconds`` after it began, or, with ``moved`` (the
        monotonic time of the step's last progress), ``seconds`` after its last progress: a long first push that keeps sending is not stuck."""
        self.steps.put(step)
        last = time.monotonic()
        while True:
            try:
                value, exc = self.ends.get(timeout=max(0.0, last + seconds - time.monotonic()))
                break
            except queue.Empty:
                newer = moved() if moved is not None else 0.0
                if newer <= last:
                    self.steps.put(None)
                    raise Stuck from None
                last = newer
        if exc is not None:
            raise exc
        return value


class Client:
    """The page's /openapi, with the key of the config's key_file (re-read every 10 seconds, so a replaced key is picked up)."""

    def __init__(self) -> None:
        self.opener = http_upstream.opener()
        self.loaded, self.config = 0.0, None

    def settings(self) -> web_sync.Config:
        now = time.monotonic()
        if self.config is None or now - self.loaded > 10:
            self.config, self.loaded = web_sync.load_config(), now
        return self.config

    def request(self, method, path, body=None) -> dict:
        try:
            config = self.settings()
        except ValueError as exc:
            raise WebError(f"config: {exc}") from None
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        headers = {"Authorization": "Bearer " + config.key, **({"Content-Type": "application/json"} if data is not None else {})}
        try:
            status, response = http_upstream.send(self.opener, method, config.base_url + path, data, headers, TIMEOUT)
            raw = response.read()
        except OSError as exc:
            raise WebError(type(exc).__name__) from None
        if status >= 300:
            self.loaded = 0.0  # The config may have been fixed meanwhile.
            raise WebError(f"http {status}", status)
        try:
            value = json.loads(raw or b"{}")
        except ValueError:
            raise WebError("answer is not JSON", status) from None
        return value if isinstance(value, dict) else {}


class Collector:
    """The items of the owner's chat from the records (see the module's reading rules). One thread uses it."""

    def __init__(self, env, stuck=None, log=None, images=None) -> None:
        self.env = env
        self.images = images  # web_images.WebImages (data contract v19); None: turns carry no images (their words lose local paths all the same).
        self.now = 0.0  # The time of the look under way.
        self.backfill = images is not None and not images.backfilled  # The first pass looks at every topic once (their images, T582).
        self.backfilling, self.backfill_extra = False, set()  # The pass under way is that one; the topics only it looks at.
        self.stuck = {} if stuck is None else stuck  # topic id -> the left-behind thread stuck reading it (WebSync.start_over).
        self.left = set()  # The topics the last look left out because their read is stuck.
        self.at, self.thread = "", None  # The topic being built now, and the thread that looks.
        self.hot, self.dirty = set(), set()
        self.unseen = set()
        self.board = None  # topic id -> its group on the board (``board_groups``); None without the board (COVEY_PINNED_TOPICS unset).
        self.states = {}  # topic id -> its state word, from its last build (a project's 等你 reads it).
        self.pushed_topics = set()
        # The full pass under way: the topics still to look at, the ones looked at so far, and whether the last step ended it.
        self.pass_left, self.pass_seen, self.pass_done = [], set(), False
        self.urgent = 0  # How many of the last look's items, at its head, are the hot topics' (WebSync.push_once sends them first).
        self.history = {}  # topic id -> ((mtime_ns, size), rows)
        self.leads = {}  # lead topic id -> its project's record (COVEY_PROJECTS=1 only), from this look.
        self.projects = {}  # project id -> its record, from this look (a member topic's ``joined_at``).
        self.extra = {}  # (job key, updated_at) -> the optional fields of an ended turn (web_turns.extras).
        self.figures = {}  # (job key, updated_at) -> the figures of an ended turn (web_turns.ended_figures).
        self.unpaired = {}  # topic id -> its answer turns with no task record, as last logged (count_unpaired).
        self.log = log  # The bridge.log writer (WebSync's runtime.log); None: nothing logged.
        self.receipts = {}  # project id -> {receipt file name: its row} (thread_end only).
        self.origins = None  # web_said.Origins of the owner's chat: who wrote each hand-on, read once.
        self.moved = 0.0  # Monotonic time of the last progress of a look and its push: a topic built, a batch sent (WebSync.loop's stuck check).
        self.lock = threading.Lock()

    def touch(self, topic) -> None:
        """The page acted on ``topic``: it is built on the next look whatever the indexes say."""
        with self.lock:
            self.dirty.add(str(topic))

    def collect(self, chat, now, full) -> tuple:
        """(items, deleted): ``items`` the topics looked at, their turns and questions, and all projects; ``deleted`` (full looks only)
        the topics and projects pushed before that are not any more (a list of {type, key}; ``seen`` is updated by the caller)."""
        state = Path(self.env.state_dir)
        self.thread = threading.current_thread()
        self.now = now
        if self.origins is None or self.origins.chat != str(chat):
            self.origins = web_said.Origins(state, chat)
        out = {topic for topic, thread in list(self.stuck.items()) if thread.is_alive()}
        back, self.left = self.left - out, out  # A stuck read that came back: its topic is looked at again now.
        running = board_read.running_jobs(state, chat)
        asking = board_read.pending_questions(state, chat)
        butler = butler_topic_id(chat)
        records = projects.records(state, chat)
        self.leads = {str(record.get("lead_topic_id")): record for record in records if record.get("lead_topic_id")} if projects.enabled() else {}
        self.projects = {str(record.get("id")): record for record in records}
        family = [butler, *self.leads]  # The conversations that are not on the board: the butler's and the projects' leads.
        hot = {row.get("topic_id") for row in (*running, *asking) if row.get("topic_id")}
        hot |= self.family_live(state, chat, family)  # A turn of one runs or asks (the board does not list them).
        with self.lock:
            touched, self.dirty = self.dirty | back, set()
        start = full and not self.pass_left  # A new pass only once the last one has ended (``full``: one is due).
        if start or hot != self.hot or touched:
            read = board_read.read(state, chat, now, keep=pins.kept(state, chat) if pins.laid() else ())
            looked = pins.PinnedTopics(None, None)
            looked.look(state, chat, now, read, board=pins.laid())
            self.unseen = set(looked.unseen)
            self.board = board_groups(looked.rows, looked.dismissed) if pins.laid() else None
        quick = [topic for topic in hot | self.hot | touched if topic not in family]
        if start:
            backfill, self.backfill = self.backfill, False  # Every topic once, not only the recent ones (images of older turns, T582).
            self.backfilling = backfill
            jobs = board_read.topic_jobs(state, chat)
            self.pass_left = [topic for topic, records_ in jobs.items() if topic in hot or topic in touched or self.recent(records_, now)]
            # The topics only that pass looks at: once it ends they are forgotten as pushed, so the next pass does not delete them.
            self.backfill_extra = {("topic", topic) for topic in jobs if topic not in self.pass_left} if backfill else set()
            self.pass_left += [key for _, key in sorted(self.backfill_extra)]
            self.pass_seen = set()
        passing = bool(self.pass_left) or start
        slice_ = []
        while self.pass_left and len(slice_) < PASS_SLICE:
            topic = self.pass_left.pop(0)
            if topic not in quick:
                slice_.append(topic)
        # The pass's slice first, then the topics that run, ask, just did or the page just acted on: their records are read now, at
        # the end, so what they pushed is never older than what a pass read (T554, 2026-11-19: answered at 23:53:06, a look of every
        # topic that had read it before pushed 等你 at 23:56:20, and nothing else went out meanwhile).
        by_topic = {topic: sorted(topic_job_records(state, topic, chat_id=chat), key=board_read.job_number)
                    for topic in [*slice_, *quick] if topic not in out}
        looked = [topic for topic in family if (start or topic in hot or topic in self.hot or topic in touched) and topic not in out]
        self.hot = hot
        items, later = [], []  # The hot topics' items (sent first, every step) and the pass's (sent as the backlog allows).
        urgent = set(quick)
        for topic, jobs in by_topic.items():
            self.at = topic
            (items if topic in urgent else later).extend(self.topic_items(state, chat, topic, jobs, now))
            self.moved = time.monotonic()
        for topic in looked:
            self.at = topic
            mine = self.topic_items(state, chat, topic, sorted(topic_job_records(state, topic, chat_id=chat), key=board_read.job_number), now)
            self.moved = time.monotonic()
            items += mine
            if mine:
                by_topic[topic] = []
        urgent = len(items)
        items += later
        self.at = ""
        deleted = []
        built = {("topic", topic) for topic in by_topic}
        if passing:
            self.pass_seen |= built
        self.pass_done = passing and not self.pass_left
        if self.pass_done:
            seen = self.pass_seen | {("topic", topic) for topic in out}  # A topic left out is still on the page: not deleted.
            deleted += [{"type": kind, "key": key} for kind, key in sorted(self.pushed_topics - seen)]
            self.pushed_topics = seen - self.backfill_extra
        if self.pass_done and self.backfilling:
            self.backfilling, self.backfill_extra = False, set()
            self.images.mark_backfilled(now)
        elif built:
            self.pushed_topics |= built
        projects_now = self.project_items(state, chat, records)  # Read every step, like the hot topics: they go with them.
        items[urgent:urgent] = projects_now
        self.urgent = urgent + len(projects_now)
        return items, deleted

    @staticmethod
    def family_live(state, chat, topics) -> set:
        """Which of ``topics`` has a task running or a question waiting: one index query of the running tasks, one of the questions each."""
        wanted = set(topics)
        found = {record.get("topic_id") for record in select_job_records(
            state, lambda record: record.get("topic_id") in wanted, chat_id=chat, status=frozenset(RUNNING))}
        store = directory_store(state / "questions")
        return {topic for topic in wanted if topic in found or store.query(chat_id=chat, topic_id=topic, status="pending")}

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
        # on a launch card that can still start it.
        starts = bool(latest and latest.get("status") == board_read.START and board_read.launch_card(state, topic, chat))
        word = self.word(records, latest, questions, starts)
        self.states[topic] = word
        label, title = ("", BUTLER_TITLE) if butler else ("", LEAD_TITLE) if project else board_read.topic_label(state, topic)
        data = read_object(topic_file(state, topic))
        found = None if butler else projects.topic_project(state, topic)
        turns, count = self.turns(state, topic, records, latest, word, project, now, questions)
        number = int(label[1:]) if label[1:].isdigit() else 0
        updated = max((epoch_seconds(record.get("updated_at")) or 0 for record in records), default=0)
        ran = [record for record in records if not board_read.unrun(record)]
        body = {
            "number": number, "title": title, "project_key": (project["id"] if project else found.id if found else ""), "state": word,
            "workspace": str(data.get("workspace_name") or ""), "harness": str(data.get("harness_id") or ""),
            "model": str((latest or {}).get("model") or ""), "model_name": model_name((latest or {}).get("model")), "updated_at": updated, "unseen": topic in self.unseen and not butler and not project,
            "kind": "butler" if butler else "lead" if project else "topic", "round": len(ran)}
        if ran:
            body["started_at"] = board_read.started_at(ran[0])
        context = next((turn["context"] for turn in reversed(turns) if turn.get("context")), None)
        if context:
            body["context"] = context
        live = next((record for record in reversed(records) if record.get("status") in LIVE), None)
        if live is not None:  # Its running turn's progress (core/progress.py), from the record the task monitor keeps.
            body["progress"] = turn_progress.progress(live, now=now or time.time(), history=turn_progress.turn_seconds(
                records, exclude=live.get("job_id")))
        body["tone"] = self.tone(records, latest, questions, body["unseen"], body.get("progress"), starts)
        if self.board is not None and not (butler or project):
            body["board"] = self.board.get(topic, "")
        gist = self.gist(state, topic) if not (butler or project) else None
        if gist:
            body["gist"] = gist
        # The page drops this topic's turns past it (data contract v12): a turn pushed as running at count + 1 whose answer came in
        # at a lower seq (T519, T559 on 2026-11-19: the answer's row and a stale "running" row of the same task) was never replaced,
        # so the 专项报告 counted that task's tokens twice and drew it as still running.
        body["last_seq"] = max((turn["seq"] for turn in turns), default=0)
        # When a member topic came into its project (data contract v13, core/projects.py add_member): the 专项报告 counts its turns
        # from then, as the 专项卡's token does (T552: T531's and T525's turns from before they came in made the report 62M more).
        joined = projects.joined_at(self.projects.get(found.id) or {}, topic) if found is not None and project is None else None
        if joined:
            body["joined_at"] = joined
        out = [web_sync.item("topic", topic, body)]
        out += [web_sync.item("turn", f"{topic}:{turn['seq']}", turn) for turn in turns]
        for row in questions:
            made = self.question(row, topic, count, body["project_key"], number, records, questions)
            if made:
                out.append(web_sync.item("question", row.get("token"), made))
        return out

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
        if latest is None and never_started(records):
            return "cancelled"  # Nothing ever ran and the last task was cancelled before it started (T561, T562): 已停止, not 空闲.
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
        kept = self.history.get(topic)
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
        if len(self.history) > 256:
            self.history.clear()
        self.history[topic] = (stamp, rows)
        return rows

    def turns(self, state, topic, records, latest, word, project=None, now=0.0, questions=()) -> tuple:
        """([turn data with seq], number of turns in the history) for ``topic``: the newest TURNS_KEPT, and one more for the task that runs
        (its progress notes as the body), waits or ended without an answer. The newest EXTRA_TURNS answers of a task also carry its process,
        plan and figures (core web_turns.py). A project's lead (``project``, its record) has its own reading: ``lead_turns``."""
        rows = self.rows(state, topic)
        wakes = {}
        if project is not None:
            wakes = self.lead_starts(state, topic, project, records)
        answered = self.answered(questions, records)
        entries = (self.lead_entries(state, topic, project, rows, records, wakes, answered, questions) if project is not None
                   else self.entries(rows, records, answered, questions, topic=topic))
        total = len(entries)
        self.count_unpaired(topic, entries)
        before = self.retried(records, entries)
        out = []
        for seq, (at, data, job) in enumerate(entries, 1):
            if seq <= total - TURNS_KEPT:
                continue
            turn = {"topic_key": topic, "seq": seq, **data}
            if job is not None and data.get("role") == "agent":
                turn["job_id"] = str(job.get("job_id") or "")
                self.put_images(turn, self.images.shown(job, topic, data.get("started_at"), self.now) if self.images else [])
            if job is not None:
                turn.update(self.ended_extras(state, job) if seq > total - EXTRA_TURNS else self.ended_figures(state, job))
                self.fold_usage(state, turn, before.get(id(job)) or ())
                # The memory notes are written after the turn has ended (app/memory_collect.py), maybe within the same
                # second as its end: read them from the record every time, not from the cache.
                notes = web_turns.notes_of(job)
                if notes:
                    turn["notes"] = notes
                else:
                    turn.pop("notes", None)
            if job is not None:
                fail = failure.failure_view(job)  # Why it did not succeed and what next (core/failure.py).
                if fail:
                    turn["failure"] = fail
                    if turn.get("role") == "agent":
                        # The line the history has is the stop's own words (or an answer that never went out), not an answer
                        # (data contract v10): T514's 官方拒绝 read as a finished turn with the stop message as its conclusion.
                        turn["status"] = "failed"
                else:
                    turn.pop("failure", None)
            if job is not None:
                estimate_of(job, turn)
            if job is not None and wakes.get(job_key(job.get("message_id"), job.get("job_id"))):
                turn["started_topics"] = wakes[job_key(job.get("message_id"), job.get("job_id"))]
            out.append(turn)
        job = next((record for record in reversed(records) if record.get("status") in RUNNING or record.get("status") == "waiting_user"), None)
        if job is not None:
            out.append(self.live_turn(state, topic, total + 1, job, wakes, now))
        elif latest is not None and latest.get("status") in ("failed", "interrupted", "cancelled") and (not rows or rows[-1].get("role") == "user"):
            at = epoch_seconds(latest.get("updated_at")) or 0
            ended = {"topic_key": topic, "seq": total + 1, "role": "agent", "body": web_sync.clip(latest.get("status_message") or ""),
                     "status": "stopped" if latest.get("status") == "cancelled" else "failed", "job_id": str(latest.get("job_id") or ""),
                     "started_at": board_read.started_at(latest), "ended_at": at, "artifacts": []}
            fail = failure.failure_view(latest)
            if fail:  # A stop the owner did not make (官方拒绝, a guard) did not succeed either (data contract v10).
                ended["failure"], ended["status"] = fail, "failed"
            out.append(ended)
        elif records and records[-1].get("status") == "cancelled" and records[-1].get("never_started") and rows \
                and rows[-1].get("role") == "user":
            # The owner's last words were never run: their task waited for 开始 (or in the queue) and was cancelled. Without a
            # line for it the page drew 排队中… under them for good (T561, T562 on 2026-11-19, launch cards closed by a stop).
            unstarted = records[-1]
            why = str(unstarted.get("status_message") or "").strip()
            out.append({"topic_key": topic, "seq": total + 1, "role": "agent", "status": "stopped",
                        "body": web_sync.clip(UNSTARTED_WORDS + (f"（{why}）" if why else "")),
                        "job_id": str(unstarted.get("job_id") or ""), "started_at": 0,
                        "ended_at": epoch_seconds(unstarted.get("updated_at")) or 0, "artifacts": []})
        return out, total

    @staticmethod
    def pair(rows, records, questions=()):
        """(row, the job record of the turn the row belongs to or None, the question record a user line answers or None) for each user
        and assistant line of the history. 2026-11-19 T550: 97 of the Covey project's 127 answer turns reached the page without their
        task, so with no token, process or plan; this pairs them in three steps:

        - An answer line written with its task (``job_id``, app/worker.py since T564) has it. A user line is paired by its message id;
          an answer's line has none and is paired by its question record (web_said.answer_record), with the task the answer started
          (its message ``answer-<token>``) or, for one handed to the task that asked, that task. The next answer line has the job of
          the user line before it.
        - A user line with no task of its own while the task of the line before has not answered yet (a supplement the lead or the
          owner sent into the running task, app/steering.py: its record is merged or there is none) leaves that task to the answer,
          not None; the line itself has no job (``put_answers`` must not put that task's answers on it).
          An answer line written before the task of the user line before it started, or long after it ended (``ran_at``), is
          not that task's: that one was queued behind the task that answers, or an automatic retry went on and answered.
        - An answer line still without a task (one in a row with no user line between: a retry after a rate limit, a stop; one
          after a line whose task is not among the records or had not started) takes, of the records no answer line took, the
          latest that had started by then, is not running, and was last written at most PAIR_SLACK seconds before it."""
        usable = [record for record in records if not board_read.unrun(record)]
        by_message = {str(record.get("message_id")): record for record in usable if record.get("message_id")}
        by_id = {str(record.get("job_id")): record for record in usable if record.get("job_id")}
        found, job, taken = [], None, set()
        for row in rows:
            if row.get("role") == "user":
                asked = web_said.answer_record(row, questions, taken) if not row.get("message_id") else None
                if asked is not None:
                    own = by_message.get("answer-" + str(asked.get("token"))) or (
                        None if asked.get("continuation_job_id") else by_id.get(str(asked.get("job_id") or "")))
                else:
                    own = by_message.get(str(row.get("message_id") or ""))
                if own is not None or job is None:
                    job = own
                found.append([row, own, asked])
            else:
                if job is not None and not Collector.ran_at(job, epoch_seconds(row.get("time")) or 0):
                    job = None  # Queued behind the task that answers, or ended before (an automatic retry went on and answered).
                found.append([row, by_id.get(str(row.get("job_id") or "")) or job, None])
                job = None
        used = {id(entry[1]) for entry in found if entry[1] is not None and entry[0].get("role") != "user"}
        for entry in found:
            if entry[0].get("role") == "user" or entry[1] is not None:
                continue
            at = epoch_seconds(entry[0].get("time")) or 0
            began = [(board_read.started_at(record), index) for index, record in enumerate(usable) if id(record) not in used
                     and record.get("status") not in RUNNING and Collector.ran_at(record, at)]
            if began:
                entry[1] = usable[max(began)[1]]
                used.add(id(entry[1]))
        for row, job, asked in found:
            yield row, job, asked

    @staticmethod
    def ran_at(record, at) -> bool:
        """Whether an answer line written at ``at`` can be the task ``record``'s: it had started by then (2 s for the clocks) and was
        last written at most PAIR_SLACK seconds before (its line is written as its result goes out, then its record)."""
        return bool(at) and board_read.started_at(record) <= at + 2 and at <= moment(record.get("updated_at")) + PAIR_SLACK

    def entries(self, rows, records, answered=None, questions=(), topic="") -> list:
        """[(time, turn data, job record or None)] of a topic's history lines; the job only on an assistant line. ``answered``
        (``answered``): the turns that answered a question card get its ``answers``. An owner line says who said it and
        what the page shows of it (``said``)."""
        out = []
        for row, job, asked in self.pair(rows, records, questions):
            at = epoch_seconds(row.get("time")) or 0
            owner = row.get("role") == "user"
            if owner:
                row = self.owner_row(row, topic, at)
            body = row.get("display_content") if owner and row.get("display_content") else row.get("content")
            data = {"role": "owner" if owner else "agent", "body": web_sync.clip(body), "status": "done", "job_id": "",
                    "started_at": at, "ended_at": at, "artifacts": []}
            if owner:
                self.said(data, row, asked)
                self.put_images(data, row.get("images"))
            self.put_answers(data, answered, job, owner)
            out.append((at, data, None if owner else job))
        return out

    def owner_row(self, row, topic, at) -> dict:
        """The owner's history line ``row`` as the page gets it: its words without local paths (web_images.scrub: the attachment
        block becomes its "附件：…" line) and, under ``images``, the refs of the images it came with (data contract v19)."""
        content = str(row.get("content") or "")
        clean = {**row, "content": web_images.scrub(content)}
        if row.get("display_content"):
            clean["display_content"] = web_images.scrub(row["display_content"])
        clean["images"] = self.images.sent(content, topic, at, self.now) if self.images is not None else []
        return clean

    @staticmethod
    def put_images(data, refs) -> None:
        if refs:
            data["images"] = refs
        else:
            data.pop("images", None)

    def said(self, data, row, asked) -> None:
        """``data`` (an owner line's turn) takes web_said's fields (data contract v11): ``by`` always, ``display`` clipped as the
        body is; the answers of the question record ``asked`` when it has them (``put_answers`` may put them again)."""
        found = web_said.said(row, asked, self.origins)
        if found.get("display"):
            found["display"] = web_sync.clip(found["display"])
        if asked is not None and answer_rows(asked):
            found["answers"] = answer_rows(asked)
        data.update(found)

    @staticmethod
    def answered(questions, records) -> dict:
        """{("owner"|"agent", job key): [{question, choice}]} of the topic's answered question cards (data contract v4), from
        the question records alone, never a turn's words: an answer sent on as a follow-up task (its message ``answer-<token>``)
        is the owner's turn of that task; one handed to the task that asked it, which went on (a native question), is that
        task's own turn. A question skipped (不回答此题, 只发补充说明, or feedback on the questions) is NOT_ANSWERED."""
        follow = {str(record.get("message_id") or ""): record for record in records}
        found = {}
        for row in questions:
            answers = row.get("answers")
            if not isinstance(answers, dict) or not answers or not row.get("token"):
                continue
            listed = answer_rows(row)
            if not listed:
                continue
            after = follow.get("answer-" + str(row["token"]))
            if after is not None:
                found[("owner", job_key(after.get("message_id"), after.get("job_id")))] = listed
                continue
            if row.get("continuation_job_id"):
                continue  # Its follow-up task is not among the records: no turn to put it on.
            asked = next((record for record in records if record.get("job_id") == row.get("job_id")), None)
            if asked is not None:
                found.setdefault(("agent", job_key(asked.get("message_id"), asked.get("job_id"))), []).extend(listed)
        return found

    @staticmethod
    def put_answers(data, answered, job, owner) -> None:
        if not answered or job is None:
            return
        listed = answered.get(("owner" if owner else "agent", job_key(job.get("message_id"), job.get("job_id"))))
        if listed:
            data["answers"] = listed

    # -- a project's lead ---------------------------------------------------------------------------------------------------

    def lead_entries(self, state, topic, project, rows, records, wakes, answered=None, questions=()) -> list:
        """The lead's conversation: the owner's words and the lead's answers, as ``entries``; a wake-up turn (the Bridge's, not the owner's
        words) is left out, with its answer unless the lead meant it for the owner (project_loop.reported), and what it started is a line of
        the page's own (role event); so is a member's end (the inbox's ``thread_end`` receipt). All in the order they happened."""
        out, shown, waking = [], set(), False
        running = {job_key(job.get("message_id"), job.get("job_id")) for job in records if job.get("status") in RUNNING or job.get("status") == "waiting_user"}
        for row, job, asked in self.pair(rows, records, questions):
            at = epoch_seconds(row.get("time")) or 0
            if row.get("role") == "user":
                waking = projects.is_wake(row.get("message_id"))
                if not waking:
                    row = self.owner_row(row, topic, at)
                    data = {"role": "owner", "body": web_sync.clip(row.get("display_content") or row.get("content")), "status": "done",
                            "job_id": "", "started_at": at, "ended_at": at, "artifacts": []}
                    self.said(data, row, asked)
                    self.put_images(data, row.get("images"))
                    self.put_answers(data, answered, job, True)
                    out.append((at, data, None))
                continue
            key = job_key(job.get("message_id"), job.get("job_id")) if job is not None else ""
            if waking and (job is None or not project_loop.reported(state, topic, key)):
                waking = False
                continue
            waking = False
            if key:
                shown.add(key)
            data = {"role": "agent", "body": web_sync.clip(row.get("content")), "status": "done", "job_id": "", "started_at": at,
                    "ended_at": at, "artifacts": []}
            self.put_answers(data, answered, job, False)
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

    def member_ends(self, state, project) -> list:
        """[(time, "T834 做完了")] of the project's inbox: only the receipts of a member's end, which the owner reads as they are."""
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

    def lead_starts(self, state, topic, project, records) -> dict:
        """{job key of a lead's turn: [the member topics that turn started]}: a member belongs to the latest turn of the lead that began
        before the member's first task came in."""
        turns = sorted((board_read.started_at(record), job_key(record.get("message_id"), record.get("job_id")))
                       for record in records if not board_read.unrun(record))
        out = {}
        for member in project.get("threads") or ():
            if not isinstance(member, str):
                continue
            jobs = [record for record in topic_job_records(state, member) if not board_read.unrun(record)]
            if not jobs:
                continue
            first = min(jobs, key=board_read.job_number)
            came = moment(first.get("created_at")) or board_read.started_at(first)
            owner = [key for began, key in turns if began <= came + 2]
            if not owner:
                continue
            label, title = board_read.topic_label(state, member)
            out.setdefault(owner[-1], []).append({"key": member, "number": int(label[1:]) if label[1:].isdigit() else 0, "title": title,
                                                  "harness": str(read_object(topic_file(state, member)).get("harness_id") or "")})
        return out

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

    @staticmethod
    def retried(records, entries) -> dict:
        """{id(the job of an answer turn): [the tasks before it that it retried and no answer line took]}: a task the bridge's restart
        interrupted (or that failed with no line) and that an automatic retry (its record's ``retry_of``, app/auto_retry.py) went on
        with is part of the turn the retry answers. 2026-11-18 T488: task 4598 spent 137M tokens, was interrupted, and 4624 answered."""
        taken = {id(job) for _, data, job in entries if job is not None and data.get("role") == "agent"}
        by_message = {str(record.get("message_id")): record for record in records if record.get("message_id")}
        out = {}
        for _, data, job in entries:
            if job is None or data.get("role") != "agent":
                continue
            chain, seen, before = [], {id(job)}, by_message.get(str(job.get("retry_of") or ""))
            while before is not None and id(before) not in seen and id(before) not in taken and not board_read.unrun(before):
                chain.append(before)
                seen.add(id(before))
                before = by_message.get(str(before.get("retry_of") or ""))
            if chain:
                out[id(job)] = chain
        return out

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
        if key not in self.figures:
            if len(self.figures) > FIGURES_KEPT:
                self.figures.clear()
            self.figures[key] = web_turns.ended_figures(state, job)
        return self.figures[key]

    def ended_extras(self, state, job) -> dict:
        """The process, plan and figures of an ended turn, read once (they do not change once it has ended)."""
        key = (job_key(job.get("message_id"), job.get("job_id")), str(job.get("updated_at")))
        if key not in self.extra:
            if len(self.extra) > 512:
                self.extra.clear()
            self.extra[key] = web_turns.extras(state, job, False)
        return self.extra[key]

    def live_turn(self, state, topic, seq, job, wakes=None, now=0.0) -> dict:
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
        self.put_images(turn, self.images.shown(job, topic, turn["started_at"], now or time.time()) if self.images else [])
        estimate_of(job, turn)
        started = (wakes or {}).get(job_key(job.get("message_id"), job.get("job_id")))
        if started:
            turn["started_topics"] = started
        return turn

    @staticmethod
    def question(row, topic, count, project="", number=0, records=(), rows=()):
        status = row.get("status", "pending")
        if status == "pending" and row.get("display_pending"):
            return None  # Held for a project's lead: not the owner's yet.
        shown = []
        for index, each in enumerate(q for q in row.get("questions") or () if isinstance(q, dict)):
            shown.append({"id": str(each.get("id") or index), "header": str(each.get("header") or ""), "question": str(each.get("question") or ""),
                          "options": [{"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                                      for o in each.get("options") or () if isinstance(o, dict)],
                          "multiple": bool(each.get("multiple"))})
        answers = row.get("answers") if isinstance(row.get("answers"), dict) else {}
        said = web_images.scrub("；".join("、".join(str(a) for a in (value.get("answers") if isinstance(value, dict) else [value]) or ())
                                          for value in answers.values()))
        state = QUESTION_STATES.get(status, "answered")
        if state == "open" and triage.held(row):
            state = "held"  # The lead has it first (COVEY_PROJECT_TRIAGE): not the owner's to answer, so not 等你 on the page.
        closed_at, closed_by = closed(row, state, records, rows)
        return {"topic_key": topic, "turn_key": f"{topic}:{count + 1}" if status == "pending" else "", "questions": shown,
                "state": state, "answer": said, "asked_at": moment(row.get("created_at")),
                "answered_at": closed_at if state == "answered" else None, "closed_at": closed_at, "closed_by": closed_by,
                "project_key": project, "topic_number": number}

    # -- projects -------------------------------------------------------------------------------------------------------

    def project_items(self, state, chat, records=None) -> list:
        out = []
        for record in projects.records(state, chat) if records is None else records:
            pid = record["id"]
            members = [topic for topic in record.get("threads") or () if isinstance(topic, str)]
            lead = str(record.get("lead_topic_id") or "")
            word, why = self.project_state(record, [lead, *members])
            try:
                memory = [self.memory_row(row) for row in project_memory.live(state, record)]
                library = [self.library_row(state, row) for row in project_library.entries(state, pid) if not row.get("purged")]
            except Exception:  # noqa: BLE001 - the rest of the project is still worth pushing
                memory, library = [], []
            out.append(web_sync.item("project", pid, {
                "title": str(record.get("name") or ""), "goal": project_lead.lead_goal(record), "status": word, "status_reason": why,
                "lead_topic_key": lead, "topic_keys": members, "memory": memory, "library": library,
                "started_at": moment(record.get("created_at")), "updated_at": moment(record.get("updated_at"))}))
        return out

    @staticmethod
    def memory_row(row) -> dict:
        """One entry of a project's memory: its words, who said it (``web_turns.SAID_BY``) and when."""
        out = {"id": str(row.get("id") or ""), "text": row["text"], "kind": row["kind"], "source": row["source"], "at": row["at"]}
        said = web_turns.said_by(row["source"])
        if said:
            out["said_by"] = said
        if row["at"]:
            out["origin"] = {"at": row["at"]}  # The record does not say which topic it came from.
        return out

    @staticmethod
    def library_row(state, row) -> dict:
        out = {"name": row["name"], "link": row["ref"], "at": row["at"], "kind": web_turns.library_kind(row)}
        if row.get("size"):
            out["size"] = row["size"]
        if row.get("topic_id"):
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


def never_started(records) -> bool:
    """Whether a topic's last task was cancelled before it started (a launch card closed by a stop, one taken out of the queue)."""
    return bool(records) and records[-1].get("status") == "cancelled" and bool(records[-1].get("never_started"))


def board_groups(rows, dismissed) -> dict:
    """topic id -> its group on the board (app/pins.py PinnedTopics.look ``rows``, without the topics the owner took off,
    ``dismissed``), as a core/look.py STATES key: waiting, running, stuck, or a result's (stopped, result, done, cancelled).
    A project's thread is its own topic here (the board's row is its project's cell, the record says which thread). The page
    counts 要你处理 by this (data contract v6 ``board``), so a stopped topic the board no longer lists (seen KEEP_SEEN ago,
    taken off with 不管了) is not 停住了 there either: T514, 2026-11-19, stopped on 11-17 and seen at once, came back on the
    page as 停住了 while the board had let it go."""
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
            out[topic] = look.record_tone(record, seen=row.get("seen") is not None)
    return out


def closed(row, state, records=(), rows=()) -> tuple:
    """(closed_at, closed_by) of a question record whose web ``state`` is ``state`` (data contract v9): when it stopped waiting
    (epoch seconds, None when not known) and how. ``closed_by``: owner (answered), feedback (只发补充说明 or feedback on the
    questions), skipped (每题都选了不回答), lead (the project's lead answered it, COVEY_PROJECT_TRIAGE), superseded (a later
    question of the topic folded it), stale (daily maintenance closed it), switched, rewound, lead_stop, web_stop or cancelled;
    "" while it is open or held. The time is the record's own (core/store.py SETTLED_STAMPS); a record from before it has none,
    so: an answer's follow-up task (``answer-<token>``) was created then, the lead's triage move, the newer question that folded
    it, the asking task's last write when it was cancelled with it."""
    if state not in ("answered", "cancelled"):
        return None, ""
    found = triage.of(row)
    if state == "answered":
        if found.get("state") == triage.ANSWERED:
            how = "lead"
        elif row.get("response_kind") == "feedback":
            how = "feedback"
        else:
            how = "skipped" if all(each["choice"] == NOT_ANSWERED for each in answer_rows(row) or [{"choice": ""}]) else "owner"
        follow = next((record for record in records if record.get("message_id") == "answer-" + str(row.get("token") or "")), None)
        at = (moment(row.get("answered_at")) or moment((follow or {}).get("created_at"))
              or (moment(found.get("at")) if how == "lead" else 0.0))
        return at or None, how
    if row.get("status") == "superseded":
        newer = next((other for other in rows if other.get("token") and other.get("token") == row.get("superseded_by")), None)
        return (moment(row.get("cancelled_at")) or moment((newer or {}).get("created_at"))) or None, "superseded"
    asked = next((record for record in records if record.get("job_id") == row.get("job_id")), None)
    at = moment(row.get("cancelled_at")) or (moment((asked or {}).get("updated_at")) if asked else 0.0)
    reason = str(row.get("cancel_reason") or "")
    return at or None, reason if reason in CANCEL_REASONS else "cancelled"


def answer_rows(record) -> list:
    """[{question, choice}] of a question record's saved answers, in its questions' order: the options chosen or the words
    written, several joined with "、"; NOT_ANSWERED for a question skipped or one the owner gave feedback on instead."""
    answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
    feedback = record.get("response_kind") == "feedback"
    out = []
    for index, question in enumerate(q for q in record.get("questions") or () if isinstance(q, dict)):
        given = answers.get(question.get("id"), answers.get(str(index)))
        given = given.get("answers") if isinstance(given, dict) else given
        parts = [str(part) for part in (given if isinstance(given, list) else [given] if given else []) if str(part).strip()]
        parts = [part for part in parts if part != SKIPPED]
        out.append({"question": str(question.get("question") or question.get("header") or "")[:500],
                    "choice": NOT_ANSWERED if feedback or not parts else web_images.scrub("、".join(parts))[:500]})
    return out
