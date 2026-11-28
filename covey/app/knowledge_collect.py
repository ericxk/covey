"""A topic's turn writes what the owner asked it to remember (docs/butler.md 知识库, COVEY_KNOWLEDGE=1 with COVEY_MEMORY=0):
the model puts ``remember`` ([{kind, text, why, scope}], at most five) into its checkpoint when the owner's words in
that turn said 记一下 / 记住 / 记下来 (app/prompts.py REMEMBER_RULE), and the Bridge, not the task runner (rule 8: a
task process changes only its own task's card and record), reads it here once the runner has exited and the record
is settled (app/detached.py finish_detached, app/job_dispatch.py for a run in this process): ``collect``.

It writes only when all of these hold, else it drops the list and logs ``knowledge remember ignored reason=…``:
the turn ended normally (done, or waiting for the owner's answer); it is the owner's own turn (app/access.py
owner_turn); the owner's words (user_text) hold a trigger word (core/memory.py CUES, the one set); it is not a turn the
butler or a lead handed on (its message id, core/butler_opened.py handed_on: the butler records with its own tool); the
topic is not a 代答 topic. The entries are ``by=topic``
with the turn as their source; the same turn (job key) never writes twice (core/knowledge.py collected). Never raises: a
failure is a log line and the turn's other ending steps go on.

With COVEY_MEMORY=1 there is no ``remember`` rule (app/knowledge_turn.py remembers): the memory's one rule takes 记一下 as
a note (app/prompts.py MEMORY_FIX_RULE, read by app/memory_collect.py; T908 M3a), and a ``remember`` a session still writes
from older rules is dropped (logged ``reason=memory``): it carries no quote of the owner's words to check it by.

A job's ``prompt_text`` says nothing about who handed it on: every message the owner writes is queued with its expanded
input there (app/app.py handle_event: the text, a group's sender line, attachments, quotes), and so are an answer, a
page's words and a supplement queued again. Read as "handed on" it dropped every topic's 记一下 (2026-11-22, T651).
"""
from __future__ import annotations

from pathlib import Path

from covey.core import knowledge, turn_source
from covey.core.checkpoint import read_checkpoint
from covey.core import memory as core_memory
from covey.core.jobs import find_job_record
from covey.core.butler import is_butler_topic
from covey.core.butler_opened import handed_on
from covey.core.topics import proxy_topic, topic_file
from covey.core.state import read_object

ENDED_WELL = ("done", "waiting_user")


def reason_to_ignore(runtime, ctx, job, record) -> str:
    """Why this turn's ``remember`` is not written; '' when it may be."""
    topic_id = str(record.get("topic_id") or getattr(job, "topic_id", "") or "")
    if record.get("status") not in ENDED_WELL:
        return "not-finished"
    if not turn_source.behavior(job, record).owner_words:
        return "not-owner-words"  # A 协作 turn's text is not the owner's (core/turn_source.py).
    if not runtime.access.owner_turn(str(record.get("chat_id") or getattr(job, "chat_id", "")),
                                     str(record.get("sender_id") or getattr(job, "sender_id", "") or "")):
        return "not-owner"
    if not any(word in str(record.get("user_text") or getattr(job, "user_text", "") or "") for word in core_memory.CUES):
        return "no-trigger"
    if handed_on(record.get("message_id") or getattr(job, "message_id", "")):
        return "handed-on"
    if is_butler_topic(topic_id, str(record.get("chat_id") or getattr(job, "chat_id", ""))):
        return "main-thread"
    if proxy_topic(ctx):
        return "proxy"
    return ""


def collect(runtime, job, ctx) -> None:
    """Write the entries of ``job``'s finished turn, as the module says. Nothing when the switch is off."""
    if not knowledge.enabled():
        return
    try:
        record = find_job_record(ctx.state_dir, str(getattr(job, "message_id", "") or ""))
        path = str((record or {}).get("checkpoint_path") or "")
        remember = read_checkpoint(Path(path)).get("remember") if path else None
        if not isinstance(remember, list) or not remember:
            return
        reason = "memory" if core_memory.enabled() else reason_to_ignore(runtime, ctx, job, record)
        key = str(record.get("key") or "")
        if reason:
            runtime.log(f"knowledge remember ignored reason={reason} job=#{record.get('job_id')}")
            return
        topic_id = str(record.get("topic_id") or "")
        number = read_object(topic_file(Path(ctx.state_dir), topic_id)).get("topic_number") if topic_id else None
        source = {"job_key": key, "job_id": record.get("job_id"), **({"topic": f"T{number}"} if type(number) is int else {})}
        workspace = str(record.get("workspace_name") or "")
        written = knowledge.remember(ctx.state_dir, remember, job_key=key, source=source, workspace=workspace)
        runtime.log(f"knowledge remember written={len(written)} job=#{record.get('job_id')}")
    except Exception as exc:  # noqa: BLE001 - the turn's ending goes on; the owner's words are in the record
        runtime.log(f"knowledge remember failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}")
