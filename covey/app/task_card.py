"""The streaming task card on CardKit (StreamingCard, held by app/cards.py's Cards, the one writer of task
cards): create it, update its answer, status and panels, close it, and replace it when updates keep
failing. What it shows is built by cards/streaming.py."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from covey.cards.cardkit_actions import batch_body, content_body, dumps, settings_body, update
from covey.cards.elements import find_element
from covey.cards.card_cost import CARD_COST_BUDGET, CARD_TOO_BIG, card_cost, text_cost, truncate_card_content
from covey.cards.streaming import (CardLook, SLIM_DETAILS_ID, STOP_ACTION_ELEMENT_ID, build_stream_card,
                                            card_details_text, other_costs)
from covey.cards.task_figures import FIGURES_ELEMENT_ID, IO_BOX_ID, io_figures, io_lines
from covey.cards.task_status import (STATUS_MORE_ID, activity_element, more_element, ran_of, status_elements,
                                              slim_status_markdown, status_markdown, status_rows)
from covey.core import butler
from covey.core.card_sequence import LEASE, adopt, raise_card_sequence, reserve
from covey.core.jobs import job_path, read_job_record
from covey.core.locks import card_lock
from covey.feishu.cardkit import extract_card_id, json_log_summary
from covey.feishu.delivery import refused_outright
from covey.feishu.transport import last_failure

# What the task card is drawn with besides the job's own fields, set when its first card is made
# (app/cards.py create_streaming_reply); a replacement card is drawn with the same.
CARD_FACTS = ("harness_capabilities", "topic_label", "model_label", "workspace_label", "redaction",
              "input_parts")


@dataclass
class StreamingReply:
    card_id: str
    message_id: str
    element_id: str = "answer"
    sequence: int = 0
    last_content: str = ""
    last_update_at: float = 0.0
    update_failures: int = 0  # Consecutive: three disable the card, any count allows a replacement.
    failures_total: int = 0  # Every failed write over the job's cards, for its record.
    disabled: bool = False
    # Disabled by failed writes (not closed or replaced): the run's end still tries its last writes once.
    failed_off: bool = False
    # The card's elements besides the body, by element_id: their card_cost, which the body's room leaves out.
    others: dict = field(default_factory=dict)
    # Below the estimate once CardKit refused a body as over its size: the most card_cost the body takes.
    answer_room: int = 0
    cut: bool = False  # The body shown is cut short (truncate_card_content): the full text goes elsewhere.
    # The run's outcome this card's status line is known to show (Cards.show_outcome), so the result
    # below it need not say it again; '' when it may show something else.
    outcome: str = ""
    # The job record that keeps a ceiling over this card's sequences (core/card_sequence.py).
    record_dir: str = ""
    record_key: str = ""
    saved_sequence: int = 0
    # The body moved up under the status line (Cards.lead_first): elements "after the body" go where it was.
    lead_first: bool = False
    # The chat list's preview of the card (``config.summary``), as it was sent or last changed while its task ran
    # (Cards.show_running_summary), or what it is when the card closes (Cards.show_outcome, written by close);
    # '' leaves the card's own.
    summary: str = ""
    # The status line Cards.show_outcome wrote, with the run's time: ✅ 已完成 · 用时 3 分 39 秒.
    timed_outcome: str = ""
    # 任务详情's text is its own element on this card (cards/streaming.py TASK_DETAILS_TEXT_ID): a running
    # turn's figures replace it in place. False for a card sent before it was, and once such a write is
    # refused: the turn's end then puts the whole panel.
    live_details: bool = False
    # The 任务详情 text this card shows (live_details): the same text is not written again.
    details_text: str = ""
    # The card has 任务详情 (the panel, "task_details"). Since 2026-11-09 a card is sent without it while it has
    # nothing to say (cards/streaming.py task_details_panel); Cards puts it in once it has (Cards.set_task_element,
    # after 本轮请求), and the anchors that were 任务详情 are 本轮请求 until then (``details_anchor``).
    details_panel: bool = True
    # Putting 任务详情 in while the turn ran was refused: not tried again before the turn's end.
    details_refused: bool = False
    # The card has the dashboard look (cards/streaming.py build_stream_card, since 2026-11-04): its status
    # line in a block at the top over the figure tiles (FIGURES_ELEMENT_ID), the body under them, so the
    # body is never moved (Cards.lead_first) and what goes at the bottom goes after 任务详情. False for a
    # card sent before, which keeps its order and anchors.
    dashboard: bool = False
    # The figure tiles this card shows (dashboard), as JSON: the same tiles are not written again.
    figures_text: str = ""
    # The card's status line is in two parts (cards/task_status.py STATUS_MORE_ID, the "A 一行仪表" look, since
    # 2026-11-04 night): the status and its time beside the tiles, the rest under them. False for a card sent
    # before, whose line says it all.
    status_rows: bool = False
    # The rest of the status line this card shows (status_rows), as plain text: unchanged, only the line beside
    # the tiles is written.
    more_text: str = ""
    # The card is the slim one (cards/streaming.py SLIM_DETAILS_ID, the butler's since 2026-11-06): the body,
    # one status line, and 详情 folded with the tiles, 本轮请求, 任务详情 and the buttons; what goes "after the
    # body" or "at the bottom" goes in 详情 (``body_anchor``, ``bottom_anchor``). False for every other card and
    # for one sent before, which keep their order; a release before reads the record and leaves it as an attribute.
    slim: bool = False
    # The card has the grey 输入/输出 box (cards/task_figures.py IO_BOX_ID, the "E" look, since 2026-11-08): its 输出 is
    # FIGURES_ELEMENT_ID, a Markdown element, and on a card that is not slim 已运行 is in it, not on the status line
    # (``update_activity`` writes 输出 alone while the status is the same). False for a card sent before; a release
    # before reads the record, leaves it as an attribute and writes its one line of figures in 输出.
    io_box: bool = False
    # The running turn's 输出 parts but 已运行 (cards/answer_card.py running_figures), as the last process write left
    # them: each status write puts them in 输出 with its 已运行.
    figures_parts: list = field(default_factory=list)
    # The status line's first part this card shows (io_box, not slim), as plain text: not written again unchanged.
    top_text: str = ""

    def save_sequence(self, sequence):
        """Keep the job record's card_sequence at least LEASE ahead of what this card has used."""
        if not self.record_key or sequence <= self.saved_sequence:
            return
        try:
            self.saved_sequence = raise_card_sequence(Path(self.record_dir), self.record_key, sequence + LEASE)
        except OSError:
            pass  # The next write tries again; this one goes ahead as writes always did.


def bind_record(progress, state_dir, key):
    """Keep `progress`'s sequence ceiling in job record `key`, where whoever takes the card over reads it."""
    if isinstance(progress, StreamingReply):
        progress.record_dir, progress.record_key = str(state_dir), key


SEQUENCE_BEHIND = "300317"  # CardKit's "sequence number compare failed": the write was not applied.


def write_live(messenger, profile, progress, method, part, body, *, key, log, **kwargs):
    """One write to the live task card ``progress`` (Messenger.write_card's response, None when
    unconfirmed), its sequence from reserve and its key ``key(sequence)``. Refused with 300317, it was
    not applied: another process wrote the card with a larger sequence since (the bot redrawing it from
    the job record when the next task starts, #1290). It is then written once more, with a fresh
    sequence above the record's card_sequence. Any other failure, a timeout too, may have landed and is
    never written again here. The caller holds the card lock."""
    sequence = reserve(progress)
    payload = messenger.write_card(profile, method, progress.card_id, part, body, sequence=sequence,
                                   key=key(sequence), **kwargs)
    failure = last_failure()
    if payload is not None or failure is None or failure.code != SEQUENCE_BEHIND:
        return payload
    record_key = getattr(progress, "record_key", "")
    record = read_job_record(job_path(Path(progress.record_dir), record_key)) if record_key else None
    if adopt(progress, (record or {}).get("card_sequence")).sequence <= sequence:
        return payload  # Nothing saved is above it: a fresh sequence would be refused the same way.
    log(f"task card write behind another writer's sequence card_id={progress.card_id} "
        f"sequence={sequence}: once more above {progress.sequence}")
    sequence = reserve(progress)
    return messenger.write_card(profile, method, progress.card_id, part, body, sequence=sequence,
                                key=key(sequence), **kwargs)


def body_anchor(progress: Any) -> str:
    """The element that what goes "after the body" is inserted after: the body, or, once the body moved
    up to the top (``lead_first``), 任务详情, the element it was under, so those keep their place. On a slim card
    the tiles, the first of its 详情: what goes after the body goes in there (the process panel, 最新补充,
    产物, the butler's own words), above 本轮请求; on one with the 输入/输出 box, after the box (after its 输出
    text it would go in the box)."""
    if getattr(progress, "slim", False):
        return IO_BOX_ID if getattr(progress, "io_box", False) else FIGURES_ELEMENT_ID
    return details_anchor(progress) if getattr(progress, "lead_first", False) else progress.element_id


def details_anchor(progress: Any) -> str:
    """任务详情 (``task_details``) when the card has it, else 本轮请求 above where it goes (``details_panel``)."""
    return "task_details" if getattr(progress, "details_panel", True) else "current_question"


def under_body(progress: Any) -> str:
    """What an element that must show without opening anything goes after: the body itself. On a slim card that is
    a failed turn's 重试 / 检查并继续 row, the butler's list of dropped supplements and a question on the card;
    on any other card the same place as ``body_anchor``."""
    return progress.element_id if getattr(progress, "slim", False) else body_anchor(progress)


def bottom_anchor(progress: Any, panel_shown: bool) -> str:
    """What the buttons of a finished turn go after (``set_task_element(at_bottom=True)``): 任务详情 on a
    dashboard card (a slim one too: inside its 详情), the last of its fixed parts once its action row is gone; on a
    card sent before, the process panel when it shows one (``panel_shown``), else the body's place."""
    if getattr(progress, "dashboard", False) or getattr(progress, "slim", False):
        return details_anchor(progress)
    return "process_panel" if panel_shown else body_anchor(progress)


def note_element(progress: Any, element: dict, cost: int | None = None) -> None:
    """``element`` is on the card now (``cost``: its card_cost, else measured): the body's room leaves it out."""
    others = getattr(progress, "others", None)
    if isinstance(others, dict) and element.get("element_id"):
        others[element["element_id"]] = card_cost(element) if cost is None else cost


def drop_element(progress: Any, element_id: str) -> None:
    """``element_id`` left the card: the body's room no longer leaves it out."""
    others = getattr(progress, "others", None)
    if isinstance(others, dict):
        others.pop(element_id, None)


def status_actions(content: str, rows: bool, slim: bool = False, io: bool = False) -> list[dict[str, Any]]:
    """The batch actions putting the status line ``content`` on a card (cards/task_status.py status_elements:
    in two parts when ``rows``, one line on a ``slim`` card, without 已运行 with the 输入/输出 box, ``io``)."""
    return [update(element) for element in status_elements(content, rows, slim, io)]


def io_card(progress: Any) -> bool:
    """Whether ``progress``'s card has 已运行 in its 输入/输出 box, not on its status line (io_box, not slim)."""
    return bool(getattr(progress, "io_box", False)) and not getattr(progress, "slim", False)


def took_status(progress: Any, content: str) -> None:
    """``progress``'s card shows the status line ``content`` now (``status_actions``): its parts' size, and the
    rest of the line under the tiles (``more_text``)."""
    rows, io = getattr(progress, "status_rows", False), io_card(progress)
    for element in status_elements(content, rows, getattr(progress, "slim", False), io):
        note_element(progress, element)
    if rows:
        progress.top_text, progress.more_text = status_rows(content, io)


def running_figures(progress: Any, content: str) -> dict | None:
    """The 输出 text (FIGURES_ELEMENT_ID) of an ``io_card`` while its turn runs: its last parts (``figures_parts``)
    with the 已运行 of the status line ``content``; None when it shows that already, or before the run started."""
    ran = ran_of(content)
    if not ran:
        return None
    element = io_figures(io_lines(getattr(progress, "figures_parts", None) or (), ran))
    return None if dumps(element) == getattr(progress, "figures_text", "") else element


MIN_BODY_COST = 2000  # A body this small refused as too big: the rest of the card is, not the body.


def reserved_cost(progress: Any) -> int:
    """What the body leaves of the card's size budget (``reserved`` of truncate_card_content): the
    card's other elements, or more once CardKit refused a body (``answer_room``)."""
    others = getattr(progress, "others", None)
    reserved = sum(others.values()) if isinstance(others, dict) else 0
    room = getattr(progress, "answer_room", 0)
    return max(reserved, CARD_COST_BUDGET - room) if room else reserved


def count_update_failure(progress: Any) -> None:
    """One failed write to the task card: the consecutive count and the job's total."""
    progress.update_failures = getattr(progress, "update_failures", 0) + 1
    progress.failures_total = getattr(progress, "failures_total", 0) + 1


def card_error(failure, before: str) -> str:
    """A task card's failed send as the job's ``last_card_error``: Feishu's code, category and words, after
    ``before`` (an earlier failure of the same card)."""
    if failure is None:
        said = "任务卡没有发出去"
    else:
        said = "任务卡发送失败（飞书 " + " ".join(filter(None, [failure.code, failure.category,
                                                            failure.detail[:80]])) + "）"
    return f"{before}；{said}" if before and before != said else said


def card_failure_notice(job: Any) -> str:
    """What the user is told when a task has no card: it still runs, but without its controls."""
    failure = last_failure()
    reason = ""
    if failure is not None:
        reason = "（飞书返回 " + " ".join(filter(None, [failure.code or failure.category, failure.detail[:80]])) + "）"
    return (f"任务 #{job.job_id} 已收到，但任务卡没有发出去{reason}。任务照常执行，执行中会再补发任务卡，"
            "结果会单独发回本话题；补发成功之前，无法在飞书里停止它、查看执行过程或回答它的提问。")


def card_look(job: Any, steers: bool = False) -> CardLook:
    """How ``job``'s task card is drawn (cards/streaming.py build_stream_card): 补充 when its Harness steers (its
    recorded capabilities, else ``steers``: a Codex job recorded without them), 换配置 for a job with a Harness
    in a topic that offers it (none on the butler), and the butler's slim look (core/butler.py
    Traits)."""
    traits = butler.traits_of(job)
    return CardLook(steer=getattr(getattr(job, "harness_capabilities", None), "steering", steers),
                    switch=bool(getattr(job, "harness_id", "")) and traits.switch_config,
                    slim=traits.slim_card)


@dataclass(frozen=True)
class StreamingCard:
    """One Harness's task-card writes on CardKit, and what cards/streaming.py builds the card with."""

    idempotency_prefix: str
    default_title: str
    element_id: str
    progress_factory: Callable[..., Any]
    log: Callable[[str], None]
    safe_idempotency_key: Callable[[str], str]
    send_text_message: Callable[[str | None, str, str, str], bool]
    compact_text: Callable[[str, int], str]
    messenger: Any  # the Messenger: CardKit writes
    send_card: Callable[..., Any]  # Messenger.send_card in the Harness's voice: one card message
    card_limit: int = 28000  # The most characters of a body before it is cut.
    update_interval: float = 1.0  # Seconds between two unforced body writes.
    replacement_limit: int = 1  # Replacement cards a job may get before it goes on in text.
    overflow_suffix: str = "\n\n[内容较长，卡片内已截断；完整内容会以 Markdown 文件发到本话题，文件发送失败时改为分段消息。]"

    def look(self, job: Any) -> CardLook:
        """``job``'s card_look; a job recorded without capabilities steers when this is Codex's ("codex-lark")."""
        return card_look(job, self.idempotency_prefix == "codex-lark")

    def create(self, profile: str | None, job: Any, initial_content: str, *,
               include_stop_action: bool = True) -> Any | None:
        essential = False
        for attempt in range(1, 4):
            card = build_stream_card(
                initial_content,
                job.title or job.user_text,
                self,
                job=job,
                include_stop_action=include_stop_action and str(job.kind or "chat") == "chat",
                essential=essential,
            )
            payload = self.messenger.create_card(profile, card)
            card_id = extract_card_id(payload or {})
            if payload:
                self.log(
                    f"cardkit create attempt={attempt} "
                    f"card_id={card_id or '(missing)'} response={json_log_summary(payload, 2000)}"
                )
            if not card_id:
                if essential:
                    return None
                # One optional control CardKit rejects (an over-long element ID left
                # T27 #424 without a card, 2026-10-22) must not cost the task its card.
                self.log(f"task card rejected job=#{job.job_id}; retrying with only the stop control")
                essential = True
                continue
            key = f"{self.idempotency_prefix}-stream-card-{job.message_id}-{attempt}"
            message_id, retry_invalid_card = self.send_card(
                profile,
                job.chat_id,
                card_id,
                key,
                reply_to_message_id=job.reply_to_message_id,
            )
            if message_id:
                return self._progress(card, card_id, message_id)
            if not retry_invalid_card:
                message_id = self._send_elsewhere(profile, job, card_id, key)
                return self._progress(card, card_id, message_id) if message_id else None
            self.log(f"retrying streaming card create/send after invalid card_id card_id={card_id}")
        return None

    def _send_elsewhere(self, profile, job, card_id, key) -> str:
        """The card ``card_id`` once its reply to ``job.reply_to_message_id`` was refused outright (a definite
        failure, never one that may have landed, rule 1): in reply once more under a key of its own (Feishu's
        2200 "Something went wrong" refused #3560's card at 13:46 on 2026-11-10 and took a text reply to the same
        message a second later), then at the chat's top level. Each failure is the job's ``last_card_error``
        (its record keeps it); the message ID, '' when none went out."""
        failure = last_failure()
        if not job.reply_to_message_id or not refused_outright(failure):
            job.last_card_error = card_error(failure, "")
            return ""
        refused = card_error(failure, "")
        for suffix, target, where in (("again", job.reply_to_message_id, "回复重发"),
                                      ("top", "", "改发到会话顶层")):
            message_id, _ = self.send_card(profile, job.chat_id, card_id, f"{key}-{suffix}",
                                           reply_to_message_id=target)
            if message_id:
                job.last_card_error = f"{refused}；{where}成功"
                self.log(f"task card sent after a refused reply job=#{job.job_id} to={suffix} "
                         f"card_id={card_id} message_id={message_id}")
                return message_id
            if not refused_outright(last_failure()):
                break  # It may have landed: nothing more is sent (rule 1).
        job.last_card_error = card_error(last_failure(), refused)
        return ""

    def _progress(self, card, card_id, message_id, **fields):
        """The live card ``card`` (its JSON as sent) as ``progress_factory`` keeps it."""
        body = next(e for e in card["body"]["elements"] if e.get("element_id") == self.element_id)
        details = card_details_text(card)
        figures = find_element(card, FIGURES_ELEMENT_ID)
        return self.progress_factory(
            card_id=card_id,
            message_id=message_id,
            last_content=body["content"],
            others=other_costs(card, self.element_id),
            live_details=details is not None,
            details_text=details or "",
            details_panel=find_element(card, "task_details") is not None,
            summary=card.get("config", {}).get("summary", {}).get("content", ""),
            dashboard=figures is not None,
            figures_text=dumps(figures) if figures else "",
            status_rows=find_element(card, STATUS_MORE_ID) is not None,
            slim=find_element(card, SLIM_DETAILS_ID) is not None,
            io_box=find_element(card, IO_BOX_ID) is not None,
            **fields,
        )

    def update(self, profile: str | None, progress: Any, content: str, *, force: bool = False) -> bool:
        with card_lock(progress):
            if progress.disabled:
                return False
            full = content
            content = truncate_card_content(full, self, reserved_cost(progress))
            now = time.monotonic()
            if content == progress.last_content:
                return True
            if not force and now - progress.last_update_at < self.update_interval:
                return False

            for _ in range(2):  # A body refused as over the card's size is written again, shorter.
                payload = write_live(
                    self.messenger, profile, progress, "PATCH", f"elements/{progress.element_id}",
                    content_body(content), log=self.log,
                    key=lambda seq: self.safe_idempotency_key(f"{self.idempotency_prefix}-stream-{progress.message_id}-{seq}"),
                )
                if payload:
                    progress.last_content = content
                    progress.last_update_at = now
                    progress.update_failures = 0
                    progress.cut = content != full
                    return True
                failure = last_failure()
                if failure is None or failure.code != CARD_TOO_BIG:
                    break
                # Over CardKit's size, not a broken card: a shorter body, at once, which does not count
                # toward disabling the card (#1150 stayed at 执行中 after three of these). A body that
                # cannot get shorter is refused for the rest of the card: an ordinary failure.
                progress.answer_room = max(0, text_cost(content) * 3 // 5)
                self.log(f"task card body over CardKit's size card_id={progress.card_id} "
                         f"cost={text_cost(content)} room={progress.answer_room}")
                shorter = truncate_card_content(full, self, reserved_cost(progress))
                if shorter == content or text_cost(content) <= MIN_BODY_COST:
                    break
                progress.failures_total = getattr(progress, "failures_total", 0) + 1
                content = shorter
            else:
                return False

            count_update_failure(progress)
            if progress.update_failures >= 3:
                progress.disabled = progress.failed_off = True
            return False

    def update_activity(self, profile: str | None, progress: Any, content: str) -> bool:
        """Update a separate status element, never rewriting the answer or panels; ``content`` is the
        producer's plain text, styled here (cards/task_status.py status_markdown). On a card whose line is in
        two parts (``status_rows``) the part beside the tiles alone while the rest is the same (每秒的 已运行),
        else both in one batch. On a card with 已运行 in its 输入/输出 box (``io_card``) what changed of the status,
        the rest of the line and 输出, 输出 alone most of the time: one write either way (DESIGN-cardio §4)."""
        with card_lock(progress):
            if progress.disabled:
                return False
            if io_card(progress):
                return self._update_io(profile, progress, content)
            top, more = status_rows(content) if getattr(progress, "status_rows", False) else (content, None)
            if more is None or more == progress.more_text:
                styled = slim_status_markdown(top) if getattr(progress, "slim", False) else status_markdown(top)
                return bool(write_live(self.messenger, profile, progress, "PATCH", "elements/activity",
                    content_body(styled), log=self.log,
                    key=lambda seq: self.safe_idempotency_key(f"activity-{progress.message_id}-{seq}"), timeout=10))
            actions = status_actions(content, True)
            if not write_live(self.messenger, profile, progress, "POST", "batch_update", batch_body(actions), log=self.log,
                              key=lambda seq: self.safe_idempotency_key(f"activity-{progress.message_id}-{seq}"),
                              timeout=10):
                return False
            took_status(progress, content)
            return True

    def _update_io(self, profile, progress, content):
        """``update_activity`` on an ``io_card``: the status (``activity``), the rest of the line (STATUS_MORE_ID)
        and 输出 with its 已运行, each when it changed, in one write. The caller holds the card lock."""
        top, more = status_rows(content, True)
        figures = running_figures(progress, content)
        elements = [*([activity_element(top)] if top != progress.top_text else ()),
                    *([more_element(more)] if more != progress.more_text else ()),
                    *([figures] if figures is not None else ())]
        if not elements:
            return True
        def key(seq):
            return self.safe_idempotency_key(f"activity-{progress.message_id}-{seq}")
        if elements == [figures]:
            ok = write_live(self.messenger, profile, progress, "PATCH", f"elements/{FIGURES_ELEMENT_ID}",
                            content_body(figures["content"]), log=self.log, key=key, timeout=10)
        else:
            ok = write_live(self.messenger, profile, progress, "POST", "batch_update",
                            batch_body([update(element) for element in elements]), log=self.log, key=key, timeout=10)
        if not ok:
            return False
        progress.top_text, progress.more_text = top, more
        if figures is not None:
            progress.figures_text = dumps(figures)
        for element in elements:
            note_element(progress, element)
        return True

    def delete_stop_action(self, profile: str | None, progress: Any) -> bool:
        with card_lock(progress):
            if progress.disabled:
                return False
            payload = write_live(
                self.messenger, profile, progress, "DELETE", f"elements/{STOP_ACTION_ELEMENT_ID}", {}, log=self.log,
                key=lambda seq: self.safe_idempotency_key(
                    f"{self.idempotency_prefix}-stream-delete-stop-{progress.message_id}-{seq}"),
            )
            if payload:
                drop_element(progress, STOP_ACTION_ELEMENT_ID)
                return True
            self.log(f"failed to delete stop action card_id={progress.card_id}")
            return False

    def close(self, profile: str | None, progress: Any) -> None:
        """Streaming off, and, when the run left one (``progress.summary``), the chat list's preview of
        the card, in the same write: a sent card's header cannot change in place (only a whole-card PUT
        changes it, and task cards are never replaced whole), but its summary can, and the list shows
        that instead of the header (T288: topics were hard to tell apart by their questions)."""
        with card_lock(progress):
            if progress.disabled:
                return
            config = {"streaming_mode": False}
            if getattr(progress, "summary", ""):
                config["summary"] = {"content": progress.summary}
            payload = write_live(
                self.messenger, profile, progress, "PATCH", "settings",
                settings_body(config), log=self.log,
                key=lambda seq: self.safe_idempotency_key(
                    f"{self.idempotency_prefix}-stream-close-{progress.message_id}-{seq}"),
            )
            if not payload:
                self.log(f"failed to close streaming mode card_id={progress.card_id}")
            else:  # Logged too: a card a chat list still shows as [生成中...] can then be traced.
                self.log(f"closed streaming mode card_id={progress.card_id}")

    def replace_card(
        self,
        profile: str | None,
        job: Any,
        content: str,
        reason: str,
        idempotency_prefix: str,
        *,
        record_replacement: Callable[[Any, str], None] | None = None,
    ) -> bool:
        job.last_card_error = reason
        if job.stream_replacements >= self.replacement_limit:
            job.progress = None
            self.send_text_message(
                profile,
                job.chat_id,
                f"{reason}；已达到替换卡上限，后续改用普通消息兜底。任务仍在后台继续执行，完成后会直接发送结果。",
                f"{idempotency_prefix}-card-fallback-limit-{job.message_id}-{int(time.time() * 1000)}",
                **({"reply_to_message_id": job.reply_to_message_id} if job.reply_to_message_id else {}),
            )
            return False
        if job.progress:
            with card_lock(job.progress):
                job.progress.disabled, job.progress.failed_off = True, False
        replacement_job = dataclasses.replace(job, message_id=f"{job.message_id}-replacement-{int(time.time() * 1000)}")
        # replace() leaves init=False fields at their defaults and drops attributes set on the job.
        for name in CARD_FACTS:
            if hasattr(job, name):
                setattr(replacement_job, name, getattr(job, name))
        replacement = self.create(profile, replacement_job, content)
        if replacement:
            bind_record(replacement, getattr(job.progress, "record_dir", ""), getattr(job.progress, "record_key", ""))
            replacement.failures_total = getattr(job.progress, "failures_total", 0)
            job.stream_replacements += 1
            job.progress = replacement
            if record_replacement is not None:
                record_replacement(job, reason)
            return True
        job.progress = None
        self.send_text_message(
            profile,
            job.chat_id,
            f"{reason}；创建替换卡失败，已切换为普通消息兜底。任务仍在后台继续执行，完成后会直接发送结果。",
            f"{idempotency_prefix}-card-fallback-{job.message_id}-{int(time.time() * 1000)}",
            **({"reply_to_message_id": job.reply_to_message_id} if job.reply_to_message_id else {}),
        )
        return False

    def update_job(
        self,
        profile: str | None,
        job: Any,
        content: str,
        *,
        force: bool = False,
        idempotency_prefix: str,
        record_replacement: Callable[[Any, str], None] | None = None,
        allow_replacement: bool = True,
    ) -> bool:
        """The job's card body; a failed write replaces the card (``allow_replacement``)."""
        progress = job.progress
        if not progress:
            return False
        if self.update(profile, progress, content, force=force):
            return True
        if progress.update_failures <= 0 or not allow_replacement:
            return False
        return self.replace_card(profile, job, content, "原流式卡片更新失败，已创建替换卡继续展示进度",
                                 idempotency_prefix, record_replacement=record_replacement)
