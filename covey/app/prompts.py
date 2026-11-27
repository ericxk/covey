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
import re
from pathlib import Path
from typing import Any

from covey.cards.review import render_history
from covey.core.process_env import env_flag
from covey.core import extensions, knowledge, projects, turn_source
from covey.core import memory as core_memory
from covey.core.text import short_count, stamp
from covey.core.checkpoint import ASK_SHORT, checkpoint_rules
from covey.core.butler_tool_specs import BUTLER_RULES_ENV, DISPATCH_BUTLER, DISPATCH_OWN

# The one rule about language (5.9 row 13: said once in a turn's rules, which are all in Chinese). Commands' and subtasks'
# descriptions show on the task card's 执行过程 (core/text.py step_action): the owner reads them, so they are in his
# language too. Antigravity's calls say it in toolSummary (backends/agy/bridge.py tool_summary); a command with no such
# field (Codex's) says it in a comment it begins with (core/text.py command_purpose): 2026-11-24 T738, their steps read
# "set -euo pipefail for v in http_proxy…" one under another.
LANGUAGE_RULE = ("写给人看的话都用用户提问的语言，用户用中文就写中文：最终回复、进度说明、总结，以及工具调用里写给人看的说明"
                 "（命令和子任务的 description、待办条目，它们会显示在飞书任务卡的“执行过程”里；工具调用的 toolSummary 也一样；"
                 "命令没有这类说明字段时，命令第一行写一行注释 `# 这一步做什么`）。")

# Scoped searches and stopping commands (5.9 row 12): the core's one sentence. The lines about walking a shared NAS
# and its runaway searches are a deployment's (an extension's SETTINGS ``search_rules``, core/extensions.py), as is
# their sentence in the resumed turns' reminder (``search_reminder``).
SEARCH_RULE = "搜索限定目录并设超时。"
SEARCH_RULES_SETTING, SEARCH_REMINDER_SETTING = "search_rules", "search_reminder"


def search_rules() -> tuple[str, ...]:
    """The constraints' lines about searches and stopping commands, each a "- " bullet: an enabled extension's
    ``search_rules`` (its lines as written), else SEARCH_RULE."""
    text = extensions.setting(SEARCH_RULES_SETTING, "").strip()
    if not text:
        return ("- " + SEARCH_RULE,)
    return tuple(line if line.startswith(("- ", "  ")) else "- " + line for line in text.splitlines() if line.strip())


# For a backend without a system channel the rules are only in a fresh session's message;
# these lines restate the reply contract on each resumed turn (``resume_reminders``).
RESUME_HEAD = (
    "接着这个话题（会话）做，要追问就在这个话题里问。",
    "报告留在本地，用户要求发布时才发。",
    "只回复要发回飞书的消息正文，用适合飞书的 Markdown：短小节、列表、编号步骤，需要时用代码块。",
    "要确认之前，先把用户要的方案和决策背景写完整，不要缩成一句提问。",
)
RESUME_REMINDERS = "\n".join((*RESUME_HEAD, SEARCH_RULE, LANGUAGE_RULE))


# A topic's resumed turn whose previous turn left the model's context high (``context_high``): one line with the
# figures that asks for the checkpoint first, so a compaction in the turn loses no progress, and says the turn goes on
# as usual. The CLIs compact on their own near the end of the window (Claude Code at about 97 万 of 100 万, Codex at 90 %
# of its model's window), so the line comes from HIGH_SHARE of the window, or only ROOM short of it on a large one: a
# turn grows the context by less than ROOM nearly always (Claude 1M p99 32 万), and a 1M session is not told from 60 万
# on. "上下文快满了" made T774 stop short of its work and say its context was full while the session went on.
HIGH_SHARE = 0.6
ROOM = 300_000
CONTEXT_HIGH = ("上一轮结束时上下文 {used} / {window} token。会话照常接着用，到窗口上限前会自动压缩，不用为此做别的："
                "先把检查点的 completed 和 next_step 更新到最新（压缩后靠它接上进度），然后照常做完这一轮的事。"
                "不要因为上下文高就停下、提前收尾、把要做的留给下一轮或等新会话，回复里也不要说上下文满了。")


def context_high(record: dict | None) -> str:
    """CONTEXT_HIGH with its figures when ``record`` (the previous turn's) holds a context size of at least HIGH_SHARE
    of the window the backend reported for that model and at least the window less ROOM; '' for anything else, a
    window not known included."""
    tokens, window = (record or {}).get("context_tokens"), (record or {}).get("context_window")
    if type(tokens) is int and type(window) is int and window > 0 and tokens >= max(HIGH_SHARE * window, window - ROOM):
        return CONTEXT_HIGH.format(used=short_count(tokens), window=short_count(window))
    return ""


def resume_reminders(notes: tuple[str, ...] = ()) -> str:
    """RESUME_REMINDERS, with an enabled extension's ``search_reminder`` in place of SEARCH_RULE, then the backend's own
    constraints (``notes``: its ``prompt_notes`` this turn, such as the redaction note of a topic that turned redaction
    on after its session started), a line each: the session's first message had only those of its own start."""
    return "\n".join((*RESUME_HEAD, extensions.setting(SEARCH_REMINDER_SETTING, "").strip() or SEARCH_RULE, LANGUAGE_RULE,
                      *notes))


def _now() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def constraints(backend_name: str, runtime_note: str = "", notes: tuple[str, ...] = ()) -> str:
    runtime_note = runtime_note or f"{backend_name} 的权限由这个桥接器控制"
    return "\n".join((
        f"你是 {backend_name}，通过飞书回复用户。",
        "",
        "这个桥接器的约束：",
        "- 只回复要发回飞书的消息正文。",
        "- 详略跟着请求走：用户要的完整方案、证据和决策背景都写全，不写空话。",
        "- 用适合飞书的 Markdown：短小节、列表、编号步骤；命令、日志、路径、diff 和代码片段放进代码块。",
        "- 不写大段文字，排成一眼能扫完的结构；不加装饰性的话。",
        f"- 你在飞书桥接器里运行，{runtime_note}。",
        "- 权限允许时，用户要你做的，你可以在当前工作区里看文件、改代码、跑验证命令。",
        "- 不做破坏性操作、不碰密钥、不大范围改文件系统、不从飞书启动长期运行的服务，除非用户最新一条消息明确要求而且风险低。",
        *search_rules(),
        # A supplement such as 'the QR code expired, send a new one' must not wait minutes for a wait loop to end.
        "- 你干活时用户发来的话，只在两次工具调用之间才送到你这里。要等用户（扫码、确认）或外面的事（部署、任务、网页）时，"
        "用每次最多 30 秒的短命令轮询，不要用一个长时间阻塞的命令或循环，这样新的指示几秒内就能读到。",
        "- 这个飞书话题是一个持续的会话：要追问就在这里问，在这里接着做。",
        "- 不要自动把过程、diff 或评审文档发到飞书，报告留在本地，用户要求发布时才发。",
        "- 除非直接有用，不写隐藏的推理、工具日志和实现细节。",
        "- " + LANGUAGE_RULE,
        # A backend's own constraints, one bullet each, after the shared ones.
        *("- " + note for note in notes),
    ))


def proxy_constraints(backend_name: str, runtime_note: str = "", notes: tuple[str, ...] = ()) -> str:
    """The rules of a 代答 topic's turns, in place of ``constraints`` and the checkpoint contract: its replies
    go to other people as the owner, in plain text; the Markdown layout, the checkpoint, its questions and 话题要点
    of an ordinary topic only drew its turns into bookkeeping (two of a turn's three tool calls wrote the checkpoint)."""
    runtime_note = runtime_note or f"{backend_name} 的权限由这个桥接器控制"
    return "\n".join((
        f"你是 {backend_name}，通过飞书桥接器替主人回复别人（代答）。",
        "",
        "这个桥接器的约束：",
        "- 这是代答话题：每一轮替主人回复一个联系人，最终回复会原样以主人的身份发给对方；怎么回写在每批消息里。",
        f"- 你在飞书桥接器里运行，{runtime_note}。",
        "- 只做读取和查询（聊天、文档、代码、记录）；不改代码和配置、不部署、不删东西（联系人笔记照每批的说明更新）；"
        "不要自己用 lark-cli 或别的工具发飞书消息，回复由 Bridge 发出。",
        "- 不用写检查点，也不写话题要点。",
        *search_rules(),
        "- " + LANGUAGE_RULE,
        *("- " + note for note in notes),
    ))


def workspace_block(workspace_name: str, workdir: Path, device: str = "", remote_workdir: str = "") -> str:
    # On a device the directory is the device's, written as text: the hub's stand-in is never shown (it is no directory).
    place = (f"当前工作目录：{remote_workdir}（在设备 {device} 上，这个任务的文件都在那台机器上）\n"
             if device else f"当前工作目录：{Path(workdir).resolve()}\n")
    return (
        f"当前工作区：{workspace_name or 'default'}\n"
        + place +
        "先读并遵守这个工作区里适用的 AGENTS.md / CLAUDE.md 和存储约定。"
        "项目产出放在这个工作区规定的位置；只在允许时用它的 .local/，有配置的外部或 NAS 数据目录就用它。"
        "桥接器的源码仓库、服务状态目录和隔离的 HOME 都不决定项目产出的根目录。"
        "明确给出的检查点路径只用来写检查点，不要据此推断产物目录。"
    )


# The owner's principles (core/principles.py), after the workspace in the rules and in each resumed turn's reminder.
PRINCIPLES_CHARS = 2000  # Of the principles' lines; the oldest go first past it.
# What a principle already allows is not asked again. Said in the principles' head and, with COVEY_MEMORY=1, in the memory's
# rules.
ASK_AFTER_PRINCIPLES = "要问主人之前，先看这些原则：原则里已经同意的事（比如测试通过就合并、空闲时直接部署）照着做，不要再问。"
PRINCIPLES_HEAD = ("主人的原则（主人说过、长期适用的原则和偏好；做事时照着做，不用再问主人“之前的思路”是什么。"
                   "和主人这一轮的话冲突时，以这一轮为准。" + ASK_AFTER_PRINCIPLES + "）：")


def principles_block(owner, workspace=(), workspace_name: str = "", limit: int = PRINCIPLES_CHARS) -> str:
    """The owner's principles a turn reads: 通用 (``owner``), then those of ``workspace_name`` (``workspace``), each
    oldest first; '' when there are none. Past ``limit`` characters of lines the oldest are left out, either group,
    and a last line says how many; one marked 常驻 (``pinned``, core/principles.py pin) never is."""
    rows = [(str(item.get("at") or ""), 0, i, "- " + item["text"], item.get("pinned") is True) for i, item in enumerate(owner)]
    rows += [(str(item.get("at") or ""), 1, i, "- " + item["text"], item.get("pinned") is True) for i, item in enumerate(workspace)]
    if not rows:
        return ""
    kept, used = [row for row in rows if row[4]], sum(len(row[3]) + 1 for row in rows if row[4])
    for row in sorted((row for row in rows if not row[4]), key=lambda row: (row[0], row[1], row[2]), reverse=True):
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


# 专项记忆 and 资料库 (core/project_memory.py, core/project_library.py), when COVEY_PROJECT_MEMORY=1: what a topic of a project reads, after
# the principles in the rules and in each resumed turn's reminder (app/project_context.py builds the lines).
PROJECT_CHARS = 3000  # Of the memory's lines given as they are; the oldest go first past it. The library's index has its own 2 KB.
PROJECT_MEMORY_HEAD = ("专项记忆（这个话题属于专项「{name}」；下面是专项里已经定下的事，做事时照着办，"
                       "和主人这一轮的话冲突时以这一轮为准）：")
PROJECT_LIBRARY_HEAD = "专项资料库（主人给专项的文件，和专项各话题的产物、交付；需要时按路径去读，设备上的话题读不到本机的文件）："
# A project's memory in its three parts (core/memory.py project_view), as a member topic reads it with COVEY_MEMORY off and as the
# lead's state pack has it (app/project_lead.py): each part's head and lines, what was left out, what was voided, the version.
PROJECT_PART_HEADS = {
    core_memory.RULE: "长期约定（主人明说以后都照做的）：",
    core_memory.DECISION: "当前决定（主人答过的问题，一题一条，只管那道题问的范围；换了批次、目标或扩大范围要重新问）：",
    core_memory.REFERENCE: "待核对参考（负责人整理的口径，不是主人原话；和主人的话冲突时让路，不能单独作为替主人答题的依据）：",
}
VOIDED_HEAD = "上一轮之后作废、不再适用的（之前带过，现在不要再照着做）："
LEFT_PARTS = "专项记忆里这一轮放不下、没有带上的：{parts}（都还在专项记忆里）。"
NO_ORIGIN = "（无出处）"  # After the owner's words of an older line the Bridge has no origin of (core/turn_source.py level agent).


def unsourced(item) -> bool:
    """Whether ``item`` is a project's line of the owner's words the Bridge has no origin of (core/turn_source.py level agent)."""
    return bool(item.get("part")) and item["by"] == core_memory.OWNER and item.get("level") == turn_source.LEVEL_AGENT


def item_text(item) -> str:
    """An item's words as a block says them: a 当前决定 with the day it was given, a line with its conditions (``applies``)."""
    text = item["text"]
    if item.get("part") == core_memory.DECISION and item.get("at"):
        text += f"（{stamp(item['at'], '%m-%d')}）"
    if item.get("applies"):
        text += f"（适用：{item['applies']}）"
    return text


def left_parts(view) -> str:
    """The line of what a project's parts left out (``view``: core/memory.py project_view), '' when nothing was."""
    parts = [f"{core_memory.PART_WORDS[part]} {len(view['parts'][part]['left'])} 条" for part in core_memory.PARTS
             if view["parts"][part]["left"]]
    return LEFT_PARTS.format(parts="、".join(parts)) if parts else ""


def project_memory_lines(view) -> list:
    """A project's memory (``view``: core/memory.py project_view) as lines: each part that has any, its head and its lines
    "- [kind] text", the oldest first ("（新）" after one written since the reader's previous turn); then what was left out, what
    was voided since that turn and the version. [] when the view has nothing to say."""
    lines = []
    for part in core_memory.PARTS:
        kept = sorted(view["parts"][part]["kept"], key=lambda item: (item["at"], item["ref"]))
        if kept:
            lines.append(PROJECT_PART_HEADS[part])
            lines += [f"- [{item['kind'] or 'fact'}] {item_text(item)}" + (NO_ORIGIN if unsourced(item) else "")
                      + ("（新）" if item["ref"] in view["new"] else "") for item in kept]
    if left_parts(view):
        lines.append(left_parts(view))
    if view["voided"]:
        lines += [VOIDED_HEAD, *(f"- [{item['kind'] or 'fact'}] {item['text']}" for item in view["voided"])]
    return [*lines, f"专项记忆版本 {view['version']}。"] if lines else []


def project_block(name, memory=(), library=(), limit: int | None = PROJECT_CHARS) -> str:
    """The project's memory and library a topic of it reads: ``memory`` (lines, oldest first; past ``limit`` characters the
    oldest are left out and a line says how many; ``limit`` None: lines already chosen, ``project_memory_lines``), then
    ``library`` (the index's lines); '' when both are empty."""
    kept, used = [], 0
    for line in reversed(list(memory)):
        if limit is not None and used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    blocks = []
    if kept:
        left = len(list(memory)) - len(kept)
        blocks.append("\n".join([PROJECT_MEMORY_HEAD.format(name=name or "这个专项"), *kept,
                                 *([f"另有 {left} 条较早的记忆未列出。"] if left else [])]))
    if library:
        blocks.append("\n".join([PROJECT_LIBRARY_HEAD, *library]))
    return "\n\n".join(blocks)


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


# 记忆 (core/memory.py), when COVEY_MEMORY=1: one block of what the owner said and what the project noted, in place of the
# principles', the knowledge entries' and the project memory's own blocks; and, on a turn whose owner's words carry a
# 以后 / 每次 / 别再 …, the rule for writing ``memory`` and ``memory_void`` into the checkpoint (app/memory_collect.py reads them).
MEMORY_HEAD = "记忆（主人说过的话，和专项里记下的约定；每条前面是它的编号、管哪儿、谁说的；做事时照着办）："
MEMORY_RULES = (
    "怎么用这些记忆：谁说了算，主人这一轮的话最大，其次是标“你说的”的，再其次是标“它总结的”的；同一类里管得窄的压过管得宽的，新的压过旧的。",
    "标“它总结的”的，默认照做；和主人的话冲突时让路。",
    f"标“它总结的”的，不能成为多问主人的理由；{ASK_SHORT}这些要主人点头的事，不看这里，照常问。",
    "这里的记忆和你自己记下的笔记冲突时，以这里为准。",
    "要问主人之前，先看标“你说的”的记忆：主人已经同意的事（比如测试通过就合并、空闲时直接部署）照着做，不要再问。",
)
MEMORY_LAYER_RULES = (  # With a project's lines in the block: what its three parts are (core/memory.py PARTS).
    "专项的记忆分三段：“长期约定”是主人明说以后都照做的；“当前决定”是主人答过的问题，一题一条，只管那道题问的范围"
    "（哪一版、哪一批、什么目标），同一范围里照着做、重试不用再问，换了批次、目标或扩大了范围，不能当作主人已经同意，照常问；"
    "“待核对参考”是负责人整理的口径。",
    "标“待核对参考”的可以照着做，但它不是主人的原话：和主人的话、长期约定、当前决定冲突时让路，也不能单独作为替主人答题、"
    "或做要主人点头的事的依据。",
)
NO_ORIGIN_RULE = "标“你说的（无出处）”的是没留下原问题或原话出处的旧记录，条件可能不全，拿不准就照常问主人。"
# 记忆自己长 (COVEY_MEMORY_LEARN=1, app/memory_turn.py learn): where to look up what the block left out, and which lines were used.
MEMORY_SEARCH_RULE = ("查记忆：放不下的、整理时收起的、以前问过主人的问题和他的回答，都能用这个命令查（只读）：{command}。"
                      "要问主人之前，先查一下他答过没有。")
MEMORY_USED_RULE = ("这一轮照着哪几条记忆做了（或因为它没有再问主人），在检查点里写 memory_used：[\"M12\"]，只写编号；"
                    "Bridge 据此决定哪些留着，不用在回复里提。")
MEMORY_FIX_RULE = (
    "记忆纠正：主人这一轮的话里说了“以后”“每次”“别再”“不要再”“记住”这类长期适用的话，才在检查点里加 memory："
    '[{"quote":"主人原话里的一段","text":"一句话，最多 200 字，把“这个”“那个”写成所指的事"}]，最多 3 条；'
    "上面标“负责人记的”的某条被主人这一轮的话否定了，再加 memory_void："
    '[{"id":"短码，如 R7K2M","quote":"主人原话里的一段"}]。quote 必须是主人这一轮原话里连续的一段，不是你的转述。'
    "只记主人说的，不记“要先问主人”；主人没明说就不要写。主人的话只管一类事时（如“监视进程这类用 Codex”），text 照写那一类，不扩大。"
)


def memory_line(item, new=False) -> str:
    """One item of the memory block: "- M12 · 管这个专项 · 当前决定 · 你说的：…" (a project's line says its part, and
    ``item_text`` what else it knows); ``new``: written since the topic's previous turn."""
    part = core_memory.PART_WORDS.get(item.get("part")) if item["scope"] == core_memory.PROJECT else None
    return (f"- {item['n']} · 管{core_memory.WORDS[item['scope']]} · " + (f"{part} · " if part else "")
            + f"{core_memory.OWNER_WORDS[item['by']]}{NO_ORIGIN if unsourced(item) else ''}：{item_text(item)}" + ("（新）" if new else ""))


def memory_block(kept, left: int = 0, project=None, *, learn=None) -> str:
    """The memory block of a turn: its items (core/memory.py select), each a ``memory_line``, then how many were left out; with
    ``project`` (core/memory.py project_view of the turn's project) what its parts left out, the lines voided since the topic's
    previous turn and the version; then the rules that say who counts (with a project's lines, what its parts are, and of an
    older line of the owner's without an origin). ``learn`` (记忆自己长, app/memory_turn.py): {search}, the command that looks
    the memory up ('' none), and the rule asking which lines were used. '' with nothing to say."""
    view = project or {"items": [], "voided": [], "new": set()}
    if not kept and not view["voided"]:
        return ""
    search = (learn or {}).get("search") or ""
    lines = [MEMORY_HEAD, *(memory_line(item, item.get("ref") in view["new"]) for item in kept)]
    if left:
        lines.append(f"另有 {left} 条放不下，没有列出" + ("，要用时按下面的命令查。" if search else "。"))
    if project is not None and left_parts(project):
        lines.append(left_parts(project))
    if view["voided"]:
        lines += [VOIDED_HEAD, *(f"- {core_memory.PART_WORDS[item['part']]}：{item['text']}" for item in view["voided"])]
    if view["items"] or view["voided"]:
        lines.append(f"专项记忆版本 {view['version']}。")
    projects_lines = [item for item in (*kept, *view["voided"]) if item["scope"] == core_memory.PROJECT]
    learned = ([MEMORY_SEARCH_RULE.format(command=search)] if search else []) + ([MEMORY_USED_RULE] if learn is not None else [])
    return "\n".join([*lines, *MEMORY_RULES, *(MEMORY_LAYER_RULES if projects_lines else ()),
                      *((NO_ORIGIN_RULE,) if any(unsourced(item) for item in kept) else ()), *learned])


# 记忆 from the one store (core/memstore.py, app/memory_turn.py): the 规矩 in the rules, the same every turn of a topic while they
# are (the rules stay one cacheable prefix); what changes turn by turn (the decisions in progress, this turn's notes, the titles of
# the related entries not carried) at the tail of the turn's message.
RULES_HEAD = "记忆 · 规矩（照做；和主人这一轮的话冲突时听这一轮的）："
RULES_SCOPES = {"global": "全局", "project": "本专项", "topic": "本话题"}
RULES_GUIDE = (f"怎么用：标“负责人记的”是负责人整理、主人还没确认的，照做，但不能单独作为{ASK_SHORT}这类要主人点头的事的依据。"
               "每轮消息末尾另有这一轮的决定和资料：决定只管它问的那一题、那个范围，换了批次或范围照常问；资料是参考。"
               "这一轮照着哪几条做了（或因此没再问主人），在检查点 memory_used 写短码，如 [\"R7K2M\"]。")
RULES_SEARCH = "查记忆（只读）：{command}；看一条的全文把 search \"<一句话>\" 换成 show <短码>。要问主人之前，先查一下他答过没有。"
TAIL_HEAD = "记忆 · 这一轮（决定只管它问的那一题、那个范围；资料是参考，和主人这一轮的话冲突时听这一轮的）："
TAIL_LISTED = "相关但没带的：{items}（要用时按规矩块里的命令 show <短码> 看全文）"


def memory_entry_line(entry, note="") -> str:
    """"- R7K2M 正文（负责人记的）": one entry of the memory blocks; ``note`` what else is said of it."""
    tags = [part for part in ("负责人记的" if entry.said != "owner" else "", note) if part]
    return f"- {entry.code} {entry.text}" + (f"（{' · '.join(tags)}）" if tags else "")


def memory_rules_block(rules, left=(), *, project_name: str = "", search: str = "") -> str:
    """The 规矩 a turn's rules carry (app/memory_turn.py rules_of), by scope, then the titles of those past their scope's room
    and how to use them; ``search`` the command that looks the memory up ('' none: a device's topic). '' without a rule."""
    if not rules and not left:
        return ""
    lines = [RULES_HEAD]
    for scope, word in RULES_SCOPES.items():
        mine = [entry for entry in rules if entry.scope == scope]
        if mine:
            lines.append(f"{word}「{project_name}」" if scope == "project" and project_name else word)
            lines += [memory_entry_line(entry) for entry in mine]
    if left:
        lines.append(f"还有 {len(left)} 条规矩放不下，只列标题（要用时看全文）："
                     + "；".join(f"{entry.code} {entry.title}" for entry in left))
    lines.append(RULES_GUIDE)
    if search:
        lines.append(RULES_SEARCH.format(command=search))
    return "\n".join(lines)


def _day(at) -> str:
    return _dt.datetime.fromtimestamp(at).strftime("%m-%d") if at else ""


def memory_tail_block(decisions=(), notes=(), listed=(), *, numbers=None, project: str = "", names=None) -> str:
    """The memory's part of a turn's tail (app/memory_turn.py tail): the decisions in progress, each with the topic it was
    asked in and its day (``numbers``: topic id -> T number); the notes (Entry, sim) with where they come from (``names``:
    project id -> name, of another project's); the related entries not carried, a code and a title each. '' with none."""
    numbers, names = numbers or {}, names or {}
    if not decisions and not notes and not listed:
        return ""
    lines = [TAIL_HEAD]
    if decisions:
        lines.append("决定（本话题的、本专项在办的）")
        for entry in decisions:
            asked = str((entry.origin or {}).get("topic_key") or (entry.owner_key if entry.scope == "topic" else ""))
            where = f"T{numbers[asked]}" if numbers.get(asked) else ""
            text = entry.text if len(entry.text) <= 240 else entry.text[:239] + "…"
            lines.append(f"- {entry.code} {text}" + (f"（{' · '.join(p for p in (where, _day(entry.at)) if p)}）"
                                                       if where or entry.at else ""))
    if notes:
        lines.append("资料（和这一轮相关的）")
        for entry, _ in notes:
            where = ""
            if entry.scope == "global":
                where = "全局"
            elif entry.scope == "project" and entry.owner_key != project:
                where = f"来自专项「{names.get(entry.owner_key) or entry.owner_key}」"
            repos = "[" + "、".join(entry.repos) + "] " if entry.repos else ""
            checked = f"核验 {_day(entry.verified_at)}" if entry.verified_at else ""
            tags = [part for part in (where, "负责人记的" if entry.said != "owner" else "", checked) if part]
            lines.append(f"- {entry.code} {repos}{entry.text}" + (f"（{' · '.join(tags)}）" if tags else ""))
    if listed:
        lines.append(TAIL_LISTED.format(items="；".join(f"{item.get('code') or item['ref']} {item['title']}" for item in listed)))
    return "\n".join(lines)

# COVEY_WORKSPACE_GISTS (app/workspace_gists.py): a new topic's rules carry the 话题要点 of the workspace's last topics.
WORKSPACE_GISTS_HEAD = ("工作区 {name} 最近几个话题的要点（只是背景：和这次的事有关时参考，无关就不管；"
                        "要细节去那个话题看，不要当成主人这一轮的要求）：")


def workspace_gists_block(workspace_name: str, lines) -> str:
    """The gists of the workspace's recent topics, a line each (app/workspace_gists.py lines); '' with none."""
    lines = list(lines)
    return "\n".join([WORKSPACE_GISTS_HEAD.format(name=workspace_name), *lines]) if lines else ""


def bridge_rules(
    *,
    backend_name: str,
    workspace_name: str,
    workdir: Path,
    runtime_note: str = "",
    notes: tuple[str, ...] = (),
    principles: str = "",
    project: str = "",
    knowledge_text: str = "",
    remember: bool = False,
    device: str = "",
    remote_workdir: str = "",
    memory_text: str = "",
    memory_fix: bool = False,
    proxy: bool = False,
    gists: str = "",
    checkpoint: str | None = None,
    home: str = "",
) -> str:
    """``proxy`` (a 代答 topic's): its own constraints and the workspace, nothing else (proxy_constraints). Otherwise
    identity and constraints, the workspace, the owner's principles (``principles``, principles_block; none: no
    block), the project's memory and library (``project``, project_block; none: no block), the owner's knowledge entries (``knowledge_text``, knowledge_block; none: no block),
    a new session's recent topics of the workspace (``gists``, workspace_gists_block; none: no block), the checkpoint
    contract (``checkpoint``, core/checkpoint.py checkpoint_rules; None: all of it; ``remember``: and the rule for writing
    entries): the same every turn of a topic. Static only, so the rules stay one cacheable prefix: what changes turn by turn goes to the prompt's
    tail (``turn_tail``). ``home``: the home block (app/home_turn.py static_block) in place of the workspace block."""
    where = home_section(workspace_name, workdir, device, remote_workdir, home=home)
    if proxy:
        return "\n\n".join((proxy_constraints(backend_name, runtime_note, notes), where))
    return "\n\n".join((constraints(backend_name, runtime_note, notes), where,
                        *memory_sections(memory_text=memory_text, principles=principles, project=project,
                                         knowledge_text=knowledge_text),
                        *((gists,) if gists else ()),
                        checkpoint_rules() if checkpoint is None else checkpoint,
                        *((REMEMBER_RULE,) if remember else ()), *((MEMORY_FIX_RULE,) if memory_fix else ())))


def home_section(workspace_name: str, workdir: Path, device: str = "", remote_workdir: str = "", *, home: str = "") -> str:
    """主目录段 of the rules: ``home`` (app/home_turn.py static_block) when given, else the workspace block as before."""
    return home or workspace_block(workspace_name, workdir, device, remote_workdir)


def memory_sections(*, memory_text: str = "", principles: str = "", project: str = "", knowledge_text: str = "") -> tuple[str, ...]:
    """记忆段 of the rules, in their order (the memory's 规矩, memory_rules_block; the principles and the project's memory,
    only with COVEY_MEMORY off; the project's library; the knowledge entries' history line), the empty ones left out. Static
    within a topic: the memory's part that changes turn by turn is memory_tail_block, at the message's tail."""
    return tuple(part for part in (memory_text, principles, project, knowledge_text) if part)


def turn_tail(*parts: str) -> str:
    """每轮动态块: the parts of a turn that change turn by turn (decisions in progress, this turn's
    notes, the titles not carried: app/memory_turn.py tail; the repositories: app/repos_turn.py tail), at the prompt's
    tail beside the checkpoint note, never in the rules; '' when every part is."""
    return "".join("\n\n" + part for part in parts if part)


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


RECENT_HEAD = "最近的对话："  # app/switch_handoff.py names it.


def first_turn_message(user_text: str, history_snapshot: list[dict[str, Any]]) -> str:
    history = render_history(history_snapshot)
    return "\n".join((
        f"当前时间：{_now()}",
        "",
        RECENT_HEAD,
        history or "（无）",
        "",
        "用户最新的消息：",
        user_text,
    ))


def resume_message(user_text: str) -> str:
    """A message continuing an existing Harness session: the session already has the conversation."""
    return "\n".join((
        f"当前时间：{_now()}",
        "",
        "用户最新的飞书消息：",
        user_text,
    ))


# The butler's rules (app/butler.py), at the head of each of its turns' messages: the owner wants the
# Bridge's rules in the user message, not the system prompt. Without "do the work in a topic" a single session in
# DeepSeek's experiment never handed any work on. One layout (core/placement.py), the 话题区: the butler's card
# in the owner's message's thread, answers and conclusions new messages in the chat area. The rules of the retired
# front-desk layout (desk_topics) went with it.
AREA_OPENING = (
    "- 你是主人的管家。主人在私聊聊天区发的每条消息是你的一轮；主人在话题里发的消息直接交给那个话题，不经过你。",
    "- 一问一答：你这一轮的回复就是给主人的答案，Bridge 把它作为聊天区的一条新消息发出，顶上链回主人那句话。"
    "结论先行，简短；不要解释卡片、话题串这些摆放；确实要主人动手时，用一句话说点哪里。",
    # A question asked only in an answer's last sentence is easily missed.
    f"- 要主人定的事（要不要做、{ASK_SHORT}这类），不要只在回复末尾问一句：写进本任务检查点的"
    " requires_user、question 和 questions（每题一个对象，能给选项就给），Bridge 会出问题卡、放进“等你”。"
    "回复里照常先说结论和依据，再一句话说要他定什么。不答也不耽误的顺口确认不用问。"
    "只有“这句话该交给哪个话题”拿不准时不写检查点，照下面说的用 AskUserQuestion 当场问：这一轮还要带着主人这句话转交。",
    # How is the butler's to decide, what is the owner's (collie's "Drive it forward"); a question leads with a lean.
    f"- 怎么做由你定：先查什么、交给哪个话题、失败了换个办法再试。做什么、{ASK_SHORT}这几类由主人定。"
    "要问时先说你倾向怎么做。",
    # The owner reads this chat only (Persona's 用户只看这个对话): a bare T number or "see the topic" says nothing to him.
    "- 写给主人的话，当他没看过别处：提到话题时写它在做的事，T 号放在后面的括号里，如“迁移账单（T432）”，不要只写 T 号；"
    "话题里得出的数字和结论直接写进回复，不要只让他去话题里看。",
    # Nothing wakes the butler after its turn (ClaudePartner's "never say later unless something brings you back").
    "- 这一轮结束后你不会自己醒来。没有在跑的话题会把结果带回来时，不要说“稍后”“等下”；没当场答完的事，派进话题再结束。",
)
# 派活门槛 (core/butler_tool_specs.py DISPATCH_OWN, DISPATCH_BUTLER): what goes to a topic, in topic_start's words.
DISPATCH_HEAD = f"其余的事都交给话题去做，不要在自己这一轮里做：{DISPATCH_OWN}。"
SHARED_RULES = (
    "- 只读的、一两分钟能做完的查询和命令（看一眼文件或日志、查配置、连得上吗、跑个只读命令），自己用工具做完直接答，不开话题。",
    "- " + DISPATCH_HEAD +
    "放哪儿按“是哪件事”分，不按它会跑多久：新的事用 Bridge 工具 topic_start 开一个话题；"
    "和已有话题是同一件事（主人提了 T 号、在说刚才那件、同一个仓库里的同一件事，几天前的也算），"
    "用 topic_send 追加，在跑的作为补充，停了的开下一轮。下面说的工具都是 Bridge 工具（MCP 服务 bridge 的工具，名字是 mcp__bridge__<工具名> 或 mcp_bridge_<工具名>）。",
    "- 用 topic_start、topic_send、topic_fork 交给话题时，写干净的任务说明：主人原话和附件 Bridge 会自动附上，"
    "你不用抄，也不要写路由的话、不要抄附件的本机路径。",
    "- 一句话里有几件独立的事，就开几个话题。话题都从主目录开工，不用选工作区；要改代码的，把用到的仓库写进 repos（repos_list 里的名字，要改的写 write），拿不准就问主人。",
    "- 两个以上话题都可能是这件事时不要猜：用 AskUserQuestion 出一道单选题，选项是候选话题（T 号和标题）和“另开”。"
    "需要已有话题的上下文、又不是同一件事的，用 topic_fork 分叉。"
    "追问只是问已有结果、不用新查新做的，用 topic_read 读了自己答，不转交；要新查、新做的才 topic_send。",
    "- 主人说某个文档、文件、结果是之前话题给的，先用 topics_search 按链接、token、路径找是哪个话题的哪一轮，"
    "没命中再读文档取标题或正文里的词搜；找到了用 topic_read 的 job 读那一轮，再决定追加还是新开；找不到就说没找到来源。",
    "- 主人说“T412 选 A”这类话，用 question_answer 代为回答；说“T412 急”，用 topic_priority。",
    # W0-7 (T892): "不用管了" only took the topic off the board, and its task ran on. Stopping can be undone (a word in the
    # topic goes on), running on cannot, so a topic still at work is stopped when it is not clear which he meant.
    "- 主人说“T412 停掉”“别做了”“不要了”，要的是它不再做：用 topic_stop 真停掉（排队和在跑的任务停下、等主人回答的问题收掉），"
    "话题还在，回复里说一句“要接着做，在话题里说一句”。只说“从看板撤下”“看板上别显示”的，用 topic_priority 并给 remove: true，"
    "回复里写明它的任务照跑、只是不在看板上。说“T412 不用管了”拿不准是哪种：快照里它在跑、排着队或在等主人回答的，"
    "先用 topic_stop 停掉再从看板撤下；已经停着的只从看板撤下。专项的话题不用 topic_stop，照下面专项那条交给负责人停。",
    "- 要看或改管家的设置，用 settings_panel 把设置卡发出来；设置只有主人能在卡上改。",
    "- 主人说出长期适用的原则或偏好（“以后……”“原则是……”“记住……”“都要……”）时，用 memory_save 记成一条规矩"
    "（kind rule；处处适用的 scope 写 global，只对某个仓库成立的在 repos 里写仓库名），回复里说一句“已记下：……”和它的短码；"
    "只对这件事、这一轮的要求不记。主人说“忘掉/删掉那条”时，用 memory_search 找到它的短码，再用 memory_revise（action retire）收起。"
    "记下的规矩每一轮都附在 Bridge 的规则里，你和各个话题都会看到。",
)
# 管家 (COVEY_BUTLER_RULES, on unless 0; docs/butler.md 管家规则): the butler only answers quickly and hands the rest on, so
# it is free for the owner's next question. It takes the place of SHARED_RULES' first two rules (everything under a
# minute or two is done in place; what goes to a topic area is what may take over a minute or two); the other rules are
# the same. Off, the rules are exactly SHARED_RULES. BUTLER_RULES_ENV is core/butler_tool_specs.py's: the tool list reads it too.
BUTLER_RULES = (
    "- 你只做快答和分派，不在自己这一轮里干几分钟的活，这样才接得住主人的下一句。"
    "快答：不用工具，或只用一两次只读的查询（看一眼文件或日志、查一项配置、topic_read、topics_list），几秒内就能答完的，直接答，不开话题。"
    f"其余的都派出去，不要先自己试一试：{DISPATCH_BUTLER}。"
    "新的事用 topic_start 开话题；和已有话题是同一件事的用 topic_send（见下一条）。"
    "拿不准是快答还是要干活的，派出去。"
    "主人点名用哪个 Harness、模型或思考预算时（“用 Claude Code”“让 Codex 做”），开话题的 topic_start 里照填；没点名就不填。"
    "原则或记忆里“某一类事用某个模型”只管那一类事，别的事不套用；填了就在 basis 写依据（主人原话或编号）。"
    # No time of its own guessing (AREA_RULE: a topic's estimate is the snapshot's, or none yet).
    "派出去后只对主人说一句放到了哪个 T 号，例如“放到 T432「标题」里做”；快照里有预计才报时间。"
    "例外：主人明确说“你直接做”“别开话题”“就在这儿做”时，这一轮自己做完再答；没有这样的话，一律照上面分。",
    # SHARED_RULES' second rule without its list of what goes to a topic: the line above gives the butler's (派活门槛 once).
    SHARED_RULES[1].replace(DISPATCH_HEAD + "放哪儿", "派出去的事放哪儿"),
)
assert BUTLER_RULES[1] != SHARED_RULES[1]


def butler_on(environ=None) -> bool:
    """Whether the butler's rules are on: COVEY_BUTLER_RULES unset or anything but 0/off/false/no (read on every call)."""
    return env_flag(BUTLER_RULES_ENV, True, environ)


def shared_rules(environ=None) -> tuple[str, ...]:
    """SHARED_RULES, the butler's two rules in place of the first two unless COVEY_BUTLER_RULES is off."""
    return BUTLER_RULES + SHARED_RULES[2:] if butler_on(environ) else SHARED_RULES


# "进度主人看 Pin" is said only where there is a Pin to look at: 手上的事 (COVEY_PINNED_OVERVIEW=1, app/overview.py),
# which a new install does not switch on (``area_rule``).
PIN_WORDS = "进度主人看 Pin。"
PINNED_OVERVIEW_ENV = "COVEY_PINNED_OVERVIEW"  # app/overview.py ENV: that module imports the app, this one must not.
AREA_RULE = ("- 交给话题后，你只用一句话说放到了哪里：新开的写“放到 T432「标题」里做”，追加的写“接着在 T432 里做”。"
             "不写进度，不写“结论会带回来”之类的话，也不用再报结论：话题每一轮结束，Bridge 都会把它的结论作为新消息发到聊天区；"
             + PIN_WORDS + "被问到时用下面的快照和 topic_read 回答。"
             "主人问某个话题“咋样了”“还要多久”“是不是卡住了”，快照里在做的那一行写着阶段、最近动静和预计还要多久，"
             "直接照它回答，不用为此开话题或去查；没写预计的就说还估不出来，标着“可能卡住了”的照实说，"
             "并告诉主人可以点那张任务卡上的「看看」。")
assert PIN_WORDS in AREA_RULE


def area_rule(environ=None) -> str:
    """AREA_RULE, without its "进度主人看 Pin" when 手上的事 is not pinned (COVEY_PINNED_OVERVIEW off; read on every
    call): an owner without the Pin was sent to look at one."""
    pinned = env_flag(PINNED_OVERVIEW_ENV, True, environ)
    return AREA_RULE if pinned else AREA_RULE.replace(PIN_WORDS, "")


def knowledge_rules() -> tuple[str, ...]:
    """The butler's rules for the knowledge entries (COVEY_KNOWLEDGE=1) and the history search
    (COVEY_HISTORY_CMD); none when neither is on, so the rules are then what they were."""
    rules = []
    if knowledge.enabled():
        rules += [
            "- 主人说“记一下……”“记下来”，用 memory_save 记一条：一件具体的事实、做法或坑记资料（kind note），定下的事记决定"
            "（kind decision），以后每次都要照做的记规矩（kind rule）；通用的 scope 写 global，只关某个仓库的在 repos 里写仓库名。"
            "主人说“把 T483 的结论记一下”，先用 topic_read 读那一轮再起草。记完在回复里列出短码和正文。",
            "- 主人说“改成……”用 memory_revise（action update 或 supersede，给原因），说“删了”“作废”用 memory_revise（action retire）；"
            "先用 memory_search 找到短码。只记主人说的，不要自己总结着记。"]
    if knowledge.history_cmd():
        order = "memory_search → topics_search → history_search" if knowledge.enabled() \
            else "topics_search → history_search"
        rules.append(f"- 查以前做过的事、之前的决定，按顺序：{order}，前一个找到了就不用查后一个。"
                     "history_search 查的是 Claude、Codex 和 Bridge 的全部历史轮次，给一句话描述；结果里的原文位置可以再用它的输出追问。")
    elif knowledge.enabled():
        rules.append("- 查以前做过的事、之前的决定，按顺序：memory_search → topics_search，前一个找到了就不用查后一个。")
    return tuple(rules)


PROJECT_RULE = (
    "- 聊天里有专项时（快照的“专项”一栏列着在做的和收起的，带 id、名字和一句目标），每件新的事先看它是不是其中某个专项的同一件事："
    "是的，用 project_send 交给那个专项的负责人（pid 用快照里的 id），收起的专项会随之重新展开，不要另开话题；"
    "拿不准是不是同一件事，就开普通话题（topic_start），不要硬归；主人说“这是 X 的事”“放到 X 里”，就归进 X，不用再判断。"
    "做完的专项不在快照里，不往里交。"
    "主人要停专项里的某个话题（T 号属于专项的话题）时，也用 project_send 交给负责人，写明停哪个话题；负责人会停掉它的任务、收掉它等主人回答的问题，你自己不动它。")


# A topic hands the butler what only it can do (app/butler_asks.py, COVEY_PROJECTS=1): its message starts with HEAD.
ASKED_RULE = (
    "- 以“↪ T12 转给管家”开头的消息，是话题 T12 替主人转来的、只有你能办的事（开专项、把话题收进专项、另开话题、交给别的话题）："
    "“你在 T12 说”后面是主人在 T12 里的原话，“T12 的理解”只作参考。以主人原话为准，原话没要的不做。"
    "开专项时把 T12 放进 topic_start 的 topics；快照或 projects_list 里已有做同一件事的专项，就用 project_send 交给它、把 T12 放进 topics 收进去；"
    "两个以上对得上或拿不准时先问主人。不要再转回 T12，也不要请主人再说一遍。")


def project_rules() -> tuple[str, ...]:
    """The butler's rules for putting a new matter into a project that already exists and for what a topic hands it
    (COVEY_PROJECTS=1); none without it, so the rules are then what they were."""
    return (PROJECT_RULE, ASKED_RULE) if projects.enabled() else ()


# The owner asking how to use Covey itself: the butler reads the running release's own one-page guide first (a release
# is the core's git archive, docs/ and all) instead of guessing or reading the code.
QUICKSTART = Path(__file__).resolve().parents[2] / "docs" / "quickstart.md"
USAGE_RULE = f"- 主人问 Covey 本身怎么用、能做什么（看板在哪、怎么开记忆、有什么特别的这类），先读 {QUICKSTART} 再答，不要凭印象说。"


def butler_rules() -> str:
    """The butler's rules."""
    return "\n".join(("[管家规则]", *AREA_OPENING, *shared_rules(), *knowledge_rules(), USAGE_RULE, *project_rules(),
                      area_rule()))


BUTLER_RULES_TEXT = butler_rules()

# #34: a butler turn that has made this many tool calls of its own, none of them the Bridge's, is reminded once to hand
# the rest to a topic (backends/claude/hooks.py, its context's ``tool_nudge``: app/butler.py butler_turn); not a turn whose
# owner said to do it right there (the butler rules' 例外).
TOOL_NUDGE_CALLS = 6
TOOL_NUDGE_TEXT = (f"[Bridge 提醒] 这一轮你已经自己调了 {TOOL_NUDGE_CALLS} 次工具。剩下的交给话题：新的事用 topic_start，"
                   "同一件事用 topic_send，对主人说一句放到了哪里就结束这一轮，好接住他的下一句；快做完了就做完再答。")
DIRECT_WORDS = re.compile(r"你直接做|直接做吧|你自己做|别开话题|不要开话题|不用开话题|就在这儿做|就在这里做")


def tool_nudge(text) -> dict:
    """The butler turn's ``tool_nudge`` (backends/claude/hooks.py): when and what to remind it; {} when the owner's words
    (``text``) say to do it right there (DIRECT_WORDS)."""
    if DIRECT_WORDS.search(str(text or "")):
        return {}
    return {"after": TOOL_NUDGE_CALLS, "text": TOOL_NUDGE_TEXT}


def butler_message(snapshot: str, message: str, note: str = "") -> str:
    """A butler turn's message: its rules, the snapshot of the owner's topics, then the turn's own message.
    ``note``: a track's parallel note (app/butler_tracks.py), between the snapshot and the message."""
    return "\n\n".join((butler_rules(), snapshot, *((note,) if note else ()), message))
