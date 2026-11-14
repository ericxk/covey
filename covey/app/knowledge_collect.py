"""A topic's turn writes what the owner asked it to remember (docs/main-thread.md 知识库, COVEY_KNOWLEDGE=1):
the model puts ``remember`` ([{kind, text, why, scope}], at most five) into its checkpoint when the owner's words in
that turn said 记一下 / 记住 / 记下来 (app/prompts.py REMEMBER_RULE), and the Bridge, not the task runner (rule 8: a
task process changes only its own task's card and record), reads it here once the runner has exited and the record
is settled (app/detached.py finish_detached, app/job_dispatch.py for a run in this process): ``collect``.

It writes only when all of these hold, else it drops the list and logs ``knowledge remember ignored reason=…``:
the turn ended normally (done, or waiting for the owner's answer); it is the owner's own turn (app/access.py
owner_turn); the owner's words (user_text) hold a trigger word; it is not a turn the main thread handed on
(prompt_text: the main thread recorded with its tool); the topic is not a 代答 topic and has no redaction (a setting
that cannot be read counts as on). The entries are ``by=topic`` with the turn as their source; the same turn
(job key) never writes twice (core/knowledge.py collected). Never raises: a failure is a log line and the turn's
other ending steps go on.
"""
from __future__ import annotations

from pathlib import Path

from covey.core import knowledge
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import find_job_record
from covey.core.main_thread import is_main_topic
from covey.core.redaction import topic_redaction
from covey.core.topics import proxy_topic, topic_file
from covey.core.state import read_object

TRIGGERS = ("记一下", "记住", "记下来")
ENDED_WELL = ("done", "waiting_user")


def reason_to_ignore(runtime, ctx, job, record) -> str:
    """Why this turn's ``remember`` is not written; '' when it may be."""
    topic_id = str(record.get("topic_id") or getattr(job, "topic_id", "") or "")
    if record.get("status") not in ENDED_WELL:
        return "not-finished"
    if not runtime.access.owner_turn(str(record.get("chat_id") or getattr(job, "chat_id", "")),
                                     str(record.get("sender_id") or getattr(job, "sender_id", "") or "")):
        return "not-owner"
    if not any(word in str(record.get("user_text") or getattr(job, "user_text", "") or "") for word in TRIGGERS):
        return "no-trigger"
    if record.get("prompt_text") or getattr(job, "prompt_text", None):
        return "handed-on"
    if is_main_topic(topic_id, str(record.get("chat_id") or getattr(job, "chat_id", ""))):
        return "main-thread"
    try:
        if proxy_topic(ctx) or topic_redaction(ctx.state_dir, topic_id):
            return "proxy-or-redaction"
    except ValueError:
        return "redaction-unreadable"
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
        reason = reason_to_ignore(runtime, ctx, job, record)
        key = str(record.get("key") or "")
        if reason:
            runtime.log(f"knowledge remember ignored reason={reason} job=#{record.get('job_id')}")
            return
        topic_id = str(record.get("topic_id") or "")
        number = read_object(topic_file(Path(ctx.state_dir), topic_id)).get("topic_number") if topic_id else None
        source = {"job_key": key, "job_id": record.get("job_id"), **({"topic": f"T{number}"} if type(number) is int else {})}
        written = knowledge.remember(ctx.state_dir, remember, job_key=key, source=source,
                                     workspace=str(record.get("workspace_name") or ""))
        runtime.log(f"knowledge remember written={len(written)} job=#{record.get('job_id')}")
    except Exception as exc:  # noqa: BLE001 - the turn's ending goes on; the owner's words are in the record
        runtime.log(f"knowledge remember failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}")
