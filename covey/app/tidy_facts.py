"""What 整理专项 reads of the records and says about them, as plain functions of the state directory: the words for topics and
projects, a 话题要点's sign, where a topic is now, the topics a 整理 looks at, the proposal card's view and the topics ticked on it, the
claim that takes an open proposal to its decided state, and the rows' words (docs/projects.md 整理专项). They read records and
write none (``claim`` returns the edit), send no card and call no model. This is the bottom of the app/tidy_*.py modules: every
other one may import it, it imports none of them (tests/test_structure.py TidyPartsTest)."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from covey.app import project_sorter as sorter
from covey.app import topic_links
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist
from covey.core.butler import is_butler_topic
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import jobs_dir, topic_job_records
from covey.core.state import epoch_seconds
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
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
    """Every topic's T number, plain: the log's and a proposal's text."""
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


def owner_chat(runtime) -> str:
    """The owner's private chat ('' before the Bridge knows it)."""
    return runtime.access.owner_chat() or ""


def judge_tools(env) -> tuple:
    """(the backend, the context) the small model's calls go through: the Claude app's, (None, None) without one."""
    app = env.topics.apps.get("claude") if env is not None and env.topics is not None else None
    harness = getattr(app, "harness", None)
    return getattr(harness, "backend", None), getattr(harness, "context", None)


def live_projects(state, chat) -> list:
    return [record for record in projects.records(state, chat) if record.get("state") in (projects.ACTIVE, projects.PAUSED)]


def recent_topics(state, chat, now, *, scope="", records=()) -> list:
    """The topics a 整理 looks at, the latest first: those with a turn in the last days, and every topic of a live project."""
    window = now - proposals.number("days") * 86400
    members = {topic for record in records for topic in record.get("threads") or ()}
    latest = {}
    for _, record in directory_store(jobs_dir(state)).query(chat_id=chat):
        topic = record.get("topic_id")
        if not isinstance(topic, str) or not projects.TOPIC_RE.fullmatch(topic) or is_butler_topic(topic, chat):
            continue
        stamp = epoch_seconds(record.get("updated_at")) or 0
        latest[topic] = max(latest.get(topic, 0), stamp)
    chosen_ = [topic for topic, stamp in latest.items() if stamp >= window or topic in members]
    if scope:
        chosen_ = [topic for topic in chosen_ if in_scope(state, topic, scope, records)]
    return sorted(chosen_, key=lambda topic: -latest[topic])[:sorter.MAX_TOPICS]


def in_scope(state, topic, scope, records) -> bool:
    word = scope.lower()
    found = projects.topic_project(state, topic)
    if found is not None and any(record["id"] == found.id and (word in str(record.get("name") or "").lower() or record["id"] == scope)
                                 for record in records):
        return True
    text = " ".join([label_title(state, topic)[1], topic_gist.lines(topic_gist.read(state, topic))]).lower()
    return word in text


def run_path(state, run) -> Path:
    """The record file of the 整理 ``run`` (its 整理专项 card's message and view)."""
    return proposals.folder(state) / "runs" / f"{run}.json"


def by_rule(state, chat, topic, records) -> tuple:
    """(project id, why) when the 话题要点 of ``topic`` names in ``related`` topics of one live project of ``records``, and
    of no other; ('', '') otherwise. No model call."""
    related = topic_gist.gist_of(topic_gist.read(state, topic)).get("related") or ()
    numbers = topic_links.topic_numbers(state, chat)
    members = {other: record["id"] for record in records for other in record.get("threads") or ()}
    hits = {}
    for ref in related if isinstance(related, list) else ():
        found = re.fullmatch(r"[Tt](\d+)", str(ref).strip())
        other = numbers.get(int(found.group(1))) if found else None
        if other and other != topic and other in members:
            hits.setdefault(members[other], []).append(f"T{found.group(1)}")
    if len(hits) != 1:
        return "", ""
    pid, refs = next(iter(hits.items()))
    return pid, f"它的话题要点说和 {'、'.join(refs[:3])} 相关，那是这个专项的话题"


def rule_join(topic, pid, why) -> dict:
    return proposals.new_proposal(proposals.JOIN, topics=[topic], project=pid, why=why, source="rule", confidence=1.0)


def still_in(state, topics, pid) -> tuple:
    """(``topics`` still members of project ``pid``, in order; the words for the others: where they went)."""
    kept, elsewhere_, out = [], {}, []
    for topic in dict.fromkeys(topics or ()):
        found = projects.topic_project(state, topic)
        if found is not None and found.id == pid and found.role == projects.MEMBER:
            kept.append(topic)
        elif found is not None:
            elsewhere_.setdefault(found.id, []).append(topic)
        else:
            out.append(topic)
    words = [f"{labels(state, group)} 已归进专项「{project_name(state, other)}」" for other, group in elsewhere_.items()]
    if out:
        words.append(f"{labels(state, out)} 已不在专项「{project_name(state, pid)}」里")
    return kept, "；".join(words)


def elsewhere(state, topics) -> str:
    """Where ``topics`` are: T12 已在专项「X」里 (one clause a project); '' for none."""
    by = {}
    for topic in topics:
        found = projects.topic_project(state, topic)
        by.setdefault(found.id if found is not None else "", []).append(topic)
    return "；".join(f"{labels(state, group)} 已在专项「{project_name(state, pid)}」里" for pid, group in by.items() if pid)


def went(state, topics) -> str:
    """Where ``topics`` went: T19、T31 已归进专项「X」 (one clause a project)."""
    by = {}
    for topic in topics:
        found = projects.topic_project(state, topic)
        by.setdefault(found.id if found is not None else "", []).append(topic)
    return "；".join(f"{labels(state, group)} 已归进专项「{project_name(state, pid)}」" if pid
                    else f"{labels(state, group)} 已归进专项" for pid, group in by.items())


def proposal_view(state, record) -> dict:
    """What the proposal card shows of ``record`` besides its rows and choices."""
    prefs = proposals.prefs(state)
    accepted = int(prefs.get("accepted_in_row") or 0)
    return {"proposal": record["id"], "labels": [label_title(state, topic) for topic in record.get("topics") or ()],
            "name": record.get("name"), "goal": record.get("goal"), "why": record.get("why"),
            "first": not prefs.get("first_sent"), "accepted": accepted,
            "ask_auto": accepted >= proposals.ACCEPTS_TO_ASK and not prefs.get("auto_create") and not prefs.get("asked_auto"),
            "state": record.get("state") or proposals.OPEN}


def chosen(state, chat, view, fields) -> tuple:
    """(the topics the owner left ticked or added on the proposal card, the T numbers he typed that are no topic of his).
    A card without choices (sent before them) keeps the proposal's topics."""
    choices = view.get("choices") or []
    if not choices or (cards.TOPICS_FIELD not in fields and cards.MORE_FIELD not in fields):  # 建专项 or the rename form
        return None, []
    picked = fields.get(cards.TOPICS_FIELD)
    picked = picked if isinstance(picked, list) else ([picked] if isinstance(picked, str) and picked else [])
    topics = [choices[int(value)]["topic"] for value in picked
              if str(value).isdigit() and int(value) < len(choices)]
    numbers, unknown = topic_links.topic_numbers(state, chat), []
    for word in re.findall(r"[Tt]?\d+", str(fields.get(cards.MORE_FIELD) or "")):
        topic = numbers.get(int(word.lstrip("Tt")))
        if topic and not sorter.hidden(state, topic) and not is_butler_topic(topic, chat):
            topics.append(topic)
        else:
            unknown.append("T" + word.lstrip("Tt"))
    return list(dict.fromkeys(topics)), unknown


def row_item(state, record, topics) -> dict:
    """What a row of the 整理专项 card shows of its proposal, as data (cards/project_proposal.py suggestion_block draws it, the web
    page's 整理专项 panel says the same): kind, name, the project's name, a merge's projects' names, why, goal, ``topics`` (the
    entries app/tidy_words.py ``entries`` makes of the record's)."""
    return {"kind": record["kind"], "name": record.get("name") or "",
            "project": project_name(state, record["project"]) if record.get("project") else "",
            "projects": [project_name(state, pid) for pid in record.get("projects") or ()],
            "why": record.get("why") or "", "goal": record.get("goal") or "", "topics": topics}


def line(state, record, linked=None) -> str:
    """One row of the 整理专项 card: Markdown when ``linked`` (the record's topics as app/tidy_words.py ``linked`` words them) is
    given (a row marked ``md``), else plain words (the log's, and rows kept before)."""
    kind, why = record["kind"], record.get("why") or ""
    esc = cards.literal_text if linked is not None else (lambda text: text)
    topics = linked if linked is not None else labels(state, record.get("topics") or ())
    name = esc(project_name(state, record.get("project"))) if record.get("project") else ""
    said = {
        proposals.CREATE: f"新建「{esc(record.get('name') or '')}」：{topics}",
        proposals.LEAVE: f"把 {topics} 移出「{name}」",
        proposals.MERGE: "合并" + "、".join(f"「{esc(project_name(state, pid))}」" for pid in record.get("projects") or ())
                         + (f"，叫「{esc(record['name'])}」" if record.get("name") else ""),
        proposals.SPLIT: f"从「{name}」拆出 {topics}，另建「{esc(record.get('name') or '')}」",
        proposals.RENAME: f"「{name}」改名「{esc(record.get('name') or '')}」",
        proposals.JOIN: f"把 {topics} 归进「{name}」",
        proposals.FOLD: f"收起「{name}」"}.get(kind, kind)
    return said + (f"（{esc(why)}）" if why else "")
