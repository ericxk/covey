"""网页工作台同步的收集 (app/web_sync.py): ``Collector`` reads the records of the owner's chat and makes the items (topics, turns, questions, projects)
the page is sent. Reading only: it sends nothing and holds no lock (rule 1, AGENTS.md). The reading rules are in app/web_sync.py's docstring."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from covey.app import pins, project_lead, project_loop, web_images, web_pairing, web_said, web_turns
from covey.core import board_read, failure, look, project_library, project_memory, projects, web_sync
from covey.core import progress as turn_progress
from covey.core.history_storage import history_lines
from covey.core.butler import butler_topic_id
from covey.core.jobs import job_key, select_job_records, topic_job_records
from covey.core.model_names import model_name
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
from covey.core.topics import topic_file
from covey.core.trace_store import load_trace

PASS_SLICE = 25  # Topics of a full pass looked at per step, after the hot ones: a pass is many steps, never one long look.
TOPIC_DAYS = 30  # A full look takes the topics with a job written within this many days.
TURNS_KEPT = 100  # The newest turns of a topic that are pushed.
STATE_WORDS = {"done": "做完", "waiting_user": "等你", "cancelled": "已停止", "failed": "失败", "interrupted": "失败"}
BUTLER_TITLE = "管家"
LEAD_TITLE = "负责人"
EXTRA_TURNS = 20  # The newest turns of a topic that get their process and plan; every kept turn gets its figures (each read from its trace file once).
FIGURES_KEPT = 8192  # Ended turns whose figures are remembered (a few numbers each): all kept turns of all topics a full look builds.
RECEIPTS_KEPT = 200  # The newest receipts of a project's inbox that are looked at.
EDIT_PREFIX = "edit-"  # The message id of the owner's edit sent into a running task (app/source_watch.py): edit-<message id>-<stamp>.
UNSTARTED_WORDS = "没开工，已取消"  # The line under the owner's words whose task was cancelled before it started (its record's reason after it).
LIVE = ("running", "cancel_requested")  # The statuses whose turn has a topic.data progress.


def estimate_of(job, turn) -> None:
    """``turn`` takes ``estimate_seconds``, the first estimate of its whole 用时 the task monitor kept on the job record
    (core/longtask.py), or loses it: a turn whose task never had one has none."""
    value = job.get("estimate_seconds")
    if type(value) is int and value > 0:
        turn["estimate_seconds"] = value
    else:
        turn.pop("estimate_seconds", None)


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
            made = web_pairing.question(row, topic, count, body["project_key"], number, records, questions)
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
            wakes = web_pairing.lead_starts(state, project, records)
        answered = web_pairing.answered(questions, records)
        entries = (self.lead_entries(state, topic, project, rows, records, wakes, answered, questions) if project is not None
                   else self.entries(rows, records, answered, questions, topic=topic))
        total = len(entries)
        self.count_unpaired(topic, entries)
        before = web_pairing.retried(records, entries)
        out = []
        for seq, (at, data, job) in enumerate(entries, 1):
            if seq <= total - TURNS_KEPT:
                continue
            turn = {"topic_key": topic, "seq": seq, **data}
            if job is not None and data.get("role") == "agent":
                turn["job_id"] = str(job.get("job_id") or "")
                web_pairing.put_images(turn, self.images.shown(job, topic, data.get("started_at"), self.now) if self.images else [])
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
                # Why it did not succeed and what next (core/failure.py). Not of a task still running: its answer line is written
                # once its result has gone out and before its record says so (app/worker.py deliver), and a look in between
                # read it as 没发出去 (T588 #5309, 2026-11-20 16:29: red until after 16:44).
                fail = failure.failure_view(job) if job.get("status") not in RUNNING else None
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
        if (job is not None and job.get("status") in RUNNING and rows and rows[-1].get("role") != "user"
                and str(rows[-1].get("job_id") or "") == str(job.get("job_id"))):
            job = None  # Its answer is the last line already, its record not yet ended: no second, running turn of it (T588 #5309).
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
            web_pairing.put_answers(data, answered, job, owner)
            out.append((at, data, None if owner else job))
        return out

    @staticmethod
    def merge_edit(data, message_id, by_message) -> None:
        """The owner's edit of a message sent into the running task as a line of its own (app/source_watch.py, message id
        ``edit-<message id>-<stamp>``) goes onto the line of the message it edits: that one shows the edited text, marked edited
        (its ``body`` stays the original), and the edit's own line is ``merged`` (the page leaves it out). Without the first line
        among these (older than the turns kept) the edit stays a line of its own. Data contract v19 (T584, 2026-11-20: both
        showed, the second as 改过)."""
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
        task it started (``job``, paired by its message id), under "收到的附件" (T584, 2026-11-20: 管家转来 showed no image)."""
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
            web_pairing.put_answers(data, answered, job, False)
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
        web_pairing.put_images(turn, self.images.shown(job, topic, turn["started_at"], now or time.time()) if self.images else [])
        estimate_of(job, turn)
        started = (wakes or {}).get(job_key(job.get("message_id"), job.get("job_id")))
        if started:
            turn["started_topics"] = started
        return turn

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
                "started_at": web_pairing.moment(record.get("created_at")), "updated_at": web_pairing.moment(record.get("updated_at"))}))
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

