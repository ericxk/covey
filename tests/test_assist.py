"""辅助模型 (app/assist.py, core/assist.py; docs/configuration.md 辅助模型): one way out for the background model calls, and
"说了不用就不用": a matrix of the enabled Harnesses × the setting (自动, Codex, Claude Code, 不用模型) × every feature (A1 to
A10), counting each process a Harness would start, then the cases around it: a timeout that never goes to another Harness,
a break kept across a restart, an official refusal's cooling, Antigravity's accounts, a 整理 whose setting changes halfway, the last check before a process,
Claude Code's error envelope read as a turn's result, and its one-shot inside an extension's relay (E2)."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.assist import harness_app, topics_with
from covey.app import assist, project_sorter, track_judge
from covey.app.track_judge import Failed
from covey.core import assist as records
from covey.core import butler_settings
from covey.core.harness import AskFailed, HarnessCapabilities
from covey.core.track_route import LiveJob, Message, Quote

NOW = 1_800_000_000.0
ANSWER = '{"route":"merge","target":1,"confidence":0.9}'
SETTINGS = {"auto": butler_settings.AssistChoice(), "codex": butler_settings.AssistChoice(harness="codex"),
            "claude": butler_settings.AssistChoice(harness="claude"), "none": butler_settings.AssistChoice(off=True)}
APPS = (("claude", "codex", "agy"), ("claude", "codex"), ("claude",))


class Spawns:
    """Every process a Harness would start: Claude Code's one-shots, a native Harness's ask_once."""

    def __init__(self):
        self.calls = []

    def count(self, name):
        return sum(1 for call in self.calls if call == name)


def native_app(name, spawns, *, answer=ANSWER, state):
    def ask_once(ctx, system, data, *, heavy=False, limit, cwd=None, follow=""):
        spawns.calls.append(name)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    backend = SimpleNamespace(capabilities=lambda ctx: HarnessCapabilities(ask_once=True), ask_once=ask_once,
                              ask_model=lambda ctx, heavy=False, follow="": f"{name}-{'big' if heavy else 'light'}")
    ctx = SimpleNamespace(backend_cmd=[name], state_dir=state, options={"harness_id": name}, device=None)
    return harness_app(backend, ctx)


def claude_app(state):
    backend = SimpleNamespace(wrap_argv=lambda argv, ctx: argv, child_env=lambda ctx: {}, name="claude")
    return harness_app(backend, SimpleNamespace(backend_cmd=["claude"], state_dir=state, options={}, device=None))


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.spawns = Spawns()
        patches = [mock.patch.object(track_judge, "claude_once", side_effect=self.claude_once),
                   mock.patch.object(assist, "ready", return_value=""),
                   mock.patch("covey.core.butler.available", return_value=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("COVEY_TRACK_JUDGE", "COVEY_TRACK_JUDGE_MS"):
            os.environ.pop(name, None)
        self.claude_answer = json.dumps({"type": "result", "is_error": False, "result": ANSWER})
        self.logs = []

    def claude_once(self, backend, ctx, data, cwd, limit, **kw):
        self.spawns.calls.append("claude")
        if isinstance(self.claude_answer, BaseException):
            raise self.claude_answer
        return self.claude_answer

    def topics(self, names=("claude", "codex", "agy"), *, default="claude", answers=None):
        answers = answers or {}
        apps = {}
        for name in names:
            apps[name] = claude_app(self.state) if name == "claude" else \
                native_app(name, self.spawns, answer=answers.get(name, ANSWER), state=self.state)
        topics = topics_with(self.state, apps, default=default)
        topics.runtime.log = self.logs.append
        return topics

    def setting(self, choice, **more):
        butler_settings.update(self.state, assist=choice, **more)

    def message(self):
        return Message("这个也一起改了", (LiveJob(1, "j1", "running", "整理对比表", NOW - 300, NOW - 200),), Quote(), now=NOW)

    def call(self, topics, feature, manual=False):
        """One call of ``feature`` the way its caller makes it (分流's judge; the others' ask)."""
        if feature == assist.TRACK:
            return assist.judge(self.message(), topics=topics, log=self.logs.append)
        try:
            return assist.ask(feature, "系统", "{}", topics=topics, limit=5, cwd=self.state / "judge", manual=manual)
        except Failed as exc:
            return exc


class MatrixTest(Case):
    def test_a_named_codex_and_none_start_no_claude_code_on_any_feature(self):
        for names in APPS:
            for key, choice in SETTINGS.items():
                for feature in assist.FEATURES:
                    with self.subTest(apps=names, setting=key, feature=feature):
                        self.spawns.calls.clear()
                        topics = self.topics(names)
                        self.setting(choice)
                        self.call(topics, feature)
                        if key in ("codex", "none"):
                            self.assertEqual(self.spawns.count("claude"), 0)
                        if key == "none":
                            self.assertEqual(self.spawns.calls, [])
                        if key == "codex":
                            self.assertEqual(self.spawns.count("codex"), 1 if "codex" in names else 0)
                        if key in ("claude", "auto"):  # 自动 with the butler off follows the Bridge's default: Claude Code.
                            self.assertEqual(self.spawns.count("codex") + self.spawns.count("agy"), 0)
                            self.assertGreaterEqual(self.spawns.count("claude"), 1)

    def test_automatic_follows_the_butler_then_the_named_harnesses(self):
        topics = self.topics()
        self.setting(SETTINGS["auto"], enabled=True, harness="agy")
        self.assertEqual(assist.route(topics).harness, "agy")
        self.setting(SETTINGS["auto"], enabled=False,
                     topic_defaults=butler_settings.TopicDefaults(harness="codex"))
        self.assertEqual(assist.route(topics).harness, "codex")  # 纯话题: the butler is not followed.
        self.setting(SETTINGS["auto"], enabled=False, topic_defaults=butler_settings.TopicDefaults())
        self.assertEqual(assist.route(topics).harness, "claude")  # Nothing named anywhere: the Bridge's default.
        self.setting(SETTINGS["auto"], enabled=True, harness="codex")
        found = assist.route(topics)
        self.assertEqual((found.harness, found.rest), ("codex", ()))  # Only the Harnesses the settings name, no others.

    def test_the_followed_settings_model_goes_with_it(self):
        topics = self.topics()
        self.setting(SETTINGS["auto"], enabled=True, harness="codex", model="acme/gpt-6-astra")
        self.assertEqual(assist.route(topics).first.follow, "acme/gpt-6-astra")
        self.setting(SETTINGS["codex"])
        self.assertEqual(assist.route(topics).first.follow, "")  # Named: its own provider.

    def test_a_named_harness_that_is_not_enabled_or_retired_stays_named(self):
        topics = self.topics(("claude",))
        self.setting(butler_settings.AssistChoice(harness="pi"))
        self.assertEqual(butler_settings.load(self.state).assist.harness, "pi")  # Not emptied into 自动.
        found = assist.route(topics)
        self.assertEqual((found.first, found.why, found.about), (None, "not_enabled", "pi"))


class NoStandbyTest(Case):
    """分流 keeps no process between questions (T898): each judgment through Claude Code is one one-shot, none before it."""

    def test_each_judgment_starts_one_one_shot_and_nothing_else(self):
        topics = self.topics()
        self.setting(SETTINGS["claude"])
        for _ in range(2):
            verdict = assist.judge(self.message(), topics=topics, log=self.logs.append)
            self.assertEqual(verdict.route, "merge")
        self.assertEqual(self.spawns.calls, ["claude", "claude"])
        self.assertFalse(hasattr(assist, "warm"))

    def test_a_settings_change_still_clears_the_breaks_and_coolings(self):
        topics = self.topics(answers={"codex": AskFailed("authentication_error")})
        self.setting(SETTINGS["codex"])
        self.call(topics, assist.TIDY)
        self.assertIn("codex", records.read(self.state, fresh=True)["breakers"])
        self.setting(SETTINGS["auto"])  # core/butler_settings.py listen: app/assist.py on_settings_changed.
        self.assertEqual(records.read(self.state, fresh=True)["breakers"], {})


class FallbackTest(Case):
    def test_a_timeout_never_goes_to_another_harness(self):
        topics = self.topics(answers={"codex": AskFailed("timeout")})
        self.setting(SETTINGS["auto"], enabled=True, harness="codex")
        for feature in (assist.TIDY, assist.LEARN, assist.TRACK):
            with self.subTest(feature=feature):
                self.spawns.calls.clear()
                outcome = self.call(topics, feature)
                self.assertEqual(self.spawns.calls, ["codex"])
                self.assertEqual(getattr(outcome, "reason", getattr(outcome, "why", "")), "timeout")

    def test_an_environment_failure_goes_to_the_next_but_for_the_track_judge(self):
        topics = self.topics(answers={"codex": AskFailed("quota_exhausted", "quota exceeded")})
        self.setting(SETTINGS["auto"], enabled=True, harness="codex",
                     topic_defaults=butler_settings.TopicDefaults(harness="claude"))
        verdict = assist.judge(self.message(), topics=topics, log=self.logs.append)
        self.assertEqual((verdict.route, verdict.why), ("new", "quota_exhausted"))
        self.assertEqual(self.spawns.calls, ["codex"])  # 分流 asks once: no second Harness on the message's path.
        records.change(self.state, lambda data: data["breakers"].clear())
        self.spawns.calls.clear()
        raw = self.call(topics, assist.TIDY)
        self.assertEqual(self.spawns.calls, ["codex", "claude"])
        self.assertEqual(track_judge.answer_text(raw, "claude"), ANSWER)
        self.spawns.calls.clear()
        self.call(topics, assist.TIDY)
        self.assertEqual(self.spawns.calls, ["claude"])  # Codex is broken for 15 minutes: not tried first again.

    def test_a_named_harness_that_fails_is_never_replaced(self):
        topics = self.topics(answers={"codex": AskFailed("authentication_error")})
        self.setting(SETTINGS["codex"])
        outcome = self.call(topics, assist.TIDY)
        self.assertEqual((outcome.reason, outcome.via, self.spawns.calls), ("authentication_error", "codex", ["codex"]))

    def test_a_break_holds_across_a_restart_for_the_automatic_calls_and_not_for_the_owners(self):
        topics = self.topics(answers={"codex": AskFailed("authentication_error")})
        self.setting(SETTINGS["codex"])
        self.call(topics, assist.LEARN)
        assist.forget()  # A restart: nothing in memory.
        records.forget()
        self.assertEqual(records.read(self.state, fresh=True)["breakers"]["codex"]["code"], "authentication_error")
        self.spawns.calls.clear()
        self.assertEqual(self.call(topics, assist.LEARN).reason, "broken")
        self.assertEqual(self.spawns.calls, [])
        ok = self.topics()
        self.call(ok, assist.TIDY, manual=True)  # ✨ 整理, the owner's click: tried once, and it clears the break.
        self.assertEqual(self.spawns.calls, ["codex"])
        self.assertNotIn("codex", records.read(self.state, fresh=True)["breakers"])

    def test_the_last_check_refuses_a_harness_the_settings_no_longer_allow(self):
        topics = self.topics()
        self.setting(SETTINGS["codex"])
        stale = assist.Route(assist.Choice("claude", topics.apps["claude"].harness.backend,
                                           topics.apps["claude"].harness.context))
        with mock.patch.object(assist, "route", return_value=stale):
            outcome = self.call(topics, assist.TIDY)
        self.assertEqual((outcome.reason, self.spawns.calls), ("not_allowed", []))
        self.assertIn("assist refused harness=claude feature=tidy", self.logs)


class RefusalTest(Case):
    def test_a_batch_features_refusal_cools_it_on_that_harness_and_a_settings_change_clears_it(self):
        topics = self.topics(answers={"codex": AskFailed("policy_refusal", "Invalid prompt")})
        self.setting(SETTINGS["auto"], enabled=True, harness="codex",
                     topic_defaults=butler_settings.TopicDefaults(harness="claude"))
        self.assertEqual(self.call(topics, assist.TIDY).reason, "policy_refusal")
        self.assertEqual(self.spawns.calls, ["codex"])  # Not sent to the next Harness.
        self.spawns.calls.clear()
        self.assertEqual(assist.route(topics, assist.TIDY).harness, "claude")  # 自动: cooled on Codex only.
        self.setting(SETTINGS["codex"])
        self.assertEqual(records.read(self.state, fresh=True)["cooling"], {})  # The owner chose again.

    def test_an_items_refusal_marks_only_that_item(self):
        topics = self.topics(answers={"codex": AskFailed("policy_refusal")})
        self.setting(SETTINGS["codex"])
        self.call(topics, assist.BACKFILL)
        self.assertEqual(records.read(self.state, fresh=True)["cooling"], {})
        self.assertEqual(assist.route(topics, assist.BACKFILL).harness, "codex")
        self.assertEqual(assist.route(topics, assist.TIDY).harness, "codex")  # Kept under 整理专项's row, not cooling it.

    def test_the_days_refusals_gate_a_harness(self):
        topics = self.topics(answers={"codex": AskFailed("policy_refusal")})
        self.setting(SETTINGS["codex"])
        for _ in range(records.DAILY_REFUSALS):
            self.call(topics, assist.LEARN)
        self.spawns.calls.clear()
        self.assertEqual(self.call(topics, assist.LEARN).reason, "refusal_gate")
        self.assertEqual(self.spawns.calls, [])


class OtherCasesTest(Case):
    def test_antigravity_with_its_accounts_held_is_not_asked(self):
        topics = self.topics()
        self.setting(butler_settings.AssistChoice(harness="agy"))
        with mock.patch.object(assist, "ready", return_value="quota_exhausted"):
            found = assist.route(topics)
            outcome = self.call(topics, assist.LEARN)
        self.assertEqual((found.why, found.detail), ("unusable", "quota_exhausted"))
        self.assertEqual((outcome.reason, self.spawns.calls), ("unusable", []))

    def test_a_tidy_whose_setting_changes_halfway_asks_the_new_one_from_the_next_call(self):
        topics = self.topics()
        where = project_sorter.Where(topics, assist.BACKFILL)
        self.setting(SETTINGS["codex"])
        project_sorter.ask("系统", "{}", where, 5)
        self.setting(SETTINGS["claude"])
        project_sorter.ask("系统", "{}", where, 5)
        self.assertEqual(self.spawns.calls, ["codex", "claude"])

    def test_a_backfill_is_kept_under_the_tidy_row(self):
        topics = self.topics()
        self.setting(SETTINGS["codex"])
        self.call(topics, assist.BACKFILL)
        rows = assist.recent(self.state)
        self.assertEqual([(row["feature"], row["label"], row["ok"]) for row in rows], [("tidy", "整理专项", True)])
        self.assertNotIn("backfill", records.read(self.state, fresh=True)["recent"])

    def test_old_rows_of_features_taken_out_are_not_listed(self):
        records.settle(self.state, feature="tidy_realtime", harness="codex", code="timeout")
        records.settle(self.state, feature="patrol", harness="codex")
        records.settle(self.state, feature="learn", harness="codex")
        self.assertEqual([row["feature"] for row in assist.recent(self.state)], ["learn"])

    def test_the_recent_row_of_each_feature_for_the_web_page(self):
        topics = self.topics(answers={"codex": AskFailed("authentication_error", "401")})
        self.setting(SETTINGS["codex"])
        self.call(topics, assist.TIDY)
        rows = assist.recent(self.state)
        self.assertEqual([(row["feature"], row["name"], row["ok"], row["code"]) for row in rows],
                         [("tidy", "Codex", False, "authentication_error")])
        self.assertEqual(assist.SHOWN, ("track", "tidy", "learn"))
        self.assertEqual(rows[0]["words"], "登录失效或没有权限")
        self.assertNotIn("401", json.dumps(records.read(self.state, fresh=True)))  # No words of the CLI kept.

    def test_the_description_names_the_models(self):
        topics = self.topics()
        self.setting(SETTINGS["codex"])
        self.assertEqual(assist.describe(topics)["words"], "现在：轻的判断用 Codex · codex-light，整理用 Codex · codex-big。")
        self.setting(SETTINGS["none"])
        self.assertEqual(assist.describe(topics)["words"], "后台活不调模型，只按规则做。")
        self.assertEqual(assist.available(topics)[0], False)


class ClaudeEnvelopeTest(unittest.TestCase):
    """Claude Code's one-shot error envelope read the way a turn's result row is (T883 P0-2)."""

    def test_its_errors_are_the_turns_reasons(self):
        cases = ((dict(is_error=True, api_error_status=401, result="Invalid API key"), "authentication_error"),
                 (dict(is_error=True, api_error_status=429, result="rate_limit_error"), "rate_limited"),
                 (dict(is_error=True, api_error_status=402, result="credit balance"), "quota_exhausted"),
                 (dict(is_error=False, stop_reason="refusal", result=""), "policy_refusal"),
                 (dict(is_error=True, result="Prompt is too long"), "too_long"),
                 (dict(is_error=True, result="something odd"), "model_error"))
        for envelope, want in cases:
            with self.subTest(want=want), self.assertRaises(Failed) as caught:
                track_judge.answer_text(json.dumps({"type": "result", "subtype": "success", **envelope}), "claude")
            self.assertEqual(caught.exception.reason, want)

    def test_a_non_zero_exit_with_its_envelope_on_stdout(self):
        envelope = json.dumps({"type": "result", "is_error": True, "api_error_status": 403,
                               "result": "OAuth token revoked " + "sk" + "-ant-" + "abcdef" * 4})
        script = f"import sys; sys.stdout.write({envelope!r}); sys.exit(1)"
        with self.assertRaises(Failed) as caught:
            track_judge.run(["python3", "-c", script], "", os.environ.copy(), None, 10)
        self.assertEqual(caught.exception.reason, "authentication_error")
        self.assertNotIn("abcdefabcdef", caught.exception.detail)  # core/failure.py said_of hides it.

    def test_a_bare_stderr_is_read_by_its_words(self):
        script = "import sys; sys.stderr.write('API Error: Connection error (ECONNREFUSED)'); sys.exit(1)"
        with self.assertRaises(Failed) as caught:
            track_judge.run(["python3", "-c", script], "", os.environ.copy(), None, 10)
        self.assertEqual(caught.exception.reason, "transport_error")

    def test_the_one_shot_runs_inside_the_backends_one_shot_with_its_settings(self):
        seen = {}

        @contextlib.contextmanager
        def one_shot(ctx, env, *, run_id=""):
            seen["inside"] = True
            yield ctx, {**env, "RELAY": "1"}, {"disableAllHooks": True, "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}}
            seen["inside"] = False
        backend = SimpleNamespace(wrap_argv=lambda argv, ctx: argv, child_env=lambda ctx: {}, one_shot=one_shot)
        ctx = SimpleNamespace(backend_cmd=["claude"], state_dir=Path("/absolute/state"), options={})

        def run(argv, data, env, cwd, limit):
            seen.update(argv=argv, env=env, during=seen["inside"])
            return "{}"
        with mock.patch.object(track_judge, "run", side_effect=run):
            track_judge.claude_once(backend, ctx, "{}", Path("/absolute/state"), 5)
        settings = json.loads(seen["argv"][seen["argv"].index("--settings") + 1])
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9")
        self.assertTrue(settings["disableAllHooks"])
        self.assertTrue(seen["during"])
        self.assertEqual(seen["env"]["RELAY"], "1")


if __name__ == "__main__":
    unittest.main()
