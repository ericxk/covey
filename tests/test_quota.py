"""账号与额度 (T831, docs/quota.md): the snapshot format (core/quota.py), its sources (core/quota_sources.py: the gateway
Claude Code signs in to, a command, an address), the Bridge's thread (app/quota_watch.py), a gateway's spent pool on a
429 (backends/claude/completion.py, core/limit_reset.py), the retry at a spent quota's reset (app/auto_retry.py QUOTA),
its card's words (core/failure.py) and a failure put right by the gateway's snapshot (app/quota_failure.py)."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_backends import RUN, SID, codex_context

from covey.app import auto_retry, quota_failure, quota_watch
from covey.backends.claude.completion import ClaudeReplyState, result_failure
from covey.core import failure, limit_reset, quota, quota_sources
from covey.core.harness import HarnessCapabilities, RunResult
from covey.core.result import ExecutionOutcome
from covey.service import plugins

NOW = 1_800_000_000.0
GATEWAY_429 = ("API Error: Request rejected (429) · upstream account quota usage limit has been reached; an account "
               "becomes available again at 17:02 UTC (in 9h50m)")


def snapshot(*accounts, **fields):
    return {"schema": 1, "collected_at": NOW, "ttl": 600, "accounts": list(accounts), **fields}


def account(alias="A1", state="ok", **fields):
    return {"alias": alias, "state": state, "windows": [{"kind": "5h", "used": 0.4, "resets_at": NOW + 3600}], **fields}


class Format(unittest.TestCase):
    def test_a_snapshot_is_kept_to_its_fields_and_says_why_it_is_not_one(self):
        data = snapshot(account(extra="dropped", windows=[{"kind": "5h", "label": "5 小时", "used": 0.42,
                                                            "resets_at": NOW + 7200, "ceiling": 0.9, "other": 1},
                                                           {"kind": "fable-week", "used": None}]),
                        account("A2", "cooling", available_at=NOW + 600), title="Claude · 我的 VPS",
                        pool={"usable": 1, "total": 2, "next_available_at": None}, link="https://ops.example.com/x",
                        cost=12.5)
        kept = quota.parse(data)
        self.assertNotIn("cost", kept)
        first, second = kept["accounts"]
        self.assertNotIn("extra", first)
        self.assertEqual(first["windows"][0], {"kind": "5h", "label": "5 小时", "used": 0.42, "resets_at": NOW + 7200,
                                               "ceiling": 0.9})
        self.assertEqual(first["windows"][1], {"kind": "other", "used": None})  # Not read: never 0.
        self.assertEqual((first["available_at"], second["available_at"]), (None, NOW + 600))
        self.assertEqual((kept["title"], kept["link"], kept["pool"]["usable"]), ("Claude · 我的 VPS",
                                                                                  "https://ops.example.com/x", 1))
        for bad, why in ((snapshot(account(state="busy")), "state"), ({**snapshot(), "schema": 2}, "schema"),
                         (snapshot(account(windows=[{"kind": "5h"}])), "used"), ({**snapshot(), "ttl": 0}, "ttl"),
                         (snapshot(*[account(f"A{n}") for n in range(51)]), "50"),
                         (snapshot(account(alias="someone@example.com")), "alias"), ([], "对象")):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                quota.parse(bad)
        self.assertNotIn("link", quota.parse(snapshot(link="http://plain.example.com")))

    def test_no_mail_address_path_or_key_leaves_a_source_in_its_words(self):
        for text in ("主号 someone@example.com", "号 https://gw.example.com/a", "/absolute/someone/.agy-accounts/b",
                     "192.0.2.12:8443", "sk-ant-api03-abcdefgh", "A" * 30):
            with self.subTest(text=text):
                self.assertNotRegex(quota.scrub(text, 40), r"@|://|/absolute|192\.0|sk-ant|AAAA")
        self.assertEqual(quota.scrub("  主号\n副号  ", 20), "主号 副号")
        self.assertEqual(quota.scrub("Fable 周额度很长很长很长", 6), "Fable")

    def test_freshness_pool_and_a_pool_no_one_can_use_by_itself(self):
        kept = quota.parse(snapshot(account("A1", "spent", available_at=NOW + 900),
                                    account("A2", "cooling", available_at=NOW + 300), account("A3", "login_required")))
        self.assertEqual([quota.freshness(kept, NOW + age) for age in (60, 900, 1300)], ["fresh", "stale", "old"])
        self.assertEqual(quota.freshness(None), "none")
        self.assertEqual(quota.pool(kept), {"usable": 0, "total": 3, "next_available_at": NOW + 300})
        self.assertFalse(quota.none_signed_in(kept))
        signed_out = quota.parse(snapshot(account("A1", "login_required"), account("A2", "expired")))
        self.assertTrue(quota.none_signed_in(signed_out))
        self.assertEqual(quota.pool(signed_out)["next_available_at"], None)
        self.assertFalse(quota.none_signed_in(quota.parse(snapshot())))

    def test_a_failed_read_keeps_the_last_snapshot_and_says_why(self):
        with tempfile.TemporaryDirectory() as folder:
            store = quota.Store(folder)
            store.saved("vps", "command", quota.parse(snapshot(account())), title="我的 VPS", now=NOW)
            entry = store.failed("vps", "command", "命令退出码 2 at /absolute/someone/bin/x", now=NOW + 60)
            self.assertEqual((entry["snapshot"]["accounts"][0]["alias"], entry["ok_at"], entry["read_at"]),
                             ("A1", NOW, NOW + 60))
            self.assertNotIn("/absolute", entry["error"])
            self.assertEqual([e["source"] for e in store.entries()], ["vps"])
            self.assertEqual(stat.S_IMODE((Path(folder) / "quota").stat().st_mode), 0o700)
            with self.assertRaises(ValueError):
                store.path("../x")
            store.forget("vps")
            self.assertEqual(store.entries(), [])


class Sources(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "home").mkdir()
        patcher = mock.patch.dict(os.environ, {"COVEY_DATA_HOME": str(self.root), "HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR",
                     quota_sources.SWITCH):
            os.environ.pop(name, None)

    def script(self, body):
        path = self.root / "quota-snapshot.py"
        path.write_text(f"import json, sys, time\n{body}\n")
        return [sys.executable, "-I", str(path)]

    def write(self, rows, mode=0o600):
        path = self.root / quota_sources.CONFIG_FILE
        path.write_text(json.dumps({"sources": rows}))
        path.chmod(mode)
        return path

    def plugin(self, *rows, fail=False):
        """A stand-in extension whose quota_sources hook gives ``rows``."""
        def sources(environ):
            if fail:
                raise RuntimeError("broken")
            return list(rows)
        return mock.patch("covey.core.extensions.hooks", return_value=[("acme_ext", SimpleNamespace(sources=sources))])

    def test_an_extensions_sources_are_read_like_the_cores_and_only_at_safe_addresses(self):
        for url, ok in (("https://gw.example.com/covey/quota", True), ("http://127.0.0.1:13425/covey/quota", True),
                        ("http://[::1]:9/q", True), ("http://gw.example.com/covey/quota", False),
                        ("https://user:pw@example.com/q", False), ("", False)):
            self.assertEqual(quota_sources.address_ok(url), ok, url)
        good = quota_sources.Source("gateway-acme", "gateway", url="https://gw.example.com/covey/quota", key="fake-key",
                                    claude=True)
        bad = quota_sources.Source("gateway-plain", "gateway", url="http://gw.example.com/covey/quota", key="fake")
        with self.plugin(good, bad, quota_sources.Source("agy-x", "command", argv=("/bin/true",))):
            found, problem = quota_sources.sources()
            self.assertEqual([s.id for s in found], ["gateway-acme"])
            self.assertIn("acme_ext", problem)
            self.assertEqual(quota_sources.claude_source(), good)
        self.assertNotIn("fake-key", repr(good))
        with self.plugin(fail=True):
            self.assertEqual(quota_sources.sources(), ([], "acme_ext: RuntimeError"))
            self.assertIsNone(quota_sources.claude_source())
        os.environ[quota_sources.SWITCH] = "0"
        with self.plugin(good):
            self.assertEqual(quota_sources.sources(), ([], ""))
            self.assertIsNone(quota_sources.claude_source())

    def test_the_gateway_gets_the_key_in_headers_no_redirect_and_a_404_is_no_quota(self):
        source = quota_sources.Source("gateway-x", "gateway", url="https://gw.example.com/covey/quota", key="fake-key",
                                      timeout=5)
        sent = []

        class Answer:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return self.body[:limit]

        def opener(body=None, error=None):
            def open_(request, timeout):
                sent.append((request.full_url, dict(request.header_items()), timeout))
                if error:
                    raise error
                return Answer(body)
            return SimpleNamespace(open=open_)

        body = json.dumps(snapshot(account())).encode()
        with mock.patch("urllib.request.build_opener", return_value=opener(body)) as build:
            self.assertEqual(quota_sources.read(source)["accounts"][0]["alias"], "A1")
        handler = build.call_args.args[0]
        url, headers, timeout = sent[0]
        self.assertEqual((url, headers["Authorization"], timeout), ("https://gw.example.com/covey/quota", "Bearer fake-key", 5))
        self.assertNotIn("X-api-key", headers)
        self.assertIsNone(handler().redirect_request(None, None, 302, "", {}, "https://elsewhere"))
        missing = urllib.error.HTTPError(source.url, 404, "Not Found", {}, None)
        with mock.patch("urllib.request.build_opener", return_value=opener(error=missing)), \
                self.assertRaises(quota_sources.SourceError) as caught:
            quota_sources.read(source)
        self.assertTrue(caught.exception.absent)
        with mock.patch("urllib.request.build_opener", return_value=opener(b"x" * (quota.MAX_BYTES + 5))), \
                self.assertRaisesRegex(quota_sources.SourceError, "KB"):
            quota_sources.read(source)
        store = quota.Store(self.root / "state")
        with mock.patch("urllib.request.build_opener", return_value=opener(error=missing)):
            entry = quota_sources.take(store, source, NOW)
        self.assertTrue(entry["absent"])
        self.assertFalse(quota_sources.due(entry, source, NOW + 3600))  # Asked again hours later, nothing said.
        self.assertTrue(quota_sources.due(entry, source, NOW + quota_sources.ABSENT_SECONDS))

    def test_a_command_source_and_its_failures(self):
        good = self.script(f"print(json.dumps({json.dumps(snapshot(account('V1')))}))")
        self.write([{"id": "vps", "kind": "command", "argv": good, "interval": 120, "title": "我的 VPS"}])
        found, problem = quota_sources.sources()
        self.assertEqual(([s.id for s in found], problem), (["vps"], ""))
        state = self.root / "state"
        self.assertEqual(quota_sources.refresh(state, found, now=NOW), ["vps"])
        self.assertEqual(quota_sources.refresh(state, found, now=NOW + 60), [])  # Not due yet.
        entry = quota.Store(state).read("vps")
        self.assertEqual((entry["title"], entry["snapshot"]["accounts"][0]["alias"]), ("我的 VPS", "V1"))
        for body, why in (("sys.exit(3)", "退出码 3"), ("print('not json')", "JSON"), ("time.sleep(30)", "秒没有结束"),
                          ("print(json.dumps({'schema': 1}))", "不是额度快照")):
            with self.subTest(why=why):
                source = quota_sources.Source("vps", "command", argv=tuple(self.script(body)), timeout=2)
                started = time.monotonic()
                with self.assertRaisesRegex(quota_sources.SourceError, why):
                    quota_sources.read(source)
                self.assertLess(time.monotonic() - started, 15)
        failed = quota_sources.take(quota.Store(state), quota_sources.Source("vps", "command",
                                                                             argv=tuple(self.script("sys.exit(3)"))))
        self.assertEqual((failed["error"], failed["snapshot"]["accounts"][0]["alias"]), ("命令退出码 3", "V1"))

    def test_a_configuration_others_may_write_or_that_is_wrong_is_not_used(self):
        good = self.script("pass")
        self.write([{"id": "vps", "kind": "command", "argv": good}], mode=0o666)
        found, problem = quota_sources.sources()
        self.assertEqual(found, [])
        self.assertIn("别人也能写", problem)
        for rows, why in (([{"id": "agy-x", "kind": "command", "argv": good}], "id"),
                          ([{"id": "a", "kind": "command", "argv": ["relative"]}], "argv"),
                          ([{"id": "a", "kind": "url", "url": "http://x"}], "url"),
                          ([{"id": "a", "kind": "command", "argv": good, "interval": 5}], "interval"),
                          ([{"id": "a", "kind": "shell"}], "kind")):
            with self.subTest(why=why):
                self.write(rows)
                with self.assertRaisesRegex(ValueError, why):
                    quota_sources.configured()
        self.write([{"id": "web", "kind": "url", "url": "https://q.example.com/s", "token_file": "/absolute/token"}])
        self.assertEqual(quota_sources.configured()[0].token_file, "/absolute/token")

    def test_the_thread_starts_only_with_something_to_read(self):
        self.assertFalse(quota_watch.wanted())
        self.assertEqual(quota_watch.QuotaWatch(SimpleNamespace(log=print)).threads(), ())
        gateway = quota_sources.Source("gateway-acme", "gateway", url="https://gw.example.com/covey/quota", key="fake")
        with self.plugin(gateway):
            self.assertTrue(quota_watch.wanted())
            os.environ[quota_sources.SWITCH] = "off"
            self.assertFalse(quota_watch.wanted())
        del os.environ[quota_sources.SWITCH]
        self.write([{"id": "vps", "kind": "command", "argv": self.script(
            f"print(json.dumps({json.dumps(snapshot(account('V1')))}))")}])
        logged = []
        watch = quota_watch.QuotaWatch(SimpleNamespace(log=logged.append))
        ctx = SimpleNamespace(state_dir=self.root / "state")
        self.assertEqual(watch.tick(ctx), ["vps"])
        (self.root / quota_sources.CONFIG_FILE).chmod(0o666)
        self.assertEqual(watch.tick(ctx), [])
        self.assertEqual(len(logged), 1)
        # Not given for a look or two (a file on a slow disk, a hook that raised): its snapshot stays on the page (T974) ...
        self.assertEqual([e["source"] for e in quota.Store(ctx.state_dir).entries()], ["vps"])
        # ... and a source no longer there goes from the page once it was not read for FORGET_SECONDS.
        quota_sources.forget_gone(ctx.state_dir, [], now=time.time() + quota_sources.FORGET_SECONDS)
        self.assertEqual(quota.Store(ctx.state_dir).entries(), [])

    def test_a_gateway_that_answered_once_and_then_404s_keeps_its_snapshot_and_says_why(self):
        """T974: the gateway's address answers 404 while its own readings are out of date; that hid the source from the page
        for ABSENT_SECONDS (6 hours) as if it had no quota address at all."""
        source = quota_sources.Source("gateway-x", "gateway", url="https://gw.example.com/covey/quota", key="fake-key")
        store = quota.Store(self.root / "state")
        missing = quota_sources.SourceError("HTTP 404", absent=True)
        with mock.patch.object(quota_sources, "read", return_value=quota.parse(snapshot(account()))):
            self.assertTrue(quota_sources.take(store, source, NOW)["ok_at"])
        with mock.patch.object(quota_sources, "read", side_effect=missing):
            entry = quota_sources.take(store, source, NOW + 300)
        self.assertEqual((entry["absent"], entry["error"], entry["ok_at"]), (False, "HTTP 404", NOW))
        self.assertEqual(entry["snapshot"]["accounts"][0]["alias"], "A1")  # The last one stays.
        self.assertTrue(quota_sources.due(entry, source, NOW + 300 + source.interval))  # Asked again at its interval, not hours on.
        # A record an older Covey wrote (absent, with a snapshot it had read before) is asked again then too.
        self.assertTrue(quota_sources.due({**entry, "absent": True}, source, NOW + 300 + source.interval))
        # A gateway that never answered is still one without the address.
        fresh = quota.Store(self.root / "other")
        with mock.patch.object(quota_sources, "read", side_effect=missing):
            self.assertTrue(quota_sources.take(fresh, source, NOW)["absent"])

    def test_the_log_says_when_a_source_begins_to_fail_and_when_it_reads_again(self):
        logged = []
        watch = quota_watch.QuotaWatch(SimpleNamespace(log=logged.append))
        state = self.root / "state"
        store = quota.Store(state)
        store.saved("gw", "gateway", quota.parse(snapshot(account())), now=NOW)
        watch.note(state, ["gw"])
        store.failed("gw", "gateway", "连不上：URLError", now=NOW + 60)
        watch.note(state, ["gw"])
        watch.note(state, ["gw"])  # Still failing the same way: said once.
        store.saved("gw", "gateway", quota.parse(snapshot(account())), now=NOW + 120)
        watch.note(state, ["gw"])
        self.assertEqual(logged, ["quota source gw: read failed: 连不上：URLError", "quota source gw: read again"])


class Classifying(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = codex_context(Path(self.dir.name), "stream-json")

    def finish(self, text):
        row = {"type": "assistant", "session_id": SID, "uuid": "q", "error": "rate_limit", "isApiErrorMessage": True,
               "message": {"id": "q", "content": [{"type": "text", "text": text}]}}
        state = ClaudeReplyState(self.ctx, None, run_id=RUN)
        for each in ({"type": "system", "subtype": "init", "session_id": SID}, row,
                     {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"}):
            state.accept(each)
        return state.finish(1, cleanup=True)

    def test_a_gateways_spent_pool_on_a_429_is_a_spent_quota_with_when_it_is_back(self):
        started = time.time()
        answer = self.finish(GATEWAY_429)
        self.assertEqual(answer.outcome.error_code, "quota_exhausted")
        self.assertAlmostEqual(answer.limit["reset_at"], started + 9 * 3600 + 50 * 60, delta=30)
        mixed = self.finish("API Error: Request rejected (429) · wait for quota to reset or use an account with "
                            "available quota")
        self.assertEqual((mixed.outcome.error_code, mixed.limit.get("reset_at")), ("quota_exhausted", None))
        passing = self.finish("API Error: Request rejected (429) · Rate limited, try again in 20s")
        self.assertEqual(passing.outcome.error_code, "rate_limited")
        row = {"type": "result", "subtype": "error_during_execution", "is_error": True, "api_error_status": 429,
               "result": GATEWAY_429}
        self.assertEqual(result_failure(row), ("failed", "quota_exhausted"))
        self.assertEqual(result_failure({**row, "result": "Too many requests"}), ("failed", "rate_limited"))
        self.assertEqual(limit_reset.relative_reset("back in 2d 3h", NOW), NOW + 2 * 86400 + 3 * 3600)
        self.assertIsNone(limit_reset.relative_reset("in a moment", NOW))


class Retrying(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = SimpleNamespace(state_dir=Path(self.dir.name))
        self.harness = SimpleNamespace(backend=SimpleNamespace(capabilities=lambda ctx: HarnessCapabilities(
            rate_limit_retry=True, fault_retry=True, incomplete_retry=True)))

    def plan(self, reset=None, **fields):
        job = SimpleNamespace(**{"message_id": "m", "job_id": 1, "topic_id": "t", "timeout": 0, "tool_trace": None,
                                 "cancel_event": SimpleNamespace(is_set=lambda: False), "timings": {}, "auto_retry": 0,
                                 "auto_continue": 0, "auto_resume": 0, "auto_fault": 0, "auto_idle": 0, "auto_quota": 0,
                                 **fields})
        turn = SimpleNamespace(outcome=ExecutionOutcome("failed", "quota_exhausted"), verified_delivery=None,
                               limit={"reset_at": reset} if reset else None)
        with mock.patch.object(auto_retry, "jitter", return_value=0.0):
            return auto_retry.plan(self.harness, self.ctx, job, turn, NOW)

    def test_a_spent_quota_is_retried_at_its_reset_within_six_hours_and_only_then(self):
        planned = self.plan(NOW + 1800)
        self.assertEqual((planned.kind, planned.due_at, planned.reset_at), ("quota_exhausted", NOW + 1860, NOW + 1800))
        self.assertIsNone(self.plan(NOW + 7 * 3600))  # Further off: the card says when.
        self.assertIsNone(self.plan())  # None known: as before, never retried.
        self.assertIsNone(self.plan(NOW + 1800, auto_quota=3))
        self.assertEqual(auto_retry.status_line(NOW + 1860, 1, "quota_exhausted", NOW + 1800),
                         f"模型额度用完，预计 {auto_retry.clock(NOW + 1800)} 恢复，将于 {auto_retry.clock(NOW + 1860)} 自动接着做")
        with mock.patch.dict(os.environ, {auto_retry.SWITCH: "0"}):
            self.assertIsNone(self.plan(NOW + 1800))

    def test_its_card_says_when_it_is_back_and_that_it_goes_on_then(self):
        record = {"status": "failed", "failure_code": "quota_exhausted", "topic_id": "t",
                  "failure_detail": "2 个号的 Gemini 额度都用完了或要重新登录，最早 17:10 恢复",
                  **auto_retry.schedule_fields(auto_retry.Planned(NOW + 1860, 1, "quota_exhausted", NOW + 1800))}
        title = failure.failure_view(record, NOW)["title"]
        self.assertTrue(title.startswith("2 个号的 Gemini 额度都用完了或要重新登录，最早 17:10 恢复；额度预计 "), title)
        self.assertIn("会自动接着做", title)
        later = failure.failure_view({**record, "auto_retry_state": "cancelled"}, NOW)["title"]
        self.assertTrue(later.endswith("最早 17:10 恢复；可以换个模型接着做"), later)
        plain = failure.failure_view({"status": "failed", "failure_code": "quota_exhausted", "topic_id": "t"}, NOW)
        self.assertEqual(plain["title"], failure.OWN["quota_exhausted"][0])


class GatewayFailure(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ctx = SimpleNamespace(state_dir=Path(temp.name), model="", device="")
        self.source = quota_sources.Source("gateway-test", "gateway", url="https://gw.example.com/covey/quota",
                                           key="fake", claude=True)
        for patcher in (mock.patch.object(quota_sources, "claude_source", return_value=self.source),
                        mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(quota_sources.SWITCH, None)

    def keep(self, *accounts, at=None):
        quota.Store(self.ctx.state_dir).saved(self.source.id, "gateway", quota.parse(snapshot(*accounts)),
                                              now=time.time() if at is None else at)

    def failed(self, code, limit=None, harness="claude"):
        outcome = ExecutionOutcome("failed", code)
        result = RunResult(outcome.summary(), SID, True, None, outcome, limit=limit)
        return quota_failure.refine(harness, self.ctx, result)

    def test_a_503_of_a_gateway_whose_accounts_all_need_a_sign_in_says_so_and_is_not_retried(self):
        self.keep(account("A1", "login_required"), account("A2", "expired"))
        found = self.failed("server_fault")
        self.assertEqual((found.outcome.error_code, found.failure_detail, found.limit),
                         ("authentication_error", quota_failure.NO_ACCOUNT, None))

    def test_a_spent_pool_is_a_spent_quota_with_the_sources_time_whatever_the_status(self):
        back = time.time() + 3600
        self.keep(account("A1", "cooling", available_at=back), account("A2", "login_required"))
        for code in ("rate_limited", "server_fault", "quota_exhausted"):
            with self.subTest(code=code):
                found = self.failed(code, limit={"evidence": []})
                self.assertEqual((found.outcome.error_code, found.limit["reset_at"]), ("quota_exhausted", back))
                self.assertTrue(found.failure_detail.startswith("网关里的号额度都用完了，最早 "), found.failure_detail)

    def test_with_an_account_usable_or_no_snapshot_the_result_stands_but_its_words_say_when(self):
        self.keep(account("A1"), account("A2", "cooling", available_at=time.time() + 60))
        plain = self.failed("server_fault")
        self.assertEqual((plain.outcome.error_code, plain.failure_detail), ("server_fault", ""))
        self.assertEqual(self.failed("rate_limited", harness="agy").outcome.error_code, "rate_limited")
        quota.Store(self.ctx.state_dir).forget(self.source.id)
        with mock.patch.object(quota_sources, "read", side_effect=quota_sources.SourceError("HTTP 404", absent=True)):
            back = time.time() + 7200
            said = self.failed("quota_exhausted", limit={"reset_at": back})
            self.assertTrue(said.failure_detail.startswith("模型服务的额度用完了，最早 "), said.failure_detail)
            self.assertEqual(self.failed("server_fault").outcome.error_code, "server_fault")
        with mock.patch.dict(os.environ, {quota_sources.SWITCH: "0"}):
            self.keep(account("A1", "login_required"))
            self.assertEqual(self.failed("server_fault").outcome.error_code, "server_fault")

    def test_a_snapshot_older_than_half_a_minute_is_read_again_first(self):
        self.keep(account("A1"), at=time.time() - 120)
        fresh = quota.parse(snapshot(account("A1", "login_required")))
        with mock.patch.object(quota_sources, "read", return_value=fresh) as read:
            found = self.failed("server_fault")
        read.assert_called_once()
        self.assertEqual(found.outcome.error_code, "authentication_error")


class Switching(unittest.TestCase):
    def test_the_cores_agy_accounts_and_an_extensions_hook_may_not_both_switch(self):
        loaded = [SimpleNamespace(package="acme_ext", hooks={"agy_turn": "acme_ext.agy"})]
        with mock.patch("covey.backends.agy.accounts.configured", return_value=True):
            self.assertIn("acme_ext", plugins.agy_switching(loaded))
            self.assertEqual(plugins.agy_switching([SimpleNamespace(package="x", hooks={})]), "")
        with mock.patch("covey.backends.agy.accounts.configured", return_value=False):
            self.assertEqual(plugins.agy_switching(loaded), "")

    def test_an_agy_as_left_behind_is_in_what_the_person_still_has_to_add_with_the_accounts_on(self):
        from covey.service import doctor
        with tempfile.TemporaryDirectory() as folder:
            manifest = {"environment": {"COVEY_EVENT_TRANSPORT": "sdk"}, "state_dir": folder}
            with mock.patch("covey.backends.agy.account_homes.link_problem", return_value="/x/agy-as 指向的文件已经没有了"):
                with mock.patch("covey.backends.agy.accounts.configured", return_value=True):
                    self.assertEqual(doctor.todo_row(manifest, None),
                                     ["agy-as 要重装（/x/agy-as 指向的文件已经没有了）：python3 -B bin/agy_as.py --install"])
                with mock.patch("covey.backends.agy.accounts.configured", return_value=False):
                    self.assertEqual(doctor.todo_row(manifest, None), [])


if __name__ == "__main__":
    unittest.main()
