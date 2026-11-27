"""The tools of a project's lead (docs/architecture.md 专项, core/butler_tool_specs.py PROJECT_TOOLS), handlers as
app/butler_env.py describes them; app/butler_tools.py puts them with the butler's. Only the job of a project's lead
(app/project_lead.py, COVEY_PROJECTS=1) may call them: ``lead`` finds the project from the job's topic and refuses any other.

- ``project_topic_start``, ``project_topic_send`` hand the work to the project's member topics through the butler's own
  functions (app/butler_topics.py topic_start, topic_send: a call from a lead's job opens or feeds a member topic, ``lead_of``).
  A project has no limit on the member topics working at once (deploys that would clash queue instead). Work for a topic
  whose model's gateway quota is spent until its reset is not sent: the lead hears when it is back and to switch first
  (``spent_quota``, core/quota_hold.py).
- ``project_topic_stop`` stops one member topic for the owner (its tasks, and the question it waits on, which is cancelled
  with the reason ``lead_stop``); the topic stays in the project's list and counts as ended.
- ``project_note`` (a line for the project's card; ``report`` marks what the owner is to hear, filtered by the style),
  ``project_goal`` (the goal and its done-when as the lead keeps them, ``lead_goal``: the web page shows it under the project's name),
  ``project_remember`` (the project's memory: ``kind`` rule or note with a ``title`` of at most 40 characters goes to
  core/memstore.py add, a rule the lead sets also to 待审核 for the owner; the older kinds as before; a line that is a snapshot,
  ``SNAPSHOT``, is refused: it goes to ``project_note``), ``project_memory_revise`` (the lead updates, replaces or puts away a
  line of it, by its id or short code, ``revise_refusal`` says whose lines it may not touch; a decision of decisions.jsonl
  through core/memstore.py), ``project_style``, ``project_finish`` write the project's record
  (core/projects.py update, under its record lock; no Feishu call). ``project_finish`` refuses a project 整理专项 made that the
  owner has not spoken in and the lead has handed no work to (``finish_gate``).
- ``project_answer`` and ``project_escalate`` are phase 2's question triage (P2-5, COVEY_PROJECT_TRIAGE=1; they are listed
  to the lead only then, and answer that it is off otherwise): the lead answers a member topic's held choice question as the
  owner would (the question's own submit path, with a note that the lead answered) or hands it to the owner with a reason
  (app/triage.py). With 协作 on (COVEY_COLLAB=1), or when it names them, an answer stands on ``evidence`` the Bridge checks
  (app/triage.py grounds): no ground of the owner's that still holds, no answer.
- With 协作 on, ``project_checklist`` keeps the project's 目标检查表 (core/project_checklist.py), and ``project_finish``
  refuses while an item of it is not met or a 协作 gap is not dealt with (``checklist_gate``). Also with it on, the 简报
  (docs/projects.md 项目简报): ``project_goal``'s ``background``, ``project_checklist``'s ``actions`` (core/project_actions.py),
  and the optional ``action`` and ``item`` of ``project_topic_start`` and ``project_topic_send``, which hang the topic on them
  (``place_of`` looks at the ids before anything is done, ``hang`` writes them once the topic is a member).

A tool that hands out, stops or answers (``acted``) marks the lead's turn as one that changed something, so the reply of
a wake-up turn that did is sent to the owner (app/project_loop.py reported: 有变化才发).

A refusal is a ValueError, which app/butler_tools.py ButlerTools.execute turns into an answer the lead reads."""
from __future__ import annotations

import dataclasses
import re
import time
from pathlib import Path

from covey.app import butler_topics, project_lead, triage as triage_flow
from covey.app.butler_handoff import choice_error
from covey.app.butler_env import reply
from covey.core import butler, memory, memstore, project_actions, project_memory, projects, quota_hold, triage, turn_source
from covey.core import project_checklist as checklist
from covey.core import project_proposals as proposals
from covey.core.jobs import find_job_record
from covey.core.text import cut
from covey.core.topics import topic_label

KINDS = ("milestone", "thread", "note")
NEW_KINDS = ("rule", "note")  # core/memstore.py's: a 规矩 and a 资料 of the project (``project_remember`` with a ``title``).
# Every kind project_remember takes: the two new ones, then the older ones, kept as they were (style is project_style's;
# practice and pitfall are the older names of constraint). app/butler_memory.py passes rule and note on only when they are here.
MEMORY_KINDS = (*NEW_KINDS, *project_memory.KINDS[:-1], *project_memory.LEGACY_KINDS)
OFFERED_KINDS = NEW_KINDS
# A line that holds for a moment, not for the project: a release number or generation in production, the cluster's room now, how
# far something is. It goes stale at once and sits in every turn after (T852), so it is a ``project_note``, not a memory.
SNAPSHOT = re.compile(r"现网第\s*\d+\s*代|第\s*\d+\s*代(?:部署|发版|发布|版本)|余量\s*[约≈]?\s*\d+\s*卡|剩余\s*\d+\s*卡|"
                      r"(?:当前|目前|现在)(?:进度|已完成|完成)\s*\d+\s*[%/]|进度\s*\d+\s*%")
SNAPSHOT_REFUSED = ("这是某一刻的快照（发版号、现网第几代、集群余量、当前进度这类），过后就过时，不要记进专项记忆：写 project_note"
                    "（专项卡上的进展），记忆里只放长期成立的决定、约束、联系人和事实。")
REVISE_ACTIONS = ("update", "supersede", "retire")
ACTED_KEPT = 40  # The record's ``acted`` rows kept: one a lead turn that handed out, stopped or answered.
TRIAGE_OFF = "问题分流没有开：负责人不能替主人答题，也不能转交问题；话题里的问题会直接问主人。"
CHECKLIST_OFF = "协作没有开（COVEY_COLLAB）：没有目标检查表，收尾照原来的规则。"
BRIEF_OFF = "协作没有开（COVEY_COLLAB）：没有简报，不能带 background、action、item。"
FINISH_NEXT = ("现在不能收尾。先安排补齐：project_topic_send 让原话题返工，或 project_topic_start 开新话题；核对过证据再用 project_checklist "
               "把那一条标 done（写 evidence）；主人说不要了的标 dropped，在 gap 里写主人怎么说的；协作缺口处理完用 close_gaps 写怎么处理的。"
               "要主人定范围或取舍的，合成一个问题用 project_note 标 report 问他。")


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


STATE_NAMES = {projects.PAUSED: "停住", projects.DONE: "做完（已收尾）", projects.CANCELLED: "已取消"}
DONE_NEXT = ("主人在专项里说一句话（负责人话题、专项的话题、管家转来、网页上）专项就会自动重开；"
             "告诉主人这一点和你打算怎么做，不要说做不了。")
OWNER_FIRST = ("这个专项是整理专项时把已有的话题收进来建的，建好后主人还没在里面说过话、你也还没派过活："
               "收进来的话题做完不等于专项完成，现在不能收尾。用 project_note（标 report）说清每个话题做到哪、还剩什么，"
               "然后结束这一轮，等主人下一步；主人在专项里说过话、或你派过活之后才能 project_finish。")


def ended_words(record, what) -> str:
    """Why a lead's tool refuses ``what`` in a project that is not active, and what comes next."""
    state = record.get("state")
    words = f"专项现在是{STATE_NAMES.get(state, state)}，不能{what}。"
    if state == projects.DONE:
        return words + DONE_NEXT
    if state == projects.PAUSED:
        return words + "主人在专项里说话或在停住卡上点继续，专项就会重新开始。"
    return words


def active(record, what="这件事") -> None:
    if record.get("state") != projects.ACTIVE:
        raise ValueError(ended_words(record, what))


def finish_gate(record) -> str:
    """'' when the lead may finish ``record``; else why not (``OWNER_FIRST``): a project 整理专项 made of topics that were there
    already (origin ``tidy``), which the owner has not spoken in since (``owner_at``, app/project_loop.py owner_spoke) and
    its lead has handed no work to (``acted``). Its topics all done is how it began, not its goal reached (else a
    project split off would be finished by its lead's first wake-up turn). A project the owner started (direct, an
    upgrade) began with his words."""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind") != "tidy" or record.get("owner_at") or record.get("acted"):
        return ""
    return OWNER_FIRST


def checklist_gate(state_dir, record) -> tuple:
    """('' , [], []) when the 目标检查表 lets the lead finish ``record`` (协作 off, COVEY_COLLAB: always); else (why not, the
    ids of the items not met, the ids of the 协作 gaps not dealt with), each named: an item open or blocked (only one the owner
    let go is met without its evidence, core/project_checklist.py MET), a ``collab_gap`` receipt the lead has not closed, read
    or not. A project whose lead keeps no checklist is held by its open gaps alone."""
    if not turn_source.enabled():
        return "", [], []
    items, gaps = checklist.unmet(record, project_lead.inbox_items(state_dir, record["id"]))
    if not items and not gaps:
        return "", [], []
    parts = []
    if items:
        parts.append("目标检查表还有没满足的：" + "；".join(
            f"{item['id']}「{cut(item['criterion'], 40)}」{checklist.STATE_WORDS[item['state']]}"
            + (f"（缺口：{cut(item['gap'], 40)}）" if item["gap"] else "") for item in items))
    if gaps:
        parts.append("协作里还有没处理的缺口：" + "；".join(f"{gap['id']}「{cut(gap['text'], 60)}」" for gap in gaps))
    return "，".join(parts) + "。" + FINISH_NEXT, [item["id"] for item in items], [gap["id"] for gap in gaps]


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


def place_of(record, args) -> tuple:
    """(action id, item id) a topic's ``action`` and ``item`` name, each '' when not given; ValueError when the ids are not the
    record's (``project_actions.check``), or 协作 is off (then there is no 简报: the call is refused, as nothing has been done)."""
    action, item = (" ".join(str(args.get(name) or "").split()) for name in ("action", "item"))
    if not (action or item):
        return "", ""
    if not turn_source.enabled():
        raise ValueError(BRIEF_OFF)
    project_actions.check(record, action, item)
    return action, item


def hang(env, pid, topic, request, place) -> str:
    """The words to add to the answer once ``topic`` is a member: it is put on the action and the item ``place`` (``place_of``) in
    the record, under its lock. '' when ``place`` names none."""
    action, item = place
    if not (action or item):
        return ""
    found = []

    def change(data):
        found[:] = project_actions.attach(data, topic, action, item, job=str(request.job_key or ""))
    projects.update(env.state_dir, pid, change)
    done, full = found
    return (f" 已挂到 {'、'.join(done)}。" if done else "") + (f" 没挂上：{'；'.join(full)}。" if full else "")


def project_topic_start(env, request):
    pid, record = lead(env, request)
    active(record, "开新话题")
    place = place_of(record, request.args)
    args = {**{key: value for key, value in request.args.items() if key not in ("action", "item")},
            "user_requested": request.args.get("user_requested") is True}
    answer = butler_topics.topic_start(env, dataclasses.replace(request, args=args))
    topic = answer.get("data", {}).get("topic_id")
    if topic:
        projects.add_member(env.state_dir, pid, topic)  # Again for a request that ran before: once only.
        answer = {**answer, "text": answer["text"] + hang(env, pid, topic, request, place)
                  + files_left(env, request, "用 project_topic_send 带上 attachments 补给它")}
        acted(env, pid, request, "start")
    return answer if answer.get("ok") else {**answer, "text": "没有开出话题：" + answer["text"]}


def project_topic_send(env, request):
    pid, record = lead(env, request)
    active(record, "给话题加话")
    place = place_of(record, request.args)
    chat = env.chat()
    found = butler_topics.find_topic(env, chat, request.args.get("thread"))
    if isinstance(found, str):
        return reply(False, found)
    topic = found[1].topic_id
    spent = spent_quota(env, topic, found[2])
    if spent:
        return reply(False, spent)
    wrong = choice_error(request.args.get("attachments"))
    if wrong:  # Before an ordinary topic is taken in for nothing.
        return reply(False, wrong)
    if topic not in (record.get("threads") or ()):
        refused = take_in(env, chat, pid, topic, request.args.get("text"))
        if refused:
            return reply(False, refused)
    args = {"topic": topic, "text": request.args.get("text"), "note": request.args.get("note"),
            "attachments": request.args.get("attachments")}
    answer = butler_topics.topic_send(env, dataclasses.replace(request, args=args))
    if answer.get("ok"):
        answer = {**answer, "text": answer["text"] + hang(env, pid, topic, request, place)
                  + files_left(env, request, "带上 attachments 再给它加一次话")}
        acted(env, pid, request, "send")
    return answer


def files_left(env, request, how) -> str:
    """What the lead is told when its call named no ``attachments`` and its turn got the owner's files (his message, his
    supplements): none went to the topic, and ``how`` it gives them; '' otherwise (T856: they go only where it names them)."""
    if request.args.get("attachments") is not None:
        return ""
    from covey.app.butler_handoff import asked, summary

    found = asked(env.state_dir, request.job_key).attachments
    return f" 主人这一轮的附件（{summary(found)}）没有转给它；要它看，{how}（如 [\"图片 1\"]）。" if found else ""


def spent_quota(env, topic, data) -> str:
    """Why work is not sent to ``topic`` (its topic.json ``data``) now: the gateway quota its model draws on is spent
    until its reset (core/quota_hold.py held), so its turn would only fail; '' when it is not."""
    state = Path(env.state_dir)
    found = quota_hold.held(state, data.get("harness_id"), data.get("model"))
    if found is None:
        return ""
    gateway, hold = found
    return quota_hold.send_words(gateway, hold, topic_label(state, topic) or topic[:8], data.get("model"))


def take_in(env, chat, pid, topic, text) -> str:
    """An ordinary topic the lead gives work to joins the project first: app/project_members.py
    join_topic, its running turn and the question it waits on left as they are. '' when it joined; else why it cannot (a lead's,
    another project's, the butler's, one the owner took out of a project)."""
    from covey.app.project_members import join_topic, wanted_topics

    state = Path(env.state_dir)
    label = topic_label(state, topic) or topic[:8]
    joins, refused = wanted_topics(env, chat, [topic])
    if refused or joins != [topic]:
        why = refused[0]["why"] if refused else "不是主人私聊里的普通话题"
        return f"{label} {why}，不能从这里给它加话。"
    if proposals.never_auto(state, topic):
        return f"{label} 被主人移出过专项，不能从这里收进来；要它做事，请主人自己决定。"
    try:
        join_topic(state, pid, topic, why=f"负责人给它加话：{' '.join(str(text or '').split())[:60]}", by="lead", log=env.runtime.log)
    except (ValueError, KeyError) as exc:
        return f"{label} 收不进这个专项：{exc}。"
    tidy = getattr(env, "tidy", None)
    if tidy is not None:
        tidy.show_joined(topic, pid)
    return ""


STOP_NOTE = "负责人按主人的意思停了这个话题"


def stop_topic(env, app, ctx, chat, request, *, by, reason, note) -> tuple:
    """(tasks asked to stop, launch cards closed) of stopping the topic of ``ctx`` for the owner: its queued and running tasks
    (app/project_clicks.py ``stop_tasks``, forwarded to the task runner as a stop on the card is, ``by`` who stopped them:
    core/jobs.py STOP_SOURCES), then the question it waits on and its waiting turn, as a switch of configuration voids them
    (app/binding.py ``retire_open_questions``, here ``cancelled`` for ``reason`` with ``note`` and the call's ``why``), then
    its launch cards not started yet. A project's lead (``project_topic_stop``) and the butler (app/butler_tools.py
    topic_stop) stop a topic so. No lock is held across a call."""
    from covey.app import binding, project_clicks
    from covey.app.launch import close_open_launches

    topic = ctx.topic_id
    stopped = project_clicks.stop_tasks(app.controls, ctx, [topic], chat, f"{reason.replace('_', '-')}-{request.id}", by=by)
    why = " ".join(str(request.args.get("why") or "").split())[:120]
    binding.retire_open_questions(env.topics, app, ctx, chat, reason=reason, job_status="cancelled", by=by,
                                  message=note + (f"：{why}" if why else ""))
    return stopped, close_open_launches(ctx, chat, getattr(env, "messenger", None), note)


def project_topic_stop(env, request):
    """Stop one member topic for the owner (``stop_topic``, the reason ``lead_stop``). The topic stays in the project's list
    and counts as ended."""
    pid, record = lead(env, request)
    if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
        raise ValueError(ended_words(record, "停话题"))
    chat = env.chat()
    found = butler_topics.find_topic(env, chat, request.args.get("thread"))
    if isinstance(found, str):
        return reply(False, found)
    app, ctx, _ = found
    topic = ctx.topic_id
    label = topic_label(Path(env.state_dir), topic) or topic[:8]
    if topic not in (record.get("threads") or ()):
        return reply(False, f"{label} 不是这个专项的话题，不能从这里停它。")
    stopped, launches = stop_topic(env, app, ctx, chat, request, by="lead", reason="lead_stop", note=STOP_NOTE)
    acted(env, pid, request, "stop")
    cards = f"，没开工的启动卡收掉 {launches} 张" if launches else ""
    return reply(True, f"已停掉 {label}：请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停），等主人回答的问题已收掉{cards}；"
                 "它仍记在专项里、算结束。要接着做，用 project_topic_send。", stopped=stopped, launches=launches, topic_id=topic)


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


def project_goal(env, request):
    """The project's goal as the lead keeps it: the record's ``lead_goal`` {text, at, job}. The record's ``goal`` stays what
    the project was opened with (the owner's words, or the work it took over), which reads as no goal. With 协作 on also its
    background, ``lead_background`` {text, at, job} (``background``): one of the two at least."""
    pid, _ = lead(env, request)
    background = " ".join(str(request.args.get("background") or "").split())[:project_actions.BACKGROUND]
    if background and not turn_source.enabled():
        raise ValueError(BRIEF_OFF)
    if turn_source.enabled() and not background and not str(request.args.get("text") or "").strip():
        raise ValueError("text（目标）和 background（背景）至少给一个")
    text = text_of(request.args) if not background or str(request.args.get("text") or "").strip() else ""
    stamp = {"at": time.time(), "job": str(request.job_key or "")}

    def change(data):
        if text:
            data["lead_goal"] = {"text": text, **stamp}
        if background:
            data["lead_background"] = {"text": background, **stamp}
    projects.update(env.state_dir, pid, change)
    return reply(True, "；".join(([f"已记下专项的目标：{text}"] if text else []) + ([f"已记下专项的背景：{background}"] if background else [])))


def project_remember(env, request):
    pid, record = lead(env, request)
    kind = request.args.get("kind")
    if kind not in MEMORY_KINDS:
        raise ValueError("kind 要是 " + "、".join(OFFERED_KINDS) + " 之一（rule 规矩：以后照做的；note 资料：供参考的事实）")
    text = text_of(request.args)
    state = Path(env.state_dir)
    legacy = project_memory.kind_of(kind) if kind not in NEW_KINDS else ("constraint" if kind == "rule" else "fact")
    if any(row["kind"] == legacy and row["text"] == text for row in project_memory.tombstones(state, record)):
        raise ValueError("主人删掉过这条专项记忆，不再记回去。")
    if SNAPSHOT.search(text):
        raise ValueError(SNAPSHOT_REFUSED)
    if kind in NEW_KINDS:
        return remember_new(state, pid, kind, text, request)
    item, new = project_memory.add(state, pid, text, legacy, "lead", origin={"job": str(request.job_key or "")})
    if not new:
        return reply(True, f"专项记忆里已有这条（{item['id']}），没有重复记。", id=item["id"], new=False)
    return reply(True, f"已记进专项记忆（{item['id']}）：{item['text']}", id=item["id"], new=True)


def remember_new(state, pid, kind, text, request):
    """project_remember of a rule or a note (core/memstore.py add): the lead's, 它总结的; a rule also goes to 待审核 for the
    owner to make it his (core/memstore.py review_put, type lead). ValueError for what memstore refuses (a title too long)."""
    title = " ".join(str(request.args.get("title") or "").split())
    if len(title) > memstore.TITLE_CHARS:
        raise ValueError(f"title 最多 {memstore.TITLE_CHARS} 个字，这次 {len(title)} 个：写一句短的标题。")
    before = {entry.ref for entry in memstore.entries(state, scope="project", owner_key=pid, statuses=None)}
    entry = memstore.add(state, kind=kind, scope="project", owner_key=pid, text=text, title=title, said="summary", by="lead",
                         origin={"job": str(request.job_key or "")}, who="lead")
    new = entry.ref not in before
    if new and kind == "rule":
        try:
            memstore.review_put(state, {"type": "lead", "title": entry.title, "text": entry.text, "items": [entry.ref],
                                        "suggest": {"kind": "rule", "scope": "project", "project_key": pid},
                                        "origin": {"job": str(request.job_key or "")}})
        except Exception:  # noqa: BLE001 - the rule is kept, 它总结的, whether or not it is offered to the owner
            pass
    words = "规矩" if kind == "rule" else "资料"
    if not new:
        return reply(True, f"专项记忆里已有这条（{entry.code}），没有重复记。", id=entry.code, ref=entry.ref, new=False)
    told = "，主人在待审核里确认后算他定的" if kind == "rule" else ""
    return reply(True, f"已记进专项{words}（{entry.code}）{entry.title}：{entry.text}{told}。", id=entry.code, ref=entry.ref, new=True)


def revise_refusal(action, entry, other=None) -> str:
    """Why the lead may not ``action`` (update, supersede or retire) the line ``entry`` (a row with its meta), '' when it may. The
    owner's 长期约定 and his explicit limits are his alone; his 当前决定 (an answer he gave) keeps its words, is put away when it
    is done, and is replaced only by a later line of the owner's (``other``, what takes its place); the lead's own are the lead's."""
    facts = memory.describe(entry)
    if facts["asserted_by"] != turn_source.PARTY_OWNER:
        return ""
    if facts["layer"] == memory.RULE or entry["kind"] == "constraint":
        return "这是主人的长期约定（或他明确留下的限制），负责人不能改、替代或作废；要改请问主人，由主人自己在专项卡上改。"
    if action == "retire":
        return ""
    if action == "update":
        return "这是主人的当前决定，不能改正文；办完了用 retire 收进历史，主人有了新的答复就用 supersede（by 写那条新答复的 id）。"
    newer = other is not None and memory.describe(other)["asserted_by"] == turn_source.PARTY_OWNER and other.get("at", 0) > entry.get("at", 0)
    return "" if newer else "这是主人的当前决定，只能被主人更新的答复替代：by 写那条新答复的 id；负责人自己写的新条不行，请问主人。"


def memory_key(state, pid, given) -> tuple:
    """(the line's id in the project's memory.json, the entry when it lives in decisions.jsonl) of what the lead named: an id,
    a short code (core/memstore.py CODE_RE) or a ref; ValueError for one of another project or of no entry."""
    given = str(given or "").strip().strip("〔〕[]【】 ")
    if not given or any(row["id"] == given for row in project_memory.stored(state, pid)):
        return given, None  # A line's own id first: an old six-hex id can read like a short code (d55bfa).
    if not (memstore.is_code(given.upper()) or ":" in given):
        entry = memstore.find(state, f"decision:{pid}:{given}")  # A decision of decisions.jsonl, by the id its pack line shows.
        return (given, entry) if entry is not None and entry.ref.startswith("decision:") else (given, None)
    entry = memstore.find(state, given)
    if entry is None:
        raise ValueError(f"{given} 不在这个专项的记忆里（写专项记忆那一条开头〔〕里的短码或 id）")
    if entry.scope != "project" or entry.owner_key != pid:
        raise ValueError(f"{given} 不是这个专项的记忆，负责人只能改本专项的")
    entry_id = entry.ref.rsplit(":", 1)[-1]
    if entry.ref.startswith("decision:") and not any(row["id"] == entry_id for row in project_memory.stored(state, pid)):
        return entry_id, entry
    return entry_id, None


def revise_decision(state, action, entry, args, origin):
    """project_memory_revise of a decision of decisions.jsonl: the owner's keeps its words and goes only when it is done
    (retire) or an answer of his newer than it takes its place (supersede ``by``); the lead's own as the lead likes."""
    mine = entry.said != "owner"
    because = {"job": origin.get("job", "")}
    if action == "update":
        if not mine:
            raise ValueError("这是主人的当前决定，不能改正文；办完了用 retire 收进历史，主人有了新的答复就用 supersede（by 写那条新答复的短码）。")
        text = " ".join(str(args.get("text") or "").split())[:memstore.DECISION_CHARS]
        made = memstore.edit(state, entry.ref, text=text or None, who="lead", because=because)
        return reply(True, f"已改专项记忆（{made.code}）：{made.text}。", id=made.code)
    if action == "retire":
        made = memstore.settle(state, entry.ref, who="lead", because=because)
        return reply(True, f"已把专项记忆（{made.code}）收进历史，不再每轮带上，仍可查：{made.text}", id=made.code)
    other = memstore.find(state, str(args.get("by") or "").strip()) if args.get("by") else None
    if other is None or other.ref == entry.ref or other.owner_key != entry.owner_key:
        raise ValueError("决定只能被同一专项里已有的一条替代：by 写那一条的短码" + ("" if mine else "，而且要是主人更新的答复"))
    if not mine and (other.said != "owner" or other.at <= entry.at):
        raise ValueError("这是主人的当前决定，只能被主人更新的答复替代：by 写那条新答复的短码；负责人自己写的新条不行，请问主人。")
    old = memstore.supersede(state, entry.ref, other.ref, who="lead", because=because)
    return reply(True, f"已用（{other.code}）{other.title} 替代 {old.code}；旧的收进历史，不再每轮带上，仍可查。", id=other.code,
                 old=old.code, new=False)


def project_memory_revise(env, request):
    """Update, replace or put away a line of the project's memory (core/project_memory.py update, supersede, retire), named by
    its id or short code; a decision of decisions.jsonl through core/memstore.py (``revise_decision``)."""
    pid, _ = lead(env, request)
    args = request.args
    action, entry_id = str(args.get("op") or ""), str(args.get("id") or "").strip()
    if action not in REVISE_ACTIONS:
        raise ValueError("op 要是 " + "、".join(REVISE_ACTIONS) + " 之一")
    if not entry_id:
        raise ValueError("id 不能为空：写专项记忆那一条开头〔〕里的短码或 id")
    kind, why = args.get("kind"), " ".join(str(args.get("why") or "").split())
    if kind and kind not in MEMORY_KINDS:
        raise ValueError("kind 要是 " + "、".join(OFFERED_KINDS) + " 之一")
    kind = ("constraint" if kind == "rule" else "fact") if kind in NEW_KINDS else kind
    text = " ".join(str(args.get("text") or "").split())[:project_lead.TEXT]
    if text and SNAPSHOT.search(text):
        raise ValueError(SNAPSHOT_REFUSED)
    common = {"why": why, "origin": {"job": str(request.job_key or "")}, "allow": revise_refusal}
    state = Path(env.state_dir)
    entry_id, decision = memory_key(state, pid, entry_id)
    if decision is not None:
        return revise_decision(state, action, decision, args, common["origin"])
    by = str(args.get("by") or "").strip()
    if by:
        by = memory_key(state, pid, by)[0]
    try:
        if action == "update":
            row = project_memory.update(state, pid, entry_id, text=text, kind=kind or "", **common)
            return reply(True, f"已改专项记忆（{row['id']}）：{row['text']}。改之前的原文留在修改记录里。", id=row["id"])
        if action == "supersede":
            old, new, made = project_memory.supersede(state, pid, entry_id, text=text, kind=kind or "",
                                                      by=by, **common)
            return reply(True, f"已用{'新写的' if made else '已有的'}（{new['id']}）{new['text']} 替代 {old['id']}；"
                         "旧的收进历史，不再每轮带上，仍可查。", id=new["id"], old=old["id"], new=made)
        row = project_memory.retire(state, pid, entry_id, **common)
        return reply(True, f"已把专项记忆（{row['id']}）收进历史，不再每轮带上，仍可查：{row['text']}", id=row["id"])
    except KeyError:
        raise ValueError("专项记录不在了") from None


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
    project_lead.keep_style(env.state_dir, now)  # The next project starts from it.
    return reply(True, "已改风格：" + "；".join(f"{name}={value}" for name, value in now.items()), style=now)


def project_finish(env, request):
    pid, record = lead(env, request)
    if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
        raise ValueError(ended_words(record, "再收尾"))
    gate = finish_gate(record)
    if gate:
        return reply(False, gate, gate="owner_first")
    summary = text_of(request.args, "summary")
    live = project_lead.live_members(env.state_dir, record)
    if live:
        return reply(False, f"还有话题在做或等主人（{'、'.join(live)}），不能收尾：等它们结束，或先处理掉。", live=live)
    unmet, items, gaps = checklist_gate(env.state_dir, record)
    if unmet:
        return reply(False, unmet, gate="checklist", unmet=items, gaps=gaps)
    item = project_lead.entry(summary, report=True, kind="finish", job=request.job_key)

    def change(data):
        data["state"], data["state_reason"], data["state_job"] = projects.DONE, summary, request.job_key
        rows = data.get("notes")
        rows = data["notes"] = rows if isinstance(rows, list) else []
        rows.append(item)
        del rows[:-project_lead.NOTES_KEPT]
    projects.update(env.state_dir, pid, change)
    dropped = [entry for entry in checklist.items(record) if entry["state"] == checklist.DROPPED] \
        if turn_source.enabled() else []
    told = ("；目标检查表里主人说不做的：" + "；".join(f"{entry['id']}「{cut(entry['criterion'], 40)}」（{cut(entry['gap'], 40)}）"
                                              for entry in dropped) + "，汇报里写明") if dropped else ""
    return reply(True, f"专项已记为完成：{summary}{told}", state=projects.DONE)


def member_ref(env, record, ref, where) -> str:
    """The topic id of ``ref`` (T number or topic id) when it is one of the project's topics; ValueError, with ``where`` (the row it is
    in) when it is not or there is none."""
    found = butler_topics.find_topic(env, env.chat(), ref)
    if isinstance(found, str):
        raise ValueError(f"{where}：{found}")
    if found[1].topic_id not in (record.get("threads") or ()):
        label = topic_label(Path(env.state_dir), found[1].topic_id) or found[1].topic_id[:8]
        raise ValueError(f"{label} 不是这个专项的话题：先用 project_topic_send 把它收进来，或换成专项里交付这一条的话题。")
    return found[1].topic_id


def topic_rows(env, record, rows, what) -> list:
    """``rows`` (items or actions of the call) with the topics they name (``topic`` of an item, ``topics`` of an item or an action) as
    topic ids of the project's; ValueError for one that is not (``member_ref``)."""
    out = []
    for row in rows:
        row = dict(row) if isinstance(row, dict) else row
        if isinstance(row, dict):
            where = row.get("id") or what
            if str(row.get("topic") or "").strip():
                row["topic"] = member_ref(env, record, row["topic"], where)
            if row.get("topics") is not None:
                refs = row["topics"] if isinstance(row["topics"], list) else [row["topics"]]
                row["topics"] = [member_ref(env, record, ref, where) for ref in refs if str(ref or "").strip()]
        out.append(row)
    return out


def project_checklist(env, request):
    """The project's 目标检查表 (core/project_checklist.py, 协作 on): items added or changed (``items``; a ``topic`` or ``topics`` are
    this project's topics, T number or topic id), 协作 gaps closed (``close_gaps``), the 简报's actions added or changed
    (``actions``, core/project_actions.py), or nothing given to read them. The record changes under its lock, items first and
    then actions, which may name the items just written (each ``apply`` checks everything first: a refusal writes nothing, of
    either). The answer is the checklist as the pack shows it, and, for ``actions`` or a read, the 简报 above it."""
    pid, record = lead(env, request)
    if not turn_source.enabled():
        return reply(False, CHECKLIST_OFF)
    state = Path(env.state_dir)
    args = request.args
    rows, close, acts = (args.get(key) if isinstance(args.get(key), list) else [] for key in ("items", "close_gaps", "actions"))
    what = ("目标检查表和简报" if rows or close else "简报") if acts else "目标检查表"
    if rows or close or acts:
        active(record, "改目标检查表")
        try:
            wanted, wanted_actions = topic_rows(env, record, rows, "新条目"), topic_rows(env, record, acts, "新动作")
        except ValueError as exc:
            return reply(False, str(exc))
        receipts, changed = project_lead.inbox_items(state, pid), []

        def change(data):
            if rows or close:
                changed[:] = checklist.apply(data, wanted, close, receipts=receipts, job=request.job_key)
            if acts:
                changed.extend(project_actions.apply(data, wanted_actions, job=request.job_key))
        try:
            record = projects.update(state, pid, change)
        except ValueError as exc:
            return reply(False, f"{what}没有改：{exc}")
    else:
        changed = []
    members = project_lead.member_rows(state, record)
    text = project_lead.checklist_block(state, record, members, detail=True)
    if acts or not (rows or close):
        text = project_lead.brief_block(state, record, members, full=True, detail=True) + "\n" + text
    head = f"已更新{what}（{'、'.join(changed)}）。" if changed else ""
    left, gaps = checklist.unmet(record, project_lead.inbox_items(state, pid))
    return reply(True, head + "\n" + text if head else text, unmet=[item["id"] for item in left], gaps=[gap["id"] for gap in gaps])


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
    """Answer the member topic's held choice question for the owner (app/triage.py answer). With 协作 on (COVEY_COLLAB=1),
    or ``evidence`` given, its grounds are checked first (app/triage.py grounds): none of the owner's that still holds, and
    nothing is answered (the question stays held; the refusal says to escalate)."""
    pid, record = lead(env, request)
    if not triage.enabled():
        return reply(False, TRIAGE_OFF)
    active(record, "替主人答题")
    try:
        app, ctx, chat = held_topic(env, request, record)
    except ValueError as exc:
        return reply(False, str(exc))
    found = []
    if turn_source.enabled() or "evidence" in request.args:
        try:
            found = triage_flow.grounds(env, record, request.args.get("evidence"))
        except ValueError as exc:
            return reply(False, f"没有替主人答：{exc}", refused="evidence")
    try:
        done = triage_flow.answer(env, app, ctx, chat, record, request.args.get("answers"), f"lead-{request.id}", found)
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
    # Optional, for the card the owner reads on a phone (cards/question_cards.py ``lead_notes``); absent: the card as before.
    extra = {key: " ".join(str(request.args.get(key) or "").split())[:limit]
             for key, limit in (("context", 120), ("recommend", 100), ("if_not", 120))}
    extra = {key: value for key, value in extra.items() if value}
    try:
        app, ctx, chat = held_topic(env, request, record)
    except ValueError as exc:
        return reply(False, str(exc))
    rows = triage_flow.held_questions(env.state_dir, {ctx.topic_id})
    if not rows:
        return reply(False, "这个话题现在没有交给你的问题（可能已经转给主人，或超过 3 分钟了）。")
    sent = [triage_flow.escalate(env, row, why, by="lead", **extra) for _, row in rows]
    acted(env, pid, request, "escalate")
    label = topic_label(Path(env.state_dir), ctx.topic_id) or ctx.topic_id[:8]
    return reply(True, f"已转给主人：{label} 的问题卡" + ("已发出。" if all(sent) else "还没发出去，系统会请主人重新投递。"), sent=all(sent))


TOOLS = {"project_topic_start": project_topic_start, "project_topic_send": project_topic_send,
         "project_topic_stop": project_topic_stop,
         "project_note": project_note, "project_goal": project_goal, "project_remember": project_remember,
         "project_memory_revise": project_memory_revise, "project_style": project_style,
         "project_finish": project_finish, "project_answer": project_answer, "project_escalate": project_escalate,
         "project_checklist": project_checklist}
