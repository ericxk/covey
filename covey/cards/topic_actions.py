"""Buttons that act on a whole topic: 换配置 and 回退.

Their clicks are handled by app/harness_switch.py and app/rewind.py. 💬 侧聊, 🔀 分叉 and 📦 压缩上下文 left the
cards on 2026-11-08 (the owner: the butler answers what a side chat did, and forks with its topic_fork); a
card sent before keeps them, and a click on them is answered by app/task_controls.py retired_action.
"""
from __future__ import annotations

from covey.cards.elements import button_row, callback, topic_button
from covey.cards.rewind import rewind_button

SWITCH_OPEN = "bridge_switch_open"
# The task card's buttons are the topic cards' bordered medium ones (cards/elements.py topic_button) in rows that
# wrap (button_row); a card sent before keeps its own until redrawn.
# The panel's element id, from when it held 🔀 分叉 and 💬 侧聊: cards sent since keep the same.
PANEL_ID = "fork_actions"


def topic_panel(topic_id, job_id, route, *, rewind=False, switch=True):
    """换配置 and 回退 under a finished turn, in a row that wraps ("none" cut a Claude topic's buttons to "🔀 …" in
    the topic side panel, 2026-11-02 review); None when it would be empty.

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


def switch_button(topic_id, job_id, harness_id, revision):
    return topic_button("换配置", element_id="switch_action", behaviors=callback(
        {"kind": SWITCH_OPEN, "topic_id": topic_id, "job_id": job_id, "harness_id": harness_id,
         "binding_revision": revision}))


def switch_panel(topic_id, job_id, harness_id, revision):
    return button_row([switch_button(topic_id, job_id, harness_id, revision)], "switch_actions")
