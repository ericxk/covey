"""A bounded queue of card clicks and the worker threads that run them."""
from __future__ import annotations

import queue
import threading
import time

from covey.core import timing


class CardActionQueue:
    """Hand a card click to a few worker threads, started by the first one.

    The callback consumer only queues the click: directory reads can wait on the
    NAS and card updates on CardKit, while Stop, messages and other cards keep
    arriving. A full queue drops the click with a log line; the controller that
    queued it decides what the dropped click leaves behind. Each controller has
    its own queue, so a slow one never holds up another.

    A queued click was acknowledged: a clean stop lets the workers run what is
    queued (CardLanes.drain waits for it through Runtime.card_queues, within its
    deadline) and never runs one twice.
    """
    LIMIT = 32

    def __init__(self, topics, *, name, workers, label):
        self.topics = topics
        self.queue = queue.Queue(maxsize=self.LIMIT)
        self.start_lock = threading.Lock()
        self.started = False
        self.running = 0  # Workers not yet gone; the last one to leave at a stop clears ``started``.
        self.name, self.workers, self.label = name, workers, label
        topics.runtime.card_queues.append(self)

    def submit(self, busy, handler, *args):
        """Run handler(*args) on a worker; False, with `<label> busy; <busy>` logged, when the queue is full."""
        work = timing.carry(handler, 'worker')  # The message or click that queued it waits for it.
        with self.start_lock:
            # The first click; or, during a stop, one queued after the workers had emptied the queue and gone.
            if not self.started:
                self.started, self.running = True, self.workers
                for _ in range(self.workers):
                    threading.Thread(target=self._work, name=self.name, daemon=True).start()
            try:
                self.queue.put_nowait((work, *args))
            except queue.Full:
                full = True
            else:
                full = False
        if full:
            timing.drop(work)
            self.topics.runtime.log(f'{self.label} busy; {busy}')
            return False
        return True

    def _work(self):
        while True:
            try:
                handler, *args = self.queue.get(timeout=.5)
            except queue.Empty:
                with self.start_lock:  # Under the lock submit puts with: nothing queued is left without a worker.
                    if self.topics.runtime.shutdown.is_set() and self.queue.empty():
                        self.running -= 1
                        self.started = self.running > 0
                        return
                continue
            try:
                handler(*args)
            except Exception as exc:
                self.topics.runtime.log(f'{self.label} failed: {type(exc).__name__}')
            finally:
                self.queue.task_done()

    def wait_queued(self, deadline, timeout):
        """On a clean stop: wait until ``deadline`` (time.monotonic) for the queued clicks to have run;
        False, with a line logged, when some were left."""
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks and deadline > time.monotonic():
                self.queue.all_tasks_done.wait(deadline - time.monotonic())
            left = self.queue.unfinished_tasks
        if left:
            self.topics.runtime.log(f'{self.label} clicks not finished at stop: {left} left after {timeout} s')
        return not left
