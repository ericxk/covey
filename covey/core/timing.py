"""Millisecond timing of the live flows: where the time from a message or a click to what the
user sees goes (docs/operations.md 排障 "慢在哪里"; scripts/bridge_timing.py reads it).

A flow is one message, one click, a job's hand-off to its task runner or a runner's start.
It opens on the thread that handles it (``begin``/``flow``) and follows the work that thread
hands to another (``carry``). Two kinds of line, written only once ``configure`` names a sink
(the Bridge and each task runner do at startup; tests and tools see none):

- ``feishu call op=<name> api=<method:path or shortcut> ms=<wall> proc_ms=<the lark-cli process,
  or the SDK request> [inner_ms= origin_ms=] bytes=<request body> via=cli|sdk attempts=<n>
  result=ok|receipt|<category>[:<code>] thread=<name> flow=<flow>:<id>``: each Feishu call made
  inside a flow. ``inner_ms``/``origin_ms`` are Feishu's own, from its ``Server-Timing`` header
  (SDK requests only): its service's time, and its CDN edge's wait on that origin; the rest of
  ``proc_ms`` is the connection, the proxy and the way to the edge. Calls outside every flow
  (streaming card updates, final replies, watchers) are counted per ``op`` instead, one
  ``feishu calls op= n= errors= ms_p50= ms_max= proc_p50= [inner_p50= origin_p50=] window_s=``
  line a minute.
- ``timing flow=<name> [outcome=] <fields> t0=<epoch ms> total_ms= api_ms= calls=
  phases=<phase>:<ms>,...``: one line per flow once it and the work it carried have ended. A
  phase is the milliseconds from the flow's start (an event's arrival from the consumer) to
  that phase's end; each Feishu call inside the flow is a phase named by its ``op``.

Lines carry operation names, ids, counts and milliseconds: never a body, a token or user text.
Timing never changes what it measures: its hooks swallow their own errors and do nothing while
no sink is configured.
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import threading
import time

FLUSH_SECONDS = 60  # How often calls outside every flow are summarized.
SERVED = ("inner", "origin")  # The Server-Timing entries of Feishu's responses that a call line carries.
_sink = None
_local = threading.local()
_process = None  # A flow for calls on threads without one of their own (a task runner's start).
_totals: dict = {}  # op -> [calls, errors, wall ms, process ms], outside every flow
_totals_since = 0.0
_totals_lock = threading.Lock()
_UNSAFE = re.compile(r"[^\w#:.+/@-]")


def configure(sink):
    """Write timing lines to ``sink`` (None: none); returns the previous sink."""
    global _sink
    previous, _sink = _sink, sink
    return previous


def enabled() -> bool:
    return _sink is not None


def _write(line: str) -> None:
    sink = _sink
    if sink is not None:
        try:
            sink(line)
        except Exception:  # noqa: BLE001 - a log line never breaks what it measures
            pass


def _clean(value) -> str:
    return _UNSAFE.sub("_", str(value))[:80]


def at(wall: float) -> tuple[float, float]:
    """A flow's ``started`` for a moment known only by its wall time (a runner's handoff)."""
    return time.monotonic() - (time.time() - wall), wall


def delay_ms(stamp, wall: float):
    """Milliseconds from Feishu's epoch ``stamp`` (an event's create time) to ``wall``; None without one.
    A message's times are in milliseconds, a click's ``timestamp`` in microseconds (S32 read
    ``feishu_ms=-1791226253978466``): the unit is told by size."""
    try:
        value = int(str(stamp))
    except (TypeError, ValueError):
        return None
    while value >= 10 ** 14:  # Epoch milliseconds stay below this until the year 5138.
        value /= 1000
    return round(wall * 1000 - value)


class Flow:
    """One flow's phases and Feishu calls; written once every holder has released it."""

    def __init__(self, name, started=None, **fields) -> None:
        now = time.monotonic()
        # started: (monotonic, wall) of the event's arrival, when that came before this.
        self.t0, self.wall = started if started else (now, time.time())
        self.name = name
        self.fields: dict = {}
        self.phases: list = []
        self.calls, self.api_ms = 0, 0.0
        self.holders, self.closed, self.until_call = 1, False, False
        self.ended = False  # The holder ``begin`` gave its opener has let go (``end``).
        self.lock = threading.Lock()
        self.note(**fields)

    @property
    def id(self) -> str:
        return next((str(self.fields[k]) for k in ("event", "message", "job") if self.fields.get(k)), "")

    def ms(self) -> int:
        return round((time.monotonic() - self.t0) * 1000)

    def mark(self, phase) -> None:
        with self.lock:
            if not self.closed:
                self.phases.append((phase, self.ms()))

    def note(self, **fields) -> None:
        with self.lock:
            self.fields.update((k, v) for k, v in fields.items() if v is not None and v != "")

    def add(self, key, value) -> None:
        with self.lock:
            self.fields[key] = (self.fields[key] + "+" if self.fields.get(key) else "") + str(value)

    def outcome(self, value) -> None:
        with self.lock:
            self.fields.setdefault("outcome", value)

    def called(self, op, ms) -> None:
        with self.lock:
            if not self.closed:
                self.calls += 1
                self.api_ms += ms
                self.phases.append((op, self.ms()))

    def hold(self) -> None:
        with self.lock:
            self.holders += 1

    def release(self) -> None:
        with self.lock:
            self.holders -= 1
            if self.holders > 0 or self.closed:
                return
            self.closed = True
            # One that was not served has its own line (``ignored message``).
            line = None if self.fields.get("outcome") == "ignored" else self.line()
        if line:
            _write(line)
        flush(force=False)

    def line(self) -> str:
        fields = dict(self.fields)
        head = [f"flow={_clean(self.name)}"] + ([f"outcome={_clean(fields.pop('outcome'))}"] if "outcome" in fields else [])
        head += [f"{k}={_clean(v)}" for k, v in fields.items()]
        phases = ",".join(f"{_clean(p)}:{ms}" for p, ms in self.phases)
        return (f"timing {' '.join(head)} t0={round(self.wall * 1000)} total_ms={self.ms()} "
                f"api_ms={round(self.api_ms)} calls={self.calls} phases={phases}")


def current():
    """This thread's open flow, else the process's; None when there is none."""
    flow = getattr(_local, "flow", None)
    if flow is None or flow.closed:
        flow = _process
    return flow if flow is not None and not flow.closed else None


def begin(name, *, started=None, process=False, until_call=False, **fields):
    """Open flow ``name`` on this thread and return it, to ``end`` it later. None when timing is
    off or this thread has a flow open already (that one measures this too). ``process``: also
    the flow of calls on threads without their own; ``until_call``: it ends after its first call."""
    global _process
    mine = getattr(_local, "flow", None)
    if _sink is None or (mine is not None and not mine.closed):
        return None
    try:
        flow = Flow(name, started, **fields)
    except Exception:  # noqa: BLE001
        return None
    flow.until_call = until_call
    _local.flow = flow
    if process:
        _process = flow
    return flow


def end(flow) -> None:
    """End a flow ``begin`` opened (None: nothing); its line follows once its carried work ends.
    Ending it again does nothing."""
    global _process
    if flow is None:
        return
    if getattr(_local, "flow", None) is flow:
        _local.flow = None
    if _process is flow:
        _process = None
    with flow.lock:
        opened, flow.ended = not flow.ended, True
    if opened:
        flow.release()


def end_current(name) -> None:
    """End this thread's flow when it is ``name`` (a job's hand-off ends once its runner is spawned)."""
    flow = getattr(_local, "flow", None)
    if flow is not None and flow.name == name:
        end(flow)


@contextmanager
def flow(name, **kwargs):
    """``begin`` .. ``end`` around a block."""
    opened = begin(name, **kwargs)
    try:
        yield opened
    finally:
        end(opened)


def mark(phase) -> None:
    """The current flow's phase ``phase`` ends now."""
    flow = current()
    if flow is not None:
        flow.mark(phase)


def note(**fields) -> None:
    flow = current()
    if flow is not None:
        flow.note(**fields)


def add(key, value) -> None:
    """Append ``value`` to the current flow's field ``key`` (``jobs=#12+#13``)."""
    flow = current()
    if flow is not None:
        flow.add(key, value)


def outcome(value) -> None:
    """The current flow's outcome, unless something it called already named a more precise one."""
    flow = current()
    if flow is not None:
        flow.outcome(value)


def arrived() -> None:
    """The event consumer read an event: the flow it starts on this thread counts from now."""
    if _sink is not None:
        _local.arrival = (time.monotonic(), time.time())


def take_arrival():
    """The arrival ``arrived`` noted on this thread, once; None without one."""
    value = getattr(_local, "arrival", None)
    _local.arrival = None
    return value


class _Carried:
    """Work handed to another thread as part of a flow, which waits for it."""

    def __init__(self, work, flow, phase) -> None:
        self.work, self.flow, self.phase = work, flow, phase
        flow.hold()

    def __call__(self, *args, **kwargs):
        flow, self.flow = self.flow, None
        if flow is None:
            return self.work(*args, **kwargs)
        previous = getattr(_local, "flow", None)
        _local.flow = flow
        try:
            if self.phase:
                flow.mark(self.phase)
            return self.work(*args, **kwargs)
        finally:
            _local.flow = previous
            flow.release()

    def drop(self) -> None:
        """It will never run: its flow no longer waits for it."""
        flow, self.flow = self.flow, None
        if flow is not None:
            flow.release()


def carry(work, phase=""):
    """``work`` to run on another thread as part of this thread's flow (marking ``phase`` when it
    starts); ``work`` itself without a flow. Carried work that will never run must be ``drop``ped."""
    flow = getattr(_local, "flow", None)
    if flow is None or flow.closed:
        return work
    return _Carried(work, flow, phase)


def drop(work) -> None:
    if isinstance(work, _Carried):
        work.drop()


# Feishu calls -------------------------------------------------------------

_ID_AFTER = {"cards", "messages", "chats", "elements", "files", "folders", "threads", "resources", "users"}
_OPS = {
    "POST /open-apis/cardkit/v1/cards": "card_create",
    "PUT /open-apis/cardkit/v1/cards/:id": "card_put",
    "PATCH /open-apis/cardkit/v1/cards/:id/settings": "card_settings",
    "POST /open-apis/cardkit/v1/cards/:id/elements": "card_add",
    "PUT /open-apis/cardkit/v1/cards/:id/elements/:id": "card_element",
    "PATCH /open-apis/cardkit/v1/cards/:id/elements/:id": "card_patch",
    "DELETE /open-apis/cardkit/v1/cards/:id/elements/:id": "card_delete",
    "PUT /open-apis/cardkit/v1/cards/:id/elements/:id/content": "card_text",
    "POST /open-apis/cardkit/v1/cards/:id/batch_update": "card_batch",
    "GET /open-apis/im/v1/messages/:id": "msg_get",
    "GET /open-apis/im/v1/chats/:id": "chat_get",
    "GET /open-apis/bot/v3/info": "bot_info",
}


def describe(argv) -> tuple[str, str]:
    """(op, api) of a lark-cli argv: ``card_create`` and ``POST:/open-apis/cardkit/v1/cards``.
    Ids in a path become ``:id``; flags and their values (bodies, text) are never read."""
    argv = [a for a in argv if isinstance(a, str)]
    if "api" in argv[:6] and len(argv) > argv.index("api") + 2:
        at = argv.index("api")
        method, path = argv[at + 1].upper(), argv[at + 2].split("?", 1)[0]
        parts = path.split("/")
        parts = [":id" if i and parts[i - 1] in _ID_AFTER else p for i, p in enumerate(parts)]
        api = method + " " + "/".join(parts)
        named = _OPS.get(api) or method.lower() + "_" + "_".join(p for p in parts[2:] if p and p != ":id")
        return named, api.replace(" ", ":")
    start = 3 if argv[1:2] == ["--profile"] else 1
    words = []
    for word in argv[start:start + 3]:
        if word.startswith("-"):
            break
        words.append(word)
    shortcut = next((w for w in words if w.startswith("+")), "")
    if shortcut:
        named = {"+messages-reply": "msg_reply", "+messages-send": "msg_send"}.get(shortcut)
        return named or shortcut.lstrip("+").replace("-", "_"), "".join(words[:1]) + shortcut
    named = "_".join(words[:2]) or "lark_cli"  # ``event schema``, ``auth status``: no ids, no flags.
    return named, named


def served_ms(headers) -> dict:
    """Feishu's own milliseconds in a response's headers: ``Server-Timing: inner; dur=62,
    cdn-cache;desc=MISS,edge;dur=0,origin;dur=77`` gives {"inner": 62, "origin": 77}."""
    try:
        value = next((str(v) for k, v in dict(headers).items() if str(k).lower() == "server-timing"), "")
        served = {}
        for entry in value.split(","):
            name, *params = (part.strip() for part in entry.split(";"))
            dur = next((p[4:] for p in params if p.startswith("dur=")), "")
            if name in SERVED and dur:
                served[name] = round(float(dur))
        return served
    except Exception:  # noqa: BLE001 - timing never fails a call
        return {}


_BODY_FLAGS = ("--data", "--content", "--text", "--markdown")


def body_bytes(argv) -> int:
    """The size of the body ``argv`` sends (its ``--data``, ``--content``, ``--text`` or ``--markdown``
    value), 0 without one: a count, never the value."""
    argv = [a for a in argv if isinstance(a, str)]
    for at, word in enumerate(argv[:-1]):
        if word in _BODY_FLAGS:
            return len(argv[at + 1].encode())
    return 0


class Call:
    """One Feishu call being timed: ``run`` its process (or SDK request), then ``done``."""

    def __init__(self, argv, via) -> None:
        self.started = time.monotonic()
        self.argv, self.via = argv, via
        self.proc_ms, self.attempts = 0.0, 0
        self.served = {}  # Feishu's own ms by Server-Timing name (``inner``, ``origin``): feishu/sdk.py.

    def run(self, execute, *args, **kwargs):
        started = time.monotonic()
        try:
            result = execute(*args, **kwargs)
            served = getattr(result, "served", None)
            if isinstance(served, dict):
                self.served = served
            return result
        finally:
            self.proc_ms += (time.monotonic() - started) * 1000
            self.attempts += 1

    def done(self, result) -> None:
        try:
            self._done(result)
        except Exception:  # noqa: BLE001
            pass

    def _done(self, result) -> None:
        ms = (time.monotonic() - self.started) * 1000
        op, api = describe(self.argv)
        flow = current()
        if flow is None:
            _count(op, ms, self.proc_ms, result in ("ok", "receipt"), self.served)
            return
        flow.called(op, ms)
        thread = _clean(threading.current_thread().name.replace(" ", "_"))
        served = "".join(f"{name}_ms={self.served[name]} " for name in SERVED if name in self.served)
        _write(f"feishu call op={op} api={_clean(api)} ms={ms:.0f} proc_ms={self.proc_ms:.0f} {served}"
               f"bytes={body_bytes(self.argv)} via={self.via} attempts={self.attempts} result={_clean(result)} "
               f"thread={thread} flow={flow.name}:{_clean(flow.id)}")
        if flow.until_call:
            end(flow)


class _Off:
    """A Call while timing is off: it only runs."""

    @staticmethod
    def run(execute, *args, **kwargs):
        return execute(*args, **kwargs)

    @staticmethod
    def done(result) -> None:
        pass


_OFF = _Off()


def call(argv, via="cli"):
    """A Call for one Feishu call by ``argv``, or one that only runs while timing is off."""
    return Call(list(argv), via) if _sink is not None else _OFF


def _count(op, ms, proc_ms, ok, served) -> None:
    global _totals_since
    with _totals_lock:
        if not _totals:
            _totals_since = time.monotonic()
        row = _totals.setdefault(op, [0, 0, [], [], {name: [] for name in SERVED}])
        row[0] += 1
        row[1] += not ok
        row[2].append(ms)
        row[3].append(proc_ms)
        for name in SERVED:
            if name in served:
                row[4][name].append(served[name])
    flush(force=False)


def _p50(values) -> int:
    values = sorted(values)
    return round(values[(len(values) - 1) // 2]) if values else 0


def flush(force=True) -> None:
    """Write the totals of calls outside every flow: now (``force``), or once a minute has passed."""
    with _totals_lock:
        if not _totals or (not force and time.monotonic() - _totals_since < FLUSH_SECONDS):
            return
        rows, window = dict(_totals), time.monotonic() - _totals_since
        _totals.clear()
    for op, (count, errors, walls, procs, served) in sorted(rows.items()):
        own = "".join(f"{name}_p50={_p50(served[name])} " for name in SERVED if served[name])
        _write(f"feishu calls op={op} n={count} errors={errors} ms_p50={_p50(walls)} ms_max={max(walls):.0f} "
               f"proc_p50={_p50(procs)} {own}window_s={window:.0f}")
