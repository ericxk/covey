"""Stopping the shell command a Claude Code turn waits on, so a supplement it was sent is read now (docs/architecture.md
补充投递, app/steer_interrupt.py).

Claude Code takes a queued supplement in only between two tool calls; a long command holds it back for as long as it
runs. Claude Code runs each Bash call as a child shell of its own (its own session and process group,
``bash -c '… && eval '<command>' …'``). Stopping that group ends the call: Claude Code gets its result (exit 143) and
reads the supplement next. Only the shells of the root's foreground Bash calls are stopped, each found among the CLI's
own children by its command line; a background command, a subagent's command and the turn itself go on.
"""
from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import time

from covey.core.procfs import stat

GRACE = 2.0  # SIGTERM, then SIGKILL to the group after this long.
KILL_WAIT = 1.0
SNIPPET = 160  # How much of a command's quoted form its shell's command line must contain.


def quoted(command: str) -> str:
    """``command`` as Claude Code's shell line evaluates it: single-quoted, each ' written '"'"'."""
    return "'" + command.replace("'", "'\"'\"'")


def children(pid: int) -> list[int]:
    """The direct children of ``pid`` (every thread's), without walking /proc."""
    found = []
    try:
        for thread in (Path("/proc") / str(pid) / "task").iterdir():
            try:
                found.extend(int(value) for value in (thread / "children").read_text().split())
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return sorted(set(found))


def _cmdline(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def shells(cli_pid: int, commands: list[str]) -> list[tuple[int, int]]:
    """(pid, start ticks) of the CLI's child shells running ``commands``: a child whose command line holds a command's
    quoted start. Each command matches its newest such shell once; one with none is left out."""
    rows = []
    for pid in children(cli_pid):
        row = stat(pid)
        if row is not None and row.state != "Z":
            rows.append((row.start_ticks, pid, _cmdline(pid)))
    found = []
    for command in commands:
        snippets = {quoted(text)[:SNIPPET] for text in (command, command.strip()) if text.strip()}
        for start, pid, line in sorted(rows, reverse=True):
            if any(snippet in line for snippet in snippets) and (pid, start) not in found:
                found.append((pid, start))
                break
    return found


def _same(pid: int, start: int) -> bool:
    row = stat(pid)
    return row is not None and row.start_ticks == start and row.state != "Z"


def stop(found: list[tuple[int, int]], *, grace: float = GRACE) -> int:
    """Stop each shell of ``found`` with what it started: SIGTERM to its process group (its own, Claude Code's), then
    SIGKILL after ``grace``. A shell that shares the CLI's group is signalled alone. A pid that names another process
    now is never signalled (pidfds). Returns how many shells were signalled."""
    own = os.getpgrp()
    fds = {}
    for pid, start in found:
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            continue
        if not _same(pid, start):
            os.close(fd)
            continue
        fds[fd] = (pid, start)
    signalled = len(fds)
    try:
        for signum, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, KILL_WAIT)):
            for fd, (pid, start) in list(fds.items()):
                row = stat(pid)
                try:
                    if row is not None and row.pgrp == pid and row.pgrp != own:
                        os.killpg(pid, signum)  # A session leader's group: its pid is the group's id.
                    else:
                        signal.pidfd_send_signal(fd, signum)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + wait
            while fds and (left := deadline - time.monotonic()) > 0:
                ready, _, _ = select.select(list(fds), [], [], left)
                for fd in ready:
                    fds.pop(fd)
                    os.close(fd)
    finally:
        for fd in fds:
            os.close(fd)
    return signalled
