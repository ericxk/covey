"""A held note's card: a message sent while the topic's questions wait, saved to go with the answer
(app/held_notes.py keeps the record and handles its buttons)."""
from __future__ import annotations

from covey.cards.elements import button_look, callback_button, card, markdown_panel, md, plain, row
from covey.cards.trace_view import clock

NOTE_KIND = "bridge_note"
LABEL = {
    "held": "📝 **这条消息还没发给模型**：{waiting}还没答，提交回答时会一起带上",
    "next_round": "↪ **留作下一轮**：{waiting}回答后单独排队执行",
    "withdrawn": "**已撤回**，这条补充不会发送",
}
ANSWER_REQUIRED = "要模型现在就看到它，点“作为问题反馈发送”（问题交回模型重新考虑）；这条消息不算问题的回答。"
FEEDBACK_LABEL = "作为问题反馈发送"


def waiting_text(record):
    """The questions a held note waits for, as its card says them: ``#1106（20:24）的 2 道题``. T182's
    note card did not say it, and the owner sent the same message again 18 minutes later. A note
    saved before this was kept says only 话题里的问题."""
    groups = [g for g in record.get("waiting") or [] if isinstance(g, dict) and g.get("job_id")]
    if not groups:
        return "话题里的问题"
    def one(group):
        when = clock(group["created_at"]) if isinstance(group.get("created_at"), (int, float)) else ""
        count = group.get("count") or 0
        return f"#{group['job_id']}" + (f"（{when}）" if when else "") + ("的提问" if count <= 1 else f"的 {count} 道题")
    return "、".join(map(one, groups))


def note_parts(note):
    """What the user sent, and apart from it its attachments' local paths, which only the model gets."""
    text = str(note.get("text") or "")
    shown = note.get("display")
    if shown and text.startswith(shown):  # Saved while `text` still carried the paths.
        return shown, text[len(shown):].strip()
    return text, str(note.get("attachments") or "")


def note_card(record):
    status = record.get("status", "held")
    if status == "attached":
        kind = "问题反馈" if record.get("response_kind") == "feedback" else "回答"
        # Without a job the answer that took it is still being sent, or its delivery is unconfirmed.
        head = (f"✓ **已随{kind}一起提交**（任务 #{record.get('job_id')}）" if record.get("job_id")
                else f"⏳ **已随{kind}一起发送**，送达状态见问题卡片；不会重复发送")
    elif status == "queued":
        head = (f"✓ **已排队为任务 #{record.get('job_id')}**，在回答之后执行" if record.get("job_id")
                else "⏳ **已提交单独排队**，送达未确认；不会重复发送")
    elif status == "steered":
        head = (f"⏳ **已作为补充送入 #{record.get('job_id')}**，送达未确认" if record.get("unconfirmed")
                else f"✓ **已补充到 #{record.get('job_id')}**，紧跟在回答之后")
    else:
        head = LABEL.get(status, status).replace("{waiting}", waiting_text(record))
    shown = note_parts(record)[0]
    elements = [md(head),
                markdown_panel("note_text", "查看内容", "\n".join(
                    "> " + line for line in str(shown).splitlines()[:40]) or "（空）")]
    if status in {"held", "next_round"}:
        if record.get("folded"):  # It waited for questions folded under newer ones (HeldNotes.wait_instead).
            elements.insert(1, md(f"{waiting_text({'waiting': record['folded']})}已收起，改等 {waiting_text(record)}。"))
        elements.append(md(ANSWER_REQUIRED))
        # Sending it as feedback first: the one that gets it to the model now (P4 of the UI review).
        # The row wraps (flow): in a topic's side panel the labels were cut to 作为问题反馈… and 留作下….
        buttons = [(FEEDBACK_LABEL, "feedback")]
        buttons.append(("改为随回答提交", "attach") if status == "next_round" else ("留作下一轮", "next"))
        buttons.append(("撤回", "withdraw"))
        elements.append(row(*(({"tag": "button", "text": plain(label), **button_look("primary" if verb == "feedback" else ""),
                                "value": {"kind": NOTE_KIND, "topic_id": record["topic_id"],
                                                           "token": record["token"], "verb": verb}}, None)
                              for label, verb in buttons), flex="flow"))
    summary = {"held": "补充已保存，随回答提交", "next_round": "补充留作下一轮"}.get(status, "补充")
    return card(elements, summary, update_multi=True)


def feedback_chooser(topic_id, token, pending):
    """A button a waiting form: which one a held note goes back to the model with as feedback."""
    return card([md("请选择要反馈的一组问题；其他问题保留待答。"), *(
        callback_button(f"反馈第 {i + 1} 组：{q['questions'][0]['question'][:40]}", {
            "kind": NOTE_KIND, "topic_id": topic_id, "token": token, "verb": "feedback", "question_token": q["token"]})
        for i, q in enumerate(pending))])
