"""What Codex, Claude Code and Antigravity learned of the owner on their own, offered to Covey's memory once a day
(COVEY_MEMORY_LEARN=1 with COVEY_MEMORY=1 and 早晚汇总, docs/butler.md 记忆自己长): each Harness keeps its own memory, only its
own topics read it, a switch of Harness loses it, and the owner sees none of it.

At the evening digest (app/digest.py daily, ``Harvest.offer``) ``queue`` reads every Harness's memory files of this machine
(core/harness_memory.py fresh: read only, entry by entry, each with a fingerprint) and puts the entries of a file new or
changed since the night before in 待审核 (core/memstore.py review_put, type import), nothing on the card and no model
called; the web's 导入 lists all of them for the owner to tick. The first night only notes what is there. (The older way,
a small model reading them into candidates, had no caller left and is gone: no background model call is made here.)"""
from __future__ import annotations

from pathlib import Path

from covey.core import harness_memory, memstore


def queue(state_dir, home=None, *, root_of=None, log=print, now=None) -> int:
    """The entries of the Harnesses' memory files new or changed since the last call put in 待审核 (type import, each
    with its fingerprint, file and Harness), the first call only noting what is there. How many. Never raises."""
    try:
        made = 0
        for item in harness_memory.fresh(state_dir, Path.home() if home is None else home, root_of=root_of, now=now):
            _, new = memstore.review_put(state_dir, {
                "type": "import", "title": item.title, "text": item.text,
                "suggest": harness_memory.suggest(item), "items": [item.fingerprint],
                "origin": {"harness": item.harness, "file": item.file, "bucket": item.bucket, "repo": item.repo}}, now=now)
            made += int(new)
        log(f"memory import queued={made}")
        return made
    except Exception as exc:  # noqa: BLE001 - the digest goes out without them
        log(f"memory import failed: {type(exc).__name__}")
        return 0


class Harvest:
    """The digest's hook (app/digest.py ``harvest``, set by app/unified.py): ``runtime`` for the log (no model is asked here)."""

    def __init__(self, runtime):
        self.runtime = runtime

    def offer(self, ctx, slot) -> None:
        """The Harnesses' entries go to 待审核 (``queue``); the digest card lists none of them."""
        queue(ctx.state_dir, log=self.runtime.log)
