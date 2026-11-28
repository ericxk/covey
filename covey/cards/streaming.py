"""The streaming task card: the whole card built from a job (build_stream_card), its title row, buttons and body.
Its status line is cards/task_status.py, its tiles and 输入/输出 box cards/task_figures.py, what it costs CardKit
cards/card_cost.py.

Creating, updating and closing the card on CardKit is app/task_card.py (StreamingCard, which these take as ``runtime``:
CardRuntime is what they read of it).
"""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple, Protocol

from covey.core.defaults import work_mode_tag
from covey.core.jobs import job_stop_action_value
from covey.core.text import safe_text
from covey.core.topics import shown_title
from covey.cards.elements import button_row, find_element, fold, grey, markdown_panel, md, topic_button
from covey.cards.review import question_preview, queued_summary, running_summary
from covey.cards.topic_actions import switch_button
from covey.cards.ui import BODY_PADDING, BODY_SPACING, ROW_ID, banded, speaker_row, topic_badge
from covey.core.text import literal
from covey.cards.card_cost import card_cost, truncate_card_content
from covey.cards.task_figures import FIGURES_ELEMENT_ID, IO_BOX_ID, STATUS_BLOCK_ID, io_box
from covey.cards.task_status import (activity_element, more_element, slim_activity_element,
                                              slim_start)
from covey.cards.trace_view import clock, ttft_line
from covey.core.progress import command_words, eta_words, slow_words, stuck_words
from covey.core.trace import has_subtasks, turn_usage


class CardLook(NamedTuple):
    """How a job's task card is drawn, worked out by app/task_card.py card_look: ``steer``, its 补充 button (the
    Harness steers); ``switch``, its 换配置 button; ``slim``, the butler's slim card."""

    steer: bool = False
    switch: bool = False
    slim: bool = False


class CardRuntime(Protocol):
    """What the card is built with: app/task_card.py's StreamingCard (cards/ does not import app/)."""

    idempotency_prefix: str
    default_title: str
    element_id: str  # the body's
    card_limit: int  # the most characters of a body before it is cut
    compact_text: Callable[[str, int], str]

    def look(self, job: Any) -> CardLook:
        """``job``'s CardLook."""


STOP_ACTION_ELEMENT_ID = "job_stop_action"
SIDE_CHAT_ELEMENT_ID = "side_chat_action"  # 💬 侧聊 on a card sent before the action row (an older release).
ACTION_ROW_ELEMENT_ID = "action_row"
BOXED_PANELS = {"current_question", "task_details"}
# 任务详情's text, by its own ID: a running turn's figures replace it in place (app/cards.py queue_trace), so
# a panel the user opened stays open. A card sent before it had one gets the whole panel when the turn ends.
TASK_DETAILS_TEXT_ID = "task_details_text"
STEER_LABEL = "补充"  # docs/ui.md 第 6 节; "📝 补充当前任务" in older releases.
STEER_ELEMENT_ID = "steer_action"
# The slim card's fold (the butler's, core/butler.py Traits.slim_card): the
# tiles, 本轮请求, 任务详情 and the running buttons, folded under 详情 below the body and its status line; what a
# card puts "after the body" or "at the bottom" goes in it too (app/task_card.py body_anchor, bottom_anchor).
SLIM_DETAILS_ID = "slim_details"
SLIM_DETAILS_TITLE = "详情"
# An option's recommendation mark, which an answer's title carried as the answer ("远端旧分支 →
# 删两个已合并的（推荐）; 本…").
RECOMMENDED = re.compile(r"\s*(?:（推荐）|\(推荐\)|\(Recommended\))")


def card_title_for_question(question: str, runtime: CardRuntime) -> str:
    """The title after T12 · #196 (the title bar and the chat-list and Pin preview): the request's first 24
    characters, two lines in the topic side panel (36 took three there), written as every card
    writes a title (core/topics.py shown_title: 验收：睡 75 秒, as the answer card and the hand-off say it)."""
    return shown_title(runtime.compact_text(RECOMMENDED.sub("", question), 24)) or runtime.default_title


HEAD_ID = "task_head"


def task_head(job: Any, heading: str, status: str) -> dict[str, Any]:
    """The task card's first row, as the answer card's (cards/ui.py speaker_row; the owner, 2026-11-22: 整体按答案卡片弄):
    the topic's T number as its badge, the turn's title bold, and the status line
    (ACTIVITY_ELEMENT_ID, cards/task_status.py status_markdown: the state a coloured tag, 14:41 结束 grey) at its
    right, written by its own ID as before. It took the white title bar's place: a sent card's title bar never
    changes, this row's state does. Without a T number the title alone leads."""
    label = str(getattr(job, "topic_label", "") or "")
    badge = topic_badge(label) if label else None
    # No grey task number after the title (the owner, 2026-11-23: #6047 said nothing to him); the cards that named a
    # task by it (补充, 回执, 暂存的消息) say 这一轮 instead.
    title = md(f"**{literal(heading)}**", size="normal_v2")
    columns = ([{"tag": "column", "width": "auto", "vertical_align": "center", "elements": [badge]}] if badge else []) + [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [title]},
        {"tag": "column", "width": "auto", "vertical_align": "center", "elements": [activity_element(status)]}]
    return {"tag": "column_set", "element_id": HEAD_ID, "horizontal_spacing": "8px", "columns": columns}


def speaker_head(job: Any) -> dict[str, Any] | None:
    """A slim card's first row (docs/ui.md 5.3 说话人; the owner, 2026-11-22: 所有 slim_card 补齐 speaker_row 卡头): who
    speaks, the picture and the name bold (cards/ui.py speaker_row: **管家**, **<专项名> · 负责人**), as
    ``job.card_speaker`` says it (app/cards.py speaker_facts). No link and no grey words, as every card in a thread has
    it: the card is in the thread of the owner's message, his words right above it. Static: the state is the status
    line's, under the body. None without a speaker (a card sent before had no row, and one with no speaker still has
    none)."""
    found = getattr(job, "card_speaker", None)
    if not isinstance(found, dict) or not str(found.get("who") or "").strip():
        return None
    return speaker_row({"who": str(found["who"])}, avatar=str(found.get("avatar") or ""), url="")


def headed(elements: list[dict[str, Any]]) -> bool:
    """Whether ``elements``, a card's, open with a speaker's row (cards/ui.py ROW_ID)."""
    return bool(elements) and elements[0].get("element_id") == ROW_ID


def build_collapsible_markdown_panel(element_id: str, title: str, content: str, content_id: str = "") -> dict[str, Any]:
    """A task card's folded panel of one markdown text (cards/elements.py markdown_panel); ``content_id``: the
    text's own element ID, to replace it alone. 本轮请求 and 任务详情 are boxed with a grey title (the card has no
    filled blocks: A 仪表盘's grey-50 blocks, with blue 进展说明 inside, read heavy on a white card); the text of
    任务详情 and 执行过程 is small."""
    small = element_id in {"process_panel", "task_details"}
    if element_id in BOXED_PANELS:
        return markdown_panel(element_id, grey(title), content, content_id=content_id, boxed=True,
                              size="notation" if small else "normal_v2")
    return markdown_panel(element_id, title, content, content_id=content_id,
                          **({"size": "notation", "quiet": True, "spacing": "4px"} if small else {}))


def _tokens(label, counts):
    """``label：输入 N · 输出 M token``; a part with neither figure is left out, never written as unknown."""
    parts = [f"{name} {counts[key]:,}" for key, name in (("input_tokens", "输入"), ("output_tokens", "输出"))
             if counts.get(key) is not None]
    return [f"{label}：{' · '.join(parts)} token"] if parts else []


def usage_lines(snapshot):
    """任务详情's token lines for a turn with subtasks (core/trace.py turn_usage): what the turn's tokens cover, then
    the main task and the subtasks (each when few, else per level). The turn's total, its input split and a turn's
    own counts without subtasks are the 输入/输出 box's: none."""
    usage = turn_usage(snapshot)
    if not has_subtasks(usage):
        return []
    # While the turn runs, every subtask started so far (turn_usage's ``subtasks``), with usage or not.
    scope = f"本轮主任务 + {usage['subtasks']} 个子任务" if usage["subtasks"] else "只含主任务"
    lines = ["token 用量：" + scope + (f"（部分统计：{'，'.join(usage['missing'])}）" if usage["missing"] else "")]
    lines += _tokens("主任务", usage["main"])
    if usage["agents"] is not None:
        nested = any(agent["level"] > 1 for agent in usage["agents"])
        for agent in usage["agents"]:
            lines += _tokens(f"子任务 · {safe_text(agent['name'], 40)}" + (f"（第 {agent['level']} 层）" if nested else ""), agent)
    else:
        for level in usage["levels"]:
            lines += _tokens(f"第 {level['level']} 层子任务（{level['count']} 个）", level)
    lines.append("输入是各次请求的输入之和，不是单次上下文长度，也不是计费折算量。")
    return lines


def task_details_text(job, usage=None) -> str:
    """What 任务详情 says, "" for nothing: a task's work mode, a timeout when one is set, a turn with subtasks their tokens (``usage_lines``) and the TTFT the backend
    gave (``ttft_line``). ``usage`` is the process snapshot: a running turn's figures so far, an ended one's.

    No 任务编号 (the title has #N and T…), no 模型 line, no
    总耗时 (用时 is in the 输入/输出 box). None of
    what that box says either: no token counts of the turn or their split by the cache, no 模型生成 · 平均 token/s."""
    mode = work_mode_tag(getattr(job, "work_mode", ""))
    lines = ([f"工作模式：{mode}"] if mode else []) + (
        [f"超时：{job.timeout}s"] if getattr(job, "timeout", 0) > 0 else [])
    if usage is not None and "subtask_usage" in usage:
        lines += usage_lines(usage)
    if usage is not None:
        lines += [line for line in (ttft_line(usage),) if line]
    if usage is not None and usage.get("ended_at") is None:
        lines += progress_lines(getattr(job, "turn_progress", None))
    return "\n".join(lines)


def progress_lines(found) -> list[str]:
    """A running turn's progress (core/progress.py, kept on the job by core/longtask.py TaskMonitor) as 任务详情 says it:
    阶段, 步骤 3/5, its last activity at a time of day (14:05 在跑测试: the panel is written with the process panel, not
    every minute) and the estimate with when it was made, or when its slow step began (第 3 步 16:33 开始，此前平均每步约 6
    分钟); [] without any."""
    if not isinstance(found, dict):
        return []
    lines = [f"阶段：{safe_text(found['phase'], 120)}"] if found.get("phase") else []
    if found.get("plan_total"):
        lines.append(f"步骤：做完 {found.get('plan_done') or 0}/{found['plan_total']}")
    at = found.get("last_activity_at")
    if at is not None:
        lines.append(f"最近动静：{clock(at)} {safe_text(found.get('last_activity') or '有动静', 60)}")
    if found.get("maybe_stuck"):
        lines.append(stuck_words())
    elif found.get("command"):
        lines.append(command_words(found))
    elif found.get("slow_step"):
        lines.append(slow_words(found, clock=clock))
    elif found.get("eta_seconds") is not None:
        made = found.get("at")
        lines.append(eta_words(found["eta_seconds"]) + (f"（{clock(made)} 估）" if made is not None else ""))
    return lines


def task_details_panel(job, usage=None):
    """The 任务详情 panel: None, always (the owner, 2026-11-22: 任务详情就没必要了, the card keeps 本轮请求 and 执行过程).
    What it said (``task_details_text``) is on no card: app/task_details.py writes nothing for a None panel, so a new
    card never gets one and a card sent before keeps the words it shows."""
    return None


def answer_element(element_id: str, content: str) -> dict[str, Any]:
    """The task card's body: the queued status, then the streamed and final answer."""
    return {"tag": "markdown", "element_id": element_id, "text_size": "normal_v2", "content": content}


def steer_label(job: Any, slim: bool = False) -> str:
    """The label of ``job``'s 补充 button: on a topic's queued card with a task before it, 现在补给 #N, the task its click
    sends the message into (``steer_target_job_id``; docs/ui.md 6); 补充 once the job runs (a click supplements it then)
    and on the butler's slim card (``slim``)."""
    target = getattr(job, "steer_target_job_id", None)
    if slim or not target or getattr(job, "execution_started", False):
        return STEER_LABEL
    return f"现在补给 #{target}"


def steer_button(job: Any, label: str = STEER_LABEL) -> dict[str, Any]:
    """``job``'s 补充 button (STEER_ELEMENT_ID): the same value however it is labelled (``steer_label``)."""
    return topic_button(label, element_id=STEER_ELEMENT_ID, tone="primary", value={
        "kind": "bridge_steer", "topic_id": getattr(job, "topic_id", ""), "job_id": job.job_id})


def build_stop_action_row(job: Any, include_steer: bool = False, *, essential: bool = False,
                          switch: bool = False, label: str = STEER_LABEL) -> list[dict[str, Any]]:
    """The job's buttons (bordered, button_look) in a row that wraps (flow, each column as wide as its label,
    so none is cut in a topic's side panel), at the bottom of the card. 停止 ends the task, so it comes
    last, in red. It has no ``confirm``: the client sends nothing on 确定 once the card was redrawn while its
    dialog was open (a running card's status line redraws every few seconds, and a
    real 停止 → 确定 never reached the Bridge; a probe card redrawn under an open dialog did the same). An
    `essential` row has only 停止: the card to fall back on when CardKit rejects the full one. ``switch``: with
    换配置 (CardLook.switch). ``label``: the 补充 button's (``steer_label``). The element IDs and values are those of
    every card before (the status line was under the row in older releases; it is in the status block at the top
    now)."""
    buttons = []
    if essential:
        include_steer = False
    if include_steer:
        buttons.append(steer_button(job, label))
    if getattr(job, "topic_id", "") and not essential and switch:
        button = switch_button(job.topic_id, job.job_id, job.harness_id, job.binding_revision)
        button["element_id"] = "running_switch"  # CardKit element IDs must fit 20 characters.
        buttons.append(button)
    buttons.append(topic_button("停止", element_id=STOP_ACTION_ELEMENT_ID, tone="danger",
                                value=job_stop_action_value(job)))
    return [button_row(buttons, ACTION_ROW_ELEMENT_ID)]


def build_stream_card(
    content: str,
    title: str,
    runtime: CardRuntime,
    *,
    job: Any | None = None,
    include_stop_action: bool = False,
    essential: bool = False,
    status: str = "",
) -> dict[str, Any]:
    """The task card (the "E" sample): a white title bar of the topic, task and title alone; the
    status, the rest of the status line under it (STATUS_MORE_ID), the grey 输入/输出 box (``io_box``: 输入 in three
    lines, 输出 the run's figures, FIGURES_ELEMENT_ID), the body (the answer as it streams, the lead under 结论 at the
    end), then 执行过程 when the first step comes (inserted after the body), 本轮请求 and 任务详情 as grey blocks, and
    the buttons at the bottom. Every element keeps the ID it had on cards sent before, which keep their order
    (app/task_card.py StreamingReply.dashboard, io_box). ``status``: the status line to start with, 等待处理 when
    empty (a queued card's says where its message goes, app/submission.py queued_line).

    The butler's card is slim (core/butler.py Traits.slim_card): no title
    bar (the quoted message says it, and a sent one never changes), its speaker's row (``speaker_head``: the picture and
    **管家**), the body (a queued card's stage goes in the status line instead, ``slim_start``), the status line as
    one line (``slim_status_markdown``), then 详情 folded (``slim_details``): the 输入/输出 box (its 输出 without
    已运行, which the status line says), 本轮请求, 任务详情 and the running buttons, every one by its old ID."""
    answer = answer_element(runtime.element_id, "")
    elements: list[dict[str, Any]] = [answer]
    look = runtime.look(job) if job is not None else CardLook()
    slim = look.slim
    heading = card_title_for_question(title, runtime)
    if job is not None:
        request = build_collapsible_markdown_panel("current_question", "本轮请求",
                                                   question_preview(job.user_text, max_lines=40, max_chars=4000))
        buttons = (build_stop_action_row(job, look.steer, essential=essential, switch=look.switch,
                                         label=steer_label(job, slim)) if include_stop_action else [])
        if slim:
            line, content = slim_start(content)  # The slim card's line comes from its body, not ``status``.
            elements = [e for e in (speaker_head(job), answer, slim_activity_element(line),
                                    slim_details([e for e in (io_box(job), request, *buttons) if e])) if e]
        else:
            # The answer card's look: its row (T number, title, the state as a tag), the 输入/输出 lines, the body, then
            # 本轮请求 and 执行过程 folded; no 任务详情 (task_details_panel).
            elements = [e for e in (task_head(job, heading, status or "等待处理"), more_element(), io_box(job), answer,
                                    request, *buttons) if e]
    card = {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 10, "android": 10, "ios": 10, "pc": 10},
                "print_step": {"default": 10000, "android": 10000, "ios": 10000, "pc": 10000},
                "print_strategy": "fast",
            },
        },
        "body": {
            # A slim card with its speaker's row is spaced as every card that opens with one (docs/ui.md 5.3).
            "padding": "6px 14px 12px 14px" if (slim and not headed(elements)) or job is None else BODY_PADDING,
            "vertical_spacing": "8px" if (slim and not headed(elements)) or job is None else BODY_SPACING,
            "elements": elements
        },
    }
    # No title bar (2026-11-22): the row (``task_head``) says the T number, task and title, and its state, which a
    # title bar could not (a sent card's header does not change in place). A slim card never had one.
    card = banded(card)  # The badge's colour in the card's config.
    label = getattr(job, "topic_label", "") or (f"#{job.job_id}" if getattr(job, "job_id", None) else "")
    if label:
        # The topic list's preview of the topic while this card is its latest message (cards/review.py
        # running_summary); a replacement card for a running task says so from the start.
        card["config"]["summary"] = {"content": running_summary(label, heading) if getattr(job, "execution_started", False)
                                     else queued_summary(label, heading)}
    # The body gets what the card's size leaves it: the rest of the card is counted first.
    answer["content"] = truncate_card_content(content, runtime, sum(other_costs(card, runtime.element_id).values()))
    return card


def slim_details(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """The slim card's 详情 (SLIM_DETAILS_ID): a client-side fold, folded, of ``elements``; opening and closing it
    calls nothing back and draws nothing again."""
    return fold(SLIM_DETAILS_ID, md(grey(SLIM_DETAILS_TITLE), size="notation"), elements,
                title_padding="2px 0px 2px 0px", icon_position="follow_text", spacing="8px")


def card_details_text(card: dict[str, Any]) -> str | None:
    """``card``'s 任务详情 text when it has its own ID (TASK_DETAILS_TEXT_ID: StreamingReply.live_details),
    else None."""
    panel = find_element(card, "task_details") or {}
    text = next((e for e in panel.get("elements", ()) if e.get("element_id") == TASK_DETAILS_TEXT_ID), None)
    return None if text is None else text["content"]


def other_costs(card: dict[str, Any], body_id: str = "answer") -> dict[str, int]:
    """card_cost of a task card's parts besides its body, by element_id (the header and the card's
    settings under ``card``): what StreamingReply.others starts from."""
    elements = card["body"]["elements"]
    details = next((e for e in elements if e.get("element_id") == SLIM_DETAILS_ID), None)
    if details is not None:
        # A slim card's 详情 holds what other cards have at the top level, written by their own IDs: each counted
        # on its own, the fold without them.
        elements = [e for e in elements if e is not details] + list(details["elements"]) + [{**details, "elements": []}]
    costs = {e.get("element_id") or f"element_{i}": card_cost(e) for i, e in enumerate(elements)
             if e.get("element_id") != body_id}
    figures = find_element(card, FIGURES_ELEMENT_ID)
    holder = next((key for key in (STATUS_BLOCK_ID, IO_BOX_ID) if key in costs), None)
    if figures is not None and FIGURES_ELEMENT_ID not in costs and holder:
        # The tiles inside the status block, or 输出 in the 输入/输出 box, are written on their own
        # (app/task_details.py card_figures, note_element): counted once.
        costs[FIGURES_ELEMENT_ID] = card_cost(figures)
        costs[holder] -= costs[FIGURES_ELEMENT_ID]
    costs["card"] = card_cost({key: value for key, value in card.items() if key != "body"})
    return costs
