"""A model gateway's OpenAI Responses models inside the regular Codex Harness (core/gateways.py).

They join Codex's model list under their own ids (``<prefix><model>``, e.g. ``acme/<model>``), so an
upstream name such as gpt-6-astra never collides with the same model of the
signed-in Codex account. A task on one of them binds a provider scoped to its
thread; CODEX_HOME, the account login and the user's config.toml stay as they are.
The provider points at the task's local relay (gateway/relay.py), which makes
Codex retry the gateway's capacity 429s.
"""
from __future__ import annotations

import dataclasses
import json

from covey.backends.gateway import connection
from covey.backends.gateway.catalog import ULTRA, ULTRA_WIRE, efforts, launch_catalog
from covey.backends.gateway.relay import CODEX_RETRIES
from covey.core import gateways


def cached(ctx, account):
    """Every Codex gateway's models, or none of a gateway that is unconfigured or has no fresh directory."""
    found = {}
    for gateway in gateways.for_harness("codex"):
        try:
            found.update(launch_catalog(gateway, connection.cached_catalog(gateway, ctx.state_dir, connection.RESPONSES),
                                        account))
        except ValueError:
            pass
    return found


def refresh(ctx, account):
    """The gateways' models, or none: an unconfigured or unreachable gateway must not hide the account's models."""
    found = {}
    for gateway in gateways.for_harness("codex"):
        try:
            found.update(launch_catalog(gateway, connection.refresh_catalog(gateway, ctx.state_dir, connection.RESPONSES),
                                        account))
        except ValueError:
            pass
    return found


def provider_config(gateway, model, base_url):
    key = "model_providers." + gateway.name + "."
    config = {
        "model": model, "model_provider": gateway.name,
        key + "name": gateway.display,
        key + "base_url": base_url + "/v1",
        key + "env_key": gateway.key_env,
        key + "wire_api": "responses",
        key + "supports_websockets": False,
        key + "requires_openai_auth": False,
        # Codex retries a 429 or a 5xx only under stream_max_retries (codex-cli 0.164.0); the relay
        # does the long waiting, so a hand-back costs one of these.
        key + "request_max_retries": CODEX_RETRIES,
        key + "stream_max_retries": CODEX_RETRIES,
    }
    for header, value in gateway.header_dict().items():
        config[key + "http_headers." + header] = value
    return config


def failed_text(gateway):
    """The failure card's detail when a 429 still ends a turn: the relay holds every 429 it sees with no time
    limit, so this is not "waited an hour" (2026-11-02: #2304's card said so after twelve seconds)."""
    return (f"{gateway.display} 返回繁忙或限速（429），Codex 自己的重试次数用完，本轮没有完成，与任务内容无关。"
            + (gateway.busy_hint + "。" if gateway.busy_hint else ""))


def check(ctx, gateway, account):
    model, effort = ctx.model[len(gateway.prefix):], ctx.effort
    if effort == ULTRA:
        if ULTRA not in efforts(account.get(model)):
            raise ValueError(f"此 {gateway.display} 模型不支持所选推理档位")
        effort = ULTRA_WIRE  # What the gateway receives.
    connection.check_model(gateway, ctx.state_dir, connection.RESPONSES, model, effort)


def bind(ctx, gateway, env, base_url):
    """The task context and environment for a gateway model behind the relay at `base_url`."""
    model = ctx.model[len(gateway.prefix):]
    config = provider_config(gateway, model, base_url)
    argv = [*ctx.backend_cmd]
    for key, value in config.items():
        argv += ["-c", key + "=" + json.dumps(value)]
    # App Server reloads config when starting or resuming a thread; its startup
    # overrides alone do not define the provider for those requests.
    options = {**ctx.options, "model_provider": gateway.name, "harness_label": f"Codex ({gateway.display})",
               "thread_config": {**ctx.options.get("thread_config", {}), **config},
               "error_texts": {**ctx.options.get("error_texts", {}), "rate_limited": failed_text(gateway)}}
    env = connection.child_env(gateway, env)
    # The relay is on loopback; a proxy must never see that hop.
    for name in ("NO_PROXY", "no_proxy"):
        env[name] = ",".join(filter(None, [env.get(name, ""), "127.0.0.1"]))
    return dataclasses.replace(ctx, model=model, backend_cmd=argv, options=options), env
