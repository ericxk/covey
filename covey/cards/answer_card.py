"""What follows a finished turn's answer on its answer card in the topic and on its relay at the private chat's top
level: the answer, then its 产物
folded (cards/artifact_panels.py drive_artifact_panel, by ARTIFACTS_ID), then its 改动 folded the same way (``change_element``,
CHANGE_ID: only a turn that changed code, core/checkpoint.py shows_change), then 输入 and 输出 as two small lines under it,
a line of 改动 after them when the turn changed code, each label grey. Each is one line that wraps on its own: its
parts apart by a grey "｜" (SEPARATOR),
each part never broken inside at a phone's width or in a topic's side panel (``unbroken``; the "E" sample
split "脱 / 敏关", "缓存 / 命中"), so a line wraps only after a separator:

- 输入: Harness ｜ 供应商 ｜ 模型 ｜ 思考 <思考预算> ｜ 脱敏开/关 ｜ 工作区 (``input_parts``, ``input_lines``);
- 输出: 用时 ｜ token/s ｜ token (the cache included, as the task card's tile) ｜ 缓存命中 ｜ 重启 (the Bridge restarts the
  turn's own process lived through, ``restart_part``) (``output_parts``, ``output_lines``);

The task card's 输入/输出 box (cards/task_figures.py io_box) is the grey box, in the topic's thread: while its turn runs, 输出 is 开始 ｜ 已运行 ｜
token ｜ token/s ｜ 上下文 (｜ 重启) (``running_figures``, ``RUNNING_LINES``).

The parts under an answer that are there to look up (产物, 验收说明, 改动) go into one closed fold
(``details``); what the owner must see (a commit not found, the completion criteria not met: ALERT_IDS) stays out of it,
and so do the 记下了 lines (MEMORY_ID: first under the answer, whatever else the card holds), the 输入/输出 lines and 独立复核 under them. No link ends the 输出 line: the card's head (cards/ui.py
speaker_row) opens the owner's message or the topic. An answer too long for one card goes on several (cards/pages.py),
these parts under the last, or after them on ``tail_card`` when it cannot hold them too; a file is followed by
``tail_card`` as well."""
from __future__ import annotations

import re
import time

from covey.cards.elements import card, chevron, collapsible, fold, grey, item, json_size, md, text_button
from covey.core.checkpoint import change_summary, delivery_commits, short_commit
from covey.cards.ui import ASK_ID, ASK_META
from covey.cards.facts import context_text, start_text, token_total, usage_figures
from covey.core.metrics import took_text
from covey.core.trace import restarts
from covey.core.text import literal, share

ARTIFACTS_ID = "answer_artifacts"
FACTS_ID = "answer_facts"
CHANGE_ID = "answer_change"
WORKSPACE_CHARS = 24
BUDGET_WORD, OLD_BUDGET_WORD = "思考", "推理"


def input_parts(harness="", provider="", model="", effort="", redaction=None, workspace=""):
    """The 输入 line's six parts, in the owner's order, none left out: Harness · 供应商 · 模型 ·
    思考 <budget> · 脱敏开/关 · 工作区; one not known reads "—" (the model 默认模型, the budget 思考 默认). The budget is
    the 思考预算 (推理深度统一叫思考预算; it read 推理 <budget> before, BUDGET_WORD). A Harness
    that cannot redact (``redaction`` None) reads 脱敏关: it does not."""
    workspace = str(workspace or "")
    if len(workspace) > WORKSPACE_CHARS:
        workspace = workspace[:WORKSPACE_CHARS - 1] + "…"
    flag = "脱敏开" if redaction else "脱敏关"
    effort = BUDGET_WORD + " " + (str(effort or "").strip() or "默认")
    return [str(part or "").strip() or "—" for part in (harness, provider, model or "默认模型", effort, flag, workspace)]


def output_parts(snapshot=None, took=None):
    """The 输出 line's parts from the turn's process snapshot (core/trace.py) and ``took`` seconds: 用时, the main
    task's output speed, its tokens with the cache (input, which counts the cache, plus output; the subtasks' too
    when reported, as the token tile) and the share of the input read from cache. Markdown, the figures bold."""
    snapshot = snapshot or {}
    parts = []
    if isinstance(took, (int, float)) and not isinstance(took, bool) and took >= 0:
        parts.append(figure("用时", took_text(took)))
    figures, tokens = _usage(snapshot)
    speed = figures["speed"]
    if speed is not None:
        parts.append(f"**{speed:.0f}** {grey('token/s')}")
    if tokens:
        parts.append(figure("token", tokens))
    if snapshot:
        given, read = figures["shown"].get("input_tokens"), figures["cache_read"]
        if type(read) is int and read > 0 and type(given) is int and given >= read:
            parts.append(figure("缓存命中", share(read, given)))
    restarted = restart_part(restarts(snapshot.get("steps")))
    if restarted:
        parts.append(restarted)
    return parts


RESTART_WORD = "重启"


def restart_part(times) -> str:
    """输出's part for the Bridge restarts the turn went on through in its own process (core/trace.py restarts; T647 E2: it
    was a line folded in 执行过程 alone): "重启 22:31 没打断", "重启 2 次没打断"; '' without one."""
    if not times:
        return ""
    if len(times) == 1:
        return figure(RESTART_WORD, time.strftime("%H:%M", time.localtime(times[0])) + " 没打断")
    return figure(RESTART_WORD, f"{len(times)} 次没打断")


def figure(label, value):
    """One part of 输出: ``label`` grey, ``value`` bold."""
    return f"{grey(label)} **{value}**"


def _usage(snapshot):
    """(cards/facts.py usage_figures of ``snapshot``, its tokens with the cache as a tile counts them or "")."""
    figures = usage_figures(snapshot or {})
    return figures, token_total(figures["shown"] if snapshot else {}, ints_only=True) or ""


def running_figures(snapshot=None, began=None) -> list:
    """A running turn's 输出 parts but 已运行 (the status line's ticks add it, ``with_ran``), as [label, Markdown]
    pairs a task card keeps (app/task_card.py StreamingReply.figures_parts): 开始 (``began``, the run's start,
    core/metrics.py run_start, else the process record's), its tokens with the cache, the main task's output speed,
    the context the next turn resumes with (a share of the model's window, its size alone without one) and the Bridge
    restarts it has gone on through (``restart_part``)."""
    snapshot = snapshot or {}
    if not isinstance(began, (int, float)) or isinstance(began, bool):
        began = snapshot.get("started_at")
    pairs = []
    if isinstance(began, (int, float)) and not isinstance(began, bool):
        pairs.append(["开始", figure("开始", start_text(began))])
    figures, tokens = _usage(snapshot)
    if tokens:
        pairs.append(["token", figure("token", tokens)])
    if figures["speed"] is not None:
        pairs.append(["token/s", f"**{figures['speed']:.0f}** {grey('token/s')}"])
    context = context_text(figures["context"], figures["window"])
    if context:
        pairs.append(["上下文", figure("上下文", context)])
    restarted = restart_part(restarts(snapshot.get("steps")))
    if restarted:
        pairs.append([RESTART_WORD, restarted])  # Seen on the running card the moment a restart went by (T647 E2).
    return pairs


def with_ran(pairs, ran) -> list:
    """The running 输出 parts ``pairs`` with 已运行 ``ran`` (its text, "" leaves it out) after 开始."""
    pairs = [list(pair) for pair in pairs or () if isinstance(pair, (list, tuple)) and len(pair) == 2]
    if ran:
        at = next((index + 1 for index, (label, _) in enumerate(pairs) if label == "开始"), 0)
        pairs.insert(at, ["已运行", figure("已运行", ran)])
    return pairs


# The order of 输出's parts, by their labels (``part_label``): a running turn's, a slim card's (its 已运行 is on its
# status line, outside the fold) and an ended one's. A label in none goes last. One line each.
RUNNING_LINES = (("开始", "已运行"), ("token", "token/s"), ("上下文",))
SLIM_RUNNING_LINES = (("开始", "token", "token/s"), ("上下文",))
ENDED_LINES = (("用时", "token/s"), ("token", "缓存命中"))


def part_label(part) -> str:
    """The label of one part of 输出 as ``output_parts`` and ``running_figures`` write it (its words without
    markup): token/s for the speed ("**69** token/s"), else its first word ("用时 1分30秒")."""
    words = MARKUP.sub("", str(part or "")).split()
    if not words:
        return ""
    return "token/s" if words[-1] == "token/s" else words[0]


def output_lines(parts, lines=ENDED_LINES) -> list:
    """``parts`` of 输出 in the order ``lines`` names, as one line (``joined``); [] without parts."""
    groups = [[] for _ in lines]
    for part in parts or ():
        label = part_label(part)
        at = next((index for index, labels in enumerate(lines) if label in labels), len(lines) - 1)
        groups[at].append(part)
    return joined([part for group in groups for part in group])


def input_lines(parts) -> list:
    """The parts of 输入 (``input_parts``: Harness ｜ 供应商 ｜ 模型 ｜ 思考 ｜ 脱敏 ｜ 工作区) as one line
    (``joined``), the workspace's hyphens no-break ones (U+2011: "demo-" / "app" in a topic's side
    panel). A part saved by an older release (a job's ``input_parts``, a relay's record) says 推理 <budget>: shown as
    思考 <budget>."""
    parts = [BUDGET_WORD + part[len(OLD_BUDGET_WORD):] if part.startswith(OLD_BUDGET_WORD + " ") else part
             for part in (str(part) for part in parts or ())]
    if len(parts) >= 6:
        parts[5:] = [part.replace("-", "\u2011") for part in parts[5:]]
    return joined(parts)


def joined(parts) -> list:
    """``parts`` as one line in a list ([] without any): each kept whole (``unbroken``), SEPARATOR between them."""
    parts = [part for part in parts if part]
    return [SEPARATOR.join(unbroken(part) for part in parts)] if parts else []


def labelled(label, text, element_id=""):
    """One row of the box: ``label`` small and grey in a column as wide as it, ``text`` beside it (its own
    ``element_id`` when given: the task card writes its 输出 alone, FIGURES_ELEMENT_ID)."""
    return {"tag": "column_set", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "auto", "elements": [md(grey(label), size="notation")]},
        {"tag": "column", "width": "weighted", "weight": 1,
         "elements": [md(text, size="notation", **({"element_id": element_id} if element_id else {}))]}]}


NBSP, JOINER = "\u00a0", "\u2060"
# Between two parts of a line: a grey full-width bar, a no-break space before it, so a wrapped line ends with the bar
# and never begins with it.
SEPARATOR = NBSP + grey("｜") + " "
MARKUP = re.compile(r"(<[^>]*>|\*\*)")


def unbroken(part):
    """One part of a line kept on one line: its spaces are no-break spaces and a word joiner sits between its
    characters where a line could break (``glued``), so a narrow card wraps only after the bar between parts
    (SEPARATOR). The topic side panel's chat column split "推理 / xhigh" (now 思考 xhigh) and "缓存命中 / 94%"; a
    no-break space alone let Chinese break inside a word ("推 / 理"). Tags and ``**`` stay as written."""
    return "".join(piece if MARKUP.fullmatch(piece) else glued(piece.replace(" ", NBSP))
                   for piece in MARKUP.split(part) if piece)


def glued(text):
    """``text`` with a word joiner between two characters unless both are ASCII letters or digits, which never
    part (a Chinese character, a hyphen or a slash may)."""
    out = text[:1]
    for before, char in zip(text, text[1:]):
        out += ("" if before.isascii() and before.isalnum() and char.isascii() and char.isalnum() else JOINER) + char
    return out


def facts_element(inputs=(), outputs=(), link="", change=None):
    """The 输入 and 输出 under an answer (FACTS_ID): one small element, a line each with its label grey, no box
    (``input_lines``, ``output_lines``), the line of 改动 after them when ``change`` (a dict, ``change_line``) says
    one; ``link``, a T number an older relay ended 输出 with. No "看你那句 ›" or "进话题 ›": the card's row opens
    them (cards/ui.py speaker_row). None with none of them."""
    rows = []
    if inputs:
        rows.append(grey("输入") + " " + "".join(input_lines(inputs)))
    parts = [*output_lines(outputs), *([f"{link} ›"] if link else [])]
    if parts:
        rows.append(grey("输出") + " " + SEPARATOR.join(parts))
    line = change_line(change)
    if line:
        rows.append(line)
    return md("\n".join(rows), size="notation", element_id=FACTS_ID) if rows else None


def change_line(change) -> str:
    """The 改动 of a turn that changed code (a dict, core/checkpoint.py) as one more line of the facts: "改动 a6bc148 ｜
    第 322 代 ｜ 已自检" (``change_summary``'s parts); '' for anything else."""
    if not isinstance(change, dict) or not change:
        return ""
    parts = [part for part in change_summary(change).split(" · ") if part]
    return grey(CHANGE_WORD) + " " + SEPARATOR.join(unbroken(literal_text(part)) for part in parts) if parts else ""


CHANGE_WORD = "改动"
# A 改动's commits the repository of the turn's workdir has no commit for (app/answer_cards.py absent_commits), when
# any: what the model wrote, checked by the Bridge (git, no model).
NOT_FOUND = "not_found"
CHECK_ID = "answer_change_check"
MISMATCH = "提交号对不上"
ABSENT = "（写过的仓库里没有）"


def not_found(change) -> list:
    """The commits of ``change`` the Bridge did not find (NOT_FOUND), [] for none or for a string of an older release."""
    found = change.get(NOT_FOUND) if isinstance(change, dict) else None
    return [str(commit) for commit in found if commit] if isinstance(found, list) else []


def change_rows(change) -> list:
    """The rows of an open 改动 fold: 提交 (every commit in full, one a line, one not found in the workdir's repository
    followed by ABSENT), 部署 and 自检 (as the checkpoint wrote them, not cut); [] for anything but a dict with
    something in it."""
    if not isinstance(change, dict) or not change:
        return []
    commits, absent = delivery_commits(change), set(not_found(change))
    shown = "\n".join(literal_text(commit) + (" " + grey(ABSENT) if commit in absent else "") for commit in commits)
    return [labelled("提交", shown if commits else grey("未提交")),
            labelled("部署", literal_text(change.get("deployed")) if change.get("deployed") else grey("未部署")),
            labelled("自检", literal_text(change.get("verified")) if change.get("verified") else grey("未自检"))]


def change_element(change):
    """The 改动 fold (CHANGE_ID) of a turn that changed code (app/answer_cards.py change): folded, one small grey line "改动 · 951327a · 第 331 代 · 已自检"
    (core/checkpoint.py change_summary), in the 产物 panel's look (a quiet ``collapsible``); open, ``change_rows``. A string
    (``change`` of an older release, "已提交 a6bc148 · 已部署 第 322 代 · 未验收"): that one grey line,
    no fold. None without one."""
    if isinstance(change, str):
        words = " ".join(change.split())
        return md(grey(literal_text(f"{CHANGE_WORD} · {words}")), size="notation", element_id=CHANGE_ID) if words else None
    rows = change_rows(change)
    if not rows:
        return None
    return collapsible(CHANGE_ID, f"{CHANGE_WORD} · {change_summary(change)}", rows, quiet=True, spacing="4px")


def check_element(change):
    """The line under the 改动 fold (CHECK_ID) when the repository of the turn's workdir has no commit for an id the
    model wrote (``not_found``): "提交号对不上" in bold body text (orange says 等你 and 可能卡住 alone, docs/ui.md 2.2),
    then which, grey: "提交号对不上：写过的仓库里没有 951327a". None when every commit was found or it could not be told."""
    absent = not_found(change)
    if not absent:
        return None
    ids = "、".join(short_commit(commit) for commit in absent)
    return md(f"**{MISMATCH}**" + grey(literal_text(f"：写过的仓库里没有 {ids}")), size="notation", element_id=CHECK_ID)


def box(element_id, inputs, outputs, outputs_id=""):
    """The task card's 输入 (the lines ``inputs``) and 输出 (``outputs``, its text's own ``outputs_id`` when given, then
    always there: "—" without lines), a row each with its label grey, as the answer card's lines (``facts_element``;
    the owner, 2026-11-22: 输入输出的 UI 风格参考答案卡片): no grey box any more; None with none."""
    rows = []
    if inputs:
        rows.append(labelled("输入", "\n".join(inputs)))
    if outputs or outputs_id:
        rows.append(labelled("输出", "\n".join(outputs) or grey("—"), outputs_id))
    if not rows:
        return None
    return {"tag": "column_set", "element_id": element_id, "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_spacing": "2px", "elements": rows}]}


MEMORY_ID = "answer_memory"
MEMORY_WRONG_KIND = "bridge_memory_wrong"
# 采纳 on a line that waits in 待审核 (app/memory_collect.py accept_click): the kind the 设置 card's 采纳 has (cards/memory_panel.py
# ACCEPT, app/memory_panel.py takes both), so no kind is new; this one carries the job (``job_id``), the 设置 card's never does.
MEMORY_ACCEPT_KIND = "bridge_memory_pin"
MEMORY_PROMOTE_KIND = "bridge_memory_promote"  # 升为全局 of the older card: no longer drawn, the click says it is offline (app/task_controls.py).
MEMORY_LINES = {"remembered": "记下了", "voided": "作废了", "undone": "已撤销", "superseded": "收起了", "pending": "待审核"}
LEAD_LINE = "负责人记下了"  # A line of the lead's project_remember (a note ``by`` lead, app/project_tools.py).
UNDOABLE = ("remembered", "superseded", "pending")  # The lines with a 不对 (app/memory_collect.py take_back).
EDIT_LABEL = "改"
ACCEPT_LABEL = "采纳"
NOTE_WORDS = "（相关时才带）"  # After a 资料 the owner asked to be noted (a note's ``entry_kind``): not carried every turn.
WIDER_WORDS = "先{scope}，要管全局吗"  # A rule held where it was said while 待审核 asks about 全局 (a note's ``review``).
# The owner's rule written into a scope already full (a note's ``over``, app/memory_collect.py): the turns carry only its title.
OVER_WORDS = {"管这个专项": "这个专项放不下了，只带标题", "管这个话题": "这个话题放不下了，只带标题", "管全局": "全局放不下了，只带标题"}


def memory_element(notes, topic_id="", job_id="", url=None):
    """The grey lines at the bottom of a turn's answer card (COVEY_MEMORY=1, app/memory_collect.py): one per note of the job
    record's ``memory_notes`` and ``lead_notes`` ({kind, text, scope, entry_id, title?, code?, redacted?, entry_kind?, review?,
    by?}), "记下了：<title, else text> · 管这个专项" (a 资料, ``entry_kind`` note, says ``NOTE_WORDS`` after its words; a line of
    the lead's says ``LEAD_LINE``) and a 不对 button that takes it back (``MEMORY_WRONG_KIND``), then with the web page on a 改
    button that opens that entry on the web page's 记忆 (``url`` + ``?code=<the note's short code>``; ``url`` default: the page's
    /memory; no button without the page); "作废了：<text>", "已撤销：<text>" once taken back. A line that waits in 待审核 has
    采纳 first (``MEMORY_ACCEPT_KIND``): a rule held where it was said asks "先管这个专项，要管全局吗" (``review``), a learned
    rule that grants is a 待审核 line. There is no 升为全局 here: moving an entry to a larger scope is the page's. None without a
    note."""
    from urllib.parse import quote

    from covey.cards.memory_panel import center_url, url_button

    url = center_url() if url is None else url
    rows = []
    for note in notes or ():
        kind = note.get("kind")
        if kind not in MEMORY_LINES:
            continue
        text = literal_text(note.get("title") or note.get("text")) + (NOTE_WORDS if note.get("entry_kind") == "note" else "")
        label = LEAD_LINE if kind == "remembered" and note.get("by") == "lead" else MEMORY_LINES[kind]
        scope = str(note.get("scope") or "")
        asks = kind == "remembered" and bool(note.get("review"))
        if asks and scope:
            scope = WIDER_WORDS.format(scope=scope)
        words = f"{label}：{text}" + (f" · {scope}" if kind in UNDOABLE and scope else "")
        if kind == "remembered" and note.get("over"):
            words += " · " + OVER_WORDS.get(str(note.get("scope") or ""), "放不下了，只带标题")
        line = md(grey(words), size="notation")
        if kind in UNDOABLE:
            value = {"topic_id": topic_id, "job_id": job_id, "entry_id": str(note.get("entry_id") or "")}
            buttons = []
            if asks or (kind == "pending" and value["entry_id"].startswith("review:")):
                buttons.append(text_button(ACCEPT_LABEL, {"kind": MEMORY_ACCEPT_KIND, **value}))
            buttons.append(text_button("不对", {"kind": MEMORY_WRONG_KIND, **value}))
            if url:
                code = str(note.get("code") or "")
                at = f"{url}?code={quote(code)}" if code and "?" not in url and "#" not in url else url
                buttons.append(url_button(EDIT_LABEL, at))
            rows.append(item(line, *buttons))
        else:
            rows.append(line)
    if not rows:
        return None
    return {"tag": "column_set", "element_id": MEMORY_ID, "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_spacing": "2px", "elements": rows}]}


ACCEPTANCE_ID = "answer_acceptance"
LOCAL_ONLY = "只做了本地验证"  # 去哪看 without a link the owner can click (docs/usage.md).


def acceptance_element(acceptance):
    """改了什么 / 去哪看 / 怎么验 of a UI change or a turn the owner asked to accept (core/checkpoint.py
    checkpoint_acceptance), three rows of grey labels (ACCEPTANCE_ID): 去哪看 the link the model copied from its reply
    when there is one the owner can click (acceptance_link), else 只做了本地验证; a row the model left empty is left out
    but 去哪看. None without a what or a how."""
    from covey.core.checkpoint import acceptance_link

    if not isinstance(acceptance, dict) or not (acceptance.get("what") or acceptance.get("how")):
        return None
    url = acceptance_link(acceptance.get("where"))
    where = f"[{literal_text(url)}]({url})" if url else grey(LOCAL_ONLY)
    rows = [labelled(label, text) for label, text in (
        ("改了什么", literal_text(acceptance.get("what")) if acceptance.get("what") else ""), ("去哪看", where),
        ("怎么验", literal_text(acceptance.get("how")) if acceptance.get("how") else "")) if text]
    return {"tag": "column_set", "element_id": ACCEPTANCE_ID, "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_spacing": "2px", "elements": rows}]}


CRITERIA_ID = "answer_criteria"


def criteria_element(rows):
    """The topic's completion criteria its turn did not meet (core/criteria.py unmet: (state, item) each), on its
    answer card (CRITERIA_ID): grey 完成标准 in a column, then a line each, "没满足：<item>" with 没满足 in bold body text
    (orange says 等你 and 可能卡住 alone, docs/ui.md 2.2), "没查：<item>" grey. None without rows: every one met, or the
    topic has none."""
    from covey.core.criteria import UNMET

    lines = [(f"**{state}**" if state == UNMET else grey(state)) + grey("：") + literal_text(item)
             for state, item in rows or () if item]
    if not lines:
        return None
    return {**labelled("完成标准", "\n".join(lines)), "element_id": CRITERIA_ID}


REVIEW_ID = "answer_review"
REVIEW_KIND = "bridge_review_open"  # app/review.py KIND.
REVIEW_LABEL = "独立复核"


def review_element(topic_id, job_id):
    """独立复核 under the 输入/输出 box of a turn that changed code (app/review.py, COVEY_REVIEW=1): a bordered button in a
    row of its own (REVIEW_ID); a click opens a read-only topic that reviews this turn's changes on another model."""
    from covey.cards.elements import button_row, callback, topic_button

    return button_row([topic_button(REVIEW_LABEL, behaviors=callback(
        {"kind": REVIEW_KIND, "topic_id": str(topic_id or ""), "job_id": job_id}))], REVIEW_ID)


DETAILS_ID = "relay_details"  # The id the relay's fold had first: one id for the fold on every card.
# What a part under the answer is called in the fold's title (``details``), by its element id.
DETAIL_WORDS = {"answer_acceptance": "验收说明", "answer_change": "改动"}
# The parts the owner must see, never folded (T647 P1-A2: folded in grey small print, they went unseen): a commit the
# workdir's repository does not have (``check_element``) and the completion criteria not met (``criteria_element``).
ALERT_IDS = (CHECK_ID, CRITERIA_ID)


def alerts(change=None, notes=()) -> bool:
    """Whether a turn's parts hold one of ALERT_IDS: a commit of ``change`` not found (``not_found``), or among ``notes`` (the
    elements app/answer_cards.py notes makes) the completion criteria not met. A 做完了 relay with one goes on its own card,
    never merged into 「N 个话题做完了」 (app/butler_relay.py relay)."""
    return bool(not_found(change)) or any(isinstance(note, dict) and note.get("element_id") == CRITERIA_ID
                                          for note in notes or ())


def details(parts) -> list:
    """The parts under an answer that are there to look up (产物, 验收说明, 改动) in one closed fold whose title names
    them, so the answer stays what the card is for (the owner, 2026-11-22: 底下的东西占了太高); one part alone stays as it is.
    The parts the owner must see (ALERT_IDS) stay out of it, first. The 输入/输出 lines stay outside, under it."""
    parts = [part for part in parts if part]
    shown = [part for part in parts if part.get("element_id") in ALERT_IDS]
    parts = [part for part in parts if part.get("element_id") not in ALERT_IDS]
    if len(parts) < 2:
        return [*shown, *parts]
    words = []
    for part in parts:
        title = ((part.get("header") or {}).get("title") or {}).get("content", "") if part.get("element_id") == ARTIFACTS_ID else ""
        word = title or DETAIL_WORDS.get(str(part.get("element_id") or ""), "")
        if word and word not in words:
            words.append(word)
    return [*shown, collapsible(DETAILS_ID, " · ".join(words or ["更多"]) + "（点开看）", parts, quiet=True, spacing="6px")]


def tail(files=None, inputs=(), outputs=(), link="", change=None, notes=(), buttons=()):
    """What follows an answer: its 产物 panel (``files``, None without), its 改动 fold (``change``,
    ``change_element``: None, {} or '' without), the line marking a commit not found (``check_element``) and the
    ``notes`` (elements: what to check of a UI change, ``acceptance_element``; the completion criteria not met,
    ``criteria_element``), those to look up in one fold when more than one and those to see first, outside it
    (``details``), then the 输入/输出 lines, then ``buttons`` (elements: 独立复核, ``review_element``) under the lines, where they can be clicked at once; one of DETAIL_WORDS among them
    goes into the fold. The 记下了 lines (MEMORY_ID, in ``notes`` or ``buttons``) are never folded: they come first, right
    under the answer, with or without 产物."""
    def is_memory(element):
        return isinstance(element, dict) and element.get("element_id") == MEMORY_ID

    memory = [element for element in (*(notes or ()), *(buttons or ())) if is_memory(element)]
    notes, buttons = [e for e in notes or () if not is_memory(e)], [e for e in buttons or () if not is_memory(e)]
    looked_up = [element for element in buttons or () if isinstance(element, dict)
                 and element.get("element_id") in DETAIL_WORDS]
    others = [element for element in buttons or () if element not in looked_up]
    parts = details([files, change_element(change), check_element(change), *(notes or ()), *looked_up])
    return [element for element in (*memory, *parts, facts_element(inputs, outputs, link), *others) if element]


def tail_card(elements, summary, lead="", *, words=False, **row):
    """The card after an answer sent as a file or on cards of its own (cards/pages.py: they could not hold it too):
    ``lead`` (one small grey line), then ``elements``; a "你问" block among them (``ask_block``) becomes the speaker's
    row (cards/ui.py speaker_row, ``row`` its other arguments: ``outcome``, ``label``, ``avatar``, ``url``), the same as
    the answer's card has. ``words``: nothing above it carried the owner's words (a file): the line of them that opens
    (``words_fold``) under a row that does not quote him (a topic's, a lead's)."""
    from covey.cards.ui import BODY_PADDING, BODY_SPACING, banded, role_of, speaker_row, split_ask

    meta, rest = split_ask(elements)
    head = [md(grey(lead), size="notation")] if lead else []
    if not meta:
        return banded(card([*head, *rest], summary, update_multi=True))
    said = [part for part in (words_fold(meta),) if part] if words and role_of(meta.get("who")) != "butler" else []
    return banded(card([speaker_row(meta, **row), *said, *head, *rest], summary, update_multi=True,
                       padding=BODY_PADDING, spacing=BODY_SPACING))  # A topic's badge has its colour in the config.


# What a turn's status is as the state tag of its topic's row (core/look.py STATES keys).
STATUS_TAGS = {"done": "result", "failed": "stopped", "interrupted": "stopped", "cancelled": "cancelled",
               "waiting_user": "waiting"}


def topic_head(label, title, status):
    """What atop a 话题区's answer card in its own thread says whose it is (core/placement.py Row.answer_head): the
    topic's row as the relay has it (cards/ui.py speaker_row: its T number's badge, its title, the turn's state as a
    tag, "[T43] **改看板** [有结果]"), with no link (the card is in the thread) and nothing quoted (the owner's message
    is right above it). A block that only carries the row (``ASK_META``, cards/ui.py split_ask takes it), never sent;
    None without a T number or title."""
    who = " ".join(part for part in (str(label or "").strip(), " ".join(str(title or "").split())) if part)
    if not who:
        return None
    return {"tag": "markdown", "content": "", ASK_META: {
        "who": who, "doing": "", "outcome": STATUS_TAGS.get(str(status or ""), "result"), "url": ""}}


ASK_CHARS = 70


# What the speaker did with the owner's message, as one sentence (not
# "管家 · 你问 · 12:31", which reads as if the butler asked). Keyed by (``verb``, ``doing``); {at} is " 12:31 " or "".
SAYS = {
    ("你问", "answer"): "答复你{at}的问题", ("你答", "answer"): "收到你{at}的回答",
    ("你问", "done"): "做完了你{at}交代的事", ("你答", "done"): "按你{at}的回答做完了",
    ("你问", "waiting"): "办你{at}交代的事，要你定", ("你答", "waiting"): "按你{at}的回答接着办，要你定",
    ("你问", "stopped"): "办你{at}交代的事时停了", ("你答", "stopped"): "按你{at}的回答接着办时停了",
    ("你问", "about"): "关于你{at}的问题", ("你答", "about"): "关于你{at}的回答",
    ("你问", "running"): "在办你{at}交代的事", ("你答", "running"): "按你{at}的回答在办",
    # A project's topic: the lead handed it the work, so no words of the owner's are quoted (app/question_display.py);
    # once its question is settled, what it does (cards/question_cards.py mirror_card), not "关于你的问题".
    ("你问", "member"): "办负责人派的事，要你定", ("你答", "member"): "办负责人派的事，要你定",
    ("你问", "member_settled"): "办负责人派的事", ("你答", "member_settled"): "办负责人派的事",
}
ALONE = "主动来汇报{at}"  # A speaker that answers no message of the owner's (a lead woken by the Bridge).


def says(verb, doing, at) -> str:
    """The sentence after the speaker: "答复你 12:31 的问题", "做完了你 09:12 交代的事"; "主动来汇报 · 09:40" for
    ``doing`` "report", a turn no message of the owner's started."""
    if doing == "report":
        return ALONE.format(at=f" · {at}" if at else "")
    template = SAYS.get((verb, doing)) or SAYS.get((verb, "answer")) or SAYS[("你问", "answer")]
    return template.format(at=f" {at} " if at else "")


def ask_block(text, *, verb="你问", at="", url="", who="", doing="answer", meta=True, avatar="", full="", parts=()):
    """The block above an answer, a conclusion or a question's copy at the private chat's top level saying which
    message of the owner's it answers: grey, two
    lines, "你问 · 12:31 ›" and the message's words (two lines at most); the whole block opens ``url`` (the
    message's thread, app/answer_cards.py ask_link). Style "A" of four samples. ``who``: its speaker (docs/ui.md 5.3,
    cards/ui.py ``speaker``) and what it did with that message in one sentence (``says``, by ``doing``: answer, done,
    waiting, stopped, about, running, member, member_settled, report), "**管家** 答复你 12:31 的问题 ›" over 「his words」; with no words, that line
    alone (a lead's wake-up turn: "主动来汇报 · 09:40"). None without words or a speaker.
    ``meta``: the block also carries what a title bar says (cards/ui.py ``ASK_META``: who, what it did, when, the
    words, the link), for the card that takes the block into its header (``split_ask``) and is never sent; a card
    that keeps the block as an element (the dispatch card) asks for none. ``avatar``: the speaker's picture, an image key
    (app/answer_cards.py ``avatar``), carried with it. ``full``: the owner's words whole, ``parts``: an answer's turn's
    questions and answers (app/answers.py answer_parts), carried for the fold that shows them (``words_fold``)."""
    from covey.cards.ui import speaker

    words = " ".join(str(text or "").split())
    if not words and not who:
        return None
    words = words if len(words) <= ASK_CHARS else words[:ASK_CHARS - 1] + "…"
    arrow = "　›" if url else ""
    sentence = says(verb, doing, at) if who else ""
    if who:
        head = speaker(who) + " " + grey(sentence + arrow)
        quoted = f"「{words}」"
    else:
        head = grey(" · ".join(part for part in (verb if words else "", at) if part) + arrow)
        quoted = words
    lines = [md(head, size="notation")] + ([md(grey(literal_text(quoted)), size="normal_v2")] if words else [])
    box = {"tag": "interactive_container", "element_id": ASK_ID, "width": "fill", "padding": "6px 10px 6px 10px",
           "corner_radius": "6px", "background_style": "grey-50", "vertical_spacing": "2px", "elements": lines}
    if url:
        box["behaviors"] = [{"type": "open_url", "default_url": url}]
    if meta and who:
        whole = " ".join(str(full or "").split()) and str(full).strip()[:ANSWER_FULL_CHARS]
        box[ASK_META] = {"who": who, "doing": doing, "verb": verb, "at": at, "says": sentence.strip(), "words": words,
                         "url": url, **({"avatar": avatar} if avatar else {}), **({"full": whole} if whole else {}),
                         **({"parts": [[str(kind), str(said)] for kind, said in parts]} if parts else {})}
    return box


WORDS_ID = "answer_words"
WORDS_LINE = 24  # Characters of the owner's words the one line shows (one line on a phone too); the fold has them all.


def words_fold(meta):
    """What the owner said that this card answers, as one grey line that opens (the owner, 2026-11-22: 回带卡没有对应上我的
    问题，收折起来只留一行): "你问 14:22 · 把话题里的卡片…" or "你答 14:22 · 要不要部署？→ 现在部署", folded; opened, his
    words whole (``full``) or each question small and grey with his answer under it (``parts``, as ``answer_block``).
    For a card whose row does not quote him (cards/ui.py speaker_row: a topic's or a lead's, not the butler's). None
    without words."""
    meta = meta or {}
    words = " ".join(str(meta.get("words") or "").split())
    if not words:
        return None
    shown = words if len(words) <= WORDS_LINE else words[:WORDS_LINE - 1] + "…"
    label = " ".join(part for part in (str(meta.get("verb") or "你问"), str(meta.get("at") or "")) if part)
    title = grey(literal_text(f"{label} · {shown}"))
    parts = [(kind, text) for kind, text in meta.get("parts") or () if str(text or "").strip()]
    if parts:
        elements = [md(_answer_lines(str(text).strip(), kind in ("question", "heading")),
                       size="notation" if kind in ("question", "heading") else "normal_v2")
                    for kind, text in parts[:ANSWER_FULL_PARTS]]
    else:
        whole = str(meta.get("full") or "").strip() or words
        elements = [md(literal_lines(whole), size="normal_v2")]
    return fold(WORDS_ID, md(title, size="notation"), elements, title_padding="0px 0px 0px 0px",
                icon_position="follow_text", spacing="4px")


HANDOFF_ID = "handoff_back"
HANDOFF_CHARS = 3000  # Of the owner's words on the line a topic's thread gets: more ends with "…".


def handoff_card(head, words, url):
    """The line a topic's thread gets when the butler hands it the owner's words (app/butler_topics.py topic_send,
    app/butler_handoff.py thread_card): a grey block, ``head`` ("管家转来你 17:54 的消息") with " ›" and his words as
    written, the whole block opening ``url``, his message at its place in the chat. The chat list previews ``head``."""
    text = str(words or "").strip()
    text = text if len(text) <= HANDOFF_CHARS else text[:HANDOFF_CHARS - 1] + "…"
    elements = [md(grey(f"{head}　›"), size="notation")] + ([md(literal_lines(text), size="normal_v2")] if text else [])
    box = {"tag": "interactive_container", "element_id": HANDOFF_ID, "width": "fill", "padding": "8px 10px 8px 10px",
           "corner_radius": "6px", "background_style": "grey-50", "vertical_spacing": "4px", "elements": elements,
           "behaviors": [{"type": "open_url", "default_url": url}]}
    return card([box], head)


def hook_card(rows):
    """The small reply under the owner's message a quiet butler turn handed on to topics that were there
    (app/butler_reply.py hook_topics): one line a topic, "→ T53「标题」 ›", the line
    opening its thread (``rows``: (words, url), url '' for none: no › and not clickable), the card at its compact
    width. None without rows."""
    elements = []
    for index, (words, url) in enumerate(rows):
        line = {"tag": "interactive_container", "element_id": f"hook_{index}", "width": "fill",
                "padding": "4px 8px 4px 8px", "corner_radius": "6px", "background_style": "grey-50",
                "elements": [md(grey(literal_text(f"→ {words}") + ("　›" if url else "")), size="notation")]}
        if url:
            line["behaviors"] = [{"type": "open_url", "default_url": url}]
        elements.append(line)
    if not elements:
        return None
    built = card(elements, " ".join(f"→ {words}" for words, _ in rows))
    built["config"]["width_mode"] = "compact"  # Small (ACCEPT-351: at the chat's full width one line left it empty).
    return built


ANSWER_FULL_CHARS = 2000  # Of an answer's whole words in its "你答" fold: more ends with "…".
ANSWER_FULL_PARTS = 24  # Its texts (a question, an answer, a heading, a note each): the rest joins the last.
# Of its opened texts' JSON: literal_text may turn a character into an entity, and a question's copy (app/question_display.py
# mirror) holds the fold beside up to 12 KB of output and the form, under CARD_BYTES.
ANSWER_FULL_BYTES = 5000


def _answer_lines(text, small):
    """One text of the opened 你答 fold, as written; a question or heading grey line by line (a font tag does not
    carry across lines in the client), an empty answer a grey "—"."""
    lines = [literal_text(line) for line in text.split("\n")]
    if not text:
        return grey("—")
    return "\n".join(grey(line) if line else line for line in lines) if small else "\n".join(lines)


def answer_block(parts, words="", *, verb="你答", at=""):
    """The "你答" block of an answer's turn (a long one is not cut off unread): ``ask_block``'s
    grey box as a client-side fold, folded, same element ID; its title "你答 · 14:51" and ``words`` (one line,
    "question → answer"), opened the whole answer, ``parts`` (app/answers.py answer_parts): each question small and
    grey, its answer in full, then 补充说明 / 另外补充, all as written (``literal_text``), at most ANSWER_FULL_CHARS.
    None without words or parts. An answer's turn has no message of the owner's to open, so no link."""
    words = " ".join(str(words or "").split()) or " ".join(
        " ".join(text.split()) for kind, text in parts if kind in ("question", "answer") and text)
    if not words:
        return None
    words = words if len(words) <= ASK_CHARS else words[:ASK_CHARS - 1] + "…"
    head = " · ".join(part for part in (verb, at) if part)
    elements, left, room = [], ANSWER_FULL_CHARS, ANSWER_FULL_BYTES
    for kind, text in parts:
        if left <= 0 or room <= 0:
            break
        text = str(text or "").strip()
        if len(text) > left:
            text, left = text[:left - 1].rstrip() + "…", 0
        else:
            left -= len(text)
        small = kind in ("question", "heading")
        content = _answer_lines(text, small)
        while text and json_size(content) > room:  # Cut to the bytes left, a tenth at a time.
            text, left = text[:max(0, int(len(text) * 0.9) - 1)].rstrip() + "…", 0
            content = _answer_lines(text, small) if len(text) > 1 else grey("…")
            if len(text) <= 1:
                break
        room -= json_size(content)
        if len(elements) == ANSWER_FULL_PARTS:
            elements[-1]["content"] += "\n\n" + content
            continue
        elements.append(md(content, size="notation" if small else "normal_v2"))
    title = md(grey(head) + "\n" + grey(literal_text(words)), size="normal_v2")
    return {"tag": "collapsible_panel", "element_id": ASK_ID, "expanded": False, "background_color": "grey-50",
            "border": {"color": "grey-50", "corner_radius": "6px"}, "padding": "6px 10px 6px 10px",
            "vertical_spacing": "6px",
            "header": {"title": title, "vertical_align": "center", "icon": chevron(), "icon_position": "right",
                       "icon_expanded_angle": -180},
            "elements": elements or [md(grey(literal_text(words)), size="normal_v2")]}


def literal_text(text):
    """``text`` shown as written: no Markdown or Feishu tag of the owner's message takes effect."""
    return literal(text)


def literal_lines(text):
    """``text`` of several lines shown as written, its line breaks kept: ``literal_text`` writes a break as "\\n" (a file
    name stays one line), which the card showed as such in the owner's words and their 附件 line (2026-11-22, T651), so
    each line is escaped on its own."""
    return "\n".join(literal_text(line) for line in str(text or "").replace("\r\n", "\n").split("\n"))
