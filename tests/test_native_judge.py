"""The small model's questions where no Claude Code is enabled (T664): one question to a Harness's own CLI and nothing else
(core/one_shot.py, backends/codex/judge.py, backends/agy/judge.py, core/agy_home.py), and where app/track_judge.py puts
each question (``tools``, ``ask``, ``judge``): the gateway when it is set, Claude Code while it is enabled, else Codex, else
Antigravity. Fake CLIs stand in for codex and agy; nothing here reaches a model."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import exec_tempdir
from support.assist import topics_with

from covey.app import assist, track_judge
from covey.backends.agy import accounts as agy_accounts
from covey.backends.agy import judge as agy_judge
from covey.backends.agy.bridge import READ_ONLY_SETTINGS, AntigravityBackend, error_code
from covey.backends.codex import judge as codex_judge
from covey.backends.codex.bridge import CodexBackend
from covey.core import agy_home, gateways, one_shot
from covey.core.harness import AskFailed, HarnessCapabilities, backend_capabilities, for_device
from covey.core.track_route import LiveJob, Message, Quote

FAKE_KEY = "sk" + "-fake-" + "0123456789" + "abcdefghij"  # Put together here: no key shape in the source.
NOW = 1_800_000_000.0
QUESTION = "顺便把第二个对比表也加上内存占用那一列"

# One fake for both CLIs. It records its argv, working directory, standard input and HOME/CODEX_HOME (and what that home
# holds) to FAKE_RECORD, then prints FAKE_ROWS (JSON lines); FAKE_MODE: "hang" (a child started first, pids in FAKE_PIDS,
# then the rows, then it lingers), "exit" (status 3, FAKE_STDERR on stderr).
FAKE = """#!{python}
import json, os, subprocess, sys, time
stdin = sys.stdin.read()
home = os.environ.get("CODEX_HOME") or os.environ.get("HOME") or ""
listing = sorted(os.listdir(home)) if home and os.path.isdir(home) else []
gemini = os.path.join(home, ".gemini", "antigravity-cli")
linked = sorted(os.listdir(gemini)) if os.path.isdir(gemini) else []
config = open(os.path.join(home, "config.toml")).read() if os.path.isfile(os.path.join(home, "config.toml")) else None
with open(os.environ["FAKE_RECORD"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin, "home": home, "listing": listing,
                        "linked": linked, "config": config}}) + "\\n")
mode = os.environ.get("FAKE_MODE", "")
if mode == "hang":
    child = subprocess.Popen(["sleep", "30"])
    open(os.environ["FAKE_PIDS"], "w").write(f"{{os.getpid()}} {{child.pid}}")
for row in json.loads(os.environ.get("FAKE_ROWS") or "[]"):
    print(json.dumps(row, ensure_ascii=False), flush=True)
if mode == "hang":
    time.sleep(30)
if mode == "exit":
    sys.stderr.write(os.environ.get("FAKE_STDERR", "boom"))
    sys.exit(3)
"""

CODEX_ROWS = [{"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "想一下"}},
              {"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                                                  "text": '{"route":"merge","target":1,"confidence":0.9}'}},
              {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
AGY_ROWS = [{"event": "init", "conversation_id": "c1", "init": {"tools": []}},
            {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS",
                                           "response": '{"route":"new","target":null,"confidence":0.8}\n'}}]


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return True


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = exec_tempdir()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.script = self.root / "fake-cli"
        self.script.write_text(FAKE.format(python=sys.executable))
        self.script.chmod(0o755)
        self.record, self.pids = self.root / "record.jsonl", self.root / "pids"
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("FAKE_", "COVEY_JUDGE", "LARK_BRIDGE_JUDGE", "COVEY_TRACK_JUDGE", "GEMINI_",
                                      "AGY_", "CODEX_HOME"))}
        # No agy-accounts.json of this machine's data home is read (backends/agy/judge.py home_of).
        patch = mock.patch.dict(os.environ, {**env, "HOME": str(self.home), "FAKE_RECORD": str(self.record),
                                             "FAKE_PIDS": str(self.pids),
                                             agy_accounts.ACCOUNTS_CONFIG_ENV: str(self.root / "agy-accounts.json")},
                                clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def rows(self, rows, **env):
        patch = mock.patch.dict(os.environ, {"FAKE_ROWS": json.dumps(rows, ensure_ascii=False), **env})
        patch.start()
        self.addCleanup(patch.stop)

    def recorded(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def ctx(self, name):
        return SimpleNamespace(backend_cmd=[str(self.script)], state_dir=self.state, options={"harness_id": name},
                               device=None)


class OneShotTest(Case):
    def run_lines(self, limit=10.0, done=lambda row: row.get("type") == "turn.completed", **kwargs):
        return one_shot.run_lines([str(self.script), "-"], "问题的正文", env=dict(os.environ), cwd=str(self.root),
                                  limit=limit, done=done, **kwargs)

    def test_the_rows_up_to_the_answer_and_the_question_only_on_standard_input(self):
        self.rows(CODEX_ROWS)
        rows = self.run_lines()
        self.assertEqual([row["type"] for row in rows][-2:], ["item.completed", "turn.completed"])
        record = self.recorded()[0]
        self.assertEqual(record["stdin"], "问题的正文")
        self.assertNotIn("问题的正文", " ".join(record["argv"]))

    def test_past_the_limit_the_whole_group_is_killed(self):
        self.rows([], FAKE_MODE="hang")
        started = time.monotonic()
        with self.assertRaises(AskFailed) as caught:
            self.run_lines(limit=1.5)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(time.monotonic() - started, 8)
        deadline = time.monotonic() + 5
        pids = [int(pid) for pid in self.pids.read_text().split()] if self.pids.exists() else []
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pids)
        self.assertFalse([pid for pid in pids if alive(pid)], "the CLI or its child outlived the limit")

    def test_the_group_is_killed_once_the_answer_is_in(self):
        self.rows(CODEX_ROWS, FAKE_MODE="hang")  # Prints its answer, then lingers with a child, as Codex does.
        started = time.monotonic()
        rows = self.run_lines(limit=20)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(rows[-1]["type"], "turn.completed")
        pids = [int(pid) for pid in self.pids.read_text().split()]  # Written before the answer.
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(pids), 2)
        self.assertFalse([pid for pid in pids if alive(pid)], "the CLI or its child outlived its answer")

    def test_an_error_exit_says_its_code_and_last_stderr_line_with_secrets_hidden_never_the_question(self):
        self.rows([], FAKE_MODE="exit", FAKE_STDERR="first line\nhttps://accounts.example/o/oauth2/x\n"
                                                    "failed: password=hunter-fake-0000\n")
        with self.assertRaises(AskFailed) as caught:
            self.run_lines(skip=("oauth2",))
        self.assertEqual(caught.exception.reason, "exit_nonzero")
        self.assertTrue(caught.exception.detail.startswith("code=3 failed:"), caught.exception.detail)
        self.assertNotIn("hunter-fake", caught.exception.detail)
        self.assertNotIn("问题", caught.exception.detail)

    def test_an_error_exit_is_read_from_its_last_stderr_lines_the_harnesss_own_reading_first(self):
        """T884: a CLI that exits on a lost login, a spent quota or a dropped connection says so on its standard error;
        the reason is that (core/assist.py code_of_text), never only exit_nonzero, and the detail hides paths too."""
        for stderr, code in (("warning: credential helper slow\n" + "x\n" * 6 + "Error: 401 Unauthorized\n",
                              "authentication_error"),
                             ("Error: insufficient_quota for /absolute/owner-home/.codex\n", "quota_exhausted"),
                             ("request failed: failed to connect to 127.0.0.1:8080\n", "transport_error"),
                             ("Error: 529 overloaded\n", "server_fault"),
                             ("API Error: content violates our usage policy\n", "policy_refusal"),
                             ("panic: something odd\n", "exit_nonzero")):
            with self.subTest(code=code):
                self.rows([], FAKE_MODE="exit", FAKE_STDERR=stderr)
                with self.assertRaises(AskFailed) as caught:
                    self.run_lines()
                self.assertEqual(caught.exception.reason, code)
                self.assertTrue(caught.exception.detail.startswith("code=3"), caught.exception.detail)
                self.assertNotIn("/absolute/owner-home", caught.exception.detail)
        self.rows([], FAKE_MODE="exit", FAKE_STDERR="You are not logged into Antigravity.\n")
        with self.assertRaises(AskFailed) as caught:
            self.run_lines(classify=error_code)  # agy's own reading of its words.
        self.assertEqual(caught.exception.reason, "authentication_error")
        self.assertEqual(one_shot.exit_code_of("", None), "exit_nonzero")

    def test_a_cli_that_does_not_start_is_spawn_failed(self):
        with self.assertRaises(AskFailed) as caught:
            one_shot.run_lines([str(self.root / "missing")], "q", env=dict(os.environ), cwd=str(self.root), limit=5,
                               done=lambda row: True)
        self.assertEqual(caught.exception.reason, "spawn_failed")

    def test_output_that_ends_without_the_answer_is_left_to_the_caller(self):
        self.rows([{"type": "thread.started"}])
        self.assertEqual(self.run_lines(), [{"type": "thread.started"}])


class CodexJudgeTest(Case):
    def catalog(self, models):
        codex = self.home / ".codex"
        codex.mkdir(exist_ok=True)
        (codex / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": slug, "visibility": "list", "description": text,
             "supported_reasoning_levels": [{"effort": effort} for effort in efforts]}
            for slug, text, efforts in models]}))

    def test_one_question_is_an_ephemeral_read_only_exec_with_its_words_on_standard_input(self):
        self.rows(CODEX_ROWS)
        text = codex_judge.ask_once(self.ctx("codex"), "只回一行 JSON", QUESTION, limit=10)
        self.assertEqual(json.loads(text), {"route": "merge", "target": 1, "confidence": 0.9})
        record = self.recorded()[0]
        argv = record["argv"]
        self.assertEqual(argv[:2], ["exec", "--ephemeral"])  # No session written.
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "-")
        self.assertEqual(record["stdin"], QUESTION)
        self.assertNotIn(QUESTION, " ".join(argv))
        self.assertIn("developer_instructions=" + json.dumps("只回一行 JSON"), argv)
        flags = {argv[i + 1] for i, part in enumerate(argv) if part == "-c"}
        for flag in ("features.shell_tool=false", "features.multi_agent=false", "features.apps=false",
                     "features.plugins=false", "features.hooks=false", "features.memories=false", 'web_search="disabled"'):
            self.assertIn(flag, flags)
        self.assertEqual(Path(record["cwd"]), self.state / "codex-judge")
        self.assertTrue(record["home"].startswith(str(self.state / "tmp" / "codex-judge-")))
        self.assertFalse(Path(record["home"]).exists())  # Its own CODEX_HOME went with the question.

    def test_its_home_has_the_login_but_no_mcp_server_rule_hook_memory_or_global_instructions(self):
        real = self.home / ".codex"
        real.mkdir()
        for name in ("auth.json", "AGENTS.md", "history.jsonl", "hooks.json", "memories_1.sqlite"):
            (real / name).write_text("x")
        for name in ("rules", "skills", "plugins", "sessions"):
            (real / name).mkdir()
        (real / "config.toml").write_text('model = "gpt-x"\n[mcp_servers.owner]\ncommand = "owner-server"\n')
        self.rows(CODEX_ROWS)
        codex_judge.ask_once(self.ctx("codex"), "s", "{}", limit=10)
        record = self.recorded()[0]
        self.assertIn("auth.json", record["listing"])
        for name in ("AGENTS.md", "history.jsonl", "hooks.json", "memories_1.sqlite", "rules", "skills", "plugins"):
            self.assertNotIn(name, record["listing"])
        from covey.backends.codex import consult
        if consult.tomllib is not None:
            self.assertNotIn("mcp_servers", record["config"] or "")
            self.assertIn('model = "gpt-x"', record["config"])

    def test_the_answer_is_the_last_agent_message_and_a_failed_turn_or_none_is_a_reason(self):
        self.assertEqual(codex_judge.answer_of(CODEX_ROWS), '{"route":"merge","target":1,"confidence":0.9}')
        with self.assertRaises(AskFailed) as caught:
            codex_judge.answer_of([{"type": "turn.failed", "error": {"code": "rate_limited", "message": "x"}}])
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("rate_limited", "x"))
        with self.assertRaises(AskFailed) as caught:
            codex_judge.answer_of([{"type": "turn.failed", "error": {"message": "something odd"}}])
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("model_error", "something odd"))
        with self.assertRaises(AskFailed) as caught:
            codex_judge.answer_of([{"type": "turn.completed"}])
        self.assertEqual(caught.exception.reason, "bad_output")

    def test_the_model_the_settings_one_else_the_variable_else_the_owners_default(self):
        """T924: no light model and no ``medium`` put in: what the setting names, else COVEY_JUDGE_CODEX_MODEL, else Codex's
        own model and budget."""
        self.catalog([("gpt-big", "Our most capable model.", ("low", "medium", "high")),
                      ("gpt-luna", "Fast and affordable model for easier tasks.", ("low", "medium")),
                      ("gpt-gemini-like", "A model.", ("minimal", "low"))])
        env = {"HOME": str(self.home)}
        self.assertEqual(codex_judge.choose(env, environ={}), ("", ""))
        self.assertEqual(codex_judge.choose(env, environ={codex_judge.MODEL_ENV: "gpt-big"}), ("gpt-big", ""))
        self.assertEqual(codex_judge.choose(env, environ={codex_judge.MODEL_ENV: "not-listed"}),
                         ("", ""))  # A model the account does not list is not asked for.
        self.assertEqual(codex_judge.choose(env, environ={codex_judge.MODEL_ENV: "gpt-big"}, follow="gpt-luna", effort="low"),
                         ("gpt-luna", "low"))  # The setting's model comes first.

    def test_real_failures_are_read_as_what_they_are_with_their_words_and_no_secret(self):
        """Replayed failures (codex-cli 0.171.0's errors, with and without codexErrorInfo; T872's words): each a reason
        core/assist.py tells apart (a break, a passing one, a refusal), never the old model_error for all."""
        cases = (
            ({"message": "Quota exceeded. Check your plan and billing details.",
              "codexErrorInfo": {"usageLimitExceeded": {}}}, "quota_exhausted"),
            ({"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}, "rate_limited"),
            ({"message": "You've hit your usage limit. Upgrade to Pro, or try again at 3:05 PM."}, "rate_limited"),
            ({"message": "unexpected status 401 Unauthorized", "codexErrorInfo": "unauthorized"}, "authentication_error"),
            ({"message": "x", "codexErrorInfo": {"other": {"httpStatusCode": 401}}}, "authentication_error"),
            ({"message": "This content was flagged", "codexErrorInfo": "cyberPolicy"}, "policy_refusal"),
            ({"message": "Invalid prompt: your prompt was flagged as potentially violating our usage policy."},
             "policy_refusal"),
            ({"message": "The model gpt-x does not exist", "codexErrorInfo": "modelNotFound"}, "model_unavailable"),
            ({"message": "stream disconnected before completion",
              "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}}}, "server_fault"),
            ({"message": "stream disconnected", "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 400}}},
             "transport_error"),
            ({"message": "workspace routing discovery timed out"}, "transport_error"),
            ({"message": "error sending request: failed to connect to the gateway"}, "transport_error"),
        )
        for error, code in cases:
            for row in ({"type": "turn.failed", "error": error}, {"type": "error", **error}):
                with self.subTest(row=row):
                    with self.assertRaises(AskFailed) as caught:
                        codex_judge.answer_of([{"type": "turn.started"}, row])
                    self.assertEqual(caught.exception.reason, code)
                    self.assertTrue(caught.exception.detail)
        with self.assertRaises(AskFailed) as caught:
            codex_judge.answer_of([{"type": "turn.failed", "error": {
                "message": "401 Unauthorized: key " + FAKE_KEY + " at /absolute/owner-home/.codex/auth.json"}}])
        self.assertNotIn(FAKE_KEY[:12], caught.exception.detail)
        self.assertNotIn("/absolute/owner-home", caught.exception.detail)

    def test_a_reconnect_notice_is_not_the_end_of_the_question(self):
        notice = {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected before completion)"}
        self.assertFalse(codex_judge.finished(notice))
        self.assertTrue(codex_judge.finished({"type": "error", "message": "unexpected status 401"}))
        self.assertEqual(codex_judge.answer_of([CODEX_ROWS[0], notice, *CODEX_ROWS[1:]]),
                         '{"route":"merge","target":1,"confidence":0.9}')
        self.rows([CODEX_ROWS[0], notice, *CODEX_ROWS[1:]])
        self.assertEqual(json.loads(codex_judge.ask_once(self.ctx("codex"), "s", "{}", limit=10))["route"], "merge")

    def test_a_question_that_follows_a_gateway_model_goes_to_that_gateway(self):
        """T884: a setting followed whose model is a model gateway's (its prefix): the question goes there, a light one
        on the gateway's light model, through a relay of its own with the gateway's provider; one of the account's own is
        the question's model too (T924)."""
        self.catalog([("gpt-big", "Our most capable model.", ("low", "medium")),
                      ("gpt-luna", "Fast and affordable model for easier tasks.", ("low", "medium"))])
        gateway = gateways.Gateway(name="acme", display="Acme", base_url="https://gateway.invalid", key_env="ACME_KEY",
                                   prefix="acme/", harnesses=("codex",))
        rows = {"acme/astra": {"display_name": "Astra (Acme)", "supported_reasoning_levels": [{"effort": "medium"}]},
                "acme/astra-mini": {"display_name": "Astra Mini (Acme)",
                                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]}}
        with mock.patch.object(gateways, "all_gateways", return_value=(gateway,)), \
                mock.patch.object(codex_judge.gateway_models, "cached", return_value=rows):
            env, ctx = {"HOME": str(self.home)}, self.ctx("codex")
            self.assertEqual(codex_judge.choose(env, environ={}, follow="acme/astra", ctx=ctx), ("acme/astra", ""))
            self.assertEqual(codex_judge.choose(env, environ={}, follow="acme/astra", effort="medium", ctx=ctx),
                             ("acme/astra", "medium"))
            self.assertEqual(codex_judge.choose(env, environ={}, follow="gpt-big", ctx=ctx), ("gpt-big", ""))
            self.assertEqual(codex_judge.model_of(ctx, follow="acme/astra", environ={}), "acme/astra")
            self.rows(CODEX_ROWS)
            with mock.patch.dict(os.environ, {"ACME_KEY": "fake-key-for-tests"}):
                codex_judge.ask_once(ctx, "s", "{}", limit=10, follow="acme/astra", environ={})
            argv = self.recorded()[-1]["argv"]
            self.assertEqual(argv[argv.index("-m") + 1], "astra")
            configs = [argv[i + 1] for i, part in enumerate(argv) if part == "-c"]
            self.assertIn('model_provider="acme"', configs)
            base = next(c for c in configs if c.startswith("model_providers.acme.base_url="))
            self.assertTrue(json.loads(base.partition("=")[2]).startswith("http://127.0.0.1:"))  # Its own relay.
            self.assertLess(argv.index('model_provider="acme"'), argv.index("exec"))
            with self.assertRaises(AskFailed) as caught:  # No key: the gateway cannot be asked, nothing starts.
                codex_judge.ask_once(ctx, "s", "{}", limit=10, follow="acme/astra", environ={})
            self.assertEqual(caught.exception.reason, "authentication_error")
            self.assertEqual(len(self.recorded()), 1)

    def test_a_question_runs_on_the_settings_model_at_its_budget(self):
        """T924: 自动（跟随管家） means the followed setting's model and budget, Codex's own budget only where that model
        lacks it or another model runs it."""
        self.catalog([("gpt-big", "Our most capable model.", ("low", "medium", "high", "xhigh")),
                      ("gpt-luna", "Fast and affordable model for easier tasks.", ("low", "medium"))])
        env = {"HOME": str(self.home)}
        self.assertEqual(codex_judge.choose(env, environ={}, follow="gpt-big", effort="xhigh"), ("gpt-big", "xhigh"))
        self.assertEqual(codex_judge.choose(env, environ={}, follow="gpt-big", effort="max"), ("gpt-big", ""))
        self.assertEqual(codex_judge.choose(env, environ={}, follow="gone", effort="xhigh"), ("", ""))
        self.rows(CODEX_ROWS)
        codex_judge.ask_once(self.ctx("codex"), "s", "{}", limit=10, follow="gpt-big", effort="xhigh", environ={})
        argv = self.recorded()[-1]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-big")
        self.assertIn('model_reasoning_effort="xhigh"', argv)

    def test_the_model_a_question_runs_on_is_said_without_starting_anything(self):
        self.catalog([("gpt-big", "Our most capable model.", ("low", "medium")),
                      ("gpt-luna", "Fast and affordable model for easier tasks.", ("low",))])
        ctx = self.ctx("codex")
        self.assertEqual(codex_judge.model_of(ctx, environ={}), "")  # config.toml names none.
        self.assertEqual(codex_judge.model_of(ctx, follow="gpt-luna", environ={}), "gpt-luna")
        (self.home / ".codex" / "config.toml").write_text('model = "gpt-big"\n')
        self.assertEqual(codex_judge.model_of(ctx, environ={}), "gpt-big")
        self.assertEqual(CodexBackend().ask_model(ctx), "gpt-big")
        with mock.patch.dict(os.environ, {codex_judge.MODEL_ENV: "gpt-big"}):
            self.assertEqual(CodexBackend().ask_model(ctx), "gpt-big")
        self.assertEqual(self.recorded(), [])

    def test_a_turn_that_never_completes_is_a_timeout(self):
        self.rows(CODEX_ROWS[:2], FAKE_MODE="hang")
        with self.assertRaises(AskFailed) as caught:
            codex_judge.ask_once(self.ctx("codex"), "s", "{}", limit=1.5)
        self.assertEqual(caught.exception.reason, "timeout")


class AgyJudgeTest(Case):
    def sign_in(self):
        folder = self.home / ".gemini" / "antigravity-cli"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / agy_home.TOKEN).write_text("fake-token")
        for name in ("conversations", "brain", "log"):
            (folder / name).mkdir(exist_ok=True)
        (folder / "bin").mkdir(exist_ok=True)
        config = self.home / ".gemini" / "config"
        config.mkdir(exist_ok=True)
        (config / "mcp_config.json").write_text(json.dumps({"mcpServers": {"owner": {"command": "x"}}}))

    def ask(self, limit=10, follow="gemini-3.8-flash", effort="low"):
        backend = SimpleNamespace(model_catalog=lambda ctx: {
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "supported_reasoning_levels": [{"effort": "low"}]},
            "gemini-3.8-flash": {"display_name": "Gemini 3.8 Flash",
                                 "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]}})
        return agy_judge.ask_once(backend, self.ctx("agy"), "只回一行 JSON", QUESTION, limit=limit,
                                  settings=READ_ONLY_SETTINGS, follow=follow, effort=effort)

    def test_without_a_sign_in_nothing_starts(self):
        with self.assertRaises(AskFailed) as caught:
            self.ask()
        self.assertEqual(caught.exception.reason, "not_signed_in")
        self.assertEqual(self.recorded(), [])

    def test_one_question_in_a_scratch_home_with_the_deny_rules_and_its_words_on_standard_input(self):
        self.sign_in()
        self.rows(AGY_ROWS)
        self.assertEqual(json.loads(self.ask()), {"route": "new", "target": None, "confidence": 0.8})
        record = self.recorded()[0]
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertIn("--disable-slash-commands", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)  # A step that asks is skipped.
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), READ_ONLY_SETTINGS)
        self.assertEqual(argv[argv.index("--model") + 1:argv.index("--model") + 4], ["gemini-3.8-flash", "--effort", "low"])
        self.assertNotIn(QUESTION, " ".join(argv))
        line = json.loads(record["stdin"])
        self.assertEqual(line["event"], "user")
        self.assertTrue(line["message"]["content"].startswith("只回一行 JSON\n\n"))
        self.assertIn(QUESTION, line["message"]["content"])
        home = Path(record["home"])
        self.assertEqual(home.parent, self.state / "tmp")
        self.assertFalse(home.exists())  # The scratch home, the conversation in it, went with the question.
        self.assertIn(agy_home.TOKEN, record["linked"])
        for name in ("conversations", "brain", "log"):
            self.assertNotIn(name, record["linked"])
        self.assertEqual(record["listing"], [".gemini"])  # No config/: none of the owner's MCP servers or rules.
        self.assertTrue((self.home / ".gemini" / "config" / "mcp_config.json").exists())  # The owner's untouched.

    def test_no_model_set_runs_on_agys_own_default_and_the_variable_names_one(self):
        self.sign_in()
        self.rows(AGY_ROWS)
        self.ask(follow="", effort="")
        self.assertNotIn("--model", self.recorded()[-1]["argv"])
        with mock.patch.dict(os.environ, {agy_judge.MODEL_ENV: "gemini-4-neon-low"}):
            self.ask(follow="", effort="")
        argv = self.recorded()[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-neon-low")
        self.assertNotIn("--effort", argv)
        with mock.patch.dict(os.environ, {agy_judge.MODEL_ENV: "gemini-4-neon-low"}):
            self.ask()  # A model the setting names comes first.
        argv = self.recorded()[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1:argv.index("--model") + 4], ["gemini-3.8-flash", "--effort", "low"])

    def test_every_question_runs_on_the_setting_s_model_when_the_catalog_lists_it(self):
        """T924: no light model picked for some questions: 分流 and 记忆自己长 run on the setting's model as 整理专项 does."""
        catalog = {"gemini-4-neon": {}, "gemini-3.8-flash": {}}
        self.assertEqual(agy_judge.choose(catalog, environ={}, follow="gemini-4-neon"), ("gemini-4-neon", ""))
        self.assertEqual(agy_judge.choose(catalog, environ={}, follow="gone-model"), ("", ""))
        self.assertEqual(agy_judge.choose(catalog, environ={}), ("", ""))  # Not the flash one: agy's own default.

    def test_a_question_takes_the_settings_budget_when_its_model_has_it(self):
        """T924: the butler's gemini-4-neon at xhigh is asked at xhigh; a budget the catalog says the model lacks, or a
        model listed without budgets, takes none (agy refuses those)."""
        catalog = {"gemini-4-neon": {"supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "xhigh"}]},
                   "gemini-pro-orchestrator": {"supported_reasoning_levels": []},
                   "gemini-3.8-flash": {"supported_reasoning_levels": [{"effort": "low"}]}}
        choose = agy_judge.choose
        self.assertEqual(choose(catalog, environ={}, follow="gemini-4-neon", effort="xhigh"), ("gemini-4-neon", "xhigh"))
        self.assertEqual(choose(catalog, environ={}, follow="gemini-4-neon", effort="max"), ("gemini-4-neon", ""))
        self.assertEqual(choose(catalog, environ={}, follow="gemini-pro-orchestrator", effort="high"),
                         ("gemini-pro-orchestrator", ""))
        self.assertEqual(choose({}, environ={}, follow="gemini-4-neon", effort="xhigh"), ("gemini-4-neon", "xhigh"))
        self.sign_in()
        self.rows(AGY_ROWS)
        backend = SimpleNamespace(model_catalog=lambda ctx: catalog)
        agy_judge.ask_once(backend, self.ctx("agy"), "只回一行 JSON", QUESTION, limit=10, settings=READ_ONLY_SETTINGS,
                           follow="gemini-4-neon", effort="xhigh")
        argv = self.recorded()[-1]["argv"]
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]), ("gemini-4-neon", "xhigh"))

    def test_an_error_result_or_no_result_is_a_reason(self):
        self.assertEqual(agy_judge.answer_of(AGY_ROWS), '{"route":"new","target":null,"confidence":0.8}\n')
        with self.assertRaises(AskFailed) as caught:
            agy_judge.answer_of([{"event": "result", "result": {"status": "ERROR", "error": "quota"}}], error_code)
        self.assertEqual(caught.exception.reason, "quota_exhausted")
        with self.assertRaises(AskFailed) as caught:
            agy_judge.answer_of([{"event": "result", "result": {"status": "ERROR", "error": "something odd"}}])
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("model_error", "something odd"))
        with self.assertRaises(AskFailed) as caught:
            agy_judge.answer_of([{"event": "init"}])
        self.assertEqual(caught.exception.reason, "bad_output")

    def test_real_error_results_are_read_as_what_they_are(self):
        """Replayed agy 1.5.x errors (T872's words): a login, a spent quota, a quota that resets by itself, a safety
        block, a model the account cannot use, a passing fault; each detail the error's words, secrets hidden."""
        cases = (("You are not logged into Antigravity.", "authentication_error"),
                 ("You have exhausted your quota on this model.", "quota_exhausted"),
                 ("Your quota will reset after 4h12m. You have exhausted your capacity on this model.", "rate_limited"),
                 ("INTERNAL (code 500): response blocked by SAFETY", "policy_refusal"),
                 ('invalid model selection (--model "gemini-4-neon" --effort "xhigh"): --effort is not supported',
                  "model_unavailable"),
                 ("UNAVAILABLE (code 503): The model is overloaded.", "server_fault"))
        for error, code in cases:
            with self.subTest(error=error):
                with self.assertRaises(AskFailed) as caught:
                    agy_judge.answer_of([{"event": "result", "result": {"status": "ERROR", "error": error}}], error_code)
                self.assertEqual(caught.exception.reason, code)
                self.assertEqual(caught.exception.detail[:12], error[:12])
        self.sign_in()
        self.rows([{"event": "result", "result": {"status": "ERROR", "error": "key " + FAKE_KEY + ": "
                                                                            "401 Unauthorized"}}])
        with self.assertRaises(AskFailed) as caught:
            AntigravityBackend().ask_once(self.ctx("agy"), "s", "{}", limit=10)
        self.assertEqual(caught.exception.reason, "authentication_error")  # The Harness's reading (error_code).
        self.assertNotIn("fake-0123456789", caught.exception.detail)

    def accounts(self, rows):
        (self.root / "agy-accounts.json").write_text(json.dumps({"accounts": rows}))

    def account_dir(self, name, signed=True):
        folder = self.root / "accounts" / name / ".gemini" / "antigravity-cli"
        folder.mkdir(parents=True)
        if signed:
            (folder / agy_home.TOKEN).write_text("fake-token-" + name)
        return folder.parent.parent

    def test_with_several_accounts_the_one_a_turn_would_take_answers_and_none_is_asked_more(self):
        """T884: agy-accounts.json lists two: the question runs with the group's active account's login (its own
        HOME's token linked), read from the records alone; nothing is written to them and no spare is probed."""
        self.sign_in()
        other = self.account_dir("b")
        self.accounts([{"name": "主号"}, {"name": "副号", "home": str(other)}])
        agy_accounts.Records(self.state).change(lambda state: state["active"].update(gemini="副号"))
        before = (self.state / agy_accounts.FOLDER / "state.json").read_text()
        self.rows(AGY_ROWS)
        self.ask()
        record = self.recorded()[-1]
        token = Path(record["home"]) / ".gemini" / "antigravity-cli" / agy_home.TOKEN
        self.assertIn(agy_home.TOKEN, record["linked"])
        self.assertEqual(len(self.recorded()), 1)  # One process: the question; no /usage probe of anyone.
        self.assertFalse(token.exists())  # The scratch home went with it.
        self.assertEqual((self.state / agy_accounts.FOLDER / "state.json").read_text(), before)
        home = agy_judge.home_of(self.ctx("agy"), {"HOME": str(self.home)}, "gemini-3.8-flash")
        self.assertEqual(home["HOME"], str(other))

    def test_every_account_held_out_is_quota_exhausted_and_nothing_starts(self):
        self.sign_in()
        other = self.account_dir("b")
        self.accounts([{"name": "主号"}, {"name": "副号", "home": str(other)}])
        until = time.time() + 3600

        def spent(state):
            state["spent"].update({"主号": {"gemini": until}, "副号": {"gemini": until}})
        agy_accounts.Records(self.state).change(spent)
        self.rows(AGY_ROWS)
        with self.assertRaises(AskFailed) as caught:
            self.ask()
        self.assertEqual(caught.exception.reason, "quota_exhausted")
        self.assertIn("Gemini", caught.exception.detail)
        self.assertEqual(self.recorded(), [])
        agy_accounts.Records(self.state).change(lambda state: state.update(only="副号", spent={"副号": {"gemini": until}}))
        with self.assertRaises(AskFailed) as caught:  # 只用这个号, and it is spent: no other is taken.
            self.ask()
        self.assertEqual(caught.exception.reason, "quota_exhausted")
        self.assertEqual(self.recorded(), [])

    def test_with_one_login_a_held_quota_is_quota_exhausted_before_anything_starts(self):
        self.sign_in()
        agy_accounts.hold(self.state, "gemini-3.8-flash", time.time() + 7200)
        self.rows(AGY_ROWS)
        with self.assertRaises(AskFailed) as caught:
            self.ask()
        self.assertEqual(caught.exception.reason, "quota_exhausted")
        with self.assertRaises(AskFailed):  # agy's own default is Gemini's group too (accounts.group_of).
            self.ask(follow="", effort="")
        self.assertEqual(self.recorded(), [])

    def test_the_model_a_question_runs_on_is_said_without_starting_anything(self):
        backend = SimpleNamespace(model_catalog=lambda ctx: {"gemini-4-neon": {}, "gemini-3.8-flash": {}})
        self.assertEqual(agy_judge.model_of(backend, self.ctx("agy")), "")
        self.assertEqual(agy_judge.model_of(backend, self.ctx("agy"), follow="gemini-4-neon"), "gemini-4-neon")
        self.assertEqual(self.recorded(), [])

    def test_signed_in_by_the_token_or_an_api_key(self):
        self.assertFalse(agy_home.signed_in({"HOME": str(self.home)}))
        self.assertTrue(agy_home.signed_in({"HOME": str(self.home), "GEMINI_API_KEY": "fake"}))
        self.sign_in()
        self.assertTrue(agy_home.signed_in({"HOME": str(self.home)}))


class CapabilityTest(unittest.TestCase):
    def test_codex_and_agy_answer_one_question_on_this_machine_only(self):
        ctx = SimpleNamespace(device=None, options={})
        for backend in (CodexBackend(), AntigravityBackend()):
            with self.subTest(backend=type(backend).__name__):
                capabilities = backend_capabilities(backend, ctx)
                self.assertTrue(capabilities.ask_once and capabilities.bridge_tools)
        self.assertFalse(for_device(HarnessCapabilities(ask_once=True), SimpleNamespace(device="box")).ask_once)


def pick(app):
    """(backend, context) of ``app``: what app/assist.py hands app/track_judge.py."""
    return app.harness.backend, app.harness.context


class RoutingTest(unittest.TestCase):
    """app/track_judge.py: where a question goes and how its answer comes back."""

    @staticmethod
    def app(name, *, ask_once=False, answer='{"route":"merge","target":1,"confidence":0.9}'):
        calls = []

        def ask(ctx, system, data, *, limit, cwd=None, **kw):
            calls.append({"system": system, "data": data, "limit": limit, "cwd": cwd, **kw})
            if isinstance(answer, AskFailed):
                raise answer
            return answer
        backend = SimpleNamespace(capabilities=lambda ctx: HarnessCapabilities(ask_once=ask_once), ask_once=ask,
                                  calls=calls)
        context = SimpleNamespace(backend_cmd=[name], state_dir=Path("/absolute/state"), options={"harness_id": name},
                                  device=None)
        return SimpleNamespace(harness=SimpleNamespace(backend=backend, context=context), backend=backend)

    def setUp(self):
        patch = mock.patch.dict(os.environ, {})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("COVEY_TRACK_JUDGE", "COVEY_TRACK_JUDGE_MS"):
            os.environ.pop(name, None)

    def test_the_assist_route_follows_the_default_and_never_one_that_cannot_answer_alone(self):
        claude, codex, agy, dsh = self.app("claude"), self.app("codex", ask_once=True), self.app("agy", ask_once=True), \
            self.app("dsh")
        with tempfile.TemporaryDirectory() as tmp:
            for apps, want in (({"claude": claude, "codex": codex, "agy": agy}, "claude"),
                               ({"codex": codex, "agy": agy}, "codex"), ({"agy": agy, "dsh": dsh}, "agy")):
                with self.subTest(apps=sorted(apps)), mock.patch.object(assist, "ready", return_value=""):
                    self.assertEqual(assist.route(topics_with(Path(tmp), apps)).harness, want)
            self.assertEqual(assist.route(topics_with(Path(tmp), {"dsh": dsh})).why, "not_one_shot")
            self.assertEqual(assist.route(topics_with(Path(tmp), {})).why, "no_backend")

    def test_a_native_harness_gets_the_question_and_its_answer_comes_back_as_an_envelope(self):
        codex = self.app("codex", ask_once=True)
        backend, ctx = pick(codex)
        raw = track_judge.ask("系统", "{}", backend=backend, ctx=ctx, limit=30, cwd="/absolute/state/judge", environ={})
        self.assertEqual(track_judge.answer_text(raw, "claude"), '{"route":"merge","target":1,"confidence":0.9}')
        self.assertEqual(backend.calls[-1], {"system": "系统", "data": "{}", "limit": 30, "cwd": "/absolute/state/judge"})
        track_judge.ask("系统", "{}", backend=backend, ctx=ctx, limit=30, cwd="/c", follow="gpt-big", effort="high",
                        environ={})
        self.assertEqual((backend.calls[-1]["follow"], backend.calls[-1]["effort"]), ("gpt-big", "high"))
        failing = self.app("agy", ask_once=True, answer=AskFailed("not_signed_in"))
        backend, ctx = pick(failing)
        with self.assertRaises(track_judge.Failed) as caught:
            track_judge.ask("系统", "{}", backend=backend, ctx=ctx, limit=30, cwd="/c", environ={})
        self.assertEqual(caught.exception.reason, "not_signed_in")
        with self.assertRaises(track_judge.Failed) as caught:
            track_judge.ask("系统", "{}", backend=None, ctx=None, limit=30, cwd="/c", environ={})
        self.assertEqual(caught.exception.reason, "no_backend")

    def test_a_retired_judge_gateway_setting_is_not_read(self):
        codex = self.app("codex", ask_once=True)
        backend, ctx = pick(codex)
        track_judge.ask("s", "{}", backend=backend, ctx=ctx, limit=5, cwd="/c", environ={"COVEY_JUDGE_GATEWAY": "gw:model"})
        self.assertEqual(len(backend.calls), 1)  # Codex answered: COVEY_JUDGE_GATEWAY is gone (T813).

    def test_the_router_asks_codex_with_its_own_limit_and_logs_where(self):
        codex = self.app("codex", ask_once=True)
        backend, ctx = pick(codex)
        live = (LiveJob(1, "j1", "running", "整理对比表", NOW - 300, NOW - 200),)
        lines = []
        verdict = track_judge.judge(Message(QUESTION, live, Quote(), now=NOW), backend=backend, ctx=ctx,
                                    log=lines.append, environ={})
        self.assertEqual((verdict.route, verdict.target), ("merge", 1))
        self.assertEqual(backend.calls[-1]["limit"], track_judge.NATIVE_MS / 1000)
        self.assertEqual(Path(backend.calls[-1]["cwd"]), Path("/absolute/state/track-judge"))
        self.assertTrue(lines[-1].startswith("track judge route=merge target=1") and lines[-1].endswith("via=codex"))
        self.assertNotIn(QUESTION, "\n".join(lines))

    def test_a_native_failure_is_new_with_its_reason(self):
        codex = self.app("codex", ask_once=True, answer=AskFailed("timeout"))
        backend, ctx = pick(codex)
        live = (LiveJob(1, "j1", "running", "整理对比表", NOW - 300, NOW - 200),)
        lines = []
        verdict = track_judge.judge(Message(QUESTION, live, Quote(), now=NOW), backend=backend, ctx=ctx,
                                    log=lines.append, environ={})
        self.assertEqual((verdict.route, verdict.why), ("new", "timeout"))
        self.assertIn("track judge failed reason=timeout", lines[-1])
        self.assertTrue(lines[-1].endswith("via=codex"))

    def test_the_native_limit_is_its_own_unless_the_variable_says(self):
        self.assertEqual(track_judge.limit_ms({}, native=True), track_judge.NATIVE_MS)
        self.assertEqual(track_judge.limit_ms({}), track_judge.DEFAULT_MS)
        self.assertEqual(track_judge.limit_ms({"COVEY_TRACK_JUDGE_MS": "6000"}, native=True), 6000)

    def test_the_native_limit_leaves_room_for_antigravity_and_the_variable_may_go_past_ten_seconds(self):
        """T924: gemini-4-neon at low answered 分流 in 9.8 to 16.6 s, and the old 10 s cut five of eight off (each a new track);
        Claude Code's one-shot keeps its 6 s."""
        self.assertEqual(track_judge.limit_ms({}, native=True), 20000)
        self.assertEqual(track_judge.limit_ms({}), 6000)
        self.assertEqual(track_judge.limit_ms({"COVEY_TRACK_JUDGE_MS": "25000"}, native=True), 25000)
        self.assertEqual(track_judge.limit_ms({"COVEY_TRACK_JUDGE_MS": "999999"}, native=True), 30000)


if __name__ == "__main__":
    unittest.main()
