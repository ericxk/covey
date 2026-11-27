"""Deploy approvals of a project (WAVE12 C4): the owner said yes to deploying a batch on a lead's question card, and the
lead hands the batch on with the approval's id, which the Bridge checks and writes at the head of the handoff.

- An approval is written by the Bridge only (``note``, from app/questions.py once an answer is on its way): a question of the
  lead's ``project_ask`` whose ``kind`` is ``deploy`` and that carries a ``deploy`` object ({commits, topics, target}),
  answered by the owner himself (app/answers.py AnswerBy ``party`` owner: his click, his web answer, his words in the topic;
  never the butler's or a lead's), with what he picked saying yes (``granted``: an option's own ``grants_approval`` first,
  else app/question_dedup.py ``polarity`` of its label; one pick that says no, or none that says yes, grants nothing).
  It goes into the project's record, field ``approvals`` (the last KEPT), as ``{id, kind, token, item, answer, commits,
  topics, target, asked_at, approved_at, by, expires_at, revoked_at, state, used_by}``, ``state`` ``active`` for TTL seconds.
- A no to a later deploy question of the same batch (a commit in common) revokes it (``revoked_at``, ``state`` revoked).
- ``check``: an approval the lead names is good only while ``active`` (``state_of``: revoked, consumed and expired are not), of
  this project (another project's id is not in this record), for the topic it names (when it names any), and only when every
  commit the task names is one of its batch. ``used`` consumes it (``used_by`` {topic, job, request, at}, ``state`` consumed): one
  approval, one hand-on. ``line`` is the Bridge's head line the topic reads; ``spoke_since`` the reminder that the owner said
  something after he said yes (a reminder, not a refusal).

The record is written under its own lock (core/projects.py update); nothing here sends anything."""
from __future__ import annotations

import re
import time

from covey.core import projects, turn_source
from covey.core.text import stamp

FIELD = "approvals"
KEPT = 20
TTL = 72 * 3600  # WAVE12 §6.1 #10: an approval stands three days, then the lead asks again.
ACTIVE, CONSUMED, EXPIRED, REVOKED = "active", "consumed", "expired", "revoked"
STATES = (ACTIVE, CONSUMED, EXPIRED, REVOKED)
DEPLOY = "deploy"
SEQ = "approval_seq"  # The record's counter of approval ids (A1, A2…): an id is never given twice in a project.
COMMIT = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")  # A commit id as a task names it (7 to 40 hex digits).
TEXT = 200


def deploy_question(question) -> bool:
    """Whether ``question`` (one of a question record's) asks to deploy a batch: ``kind`` deploy and a ``deploy`` object."""
    return isinstance(question, dict) and question.get("kind") == DEPLOY and isinstance(question.get("deploy"), dict)


def _strings(value, limit=40) -> list:
    return [" ".join(str(item).split())[:TEXT] for item in value if isinstance(item, str) and item.strip()][:limit] \
        if isinstance(value, list) else []


def granted(question, answer):
    """True when the owner's pick(s) ``answer`` (a record's ``answers[<id>]``: {"answers": [labels…]}) on ``question`` say yes
    to deploying, False when one says no, None when they say neither (free words, nothing picked). An option's own
    ``grants_approval`` (a bool) decides it; without one, app/question_dedup.py ``polarity`` of its label."""
    from covey.app.question_dedup import polarity

    picked = answer.get("answers") if isinstance(answer, dict) else None
    picked = [item for item in picked if isinstance(item, str) and item.strip()] if isinstance(picked, list) else []
    options = {str(option.get("label") or "").strip(): option for option in question.get("options") or ()
               if isinstance(option, dict)}
    said = []
    for label in picked:
        option = options.get(label.strip())
        if option is None:
            said.append(None)  # Free words, not an option: never an approval.
        elif isinstance(option.get("grants_approval"), bool):
            said.append(option["grants_approval"])
        else:
            said.append({"yes": True, "no": False}.get(polarity(label)))
    if False in said:
        return False
    return True if said and all(value is True for value in said) else None


def state_of(row, now=None) -> str:
    """What an approval row is now: revoked (``revoked_at``), consumed, expired (past ``expires_at``), else active."""
    now = time.time() if now is None else now
    if not isinstance(row, dict):
        return EXPIRED
    if (row.get("revoked_at") or 0) > 0 or row.get("state") == REVOKED:
        return REVOKED
    if row.get("state") == CONSUMED:
        return CONSUMED
    if now >= float(row.get("expires_at") or 0):
        return EXPIRED
    return ACTIVE


def rows(record) -> list:
    """The approval rows of a project's record, oldest first."""
    found = record.get(FIELD) if isinstance(record, dict) else None
    return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []


def find(record, approval_id) -> dict | None:
    wanted = str(approval_id or "").strip().upper()
    return next((row for row in rows(record) if str(row.get("id") or "").upper() == wanted), None) if wanted else None


def _same_commit(a, b) -> bool:
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def _shares(row, commits) -> bool:
    return any(_same_commit(mine, theirs) for mine in _strings(row.get("commits")) for theirs in commits)


def project_of(state_dir, topic_id):
    """(project id, record) when ``topic_id`` is a project's lead topic; None else."""
    found = projects.topic_project(state_dir, str(topic_id or ""))
    if found is None:
        return None
    record = projects.read_record(state_dir, found.id)
    if not record or str(record.get("lead_topic_id") or "") != str(topic_id or ""):
        return None
    return found.id, record


def note(state_dir, question, by, *, now=None, log=None) -> dict:
    """What the owner's answer of the question record ``question`` (status submitting, its ``answers`` saved) does to the
    project's approvals, written under the record's lock: ``{"granted": [ids], "revoked": [ids]}`` (both empty for anything but
    the owner's own answer of a lead's deploy question). ``by``: who answered (app/answers.py AnswerBy). Never raises."""
    done = {"granted": [], "revoked": []}
    try:
        from covey.app.answers import answer_by

        who = answer_by(by)
        asked = [q for q in question.get("questions") or () if deploy_question(q)]
        if not asked or question.get("origin") != "project_ask" or who is None or who.party != turn_source.PARTY_OWNER:
            return done
        if question.get("response_kind") == "feedback":
            return done
        found = project_of(state_dir, question.get("topic_id"))
        if found is None:
            return done
        now = time.time() if now is None else now
        answers = question.get("answers") if isinstance(question.get("answers"), dict) else {}
        decided = [(q, granted(q, answers.get(q.get("id")))) for q in asked]

        def change(record):
            kept = record[FIELD] = rows(record)
            for q, yes in decided:
                plan = q["deploy"]
                commits = _strings(plan.get("commits"))
                if yes is False:
                    for row in kept:
                        if state_of(row, now) == ACTIVE and commits and _shares(row, commits):
                            row.update(revoked_at=now, state=REVOKED)
                            done["revoked"].append(row["id"])
                    continue
                if yes is not True:
                    continue
                seq = int(record.get(SEQ) or 0) + 1
                record[SEQ] = seq
                pick = (answers.get(q.get("id")) or {}).get("answers") or []
                row = {"id": f"A{seq}", "kind": DEPLOY, "token": str(question.get("token") or ""), "item": str(q.get("id") or ""),
                       "answer": "、".join(str(item) for item in pick if isinstance(item, str))[:TEXT],
                       "commits": commits, "topics": _strings(plan.get("topics")),
                       "target": " ".join(str(plan.get("target") or "").split())[:TEXT],
                       "asked_at": float(question.get("created_at") or 0), "approved_at": now,
                       "by": {"party": who.party, "channel": who.channel, "message_id": who.message_id},
                       "expires_at": now + TTL, "revoked_at": 0, "state": ACTIVE, "used_by": []}
                kept.append(row)
                done["granted"].append(row["id"])
            del kept[:-KEPT]
        projects.update(state_dir, found[0], change)
    except Exception as exc:  # noqa: BLE001 - the answer goes on; without the approval the lead asks again
        if log is not None:
            log(f"deploy approval not noted token={question.get('token')}: {type(exc).__name__}")
        return {"granted": [], "revoked": []}
    return done


def named_commits(*texts) -> list:
    """The commit ids the texts name (COMMIT), once each, in order."""
    found = []
    for text in texts:
        for match in COMMIT.finditer(str(text or "")):
            if match.group(0) not in found:
                found.append(match.group(0))
    return found


def replayed(row, request, topic) -> bool:
    """Whether ``row`` was consumed by the tool request ``request`` itself (for ``topic``, when given): the same call again
    (app/butler_requests.py answers a request once; a replay comes back with its id), not another hand-on."""
    return bool(request) and any(isinstance(item, dict) and item.get("request") == request
                                 and (not topic or item.get("topic") == topic) for item in row.get("used_by") or ())


def check(record, approval_id, commits, *, topic="", request="", now=None) -> tuple:
    """(the approval row, '') when ``approval_id`` is good for handing on a deploy of ``commits`` (those the task names) to
    ``topic``; (None, why) else: not of this project, revoked, consumed (but by the request ``request`` itself: ``replayed``),
    expired, for other topics, or a commit the task names is not in its batch."""
    now = time.time() if now is None else now
    row = find(record, approval_id)
    if row is None:
        return None, f"没有授权凭证 {approval_id}：只有这个专项里主人在问题卡上同意部署后 Bridge 给的凭证才算（别的专项的不算）。"
    state = state_of(row, now)
    if state == CONSUMED and replayed(row, request, topic):
        state = ACTIVE
    if state == REVOKED:
        return None, f"授权凭证 {row['id']} 已撤销：主人后来对这一批说了不部署。要部署就用 project_ask 重新问主人。"
    if state == CONSUMED:
        return None, f"授权凭证 {row['id']} 已经用过（一次授权只派一次部署）。要再部署就用 project_ask 重新问主人。"
    if state == EXPIRED:
        return None, f"授权凭证 {row['id']} 已过期（{TTL // 3600} 小时内有效）。要部署就用 project_ask 重新问主人。"
    topics = _strings(row.get("topics"))
    if topic and topics and topic not in topics:
        return None, f"授权凭证 {row['id']} 只给 {'、'.join(topics)} 部署，不包括这个话题。"
    batch = _strings(row.get("commits"))
    outside = [commit for commit in commits if not any(_same_commit(commit, mine) for mine in batch)]
    if outside:
        return None, (f"任务说明里的提交 {'、'.join(outside)} 不在授权凭证 {row['id']} 的这一批里（{'、'.join(batch) or '没列提交'}）："
                      "只派主人同意的那几个，或用 project_ask 重新问主人。")
    return row, ""


def line(row) -> str:
    """The Bridge's head line of a handoff with approval ``row``: who said yes, when, to which commits and target, its id."""
    commits = "、".join(_strings(row.get("commits"))) or "（问题卡上没列提交）"
    target = str(row.get("target") or "").strip() or "（问题卡上没写目标）"
    return (f"主人 {stamp(row.get('approved_at'), '%m-%d %H:%M', '')} 在问题卡上同意部署这一批：提交 {commits}；目标 {target}"
            f"（凭证 {row.get('id')}）。")


def spoke_since(record, row) -> bool:
    """Whether the owner said something in the project after he said yes to ``row`` (``owner_at``, app/project_loop.py)."""
    return float(record.get("owner_at") or 0) > float(row.get("approved_at") or 0) + 1


def used(state_dir, project_id, approval_id, *, topic, job, request="", now=None) -> bool:
    """Consume ``approval_id``: ``used_by`` gets {topic, job, request, at} (``job`` the lead's turn, ``request`` its tool call)
    and ``state`` consumed, under the record's lock; False when it was no longer active by then (another hand-on took it)."""
    now = time.time() if now is None else now
    taken = []

    def change(record):
        row = find(record, approval_id)
        if row is None or state_of(row, now) != ACTIVE:
            return
        row["used_by"] = [*(item for item in row.get("used_by") or () if isinstance(item, dict)),
                          {"topic": str(topic or ""), "job": str(job or ""), "request": str(request or ""), "at": now}]
        row["state"] = CONSUMED
        taken.append(row["id"])
    projects.update(state_dir, project_id, change)
    return bool(taken)


def revoke(state_dir, project_id, approval_id, *, now=None) -> bool:
    """Revoke ``approval_id`` (``revoked_at``, ``state`` revoked) while it is active; whether it was."""
    now = time.time() if now is None else now
    taken = []

    def change(record):
        row = find(record, approval_id)
        if row is not None and state_of(row, now) == ACTIVE:
            row.update(revoked_at=now, state=REVOKED)
            taken.append(row["id"])
    projects.update(state_dir, project_id, change)
    return bool(taken)


def answer_line(state_dir, question) -> str:
    """What the lead's continuation reads of the approvals its owner's answer to ``question`` gave or took: '' for none."""
    found = project_of(state_dir, question.get("topic_id")) if isinstance(question, dict) else None
    if found is None:
        return ""
    token = str(question.get("token") or "")
    made = [row for row in rows(found[1]) if row.get("token") == token and state_of(row) == ACTIVE]
    lines = [f"[Bridge] 主人在问题卡上同意部署（凭证 {row['id']}，{TTL // 3600} 小时内有效、只能用一次）：派部署时 "
             f"project_topic_send 或 project_topic_start 带 approval=\"{row['id']}\"，任务说明只写这一批的提交"
             f"（{'、'.join(_strings(row.get('commits'))) or '问题卡上没列提交'}）。" for row in made]
    return "\n".join(lines)


def active_lines(record, now=None) -> list:
    """The pack's lines of the approvals still good: id, commits, target, until when."""
    now = time.time() if now is None else now
    return [f"- 部署授权 {row['id']}：提交 {'、'.join(_strings(row.get('commits'))) or '（没列）'}；目标 "
            f"{row.get('target') or '（没写）'}；{stamp(row.get('expires_at'), '%m-%d %H:%M', '')} 前有效，派部署时带 approval"
            for row in rows(record) if state_of(row, now) == ACTIVE]

