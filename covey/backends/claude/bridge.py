#!/usr/bin/env python3
"""The Claude Code Harness: its spec and backend.

All bridge behavior lives in the components of ``covey.app.app``; this file
only describes the Claude Code Harness: every turn runs ``claude -p`` with stream-json
input and output in ``--workdir`` (``backends/claude/runtime.py``), multi-turn
continuity via ``--resume <session_id>``; the bridge rules travel at the system level, appended to Claude Code's own
system prompt on every invocation (``system_instructions``, app/rules_sent.py); a resumed
session compacted before the turn when asked (``backends/claude/compaction.py``). A topic on a removed provider's model
(core/providers.py RETIRED_PREFIXES) is refused before Claude Code starts.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import shlex
from typing import Any

from covey.core import extensions, home
from covey.core.harness import BackendRuntime, HarnessCapabilities, HARNESS_REGISTRY, for_device

from covey.core.process import device_of
from covey.core.process_env import child_process_env, process_device
from covey.core.harness import BackendConfig, BridgeContext, HarnessSpec, RunRequest, RunResult, UUID_RE
from covey.backends.claude.models import model_catalog, with_extension_models
from covey.backends.claude import rewind, upstream
from covey.backends.claude.argv import TURN_HOOKS, merge_hooks
from covey.core.providers import retired_provider, retired_provider_notice

STATE_DIR_NAME = "claude-lark-bridge"

# Literal defaults: the LARK_CLAUDE_BRIDGE_* variables that set them are no longer read (core/ignored_env.py).
DEFAULT_MODEL = "claude-fable-5"
DEFAULT_EFFORT = "xhigh"
DEFAULT_CLAUDE_CMD = "claude"
DEFAULT_CLAUDE_TIMEOUT = 0
SKIP_PERMISSIONS_DEFAULT = True
LOGIN_SHELL_DEFAULT = False
# Claude Code compacts its context when it nears this many tokens instead of near the model's window.
AUTO_COMPACT_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
# With this set, Claude Code behind a gateway (ANTHROPIC_BASE_URL) also lists the gateway's /v1/models
# (claude-opus-5, …) after its four tier aliases; it reads them from its cache and refreshes the
# cache in the background (2.1.310). Without a gateway it does nothing (the launch card had no Opus 5).
GATEWAY_MODELS_ENV = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"

CLAUDE_SPEC = HarnessSpec(
    name="claude",
    title="Claude Code",
    prompt_agent_name="Claude Code",
    idempotency_prefix="claude-lark",
    env_prefix="LARK_CLAUDE_BRIDGE",
    state_dir_name=STATE_DIR_NAME,
    session_filename="claude_session_id",
    session_pattern=UUID_RE,
    default_profile=None,  # Always the Bridge's --profile (app/unified.py).
)


class ClaudeBackend(BackendRuntime):
    def add_cli_args(self, parser: Any) -> None:
        # compat(ab0ce25): options that no longer do anything, kept so a harnesses.json written before still
        # parses: --claude-transport, whose one-shot cli transport went in ab0ce25 (only stream-json is accepted),
        # and the TRAE provider's --traex-ppe-env (0fec8a0) and --traex-site (4ed3b95), unlisted, which went with
        # it; retire when no harnesses.json passes one (compat_report: removed-options)
        parser.add_argument("--claude-transport", choices=["stream-json"], default="stream-json")
        parser.add_argument("--traex-ppe-env", help=argparse.SUPPRESS)
        parser.add_argument("--traex-site", help=argparse.SUPPRESS)
        # --claude-profiles went with the Claude gateway profiles: this machine's own Claude Code
        # login is what runs; its value is ignored and startup logs one line (same check as the options above)
        parser.add_argument("--claude-profiles", help=argparse.SUPPRESS)
        parser.add_argument("--model", default=DEFAULT_MODEL, help="claude --model (default: claude-fable-5)")
        parser.add_argument(
            "--effort",
            default=DEFAULT_EFFORT,
            help="claude --effort: low|medium|high|xhigh|max|ultracode (default: xhigh)",
        )
        parser.add_argument("--claude-cmd", default=DEFAULT_CLAUDE_CMD, help="claude executable / wrapper command")
        parser.add_argument(
            "--claude-timeout",
            type=int,
            default=DEFAULT_CLAUDE_TIMEOUT,
            help="max seconds per background claude job; <=0 disables the timeout",
        )
        parser.add_argument(
            "--no-skip-permissions",
            action="store_true",
            help="do NOT pass --dangerously-skip-permissions to claude",
        )
        parser.add_argument(
            "--claude-login-shell",
            action="store_true",
            default=LOGIN_SHELL_DEFAULT,
            help="start claude through `bash -ic`, for an environment that interactive shell setup provides; "
                 "the command must still be on the Bridge's PATH or absolute, since the preflight checks do not "
                 "go through that shell (default: off)",
        )
        parser.add_argument(
            "--claude-auto-compact-window",
            type=_token_count,
            metavar="TOKENS",
            help=f"set {AUTO_COMPACT_ENV} for the claude child (default: unset, its environment is left as is)",
        )
        parser.add_argument(
            "--claude-compact-between-turns",
            type=_token_count,
            metavar="TOKENS",
            help="compact a resumed session before the turn when its context at the end of the last turn "
                 "reached TOKENS (default: only when the Bridge asks, as the butler's threshold does)",
        )

    def build_config(self, args: Any) -> BackendConfig:
        options = {"transport": "stream-json"}
        if args.claude_auto_compact_window:
            options["auto_compact_window"] = args.claude_auto_compact_window
        if args.claude_compact_between_turns:
            options["compact_between_turns"] = args.claude_compact_between_turns
        if args.claude_login_shell:
            options["login_shell"] = True
        if args.claude_profiles:
            options["ignored_claude_profiles"] = True
        return BackendConfig(
            model=args.model,
            effort=args.effort,
            timeout=args.claude_timeout,
            backend_cmd=shlex.split(args.claude_cmd),
            skip_permissions=not args.no_skip_permissions and SKIP_PERMISSIONS_DEFAULT,
            options=options,
        )

    def child_env(self, ctx: BridgeContext) -> dict[str, str]:
        """The Bridge's environment for a claude child, with the gateway's models unless the Bridge's
        environment names its own value; the compaction window only when configured."""
        env = child_process_env(ctx.workdir, device=process_device(ctx))
        env.update(home.env(ctx))  # COVEY_HOME, COVEY_TOPIC, COVEY_STATE and <数据根>/bin on PATH (core/home.py).
        env.setdefault(GATEWAY_MODELS_ENV, "1")
        # Every turn backs up the files it edits, so ↩️ 回退 can put them back (backends/claude/rewind.py).
        env.setdefault(rewind.FILE_CHECKPOINTS_ENV, "1")
        window = ctx.options.get("auto_compact_window")
        if window:
            env[AUTO_COMPACT_ENV] = str(window)
        return env

    def wrap_argv(self, argv: list[str], ctx: BridgeContext) -> list[str]:
        if device_of(ctx):  # The device's login shell is the spawn request's ``shell`` (core/device_link.py).
            return argv
        if not ctx.options.get("login_shell"):
            return argv
        return ["bash", "-ic", shlex.join(argv)]

    def capabilities(self, ctx):
        from covey.backends.claude.process import IMAGE_FRAME
        from covey.core.attachments import MAX_IMAGE_BYTES
        return for_device(HarnessCapabilities(questions=True, image_bytes=MAX_IMAGE_BYTES, image_frame=IMAGE_FRAME, steering=True,
            native_fork=True, rate_limit_retry=True, incomplete_retry=True, fault_retry=True, steer_reads=True, compaction=True,
            command_stop=True, session_files=True, rewind=True, bridge_tools=True, read_only=True,
            # 协作 asks held to CONSULT in a fork (C8 on the installed CLI: tests/test_collab_c8.py).
            collab=True,
            # question_tool: backends/claude/control.py answers the root's AskUserQuestion; nudges: TurnHooks' PostToolUse.
            question_tool="AskUserQuestion", nudges=True,
            # The bridge rules by --append-system-prompt on every invocation: a session keeps its first one's, a compaction
            # renders them again from the invocation's own (Claude Code 2.1.331, app/rules_sent.py).
            system_instructions=True, system_refresh="compaction"), ctx)

    def model_catalog(self, ctx):
        return with_extension_models(model_catalog(self, ctx, self.child_env(ctx)))

    def refresh_model_catalog(self, ctx):
        return with_extension_models(model_catalog(self, ctx, self.child_env(ctx), refresh=True))

    def normalize_model_settings(self, ctx, model, effort):
        if not model:
            return model, effort
        catalog = self.model_catalog(ctx)
        if model not in catalog:
            raise ValueError("模型已不在 Claude 目录中；请在主聊天发送新消息，在启动卡上选择其他模型")
        levels = {item["effort"] for item in catalog[model]["supported_reasoning_levels"]}
        if effort and effort not in levels:
            raise ValueError("该 Claude 模型不支持此推理档位")
        return model, effort

    def retired_gateway(self, ctx):
        """Whether the topic's model is a "<gateway>/<model>" id not on the account's menu (a gateway model removed
        since the topic chose it), whose id would otherwise go to the account's Claude Code. One the account's
        gateway lists under a prefixed id (such as claude/claude-opus-5, e8c34ee) runs; when the menu cannot be read, Claude Code says."""
        model = str(ctx.model or "")
        if "/" not in model:
            return False
        try:
            return model not in self.model_catalog(ctx)
        except (RuntimeError, OSError, ValueError):
            return False

    def run(self, request: RunRequest) -> RunResult:
        request.check_cancelled()
        # Also a job submitted before the provider was removed (core/model_settings.py refuses new ones).
        removed = retired_provider(request.context.model)
        if removed:
            raise ValueError(retired_provider_notice(removed))
        if self.retired_gateway(request.context):
            raise ValueError("本话题的模型所在网关已下线，不能再用它；请点任务卡上的“换配置”选择其他模型")
        return extension_stream(self, request, self.child_env(request.context))

    def compact_session(self, request: RunRequest) -> bool:
        """``/compact`` on ``request.session_id`` and nothing else (the butler's idle compaction), in the turn's
        environment and inside the extensions' ``claude_turn`` hooks (backends/claude/runtime.py compact_only)."""
        from covey.backends.claude.runtime import compact_only
        if device_of(request.context):
            return False  # The session is on a device: its file is not ours to read (compact_before_turn).
        ctx, env = request.context, self.child_env(request.context)
        with _extension_turn(ctx, env, run_id=request.inputs.run_id) as (ctx, env):
            return compact_only(self, dataclasses.replace(request, context=ctx), env)

    def handoff_summary(self, ctx, session_id, prompt, *, cancel=None, run_id=""):
        """(text, outcome) of a handoff summary on a fork of ``session_id`` (backends/claude/handoff.py), for the
        first run after 🔁 换配置 away from this session (app/switch_handoff.py), inside the extensions'
        ``claude_turn`` hooks as a turn is."""
        from covey.backends.claude.handoff import summarize
        # The same refusals as ``run``: a removed provider's or gateway's model is never asked, not even for a summary.
        removed = retired_provider(ctx.model)
        if removed:
            return "", f"原来的模型来自已下线的供应商「{removed}」，没有发送"
        if self.retired_gateway(ctx):
            return "", "原来的模型所在网关已下线，没有发送"
        env = self.child_env(ctx)
        with _extension_turn(ctx, env, run_id=run_id) as (ctx, env):
            return summarize(self, ctx, session_id, prompt, env, cancel=cancel)

    @contextlib.contextmanager
    def one_shot(self, ctx, env, *, run_id=""):
        """(ctx, env, settings) for one question to a Claude Code started by the Bridge outside a turn (the 辅助模型's
        one-shot, app/track_judge.py): the extensions' ``claude_turn`` hooks around it as around a turn
        (``_extension_turn``; covey-internal's protocol-normalization relay, COVEY_CLAUDE_CLI_NORMALIZE=1, is one: a
        machine that reaches its model only through it would have every background question refused upstream without
        it), and ``settings`` its ``--settings``: no hook of the user's (``disableAllHooks``) and, when a hook gave the
        turn a relay (backends/claude/upstream.py SETTINGS_ENV: its ANTHROPIC_BASE_URL), what points Claude Code at it.
        The hooks' own Claude Code hooks (TURN_HOOKS) are not taken: the question runs none. Whatever the hooks started
        ends when the block does."""
        with _extension_turn(ctx, env, run_id=run_id) as (ctx, env):
            yield ctx, env, {"disableAllHooks": True, **upstream.settings(ctx)}

    def subagent_report(self, ctx, session_id, since):
        """Each subagent of the session's turn from ``since``: its description, end and last text (backends/claude/subagent_report.py)."""
        from covey.backends.claude.subagent_report import report
        return report(ctx, session_id, since, self.child_env(ctx))

    def rewind_point(self, ctx, session_id, prompt_uuid):
        """Where dropping the turn ``prompt_uuid`` began leaves the session (backends/claude/rewind.py)."""
        return rewind.rewind_point(ctx, session_id, prompt_uuid, self.child_env(ctx))

    def rewind_files(self, ctx, session_id, prompt_uuid):
        """Put back the files that turn's edit tools changed."""
        return rewind.rewind_files(self, ctx, session_id, prompt_uuid, self.child_env(ctx))

    def prompt_runtime_note(self, ctx: BridgeContext) -> str:
        if ctx.skip_permissions:
            return "Claude Code 权限模式：bypass（--dangerously-skip-permissions）"
        # backends/claude/control.py answers the root AskUserQuestion and denies anything else.
        return "Claude Code 权限模式：default，桥接器只回答 AskUserQuestion，其他权限请求一律拒绝"

    def prompt_notes(self, ctx: BridgeContext) -> tuple[str, ...]:
        return self._completion_notes(ctx)

    def _completion_notes(self, ctx: BridgeContext) -> tuple[str, ...]:
        wait, total = ctx.options.get("completion_wait_seconds"), ctx.options.get("completion_total_seconds")
        limits = ([f"每次回复后 {_duration(wait)}"] if wait else []) + (
            [f"第一次回复后 {_duration(total)}"] if total else []) + (
            [f"任务超时（{_duration(ctx.timeout)}）"] if ctx.timeout > 0 else [])
        # A reply that only says "still waiting" would stand as the answer once the work has ended.
        last_reply = ("你的最后一条回复就是用户留下的答案，所以每条回复都要能单独成立；"
                      "答案要用到后台工作的结果时，等它出来再结束这一轮。")
        if not limits:
            return ("回复之后还在跑的后台命令或 Agent 会让这个任务一直开着，直到它们结束，没有时限，在那之前用户看到的都是在做。"
                    "只为会自己结束的事（等待、构建、检查）开后台任务，用不上了就停掉，不要在后台留下服务或 `tail -f`。"
                    + last_reply,)
        return ("回复之后还在跑的后台命令或 Agent 会让这个任务一直开着。"
                f"桥接器最多等到{'或'.join(limits)}；之后停掉 Claude 和这些任务，把你最后一条完整的回复连同一条说明发出去。"
                "不要开预计会超过这个时间的后台等待，改在回复里说用户什么时候再问。" + last_reply,)

    def startup_lines(self, ctx: BridgeContext) -> list[str]:
        window, between = ctx.options.get("auto_compact_window"), ctx.options.get("compact_between_turns")
        return [
            f"claude_cmd={ctx.backend_cmd} model={ctx.model or '(default)'} "
            f"effort={ctx.effort or '(default)'} skip_permissions={ctx.skip_permissions} "
            f"login_shell={bool(ctx.options.get('login_shell'))} transport=stream-json"
            + (f" auto_compact_window={window}" if window else "")
            + (f" compact_between_turns={between}" if between else "")
        ] + (["--claude-profiles is no longer used (Claude gateway profiles were removed; this machine's own Claude "
              "Code login runs); its value is ignored"] if ctx.options.get("ignored_claude_profiles") else [])

    def on_startup(self, ctx: BridgeContext) -> None:
        return None




@contextlib.contextmanager
def _extension_turn(ctx, env, *, run_id=""):
    """Optional extension hooks around a Claude Code turn (core/extensions.py ``claude_turn``): every enabled
    package's, nested in the order they are enabled (the first is the outermost, so the last one's context and
    environment reach Claude Code). The Claude Code hooks each puts under argv.TURN_HOOKS are its own (it is handed the
    context without the ones outside it gave); they are merged by event in the same order, so no package's replace
    another's. ExtensionError, with the package, for hooks of another shape."""
    given = [ctx.options.get(TURN_HOOKS)]
    with contextlib.ExitStack() as stack:
        for package, hook in extensions.hooks("claude_turn"):
            if TURN_HOOKS in ctx.options:
                ctx = dataclasses.replace(ctx, options={k: v for k, v in ctx.options.items() if k != TURN_HOOKS})
            ctx, env = stack.enter_context(hook.turn(ctx, env, run_id=run_id))
            try:
                given.append(merge_hooks(ctx.options.get(TURN_HOOKS)))
            except ValueError as error:
                raise extensions.ExtensionError(package, str(error)) from None
        merged = merge_hooks(*given)
        if merged:
            ctx = dataclasses.replace(ctx, options={**ctx.options, TURN_HOOKS: merged})
        yield ctx, env


def extension_stream(backend, request, env):
    """``run_stream`` for the turn, inside the extensions' ``claude_turn`` hooks (``_extension_turn``)."""
    from covey.backends.claude.runtime import run_stream
    with _extension_turn(request.context, env, run_id=request.inputs.run_id) as (ctx, env):
        return run_stream(backend, dataclasses.replace(request, context=ctx), env)


def _token_count(value: str) -> int:
    try:
        tokens = int(value)
    except ValueError:
        tokens = 0
    if tokens <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of tokens, got {value!r}")
    return tokens


def _duration(seconds):
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


HARNESS_REGISTRY.register("claude", ClaudeBackend)
