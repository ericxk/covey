"""What a device may still hold of a task the hub has given up on: the pending reaps and the last task of each topic.

A turn on a device ends for the hub in ways that leave the Agent running over there: the link stayed down past the
patience (``lost``), the task runner was killed or the hub's machine restarted, the user stopped the turn while the
device could not be reached, an official refusal stopped the runner's tree (which never contained the device's
Agent). None of those may be settled as "stopped" until the device says nothing of the task is left. The rule, kept
in one place (A-SPEC 5.5):

- **register**: whoever gave the task up writes an entry in ``<state>/devices/<alias>/pending-reap.json`` (keyed by
  the device task key, with the shim path, task directory and process group the link saved in ``runs/<key>/device.json``);
  registering is idempotent and never talks to the device;
- **reap**: ``replay`` asks the device (``reap``, idempotent, only a group provably the task's is killed) for each
  entry and forgets the confirmed ones; an entry the device cannot be reached for stays and is tried again by the
  ``devices`` thread (app/devices.py) every minute;
- **before a new turn**: a topic's next turn starts by reaping the topic's last task (``<alias>/tasks/<topic>.json``,
  written when a task is spawned) so the Agent that may still be running is gone before ``--resume`` opens its
  session again (``reap_before_spawn``).
- **a turn's marked commands** (core/device_access.py): an entry with ``mark`` (keyed ``mark-<mark>``, reason
  ``turn_end``) stands for the commands a turn ran on the device with its mark; ``reap_one`` asks the device to reap
  that mark instead of a task.

Only core modules are imported. Nothing here writes a job record or a card."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import time
from pathlib import Path

from covey.core import device_transport as dt
from covey.core.state import ensure_private_dir, read_object, write_json

log = logging.getLogger(__name__)

PENDING = "pending-reap.json"
TOPIC_TASKS = "tasks"
GRACE = 5.0

# Why a reap was registered (the entry's ``reason``).
DEVICE_LOST = "device_lost"  # The patience ran out.
STOP = "stop"  # The user stopped the turn while the device was away (or the stop could not be confirmed).
RUNNER_LOST = "runner_lost"  # The task runner ended without the link having seen the Agent end.
RESTART = "restart"  # The hub's machine or the Bridge restarted under the turn.
POLICY = "policy_stop"  # An official refusal stopped the runner.
TURN_END = "turn_end"  # A turn ended (core/device_access.py reap_turn) while a command it ran there may still run.


def _dir(state_dir, alias):
    return Path(state_dir) / "devices" / alias


def pending_path(state_dir, alias):
    return _dir(state_dir, alias) / PENDING


@contextlib.contextmanager
def _locked(state_dir, alias):
    """One writer at a time across the Bridge and the task runners (flock on a file of its own)."""
    folder = _dir(state_dir, alias)
    ensure_private_dir(folder)
    fd = os.open(folder / ".reap.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def entries(state_dir, alias):
    """The pending reaps of ``alias``: ``{task key: entry}`` (``{}`` for none, or for a file that is not one)."""
    try:
        found = read_object(pending_path(state_dir, alias)).get("entries")
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in found.items() if isinstance(value, dict)} if isinstance(found, dict) else {}


def aliases(state_dir):
    """The devices with a pending reap file, sorted."""
    folder = Path(state_dir) / "devices"
    try:
        return sorted(item.name for item in folder.iterdir() if (item / PENDING).is_file())
    except OSError:
        return []


def _write(state_dir, alias, found):
    write_json(pending_path(state_dir, alias), {"schema": 1, "entries": found})


def register(state_dir, alias, task, *, reason, shim="", task_dir="", pgid=None, topic_id="", job_key="",
             run_dir="", mark="", now=None):
    """Note that the device ``alias`` may still run task ``task`` (or, with ``mark``, the commands a turn ran with that
    mark: ``task`` is then ``mark-<mark>``). Already noted: the first entry stays (its reason is the first cause), only
    the time of the latest asking is kept. The entry."""
    now = now if now is not None else time.time()
    with _locked(state_dir, alias):
        found = entries(state_dir, alias)
        entry = found.get(task)
        if entry is None:
            entry = {"alias": alias, "task": task, "reason": reason, "shim": str(shim or ""), "task_dir": str(task_dir or ""),
                     "pgid": pgid if isinstance(pgid, int) and not isinstance(pgid, bool) else None,
                     "topic_id": str(topic_id or ""), "job_key": str(job_key or ""), "run_dir": str(run_dir or ""),
                     "registered": now, "tries": 0, **({"mark": str(mark)} if mark else {})}
        found[task] = entry
        _write(state_dir, alias, found)
    return entry


def forget(state_dir, alias, task):
    with _locked(state_dir, alias):
        found = entries(state_dir, alias)
        if found.pop(task, None) is not None:
            _write(state_dir, alias, found)


def _tried(state_dir, alias, task, now):
    with _locked(state_dir, alias):
        found = entries(state_dir, alias)
        if task in found:
            found[task] = dict(found[task], tries=int(found[task].get("tries") or 0) + 1, last_try=now)
            _write(state_dir, alias, found)


def needs_reap(record):
    """Whether a ``device.json`` says the device may still hold the task: it did not end cleanly with nothing left
    behind and no reap confirmed it."""
    if not isinstance(record, dict) or not record.get("task") or not record.get("device"):
        return False
    if record.get("reaped") or record.get("cleanup_confirmed"):
        return False
    exit_info = record.get("exit")
    if record.get("state") == "exited" and isinstance(exit_info, dict) and not exit_info.get("left"):
        return False
    return True


def register_run(state_dir, run_dir, reason, *, topic_id="", job_key=""):
    """Register the device task of ``runs/<key>/`` (its ``device.json``) when the device may still hold it: the entry,
    or None (a task that ran here, one that ended cleanly, or one already reaped)."""
    try:
        record = read_object(Path(run_dir) / "device.json")
    except (OSError, ValueError):
        return None
    if not needs_reap(record):
        return None
    reason = DEVICE_LOST if record.get("lost") and reason == RUNNER_LOST else reason
    return register(state_dir, record["device"], record["task"], reason=reason, shim=record.get("shim") or "",
                    task_dir=record.get("task_dir") or "", pgid=record.get("pgid"), topic_id=topic_id,
                    job_key=job_key, run_dir=str(run_dir))


def reap_one(transport, entry, grace=GRACE):
    """Ask the device to stop what is left of ``entry``'s task: True when it says nothing is left (or never had the
    task), False when it could not be reached or would not (the group is not provably the task's, or still there). An
    entry with ``mark`` reaps the commands kept under that mark."""
    try:
        if entry.get("mark"):
            answer = transport.call("reap", mark=entry["mark"], grace=grace, patience=grace + 30.0)
            return not answer.get("after")
        answer = transport.call("reap", task=entry["task"], grace=grace, patience=grace + 30.0,
                                shim_path=entry.get("shim") or None)
    except (dt.NoTask, dt.NoPgid, dt.NotFound):
        return True  # The device has no such task, or it never ran: nothing of it is left.
    except dt.DeviceError as exc:
        log.info("reap of %s on the device failed: %s", entry.get("task"), exc)
        return False
    return not answer.get("after") and not answer.get("refused")


def replay(state_dir, alias, transport, *, grace=GRACE, now=None):
    """Reap every pending entry of ``alias``: ``(confirmed keys, kept keys)``. The first unreachable answer ends the
    round (the others would wait as long); the entries it left stay."""
    now = now if now is not None else time.time()
    done, kept = [], []
    pending = entries(state_dir, alias)
    for task in sorted(pending, key=lambda key: pending[key].get("registered") or 0):
        entry = pending[task]
        _tried(state_dir, alias, task, now)
        if reap_one(transport, entry, grace):
            forget(state_dir, alias, task)
            note_ended(state_dir, alias, entry.get("topic_id"), task)
            done.append(task)
        else:
            kept.append(task)
            if _unreachable(transport):  # The rest would wait as long: they stay for the next round.
                kept.extend(key for key in pending if key not in done and key not in kept)
                break
    return done, kept


def _unreachable(transport):
    """Whether a quick call says the device cannot be reached now (one more refused reap is not worth waiting for)."""
    try:
        transport.call("status", task="-", patience=5.0)
    except dt.Unreachable:
        return True
    except dt.DeviceError:
        return False
    return False


# ---------------------------------------------------------------- the last task of a topic

def _topic_file(state_dir, alias, topic_id):
    name = hashlib.sha256(str(topic_id).encode()).hexdigest()[:16]
    return _dir(state_dir, alias) / TOPIC_TASKS / (name + ".json")


def note_task(state_dir, alias, topic_id, task, *, shim="", task_dir="", pgid=None, run_dir=""):
    """The topic's newest device task, written when it is spawned: the next turn reaps it first (``reap_before_spawn``)."""
    if not topic_id:
        return
    ensure_private_dir(_dir(state_dir, alias) / TOPIC_TASKS)
    write_json(_topic_file(state_dir, alias, topic_id),
               {"schema": 1, "topic_id": str(topic_id), "task": task, "shim": str(shim or ""),
                "task_dir": str(task_dir or ""), "pgid": pgid, "run_dir": str(run_dir or ""), "ended": False,
                "at": time.time()})


def note_ended(state_dir, alias, topic_id, task):
    """The device has nothing of ``task`` left (it ended cleanly or was reaped): the next turn need not reap it."""
    if not topic_id:
        return
    path = _topic_file(state_dir, alias, topic_id)
    try:
        record = read_object(path)
    except (OSError, ValueError):
        return
    if record.get("task") == task and not record.get("ended"):
        write_json(path, dict(record, ended=True, ended_at=time.time()))


def previous_task(state_dir, alias, topic_id):
    """The topic's last device task as noted (``{}`` for none, or for one noted as ended)."""
    if not topic_id:
        return {}
    try:
        record = read_object(_topic_file(state_dir, alias, topic_id))
    except (OSError, ValueError):
        return {}
    return {} if record.get("ended") or not record.get("task") else record


def reap_before_spawn(state_dir, alias, topic_id, transport, *, grace=GRACE):
    """A topic's next turn is about to open its session on the device: stop what may be left of its last task there.
    True when nothing is (or there was none); False when the device will not confirm, and the new turn must not start
    (two Agents on one session would write over each other)."""
    entry = previous_task(state_dir, alias, topic_id)
    if not entry:
        return True
    if reap_one(transport, entry, grace):
        forget(state_dir, alias, entry["task"])
        note_ended(state_dir, alias, topic_id, entry["task"])
        return True
    return False
