"""DeepSeek Harness (dsh): a model gateway's Anthropic Messages models over dsh's headless profile.

One turn is one ``dsh --profile headless --json`` process. A private patch
overlay declares the Acme route and the chosen model; the personal SK
reaches dsh only through its environment. ``--session-id`` continues the
topic's native session. The headless profile has no question tool, so a task
asks through the checkpoint file like every other Harness.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

from covey.core import env as cenv
from covey.backends.gateway import connection
from covey.core import gateways
from covey.core.harness import BackendConfig, BackendRuntime, HARNESS_REGISTRY, HarnessSpec, JobCancelled
from covey.core.process import stop_group, stream_subprocess_lines
from covey.core.process_env import child_process_env
from covey.core.reply_state import ReplyState
from covey.core.result import MAX_REPLY_BYTES
from covey.core.state import ensure_private_dir
from covey.core.steps import AnswerNotes, Steps
from covey.core.text import tool_detail
from covey.core.usage import TurnTokens

SESSION_RE = re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The dsh provider route is the gateway's name; dsh's own routes are deepseek-official and the pi-ai catalog.
NO_GATEWAY = "没有配置给 dsh 用的模型网关（扩展的 GATEWAYS 或 harnesses.json 的 gateways），DeepSeek Harness 不可用"
# Native error codes the model service reports in turn_end.
AUTH_CODES = frozenset({"MISSING_CREDENTIAL", "INVALID_CREDENTIAL"})
LIMIT_CODES = frozenset({"QUOTA", "RATE_LIMIT"})


def native_home():
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()


DSH_SPEC = HarnessSpec(
    name="dsh", title="DeepSeek Harness", prompt_agent_name="DeepSeek Harness (dsh)",
    idempotency_prefix="dsh-lark", env_prefix="LARK_DSH_BRIDGE", state_dir_name="dsh-lark-bridge",
    session_filename="dsh_session_id", session_pattern=SESSION_RE,
    default_profile=cenv.get("COVEY_PROFILE", "").strip() or None)  # The Codex Harness's profile.


class DshSessionStore:
    def sessions_dir(self, workdir):
        return native_home() / "sessions"

    def transcript(self, workdir, session_id):
        # sessions/<encoded cwd>/<session id>/
        return next(iter(sorted(self.sessions_dir(workdir).glob(f"*/{session_id}"))), None)


def route_patch(gateway, catalog, model, effort):
    """The patch overlay: the gateway's route with every dsh model, and the task's model as the default."""
    models = []
    for name, row in catalog.items():
        levels = [r["effort"] for r in row["supported_reasoning_levels"]]
        entry = {"id": name, "name": row["display_name"], "reasoningEfforts": {e: e for e in levels} if levels else False}
        if levels:
            # The gateway reads the level from output_config.effort, sent with adaptive thinking.
            entry["compat"] = {"forceAdaptiveThinking": True}
        models.append(entry)
    return [
        {"id": "llm-pi-ai", "config": {"providers": {gateway.name: {
            "displayName": gateway.display, "api": connection.MESSAGES, "baseURL": gateway.url(),
            "apiKeyEnv": gateway.key_env, "headers": gateway.header_dict(), "models": models,
            # A 429 usually means the upstream is busy: retry it for minutes, not seconds.
            "retryPolicy": connection.RETRY_POLICY}}}},
        {"id": "agent-default-model", "config": {"provider": gateway.name, "model": model,
                                                 **({"reasoningEffort": effort} if effort else {})}},
        {"id": "session-title-llm", "disabled": True},  # No extra model request for a title nobody shows.
    ]


DETAIL_KEYS = ("command", "path", "file_path", "pattern", "query", "url")  # A step's detail, the first it has.


class DshEventAdapter:
    """``--json`` rows to public Harness events. Reasoning (``thinking``) is never published."""
    def __init__(self, emit):
        self.emit = emit
        self.steps = Steps(emit)
        self.notes = AnswerNotes(emit)
        self.texts = 0
        self.tokens = TurnTokens()
        # Generation time per step: from step_start to the step's first committed row.
        # dsh commits thinking, text and tool calls together when the model finishes;
        # tools run after that, before step_end, so their time is excluded.
        self.step_started = self.step_generated = None

    @property
    def tools(self):
        return self.steps.steps

    def accept(self, row, now):
        kind = row.get("type")
        if kind in {"thinking", "text", "tool_call"} and self.step_started is not None and self.step_generated is None:
            self.step_generated = now
        if kind == "status" and row.get("phase") == "step_start":
            self.step_started, self.step_generated = now, None
        elif kind == "tool_call" and isinstance(row.get("callId"), str) and row["callId"]:
            self.notes.to_notes(now)
            self.steps.start(row["callId"], str(row.get("tool") or "tool").lower(),
                             tool_detail(row.get("input"), DETAIL_KEYS)[:500], now)
        elif kind == "tool_result" and row.get("callId") in self.steps:
            self.steps.finish(row["callId"], row.get("status") == "completed", now)
        elif kind == "text" and isinstance(row.get("text"), str) and row["text"].strip():
            self.texts += 1
            self.notes.text("text-" + str(self.texts), row["text"])
        elif kind == "status" and row.get("phase") == "step_end" and isinstance(row.get("usage"), dict):
            usage = row["usage"]
            counts = {name: usage.get(field) for name, field in (("output", "outputTokens"), ("input", "inputTokens"),
                      ("read", "cacheReadTokens"), ("write", "cacheWriteTokens"))}
            seconds = None if self.step_started is None else max(0.0, (self.step_generated or now) - self.step_started)
            if not self.tokens.add(*counts.values(), seconds=seconds):  # No rate check, no context (as before).
                return
            self.step_started = self.step_generated = None
            # inputTokens is the uncached part; cache reads and writes are reported beside it.
            self.emit("token_usage", self.tokens.payload())


class DshBackend(BackendRuntime):
    def add_cli_args(self, parser):
        parser.add_argument("--dsh-cmd", default="dsh")
        parser.add_argument("--dsh-timeout", type=int, default=0)
        parser.add_argument("--model", default="ali-deepseek-v4.1-flash")
        parser.add_argument("--reasoning-effort", default="high")

    def build_config(self, args):
        return BackendConfig(args.model, args.reasoning_effort, args.dsh_timeout, shlex.split(args.dsh_cmd), True,
                             {"harness_id": "dsh"})

    @staticmethod
    def gateway():
        found = gateways.dsh_gateway()
        if found is None:
            raise ValueError(NO_GATEWAY)
        return found

    def model_catalog(self, ctx):
        found = gateways.dsh_gateway()
        return connection.cached_catalog(found, ctx.state_dir, connection.MESSAGES) if found else {}

    def refresh_model_catalog(self, ctx):
        found = self.gateway()
        catalog = connection.refresh_catalog(found, ctx.state_dir, connection.MESSAGES)
        if not catalog:
            raise ValueError(f"{found.display} 当前没有 dsh 可用的模型")
        return catalog

    def normalize_model_settings(self, ctx, model, effort):
        found = self.gateway()
        if not model:
            raise ValueError(f"dsh 必须选择 {found.display} 模型")
        connection.check_model(found, ctx.state_dir, connection.MESSAGES, model, effort)
        return model, effort

    def environment(self, ctx):
        env = connection.child_env(self.gateway(), child_process_env(ctx.workdir))
        # Full access matches Codex's default; dsh's headless approvals have nobody to ask.
        env.update(DSH_HOME=str(native_home()), DSH_PERMISSION_MODE="danger-full-access",
                   DSH_TELEMETRY_MODE="DISABLED")
        return env

    def run(self, request):
        request.check_cancelled()
        ctx, session_id = request.context, request.session_id
        found = self.gateway()
        catalog = connection.check_model(found, ctx.state_dir, connection.MESSAGES, ctx.model, ctx.effort)
        env = self.environment(ctx)
        tmp = ctx.state_dir / "tmp"
        ensure_private_dir(tmp)
        fd, patch = tempfile.mkstemp(prefix="dsh-patch-", suffix=".yml", dir=str(tmp))
        try:
            with os.fdopen(fd, "w") as handle:  # JSON is YAML; mkstemp already made it owner-only.
                json.dump(route_patch(found, catalog, ctx.model, ctx.effort), handle)
            argv = [*ctx.backend_cmd, "--profile", "headless", "--patch", patch, "--json",
                    *(["--session-id", session_id] if session_id else []), "-"]
            options = {**ctx.options, "harness_id": "dsh", "harness_label": "dsh",
                       "error_texts": {**ctx.options.get("error_texts", {}), "rate_limited": connection.busy_text(found)}}
            return run_headless(dataclasses.replace(request, context=dataclasses.replace(ctx, options=options)), argv, env)
        finally:
            try:
                os.unlink(patch)
            except OSError:
                pass

    def prompt_runtime_note(self, ctx):
        return "dsh permission mode: danger-full-access"

    def startup_lines(self, ctx):
        return [f"dsh_cmd={ctx.backend_cmd} model={ctx.model} reasoning_effort={ctx.effort} home={native_home()}"]

    def on_startup(self, ctx):
        ensure_private_dir(ctx.state_dir / "tmp")


class HeadlessRows:
    """What one headless run printed, fed line by line by the shared reader."""

    def __init__(self, request, adapter, expected_session):
        self.request, self.adapter, self.expected_session = request, adapter, expected_session
        self.session_observer = request.observers.session
        self.sid, self.final, self.reason = None, None, None
        self.started = self.invalid = self.driver_error = False
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
        kind = row.get("type")
        if kind == "error":
            self.driver_error = True  # A runner failure outside a turn; may precede the session row.
            return
        if kind == "session":
            value = row.get("sessionId")
            if (self.sid is not None or not isinstance(value, str) or not SESSION_RE.fullmatch(value)
                    or (self.expected_session is not None and value != self.expected_session)):
                self.invalid = True
                return
            self.sid = value
            if self.session_observer:
                try:
                    self.session_observer(self.sid)
                except Exception:
                    pass
            return
        if self.sid is None or self.final is not None:
            self.invalid = True
            return
        if kind == "status" and row.get("phase") == "turn_start":
            self.started = True
        elif kind == "status" and row.get("phase") == "turn_end":
            self.reason = row.get("reason") if isinstance(row.get("reason"), dict) else {}
        elif kind == "final":
            self.final = row.get("text") if isinstance(row.get("text"), str) else ""
        try:
            self.adapter.accept(row, time.time())
        except Exception:
            pass  # Observability never decides the outcome.


def headless_outcome(rows, rc, cancel_event):
    """(status, code) of a run whose output the reader has drained; ("completed", "") when
    ``rows.final`` is the reply."""
    error = (rows.reason or {}).get("error")
    native = error.get("code") if isinstance(error, dict) else None
    if rows.invalid:
        return "failed", "protocol_error"
    if native in AUTH_CODES:
        return "failed", "authentication_error"
    if native in LIMIT_CODES:
        return "failed", "rate_limited"
    if type(rc) is not int or rc != 0 or rows.driver_error:
        return "failed", "process_exit" if rows.reason is None else "root_failed"
    if not (rows.sid and rows.started and rows.reason is not None and rows.final is not None):
        return "failed", "root_incomplete"
    if rows.reason.get("kind") != "completed":
        return "failed", "root_failed"
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled", "cancelled"
    if not rows.final.strip() or len(rows.final.encode("utf-8")) > MAX_REPLY_BYTES:
        return "failed", "output_missing"
    return "completed", ""


def run_headless(request, argv, env):
    ctx, prompt, expected_session = request.context, request.prompt, request.session_id
    state = ReplyState(ctx.options, request.inputs.run_id, request.publish)
    adapter = DshEventAdapter(lambda kind, data: request.emit(kind, data, None))
    rows = HeadlessRows(request, adapter, expected_session)
    cancel_event = request.inputs.cancel_event
    rc, forced_cleanup = None, False
    status, code, text = "failed", "root_incomplete", ""
    try:
        state.invalidate()
        rc, _ = stream_subprocess_lines(argv, stdin_text=prompt, timeout=ctx.timeout, on_line=rows.on_line, env=env,
                                        cwd=str(ctx.workdir), cancel_event=cancel_event, process_observer=rows.observe)
        outcome = headless_outcome(rows, rc, cancel_event)
        if outcome[0] == "completed":
            text = rows.final.strip()
            state.preserve(rows.sid, ("dsh-headless", rows.sid, state.run_id), ("final:" + state.run_id,), text)
        status, code = outcome
    except JobCancelled:
        if not state.enabled:
            raise
        status, code = "cancelled", "cancelled"
    except subprocess.TimeoutExpired:
        if not state.enabled:
            raise subprocess.TimeoutExpired("dsh headless", ctx.timeout) from None
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
    observed = process.poll() if process is not None else None
    return state.finish(text, rows.sid or expected_session, status, code,
                        identity=bool(rows.sid and rows.started and not rows.invalid),
                        cleanup=not forced_cleanup and type(observed) is int and (rc is None or observed == rc),
                        cancel_event=cancel_event, emit=request.emit)


HARNESS_REGISTRY.register("dsh", DshBackend)
