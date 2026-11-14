"""Pi (pi, ``@earendil-works/pi-coding-agent``, formerly ``@mariozechner/pi-coding-agent``): GPT models from the Codex login or from a model gateway.

One turn is one ``pi --mode rpc`` process (backends/pi/rpc.py) with a private home made for it
(backends/pi/home.py). Two providers: Codex's own ChatGPT login (pi's ``openai-codex``, the
account's models) and a gateway's Responses models (``<prefix><model>``) behind the same BusyRelay as
the Codex Harness, which holds every 429 and asks the gateway again until another answer comes. pi
has no native question tool, so a task asks through the checkpoint file; it has no sub-agents,
so Codex's ``ultra`` budget is not offered. Sessions live in one root and are always resumed or
forked by their file's path: a bare id from another directory makes pi ask on the terminal.
"""
from __future__ import annotations

import dataclasses
import shlex
from pathlib import Path

from covey.core import env as cenv
from covey.backends.gateway import catalog, connection
from covey.core import gateways
from covey.backends.pi import home
from covey.backends.pi.rpc import run_rpc
from covey.core.harness import (BackendConfig, BackendRuntime, HarnessCapabilities, HARNESS_REGISTRY,
                                         HarnessSpec, UUID_RE)
from covey.core.attachments import MAX_IMAGE_BYTES
from covey.core.paths import component_dir
from covey.core.process_env import child_process_env
from covey.core.reply_state import ReplyState
from covey.core.state import ensure_private_dir

DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "high"
SESSIONS = "pi-sessions"

PI_SPEC = HarnessSpec(
    name="pi", title="Pi", prompt_agent_name="Pi", idempotency_prefix="pi-lark",
    env_prefix="LARK_PI_BRIDGE", state_dir_name="pi-lark-bridge", session_filename="pi_session_id",
    session_pattern=UUID_RE,
    default_profile=cenv.get("COVEY_PROFILE", "").strip() or None)  # The Codex Harness's profile.

# The failure card's detail where the shared words would mislead.
AUTH_TEXT = ("ChatGPT 拒绝了 Codex 登录的访问令牌（401/403）。Pi 只借用这个令牌，不会刷新它：在 Codex Harness "
             "跑一轮任意任务刷新登录，再重试本轮。")


def busy_text(gateway):
    """The relay holds the gateway's 429s and its in-stream capacity errors, so what still ends a turn this way came
    where the relay does not look (after the output began) or pi did not retry: no word on retries spent."""
    return (f"{gateway.display} 返回繁忙或限速，本轮没有完成，与任务内容无关。"
            + (gateway.busy_hint + "。" if gateway.busy_hint else ""))


class PiSessionStore:
    """pi's sessions: ``<root>/<timestamp>_<id>.jsonl``, the root under the bridge's state."""

    def __init__(self):
        self.root = None

    def bind_state_dir(self, state_dir):
        self.root = Path(state_dir) / SESSIONS

    def sessions_dir(self, workdir=None):
        return self.root or component_dir(SESSIONS)

    def transcript(self, workdir, session_id):
        return session_file(self.sessions_dir(workdir), session_id)


def sessions_root(ctx):
    return Path(ctx.state_dir) / SESSIONS


def session_file(root, session_id):
    """The session's file, the id after the file name's timestamp. Under ``--session-dir`` pi (0.73.1 and 1.5.2) writes it
    in that directory itself (checked 2026-11-04); its default root has a directory per cwd."""
    if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
        return None
    root = Path(root)
    return next(iter(sorted(root.glob(f"*_{session_id}.jsonl")) + sorted(root.glob(f"*/*_{session_id}.jsonl"))), None)


def account_catalog():
    """The Codex account's GPT models, without ``ultra``."""
    from covey.core.model_settings import model_catalog
    return {model: budgets(row) for model, row in model_catalog().items() if model.startswith("gpt")}


def budgets(row):
    return {**row, "supported_reasoning_levels": [level for level in row.get("supported_reasoning_levels", [])
                                                  if level.get("effort") in home.BUDGETS]}


def gateway_catalog(gateway, models):
    return {model: budgets(row) for model, row in catalog.launch_catalog(gateway, models, {}).items()
            if model[len(gateway.prefix):].startswith("gpt")}


def gateway_models(ctx, fetch):
    """The Pi gateways' models; one that is unconfigured or unreachable adds none (it never hides the account's)."""
    found = {}
    for gateway in gateways.for_harness("pi"):
        try:
            found.update(gateway_catalog(gateway, fetch(gateway, ctx.state_dir, connection.RESPONSES)))
        except ValueError:
            pass
    return found


class PiBackend(BackendRuntime):
    def add_cli_args(self, parser):
        parser.add_argument("--pi-cmd", default="pi")
        parser.add_argument("--pi-timeout", type=int, default=0)
        parser.add_argument("--model", default=DEFAULT_MODEL)
        parser.add_argument("--reasoning-effort", default=DEFAULT_EFFORT)

    def build_config(self, args):
        return BackendConfig(args.model, args.reasoning_effort, args.pi_timeout, shlex.split(args.pi_cmd), True,
                             {"harness_id": "pi"})

    def capabilities(self, ctx):
        return HarnessCapabilities(image_bytes=MAX_IMAGE_BYTES, steering=True, native_fork=True, model_requests=True,
                                   steer_reads=True)

    def model_catalog(self, ctx):
        return {**account_catalog(), **gateway_models(ctx, connection.cached_catalog)}

    def refresh_model_catalog(self, ctx):
        """The account's models, and the gateways' when they answer: an unreachable gateway never hides the account's."""
        return {**account_catalog(), **gateway_models(ctx, connection.refresh_catalog)}

    def check(self, ctx, account):
        """The task's model and budget, before pi starts."""
        home.thinking(ctx.effort)
        gateway = gateways.for_model("pi", ctx.model)
        if gateway is not None:
            connection.check_model(gateway, ctx.state_dir, connection.RESPONSES, ctx.model[len(gateway.prefix):],
                                   ctx.effort)
            return
        row = account.get(ctx.model)
        if row is None:
            raise ValueError("模型已不在 Codex 账号的模型目录中；请在主聊天发送新消息，在启动卡上选择其他模型")
        if ctx.effort not in {level.get("effort") for level in row["supported_reasoning_levels"]}:
            raise ValueError("此模型不支持所选推理档位")

    def session_args(self, ctx, session_id):
        """Resume or fork by the session file's path, never by a bare id."""
        source = session_id or ctx.options.get("fork_session")
        if not source:
            return []
        path = session_file(sessions_root(ctx), source)
        if path is None:
            raise ValueError("找不到 Pi 的会话文件，无法续接；请在本话题重新发送消息开始新会话")
        return ["--session" if session_id else "--fork", str(path)]

    def argv(self, ctx, session_id):
        gateway = gateways.for_model("pi", ctx.model)
        model = ctx.model[len(gateway.prefix):] if gateway else ctx.model
        sessions = sessions_root(ctx)
        ensure_private_dir(sessions)
        return [*ctx.backend_cmd, "--mode", "rpc", "--session-dir", str(sessions), *self.session_args(ctx, session_id),
                "--provider", gateway.name if gateway else home.CODEX_PROVIDER, "--model", model,
                "--thinking", home.thinking(ctx.effort)]

    def run(self, request):
        request.check_cancelled()
        ctx = request.context
        account = account_catalog()
        self.check(ctx, account)
        argv = self.argv(ctx, request.session_id)
        gateway = gateways.for_model("pi", ctx.model)
        if gateway is None:
            try:
                token = home.access_token()
            except ValueError as exc:
                return refused(request, "authentication_error", str(exc))
            texts = {"authentication_error": AUTH_TEXT}
            return self.turn(request, argv, account, "", token, texts, "Pi")
        # Lives exactly as long as this turn; its waits show on the card like the Codex Harness's.
        with catalog.turn_relay(request, gateway, ctx.state_dir) as relay:
            return self.turn(request, argv, account, relay.base_url, "", {"rate_limited": busy_text(gateway)},
                             f"Pi ({gateway.display})")

    def turn(self, request, argv, account, relay_url, token, texts, label):
        ctx = request.context
        with home.TurnHome(ctx.state_dir, ctx.model, ctx.effort, account, relay_url) as turn_home:
            env = home.environment(child_process_env(ctx.workdir), ctx.model, turn_home.path, token)
            options = {**ctx.options, "error_texts": {**ctx.options.get("error_texts", {}), **texts}}
            request = dataclasses.replace(request, context=dataclasses.replace(ctx, options=options))
            return run_rpc(request, argv, env, context_window=home.context_window(ctx.model, account), label=label)

    def prompt_runtime_note(self, ctx):
        return "Pi has no sandbox: every command runs with the bot user's full permissions"

    def startup_lines(self, ctx):
        return [f"pi_cmd={ctx.backend_cmd} model={ctx.model} reasoning_effort={ctx.effort} "
                f"sessions={sessions_root(ctx)}"]

    def on_startup(self, ctx):
        ensure_private_dir(ctx.state_dir / "tmp")


def refused(request, code, text):
    """A turn that never started pi, failed with the route's own words for ``code``."""
    ctx = request.context
    state = ReplyState({**ctx.options, "harness_id": "pi", "harness_label": "Pi", "error_texts": {code: text}},
                       request.inputs.run_id, request.publish)
    try:
        state.invalidate()
    except Exception:
        pass
    return state.result("", request.session_id, status="failed", code=code, cleanup=True, protection=True)


HARNESS_REGISTRY.register("pi", PiBackend)
