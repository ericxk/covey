"""The receipt card of a supplement sent into a running task.

A receipt shows one delivery record (``core/deliveries.py``): on the queued
job's own card or on a browse card.
"""
from __future__ import annotations

import time

from covey.cards.elements import button_row, card, markdown_panel, topic_button
from covey.core.deliveries import CHECK, REQUEUE, WITHDRAW, offers, shown_text
from covey.core.butler import worded

CHECK_KIND = "bridge_steer_status"
REQUEUE_KIND = "bridge_steer_requeue"
WITHDRAW_KIND = "bridge_steer_withdraw"
# The receipt's buttons in their order, each shown while the record offers it (core/deliveries.py offers).
BUTTONS = ((WITHDRAW, "撤回", WITHDRAW_KIND, ""), (CHECK, "检查送达状态", CHECK_KIND, ""),
           (REQUEUE, "重新排队执行", REQUEUE_KIND, "primary"))
STAGE = {
    "submitting": "⏳ 已保存，正在送达",
    "held": "⏳ 已保存，任务准备好后自动送入",
    "withdrawn": "已撤回，没有发出",
    # The model takes a supplement in when the step it is on ends: a command or a sub-task can take long
    # (repo review U6: #1052 read 45 minutes after 后端已接收; Claude Code reads queued input only then).
    "sent": "✓ 后端已接收 · 当前一步结束后读到",
    "read": "✓ 模型已读取",
    "refused": "⚠️ 没有发出",
    "uncertain": "⚠️ 送达未确认",
}


def _clock(value):
    return time.strftime("%H:%M:%S", time.localtime(value)) if isinstance(value, (int, float)) else ""


def stage_line(record):
    status = record.get("status", "submitting")
    if status == "sent" and record.get("target_ended") and not record.get("merged"):
        return "⚠️ 任务已结束，模型没有读到"
    line = STAGE.get(status, status)
    if status in {"refused", "uncertain"} and record.get("error"):
        line += "：" + str(record["error"])
    return line


def history_lines(record):
    """What happened to it, in the order it happened."""
    steps = [(record.get("saved_at"), "已保存"), (record.get("held_at"), "等任务准备好"),
             (record.get("sent_at"), "并入本轮请求" if record.get("merged") else "后端已接收"),
             (record.get("read_at"), "模型已读取"),
             (record.get("withdrawn_at") if record.get("status") == "withdrawn" else None, "已撤回"),
             (record.get("ended_at") if record.get("status") == "sent" else None, "任务结束，模型未读取")]
    rows = [f"{label} {_clock(at)}" for at, label in sorted((s for s in steps if isinstance(s[0], (int, float))),
                                                             key=lambda s: s[0])]
    if record.get("status") == "refused":
        rows.append("没有发出：" + str(record.get("error") or ""))
    elif record.get("status") == "uncertain":
        rows.append("送达未确认：" + str(record.get("error") or ""))
    return " → ".join(rows)


def receipt_card(record, *, source=""):
    """The compact receipt: one line, details on demand, a check button while unconfirmed."""
    job_id = record.get("job_id")
    status = record.get("status", "submitting")
    unread = status == "sent" and record.get("target_ended") and not record.get("merged")
    if unread:
        head = f"⚠️ **任务 #{job_id} 已结束，模型没有读到这条补充**"
        summary = f"补充 #{job_id} 未被读取"
    elif status in {"sent", "read"}:
        head = f"✓ **已补充到 #{job_id}**" + ("，不再单独排队" if source else "")
        summary = f"✓ 已补充到 #{job_id}"
    elif status == "refused":
        head = f"⚠️ **没有补充到 #{job_id}**：{record.get('error') or '任务当前不能接收补充'}"
        summary = f"没有补充到 #{job_id}"
    elif status == "held":
        head = f"⏳ **已保存，尚未送达 #{job_id}**：{record.get('error') or '任务刚开始'}，准备好后自动送入"
        summary = f"补充 #{job_id} 已保存，尚未送达"
    elif status == "withdrawn":
        head = f"**已撤回**，这条补充没有发给 #{job_id}"
        summary = f"补充 #{job_id} 已撤回"
    else:
        head = f"⏳ **补充 #{job_id} 送达未确认**" + (f"：{record['error']}" if record.get("error") else "")
        summary = f"补充 #{job_id} 送达未确认"
    stage = " · " + STAGE[status] if status in {"sent", "read"} and not unread else ""
    if record.get("merged"):
        stage = " · ✓ 任务开始前收到，已并入本轮请求"
    details = []
    if record.get("requeued_job_id"):
        details.append(f"已重新排队为任务 #{record['requeued_job_id']}。")
    elif record.get("requeue_state") in {"submitting", "uncertain"}:
        details.append("重新排队已提交，结果未确认；不会重复排队，需要时直接在本话题再发送一次。")
    if source:
        details.append(source)
    details.append("状态：" + history_lines(record))
    if unread:
        details.append("后端接收了这条补充，但任务在模型读到它之前就结束了（停止、完成或中断）。需要时"
                       + ("点“重新排队执行”，或" if record.get("requeue") else "") + "直接在本话题再发送一次。")
    elif status in {"sent"} and not record.get("merged"):
        details.append("“后端已接收”表示任务进程已把补充交给模型后端。模型在手上这一步（一条命令、一个子任务）结束后才读到它，"
                       "长命令或子任务在跑时要等它们结束。Claude 读到后这里变成“模型已读取”；其他后端不提供读取回执，状态停在这里。")
    if status == "uncertain":
        details.append("不会自动重发。点“检查送达状态”重新核对；确认没有发出后可重新排队。")
    if status == "held":
        details.append("任务开始接收补充后自动送入，只送一次；送入前可以撤回。任务在那之前结束时显示“没有发出”。")
    # The butler is no topic: its receipts say so (the owner's own words below stay as written).
    details = [worded(line, record) for line in details]
    details.append("补充内容：\n" + "\n".join("> " + line for line in str(shown_text(record)).splitlines()[:40]))
    elements = [{"tag": "markdown", "element_id": "steer_receipt_line", "content": head + stage},
                markdown_panel("steer_receipt", "查看详情", "\n\n".join(details))]
    offered = offers(record)
    buttons = [(label, kind, tone) for offer, label, kind, tone in BUTTONS if offer in offered]
    if buttons:
        elements.append(button_row([
            topic_button(label, tone=tone, value={"kind": kind, "topic_id": record.get("topic_id"),
                                                  "token": record.get("token"), "job_id": record.get("job_id")})
            for label, kind, tone in buttons]))
    return card(elements, summary, update_multi=True)
