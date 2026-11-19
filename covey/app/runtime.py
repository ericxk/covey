"""Process-wide Bridge state, one object passed by reference to every part that needs it.

Nothing here knows a Harness: the job registry, the scheduler's queue, the
shutdown signal, the log and the owner identity are the same for all of them.
"""
from __future__ import annotations

import datetime as _dt
import queue
import threading
from pathlib import Path

from covey.app import detached
from covey.app.access import Access
from covey.core import butler_tracks, timing
from covey.core.jobs import job_key
from covey.core.scheduler import topic_key
from covey.core.source_book import SourceBook


def print_log(message: str) -> None:
    # Milliseconds: the timing lines (core/timing.py) and a flow's other lines line up.
    ts = _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
    print(f"[{ts}] {message}", flush=True)


def job_stage(job) -> str:
    """Where a task is for a supplement: queued (registered, not started), starting (taken by a worker, no backend
    control yet), running, paused (waiting for an answer) or ended. Reads the task's own fields only (no lock, no
    file, no call): app/steering.py steer_stage says it, and ``JobRegistry.family_live`` counts by it."""
    if job.cancel_event.is_set() or job.answer_finalized:
        return "ended"
    control = job.backend_control
    if control is not None:
        waiting = getattr(control, "pause_for_question", "") or getattr(control, "awaiting_answer", False) is True
        return "paused" if waiting else "running"
    return "starting" if job.execution_started else "queued"


def supplement_target_of(jobs, stage):
    """Of a topic's ``jobs``, the one a supplement goes to, ``stage`` saying where each is (``job_stage``, or Steering's
    steer_stage): the newest that has started (starting, running, paused), else the newest queued; None when none is
    open. A job cancelled before it started is none of them."""
    open_jobs = [j for j in jobs if stage(j) != "ended" and not getattr(j, "cancelled_before_start", False)]
    started = [j for j in open_jobs if stage(j) != "queued"]
    return max(started or open_jobs, key=lambda j: int(j.job_id), default=None)


class JobRegistry:
    """Every job this process runs or waits on, by job id and by message, and the queue the
    scheduler takes them from. ``lock`` guards ``by_id``, ``by_message_id``, ``finishing`` and ``counter``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_id: dict = {}
        self.by_message_id: dict = {}
        # Jobs that ended while their task runner still uploads (detached.release_done), by message: out
        # of the registry, so their topic goes on, but the outbox, automatic retries and maintenance
        # wait for the runner's exit as before.
        self.finishing: set = set()
        self.queue: queue.Queue = queue.Queue()
        self.counter = 0

    def settling(self, message_id) -> bool:
        """``message_id``'s job is registered here, or has ended with its task runner still finishing."""
        with self.lock:
            return message_id in self.by_message_id or message_id in self.finishing

    def next_id(self) -> int:
        with self.lock:
            self.counter += 1
            return self.counter

    def register(self, job) -> None:
        with self.lock:
            self.by_id[int(job.job_id)] = job
            if job.message_id:
                self.by_message_id[str(job.message_id)] = job

    def unregister(self, job) -> None:
        with self.lock:
            self.by_id.pop(int(job.job_id), None)
            if job.message_id and self.by_message_id.get(str(job.message_id)) is job:
                self.by_message_id.pop(str(job.message_id), None)

    def enqueue(self, job) -> None:
        self.register(job)
        self.queue.put(job)

    def find(self, job_id: int, message_id: str = ""):
        with self.lock:
            job = self.by_id.get(job_id)
            if job is not None and (not message_id or job.message_id == message_id):
                return job
            if message_id:
                return self.by_message_id.get(message_id)
            return None

    def live_job(self, topic_id, chat_id, job_id=None):
        """The topic's job whose backend takes live controls (answers, supplements) now: not one
        paused on a question. ``job_id`` narrows it to that job."""
        with self.lock:
            return next(
                (
                    j
                    for j in self.by_id.values()
                    if j.topic_id == topic_id
                    and j.chat_id == chat_id
                    and j.backend_control is not None
                    and not getattr(j.backend_control, "pause_for_question", "")
                    and (job_id is None or j.job_id == int(job_id))
                ),
                None,
            )

    def ahead_of(self, job):
        """This topic's jobs before ``job`` that are still to run or running."""
        with self.lock:
            return sorted((j for j in self.by_id.values()
                           if j is not job and j.topic_id == job.topic_id
                           and j.chat_id == job.chat_id and int(j.job_id) < int(job.job_id)
                           and not j.cancelled_before_start
                           and not (j.cancel_event and j.cancel_event.is_set())),
                          key=lambda j: int(j.job_id))

    def topic_jobs(self, topic_id, chat_id) -> list:
        """This topic's jobs here, in the order they were registered."""
        with self.lock:
            return [j for j in self.by_id.values() if j.topic_id == topic_id and j.chat_id == chat_id]

    def open_in_topic(self, topic_id, chat_id) -> list:
        """This topic's jobs here that have not ended (``job_stage``: queued, starting, running, paused), in the order they
        were registered. Reads the registry and the jobs' own fields only."""
        with self.lock:
            return [j for j in self.by_id.values()
                    if j.topic_id == topic_id and j.chat_id == chat_id and job_stage(j) != "ended"]

    def supplement_target(self, topic_id, chat_id):
        """The task of this topic a supplement goes to: the newest one that has started (``job_stage`` starting, running
        or paused: a paused one refuses it, and the sender says so), else the newest queued one; None when none is open
        here. A message queued behind the running turn is never it while that turn runs (T551, 2026-11-19: three of the
        butler's supplements went into the queued #5008 and the running #4992 read none). Reads this registry only."""
        with self.lock:
            jobs = [j for j in self.by_id.values() if j.topic_id == topic_id and j.chat_id == chat_id]
        return supplement_target_of(jobs, job_stage)

    def family_live(self, chat_id) -> list:
        """The butler family's tasks in ``chat_id`` (core/butler_tracks.py: slot 0, with COVEY_BUTLER_TRACKS=1 the
        tracks too) that are not ended and not waiting for an answer, as ``job_stage`` says (queued, starting,
        running), oldest first. The tracks that are running count against COVEY_BUTLER_TRACKS_MAX; a task waiting for the
        owner's answer holds its slot but is not one of them. Reads this registry only, no file and no call."""
        topics = frozenset(butler_tracks.family_ids(chat_id))
        with self.lock:
            live = [job for job in self.by_id.values()
                    if job.chat_id == chat_id and job.topic_id in topics
                    and job_stage(job) in ("queued", "starting", "running")]
        return sorted(live, key=lambda job: int(job.job_id))

    def in_topic(self, topic_id, *, started=False) -> bool:
        """A job of this topic is here, in any chat; ``started``: one no longer queued."""
        with self.lock:
            return any(j.topic_id == topic_id and (not started or j.execution_started) for j in self.by_id.values())

    def has_key(self, key) -> bool:
        """The job whose record is ``key`` is here: queued, running or being handed over."""
        with self.lock:
            return any(job_key(j.message_id, j.job_id) == key for j in self.by_id.values())

    def topic_count(self) -> int:
        """How many topics have a job here (the scheduler's unit)."""
        with self.lock:
            return len({topic_key(j) for j in self.by_id.values()})

    def busy(self) -> bool:
        with self.lock:
            return bool(self.by_id or self.finishing)

    def cancel_all(self) -> None:
        with self.lock:
            for job in self.by_id.values():
                job.cancel_event.set()


class Runtime:
    """What every Harness, controller and worker of one Bridge process shares.

    - ``jobs``: the JobRegistry; ``session_lock`` serializes session-pointer updates.
    - ``shutdown``: set once when the process stops; every loop waits on it.
    - ``log``: one line to the Bridge log; tests replace ``log_sink``.
    - ``owner_open_id``: the Bridge's owner (paired or configured). ``access`` (app/access.py)
      decides who else is served: members of the groups the owner activated.
    - ``state_dir``: the Bridge's state directory, once its context is built (None in a bare app).
    - ``job_ended``: callables told each job that leaves the registry (``unregister_reply_job``): how
      代答 learns at once that a job of one of its topics ended (app/proxy.py ``job_ended``).
    - ``card_queues``: every controller's CardActionQueue (app/card_queue.py), so a clean stop
      runs the clicks they accepted (CardLanes.drain).
    - ``script_path`` / ``cli_argv``: how this process was started; a task runner rebuilds
      the Bridge from them. ``runner``: inside a task runner, its link to the Bridge
      (app.detached.RunnerLink); ``in_runner`` says whether there is one.
    - ``adopted_keys``: jobs whose live task runner survived a restart.
    - ``connection_health`` / ``availability_health``: the latest samples of the
      connection and model-access watchers, reported by the health endpoint.
    - ``sources``: the owner's messages read back for a recall or an edit (core/source_book.py, app/source_watch.py).
    """

    def __init__(self, *, script_path: str | Path, log_sink=print_log) -> None:
        self.script_path = Path(script_path)
        self.cli_argv: list[str] = []
        self.state_dir: Path | None = None
        self.runner = None
        self.shutdown = threading.Event()
        self.jobs = JobRegistry()
        self.session_lock = threading.Lock()
        self.log_sink = log_sink
        self.owner_open_id: str | None = None
        self.access = Access(self)
        self.adopted_keys: frozenset | set = frozenset()
        self.connection_health: dict = {}
        self.availability_health: dict = {}
        self.job_ended: list = []
        self.card_queues: list = []
        self.sources = SourceBook()

    @property
    def in_runner(self) -> bool:
        return self.runner is not None

    def log(self, message: str) -> None:
        self.log_sink(message)

    def detached(self) -> bool:
        """Jobs run in their own task runner process: the live Bridge, not a runner or a test app."""
        return bool(self.cli_argv) and not self.in_runner

    def enqueue_reply_job(self, job, harness: str) -> None:
        """Queue ``job`` for the scheduler as a job of ``harness``. The live Bridge saves it first,
        so a restart while it waits queues it again; a task runner never starts jobs."""
        if self.in_runner:
            raise RuntimeError("任务进程不新建任务")
        if not job.harness_id:
            job.harness_id = harness
        if self.detached() and job.detached_run_dir is None:
            # Before the queue sees it: a restart while it waits queues it again.
            detached.remember_queued(self.state_dir, harness, job)
        self.jobs.enqueue(job)
        timing.add("jobs", f"#{job.job_id}")  # The message or click that queued it names it (core/timing.py).
        timing.mark("queued")

    def unregister_reply_job(self, job) -> None:
        """``job`` ended or never ran: it leaves the registry and its saved waiting copy goes."""
        self.jobs.unregister(job)
        if job.message_id and self.state_dir is not None:
            detached.forget_queued(self.state_dir, job.message_id)
        for listener in self.job_ended:
            try:
                listener(job)
            except Exception as exc:  # noqa: BLE001 - a listener that fails leaves the job ended all the same
                self.log(f"job end listener failed job=#{job.job_id}: {type(exc).__name__}: {str(exc)[:200]}")
