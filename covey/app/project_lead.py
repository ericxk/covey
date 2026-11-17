"""The lead's runs (docs/architecture.md 专项, phase 2, COVEY_PROJECTS=1): what a project's lead topic (the 负责人) reads and
how its session lives. Only a lead topic's turn (core/butler.py ``is_lead``) comes here; with the switch off nothing does.

- ``state_pack``: the project's state read from disk, put in front of every turn: the goal, the members (status, one
  sentence, deliverables), the project's memory and library (phase 2's P2-7 fills both; here the memory the lead wrote
  with project_remember, the library empty), the style, the inbox since its last wake-up and the last ten decisions. The
  session is only a cache of it: a new one gets the whole pack, a resumed one the changing parts.
- ``lead_rules``: the lead's rules in layers (identity, tools, style, and the practice layer phase 3 fills for a
  specialist: nothing now).
- ``settle_session`` (app/worker.py ``_start_turn``): the session is dropped, and the pack sent again, when the previous
  turn read over ``RESET_TOKENS``, the topic has been idle ``RESET_IDLE`` seconds, or the model changed.
- ``lead_turn`` (app/butler.py ``butler_turn``): the turn's context (its tools, the built-in ones narrowed to
  ``LEAD_TOOLS``) and message (rules, pack, then the turn's own message).
- The inbox, ``projects/<id>/inbox/<n>.json`` ({id, kind, thread, text, at, taken_by}): receipts of what happened since
  the lead last looked (a member topic's turn ended, a question held, the owner spoke in a member topic). ``add_receipt`` writes
  one (the Bridge does, app/project_loop.py: a member's turn ended, the owner spoke in a member topic, nothing moved), ``take_receipts`` gives the lead's turn what it has not read.

Rules 1 and 6 (AGENTS.md): the records are written under their own lock and nothing here sends to Feishu."""
from __future__ import annotations

import dataclasses
import secrets
import sys
import time
from pathlib import Path

from covey.app.prompts import TOOL_NOTES_LANGUAGE
from covey.app.triage_view import pack_block
from covey.core import board_read, butler, project_library, project_memory, projects, triage
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import job_key, job_path, read_job_record, topic_job_records
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json
from covey.core.statuses import ENDED, RUNNING
from covey.core.text import cut
from covey.core.topics import topic_label, topic_title
from covey.core.turn_words import RESULTS, UNSET_PHASES

RESET_TOKENS = 150_000  # A previous turn that read more than this: the next starts a new session.
RESET_IDLE = 24 * 3600  # Idle this long: the cached prompt is long gone, a new session costs no more.
DECISIONS = 10  # Entries of the decision log in the pack.
NOTES_KEPT, DECISIONS_KEPT = 100, 50  # What the record keeps of each list (the memory is core/project_memory.py's).
TEXT = 200  # Characters of one note, decision or memory entry.
LINE = 120
INBOX_ROWS = 20
GOOD_ENDS = frozenset({"done", "waiting_user", "continued"})  # A turn that took its inbox and answered.

# The Claude Code tools the lead has itself: it reads, it does not change anything or run commands (the work is the
# threads'). backends/claude/argv.py turns ``options["tools"]`` into --tools.
LEAD_TOOLS = ("Read", "Grep", "Glob")

REPORT, DETAIL, SPAWN = "report", "detail", "spawn"
STYLE_CHOICES = {REPORT: ("ask_only", "milestones", "each_thread", "verbose"), DETAIL: ("一行", "简", "全"),
                 SPAWN: ("保守", "正常", "积极")}
STYLE_DEFAULT = {REPORT: "milestones", DETAIL: "简", SPAWN: "正常"}
SPAWN_LIMIT = {"保守": 1, "正常": 2, "积极": 4}  # Member topics at the same time (projects.MAX_MEMBERS caps them all).
REPORT_WORDS = {"ask_only": "只在需要主人定的时候说", "milestones": "只报里程碑和完成", "each_thread": "每个话题结束都报一句",
                "verbose": "详细，进展都报"}
# Which of a lead's report notes reach the owner under each ``report`` (phase 2's P2-8 delivers them; the filter is here).
REPORT_KINDS = {"ask_only": (), "milestones": ("milestone", "finish"), "each_thread": ("milestone", "finish", "thread"),
                "verbose": ("milestone", "finish", "thread", "note")}


def style_of(record) -> dict:
    """The record's style with each missing or invalid choice at its default."""
    given = record.get("style") if isinstance(record.get("style"), dict) else {}
    return {name: given[name] if given.get(name) in choices else STYLE_DEFAULT[name]
            for name, choices in STYLE_CHOICES.items()}


STYLE_FILE = "project-style.json"  # <state>/: the style last set on any project (COVEY_PROJECT_STYLE_KEEP=1).


def last_style(state_dir) -> dict:
    """The style ``keep_style`` saved last, each choice valid ({} when there is none): what a new project starts with."""
    saved = read_object(Path(state_dir) / STYLE_FILE)
    return {name: saved[name] for name, choices in STYLE_CHOICES.items() if saved.get(name) in choices}


def keep_style(state_dir, style) -> None:
    """Keep ``style`` (a record's, valid choices only) as the one a new project starts with."""
    write_json(Path(state_dir) / STYLE_FILE, {name: style[name] for name, choices in STYLE_CHOICES.items() if style.get(name) in choices},
               ensure_ascii=False, indent=1)


def reportable(style, kind) -> bool:
    """Whether a report note of ``kind`` (milestone, finish, thread, note) is for the owner under ``style`` (a record's style)."""
    return kind in REPORT_KINDS.get(style_of({"style": style})[REPORT], ())


def now_of(now=None) -> float:
    return time.time() if now is None else now


# The rules.

IDENTITY = (
    "[负责人规则]",
    "- 你是一个专项的负责人：主人交给你一件要分成几个话题、跨几天才能做完的事，你拆、派、跟进、收尾，不自己动手干活。"
    "你的 Claude Code 工具只有 Read、Grep、Glob，只用来读文件；改东西、跑命令、联网都是专项的话题里做的事。",
    "- 每一轮开头有“专项状态”：目标和完成标准、话题清单、专项记忆、风格、自上次以来的收件箱、近 10 条决定。"
    "它是磁盘上的真实状态，比你对之前几轮的印象可靠；对不上时以它为准。",
    "- 一轮只做这一轮该做的判断，派完就结束这一轮，不要等话题做完，不要轮询：话题有了结果，会有下一轮叫醒你。",
)
TOOL_RULES = (
    "- 工具都是 Bridge 工具（mcp__bridge__<工具名>）。开话题用 project_topic_start（写干净的任务说明：做什么、范围、约束、"
    "交付什么；不抄本机路径），给已有话题加话用 project_topic_send；同时最多 4 个话题在做（排队和在跑的；等主人回答的不占名额），风格里的“派活”档更低时按那档。",
    "- 每次有意义的判断或进展，用 project_note 记一句（专项卡上显示）；要告诉主人的才标 report，别每步都报；"
    "定下的决定标 decision，之后每一轮都会看到最近 10 条。",
    "- 主人说出这个专项里长期适用的做法、偏好或坑，用 project_remember 记下；主人说“少打扰我”“多报一点”“多开几个话题”这类，"
    "用 project_style 改汇报风格。",
    "- 目标和完成标准都达到、话题都结束了，才用 project_finish 收尾；还有话题在跑时它会拒绝。主人要停专项里的某个话题（包括停掉"
    "一个还在等主人回答的话题），用 project_topic_stop：它停掉那个话题排队和在跑的任务、收掉它等主人回答的问题，话题仍记在专项里、算结束。",
    "- 部署、发布、对外发送、花钱、删东西、不可逆的操作，你没有工具，也不要替主人决定：这些在话题里做，话题会照旧问主人。"
    "话题里问到主人的选择题，不由你答（分流还没开）。",
    "- 对主人说话要克制：这一轮的回复只有主人在场触发的轮才会发给他；叫醒你的轮，回复不发，要说的话用 project_note 并标 report。",
)
NOT_YET = "话题里问到主人的选择题，不由你答（分流还没开）。"  # The tail of a TOOL_RULES line while COVEY_PROJECT_TRIAGE is off.
TRIAGE_RULES = (
    "- 分流：专项的话题问了选择题，会先交给你（“专项状态”里“交给你的话题问题”，收件箱里有 question 回执），3 分钟内你不处理就转给主人。"
    "只替主人答你能依据专项记忆、决定日志和主人在这个专项里说过的话答的题：用 project_answer，每题都答，答案写选项原文，多选给数组；"
    "回答会注明是你替主人答的，记在专项卡上，主人可以点“不对”纠正。",
    "- 拿不准、记忆里没有依据、涉及部署、发布、对外发送、花钱、删东西、不可逆的操作，都用 project_escalate 转给主人并写清 why，别猜；"
    "自由文字的问题不会交给你。主人纠正过的做法，用 project_remember 记下，之后照做。",
)
PRACTICE = ""  # Phase 3's layer: how this kind of specialist works (project.leader.profile). Nothing reads it yet.


def style_rule(style) -> str:
    return (f"- 汇报风格：{REPORT_WORDS[style[REPORT]]}；说明的详细程度“{style[DETAIL]}”；派活“{style[SPAWN]}”"
            f"（同时最多 {min(projects.MAX_MEMBERS, SPAWN_LIMIT[style[SPAWN]])} 个话题）。")


def lead_rules(style=None, practice=PRACTICE) -> str:
    """The lead's rules: identity, tools, the style as the record has it, the practice layer (``practice``; none by
    default), the language of tool notes."""
    tools = TOOL_RULES if not triage.enabled() else (*(rule.replace(NOT_YET, "") for rule in TOOL_RULES), *TRIAGE_RULES)
    layers = [*IDENTITY, *tools, style_rule(style_of({"style": style})) if style else style_rule(STYLE_DEFAULT)]
    if practice:
        layers.append(practice)
    layers.append("- " + TOOL_NOTES_LANGUAGE)
    return "\n".join(layers)


# The record's lists.

def entry(text, **fields) -> dict:
    return {"id": secrets.token_hex(3), "at": time.time(), "text": " ".join(str(text or "").split())[:TEXT], **fields}


def append(state_dir, project_id, field, item, kept) -> dict:
    """``item`` at the end of the record's list ``field`` (the oldest dropped past ``kept``); the record."""
    def change(record):
        rows = record.get(field)
        rows = record[field] = rows if isinstance(rows, list) else []
        rows.append(item)
        del rows[:-kept]
    return projects.update(state_dir, project_id, change)


def library_index(state_dir, project_id) -> list:
    """The project's library (the files the owner gave it and what its topics delivered) as index lines, at most 2 KB
    (core/project_library.py): none without COVEY_PROJECT_MEMORY=1."""
    if not projects.memory_enabled():
        return []
    labels = {topic: topic_label(state_dir, topic) or topic[:8] for topic in {row["topic_id"] for row in project_library.entries(state_dir, project_id)} if topic}
    return project_library.index_lines(state_dir, project_id, labels=labels)


# The inbox.

def inbox_dir(state_dir, project_id) -> Path:
    return projects.folder(state_dir) / project_id / "inbox"


def add_receipt(state_dir, project_id, kind, text, thread="", at=None) -> dict:
    """One receipt in the inbox (what a member's turn did, a question held, what the owner said in a member topic): a file of
    its own, named in the order they came, so writers never share one."""
    stamp = now_of(at)
    item = {"id": f"{int(stamp * 1000):013d}-{secrets.token_hex(2)}", "kind": str(kind)[:20], "thread": str(thread or ""),
            "text": " ".join(str(text or "").split())[:500], "at": stamp, "taken_by": ""}
    write_json(inbox_dir(state_dir, project_id) / (item["id"] + ".json"), item, ensure_ascii=False, indent=1)
    return item


def took_it(state_dir, taken_by, key) -> bool:
    """Whether the receipts job ``taken_by`` took are still the lead's to read for job ``key``: its own, or those of a turn
    that did not end well (the receipts it was given may never have been read)."""
    if not taken_by or taken_by == key:
        return True
    status = (read_job_record(job_path(state_dir, taken_by)) or {}).get("status")
    return status not in GOOD_ENDS and status not in RUNNING


def take_receipts(state_dir, project_id, key, limit=INBOX_ROWS) -> list:
    """The receipts the lead's turn ``key`` has not read, oldest first, marked as taken by it (a second call of the same
    turn gives them again)."""
    found = []
    try:
        paths = sorted(inbox_dir(state_dir, project_id).glob("*.json"))
    except OSError:
        return found
    for path in paths:
        if len(found) >= limit:
            break
        with resource_lock("record", path):
            item = read_object(path)
            if not item.get("id") or not took_it(state_dir, item.get("taken_by"), key):
                continue
            if item.get("taken_by") != key:
                item["taken_by"] = key
                write_json(path, item, ensure_ascii=False, indent=1)
        found.append(item)
    return found


# The pack.

def clock(seconds) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(seconds)) if seconds else "—"


def last_line(text) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def member_row(state_dir, topic_id) -> dict:
    """One member topic as the pack's table has it: label, title, status words, one sentence, deliverables, whether a turn runs."""
    records = sorted(topic_job_records(state_dir, topic_id), key=lambda r: int(r.get("job_id") or 0))
    latest = records[-1] if records else {}
    status = str(latest.get("status") or "")
    if status in RUNNING:
        phase = str(latest.get("phase") or "")
        status_words = "在做"
        said = phase if phase and phase not in UNSET_PHASES else str(latest.get("user_text") or "").split("\n", 1)[0]
    elif status == "waiting_user":
        status_words, said = "等主人回答", last_line(latest.get("status_message"))
    elif status in ENDED or status == "continued":
        status_words, said = RESULTS.get(status, "已结束"), last_line(latest.get("final_text"))
    else:
        status_words, said = ("还没开工" if latest else "没有任务"), ""
    path = str(latest.get("checkpoint_path") or "")
    artifacts = (read_checkpoint(Path(path)) if path else {}).get("artifacts")
    artifacts = [str(item) for item in artifacts if item] if isinstance(artifacts, list) else []
    return {"topic_id": topic_id, "label": topic_label(state_dir, topic_id) or topic_id[:8],
            "title": topic_title(state_dir, topic_id) or "（没有标题）", "status": status_words,
            "said": cut(said, LINE), "artifacts": [cut(item, LINE) for item in artifacts[:3]],
            "live": status in RUNNING or status == "waiting_user", "waiting": status == "waiting_user"}


def member_rows(state_dir, record) -> list:
    return [member_row(state_dir, topic_id) for topic_id in record.get("threads") or () if isinstance(topic_id, str)]


def live_members(state_dir, record) -> list:
    """The labels of the members with a turn running or waiting for the owner."""
    return [row["label"] for row in member_rows(state_dir, record) if row["live"]]


def busy_members(state_dir, record) -> list:
    """The labels of the members with a turn queued or running: one waiting for the owner's answer holds no place (the
    spawn limit counts the turns that work)."""
    return [row["label"] for row in member_rows(state_dir, record) if row["live"] and not row["waiting"]]


def thread_line(row) -> str:
    parts = [f"{row['label']} · {row['title'][:40]} · {row['status']}"]
    if row["said"]:
        parts.append(row["said"])
    if row["artifacts"]:
        parts.append("交付：" + "；".join(row["artifacts"]))
    return "- " + " · ".join(parts)


def receipt_line(item) -> str:
    return f"- {clock(item.get('at'))} {item.get('kind') or '回执'}" + (
        f"（{topic_label_of(item)}）" if item.get("thread") else "") + f"：{item.get('text') or ''}"


def topic_label_of(item) -> str:
    return str(item.get("label") or str(item.get("thread"))[:8])


def state_pack(state_dir, record, key, *, full, receipts=None) -> str:
    """The pack at the head of the lead's turn ``key``. ``full``: the whole of it (a new session); else the parts that
    change between turns (members, style, inbox, decisions), the goal and the memory being in the session already.
    ``receipts``: the inbox's items when the caller has taken them."""
    style = style_of(record)
    if receipts is None:
        receipts = take_receipts(state_dir, record["id"], key)
    out = [f"[专项状态：{record.get('name') or record['id']}]"]
    if full:
        out.append("目标和完成标准：\n" + (record.get("goal") or "（主人没有写目标：第一轮先问清楚，或按专项名推断后用 project_note 记下）"))
    rows = member_rows(state_dir, record)
    out.append("话题（" + (f"{len(rows)} 个，{sum(1 for row in rows if row['live'])} 个没结束" if rows else "还没有") + "）："
               + ("\n" + "\n".join(map(thread_line, rows)) if rows else ""))
    if full:
        memory = project_memory.live(state_dir, record, style=False)
        if memory:
            out.append("专项记忆：\n" + "\n".join(f"- [{row['kind']}] {row['text']}" for row in memory[-20:]))
        struck = project_memory.tombstones(state_dir, record)
        if struck:
            out.append("主人删掉的记忆（近 30 天，不要再记回去）：\n" + "\n".join(f"- [{row['kind']}] {row['text']}" for row in struck[-10:]))
        library = library_index(state_dir, record["id"])
        if library:
            out.append("资料库：\n" + "\n".join(f"- {line}" for line in library))
    held = pack_block(state_dir, record)  # COVEY_PROJECT_TRIAGE=1: the questions held for the lead, else nothing.
    if held:
        out.append(held)
    out.append("风格：" + "；".join(f"{name}={value}" for name, value in style.items()))
    decisions = [row for row in record.get("decisions") or () if isinstance(row, dict) and row.get("text")][-DECISIONS:]
    if decisions:
        out.append(f"近 {len(decisions)} 条决定：\n" + "\n".join(f"- {clock(row.get('at'))} {row['text']}" for row in decisions))
    out.append("自上次唤醒以来的收件箱：" + ("\n" + "\n".join(map(receipt_line, receipts)) if receipts else "没有新的。"))
    return "\n\n".join(out)


# The session.

def previous_turn(state_dir, topic_id, key):
    """The lead topic's last turn before ``key`` that started (its job record), or None."""
    found = [record for record in topic_job_records(state_dir, topic_id)
             if record.get("key") != key and record.get("status") not in {"waiting_workspace"}
             and (record.get("status") in ENDED or record.get("status") in {"waiting_user"} or record.get("session_id"))]
    return max(found, key=lambda record: int(record.get("job_id") or 0), default=None)


def rebuild_reason(state_dir, topic_id, key, model, now=None) -> str:
    """Why the lead's session must be built again ('' when it may go on): the previous turn read over RESET_TOKENS, the
    topic was idle over RESET_IDLE, or the model is not the previous turn's."""
    last = previous_turn(state_dir, topic_id, key)
    if last is None:
        return ""
    tokens = last.get("input_tokens")
    tokens = tokens if type(tokens) is int else last.get("context_tokens")
    if type(tokens) is int and tokens > RESET_TOKENS:
        return f"上一轮读了 {tokens} token"
    ended = board_read.ended_at(last)
    if ended and now_of(now) - ended > RESET_IDLE:
        return "空闲超过 24 小时"
    before = str(last.get("model") or "")
    if before and model and before != model:
        return f"换了模型（{before} → {model}）"
    return ""


def settle_session(ctx, job, sessions, runtime, revision, session_id, now=None):
    """(revision, session_id) a lead's turn starts with (app/worker.py ``_start_turn``): the pointer cleared when
    ``rebuild_reason`` says so, so the turn starts a new session and the pack goes in whole. Other topics, and a lead
    topic with the switch off, are not touched."""
    if not session_id or not butler.is_lead(job.topic_id, job.chat_id):
        return revision, session_id
    reason = rebuild_reason(ctx.state_dir, job.topic_id, job_key(job.message_id, job.job_id), job.model or ctx.model, now)
    if not reason:
        return revision, session_id
    with runtime.session_lock:
        cleared = sessions.clear_session(ctx, revision, session_id)
    runtime.log(f"lead session rebuilt job=#{job.job_id} topic={job.topic_id[:12]}: {reason}")
    return sessions.capture_session(ctx) if cleared else (revision, session_id)


# The turn.

def lead_turn(ctx, job, prompt, session_id, log, release, now=None):
    """(context, message) of a lead's turn: the Bridge's tools of the lead ,
    the built-in tools narrowed to LEAD_TOOLS, and the message headed by the rules and the state pack. A turn whose
    project record is gone (``read_record``) goes on as the butler's would not: no tools, the message as it is."""
    found = projects.topic_project(ctx.state_dir, job.topic_id)
    record = projects.read_record(ctx.state_dir, found.id) if found is not None else {}
    if not record:
        log(f"lead turn without a project record job=#{job.job_id}")
        return ctx, prompt
    key = job_key(job.message_id, job.job_id)
    config = butler.mcp_config(release, ctx.state_dir, key, sys.executable, lead=record["id"])
    ctx = dataclasses.replace(ctx, options={**ctx.options, "mcp_config": config, "tools": LEAD_TOOLS})
    if job.kind != "chat":
        return ctx, prompt
    try:
        pack = state_pack(ctx.state_dir, record, key, full=not session_id)
    except Exception as exc:  # noqa: BLE001 - the turn goes on with the rules alone
        log(f"lead state pack unavailable job=#{job.job_id}: {type(exc).__name__}")
        pack = "[专项状态暂时读不到：先用 topic_read 看话题]"
    return ctx, "\n\n".join((lead_rules(record.get("style")), pack, prompt))


# What the turn did.

def outcome_line(state_dir, key) -> str:
    """The lead's turn ``key`` in a line when it opened or handed on to no thread (app/butler_reply.py outcome_words says
    those): the note it recorded or the project it finished; '' with neither."""
    job = read_job_record(job_path(state_dir, key)) or {}
    found = projects.topic_project(state_dir, str(job.get("topic_id") or ""))
    record = projects.read_record(state_dir, found.id) if found is not None else {}
    if record.get("state") == projects.DONE and record.get("state_job") == key:
        return "🏁 专项完成：" + cut(str(record.get("state_reason") or ""), 30)
    notes = [row for row in record.get("notes") or () if isinstance(row, dict) and row.get("job") == key]
    return "📌 记进展：" + cut(str(notes[-1].get("text") or ""), 30) if notes else ""
