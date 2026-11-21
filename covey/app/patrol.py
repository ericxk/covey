"""The feature object of "what the butler would have told the owner" (docs/butler.md 我本来想找你的事), COVEY_PATROL=shadow.

One thread, none unless COVEY_PATROL is ``shadow`` or ``on`` (core/patrol.py mode): each minute (COVEY_PATROL_TICK_SECONDS) ``look`` the rules
(app/patrol_rules.py) at the owner's private chat and record what is new (``record``: once per object while its state
stays, core/patrol.py); each half hour (COVEY_PATROL_JUDGE_MINUTES), when candidates wait and the day's calls (COVEY_PATROL_DAILY_CALLS,
default 48) are not used up, ask the small model (app/patrol_judge.py) about the oldest BATCH of them and write its
decisions. A failed call is tried again at the next round (a candidate gives up after TRIES), and no model is called
while the patrol's own lock is held (rule 1). It sends nothing, wakes nobody and changes no project: the decisions are read
by the digest (app/digest.py wanted).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from covey.app import patrol_judge, patrol_rules
from covey.core import patrol
from covey.core.locks import resource_lock
from covey.core.loops import every

TICK = 60  # The defaults of COVEY_PATROL_TICK_SECONDS and, in minutes, COVEY_PATROL_JUDGE_MINUTES.
JUDGE_EVERY = 1800
START_WAIT = 30  # After the Bridge starts: its own recovery goes first.
PENDING_KEPT = 60
TRIES = 4  # Rounds a candidate may fail to be judged in.


def record(data, found, now, state_dir=None) -> list:
    """Put ``found`` (Candidates) into ``data`` (core/patrol.load): the ones new, or in another state than when last
    seen, or not seen for a day, are added to ``pending`` (but the RECORD_ONLY kinds), appended to the day's candidates
    file when ``state_dir`` is given and kept in ``seen``; the ones seen as before only have their ``last`` moved.
    Objects not found for a day are forgotten. The rows recorded."""
    seen, new = data["seen"], []
    for candidate in found:
        entry = seen.get(candidate.key)
        if isinstance(entry, dict) and entry.get("sig") == candidate.sig and now - float(entry.get("last") or 0) < patrol.GONE:
            entry["last"] = now
            continue
        row = {**candidate.row(), "id": f"{int(now * 1000)}-{hashlib.sha1((candidate.key + candidate.sig).encode()).hexdigest()[:6]}",
               "at": now, "tries": 0}
        seen[candidate.key] = {"sig": candidate.sig, "at": now, "last": now}
        new.append(row)
        if state_dir is not None:
            patrol.append(state_dir, "candidates", now, {key: value for key, value in row.items() if key != "tries"})
    for key in [key for key, entry in seen.items() if not isinstance(entry, dict) or now - float(entry.get("last") or 0) >= 14 * patrol.GONE]:
        del seen[key]
    # The record-only kinds (core/patrol.py RECORD_ONLY) are written down and never judged.
    data["pending"] = [*data["pending"], *(row for row in new if row.get("kind") not in patrol.RECORD_ONLY)][-PENDING_KEPT:]
    return new


def settle(data, batch, decisions, now, state_dir) -> None:
    """The decisions on ``batch`` written down and the candidates they cover out of ``pending``; the others of the batch
    had a try (and go after TRIES)."""
    done = {item["candidate"] for item in decisions}
    by_id = {row["id"]: row for row in batch}
    for item in decisions:
        row = by_id[item["candidate"]]
        patrol.append(state_dir, "decisions", now, {
            "id": f"d{int(now * 1000)}-{item['candidate'][-6:]}", "at": now, "candidate": row["id"], "kind": row.get("kind"),
            "key": row.get("key"), "chat": row.get("chat"), "title": row.get("title"), "what": row.get("what"),
            "project": row.get("project") or "", "verdict": item["verdict"], "why": item["why"], "action": item["action"]})
    taken = {row["id"] for row in batch}
    kept = []
    for row in data["pending"]:
        if row["id"] in done:
            continue
        if row["id"] in taken:
            row = {**row, "tries": int(row.get("tries") or 0) + 1}
            if row["tries"] >= TRIES:
                continue
        kept.append(row)
    data["pending"] = kept


class Patrol:
    """``runtime``: app/runtime.py Runtime; ``env``: app/butler_env.py ButlerEnv (the Harness apps, the state directory)."""
    name = "patrol"

    def __init__(self, runtime, env) -> None:
        self.runtime, self.env = runtime, env
        self.judged_at = 0.0

    def threads(self):
        return ((self.name, self.run),) if patrol.enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, patrol.number("tick_seconds"), self.tick, isolate=self.name)

    def tick(self, now=None) -> None:
        """One minute: look, record; and every half hour, ask the model."""
        if not patrol.enabled() or not self.env.state_dir:
            return
        now = time.time() if now is None else now
        state_dir = Path(self.env.state_dir)
        chat = self.runtime.access.notify_chat()
        if chat:
            found = patrol_rules.find(patrol_rules.look(state_dir, chat, now))
            with resource_lock("patrol", str(patrol.folder(state_dir))):
                data = patrol.load(state_dir)
                new = record(data, found, now, state_dir)
                patrol.save(state_dir, data)
            if new:
                self.runtime.log(f"patrol recorded {len(new)}: " + ", ".join(sorted({row['kind'] for row in new})))
        if now - self.judged_at >= patrol.number("judge_minutes") * 60:
            self.judge(state_dir, now)

    def judge(self, state_dir, now) -> None:
        """Ask about the oldest BATCH of pending candidates, if there are any and a call is left today. The lock is
        taken to read and to write, never across the call."""
        with resource_lock("patrol", str(patrol.folder(state_dir))):
            data = patrol.load(state_dir)
            batch = data["pending"][:patrol_judge.BATCH]
            used = patrol.calls_today(data, now)
        if not batch:
            return
        if used >= patrol.number("daily_calls"):
            self.runtime.log(f"patrol judge skipped: {used} calls today")
            self.judged_at = now
            return
        with resource_lock("patrol", str(patrol.folder(state_dir))):
            data = patrol.load(state_dir)
            data["calls"] = {"day": patrol.day_of(now), "n": used + 1}  # Counted before the call: a failed one used the model too.
            patrol.save(state_dir, data)
        self.judged_at = now
        app = self.env.topics.apps.get("claude")
        backend = getattr(getattr(app, "harness", None), "backend", None)
        context = getattr(getattr(app, "harness", None), "context", None)
        decisions, _ = patrol_judge.decide(state_dir, batch, backend=backend, ctx=context, log=self.runtime.log)
        with resource_lock("patrol", str(patrol.folder(state_dir))):
            data = patrol.load(state_dir)
            settle(data, batch, decisions, now, state_dir)
            patrol.save(state_dir, data)
