"""网页工作台同步里不碰 Collector 缓存的那部分 (app/web_collect.py): 把话题历史的每一行配上它的任务记录 (pair, retried)、
把答过的问题挂到轮次上 (answered, put_answers, answer_rows)、问题怎样、何时关闭 (question, closed) 和负责人哪一轮
派出了哪些成员 (lead_starts). Pure functions of what the caller read: they read no cache, hold no state and take no lock; the
one read of the disk they make is lead_starts' (a member's task records), as the method it was had made."""
from __future__ import annotations

from covey.app import web_images, web_said
from covey.app.web_said import NOT_ANSWERED  # turn.data answers' choice of a question the owner chose not to answer (data contract v4).
from covey.app.answers import SKIPPED
from covey.core import board_read, triage
from covey.core.jobs import job_key, topic_job_records
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import RUNNING
from covey.core.topics import topic_file

PAIR_SLACK = 300  # Seconds after a task's record was last written that an answer line with no other task may still be its own (pair).
QUESTION_STATES = {"pending": "open", "cancelled": "cancelled", "superseded": "cancelled"}
CANCEL_REASONS = ("stale", "switched", "rewound", "lead_stop", "web_stop")  # A cancelled question's cancel_reason, pushed as its closed_by.


def moment(value) -> float:
    """``value`` (epoch seconds or an ISO time) as epoch seconds; 0 when it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return epoch_seconds(value) or 0.0


def pair(rows, records, questions=()):
    """(row, the job record of the turn the row belongs to or None, the question record a user line answers or None) for each user
    and assistant line of the history. Without it most answer turns reach the page without their task, so with no token, process or
    plan; this pairs them in three steps:

    - An answer line written with its task (``job_id``, app/worker.py) has it. A user line is paired by its message id;
      an answer's line has none and is paired by its question record (web_said.answer_record), with the task the answer started
      (its message ``answer-<token>``) or, for one handed to the task that asked, that task. The next answer line has the job of
      the user line before it.
    - A user line with no task of its own while the task of the line before has not answered yet (a supplement the lead or the
      owner sent into the running task, app/steering.py: its record is merged or there is none) leaves that task to the answer,
      not None; the line itself has no job (``put_answers`` must not put that task's answers on it).
      An answer line written before the task of the user line before it started, or long after it ended (``ran_at``), is
      not that task's: that one was queued behind the task that answers, or an automatic retry went on and answered.
    - An answer line still without a task (one in a row with no user line between: a retry after a rate limit, a stop; one
      after a line whose task is not among the records or had not started) takes, of the records no answer line took, the
      latest that had started by then, is not running, and was last written at most PAIR_SLACK seconds before it."""
    usable = [record for record in records if not board_read.unrun(record)]
    by_message = {str(record.get("message_id")): record for record in usable if record.get("message_id")}
    by_id = {str(record.get("job_id")): record for record in usable if record.get("job_id")}
    found, job, taken = [], None, set()
    for row in rows:
        if row.get("role") == "user":
            asked = web_said.answer_record(row, questions, taken) if not row.get("message_id") else None
            if asked is not None:
                own = by_message.get("answer-" + str(asked.get("token"))) or (
                    None if asked.get("continuation_job_id") else by_id.get(str(asked.get("job_id") or "")))
            else:
                own = by_message.get(str(row.get("message_id") or ""))
            if own is not None or job is None:
                job = own
            found.append([row, own, asked])
        else:
            if job is not None and not ran_at(job, epoch_seconds(row.get("time")) or 0):
                job = None  # Queued behind the task that answers, or ended before (an automatic retry went on and answered).
            found.append([row, by_id.get(str(row.get("job_id") or "")) or job, None])
            job = None
    used = {id(entry[1]) for entry in found if entry[1] is not None and entry[0].get("role") != "user"}
    for entry in found:
        if entry[0].get("role") == "user" or entry[1] is not None:
            continue
        at = epoch_seconds(entry[0].get("time")) or 0
        began = [(board_read.started_at(record), index) for index, record in enumerate(usable) if id(record) not in used
                 and record.get("status") not in RUNNING and ran_at(record, at)]
        if began:
            entry[1] = usable[max(began)[1]]
            used.add(id(entry[1]))
    for row, job, asked in found:
        yield row, job, asked


def ran_at(record, at) -> bool:
    """Whether an answer line written at ``at`` can be the task ``record``'s: it had started by then (2 s for the clocks) and was
    last written at most PAIR_SLACK seconds before (its line is written as its result goes out, then its record)."""
    return bool(at) and board_read.started_at(record) <= at + 2 and at <= moment(record.get("updated_at")) + PAIR_SLACK


def answered(questions, records) -> dict:
    """{("owner"|"agent", job key): [{question, choice}]} of the topic's answered question cards (data contract v4), from
    the question records alone, never a turn's words: an answer sent on as a follow-up task (its message ``answer-<token>``)
    is the owner's turn of that task; one handed to the task that asked it, which went on (a native question), is that
    task's own turn. A question skipped (不回答此题, 只发补充说明, or feedback on the questions) is NOT_ANSWERED."""
    follow = {str(record.get("message_id") or ""): record for record in records}
    found = {}
    for row in questions:
        answers = row.get("answers")
        if not isinstance(answers, dict) or not answers or not row.get("token"):
            continue
        listed = answer_rows(row)
        if not listed:
            continue
        after = follow.get("answer-" + str(row["token"]))
        if after is not None:
            found[("owner", job_key(after.get("message_id"), after.get("job_id")))] = listed
            continue
        if row.get("continuation_job_id"):
            continue  # Its follow-up task is not among the records: no turn to put it on.
        asked = next((record for record in records if record.get("job_id") == row.get("job_id")), None)
        if asked is not None:
            found.setdefault(("agent", job_key(asked.get("message_id"), asked.get("job_id"))), []).extend(listed)
    return found


def put_images(data, refs) -> None:
    if refs:
        data["images"] = refs
    else:
        data.pop("images", None)


def put_answers(data, answered, job, owner) -> None:
    if not answered or job is None:
        return
    listed = answered.get(("owner" if owner else "agent", job_key(job.get("message_id"), job.get("job_id"))))
    if listed:
        data["answers"] = listed


def retried(records, entries) -> dict:
    """{id(the job of an answer turn): [the tasks before it that it retried and no answer line took]}: a task the bridge's restart
    interrupted (or that failed with no line) and that an automatic retry (its record's ``retry_of``, app/auto_retry.py) went on
    with is part of the turn the retry answers: its tokens count there."""
    taken = {id(job) for _, data, job in entries if job is not None and data.get("role") == "agent"}
    by_message = {str(record.get("message_id")): record for record in records if record.get("message_id")}
    out = {}
    for _, data, job in entries:
        if job is None or data.get("role") != "agent":
            continue
        chain, seen, before = [], {id(job)}, by_message.get(str(job.get("retry_of") or ""))
        while before is not None and id(before) not in seen and id(before) not in taken and not board_read.unrun(before):
            chain.append(before)
            seen.add(id(before))
            before = by_message.get(str(before.get("retry_of") or ""))
        if chain:
            out[id(job)] = chain
    return out


def lead_starts(state, project, records) -> dict:
    """{job key of a lead's turn: [the member topics that turn started]}: a member belongs to the latest turn of the lead that began
    before the member's first task came in."""
    turns = sorted((board_read.started_at(record), job_key(record.get("message_id"), record.get("job_id")))
                   for record in records if not board_read.unrun(record))
    out = {}
    for member in project.get("threads") or ():
        if not isinstance(member, str):
            continue
        jobs = [record for record in topic_job_records(state, member) if not board_read.unrun(record)]
        if not jobs:
            continue
        first = min(jobs, key=board_read.job_number)
        came = moment(first.get("created_at")) or board_read.started_at(first)
        owner = [key for began, key in turns if began <= came + 2]
        if not owner:
            continue
        label, title = board_read.topic_label(state, member)
        out.setdefault(owner[-1], []).append({"key": member, "number": int(label[1:]) if label[1:].isdigit() else 0, "title": title,
                                              "harness": str(read_object(topic_file(state, member)).get("harness_id") or "")})
    return out


def question(row, topic, count, project="", number=0, records=(), rows=()):
    status = row.get("status", "pending")
    if status == "pending" and row.get("display_pending"):
        return None  # Held for a project's lead: not the owner's yet.
    shown = []
    for index, each in enumerate(q for q in row.get("questions") or () if isinstance(q, dict)):
        shown.append({"id": str(each.get("id") or index), "header": str(each.get("header") or ""), "question": str(each.get("question") or ""),
                      "options": [{"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                                  for o in each.get("options") or () if isinstance(o, dict)],
                      "multiple": bool(each.get("multiple"))})
    answers = row.get("answers") if isinstance(row.get("answers"), dict) else {}
    said = web_images.scrub("；".join("、".join(str(a) for a in (value.get("answers") if isinstance(value, dict) else [value]) or ())
                                      for value in answers.values()))
    state = QUESTION_STATES.get(status, "answered")
    if state == "open" and triage.held(row):
        state = "held"  # The lead has it first (COVEY_PROJECT_TRIAGE): not the owner's to answer, so not 等你 on the page.
    closed_at, closed_by = closed(row, state, records, rows)
    return {"topic_key": topic, "turn_key": f"{topic}:{count + 1}" if status == "pending" else "", "questions": shown,
            "state": state, "answer": said, "asked_at": moment(row.get("created_at")),
            "answered_at": closed_at if state == "answered" else None, "closed_at": closed_at, "closed_by": closed_by,
            "project_key": project, "topic_number": number}


def closed(row, state, records=(), rows=()) -> tuple:
    """(closed_at, closed_by) of a question record whose web ``state`` is ``state`` (data contract v9): when it stopped waiting
    (epoch seconds, None when not known) and how. ``closed_by``: owner (answered), feedback (只发补充说明 or feedback on the
    questions), skipped (每题都选了不回答), lead (the project's lead answered it, COVEY_PROJECT_TRIAGE), superseded (a later
    question of the topic folded it), stale (daily maintenance closed it), switched, rewound, lead_stop, web_stop or cancelled;
    "" while it is open or held. The time is the record's own (core/store.py SETTLED_STAMPS); a record from before it has none,
    so: an answer's follow-up task (``answer-<token>``) was created then, the lead's triage move, the newer question that folded
    it, the asking task's last write when it was cancelled with it."""
    if state not in ("answered", "cancelled"):
        return None, ""
    found = triage.of(row)
    if state == "answered":
        if found.get("state") == triage.ANSWERED:
            how = "lead"
        elif row.get("response_kind") == "feedback":
            how = "feedback"
        else:
            how = "skipped" if all(each["choice"] == NOT_ANSWERED for each in answer_rows(row) or [{"choice": ""}]) else "owner"
        follow = next((record for record in records if record.get("message_id") == "answer-" + str(row.get("token") or "")), None)
        at = (moment(row.get("answered_at")) or moment((follow or {}).get("created_at"))
              or (moment(found.get("at")) if how == "lead" else 0.0))
        return at or None, how
    if row.get("status") == "superseded":
        newer = next((other for other in rows if other.get("token") and other.get("token") == row.get("superseded_by")), None)
        return (moment(row.get("cancelled_at")) or moment((newer or {}).get("created_at"))) or None, "superseded"
    asked = next((record for record in records if record.get("job_id") == row.get("job_id")), None)
    at = moment(row.get("cancelled_at")) or (moment((asked or {}).get("updated_at")) if asked else 0.0)
    reason = str(row.get("cancel_reason") or "")
    return at or None, reason if reason in CANCEL_REASONS else "cancelled"


def answer_rows(record) -> list:
    """[{question, choice}] of a question record's saved answers, in its questions' order: the options chosen or the words
    written, several joined with "、"; NOT_ANSWERED for a question skipped or one the owner gave feedback on instead."""
    answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
    feedback = record.get("response_kind") == "feedback"
    out = []
    for index, question in enumerate(q for q in record.get("questions") or () if isinstance(q, dict)):
        given = answers.get(question.get("id"), answers.get(str(index)))
        given = given.get("answers") if isinstance(given, dict) else given
        parts = [str(part) for part in (given if isinstance(given, list) else [given] if given else []) if str(part).strip()]
        parts = [part for part in parts if part != SKIPPED]
        out.append({"question": str(question.get("question") or question.get("header") or "")[:500],
                    "choice": NOT_ANSWERED if feedback or not parts else web_images.scrub("、".join(parts))[:500]})
    return out
