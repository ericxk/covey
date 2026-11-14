"""↩️ 回退: dropping a topic's latest Claude Code turn (app/rewind.py, backends/claude/rewind.py).

Claude Code's side over synthetic session files and a mocked CLI (the flags were verified on 2.1.310,
2026-11-03): the kept entry, the files a file rewind would change, the argv. The runtime's: resuming at
the kept entry once, the prompt reported when Claude Code takes it. The topic's: who may go back, the
history rows, the marker the next turn takes, 撤销回退, the button and the card.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context
from support.cards import walk_dicts
from support.sessions import write_session_id
from support.fake_lark import FakeCardKit
from support.world import UnifiedCase
from support.records import write_topic

from covey.app import rewind as rw
from covey.backends.claude import compaction as claude_compaction
from covey.backends.claude import rewind as claude_rewind
from covey.backends.claude.argv import stream_argv
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.claude.control import ClaudeControl
from covey.backends.claude.sessions import encode_project_dir
from covey.cards import rewind as cards
from covey.cards.topic_actions import topic_panel
from covey.core.jobs import find_job_record, job_path, list_job_records, upsert_job_record
from covey.core.history_storage import append_history, drop_last_round, recent_history, restore_rounds
from covey.core.session_state import read_session_id
from covey.core.store import invalidate_directory
from covey.core.topics import read_topic

SID = "2b45a44e-4784-4f1d-bec9-0d881921a79c"
FIRST, SECOND = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
LAST_OF_FIRST = "310025bf-c8e7-48de-a72e-48306d5ca792"


class ClaudeSideTest(unittest.TestCase):
    """The session file as Claude Code 2.1.310 wrote it in the 2026-11-03 trial, cut to what matters."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.config = self.root / "claude-config"
        self.env = {"CLAUDE_CONFIG_DIR": str(self.config)}
        self.ctx = SimpleNamespace(workdir=self.work, backend_cmd=["claude"], options={})
        backups = self.config / "file-history" / SID
        backups.mkdir(parents=True)
        (backups / "a1@v2").write_text("one")
        (self.work / "a.txt").write_text("two")  # The second turn changed it.
        (self.work / "b.txt").write_text("bee")  # The second turn made it.
        (self.work / "c.txt").write_text("same")  # Tracked, not changed since.
        (backups / "c1@v1").write_text("same")
        work = str(self.work)
        self.write([
            {"type": "user", "uuid": FIRST, "parentUuid": None, "message": {"role": "user", "content": "make a.txt"}},
            {"type": "file-history-snapshot", "messageId": FIRST, "snapshot": {"trackedFileBackups": {}}},
            {"type": "assistant", "uuid": LAST_OF_FIRST, "parentUuid": FIRST, "message": {"content": []}},
            {"type": "user", "uuid": SECOND, "parentUuid": LAST_OF_FIRST, "message": {"role": "user", "content": "x"}},
            {"type": "file-history-snapshot", "messageId": SECOND, "snapshot": {"trackedFileBackups": {
                "a.txt": {"backupFileName": "a1@v2", "realParentDir": work},
                "c.txt": {"backupFileName": "c1@v1", "realParentDir": work}}}},
            {"type": "file-history-delta", "snapshotMessageId": SECOND, "trackingPath": "b.txt",
             "backup": {"backupFileName": None, "realParentDir": work}},
            {"type": "assistant", "uuid": "e9506d59-7dfd-4077-9ae5-9143b53c3922", "parentUuid": SECOND},
        ])

    def write(self, rows):
        folder = self.config / "projects" / encode_project_dir(self.work)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{SID}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_the_kept_entry_is_the_prompts_parent_and_the_files_are_those_a_file_rewind_would_change(self):
        point = claude_rewind.rewind_point(self.ctx, SID, SECOND, self.env)
        self.assertEqual(point["keep"], LAST_OF_FIRST)
        self.assertTrue(point["checkpoint"])
        self.assertEqual(point["files"], [(str(self.work / "a.txt"), "restore"), (str(self.work / "b.txt"), "delete")])
        self.assertEqual(point["file_count"], 2)

    def test_a_long_session_file_is_read_from_its_end_in_pieces(self):
        with mock.patch.object(claude_rewind, "TAIL_BYTES", 64):
            self.assertEqual(claude_rewind.rewind_point(self.ctx, SID, SECOND, self.env)["keep"], LAST_OF_FIRST)
            self.assertEqual(claude_rewind.rewind_point(self.ctx, SID, FIRST, self.env)["keep"], "")

    def test_the_first_turn_keeps_nothing_and_a_prompt_not_in_the_session_is_refused(self):
        point = claude_rewind.rewind_point(self.ctx, SID, FIRST, self.env)
        self.assertEqual((point["keep"], point["checkpoint"], point["file_count"]), ("", True, 0))
        with self.assertRaisesRegex(ValueError, "找不到这一轮的提问"):
            claude_rewind.rewind_point(self.ctx, SID, str(uuid.uuid4()), self.env)
        with self.assertRaisesRegex(ValueError, "会话记录"):
            claude_rewind.rewind_point(self.ctx, str(uuid.uuid4()), SECOND, self.env)
        # A turn that ran before file backups were kept has no snapshot: only the conversation can go.
        self.write([{"type": "user", "uuid": SECOND, "parentUuid": LAST_OF_FIRST}])
        point = claude_rewind.rewind_point(self.ctx, SID, SECOND, self.env)
        self.assertEqual((point["keep"], point["checkpoint"], point["files"]), (LAST_OF_FIRST, False, []))

    def test_files_go_back_through_claude_code_with_its_backups_on_and_its_refusal_is_said(self):
        backend = ClaudeBackend()
        done = subprocess.CompletedProcess([], 0, stdout=f"Files rewound to state at message {SECOND}\n", stderr="")
        with mock.patch.object(claude_rewind.subprocess, "run", return_value=done) as run:
            self.assertIn("Files rewound", claude_rewind.rewind_files(backend, self.ctx, SID, SECOND, self.env))
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv[-6:], ["claude", "-p", "--resume", SID, "--rewind-files", SECOND][-6:])
        self.assertEqual(kwargs["env"][claude_rewind.FILE_CHECKPOINTS_ENV], "1")
        self.assertEqual((kwargs["cwd"], kwargs["stdin"]), (str(self.work), subprocess.DEVNULL))
        refused = subprocess.CompletedProcess([], 1, stdout="", stderr="Error: No file checkpoint found for this message.\n")
        with mock.patch.object(claude_rewind.subprocess, "run", return_value=refused), \
                self.assertRaisesRegex(ValueError, "No file checkpoint found"):
            claude_rewind.rewind_files(backend, self.ctx, SID, SECOND, self.env)

    def test_every_claude_child_keeps_file_backups_and_can_rewind(self):
        backend = ClaudeBackend()
        ctx = make_context(self.root)
        with mock.patch.dict(os.environ):
            os.environ.pop(claude_rewind.FILE_CHECKPOINTS_ENV, None)
            self.assertEqual(backend.child_env(ctx)[claude_rewind.FILE_CHECKPOINTS_ENV], "1")
        self.assertTrue(backend.capabilities(ctx).rewind)


class ClaudeRuntimeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = make_context(Path(tmp.name))

    def test_a_rewound_session_resumes_at_the_kept_entry_and_compacts_on_the_next_turn(self):
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "resume_at": LAST_OF_FIRST,
                                                     "compact_first": True, "compact_between_turns": 1})
        argv = stream_argv(ClaudeBackend(), ctx, SID)
        self.assertEqual(argv[argv.index("--resume") + 1:argv.index("--resume") + 4],
                         [SID, "--resume-session-at", LAST_OF_FIRST])
        self.assertNotIn("--resume-session-at", stream_argv(ClaudeBackend(), ctx))  # A fresh session.
        self.assertIsNone(claude_compaction.reason(ctx, SID, {}))

    def test_the_turns_prompt_is_reported_once_when_claude_code_takes_it(self):
        events = []
        control = ClaudeControl(SimpleNamespace(send=lambda row: None), SimpleNamespace(submit=lambda _: None,
                                session_id=SID), lambda kind, data: events.append((kind, data)))
        control.prompt_id = SECOND
        control.accept({"type": "user", "uuid": str(uuid.uuid4()), "session_id": SID})
        control.accept({"type": "user", "uuid": SECOND, "session_id": SID, "isReplay": True})
        control.accept({"type": "user", "uuid": SECOND, "session_id": SID, "isReplay": True})
        self.assertEqual(events, [("prompt_read", {"uuid": SECOND, "session_id": SID})])


class TurnTest(unittest.TestCase):
    """A turn through Runs: it resumes at the kept entry while the marker waits, its prompt_read records the prompt
    and takes the marker."""

    def test_a_turn_resumes_at_the_kept_entry_records_its_prompt_and_takes_the_marker(self):
        from support.world import build_claude_app
        from covey.core.harness import HarnessCapabilities, RunResult
        from covey.core.jobs import BridgeReplyJob

        seen = []

        class Backend:
            def capabilities(self, ctx):
                return HarnessCapabilities(rewind=True)

            def prompt_runtime_note(self, ctx):
                return ""

            def run(self, request):
                seen.append(request.context.options.get("resume_at"))
                request.callbacks.event("prompt_read", {"uuid": "p3", "session_id": SID}, None)
                return RunResult("done", request.session_id)
        with tempfile.TemporaryDirectory() as tmp:
            app = build_claude_app(Backend())
            ctx = dataclasses.replace(make_context(Path(tmp)), topic_id="a" * 16)
            ctx.history_path.parent.mkdir(parents=True, exist_ok=True)
            write_topic(ctx, {"topic_id": ctx.topic_id})
            rw.marker_path(ctx).write_text(json.dumps(
                {"session_id": SID, "keep": LAST_OF_FIRST, "token": "t" * 24, "job_id": 2}))
            job = BridgeReplyJob(job_id=3, user_text="next", chat_id="chat", message_id="job-3", topic_id=ctx.topic_id,
                                 timeout=0, checkpoint_path=str(Path(tmp) / "cp.json"))
            upsert_job_record(ctx.state_dir, "job-3", topic_id=ctx.topic_id, chat_id="chat", job_id=3)
            app.runs.generate_reply_for_worker(ctx, job, SID, None)
            self.assertEqual(seen, [LAST_OF_FIRST])
            self.assertIsNone(rw.read_marker(ctx))
            self.assertEqual(find_job_record(ctx.state_dir, "job-3")["prompt_uuid"], "p3")
            app.runs.generate_reply_for_worker(ctx, job, SID, None)
            self.assertEqual(seen, [LAST_OF_FIRST, None])


class HistoryTest(unittest.TestCase):
    def test_the_last_round_leaves_the_history_and_comes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            for n in (1, 2):
                append_history(path, "user", f"question {n}", {"type": "im.message.receive_v1", "message_id": f"m{n}"})
                if n == 2:
                    append_history(path, "user", "补充", {"message_id": "m2b"})  # A supplement while it ran.
                append_history(path, "assistant", f"answer {n}")
            before = path.read_text()
            dropped = drop_last_round(path)
            self.assertEqual([row["content"] for row in recent_history(path)], ["question 1", "answer 1"])
            self.assertEqual(len(dropped), 3)
            restore_rounds(path, dropped)
            self.assertEqual(path.read_text(), before)


class RewindTests(UnifiedCase):
    """A Claude topic with two finished turns in session SID; the backend's two calls mocked."""

    def setUp(self):
        super().setUp()
        self.receive("src", "first question")
        _, self.source = self.route("src")
        data = read_topic(self.source)
        data.update(harness_id="claude", model="claude-model", effort="high", model_settings_snapshot=True,
                    workspace_locked=True, harness_locked=True)
        write_topic(self.source, data)
        self.claude, self.source = self.route("src")
        write_session_id(self.source.session_path, SID)
        self.source.history_path.unlink(missing_ok=True)
        for n in (1, 2):
            append_history(self.source.history_path, "user", f"question {n}", {})
            append_history(self.source.history_path, "assistant", f"answer {n}", {})
        for record in list_job_records(self.ctx.state_dir, limit=0):
            job_path(self.ctx.state_dir, record["key"]).unlink()
        invalidate_directory(job_path(self.ctx.state_dir, "x").parent)
        self.app.runtime.jobs.by_id.clear()
        for n, prompt in ((1, FIRST), (2, SECOND)):
            upsert_job_record(self.ctx.state_dir, f"job-{n}", topic_id=self.source.topic_id, chat_id="chat", job_id=n,
                              status="done", backend="claude", harness_id="claude", kind="chat", binding_revision=1,
                              user_text=f"question {n}", prompt_uuid=prompt, prompt_session=SID)
        self.backend = self.claude.backend
        self.point = {"keep": LAST_OF_FIRST, "checkpoint": True, "files": [(str(self.work / "a.txt"), "restore")],
                      "file_count": 1}
        patcher = mock.patch.object(type(self.backend), "rewind_point", side_effect=lambda ctx, sid, prompt: dict(
            self.point, keep="" if prompt == FIRST else self.point["keep"]))
        self.rewind_point = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(type(self.backend), "rewind_files", return_value="Files rewound")
        self.rewind_files = patcher.start()
        self.addCleanup(patcher.stop)
        self.cards = self.app.rewind

    def open(self, job_id=2, click="click-open"):
        value = {"kind": cards.OPEN, "topic_id": self.source.topic_id, "job_id": job_id, "harness_id": "claude",
                 "binding_revision": 1}
        with FakeCardKit({"data": {"card_id": "card_rw"}}, sent=("om_rw", False)) as lark:
            self.cards.open(self.ctx, {"chat_id": "chat", "event_id": click, "message_id": "om_task"}, value)
        token = hashlib.sha256(f"{self.source.topic_id}\0rewind\0{click}".encode()).hexdigest()[:24]
        path = self.ctx.state_dir / "rewind-forms" / (token + ".json")
        return (json.loads(path.read_text()), path, lark) if path.exists() else (None, None, lark)

    def click(self, form, kind, **value):
        event = {"chat_id": "chat", "event_id": "ev-" + kind, "message_id": form["message_id"],
                 "action_value": {"kind": kind, "token": form["token"], **value}}
        with mock.patch.object(self.cards, "submit", side_effect=lambda busy, handler, *args: handler(*args) or True), \
                FakeCardKit({"data": {}}) as lark:
            self.cards.dispatch(self.ctx, event)
        return lark

    def marker(self):
        return rw.read_marker(self.source)

    def history(self):
        return [row["content"] for row in recent_history(self.source.history_path)]

    def test_the_latest_turn_goes_and_the_next_turn_resumes_at_the_entry_before_it(self):
        form, path, lark = self.open()
        self.assertEqual((form["status"], form["user_text"], form["point"]["keep"]), ("ready", "question 2", LAST_OF_FIRST))
        self.assertIn("对话和文件都回退", json.dumps(lark.api.call_args_list, ensure_ascii=False, default=str))
        self.click(form, cards.CONFIRM, files=False)
        form = json.loads(path.read_text())
        self.assertEqual((form["status"], form["done"]["files"]), ("done", 0))
        self.rewind_files.assert_not_called()
        self.assertEqual(self.history(), ["question 1", "answer 1"])
        self.assertEqual(self.marker()["keep"], LAST_OF_FIRST)
        self.assertEqual(find_job_record(self.ctx.state_dir, "job-2")["rewound"]["files"], 0)
        # The next turn resumes at the kept entry until Claude Code has its prompt; then the marker goes.
        rewinds = self.claude.rewinds
        job = SimpleNamespace(job_id=3, message_id="job-3")
        upsert_job_record(self.ctx.state_dir, "job-3", topic_id=self.source.topic_id, chat_id="chat", job_id=3)
        turn = rewinds.turn_context(self.source, job, SID)
        self.assertEqual(turn.options["resume_at"], LAST_OF_FIRST)
        rewinds.prompt_read(turn, job, {"uuid": "new-prompt", "session_id": SID})
        self.assertIsNone(self.marker())
        self.assertEqual(find_job_record(self.ctx.state_dir, "job-3")["prompt_uuid"], "new-prompt")
        self.assertNotIn("resume_at", rewinds.turn_context(self.source, job, SID).options)

    def test_a_card_write_cardkit_did_not_confirm_keeps_its_sequence_reserved(self):
        # Hard rule 3: the write reserved the next sequence before the call; the saved form must not go
        # back to the one before it, or the next write to the card would reuse the reserved one.
        form, path, _ = self.open()
        before = form["sequence"]
        with mock.patch.object(self.cards.topics.messenger, "put_card", return_value=None):
            self.click(form, cards.CONFIRM, files=False)
            form = json.loads(path.read_text())
            self.assertEqual((form["status"], form["sequence"]), ("done", before + 1))
            self.click(form, cards.UNDO)
        form = json.loads(path.read_text())
        self.assertEqual((form["status"], form["sequence"]), ("undone", before + 2))

    def test_files_go_back_first_and_a_refusal_there_changes_nothing(self):
        form, path, _ = self.open()
        self.rewind_files.side_effect = ValueError("Claude Code 没能改回文件：No file checkpoint found")
        self.click(form, cards.CONFIRM, files=True)
        form = json.loads(path.read_text())
        self.assertEqual(form["status"], "ready")
        self.assertIn("No file checkpoint", form["notice"])
        self.assertEqual(self.history(), ["question 1", "answer 1", "question 2", "answer 2"])
        self.assertIsNone(self.marker())
        self.rewind_files.side_effect = None
        self.click(form, cards.CONFIRM, files=True)
        form = json.loads(path.read_text())
        self.assertEqual((form["status"], form["done"]["files"]), ("done", 1))
        self.assertEqual(self.rewind_files.call_args.args[1:], (SID, SECOND))
        self.assertNotIn("撤销回退", json.dumps(cards.done_card(form), ensure_ascii=False))  # Files cannot come back.

    def test_only_the_latest_turn_of_the_current_session_and_only_while_the_topic_is_idle(self):
        def refused(job_id=2):
            with self.assertRaises(ValueError) as caught:
                rw.check_rewind(self.app.topics, self.claude, self.source, "chat", job_id)
            return str(caught.exception)
        self.assertIn("最新的一轮（任务 #2）", refused(1))
        upsert_job_record(self.ctx.state_dir, "job-3", topic_id=self.source.topic_id, chat_id="chat", job_id=3,
                          status="running", backend="claude", kind="chat", binding_revision=1)
        self.assertIn("停止完成后再回退", refused())
        upsert_job_record(self.ctx.state_dir, "job-3", status="failed")  # It never reached the session.
        self.assertEqual(rw.check_rewind(self.app.topics, self.claude, self.source, "chat", 2)[1], SID)
        self.assertIn("没有进入 Claude Code 的会话", refused(3))
        write_session_id(self.source.session_path, str(uuid.uuid4()))
        self.assertIn("会话已经换过", refused())
        write_session_id(self.source.session_path, SID)
        data = read_topic(self.source)
        write_topic(self.source, {**data, "binding_revision": 2})
        self.assertIn("换过配置", refused())

    def test_one_step_until_the_next_message_and_undo_while_the_next_turn_has_not_taken_it(self):
        form, path, _ = self.open()
        self.click(form, cards.CONFIRM, files=False)
        with self.assertRaisesRegex(ValueError, "已经回退过"):
            rw.check_rewind(self.app.topics, self.claude, self.source, "chat", 2)
        with self.assertRaisesRegex(ValueError, rw.PENDING):
            rw.check_rewind(self.app.topics, self.claude, self.source, "chat", 1)
        form = json.loads(path.read_text())
        self.click(form, cards.UNDO)
        self.assertEqual(json.loads(path.read_text())["status"], "undone")
        self.assertIsNone(self.marker())
        self.assertEqual(self.history(), ["question 1", "answer 1", "question 2", "answer 2"])
        self.assertFalse(find_job_record(self.ctx.state_dir, "job-2").get("rewound"))
        # Once the next turn has its prompt, there is nothing to undo.
        form, path, _ = self.open(click="third")
        self.click(form, cards.CONFIRM, files=False)
        token = self.marker()["token"]
        turn = dataclasses.replace(self.source, options={**self.source.options, "rewind_token": token})
        self.claude.rewinds.prompt_read(turn, SimpleNamespace(job_id=3, message_id="job-3"), {})
        self.click(json.loads(path.read_text()), cards.UNDO)
        self.assertIn("不能撤销", json.loads(path.read_text())["notice"])

    def test_a_turn_that_began_the_session_clears_the_pointer_and_undo_brings_it_back(self):
        upsert_job_record(self.ctx.state_dir, "job-1", prompt_session="older")  # Job 2 began this session.
        upsert_job_record(self.ctx.state_dir, "job-2", prompt_uuid=FIRST)
        form, path, _ = self.open()
        self.click(form, cards.CONFIRM, files=False)
        self.assertIsNone(read_session_id(self.source.session_path, self.claude.spec.session_pattern))
        self.assertIsNone(self.marker())
        self.click(json.loads(path.read_text()), cards.UNDO)
        self.assertEqual(read_session_id(self.source.session_path, self.claude.spec.session_pattern), SID)

    def test_cancel_changes_nothing(self):
        form, path, _ = self.open()
        self.click(form, cards.CANCEL)
        self.assertEqual(json.loads(path.read_text())["status"], "cancelled")
        self.assertEqual(len(self.history()), 4)
        self.assertIsNone(self.marker())

    def test_a_refused_open_says_why_in_the_topic(self):
        self.claude.messenger.reply = mock.Mock()
        form, _, _ = self.open(job_id=1)
        self.assertIsNone(form)
        self.assertIn("最新的一轮", self.claude.messenger.reply.call_args.args[2])

    def test_a_marker_of_another_session_is_void(self):
        form, _, _ = self.open()
        self.click(form, cards.CONFIRM, files=False)
        turn = self.claude.rewinds.turn_context(self.source, SimpleNamespace(job_id=3, message_id="job-3"), "other")
        self.assertNotIn("resume_at", turn.options)
        self.assertIsNone(self.marker())


class RewindCardTest(unittest.TestCase):
    def test_the_panel_of_a_turn_has_the_button_where_the_harness_can_rewind(self):
        with_button = json.dumps(topic_panel("t" * 16, 7, ("claude", 2), rewind=True), ensure_ascii=False)
        self.assertIn("↩️ 回退", with_button)
        self.assertIn(cards.OPEN, with_button)
        self.assertNotIn("↩️ 回退", json.dumps(topic_panel("t" * 16, 7, ("codex", 2)), ensure_ascii=False))

    def test_the_card_offers_files_only_when_there_are_some(self):
        form = {"token": "a" * 24, "job_id": 7, "topic": "T9", "user_text": "改 a.txt", "workdir": "/w",
                "point": {"keep": "k", "checkpoint": True, "files": [("/w/a.txt", "restore"), ("/w/b", "delete")],
                          "file_count": 3}}
        text = json.dumps(cards.rewind_card(form), ensure_ascii=False)
        for words in ("对话和文件都回退", "只回退对话", "`a.txt`：改回这一轮之前", "`b`：删除", "另有 1 个", "话题 T9 · 任务 #7"):
            self.assertIn(words, text)
        form["point"] = {"keep": "k", "checkpoint": False, "files": [], "file_count": 0}
        text = json.dumps(cards.rewind_card(form), ensure_ascii=False)
        self.assertIn("回退这一轮", text)
        self.assertNotIn("对话和文件都回退", text)
        self.assertIn(cards.NO_CHECKPOINT, text)

    def test_after_a_conversation_rewind_the_files_left_as_they_are_are_a_list_of_their_own(self):
        # The 2026-11-03 client check: "文件没有动：- `a`：改回这一轮之前；- `b`：…" on one line, the action read as done.
        form = {"token": "a" * 24, "job_id": 7, "topic": "T9", "user_text": "改 a.txt", "workdir": "/w",
                "point": {"keep": "k", "checkpoint": True, "files": [("/w/a.txt", "restore"), ("/w/b", "delete")],
                          "file_count": 2}, "done": {"files": 0, "keep": "k"}}
        contents = [e.get("content", "") for e in walk_dicts(cards.done_card(form)) if e.get("tag") == "markdown"]
        self.assertIn("- `a.txt`\n- `b`", contents)
        self.assertTrue(any("没有改回" in c for c in contents))
        self.assertFalse(any("改回这一轮之前" in c or "删除" in c for c in contents))

    def test_a_turn_that_began_its_session_says_the_next_message_starts_a_new_one(self):
        # The 2026-11-03 client check: "下一条消息接在上一轮后面" after the session's only turn.
        form = {"token": "a" * 24, "job_id": 7, "topic": "T9", "user_text": "建 hello.txt", "workdir": "/w",
                "point": {"keep": "", "checkpoint": True, "files": [("/mnt/x/runs/accept/scratch/hello.txt", "delete")],
                          "file_count": 1}}
        text = json.dumps(cards.rewind_card(form), ensure_ascii=False)
        self.assertIn(cards.WHAT_FRESH, text)
        self.assertNotIn(cards.WHAT, text)
        self.assertIn("`…/scratch/hello.txt`：删除", text)  # Outside the workdir: its last two parts.
        done = json.dumps(cards.done_card({**form, "done": {"files": 1, "keep": ""}}), ensure_ascii=False)
        self.assertIn("下一条消息开新会话", done)
        self.assertNotIn("接在上一轮后面", done)
        form["point"]["keep"] = "k"
        self.assertIn("接在上一轮后面", json.dumps(cards.done_card({**form, "done": {"files": 1, "keep": "k"}}),
                                               ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
