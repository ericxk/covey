"""Several Google accounts for Antigravity (backends/agy/account_homes.py, backends/agy/accounts.py; T674, moved into the
core by T831): each account a HOME of its own that shares the conversations, the turn starting on its group's active
account and going on in the same conversation on the next one when /usage shows its group spent; an account signed out
held out until it signs in again, one /usage says nothing of not taken for signed out, the switch under a lock, the
page's snapshots made from the state alone."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from covey.backends.agy.bridge import AntigravityBackend
from covey.core import extensions
from covey.core.harness import RunCallbacks, RunInputs, RunRequest
from covey.backends.agy import account_homes as agy_homes
from covey.backends.agy import accounts as agy_accounts
from covey.core import quota
from support import FIXTURES, make_context

RUN = "33333333-3333-4333-8333-333333333333"
SID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"
# agy as the hook meets it: /usage answers by the account (its antigravity-cli directory, links followed), any other
# run is the core's scripted stream, failed with FAKE_AGY_SPENT for the accounts FAKE_SPENT names.
WRAPPER = '''import json, os, runpy, sys
args = sys.argv[1:]
cli = os.path.realpath(os.path.join(os.environ.get("HOME", ""), ".gemini", "antigravity-cli"))
if args == ["-p", "/usage", "--output-format", "json"]:
    with open(os.environ["FAKE_PROBES"], "a") as probes:
        probes.write(cli + "\\n")
    usage = json.loads(os.environ.get("FAKE_USAGE", "{}")).get(cli)
    if usage is None:
        print("error: Eligibility check failed", file=sys.stderr); sys.exit(1)
    print(json.dumps(usage)); sys.exit(0)
spent = json.loads(os.environ.get("FAKE_SPENT", "{}")).get(cli)
if spent:
    os.environ["FAKE_AGY_SPENT"] = spent
sys.argv = [os.environ["FAKE_AGY_STREAM"], *args]
runpy.run_path(os.environ["FAKE_AGY_STREAM"], run_name="__main__")
'''
GEMINI = ("Models within this group: Gemini 4 Neon, Gemini 4 Carbon, Orchestrator (Carbon), Gemini 3.8 Flash")
THIRD = "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B"


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def usage(gemini_5h=1.0, gemini_week=1.0, third_5h=1.0, reset_5h=None, reset_week=None):
    """``agy -p /usage --output-format json``'s output as agy 1.5.14 printed it (2026-11-23)."""
    now = time.time()
    reset_5h, reset_week = reset_5h or now + 3 * 3600, reset_week or now + 5 * 86400

    def bucket(key, window, left, reset):
        return {"id": f"{key}-{window}", "name": "Five Hour Limit Remaining" if window == "5h" else
                "Weekly Limit Remaining", "window": window, "remaining_fraction": left, "reset_time": iso(reset)}
    return {"conversation_id": "", "status": "SUCCESS", "response": "Gemini Models\t...", "command": {"name": "usage",
            "data": {"description": "Within each group, models share a weekly limit and a 5-hour limit.", "groups": [
                {"name": "Gemini Models", "description": GEMINI,
                 "buckets": [bucket("gemini", "weekly", gemini_week, reset_week),
                             bucket("gemini", "5h", gemini_5h, reset_5h)]},
                {"name": "Claude and GPT models", "description": THIRD,
                 "buckets": [bucket("3p", "weekly", 1, 0), bucket("3p", "5h", third_5h, reset_5h)]}]}}}


class HomeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.real, self.own = self.root / "home", self.root / "accounts" / "b"
        cli = self.real / ".gemini" / "antigravity-cli"
        (cli / "conversations").mkdir(parents=True)
        (cli / "conversations" / "c1.db").write_text("main's")
        (cli / "antigravity-oauth-token").write_text("fake-main-login")
        (self.real / ".gemini" / "config").mkdir()
        (self.real / ".gemini" / "config" / "GEMINI.md").write_text("rules")
        (self.real / ".config" / "git").mkdir(parents=True)
        (self.real / ".config" / "antigravity").mkdir()
        (self.real / ".ssh").mkdir()
        (self.real / ".gitconfig").write_text("[user]\n")

    def test_an_account_shares_the_conversations_and_keeps_what_it_had_aside(self):
        own_cli = self.own / ".gemini" / "antigravity-cli"
        (own_cli / "conversations").mkdir(parents=True)
        (own_cli / "conversations" / "c2.db").write_text("its own")
        (own_cli / "brain").mkdir()
        moved = agy_homes.prepare(self.own, self.real)
        real_cli = self.real / ".gemini" / "antigravity-cli"
        for name in agy_homes.SHARED:
            self.assertTrue((own_cli / name).is_symlink(), name)
            self.assertEqual(os.readlink(own_cli / name), str(real_cli / name))
            self.assertTrue((real_cli / name).is_dir(), name)
        self.assertEqual((own_cli / "conversations" / "c1.db").read_text(), "main's")
        aside, = moved
        self.assertEqual((Path(aside) / "c2.db").read_text(), "its own")  # Renamed beside, never removed.
        self.assertEqual(len(list(own_cli.glob("brain.own-*"))), 0)  # An empty one goes.
        self.assertTrue((self.own / ".config" / "antigravity").is_dir())
        self.assertEqual(agy_homes.prepare(self.own, self.real), [])  # Once is enough.
        # A parent the test never made (setUp made accounts/ under the umask): prepare makes both, owner-only.
        fresh = self.root / "new-accounts" / "c"
        agy_homes.prepare(fresh, self.real)
        for path in (fresh.parent, fresh, fresh / ".gemini", fresh / ".gemini" / "antigravity-cli", fresh / ".config"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)  # Owner-only, each directory it made.

    def test_a_home_links_the_real_one_but_the_accounts_login(self):
        with agy_homes.AccountHome(self.root / "tmp", self.real, self.own) as home:
            self.assertEqual(home.parent, self.root / "tmp")
            self.assertEqual(os.readlink(home / ".ssh"), str(self.real / ".ssh"))
            self.assertEqual(os.readlink(home / ".gitconfig"), str(self.real / ".gitconfig"))
            gemini, config = home / ".gemini", home / ".config"
            self.assertTrue(gemini.is_dir() and not gemini.is_symlink())
            self.assertEqual(os.readlink(gemini / "config"), str(self.real / ".gemini" / "config"))
            self.assertEqual(os.readlink(gemini / "antigravity-cli"), str(self.own / ".gemini" / "antigravity-cli"))
            self.assertEqual(os.readlink(config / "git"), str(self.real / ".config" / "git"))
            self.assertEqual(os.readlink(config / "antigravity"), str(self.own / ".config" / "antigravity"))
            # agy writes its login as a new file renamed over the old one: it lands in the account's directory.
            cli = gemini / "antigravity-cli"
            (cli / "token.tmp").write_text("fake-b-login")
            os.replace(cli / "token.tmp", cli / "antigravity-oauth-token")
            self.assertEqual((cli / "conversations" / "c1.db").read_text(), "main's")
        self.assertFalse(home.exists())
        self.assertEqual((self.own / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").read_text(),
                         "fake-b-login")
        self.assertEqual((self.real / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").read_text(),
                         "fake-main-login")
        self.assertEqual((self.real / ".gemini" / "config" / "GEMINI.md").read_text(), "rules")
        self.assertTrue(agy_homes.signed_in(self.own))

    def test_agy_as_runs_agy_as_the_account_and_lists_the_accounts(self):
        record = self.root / "agy-as.json"
        script = self.root / "fake_agy.py"
        script.write_text("import json, os, sys\n"
                          "json.dump({'args': sys.argv[1:], 'home': os.environ['HOME'], 'cli': os.path.realpath("
                          "os.path.join(os.environ['HOME'], '.gemini', 'antigravity-cli'))}, open(sys.argv[1], 'w'))\n"
                          "sys.exit(7)\n")
        env = {"HOME": str(self.real), "AGY_ACCOUNTS_DIR": str(self.root / "accounts"), "AGY_CMD": sys.executable}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err), \
                mock.patch("signal.signal"):
            self.assertEqual(agy_homes.main(["b", str(script), str(record), "-p", "x"]), 7)
            self.assertIn("还没有登录", err.getvalue())
            self.assertEqual(agy_homes.main(["--list"]), 0)
            self.assertEqual(agy_homes.main(["../x"]), 2)
        seen = json.loads(record.read_text())
        self.assertEqual(seen["args"], [str(record), "-p", "x"])
        self.assertEqual(seen["cli"], str((self.own / ".gemini" / "antigravity-cli").resolve()))
        self.assertFalse(Path(seen["home"]).exists())
        self.assertEqual(Path(seen["home"]).parent, self.root / "accounts" / ".homes")
        self.assertEqual(out.getvalue().split("\t")[:2], ["b", "未登录"])


class ReadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "agy-accounts.json"
        patcher = mock.patch.dict(os.environ, {agy_accounts.ACCOUNTS_CONFIG_ENV: str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False))

    def test_the_configuration_is_off_until_written_and_says_what_is_wrong(self):
        self.assertEqual(agy_accounts.load(), [])
        self.write({"accounts": [{"name": "主号"}, {"name": "副号", "home": "/absolute/b"}]})
        self.assertEqual(agy_accounts.load(), [agy_accounts.Account("主号", None),
                                               agy_accounts.Account("副号", Path("/absolute/b"))])
        self.write({"enabled": False, "accounts": [{"name": "主号"}, {"name": "副号", "home": "/absolute/b"}]})
        self.assertEqual(agy_accounts.load(), [])
        self.write({"accounts": [{"name": "主号"}]})
        self.assertEqual(agy_accounts.load(), [])  # One account: the machine's own login, nothing to switch to.
        self.write({"accounts": [{"name": "主号"}, {"name": "副号", "home": "/absolute/b", "enabled": False}]})
        self.assertEqual(agy_accounts.load(), [])
        self.assertFalse(agy_accounts.configured())
        for bad in ({"accounts": [{"name": "a"}, {"name": "a", "home": "/b"}]},
                    {"accounts": [{"name": "a"}, {"name": "b", "home": "relative"}]},
                    {"accounts": [{"name": "a"}, {"name": "b"}]}, {"accounts": "x"}, [1]):
            with self.subTest(bad=bad):
                self.write(bad)
                with self.assertRaises(ValueError):
                    agy_accounts.load()
        self.path.write_text("{")
        with self.assertRaises(ValueError):
            agy_accounts.load()

    def test_usage_reads_each_groups_lowest_bucket_and_when_a_spent_one_resets(self):
        now = time.time()
        found = agy_accounts.parse_usage(json.dumps(usage(gemini_5h=0, reset_5h=now + 7200)))
        self.assertEqual(sorted(found), ["3p", "gemini"])
        gemini = found["gemini"]
        self.assertEqual((gemini["left"], gemini["window"], gemini["name"]), (0.0, "5h", "Gemini Models"))
        self.assertAlmostEqual(gemini["reset"], now + 7200, delta=2)
        self.assertEqual((found["3p"]["left"], found["3p"]["reset"]), (1.0, 0.0))
        both = agy_accounts.parse_usage(json.dumps(usage(gemini_5h=0, gemini_week=0.005, reset_5h=now + 60,
                                                         reset_week=now + 86400)))["gemini"]
        self.assertEqual(both["window"], "weekly")  # The one that resets last says when it is back.
        for text in ("", "{", json.dumps({"status": "ERROR", "error": "Eligibility check failed"}), "[]"):
            self.assertIsNone(agy_accounts.parse_usage(text))

    def test_a_models_group_is_the_one_usage_names_it_in_else_told_from_its_id(self):
        state = self.root / "state"
        (state / "launch-models").mkdir(parents=True)
        (state / "launch-models" / "agy.json").write_text(json.dumps({"models": {
            "gemini-pro-orchestrator": {"display_name": "Orchestrator (Carbon)"},
            "claude-opus-5-5": {"display_name": "Claude Opus 5.5"}}}))
        groups = agy_accounts.parse_usage(json.dumps(usage()))
        self.assertEqual(agy_accounts.group_of(state, "gemini-pro-orchestrator", groups), "gemini")
        self.assertEqual(agy_accounts.group_of(state, "claude-opus-5-5", groups), "3p")
        self.assertEqual(agy_accounts.group_of(state, "gpt-oss-120b"), "3p")
        self.assertEqual(agy_accounts.group_of(state, ""), "gemini")

    def test_a_turn_starts_on_the_active_account_unless_it_is_held_out(self):
        main, b = agy_accounts.Account("主号", None), agy_accounts.Account("副号", self.root / "b")
        (self.root / "b" / ".gemini" / "antigravity-cli").mkdir(parents=True)
        accounts, now = [main, b], time.time()
        self.assertEqual(agy_accounts.pick({}, accounts, "gemini", now), main)
        self.assertEqual(agy_accounts.pick({"active": "副号"}, accounts, "gemini", now), main)  # Not signed in.
        (self.root / "b" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("fake")
        self.assertEqual(agy_accounts.pick({"active": "副号"}, accounts, "gemini", now), b)
        self.assertEqual(agy_accounts.pick({"active": "副号"}, accounts, "3p", now), b)  # An older file's one name: every group's.
        self.assertEqual(agy_accounts.pick({"active": {"gemini": "副号"}}, accounts, "3p", now), main)  # Each group its own.
        spent = {"active": "副号", "spent": {"副号": {"gemini": now + 60}}}
        self.assertEqual(agy_accounts.pick(spent, accounts, "gemini", now), main)
        self.assertEqual(agy_accounts.pick(spent, accounts, "3p", now), b)
        spent["spent"]["主号"] = {"gemini": now + 30}
        self.assertEqual(agy_accounts.pick(spent, accounts, "gemini", now), b)  # All held out: the active one.
        self.assertEqual(agy_accounts.pick({"login": {"主号": now + 60}}, accounts, "gemini", now), b)  # An older file's.
        self.assertEqual(agy_accounts.pick({"paused": {"主号": now}}, accounts, "gemini", now), b)
        self.assertEqual(agy_accounts.pick({"only": "主号", "spent": {"主号": {"gemini": now + 60}}}, accounts, "gemini",
                                           now), main)  # 只用这个号: it, spent or not.


class TurnSetup(unittest.TestCase):
    """Through the core's Antigravity backend: a turn as the Bridge runs it, its agy a stand-in (WRAPPER)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.real, self.own = self.root / "home", self.root / "accounts" / "b"
        self.real_cli = self.real / ".gemini" / "antigravity-cli"
        self.own_cli = self.own / ".gemini" / "antigravity-cli"
        for cli in (self.real_cli, self.own_cli):
            cli.mkdir(parents=True)
            (cli / "antigravity-oauth-token").write_text("fake-login")
        self.config = self.root / "agy-accounts.json"
        self.config.write_text(json.dumps({"accounts": [{"name": "主号"}, {"name": "副号", "home": str(self.own)}]},
                                          ensure_ascii=False))
        wrapper = self.root / "agy_wrapper.py"
        wrapper.write_text(WRAPPER)
        self.probes = self.root / "probes.txt"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_")}
        patcher = mock.patch.dict(os.environ, {
            **clean, "HOME": str(self.real), agy_accounts.ACCOUNTS_CONFIG_ENV: str(self.config),
            "FAKE_AGY_STREAM": str(FIXTURES / "harness" / "agy_stream.py"),
            "FAKE_AGY_RECORD": str(self.root / "record.json"), "FAKE_PROBES": str(self.probes)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = make_context(self.root, model="gemini-4-neon", effort="", backend_cmd=[sys.executable, "-I", str(wrapper)])
        self.state = self.root / "state" / "agy-accounts"

    def accounts(self, spent=None, **usages):
        """FAKE_SPENT and FAKE_USAGE by account: ``main``, ``b``."""
        clis = {"main": str(self.real_cli.resolve()), "b": str(self.own_cli.resolve())}
        os.environ["FAKE_SPENT"] = json.dumps({clis[k]: v for k, v in (spent or {}).items()})
        os.environ["FAKE_USAGE"] = json.dumps({clis[k]: v for k, v in usages.items()})

    def run_turn(self, scenario="ok"):
        os.environ["FAKE_AGY_SCENARIO"] = scenario
        events = []
        with mock.patch.object(extensions, "hook", return_value=None):
            result = AntigravityBackend().run(RunRequest(self.ctx, "做个任务", callbacks=RunCallbacks(
                event=lambda kind, data, control: events.append((kind, data))),
                inputs=RunInputs(threading.Event(), run_id=RUN)))
        path = self.root / "record.json.all"
        runs = [json.loads(line) for line in path.read_text().splitlines()]
        path.unlink()
        notes = [data["text"] for kind, data in events if kind == "progress_note" and data["id"].startswith("agy-switch-")]
        return result, runs, notes

    def state_now(self):
        return json.loads((self.state / "state.json").read_text())


class TurnTests(TurnSetup):

    def test_a_spent_account_hands_the_conversation_to_the_next_and_the_next_turns_stay_there(self):
        reset = time.time() + 2 * 3600
        self.accounts(spent={"main": "after-steps"}, main=usage(gemini_5h=0, reset_5h=reset), b=usage())
        result, runs, notes = self.run_turn()
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id), ("完成", SID))
        first, second = runs
        self.assertEqual((first["home"], first["cli"]), (str(self.real), str(self.real_cli.resolve())))
        self.assertEqual(second["cli"], str(self.own_cli.resolve()))
        self.assertEqual(second["argv"][second["argv"].index("--conversation") + 1], SID)
        self.assertFalse(Path(second["home"]).exists())  # The account's HOME goes with the turn.
        note, = notes
        self.assertEqual(note, f"主号的 Gemini 5 小时额度用完了（{agy_accounts.when(reset)} 恢复），换副号接着做")
        state = self.state_now()
        self.assertEqual(state["active"], {"gemini": "副号"})
        self.assertEqual(state["switched"]["gemini"]["to"], "副号")
        self.assertAlmostEqual(state["spent"]["主号"]["gemini"], reset, delta=2)
        self.assertEqual(state["groups"], {"gemini-4-neon": "gemini"})
        # The next turn starts on 副号 and stays there, though 主号 has quota again: no going back and forth.
        self.accounts(main=usage(), b=usage())
        self.probes.write_text("")
        result, runs, notes = self.run_turn()
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(([run["cli"] for run in runs], notes), ([str(self.own_cli.resolve())], []))
        self.assertEqual(self.probes.read_text(), "")  # A turn that does not fail asks /usage nothing.
        self.ctx = dataclasses.replace(self.ctx, model="claude-opus-5-5")
        result, runs, notes = self.run_turn()
        # Claude/GPT is a group of its own: its active account is still 主号, whose Claude quota was never spent.
        self.assertEqual([run["cli"] for run in runs], [str(self.real_cli.resolve())])
        rows = [json.loads(line) for line in (self.state / "turns.jsonl").read_text().splitlines()]
        self.assertEqual([(r["account"], r["group"]) for r in rows],
                         [("主号", "gemini"), ("副号", "gemini"), ("副号", "gemini"), ("主号", "3p")])
        self.assertEqual(rows[0]["run"], RUN)
        log = (self.state / "accounts.log").read_text()
        self.assertIn("switch run=33333333 from=主号 to=副号 code=quota_exhausted group=gemini", log)
        self.assertIn("turn run=33333333 account=副号 group=gemini", log)

    def test_a_passing_limit_with_quota_left_changes_no_account(self):
        self.accounts(main=usage(gemini_5h=0.4), b=usage())
        result, runs, notes = self.run_turn("busy")
        self.assertEqual(result.outcome.error_code, "rate_limited")
        self.assertEqual((len(runs), notes), (1, []))
        self.assertIn("why=quota-left", (self.state / "accounts.log").read_text())
        self.assertEqual(self.state_now().get("active", {}).get("gemini", "主号"), "主号")

    def test_agys_own_words_count_when_usage_lags_or_says_nothing(self):
        """/usage may lag a request behind the refusal: "exhausted your capacity … reset after 1h2m3s" with NEAR or
        less left is spent until then; without /usage the words alone do; neither, and nothing changes."""
        cases = ((usage(gemini_5h=0.05), "before-init", True), (None, "before-init", True),
                 (usage(gemini_5h=0.5), "before-init", False), (None, "busy", False))
        for main, spent, switched in cases:
            with self.subTest(main=main and "usage", spent=spent):
                for leftover in (self.state / "state.json", self.probes):
                    if leftover.exists():
                        leftover.unlink()
                self.accounts(spent={"main": spent} if spent != "busy" else {}, b=usage(),
                              **({"main": main} if main else {}))
                result, runs, notes = self.run_turn("busy" if spent == "busy" else "ok")
                self.assertEqual((not result.is_error, len(runs)), (switched, 2 if switched else 1))
                if switched:
                    until = self.state_now()["spent"]["主号"]["gemini"]
                    self.assertAlmostEqual(until, time.time() + 3723, delta=30)  # reset after 1h2m3s
                    self.assertTrue(notes[0].startswith("主号的 Gemini ") and notes[0].endswith("换副号接着做"), notes)
        self.assertEqual(agy_accounts.reset_in("Your quota will reset after 4h12m."), 4 * 3600 + 12 * 60)
        self.assertEqual(agy_accounts.reset_in("You have exhausted your quota on this model."), 0)

    def test_a_failed_login_holds_the_account_out_and_the_turn_goes_on(self):
        self.accounts(spent={"main": "logged-out"}, b=usage())
        result, runs, notes = self.run_turn()
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(notes, ["主号的登录用不了了，要重新登录，换副号接着做"])
        self.assertEqual(self.state_now()["login"]["主号"]["mark"], agy_accounts.login_mark(agy_accounts.Account("主号", None),
                                                                                          self.real))
        self.assertEqual(self.probes.read_text().splitlines(), [str(self.own_cli.resolve())])  # Only the next one.
        # Held out however long it is, until it signs in again (its login file changes).
        state, main = self.state_now(), agy_accounts.Account("主号", None)
        self.assertTrue(agy_accounts.held(state, main, "3p", time.time() + 7 * 86400, self.real))
        token = self.real_cli / "antigravity-oauth-token"
        token.write_text("fake-new-login-after-signing-in-again")
        self.assertFalse(agy_accounts.held(state, main, "3p", time.time(), self.real))

    def test_with_every_account_spent_the_turn_fails_as_spent_with_when_the_first_is_back(self):
        reset = time.time() + 3600
        self.accounts(spent={"main": "after-steps"}, main=usage(gemini_5h=0, reset_5h=reset),
                      b=usage(gemini_week=0, reset_week=reset + 86400))
        result, runs, notes = self.run_turn()
        self.assertEqual((result.outcome.error_code, len(runs), notes), ("quota_exhausted", 1, []))
        self.assertAlmostEqual(result.limit["reset_at"], reset, delta=2)  # For the core's retry at the reset.
        self.assertEqual(result.failure_detail,
                         f"2 个号的 Gemini 额度都用完了或要重新登录，最早 {agy_accounts.when(reset)} 恢复")
        spent = self.state_now()["spent"]
        self.assertEqual(sorted(spent), ["主号", "副号"])
        self.assertAlmostEqual(spent["副号"]["gemini"], reset + 86400, delta=2)
        # A turn after 主号's reset starts there by itself.
        self.assertEqual(agy_accounts.pick(self.state_now(), agy_accounts.load(), "gemini", reset + 60).name, "主号")

    def test_without_a_configuration_or_with_a_broken_one_the_turn_runs_as_it_did(self):
        """Fewer than two accounts, or a file that cannot be used: the accounts are off (the doctor says why)."""
        self.accounts(spent={"main": "after-steps"}, b=usage())
        for text in (None, "{"):
            with self.subTest(text=text):
                if text is None:
                    self.config.unlink()
                else:
                    self.config.write_text(text)
                result, runs, notes = self.run_turn()
                self.assertEqual((result.outcome.error_code, len(runs), notes), ("quota_exhausted", 1, []))
                self.assertEqual(runs[0]["home"], str(self.real))
        self.assertFalse(self.probes.exists())


class MovedRulesTests(TurnSetup):
    """T831's rules on top of T674's: an account /usage says nothing of is tried, a switch another topic made is taken,
    只用这个号 waits for its account, the page's snapshots, agy-as installed as a copy."""

    def test_an_account_usage_says_nothing_of_is_not_taken_for_signed_out(self):
        reset = time.time() + 3600
        self.accounts(spent={"main": "after-steps"}, main=usage(gemini_5h=0, reset_5h=reset))  # 副号's /usage fails.
        result, runs, notes = self.run_turn()
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(len(runs), 2)
        self.assertTrue(notes[0].endswith("换副号接着做"), notes)
        state = self.state_now()
        self.assertIn("副号", state["unknown"])
        self.assertNotIn("副号", state["login"])

    def test_a_switch_another_topic_made_meanwhile_is_taken_without_asking(self):
        self.accounts(b=usage())
        records = agy_accounts.Records(self.ctx.state_dir)
        with contextlib.ExitStack() as stack:
            turn = agy_accounts.Turn(self.ctx, dict(os.environ), agy_accounts.load(), stack, records, RUN)
            turn.first()
            self.assertEqual(turn.current.name, "主号")

            def other(state):
                state["active"]["gemini"] = "副号"
                state["spent"].setdefault("主号", {})["gemini"] = time.time() + 600
            records.change(other)
            env, note = turn({"code": "quota_exhausted", "error": "You have exhausted your quota on this model."})
        self.assertTrue(note.startswith("主号的 Gemini 额度用完了（") and note.endswith("恢复），换副号接着做"), note)
        self.assertFalse(self.probes.exists())  # Nothing asked: the other topic did.
        self.assertIn("followed run=33333333 from=主号 to=副号", (self.state / "accounts.log").read_text())

    def test_only_this_account_waits_for_it_and_says_so(self):
        reset = time.time() + 1800
        agy_accounts.set_choice(self.ctx.state_dir, only="主号")
        self.accounts(spent={"main": "after-steps"}, main=usage(gemini_5h=0, reset_5h=reset), b=usage())
        result, runs, notes = self.run_turn()
        self.assertEqual((result.outcome.error_code, len(runs), notes), ("quota_exhausted", 1, []))
        self.assertEqual(result.failure_detail, f"只用主号：它的 Gemini 额度用完了，最早 {agy_accounts.when(reset)} 恢复")
        self.assertAlmostEqual(result.limit["reset_at"], reset, delta=2)
        with self.assertRaises(ValueError):
            agy_accounts.set_choice(self.ctx.state_dir, pause="没有的号")
        state = agy_accounts.set_choice(self.ctx.state_dir, pause="主号")
        self.assertNotIn("only", state)  # Paused, it is no longer the only one.
        self.assertEqual(agy_accounts.pick(state, agy_accounts.load(), "gemini", time.time()).name, "副号")

    def test_the_snapshots_are_made_from_the_state_one_a_group(self):
        reset = time.time() + 3600
        self.accounts(spent={"main": "after-steps"}, main=usage(gemini_5h=0, reset_5h=reset), b=usage(third_5h=0.3))
        self.run_turn()
        self.probes.unlink()
        written = agy_accounts.snapshots(self.ctx.state_dir)
        self.assertEqual(written, ["agy.gemini", "agy.3p"])
        self.assertFalse(self.probes.exists())  # Made from the state: no account asked.
        store = quota.Store(self.ctx.state_dir)
        gemini, third = store.read("agy.gemini"), store.read("agy.3p")
        self.assertEqual((gemini["kind"], gemini["title"], gemini["switched"]["to"]), ("agy", "agy · Gemini", "副号"))
        main, b = gemini["snapshot"]["accounts"]
        self.assertEqual((main["alias"], main["state"], b["state"], b.get("active")), ("主号", "spent", "ok", True))
        self.assertAlmostEqual(main["available_at"], reset, delta=2)
        self.assertEqual([(w["kind"], w["used"]) for w in main["windows"]], [("5h", 1.0), ("7d", 0.0)])
        self.assertEqual(quota.pool(gemini["snapshot"]), {"usable": 1, "total": 2, "next_available_at": None})
        main3, b3 = third["snapshot"]["accounts"]
        self.assertEqual((main3.get("active"), b3.get("active")), (True, None))  # Claude/GPT still on 主号.
        self.assertEqual([w["used"] for w in b3["windows"] if w["kind"] == "5h"], [0.7])
        # An account named after an e-mail is shown by its number; off, the agy snapshots go.
        self.assertEqual(agy_accounts.alias_of(agy_accounts.Account("someone@example.com", None), 2), "号 2")
        self.config.unlink()
        self.assertEqual(agy_accounts.snapshots(self.ctx.state_dir), [])
        self.assertEqual(store.entries(), [])

    def log_in(self, cli, *emails, tail=""):
        """An agy cli.log of the sign-ins it logged (the line agy 1.5.14 writes), then ``tail``."""
        lines = [f"I1126 20:31:16.740793       1 server_oauth.go:226] OAuth: authenticated successfully as {e}" for e in emails]
        (cli / "cli.log").write_text("\n".join(["I1126 20:00:00.000000 1 other.go:1] starting", *lines, tail]) + "\n")

    def test_each_account_shows_the_e_mail_its_own_log_says_and_never_reads_the_token(self):
        self.log_in(self.real_cli, "old@example.com", "main@example.com", tail="I1126 20:32:00.000000 1 x.go:1] done")
        self.log_in(self.own_cli, "spare@example.com")
        opened = []
        real_open = open

        def watching(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)
        with mock.patch("builtins.open", watching):
            agy_accounts.snapshots(self.ctx.state_dir)
        self.assertFalse([path for path in opened if agy_homes.TOKEN in path], opened)  # The login's file is never opened.
        main, spare = quota.Store(self.ctx.state_dir).read("agy.gemini")["snapshot"]["accounts"]
        self.assertEqual((main["alias"], main["login"], spare["login"]), ("主号", "main@example.com", "spare@example.com"))
        self.assertEqual(agy_accounts.login_email(self.root / "nowhere"), "")
        # The log is gone (agy starts a new one): the e-mail seen last stays; one never seen leaves the name to show.
        (self.real_cli / "cli.log").unlink()
        (self.own_cli / "cli.log").write_text("OAuth: authenticated successfully as not-an-email\n")
        agy_accounts.snapshots(self.ctx.state_dir)
        main, spare = quota.Store(self.ctx.state_dir).read("agy.gemini")["snapshot"]["accounts"]
        self.assertEqual((main["login"], spare["login"]), ("main@example.com", "spare@example.com"))
        state = agy_accounts.Records(self.ctx.state_dir).read()
        state["logins"].pop("副号")
        (self.state / "state.json").write_text(json.dumps(state))
        agy_accounts.snapshots(self.ctx.state_dir)
        self.assertNotIn("login", quota.Store(self.ctx.state_dir).read("agy.gemini")["snapshot"]["accounts"][1])
        self.assertNotIn("fake-login", json.dumps(quota.Store(self.ctx.state_dir).entries()))

    def test_the_snapshot_entries_say_which_accounts_ran_a_turn_in_each_group_this_week(self):
        now = time.time()
        records = agy_accounts.Records(self.ctx.state_dir)
        for at, account, group in ((now - 3600, "主号", "gemini"), (now - 8 * 86400, "副号", "gemini"),
                                   (now - 2 * 86400, "副号", "3p")):
            records.turn({"at": round(at), "run": "r", "account": account, "group": group, "model": "m"})
        with (self.state / "turns.jsonl").open("a") as turns:
            turns.write("not json\n{\"at\": \"x\"}\n")  # Rows that do not read are skipped.
        agy_accounts.snapshots(self.ctx.state_dir)
        store = quota.Store(self.ctx.state_dir)
        self.assertEqual((store.read("agy.gemini")["recent"], store.read("agy.3p")["recent"]), (["主号"], ["副号"]))

    def test_agy_as_installs_as_a_copy_that_needs_nothing_else(self):
        folder = self.root / "bin"
        target = agy_homes.install(folder)
        self.assertEqual(target, folder / "agy-as")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)  # Executable (the test's /dev/shm is noexec: not run here).
        self.assertTrue(target.read_text().startswith("#!"))
        done = subprocess.run([sys.executable, "-I", str(target), "--help"], capture_output=True, text=True, timeout=30,
                              cwd=str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("agy-as --install", done.stdout)
        old = self.root / "old"
        old.mkdir()
        os.symlink(self.root / "covey_internal" / "bin" / "agy_as.py", old / "agy-as")
        self.assertIn("已经没有了", agy_homes.link_problem(old))
        (self.root / "covey_internal" / "bin").mkdir(parents=True)
        (self.root / "covey_internal" / "bin" / "agy_as.py").write_text("")
        self.assertIn("扩展里的旧文件", agy_homes.link_problem(old))
        agy_homes.install(old)  # The link itself replaced, what it pointed at left alone.
        self.assertEqual(agy_homes.link_problem(old), "")
        # A script of one's own that runs the extension's copy of an older release: said too, replaced the same way.
        (old / "agy-as").write_text("#!/bin/sh\nexec python3 /absolute/releases/x/extensions/covey_internal/bin/agy_as.py\n")
        self.assertIn("还在找扩展里的旧文件", agy_homes.link_problem(old))
        agy_homes.install(old)
        self.assertEqual(agy_homes.link_problem(old), "")
        self.assertEqual((self.root / "covey_internal" / "bin" / "agy_as.py").read_text(), "")


if __name__ == "__main__":
    unittest.main()
