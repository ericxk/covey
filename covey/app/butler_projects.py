"""管家的专项工具 (docs/butler.md, COVEY_PROJECTS=1): ``projects_list``, ``project_send`` and ``projects_tidy`` (``TOOLS``, listed to the butler
when COVEY_PROJECTS=1, the last with COVEY_PROJECT_TIDY too, core/butler_tool_specs.py), handlers as app/butler_env.py describes them.

- ``projects_list``: the chat's projects and the proposals 整理专项 left for the owner, in the 看板's words.
- ``project_send`` hands the owner's words to a project's lead as a turn the owner started (a supplement to the turn it is in, else its
  next turn); once a request (its record in the butler's request path, app/butler_topics.py).
- ``projects_tidy`` starts 整理专项 (app/project_tidy.py start).

The lead has its own tools (app/project_tools.py): ``butler_only`` refuses these to a lead's job. Rule 6 (AGENTS.md): a steer that may have
arrived is never sent twice."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import project_facts
from covey.app.butler_env import arg_text, reply
from covey.app.projects import state_of
from covey.core import butler_opened, project_proposals, projects
from covey.core.butler_opened import done
from covey.core.jobs import find_job_record
from covey.core.state import read_object
from covey.core.text import cut
from covey.core.topics import move_topic_message, topic_label


NO_CHAT = "还不知道主人和机器人的私聊（主人还没在私聊里发过消息），没有执行。"
OFF_WORDS = "专项没有开（COVEY_PROJECTS）。"
STATE_WORDS = {projects.ACTIVE: "在做", projects.PAUSED: "停住", projects.DONE: "做完", projects.CANCELLED: "已取消"}
FOLDED_WORDS = "收起"  # A project paused for being idle (core/projects.py ``folded``): the state the butler hears.
LISTED_ENDED = 3  # Projects that ended, listed after the ones that are on.


def butler_only(env, request):
    """A refusal (the answer), or None when the call is the butler's: the lead has its own tools."""
    from covey.app import butler_topics

    if not projects.enabled():
        return reply(False, OFF_WORDS)
    if butler_topics.lead_of(env, request) is not None:
        return reply(False, "负责人不用这个工具：它只管自己的专项，话题用 project_topic_start、project_topic_send。")
    return None


def projects_list(env, request):
    refused = butler_only(env, request)
    if refused is not None:
        return refused
    state, chat = Path(env.state_dir), env.chat()
    found = projects.records(state, chat)
    live = [record for record in found if state_of(record) in (projects.ACTIVE, projects.PAUSED)]
    ended = [record for record in found if record not in live][-LISTED_ENDED:]
    # The proposals waiting for him, said the same way as the 看板 (the owner, 2026-11-18 23:17: 怎么可能没有专项呢).
    waiting = []
    if project_proposals.enabled():
        waiting = project_proposals.waiting(state)  # As the 看板 lists them (app/digest.py projects_view).
    pending = ("；还有 " + str(len(waiting)) + " 个提议等主人定（在看板的专项区）：" + "、".join(
        f"「{cut(record.get('name'), 20)}」（{'、'.join(topic_label(state, topic) or topic[:6] for topic in record.get('topics') or ())}）"
        for record in waiting[:6])) if waiting else ""
    if not live and not ended:
        return reply(True, "还没有专项" + (pending or "。"), count=0, waiting=len(waiting))
    rows = []
    for record in [*live, *ended]:
        threads = "、".join(topic_label(state, topic) or topic[:6] for topic in record.get("threads") or ()) or "还没有话题"
        note = project_facts.lead_note(record)
        rows.append(f"{record['id']} · {cut(record.get('name'), 30)} · {FOLDED_WORDS if projects.folded(record) else STATE_WORDS.get(state_of(record), state_of(record))}"
                    f" · 话题 {threads}" + (f" · 最近：{cut(note, 40)}" if note else ""))
    return reply(True, "\n".join(rows) + (f"\n{pending.lstrip('；')}" if pending else ""), count=len(found), waiting=len(waiting),
                 projects=[{"id": record["id"], "name": record.get("name"), "state": state_of(record)}
                           for record in [*live, *ended]])


def find_project(state, chat, ref):
    """The record ``ref`` names (its id, an id's start, or its name when only one has it) among the chat's that are on or
    finished (the owner's words open a finished one again, project_send); a str saying why not."""
    text = " ".join(str(ref or "").split())
    if not text:
        return "pid 不能为空：用 projects_list 查专项的 id。"
    found = [record for record in projects.records(state, chat) if state_of(record) in (projects.ACTIVE, projects.PAUSED, projects.DONE)]
    for same in (lambda record: record["id"] == text, lambda record: record["id"].startswith(text),
                 lambda record: text in str(record.get("name") or "")):
        matches = [record for record in found if same(record)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return f"「{text}」对得上不止一个专项：{'、'.join(record['id'] for record in matches)}，给完整的 id。"
    return f"没有在做或做完的专项「{text}」：用 projects_list 查 id。"


def lead_target(env, record):
    """(app, context) of the project's lead topic, or None when its topic.json cannot be read or its Harness has no app here.
    ``Topics.target`` gives none for a lead (its Traits row is not a card target), and the lead's topic.json has no root
    message to address it by: the context comes from an alias of the Bridge's own, ``project-lead-<id>``, never a Feishu
    message's id, that belongs to the lead (the way app/butler_tracks.py ``enter`` gets a track's)."""
    from covey.core.topics import load_topic

    state, chat, lead = Path(env.state_dir), str(record.get("chat_id") or ""), str(record.get("lead_topic_id") or "")
    try:
        topic = load_topic(state, lead)
    except (OSError, ValueError):
        return None
    app = env.topics.apps.get(topic.get("harness_id"))
    if app is None or topic.get("chat_id") != chat:
        return None
    alias = projects.LEAD_ALIAS_PREFIX + str(record["id"])
    move_topic_message(state, lead, chat, alias)
    return app, app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": alias})


def lead_prompt(name, text, owner) -> str:
    """What the lead's model gets when the butler hands it the owner's words (the Bridge's note, the butler's task text, the
    owner's own words quoted): a turn the owner started, not a wake-up."""
    from covey.app.butler_handoff import NOTE

    parts = [f"{NOTE} 主人在私聊里对专项「{name}」说了话，由管家转给你；这是主人触发的一轮，不是自动唤醒，你的回复会由 Bridge "
             "发给主人。按他的意思调整专项：开话题、给话题加话、记决定或改风格就用 project_* 工具。",
             "管家整理的说明：\n" + text]
    if owner.words:
        parts.append("主人原话：\n" + "\n".join("> " + line for line in owner.words.split("\n")))
    return "\n\n".join(parts)


def project_send(env, request):
    """Hand the owner's words to a project's lead: a supplement to the turn it is in, else its next turn (message id
    "main-<token>": the owner's, not a wake-up). Once a request."""
    from covey.app import butler_handoff, butler_topics
    from covey.core.history_storage import append_history
    from covey.core.steer import SteerRefused, SteerUncertain

    refused = butler_only(env, request)
    if refused is not None:
        return refused
    chat, state = env.chat(), Path(env.state_dir)
    if not chat:
        return reply(False, NO_CHAT)
    text = arg_text(request.args.get("text"))
    if not text:
        return reply(False, "text 不能为空：写这次交给负责人的事。")
    path, token = butler_opened.record_path(state, request), butler_opened.request_token(request)
    saved = read_object(path)
    if saved.get("answer"):
        return saved["answer"]
    record = find_project(state, chat, request.args.get("pid"))
    if isinstance(record, str):
        return reply(False, record)
    name, lead, mid = cut(record.get("name"), 30) or "专项", record["lead_topic_id"], "main-" + token
    found = lead_target(env, record)
    if found is None:
        return reply(False, f"专项「{name}」的负责人话题读不到，这次没有转。")
    app, ctx = found
    if state_of(record) == projects.DONE:  # His words through the butler: a finished project opens again (the steer below
        from covey.app import project_loop  # reaches no owner_turn).
        try:
            projects.update(state, record["id"], project_loop.owner_spoke)
        except (KeyError, ValueError, OSError):
            return reply(False, f"专项「{name}」已收尾，重开没有成功，这次没有转。")
    # No ``topic_id``: the readers of main-thread/opened (core/butler_opened.py) take a record with one for a topic the request handed
    # on to, and the lead is not one (it is not relayed, has no T number and no thread of its own).
    note = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "lead_topic_id": lead, "project_id": record["id"],
            "at": time.time()}
    butler_opened.save(path, note)
    owner = butler_handoff.asked(state, request.job_key)
    sent = lead_prompt(name, text, owner)
    shown = owner.words or text
    data = {"project_id": record["id"], "lead_topic_id": lead}
    job = butler_topics.live_turn(app, ctx, chat)
    if job is not None:
        try:
            saved = app.steering.deliver_steer(ctx, job, sent, mid, shown=shown)
        except SteerUncertain as exc:
            return done(path, note, reply(False, f"补充可能已送到专项「{name}」负责人的任务 #{job.job_id}，送达没有确认（{exc}）；"
                                        "不会重发，别再转一次。", **data, uncertain=True))
        except SteerRefused as exc:  # Nothing was sent (rule 6).
            return done(path, note, reply(False, f"没有送进专项「{name}」负责人正在跑的任务 #{job.job_id}：{exc}。没有发出；"
                                        "等这一轮结束后再转。", **data))
        return done(path, note, reply(True, f"已转给专项「{name}」的负责人："
                                          f"{butler_topics.steered_words(job, saved)}。", **data, steered=True))
    if not (find_job_record(state, mid) or app.runtime.jobs.settling(mid)):
        append_history(ctx.history_path, "user", shown, {"message_id": mid})
        if app.submission.enqueue_chat_job(ctx, shown, chat, mid, prompt_content=sent, title=name) is None:
            return done(path, note, reply(False, f"专项「{name}」负责人的下一轮没有提交：模型配置无法确认。", **data))
    return done(path, note, reply(True, f"已转给专项「{name}」的负责人：它开了新的一轮，结果会由 Bridge 发给主人。",
                                **data, steered=False))


def projects_tidy(env, request):
    """整理专项 now (app/project_tidy.py start), in its own thread: what the butler tells the owner (started, or how far the one
    running has got)."""
    refused = butler_only(env, request)
    if refused is not None:
        return refused
    tidy = getattr(env, "tidy", None)
    if tidy is None or not project_proposals.enabled():
        return reply(False, "整理专项没有开（COVEY_PROJECT_TIDY）。")
    scope = " ".join(str(request.args.get("scope") or "").split())[:40]
    words = tidy.start(origin="butler", scope=scope)
    return reply(words == project_proposals.STARTED_WORDS or words.startswith(project_proposals.BUSY_PREFIX), words, scope=scope)


TOOLS = {"projects_list": projects_list, "project_send": project_send, "projects_tidy": projects_tidy}
