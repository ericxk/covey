"""A gateway's Responses models on the launch card, and the relay's waits on the task card.

What the Codex and Pi Harnesses share about those models: their launch-catalog rows (``<prefix><model>``,
named "(<display name>)", with Codex's ``ultra`` where it applies) and the ``notify`` that puts a BusyRelay's
runs of 429s (gateway/relay.py) on the card. Codex binds the models to its threads in
backends/codex/gateway.py; Pi in backends/pi/.
"""
from __future__ import annotations

import dataclasses
import time

from covey.backends.gateway import connection
from covey.backends.gateway.relay import RECOVERED, RETRYING, BusyRelay
from covey.core.metrics import clock_text
# Codex runs ultra itself: the request carries xhigh plus a developer message turning on
# proactive sub-agent delegation (codex-cli 0.164.0, checked against a local stand-in
# provider 2026-10-27). Acme never sees "ultra", so its directory does not list it; an
# Acme model has it when the account's model of the same name does and Acme serves xhigh.
ULTRA, ULTRA_WIRE = "ultra", "xhigh"


def efforts(row):
    return [level["effort"] for level in (row or {}).get("supported_reasoning_levels", [])]


def has_ultra(model, row, account):
    return ULTRA in efforts(account.get(model)) and ULTRA_WIRE in efforts(row)


def launch_catalog(gateway, models, account):
    return {gateway.prefix + model: {**row, "display_name": row["display_name"] + f" ({gateway.display})",
                             "supported_reasoning_levels": row["supported_reasoning_levels"]
                             + ([{"effort": ULTRA}] if has_ultra(model, row, account) else [])}
            for model, row in models.items()}


def busy_text(busy):
    waited = clock_text(busy.seconds)
    if busy.state == RETRYING:
        text = f"{busy.reason}，正在第 {busy.retries} 次重试，已持续 {waited}"
    elif busy.state == RECOVERED:
        text = f"{busy.reason}，重试 {busy.retries} 次后恢复，持续 {waited}"
    else:
        text = f"{busy.reason}，重试 {busy.retries} 次、持续 {waited} 仍未恢复，本轮结束"
    if busy.runs > 1:  # Short runs that never reached the card count here too.
        text += f"；本轮共繁忙 {busy.runs} 次，重试 {busy.total_retries} 次，累计 {clock_text(busy.total_seconds)}"
    return text


def retry_notice(request, gateway):
    """The relay's ``notify``: a run of the gateway's 429s on the card. Each run the card hears of is one
    执行过程 line, under its sub-task when a sub-agent waits, with its count and the thread's totals for
    the turn; its own key, so Codex's own retry notes (backend-retry) never write over it. Only the main
    task's waits take the status line (模型请求重试中: which try, how long so far, busy or rate-limited;
    no wait to show and no limit); the run's end gives it back and, being only a count, never asks for an
    urgent redraw."""
    def notify(busy):
        busy = dataclasses.replace(busy, display=gateway.display)  # Whatever the relay called it, the card names this one.
        data = {"id": f"{gateway.name}-busy-{busy.runs}", "title": gateway.busy_note(), "text": busy_text(busy)}
        if busy.child:
            data["child_id"] = busy.child
        if busy.state != RETRYING:
            data["urgent"] = False
        elif not busy.child:
            data["retry"] = {"attempt": busy.retries, "reason": busy.reason,
                             "since": int((time.time() - busy.seconds) * 1000)}
        request.emit("progress_note", data)
    return notify


def turn_relay(request, gateway, state_dir):
    """The local relay one gateway turn talks to (gateway/relay.py), to use as a context manager that lives
    exactly as long as the turn; its waits show on the card (retry_notice)."""
    return BusyRelay(gateway.url(), notify=retry_notice(request, gateway),
                     log=connection.relay_log(gateway, state_dir), name=gateway.name, display=gateway.display)
