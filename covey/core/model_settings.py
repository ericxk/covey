"""Persisted model defaults with per-job configuration snapshots."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from covey.core.providers import retired_provider, retired_provider_notice
from covey.core.locks import LOCK
from covey.core.topics import read_topic, workspace_locked
from covey.core.state import read_json


def model_catalog():
    home = os.environ.get("CODEX_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".codex"
    try:
        cache = read_json(root / "models_cache.json")
    except (ValueError, OSError):
        return {}
    rows = cache.get("models", []) if isinstance(cache, dict) else []
    if not isinstance(rows, list):
        return {}
    return {row["slug"]: row for row in rows if isinstance(row, dict) and row.get("visibility") == "list"
            and isinstance(row.get("slug"), str) and row["slug"]}


def settings_path(ctx):
    return Path(ctx.options.get("model_settings_path") or ctx.state_dir / "model-settings.json")


def _saved_settings(ctx):
    try:
        data = read_json(settings_path(ctx))
        if isinstance(data["model"], str) and isinstance(data["effort"], str):
            return data["model"], data["effort"]
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return None


def settings(ctx):
    saved = _saved_settings(ctx)
    return saved if saved is not None else (ctx.model, ctx.effort)


def recorded_work_mode(record):
    """A record's work mode; a record from before modes, or of the default mode, has none."""
    mode = record.get("work_mode", "")
    return mode if isinstance(mode, str) else ""


def recorded_model_settings(ctx, record):
    """Preserve explicit defaults while retaining the legacy missing-model rule."""
    model, effort = record.get("model"), record.get("effort")
    explicit = record.get("model_settings_snapshot") is True
    if explicit:
        if not isinstance(model, str) or not isinstance(effort, str):
            raise ValueError("任务的模型参数快照缺失或损坏，已拒绝使用当前设置替代")
        return model, effort, True
    if isinstance(model, str) and model:
        return model, effort if isinstance(effort, str) else ctx.effort, True
    # Old manually constructed jobs used an empty model to mean "use context".
    return ctx.model, ctx.effort, False


def strict_recorded_model_settings(record, *, native_default_model=True):
    """Read original settings for an explicit retry or question continuation."""
    model, effort = record.get("model"), record.get("effort")
    if (not isinstance(model, str) or not isinstance(effort, str)
            or (not model and (not native_default_model or record.get("model_settings_snapshot") is not True))):
        raise ValueError("原任务的模型参数快照缺失或损坏，未启动任务；请核查原任务后另开独立任务")
    return model, effort


class ModelSettings:
    """One Harness's model and effort: its saved defaults, a topic's snapshot over them,
    and what a new job runs with. ``harness``: whose backend, catalog and settings file."""

    def __init__(self, harness) -> None:
        self.harness = harness

    def backend_model_catalog(self, ctx):
        provider = getattr(self.harness.backend, "model_catalog", None)
        if callable(provider):
            return provider(ctx)
        # Other backends must never inherit Codex's model directory.
        return model_catalog() if self.harness.spec.name == "codex" else {}

    def configured_context(self, ctx):
        """The model and effort a job here runs with. Each Harness keeps its own defaults
        (model-settings/<harness>.json); a topic's snapshot overrides them. A topic without
        one (from before snapshots) runs on its Harness's current defaults."""
        ctx = dataclasses.replace(ctx, options={**ctx.options, "model_settings_path": str(
            self.harness.model_settings_path(ctx.state_dir))})
        if self.harness.spec.name == "codex" or callable(getattr(self.harness.backend, "model_catalog", None)):
            model, effort = settings(ctx)
            ctx = dataclasses.replace(ctx, model=model, effort=effort)
        if not ctx.topic_id:
            return ctx
        with LOCK:
            data = read_topic(ctx)
        if not data.get("model_settings_snapshot"):
            return ctx
        if not isinstance(data.get("model"), str) or not isinstance(data.get("effort"), str):
            raise ValueError("本话题模型配置损坏，未使用其他话题的设置替代")
        return dataclasses.replace(ctx, model=data["model"], effort=data["effort"], work_mode=recorded_work_mode(data))

    def prepare_submission_context(self, ctx):
        """Resolve a native default only for new jobs, not for control commands. A topic
        without its three selections waits for them as it is. The backend's startup check runs
        on the worker before the job (it can take a minute); a check that failed just now
        refuses the job here, before it is queued."""
        if not workspace_locked(ctx):
            return ctx  # Initial messages wait for all three selections.
        removed = retired_provider(ctx.model)
        if removed:
            raise ValueError(retired_provider_notice(removed))
        self.harness.refuse_while_unavailable()
        normalizer = getattr(self.harness.backend, "normalize_model_settings", None)
        if not ctx.model and callable(normalizer):
            model, effort = normalizer(ctx, ctx.model, ctx.effort)
            return dataclasses.replace(ctx, model=model, effort=effort)
        model, effort = self.effort_preset(ctx, ctx.model, ctx.effort)
        if (model, effort) != (ctx.model, ctx.effort):
            return dataclasses.replace(ctx, model=model, effort=effort)
        return ctx

    def effort_preset(self, ctx, model, effort):
        """Where each budget is its own provider (a backend's ``preset_for_effort``), a new job runs the provider of its budget."""
        switch = getattr(self.harness.backend, "preset_for_effort", None)
        return switch(ctx, model, effort) if callable(switch) and model and effort else (model, effort)
