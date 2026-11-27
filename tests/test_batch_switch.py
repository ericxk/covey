"""批量换配置 and the basis of a named configuration (T786): app/batch_switch.py, cards/batch_switch.py, app/config_basis.py.

The butler's or a lead's topics_switch only sends a card; the owner's 确认更换 switches each topic as 🔁 换配置 does, a busy one is
refused and 再试没换的 tries it again; a call that names a Harness, model or budget does so on the owner's words, which the Bridge
finds, or is refused."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

from support.world import UnifiedCase
from test_butler_topics import CHAT, ButlerTopicsCase

from covey.app import batch_switch, butler_tools, config_basis
from covey.cards import batch_switch as cards
from covey.core import butler, principles
from covey.core.butler_requests import Request
from covey.core.butler_tool_specs import LEAD_SHARED, SECONDARY, TOOLS, lead_tools, listed
from covey.core.jobs import job_path, list_job_records, upsert_job_record
from covey.core.store import invalidate_directory
from covey.core.topics import load_topic

OWNER_WORDS = "把 T1 和 T2 都换成 Codex 吧，监视进程这类给 codex 就行"


class BasisTest(UnifiedCase):
    """config_basis.check: the owner's words of the turn, a principle, a memory line the turn carried; nothing else."""

    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        upsert_job_record(self.state, "main-job", chat_id=CHAT, topic_id=butler.butler_topic_id(CHAT), job_id=1,
                          status="running", user_text=OWNER_WORDS, memory_ids=[{"n": "M49", "ref": "principle:x",
                                                                                  "scope": "global", "by": "owner"}])

    def check(self, basis, **kwargs):
        return config_basis.check(self.state, "main-job", basis, **kwargs)

    def test_a_run_of_the_owners_words_is_found_without_spaces_case_or_punctuation(self):
        self.assertEqual(self.check("“监视进程这类给 Codex”"), ("监视进程这类给 Codex", ""))
        self.assertEqual(self.check("换成codex"), ("换成codex", ""))

    def test_words_the_owner_did_not_say_are_refused_and_say_the_scope(self):
        shown, why = self.check("任务都用 codex")
        self.assertEqual(shown, "")
        self.assertIn("没找到", why)
        self.assertIn("只管它说的那类事", why)

    def test_nothing_or_a_bare_word_is_no_basis(self):
        self.assertEqual(self.check(""), ("", config_basis.MISSING))
        self.assertTrue(self.check("co")[1])  # Shorter than SHORTEST once compacted.

    def test_a_principle_by_its_id_or_its_words_and_a_memory_line_the_turn_carried(self):
        row, _ = principles.save(self.state, principles.OWNER, "跟踪进度、监视进程这类工作用 Codex，其他任务照常")
        self.assertEqual(self.check(f"原则 {row['id']}"), (config_basis.short(row["text"]), ""))
        self.assertEqual(self.check("监视进程这类工作用 Codex")[1], "")
        self.assertEqual(self.check("记忆 M49"), ("记忆 M49", ""))
        self.assertTrue(self.check("记忆 M50")[1])  # Not carried by this turn.

    def test_a_call_that_names_a_configuration_without_a_basis_is_refused_before_it_runs(self):
        ran = []
        env = SimpleNamespace(state_dir=self.state, chat=lambda: CHAT, serving=self.app.butler_env.serving,
                              runtime=self.app.runtime)
        tools = butler_tools.ButlerTools(env, {"topic_start": lambda env, request: ran.append(request) or
                                               {"ok": True, "text": "ok", "data": {}}})
        refused = tools.execute(Request("r1", "topic_start", {"harness": "codex"}, "main-job", time.time()))
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["text"], config_basis.MISSING)
        stretched = tools.execute(Request("r2", "topic_start", {"harness": "codex", "basis": "修代码用 codex"},
                                          "main-job", time.time()))
        self.assertFalse(stretched["ok"])
        self.assertEqual(ran, [])
        tools.execute(Request("r3", "topic_start", {"harness": "codex", "basis": "监视进程这类给 codex"}, "main-job",
                              time.time()))
        tools.execute(Request("r4", "topic_start", {"title": "x"}, "main-job", time.time()))  # Names nothing: as before.
        self.assertEqual([request.id for request in ran], ["r3", "r4"])


class SpecTest(UnifiedCase):
    def test_the_butler_and_a_lead_have_topics_switch_listed_lazily_for_agy(self):
        names = [tool["name"] for tool in listed({})]
        self.assertIn("topics_switch", names)
        self.assertIn("topics_switch", SECONDARY)
        self.assertIn("topics_switch", LEAD_SHARED)
        lead = [tool["name"] for tool in lead_tools({"COVEY_PROJECT_LEAD": "p1"})]
        self.assertIn("topics_switch", lead)
        spec = next(tool for tool in TOOLS if tool["name"] == "topics_switch")
        self.assertEqual(spec["input_schema"]["required"], ["topics", "basis"])
        self.assertIn("topics_switch", batch_switch.TOOLS)
        start = next(tool for tool in TOOLS if tool["name"] == "topic_start")
        self.assertIn("basis", start["input_schema"]["properties"])


class BatchCase(ButlerTopicsCase):
    def setUp(self):
        super().setUp()
        first, second = self.start("r1", title="监视训练"), self.start("r2", title="修部署检查")
        self.topics = [first["data"]["topic_id"], second["data"]["topic_id"]]
        self.labels = [first["data"]["label"], second["data"]["label"]]
        for record in list_job_records(self.ctx.state_dir, limit=0):  # Their first turns: ended, so a switch may go.
            job_path(self.ctx.state_dir, record["key"]).unlink()
        invalidate_directory(job_path(self.ctx.state_dir, "x").parent)
        self.app.runtime.jobs.by_id.clear()
        self.before = load_topic(self.ctx.state_dir, self.topics[0])["harness_id"]  # The fixture's default Harness.
        self.after = "claude" if self.before != "claude" else "codex"
        self.title = self.app.apps[self.after].spec.title
        upsert_job_record(self.ctx.state_dir, "main-job", chat_id=CHAT, topic_id=butler.butler_topic_id(CHAT), job_id=1,
                          status="running", user_text=f"把 {self.labels[0]} 和 {self.labels[1]} 都换成 {self.title}")
        self.cards = []
        self.app.messenger.send_message = mock.Mock(side_effect=self.sent)
        self.app.messenger.patch_card = mock.Mock(return_value=True)
        self.app.messenger.reply = mock.Mock(return_value="om_notice")
        self.events = iter(range(1, 100))

    def sent(self, profile, chat, text, key, **kwargs):
        self.cards.append(kwargs["card"])
        kwargs["on_card"]("om_batch")
        return True

    def switch(self, request_id="s1", **args):
        fields = {"topics": list(self.labels), "harness": self.after, "basis": f"都换成 {self.title}", **args}
        return batch_switch.topics_switch(self.env, Request(request_id, "topics_switch", fields, "main-job", 0.0))

    def record(self, request_id="s1"):
        from covey.core.butler_opened import request_token

        return json.loads(batch_switch.path(self.ctx.state_dir, request_token(SimpleNamespace(id=request_id))).read_text())

    def click(self, kind, request_id="s1", owner=True, message_id="om_batch"):
        event = {"event_id": f"e{next(self.events)}", "message_id": message_id, "chat_id": CHAT}
        return self.app.batch_switch.claim(self.ctx, event, {"kind": kind, "batch": self.record(request_id)["token"]}, owner)

    def harness(self, index):
        return load_topic(self.ctx.state_dir, self.topics[index])["harness_id"]


class TopicsSwitchTest(BatchCase):
    def test_the_call_sends_one_card_and_switches_nothing(self):
        answer = self.switch()
        self.assertTrue(answer["ok"], answer)
        self.assertIn("点「确认更换」后才换", answer["text"])
        self.assertEqual(answer["data"]["count"], 2)
        self.assertEqual([self.harness(0), self.harness(1)], [self.before, self.before])
        drawn = json.dumps(self.cards[0], ensure_ascii=False)
        for words in ("批量换配置", "等你确认", "确认更换", self.labels[0], "→", self.title, f"主人说：「都换成 {self.title}」"):
            self.assertIn(words, drawn)
        self.assertEqual(self.record()["status"], "pending")
        self.assertEqual(self.switch(), answer)  # The same request: the saved answer, no second card.
        self.assertEqual(self.app.messenger.send_message.call_count, 1)

    def test_rows_it_cannot_switch_are_said_and_a_call_with_none_sends_no_card(self):
        answer = self.switch(topics=[self.labels[0], "T999"])
        self.assertTrue(answer["ok"])
        self.assertIn("T999", answer["text"])
        rows = self.record()["rows"]
        self.assertEqual([row["state"] for row in rows], ["ready", "skip"])
        nothing = self.switch("s2", topics=[self.labels[0]], harness=self.before)
        self.assertFalse(nothing["ok"])
        self.assertIn("已经是这个配置", nothing["text"])
        self.assertEqual(self.app.messenger.send_message.call_count, 1)

    def test_a_call_on_words_the_owner_did_not_say_is_refused(self):
        answer = self.switch(basis="以后所有任务都用 Codex")
        self.assertFalse(answer["ok"])
        self.assertIn("没找到", answer["text"])
        self.app.messenger.send_message.assert_not_called()

    def test_confirm_switches_each_topic_and_tells_its_thread(self):
        self.switch()
        self.assertEqual(self.click(cards.CONFIRM), "batch_switch_confirm")
        self.assertEqual([self.harness(0), self.harness(1)], [self.after, self.after])
        record = self.record()
        self.assertEqual((record["status"], [row["state"] for row in record["rows"]]), ("done", ["done", "done"]))
        topic = load_topic(self.ctx.state_dir, self.topics[0])
        self.assertEqual(topic["binding_revision"], 2)
        self.assertEqual(self.app.messenger.reply.call_count, 2)
        self.assertIn("批量换配置卡上确认", self.app.messenger.reply.call_args.args[2])
        last = json.dumps(self.app.messenger.patch_card.call_args.args[2], ensure_ascii=False)
        self.assertIn("换好 2 个", last)
        self.assertEqual(self.click(cards.CONFIRM), "batch_switch_confirm")  # Decided: only redrawn.
        self.assertEqual(load_topic(self.ctx.state_dir, self.topics[0])["binding_revision"], 2)

    def test_a_busy_topic_is_left_and_switched_by_a_retry_once_it_stopped(self):
        self.switch()
        upsert_job_record(self.ctx.state_dir, "busy", topic_id=self.topics[1], chat_id=CHAT, job_id=7, status="running")
        self.click(cards.CONFIRM)
        rows = self.record()["rows"]
        self.assertEqual([row["state"] for row in rows], ["done", "failed"])
        self.assertIn("再试没换的", rows[1]["why"])
        self.assertIn("再试没换的", json.dumps(self.app.messenger.patch_card.call_args.args[2], ensure_ascii=False))
        upsert_job_record(self.ctx.state_dir, "busy", status="done")
        self.assertEqual(self.click(cards.RETRY), "batch_switch_retry")
        self.assertEqual([row["state"] for row in self.record()["rows"]], ["done", "done"])
        self.assertEqual(self.harness(1), self.after)

    def test_cancel_switches_nothing_and_a_topic_changed_since_is_left_alone(self):
        self.switch()
        self.click(cards.CANCEL)
        self.assertEqual(self.record()["status"], "cancelled")
        self.click(cards.CONFIRM)
        self.assertEqual([self.harness(0), self.harness(1)], [self.before, self.before])
        self.switch("s2")
        topic = load_topic(self.ctx.state_dir, self.topics[0])
        path = self.ctx.state_dir / "topics" / self.topics[0] / "topic.json"
        path.write_text(json.dumps({**topic, "binding_revision": 5}, ensure_ascii=False))
        self.click(cards.CONFIRM, "s2")
        self.assertEqual([row["state"] for row in self.record("s2")["rows"]], ["stale", "done"])
        self.assertEqual(self.harness(0), self.before)

    def test_only_the_owner_and_only_the_card_of_the_record(self):
        self.switch()
        self.assertEqual(self.click(cards.CONFIRM, owner=False), "owner_only")
        self.assertEqual(self.click(cards.CONFIRM, message_id="om_other"), "batch_switch_stale")
        self.assertEqual([self.harness(0), self.harness(1)], [self.before, self.before])
        self.assertIsNone(self.app.batch_switch.claim(self.ctx, {"event_id": "x"}, {"kind": "bridge_job_stop"}, True))

    def test_a_lead_switches_only_its_own_projects_topics(self):
        with mock.patch("covey.app.butler_topics.lead_of", return_value=("p1", "专项")), \
                mock.patch("covey.core.projects.topic_project",
                           side_effect=lambda state, topic: SimpleNamespace(id="p1") if topic == self.topics[0] else None):
            answer = self.switch()
        self.assertTrue(answer["ok"], answer)
        rows = self.record()["rows"]
        self.assertEqual([row["state"] for row in rows], ["ready", "skip"])
        self.assertEqual(rows[1]["why"], "不是这个专项的话题")
        self.assertIn("专项「专项」的负责人提议的", json.dumps(self.cards[0], ensure_ascii=False))


GONE = {"pid": 999_999_999, "start_ticks": "1", "boot_id": "gone"}  # A process that is no more (a restart).


class MidStateTest(BatchCase):
    """WAVE12 C10 (T904): what a restart or a send that went wrong leaves mid-way, and the click after it."""

    def leave(self, request_id="s1", **fields):
        """The record as an earlier process left it."""
        target = batch_switch.path(self.ctx.state_dir, self.record(request_id)["token"])
        target.write_text(json.dumps({**self.record(request_id), **fields}, ensure_ascii=False))

    def running_left(self, owner=GONE):
        rows = self.record()["rows"]
        rows[0] = {**rows[0], "state": "switching"}
        self.leave(status="running", rows=rows, **({"owner": owner} if owner is not None else {}))

    def test_a_switching_whose_process_is_gone_is_settled_by_the_next_click_and_retried(self):
        self.switch()
        self.running_left()
        self.assertEqual(self.click(cards.RETRY), "batch_switch_retry")
        record = self.record()
        self.assertEqual((record["status"], [row["state"] for row in record["rows"]]), ("done", ["done", "done"]))
        self.assertEqual([self.harness(0), self.harness(1)], [self.after, self.after])
        self.assertEqual(record["owner"], batch_switch.me())

    def test_a_confirm_on_one_left_running_settles_it_and_offers_the_retry(self):
        self.switch()
        self.running_left()
        self.click(cards.CONFIRM)
        record = self.record()
        self.assertEqual(record["status"], "done")
        self.assertEqual([row["why"] for row in record["rows"]], list(batch_switch.LEFT_WHY))
        self.assertIn("再试没换的", json.dumps(self.app.messenger.patch_card.call_args.args[2], ensure_ascii=False))
        self.assertEqual([self.harness(0), self.harness(1)], [self.before, self.before])  # Nothing switched by it.

    def test_the_bridges_start_settles_what_a_gone_process_left_running_and_redraws_its_card(self):
        import os
        from covey.core.process import process_identity
        self.switch()
        self.switch("s2", topics=[self.labels[0]])
        self.switch("s3", topics=[self.labels[1]])
        self.running_left()
        self.leave("s2", status="running", owner=process_identity(os.getppid()))  # Alive, another process's: left alone.
        self.leave("s3", status="running")  # An older release's, naming no process: settled.
        self.assertEqual(self.app.batch_switch.settle_all(self.ctx), 2)
        self.assertEqual([self.record(r)["status"] for r in ("s1", "s2", "s3")], ["done", "running", "done"])
        self.assertEqual([row["state"] for row in self.record()["rows"]], ["failed", "failed"])
        self.assertEqual(self.app.messenger.patch_card.call_count, 2)
        self.assertEqual(self.app.batch_switch.settle_all(self.ctx), 0)  # Settled once.

    def test_a_click_on_a_card_whose_record_has_no_message_yet_takes_it(self):
        self.switch()
        record = self.record()
        self.leave(status="sending", message_id="", answer=None)  # The Bridge stopped after the send, before the save.
        self.assertEqual(self.click(cards.CONFIRM), "batch_switch_confirm")
        record = self.record()
        self.assertEqual((record["message_id"], record["status"]), ("om_batch", "done"))
        self.assertTrue(record["answer"]["ok"])
        self.assertEqual([self.harness(0), self.harness(1)], [self.after, self.after])
        self.assertEqual(self.switch(), record["answer"])  # The same request again: the card is out, none sent.
        self.assertEqual(self.app.messenger.send_message.call_count, 1)

    def test_a_record_that_cannot_be_read_now_is_never_written_over(self):
        """G1 (W1-F): a torn record read as {} was a new request (a card again) or a click on nothing written over it."""
        self.switch()
        token = self.record()["token"]
        target = batch_switch.path(self.ctx.state_dir, token)
        body = target.read_bytes()
        target.write_bytes(body[: len(body) // 2])
        torn = target.read_bytes()
        answer = self.switch()
        self.assertFalse(answer["ok"])
        self.assertIn("暂时读不出", json.dumps(answer, ensure_ascii=False))
        self.assertEqual(self.app.messenger.send_message.call_count, 1)  # No second card.
        event = {"event_id": "e-torn", "message_id": "om_batch", "chat_id": CHAT}
        self.assertEqual(self.app.batch_switch.claim(self.ctx, event, {"kind": cards.CONFIRM, "batch": token}, True),
                         "batch_switch_stale")
        self.assertEqual(self.app.batch_switch.decide(self.ctx, target, cards.CONFIRM, token), "ignored")
        self.assertEqual(self.app.batch_switch.adopt(target, "om_batch"), None)
        self.assertEqual(batch_switch.finish(target, status="pending"), False)
        self.assertEqual(target.read_bytes(), torn)
        self.assertEqual([self.harness(0), self.harness(1)], [self.before, self.before])

    def test_a_card_feishu_refused_is_failed_and_the_same_request_sends_it_again_under_its_key(self):
        refused = mock.Mock(return_value=False)
        self.app.messenger.send_message, self.app.messenger.last_outcome = refused, mock.Mock(return_value="not_sent")
        answer = self.switch()
        self.assertFalse(answer["ok"])
        self.assertEqual(self.record()["status"], "failed")
        self.app.messenger.send_message = mock.Mock(side_effect=self.sent)
        self.assertTrue(self.switch()["ok"])
        self.assertEqual((self.record()["status"], self.record()["message_id"]), ("pending", "om_batch"))
        self.assertEqual(refused.call_args.args[3], self.app.messenger.send_message.call_args.args[3])

    def test_a_card_that_may_have_gone_is_never_sent_again_and_a_click_on_it_takes_it(self):
        unsure = mock.Mock(return_value=False)
        self.app.messenger.send_message, self.app.messenger.last_outcome = unsure, mock.Mock(return_value="uncertain")
        answer = self.switch()
        self.assertEqual((answer["ok"], answer["text"]), (False, batch_switch.UNCERTAIN_SENT))
        self.assertEqual((self.record()["status"], self.record()["message_id"]), ("uncertain", ""))
        self.assertEqual(self.switch(), answer)  # The same request: not sent again.
        unsure.assert_called_once()
        self.assertEqual(self.click(cards.CONFIRM, message_id="om_late"), "batch_switch_confirm")
        self.assertEqual((self.record()["message_id"], self.record()["status"]), ("om_late", "done"))
        self.assertEqual(self.click(cards.CONFIRM, message_id="om_other"), "batch_switch_stale")  # Only that card.

    def test_switching_never_stays_running_when_it_breaks_part_way(self):
        self.switch()
        put_row, calls = self.app.batch_switch.put_row, []

        def broken(target, index, **fields):
            calls.append(index)
            if len(calls) == 2:  # The first row's outcome cannot be written.
                raise OSError("disk")
            return put_row(target, index, **fields)
        with mock.patch.object(self.app.batch_switch, "put_row", side_effect=broken):
            with self.assertRaises(OSError):
                self.click(cards.CONFIRM)
        record = self.record()
        self.assertEqual((record["status"], [row["state"] for row in record["rows"]]), ("done", ["failed", "failed"]))
        self.assertEqual([row["why"] for row in record["rows"]], list(batch_switch.BROKEN_WHY))
        self.assertIn("再试没换的", json.dumps(self.app.messenger.patch_card.call_args.args[2], ensure_ascii=False))


class TopicCardBasisTest(ButlerTopicsCase):
    def test_a_topic_opened_on_the_owners_words_says_so_on_its_card(self):
        upsert_job_record(self.ctx.state_dir, "main-job", chat_id=CHAT, topic_id=butler.butler_topic_id(CHAT), job_id=1,
                          status="running", user_text="这个监视进程给 codex 跑")
        answer = self.start("r1", harness="codex", basis="监视进程给 codex")
        self.assertTrue(answer["ok"], answer)
        card = json.dumps(self.app.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False)
        self.assertIn("按「监视进程给 codex」", card)
        plain = self.start("r2")
        self.assertTrue(plain["ok"], plain)
        self.assertNotIn("按「", json.dumps(self.app.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False))
