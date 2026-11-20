"""The bot's four events over one lark_oapi WebSocket (``COVEY_EVENT_TRANSPORT=sdk``) instead of four lark-cli consumers.

Off by default: feishu/events.py ``consume`` asks ``enabled()`` and only then comes here, and nothing in this module
imports ``lark_oapi`` before the connection thread starts. One app has one event connection, so with the switch on
no lark-cli consumer runs at all (lark-cli refuses to start while this one is connected).

The SDK hands each event over as the raw v2 envelope; the converters below turn it into the dict lark-cli's ``--jq``
of the same event key prints (feishu/events.py ``*_JQ``), so the handlers cannot tell the two transports apart. A
callback (``card.action.trigger``) is answered with a toast in the same frame, once the click is queued: lark-cli
always answers ``{"code":200}`` itself. Events are queued per key and handled on that key's own consumer thread,
as with lark-cli, so a slow message never delays the connection's pings or a click's toast.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from covey.core import env as cenv
from covey.core import timing
from covey.feishu import events
from covey.feishu.events import (CARD_ACTION_EVENT_KEY, MENU_EVENT_KEY, MESSAGE_EVENT_KEY, READ_EVENT_KEY,
                                 REPLY_CONTEXT, SUBSCRIPTIONS, consumer, format_event_stderr)

TRANSPORT_SWITCH = 'COVEY_EVENT_TRANSPORT'  # ``sdk``; empty or ``cli``: lark-cli's event consumers.
TOAST_TEXT = '收到'
TOAST = {'toast': {'type': 'info', 'content': TOAST_TEXT}}  # What the click's callback answers.
DEDUP_LIMIT = 2048  # Event IDs remembered: an SDK reconnect or Feishu's redelivery repeats a recent one.
QUEUE_LIMIT = 256  # Events waiting for one key's consumer thread.
CONNECT_RETRY_SECONDS = 20  # Ceiling of the wait before the connection thread starts the client again.
POLL_SECONDS = 1.0  # How often the watcher reads whether the client is connected.


def enabled() -> bool:
    return (cenv.get(TRANSPORT_SWITCH) or '').strip().lower() == 'sdk'


# ---- the converters: v2 envelope in, the dict of the key's JQ out ----

def _dig(node: Any, *path: str) -> Any:
    for name in path:
        node = node.get(name) if isinstance(node, dict) else None
    return node


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _stamp(value: Any) -> str | None:
    """A timestamp as lark-cli prints it: a string (Feishu's are strings already; a number becomes one)."""
    if isinstance(value, bool) or value in (None, ''):
        return None
    return str(value)


def _compact_mentions(mentions: Any) -> list[dict[str, str]]:
    """The message's mentions as lark-cli's compact form (``id`` an open_id, ``key``, ``name``)."""
    result = []
    for mention in mentions if isinstance(mentions, list) else []:
        if not isinstance(mention, dict):
            continue
        ident = mention.get('id')
        if isinstance(ident, dict):
            ident = ident.get('open_id') or ident.get('user_id') or ident.get('union_id')
        result.append({'id': ident or '', 'key': str(mention.get('key') or ''), 'name': str(mention.get('name') or '')})
    return result


def _plain_text(message_type: str, content: Any, mentions: list[dict[str, str]]) -> str:
    """A text message's content as readable text (its words, mention placeholders named); the content of any
    other type stays the message API's JSON, which feishu/message_parts.py reads as it reads lark-cli's rendering."""
    content = content if isinstance(content, str) else ''
    if message_type != 'text':
        return content
    try:
        body = json.loads(content)
    except ValueError:
        return content
    words = body.get('text') if isinstance(body, dict) else None
    if not isinstance(words, str):
        return content
    named = {m['key']: m['name'].lstrip('@') for m in mentions if m['key'] and m['name']}
    if named:
        pattern = '|'.join(re.escape(key) for key in sorted(named, key=len, reverse=True))
        words = re.sub(pattern, lambda found: '@' + named[found.group(0)], words)
    return words


def message_event(raw: dict[str, Any]) -> dict[str, Any]:
    """``im.message.receive_v1``: the fields of events.MESSAGE_EVENT_JQ. ``reply_to`` is the message's parent
    (lark-cli's name for it; ``parent_id`` stays empty as there), ``update_time`` is set only when it differs from
    ``create_time``, ``sender_type`` ``app`` reads ``bot``."""
    header, message = raw.get('header') or {}, _dig(raw, 'event', 'message') or {}
    sender = _dig(raw, 'event', 'sender') or {}
    mentions = _compact_mentions(message.get('mentions'))
    message_type = _text(message.get('message_type'))
    created, updated = _stamp(message.get('create_time')), _stamp(message.get('update_time'))
    sender_type = _text(sender.get('sender_type'))
    return {
        'chat_id': _text(message.get('chat_id')), 'chat_type': _text(message.get('chat_type')),
        'sender_id': _text(_dig(sender, 'sender_id', 'open_id')),
        'sender_type': 'bot' if sender_type == 'app' else sender_type,
        'message_type': message_type, 'content': _plain_text(message_type or '', message.get('content'), mentions),
        'message_id': _text(message.get('message_id')), 'id': _text(message.get('message_id')),
        'root_id': _text(message.get('root_id')), 'parent_id': None, 'reply_to': _text(message.get('parent_id')),
        'thread_id': _text(message.get('thread_id')), 'create_time': created,
        'update_time': updated if updated and updated != created else None,
        'event_id': _text(header.get('event_id')), 'timestamp': _stamp(header.get('create_time')),
        'type': MESSAGE_EVENT_KEY, 'mentions': mentions, 'user_agent': _text(message.get('user_agent')),
    }


def _json_string(value: Any) -> str | None:
    """A click's value or form as lark-cli prints it: the JSON string of an object, nothing for an empty one."""
    if value in (None, '', {}, []):
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def card_action_event(raw: dict[str, Any]) -> dict[str, Any]:
    """``card.action.trigger``: the fields of events.CARD_ACTION_EVENT_JQ. ``action_value`` and ``form_value`` are
    JSON strings (app/card_actions.py reads either); ``formValue`` is the old client's name and stays empty."""
    header, event = raw.get('header') or {}, raw.get('event') or {}
    action, context = event.get('action') or {}, event.get('context') or {}
    return {
        'event_id': _text(header.get('event_id')), 'operator_id': _text(_dig(event, 'operator', 'open_id')),
        'message_id': _text(context.get('open_message_id')), 'chat_id': _text(context.get('open_chat_id')),
        'token': _text(event.get('token')), 'action_tag': _text(action.get('tag')),
        'action_value': _json_string(action.get('value')), 'action_name': _text(action.get('name')),
        'form_value': _json_string(action.get('form_value')), 'formValue': None,
        'option': _text(action.get('option')), 'input_value': _text(action.get('input_value')),
        'timestamp': _stamp(header.get('create_time')),
    }


def menu_event(raw: dict[str, Any]) -> dict[str, Any]:
    """``application.bot.menu_v6``: the fields of events.MENU_EVENT_JQ. ``timestamp`` is the event's delivery time,
    ``menu_timestamp`` the click's, both millisecond strings."""
    header, event = raw.get('header') or {}, raw.get('event') or {}
    operator = _text(_dig(event, 'operator', 'operator_id', 'open_id'))
    return {'event_id': _text(header.get('event_id')), 'event_key': _text(event.get('event_key')),
            'operator_id': operator, 'operator_open_id': operator, 'timestamp': _stamp(header.get('create_time')),
            'menu_timestamp': _stamp(event.get('timestamp')), 'app_id': _text(header.get('app_id'))}


def read_event(raw: dict[str, Any]) -> dict[str, Any]:
    """``im.message.message_read_v1``: the fields of events.READ_EVENT_JQ, straight from the envelope."""
    reader = _dig(raw, 'event', 'reader') or {}
    ids = _dig(raw, 'event', 'message_id_list')
    return {'event_id': _text(_dig(raw, 'header', 'event_id')), 'reader_id': _text(_dig(reader, 'reader_id', 'open_id')),
            'read_time': _stamp(reader.get('read_time')), 'message_ids': ids if isinstance(ids, list) else None}


CONVERTERS = {MESSAGE_EVENT_KEY: message_event, CARD_ACTION_EVENT_KEY: card_action_event,
              MENU_EVENT_KEY: menu_event, READ_EVENT_KEY: read_event}


# ---- the connection ----

class _Link:
    """Stands in for the ``Popen`` of a lark-cli consumer in events.ConsumerHealth: ``pid`` is this process,
    ``poll()`` is None while the connection is up (health's ``alive``)."""

    def __init__(self):
        self.pid = os.getpid()
        self.up = False

    def poll(self):
        return None if self.up else 1


class Intake:
    """The one WebSocket client, the per-key queues and the ready state of each consumer."""

    def __init__(self, ctx, shutdown: threading.Event, log, retry_seconds):
        self.ctx, self.shutdown, self.log = ctx, shutdown, log
        self.retry = min(retry_seconds, CONNECT_RETRY_SECONDS)
        self.lock = threading.Lock()
        self.queues = {key: queue.Queue(QUEUE_LIMIT) for key in SUBSCRIPTIONS}
        self.attached: set[str] = set()
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.link = _Link()
        self.connected = False
        self.started = False
        self.client = None
        self.loop = None

    # -- events in --

    def first_time(self, event_id: str | None) -> bool:
        """False for an event ID seen within the last DEDUP_LIMIT events."""
        if not event_id:
            return True
        with self.lock:
            if event_id in self.seen:
                self.seen.move_to_end(event_id)
                return False
            self.seen[event_id] = None
            while len(self.seen) > DEDUP_LIMIT:
                self.seen.popitem(last=False)
        return True

    def on_payload(self, payload: bytes) -> dict[str, Any] | None:
        """What the SDK calls (on its loop thread) for an event frame: queue the event, answer a click with its
        toast. Quick on purpose: the callback's answer is due within 3 s, and this thread also runs the pings."""
        try:
            raw = json.loads(payload)
            key = _dig(raw, 'header', 'event_type')
            convert = CONVERTERS.get(key)
            if convert is None:
                return None
            event = convert(raw)
        except (ValueError, TypeError, AttributeError) as exc:
            self.log(f'event websocket: unreadable event ({type(exc).__name__})')
            return None
        if self.first_time(event.get('event_id')):
            try:
                self.queues[key].put_nowait(event)
            except queue.Full:
                self.log(f'event websocket: {key} queue full; event dropped')
                return None
        return TOAST if key == CARD_ACTION_EVENT_KEY else None

    # -- consumers --

    def consume(self, key: str, handle) -> None:
        """Handle the key's events on this thread until shutdown: events.consume's job with lark-cli."""
        sub, health = SUBSCRIPTIONS[key], consumer(key)
        marker = {REPLY_CONTEXT: True} if key == MESSAGE_EVENT_KEY else {}  # The event names type, root, parent, mentions.
        health.starting(self.link)
        with self.lock:
            self.attached.add(key)
            up = self.connected
        if up:
            self._ready(key)
        self._start()
        pending = self.queues[key]
        while not self.shutdown.is_set():
            try:
                event = pending.get(timeout=.5)
            except queue.Empty:
                continue
            health.begin()
            timing.arrived()  # A message's or click's flow counts from here (core/timing.py).
            try:
                handle({**event, **marker}, self.ctx)
            except Exception as exc:  # noqa: BLE001
                self.log(f'{sub.handler} error: {exc}')
            finally:
                timing.take_arrival()
                health.end()

    def _ready(self, key: str) -> None:
        line = f'[event] ready event_key={key}'  # The line deploy_bridge.sh counts and ConsumerHealth.stderr reads.
        consumer(key).stderr(line)
        self.log(format_event_stderr(line))

    def _set_connected(self, up: bool) -> None:
        with self.lock:
            if self.connected == up:
                return
            self.connected = up
            self.link.up = up
            keys = sorted(self.attached)
        for key in keys:
            if up:
                self._ready(key)
            else:
                consumer(key).failed('disconnected')
        if not up:
            self.log('event websocket disconnected; the SDK reconnects')

    # -- the client --

    def _start(self) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self._run, name='event-websocket', daemon=True).start()

    def _run(self) -> None:
        """Connect and run the SDK client until shutdown, again after each failure."""
        while not self.shutdown.is_set():
            over = threading.Event()
            try:
                self._run_client(over)
            except Exception as exc:  # noqa: BLE001
                if self.shutdown.is_set():
                    break
                self.log(f'event websocket stopped: {type(exc).__name__}; retrying in {self.retry:g}s')
            finally:
                over.set()
                self._set_connected(False)
            self.shutdown.wait(self.retry)

    def _run_client(self, over: threading.Event) -> None:
        # lark_oapi is imported here, on this thread: its ws module makes the asyncio loop that start() runs on.
        from covey.feishu import sdk
        from lark_oapi.core.enum import LogLevel
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.ws import client as ws_client
        creds = sdk.client(self.ctx.profile)  # In memory only; never logged or written.
        handler = EventDispatcherHandler.builder('', '').build()
        handler._do_without_validation = self.on_payload  # The raw envelope: no typed models, no validation.
        client = ws_client.Client(creds.config.app_id, creds.config.app_secret, log_level=LogLevel.ERROR,
                                  event_handler=handler, domain=creds.config.domain)
        self.client, self.loop = client, ws_client.loop
        threading.Thread(target=self._watch, args=(client, over), name='event-websocket-watch', daemon=True).start()
        try:
            client.start()  # Blocks: the SDK reconnects by itself; ends when stop() stops its loop.
        except RuntimeError:
            if not self.shutdown.is_set():
                raise

    def _watch(self, client, over: threading.Event) -> None:
        """Read whether the client holds a connection (lark-oapi 1.7.4 keeps it in ``_conn``)."""
        while not over.wait(POLL_SECONDS) and not self.shutdown.is_set():
            self._set_connected(getattr(client, '_conn', None) is not None)

    def stop(self) -> None:
        """Close the connection and stop the SDK's loop; called when the Bridge stops."""
        client, loop = self.client, self.loop
        if client is None or loop is None:
            return
        client._auto_reconnect = False
        try:
            import asyncio
            asyncio.run_coroutine_threadsafe(client._disconnect(), loop).result(2)
        except Exception:  # noqa: BLE001 - the process is ending
            pass
        loop.call_soon_threadsafe(loop.stop)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {'status': 'receiving' if self.connected else ('disconnected' if self.started else 'starting'),
                    'transport': 'sdk', 'scope': 'SDK WebSocket connection state'}


_intake: Intake | None = None
_intake_lock = threading.Lock()


def intake(ctx, shutdown, log, retry_seconds) -> Intake:
    global _intake
    with _intake_lock:
        if _intake is None:
            _intake = Intake(ctx, shutdown, log, retry_seconds)
        return _intake


def consume(ctx, key, *, handle, shutdown, log, retry_seconds) -> None:
    """events.consume's SDK side: handle(event, ctx) for each event of ``key`` until shutdown."""
    intake(ctx, shutdown, log, retry_seconds).consume(key, handle)


def stop() -> None:
    """Close the connection, if this process opened one."""
    if _intake is not None:
        _intake.stop()


def watch_status(runtime) -> None:
    """The connections thread in SDK mode (feishu/connections.py): the SDK connection's state for health."""
    while not runtime.shutdown.is_set():
        current = _intake
        sample = current.status() if current is not None else {'status': 'starting', 'transport': 'sdk'}
        runtime.connection_health = {**sample, 'checked_at': time.time()}
        runtime.shutdown.wait(30)


def reset() -> None:
    """Forget the connection (tests)."""
    global _intake
    with _intake_lock:
        _intake = None
    events._consumers.clear()
