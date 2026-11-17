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
- The key is read from the config's key_file and appears in no log line and no record.

Rule 1 (AGENTS.md): no lock is held across a request; records are read, not locked."""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from covey.app import pins
from covey.app.web_ops import Ops
from covey.core import board_read, http_upstream, project_library, project_memory, projects, web_sync
from covey.core.history_storage import history_lines
from covey.core.butler import butler_topic_id
from covey.core.jobs import select_job_records, topic_job_records
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
STUCK_SECONDS = 60  # A step not ended in this long is left behind (an NFS read can hold its thread for good); the next runs in a new thread.
MAX_LEFT = 3  # Left-behind steps still stuck: with this many no new thread is started until one of them ends.
STATE_WORDS = {"done": "做完", "waiting_user": "等你", "cancelled": "已停止", "failed": "失败", "interrupted": "失败"}
BUTLER_TITLE = "管家"
QUESTION_STATES = {"pending": "open", "cancelled": "cancelled", "superseded": "cancelled"}


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

    def call(self, step, seconds):
        self.steps.put(step)
        try:
            value, exc = self.ends.get(timeout=seconds)
        except queue.Empty:
            self.steps.put(None)
            raise Stuck from None
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
        hot = {row.get("topic_id") for row in (*running, *asking) if row.get("topic_id")}
        if self.butler_live(state, chat, butler):
            hot.add(butler)  # The butler's turn runs or asks (the board does not list it).
        with self.lock:
            touched, self.dirty = self.dirty, set()
        if full or hot != self.hot or touched:
            read = board_read.read(state, chat, now, keep=pins.kept(state, chat) if pins.laid() else ())
            self.unseen = set(pins.board_results(state, chat, now, read)[0])
        if full:
            by_topic = {topic: records for topic, records in board_read.topic_jobs(state, chat).items()
                        if topic in hot or topic in touched or self.recent(records, now)}
        else:
            by_topic = {topic: sorted(topic_job_records(state, topic, chat_id=chat), key=board_read.job_number)
                        for topic in hot | self.hot | touched if topic != butler}
        look_butler = full or butler in hot or butler in self.hot or butler in touched
        self.hot = hot
        items = []
        for topic, records in by_topic.items():
            items += self.topic_items(state, chat, topic, records, now)
        if look_butler:
            mine = self.topic_items(state, chat, butler, sorted(topic_job_records(state, butler, chat_id=chat), key=board_read.job_number), now)
            items += mine
            if mine:
                by_topic[butler] = []
        deleted = []
        if full:
            seen = {("topic", topic) for topic in by_topic}
            deleted += [{"type": kind, "key": key} for kind, key in sorted(self.pushed_topics - seen)]
            self.pushed_topics = seen
        elif by_topic:
            self.pushed_topics |= {("topic", topic) for topic in by_topic}
        items += self.project_items(state, chat)
        return items, deleted

    @staticmethod
    def butler_live(state, chat, butler) -> bool:
        """Whether the butler's task runs or a question of it waits: two index queries."""
        return bool(select_job_records(state, lambda record: record.get("topic_id") == butler, chat_id=chat, status=frozenset(RUNNING))
                    or directory_store(state / "questions").query(chat_id=chat, topic_id=butler, status="pending"))

    @staticmethod
    def recent(records, now) -> bool:
        return any((epoch_seconds(record.get("updated_at")) or now) >= now - TOPIC_DAYS * 86400 for record in records)

    # -- topics ---------------------------------------------------------------------------------------------------------

    def topic_items(self, state, chat, topic, records, now) -> list:
        butler = topic == butler_topic_id(chat)
        if not records and not (butler and self.rows(state, topic)):
            return []  # A topic with no task has nothing the page shows (and a name the page made up is not looked up).
        asking = sorted(directory_store(state / "questions").query(chat_id=chat, topic_id=topic), key=lambda pair: pair[1].get("created_at") or 0)
        questions = [row for _, row in asking]
        latest = next((record for record in reversed(records) if not board_read.unrun(record)), None)
        word = self.word(records, latest, questions)
        self.states[topic] = word
        label, title = ("", BUTLER_TITLE) if butler else board_read.topic_label(state, topic)
        data = read_object(topic_file(state, topic))
        found = None if butler else projects.topic_project(state, topic)
        turns, count = self.turns(state, topic, records, latest, word)
        number = int(label[1:]) if label[1:].isdigit() else 0
        updated = max((epoch_seconds(record.get("updated_at")) or 0 for record in records), default=0)
        out = [web_sync.item("topic", topic, {
            "number": number, "title": title, "project_key": found.id if found else "", "state": word,
            "workspace": str(data.get("workspace_name") or ""), "harness": str(data.get("harness_id") or ""),
            "model": str((latest or {}).get("model") or ""), "updated_at": updated, "unseen": topic in self.unseen and not butler,
            "kind": "butler" if butler else "topic"})]
        out += [web_sync.item("turn", f"{topic}:{turn['seq']}", turn) for turn in turns]
        for row in questions:
            made = self.question(row, topic, count)
            if made:
                out.append(web_sync.item("question", row.get("token"), made))
        return out

    @staticmethod
    def word(records, latest, questions) -> str:
        if any(row.get("status", "pending") == "pending" and not row.get("display_pending") for row in questions):
            return "等你"
        if any(record.get("status") in RUNNING for record in records):
            return "在做"
        if latest is None:
            return "空闲"
        return STATE_WORDS.get(latest.get("status"), "在做")

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

    def turns(self, state, topic, records, latest, word) -> tuple:
        """([turn data with seq], number of turns in the history) for ``topic``: the newest TURNS_KEPT, and one more for the task that runs
        (its progress notes as the body), waits or ended without an answer."""
        rows = self.rows(state, topic)
        total = len(rows)
        out = []
        for seq, row in enumerate(rows, 1):
            if seq <= total - TURNS_KEPT:
                continue
            at = epoch_seconds(row.get("time")) or 0
            body = row.get("display_content") if row.get("role") == "user" and row.get("display_content") else row.get("content")
            out.append({"topic_key": topic, "seq": seq, "role": "owner" if row.get("role") == "user" else "agent",
                        "body": web_sync.clip(body), "status": "done", "job_id": "", "started_at": at, "ended_at": at, "artifacts": []})
        job = next((record for record in reversed(records) if record.get("status") in RUNNING or record.get("status") == "waiting_user"), None)
        if job is not None:
            out.append(self.live_turn(state, topic, total + 1, job))
        elif latest is not None and latest.get("status") in ("failed", "interrupted", "cancelled") and (not rows or rows[-1].get("role") == "user"):
            at = epoch_seconds(latest.get("updated_at")) or 0
            out.append({"topic_key": topic, "seq": total + 1, "role": "agent", "body": web_sync.clip(latest.get("status_message") or ""),
                        "status": "stopped" if latest.get("status") == "cancelled" else "failed", "job_id": str(latest.get("job_id") or ""),
                        "started_at": board_read.started_at(latest), "ended_at": at, "artifacts": []})
        return out, total

    def live_turn(self, state, topic, seq, job) -> dict:
        notes = []
        try:
            snapshot = load_trace(state, job) or {}
            steps = snapshot.get("steps") or {}
            for key in snapshot.get("step_order") or list(steps):
                step = steps.get(key) or {}
                if step.get("status") == "note" and step.get("title") == "进展说明" and step.get("detail"):
                    notes.append(str(step["detail"]))
        except (OSError, ValueError, KeyError, TypeError):
            notes = []
        return {"topic_key": topic, "seq": seq, "role": "agent", "body": web_sync.clip("\n\n".join(notes)),
                "status": "waiting" if job.get("status") == "waiting_user" else "running", "job_id": str(job.get("job_id") or ""),
                "started_at": board_read.started_at(job), "ended_at": 0, "artifacts": []}

    @staticmethod
    def question(row, topic, count):
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
        return {"topic_key": topic, "turn_key": f"{topic}:{count + 1}" if status == "pending" else "", "questions": shown,
                "state": QUESTION_STATES.get(status, "answered"), "answer": said}

    # -- projects -------------------------------------------------------------------------------------------------------

    def project_items(self, state, chat) -> list:
        out = []
        for record in projects.records(state, chat):
            pid = record["id"]
            members = [topic for topic in record.get("threads") or () if isinstance(topic, str)]
            lead = str(record.get("lead_topic_id") or "")
            word, why = self.project_state(record, [lead, *members])
            try:
                memory = [{"text": row["text"], "kind": row["kind"], "source": row["source"], "at": row["at"]}
                          for row in project_memory.live(state, record)]
                library = [{"name": row["name"], "link": row["ref"], "at": row["at"]}
                           for row in project_library.entries(state, pid) if not row.get("purged")]
            except Exception:  # noqa: BLE001 - the rest of the project is still worth pushing
                memory, library = [], []
            out.append(web_sync.item("project", pid, {
                "title": str(record.get("name") or ""), "goal": str(record.get("goal") or ""), "status": word, "status_reason": why,
                "lead_topic_key": lead, "topic_keys": members, "memory": memory, "library": library,
                "updated_at": epoch_seconds(record.get("updated_at")) if isinstance(record.get("updated_at"), str) else (record.get("updated_at") or 0)}))
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
        return (("web-push", lambda ctx: self.loop("push", self.push_once, lambda: self.client.settings().push_seconds, self.start_over)),
                ("web-inbox", lambda ctx: self.loop("inbox", self.inbox_once, lambda: self.client.settings().poll_seconds)))

    def loop(self, label, step, seconds, reset=None) -> None:
        """``step`` every ``seconds()``, in a Worker; a failure is logged (the first, then every tenth) and waited out 2, 4 ... MAX_BACKOFF
        seconds. A step not ended in STUCK_SECONDS is logged and left behind: ``reset()`` drops what it was building and the next step
        runs in a new Worker (none while MAX_LEFT left-behind ones are still stuck)."""
        worker, left = Worker(f"web-{label}-step"), []
        while not self.runtime.shutdown.is_set():
            try:
                worker.call(step, STUCK_SECONDS)
                if self.down.pop(label, 0):
                    self.runtime.log(f"web sync {label} recovered")
                wait = seconds()
            except Stuck:
                left = [each for each in (*left, worker) if each.thread.is_alive()]
                self.runtime.log(f"web sync {label} stuck: a step has not ended in {STUCK_SECONDS} s; left behind ({len(left)} so far)")
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
