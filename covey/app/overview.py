"""手上的事: one card in the owner's private chat with the bot, pinned (Pin 消息), that lists what waits for the
owner and what runs there (cards/overview.py). On unless COVEY_PINNED_OVERVIEW is 0 (docs/configuration.md):
without it there is no thread and no card. Only the private chat, nothing in groups.

Every TICK seconds the loop reads the private chat's pending questions and running jobs from the indexes of
questions/ and jobs/ (no record lock, no Feishu call; core/board_read.py, with the board on one read for both) and
redraws the card whole when what it shows changed, at most once every GAP seconds, and anyway every REFRESH seconds.
The card writes times of day, not durations, so it changes only with its content (a row's 等了 18 小时 5 分 would redraw it every minute).
A job seen running that has ended stays under 刚结束 for RECENT seconds; this is the process's memory, a restart
forgets it.

Every row opens what it is about, the whole row (cards/ui.py topic_row): a question's its card, a stopped or running
task's its task card (检查并继续, 看看, 补充 and 停止 are there), one that ended a moment ago its card in the topic's thread
(``link``: read once from Feishu, then kept). In the owner's private chat with 整理专项 on, its proposals waiting for his
好 are 等你 too (core/board_read.py Asks.proposals): listed and counted as the 看板 lists and counts them, with its
建专项 and 不管了 (a click redraws this card at once: app/unified.py proposal_decided). With the web page's address set
(core/web_sync.py page_url) the last line has 打开网页.

The card's record is <state>/pinned-overview.json: its chat, card and message, the CardKit sequence of its last
write (reserved and saved before the write, as for form cards: core/card_sequence.py), its generation and when it
was sent and pinned. Once the owner's private chat is known (Access.owner_chat) the card goes out as a new message
and is pinned; a pin that failed is tried again every PIN_RETRY seconds, but a card once pinned is not pinned again,
so one the owner took off the pins stays off. A card older than ROTATE seconds, one in another chat than the
owner's now, and one that FAILS writes in a row did not change, give way to a new card, pinned the same way; the old
one is unpinned and redrawn once to say it no longer changes. A send that failed is tried again SEND_RETRY seconds
later with the same idempotency key (the generation names it), so one whose outcome was unknown posts no second card.

With COVEY_PINNED_TOPICS=1 too, the loop also keeps the pinned board (app/pins.py) once the card is pinned,
and the card becomes its index: how many each group has, how many are pinned, and the rows without a pin. The topics
the owner took off the board (app/pins.py, dismissed) are in none of it, only in one line at the bottom. The line of
questions waiting over a day has 这些都不用回答了 (DISMISS, the owner's only; its value names the topics it lists):
it takes them off the board, and the card is redrawn once the click is acknowledged. The same line and button are on
the 早晚汇总 card (app/digest.py), shown only when the click can take effect (``dismissable``); its value's ``from``
names that card, and ``handed`` (app/unified.py fills it) gets the click once acknowledged, beside the redraw, to
change that card too. The line it leaves has 撤销 (UNDO, ``undo``: the owner's only; what that 不管了 took off comes back, only the
topics whose entry is still that click's: app/pins.py undismiss; ``unhanded`` is ``handed`` for it). ``tick`` runs one at a time
(``turn``), from the loop or after such a click, so the card's writes keep their order (core/card_sequence.py).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from covey.core.process_env import env_flag
from covey.app import owner_ledger
from covey.app.card_lanes import after_ack
from covey.app.pins import GROUPS, PinnedTopics, dismiss, kept, last_click, record_path, taken_off, undismiss
from covey.app.topic_links import link_for
from covey.cards.overview import DISMISS, DISMISS_MAX, UNDO, clock, lists, overview_card, retired_card
from covey.core.card_sequence import CardHandle, file_card, reserve
from covey.core.jobs import job_path, read_job_record
from covey.core.locks import resource_lock
from covey.core import board_read, look, memory_words, projects, turn_words, web_sync
from covey.core import progress as turn_progress
from covey.core.loops import every
from covey.core.state import read_object, write_json
from covey.core.statuses import RUNNING, TERMINAL
from covey.core.metrics import timings_of
from covey.core.topics import topic_title

ENV = "COVEY_PINNED_OVERVIEW"
RECORD = "pinned-overview.json"
TICK = 10
GAP = 15
REFRESH = 3600  # Only the day changes the card with the clock: a safety redraw.
RECENT = 600
SEND_RETRY = 60
PIN_RETRY = 1800
ROTATE = 7 * 86400
FAILS = 20  # About five minutes of redraws, GAP apart.
LINK_RETRY = 600  # A 等我 card's link that could not be read: tried again this much later.
TIMEOUT = 15  # Of one CardKit write; the loop is in no hurry.
# The phase a running task shows before its model wrote one (core/turn_words.py): no phase yet.
UNSET_PHASES = turn_words.UNSET_PHASES


PROJECT_GO = "在看板上点「继续」接着做"  # What a project a guard paused waits for (cards/board.py PROJECT_GO_KIND).


def board_row_tone(row, unseen) -> str:
    """The core/look.py STATES key of a result ``row`` of the board (app/pins.py board): its record's tone, a done one
    有结果 until the owner has seen it (``unseen``: the board's 有结果未看)."""
    return look.record_tone(row.get("record") or {}, seen=row["topic"] not in unseen)


def dismissable():
    """Whether 这些都不用回答了 takes effect (``Overview.claim``): 手上的事 and the pinned board both on."""
    return Overview.enabled() and PinnedTopics.enabled()


def waiting_count(state, chat, now, *, proposals=False) -> int:
    """How many things wait for the owner in ``chat``, counted as the 看板 and 手上的事 count them: core/board_read.py
    waits with the topics taken off the board left out while the board is on (app/digest.py collect), its ``count``, and
    with ``proposals`` (the owner's private chat) 整理专项's proposals waiting for his 好 (core/board_read.py Asks.proposals)."""
    found = board_read.waits(state, chat, now, off=taken_off(state, chat, now) if dismissable() else ()).count()
    return found + (len(board_read.waiting_proposals(state)) if proposals else 0)


class Overview:
    """``titles``: Harness name -> the title the card gives it (Claude Code, Codex, ...). ``chat``: the chat it is
    pinned in, '' the owner's private chat (Access.owner_chat); a personal group has
    one of its own (app/group_boards.py), its record its own (app/pins.py record_path), ``where`` the card's word for
    it and ``name`` its thread's."""

    def __init__(self, runtime, messenger, voice, titles, *, chat="", where="私聊", name="overview"):
        self.runtime, self.messenger, self.voice, self.titles = runtime, messenger, voice, titles
        self.fixed, self.word, self.name = chat, where, name
        self.others = {}  # chat_id -> the Overview pinned there (app/group_boards.py): its clicks are its own.
        self.topics = {}  # topic_id -> its label (T12): it never changes once known.
        self.seen = {}  # The running rows of the last look, by job key: one that is gone may have ended.
        self.history, self.now = {}, 0.0  # The last view's jobs by topic and its time (``progress_of``).
        self.ended = {}  # job key -> its 刚结束 row.
        self.drawn = None  # What the card last drawn showed, without its 更新于.
        self.drawn_at = 0.0
        self.failures = 0
        self.never_had = None  # Whether the chat has had no topic yet (None: not asked; False once it had one, for good).
        self.send_tried = 0.0
        self.pin_tried = 0.0
        self.pins = PinnedTopics(runtime, messenger, voice)
        self.links, self.profile = {}, None  # A card (or a topic) -> (its applink, when read); the profile of the last view.
        self.turn = threading.Lock()  # One tick at a time: the loop's, or one after a click.
        # A card other than this one with the line of old questions (its value's ``from``) -> what it does once a
        # click there is acknowledged: (ctx, the clicked message's id, the topics taken off).
        self.handed = {}
        self.unhanded = {}  # The same for 撤销 (UNDO): origin -> what it does with (ctx, message id, the topics put back).

    @staticmethod
    def enabled():
        """COVEY_PINNED_OVERVIEW: on unset, 0/off/false/no turn it off."""
        return env_flag(ENV, True)

    def threads(self):
        """Its worker, only when switched on (app/unified.py worker_targets)."""
        return ((self.name, self.run),) if self.enabled() else ()

    def chat(self):
        return self.fixed or self.runtime.access.notify_chat()

    def private(self, chat) -> bool:
        """Whether this card in ``chat`` is the owner's private chat's: a group's has no proposals of 整理专项 (it sorts the
        private chat's topics)."""
        return not self.fixed and chat not in getattr(getattr(self.runtime, "access", None), "agent_groups", ())

    @property
    def where(self):
        """The card's word for its chat: 群 too for the default overview when ``spaces.notify_space`` is a personal group."""
        if not self.fixed and self.chat() in getattr(self.runtime.access, "agent_groups", ()):
            return "群"
        return self.word

    def run(self, ctx):
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate="overview")

    def tick(self, ctx, now=None, *, redraw=False):
        """One look, and the card and the board brought to it; ``redraw``: the card now, without waiting GAP."""
        with self.turn:
            if redraw:
                self.drawn_at = 0.0
            self.step(ctx, time.time() if now is None else now)

    def step(self, ctx, now):
        chat = self.chat()
        if not chat:
            return  # No private chat with the owner yet: nowhere to pin it.
        self.settle(ctx, chat)
        view = self.view(ctx, chat, now)
        path = record_path(ctx.state_dir, chat, RECORD)
        record = read_object(path)
        if (record.get("chat_id") != chat or not record.get("card_id") or not record.get("message_id")
                or now - float(record.get("sent_at") or 0) >= ROTATE or self.failures >= FAILS):
            if now - self.send_tried < SEND_RETRY:
                return
            self.send_tried = now
            new = self.send(ctx, path, record, chat, view, now)
            if new is None:
                return
            if record.get("message_id"):
                self.retire(ctx, record)
            record = new
        else:
            self.redraw(ctx, path, record, view, now)
        self.pin(ctx, path, record, now)
        if "board" in view:
            self.pins.tick(ctx, chat, read_object(path), now)

    # What the card shows.

    def view(self, ctx, chat, now):
        state = ctx.state_dir
        board = self.pins.enabled()
        # The indexes read once (core/board_read.py), and judged as the board judges them (PinnedTopics.look: with the
        # board on, its record's rules, results and topics taken off; else its rules' defaults). The butler
        #  (app/butler.py) talks at the chat's top level: not listed; what it asks the owner waits under 等你 all the
        # same, under its speaker (board_read.top_questions, docs/ui.md 5.3).
        read = board_read.read(state, chat, now, keep=kept(state, chat) if board else ())
        self.profile = getattr(ctx, "profile", None)
        looked = self.pins.look(state, chat, now, read, board=board)
        off = self.pins.dismissed if board else {}
        # 等主人的账 (app/owner_ledger.py), the one every reader counts from: 等我 (core/board_read.py waits), the latest
        # first: the questions, the butler's and the leads' too, and the tasks waiting for 开始; with the board on, its
        # topics taken off left out (its look). 要你处理 as the 看板 counts it (core/board_read.py Asks): 等我, the results
        # that stopped, what may be stuck, the projects a guard paused and, in the private chat, 整理专项's proposals.
        ledger = owner_ledger.of(state, chat, now, self.pins, proposals=self.private(chat), off=off)
        found, asks = ledger.waits, ledger.asks
        jobs = sorted([record for record in read.jobs if record.get("status") in RUNNING],
                      key=lambda record: int(record.get("job_id") or 0))
        # The topics' earlier turns as the read has them, for the estimate (core/progress.py).
        self.history = {}
        for record in read.jobs:
            self.history.setdefault(record.get("topic_id") or "", []).append(record)
        self.now = now
        running = {record.get("key") or str(record.get("job_id")): self.running_row(state, record, asks)
                   for record in jobs}
        for key, row in self.seen.items():
            if key not in running:
                record = read_job_record(job_path(state, key)) or {}
                status = record.get("status")
                if status in TERMINAL:
                    # Its line as the 看板 says it; no ``now``, so that the card changes with its content only. It opens its
                    # card in the topic's thread (the answer follows it there).
                    card = str(record.get("card_message_id") or "")
                    self.ended[key] = {"label": row["label"], "title": row["title"], "topic": row["topic"],
                                       "status": status, "at": now, "tone": look.record_tone(record),
                                       "line": board_read.ended_line(record), "url": self.link(state, row["topic"], card)}
        self.seen = running
        self.ended = {key: row for key, row in self.ended.items() if now - row["at"] < RECENT and key not in running}
        waiting = [self.waiting_row(state, wait) for wait in found.all]
        stopped = self.stopped_rows(state, asks)
        # A topic that runs again, asks or stopped: its row there says enough.
        busy = {row["topic"] for row in (*running.values(), *waiting, *stopped)}
        view = {"waiting": waiting, "stopped": stopped, "running": list(running.values()),
                "ended": sorted((row for row in self.ended.values() if row["topic"] not in busy),
                                key=lambda row: row["at"], reverse=True)}
        proposals = self.proposal_rows(state, asks)
        if proposals:
            view["proposals"] = proposals
        if board:
            view["board"] = self.board(state, chat, found, looked, asks, stopped)
        url = web_sync.page_url()
        if url:
            view["web"] = url
        if self.never_had is not False and not lists(view):
            # Nothing to list: a first use (还没有话题) or a clear board (cards/ui.py empty_words). Once the chat had a
            # topic it is no first use again.
            self.never_had = not board_read.has_topics(state, chat)
            if self.never_had:
                view["first_use"] = True
        return view

    @staticmethod
    def listed(view) -> bool:
        """Whether ``view`` lists anything (cards/overview.py lists)."""
        return lists(view)

    def stopped_rows(self, state, asks):
        """停住了 as 要你处理 counts it (core/board_read.py Asks): a row each topic whose result stopped (why, where; it opens
        its task card, where 检查并继续 is) and each project a guard paused (why; it opens the project's card, where 继续
        is, or the 看板's)."""
        rows = []
        for topic, record in sorted(asks.stopped.items(), key=lambda item: board_read.ended_at(item[1]), reverse=True):
            label, title = self.topic(state, topic)
            card = str(record.get("card_message_id") or "")
            rows.append({"label": label, "title": title, "topic": topic, "status": record.get("status"),
                         "at": board_read.ended_at(record), "tone": "stopped", "line": board_read.ended_line(record),
                         "url": self.link(state, topic, card) if card else ""})
        for record in asks.projects.values():
            lead, card = str(record.get("lead_topic_id") or ""), projects.card_message(record)
            rows.append({"label": f"专项「{record.get('name') or ''}」", "title": "", "topic": lead, "tone": "stopped",
                         "project": str(record.get("id") or ""),
                         "at": float(record.get("updated_at") or 0) or None,
                         "line": " · ".join(part for part in (projects.stop_reason(record), PROJECT_GO) if part),
                         "url": self.link(state, lead, card) if lead and card else ""})
        return rows

    def proposal_rows(self, state, asks):
        """整理专项's proposals waiting for his 好 as 要你处理 names them (``asks.proposals``), the newest first: each its id, name,
        goal and topics (T number and title: no link, one read each would be too many), for 建专项 and 不管了
        (cards/board.py proposal_block)."""
        rows = []
        for key, record in asks.proposals.items():
            topics = [{"topic": topic, "label": self.topic(state, topic)[0], "title": ""}
                      for topic in dict.fromkeys(record.get("topics") or ())]
            rows.append({"id": key, "name": str(record.get("name") or ""), "goal": str(record.get("goal") or ""),
                         "rows": topics})
        return rows

    def board(self, state, chat, found, looked, asks, stopped):
        """What the card shows over the pinned board: each state's count (要你处理 as ``asks`` counts it, core/board_read.py
        Asks; 有结果 the results on it not seen yet, 在做 the rest of what runs), how many are pinned, the rows without a pin
        by state (under 等你 every 等我 whose card is not pinned; under 停住了 the results that stopped and the projects a
        guard paused, ``stopped``; 已停止 the owner's own stops), and 等我 waiting too long to be on the board (newest
        first); ``looked``: the board's look (PinnedTopics.look of the indexes as read for it)."""
        rows, held = looked
        off = self.pins.dismissed
        rows = [row for row in rows if row["topic"] not in off]
        listed = {group: [self.board_row(state, row, asks) for row in rows if row["group"] == group
                          and row["topic"] not in held] for group in GROUPS}
        pinned = {row["message_id"] for row in rows if row["group"] == "waiting" and row["topic"] in held}
        listed["waiting"] = [self.waiting_row(state, wait) for wait in found.live
                             if (wait.record.get("question_message_id") if wait.kind == "question" else wait.card) not in pinned]
        old = [self.waiting_row(state, wait) for wait in found.stale]
        gone = sorted(off, key=lambda topic: off[topic]["at"], reverse=True)
        # A result by what it is (core/look.py record_tone): 已停止 is background, the rest 有结果; 停住了 is what 要你处理
        # counts (``stopped``: a topic's, a project's), each unless a pin shows that very stop (its topic's own pin, or its
        # project's cell pinned on that thread's result). One behind its project's pinned cell showing another thread is
        # listed: the band counts it and nothing else on the card would show it (T734).
        results = listed.pop("result")
        shown = {str((row.get("record") or {}).get("topic_id") or row["topic"]) for row in rows if row["topic"] in held}
        listed.update(stopped=[row for row in stopped if row.get("project") or row["topic"] not in shown],
                      cancelled=[row for row in results if row["tone"] == "cancelled"],
                      result=[row for row in results if row["tone"] not in ("stopped", "cancelled")])
        tones = [board_row_tone(row, self.pins.unseen) for row in rows if row["group"] == "result"]
        stuck = sum(row["group"] == "running" and (row.get("record") or {}).get("topic_id") in asks.stuck for row in rows)
        counts = {**asks.counts(), "result": tones.count("result"),
                  "running": sum(row["group"] == "running" for row in rows) - stuck}
        undo = last_click(off, self.now)  # 撤销最近一次: his latest 不管了 click, if still on.
        proposals = self.proposal_rows(state, asks)  # Never pinned: each is a row here.
        return {"counts": counts, "pinned": sum(row["topic"] in held for row in rows), **listed, "stale": old,
                "dismissed": [self.topic(state, topic)[0] for topic in gone], **({"undo": undo} if undo else {}),
                **({"proposals": proposals} if proposals else {})}

    def board_row(self, state, row, asks):
        record = row["record"]
        if row["group"] == "running":
            return self.running_row(state, record, asks)
        label, title = self.topic(state, row["topic"])
        card = str(record.get("card_message_id") or "")
        return {"label": label, "title": title, "topic": row["topic"], "status": record.get("status"),
                "at": row["at"], "seen": row["topic"] not in self.pins.unseen,
                "tone": board_row_tone(row, self.pins.unseen), "line": board_read.ended_line(record),
                "url": self.link(state, row["topic"], card)}

    def waiting_row(self, state, wait):
        """The row of ``wait`` (core/board_read.py Wait): who asks, what (or that it waits for 开始), since when, and
        the link that opens the card it is acted on (``url``, '' while it cannot be read)."""
        record, topic = wait.record, wait.topic
        chat = record.get("chat_id") or ""
        if topic and chat and not board_read.listed(topic, chat):  # The butler or a lead: its speaker, no T number.
            label, title = board_read.speaker_of(state, topic, chat) or "管家", ""
        else:
            label, title = self.topic(state, topic)
        # The question itself: most headers are the generic 补充信息.
        ask = turn_words.ask_words(record) if wait.kind == "question" else turn_words.START_LINE
        ask = memory_words.for_chat(ask, state, chat, url="")  # Titles for memory short codes (core/memory_words.py).
        return {"label": label, "title": title, "topic": topic, "ask": ask, "since": wait.since,
                "url": self.link(state, topic, wait.card)}

    def link(self, state, topic, card):
        """The applink of ``card`` in ``topic`` (app/topic_links.py link_for: one read of the message, then kept), else
        (no ``card``) of the topic at its newest card; this process's memory first; one that could not be read is tried
        again LINK_RETRY seconds later. '' for no topic."""
        if not topic:
            return ""
        key = card or f"topic:{topic}"
        known = self.links.get(key)
        if known and (known[0] or time.monotonic() - known[1] < LINK_RETRY):
            return known[0]
        try:
            url = link_for(state, topic, card=card, profile=self.profile)
        except Exception:  # noqa: BLE001 - a row without a link, as before
            url = ""
        self.links[key] = (url, time.monotonic())
        return url

    def running_row(self, state, record, asks=None):
        """A running (or queued) job's row; ``stuck`` when 要你处理 counts its topic as 可能卡住 (``asks``,
        core/board_read.py Asks) and this is its job running."""
        label, title = self.topic(state, record.get("topic_id") or "")
        timings = timings_of(record)
        phase = str(record.get("phase") or "")
        harness = record.get("harness_id") or record.get("backend") or ""
        stuck = bool(asks is not None and record.get("status") == "running" and record.get("topic_id") in asks.stuck)
        card = str(record.get("card_message_id") or "")  # Its task card: 看看, 补充 and 停止 are there.
        return {"label": label, "title": title, "topic": record.get("topic_id") or "", "stuck": stuck,
                "url": self.link(state, record.get("topic_id") or "", card) if card else "",
                "harness": self.titles.get(harness, harness),
                "workspace": record.get("workspace_name") or "", "status": record.get("status"),
                "started": timings.get("backend_started") or timings.get("dispatched"),
                "phase": "" if phase in UNSET_PHASES else phase,
                # The checkpoint's steps and when each started, as the task monitor keeps them on the record
                # (core/longtask.py step_fields): the butler's snapshot says which step (app/butler.py).
                "steps": record.get("steps"), "step_times": record.get("step_times"),
                # Its estimate as a time of day, or 可能卡住了 (core/progress.py board_words): times of day, not durations.
                "progress": turn_progress.board_words(self.progress_of(record), self.now, clock=True)}

    def progress_of(self, record):
        """The progress of a running job ``record`` (core/progress.py), its topic's earlier turns as the last view read."""
        history = turn_progress.turn_seconds(self.history.get(record.get("topic_id") or "", ()), exclude=record.get("job_id"))
        return turn_progress.progress(record, history=history, now=self.now)

    def topic(self, state, topic_id):
        """(T number, the topic's title: core/topics.py topic_title); the number kept once the topic has it."""
        if topic_id in self.topics:
            return self.topics[topic_id], topic_title(state, topic_id)
        label, title = board_read.topic_label(state, topic_id)
        if label:  # Not kept without: its number may not be written yet.
            self.topics[topic_id] = label
        return label, title

    # The card.

    def send(self, ctx, path, record, chat, view, now):
        """A new card for ``view`` as a message in ``chat``: its record, None when it did not go out. Its key holds
        the generation, so a try after one whose outcome was unknown posts nothing new."""
        generation = int(record.get("generation") or 0) + 1
        key = self.voice.key("overview", f"{chat}-{generation}")
        cid, mid = self.messenger.create_and_send_card(ctx.profile, chat,
                                                       overview_card(view, now, clock(now), where=self.where), key,
                                                       voice=self.voice)
        if not mid:
            self.runtime.log(f"pinned overview not sent generation={generation} card={'yes' if cid else 'no'}")
            return None
        new = {"schema_version": 1, "chat_id": chat, "card_id": cid, "message_id": mid, "sequence": 0,
               "generation": generation, "sent_at": now}
        with resource_lock("pinned-overview", str(path)):
            write_json(path, new)
        self.drawn, self.drawn_at, self.failures = overview_card(view, now, where=self.where), now, 0
        self.send_tried = self.pin_tried = 0.0  # Pin it now; a new chat or an old card may replace it at once.
        self.runtime.log(f"pinned overview sent generation={generation} message={mid[-8:]}")
        return new

    def redraw(self, ctx, path, record, view, now):
        """The card redrawn whole when what it shows changed (GAP apart) or REFRESH has passed."""
        shown = overview_card(view, now, where=self.where)
        if now - self.drawn_at < (GAP if shown != self.drawn else REFRESH):
            return
        handle = file_card(path, record, shared=False)  # Only this loop writes it.
        card = overview_card(view, now, clock(now), where=self.where)
        with handle.lock:
            sequence = reserve(handle)
            result = self.messenger.put_card(ctx.profile, record["card_id"], card, sequence=sequence,
                                             key=f"overview-{record.get('generation') or 0}-{sequence}",
                                             timeout=TIMEOUT)
        self.drawn_at = now
        if result is None:
            self.failures += 1
            self.runtime.log(f"pinned overview update failed failures={self.failures}")
            return
        self.drawn, self.failures = shown, 0

    def pin(self, ctx, path, record, now):
        if record.get("pinned_at") or now - self.pin_tried < PIN_RETRY:
            return
        self.pin_tried = now
        if not self.messenger.pin(ctx.profile, record["message_id"]):
            self.runtime.log("pinned overview pin failed")
            return
        with resource_lock("pinned-overview", str(path)):
            saved = read_object(path)
            if saved.get("message_id") == record["message_id"]:
                write_json(path, {**saved, "pinned_at": now})
        self.runtime.log(f"pinned overview pinned message={record['message_id'][-8:]}")

    @staticmethod
    def matches(raw):
        return isinstance(raw, dict) and raw.get("kind") in (DISMISS, UNDO)

    def claim(self, ctx, event, raw, owner):
        """A click on 这些都不用回答了 (app/unified.py PANELS), else None: the topics its value names taken off the
        board, and the card redrawn once the click is acknowledged; a click on another card (``from``, ``handed``)
        also hands it the topics, beside the redraw."""
        if not self.matches(raw):
            return None
        other = self.others.get(str(event.get("chat_id") or ""))
        if other is not None:
            return other.claim(ctx, event, raw, owner)
        if not owner:
            return "owner_only"
        chat = self.chat()
        if raw["kind"] == UNDO:
            return self.undo(ctx, event, raw, chat)
        topics = raw.get("topics") if isinstance(raw.get("topics"), list) else []
        topics = list(dict.fromkeys(topic for topic in topics if isinstance(topic, str) and topic))[:DISMISS_MAX]
        if not chat or not topics or not dismissable():
            return "overview_dismiss_ignored"
        at = time.time()
        new = dismiss(ctx.state_dir, chat, topics, at, "waiting", "card")
        self.runtime.log(f"pinned overview dismissed topics={len(new)}/{len(topics)}")
        hand, message = self.handed.get(raw.get("from")), str(event.get("message_id") or "")
        if hand is not None and message:
            after_ack(lambda: hand(ctx, message, topics, at), beside=True)
        after_ack(lambda: self.tick(ctx, redraw=True))
        return "overview_dismiss"

    def undo(self, ctx, event, raw, chat):
        """撤销 under 不管了's line (the owner's, checked by ``claim``): the topics its value names, each with the ``at`` it
        was taken off at, put back (app/pins.py undismiss: one taken off again since, or already back because its
        question was answered or retired, is left alone); this card redrawn once the click is acknowledged, and a card
        it came from (``from``, ``unhanded``) too, beside it, with the topics really put back."""
        raw_taken = raw.get("taken") if isinstance(raw.get("taken"), dict) else {}
        taken = {topic: at for topic, at in list(raw_taken.items())[:DISMISS_MAX]
                 if isinstance(topic, str) and topic and isinstance(at, (int, float)) and not isinstance(at, bool)}
        if not chat or not taken:
            return "overview_undismiss_ignored"
        back = undismiss(ctx.state_dir, chat, taken)
        self.runtime.log(f"pinned overview undismissed topics={len(back)}/{len(taken)}")
        hand, message = self.unhanded.get(raw.get("from")), str(event.get("message_id") or "")
        if hand is not None and message:
            after_ack(lambda: hand(ctx, message, list(taken)), beside=True)
        after_ack(lambda: self.tick(ctx, redraw=True))
        return "overview_undismiss"

    def settle(self, ctx, chat):
        """A chat that changed between 'a board of its own' and 'the default one' (``spaces.notify_space``) leaves records
        of the other kind behind, which nothing writes any more: their cards are retired, their pins taken off, the
        records archived (``.moved``). The default board only: a group's own ``fixed`` chat keeps just its own.
        No lock is held over the calls."""
        if self.fixed:
            return
        state = ctx.state_dir
        # The chat's own records, left from when it had a board of its own (it is the default one now).
        for stem in ("pinned-overview", "pinned-topics", "digest"):
            old = Path(state) / f"{stem}.{chat}.json"
            if old.exists() and old != record_path(state, chat, f"{stem}.json"):
                self.release(ctx, old, chat)
        # The default records of another chat (it has a board of its own now, or the notify space moved).
        for name in (RECORD, "pinned-topics.json"):
            path = Path(state) / name
            record = read_object(path)
            if record.get("chat_id") and record["chat_id"] != chat and (name != RECORD or not record.get("message_id")):
                self.release(ctx, path, chat, retire_card=False)

    def release(self, ctx, path, chat, retire_card=True):
        """The record at ``path`` no longer belongs to ``chat``'s board: its pins off and its card retired (a write of
        the card itself, never of the record), then the file archived; left as it is, to be tried again, when an unpin
        failed."""
        record = read_object(path)
        left = []
        for pin in record.get("pins") if isinstance(record.get("pins"), list) else []:
            if isinstance(pin, dict) and pin.get("message_id") and not self.messenger.unpin(ctx.profile, pin["message_id"]):
                left.append(pin)
        if left:
            with resource_lock("pinned-topics", str(path)):
                write_json(path, {**read_object(path), "pins": left})
            self.runtime.log(f"board record {path.name} not released: {len(left)} pins stay")
            return
        if retire_card and record.get("message_id") and record.get("card_id"):
            self.retire(ctx, record)
        with resource_lock("pinned-overview" if "overview" in path.name else "pinned-topics", str(path)):
            os.replace(path, path.with_name(path.name + ".moved"))
        self.runtime.log(f"board record {path.name} released and archived")

    def retire(self, ctx, old):
        """The card ``old`` gave way to a newer one: off the pins, and saying so. Its last write: the sequence
        is not saved, nothing writes it again."""
        unpinned = self.messenger.unpin(ctx.profile, old["message_id"]) if old.get("pinned_at") else None
        written = None
        if old.get("card_id"):
            handle = CardHandle(old["card_id"], lambda sequence: None, old.get("sequence"), shared=False)
            with handle.lock:
                sequence = reserve(handle)
                written = self.messenger.put_card(ctx.profile, old["card_id"], retired_card(), sequence=sequence,
                                                  key=f"overview-{old.get('generation') or 0}-retired", timeout=TIMEOUT)
        self.runtime.log(f"pinned overview retired message={old['message_id'][-8:]} unpinned={unpinned} "
                         f"redrawn={written is not None}")


