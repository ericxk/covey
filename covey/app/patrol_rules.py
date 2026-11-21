"""The rules of the first layer (docs/butler.md 我本来想找你的事): from the records the board already reads, which things
look as if the owner should hear of them or the projects be tidied. No model, no Feishu call, nothing written.

``look`` reads once, as 手上的事 and the board do (core/board_read.py read: one query of each index; app/pins.py
board_results for 有结果未看), so a look every minute costs what their loops do. ``find`` judges that read and gives
candidates (``Candidate``), every kind below, each about one object: 

- ``ask``: a topic's question waits for the owner over COVEY_PATROL_ASK_MINUTES (default 120);
- ``unseen``: a finished result stays unseen over COVEY_PATROL_UNSEEN_MINUTES (360);
- ``fails``: a topic's last COVEY_PATROL_FAILS (2) turns all failed;
- ``stuck``: a task has run or queued with its record unchanged over COVEY_PATROL_STALL_MINUTES (180);
- ``group``: ordinary topics of the last three days that look like one matter (the same workspace and two words of the
  title alike, or four words alike), at most GROUP_TOPICS looked at;
- ``join``: an ordinary topic that fits an active project (its name and goal share two words with the title, or one with
  the same workspace as the project's topics) and is in none;
- ``project_idle``: an active project, or one a guard paused, with receipts nobody read (or the pause) and no task running
  for COVEY_PATROL_PROJECT_MINUTES (720); the idle close of a project is app/project_loop.py's, not read here;
- ``project_messy``: a project with PROJECT_THREADS topics or more, or topics in PROJECT_SPREAD workspaces or more;
- ``deferred`` (#55, record only): a butler turn of the last day that ended done with a reply that puts something off
  (core/turn_words.py defers) while it opened, forked and handed on to no topic: nothing brings that matter back;
- ``unrelayed`` (#55, record only): a topic's turn that ended done over COVEY_PATROL_RELAY_MINUTES (10) ago, within the
  last day, whose conclusion was due at the top level (app/relay_targets.py) and whose relay record does not say it went
  (main-thread/relays/: none, ``submitting``, ``uncertain`` or ``unsent``).

``sig`` is the state the object was found in: the same sig is the same matter (core/patrol.py). Words are the title's
ASCII words of three letters and Chinese pairs, minus the pairs every request has (``STOP``).
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from pathlib import Path

from covey.app.pins import board_results, kept, laid
from covey.core import board_read, butler, butler_opened, patrol, projects, turn_words
from covey.core.board_read import ended_at, order, unrun
from covey.core.jobs import job_key, select_job_records
from covey.core.metrics import took_text
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import RUNNING, TERMINAL

GROUP_TOPICS = 30
PROJECT_THREADS = 6
PROJECT_SPREAD = 3
INBOX_LOOKED = 20
TITLE = 30
ASCII = re.compile(r"[a-z0-9]{3,}")
CJK = re.compile(r"[一-鿿]+")
STOP = frozenset("帮我 一下 这个 那个 可以 需要 请你 请帮 我想 想要 看看 看下 怎么 如何 什么 是否 能否 一个 我们 他们 然后 还有 以及 或者 如果 现在 目前 之前 之后 已经 继续 问题 处理 进行 需求 功能 相关 内容 文件 代码 结果 帮忙 麻烦 谢谢".split())


@dataclasses.dataclass(frozen=True)
class Candidate:
    """``kind`` (core/patrol.py KINDS), ``key`` the object's (kind and topic or project), ``sig`` its state, ``topics`` the
    topics it is about, ``project`` a project id where one is meant, ``what`` one line for the owner, ``wait`` seconds
    waited (0 when it has none)."""
    kind: str
    key: str
    sig: str
    chat: str
    title: str
    what: str
    topics: tuple = ()
    project: str = ""
    wait: float = 0.0

    def row(self) -> dict:
        return dataclasses.asdict(self) | {"topics": list(self.topics)}


@dataclasses.dataclass
class Look:
    state: Path
    chat: str
    now: float
    read: board_read.Read
    unseen: dict
    off: dict
    projects: list
    butler_turns: list = dataclasses.field(default_factory=list)  # ``butler_turns``: the board's read leaves them out.


def look(state, chat, now) -> Look:
    """One read of ``chat``. The board's ``keep`` is handed over when the board is laid, as app/digest.py collect does."""
    read = board_read.read(state, chat, now, keep=kept(state, chat) if laid() else ())
    unseen, off = board_results(state, chat, now, read)
    found = projects.records(state, chat) if projects.enabled() else []
    return Look(Path(state), chat, now, read, unseen, off, found, butler_turns(state, chat, now))


def butler_turn(topic, chat) -> bool:
    """Whether ``topic`` is the butler's (its family's), not a project's lead."""
    return butler.is_butler_topic(topic, chat) and not butler.is_lead(topic, chat)


def butler_turns(state, chat, now) -> list:
    """The butler's turns that ended done within patrol.GONE, newest first; only those records are read."""
    since = now - patrol.GONE

    def recent(fields):
        topic, updated = fields.get("topic_id"), fields.get("updated_at")
        if topic is not None and not butler_turn(topic, chat):
            return False
        return updated is None or (epoch_seconds(updated) or now) >= since
    return select_job_records(state, lambda record: butler_turn(record.get("topic_id"), chat) and ended_at(record) >= since,
                              chat_id=chat, status="done", narrow=recent)


def words(text) -> set:
    """The words of ``text`` that say what a matter is about."""
    text = str(text or "").lower()
    found = set(ASCII.findall(text))
    for run in CJK.findall(text):
        found |= {run[i:i + 2] for i in range(len(run) - 1)}
    return found - STOP


def clipped(text, limit=TITLE) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def stamp(value) -> float:
    """``value`` (a record's time: epoch seconds or ISO 8601) as epoch seconds; 0 when it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return epoch_seconds(value) or 0.0


def hours(seconds) -> str:
    return took_text(max(0.0, seconds))


class Reader:
    """What ``find`` needs of one ``Look``, worked out once: titles, workspaces, the project of each topic."""

    def __init__(self, seen: Look) -> None:
        self.seen = seen
        cells = seen.read.cells
        self.leads = {projects.lead_topic_id(seen.chat, record["id"]) for record in seen.projects if projects.valid_id(record.get("id"))}
        self.members = set(cells) | {topic for record in seen.projects for topic in record.get("threads") or () if isinstance(topic, str)}
        self.project_by_topic = {}  # A project topic (a member or the lead's) → its project's id: the model is told the work is in one.
        for record in seen.projects:
            pid = str(record.get("id") or "")
            if pid:
                self.project_by_topic.update({topic: pid for topic in record.get("threads") or () if isinstance(topic, str)})
            if projects.valid_id(pid):
                self.project_by_topic[projects.lead_topic_id(seen.chat, pid)] = pid
        self.latest = {}
        for record in seen.read.jobs:
            topic = record.get("topic_id") or ""
            if topic and not unrun(record) and int(record.get("job_id") or 0) > int(self.latest.get(topic, {}).get("job_id") or 0):
                self.latest[topic] = record

    def label(self, topic):
        number, title = board_read.topic_label(self.seen.state, topic)
        return (number + " " if number else "") + clipped(title), clipped(title)

    def ordinary(self, topic) -> bool:
        return topic and topic not in self.members and topic not in self.leads and topic not in self.seen.off

    def project_of(self, topic) -> str:
        """The id of the project ``topic`` belongs to, '' for a topic of none."""
        return self.project_by_topic.get(topic, "")

    def workspace(self, topic) -> str:
        return str(self.latest.get(topic, {}).get("workspace_name") or "")


def asks(reader: Reader, config) -> list:
    seen, found, newest = reader.seen, [], {}
    for record in seen.read.questions:
        topic = record.get("topic_id") or ""
        if topic and topic not in seen.off and (record.get("created_at") or 0) > newest.get(topic, {}).get("created_at", 0):
            newest[topic] = record
    for topic, record in newest.items():
        waited = seen.now - float(record.get("created_at") or seen.now)
        if waited >= config["ask_minutes"] * 60:
            label, title = reader.label(topic)
            found.append(Candidate("ask", f"ask:{topic}", str(record.get("token") or record.get("created_at")), seen.chat, title,
                                   f"{label}：有个问题等了你 {hours(waited)}", (topic,), wait=waited,
                                   project=reader.project_of(topic)))
    return found


def unseen(reader: Reader, config) -> list:
    seen, found = reader.seen, []
    for topic, record in seen.unseen.items():
        waited = seen.now - ended_at(record)
        if waited >= config["unseen_minutes"] * 60:
            label, title = reader.label(topic)
            found.append(Candidate("unseen", f"unseen:{topic}", str(record.get("job_id")), seen.chat, title,
                                   f"{label}：做完的结果放了 {hours(waited)} 还没看", (topic,), wait=waited,
                                   project=reader.project_of(topic)))
    return found


def fails(reader: Reader, config) -> list:
    seen, found, by_topic = reader.seen, [], {}
    for record in seen.read.jobs:
        topic = record.get("topic_id") or ""
        if topic and topic not in seen.off and record.get("status") in TERMINAL and not unrun(record):
            by_topic.setdefault(topic, []).append(record)
    for topic, records in by_topic.items():
        records.sort(key=board_read.job_number)
        run = 0
        for record in reversed(records):
            if record.get("status") != "failed":
                break
            run += 1
        if run >= config["fails"] and reader.latest.get(topic, {}).get("status") == "failed":
            label, title = reader.label(topic)
            found.append(Candidate("fails", f"fails:{topic}", str(board_read.job_number(records[-1])), seen.chat, title,
                                   f"{label}：连着失败了 {run} 次", (topic,), project=reader.project_of(topic)))
    return found


def stuck(reader: Reader, config) -> list:
    seen, found, running = reader.seen, [], {}
    waiting = {record.get("topic_id") for record in seen.read.questions}
    for record in seen.read.jobs:
        topic = record.get("topic_id") or ""
        if topic and record.get("status") in RUNNING and topic not in seen.off and topic not in waiting and (
                topic not in running or order(record) > order(running[topic])):
            running[topic] = record
    for topic, record in running.items():
        quiet = seen.now - (epoch_seconds(record.get("updated_at")) or seen.now)
        if quiet >= config["stall_minutes"] * 60:
            label, title = reader.label(topic)
            found.append(Candidate("stuck", f"stuck:{topic}", str(record.get("job_id")), seen.chat, title,
                                   f"{label}：任务 {hours(quiet)} 没有动静", (topic,), wait=quiet,
                                   project=reader.project_of(topic)))
    return found


def groups(reader: Reader, config) -> list:
    """Ordinary topics that look like one matter, as clusters of two or more (the newest GROUP_TOPICS topics)."""
    seen = reader.seen
    topics = sorted((topic for topic in reader.latest if reader.ordinary(topic)),
                    key=lambda topic: -board_read.job_number(reader.latest[topic]))[:GROUP_TOPICS]
    info = {topic: (words(reader.label(topic)[1]), reader.workspace(topic)) for topic in topics}
    parent = {topic: topic for topic in topics}

    def root(topic):
        while parent[topic] != topic:
            parent[topic] = parent[parent[topic]]
            topic = parent[topic]
        return topic

    for i, one in enumerate(topics):
        for other in topics[i + 1:]:
            shared = len(info[one][0] & info[other][0])
            same_place = bool(info[one][1]) and info[one][1] == info[other][1]
            if shared >= 4 or (same_place and shared >= 2):
                parent[root(other)] = root(one)
    clusters = {}
    for topic in topics:
        clusters.setdefault(root(topic), []).append(topic)
    found = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort()
        digest = hashlib.sha1("|".join(members).encode()).hexdigest()[:10]
        labels = [reader.label(topic)[0] for topic in members[:4]]
        found.append(Candidate("group", f"group:{digest}", digest, seen.chat, clipped(reader.label(members[0])[1]),
                               "这几个话题像是同一件事：" + "、".join(labels), tuple(members)))
    return found


def project_words(record) -> set:
    return words(f"{record.get('name') or ''} {record.get('goal') or ''}")


def project_places(reader: Reader, record) -> set:
    return {reader.workspace(topic) for topic in record.get("threads") or () if reader.workspace(topic)}


def joins(reader: Reader, config) -> list:
    seen, found = reader.seen, []
    live = [record for record in seen.projects if record.get("state") == projects.ACTIVE and projects.valid_id(record.get("id"))]
    if not live:
        return found
    for topic in reader.latest:
        if not reader.ordinary(topic):
            continue
        mine, place = words(reader.label(topic)[1]), reader.workspace(topic)
        best = None
        for record in live:
            shared = len(mine & project_words(record))
            near = bool(place) and place in project_places(reader, record)
            if shared >= 2 or (near and shared >= 1):
                if best is None or shared > best[0]:
                    best = (shared, record)
        if best:
            label, title = reader.label(topic)
            name = clipped(best[1].get("name"))
            found.append(Candidate("join", f"join:{topic}", best[1]["id"], seen.chat, title,
                                   f"{label}：看起来属于专项「{name}」，还没归进去", (topic,), project=best[1]["id"]))
    return found


def unread_receipts(state, project_id) -> float:
    """When the newest receipt nobody read came (of the first INBOX_LOOKED); 0 when there is none."""
    folder = projects.folder(state) / project_id / "inbox"
    try:
        paths = sorted(folder.glob("*.json"))[:INBOX_LOOKED]
    except OSError:
        return 0.0
    return max([stamp(row.get("at")) for row in map(read_object, paths) if not row.get("taken_by")], default=0.0)


def project_rows(reader: Reader, config) -> list:
    seen, found = reader.seen, []
    for record in seen.projects:
        pid = record.get("id")
        if not projects.valid_id(pid):
            continue
        state, name = record.get("state"), clipped(record.get("name"))
        threads = [topic for topic in record.get("threads") or () if isinstance(topic, str)]
        idle = state == projects.ACTIVE or (state == projects.PAUSED and record.get("state_reason") in projects.GUARD_REASONS)
        if idle and not any(reader.latest.get(topic, {}).get("status") in RUNNING for topic in [*threads, projects.lead_topic_id(seen.chat, pid)]):
            waiting = unread_receipts(seen.state, pid)
            moved = max([stamp(record.get("updated_at")), waiting,
                         *(ended_at(reader.latest[topic]) for topic in threads if topic in reader.latest)])
            quiet = seen.now - moved
            if quiet >= config["project_minutes"] * 60 and (state == projects.PAUSED or waiting):
                found.append(Candidate("project_idle", f"project_idle:{pid}", f"{state}:{int(moved)}", seen.chat, name,
                                       f"专项「{name}」有事没人动，已经 {hours(quiet)} 没有进展", tuple(threads), project=pid, wait=quiet))
        if state in (projects.ACTIVE, projects.PAUSED):
            spread = {reader.workspace(topic) for topic in threads if reader.workspace(topic)}
            if len(threads) >= PROJECT_THREADS:
                reason, sig = f"已经有 {len(threads)} 个话题", "threads"
            elif len(spread) >= PROJECT_SPREAD:
                reason, sig = f"话题分散在 {len(spread)} 个工作目录", "spread"
            else:
                continue
            found.append(Candidate("project_messy", f"project_messy:{pid}", sig, seen.chat, name, f"专项「{name}」越做越杂：{reason}", tuple(threads), project=pid))
    return found


def tidied(reader: Reader, config) -> list:
    """With 整理专项 on (COVEY_PROJECT_TIDY, core/project_proposals.py), what looks like one matter is its judgment, not these
    words: its open proposals to make a project are ``group`` candidates (the joins it is sure of it does itself; the others it
    puts to the owner on its card)."""
    from covey.core import project_proposals

    found = []
    for record in project_proposals.proposals(reader.seen.state, (project_proposals.OPEN,)):
        members = sorted(record.get("topics") or ())
        if record.get("kind") != project_proposals.CREATE or len(members) < 1:
            continue
        labels = [reader.label(topic)[0] for topic in members[:4]]
        found.append(Candidate("group", f"group:{record['id'][:10]}", record["id"][:10], reader.seen.chat,
                               clipped(record.get("name")), f"整理专项提议建专项「{clipped(record.get('name'))}」："
                               + "、".join(label for label in labels if label), tuple(members)))
    return found


# What a record of the past says and will keep saying, per state directory and job key: whether a butler job handed
# anything on (main-thread/opened/ is scanned once for it), whether a topic job's conclusion was due at the top level,
# whether its relay went. Bounded: cleared past CACHE_KEPT.
CACHE_KEPT = 2000
_handed, _due, _relayed = {}, {}, set()


def _remember(cache, key, value):
    if len(cache) >= CACHE_KEPT:
        cache.clear()
    cache[key] = value
    return value


def handed(state, key) -> bool:
    """Whether the butler job ``key`` opened, forked or handed on to a topic (an answered request of it)."""
    found = _handed.get((str(state), key))
    if found is None:
        found = _remember(_handed, (str(state), key), any(butler_opened.answered(record)
                                                           for _, record in butler_opened.by_job(state, key)))
    return found


def deferred(reader: Reader, config) -> list:
    """#55: the butler said it would come back to something and nothing will (core/turn_words.py defers)."""
    seen, found = reader.seen, []
    for record in seen.butler_turns:
        key = str(record.get("key") or job_key(str(record.get("message_id") or ""), record.get("job_id") or ""))
        text = str(record.get("final_text") or "")
        if not turn_words.defers(text) or handed(seen.state, key):
            continue
        asked = clipped(turn_words.request_line(record.get("user_text")))
        found.append(Candidate("deferred", f"deferred:{key}", key, seen.chat, asked,
                               f"管家答“{clipped(text)}”，这一轮没开也没转话题，没人接着做", (str(record.get("topic_id") or ""),),
                               wait=seen.now - ended_at(record)))
    return found


def unrelayed(reader: Reader, config) -> list:
    """#55: a topic's conclusion due at the top level whose relay is not known to have gone."""
    from covey.app.relay_send import relay_path
    from covey.app.relay_targets import awaited_target, relay_target

    seen, found = reader.seen, []
    for record in seen.read.jobs:
        topic = record.get("topic_id") or ""
        if record.get("status") != "done" or not topic or topic in seen.off:
            continue
        waited = seen.now - ended_at(record)
        if not config.get("relay_minutes", patrol.number("relay_minutes")) * 60 <= waited < patrol.GONE:
            continue
        key = str(record.get("key") or job_key(str(record.get("message_id") or ""), record.get("job_id") or ""))
        if (str(seen.state), key) in _relayed:
            continue
        state = str(read_object(relay_path(seen.state, key)).get("state") or "")
        if state in ("sent", "inline", "skipped"):
            if len(_relayed) >= CACHE_KEPT:
                _relayed.clear()
            _relayed.add((str(seen.state), key))
            continue
        due = _due.get((str(seen.state), key))
        if due is None:
            due = _remember(_due, (str(seen.state), key), bool(relay_target(seen.state, record) or awaited_target(seen.state, record)))
        if not due:
            continue
        label, title = reader.label(topic)
        why = "没有确认发出" if state else "没有发出"
        found.append(Candidate("unrelayed", f"unrelayed:{key}", state or "none", seen.chat, title,
                               f"{label}：做完 {hours(waited)} 了，结论回聊天区的那条{why}", (topic,), wait=waited,
                               project=reader.project_of(topic)))
    return found


def find(seen: Look, config=None) -> list:
    """Every candidate ``seen`` holds, one per object and kind. With 整理专项 on, ``tidied`` stands in for ``groups`` and
    ``joins``."""
    from covey.core import project_proposals

    config = config or {name: patrol.number(name) for name in patrol.NUMBERS}
    reader = Reader(seen)
    found = []
    alike = (tidied,) if project_proposals.enabled() else (groups, joins)
    for rule in (asks, unseen, fails, stuck, *alike, project_rows, deferred, unrelayed):
        found += rule(reader, config)
    return found
