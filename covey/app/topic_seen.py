"""Which topics the owner has seen, from Feishu's read receipts: with COVEY_PINNED_TOPICS=1 one more lark-cli
consumer (im.message.message_read_v1, feishu/events.py SUBSCRIPTIONS) runs in its own worker thread; without it
there is neither.

Feishu sends the receipt when someone reads messages the bot sent in a private chat: the reader, the read time and
the messages, but no chat. Only the owner's count (Access.is_owner), and each message is looked up among the owner's
private chat's aliases (core/topics.py message_topic: the bot's task, question and answer cards, and the messages it
sent in a topic); the latest read time of each topic found goes into <state>/topic-seen.json (core/topic_seen.py).
A message of no topic (the chat's top level, a menu panel) is left out, with no Feishu call to look it up.
"""
from __future__ import annotations

from covey.core.topic_seen import enabled, record_reads
from covey.core.topics import message_topic
from covey.feishu.events import READ_EVENT_KEY


def read_seconds(value) -> float | None:
    """Feishu's read time (epoch milliseconds, a string) as epoch seconds; None when it is not one."""
    try:
        milliseconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return milliseconds / 1000 if milliseconds > 0 else None


class TopicSeen:
    """``consume(ctx, key, handle)`` runs one SUBSCRIPTIONS consumer until shutdown (app/unified.py consume_events,
    so the Bridge's stop ends it with the others)."""

    def __init__(self, runtime, consume):
        self.runtime, self.consume = runtime, consume

    def threads(self):
        """Its consumer, only when switched on (app/unified.py worker_targets)."""
        return (("topic-seen", self.run),) if enabled() else ()

    def run(self, ctx):
        self.consume(ctx, READ_EVENT_KEY, self.handle)

    def handle(self, event, ctx):
        """One read receipt: the owner's, for messages of known topics, moves those topics' times on."""
        if not self.runtime.access.is_owner(str(event.get("reader_id") or "")):
            return
        seconds = read_seconds(event.get("read_time"))
        chat = self.runtime.access.notify_chat()
        messages = event.get("message_ids")
        if seconds is None or not chat or not isinstance(messages, list):
            return
        reads = {}
        for message in messages:
            topic = message_topic(ctx.state_dir, chat, message) if isinstance(message, str) else ""
            if topic:
                reads[topic] = max(seconds, reads.get(topic, 0))
        if reads:
            record_reads(ctx.state_dir, reads)
