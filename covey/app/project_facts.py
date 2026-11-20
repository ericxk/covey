"""What a project's card says, read from the records: the lead's and the members' state, the 分流记录, the 归入记录, the project's memory and library rows.

No Feishu call and no lock: ``facts`` is a pure read of the project's record and of the job and question records
(app/project_cards.py draws and writes the card from it, cards/project_card.py is its look).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from covey.cards import answer_card
from covey.cards.facts import tile_count
from covey.cards.project_card import CORRECTED, DONE, PAUSED, RUNNING, WAITING, Member, Project
from covey.app.card_publish import clock, words
from covey.core import board_read, project_library, project_memory, projects
from covey.core.jobs import topic_job_records
from covey.core.metrics import run_seconds, timings_of
from covey.core.state import epoch_seconds, read_object
from covey.core.store import directory_store
from covey.core.topics import topic_file, topic_label, topic_title

MEMBER_CAP = 40  # Member topics read for one card (the card draws MAX_FOLDS).
LIST_CAP = 50  # Rows read of the memory and the library.


def newest_job(state_dir, topic_id):
    from covey.app.dispatch_cards import job_number

    return max(topic_job_records(Path(state_dir), str(topic_id)), key=job_number, default=None)


def pending_question(state_dir, topic_ids):
    """(topic id, question message id) of the newest question that waits for the owner (core/board_read.py asks), among
    ``topic_ids``; ('', '') for none."""
    best = None
    for topic in topic_ids:
        for _, row in directory_store(Path(state_dir) / "questions").query(topic_id=topic, status="pending"):
            if not board_read.asks(row):  # As 等我 counts a question (core/board_read.py waits).
                continue
            if best is None or (row.get("created_at") or 0) > (best[0] or 0):
                best = (row.get("created_at"), topic, str(row["question_message_id"]))
    return (best[1], best[2]) if best else ("", "")


def harness_of(state_dir, topic_id, job=None) -> str:
    """The id of the Harness ``topic_id`` runs on: the one its newest job ran with (a model switch replaces the topic's
    binding for the turns after it, and the job is what runs), else the one topic.json binds it to; '' for none."""
    for value in ((job or {}).get("harness_id"), (job or {}).get("backend")):
        if isinstance(value, str) and value:
            return value
    value = read_object(topic_file(Path(state_dir), str(topic_id))).get("harness_id")
    return value if isinstance(value, str) else ""


def member_view(state_dir, topic_id, now, url="", titles=None) -> Member:
    from covey.app import dispatch_cards as dc

    job = newest_job(state_dir, topic_id)
    status, body = dc.status_of(state_dir, {"topic_id": topic_id, "starts": True}, job, now)
    ended = status not in (dc.RUNNING, dc.WAITING)
    at = (epoch_seconds((job or {}).get("updated_at")) or now) if ended else now
    seconds = run_seconds(timings_of(job or {}), None, at) if job is not None else None
    word = DONE if ended else (WAITING if status == dc.WAITING else RUNNING)
    if status in (dc.FAILED, dc.STOPPED):
        body = ("失败：" if status == dc.FAILED else "已停止：") + body
    return Member(topic_id=topic_id, status=word, label=topic_label(Path(state_dir), topic_id),
                  title=topic_title(Path(state_dir), topic_id), body=words(body), url=url,
                  took=dc.quantize(seconds) if seconds is not None else "",
                  harness=(titles or {}).get(harness_of(state_dir, topic_id, job), "") if titles else "")


def lead_state(state_dir, record, now) -> str:
    from covey.app import dispatch_cards as dc

    job = newest_job(state_dir, record.get("lead_topic_id") or "")
    if job is None:
        return "待命"
    status, body = dc.status_of(state_dir, {"topic_id": record.get("lead_topic_id"), "starts": True}, job, now)
    if status in (dc.RUNNING, dc.WAITING):
        return ("等你 · " if status == dc.WAITING else "") + words(body, 80)
    return "待命"


def lead_note(record) -> str:
    note = record.get("lead_note")
    text = note.get("text") if isinstance(note, dict) else note
    return words(text, 160) if isinstance(text, str) else ""


def triage_lines(record) -> tuple:
    """The 分流记录's lines, the newest first: a string, or (string, question token) for a row of an answer the lead gave that the
    owner has not corrected yet (it gets the 不对 button, cards/project_card.py)."""
    rows = record.get("triage") if isinstance(record.get("triage"), list) else []
    lines = []
    for row in reversed(rows[-LIST_CAP:]):
        text = row.get("text") if isinstance(row, dict) else row
        if isinstance(text, str) and text.strip():
            at = clock(row.get("at")) if isinstance(row, dict) and isinstance(row.get("at"), (int, float)) else ""
            line = (at + " " if at else "") + words(text, 120)
            if isinstance(row, dict) and row.get("kind") == "answered" and row.get("token") and not row.get("corrected"):
                lines.append((line, str(row["token"])))
            else:
                lines.append(line + (CORRECTED if isinstance(row, dict) and row.get("corrected") else ""))
    return tuple(lines)


JOIN_WORDS = {"start": "建专项时收进", "tidy": "整理专项归入", "rule": "整理专项按话题要点归入", "lead": "负责人加话时收进"}


def join_lines(state_dir, record) -> tuple:
    """The 归入记录's lines, the newest first: (T531「标题」 · 谁收进的 · 为什么, topic id for its 移出 or '' once it left or
    while 整理专项 is off)."""
    from covey.core import project_proposals

    tidy = project_proposals.enabled()
    threads = set(record.get("threads") or ())
    lines = []
    for row in reversed([row for row in record.get("joins") or () if isinstance(row, dict)][-LIST_CAP:]):
        topic = str(row.get("topic") or "")
        if not projects.TOPIC_RE.fullmatch(topic):
            continue
        title = words(topic_title(Path(state_dir), topic), 20)
        name = (topic_label(Path(state_dir), topic) or topic[:6]) + (f"「{title}」" if title else "")
        gone = bool(row.get("left_at")) or topic not in threads
        parts = [clock(row.get("at")) if isinstance(row.get("at"), (int, float)) else "", name,
                 JOIN_WORDS.get(row.get("by"), JOIN_WORDS["tidy"]), words(row.get("why") or "", 60), "已移出" if gone else ""]
        lines.append((" · ".join(part for part in parts if part), topic if tidy and not gone else ""))
    return tuple(lines)


def memory_rows(state_dir, record) -> tuple:
    """(entry id, text) of the project's memory (core/project_memory.py: projects/<id>/memory.json and what an older release
    kept in the record), the newest first; a deleted entry and the style line are not rows."""
    rows = [(row["id"], words(row["text"], 120)) for row in project_memory.live(state_dir, record, style=False)]
    return tuple(reversed(rows[-LIST_CAP:]))


def library_rows(state_dir, project_id) -> tuple:
    """(label, entry id) of the project's library index (core/project_library.py), the newest first, then (file name,) of the
    files in projects/<id>/library/ the index does not name (one level, no links), by name."""
    indexed = project_library.entries(state_dir, project_id)
    rows = [(words(project_library.row_label(row), 100), row["id"]) for row in reversed(indexed)]
    folder = projects.folder(state_dir) / project_id / "library"
    named = {row["ref"] for row in indexed if row["kind"] == "file"}
    names = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith(".") and not entry.name.endswith(".part") \
                        and entry.name not in named:
                    names.append(entry.name)
                if len(names) >= LIST_CAP:
                    break
    except OSError:
        pass
    return (*rows, *((name,) for name in sorted(names)))[:LIST_CAP]


def facts(state_dir, record, now, titles=None, links=None) -> Project:
    """What the card of ``record`` says now. ``links``: {"need": url of the question card, "threads": {topic id: url}}."""
    from covey.app import dispatch_cards as dc

    links = links or {}
    pid, lead = str(record.get("id") or ""), str(record.get("lead_topic_id") or "")
    topics = [topic for topic in (record.get("threads") or ()) if isinstance(topic, str)][:MEMBER_CAP]
    members = tuple(member_view(state_dir, topic, now, (links.get("threads") or {}).get(topic, ""), titles)
                    for topic in topics)
    waiting = next((member for member in members if member.status == WAITING), None)
    state = record.get("state")
    if state == projects.DONE:
        status = DONE
    elif state == projects.PAUSED:
        status = PAUSED
    else:
        status = WAITING if waiting or lead_state(state_dir, record, now).startswith("等你") else RUNNING
    need = ""
    if status == WAITING:
        need = waiting.body.removeprefix("等你回答：") if waiting else lead_state(state_dir, record, now).removeprefix("等你 · ")
        need = " · ".join(part for part in (waiting.label if waiting else "", need) if part)
    began = float(record.get("created_at") or now)
    end = now if status != DONE else float(record.get("updated_at") or now)
    elapsed = dc.quantize(max(0.0, end - began))
    used = record.get("tokens_used")
    tokens = tile_count(used) if type(used) is int and used > 0 else ""
    # The box is the lead's own: its newest job, else what its topic.json binds it to (not a member's, which has its own
    # Harness: the card lists it in its fold).
    job = newest_job(state_dir, lead)
    if job is None:
        job = read_object(topic_file(Path(state_dir), lead))
    harness = harness_of(state_dir, lead, job)
    model = str(job.get("model") or "").rsplit(" · ", 1)[-1]
    effort = str(job.get("effort") or "").strip()
    inputs = tuple(part for part in ((titles or {}).get(harness, harness), model,
                                     f"{answer_card.BUDGET_WORD} {effort}" if effort else "") if part)
    parts = [answer_card.figure("开始", clock(began)), answer_card.figure("已运行" if status != DONE else "用时", elapsed)]
    if tokens:
        parts.append(answer_card.figure("token", tokens))
    ask = record.get("ask") if isinstance(record.get("ask"), dict) else {}
    return Project(project_id=pid, lead_topic=lead, name=str(record.get("name") or ""), status=status,
                   need=need, need_url=str(links.get("need") or ""), members=members, elapsed=elapsed, tokens=tokens,
                   lead_state=projects.reason_words(record) or lead_state(state_dir, record, now), lead_note=lead_note(record),
                   triage=triage_lines(record), joins=join_lines(state_dir, record), memory=memory_rows(state_dir, record), library=library_rows(state_dir, pid),
                   inputs=inputs, outputs=tuple(answer_card.output_lines(parts, answer_card.RUNNING_LINES)),
                   ask_text=str(ask.get("text") or ""), ask_at=str(ask.get("at") or ""), ask_url=str(ask.get("url") or ""),
                   origin_note=origin_words((record.get("origin") or {}).get("note") if isinstance(record.get("origin"), dict) else ""))


def origin_words(note) -> str:
    """The origin line of the card as the owner reads it today: a record written before 2026-11-16 holds the older
    wording (由快事升级……), which is shown as the new one; the record itself is not rewritten."""
    note = str(note or "")
    if note.startswith("由快事升级（你说了"):
        return "由管家转成专项"
    rounds = re.fullmatch(r"由快事升级（来回第 (\d+) 轮）", note)
    return f"来回 {rounds.group(1)} 轮后转成专项" if rounds else note
