"""The tools of a project's lead (docs/architecture.md 专项, core/main_tool_specs.py PROJECT_TOOLS), handlers as
app/main_env.py describes them; app/main_tools.py puts them with the main thread's. Only the job of a project's lead
(app/project_lead.py, COVEY_PROJECTS=1) may call them: ``lead`` finds the project from the job's topic and refuses any other.

- ``project_thread_start``, ``project_thread_send`` hand the work to the project's threads through the main thread's own
  functions (app/main_topics.py topic_start, topic_send: a call from a lead's job opens or feeds a thread, ``lead_of``).
  At most MAX_THREADS threads (or what the style's 派活 allows) have a turn running or waiting at once.
- ``project_note`` (a line for the project's card; ``report`` marks what the owner is to hear, filtered by the style),
  ``project_remember`` (the project's memory), ``project_style``, ``project_finish`` write the project's record
  (core/projects.py update, under its record lock; no Feishu call).
- ``project_answer`` and ``project_escalate`` are phase 2's question triage (P2-5): the tools are defined and answer that
  it is off.

A refusal is a ValueError, which app/main_tools.py MainTools.execute turns into an answer the lead reads."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.app import main_topics, project_lead
from covey.app.main_env import reply
from covey.core import main_thread, projects
from covey.core.jobs import find_job_record
from covey.core.topics import topic_label

KINDS = ("milestone", "thread", "note")
MEMORY_KINDS = ("decision", "practice", "pitfall", "fact")
TRIAGE_OFF = "分流还没开：负责人现在不能替主人答题，也不能转交问题；线程的问题会直接问主人。"


def lead(env, request) -> tuple:
    """(project id, its record) of the project whose lead's job made ``request``; ValueError when there is none: the
    switch is off, the job is not a lead's, or its record cannot be read."""
    if not projects.enabled():
        raise ValueError("专项没有开")
    state = Path(env.state_dir)
    job = find_job_record(state, request.job_key) or {}
    topic = str(job.get("topic_id") or "")
    if not topic or not main_thread.is_lead(topic, job.get("chat_id")):
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


def spawn_cap(record) -> int:
    return min(projects.MAX_THREADS, project_lead.SPAWN_LIMIT[project_lead.style_of(record)[project_lead.SPAWN]])


def project_thread_start(env, request):
    pid, record = lead(env, request)
    active(record, "派新线程")
    live = project_lead.live_threads(env.state_dir, record)
    if len(live) >= spawn_cap(record):
        return reply(False, f"同时最多 {spawn_cap(record)} 个线程在做（现在：{'、'.join(live)}）：等有线程结束再派。",
                     live=live)
    args = {**request.args, "user_requested": request.args.get("user_requested") is True}
    answer = main_topics.topic_start(env, dataclasses.replace(request, args=args))
    topic = answer.get("data", {}).get("topic_id")
    if topic:
        projects.add_thread(env.state_dir, pid, topic)  # Again for a request that ran before: once only.
    return answer if answer.get("ok") else {**answer, "text": "没有派出线程：" + answer["text"]}


def project_thread_send(env, request):
    pid, record = lead(env, request)
    active(record, "给线程加话")
    chat = env.chat()
    found = main_topics.find_topic(env, chat, request.args.get("thread"))
    if isinstance(found, str):
        return reply(False, found)
    topic = found[1].topic_id
    if topic not in (record.get("threads") or ()):
        return reply(False, f"{topic_label(Path(env.state_dir), topic) or topic[:8]} 不是这个专项的线程，不能从这里给它加话。")
    args = {"topic": topic, "text": request.args.get("text"), "note": request.args.get("note")}
    return main_topics.topic_send(env, dataclasses.replace(request, args=args))


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
        raise ValueError("kind 要是 " + "、".join(MEMORY_KINDS) + " 之一")
    item = project_lead.entry(text_of(request.args), kind=kind)
    same = next((row for row in project_lead.memory_of(record) if row.get("kind") == kind and row["text"] == item["text"]), None)
    if same is not None:
        return reply(True, f"专项记忆里已有这条（{same.get('id')}），没有重复记。", id=same.get("id"), new=False)

    def change(data):
        rows = data.get("memory")
        rows = data["memory"] = rows if isinstance(rows, list) else []
        rows.append(item)
        del rows[:-project_lead.MEMORY_KEPT]
    projects.update(env.state_dir, pid, change)
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
    line = project_lead.entry("汇报风格：" + "；".join(f"{name}={value}" for name, value in now.items()), kind="style")

    def change(data):
        data["style"] = now
        rows = [row for row in data.get("memory") or () if isinstance(row, dict) and row.get("kind") != "style"]
        data["memory"] = [*rows, line]  # One style line in the memory: the last word the owner gave.
    projects.update(env.state_dir, pid, change)
    return reply(True, "已改风格：" + "；".join(f"{name}={value}" for name, value in now.items()), style=now)


def project_finish(env, request):
    pid, record = lead(env, request)
    if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
        raise ValueError(f"专项现在是 {record.get('state')}，已经结束了")
    summary = text_of(request.args, "summary")
    live = project_lead.live_threads(env.state_dir, record)
    if live:
        return reply(False, f"还有线程在做或等主人（{'、'.join(live)}），不能收尾：等它们结束，或先处理掉。", live=live)
    item = project_lead.entry(summary, report=True, kind="finish", job=request.job_key)

    def change(data):
        data["state"], data["state_reason"], data["state_job"] = projects.DONE, summary, request.job_key
        rows = data.get("notes")
        rows = data["notes"] = rows if isinstance(rows, list) else []
        rows.append(item)
        del rows[:-project_lead.NOTES_KEPT]
    projects.update(env.state_dir, pid, change)
    return reply(True, f"专项已记为完成：{summary}", state=projects.DONE)


def project_answer(env, request):
    lead(env, request)
    return reply(False, TRIAGE_OFF)


def project_escalate(env, request):
    lead(env, request)
    return reply(False, TRIAGE_OFF)


TOOLS = {"project_thread_start": project_thread_start, "project_thread_send": project_thread_send,
         "project_note": project_note, "project_remember": project_remember, "project_style": project_style,
         "project_finish": project_finish, "project_answer": project_answer, "project_escalate": project_escalate}
