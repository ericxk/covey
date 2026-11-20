"""Pi's private home for one turn, and how its models reach it.

pi reads auth.json, models.json and settings.json from ``PI_CODING_AGENT_DIR``. Each turn gets a
new one under ``<state>/tmp/pi-home-*`` (owner-only), removed when the turn ends: an Acme turn's
provider points at that turn's relay port, and models.json resolves environment names only for
``apiKey`` and ``headers``, never for ``baseUrl``. The files name environment variables only: the
Codex access token (``PI_CODEX_ACCESS_TOKEN``, read fresh from Codex's auth.json each turn) and the
Acme SK reach pi through its environment, never through a file or argv. pi never sees Codex's
refresh token: pi's OAuth flow would store the rotated one it gets back and log Codex out, so the
Codex CLI alone refreshes the login and a turn whose token is about to expire stops before pi starts.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from covey.backends.gateway import connection
from covey.core import gateways
from covey.core.paths import codex_home
from covey.core.state import ensure_private_dir

CODEX_PROVIDER = "openai-codex"  # pi's built-in ChatGPT (Codex) provider.
TOKEN_ENV = "PI_CODEX_ACCESS_TOKEN"
# pi's thinking levels; "max" runs as xhigh with that turn's map sending "max" on the wire.
THINKING = ("low", "medium", "high", "xhigh")
BUDGETS = (*THINKING, "max")  # "ultra" is Codex's xhigh plus sub-agent delegation; pi has no sub-agents.
CONTEXT_WINDOW = 272000
MAX_TOKENS = 128000
# A turn may run for hours on one access token; under this much left it does not start.
TOKEN_MARGIN = 60 * 60
# pi's own retries after an error the relay or ChatGPT hands back (5xx, a dropped stream): 2, 4, 8, 16, 32 s.
RETRIES = 5
# The OpenAI SDK under pi's openai-responses gives up on response headers after 10 minutes by default and
# sends the request again, which ends the relay's hold. This is the longest a Node timer waits (2^31-1 ms,
# about 24.8 days; a longer one fires at once). pi sets undici's headersTimeout and bodyTimeout to 0 itself.
HOLD_MS = 2**31 - 1

TOKEN_TEXT = ("Codex 登录的访问令牌{when}。Pi 只借用这个令牌，不会刷新它：在 Codex Harness 跑一轮任意任务"
              "（Codex 自己会刷新登录），再重试本轮。")


def token_expiry(token):
    """The JWT's ``exp`` in epoch seconds, or None when it has none."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return exp if type(exp) in (int, float) else None


def access_token(now=None, margin=TOKEN_MARGIN):
    """Codex's current ChatGPT access token; ValueError with the owner's words when it is missing,
    expired or within ``margin`` seconds of expiring (None: not checked)."""
    try:
        data = json.loads((codex_home() / "auth.json").read_text())
        token = data["tokens"]["access_token"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(TOKEN_TEXT.format(when="读取不到（~/.codex/auth.json 里没有 ChatGPT 登录）")) from None
    if not isinstance(token, str) or not token:
        raise ValueError(TOKEN_TEXT.format(when="读取不到（~/.codex/auth.json 里没有 ChatGPT 登录）"))
    exp = token_expiry(token)
    now = time.time() if now is None else now
    if margin is not None and exp is not None and exp - now < margin:
        when = time.strftime("%m-%d %H:%M", time.localtime(exp))
        raise ValueError(TOKEN_TEXT.format(when=f"已过期（{when}）" if exp <= now else f"将在 {when} 过期"))
    return token


def thinking(effort):
    """pi's ``--thinking`` level for a budget."""
    if effort not in BUDGETS:
        raise ValueError("Pi 不支持所选推理档位")
    return "xhigh" if effort == "max" else effort


def model_row(model, row, effort, account_row=None):
    """One models.json entry. ``account_row`` (Codex's cache) gives the context window and inputs."""
    source = account_row or row or {}
    window = source.get("context_window")
    modalities = source.get("input_modalities")
    levels = {"off": None, "minimal": None}
    if effort == "max":
        levels["xhigh"] = "max"
    return {"id": model, "name": str((row or {}).get("display_name") or model), "reasoning": True,
            "input": [m for m in ("text", "image") if m in modalities] if isinstance(modalities, list) else ["text", "image"],
            "contextWindow": window if type(window) is int and window > 0 else CONTEXT_WINDOW,
            "maxTokens": MAX_TOKENS, "thinkingLevelMap": levels}


def context_window(ctx_model, account):
    """The model's context window as pi is told it (auto-compaction works from it)."""
    providers = models_config(ctx_model, "high", account)["providers"]
    return next(iter(providers.values()))["models"][0]["contextWindow"]


def models_config(ctx_model, effort, account, relay_url=""):
    """models.json for one turn: the task's model under its provider (Codex's or the relay's gateway)."""
    gateway = gateways.for_model("pi", ctx_model)
    if gateway is not None:
        model = ctx_model[len(gateway.prefix):]
        row = model_row(model, None, effort, account.get(model))
        return {"providers": {gateway.name: {
            "baseUrl": relay_url.rstrip("/") + "/v1", "api": "openai-responses", "apiKey": key_command(gateway.key_env),
            "headers": gateway.header_dict(), "models": [row]}}}
    row = model_row(ctx_model, account.get(ctx_model), effort)
    return {"providers": {CODEX_PROVIDER: {"apiKey": key_command(TOKEN_ENV),
                                           "models": [{**row, "api": "openai-codex-responses"}]}}}


def settings_config():
    return {
        "quietStartup": True,
        "transport": "sse",
        # Auto-compaction stays on.
        "compaction": {"enabled": True},
        # pi reads the secret it was given from its own environment on every request, and its bash tool hands
        # that environment to each command: a command the model runs (env, printenv, a crash dump) would read the
        # secret into the turn's transcript. pi puts this line before each command; Codex
        # keeps such variables out of its commands' environment by default.
        "shellCommandPrefix": "unset " + " ".join([TOKEN_ENV, *(g.key_env for g in gateways.for_harness("pi"))]),
        "retry": {"enabled": True, "maxRetries": RETRIES, "baseDelayMs": 2000,
                  "provider": {"timeoutMs": HOLD_MS}},
    }


def key_command(env_name):
    """A models.json ``apiKey`` that has pi read the secret from its environment. pi 0.73.1 took a bare
    variable name; 1.x takes a bare name as the key itself and only resolves ``$NAME`` or ``!command``, and
    its openai-codex provider no longer reads an "api_key" entry of auth.json. ``!printenv`` works in both,
    and the file still holds no secret."""
    return "!printenv " + env_name


def auth_config():
    # Nothing: the token comes from the provider's apiKey command in models.json (key_command).
    return {}


class TurnHome:
    """The turn's ``PI_CODING_AGENT_DIR``; a context manager that removes it."""

    def __init__(self, state_dir, model, effort, account, relay_url=""):
        tmp = Path(state_dir) / "tmp"
        ensure_private_dir(tmp)
        self.path = Path(tempfile.mkdtemp(prefix="pi-home-", dir=str(tmp)))  # mkdtemp is 0700.
        try:
            for name, data in (("auth.json", auth_config()), ("settings.json", settings_config()),
                               ("models.json", models_config(model, effort, account, relay_url))):
                fd = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump(data, handle)
        except BaseException:
            self.remove()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.remove()

    def remove(self):
        shutil.rmtree(self.path, ignore_errors=True)


def environment(env, model, agent_dir, token=""):
    """pi's environment: its home, the route's secret (``token`` is Codex's, from access_token()), no
    telemetry and no startup network calls."""
    env = {**env, "PI_CODING_AGENT_DIR": str(agent_dir), "PI_TELEMETRY": "0", "PI_OFFLINE": "1"}
    for name in (TOKEN_ENV, *(g.key_env for g in gateways.for_harness("pi"))):
        env.pop(name, None)  # Only the route's own secret reaches pi.
    gateway = gateways.for_model("pi", model)
    if gateway is not None:
        env = connection.child_env(gateway, env)
        # The relay is on loopback; a proxy must never see that hop.
        for name in ("NO_PROXY", "no_proxy"):
            env[name] = ",".join(filter(None, [env.get(name, ""), "127.0.0.1"]))
    else:
        env[TOKEN_ENV] = token
    return env
