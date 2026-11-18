"""An urgent supplement the model has not read: the command it waits on, found and stopped (docs/architecture.md 补充投递).

Claude Code reads a supplement only between two tool calls, so one sent during a long command waits for that command
to end (2026-11-19, #4815: "the QR code expired, send a new one" waited 280 s for a 540 s wait loop). A supplement the
owner or the butler marks urgent (``is_urgent``, the tools' ``urgent``) that is still unread URGENT_WAIT seconds after it
was sent stops the command that runs then, and only it: each command of Claude Code's Bash tool is a ``bash -c`` child
of the CLI in a process group of its own, which ``find_shell`` finds by the running step's command and ``stop_shell``
ends. The turn goes on: the tool call returns, the model reads the supplement next. A command that may leave things
half done (``risky``: a push, a deploy, a migration, a delete) is left to end by itself.

Standard library only."""
from __future__ import annotations

import html
import os
import re
import signal
import time

from covey.core import procfs

URGENT_WAIT = 20.0  # Seconds an urgent supplement waits to be read before its command is stopped.
STOP_GRACE = 5.0  # Seconds between SIGTERM and SIGKILL.
KEY_CHARS = 40  # Letters and digits of a command compared with a shell's command line.

URGENT_WORDS = ("马上", "立刻", "立即", "赶紧", "赶快", "尽快", "紧急", "急事", "很急", "先停", "停下", "停掉", "快停",
                "别跑了", "urgent", "asap")
URGENT_NOTE = ("\n\n[Bridge 说明] 这条补充是急事：送达 20 秒后你还没读到，Bridge 会停掉你正在跑的那条命令，"
               "好让你马上读到；需要的话先处理这条，再决定要不要重跑那条命令。")
RISKY = re.compile(r"\b(git\s+push|push|deploy\w*|publish|release|migrat\w*|rm|rmdir|mv|dd|mkfs|kubectl|terraform|"
                   r"helm|docker\s+push|pip\s+install|apt(-get)?|yum|npm\s+publish|drop\s+table|truncate)\b"
                   r"|deploy_bridge|>\s*/dev/sd", re.IGNORECASE)
SHELL_MARK = "shell-snapshots/snapshot-"  # In the command line of each Bash tool shell of Claude Code.


def is_urgent(text) -> bool:
    """Whether the words of a supplement ask for it at once."""
    words = str(text or "").lower()
    return any(word in words for word in URGENT_WORDS)


def command_of(detail) -> str:
    """The command of a step's ``detail`` as the process panel shows it ("描述 · command", HTML-escaped, Markdown
    escapes), unescaped; the whole detail when it names no description."""
    text = html.unescape(str(detail or "")).replace("\\", "")
    return text.split(" · ", 1)[1] if " · " in text else text


def risky(command) -> bool:
    """Whether stopping ``command`` midway may leave things half done."""
    return bool(RISKY.search(str(command or "")))


def key(text) -> str:
    return "".join(char for char in str(text or "") if char.isalnum())


def _cmdline(pid) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read(1 << 16).replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def find_shell(parent, command, *, proc="/proc") -> int | None:
    """The pid of the Bash tool shell of ``parent`` (Claude Code's pid) that runs ``command``: a child in a process
    group of its own whose command line has SHELL_MARK and the first KEY_CHARS letters and digits of ``command``.
    None when there is no such one, or more than one."""
    wanted = key(command)[:KEY_CHARS]
    if not wanted or not parent:
        return None
    found = []
    try:
        entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return None
    for entry in entries:
        stat = procfs.stat(int(entry))
        if stat is None or stat.ppid != parent or stat.pgrp != int(entry) or stat.state == "Z":
            continue
        line = _cmdline(entry)
        if SHELL_MARK in line and wanted in key(line):
            found.append(int(entry))
    return found[0] if len(found) == 1 else None


def stop_shell(pid, *, grace=STOP_GRACE) -> bool:
    """End the process group ``pid`` leads: SIGTERM, then SIGKILL after ``grace`` seconds. Only while ``pid`` is still
    the same process (its start time); True when the group got a signal."""
    first = procfs.stat(pid)
    if first is None or first.pgrp != pid:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        now = procfs.stat(pid)
        if now is None or now.start_ticks != first.start_ticks or now.state == "Z":
            return True
        time.sleep(0.2)
    now = procfs.stat(pid)
    if now is not None and now.start_ticks == first.start_ticks:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    return True
