"""The model text of a quoted message (render_replied_message_context) and of a merged forward, nested forwards included
(render_merge_forward_detail): each message's sender, time and text, and its images and files, downloaded."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from covey.feishu import message_read
from covey.feishu.mentions import quote_label_lines, with_mention_names
from covey.feishu.message_parts import (collect_message_parts_named, content_field, resource_specs,
                                                 text_message_texts)
from covey.feishu.message_read import (IncomingMedia, download_resource, fetch_detail_or_error,
                                                fetch_resources, item_type, message_item, message_items)


MERGE_FORWARD_TYPE = "merge_forward"
MAX_MERGE_FORWARD_ITEMS = 50
# Feishu replaces forwards nested past its own limit with this text.
FEISHU_HIDDEN_FORWARD = "转发层数过多"


def _body_content(item: dict[str, Any]) -> str:
    body = item.get("body")
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, str):
            return content
    content = item.get("content")
    return content if isinstance(content, str) else ""


def _sender_label(item: dict[str, Any], owner: str = "") -> str:
    """Who sent ``item``; with ``owner`` known (a group topic), the owner is 主人 and anyone else
    is marked 非主人."""
    sender = item.get("sender")
    if not isinstance(sender, dict):
        return "unknown"
    sender_type = str(sender.get("sender_type") or sender.get("type") or "unknown")
    sender_id = ""
    raw_id = sender.get("id")
    if isinstance(raw_id, str):
        sender_id = raw_id
    sender_id_obj = sender.get("sender_id")
    if not sender_id and isinstance(sender_id_obj, dict):
        for key in ("open_id", "user_id", "union_id"):
            value = sender_id_obj.get(key)
            if isinstance(value, str) and value:
                sender_id = value
                break
    if owner and sender_id == owner:
        return "主人"
    if sender_type == "app":
        return f"机器人 ({sender_id[-8:]})" if sender_id else "机器人"
    if owner and sender_id:
        return f"{sender_type} ({sender_id[-8:]}，非主人)"
    return f"{sender_type} ({sender_id[-8:]})" if sender_id else sender_type


def _message_time(item: dict[str, Any]) -> str:
    for key in ("create_time", "update_time"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _message_detail_error(detail: dict[str, Any]) -> str:
    error = detail.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("msg") or json.dumps(error, ensure_ascii=False))
    return str(error or detail.get("msg") or "unknown error")


def _resource_line(prefix: str, kind: str, name: str, media: IncomingMedia, key: str) -> str:
    """The line a quoted or forwarded image or file is shown as: where it was saved, or why it was not."""
    label = prefix + ("图片" if kind == "image" else "文件")
    named = f"（{name}）" if name else ""
    if media.local_path:
        return f"[{label}: {media.local_path}]{named}"
    return f"[{label}下载失败{named}: {key} - {media.error or 'unknown error'}]"


def _merge_child_parts(item: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """Display text plus resource specs of one quoted or merged-forward message: mention
    placeholders named, and lines posing as the Bridge's sender labels quoted."""
    text, specs = _child_parts(item)
    return quote_label_lines(with_mention_names(text, item)), specs


def message_text(item: dict[str, Any]) -> str:
    """The words of a message as Feishu returns it read back (its images and files only named by their keys' absence):
    what app/source_watch.py gives a task once the owner edited the message."""
    return _merge_child_parts(item)[0]


def changed_words(item: dict[str, Any]) -> str:
    """What a quoted message's header adds when the message was recalled (``deleted``) or edited (``updated``) since it
    was sent, else ''."""
    if item.get("deleted") is True:
        return "，已被发送者撤回"
    return "，发出后编辑过（下面是编辑后的内容）" if item.get("updated") is True else ""


def _child_parts(item: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """Return display text plus resource specs for one merged-forward child."""
    msg_type = str(item.get("msg_type") or item.get("message_type") or "").lower()
    raw_content = _body_content(item)
    if msg_type in {"text", "post"}:
        texts, image_keys, file_keys, names = collect_message_parts_named(raw_content)
        if msg_type == "text" and not texts:
            texts = text_message_texts(raw_content)
        return "\n".join(texts).strip(), resource_specs(image_keys, file_keys, names)
    if msg_type in {"image", "img"}:
        key = content_field(raw_content, "image_key")
        return "", [("image", key, "")] if key else []
    if msg_type in {"file", "media", "video", "audio"}:
        key = content_field(raw_content, "file_key")
        return "", [("file", key, content_field(raw_content, "file_name") or "")] if key else []
    if msg_type == "interactive":
        texts, image_keys, file_keys, names = collect_message_parts_named(raw_content)
        text = "\n".join(texts).strip()
        if not text and not image_keys and not file_keys:
            text = "[卡片消息，无可提取文本]"
        return text, resource_specs(image_keys, file_keys, names)
    if msg_type == MERGE_FORWARD_TYPE:
        return "[嵌套合并转发，未展开]", []
    return f"[未支持的消息类型: {msg_type or '未知'}]", []


def render_replied_message_context(
    event: dict[str, Any],
    parent_detail: dict[str, Any],
    *,
    media_dir: Path,
    profile: str | None = None,
    downloader: Any = download_resource,
    message_detail_fetcher: Any = None,
    owner_id: str = "",
) -> str:
    """The message ``event`` replies to, as prompt context; ``owner_id`` labels its sender (a group topic)."""
    parent_id = str(event.get("parent_id") or "").strip()
    if not parent_id:
        return ""

    root_id = str(event.get("root_id") or "").strip()
    thread_id = str(event.get("thread_id") or "").strip()
    topic_root = bool(thread_id and root_id == parent_id)
    lines = [("话题起始消息（背景参考）：" if topic_root else "回复上下文："),
             f"- parent_id: `{parent_id}`"]
    if root_id:
        lines.append(f"- root_id: `{root_id}`")
    if thread_id:
        lines.append(f"- thread_id: `{thread_id}`")
    if topic_root:
        lines.append("飞书话题中的 parent_id 可指向话题起始消息，不代表本条消息要补充话题的第一个任务。"
                     "本轮意图以上方最新消息为准；补充目标以任务卡绑定的任务为准。")

    if parent_detail.get("ok") is False:
        lines.append(f"- 被回复消息获取失败：{_message_detail_error(parent_detail)}")
        return "\n".join(lines)

    item = message_item(parent_detail, parent_id)
    if not item:
        lines.append("- 被回复消息获取失败：响应里没有对应消息。")
        return "\n".join(lines)

    sender = _sender_label(item, owner_id)
    msg_type = item_type(item) or "unknown"
    created = _message_time(item)
    header = f"{'话题起始消息' if topic_root else '被回复消息'}：发送者 {sender}，类型 `{msg_type}`"
    if created:
        header += f"，时间戳 `{created}`"
    header += changed_words(item)
    lines.append(header)
    if item.get("deleted") is True:
        return "\n".join(lines)  # What is left of it is Feishu's placeholder, not the sender's words.

    if msg_type == MERGE_FORWARD_TYPE:
        lines.append(render_merge_forward_detail(parent_detail, parent_message_id=parent_id,
            media_dir=media_dir, profile=profile, downloader=downloader,
            message_detail_fetcher=message_detail_fetcher, owner_id=owner_id))
        return "\n".join(lines)
    body_text, specs = _merge_child_parts(item)
    if body_text:
        lines.append(body_text)
    elif not specs:
        lines.append("[被回复消息无可提取文本]")

    for kind, key, name, downloaded in fetch_resources(specs, [parent_id], media_dir=media_dir, profile=profile,
                                                       downloader=downloader):
        lines.append(_resource_line("被回复", kind, name, downloaded, key))

    return "\n".join(lines)


def _forward_failure(detail: Any, state: dict) -> str | None:
    """The whole text for a forward whose detail cannot be expanded, or None."""
    if not isinstance(detail, dict):
        return "[合并转发详情格式异常，未展开]"
    if detail.get("ok") is False:
        error = detail.get("error")
        if isinstance(error, dict):
            error_text = error.get("message") or error.get("msg") or json.dumps(error, ensure_ascii=False)
        else:
            error_text = str(error or "unknown error")
        state["omitted"].append("展开失败")
        return f"收到一条合并转发消息，但展开失败：{error_text}。可能是机器人无权查看原消息，这部分内容没有传给模型。"
    return None


def _is_forward_child(item: dict[str, Any], parent_message_id: str, tree: bool) -> bool:
    msg_type = str(item.get("msg_type") or item.get("message_type") or "").lower()
    item_id = str(item.get("message_id") or item.get("id") or "")
    if item_id == parent_message_id and msg_type == MERGE_FORWARD_TYPE:
        return False
    return not (tree and item.get("upper_message_id") != parent_message_id)


def _forward_limit_line(state: dict, max_items: int) -> str:
    state["omitted"].append(f"累计超过 {max_items} 条")
    return f"[合并转发累计达到 {max_items} 条限制，后续未展开]"


def _nested_forward_refusal(child_id: str, state: dict, depth: int, max_depth: int, max_items: int) -> str | None:
    """Why a nested forward is not expanded, or None to expand it."""
    if not child_id:
        return "[嵌套合并转发缺少消息 ID，未展开]"
    if child_id in state["seen"]:
        return "[重复或循环引用的合并转发，已跳过]"
    if depth + 1 >= max_depth:
        state["omitted"].append(f"嵌套超过 {max_depth} 层")
        return f"[嵌套达到 {max_depth} 层限制，后续未展开]"
    if state["remaining"] <= 0:
        return _forward_limit_line(state, max_items)
    return None


def _forward_resource_lines(
    item: dict[str, Any],
    specs: list[tuple[str, str, str]],
    parent_message_id: str,
    state: dict,
    *,
    media_dir: Path,
    profile: str | None,
    downloader: Any,
) -> list[str]:
    lines: list[str] = []
    item_id = str(item.get("message_id") or item.get("id") or "")
    # A resource belongs to the forwarded message itself; the forward's own
    # ID answers 234003 "File not in msg". Keep it as a fallback.
    owners = list(dict.fromkeys(x for x in (item_id, parent_message_id) if x))
    for kind, key, name, downloaded in fetch_resources(specs, owners, media_dir=media_dir, profile=profile,
                                                       downloader=downloader, start=state["media_index"]):
        state["media_index"] += 1
        state[("images" if kind == "image" else "files") if downloaded.local_path else "failed"] += 1
        lines.append(_resource_line("", kind, name, downloaded, key))
    return lines


def _forward_message_lines(
    item: dict[str, Any],
    parent_message_id: str,
    state: dict,
    *,
    media_dir: Path,
    profile: str | None,
    downloader: Any,
) -> list[str]:
    """One forwarded message that is not itself a forward: its text, then its downloaded resources."""
    lines: list[str] = []
    body_text, specs = _merge_child_parts(item)
    if body_text:
        lines.append(body_text)
        if FEISHU_HIDDEN_FORWARD in body_text:
            state["omitted"].append("飞书隐藏了过深的转发层")
    lines.extend(_forward_resource_lines(item, specs, parent_message_id, state,
                                         media_dir=media_dir, profile=profile, downloader=downloader))
    lines.append("")
    return lines


def _nested_forward_lines(
    item: dict[str, Any],
    detail: dict[str, Any],
    items: list[dict[str, Any]],
    tree: bool,
    state: dict,
    *,
    media_dir: Path,
    profile: str | None,
    max_items: int,
    downloader: Any,
    fetcher: Any,
    max_depth: int,
    depth: int,
) -> list[str]:
    """A forward inside a forward: why it is not expanded, or its rendering between two marker lines."""
    child_id = str(item.get("message_id") or item.get("id") or "")
    refusal = _nested_forward_refusal(child_id, state, depth, max_depth, max_items)
    if refusal is not None:
        return [refusal]
    if tree and any(other.get("upper_message_id") == child_id for other in items):
        nested = detail  # Already in this response.
    else:
        nested = fetch_detail_or_error(fetcher, child_id, profile)
    lines = [f"—— 嵌套转发 · 第 {depth + 2} 层 ——"]
    lines.append(render_merge_forward_detail(nested, parent_message_id=child_id,
        media_dir=media_dir, profile=profile, max_items=max_items, downloader=downloader,
        message_detail_fetcher=fetcher, max_depth=max_depth, _depth=depth+1, _state=state))
    lines.append("—— 嵌套转发结束 ——")
    return lines


def render_merge_forward_detail(
    detail: dict[str, Any],
    *,
    parent_message_id: str,
    media_dir: Path,
    profile: str | None = None,
    max_items: int = MAX_MERGE_FORWARD_ITEMS,
    downloader: Any = download_resource,
    message_detail_fetcher: Any = None,
    max_depth: int = 5,
    owner_id: str = "",
    _depth: int = 0,
    _state: dict | None = None,
) -> str:
    """Render a merged-forward message into text context and downloaded resources; ``owner_id``
    labels the senders (a group topic), nested levels included."""
    fetcher = message_detail_fetcher or message_read.fetch_message_detail
    state = _state if _state is not None else new_forward_state(max_items, owner_id)
    if parent_message_id in state["seen"]:
        return "[重复或循环引用的合并转发，已跳过]"
    state["seen"].add(parent_message_id)
    failure = _forward_failure(detail, state)
    if failure is not None:
        return failure

    items = message_items(detail)
    # The API returns every nested level at once, each item naming its forward in
    # upper_message_id. Rendering them all at the top and again per nested fetch
    # repeated content (14 messages were counted as 47).
    tree = any(item.get("upper_message_id") for item in items)
    children = [item for item in items if _is_forward_child(item, parent_message_id, tree)]
    if not children:
        return "收到一条合并转发消息，但消息详情里没有可展开的子消息。"

    total = len(children)
    truncated = total > max_items
    selected = children[:max_items]
    lines = [f"合并转发消息，共 {total} 条记录。"]
    if truncated:
        lines.append(f"仅解析前 {max_items} 条，后续内容已省略。")
        state["omitted"].append(f"超过 {max_items} 条")
    lines.append("")

    for idx, item in enumerate(selected, 1):
        if state["remaining"] <= 0:
            lines.append(_forward_limit_line(state, max_items))
            break
        state["remaining"] -= 1
        state["messages"] += 1
        header = f"[{idx}] 发送者：{_sender_label(item, state.get('owner', ''))}"
        created = _message_time(item)
        if created:
            header += f"　时间戳：{created}"
        lines.append(header)

        if item_type(item) == MERGE_FORWARD_TYPE:
            lines.extend(_nested_forward_lines(item, detail, items, tree, state, media_dir=media_dir, profile=profile,
                max_items=max_items, downloader=downloader, fetcher=fetcher, max_depth=max_depth, depth=_depth))
        else:
            lines.extend(_forward_message_lines(item, parent_message_id, state,
                                                media_dir=media_dir, profile=profile, downloader=downloader))

    return "\n".join(lines).strip()


def new_forward_state(max_items: int = MAX_MERGE_FORWARD_ITEMS, owner: str = "") -> dict:
    return {"remaining": max_items, "seen": set(), "media_index": 0, "messages": 0,
            "images": 0, "files": 0, "failed": 0, "omitted": [], "owner": owner}
