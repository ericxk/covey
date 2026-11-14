"""手上的事, the pinned board and 早晚汇总 in each personal group (core/spaces.py ``personal_groups``).

The owner may talk to the main thread in a group more than in a private chat, and follows its topics in the group's
pins too, the private chat keeping its own. Each personal group but the notify space (whose board is the default one,
``Access.board_groups``) gets an Overview of its own (app/overview.py: ``chat`` fixed to the group, its
thread ``overview-<chat_id tail>``, the card saying 群), which lays that group's board (app/pins.py) over it, both in
records of their own (app/pins.py ``record_path``, configured with ``configure_boards``). They run when the private
chat's do: COVEY_PINNED_OVERVIEW=1, and COVEY_PINNED_TOPICS=1 for the board. 早晚汇总 (app/digest.py) too:
a Digest of its own (thread ``digest-<chat_id tail>``, record ``digest.<chat_id>.json``) at COVEY_DIGEST_TIMES,
its card's 这些都不用回答了 handed to it by that group's Overview.

A group sends no read receipts (im.message.message_read_v1 is for private chats only), so nothing there would ever
be seen and a result would stay pinned until ten newer ones push it off. Instead the owner's message anywhere in the
group (``owner_active``, app/card_routing.py) counts as reading it: every topic of the group read now goes into
topic-seen.json, so a result that ended before it is seen, stays KEEP_SEEN more (12 hours, as a seen one does in the
private chat) and gives way first when the board is full. Unpinning one by hand takes it off at once, as ever.
"""
from __future__ import annotations

import time

from covey.app.digest import Digest
from covey.app.overview import Overview
from covey.app.pins import kept, laid
from covey.core import board_read
from covey.core.topic_seen import record_reads


class GroupBoards:
    """``overview``: the private chat's Overview, which hands a click on a group's card to that group's."""

    def __init__(self, runtime, messenger, voice, titles, overview):
        self.runtime, self.messenger, self.voice, self.titles = runtime, messenger, voice, titles
        self.overview = overview
        self.boards = {}  # chat_id -> its Overview, made once the harness config named the group.
        self.digests = {}  # chat_id -> its Digest.

    def board(self, chat):
        board = self.boards.get(chat)
        if board is None:
            board = self.boards[chat] = Overview(self.runtime, self.messenger, self.voice, self.titles, chat=chat,
                                                 where="群", name="overview-" + chat[-8:])
            self.overview.others[chat] = board
        return board

    def digest(self, chat):
        digest = self.digests.get(chat)
        if digest is None:
            digest = self.digests[chat] = Digest(self.runtime, self.messenger, self.voice, chat=chat,
                                                 name="digest-" + chat[-8:])
            self.board(chat).handed[digest.origin] = digest.dismissed
        return digest

    def threads(self):
        """Each personal group's Overview and Digest threads (app/unified.py worker_targets, after the harness config is
        read); none of a feature that is off."""
        chats = sorted(self.runtime.access.board_groups())
        return (*(thread for chat in chats if Overview.enabled() for thread in self.board(chat).threads()),
                *(thread for chat in chats for thread in self.digest(chat).threads()))

    def owner_active(self, ctx, chat, now=None):
        """The owner wrote in ``chat``: when it is a personal group with the board laid, the topics of its read now
        are read now (topic-seen.json). The topics whose time moved on."""
        if chat not in self.runtime.access.agent_groups or not laid():
            return {}
        now = time.time() if now is None else now
        read = board_read.read(ctx.state_dir, chat, now, keep=kept(ctx.state_dir, chat))
        topics = {record.get("topic_id") for record in read.jobs}
        return record_reads(ctx.state_dir, {topic: now for topic in topics if isinstance(topic, str) and topic},
                            now=now)
