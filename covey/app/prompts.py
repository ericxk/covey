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
from covey.core import env as cenv
from covey.core import extensions, knowledge, projects
from covey.core import memory as core_memory
from covey.core.checkpoint import checkpoint_rules
from covey.core.butler_tool_specs import BUTLER_RULES_ENV

# The one rule about language (5.9 row 13: said once in a turn's rules, which are all in Chinese). Commands' and subtasks'
# descriptions show on the task card's 执行过程 (core/text.py step_action): the owner reads them, so they are in his
# language too.
LANGUAGE_RULE = ("写给人看的话都用用户提问的语言，用户用中文就写中文：最终回复、进度说明、总结，以及工具调用里写给人看的说明"
                 "（命令和子任务的 description、待办条目，它们会显示在飞书任务卡的“执行过程”里）。")

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
    "报告留在本地，用户要求发布时才发；不要选全局最新的评审结果。",
    "只回复要发回飞书的消息正文，用适合飞书的 Markdown：短小节、列表、编号步骤，需要时用代码块。",
    "要确认之前，先把用户要的方案和决策背景写完整，不要缩成一句提问。",
)
RESUME_REMINDERS = "\n".join((*RESUME_HEAD, SEARCH_RULE, LANGUAGE_RULE))


# A topic's resumed turn whose previous turn left the model's context over FULL_SHARE of its window (the CLI compacts
# only near the end): one line telling it to bring the checkpoint up to date first (``context_full``).
FULL_SHARE = 0.6
CONTEXT_FULL = "上下文快满了：先把检查点的 completed 和 next_step 更新到最新，再接着做。"


def context_full(record: dict | None) -> str:
    """CONTEXT_FULL when ``record`` (the previous turn's) holds a context size over FULL_SHARE of the window the
    backend reported for that model; '' for anything else, a window not known included."""
    tokens, window = (record or {}).get("context_tokens"), (record or {}).get("context_window")
    if type(tokens) is int and type(window) is int and window > 0 and tokens >= FULL_SHARE * window:
        return CONTEXT_FULL
    return ""


def resume_reminders() -> str:
    """RESUME_REMINDERS, with an enabled extension's ``search_reminder`` in place of SEARCH_RULE."""
    return "\n".join((*RESUME_HEAD, extensions.setting(SEARCH_REMINDER_SETTING, "").strip() or SEARCH_RULE, LANGUAGE_RULE))


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
        "- 不要自动把过程、diff 或评审文档发到飞书，报告留在本地，用户要求发布时才发；不要选别的话题最新的评审结果。",
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
# 以后这类按推荐走 (app/standing_answers.py) records the owner's standing go-ahead as a principle: what it allows is not
# asked again. Said in the principles' head and, with COVEY_MEMORY=1, in the memory's rules.
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
PROJECT_CHARS = 3000  # Of the memory's lines; the oldest go first past it. The library's index has its own 2 KB.
PROJECT_MEMORY_HEAD = ("专项记忆（这个话题属于专项「{name}」；下面是专项里已经定下的事，做事时照着办，"
                       "和主人这一轮的话冲突时以这一轮为准）：")
PROJECT_LIBRARY_HEAD = "专项资料库（主人给专项的文件，和专项各话题的产物、交付；需要时按路径去读，设备上的话题读不到本机的文件）："


def project_block(name, memory=(), library=(), limit: int = PROJECT_CHARS) -> str:
    """The project's memory and library a topic of it reads: ``memory`` (lines, oldest first; past ``limit`` characters the
    oldest are left out and a line says how many), then ``library`` (the index's lines); '' when both are empty."""
    kept, used = [], 0
    for line in reversed(list(memory)):
        if used + len(line) + 1 > limit:
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
    "标“它总结的”的，不能成为多问主人的理由；部署、删除、花钱这些要主人点头的事，不看这里，照常问。",
    "这里的记忆和你自己记下的笔记冲突时，以这里为准。",
    "要问主人之前，先看标“你说的”的记忆：主人已经同意的事（比如测试通过就合并、空闲时直接部署）照着做，不要再问。",
)
MEMORY_FIX_RULE = (
    "记忆纠正：主人这一轮的话里说了“以后”“每次”“别再”“不要再”“记住”这类长期适用的话，才在检查点里加 memory："
    '[{"quote":"主人原话里的一段","text":"一句话，最多 200 字，把“这个”“那个”写成所指的事"}]，最多 3 条；'
    "上面标“它总结的”的某条被主人这一轮的话否定了，再加 memory_void："
    '[{"id":"编号，如 M12","quote":"主人原话里的一段"}]。quote 必须是主人这一轮原话里连续的一段，不是你的转述。'
    "只记主人说的，不记“要先问主人”；主人没明说就不要写。"
)


def memory_block(kept, left: int = 0) -> str:
    """The memory block of a turn: its items (core/memory.py select), each "- M12 · 管这个专项 · 你说的：…", then how many
    were left out and the four rules that say who counts; '' with none."""
    if not kept:
        return ""
    lines = [MEMORY_HEAD]
    lines += [f"- {item['n']} · 管{core_memory.WORDS[item['scope']]} · {core_memory.OWNER_WORDS[item['by']]}：{item['text']}" for item in kept]
    if left:
        lines.append(f"另有 {left} 条放不下，没有列出。")
    return "\n".join([*lines, *MEMORY_RULES])


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
) -> str:
    """``proxy`` (a 代答 topic's): its own constraints and the workspace, nothing else (proxy_constraints). Otherwise
    identity and constraints, the workspace, the owner's principles (``principles``, principles_block; none: no
    block), the project's memory and library (``project``, project_block; none: no block), the owner's knowledge entries (``knowledge_text``, knowledge_block; none: no block),
    a new session's recent topics of the workspace (``gists``, workspace_gists_block; none: no block), the checkpoint
    contract (``checkpoint``, core/checkpoint.py checkpoint_rules; None: all of it; ``remember``: and the rule for writing
    entries): the same every turn of a topic."""
    if proxy:
        return "\n\n".join((proxy_constraints(backend_name, runtime_note, notes),
                            workspace_block(workspace_name, workdir, device, remote_workdir)))
    return "\n\n".join((constraints(backend_name, runtime_note, notes),
                        workspace_block(workspace_name, workdir, device, remote_workdir), *((memory_text,) if memory_text else ()),
                        *((principles,) if principles else ()),
                        *((project,) if project else ()),
                        *((knowledge_text,) if knowledge_text else ()), *((gists,) if gists else ()),
                        checkpoint_rules() if checkpoint is None else checkpoint,
                        *((REMEMBER_RULE,) if remember else ()), *((MEMORY_FIX_RULE,) if memory_fix else ())))


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
    "- 要主人定的事（选哪个、要不要交给谁、要不要做、部署删除花钱这类），不要只在回复末尾问一句：写进本任务检查点的"
    " requires_user、question 和 questions（每题一个对象，能给选项就给），Bridge 会出问题卡、放进“等你”。"
    "回复里照常先说结论和依据，再一句话说要他定什么。不答也不耽误的顺口确认不用问。"
    "只有“这句话该交给哪个话题”拿不准时不写检查点，照下面说的用 AskUserQuestion 当场问：这一轮还要带着主人这句话转交。",
    # How is the butler's to decide, what is the owner's (collie's "Drive it forward"); a question leads with a lean.
    "- 怎么做由你定：先查什么、交给哪个话题、失败了换个办法再试。做什么、不可逆的、对外可见的、花钱的由主人定。"
    "要问时先说你倾向怎么做。",
    # The owner reads this chat only (Persona's 用户只看这个对话): a bare T number or "see the topic" says nothing to him.
    "- 写给主人的话，当他没看过别处：提到话题时写它在做的事，T 号放在后面的括号里，如“迁移账单（T432）”，不要只写 T 号；"
    "话题里得出的数字和结论直接写进回复，不要只让他去话题里看。",
    # Nothing wakes the butler after its turn (ClaudePartner's "never say later unless something brings you back").
    "- 这一轮结束后你不会自己醒来。没有在跑的话题会把结果带回来时，不要说“稍后”“等下”；没当场答完的事，派进话题再结束。",
)
SHARED_RULES = (
    "- 只读的、一两分钟能做完的查询和命令（看一眼文件或日志、查配置、连得上吗、跑个只读命令），自己用工具做完直接答，不开话题。",
    "- 其余的事都交给话题去做，不要在自己这一轮里做：可能超过一两分钟的、要改代码或文件的、主人说开话题的。"
    "放哪儿按“是哪件事”分，不按它会跑多久：新的事用 Bridge 工具 topic_start 开一个话题；"
    "和已有话题是同一件事（主人提了 T 号、在说刚才那件、同一个仓库里的同一件事，几天前的也算），"
    "用 topic_send 追加，在跑的作为补充，停了的开下一轮。下面说的工具都是 Bridge 工具（MCP 服务 bridge 的工具，名字是 mcp__bridge__<工具名> 或 mcp_bridge_<工具名>）。",
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
    # No time of its own guessing (AREA_RULE: a topic's estimate is the snapshot's, or none yet).
    "派出去后只对主人说一句放到了哪个 T 号，例如“放到 T432「标题」里做”；快照里有预计才报时间。"
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
    """AREA_RULE, without its "进度主人看 Pin" unless 手上的事 is pinned (COVEY_PINNED_OVERVIEW=1; read on every
    call): an owner without the Pin was sent to look at one."""
    pinned = str(cenv.get(PINNED_OVERVIEW_ENV, "", environ)).strip() == "1"
    return AREA_RULE if pinned else AREA_RULE.replace(PIN_WORDS, "")


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


PROJECT_RULE = (
    "- 聊天里有专项时（快照的“专项”一栏列着在做的和收起的，带 id、名字和一句目标），每件新的事先看它是不是其中某个专项的同一件事："
    "是的，用 project_send 交给那个专项的负责人（pid 用快照里的 id），收起的专项会随之重新展开，不要另开话题；"
    "拿不准是不是同一件事，就开普通话题（topic_start），不要硬归；主人说“这是 X 的事”“放到 X 里”，就归进 X，不用再判断。"
    "做完的专项不在快照里，不往里交。"
    "主人要停专项里的某个话题（T 号属于专项的话题）时，也用 project_send 交给负责人，写明停哪个话题；负责人会停掉它的任务、收掉它等主人回答的问题，你自己不动它。")


def project_rules() -> tuple[str, ...]:
    """The butler's rule for putting a new matter into a project that already exists (COVEY_PROJECTS=1); none without it, so
    the rules are then what they were."""
    return (PROJECT_RULE,) if projects.enabled() else ()


# The owner asking how to use Covey itself: the butler reads the running release's own one-page guide first (a release
# is the core's git archive, docs/ and all) instead of guessing or reading the code.
QUICKSTART = Path(__file__).resolve().parents[2] / "docs" / "quickstart.md"
USAGE_RULE = f"- 主人问 Covey 本身怎么用（看板在哪、怎么开记忆这类），先读 {QUICKSTART} 再答，不要凭印象说。"


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
