"""A real Codex BridgeApp for the native question and delivery status tests.

``CodexChainCase.prepare_case`` builds it with the App Server RPC, Git discovery
and outbound transport simulated; no RunResult is manufactured, the production
adapter constructs every result. The shared delivery invariants themselves are
in ``test_backend_contract`` over ``support.backend_drivers``.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_backends import FakeRpc, agent, codex_context, finished, turn, codex_server
from support.fake_lark import OutboundTransport, install_card_transport
from support.world import harness_app
from covey.core.jobs import BridgeReplyJob
from covey.core.state import write_private_text
from covey.backends.codex.bridge import CODEX_SPEC, CodexBackend
from covey.backends.codex.sessions import CodexSessionStore

ROOT_TEXT = "# Codex verified report\n\n" + ("Current-run public evidence and limits.\n" * 12) + "\nVerified end."
ERROR_SECRET = "PRIVATE_CODEX_ERROR_SENTINEL"
QUESTION = "Synthetic partial checkpoint; never register this question."


class CodexChainCase(unittest.TestCase):
    """``prepare_case`` returns a namespace ``c`` with the app, job, transport and wrapped seams."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def prepare_case(self, *, partial=False, send_ok=False):
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="codex-shared-fixture-")))
        ctx = codex_context(root, "app-server")
        # The Codex Harness's context, as the Bridge builds it: its topics are bound to Codex.
        ctx = dataclasses.replace(ctx, topic_id="topic-codex-chain", reply_to_message_id="om-root", timeout=0,
                                  options={**ctx.options, "harness_id": "codex"})
        ctx.state_dir.mkdir(mode=0o700)
        backend = CodexBackend()
        app = harness_app(CODEX_SPEC, backend, CodexSessionStore())
        tx = OutboundTransport(ok=send_ok)
        app.messenger.send_message = tx.send_text
        app.artifacts.send_result_file = tx.send_file
        app.runtime.log_sink = mock.Mock()
        app.cards.stream = dataclasses.replace(app.cards.stream, send_text_message=tx.send_text)
        install_card_transport(self.stack, tx)
        # This temporary workspace is not Git. Keep the real diff functions,
        # replacing only the external Git command used to discover that fact.
        git = self.stack.enter_context(mock.patch("covey.core.diff._run_git",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="")))
        job = BridgeReplyJob(job_id=31, user_text="Produce a synthetic Codex public report.",
            chat_id="chat-codex-chain", message_id="om-codex-chain", topic_id=ctx.topic_id,
            reply_to_message_id=ctx.reply_to_message_id, timeout=0, model=ctx.model,
            effort=ctx.effort, model_settings_snapshot=True, progress=None)
        c = SimpleNamespace(root=root, ctx=ctx, app=app, backend=backend, tx=tx, job=job,
            partial=partial, rpc_instances=[], git=git)
        c.backend_run = self.stack.enter_context(mock.patch.object(backend, "run", wraps=backend.run))
        c.finalize_session = self.stack.enter_context(mock.patch.object(app.sessions, "finalize_session_from_worker",
            wraps=app.sessions.finalize_session_from_worker))
        import covey.app.sessions as sessions
        c.cas = self.stack.enter_context(mock.patch.object(sessions, "compare_and_set_session",
            wraps=sessions.compare_and_set_session))
        original_event = app.interaction.structured_harness_event

        def event(context_, actual_job, kind, data, rpc):
            result = original_event(context_, actual_job, kind, data, rpc)
            if partial and kind == "turn_finished":
                raise RuntimeError(ERROR_SECRET)  # Independent terminal callback fault.
            return result

        if partial:
            app.interaction.structured_harness_event = event

        def checkpoint():
            if partial:
                write_private_text(Path(job.checkpoint_path), json.dumps({"requires_user": True,
                    "question": QUESTION, "phase": "synthetic-checkpoint"}))

        def rpc_factory(argv, cwd, env):
            rpc = FakeRpc([finished(turn([agent("final", ROOT_TEXT)]))])
            request = rpc.request
            def read(method, params, timeout=30):
                if method == "turn/start":
                    checkpoint()
                return request(method, params, timeout)
            rpc.request = read
            c.rpc_instances.append(rpc)
            return rpc
        self.stack.enter_context(codex_server(factory=rpc_factory))
        return c
