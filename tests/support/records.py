"""Records on disk as the Bridge writes them: a state directory for one test, a topic's topic.json, a job
record and a run directory's files, each written by the production writer under the production name, and
what was written read back. The code under test writes through core/ as in the Bridge; the test asserts on
the record a later read would see.

A test that means to write a record no writer makes (malformed, from an older release, past the status
guard of upsert_job_record) writes it itself and says why."""
from __future__ import annotations

import tempfile
from pathlib import Path

from covey.core import run_dir as rd
from covey.core.jobs import job_path, read_job_record, upsert_job_record
from covey.core.state import ensure_private_dir, write_json
from covey.core.topics import topic_file

# run_files' keyword -> the file's name in a run directory.
RUN_FILES = {"handoff": rd.HANDOFF, "runner": rd.RUNNER, "wake": rd.WAKE, "done": rd.DONE, "exit": rd.EXIT}


def state_dir(test) -> Path:
    """A fresh state directory, removed by ``test``'s cleanups."""
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    return Path(temp.name)


def saved(state, key) -> dict:
    """The job record ``key`` as written in the state directory ``state``; {} when there is none."""
    return read_job_record(job_path(Path(state), key)) or {}


def write_topic(where, data=None, /, *, topic_id=None, **fields) -> Path:
    """Write ``data`` updated with ``fields`` as a topic's topic.json, as the Bridge's topic writers do
    (write_json, ensure_ascii=False, indent=2); its path. ``where`` is a topic's context (the file beside
    its history, where read_topic looks) or a state directory and the topic's ``topic_id`` (core/topics.py
    topic_file; it names the directory and is not written into the file)."""
    data = {**(data or {}), **fields}
    if topic_id is None:
        path = Path(where.history_path).parent / "topic.json"
    else:
        path = topic_file(Path(where), topic_id)
    write_json(path, data, ensure_ascii=False, indent=2)
    return path


def write_job(state, key, **fields) -> dict:
    """Write job ``key``'s record with ``fields`` through upsert_job_record (so with the key, revision,
    schema_version and times it adds); the record."""
    return upsert_job_record(Path(state), key, **fields)


def run_files(where, key=None, /, *, folders=(), **files) -> Path:
    """A run directory with ``files`` in it; its path. ``where`` is the directory, or a state directory
    with the job's ``key`` (core/run_dir.py run_dir). The directory and each of ``folders`` (rd.INBOX,
    rd.ACKS ...) are made private as the Bridge makes them; each of ``files`` (handoff=, runner=, wake=,
    done=, exit=, RUN_FILES) is written by write_json under its run_dir name."""
    directory = rd.run_dir(where, key) if key is not None else Path(where)
    ensure_private_dir(directory)
    for folder in folders:
        ensure_private_dir(directory / folder)
    for name, value in files.items():
        write_json(directory / RUN_FILES[name], value, ensure_ascii=False)
    return directory
