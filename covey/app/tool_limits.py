"""What a turn may do beyond the Bridge's words, held where its Harness can (core/harness.py TOOL_LIMIT,
``HarnessCapabilities.read_only``): app/runs.py puts ``limit``'s answer into the turn's context before anything of the
turn runs, and backends/claude/argv.py and backends/codex/appserver.py read it. A turn none of these applies to runs as
before.

- READ: a turn a member started in a group whose members are read-only (app/access.py ``read_only``, the 群聊 card's
  群成员只读). Claude Code gets Read, Grep and Glob and none of the owner's MCP servers; Codex runs in its read-only
  sandbox; a Harness that can do neither does not run it (``refusal``). Its message ends with MEMBER_NOTE.
- PROXY: a 代答 topic's turn (core/topics.py proxy_topic). Claude Code gets Read, Grep, Glob, Bash, Edit and Write and
  none of the owner's MCP servers, and a hook that lets its shell run only lark-cli's read commands and its edit tools
  write only in the contact's notes (backends/claude/tool_guard.py, TOOL_NOTES); the other Harnesses run as before.
- CONSULT: a 协作 ask's turn (core/turn_source.py ``behavior(job).read_only``, COVEY_COLLAB=1), in its channel
  (app/collab_channel.py). Claude Code gets Read, Grep and Glob and no MCP server at all, whatever the turn's options say
  (the Bridge's tools, a lead's tool list); Codex runs read-only in a private CODEX_HOME with its apps, plugins, memories
  and hooks off and none of the Bridge's tools (backends/codex/consult.py); any other Harness does not run it
  (``refusal``). Its message ends with CONSULT_NOTE. Apart from the others: READ is as it was.

- LEAD: a project lead's turn (app/project_lead.py ``held``, set there after ``limited``, not here): reading, searching and
  its Bridge tools on whichever Harness the lead runs (core/harness.py LEAD). A Harness that cannot take the Bridge's tools
  cannot run a lead and does not run its turn (``refusal``); each that can holds LEAD.

Several at once: the strictest (``limit``): CONSULT, then PROXY (a 代答 topic's own), then READ; a lead's LEAD gives way to
all three.

The butler's read-only first turn (LOOK, the butler or a lead judging a topic read-only) is gone (T956): the Bridge decides
a turn's limit by rules alone; a job record that still says ``read_only`` is read as any other.
"""
from __future__ import annotations

import dataclasses

from covey.core import turn_source
from covey.core.harness import CONSULT, LEAD, PROXY, READ, TOOL_LIMIT, TOOL_NOTES
from covey.core.proxy import read_contacts
from covey.core.topics import proxy_topic, read_topic

# The end of a READ turn's message.
MEMBER_NOTE = ("[Bridge 说明] 这一轮只读：发起这一轮的是群成员，主人把这个群设成了群成员只读。只能读文件和搜索，"
               "不能改文件、不能联网，也不执行有副作用的命令，检查点也不用写；要改东西才能做完的，在回复里写清楚要改什么，留给主人。")
# The end of a CONSULT turn's message (app/collab_channel.py puts the ask above it).
CONSULT_NOTE = ("[Bridge 说明] 这一轮是只读咨询：只能读文件和搜索，不能改文件、不能执行命令、不能联网，"
                "不写检查点、不提问。回复就是给对方的答复：先写结论，再写依据（文件路径、哪一轮做的结论）；"
                "拿不准就说不知道或缺什么材料，不要猜。")
# Strictest first: what ``limit`` takes when several apply.
ORDER = (CONSULT, PROXY, READ)


def consult(job) -> bool:
    """Whether ``job`` is a turn held to reading by its source (core/turn_source.py: a 协作 ask): CONSULT."""
    return job is not None and turn_source.of(job) is not None and turn_source.behavior(job).read_only


def limit(access, ctx, job) -> str:
    """The turn's limit (core/harness.py CONSULT, PROXY, READ), '' for none; the strictest (``ORDER``) when
    several apply: see the module docstring."""
    found = set()
    if consult(job):
        found.add(CONSULT)
    if proxy_topic(ctx):
        found.add(PROXY)
    if job is not None and access.read_only(job.chat_id, getattr(job, "sender_id", "")):
        found.add(READ)
    return next((name for name in ORDER if name in found), "")


def notes_of(ctx) -> str:
    """The notes directory of the contact whose 代答 topic ``ctx`` is ('' when it has none)."""
    try:
        contact = (read_topic(ctx).get("proxy") or {}).get("contact")
    except (OSError, ValueError, AttributeError):
        return ""
    found = next((c for c in read_contacts(ctx.state_dir) if c.get("key") == contact), {}) if contact else {}
    return str(found.get("session_dir") or "")


def limited(access, ctx, job, log=lambda _: None):
    """``ctx`` with the turn's limit in its options (a PROXY turn's with the contact's notes too); ``ctx`` itself for a
    turn without one."""
    found = limit(access, ctx, job)
    if not found:
        return ctx
    extra = {TOOL_NOTES: notes_of(ctx)} if found == PROXY else {}
    log(f"turn limit job=#{getattr(job, 'job_id', '')} limit={found}")
    return dataclasses.replace(ctx, options={**ctx.options, TOOL_LIMIT: found, **extra})


def refusal(ctx, capabilities, title) -> str:
    """Why a READ, CONSULT or LEAD turn does not run on this Harness (it cannot hold the turn to reading), '' when it may."""
    found = ctx.options.get(TOOL_LIMIT)
    if found == CONSULT and not (capabilities.read_only and capabilities.collab):
        return f"{title} 还不能做只读咨询，这一轮没有启动。"
    if found == LEAD and not capabilities.bridge_tools:
        return (f"{title} 不能当专项负责人（用不了 Bridge 工具，也守不住只读），这一轮没有启动。"
                "可以点专项卡上的「换配置」，换成能接 Bridge 工具的 Harness。")
    if found != READ or capabilities.read_only:
        return ""
    return (f"这个群设成了群成员只读，{title} 做不到只读，这一轮没有启动。"
            "可以点“🔁 换配置”换到 Claude 或 Codex 的模型再发一次。")


def note(ctx) -> str:
    """What the turn's message ends with: MEMBER_NOTE for a READ turn, CONSULT_NOTE for a CONSULT one, '' otherwise."""
    found = ctx.options.get(TOOL_LIMIT)
    return "\n\n" + MEMBER_NOTE if found == READ else "\n\n" + CONSULT_NOTE if found == CONSULT else ""
