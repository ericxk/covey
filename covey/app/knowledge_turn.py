"""What a turn reads of the knowledge entries (core/knowledge.py, COVEY_KNOWLEDGE=1) and the history command
(COVEY_HISTORY_CMD), for app/runs.py: ``block`` is the text knowledge_block builds for the turn, ``remembers``
whether its rules tell the model to write ``remember`` into the checkpoint.

A 代答 topic gets nothing (its replies go to other people as the owner). 通用 entries and the history command are the
owner's own turn only (``owner_turn``: a colleague's topic in a group does not read the owner's personal notes or
search the owner's history); a workspace's entries go to whoever asks in that workspace. A new session reads every
current entry; a resumed one only those added or rewritten after its topic's previous turn ended (the jobs/ record's
end), leaving out the ones that turn's own checkpoint made: the session wrote them itself. Nothing is written.
"""
from __future__ import annotations

from covey.app.prompts import knowledge_block
from covey.core import knowledge, turn_source
from covey.core import memory as core_memory
from covey.core.board_read import ended_at
from covey.core.jobs import jobs_dir
from covey.core.butler import is_butler_topic
from covey.core.butler_opened import handed_on
from covey.core.redaction import topic_redaction
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
from covey.core.topics import proxy_topic


def previous_turn(state_dir, topic_id, job_id) -> tuple[float | None, set]:
    """(when the latest earlier turn of ``topic_id`` ended, the keys of the topic's earlier turns); None without one."""
    ended, keys = None, set()
    for _, record in directory_store(jobs_dir(state_dir)).query(topic_id=topic_id):
        try:
            number = int(record.get("job_id") or 0)
        except (TypeError, ValueError):
            continue
        if number >= int(job_id or 0) or record.get("status") in RUNNING or not record.get("status"):
            continue
        keys.add(str(record.get("key") or ""))
        at = ended_at(record)
        ended = at if ended is None else max(ended, at)
    return ended, keys


def block(ctx, job, *, owner_turn, resumed: bool) -> str:
    """The knowledge block for ``job``'s turn: '' when neither the entries nor the history command are on, for a 代答
    topic, or (``resumed``) when nothing is new. ``owner_turn()``: whether it is the owner's own turn."""
    on = knowledge.enabled()
    if not (on or knowledge.history_cmd()) or proxy_topic(ctx):
        return ""
    owner = owner_turn()
    # The history command is a path on this machine: a device topic's agent cannot run it.
    command = knowledge.history_cmd() if owner and not getattr(ctx, "device", "") else ""
    mine, theirs = [], []
    # With COVEY_MEMORY=1 the entries are in the one memory (core/memstore.py) and its block carries them (app/memory_turn.py):
    # only the history command's line is left here.
    if on and not core_memory.enabled():
        topic_id = getattr(job, "topic_id", "") or ctx.topic_id
        if resumed:
            since, keys = previous_turn(ctx.state_dir, topic_id, getattr(job, "job_id", 0))
            if since is not None:
                mine, theirs = knowledge.added_since(ctx.state_dir, ctx.workspace_name, since)
                skip = lambda e: e["by"] == "topic" and e["source"].get("job_key") in keys  # noqa: E731
                mine, theirs = [e for e in mine if not skip(e)], [e for e in theirs if not skip(e)]
        else:
            mine, theirs = knowledge.for_turn(ctx.state_dir, ctx.workspace_name)
    return knowledge_block(mine if owner else [], theirs, ctx.workspace_name, state_dir=ctx.state_dir,
                           history=command, new=resumed)


def remembers(ctx, job, *, owner_turn) -> bool:
    """Whether the turn's rules carry the ``remember`` rule: entries are on and the memory off (with COVEY_MEMORY=1 the
    memory's one rule takes 记一下 too, app/prompts.py MEMORY_FIX_RULE, T908 M3a), it is the owner's own turn in a topic of
    their own (not the butler's, which has tools; not one the butler or a lead handed on, whose owner's words the butler
    took with its own tool; not a 代答 topic; not one with redaction on, or whose setting cannot be read), the cases
    app/knowledge_collect.py would collect from: a turn it drops is never told to write ``remember``, so its model does
    not tell the owner 已记下 for nothing."""
    if not knowledge.enabled() or core_memory.enabled() or not owner_turn() or proxy_topic(ctx):
        return False
    if not turn_source.behavior(job).owner_words:
        return False  # A 协作 turn's words are not the owner's: no 记一下 from them (app/knowledge_collect.py skips it too).
    if handed_on(getattr(job, "message_id", "")):
        return False
    topic_id = getattr(job, "topic_id", "") or ctx.topic_id
    if is_butler_topic(topic_id, getattr(job, "chat_id", "")):
        return False
    try:
        return not topic_redaction(ctx.state_dir, topic_id)
    except ValueError:
        return False
