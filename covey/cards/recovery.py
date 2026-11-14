"""The buttons that rerun a stopped task, and the card of one whose task card cannot take them any more
(app/task_controls.py, app/auto_retry.py and app/cards.py decide which; app/outbox.py names the label);
``recovery_view`` draws that card from what app/recovery_cards.py read."""
from __future__ import annotations

import datetime as _dt

from covey.cards.elements import card, column_button, md, plain
from covey.cards.topic_actions import switch_panel
from covey.cards.trace_view import TraceView
from covey.core.trace import ToolTrace, finish_record_trace
from covey.cards.trace_updates import panel_json

RETRY_KIND = "bridge_job_retry"


def interrupted_summary(name, label="", due_at=None):
    """An interrupted task's chat-list preview (``name``: its topic, else #job): when it continues by itself
    (``due_at``, an automatic retry's time), else whether ``label`` 检查并继续 applies."""
    if due_at is not None:
        return f"⏳ {name} · 已中断 · {_dt.datetime.fromtimestamp(due_at).astimezone().strftime('%H:%M')} 自动检查并继续"
    return f"⚠️ {name} · 已中断" + (" · 待检查并继续" if label == "检查并继续" else "")


def stopped_card(text, *, retry=None, actions=(), panels=(), header=None, summary=None):
    """An interrupted, failed or moved task's card: ``text``; ``retry``, a (label, topic_id, job_id), as its
    button, or ``actions`` in its place (a pending automatic retry's line and row); then ``panels``.
    ``header``: its (template, title) title bar; ``summary``: its chat-list preview, which a topic's row shows
    while it is the topic's latest message."""
    elements = [md(text[:10000])]
    if retry:
        label, topic_id, job_id = retry
        elements.append({"tag": "column_set", "element_id": "recovery_actions", "columns": [
            {"tag": "column", "width": "weighted", "elements": [
                {"tag": "button", "text": plain(label), "type": "primary",
                 "value": {"kind": RETRY_KIND, "topic_id": topic_id, "job_id": job_id}}]}]})
    elements.extend(actions)
    return card(elements + list(panels), summary, header=header)


def result_actions(topic_id, job_id, label, *, stop_kind=""):
    """The row after a failed task's answer: ``label`` (检查并继续, 重新投递结果) and, with ``stop_kind``,
    停止自动重试."""
    value = {"topic_id": topic_id, "job_id": job_id}
    columns = [column_button(label, {"kind": RETRY_KIND, **value})]
    if stop_kind:
        columns.append(column_button("停止自动重试", {"kind": stop_kind, **value}))
    return {"tag": "column_set", "element_id": "result_actions", "columns": columns}


def retry_status(content):
    """A pending automatic retry's line above the row."""
    return md(content, element_id="auto_retry_status")


def result_note(content):
    """The small print under the row."""
    return md(content, element_id="result_actions_note", size="notation")


def recovery_view(record, text, *, title, topic, label, continued, moved_to, how, retry, actions, snapshot):
    """The recovery card of ``record``'s task from what app/recovery_cards.py read and worded: ``text``; ``title``, what
    became of the task; ``topic``, its label or ""; ``how``, how an automatic retry moved it to ``moved_to``;
    ``retry`` and ``actions`` as ``stopped_card`` takes them; ``snapshot``, its trace for the 执行过程 panel; then the
    换配置 panel of a task with a binding."""
    # The title bar says what became of the task, colored by it: it replaces the task card's blue bar (a
    # bold first line read like any notice, 2026-11-02 review). The text starts with what to know next.
    tags = " · ".join(filter(None, [topic, f"#{record['job_id']}"]))
    header = (("grey", f"{tags} · 已转到任务 #{moved_to} 继续{how}") if moved_to is not None
              else ("blue" if continued else "green" if title == "结果已送达"
                    else "red" if title in {"执行失败", "任务未能启动"} else "orange", f"{tags} · {title}"))
    panels = []
    if snapshot:
        trace = finish_record_trace(ToolTrace.restore(snapshot), record)
        panels.append(panel_json(TraceView(trace.snapshot()).panel_model(False, timed=True)))
    if record.get("harness_id") and record.get("topic_id") and record.get("binding_revision"):
        panels.append(switch_panel(record["topic_id"], record["job_id"], record["harness_id"],
                                   record.get("binding_revision", 1)))
    name = topic or f"#{record['job_id']}"
    if moved_to is not None:
        summary = f"{name} · 已转到 #{moved_to} 继续"
    elif continued:
        summary = f"{name} · 已收到回答，继续处理中"
    elif title == "结果已送达":
        summary = f"✅ {name} · 结果已送达"
    elif title != "任务已中断":
        summary = f"⚠️ {name} · {title}"
    else:
        summary = interrupted_summary(name, label, record["auto_retry_at"] if actions else None)
    return stopped_card(text.lstrip("\n") or header[1], retry=retry, actions=actions, panels=panels, header=header,
                        summary=summary)
