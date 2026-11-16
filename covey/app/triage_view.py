"""The questions held for a project's lead, as the lead reads them (app/triage.py is the rest of the question triage; it imports this,
and app/project_lead.py puts ``pack_block`` in the lead's 专项状态)."""
from __future__ import annotations

import time
from pathlib import Path

from covey.core import triage
from covey.core.store import directory_store
from covey.core.topics import topic_label


def held_questions(state_dir, topics=None) -> list:
    """[(path, record)] of the held questions (of the topics ``topics`` when given), oldest first."""
    rows = [(path, row) for path, row in directory_store(Path(state_dir) / "questions").query(status="pending") if triage.held(row)]
    rows = [row for row in rows if topics is None or row[1].get("topic_id") in topics]
    return sorted(rows, key=lambda row: row[1].get("created_at") or 0)


def lead_held(state_dir) -> list:
    """[(path, record)] of the questions whose triage is still ``held`` in any status (a lead's answer that left ``pending`` is one:
    the sweep settles it), oldest first."""
    rows = [(path, row) for path, row in directory_store(Path(state_dir) / "questions").query() if triage.state(row) == triage.HELD]
    return sorted(rows, key=lambda row: row[1].get("created_at") or 0)


def question_lines(record) -> list:
    """What the lead reads of a held question: each question's id, text and options."""
    lines = []
    for question in record.get("questions") or ():
        options = " / ".join(str(o.get("label")) for o in question.get("options") or () if isinstance(o, dict))
        lines.append(f"[{question.get('id')}] {question.get('question')}（{'可多选' if question.get('multiple') else '单选'}）选项：{options}")
    return lines


def pack_block(state_dir, project) -> str:
    """The lead's 状态 block of the questions held for it: empty when there are none (and without the switch)."""
    if not triage.enabled():
        return ""
    rows = held_questions(state_dir, set(project.get("threads") or ()))
    if not rows:
        return ""
    now, out = time.time(), []
    for _, row in rows:
        left = max(0, int(float(triage.of(row).get("deadline") or 0) - now))
        out.append(f"- {topic_label(Path(state_dir), row.get('topic_id')) or str(row.get('topic_id'))[:8]}（还剩 {left} 秒转给主人）\n"
                   + "\n".join("  " + line for line in question_lines(row)))
    return ("交给你的话题问题（能依据专项记忆和主人定过的事回答的，用 project_answer 替主人答；答不了用 project_escalate 转给主人）：\n"
            + "\n".join(out))
