"""Bounded concurrency across topics; FIFO within each topic."""

from __future__ import annotations

import queue
import threading
import time
from covey.core.metrics import mark

MAX_WORKERS = 10


def topic_key(job):
    return (job.chat_id, job.topic_id)


class WorkspaceScheduler:
    def __init__(self, shutdown, incoming, process, log, max_workers=MAX_WORKERS):
        self.shutdown, self.incoming = shutdown, incoming
        self.process, self.log = process, log
        self.max_workers = max(1, min(MAX_WORKERS, max_workers))
        self.active = {}
        self.lock = threading.Lock()
        self.pending = []

    def eligible(self, index):
        job = self.pending[index]
        key = topic_key(job)
        if (getattr(job, "start_held_until", 0) or 0) > time.time():
            return False  # Held for the owner's words (app/butler.py); its topic's later jobs wait behind it.
        return key not in self.active.values() and not any(topic_key(j) == key for j in self.pending[:index])

    def execute(self, job):
        try:
            mark(job, 'dispatched')
            self.process(job)
        except Exception as exc:  # noqa: BLE001 - keep other workspaces running.
            self.log(f"workspace job failed: {exc}")
        finally:
            with self.lock:
                self.active.pop(job.message_id, None)
            self.incoming.task_done()

    def run(self):
        threads = []
        while not self.shutdown.is_set():
            try:
                job = self.incoming.get(timeout=0.1)
                self.pending.append(job)
            except queue.Empty:
                pass
            cancelled = [job for job in self.pending if getattr(job, "cancelled_before_start", False)]
            for job in cancelled:
                self.pending.remove(job)
                self.incoming.task_done()
            with self.lock:
                for index in range(len(self.pending)):
                    if len(self.active) >= self.max_workers:
                        break
                    if self.eligible(index):
                        job = self.pending.pop(index)
                        self.active[job.message_id] = topic_key(job)
                        thread = threading.Thread(target=self.execute, args=(job,), daemon=True)
                        threads = [t for t in threads if t.is_alive()]
                        threads.append(thread)
                        thread.start()
                        break
        for job in self.pending:
            job.cancel_event.set()
        for thread in threads:
            thread.join(timeout=12)
