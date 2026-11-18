"""网页工作台同步 (docs/butler.md 网页工作台, COVEY_WEB=1 and COVEY_WEB_CONFIG): pushes the state of the owner's projects, topics, turns and
questions to the web page (the 妙搭 app) and fetches what the page sent, which app/web_ops.py does. Off by default: without the switch
there is no thread and no request.

- ``WebSync`` (app/unified.py PARTS ``web_sync``, a feature object: ``threads()``, none without the switch): two supervised threads. ``web-push``
  builds the items (``Collector``), keeps those whose ``rev`` (core/web_sync.py) differs from what was sent, and posts them to
  /sync/upsert in batches; ``web-inbox`` asks /sync/inbox, writes each operation's seq and op_id into the ledger before it is done, does it
  (``Ops.run``) and acknowledges it at /sync/ack. A failed request backs off (2, 4 ... 30 seconds) and touches no other thread.
- What is read, and how often: every ``push_seconds`` only the running tasks and the pending questions (two index queries, the records they
  name), plus the topics that stopped running a moment ago or that the page just acted on; every FULL_SECONDS all topics of the last
  TOPIC_DAYS days, the projects, and the deletions. A topic's state and "做完没看" are the board's reading (core/board_read.py, app/pins.py
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

from covey.app import pins, project_lead, project_loop, web_turns
from covey.app.answers import SKIPPED
from covey.app.web_ops import Ops
from covey.core import board_read, failure, http_upstream, look, project_library, project_memory, projects, triage, web_sync
from covey.core import progress as turn_progress
from covey.core.history_storage import history_lines
from covey.core.butler import butler_topic_id
from covey.core.jobs import job_key, select_job_records, topic_job_records
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
from covey.core.topics import topic_file
from covey.core.trace_store import load_trace

FULL_SECONDS = web_sync.FULL_SECONDS
TOPIC_DAYS = 30  # A full look takes the topics with a job written within this many days.
TURNS_KEPT = 100  # The newest turns of a topic that are pushed.
MAX_BACKOFF = 30
TIMEOUT = 20
STUCK_SECONDS = 60  # A step with no progress in this long is left behind (an NFS read can hold its thread for good); the next runs in a new thread.
MAX_LEFT = 3  # Left-behind steps still stuck: with this many no new thread is started until one of them ends.
STATE_WORDS = {"done": "做完", "waiting_user": "等你", "cancelled": "已停止", "failed": "失败", "interrupted": "失败"}
BUTLER_TITLE = "管家"
LEAD_TITLE = "负责人"
EXTRA_TURNS = 20  # The newest turns of a topic that get their process, plan and figures (each one read from its trace file, once when it ended).
RECEIPTS_KEPT = 200  # The newest receipts of a project's inbox that are looked at.
QUESTION_STATES = {"pending": "open", "cancelled": "cancelled", "superseded": "cancelled"}
NOT_ANSWERED = "没答"  # turn.data answers' choice of a question the owner chose not to answer (data contract v4).
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

    def __init__(self, env) -> None:
        self.env = env
        self.hot, self.dirty = set(), set()
        self.unseen = set()
        self.states = {}  # topic id -> its state word, from its last build (a project's 等你 reads it).
        self.pushed_topics = set()
        self.history = {}  # topic id -> ((mtime_ns, size), rows)
        self.leads = {}  # lead topic id -> its project's record (COVEY_PROJECTS=1 only), from this look.
        self.extra = {}  # (job key, updated_at) -> the optional fields of an ended turn (web_turns.extras).
        self.receipts = {}  # project id -> {receipt file name: its row} (thread_end only).
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
        running = board_read.running_jobs(state, chat)
        asking = board_read.pending_questions(state, chat)
        butler = butler_topic_id(chat)
        records = projects.records(state, chat)
        self.leads = {str(record.get("lead_topic_id")): record for record in records if record.get("lead_topic_id")} if projects.enabled() else {}
        family = [butler, *self.leads]  # The conversations that are not on the board: the butler's and the projects' leads.
        hot = {row.get("topic_id") for row in (*running, *asking) if row.get("topic_id")}
        hot |= self.family_live(state, chat, family)  # A turn of one runs or asks (the board does not list them).
        with self.lock:
            touched, self.dirty = self.dirty, set()
        if full or hot != self.hot or touched:
            read = board_read.read(state, chat, now, keep=pins.kept(state, chat) if pins.laid() else ())
            self.unseen = set(pins.board_results(state, chat, now, read)[0])
        if full:
            by_topic = {topic: records_ for topic, records_ in board_read.topic_jobs(state, chat).items()
                        if topic in hot or topic in touched or self.recent(records_, now)}
        else:
            by_topic = {topic: sorted(topic_job_records(state, topic, chat_id=chat), key=board_read.job_number)
                        for topic in hot | self.hot | touched if topic not in family}
        looked = [topic for topic in family if full or topic in hot or topic in self.hot or topic in touched]
        self.hot = hot
        items = []
        for topic, jobs in by_topic.items():
            items += self.topic_items(state, chat, topic, jobs, now)
            self.moved = time.monotonic()
        for topic in looked:
            mine = self.topic_items(state, chat, topic, sorted(topic_job_records(state, topic, chat_id=chat), key=board_read.job_number), now)
            self.moved = time.monotonic()
            items += mine
            if mine:
                by_topic[topic] = []
        deleted = []
        if full:
            seen = {("topic", topic) for topic in by_topic}
            deleted += [{"type": kind, "key": key} for kind, key in sorted(self.pushed_topics - seen)]
            self.pushed_topics = seen
        elif by_topic:
            self.pushed_topics |= {("topic", topic) for topic in by_topic}
        items += self.project_items(state, chat, records)
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
        word = self.word(records, latest, questions)
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
            "model": str((latest or {}).get("model") or ""), "updated_at": updated, "unseen": topic in self.unseen and not butler and not project,
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
        body["tone"] = self.tone(records, latest, questions, body["unseen"], body.get("progress"))
        gist = self.gist(state, topic) if not (butler or project) else None
        if gist:
            body["gist"] = gist
        out = [web_sync.item("topic", topic, body)]
        out += [web_sync.item("turn", f"{topic}:{turn['seq']}", turn) for turn in turns]
        for row in questions:
            made = self.question(row, topic, count, body["project_key"], number)
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
    def word(records, latest, questions) -> str:
        """The topic's state word. Running is what RUNNING says; a ``continued`` turn's outcome is its follow-up's, so the
        turn before it speaks; any other status (waiting_workspace, one this does not know) is not running: 空闲."""
        if any(row.get("status", "pending") == "pending" and not row.get("display_pending") for row in questions):
            return "等你"
        if any(record.get("status") in RUNNING for record in records):
            return "在做"
        if latest is not None and latest.get("status") == "continued":
            latest = next((record for record in reversed(records) if record.get("status") in STATE_WORDS), None)
        if latest is None:
            return "空闲"
        return STATE_WORDS.get(latest.get("status"), "空闲")

    @staticmethod
    def tone(records, latest, questions, unseen, progress) -> str:
        """The topic's state as a core/look.py STATES key (data contract v5 ``tone``): the page colours by it, and says
        its word. As ``word`` reads the records, and more: 可能卡住 for a running turn that may be stuck, 有结果 for an
        ended one not seen (``unseen``) and 做完 once seen, 停住了 for a failed one and for one stopped by anyone but the
        owner (core/look.py owner_stopped), 已停止 for his own."""
        if any(row.get("status", "pending") == "pending" and not row.get("display_pending") for row in questions):
            return "waiting"
        live = next((record for record in reversed(records) if record.get("status") in RUNNING), None)
        if live is not None:
            return look.record_tone(live, stuck=bool((progress or {}).get("maybe_stuck")))
        if latest is not None and latest.get("status") == "continued":
            latest = next((record for record in reversed(records) if record.get("status") in STATE_WORDS), None)
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
        entries = (self.lead_entries(state, topic, project, rows, records, wakes, answered) if project is not None
                   else self.entries(rows, records, answered))
        total = len(entries)
        out = []
        for seq, (at, data, job) in enumerate(entries, 1):
            if seq <= total - TURNS_KEPT:
                continue
            turn = {"topic_key": topic, "seq": seq, **data}
            if job is not None and seq > total - EXTRA_TURNS:
                turn.update(self.ended_extras(state, job))
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
            if fail:
                ended["failure"] = fail
            out.append(ended)
        return out, total

    @staticmethod
    def pair(rows, records):
        """(row, the job record of the owner's message the row answers or None) for each user and assistant line of the history: a user line
        is paired by its message id, the next assistant line has the job of the user line before it."""
        by_message = {str(record.get("message_id")): record for record in records if record.get("message_id") and not board_read.unrun(record)}
        job = None
        for row in rows:
            if row.get("role") == "user":
                job = by_message.get(str(row.get("message_id") or ""))
                yield row, job
            else:
                yield row, job
                job = None

    def entries(self, rows, records, answered=None) -> list:
        """[(time, turn data, job record or None)] of a topic's history lines; the job only on an assistant line. ``answered``
        (``answered``): the turns that answered a question card get its ``answers``."""
        out = []
        for row, job in self.pair(rows, records):
            at = epoch_seconds(row.get("time")) or 0
            owner = row.get("role") == "user"
            body = row.get("display_content") if owner and row.get("display_content") else row.get("content")
            data = {"role": "owner" if owner else "agent", "body": web_sync.clip(body), "status": "done", "job_id": "",
                    "started_at": at, "ended_at": at, "artifacts": []}
            self.put_answers(data, answered, job, owner)
            out.append((at, data, None if owner else job))
        return out

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

    def lead_entries(self, state, topic, project, rows, records, wakes, answered=None) -> list:
        """The lead's conversation: the owner's words and the lead's answers, as ``entries``; a wake-up turn (the Bridge's, not the owner's
        words) is left out, with its answer unless the lead meant it for the owner (project_loop.reported), and what it started is a line of
        the page's own (role event); so is a member's end (the inbox's ``thread_end`` receipt). All in the order they happened."""
        out, shown, waking = [], set(), False
        running = {job_key(job.get("message_id"), job.get("job_id")) for job in records if job.get("status") in RUNNING or job.get("status") == "waiting_user"}
        for row, job in self.pair(rows, records):
            at = epoch_seconds(row.get("time")) or 0
            if row.get("role") == "user":
                waking = projects.is_wake(row.get("message_id"))
                if not waking:
                    data = {"role": "owner", "body": web_sync.clip(row.get("display_content") or row.get("content")), "status": "done",
                            "job_id": "", "started_at": at, "ended_at": at, "artifacts": []}
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
        estimate_of(job, turn)
        started = (wakes or {}).get(job_key(job.get("message_id"), job.get("job_id")))
        if started:
            turn["started_topics"] = started
        return turn

    @staticmethod
    def question(row, topic, count, project="", number=0):
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
        said = "；".join("、".join(str(a) for a in (value.get("answers") if isinstance(value, dict) else [value]) or ())
                        for value in answers.values())
        state = QUESTION_STATES.get(status, "answered")
        if state == "open" and triage.held(row):
            state = "held"  # The lead has it first (COVEY_PROJECT_TRIAGE): not the owner's to answer, so not 等你 on the page.
        # The record has no answer time of its own: the last write of an answered question is the nearest to it.
        answered = (moment(row.get("answered_at")) or moment(row.get("updated_at")) or None) if state == "answered" else None
        return {"topic_key": topic, "turn_key": f"{topic}:{count + 1}" if status == "pending" else "", "questions": shown,
                "state": state, "answer": said, "asked_at": moment(row.get("created_at")), "answered_at": answered,
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
                "title": str(record.get("name") or ""), "goal": str(record.get("goal") or ""), "status": word, "status_reason": why,
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
            return "做完", ""
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
        self.collector = Collector(env)
        self.ops = Ops(env, hear)
        self.sent = {}  # (type, key) -> the rev pushed last.
        self.last_full = 0.0
        self.ledger = None
        self.down = {}  # thread name -> failures in a row.

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
                while len(left) >= MAX_LEFT and not self.runtime.shutdown.wait(STUCK_SECONDS):
                    left = [each for each in left if each.thread.is_alive()]
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
        """After a look was left behind: a new collector, nothing counted as sent, so the next look is a full one. The left-behind
        look keeps writing only to the old ones."""
        self.collector, self.sent, self.last_full = Collector(self.env), {}, 0.0

    def push_once(self, now=None) -> int:
        """One look and one push; how many items went out. Raises WebError when the page did not take a batch (the batches before it count).
        A full look that has nothing to send still posts an empty batch, so the page's 最后同步 moves on in a quiet minute."""
        chat = self.runtime.access.owner_chat()
        if not chat or not self.env.state_dir:
            return 0
        collector, sent = self.collector, self.sent
        now = time.time() if now is None else now
        full = now - self.last_full >= FULL_SECONDS
        items, deleted = collector.collect(chat, now, full)
        changed = [each for each in items if sent.get((each["type"], each["key"])) != each["rev"]]
        count = 0
        for batch in web_sync.batches(changed):
            self.client.request("POST", "/sync/upsert", {"items": batch, "deleted": []})
            collector.moved = time.monotonic()  # The first push of a big chat is many batches: each one taken is progress.
            for each in batch:
                sent[(each["type"], each["key"])] = each["rev"]
            count += len(batch)
        if deleted or (full and not count):
            self.client.request("POST", "/sync/upsert", {"items": [], "deleted": deleted})
            for each in deleted:
                sent.pop((each["type"], each["key"]), None)
        if full and collector is self.collector:
            self.last_full = now
        return count

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
            elif known.get("state") == "received":
                ledger.finish(op_id, "failed", "这条执行到一半没有结果（Covey 重启或卡住了），结果不确定，没有重发")
            saved = ledger.get(op_id)
            results.append({"seq": seq, "op_id": op_id, "state": saved["state"], "message": saved["message"]})
        if results:
            self.client.request("POST", "/sync/ack", {"results": results})
            ledger.acked([each["op_id"] for each in results])
        return fresh


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
                    "choice": NOT_ANSWERED if feedback or not parts else "、".join(parts)[:500]})
    return out
