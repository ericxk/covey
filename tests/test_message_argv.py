"""Golden argv of every message the Bridge writes to Feishu through lark-cli.

A write's argv is its identity: transport.receipt keys the receipt under <state>/delivery-receipts on the
argv's fingerprint, and task runners of an older release share those receipts, so a key tried with other
argv fails closed as idempotency_conflict. Each list below is the argv as the Bridge builds it today, element
by element: text and Markdown (send, reply in thread, chunks, audit masking, plain-text and smaller-piece
fallbacks), answer cards sent first, card messages and CardKit creates (feishu/cardkit.py), the owner's first
private message (app/bot_menu.py), file replies and thread forwards (``api POST``) and the login QR image
(app/proxy_send.py). The SDK transport is off: every write goes to lark-cli, here a fake."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import requires_extension
from covey.app.bot_menu import open_owner_chat
from covey.feishu import transport
from covey.feishu.delivery import send_message_with_fallback
from covey.feishu.messenger import Messenger, Voice

P = "named"
VOICE = Voice("cx", "GOLDEN_ARGV")
OK = {"ok": True, "data": {"message_id": "om_new", "chat_id": "oc_dm"}}
CREATED = {"code": 0, "data": {"card_id": "7card"}}
CONTENT = {"ok": False, "error": {"type": "api", "subtype": "invalid_content", "message": "content invalid"}}
AUDIT = {"ok": False, "error": {"type": "api", "code": 230028, "message": "contain sensitive data: EMAIL_ADDRESS"}}
AUDIT_PHONE = {"ok": False, "error": {"type": "api", "code": 230028, "message": "contain sensitive data: PHONE_NUMBER"}}
CARD = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答：<b>好</b>"}]}}
LONG = "cx-reply-om_" + "x" * 48  # Past Feishu's 50 characters: sent under a digest.


class FakeCli:
    """``subprocess.run`` for lark-cli: records each argv; ``refuse`` maps an idempotency key to the error
    payload its write gets, every other write succeeds."""

    def __init__(self):
        self.argvs, self.refuse = [], {}

    def __call__(self, argv, **_):
        self.argvs.append(list(argv))
        key = argv[argv.index("--idempotency-key") + 1] if "--idempotency-key" in argv else ""
        payload = CREATED if "/open-apis/cardkit/v1/cards" in argv else self.refuse.get(key, OK)
        ok = payload.get("ok", True)
        return subprocess.CompletedProcess(argv, 0 if ok else 1, json.dumps(payload), "")


@contextlib.contextmanager
def recording():
    """A FakeCli in place of lark-cli, receipts in a fresh directory, the SDK transport off."""
    cli = FakeCli()
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ), \
            mock.patch("subprocess.run", cli), mock.patch.object(transport.time, "sleep"):
        os.environ.pop("COVEY_CARD_TRANSPORT", None)
        previous = transport.configure_receipts(tmp)
        try:
            cli.tmp = Path(tmp)
            yield cli
        finally:
            transport.configure_receipts(previous)
            transport.set_failure(None)


def send(text, key, *, fmt="markdown", reply_to="", card=None, chunk_limit=12000, fallback_limit=3000, profile=P):
    return send_message_with_fallback(profile=profile, chat_id="oc_1", text=text, idempotency_key=key,
                                      message_format=fmt, chunk_limit=chunk_limit, fallback_chunk_limit=fallback_limit,
                                      log=lambda _: None, fallback_key_prefix="cx", reply_to_message_id=reply_to,
                                      card=card)


def text_messages(cli):
    send("**答**：好", "cx-reply-om_1")
    send("纯文本", "cx-reply-om_2", fmt="text", reply_to="om_root")
    send("@张三 看一下", "cx-reply-om_3", profile=None)
    send("-", "cx-reply-om_4", fmt="text")
    send("第一段\n第二段", "cx-reply-om_5", chunk_limit=4)
    send("长键", LONG, reply_to="om_root")
    send("怪键", "cx reply/om_6:é")


def messenger_text(cli):
    with mock.patch.dict(os.environ, {"GOLDEN_ARGV_MESSAGE_FORMAT": "text"}):
        Messenger(lambda _: None).send_message(P, "oc_1", "好", "cx-final-om_7", voice=VOICE)
    Messenger(lambda _: None).reply(SimpleNamespace(profile=P, reply_to_message_id="om_root"), "oc_1", "答",
                                    "cx-final-om_8", voice=VOICE)


def answer_cards(cli):
    send("答：好", "cx-answer-om_1", card=CARD)
    send("答：好", "cx-answer-om_2", card=CARD, reply_to="om_root")
    send("答：好", LONG, card=CARD)
    cli.refuse["cx-answer-om_3-card"] = CONTENT  # Refused outright: the text goes as Markdown.
    send("一\n二", "cx-answer-om_3", card=CARD, chunk_limit=2, reply_to="om_root")
    send("答：好", "cx-answer-om_4", card=CARD, fmt="text")  # Text format: no card.


def fallbacks(cli):
    cli.refuse["cx-reply-om_1"] = AUDIT
    send("写信到 a.b@example.com 吧", "cx-reply-om_1", reply_to="om_root")
    cli.refuse.update({"cx-reply-om_2": CONTENT, "cx-reply-om_2-text": CONTENT})
    send("甲甲甲甲\n乙乙乙乙\n丙丙", "cx-reply-om_2", fallback_limit=5)
    cli.refuse.update({"cx-reply-om_3": AUDIT_PHONE, "cx-reply-om_3-masked": CONTENT})
    send("电话 13912345678", "cx-reply-om_3", reply_to="om_root")  # Masked, then refused as Markdown too.


def card_messages(cli):
    messenger = Messenger(lambda _: None)
    messenger.send_card(P, "oc_1", "7abc", "cx-stream-card-om_1-1", voice=VOICE, reply_to_message_id="om_root")
    messenger.send_card(P, "oc_1", "7abc", "cx-stream-card-om_2-1", voice=VOICE)
    messenger.send_card(None, "oc_1", "7abc", LONG, voice=VOICE, reply_to_message_id="om_root")
    messenger.create_and_send_card(P, "oc_1", CARD, "cx-browse-ev_1", voice=VOICE, reply_to_message_id="om_root")


def owner_first_message(cli):
    assert open_owner_chat(P, "ou_owner", "menu-first-ev_1", lambda _: None) == "oc_dm"
    open_owner_chat(None, "ou_owner", LONG, lambda _: None)


def api_messages(cli):
    messenger = Messenger(lambda _: None)
    messenger.cardkit(P, "POST", "/open-apis/im/v1/messages/om_root/reply", {
        "msg_type": "file", "content": json.dumps({"file_key": "file_v3_k"}), "reply_in_thread": True,
        "uuid": VOICE.safe_key("artifact-" + "a" * 64)})
    messenger.cardkit(P, "POST", "/open-apis/im/v1/threads/omt_1/forward", {"receive_id": "oc_2"},
                      params={"receive_id_type": "chat_id", "uuid": "fork-tok"})
    messenger.cardkit(P, "POST", "/open-apis/im/v1/messages/merge_forward",
                      {"receive_id": "oc_2", "message_id_list": ["om_a", "om_b"]},
                      params={"receive_id_type": "chat_id", "uuid": "fork-merge0-tok"})


def login_qr(cli):
    from covey_internal.proxy_login import ProxyLogin  # The company's sender (hook proxy_sender).
    home = cli.tmp / "home"
    folder = cli.tmp / "tmp"
    for path in (home, folder):
        path.mkdir()
    (home / "qr.png").write_bytes(b"png")
    login = ProxyLogin(SimpleNamespace(runtime=SimpleNamespace(log=lambda _: None), messenger=Messenger(lambda _: None)))
    login.login_line(SimpleNamespace(profile=P), {"send_home": str(home)}, folder, "om_panel",
                     json.dumps({"event": "qr_login", "qr_path": str(home / "qr.png")}))
    # The copy's name has the time in it: the argv's last element is checked by its shape only.
    name = cli.argvs[-1][-1]
    assert name.startswith("login-") and name.endswith(".png"), name
    cli.argvs[-1][-1] = "login-<ns>.png"


# Scenario -> the argv of each lark-cli call it made, in order.
GOLDEN = {
    "text_messages": [
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown",
         "**答**：好", "--idempotency-key", "cx-reply-om_1", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--text", "纯文本", "--idempotency-key", "cx-reply-om_2", "--format", "json"],
        ["lark-cli", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown", "\u200b@张三 看一下",
         "--idempotency-key", "cx-reply-om_3", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text",
         "\u200b-", "--idempotency-key", "cx-reply-om_4", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown",
         "第一段", "--idempotency-key", "cx-reply-om_5-1-of-2", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown",
         "第二段", "--idempotency-key", "cx-reply-om_5-2-of-2", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "长键", "--idempotency-key", "cx-e9ee083bb2f604773ef4b48c", "--format",
         "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown",
         "怪键", "--idempotency-key", "cx-reply-om_6--", "--format", "json"],
    ],
    "messenger_text": [
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text", "好",
         "--idempotency-key", "cx-final-om_7", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "答", "--idempotency-key", "cx-final-om_8", "--format", "json"],
    ],
    "answer_cards": [
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--msg-type",
         "interactive", "--content",
         '{"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答：<b>好</b>"}]}}',
         "--idempotency-key", "cx-answer-om_1-card", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--msg-type", "interactive", "--content",
         '{"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答：<b>好</b>"}]}}',
         "--idempotency-key", "cx-answer-om_2-card", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--msg-type",
         "interactive", "--content",
         '{"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答：<b>好</b>"}]}}',
         "--idempotency-key", "cx-de97486f35af66895d61993d", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--msg-type", "interactive", "--content",
         '{"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答：<b>好</b>"}]}}',
         "--idempotency-key", "cx-answer-om_3-card", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "一", "--idempotency-key", "cx-answer-om_3-1-of-2", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "二", "--idempotency-key", "cx-answer-om_3-2-of-2", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text",
         "答：好", "--idempotency-key", "cx-answer-om_4", "--format", "json"],
    ],
    "fallbacks": [
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "写信到 a.b@example.com 吧", "--idempotency-key", "cx-reply-om_1", "--format",
         "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "写信到 …@example.com 吧\n\n（飞书审核拒收了原文里的明文邮箱或手机号，这里已打码；原文保存在本地任务记录）",
         "--idempotency-key", "cx-reply-om_1-masked", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown",
         "甲甲甲甲\n乙乙乙乙\n丙丙", "--idempotency-key", "cx-reply-om_2", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text",
         "甲甲甲甲\n乙乙乙乙\n丙丙", "--idempotency-key", "cx-reply-om_2-text", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text",
         "甲甲甲甲", "--idempotency-key", "cx-reply-om_2-fb-1-of-3", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text",
         "乙乙乙乙", "--idempotency-key", "cx-reply-om_2-fb-2-of-3", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text", "丙丙",
         "--idempotency-key", "cx-reply-om_2-fb-3-of-3", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "电话 13912345678", "--idempotency-key", "cx-reply-om_3", "--format",
         "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--markdown", "电话 139****5678\n\n（飞书审核拒收了原文里的明文邮箱或手机号，这里已打码；原文保存在本地任务记录）",
         "--idempotency-key", "cx-reply-om_3-masked", "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--text", "电话 139****5678\n\n（飞书审核拒收了原文里的明文邮箱或手机号，这里已打码；原文保存在本地任务记录）",
         "--idempotency-key", "cx-reply-om_3-masked-text", "--format", "json"],
    ],
    "card_messages": [
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--msg-type", "interactive", "--content",
         '{"type": "card", "data": {"card_id": "7abc"}}', "--idempotency-key", "cx-stream-card-om_1-1", "--format",
         "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--msg-type",
         "interactive", "--content", '{"type": "card", "data": {"card_id": "7abc"}}', "--idempotency-key",
         "cx-stream-card-om_2-1", "--format", "json"],
        ["lark-cli", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root", "--reply-in-thread",
         "--msg-type", "interactive", "--content", '{"type": "card", "data": {"card_id": "7abc"}}',
         "--idempotency-key", "cx-e9ee083bb2f604773ef4b48c", "--format", "json"],
        ["lark-cli", "--profile", "named", "api", "POST", "/open-apis/cardkit/v1/cards", "--as", "bot", "--data",
         '{"type": "card_json", "data": "{\\"schema\\": \\"2.0\\", \\"body\\": {\\"elements\\": [{\\"tag\\": \\"markdown\\", \\"content\\": \\"答：<b>好</b>\\"}]}}"}',
         "--format", "json"],
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_root",
         "--reply-in-thread", "--msg-type", "interactive", "--content",
         '{"type": "card", "data": {"card_id": "7card"}}', "--idempotency-key", "cx-browse-ev_1", "--format", "json"],
    ],
    "owner_first_message": [
        ["lark-cli", "--profile", "named", "im", "+messages-send", "--user-id", "ou_owner", "--text",
         "菜单的结果都发在这个私聊里。", "--as", "bot", "--idempotency-key", "menu-first-ev_1", "--format", "json"],
        ["lark-cli", "im", "+messages-send", "--user-id", "ou_owner", "--text", "菜单的结果都发在这个私聊里。", "--as", "bot",
         "--idempotency-key", "cx-reply-om_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "--format", "json"],
    ],
    "api_messages": [
        ["lark-cli", "--profile", "named", "api", "POST", "/open-apis/im/v1/messages/om_root/reply", "--as", "bot",
         "--data",
         '{"msg_type": "file", "content": "{\\"file_key\\": \\"file_v3_k\\"}", "reply_in_thread": true, "uuid": "cx-e55eee9fa55c27c829576f53"}',
         "--format", "json"],
        ["lark-cli", "--profile", "named", "api", "POST", "/open-apis/im/v1/threads/omt_1/forward", "--params",
         '{"receive_id_type": "chat_id", "uuid": "fork-tok"}', "--as", "bot", "--data", '{"receive_id": "oc_2"}',
         "--format", "json"],
        ["lark-cli", "--profile", "named", "api", "POST", "/open-apis/im/v1/messages/merge_forward", "--params",
         '{"receive_id_type": "chat_id", "uuid": "fork-merge0-tok"}', "--as", "bot", "--data",
         '{"receive_id": "oc_2", "message_id_list": ["om_a", "om_b"]}', "--format", "json"],
    ],
    "login_qr": [
        ["lark-cli", "--profile", "named", "im", "+messages-reply", "--as", "bot", "--message-id", "om_panel",
         "--image", "login-<ns>.png"],
    ],
}


class MessageArgvTests(unittest.TestCase):
    maxDiff = None

    def check(self, scenario):
        with recording() as cli:
            scenario(cli)
        self.assertEqual(cli.argvs, GOLDEN[scenario.__name__])

    def test_text_and_markdown_messages(self):
        self.check(text_messages)

    def test_messenger_text_messages(self):
        self.check(messenger_text)

    def test_answer_cards_sent_first(self):
        self.check(answer_cards)

    def test_audit_masking_and_content_fallbacks(self):
        self.check(fallbacks)

    def test_card_messages_and_cardkit_creates(self):
        self.check(card_messages)

    def test_the_owners_first_private_message(self):
        self.check(owner_first_message)

    def test_file_replies_and_thread_forwards(self):
        self.check(api_messages)

    @requires_extension()
    def test_the_login_qr_image(self):
        self.check(login_qr)


SCENARIOS = (text_messages, messenger_text, answer_cards, fallbacks, card_messages, owner_first_message,
             api_messages, login_qr)


if __name__ == "__main__":
    unittest.main()
