"""One driver per live backend for the shared contract in ``test_backend_contract.py``.

A driver builds a real BridgeApp of its backend. Only the model boundary (the
Codex App Server RPC, a scripted Claude, dsh or pi child process, the
in-memory MASO SDK), Git discovery and the Feishu transport are simulated; the
production adapter builds every result. A scenario says what the one turn does:

- ``complete``: a verified reply and a clean finish;
- ``partial``: a verified reply, then the turn fails (Codex: a terminal callback
  fault, Claude: a child failing late, MASO: failed and aborted children);
- ``failed``: text appears but the turn fails before it is verified.

``driver.run(case, scenario)`` is a context manager yielding a namespace with
``app``, ``ctx``, ``job``, ``tx`` (an OutboundTransport, failing until a test sets
``tx.ok``), ``body`` (the verified reply), ``secrets`` (texts that must never be
sent), ``backend_run``/``authorize``/``cas``/``finalize_session`` (wrapping mocks),
``candidates`` (the record as each verified candidate was observed) and, after
``process()``, ``sid`` (the native session of the turn).
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import FIXTURES, make_context
from support.fake_backends import CLAUDE_JSONL, OTHER, SID, TURN, FakeRpc, agent, codex_context, finished, turn, codex_server
from support.worker_seams import leave_out_side_work
from support.fake_lark import OutboundTransport, install_card_transport
from support.sessions import write_session_id
from support.world import harness_app
from covey.app.harnesses import components
from covey.core.jobs import BridgeReplyJob, find_job_record
from covey.core.session_guard import session_is_blocked
from covey.core.state import write_private_text
from covey.backends.gateway import connection
from support import acme as acme_gateway
try:  # The company extension; without it the contract runs the core's Harnesses only.
    from support.maso_transport import InMemoryMasoTransport
    from covey_internal.maso.sessions import MasoSessionError, MasoSessionLease
except ModuleNotFoundError:
    InMemoryMasoTransport = None

QUESTION = "Synthetic checkpoint question; a partial run must never publish it."
ERROR_SECRET = "PRIVATE_BACKEND_ERROR_SENTINEL"


class Driver:
    name = ""
    scenarios = ("complete", "partial", "failed")
    sid = SID
    other_sid = OTHER

    def __repr__(self):
        return self.name

    @contextlib.contextmanager
    def run(self, case, scenario, *, resume=True, send_ok=False, on_candidate=None):
        with contextlib.ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=self.name + "-contract-")))
            run = SimpleNamespace(driver=self, case=case, scenario=scenario, resume=resume, root=root,
                                  stack=stack, tx=OutboundTransport(ok=send_ok), candidates=[], secrets=[])
            run.ctx = dataclasses.replace(self.context(run), topic_id="topic-contract",
                                          reply_to_message_id="om-root")
            run.ctx.state_dir.mkdir(mode=0o700, exist_ok=True)
            spec, backend, store = components(self.name)
            run.app = app = harness_app(spec, backend, store)
            app.runtime.log_sink = mock.Mock()
            app.messenger.send_message = run.tx.send_text
            app.artifacts.send_result_file = run.tx.send_file
            app.cards.stream = dataclasses.replace(app.cards.stream, send_text_message=run.tx.send_text)
            install_card_transport(stack, run.tx)
            # The workspace is not Git: the real diff code runs, its one Git call answers so.
            stack.enter_context(mock.patch("covey.core.diff._run_git",
                                           return_value=SimpleNamespace(returncode=1, stdout="", stderr="")))
            leave_out_side_work(stack, diff=False, closing=False)
            if resume:
                self.seed_session(run)
            self.install(run)
            run.job = BridgeReplyJob(job_id=7, user_text="Produce the synthetic public result.",
                                     chat_id="chat-contract", message_id="om-contract", topic_id=run.ctx.topic_id,
                                     reply_to_message_id=run.ctx.reply_to_message_id, timeout=run.ctx.timeout,
                                     model=run.ctx.model, effort=run.ctx.effort, model_settings_snapshot=True,
                                     progress=None)
            wrap = lambda owner, name: stack.enter_context(  # noqa: E731
                mock.patch.object(owner, name, wraps=getattr(owner, name)))
            run.backend_run = wrap(backend, "run")
            run.defer_question = wrap(app.questions, "defer_checkpoint_question")
            run.deliver_question = wrap(app.questions, "deliver_checkpoint_questions")
            run.finalize_session = wrap(app.sessions, "finalize_session_from_worker")
            import covey.core.saved_results as saved
            import covey.app.sessions as sessions
            run.cas = wrap(sessions, "compare_and_set_session")
            run.authorize = stack.enter_context(mock.patch("covey.app.turn_settle.authorize_verified_reply",
                                                           wraps=saved.authorize_verified_reply))
            original_observe = saved.observe_verified_reply

            def observe(context, job, backend_name, reply):
                result = original_observe(context, job, backend_name, reply)
                if reply is not None:
                    record = find_job_record(run.ctx.state_dir, job.message_id)
                    run.candidates.append({"state": record.get("verified_reply_state"),
                                           "authorized": record.get("verified_reply_authorized"),
                                           "sid_blocked": session_is_blocked(context, self.name, reply.session_id)})
                    if on_candidate is not None:
                        on_candidate(run)
                return result

            stack.enter_context(mock.patch("covey.app.runs.observe_verified_reply", side_effect=observe))
            run.process = lambda: self.process(run)
            yield run

    def process(self, run):
        run.app.process_reply_job(run.job, run.ctx)
        record = find_job_record(run.ctx.state_dir, run.job.message_id)
        run.case.assertIsInstance(record, dict)
        run.case.assertEqual(run.backend_run.call_count, 1)
        request = run.backend_run.call_args.args[0]
        run.sid = self.turn_session(run)
        run.case.assertEqual(request.session_id, run.sid if run.resume else None)
        run.case.assertEqual(request.inputs.run_id, run.job.bridge_run_id)
        self.check_boundary(run, record)
        return record

    # Per backend.
    def context(self, run):
        raise NotImplementedError

    def seed_session(self, run):
        write_session_id(run.ctx.session_path, self.sid)

    def install(self, run):
        """Script the model boundary for ``run.scenario``; set ``run.body`` and ``run.secrets``."""
        raise NotImplementedError

    def turn_session(self, run):
        return self.sid

    def check_boundary(self, run, record):
        """What the backend's own boundary saw (one process, one turn ...)."""

    def check_sealed(self, run):
        """Backend-specific proof that a sealed session cannot be resumed."""


class CodexDriver(Driver):
    """The Codex App Server over ``FakeRpc``."""

    name = "codex"
    BODY = "# Verified report\n\n" + ("Current-run public evidence and limits.\n" * 12) + "\nVerified end."
    UNVERIFIED = "UNVERIFIED_CODEX_BODY_SENTINEL"

    def context(self, run):
        ctx = codex_context(run.root, "app-server")
        return dataclasses.replace(ctx, timeout=0, options={**ctx.options, "harness_id": self.name})

    def install(self, run):
        run.body, run.rpcs = self.BODY, []
        failed = run.scenario == "failed"
        if failed:
            run.secrets = [self.UNVERIFIED, self.BODY, ERROR_SECRET]
        if run.scenario == "partial":
            run.secrets = [ERROR_SECRET]
            original = run.app.interaction.structured_harness_event

            def event(context, job, kind, data, rpc):
                result = original(context, job, kind, data, rpc)
                if kind == "turn_finished":
                    raise RuntimeError(ERROR_SECRET)  # A terminal callback fault after the verified reply.
                return result

            run.app.interaction.structured_harness_event = event

        def rpc_factory(argv, cwd, env):
            rows = ([{"method": "item/completed", "params": {"threadId": SID, "turnId": TURN,
                                                             "item": agent("prefix", self.UNVERIFIED)}}]
                    if failed else [])
            rows.append(finished(turn([agent("final", self.BODY)], status="failed" if failed else "completed",
                                      error={"message": ERROR_SECRET} if failed else None)))
            rpc = FakeRpc(rows)
            run.rpcs.append(rpc)
            return rpc

        run.stack.enter_context(codex_server(factory=rpc_factory))

    def check_boundary(self, run, record):
        run.case.assertEqual(run.job.native_session_claims, {SID})
        run.case.assertEqual(len(run.rpcs), 1)
        rpc = run.rpcs[0]
        run.case.assertTrue(rpc.closed)
        methods = [name for name, _ in rpc.requests]
        run.case.assertEqual(methods.count("turn/start"), 1)
        run.case.assertEqual(methods.count("thread/resume"), int(run.resume))
        run.case.assertEqual(methods.count("thread/start"), int(not run.resume))
        if run.resume:
            run.case.assertEqual(next(p for name, p in rpc.requests if name == "thread/resume")["threadId"], SID)


class ClaudeDriver(Driver):
    """Claude stream-json over the scripted ``claude_jsonl.py`` child process."""

    name = "claude"
    SCRIPTS = {"complete": "success", "partial": "child-failed-late", "failed": "max-tokens"}

    def context(self, run):
        ctx = codex_context(run.root, "stream-json")
        return dataclasses.replace(ctx, timeout=8, options={**ctx.options, "harness_id": self.name},
                                   backend_cmd=[sys.executable, str(CLAUDE_JSONL), self.SCRIPTS[run.scenario]])

    def install(self, run):
        run.app.cards.streaming = False
        run.body = "Complete answer"
        if run.scenario == "failed":  # The answer stops at the output limit.
            run.secrets = [run.body]


class MasoDriver(Driver):
    """MASO over ``InMemoryMasoTransport``: the real worker, run_sdk, completion and leases."""

    name = "maso"
    BODY = ("# Verified root plan\n\n" + "Current-run public findings and explicit limitations.\n" * 180
            + "\n# Verified ending\nPreserve every line.")
    CHILDREN = {"complete": ("completed",), "partial": ("completed", "error", "aborted"), "failed": ("completed",)}

    def context(self, run):
        return make_context(run.root, profile="synthetic-maso", backend_cmd=["synthetic-maso-python"],
                            model="synthetic-model", effort="", options={"completion_wait_seconds": 3})

    def seed_session(self, run):
        # The previous turn's owner, made by the production lease rather than a fake result.
        with MasoSessionLease(run.ctx.state_dir, run.ctx.topic_id, run.ctx.workdir, None) as lease:
            run.old_sid = lease.session_id
            lease.start("00000000-0000-4000-8000-000000000001")
            lease.finish(True)
        write_session_id(run.ctx.session_path, run.old_sid)
        run.case.assertEqual(run.app.sessions.capture_session(run.ctx)[1], run.old_sid)

    def install(self, run):
        app = run.app
        app.cards.streaming = False
        app.cards.finish_result_controls = mock.Mock()  # Only the recovery card's presentation.
        app.cards.stream = dataclasses.replace(app.cards.stream, card_limit=20000, replacement_limit=0)
        run.stack.enter_context(mock.patch("covey.app.worker.close_streaming_reply_async"))
        run.body = self.BODY
        if run.scenario == "failed":
            run.secrets = [self.BODY]
        run.transport = InMemoryMasoTransport(self.BODY, children=self.CHILDREN[run.scenario],
                                              root_error=run.scenario == "failed")
        run.transport.install(run.stack)

    def turn_session(self, run):
        return run.old_sid if run.resume else run.transport.spawn_session_ids[0]

    def check_boundary(self, run, record):
        t = run.transport
        run.case.assertEqual(len(t.payloads), 1)
        run.case.assertEqual((t.sdk_resumes, t.sdk_spawns), (1, 0) if run.resume else (0, 1))
        run.case.assertIs(t.payloads[0]["resume"], run.resume)
        run.case.assertEqual(t.payloads[0]["session_id"], run.sid)
        kinds = [event.get("type") for event in t.wire_events]
        run.case.assertLess(kinds.index("terminal"), kinds.index("cleanup"))
        run.case.assertEqual("result" in kinds, run.scenario == "complete")
        terminal = next(event for event in t.wire_events if event.get("type") == "terminal")
        children = {row["classification"] for row in terminal["agents"] if row["agent_id"] != run.sid}
        run.case.assertEqual(children, set(self.CHILDREN[run.scenario]))
        if run.scenario != "failed":
            run.case.assertLess(kinds.index("root_reply"), kinds.index("terminal"))
        # Children report under the root: named from the Task call, their usage the subtask report the
        # record keeps (a child without usage is unreported), never the root's own figures.
        states = {event["id"]: event for event in t.wire_events if event.get("type") == "agent_state"}
        run.case.assertEqual(len(states), len(self.CHILDREN[run.scenario]))
        named = "Check the synthetic…" if len(self.CHILDREN[run.scenario]) == 1 else "Synthetic brief."
        run.case.assertTrue(all(e["name"] == named and "parent_id" not in e for e in states.values()))
        completed = self.CHILDREN[run.scenario].count("completed")
        usage = record["subtask_usage"]
        run.case.assertEqual((usage["count"], usage["unreported"]), (completed, len(self.CHILDREN[run.scenario]) - completed))
        run.case.assertEqual(usage["levels"][0]["output_tokens"], 5 * completed)
        run.case.assertEqual(usage["levels"][0]["input_tokens"], 50 * completed)
        owner = json.loads((run.ctx.state_dir / "maso-sessions" / run.sid / "owner.json").read_text())
        run.case.assertEqual(owner["status"], "completed" if run.scenario == "complete" else "interrupted")

    def check_sealed(self, run):
        with run.case.assertRaises(MasoSessionError):
            with MasoSessionLease(run.ctx.state_dir, run.ctx.topic_id, run.ctx.workdir, run.sid):
                run.case.fail("an interrupted owner was resumed")


class DshDriver(Driver):
    """dsh headless over the scripted ``dsh_headless.py`` child process; dsh has no children."""

    name = "dsh"
    scenarios = ("complete", "failed")
    sid = "session-11111111-1111-4111-8111-111111111111"
    other_sid = "session-22222222-2222-4222-8222-222222222222"
    SCRIPTS = {"complete": "completed", "failed": "exit-after-final"}
    BODY = "Verified dsh answer"

    def context(self, run):
        return make_context(run.root, model="ali-deepseek-v4.1-flash", effort="max", timeout=8,
                            backend_cmd=[sys.executable, "-I", str(FIXTURES / "harness/dsh_headless.py")],
                            options={"harness_id": self.name})

    def install(self, run):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("ACME_", "DSH_"))}
        run.stack.enter_context(mock.patch.dict(os.environ, {
            **clean, "HOME": str(run.root), "ACME_API_KEY": "sk-unit-private-key",
            "FAKE_DSH_RECORD": str(run.root / "record.json"), "FAKE_DSH_SCENARIO": self.SCRIPTS[run.scenario],
            "FAKE_DSH_TEXT": self.BODY}, clear=True))
        path = connection.cache_path(acme_gateway(), run.ctx.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updated_at": time.time(), "rows": [
            {"id": "ali-deepseek-v4.1-flash", "protocol": "anthropic-messages",
             "supported_reasoning_levels": ["low", "high", "max"], "default_reasoning_level": "high"}]}))
        run.app.cards.streaming = False
        run.body = self.BODY
        if run.scenario == "failed":  # The final row arrives, then the process exits 3.
            run.secrets = [run.body]


class PiDriver(Driver):
    """Pi over the scripted ``pi_rpc.py`` RPC child on the Codex login; pi has no children."""

    name = "pi"
    scenarios = ("complete", "failed")
    sid = "01a1a217-4147-747b-a08f-83323257ddf3"
    other_sid = "01a1a21e-5b23-7308-9dd0-f72c93ca797c"
    SCRIPTS = {"complete": "ok", "failed": "exit-after-end"}
    BODY = "Verified Pi answer"

    def context(self, run):
        return make_context(run.root, model="gpt-6-astra", effort="high", timeout=8,
                            backend_cmd=[sys.executable, "-I", str(FIXTURES / "harness/pi_rpc.py")],
                            options={"harness_id": self.name})

    def seed_session(self, run):
        # pi resumes by the session's file (backends/pi/bridge.py), so the previous turn left one.
        folder = run.ctx.state_dir / "pi-sessions"
        folder.mkdir()
        (folder / f"2026-11-04T00-00-00-000Z_{self.sid}.jsonl").write_text("")
        super().seed_session(run)

    def install(self, run):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("ACME_", "PI_", "CODEX_", "FAKE_PI"))}
        run.stack.enter_context(mock.patch.dict(os.environ, {
            **clean, "HOME": str(run.root), "FAKE_PI_RECORD": str(run.root / "pi-record.json"),
            "FAKE_PI_SCENARIO": self.SCRIPTS[run.scenario], "FAKE_PI_TEXT": self.BODY}, clear=True))
        codex = run.root / ".codex"
        codex.mkdir(exist_ok=True)
        claims = json.dumps({"exp": int(time.time()) + 86400}).encode()
        token = "e30." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".c2ln"
        (codex / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}))
        (codex / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}]}]}))
        run.app.cards.streaming = False
        run.body = self.BODY
        if run.scenario == "failed":  # The reply streams and its turn ends, then pi exits 3 before the end is confirmed.
            run.secrets = [run.body]


DRIVERS = (CodexDriver(), ClaudeDriver(), *([MasoDriver()] if InMemoryMasoTransport else []), DshDriver(), PiDriver())


def seed_checkpoint_question(run):
    """What a model's own checkpoint file would ask; a partial run must never publish it."""
    write_private_text(Path(run.job.checkpoint_path), json.dumps({
        "requires_user": True, "question": QUESTION, "phase": "synthetic-checkpoint"}))


def point_session_at_other(run):
    """A concurrent explicit session selection through the real pointer writer."""
    write_session_id(run.ctx.session_path, run.driver.other_sid)
