"""A forked topic's first run starts a new native session copied from the source."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from covey.core.harness import RunRequest
from covey.backends.claude.bridge import ClaudeBackend  # noqa: E402
from covey.backends.claude.argv import stream_argv  # noqa: E402
from covey.backends.codex.bridge import CodexBackend  # noqa: E402
from support.fake_backends import FakeRpc, SID, codex_context, finished, codex_server  # noqa: E402

SOURCE = "22222222-2222-4222-8222-222222222222"
FORKED = "33333333-3333-4333-8333-333333333333"


class ForkingRpc(FakeRpc):
    def request(self, method, params, timeout=30):
        if method == "thread/fork":
            self.requests.append((method, params))
            return {"thread": {"id": self.start_sid}}
        return super().request(method, params, timeout)


class NativeForkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_claude_resumes_the_source_into_a_new_session_only_before_its_own(self):
        ctx = codex_context(self.root, "stream-json")
        ctx.options["fork_session"] = SOURCE
        backend = ClaudeBackend()
        argv = stream_argv(backend, ctx)
        index = argv.index("--resume")
        self.assertEqual(argv[index:index + 3], ["--resume", SOURCE, "--fork-session"])
        argv = stream_argv(backend, ctx, SID)
        self.assertEqual(argv[argv.index("--resume") + 1], SID)
        self.assertNotIn("--fork-session", argv)
        self.assertNotIn(SOURCE, argv)

    def test_codex_forks_the_source_thread_and_keeps_the_new_one(self):
        ctx = codex_context(self.root, "app-server")
        ctx.options["fork_session"] = SOURCE
        rpc = ForkingRpc([finished()], start_sid=FORKED)
        with codex_server(rpc):
            result = CodexBackend().run(RunRequest(ctx, "unit probe", None, instructions="unit bridge rules"))
        methods = [name for name, _ in rpc.requests]
        self.assertIn("thread/fork", methods)
        self.assertNotIn("thread/start", methods)
        params = next(params for name, params in rpc.requests if name == "thread/fork")
        self.assertEqual((params["threadId"], params["cwd"]), (SOURCE, str(self.root)))
        self.assertTrue(params["developerInstructions"].startswith("unit bridge rules\n\n"))
        self.assertEqual(result.session_id, FORKED)

    def test_codex_refuses_a_fork_that_returns_the_source_thread(self):
        ctx = codex_context(self.root, "app-server")
        ctx.options["fork_session"] = SOURCE
        rpc = ForkingRpc([finished()], start_sid=SOURCE)
        with codex_server(rpc):
            result = CodexBackend().run(RunRequest(ctx, "unit probe", None))
        self.assertNotIn("turn/start", [name for name, _ in rpc.requests])
        self.assertTrue(result.is_error)

if __name__ == "__main__":
    unittest.main()
