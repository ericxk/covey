"""任务详情 and the process panel (执行过程) on a job's live task card, and the dashboard card's tiles or the
输入/输出 box's 输出 that go with them: TaskDetails, a part of app/cards.py's Cards (the only writer of task
cards), which reaches it as ``Cards.details``; queue_trace, trace_updates and finalize_trace are also Cards'."""
from __future__ import annotations

import copy

from covey.core.updates import CoalescedUpdates
from covey.cards.cardkit_actions import dumps, update
from covey.cards.streaming import task_details_panel
from covey.cards.task_figures import compact_figures, figures_element, io_figures, io_lines
from covey.cards.task_status import ran_shown
from covey.cards import answer_card
from covey.core.trace import ToolTrace
from covey.cards.trace_updates import incremental_actions, panel_json
from covey.cards.trace_view import TraceView
from covey.app.task_card import note_element, panel_room
from covey.core.jobs import upsert_job_record
from covey.core.locks import card_lock
from covey.core.metrics import run_seconds, run_start, took_text
from covey.core.trace_store import save_trace


class TaskDetails:
    """The process panel and 任务详情 of ``cards``' task cards: their writes go through ``cards`` (``_batch``,
    ``set_task_element``) under the card lock its callers hold."""

    def __init__(self, cards) -> None:
        self.cards = cards

    def set_process_panel(self, ctx, job, panel, details=None, figures=None):
        """The process panel (``panel_json``): what changed since the panel this card shows, in one batch, else drawn
        whole. ``details``: a running turn's 任务详情 (``_live_details``), its text in that batch, or on
        its own after a whole panel; ``figures``: a dashboard card's tiles (``card_figures``), the same way. A
        batch without a change to the panel is not written for them alone (the token figures change with
        nearly every event, the steps and the title's rounded tokens far less often)."""
        if not job.progress:
            return self.cards.set_task_element(ctx, job, panel)
        previous = job.trace_rendered_panel
        if job.trace_rendered_card != job.progress.card_id:
            previous = None
        if previous is None:
            ok = self.cards.set_task_element(ctx, job, panel)
            if ok and (details is not None or figures is not None):
                self.write_elements(ctx, job, [details] if details is not None else [], figures)
        else:
            actions = incremental_actions(previous, panel)
            if not actions:
                return True
            if details is not None:
                actions.append(self._details_action(details))
            if figures is not None:
                actions.append(update(figures))
            ok = self.cards._batch(ctx, job, actions, "trace-update") is not None
            if ok and details is not None:
                self._took_details(job, details)
            if ok and figures is not None:
                self.took_figures(job, figures)
            if not ok:
                self.cards._write_failed(job, "Failed to update process content")
        job.trace_rendered_panel = panel if ok else None
        job.trace_rendered_card = job.progress.card_id
        if ok:
            note_element(job.progress, panel)
        return ok

    @staticmethod
    def _live_details(job, snapshot):
        """任务详情 with ``snapshot``'s figures so far, when this card takes its text alone (live_details)
        and shows other text, or has no 任务详情 yet (``details_panel``) and it has words now; else None. One
        with nothing to say (task_details_panel None) is never written: a card sent by an older release keeps
        the words it shows."""
        progress = job.progress
        if not getattr(progress, "live_details", False) and getattr(progress, "details_panel", True):
            return None
        panel = task_details_panel(job, snapshot)
        if panel is None or not getattr(progress, "details_panel", True):
            return panel
        return None if panel["elements"][0]["content"] == progress.details_text else panel

    @staticmethod
    def card_figures(job, snapshot, took=None, ended=False):
        """A dashboard card's tiles with ``snapshot``'s figures (``took``: the run's seconds, else the
        snapshot's), when they differ from those it shows; else None. An ended run's 用时 is by its timings
        (Cards._took), as its status line and answer card say it. ``ended``: the run ended (show_outcome), with
        or without a process record."""
        progress = job.progress
        if not getattr(progress, "dashboard", False):
            return None
        timings = getattr(job, "timings", None)
        if took is None and isinstance(snapshot, dict) and snapshot.get("ended_at") is not None:
            took = run_seconds(timings, snapshot)
        if getattr(progress, "io_box", False):
            element = TaskDetails._io_figures(job, snapshot, took, ended)
        else:
            element = figures_element(snapshot, took, compact=compact_figures(getattr(progress, "figures_text", "")))
        return None if dumps(element) == getattr(progress, "figures_text", "") else element

    @staticmethod
    def _io_figures(job, snapshot, took, ended=False):
        """输出 of a card with the 输入/输出 box (cards/task_figures.py io_box): an ended run's as its answer card's
        (用时 · token/s / token · 缓存命中, ``took`` its seconds); a running one's parts kept for the status line's
        writes (StreamingReply.figures_parts) and drawn with 已运行 as long as it has run (none on a slim card).
        A run that ended (``ended``, or its snapshot says so) without a time or a record (a queued task cancelled,
        a turn that never reached the backend) has no 开始 or 已运行: "—", as the box drew it before the run."""
        progress, timings = job.progress, getattr(job, "timings", None)
        ended = ended or (isinstance(snapshot, dict) and snapshot.get("ended_at") is not None)
        if took is not None or ended:
            return io_figures(answer_card.output_lines(answer_card.output_parts(snapshot, took)))
        progress.figures_parts = answer_card.running_figures(snapshot, run_start(timings))
        if getattr(progress, "slim", False):
            return io_figures(io_lines(progress.figures_parts, slim=True))
        ran = run_seconds(timings, snapshot)
        return io_figures(io_lines(progress.figures_parts, ran_shown(took_text(ran)) if ran is not None else ""))

    @staticmethod
    def took_figures(job, element):
        job.progress.figures_text = dumps(element)
        note_element(job.progress, element)

    def write_elements(self, ctx, job, details, figures):
        """任务详情's text (``details``: [panel] or []) and the tiles (``figures``, or None) alone, in one
        batch; whether CardKit took it. A failed write ends live 任务详情 on this card (``_write_details``).
        The caller holds the card lock."""
        if not details:
            if figures is None:
                return True
            ok = self.cards._batch(ctx, job, [update(figures)], "task-figures") is not None
            if ok:
                self.took_figures(job, figures)
            return ok
        return self._write_details(ctx, job, details[0], figures)

    @staticmethod
    def _details_action(panel):
        """The batch action putting 任务详情 ``panel``'s text in place of the card's (TASK_DETAILS_TEXT_ID)."""
        return update(panel["elements"][0])

    @staticmethod
    def _took_details(job, panel):
        job.progress.details_text = panel["elements"][0]["content"]
        note_element(job.progress, panel)

    def _write_details(self, ctx, job, panel, figures=None):
        """任务详情 ``panel``'s text alone in place of the card's (with a dashboard card's tiles, ``figures``);
        whether CardKit took it. A failed write ends live 任务详情 on this card, so it is never retried with
        every event: the turn's end puts the whole panel. The caller holds the card lock."""
        actions = [self._details_action(panel)] + ([update(figures)] if figures is not None else [])
        if self.cards._batch(ctx, job, actions, "task-details") is None:
            job.progress.live_details = False
            self.cards.runtime.log(f"task details not taken alone card_id={job.progress.card_id}: the whole panel at the end")
            return False
        self._took_details(job, panel)
        if figures is not None:
            self.took_figures(job, figures)
        return True

    def set_task_details(self, ctx, job, snapshot):
        """任务详情 with the ended turn's figures and time: only its text on a card that takes it alone
        (live_details), so a panel the user opened stays open; else, or when that write fails, the whole
        panel (put in under 本轮请求 on a card without one, set_task_element); none when it has nothing to say. A dashboard card's tiles go with it, unless they show these figures already."""
        panel = task_details_panel(job, snapshot)
        figures = self.card_figures(job, snapshot, ended=True)
        if panel is None:
            # Nothing to say: a card without 任务详情 stays without, one sent before keeps its words.
            return self.write_elements(ctx, job, [], figures)
        if getattr(job.progress, "live_details", False):
            if panel["elements"][0]["content"] == job.progress.details_text:
                return self.write_elements(ctx, job, [], figures)
            if self._write_details(ctx, job, panel, figures):
                return True
        ok = self.cards.set_task_element(ctx, job, panel)
        if figures is not None:
            self.write_elements(ctx, job, [], figures)
        return ok

    def trace_updates(self, job):
        if job.card_updates is None:
            job.card_updates = CoalescedUpdates(self.cards.runtime.log)
        return job.card_updates

    def queue_trace(self, ctx, job, urgent=False):
        if job.trace_card_updates is None:
            # Separate, so answers, questions and control receipts retain their own low-latency queue.
            job.trace_card_updates = CoalescedUpdates(self.cards.runtime.log)
        def flush():
            with job.trace_lock:
                snapshot = copy.deepcopy(job.tool_trace.snapshot())
            trace = ToolTrace.restore(snapshot)

            def stale():
                # Taken before the trace ended (show_outcome may have drawn the end): not drawn over it.
                return trace.ended_at is None and job.tool_trace.ended_at is not None
            if stale():
                return
            save_trace(ctx.state_dir, job.message_id, snapshot)
            if job.trace_step_count != trace.total:
                job.trace_step_count = trace.total
                upsert_job_record(ctx.state_dir, job.message_id, tool_step_count=trace.total)
            with card_lock(job.progress):
                if stale():
                    return
                running = trace.ended_at is None
                panel = panel_json(TraceView(trace.snapshot()).panel_model(running=running, cost=panel_room(job.progress)))
                # A running turn's 任务详情 goes with its panel: once 0a26e8e took the model's figures off
                # the running panel's title, a running card showed them nowhere.
                details = self._live_details(job, snapshot) if running else None
                if details is not None and not getattr(job.progress, "details_panel", True):
                    # 任务详情 has words for the first time (a subtask, redaction's count): the panel goes in, tried
                    # once while the turn runs (not with every event); its end tries again (set_task_details).
                    if not getattr(job.progress, "details_refused", False) and not self.cards.set_task_element(ctx, job, details):
                        job.progress.details_refused = True
                    details = None
                self.set_process_panel(ctx, job, panel, details, self.card_figures(job, snapshot) if running else None)
                if not running:
                    self.set_task_details(ctx, job, snapshot)
        terminal = job.tool_trace.ended_at is not None
        return job.trace_card_updates.submit("trace", flush, terminal or urgent)

    def finalize_trace(self, ctx, job, stopped=False):
        """End the job's trace for its final card; ``stopped``: the user stopped the task."""
        trace = job.tool_trace
        if trace is None:
            return False
        self.cards._end_trace(ctx, job, stopped)
        upsert_job_record(ctx.state_dir, job.message_id, tool_step_count=trace.total)
        self.queue_trace(ctx, job, urgent=True)
        trace_closed = job.trace_card_updates.close()
        return self.trace_updates(job).close() and trace_closed
