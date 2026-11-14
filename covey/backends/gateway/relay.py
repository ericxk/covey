"""A local relay in front of a model gateway (core/gateways.py) for one Codex or Pi task, so its 429s are ridden out.

Acme answers 429 in two ways. Short of capacity (many users) it says
"业务申请资源不足，平台申请扩容" without a Retry-After header, and Codex fails a
429 without one at once. Over the key's request rate it says so with
x-ratelimit-limit 120, x-ratelimit-remaining 0 and Retry-After 1 (first seen
2026-11-02 11:03), and Codex spends one of its ten retries a second on it: #2304
failed twelve seconds after its first. The relay forwards every request and
response unchanged, except a 429: it holds Codex's request and sends the same
body upstream again itself, at once after a capacity 429 (Acme hands freed
capacity to whichever request asks next, so a request that waits between tries
only loses its turn) and after the Retry-After of a rate-limit 429 (at most
LIMIT_WAIT seconds; sooner is refused again). It keeps on until another answer
comes or the task ends: the owner chose no time limit (2026-11-02), Stop ends
the wait. A ``window`` in seconds still makes it give up. Tools are never run
again: only the model request is repeated, as Codex itself would repeat it.
A spent daily quota is a 403 ("quota exceeded: Daily Limits exceeded"), never
held.

Acme's upstream may also run out of capacity after it began a 200 event
stream: a Pi turn on gpt-6.1-sol (2026-11-04 00:49) got its response.created
and then a data line {"error": {..., "message": "Requests have exceeded the
throughput limit on your Provisioned-Managed deployment ..."}} (pi's OpenAI SDK
throws a data line's error as it is). Neither Codex nor pi retries that, so the
relay reads a stream's opening events before it answers: when the first event
past them (OPENING) carries a capacity error (CAPACITY_TEXTS), the stream is a
capacity 429 like any other; else the read bytes go out first, unchanged.

Why the relay retries instead of Codex (codex-cli 0.164.0, measured 2026-11-01
against a local stand-in upstream): Codex retries a 429 only under
stream_max_retries (at most 100) and keeps one count per request for 429s,
5xx and dropped streams together; a failure that carries no Retry-After waits
0.2 s × 2^count, so after 11 relayed 429s one dropped stream waited 414 s.
Codex itself never retries a 429 then: the relay holds the request until an
answer comes. Codex sets no timeout while it waits for response headers
(rust-v0.164.0: codex-client Provider.build_request has timeout None, the
reqwest client has none, and stream_idle_timeout starts once headers arrive;
a stand-in holding them 1260 s saw no hang-up or second request). With a
window, once it is over the 429 goes back without Retry-After and the turn
fails at once.

Runs of 429s are kept per Codex thread: a request carries its thread in the
``thread-id`` header, and a sub-agent's also ``x-openai-subagent: collab_spawn``
(codex-api requests/headers.rs), so a sub-agent's wait is told apart from the
main task's. ``notify(Busy)`` tells the card about a run once it has lasted
SHOW_AFTER seconds, then every NOTE_EVERY seconds while retrying, and when it
ends, recovered or given up, with the thread's totals for the turn. Acme's
429s mostly come in bursts of a few seconds: such a run is only counted into
the totals, and the card never hears of it. The start and end of every run go
to ``log`` with the upstream's error code and message (and a rate limit's
figures), so the two kinds can later be told apart.

The relay listens on 127.0.0.1 for the length of one task. The SK passes
through in the request headers exactly as Codex sends it, and is never logged.
The relay never follows a redirect: a 3xx goes back to the client as it came.
"""
from __future__ import annotations

import dataclasses
import email.utils
import json
import re
import select
import socket
import threading
import time

from covey.backends.gateway.connection import NO_REDIRECT
from covey.core.http_upstream import copy_headers, pipe, send
from covey.core.serving import LoopbackServer

# Seconds between the card's notes of one run of 429s: the relay tries again at once, so a note per try
# would redraw the card for every 429.
NOTE_EVERY = 5
# A run shorter than this is only counted: on 2026-11-01 half of 238 runs ended within 10 s.
SHOW_AFTER = 15
SUBAGENT = "collab_spawn"  # x-openai-subagent of a Codex sub-agent's requests.
LIMIT_WAIT = 60  # Longest wait on one rate-limit 429's Retry-After.
# Codex's own retry budget per request, for 5xx and dropped streams: the relay never hands a 429 back to retry.
CODEX_RETRIES = 10
# notify's states.
RETRYING, RECOVERED, GAVE_UP = "retrying", "recovered", "gave_up"
# What the gateway ({} is its display name) answered in a run: capacity 429s, rate-limit 429s or both.
REASON, LIMITED, BOTH = "{} 繁忙（429）", "{} 限速（429，请求太频繁）", "{} 繁忙和限速（429）"
DISPLAY = "模型网关"  # The name used where a gateway's own is not known.
# Hop-by-hop headers stay on their own connection; everything else passes through.
HOP = frozenset({"host", "connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade",
                 "transfer-encoding", "content-length"})
BUSY_DROP = HOP | {"retry-after"}  # send_busy sets its own.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A Responses stream's events before its output; the relay holds them until the next one shows whether the
# upstream failed. At most OPENING_BYTES are held.
OPENING = frozenset({"response.created", "response.in_progress", "response.queued"})
OPENING_BYTES = 64 * 1024
# An in-stream error's code, type or message saying the upstream is out of capacity; a spent quota is not.
CAPACITY_TEXTS = ("throughput limit", "too_many_requests", "too many requests", "rate_limit_exceeded", "资源不足")


def busy_detail(payload):
    """The upstream's error code and message for the log, one line, emails masked; never on the card."""
    try:
        error = json.loads(payload).get("error")
    except (ValueError, AttributeError):
        error = None
    if isinstance(error, dict):
        text = f"code={error.get('code', '')} message={error.get('message', '')}"
    else:
        text = "body=" + payload[:200].decode("utf-8", "replace")
    return EMAIL.sub("<email>", " ".join(text.split()))[:300]


def stream_error(data):
    """The capacity error a stream event carries: Azure's data line {"error": {...}}, an ``error`` event or a
    ``response.failed`` one; None for any other event or error."""
    error = data.get("error")
    if not isinstance(error, dict):
        error = data if data.get("type") == "error" else (
            (data.get("response") or {}).get("error") if data.get("type") == "response.failed" else None)
    if not isinstance(error, dict):
        return None
    text = " ".join(str(error.get(key) or "") for key in ("code", "type", "message")).lower()
    return error if any(marker in text for marker in CAPACITY_TEXTS) else None


def stream_opening(response):
    """Read an event stream up to its first event past OPENING: (the bytes read, that event's capacity error or
    None). The stream's end or OPENING_BYTES also ends the read, without an error."""
    held = b""
    while len(held) < OPENING_BYTES:
        chunk = response.read1(65536)
        if not chunk:
            break
        held += chunk
        *events, _ = held.replace(b"\r\n", b"\n").split(b"\n\n")  # The last part may be cut short.
        for event in events:
            lines = [line[5:].strip() for line in event.split(b"\n") if line.startswith(b"data:")]
            if not lines:
                continue  # A comment or a keep-alive.
            try:
                data = json.loads(b"\n".join(lines))
            except ValueError:
                return held, None
            if not isinstance(data, dict):
                return held, None
            if data.get("type") not in OPENING:
                return held, stream_error(data)
    return held, None


def limit_wait(headers):
    """Seconds a rate-limit 429 asks to wait (its Retry-After, at most LIMIT_WAIT); None for a capacity 429."""
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            seconds = 1
    return min(max(seconds, 0.0), LIMIT_WAIT)


def limit_detail(headers):
    """A rate-limit 429's figures for the log."""
    return " ".join(f"{name}={headers.get(header)}" for name, header in (
        ("retry_after", "Retry-After"), ("limit", "x-ratelimit-limit"), ("remaining", "x-ratelimit-remaining"))
        if headers.get(header) is not None)


@dataclasses.dataclass(frozen=True)
class Busy:
    """What ``notify`` hears of a run of 429s."""
    state: str
    retries: int  # Requests sent again in this run.
    seconds: float  # This run so far.
    runs: int  # The thread's runs in this turn, this one included.
    total_retries: int  # Over those runs.
    total_seconds: float
    child: str = ""  # A sub-agent's thread id; "" for the main task.
    limited: int = 0  # Rate-limit 429s among this run's.
    display: str = DISPLAY  # The gateway's name on the card.

    @property
    def reason(self):
        text = REASON if not self.limited else LIMITED if self.limited >= self.retries else BOTH
        return text.format(self.display)


class Run:
    def __init__(self):
        self.busy = 0  # 429s so far; any other answer ends the run.
        self.limited = 0  # Rate-limit 429s among them.
        self.since = None  # When the run began (monotonic).
        self.noted = None  # When the card last heard of it (monotonic).


def agent(child):
    # A thread id is a UUIDv7: its first digits are the time, shared by sub-agents started together.
    return f" agent={child[-8:]}" if child else ""


def client_gone(handler):
    """Whether Codex has closed the connection of a request the relay holds."""
    try:
        readable, _, _ = select.select([handler.connection], [], [], 0)
        return bool(readable) and handler.connection.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class BusyRelay:
    def __init__(self, upstream, *, window=None, note_every=NOTE_EVERY, show_after=SHOW_AFTER, notify=None,
                 log=None, name="gateway", display=DISPLAY):
        self.name, self.display = name, display
        self.upstream, self.window, self.note_every, self.show_after = (upstream.rstrip("/"), window, note_every,
                                                                        show_after)
        self.notify = notify or (lambda busy: None)
        self.log = log or (lambda line: None)
        self.runs = {}  # Thread ("" for the main task) -> its current Run.
        self.totals = {}  # Thread -> (runs, retries, seconds) of its ended runs this turn.
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.server = LoopbackServer(self.forward, name + "-relay")

    @property
    def base_url(self):
        return self.server.base_url

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *args):
        self.closed.set()  # A held request never goes upstream again.
        self.server.__exit__(*args)

    def report(self, child, state, retries, seconds, limited):
        """A Busy with the thread's totals; an ended run joins them. Under the lock."""
        runs, total_retries, total_seconds = self.totals.get(child, (0, 0, 0.0))
        busy = Busy(state, retries, seconds, runs + 1, total_retries + retries, total_seconds + seconds, child,
                    limited, self.display)
        if state != RETRYING:
            self.totals[child] = busy.runs, busy.total_retries, busy.total_seconds
            self.runs.pop(child, None)
        return busy

    def busy_answer(self, child, detail, limited=False):
        """Count a 429 of the thread's request: (attempt in its run, seconds since the run began, the Busy
        for the card or None). `detail` goes to the log when it starts a run."""
        with self.lock:
            now = time.monotonic()
            run = self.runs.setdefault(child, Run())
            if run.since is None:
                run.since = now
                self.log(f"{self.name} busy began {detail}{agent(child)}")
            run.busy += 1
            run.limited += bool(limited)
            busy_for, note = now - run.since, None
            if busy_for >= self.show_after and (run.noted is None or now - run.noted >= self.note_every):
                run.noted = now
                note = self.report(child, RETRYING, run.busy, busy_for, run.limited)
            return run.busy, busy_for, note

    def answered(self, child, status):
        """End the thread's run of capacity 429s, if any: its Busy when the card heard of the run, else None."""
        with self.lock:
            run = self.runs.get(child)
            if run is None:
                return None
            seconds = time.monotonic() - run.since
            self.log(f"{self.name} busy ended status={status} retries={run.busy} limited={run.limited} "
                     f"seconds={seconds:.0f}{agent(child)}")
            busy = self.report(child, RECOVERED, run.busy, seconds, run.limited)
            return busy if run.noted is not None else None  # A run the card never heard of ends quietly.

    def gave_up(self, child, attempt, seconds):
        with self.lock:
            limited = self.runs[child].limited
            self.log(f"{self.name} busy gave up retries={attempt} limited={limited} seconds={seconds:.0f}{agent(child)}")
            return self.report(child, GAVE_UP, attempt - 1, seconds, limited)  # The last 429 is not tried again.

    def tell(self, busy):
        try:
            self.notify(busy)
        except Exception:  # noqa: BLE001 - the card's line never decides the request
            pass

    def forward(self, handler, body):
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in HOP}
        child = handler.headers.get("thread-id", "") if handler.headers.get("x-openai-subagent") == SUBAGENT else ""
        while True:
            try:
                status, response = send(NO_REDIRECT, handler.command, self.upstream + handler.path, body, headers, 600)
            except OSError:
                # Codex retries a 502 the same way it retries other transport failures.
                handler.send_response(502)
                handler.send_header("Content-Length", "0")
                handler.send_header("Connection", "close")
                handler.end_headers()
                return
            with response:
                held, failed = b"", None
                if status == 200 and "text/event-stream" in (response.headers.get("Content-Type") or ""):
                    held, failed = stream_opening(response)
                if status != 429 and failed is None:
                    # Any other answer (200, 5xx, 403) ends the thread's run of 429s.
                    ended = self.answered(child, status)
                    if ended:
                        self.tell(ended)
                    self.relay(handler, status, response, held)
                    return
                if failed is None:
                    payload = response.read(1024 * 1024)
                    wait = limit_wait(response.headers)
                    detail = busy_detail(payload) + (" " + limit_detail(response.headers) if wait is not None else "")
                else:
                    wait, detail = None, "in-stream " + busy_detail(json.dumps({"error": failed}).encode())
                attempt, busy_for, note = self.busy_answer(child, detail, wait is not None)
                if self.window is not None and busy_for > self.window:
                    self.tell(self.gave_up(child, attempt, busy_for))
                    if failed is None:
                        self.send_busy(handler, response, payload, "")  # Without Retry-After Codex fails the turn.
                    else:
                        self.relay(handler, status, response, held)  # As it came: the client fails the turn.
                    return
            if note:
                self.tell(note)
            if self.closed.wait(wait or 0) or client_gone(handler):
                handler.close_connection = True
                return

    def send_busy(self, handler, response, payload, retry_after):
        handler.send_response(429)
        copy_headers(handler, response, BUSY_DROP)
        if retry_after:
            handler.send_header("Retry-After", retry_after)
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)

    def relay(self, handler, status, response, held=b""):
        """The upstream's answer as it is, ``held`` (what stream_opening read) first."""
        handler.send_response(status)
        copy_headers(handler, response, HOP)
        # Streamed responses (SSE) end when the connection closes.
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            pipe(handler, response, held=held)
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up or reset mid-stream; leaving the with block closes the upstream response.
            handler.close_connection = True
