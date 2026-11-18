"""What a butler tool handler gets besides its request (docs/butler.md): the Bridge's runtime, topic
routes, messenger and state, built once in app/unified.py. A handler is ``handler(env, request) -> dict`` with
request a core/butler_requests.py Request and the answer {"ok": bool, "text": str, "data": dict}; each module that
has handlers names them in a module-level ``TOOLS`` dict (tool name -> handler), the names of
core/butler_tool_specs.py."""
from __future__ import annotations

import contextlib
import dataclasses
import threading
from pathlib import Path
from typing import Any

_serving = threading.local()  # The chat of the butler whose call this thread runs (ButlerEnv.serving).


@dataclasses.dataclass
class ButlerEnv:
    runtime: Any  # app/runtime.py Runtime: log, access (owner_chat(), is_owner()).
    topics: Any  # app/topics.py Topics: apps by Harness name, default, target(), route().
    messenger: Any  # feishu/messenger.py Messenger.
    voice: Any  # The Bridge's own notices' voice (topics.voice).
    state_dir: Path
    profile: str
    settings: Any = None  # app/settings_hub.py SettingsHub: the 设置 card (the tool settings_panel).
    projects: Any = None  # app/projects.py Projects (COVEY_PROJECTS=1): topic_start's ``project`` and the 派发卡's upgrade.
    tidy: Any = None  # app/project_tidy.py ProjectTidy (COVEY_PROJECT_TIDY): projects_tidy and the web page's ``tidy``.

    def chat(self) -> str:
        """The chat of the butler whose call this thread runs (``serving``: each chat with a butler has its
        own, Access.agent_chat); else the owner's private chat with the bot, '' before the owner has written there."""
        return getattr(_serving, "chat", "") or self.runtime.access.owner_chat() or ""

    @staticmethod
    @contextlib.contextmanager
    def serving(chat):
        """``chat()`` is ``chat`` in this thread while the block runs ('' leaves the owner's)."""
        before, _serving.chat = getattr(_serving, "chat", ""), str(chat or "")
        try:
            yield
        finally:
            _serving.chat = before


def reply(ok: bool, text: str, **data) -> dict:
    return {"ok": bool(ok), "text": str(text), "data": data}
