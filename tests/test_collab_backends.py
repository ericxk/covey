"""协作咨询轮的硬只读 (C8 layer 1, docs/architecture.md 多参与者协作 · 能力与预检): a 协作 ask's turn is held to CONSULT,
the strictest limit, whatever its options carry; Claude Code gets Read, Grep and Glob and no MCP server at all, Codex its
read-only sandbox in a private CODEX_HOME with its apps, plugins, memories, hooks, goals and questions off and none of the
Bridge's tools; READ is as it was; a channel's turn forks the helper's session without touching it."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO, make_context, make_job
from covey.app import collab_channel, tool_limits
from covey.backends.claude import argv as claude_argv
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.codex import appserver, consult, turn
from covey.backends.codex.bridge import CodexBackend
from covey.core import collab, turn_source
from covey.core.harness import (CONSULT, LEAD, PROXY, READ, TOOL_LIMIT, HarnessCapabilities, backend_capabilities)
from covey.core.jobs import find_job_record
from covey.core.state import write_json

BRIDGE_TOOLS = {"mcpServers": {"bridge": {"command": "python3", "args": ["mcp.py"], "env": {"K": "v"}}}}


def asked():
    return turn_source.make(turn_source.ASK, asserted_by="topic", origin={"topic": "t3"}, collab="c" * 24,
                            ask=collab.ask_id("c" * 24, "t5", 1))


class LimitTest(unittest.TestCase):
    """app/tool_limits.py: CONSULT apart from READ, the strictest of several."""

    def access(self, read=False):
        return SimpleNamespace(read_only=lambda chat, sender: read)

    def limit(self, *, source=None, look=False, read=False, proxy=False):
        job = make_job(chat_id="oc_1", sender_id="ou_x")
        job.read_only = look  # What an older release's read-only first turn left on a job: nothing now (T956).
        if source:
            job.turn_source = source
        with mock.patch.object(tool_limits, "proxy_topic", return_value=proxy):
            return tool_limits.limit(self.access(read), SimpleNamespace(), job)

    def test_an_ask_is_held_to_consult_whatever_else_applies(self):
        self.assertEqual(self.limit(source=asked()), CONSULT)
        for kwargs in ({"look": True}, {"read": True}, {"proxy": True}, {"look": True, "read": True}):
            with self.subTest(kwargs):
                self.assertEqual(self.limit(source=asked(), **kwargs), CONSULT)

    def test_without_an_ask_each_limit_is_what_it_was(self):
        # COVEY_COLLAB=0 and every job as before: one limit alone is the one it always was.
        self.assertEqual(self.limit(), "")
        self.assertEqual(self.limit(look=True), "")
        self.assertEqual(self.limit(read=True), READ)
        self.assertEqual(self.limit(proxy=True), PROXY)
        self.assertEqual(self.limit(proxy=True, read=True), PROXY)  # A 代答 topic's own, as before.
        # The integrator's continuation is no read-only turn: as the job says.
        self.assertEqual(self.limit(source=turn_source.make(turn_source.REPLY, asserted_by="bridge", collab="c1")), "")

    def test_several_at_once_take_the_strictest(self):
        self.assertEqual(self.limit(look=True, read=True), READ)

    def test_a_harness_that_cannot_hold_an_ask_does_not_run_it(self):
        ctx = make_context(Path("/x"), options={TOOL_LIMIT: CONSULT})
        for caps in (HarnessCapabilities(), HarnessCapabilities(read_only=True), HarnessCapabilities(collab=True)):
            with self.subTest(caps):
                self.assertIn("还不能做只读咨询", tool_limits.refusal(ctx, caps, "Later"))
        self.assertEqual(tool_limits.refusal(ctx, HarnessCapabilities(read_only=True, collab=True), "Claude"), "")
        self.assertTrue(tool_limits.note(ctx).endswith(tool_limits.CONSULT_NOTE))


class ClaudeConsultTest(unittest.TestCase):
    def argv(self, **options):
        ctx = make_context(Path("/x"), backend_cmd=["claude"], options=options)
        return claude_argv.stream_argv(ClaudeBackend(), ctx)

    @staticmethod
    def value(argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_an_ask_has_read_grep_and_glob_and_no_mcp_server_even_with_the_bridges_tools(self):
        for options in ({}, {"mcp_config": BRIDGE_TOOLS}, {"tools": ("Read", "Bash")},
                        {"mcp_config": BRIDGE_TOOLS, "tools": ("Write",)}):
            with self.subTest(options=sorted(options)):
                argv = self.argv(**{TOOL_LIMIT: CONSULT, **options})
                self.assertEqual(argv.count("--tools"), 1)
                self.assertEqual(self.value(argv, "--tools"), "Read,Grep,Glob")
                self.assertEqual(argv.count("--mcp-config"), 1)
                self.assertEqual(self.value(argv, "--mcp-config"), '{"mcpServers":{}}')
                self.assertIn("--strict-mcp-config", argv)
                self.assertNotIn("--allowedTools", argv)
                self.assertNotIn("--disallowedTools", argv)
                self.assertNotIn("bridge", " ".join(argv))

    def test_its_fork_resumes_the_helpers_session_at_the_entry_and_forks(self):
        argv = self.argv(**{TOOL_LIMIT: CONSULT, "fork_session": "s-helper", "fork_at": "u-keep"})
        self.assertEqual(argv[argv.index("--resume"):argv.index("--resume") + 5],
                         ["--resume", "s-helper", "--resume-session-at", "u-keep", "--fork-session"])

    def test_its_environment_keeps_tool_search_as_any_turn_without_tools(self):
        ctx = make_context(Path("/x"), options={TOOL_LIMIT: CONSULT, "mcp_config": BRIDGE_TOOLS})
        self.assertNotIn("ENABLE_TOOL_SEARCH", claude_argv.turn_env(ctx, {}))

    def test_the_butlers_and_a_leads_turn_without_the_limit_are_as_before(self):
        argv = self.argv(mcp_config=BRIDGE_TOOLS)
        self.assertEqual(self.value(argv, "--allowedTools"), "mcp__bridge")
        self.assertEqual(self.value(self.argv(mcp_config=BRIDGE_TOOLS, **{TOOL_LIMIT: LEAD}), "--tools"), "Read,Grep,Glob")


class CodexConsultTest(unittest.TestCase):
    def ctx(self, **options):
        return make_context(Path("/x"), options=options)

    def test_an_ask_runs_read_only_for_the_thread_and_the_turn_and_never_with_the_bridges_tools(self):
        params, _ = appserver.thread_params(self.ctx(**{TOOL_LIMIT: CONSULT, "mcp_config": BRIDGE_TOOLS}), None)
        self.assertEqual(params["sandbox"], "read-only")
        self.assertFalse([key for key in params["config"] if key.startswith("mcp_servers.bridge.")])
        self.assertTrue(params["config"]["mcp_servers.bridge_workspace.enabled"])  # The read-only workspace reader stays.
        sent = {}
        rpc = SimpleNamespace(thread_id="thread-1", request=lambda method, params, timeout=None: sent.update(params)
                              or {"turn": {"id": "turn-1"}})
        appserver.start_turn(rpc, SimpleNamespace(inputs=SimpleNamespace(attachments=())),
                             self.ctx(**{TOOL_LIMIT: CONSULT}), "hi", None)
        self.assertEqual(sent["sandboxPolicy"], {"type": "readOnly"})
        # The butler's turn without the limit still has its Bridge tools.
        params, _ = appserver.thread_params(self.ctx(mcp_config=BRIDGE_TOOLS), None)
        self.assertTrue(params["config"]["mcp_servers.bridge.enabled"])

    def test_an_asks_app_server_turns_off_what_runs_outside_the_sandbox_or_writes_on_its_own(self):
        flags = appserver.server_flags(self.ctx(**{TOOL_LIMIT: CONSULT}))
        pairs = dict(flags[i + 1].split("=", 1) for i in range(0, len(flags), 2))
        for key in ("features.apps", "features.plugins", "features.remote_plugin", "features.memories",
                    "memories.generate_memories", "memories.use_memories", "features.hooks", "features.goals",
                    "features.image_generation", "features.default_mode_request_user_input"):
            self.assertEqual(pairs.get(key), "false", key)  # codex-cli 0.171.0 takes a key it does not know silently.
        self.assertEqual(pairs.get("web_search"), '"disabled"')
        self.assertEqual(appserver.server_flags(self.ctx()), ["-c", "features.default_mode_request_user_input=true"])  # As before.

    def test_an_asks_request_for_the_owners_input_is_refused(self):
        sent, events = [], []
        rpc = SimpleNamespace(thread_id="th", turn_id="tu", no_questions=True, send=sent.append)
        row = {"id": 7, "method": "item/tool/requestUserInput"}
        turn.answer_server_request(rpc, row, {"threadId": "th", "turnId": "tu"}, row["method"], None,
                                   lambda *args: events.append(args))
        self.assertEqual(events, [])
        self.assertEqual(sent[0]["id"], 7)
        self.assertIn("error", sent[0])

    def test_the_private_home_links_all_but_rules_hooks_plugins_skills_memories_history_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            real, state = Path(tmp) / "real", Path(tmp) / "state"
            real.mkdir()
            for name in ("auth.json", "session_index.jsonl", "state_5.sqlite", "history.jsonl", "hooks.json",
                         "memories_1.sqlite", "AGENTS.md"):
                (real / name).write_text("x")
            for name in ("sessions", "archived_sessions", "rules", "hooks", "plugins", "skills", "memories"):
                (real / name).mkdir()
            (real / "rules" / "default.rules").write_text('prefix_rule(pattern=["touch"], decision="allow")')
            (real / "config.toml").write_text('model = "gpt-x"\nmodel_provider = "acme"\nservice_tier = "flex"\n'
                                              'approval_policy = "never"\n[mcp_servers.owner]\ncommand = "x"\n'
                                              '[model_providers.acme]\nbase_url = "https://acme.example/v1"\n'
                                              'env_key = "ACME_KEY"\nhttp_headers = { "X-A" = "b" }\n'
                                              '[projects."/x"]\ntrust_level = "trusted"\n')
            with consult.ConsultHome(state, {"CODEX_HOME": str(real), "PATH": "/bin"}) as env:
                home = Path(env["CODEX_HOME"])
                self.assertEqual(env["PATH"], "/bin")
                self.assertTrue(str(home).startswith(str(state / "tmp")))
                linked = sorted(p.name for p in home.iterdir() if p.is_symlink())
                self.assertEqual(linked, ["AGENTS.md", "archived_sessions", "auth.json", "session_index.jsonl", "sessions",
                                          "state_5.sqlite"])
                config = (home / "config.toml").read_text()
                self.assertNotIn("mcp_servers", config)
                self.assertNotIn("projects", config)
                self.assertNotIn("approval_policy", config)
                parsed = consult.tomllib.loads(config)
                self.assertEqual((parsed["model"], parsed["model_provider"], parsed["service_tier"]), ("gpt-x", "acme", "flex"))
                self.assertEqual(parsed["model_providers"]["acme"]["http_headers"], {"X-A": "b"})
            self.assertFalse(home.exists())  # Removed with the turn: its links, never what they point at.
            self.assertTrue((real / "rules" / "default.rules").exists() and (real / "sessions").is_dir())

    def test_a_built_in_provider_takes_no_table_and_an_unreadable_config_gives_an_empty_one(self):
        self.assertEqual(consult.minimal_config('model_provider = "openai"\nmodel = "m"\n[model_providers.openai]\nx = 1\n'),
                         'model_provider = "openai"\nmodel = "m"\n')
        self.assertEqual(consult.minimal_config("not = [toml"), "")

    def test_the_backend_runs_an_ask_in_the_private_home_and_any_other_turn_as_before(self):
        seen = []

        def run(request, env):
            seen.append(env.get("CODEX_HOME"))
            return "result"
        backend = CodexBackend()
        with tempfile.TemporaryDirectory() as tmp, mock.patch("covey.backends.codex.bridge.run_appserver", side_effect=run), \
                mock.patch("covey.backends.codex.bridge.child_process_env", return_value={"CODEX_HOME": tmp}):
            state = Path(tmp) / "state"
            for options in ({TOOL_LIMIT: CONSULT}, {}):
                ctx = make_context(Path(tmp), state_dir=state, model="gpt-x", options=options)
                request = SimpleNamespace(context=ctx, check_cancelled=lambda: None)
                self.assertEqual(backend.run(request), "result")
        self.assertTrue(seen[0].startswith(str(state / "tmp" / "codex-consult-")))
        self.assertEqual(seen[1], tmp)



class CodexReadingTest(unittest.TestCase):
    """READ and LEAD turns on Codex (backends/codex/consult.py READING_FLAGS, ReadingHome; the real CLI in
    tests/test_collab_c8.py)."""

    def ctx(self, **options):
        return make_context(Path("/x"), options=options)

    def test_a_read_or_lead_turn_turns_off_apps_plugins_memories_and_hooks_and_keeps_the_question(self):
        for limit in (READ, LEAD):
            flags = appserver.server_flags(self.ctx(**{TOOL_LIMIT: limit}))
            pairs = dict(flags[i + 1].split("=", 1) for i in range(0, len(flags), 2))
            for key in ("features.apps", "features.plugins", "features.remote_plugin", "features.memories",
                        "memories.generate_memories", "memories.use_memories", "features.hooks"):
                self.assertEqual(pairs.get(key), "false", (limit, key))
            self.assertEqual(pairs.get("features.default_mode_request_user_input"), "true")  # Its question, as before.
            self.assertNotIn("web_search", pairs)
        proxy = appserver.server_flags(self.ctx(**{TOOL_LIMIT: PROXY}))  # A 代答 turn: its question, no memories.
        self.assertEqual(proxy[:2], ["-c", "features.default_mode_request_user_input=true"])
        self.assertNotIn("features.apps=false", proxy)  # Its lark-cli and the rest as before (tests/test_native_memory.py).

    def test_the_reading_home_links_all_but_rules_hooks_plugins_and_memories_and_keeps_the_owners_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            real, state = Path(tmp) / "real", Path(tmp) / "state"
            real.mkdir()
            for name in ("auth.json", "session_index.jsonl", "state_5.sqlite", "history.jsonl", "hooks.json",
                         "memories_1.sqlite", "memories.lock", "AGENTS.md", "config.toml"):
                (real / name).write_text("x")
            for name in ("sessions", "rules", "hooks", "plugins", "skills", "memories"):
                (real / name).mkdir()
            with consult.ReadingHome(state, {"CODEX_HOME": str(real), "PATH": "/bin"}) as env:
                home = Path(env["CODEX_HOME"])
                self.assertEqual(env["PATH"], "/bin")
                self.assertTrue(home.name.startswith("codex-read-") and home.parent == state / "tmp")
                self.assertEqual(sorted(p.name for p in home.iterdir()),
                                 ["AGENTS.md", "auth.json", "config.toml", "history.jsonl", "session_index.jsonl",
                                  "sessions", "skills", "state_5.sqlite"])
                self.assertTrue(all(p.is_symlink() for p in home.iterdir()))  # The owner's config.toml too, as it is.
                self.assertEqual((home / "config.toml").resolve(), (real / "config.toml").resolve())
            self.assertFalse(home.exists())
            self.assertTrue((real / "rules").is_dir() and (real / "config.toml").exists())

    def test_only_a_read_turn_resuming_its_thread_runs_in_the_reading_home(self):
        seen = []

        def run(request, env):
            seen.append(env.get("CODEX_HOME"))
            return "result"
        backend = CodexBackend()
        with tempfile.TemporaryDirectory() as tmp, mock.patch("covey.backends.codex.bridge.run_appserver", side_effect=run), \
                mock.patch("covey.backends.codex.bridge.child_process_env", return_value={"CODEX_HOME": tmp}):
            state = Path(tmp) / "state"
            cases = [({TOOL_LIMIT: READ}, "thread-1", False), ({TOOL_LIMIT: READ}, None, False),
                     ({}, None, False), ({}, "thread-1", False),
                     ({TOOL_LIMIT: READ}, "thread-1", True)]
            for options, session, on_device in cases:
                ctx = make_context(Path(tmp), state_dir=state, model="gpt-x", options=options)
                request = SimpleNamespace(context=ctx, check_cancelled=lambda: None, session_id=session)
                with mock.patch("covey.backends.codex.bridge.device_of", return_value="gpu1" if on_device else ""):
                    self.assertEqual(backend.run(request), "result")
        self.assertTrue(seen[0].startswith(str(state / "tmp" / "codex-read-")))  # A group member's turn going on.
        # Starting a thread (READ's first turn), an ordinary turn, a device's: the owner's home.
        self.assertEqual(seen[1:], [tmp] * 4)
        self.assertFalse(list((state / "tmp").glob("codex-read-*")))

class CapabilityTest(unittest.TestCase):
    def test_claude_and_codex_declare_it_on_this_machine_and_not_on_a_device(self):
        for backend in (ClaudeBackend(), CodexBackend()):
            local = backend_capabilities(backend, make_context(Path("/x")))
            self.assertTrue(local.collab and local.read_only, type(backend).__name__)
            remote = backend_capabilities(backend, make_context(Path("/x"), device="gpu1", remote_workdir="/p"))
            self.assertFalse(remote.collab)


class ForkPlanTest(unittest.TestCase):
    """app/collab_channel.py fork_plan: the helper's session as it is now, only read."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.helper, self.channel = "a1" * 16, collab.channel_id("c" * 24, "a1" * 16)
        for topic in (self.helper, self.channel):
            write_json(self.state / "topics" / topic / "topic.json", {"topic_id": topic, "chat_id": "oc_1",
                                                                        "workdir": "/work", "harness_id": "claude"})
        (self.state / "topics" / self.helper / "claude_session_id").write_text("11111111-2222-4333-8444-555555555555")
        self.rewind = mock.Mock(return_value={"keep": "u-before-running"})
        self.harness = SimpleNamespace(spec=SimpleNamespace(session_filename="claude_session_id",
                                                            session_pattern=__import__("re").compile(r"[0-9a-f-]{36}")),
                                       store=SimpleNamespace(transcript=lambda workdir, sid: Path("/t.jsonl")),
                                       backend=SimpleNamespace(rewind_point=self.rewind,
                                                               capabilities=lambda ctx: HarnessCapabilities(native_fork=True, rewind=True)))
        self.ctx = make_context(Path("/work"), state_dir=self.state, topic_id=self.channel,
                                history_path=self.state / "topics" / self.channel / "history.jsonl")

    def plan(self):
        return collab_channel.fork_plan(self.harness, self.ctx, self.helper, "oc_1")

    def test_the_whole_session_when_nothing_runs_and_no_rewind_waits(self):
        self.assertEqual(self.plan(), {"fork_session": "11111111-2222-4333-8444-555555555555"})

    def test_a_waiting_rewind_is_used_and_never_removed(self):
        marker = self.state / "topics" / self.helper / "rewind-next.json"
        write_json(marker, {"token": "t1", "session_id": "11111111-2222-4333-8444-555555555555", "keep": "u-kept"})
        before = marker.read_bytes()
        self.assertEqual(self.plan()["fork_at"], "u-kept")
        self.assertEqual(marker.read_bytes(), before)

    def test_a_turn_of_the_helper_running_meanwhile_is_left_out(self):
        from covey.core.jobs import upsert_job_record

        upsert_job_record(self.state, "om_now", job_id=9, topic_id=self.helper, chat_id="oc_1", status="running",
                          message_id="om_now", prompt_uuid="u-prompt", prompt_session="11111111-2222-4333-8444-555555555555")
        self.assertEqual(self.plan()["fork_at"], "u-before-running")
        # Without a rewind point to fork at (Codex), the helper's record instead.
        self.harness.backend.capabilities = lambda ctx: HarnessCapabilities(native_fork=True)
        self.assertEqual(self.plan(), {})

    def test_no_session_another_directory_or_no_transcript_take_the_record(self):
        (self.state / "topics" / self.helper / "claude_session_id").unlink()
        self.assertEqual(self.plan(), {})

    def test_fork_start_records_what_the_turn_stood_on(self):
        from covey.core.jobs import upsert_job_record

        job = make_job(job_id=3, message_id="collab-x-1", chat_id="oc_1", topic_id=self.channel)
        upsert_job_record(self.state, job.message_id, job_id=3, topic_id=self.channel, chat_id="oc_1", status="running")
        write_json(self.state / "topics" / self.channel / "topic.json",
                   {"topic_id": self.channel, "chat_id": "oc_1", "workdir": "/work",
                    collab_channel.FIELD: {"helper": self.helper, "collab": "c" * 24}})
        ctx, rows = collab_channel.fork_start(self.harness, lambda _: None, self.ctx, job, [])
        self.assertEqual(ctx.options["fork_session"], "11111111-2222-4333-8444-555555555555")
        self.assertEqual(find_job_record(self.state, job.message_id)[collab_channel.EVIDENCE]["level"], collab.FORKED)
        (self.state / "topics" / self.helper / "claude_session_id").unlink()
        ctx, rows = collab_channel.fork_start(self.harness, lambda _: None, self.ctx, job, [])
        self.assertTrue(ctx.options[collab_channel.RECORD_ONLY])
        self.assertEqual(find_job_record(self.state, job.message_id)[collab_channel.EVIDENCE]["level"], collab.RECORD)
        # Any other topic: as it came.
        other = make_context(Path("/work"), state_dir=self.state, topic_id="b2" * 16)
        self.assertEqual(collab_channel.fork_start(self.harness, lambda _: None, other, job, ["h"]), (other, ["h"]))


if __name__ == "__main__":
    unittest.main()


class NoTomlTest(unittest.TestCase):
    """Python 3.10 (requires-python >=3.10) has no tomllib: the Codex backend reads the owner's config.toml with
    core/toml_compat.py (tomli, the reader tomllib was made from), so a consult keeps its model and provider there too.
    CI's 3.10 job was red from 9f648d6 (the import failed) and again from 5ff525e (the config came out empty)."""

    CONFIG = 'model = "m"\nmodel_provider = "acme"\napproval_policy = "never"\n[model_providers.acme]\nbase_url = "https://acme.example/v1"\n'

    def test_without_the_standard_librarys_reader_a_consults_config_reads_the_same(self):
        code = ("import sys\nsys.modules['tomllib'] = None\nfrom covey.backends.codex import consult\n"
                "print(consult.tomllib.__name__)\nprint(consult.minimal_config(sys.argv[1]), end='')\n")
        done = subprocess.run([sys.executable, "-B", "-c", code, self.CONFIG], cwd=str(REPO), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        name, _, config = done.stdout.partition("\n")
        self.assertEqual(name, "covey.core.toml_compat")
        self.assertEqual(config, 'model_provider = "acme"\nmodel = "m"\n\n[model_providers.acme]\n'
                                 'base_url = "https://acme.example/v1"\n')
        self.assertEqual(config, consult.minimal_config(self.CONFIG))
