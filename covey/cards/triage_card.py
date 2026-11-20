"""The 不对 form of the 专项卡's 分流记录 (docs/butler.md 专项的问题分流, app/project_clicks.py ``triage_wrong``): a card of its own with one
input, because the 专项卡 is redrawn and holds no field (cards/project_card.py). The owner says in a sentence how the lead's answer
should have been; the submit button's callback is ``bridge_project_triage_fix`` with the question's token."""
from __future__ import annotations

from covey.cards.answer_card import literal_text
from covey.cards.elements import card, md, plain

FIX_KIND = "bridge_project_triage_fix"
FIELD = "fix"
MAX = 300


def fix_form(summary, value):
    """The form for the row ``summary`` says (the topic, the question and what the lead answered); ``value`` is the click's."""
    field = {"tag": "input", "name": FIELD, "input_type": "multiline_text", "required": True, "rows": 3, "auto_resize": True,
             "max_rows": 8, "width": "fill", "max_length": MAX, "placeholder": plain("该怎么答，或这类事以后怎么定")}
    submit = {"tag": "button", "name": "submit", "form_action_type": "submit", "type": "primary", "text": plain("提交纠正"),
              "behaviors": [{"type": "callback", "value": {**value, "kind": FIX_KIND}}]}
    return card([md("**纠正负责人替你答的这一条**"),
                 md("<font color='grey'>" + literal_text(summary) + "</font>", size="notation"),
                 md("<font color='grey'>提交后，这句话会作为补充发给那个话题，也记进专项的决定日志，负责人之后照做。</font>", size="notation"),
                 {"tag": "form", "name": "triage_fix", "elements": [field, submit]}], "纠正分流 · 表单")


def fixed_card(summary):
    return card([md("**已纠正**"), md("<font color='grey'>" + literal_text(summary) + "</font>", size="notation")], "已纠正")
