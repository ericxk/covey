"""One Pi turn over ``pi --mode rpc``: a process per turn, JSONL both ways.

The bridge asks ``get_state`` for the session id and file, then sends the prompt. Steering
(``steer``), stopping (``abort``) and the end check go over the same stdin. pi prints
``agent_end`` after every failed attempt and then ``auto_retry_start``; an auto-compaction
can follow too. So an ``agent_end`` is only a candidate: the turn has ended when a ``get_state``
sent after it says nothing is streaming or compacting and no retry or continue started in between.
Closing stdin ends pi at once; its process group is stopped as the fallback.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import queue
import subprocess
import threading
import time

from covey.backends.pi.events import PiEventAdapter, failure_code, message_texts
from covey.core.attachments import MAX_IMAGE_BYTES, read_images
from covey.core.harness import JobCancelled
from covey.core.process import stop_group
from covey.core.reply_state import ReplyState
from covey.core.result import MAX_REPLY_BYTES
from covey.core.steer import SteerRefused, refuse_steer, steer_text

ACK_SECONDS = 8  # How long a steer waits for pi's response before it counts as possibly delivered.
ABORT_SECONDS = 10  # How long a stopped turn waits for its aborted agent_end.
EXIT_SECONDS = 5
FINAL = {"stop", "length", "aborted", "error"}


def images(attachments):
    return [{"type": "image", "data": data, "mimeType": mime}
            for mime, data in read_images(attachments or (), limit=MAX_IMAGE_BYTES)]


class PiControl:
    """The task's controls for a running pi; the bridge never replays a write (rule 6)."""
    native_blocking_questions = False
    supports_steer = True
    awaiting_answer = False

    def __init__(self, process, emit):
        self.process, self.emit = process, emit
        self.user_requests = set()
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.ids = itertools.count(1)
        self.waiting = {}  # request id -> [threading.Event, response row]
        self.steers = []  # (sent text, event id), until pi starts that user message
        self.accepting = False
        self.closing = False
        self.broken = False

    def send(self, row):
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.broken = True
                raise

    def command(self, kind, **fields):
        rid = f"{kind}-{next(self.ids)}"
        self.send({"id": rid, "type": kind, **fields})
        return rid

    def request(self, kind, seconds, **fields):
        """Send a command and wait for its response; None when none came in time."""
        rid = f"{kind}-{next(self.ids)}"
        slot = [threading.Event(), None]
        self.waiting[rid] = slot
        try:
            self.send({"id": rid, "type": kind, **fields})
            slot[0].wait(seconds)
            return slot[1]
        finally:
            self.waiting.pop(rid, None)

    def resolve(self, row):
        slot = self.waiting.get(row.get("id"))
        if slot is None:
            return False
        slot[1] = row
        slot[0].set()
        return True

    def steer(self, text, event_id):
        """Send a supplement and wait for pi's ack outside the lock: the turn's loop takes the lock to see
        pi start that message, and pi may print it before the ack."""
        with self.lock:
            refuse_steer(text, closing=self.closing or self.broken, ready=self.accepting, label="Pi")
            message = steer_text(text)
            entry = (message, event_id)
            self.steers.append(entry)  # Until pi starts it; an unconfirmed one stays: a late read still counts.
        try:
            response = self.request("steer", ACK_SECONDS, message=message)
        except (OSError, ValueError):
            raise RuntimeError("Pi 补充送达未确认，不会自动重发") from None
        if response is None:
            raise RuntimeError("Pi 补充送达未确认，不会自动重发")
        if response.get("success") is not True:
            with self.lock:
                if entry in self.steers:
                    self.steers.remove(entry)
            raise SteerRefused("Pi 当前不能接收补充；请在本轮结束后发送下一轮要求")

    def accept(self, row):
        """A user message pi starts is a supplement the model now reads."""
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if row.get("type") != "message_start" or message.get("role") != "user":
            return
        content = message.get("content")
        text = content if isinstance(content, str) else "".join(
            c.get("text", "") for c in content or () if isinstance(c, dict) and c.get("type") == "text")
        with self.lock:
            for index, (sent, event_id) in enumerate(self.steers):
                if sent == text:
                    del self.steers[index]
                    break
            else:
                return
        self.emit("steer_read", {"event_id": event_id})

    def answer(self, request_id, answers):
        raise SteerRefused("Pi 没有原生问题")

    def reject_question(self, request_id, reason):
        raise SteerRefused("Pi 没有原生问题")

    def wait_for_answer(self, question):
        pass

    def abort(self):
        with contextlib.suppress(OSError, ValueError):
            self.command("abort")

    def close(self):
        """Stop the turn: abort, close stdin, then the process group."""
        with self.lock:
            self.closing = True
            self.accepting = False
        self.abort()
        with contextlib.suppress(OSError, ValueError):
            self.process.stdin.close()
        stop_group(self.process, EXIT_SECONDS)


class TurnRows:
    """What the turn printed, and whether it has ended."""

    def __init__(self, expected_session):
        self.expected_session = expected_session
        self.sid = self.session_file = None
        self.started = self.invalid = self.ended = False
        self.refusal = None  # pi's error when it refused the prompt.
        self.candidate = None  # The last assistant message of an agent_end that may end the turn.
        self.probe = None  # The get_state id sent to confirm the candidate.
        self.compacting = False
        self.last_assistant = None


def read_stdout(stream, rows):
    """pi's stdout, split on "\\n" only (JSON strings may hold U+2028), as parsed rows; None at EOF. Each
    row is stamped with when it came (``_received_at``): the turn's loop may take it up later."""
    try:
        for raw in stream:
            try:
                row = json.loads(raw.decode("utf-8"))
            except ValueError:
                row = {"type": "_invalid"}
            row = row if isinstance(row, dict) else {"type": "_invalid"}
            row["_received_at"] = time.time()
            rows.put(row)
    except (OSError, ValueError):
        pass
    finally:
        rows.put(None)


def outcome(turn, rc, cancelled):
    """(status, code) of a turn whose rows are read; ("completed", "") when the last message is the reply."""
    message = turn.last_assistant or {}
    reason = message.get("stopReason")
    if turn.invalid:
        return "failed", "protocol_error"
    if cancelled:
        return "cancelled", "cancelled"
    if turn.refusal is not None:
        return "failed", failure_code(turn.refusal)
    if not (turn.sid and turn.started):
        return "failed", "process_exit" if type(rc) is int and rc != 0 else "root_incomplete"
    if not turn.ended:
        return "failed", "process_exit" if type(rc) is int and rc != 0 else "root_incomplete"
    if reason == "error":
        return "failed", failure_code(message.get("errorMessage"))
    if reason == "aborted":
        return "cancelled", "cancelled"
    if reason != "stop":
        return "failed", "root_incomplete"
    text = message_texts(message).strip()
    if not text or len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        return "failed", "output_missing"
    return "completed", ""


def run_rpc(request, argv, env, *, context_window=0, label="Pi"):
    ctx, inputs = request.context, request.inputs
    options = {**ctx.options, "harness_id": "pi", "harness_label": label}
    state = ReplyState(options, inputs.run_id, request.publish)
    adapter = PiEventAdapter(lambda kind, data: request.emit(kind, data, None), context_window)
    turn = TurnRows(request.session_id)
    cancel = inputs.cancel_event
    request.check_cancelled()
    try:
        state.invalidate()
    except Exception:
        return state.result("", request.session_id, status="failed", code="storage_failed")
    try:
        payload = images(inputs.attachments)
    except ValueError:  # Not an image pi takes: as the Claude Harness ends the turn (claude/runtime.py).
        return state.result("", request.session_id, status="failed", code="protocol_error")
    except OSError:
        return state.result("", request.session_id, status="failed", code="transport_error")
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=env, cwd=str(ctx.workdir), start_new_session=True)
    control = PiControl(process, lambda kind, data: request.emit(kind, data, control))
    rows = queue.Queue()
    reader = threading.Thread(target=read_stdout, args=(process.stdout, rows), daemon=True)
    reader.start()
    status, code, text = "failed", "root_incomplete", ""
    deadline = time.monotonic() + ctx.timeout if ctx.timeout > 0 else None
    try:
        try:
            request.observers.process(process)
        except Exception:
            pass
        request.observers.control(control)
        control.send({"id": "state-0", "type": "get_state"})
        stopped, timed_out = _read_loop(rows, turn, control, adapter, request, payload, deadline)
        control.closing, control.accepting = True, False
        if not timed_out:
            rc = process.poll()
            if rc is None and not (stopped or turn.ended or turn.invalid or turn.refusal is not None):
                # Its output ended without the turn ending: pi is exiting, but the exit code may not be
                # collectable yet (a loaded machine); wait for it so the failure is process_exit, not a guess.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    rc = process.wait(2)
            status, code = outcome(turn, rc, stopped and cancel.is_set())
        if status == "completed":
            text = message_texts(turn.last_assistant).strip()
            state.preserve(turn.sid, ("pi-rpc", turn.sid, state.run_id), ("final:" + state.run_id,), text)
        elif timed_out:
            status, code = "timed_out", "timeout"
    except JobCancelled:
        status, code = "cancelled", "cancelled"
    except (OSError, ValueError):
        code = "transport_error"
    except Exception:
        code = "storage_failed" if state.storage_failed else "protocol_error"
    finally:
        forced = _close(process, control, reader, request)
    return state.finish(text, turn.sid or request.session_id, status, code,
                        identity=bool(turn.sid and turn.started and not turn.invalid),
                        cleanup=not forced and process.poll() is not None, cancel_event=cancel, emit=request.emit)


def _read_loop(rows, turn, control, adapter, request, payload, deadline):
    """Take pi's rows until the turn ends, its output ends or it is refused; a stop aborts the turn and waits
    ABORT_SECONDS for it to end, past ``deadline`` the task timed out. (stopped, timed_out)."""
    cancel = request.inputs.cancel_event
    stopped = timed_out = False
    abort_deadline = None
    while True:
        if cancel.is_set() and not stopped:
            stopped = True
            control.closing, control.accepting = True, False
            control.abort()
            abort_deadline = time.monotonic() + ABORT_SECONDS
        if abort_deadline is not None and time.monotonic() > abort_deadline:
            break
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            break
        try:
            row = rows.get(timeout=0.2)
        except queue.Empty:
            continue
        if row is None:
            break
        accept(row, turn, control, adapter, request, payload)
        if turn.ended or turn.invalid or turn.refusal is not None:
            break
    return stopped, timed_out


def _close(process, control, reader, request):
    """Close pi after its turn, however it ended: input closed, EXIT_SECONDS to exit, its group stopped, the
    controls dropped. Whether it had to be stopped (it did not exit by itself)."""
    forced = False
    control.closing, control.accepting = True, False
    with contextlib.suppress(OSError, ValueError):
        process.stdin.close()
    try:
        process.wait(EXIT_SECONDS)
    except subprocess.TimeoutExpired:
        forced = True
    # pi's bash tool runs each command in a group of its own (detached), which this does not reach: what
    # they leave running is the task's leftovers (app/leftovers.py), found by COVEY_TASK_DIR.
    stop_group(process, EXIT_SECONDS)
    reader.join(timeout=2)
    with contextlib.suppress(OSError):
        process.stdout.close()
    try:
        request.observers.control(None)
    except Exception:
        pass
    return forced


def accept(row, turn, control, adapter, request, payload):
    kind = row.get("type")
    now = row.pop("_received_at", None)
    now = now if type(now) is float else time.time()
    if kind == "_invalid":
        turn.invalid = True
        return
    if kind == "response":
        if control.resolve(row):
            return
        rid, data = row.get("id"), row.get("data") if isinstance(row.get("data"), dict) else {}
        if rid == "state-0":
            on_state(row, data, turn, control, request, payload)
            if turn.sid is not None:
                adapter.request_sent(time.time())
        elif rid == "prompt":
            if row.get("success") is not True:
                turn.refusal = str(row.get("error") or "")
        elif rid == turn.probe:
            turn.probe = None
            if (row.get("success") is True and turn.candidate is not None and not turn.compacting
                    and data.get("isStreaming") is False and data.get("isCompacting") is False):
                turn.ended = True
        return
    if turn.sid is None:
        return  # Nothing of the turn comes before its state and prompt.
    try:
        control.accept(row)
    except Exception:
        pass  # A steer_read never decides the turn.
    if kind == "agent_start":
        turn.started, turn.candidate = True, None
        control.accepting = not control.closing
    elif kind == "message_end" and isinstance(row.get("message"), dict) and row["message"].get("role") == "assistant":
        turn.last_assistant = row["message"]
    elif kind == "agent_end":
        last = next((m for m in reversed(row.get("messages") or []) if isinstance(m, dict) and m.get("role") == "assistant"),
                    turn.last_assistant)
        if last is None or last.get("stopReason") in FINAL:
            turn.last_assistant, turn.candidate = last, last or {}
            probe(turn, control)
    elif kind == "auto_retry_start":
        turn.candidate = None
    elif kind == "auto_retry_end" and row.get("success") is False and turn.candidate is None:
        turn.candidate = turn.last_assistant or {}  # The retries are spent (or a stop ended their wait).
        probe(turn, control)
    elif kind == "compaction_start":
        turn.compacting = True
    elif kind == "compaction_end":
        turn.compacting = False
        if row.get("willRetry"):
            turn.candidate = None
        elif turn.candidate is not None:
            probe(turn, control)
    try:
        adapter.accept(row, now)
    except Exception:
        pass  # Observability never decides the outcome.


def probe(turn, control):
    try:
        turn.probe = control.command("get_state")
    except (OSError, ValueError):
        turn.probe = None


def on_state(row, data, turn, control, request, payload):
    sid, path = data.get("sessionId"), data.get("sessionFile")
    expected = turn.expected_session
    if (row.get("success") is not True or not isinstance(sid, str) or not sid or not isinstance(path, str)
            or (expected is not None and sid != expected)):
        turn.invalid = True
        return
    turn.sid, turn.session_file = sid, path
    try:
        request.observers.session(sid)
    except Exception:
        pass
    control.send({"id": "prompt", "type": "prompt", "message": request.prompt, **({"images": payload} if payload else {})})
