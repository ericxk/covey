"""Mentions in a Feishu message: whom one names (mention_id), the block listing them for the model (render_mentions),
their placeholders written as names, and the lines of a message body that read like the Bridge's sender labels,
quoted."""

from __future__ import annotations

import re
from typing import Any


def mention_id(mention: dict[str, Any]) -> str:
    """The ID a mention names: an open_id in an event; in a message detail a user's open_id,
    a bot's app ID."""
    raw_id = mention.get("id")
    if isinstance(raw_id, dict):
        for key in ("open_id", "user_id", "union_id"):
            value = raw_id.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(raw_id, str):
        return raw_id
    return ""


def drop_mentions(mentions: Any, ids) -> list:
    """``mentions`` without the entries naming one of ``ids``."""
    if not isinstance(mentions, list):
        return []
    return [m for m in mentions if not (isinstance(m, dict) and mention_id(m) in ids)]


# Lines a message body could use to pass for the Bridge's own labels of who sent what.
_LABEL_LINE = re.compile(r"(?m)^([ \t]*)(?=发送者[：:]|被回复消息|话题起始消息|回复上下文[：:]|提及信息[：:]|\[\d+\][ 　]*发送者[：:])")


def quote_label_lines(text: str) -> str:
    """``text`` with each line that reads like one of the Bridge's sender labels quoted (``> ``):
    in a prompt, an unquoted label line is always the Bridge's."""
    return _LABEL_LINE.sub(r"\1> ", text)


def with_mention_names(text: str, item: dict[str, Any]) -> str:
    """``text`` of a fetched message with Feishu's mention placeholders (``@_user_1``) as ``@名字``."""
    mentions = item.get("mentions") if isinstance(item.get("mentions"), list) else []
    named = {str(m["key"]): str(m["name"]).lstrip("@") for m in mentions
             if isinstance(m, dict) and m.get("key") and m.get("name")}
    if not text or not named:
        return text
    pattern = "|".join(re.escape(key) for key in sorted(named, key=len, reverse=True))
    return re.sub(pattern, lambda m: "@" + named[m.group(0)], text)


def render_mentions(mentions: Any) -> str:
    if not isinstance(mentions, list) or not mentions:
        return ""
    lines = ["提及信息："]
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = str(mention.get("key") or "").strip()
        name = str(mention.get("name") or "").strip()
        mentioned_type = str(mention.get("mentioned_type") or "").strip()
        named = mention_id(mention)
        parts = []
        if key:
            parts.append(f"`{key}`")
        if name:
            parts.append(name)
        if mentioned_type:
            parts.append(mentioned_type)
        if named:
            parts.append(f"`{named}`")
        if parts:
            lines.append("- " + " / ".join(parts))
    return "\n".join(lines) if len(lines) > 1 else ""
