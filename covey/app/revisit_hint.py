"""A line under an ordinary topic's answer that says nothing will come back to what the reply put off (display only).

When an ordinary topic's turn ends done, its reply puts something off ("稍后", "等下", the words of core/turn_words.py
``defers``), its checkpoint has no ``revisit`` and none of its subtasks is still running, the answer card gets one grey line,
"这一轮结束后没有人会回来看" (cards/answer_card.py ``revisit_hint_element``), and with 到点回来看 on (COVEY_REVISIT=1, core/revisits.py) a
button 约 30 分钟后回来看. The saved reply, the history and each redelivery stay as the model wrote them (rule 5): only the card's
tail says it, and a redelivery sends the tail the first send kept (app/saved_send.py).

The click (``click``, owner only, TaskControls.revisit_hint) writes that topic's revisit record the way a checkpoint's ``revisit``
does (core/revisits.py armed): ``MINUTES`` from now, the look "回来看上一轮说稍后要做的事"; the turn it queues is an ordinary
revisit turn. A revisit the topic has set since stays, and a second click says so. The click is answered at once, the reply in
the topic follows (``after_ack``)."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import revisits
from covey.app.card_actions import action_value
from covey.app.card_lanes import after_ack
from covey.cards.answer_card import revisit_hint_element
from covey.core import revisits as records
from covey.core import turn_words
from covey.core.checkpoint import read_checkpoint
from covey.core.protocol import AGENT_LIVE

MINUTES = 30
LOOK = "回来看上一轮说稍后要做的事"
NOT_OWNER = "只有机器人的主人能设回来看。"


def live_subtasks(snapshot) -> bool:
    """Whether a process snapshot (core/trace.py) has a subtask at any level still running."""
    stack = [snapshot] if isinstance(snapshot, dict) else []
    while stack:
        node = stack.pop()
        if any(isinstance(agent, dict) and agent.get("status") in AGENT_LIVE for agent in (node.get("agents") or {}).values()):
            return True
        stack.extend(child for child in (node.get("children") or {}).values() if isinstance(child, dict))
    return False


def unwatched(state_dir, *, topic_id, chat_id, text, done, checkpoint_path="", snapshot=None) -> bool:
    """Whether the turn's reply ``text`` puts something off with nothing to bring it back: an ordinary topic's turn that
    ended ``done``, a reply ``defers`` (core/turn_words.py), no ``revisit`` in its checkpoint, no subtask still running."""
    if not done or not turn_words.defers(text) or live_subtasks(snapshot):
        return False
    if not revisits.ordinary(state_dir, topic_id, chat_id):
        return False
    point = read_checkpoint(Path(checkpoint_path)) if checkpoint_path else {}
    return records.wanted(point) is None


def elements(state_dir, *, topic_id, chat_id, job_id, text, done, checkpoint_path="", snapshot=None) -> list:
    """[the hint element] for an answer card's tail when ``unwatched`` (the button only with 到点回来看 on); [] otherwise."""
    if not unwatched(state_dir, topic_id=topic_id, chat_id=chat_id, text=text, done=done, checkpoint_path=checkpoint_path,
                     snapshot=snapshot):
        return []
    return [revisit_hint_element(topic_id, job_id, button=records.enabled())]


def click(host, ctx, event) -> bool:
    """約 30 分钟后回来看 on the hint (``host``: the TaskControls): the topic's revisit set, then said in the topic."""
    raw = action_value(event)
    if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
        return True
    if not host.runtime.access.is_owner(str(event.get("operator_id") or "")):
        raise ValueError(NOT_OWNER)
    chat = str(event.get("chat_id") or "")
    click_id = str(event.get("event_id") or event.get("message_id") or "")
    state, topic = Path(ctx.state_dir), ctx.topic_id
    if not records.enabled() or not revisits.ordinary(state, topic, chat):
        text = "到点回来看没有开着，这个话题现在设不了。"
    else:
        now = time.time()
        made = []

        def edit(data):
            if data:
                return data  # One it set already, or a newer one: it stays.
            made.append(True)
            return records.armed(topic, chat, {"minutes": MINUTES, "look": LOOK}, by=f"hint-{raw.get('job_id')}", round_=1, now=now)
        found = records.change(state, topic, edit)
        due = time.strftime("%H:%M", time.localtime(float((found or {}).get("due_at") or now)))
        text = (f"好，约 {MINUTES} 分钟后（{due}）回来看。" if made else f"这个话题已经约好 {due} 回来看。")
        host.runtime.log(f"revisit hint topic={topic[-8:]} set={bool(made)}")
    after_ack(lambda: host.messenger.reply(ctx, chat, text, host.voice.key("revisit-hint", click_id), voice=host.voice))
    return True
