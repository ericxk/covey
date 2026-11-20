"""Card clicks run in order within a topic and in parallel across topics.

One consumer thread reads every card click. Handling a click on that thread
made a slow one (开始任务 creates and sends a task card, 2.9–4.2 s; 提交回答
4.4 s; 分叉 3.5–5.2 s) hold every other card's next click. Each
click now goes to its topic's lane: a later click in the same topic still waits
for the earlier one (a model choice before 开始任务 on a launch card), clicks
in other topics do not. Clicks without a topic get a lane per card message.

A click is acknowledged when its handler returns (its ``card action`` log line),
and Feishu wants that within 3 s. What a handler leaves for after that
(``after_ack``: every new task card and notice a click sends — 提交回答, 开始任务,
检查并继续, 重新排队执行, 留作下一轮) runs in its topic's
follow-up lane: the topic's follow-ups in order, while the topic's next click
need not wait. A follow-up ``beside`` the others (开始任务's receipt on its launch card,
S36) runs in a lane of its own, at the same time as the task cards.

A clean stop (``drain``) lets the clicks being handled and the acknowledged clicks'
follow-ups finish, for at most DRAIN_SECONDS: the user was told an answer is being sent.
Clicks not yet started are left; they were never acknowledged. The clicks a
controller's CardActionQueue accepted (app/card_queue.py, Runtime.card_queues) run
within the same time.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import functools
import queue
import threading
import time
import uuid

from covey.app.card_actions import click_kind
from covey.core import timing

WORKERS = 4
LIMIT = 256  # Beyond this backlog a click is handled on the consumer thread, as before.
SLOW_WAIT_MS = 1000
DRAIN_SECONDS = 6  # Within the Manager's 15 s between SIGTERM and SIGKILL, with the rest of the stop.
_click = threading.local()  # On a thread handling a click: its follow-ups, and how they report a failure.


def after_ack(work, *, beside=False):
    """Run ``work()`` once the click being handled is acknowledged, in its topic's follow-up lane;
    ``beside``: in a second lane of the topic, so the follow-ups need not wait for it nor it for them
    (a write to a card none of them touches). Off a card lane (a typed answer, a test calling a
    handler) it runs now and its exceptions reach the caller, as they did before follow-ups existed."""
    pending = getattr(_click, "pending", None)
    if pending is None:
        work()
    else:
        pending.append((work, getattr(_click, "report", None), beside))


def clicked_message() -> str:
    """The message id of the card whose click this thread is handling (before its acknowledgement), '' elsewhere: a
    write to several cards puts the one the owner is looking at first."""
    return getattr(_click, "message", "") or ""


@contextmanager
def reporting(report):
    """Inside this block, a follow-up that fails calls ``report(exc)``: the reply its click's handler
    would have sent had the failure happened before the acknowledgement."""
    previous = getattr(_click, "report", None)
    _click.report = report
    try:
        yield
    finally:
        _click.report = previous


class CardLanes:
    def __init__(self, handle, lane_key, log, shutdown, health=None):
        self.handle, self.lane_key, self.log, self.shutdown, self.health = handle, lane_key, log, shutdown, health
        self.lock = threading.Lock()
        self.settled = threading.Condition(self.lock)
        self.lanes = {}  # lane -> deque of (work, queued_at, what); present while a worker owns it
        self.ready = queue.Queue()
        self.size = 0
        self.unsettled = 0  # Clicks being handled and follow-ups not yet run: what a clean stop waits for.
        self.started = False

    def submit(self, event, ctx):
        try:
            lane = self.lane_key(event, ctx)
        except Exception as exc:  # noqa: BLE001 - a lane is a scheduling hint, never a reason to drop a click
            self.log(f"card lane unavailable: {type(exc).__name__}")
            lane = ""
        arrival = timing.take_arrival()  # The click's flow counts from the consumer reading it.
        if not self._queue(lane, functools.partial(self.run, event, ctx, lane, arrival), "card action"):
            if lane:
                self.log(f"card lanes busy backlog={self.size}; handling on the consumer thread")
            self.run(event, ctx, lane, arrival)

    def _queue(self, lane, work, what):
        """Queue ``work`` at the end of ``lane``; False when it is to run on this thread instead
        (no lane, or a backlog beyond LIMIT)."""
        with self.lock:
            if not self.started:
                self.started = True
                for n in range(WORKERS):
                    threading.Thread(target=self._work, name=f"card-lane-{n}", daemon=True).start()
            if not lane or self.size >= LIMIT:
                return False
            self.size += 1
            if lane in self.lanes:
                self.lanes[lane].append((work, time.monotonic(), what))
            else:
                self.lanes[lane] = deque([(work, time.monotonic(), what)])
                self.ready.put(lane)
        return True

    def run(self, event, ctx, lane="", arrival=None):
        token = uuid.uuid4().hex
        self._unsettle(1)
        flow = None
        try:
            # One timing line per click (core/timing.py): from its arrival to its last follow-up.
            flow = timing.begin("click", started=arrival, kind=click_kind(event), event=str(event.get("event_id") or "")[-10:],
                                message=str(event.get("message_id") or "")[-10:])
            if flow is not None:
                flow.note(feishu_ms=timing.delay_ms(event.get("timestamp"), flow.wall))
                flow.mark("lane")
            if self.health is not None:
                self.health.begin(token)
            _click.pending, _click.report, _click.message = [], None, str(event.get("message_id") or "")
            try:
                self.handle(event, ctx)
            except Exception as exc:  # noqa: BLE001 - one click must not end a lane
                self.log(f"handle_card_action_event error: {exc}")
            finally:
                follow_ups, _click.pending, _click.message = _click.pending, None, ""
                timing.mark("ack")
                if self.health is not None:
                    self.health.end(token)
            label = (f"kind={click_kind(event)} "
                     f"event={str(event.get('event_id') or '')[-10:]}")
            for work, report, beside in follow_ups:
                name = "beside" if beside else "followup"
                follow_up = functools.partial(self._follow_up, timing.carry(work, name), report, label)
                self._unsettle(1)  # Before it is queued: a stop sees it from here until it has run.
                if not self._queue((name, lane) if lane else "", follow_up, "card follow-up"):
                    follow_up()
        finally:
            timing.end(flow)
            self._unsettle(-1)

    def _unsettle(self, change):
        with self.settled:
            self.unsettled += change
            if not self.unsettled:
                self.settled.notify_all()

    def drain(self, timeout=DRAIN_SECONDS, queues=()):
        """On a clean stop, once ``shutdown`` is set: wait up to ``timeout`` seconds for the clicks
        being handled, the follow-ups of acknowledged clicks and then the clicks ``queues``
        (CardActionQueue) hold. False when some were left."""
        deadline = time.monotonic() + timeout
        settled = True
        with self.settled:
            while self.unsettled:
                left = deadline - time.monotonic()
                if left <= 0:
                    self.log(f"card follow-ups not finished at stop: {self.unsettled} left after {timeout} s")
                    settled = False
                    break
                self.settled.wait(left)
        return all([actions.wait_queued(deadline, timeout) for actions in queues]) and settled

    def _follow_up(self, work, report, label):
        token, started = uuid.uuid4().hex, time.monotonic()
        if self.health is not None:
            self.health.begin(token)
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - the click is acknowledged: its follow-up reports instead
            self.log(f"card follow-up error: {type(exc).__name__}")
            if report is not None:
                try:
                    report(exc)
                except Exception as error:  # noqa: BLE001
                    self.log(f"card follow-up failure not reported: {type(error).__name__}")
        finally:
            if self.health is not None:
                self.health.end(token)
            self.log(f"card follow-up {label} elapsed_ms={(time.monotonic() - started) * 1000:.0f}")
            self._unsettle(-1)

    def _work(self):
        # Stopping, a worker stays while drain waits: follow-ups still run, new clicks do not start.
        while not self.shutdown.is_set() or self.unsettled:
            try:
                lane = self.ready.get(timeout=.5)
            except queue.Empty:
                continue
            while True:
                with self.lock:
                    items = self.lanes[lane]
                    if items and self.shutdown.is_set() and items[0][2] == "card action":
                        self.size -= len(items)  # Never acknowledged: not started while stopping.
                        items.clear()
                    if not items:
                        del self.lanes[lane]
                        break
                    work, queued_at, what = items[0]
                waited = (time.monotonic() - queued_at) * 1000
                if waited > SLOW_WAIT_MS:
                    behind = "clicks" if what == "card action" else "follow-ups"
                    self.log(f"{what} waited_ms={waited:.0f} behind earlier {behind} in its topic")
                work()
                with self.lock:
                    items.popleft()
                    self.size -= 1
