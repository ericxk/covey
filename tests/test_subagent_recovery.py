"""What a failed or interrupted Claude turn's subagents delivered reaches the next turn (backends/claude/subagent_report.py,
app/subagent_recovery.py, the block core/checkpoint.py ``subagents_note`` words)."""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.world import UnifiedCase
from support.fake_backends import SID
from test_auto_retry import Failing

from covey.app import auto_retry, subagent_recovery
from covey.backends.claude import subagent_report
from covey.core.checkpoint import CHECKPOINT_RULES, checkpoint_note, subagents_note
from covey.core.harness import HarnessCapabilities, RunResult
from covey.core.jobs import find_job_record, upsert_job_record


def write_agent(folder, name, *, finished, description, texts=(), error=False, tool_use=False):
    """One subagent as Claude Code 2.1.3xx writes it: ``agent-<name>.jsonl`` and its ``.meta.json``."""
    rows = [{"type": "user", "isSidechain": True, "message": {"role": "user", "content": "go"}}]
    for text in texts:
        rows.append({"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "stop_reason": "end_turn",
                                                                           "content": [{"type": "text", "text": text}]}})
    if error:
        rows.append({"type": "assistant", "isApiErrorMessage": True,
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "API Error: 529"}]}})
    if tool_use:
        rows.append({"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
                                                       "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}})
    (folder / f"agent-{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{torn")
    meta = {"description": description, "toolUseId": f"toolu_{name}", "spawnDepth": 1}
    if finished:
        meta["finished"] = True
    (folder / f"agent-{name}.meta.json").write_text(json.dumps(meta))


class ReadTest(unittest.TestCase):
    def test_each_subagent_with_its_end_and_last_text(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            write_agent(folder, "a", finished=True, description="审计颜色", texts=["中间", "最终结论"])
            write_agent(folder, "b", finished=True, description="跑评测", texts=["半截"], error=True)
            write_agent(folder, "c", finished=False, description="写文档", texts=["先查了"], tool_use=True)
            (folder / "agent-d.jsonl").write_text("not json at all\n")  # No meta, no rows.
            found = {x["id"]: x for x in subagent_report.read(folder, 0)}
        self.assertEqual({k: (v["description"], v["end"], v["text"]) for k, v in found.items() if k != "agent-d"}, {
            "toolu_a": ("审计颜色", "finished", "最终结论"),
            "toolu_b": ("跑评测", "failed", "半截"),
            "toolu_c": ("写文档", "unfinished", "先查了")})
        self.assertEqual((found["agent-d"]["end"], found["agent-d"]["text"]), ("unfinished", ""))

    def test_a_missing_directory_and_old_files_give_nothing(self):
        self.assertEqual(subagent_report.read("/nonexistent/subagents", 0), [])
        with tempfile.TemporaryDirectory() as raw:
            write_agent(Path(raw), "a", finished=True, description="x", texts=["y"])
            self.assertEqual(subagent_report.read(raw, 4_000_000_000), [])

    def test_the_report_never_raises(self):
        ctx = mock.Mock(workdir="/w")
        with mock.patch.object(subagent_report, "ClaudeSessionStore", side_effect=RuntimeError("format")):
            self.assertEqual(subagent_report.report(ctx, "sid", 0, {}), [])


class Backend:
    def __init__(self, found):
        self.found, self.asked = found, []

    def capabilities(self, ctx):
        return HarnessCapabilities(turn_export=True)

    def subagent_report(self, ctx, session_id, since):
        self.asked.append((session_id, since))
        return self.found


def agent(number, end="finished", text=None):
    return {"id": f"t{number}", "description": f"任务{number}", "end": end, "text": text if text is not None else f"结论{number}"}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = mock.Mock(state_dir=Path(self.dir.name))
        self.record = {"key": "om_1", "status": "failed", "session_id": "sid", "timings": {"backend_requested": 100.0}}

    def collect(self, found, record=None):
        harness = mock.Mock(backend=Backend(found))
        return subagent_recovery.collect(harness, self.ctx, record or self.record), harness.backend

    def test_only_a_failed_or_interrupted_turn_of_a_backend_that_reads_them(self):
        for status in ("done", "cancelled", "running"):
            self.assertIsNone(self.collect([agent(1)], {**self.record, "status": status})[0], status)
        self.assertIsNone(self.collect([agent(1)], {**self.record, "session_id": ""})[0])
        self.assertIsNone(self.collect([])[0])
        self.assertIsNone(subagent_recovery.collect(mock.Mock(backend=object()), self.ctx, self.record))
        got, backend = self.collect([agent(1)], {**self.record, "status": "interrupted"})
        self.assertEqual(got["items"][0]["text"], "结论1")
        self.assertEqual(backend.asked, [("sid", 100.0)])

    def test_a_failure_reading_is_no_block(self):
        backend = Backend([])
        backend.subagent_report = mock.Mock(side_effect=OSError("gone"))
        self.assertIsNone(subagent_recovery.collect(mock.Mock(backend=backend), self.ctx, self.record))

    def test_texts_are_cut_and_the_rest_past_eight_goes_to_a_file_with_the_unfinished_first(self):
        found = [agent(n, text="长" * 3000 if n == 1 else None) for n in range(1, 12)] + [agent(12, "unfinished", "半截")]
        got, _ = self.collect(found)
        self.assertEqual(len(got["items"]), 8)
        self.assertEqual(got["more"], 4)
        self.assertIn("任务12", [i["description"] for i in got["items"]])  # The unfinished one is shown.
        [path] = got["files"]
        self.assertEqual(Path(path).parent.name, "om_1")
        self.assertEqual(Path(path).name, "subagents.md")
        self.assertTrue(all(len(i["text"]) <= 1501 for i in got["items"]))
        body = Path(path).read_text(encoding="utf-8")
        self.assertEqual(body.count("## "), 4)
        shown = {i["description"] for i in got["items"]}
        self.assertTrue(all(f"## 任务{n}" not in body for n in range(1, 13) if f"任务{n}" in shown))

    def test_an_unwritable_file_is_a_block_without_it(self):
        with mock.patch("covey.app.subagent_recovery.run_dir", side_effect=OSError("ro")):
            got, _ = self.collect([agent(n) for n in range(1, 11)])
        self.assertEqual((len(got["items"]), got["files"]), (8, []))

    def test_what_an_earlier_dead_turn_delivered_is_carried_on(self):
        earlier = {"items": [agent(1), agent(2, "unfinished")], "more": 0, "files": ["/x/subagents.md"]}
        record = {**self.record, "recovery_context": {"subagents": earlier}}
        got, _ = self.collect([], record)  # The retry died before any subagent.
        self.assertEqual([i["description"] for i in got["items"]], ["任务1", "任务2"])
        got, _ = self.collect([agent(2, "finished", "新")], record)  # It finished one the first had not.
        self.assertEqual([(i["description"], i["end"]) for i in got["items"]], [("任务1", "finished"), ("任务2", "finished")])
        self.assertEqual(got["files"], ["/x/subagents.md"])


class NoteTest(unittest.TestCase):
    def test_the_block_names_each_end_and_the_file(self):
        delivered = {"items": [agent(1), agent(2, "unfinished", ""), agent(3, "failed")], "more": 2, "files": ["/r/subagents.md"]}
        note = checkpoint_note(Path("/c.json"), {"previous_status": "failed", "subagents": delivered})
        self.assertIn("任务1（已完成）：结论1", note)
        self.assertIn("任务2（没做完）：（没有文字）", note)
        self.assertIn("任务3（出错）", note)
        self.assertIn("不要重做", note)
        self.assertIn("另有 2 个子 Agent 的结果写在：/r/subagents.md", note)
        self.assertEqual(note.count("结论1"), 1)  # Not also in the JSON of the status.
        self.assertIn('"previous_status"', note)

    def test_only_the_block_when_nothing_else_is_known(self):
        note = checkpoint_note(Path("/c.json"), {"subagents": {"items": [agent(1)], "more": 0, "files": []}})
        self.assertNotIn("上次任务状态", note)
        self.assertIn("任务1", note)

    def test_nothing_or_anything_odd_is_no_block(self):
        for odd in (None, {}, {"items": []}, {"items": "x"}, "x", {"items": [3]}):
            self.assertEqual(subagents_note(odd), "", odd)
        self.assertEqual(checkpoint_note(Path("/c.json")), "本任务检查点文件：/c.json")

    def test_the_checkpoint_rule_says_to_note_what_a_subagent_returns(self):
        self.assertIn("子 Agent 返回后记一句它的结论", CHECKPOINT_RULES)
        self.assertIn("登记进 artifacts", CHECKPOINT_RULES)


class SubagentBackend(Failing):
    found = [agent(1), agent(2, "unfinished", "半截")]

    def capabilities(self, ctx):
        return HarnessCapabilities(rate_limit_retry=True, incomplete_retry=True, turn_export=True)

    def subagent_report(self, ctx, session_id, since):
        return self.found

    def run(self, request):
        result = super().run(request)
        return RunResult(result.text, SID, True, None, result.outcome)  # A failed turn still names its session.


class WiredTest(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.claude = self.app.apps["claude"]
        self.backend = self.claude.harness.backend = SubagentBackend()
        self.claude.cards.update_record_elements = mock.Mock(return_value=True)

    def failed(self, mid="task"):
        self.receive(mid, "initial")
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, "chat", "claude")
        app, ctx = self.route(mid)
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        app.process_reply_job(job, app.harness.context)
        self.topic_ctx, self.first = ctx, job
        return find_job_record(self.ctx.state_dir, job.message_id)

    def test_a_retry_carries_what_the_dead_turn_s_subagents_delivered(self):
        record = self.failed()
        self.assertEqual(record["status"], "failed")
        auto_retry.tick(self.app, self.ctx, now=record["auto_retry_at"] + 1)
        [job] = list(self.app.runtime.jobs.queue.queue)
        items = job.recovery_context["subagents"]["items"]
        self.assertEqual([(i["description"], i["end"]) for i in items], [("任务1", "finished"), ("任务2", "unfinished")])

    def test_the_owner_s_next_message_gets_the_block_too(self):
        record = self.failed()
        job = dataclasses.replace(self.first, job_id=self.first.job_id + 1, message_id="next", recovery_context=None)
        job.checkpoint_path = str(Path(self.root) / "cp.json")
        note = self.claude.runs._checkpoint_note(self.topic_ctx, job)
        self.assertIn("任务2（没做完）：半截", note)
        self.assertEqual(job.recovery_context["subagents"]["items"][0]["description"], "任务1")
        self.assertNotIn("上次任务状态", note)
        self.assertEqual(record["status"], "failed")

    def test_after_a_task_that_succeeded_there_is_no_block(self):
        record = self.failed()
        upsert_job_record(self.ctx.state_dir, "om_later", topic_id=record["topic_id"], chat_id=record["chat_id"], job_id=2,
                          kind="chat", backend=record["backend"], status="done", created_at="2099-01-01T00:00:00Z",
                          updated_at="2099-01-01T00:00:00Z")
        job = dataclasses.replace(self.first, job_id=self.first.job_id + 2, message_id="next", recovery_context=None)
        job.checkpoint_path = str(Path(self.root) / "cp.json")
        self.assertNotIn("子 Agent", self.claude.runs._checkpoint_note(self.topic_ctx, job))
        self.assertIsNone(job.recovery_context)


if __name__ == "__main__":
    unittest.main()
