"""The owner's recalled or edited messages (app/source_watch.py): read back, the task stops or takes the new words."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app import source_watch
from covey.app.source_watch import SourceWatch, read_back
from covey.core import knowledge
from covey.core.jobs import RECALLED, find_job_record, parse_job_stop_action_value, upsert_job_record
from covey.core.source_book import GRACE_SECONDS, SourceBook, Watched
from covey.feishu.forward import render_replied_message_context
from support.world import TopicFixture


def detail(message_id, *, deleted=False, updated=False, update_time="1000", text="原文"):
    content = json.dumps({"text": text}, ensure_ascii=False)
    return {"ok": True, "data": {"items": [{"message_id": message_id, "msg_type": "text", "body": {"content": content},
                                            "deleted": deleted, "updated": updated, "update_time": update_time,
                                            "create_time": "1000", "sender": {"id": "ou_user", "sender_type": "user"}}]}}


class ReadBackTest(unittest.TestCase):
    def test_what_a_read_back_says(self):
        self.assertEqual(read_back(detail("om_a", deleted=True), "om_a", "1000")[0], "recalled")
        self.assertEqual(read_back(detail("om_a", updated=True, update_time="2000", text="新的"), "om_a", "1000"),
                         ("edited", "新的", "2000"))
        # The same edit read again is not a second one.
        self.assertEqual(read_back(detail("om_a", updated=True, update_time="2000"), "om_a", "2000")[0], "same")
        self.assertEqual(read_back(detail("om_a"), "om_a", "1000")[0], "same")
        self.assertEqual(read_back({"ok": False, "error": "code 230011: The message was recalled"}, "om_a", "1")[0],
                         "recalled")
        self.assertEqual(read_back({"ok": False, "error": "timeout"}, "om_a", "1")[0], "unknown")
        self.assertEqual(read_back({"ok": True, "data": {"items": []}}, "om_a", "1")[0], "unknown")

    def test_a_stop_by_the_recall_keeps_its_by(self):
        value = {"kind": "bridge_job_stop", "job_id": "3", "message_id": "om_a", "by": RECALLED}
        self.assertEqual(parse_job_stop_action_value(value)["by"], RECALLED)
        self.assertNotIn("by", parse_job_stop_action_value({**value, "by": "else"}))

    def test_the_book_keeps_its_newest(self):
        book = SourceBook()
        with mock.patch("covey.core.source_book.MAX_WATCHED", 2):
            for n in range(3):
                book.note(Watched(f"om_{n}", "oc", "claude", "t", "1", added=n))
        self.assertEqual(sorted(item.message_id for item in book.snapshot()), ["om_1", "om_2"])


class QuotedMessageTest(unittest.TestCase):
    def test_a_quoted_message_says_it_was_recalled_or_edited(self):
        event = {"parent_id": "om_p"}
        gone = render_replied_message_context(event, detail("om_p", deleted=True, text="This message was recalled"),
                                              media_dir=Path("/nonexistent"))
        self.assertIn("已被发送者撤回", gone)
        self.assertNotIn("This message was recalled", gone)
        edited = render_replied_message_context(event, detail("om_p", updated=True, text="改后"),
                                                media_dir=Path("/nonexistent"))
        self.assertIn("编辑过", edited)
        self.assertIn("改后", edited)
        plain = render_replied_message_context(event, detail("om_p"), media_dir=Path("/nonexistent"))
        self.assertNotIn("编辑过", plain)


class SourceWatchTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.watch = SourceWatch(self.app.runtime, SimpleNamespace(apps={self.app.spec.name: self.app}),
                                 self.app.messenger, self.app.voice)

    def round(self, answer, now=None):
        with mock.patch("covey.feishu.message_read.fetch_message_detail", return_value=answer):
            self.watch.round(self.ctx, now=now)

    def test_only_the_owners_messages_are_watched(self):
        self.receive("om_a", "one")
        self.app.handle_event({"chat_id": "oc_chat", "sender_id": "ou_other", "message_id": "om_b", "content": "x"},
                              self.ctx, fetch_message_details=False)
        self.assertEqual([item.message_id for item in self.app.runtime.sources.snapshot()], ["om_a"])

    def test_nothing_is_watched_with_the_switch_off(self):
        with mock.patch.dict("os.environ", {"COVEY_SOURCE_WATCH": "0"}):
            self.receive("om_a", "one")
            self.assertEqual(self.watch.threads(), ())
        self.assertEqual(len(self.app.runtime.sources), 0)

    def test_a_queued_task_whose_message_is_recalled_never_runs(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.round(detail("om_a", deleted=True))
        self.assertTrue(job.cancelled_before_start)
        record = find_job_record(self.topic("om_a").state_dir, "om_a")
        self.assertEqual((record["status"], record.get("cancel_requested_by")), ("cancelled", RECALLED))
        self.assertEqual(len(self.app.runtime.sources), 0)
        said = " ".join(str(call.args[2]) for call in self.app.messenger.send_message.call_args_list)
        self.assertNotIn("已取消排队任务", said)  # No reply quoting a message that is gone.
        self.app.process_reply_job(job, self.topic("om_a"))
        self.assertEqual(len(self.backend.calls), 0)

    def test_a_running_task_whose_message_is_recalled_stops(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(), supports_steer=True)
        self.round(detail("om_a", deleted=True))
        self.assertTrue(job.cancel_event.is_set())
        self.assertEqual(job.cancel_requested_by, RECALLED)

    def test_a_queued_task_takes_the_edited_words_into_its_request(self):
        self.receive("om_a", "原来的问题")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.round(detail("om_a", updated=True, update_time="2000", text="改过的问题"))
        self.assertIn("改过的问题", job.prompt_text)
        self.assertIn("主人编辑了刚才的消息", job.prompt_text)
        self.round(detail("om_a", updated=True, update_time="2000", text="改过的问题"))
        self.assertEqual(job.prompt_text.count("改过的问题"), 1)  # One edit, one supplement.

    def test_a_running_task_gets_the_edit_as_a_supplement(self):
        self.receive("om_a", "原来的问题")
        job = self.app.runtime.jobs.queue.get_nowait()
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value="sent"), supports_steer=True)
        self.app.cards.set_task_element = mock.Mock(return_value=True)
        self.round(detail("om_a", updated=True, update_time="2000", text="改过的问题"))
        job.backend_control.steer.assert_called_once()
        self.assertIn("改过的问题", job.backend_control.steer.call_args.args[0])

    def test_an_edit_after_the_turn_ended_is_told_not_rerun(self):
        self.receive("om_a", "原来的问题")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(job)
        self.app.messenger.send_message.reset_mock()
        self.round(detail("om_a", updated=True, update_time="2000", text="改过的问题"))
        said = [str(call.args[2]) for call in self.app.messenger.send_message.call_args_list]
        self.assertTrue(any("改过的问题" in text and "没有重做" in text for text in said), said)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 0)

    def test_a_message_is_no_longer_read_back_past_its_grace(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(job)
        self.round(detail("om_a"), now=1000.0)
        self.assertEqual(len(self.app.runtime.sources), 1)
        with mock.patch("covey.feishu.message_read.fetch_message_detail") as fetch:
            self.watch.round(self.ctx, now=1000.0 + GRACE_SECONDS + 1)
        fetch.assert_not_called()
        self.assertEqual(len(self.app.runtime.sources), 0)

    def test_a_recall_after_the_turn_takes_back_what_it_remembered(self):
        self.receive("om_a", "记一下：周五发版")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(job)
        state = self.topic("om_a").state_dir
        record = find_job_record(state, "om_a")
        entry, _ = knowledge.save(state, knowledge.OWNER, "fact", "周五发版", source={"job_key": record["key"]}, by="topic")
        upsert_job_record(state, record["key"], memory_notes=[{"kind": "remembered", "text": "x", "entry_id": "m1"}])
        with mock.patch("covey.app.memory_collect._forget") as forget, mock.patch("covey.app.memory_collect.show"):
            self.round(detail("om_a", deleted=True))
        forget.assert_called_once_with(state, record["topic_id"], "m1")
        self.assertEqual(find_job_record(state, "om_a")["memory_notes"][0]["kind"], "undone")
        self.assertEqual(knowledge.entries(state), [])
        self.assertEqual(knowledge.find(state, entry["id"])["state"], knowledge.DROPPED)

    def test_the_topics_a_recalled_turn_opened_get_the_line_with_stop(self):
        from covey.core import butler_opened
        from covey.core.topics import topic_file

        self.receive("om_a", "帮我开个话题")
        butler_job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.unregister(butler_job)
        state = self.topic("om_a").state_dir
        self.receive("om_t", "the opened topic's task")
        opened_job = self.app.runtime.jobs.queue.get_nowait()
        topic = opened_job.topic_id
        self.assertTrue(json.loads(topic_file(state, topic).read_text())["root_id"].startswith("om_"))
        folder = butler_opened.folder(state)
        folder.mkdir(parents=True, exist_ok=True)
        key = find_job_record(state, "om_a")["key"]
        (folder / "req1.json").write_text(json.dumps({"job_key": key, "chat_id": "oc_chat", "topic_id": topic,
                                                      "plan": {}, "answer": {"ok": True}}))
        self.app.runtime.sources.drop("om_t")
        self.app.messenger.send_message.reset_mock()
        self.round(detail("om_a", deleted=True))
        cards = [call.kwargs.get("card") for call in self.app.messenger.send_message.call_args_list
                 if call.kwargs.get("reply_to_message_id") == "om_t"]
        self.assertEqual(len(cards), 1)
        values = [node["behaviors"][0]["value"] for node in walk(cards[0]) if node.get("tag") == "button"]
        self.assertEqual(values, [{"kind": "bridge_job_stop", "job_id": str(opened_job.job_id), "message_id": "om_t",
                                   "by": RECALLED}])
        self.assertFalse(opened_job.cancel_event.is_set())  # Marked, not stopped: the owner's choice.

    def test_a_recalled_supplement_is_followed_by_one_saying_ignore_it(self):
        self.receive("om_a", "one")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.runtime.jobs.register(job)
        self.app.runtime.sources.drop("om_a")
        from covey.core.deliveries import delivery_path, save_delivery
        save_delivery(delivery_path(self.topic("om_a").state_dir, job.topic_id, "om_s"), {"status": "sent"})
        source_watch.note(self.app, self.topic("om_a"), {"create_time": "1000"}, "om_s", "oc_chat", "ou_user", "补充的话",
                          steered=True)
        self.assertEqual(self.app.runtime.sources.get("om_s").steer, (job.job_id, job.topic_id))
        self.round(detail("om_s", deleted=True))
        self.assertIn("撤回了刚才那条补充", job.prompt_text)
        self.assertIn("补充的话", job.prompt_text)
        self.assertFalse(job.cancel_event.is_set())


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


if __name__ == "__main__":
    unittest.main()
