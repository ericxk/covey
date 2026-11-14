#!/usr/bin/env python3
"""A received Feishu message as the text its model is sent (enrich_incoming_event): what the user wrote, its
attachments downloaded and listed (core/attachments.py), its mentions, the message it quotes and a merged forward
(feishu/forward.py). Its content is read by feishu/message_parts.py, Feishu by feishu/message_read.py."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from covey.core import timing
from covey.core.attachments import (ATTACHMENTS_HEADER, ATTACHMENTS_HINT, FAILED_HEADER, attachment_line,
                                             attachment_summary, document_link_note)
from covey.core.state import ensure_private_dir, write_private_text
from covey.feishu import message_read
from covey.feishu.events import REPLY_CONTEXT
from covey.feishu.forward import (MERGE_FORWARD_TYPE, new_forward_state, render_merge_forward_detail,
                                           render_replied_message_context)
from covey.feishu.mentions import drop_mentions, render_mentions
from covey.feishu.message_parts import collect_message_parts_named, resource_specs, text_message_texts
from covey.feishu.message_read import (IncomingMedia, apply_message_metadata, fetch_detail_or_error,
                                                fetch_message_detail, fetch_resources, message_item)


@dataclass(frozen=True)
class EnrichedIncomingMessage:
    text: str
    event: dict[str, Any]
    display_text: str = ""
    # What `text` adds to what the user wrote (attachment paths, a forward's bodies, the quoted message), for
    # an answer or a held note that keeps the user's wording on its cards; the name is older than the quotes.
    attachments: str = ""


def _saved_root_path(media_dir: Path, message_id: str) -> Path:
    return Path(media_dir) / ("topic-root-" + re.sub(r"[^A-Za-z0-9_-]", "_", message_id)[:120] + ".json")


def _saved_root(media_dir: Path, message_id: str) -> dict[str, Any] | None:
    """The topic's first message as fetched for an earlier reply in it; None when not saved."""
    try:
        detail = json.loads(_saved_root_path(media_dir, message_id).read_text())
    except (OSError, ValueError):
        return None
    return detail if isinstance(detail, dict) and message_item(detail, message_id) else None


def _save_root(media_dir: Path, message_id: str, detail: dict[str, Any]) -> None:
    """Every reply in a topic names its first message as its parent: read it from Feishu once
    (0.43 s each time, S37) and keep it beside the topic's media. Only a detail that has it."""
    if detail.get("ok") is False or not message_item(detail, message_id):
        return
    try:
        ensure_private_dir(Path(media_dir))
        write_private_text(_saved_root_path(media_dir, message_id), json.dumps(detail, ensure_ascii=False))
    except (OSError, TypeError, ValueError):
        pass  # Fetched again next time.


def _needs_own_detail(event: dict[str, Any], message_type: str) -> bool:
    """Whether the message's own detail adds anything to its event. The body is the event's
    anyway; the consumer marks an event (REPLY_CONTEXT) when its lark-cli puts the type, root,
    thread, mentions and the parent of a reply (``reply_to``, our ``parent_id``) in it. A reply
    always has a root_id (so did every one of 652 live user messages, 2026-10-26): an unmarked
    event, a root without a parent, or no type still fetches."""
    return (not event.get(REPLY_CONTEXT) or not message_type
            or (bool(event.get("root_id")) and not event.get("parent_id")))


def _current_message_type(
    enriched_event: dict[str, Any],
    message_id: str,
    message_type: str,
    message_detail: dict[str, Any] | None,
    profile: str | None,
    fetch_message_details: bool,
    fetcher: Any,
) -> str:
    """Copy the message's own detail onto the event; return the type it names. It is fetched only
    when the event lacks what it would add (_needs_own_detail)."""
    current_detail = message_detail
    if (fetch_message_details and message_id and message_type != MERGE_FORWARD_TYPE and current_detail is None
            and _needs_own_detail(enriched_event, message_type)):
        current_detail = fetch_detail_or_error(fetcher, message_id, profile)
        timing.note(detail="fetched")  # Its own detail: skipped when the event names all it would add.
    if current_detail and message_type != MERGE_FORWARD_TYPE and current_detail.get("ok") is not False:
        item = message_item(current_detail, message_id)
        if item:
            apply_message_metadata(enriched_event, item)
            message_type = str(enriched_event.get("message_type") or message_type).strip()
    return message_type


def _merge_forward_message(
    detail: dict[str, Any],
    enriched_event: dict[str, Any],
    message_id: str,
    *,
    media_dir: Path,
    profile: str | None,
    fetcher: Any,
    owner_id: str = "",
) -> EnrichedIncomingMessage:
    state = new_forward_state(owner=owner_id)
    text = render_merge_forward_detail(
        detail,
        parent_message_id=message_id,
        message_detail_fetcher=fetcher,
        media_dir=media_dir,
        profile=profile,
        _state=state,
    )
    received = attachment_summary(state["images"], state["files"], state["failed"])
    display = f"[合并转发 {state['messages']} 条" + (f"，{received}" if received else "")
    omitted = list(dict.fromkeys(state["omitted"]))
    display += (f"；未完整展开：{'、'.join(omitted)}]" if omitted else "]")
    note = document_link_note(text)
    text += ("\n\n" + note if note else "")
    return EnrichedIncomingMessage(text, enriched_event, display, text)


def _download_incoming_media(
    message_id: str,
    image_keys: list[str],
    file_keys: list[str],
    file_names: dict[str, str],
    media_dir: Path,
    profile: str | None,
) -> list[IncomingMedia]:
    if not message_id:
        return []
    # Images and files are numbered apart.
    return [downloaded for specs in (resource_specs(image_keys, []), resource_specs([], file_keys, file_names))
            for *_, downloaded in fetch_resources(specs, [message_id], media_dir=media_dir, profile=profile,
                                                  downloader=message_read.download_resource)]


def _attachment_lines(
    media: list[IncomingMedia],
    successful: list[IncomingMedia],
    failed: list[IncomingMedia],
    file_names: dict[str, str],
) -> list[str]:
    numbers = {item.key: sum(1 for other in media[:i + 1] if other.kind == item.kind) for i, item in enumerate(media)}

    def label(item):
        name = file_names.get(item.key, "")
        return ("图片" if item.kind == "image" else "文件") + f" {numbers[item.key]}" + (f"（{name}）" if name else "")
    lines: list[str] = []
    if successful:
        # Downloaded is all the bridge knows; whether the model read a file shows in its answer.
        lines.append(ATTACHMENTS_HEADER)
        for item in successful:
            lines.append(attachment_line(item.kind, numbers[item.key], item.local_path, file_names.get(item.key, "")))
        lines.append(ATTACHMENTS_HINT)
    if failed:
        lines.append(FAILED_HEADER)
        for item in failed:
            lines.append(f"- {label(item)}: {(item.error or 'unknown error')[-200:]}")
    return lines


def _reply_context_lines(
    enriched_event: dict[str, Any],
    message_id: str,
    *,
    media_dir: Path,
    profile: str | None,
    fetch_message_details: bool,
    fetcher: Any,
    owner_id: str = "",
) -> tuple[list[str], str]:
    """Mentions and the replied-to message, for a message that is not a command; and the quote:
    the replied-to message when it is not the topic's first message, which every reply in a topic
    names as its parent (an answer or a held note sends the model the quote, not the topic root)."""
    lines: list[str] = []
    quote = ""
    mentions_text = render_mentions(enriched_event.get("mentions"))
    if mentions_text:
        lines.append(mentions_text)
    parent_id = str(enriched_event.get("parent_id") or "").strip()
    if parent_id and parent_id != message_id:
        # The topic's first message (render_replied_message_context's topic_root) does not change.
        topic_root = (bool(str(enriched_event.get("thread_id") or "").strip())
                      and str(enriched_event.get("root_id") or "").strip() == parent_id)
        parent_detail = _saved_root(media_dir, parent_id) if fetch_message_details and topic_root else None
        if parent_detail is not None:
            timing.note(parent="saved")
        else:
            parent_detail = (fetch_detail_or_error(fetcher, parent_id, profile) if fetch_message_details
                             else {"ok": False, "error": "message detail fetch disabled"})
            timing.note(parent="fetched" if fetch_message_details else "skipped")
            if fetch_message_details and topic_root:
                _save_root(media_dir, parent_id, parent_detail)
        reply_context = render_replied_message_context(
            enriched_event,
            parent_detail,
            message_detail_fetcher=fetcher,
            media_dir=media_dir,
            profile=profile,
            owner_id=owner_id,
        )
        if reply_context:
            lines.append(reply_context)
            quote = "" if topic_root else reply_context
    return lines, quote


def enrich_incoming_event(
    event: dict[str, Any],
    media_dir: Path,
    profile: str | None = None,
    message_detail: dict[str, Any] | None = None,
    *,
    fetch_message_details: bool = False,
    message_detail_fetcher: Any = fetch_message_detail,
    owner_id: str = "",
    hidden_mentions=(),
) -> EnrichedIncomingMessage:
    """Build backend prompt text from a Lark event, downloading image resources when possible.

    In a group topic ``owner_id`` labels who sent quoted and forwarded messages, and the
    mentions naming one of ``hidden_mentions`` (this bot) are left out, also when the
    message detail fetched here lists them again."""
    enriched_event = dict(event)
    if not enriched_event.get("parent_id") and enriched_event.get("reply_to"):
        enriched_event["parent_id"] = enriched_event["reply_to"]  # lark-cli's name for it.
    raw_content = str(event.get("content") or "").strip()
    message_id = str(event.get("message_id") or event.get("id") or "").strip()
    message_type = _current_message_type(enriched_event, message_id, str(event.get("message_type") or "").strip(),
                                         message_detail, profile, fetch_message_details, message_detail_fetcher)
    if hidden_mentions and isinstance(enriched_event.get("mentions"), list):
        enriched_event["mentions"] = drop_mentions(enriched_event["mentions"], hidden_mentions)

    if message_type == MERGE_FORWARD_TYPE:
        detail = message_detail if message_detail is not None else fetch_detail_or_error(message_detail_fetcher, message_id, profile)
        return _merge_forward_message(detail, enriched_event, message_id, media_dir=media_dir, profile=profile,
                                      fetcher=message_detail_fetcher, owner_id=owner_id)

    texts, image_keys, file_keys, file_names = collect_message_parts_named(raw_content)

    if not texts and message_type == "text" and raw_content:
        texts = text_message_texts(raw_content, plain=True)

    media = _download_incoming_media(message_id, image_keys, file_keys, file_names, media_dir, profile)

    lines: list[str] = []
    if texts:
        lines.append("\n".join(texts).strip())
    elif raw_content and not image_keys and not file_keys:
        lines.append(raw_content)

    successful = [item for item in media if item.local_path]
    failed = [item for item in media if item.error]
    attachment_lines = _attachment_lines(media, successful, failed, file_names)
    lines.extend(attachment_lines)
    attachments = "\n".join(attachment_lines)

    primary_text = "\n".join(texts).strip()
    if not primary_text and raw_content and not image_keys and not file_keys:
        primary_text = raw_content
    context, quote = _reply_context_lines(enriched_event, message_id, media_dir=media_dir, profile=profile,
                                          fetch_message_details=fetch_message_details,
                                          fetcher=message_detail_fetcher, owner_id=owner_id)
    lines.extend(context)

    note = document_link_note("\n".join(lines))
    if note:
        lines.append(note)
    # An answer's or held note's model text: the user's wording plus its attachments and the quoted message.
    # The topic root and the mentions stay out (each answer would repeat them); the link note covers these.
    quote_note = document_link_note("\n".join((primary_text, attachments, quote)))
    attachments = "\n\n".join(part for part in (attachments, quote, quote_note) if part)
    received = attachment_summary(sum(1 for item in successful if item.kind == "image"),
                                  sum(1 for item in successful if item.kind == "file"), len(failed),
                                  [file_names[k] for k in file_keys if file_names.get(k)])
    if received:
        display_text = (primary_text + "\n" if primary_text else "") + "附件：" + received
    else:
        display_text = primary_text or "[消息]"
    return EnrichedIncomingMessage("\n\n".join(line for line in lines if line).strip(), enriched_event, display_text,
                                   attachments)
