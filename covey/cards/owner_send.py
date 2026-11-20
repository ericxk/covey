"""The 代发草稿 card (app/owner_send.py): a message a topic drafted for the owner to send as themselves. It shows whom it
goes to (name, department, e-mail, as the owner's login found them), why, and the text in a box the owner may change;
发送 sends what the box holds, 不发 drops it. Several people found for a name: a row each to choose. Then what came
of it, the button back when it certainly did not go out (重新发送). Every value carries its kind and the draft's id; 发送
also the digest of the text drawn, so a click on an older drawing is told apart."""
from __future__ import annotations

import re

from covey.cards.elements import button_row, callback, md, note, panel_card, plain
from covey.core.owner_send import digest
from covey.core.text import literal

SEND = "bridge_owner_send"
DROP = "bridge_owner_send_drop"
PICK = "bridge_owner_send_pick"
TEXT_FIELD = "text"
PICKS = 5  # Candidates drawn for a name.
EDIT_LIMIT = 1000  # An input box's max_length: CardKit refuses more (ErrCode 11310, T568 acceptance 2026-11-19).
# Feishu's audit refuses a bot's card with an e-mail address in it ("contain sensitive data: EMAIL_ADDRESS", same day); a
# phone number is masked as well. A text with either is drawn masked and is not editable (the box would send the mask).
EMAIL = re.compile(r"([\w.+-]+)@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")

# The card's heading by the draft's state, and whether the form (box and buttons) is there.
STATES = {
    "drafted": ("✉️ 代发草稿：确认后以你的身份发出", True),
    "choose": ("✉️ 代发草稿：先选收件人", False),
    "unresolved": ("✉️ 代发草稿：没找到收件人", False),
    "sending": ("⏳ 正在以你的身份发送…", False),
    "sent": ("✅ 已以你的身份发出", False),
    "uncertain": ("⚠️ 可能已发出，没能确认", False),
    "unsent": ("⚠️ 没发出", True),
    "declined": ("这条不发了", False),
    "superseded": ("这条草稿已被新的草稿替换", False),
}


def masked(text) -> str:
    """``text`` as a card may show it: e-mail addresses as their local part and @…, phone numbers as 138****1234."""
    return PHONE.sub(r"\1****\2", EMAIL.sub(r"\1@…", str(text or "")))


def editable(text) -> bool:
    """Whether the card offers ``text`` in its box: short enough for it and shown as it is (``masked``)."""
    text = str(text or "")
    return len(text) <= EDIT_LIMIT and masked(text) == text


def _value(kind, record, **more):
    return {"kind": kind, "draft": record["id"], **more}


def recipient_line(record) -> str:
    who = record.get("recipient") or {}
    if who.get("name"):
        parts = [literal(who["name"])] + [literal(masked(who[k])) for k in ("department", "email") if who.get(k)]
        return "**发给**：" + " · ".join(parts) + ("（私聊）" if who.get("kind") != "group" else "（群）")
    if who.get("chat_id"):
        return f"**发给**：会话 `{who['chat_id']}`"
    asked = record.get("to") or {}
    shown = asked.get("name") or masked(asked.get("email") or "") or asked.get("open_id") or ""
    return "**发给**：" + (literal(shown) if shown else "（没写）")


def owner_send_card(record, view) -> dict:
    """The whole card of ``record`` drawn as ``view`` ({state, notice}): the record's status unless the view says choose,
    unresolved or sending."""
    state = view.get("state") if view.get("state") in STATES else record.get("status", "drafted")
    heading, form = STATES.get(state, STATES["drafted"])
    text = str(record.get("text") or "")
    elements = [md(f"**{heading}**"), md(recipient_line(record))]
    if record.get("topic_label"):
        elements.append(note(f"来自 {literal(record['topic_label'])}"))
    if record.get("why"):
        elements.append(note("为什么发：" + literal(record["why"])))
    if state == "choose":
        elements.append(md(f"按“{literal((record.get('to') or {}).get('name', ''))}”找到多个人，点要发给的那位："))
        for index, candidate in enumerate((record.get("candidates") or [])[:PICKS]):
            label = masked(" · ".join(str(candidate.get(k)) for k in ("name", "department", "email") if candidate.get(k)))
            elements.append(button_row([{"tag": "button", "type": "default", "size": "medium", "text": plain(label[:60]),
                                         "behaviors": callback(_value(PICK, record, pick=index))}]))
    if state == "unresolved":
        elements.append(md("没能按话题给的收件人找到对方（" + literal(record.get("reason") or "查不到") + "）。"
                           "这条没有发；可以在话题里告诉它对方的邮箱，让它重写草稿。"))
    if form:
        if view.get("hide"):
            body = [note("正文飞书不让卡片显示（被它的内容审核拦下），请到本话题这一轮的回答里核对正文；发出的是话题写的原文。")]
        elif editable(text):
            body = [note("要发的正文（可以直接改，发出的就是框里的内容）："),
                    {"tag": "input", "name": TEXT_FIELD, "width": "fill", "max_length": EDIT_LIMIT,
                     "input_type": "multiline_text", "rows": 6, "auto_resize": True, "max_rows": 20, "default_value": text,
                     "placeholder": plain("正文")}]
        else:
            body = [md("要发的正文：\n" + "\n".join(literal(line) for line in masked(text).split("\n"))),
                    note(f"正文超过 {EDIT_LIMIT} 字或含邮箱、手机号（卡上已遮住），卡上不能改；发出的是话题写的原文。"
                         "要改的话在话题里说，让它重写草稿。")]
        elements.append({"tag": "form", "name": "owner_send", "elements": [
            *body,
            button_row([
                {"tag": "button", "name": "owner_send_go", "form_action_type": "submit", "type": "primary_filled",
                 "size": "medium", "text": plain("重新发送" if state == "unsent" else "发送"),
                 "behaviors": callback(_value(SEND, record, text=digest(text)))},
                {"tag": "button", "name": "owner_send_drop", "type": "default", "size": "medium", "text": plain("不发送"),
                 "behaviors": callback(_value(DROP, record))}]),
        ]})
    else:
        label = "发出的原文：" if state == "sent" else "正文："
        if not view.get("hide"):
            elements.append(md(label + "\n" + "\n".join(literal(line) for line in masked(text).split("\n"))))
    lines = {
        "unsent": "没发出：" + literal(record.get("reason") or "发送失败") + "。改好正文可以点“重新发送”。",
        "sent": f"{record.get('sent_at_text') or ''} 发出" + ("，读回确认" if record.get("message_id") else "") + "。",
        "uncertain": "发送工具没给出结果（" + literal(record.get("reason") or "没有结果") + "），不会自动重发，请到会话里看一眼。",
        "sending": "以你的身份发送中，约 1 分钟。",
    }
    if state in lines:
        elements.append(note(lines[state]))
    if state == "unsent" and "登录" in str(record.get("reason") or ""):
        elements.append(note("发送工具没登录的话，先点机器人菜单「代答」，在代答卡上点“网页端登录”扫码。"))
    if view.get("notice"):
        elements.append(md(view["notice"]))
    return panel_card(elements, heading)
