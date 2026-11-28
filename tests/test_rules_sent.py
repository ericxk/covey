"""W1-E (WAVE12 C9, the owner's call of 11-28): the bridge rules at the system level of each core Harness
(app/rules_sent.py), and the pieces around them: what a resumed turn's message tells of them (nothing, or the turn's whole
when the session has an older version), the record kept only after a turn that ended well, each Harness's channel (Claude
Code's --append-system-prompt, Codex's developerInstructions, dsh's patch, agy's GEMINI.md), the butler's and a lead's
rules and pack, the compactions a turn's record counts, 代发's rule out of the owner's words, the memory command that
outlives a deploy, and a backend whose notes fail."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job, quiet
from support.fake_backends import RecordingBackend
from support.records import write_job, write_topic
from support.world import harness_app

from covey.app import owner_send, project_lead, rules_sent
from covey.app.prompts import butler_message, butler_rules
from covey.app.rules_sent import OVERRIDE, SYSTEM
from covey.app.sessions import mark_system_rules
from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.backends.codex.bridge import CODEX_SPEC
from covey.core.checkpoint import CHECKPOINT_RULES
from covey.core.harness import LEAD, LOOK, LOOK_ROOTS, TOOL_LIMIT, RunResult
from covey.core.jobs import job_path, read_job_record
from covey.core.protocol import COMPACT_STEP
from covey.core.session_guard import finish_native_session
from covey.core.state import write_json
from covey.core.trace import ToolTrace

TOPIC = "a" * 32
SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        self.ctx = make_context(self.root, topic_id=TOPIC, history_path=self.state / "topics" / TOPIC / "history.jsonl",
                                model="m")
        write_topic(self.ctx, {"binding_revision": 1})
        patcher = mock.patch.dict(os.environ, {rules_sent.ENV: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, number=5, **fields):
        return make_job(job_id=number, message_id=f"om{number}", topic_id=TOPIC, chat_id="oc", result_backend="claude",
                        **fields)

    def recorded(self, number=1, sid=SID, current="v1", refresh="compaction", reason="new_session", mode=SYSTEM):
        write_job(self.state, f"om{number}", job_id=number, topic_id=TOPIC, chat_id="oc", status="done")
        rules_sent.record(self.ctx, self.job(number), sid, rules_sent.Plan(mode, reason, current, refresh))

    def decide(self, sid=SID, current="v1", refresh="compaction", previous=None, before=None, ctx=None):
        return rules_sent.decide(ctx or self.ctx, self.job(), sid, current=current, refresh=refresh,
                                 previous={"status": "done"} if previous is None else previous, before=before)

    def entry(self, sid=SID):
        return rules_sent.entry(self.ctx, self.job(), sid)


class DecideTest(Case):
    def test_a_session_with_the_turns_version_gets_nothing_and_an_older_one_the_turns_whole(self):
        self.assertEqual(self.decide(sid=None), (SYSTEM, "new_session", True))
        self.assertEqual(self.decide()[:2], (OVERRIDE, "unknown"))  # A session the Bridge has no record of.
        self.recorded()
        self.assertEqual(self.decide(), (SYSTEM, "", True))
        self.assertEqual(self.decide(current="v2")[:2], (OVERRIDE, "changed"))
        self.assertEqual(self.decide(sid=OTHER)[:2], (OVERRIDE, "unknown"))

    def test_once_told_it_holds_while_the_conversation_is_intact(self):
        self.recorded()
        self.recorded(number=2, current="v2", reason="changed", mode=OVERRIDE)
        self.assertEqual(self.decide(current="v2"), (SYSTEM, "told", True))
        checkpoint = self.root / "cp.json"
        write_json(checkpoint, {"phase": "x", "rules_missing": True})
        compact = make_context(self.root, topic_id=TOPIC, history_path=self.ctx.history_path, model="m",
                               options={"compact_first": True})
        for previous, before, ctx, why in (
                ({"status": "done", "compactions": 1}, None, None, "compacted"),
                (None, None, compact, "compacted"),
                ({"status": "done", "context_tokens": 69_000}, {"context_tokens": 100_000}, None, "context_dropped"),
                ({"status": "failed"}, None, None, "previous_failed"),
                ({"status": "done", "checkpoint_path": str(checkpoint)}, None, None, "rules_missing")):
            with self.subTest(why=why):
                self.assertEqual(self.decide(current="v2", previous=previous, before=before, ctx=ctx), (OVERRIDE, why, False))
        self.assertEqual(self.decide(current="v2", previous={"status": "done", "context_tokens": 71_000},
                                     before={"context_tokens": 100_000})[0], SYSTEM)
        for number in range(rules_sent.N_FULL):
            self.recorded(number=10 + number, current="v2", reason="told")
        self.assertEqual(self.decide(current="v2"), (OVERRIDE, "every_n", False))

    def test_a_harness_that_reads_them_again_never_needs_the_message(self):
        self.assertEqual(self.decide(refresh=rules_sent.REREAD)[:2], (SYSTEM, "reread"))
        self.assertEqual(self.decide(refresh=rules_sent.REREAD, previous={"status": "done", "compactions": 2}),
                         (SYSTEM, "reread", False))  # Not intact all the same: a lead's pack goes whole.

    def test_a_switch_of_binding_or_model_counts_as_no_record_and_a_fork_reads_its_source(self):
        self.recorded()
        write_topic(self.ctx, {"binding_revision": 2})
        self.assertEqual(self.decide()[:2], (OVERRIDE, "unknown"))
        write_topic(self.ctx, {"binding_revision": 1})
        other_model = make_context(self.root, topic_id=TOPIC, history_path=self.ctx.history_path, model="n")
        self.assertEqual(self.decide(ctx=other_model)[:2], (OVERRIDE, "unknown"))
        fork = make_context(self.root, topic_id=TOPIC, history_path=self.ctx.history_path, model="m",
                            options={"fork_session": SID})
        self.assertEqual(self.decide(sid=None, ctx=fork), (SYSTEM, "", True))

    def test_the_switch(self):
        self.assertTrue(rules_sent.enabled({}))
        for off in ("0", "off", "false", "no"):
            self.assertFalse(rules_sent.enabled({rules_sent.ENV: off}))
        self.assertEqual((rules_sent.N_FULL, rules_sent.REBUILD_TURNS_FALLBACK), (8, 12))


class RecordTest(Case):
    def test_a_new_session_or_one_rendered_again_by_a_compaction_has_the_turns_version(self):
        self.recorded(current="v1")
        self.assertEqual((self.entry()["system"], self.entry()["override"], self.entry()["turns"]), ("v1", "", 0))
        self.recorded(number=2, current="v2", reason="changed", mode=OVERRIDE)
        self.assertEqual((self.entry()["system"], self.entry()["override"], self.entry()["turns"]), ("v1", "v2", 0))
        self.recorded(number=3, current="v2", reason="told")
        self.assertEqual(self.entry()["turns"], 1)
        # A compaction on Claude Code renders the system prompt again from the invocation's: the turn's version.
        rules_sent.record(self.ctx, self.job(4), SID, rules_sent.Plan(SYSTEM, "told", "v2", "compaction"), compacted=True)
        self.assertEqual((self.entry()["system"], self.entry()["override"]), ("v2", ""))
        # Codex keeps its thread's start: a compaction changes nothing there.
        self.recorded(number=5, sid=OTHER, current="v1", refresh="")
        rules_sent.record(self.ctx, self.job(6), OTHER, rules_sent.Plan(OVERRIDE, "changed", "v3", ""), compacted=True)
        self.assertEqual((self.entry(OTHER)["system"], self.entry(OTHER)["override"]), ("v1", "v3"))

    def test_a_harness_that_reads_them_again_keeps_no_record(self):
        self.recorded(refresh=rules_sent.REREAD)
        self.assertEqual(self.entry(), {})

    def test_only_a_turn_that_ended_well_with_a_plan_is_recorded_and_its_compactions_are_read_from_its_record(self):
        job = self.job(7)
        job.session_id = SID
        job.rules_plan = rules_sent.Plan(SYSTEM, "new_session", "v1", "compaction")
        for status in ("failed", "interrupted", "cancelled"):
            rules_sent.settle(self.ctx, job, status)
        self.assertEqual(self.entry(), {})
        plain = self.job(8)
        plain.session_id = SID
        rules_sent.settle(self.ctx, plain, "done")  # No plan: its rules were in its message.
        self.assertEqual(self.entry(), {})
        rules_sent.settle(self.ctx, job, "waiting_user")
        self.assertEqual(self.entry()["system"], "v1")
        later = self.job(9)
        later.session_id = SID
        later.rules_plan = rules_sent.Plan(OVERRIDE, "changed", "v2", "compaction")
        write_job(self.state, "om9", job_id=9, topic_id=TOPIC, chat_id="oc", status="done", compactions=1)
        rules_sent.settle(self.ctx, later, "done")
        self.assertEqual(self.entry()["system"], "v2")

    def test_a_forks_record_starts_from_its_source_and_a_rewind_forgets(self):
        self.recorded()
        fork = make_context(self.root, topic_id=TOPIC, history_path=self.ctx.history_path, model="m",
                            options={"fork_session": SID})
        rules_sent.record(fork, self.job(2), OTHER, rules_sent.Plan(SYSTEM, "", "v1", "compaction"))
        self.assertEqual((self.entry(OTHER)["system"], self.entry(OTHER)["turns"]), ("v1", 1))
        rules_sent.forget(self.state, TOPIC)
        self.assertEqual((self.entry(), self.entry(OTHER)), ({}, {}))


class SystemBackend(RecordingBackend):
    """RecordingBackend that keeps each request (its instructions too) and declares a system channel."""

    def __init__(self, refresh="compaction"):
        super().__init__()
        self.requests = []
        self.declare(system_instructions=True, system_refresh=refresh)

    def run(self, request):
        self.requests.append(request)
        return RunResult("完成", request.session_id or str(uuid.uuid4()))


class RunsTest(Case):
    def app(self, refresh="compaction", spec=CLAUDE_SPEC):
        backend = SystemBackend(refresh)
        app = harness_app(spec, backend, ClaudeSessionStore())
        self.logs = quiet(app)
        return app, backend

    def turn(self, app, backend, number, sid, **fields):
        write_job(self.state, f"om{number}", job_id=number, topic_id=TOPIC, chat_id="oc", status="running",
                  backend=app.runs.harness.spec.name)
        job = self.job(number, user_text="接着做", **fields)
        job.result_backend = app.runs.harness.spec.name
        job.checkpoint_path = str(self.root / f"cp{number}.json")
        app.runs.generate_reply_for_worker(self.ctx, job, sid, None)
        for claimed in job.native_session_claims:  # Settled as the worker does, so the next turn may resume it.
            finish_native_session(self.ctx, app.runs.harness.spec.name, claimed, job.bridge_run_id, True)
        return job, backend.requests[-1]

    def end(self, job, sid=SID, status="done", **fields):
        write_job(self.state, job.message_id, status=status, **fields)
        job.session_id = sid
        rules_sent.settle(self.ctx, job, status)

    def test_the_rules_go_at_the_system_level_and_a_resumed_turns_message_has_none(self):
        app, backend = self.app()
        job, request = self.turn(app, backend, 1, None)
        self.assertIn(CHECKPOINT_RULES, request.instructions)
        self.assertNotIn(CHECKPOINT_RULES, request.prompt)
        self.assertNotIn("你是 Claude Code，通过飞书回复用户。", request.prompt)
        self.assertEqual((job.rules_plan.mode, job.rules_plan.reason), (SYSTEM, "new_session"))
        self.assertEqual(read_job_record(job_path(self.state, "om1"))["rules_sent"], SYSTEM)
        mark_system_rules(self.ctx, "claude", SID)  # What the run does when it sees the new session.
        self.end(job)
        job, request = self.turn(app, backend, 2, SID)
        self.assertIn(CHECKPOINT_RULES, request.instructions)  # On every invocation: a compaction renders them again.
        self.assertNotIn(CHECKPOINT_RULES, request.prompt)
        self.assertNotIn(rules_sent.OVERRIDE_HEAD[:10], request.prompt)
        self.assertIn("本任务检查点文件：", request.prompt)  # What changes each turn stays in the message.
        self.assertEqual(job.rules_plan.mode, SYSTEM)

    def test_a_session_with_an_older_version_is_told_the_turns_whole_once(self):
        app, backend = self.app()
        mark_system_rules(self.ctx, "claude", SID)
        self.recorded(current="old-version")
        job, request = self.turn(app, backend, 2, SID)
        self.assertEqual((job.rules_plan.mode, job.rules_plan.reason), (OVERRIDE, "changed"))
        self.assertIn(rules_sent.OVERRIDE_HEAD.format(version=job.rules_plan.current), request.prompt)
        self.assertIn(CHECKPOINT_RULES, request.prompt)
        self.end(job)
        job, request = self.turn(app, backend, 3, SID)
        self.assertEqual((job.rules_plan.mode, job.rules_plan.reason), (SYSTEM, "told"))
        self.assertNotIn(CHECKPOINT_RULES, request.prompt)
        self.end(job, compactions=1)  # On Claude Code the compaction rendered the system prompt again: the turn's.
        job, request = self.turn(app, backend, 4, SID)
        self.assertEqual((job.rules_plan.mode, job.rules_plan.reason), (SYSTEM, ""))

    def test_a_session_from_before_gets_its_rules_in_each_resumed_message_as_before(self):
        app, backend = self.app()
        job, request = self.turn(app, backend, 2, SID)  # No mark: it never had them at the system level.
        self.assertIsNone(job.rules_plan)
        self.assertIn(CHECKPOINT_RULES, request.prompt)
        self.assertEqual(read_job_record(job_path(self.state, "om2"))["rules_sent"], rules_sent.FULL)
        reread, backend = self.app(refresh=rules_sent.REREAD)  # dsh, agy: theirs are read again at every turn.
        job, request = self.turn(reread, backend, 3, SID)
        self.assertEqual((job.rules_plan.mode, job.rules_plan.reason), (SYSTEM, "reread"))
        self.assertNotIn(CHECKPOINT_RULES, request.prompt)

    def test_with_the_switch_off_or_on_a_harness_without_a_channel_the_rules_are_in_the_message(self):
        app, backend = self.app()
        with mock.patch.dict(os.environ, {rules_sent.ENV: "0"}):
            job, request = self.turn(app, backend, 1, None)
        self.assertEqual(request.instructions, "")
        self.assertTrue(request.prompt.startswith("你是 Claude Code，通过飞书回复用户。"))
        self.assertIsNone(job.rules_plan)
        backend.declare()
        job, request = self.turn(app, backend, 2, None)
        self.assertEqual(request.instructions, "")
        self.assertIn(CHECKPOINT_RULES, request.prompt)

    def test_what_changes_as_other_topics_go_on_is_in_a_new_sessions_message_not_the_system_level(self):
        app, backend = self.app()
        with mock.patch("covey.app.workspace_gists.block", return_value="GISTS-OF-THE-WORKSPACE"), \
                mock.patch("covey.app.project_context.library_block", return_value="LIBRARY-INDEX"):
            job, request = self.turn(app, backend, 1, None)
            self.assertTrue(request.prompt.startswith("GISTS-OF-THE-WORKSPACE\n\nLIBRARY-INDEX\n\n"))
            self.assertNotIn("GISTS-OF-THE-WORKSPACE", request.instructions)
            mark_system_rules(self.ctx, "claude", SID)
            job, request = self.turn(app, backend, 2, SID)
            self.assertNotIn("GISTS-OF-THE-WORKSPACE", request.prompt)

    def test_a_backend_whose_notes_fail_gives_none(self):
        app, _ = self.app()
        app.runs.harness.backend.prompt_notes = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertEqual(app.runs.backend_notes(self.ctx), ())
        self.assertTrue(any("prompt notes of claude unavailable: RuntimeError" in line for line in self.logs))


class ChannelTest(unittest.TestCase):
    """Each core Harness's own channel, as measured on 11-28 against a local fake model service."""

    def test_claude_code_appends_them_on_every_invocation(self):
        from covey.backends.claude.argv import stream_argv
        from covey.backends.claude.bridge import ClaudeBackend

        root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, root, True)
        ctx = make_context(root, backend_cmd=["claude"])
        for sid in (None, SID):
            argv = stream_argv(ClaudeBackend(), ctx, sid, instructions="RULES")
            self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "RULES")
        caps = ClaudeBackend().capabilities(ctx)
        self.assertEqual((caps.system_instructions, caps.system_refresh), (True, rules_sent.ON_COMPACTION))

    def test_codex_keeps_the_threads_and_tells_a_turns_own_in_its_input(self):
        from covey.backends.codex import appserver

        lead = SimpleNamespace(options={TOOL_LIMIT: LEAD})
        look = SimpleNamespace(options={TOOL_LIMIT: LOOK, LOOK_ROOTS: {}})
        plain = SimpleNamespace(options={})
        self.assertEqual(appserver.thread_instructions(plain), appserver.CODEX_INSTRUCTIONS)
        self.assertEqual(appserver.thread_instructions(look), appserver.CODEX_INSTRUCTIONS)
        self.assertEqual(appserver.thread_instructions(lead), appserver.ASK_LEAD + appserver.SUBAGENTS)
        self.assertEqual(appserver.turn_instructions(plain), "")
        self.assertEqual(appserver.turn_instructions(lead), "")
        self.assertTrue(appserver.turn_instructions(look).startswith(appserver.TURN_HEAD))
        self.assertIn(appserver.LOOK_NO_WRITE, appserver.turn_instructions(look))

    def test_dsh_puts_them_after_its_persona_with_no_prompt_variables_left(self):
        from covey.backends.dsh.bridge import PERSONA_PREFIX, route_patch

        gateway = SimpleNamespace(name="acme", display="Acme", url=lambda: "http://127.0.0.1:1", key_env="K",
                                  header_dict=lambda: {})
        rows = route_patch(gateway, {}, "m", "", None, "规则：不要留 {{占位}} 和 }} 这样的")
        found = next(row for row in rows if row.get("id") == "system-prompt")
        self.assertEqual(found["config"]["personaPrefix"], PERSONA_PREFIX)
        self.assertNotIn("{{", found["config"]["personaSuffix"])
        self.assertNotIn("}}", found["config"]["personaSuffix"])
        self.assertFalse(any(row.get("id") == "system-prompt" for row in route_patch(gateway, {}, "m", "", None, "")))

    def test_agy_reads_them_from_its_private_homes_gemini_md(self):
        from covey.backends.agy import home as agy_home

        root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, root, True)
        real = root / "home"
        (real / ".gemini" / "config").mkdir(parents=True)
        (real / ".gemini" / "GEMINI.md").write_text("OWNER RULES\n", encoding="utf-8")
        (real / ".gemini" / "config" / "mcp_config.json").write_text("{}", encoding="utf-8")
        (real / ".gemini" / "antigravity").mkdir()
        with agy_home.PrivateHome(root / "state", real, None, {}, key=TOPIC, rules="BRIDGE RULES") as path:
            gemini = path / ".gemini"
            self.assertEqual((gemini / "GEMINI.md").read_text(encoding="utf-8"), "OWNER RULES\n\n\nBRIDGE RULES")
            self.assertFalse((gemini / "GEMINI.md").is_symlink())
            self.assertTrue((gemini / "config" / "mcp_config.json").is_symlink())  # No Bridge tools: the owner's own.
            self.assertTrue((gemini / "antigravity").is_symlink())
        self.assertEqual((real / ".gemini" / "GEMINI.md").read_text(encoding="utf-8"), "OWNER RULES\n")
        long = "".join(f"第 {n} 行规则，写得长一点好超过上限。\n" for n in range(1200))
        first, rest = agy_home.rules_parts(long)
        self.assertLessEqual(len(first.encode()), agy_home.RULES_BYTES)
        self.assertEqual(first + rest, long)
        with agy_home.PrivateHome(root / "state", real, None, {}, key=TOPIC, rules=long) as path:
            self.assertTrue((path / ".gemini" / "config" / "GEMINI.md").read_text(encoding="utf-8").endswith(rest))

    def test_agy_and_dsh_declare_a_channel_read_at_every_turn(self):
        from covey.backends.agy.bridge import AntigravityBackend, Homes
        from covey.backends.dsh.bridge import DshBackend

        root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, root, True)
        ctx = make_context(root)
        for backend in (AntigravityBackend(), DshBackend()):
            caps = backend.capabilities(ctx)
            self.assertEqual((caps.system_instructions, caps.system_refresh), (True, rules_sent.REREAD))
        device = make_context(root, device="gpu1", remote_workdir="/absolute/work")
        self.assertFalse(AntigravityBackend().capabilities(device).system_instructions)
        self.assertEqual(Homes(device, "RULES").rules, "")


class CompactionsTest(unittest.TestCase):
    def test_the_main_tasks_compactions_that_ended_well_count(self):
        trace = ToolTrace()
        for key, status, child in (("c1", "done", None), ("c2", "failed", None), ("c3", "done", "sub")):
            event = {"id": key, "name": "context_compaction", "status": status, "started_at": 1.0, "ended_at": 2.0}
            trace.accept("tool", {**event, **({"child_id": child} if child else {})})
        self.assertEqual(trace.compactions(), 1)
        self.assertEqual(ToolTrace.restore(trace.snapshot()).compactions(), 1)
        self.assertEqual(trace.steps["c1"]["title"], COMPACT_STEP)

    def test_the_job_record_counts_them_as_they_end(self):
        root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, root, True)
        app = harness_app(CLAUDE_SPEC, RecordingBackend(), ClaudeSessionStore())
        quiet(app)
        ctx = make_context(root)
        job = make_job(job_id=1, message_id="om1")
        from covey.core.trace import task_trace
        job.tool_trace = task_trace()
        write_job(ctx.state_dir, "om1", job_id=1, status="running")
        event = {"id": "compact-auto-1", "name": "context_compaction", "started_at": 1.0}
        app.interaction.structured_harness_event(ctx, job, "tool", {**event, "status": "running"}, None)
        self.assertNotIn("compactions", read_job_record(job_path(ctx.state_dir, "om1")))
        app.interaction.structured_harness_event(ctx, job, "tool", {**event, "status": "done", "ended_at": 2.0}, None)
        self.assertEqual(read_job_record(job_path(ctx.state_dir, "om1"))["compactions"], 1)


class OwnerSendNoteTest(unittest.TestCase):
    def note(self, job, waiting=(), ended=()):
        with mock.patch.object(owner_send, "enabled", return_value=True), \
                mock.patch.object(owner_send, "allowed", return_value=""), \
                mock.patch.object(owner_send.records, "open_lines", return_value=list(waiting)), \
                mock.patch.object(owner_send.records, "turn_lines", return_value=list(ended)):
            return owner_send.turn_note(SimpleNamespace(state_dir=Path("/absolute/state"), topic_id=TOPIC), job)

    def test_the_rule_only_with_a_draft_open_or_words_that_may_send(self):
        plain = SimpleNamespace(topic_id=TOPIC, chat_id="oc", user_text="把 README 的错字改了", prompt_text="")
        self.assertEqual(self.note(plain), "")
        self.assertIn(owner_send.records.RULE, self.note(SimpleNamespace(**{**vars(plain), "user_text": "做完通知一下张三"})))
        self.assertIn(owner_send.records.RULE, self.note(plain, waiting=["- 草稿 a（给 张三）：等你确认"]))
        ended = self.note(plain, ended=["- 草稿 a（给 张三）：已发出，11-28 09:00"])
        self.assertNotIn(owner_send.records.RULE, ended)
        self.assertIn("之前的代发草稿：\n- 草稿 a（给 张三）：已发出", ended)

    def test_it_follows_the_checkpoint_note_not_the_owners_words_and_the_history_is_told_apart(self):
        root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, root, True)
        backend = RecordingBackend()
        app = harness_app(CODEX_SPEC, backend, ClaudeSessionStore())
        quiet(app)
        ctx = make_context(root, topic_id=TOPIC)
        answer = "选 B，发给张三"
        history = [{"role": "user", "content": "先前的话", "message_id": "om0"}, {"role": "assistant", "content": "好"},
                   {"role": "user", "content": answer}]  # An answer: the last row, without the job's message id.
        job = make_job(job_id=2, message_id="om2", topic_id=TOPIC, chat_id="oc", user_text=answer, history_snapshot=history)
        job.checkpoint_path = str(root / "cp.json")
        with mock.patch.object(owner_send, "enabled", return_value=True), \
                mock.patch.object(owner_send, "allowed", return_value=""):
            app.runs.generate_reply_for_worker(ctx, job, None, None)
        prompt = backend.calls[-1][1]
        self.assertEqual(prompt.count(answer), 1)  # Not once in 最近的对话 and once more as the latest message.
        self.assertLess(prompt.index("本任务检查点文件："), prompt.index(owner_send.records.RULE))
        self.assertLess(prompt.index(answer), prompt.index("本任务检查点文件："))


class ButlerAndLeadTest(Case):
    def test_the_butlers_rules_go_at_the_head_of_its_message_unless_they_are_at_the_system_level(self):
        self.assertTrue(butler_message("快照", "消息").startswith(butler_rules()))
        self.assertEqual(butler_message("快照", "消息", rules=""), "快照\n\n消息")
        job = self.job()
        self.assertFalse(rules_sent.at_system(job))
        job.rules_plan = rules_sent.Plan(SYSTEM, "", "v1", "compaction", intact=False)
        self.assertTrue(rules_sent.at_system(job) and rules_sent.lost(job))

    def test_a_resumed_lead_pack_says_only_what_changed_in_its_three_parts(self):
        rows = [{"topic_id": "t1", "label": "T1", "title": "做完的", "status": "完成", "said": "交了", "artifacts": [],
                 "live": False, "waiting": False, "ended": True},
                {"topic_id": "t2", "label": "T2", "title": "在做的", "status": "在做", "said": "", "artifacts": [],
                 "live": True, "waiting": False, "ended": False},
                {"topic_id": "t3", "label": "T3", "title": "回执点到的", "status": "完成", "said": "又交了", "artifacts": [],
                 "live": False, "waiting": False, "ended": True}]
        record = {"id": "proj01", "name": "专项", "goal": "目标", "style": {}, "threads": ["t1", "t2", "t3"],
                  "decisions": [{"id": "d1", "at": 1.0, "text": "旧决定"}, {"id": "d2", "at": 2.0, "text": "新决定"}]}
        with mock.patch.object(project_lead, "member_rows", return_value=rows), \
                mock.patch.object(project_lead, "memory_lines", return_value=["- R1 规矩"]), \
                mock.patch.object(project_lead, "inbox_block", return_value="收件箱"):
            keep = {}
            whole = project_lead.state_pack(self.state, record, "k1", full=False, receipts=[], keep=keep)
            self.assertIn("做完的", whole)
            self.assertIn("旧决定", whole)
            sent = {**keep, "decisions": ["d1"]}
            pack = project_lead.state_pack(self.state, record, "k2", full=False, receipts=[{"thread": "t3", "text": "x"}],
                                           sent=sent)
        self.assertNotIn("做完的", pack)
        self.assertIn("在做的", pack)
        self.assertIn("回执点到的", pack)
        self.assertIn(project_lead.TOPICS_LEFT.format(count=1), pack)
        self.assertIn(project_lead.MEMORY_SAME.format(version=keep["memory"]), pack)
        self.assertNotIn("- R1 规矩", pack)
        self.assertIn("新决定", pack)
        self.assertNotIn("旧决定", pack)
        self.assertEqual(keep["decisions"], ["d1", "d2"])

    def test_the_pack_sent_belongs_to_one_session_and_is_kept_after_a_turn_that_ended_well(self):
        job = self.job(9)
        job.session_id = SID
        job.pack_pending = {"project": "proj01", "topics": {"t1": "x"}, "memory": "m", "decisions": ["d1"]}
        project_lead.pack_settle(self.ctx, job)
        self.assertEqual(project_lead.pack_sent(self.state, "proj01", job, SID)["decisions"], ["d1"])
        self.assertIsNone(project_lead.pack_sent(self.state, "proj01", job, OTHER))


class MemoryCommandTest(unittest.TestCase):
    def test_the_memory_command_goes_through_the_data_roots_covey_when_it_is_there(self):
        from covey.app import memory_turn

        root = Path(tempfile.mkdtemp(prefix="rules-sent-"))
        self.addCleanup(shutil.rmtree, root, True)
        ctx = make_context(root, topic_id=TOPIC)
        job = SimpleNamespace(topic_id=TOPIC)
        with mock.patch.dict(os.environ, {"COVEY_DATA_HOME": str(root / "data")}):
            before = memory_turn.search_command(ctx, job, True)
            self.assertIn("covey_memory.py", before)
            (root / "data" / "bin").mkdir(parents=True)
            (root / "data" / "bin" / "covey").write_text("#!/bin/sh\n")
            line = memory_turn.search_command(ctx, job, True)
        self.assertTrue(line.startswith(f"{root / 'data' / 'bin' / 'covey'} memory search --state "))
        self.assertIn("--owner", line)
        self.assertNotIn("releases", line)


if __name__ == "__main__":
    unittest.main()
