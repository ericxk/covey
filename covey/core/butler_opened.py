"""The butler's requests that opened, forked or handed on to a topic: main-thread/opened/<request token>.json,
written by app/butler_topics.py (topic_start, topic_fork, topic_send) and the project tools that open or feed a thread
(app/butler_projects.py, app/projects.py), which read and write each request's own record by its path (``record_path``,
``save``, ``done``: the request ledger of rule 6) while they handle that request; every other reader goes through here. Each
record: {job_key (the butler job's), chat_id, topic_id or fork_topic_id, plan (topic_start's), source
(topic_fork's), title, note, by ("lead": a project's lead handed on its own words, app/butler_topics.py said_by),
answer: {ok, data}, at, ...}.

The scans keep the folder's own order (``Path.glob``): the callers that pick the latest by ``at`` keep the first of
equal ones, as they did when each scanned the folder itself.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import Unreadable, keyed_json_path, read_object, update_json, write_json

OPENED = Path("main-thread") / "opened"
# The same call twice in one butler turn (``first_call``): main-thread/opened-calls/<call key>.json names the request that
# made it first. Kept as long as the requests' own records (core/maintenance_rules.py Temporary).
CALLS = Path("main-thread") / "opened-calls"
SAME_CHARS = 500  # Of a call's text: two that differ only past it are the same call.
CLAIM_SECONDS = 600  # A first call with no record or answer this long after it claimed the key is taken over.
# A hand-on's synthetic message id: HANDED + its request's token (app/butler_topics.py, app/butler_projects.py project_send;
# a lead's project_topic_start and project_topic_send go through the butler's own).
HANDED = "main-"


def request_token(request) -> str:
    """The request's name in record files, send keys and synthetic message ids."""
    return hashlib.sha256(str(request.id).encode()).hexdigest()[:32]


def handed_on(message_id) -> bool:
    """Whether a job of ``message_id`` is a turn the butler or a lead handed on to a topic: the butler took the owner's
    words with its own tools (记一下 with knowledge_save). A job's ``prompt_text`` says nothing about it: every message the
    owner writes is queued with its expanded input there (app/app.py handle_event), and so are an answer, a page's words
    (web-…) and a supplement queued again."""
    return str(message_id or "").startswith(HANDED)


def record_path(state_dir, request) -> Path:
    """Where the request's own record is (``OPENED``/<its token>.json)."""
    return keyed_json_path(state_dir, OPENED, request_token(request))


def save(path, record) -> None:
    """Write the request's ``record`` to its ``path``."""
    write_json(path, record, ensure_ascii=False, indent=1)


def done(path, record, answer):
    """Keep ``answer`` as the request's answer in its record and return it: the same request again gets it back, nothing
    sent twice (rule 6)."""
    record["answer"] = answer
    save(path, record)
    return answer


def folder(state_dir) -> Path:
    return Path(state_dir) / OPENED


def records(state_dir) -> list[tuple[str, dict]]:
    """Every record as (its request token, the record), in the folder's order; [] without the folder."""
    where = folder(state_dir)
    return [(path.stem, read_object(path)) for path in where.glob("*.json")] if where.is_dir() else []


def by_token(state_dir, token) -> dict:
    """The record of the request ``token``; {} when there is none."""
    return read_object(folder(state_dir) / (str(token) + ".json"))


def by_job(state_dir, job_key) -> list[tuple[str, dict]]:
    """The records the butler job ``job_key`` wrote, whatever their answer, in the folder's order."""
    return [(token, record) for token, record in records(state_dir) if record.get("job_key") == job_key]


def answered(record) -> bool:
    """Whether the request's tool call succeeded (its answer's ok)."""
    answer = record.get("answer")
    return isinstance(answer, dict) and answer.get("ok") is True


FORK, START, SEND = "fork", "start", "send"


def kind_of(record) -> str:
    """Which tool wrote the request's record: topic_fork's (FORK, once it has its fork_topic_id), topic_start's
    (START: its plan), else topic_send's (SEND)."""
    if record.get("fork_topic_id"):
        return FORK
    return START if isinstance(record.get("plan"), dict) else SEND


def topic_of(record):
    """The topic the request opened, forked or handed on to (its fork's, else its own topic_id)."""
    return record.get("fork_topic_id") or record.get("topic_id")


def latest_for_topic(state_dir, topic_id):
    """(token, record) of the latest successful request that opened, forked or handed on to ``topic_id``; None."""
    best = None
    for token, record in records(state_dir):
        if (topic_of(record) == topic_id and answered(record)
                and (best is None or float(record.get("at") or 0) > float(best[1].get("at") or 0))):
            best = (token, record)
    return best


def steered(state_dir, job_id, topic_id):
    """(token, record) of the latest topic_send that went to the running job ``job_id`` of ``topic_id`` as a 补充
    (its answer's data says steered); None. Of equal ``at``, the first file in sorted order."""
    found = None
    for token, record in sorted(records(state_dir), key=lambda row: row[0] + ".json"):
        data = (record.get("answer") or {}).get("data") or {}
        if (data.get("steered") is True and str(data.get("job_id")) == str(job_id) and topic_id
                and data.get("topic_id") == topic_id
                and (found is None or float(record.get("at") or 0) > float(found[1].get("at") or 0))):
            found = (token, record)
    return found


def call_key(job_key, tool, title, text, rest=None) -> str:
    """The name of a call of ``tool`` in the butler turn ``job_key`` with ``title`` (its blanks folded), the first
    SAME_CHARS characters of ``text`` and the same other arguments (``rest``: a model that calls again after a timeout
    sends them all again; one that opens the same task on another Harness on purpose names another): what ``first_call``
    takes as the same call."""
    others = json.dumps(rest or {}, ensure_ascii=False, sort_keys=True, default=str)
    raw = "\n".join((str(job_key or ""), str(tool), " ".join(str(title or "").split()), str(text or "")[:SAME_CHARS], others))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _call_path(state_dir, request, tool, title, text, rest):
    return keyed_json_path(state_dir, CALLS, call_key(request.job_key, tool, title, text, rest))


def once_a_turn(state_dir, request, tool, title, text, make, *, rest=None):
    """``make()`` (the tool's work, its answer) unless the request's turn made the same call before (WAVE12 W2-B: a model
    that calls topic_start, topic_fork or project_send again after a timeout, with a new request id, opened a second
    topic): the same ``tool``, ``title``, first SAME_CHARS of ``text`` and other arguments (``rest``; ``call_key``), in the
    same butler or lead turn (``request.job_key``). Then the first call's answer when it succeeded, a refusal while it is
    still being made (claimed, no answer, less than CLAIM_SECONDS ago). A call that did not succeed gives the key up: the
    next one is made. Never across turns; a request with no turn, or a key that cannot be read, is made as before."""
    if not getattr(request, "job_key", ""):
        return make()
    path = _call_path(state_dir, request, tool, title, text, rest)
    mine, now = request_token(request), time.time()
    found = {}

    def claim(saved):
        first = str(saved.get("token") or "")
        if first and first != mine:
            answer = by_token(state_dir, first).get("answer")
            if isinstance(answer, dict) and answer.get("ok") is True:
                found["answer"] = answer
                return saved
            if not isinstance(answer, dict) and now - float(saved.get("at") or 0) < CLAIM_SECONDS:
                found["busy"] = True
                return saved
        return {"token": mine, "job_key": request.job_key, "tool": tool, "at": now}
    try:
        with resource_lock("record", path):
            path.parent.mkdir(parents=True, exist_ok=True)
            update_json(path, claim, ensure_ascii=False, indent=1)
    except (Unreadable, OSError):
        return make()  # Not known: the call is made, as before.
    if "answer" in found:
        answer = found["answer"]
        return {**answer, "text": "这一轮已经用同样的标题和说明调过一次，没有再开：" + str(answer.get("text") or ""),
                "data": {**(answer.get("data") or {}), "repeated": True}}
    if found.get("busy"):
        return {"ok": False, "text": "这一轮里同样的调用正在办（同样的标题和说明），没有再开一个；等它的结果，不要再调。",
                "data": {"repeated": True}}
    answer = make()
    if not (isinstance(answer, dict) and answer.get("ok") is True):
        try:
            with resource_lock("record", path):
                if read_object(path).get("token") == mine:
                    path.unlink(missing_ok=True)  # Its own claim only: the next call is made.
        except OSError:
            pass
    return answer
