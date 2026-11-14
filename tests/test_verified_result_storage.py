"""Independent saved-result fixtures using synthetic private state only.

All state is synthetic and lives in TemporaryDirectory. This module does not
start a backend, inspect a native session, or provide a new test runner.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from support import requires_extension
from covey.core.jobs import job_key, job_path, read_job_record, upsert_job_record
from covey.core.result import ExecutionOutcome, MAX_REPLY_BYTES, VerifiedRootReply
from covey.core.saved_results import (
    RETRY_POLICY,
    authorize_verified_reply,
    display_saved_reply,
    load_authorized_reply,
    observe_verified_reply,
)


RUN_ID = "11111111-2222-4333-8444-555555555555"
OTHER_RUN_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

# Generation is persisted authorization identity, not Python object identity.


def reply_value(**changes):
    fields = {
        "backend": "claude",
        "run_id": RUN_ID,
        "session_id": "session-fixture",
        "boundary": ("user-fixture", "steer-fixture"),
        "message_ids": ("assistant-plan", "assistant-final"),
        "text": "Verified plan\n\nVerified final answer.",
        "generation": 1,
    }
    fields.update(changes)
    return VerifiedRootReply(**fields)


def confirmed_outcome(**changes):
    fields = {
        "status": "failed",
        "error_code": "child_unknown",
        "identity_confirmed": True,
        "cleanup_confirmed": True,
        "protection_confirmed": True,
        "resume_allowed": False,
        "reply_fingerprint": reply_value().fingerprint,
    }
    fields.update(changes)
    return ExecutionOutcome(**fields)


class VerifiedReplyAcceptance(unittest.TestCase):
    @requires_extension()
    def test_supported_backends_roundtrip_json_without_losing_identity(self):
        for backend in ("maso", "claude", "codex", "dsh"):
            with self.subTest(backend=backend):
                reply = reply_value(backend=backend)
                restored = VerifiedRootReply.from_dict(json.loads(json.dumps(reply.to_dict())))
                self.assertEqual(restored, reply)
                self.assertIsInstance(restored.boundary, tuple)
                self.assertIsInstance(restored.message_ids, tuple)

    def test_generation_default_is_one_and_wire_always_contains_it(self):
        fields = reply_value().to_dict()
        fields.pop("generation")
        reply = VerifiedRootReply(**fields)
        self.assertIs(type(reply.generation), int)
        self.assertEqual(reply.generation, 1)
        self.assertEqual(reply.to_dict()["generation"], 1)
        self.assertEqual(reply_value(generation=2).generation, 2)

    def test_generation_rejects_boolean_float_zero_and_negative(self):
        for generation in (True, False, 1.0, 2.0, 0, -1, None, "1", []):
            with self.subTest(generation=generation), self.assertRaises((ValueError, TypeError)):
                reply_value(generation=generation)

    def test_wire_requires_explicit_strict_generation(self):
        original = reply_value().to_dict()
        missing = dict(original)
        missing.pop("generation")
        with self.assertRaises(ValueError):
            VerifiedRootReply.from_dict(missing)
        for generation in (True, 1.0, 0, -1, None, "1"):
            with self.subTest(generation=generation), self.assertRaises((ValueError, TypeError)):
                VerifiedRootReply.from_dict({**original, "generation": generation})

    def test_unknown_backend_is_rejected(self):
        for backend in ("", "legacy", "neeko", "NEEKO", "neeko-next", None, 1, True):  # removed
            with self.subTest(backend=backend), self.assertRaises((ValueError, TypeError)):
                reply_value(backend=backend)

    def test_run_id_must_be_canonical_uuid(self):
        for run_id in (None, True, 7, "", "not-a-uuid", RUN_ID.replace("-", ""), "{" + RUN_ID + "}", OTHER_RUN_ID.upper()):
            with self.subTest(run_id=run_id), self.assertRaises((ValueError, TypeError)):
                reply_value(run_id=run_id)

    def test_session_identity_is_nonempty_bounded_and_whitespace_free(self):
        for sid in (None, True, 9, "", " ", "session with space", "session\nnext", "x" * 257):
            with self.subTest(session_id=sid), self.assertRaises((ValueError, TypeError)):
                reply_value(session_id=sid)

    def test_constructor_boundary_and_message_ids_require_nonempty_tuples(self):
        for field in ("boundary", "message_ids"):
            for value in (None, "identity", [], ["identity"], ()):
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                    reply_value(**{field: value})

    def test_duplicate_or_malformed_boundary_members_are_rejected(self):
        for field in ("boundary", "message_ids"):
            for values in (("same", "same"), ("",), (None,), (True,), ("bad id",), ("x" * 257,), ([],)):
                with self.subTest(field=field, values=values), self.assertRaises((ValueError, TypeError)):
                    reply_value(**{field: values})

    def test_boundary_item_limit_is_enforced(self):
        large = tuple("id-" + str(index) for index in range(20001))
        for field in ("boundary", "message_ids"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                reply_value(**{field: large})

    def test_text_must_be_actual_nonblank_text(self):
        for text in (None, True, 12, b"bytes", ["text"], "", " \n\t"):
            with self.subTest(text=text), self.assertRaises((ValueError, TypeError)):
                reply_value(text=text)

    def test_text_limit_counts_utf8_bytes(self):
        text = "\u00e9" * (MAX_REPLY_BYTES // 2)
        self.assertEqual(len(reply_value(text=text).text.encode("utf-8")), MAX_REPLY_BYTES)
        with self.assertRaises(ValueError):
            reply_value(text=text + "\u00e9")

    def test_text_is_not_rewritten_or_stripped(self):
        text = "  First line\n\nSecond line.  "
        self.assertEqual(reply_value(text=text).text, text)
        self.assertEqual(VerifiedRootReply.from_dict(reply_value(text=text).to_dict()).text, text)

    def test_from_dict_requires_exact_field_set(self):
        good = reply_value().to_dict()
        for field in good:
            malformed = dict(good)
            malformed.pop(field)
            with self.subTest(missing=field), self.assertRaises((ValueError, TypeError)):
                VerifiedRootReply.from_dict(malformed)
        malformed = {**good, "authorized": True}
        with self.assertRaises(ValueError):
            VerifiedRootReply.from_dict(malformed)
        for value in (None, [], "progress text", ("legacy", "session", False)):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                VerifiedRootReply.from_dict(value)

    def test_from_dict_revalidates_member_types_and_duplicates(self):
        for field in ("boundary", "message_ids"):
            for value in ("one", None, [], ["duplicate", "duplicate"], [True], ["space id"]):
                malformed = {**reply_value().to_dict(), field: value}
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                    VerifiedRootReply.from_dict(malformed)

    def test_body_digest_and_fingerprint_are_independently_recomputable(self):
        reply = reply_value(text="Fixture \u00e9\nline")
        expected = hashlib.sha256(reply.text.encode("utf-8")).hexdigest()
        raw = json.dumps(reply.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(reply.sha256, expected)
        self.assertEqual(reply.fingerprint, hashlib.sha256(raw).hexdigest())

    @requires_extension()
    def test_fingerprint_binds_every_reply_identity_field_and_order(self):
        original = reply_value()
        changes = (
            {"backend": "maso"}, {"run_id": OTHER_RUN_ID}, {"session_id": "other-session"},
            {"generation": 2},
            {"boundary": tuple(reversed(original.boundary))},
            {"message_ids": tuple(reversed(original.message_ids))}, {"text": original.text + "!"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(replace(original, **change).fingerprint, original.fingerprint)

    def test_verified_reply_is_immutable(self):
        reply = reply_value()
        with self.assertRaises(FrozenInstanceError):
            reply.text = "replacement"


class OutcomeAcceptance(unittest.TestCase):
    def test_confirmation_fields_accept_only_actual_booleans(self):
        for field in ("identity_confirmed", "cleanup_confirmed", "protection_confirmed", "resume_allowed"):
            for value in (0, 1, None, "true", [], {}):
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                    confirmed_outcome(**{field: value})

    def test_unknown_status_and_error_code_are_rejected(self):
        for change in ({"status": "partial"}, {"status": "running"}, {"status": True},
                       {"error_code": "private arbitrary exception"}, {"error_code": None}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                confirmed_outcome(**change)

    def test_delivery_requires_all_three_independent_confirmations(self):
        for identity, cleanup, protection in itertools.product((False, True), repeat=3):
            with self.subTest(identity=identity, cleanup=cleanup, protection=protection):
                outcome = confirmed_outcome(identity_confirmed=identity, cleanup_confirmed=cleanup,
                                            protection_confirmed=protection)
                self.assertIs(outcome.delivery_authorized, identity and cleanup and protection)
                self.assertFalse(outcome.resume_allowed)

    def test_resume_requires_completed_execution_and_all_confirmations(self):
        for state in ("failed", "cancelled", "timed_out", "unknown"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                confirmed_outcome(status=state, resume_allowed=True)
        for field in ("identity_confirmed", "cleanup_confirmed", "protection_confirmed"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                confirmed_outcome(status="completed", error_code="", resume_allowed=True, **{field: False})
        self.assertTrue(confirmed_outcome(status="completed", error_code="", resume_allowed=True).resume_allowed)

    def test_known_failure_can_authorize_but_unknown_cannot(self):
        for state in ("failed", "cancelled", "timed_out", "unknown"):
            with self.subTest(state=state):
                outcome = confirmed_outcome(status=state)
                self.assertIs(outcome.delivery_authorized, state != "unknown")
                self.assertFalse(outcome.resume_allowed)
                self.assertNotEqual(outcome.job_status, "done")

    def test_job_status_mapping_preserves_failure_and_cancellation(self):
        expected = {"completed": "done", "failed": "failed", "cancelled": "cancelled",
                    "timed_out": "failed", "unknown": "failed"}
        for state, mapped in expected.items():
            with self.subTest(state=state):
                self.assertEqual(confirmed_outcome(status=state).job_status, mapped)

    def test_display_preserves_exact_verified_text_and_separates_notice(self):
        reply = reply_value(text="  Verified content\nsecond line  ")
        partial = confirmed_outcome(reply_fingerprint=reply.fingerprint)
        self.assertEqual(display_saved_reply(reply, partial), partial.notice() + "\n\n" + reply.text)
        completed = confirmed_outcome(status="completed", error_code="", reply_fingerprint=reply.fingerprint)
        self.assertEqual(display_saved_reply(reply, completed), reply.text)
        self.assertTrue(partial.notice())

    def test_outcome_reply_fingerprint_rejects_wrong_type_or_digest_shape(self):
        for fingerprint in (True, 1, None, "0" * 63, "A" * 64, "z" * 64):
            with self.subTest(fingerprint=fingerprint), self.assertRaises((ValueError, TypeError)):
                confirmed_outcome(reply_fingerprint=fingerprint)

    def test_outcome_roundtrip_preserves_all_authorization_facts(self):
        outcome = confirmed_outcome()
        self.assertEqual(ExecutionOutcome(**json.loads(json.dumps(asdict(outcome)))), outcome)


class SavedResultAcceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="saved-result-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.workdir = self.root / "workspace"
        self.workdir.mkdir()
        self.ctx = SimpleNamespace(state_dir=self.state, workdir=self.workdir, topic_id="topic-fixture")
        self.job = SimpleNamespace(
            bridge_run_id=RUN_ID, job_id=7, message_id="message-fixture", chat_id="chat-fixture",
            topic_id=self.ctx.topic_id, verified_reply=None, verified_result_key=None,
        )
        self.backend = "claude"
        upsert_job_record(self.state, self.job.message_id, job_id=self.job.job_id,
                          chat_id=self.job.chat_id, topic_id=self.job.topic_id,
                          status="running", execution_status="running", native_resume_allowed=False)

    def record(self):
        record = read_job_record(job_path(self.state, self.job.message_id))
        self.assertIsInstance(record, dict)
        return record

    def saved_path(self):
        return self.state / "verified-results" / (self.job.verified_result_key + ".json")

    def saved(self):
        return json.loads(self.saved_path().read_text(encoding="utf-8"))

    def write_saved(self, saved):
        self.saved_path().write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    def stage(self, reply=None):
        reply = reply if reply is not None else reply_value()
        observe_verified_reply(self.ctx, self.job, self.backend, reply)
        return reply

    def grant(self, outcome=None):
        reply = self.stage()
        outcome = outcome if outcome is not None else confirmed_outcome()
        self.assertTrue(authorize_verified_reply(self.ctx, self.job, self.backend, reply, outcome))
        return reply, outcome, self.record()

    def load(self, record=None, *, ctx=None, backend=None, chat_id=None):
        return load_authorized_reply(ctx if ctx is not None else self.ctx,
                                     record if record is not None else self.record(),
                                     backend if backend is not None else self.backend,
                                     chat_id if chat_id is not None else self.job.chat_id)

    def assert_authorization_denied(self, reply, outcome=None, *, ctx=None, job=None, backend=None):
        try:
            result = authorize_verified_reply(ctx if ctx is not None else self.ctx,
                                               job if job is not None else self.job,
                                               backend if backend is not None else self.backend,
                                               reply, outcome if outcome is not None else confirmed_outcome())
        except (ValueError, TypeError, KeyError, OSError):
            return
        self.assertIs(result, False)

    def test_a_record_that_does_not_decode_reads_as_no_record(self):
        # Only a JSONDecodeError was caught: bytes that are not UTF-8 raised UnicodeDecodeError to every reader.
        path = job_path(self.state, self.job.message_id)
        path.write_bytes(b'{"status": "running", "final_text": "\xe6\xb5"}')
        self.assertIsNone(read_job_record(path))

    def test_candidate_is_saved_but_not_delivery_authorized(self):
        reply = self.stage()
        record = self.record()
        self.assertEqual(self.saved()["state"], "candidate")
        self.assertEqual(record["verified_reply_fingerprint"], reply.fingerprint)
        self.assertIs(record["verified_reply_authorized"], False)
        self.assertEqual(record["result_retry_policy"], RETRY_POLICY)
        with self.assertRaises(ValueError):
            self.load(record)

    def test_each_missing_confirmation_prevents_authorization(self):
        reply = self.stage()
        for field in ("identity_confirmed", "cleanup_confirmed", "protection_confirmed"):
            with self.subTest(field=field):
                self.assert_authorization_denied(reply, confirmed_outcome(**{field: False}))
                self.assertEqual(self.saved()["state"], "candidate")
                self.assertIs(self.record()["verified_reply_authorized"], False)

    def test_authorized_partial_reply_keeps_failed_execution_and_no_resume(self):
        reply, outcome, record = self.grant()
        self.assertEqual(self.load(record), (reply, outcome))
        self.assertEqual(record["execution_status"], "failed")
        self.assertIs(record["native_resume_allowed"], False)
        self.assertIs(record["verified_reply_authorized"], True)
        self.assertIs(record["delivery_ok"], False)
        self.assertEqual(record["final_text"], reply.text)

    def test_completed_authorization_can_keep_continuation_disabled(self):
        reply, outcome, record = self.grant(confirmed_outcome(status="completed", error_code=""))
        self.assertEqual(self.load(record), (reply, outcome))
        self.assertEqual(record["execution_status"], "done")
        self.assertIs(record["native_resume_allowed"], False)

    def test_no_candidate_cannot_be_authorized(self):
        self.assert_authorization_denied(reply_value())
        self.assertIsNot(self.record().get("verified_reply_authorized"), True)

    def test_legacy_tuple_error_progress_and_duck_type_are_not_candidates(self):
        reply = reply_value()
        invalid = ((reply.text, reply.session_id, False), "backend failed", {"text": reply.text},
                   {"kind": "answer_snapshot", "text": reply.text}, SimpleNamespace(**reply.to_dict()))
        for value in invalid:
            with self.subTest(kind=type(value).__name__), self.assertRaises((ValueError, TypeError)):
                observe_verified_reply(self.ctx, self.job, self.backend, value)
        self.assertIsNone(self.job.verified_reply)
        self.assertEqual(list((self.state / "verified-results").glob("*.json")), [])

    @requires_extension()
    def test_observation_rejects_cross_backend_and_cross_run(self):
        for reply in (reply_value(backend="maso"), reply_value(run_id=OTHER_RUN_ID)):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                observe_verified_reply(self.ctx, self.job, self.backend, reply)
        self.assertIsNone(self.job.verified_reply)

    def test_revocations_before_any_candidate_advance_the_generation(self):
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        self.assertEqual(self.saved()["state"], "invalidated")
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        self.assertEqual(self.saved()["generation"], 3)
        with self.assertRaises(ValueError):
            self.stage(reply_value(generation=1))
        reply = self.stage(reply_value(generation=3))
        self.assertEqual((self.saved()["generation"], self.record()["verified_reply_generation"]), (3, 3))
        self.assertEqual(self.saved()["fingerprint"], reply.fingerprint)

    def test_storage_failure_keeps_no_candidate(self):
        with mock.patch("covey.core.saved_results.write_private_text", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                self.stage()
        self.assertIsNone(self.job.verified_reply)
        self.assert_authorization_denied(reply_value())
        self.stage()
        self.assertEqual(self.saved()["generation"], 1)
        self.assertNotIn("authorization", self.saved())

    def test_none_revokes_candidate_and_prevents_old_authorization(self):
        reply = self.stage()
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        self.assertIsNone(self.job.verified_reply)
        self.assertEqual(self.saved()["state"], "invalidated")
        self.assertIs(self.record()["verified_reply_authorized"], False)
        self.assert_authorization_denied(reply)

    def test_none_revokes_authorized_reply_even_with_stale_job_record(self):
        reply, outcome, stale_record = self.grant()
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        with self.assertRaises(ValueError):
            self.load(stale_record)
        self.assert_authorization_denied(reply, outcome)
        self.assertNotIn("outcome", self.saved())

    def test_replacement_is_unapproved_and_old_reply_cannot_authorize_it(self):
        old_reply, _, stale_record = self.grant()
        replacement = replace(old_reply, text="New verified result", message_ids=("new-final",), generation=2)
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        self.stage(replacement)
        self.assertEqual(self.saved()["state"], "candidate")
        self.assertIs(self.record()["verified_reply_authorized"], False)
        self.assert_authorization_denied(old_reply)
        with self.assertRaises(ValueError):
            self.load(stale_record)
        self.assertTrue(authorize_verified_reply(
            self.ctx, self.job, self.backend, replacement,
            confirmed_outcome(reply_fingerprint=replacement.fingerprint),
        ))
        self.assertEqual(self.load()[0], replacement)
        with self.assertRaises(ValueError):
            self.load(stale_record)

    def test_boundary_only_replacement_rejects_old_reply_and_stale_record(self):
        old_reply, _, stale_record = self.grant()
        replacement = replace(old_reply, boundary=("new-user-boundary",), generation=2)
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        self.stage(replacement)
        self.assert_authorization_denied(old_reply)
        with self.assertRaises(ValueError):
            self.load(stale_record)
        self.assertTrue(authorize_verified_reply(
            self.ctx, self.job, self.backend, replacement,
            confirmed_outcome(reply_fingerprint=replacement.fingerprint),
        ))
        self.assertEqual(self.load()[0], replacement)
        with self.assertRaises(ValueError):
            self.load(stale_record)

    def test_revocation_without_any_candidate_advances_expected_generation(self):
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        with self.assertRaises(ValueError):
            self.stage(reply_value(generation=1))
        self.assert_authorization_denied(reply_value(generation=1))
        current = self.stage(reply_value(generation=2))
        self.assertEqual(self.saved()["reply"]["generation"], 2)
        self.assertTrue(authorize_verified_reply(
            self.ctx, self.job, self.backend, current,
            confirmed_outcome(reply_fingerprint=current.fingerprint),
        ))
        self.assertEqual(self.load()[0], current)

    def test_each_empty_revocation_advances_generation(self):
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        for generation in (1, 2):
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                self.stage(reply_value(generation=generation))
        current = self.stage(reply_value(generation=3))
        self.assertEqual(self.saved()["reply"]["generation"], current.generation)

    def test_equal_body_new_generation_is_legal_after_revocation(self):
        previous, previous_outcome, old_record = self.grant()
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        current = replace(previous, generation=previous.generation + 1)
        self.assertEqual(current.text, previous.text)
        self.assertEqual(current.message_ids, previous.message_ids)
        self.assertEqual(current.boundary, previous.boundary)
        self.assertNotEqual(current.fingerprint, previous.fingerprint)
        self.stage(current)
        with self.assertRaises(ValueError):
            self.stage(previous)
        self.assert_authorization_denied(previous, previous_outcome)
        self.assertTrue(authorize_verified_reply(
            self.ctx, self.job, self.backend, current,
            confirmed_outcome(reply_fingerprint=current.fingerprint),
        ))
        self.assertEqual(self.load()[0], current)
        with self.assertRaises(ValueError):
            self.load(old_record)

    def test_delayed_old_observation_does_not_replace_current_authorization(self):
        previous = self.stage()
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        current = self.stage(replace(previous, generation=2))
        outcome = confirmed_outcome(reply_fingerprint=current.fingerprint)
        self.assertTrue(authorize_verified_reply(self.ctx, self.job, self.backend, current, outcome))
        before = self.saved_path().read_bytes()
        record = self.record()
        old_wire_copy = VerifiedRootReply.from_dict(json.loads(json.dumps(previous.to_dict())))
        with self.assertRaises(ValueError):
            self.stage(old_wire_copy)
        self.assert_authorization_denied(old_wire_copy)
        self.assertEqual(self.saved_path().read_bytes(), before)
        self.assertEqual(self.load(record), (current, outcome))

    def test_same_generation_deserialized_reply_can_be_authorized(self):
        original = self.stage()
        deserialized = VerifiedRootReply.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertIsNot(deserialized, original)
        self.assertEqual(deserialized, original)
        outcome = confirmed_outcome(reply_fingerprint=deserialized.fingerprint)
        self.assertTrue(authorize_verified_reply(self.ctx, self.job, self.backend, deserialized, outcome))
        self.assertEqual(self.load(), (original, outcome))

    def test_revocation_generation_survives_fresh_job_object(self):
        previous = self.stage()
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        reloaded_job = SimpleNamespace(
            bridge_run_id=RUN_ID, job_id=7, message_id="message-fixture", chat_id="chat-fixture",
            topic_id=self.ctx.topic_id, verified_reply=None, verified_result_key=None,
        )
        with self.assertRaises(ValueError):
            observe_verified_reply(self.ctx, reloaded_job, self.backend, previous)
        current = replace(previous, generation=2)
        observe_verified_reply(self.ctx, reloaded_job, self.backend, current)
        self.assertTrue(authorize_verified_reply(
            self.ctx, reloaded_job, self.backend, current,
            confirmed_outcome(reply_fingerprint=current.fingerprint),
        ))
        self.assertEqual(self.load()[0], current)

    def test_saved_generation_is_explicit_and_strict_on_reload(self):
        _, _, record = self.grant()
        original = self.saved()
        self.assertIs(type(original["reply"]["generation"]), int)
        self.assertEqual(original["reply"]["generation"], 1)
        for value in (True, 1.0, 0, -1, None, "1"):
            with self.subTest(generation=value):
                changed = copy.deepcopy(original)
                changed["reply"]["generation"] = value
                self.write_saved(changed)
                with self.assertRaises((ValueError, TypeError)):
                    self.load(record)
        changed = copy.deepcopy(original)
        changed["reply"].pop("generation")
        self.write_saved(changed)
        with self.assertRaises(ValueError):
            self.load(record)
        self.write_saved(original)

    def test_authorization_requires_nonempty_matching_outcome_reply_fingerprint(self):
        reply = self.stage()
        for fingerprint in ("", "0" * 64, reply_value(generation=2).fingerprint):
            with self.subTest(fingerprint=fingerprint):
                self.assert_authorization_denied(reply, confirmed_outcome(reply_fingerprint=fingerprint))
                self.assertEqual(self.saved()["state"], "candidate")
        missing = ExecutionOutcome(status="failed", error_code="child_unknown", identity_confirmed=True,
                                   cleanup_confirmed=True, protection_confirmed=True)
        self.assert_authorization_denied(reply, missing)

    def test_new_generation_cannot_use_previous_outcome_authorization(self):
        previous, previous_outcome, _ = self.grant()
        observe_verified_reply(self.ctx, self.job, self.backend, None)
        current = self.stage(replace(previous, generation=2))
        self.assert_authorization_denied(current, previous_outcome)
        self.assertEqual(self.saved()["state"], "candidate")
        current_outcome = replace(previous_outcome, reply_fingerprint=current.fingerprint)
        self.assertTrue(authorize_verified_reply(self.ctx, self.job, self.backend, current, current_outcome))
        self.assertEqual(self.load(), (current, current_outcome))

    def test_repeated_observation_of_current_reply_does_not_regrant_or_rewrite(self):
        reply, _, record = self.grant()
        before = self.saved_path().read_bytes()
        observe_verified_reply(self.ctx, self.job, self.backend, reply)
        self.assertEqual(self.saved_path().read_bytes(), before)
        self.assertEqual(self.record()["verified_reply_state"], "authorized")
        self.assertEqual(self.load(record)[0], reply)

    def test_duplicate_authorization_is_not_a_new_authorization(self):
        reply, outcome, _ = self.grant()
        upsert_job_record(self.state, self.job.message_id, delivery_ok=True)
        before = self.saved_path().read_bytes()
        record_before = self.record()
        result = authorize_verified_reply(self.ctx, self.job, self.backend, reply, outcome)
        self.assertIs(type(result), bool)
        self.assertEqual(self.saved_path().read_bytes(), before)
        self.assertEqual(self.record(), record_before)
        self.assertIs(self.record()["delivery_ok"], True)

    def test_changed_live_job_scope_cannot_authorize_old_candidate(self):
        reply = self.stage()
        changes = ({"bridge_run_id": OTHER_RUN_ID}, {"job_id": 8}, {"message_id": "other-message"},
                   {"chat_id": "other-chat"}, {"topic_id": "other-topic"})
        for change in changes:
            with self.subTest(change=change):
                changed = SimpleNamespace(**{**vars(self.job), **change})
                self.assert_authorization_denied(reply, job=changed)
        changed_ctx = SimpleNamespace(**{**vars(self.ctx), "workdir": self.root / "other-workspace"})
        self.assert_authorization_denied(reply, ctx=changed_ctx)
        self.assert_authorization_denied(reply, backend="maso")
        self.assertEqual(self.saved()["state"], "candidate")

    def test_saved_scope_tampering_prevents_authorization(self):
        reply = self.stage()
        original = self.saved()
        mutations = {"backend": "maso", "run_id": OTHER_RUN_ID, "job_key": "other-message", "job_id": 8,
                     "chat_id": "other-chat", "topic_id": "other-topic", "workdir": str(self.root / "other")}
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["scope"][field] = value
                self.write_saved(changed)
                self.assert_authorization_denied(reply)
        self.write_saved(original)

    def test_candidate_body_and_fingerprint_tampering_prevents_authorization(self):
        reply = self.stage()
        original = self.saved()
        for field in ("text", "fingerprint"):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                if field == "text":
                    changed["reply"]["text"] = "Altered public body"
                else:
                    changed["fingerprint"] = "0" * 64
                self.write_saved(changed)
                self.assert_authorization_denied(reply)
        self.write_saved(original)

    def test_candidate_version_must_be_exact_integer_one(self):
        reply = self.stage()
        original = self.saved()
        for version in (True, 1.0, "1", 2, None):
            with self.subTest(version=version):
                changed = copy.deepcopy(original)
                changed["version"] = version
                self.write_saved(changed)
                self.assert_authorization_denied(reply)
        self.write_saved(original)

    def test_load_rejects_legacy_records_and_forged_authorized_flags(self):
        reply = self.stage()
        record = self.record()
        for legacy in ({}, {"final_text": reply.text}, {"status": "failed", "final_text": "backend error"},
                       {"last_snapshot": reply.text}):
            with self.subTest(legacy=legacy), self.assertRaises(ValueError):
                self.load(legacy)
        record.update(verified_reply_authorized=True, verified_reply_state="authorized")
        with self.assertRaises(ValueError):
            self.load(record)

    def test_load_rejects_wrong_policy_state_and_nonboolean_authorization(self):
        _, _, original = self.grant()
        changes = ({"result_retry_policy": "rerun"}, {"verified_reply_state": "candidate"},
                   {"verified_reply_authorized": False}, {"verified_reply_authorized": 1},
                   {"verified_reply_authorized": "true"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load({**original, **change})

    def test_load_rejects_cross_backend_chat_topic_and_workdir(self):
        _, _, record = self.grant()
        contexts = (SimpleNamespace(**{**vars(self.ctx), "topic_id": "other-topic"}),
                    SimpleNamespace(**{**vars(self.ctx), "workdir": self.root / "other-workspace"}))
        for ctx in contexts:
            with self.subTest(ctx=ctx), self.assertRaises(ValueError):
                self.load(record, ctx=ctx)
        with self.assertRaises(ValueError):
            self.load(record, backend="maso")
        with self.assertRaises(ValueError):
            self.load(record, chat_id="other-chat")

    def test_load_rejects_cross_run_job_key_and_job_number(self):
        _, _, record = self.grant()
        for change in ({"result_run_id": OTHER_RUN_ID}, {"key": "other-message"}, {"job_id": 8}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load({**record, **change})

    def test_load_saved_scope_rejects_each_identity_mutation(self):
        _, _, record = self.grant()
        original = self.saved()
        mutations = {"backend": "maso", "run_id": OTHER_RUN_ID, "job_key": "other-message", "job_id": 8,
                     "chat_id": "other-chat", "topic_id": "other-topic", "workdir": str(self.root / "other")}
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["scope"][field] = value
                self.write_saved(changed)
                with self.assertRaises(ValueError):
                    self.load(record)
        self.write_saved(original)

    def test_load_version_requires_int_one_not_boolean_or_float(self):
        _, _, record = self.grant()
        original = self.saved()
        for version in (True, 1.0, "1", 0, 2, None):
            with self.subTest(version=version):
                changed = copy.deepcopy(original)
                changed["version"] = version
                self.write_saved(changed)
                with self.assertRaises(ValueError):
                    self.load(record)
        self.write_saved(original)

    def test_load_rejects_saved_reply_field_or_body_tampering(self):
        _, _, record = self.grant()
        original = self.saved()
        changes = {"backend": "maso", "run_id": OTHER_RUN_ID, "session_id": "other-session", "generation": 2,
                   "boundary": ["other-user"], "message_ids": ["other-final"], "text": "Changed body"}
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["reply"][field] = value
                self.write_saved(changed)
                with self.assertRaises(ValueError):
                    self.load(record)
        self.write_saved(original)

    def test_recomputed_saved_fingerprint_cannot_override_job_authorized_reply(self):
        _, _, record = self.grant()
        changed = self.saved()
        changed["reply"]["text"] = "Replacement body with its own correct digest"
        replacement = VerifiedRootReply.from_dict(changed["reply"])
        changed["fingerprint"] = replacement.fingerprint
        self.write_saved(changed)
        with self.assertRaises(ValueError):
            self.load(record)

    def test_load_rejects_both_saved_and_record_fingerprint_corruption(self):
        _, _, record = self.grant()
        original = self.saved()
        changed = copy.deepcopy(original)
        changed["fingerprint"] = "f" * 64
        self.write_saved(changed)
        with self.assertRaises(ValueError):
            self.load(record)
        self.write_saved(original)
        with self.assertRaises(ValueError):
            self.load({**record, "verified_reply_fingerprint": "f" * 64})

    def test_authorized_outcome_status_or_error_code_tampering_is_rejected(self):
        _, _, record = self.grant()
        original = self.saved()
        for field, value in (("status", "completed"), ("status", "timed_out"), ("error_code", "child_error"),
                             ("reply_fingerprint", reply_value(generation=2).fingerprint)):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(original)
                changed["outcome"][field] = value
                self.assertEqual(changed["fingerprint"], original["fingerprint"])
                self.write_saved(changed)
                with self.assertRaises((ValueError, TypeError)):
                    self.load(record)
        self.write_saved(original)

    def test_authorized_resume_permission_tampering_is_rejected(self):
        _, _, record = self.grant(confirmed_outcome(status="completed", error_code=""))
        changed = self.saved()
        changed["outcome"]["resume_allowed"] = True
        self.write_saved(changed)
        with self.assertRaises((ValueError, TypeError)):
            self.load(record)

    def test_matching_forged_job_fields_do_not_authorize_an_altered_outcome(self):
        _, _, record = self.grant()
        changed = self.saved()
        changed["outcome"].update(status="completed", error_code="", resume_allowed=True)
        self.write_saved(changed)
        forged = {**record, "execution_status": "done", "native_resume_allowed": True}
        with self.assertRaises((ValueError, TypeError)):
            self.load(forged)

    def test_job_execution_and_resume_fields_must_match_authorized_outcome(self):
        _, _, record = self.grant()
        for change in ({"execution_status": "done"}, {"execution_status": "cancelled"},
                       {"native_resume_allowed": True}, {"native_resume_allowed": 0},
                       {"native_resume_allowed": None}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                self.load({**record, **change})
        for field in ("execution_status", "native_resume_allowed"):
            changed = dict(record)
            changed.pop(field)
            with self.subTest(missing=field), self.assertRaises((ValueError, TypeError)):
                self.load(changed)

    def test_saved_outcome_requires_all_confirmations_as_real_booleans(self):
        _, _, record = self.grant()
        original = self.saved()
        for field in ("identity_confirmed", "cleanup_confirmed", "protection_confirmed"):
            for value in (False, 1):
                with self.subTest(field=field, value=value):
                    changed = copy.deepcopy(original)
                    changed["outcome"][field] = value
                    self.write_saved(changed)
                    with self.assertRaises((ValueError, TypeError)):
                        self.load(record)
        self.write_saved(original)

    def test_corrupt_or_missing_saved_record_has_no_legacy_fallback(self):
        _, _, record = self.grant()
        record["final_text"] = "Legacy body must not be used"
        self.saved_path().write_text("{broken-json", encoding="utf-8")
        with self.assertRaises((ValueError, OSError)):
            self.load(record)
        self.saved_path().unlink()
        with self.assertRaises((ValueError, OSError)):
            self.load(record)

    def test_missing_result_directory_is_not_created_by_load(self):
        _, _, record = self.grant()
        missing_state = self.root / "missing-state"
        missing_ctx = SimpleNamespace(**{**vars(self.ctx), "state_dir": missing_state})
        with self.assertRaises((ValueError, OSError)):
            self.load(record, ctx=missing_ctx)
        self.assertFalse(missing_state.exists())

    def test_load_is_pure_read_and_uses_only_saved_authorized_content(self):
        reply, outcome, record = self.grant()
        record["final_text"] = "Unverified latest progress"
        allowed_paths = {path.resolve() for path in self.saved_path().parent.glob("*.json")}
        allowed_paths.add(job_path(self.state, self.job.message_id).resolve())
        original_read = Path.read_text

        def checked_read(path, *args, **kwargs):
            # Durable generation indexes and job authorization facts are valid
            # reads; native sessions and progress stores are not.
            self.assertIn(path.resolve(), allowed_paths)
            return original_read(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "read_text", autospec=True, side_effect=checked_read),
            mock.patch.object(Path, "mkdir", side_effect=AssertionError("load must not create directories")),
            mock.patch("covey.core.saved_results.write_private_text", side_effect=AssertionError("load must not write")),
            mock.patch("covey.core.saved_results.upsert_job_record", side_effect=AssertionError("load must not update jobs")),
        ):
            self.assertEqual(self.load(record), (reply, outcome))

    def test_invalid_saved_key_cannot_escape_private_result_directory(self):
        _, _, record = self.grant()
        for key in (None, "", "../outside", "0" * 63, "A" * 64, "z" * 64):
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                self.load({**record, "verified_result_key": key})

    def test_saved_result_file_symlink_is_rejected_without_reading_target(self):
        _, _, record = self.grant()
        target = self.root / "fixture-target.json"
        target.write_text(self.saved_path().read_text(), encoding="utf-8")
        self.saved_path().unlink()
        self.saved_path().symlink_to(target)
        with self.assertRaises(ValueError):
            self.load(record)

    def test_saved_result_directory_symlink_is_rejected(self):
        _, _, record = self.grant()
        folder = self.saved_path().parent
        moved = self.root / "fixture-results"
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.load(record)

    def test_synthetic_job_key_matches_saved_scope(self):
        self.stage()
        self.assertEqual(self.saved()["scope"]["job_key"], job_key(self.job.message_id, self.job.job_id))
        self.assertEqual(self.saved()["scope"]["job_id"], self.job.job_id)
        self.assertEqual(self.saved()["scope"]["topic_id"], self.ctx.topic_id)
