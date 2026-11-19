"""The tools of a project's lead (docs/architecture.md 专项, core/butler_tool_specs.py PROJECT_TOOLS), handlers as
app/butler_env.py describes them; app/butler_tools.py puts them with the butler's. Only the job of a project's lead
(app/project_lead.py, COVEY_PROJECTS=1) may call them: ``lead`` finds the project from the job's topic and refuses any other.

- ``project_topic_start``, ``project_topic_send`` hand the work to the project's member topics through the butler's own
  functions (app/butler_topics.py topic_start, topic_send: a call from a lead's job opens or feeds a member topic, ``lead_of``).
  A project has no limit on the member topics working at once (deploys that would clash queue instead).
- ``project_topic_stop`` stops one member topic for the owner (its tasks, and the question it waits on, which is cancelled
  with the reason ``lead_stop``); the topic stays in the project's list and counts as ended.
- ``project_note`` (a line for the project's card; ``report`` marks what the owner is to hear, filtered by the style),
  ``project_remember`` (the project's memory), ``project_style``, ``project_finish`` write the project's record
  (core/projects.py update, under its record lock; no Feishu call).
- ``project_answer`` and ``project_escalate`` are phase 2's question triage (P2-5, COVEY_PROJECT_TRIAGE=1; they are listed
  to the lead only then, and answer that it is off otherwise): the lead answers a member topic's held choice question as the
  owner would (the question's own submit path, with a note that the lead answered) or hands it to the owner with a reason
  (app/triage.py).

A tool that hands out, stops or answers (``acted``) marks the lead's turn as one that changed something, so the reply of
a wake-up turn that did is sent to the owner (app/project_loop.py reported; 2026-11-19, the owner: 有变化才发).

A refusal is a ValueError, which app/butler_tools.py ButlerTools.execute turns into an answer the lead reads."""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.app import butler_topics, project_lead, triage as triage_flow
from covey.app.butler_env import reply
from covey.core import butler, project_memory, projects, triage
from covey.core import project_proposals as proposals
from covey.core.jobs import find_job_record
from covey.core.topics import topic_label

KINDS = ("milestone", "thread", "note")
MEMORY_KINDS = (*project_memory.KINDS[:-1], *project_memory.LEGACY_KINDS)  # style is project_style's; practice and pitfall are the older names of constraint.
OFFERED_KINDS = project_memory.KINDS[:-1]
ACTED_KEPT = 40  # The record's ``acted`` rows kept: one a lead turn that handed out, stopped or answered.
TRIAGE_OFF = "问题分流没有开：负责人不能替主人答题，也不能转交问题；话题里的问题会直接问主人。"


def lead(env, request) -> tuple:
    """(project id, its record) of the project whose lead's job made ``request``; ValueError when there is none: the
    switch is off, the job is not a lead's, or its record cannot be read."""
    if not projects.enabled():
        raise ValueError("专项没有开")
    state = Path(env.state_dir)
    job = find_job_record(state, request.job_key) or {}
    topic = str(job.get("topic_id") or "")
    if not topic or not butler.is_lead(topic, job.get("chat_id")):
        raise ValueError("只有专项的负责人能用这个工具")
    found = projects.topic_project(state, topic)
    record = projects.read_record(state, found.id) if found is not None else {}
    if not record:
        raise ValueError("读不到这个专项的记录")
    return found.id, record


def active(record, what="这件事") -> None:
    if record.get("state") != projects.ACTIVE:
        raise ValueError(f"专项现在是 {record.get('state')}，不能{what}")


def text_of(args, name="text", limit=project_lead.TEXT) -> str:
    text = " ".join(str(args.get(name) or "").split())
    if not text:
        raise ValueError(f"{name} 不能为空")
    return text[:limit]


def acted(env, pid, request, what) -> None:
    """Mark the lead's turn ``request.job_key`` as one that changed something the owner is to hear of (``what``: start,
    send, stop, answer, escalate), once a turn: app/project_loop.py ``reported`` sends a wake-up turn's reply then."""
    key = str(getattr(request, "job_key", "") or "")
    if not key:
        return

    def change(data):
        rows = data.get("acted")
        rows = data["acted"] = rows if isinstance(rows, list) else []
        if not any(isinstance(row, dict) and row.get("job") == key for row in rows):
            rows.append({"job": key, "what": what, "at": time.time()})
        del rows[:-ACTED_KEPT]
    projects.update(env.state_dir, pid, change)


def project_topic_start(env, request):
    pid, record = lead(env, request)
    active(record, "开新话题")
    args = {**request.args, "user_requested": request.args.get("user_requested") is True}
    answer = butler_topics.topic_start(env, dataclasses.replace(request, args=args))
    topic = answer.get("data", {}).get("topic_id")
    if topic:
        projects.add_member(env.state_dir, pid, topic)  # Again for a request that ran before: once only.
        acted(env, pid, request, "start")
    return answer if answer.get("ok") else {**answer, "text": "没有开出话题：" + answer["text"]}


def project_topic_send(env, request):
    pid, record = lead(env, request)
    active(record, "给话题加话")
    chat = env.chat()
    found = butler_topics.find_topic(env, chat, request.args.get("thread"))
    if isinstance(found, str):
        return reply(False, found)
    topic = found[1].topic_id
    if topic not in (record.get("threads") or ()):
        refused = take_in(env, chat, pid, topic, request.args.get("text"))
        if refused:
            return reply(False, refused)
    args = {"topic": topic, "text": request.args.get("text"), "note": request.args.get("note")}
    answer = butler_topics.topic_send(env, dataclasses.replace(request, args=args))
    if answer.get("ok"):
        acted(env, pid, request, "send")
    return answer


def take_in(env, chat, pid, topic, text) -> str:
    """An ordinary topic the lead gives work to joins the project first (the owner, 2026-11-19: 加话时顺带收进专项): app/projects.py
    join_topic, its running turn and the question it waits on left as they are. '' when it joined; else why it cannot (a lead's,
    another project's, the butler's, one the owner took out of a project)."""
    from covey.app.projects import join_topic, wanted_topics

    state = Path(env.state_dir)
    label = topic_label(state, topic) or topic[:8]
    joins, refused = wanted_topics(env, chat, [topic])
    if refused or joins != [topic]:
        why = refused[0]["why"] if refused else "不是主人私聊里的普通话题"
        return f"{label} {why}，不能从这里给它加话。"
    if proposals.never_auto(state, topic):
        return f"{label} 被主人移出过专项，不能从这里收进来；要它做事，请主人自己决定。"
    try:
        join_topic(state, pid, topic, why=f"负责人给它加话：{' '.join(str(text or '').split())[:60]}", by="lead")
    except (ValueError, KeyError) as exc:
        return f"{label} 收不进这个专项：{exc}。"
    tidy = getattr(env, "tidy", None)
    if tidy is not None:
        tidy.show_joined(topic, pid)
    return ""


STOP_NOTE = "负责人按主人的意思停了这个话题"


def project_topic_stop(env, request):
    """Stop one member topic for the owner: its queued and running tasks (app/project_cards.py ``stop_tasks``, forwarded to the
    task runner as a stop on the card is), then the question it waits on and its waiting turn, as a switch of configuration voids
    them (app/binding.py ``retire_open_questions``, here ``cancelled`` with the reason ``lead_stop``). The topic stays in
    the project's list and counts as ended. No lock is held across a call."""
    from covey.app import binding, project_cards

    pid, record = lead(env, request)
    if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
        raise ValueError(f"专项现在是 {record.get('state')}，已经结束了")
    chat = env.chat()
    found = butler_topics.find_topic(env, chat, request.args.get("thread"))
    if isinstance(found, str):
        return reply(False, found)
    app, ctx, _ = found
    topic = ctx.topic_id
    label = topic_label(Path(env.state_dir), topic) or topic[:8]
    if topic not in (record.get("threads") or ()):
        return reply(False, f"{label} 不是这个专项的话题，不能从这里停它。")
    stopped = project_cards.stop_tasks(app.controls, ctx, [topic], chat, f"lead-stop-{request.id}", by="lead")
    why = " ".join(str(request.args.get("why") or "").split())[:120]
    binding.retire_open_questions(env.topics, app, ctx, chat, reason="lead_stop", job_status="cancelled",
                                  message=STOP_NOTE + (f"：{why}" if why else ""))
    acted(env, pid, request, "stop")
    return reply(True, f"已停掉 {label}：请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停），等主人回答的问题已收掉；"
                 "它仍记在专项里、算结束。要接着做，用 project_topic_send。", stopped=stopped, topic_id=topic)


def project_note(env, request):
    pid, record = lead(env, request)
    args = request.args
    kind = args.get("kind") if args.get("kind") in KINDS else "note"
    report, decision = args.get("report") is True, args.get("decision") is True
    item = project_lead.entry(text_of(args), report=report, kind=kind, job=request.job_key)

    def change(data):
        for field, row, kept in (("notes", item, project_lead.NOTES_KEPT),
                                 *((("decisions", {**item, "report": False}, project_lead.DECISIONS_KEPT),) if decision else ())):
            rows = data.get(field)
            rows = data[field] = rows if isinstance(rows, list) else []
            rows.append(row)
            del rows[:-kept]
    projects.update(env.state_dir, pid, change)
    shown = ("，会按风格告诉主人" if project_lead.reportable(record.get("style"), kind) else "，按风格不会打扰主人") if report else ""
    return reply(True, f"已记在专项卡上{shown}" + ("；也记进了决定日志" if decision else "") + "。", id=item["id"], report=report)


def project_remember(env, request):
    pid, record = lead(env, request)
    kind = request.args.get("kind")
    if kind not in MEMORY_KINDS:
        raise ValueError("kind 要是 " + "、".join(OFFERED_KINDS) + " 之一")
    text, kind = text_of(request.args), project_memory.kind_of(kind)
    state = Path(env.state_dir)
    if any(row["kind"] == kind and row["text"] == text for row in project_memory.tombstones(state, record)):
        raise ValueError("主人删掉过这条专项记忆，不再记回去。")
    item, new = project_memory.add(state, pid, text, kind, "lead")
    if not new:
        return reply(True, f"专项记忆里已有这条（{item['id']}），没有重复记。", id=item["id"], new=False)
    return reply(True, f"已记进专项记忆（{item['id']}）：{item['text']}", id=item["id"], new=True)


def project_style(env, request):
    pid, record = lead(env, request)
    changes = {name: request.args[name] for name in project_lead.STYLE_CHOICES if name in request.args}
    for name, value in changes.items():
        if value not in project_lead.STYLE_CHOICES[name]:
            raise ValueError(f"{name} 要是 " + "、".join(project_lead.STYLE_CHOICES[name]) + " 之一")
    now = {**project_lead.style_of(record), **changes}
    if not changes:
        return reply(True, "现在的风格：" + "；".join(f"{name}={value}" for name, value in now.items()), style=now)
    text = "汇报风格：" + "；".join(f"{name}={value}" for name, value in now.items())
    # One style line in the memory (the last word the owner gave), written with the record's style field.
    project_memory.set_style(env.state_dir, pid, text, also=lambda data: data.__setitem__("style", now))
    if projects.style_keep_enabled():
        project_lead.keep_style(env.state_dir, now)  # The next project starts from it.
    return reply(True, "已改风格：" + "；".join(f"{name}={value}" for name, value in now.items()), style=now)


def project_finish(env, request):
    pid, record = lead(env, request)
    if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
        raise ValueError(f"专项现在是 {record.get('state')}，已经结束了")
    summary = text_of(request.args, "summary")
    live = project_lead.live_members(env.state_dir, record)
    if live:
        return reply(False, f"还有话题在做或等主人（{'、'.join(live)}），不能收尾：等它们结束，或先处理掉。", live=live)
    item = project_lead.entry(summary, report=True, kind="finish", job=request.job_key)

    def change(data):
        data["state"], data["state_reason"], data["state_job"] = projects.DONE, summary, request.job_key
        rows = data.get("notes")
        rows = data["notes"] = rows if isinstance(rows, list) else []
        rows.append(item)
        del rows[:-project_lead.NOTES_KEPT]
    projects.update(env.state_dir, pid, change)
    return reply(True, f"专项已记为完成：{summary}", state=projects.DONE)


def held_topic(env, request, record):
    """(app, context, chat) of the member topic ``thread`` names; ValueError saying why not."""
    chat = env.chat()
    found = butler_topics.find_topic(env, chat, request.args.get("thread"))
    if isinstance(found, str):
        raise ValueError(found)
    app, ctx, _ = found
    if ctx.topic_id not in (record.get("threads") or ()):
        raise ValueError(f"{topic_label(Path(env.state_dir), ctx.topic_id) or ctx.topic_id[:8]} 不是这个专项的话题。")
    return app, ctx, chat


def project_answer(env, request):
    """Answer the member topic's held choice question for the owner (app/triage.py answer)."""
    pid, record = lead(env, request)
    if not triage.enabled():
        return reply(False, TRIAGE_OFF)
    active(record, "替主人答题")
    try:
        app, ctx, chat = held_topic(env, request, record)
        done = triage_flow.answer(env, app, ctx, chat, record, request.args.get("answers"), f"lead-{request.id}")
    except ValueError as exc:
        return reply(False, str(exc))
    acted(env, pid, request, "answer")
    if done["status"] == "uncertain":
        return reply(False, f"{done['label']} 的回答已保存，但送达状态未确认：先用 topic_read 核对，别再答一次。", status="uncertain")
    if done["status"] == "pending":
        return reply(True, f"已把回答交给 {done['label']} 的任务进程：{done['line']}。它还没确认收到；"
                     f"{triage.ANSWERING} 秒内没送到，问题会转给主人（专项卡的分流记录会写）。先别再答一次。", status="pending")
    return reply(True, f"已替主人答了 {done['line']}（话题照常往下做，记在专项卡的分流记录里，主人可以点“不对”纠正）。", status=done["status"])


def project_escalate(env, request):
    """Hand the member topic's held question to the owner now, with ``why`` on the card (app/triage.py escalate)."""
    pid, record = lead(env, request)
    if not triage.enabled():
        return reply(False, TRIAGE_OFF)
    why = text_of(request.args, "why", limit=120)
    try:
        app, ctx, chat = held_topic(env, request, record)
    except ValueError as exc:
        return reply(False, str(exc))
    rows = triage_flow.held_questions(env.state_dir, {ctx.topic_id})
    if not rows:
        return reply(False, "这个话题现在没有交给你的问题（可能已经转给主人，或超过 3 分钟了）。")
    sent = [triage_flow.escalate(env, row, why, by="lead") for _, row in rows]
    acted(env, pid, request, "escalate")
    label = topic_label(Path(env.state_dir), ctx.topic_id) or ctx.topic_id[:8]
    return reply(True, f"已转给主人：{label} 的问题卡" + ("已发出。" if all(sent) else "还没发出去，系统会请主人重新投递。"), sent=all(sent))


TOOLS = {"project_topic_start": project_topic_start, "project_topic_send": project_topic_send,
         "project_topic_stop": project_topic_stop,
         "project_note": project_note, "project_remember": project_remember, "project_style": project_style,
         "project_finish": project_finish, "project_answer": project_answer, "project_escalate": project_escalate}
