"""What 整理专项 reads of the records and says about them, as plain functions: the words for topics and projects, a 话题要点's sign,
where a topic is now, and the claim that takes an open proposal to its decided state (docs/butler.md 整理专项). Nothing here sends
a card or calls a model; app/project_tidy.py and the app/tidy_*.py modules it is made of use it."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import topic_job_records
from covey.core.statuses import RUNNING
from covey.core.text import cut
from covey.core.topics import label_title

TURN = "turn"  # The reason a turn the owner started gives (``saw``): a join only, and only when the 话题要点 changed.
NOW_CHARS = 80  # Of a topic's latest conclusion on the proposal card (现在：…).
PROJECT_KINDS = (proposals.SPLIT, proposals.MERGE, proposals.LEAVE, proposals.RENAME, proposals.FOLD)  # Proposals about a project.


def ended(record) -> bool:
    return record.get("status") not in RUNNING and bool(record.get("status"))


def project_name(state, pid) -> str:
    return str(projects.read_record(state, pid).get("name") or "专项")


def labels(state, topics) -> str:
    """Every topic's T number (the owner, 2026-11-18: 不准用“等”字省略), plain: the log's and a proposal's text."""
    return "、".join(label_title(state, topic)[0] or "话题" for topic in topics)


class Taken(Exception):
    """A proposal some other click decided first: ``state`` is what it is now."""

    def __init__(self, state) -> None:
        super().__init__(state)
        self.state = state


def claim(to):
    """The edit (core/project_proposals.py update_proposal) that takes an open proposal to ``to`` and raises Taken for one that is not
    open any more, so two clicks at once (the card's and the web page's) do not both carry it out."""
    def edit(data):
        if data.get("state") != proposals.OPEN:
            raise Taken(data.get("state") or "")
        data.update(state=to, decided_at=time.time())
    return edit


def gist_sign(state, topic) -> str:
    """What the judge reads of a topic's 话题要点 that may change its project, as a short hash ('' without one)."""
    gist = topic_gist.gist_of(topic_gist.read(state, topic))
    if not gist:
        return ""
    text = "\n".join([str(gist.get("essence") or ""), str(gist.get("goal") or ""), ",".join(map(str, gist.get("related") or ()))])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def where_now(state, chat, topic) -> str:
    """Where ``topic`` is now, in its own words: the phase its newest turn's checkpoint says (a short phrase in the owner's
    language, what the task card's status line shows), or ''."""
    jobs = sorted(topic_job_records(Path(state), topic, chat_id=chat), key=lambda job: int(job.get("job_id") or 0))
    for job in reversed(jobs[-3:]):
        path = str(job.get("checkpoint_path") or "")
        phase = read_checkpoint(Path(path)).get("phase") if path else ""
        if isinstance(phase, str) and phase.strip():
            return cut(phase, NOW_CHARS)
    return ""


def latest(conclusions) -> str:
    """The first sentence of a topic's latest conclusion when it has no phase to say: where it is now, on the proposal card
    ('' for one too short to say anything, as a lone "!")."""
    text = " ".join(str(conclusions[-1] if conclusions else "").split())
    first = re.split(r"(?<=[。！？!?])", text, maxsplit=1)[0] if text else ""
    return cut(first, NOW_CHARS) if len(first) >= 6 else ""


def foldable(state, pid) -> bool:
    """Whether project ``pid`` is active and nothing of it runs or waits for the owner (so folding it away loses nothing)."""
    record = projects.read_record(state, pid)
    if record.get("state") != projects.ACTIVE:
        return False
    topics = [record.get("lead_topic_id"), *(record.get("threads") or ())]
    return not any(job.get("status") in RUNNING or job.get("status") == "waiting_user"
                   for topic in topics if topic for job in topic_job_records(state, topic))
