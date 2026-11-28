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
from covey.cards.project_card import (CANCELLED, CORRECTED, DONE, IDLE, RUNNING, STOPPED, STUCK, WAITING, Member,
                                      Project)
from covey.app.card_publish import clock, words
from covey.core import board_read, look, memory, memory_usage, memstore, project_library, project_memory, projects
from covey.core import progress as turn_progress
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


def rebound(topic, job) -> bool:
    """Whether ``topic`` (a topic.json) was bound anew after ``job`` (its newest job record) ran: a 换配置 is on record
    (``switches``) and ``job`` did not run on the binding it made (``binding_revision``), so what the topic runs with from
    now on is its own, not the job's. A topic never switched: never (its jobs ran on the one binding there is)."""
    switches, revision = topic.get("switches"), job.get("binding_revision")
    if not (isinstance(switches, list) and switches and topic.get("harness_id")):
        return False
    current = topic.get("binding_revision")
    return not (type(revision) is int and type(current) is int and revision >= current)


def member_state(job, status, now) -> str:
    """A member's state (cards/project_card.py, a core/look.py STATES key) from its newest ``job`` and what the 派发卡 makes
    of it (``status``, app/dispatch_cards.py status_of): 等你, 在做 or 可能卡住 (core/progress.py maybe_stuck) while it
    runs, else its record's tone (core/look.py record_tone: 停住了, 已停止 the owner's own stop, 做完)."""
    from covey.app import dispatch_cards as dc

    if status == dc.WAITING:
        return WAITING
    if status == dc.RUNNING:
        stuck = (job or {}).get("status") == "running" and turn_progress.progress(job, now=now).get("maybe_stuck")
        return STUCK if stuck else RUNNING
    tone = look.record_tone(job)
    return tone if tone in (STOPPED, CANCELLED) else DONE


def member_view(state_dir, topic_id, now, url="", titles=None) -> Member:
    from covey.app import dispatch_cards as dc

    job = newest_job(state_dir, topic_id)
    status, body = dc.status_of(state_dir, {"topic_id": topic_id, "starts": True}, job, now)
    ended = status not in (dc.RUNNING, dc.WAITING)
    at = (epoch_seconds((job or {}).get("updated_at")) or now) if ended else now
    seconds = run_seconds(timings_of(job or {}), None, at) if job is not None else None
    word = member_state(job, status, now)
    if word in (STOPPED, CANCELLED):  # Why it stopped and where, as the 看板 says it (its state is the fold's word).
        body = board_read.ended_line(job, now) or body
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
    owner has not corrected yet (its 不对 button is retired, T898: the owner says it in the topic)."""
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


JOIN_WORDS = {"start": "建专项时收进", "tidy": "整理专项归入", "rule": "整理专项按话题要点归入", "lead": "负责人加话时收进",
              "butler": "管家收进"}


def join_lines(state_dir, record) -> tuple:
    """The 归入记录's lines, the newest first: (T31「标题」 · 谁收进的 · 为什么, topic id for its 移出 or '' once it left or
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


def memory_listing(state_dir, record) -> tuple:
    """((entry id, text) of the project's memory, how many lines it holds): what a turn can carry (core/memory.py: a line put
    away in the history is not carried and not listed), the newest first by when a turn last used it (core/memory_usage.py,
    with COVEY_MEMORY_LEARN on) as a turn's own choice orders them; at most LIST_CAP rows, the count is of them all.
    projects/<id>/memory.json and what an older release kept in the record; a deleted entry and the style line are not rows."""
    live = project_memory.live(state_dir, record, style=False)
    used = memory_usage.last_used(state_dir) if memory.learning() else None
    found = [item for item in memory.items(project=live, project_id=str(record.get("id") or ""), redacted=True, used=used)
             if item.get("layer") != memory.HISTORY]
    texts = {row["id"]: row["text"] for row in live}
    found.sort(key=lambda item: (-memory.seen(item), item["ref"]))
    rows = [(item["ref"].rpartition(":")[2], words(texts.get(item["ref"].rpartition(":")[2], item["text"]), 120)) for item in found]
    return tuple(rows[:LIST_CAP]), len(rows)


def memory_counts(state_dir, project_id) -> tuple:
    """(rules, waiting for review) of the project's memory in use (core/memstore.py), and () when the store cannot be read. 待审核
    are the open items whose suggestion names this project."""
    try:
        found = memstore.entries(state_dir, scope="project", owner_key=project_id, kinds=("rule",))
        waiting = sum(1 for item in memstore.review_items(state_dir)
                      if isinstance(item.get("suggest"), dict) and item["suggest"].get("scope") == "project"
                      and item["suggest"].get("project_key") == project_id)
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    return len(found), waiting


def memory_url(project_id="") -> str:
    """The web page's 记忆, at the project's group (``scope`` p:<id>); '' without the web page or when it cannot be read."""
    from covey.app import web_memory

    try:
        return web_memory.center_url(scope=f"p:{project_id}" if project_id else "")
    except (OSError, ValueError, TypeError):
        return ""


def memory_rows(state_dir, record) -> tuple:
    """(entry id, text) of the project's memory, the newest first (``memory_listing``)."""
    return memory_listing(state_dir, record)[0]


def library_rows(state_dir, project_id) -> tuple:
    """(label, entry id) of the project's library index (core/project_library.py), the newest first, then (file name,) of the
    files in projects/<id>/library/ the index does not name (one level, no links), by name."""
    indexed = project_library.entries(state_dir, project_id)
    # A member turn's whole reply (``source`` result, W1-C C7) is for the lead and the topics to read, one a turn: not listed on
    # the card, where it would push the owner's files out (its path is in the lead's receipt).
    rows = [(words(project_library.row_label(row), 100), row["id"]) for row in reversed(indexed) if row.get("source") != "result"]
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


def facts(state_dir, record, now, titles=None, links=None, avatar="") -> Project:
    """What the card of ``record`` says now. ``links``: {"need": url of the question card, "threads": {topic id: url}};
    ``avatar``: the image key of the lead's picture (app/project_cards.py had it uploaded), '' for its name alone."""
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
        # 停住了 (and why) for a guard's pause, 已停止 for the owner's stop (core/projects.py pause_tone).
        status = {"stopped": STOPPED, "cancelled": CANCELLED}.get(projects.pause_tone(record), IDLE)
    else:
        status = WAITING if waiting or lead_state(state_dir, record, now).startswith("等你") else RUNNING
    need, need_from = "", ""
    if status == WAITING:  # What waits and who asks it: a member's T number, else the lead.
        need = waiting.body.removeprefix(dc.ASK_WORDS) if waiting else lead_state(state_dir, record, now).removeprefix("等你 · ")
        need_from = (waiting.label or "话题") if waiting else "负责人"
    began = float(record.get("created_at") or now)
    end = now if status != DONE else float(record.get("updated_at") or now)
    elapsed = dc.quantize(max(0.0, end - began))
    used = record.get("tokens_used")
    tokens = tile_count(used) if type(used) is int and used > 0 else ""
    # The box is the lead's own: its newest job, else (no job yet, or 换配置 bound it anew since) what its topic.json binds it
    # to (not a member's, which has its own Harness: the card lists it in its fold).
    job, bound = newest_job(state_dir, lead), read_object(topic_file(Path(state_dir), lead))
    if job is None or rebound(bound, job):
        job = bound
    harness = harness_of(state_dir, lead, job)
    model = str(job.get("model") or "").rsplit(" · ", 1)[-1]
    effort = str(job.get("effort") or "").strip()
    inputs = tuple(part for part in ((titles or {}).get(harness, harness), model,
                                     f"{answer_card.BUDGET_WORD} {effort}" if effort else "") if part)
    parts = [answer_card.figure("开始", clock(began)), answer_card.figure("已运行" if status != DONE else "用时", elapsed)]
    if tokens:
        parts.append(answer_card.figure("token", tokens))
    ask = record.get("ask") if isinstance(record.get("ask"), dict) else {}
    listed = memory_listing(state_dir, record)
    return Project(project_id=pid, lead_topic=lead, name=str(record.get("name") or ""), status=status,
                   reason=projects.stop_reason(record), need=need, need_url=str(links.get("need") or ""), need_from=need_from,
                   members=members, avatar=str(avatar or ""),
                   elapsed=elapsed, tokens=tokens,
                   lead_state=projects.reason_words(record) or lead_state(state_dir, record, now), lead_note=lead_note(record),
                   triage=triage_lines(record), joins=join_lines(state_dir, record), memory=listed[0], memory_total=listed[1],
                   memory_counts=memory_counts(state_dir, pid), memory_url=memory_url(pid), library=library_rows(state_dir, pid),
                   inputs=inputs, outputs=tuple(answer_card.output_lines(parts, answer_card.RUNNING_LINES)),
                   ask_text=str(ask.get("text") or ""), ask_at=str(ask.get("at") or ""), ask_url=str(ask.get("url") or ""),
                   origin_note=origin_words((record.get("origin") or {}).get("note") if isinstance(record.get("origin"), dict) else ""))


def origin_words(note) -> str:
    """The origin line of the card as the owner reads it today: a record written by an older release holds the older
    wording (由快事升级……), which is shown as the new one; the record itself is not rewritten."""
    note = str(note or "")
    if note.startswith("由快事升级（你说了"):
        return "由管家转成专项"
    rounds = re.fullmatch(r"由快事升级（来回第 (\d+) 轮）", note)
    return f"来回 {rounds.group(1)} 轮后转成专项" if rounds else note
