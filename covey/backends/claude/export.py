"""One turn's model requests and responses, from Claude Code's own session records (the 下载请求与结果 button).

Claude Code 2.1.310 writes every model request into the session transcript, compactly: the system prompt and
tools once per shape (``api-request-shape``), messages that are not transcript rows as ``api-request-blob``,
and per request an ``api-request`` row: its params, ``base`` (an earlier request; else the one before) and
``keep`` (how many of that request's messages it starts with), ``tail`` (the hashes of the messages after
them: a blob, a user row's message, or an assistant message's rows merged by message id), the
``cacheControl`` marks and ``requestDigest``, the SHA-256 of the request object with keys sorted in compact
JSON. That object is Claude Code's form of the request: string contents become one text block, and its
messages may still hold the ``system`` role, which it folds into the user turn when it sends. A rebuilt request whose digest matches is exactly the one
Claude Code recorded (checked on 1,783 requests of five sessions, 2.1.300 and 2.1.310: all matched
but the requests with an image, whose data the transcript does not keep, and those built on them).

The export has, for the main session and each subagent transcript: the requests made in the turn's time
window, each rebuilt (or why not) with the assistant messages that answered it, and every transcript row
of the window as written. Nothing here reads the network or the Bridge's records.
"""

from __future__ import annotations

import hashlib
import json

from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root
from covey.core.turn_export_rows import MARGIN, in_window, read_rows, stamp, window_rows

FORMAT = "harness-bridge/claude-turn-export"
VERSION = 1
NOTE = ("requests：本轮每次模型请求，按 Claude Code 会话记录（api-request、api-request-shape、api-request-blob）还原的"
        "请求对象；digest_verified 为 true 表示与 Claude Code 记下的 requestDigest 一致。这是 Claude Code 自己的请求"
        "形式，messages 里可能还有 system 角色，发送时并入 user。responses：之后返回的 assistant 消息（同一消息 ID 的"
        "内容块合并，带 usage、stop_reason）。rows：本轮时间窗内会话记录的原始行，未作改动。subagents：子任务的同样内容。")


def canon(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(canon(value).encode()).hexdigest()


def with_marks(body, marks):
    """(``body`` with each ``{"path", "value"}`` cache mark set as ``cache_control``, the marks whose path is not
    in it). The containers along a path are copied, so the messages shared with other requests stay as they are."""
    unapplied = []
    for mark in marks or ():
        path = mark.get("path") or []
        try:
            node, copies = body, []
            for key in path:
                child = node[key]
                if not isinstance(child, (dict, list)):
                    raise TypeError(key)
                copies.append((node, key, dict(child) if isinstance(child, dict) else list(child)))
                node = copies[-1][2]
            if not isinstance(node, dict):
                raise TypeError(path[-1] if path else None)
        except (KeyError, IndexError, TypeError):
            unapplied.append(mark)
            continue
        for parent, key, child in copies:
            parent[key] = child
        node["cache_control"] = mark.get("value")
    return body, unapplied


def transcript_export(path, since, until):
    """The requests of ``path`` made between ``since`` and ``until`` (epoch seconds), rebuilt, with their
    responses, and the rows written in that window."""
    rows = read_rows(path)
    shapes, store, merged, by_request = {}, {}, {}, {}
    previous, requests, current = [], [], None
    for row in rows:
        kind = row.get("type")
        message = row.get("message") if isinstance(row.get("message"), dict) else None
        if kind == "api-request-shape" and isinstance(row.get("shape"), dict):
            shapes[row.get("shapeHash")] = row["shape"]
        elif kind == "api-request-blob" and isinstance(row.get("hash"), str):
            store[row["hash"]] = row.get("message")
        elif kind == "user" and message is not None:
            entry = {"role": message.get("role"), "content": message.get("content")}
            store[digest(entry)] = entry
        elif kind == "assistant" and message is not None:
            parts = merged.setdefault(message.get("id"), {"rows": [], "content": []})
            parts["rows"].append(message)
            parts["content"].extend(message.get("content") or [])
            entry = {"role": "assistant", "content": list(parts["content"])}
            store[digest(entry)] = entry
            if current is not None and message.get("id") not in current["response_ids"]:
                current["response_ids"].append(message.get("id"))
        elif kind == "api-request":
            current = rebuild(row, shapes, store, by_request, previous)
            previous = current.pop("messages")
            by_request[row.get("id")] = previous
            current["in_window"] = in_window(stamp(row), since, until)
            requests.append(current)
    selected = []
    for request in requests:
        ids = request.pop("response_ids")
        if request.pop("in_window"):
            request["responses"] = [response(merged[mid]) for mid in ids if mid in merged]
            selected.append(request)
    return {"file": str(path), "requests": selected, "rows": window_rows(rows, since, until)}


def rebuild(row, shapes, store, by_request, previous):
    """One api-request row as {id, timestamp, …, request or error, messages (for the next), response_ids}. A
    message the transcript does not hold (an image's data: the row keeps only "[Image: source: …]") stands as
    ``{"missing_message": hash}`` and is listed, so the rest of the request and the requests built on it
    are still there; their digests cannot match."""
    out = {"id": row.get("id"), "timestamp": row.get("timestamp"), "query_source": row.get("querySource"),
           "request_digest": row.get("requestDigest"), "digest_verified": False, "response_ids": []}
    base = by_request.get(row.get("base")) if row.get("base") else previous
    if base is None:
        out.update(request=None, error=f"base request {row.get('base')} is not in this transcript", messages=[])
        return out
    tail, missing = [], []
    for key in row.get("tail") or []:
        if key in store:
            tail.append(as_blocks(store[key]))
        else:
            missing.append(key)
            tail.append({"missing_message": key})
    messages = list(base[:int(row.get("keep") or 0)]) + tail
    out["messages"] = messages
    shape = shapes.get(row.get("shapeHash"))
    if shape is None:
        out.update(request=None, error=f"shape {row.get('shapeHash')} is not in this transcript")
        return out
    body, unapplied = with_marks({**(row.get("params") or {}), "system": shape.get("system"),
                                  "tools": shape.get("tools"), "messages": messages}, row.get("cacheControl"))
    out.update(request=body, digest_verified=digest(body) == row.get("requestDigest"))
    if missing:
        out["missing_messages"] = missing
    if unapplied:
        out["unapplied_cache_marks"] = unapplied
    if any("missing_message" in m for m in messages if isinstance(m, dict)) and not missing:
        out["inherits_missing_messages"] = True
    return out


def as_blocks(message):
    """A message whose content is a string, with it as one text block, as Claude Code sends (and digests) it: the
    summary that starts a compacted session is one."""
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return {**message, "content": [{"type": "text", "text": message["content"]}]}
    return message


def response(parts):
    """An assistant message as answered: its last row's fields with the content of all its rows."""
    return {**parts["rows"][-1], "content": parts["content"]}


def session_files(ctx, session_id, env):
    """(transcript, subagent transcripts) of the session, or (None, [])."""
    path = ClaudeSessionStore(claude_projects_root(env)).locate(ctx.workdir, session_id)
    if path is None:
        return None, []
    agents = path.parent / session_id / "subagents"
    return path, sorted(agents.glob("agent-*.jsonl")) if agents.is_dir() else []


def export_turn(ctx, session_id, since, until, env):
    """The turn's export (a JSON-ready dict). ValueError when the session's transcript is not found."""
    path, agents = session_files(ctx, session_id, env)
    if path is None:
        raise ValueError("找不到本轮的 Claude Code 会话记录")
    main = transcript_export(path, since, until)
    subagents = []
    for agent in agents:
        if agent.stat().st_mtime < since - MARGIN:
            continue
        part = transcript_export(agent, since, until)
        if part["requests"] or part["rows"]:
            meta = agent.with_suffix(".meta.json")
            try:
                part["meta"] = json.loads(meta.read_text()) if meta.is_file() else None
            except (OSError, ValueError):
                part["meta"] = None
            subagents.append(part)
    return {"format": FORMAT, "version": VERSION, "note": NOTE, "session_id": session_id,
            "window": {"since": since, "until": until, "margin_seconds": MARGIN}, "main": main, "subagents": subagents}
