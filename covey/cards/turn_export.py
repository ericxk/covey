"""The 下载请求与结果 element at the bottom of an ended task card (app/turn_export.py fills it)."""
from __future__ import annotations

import time

from covey.cards.elements import callback, topic_button
from covey.core.text import safe_text

ELEMENT = "turn_export"
KIND = "bridge_turn_export"
LABEL = "📥 下载请求与结果"


def _size(n):
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def export_panel(topic_id, job_id, state=None):
    """The button, and one line for a state ({status: building|done|failed, …}): a click packs the turn; once
    uploaded the button opens the file in the cloud space. It stays clickable while packing: a 'building' a
    restart cut off is started again by the next click (a click during a live one only redraws)."""
    state = state if isinstance(state, dict) else {}
    status = state.get("status")
    button = topic_button(LABEL, element_id="export_action", behaviors=(
        [{"type": "open_url", "default_url": state["url"]}] if status == "done" and state.get("url")
        else callback({"kind": KIND, "topic_id": topic_id, "job_id": job_id})))
    lines = []
    if status == "building":
        lines.append("📥 正在打包本轮的请求和结果，完成后上传到云空间…")
    elif status == "done" and state.get("url"):
        at = time.strftime("%H:%M", time.localtime(state.get("at") or time.time()))
        counts = f"{state.get('requests', 0)} 次请求" + (f" · {state['subagents']} 个子任务" if state.get("subagents") else "")
        lines.append(f"📥 {at} 已上传云空间：[{safe_text(state.get('name') or '文件', 80)}]({state['url']})"
                     f" · {counts} · {_size(int(state.get('size') or 0))}（解压后 {_size(int(state.get('raw_size') or 0))}）")
    elif status == "failed":
        lines.append(f"📥 打包没有完成：{safe_text(state.get('error') or '未知原因', 120)}；可以再点一次")
    # Sized to its text like the 🔁 📦 buttons above it, not stretched across the card.
    column = [{"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": "auto", "elements": [button]}]}]
    column += [{"tag": "markdown", "text_size": "notation", "content": "\n".join(lines)}] if lines else []
    return {"tag": "column_set", "element_id": ELEMENT, "flex_mode": "none", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": column}]}
