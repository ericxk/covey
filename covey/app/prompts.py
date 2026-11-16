"""Shared prompt builders: the bridge rules and each turn's message.

The rules are the same on every turn of a topic: who the model is and the bridge's
constraints, the workspace, and the checkpoint contract. A backend whose capabilities
declare ``system_instructions`` sends them at system level on every turn; for any other
backend they open the message of a fresh session (``app/runs.py``). The message says what
changes per turn: the time, the user's message (with the earlier conversation on a fresh
session), and this turn's checkpoint file. Templates are joined line by line, so a
multi-line message never shifts their indentation.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from covey.cards.review import render_history
from covey.core import env as cenv
from covey.core import knowledge
from covey.core.checkpoint import CHECKPOINT_RULES
from covey.core.butler_tool_specs import BUTLER_RULES_ENV

# Commands' and subtasks' descriptions show on the task card's 执行过程 (core/text.py step_action): the owner
# reads them, so they are in the owner's language too (T444 V11: 27 of #3288's 33 were English).
TOOL_NOTES_LANGUAGE = ("工具调用里写给人看的说明（命令和子任务的 description、待办条目）也用用户提问的语言写；"
                       "用户用中文时写中文。这些说明会显示在飞书任务卡的“执行过程”里。")

# For a backend without a system channel the rules are only in a fresh session's message;
# these lines restate the reply contract on each resumed turn.
RESUME_REMINDERS = "\n".join((
    "Continue the current topic/session. Ask follow-up questions in this topic.",
    "Keep reports local unless the user requests publication; do not select a globally latest review.",
    "Reply only with the Feishu-friendly Markdown message text that should be sent back to Feishu.",
    "Use short sections, bullets, numbered steps, and fenced code blocks where useful.",
    "Preserve the requested plan and decision context before asking for confirmation; do not shorten them to a question.",
    "Scope searches to specific directories with a real timeout; stop commands by process group and leave none running.",
    "默认使用用户提问的语言回复；如果用户使用中文，最终回复、进度说明和总结都用中文。",
    TOOL_NOTES_LANGUAGE,
))


def _now() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def constraints(backend_name: str, runtime_note: str = "", notes: tuple[str, ...] = ()) -> str:
    runtime_note = runtime_note or f"{backend_name} permissions controlled by this bridge"
    return "\n".join((
        f"You are {backend_name}, replying to the user through Feishu.",
        "",
        "Constraints for this bridge:",
        "- Reply only with the message text that should be sent back to Feishu.",
        "- Match detail to the request. Preserve the complete requested plan, evidence and decision context; avoid filler.",
        "- Format replies as Feishu-friendly Markdown: use short sections, bullets, numbered steps,",
        "  and fenced code blocks for commands, logs, paths, diffs, or snippets.",
        "- Avoid huge paragraphs; prefer scannable structure. Do not add decorative filler.",
        f"- You are running from a Feishu bridge with {runtime_note}.",
        "- If permissions permit it, you may inspect files, edit code, and run verification commands",
        "  for the current workspace when the user asks you to do so.",
        "- Avoid destructive operations, secret access, broad filesystem changes, and long-running",
        "  services from Feishu unless the user's latest message is explicit and low-risk.",
        "- Scope every search: never walk a home, a workspace root, a data or NAS tree as a whole",
        "  (`rg --files`, `find`, `ls -R`, `du`); a filter after a pipe does not limit the walk. Search the",
        "  specific repository or subdirectory, limit the depth, and give the command a real timeout",
        "  (e.g. `timeout --kill-after=2s 30s`). After a timeout, narrow the search instead of retrying it.",
        "- Stop a command by its whole process group (`kill -TERM -- -<PGID>`), not only its shell or the last",
        "  filter of a pipeline, then check that nothing you started is still running, including processes",
        "  adopted by PID 1. A search that ends with 134 (`memory allocation ... failed`) or 137 walked too",
        "  much: narrow it; do not rerun it unchanged or raise a memory limit.",
        "- This Feishu topic is one persistent session. Ask follow-up questions here and continue here.",
        "- Do not automatically publish process, diff or review documents to Feishu; keep local reports",
        "  unless the user requests publication. Never select another topic's latest review run.",
        "- Do not include hidden reasoning, tool logs, or implementation details unless directly useful.",
        "- 默认使用用户提问的语言回复；如果用户使用中文，最终回复、进度说明和总结都用中文。",
        "- " + TOOL_NOTES_LANGUAGE,
        # A backend's own constraints, one bullet each, after the shared ones.
        *("- " + note for note in notes),
    ))


def workspace_block(workspace_name: str, workdir: Path, device: str = "", remote_workdir: str = "") -> str:
    # On a device the directory is the device's, written as text: the hub's stand-in is never shown (it is no directory).
    place = (f"Current workdir: {remote_workdir} (on device {device}; the files of this task are on that machine)\n"
             if device else f"Current workdir: {Path(workdir).resolve()}\n")
    return (
        f"Current workspace: {workspace_name or 'default'}\n"
        + place +
        "Read and follow the applicable AGENTS.md / CLAUDE.md and storage conventions in this workspace. "
        "Keep project outputs in the locations prescribed by that workspace; use its .local/ "
        "only when permitted, and honor any configured external/NAS data directory. "
        "The bridge source repository, service state directory and an isolated HOME do not determine the project output root. "
        "An explicit checkpoint path is for that checkpoint only; do not infer an artifact directory from it."
    )


# The owner's principles (core/principles.py), after the workspace in the rules and in each resumed turn's reminder.
PRINCIPLES_CHARS = 2000  # Of the principles' lines; the oldest go first past it.
PRINCIPLES_HEAD = ("主人的原则（主人说过、长期适用的原则和偏好；做事时照着做，不用再问主人“之前的思路”是什么。"
                   "和主人这一轮的话冲突时，以这一轮为准）：")


def principles_block(owner, workspace=(), workspace_name: str = "", limit: int = PRINCIPLES_CHARS) -> str:
    """The owner's principles a turn reads: 通用 (``owner``), then those of ``workspace_name`` (``workspace``), each
    oldest first; '' when there are none. Past ``limit`` characters of lines the oldest are left out, either group,
    and a last line says how many."""
    rows = [(str(item.get("at") or ""), 0, i, "- " + item["text"]) for i, item in enumerate(owner)]
    rows += [(str(item.get("at") or ""), 1, i, "- " + item["text"]) for i, item in enumerate(workspace)]
    if not rows:
        return ""
    kept, used = [], 0
    for row in sorted(rows, key=lambda row: (row[0], row[1], row[2]), reverse=True):
        if used + len(row[3]) + 1 > limit:
            break
        kept.append(row)
        used += len(row[3]) + 1
    lines = [PRINCIPLES_HEAD]
    for group, title in ((0, "通用："), (1, f"工作区 {workspace_name}：")):
        shown = sorted((row for row in kept if row[1] == group), key=lambda row: (row[0], row[2]))
        if shown:
            lines += [title, *(row[3] for row in shown)]
    if len(kept) < len(rows):
        lines.append(f"另有 {len(rows) - len(kept)} 条较早的原则未列出。")
    return "\n".join(lines)


# 知识条目 (core/knowledge.py), when COVEY_KNOWLEDGE=1: the owner's entries after the principles, and the rule
# that a topic's turn writes ``remember`` into its checkpoint only when the owner asked in that turn (collected by
# app/knowledge_collect.py). The history command line is there whenever COVEY_HISTORY_CMD is set.
KNOWLEDGE_HEAD = ("主人的知识条目（主人让记下的决定、做法、坑和事实；做事时参考，和主人这一轮的话冲突时以这一轮为准）：")
KNOWLEDGE_NEW_HEAD = "主人新记或改动的知识条目（上一轮之后；更早的这个会话已经有了）："
REMEMBER_RULE = (
    "知识条目：只有主人这一轮的原话明确说了“记一下”“记住”“记下来”，才在检查点里加 remember："
    '[{"kind":"decision|practice|pitfall|fact|question","text":"一句话，最多 200 字","why":"原因，可空",'
    '"scope":"owner|workspace"}]，最多 5 条（owner 是所有工作区通用的，workspace 只对当前工作区），'
    "并在最终回复里逐条列出记了什么。主人没说就不要写 remember，也不要自己总结着记。"
)


def history_line(command: str) -> str:
    return (f"要查以前做过的事（“之前”“上次”“那个 X 怎么弄的”，或你自己缺背景时）：先看知识条目，"
            f"再用 `{command} search <一句话>`；不要自己 rg 状态目录或会话目录。")


def knowledge_block(owner, workspace=(), workspace_name: str = "", *, state_dir="", history: str = "", new: bool = False,
                    limit: int = knowledge.KNOWLEDGE_CHARS) -> str:
    """The knowledge entries a turn reads: 通用 (``owner``), then those of ``workspace_name`` (``workspace``), each
    group's newest first, and the history command line when ``history`` is the command. ``new``: only the entries
    added or rewritten since the turn before (a resumed session has the others). Past ``limit`` characters of lines
    the oldest are left out, either group, and a last line says how many and where the rest is (``state_dir``).
    '' when there is nothing to say."""
    rows = [(e, 0) for e in owner] + [(e, 1) for e in workspace]
    newest = sorted(reversed(rows), key=lambda row: -knowledge.epoch(row[0]["at"]))
    kept, left = knowledge.trim([entry for entry, _ in newest], limit)
    shown = {entry["id"] for entry in kept}
    lines = []
    if kept:
        lines.append(KNOWLEDGE_NEW_HEAD if new else KNOWLEDGE_HEAD)
        for group, title in ((0, "通用："), (1, f"工作区 {workspace_name}：")):
            mine = [entry for entry, which in newest if which == group and entry["id"] in shown]
            if mine:
                lines += [title, *map(knowledge.entry_line, mine)]
        if left:
            cut = {which for entry, which in newest if entry["id"] not in shown}
            where = "、".join(str(knowledge.md_path(state_dir, knowledge.OWNER if which == 0 else knowledge.WORKSPACE,
                                                    "" if which == 0 else workspace_name)) for which in sorted(cut))
            lines.append(f"另有 {left} 条较早的未列出" + (f"，见 {where}。" if state_dir else "。"))
    if history:
        lines.append(history_line(history))
    return "\n".join(lines)


def bridge_rules(
    *,
    backend_name: str,
    workspace_name: str,
    workdir: Path,
    runtime_note: str = "",
    notes: tuple[str, ...] = (),
    principles: str = "",
    knowledge_text: str = "",
    remember: bool = False,
    device: str = "",
    remote_workdir: str = "",
) -> str:
    """Identity and constraints, the workspace, the owner's principles (``principles``, principles_block; none: no
    block), the owner's knowledge entries (``knowledge_text``, knowledge_block; none: no block), the checkpoint
    contract (``remember``: and the rule for writing entries): the same every turn of a topic."""
    return "\n\n".join((constraints(backend_name, runtime_note, notes),
                        workspace_block(workspace_name, workdir, device, remote_workdir), *((principles,) if principles else ()),
                        *((knowledge_text,) if knowledge_text else ()),
                        CHECKPOINT_RULES, *((REMEMBER_RULE,) if remember else ())))


def earlier_history(rows: list[dict[str, Any]], message_id: str, user_text: str) -> list[dict[str, Any]]:
    """The conversation before this turn's message.

    A message is in the history before its job is queued (``app/app.py``), and the prompt
    states it as the latest message; an answer or a held note is the last row, without the
    job's message ID.
    """
    kept = [row for row in rows if not message_id or row.get("message_id") != message_id]
    last = kept[-1] if kept and len(kept) == len(rows) else {}
    content = str(last.get("content", "")).strip() if last.get("role", "user") == "user" else ""
    return kept[:-1] if content and user_text.strip().endswith(content) else kept


def first_turn_message(user_text: str, history_snapshot: list[dict[str, Any]]) -> str:
    history = render_history(history_snapshot)
    return "\n".join((
        f"Current time: {_now()}",
        "",
        "Recent conversation:",
        history or "[none]",
        "",
        "Latest user message:",
        user_text,
    ))


def resume_message(user_text: str) -> str:
    """A message continuing an existing Harness session: the session already has the conversation."""
    return "\n".join((
        f"Current time: {_now()}",
        "",
        "Latest Feishu user message:",
        user_text,
    ))


# The butler's rules (app/butler.py), at the head of each of its turns' messages: the owner wants the
# Bridge's rules in the user message, not the system prompt. Without "do the work in a topic" a single session in
# DeepSeek's experiment never handed any work on. One layout (core/placement.py), the 话题区: the butler's card
# in the owner's message's thread, answers and conclusions new messages in the chat area. The rules of the front-desk
# layout (desk_topics) went with it on 2026-11-09.
AREA_OPENING = (
    "- 你是主人的管家。主人在私聊聊天区发的每条消息是你的一轮；主人在话题里发的消息直接交给那个话题，不经过你。",
    "- 一问一答：你这一轮的回复就是给主人的答案，Bridge 把它作为聊天区的一条新消息发出，顶上链回主人那句话。"
    "结论先行，简短；不要提卡片、话题串这些摆放，也不要让主人去别处看。",
)
SHARED_RULES = (
    "- 只读的、一两分钟能做完的查询和命令（看一眼文件或日志、查配置、连得上吗、跑个只读命令），自己用工具做完直接答，不开话题。",
    "- 其余的事都交给话题去做，不要在自己这一轮里做：可能超过一两分钟的、要改代码或文件的、主人说开话题的。"
    "放哪儿按“是哪件事”分，不按它会跑多久：新的事用 Bridge 工具 topic_start 开一个话题；"
    "和已有话题是同一件事（主人提了 T 号、在说刚才那件、同一个仓库里的同一件事，几天前的也算），"
    "用 topic_send 追加，在跑的作为补充，停了的开下一轮。下面说的工具都是 Bridge 工具（mcp__bridge__<工具名>）。",
    "- 用 topic_start、topic_send、topic_fork 交给话题时，写干净的任务说明：主人原话和附件 Bridge 会自动附上，"
    "你不用抄，也不要写路由的话、不要抄附件的本机路径。",
    "- 一句话里有几件独立的事，就开几个话题。工作区用 workspaces_list 选，拿不准就问主人。",
    "- 两个以上话题都可能是这件事时不要猜：用 AskUserQuestion 出一道单选题，选项是候选话题（T 号和标题）和“另开”。"
    "需要已有话题的上下文、又不是同一件事的，用 topic_fork 分叉。"
    "追问只是问已有结果、不用新查新做的，用 topic_read 读了自己答，不转交；要新查、新做的才 topic_send。",
    "- 主人说某个文档、文件、结果是之前话题给的，先用 topics_search 按链接、token、路径找是哪个话题的哪一轮，"
    "没命中再读文档取标题或正文里的词搜；找到了用 topic_read 的 job 读那一轮，再决定追加还是新开；找不到就说没找到来源。",
    "- 主人说“T412 选 A”这类话，用 question_answer 代为回答；说“T412 急”，用 topic_priority；"
    "说“T412 不用管了”“从看板撤下”，用 topic_priority 并给 remove: true。",
    "- 要看或改管家的设置，用 settings_panel 把设置卡发出来；设置只有主人能在卡上改。",
    "- 主人说出长期适用的原则或偏好（“以后……”“原则是……”“记住……”“都要……”）时，用 principle_save 记下"
    "（所有工作区都适用的记通用，只对某个工作区成立的记那个工作区），回复里说一句“已记下：……”；"
    "只对这件事、这一轮的要求不记。主人说“忘掉/删掉那条”时，用 principles_list 找到它，再用 principle_delete 删掉。"
    "记下的原则每一轮都附在 Bridge 的规则里，你和各个话题都会看到。",
    "- " + TOOL_NOTES_LANGUAGE,
)
# 管家 (COVEY_BUTLER_RULES=1, docs/butler.md 管家规则): the butler only answers quickly and hands the rest on, so
# it is free for the owner's next question. It takes the place of SHARED_RULES' first two rules (everything under a
# minute or two is done in place; what goes to a topic area is what may take over a minute or two); the other rules are
# the same. Off, the rules are exactly SHARED_RULES. BUTLER_RULES_ENV is core/butler_tool_specs.py's: the tool list reads it too.
BUTLER_RULES = (
    "- 你只做快答和分派，不在自己这一轮里干几分钟的活，这样才接得住主人的下一句。"
    "快答：不用工具，或只用一两次只读的查询（看一眼文件或日志、查一项配置、topic_read、topics_list），几秒内就能答完的，直接答，不开话题。"
    "其余的都派出去，不要先自己试一试：要查一圈的、要跑命令的、要改代码或文件的、要联网或多步操作的、预计超过半分钟的。"
    "新的事用 topic_start 开话题；和已有话题是同一件事的用 topic_send（见下一条）。"
    "拿不准是快答还是要干活的，派出去。"
    "主人点名用哪个 Harness、模型或思考预算时（“用 Claude Code”“让 Codex 做”），开话题的 topic_start 里照填；没点名就不填。"
    "派出去后只对主人说一句：放到哪个 T 号、大概多久，例如“放到 T432「标题」里做，大概十分钟”。"
    "例外：主人明确说“你直接做”“别开话题”“就在这儿做”时，这一轮自己做完再答；没有这样的话，一律照上面分。",
    # SHARED_RULES' second rule, as is, but for what goes to a topic area.
    SHARED_RULES[1].replace("可能超过一两分钟的、要改代码或文件的、主人说开话题的。",
                            "要查一圈的、要跑命令的、要改代码或文件的、要联网或多步操作的、预计超过半分钟的、主人说开话题的。"),
)
assert BUTLER_RULES[1] != SHARED_RULES[1]


def butler_on(environ=None) -> bool:
    """Whether COVEY_BUTLER_RULES=1 (only ``1``; read on every call)."""
    return str(cenv.get(BUTLER_RULES_ENV, "", environ)).strip() == "1"


def shared_rules(environ=None) -> tuple[str, ...]:
    """SHARED_RULES, the butler's two rules in place of the first two when COVEY_BUTLER_RULES=1."""
    return BUTLER_RULES + SHARED_RULES[2:] if butler_on(environ) else SHARED_RULES


AREA_RULE = ("- 交给话题后，你只用一句话说放到了哪里：新开的写“放到 T432「标题」里做”，追加的写“接着在 T432 里做”。"
             "不写进度，不写“结论会带回来”之类的话，也不用再报结论：话题每一轮结束，Bridge 都会把它的结论作为新消息发到聊天区；"
             "进度主人看 Pin。被问到时用下面的快照和 topic_read 回答。")
def knowledge_rules() -> tuple[str, ...]:
    """The butler's rules for the knowledge entries (COVEY_KNOWLEDGE=1) and the history search
    (COVEY_HISTORY_CMD); none when neither is on, so the rules are then what they were."""
    rules = []
    if knowledge.enabled():
        rules += [
            "- 主人说“记一下……”“记下来”，用 knowledge_save 记一条（决定、做法、坑、事实、待定；通用的记 owner，只对某个工作区的记那个工作区）；"
            "“记住”若是以后每次都要遵守的做法或偏好，仍用 principle_save，是一件具体的事实、决定或坑就记知识条目。"
            "主人说“把 T483 的结论记一下”，先用 topic_read 读那一轮再起草。记完在回复里列出 id 和正文。",
            "- 主人说“改成……”用 knowledge_update（给原因），说“删了”“作废”用 knowledge_drop；先用 knowledge_search 找到 id。"
            "只记主人说的，不要自己总结着记。"]
    if knowledge.history_cmd():
        order = "knowledge_search → topics_search → history_search" if knowledge.enabled() \
            else "topics_search → history_search"
        rules.append(f"- 查以前做过的事、之前的决定，按顺序：{order}，前一个找到了就不用查后一个。"
                     "history_search 查的是 Claude、Codex 和 Bridge 的全部历史轮次，给一句话描述；结果里的原文位置可以再用它的输出追问。")
    elif knowledge.enabled():
        rules.append("- 查以前做过的事、之前的决定，按顺序：knowledge_search → topics_search，前一个找到了就不用查后一个。")
    return tuple(rules)


def butler_rules() -> str:
    """The butler's rules."""
    return "\n".join(("[管家规则]", *AREA_OPENING, *shared_rules(), *knowledge_rules(), AREA_RULE))


BUTLER_RULES_TEXT = butler_rules()


def butler_message(snapshot: str, message: str, note: str = "") -> str:
    """A butler turn's message: its rules, the snapshot of the owner's topics, then the turn's own message.
    ``note``: a track's parallel note (app/butler_tracks.py), between the snapshot and the message."""
    return "\n\n".join((butler_rules(), snapshot, *((note,) if note else ()), message))
