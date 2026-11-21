"""The long rate-limit ladder, the server-fault kind, a limit's reset time, progress and the anti-spin stop
(app/auto_retry.py plan; core/limit_reset.py; backends/claude/completion.py) and a revisit turn that is retried
(app/revisits.py lineage)."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_backends import RUN, SID, assistant, codex_context, result

from covey.app import auto_retry, revisits
from covey.backends.claude.completion import ClaudeReplyState, result_failure
from covey.core import limit_reset
from covey.core.harness import HarnessCapabilities
from covey.core.jobs import upsert_job_record
from covey.core.result import ExecutionOutcome

NOW = 1_800_000_000.0
INIT = {"type": "system", "subtype": "init", "session_id": SID}


def limited_row(**extra):
    return {"type": "assistant", "session_id": SID, "uuid": "limit-row", "error": "rate_limit", "isApiErrorMessage": True,
            "message": {"id": "limit-row", "content": [{"type": "text", "text": "You've hit your limit · resets 4:20pm (Asia/Shanghai)"}]},
            **extra}


class Reading(unittest.TestCase):
    def test_a_rejected_limit_names_its_reset_and_a_warning_does_not(self):
        self.assertEqual(limit_reset.row_reset(limited_row(quotaLimits={"status": "rejected", "resetsAt": NOW + 600})), NOW + 600)
        self.assertEqual(limit_reset.row_reset(limited_row(quotaLimits={"resetsAt": (NOW + 600) * 1000})), NOW + 600)
        self.assertIsNone(limit_reset.row_reset(limited_row(quotaLimits={"status": "allowed", "resetsAt": NOW + 600})))
        event = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": NOW + 5}}
        self.assertEqual(limit_reset.row_reset(event), NOW + 5)
        event["rate_limit_info"]["status"] = "allowed_warning"
        self.assertIsNone(limit_reset.row_reset(event))
        self.assertIsNone(limit_reset.row_reset({"type": "assistant", "quotaLimits": {"resetsAt": True}}))

    def test_the_words_are_read_with_their_time_zone_and_not_without_one(self):
        now = dt.datetime(2026, 11, 21, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        at = limit_reset.text_reset("You've hit your limit · resets 4:20pm (Asia/Shanghai)", now)
        self.assertEqual(dt.datetime.fromtimestamp(at, dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M"), "11-21 16:20")
        # Already past today: tomorrow's.
        late = dt.datetime(2026, 11, 21, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        self.assertEqual(limit_reset.text_reset("resets 4:20pm (Asia/Shanghai)", late) - at, 86400)
        self.assertIsNone(limit_reset.text_reset("resets 4:20pm", now))
        self.assertIsNone(limit_reset.text_reset("resets 4:20pm (Local)", now))
        self.assertIsNone(limit_reset.text_reset("resets 4:20pm (Not/AZone)", now))
        dated = limit_reset.text_reset("resets Nov 23, 4pm (UTC)", now)
        self.assertEqual(dt.datetime.fromtimestamp(dated, dt.timezone.utc).strftime("%m-%d %H:%M"), "11-23 16:00")
        # A 5-hour limit's line whose time is just past: the limit is over, the past time is returned.
        just_past = dt.datetime(2026, 11, 21, 16, 25, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        self.assertEqual(limit_reset.text_reset("You've hit your 5-hour limit · resets 4:20pm (Asia/Shanghai)", just_past), at)

    def test_the_evidence_keeps_the_format_and_cuts_the_words(self):
        row = limited_row(quotaLimits={"status": "rejected", "resetsAt": NOW}, secret_field="sk-x" * 50)
        row["message"]["content"][0]["text"] = "x" * 5000
        kept = limit_reset.evidence(row)
        self.assertEqual(kept["quotaLimits"]["resetsAt"], NOW)
        self.assertNotIn("secret_field", kept)
        self.assertLessEqual(len(kept["text"]), limit_reset.TEXT_CHARS)

    def test_a_fault_is_told_by_its_status_or_its_words(self):
        for status in (500, 502, 503, 504, 529):
            self.assertTrue(limit_reset.fault_in(status, ""))
        self.assertTrue(limit_reset.fault_in(None, "API Error: Connection reset (ECONNRESET)"))
        self.assertTrue(limit_reset.fault_in(None, "Stream idle timeout - partial response received"))
        self.assertFalse(limit_reset.fault_in(400, "invalid request"))
        self.assertFalse(limit_reset.fault_in(None, ""))


class Stream(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = codex_context(Path(self.dir.name), "stream-json")

    def finish(self, rows, rc=0):
        state = ClaudeReplyState(self.ctx, None, run_id=RUN)
        for row in [*rows, {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"}]:
            state.accept(row)
        return state.finish(rc, cleanup=True)

    def test_a_limit_with_a_reset_carries_it_and_its_rows_to_the_outcome_and_the_diagnostics(self):
        event = {"type": "rate_limit_event", "session_id": SID, "rate_limit_info": {"status": "rejected", "resetsAt": NOW + 900}}
        answer = self.finish([INIT, event, limited_row(quotaLimits={"status": "rejected", "resetsAt": NOW + 900}), ], rc=1)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "rate_limited"))
        self.assertEqual(answer.limit["reset_at"], NOW + 900)
        self.assertEqual([row["type"] for row in answer.limit["evidence"]], ["rate_limit_event", "assistant"])
        saved = json.loads((Path(self.ctx.state_dir) / "claude-diagnostics" / (RUN + ".json")).read_text())
        self.assertEqual(saved["limit"]["reset_at"], NOW + 900)

    def test_the_words_are_the_fallback_for_a_reset(self):
        answer = self.finish([INIT, limited_row()], rc=1)
        self.assertEqual(answer.outcome.error_code, "rate_limited")
        self.assertIn("reset_at", answer.limit)
        self.assertEqual(self.finish([INIT, limited_row(message={"content": [{"type": "text", "text": "429"}]})], rc=1).limit.get("reset_at"), None)

    def test_a_gateways_429_without_a_reset_has_none(self):
        row = limited_row()
        row["message"]["content"][0]["text"] = "Request rejected (429) · quota reached"
        self.assertIsNone(self.finish([INIT, row], rc=1).limit.get("reset_at"))

    def test_an_overload_ends_as_a_server_fault_whichever_way_it_comes(self):
        overloaded = result(is_error=True, terminal_reason="api_error", api_error_status=529,
                            result='API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}')
        self.assertEqual(self.finish([INIT, overloaded]).outcome.error_code, "server_fault")
        for status in (500, 502, 503, 504):
            self.assertEqual(result_failure(result(is_error=True, api_error_status=status)), ("failed", "server_fault"))
        dropped = result(is_error=True, result="API Error: Connection reset (ECONNRESET)")
        self.assertEqual(result_failure(dropped), ("failed", "server_fault"))
        error = {"type": "assistant", "session_id": SID, "uuid": "e", "error": "server_error",
                 "message": {"id": "e", "content": [{"type": "text", "text": "API Error: 500"}]}}
        answer = self.finish([INIT, error], rc=1)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "server_fault"))
        self.assertEqual(answer.limit["evidence"][0]["error"], "server_error")
        # A rate limit wins over a fault; a 400 is neither.
        self.assertEqual(self.finish([INIT, error, limited_row()], rc=1).outcome.error_code, "rate_limited")
        self.assertEqual(result_failure(result(is_error=True, api_error_status=400)), ("failed", "root_failed"))

    def test_a_fault_the_turn_got_past_leaves_nothing(self):
        answer = self.finish([INIT, {"type": "assistant", "session_id": SID, "uuid": "e", "error": "server_error",
                                      "message": {"id": "e", "content": [{"type": "text", "text": "API Error: 500"}]}},
                              assistant(), result()])
        self.assertEqual(answer.outcome.status, "completed")
        self.assertIsNone(answer.limit)


class Planning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = SimpleNamespace(state_dir=Path(self.dir.name))
        self.harness = SimpleNamespace(backend=SimpleNamespace(capabilities=lambda ctx: HarnessCapabilities(
            rate_limit_retry=True, fault_retry=True, incomplete_retry=True)))

    def job(self, **fields):
        return SimpleNamespace(**{"message_id": "m", "job_id": 1, "topic_id": "t",
                                  "cancel_event": SimpleNamespace(is_set=lambda: False), "timeout": 0, "auto_retry": 0,
                                  "auto_continue": 0, "auto_resume": 0, "auto_fault": 0, "auto_idle": 0,
                                  "tool_trace": None, "timings": {}, **fields})

    def turn(self, code="rate_limited", limit=None):
        return SimpleNamespace(outcome=ExecutionOutcome("failed", code), verified_delivery=None, limit=limit)

    def plan(self, job=None, turn=None, now=NOW):
        with mock.patch.object(auto_retry, "jitter", return_value=0.0):
            return auto_retry.plan(self.harness, self.ctx, job or self.job(), turn or self.turn(), now)

    def worked(self):
        return {"tool_trace": SimpleNamespace(total=5), "timings": {"backend_started": NOW - 600, "execution_finished": NOW}}

    def test_the_ladder_spans_hours_and_the_server_fault_one_minutes(self):
        self.assertEqual([auto_retry.delays_of("rate_limited")[n] for n in range(6)], [60, 180, 600, 1800, 3600, 3600])
        for done, delay in enumerate((60, 180, 600, 1800, 3600, 3600)):
            planned = self.plan(self.job(auto_retry=done))
            self.assertEqual((planned.attempt, planned.due_at - NOW), (done + 1, delay))
        self.assertIsNone(self.plan(self.job(auto_retry=6)))
        for done, delay in enumerate((30, 120, 300)):
            planned = self.plan(self.job(auto_fault=done), self.turn("server_fault"))
            self.assertEqual((planned.kind, planned.attempt, planned.due_at - NOW), ("server_fault", done + 1, delay))
        self.assertIsNone(self.plan(self.job(auto_fault=3), self.turn("server_fault")))

    def test_every_retry_gets_up_to_half_a_minute_of_jitter(self):
        dues = {auto_retry.plan(self.harness, self.ctx, self.job(), self.turn(), NOW).due_at - NOW - 60 for _ in range(30)}
        self.assertTrue(all(0 <= extra <= 30 for extra in dues))
        self.assertGreater(len(dues), 1)
        restart = {"status": "interrupted", "interrupted_by": "restart", "kind": "chat", "topic_id": "t"}
        with mock.patch.object(auto_retry, "retry_label", return_value="检查并继续"):
            extra = auto_retry.plan_restart(restart, NOW).due_at - NOW - 60
        self.assertTrue(0 <= extra <= 30)

    def test_a_known_reset_within_six_hours_is_waited_for(self):
        planned = self.plan(turn=self.turn(limit={"reset_at": NOW + 3 * 3600}))
        self.assertEqual((planned.due_at, planned.reset_at), (NOW + 3 * 3600 + 60, NOW + 3 * 3600))
        farther = self.plan(turn=self.turn(limit={"reset_at": NOW + 7 * 3600}))
        self.assertEqual((farther.due_at, farther.reset_at), (NOW + 60, 0.0))
        past = self.plan(turn=self.turn(limit={"reset_at": NOW - 30}))
        self.assertEqual((past.due_at, past.reset_at), (NOW + 60, 0.0))
        # Not for a fault.
        self.assertEqual(self.plan(turn=self.turn("server_fault", {"reset_at": NOW + 3600})).due_at, NOW + 30)

    def test_a_retry_that_made_progress_starts_the_chain_over(self):
        planned = self.plan(self.job(auto_retry=4, auto_continue=1, **self.worked()))
        self.assertEqual((planned.attempt, planned.due_at - NOW, planned.progress), (1, 60, True))
        fields = auto_retry.schedule_fields(planned)
        self.assertTrue(fields["auto_retry_progress"])
        counts = auto_retry.chain_counts({**fields, "auto_retry": 4, "auto_continue": 1, "auto_fault": 2,
                                          "auto_retry_kind": "rate_limited"}, planned.attempt)
        self.assertEqual((counts["auto_retry"], counts["auto_continue"], counts["auto_fault"]), (1, 0, 0))
        # No progress: the count goes on. A first failure is no retry, so it has no progress to speak of.
        self.assertEqual(self.plan(self.job(auto_retry=4)).attempt, 5)
        self.assertEqual(self.plan(self.job(**self.worked())).attempt, 1)
        # Too few steps, or no time: not progress.
        self.assertEqual(self.plan(self.job(auto_retry=2, tool_trace=SimpleNamespace(total=2), timings=self.worked()["timings"])).attempt, 3)
        self.assertEqual(self.plan(self.job(auto_retry=2, tool_trace=SimpleNamespace(total=9), timings={"backend_started": NOW - 5})).attempt, 3)

    def test_three_retries_at_a_reset_with_no_progress_stop_the_chain(self):
        reset = self.turn(limit={"reset_at": NOW + 600})
        first = self.plan(self.job(auto_retry=1), reset)
        self.assertEqual(first.idle, 1)
        second = self.plan(self.job(auto_retry=2, auto_idle=1), reset)
        self.assertEqual(second.idle, 2)
        third = self.plan(self.job(auto_retry=3, auto_idle=2), reset)
        self.assertIsNone(third)  # Its third retry at a reset went nowhere.
        # Progress in between clears it; with no reset the ladder goes on instead.
        self.assertEqual(self.plan(self.job(auto_retry=3, auto_idle=2, **self.worked()), reset).idle, 0)
        self.assertIsNotNone(self.plan(self.job(auto_retry=3, auto_idle=2), self.turn()))
        self.assertEqual(auto_retry.chain_counts(auto_retry.schedule_fields(second), 3)[auto_retry.IDLE], 2)

    def test_the_switch_off_is_the_old_ladder_with_no_fault_kind_no_reset_and_no_jitter(self):
        with mock.patch.dict("os.environ", {"COVEY_AUTO_RETRY_LONG": "0"}):
            planned = auto_retry.plan(self.harness, self.ctx, self.job(), self.turn(limit={"reset_at": NOW + 600}), NOW)
            self.assertEqual((planned.due_at - NOW, planned.reset_at), (60, 0.0))
            self.assertEqual(auto_retry.plan(self.harness, self.ctx, self.job(auto_retry=1), self.turn(), NOW).due_at - NOW, 180)
            self.assertIsNone(auto_retry.plan(self.harness, self.ctx, self.job(auto_retry=2), self.turn(), NOW))
            self.assertIsNone(auto_retry.plan(self.harness, self.ctx, self.job(), self.turn("server_fault"), NOW))
            self.assertEqual(auto_retry.plan(self.harness, self.ctx, self.job(auto_retry=1, **self.worked()), self.turn(), NOW).attempt, 2)
            self.assertIn("第 2/2 次", auto_retry.status_line(NOW, 2, "rate_limited"))

    def test_the_status_line_says_when_it_goes_on_and_when_the_limit_resets(self):
        at = lambda value: dt.datetime.fromtimestamp(value).astimezone().strftime("%H:%M")
        self.assertEqual(auto_retry.status_line(NOW + 100, 2, "rate_limited"), f"模型服务限流，将于 {at(NOW + 100)} 自动重试")
        self.assertEqual(auto_retry.status_line(NOW + 100, 2, "rate_limited", NOW + 40),
                         f"模型服务限流，预计 {at(NOW + 40)} 恢复，将于 {at(NOW + 100)} 自动重试")
        self.assertEqual(auto_retry.status_line(NOW + 100, 2, "server_fault"), f"模型服务临时故障，将于 {at(NOW + 100)} 自动重试（第 2/3 次）")
        self.assertEqual(auto_retry.queued_line("rate_limited", 2), "自动重试已入队")


class RevisitRetries(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name)
        self.message = "revisit-topic-1-0"

    def record(self, key, **fields):
        upsert_job_record(self.state, key, job_id=len(key), topic_id="t", chat_id="c", **fields)
        from covey.core.jobs import find_job_record
        return find_job_record(self.state, key)

    def test_a_revisit_turn_with_a_retry_to_come_is_not_the_end_of_the_watch(self):
        first = self.record("k1", message_id=self.message, status="failed", auto_retry_state="pending")
        self.assertEqual(revisits.lineage(self.state, first), (self.message, first, True))
        first = self.record("k1", auto_retry_state="queued", retried_by="auto-retry-1")
        self.assertEqual(revisits.lineage(self.state, first)[2], True)  # Queued, its record not there yet.
        retry = self.record("k2", message_id="auto-retry-1", retry_of="k1", status="running")
        self.assertEqual(revisits.lineage(self.state, first)[2], True)
        self.assertEqual(revisits.lineage(self.state, retry)[2], True)  # From the retry too.
        retry = self.record("k2", status="done")
        message, tail, waiting = revisits.lineage(self.state, first)
        self.assertEqual((message, tail["key"], waiting), (self.message, "k2", False))
        message, tail, waiting = revisits.lineage(self.state, retry)
        self.assertEqual((message, tail["key"], waiting), (self.message, "k2", False))

    def test_a_cancelled_retry_leaves_the_failed_turn_as_the_end_and_other_turns_are_untouched(self):
        first = self.record("k1", message_id=self.message, status="failed", auto_retry_state="cancelled")
        self.assertEqual(revisits.lineage(self.state, first), (self.message, first, False))
        own = self.record("k3", message_id="msg", status="failed", auto_retry_state="pending")
        self.assertEqual(revisits.lineage(self.state, own), ("msg", own, False))


if __name__ == "__main__":
    unittest.main()
