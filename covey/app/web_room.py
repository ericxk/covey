"""What the web page's private chat (私聊区, docs/web.md 私聊区) needs beside the butler's and the leads' turns: whom a butler turn
handed the owner's words to (``handed``) and when a topic turn's conclusion went to the top level of the private chat (``relayed_at``).

Reading only, as app/web_collect.py: the butler's request records (core/butler_opened.py, main-thread/opened/) and the relay records
(app/relay_send.py, main-thread/relays/), each folder indexed once and read again only when its mtime changed (every record write
replaces its file, core/state.py write_private_text, so the folder's mtime moves with it)."""
from __future__ import annotations

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


class Room:
    """The two indexes of one Collector (one thread uses it)."""

    def __init__(self) -> None:
        self.opened_at, self.opened = (), {}  # folder stamp, {butler job key: [records it wrote, in ``at`` order]}
        self.relays_at, self.relays = (), {}  # folder stamp, {job key: sent_at} of the relays that went out

    def refresh(self, state) -> None:
        """Index both folders again when they changed since the last look."""
        state = Path(state)
        stamp = folder_stamp(butler_opened.folder(state))
        if stamp != self.opened_at:
            self.opened_at, found = stamp, {}
            for _, record in butler_opened.records(state):
                if butler_opened.answered(record) and record.get("job_key"):
                    found.setdefault(str(record["job_key"]), []).append(record)
            for rows in found.values():
                rows.sort(key=lambda row: float(row.get("at") or 0))
            self.opened = found
        where = state / RELAYS
        stamp = folder_stamp(where)
        if stamp != self.relays_at:
            self.relays_at, found = stamp, {}
            for path in where.glob("*.json") if where.is_dir() else ():
                record = read_object(path)
                if record.get("state") == SENT and record.get("job_key"):
                    found[str(record["job_key"])] = float(record.get("sent_at") or record.get("at") or 0)
            self.relays = found

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
