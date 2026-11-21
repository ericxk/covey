"""The nodes' alerts, sent by the Bridge (docs/architecture.md "节点"): what the gateway left in ``<state>/nodes/events/``
(app/node_gateway.py) and the nodes that went silent.

- **where**: to the topic of the task whose process used the most memory when the event names one (a process's
  ``task``, the job key of its COVEY_TASK_DIR, found with core/jobs.py ``find_job_record``), else the top level of
  the owner's private chat, kept there as one of the Bridge's own cards (app/pins.py remember_notice): a reply to it
  is the butler's, not a topic opened on the alert.
- **one card an alert**: a ``raise`` (or a ``once``) becomes an alert record ``<state>/nodes/alerts/<alias>-<rule
  hash>.json``, sent once (cards/node_cards.py ``alert_card``; its Markdown when the card is refused); the ``clear``
  of the same rule redraws that card in place (``cleared_card``, Messenger.patch_card), never a new message. A raise
  cleared before it was sent is dropped. At most PER_HOUR alerts are sent an hour; the rest are only logged.
- **silent nodes** (``offline``): a node other than the hub's own that was connected and has not been heard from for
  OFFLINE_SECONDS raises; heard again, it clears.
- **at night** (core/quiet_hours.py): an alert that is not ``severe`` waits for the quiet hours to end, still pending;
  cleared before then, it is dropped as any pending one is.

No record lock is held while Feishu is called: the record is written, then the send, then the outcome.
"""
from __future__ import annotations

import collections
import hashlib
import os
import time
from pathlib import Path

from covey.app import pins
from covey.cards.node_cards import alert_card, alert_text, cleared_card
from covey.core import nodes, quiet_hours
from covey.core.jobs import find_job_record
from covey.core.loops import every
from covey.core.state import ensure_private_dir, read_object, write_json
from covey.core.topics import topic_label

POLL = 5
PER_HOUR = 6
OFFLINE_SECONDS = 600.0
SEND_TRIES, PATCH_TRIES = 3, 5
KEEP_DONE = 7 * 86400


# Alerts sent at any hour: a process was killed for memory, a disk takes no writes. Memory tight, a disk nearly full and a
# node gone silent (a laptop asleep) wait out the night (core/quiet_hours.py).
SEVERE = frozenset({"oom", "write"})


def severe(rule) -> bool:
    """Whether an alert of ``rule`` (``mem``, ``oom``, ``disk:<path>``, ``write:<path>``, ``offline``) goes out at night too."""
    return str(rule or "").split(":", 1)[0] in SEVERE


def alert_path(state_dir, alias, rule):
    return nodes.nodes_dir(state_dir) / "alerts" / ("%s-%s.json" % (alias, hashlib.sha256(rule.encode()).hexdigest()[:10]))


class NodeAlerts:
    def __init__(self, runtime, messenger, voice, *, environ=None):
        self.runtime, self.messenger, self.voice, self.environ = runtime, messenger, voice, environ
        self.sent = collections.deque()

    def threads(self):
        return (("nodes-alerts", self.run),) if nodes.enabled(self.environ) else ()

    def run(self, ctx):
        every(self.runtime, POLL, self.round, args=(ctx,), isolate="node alerts")

    def round(self, ctx):
        self.take_events(ctx)
        self.watch_silence(ctx)
        self.deliver_all(ctx)

    # ------------------------------------------------------------ events into alert records

    def take_events(self, ctx):
        folder = nodes.nodes_dir(ctx.state_dir) / "events"
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
        except OSError:
            return
        for name in names:
            path = folder / name
            event = read_object(path)
            try:
                path.unlink()
            except OSError:
                continue  # Another Bridge took it.
            if event.get("alias") and event.get("rule"):
                self.apply(ctx, event)

    def apply(self, ctx, event):
        path = alert_path(ctx.state_dir, event["alias"], event["rule"])
        alert = read_object(path)
        kind = event.get("kind")
        if kind in ("raise", "once"):
            if alert.get("state") in ("pending", "sent") and kind == "raise":
                return  # Raised already.
            ensure_private_dir(path.parent)
            write_json(path, {"event": event, "state": "pending", "tries": 0, "key": "node-alert-" + event.get("id", "")},
                       ensure_ascii=False)
        elif kind == "clear":
            if alert.get("state") == "pending":
                write_json(path, dict(alert, state="dropped", cleared_at=event.get("at")), ensure_ascii=False)
            elif alert.get("state") == "sent" and alert.get("message_id"):
                write_json(path, dict(alert, state="clearing", cleared_at=event.get("at") or time.time(), tries=0),
                           ensure_ascii=False)

    def watch_silence(self, ctx, now=None):
        now = time.time() if now is None else now
        for alias in nodes.active(ctx.state_dir):
            if alias == nodes.LOCAL:
                continue
            link = nodes.read_link(ctx.state_dir, alias)
            if not link:
                continue
            heard = float(link.get("heard") or 0)
            raised = read_object(alert_path(ctx.state_dir, alias, "offline")).get("state") in ("pending", "sent")
            silent = now - heard
            if not raised and heard and silent >= OFFLINE_SECONDS:
                self.apply(ctx, {"alias": alias, "rule": "offline", "kind": "raise", "at": now, "heard": heard,
                                 "silent": silent, "id": "%s-%d" % (alias, int(heard))})
            elif raised and nodes.online(link, now):
                self.apply(ctx, {"alias": alias, "rule": "offline", "kind": "clear", "at": now})

    # ------------------------------------------------------------ sending

    def deliver_all(self, ctx):
        folder = nodes.nodes_dir(ctx.state_dir) / "alerts"
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return
        now = time.time()
        for name in names:
            if not name.endswith(".json"):
                continue
            path = folder / name
            alert = read_object(path)
            state = alert.get("state")
            if state == "pending":
                if not severe((alert.get("event") or {}).get("rule")) and quiet_hours.quiet(now, self.environ):
                    self.hold(path, alert, now)
                    continue
                self.send(ctx, path, alert)
            elif state == "clearing":
                self.redraw(ctx, path, alert)
            elif state in ("done", "dropped", "unsent", "unpatched") and now - path.stat().st_mtime > KEEP_DONE:
                path.unlink(missing_ok=True)

    def hold(self, path, alert, now):
        """A pending alert that is not severe, at night: it stays pending (a clear drops it); the first time, its record
        says since when and the log says so."""
        if alert.get("held"):
            return
        write_json(path, dict(alert, held=now), ensure_ascii=False)
        event = alert.get("event") or {}
        self.runtime.log(f"node alert held {quiet_hours.words(now, self.environ)} {event.get('alias')} {event.get('rule')}")

    def name(self, ctx, alias):
        entry = nodes.registry(ctx.state_dir).get(alias) or {}
        return entry.get("title") or alias

    def tasks(self, ctx, event):
        """task key -> how the card names it, for the event's processes (``top``, ``writers``)."""
        out = {}
        for proc in (event.get("top") or []) + (event.get("writers") or []):
            task = proc.get("task")
            if task and task not in out:
                record = find_job_record(Path(ctx.state_dir), task) or {}
                label = topic_label(Path(ctx.state_dir), record.get("topic_id", "")) if record.get("topic_id") else ""
                out[task] = " · ".join(bit for bit in (label, f"任务 #{record['job_id']}" if record.get("job_id") else "") if bit)
        return {k: v for k, v in out.items() if v}

    def target(self, ctx, event):
        """(chat, message replied to): the topic of the first process's task that has one, else the owner's chat."""
        for proc in event.get("top") or []:
            record = find_job_record(Path(ctx.state_dir), proc.get("task") or "") if proc.get("task") else None
            if record and record.get("chat_id"):
                return record["chat_id"], str(record.get("reply_to_message_id") or "")
        chat = self.runtime.access.notify_chat()
        return (chat, "") if chat else None

    def send(self, ctx, path, alert):
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        event = alert["event"]
        if len(self.sent) >= PER_HOUR:
            write_json(path, dict(alert, state="unsent", why="limit"), ensure_ascii=False)
            self.runtime.log(f"node alert not sent (over {PER_HOUR} an hour) {event.get('alias')} {event.get('rule')}")
            return
        target = self.target(ctx, event)
        if target is None:
            return  # No chat with the owner yet: tried next round.
        alert["tries"] = int(alert.get("tries") or 0) + 1
        write_json(path, alert, ensure_ascii=False)  # The try counts even if the process ends during it.
        name, tasks = self.name(ctx, event["alias"]), self.tasks(ctx, event)
        cards = []
        # At the top level (no topic's): a reply to it is the butler's (app/pins.py remember_notice).
        top = {} if target[1] else {"on_sent": lambda message: pins.remember_notice(ctx.state_dir, target[0], message)}
        ok = self.messenger.send_message(ctx.profile, target[0], alert_text(event, name, tasks),
                                         self.voice.safe_key(alert["key"]), reply_to_message_id=target[1],
                                         voice=self.voice, card=alert_card(event, name, tasks), on_card=cards.append, **top)
        if ok:
            self.sent.append(now)
            alert.update(state="sent", message_id=cards[0] if cards else "", sent_at=now)
            self.runtime.log(f"node alert sent {event['alias']} {event['rule']} card={'yes' if cards else 'no'}")
        elif alert["tries"] >= SEND_TRIES:
            alert["state"] = "unsent"
            self.runtime.log(f"node alert not sent after {alert['tries']} tries {event['alias']} {event['rule']}")
        if alert.get("state") == "sent" and event.get("kind") == "once":
            alert["state"] = "done"
        write_json(path, alert, ensure_ascii=False)

    def redraw(self, ctx, path, alert):
        event = alert["event"]
        alert["tries"] = int(alert.get("tries") or 0) + 1
        card = cleared_card(event, self.name(ctx, event["alias"]), self.tasks(ctx, event),
                            float(alert.get("cleared_at") or time.time()))
        if self.messenger.patch_card(ctx.profile, alert["message_id"], card):
            alert["state"] = "done"
            self.runtime.log(f"node alert card redrawn: {event['alias']} {event['rule']} cleared")
        elif alert["tries"] >= PATCH_TRIES:
            alert["state"] = "unpatched"
        write_json(path, alert, ensure_ascii=False)
