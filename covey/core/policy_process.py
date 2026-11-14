"""Stop one identity-checked task tree, including children with their own sessions.

Freeze before discovery so an old runner cannot deliver success while being stopped.
Use pidfds, never process names or an unverified/recycled PID. No network operations.
"""
import os
from pathlib import Path
import signal
import time

from covey.core.procfs import stat
from covey.core.process import process_identity


def stop_tree(identity, *, timeout=8, before_kill=None):
    expected = {k: identity.get(k) for k in ('pid', 'start_ticks', 'boot_id')}
    if not expected.get('pid') or process_identity(expected['pid']) != expected:
        return {'confirmed': False, 'reason': 'runner_identity_mismatch', 'processes': []}
    frozen, handles = [], []
    deadline = time.monotonic() + timeout

    def freeze(pid, expected_identity=None, parent=None):
        before = process_identity(pid)
        if expected_identity is not None and before != expected_identity:
            raise RuntimeError('identity_changed')
        if before is None:
            return
        row = stat(pid)  # None when gone or unreadable, as process_identity takes it.
        if parent is not None and (row is None or row.ppid != parent):
            return  # The child exited/reparented; never act on a recycled PID.
        fd = os.pidfd_open(pid)
        if process_identity(pid) != before:
            os.close(fd)
            raise RuntimeError('identity_changed')
        handles.append(fd)
        signal.pidfd_send_signal(fd, signal.SIGSTOP)
        frozen.append(before)
        while time.monotonic() < deadline:
            row = stat(pid)
            if row is None or row.state in {'T', 't', 'Z'}:
                return
            time.sleep(.001)
        raise RuntimeError('freeze_unconfirmed')

    try:
        freeze(expected['pid'], expected)
        index = 0
        while index < len(frozen):
            if time.monotonic() > deadline or len(frozen) > 2048:
                raise RuntimeError('tree_budget')
            pid = frozen[index]['pid']
            index += 1
            seen = {r['pid'] for r in frozen}
            for thread in (Path('/proc') / str(pid) / 'task').iterdir():
                try:
                    children = (thread / 'children').read_text().split()
                except FileNotFoundError:
                    continue
                for value in children:
                    child = int(value)
                    if child not in seen:
                        try:
                            freeze(child, parent=pid)
                        except ProcessLookupError:
                            pass
                        seen.add(child)
        # Descendants first, runner last; all were frozen, none can spawn replacements.
        if before_kill is not None:
            before_kill(frozen)
        for fd in reversed(handles):
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while time.monotonic() < deadline:
            if all(process_identity(row['pid']) != row for row in frozen):
                return {'confirmed': True, 'reason': 'owned_tree_stopped', 'processes': frozen}
            time.sleep(.02)
        return {'confirmed': False, 'reason': 'cleanup_unconfirmed', 'processes': frozen}
    except (OSError, RuntimeError) as exc:
        # A partial failure must not leave an unreported, permanently frozen tree.
        for fd in reversed(handles):
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
        return {'confirmed': False, 'reason': type(exc).__name__, 'processes': frozen}
    finally:
        for fd in handles:
            os.close(fd)
