"""What the web page's room (大厅, docs/web.md 大厅) needs beside the butler's and the leads' turns: whom a butler turn
handed the owner's words to (``handed``) and when a topic turn's conclusion went to the top level of the private chat (``relayed_at``).

Reading only, as app/web_collect.py: the butler's request records (core/butler_opened.py, main-thread/opened/) and the relay records
(app/relay_send.py, main-thread/relays/), each folder indexed once and looked at again only when its mtime changed (every record write
replaces its file, core/state.py write_private_text, so the folder's mtime moves with it). Then only the files whose stamp changed are
read again (``Files``, shared by the collectors of one WebSync): the folders hold about 1,800 records, read whole in about 4.5 s on the
network disk each time one of them was written to, by each of the four collectors, and the touch after a web answer waited on that
(2026-11-28 T959: the question stayed open on the page for many seconds after the answer had gone to its task)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from covey.app.relay_send import RELAYS
from covey.core import board_read, butler_opened, projects
from covey.core.jobs import job_key
from covey.core.state import read_object

SENT = "sent"  # A relay record whose message went out (app/relay_send.py): its turn is in the private chat.
HANDED_KEPT = 8  # The topics one butler turn is said to have handed on to, at most.


def folder_stamp(path) -> tuple:
    try:
        stat = Path(path).stat()
    except OSError:
        return ()
    return (stat.st_mtime_ns, stat.st_size)


def file_stamps(folder) -> dict:
    """{name: (mtime_ns, size, inode)} of the folder's ``*.json`` files as ``glob`` names them (no dotted ones), in the folder's order;
    {} without it. One listing and a stat a file, no file opened (on the network disk an open costs what a whole read does)."""
    out = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                out[entry.name] = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        return {}
    return out


class Files:
    """What each record file said, by its stamp: path -> ((mtime_ns, size, inode), record). Shared by every Collector of one WebSync
    (app/web_collect.py Shared), so one made again after a stuck read (WebSync.start_over_*) or the touch's, which looks seldom, reads
    only what changed since any of them looked. A late write of a left-behind thread is keyed by the stamp it read: right, or read
    again. No read of a file under ``lock``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows = {}

    def records(self, folder) -> list:
        """[(name, record)] of the folder's ``*.json`` files in the folder's order: a file whose stamp is the one last read is not
        opened again; one gone from the folder is forgotten."""
        folder = Path(folder)
        stamps = file_stamps(folder)
        out, prefix = [], str(folder) + os.sep
        for name, stamp in stamps.items():
            key = prefix + name
            with self.lock:
                kept = self.rows.get(key)
            if kept is not None and kept[0] == stamp:
                out.append((name, kept[1]))
                continue
            record = read_object(folder / name)
            with self.lock:
                self.rows[key] = (stamp, record)
            out.append((name, record))
        with self.lock:
            for key in [key for key in self.rows if key.startswith(prefix) and key[len(prefix):] not in stamps]:
                del self.rows[key]
        return out


class Room:
    """The two indexes of one Collector (one thread uses it); ``files`` the record files as last read (``Files``, shared)."""

    def __init__(self, files=None) -> None:
        self.files = Files() if files is None else files
        self.opened_at, self.opened = (), {}  # folder stamp, {butler job key: [records it wrote, in ``at`` order]}
        self.relays_at, self.relays = (), {}  # folder stamp, {job key: sent_at} of the relays that went out

    def refresh(self, state) -> None:
        """Index both folders again when they changed since the last look (the folder's stamp is taken before its files are listed:
        one written to meanwhile is looked at again next time)."""
        state = Path(state)
        where = butler_opened.folder(state)
        stamp = folder_stamp(where)
        if stamp != self.opened_at:
            found = {}
            for _, record in self.files.records(where):
                if butler_opened.answered(record) and record.get("job_key"):
                    found.setdefault(str(record["job_key"]), []).append(record)
            for rows in found.values():
                rows.sort(key=lambda row: float(row.get("at") or 0))
            self.opened_at, self.opened = stamp, found
        where = state / RELAYS
        stamp = folder_stamp(where)
        if stamp != self.relays_at:
            found = {}
            for _, record in self.files.records(where):
                if record.get("state") == SENT and record.get("job_key"):
                    found[str(record["job_key"])] = float(record.get("sent_at") or record.get("at") or 0)
            self.relays_at, self.relays = stamp, found

    def relayed_at(self, job) -> float:
        """When ``job``'s conclusion went to the top level of the private chat (seconds); 0 when it did not."""
        return self.relays.get(job_key(str(job.get("message_id") or ""), job.get("job_id") or ""), 0.0)

    def handed(self, state, job) -> list:
        """[{key, kind, number, title, project_key, how}] the butler's ``job`` handed the owner's words to: ``kind`` "topic" (``how``
        start, send or fork) or "lead" (a project's lead, ``project_key`` its project; ``how`` send)."""
        out, seen = [], set()
        for record in self.opened.get(job_key(str(job.get("message_id") or ""), job.get("job_id") or ""), ()):
            lead = str(record.get("lead_topic_id") or "")
            if lead:
                pid = str(record.get("project_id") or "")
                if (lead, "lead") in seen:
                    continue
                seen.add((lead, "lead"))
                found = projects.read_record(state, pid) if pid else {}
                out.append({"key": lead, "kind": "lead", "number": 0, "title": str(found.get("name") or ""),
                            "project_key": pid, "how": butler_opened.SEND})
                continue
            topic = str(butler_opened.topic_of(record) or "")
            if not topic or (topic, "topic") in seen:
                continue
            seen.add((topic, "topic"))
            label, title = board_read.topic_label(state, topic)
            member = projects.topic_project(state, topic)
            out.append({"key": topic, "kind": "topic", "number": int(label[1:]) if label[1:].isdigit() else 0, "title": title,
                        "project_key": member.id if member is not None else "", "how": butler_opened.kind_of(record)})
        return out[:HANDED_KEPT]
