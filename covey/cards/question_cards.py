"""The question form, its standalone card, the feedback card and the answer notices."""
from __future__ import annotations

import re

from covey.cards.elements import (BUTTON_SPACING, button_look, button_row, callback, callback_button, card, markdown_panel,
                                  notice_card, plain, topic_button)
from covey.cards.review import compact_text
from covey.cards.streaming import RECOMMENDED
from covey.core import triage, turn_words
from covey.core.stale import STALE_NOTE

SKIP_VALUE = "skip"
SKIP_LABEL = "不回答此题"
SUPPLEMENT = "supplement"  # The card-wide 补充说明 field.
# The button beside 提交回答 that leaves every question unanswered and sends only 补充说明, which it requires
# (the owner, 2026-10-28: skipping a model's questions to give a new instruction took a 不回答此题 per question).
# Since 2026-11-04 it is also the way to say the questions do not fit: "问题不合适 / 修改要求" opened a second card
# for the same thing (the owner, 18:05: 为啥要这么多选项; 18:27 picked the simpler bottom).
SKIP_ALL_KIND = "bridge_question_skip"
SKIP_ALL_LABEL = "只发补充说明"
SUPPLEMENT_HINT = "不答这些题时，写原因或新要求"
STATUS_LABEL = "查看提交状态"
# A top-level card's single-choice question (``top_level_card``) shows each option as a submit button, so one tap
# answers (the owner's night run, 2026-11-07: a dropdown took four steps and 600 px, and iOS's picker could not be cancelled).
# Its callback value's PICK is the option's index, or SKIP_VALUE for 不回答此题 (app/answers.py picked_fields).
PICK = "pick"
# Headers that name no topic: the default a bare checkpoint question gets, and numbering.
GENERIC_HEADER = re.compile(r"(补充信息|补充|信息|问题|说明|确认|选择|其他|第[一二三四五六七八九十\d]+题)")
# Where an answered question went: its follow-up task (continuation_job_id) in this state.
FOLLOW_UP = turn_words.FOLLOW_UP
# The superseded_reason of a question folded once a newer question card of its topic was shown
# (app/questions.py fold_older_questions); without it, an older duplicate retired once a later
# identical form was answered (app/checkpoint_questions.py).
FOLDED = "newer_question"


# 以后这类按推荐走 (app/standing_answers.py): under an open form whose every question has one recommended option, a
# button that turns this kind of question into a principle of the workspace once the owner confirms the sentence
# a small model wrote; then the question is answered with the recommended options. Its own card, so the form stays.
STANDING_KIND = "bridge_question_standing"
STANDING_LABEL = "以后这类按推荐走"
STANDING_SAVE, STANDING_EDIT, STANDING_CANCEL = "bridge_standing_save", "bridge_standing_edit", "bridge_standing_cancel"
STANDING_FIELD = "principle"


def recommended(questions):
    """[(question index, option index)] of the one recommended option of each question (its label marked （推荐） or
    (Recommended)), or None unless every question has exactly one; a multiple choice has none to go by."""
    picked = []
    for i, q in enumerate(questions or []):
        options = q.get("options") or [] if isinstance(q, dict) else []
        marked = [j for j, o in enumerate(options) if isinstance(o, dict) and RECOMMENDED.search(str(o.get("label") or ""))]
        if len(marked) != 1 or q.get("multiple") or q.get("multiSelect"):
            return None
        picked.append((i, marked[0]))
    return picked or None


STANDING_ENV = "COVEY_STANDING_ANSWERS"  # 0 takes the button off the cards; on by default.


def standing_offered(record):
    """Whether ``record``'s open form gets 以后这类按推荐走: the switch is on, its every question has one recommended
    option, and it is a topic's (a card can name it: not the butler's, whose questions are not a workspace's)."""
    from covey.core.butler import traits
    from covey.core.process_env import env_flag
    return (env_flag(STANDING_ENV, True) and bool(record.get("topic_id")) and recommended(record.get("questions")) is not None
            and traits(record.get("topic_id"), record.get("chat_id")).card_target)


def standing_row(record):
    """The 以后这类按推荐走 button under an open form (``recommended``), outside the form, which it does not submit."""
    return button_row([topic_button(STANDING_LABEL, element_id="standing_action", behaviors=callback(
        {"kind": STANDING_KIND, "topic_id": record["topic_id"], "token": record["token"]}))], "standing_actions")


def standing_card(form):
    """The card of one 以后这类按推荐走 (``form``, app/standing_answers.py): the sentence to keep as a principle of the
    workspace with 记下 / 改一下 / 算了, the same in a box to change, or what became of it."""
    status, text, where = form.get("status"), str(form.get("text") or ""), f"工作区 {form.get('workspace')}"
    value = {"token": form["token"]}
    picks = "；".join(str(row) for row in form.get("picks") or [])
    if status == "saved":
        done = f"已记下（{where}的原则）：{text}\n\n" + (
            f"这道题已按推荐回答：{picks}" if form.get("answered") else "这道题已经答过或取消了，没有再答。")
        return notice_card(done, "已记下原则")
    if status == "cancelled":
        return notice_card("算了，没有记下。这道题还在上面等你回答。", "没有记下原则")
    notice = [{"tag": "markdown", "content": form["notice"]}] if form.get("notice") else []
    lead = {"tag": "markdown", "content": f"**以后这类按推荐走**：记成{where}的一条原则，以后这类问题按它直接做，不再问你。"
                                          + (f"\n这道题会按推荐回答：{picks}" if picks else "")}
    if status == "editing":
        return card([lead, *notice, {"tag": "form", "name": "standing_" + form["token"], "elements": [
            {"tag": "input", "name": STANDING_FIELD, "input_type": "multiline_text", "rows": 3, "width": "fill",
             "max_length": 200, "default_value": text, "placeholder": plain("一句话，最多 200 字")},
            {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "button", "text": plain("记下"), "type": "primary_filled", "width": "fill",
                     "form_action_type": "submit", "name": "standing_save",
                     "behaviors": [{"type": "callback", "value": {"kind": STANDING_SAVE, **value}}]}]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "button", "text": plain("算了"), "type": "default", "width": "fill",
                     "form_action_type": "submit", "name": "standing_cancel",
                     "behaviors": [{"type": "callback", "value": {"kind": STANDING_CANCEL, **value}}]}]}]}]}],
                    "改一下这条原则", default_width=True)
    return card([lead, {"tag": "markdown", "content": f"> {text}"}, *notice, button_row([
        topic_button("记下", tone="primary", behaviors=callback({"kind": STANDING_SAVE, **value})),
        topic_button("改一下", behaviors=callback({"kind": STANDING_EDIT, **value})),
        topic_button("算了", behaviors=callback({"kind": STANDING_CANCEL, **value}))], "standing_choices")],
        "以后这类按推荐走？", default_width=True)


HELD_NOTE = "已交负责人：它能依据专项记忆答的就替你答，答不了或 3 分钟没处理，会转给你。"
ESCALATED_NOTE = "负责人没有答，转给你了，见话题里的问题卡。"


def triage_element(record):
    """The task card's line for a question the project's lead has (held) or handed back (escalated, with its why)."""
    why = str(triage.of(record).get("why") or "")
    text = HELD_NOTE if triage.held(record) else ESCALATED_NOTE + (f"理由：{why}" if why else "")
    return {"tag": "markdown", "element_id": "q_" + record["token"], "content": text}


def folded(record):
    """Whether ``record`` was folded under a newer question of its topic (``superseded_by_job`` asked it)."""
    return record.get("status") == "superseded" and record.get("superseded_reason") == FOLDED


def folded_refusal(record):
    """Why a click on a folded question's form sent nothing, written on the clicked card."""
    return f"这组问题已收起，#{record.get('superseded_by_job')} 又提了问题，请在新卡上回答"


def question_header(q):
    """A question's own header, unless it names no topic (``GENERIC_HEADER``): "" then."""
    header = compact_text(q.get("header") or "", 24)
    return "" if not header or GENERIC_HEADER.fullmatch(header) else header


def question_lead(i, count, q):
    """Question ``i``'s bold first line: ``**1/3 · 借鉴美临**``; one question shows only its header."""
    header = question_header(q)
    label = f"{i + 1}/{count}" + (f" · {header}" if header else "") if count > 1 else header
    return f"**{label}**" if label else ""


def question_element(record, tags="", *, headed=False):
    """A question's element: its open form, or once settled the folded panel that says what came of it.
    ``tags``: ``T191 · #1084``, which lead the form's first line on the standalone card; ``headed``: the
    card's title bar says them and 需要你回答, so the form starts with its state."""
    token = record["token"]
    if record.get("status") == "superseded" and not folded(record):
        return {"tag": "markdown", "element_id": "q_" + token,
                "content": f"相同问题已在后续卡片回答 · 后续任务 #{record['continuation_job_id']}。无需重复提交。"}
    if folded(record):
        return _settled_panel(record, token)
    if triage.held(record):
        return triage_element(record)
    if record.get("display_pending") or (record.get("body_delivery_required") and not record.get("body_delivery_confirmed")):
        return {"tag": "markdown", "element_id": "q_" + token,
                "content": "完整结果或问题控件尚未送达，请使用本话题任务卡的重试按钮，重新投递后再回答。"}
    if record.get("status") in {"cancelled", "uncertain", "submitting", "answered"}:
        return _settled_panel(record, token)
    return _question_form(record, token, tags, headed)


def settled_title(record):
    """The folded question's first line: whose question it is and where it went, e.g.
    ``❓ #1107 的提问 · 已回答 → #1111 ✅ 已完成``, ``❓ #1106 的 2 道题 · 你提了反馈 → #1125 又提了问题``
    (the owner asked "这个是啥东西？" of one that read only 回答已送达 · 后续任务已完成, #1127)."""
    count = len(record["questions"])
    title = f"❓ #{record['job_id']} 的" + ("提问" if count == 1 else f" {count} 道题")
    status, delivery = record["status"], record.get("delivery_state", "delivered")
    if folded(record):
        return title + f" · 已收起 → #{record.get('superseded_by_job')} 又提了问题"
    if status == "cancelled":
        reason = {"switched": "话题已换配置，直接在话题里回复新模型即可", "rewound": "这一轮已回退，问题作废",
                  "stale": STALE_NOTE, "lead_stop": "负责人按你的意思停了这个话题",
                  "web_stop": "你在网页上停了这个话题"}.get(record.get("cancel_reason"), "")  # stale: closed by daily maintenance.
        return title + " · 已取消" + (" · " + reason if reason else "")
    answer = "反馈" if record.get("response_kind") == "feedback" else "回答"
    if status == "submitting":
        return title + f" · {answer}已保存，正在发送"
    if status == "uncertain":
        return title + f" · {answer}已保存，送达未确认，请先核查，不会自动重复发送"
    title += " · 你提了反馈" if answer == "反馈" else " · 已回答"
    if delivery in FOLLOW_UP:
        follow_up = f"#{record['continuation_job_id']}" if record.get("continuation_job_id") else "后续任务"
        return title + f" → {follow_up} {FOLLOW_UP[delivery]}"
    return title + {"awaiting_ack": "，等待模型确认收到", "queued": "，等待继续执行"}.get(delivery, "")


def settled_summary(record):
    """The folded question's panel: each question and its answer, or the questions and the feedback;
    folded under a newer question, the questions and where to say more."""
    count = len(record["questions"])
    if folded(record):
        asked = "\n\n".join("\n".join(filter(None, [question_lead(i, count, q), q["question"]]))
                            for i, q in enumerate(record["questions"]))
        return asked + f"\n\n这组问题没有回答。要补充的，写在 #{record.get('superseded_by_job')} 的问题卡上。"
    if record.get("response_kind") == "feedback":
        asked = "\n".join("- " + q["question"][:300] for q in record["questions"])
        return f"原问题：\n{asked}\n\n你的反馈：\n{record['feedback']}\n\n原问题没有被回答，也不表示同意。"
    parts = []
    for i, q in enumerate(record["questions"]):
        answer = "；".join((record.get("answers", {}).get(q["id"]) or {}).get("answers", []))
        parts.append("\n".join(filter(None, [question_lead(i, count, q), q["question"], "答：" + answer])))
    summary = "\n\n".join(parts)
    return summary + ("\n\n补充说明：\n" + record["supplement"] if record.get("supplement") else "")


def _settled_panel(record, token):
    """The collapsed panel of a question whose answer is saved, being sent, unconfirmed or cancelled.
    查看提交状态 only while the delivery is unconfirmed: once the follow-up runs, the title says it."""
    title = settled_title(record)
    panel = markdown_panel("q_" + token, title, settled_summary(record)[:10000] or title)
    if record["status"] in {"submitting", "uncertain"} or (record["status"] == "answered"
                                                           and record.get("delivery_state") == "awaiting_ack"):
        panel["elements"].append(callback_button(STATUS_LABEL, {"kind": "bridge_question_status",
                                                                "topic_id": record["topic_id"], "token": token}))
    if record.get('status') == 'uncertain' and record.get('answers'):
        panel['elements'].append({'tag':'button','text':{'tag':'plain_text','content':'核查已保存回答并继续'},
            'behaviors':[{'type':'callback','value':{'kind':'bridge_question_resume','topic_id':record['topic_id'],'token':token}}]})
    return panel


def single_choice(questions):
    """Whether ``questions`` is one question with options, one of which is picked: what option buttons can answer."""
    return len(questions) == 1 and bool(questions[0].get("options")) and not questions[0].get("multiple")


TOP_COPY = "top_copy"  # Set (never saved) on the record a 话题区 question's top-level copy is drawn from: mirror_card.


def top_level_card(record):
    """Whether ``record``'s card is at the private chat's top level: the top-level copy of a 话题区's question
    (mirror_card)."""
    return bool(record.get(TOP_COPY))


def picks(record):
    """Whether ``record``'s form shows its options as buttons: a single_choice question on a card at the top level
    (``top_level_card``). A topic's cards keep the dropdown (the owner: 话题里的卡先不动)."""
    return single_choice(record["questions"]) and top_level_card(record)


def how_to_answer(record):
    """The rules of answering, in the form's folded 怎么回答 panel: one line above it says what to do."""
    if picks(record):
        return "\n".join("- " + rule for rule in [
            f"点一个选项就提交它；不想回答点“{SKIP_LABEL}”。",
            "选项都不合适时写在回答框，再点“提交回答”。",
            "其他想说的写在最下面的“补充说明”，点选项时一起带上。",
            f"不想答这道题（问题不合适、想改要求）：写在“补充说明”，点“{SKIP_ALL_LABEL}”。",
            f"也可以{_reply_where(record)}，当作这道题的回答。"])
    multiple = len(record["questions"]) > 1
    rules = [f"不想回答的题选“{SKIP_LABEL}”（没有选项的题勾选它）。",
             "选项都不合适时写在回答框；写了回答就以你写的为准，下拉框里选的不算。",
             "其他想说的（例如不回答的原因）写在最下面的“补充说明”。",
             f"不想答这些题（问题不合适、想改要求或给新要求）：写在“补充说明”，点“{SKIP_ALL_LABEL}”。所有题记为"
             f"{SKIP_LABEL}，不表示同意；模型按补充说明重新评估。"]
    if multiple:
        rules += [("每题都处理后提交才能继续。" if record["blocking"] else "每题都处理后才能提交。"),
                  f"{_reply_where(record)}会先保存为补充，提交回答时一起带上；也可在补充回执上选择作为问题反馈发送。"]
    else:
        rules.append(f"也可以{_reply_where(record)}，当作这道题的回答。")
    return "\n".join("- " + rule for rule in rules)


def _reply_where(record):
    """Where the owner writes an answer as a message: in the topic, or, for a card at the top level
    (``top_level_card``), quoting this card in the private chat (its quote goes to the topic: core/topics.py
    context_for_topic)."""
    return "引用这张卡直接回复" if top_level_card(record) else "直接在话题里回复"


def _question_form(record, token, tags="", headed=False):
    """The open form: whose questions they are, one line on how to answer and the rest folded, each
    question's fields, 补充说明 and the buttons. T191 #1084's three questions came after ten lines of
    rules, without numbers or headers, their options in the same type as the questions."""
    state = ("等待回答后继续" if record["blocking"] else "本轮已结束，可继续补充" if record.get("task_finished")
             else "其他工作继续")
    count = len(record["questions"])
    buttons = picks(record)
    line = (f"共 {count} 道题：逐题选择或填写，最后点“提交回答”。" if count > 1
            else f"点选项就提交；都不合适时写在回答框再点“提交回答”，也可以{_reply_where(record)}。" if buttons
            else f"在卡里选择或填写后点“提交回答”，也可以{_reply_where(record)}。")
    if headed:
        elements = [{"tag": "markdown", "content": f"**{state}** · {line}"}]
    else:
        elements = [{"tag": "markdown", "content": f"**{tags + ' · ' if tags else ''}需要你补充 · {state}**"},
                    {"tag": "markdown", "content": line}]
    if triage.state(record) == triage.ESCALATED and triage.of(record).get("why"):
        elements.insert(1, {"tag": "markdown", "content": f"负责人转给你的理由：{triage.of(record)['why']}"})
    elements.append(markdown_panel("qh_" + token, "怎么回答", how_to_answer(record), size="notation", quiet=True))
    for i, q in enumerate(record["questions"]):
        elements.extend(_question_fields(i, q, count, record if buttons else None))
    # One place for anything beyond the answers, so no answer box doubles as a note.
    elements.append({"tag": "markdown", "content": "**补充说明**（可选）"})
    elements.append({"tag": "input", "name": SUPPLEMENT, "input_type": "multiline_text", "rows": 2,
                     "auto_resize": True, "max_rows": 5, "width": "fill", "max_length": 1000,
                     "placeholder": {"tag": "plain_text", "content": SUPPLEMENT_HINT}})
    # Two buttons on one row (the owner, 2026-11-04 18:27: four, each on its own line, read as too many). A card
    # sent before keeps its 问题不合适 / 修改要求, which still opens the feedback card (app/questions.py).
    submit = {"tag": "button", "text": {"tag": "plain_text", "content": "提交回答"}, "type": "primary_filled",
              "width": "fill", "form_action_type": "submit", "name": "submit",
              "behaviors": [{"type": "callback", "value": {"kind": "bridge_question", "topic_id": record["topic_id"],
                                                          "token": token}}]}
    skip = {"tag": "button", "text": {"tag": "plain_text", "content": SKIP_ALL_LABEL}, "type": "default",
            "width": "fill", "form_action_type": "submit", "name": "skip_all",
            "behaviors": [{"type": "callback", "value": {"kind": SKIP_ALL_KIND, "topic_id": record["topic_id"],
                                                        "token": token}}]}
    elements.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [submit]},
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [skip]}]})
    return {"tag": "form", "element_id": "q_" + token, "name": "question_form_" + token, "elements": elements}


def _pick_row(record, options):
    """The options of a picks() card, each a submit button as wide as its label, then 不回答此题, in a row that
    wraps (as build_stop_action_row). A button sends the form, so 补充说明 goes with it; its value says which."""
    def pick(name, label, value):
        return {"tag": "button", "text": plain(label[:100]), **button_look(), "form_action_type": "submit",
                "name": name, "behaviors": [{"type": "callback", "value": {
                    "kind": "bridge_question", "topic_id": record["topic_id"], "token": record["token"], PICK: value}}]}

    buttons = [pick(f"pick_{j}", o["label"], j) for j, o in enumerate(options)]
    buttons.append(pick("pick_skip", SKIP_LABEL, SKIP_VALUE))
    return {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": BUTTON_SPACING,
            "columns": [{"tag": "column", "width": "auto", "elements": [button]} for button in buttons]}


def _question_fields(i, q, count=1, record=None):
    """Question ``i`` of ``count``: its number and header in bold, its text, its options as a list, the
    dropdown (with ``record``, a picks() card's: a button per option), the answer box and, without options,
    a skip box."""
    elements = []
    lead = question_lead(i, count, q)
    for start in range(0, len(q["question"]), 6000):
        chunk = q["question"][start : start + 6000]
        elements.append({"tag": "markdown", "content": lead + "\n" + chunk if lead and not start else chunk})
    options = q.get("options") or []
    if options:
        if any(o.get("description") for o in options):
            listed = "\n".join(f"- **{o['label']}**" + (f"：{o['description']}" if o.get("description") else "")
                               for o in options)
            elements.append({"tag": "markdown", "content": listed[:6000]})
    if options and record is not None:
        elements.append(_pick_row(record, options))
    elif options:
        elements.append(
            {
                "tag": "multi_select_static" if q.get("multiple") else "select_static",
                "name": f"option_{i}",
                "width": "fill",
                "placeholder": {"tag": "plain_text", "content": "选择选项"},
                "options": [
                    {"text": {"tag": "plain_text", "content": o["label"][:100]}, "value": str(j)}
                    for j, o in enumerate(options)
                ] + [{"text": {"tag": "plain_text", "content": SKIP_LABEL}, "value": SKIP_VALUE}],
            }
        )
    elements.append(
        {
            "tag": "input",
            "name": f"answer_{i}",
            "input_type": "multiline_text",
            "rows": 2, "auto_resize": True, "max_rows": 5, "width": "fill",
            "placeholder": {"tag": "plain_text", "content": "选项都不合适时，在这里写你的回答" if options else "填写回答"},
            "max_length": 1000,
        }
    )
    if not options:  # No dropdown to carry the choice.
        elements.append({"tag": "checker", "name": f"skip_{i}", "checked": False,
                         "text": {"tag": "plain_text", "content": SKIP_LABEL}})
    return elements


def question_card(record, topic=""):
    """The standalone question card; ``topic``: its topic's label (T191), with the task's number before
    the form. No 🔁 换配置 since 2026-11-04: the task card above it has one (the owner: 为啥要这么多选项).
    ``refusal`` (not saved: app/interaction.py adds it) says under it why a click on its form sent nothing."""
    pending = record.get('status') == 'pending'
    # A quiet butler turn's own card at the top level (app/question_display.py ask_block_for) has no task
    # number: the owner saw no card with it (B4 review).
    number = "" if record.get("mirror_state") == "at_top" else f"#{record.get('job_id', '')}"
    tags = " · ".join(filter(None, [topic, number]))
    # An open form gets an orange title bar: in a topic it read like any notice (2026-11-02 review). A settled
    # card is repainted whole (QuestionDisplay.paint_standalone), so the bar goes with the form.
    count = len(record['questions'])
    form = pending and not record.get("display_pending") and not (
        record.get("body_delivery_required") and not record.get("body_delivery_confirmed"))
    header = ("orange", "❓ " + " · ".join(filter(None, [tags, "需要你回答"])) + (f" · {count} 题" if count > 1 else "")
              ) if form else None
    element = question_element(record, tags, headed=bool(header))
    # The topic list's preview while the card is the topic's latest message: what the open form's bar says.
    summary = (header[1] if header else f"需要回答 · {count} 项" if pending else "相同问题已回答 · 无需重复提交"
               if record.get('status') == 'superseded' and not folded(record)
               else element.get('header', {}).get('title', {}).get('content', '回答已保存'))
    elements = [element]
    if form and record.get("token") and standing_offered(record):
        elements.append(standing_row(record))
    if record.get('refusal'):
        elements.append({"tag": "markdown", "content": "⚠️ " + record['refusal']})
    return card(elements, summary, default_width=True, header=header)


def mirror_card(record, topic="", output="", ask=None, files=None):
    """The question card again at the private chat's top level (app/question_display.py mirror): the same form,
    with ``output``, what the turn wrote before it asked (the owner, 2026-11-07: 回答前要先看前面的输出), above it.
    Settled, it folds as the card in the topic does; the output stays. ``ask``: the "你问" block above it all.
    ``files``: the asking turn's 产物 panel, as its answer card in the topic has it (app/artifacts.py answer_files),
    right under the output (alone above the form when the output went as its own message)."""
    built = question_card({**record, TOP_COPY: True}, topic)  # Options as buttons (``top_level_card``).
    lead = []
    if output:
        from covey.cards.relay_card import reply_body
        lead.append({"tag": "markdown", "content": reply_body(output)})
    if files:
        lead.append(files)
    if output:
        lead.append({"tag": "markdown", "text_size": "notation",
                     "content": f"<font color='grey'>{topic or '话题'} 这一轮的输出，问题在下面；在这里答和在话题里答一样</font>"})
    if lead:
        built["body"]["elements"] = [*lead, {"tag": "hr"}, *built["body"]["elements"]]
    if ask:  # The "你问" block (cards/answer_card.py ask_block): which of the owner's messages it answers.
        built["body"]["elements"] = [ask, *built["body"]["elements"]]
    return built


def answer_notice_card(text):
    return card([{"tag": "markdown", "content": text}], "回答状态", update_multi=True)


def question_feedback_card(record):
    return card([{
        "tag": "form", "name": "question_feedback_" + record["token"],
        "elements": [
            {"tag": "markdown", "content": "**反馈问题 / 修改要求**\n说明哪些问题不合适，或你想修改什么。"
             "提交后模型会重新评估；这不是对原问题的回答或授权。"},
            {"tag": "markdown", "content": "当前问题：\n" + "\n".join(q["question"][:200] for q in record["questions"])[:3000]},
            {"tag": "input", "name": "feedback", "input_type": "multiline_text", "rows": 3,
             "width": "fill", "max_length": 1000,
             "placeholder": {"tag": "plain_text", "content": "说明问题、拒绝回答或修改要求（最多 1,000 字符）"}},
            {"tag": "button", "name": "submit_feedback", "form_action_type": "submit", "type": "primary_filled",
             "text": {"tag": "plain_text", "content": "提交反馈，重新讨论"},
             "behaviors": [{"type": "callback", "value": {
                 "kind": "bridge_question", "topic_id": record["topic_id"], "token": record["token"],
                 "mode": "feedback_submit"}}]},
        ],
    }], "反馈问题 / 修改要求")
