"""A turn's 用时 is one figure on every card (C8/V7, 2026-11-08): from the backend's request to the end of its
execution (core/metrics.py run_seconds), written without spaces (took_text). T443 #3283 read 1分29秒 on its task card,
1 分 30 秒 on its status card and Pin, 1分30秒 on its answer card."""
from __future__ import annotations

import copy
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from support.fake_backends import RecordingBackend
from support.world import TopicFixture

from covey.app import answer_cards
from covey.app.cards import Cards
from covey.app.task_card import StreamingReply
from covey.app.recovery_cards import asked_took
from covey.cards.answer_card import output_parts
from covey.cards.task_figures import figure_cells
from covey.core import butler, metrics
from covey.core.harness import RunResult
from covey.core.longtask import TaskMonitor

# T443 #3283's times (VERIFY-board.md C8): the trace ended 1.2 s before the execution did.
REQUESTED, STARTED, TRACE_END, FINISHED = 1000.0, 1000.05, 1089.81, 1090.99
TIMINGS = {"queued": 999.0, "dispatched": 999.5, "backend_requested": REQUESTED, "backend_started": STARTED,
           "execution_finished": FINISHED}
SNAPSHOT = {"started_at": REQUESTED, "ended_at": TRACE_END}


class RunSecondsTest(unittest.TestCase):
    def test_from_the_backend_request_to_the_execution_s_end(self):
        self.assertAlmostEqual(metrics.run_seconds(TIMINGS, SNAPSHOT), FINISHED - REQUESTED)
        # The start falls back stage by stage; a record without timings goes by its process snapshot.
        later = {key: value for key, value in TIMINGS.items() if key != "backend_requested"}
        self.assertAlmostEqual(metrics.run_seconds(later), FINISHED - STARTED)
        self.assertAlmostEqual(metrics.run_seconds({"queued": 999.0, "execution_finished": FINISHED}), FINISHED - 999.0)
        self.assertAlmostEqual(metrics.run_seconds({}, SNAPSHOT), TRACE_END - REQUESTED)
        self.assertAlmostEqual(metrics.run_seconds({"backend_requested": REQUESTED}, now=REQUESTED + 30), 30)
        self.assertIsNone(metrics.run_seconds({"received": 990.0}))

    def test_one_writing_without_spaces(self):
        self.assertEqual([metrics.took_text(s) for s in (0, 9.9, 65, 90.99, 3600 + 23 * 60 + 5)],
                         ["0秒", "9秒", "1分05秒", "1分30秒", "1小时23分"])


class EveryCardTest(unittest.TestCase):
    def job(self):
        return types.SimpleNamespace(timings=dict(TIMINGS))

    def test_task_card_answer_card_relay_status_card_and_butler_say_the_same(self):
        job = self.job()
        seconds = FINISHED - REQUESTED
        self.assertAlmostEqual(Cards._took(job, SNAPSHOT), seconds)  # The status line's 用时 and the tile's.
        self.assertAlmostEqual(answer_cards.took(job, SNAPSHOT), seconds)  # The answer card and the relay.
        self.assertAlmostEqual(butler.run_seconds(job), seconds)  # The butler's one-minute rule.
        self.assertAlmostEqual(asked_took("", {"timings": dict(TIMINGS)}), seconds)
        self.assertIn("<font color='grey'>用时</font> **1分30秒**", output_parts(SNAPSHOT, answer_cards.took(job, SNAPSHOT)))
        self.assertEqual(figure_cells(SNAPSHOT, Cards._took(job, SNAPSHOT))[0], ("用时", "1分30秒", ""))

    def test_the_status_card_reads_the_record_s_timings(self):
        from covey.app import status_cards

        record = {"timings": dict(TIMINGS)}
        self.assertEqual(status_cards.ended(record), FINISHED)
        self.assertAlmostEqual(metrics.run_seconds(record["timings"], now=status_cards.ended(record)), FINISHED - REQUESTED)

    def test_the_board_s_running_row_starts_where_its_time_does(self):
        from covey.core.board_read import started_at

        self.assertEqual(started_at({"timings": dict(TIMINGS)}), REQUESTED)
        self.assertEqual(started_at({"timings": {"received": 990.0}}), 990.0)

    def test_the_queued_card_s_running_task_starts_where_its_time_does(self):
        from covey.app import submission

        # The queued card's 已运行 takes core/metrics.py run_start, stage by stage, not its own two stages.
        for timings in (dict(TIMINGS), {"backend_started": STARTED, "queued": 999.0}, {"queued": 999.0}):
            self.assertEqual(submission.started_at({"timings": timings}), metrics.run_start(timings))
        self.assertIsNone(submission.started_at({"timings": {"received": 990.0}}))
        self.assertIsNone(submission.started_at({}))

    def test_running_it_counts_from_the_backend_request(self):
        job = types.SimpleNamespace(timings={"backend_requested": REQUESTED}, backend_process=None, tool_trace=None,
                                    backend_retry={}, native_question_active=False)
        monitor = TaskMonitor(job, Path("/nonexistent/checkpoint.json"), None, None, None, None, None,
                              clock=lambda: 5.0, wall=lambda: REQUESTED + 125)
        facts, _ = monitor.facts(7.0)
        self.assertAlmostEqual(facts.elapsed, 125)
        job.timings = {}  # Before the request: from the monitor's own start.
        facts, _ = monitor.facts(7.0)
        self.assertAlmostEqual(facts.elapsed, 2.0)


class WordsTest(unittest.TestCase):
    """The state words are defined once (core/turn_words.py, C10) and read as before."""

    def test_every_reader_takes_the_one_table(self):
        from covey.app import butler as butler_app, butler_tools, overview, status_cards
        from covey.cards import overview as overview_card, question_cards, review
        from covey.core import longtask, turn_words

        self.assertIs(review.outcome_line, turn_words.outcome_line)
        self.assertIs(question_cards.FOLLOW_UP, turn_words.FOLLOW_UP)
        # 手上的事's ended rows say their state as the 看板 does (T604 #52: core/look.py STATES, cards/ui.py topic_row).
        from covey.core import look
        self.assertLessEqual(set(overview_card.ENDED_TONES.values()), set(look.STATES))
        self.assertIs(butler_tools.GROUP_NAMES, turn_words.GROUP_NAMES)
        self.assertIs(butler_app.RESULTS, turn_words.RESULTS)
        self.assertIs(overview.UNSET_PHASES, turn_words.UNSET_PHASES)
        self.assertIs(status_cards.UNSET_PHASES, turn_words.BLANK_PHASES)
        self.assertEqual(longtask.SEED_PHASE, turn_words.SEED_PHASE)
        self.assertEqual([review.outcome_line(s) for s in ("done", "failed", "cancelled", "waiting_user", "x")],
                         ["有结果", "停住了", "已停止", "等你", "有结果"])
        self.assertEqual(dict(butler_app.GROUPS)["result"], "有结果没看")


class TraceEndsFirstBackend(RecordingBackend):
    """One tool step, the trace closed as the turn closes (trace_finished), then a while before the backend
    returns: T443 #3283's trace ended 1.2 s before its execution did."""

    LAG = 0.3

    def run(self, request):
        for status in ("running", "completed"):
            request.emit("tool", {"id": "a", "name": "bash", "status": status, "detail": "ls", "started_at": time.time()})
        request.emit("trace_finished", {})
        time.sleep(self.LAG)
        return RunResult("完成", request.session_id or str(uuid.uuid4()))


class WorkerTest(TopicFixture, unittest.TestCase):
    """The worker marks the execution's end before the status line's outcome is written, so the status line's
    用时 is the answer card's even when the trace ended first (T443: 1分29秒 against 1分30秒)."""

    def test_the_status_line_s_took_is_the_answer_card_s(self):
        self.app.harness.backend = TraceEndsFirstBackend()
        self.app.cards.create_streaming_reply = mock.Mock(return_value=StreamingReply("card", "om_card"))
        self.app.messenger.write_card = mock.Mock(return_value=None)
        self.app.messenger.put_card = mock.Mock(return_value=None)
        shown, real = [], self.app.cards.show_outcome

        def show_outcome(ctx, job, content, stopped=False, *, timed=False):
            shown.append((copy.deepcopy(job.timings), timed))  # What Cards._took reads for the status line.
            return real(ctx, job, content, stopped, timed=timed)

        with mock.patch.object(self.app.cards, "show_outcome", side_effect=show_outcome):
            self.receive("om_1", "hello")
            job = self.work()
        snapshot = job.tool_trace.snapshot()
        timings, timed = next(item for item in shown if item[1])
        self.assertLess(snapshot["ended_at"], job.timings["execution_finished"] - TraceEndsFirstBackend.LAG / 2)
        self.assertEqual(timings.get("execution_finished"), job.timings["execution_finished"])
        self.assertAlmostEqual(Cards._took(types.SimpleNamespace(timings=timings), snapshot),
                               answer_cards.took(job, snapshot))


if __name__ == "__main__":
    unittest.main()
