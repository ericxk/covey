"""The Harnesses of one Bridge, one object each.

A Harness differs from another only by data: its spec, its backend, its session
store, its Feishu voice and the base context built from its own arguments. Behaviour
shared by all of them lives in the app that runs a Harness (app/app.py).
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from covey.core import env as cenv
from covey.core.harness import HARNESS_REGISTRY, BridgeContext, HarnessBackend, HarnessSpec, SessionStore
from covey.core.paths import component_dir
from covey.core.roster import HARNESSES, NAMES
from covey.core.state import canonical_workdir, ensure_private_dir
from covey.core.store import directory_store
from covey.feishu.messenger import Voice


def components(name):
    """The spec, a new backend and a new session store of a roster Harness."""
    entry = HARNESSES.get(name)
    if entry is None:
        raise ValueError('Harness 不在后端名单中：' + str(name))
    module = importlib.import_module(entry.module)
    store = getattr(importlib.import_module(entry.store_module or entry.module), entry.store)
    return getattr(module, name.upper().replace('-', '_') + '_SPEC'), HARNESS_REGISTRY.create(name), store()


def enabled_names(config):
    """The bot's enabled Harnesses, default first; an omitted list preserves the full roster."""
    if not isinstance(config, dict):
        raise ValueError('Harness config must be a JSON object')
    names = config.get('enabled_harnesses', list(NAMES))
    if (not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in NAMES for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('enabled_harnesses must be a non-empty list of distinct Harness IDs: ' + ', '.join(NAMES))
    return tuple(names)


def models_first(config, name):
    """The models a Harness's entry in harnesses.json puts first on the model selects (``models_first``: a list of
    model ids, the owner's order; app/launch_models.first_models); () without one. Not in ``args``, so a release
    that does not know it still starts with the file."""
    first = config.get('harnesses', {}).get(name, {}).get('models_first', [])
    if not isinstance(first, list) or not all(isinstance(x, str) and x for x in first):
        raise ValueError('models_first must be a list of model ids: ' + name)
    return tuple(first)


STARTUP_RETRY_SECONDS = 60  # A failed backend startup check is not run again sooner.


class Harness:
    """One Harness as the Bridge runs it.

    - ``spec``, ``backend``, ``store``: what it is, how it runs a turn, where its native sessions live.
    - ``voice``: the idempotency-key prefix and text settings of everything sent for it.
    - ``context``: its base BridgeContext, from its own arguments (set by the Bridge at startup).
    - ``started``: its backend's startup check passed (``ensure_started``); ``startup_failure``:
      when and why it last failed, refused again without a new check for STARTUP_RETRY_SECONDS.
    """

    def __init__(self, spec: HarnessSpec, backend: HarnessBackend, store: SessionStore) -> None:
        self.spec = spec
        self.backend = backend
        self.store = store
        self.voice = Voice(spec.idempotency_prefix, spec.env_prefix)
        self.context: BridgeContext | None = None
        self.started = False
        self.startup_lock = threading.Lock()
        self.startup_failure: tuple[float, str] | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    def default_state_dir(self) -> Path:
        # Only the default Harness's parser is the Bridge's: the others are always given the Bridge's --state-dir
        # and --profile (app/unified.py), so their <PREFIX>_STATE_DIR and profile variables are not read.
        override = cenv.get(self.spec.env("STATE_DIR"), "").strip() if self.spec.name == NAMES[0] else ""
        if override:
            return Path(override).expanduser()
        return component_dir(self.spec.state_dir_name)

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=f"Feishu/Lark -> {self.spec.title} topic bridge")
        parser.add_argument("--state-dir", type=Path, default=self.default_state_dir())
        parser.add_argument("--workdir", type=Path, default=Path.cwd())
        parser.add_argument("--profile", default=self.spec.default_profile)
        self.backend.add_cli_args(parser)
        return parser

    def build_context(self, args: argparse.Namespace) -> BridgeContext:
        config = self.backend.build_config(args)
        workdir = canonical_workdir(args.workdir)
        if not workdir.is_dir():
            raise SystemExit(f"workdir not found: {workdir}")
        state_dir = args.state_dir.expanduser().resolve()
        ensure_private_dir(state_dir)
        bind_state = getattr(self.store, "bind_state_dir", None)
        if bind_state is not None:
            bind_state(state_dir)
        return BridgeContext(
            profile=args.profile or None, workspace_name=workdir.name, default_workdir=workdir,
            workdir=workdir, state_dir=state_dir, history_path=state_dir / "history.jsonl",
            session_path=state_dir / self.spec.session_filename,
            sessions_dir=self.store.sessions_dir(workdir), model=config.model, effort=config.effort,
            timeout=config.timeout, backend_cmd=config.backend_cmd, skip_permissions=config.skip_permissions,
            options={**config.options, "workspace_selection": True, "harness_id": self.spec.name},
        )

    def ensure_started(self, ctx: BridgeContext) -> None:
        """Run the backend's startup check before its first job, on the thread that runs jobs: one backend's
        takes up to a minute. A failure or timeout raises RuntimeError and is remembered, so jobs for
        the next STARTUP_RETRY_SECONDS are refused at once instead of each running the check again."""
        with self.startup_lock:
            if self.started:
                return
            self.refuse_while_unavailable()
            hook = getattr(self.backend, "on_startup", None)
            try:
                if hook:
                    hook(ctx)
            except subprocess.SubprocessError as exc:  # A timeout is not an OSError: say it like the others.
                why = "超时" if isinstance(exc, subprocess.TimeoutExpired) else f"失败（{type(exc).__name__}）"
                self.startup_failure = (time.monotonic(), f"{self.spec.title} 启动检查{why}")
                raise RuntimeError(self.startup_failure[1]) from None
            except (OSError, ValueError, RuntimeError) as exc:
                self.startup_failure = (time.monotonic(), str(exc) or type(exc).__name__)
                raise
            self.started, self.startup_failure = True, None

    def refuse_while_unavailable(self) -> None:
        """Raise RuntimeError while the last startup check failed less than STARTUP_RETRY_SECONDS ago.
        Never runs the check, so a thread that must not wait (the message consumer) can ask."""
        failure = self.startup_failure
        if failure and time.monotonic() - failure[0] < STARTUP_RETRY_SECONDS:
            raise RuntimeError(failure[1])

    def model_settings_path(self, state_dir: Path) -> Path:
        """This Harness's saved model and effort defaults."""
        return Path(state_dir) / "model-settings" / (self.spec.name + ".json")

    def runs_jobs_of(self, record) -> bool:
        """Whether job ``record`` is this Harness's."""
        backend = record.get("backend")
        # compat(5535387): a job of the 豆包 Harness, folded into Claude as a provider and since removed, is
        # Claude's, so the buttons on its card still find it; retire when no job record has backend "doubao"
        # (compat_report: doubao-jobs)
        return backend == self.spec.name or (backend == "doubao" and self.spec.name == "claude")

    def topic_records(self, ctx: BridgeContext, chat_id: str, **filters) -> list[dict[str, Any]]:
        """This Harness's chat jobs of the topic, newest first; ``filters`` (indexed fields, such as
        status) narrow what is copied."""
        records = [r for _, r in directory_store(ctx.state_dir / "jobs").query(topic_id=ctx.topic_id, chat_id=chat_id, **filters)
                   if self.runs_jobs_of(r) and r.get("kind", "chat") == "chat"]
        return sorted(records, key=lambda r: (str(r.get("updated_at") or r.get("created_at") or ""), int(r.get("job_id") or 0)), reverse=True)

    def topic_record(self, ctx, chat_id, job_id):
        """Copy only the requested, topic-scoped job for card navigation."""
        return next((r for _, r in directory_store(ctx.state_dir / "jobs").query(
            topic_id=ctx.topic_id, chat_id=chat_id, job_id=job_id)
            if self.runs_jobs_of(r) and r.get("kind", "chat") == "chat"), None)
