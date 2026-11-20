"""Antigravity (Google's ``agy`` CLI): Gemini and the other models of the owner's Google account, headless.

One turn is one ``agy --output-format stream-json --input-format stream-json`` process: the prompt goes
in as one ``user`` line on stdin, which is then closed, so agy ends after that turn. Its rows are one
``init`` (the conversation id, the native session), ``step_update`` rows (text deltas, tool steps) and one
``result`` with the turn's answer and status. ``--conversation <id>`` continues the topic's conversation.
agy signs in with the Google account it was logged in with on this machine (``agy`` once, interactively);
the Bridge never sees the token. It has no question a headless run can answer, so a task asks through
the checkpoint file like dsh and Pi; stdin takes text only, so images are given by their path.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

from covey.core.harness import (BackendConfig, BackendRuntime, HARNESS_REGISTRY, HarnessCapabilities, HarnessSpec,
                                JobCancelled, UUID_RE)
from covey.core import env as cenv
from covey.core.process import stop_group, stream_subprocess_lines
from covey.core.process_env import child_process_env
from covey.core.reply_state import ReplyState
from covey.core.result import MAX_REPLY_BYTES
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
TOOL_NAMES = {"run_command": "bash", "view_file": "read", "view_file_outline": "read", "view_code_item": "read",
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
    (``total_tokens`` is input plus output; seen 2026-11-20 on 1.5.13), its output counts the thinking."""
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
        self.texts = {}  # step_index -> the text streamed so far
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
        key = f"step-{index}"
        if kind == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                if not self.first_output:
                    self.first_output = True
                    self.emit("model_output", {"received_at": now})
                self.texts[index] = self.texts.get(index, "") + delta
                if self.texts[index].strip():
                    self.notes.text(key, self.texts[index])
            if step.get("state") == "DONE":
                # One model request: its usage and the time it took (thinking and text, no tool).
                seconds = step.get("duration_seconds")
                self.add_usage(step.get("usage"), seconds if isinstance(seconds, (int, float)) else None)
        elif kind == "tool":
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            native = str(step.get("tool_name") or info.get("name") or "tool")
            if key not in self.steps:
                self.notes.to_notes(now)
                self.steps.start(key, TOOL_NAMES.get(native, native),
                                 tool_detail(tool_inputs(info.get("parameters")), DETAIL_KEYS)[:500], now)
            if step.get("state") == "DONE":
                self.steps.finish(key, not info.get("error"), now)

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
        return HarnessCapabilities()

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
        return run_stream(request, self.argv(ctx, request.session_id), stdin, self.environment(ctx))

    def prompt_runtime_note(self, ctx):
        return ("Antigravity permission mode: always-proceed (--dangerously-skip-permissions)" if ctx.skip_permissions
                else "Antigravity permission mode: request-review; an action that needs approval is skipped")

    def startup_lines(self, ctx):
        return [f"agy_cmd={ctx.backend_cmd} model={ctx.model or '(agy default)'} effort={ctx.effort or '(default)'} "
                f"home={native_home()}"]


class StreamRows:
    """What one run printed, fed line by line by the shared reader."""

    def __init__(self, request, adapter, expected_session):
        self.request, self.adapter, self.expected_session = request, adapter, expected_session
        self.sid = self.result = None
        self.invalid = False
        self.process = None

    def observe(self, p):
        self.process = p
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


def run_stream(request, argv, stdin, env):
    ctx, expected_session = request.context, request.session_id
    state = ReplyState(ctx.options, request.inputs.run_id, request.publish)
    adapter = AntigravityEventAdapter(lambda kind, data: request.emit(kind, data, None))
    rows = StreamRows(request, adapter, expected_session)
    cancel_event = request.inputs.cancel_event
    rc, stderr, forced_cleanup = None, "", False
    status, code, text = "failed", "root_incomplete", ""
    try:
        state.invalidate()
        rc, stderr = stream_subprocess_lines(argv, stdin_text=stdin, timeout=ctx.timeout, on_line=rows.on_line,
                                             env=env, cwd=str(ctx.workdir), cancel_event=cancel_event,
                                             process_observer=rows.observe)
        status, code = stream_outcome(rows, rc, cancel_event)
        if code in ("process_exit", "root_incomplete") and rows.sid is None and auth_failure(stderr):
            code = "authentication_error"
        if status == "completed":
            text = rows.result["response"].strip()
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
        # Only an exceptional exit leaves the process unreaped; signal its private group.
        process = rows.process
        if process is not None and process.poll() is None:
            forced_cleanup = True
            stop_group(process, 5)
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
