"""The buttons that rerun a stopped task, and the card of one whose task card cannot take them any more
(app/task_controls.py, app/auto_retry.py and app/cards.py decide which; app/outbox.py names the label);
``recovery_view`` draws that card from what app/recovery_cards.py read."""
from __future__ import annotations

import datetime as _dt
import json

from covey.cards.elements import card, column_button, grey, markdown_panel, md, plain
from covey.cards.topic_actions import SWITCH_OPEN, switch_panel
from covey.cards.ui import band_label, conclusion_band, styled
from covey.core import look
from covey.core.text import literal
from covey.cards.trace_view import TraceView
from covey.core.trace import ToolTrace, finish_record_trace
from covey.cards.trace_updates import panel_json

RETRY_KIND = "bridge_job_retry"
# The next steps of a task that did not succeed that start a turn of their own (core/failure.py actions):
# 看原因, a read-only turn that looks into why, and 拆小再试 (app/task_controls.py follow_up).
DIAGNOSE_KIND, SPLIT_KIND = "bridge_job_diagnose", "bridge_job_split"
FOLLOW_UPS = {"diagnose": ("看原因", DIAGNOSE_KIND), "split": ("拆小再试", SPLIT_KIND)}
FOLLOW_UP_NOTES = {"diagnose": "看原因：开一轮只读排查，看看这一轮为什么没成功，不改文件、不重跑。",
                   "split": "拆小再试：把这件事拆成更小的几步重新做，被拦下的那块先跳过。",
                   "switch_model": "换个模型：打开换配置，换一个模型接着做这件事。",
                   "other_harness": "换个 Harness：打开换配置，预选一个现在能用的别的 Harness，你确认后才换，接着做这件事。"}
# 换个模型 (core/failure.py SWITCH_MODEL): the 换配置 card's own click (bridge_switch_open), so a failure whose retry cannot
# succeed (额度用完, 账号被拦, 安全拒绝) offers the one next step that can right in its row, not only under 执行过程.
SWITCH_MODEL = "switch_model"
SWITCH_LABEL = "换个模型"
# 换个 Harness on a limit's failure (app/auto_retry.py other_harness, #36): the same 换配置 click with ``other``, which
# preselects another Harness usable now (app/harness_switch.py); never switched without the owner's 确认更换.
OTHER_HARNESS = "other_harness"
OTHER_LABEL = "换个 Harness"
DETAILS_ID = "failure_details"


def interrupted_summary(name, label="", due_at=None):
    """An interrupted task's chat-list preview (``name``: its topic, else #job): when it continues by itself
    (``due_at``, an automatic retry's time), else whether ``label`` 检查并继续 applies."""
    if due_at is not None:
        return f"{name} 停住了 · {_dt.datetime.fromtimestamp(due_at).astimezone().strftime('%H:%M')} 自动检查并继续"
    return f"{name} 停住了" + (" · 可以检查并继续" if label == "检查并继续" else "")


def stopped_card(text, *, retry=None, actions=(), panels=(), header=None, summary=None, band=None):
    """An interrupted, failed or moved task's card: ``band`` (cards/ui.py conclusion_band: what became of it and
    why), then ``text``; ``retry``, a (label, topic_id, job_id), as its button, or ``actions`` in its place (a
    pending automatic retry's line and row); then ``panels``. ``header``: its (template, title) title bar, of a
    card without a band; ``summary``: its chat-list preview, which a topic's row shows while it is the topic's latest
    message."""
    elements = ([band] if band else []) + ([md(text[:10000])] if text.strip() else [])
    if retry:
        label, topic_id, job_id = retry
        elements.append({"tag": "column_set", "element_id": "recovery_actions", "columns": [
            {"tag": "column", "width": "weighted", "elements": [
                {"tag": "button", "text": plain(label), "type": "primary",
                 "value": {"kind": RETRY_KIND, "topic_id": topic_id, "job_id": job_id}}]}]})
    elements.extend(actions)
    built = card(elements + list(panels), summary, header=header)
    return styled(built) if band else built


def switch_route(record):
    """(harness_id, binding_revision) of ``record``'s topic, which the 换配置 click needs; None without one."""
    if record.get("harness_id") and record.get("topic_id") and record.get("binding_revision"):
        return record["harness_id"], record["binding_revision"]
    return None


def offers_switch(elements) -> bool:
    """Whether ``elements`` hold a 换个模型 button already."""
    return f'"kind": "{SWITCH_OPEN}"' in json.dumps(list(elements), ensure_ascii=False)


def result_actions(topic_id, job_id, label, *, stop_kind="", follow_ups=(), route=None):
    """The row after a failed task's answer: ``label`` (检查并继续, 现在重试, 重新投递结果; "" for none), with
    ``stop_kind`` 停止自动重试, then ``follow_ups`` (FOLLOW_UPS: 看原因, 拆小再试; SWITCH_MODEL: 换个模型 and
    OTHER_HARNESS: 换个 Harness, both with the topic's ``route``, its (harness_id, binding_revision), and left out
    without one)."""
    value = {"topic_id": topic_id, "job_id": job_id}
    columns = [column_button(label, {"kind": RETRY_KIND, **value})] if label else []
    if stop_kind:
        columns.append(column_button("停止自动重试", {"kind": stop_kind, **value}))
    for name in follow_ups:
        if name in FOLLOW_UPS:
            columns.append(column_button(FOLLOW_UPS[name][0], {"kind": FOLLOW_UPS[name][1], **value}))
        elif name in (SWITCH_MODEL, OTHER_HARNESS) and route:
            columns.append(column_button(SWITCH_LABEL if name == SWITCH_MODEL else OTHER_LABEL, {
                "kind": SWITCH_OPEN, **value, "harness_id": route[0], "binding_revision": route[1],
                **({"other": True} if name == OTHER_HARNESS else {})}))
    return {"tag": "column_set", "element_id": "result_actions", "columns": columns}


def failure_details(text):
    """技术详情 under a failed task card's one-line reason: the words it said before (core/failure.py card_body)."""
    return markdown_panel(DETAILS_ID, grey("技术详情"), text[:6000], boxed=True, size="notation")


def retry_status(content):
    """A pending automatic retry's line above the row."""
    return md(content, element_id="auto_retry_status")


def result_note(content):
    """The small print under the row."""
    return md(content, element_id="result_actions_note", size="notation")


def recovery_view(record, text, *, word, headline, line="", topic, label, continued, moved_to, how, retry, actions,
                  snapshot):
    """The recovery card of ``record``'s task from what app/recovery_cards.py read and worded: its conclusion band
    (docs/ui.md 5.2 停住的卡: ``word``, the state's key in core/look.py STATES, with its topic's label ``topic`` or "" and its number; ``headline``,
    what became of it or why; ``line``, what can be done next), then ``text``; ``how``, how an automatic retry moved
    it to ``moved_to``; ``retry`` and ``actions`` as ``stopped_card`` takes them; ``snapshot``, its trace for the
    执行过程 panel; then the 换配置 panel of a task with a binding. No title bar: the band is the card's centre (it
    replaced the coloured bar)."""
    if moved_to is not None:
        headline, line = f"已转到任务 #{moved_to} 继续{how}", ""
    band = conclusion_band(band_label(word, topic, f"#{record['job_id']}"), literal(headline), literal(line))
    panels = []
    if snapshot:
        trace = finish_record_trace(ToolTrace.restore(snapshot), record)
        panels.append(panel_json(TraceView(trace.snapshot()).panel_model(False, timed=True)))
    if switch_route(record) and not offers_switch(actions):  # Else 换个模型 in the row opens the same card.
        panels.append(switch_panel(record["topic_id"], record["job_id"], record["harness_id"],
                                   record.get("binding_revision", 1)))
    name = topic or f"#{record['job_id']}"
    if moved_to is not None:
        summary = f"{name} · 已转到 #{moved_to} 继续"
    elif continued or word != "stopped" or record.get("status") != "interrupted":
        summary = f"{name} {look.word(word)} · {headline}"
    else:
        pending = actions and record.get("auto_retry_state") == "pending"  # Else they are 看原因 and 拆小再试 alone.
        summary = interrupted_summary(name, label, record["auto_retry_at"] if pending else None)
    return stopped_card(text.lstrip("\n"), retry=retry, actions=actions, panels=panels, summary=summary, band=band)
