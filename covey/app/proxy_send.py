"""Proxy replies (代答): Feishu as the owner sees it. A batch's saved and authorized reply is sent as the
owner by a sender, a module the extension hook ``proxy_sender`` (core/extensions.py) provides (``web_send``; the
company's is the sender tool, extensions/covey_internal/proxy_sender.py). Then read back as the owner with
lark-cli; the owner's chat is read to tell whether they answered a batch themselves. With no sender there is no
代答 entrance at all (``available``): no workers, no menu item, no tab. The browser (feishu/web_service.py) is
the acceptance page's alone.

Held by Proxy as ``web``. Whether a reply goes out at all, the batch's move to ``sending`` before it does,
and what becomes of it after (``finish_send``) are Proxy's (``send``, under Proxy.lock); here the sender runs,
outside every lock.
"""
from __future__ import annotations

import datetime
import re
import subprocess
import time

from covey.core import extensions
from covey.core import proxy as records
from covey.feishu.transport import CliFailed, cli_json, lark_argv

READ_BACKS = 4  # A sent reply the sender gave no om_ for: read back once a poll, on this many polls (repo review F5).
NO_SENDER = "没有代答发送器（没有启用提供它的扩展）"


def available() -> bool:
    """Whether this deployment has a sender: without one 代答 is not offered."""
    return extensions.hook("proxy_sender") is not None


# An at tag in any spelling (repo review F1: the tool makes ``<at user_id="all">…</at>`` a real @, as the owner):
# with its name, or bare.
AT_TAG = re.compile(r"<\s*at\b([^>]*)>(.*?)<\s*/\s*at\s*>|<\s*at\b([^>]*?)/\s*>", re.I | re.S)
QUOTED = re.compile(r"""=\s*["']?([^"'\s/>]+)""")


def plain_mentions(text):
    """``text`` with every at tag as the plain text it shows: ``<at user_id="all">所有人</at>`` is ``@所有人``,
    a bare ``<at user_id="all"/>`` ``@all``. A reply never mentions anyone: the tool would, as the owner. Until
    none is left, so a tag a removed one leaves behind goes too."""
    def plain(match):
        name = " ".join((match[2] or "").split()) or next(iter(QUOTED.findall(match[1] or match[3] or "")), "")
        return name if name.startswith("@") else "@" + name
    while True:
        text, count = AT_TAG.subn(plain, text)
        if not count:
            return text


def minute(milliseconds):
    """'YYYY-MM-DD HH:MM' (local) of the tool's create_time; '' when there is none."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(milliseconds) / 1000)) if milliseconds else ""
    except (TypeError, ValueError):
        return ""


def user_cli(profile, *args, timeout=60):
    """A lark-cli call as the owner (``profile``'s user login); its JSON, RuntimeError on failure."""
    try:
        return cli_json(lark_argv(profile, *args, "--as", "user", "--format", "json"), timeout=timeout,
                        runner=subprocess.run)
    except CliFailed as exc:
        raise RuntimeError(exc.reason(300) or f"lark-cli rc={exc.proc.returncode}") from None


def iso(seconds):
    """Local time as lark-cli's --start takes it: ISO 8601 with a +08:00 offset (not +0800)."""
    return datetime.datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def message_epoch(text):
    """Seconds of a create_time the shortcuts print (local 'YYYY-MM-DD HH:MM'); 0 when unreadable."""
    try:
        return time.mktime(time.strptime(str(text)[:16], "%Y-%m-%d %H:%M"))
    except ValueError:
        return 0


def clock(text):
    """HH:MM of a create_time the shortcuts print; now when it is unreadable."""
    return time.strftime("%H:%M", time.localtime(message_epoch(text) or time.time()))


class NullLogin:
    """The login side of a deployment without a sender: nothing to check, nothing to prune."""

    status_next = 0.0
    login_seen = None

    def __init__(self, proxy=None) -> None:
        self.proxy = proxy

    def check_login(self, ctx, settings):
        return None

    def login_gone(self, ctx):
        return None

    def login_back(self, ctx):
        return None

    def prune_web_caches(self, ctx):
        return None


class ProxySend:
    """Reading what the owner's chat shows, and the sender (``sender``, None without one) that sends as the owner."""

    def __init__(self, proxy, sender=None) -> None:
        self.proxy = proxy
        self.sender = sender
        self.reads = {}  # Batch id: read-backs left for a sent reply without its om_ (``read_backs``).

    def web_send(self, ctx, settings, contact, batch, reply):
        """``reply`` sent as the owner by the sender, its result ``finish_send``'s (docs/proxy.md)."""
        if self.sender is None:
            return {"ok": False, "submitted": False, "type": "launch", "error": NO_SENDER}
        return self.sender.web_send(ctx, settings, contact, batch, reply)

    def run_tool(self, settings, args, *, cwd, timeout, on_line=None):
        return self.sender.run_tool(settings, args, cwd=cwd, timeout=timeout, on_line=on_line)

    def problems(self, ctx, settings):
        """What keeps the sender from sending, for 代答 状态 and the 代答设置 card."""
        return [NO_SENDER] if self.sender is None else self.sender.problems(ctx, settings)

    def chat_messages(self, settings, contact, start, *, timeout):
        """The messages lark-cli lists of the owner's chat with ``contact`` from ``start`` on, as the owner
        (+chat-messages-list); RuntimeError when it fails. ProxyIntake.list_chat takes them in, ``replies`` reads back."""
        chat_id = contact.get("chat_id") or contact["target"]
        return records.search_messages(
            user_cli(settings["user_profile"], "im", "+chat-messages-list", "--chat-id", chat_id, "--start", iso(start),
                     "--page-size", "50", "--no-reactions", timeout=timeout))

    def recent_chat(self, settings, contact, limit=records.RECENT_MESSAGES):
        """The chat's newest ``limit`` messages as the owner, oldest first (core/proxy.py incoming), the owner's
        own among them, for a batch's prompt; [] when the read fails, and the prompt asks the model to read it."""
        chat_id = contact.get("chat_id") or contact["target"]
        try:
            items = records.search_messages(
                user_cli(settings["user_profile"], "im", "+chat-messages-list", "--chat-id", chat_id, "--order", "desc",
                         "--page-size", str(limit), "--no-reactions", timeout=30))
        except RuntimeError as exc:
            self.proxy.runtime.log(f"proxy recent chat unread contact={records.digest(contact['key'])[:8]}: {str(exc)[:200]}")
            return []
        return [records.incoming(item) for item in reversed(items)]

    def replies(self, settings, contact, start):
        """chat_messages as core/proxy.py incoming, each with the message it replies to (``reply_to``)."""
        return [records.incoming(item) | {"reply_to": str(item.get("reply_to") or "")}
                for item in self.chat_messages(settings, contact, start, timeout=60)]

    def read_back(self, settings, contact, batch, tries=4):
        """The owner's message carrying ``batch``'s reply (core/proxy.py incoming: its id, time…), {} when
        it does not show in ``tries`` reads 15 s apart. In a group it replies to the message sender replied to."""
        anchor = records.anchor(batch)["id"]
        for attempt in range(tries):
            if attempt and self.proxy.runtime.shutdown.wait(15):
                break
            try:
                messages = self.replies(settings, contact, float(batch["send_started_at"]) - 120)
            except RuntimeError:
                continue
            for message in messages:
                if (message["sender_id"] == settings["owner_user_id"]
                        and records.squash(message["content"]) == records.squash(batch["reply"])
                        and (contact["kind"] == "p2p" or message["reply_to"] == anchor)):
                    return message
        return {}

    def read_backs(self, ctx, settings, contacts):
        """Each sent reply the tool gave no om_ for (``reads``) read back once more, its om_ written to the batch
        when it shows (``owner_replied`` tells the proxy's own messages by it, till then by their text). Each
        poll; READ_BACKS reads in all."""
        for batch_id, left in list(self.reads.items()):
            batch = records.read_batch(ctx.state_dir, batch_id)
            contact = contacts.get(batch["contact"]) if batch else None
            if contact is None or batch["status"] != "sent" or batch.get("sent_message_id"):
                self.reads.pop(batch_id, None)
                continue
            found = self.read_back(settings, contact, batch, tries=1)
            if found or left <= 1:
                self.reads.pop(batch_id, None)
            else:
                self.reads[batch_id] = left - 1
            if found:
                self.proxy.update(ctx, batch, sent_message_id=found["id"])

    def owner_replied(self, ctx, settings, contact, batch):
        """Whether the owner answered the batch themselves: in a private chat any later message of theirs,
        in a group a reply to one of its messages. Messages this proxy sent do not count: those read back
        by their id, those that may have gone out (uncertain) or went out without one by their text."""
        ids = {m["id"] for m in batch["messages"]}
        positions = [int(m["position"]) for m in batch["messages"] if m["position"] and records.listed(m)]
        newest = max(message_epoch(m["time"]) for m in batch["messages"])
        done = [b for b in records.read_batches(ctx.state_dir, statuses=("sent", "uncertain")) if b["contact"] == contact["key"]]
        sent = {b.get("sent_message_id") for b in done if b["status"] == "sent"}
        maybe = {records.squash(b.get("reply")) for b in done if b["status"] == "uncertain" or not b.get("sent_message_id")}
        try:
            messages = self.replies(settings, contact, message_epoch(batch["messages"][0]["time"]) - 120)
        except RuntimeError:
            return False

        def later(message):
            # By position when both are in the chat's main list; else (a thread's replies are -3 there) by the
            # minute it was sent: the owner's message before a batch of thread replies once counted as their
            # answer, and the batch went unanswered unsaid (repo review D9).
            if positions and message["position"] and records.listed(message):
                return int(message["position"]) > max(positions)
            return message_epoch(message["time"]) > newest
        return any(m["sender_id"] == settings["owner_user_id"] and m["id"] not in sent
                   and records.squash(m["content"]) not in maybe
                   and (m["reply_to"] in ids if contact["kind"] == "group" else later(m))
                   for m in messages)
