"""Where a Harness's models come from, in one table (the owner, 2026-11-03).

A Harness is the agent program that runs a task: Claude Code, Codex, DeepSeek Harness
(core/roster.py). A provider is the account, gateway or API that serves its model, each with its own
models and budgets. A provider's models carry its prefix in their ids (``acme/gpt-6-astra``, a gateway's),
so the same model of two providers never collides; a model without one is its Harness's own
provider's. The model selects name a model "Harness · provider · model", the Harness's own provider
left out (Codex · Acme · GPT-6 Astra, Claude Code · Opus 5.5).
"""
from __future__ import annotations

from dataclasses import dataclass

from covey.core import gateways
from covey.core.extensions import ExtensionError, manifests


@dataclass(frozen=True)
class Provider:
    key: str
    harness: str  # The Harness its models run in.
    name: str  # Its name in a model's label; '' for the Harness's own provider.
    prefix: str  # Its model ids' prefix; '' for the Harness's own provider.
    access: str  # Where its sign-in or key lives (docs/harnesses.md).
    menu: str = ""  # Its name in the 换配置 card's provider select, where that is not ``name``.


CORE_PROVIDERS = (
    # Named for the account it signs in with, not "Claude Code 自带" beside the Harness Claude Code (2026-11-04).
    Provider("anthropic", "claude", "", "", "Claude Code 自己的登录（Claude 账号或它配置的网关）", menu="Claude 自带"),
    Provider("openai", "codex", "", "", "Codex 自己的登录"),
    # Pi has no login of its own: both are named on its models (Pi · Codex · GPT-6 Astra).
    Provider("openai", "pi", "Codex", "", "Codex 的 ChatGPT 登录（只借用访问令牌，由 Codex 刷新）", menu="Codex 自带"),
    Provider("google", "agy", "", "", "Antigravity 自己的登录（Google 账号，agy 里登录）", menu="Antigravity 自带"),
)
# The model gateways' providers (core/gateways.py) are added by providers(): Codex and Pi name the gateway on its
# models (Codex · Acme · GPT-6 Astra); DeepSeek Harness runs only its gateway's, so its ids carry no prefix.

# compat(826a0d9): providers since removed, by their models' prefix, with the name a topic still on one of their
# models is told is gone (Claude Code's Kiro 共享网关 2026-11-01; TRAE and 豆包 2026-11-05: 后续没有这个供应商了;
# Codex's Magpie 2026-11-10; Claude gateway profiles, "profile/<id>/<model>", 2026-11-14): core/model_settings.py refuses such a topic's new message and backends/claude/bridge.py,
# backends/codex/bridge.py a job queued before, both before the Harness starts, and app/launch_models.py offers none
# of their models from a catalog saved before; retire when no topic.json, launch default or model choice names one
# (compat_report: retired-provider)
CORE_RETIRED_PREFIXES = {"kiro-shared/": "Kiro 共享网关", "traex/": "TRAE", "doubao/": "豆包", "magpie/": "Magpie",
                         "profile/": "Claude 网关档位"}


def merged(core: tuple, core_retired: dict, found) -> tuple[tuple, dict]:
    """(PROVIDERS, RETIRED_PREFIXES) of the core tables and the enabled extensions' manifests (core/extensions.py),
    the core first and every order kept. A provider on a Harness and prefix the core or another extension has, or a
    retired prefix already listed, raises ExtensionError."""
    providers, retired = list(core), dict(core_retired)
    owners = {(p.harness, p.prefix): "the core" for p in providers} | dict.fromkeys(retired, "the core")
    for extension in found:
        for fields in extension.providers:
            provider = Provider(**fields)
            where = (provider.harness, provider.prefix)
            if where in owners:
                raise ExtensionError(extension.package, f"provider {provider.key!r} on Harness "
                                                f"{provider.harness!r} with prefix {provider.prefix!r} is already "
                                                f"defined by {owners[where]}")
            owners[where] = extension.package
            providers.append(provider)
        for prefix in extension.retired_prefixes:
            if prefix in owners:
                raise ExtensionError(extension.package, f"retired prefix {prefix!r} is already listed by {owners[prefix]}")
            owners[prefix] = extension.package
        retired.update(extension.retired_prefixes)
    return tuple(providers), retired


PROVIDERS, RETIRED_PREFIXES = merged(CORE_PROVIDERS, CORE_RETIRED_PREFIXES, manifests())


def gateway_providers():
    found = []
    for gateway in gateways.all_gateways():
        access = gateway.access or f"{gateway.display} 个人密钥"
        for harness in gateway.harnesses:
            if harness == "dsh":
                if gateway is gateways.dsh_gateway():
                    found.append(Provider(gateway.name, "dsh", "", "", access, menu=gateway.display))
            else:
                found.append(Provider(gateway.name, harness, gateway.display, gateway.prefix, access))
    return found


def providers():
    """PROVIDERS and the model gateways' providers (those can come from harnesses.json after import)."""
    return (*PROVIDERS, *gateway_providers())


def retired_provider(model):
    """The name of the removed provider whose prefix ``model`` carries (RETIRED_PREFIXES), else ''."""
    return next((name for prefix, name in RETIRED_PREFIXES.items() if str(model or "").startswith(prefix)), "")


def retired_provider_notice(name):
    """What a topic on removed provider ``name``'s model is told instead of running."""
    return f"本话题的模型来自已下线的供应商「{name}」，不能再用它；请点任务卡上的“🔁 换配置”选择其他模型。"


def provider_of(harness, model):
    """The provider of ``harness``'s model ``model``: the one whose prefix it carries, else the Harness's own;
    None for a Harness the table does not know."""
    own = None
    for provider in providers():
        if provider.harness != harness:
            continue
        if provider.prefix and str(model or "").startswith(provider.prefix):
            return provider
        if not provider.prefix and own is None:
            own = provider
    return own


def menu_name(provider, harness_title):
    """The provider as the 换配置 card's provider select names it: its own name, else its Harness's own login
    (Claude 自带) or, for DeepSeek Harness, which runs only its gateway's models, the gateway (``menu``)."""
    if provider is None:
        return harness_title
    return provider.menu or provider.name or harness_title + " 自带"

