"""What a project's memory and library (core/project_memory.py, core/project_library.py) do in the Bridge, when
COVEY_PROJECTS=1 and COVEY_PROJECT_MEMORY=1; with either off every function here does nothing and reads nothing.

- ``block``: the lines a topic of a project reads when it starts (app/runs.py puts them into the rules of a new session and
  into the reminder of a resumed one): the memory, and in a new session the library's index too. An ordinary topic, a
  lead's (it has its pack, app/project_lead.py) and a 代答 topic get none. The memory is ``memory_view``'s, the one read the
  lead's pack and the web page take too (core/memory.py project_view): its three layers, each with its own room, what was
  voided since the topic's previous turn, the version.
- ``remember_answer`` (app/questions.py): the owner's answer to a question card becomes decisions (core/memstore.py
  add_decision: the project's decisions.jsonl for a project's topic or lead, the topic's for an ordinary topic), one a question
  answered ("主人答：question→answer", ``answer_line``; ``question_ref`` the record's token and the question's place, so an answer
  submitted again is not noted twice). Feedback and skipped answers do not, nor the card's own extra question
  (app/question_dedup.py; ``settle`` takes the owner's pick there), nor what is no answer (``real``, rules alone: words typed
  in the topic that are none of the options, or a reply that asks back or holds on). A question that names an entry of another project's memory
  by its id (``elsewhere``) is history at once. ``decision_rows``: those decisions as the lines of the project's memory read
  them (core/memory.py project_view), so every reader of it (a member topic's turn, the lead's pack, triage's grounds) has them.
- ``owner_files`` (app/app.py, the message of the owner in a lead's topic: quoting the project's card goes there): the files it
  carries are copied into the library with their sha256.
- ``turn_to_library`` (app/project_loop.py after_runner, the Bridge once a topic's run has ended; rule 8: a task process writes
  nothing of the project's): the artifacts the turn's checkpoint lists and its delivery become index lines, by path or link only,
  nothing copied; ``result_to_library`` (the same, C7): a member turn's whole reply, kept in result-files/, one line by its
  path, ``redacted`` when its topic has redaction on (only such a topic reads it then: ``block``).

Rule 1 (AGENTS.md): the records are written under their own lock; nothing is sent to Feishu. Nothing raises: a project's memory
is never why a turn or an answer does not go through."""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.app import question_dedup
from covey.app.answers import MESSAGE, SKIPPED
from covey.app.prompts import project_block, project_memory_lines
from covey.core import board_read
from covey.core import memory as core_memory
from covey.core import memory_usage, memstore, project_library, project_memory, projects, triage
from covey.core.attachments import ATTACHMENTS_HEADER, parse_attachment_block
from covey.core.checkpoint import DELIVERY_LABELS, checkpoint_delivery, read_checkpoint
from covey.core.redaction import topic_redaction
from covey.core.state import write_json
from covey.core.text import cut
from covey.core.topics import proxy_topic, topic_label

ANSWER_CHARS = 190  # Of an answer that does not fit with its question in one line: what it keeps before the question is cut.
ANSWER_HEAD = "主人答："
# What makes an answer no answer (``real``): it asks back (ends with a question mark or 吗、么、呢, sentence marks after it
# aside) or holds on (starts with 等等、先别). Rules alone, no model: what slips through the owner voids from the search.
ASKS_BACK = re.compile(r"(?:[?？]|[吗么呢])[\s。.！!~～…]*$")
HOLDS_ON = re.compile(r"^[\s，,。.、：:]*(?:等等|先别)")
RECOMMENDED = re.compile(r"[（(]\s*推荐\s*[）)]")  # An option's own mark, not part of what it says.
_MARKS = re.compile(r"[\s，,。.！!、；;：:（）()【】\[\]“”\"'‘’~～…]+")
ARTIFACTS_PER_TURN = 8
FILES_PER_MESSAGE = 5


def member_record(ctx, job):
    """(project id, its record) of the project ``job``'s topic is an ordinary topic of, that is not cancelled; None otherwise
    (also with COVEY_PROJECTS or COVEY_PROJECT_MEMORY off, and for a 代答 topic)."""
    if not projects.memory_enabled() or proxy_topic(ctx):
        return None
    state = ctx.state_dir
    found = projects.topic_project(state, getattr(job, "topic_id", "") or ctx.topic_id)
    if found is None or found.role != projects.MEMBER:
        return None
    record = projects.read_record(state, found.id)
    if not record or record.get("state") == projects.CANCELLED:
        return None
    return found.id, record


def memory_rows(ctx, job) -> tuple[str, list]:
    """(project id, its live memory lines without the style line) for COVEY_MEMORY=1's one block (app/memory_turn.py); ('', [])
    when ``member_record`` has none. Never raises."""
    try:
        found = member_record(ctx, job)
        if not found:
            return "", []
        rows = project_memory.live(ctx.state_dir, found[1], style=False)
        return found[0], rows + [row for row in decision_rows(ctx.state_dir, found[0], rows) if not row["deleted"]]
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return "", []


DECISION_VALID = {"done": "done", "archived": "done", "pending": "done"}  # A decision's status as a line's ``valid``.


def decision_rows(state_dir, project_id, rows=None) -> list:
    """The project's decisions noted in its decisions.jsonl (core/memstore.py add_decision: the owner's answers since the
    memory moved there) as lines of its memory ({id, text, kind decision, source owner_answer, at, deleted, valid, origin,
    code}), the ones ``rows`` (the memory's own lines, read when None) do not have already; [] when none can be read."""
    try:
        if rows is None:
            rows = project_memory.entries(state_dir, projects.read_record(state_dir, project_id))
        known = {row.get("id") for row in rows}
        out = []
        for entry in memstore.decisions(state_dir, "project", project_id):
            entry_id = entry.ref.rsplit(":", 1)[-1]
            if entry_id in known or not entry.ref.startswith("decision:"):
                continue
            origin = {key: value for key, value in (entry.origin or {}).items() if key not in ("at", "topic_key")}
            token, _, place = str(origin.get("question") or "").partition("-")
            if project_memory.TOKEN.fullmatch(token):  # The question record and its place, as an older answer line names them.
                origin["question"] = token
                origin.setdefault("index", int(place) if place.isdigit() else origin.get("index"))
            if (entry.origin or {}).get("topic_key"):
                origin["topic"] = entry.origin["topic_key"]
            row = {"id": entry_id, "text": entry.text, "kind": "decision", "source": "owner_answer" if entry.said == "owner" else "lead",
                   "at": entry.at, "deleted": (entry.edited_at or entry.at or 1) if entry.status == "void" else 0,
                   "origin": {key: value for key, value in origin.items() if value not in (None, "")}, "code": entry.code}
            if entry.superseded_by:
                row.update(valid="superseded", superseded_by=entry.superseded_by.rsplit(":", 1)[-1])
            elif entry.status in DECISION_VALID:
                row["valid"] = DECISION_VALID[entry.status]
            if entry.redacted:
                row["redacted"] = True
            out.append(row)
        return out
    except Exception:  # noqa: BLE001 - the memory's own lines are read all the same
        return []


def marked(state_dir, topic_id) -> bool:
    """Whether ``topic_id`` reads the lines from topics with redaction on: its own redaction is on (off when it cannot be read)."""
    try:
        return bool(topic_redaction(state_dir, topic_id))
    except ValueError:
        return False


def memory_view(ctx, job, *, redacted=None):
    """The project's memory as ``job``'s turn in a member topic reads it (core/memory.py project_view of every entry), None when
    ``member_record`` has none: ``since`` the start of the topic's previous turn (core/board_read.py previous_start), so the lines
    voided after it are said. ``redacted``: whether the topic reads the lines from topics with redaction on (None: its own
    setting, ``marked``)."""
    found = member_record(ctx, job)
    if found is None:
        return None
    pid, record = found
    topic = getattr(job, "topic_id", "") or ctx.topic_id
    since = board_read.previous_start(ctx.state_dir, topic, getattr(job, "job_id", 0))
    rows = project_memory.entries(ctx.state_dir, record)
    return core_memory.project_view(rows + decision_rows(ctx.state_dir, pid, rows), pid,
                                    redacted=marked(ctx.state_dir, topic) if redacted is None else redacted, since=since,
                                    used=memory_usage.last_used(ctx.state_dir) if core_memory.learning() else None)


def block(ctx, job, *, resumed: bool) -> str:
    """The project block for ``job``'s turn in ``ctx``'s topic: '' unless it is a topic of a project that is not cancelled.
    ``resumed``: the memory alone (the session read the library's index when it began). With COVEY_MEMORY=1 the memory is in
    the one memory block (app/memory_turn.py), so only the library's index is here."""
    try:
        found = member_record(ctx, job)
        if found is None:
            return ""
        pid, record = found
        state = ctx.state_dir
        memory = [] if core_memory.enabled() else project_memory_lines(memory_view(ctx, job))
        topic = getattr(job, "topic_id", "") or ctx.topic_id
        library = [] if resumed else ["- " + line for line in project_library.index_lines(state, pid, redacted_reader=marked(state, topic))]
        return project_block(record.get("name"), memory, library, limit=None)
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""


def library_block(ctx, job) -> str:
    """The project's library index alone (``block``'s, without the memory) for a new session's first message when the
    rules are at the system level (app/runs.py ``_background``: it changes as the project's topics go on); '' outside a
    project or with an empty library."""
    try:
        found = member_record(ctx, job)
        if found is None:
            return ""
        pid, record = found
        topic = getattr(job, "topic_id", "") or ctx.topic_id
        library = ["- " + line for line in project_library.index_lines(ctx.state_dir, pid,
                                                                        redacted_reader=marked(ctx.state_dir, topic))]
        return project_block(record.get("name"), [], library, limit=None) if library else ""
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""


def answer_line(question, answer) -> str:
    """One answered question as a line of the project's memory, "主人答：<question>→<answer>": both whole when they fit in
    core/project_memory.py ANSWER_TEXT, else the question cut first, the answer keeping at least ``ANSWER_CHARS`` (the whole of
    both stays in the question record kept beside the memory)."""
    question, answer = " ".join(str(question or "").split()), " ".join(str(answer or "").split())
    room = project_memory.ANSWER_TEXT - len(ANSWER_HEAD) - 1
    if len(question) + len(answer) > room:
        answer = cut(answer, max(room - len(question), ANSWER_CHARS))
        question = cut(question, room - len(answer))
    return f"{ANSWER_HEAD}{question}→{answer}"


def elsewhere(state_dir, project_id, lines, log=None) -> None:
    """Of ``lines`` (``remember_answer``'s, each with ``asked``: the question's id, header and text), the ones whose question
    names an entry of another project's memory by its id (core/project_memory.py quoted) get ``valid`` done: the card sorted
    out that project's memory (a migration asking how each of its lines counts), and what the owner chose holds in that line,
    not as a decision of this project, so it is noted as history and never carried. Only a question with a word that looks
    like an id makes the projects' memories be read; when they cannot be, the answers are noted as decisions as before."""
    asked = [line for line in lines if project_memory.QUOTE.search(line["asked"])]
    if not asked:
        return
    try:
        known = project_memory.ids(project_memory.across(state_dir))
    except Exception as exc:  # noqa: BLE001 - noted as decisions, as before
        if log is not None:
            log(f"project memory answers not read against the other projects: {type(exc).__name__}")
        return
    for line in asked:
        about = project_memory.quoted(known, project_id, line["asked"])
        if about:
            line["valid"] = "done"
            if log is not None:
                log(f"project memory answer {line['index']} noted as history: it names project {about[0]}'s lines")


def remember_answer(state_dir, topic_id, record, log=None, *, owners=False, settles=None, origin=None, proof=None) -> None:
    """Note the owner's saved answer ``record`` (a question record with ``answers``) of ``topic_id`` as decisions (core/memstore.py
    add_decision): the project's (COVEY_PROJECT_MEMORY=1) for a topic or the lead of a project, the topic's own (COVEY_MEMORY on)
    for an ordinary one; a decision each question answered (``answer_line``), its ``question_ref`` ``<token>-<place>`` so it is
    noted once; one whose question is about another project's memory is history (``elsewhere``). Only for ``owners``: the
    owner himself submitted it (app/questions.py owner_submitted: his click, his message in the topic, his answer on the web
    page or the one he gave the butler); an answer not known to be his is never noted as ``who="owner"`` (W0-1: the words a
    lead or the butler sent a waiting topic had been noted as the owner's). Only the record's
    ``questions``: the card's extra question (app/question_dedup.py) is never a decision, its pick goes to ``settle``, which
    writes its rule only for ``owners`` too.
    An answer the lead gave in the owner's place (COVEY_PROJECT_TRIAGE=1: the record is still ``held`` while it is saved,
    app/triage.py ``answer``) is not the owner's word and is not noted; the 分流记录 and the decision log hold it, and the
    owner corrects it in the topic (the 分流记录's 不对 is retired, T898). A question the lead passed on (``escalated``) and the owner answered is noted as always.
    ``settles``: whether the extra question's pick writes its rule (WAVE12 C2: the owner's own click or web page answer;
    ``owners`` when not given); ``proof``: that click's core/memory_gate.py Proven (app/memory_proof.py by_click), which the
    rule is written through. ``origin``: where the owner's word came in (app/questions.py: the question's token, his
    message's id, ``via``), kept on each decision's origin. Only real answers (``answered``, ``real``) are noted, and only a
    real answer is what a rule of the extra question says to do."""
    settles = owners if settles is None else settles
    came = {key: str(value) for key, value in (origin or {}).items() if key in ("question", "message_id", "via") and value}
    try:
        if record.get("response_kind") == "feedback" or triage.state(record) in {triage.HELD, triage.ANSWERED}:
            return
        state = Path(state_dir)
        found = projects.topic_project(state, topic_id)
        if found is not None and not projects.memory_enabled():
            return
        scope, owner = ("project", found.id) if found is not None else ("topic", str(topic_id or ""))
        if scope == "project" and not projects.read_record(state, owner):
            raise KeyError("project record")  # Gone: nothing to note it in (logged below).
        if scope == "topic" and (not core_memory.enabled() or not (state / "topics" / owner / "topic.json").is_file()):
            return
        lines = answered(record) if owners else []
        saved = record.get("answers") if isinstance(record.get("answers"), dict) else {}
        given = sum(1 for answer in saved.values() if isinstance(answer, dict)
                    and any(str(part).strip() and part != SKIPPED for part in answer.get("answers") or ()))
        if owners and len(lines) < given and log is not None:
            log(f"question answers not noted as decisions: {given - len(lines)} of {given} not an answer")  # Never his words.
        if lines:
            token = str(record.get("token") or "")
            if scope == "project":
                elsewhere(state, owner, lines, log)
            redacted = marked(state, topic_id)
            if scope == "project" and project_memory.TOKEN.fullmatch(token):
                keep_record(state, owner, {**record, "topic_id": record.get("topic_id") or topic_id})
            for line in lines:
                memstore.add_decision(state, scope=scope, owner_key=owner, question=line["question"], answer=line["answer"],
                                      text=line["text"], question_ref=f"{token}-{line['index']}" if project_memory.TOKEN.fullmatch(token) else "",
                                      topic_key=str(topic_id or ""), chosen=line["answer"], category=question_dedup.category(line["asked"]),
                                      polarity=question_dedup.polarity(line["answer"]),
                                      status="done" if line.get("valid") == "done" else "active", redacted=redacted,
                                      origin={"topic_key": str(topic_id or ""), "item": line["item"], "index": line["index"],
                                              **({"job": str(record["job_key"])} if record.get("job_key") else {}), **came},
                                      who="owner")
        question_dedup.settle(state, topic_id, record, {line["item"]: line["answer"] for line in answered(record)}, owner=settles,
                              proof=proof, log=log)  # Only the owner's own click or web answer (``settles``) writes a rule.
    except Exception as exc:  # noqa: BLE001 - the answer is delivered whether or not it was noted
        if log is not None:
            log(f"memory of an answer failed: {type(exc).__name__}")


RECORD_KEYS = ("token", "topic_id", "job_id", "job_key", "questions", "answers", "supplement", "attachments", "created_at")


def keep_record(state_dir, project_id, record) -> None:
    """The question record as it was answered kept whole beside the project's memory the first time (answers/<token>.json,
    core/project_memory.py answer_record reads it): the options, their descriptions and the context a decision's line leaves out."""
    path = project_memory.answer_path(state_dir, project_id, str(record.get("token") or ""))
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"schema_version": project_memory.SCHEMA, **{key: record[key] for key in RECORD_KEYS if key in record},
                      "noted_at": time.time()}, ensure_ascii=False, indent=1)


def _plain(text) -> str:
    """``text`` as an option's words are compared (``real``): its 推荐 mark, blanks and punctuation dropped, lower case."""
    return _MARKS.sub("", RECOMMENDED.sub("", str(text or ""))).lower()


def asks_back(text) -> bool:
    """Whether ``text`` asks back or holds on rather than answers (``ASKS_BACK``, ``HOLDS_ON``)."""
    words = " ".join(str(text or "").split())
    return bool(words) and (bool(ASKS_BACK.search(words)) or bool(HOLDS_ON.match(words)))


def real(question, parts, channel) -> bool:
    """Whether ``parts`` (what was given to ``question``, skipped ones left out) is a real answer, by rules alone (no model):
    every part one of the question's options (picked, or its words typed: ``_plain``) always; for a question with options,
    words typed in the topic (``channel`` app/answers.py MESSAGE) that are none of them never (a remark in the topic while a
    card waits is taken as its answer); anything else (a question without options, the card's or the page's answer box) unless
    it asks back or holds on (``asks_back``)."""
    options = {_plain(option.get("label")) for option in question.get("options") or () if isinstance(option, dict)}
    options.discard("")
    if options and all(_plain(part) in options for part in parts):
        return True
    if options and channel == MESSAGE:
        return False
    return not any(asks_back(part) for part in parts)


def answered(record) -> list:
    """The questions of ``record`` the owner really answered (skipped ones and those ``real`` says are none left out), in
    order: {index (from 1), item (its id), question, answer, text (``answer_line``), asked (its id, header and words)}. Never
    the card's extra question."""
    answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
    by = record.get("answer_by") if isinstance(record.get("answer_by"), dict) else {}
    lines = []
    for index, question in enumerate(record.get("questions") or (), 1):
        if not isinstance(question, dict):
            continue
        given = (answers.get(question.get("id")) or {}).get("answers")
        parts = [str(part) for part in given or () if str(part).strip() and part != SKIPPED]
        if parts and real(question, parts, str(by.get("channel") or "")):
            words = str(question.get("question") or question.get("header") or "")
            lines.append({"index": index, "item": str(question.get("id") or ""), "question": cut(" ".join(words.split()), 590),
                          "answer": cut("；".join(parts), 590), "text": answer_line(words, "；".join(parts)),
                          "asked": " ".join(str(question.get(key) or "") for key in ("id", "header", "question"))})
    return lines


def owner_files(state_dir, topic_id, chat_id, text, media, log=None) -> None:
    """The files the owner's message ``text`` lists (the attachments block the Bridge wrote) copied into the library of the project
    whose lead's topic is ``topic_id``; only a file saved under ``media`` (the topic's incoming_media) is."""
    try:
        if not projects.memory_enabled() or ATTACHMENTS_HEADER not in str(text or "") or not projects.is_lead_topic(state_dir, topic_id, chat_id):
            return
        found = projects.topic_project(state_dir, topic_id)
        root = Path(media).resolve()
        for item in parse_attachment_block(text)[:FILES_PER_MESSAGE]:
            path = Path(item.path).resolve()
            if root in path.parents and path.is_file():
                project_library.add_file(state_dir, found.id, path, name=item.name or path.name, source="owner_file", topic_id=topic_id)
    except Exception as exc:  # noqa: BLE001 - the message goes on
        if log is not None:
            log(f"project library of a message failed: {type(exc).__name__}")


def source_redacted(state_dir, topic_id) -> bool:
    """Whether what ``topic_id`` wrote is for the topics with redaction on alone: its own redaction is on, or its setting
    cannot be read (``marked`` is the reader's side, off then)."""
    try:
        return bool(topic_redaction(state_dir, topic_id))
    except ValueError:
        return True


def result_to_library(state_dir, project_id, topic_id, job_record, path, log=None) -> None:
    """The whole reply of a member topic's turn that ended (C7: kept at ``path`` by app/project_loop.py after_runner,
    app/artifacts.py write_result_file) noted in the library of ``project_id`` by its path, "T12 #41 结论全文" (source
    ``result``); ``redacted`` when its topic has redaction on or that cannot be read (``source_redacted``): only a topic
    with redaction on lists it then (core/project_library.py index_lines)."""
    try:
        if not projects.memory_enabled() or not path:
            return
        label = topic_label(Path(state_dir), topic_id) or str(topic_id)[:8]
        project_library.add_ref(state_dir, project_id, "path", str(path), name=f"{label} #{job_record.get('job_id')} 结论全文",
                                source="result", topic_id=topic_id, redacted=source_redacted(state_dir, topic_id))
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        if log is not None:
            log(f"project library of a result failed: {type(exc).__name__}")


def turn_to_library(state_dir, project_id, topic_id, job_record, log=None) -> None:
    """The artifacts and the delivery of a topic's turn that ended well (``job_record``, its job record) noted in the library of
    ``project_id``: where they are, not copies."""
    try:
        if not projects.memory_enabled() or job_record.get("status") != "done":
            return
        path = str(job_record.get("checkpoint_path") or "")
        checkpoint = read_checkpoint(Path(path)) if path else {}
        artifacts = checkpoint.get("artifacts")
        for ref in [str(item) for item in artifacts if item][:ARTIFACTS_PER_TURN] if isinstance(artifacts, list) else ():
            project_library.add_ref(state_dir, project_id, "link" if ref.startswith(("http://", "https://")) else "path", ref,
                                    source="artifact", topic_id=topic_id)
        delivery = checkpoint_delivery(checkpoint)
        if delivery:
            line = "；".join(f"{label} {delivery[name]}" for name, label in DELIVERY_LABELS if delivery.get(name))  # As line_of reads it.
            project_library.add_ref(state_dir, project_id, "delivery", line, name="交付", source="delivery", topic_id=topic_id)
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        if log is not None:
            log(f"project library of a turn failed: {type(exc).__name__}")
