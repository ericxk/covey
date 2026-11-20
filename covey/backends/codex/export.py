"""One turn's model requests and responses, from Codex's own rollout (the 下载请求与结果 button).

Codex 0.164.0 writes everything its model is sent into the thread's rollout, one ``response_item`` row per item
in order: the tools (``additional_tools``), the instructions (developer messages), AGENTS.md and the
environment, the user's messages, the model's output (reasoning with its encrypted content, messages, tool
calls) and the tools' outputs, other agents' messages (``agent_message``). A request's ``input`` is the items
so far, each without ``internal_chat_message_metadata_passthrough`` (rollout metadata Codex does not send).
After a response a ``token_usage_record`` row names it and its usage; a ``compacted`` row puts its
``replacement_history`` in place of the items. A sub-agent's thread starts with its parent's rows copied
after the parent's ``session_meta``, the items marked ``inherited_user_message``: history, not its requests.

Checked against a stand-in Responses server, through the Bridge's App Server path with the
catalog's request shape (25 requests of 12 threads: turns, parallel tool calls, reasoning, images in messages
and in the exec tool's output, sub-agents, two local compactions): every rebuilt input equal to the one sent,
item for item, but for what ``not_recorded`` lists, which Codex adds when it sends: a local compaction's prompt,
an "aborted" output for a call without one (a sub-agent forked mid-call), and for a model outside Codex's
catalog the tools and base instructions, which then stay out of the items. For a catalog model (Responses
Lite: the tools travel as the ``additional_tools`` item) images go without their ``detail``.

A response with no usage row was cut off: a sub-agent's message arrived while it streamed (its ``agent_message``
then precedes the next output) or the stream broke and Codex sent the request again. Its partial output stays
in the items, as Codex keeps it; the next output opens a request when an item came in between. Of 7,066
requests rebuilt from 120 real rollouts, 819 had no usage row. The ids carry the second they were
made: of the 6,247 with one, every output item was made no earlier than its response and no later than the
next response but one, a reasoning item older than its response (most likely a broken stream sent again).

Not in the rollout at all: the request's other fields (model, reasoning, store, include, prompt_cache_key,
client_metadata; the turn's ``turn_context`` row stands for them) and a request that failed and was sent again.
Over WebSocket (the signed-in account's models) Codex sends only the items after its previous response, with
that response's id; the export gives the whole input.

The export has, for the main thread and each sub-agent thread of the session (``session_meta.session_id``):
the thread's ``session_meta``, the requests made in the turn's time window with their output and usage, and
every rollout row of the window as written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from covey.backends.codex.sessions import CodexSessionStore
from covey.core.turn_export_rows import MARGIN, in_window, read_rows, stamp, window_rows

FORMAT = "harness-bridge/codex-turn-export"
VERSION = 1
PASSTHROUGH = "internal_chat_message_metadata_passthrough"
# Items the model writes; every other item is input. A message is the model's when its role is assistant.
OUTPUTS = frozenset({"reasoning", "function_call", "custom_tool_call", "local_shell_call", "web_search_call",
                     "image_generation_call", "tool_search_call"})
CALLS = frozenset({"function_call", "custom_tool_call"})
LOCAL_PROMPT = "Codex 在 input 末尾另加的压缩提示词（一条 user 消息）"
REMOTE = "发往 responses/compact 的请求：input 是当时的上下文，response.output 是替换后的上下文（replacement_history）"
NO_TOOLS = ("工具定义（请求的 tools 字段）和基础指令（meta.base_instructions，发送时是 input 的第一条 developer 消息）："
            "Codex 对模型目录外的模型不把它们记进上下文")
NOTE = ("requests：本轮每次模型请求。request.input 按 Codex 会话记录（rollout）的 response_item 还原：到这次请求为止的"
        "上下文，去掉 Codex 不发送的 internal_chat_message_metadata_passthrough；工具定义在 additional_tools 项里。"
        "not_recorded 列出 Codex 发送时另加、会话记录里没有的部分。请求的其他字段（model、reasoning、prompt_cache_key 等）"
        "会话记录没有，turn_context 是那一轮的设置。response：模型输出的各项和 token_usage_record 的用量；compaction 标出"
        "压缩上下文的请求；response.id 为空表示回复没有用量记录：流到一半被子 Agent 新来的消息打断，或断流后重发。用账号模型时 Codex 经 WebSocket 只发上次回复之后的新增项，这里给的是完整 input。失败后重发的"
        "请求不在记录里。meta：会话的 session_meta。rows：本轮时间窗内会话记录的原始行，未作改动。subagents：子 Agent "
        "会话的同样内容。")


def as_sent(item, lite):
    """``item`` as Codex sends it: without the rollout's metadata; for a model of its catalog (whose requests
    carry the tools as an ``additional_tools`` item: Responses Lite) its images without ``detail``."""
    item = {key: value for key, value in item.items() if key != PASSTHROUGH}
    for key in ("content", "output") if lite else ():
        parts = item.get(key)
        if isinstance(parts, list) and any(isinstance(part, dict) and "detail" in part for part in parts):
            item[key] = [{k: v for k, v in part.items() if k != "detail"}
                         if isinstance(part, dict) and part.get("type") == "input_image" else part for part in parts]
    return item


def is_lite(items):
    return any(item.get("type") == "additional_tools" for item in items)


def is_output(item):
    kind = item.get("type")
    return kind in OUTPUTS or (kind == "message" and item.get("role") == "assistant")


def rollout_export(path, since, until):
    """The rollout at ``path``: its session_meta, the requests made between ``since`` and ``until`` (epoch
    seconds) with their input, output and usage, and the rows written in that window."""
    rows = read_rows(path)
    meta = rows[0].get("payload") if rows and rows[0].get("type") == "session_meta" else None
    own = (meta or {}).get("id")
    history, requests, current, context, copied, lite = [], [], None, None, False, False
    for row in rows:
        kind = row.get("type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if kind == "session_meta":
            copied = payload.get("id") != own  # A fork's copy of its parent's rows follows the parent's.
        elif kind == "turn_context":
            context = payload
        elif kind == "response_item":
            item = as_sent(payload, lite or payload.get("type") == "additional_tools")
            lite = lite or item.get("type") == "additional_tools"
            inherited = bool((row.get("metadata") or {}).get("inherited_user_message"))
            copied = copied and inherited
            if is_output(item) and not inherited:
                if current is None or current["after_input"]:  # A response with no usage row ended before it.
                    current = opened(row, history, context)
                    requests.append(current)
                current["response"]["output"].append(item)
            elif current is not None:
                current["after_input"] = True
            history.append(item)
        elif kind == "token_usage_record" and not copied and payload.get("thread_id", own) == own:
            if current is None:  # A response with no output item.
                current = opened(row, history, context)
                requests.append(current)
            current["response"].update(id=payload.get("response_id"), usage=payload.get("usage"))
            current = None
        elif kind == "compacted":
            replacement = [item for item in payload.get("replacement_history") or [] if isinstance(item, dict)]
            lite = is_lite(replacement)
            replacement = [as_sent(item, lite) for item in replacement]
            rid = payload.get("compaction_response_id")
            last = requests[-1] if requests else None
            # A local compaction's summary is the request just before; a row naming no response gets the one
            # whose output is the newest item.
            summary = last and (last["response"]["id"] == rid if rid else
                                bool(last["response"]["output"]) and history[-1] is last["response"]["output"][-1])
            if copied:
                pass
            elif summary:
                last.update(compaction="local", not_recorded=[LOCAL_PROMPT])
            else:
                requests.append({**opened(row, history, context), "compaction": "remote", "not_recorded": [REMOTE]})
                requests[-1]["response"].update(id=rid, output=replacement)
            history, current = list(replacement), None
    selected = []
    for request in requests:
        request.pop("after_input")
        if in_window(stamp(request), since, until):
            selected.append(noted(request))
    return {"file": str(path), "meta": meta, "requests": selected, "rows": window_rows(rows, since, until)}


def opened(row, history, context):
    """A request whose first output (or usage, or compaction) is ``row``: its input is ``history`` so far."""
    turn = (row.get("payload") or {}).get(PASSTHROUGH) or {}
    return {"timestamp": row.get("timestamp"), "turn_id": turn.get("turn_id") or (context or {}).get("turn_id"),
            "turn_context": context, "request": {"input": list(history)},
            "response": {"id": None, "output": [], "usage": None}, "after_input": False}


def noted(request):
    """``request`` with ``not_recorded`` naming what Codex adds to its input when it sends."""
    items = request["request"]["input"]
    notes = list(request.get("not_recorded") or [])
    answered = {item.get("call_id") for item in items if str(item.get("type", "")).endswith("_output")}
    for item in items:
        if item.get("type") in CALLS and item.get("call_id") not in answered:
            notes.append(f"调用 {item.get('call_id')} 之后 Codex 补的输出项（output 为 \"aborted\"，id 由 Codex 生成）")
    if items and request.get("compaction") != "remote" and not is_lite(items):
        notes.append(NO_TOOLS)
    if notes:
        request["not_recorded"] = notes
    return request


def session_meta(path):
    """The rollout's first row's payload, or None."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            row = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    payload = row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None
    return payload if isinstance(payload, dict) else None


def thread_files(session_id, since, sessions):
    """(the thread's rollout or None, the rollouts written since ``since`` of the sub-agent threads under it)."""
    store = CodexSessionStore(Path(sessions))
    root = store.transcript(Path("."), session_id)
    if root is None:
        return None, []
    agents = []
    for path in sorted(store.sessions_dir(Path(".")).glob("*/*/*/rollout-*.jsonl")):
        try:
            fresh = path != root and os.stat(path).st_mtime >= since - MARGIN
        except OSError:
            continue
        meta = session_meta(path) if fresh else None
        if meta and meta.get("session_id") == session_id and meta.get("id") != session_id:
            agents.append(path)
    return root, agents


def export_turn(ctx, session_id, since, until, sessions):
    """The turn's export (a JSON-ready dict) from the rollouts under ``sessions`` (CODEX_HOME's).
    ValueError when the thread's rollout is not found."""
    root, agents = thread_files(session_id, since, sessions)
    if root is None:
        raise ValueError("找不到本轮的会话记录")
    subagents = [part for part in (rollout_export(path, since, until) for path in agents)
                 if part["requests"] or part["rows"]]
    return {"format": FORMAT, "version": VERSION, "note": NOTE, "session_id": session_id,
            "window": {"since": since, "until": until, "margin_seconds": MARGIN},
            "main": rollout_export(root, since, until), "subagents": subagents}
