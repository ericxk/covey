"""模型网关的配置：一个网关是“OpenAI Responses / Anthropic Messages 兼容”的上游，由数据描述，可以有多个。

来源两处，核心不写任何一家的地址：扩展 manifest 的 ``GATEWAYS``（core/extensions.py）和 harnesses.json 顶层的
``gateways``（Bridge 启动时 ``configure``，给自己部署的人配自己的网关）。名字相同时 harnesses.json 的不能覆盖扩展的。
网关的模型 id 带自己的前缀（``acme/gpt-6-astra``）；给 dsh 用的网关模型不带前缀（dsh 只用第一个声明它的网关）。
这里只存配置、不联网（联网和目录缓存在 backends/gateway/）；只用标准库。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from covey.core.extensions import ExtensionError, manifests

GATEWAY_HARNESSES = ("codex", "pi", "dsh")  # 网关能接的 Harness。
NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
FIELDS = {"display", "prefix", "base_url", "base_url_env", "key_env", "key_file", "key_file_env", "headers",
          "header_envs", "harnesses", "quota_pattern", "busy_hint", "access"}


@dataclass(frozen=True)
class Gateway:
    name: str  # 状态目录名、Codex/Pi 里的 provider 名、模型目录缓存名。
    display: str  # 卡上的名字。
    base_url: str
    key_env: str  # 密钥进子进程用的环境变量名。
    prefix: str = ""  # Codex、Pi 上模型 id 的前缀；全是 dsh 用的网关可以留空。
    base_url_env: str = ""  # 设了就覆盖 base_url。
    key_file: str = ""  # 没有环境变量时读的文件；相对路径在 XDG 配置目录下，默认 harness-bridge/<name>-api-key。
    key_file_env: str = ""
    headers: tuple = ()  # ((名, 值), …) 每个请求都带。
    header_envs: tuple = ()  # ((头名, 环境变量), …) 环境变量非空时覆盖头的值。
    harnesses: tuple = ("codex", "pi", "dsh")
    quota_pattern: str = ""  # 403 响应里表示“额度用完”的正则（空则没有这种 403）。
    busy_hint: str = ""  # 繁忙失败卡的末句，例如指向用量页面。
    access: str = ""  # 供应商表里“密钥在哪”。

    @property
    def missing_key(self):
        relative = self.key_file or f"harness-bridge/{self.name}-api-key"
        shown = relative if relative.startswith(("/", "~")) else "~/.config/" + relative
        return f"{self.display} 个人密钥未配置：写入 {shown}（权限 600）或设置 {self.key_env}"

    def url(self):
        return ((os.environ.get(self.base_url_env, "").strip() if self.base_url_env else "") or self.base_url).rstrip("/")

    def header_dict(self):
        merged = dict(self.headers)
        for header, variable in self.header_envs:
            merged[header] = os.environ.get(variable, "").strip() or merged.get(header, "")
        return merged

    def key_path(self):
        configured = os.environ.get(self.key_file_env, "").strip() if self.key_file_env else ""
        if configured:
            return Path(configured).expanduser()
        relative = self.key_file or f"harness-bridge/{self.name}-api-key"
        if relative.startswith(("/", "~")):
            return Path(relative).expanduser()
        config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        return (Path(config).expanduser() if config else Path.home() / ".config") / relative

    def quota_text(self, message):
        return bool(self.quota_pattern and re.search(self.quota_pattern, str(message), re.IGNORECASE))

    def busy_note(self):
        return self.display + BUSY_SUFFIX


def parse(name, fields, where):
    """A Gateway from its manifest or harnesses.json entry, checked for shape; ``where`` names it in errors."""
    def bad(why):
        return ExtensionError(where, f"gateway {name!r}: {why}") if where else ValueError(f"gateway {name!r}: {why}")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise bad("the name must be lower-case letters, digits and hyphens")
    if not isinstance(fields, dict) or set(fields) - FIELDS:
        raise bad(f"must be a dict with the fields {sorted(FIELDS)}")
    texts = {k: fields.get(k, "") for k in ("display", "prefix", "base_url", "base_url_env", "key_env", "key_file",
                                            "key_file_env", "quota_pattern", "busy_hint", "access")}
    if not all(isinstance(v, str) for v in texts.values()):
        raise bad("its fields (except headers, header_envs, harnesses) are strings")
    if not texts["base_url"].startswith(("http://", "https://")) or not texts["key_env"]:
        raise bad("needs base_url (http or https) and key_env")
    pairs = {}
    for key in ("headers", "header_envs"):
        value = fields.get(key, {})
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise bad(f"{key} must map strings to strings")
        pairs[key] = tuple(value.items())
    harnesses = fields.get("harnesses", list(GATEWAY_HARNESSES))
    if (not isinstance(harnesses, (list, tuple)) or not harnesses or len(set(harnesses)) != len(harnesses)
            or any(h not in GATEWAY_HARNESSES for h in harnesses)):
        raise bad(f"harnesses must be a non-empty list from {list(GATEWAY_HARNESSES)}")
    if {"codex", "pi"} & set(harnesses) and not texts["prefix"]:
        raise bad("a gateway for Codex or Pi needs a model prefix such as 'name/'")
    if texts["quota_pattern"]:
        try:
            re.compile(texts["quota_pattern"])
        except re.error:
            raise bad("quota_pattern is not a regular expression") from None
    return Gateway(name=name, display=texts.pop("display") or name, harnesses=tuple(harnesses),
                   headers=pairs["headers"], header_envs=pairs["header_envs"], **{
                       k: v for k, v in texts.items() if k != "display"})


_configured: dict = {}  # harnesses.json 的 gateways，Bridge 启动时 configure。


def configure(rows) -> None:
    """Take harnesses.json's ``gateways`` (name -> fields; None for none); a bad entry raises ValueError."""
    if rows is None:
        rows = {}
    if not isinstance(rows, dict):
        raise ValueError("harnesses.json gateways must be an object")
    found = {name: parse(name, fields, "") for name, fields in rows.items()}
    _configured.clear()
    _configured.update(found)
    forget()


def _merge(found):
    """name -> Gateway: the extensions' first, then the configured; a repeated name, or a prefix a Harness
    already has, raises (ExtensionError for an extension's)."""
    gateways, owners = {}, {}
    for source, items in found:
        for name, gateway in items:
            where = ExtensionError if source else ValueError
            clash = None
            if name in gateways:
                clash = f"gateway name {name!r} is already defined by {owners[name]}"
            for other in gateways.values():
                if (gateway.prefix and gateway.prefix == other.prefix and (set(gateway.harnesses) & set(other.harnesses)) - {"dsh"}):
                    clash = f"gateway prefix {gateway.prefix!r} is already used by {other.name!r}"
            if clash:
                raise where(source, clash) if source else where(clash)
            gateways[name] = gateway
            owners[name] = source or "harnesses.json"
    return gateways


_cached: list = []


def forget() -> None:
    """Drop the merged list (``configure`` and the tests that change the enabled extensions)."""
    _cached.clear()


def all_gateways() -> tuple:
    """Every gateway, in order: the enabled extensions', then harnesses.json's."""
    if not _cached:
        _cached.append(_load())
    return _cached[0]


def _load() -> tuple:
    found = [(extension.package, [(n, parse(n, f, extension.package)) for n, f in extension.gateways.items()])
             for extension in manifests()]
    found.append(("", list(_configured.items())))
    return tuple(_merge(found).values())


def named(name):
    return next((g for g in all_gateways() if g.name == name), None)


def for_harness(harness) -> tuple:
    return tuple(g for g in all_gateways() if harness in g.harnesses)


def for_model(harness, model):
    """The gateway ``model`` of ``harness`` belongs to by its prefix (Codex, Pi), else None. dsh's ids carry no
    prefix: ``for_harness("dsh")`` is its gateway list."""
    if harness == "dsh":
        return None
    return next((g for g in for_harness(harness) if g.prefix and str(model or "").startswith(g.prefix)), None)


def dsh_gateway():
    """The gateway dsh runs on: the first one that declares it; None means dsh is not available."""
    return next(iter(for_harness("dsh")), None)


def quota_text(message) -> bool:
    """A 403's message that is a spent quota of any gateway (not a refused identity)."""
    return any(g.quota_text(message) for g in all_gateways())


BUSY_SUFFIX = " 繁忙记录"  # 网关繁忙记录的标题 "<显示名> 繁忙记录"（Gateway.busy_note）；旧记录里的标题照此认。


def is_busy_note(title) -> bool:
    return isinstance(title, str) and title.endswith(BUSY_SUFFIX)
