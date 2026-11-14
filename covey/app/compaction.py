"""The main thread's compaction requests: its next turn compacts its native session first.

A turn of the main thread that ends past its ``compact_tokens`` records a request in the topic's
``compact-next.json`` (app/main_thread.py compact_main_thread). The next turn, when it resumes the session, takes the
request (``turn_context``, in the task runner) and runs with ``compact_first``: a backend with the ``compaction``
capability compacts the session before the turn and shows it in 执行过程 (backends/claude/compaction.py). Until
2026-11-08 a topic's finished card had a 📦 压缩上下文 button that recorded the same request; a request one left is
still taken by its topic's next turn.

Since 2026-11-09 the main thread's own task runner marks its request ``idle`` (with the context's ``tokens`` and the
``threshold``): the Bridge compacts the session while the topic is idle (app/idle_compaction.py), and a turn leaves
such a request to it. A turn still takes one that failed there (``failed``), and one whose context reached FALLBACK
times the threshold, so the context never grows on while the idle compaction keeps being stopped.
"""
from __future__ import annotations

import dataclasses
import time

from covey.core.harness import backend_capabilities
from covey.core.jobs import upsert_job_record
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

FLAG = "compact-next.json"
FALLBACK = 1.5  # A context this many times the threshold compacts before the next turn, idle or not.


def left_for_idle(request) -> bool:
    """Whether a turn leaves ``request`` to the idle compaction: marked ``idle``, not ``failed`` there, and its
    context under FALLBACK times its threshold."""
    tokens, threshold = request.get("tokens"), request.get("threshold")
    if not request.get("idle") or request.get("failed"):
        return False
    return not (type(tokens) is int and type(threshold) is int and threshold > 0 and tokens >= FALLBACK * threshold)


class Compaction:
    """One Harness's compaction requests.

    - ``harness``: whether its backend can compact (``HarnessCapabilities.compaction``).
    - ``runtime``: the log.
    """

    def __init__(self, harness, *, runtime) -> None:
        self.harness = harness
        self.runtime = runtime

    def supported(self, ctx):
        return backend_capabilities(self.harness.backend, ctx).compaction

    @staticmethod
    def path(ctx):
        return ctx.history_path.parent / FLAG

    def request(self, ctx):
        """The waiting request ({at, job_id, click}), or None."""
        value = read_object(self.path(ctx))
        return value if value.get("at") else None

    def set_requested(self, ctx, job_id=0, **sizes):
        """Record the request; a repeated request keeps the first one's time. ``sizes``: the main thread's
        ``tokens``, ``threshold`` and ``idle`` (app/main_thread.py compact_main_thread), which a repeated request
        updates. The file's ``click`` (always "") is still written: a release before 2026-11-10 reads the same file."""
        path = self.path(ctx)
        with resource_lock("record", path.absolute()):
            current = self.request(ctx)
            if not current:
                write_json(path, {"at": time.time(), "job_id": job_id, "click": "", **sizes}, ensure_ascii=False)
            elif sizes and any(current.get(key) != value for key, value in sizes.items()):
                write_json(path, {**current, **sizes}, ensure_ascii=False)
        self.runtime.log(f"compaction requested topic={ctx.topic_id[:12]} job=#{job_id}")

    def turn_context(self, ctx, job, session_id):
        """The run's context: with ``compact_first`` when a request waits and the turn resumes a session,
        whose request it then takes. A fresh or forked session has nothing to compact and leaves it."""
        if not session_id or not ctx.topic_id or not self.supported(ctx) or ctx.options.get("resume_at"):
            return ctx  # A turn after ↩️ 回退 (app/rewind.py) leaves the request to the next.
        path = self.path(ctx)
        with resource_lock("record", path.absolute()):
            request = self.request(ctx)
            if request is None:
                return ctx
            if left_for_idle(request):
                self.runtime.log(f"compaction left for idle job=#{job.job_id} topic={ctx.topic_id[:12]}")
                return ctx
            path.unlink(missing_ok=True)
        upsert_job_record(ctx.state_dir, job.message_id, compaction_requested=request)
        self.runtime.log(f"compaction taken by job=#{job.job_id} topic={ctx.topic_id[:12]}")
        return dataclasses.replace(ctx, options={**ctx.options, "compact_first": True})
