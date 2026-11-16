"""The argv of the bot's ``im +messages-send`` / ``+messages-reply`` (message_argv), and the request
lark-cli makes for it, rebuilt from that argv.

The SDK transport (feishu/sdk.py) posts it in process; the argv stays the call's identity: its
receipt fingerprint, its timing op and its lark-cli fallback. Ported from lark-cli v1.0.98
(shortcuts/im/im_messages_send.go, im_messages_reply.go, helpers.go): the body is the one
lark-cli's ``--dry-run`` prints, byte for byte. Anything lark-cli itself would read, upload or
reject (``@file`` and ``-`` input, markdown image URLs, a malformed ID or key) is left to lark-cli.
"""
from __future__ import annotations

import json
import re

from covey.feishu import sdk
from covey.feishu.transport import lark_argv

# Go's RE2 \s is ASCII [\t\n\f\r ]; Python's is Unicode (it matches U+3000), so classes are spelt out.
_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_H2_TO_H6 = re.compile(r"^#{2,6} (.+)$", re.M)
_H1 = re.compile(r"^# (.+)$", re.M)
_HAS_H1_TO_H3 = re.compile(r"^#{1,3} ", re.M)
_CONSECUTIVE_HEADINGS = re.compile(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", re.M)
_TABLE_NO_GAP = re.compile(r"^([^|\n].*)\n(\|.+\|)", re.M)
_TABLE_AFTER = re.compile(r"((?:^\|.+\|[\t\f\r ]*\n?)+)", re.M)
_EXCESS_NEWLINES = re.compile(r"\n{3,}")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\t\n\f\r ]+)\)")
_REMOTE_IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\t\n\f\r ]+)\)")
_SURROGATE = re.compile("[\ud800-\udfff]")
_CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]+")
_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]+")
_VALUE_FLAGS = {"--as", "--chat-id", "--message-id", "--msg-type", "--content", "--text", "--markdown",
                "--idempotency-key", "--format"}
# (chat_id, message_id) -> whether a reply to that message stays at the chat's top level, quoting it, instead of
# opening or joining its thread: the butler's (app/butler.py). None, as without COVEY_BUTLER:
# every reply is in its thread.
_top_level = None


def configure_top_level(predicate):
    """Set the process's top-level predicate (None: none); the previous one, to restore."""
    global _top_level
    previous, _top_level = _top_level, predicate
    return previous


def reply_in_thread(chat_id, reply_to) -> bool:
    """Whether a reply to ``reply_to`` goes in that message's thread: all do but the butler's."""
    return _top_level is None or not _top_level(chat_id, reply_to)


def message_argv(profile, *, chat_id="", user_id="", reply_to="", in_thread=True, flag, value, key):
    """The lark-cli argv of one message the bot writes under the idempotency ``key`` (already safe): ``flag``
    ``value`` its content (``--markdown`` or ``--text`` and the text; ``--content`` and a card's JSON, an
    interactive message), sent to ``chat_id`` or in reply to ``reply_to``, in its thread (a quote at the chat's
    top level instead when ``in_thread`` is False or configure_top_level's predicate says so: the butler's
    replies). ``user_id``: to that user, the owner's first private message (app/bot_menu.py), with its flags in
    the order it always had.
    The argv is the write's receipt fingerprint, shared with task runners of older releases: never reorder."""
    content = ["--msg-type", "interactive", flag, value] if flag == "--content" else [flag, value]
    tail = ["--idempotency-key", key, "--format", "json"]
    if user_id:
        return lark_argv(profile, "im", "+messages-send", "--user-id", user_id, *content, "--as", "bot", *tail)
    thread = ["--reply-in-thread"] if in_thread and reply_in_thread(chat_id, reply_to) else []
    target = ["--message-id", reply_to, *thread] if reply_to else ["--chat-id", chat_id]
    return lark_argv(profile, "im", "+messages-reply" if reply_to else "+messages-send", "--as", "bot", *target,
                     *content, *tail)


def go_json(value: str) -> str:
    """Go's ``json.Marshal`` of a string: HTML characters and U+2028/U+2029 escaped, and what
    is not valid UTF-8 (here a lone surrogate) as U+FFFD."""
    text = json.dumps(_SURROGATE.sub("\ufffd", value), ensure_ascii=False)
    for char, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                         ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(char, escape)
    return text


def optimize_markdown(text: str, *, step_headings: bool = True) -> str:
    """lark-cli's optimizeMarkdownStyle: headings stepped down, spacing around headings and
    tables, code blocks untouched, image references other than ``img_`` keys removed.
    ``step_headings=False``: headings as they are (an answer card, whose # and ## show bold)."""
    blocks: list[str] = []

    def hold(match):
        blocks.append(match.group(0))
        return f"___CB_{len(blocks) - 1}___"

    out = _CODE_BLOCK.sub(hold, text)
    if step_headings and _HAS_H1_TO_H3.search(text):
        out = _H2_TO_H6.sub(r"##### \1", out)
        out = _H1.sub(r"#### \1", out)
    out = _CONSECUTIVE_HEADINGS.sub("\\1\n\n\\2", out)
    out = _TABLE_NO_GAP.sub("\\1\n\n\\2", out)
    out = _TABLE_AFTER.sub("\\1\n", out)
    for index, block in enumerate(blocks):
        out = out.replace(f"___CB_{index}___", block, 1)
    out = _EXCESS_NEWLINES.sub("\n\n", out)
    if "![" in out:
        def keep_key(match):
            image = match.group(0)
            start, end = image.rfind("("), image.rfind(")")
            return image if start >= 0 and end > start and image[start + 1:end].startswith("img_") else ""
        out = _IMAGE.sub(keep_key, out)
    return out


def from_argv(argv):
    """``(profile, method, path, body, params)`` of the request ``argv`` makes; None when only
    lark-cli can make it. Only the flags the Bridge passes are understood."""
    argv = list(argv)
    if argv[:2] != ["lark-cli", "--profile"] or len(argv) < 5 or argv[3] != "im":
        return None
    shortcut, flags, rest = argv[4], {}, argv[5:]
    while rest:
        name = rest.pop(0)
        if name == "--reply-in-thread" and name not in flags:
            flags[name] = True
        elif name in _VALUE_FLAGS and name not in flags and rest:
            flags[name] = rest.pop(0)
        else:
            return None
    key = flags.get("--idempotency-key", "")
    sources = [name for name in ("--text", "--markdown", "--content") if name in flags]
    if (flags.get("--as") != "bot" or flags.get("--format") != "json" or not 0 < len(key) <= 50
            or len(sources) != 1):
        return None
    value = flags[sources[0]]
    if not value or value == "-" or value.startswith("@"):  # Empty, stdin or @file: lark-cli's own.
        return None
    if sources[0] == "--content":
        if flags.get("--msg-type") != "interactive":
            return None
        try:
            json.loads(value)
        except ValueError:
            return None
        msg_type, content = "interactive", value
    elif "--msg-type" in flags:
        return None
    elif sources[0] == "--text":
        # Go escapes "<", so lark-cli's normalizeAtMentions never matches what it built here.
        msg_type, content = "text", '{"text":' + go_json(value) + "}"
    elif _REMOTE_IMAGE.search(value):
        return None  # lark-cli downloads and uploads these images first.
    else:
        msg_type = "post"
        content = '{"zh_cn":{"content":[[{"tag":"md","text":' + go_json(optimize_markdown(value)) + "}]]}}"
    body = {"content": content, "msg_type": msg_type}
    if shortcut == "+messages-send" and "--message-id" not in flags and "--reply-in-thread" not in flags:
        if not _CHAT_ID.fullmatch(flags.get("--chat-id", "")):
            return None
        path, params = "/open-apis/im/v1/messages", {"receive_id_type": "chat_id"}
        body["receive_id"] = flags["--chat-id"]
    elif shortcut == "+messages-reply" and "--chat-id" not in flags:
        if not _MESSAGE_ID.fullmatch(flags.get("--message-id", "")):
            return None
        path, params = f"/open-apis/im/v1/messages/{flags['--message-id']}/reply", None
        if flags.get("--reply-in-thread"):
            body["reply_in_thread"] = True
    else:
        return None
    body["uuid"] = key
    return argv[2], "POST", path, body, params


def runner(argv, log):
    """The in-process request for this lark-cli message ``argv``; None to spawn lark-cli."""
    request = from_argv(argv) if sdk.enabled() else None
    return request and sdk.runner_or_none(*request, log=log, label="Message", cli_errors=True)


def read_runner(profile, message_id, params):
    """The in-process ``GET /open-apis/im/v1/messages/<id>``; None to spawn lark-cli."""
    if not sdk.enabled() or not _MESSAGE_ID.fullmatch(message_id):
        return None
    return sdk.runner_or_none(profile, "GET", f"/open-apis/im/v1/messages/{message_id}", None, params,
                              label="Message", cli_errors=True)
