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
- ``project_ask`` (WAVE12 C4): what the owner is to decide, as a question card under every style. The questions are written
  into the lead turn's own checkpoint (core/jobs.py checkpoint_file, under its lock) with ``origin`` ``project_ask``: at the
  turn's end app/runs.py makes the card of them as of any turn's checkpoint (the 等你 count, the web page, T889's node budget),
  the question record keeps the origin. A second call, or a later turn's, takes the lead's questions still pending in (their
  ids kept), at most ASK_MAX; the older card folds once the new one is shown (app/questions.py fold_older_questions). A
  deploy question (``kind`` deploy with ``deploy``) the owner says yes to gives an approval (app/project_approvals.py).
- ``project_note`` (a line for the project's card; ``report`` marks what the owner is to hear, filtered by the style; the
  kind ``ask`` of W0-2 is taken one release more and asked with ``project_ask``),
  ``project_goal`` (the goal and its done-when as the lead keeps them, ``lead_goal``: the web page shows it under the project's name),
  ``project_remember`` (the project's memory: ``kind`` rule or note, a ``title`` of at most 40 characters, through the one
  write gate, core/memory_gate.py write, 负责人记的: it holds at once in the project and waits for no one, and the lead's card
  says "负责人记下了：…" with 不对 (the job's ``lead_notes``, app/memory_collect.py show); the older kinds that wrote memory.json
  straight are gone (T908 M3b). A line that is a state of the moment, ``SNAPSHOT`` or the gate's, is refused: it goes to the
  brief or ``project_note``; a rule past the project's room for the lead's rules (core/memory_review.py RULE_CAPS) is refused
  until it merges or demotes one), ``project_memory_revise`` (the lead updates, replaces or puts away a line of it, by its id or
  short code, ``revise_refusal`` says whose lines it may not touch; a decision of decisions.jsonl through core/memstore.py;
  each change in the memory's 记录, core/memstore.py noted), ``project_style``, ``project_finish`` write the project's record
  (core/projects.py update, under its record lock; no Feishu call). ``project_finish`` refuses a project 整理专项 made that the
  owner has not spoken to (in the lead's topic, through the butler's project_send, on the project's own cards or page:
  ``owner_lead_at``) and whose lead has opened or fed no topic (``finish_gate``, W2-C).
- ``project_answer`` and ``project_escalate`` are phase 2's question triage (P2-5, COVEY_PROJECT_TRIAGE=1; they are listed
  to the lead only then, and answer that it is off otherwise): the lead answers a member topic's held choice question as the
  owner would (the question's own submit path, with a note that the lead answered) or hands it to the owner with a reason
  (app/triage.py). An answer stands on ``evidence`` the Bridge checks (app/triage.py grounds), with 协作 on or off (W0-2):
  no ground of the owner's that still holds, no answer.
- With 协作 on, ``project_checklist`` keeps the project's 目标检查表 (core/project_checklist.py), and ``project_finish``
  refuses while an item of it is not met or a 协作 gap is not dealt with (``checklist_gate``). An item turned ``dropped``
  names the owner's word for it, which the Bridge checks (``dropped_bases``: his message in the project, or a question
  he answered himself; W2-C). Also with it on, the 简报
  (docs/projects.md 项目简报): ``project_goal``'s ``background``, ``project_checklist``'s ``actions`` (core/project_actions.py),
  and the optional ``action`` and ``item`` of ``project_topic_start`` and ``project_topic_send``, which hang the topic on them
  (``place_of`` looks at the ids before anything is done, ``hang`` writes them once the topic is a member).

A tool that hands out, stops, answers or asks (``acted``) notes what the lead's turn did ({job, whats, at}): a wake-up turn's
report line names it under the styles that tell each step (app/project_loop.py report_out); the reply itself is the owner's
only when he was there (WAVE12 C4, C5).

``project_topic_start`` and ``project_topic_send`` take an ``approval``: the Bridge checks it (app/project_approvals.py check:
this project's, active, the commits the task names all of its batch), heads the handoff with its line and consumes it. Any
line of the lead's own words that starts with ``[Bridge`` is handed on as ``〔负责人写的〕[Bridge…`` (``unforged``): only the
Bridge's head note says what the owner approved.

A refusal is a ValueError, which app/butler_tools.py ButlerTools.execute turns into an answer the lead reads."""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path

from covey.app import butler_topics, project_approvals, project_lead, triage as triage_flow
from covey.app.butler_handoff import choice_error
from covey.app.butler_env import reply
from covey.core import ask_kinds, butler, memory, memory_gate, memstore, project_actions, project_memory, projects, quota_hold, triage
from covey.core import turn_source
from covey.core import project_checklist as checklist
from covey.core import project_proposals as proposals
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import checkpoint_file, find_job_record, upsert_job_record
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_private_text
from covey.core.store import directory_store
from covey.core.text import cut
from covey.core.topics import topic_label

KINDS = ("milestone", "thread", "note")  # project_note's. Its W0-2 ``ask`` is asked with project_ask one release more (``project_note``).
ASK_ORIGIN = "project_ask"  # A question record's and a checkpoint's ``origin``: the lead asked it with project_ask.
ASK_MAX = 4  # Questions on one card of the lead's (core/butler_tool_specs.py PROJECT_ASK maxItems).
FORGED = "〔负责人写的〕"  # Before a line of the lead's own words that starts with "[Bridge" (``unforged``).
APPROVAL = "_approval_line"  # The verified approval line project_topic_start/send hand app/butler_topics.py (never the lead's).
NEW_KINDS = ("rule", "note")  # core/memstore.py's: a 规矩 and a 资料 of the project (``project_remember`` with a ``title``).
# The kinds project_memory_revise takes: the two new ones, then the older ones of the lines already there (style is
# project_style's; practice and pitfall are the older names of constraint). project_remember takes the new two alone.
MEMORY_KINDS = (*NEW_KINDS, *project_memory.KINDS[:-1], *project_memory.LEGACY_KINDS)
OFFERED_KINDS = NEW_KINDS
# A line that holds for a moment, not for the project: a release number or generation in production, the cluster's room now, how
# far something is, who does what now, by when (T852, T908 M3b). It goes stale at once and sits in every turn after, so it is
# the brief's or a ``project_note``, not a memory.
SNAPSHOT = re.compile(r"现网第\s*\d+\s*代|第\s*\d+\s*代(?:部署|发版|发布|版本)|余量\s*[约≈]?\s*\d+\s*卡|剩余\s*\d+\s*卡|"
                      r"(?:当前|目前|现在)(?:进度|已完成|完成)\s*\d+\s*[%/]|进度\s*\d+\s*%|"
                      r"(?<![A-Za-z0-9])T\d+\s*(?:负责|在做|在跑|接手|跟进)|"
                      r"(?<!每)(?:\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}-\d{1,2}|周[一二三四五六日天]|月底|月初)\s*(?:前|之前|以前)?\s*"
                      r"(?:完成|上线|交付|做完|发布|提测)")
SNAPSHOT_REFUSED = ("这是会变的现状（发版号、现网第几代、集群余量、进度、谁在做哪件、几号前交付这类），过后就过时，不要记进专项记忆：")
BRIEF_PLACE = "写进项目简报（project_goal 的 background）或 project_note（专项卡上的进展）；记忆里只放长期成立的规矩和资料。"
NOTE_PLACE = "写 project_note（专项卡上的进展）；记忆里只放长期成立的规矩和资料。"
REMEMBER_KINDS = "kind 要是 rule 或 note（rule 规矩：以后照做的；note 资料：供参考的事实、联系人、做法）。"
REVISE_ACTIONS = ("update", "supersede", "retire")
ACTED_KEPT = 40  # The record's ``acted`` rows kept: one a lead turn that handed out, stopped or answered.
TRIAGE_OFF = "问题分流没有开：负责人不能替主人答题，也不能转交问题；话题里的问题会直接问主人。"
CHECKLIST_OFF = "协作没有开（COVEY_COLLAB）：没有目标检查表，收尾照原来的规则。"
BRIEF_OFF = "协作没有开（COVEY_COLLAB）：没有简报，不能带 background、action、item。"
FINISH_NEXT = ("现在不能收尾。先安排补齐：project_topic_send 让原话题返工，或 project_topic_start 开新话题；核对过证据再用 project_checklist "
               "把那一条标 done（写 evidence）；主人说不要了的标 dropped，gap 写主人怎么说的，evidence 写他的依据（msg:<消息 id> 或 "
               "ask:<问题 token>）；协作缺口处理完用 close_gaps 写怎么处理的。要主人定范围或取舍的，合成一个问题用 project_ask 问他。")
HANDED = ("start", "send")  # What of ``acted`` is the lead handing out work (``finish_gate``); a stop, an answer or a question is not.


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
DONE_NEXT = ("主人对专项说一句话（负责人话题里、管家转来、网页上）专项就会自动重开；他在专项的某个话题里说话不会重开专项。"
             "告诉主人这一点和你打算怎么做，不要说做不了。")
OWNER_FIRST = ("这个专项是整理专项时把已有的话题收进来建的，建好后主人还没对你说过话、你也还没开过或派过话题："
               "收进来的话题做完不等于专项完成，现在不能收尾。用 project_ask 说清每个话题做到哪、还剩什么、问主人下一步，"
               "然后结束这一轮，等主人下一步；主人在负责人话题里（或经管家转给你）说过话、或你用 project_topic_start、"
               "project_topic_send 派过活之后才能 project_finish。")


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


def handed_out(record) -> bool:
    """Whether the lead opened or fed a topic of ``record``'s project (``acted`` rows with ``start`` or ``send``, HANDED; a row of
    an older release kept one ``what``)."""
    for row in record.get("acted") or ():
        if not isinstance(row, dict):
            continue
        whats = row.get("whats") if isinstance(row.get("whats"), list) else [row.get("what")]
        if any(what in HANDED for what in whats):
            return True
    return False


def finish_gate(record) -> str:
    """'' when the lead may finish ``record``; else why not (``OWNER_FIRST``): a project 整理专项 made of topics that were there
    already (origin ``tidy``), which the owner has not spoken to since (``owner_lead_at``, app/project_loop.py owner_spoke:
    his turn in the lead's topic, the butler's project_send, the project's pause card or web page; his word in a member
    topic, ``heard``, is that topic's and does not count) and whose lead has handed out no work (``handed_out``: a stop, an
    answer, an escalation or a question is not). Its topics all done is how it began, not its goal reached (else a project
    split off would be finished by its lead's first wake-up turn). A project the owner started (direct, an upgrade) began
    with his words. W2-C: ``owner_at`` (any word of his in the project) and any ``acted`` row let it through before."""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind") != "tidy" or record.get("owner_lead_at") or handed_out(record):
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


def text_of(args, name="text", limit=project_lead.TEXT, *, strict=False) -> str:
    """``args[name]`` on one line, cut to ``limit``; ValueError when empty. ``strict``: one longer is refused instead (what
    the owner is sent whole, project_lead.NOTE_TEXT: a cut one ended mid-sentence in his chat)."""
    text = " ".join(str(args.get(name) or "").split())
    if not text:
        raise ValueError(f"{name} 不能为空")
    if strict and len(text) > limit:
        raise ValueError(f"{name} 最多 {limit} 字（这次 {len(text)} 字）：精简后再调一次，Bridge 不替你截断。")
    return text[:limit]


def acted(env, pid, request, what) -> None:
    """Note what the lead's turn ``request.job_key`` did (``what``: start, send, stop, answer, escalate, ask): one row a turn,
    ``{job, whats, at}``, each kind once in the order done. app/project_loop.py report_out says it in a wake-up turn's report
    line under the styles that tell each step; app/project_tools.py finish_gate reads that the lead handed out work."""
    key = str(getattr(request, "job_key", "") or "")
    if not key:
        return

    def change(data):
        rows = data.get("acted")
        rows = data["acted"] = rows if isinstance(rows, list) else []
        row = next((row for row in rows if isinstance(row, dict) and row.get("job") == key), None)
        if row is None:
            row = {"job": key, "whats": [], "at": time.time()}
            rows.append(row)
        whats = row.get("whats") if isinstance(row.get("whats"), list) else [row["what"]] if row.get("what") else []
        row["whats"] = whats if what in whats else [*whats, what]
        row.pop("what", None)
        del rows[:-ACTED_KEPT]
    projects.update(env.state_dir, pid, change)


def unforged(text):
    """``text`` (the lead's words for a topic) with each line that starts with "[Bridge" set after FORGED: a topic believes only
    the Bridge's own head note (WAVE12 C4, RT-3). Anything but a string as it is."""
    if not isinstance(text, str) or "[Bridge" not in text:
        return text
    return "\n".join(FORGED + line if line.lstrip().startswith("[Bridge") else line for line in text.split("\n"))


def approved(env, pid, record, request, topic, words) -> tuple:
    """(the approval row, its head line) the call's ``approval`` names, checked for handing ``topic`` the task ``words``
    (app/project_approvals.py check); (None, '') when it names none; ValueError saying why it is refused."""
    given = " ".join(str(request.args.get("approval") or "").split())
    if not given:
        return None, ""
    row, why = project_approvals.check(record, given, project_approvals.named_commits(*words), topic=topic,
                                       request=str(getattr(request, "id", "") or ""))
    if row is None:
        raise ValueError(why)
    return row, project_approvals.line(row)


def spoke_note(record, row) -> str:
    """The reminder when the owner spoke in the project after he approved ``row`` (it does not refuse)."""
    return (" 注意：主人在同意之后又在专项里说过话，确认他没改口再派。" if row is not None and project_approvals.spoke_since(record, row)
            else "")


def handed_args(args, keep_out=("action", "item", "approval")) -> dict:
    """The lead's arguments as app/butler_topics.py takes them: without ``keep_out`` and any of the Bridge's own (``_``…), its
    words ``unforged``."""
    return {key: unforged(value) if key in ("prompt", "text", "note") else value
            for key, value in args.items() if key not in keep_out and not str(key).startswith("_")}


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
    try:
        row, head = approved(env, pid, record, request, "", (request.args.get("prompt"), request.args.get("title")))
    except ValueError as exc:
        return reply(False, f"没有开出话题：{exc}", refused="approval")
    args = {**handed_args(request.args), "user_requested": request.args.get("user_requested") is True,
            **({APPROVAL: head} if head else {})}
    answer = butler_topics.topic_start(env, dataclasses.replace(request, args=args))
    topic = answer.get("data", {}).get("topic_id")
    if topic:
        projects.add_member(env.state_dir, pid, topic)  # Again for a request that ran before: once only.
        answer = {**answer, "text": answer["text"] + hang(env, pid, topic, request, place)
                  + files_left(env, request, "用 project_topic_send 带上 attachments 补给它")
                  + used_note(env, pid, record, row, topic, request)}
        acted(env, pid, request, "start")
    return answer if answer.get("ok") else {**answer, "text": "没有开出话题：" + answer["text"]}


def used_note(env, pid, record, row, topic, request) -> str:
    """Consume the approval ``row`` (None: nothing) the hand-on to ``topic`` carried; the words to add to the answer."""
    if row is None:
        return ""
    project_approvals.used(env.state_dir, pid, row["id"], topic=topic, job=str(request.job_key or ""),
                           request=str(getattr(request, "id", "") or ""))
    return f" 已带上部署授权（凭证 {row['id']}，Bridge 写在交接说明开头）。" + spoke_note(record, row)


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
    try:
        row, head = approved(env, pid, record, request, topic, (request.args.get("text"), request.args.get("note")))
    except ValueError as exc:
        return reply(False, f"没有转：{exc}", refused="approval")
    if topic not in (record.get("threads") or ()):
        refused = take_in(env, chat, pid, topic, request.args.get("text"))
        if refused:
            return reply(False, refused)
    args = {"topic": topic, "text": unforged(request.args.get("text")), "note": unforged(request.args.get("note")),
            "attachments": request.args.get("attachments"), "memory": request.args.get("memory"),
            **({APPROVAL: head} if head else {})}
    answer = butler_topics.topic_send(env, dataclasses.replace(request, args=args))
    if answer.get("ok"):
        answer = {**answer, "text": answer["text"] + hang(env, pid, topic, request, place)
                  + files_left(env, request, "带上 attachments 再给它加一次话")
                  + used_note(env, pid, record, row, topic, request)}
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
    """(tasks asked to stop, questions or waiting turns put away, launch cards closed) of stopping the topic of ``ctx`` for the owner: its queued and running tasks
    (app/project_clicks.py ``stop_tasks``, forwarded to the task runner as a stop on the card is, ``by`` who stopped them:
    core/jobs.py STOP_SOURCES), then the question it waits on and its waiting turn, as a switch of configuration voids them
    (app/binding.py ``retire_open_questions``, here ``cancelled`` for ``reason`` with ``note`` and the call's ``why``), then
    its launch cards not started yet. A project's lead (``project_topic_stop``) and the butler (app/butler_tools.py
    topic_stop) stop a topic so. No lock is held across a call."""
    from covey.app import binding, project_clicks
    from covey.app.launch import close_open_launches
    from covey.core.store import directory_store

    topic = ctx.topic_id
    stopped = project_clicks.stop_tasks(app.controls, ctx, [topic], chat, f"{reason.replace('_', '-')}-{request.id}", by=by)
    why = " ".join(str(request.args.get("why") or "").split())[:120]
    # What is put away is counted first (retire_open_questions returns nothing): an idle topic has none, and says so.
    asks = sum(1 for _, row in directory_store(ctx.state_dir / "questions").query(topic_id=topic, chat_id=chat)
               if row.get("status") in binding.OPEN_FOR_THE_OWNER)
    waiting = sum(1 for _, row in directory_store(ctx.state_dir / "jobs").query(topic_id=topic, chat_id=chat)
                  if row.get("status") == "waiting_user" and row.get("key"))
    retired = max(asks, waiting)
    binding.retire_open_questions(env.topics, app, ctx, chat, reason=reason, job_status="cancelled", by=by,
                                  message=note + (f"：{why}" if why else ""))
    return stopped, retired, close_open_launches(ctx, chat, getattr(env, "messenger", None), note)


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
    stopped, retired, launches = stop_topic(env, app, ctx, chat, request, by="lead", reason="lead_stop", note=STOP_NOTE)
    acted(env, pid, request, "stop")
    cards = f"，没开工的启动卡收掉 {launches} 张" if launches else ""
    return reply(True, f"已停掉 {label}：请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停），等主人回答的问题已收掉{cards}；"
                 "它仍记在专项里、算结束。要接着做，用 project_topic_send。", stopped=stopped, retired=retired, launches=launches, topic_id=topic)


def project_note(env, request):
    pid, record = lead(env, request)
    args = request.args
    if args.get("kind") == project_lead.ASK:
        # W0-2's ask (a session that began before W1-A): asked as a question card, one release more; then taken out.
        return project_ask(env, dataclasses.replace(request, args={
            "summary": text_of(args), "questions": [{"id": "ask", "question": text_of(args), "kind": ask_kinds.OTHER}]}))
    kind = args.get("kind") if args.get("kind") in KINDS else "note"
    report, decision = args.get("report") is True, args.get("decision") is True
    text = text_of(args, limit=project_lead.NOTE_TEXT, strict=True)
    item = project_lead.entry(text, limit=project_lead.NOTE_TEXT, report=report, kind=kind, job=request.job_key)

    def change(data):
        # The decision log's copy keeps project_lead.TEXT: every turn's pack carries the latest DECISIONS of them.
        logged = {**item, "text": cut(item["text"], project_lead.TEXT), "report": False}
        for field, row, kept in (("notes", item, project_lead.NOTES_KEPT),
                                 *((("decisions", logged, project_lead.DECISIONS_KEPT),) if decision else ())):
            rows = data.get(field)
            rows = data[field] = rows if isinstance(rows, list) else []
            rows.append(row)
            del rows[:-kept]
    projects.update(env.state_dir, pid, change)
    shown = ("，Bridge 会按风格把这句原文发给主人" if project_lead.reportable(record.get("style"), kind)
             else "，按风格不会打扰主人") if report else ""
    return reply(True, f"已记在专项卡上{shown}" + ("；也记进了决定日志" if decision else "") + "。", id=item["id"], report=report)


def _words(value, limit=0) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text[:limit] if limit else text


def ask_question(raw, index) -> dict:
    """One question of ``project_ask`` as a card takes it (core/checkpoint.py normalize_questions keeps the rest); ValueError
    saying what is wrong with it."""
    where = f"第 {index} 题"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}要是一个对象（id、question、kind…）")
    qid, words, kind = " ".join(str(raw.get("id") or "").split()), _words(raw.get("question"), 500), raw.get("kind")
    if not qid or not words:
        raise ValueError(f"{where}要有 id 和 question")
    if kind not in ask_kinds.QUESTION_KINDS:
        raise ValueError(f"{where}的 kind 只能是 {'、'.join(ask_kinds.QUESTION_KINDS)} 之一")
    made = {"id": qid, "question": words, "kind": kind}
    for name, limit in (("header", 40), ("context", 300)):
        if _words(raw.get(name)):
            made[name] = _words(raw.get(name), limit)
    if raw.get("multiple") is True:
        made["multiple"] = True
    options, labels = [], set()
    for option in raw.get("options") or () if isinstance(raw.get("options"), list) else ():
        label = _words(option.get("label"), 100) if isinstance(option, dict) else _words(option, 100)
        if not label or label in labels:
            continue
        labels.add(label)
        item = {"label": label}
        if isinstance(option, dict) and _words(option.get("description")):
            item["description"] = _words(option.get("description"), 200)
        if isinstance(option, dict) and isinstance(option.get("grants_approval"), bool):
            item["grants_approval"] = option["grants_approval"]
        options.append(item)
    if options:
        made["options"] = options
    plan = raw.get("deploy")
    if kind == project_approvals.DEPLOY:
        if not isinstance(plan, dict) or not options:
            raise ValueError(f"{where}的 kind 是 deploy：要给选项（同意的那项可标 grants_approval）并填 deploy（commits、target）")
        made["deploy"] = {"commits": project_approvals._strings(plan.get("commits")),
                          "topics": project_approvals._strings(plan.get("topics")),
                          "target": " ".join(str(plan.get("target") or "").split())[:200]}
    return made


def pending_asks(state_dir, topic) -> list:
    """The lead's questions still waiting for the owner in its topic ``topic``: each pending question record's questions whose
    origin is ASK_ORIGIN, oldest first (a later one of the same id replaces it)."""
    records = sorted((record for _, record in directory_store(Path(state_dir) / "questions").query(topic_id=topic, status="pending")
                      if record.get("origin") == ASK_ORIGIN),
                     key=lambda record: (int(record.get("job_id") or 0), record.get("created_at") or 0))
    return [dict(question) for record in records for question in record.get("questions") or () if isinstance(question, dict)]


def merged(*groups) -> list:
    """The questions of ``groups`` in order, one per id: a later one of the same id takes the earlier one's place."""
    found = {}
    for group in groups:
        for question in group:
            found[str(question.get("id") or "")] = question  # A dict keeps the place the id first took.
    return list(found.values())


def project_ask(env, request):
    """Ask the owner (WAVE12 C4): the questions go into the lead turn's checkpoint, under its lock, with the lead's questions
    still pending and those an earlier call of this turn wrote (``merged``, at most ASK_MAX); the turn ends waiting for him and
    the card is made at its end (app/runs.py). Nothing is sent from here."""
    pid, record = lead(env, request)
    raw = request.args.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("questions 不能为空：至少一题")
    if len(raw) > ASK_MAX:
        raise ValueError(f"一次最多问 {ASK_MAX} 题：合并或删掉不急的")
    asked = [ask_question(question, index) for index, question in enumerate(raw, 1)]
    if len({question["id"] for question in asked}) != len(asked):
        raise ValueError("这几题的 id 有重复")
    state = Path(env.state_dir)
    job = find_job_record(state, request.job_key) or {}
    older = pending_asks(state, str(job.get("topic_id") or ""))
    path = checkpoint_file(state, request.job_key)
    summary = _words(request.args.get("summary"), 200)
    with resource_lock("checkpoint", path):
        cp = read_checkpoint(path)
        current = [q for q in cp.get("questions") or () if isinstance(q, dict)] if cp.get("origin") == ASK_ORIGIN else []
        questions = merged(older, current, asked)
        if len(questions) > ASK_MAX:
            ids = "、".join(q["id"] for q in merged(older, current))
            raise ValueError(f"连同还没答的（{ids}）一共 {len(questions)} 题，超过 {ASK_MAX} 题：先精简，"
                             "或用同一个 id 改写旧题")
        question = summary or (cp.get("question") if current else "") or questions[0]["question"]
        cp.update(requires_user=True, origin=ASK_ORIGIN, questions=questions, question=question)
        write_private_text(path, json.dumps(cp, ensure_ascii=False))
    acted(env, pid, request, "ask")
    kept = [q["id"] for q in older if q["id"] in {question["id"] for question in questions}]
    return reply(True, f"已记下 {len(questions)} 题，这一轮结束时 Bridge 发问题卡给主人（任何汇报风格都发，进“等你”）"
                 + (f"；还没答的 {'、'.join(kept)} 并进了这张卡，旧卡会收起" if kept else "")
                 + "。不用在回复里再问一遍；主人答了会接着叫你。", questions=[q["id"] for q in questions])

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


def snapshot_refused() -> str:
    """Where a state of the moment goes instead of the memory: the brief with 协作 on, else the card's notes."""
    return SNAPSHOT_REFUSED + (BRIEF_PLACE if turn_source.enabled() else NOTE_PLACE)


def project_remember(env, request):
    pid, record = lead(env, request)
    kind = request.args.get("kind")
    if kind not in NEW_KINDS:
        raise ValueError(REMEMBER_KINDS)
    text = text_of(request.args)
    state = Path(env.state_dir)
    legacy = "constraint" if kind == "rule" else "fact"
    if any(row["kind"] == legacy and row["text"] == text for row in project_memory.tombstones(state, record)):
        raise ValueError("主人删掉过这条专项记忆，不再记回去。")
    if SNAPSHOT.search(text):
        raise ValueError(snapshot_refused())
    return remember_new(state, pid, kind, text, request)


def lead_room(state, pid) -> tuple[int, int]:
    """(rules, characters) of the lead's rules in force in the project (负责人记的: not the owner's)."""
    rules = [entry for entry in memstore.entries(state, scope="project", owner_key=pid, kinds=("rule",)) if entry.said != "owner"]
    return len(rules), sum(len(entry.text) for entry in rules)


def room_refusal(state, pid, text) -> str:
    """Why the lead's new rule ``text`` does not fit the project's room for its rules (core/memory_review.py RULE_CAPS, the
    turn lists the titles alone past it), '' when it fits or says what a rule there says already."""
    from covey.core.memory_review import RULE_CAPS

    most, chars = RULE_CAPS["project"]
    if any(memstore.same_key(entry.text) == memstore.same_key(text)
           for entry in memstore.entries(state, scope="project", owner_key=pid, kinds=("rule",))):
        return ""
    count, used = lead_room(state, pid)
    if count < most and used + len(text) <= chars:
        return ""
    return (f"负责人记的规矩已占满这个专项的名额（{most} 条 / {chars:,} 字，现在 {count} 条 {used:,} 字），没有记。"
            "先用 project_memory_revise 把说同一件事的合并（supersede，by 写要留的那条），或把不用每轮照做的改成资料"
            "（update，kind 写 fact），再记这条。")


def remember_new(state, pid, kind, text, request):
    """project_remember of a rule or a note through core/memory_gate.py write: the lead's (负责人记的), in force in the project
    at once; it goes to no 待审核 (the owner does not review the lead's lines one by one). A rule past the room for the lead's
    rules is refused (``room_refusal``), as are the gate's refusals (a state of the moment: the brief's). A new rule is a line
    on the lead's card (the job's ``lead_notes``). ValueError for what is refused (a title too long, memstore's)."""
    title = " ".join(str(request.args.get("title") or "").split())
    if len(title) > memstore.TITLE_CHARS:
        raise ValueError(f"title 最多 {memstore.TITLE_CHARS} 个字，这次 {len(title)} 个：写一句短的标题。")
    if kind == "rule":
        full = room_refusal(state, pid, text)
        if full:
            raise ValueError(full)
    written = memory_gate.write(state, kind=kind, scope="project", owner_key=pid, text=text, title=title, proof=None, by="lead",
                                source="project_remember", place="lead", origin={"job": str(request.job_key or "")}, strict=True)
    if written.entry is None:
        offered = f"可以改成：{written.rewrite}。" if written.rewrite else ""
        raise ValueError(f"{written.reason or '这条写法不合适'}，没有记。{offered}进度、分工、排期这类会变的现状不进记忆："
                         + (BRIEF_PLACE if turn_source.enabled() else NOTE_PLACE))
    entry = written.entry
    words = "规矩" if kind == "rule" else "资料"
    if written.done == memstore.SAME:
        return reply(True, f"专项记忆里已有这条（{entry.code}），没有重复记。", id=entry.code, ref=entry.ref, new=False)
    if kind == "rule":
        card_line(state, request, entry)
    return reply(True, f"已记进专项{words}（{entry.code}）{entry.title}：{entry.text}。", id=entry.code, ref=entry.ref, new=True)


def card_line(state, request, entry) -> None:
    """负责人记下了：<the rule> · 管这个专项, with 不对, on the lead's card of this turn: a note in the job's ``lead_notes`` that
    app/memory_collect.py shows with the turn's other memory lines once it ends. Never raises."""
    try:
        job = find_job_record(state, str(request.job_key or "")) or {}
        if not job.get("key"):
            return
        notes = [dict(note) for note in job.get("lead_notes") or () if isinstance(note, dict)]
        notes.append({"kind": "remembered", "by": "lead", "text": entry.text, "title": entry.title, "scope": "管这个专项",
                      "entry_id": memstore.parse_ref(entry.ref)[2], "ref": entry.ref, **({"code": entry.code} if entry.code else {})})
        upsert_job_record(state, str(job["key"]), lead_notes=notes[-5:])
    except Exception:  # noqa: BLE001 - the rule is written whether or not the card says so
        pass


def revise_refusal(action, entry, other=None) -> str:
    """Why the lead may not ``action`` (update, supersede or retire) the line ``entry`` (a row with its meta), '' when it may. The
    owner's 规矩 and his explicit limits are his alone; his 决定 (an answer he gave) keeps its words, is put away when it
    is done, and is replaced only by a later line of the owner's (``other``, what takes its place); the lead's own are the lead's."""
    facts = memory.describe(entry)
    if facts["asserted_by"] != turn_source.PARTY_OWNER:
        return ""
    if facts["layer"] == memory.RULE or entry["kind"] == "constraint":
        return "这是主人定的规矩（或他明确留下的限制），负责人不能改、替代或作废；要改请问主人，由主人自己在专项卡上改。"
    if action == "retire":
        return ""
    if action == "update":
        return "这是主人的决定，不能改正文；办完了用 retire 收进历史，主人有了新的答复就用 supersede（by 写那条新答复的 id）。"
    newer = other is not None and memory.describe(other)["asserted_by"] == turn_source.PARTY_OWNER and other.get("at", 0) > entry.get("at", 0)
    return "" if newer else "这是主人的决定，只能被主人更新的答复替代：by 写那条新答复的 id；负责人自己写的新条不行，请问主人。"


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
            raise ValueError("这是主人的决定，不能改正文；办完了用 retire 收进历史，主人有了新的答复就用 supersede（by 写那条新答复的短码）。")
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
        raise ValueError("这是主人的决定，只能被主人更新的答复替代：by 写那条新答复的短码；负责人自己写的新条不行，请问主人。")
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
        raise ValueError(snapshot_refused())
    common = {"why": why, "origin": {"job": str(request.job_key or "")}, "allow": revise_refusal}
    state = Path(env.state_dir)
    entry_id, decision = memory_key(state, pid, entry_id)
    if decision is not None:
        return revise_decision(state, action, decision, args, common["origin"])
    by = str(args.get("by") or "").strip()
    if by:
        by = memory_key(state, pid, by)[0]
    ref, because = f"project:{pid}:{entry_id}", {"job": common["origin"]["job"]}
    was = memstore.find(state, ref)  # In the memory's 记录 as every other change is (T908 M3b).
    try:
        if action == "update":
            row = project_memory.update(state, pid, entry_id, text=text, kind=kind or "", **common)
            memstore.noted(state, was, ref, who="lead", action="edit", because=because)
            return reply(True, f"已改专项记忆（{row['id']}）：{row['text']}。改之前的原文留在修改记录里。", id=row["id"])
        if action == "supersede":
            old, new, made = project_memory.supersede(state, pid, entry_id, text=text, kind=kind or "",
                                                      by=by, **common)
            if made:
                memstore.noted(state, None, f"project:{pid}:{new['id']}", who="lead", action="add", because=because)
            memstore.noted(state, was, ref, who="lead", action="supersede", because=because)
            return reply(True, f"已用{'新写的' if made else '已有的'}（{new['id']}）{new['text']} 替代 {old['id']}；"
                         "旧的收进历史，不再每轮带上，仍可查。", id=new["id"], old=old["id"], new=made)
        row = project_memory.retire(state, pid, entry_id, **common)
        memstore.noted(state, was, ref, who="lead", action="settle" if was is not None and was.kind == "decision" else "archive",
                       because=because)
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
    summary = text_of(request.args, "summary", limit=project_lead.NOTE_TEXT, strict=True)
    live = project_lead.live_members(env.state_dir, record)
    if live:
        return reply(False, f"还有话题在做或等主人（{'、'.join(live)}），不能收尾：等它们结束，或先处理掉。", live=live)
    unmet, items, gaps = checklist_gate(env.state_dir, record)
    if unmet:
        return reply(False, unmet, gate="checklist", unmet=items, gaps=gaps)
    item = project_lead.entry(summary, limit=project_lead.NOTE_TEXT, report=True, kind="finish", job=request.job_key)

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


OWNER_ANSWERED = ("submitting", "answered", "uncertain")  # A question the owner gave his answer to (its delivery aside).


def owner_basis(env, record, ref) -> bool:
    """Whether ``ref`` (core/project_checklist.py BASIS: msg:<id> or ask:<token>) is the owner's own word in ``record``'s
    project: his message in it (app/triage.py owner_message, project_answer's msg: ground), a question of the project's
    topics (the lead's or a member's) he answered himself (its ``answer_by`` party owner, WAVE12 C2), or his 收尾 on the
    project's pause card (core/project_checklist.py pause_ref). Reads only."""
    kind, _, key = ref.partition(":")
    if kind == "msg":
        return bool(triage_flow.owner_message(env, record, key))
    if ref.startswith(checklist.PAUSE_REF):
        ask = record.get("pause_ask") if isinstance(record.get("pause_ask"), dict) else {}
        return ask.get("choice") == "finish" and checklist.pause_ref(ask) == ref
    if not triage_flow.MESSAGE_ID.fullmatch(key):
        return False
    question = read_object(triage_flow.question_path(env.state_dir, key))
    topics = {str(record.get("lead_topic_id") or ""), *(topic for topic in record.get("threads") or () if isinstance(topic, str))}
    by = question.get("answer_by") if isinstance(question.get("answer_by"), dict) else {}
    return (question.get("token") == key and question.get("topic_id") in topics and question.get("chat_id") == record.get("chat_id")
            and question.get("status") in OWNER_ANSWERED and by.get("party") == turn_source.PARTY_OWNER)


def dropped_bases(env, record, rows) -> tuple:
    """(the refs that are the owner's word, ``owner_basis``; those that are not) among what the rows of a project_checklist
    call that turn an item ``dropped`` name in their ``evidence`` (an item's own evidence when the row gives none), each
    once. core/project_checklist.py apply then takes a drop only on one of the first (W2-C)."""
    by_id = {item["id"]: item for item in checklist.items(record)}
    refs = []
    for row in rows:
        if isinstance(row, dict) and row.get("state") == checklist.DROPPED:
            text = row.get("evidence") if "evidence" in row else by_id.get(str(row.get("id") or ""), {}).get("evidence")
            refs.extend(checklist.bases_of(text))
    good, bad = set(), []
    for ref in dict.fromkeys(refs):
        (good.add(ref) if owner_basis(env, record, ref) else bad.append(ref))
    return good, bad


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
        bases, unproven = dropped_bases(env, record, wanted)  # Read before the record's lock: nothing of it under the lock.

        def change(data):
            if rows or close:
                changed[:] = checklist.apply(data, wanted, close, receipts=receipts, job=request.job_key, bases=bases)
            if acts:
                changed.extend(project_actions.apply(data, wanted_actions, job=request.job_key))
        try:
            record = projects.update(state, pid, change)
        except ValueError as exc:
            told = (f"；这些依据 Bridge 核对不上（不是主人在这个专项里说的话或他本人答过的题）：{'、'.join(unproven)}"
                    if unproven else "")
            return reply(False, f"{what}没有改：{exc}{told}")
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
    """Answer the member topic's held choice question for the owner (app/triage.py answer). Its grounds (``evidence``) are
    checked first, with 协作 (COVEY_COLLAB) on or off (W0-2: off, the lead had answered on nothing): none of the owner's that
    still holds (app/triage.py grounds), and nothing is answered (the question stays held; the refusal says to escalate)."""
    pid, record = lead(env, request)
    if not triage.enabled():
        return reply(False, TRIAGE_OFF)
    active(record, "替主人答题")
    try:
        app, ctx, chat = held_topic(env, request, record)
    except ValueError as exc:
        return reply(False, str(exc))
    try:
        found = triage_flow.grounds(env, record, request.args.get("evidence"), asked=triage_flow.held_words(env, ctx.topic_id))
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
    return reply(True, f"已替主人答了 {done['line']}（话题照常往下做，记在专项卡的分流记录里，主人不同意会在那个话题里直接说）。",
                 status=done["status"])


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
         "project_note": project_note, "project_ask": project_ask, "project_goal": project_goal, "project_remember": project_remember,
         "project_memory_revise": project_memory_revise, "project_style": project_style,
         "project_finish": project_finish, "project_answer": project_answer, "project_escalate": project_escalate,
         "project_checklist": project_checklist}
