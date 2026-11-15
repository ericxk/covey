"""Devices at the app's edge: the thread that keeps them in order, and what a turn on one does when its link is lost.

Off unless ``COVEY_DEVICES=1`` lists devices (core/devices.py): then there is no thread and nothing here runs.

- **the ``devices`` thread** (``Devices``, every ``PERIOD`` seconds): per device, a cheap look that it answers (``hello``,
  refreshing the cached one; ``<state>/devices/<alias>/health.json`` keeps what it saw), then the pending reaps
  (core/device_reap.py ``replay``: the tasks the hub gave up on while the device was away) and, once an hour, the
  device's ``gc`` (old task directories, shim directories nobody uses, checkpoint mirrors under ``~/.covey/checkpoints/``).
  A device that does not answer is skipped whole; its pending reaps wait for the next round.
- **a lost link** (the helpers below, used by app/worker.py, app/detached.py, app/app.py and app/policy_guard.py): a turn
  whose device stayed away past the patience is settled as interrupted (``DeviceLost``), one the device lost (it
  restarted) as failed (``DeviceGone``); a runner that ended, a restart and an official stop each *register* a pending
  reap (``register_run``) or, for the stop that must be sure, reap now (``reap_run``). None of them talks to the device
  while holding a record's lock, and none marks a task stopped before the device said nothing of it is left."""
from __future__ import annotations

import time
from pathlib import Path

from covey.core import device_link, device_reap, devices
from covey.core import device_transport as dt
from covey.core import turn_words
from covey.core.jobs import DEVICE_LOST_STATUS
from covey.core.loops import every
from covey.core.state import write_json

PERIOD = 60  # Seconds between two rounds of the thread.
GC_EVERY = 3600.0  # Seconds between two ``gc`` calls to one device.
GC_KEEP_DAYS = 7


class DeviceLost(Exception):
    """The turn's device stayed away past the patience: the turn is interrupted; the Agent is reaped when it is back."""


class DeviceGone(Exception):
    """The device no longer has the turn's task (it restarted, its supervisor was killed): the turn failed."""


def link_of(job):
    """The turn's ``DeviceProcess`` (what the backend started on a device), else None: a process here, none, or a test's
    stand-in (it has no ``details`` of its own)."""
    process = getattr(job, "backend_process", None)
    if process is None or not callable(getattr(type(process), "details", None)):
        return None
    return process


def end_of(job):
    """How the turn's link ended: ``lost`` (the patience ran out), ``gone`` (the device lost the task) or '' (anything else,
    a turn here too)."""
    process = link_of(job)
    if process is None:
        return ""
    state = process.details()
    return "lost" if state.get("lost") else "gone" if state.get("state") == "gone" else ""


def verdict(job):
    """Raise ``DeviceLost`` or ``DeviceGone`` for a turn whose link ended so, unless the user stopped it (a stop is a stop)."""
    end = "" if job.cancel_event.is_set() else end_of(job)
    if end:
        alias = link_of(job).details().get("alias", "")
        raise (DeviceLost if end == "lost" else DeviceGone)(alias)


def state_of(job):
    """The link's last words on the turn, for its record (``device_state``): {} for a turn here."""
    process = link_of(job)
    return dict(process.details()) if process is not None else {}


def stop_note(job):
    """What the card of a stopped turn adds when the stop could not be confirmed on the device (it was asked for while the
    link was down: the reap is pending), else ''."""
    process = link_of(job)
    if process is None:
        return ""
    state = process.details()
    if not state.get("stop_requested") or getattr(process, "cleanup_confirmed", True):
        return ""
    return turn_words.device_stop_pending(state.get("alias", ""))


def lost_notice(record, run_dir=None):
    """The recovery card's text for a turn the device lost past the patience, from the record's ``device_state`` or, failing
    that, the run's ``device.json``; '' when the record is no such turn."""
    state = record.get("device_state") if isinstance(record.get("device_state"), dict) else {}
    saved = device_link.read_record(run_dir) if run_dir else {}
    alias = state.get("alias") or saved.get("device") or record.get("device") or ""
    limit = state.get("limit_seconds") or saved.get("limit_seconds") or 0
    return turn_words.device_lost_notice(alias, limit) if alias and limit else ""


def device_of_record(record):
    return str((record or {}).get("device") or "")


def register_run(state_dir, directory, record, reason):
    """Register the pending reap of the device task of ``record``'s run, when it ran on a device the Agent may still be
    on (core/device_reap.py register_run). The entry, or None. Never talks to the device."""
    if not device_of_record(record):
        return None
    return device_reap.register_run(state_dir, directory, reason, topic_id=str(record.get("topic_id") or ""),
                                    job_key=str(record.get("key") or ""))


def register_left(log, state_dir, record, directory, reason=""):
    """``register_run`` for a task runner that ended (or a Bridge that started over one that did): the reason is the record's
    (the device was lost past the patience, a stop was asked for) unless given. Logged; a failure is only logged."""
    reason = reason or (device_reap.DEVICE_LOST if record.get("status_message") == DEVICE_LOST_STATUS
                        else device_reap.STOP if record.get("cancel_requested_by") else device_reap.RUNNER_LOST)
    try:
        entry = register_run(state_dir, directory, record, reason)
    except (OSError, ValueError) as exc:
        log(f"device reap not registered job={record.get('job_id')}: {type(exc).__name__}")
        return None
    if entry is not None:
        log(f"device reap registered job={record.get('job_id')} device={entry.get('alias')} reason={entry.get('reason')}")
    return entry


def reap_run(state_dir, directory, record, reason, *, grace=3.0):
    """Register and try the reap now: True (nothing of the task is left on the device, or it ran here / ended cleanly),
    False (the device did not confirm; the entry stays for the thread). Used where a task is declared stopped."""
    if not device_of_record(record):
        return True
    entry = register_run(state_dir, directory, record, reason)
    if entry is None:
        return True
    alias = entry["alias"]
    try:
        transport = device_link.transport_for(devices.current(), alias, Path(state_dir))
        confirmed = device_reap.reap_one(transport, entry, grace)
    except (devices.DeviceConfigError, devices.UnknownDevice, OSError, dt.DeviceError):
        return False
    if confirmed:
        device_reap.forget(state_dir, alias, entry["task"])
        device_reap.note_ended(state_dir, alias, entry.get("topic_id"), entry["task"])
    return confirmed


class Devices:
    """The ``devices`` feature (app/unified.py ``features``): its thread, only when devices are configured."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.gc_at = {}

    def threads(self):
        """One worker, only while ``COVEY_DEVICES`` is on: a deployment without devices starts no thread."""
        return (("devices", self.run),) if devices.enabled() else ()

    def run(self, ctx) -> None:
        every(self.runtime, PERIOD, self.round, args=(ctx,))

    def round(self, ctx) -> None:
        """One round over every configured device; a failure of one is logged and does not stop the others."""
        config = devices.current()
        for alias in sorted(config.devices):
            try:
                self.look(ctx, config, alias)
            except Exception as exc:  # noqa: BLE001 - one device must not stop the rest
                self.runtime.log(f"devices: {alias} round failed: {type(exc).__name__}")

    def look(self, ctx, config, alias) -> None:
        state_dir = Path(ctx.state_dir)
        transport = device_link.transport_for(config, alias, state_dir)
        answered = self.probe(state_dir, config, alias, transport)
        if not answered:
            return
        if device_reap.entries(state_dir, alias):
            done, kept = device_reap.replay(state_dir, alias, transport)
            self.runtime.log(f"devices: {alias} reaped {len(done)} pending, {len(kept)} still pending")
        if time.monotonic() - self.gc_at.get(alias, float("-inf")) >= GC_EVERY:
            self.gc_at[alias] = time.monotonic()
            self.collect(alias, transport)

    def probe(self, state_dir, config, alias, transport) -> bool:
        """Whether the device answers a ``hello`` (the shim pushed first, as a turn would): the cached hello is refreshed and
        ``health.json`` says what was seen."""
        error = ""
        try:
            devices.push_file(transport, dt.shim_bytes())
            devices.write_hello(state_dir, alias, transport.call("hello", want=[], patience=config.connect_seconds + 10.0))
        except (dt.DeviceError, OSError) as exc:
            error = type(exc).__name__
        write_json(devices.device_dir(state_dir, alias) / "health.json",
                   {"at": time.time(), "ok": not error, **({"error": error} if error else {})})
        return not error

    def collect(self, alias, transport) -> None:
        try:
            answer = transport.call("gc", keep_days=GC_KEEP_DAYS, patience=60.0)
        except (dt.DeviceError, OSError) as exc:
            self.runtime.log(f"devices: {alias} gc failed: {type(exc).__name__}")
            return
        removed = [len(answer.get(name) or []) for name in ("removed_tasks", "removed_shims", "removed_checkpoints", "removed_incoming")]
        if any(removed):
            self.runtime.log("devices: %s gc removed %d tasks, %d shims, %d checkpoints, %d attachment directories"
                             % (alias, *removed))
