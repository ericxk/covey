"""Antigravity (Google's ``agy`` CLI): Gemini and the other models of the owner's Google account, headless.

One turn is one ``agy --output-format stream-json --input-format stream-json`` process: the prompt goes
in as one ``user`` line on stdin, which is then closed, so agy ends after that turn. Its rows are one
``init`` (the conversation id, the native session), ``step_update`` rows (text deltas, tool steps) and one
``result`` with the turn's answer and status. ``--conversation <id>`` continues the topic's conversation.
agy signs in with the Google account it was logged in with on this machine (``agy`` once, interactively);
the Bridge never sees the token. It has no question a headless run can answer, so a task asks through
the checkpoint file like dsh and Pi; stdin takes text only, so images are given by their path (agy opens
them with its own read_file).

agy takes nothing into a running turn (another ``user`` line is a turn of its own, after this one), so a
supplement stops the turn and goes on with it: SIGINT ends agy at once with ``result.error`` "interrupted"
(its conversation keeps what was done; seen on 1.5.13), the commands it left running are stopped,
and the same conversation goes on in a new process with the supplement as its message (AgyControl). The
job's reply is the last process's answer.

The butler's turn (``options["mcp_config"]``, its Bridge tools) runs with a home of its own whose MCP config adds them
(backends/agy/home.py): agy reads MCP servers from one file in the home and from nowhere a turn could name.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
import re
import os
import shlex
import signal
import subprocess
import threading
import time

from covey.backends.agy.home import PrivateHome
from covey.core.harness import (BackendConfig, BackendRuntime, HARNESS_REGISTRY, HarnessCapabilities, HarnessSpec,
                                JobCancelled, UUID_RE, for_device)
from covey.core import env as cenv
from covey.core import procfs
from covey.core.process import is_owned, process_identity, stop_group, stream_subprocess_lines
from covey.core.process_env import child_process_env
from covey.core.reply_state import ReplyState
from covey.core.result import MAX_REPLY_BYTES
from covey.core.steer import SteerRefused, refuse_steer, steer_text
from covey.core.steps import AnswerNotes, Steps
from covey.core.text import redact, tool_detail
from covey.core.usage import TurnTokens

DEFAULT_MODEL = ""  # agy's own default (its saved /model, else the account's).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
STDERR_LINES = 40
MODELS_SECONDS = 60  # How long ``agy models`` may take for the launch catalog.
# What agy's error says when the turn failed for the account, not the task (its own words, matched loosely).
AUTH_RE = re.compile(r"authenticat|sign(ed)? ?in|log ?in|unauthori[sz]ed|\b401\b|credential", re.I)
LIMIT_RE = re.compile(r"\b429\b|rate.?limit|resource.?exhausted|too many requests", re.I)
QUOTA_RE = re.compile(r"quota|credits?\b.*(exhausted|insufficient|out)|out of credits|usage limit", re.I)
NOT_LOGGED_IN = ("Antigravity 还没有登录：在运行机器人的账号下执行一次 agy，选 Google OAuth 登录"
                 "（远程机器上打开它给的链接、把授权码贴回终端），再重试本轮。")

AGY_SPEC = HarnessSpec(
    name="agy", title="Antigravity", prompt_agent_name="Antigravity", idempotency_prefix="agy-lark",
    env_prefix="COVEY_AGY", state_dir_name="agy-bridge", session_filename="agy_session_id",
    session_pattern=UUID_RE,
    default_profile=cenv.get("COVEY_PROFILE", "").strip() or None)  # The Codex Harness's profile.


def native_home():
    """Where agy keeps its settings, conversations and logs."""
    return Path.home() / ".gemini" / "antigravity-cli"


class AntigravitySessionStore:
    def sessions_dir(self, workdir):
        return native_home() / "conversations"

    def transcript(self, workdir, session_id):
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
            return None
        return next(iter(sorted(self.sessions_dir(workdir).glob(f"{session_id}*"))), None)


# agy's tools by the step names the cards know (core/protocol.py STEP_TITLES); any other shows as it is.
TOOL_NAMES = {"run_command": "bash", "view_file": "read", "read_file": "read", "view_file_outline": "read", "view_code_item": "read",
              "write_to_file": "write", "replace_file_content": "edit", "multi_replace_file_content": "edit",
              "grep_search": "grep", "find_by_name": "glob", "list_dir": "glob", "read_url_content": "webfetch",
              "search_web": "websearch", "invoke_subagent": "agent"}
# A tool parameter by the key tool_detail reads.
PARAMETER_KEYS = {"commandline": "command", "command": "command", "absolutepath": "path", "targetfile": "path",
                  "file": "path", "path": "path", "searchpath": "path", "directorypath": "path",
                  "searchdirectory": "path", "query": "query", "pattern": "pattern", "url": "url"}
DETAIL_KEYS = ("command", "path", "pattern", "query", "url")


def tool_inputs(parameters):
    """agy's parameters (``CommandLine``, ``AbsolutePath``…) under the keys tool_detail reads."""
    if not isinstance(parameters, dict):
        return {}
    found = {}
    for key, value in parameters.items():
        name = PARAMETER_KEYS.get(str(key).replace("_", "").lower())
        if name and name not in found and isinstance(value, (str, int, float)) and str(value).strip():
            found[name] = str(value)
    return found


def usage_counts(usage):
    """(output, uncached input, cache reads) of agy's usage: its ``input_tokens`` leaves the cache reads out
    (``total_tokens`` is input plus output; seen on 1.5.13), its output counts the thinking."""
    if not isinstance(usage, dict):
        return None
    output, uncached, read = (usage.get(k) for k in ("output_tokens", "input_tokens", "cache_read_tokens"))
    if type(output) is not int:
        return None
    return output, uncached, read


class AntigravityEventAdapter:
    """``step_update`` rows to public Harness events. Thinking and tool output are never published."""

    def __init__(self, emit):
        self.emit = emit
        self.steps = Steps(emit)
        self.notes = AnswerNotes(emit)
        self.texts = {}  # step key -> the text streamed so far
        self.segment = 0  # Which agy process of the turn (a supplement starts the next one).
        self.tokens = TurnTokens()
        self.counted = False  # Whether a model request's usage was added.
        self.first_output = False

    @property
    def tools(self):
        return self.steps.steps

    def accept(self, row, now):
        if row.get("event") == "result":
            if not self.counted:  # No model request said its usage: the turn's total, untimed.
                self.add_usage((row.get("result") or {}).get("usage") if isinstance(row.get("result"), dict) else None)
            return
        step = row.get("step_update")
        if row.get("event") != "step_update" or not isinstance(step, dict):
            return
        kind, index = step.get("step_type"), step.get("step_index")
        key = f"step-{index}" if not self.segment else f"step-{self.segment}-{index}"
        if kind == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                if not self.first_output:
                    self.first_output = True
                    self.emit("model_output", {"received_at": now})
                self.texts[key] = self.texts.get(key, "") + delta
                if self.texts[key].strip():
                    self.notes.text(key, self.texts[key])
            if step.get("state") == "DONE":
                # One model request: its usage and the time it took (thinking and text, no tool).
                seconds = step.get("duration_seconds")
                self.add_usage(step.get("usage"), seconds if isinstance(seconds, (int, float)) else None)
        elif kind == "system_message":
            self.notes.to_notes(now)  # A subagent came back: what the model wrote before it is a progress note.
        elif kind in ("tool", "subagent"):
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            native = str(step.get("tool_name") or info.get("name") or "tool")
            if key not in self.steps:
                self.notes.to_notes(now)
                self.steps.start(key, TOOL_NAMES.get(native, native),
                                 tool_detail(tool_inputs(info.get("parameters")), DETAIL_KEYS)[:500], now)
            if step.get("state") == "DONE":
                self.steps.finish(key, not info.get("error"), now)

    def stopped(self, now):
        """The process was stopped for a supplement: its steps still running end failed (their command was
        stopped), what it wrote becomes a note, and the next process's steps get keys of their own."""
        for key in self.steps.running():
            self.steps.finish(key, False, now)
        self.notes.to_notes(now)
        self.segment += 1

    def add_usage(self, usage, seconds=None):
        counts = usage_counts(usage)
        if counts is not None and self.tokens.add(*counts, 0, seconds=seconds):
            self.counted = True
            self.emit("token_usage", self.tokens.payload())


SLUG_RE = re.compile(r"[a-z][a-z0-9]*(?:[-._][a-z0-9]+)+")
DEFAULT_EFFORTS = ("high", "medium")  # The budget a model's card preselects, the first it has.


def models_from_listing(text):
    """The launch catalog of ``agy models``' output, one ``<slug>\t<name>`` a line (1.5.13: ``gemini-4-neon-high\t
    Gemini 4 Neon (High)``). A slug ending in a budget is that budget of its model (``--model gemini-4-neon --effort
    high``, which agy takes and checks), so the card lists the model once with its budgets; one without
    (``gemini-pro-orchestrator``) is a model of its own. Any other line (``Fetching…``, a heading) is skipped."""
    catalog = {}
    for line in str(text or "").splitlines():
        slug, _, name = line.strip().partition("\t")
        slug, name = slug.strip(), name.strip()
        if not SLUG_RE.fullmatch(slug):
            continue
        base, _, effort = slug.rpartition("-")
        if effort in EFFORTS and base:
            row = catalog.setdefault(base, {"display_name": re.sub(r"\s*\([^()]*\)$", "", name) or base,
                                            "supported_reasoning_levels": []})
            if effort not in [level["effort"] for level in row["supported_reasoning_levels"]]:
                row["supported_reasoning_levels"].append({"effort": effort})
        else:
            catalog.setdefault(slug, {"display_name": name or slug, "supported_reasoning_levels": []})
    for row in catalog.values():
        row["supported_reasoning_levels"].sort(key=lambda level: EFFORTS.index(level["effort"]))
        levels = [level["effort"] for level in row["supported_reasoning_levels"]]
        default = next((effort for effort in DEFAULT_EFFORTS if effort in levels), levels[0] if levels else "")
        if default:
            row["default_reasoning_level"] = default
    return catalog


class AntigravityBackend(BackendRuntime):
    def add_cli_args(self, parser):
        parser.add_argument("--agy-cmd", default="agy")
        parser.add_argument("--agy-timeout", type=int, default=0)
        parser.add_argument("--model", default=DEFAULT_MODEL)
        parser.add_argument("--reasoning-effort", default="")
        parser.add_argument("--no-skip-permissions", dest="skip_permissions", action="store_false")

    def build_config(self, args):
        return BackendConfig(args.model, args.reasoning_effort, args.agy_timeout, shlex.split(args.agy_cmd),
                             args.skip_permissions, {"harness_id": "agy"})

    def capabilities(self, ctx):
        return for_device(HarnessCapabilities(steering=True, steer_reads=True, bridge_tools=True), ctx)

    def environment(self, ctx):
        return child_process_env(ctx.workdir)

    def model_catalog(self, ctx):
        path = Path(ctx.state_dir) / "launch-models" / "agy.json"
        try:
            saved = json.loads(path.read_text()).get("models") or {}
        except (OSError, ValueError, AttributeError):
            return {}
        return {model: {"display_name": row.get("display_name") or model,
                        "supported_reasoning_levels": [{"effort": e} for e in row.get("levels") or ()],
                        **({"default_reasoning_level": row["default"]} if row.get("default") else {})}
                for model, row in saved.items() if isinstance(row, dict)}

    def refresh_model_catalog(self, ctx):
        """The models ``agy models`` lists for the signed-in account; none (not signed in, not installed) raises."""
        try:
            done = subprocess.run([*ctx.backend_cmd, "models"], cwd=str(ctx.workdir), env=self.environment(ctx),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=MODELS_SECONDS,
                                  start_new_session=True, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("agy models 没有回应") from None
        except OSError as exc:
            raise ValueError(f"agy 不能运行：{exc.strerror or exc}") from None
        if done.returncode != 0:
            raise ValueError(NOT_LOGGED_IN if AUTH_RE.search(done.stderr + done.stdout) else
                             f"agy models 失败（退出码 {done.returncode}）")
        catalog = models_from_listing(done.stdout)
        if not catalog:
            raise ValueError("agy models 没有列出模型")
        return catalog

    def normalize_model_settings(self, ctx, model, effort):
        """An empty model is agy's own default; a budget agy does not know never reaches it, nor one the saved
        catalog says the model lacks (agy refuses those itself, after starting)."""
        if effort and effort not in EFFORTS:
            raise ValueError("Antigravity 不支持所选推理档位")
        levels = self.levels(ctx, model)
        if effort and levels and effort not in levels:
            raise ValueError("此模型不支持所选推理档位")
        return model, effort

    def levels(self, ctx, model):
        """The budgets the saved catalog lists for ``model``; None when the catalog does not know it."""
        row = self.model_catalog(ctx).get(model) if model else None
        return None if row is None else [level["effort"] for level in row["supported_reasoning_levels"]]

    def argv(self, ctx, session_id):
        # A model the catalog lists without budgets takes no --effort (agy refuses one).
        effort = "" if self.levels(ctx, ctx.model) == [] else ctx.effort
        return [*ctx.backend_cmd, "--output-format", "stream-json", "--input-format", "stream-json",
                "--disable-slash-commands",
                *(["--dangerously-skip-permissions"] if ctx.skip_permissions else []),
                *(["--conversation", session_id] if session_id else []),
                *(["--model", ctx.model] if ctx.model else []),
                *(["--effort", effort] if effort else [])]

    def run(self, request):
        request.check_cancelled()
        ctx = request.context
        self.normalize_model_settings(ctx, ctx.model, ctx.effort)
        options = {**ctx.options, "harness_id": "agy", "harness_label": "Antigravity",
                   "error_texts": {**ctx.options.get("error_texts", {}), "authentication_error": NOT_LOGGED_IN}}
        request = dataclasses.replace(request, context=dataclasses.replace(ctx, options=options))
        stdin = json.dumps({"event": "user", "message": {"content": request.prompt}}, ensure_ascii=False) + "\n"
        argv, env = self.argv(ctx, request.session_id), self.environment(ctx)
        if not ctx.options.get("mcp_config"):
            return run_stream(request, argv, stdin, env)
        # The butler's turn: its Bridge tools through a home of its own (backends/agy/home.py).
        home = PrivateHome(ctx.state_dir, env.get("HOME") or Path.home(), ctx.options["mcp_config"], env)
        with home as path:
            if home.note:
                logging.getLogger(__name__).warning("agy private home: %s", home.note)
            return run_stream(request, argv, stdin, {**env, "HOME": str(path)})

    def prompt_runtime_note(self, ctx):
        return ("Antigravity 权限模式：always-proceed（--dangerously-skip-permissions）" if ctx.skip_permissions
                else "Antigravity 权限模式：request-review，要批准的操作会被跳过")

    def startup_lines(self, ctx):
        return [f"agy_cmd={ctx.backend_cmd} model={ctx.model or '(agy default)'} effort={ctx.effort or '(default)'} "
                f"home={native_home()}"]


class StreamRows:
    """What one run printed, fed line by line by the shared reader."""

    def __init__(self, request, adapter, expected_session, control=None, *, resumed=False):
        self.request, self.adapter, self.expected_session = request, adapter, expected_session
        self.control, self.resumed = control, resumed
        self.sid = self.result = None
        self.invalid = False
        self.process = None
        self.kinds = {}  # step_index -> step_type, of the root conversation's steps
        self.texts = {}  # step_index -> what an agent_response step wrote

    def reply(self):
        """The turn's answer: what the model wrote after its last step that was not writing (a tool, a subagent, a
        system message such as a subagent's return). agy's ``result.response`` joins everything the model wrote in
        the turn: the progress lines GEMINI.md asks for and the "waiting for the subagents" lines before the answer.
        '' when the answer is not the end of ``response`` (then ``response`` is taken as it is)."""
        response = str((self.result or {}).get("response") or "").strip()
        last = max((index for index, kind in self.kinds.items() if kind != "agent_response"), default=-1)
        answer = "".join(self.texts[index] for index in sorted(self.texts) if index > last).strip()
        return answer if answer and response.endswith(answer) else response

    def observe(self, p):
        self.process = p
        if self.control is not None:
            self.control.started(p)
        try:
            self.request.observers.process(p)
        except Exception:
            pass

    def on_line(self, line):
        # Never raise here: the shared reader still owns draining and reaping the process.
        try:
            row = json.loads(line)
        except ValueError:
            self.invalid = True
            return
        if not isinstance(row, dict):
            self.invalid = True
            return
        kind = row.get("event")
        if kind == "init":
            init = row.get("init") if isinstance(row.get("init"), dict) else {}
            value = row.get("conversation_id") or init.get("conversation_id")
            if (self.sid is not None or not isinstance(value, str) or not UUID_RE.fullmatch(value)
                    or (self.expected_session is not None and value != self.expected_session)):
                self.invalid = True
                return
            self.sid = value
            if self.control is not None:
                with self.control.lock:
                    self.control.ready = not self.control.closing
            if not self.resumed:  # A process after a supplement goes on with the conversation already reported.
                try:
                    self.request.observers.session(value)
                except Exception:
                    pass
            return
        if kind == "result":
            result = row.get("result")
            if self.result is not None or not isinstance(result, dict):
                self.invalid = True
                return
            if self.sid is not None and result.get("conversation_id") not in (None, "", self.sid):
                self.invalid = True
                return
            self.result = result
        elif kind == "step_update":
            step = row.get("step_update")
            if self.sid is None or self.result is not None or not isinstance(step, dict):
                self.invalid = True
                return
            if step.get("conversation_id") not in (None, self.sid):
                # A subagent's steps carry their own conversation; never the root's answer.
                return
            index = step.get("step_index")
            if type(index) is int:
                self.kinds[index] = str(step.get("step_type") or "")
                if self.kinds[index] == "agent_response" and isinstance(step.get("text_delta"), str):
                    self.texts[index] = self.texts.get(index, "") + step["text_delta"]
        try:
            self.adapter.accept(row, time.time())
        except Exception:
            pass  # Observability never decides the outcome.


def stream_outcome(rows, rc, cancel_event):
    """(status, code) of a run whose output the reader has drained; ("completed", "") when the result's
    ``response`` is the reply."""
    result = rows.result or {}
    status, error = result.get("status"), str(result.get("error") or "")
    if rows.invalid:
        return "failed", "protocol_error"
    if status == "ERROR" or (rows.result is None and type(rc) is int and rc != 0):
        if AUTH_RE.search(error):
            return "failed", "authentication_error"
        if QUOTA_RE.search(error):
            return "failed", "quota_exhausted"
        if LIMIT_RE.search(error):
            return "failed", "rate_limited"
    if status in ("CANCELED", "INTERRUPTED") and cancel_event is not None and cancel_event.is_set():
        return "cancelled", "cancelled"
    if type(rc) is not int or rc != 0:
        return "failed", "process_exit" if rows.result is None else "root_failed"
    if not (rows.sid and rows.result is not None):
        return "failed", "root_incomplete"
    if status != "SUCCESS":
        return "failed", "root_failed"
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled", "cancelled"
    text = result.get("response")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        return "failed", "output_missing"
    return "completed", ""


def stderr_lines(stderr):
    """agy's last lines of stderr for the failure log, credentials hidden and its sign-in URL left out."""
    lines = [line for line in str(stderr or "").splitlines() if line.strip() and "oauth2" not in line]
    return [redact(line)[:400] for line in lines[-STDERR_LINES:]]


def auth_failure(stderr):
    """Whether agy stopped before its turn for want of a login (it prints its sign-in URL on stderr)."""
    return bool(AUTH_RE.search(stderr or ""))


RESUMED = ("用户补充（你刚才的执行被这条补充打断了，已经做完的仍然有效，停下来的命令没有跑完；"
           "先看补充，再决定怎么接着做）：\n")
STOP_SECONDS = 5  # How long the commands a stopped turn left running get to exit before they are killed.


def descendants(root):
    """The identities of this user's processes under ``root`` now (agy's commands and helpers)."""
    parents = {}
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            row = procfs.stat(entry.name)
            if row is not None:
                parents[int(entry.name)] = row.ppid
    found, frontier = [], [root]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent:
                identity = process_identity(pid)
                if identity is not None:
                    found.append(identity)
                    frontier.append(pid)
    return found


def stop_left(identities):
    """Stop the processes of ``identities`` still alive, each checked against its identity first (never a
    recycled PID): TERM, STOP_SECONDS, then KILL."""
    alive = [identity for identity in identities if is_owned(identity)]
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for identity in alive:
            if is_owned(identity):
                try:
                    os.kill(int(identity["pid"]), signum)
                except OSError:
                    pass
        deadline = time.monotonic() + STOP_SECONDS
        while signum == signal.SIGTERM and time.monotonic() < deadline and any(map(is_owned, alive)):
            time.sleep(0.1)


class AgyControl:
    """The task's controls: a supplement stops the running agy (SIGINT) and is the next process's message.
    Writes are never replayed (rule 6): an accepted supplement is held here until a process takes it."""
    native_blocking_questions = False
    supports_steer = True
    awaiting_answer = False

    def __init__(self, emit):
        self.emit = emit
        self.user_requests = set()
        self.lock = threading.Lock()
        self.process = None
        self.ready = False  # The running process said its conversation (init).
        self.closing = False
        self.pending = []  # (text, event id), until a process takes them
        self.reaper = None  # The thread that stops what a stopped process left running, once it has exited.
        self.stopping = False

    def steer(self, text, event_id):
        with self.lock:
            refuse_steer(text, closing=self.closing, ready=self.ready or self.stopping, label="Antigravity")
            self.pending.append((steer_text(text), event_id))
            if self.stopping:
                return  # Already stopping for an earlier one: both go on together.
            process = self.process
            if process is None or process.poll() is not None:
                return  # Between two processes: the next one takes it.
            self.stopping = True
            left = descendants(process.pid)
            try:
                os.kill(process.pid, signal.SIGINT)
            except OSError:
                pass
            # Once agy has exited: a command it left may hold its output open, so this cannot wait for its end.
            self.reaper = threading.Thread(target=self.reap, args=(process, left), daemon=True)
            self.reaper.start()

    @staticmethod
    def reap(process, left):
        """Until agy has exited, keep adding what runs under it (a command it was starting as it was stopped), then
        stop what is left; one that will not exit in STOP_SECONDS * 6 is stopped with its group."""
        seen = {identity["pid"]: identity for identity in left}
        deadline = time.monotonic() + STOP_SECONDS * 6
        while process.poll() is None and time.monotonic() < deadline:
            seen.update((identity["pid"], identity) for identity in descendants(process.pid))
            time.sleep(0.05)
        if process.poll() is None:
            stop_group(process, STOP_SECONDS)
        stop_left(list(seen.values()))

    def take(self):
        """The held supplements as the next process's message and their event ids; ('', []) when none."""
        with self.lock:
            reaper, self.reaper = self.reaper, None
        if reaper is not None:
            reaper.join()
        with self.lock:
            pending, self.pending = self.pending, []
            self.stopping, self.ready = False, False
        if not pending:
            return "", []
        return RESUMED + "\n\n".join(text for text, _ in pending), [event_id for _, event_id in pending]

    def started(self, process):
        with self.lock:
            self.process, self.ready = process, False

    def answer(self, request_id, answers):
        raise SteerRefused("Antigravity 没有原生问题")

    def reject_question(self, request_id, reason):
        raise SteerRefused("Antigravity 没有原生问题")

    def wait_for_answer(self, question):
        pass

    def close(self):
        with self.lock:
            self.closing, self.ready = True, False
            process = self.process
        if process is not None and process.poll() is None:
            stop_group(process, STOP_SECONDS)


def resume_argv(argv, session_id):
    """``argv`` resuming ``session_id``: its --conversation set to it."""
    argv = list(argv)
    if "--conversation" in argv:
        argv[argv.index("--conversation") + 1] = session_id
        return argv
    return [*argv, "--conversation", session_id]


def user_line(text):
    return json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n"


def run_stream(request, argv, stdin, env):
    """One job's turn: agy processes, the first with the prompt, each next one resuming the same conversation with
    the supplements that stopped the one before (AgyControl)."""
    ctx, expected_session = request.context, request.session_id
    state = ReplyState(ctx.options, request.inputs.run_id, request.publish)
    adapter = AntigravityEventAdapter(lambda kind, data: request.emit(kind, data, None))
    control = AgyControl(lambda kind, data: request.emit(kind, data, control))
    cancel_event = request.inputs.cancel_event
    deadline = time.monotonic() + ctx.timeout if ctx.timeout > 0 else None
    rows = StreamRows(request, adapter, expected_session, control)
    rc, stderr, forced_cleanup = None, "", False
    status, code, text = "failed", "root_incomplete", ""
    try:
        request.observers.control(control)
    except Exception:
        pass
    try:
        state.invalidate()
        while True:
            left = 0 if deadline is None else max(1, int(deadline - time.monotonic()))
            rc, stderr = stream_subprocess_lines(argv, stdin_text=stdin, timeout=left, on_line=rows.on_line,
                                                 env=env, cwd=str(ctx.workdir), cancel_event=cancel_event,
                                                 process_observer=rows.observe)
            message, event_ids = control.take()
            if not message or cancel_event.is_set() or rows.sid is None or rows.invalid or control.closing:
                break
            # Stopped for a supplement (or one came as it ended): the same conversation goes on with it.
            argv, stdin = resume_argv(argv, rows.sid), user_line(message)
            rows = StreamRows(request, adapter, rows.sid, control, resumed=True)
            adapter.stopped(time.time())
            for event_id in event_ids:
                control.emit("steer_read", {"event_id": event_id})
        status, code = stream_outcome(rows, rc, cancel_event)
        if code in ("process_exit", "root_incomplete") and rows.sid is None and auth_failure(stderr):
            code = "authentication_error"
        if status == "completed":
            text = rows.reply()
            state.preserve(rows.sid, ("antigravity-stream", rows.sid, state.run_id), ("final:" + state.run_id,), text)
    except JobCancelled:
        if not state.enabled:
            raise
        status, code = "cancelled", "cancelled"
    except subprocess.TimeoutExpired:
        if not state.enabled:
            raise subprocess.TimeoutExpired("agy", ctx.timeout) from None
        status, code = "timed_out", "timeout"
    except OSError:
        code = "process_exit" if rc is None else "transport_error"
    except Exception:
        code = "storage_failed" if state.storage_failed else "protocol_error"
    finally:
        with control.lock:
            control.closing, control.ready = True, False
        # Only an exceptional exit leaves the process unreaped; signal its private group.
        process = rows.process
        if process is not None and process.poll() is None:
            forced_cleanup = True
            stop_group(process, 5)
        try:
            request.observers.control(None)
        except Exception:
            pass
    if status != "completed":
        try:
            request.emit("backend_failure", {"status": status, "code": code, "stage": "turn",
                                             **({"exit_code": rc} if type(rc) is int else {}),
                                             "stderr": stderr_lines(stderr)}, None)
        except Exception:
            pass
    observed = process.poll() if process is not None else None
    return state.finish(text, rows.sid or expected_session, status, code,
                        identity=bool(rows.sid and rows.result is not None and not rows.invalid),
                        cleanup=not forced_cleanup and type(observed) is int and (rc is None or observed == rc),
                        cancel_event=cancel_event, emit=request.emit)


HARNESS_REGISTRY.register("agy", AntigravityBackend)
