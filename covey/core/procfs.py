"""A process's /proc/<pid>/stat and this boot's id, read one way for every reader in the package. Standard library
only. The boot watchdog (service/boot.py), the memory watchdog (service/memguard.py) and an extension's runtime supervisor
(an extension's supervisor) keep their own readers: each of them runs without the rest of the package."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Stat:
    """What the Bridge reads of /proc/<pid>/stat: the command name (comm, between the first "(" and the last
    ")") and fields 3 to 6, 22 and 24 of proc(5)."""
    name: str
    state: str
    ppid: int
    pgrp: int
    session: int
    start_ticks: int
    rss_pages: int


def stat(pid) -> Stat | None:
    """``pid``'s stat; None when the process has gone or its stat cannot be read, decoded or parsed."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        fields = raw[raw.rindex(")") + 1:].split()
        return Stat(raw[raw.index("(") + 1:raw.rindex(")")], fields[0], int(fields[1]), int(fields[2]),
                    int(fields[3]), int(fields[19]), int(fields[21]))
    except (OSError, ValueError, IndexError):
        return None


def boot_id() -> str:
    """This boot's id. Raises OSError when it cannot be read."""
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
