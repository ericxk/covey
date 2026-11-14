"""Public model metadata from the Claude initialization response, never account data."""

from __future__ import annotations

import dataclasses
import subprocess
import threading
import time
from pathlib import Path

from covey.backends.claude.argv import stream_argv
from covey.backends.claude.process import ClaudeProcess
from covey.core.defaults import ULTRACODE, ULTRACODE_RUNS_AS
from covey.core.state import read_json, write_json

_LOCK = threading.RLock()
_CACHE = {}
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", ULTRACODE})


def levels_with_ultracode(efforts):
    """``efforts`` with ``ultracode`` added where ``xhigh`` is offered, as on Claude Code's own models."""
    efforts = list(efforts)
    return efforts + [ULTRACODE] if ULTRACODE_RUNS_AS in efforts and ULTRACODE not in efforts else efforts


def catalog_row(slug, display_name, description, efforts, default):
    """One model of Claude's list."""
    return {"slug": slug, "display_name": display_name, "description": description, "visibility": "list",
            "supported_reasoning_levels": [{"effort": effort} for effort in levels_with_ultracode(efforts)],
            "default_reasoning_level": default}


# Claude Code lists a gateway's models (claude-opus-5, …) from one cache filed under the base URL it was
# fetched from, refreshed in the background, so a probe after another Claude Code fetched it from another base URL
# comes back without any of them (2026-11-03: Opus 5 left the launch and 换配置 menus at 19:47 and was back at
# 19:52; redacted turns did that through their relay until they stopped fetching it). The last list that had
# them stands in for this long.
GATEWAY_KEEP = 24 * 3600


def keep_gateway_models(ctx, catalog, now=None):
    """``catalog`` with the gateway's models of the last probe that listed them, when this one lists none and
    that one was less than GATEWAY_KEEP ago; a probe that lists them is remembered. The gateway's models are the
    full model ids (claude-opus-5, or <gateway>/<model> where a gateway prefixes them); Claude Code's own entries
    are its tier aliases (default, opus, sonnet, haiku, fable)."""
    now = time.time() if now is None else now
    path = Path(ctx.state_dir) / "native-models" / "claude-gateway.json"
    rows = {slug: row for slug, row in catalog.items() if "/" in slug or "-" in slug}
    if rows:
        write_json(path, {"at": now, "models": rows}, ensure_ascii=False)
        return catalog
    try:
        saved = read_json(path)
        if 0 <= now - saved["at"] <= GATEWAY_KEEP and isinstance(saved["models"], dict):
            return {**catalog, **saved["models"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return catalog


def normalize_catalog(info):
    rows = info.get("models")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Claude 没有返回可用模型目录，请检查 CLI 版本和授权")
    catalog = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("value"), str) or not row["value"].strip():
            raise ValueError("Claude 模型目录格式无效")
        slug = row["value"]
        if slug in catalog:
            raise ValueError("Claude 模型目录包含重复标识")
        raw = row.get("supportedEffortLevels", [])
        if not isinstance(raw, list) or any(not isinstance(x, str) or x not in EFFORTS for x in raw):
            raise ValueError("Claude 推理档位格式无效")
        levels = list(dict.fromkeys(raw)) if row.get("supportsEffort") is True else []
        catalog[slug] = catalog_row(slug, str(row.get("displayName") or slug), str(row.get("description") or ""),
                                    levels, "")  # Do not invent a model's default effort.
    return catalog


def model_catalog(backend, ctx, env, *, refresh=False):
    key = (str(ctx.state_dir), str(ctx.workdir), tuple(ctx.backend_cmd))
    with _LOCK:
        cached = _CACHE.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < 60:
            return {
                k: {**v, "supported_reasoning_levels": [dict(x) for x in v["supported_reasoning_levels"]]}
                for k, v in cached[1].items()
            }
        _CACHE.pop(key, None)
        # A fresh context prevents catalog controls from claiming a task's SID
        # or process. Bounded initialization does not submit a model request.
        # The launch-catalog thread (``refresh``) waits longer: at load ~420 Claude Code took over 30 s to answer
        # (2026-11-03 20:26–20:42, the launch card lost its Claude models).
        # It keeps only --claude-login-shell: the probe starts claude as the turns do.
        probe = dataclasses.replace(ctx, timeout=60 if refresh else 30,
                                    options={"login_shell": True} if ctx.options.get("login_shell") else {})
        process = ClaudeProcess(stream_argv(backend, probe, catalog=True), probe, env)
        try:
            catalog = keep_gateway_models(probe, normalize_catalog(process.initialize()))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Claude 模型目录查询超时，请稍后刷新") from exc
        finally:
            if not process.close():
                raise RuntimeError("Claude 模型目录进程清理未确认")
        _CACHE[key] = (time.monotonic(), catalog)
        return {
            k: {**v, "supported_reasoning_levels": [dict(x) for x in v["supported_reasoning_levels"]]}
            for k, v in catalog.items()
        }
