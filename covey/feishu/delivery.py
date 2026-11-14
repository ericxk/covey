#!/usr/bin/env python3
"""Shared Feishu message delivery helpers for bridge scripts."""

from __future__ import annotations

import functools
import hashlib
import json
import re
import subprocess
from collections.abc import Callable

from covey.feishu import message_request
from covey.feishu.message_request import message_argv
from covey.feishu.transport import call, last_failure, message_id_of, tried

# A card refused with these is not refused for what it shows: Markdown would fare no better (authentication),
# or the key's earlier try must be settled first (rule 1), so the answer stays on the card's route.
CARD_KEPT = {"authentication", "idempotency_conflict", "reconciliation_required"}


def safe_idempotency_key(value: str, fallback: str = "lark-bridge", digest_prefix: str | None = None) -> str:
    # Feishu message UUIDs are capped at 50 chars. Keep long bridge-generated
    # keys deterministic while avoiding API validation failures.
    key = re.sub(r"[^A-Za-z0-9_-]", "-", value) or fallback
    if len(key) <= 50:
        return key
    prefix = fallback[:2] if digest_prefix is None else digest_prefix
    return f"{prefix}-" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:24]


def split_text(text: str, limit: int) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < max(1, limit // 2):
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    chunks.append(remaining)
    return chunks


# What Feishu's DLP audit refuses in a message (transport.AUDIT_REJECTED), by the word its error names:
# an email keeps its domain (``…@example.com``), a mainland mobile number its first three and last four digits.
SENSITIVE = {
    "EMAIL": (re.compile(r"[A-Za-z0-9._%+-]+@(?=[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"), lambda m: "…@"),
    "PHONE": (re.compile(r"(?<![\d.])(?:\+?86[- ]?)?(1[3-9]\d)\d{4}(\d{4})(?![\d.])"), lambda m: f"{m[1]}****{m[2]}"),
}
MASKED_NOTE = "（飞书审核拒收了原文里的明文邮箱或手机号，这里已打码；原文保存在本地任务记录）"


def mask_sensitive(text: str, detail: str = "") -> str:
    """``text`` with what the audit's ``detail`` names masked; every kind when it names none of them."""
    named = [kind for kind in SENSITIVE if kind in detail.upper()] or list(SENSITIVE)
    for kind in named:
        pattern, masked = SENSITIVE[kind]
        text = pattern.sub(masked, text)
    return text


def resolve_message_format(default_format: str, valid_formats: set[str], fallback: str = "markdown") -> str:
    fmt = (default_format or fallback).lower()
    return fmt if fmt in valid_formats else fallback


def _learn(payload, on_sent) -> None:
    """Hand ``on_sent`` the message ID in Feishu's answer to a send, when it names one."""
    message_id = message_id_of(payload)
    if on_sent is not None and message_id:
        on_sent(message_id)


def _send(argv, key, log):
    """One message write under its idempotency ``key``: Feishu's answer, None when it was not sent (the
    reason in transport.last_failure). The SDK posts it when it can; lark-cli, as this module runs it, if not."""
    return call(argv, timeout=30, log=log, runner=message_request.runner(argv, log) or subprocess.run, uuid=key)


def send_message_chunk(
    *,
    profile: str | None,
    chat_id: str,
    text: str,
    idempotency_key: str,
    message_format: str,
    log: Callable[[str], None],
    fallback_key_prefix: str,
    reply_to_message_id: str = "",
    on_sent: Callable[[str], None] | None = None,
) -> bool:
    """One chunk; ``on_sent`` learns its message ID once it is sent."""
    if text.startswith("@") or text == "-":
        # lark-cli reads "@path" as a file to send and "-" as stdin: a reply that starts like that
        # (a model's "@张三 …", or a path) is text. A zero-width space keeps it literal on either transport.
        text = "\u200b" + text
    key = safe_idempotency_key(idempotency_key, fallback=fallback_key_prefix)
    payload = _send(message_argv(profile, chat_id=chat_id, reply_to=reply_to_message_id, key=key, value=text,
                                 flag="--markdown" if message_format == "markdown" else "--text"), key, log)
    if payload is None:
        return False
    _learn(payload, on_sent)
    log(f"sent {message_format} reply to {chat_id}")
    return True


def refused_outright(failure) -> bool:
    """Whether Feishu turned a card down for good: for its content, its size or its audit, not a try that may
    have landed or may pass later."""
    return (failure is not None and not failure.ambiguous and not failure.retryable
            and failure.category not in CARD_KEPT)


def first_chunk_key(idempotency_key: str, total: int) -> str:
    """The key of the first of ``total`` Markdown chunks a text goes as (send_message_with_fallback)."""
    return idempotency_key if total == 1 else f"{idempotency_key}-1-of-{total}"


def card_tried(*, text: str, idempotency_key: str, chunk_limit: int, fallback_key_prefix: str) -> bool:
    """Whether ``text`` with a card under ``idempotency_key`` (send_card_first) would go as a card under a key a
    write was already tried with (its receipt holds that try's content, so another card under it is refused as
    ``idempotency_conflict``); False when the Markdown route was tried for it instead (the same text again)."""
    first = first_chunk_key(idempotency_key, len(split_text(text, chunk_limit)))
    if tried(safe_idempotency_key(first, fallback=fallback_key_prefix)):
        return False
    return tried(safe_idempotency_key(f"{idempotency_key}-card", fallback=fallback_key_prefix))


def send_card_first(
    *,
    profile: str | None,
    chat_id: str,
    card: dict,
    idempotency_key: str,
    first_chunk_key: str,
    log: Callable[[str], None],
    fallback_key_prefix: str,
    reply_to_message_id: str = "",
    on_sent: Callable[[str], None] | None = None,
    on_card: Callable[[str], None] | None = None,
) -> bool | None:
    """``card`` as a card message under its own key (``idempotency_key``-card): True once it is sent, False
    when it may have landed or may pass later, None when the text goes as Markdown instead. That is when
    Feishu refused the card outright, or the Markdown route was already tried for this text
    (``first_chunk_key`` has a receipt: a release before answer cards, or an earlier refusal), so one text
    never takes both routes while either may have landed. ``on_sent`` learns the card's message ID once it is sent."""
    if tried(safe_idempotency_key(first_chunk_key, fallback=fallback_key_prefix)):
        return None
    key = safe_idempotency_key(f"{idempotency_key}-card", fallback=fallback_key_prefix)
    payload = _send(message_argv(profile, chat_id=chat_id, reply_to=reply_to_message_id, key=key, flag="--content",
                                 value=json.dumps(card, ensure_ascii=False)), key, log)
    if payload is not None:
        log(f"sent card reply to {chat_id}")
        _learn(payload, on_sent)
        _learn(payload, on_card)
        return True
    if not refused_outright(last_failure()):
        return False
    log(f"card reply refused category={last_failure().category}; sending it as Markdown")
    return None


def send_message_with_fallback(
    *,
    profile: str | None,
    chat_id: str,
    text: str,
    idempotency_key: str,
    message_format: str,
    chunk_limit: int,
    fallback_chunk_limit: int,
    log: Callable[[str], None],
    fallback_key_prefix: str,
    reply_to_message_id: str = "",
    card: dict | None = None,
    on_sent: Callable[[str], None] | None = None,
    on_card: Callable[[str], None] | None = None,
) -> bool:
    """``text`` in chunks of ``chunk_limit``, each falling back to plain text and to smaller chunks when its
    content is refused. ``card``: the same text as a card, sent first (send_card_first) in markdown format.
    ``on_sent`` learns the ID of each message sent."""
    chunks = split_text(text, chunk_limit)
    total = len(chunks)
    route = dict(profile=profile, chat_id=chat_id, log=log, fallback_key_prefix=fallback_key_prefix,
                 reply_to_message_id=reply_to_message_id, on_sent=on_sent)
    if card is not None and message_format == "markdown":
        sent = send_card_first(card=card, idempotency_key=idempotency_key,
                               first_chunk_key=first_chunk_key(idempotency_key, total),
                               on_card=on_card, **route)
        if sent is not None:
            return sent
    send_chunk = functools.partial(send_message_chunk, **route)
    all_sent = True
    for idx, chunk in enumerate(chunks, 1):
        key = idempotency_key if total == 1 else f"{idempotency_key}-{idx}-of-{total}"
        chunk_sent = send_chunk(text=chunk, idempotency_key=key, message_format=message_format)
        refused = last_failure() if not chunk_sent else None
        if refused is not None and refused.category == "audit_rejected":
            masked = mask_sensitive(chunk, refused.detail)
            if masked != chunk:
                # Once more, masked, under its own key; the fallbacks below send the masked text too.
                chunk, key = f"{masked}\n\n{MASKED_NOTE}", f"{key}-masked"
                log(f"message chunk {idx}/{total} refused by Feishu's audit; sending it masked")
                chunk_sent = send_chunk(text=chunk, idempotency_key=key, message_format=message_format)
        content_failure = lambda: last_failure() is not None and last_failure().category == "content"
        if not chunk_sent and message_format == "markdown" and content_failure():
            chunk_sent = send_chunk(text=chunk, idempotency_key=f"{key}-text", message_format="text")
        if not chunk_sent and len(chunk) > fallback_chunk_limit and content_failure():
            fallback_chunks = split_text(chunk, fallback_chunk_limit)
            fallback_total = len(fallback_chunks)
            for fallback_idx, fallback_chunk in enumerate(fallback_chunks, 1):
                chunk_sent = send_chunk(text=fallback_chunk, message_format="text",
                                        idempotency_key=f"{key}-fb-{fallback_idx}-of-{fallback_total}")
                if not chunk_sent:
                    break  # A later piece's success would clear this failure's classification.
        if not chunk_sent:
            all_sent = False
            log(f"failed to send message chunk {idx}/{total} to {chat_id}")
            break  # Preserve failure classification and chunk ordering for retry.
    return all_sent
