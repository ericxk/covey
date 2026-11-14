"""Shared finalizer fault injection; multi-root inputs are not backend capabilities."""
from __future__ import annotations

import contextlib
import dataclasses
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests")]

import covey.app.sessions as sessions
import covey.core.session_guard as guards
from covey.core.session_state import capture_session_state
from support import require_extension

require_extension()  # Imports the company extension below.
from covey_internal.maso.bridge import MASO_SPEC, MasoBackend
from covey_internal.maso.sessions import MasoSessionStore
from support import make_context
from support.sessions import clear_session_id, write_session_id
from support.world import harness_app
from support.fake_lark import install_card_transport
from support.worker_seams import leave_out_side_work

FIRST = "00000000-0000-4000-8000-000000000011"
SECOND = "00000000-0000-4000-8000-000000000022"
THIRD = "00000000-0000-4000-8000-000000000033"
RUN = "00000000-0000-4000-8000-000000000099"


class SessionClaimsFinalizationTests(unittest.TestCase):
    def fixture(self, claims, old_sid=FIRST):
        temporary = tempfile.TemporaryDirectory(prefix="session-claims-fixture-")
        self.addCleanup(temporary.cleanup)
        ctx = dataclasses.replace(make_context(Path(temporary.name), profile="synthetic-maso"),
                                  topic_id="claims-topic", reply_to_message_id="om-claims")
        ctx.state_dir.mkdir(mode=0o700)
        backend = MasoBackend()
        backend.run = mock.Mock(side_effect=AssertionError("finalizer test must not run a backend"))
        app = harness_app(MASO_SPEC, backend, MasoSessionStore())
        app.runtime.log_sink = mock.Mock()
        if old_sid is not None:
            write_session_id(ctx.session_path, old_sid)
        revision, actual_sid = capture_session_state(ctx.session_path, MASO_SPEC.session_pattern)
        self.assertEqual(actual_sid, old_sid)
        job = SimpleNamespace(bridge_run_id=RUN, native_session_claims=set(claims))
        for sid in claims:
            guards.claim_native_session(ctx, "maso", sid, RUN)
        return ctx, app, job, revision

    def test_multiple_claims_never_unlink_even_if_later_finish_fails_or_is_interrupted(self):
        for failure in ("none", "false", "error", "interrupted"):
            with self.subTest(failure=failure):
                ctx, app, job, revision = self.fixture((FIRST, SECOND))
                calls, removed = [], []
                original_finish, original_unlink = guards.finish_native_session, Path.unlink

                def finish(context, backend, sid, run_id, allow_resume):
                    calls.append((sid, allow_resume))
                    if len(calls) == 2:
                        if failure == "false":
                            return False
                        if failure == "error":
                            raise OSError("synthetic later-finalization failure")
                        if failure == "interrupted":
                            raise KeyboardInterrupt("synthetic interruption between claims")
                    return original_finish(context, backend, sid, run_id, allow_resume)

                def unlink(path, *args, **kwargs):
                    if path in {guards._path(ctx, "maso", FIRST), guards._path(ctx, "maso", SECOND)}:
                        removed.append(path.name)
                    return original_unlink(path, *args, **kwargs)

                with mock.patch.object(sessions, "finish_native_session", side_effect=finish), \
                        mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                    try:
                        accepted = app.sessions.finalize_session_from_worker(
                            ctx, job, FIRST, SECOND, revision, allow_resume=True)
                    except (OSError, KeyboardInterrupt):
                        self.assertIn(failure, {"error", "interrupted"})
                    else:
                        self.assertIs(accepted, False, "multiple roots cannot authorize native continuation")
                self.assertEqual({sid for sid, _ in calls}, {FIRST, SECOND})
                self.assertTrue(all(allowed is False for _, allowed in calls))
                self.assertEqual(removed, [], "even a transient first-claim unlink is unsafe")
                for sid in (FIRST, SECOND):
                    self.assertTrue(guards.session_is_blocked(ctx, "maso", sid))
                    self.assertTrue(guards._path(ctx, "maso", sid).is_file())
                self.assertNotEqual(capture_session_state(ctx.session_path, MASO_SPEC.session_pattern)[1], SECOND)
                self.assertIsNone(app.sessions.capture_session(ctx)[1])
                app.backend.run.assert_not_called()

    def test_shared_worker_does_not_treat_multiple_protected_roots_as_resume_permission(self):
        # These deliberately inconsistent callbacks test the shared safety branch,
        # not a capability claimed for MASO or any real SDK.
        from covey.core.harness import RunResult
        from covey.core.jobs import BridgeReplyJob, find_job_record
        from covey.core.result import ExecutionOutcome, VerifiedRootReply
        from support.fake_lark import MemoryTransport

        for verified in (False, True):
            with self.subTest(verified=verified), contextlib.ExitStack() as stack:
                ctx, app, _, _ = self.fixture(())
                tx = MemoryTransport(card_ok=False, text_ok=True, file_ok=True)
                app.messenger.send_message, app.artifacts.send_result_file = tx.send_text, tx.send_file
                app.cards.streaming = False
                app.cards.finish_result_controls = mock.Mock()
                app.cards.stream = dataclasses.replace(app.cards.stream,
                    card_limit=20000, replacement_limit=0,
                    send_text_message=tx.send_text)
                install_card_transport(stack, tx)
                leave_out_side_work(stack)

                def inconsistent_backend(request):
                    request.callbacks.session(SECOND)
                    if not verified:
                        return RunResult("Synthetic unverified completion", SECOND)
                    reply = VerifiedRootReply("maso", request.inputs.run_id, SECOND,
                        ("synthetic-user",), ("synthetic-message",), "Synthetic current-root public text")
                    request.callbacks.verified_reply(reply)
                    outcome = ExecutionOutcome("completed", identity_confirmed=True,
                        cleanup_confirmed=True, protection_confirmed=True, resume_allowed=True,
                        reply_fingerprint=reply.fingerprint)
                    return RunResult(reply.text, SECOND, public_reply=reply, outcome=outcome)

                app.backend.run = mock.Mock(side_effect=inconsistent_backend)
                job = BridgeReplyJob(job_id=14, user_text="Synthetic conflicting roots",
                    chat_id="chat-claims", message_id="om-claims-job", topic_id=ctx.topic_id,
                    reply_to_message_id=ctx.reply_to_message_id, timeout=0, model=ctx.model,
                    effort=ctx.effort, model_settings_snapshot=True, progress=None)
                removed = []
                original_unlink = Path.unlink

                def unlink(path, *args, **kwargs):
                    if path in {guards._path(ctx, "maso", FIRST), guards._path(ctx, "maso", SECOND)}:
                        removed.append(path.name)
                    return original_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                    app.process_reply_job(job, ctx)
                record = find_job_record(ctx.state_dir, job.message_id)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(job.native_session_claims, {FIRST, SECOND})
                self.assertEqual(removed, [])
                self.assertIsNone(app.sessions.read_session_id(ctx.session_path))
                for sid in (FIRST, SECOND):
                    self.assertTrue(guards.session_is_blocked(ctx, "maso", sid))
                if verified:
                    self.assertEqual(record["execution_status"], "failed")
                    self.assertIs(record["native_resume_allowed"], False)
                    self.assertIs(record["verified_reply_authorized"], True)
                    self.assertEqual(record["result_retry_policy"], "saved_delivery_only")
                app.backend.run.assert_called_once()

    def test_unique_root_can_finish_but_recording_respects_the_original_revision(self):
        cases = ((None, None), (FIRST, None), (FIRST, FIRST), (FIRST, THIRD))
        for old_sid, user_new in cases:
            with self.subTest(old_sid=old_sid, user_new=user_new):
                sid = old_sid or SECOND
                ctx, app, job, revision = self.fixture((sid,), old_sid=old_sid)
                current = None
                if user_new is not None:
                    clear_session_id(ctx.session_path, MASO_SPEC.session_pattern)
                    write_session_id(ctx.session_path, user_new)
                    current = capture_session_state(ctx.session_path, MASO_SPEC.session_pattern)
                self.assertIs(app.sessions.finalize_session_from_worker(ctx, job, old_sid, sid, revision, allow_resume=True), True)
                self.assertFalse(guards.session_is_blocked(ctx, "maso", sid))
                app.sessions.record_session_from_worker(ctx, old_sid, sid, revision)
                after = capture_session_state(ctx.session_path, MASO_SPEC.session_pattern)
                if current is not None:
                    self.assertEqual(after, current, "an old result must not replace a /new revision")
                else:
                    self.assertEqual(after, (revision + 1, sid))
                app.backend.run.assert_not_called()

    def test_multiple_claims_keep_a_newer_topic_revision_unchanged(self):
        for replacement in (FIRST, THIRD):
            with self.subTest(replacement=replacement):
                ctx, app, job, revision = self.fixture((FIRST, SECOND))
                clear_session_id(ctx.session_path, MASO_SPEC.session_pattern)
                write_session_id(ctx.session_path, replacement)
                before = capture_session_state(ctx.session_path, MASO_SPEC.session_pattern)
                self.assertIs(app.sessions.finalize_session_from_worker(ctx, job, FIRST, SECOND, revision, allow_resume=True), False)
                self.assertEqual(capture_session_state(ctx.session_path, MASO_SPEC.session_pattern), before)
                for sid in (FIRST, SECOND):
                    self.assertTrue(guards.session_is_blocked(ctx, "maso", sid))
                app.backend.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
