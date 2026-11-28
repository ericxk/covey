#!/usr/bin/env python3
"""The Codex Harness: its spec and backend.

All bridge behavior lives in the components of ``covey.app.app``; this file
only describes the Codex Harness: every turn runs over ``codex app-server --stdio``
(``backends/codex/appserver.py``).
"""

from __future__ import annotations

import argparse
import dataclasses
import shlex
from typing import Any

from covey.core import env as cenv, home
from covey.core.harness import BackendRuntime, HarnessCapabilities, HARNESS_REGISTRY, for_device

from covey.core.process_env import child_process_env, process_device
from covey.core.process import device_of
from covey.core.harness import BackendConfig, BridgeContext, HarnessSpec, RunRequest, RunResult, UUID_RE
from covey.core.providers import retired_provider, retired_provider_notice
from covey.core.state import ensure_private_dir
from covey.backends.codex import gateway as gateway_models
from covey.backends.codex.appserver import QUESTION_TOOL, topic_data, run_appserver, sandbox_mode
from covey.backends.codex.consult import ConsultHome, ReadingHome
from covey.backends.codex import device_home
from covey.core import device_access, device_native, devices, topic_repos
from covey.core.topics import update_repos
from covey.core.harness import CONSULT, EXTERNAL, LEAD, READ, TOOL_LIMIT
from covey.backends.gateway.catalog import turn_relay
from covey.core import gateways

STATE_DIR_NAME = "codex-lark-bridge"

# Literal defaults: the COVEY_CODEX_* variables that set them are no longer read (core/ignored_env.py).
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_REASONING_EFFORT = "max"
DEFAULT_SANDBOX = "danger-full-access"
DEFAULT_CODEX_CMD = "codex"
DEFAULT_CODEX_TIMEOUT = 0
# A 交接摘要 not asked for: the old model would read the old session again without the term relay.


CODEX_SPEC = HarnessSpec(
    name="codex",
    title="Codex",
    prompt_agent_name="Codex",
    idempotency_prefix="codex-lark",
    env_prefix="COVEY",  # Read as COVEY_<X>, else the old LARK_BRIDGE_<X> (core/env.py).
    state_dir_name=STATE_DIR_NAME,
    session_filename="codex_session_id",
    session_pattern=UUID_RE,
    default_profile=cenv.get("COVEY_PROFILE", "").strip() or None,
)


class CodexBackend(BackendRuntime):
    def add_cli_args(self, parser: Any) -> None:
        parser.add_argument(
            "--codex-timeout",
            type=int,
            default=DEFAULT_CODEX_TIMEOUT,
            help="maximum seconds for one background Codex job; <=0 disables the bridge timeout",
        )
        parser.add_argument(
            "--sandbox",
            choices=["read-only", "workspace-write", "danger-full-access"],
            default=DEFAULT_SANDBOX,
        )
        parser.add_argument("--model", default=DEFAULT_MODEL)
        parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
        # compat(ab0ce25): the exec transport is gone; the option stays, accepting app-server only, so a
        # service command line written before (Codex is the default Harness: harnesses.json's "codex" entry is
        # never read) and the task runners' handoff argv copied from it still parse; retire when no service
        # command and no live runner's handoff passes it (compat_report: codex-transport)
        parser.add_argument("--codex-transport", choices=["app-server"], default="app-server")
        parser.add_argument("--codex-cmd", default=DEFAULT_CODEX_CMD, help="codex executable / wrapper command")
        # compat(58800eb): the Magpie provider was removed; its option stays,
        # unlisted and ignored, so a service command line written before and the runners' handoff argv copied from
        # it still parse, and a value set gets one line at the start; retire when no service command and no live
        # runner's handoff passes it (compat_report: codex-magpie-url)
        parser.add_argument("--codex-magpie-url", default="", help=argparse.SUPPRESS)

    def build_config(self, args: Any) -> BackendConfig:
        return BackendConfig(
            model=args.model,
            effort=args.reasoning_effort,
            timeout=args.codex_timeout,
            backend_cmd=shlex.split(args.codex_cmd),
            skip_permissions=True,
            options={"sandbox": args.sandbox, "transport": args.codex_transport,
                     **({"ignored_magpie_url": True} if getattr(args, "codex_magpie_url", "") else {})},
        )

    def sandbox(self, ctx: BridgeContext) -> str:
        return sandbox_mode(ctx)

    def capabilities(self, ctx):
        # collab: a 协作 ask held to CONSULT in a private CODEX_HOME (backends/codex/consult.py; C8 on the installed CLI:
        # tests/test_collab_c8.py, the exec-policy, apps, network and memory checks). rate_limit_retry, fault_retry: what
        # backends/codex/turn.py codex_failure_code tells apart. ask_once: backends/codex/judge.py.
        return for_device(HarnessCapabilities(questions=True, steering=True, native_fork=True, session_files=True,
                                              bridge_tools=True, read_only=True, collab=True, rate_limit_retry=True,
                                              fault_retry=True, ask_once=True,
                                              question_tool=QUESTION_TOOL,
                                              # The bridge rules in a new thread's developerInstructions, kept from its
                                              # start through compactions (codex-cli 0.172.0, app/rules_sent.py).
                                              system_instructions=True), ctx)

    def ask_once(self, ctx, system, data, *, limit, cwd=None, follow="", effort=""):
        """One question to Codex and nothing else (backends/codex/judge.py): the answer's text, or AskFailed. ``follow``:
        the model of the 辅助模型's setting (a model gateway's: the question goes to that gateway); ``effort``: its
        budget."""
        from covey.backends.codex.judge import ask_once
        return ask_once(ctx, system, data, limit=limit, cwd=cwd, follow=follow, effort=effort)

    def ask_model(self, ctx, *, follow=""):
        """The model id ``ask_once`` would run on (backends/codex/judge.py model_of: a gateway's with its prefix, Codex's own
        default as config.toml names it, '' when unknown); nothing is started or fetched."""
        from covey.backends.codex.judge import model_of
        return model_of(ctx, follow=follow)

    # The signed-in account's models plus the gateways' Responses models (<prefix><model>, core/gateways.py).
    def model_catalog(self, ctx: BridgeContext) -> dict:
        account = self.account_models(ctx)
        return {**account, **gateway_models.cached(ctx, account)}

    def refresh_model_catalog(self, ctx: BridgeContext) -> dict:
        account = self.account_models(ctx, refresh=True)
        return {**account, **gateway_models.refresh(ctx, account)}

    @staticmethod
    def account_models(ctx, *, refresh=False) -> dict:
        """The signed-in account's models: this machine's Codex home, or for a turn on a device (``ctx.device``) the
        device's own list, fetched with the shim's ``get`` (core/devices.py ``codex_root``)."""
        from covey.core.model_settings import model_catalog
        alias = device_of(ctx)
        if not alias:
            return model_catalog()
        from covey.core import devices
        return model_catalog(devices.codex_root(ctx.state_dir, alias, refresh=refresh))

    def run(self, request: RunRequest) -> RunResult:
        request.check_cancelled()
        ctx = request.context
        env = child_process_env(ctx.workdir, device=process_device(ctx))
        env.update(home.env(ctx))  # COVEY_HOME, COVEY_TOPIC, COVEY_STATE and <数据根>/bin on PATH (core/home.py).
        removed = retired_provider(ctx.model)
        if removed:  # A job queued on a removed provider's model before (core/providers.py RETIRED_PREFIXES).
            raise ValueError(retired_provider_notice(removed))
        limit = ctx.options.get(TOOL_LIMIT)
        native = None if device_of(ctx) else self.native(ctx)
        if native is not None:  # The topic kept a device (Codex 原生快路): every turn names its environments.
            ctx = dataclasses.replace(ctx, options={**ctx.options, "environments": native["environments"]})
            request = dataclasses.replace(request, context=ctx)
        if limit == CONSULT:
            # A 协作 ask: its app server in a CODEX_HOME of its own (backends/codex/consult.py), for this turn alone.
            with ConsultHome(ctx.state_dir, env) as env:
                return self._run(request, ctx, env)
        if limit in (READ, LEAD) and request.session_id and not device_of(ctx):
            # A group member's turn, or a project lead's, going on on the topic's thread: a home without the owner's
            # exec-policy rules (an allow rule runs a command outside the sandbox), hooks, plugins and memories
            # (ReadingHome). Only a resumed thread: one started or forked there is recorded under that home and never
            # found again once it is gone (tests/test_collab_c8.py), so a READ or LEAD turn starting the thread runs in the
            # owner's home, with READING_FLAGS alone. A device's home is the device's.
            with ReadingHome(ctx.state_dir, env) as env:
                return self._run(request, ctx, env)
        if native is not None and len(native["environments"]) > 1:
            # A device among the turn's environments: the app server in the device home (backends/codex/device_home.py),
            # whose environments.toml says how to reach each ready device; its G6 line in the task's devices.jsonl.
            env = device_home.prepare(ctx.state_dir, env, native["entries"])
            device_native.note_attached(native["turn"], native["environments"])
        try:
            return self._run(request, ctx, env)
        finally:
            if not device_of(ctx):
                self.keep_devices(ctx)

    @staticmethod
    def native(ctx):
        """The turn's Codex 原生快路 (core/device_native.py ``environments``): None for a topic that kept no device (and
        whenever the device switch is off), else ``{environments, entries, turn}``. Never raises: a turn that cannot
        tell runs as one whose topic kept nothing."""
        try:
            data = topic_data(ctx)
            if not devices.enabled() or not device_native.kept(data):
                return None
            config = devices.current()
            turn = device_native.turn_of(ctx.state_dir, topic_repos.task_dir_of(ctx.state_dir), ctx.topic_id)
            hub = device_native.hub_version(ctx.backend_cmd)
            local = not ctx.options.get(EXTERNAL) and device_native.topic_local(data)
            found = device_native.environments(turn, config, data, workdir=ctx.workdir, local=local, hub=hub)
            ready = {item["environmentId"] for item in found or ()}
            return {"environments": found, "turn": turn,
                    "entries": [item for item in device_native.entries(ctx.state_dir, config, hub) if item["id"] in ready]}
        except Exception:  # noqa: BLE001 - the turn runs on this machine alone, as before
            return None

    @staticmethod
    def keep_devices(ctx) -> None:
        """At the turn's end: the devices it used for the first time that a Codex turn may use as its environments
        (core/device_native.py ``due``) into ``topic.json`` ``device_envs``, so the topic's next turns have them. Nothing
        when the turn used no device; a failure changes nothing."""
        try:
            task_dir = topic_repos.task_dir_of(ctx.state_dir)
            data = topic_data(ctx)
            rows = device_access.read_audit(task_dir) if task_dir else []
            if not ctx.topic_id or not device_native.used(rows, data):
                return
            config = devices.current()
            if not config.devices:
                return
            turn = device_native.turn_of(ctx.state_dir, task_dir, ctx.topic_id)
            local = not ctx.options.get(EXTERNAL) and device_native.topic_local(data)
            added = device_native.due(turn, config, rows, data, local=local, hub=device_native.hub_version(ctx.backend_cmd))
            if added:
                update_repos(ctx.state_dir, ctx.topic_id,
                             lambda entries: device_native.kept({device_native.KEY: entries + added}),
                             key=device_native.KEY)
        except Exception:  # noqa: BLE001 - the topic keeps what it had
            pass

    def _run(self, request, ctx, env):
        gateway = gateways.for_model("codex", ctx.model)
        if gateway is not None:
            gateway_models.check(ctx, gateway, self.account_models(ctx))
            # Lives exactly as long as this turn; its waits show on the card like Codex's own retries.
            with turn_relay(request, gateway, ctx.state_dir) as relay:
                ctx, env = gateway_models.bind(ctx, gateway, env, relay.base_url)
                return run_appserver(dataclasses.replace(request, context=ctx), env)
        return run_appserver(request, env)

    def handoff_summary(self, ctx, session_id, prompt, *, cancel=None, run_id=""):
        """(text, outcome) of a handoff summary on an ephemeral fork of ``session_id`` (backends/codex/handoff.py), for
        the first run after 🔁 换配置 away from this thread (app/switch_handoff.py). A gateway model's summary goes
        through a relay of its own, as a turn does. ``run_id``: unused."""
        from covey.backends.codex.handoff import HandoffHome, summarize
        if device_of(ctx):
            return "", "原会话在设备上"
        if retired_provider(ctx.model):
            return "", "原模型的供应商已下线"
        gateway = gateways.for_model("codex", ctx.model)
        try:
            with HandoffHome(ctx.state_dir, child_process_env(ctx.workdir)) as env:
                if gateway is None:
                    return summarize(ctx, session_id, prompt, env, cancel=cancel)
                gateway_models.check(ctx, gateway, self.account_models(ctx))
                with turn_relay(RunRequest(ctx, prompt), gateway, ctx.state_dir) as relay:
                    ctx, env = gateway_models.bind(ctx, gateway, env, relay.base_url)
                    return summarize(ctx, session_id, prompt, env, cancel=cancel)
        except ValueError as exc:  # A gateway model's catalog or budget, in the owner's words: their first sentence.
            return "", "出错（" + str(exc).split("；")[0].split("。")[0][:60] + "）"
        except (OSError, RuntimeError) as exc:
            return "", f"出错（{type(exc).__name__}）"

    def prompt_runtime_note(self, ctx: BridgeContext) -> str:
        return f"Codex 沙箱模式：{self.sandbox(ctx)}"

    def startup_lines(self, ctx: BridgeContext) -> list[str]:
        return [
            f"codex_cmd={ctx.backend_cmd} model={ctx.model} "
            f"reasoning_effort={ctx.effort} sandbox={self.sandbox(ctx)}"
        ] + (["--codex-magpie-url is no longer used (the Magpie provider was removed); its value is ignored"]
             if ctx.options.get("ignored_magpie_url") else [])

    def on_startup(self, ctx: BridgeContext) -> None:
        ensure_private_dir(ctx.state_dir / "tmp")


HARNESS_REGISTRY.register("codex", CodexBackend)
