"""What a new topic reads of the workspace's recent topics (COVEY_WORKSPACE_GISTS=N, off unset): the 话题要点
(core/topic_gist.py) of the last N other topics of the same workspace in the same chat, so a new topic knows what the
workspace was busy with lately (the conclusions of the topics before do not come back otherwise). For app/runs.py: only a
new session's rules carry the block (app/prompts.py workspace_gists_block), never a resumed turn's reminder, so the rules
of a session do not change under it.

Who reads what: only the owner's own turn (``owner_turn``) in an ordinary topic (not the butler's, which has its own
snapshot; not a 代答 topic); only topics of the same chat (a group's topic never reads the private chat's, nor the other
way); a topic with redaction on (or whose setting cannot be read) is read only by one with it on. A topic without a gist
is skipped (gists are written while 整理专项 is on, COVEY_PROJECT_TIDY). The topics are the ones with a job of the
workspace within DAYS, newest first; each gist is cut to its essence and goal. Nothing is written; nothing raises into
the turn.

Whose topics (``scope``): a topic in a project reads the other topics of the same project (any workspace); a topic that
started in the home (``home_start``) reads only those that used one of the repositories it works with (``repos``: named
for it or written, core/topic_repos.py works_with; one its steps only read counts on neither side), none when it has
none, so the home's root never mixes topics together; any other topic reads its workspace's, as before.
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.app.prompts import workspace_gists_block
from covey.core import env as cenv
from covey.core import topic_gist
from covey.core.butler import is_butler_topic
from covey.core.collab import is_channel
from covey.core.jobs import select_job_records
from covey.core.redaction import topic_redaction
from covey.core.state import epoch_seconds
from covey.core import projects
from covey.core.text import cut
from covey.core.topic_repos import of_topic, repo_of, works_with
from covey.core.topics import home_start, label_title, load_topic, proxy_topic

ENV = "COVEY_WORKSPACE_GISTS"
MAX = 10  # The most topics one block may carry.
DAYS = 14  # Topics with a job of the workspace within this many days.
ESSENCE, GOAL = 80, 100  # Characters of each topic's essence and goal in the block.


DEFAULT = 3  # Unset.


def count(environ=None) -> int:
    """How many recent topics a new topic carries: COVEY_WORKSPACE_GISTS as a whole number from 1 to MAX; DEFAULT unset
    or empty; 0 (off) for 0, off and anything else."""
    raw = str(cenv.get(ENV, "", environ) or "").strip()
    if not raw:
        return DEFAULT
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= MAX else 0


def _redacted(state_dir, topic_id) -> bool:
    try:
        return bool(topic_redaction(state_dir, topic_id))
    except ValueError:
        return True


def recent(state_dir, chat, workspace, topic_id, limit, *, redacted=False, now=None, keep=None) -> list:
    """[(topic, gist)] of the last ``limit`` other topics of ``workspace`` in ``chat`` that have a gist, newest first;
    a redacting one only when ``redacted``. With ``keep`` (``keep(topic, record)``) ``workspace`` is not asked for: a
    topic is read when ``keep`` says so."""
    now = time.time() if now is None else now
    since = now - DAYS * 86400

    def fresh(fields):  # A record whose time cannot be read is read whole.
        at = epoch_seconds(fields.get("updated_at"))
        return at is None or at >= since

    records = select_job_records(state_dir, lambda record: ((keep is not None or record.get("workspace_name") == workspace)
                                                            and record.get("topic_id") not in ("", None, topic_id)),
                                 chat_id=chat, narrow=fresh)
    found, seen = [], set()
    for record in records:
        topic = str(record.get("topic_id") or "")
        if topic in seen:
            continue
        seen.add(topic)
        if is_butler_topic(topic, chat) or is_channel(topic) or (keep is not None and not keep(topic, record)):
            continue
        if _redacted(state_dir, topic) and not redacted:
            continue
        gist = topic_gist.gist_of(topic_gist.read(state_dir, topic))
        if gist:
            found.append((topic, gist))
            if len(found) >= limit:
                break
    return found


def _data(state_dir, topic_id) -> dict:
    try:
        return load_topic(Path(state_dir), topic_id) if topic_id else {}
    except Exception:  # noqa: BLE001 - no record, no scope
        return {}


def scope(state_dir, topic_id, workspace):
    """(name, keep) the topic reads by: ``keep`` is None for the workspace's topics, else a ``keep(topic, record)`` for
    ``recent``; keep False (never) with a name '' when there is nothing to read (see the module's docstring)."""
    data = _data(state_dir, topic_id)
    found = projects.topic_project(Path(state_dir), topic_id) if topic_id else None
    if found is not None and found.role == projects.MEMBER and projects.enabled():

        def same_project(other, record):
            there = projects.topic_project(Path(state_dir), other)
            return there is not None and there.id == found.id
        return workspace, same_project
    if not home_start(data):
        return workspace, None
    own = [repo for repo in of_topic(data) if works_with(repo)]  # Not one its steps only read.
    mine = {repo_of(repo.path) for repo in own} - {""}
    if not mine:
        return "", False
    name = "、".join(dict.fromkeys(repo.name for repo in own))

    def used(other, record):
        theirs = ({repo_of(repo.path) for repo in of_topic(_data(state_dir, other)) if works_with(repo)}
                  | {repo_of(record.get("workdir"))})
        return bool(mine & theirs)
    return name, used


def lines(state_dir, found) -> list:
    """One line a topic: ``- T12「标题」：本质；现在的目标：…``."""
    rows = []
    for topic, gist in found:
        label, title = label_title(state_dir, topic)
        name = label + (f"「{cut(title, 30)}」" if title else "") or "一个话题"
        goal = f"；现在的目标：{cut(gist['goal'], GOAL)}" if gist.get("goal") else ""
        rows.append(f"- {name}：{cut(gist['essence'], ESSENCE)}{goal}")
    return rows


def block(ctx, job, *, owner_turn) -> str:
    """The block of a new session's rules for ``job`` (''): COVEY_WORKSPACE_GISTS on, the owner's own turn in an
    ordinary topic with a workspace, and gists to say."""
    try:
        limit = count()
        topic_id = str(getattr(job, "topic_id", "") or ctx.topic_id or "")
        chat = str(getattr(job, "chat_id", "") or "")
        if (not limit or job is None or not ctx.workspace_name or not chat or proxy_topic(ctx)
                or is_butler_topic(topic_id, chat) or not owner_turn()):
            return ""
        name, keep = scope(ctx.state_dir, topic_id, ctx.workspace_name)
        if keep is False:
            return ""
        found = recent(ctx.state_dir, chat, ctx.workspace_name, topic_id, limit,
                       redacted=_redacted(ctx.state_dir, topic_id), keep=keep)
        return workspace_gists_block(name, lines(ctx.state_dir, found))
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""
