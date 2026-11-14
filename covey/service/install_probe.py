"""./install 对 Harness 做的一次真实探测：薄适配层，判断本身在 core/harness_probe.py（安装器、doctor、可用性检查共用一份）。

返回 ``Probe(ok, kind, detail)``：kind 是 OK、NOT_LOGGED_IN、NETWORK（含超时）或 OTHER（含没装）。
结果带缓存，位置是数据根下的 ``harness-probe/``（与 ``harness_probe.probe_directory()`` 同一处）；环境用将写进服务清单的那一份，
代理变量变了缓存即作废。只用标准库。"""
from __future__ import annotations

from dataclasses import dataclass

from covey.core import harness_probe

OK, NOT_LOGGED_IN, NETWORK, OTHER = "ok", "not_logged_in", "network", "other"
KINDS = {"ok": OK, "not_logged_in": NOT_LOGGED_IN, "network": NETWORK, "timeout": NETWORK}


@dataclass(frozen=True)
class Probe:
    ok: bool
    kind: str  # OK, NOT_LOGGED_IN, NETWORK or OTHER
    detail: str = ""  # one short line of what the CLI said (already trimmed and scrubbed of key-like strings)


def probe(name, env, *, cwd=None, timeout=harness_probe.DEFAULT_TIMEOUT, directory=None, cached=harness_probe.cached_probe):
    """Ask Harness ``name`` one cheap question under ``env`` (cached in ``directory``); a Probe."""
    row = cached(directory or harness_probe.probe_directory(), name, env=env, cwd=cwd, timeout=timeout)
    return Probe(bool(row.get("ok")), KINDS.get(row.get("reason"), OTHER), row.get("detail") or "")
