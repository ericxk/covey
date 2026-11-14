#!/usr/bin/env python3
"""Shared CardKit API helpers for Feishu bridge scripts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from covey.feishu import message_request, sdk
from covey.feishu.message_request import message_argv
from covey.feishu.delivery import safe_idempotency_key
from covey.feishu.transport import call, last_failure, lark_argv, message_id_of


def json_log_summary(value: Any, limit: int = 2000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _cardkit_api_once(
    profile: str,
    method: str,
    path: str,
    data: dict[str, Any],
    *,
    log: Callable[[str], None],
    timeout: int = 30,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    # Only message creation needs the durable UUID receipt; CardKit mutations
    # already target the same entity/sequence and must return their full payload.
    message_write = method == "POST" and path.startswith("/open-apis/im/v1/messages")
    execute = None
    if path.startswith('/open-apis/cardkit/'):
        execute = sdk.runner_or_none(profile, method, path, data, params, log=log)
    elif message_write:  # A file message: lark-cli's `api POST` would have sent this same body.
        execute = sdk.runner_or_none(profile, method, path, data, params, log=log, label='Message', cli_errors=True)
    return call(lark_argv(profile, "api", method, path,
                *(["--params", json.dumps(params, ensure_ascii=False)] if params else []),
                "--as", "bot", "--data", json.dumps(data, ensure_ascii=False), "--format", "json"),
                timeout=timeout, log=log, uuid=str(data.get("uuid") or "") if message_write else "",
                retry_safe=method == "GET" or bool(data.get("uuid")), runner=execute)


def cardkit_api(profile, method, path, data, *, log, timeout=30, params=None):
    if data.get("uuid"):
        data = {**data, "uuid": safe_idempotency_key(str(data["uuid"]))}
    return _cardkit_api_once(profile, method, path, data, log=log, timeout=timeout, params=params)


def extract_card_id(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("card_id", "cardId"):
            value = data.get(key)
            if value:
                return str(value)
        card = data.get("card")
        if isinstance(card, dict):
            for key in ("card_id", "cardId"):
                value = card.get(key)
                if value:
                    return str(value)
    for key in ("card_id", "cardId"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def send_card_message(
    profile: str,
    chat_id: str,
    card_id: str,
    key: str,
    *,
    reply_to_message_id: str = "",
    in_thread: bool = True,
    log: Callable[[str], None],
) -> tuple[str, bool]:
    """The card ``card_id`` as a message under ``key``, already safe (Voice.safe_key), in reply to
    ``reply_to_message_id`` in its thread (``in_thread`` False: quoted at the top level) or to ``chat_id``: its
    message ID ('' when not sent) and whether Feishu called the card invalid."""
    content = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
    argv = message_argv(profile, chat_id=chat_id, reply_to=reply_to_message_id, in_thread=in_thread, key=key,
                        flag="--content", value=content)
    payload = call(argv, timeout=30, log=log, uuid=key, runner=message_request.runner(argv, log))
    if payload is None:
        return "", bool(last_failure() and last_failure().category == 'invalid_card')
    message_id = message_id_of(payload)
    if not message_id:
        log(f"send card did not return message_id card_id={card_id} key={key}")
        return "", False
    log(f"sent streaming card to {chat_id} card_id={card_id} message_id={message_id}")
    return message_id, False
