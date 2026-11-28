"""Buttons that act on a whole topic: 换配置 and 回退.

Their clicks are handled by app/harness_switch.py and app/rewind.py. 💬 侧聊, 🔀 分叉 and 📦 压缩上下文 left the
cards (the butler answers what a side chat did, and forks with its topic_fork); a card sent by an older release
keeps them, and a click on them is answered by app/task_controls.py retired_action.
"""
from __future__ import annotations

from covey.cards.elements import button_row, callback, topic_button
from covey.cards.rewind import rewind_button

SWITCH_OPEN = "bridge_switch_open"
# The mark in the value of a button on a relay card at the private chat's top level (app/relay_stops.py, a 停住了 relay's
# buttons; ``relay_switch``, a 做完了 relay's 换配置): its click is answered there, not in the topic's thread.
RELAY = "relay"
# The mark in a 停住了 relay's 换个模型 (app/relay_stops.py): its 换配置 card preselects a model of another Harness usable
# now when there is one (app/harness_switch.py other_harness_key), else the topic's own; the owner still confirms.
PREFER_OTHER = "prefer_other"
RELAY_SWITCH_ID = "relay_switch"  # A 做完了 relay's 换配置 (``relay_switch``).
RELAY_ROW_ID = "relay_switch_row"  # Its row of its own (``alone``).
# The task card's buttons are the topic cards' bordered small ones (cards/elements.py topic_button) in rows that
# wrap (button_row); a card sent before keeps its own until redrawn.
# The panel's element id, from when it held 🔀 分叉 and 💬 侧聊: cards sent since keep the same.
PANEL_ID = "fork_actions"


def topic_panel(topic_id, job_id, route, *, rewind=False, switch=True):
    """换配置 and 回退 under a finished turn, in a row that wraps ("none" cut a Claude topic's buttons to "🔀 …" in
    the topic side panel); None when it would be empty.

    `route` is the topic's (harness_id, binding_revision), which both buttons need; None leaves both out.
    `rewind`: the Harness can drop a turn (app/rewind.py); whether this one can is checked on the click.
    `switch` False leaves 换配置 out (the butler: its settings card holds its configuration)."""
    if not route:
        return None
    buttons = ([switch_button(topic_id, job_id, *route)] if switch else []) + (
        [rewind_button(topic_id, job_id, *route)] if rewind else [])
    if not buttons:
        return None
    return button_row(buttons, PANEL_ID)


def switch_value(topic_id, job_id, harness_id, revision, **marks):
    """What a 换配置 click carries (app/harness_switch.py): the topic, the turn and the binding it was drawn under, and
    ``marks`` (RELAY, PREFER_OTHER, ``other``)."""
    return {"kind": SWITCH_OPEN, "topic_id": topic_id, "job_id": job_id, "harness_id": harness_id,
            "binding_revision": revision, **marks}


def switch_button(topic_id, job_id, harness_id, revision):
    return topic_button("换配置", element_id="switch_action", behaviors=callback(
        switch_value(topic_id, job_id, harness_id, revision)))


def switch_panel(topic_id, job_id, harness_id, revision):
    return button_row([switch_button(topic_id, job_id, harness_id, revision)], "switch_actions")


def relay_switch(topic_id, job_id, harness_id, revision):
    """换配置 on a 做完了 relay at the private chat's top level (app/butler_relay.py after_turn, T669 E1): the task card's
    button with RELAY, so its 换配置 card opens at the top level under the relay, not in the topic's thread."""
    return topic_button("换配置", element_id=RELAY_SWITCH_ID, behaviors=callback(
        switch_value(topic_id, job_id, harness_id, revision, **{RELAY: True})))


def alone(button):
    """``button`` (``relay_switch``) in a row of its own (RELAY_ROW_ID) under a relay's 输入/输出 lines, as the elements
    cards/answer_card.py tail takes for its ``buttons``."""
    return [button_row([button], RELAY_ROW_ID)]
