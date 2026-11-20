"""话题加入和移出专项 (docs/butler.md 整理专项, COVEY_PROJECTS=1): an ordinary topic becomes a member of a project that is there
(``join_topic``) and a member an ordinary topic again (``leave_topic``), and the existing topics a direct start takes in (``wanted_topics``).

- ``join_topic``: topic.json's ``project`` (under the topic's lock), the record's ``threads`` (so the board and the Pins show it in the
  project's cell), a line of its ``joins`` and a ``joined`` receipt for the lead (app/project_lead.py ``add_receipt``). Callers: app/projects.py
  (``gather``, a direct start with ``topics``), app/project_tidy.py (整理专项), app/project_tools.py (the lead's project_topic_add).
- ``leave_topic`` (the owner's 移出, 整理专项's 拆去): the topic leaves the record and the lead gets a ``left`` receipt.

Locks (rules 1 and 2): a topic's lock is taken before LOCK (``set_topic_project`` takes LOCK inside it); no Feishu call is made."""
from __future__ import annotations

import re
from pathlib import Path

from covey.app import project_clicks, project_facts, project_lead
from covey.app.butler_env import arg_text
from covey.app.project_clicks import topic_ctx
from covey.core import projects
from covey.core.locks import topic_lock
from covey.core.text import cut
from covey.core.topics import set_topic_project, topic_label, topic_title


MAX_JOINED = 10  # Existing topics one direct start takes in.


def topic_line(state, topic) -> str:
    """T531「title」：where its newest job stands, for the lead's start receipt."""
    job = project_facts.newest_job(state, topic) or {}
    now = " ".join(part for part in (str(job.get("status") or ""), str(job.get("status_message") or "")) if part)
    return f"{topic_label(state, topic) or topic[:6]}「{cut(topic_title(state, topic), 30)}」" + (f"：{cut(now, 60)}" if now else "")


def wanted_topics(env, chat, raw):
    """([topic id], [{ref, why}]): the existing topics a direct start takes in, from topic_start's ``topics`` (T numbers or ids,
    a list or one string of them), and the ones it cannot with why (app/butler_topics.py topic_ref's reasons, a lead's, another
    project's, too many)."""
    from covey.app import butler_topics

    if isinstance(raw, str):
        raw = re.split(r"[\s,，、;；]+", raw)
    if not isinstance(raw, (list, tuple)):
        return [], []
    state, joins, refused = Path(env.state_dir), [], []
    for item in raw:
        ref = arg_text(item, 80)
        if not ref:
            continue
        found = butler_topics.topic_ref(env, chat, ref)
        if isinstance(found, str):
            refused.append({"ref": ref, "why": found})
            continue
        topic = found[0]
        if topic in joins:
            continue
        other = projects.topic_project(state, topic)
        if other is not None:
            name = cut(projects.read_record(state, other.id).get("name"), 30) or other.id
            refused.append({"ref": ref, "why": f"是专项「{name}」的负责人" if other.role == projects.LEAD else f"已经在专项「{name}」里"})
        elif len(joins) >= MAX_JOINED:
            refused.append({"ref": ref, "why": f"一次最多收 {MAX_JOINED} 个话题"})
        else:
            joins.append(topic)
    return joins, refused


GATHERED_NOTE = ("这些话题是整理时收进来的，它们做完不等于专项完成：这一轮写下目标（project_goal），用 project_note（标 report）"
                 "说清每个话题做到哪、还剩什么，然后等主人下一步，不要收尾")
JOINED, LEFT = "joined", "left"  # The receipts the lead gets.


BY_WORDS = {"start": "建专项时", "tidy": "整理专项", "rule": "整理专项", "lead": "负责人加话时"}  # Who took a topic in, in the
# lead's receipt and on the card's 归入记录 (core/projects.py JOIN_BY; "rule" is 整理专项 going by the 话题要点's related topics).


def join_topic(state, pid, topic, *, why="", by="tidy") -> dict:
    """An ordinary topic becomes a topic of project ``pid``: ``project`` {id, role thread} in its topic.json (under its topic lock),
    in the record's ``threads`` (so the board and the Pins show it in the project's cell, core/projects.py cells), a line of
    its ``joins`` (the card's 归入记录, ``by`` who took it in) and a ``joined`` receipt in the lead's inbox; a project folded
    away opens again (app/project_loop.py reopen). Its running turn goes on and a question waiting for the owner stays his.
    The record; ValueError when the project is over or the topic is a lead's or another project's."""
    from covey.app import project_loop

    record = projects.read_record(state, pid)
    if not record or projects.state_of(record) not in (projects.ACTIVE, projects.PAUSED):
        raise ValueError("这个专项已经结束了")
    found = projects.topic_project(state, topic)
    if found is not None and (found.id != pid or found.role != projects.MEMBER):
        raise ValueError("这个话题已经在别的专项里")
    if found is None:
        with topic_lock(topic_ctx(state, topic)):
            set_topic_project(state, topic, pid, projects.MEMBER)
    if topic not in (record.get("threads") or ()):
        projects.add_member(state, pid, topic)
        record = projects.log_join(state, pid, topic, by, why)
        title = cut(topic_title(state, topic), 30)
        project_lead.add_receipt(state, pid, JOINED, f"{BY_WORDS.get(by, BY_WORDS['tidy'])}把话题{f'「{title}」' if title else ''}"
                                 "归进了这个专项" + (f"：{cut(why, 120)}" if why else ""), thread=topic)
    if projects.folded(record):
        record = projects.update(state, pid, project_loop.reopen)
    return record


def leave_topic(state, pid, topic, *, why="", went="") -> bool:
    """移出: ``topic`` an ordinary topic again (app/project_clicks.py release), out of the record's ``threads``, and a ``left``
    receipt for the lead; whether it was the project's. ``went``: where 整理专项 takes it (拆去了新专项「X」), said to the lead
    instead of the owner's 移出 (which keeps it out of projects for good)."""
    record = projects.read_record(state, pid)
    if not record or topic not in (record.get("threads") or ()):
        found = projects.topic_project(state, topic)
        if found is None or found.id != pid:
            return False
    project_clicks.release(state, topic, pid)
    projects.remove_member(state, pid, topic)
    projects.log_leave(state, pid, topic)
    title = cut(topic_title(state, topic), 30)
    named = f"「{title}」" if title else ""
    words = (f"话题{named}{went}，不再归这个专项" if went
             else f"主人把话题{named}移出了这个专项，以后它不再归进来" + (f"：{cut(why, 120)}" if why else ""))
    project_lead.add_receipt(state, pid, LEFT, words, thread=topic)
    return True
