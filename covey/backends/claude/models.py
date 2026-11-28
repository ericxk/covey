"""Public model metadata from the Claude initialization response, never account data."""

from __future__ import annotations

import dataclasses
import logging
import subprocess
import threading
import time
from pathlib import Path

from covey.backends.claude.argv import stream_argv
from covey.backends.claude.process import ClaudeProcess
from covey.core import extensions
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
# comes back without any of them (Opus 5 left the launch and 换配置 menus for minutes; turns behind a relay did that
# until they stopped fetching it). The last list that had
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
        # A level this release does not know (a newer Claude Code's) is left off that model, never the whole list: one
        # unknown value used to take every Claude model off the launch card of a new install, which has no older list.
        raw = row.get("supportedEffortLevels", [])
        known = [x for x in raw if isinstance(x, str) and x in EFFORTS] if isinstance(raw, list) else []
        levels = list(dict.fromkeys(known)) if row.get("supportsEffort") is True else []
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
        # (the launch card lost its Claude models).
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


LOG = logging.getLogger(__name__)
_REPORTED = set()  # What was logged already, so a bad row or a failing hook is one line, not one per refresh.
EXTENSION_MODELS_TIMEOUT = 5.0


def _report(key, message):
    if key not in _REPORTED:
        _REPORTED.add(key)
        LOG.warning("%s", message)


def extension_models():
    """The rows of the enabled packages' ``claude_models`` hooks (``models() -> list of catalog_row``). A row is kept
    only when its slug starts with a prefix the same package's PROVIDERS declare for the Claude Harness, so
    core/providers.py can label it; the rest is dropped with one log line. A hook that raises or takes longer than
    EXTENSION_MODELS_TIMEOUT adds nothing."""
    rows = {}
    for extension in extensions.manifests():
        if "claude_models" not in extension.hooks:
            continue
        prefixes = [p["prefix"] for p in extension.providers if p.get("harness") == "claude" and p.get("prefix")]
        found, failed = [], []

        def ask(package=extension.package):
            try:
                found.extend(dict(row) for row in extensions.hook_module(package, "claude_models").models())
            except Exception as error:  # noqa: BLE001: an extension's failure must not touch the native catalog.
                failed.append(error)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        worker.join(EXTENSION_MODELS_TIMEOUT)
        if worker.is_alive() or failed:
            why = "timed out" if worker.is_alive() else f"{type(failed[0]).__name__}: {failed[0]}"
            _report((extension.package, "hook"), f"extension {extension.package!r} claude_models gave no models ({why})")
            continue
        for row in found:
            slug = row.get("slug") if isinstance(row, dict) else None
            levels = row.get("supported_reasoning_levels") if isinstance(row, dict) else None
            if (not isinstance(slug, str) or not isinstance(levels, list)
                    or not any(slug.startswith(prefix) for prefix in prefixes)):
                _report((extension.package, str(slug)), f"extension {extension.package!r} claude_models row {slug!r} "
                        "dropped: its slug must start with a prefix its PROVIDERS declare for claude")
            elif slug not in rows:
                rows[slug] = row
    return rows


def with_extension_models(catalog):
    """``catalog`` with the extensions' models added after its own; a slug the catalog has stays as it is."""
    try:
        extra = extension_models()
    except Exception as error:  # noqa: BLE001: the manifests failing to load is reported where they are used.
        _report(("manifests", type(error).__name__), f"claude_models skipped: {error}")
        return catalog
    return {**catalog, **{slug: row for slug, row in extra.items() if slug not in catalog}} if extra else catalog
