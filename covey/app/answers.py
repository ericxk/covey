"""Answers to questions, without saving or sending anything.

A submitted form's fields become the saved answers (or feedback that releases the questions);
a saved answer becomes the text its cards show, the message the backend gets, and the answers a
native question returns. Questions (app/questions.py) saves, sends and queues them.

The question a card adds of its own (app/question_dedup.py: the record's ``extra_questions``, drawn after its questions) is
never one of the answers: its pick is saved apart (``extra_answers``, ``submitted_answer``), so nothing of it reaches the
model, the answer's title or the decision noted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from covey.app.held_notes import note_parts
from covey.app.question_dedup import EXTRA_ANSWERS, extra_answers
from covey.cards.question_cards import GENERIC_HEADER, SKIP_LABEL, SKIP_VALUE, SUBMIT_LABEL, SUPPLEMENT, single_choice
from covey.cards.review import compact_text


class AnswerIncomplete(ValueError):
    """The submitted form cannot be an answer yet; nothing was saved or sent."""


@dataclass(frozen=True)
class AnswerBy:
    """Who answered a question, and how (WAVE12 C2). app/questions.py submit_answer takes one or, as before, the open_id
    of who clicked (a string; a task runner of an older release sends that), and reads its ``open_id`` (``open_id_of``)."""

    party: str  # core/turn_source.py PARTY_OWNER / PARTY_BUTLER / PARTY_LEAD
    channel: str  # "card" | "message" | "web" | "tool"
    open_id: str = ""  # Who clicked (a card's).
    message_id: str = ""  # The owner's message: his own (message), the one the butler answers by (tool), the page's op (web).


def open_id_of(by):
    """The open_id ``by`` names: an AnswerBy's (None when it has none); anything else (an open_id, None) as it is."""
    if isinstance(by, AnswerBy):
        return by.open_id or None
    return by


SKIPPED = "用户选择不回答此题（不表示同意或授权）"
# 补充说明's heading when no question was answered (only a 补充说明 submitted, or each one skipped): the note may say the
# questions do not fit, as 问题不合适 / 修改要求 did in older releases.
UNANSWERED_NOTE = "补充说明（没有回答上面的问题，不表示同意或授权；可能是对问题的反馈、疑问或新的要求，请据此重新评估：是疑问就先解释，还要他定的再问）："
ATTACHMENTS = "_attachments"  # What a topic reply adds to its wording (paths, forwards, quotes): for the model, never shown.
TITLE_CHARS = 30  # After the card's "T12 · #34 · ".
LABEL_CHARS = 12  # A question's short label in the title, when it has no header of its own.
SUPPLEMENT_JOIN = "；补充："  # Before the 补充说明 in a title after some answers,
SUPPLEMENT_MIN = 4  # when this many of its characters still fit.


def question_feedback_fields(questions, text):
    """Explicit feedback can release a native question without inventing its answers.

    Keep the ordinary fields as well so an already-running older task runner
    receives the same refusal/feedback through its existing answer transport.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请先说明哪些问题不合适，或你想修改什么")
    text = text.strip()
    notice = "用户选择反馈问题、重新讨论，未回答本题；未回答部分不表示同意或授权。"
    fields = {f"answer_{i}": notice for i in range(len(questions))}
    fields[f"answer_{len(questions) - 1}"] += "\n用户反馈：\n" + text
    fields["_question_feedback"] = text
    return fields


NOTE_EMPTY = f"补充说明是空的，这次没有提交：先在最下面的“补充说明”里写上你想说的，再点“{SUBMIT_LABEL}”"


def skip_all_fields(questions, fields):
    """Every question 不回答此题, whatever was picked or written, and the 补充说明, which this requires (AnswerIncomplete when
    it is empty): 只发补充说明 and 先问一句 on a card sent before T631, and the web page's ``skip``. The fields a runner of any
    release reads the same way (forward_answer)."""
    supplement = str(fields.get(SUPPLEMENT) or "").strip()
    if not supplement:
        raise AnswerIncomplete(NOTE_EMPTY)
    skipped = {f"skip_{i}": True for i in range(len(questions))}
    return {**skipped, SUPPLEMENT: supplement,
            **({ATTACHMENTS: fields[ATTACHMENTS]} if fields.get(ATTACHMENTS) else {})}


def given(fields, i):
    """Whether the form's ``fields`` answer question ``i`` at all: written, picked (不回答此题 too) or its skip box ticked."""
    selected = fields.get(f"option_{i}")
    picked = any(str(value).strip() for value in selected) if isinstance(selected, list) else selected not in (None, "")
    return bool(str(fields.get(f"answer_{i}") or "").strip()) or fields.get(f"skip_{i}") in (True, "true") or picked


def with_note(questions, fields):
    """提交回答's fields (T631: the one button, 只发补充说明 and 先问一句 folded into it): with 补充说明 written, each question
    left unanswered is 不回答此题, so a 补充说明 alone skips them all (answer_message heads it UNANSWERED_NOTE); without one,
    form_answers asks for every question as before."""
    if not str(fields.get(SUPPLEMENT) or "").strip():
        return fields
    return {**fields, **{f"skip_{i}": True for i in range(len(questions)) if not given(fields, i)}}


def picked_fields(questions, fields, pick):
    """The form an option's button sends (cards/question_cards.py PICK): the question answered with option
    ``pick`` (SKIP_VALUE: 不回答此题), whatever the dropdown or answer box held; 补充说明 and attachments kept.
    AnswerIncomplete, so nothing is submitted, when ``pick`` names no option of a single_choice question (a card
    drawn for other questions, or a value no card sends)."""
    options = questions[0]["options"] if single_choice(questions) else []
    valid = pick == SKIP_VALUE or (type(pick) is int and 0 <= pick < len(options))
    if not options or not valid:
        raise AnswerIncomplete("这个选项对不上这道题，请在最新的问题卡上重新选")
    kept = {key: fields[key] for key in (SUPPLEMENT, ATTACHMENTS) if fields.get(key)}
    return {**kept, "option_0": str(pick)}


def form_answers(questions, fields):
    """Each question's answers from the form, by question id: the text written (it wins over whatever
    was picked), else the options chosen, or 不回答此题. AnswerIncomplete names what is missing or
    contradictory."""
    answers, missing = {}, []
    for i, q in enumerate(questions):
        text = str(fields.get(f"answer_{i}") or "").strip()
        selected = fields.get(f"option_{i}")
        skipped = fields.get(f"skip_{i}") in (True, "true")
        parts = []
        # A written answer wins: a dropdown cannot be cleared once something is
        # picked, and the box is for when no option fits, so what was picked (不回答此题 too) is dropped.
        if text:
            parts = [text]
        elif selected is not None and selected != "":
            options = q.get("options") or []
            values = selected if isinstance(selected, list) else [selected]
            if SKIP_VALUE in values:
                if len(values) > 1:
                    raise AnswerIncomplete(f'“{q["question"][:40]}”选了“{SKIP_LABEL}”，就不能再选其他选项')
                skipped, values = True, []
            if not q.get('multiple') and len(values) > 1:
                raise AnswerIncomplete(f'“{q["question"][:40]}”只能选择一个选项')
            for value in values:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    raise AnswerIncomplete('选项无效，请重新选择') from None
                if not 0 <= index < len(options):
                    raise AnswerIncomplete('选项无效，请重新选择')
                if options[index]['label'] not in parts:
                    parts.append(options[index]['label'])
        if skipped and not text:
            parts = [SKIPPED]
        if not parts:
            missing.append(q["question"][:40])
        answers[q["id"]] = {"answers": parts}
    if missing:
        # Only the form gets here (answer_fields asks for every question itself): a 补充说明 skips what is left (with_note).
        raise AnswerIncomplete(f"还有 {len(missing)} 题没回答：" + "；".join(missing)
                               + f"（不想回答可选“{SKIP_LABEL}”，或在补充说明里写一句再点“{SUBMIT_LABEL}”）")
    return answers


def submitted_answer(record, fields):
    """What a submitted form saves on the question: (answers, response fields). Feedback stands
    for every answer; an answer resubmitted after reconciliation keeps what was saved."""
    feedback = fields.get("_question_feedback")
    attachments = str(fields.get(ATTACHMENTS) or "").strip()
    if feedback is not None:
        fields = question_feedback_fields(record["questions"], feedback)
    answers = form_answers(record["questions"], fields)
    supplement = str(fields.get(SUPPLEMENT) or "").strip()
    if record.get("reconcile_saved"):
        answers, supplement = record["answers"], record.get("supplement", "")
        attachments = record.get("attachments", "")
    response = {"response_kind": "feedback", "feedback": feedback.strip()} if feedback is not None else {}
    if supplement and feedback is None:
        response["supplement"] = supplement
    extra = extra_answers(record, fields) if feedback is None and not record.get("reconcile_saved") else {}
    if extra:
        response[EXTRA_ANSWERS] = extra  # The card's own question (app/question_dedup.py), apart from the answers.
    if attachments:
        response["attachments"] = attachments
    return answers, response


def question_label(question):
    """A question's short label: its header unless that names no topic (``GENERIC_HEADER``), else
    the start of the question."""
    header = compact_text(question.get("header") or "", LABEL_CHARS)
    if header and not GENERIC_HEADER.fullmatch(header):
        return header
    return compact_text(question.get("question") or header, LABEL_CHARS)


def answer_title(record, answers, feedback=""):
    """The follow-up task card's title, at most TITLE_CHARS: what was asked and what was answered,
    "label → answer" per question (an answer alone names no question).
    With every question unanswered, the 补充说明 is what was said: ``补充：<its start>`` (a title of
    "<key> → 不回答" over and over said nothing); with some answered, it follows them when there is room.
    The whole answer is in 本轮请求 and in the prompt."""
    if feedback:
        return compact_text("问题反馈：" + feedback, TITLE_CHARS)
    supplement = compact_text(record.get("supplement") or "", TITLE_CHARS)
    parts, labelled, answered = [], False, False
    for q in record["questions"]:
        given = "；".join(a for a in answers[q["id"]]["answers"] if a != SKIPPED)
        answered = answered or bool(given)
        label = question_label(q)
        labelled = labelled or bool(label)
        parts.append(f"{label} → {given or '不回答'}" if label else given or "不回答")
    if supplement and not answered:
        return compact_text("补充：" + supplement, TITLE_CHARS)
    title = compact_text(("" if labelled else "回答：") + "；".join(parts), TITLE_CHARS)
    if supplement and len(title) + len(SUPPLEMENT_JOIN) + SUPPLEMENT_MIN <= TITLE_CHARS and not title.endswith("…"):
        title = compact_text(title + SUPPLEMENT_JOIN + supplement, TITLE_CHARS)
    return title


def shown_answer_title(shown):
    """``answer_title`` read back from the words an answer's turn shows (``answer_message``'s ``shown``, its
    ``user_text``), for a turn saved without its ``title``: "question → answer" per question (each question and its
    answers one line there; the question's start, as ``question_label`` without a header), or ``补充：<its start>`` when every question was skipped; '' when ``shown`` is not an
    answer's."""
    sections = str(shown or "").split("\n\n")
    lines = [line.strip() for line in sections[0].split("\n")]
    if not lines or lines[0] != "用户补充回答：":
        return ""
    lines = [line for line in lines[1:] if line]
    pairs = [(lines[i], lines[i + 1] if i + 1 < len(lines) else "") for i in range(0, len(lines), 2)]
    parts = [(compact_text(question, LABEL_CHARS), "；".join(a for a in answer.split("；") if a and a != SKIPPED))
             for question, answer in pairs]
    supplement = next((section.split("\n", 1)[1] for section in sections[1:]
                       if section.startswith("用户补充说明") and "\n" in section), "")
    if supplement and not any(given for _, given in parts):
        return compact_text("补充：" + supplement, TITLE_CHARS)
    return compact_text("；".join(f"{question} → {given or '不回答'}" for question, given in parts), TITLE_CHARS)


FEEDBACK_START = "用户对当前提问的反馈（未回答原问题，不表示同意或授权，请重新评估问题和需求）：\n"  # ``answer_message``'s for feedback.
ANSWER_START = "用户补充回答："  # ``answer_message``'s ``shown`` for answered questions begins so;
# its 补充说明 (UNANSWERED_NOTE's heading too) and 另外补充 sections each follow a blank line so.
NOTE_SECTION = re.compile(r"\n\n用户(补充说明|另外补充)[^\n]*?：\n")


def answer_parts(shown, questions=()):
    """The whole of an answer's turn as ``answer_message`` wrote its ``shown`` (a job's ``user_text``), for its "你答"
    fold (cards/answer_card.py answer_block): [(kind, text)], kind "question" and "answer" for each question and its
    answer, then "heading" (补充说明 or 另外补充) and "note" for each section after them; [] when ``shown`` is not an
    answer's (feedback, an ordinary turn). ``questions``, the question record's questions' words in order, split
    answers of several lines exactly; without them (or when they do not match), each question and answer is one line,
    as ``shown_answer_title`` reads them, or, with an odd number of lines, all of them are one "answer"."""
    text = str(shown or "").replace("\r\n", "\n").lstrip()
    if not text.startswith(ANSWER_START):
        return []
    pieces = NOTE_SECTION.split(text[len(ANSWER_START):].removeprefix("\n"))
    parts = []
    for question, answer in _answer_pairs(pieces[0], [str(q) for q in questions or () if str(q or "").strip()]):
        parts += [*([("question", question.strip())] if question.strip() else []), ("answer", answer.strip())]
    for name, said in zip(pieces[1::2], pieces[2::2]):
        if said.strip():
            parts += [("heading", name), ("note", said.strip())]
    return parts


def _answer_pairs(block, questions):
    """(question, answer) pairs of ``answer_parts``' first section ``block``."""
    rest, pairs = block, []
    for index, question in enumerate(questions):
        if not rest.startswith(question + "\n") and rest != question:
            break
        rest = rest[len(question) + 1:]
        if index + 1 == len(questions):
            return [*pairs, (question, rest)]
        cut = rest.find("\n" + questions[index + 1] + "\n")
        cut = cut if cut >= 0 else (len(rest) - len(questions[index + 1]) - 1
                                    if rest.endswith("\n" + questions[index + 1]) else -1)
        if cut < 0:
            break
        pairs.append((question, rest[:cut]))
        rest = rest[cut + 1:]
    lines = [line for line in block.split("\n") if line.strip()]
    if len(lines) % 2:  # Not a line each: an answer of several lines, told apart from its question by nothing.
        return [("", "\n".join(lines))]
    return [(lines[i], lines[i + 1]) for i in range(0, len(lines), 2)]


def answer_message(record, answers, feedback, notes):
    """(shown, text, delivered) for a saved answer and the held notes that go with it: what the
    user wrote, for cards; that plus the attachments' local paths, for the model; and the
    answers a native question returns."""
    attachments = record.get("attachments", "")
    riders = [attachments] if attachments else []
    if feedback:
        shown = FEEDBACK_START + feedback
    else:
        shown = "用户补充回答：\n" + "\n".join(
            q["question"] + "\n" + "；".join(answers[q["id"]]["answers"]) for q in record["questions"])
    text = shown + ("\n" + attachments if attachments else "")
    if record.get("supplement") and not feedback:
        # Every question skipped (only a 补充说明 submitted): the note may say the questions do not fit or ask about them,
        # as 问题不合适 / 修改要求 and 先问一句 did in older releases, so the model is told to reconsider them with it.
        none = all(answers[q["id"]]["answers"] == [SKIPPED] for q in record["questions"])
        head = UNANSWERED_NOTE if none else "补充说明："
        shown += f"\n\n用户{head}\n" + record["supplement"]
        text += f"\n\n用户{head}\n" + record["supplement"]
        riders.append(head + record["supplement"])
    delivered = answers
    extra_notes = [note for note in notes if not feedback or note_parts(note)[0].strip() != feedback]
    if extra_notes:
        parts = [note_parts(note) for note in extra_notes]
        extra = "\n\n".join(said + ("\n" + paths if paths else "") for said, paths in parts)
        shown += "\n\n用户另外补充：\n" + "\n\n".join(said for said, _ in parts)
        text += "\n\n用户另外补充：\n" + extra
        riders.append("另外补充：" + extra)
    if riders:
        # A native answer reaches the model as the question's result: notes ride on the last answer,
        # so the model answers once with everything instead of again for a separate supplement.
        last = record["questions"][-1]["id"]
        delivered = {**answers, last: {"answers": [*answers[last]["answers"], *riders]}}
    return shown, text, delivered


# 先问一句 on a 代发草稿 (T604 #13; app/owner_send.py ask): the owner's question about it, which the topic answers as its next turn
# or in the task it runs. It decides nothing, and the card stays open.
ASK_PREFIX = "ask-"  # Its turn's message id: app/task_controls.py leaves the task waiting for an answer as it is.


def ask_id(key, event_id):
    """The message id of the turn a 先问一句 click asks with: the same click (an event Feishu sent again) the same id."""
    return ASK_PREFIX + hashlib.sha256(f"{key}-{event_id}".encode()).hexdigest()[:16]


def ask_title(words):
    """The task card's title of a 先问一句's turn."""
    return compact_text("先问一句：" + str(words or ""), TITLE_CHARS)


# 不处理 (T604 #15; app/questions.py set_aside, take_set_aside): what the topic's next turn starts with for questions the
# owner set aside, then each as an answer of every question 不回答此题 reads.
SET_ASIDE_START = "你之前问的题，用户点了“不处理”：都不回答，不表示同意或授权；没有它做不了的先放着，不用再问同样的题。"


def set_aside_answers(questions):
    """不处理's answers: every question 不回答此题 with nothing added, as ``skip_all_fields`` skips them without the 补充说明
    it requires."""
    return form_answers(questions, {f"skip_{i}": True for i in range(len(questions))})


def set_aside_message(records, notes=()):
    """What the turn that takes ``records`` (questions set aside) starts with: SET_ASIDE_START, then each one's answer as
    ``answer_message`` writes it (every question 不回答此题), the held ``notes`` with the last."""
    parts = []
    for index, record in enumerate(records):
        given = record.get("answers") or set_aside_answers(record["questions"])
        _, text, _ = answer_message({**record, "supplement": ""}, given, "", list(notes) if index == len(records) - 1 else [])
        parts.append(text)
    return SET_ASIDE_START + "\n" + "\n\n".join(parts)


def answer_fields(record, answers, note, owner=False):
    """The form fields 提交回答 would send for ``answers`` (question id or text -> option label, or labels for a
    multiple choice), with ``note`` as the 补充说明: ``form_answers`` the other way, for the butler's
    question_answer (app/butler_tools.py, its PROXY_NOTE). ``owner``: the owner's own answer (the web page,
    app/web_ops.py): a question without options takes the text given, a text that is no option of its question
    is a written answer, as the card's answer box is (a written answer wins in form_answers), ``{"skip": true}``
    is 不回答此题 (the card's ``skip_i``), and with ``note`` a question left out is 不回答此题, as 提交回答 has it
    (``with_note``; ``answers`` may then be empty). ValueError says what does not fit."""
    noted = owner and bool(str(note or "").strip())
    if answers is None and noted:
        answers = {}
    if not isinstance(answers, dict) or not (answers or noted):
        raise ValueError("answers 要是“题目 id → 选项原文”的对象" + ("；不答题时写上补充说明" if owner else ""))
    questions = [q for q in record.get("questions") or [] if isinstance(q, dict)]
    free = [str(q.get("question") or "")[:40] for q in questions if not q.get("options")]
    if free and not owner:
        raise ValueError("这组问题里有自由回答的题（" + "；".join(free) + "），不能代答：改用 topic_send 把主人的话交给话题。")
    by_name = {}
    for i, q in enumerate(questions):
        for name in (q.get("id"), q.get("question"), q.get("header")):
            if isinstance(name, str) and name.strip():
                by_name.setdefault(name.strip(), i)
    given = {}
    for key, value in answers.items():
        index = by_name.get(str(key).strip())
        if index is None:
            raise ValueError(f"没有题目“{str(key)[:40]}”：用 topic_read 里方括号中的题目 id")
        if index in given:
            raise ValueError(f"“{str(questions[index].get('question'))[:40]}”答了两次")
        given[index] = value
    missing = [str(q.get("question") or "")[:40] for i, q in enumerate(questions) if i not in given]
    if missing and not noted:
        raise ValueError("每题都要答，还差：" + "；".join(missing) + ("（不答的题写上补充说明就算不回答）" if owner else ""))
    fields = {}
    for i, q in enumerate(questions):
        if i not in given:  # Left out with the owner's 补充说明: 不回答此题.
            fields[f"skip_{i}"] = True
            continue
        value = given[i]
        labels = [str(o.get("label") or "") for o in q.get("options") or [] if isinstance(o, dict)]
        if owner and isinstance(value, dict) and value.get("skip") is True:
            fields[f"skip_{i}"] = True  # 不回答此题, with or without options.
            continue
        if owner and isinstance(value, str) and value.strip() and value.strip() not in labels:
            fields[f"answer_{i}"] = value.strip()  # Written, as in the card's answer box.
            continue
        values = value if isinstance(value, list) else [value]
        if not values or not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"“{str(q.get('question'))[:40]}”的答案要是选项原文" + ("或原文的列表" if q.get("multiple") else ""))
        if not q.get("multiple") and len(values) > 1:
            raise ValueError(f"“{str(q.get('question'))[:40]}”是单选，只能选一个")
        labels = [str(o.get("label") or "") for o in q["options"] if isinstance(o, dict)]
        chosen = []
        for item in values:
            if item.strip() not in labels:
                raise ValueError(f"“{item[:40]}”不是“{str(q.get('question'))[:40]}”的选项；选项是："
                                 + "；".join(labels) + "。自由文字的回答改用 topic_send。")
            index = str(labels.index(item.strip()))
            if index not in chosen:
                chosen.append(index)
        fields[f"option_{i}"] = chosen if q.get("multiple") else chosen[0]
    fields[SUPPLEMENT] = note
    return fields
