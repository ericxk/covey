"""The 补充 form, what stands in for it once it can send nothing, and the task card's 最新补充 line
(app/steering.py decides which and handles the form)."""
from __future__ import annotations

from covey.cards.elements import card, markdown_panel, md, plain


def steer_form(job_id, label, raw, *, text="", notice=""):
    """The 补充 form of task ``job_id`` (``label`` names it), filled in with ``text``; its submit
    button calls back with ``raw``."""
    # 1,000 characters is the most a Feishu input takes; a longer one, or one with files, is sent as a message
    # in the topic and then 补充当前任务 on its queued card.
    field = {"tag": "input", "name": "supplement", "input_type": "multiline_text", "required": True,
             "rows": 6, "auto_resize": True, "max_rows": 15, "width": "fill", "max_length": 1000,
             "placeholder": plain(f"补充任务 #{job_id} 的要求")}
    if text:
        field["default_value"] = text
    head = f"**补充任务 {label}**" + (f"\n{notice}" if notice else "")
    return card([
        md(head),
        md("<font color='grey'>提交后送入正在执行的任务；任务结束后这张卡失效。最多 1000 字；更长的内容或附件，"
           "直接在本话题发消息，再点那条消息卡上的“补充当前任务”。</font>", size="notation"),
        {"tag": "form", "name": "steer", "elements": [field, {
            "tag": "button", "name": "submit", "form_action_type": "submit", "type": "primary",
            "text": plain(f"提交补充到 #{job_id}"),
            # Open only while the task runs (closed when it ends): the topic list's preview says so.
            "behaviors": [{"type": "callback", "value": raw}]}]}], f"⏳ {label} · 执行中 · 补充表单")


def closed_card(text, summary, *, unsent=None):
    """A 补充 form closed, or a queued card whose message became a supplement: ``text``, and the text that
    was not sent when ``unsent`` is given."""
    elements = [md(text)]
    if unsent is not None:
        elements.append(markdown_panel("steer_receipt", "查看未发出的内容", unsent or "（空）"))
    return card(elements, summary)


def latest_request(line):
    """The task card's 最新补充 line."""
    return md(line, element_id="latest_request", size="notation")
