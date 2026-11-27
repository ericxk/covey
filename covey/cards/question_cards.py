"""The question form, its standalone card, the feedback card and the answer notices."""
from __future__ import annotations

import dataclasses
import re

from covey.cards.board import OPEN_KIND
from covey.cards.elements import (BUTTON_SPACING, button_look, button_row, callback, card, fold, grey, item, md, note, plain,
                                  shown_width, text_button, topic_button)
from covey.cards.review import compact_text, opening
from covey.cards.ui import BODY_PADDING, BODY_SPACING, banded, peek, receipt, speaker_row, split_ask
from covey.core import card_limits, look, triage, turn_words, ui_samples
from covey.core.stale import STALE_NOTE
from covey.core.text import literal

SKIP_VALUE = "skip"
SKIP_LABEL = "不回答此题"
SUPPLEMENT = "supplement"  # The card-wide 补充说明 field.
# 提交回答 is the form's one way to send (T631): what was picked or written, and 补充说明. With 补充说明 written, a question
# left unanswered is 不回答此题 (app/answers.py with_note): only a 补充说明 skips every question, which is how the owner
# says the questions do not fit, gives a new instruction or asks about them first.
SUBMIT_LABEL = "提交回答"
SUPPLEMENT_HINT = "其他想说的：不答的原因、新要求，或想先问的话"
STATUS_LABEL = "查看提交状态"
# What a refused click had in the form (app/interaction.py refuse_on_card), never saved: the card redrawn with the refusal
# keeps it, so nothing typed is lost (a redrawn card's fields are empty otherwise).
DRAFT = "draft"
# compat: 只发补充说明 and 先问一句 on cards sent before T631, which merged them into 提交回答. Each sends the 补充说明 with every
# question 不回答此题 (app/interaction.py skip_all).
SKIP_ALL_KIND = "bridge_question_skip"
ASK_KIND = "bridge_question_ask"
# 不处理 (T604 #15, app/questions.py set_aside): every question 不回答此题 with nothing added, nothing sent now; the record is
# SET_ASIDE (its status and its response_kind) and off the 等你 lists. 撤销 (RESTORE_KIND) opens the form again until the
# topic's next turn takes it as its answer (take_set_aside; the record answered then, its response_kind still SET_ASIDE).
SET_ASIDE = "set_aside"
SET_ASIDE_KIND = "bridge_question_set_aside"
SET_ASIDE_LABEL = "不处理"
RESTORE_KIND = "bridge_question_restore"
RESTORE_LABEL = "撤销"
SET_ASIDE_SETTLED = "这组问题已经答过、收起或取消了，没有撤下"
SET_ASIDE_HELD = f"任务正停在这道题上等你回答，撤下的话它会一直等，没有撤下：回答它，或只写补充说明再点“{SUBMIT_LABEL}”"
SET_ASIDE_ANSWER = f"这组题你点了“{SET_ASIDE_LABEL}”，已从“等你”里撤下；要回答的话先点卡上的“{RESTORE_LABEL}”"
# 还有 N 件等你 under a card whose question the owner just settled (``others_row``): ``OTHERS`` is set on the record, never
# saved (app/question_display.py ``paint``): {"n": what still waits for him as the 看板 counts it, "board": 去看板 offered}.
OTHERS = "others_waiting"
# A top-level card's single-choice question (``top_level_card``) shows each option as a submit button, so one tap
# answers (a dropdown took four steps and 600 px, and iOS's picker could not be cancelled).
# Its callback value's PICK is the option's index, or SKIP_VALUE for 不回答此题 (app/answers.py picked_fields).
PICK = "pick"
# Headers that name no topic: the default a bare checkpoint question gets, and numbering.
GENERIC_HEADER = re.compile(r"(补充信息|补充|信息|问题|说明|确认|选择|其他|第[一二三四五六七八九十\d]+题)")
# Where an answered question went: its follow-up task (continuation_job_id) in this state.
FOLLOW_UP = turn_words.FOLLOW_UP
ASK_HEADLINE = 36  # Characters of the question the chat list's 要你定 shows (``ask_headline``); the card has it whole.
ASK_PART = 16  # Characters of each question 要你定 names when there are several (``ask_headline``).
RESUME_LABEL = "核查已保存回答并继续"
CONTEXT_CHARS = 120  # Characters of a question's background line (``question_context``).
# The superseded_reason of a question folded once a newer question card of its topic was shown
# (app/questions.py fold_older_questions); without it, an older duplicate retired once a later
# identical form was answered (app/checkpoint_questions.py).
FOLDED = "newer_question"


HELD_NOTE = "已交负责人：它能按记忆答的就替你答，答不了或 3 分钟没处理，会转给你。"  # 记忆, one word (docs/glossary.md).
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


_ASK_ENDS = "？?。.！!：:；;，, "


def ask_headline(record, limit=ASK_HEADLINE) -> str:
    """What the chat list's preview of an open question card names after 要你定 (T647 A6: "要你定：2 件事" said nothing
    of what waits): one question's own header (``question_header``), else the question itself; several, each one's
    header or the start of its words (ASK_PART), joined with "；" while they fit ``limit``, then "等 N 件". "N 件事" only
    when no question has a word to show."""
    questions = [q for q in record.get("questions") or () if isinstance(q, dict)]
    if len(questions) == 1:
        own = question_header(questions[0]) or compact_text(questions[0].get("question") or "", limit)
        return own or "1 件事"
    parts = []
    for q in questions:
        part = question_header(q) or compact_text(q.get("question") or "", ASK_PART)
        parts.append(part if part.endswith("…") else part.rstrip(_ASK_ENDS))
    parts = [part for part in parts if part]
    if not parts:
        return f"{len(questions)} 件事"
    shown, used = [], 0
    for part in parts:
        if shown and used + len(part) + 1 > limit:
            break
        shown.append(part)
        used += len(part) + 1
    return "；".join(shown) + (f" 等 {len(questions)} 件" if len(shown) < len(questions) else "")


def question_lead(i, count, q):
    """Question ``i``'s bold first line: ``**1/3 · 借鉴美临**``; one question shows only its header."""
    header = question_header(q)
    label = f"{i + 1}/{count}" + (f" · {header}" if header else "") if count > 1 else header
    return f"**{label}**" if label else ""


def question_element(record, tags="", *, headed=False, compact=0):
    """A question's element: its open form, or once settled the folded panel that says what came of it.
    ``tags``: ``T12 · #196``, which lead the form's first line when nothing above it says whose it is (a task card's
    form, a top-level copy sent before the speaker's row); ``headed``: the card's row (cards/ui.py speaker_row) says
    whose it is and 等你, so the form starts with its first question; ``compact``: the open form drawn smaller, for a card
    that cannot hold it whole (``_question_form``, ``fitted``)."""
    token = record["token"]
    if record.get("status") == "superseded" and not folded(record):
        return {"tag": "markdown", "element_id": "q_" + token,
                "content": f"相同问题已在后续卡片回答 · 后续任务 #{record['continuation_job_id']}。无需重复提交。"}
    if folded(record):
        return settled_element(record)
    if triage.held(record):
        return triage_element(record)
    if record.get("display_pending") or (record.get("body_delivery_required") and not record.get("body_delivery_confirmed")):
        return {"tag": "markdown", "element_id": "q_" + token,
                "content": "完整结果或问题控件尚未送达，请使用本话题任务卡的重试按钮，重新投递后再回答。"}
    if record.get("status") in {"cancelled", "uncertain", "submitting", "answered", SET_ASIDE}:
        return settled_element(record)
    return _question_form(with_dedup(record), token, tags, headed, compact)


DEDUP_MARKS, DEDUP_EXTRA, DEDUP_DRAWN = "dedup", "extra_questions", "dedup_drawn"  # app/question_dedup.py MARK_KEY, EXTRA_KEY.


def with_dedup(record) -> dict:
    """``record`` as an open form draws it (app/question_dedup.py ``drawn``): each question answered before with its grey line
    上次你答过：… under its words, then the ``extra_questions`` (the third same-class one). Every drawing of the form goes
    through here (the task card's element, the standalone card, the top-level copy, a repaint), so a repaint keeps both; a
    record drawn already is as it is. Only for drawing: the record's ``questions`` are never changed."""
    hits = record.get(DEDUP_MARKS) if isinstance(record.get(DEDUP_MARKS), dict) else {}
    extra = [q for q in record.get(DEDUP_EXTRA) or () if isinstance(q, dict) and q.get("id")]
    if record.get(DEDUP_DRAWN) or (not hits and not extra):
        return record
    questions = []
    for question in record.get("questions") or ():
        mark = hits.get(str(question.get("id"))) if isinstance(question, dict) else None
        if isinstance(mark, dict) and mark.get("line"):
            question = {**question, "question": f"{question.get('question') or ''}\n{grey(mark['line'])}"}
        questions.append(question)
    questions += [{key: value for key, value in q.items() if key != "class"} for q in extra]
    return {**record, "questions": questions, DEDUP_DRAWN: True}


CANCEL_REASONS = {"switched": "话题已换配置，直接在话题里回复新模型即可", "rewound": "这一轮已回退，问题作废",
                  "stale": STALE_NOTE, "lead_stop": "负责人按你的意思停了这个话题",
                  "web_stop": "你在网页上停了这个话题", "butler_stop": "管家按你的意思停了这个话题"}  # stale: closed by daily maintenance.


def settled_title(record):
    """What came of a settled question in one line, whose question it is and where it went: ``#17 的提问 · 已回答 → #21
    有结果``, ``#16 的 2 道题 · 你提了反馈 → #25 又提了问题`` (not only 回答已送达 · 后续任务已完成, which says nothing of
    what it was). The chat list's preview of its card and the grey line inside its fold (``settled_peek``); the ❓ it
    led with went with the old folded panel (T647 A4)."""
    count = len(record["questions"])
    title = f"#{record['job_id']} 的" + ("提问" if count == 1 else f" {count} 道题")
    status, delivery = record["status"], record.get("delivery_state", "delivered")
    if folded(record):
        return title + f" · 已收起 → #{record.get('superseded_by_job')} 又提了问题"
    if status == SET_ASIDE:
        return title + " · 你选了不处理 · 话题下一轮开始前可以撤销"
    if status == "answered" and record.get("response_kind") == SET_ASIDE:
        taken = record.get("continuation_job_id")
        return title + " · 你选了不处理" + (f" → 随 #{taken} 告诉了模型" if taken else "")
    if status == "cancelled":
        reason = CANCEL_REASONS.get(record.get("cancel_reason"), "")
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
    if record.get("response_kind") == SET_ASIDE:
        asked = "\n".join("- " + q["question"][:300] for q in record["questions"])
        how = {SET_ASIDE: f"从“等你”里撤下了，话题下一轮开始时告诉模型；在那之前点“{RESTORE_LABEL}”，这张卡回到能答的样子。",
               "answered": "已随话题的下一轮告诉模型。"}.get(record["status"], "")
        return f"原问题：\n{asked}\n\n你选了不处理：每题记为不回答（不表示同意或授权）。{how}"
    parts = []
    for i, q in enumerate(record["questions"]):
        answer = "；".join((record.get("answers", {}).get(q["id"]) or {}).get("answers", []))
        parts.append("\n".join(filter(None, [question_lead(i, count, q), q["question"], "答：" + answer])))
    summary = "\n\n".join(parts)
    return summary + ("\n\n补充说明：\n" + record["supplement"] if record.get("supplement") else "")


def settled_buttons(record):
    """The buttons a settled question keeps, in view under it (T647 A4: they were in a closed fold): 查看提交状态 while
    its answer's delivery is unconfirmed, 核查已保存回答并继续 for an uncertain one with answers, 撤销 for one set aside
    (until the topic's next turn takes it). [] for the others."""
    status = record["status"]
    value = {"topic_id": record["topic_id"], "token": record["token"]}
    labels = []
    if status in {"submitting", "uncertain"} or (status == "answered" and record.get("delivery_state") == "awaiting_ack"):
        labels.append((STATUS_LABEL, "bridge_question_status"))
    if status == "uncertain" and record.get("answers"):
        labels.append((RESUME_LABEL, "bridge_question_resume"))
    if status == SET_ASIDE:  # 不处理 taken back while the topic's next turn has not taken it.
        labels.append((RESTORE_LABEL, RESTORE_KIND))
    return [topic_button(label, behaviors=callback({"kind": kind, **value})) for label, kind in labels]


def settled_element(record):
    """A settled question (answered, being sent or unconfirmed, cancelled, set aside, folded under a newer one) as its
    top-level copy shows it, in its topic's thread and on a task card too (docs/ui.md 5.2 问题卡; T647 A4: the thread
    kept the old fold "❓ #431 的 2 道题 · 已回答"): its first lines in view, the rest folded with the answers
    (``settled_peek``), what came of it in a grey line under them (``settled_line``) and the buttons it keeps
    (``settled_buttons``). One element by the question's ID (q_<token>), so a task card's question is replaced in
    place as before."""
    parts = [settled_peek(record), *[line for line in (settled_line(record),) if line]]
    buttons = settled_buttons(record)
    if buttons:
        parts.append(button_row(buttons))
    return {"tag": "column_set", "element_id": "q_" + record["token"], "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_spacing": "4px", "elements": parts}]}


def single_choice(questions):
    """Whether ``questions`` is one question with options, one of which is picked: what option buttons can answer.
    A record saved before ``multiSelect`` was read as ``multiple`` (app/checkpoint_questions.py) says it the native way."""
    return (len(questions) == 1 and bool(questions[0].get("options"))
            and not (questions[0].get("multiple") or questions[0].get("multiSelect")))


TOP_COPY = "top_copy"  # Set (never saved) on the record a 话题区 question's top-level copy is drawn from: mirror_card.


def top_level_card(record):
    """Whether ``record``'s card is at the private chat's top level: the top-level copy of a 话题区's question
    (mirror_card)."""
    return bool(record.get(TOP_COPY))


def aside_offered(record):
    """Whether ``record``'s open form gets 不处理 (T604 #15): a topic's question (not the butler's nor a lead's, which ask at
    the chat's top level: ``Traits.card_target``) that no turn is held on. A turn waiting in place for its answer (Claude's
    own question tool, ``native_live_question``) or paused on it (Codex's, ``native_paused_question``) reads nothing else
    until it is answered: one set aside would leave it waiting for ever."""
    from covey.core.butler import traits
    return (bool(record.get("topic_id")) and not record.get("native_live_question")
            and not record.get("native_paused_question") and traits(record.get("topic_id"), record.get("chat_id")).card_target)


def picks(record):
    """Whether ``record``'s form shows its options as buttons: a single_choice question, on a topic's card as on its
    top-level copy (``top_level_card``). Several questions or a multiple choice keep the dropdown. The topic's cards
    kept it until colleagues answered there too (T604; the owner had said 话题里的卡先不动)."""
    return single_choice(record["questions"])


HELP_ID = "qh_"  # 怎么回答, the form's last line (``answer_help``), by the question's token.


def answer_hint(record):
    """The grey line under the form's buttons, 怎么回答 and what to do in a few words, which opens on its rules
    (``how_to_answer``): a click on an option sends it, several questions go with 提交回答, and where else an answer may
    be written. Not between the card's row and the first question: there it pushed the questions off the first screen
    (the owner, 2026-11-23)."""
    questions = record["questions"]
    if len(questions) > 1:
        return f"怎么回答：逐题选择或填写，最后点“{SUBMIT_LABEL}”"
    if picks(record):
        return f"怎么回答：点选项就提交，也可以{_reply_where(record)}"
    done = "选好或写好" if questions and questions[0].get("options") else "写好"
    return f"怎么回答：{done}后点“{SUBMIT_LABEL}”，也可以{_reply_where(record)}"


def answer_help(record, compact=0):
    """The form's last element: ``answer_hint`` as one small grey line, folded on the rules (``how_to_answer``), the
    chevron after its words as on the line with the owner's words (cards/answer_card.py words_fold); ``compact``: the
    line alone, unfolded (COMPACT_HINTS when the answer boxes are gone)."""
    if compact:
        return md(grey(COMPACT_HINTS.get(compact) or answer_hint(record)), size="notation")
    return fold(HELP_ID + record["token"], md(grey(answer_hint(record)), size="notation"),
                [md(how_to_answer(record), size="notation")], title_padding="0px 0px 0px 0px",
                icon_position="follow_text", spacing="4px")


def how_to_answer(record):
    """The rules of answering, folded under the form's last line (``answer_help``)."""
    if picks(record):
        return "\n".join("- " + rule for rule in [
            f"点一个选项就提交它；不想回答点“{SKIP_LABEL}”。",
            f"选项都不合适时写在回答框，再点“{SUBMIT_LABEL}”。",
            f"其他想说的写在“补充说明”，点选项或“{SUBMIT_LABEL}”时一起带上。",
            f"不想答这道题（问题不合适、想改要求或有疑问）：只写“补充说明”，点“{SUBMIT_LABEL}”。这道题记为{SKIP_LABEL}，"
            "不表示同意；模型按补充说明重新评估。",
            *aside_rules(record),
            f"也可以{_reply_where(record)}，当作这道题的回答。"])
    multiple = len(record["questions"]) > 1
    listed = any(q.get("options") for q in record["questions"])  # A dropdown on the card: its rules.
    rules = [f"不想回答的题选“{SKIP_LABEL}”（没有选项的题勾选它）。" if listed else f"不想回答就勾选“{SKIP_LABEL}”。",
             *(["选项都不合适时写在回答框；写了回答就以你写的为准，下拉框里选的不算。"] if listed else []),
             "其他想说的（不回答的原因、问题不合适、新要求或疑问）写在“补充说明”。写了补充说明，没答的题记为"
             f"{SKIP_LABEL}，不表示同意；模型按补充说明重新评估。", *aside_rules(record)]
    if multiple:
        rules += ["没写补充说明时，每题都处理后才能提交" + ("，提交了任务才继续。" if record["blocking"] else "。"),
                  f"{_reply_where(record)}会先保存为补充，提交回答时一起带上；也可在补充回执上选择作为问题反馈发送。"]
    else:
        rules.append(f"也可以{_reply_where(record)}，当作这道题的回答。")
    return "\n".join("- " + rule for rule in rules)


def aside_rules(record):
    """怎么回答's line for 不处理 beside 提交回答 (``aside_offered``); none without it."""
    if not aside_offered(record):
        return []
    return [f"先不管这组题：点“{SET_ASIDE_LABEL}”。每题记为{SKIP_LABEL}，从“等你”里撤下，话题下一轮开始时告诉模型；"
            f"在那之前可以点“{RESTORE_LABEL}”。"]


def _reply_where(record):
    """Where the owner writes an answer as a message: in the topic, or, for a card at the top level
    (``top_level_card``), quoting this card in the private chat (its quote goes to the topic: core/topics.py
    context_for_topic)."""
    return "引用这张卡直接回复" if top_level_card(record) else "直接在话题里回复"


def waiting_state(record):
    """What the task does while ``record``'s questions wait: it waits for the answer, its turn ended, or it goes on."""
    return ("等你回答后继续" if record["blocking"] else "本轮已结束，可继续补充" if record.get("task_finished")
            else "其他工作继续")


# A form drawn smaller (``compact``) for a card that cannot hold it whole (``fitted``): 1, each question's words, background
# and options in one Markdown element and 怎么回答 one line; 2, no answer box under a question with options either (补充说明
# takes what no option says); 3 (BRIEF), no field a question: the questions listed, the owner's answers written in 补充说明
# and sent with 提交回答 (a question not answered on its own is 不回答此题, the model reading 补充说明), or answered on the
# web. 提交回答 is always there.
BRIEF = 3
COMPACT_HINTS = {2: f"怎么回答：逐题选择，选项都不合适的写在“补充说明”，最后点“{SUBMIT_LABEL}”",
                 BRIEF: f"怎么回答：把每题的回答写在“补充说明”里，点“{SUBMIT_LABEL}”"}
BRIEF_HEAD = "这组 {n} 道题放不下一张卡的逐题选项"
BRIEF_HOW = f"把每题的回答写在下面的“补充说明”里（如“1 选 A；2 选 B”），再点“{SUBMIT_LABEL}”"
BRIEF_WEB = "，或到网页工作台逐题作答"
BRIEF_CHARS, BRIEF_BYTES = 160, 12_000  # Each question's words in the list; the list, the rest counted as 还有 N 题.


def brief_questions(record):
    """The questions of a form drawn BRIEF, as one Markdown element: how to answer them, then each question's number, header,
    opening words and options."""
    from covey.core.butler import is_butler_topic

    questions = record["questions"]
    count = len(questions)
    # The butler's own questions are answered in Feishu only (docs/web.md 答题).
    web = "" if is_butler_topic(record.get("topic_id"), record.get("chat_id")) else BRIEF_WEB
    lines = [f"**{BRIEF_HEAD.format(n=count)}**", grey(BRIEF_HOW + web + "。")]
    used = sum(len(line.encode()) for line in lines)
    for i, q in enumerate(questions):
        lead = question_lead(i, count, q) or f"**{i + 1}.**"
        line = f"{lead} {literal(compact_text(q.get('question') or '', BRIEF_CHARS))}"
        options = " / ".join(str(o.get("label") or "")[:40] for o in q.get("options") or ())
        if options:
            line += "\n" + grey("选项：" + literal(options))
        if used + len(line.encode()) > BRIEF_BYTES:
            lines.append(grey(f"还有 {count - i} 题没有列出" + ("，到网页工作台看全部" if web else "")))
            break
        lines.append(line)
        used += len(line.encode())
    return md("\n".join(lines))


def _question_form(record, token, tags="", headed=False, compact=0):
    """The open form: its first question right under the card's row (``headed``) or under one bold line that says whose
    it is, each question's fields, 补充说明, the buttons, then how to answer as one grey line that opens
    (``answer_help``). Above the first question only what the owner needs to decide: a turn that does not wait for the
    answer says so in grey, a question its project's lead handed on says why (``lead_notes``). Not questions after ten
    lines of rules, without numbers or headers, their options in the same type as the questions; nor, before them, a
    band that said the card's row and the first question's header again (the owner, 2026-11-23)."""
    count = len(record["questions"])
    buttons = picks(record)
    if headed:  # The row says whose it is and 等你; "等你回答后继续" said nothing more.
        elements = [] if record["blocking"] else [note(waiting_state(record))]
    else:
        elements = [{"tag": "markdown",
                     "content": f"**{tags + ' · ' if tags else ''}{look.word('waiting')} · {waiting_state(record)}**"}]
    if triage.state(record) == triage.ESCALATED and triage.of(record).get("why"):
        elements.extend(lead_notes(record))
    draft = record.get(DRAFT) if isinstance(record.get(DRAFT), dict) else {}
    if compact >= BRIEF:
        elements.append(brief_questions(record))
    for i, q in enumerate(record["questions"] if compact < BRIEF else ()):
        elements.extend(_question_fields(i, q, count, record if buttons else None, draft, compact))
    # One place for anything beyond the answers, so no answer box doubles as a note.
    elements.append({"tag": "markdown", "content": "**补充说明**（可选）"})
    elements.append({"tag": "input", "name": SUPPLEMENT, "input_type": "multiline_text", "rows": 2,
                     "auto_resize": True, "max_rows": 5, "width": "fill", "max_length": 1000,
                     "placeholder": {"tag": "plain_text", "content": SUPPLEMENT_HINT}, **_typed(draft, SUPPLEMENT)})
    elements.append(_send_row(record, token))
    elements.append(answer_help(record, compact))
    return {"tag": "form", "element_id": "q_" + token, "name": "question_form_" + token, "elements": elements}


def _send_row(record, token):
    """The form's buttons (T631: five read as too many): 提交回答, the one submit, and beside it, when the form offers it
    (``aside_offered``), 不处理 as a bordered button of the same width, which sends no field. A card sent before keeps
    its 只发补充说明 and 先问一句 (SKIP_ALL_KIND, ASK_KIND) or 问题不合适 / 修改要求, which still opens the feedback card."""
    value = {"topic_id": record["topic_id"], "token": token}
    submit = {"tag": "button", "text": plain(SUBMIT_LABEL), "type": "primary_filled", "width": "fill",
              "form_action_type": "submit", "name": "submit", "behaviors": callback({"kind": "bridge_question", **value})}
    if not aside_offered(record):
        return submit
    aside = {"tag": "button", "text": plain(SET_ASIDE_LABEL), "type": "default", "width": "fill", "name": "set_aside",
             "behaviors": callback({"kind": SET_ASIDE_KIND, **value})}
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [button]} for button in (submit, aside)]}


# How a picks() card lays out its buttons (pick_rows), by their labels' shown_width (a CJK character two). The owner,
# 2026-11-24 (T746): short ones side by side as they were, many or long ones broken into rows.
PICK_ROW = 56  # What one row of buttons may take, PICK_CHROME counted for each: about 380 px. A desktop card is up to 600 px
# and does not wrap a row, so too long a one goes past its edge; a topic's side panel (272 px) and a phone wrap one.
PICK_CHROME = 4  # What a small bordered button takes beyond its label: its padding, its border and the gap to the next.
PICK_LONG = PICK_ROW // 2 - PICK_CHROME  # Wider than this, an option could not share a row with another as wide.


def pick_rows(labels):
    """The rows a picks() card puts its buttons in, ``labels`` being its options' and then 不回答此题's: their indexes in
    order, as many to a row as PICK_ROW takes, so a single row when they all fit; None when an option is long
    (PICK_LONG), the options then one under another."""
    if any(shown_width(label) > PICK_LONG for label in labels[:-1]):
        return None
    rows, used = [[]], 0
    for i, label in enumerate(labels):
        if rows[-1] and used + shown_width(label) + PICK_CHROME > PICK_ROW:
            rows.append([])
            used = 0
        rows[-1].append(i)
        used += shown_width(label) + PICK_CHROME
    return rows


def _pick_rows(record, options):
    """The buttons of a picks() card, a submit button an option then 不回答此题, as pick_rows lays them out: rows that
    wrap (button_row), each button as wide as its label; with a long option, each option a row of its own as wide as
    the card and 不回答此题 under them as wide as its label. Their words are the model's, of any length: in one row
    Feishu's desktop client squeezed three long options to "…" and pushed 不回答此题 past the card's edge, and one under
    another drew two short options as two bars across the card, three rows for three words (the owner, 2026-11-24
    T746). A button sends the form, so 补充说明 goes with it; its value says which."""
    def pick(name, label, value, width):
        return {"tag": "button", "text": plain(label), **button_look(), **({"width": width} if width else {}),
                "form_action_type": "submit", "name": name, "behaviors": [{"type": "callback", "value": {
                    "kind": "bridge_question", "topic_id": record["topic_id"], "token": record["token"], PICK: value}}]}

    labels = [o["label"][:100] for o in options]
    rows = pick_rows(labels + [SKIP_LABEL])
    buttons = [pick(f"pick_{j}", label, j, "" if rows else "fill") for j, label in enumerate(labels)]
    buttons.append(pick("pick_skip", SKIP_LABEL, SKIP_VALUE, "" if rows else "default"))
    if rows:
        return [button_row([buttons[i] for i in row]) for row in rows]
    return [{"tag": "column_set", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_spacing": BUTTON_SPACING, "elements": buttons}]}]


def lead_notes(record):
    """What the project's lead hands the owner with a question it escalated (app/triage.py escalate): its why, then, when it
    wrote them, a line of background in grey and its recommendation with what not taking it costs. Only the why without them."""
    found = triage.of(record)
    elements = [{"tag": "markdown", "content": f"负责人转给你的理由：{found['why']}"}]
    if found.get("context"):
        elements.append(note("背景：" + literal(compact_text(found["context"], CONTEXT_CHARS))))
    if found.get("recommend"):
        line = f"**负责人推荐：{literal(compact_text(found['recommend'], 100))}**"
        if found.get("if_not"):
            line += f"\n不选它的代价：{literal(compact_text(found['if_not'], CONTEXT_CHARS))}"
        elements.append({"tag": "markdown", "content": line})
    return elements


def question_context(q):
    """The one line of background a checkpoint's question may carry (``context``, core/checkpoint.py CHECKPOINT_RULES:
    what the question changes, why it is asked now), on one line and at most CONTEXT_CHARS; '' without one, or when it
    is not text."""
    raw = q.get("context") if isinstance(q, dict) else None
    return compact_text(raw, CONTEXT_CHARS) if isinstance(raw, str) else ""


def _typed(draft, name):
    """The ``default_value`` of input ``name`` that keeps what ``draft`` (DRAFT) had in it; {} without anything."""
    value = draft.get(name)
    return {"default_value": value[:1000]} if isinstance(value, str) and value.strip() else {}


def _chosen(draft, name, options, multiple):
    """The dropdown ``name``'s choice as ``draft`` (DRAFT) had it: ``initial_option`` or, ``multiple``, ``selected_values``;
    {} without one or with a value no option of this card has."""
    valid = [str(j) for j in range(len(options))] + [SKIP_VALUE]
    value = draft.get(name)
    if multiple:
        chosen = [str(v) for v in value if str(v) in valid] if isinstance(value, list) else []
        return {"selected_values": chosen} if chosen else {}
    return {"initial_option": str(value)} if isinstance(value, (str, int)) and str(value) in valid else {}


def short_question(q) -> bool:
    """Whether question ``q``'s words are one short line (at most ASK_HEADLINE characters) with no Markdown emphasis or
    code in them: the form shows such a question alone in bold, as its first line (``_question_fields``)."""
    text = str(q.get("question") or "").strip()
    return (0 < len(" ".join(text.split())) <= ASK_HEADLINE and "\n" not in text
            and not any(mark in text for mark in "*_`~"))


def _question_fields(i, q, count=1, record=None, draft=None, compact=0):
    """Question ``i`` of ``count``: its number and header in bold, its text whole (one short question with neither in
    bold itself, ``short_question``: the band that said it is gone), its background (``question_context``) in small grey
    print under it, its options as a list, the dropdown (with ``record``, a picks() card's: a button per option), the
    answer box and, without options, a skip box; each filled as ``draft`` (DRAFT) had it. ``compact`` (``_question_form``):
    the words, background and options one Markdown element; from 2 no answer box under the options."""
    draft = draft or {}
    elements = []
    if compact:
        return _compact_fields(i, q, count, record, draft, compact)
    lead = question_lead(i, count, q)
    text = f"**{q['question'].strip()}**" if not lead and short_question(q) else q["question"]
    for start in range(0, len(text), 6000):
        chunk = text[start : start + 6000]
        elements.append({"tag": "markdown", "content": lead + "\n" + chunk if lead and not start else chunk})
    background = question_context(q)
    if background:  # Read on a phone, a question alone did not say what it would change (T604 #14).
        elements.append(note("背景：" + literal(background)))
    options = q.get("options") or []
    # A question with samples says each option's description over its sample (``sample_blocks``), not again here.
    if options and not ui_samples.has(q):
        if any(o.get("description") for o in options):
            listed = "\n".join(f"- **{o['label']}**" + (f"：{o['description']}" if o.get("description") else "")
                               for o in options)
            elements.append({"tag": "markdown", "content": listed[:6000]})
    if options and record is not None:
        elements.extend(_pick_rows(record, options))
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
                **_chosen(draft, f"option_{i}", options, bool(q.get("multiple"))),
            }
        )
    elements.append(
        {
            "tag": "input",
            "name": f"answer_{i}",
            "input_type": "multiline_text",
            "rows": 2, "auto_resize": True, "max_rows": 5, "width": "fill",
            "placeholder": {"tag": "plain_text", "content": "选项都不合适时，在这里写你的回答" if options else "填写回答"},
            "max_length": 1000, **_typed(draft, f"answer_{i}"),
        }
    )
    if not options:  # No dropdown to carry the choice.
        elements.append({"tag": "checker", "name": f"skip_{i}", "checked": draft.get(f"skip_{i}") in (True, "true"),
                         "text": {"tag": "plain_text", "content": SKIP_LABEL}})
    return elements


def _compact_fields(i, q, count, record, draft, compact):
    """``_question_fields`` drawn ``compact`` (``_question_form``): the question's number and header, its words, its
    background grey and its options listed (each description beside its label, but not where a sample block says it) in
    one Markdown element, then the same dropdown or buttons; the answer box only below 2 or without options, the skip box
    without options."""
    lead = question_lead(i, count, q)
    words = str(q.get("question") or "").strip()[:6000]
    parts = [lead, f"**{words}**" if not lead and short_question(q) else words]
    background = question_context(q)
    if background:
        parts.append(grey("背景：" + literal(background)))
    options = q.get("options") or []
    if options and not ui_samples.has(q) and any(o.get("description") for o in options):
        parts.append("\n".join(f"- **{o['label']}**" + (f"：{o['description']}" if o.get("description") else "")
                               for o in options)[:3000])
    elements = [{"tag": "markdown", "content": "\n".join(part for part in parts if part)}]
    full = _question_fields(i, {**q, "context": None, "question": ""}, count, record, draft)
    # What follows the words in the full fields: the dropdown or buttons, the answer box, the skip box.
    rest = [element for element in full if element.get("tag") != "markdown"]
    if options and compact >= 2:
        rest = [element for element in rest if not (element.get("tag") == "input" and element.get("name") == f"answer_{i}")]
    return [*elements, *rest]


def others_row(record):
    """The grey line 还有 N 件等你 under a settled question's card, with 去看板 when the bot has the 看板 (the button sends
    one, cards/board.py OPEN_KIND); [] unless the record says something else waits (``OTHERS``)."""
    others = record.get(OTHERS)
    if record.get("status") == "pending" or not isinstance(others, dict) or not others.get("n"):
        return []
    line = receipt(f"还有 {others['n']} 件等你")
    return [item(line, text_button("去看板", {"kind": OPEN_KIND}))] if others.get("board") else [line]


def question_card(record, topic="", title="", *, rowed=False):
    """The standalone question card; ``topic``: its topic's label. No 🔁 换配置: the task card above it has one.
    ``refusal`` (not saved: app/interaction.py adds it) says under it why a click on its form sent nothing.
    In its thread (``topic`` or ``title``, the topic's title) it opens with the topic's row as every card of a topic does
    (cards/ui.py speaker_row: its T number's badge, its title, 等你 or how it was settled; no link, the card is in the
    thread), and the first question follows it (docs/ui.md 5.2 问题卡). ``rowed``: a row goes above it either way (the
    top-level copy's, ``mirror_card``). With no row the form's first line says whose it is (``question_element``).
    Drawn to fit one card (``question_fit``)."""
    return question_fit(record, topic, title, rowed=rowed).card


@dataclasses.dataclass(frozen=True)
class Fit:
    """A question card drawn to fit one card (``fitted``): ``card``; ``whole``, nothing of it left out or drawn smaller;
    ``left_out``, the samples (by ``sample_blocks``' order) not drawn on it."""
    card: dict
    whole: bool = True
    left_out: tuple = ()


def within(built) -> bool:
    """Whether ``built`` keeps within what one card may hold (CARD_BYTES of JSON, CARD_NODES tagged nodes)."""
    return ui_samples.size(built) <= CARD_BYTES and ui_samples.nodes(built) <= CARD_NODES


def question_fit(record, topic="", title="", *, rowed=False, outer=None, elsewhere=()) -> Fit:
    """``question_card`` as a ``Fit``. ``outer`` (card -> card): what goes round the card before it is sent (the top-level
    copy's row, the turn's output, its 产物: ``mirror_fit``), counted with it, so what is sent is what was measured;
    ``elsewhere``: the samples another card of the question draws (``sample_blocks``)."""
    pending = record.get('status') == 'pending'
    # A quiet butler turn's own card at the top level (app/question_display.py ask_block_for) has no task
    # number: the owner saw no card with it (B4 review).
    number = "" if record.get("mirror_state") == "at_top" else f"#{record.get('job_id', '')}"
    tags = " · ".join(filter(None, [topic, number]))
    count = len(record['questions'])
    form = pending and not record.get("display_pending") and not (
        record.get("body_delivery_required") and not record.get("body_delivery_confirmed"))
    # The open form had a conclusion band ("等你 · #431", 要你定：…, 等你回答后继续) between the row and the questions: it
    # said the row's 等你 and the first question's header again and pushed the questions off the first screen (the owner,
    # 2026-11-23). The row says whose it is and that it waits; what is asked is the chat list's preview alone
    # (``ask_headline``: the questions' headers or words, not "2 件事"), with no task number in it.
    who = " ".join(part for part in (topic, " ".join(str(title or "").split())) if part)
    own_row = bool(who) and not record.get(TOP_COPY)
    # The topic list's preview while the card is the topic's latest message: what is asked of the owner, else what came
    # of the question (``settled_title``).
    summary = (f"{topic + ' ' if topic else ''}{look.word('waiting')} · 要你定：{ask_headline(record)}" if form
               else f"{look.word('waiting')} · {count} 题" if pending else "相同问题已回答 · 无需重复提交"
               if record.get('status') == 'superseded' and not folded(record)
               else settled_title(record))
    row = [speaker_row({"who": who, "doing": "waiting"}, outcome="waiting",
                       tag=SETTLED_TAGS.get(record.get("status")), url="")] if own_row else []
    look_of = dict(padding=BODY_PADDING, spacing=BODY_SPACING) if own_row else {}

    def drawn(shown, compact=0):
        element = question_element(record, tags, headed=own_row or rowed, compact=compact)
        elements = [*row, *shown, element, *others_row(record)]
        if record.get('refusal'):
            elements.append({"tag": "markdown", "content": "⚠️ " + record['refusal']})
        built = banded(card(elements, summary, default_width=True, **look_of))  # The badge's colours.
        return outer(built) if outer else built
    return fitted(record, drawn, form, elsewhere)


# 样张 (core/ui_samples.py, docs/ui.md 12): what the open form's question card draws above its first question.
SAMPLES_HEAD = "下面 {n} 版样张由卡片 JSON 画成，样张里的按钮点不动；选哪一版，点最下面对应的选项。"
SAMPLE_LEFT_OUT = "这版样张放不下这张卡，没有画"
SAMPLE_IN_TOPIC = "这版样张放不下这张卡，话题里的问题卡上有"  # Left out of the top-level copy, drawn on the card in the topic.
SAMPLES_LEFT_OUT = "这 {n} 版样张放不下这张卡，没有画"
SAMPLES_IN_TOPIC = "，话题里的问题卡上画了 {n} 版"
# What one card may hold (scripts/cardkit_check.py LIMITS: 30 KB of JSON, 200 tagged nodes), a little kept free: the targets of
# core/card_limits.py.
CARD_BYTES, CARD_NODES = card_limits.TARGET_BYTES, card_limits.TARGET_NODES


def sample_blocks(record, left_out=(), elsewhere=()):
    """Each sample of ``record``'s questions (core/ui_samples.py ``kept``) as the card draws it: (elements, colours), its
    option's label in bold, the option's description in grey under it and then the sample, or why it is not drawn (a note
    ``carry`` kept, or SAMPLE_LEFT_OUT for the ``left_out`` ones, by their order: SAMPLE_IN_TOPIC for one of them another
    card of the question draws, ``elsewhere``)."""
    blocks = []
    for index, (head, kept) in enumerate(_sample_heads(record)):
        shown = [] if index in left_out else kept.get("elements") or []
        why = ((SAMPLE_IN_TOPIC if index in elsewhere else SAMPLE_LEFT_OUT) if index in left_out
               else kept.get("note", ""))
        colors = (kept.get("colors") or {}) if shown else {}
        blocks.append(([md(head), *shown, *([note(why)] if why else [])], colors))
    return blocks


def _sample_heads(record):
    """(the bold label and grey description over a sample, what ``carry`` kept of it) for each sample, in order."""
    heads = []
    many = len(record["questions"]) > 1
    for i, q in enumerate(record["questions"]):
        for option in q.get("options") or ():
            kept = ui_samples.kept(option)
            if not kept:
                continue
            label = f"{question_header(q) or f'第 {i + 1} 题'} · " if many else ""
            head = f"**{label}{literal(str(option.get('label') or ''))}**"
            if option.get("description"):
                head += "\n" + grey(literal(str(option["description"])))
            heads.append((head, kept))
    return heads


def samples_brief(record, elsewhere=()):
    """Every sample left out, in one Markdown element: how many, how many another card of the question draws
    (``elsewhere``), then each option's label and description as its block had them, a list."""
    heads = _sample_heads(record)
    first = SAMPLES_LEFT_OUT.format(n=len(heads)) + (SAMPLES_IN_TOPIC.format(n=len(elsewhere)) if elsewhere else "")
    listed = ["- " + head.replace("\n", "：", 1) for head, _ in heads]
    return md("\n".join([grey(first), *listed])[:6000])


def _steps(record, form, elsewhere=()):
    """What ``fitted`` tries, in order, as (shown above the form, its colours, the form's ``compact``): an open form with
    samples SAMPLES_HEAD, then each sample block (``sample_blocks``) after a thin rule, a rule under the last, then the
    same with the last samples left out one by one, then every sample in one line (``samples_brief``); then the form drawn
    smaller (``_question_form`` compact 1 and 2), and last BRIEF, the questions listed with no sample."""
    sampled = form and ui_samples.any_of(record["questions"])
    brief = []
    if sampled:
        carried = _options_with_samples(record)
        drawable = [index for index, option in enumerate(carried) if ui_samples.kept(option).get("elements")]
        for gone in range(len(drawable) + 1):
            left_out = tuple(drawable[len(drawable) - gone:])
            blocks = sample_blocks(record, left_out, elsewhere)
            shown = [note(SAMPLES_HEAD.format(n=len(carried)))]
            colors = {}
            for elements, own in blocks:
                shown += [{"tag": "hr"}, *elements]
                colors.update({name: value for name, value in own.items() if name not in colors})
            yield [*shown, {"tag": "hr"}], colors, 0, left_out
        brief = [samples_brief(record, elsewhere), {"tag": "hr"}]
        yield brief, {}, 0, tuple(drawable)
        left = tuple(drawable)
    else:
        yield [], {}, 0, ()
        left = ()
    if not form:
        return
    for compact in (1, 2):
        yield brief, {}, compact, left
    yield [], {}, BRIEF, left


def fitted(record, drawn, form, elsewhere=()) -> Fit:
    """``drawn(shown, compact)`` at the first of ``_steps`` that keeps within one card (``within``), else at the last; the
    samples' own colours join the card's config. Each step leaves less on the card, never the question's 提交回答."""
    built, whole, left_out = None, False, ()
    for step, (shown, colors, compact, left_out) in enumerate(_steps(record, form, elsewhere)):
        built = drawn(shown, compact)
        if colors:
            style = built.setdefault("config", {}).setdefault("style", {})
            style["color"] = {**colors, **style.get("color", {})}
        if within(built):
            whole = step == 0
            break
    return Fit(built, whole, left_out)


def _options_with_samples(record) -> list:
    """The options of ``record``'s questions that carry a sample, in the order ``sample_blocks`` draws them."""
    return [option for q in record["questions"] for option in q.get("options") or () if ui_samples.kept(option)]


SETTLED_TAGS = {"answered": ("已回答", "grey"), SET_ASIDE: ("不处理", "grey"), "cancelled": ("已取消", "grey"),
                "superseded": ("已收起", "grey")}
ANSWER_LINE_CHARS = 80


def settled_line(record):
    """The line a settled question keeps in view under its question (``settled_element``): what was answered and where it
    went, "→ 你选了「…」 · #21 在做"; an answer being sent or not confirmed yet says that ("· 已保存，送达未确认…"), a
    question folded under a newer one where to answer now, a cancelled one why, feedback and 不处理 that. One Markdown
    element; None for one still open, or answered on a later card (superseded, not folded)."""
    count = len(record.get("questions") or ())
    status = record.get("status")
    if not count or status in (None, "pending") or (status == "superseded" and not folded(record)):
        return None
    delivery, taken = record.get("delivery_state"), record.get("continuation_job_id")
    feedback = record.get("response_kind") == "feedback"
    if folded(record):
        answer = f"已收起：#{record.get('superseded_by_job')} 又提了问题，在新卡上回答"
    elif status == "cancelled":
        reason = CANCEL_REASONS.get(record.get("cancel_reason"), "")
        answer = "已取消" + (" · " + reason if reason else "")
    elif status == SET_ASIDE:
        answer = "你选了不处理 · 话题下一轮开始前可以撤销"
    elif record.get("response_kind") == SET_ASIDE:
        answer = "你选了不处理" + (f" · 随 #{taken} 告诉了模型" if taken else "")
    else:
        if feedback:
            answer = "你提了反馈，原问题没有回答"
        else:
            given = [" ".join("；".join((record.get("answers", {}).get(q["id"]) or {}).get("answers", [])).split())
                     for q in record["questions"]]
            words = " / ".join(part for part in given if part)
            answer = f"你选了「{compact_text(words, ANSWER_LINE_CHARS)}」" if words else "已回答"
        if status == "submitting":
            answer += " · 已保存，正在发送"
        elif status == "uncertain":
            answer += " · 已保存，送达未确认：先点“查看提交状态”核查，不会自动重复发送"
        elif taken and delivery in FOLLOW_UP:
            answer += f" · #{taken} {FOLLOW_UP[delivery]}"
        elif delivery in ("awaiting_ack", "queued"):
            answer += {"awaiting_ack": " · 等模型确认收到", "queued": " · 等待继续执行"}[delivery]
    return {"tag": "markdown", "content": grey("→ " + literal(answer)), "text_size": "notation"}


def settled_peek(record):
    """A settled question as 犹抱琵琶半遮面 (cards/ui.py peek), the head of ``settled_element``: the question's first lines in
    view (its own header bold before them), one click away the rest of it, what was answered and where it went
    (``settled_title``, grey). Several questions say how many before the first one's opening and open on all of them. Its
    buttons are not in it (``settled_buttons``: a closed fold hid them, T647 A4)."""
    token, questions = record["token"], record["questions"]
    count, first = len(questions), questions[0]
    header = question_header(first) if count == 1 else f"{count} 道题"
    lead = f"**{literal(header)}** " if header else ""
    head, rest = opening(first.get("question") or "", QUESTION_WIDTH - 2 * len(header) - 2)
    head = head or compact_text(first.get("question") or "", 40)
    plain = (count == 1 and record.get("status") == "answered"
             and record.get("response_kind") not in ("feedback", SET_ASIDE))
    body = ""
    if plain:  # The rest goes on from the opening, then the answer as the fold said it.
        answer = "；".join((record.get("answers", {}).get(first["id"]) or {}).get("answers", []))
        body = "\n".join(part for part in (rest, "答：" + answer if answer else "") if part)
        body += ("\n\n补充说明：\n" + record["supplement"]) if record.get("supplement") else ""
    body = body.strip() or settled_summary(record)
    elements = [{"tag": "markdown", "content": body[:10000]},
                {"tag": "markdown", "content": grey(literal(settled_title(record))), "text_size": "notation"}]
    # Inside the question's element (q_<token>, ``settled_element``): an ID of its own, at most 20 characters.
    return peek(PEEK_PREFIX + token[:20 - len(PEEK_PREFIX)], lead + head, elements)


OUTPUT_ID = "mirror_output"
PEEK_PREFIX = "qp_"  # A settled question's peek, inside its element q_<token>.
QUESTION_WIDTH = 120  # A settled question's opening, in its fold's title: about two lines.
OUTPUT_LABEL = "这一轮输出"


def mirror_card(record, topic="", output="", ask=None, files=None):
    """The question card again at the private chat's top level (app/question_display.py mirror): the same form, under
    the speaker's row that says whose it is (cards/ui.py speaker_row: ``ask``, the "你问" block, becomes the row, a click
    on it opening the topic's thread; the owner's words one line under it that opens, cards/answer_card.py words_fold).
    ``output``, what the turn wrote before it asked, comes first, whole (cards/review.py reply_shown), then a thin rule
    and the question and its options (nothing between them: the band that was there said the row again, the owner
    2026-11-23), then ``files``,
    the asking turn's 产物 panel as its answer card in the topic has it (app/artifacts.py answer_files). Settled, the
    question is ``settled_element`` as on its card in the thread (its first lines, the rest folded with the answer, a line
    under it of what he answered); the output stays, and the row says what it was about, not 要你定. Drawn to fit one
    card with all of that (``mirror_fit``)."""
    return mirror_fit(record, topic, output, ask, files).card


def mirror_fit(record, topic="", output="", ask=None, files=None) -> Fit:
    """``mirror_card`` as a ``Fit``: the row, the owner's words, the output and 产物 are counted with the form, so a copy
    that cannot hold them all leaves out its last samples, then draws its form smaller (``fitted``) — never one CardKit
    refuses (300305, 2026-11-27 T884: the samples fitted the card in the topic, the copy's row and 产物 took it to 201
    nodes). ``whole`` False: the copy could not hold all of it (app/question_display.py mirror sends the output on cards
    of its own first). A sample it leaves out that the card in the topic draws says so (SAMPLE_IN_TOPIC)."""
    from covey.cards.answer_card import says, words_fold
    from covey.cards.review import reply_body, reply_shown

    meta, _ = split_ask([ask] if ask else [])
    legacy = [ask] if ask and meta is None else []  # A block sent before the title bar: kept as it was, at the top.
    settled = SETTLED_TAGS.get(record.get("status"))
    # The turn's output first (the owner, 2026-11-22: without it the questions cannot be understood), then the question.
    # A thin rule between them (as cards/proxy_panel.py RULE), a dict of its own on each card.
    said = [*reply_shown(reply_body(output), OUTPUT_ID, label=OUTPUT_LABEL), {"tag": "hr"}] if output else []
    words = [part for part in (words_fold(meta),) if part] if meta else []  # What he said, one line that opens.
    if settled and meta and not legacy:  # It no longer asks: "关于你 20:33 的问题", not "要你定".
        # A project's topic doing its lead's work says that still, "办负责人派的事" (cards/answer_card.py SAYS), not
        # "关于你的问题" of words it never quoted; a copy sent before keeps its "member" too.
        doing = "member_settled" if meta.get("doing") == "member" else "about"
        meta = {**meta, "doing": doing, "says": says(meta.get("verb") or "你问", doing, meta.get("at") or "").strip()}

    def outer(built):
        elements = [*words, *said, *built["body"]["elements"], *([files] if files else [])]
        if legacy:
            built["body"]["elements"] = [*legacy, *elements]
        else:
            built["body"]["elements"] = [speaker_row(meta, outcome="waiting", label=topic or "话题", tag=settled),
                                         *elements]
            built["body"].update(padding=BODY_PADDING, vertical_spacing=BODY_SPACING)
        return banded(built)  # The topic's badge has its colour in the card's config.
    # The samples the card in the topic draws (a question at the top level itself has no card there).
    elsewhere = () if record.get("at_top") or record.get("status") != "pending" else _drawn_in_topic(record, topic)
    # Options as buttons (``top_level_card``); the row this card opens with says whose it is, but not over a legacy block.
    return question_fit({**record, TOP_COPY: True}, topic, rowed=not legacy, outer=outer, elsewhere=elsewhere)


def _drawn_in_topic(record, topic):
    """The samples (by ``sample_blocks``' order) the question's card in its topic draws, as near as the copy can tell
    (its title is not known here: the row's words take no node)."""
    if not ui_samples.any_of(record.get("questions")):
        return ()
    carried = _options_with_samples(record)
    drawable = {index for index, option in enumerate(carried) if ui_samples.kept(option).get("elements")}
    return tuple(sorted(drawable - set(question_fit(record, topic).left_out)))


OUTPUT_NEXT = "这一轮的输出，问题在下一条"


def output_pages(output, topic="", ask=None, *, link=None, finish=None):
    """What the turn wrote before it asked, when it is too long to go on the question's copy at the top level
    (app/question_display.py mirror): on cards of its own before the copy (cards/pages.py Paged), each opening with the
    copy's row (cards/ui.py speaker_row: the topic's T number as its badge, its title, 等你, the way into the topic;
    "1/2" in it), the owner's words that open under the first one's (cards/answer_card.py words_fold), the output from
    这一轮输出 on, cut where it fits, and under the last one grey line: the question comes next (OUTPUT_NEXT).
    ``output``: card Markdown; ``link`` (text -> text): its T numbers as links, once, before it is cut; ``finish`` (card
    -> card): the owner's words', once (cards/pages.py finished). A block sent before the title bar (no speaker) stands
    atop the first, as on the copy, which then has no row either."""
    from covey.cards.answer_card import words_fold
    from covey.cards.pages import drawer, finished, paged, paged_after, tail_apart
    from covey.cards.relay_card import stage_drawer, stages_of
    from covey.cards.review import reply_body

    meta, _ = split_ask([ask] if ask else [])
    legacy = [ask] if ask and meta is None else []
    words, _ = finished(finish, [*legacy, *([part for part in (words_fold(meta),) if part] if meta else [])], ())
    label = topic or "话题"

    def row(page):
        return speaker_row(meta, outcome="waiting", label=label, page=page)
    body = reply_body(output)
    # Its 这一轮中途的汇报 folded under the last card, as on the copy (cards/relay_card.py stages_apart; the cards go
    # before the copy and are not kept, so only the fold's title closes it), else on a card of its own after them.
    head, tail = ([row("9/9")] if not legacy else [*legacy]), [note(OUTPUT_NEXT)]
    body, stages, fold = stages_of(body, [*head, *tail], closable=False, element_id=OUTPUT_ID)
    body = link(body) if link else body
    lead = f"{label} · 这一轮的输出"
    draw = drawer(lead, row=None if legacy else row, words=words, label=OUTPUT_LABEL, element_id=OUTPUT_ID,
                  tail=[*([fold] if fold else []), *tail])
    if not stages:
        return paged(body, draw, reserve=True)
    if fold is not None:
        return tail_apart(paged(body, draw), draw, stages)
    return paged_after(body, draw, stages, stage_drawer(lead, row=None if legacy else row, tail=tail, closable=False,
                                                        element_id=OUTPUT_ID), reserve=True)


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
