"""The line a topic's thread gets when the owner recalled the message it was opened or handed work from
(app/source_watch.py): 来源消息已撤回, and 停止 while a task of the topic still runs or waits in the queue."""
from __future__ import annotations

from covey.cards.elements import card, item, note, text_button
from covey.core.jobs import JOB_STOP_ACTION_KIND

RECALLED_LINE = "来源消息已撤回：你撤回了管家据以开这个话题（或交来这件事）的那条消息"


def recalled_card(job_id=None, message_id="", by=""):
    """The line, with 停止 for the task ``job_id`` (its record's ``message_id``) when there is one still going: the
    task card's own stop kind, ``by`` saying the stop came from a recall (core/jobs.py parse_job_stop_action_value)."""
    buttons = ()
    if job_id:
        value = {"kind": JOB_STOP_ACTION_KIND, "job_id": str(job_id), "message_id": str(message_id or "")}
        buttons = (text_button("停止", {**value, **({"by": by} if by else {})}, tone="danger"),)
    line = RECALLED_LINE + ("。话题里的任务还在做，要停就点停止。" if buttons else "。话题里没有在做的任务。")
    return card([item(note(line), *buttons)], "来源消息已撤回", default_width=True, update_multi=True,
                padding="8px 12px 8px 12px")
