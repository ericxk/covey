"""The steps an adapter publishes as ``tool`` events (a tool call, a command, a compaction) and the words a
model wrote before a tool call, one lifecycle for every backend.

A step starts running, may change while it runs, and ends done or failed; one that ended never runs again.
What each call publishes is what the adapters published before they shared this: a repeated start while a
step runs publishes it afresh and a repeated end publishes the later end (the trace keeps the first start
and drops an unchanged step, ``cards/trace.py`` ``apply_normalized``). Standard library only.
"""

from __future__ import annotations

from covey.core.protocol import STEP_ENDED


class Steps:
    """The steps of one turn by key, each published through ``emit("tool", step)``."""

    def __init__(self, emit):
        self.emit = emit
        self.steps = {}
        self.slots = {}  # prefix -> the key of its step now open
        self.opened = {}  # prefix -> how many it has opened

    def __contains__(self, key):
        return key in self.steps

    def get(self, key):
        return self.steps.get(key)

    def running(self):
        """The keys of the steps still running, in the order they were first started."""
        return [key for key, step in self.steps.items() if step.get("status") == "running"]

    def start(self, key, name, detail, now, **fields):
        """Start ``key`` and publish it; None when it already ended. ``detail=None`` leaves the field out."""
        if self.steps.get(key, {}).get("status") in STEP_ENDED:
            return None
        step = {"public_progress": True, "id": key, "name": name, **({} if detail is None else {"detail": detail}),
                "status": "running", "started_at": now, **fields}
        self.steps[key] = step
        self.emit("tool", step.copy())
        return step

    def update(self, key, **fields):
        """Change a running step and publish it; whether it was running."""
        step = self.steps.get(key)
        if step is None or step.get("status") != "running":
            return False
        step.update(fields)
        self.emit("tool", step.copy())
        return True

    def finish(self, key, ok, now, **fields):
        """End a known step as done or failed and publish it; whether it is known."""
        step = self.steps.get(key)
        if step is None:
            return False
        step.update(status="done" if ok else "failed", ended_at=now, **fields)
        self.emit("tool", step.copy())
        return True

    def open_slot(self, prefix, name, now, detail=None):
        """Open the step of ``prefix`` (``<prefix>-1``, ``<prefix>-2``, ...): one at a time, so an open while
        one is open is that step, published once (a compaction's keepalive rows)."""
        key = self.slots.get(prefix)
        if key is not None:
            return self.steps[key]
        self.opened[prefix] = self.opened.get(prefix, 0) + 1
        key = self.slots[prefix] = f"{prefix}-{self.opened[prefix]}"
        return self.start(key, name, detail, now)

    def close_slot(self, prefix, ok, now, detail=None, *, name=None):
        """End the open step of ``prefix``. With none open it is ignored, unless ``name`` is given: then a step
        of that name opens and ends at ``now`` (Claude Code's compact_boundary without a status row)."""
        key = self.slots.pop(prefix, None)
        if key is None:
            if name is None:
                return False
            self.opened[prefix] = self.opened.get(prefix, 0) + 1
            key = f"{prefix}-{self.opened[prefix]}"
            self.steps[key] = {"public_progress": True, "id": key, "name": name, "started_at": now}
        return self.finish(key, ok, now, **({} if detail is None else {"detail": detail}))

    def put(self, event):
        """Publish a step the adapter built whole (Codex's items): not an ended one going back to running, nor
        one that did not change. ``event`` itself is published and a copy kept; whether it was published."""
        old = self.steps.get(event["id"])
        if old is not None and old.get("status") in STEP_ENDED and event.get("status") == "running":
            return False
        if event == old:
            return False
        self.steps[event["id"]] = dict(event)
        self.emit("tool", event)
        return True


class AnswerNotes:
    """What the model wrote before a tool call: the answer so far while it is held, a progress note once a
    tool starts (and the answer is empty again)."""

    def __init__(self, emit):
        self.emit = emit
        self.held = {}

    def text(self, key, body, show=True):
        """Hold ``body`` as note ``key``; ``show`` publishes it as the answer so far."""
        self.held[key] = body
        if show:
            self.emit("answer_snapshot", {"text": body})

    def to_notes(self, now=None, child_id=None):
        """Publish the held texts as progress notes, then the empty answer. ``now=None`` leaves out
        ``received_at`` (Claude's notes take the time they are shown)."""
        for key, body in self.held.items():
            note = {"id": key, "text": body}
            if now is not None:
                note["received_at"] = now
            if child_id is not None:
                note["child_id"] = child_id
            self.emit("progress_note", note)
        self.held.clear()
        self.emit("answer_snapshot", {"text": ""})

    def clear(self):
        self.held.clear()
