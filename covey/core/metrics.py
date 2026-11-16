"""Content-free job timings. Missing stages remain unknown, never zero latency."""
from __future__ import annotations

import math
import time

STAGES = ('received', 'submitted', 'queued', 'dispatched', 'backend_requested',
          'backend_started', 'first_output', 'execution_finished', 'delivery_started', 'delivered')
SPANS = {'selection_ms': ('received', 'submitted'), 'submission_ms': ('submitted', 'queued'),
         'queue_ms': ('queued', 'dispatched'), 'prepare_ms': ('dispatched', 'backend_requested'),
         'first_output_ms': ('backend_requested', 'first_output'),
         'execution_ms': ('backend_requested', 'execution_finished'),
         'delivery_ms': ('delivery_started', 'delivered'), 'total_ms': ('received', 'delivered')}
# Where a turn's run starts (``run_start``): the first of these stages that is known. Its 用时 is from there to the
# end of its execution (``run_seconds``), the same span as SPANS["execution_ms"]: one figure on every card (C8,
# 2026-11-08: the task card, the answer card, the relay, the status card and Pin, the butler's one-minute rule).
RUN_START = ('backend_requested', 'backend_started', 'dispatched', 'queued')


def mark(job, stage, when=None):
    if stage not in STAGES:
        raise ValueError('unknown timing stage')
    if not hasattr(job, 'timings'):
        job.timings = {}
    job.timings.setdefault(stage, time.time() if when is None else when)


def finite(value):
    """A finite int or float, negative too (not a bool); core/usage.number also wants it >= 0."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def run_start(timings):
    """When the turn of ``timings`` began to run: the first stage of RUN_START it knows; None before."""
    timings = timings if isinstance(timings, dict) else {}
    return next((float(timings[stage]) for stage in RUN_START if finite(timings.get(stage))), None)


def run_seconds(timings, snapshot=None, now=None):
    """The turn's 用时 in seconds: from ``run_start`` to the end of its execution (``execution_finished``); a turn
    without those timings (an older record) from its process snapshot's ``started_at`` (core/trace.py). Without an
    end, the snapshot's ``ended_at``, else ``now`` (the time now): how long it has run so far. None when no start is
    known."""
    timings = timings if isinstance(timings, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    begun = run_start(timings)
    if begun is None and finite(snapshot.get('started_at')):
        begun = float(snapshot['started_at'])
    if begun is None:
        return None
    end = next((float(value) for value in (timings.get('execution_finished'), snapshot.get('ended_at')) if finite(value)),
               None)
    if end is None:
        end = time.time() if now is None else now
    return max(0.0, end - begun)


def took_text(seconds):
    """How every card writes a turn's 用时 and 已运行, without spaces (a third of a topic's side panel wrapped
    "1 分 20 秒", 2026-11-04): 45秒, 1分05秒, 2小时3分."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes // 60}小时{minutes % 60}分" if minutes >= 60 else f"{minutes}分{seconds:02d}秒"


def durations(timings):
    result = {}
    for name, (start, end) in SPANS.items():
        a, b = timings.get(start), timings.get(end)
        if finite(a) and finite(b) and b >= a:
            result[name] = round((b - a) * 1000, 2)
    return result



def timings_of(record) -> dict:
    """A job record's ``timings`` (core/jobs.py), {} when it has none or they are not a dict."""
    timings = record.get("timings")
    return timings if isinstance(timings, dict) else {}


def job_took(timings, snapshot=None, *, gate=None, fallback=None):
    """A run's 用时 as a card says it: ``run_seconds`` of ``timings`` and its process ``snapshot`` when ``gate``
    (called with them; None: always) lets it, else, or when that knows no start, ``fallback``. The answer card's
    has no gate (app/answer_cards.py took); a task that asked has ``ended`` and its monitor's elapsed seconds
    (app/recovery_cards.py asked_took)."""
    if gate is None or gate(timings, snapshot):
        seconds = run_seconds(timings, snapshot)
        if seconds is not None:
            return seconds
    return fallback


def ended(timings, snapshot):
    """A ``job_took`` gate: the run's end is known, by its process snapshot's ``ended_at`` or its
    ``execution_finished`` timing."""
    return bool(snapshot and isinstance(snapshot.get("ended_at"), (int, float))
                or isinstance(timings.get("execution_finished"), (int, float)))


def mark_first_output(job) -> bool:
    """``mark`` the backend's first output of ``job`` (any output marks it, the first time only); whether this was
    it, for what is said once (app/worker.py's record line, app/interaction.py's record write)."""
    first = 'first_output' not in job.timings
    mark(job, 'first_output')
    return first


def duration(seconds):
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"


def step_age(seconds):
    """How long the current step has run, to the minute: "不到 1 分钟", "40 分钟", "1 小时 5 分钟"."""
    return "不到 1 分钟" if seconds < 60 else duration(seconds)


def clock_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes // 60} 小时 {minutes % 60} 分" if minutes >= 60 else f"{minutes} 分 {seconds:02d} 秒"
