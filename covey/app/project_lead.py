"""The lead's runs (docs/architecture.md 专项, phase 2, COVEY_PROJECTS=1): what a project's lead topic (the 负责人) reads and
how its session lives. Only a lead topic's turn (core/butler.py ``is_lead``) comes here; with the switch off nothing does.

- ``state_pack``: the project's state read from disk, put in front of every turn: the goal, the members (status, one
  sentence, deliverables), the project's memory and library, the style, the inbox since its last wake-up and the last ten
  decisions. The session is only a cache of it: a new one gets the whole pack, a resumed one the changing parts. The memory
  is in both, read as a member topic reads it (core/memory.py project_view: the same parts, choice, version), a resumed one
  with what was voided since the lead's previous turn, so a delete is gone at the next turn's start.
- ``lead_rules``: the lead's rules in layers (identity, tools, style, and the practice layer phase 3 fills for a
  specialist: nothing now).
- ``settle_session`` (app/worker.py ``_start_turn``): the session is dropped, and the pack sent again, when the previous
  turn's conversation ended over ``RESET_TOKENS``, the topic has been idle ``RESET_IDLE`` seconds, or the model changed.
- ``lead_turn`` (app/butler.py ``butler_turn``): the turn's context (its Bridge tools, the turn held to reading on
  whichever Harness it runs: LEAD, ``held``) and message (rules, pack, then the turn's own message).
- The inbox, ``projects/<id>/inbox/<n>.json`` ({id, kind, thread, text, at, taken_by}): receipts of what happened since
  the lead last looked (a member topic's turn ended, a question held, the owner spoke in a member topic). ``add_receipt`` writes
  one (the Bridge does, app/project_loop.py: a member's turn ended, the owner spoke in a member topic, nothing moved), ``take_receipts`` gives the lead's turn what it has not read.
- With 协作 on (COVEY_COLLAB=1, docs/projects.md 负责人对照目标跟进) the lead answers for the project's goal: its rules say so
  (``COLLAB_RULES``), and its pack (the consult's ``read_only_pack`` too) shows the 简报 (``brief_block``: the background, once
  in a new session, and the actions a line each, core/project_actions.py), the 目标检查表 and the 协作 gaps not dealt
  with (``checklist_block``, core/project_checklist.py), the memory's lines with their ids and the owner's own words in the
  project (the grounds project_answer names, app/triage.py grounds), and the inbox in groups (``inbox_block``: what to look
  at first, the topics' deliveries, the consults' receipts folded). Off, rules and pack are as they were.

Rules 1 and 6 (AGENTS.md): the records are written under their own lock and nothing here sends to Feishu."""
from __future__ import annotations

import dataclasses
import re
import secrets
import sys
import time
from pathlib import Path

from covey.app import project_approvals, project_context
from covey.app.prompts import project_memory_lines
from covey.app.triage_view import pack_block
from covey.core import board_read, butler, collab, devices, project_actions, project_checklist, project_library, project_memory, projects, triage
from covey.core import memory as core_memory
from covey.core import turn_source
from covey.core.checkpoint import (ASK_SHORT, checked, checkpoint_delivery, delivery_commits, read_checkpoint, short_commit,
                                   turn_summary)
from covey.core.harness import CONSULT, LEAD, PROXY, READ, TOOL_LIMIT
from covey.core.jobs import job_key, job_path, read_job_record, topic_job_records
from covey.core.locks import resource_lock
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.statuses import ENDED, RUNNING
from covey.core.text import cut, last_line, stamp
from covey.core.topics import topic_file, topic_label, topic_title
from covey.core.turn_words import RESULTS, UNSET_PHASES
from covey.core.web_sync import PREFIX as WEB_PREFIX

# A previous turn whose conversation ended over this (its context_tokens): the next starts a new session. The leads' on
# one day (T776) ended between 99K and 316K; 300K lets all but the longest go on and read the cache.
RESET_TOKENS = 300_000
RESET_IDLE = 24 * 3600  # Idle this long: the cached prompt is long gone, a new session costs no more.
DECISIONS = 10  # Entries of the decision log in the pack.
NOTES_KEPT, DECISIONS_KEPT = 100, 50  # What the record keeps of each list (the memory is core/project_memory.py's).
TEXT = 200  # Characters of one note, decision or memory entry.
LINE = 120
INBOX_ROWS = 20
GOOD_ENDS = frozenset({"done", "waiting_user", "continued"})  # A turn that took its inbox and answered.

# What the lead may do itself, on any Harness: read and search, and its Bridge tools; it changes nothing and runs no command
# (the work is the threads'). Its turn is held to LEAD (core/harness.py): Claude Code gets Read, Grep and Glob, Codex its
# read-only sandbox with the Bridge's tools approved, Antigravity deny rules for file writes, commands and URL reads.
HELD_BY = (CONSULT, PROXY, READ)  # Stricter limits a turn may have already (app/tool_limits.py): they stay.

# A project has no limit on its member topics at the same time, and its style no 派活 (deploys
# that would clash queue, the topics are not held back). An older record's or project-style.json's ``spawn`` is read past.
REPORT, DETAIL = "report", "detail"
STYLE_CHOICES = {REPORT: ("ask_only", "milestones", "each_thread", "verbose"), DETAIL: ("一行", "简", "全")}
STYLE_DEFAULT = {REPORT: "milestones", DETAIL: "简"}
REPORT_WORDS = {"ask_only": "只在需要主人定的时候说", "milestones": "只报里程碑和完成", "each_thread": "每个话题结束都报一句",
                "verbose": "详细，进展都报"}
# Which of a lead's report notes reach the owner under each ``report`` (app/project_loop.py report_out sends their words; the
# filter is here). What the owner is to decide is a question card of project_ask (WAVE12 C4), under every style: no note kind.
# ``ask`` was W0-2's note kind for it; app/project_tools.py project_note asks it with project_ask one release more.
ASK = "ask"
REPORT_KINDS = {"ask_only": (), "milestones": ("milestone", "finish"), "each_thread": ("milestone", "finish", "thread"),
                "verbose": ("milestone", "finish", "thread", "note")}


def style_of(record) -> dict:
    """The record's style with each missing or invalid choice at its default."""
    given = record.get("style") if isinstance(record.get("style"), dict) else {}
    return {name: given[name] if given.get(name) in choices else STYLE_DEFAULT[name]
            for name, choices in STYLE_CHOICES.items()}


STYLE_FILE = "project-style.json"  # <state>/: the style last set on any project, the next project's start.


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
    "你这一轮是只读的：只能读文件、搜索和用 Bridge 工具，改文件、执行命令、联网都会被拒绝；改东西、跑命令、联网都是专项的话题里做的事。",
    "- 每一轮开头有“专项状态”：目标和完成标准、话题清单、专项记忆、风格、自上次以来的收件箱、近 10 条决定。"
    "它是磁盘上的真实状态，比你对之前几轮的印象可靠；对不上时以它为准。专项记忆每一轮都带，和专项的话题读到的是同一份，"
    "写着作废的那几条不再适用。",
    "- 一轮只做这一轮该做的判断，派完就结束这一轮，不要等话题做完，不要轮询：话题有了结果，会有下一轮叫醒你。",
)
# Without COVEY_PROJECT_AUTOWAKE=1 a topic's end wakes the lead no more (app/project_loop.py autowake_due): IDENTITY says so
# (T647 P0-6: it promised the next turn, and the lead told the owner it would follow up).
WOKEN = "话题有了结果，会有下一轮叫醒你。"
NOT_WOKEN = ("话题有了结果不会叫醒你（没开叫醒负责人）：它的回执留在收件箱，主人下次找你时一起读；"
             "别对主人说“我会跟进”“有结果我再告诉你”。")
assert any(WOKEN in rule for rule in IDENTITY)
TOOL_RULES = (
    "- 下面说的工具都是 Bridge 工具（MCP 服务 bridge 的工具，名字是 mcp__bridge__<工具名> 或 mcp_bridge_<工具名>）。开话题用 project_topic_start（写干净的任务说明：做什么、范围、约束、"
    "交付什么；不抄本机路径；什么时候问主人话题自己知道，不用写“严禁发问题卡”“别问主人”），给已有话题加话用 project_topic_send；同时在做的话题不设上限，一件独立的事一个话题。",
    "- 主人点名要比较几种做法时：用 project_topic_start 开两个只读话题（readonly 为 true），给一模一样的任务说明，最好一家一个"
    "模型（harness、model）；两边都结束后再汇总，写清采纳了哪边的哪部分、为什么，以及两边都没回答的问题。",
    "- 派活前先看已有的话题，能追加就追加：先看“专项状态”的话题清单，再用 topics_list、topics_search 看主人私聊里还没进专项的话题。"
    "同一件事、同一块代码、同一个问题已经有话题在做或做过，就用 project_topic_send 追加给它（不在专项里的普通话题会顺带收进来）；"
    "确实独立的事才用 project_topic_start 新开。",
    "- 每次有意义的判断或进展，用 project_note 记一句（专项卡上显示）；要告诉主人的才标 report（Bridge 按汇报风格把这句原文发给他），"
    "别每步都报；定下的决定标 decision，之后每一轮都会看到最近 10 条。",
    "- 要主人定的事（范围、取舍、部署这一批、要不要停）用 project_ask 问：每题写 kind，选项写清，一次最多 4 题，"
    "你之前问了还没答的会并进同一张卡；这一轮结束时 Bridge 发问题卡，任何汇报风格都发，主人答了会接着叫你。不要只在回复里问。",
    "- 专项的目标和完成标准用 project_goal 写成给主人看的一两句（网页上专项标题下显示）：第一轮写，主人改了要求再更新；只写目标，不写进度。",
    "- 主人说出这个专项里长期适用的做法、偏好或坑，用 project_remember 记下：kind 写 rule（规矩，以后照做的）或 note（资料，供参考的事实），"
    "title 写一句不超过 40 字的标题；你记的规矩会进待审核，主人确认后才算他定的。主人答过的题 Bridge 已记成决定，不用再记。"
    "主人说“少打扰我”“多报一点”这类，用 project_style 改汇报风格。",
    "- 专项记忆的口径变了（记错了、主人改了主意、两条说同一件事），用 project_memory_revise 改那一条（id 写那一条开头〔〕里的短码）：update 改正文，supersede 用新条"
    "（或 by 写已有一条的短码）替代旧条，retire 收进历史；不许再追加一条“XXX 作废”，旧条还会每轮带上。主人的长期约定和他留下的限制你不能改，"
    "要改就问主人。",
    "- 不要把发版号、现网第几代、集群瞬时余量、某一刻的进度这类快照写进专项记忆（project_remember 会拒绝），它们过后就过时：写 project_note；"
    "专项记忆只放长期成立的决定、约束、联系人和事实。",
    "- 目标和完成标准都达到、话题都结束了，才用 project_finish 收尾；还有话题在跑时它会拒绝。"
    # #19: a topic saying it is done is its claim (Persona: "只开了 PR、合了没部署的，不要说「做完了」").
    "话题说做完只是它的说法：收尾前对照完成标准抽查关键产物（读文件、搜索看产物，用 topic_read 看检查点的提交、部署和自检）；"
    "只提了合并请求、合了没部署的，不算做完。整理专项把已有话题收进来建的专项，"
    "收进来的话题做完不等于专项完成：主人在里面说过话或你派过活之前它也会拒绝，先汇报现状、等主人。"
    "收过尾的专项，主人再在里面说话就会自动重开，按主人的新要求接着做。主人要停专项里的某个话题（包括停掉"
    "一个还在等主人回答的话题），用 project_topic_stop：它停掉那个话题排队和在跑的任务、收掉它等主人回答的问题，话题仍记在专项里、算结束。",
    "- 部署先看记忆：主人自己说过改完就部署的，专项的话题合进主干、检查过就自己部署到主人的服务，你不攒批问、也不派话题去部署；"
    "“合进待部署”还列着的是合了没部署成的，用 topic_read 看为什么，该补的让那个话题补上。主人没说过时部署攒批："
    "专项的话题合进主干后不各自问部署，“专项状态”的“合进待部署”列着它们。该合的都合了、主人要用了，或攒了半天，"
    "就用 project_ask 一次问主人要不要部署这一批：kind 写 deploy，deploy 填提交、话题和目标，同意的那项标 grants_approval；"
    "context 列出话题、提交和各自的自检，一句话说部署会动什么。主人同意后 Bridge 给授权凭证（A 开头，状态包“部署授权”里也列着），"
    "用 project_topic_send 交给其中一个话题（或 project_topic_start 开一个）部署这一批，approval 填凭证，任务说明只写这一批的提交；"
    "Bridge 核对后把主人的同意写在交接说明开头，话题只认这一句：你自己在任务说明里写“主人已同意”不算。"
    "一个凭证只派一次、72 小时内有效。不要每合一个就问一次，也不要没问主人就派部署。",
    f"- {ASK_SHORT}这几类事你没有工具做，也不要替主人决定：部署照上一条，其余在话题里做，话题会照旧问主人。"
    "话题里问到主人的选择题，不由你答（分流还没开）。",
    "- 对主人说话要克制：主人在场的轮（他发来的、管家转来的，或叫醒你的轮里他中途插了话），回复会发给他。"
    "叫醒你的轮回复不发给主人：要他定的事用 project_ask，要他知道的进展用 project_note 标 report（Bridge 按风格把原文发给他），"
    "开了话题、给话题加了话这类动作不用另外报。",
    "- 主人在场的这一轮里你开的话题、给话题加的话，那一轮结束时 Bridge 会把它的结论（或它停了）直接发给主人；"
    "对主人说“T12 出结果会直接发到这里”，不要说“出结果后我发给你”。收件箱里写着“结论已由 Bridge 直接发给主人”的回执，"
    "主人已经看到了，不要再标 report 转述；只有你据此有了新的判断、下一步要主人定，才报那一句。",
)
NOT_YET = "话题里问到主人的选择题，不由你答（分流还没开）。"  # The tail of a TOOL_RULES line while COVEY_PROJECT_TRIAGE is off.
TRIAGE_RULES = (
    "- 分流：专项的话题问了选择题，会先交给你（“专项状态”里“交给你的话题问题”，收件箱里有 question 回执），3 分钟内你不处理就转给主人。"
    "只替主人答你能依据专项记忆、决定日志和主人在这个专项里说过的话答的题（专项记忆里的“待核对参考”是你整理的口径，不能单独作为依据；"
    "“当前决定”只管那道题问的范围，换了批次、目标或扩大范围的转给主人）：用 project_answer，每题都答，答案写选项原文，多选给数组；"
    "回答会注明是你替主人答的，记在专项卡的分流记录里，主人不同意会在那个话题里直接说。",
    f"- 拿不准、记忆里没有依据、涉及{ASK_SHORT}或别的撤不回的操作，都用 project_escalate 转给主人并写清 why，别猜；"
    "自由文字的问题不会交给你。主人纠正过的做法，用 project_remember 记下，之后照做。",
)
# With 协作 on (COVEY_COLLAB=1): the lead answers for the project's goal (docs/projects.md 负责人对照目标跟进), not for
# passing on what its topics said, and an answer it gives for the owner names its grounds (app/triage.py grounds).
COLLAB_RULES = (
    "- 目标检查表：用 project_checklist 把专项的完成标准拆成几条，每条写清由哪个话题交付、依赖哪几条、证据在哪、还缺什么；"
    "第一轮写，目标变了再改。“专项状态”里的“目标检查表”是它现在的样子。",
    "- 收到话题的交付（thread_end）或协作缺口（collab_gap）时，对照目标检查表查：交付有没有覆盖那条标准、证据够不够"
    "（读文件、用 topic_read 抽查）、两个结果有没有冲突、后面的条目要的前置结果有没有；有漏项就安排补齐：project_topic_send 让原话题返工，"
    "或 project_topic_start 开新话题，并用 project_checklist 写明缺口和由谁补。协作缺口处理完用 close_gaps 写怎么处理的。",
    "- 话题说做完只是它的说法：核对过证据才把那一条标 done（evidence 写提交号、产物路径或检查点里的自检）；还有没满足的条目或没处理的协作缺口时 "
    "project_finish 会拒绝。主人说不要了的才标 dropped，在 gap 里写主人怎么说的；需要主人定范围、顺序或取舍时，合成一个问题用 project_ask 问他。",
    "- 协作咨询的回执（collab_answered）只供你参考，不会叫醒你，汇总方会自己交付结论。向主人汇报只说新的东西，比如“3 条标准只覆盖了 2 条，"
    "缺原版基线对照，还不能收尾”；不要把话题或汇总方的结论换个说法再发一遍。",
    "- 简报：第一轮用 project_goal 的 background 写背景，再用 project_checklist 的 actions 把目标拆成 3–8 个动作，"
    "每个写清条目、话题和一句现状（note）；动作的状态由 Bridge 算，不用你写。开话题、给话题加话时带 action 或 item，主人在对话里要改简报就照改。",
)
EVIDENCE_RULE = (
    "- project_answer 必须写 evidence：列出你依据的专项记忆条目短码（状态包专项记忆每行开头〔〕里的）或主人在这个专项里的原话"
    "（msg:消息 id，见状态包“主人在专项里说过的话”）。只有长期约定、当前决定里没标（无出处）、仍有效的条目和主人原话算依据；"
    "待核对参考是你整理的，只能一起列，不能单独作依据；作废、做完、被替代或只限当次的 Bridge 会拒绝。被拒了就用 project_escalate 转给主人，"
    "不要换个说法再答。回答会带上依据，记在专项卡的分流记录里。"
)
PRACTICE = ""  # Phase 3's layer: how this kind of specialist works (project.leader.profile). Nothing reads it yet.


def style_rule(style) -> str:
    return f"- 汇报风格：{REPORT_WORDS[style[REPORT]]}；说明的详细程度“{style[DETAIL]}”。"


def lead_rules(style=None, practice=PRACTICE) -> str:
    """The lead's rules: identity (NOT_WOKEN for WOKEN without COVEY_PROJECT_AUTOWAKE=1), tools, the style as the record has it,
    the practice layer (``practice``; none by default). The language rule is the bridge rules' (app/prompts.py
    LANGUAGE_RULE), said there once. With 协作 on
    (COVEY_COLLAB=1) the lead's duty to the goal (``COLLAB_RULES``) follows the tools; with the triage, on or off 协作, the
    grounds an answer names (``EVIDENCE_RULE``, W0-2)."""
    tools = TOOL_RULES if not triage.enabled() else (*(rule.replace(NOT_YET, "") for rule in TOOL_RULES), *TRIAGE_RULES)
    if turn_source.enabled():
        tools = (*tools, *COLLAB_RULES)
    if triage.enabled():
        tools = (*tools, EVIDENCE_RULE)  # project_answer checks its grounds with 协作 on or off (W0-2).
    identity = IDENTITY if projects.autowake_enabled() else tuple(rule.replace(WOKEN, NOT_WOKEN) for rule in IDENTITY)
    layers = [*identity, *tools, style_rule(style_of({"style": style})) if style else style_rule(STYLE_DEFAULT)]
    if practice:
        layers.append(practice)
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


def add_receipt(state_dir, project_id, kind, text, thread="", at=None, relayed=False) -> dict:
    """One receipt in the inbox (what a member's turn did, a question held, what the owner said in a member topic): a file of
    its own, named in the order they came, so writers never share one. ``relayed``: the member turn's result went to the
    owner already (app/project_loop.py after_runner), kept only when true."""
    stamp = now_of(at)
    item = {"id": f"{int(stamp * 1000):013d}-{secrets.token_hex(2)}", "kind": str(kind)[:20], "thread": str(thread or ""),
            "text": " ".join(str(text or "").split())[:500], "at": stamp, "taken_by": "",
            **({"relayed": True} if relayed else {})}
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


def taken_receipts(state_dir, project_id, key) -> list:
    """The receipts the lead's turn ``key`` took (``take_receipts``), oldest first; reads only."""
    try:
        paths = sorted(inbox_dir(state_dir, project_id).glob("*.json"))
    except OSError:
        return []
    return [item for item in (read_object(path) for path in paths) if key and item.get("taken_by") == key]


def inbox_items(state_dir, project_id) -> list:
    """Every receipt of the project's inbox, read or not, oldest first: the 协作 gaps are read from them
    (core/project_checklist.py gaps). Reads only."""
    try:
        paths = sorted(inbox_dir(state_dir, project_id).glob("*.json"))
    except OSError:
        return []
    return [item for item in (read_object(path) for path in paths) if item.get("id")]


# The pack.

def member_row(state_dir, topic_id) -> dict:
    """One member topic as the pack's table has it: label, title, status words, one sentence (an ended turn's: its checkpoint's
    phase and delivery, else its reply's opening, core/checkpoint.py turn_summary), deliverables, whether a turn runs."""
    records = sorted(topic_job_records(state_dir, topic_id), key=lambda r: int(r.get("job_id") or 0))
    latest = records[-1] if records else {}
    status = str(latest.get("status") or "")
    path = str(latest.get("checkpoint_path") or "")
    checkpoint = read_checkpoint(Path(path)) if path else {}
    if status in RUNNING:
        phase = str(latest.get("phase") or "")
        status_words = "在做"
        said = phase if phase and phase not in UNSET_PHASES else str(latest.get("user_text") or "").split("\n", 1)[0]
    elif status == "waiting_user":
        status_words, said = "等主人回答", last_line(latest.get("status_message"))
    elif status in ENDED or status == "continued":
        status_words, said = RESULTS.get(status, "已结束"), turn_summary(latest, checkpoint, LINE)
    else:
        status_words, said = ("还没开工" if latest else "没有任务"), ""
    artifacts = checkpoint.get("artifacts")
    artifacts = [str(item) for item in artifacts if item] if isinstance(artifacts, list) else []
    return {"topic_id": topic_id, "label": topic_label(state_dir, topic_id) or topic_id[:8],
            "title": topic_title(state_dir, topic_id) or "（没有标题）", "status": status_words,
            "said": cut(said, LINE), "artifacts": [cut(item, LINE) for item in artifacts[:3]],
            "live": status in RUNNING or status == "waiting_user", "waiting": status == "waiting_user",
            "ended": status in ENDED or status == "continued"}


def member_rows(state_dir, record) -> list:
    return [member_row(state_dir, topic_id) for topic_id in record.get("threads") or () if isinstance(topic_id, str)]


def live_members(state_dir, record) -> list:
    """The labels of the members with a turn running or waiting for the owner."""
    return [row["label"] for row in member_rows(state_dir, record) if row["live"]]


# A delivery's ``deployed`` that says it was not deployed (an empty one says nothing at all).
NOT_DEPLOYED = re.compile(r"(?:未|没|没有|不|尚未|暂未|还没有?|待|等[^，。；,;]{0,12})\s*(?:部署|上线|发布)|not (?:yet )?deployed"
                          r"|^\s*(?:无|none|n/?a|-|—|否|no)\s*$", re.IGNORECASE)
DEPLOY_ROWS = 12  # The 合进待部署 lines a pack shows; the rest counted.


def deployed(delivery) -> bool:
    """Whether a checkpoint's ``delivery`` says its turn deployed something."""
    words = str((delivery or {}).get("deployed") or "").strip()
    return bool(words) and not NOT_DEPLOYED.search(words)


def waiting_deploys(state_dir, record) -> list:
    """部署攒批: the member topics' ended turns that merged commits (their checkpoint's ``delivery``, kept on the job record) and that no
    deploy came after, oldest first: [{label, commits, checked, ended}]. A turn whose delivery says it deployed covers every turn that ended
    before it started (a deploy takes the trunk as it is), whichever topic it was in."""
    rows, deploys = [], []
    for topic_id in record.get("threads") or ():
        if not isinstance(topic_id, str):
            continue
        label = topic_label(state_dir, topic_id) or topic_id[:8]
        for job in topic_job_records(state_dir, topic_id):
            if job.get("status") not in ENDED and job.get("status") != "continued":
                continue
            delivery = checkpoint_delivery({"delivery": job.get("delivery")})
            if not delivery:
                continue
            if deployed(delivery):
                deploys.append(epoch_seconds(job.get("created_at")) or 0)
            elif delivery_commits(delivery):
                rows.append({"label": label, "commits": delivery_commits(delivery), "checked": checked(delivery),
                             "ended": epoch_seconds(job.get("updated_at")) or 0})
    last = max(deploys, default=0)
    return sorted((row for row in rows if row["ended"] > last), key=lambda row: row["ended"])


def deploy_block(state_dir, record) -> str:
    """The pack's 合进待部署 (TOOL_RULES 部署先看记忆: the owner's own word to deploy what merges, else 部署攒批), '' when
    nothing waits."""
    rows = waiting_deploys(state_dir, record)
    if not rows:
        return ""
    lines = [f"- {row['label']} · " + " ".join(short_commit(c) for c in row["commits"][:4])
             + (f" 等 {len(row['commits'])} 个提交" if len(row["commits"]) > 4 else "")
             + f" · {'已自检' if row['checked'] else '未自检'} · {stamp(row['ended'])} 合进" for row in rows[-DEPLOY_ROWS:]]
    more = f"（另有更早的 {len(rows) - DEPLOY_ROWS} 轮没列）" if len(rows) > DEPLOY_ROWS else ""
    return (f"合进待部署（{len(rows)} 轮还没部署，怎么办见规则的“部署先看记忆”）{more}：\n" + "\n".join(lines))


def thread_line(row) -> str:
    parts = [f"{row['label']} · {row['title'][:40]} · {row['status']}"]
    if row["said"]:
        parts.append(row["said"])
    if row["artifacts"]:
        parts.append("交付：" + "；".join(row["artifacts"]))
    return "- " + " · ".join(parts)


RELAYED_NOTE = "（结论已由 Bridge 直接发给主人，不要再转述）"


def receipt_line(item) -> str:
    return f"- {stamp(item.get('at'))} {item.get('kind') or '回执'}" + (
        f"（{topic_label_of(item)}）" if item.get("thread") else "") + f"：{item.get('text') or ''}" + (
        RELAYED_NOTE if item.get("relayed") is True else "")


def topic_label_of(item) -> str:
    return str(item.get("label") or str(item.get("thread"))[:8])


def lead_goal(record) -> str:
    """The goal the lead wrote with project_goal (app/project_tools.py), '' before it did."""
    found = record.get("lead_goal") if isinstance(record.get("lead_goal"), dict) else {}
    return " ".join(str(found.get("text") or "").split())


def lead_background(record) -> str:
    """The background the lead wrote with project_goal's ``background`` (app/project_tools.py), '' before it did."""
    found = record.get("lead_background") if isinstance(record.get("lead_background"), dict) else {}
    return " ".join(str(found.get("text") or "").split())


LEAD_MEMORY_HEAD = "专项记忆（每一轮都带，和专项的话题读到的是同一份）："


# 协作 (COVEY_COLLAB=1): what the pack says besides, for the lead to answer for the goal and name its grounds.

CHECKLIST_EMPTY = "目标检查表：还没有。用 project_checklist 把完成标准拆成几条（每条：交付的话题、依赖、证据、缺口）。"
GAPS_HEAD = "没处理的协作缺口（处理完用 project_checklist 的 close_gaps 写怎么处理的；有它们时 project_finish 会拒绝）："
OWNER_WORDS_HEAD = "主人在专项里说过的话（最近几条，替主人答题时可以写 msg:消息 id 作依据）："
OWNER_PREFIXES = ("om_", WEB_PREFIX)  # The message ids of the owner's own words the Bridge took in: Feishu's, the web page's.
OWNER_WORDS_SHOWN = 5
OWNER_WORDS_TEXT = 100
FIRST_KINDS = (collab.RECEIPT_GAP, "question", "owner_choice", "start", "owner_said")  # The receipts the lead looks at first.
CONSULTS_SHOWN = 3  # The consults' receipts the inbox shows; the others are counted.


def entry_id(item) -> str:
    """The id of a project memory item (core/memory.py items: its ref is project:<project id>:<entry id>)."""
    parts = str(item.get("ref") or "").split(":", 2)
    return parts[2] if len(parts) == 3 else ""


def memory_lines(view, codes=None) -> list:
    """The memory's lines as the pack says them (app/prompts.py project_memory_lines): each carried line starts with its short
    code in 〔〕 (core/memstore.py; its entry id when ``codes``, entry id -> code, has none), what project_memory_revise takes
    and project_answer's evidence names (app/triage.py grounds)."""
    codes = codes or {}
    parts = {part: {**room, "kept": [{**item, "text": f"〔{codes.get(entry_id(item)) or entry_id(item)}〕{item['text']}"}
                                     for item in room["kept"]]}
             for part, room in view["parts"].items()}
    return project_memory_lines({**view, "parts": parts})


def memory_codes(state_dir, project_id) -> dict:
    """entry id -> short code of every line of the project's memory (core/memstore.py, a line without one gets it now); {} when
    they cannot be read (the pack says the ids then)."""
    from covey.core import memstore

    try:
        return {entry.ref.rsplit(":", 1)[-1]: entry.code
                for entry in memstore.entries(state_dir, scope="project", owner_key=project_id, statuses=None) if entry.code}
    except Exception:  # noqa: BLE001 - the ids then
        return {}


def owner_message(record) -> bool:
    """Whether the job ``record`` began with the owner's own words as the Bridge took them in (his Feishu message, his words on
    the web page) and of a source whose words are his (core/turn_source.py owner_words: no 协作 turn, no hand-on)."""
    return str(record.get("message_id") or "").startswith(OWNER_PREFIXES) and turn_source.behavior(record).owner_words


def owner_word_lines(state_dir, record) -> list:
    """The owner's own words in the lead's topic, and those the butler handed the lead (the project record's ``relayed``,
    app/butler_projects.py project_send, WAVE12 C2: his message in the butler's chat, his words), the newest first (at most
    OWNER_WORDS_SHOWN), each naming itself as project_answer's evidence does: msg:<message id>."""
    lead = str(record.get("lead_topic_id") or "")
    rows = [(epoch_seconds(job.get("created_at")) or 0, str(job["message_id"]), job.get("user_text"), "")
            for job in (topic_job_records(state_dir, lead) if lead else ()) if owner_message(job)]
    seen = {row[1] for row in rows}
    rows += [(float(item.get("at") or 0), str(item["message_id"]), item.get("text"), "（管家转来）")
             for item in record.get("relayed") or () if isinstance(item, dict) and item.get("message_id")
             and str(item["message_id"]).startswith(OWNER_PREFIXES) and item["message_id"] not in seen]
    rows.sort(key=lambda row: row[0], reverse=True)
    return [f"- msg:{message} · {stamp(at)} · {via}{cut(text, OWNER_WORDS_TEXT)}"
            for at, message, text, via in rows[:OWNER_WORDS_SHOWN]]


BRIEF_EMPTY = "还没写简报：project_goal 写背景，project_checklist 的 actions 拆 3–8 个动作"
BRIEF_BACKGROUND = 120  # Characters of the background the pack says, once, in a new session.
BRIEF_UNMET = 5  # Items an action's line names as not met; the others are counted.
BRIEF_UNMAPPED = 8  # Topics the pack names as on no item; the others are counted.


def topic_states(rows) -> dict:
    """Topic id -> what core/project_actions.py ``state`` reads of it, from ``member_rows``: its turn waits for the owner, runs
    (or queues), or neither."""
    return {row["topic_id"]: project_actions.WAITING if row["waiting"] else project_actions.RUNNING if row["live"]
            else project_actions.ENDED for row in rows}


def id_list(ids, shown) -> str:
    """``ids`` joined, at most ``shown`` of them, the rest counted."""
    return "、".join(ids[:shown]) + (f" 等 {len(ids)} 条" if len(ids) > shown else "")


def brief_block(state_dir, record, rows, *, full, detail=False) -> str:
    """The 简报 as the pack says it (core/project_actions.py), short: the background (``full``: a new session only, cut to
    BRIEF_BACKGROUND), an action a line (id, title, what it reads as, the items not met, the lead's line; one that reads 做完 only
    that), the topics that are on no item. ``detail``: for the tool's answer, the whole background and each action with its
    items and topics. A record with no background and no action gets one line asking for them. ``rows``: ``member_rows``."""
    background, actions = lead_background(record), project_actions.items(record)
    if not background and not actions:
        return BRIEF_EMPTY
    entries = project_checklist.items(record)
    by_id = {item["id"]: item for item in entries}
    states = topic_states(rows)
    labels = {row["topic_id"]: row["label"] for row in rows}
    live = [action for action in actions if action["state"] != project_actions.DROPPED]
    dropped = len(actions) - len(live)
    out = [f"简报（{len(live)} 个动作{f'，另有 {dropped} 个不做了' if dropped else ''}）：" if actions else "简报："]
    if full and background:
        out.append("背景：" + (background if detail else cut(background, BRIEF_BACKGROUND)))
    elif full:
        out.append("背景：还没写（project_goal 的 background）")
    if not actions:
        out.append("动作：还没写（project_checklist 的 actions 拆 3–8 个）")
    for action in live:
        found = project_actions.state(action, entries, states)
        parts = [f"{action['id']} {action['title']}", project_actions.STATE_WORDS[found]]
        if found != project_actions.DONE:
            unmet = [item_id for item_id in action["items"] if item_id in by_id and by_id[item_id]["state"] not in project_checklist.MET]
            if unmet:
                parts.append("没满足 " + id_list(unmet, BRIEF_UNMET))
            if action["note"]:
                parts.append("现状：" + action["note"])
        if detail:
            parts.append("条目 " + ("、".join(action["items"]) or "没有"))
            parts.append("话题 " + ("、".join(labels.get(topic) or topic[:8] for topic in action["topics"]) or "没有"))
        out.append("- " + " · ".join(parts))
    loose = project_actions.unmapped(record)
    if loose:
        names = [labels.get(topic) or topic[:8] for topic in loose]
        out.append("还没挂到条目上的话题：" + "、".join(names[:BRIEF_UNMAPPED]) + (f" 等 {len(names)} 个" if len(names) > BRIEF_UNMAPPED else ""))
    return "\n".join(out)


def delivering(item) -> str:
    """The topic that delivers ``item``: its ``topic``, or, with none named, the first that took part (``topics``); '' with neither."""
    return (project_checklist.topics_of(item) or [""])[0]


def checklist_block(state_dir, record, rows, detail=False) -> str:
    """The 目标检查表 and the 协作 gaps not dealt with, as the pack says them (core/project_checklist.py): an item a line (its id,
    state, criterion, the topic that delivers it and how that topic is, what it waits on, the evidence, the gap); then what to
    look into: an item met while one it needs is not, an item not met whose topic has ended (its claim is to be checked
    against the criterion); then the open gaps. ``rows``: ``member_rows``. ``detail``: for the tool's answer, an item says the
    other topics on it too (``topics``), what a ``topics`` of a change replaces. The topic an item's line names is ``delivering``."""
    entries = project_checklist.items(record)
    out = []
    if entries:
        by_topic = {row["topic_id"]: row for row in rows}
        states = {item["id"]: item["state"] for item in entries}
        met = sum(1 for item in entries if item["state"] in project_checklist.MET)
        out.append(f"目标检查表（{len(entries)} 条：{met} 条满足或不做了，{len(entries) - met} 条没满足）：")
        for item in entries:
            topic = delivering(item)
            row = by_topic.get(topic)
            parts = [item["id"], project_checklist.STATE_WORDS[item["state"]], item["criterion"],
                     (f"{row['label'] if row else topic_label(state_dir, topic) or topic[:8]}"
                      f"（{row['status'] if row else '不在专项里了'}）") if topic else "还没有话题"]
            others = [each for each in project_checklist.topics_of(item) if each != topic]
            if detail and others:
                parts.append("参与的话题 " + "、".join(by_topic[each]["label"] if each in by_topic else topic_label(state_dir, each) or each[:8]
                                                    for each in others))
            if item["needs"]:
                parts.append("依赖 " + "、".join(f"{need}（{project_checklist.STATE_WORDS.get(states.get(need), '不在了')}）"
                                               for need in item["needs"]))
            parts += [f"{name}：{item[key]}" for key, name in (("evidence", "证据"), ("gap", "缺口")) if item[key]]
            out.append("- " + " · ".join(parts))
        for item, waiting in project_checklist.conflicts(record):
            out.append(f"⚠️ {item['id']} 标了{project_checklist.STATE_WORDS[item['state']]}，但它依赖的 {'、'.join(waiting)} "
                       "还没满足：核对是不是冲突，或漏了前置结果。")
        for item in entries:
            row = by_topic.get(delivering(item))
            if item["state"] not in project_checklist.MET and row and row["ended"]:
                out.append(f"⚠️ {row['label']} 已经结束（{row['status']}），它交付的 {item['id']} 还没满足："
                           "对照这条标准核对它的交付和证据，缺什么就让它返工。")
    else:
        out.append(CHECKLIST_EMPTY)
    open_gaps = project_checklist.gaps(record, inbox_items(state_dir, record["id"]))
    if open_gaps:
        out += [GAPS_HEAD, *(f"- {gap['id']} · {stamp(gap['at'])} · {cut(gap['text'], 200)}" for gap in open_gaps)]
    return "\n".join(out)


UNLOCKS_SHOWN = 4  # The items a delivery says it unlocks.


def unlock_words(entries, mine) -> str:
    """What the items ``mine`` (ids) not met yet would let begin, the items that wait on them directly (core/project_checklist.py
    unlocks): '；满足后 c15 可开始、c16 还在等 c4', or ''. Only the next step: what lies further on is the checklist's."""
    found = project_checklist.unlocks(entries, mine)
    words = [f"{item_id} " + (f"还在等 {'、'.join(waits)}" if waits else "可开始") for item_id, waits in found[:UNLOCKS_SHOWN]]
    if len(found) > UNLOCKS_SHOWN:
        words.append(f"另有 {len(found) - UNLOCKS_SHOWN} 条")
    return "；满足后 " + "、".join(words) if words else ""


def inbox_block(state_dir, receipts, record) -> str:
    """The inbox as the pack says it. With 协作 on (COVEY_COLLAB=1) in groups, each in the order it came: what to look at
    first (a 协作 gap, a question, the owner's choice or word, a start), the topics' deliveries (``thread_end``, with the
    items of the 目标检查表 a topic delivers that are not met yet), the rest, and last the consults' receipts, folded (the
    newest CONSULTS_SHOWN and a count: they wake nobody, and their integrator delivers); off, a line each as before."""
    head = "自上次唤醒以来的收件箱："
    if not receipts:
        return head + "没有新的。"
    if not turn_source.enabled():
        return head + "\n" + "\n".join(map(receipt_line, receipts))
    named = [{**item, "label": topic_label(state_dir, item["thread"]) or ""} if item.get("thread") and not item.get("label")
             else item for item in receipts]
    first = [item for item in named if item.get("kind") in FIRST_KINDS]
    ends = [item for item in named if item.get("kind") == "thread_end"]
    consults = [item for item in named if item.get("kind") == collab.RECEIPT_ANSWERED]
    rest = [item for item in named if item not in first and item not in ends and item not in consults]
    entries = project_checklist.items(record)
    open_items = [item for item in entries if item["state"] not in project_checklist.MET]
    out = [head]
    if first:
        out += ["先看：", *(receipt_line(item).replace("- ", "- ⚠️ ", 1) if item.get("kind") == collab.RECEIPT_GAP
                            else receipt_line(item) for item in first)]
    if ends:
        out.append(f"话题的交付和结束（{len(ends)} 条，对照目标检查表看各覆盖了哪条）：")
        for item in ends:
            mine = [entry["id"] for entry in open_items if item.get("thread") in project_checklist.topics_of(entry)]
            out.append(receipt_line(item) + (f"（它交付的 {'、'.join(mine)} 还没满足{unlock_words(entries, mine)}）" if mine else ""))
    if rest:
        out += ["其他：", *map(receipt_line, rest)]
    if consults:
        out.append(f"协作咨询的回执（{len(consults)} 条，只供参考，不会叫醒你；汇总方会自己交付结论）：")
        out += [f"- {stamp(item.get('at'))} {cut(item.get('text'), 100)}" for item in consults[-CONSULTS_SHOWN:]]
        if len(consults) > CONSULTS_SHOWN:
            out.append(f"另有 {len(consults) - CONSULTS_SHOWN} 条更早的（要细节用 topic_read 看汇总方的话题）。")
    return "\n".join(out)


def device_workspace(state_dir, record) -> tuple:
    """(name, value) of the project's workspace when it is on a device (COVEY_DEVICES=1, ``gpu1:/home/me/proj``): the
    workspace of the topic the project grew from, else the one it was opened with, as the lead's own would be. Its topics
    work there; the lead does not (app/projects.py step_lead: on a device it would have none of its tools), and its pack
    says so (``state_pack``). ('', '') for a project anywhere else, or with the switch off (then nothing is read)."""
    if not devices.enabled():
        return "", ""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    source = read_object(topic_file(Path(state_dir), origin["topic_id"])) if origin.get("topic_id") else {}
    value = str(source.get("workdir") or origin.get("workdir") or "")
    if not devices.is_device_value(value):
        return "", ""
    return str(source.get("workspace_name") or origin.get("workspace") or ""), value


DEVICE_PLACE = ("工作区：专项的话题在设备上的工作区「{name}」（{value}）里做；你自己在 Bridge 本机（设备上用不了专项的工具），"
                "开专项的话题时 workspace 填「{name}」，Harness 用那台设备声明的。")


def state_pack(state_dir, record, key, *, full, receipts=None, since=None, redacted=False) -> str:
    """The pack at the head of the lead's turn ``key``. ``full``: the whole of it (a new session); else the parts that
    change between turns (members, memory, style, inbox, decisions), the goal being in the session already.
    ``receipts``: the inbox's items when the caller has taken them. The memory is core/memory.py project_view's, as a member
    topic reads it: ``since`` the lead's previous turn's start (a resumed session: what was voided since then is said),
    ``redacted`` whether the lead's topic reads the lines from topics with redaction on; with the triage on, the owner's own
    words (``owner_word_lines``: grounds project_answer may name, W0-2). With 协作 on (COVEY_COLLAB=1) also
    the 简报 (``brief_block``: its background in a new session only), the 目标检查表 and the open 协作 gaps (``checklist_block``)
    and the inbox in groups (``inbox_block``), in a new session and a resumed one."""
    style = style_of(record)
    if receipts is None:
        receipts = take_receipts(state_dir, record["id"], key)
    collab_on = turn_source.enabled()
    out = [f"[专项状态：{record.get('name') or record['id']}]"]
    if full:
        out.append("目标和完成标准：\n" + (record.get("goal") or "（主人没有写目标：第一轮先问清楚，或按专项名推断）")
                   + ("\n你写下的（project_goal）：" + lead_goal(record) if lead_goal(record) else ""))
    if not lead_goal(record):
        out.append("（还没用 project_goal 写专项的目标：这一轮写一句给主人看的，说清要做成什么、做到什么算完）")
    if full:
        name, value = device_workspace(state_dir, record)
        if value:
            out.append(DEVICE_PLACE.format(name=name or value, value=value))
    rows = member_rows(state_dir, record)
    out.append("话题（" + (f"{len(rows)} 个，{sum(1 for row in rows if row['live'])} 个没结束" if rows else "还没有") + "）："
               + ("\n" + "\n".join(map(thread_line, rows)) if rows else ""))
    if collab_on:
        out += [brief_block(state_dir, record, rows, full=full), checklist_block(state_dir, record, rows)]
    waiting = deploy_block(state_dir, record)
    if waiting:
        out.append(waiting)
    granted = project_approvals.active_lines(record)
    if granted:
        out.append("部署授权（主人在问题卡上同意的，派部署时用 approval 带上）：\n" + "\n".join(granted))
    rows = project_memory.entries(state_dir, record)
    view = core_memory.project_view(rows + project_context.decision_rows(state_dir, record["id"], rows), record["id"], redacted=redacted,
                                    since=None if full else since)
    memory = memory_lines(view, memory_codes(state_dir, record["id"]) if view["parts"] else None)
    if memory:
        out.append(LEAD_MEMORY_HEAD + "\n" + "\n".join(memory))
    words = owner_word_lines(state_dir, record) if triage.enabled() else []  # Grounds project_answer may name (W0-2).
    if words:
        out.append(OWNER_WORDS_HEAD + "\n" + "\n".join(words))
    if full:
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
        out.append(f"近 {len(decisions)} 条决定：\n" + "\n".join(f"- {stamp(row.get('at'))} {row['text']}" for row in decisions))
    reopened = [row for row in record.get("reopened") or () if isinstance(row, dict)]
    if reopened and record.get("state") == projects.ACTIVE:
        last = reopened[-1]
        out.append(f"重开：这个专项收过尾（{cut(str(last.get('summary') or ''), 120) or '没有写结论'}），主人 {stamp(last.get('at'))} "
                   "又在专项里说话，已重新打开：按主人的新要求接着做，做完再 project_finish。")
    out.append(inbox_block(state_dir, receipts, record))
    return "\n\n".join(out)


# The session.

def previous_turn(state_dir, topic_id, key):
    """The lead topic's last turn before ``key`` that started (its job record), or None."""
    found = [record for record in topic_job_records(state_dir, topic_id)
             if record.get("key") != key and record.get("status") not in {"waiting_workspace"}
             and (record.get("status") in ENDED or record.get("status") in {"waiting_user"} or record.get("session_id"))]
    return max(found, key=lambda record: int(record.get("job_id") or 0), default=None)


def rebuild_reason(state_dir, topic_id, key, model, now=None) -> str:
    """Why the lead's session must be built again ('' when it may go on): the previous turn ended with its conversation
    over RESET_TOKENS, the topic was idle over RESET_IDLE, or the model is not the previous turn's.

    The conversation's size is the previous turn's ``context_tokens`` (what its next request would start from), not its
    ``input_tokens``: those add up every request of the turn, each of which read the whole conversation again (T776: a
    lead turn of 41 steps on a 200K conversation had read 918K, and every turn of the lead started a new session, its
    first request reading nothing from the cache). A record without it (one from before the counts) falls back to them."""
    last = previous_turn(state_dir, topic_id, key)
    if last is None:
        return ""
    tokens = last.get("context_tokens")
    tokens = tokens if type(tokens) is int else last.get("input_tokens")
    if type(tokens) is int and tokens > RESET_TOKENS:
        return f"上一轮结束时对话有 {tokens} token"
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

def held(ctx):
    """``ctx`` held to LEAD (core/harness.py TOOL_LIMIT: reading, searching and the Bridge's tools, on whichever Harness the
    lead runs); a stricter limit the turn has already (``HELD_BY``) stays, a LOOK gives way."""
    if ctx.options.get(TOOL_LIMIT) in HELD_BY:
        return ctx
    return dataclasses.replace(ctx, options={**ctx.options, TOOL_LIMIT: LEAD})


def lead_turn(ctx, job, prompt, session_id, log, release, now=None):
    """(context, message) of a lead's turn: the Bridge's tools of the lead, the turn held to reading (``held``), and the
    message headed by the rules and the state pack. A turn whose project record is gone (``read_record``) goes on as the
    butler's would not: no tools, the message as it is, held to reading all the same."""
    found = projects.topic_project(ctx.state_dir, job.topic_id)
    record = projects.read_record(ctx.state_dir, found.id) if found is not None else {}
    if not record:
        log(f"lead turn without a project record job=#{job.job_id}")
        return held(ctx), prompt
    key = job_key(job.message_id, job.job_id)
    config = butler.mcp_config(release, ctx.state_dir, key, sys.executable, lead=record["id"])
    ctx = held(dataclasses.replace(ctx, options={**ctx.options, "mcp_config": config}))
    if job.kind != "chat":
        return ctx, prompt
    try:
        pack = state_pack(ctx.state_dir, record, key, full=not session_id, redacted=project_context.marked(ctx.state_dir, job.topic_id),
                          since=board_read.previous_start(ctx.state_dir, job.topic_id, job.job_id) if session_id else None)
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


# What a 协作 consult of the lead reads (app/collab_channel.py): the pack, nothing taken.

def peek_receipts(state_dir, project_id, limit=INBOX_ROWS) -> list:
    """The receipts no turn of the lead has read for good, oldest first, read only: none is marked as taken (the lead's
    own turn takes them, ``take_receipts``)."""
    try:
        paths = sorted(inbox_dir(state_dir, project_id).glob("*.json"))
    except OSError:
        return []
    found = [item for item in (read_object(path) for path in paths)
             if item.get("id") and took_it(state_dir, item.get("taken_by"), "")]
    return found[:limit]


def read_only_pack(state_dir, record, *, redacted=False) -> str:
    """The lead's state pack (``state_pack``, whole) as a 协作 consult of the lead reads it: its receipts peeked
    (``peek_receipts``), never taken, so the lead's next turn reads them all the same."""
    return state_pack(state_dir, record, "", full=True, receipts=peek_receipts(state_dir, record["id"]), redacted=redacted)
