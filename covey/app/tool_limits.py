"""What a turn may do beyond the Bridge's words, held where its Harness can (core/harness.py TOOL_LIMIT,
``HarnessCapabilities.read_only``): app/runs.py puts ``limit``'s answer into the turn's context before anything of the
turn runs, and backends/claude/argv.py and backends/codex/appserver.py read it. A turn none of these applies to runs as
before.

- READ: a turn a member started in a group whose members are read-only (app/access.py ``read_only``, the 群聊 card's
  群成员只读). Claude Code gets Read, Grep and Glob and none of the owner's MCP servers; Codex runs in its read-only
  sandbox; a Harness that can do neither does not run it (``refusal``). Its message ends with MEMBER_NOTE.
- LOOK: the first turn of a topic the butler opened without the owner's click because it judged the work read-only
  (app/butler_topics.py; the job's ``read_only``, its task text LOOK_NOTE). Claude Code goes without the tools that
  change files (its shell stays, for read-only commands); Codex runs read-only; another Harness has the words alone.
- PROXY: a 代答 topic's turn (core/topics.py proxy_topic). Claude Code gets Read, Grep, Glob, Bash, Edit and Write and
  none of the owner's MCP servers, and a hook that lets its shell run only lark-cli's read commands and its edit tools
  write only in the contact's notes (backends/claude/tool_guard.py, TOOL_NOTES); the other Harnesses run as before.
"""
from __future__ import annotations

import dataclasses

from covey.core.harness import LOOK, PROXY, READ, TOOL_LIMIT, TOOL_NOTES
from covey.core.proxy import read_contacts
from covey.core.topics import proxy_topic, read_topic

# The end of a READ turn's message, and the head of the line a LOOK turn's task text has (app/butler_handoff.py).
MEMBER_NOTE = ("[Bridge 说明] 这一轮只读：发起这一轮的是群成员，主人把这个群设成了群成员只读。只能读文件和搜索，"
               "不能改文件、不能联网，也不执行有副作用的命令，检查点也不用写；要改东西才能做完的，在回复里写清楚要改什么，留给主人。")
LOOK_NOTE = ("这一轮只读：这件事被判断为只要查看和回答，所以没等主人点开始就开工了。这一轮不改任何文件、"
             "不执行有副作用的命令，检查点也不用写；要改东西才能做完的，在回复里写清楚要改什么，等主人下一句。")


def limit(access, ctx, job) -> str:
    """The turn's limit (core/harness.py READ, LOOK, PROXY), '' for none: see the module docstring."""
    if proxy_topic(ctx):
        return PROXY
    if job is None:
        return ""
    if getattr(job, "read_only", False) is True:
        return LOOK
    return READ if access.read_only(job.chat_id, getattr(job, "sender_id", "")) else ""


def notes_of(ctx) -> str:
    """The notes directory of the contact whose 代答 topic ``ctx`` is ('' when it has none)."""
    try:
        contact = (read_topic(ctx).get("proxy") or {}).get("contact")
    except (OSError, ValueError, AttributeError):
        return ""
    found = next((c for c in read_contacts(ctx.state_dir) if c.get("key") == contact), {}) if contact else {}
    return str(found.get("session_dir") or "")


def limited(access, ctx, job, log=lambda _: None):
    """``ctx`` with the turn's limit in its options (a PROXY turn's with the contact's notes too); ``ctx`` itself for
    a turn without one."""
    found = limit(access, ctx, job)
    if not found:
        return ctx
    extra = {TOOL_NOTES: notes_of(ctx)} if found == PROXY else {}
    log(f"turn limit job=#{getattr(job, 'job_id', '')} limit={found}")
    return dataclasses.replace(ctx, options={**ctx.options, TOOL_LIMIT: found, **extra})


def refusal(ctx, capabilities, title) -> str:
    """Why a READ turn does not run on this Harness (it cannot hold the turn to reading), '' when it may."""
    if ctx.options.get(TOOL_LIMIT) != READ or capabilities.read_only:
        return ""
    return (f"这个群设成了群成员只读，{title} 做不到只读，这一轮没有启动。"
            "可以点“🔁 换配置”换到 Claude 或 Codex 的模型再发一次。")


def note(ctx) -> str:
    """What the turn's message ends with: MEMBER_NOTE for a READ turn, '' otherwise."""
    return "\n\n" + MEMBER_NOTE if ctx.options.get(TOOL_LIMIT) == READ else ""
