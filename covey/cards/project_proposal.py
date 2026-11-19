"""整理专项's cards (docs/butler.md 整理专项, app/project_tidy.py): the proposal card (建个专项？), its rename form, the 整理专项 card of
a scheduled or asked-for 整理, and the line a topic's cards get once it joined a project (已归进「X」 · 移出). Each is drawn whole
from its view (a dict kept in the record), so a click redraws it the same way (rule 9: a form that changes replaces the card).
The buttons' kinds are in app/card_actions.py ACTIONS, handled before topic routing (app/unified.py PANELS)."""
from __future__ import annotations

from covey.cards.elements import (button_row, card, callback, item, md, note, panel_header, plain, section, styled_panel,
                                  text_button)
from covey.core.text import literal as literal_text

CREATE_KIND = "bridge_tidy_create"  # 建专项
RENAME_KIND = "bridge_tidy_rename"  # 改个名再建 (a card sent before name_form): the card becomes the rename form
NAMED_KIND = "bridge_tidy_named"  # 建专项 with the name on the card (name_form), or the rename form's
BACK_KIND = "bridge_tidy_back"  # The rename form's 取消: the proposal again (a card sent before name_form)
DECLINE_KIND = "bridge_tidy_decline"  # 暂不考虑 (a proposal card's), 不用 (one row's of the 整理专项 card)
NEVER_KIND = "bridge_tidy_never"  # 以后别提议 (a card sent before name_form)
AUTO_KIND = "bridge_tidy_auto"  # 以后直接建
OK_KIND = "bridge_tidy_ok"  # 好 on one row of the 整理专项 card
ALL_KIND = "bridge_tidy_all"  # 全部照办 (a card sent before 2026-11-18: no new card has it)
LEAVE_KIND = "bridge_tidy_leave"  # 移出 on a topic's card
LESSON_KIND = "bridge_tidy_lesson"  # 记下 under a 不用 or 移出: the owner's reason
START_KIND = "bridge_tidy_start"  # 整理专项 on the 设置 card
KINDS = (CREATE_KIND, RENAME_KIND, NAMED_KIND, BACK_KIND, DECLINE_KIND, NEVER_KIND, AUTO_KIND, OK_KIND, ALL_KIND, LEAVE_KIND,
         LESSON_KIND, START_KIND)
LABELS = {CREATE_KIND: "建专项", RENAME_KIND: "改个名再建专项", NAMED_KIND: "改名后建专项", BACK_KIND: "取消改名",
          DECLINE_KIND: "不用这条整理建议", NEVER_KIND: "以后别提议专项", AUTO_KIND: "以后直接建专项", OK_KIND: "照这条整理",
          ALL_KIND: "整理建议全部照办", LEAVE_KIND: "移出专项", LESSON_KIND: "记下整理的理由", START_KIND: "整理专项"}

ICON = "folder-add_outlined"
INTRO = "专项是一件要分几个话题、跨几天推进的事，由一个负责人带着。"
AFTER = ("建成专项后：这件事的话题（以后分出去的也算）在看板上并成一格；这件事里的问题由负责人先过一遍，"
         "例行的它替你答；进展由它汇报。")
ASK_AUTO = "你已经连着 {} 次直接点了建：以后这种直接建，只留撤回？"
NAME_FIELD, GOAL_FIELD, WHY_FIELD, TOPICS_FIELD, MORE_FIELD = "name", "goal", "why", "topics", "more"
CARD_ROWS = 5
OUTCOME = {"done": "已照办", "declined": "不用", "never": "以后不再提议", "failed": "没做成", "expired": "已过时"}
PROPOSAL_OUTCOME = {**OUTCOME, "declined": "不管了"}  # The proposal card's word for its 不管了 (暂不考虑 before 2026-11-19)
KIND_WORDS = {"create": "新建", "join": "归进", "leave": "移出", "merge": "合并", "split": "拆开", "rename": "改名", "fold": "收起"}
TIDY_ICON = "folder_outlined"


def topics_line(labels) -> str:
    """"T488 修 064 采购审批题包 · T505 优化 Bootstrap 下一环节": (label, title) pairs."""
    return " · ".join(" ".join(part for part in (label, title) if part) for label, title in labels)


def _value(kind, view, **more):
    return {"kind": kind, "proposal": view.get("proposal", ""), **more}


def topic_link(row) -> str:
    """The T number as a link that opens the topic (app/topic_links.py), or plain when there is no link."""
    label = literal_text(row.get("label") or "话题")
    return f"[{label}]({row['link']})" if row.get("link") else label


def span(row) -> str:
    """"19 天 · 118 轮" of a row (its days and the owner's turns), or ''."""
    days, turns = row.get("days"), row.get("turns")
    parts = ([f"{max(1, round(days))} 天"] if isinstance(days, (int, float)) and days >= 1 else []) + \
            ([f"{turns} 轮"] if isinstance(turns, int) and turns > 0 else [])
    return " · ".join(parts)


def topic_lines(view) -> list:
    """The topics of a proposal as the owner can tell them and judge (the owner, 2026-11-18: 我都不知道 T252 在哪…哪几个话题，
    这些话题我应该能点开看，同时你应该有这些话题的 Summary，不然我咋判断能建专项呢？): every topic of it, each its T number as a
    link that opens it, its title and how long it has gone on, then a summary: what it is doing (its 话题要点's 本质) and
    where it is now (its latest conclusion). A view kept before rows (labels only) is drawn as before."""
    rows = view.get("rows")
    if not rows:
        return [md("这几个话题像是一件事：" + literal_text(topics_line(view.get("labels") or ())))]
    lines = [md(f"这 {len(rows)} 个话题像是一件事：" if len(rows) > 1 else "这个话题推了很久，像是一件要长期做的事：")]
    for row in rows:
        how_long = span(row)
        lines.append(md(f"**{topic_link(row)}** {literal_text(row.get('title') or '（没有标题）')}"
                        + (f" <font color='grey'>· {how_long}</font>" if how_long else "")))
        summary = ([f"在做：{literal_text(row['essence'])}"] if row.get("essence") else []) + \
                  ([f"现在：{literal_text(row['now'])}"] if row.get("now") else [])
        if summary:
            lines.append(md("\n".join(summary)))
    return lines


def choice_text(choice) -> str:
    return " ".join(part for part in (choice.get("label"), choice.get("title")) if part)[:80] or "话题"


def name_form(view):
    """建专项 after the owner's own touches (the owner, 2026-11-18: 直接给一个可编辑的名字然后一个确认按钮就行…一个暂不考虑就行;
    在页面增删一下话题…让人微操一波): which topics (``choices``: the proposal's, ticked, then the recent others; a select's
    options are fixed when the card is sent, so any other topic goes in by its T number), the name ready to change, and
    建专项 / 不管了. A view without choices (kept before them) has the name alone."""
    elements = []
    choices = view.get("choices") or []
    if choices:
        chosen = [str(index) for index, choice in enumerate(choices) if choice.get("chosen")]
        elements += [md("**哪些话题**：勾掉不要的，或从最近的话题里加；不在里面的填 T 号"),
                     {"tag": "multi_select_static", "name": TOPICS_FIELD, "width": "fill", "placeholder": plain("选话题"),
                      "options": [{"text": plain(choice_text(choice)), "value": str(index)}
                                  for index, choice in enumerate(choices)], "selected_values": chosen},
                     {"tag": "input", "name": MORE_FIELD, "width": "fill", "max_length": 100,
                      "placeholder": plain("再加话题：填 T 号，比如 T488 T505")}]
    elements += [md("**专项名**：可以直接改"),
                 {"tag": "input", "name": NAME_FIELD, "required": True, "width": "fill", "max_length": 30,
                  "default_value": view.get("name") or "", "placeholder": plain("专项名：对象 + 要达成的事，12 个字以内")}]
    submit = {"tag": "button", "name": "tidy_build", "form_action_type": "submit", "type": "primary",
              "text": plain("建专项"), "behaviors": callback(_value(NAMED_KIND, view))}
    later = {"tag": "button", "name": "tidy_later", "type": "default", "text": plain("不管了"),
             "behaviors": callback(_value(DECLINE_KIND, view))}
    return {"tag": "form", "name": "tidy_name", "elements": [*elements, button_row([submit, later])]}


def proposal_card(view):
    """建个专项？ ``view``: proposal (id), rows [{label, title, link, essence, now, days, turns}] (``topic_lines``; labels [(T 号,
    标题)] in a view kept before them), name, goal, why, first (the first proposal ever: one sentence of what a project is),
    ask_auto (5 in a row: offer 以后直接建), accepted (the count), state (open, or what came of it), changed (what became of
    its topics since it was sent, while open: app/project_tidy.py follow), note (the line under it once decided), lesson (a
    reason form under a 暂不考虑). Open, it ends with the name to change and 建专项 / 不管了 (``name_form``)."""
    elements = topic_lines(view)
    if view.get("why"):
        elements.append(note("为什么提：" + literal_text(view["why"])))
    goal = literal_text(view.get("goal") or "")
    state = view.get("state") or "open"
    if state == "open" and view.get("changed"):
        elements.append(note(literal_text(view["changed"])))
    if goal:
        elements.append(md(f"**目标**：{goal}"))
    if view.get("first"):
        elements.append(note(INTRO))
    elements.append(note(AFTER))
    if state == "open":
        if view.get("ask_auto"):
            elements.append(item(md(ASK_AUTO.format(view.get("accepted") or 5)),
                                 text_button("以后直接建", _value(AUTO_KIND, view), tone="primary")))
        elements.append(name_form(view))
    else:
        elements.append(md(f"<font color='grey'>{literal_text(view.get('note') or PROPOSAL_OUTCOME.get(state, ''))}</font>"))
        if view.get("lesson"):
            elements.append(lesson_form(_value(LESSON_KIND, view)))
    title = "建个专项？" if state == "open" else f"专项提议 · {PROPOSAL_OUTCOME.get(state, '已处理')}"
    return styled_panel(panel_header(ICON, title, subtitle="整理专项"), elements, f"{title} · {view.get('name') or ''}")


def rename_form(view):
    """改个名再建: the name and the goal, both filled in with the proposal's, and 建专项 / 取消."""
    name = {"tag": "input", "name": NAME_FIELD, "required": True, "width": "fill", "max_length": 30,
            "default_value": view.get("name") or "", "placeholder": plain("专项名：对象 + 要达成的事，12 个字以内")}
    goal = {"tag": "input", "name": GOAL_FIELD, "width": "fill", "max_length": 200, "default_value": view.get("goal") or "",
            "placeholder": plain("目标：做到什么算完")}
    submit = {"tag": "button", "name": "tidy_named", "form_action_type": "submit", "type": "primary",
              "text": plain("建专项"), "behaviors": callback(_value(NAMED_KIND, view))}
    back = {"tag": "button", "name": "tidy_back", "type": "default", "text": plain("取消"),
            "behaviors": callback(_value(BACK_KIND, view))}
    elements = [md("建成一个专项：" + literal_text(topics_line(view.get("labels") or ()))),
                {"tag": "form", "name": "tidy_rename", "elements": [name, goal, button_row([submit, back])]},
                note("你取的名字以后不会被整理专项改掉。")]
    return styled_panel(panel_header(ICON, "改个名再建", subtitle="整理专项"), elements, "改个名再建专项")


def lesson_form(value):
    """One line for why (optional): the judge reads it as a rule of 整理专项 from then on."""
    field = {"tag": "input", "name": WHY_FIELD, "width": "fill", "max_length": 200,
             "placeholder": plain("为什么不对（可以不写）：这句话会记进整理的规则")}
    # Not the form's own name: Feishu refuses a card whose two components share one (code 230099, ErrCode 11310).
    submit = {"tag": "button", "name": "tidy_lesson_save", "form_action_type": "submit", "type": "default", "size": "small",
              "text": plain("记下"), "behaviors": callback(value)}
    return {"tag": "form", "name": "tidy_lesson", "elements": [field, submit]}


def lesson_card(text, value=None):
    """The answer to 移出 under a topic's card: what happened, and the reason form (``value`` its button's) when there is one."""
    elements = [md(literal_text(text))]
    if value is not None:
        elements.append(lesson_form(value))
    return card(elements, "整理专项", default_width=True, update_multi=True, padding="10px 12px 10px 12px", spacing="6px")


def tidy_card(view):
    """The 整理专项 card. ``view``: run (id), state (running, done, failed), progress {done, total, current} while it runs, why (the
    failure in words), did [lines done at once], rows [{proposal, line, state, note}] (at most CARD_ROWS), skipped {words},
    scope (the words it was narrowed to)."""
    state = view.get("state") or "running"
    rows = list(view.get("rows") or ())[:CARD_ROWS]
    elements = []
    if view.get("scope"):
        elements.append(note(f"只整理和「{literal_text(view['scope'])}」有关的"))
    if state == "running":
        progress = view.get("progress") or {}
        current = progress.get("current") or ""
        line = f"正在读 {literal_text(current)}（{progress.get('done', 0)}/{progress.get('total', 0)}）" if current else \
            "正在整理……" if not progress.get("total") else f"正在判断（{progress.get('total')} 个话题）"
        elements.append(md(line))
    elif state == "failed":
        elements.append(md(f"这次没整理成：{literal_text(view.get('why') or '出错了')}。可以过一会儿再试。"))
    did = list(view.get("did") or ())
    if did:
        elements += [section("已经做了")] + [md("· " + literal_text(line)) for line in did]
    proposed = [name for name in view.get("proposed") or () if name]
    if proposed:
        # Each new project is a proposal card of its own, with its topics linked and summed up, to change and confirm one by one.
        elements += [section(f"新专项的提议（{len(proposed)} 个）"),
                     md("、".join(f"「{literal_text(name)}」" for name in proposed)),
                     note("每个一张提议卡，在下面：话题都列全、能点开、带摘要，可以增删话题、改名，再逐个确认。")]
    if rows:
        elements.append(section(f"要你定的（{len(rows)} 条）"))
        for row in rows:
            line = row.get("line") or "" if row.get("md") else literal_text(row.get("line") or "")
            if (row.get("state") or "open") == "open":
                elements.append(item(md("· " + line), text_button("好", {"kind": OK_KIND, "run": view.get("run", ""),
                                                                        "proposal": row.get("proposal", "")}, tone="primary"),
                                     text_button("不用", {"kind": DECLINE_KIND, "run": view.get("run", ""),
                                                        "proposal": row.get("proposal", "")})))
            else:
                elements.append(md(f"· {line} <font color='grey'>{literal_text(row.get('note') or OUTCOME.get(row.get('state'), ''))}</font>"))
        # No 全部照办: each one is the owner's own yes (the owner, 2026-11-18). A card sent before keeps its button.
    pending = [name for name in view.get("pending") or () if name]
    if pending:
        # What this 整理 would propose again and still waits for him (the owner, 2026-11-18 23:19: 为啥现在我点了整理专项
        # 说不需要呢): named, to decide on the 看板 — never “没有要改的” while they wait.
        elements += [section(f"还在等你定（{len(pending)} 个）"),
                     md("、".join(f"「{literal_text(name)}」" for name in pending)),
                     note("之前提过、你还没定。在看板的专项区可以直接点「建专项」或「不管了」。")]
    if state == "done" and not did and not rows and not proposed and not pending:
        elements.append(md("专项和最近动过的话题都看过了，没有要改的。"))
    for words in view.get("skipped") or ():
        elements.append(note(words if view.get("skipped_md") else literal_text(words)))
    count = len(rows) + len(proposed) + len(pending)
    tags = ((("正在整理", "blue"),) if state == "running" else (("没整理成", "red"),) if state == "failed"
            else ((f"{count} 条建议", "orange"),) if count else (("整理好了", "green"),))
    head = panel_header(TIDY_ICON, "整理专项", subtitle=f"专项和{TIDY_SPAN}", tags=tags)
    return styled_panel(head, elements, "整理专项")


def joined_element(name, topic, pid):
    """The line a topic's card gets once it joined a project: 已归进「X」 and 移出."""
    return item(note(f"已归进专项「{literal_text(name)}」"),
                text_button("移出", {"kind": LEAVE_KIND, "topic_id": topic, "project_id": pid}))


def joined_card(name, topic, pid):
    """The line alone, in the thread of a topic that has neither a topic card nor a status card to carry it."""
    return card([joined_element(name, topic, pid)], f"已归进专项「{name}」", default_width=True, update_multi=True,
                padding="8px 12px 8px 12px")


TIDY_SPAN = "最近 7 天动过的话题"  # Topics with a turn in the last 7 days, not the ones opened then (the owner, 2026-11-18).
START_LINE = f"把专项和{TIDY_SPAN}重新看一遍，给出建议"


def board_rows(names):
    """The 专项 part of the bot menu's 看板 and of 早晚汇总 (cards/digest.py, COVEY_PROJECT_TIDY on; the owner, 2026-11-18: 不是说
    给专项整理做一个按钮么，咋没有呢): the projects by name, and 整理专项 at once."""
    shown = "、".join(f"「{literal_text(name)}」" for name in names[:6]) + (f" 等 {len(names)} 个" if len(names) > 6 else "")
    return [section(f"专项（{len(names)}）"), md(shown if names else "<font color='grey'>还没有专项</font>"),
            item(md(START_LINE), text_button("整理专项", {"kind": START_KIND}, tone="primary"))]
