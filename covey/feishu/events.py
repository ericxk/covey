#!/usr/bin/env python3
"""Shared Lark event consumer helpers for Feishu bridge scripts."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from covey.core import timing
from covey.core.process import stop_process
from covey.feishu.transport import lark_argv, run_cli


LogFn = Callable[[str], None]
EventHandler = Callable[[dict[str, Any]], None]
ProcessSetter = Callable[[subprocess.Popen[str] | None], None]
DelayFn = Callable[[int], int | float]
MESSAGE_EVENT_KEY = "im.message.receive_v1"
CARD_ACTION_EVENT_KEY = "card.action.trigger"
# The bot menu's clicks (app/bot_menu.py). Optional: a console without the subscription leaves health ready
# (app/health.py and service/doctor.py require only the two above).
MENU_EVENT_KEY = "application.bot.menu_v6"
# The owner read the bot's private chat messages (app/topic_seen.py). Consumed only with COVEY_PINNED_TOPICS=1;
# lark-cli asks the app for im:message as well as im:message:readonly before it subscribes.
READ_EVENT_KEY = "im.message.message_read_v1"


# Health of each event subscription, read by the Bridge health server.
class ConsumerHealth:
    def __init__(self, key):
        self.key = key
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.ready_at = None
        self.last_event = None
        self.handling_since = None
        self.events = 0
        self.restarts = 0
        self.error = None
        self.proc = None
        self.state = 'starting'
        self.unsubscribed = False  # lark-cli said the console lacks this subscription (a scan on the phone fixes it)
        self.durations = deque(maxlen=128)
        # Work handed to other threads (card lanes) reports its own time per token;
        # the consumer thread then only counts events and stops timing itself.
        self.running = {}
        self.inline_durations = True

    def starting(self, proc):
        with self.lock:
            self.proc, self.started = proc, time.monotonic()
            self.ready_at = None
            self.state, self.error = 'starting', None
            self.unsubscribed = False

    def stderr(self, text):
        with self.lock:
            if NOT_SUBSCRIBED in text:
                self.unsubscribed = True
            ready = text.split('event_key=', 1)[1].split() if text.startswith('[event] ready event_key=') else []
            if ready and ready[0] == self.key:
                self.ready_at = time.monotonic()
                self.state, self.error = 'ready', None
            # Never persist raw stderr, event content, identities or tokens.
            if text.startswith('{'):
                try:
                    error = json.loads(text).get('error')
                    if isinstance(error, dict):
                        kind = error.get('type')
                        self.error = kind if kind in {'auth', 'permission', 'network', 'validation', 'api', 'internal'} else 'cli_error'
                        self.state = 'degraded'
                except (ValueError, AttributeError):
                    pass

    def begin(self, token=None):
        with self.lock:
            if token is not None:
                self.running[token] = time.monotonic()
                return
            self.last_event = self.handling_since = time.monotonic()
            self.events += 1

    def end(self, token=None):
        with self.lock:
            start = self.running.pop(token, None) if token is not None else self.handling_since
            if start is not None and (token is not None or self.inline_durations):
                self.durations.append((time.monotonic() - start) * 1000)
            if token is None:
                self.handling_since = None

    def failed(self, reason):
        with self.lock:
            self.state, self.error = 'retrying', reason
            self.restarts += 1

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            alive = self.proc is not None and self.proc.poll() is None
            starts = [s for s in (self.handling_since, *self.running.values()) if s is not None]
            elapsed = now - min(starts) if starts else 0
            values = sorted(self.durations)
            return {'state': self.state, 'pid': self.proc.pid if self.proc else None,
                    'alive': alive, 'ready': alive and self.state == 'ready',
                    'starting_seconds': round(now - self.started, 2),
                    'handling_seconds': round(elapsed, 2), 'events': self.events,
                    'last_event_age_seconds': round(now - self.last_event, 2) if self.last_event else None,
                    'restarts': self.restarts, 'error': self.error,
                    'handler_ms': {'samples': len(values),
                                   'p50': round(values[len(values) // 2], 2) if values else None,
                                   'p95': round(values[min(len(values) - 1, int(len(values) * .95))], 2) if values else None}}


# What lark-cli's consume says when the console lacks the event or callback ("... not subscribed in console: <key>").
NOT_SUBSCRIBED = 'not subscribed in console'
UNSUBSCRIBED_RETRY_SECONDS = 20  # Ceiling of the wait before asking again then: a scan on the phone is picked up soon.

_consumers = {}
_lock = threading.Lock()


def consumer(key):
    with _lock:
        return _consumers.setdefault(key, ConsumerHealth(key))


def consumers_snapshot():
    with _lock:
        entries = list(_consumers.items())
    return {key: value.snapshot() for key, value in entries}
MESSAGE_EVENT_JQ = (
    "{chat_id, chat_type, sender_id, sender_type, message_type, content, message_id, id, "
    "root_id, parent_id, reply_to, thread_id, create_time, update_time, event_id, timestamp, type, mentions, user_agent}"
)
# Set by the consumer on a message event whose lark-cli names a reply's parent and root itself;
# only then may a plain message skip its own detail (feishu/media.py). The jq above lets no
# event carry it.
REPLY_CONTEXT = "reply_context_in_event"


def with_parent(event: dict) -> dict:
    """``event`` with the message it quotes as ``parent_id``: lark-cli and the WS intake (feishu/ws_intake.py message_event)
    name it ``reply_to`` and leave ``parent_id`` empty, and every route reads ``parent_id`` (core/topics.py, app/butler.py
    app_for: 引用直达). The same event when it names none or has one already; a copy otherwise (W0-3)."""
    if event.get("parent_id") or not event.get("reply_to"):
        return event
    return {**event, "parent_id": event["reply_to"]}
# ``timestamp``: when Feishu delivered the click (epoch microseconds), for its delay before it reached us (core/timing.py).
CARD_ACTION_EVENT_JQ = "{event_id, operator_id, message_id, chat_id, token, action_tag, action_value, action_name, form_value, formValue, option, input_value, timestamp}"
# A menu click names no chat: only who clicked which item.
MENU_EVENT_JQ = "{event_id, event_key, operator_id, operator_open_id, timestamp, menu_timestamp, app_id}"
# A read receipt names no chat either: who read (open_id), when (epoch milliseconds, a string) and which of the bot's
# messages. lark-cli passes it on in its V2 envelope.
READ_EVENT_JQ = ("{event_id: .header.event_id, reader_id: .event.reader.reader_id.open_id, "
                 "read_time: .event.reader.read_time, message_ids: .event.message_id_list}")


class Subscription(NamedTuple):
    """One event the bot consumes: the fields lark-cli prints and how the Bridge's log names its consumer."""
    jq: str
    label: str  # "starting <label>", "<label> exited rc=…; restarting in …s"
    handler: str  # "<handler> error: …" when handling an event raises
    stop_label: str  # "stopping <stop_label>" when the Bridge stops (unified.py stop_children)
    retry_failed: bool = True  # A failed exit (rc != 0) restarts after consume's retry_seconds, not 5 s.


# In the order the Bridge stops them.
SUBSCRIPTIONS = {
    MESSAGE_EVENT_KEY: Subscription(MESSAGE_EVENT_JQ, "event consumer", "handle_event", "message event consumer",
                                    retry_failed=False),
    CARD_ACTION_EVENT_KEY: Subscription(CARD_ACTION_EVENT_JQ, "card action event consumer", "handle_card_action_event",
                                        "card action event consumer"),
    MENU_EVENT_KEY: Subscription(MENU_EVENT_JQ, "bot menu event consumer", "handle_menu_event",
                                 "bot menu event consumer"),
    READ_EVENT_KEY: Subscription(READ_EVENT_JQ, "message read event consumer", "handle_read_event",
                                 "message read event consumer"),
}


def format_event_stderr(line: str) -> str | None:
    if not line:
        return None
    return line if line.startswith(("[event]", "[source]")) else f"[event] {line}"


def _format_delay(delay: int | float) -> str:
    return f"{delay:g}" if isinstance(delay, float) else str(delay)


def _pipe_stderr(proc: subprocess.Popen[str], log: LogFn, health: ConsumerHealth) -> None:
    assert proc.stderr is not None
    for line in proc.stderr:
        text = line.rstrip()
        health.stderr(text)
        text = format_event_stderr(text)
        if text is not None:
            log(text)


def _sleep_with_interrupts(
    delay: int | float,
    *,
    shutdown: threading.Event,
) -> None:
    deadline = time.time() + delay
    while time.time() < deadline and not shutdown.is_set():
        time.sleep(min(5, max(0.1, deadline - time.time())))


def _read_events(
    proc: subprocess.Popen[str],
    *,
    shutdown: threading.Event,
    health: ConsumerHealth,
    handle_event: EventHandler,
    log: LogFn,
    handler_error_label: str | None,
) -> None:
    """Hand each JSON object the consumer prints to handle_event, until EOF or shutdown."""
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        if shutdown.is_set():
            break
        line = raw_line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        health.begin()
        timing.arrived()  # A message's or click's flow counts from here (core/timing.py).
        try:
            handle_event(event)
        except Exception as exc:  # noqa: BLE001
            if not handler_error_label:
                raise
            log(f'{handler_error_label} error: {exc}')
        finally:
            timing.take_arrival()
            health.end()


def _close_consumer(
    proc: subprocess.Popen[str],
    finished: threading.Event,
    guard_thread: threading.Thread,
    stderr: threading.Thread,
    *,
    exit_label: str,
    log: LogFn,
    set_process: ProcessSetter,
) -> None:
    # EOF can precede process exit. Bound the wait and close pipes;
    # consumers must not leak after an exception or shutdown.
    stop_process(proc, exit_label, log)
    finished.set()
    guard_thread.join(timeout=1)
    stderr.join(timeout=1)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe and not pipe.closed:
            pipe.close()
    set_process(None)


def consume_event_stream(
    *,
    argv: Sequence[str],
    start_log: str,
    exit_label: str,
    shutdown: threading.Event,
    set_process: ProcessSetter,
    handle_event: EventHandler,
    log: LogFn,
    restart_delay: int | float | DelayFn = 5,
    handler_error_label: str | None = None,
    ready_timeout: float = 60,
) -> None:
    """Run a lark-cli event consumer until shutdown. Only lines that start with ``{`` can be events; its
    stderr is logged through format_event_stderr."""
    key = argv[argv.index('consume') + 1] if 'consume' in argv else exit_label
    health = consumer(key)
    while not shutdown.is_set():
        log(start_log)
        try:
            proc = subprocess.Popen(list(argv), text=True, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            health.failed('spawn_failed')
            log(f'{exit_label} failed to start: {type(exc).__name__}; retrying')
            _sleep_with_interrupts(5, shutdown=shutdown)
            continue
        set_process(proc)
        health.starting(proc)
        finished = threading.Event()

        def guard(proc=proc, finished=finished):
            # A quiet subscription is healthy. Only lack of initial readiness
            # triggers replacement, never absence of user messages or long jobs.
            while not finished.wait(.25):
                snapshot = health.snapshot()
                if shutdown.is_set():
                    stop_process(proc, exit_label, log)
                    return
                if snapshot['starting_seconds'] > ready_timeout and health.ready_at is None:
                    health.failed('ready_timeout')
                    stop_process(proc, exit_label, log)
                    return

        stderr = threading.Thread(target=_pipe_stderr, args=(proc, log, health), daemon=True)
        guard_thread = threading.Thread(target=guard, daemon=True)
        stderr.start()
        guard_thread.start()
        try:
            _read_events(proc, shutdown=shutdown, health=health, handle_event=handle_event, log=log,
                         handler_error_label=handler_error_label)
        finally:
            _close_consumer(proc, finished, guard_thread, stderr, exit_label=exit_label, log=log,
                            set_process=set_process)
        rc = proc.wait()
        if shutdown.is_set():
            break
        health.failed('consumer_exited')
        delay = restart_delay(rc) if callable(restart_delay) else restart_delay
        log(f"{exit_label} exited rc={rc}; restarting in {_format_delay(delay)}s")
        _sleep_with_interrupts(delay, shutdown=shutdown)


def names_reply_context(lark_argv: Callable[..., list[str]], profile: str | None) -> bool:
    """Whether this lark-cli's message events carry ``reply_to``, ``root_id`` and ``mentions``
    (lark-cli 1.0.69+). An older one leaves them out, and a reply then looks like any message;
    a lark-cli that cannot say counts as older."""
    try:
        proc = run_cli(lark_argv(profile, "event", "schema", MESSAGE_EVENT_KEY, "--json"), runner=subprocess.run,
                       text=True, capture_output=True, timeout=15, check=False)
        fields = json.loads(proc.stdout)["resolved_output_schema"]["properties"]
        return proc.returncode == 0 and {"reply_to", "root_id", "mentions"} <= set(fields)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return False


def consume(
    ctx: Any,
    key: str,
    *,
    handle: Callable[[dict[str, Any], Any], None],
    set_process: ProcessSetter,
    shutdown: threading.Event,
    log: LogFn,
    retry_seconds: int | float,
) -> None:
    """Run the bot's lark-cli consumer of one SUBSCRIPTIONS event until shutdown, calling handle(event, ctx)
    for each event. A message event carries REPLY_CONTEXT when this lark-cli names a reply's parent."""
    sub = SUBSCRIPTIONS[key]
    marker = {}
    if key == MESSAGE_EVENT_KEY:
        marker = {REPLY_CONTEXT: True} if names_reply_context(lark_argv, ctx.profile) else {}
        if not marker:
            log("lark-cli message events do not name a reply's parent (before 1.0.69): each message's detail is read")
    health = consumer(key)

    def delay(rc):
        if not rc:
            return 5
        # A missing console subscription is fixed by a scan, not by waiting: ask again soon (capped); other failures keep retry_seconds.
        return min(retry_seconds, UNSUBSCRIBED_RETRY_SECONDS) if health.unsubscribed else retry_seconds

    consume_event_stream(
        argv=lark_argv(ctx.profile, "event", "consume", key, "--as", "bot", "--jq", sub.jq),
        start_log=f"starting {sub.label}",
        exit_label=sub.label,
        shutdown=shutdown,
        set_process=set_process,
        handle_event=lambda event: handle({**event, **marker}, ctx),
        log=log,
        restart_delay=delay if sub.retry_failed else 5,
        handler_error_label=sub.handler,
    )
