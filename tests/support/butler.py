"""The butler for a test (core/butler.py): its switch on, a fresh state directory with a topic's context,
and the records butler tools leave behind (main-thread/opened/<request>.json) written as they write them.

``ButlerCase`` is the setUp the main_* tests shared; ``switch_on`` is the switch alone for a case built on
another base (UnifiedCase, MainTopicsCase)."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from covey.core import butler
from covey.core.state import write_json

from . import make_context
from .records import state_dir, write_topic

CHAT = "oc_main"
BUTLER = butler.butler_topic_id(CHAT)


def switch_on(test, value: str = "1") -> None:
    """Set the butler's switch (butler.ENV) to ``value`` until ``test``'s cleanups."""
    patch = mock.patch.dict(os.environ, {butler.ENV: value})
    patch.start()
    test.addCleanup(patch.stop)


class ButlerCase(unittest.TestCase):
    """The butler's switch set to ``switch``, a state directory under ``self.root`` and ``self.ctx`` the context
    of ``context_topic`` (None: the butler's own topic of CHAT); ``self.state`` is its state directory, made."""

    switch = "1"
    context_topic: str | None = None

    def setUp(self):
        switch_on(self, self.switch)
        self.root = state_dir(self)
        self.ctx = make_context(self.root, topic_id=self.context_topic or BUTLER)
        self.state = self.ctx.state_dir
        self.state.mkdir(parents=True)

    def opened(self, name: str = "a", /, **record) -> Path:
        """Write ``record`` as main-thread/opened/<name>.json, as app/butler_topics.py records a request; its path."""
        path = self.state / "main-thread" / "opened" / f"{name}.json"
        write_json(path, record, ensure_ascii=False)
        return path

    def topic(self, topic_id: str, /, **fields) -> Path:
        """Write topic ``topic_id``'s topic.json with ``fields`` (support/records.py write_topic: the id names the
        directory; a ``topic_id`` field is written only when given); its path."""
        return write_topic(self.state, fields, topic_id=topic_id)
