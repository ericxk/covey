"""整理专项's cards (docs/butler.md 整理专项, app/project_tidy.py): the proposal card (建个专项？), its rename form, the 整理专项 card of
a scheduled or asked-for 整理, and the line a topic's cards get once it joined a project (已归进「X」 · 移出). Each is drawn whole
from its view (a dict kept in the record), so a click redraws it the same way (rule 9: a form that changes replaces the card).
The buttons' kinds are in app/card_actions.py ACTIONS, handled before topic routing (app/unified.py PANELS)."""
from __future__ import annotations

from covey.cards.elements import (button_row, card, callback, item, md, note, panel_header, plain, section, styled_panel,
                                  text_button, topic_button)
from covey.core.text import literal as literal_text

CREATE_KIND = "bridge_tidy_create"  # 建专项
RENAME_KIND = "bridge_tidy_rename"  # 改个名再建: the card becomes the rename form
NAMED_KIND = "bridge_tidy_named"  # The rename form's 建专项
BACK_KIND = "bridge_tidy_back"  # The rename form's 取消: the proposal again
DECLINE_KIND = "bridge_tidy_decline"  # 不用 (a proposal card's, or one row's of the 整理专项 card)
NEVER_KIND = "bridge_tidy_never"  # 以后别提议
AUTO_KIND = "bridge_tidy_auto"  # 以后直接建
OK_KIND = "bridge_tidy_ok"  # 好 on one row of the 整理专项 card
ALL_KIND = "bridge_tidy_all"  # 全部照办
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
AFTER = "建成专项后：看板上这几个话题并成一格；这件事里的问题由负责人先过一遍，例行的它替你答；进展由它汇报。"
ASK_AUTO = "你已经连着 {} 次直接点了建：以后这种直接建，只留撤回？"
NAME_FIELD, GOAL_FIELD, WHY_FIELD = "name", "goal", "why"
CARD_ROWS = 5
OUTCOME = {"done": "已照办", "declined": "不用", "never": "以后不再提议", "failed": "没做成", "expired": "已过时"}
KIND_WORDS = {"create": "新建", "join": "归进", "leave": "移出", "merge": "合并", "split": "拆开", "rename": "改名", "fold": "收起"}
TIDY_ICON = "folder_outlined"


def topics_line(labels) -> str:
    """"T488 修 064 采购审批题包 · T505 优化 Bootstrap 下一环节": (label, title) pairs."""
    return " · ".join(" ".join(part for part in (label, title) if part) for label, title in labels)


def _value(kind, view, **more):
    return {"kind": kind, "proposal": view.get("proposal", ""), **more}


def proposal_card(view):
    """建个专项？ ``view``: proposal (id), labels [(T 号, 标题)], name, goal, why, first (the first proposal ever: one sentence of what
    a project is), ask_auto (5 in a row: offer 以后直接建), accepted (the count), state (open, or what came of it), note (the line
    under it once decided), lesson (a reason form under a 不用)."""
    elements = [md("这几个话题像是一件事：" + literal_text(topics_line(view.get("labels") or ())))]
    if view.get("why"):
        elements.append(note(literal_text(view["why"])))
    name, goal = literal_text(view.get("name") or ""), literal_text(view.get("goal") or "")
    elements.append(md(f"**建成专项「{name}」？**" + (f" 目标：{goal}" if goal else "")))
    if view.get("first"):
        elements.append(note(INTRO))
    elements.append(note(AFTER))
    state = view.get("state") or "open"
    if state == "open":
        if view.get("ask_auto"):
            elements.append(item(md(ASK_AUTO.format(view.get("accepted") or 5)),
                                 text_button("以后直接建", _value(AUTO_KIND, view), tone="primary")))
        elements.append(button_row([
            topic_button("建专项", tone="primary", behaviors=callback(_value(CREATE_KIND, view))),
            topic_button("改个名再建", behaviors=callback(_value(RENAME_KIND, view))),
            topic_button("不用", behaviors=callback(_value(DECLINE_KIND, view))),
            topic_button("以后别提议", behaviors=callback(_value(NEVER_KIND, view)))]))
    else:
        elements.append(md(f"<font color='grey'>{literal_text(view.get('note') or OUTCOME.get(state, ''))}</font>"))
        if view.get("lesson"):
            elements.append(lesson_form(_value(LESSON_KIND, view)))
    title = "建个专项？" if state == "open" else f"专项提议 · {OUTCOME.get(state, '已处理')}"
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
    elements = [md("这几个话题建成一个专项：" + literal_text(topics_line(view.get("labels") or ()))),
                {"tag": "form", "name": "tidy_rename", "elements": [name, goal, button_row([submit, back])]},
                note("你取的名字以后不会被整理专项改掉。")]
    return styled_panel(panel_header(ICON, "改个名再建", subtitle="整理专项"), elements, "改个名再建专项")


def lesson_form(value):
    """One line for why (optional): the judge reads it as a rule of 整理专项 from then on."""
    field = {"tag": "input", "name": WHY_FIELD, "width": "fill", "max_length": 200,
             "placeholder": plain("为什么不对（可以不写）：这句话会记进整理的规则")}
    submit = {"tag": "button", "name": "tidy_lesson", "form_action_type": "submit", "type": "default", "size": "small",
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
    if rows:
        elements.append(section(f"要你定的（{len(rows)} 条）"))
        for row in rows:
            line = literal_text(row.get("line") or "")
            if (row.get("state") or "open") == "open":
                elements.append(item(md("· " + line), text_button("好", {"kind": OK_KIND, "run": view.get("run", ""),
                                                                        "proposal": row.get("proposal", "")}, tone="primary"),
                                     text_button("不用", {"kind": DECLINE_KIND, "run": view.get("run", ""),
                                                        "proposal": row.get("proposal", "")})))
            else:
                elements.append(md(f"· {line} <font color='grey'>{literal_text(row.get('note') or OUTCOME.get(row.get('state'), ''))}</font>"))
        if any((row.get("state") or "open") == "open" for row in rows):
            elements.append(button_row([topic_button("全部照办", tone="primary",
                                                     behaviors=callback({"kind": ALL_KIND, "run": view.get("run", "")}))]))
    if state == "done" and not did and not rows:
        elements.append(md("专项和最近的话题都看过了，没有要改的。"))
    for words in view.get("skipped") or ():
        elements.append(note(literal_text(words)))
    tags = ((("正在整理", "blue"),) if state == "running" else (("没整理成", "red"),) if state == "failed"
            else ((f"{len(rows)} 条建议", "orange"),) if rows else (("整理好了", "green"),))
    head = panel_header(TIDY_ICON, "整理专项", subtitle="专项和最近 7 天的话题", tags=tags)
    return styled_panel(head, elements, "整理专项")


def joined_element(name, topic, pid):
    """The line a topic's card gets once it joined a project: 已归进「X」 and 移出."""
    return item(note(f"已归进专项「{literal_text(name)}」"),
                text_button("移出", {"kind": LEAVE_KIND, "topic_id": topic, "project_id": pid}))


def joined_card(name, topic, pid):
    """The line alone, in the thread of a topic that has neither a topic card nor a status card to carry it."""
    return card([joined_element(name, topic, pid)], f"已归进专项「{name}」", default_width=True, update_multi=True,
                padding="8px 12px 8px 12px")


def settings_row():
    """The 设置 card's row (私聊默认, COVEY_PROJECT_TIDY on): 整理专项 at once."""
    return [section("专项"), item(md("把专项和最近 7 天的话题重新看一遍，给出建议"),
                                   text_button("整理专项", {"kind": START_KIND}, tone="primary"))]
