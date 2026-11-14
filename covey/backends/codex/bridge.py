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

from covey.core import env as cenv
from covey.core.harness import BackendRuntime, HarnessCapabilities, HARNESS_REGISTRY

from covey.core.process_env import child_process_env
from covey.core.harness import BackendConfig, BridgeContext, HarnessSpec, RunRequest, RunResult, UUID_RE
from covey.core.providers import retired_provider, retired_provider_notice
from covey.core.state import ensure_private_dir
from covey.backends.codex.sessions import rollout_root
from covey.backends.codex import gateway as gateway_models
from covey.backends.codex.appserver import run_appserver
from covey.backends.gateway.catalog import turn_relay
from covey.core import gateways

STATE_DIR_NAME = "codex-lark-bridge"

# Literal defaults: the COVEY_CODEX_* variables that set them are no longer read (core/ignored_env.py).
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_REASONING_EFFORT = "max"
DEFAULT_SANDBOX = "danger-full-access"
DEFAULT_CODEX_CMD = "codex"
DEFAULT_CODEX_TIMEOUT = 0


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
        # compat(58800eb): the Magpie provider was removed (2026-11-10); its option stays,
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
        return str(ctx.options.get("sandbox") or DEFAULT_SANDBOX)

    def capabilities(self, ctx):
        return HarnessCapabilities(questions=True, steering=True, native_fork=True, turn_export=True)

    # The signed-in account's models plus the gateways' Responses models (<prefix><model>, core/gateways.py).
    def model_catalog(self, ctx: BridgeContext) -> dict:
        from covey.core.model_settings import model_catalog
        account = model_catalog()
        return {**account, **gateway_models.cached(ctx, account)}

    def refresh_model_catalog(self, ctx: BridgeContext) -> dict:
        from covey.core.model_settings import model_catalog
        account = model_catalog()
        return {**account, **gateway_models.refresh(ctx, account)}

    def run(self, request: RunRequest) -> RunResult:
        request.check_cancelled()
        ctx = request.context
        env = child_process_env(ctx.workdir)
        removed = retired_provider(ctx.model)
        if removed:  # A job queued on a removed provider's model before (core/providers.py RETIRED_PREFIXES).
            raise ValueError(retired_provider_notice(removed))
        gateway = gateways.for_model("codex", ctx.model)
        if gateway is not None:
            from covey.core.model_settings import model_catalog
            gateway_models.check(ctx, gateway, model_catalog())
            # Lives exactly as long as this turn; its waits show on the card like Codex's own retries.
            with turn_relay(request, gateway, ctx.state_dir) as relay:
                ctx, env = gateway_models.bind(ctx, gateway, env, relay.base_url)
                return run_appserver(dataclasses.replace(request, context=ctx), env)
        return run_appserver(request, env)

    def export_turn(self, ctx, session_id, since, until):
        """The turn's requests and responses from Codex's rollouts (backends/codex/export.py)."""
        from covey.backends.codex.export import export_turn
        return export_turn(ctx, session_id, since, until, self.rollout_dir())

    def rollout_dir(self):
        return rollout_root()

    def prompt_runtime_note(self, ctx: BridgeContext) -> str:
        return f"Codex sandbox mode: {self.sandbox(ctx)}"

    def startup_lines(self, ctx: BridgeContext) -> list[str]:
        return [
            f"codex_cmd={ctx.backend_cmd} model={ctx.model} "
            f"reasoning_effort={ctx.effort} sandbox={self.sandbox(ctx)}"
        ] + (["--codex-magpie-url is no longer used (the Magpie provider was removed); its value is ignored"]
             if ctx.options.get("ignored_magpie_url") else [])

    def on_startup(self, ctx: BridgeContext) -> None:
        ensure_private_dir(ctx.state_dir / "tmp")


HARNESS_REGISTRY.register("codex", CodexBackend)
