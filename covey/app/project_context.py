"""What a project's memory and library (core/project_memory.py, core/project_library.py) do in the Bridge, when
COVEY_PROJECTS=1 and COVEY_PROJECT_MEMORY=1; with either off every function here does nothing and reads nothing.

- ``block``: the lines a topic of a project reads when it starts (app/runs.py puts them into the rules of a new session and
  into the reminder of a resumed one): the memory, and in a new session the library's index too. An ordinary topic, a
  lead's (it has its pack, app/project_lead.py) and a 代答 topic get none.
- ``remember_answer`` (app/questions.py): the owner's answer to a question card of a project's topic or lead becomes a memory
  entry (kind decision, source owner_answer): the question and the answer in one line. Feedback and skipped answers do not.
- ``owner_files`` (app/app.py, the message of the owner in a lead's topic: quoting the project's card goes there): the files it
  carries are copied into the library with their sha256.
- ``turn_to_library`` (app/project_loop.py after_runner, the Bridge once a topic's run has ended; rule 8: a task process writes
  nothing of the project's): the artifacts the turn's checkpoint lists and its delivery become index lines, by path or link only,
  nothing copied.

Rule 1 (AGENTS.md): the records are written under their own lock; nothing is sent to Feishu. Nothing raises: a project's memory
is never why a turn or an answer does not go through."""
from __future__ import annotations

from pathlib import Path

from covey.app.answers import SKIPPED
from covey.app.prompts import project_block
from covey.core import project_library, project_memory, projects, triage
from covey.core.attachments import ATTACHMENTS_HEADER, parse_attachment_block
from covey.core.checkpoint import checkpoint_delivery, read_checkpoint
from covey.core.text import cut
from covey.core.topics import proxy_topic

QUESTION_CHARS = 70
ANSWER_CHARS = 50
ARTIFACTS_PER_TURN = 8
FILES_PER_MESSAGE = 5


def block(ctx, job, *, resumed: bool) -> str:
    """The project block for ``job``'s turn in ``ctx``'s topic: '' unless it is a topic of a project that is not cancelled.
    ``resumed``: the memory alone (the session read the library's index when it began)."""
    try:
        if not projects.memory_enabled() or proxy_topic(ctx):
            return ""
        state = ctx.state_dir
        found = projects.topic_project(state, getattr(job, "topic_id", "") or ctx.topic_id)
        if found is None or found.role != projects.MEMBER:
            return ""
        record = projects.read_record(state, found.id)
        if not record or record.get("state") == projects.CANCELLED:
            return ""
        memory = [f"- [{row['kind']}] {row['text']}" for row in project_memory.live(state, record, style=False)]
        library = [] if resumed else ["- " + line for line in project_library.index_lines(state, found.id)]
        return project_block(record.get("name"), memory, library)
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""


def remember_answer(state_dir, topic_id, record, log=None) -> None:
    """Note the owner's saved answer ``record`` (a question record with ``answers``) of ``topic_id`` as a decision of its project.
    An answer the lead gave in the owner's place (COVEY_PROJECT_TRIAGE=1: the record is still ``held`` while it is saved,
    app/triage.py ``answer``) is not the owner's word and is not noted; the 分流记录 and the decision log hold it, and the
    owner's 不对 corrects it. A question the lead passed on (``escalated``) and the owner answered is noted as always."""
    try:
        if not projects.memory_enabled() or record.get("response_kind") == "feedback" or triage.state(record) in {triage.HELD, triage.ANSWERED}:
            return
        found = projects.topic_project(state_dir, topic_id)
        if found is None:
            return
        answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
        pairs = []
        for question in record.get("questions") or ():
            given = (answers.get(question.get("id")) or {}).get("answers")
            parts = [str(part) for part in given or () if str(part).strip() and part != SKIPPED]
            if parts:
                pairs.append(f"{cut(question.get('question'), QUESTION_CHARS)}→{cut('；'.join(parts), ANSWER_CHARS)}")
        if pairs:
            project_memory.add(state_dir, found.id, "主人答：" + "；".join(pairs), "decision", "owner_answer")
    except Exception as exc:  # noqa: BLE001 - the answer is delivered whether or not it was noted
        if log is not None:
            log(f"project memory of an answer failed: {type(exc).__name__}")


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
            line = "；".join(f"{label} {delivery[name]}" for name, label in (("commit", "提交"), ("deployed", "部署"), ("verified", "验收"))
                            if delivery.get(name))
            project_library.add_ref(state_dir, project_id, "delivery", line, name="交付", source="delivery", topic_id=topic_id)
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        if log is not None:
            log(f"project library of a turn failed: {type(exc).__name__}")
