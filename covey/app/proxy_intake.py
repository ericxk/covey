"""Proxy replies (代答): taking messages in, as the owner (the IM events are the bot's and never show the
owner's chats). ``listen`` is a Bridge worker: every LIST_SECONDS (an active chat ACTIVE_LIST_SECONDS, within
LISTS a round) it lists the chats whose chat_id it knows, every SEARCH_SECONDS it searches the whitelisted
groups for messages that @ the owner and all private messages from people (one search, whatever the
whitelist's size: chats without a chat_id yet and replies inside threads, which no list shows), keeping the
whitelisted senders'. What is new waits in ``inbox``; Proxy.run, the only worker that writes batches, takes
it (``drain_inbox``) at once (Proxy.wake) and every POLL_SECONDS. Reads back off on failures (``guarded``).

Held by Proxy as ``intake``. Network calls only, under no lock but ``inbox_lock`` for the hand-over; it
writes no record.
"""
from __future__ import annotations

import concurrent.futures
import subprocess
import threading
import time

from covey.app.proxy_send import iso, message_epoch, user_cli
from covey.core import proxy as records

# The lists (+chat-messages-list, GET /open-apis/im/v1/messages) run as the owner's user app, which the owner also
# uses elsewhere. Its limit is 1000 calls a minute and 50 a second per app (频率限制 of
# https://open.feishu.cn/document/server-docs/im-v1/message/list); LISTS a ROUND_SECONDS keeps
# take-in at 240 a minute (24%) at most, whatever the whitelist's size: 8 chats 160 a minute, 16 at the cap.
ROUND_SECONDS = 1  # ``listen``'s round: the known chats due are listed (+chat-messages-list shows a message at once).
LIST_SECONDS = 3  # A known chat is listed again that many seconds after its last list...
ACTIVE_LIST_SECONDS = 2  # ...and sooner while it is active: a message taken in or a batch within ACTIVE_SECONDS.
ACTIVE_SECONDS = 300
LISTS = 4  # Chats one round lists at most, the longest waiting first; past that the whitelist takes turns.
SEARCH_SECONDS = 60  # The search, every that many seconds of ``listen``'s rounds.
BACKOFF_FIRST = 10  # Seconds a read waits after its first failure, doubled for each one in a row...
BACKOFF_LIMIT = 600  # ...up to this (an error, a rate limit); a chat that waits takes no list of a round.
LOOKBACK = 900  # Seconds each read reaches back: the message search shows a new message within about a minute.
# After failures a read reaches back to its source's last good one instead, at most this far: a host-wide stall
# (every call timing out at 30 s) can leave chats' lists reading nothing for longer than LOOKBACK.
CATCH_UP = 7200
SEARCH_PAGES = 4  # Pages of 50 a poll's search reads (15 minutes of private messages on a busy day: 6).


def order(message):
    """The order a chat's messages are taken in: by position, then by time."""
    return int(message["position"] or 0), message["time"]


def list_source(contact):
    """The source a chat's list backs off as (``ProxyIntake.guarded``)."""
    return "list " + records.digest(contact["key"])[:8]


class ProxyIntake:
    """``listen``'s state, in memory only: ``inbox`` {contact key: new messages} found and not yet taken in by
    Proxy.run (under ``inbox_lock``), ``failures`` and ``read_ok`` of each read's source, ``listed_at`` of each
    chat, ``active`` (Proxy.tick marks it too, for a batch under way) and ``searching``, the search's thread."""

    def __init__(self, proxy) -> None:
        self.proxy = proxy
        self.inbox, self.inbox_lock = {}, threading.Lock()
        self.failures = {}  # A read's source: (failures in a row, monotonic time of its next try); listen's threads.
        self.read_ok = {}  # A read's source: epoch seconds its last good read started (``reach``); listen's threads.
        self.listed_at = {}  # Contact key: monotonic time of its chat's last list.
        self.active = {}  # Contact key: epoch seconds of its last message taken in or batch under way.
        self.searching = None  # ``listen``'s search, a thread of its own while it runs.

    def drain_inbox(self):
        """{contact key: new messages} found since the last call, ``inbox`` emptied (Proxy.run, Proxy.tick)."""
        with self.inbox_lock:
            inbox, self.inbox = self.inbox, {}
        return inbox

    def listen(self, ctx):
        """The take-in worker: a round every ROUND_SECONDS lists the known chats due (``listed``), and every
        SEARCH_SECONDS also searches (``search``); what is new goes to ``inbox`` and wakes Proxy.run. Network
        calls only, under no lock but ``inbox_lock`` for the hand-over."""
        searched = None
        while not self.proxy.runtime.shutdown.is_set():
            started = time.monotonic()
            settings = self.proxy.settings(ctx)
            if settings.get("enabled") and settings.get("owner_user_id"):
                search = searched is None or started - searched >= SEARCH_SECONDS
                if search:
                    searched = started
                self.listen_once(ctx, settings, search=search)
            # Rounds start ROUND_SECONDS apart at least, so LISTS a round bounds the lists a minute.
            if self.proxy.runtime.shutdown.wait(max(0.0, started + ROUND_SECONDS - time.monotonic())):
                return

    def listen_once(self, ctx, settings, *, search):
        """One round: the chats due listed; with ``search`` the search started in a thread of its own unless the
        last one still runs (repo review F7: its two calls, up to 60 s each, held up every chat's list)."""
        contacts = [c for c in records.read_contacts(ctx.state_dir) if not c.get("paused")]
        if not contacts:
            return
        if search and not (self.searching and self.searching.is_alive()):
            self.searching = threading.Thread(
                target=lambda: self.hand_over(settings, contacts, self.guarded("search", lambda: self.search(settings, contacts), [])),
                name="proxy-search", daemon=True)
            self.searching.start()
        self.hand_over(settings, contacts, self.listed(settings, contacts))

    def hand_over(self, settings, contacts, found):
        """What is new in ``found`` into ``inbox``, and Proxy.run woken for it (Proxy.wake)."""
        with self.inbox_lock:
            new = self.new_messages(settings, contacts, found, self.inbox)
            for key, messages in new.items():
                self.inbox[key] = sorted([*self.inbox.get(key, []), *messages], key=order)
        for key in new:
            self.mark_active(key, time.time())
        if new:
            self.proxy.wake.set()

    def guarded(self, source, read, default):
        """``read()``, or ``default`` while ``source`` backs off: each failure in a row doubles its wait (up to
        BACKOFF_LIMIT), logged on the first and once it reads again, not every round."""
        count, until = self.failures.get(source, (0, 0.0))
        if time.monotonic() < until:
            return default
        began = time.time()
        try:
            value = read()
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            if self.proxy.runtime.shutdown.is_set():
                # The Bridge's stop ended it (lark-cli rc=-15, or after it printed): no failure of the source. Every
                # restart logged "proxy take-in failed … rc=-15" for the lists under way.
                return default
            wait = min(BACKOFF_LIMIT, BACKOFF_FIRST * 2 ** count)
            self.failures[source] = (count + 1, time.monotonic() + wait)
            if not count:
                self.proxy.runtime.log(f"proxy take-in failed source={source}: {type(exc).__name__}: {str(exc)[:200]}; "
                                       f"tried again in {wait}s, longer while it fails")
            return default
        self.read_ok[source] = began
        if self.failures.pop(source, None):
            self.proxy.runtime.log(f"proxy take-in reads again source={source} after {count} failure(s)")
        return value

    def reach(self, source):
        """Epoch seconds a read of ``source`` starts from: LOOKBACK back, or back to its last good read (less a
        minute) when failures kept it from reading for longer, at most CATCH_UP."""
        now = time.time()
        return max(now - CATCH_UP, min(now - LOOKBACK, self.read_ok.get(source, now) - 60))

    def listed(self, settings, contacts):
        """The newest messages of the known chats (a group's target is its chat; a person's chat once a search
        found it) that are due (``due``), at most LISTS a round, the longest waiting first, all at once, each its
        own source to back off."""
        now = time.monotonic()
        known = [c for c in contacts if (c.get("chat_id") or c["kind"] == "group") and self.due(c, now)]
        known = sorted(known, key=lambda c: (self.listed_at.get(c["key"], float("-inf")), c["key"]))[:LISTS]
        if not known:
            return []
        for contact in known:
            self.listed_at[contact["key"]] = now
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(known), thread_name_prefix="proxy-list") as pool:
            reads = list(pool.map(lambda c: self.guarded(list_source(c), lambda: self.list_chat(settings, c), []), known))
        return [message for messages in reads for message in messages]

    def due(self, contact, now):
        """Whether ``contact``'s chat is listed this round: LIST_SECONDS after its last list, ACTIVE_LIST_SECONDS
        while it is active, and not while its list backs off (``guarded``), which takes no list from the others.
        Half a round early counts: rounds start a little late, never early, so 3 s would come to 4."""
        every = ACTIVE_LIST_SECONDS if time.time() - self.active.get(contact["key"], 0.0) < ACTIVE_SECONDS else LIST_SECONDS
        return now - self.listed_at.get(contact["key"], float("-inf")) >= every - ROUND_SECONDS / 2 \
            and now >= self.failures.get(list_source(contact), (0, 0.0))[1]

    def mark_active(self, key, when):
        self.active[key] = max(self.active.get(key, 0.0), when)

    def list_chat(self, settings, contact):
        chat = contact.get("chat_id") or contact["target"]
        start = max(self.reach(list_source(contact)), float(contact.get("since") or 0) - 60)
        # The list leaves out the chat_id and the chat_type: every message is the listed chat's.
        kind = "group" if contact["kind"] == "group" else "p2p"
        return [records.incoming({**item, "chat_id": item.get("chat_id") or chat, "chat_type": item.get("chat_type") or kind})
                for item in self.proxy.web.chat_messages(settings, contact, start, timeout=30)]

    def search(self, settings, contacts):
        """The messages of the owner's searches: the groups' @s, every person's private messages."""
        profile = settings["user_profile"]
        start = iso(max(self.reach("search"), min(float(c.get("since") or 0) for c in contacts)))
        found = []
        groups = [c["target"] for c in contacts if c["kind"] == "group"]
        common = ("--start", start, "--page-size", "50", "--page-all", "--page-limit", str(SEARCH_PAGES), "--no-reactions")
        if groups:
            found += self.messages(profile, "--is-at-me", "--chat-id", ",".join(groups), *common)
        if any(c["kind"] == "p2p" for c in contacts):
            # Every person's private messages to the owner, then only the whitelisted senders' (addressed):
            # one call whatever the whitelist's size. The owner's own messages come too; bots' do not.
            found += self.messages(profile, "--chat-type", "p2p", "--sender-type", "user", *common)
        return found

    @staticmethod
    def new_messages(settings, contacts, found, pending=None):
        """{contact key: its new messages in ``found``, in order}: addressed to the owner, not seen, not before
        ``since``, and not among ``pending`` (messages found already, waiting to be taken in)."""
        owner, new = settings["owner_user_id"], {}
        for contact in contacts:
            known = {*contact.get("seen", []), *(m["id"] for m in (pending or {}).get(contact["key"], []))}
            messages = {m["id"]: m for m in found if records.addressed(contact, m, owner) and m["id"] not in known
                        and message_epoch(m["time"]) + 60 >= float(contact.get("since") or 0)}
            if messages:
                new[contact["key"]] = sorted(messages.values(), key=order)
        return new

    def messages(self, profile, *args):
        """The messages of one ``+messages-search`` as the owner. Results do not come newest first, so
        what lies past SEARCH_PAGES may be missed: that is logged."""
        payload = user_cli(profile, "im", "+messages-search", *args)
        kind = "p2p" if "p2p" in args else "group"
        if (payload.get("data") or {}).get("has_more"):
            self.proxy.runtime.log(f"proxy search truncated pages={SEARCH_PAGES} chat_type={kind}")
        # Each result says its chat_type; the search's own (--chat-type p2p, or the groups' --chat-id) when not.
        return [records.incoming({**item, "chat_type": item.get("chat_type") or kind})
                for item in records.search_messages(payload)]
