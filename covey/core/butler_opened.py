"""The butler's requests that opened, forked or handed on to a topic: main-thread/opened/<request token>.json,
written by app/butler_topics.py (topic_start, topic_fork, topic_send), which reads and writes each request's own
record by its path (``record_path``) while it handles that request; every other reader goes through here. Each
record: {job_key (the butler job's), chat_id, topic_id or fork_topic_id, plan (topic_start's), source
(topic_fork's), title, note, answer: {ok, data}, at, ...}.

The scans keep the folder's own order (``Path.glob``): the callers that pick the latest by ``at`` keep the first of
equal ones, as they did when each scanned the folder itself.
"""
from __future__ import annotations

from pathlib import Path

from covey.core.state import read_object

OPENED = Path("main-thread") / "opened"


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
