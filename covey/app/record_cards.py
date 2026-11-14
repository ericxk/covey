"""Writes to a job's card from its record once its own writer has ended (finished, or the job is in another
process), and to browse cards (question history, trace pages) by their saved address: RecordCards, a part of
app/cards.py's Cards, which keeps their methods (update_record_panel, repaint_browse_card, replace_record_card,
update_record_elements, set_record_summary, set_record_activity) as its own."""
from __future__ import annotations

import functools

from covey.app.task_card import note_element
from covey.cards.cardkit_actions import content_body, element_body, settings_body
from covey.cards.task_status import slim_status_markdown, status_markdown
from covey.core import main_thread
from covey.core.card_sequence import file_card, job_card, reserve
from covey.core.locks import resource_lock
from covey.core.state import read_json


class RecordCards:
    """Card writes by record or saved address for ``cards``: its messenger, voice and job registry."""

    def __init__(self, cards) -> None:
        self.cards = cards

    def _browse_file(self, ctx, source):
        """Where the address of the browse card message ``source`` is saved (Messenger.publish_card)."""
        return ctx.state_dir / "browse-cards" / (self.cards.voice.safe_key(source) + ".json")

    def _find_record_card(self, ctx, chat, event, record):
        """(card_id, browse file or None) of the card ``event`` was clicked on, a job's task card or a browse card
        of this topic and chat. Raises ValueError for a browse card it cannot place."""
        source = event.get("message_id", "")
        if source == record.get("card_message_id"):
            return record.get("card_id"), None
        mapping = self._browse_file(ctx, source)
        saved = read_json(mapping) if mapping.exists() else {}
        if saved.get("topic_id") != ctx.topic_id or saved.get("chat_id") != chat:
            raise ValueError("原卡片无法定位，请重新打开任务卡片")
        return saved.get("card_id"), mapping

    @staticmethod
    def _open_record_card(ctx, record, cid, mapping, live):
        """The card ``_find_record_card`` found, opened from what is saved now, above the sequences ``live``
        (its live writer's). The caller holds the card's lock: a second click may have waited behind the first
        network update."""
        if mapping:
            return file_card(mapping, read_json(mapping), also=(record.get("card_sequence"), *live))
        return job_card(ctx.state_dir, record["key"], cid, *live)

    def update_record_panel(self, ctx, chat, mid, event, record, panel):
        """Replace ``panel`` (by element_id) on the card ``event`` was clicked on, a job's task card or a
        browse card. Raises ValueError when CardKit did not take it."""
        cid, mapping = self._find_record_card(ctx, chat, event, record)
        if not cid:
            raise ValueError("原卡片不存在")
        # Card ownership outlives backend control (questions and finalization).
        # Using runtime.jobs.live_job here would exclude a paused question and
        # advance only the persisted sequence, breaking the live writer.
        job = next((j for j in self.cards.runtime.jobs.topic_jobs(ctx.topic_id, chat) if j.job_id == record["job_id"]), None)
        progress = job.progress if job and job.progress and job.progress.card_id == cid else None
        with resource_lock("card", cid):
            live = (progress.sequence,) if progress else ()
            sequence = reserve(self._open_record_card(ctx, record, cid, mapping, live))
            if progress:
                progress.sequence = sequence  # Its live writer goes on from here, whatever this update's outcome.
            result = self.cards.messenger.write_card(ctx.profile, "PUT", cid, f"elements/{panel['element_id']}",
                                               element_body(panel),
                                               sequence=sequence, key=self.cards.voice.key("trace-page", mid))
            if result is None:
                raise ValueError("翻页更新失败，请在原卡片重试")
            if progress:
                note_element(progress, panel)
            if progress and panel["element_id"] == "process_panel":
                job.trace_rendered_panel = panel
                job.trace_rendered_card = cid

    def repaint_browse_card(self, ctx, chat, mid, source, card):
        mapping = self._browse_file(ctx, source)
        try:
            saved_card = read_json(mapping)
            with resource_lock("card", saved_card["card_id"]):
                self.cards.messenger.publish_card(ctx, chat, mid, card, source_message=source, update_only=True, voice=self.cards.voice)
            return True
        except Exception as exc:  # noqa: BLE001 - the delivery state is already saved
            self.cards.runtime.log(f"browse card update failed: {type(exc).__name__}")
            return False

    def _record_write(self, ctx, record, tag, send):
        """One write to the card of a finished job's ``record``, ``send(sequence=, key=)``, under the card's lock;
        whether CardKit took it, False without a card. Its sequence is saved in the record before the write goes
        out: an unconfirmed one never lends its sequence."""
        if not record.get("card_id"):
            return False
        with resource_lock("card", record["card_id"]):
            sequence = reserve(job_card(ctx.state_dir, record["key"], record["card_id"]))
            return send(sequence=sequence, key=self.cards.voice.key(tag, f"{record['key']}-{sequence}")) is not None

    def _record_part(self, ctx, record, tag, method, part, body):
        """``_record_write`` of ``body`` to ``part`` of the card."""
        return self._record_write(ctx, record, tag, functools.partial(self.cards.messenger.write_card, ctx.profile, method,
                                                                      record.get("card_id"), part, body))

    def update_record_elements(self, ctx, record, elements):
        """Replace ``elements`` (by element_id) on a finished job's card in place; whether CardKit
        took every one. For the Bridge once the job's own writer has ended."""
        if not record.get("card_id"):
            return False
        ok = True
        with resource_lock("card", record["card_id"]):
            for element in elements:
                ok = self._record_part(ctx, record, "record-element", "PUT", f"elements/{element['element_id']}",
                                       element_body(element)) and ok
        return ok

    def set_record_summary(self, ctx, record, summary):
        """``summary`` as the chat-list preview of a finished job's card (``config.summary``); whether CardKit took it."""
        return self._record_part(ctx, record, "record-summary", "PATCH", "settings",
                                 settings_body({"summary": {"content": summary}}))

    def set_record_activity(self, ctx, record, content):
        """Put ``content`` in the status line at the top of a finished job's card (``activity``,
        the run's outcome once the job ended, styled as every status line, ``status_markdown``); whether
        CardKit took it. The line alone on a card whose line is in two parts too (STATUS_MORE_ID): what comes
        here (↪ 已回答 → #N, the stale note) has nothing for under the tiles, and the run's end emptied it. On the
        main thread's card as its slim line (``slim_status_markdown``; the record does not say which look its card has,
        and either line reads right on the other)."""
        traits = main_thread.traits_of(record)
        styled = slim_status_markdown(content) if traits.slim_card else status_markdown(content)
        return self._record_part(ctx, record, "record-activity", "PATCH", "elements/activity", content_body(styled))

    def replace_record_card(self, ctx, record, card):
        return self._record_write(ctx, record, "record-card",
                                  functools.partial(self.cards.messenger.put_card, ctx.profile, record.get("card_id"), card))
