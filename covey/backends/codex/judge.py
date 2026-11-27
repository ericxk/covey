"""One question to Codex and nothing else (CodexBackend.ask_once, core/harness.py HarnessCapabilities.ask_once): the
background model's questions (app/assist.py, 辅助模型) when they go to Codex.

``codex exec --ephemeral`` (no session written) in a CODEX_HOME of its own (``JudgeHome``: backends/codex/consult.py's
ConsultHome without the owner's AGENTS.md either: the login and the model's provider, no MCP server, hook, rule, plugin,
skill, memory or history), the read-only sandbox, the tools a question does not need off (``FLAGS``: no shell, no
subagent, no web search), the question's system prompt as the developer instructions and its JSON on standard input (never
in argv), in an empty directory of its own. Its JSONL events are read until the turn completes or fails (an ``error``
event that only says Codex reconnects is not the end); the answer is its last agent message, and the process group is
killed then (Codex takes about a second more to exit) or at the limit (core/one_shot.py). Seen on codex-cli 0.171.0: 5 to
7 s a question (up to 11), about 3 of them the model's over some 15k input tokens of Codex's own instructions and tools.

The model (``choose``): COVEY_JUDGE_CODEX_MODEL when the account's catalog (core/model_settings.py model_catalog) or its
gateway's lists it; else, when the question follows a setting whose model is a model gateway's (``follow``: its prefix,
core/gateways.py), that gateway's: for a light question the first of its models whose id or description says it is a light
one (``LIGHT``), else the followed model itself, for a heavy one the followed model; else for a light question (the track
router, 记忆自己长) the first light model of the account's catalog (gpt-6-luna, "Fast and
affordable model for easier tasks"), for a heavy one (整理专项) the owner's own default (no ``-m``). A followed model of the
account's own (no prefix) changes nothing. The effort is the lowest the model has for a light question, ``medium`` for a
heavy one. A gateway's model goes through a relay of its own for the question (backends/gateway/relay.py, its spent
quota held there as in a turn), the gateway's provider given on the command line (backends/codex/gateway.py
provider_config). ``model_of``: the model a question would run on, without starting anything.

A failed question's reason (``failure_code``): what backends/codex/turn.py codex_failure_code reads of the error (a login,
a spent quota, a limit, a refusal, a missing model, a fault, a lost connection), else what its words say (a usage limit,
core/assist.py code_of_text), else ``model_error``; its detail the error's own words as a card may show them
(core/failure.py said_of).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from covey.backends.codex import gateway as gateway_models
from covey.backends.codex.consult import FLAGS as CONSULT_FLAGS, LEFT_OUT, ConsultHome, real_home, tomllib
from covey.backends.codex.turn import SPENT, codex_failure_code, said_words
from covey.backends.gateway import connection
from covey.backends.gateway.catalog import ULTRA, ULTRA_WIRE
from covey.backends.gateway.relay import BusyRelay
from covey.core import env as cenv
from covey.core import gateways, quota_hold
from covey.core.assist import code_of_text
from covey.core.failure import said_of
from covey.core.harness import AskFailed
from covey.core.model_settings import model_catalog
from covey.core.one_shot import run_lines
from covey.core.process_env import child_process_env
from covey.core.state import ensure_private_dir

MODEL_ENV = "COVEY_JUDGE_CODEX_MODEL"
# A model the catalog calls light: a word of its id, or its description ("Fast and affordable model for easier tasks").
LIGHT = re.compile(r"(?<![a-z0-9])(luna|mini|nano|lite|small)(?![a-z])|affordable|easier tasks|simpler tasks", re.I)
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
HEAVY_EFFORT = "medium"
# On top of a consult's (apps, plugins, memories, hooks, goals, image generation, web search, the question off): the
# shell, subagents and the other tools a one-line answer has no use for. The app server and exec ignore a key they do not
# know (codex-cli 0.171.0).
FLAGS = (*CONSULT_FLAGS, "features.shell_tool=false", "features.multi_agent=false", "features.browser_use=false",
         "features.computer_use=false", "features.in_app_browser=false", "features.sleep_tool=false",
         "features.tool_suggest=false", "features.skill_search=false", "features.shell_snapshot=false")
# The reasons a question's failure may have (core/assist.py): of a turn's codes, these; any other is model_error.
ASKED = frozenset({"authentication_error", "quota_exhausted", "rate_limited", "server_fault", "transport_error",
                   "model_unavailable", "model_blocked", "policy_refusal"})
# Codex's own words of its plan's window used up (``usageLimitExceeded`` said in words: "You've hit your usage limit.").
USAGE = re.compile(r"usage limit|hit your limit", re.I)
# An ``error`` event that says Codex is retrying the model's stream ("Reconnecting... 2/5 (stream disconnected …)"), not
# that the turn failed: the turn's own end follows.
RETRYING = re.compile(r"^\s*(?:re-?connecting|retrying)\b", re.I)


class JudgeHome(ConsultHome):
    """A consult's home (backends/codex/consult.py ConsultHome) without the owner's global AGENTS.md too: the question
    reads only its own words. <state>/tmp/codex-judge-*, removed with the question."""

    PREFIX = "codex-judge-"

    @staticmethod
    def leaves_out(name: str) -> bool:
        return name in LEFT_OUT or name.startswith("memories") or name == "AGENTS.md"


def gateway_rows(ctx, account, model) -> dict:
    """The models of the gateway ``model`` belongs to by its prefix ({prefixed id: row}, its saved directory; nothing is
    fetched), {} for a model of the account's own or a gateway with no fresh directory."""
    gateway = gateways.for_model("codex", model) if model else None
    if gateway is None:
        return {}
    return {slug: row for slug, row in gateway_models.cached(ctx, account).items() if slug.startswith(gateway.prefix)}


def light_of(catalog) -> str:
    return next((slug for slug, row in catalog.items()
                 if LIGHT.search(slug) or LIGHT.search(str(row.get("description") or ""))), "")


def choose(env, *, heavy: bool, environ=None, follow: str = "", ctx=None) -> tuple:
    """(model, effort) of a question: '' for Codex's own default (see the module docstring). ``follow``: the model of the
    setting the question follows; ``ctx`` (its state directory) finds a gateway's saved directory."""
    account = model_catalog(real_home(env))
    wanted = str(cenv.get(MODEL_ENV, "", environ) or "").strip()
    wanted_rows = gateway_rows(ctx, account, wanted) if ctx is not None else {}
    followed = gateway_rows(ctx, account, follow) if ctx is not None and follow else {}
    on_gateway = bool(follow) and gateways.for_model("codex", follow) is not None
    catalog = {**account, **wanted_rows, **followed}
    if wanted and (not account or wanted in account or wanted in wanted_rows):
        model = wanted
    elif on_gateway:
        model = follow if heavy else (light_of(followed) or follow)
    elif heavy:
        model = ""
    else:
        model = light_of(account)
    levels = [level.get("effort") for level in (catalog.get(model) or {}).get("supported_reasoning_levels") or ()
              if isinstance(level, dict)]
    levels = [effort for effort in EFFORTS if effort in levels]
    if heavy:
        effort = HEAVY_EFFORT if not levels or HEAVY_EFFORT in levels else levels[0]
    else:
        effort = levels[0] if levels else "low"
    return model, effort


def argv(ctx, system: str, model: str, effort: str, command=None) -> list:
    """The ``codex exec`` command line of one question (its words come on standard input: ``-``); ``command``: the CLI
    with a gateway's provider in front (``ask_once``), else ``ctx.backend_cmd``."""
    flags = [part for flag in FLAGS for part in ("-c", flag)]
    return [*(command or ctx.backend_cmd), "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "--color",
            "never", "--json", *(["-m", model] if model else []),
            *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
            *flags, "-c", "developer_instructions=" + json.dumps(system), "-"]


def failure_code(error) -> str:
    """The reason of a failed question from its error (a turn.failed's ``error``, an ``error`` event): see the module
    docstring."""
    code = codex_failure_code(error)
    if code == "workspace_routing_timeout":
        return "transport_error"
    if code in ASKED:
        return code
    words = " ".join(str(error.get(key) or "") for key in ("code", "type", "message")) if isinstance(error, dict) else ""
    if USAGE.search(words):
        return "quota_exhausted" if SPENT.search(words) else "rate_limited"
    return code_of_text(words) or "model_error"


def finished(row) -> bool:
    """Whether ``row`` ends the question: the turn completed or failed, or an error that is not a reconnect."""
    kind = row.get("type")
    return kind in ("turn.completed", "turn.failed") or (kind == "error" and not RETRYING.search(str(row.get("message") or "")))


def answer_of(rows) -> str:
    """The last agent message of a completed turn; AskFailed otherwise (``failure_code``, its words as the detail)."""
    failed = next((row for row in rows if row.get("type") == "turn.failed"), None) or next(
        (row for row in rows if row.get("type") == "error" and not RETRYING.search(str(row.get("message") or ""))), None)
    if failed is not None:
        error = failed.get("error") if isinstance(failed.get("error"), dict) else failed
        words = said_words(error) or said_of(str(error.get("code") or error.get("type") or ""))
        raise AskFailed(failure_code(error), words)
    texts = [(row.get("item") or {}).get("text") for row in rows if row.get("type") == "item.completed"
             and (row.get("item") or {}).get("type") in ("agent_message", "assistant_message")]
    text = next((text for text in reversed(texts) if isinstance(text, str) and text.strip()), "")
    if not text:
        raise AskFailed("bad_output")
    return text


def relay(gateway, state_dir):
    """The local relay of a gateway question: a turn's (backends/gateway/catalog.py turn_relay) with no card to tell."""
    return BusyRelay(gateway.url(), log=connection.relay_log(gateway, state_dir), name=gateway.name,
                     display=gateway.display, hold=quota_hold.Hold(state_dir, gateway) if gateway.quota_pattern else None)


def on_gateway(gateway, model, env, base_url, command) -> tuple:
    """(command, env) of a question on ``gateway``'s ``model`` (its prefixed id) through the relay at ``base_url``: the
    provider on the command line (backends/codex/gateway.py provider_config), the gateway's key in the environment
    (AskFailed authentication_error without one), the loopback hop kept from any proxy."""
    config = gateway_models.provider_config(gateway, model[len(gateway.prefix):], base_url)
    try:
        env = connection.child_env(gateway, env)
    except ValueError as exc:
        raise AskFailed("authentication_error", said_of(str(exc))) from None
    for name in ("NO_PROXY", "no_proxy"):
        env[name] = ",".join(filter(None, [env.get(name, ""), "127.0.0.1"]))
    for key, value in config.items():
        command = [*command, "-c", key + "=" + json.dumps(value)]
    return command, env


def ask_once(ctx, system: str, data: str, *, heavy: bool = False, limit: float, cwd=None, follow: str = "",
             environ=None) -> str:
    """The answer's text to ``data`` under ``system``, within ``limit`` seconds; AskFailed with the reason otherwise. ``cwd``:
    the empty directory it runs in (made; default <state>/codex-judge). ``follow``: the model of the setting the question
    follows (a gateway's: the question goes there, ``choose``)."""
    folder = Path(cwd) if cwd is not None else Path(ctx.state_dir) / "codex-judge"
    ensure_private_dir(folder)
    env = child_process_env(folder)
    model, effort = choose(env, heavy=heavy, environ=environ, follow=follow, ctx=ctx)
    gateway = gateways.for_model("codex", model) if model else None

    def run(command, run_env, name):
        return run_lines(argv(ctx, system, name, ULTRA_WIRE if gateway is not None and effort == ULTRA else effort,
                              command), data, env=run_env, cwd=str(folder), limit=limit, done=finished)

    with JudgeHome(ctx.state_dir, env) as home_env:
        if gateway is None:
            rows = run(ctx.backend_cmd, home_env, model)
        else:
            with relay(gateway, ctx.state_dir) as busy:
                command, run_env = on_gateway(gateway, model, home_env, busy.base_url, ctx.backend_cmd)
                rows = run(command, run_env, model[len(gateway.prefix):])
    return answer_of(rows)


def default_model(env) -> str:
    """The model Codex runs with no ``-m``: the ``model`` of the owner's config.toml (a question's home keeps it,
    backends/codex/consult.py KEPT); '' when it names none or cannot be read."""
    try:
        data = tomllib.loads((real_home(env) / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    model = data.get("model") if isinstance(data, dict) else None
    return model.strip() if isinstance(model, str) else ""


def model_of(ctx, *, heavy: bool = False, follow: str = "", environ=None) -> str:
    """The model id a question (``ask_once``) would run on, a gateway's with its prefix; for Codex's own default the one
    config.toml names (``default_model``), '' when none does. Nothing is started, nothing fetched."""
    env = child_process_env()
    model, _ = choose(env, heavy=heavy, environ=environ, follow=follow, ctx=ctx)
    return model or default_model(env)
