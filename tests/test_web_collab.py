"""网页的协作 (data contract v31, docs/web.md 协作与三层记忆, COVEY_COLLAB): the page's ``say`` with several picked or a quote of a collaboration goes
by core/collab.py ``route``, ``stop`` with ``collab`` stops that collaboration alone, ``memory_fix`` corrects a project's memory line; the
collaboration records become ``collab`` items, the integrator's continuation turn and a helper's 已协助 line are marked. Without the switch nothing
of it is read, pushed or changed: the say to one and the plain stop are as they were."""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from support.records import write_job, write_topic

from covey.app import collab_checks, web_collect as app_collect
from covey.core import collab, collab_records, memory as core_memory, project_memory, turn_source
from covey.core.history_storage import append_history

from test_web_sync import BUTLER, CHAT, OTHER, PID, T0, TOPIC, WebCase, Ctx

THIRD, FOURTH, FIFTH = "e" * 32, "f" * 32, "a" * 32
ON = {turn_source.SWITCH: "1"}


class Gateway:
    """The Bridge's ``collab`` part as the page calls it: what it was asked, and what it answers."""

    def __init__(self):
        self.calls, self.start_answer, self.clarify_answer = [], None, {"collab": {}}

    def start(self, ctx, chat, **fields):
        self.calls.append(("start", chat, fields))
        return self.start_answer or {"collab": {"id": "c1"}}

    def clarify(self, ctx, collab_id, helper, question):
        self.calls.append(("clarify", collab_id, helper, question))
        return self.clarify_answer

    def stop(self, ctx, collab_id, by="card"):
        self.calls.append(("stop", collab_id, by))
        return {"collab": {}}


class CollabCase(WebCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)
        self.gateway = Gateway()
        self.env.collab = self.gateway
        self.runtime.access.is_owner = lambda open_id: open_id == "ou_owner"
        self.env.topics.target = self.reach
        self.topic(TOPIC, 7, "整理账单")
        self.topic(OTHER, 8, "对账脚本")
        for number, key in enumerate((THIRD, FOURTH, FIFTH), 9):
            write_topic(self.state, topic_id=key, chat_id=CHAT, harness_id="claude", topic_number=number, origin={"title": f"话题{number}"})

    def reach(self, state, topic, chat):
        if chat != CHAT or topic not in (TOPIC, OTHER, THIRD, FOURTH, FIFTH):
            return None
        return self.app, Ctx(topic, state), {}

    def say(self, op_id, topic=TOPIC, **payload):
        self.web.op("say", len(self.web.outbox) + 1, op_id, topic, **payload)
        self.sync.inbox_once()
        return self.web.acks[-1]["state"], self.web.acks[-1]["message"]

    def collaboration(self, cid="c" * 24, integrator=OTHER, helpers=(THIRD,), asks=(), **fields):
        """A collaboration record of the chat, written as the Bridge writes it; ``asks``: [(to, [states it goes through], {fields})]."""
        def edit(_):
            record = collab_records.new_record(cid, CHAT, root={"message_id": "web-op-1", "job_key": "k1", "job_id": 5, "words": "请核对对账"},
                                               integrator=collab_records.participant(integrator, collab.TOPIC, label="T8"),
                                               helpers=[collab_records.participant(key, collab.TOPIC, label=f"T{9 + i}", named=True)
                                                        for i, key in enumerate(helpers)], goal_rev=5, source={}, now=T0 - 100)
            for to, states, extra in asks:
                ask = collab_records.new_ask(cid, to=to, frm=integrator, number=1, text="账对得上吗", channel="collab-x", now=T0 - 90)
                for state in states:
                    collab_records.move_ask(ask, state, T0 - 50)
                ask.update(extra)
                record["asks"].append(ask)
            record.update(fields)
            return record
        collab_records.change(self.state, cid, edit)
        return cid

    def acked(self, index=-1):
        return self.web.acks[index]["state"], self.web.acks[index]["message"]


class SayTest(CollabCase):
    def test_several_picked_in_the_room_start_a_collaboration_whose_integrator_is_the_first_topic(self):
        state, message = self.say("op-s1", BUTLER, room=True, text="三位核对一下账", to=[TOPIC, OTHER, THIRD])
        self.assertEqual(state, "done")
        self.assertTrue(message.startswith("已发起协作："), message)
        kind, chat, fields = self.gateway.calls[0]
        self.assertEqual((kind, chat, fields["integrator"], [ask["to"] for ask in fields["asks"]], fields["owner_named"]),
                         ("start", CHAT, TOPIC, [OTHER, THIRD], [TOPIC, OTHER, THIRD]))
        self.assertEqual((fields["root"]["message_id"], fields["root"]["words"], fields["asserted_by"]),
                         ("web-op-s1", "三位核对一下账", turn_source.PARTY_OWNER))
        self.assertEqual({ask["question"] for ask in fields["asks"]}, {"三位核对一下账"})
        self.assertEqual(self.app.queued, [])

    def test_the_owner_picks_the_integrator(self):
        self.say("op-s2", BUTLER, room=True, text="核对", to=[TOPIC, OTHER], integrator=OTHER)
        fields = self.gateway.calls[0][2]
        self.assertEqual((fields["integrator"], [ask["to"] for ask in fields["asks"]]), (OTHER, [TOPIC]))

    def test_in_a_topic_the_picked_ones_are_helpers_and_the_topic_integrates(self):
        state, _ = self.say("op-s3", TOPIC, text="帮我看下", to=[OTHER])
        fields = self.gateway.calls[0][2]
        self.assertEqual((state, fields["integrator"], [ask["to"] for ask in fields["asks"]]), ("done", TOPIC, [OTHER]))

    def test_a_started_collaboration_that_was_started_before_says_so(self):
        self.gateway.start_answer = {"collab": {}, "again": True}
        self.assertIn("已经发起过", self.say("op-s4", BUTLER, room=True, text="核对", to=[TOPIC, OTHER])[1])

    def test_a_refusal_of_the_start_is_the_failure(self):
        self.gateway.start_answer = {"refused": "协作只在主人的私聊里发起"}
        self.assertEqual(self.say("op-s5", BUTLER, room=True, text="核对", to=[TOPIC, OTHER]), ("failed", "协作只在主人的私聊里发起"))

    def test_one_picked_is_the_single_target_as_before(self):
        state, message = self.say("op-s6", TOPIC, room=True, text="你好", to=[TOPIC])
        self.assertEqual((state, message, self.gateway.calls), ("done", "已开下一轮", []))
        self.assertEqual([(topic, text) for topic, _, text in self.app.queued], [(TOPIC, "你好")])
        self.assertEqual(self.say("op-s7", TOPIC, text="再说", to=[TOPIC])[0], "done")  # The topic page's own topic.

    def test_more_than_three_helpers_are_refused_before_anything_is_sent(self):
        state, message = self.say("op-s8", BUTLER, room=True, text="都来", to=[TOPIC, OTHER, THIRD, FOURTH, FIFTH])
        self.assertEqual((state, self.gateway.calls, self.app.queued), ("failed", [], []))
        self.assertIn("一次最多请 3 位", message)

    def test_only_leads_picked_go_to_the_butler_once_told_whom(self):
        self.butler_topic()
        self.hearing()
        lead = lambda state, chat, key: collab.Participant(key, collab.LEAD) if key in (TOPIC, OTHER) else None  # noqa: E731
        with mock.patch.object(collab_checks, "participant", side_effect=lead):
            state, message = self.say("op-s9", BUTLER, room=True, text="这件事怎么办", to=[TOPIC, OTHER])
        self.assertEqual((state, message, self.gateway.calls), ("done", "已交给管家", []))
        said = json.loads(self.heard[0]["content"])["text"]
        self.assertTrue(said.startswith("（主人在网页上点了"), said)
        self.assertTrue(said.endswith("这件事怎么办"), said)

    def test_a_quote_of_a_collaboration_asks_each_picked_again(self):
        cid = self.collaboration(integrator=OTHER, helpers=(THIRD,))
        state, message = self.say("op-q1", BUTLER, room=True, text="再核对一次", to=[THIRD], quote={"topic_key": OTHER, "collab": cid})
        self.assertEqual((state, self.gateway.calls), ("done", [("clarify", cid, THIRD, "再核对一次")]))
        self.assertTrue(message.startswith("已再问 "), message)
        self.gateway.clarify_answer = {"refused": "上一次还没有答复"}
        state, message = self.say("op-q2", BUTLER, room=True, text="再核对一次", to=[THIRD], quote={"topic_key": OTHER, "collab": cid})
        self.assertEqual(state, "failed")
        self.assertIn("上一次还没有答复", message)

    def test_a_quote_of_a_collaboration_with_no_one_picked_goes_to_its_integrator_as_the_owners_words(self):
        cid = self.collaboration(integrator=OTHER, helpers=(THIRD,))
        state, _ = self.say("op-q3", BUTLER, room=True, text="补一句", quote={"topic_key": OTHER, "collab": cid})
        self.assertEqual((state, self.gateway.calls, [(topic, text) for topic, _, text in self.app.queued]), ("done", [], [(OTHER, "补一句")]))
        self.assertEqual(self.say("op-q4", BUTLER, room=True, text="x", quote={"topic_key": OTHER, "collab": "z" * 24})[0], "failed")

    def test_without_the_switch_the_new_fields_change_nothing_for_one_target_and_the_rest_fail_saying_so(self):
        with mock.patch.dict(os.environ, {turn_source.SWITCH: "0"}):
            self.assertEqual(self.say("op-o1", TOPIC, text="你好", to=[OTHER]), ("done", "已开下一轮"))  # Not asked: to the topic as before.
            self.assertEqual([(topic, text) for topic, _, text in self.app.queued], [(TOPIC, "你好")])
            for op, fields in (("op-o2", {"to": [TOPIC, OTHER]}), ("op-o3", {"quote": {"topic_key": OTHER, "collab": "c" * 24}})):
                state, message = self.say(op, TOPIC, text="核对", **fields)
                self.assertEqual(state, "failed")
                self.assertIn("COVEY_COLLAB", message)
        self.assertEqual(self.gateway.calls, [])
        self.env.collab = None
        state, message = self.say("op-o4", TOPIC, text="核对", to=[TOPIC, OTHER])
        self.assertEqual(state, "failed")
        self.assertIn("没有协作部件", message)
        self.assertEqual(self.say("op-o5", TOPIC, text="你好", to=[OTHER])[0], "done")  # Without the part too: as before.


class StopTest(CollabCase):
    def test_stop_with_a_collaboration_stops_that_one_only(self):
        cid = self.collaboration(integrator=OTHER, helpers=(THIRD,), asks=[(THIRD, [collab.QUEUED], {})])
        self.web.op("stop", 1, "op-t1", OTHER, collab=cid)
        self.sync.inbox_once()
        state, message = self.acked()
        self.assertEqual((state, self.gateway.calls, self.app.stopped), ("done", [("stop", cid, "web")], []))
        self.assertIn("已停止这次协作", message)
        self.assertIn("1 次咨询", message)

    def test_a_collaboration_that_is_over_has_nothing_to_stop(self):
        cid = self.collaboration(state=collab.DONE)
        self.web.op("stop", 1, "op-t2", OTHER, collab=cid)
        self.web.op("stop", 2, "op-t3", OTHER, collab="z" * 24)
        self.sync.inbox_once()
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual([row["state"] for row in self.web.acks], ["done", "failed"])

    def test_without_the_switch_a_collab_stop_fails_and_never_stops_the_integrators_task(self):
        cid = self.collaboration(integrator=OTHER)
        self.jobs[OTHER] = [SimpleNamespace(job_id="8", message_id="om_8", status="running")]
        with mock.patch.dict(os.environ, {turn_source.SWITCH: "0"}):
            self.web.op("stop", 1, "op-t4", OTHER, collab=cid)
            self.sync.inbox_once()
        self.assertEqual(self.acked()[0], "failed")
        self.assertIn("COVEY_COLLAB", self.acked()[1])
        self.assertEqual((self.gateway.calls, self.app.stopped), ([], []))


class MemoryFixTest(CollabCase):
    """v31's ``memory_fix`` is gone (T908 M3b): an older page's correction is refused as an operation not known, and the line
    stays as it was (the page changes the memory through ``memory_edit`` v43)."""

    def setUp(self):
        super().setUp()
        self.entry, _ = project_memory.add(self.state, PID, "对账以月底为准", "constraint", "lead")

    def test_an_older_pages_correction_is_refused_and_changes_nothing(self):
        for n, (op, payload) in enumerate((("op-m1", {"what": "confirm"}), ("op-m2", {"what": "scope", "applies": "只在迁移账单时"}))):
            self.web.op("memory_fix", n + 1, op, project=PID, project_key=PID, entry_id=self.entry["id"], **payload)
            self.sync.inbox_once()
            state, message = self.acked()
            self.assertEqual(state, "failed")
            self.assertIn("不认识的操作", message)
        row = next(row for row in project_memory.entries(self.state, self.reload()) if row["id"] == self.entry["id"])
        self.assertEqual(core_memory.describe(row)["level"], "agent")
        self.assertNotIn("applies", row)


class PushTest(CollabCase):
    def items(self, kind="collab"):
        return {item["key"]: item["data"] for item in self.web.items(kind)}

    def helped(self):
        """A collaboration of T8 asking T7 (TOPIC), answered, on the answer of T7's task #7."""
        reply = {"text": "对得上，差 3 分钱", "at": T0 - 40, "job_id": 7, "goal_rev": 5, "owner_since": 0,
                 "evidence": {"level": collab.FORKED, "helper": TOPIC, "job_id": 7}}
        return self.collaboration(integrator=OTHER, helpers=(TOPIC,), asks=[(TOPIC, [collab.QUEUED, collab.TAKEN, collab.REPLIED],
                                                                          {"reply": reply, "evidence": reply["evidence"]})])

    def test_a_collaboration_is_one_item_with_its_asks_their_words_and_the_answer(self):
        cid = self.helped()
        self.sync.push_once(T0)
        data = self.items()[cid]
        self.assertEqual((data["key"], data["integrator"], data["helpers"], data["state"], data["goal_rev"]), (cid, OTHER, [TOPIC], "open", "5"))
        self.assertEqual(data["root"], {"message_id": "web-op-1", "job": "k1", "words": "请核对对账"})
        ask = data["asks"][0]
        self.assertEqual((ask["from"], ask["to"], ask["number"], ask["state"], ask["words"], ask["text"], ask["late"]),
                         (OTHER, TOPIC, 1, "replied", "已答复", "账对得上吗", None))
        self.assertEqual((ask["reply"]["text"], ask["reply"]["at"], ask["reply"]["topic_key"]), ("对得上，差 3 分钱", int(T0 - 40), TOPIC))
        self.assertEqual(ask["reply"]["evidence"][0]["text"], collab.EVIDENCE_WORDS[collab.FORKED])
        self.assertEqual(ask["reply"]["evidence"][0]["topic_key"], TOPIC)
        turns = self.items("turn")
        seq = next(turn["seq"] for key, turn in turns.items() if turn["topic_key"] == TOPIC and turn["role"] == "agent" and turn.get("job_id") == "7")
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.items()[cid]["asks"][0]["reply"]["seq"], seq)
        self.assertEqual(self.items()[cid]["asks"][0]["reply"]["evidence"][0]["seq"], seq)

    def test_an_ask_that_was_not_taken_has_its_reason_and_a_late_answer_its_words(self):
        cid = self.collaboration(integrator=OTHER, helpers=(TOPIC, THIRD), asks=[
            (TOPIC, [collab.NOT_TAKEN], {"reason": "对方在等主人回答问题"}),
            (THIRD, [collab.QUEUED, collab.CANCELLED], {"reason": "主人停了这件事", "late": {"text": "迟到的答复", "at": T0 - 5}})])
        self.sync.push_once(T0)
        asks = self.items()[cid]["asks"]
        self.assertEqual([(ask["state"], ask["reason"], ask["reply"]) for ask in asks],
                         [("not_taken", "对方在等主人回答问题", None), ("cancelled", "主人停了这件事", None)])
        self.assertEqual(asks[1]["late"], {"text": "迟到的答复", "at": int(T0 - 5)})

    def test_only_a_changed_collaboration_goes_again_and_one_that_is_gone_is_deleted(self):
        cid = self.helped()
        self.sync.push_once(T0)
        sent = len(self.web.items("collab"))
        self.sync.push_once(T0 + 1)
        self.assertEqual(len(self.web.items("collab")), sent)
        collab_records.change(self.state, cid, lambda record: {**record, "state": collab.CANCELLED, "stopped_by": "web"})
        self.sync.push_once(T0 + 2)
        self.assertEqual(self.items()[cid]["state"], "cancelled")
        (self.state / "collab" / f"{cid}.json").unlink()
        self.sync.push_once(T0 + 100)
        self.assertIn({"type": "collab", "key": cid}, [row for batch in self.web.upserts for row in batch["deleted"]])

    def test_another_chats_collaboration_is_not_pushed(self):
        cid = self.helped()
        collab_records.change(self.state, cid, lambda record: {**record, "chat_id": "oc_other"})
        self.sync.push_once(T0)
        self.assertEqual(self.items(), {})

    def test_the_continuation_turn_of_the_integrator_is_marked_and_its_prompt_is_not_pushed(self):
        cid = self.helped()
        message = collab.reply_message(cid, 1)
        long = "协作的答复到齐了。" + "很长的提示 " * 200
        append_history(self.state / "topics" / OTHER / "history.jsonl", "user", long, {"message_id": message, "chat_id": CHAT})
        append_history(self.state / "topics" / OTHER / "history.jsonl", "assistant", "对得上", {"job_id": 31})
        write_job(self.state, "k-reply", topic_id=OTHER, chat_id=CHAT, job_id=31, status="done", message_id=message, model="opus")
        self.sync.push_once(T0)
        turns = sorted((item["data"] for item in self.web.items("turn") if item["data"]["topic_key"] == OTHER), key=lambda turn: turn["seq"])
        mine = [turn for turn in turns if turn.get("collab")]
        self.assertEqual([(turn["role"], turn["collab"]) for turn in mine],
                         [("owner", {"key": cid, "role": "integrator"}), ("agent", {"key": cid, "role": "integrator"})])
        self.assertEqual((mine[0]["body"], mine[0]["display"]), (app_collect.CONTINUED_WORDS, app_collect.CONTINUED_WORDS))
        self.assertEqual(mine[1]["body"], "对得上")
        self.assertFalse([turn for turn in turns if turn.get("collab") is None and "很长的提示" in turn["body"]])

    def test_a_helper_gets_one_event_line_that_changes_nothing_else_of_its_topic(self):
        self.sync.push_once(T0)
        before = self.items("topic")[TOPIC]
        cid = self.collaboration(integrator=OTHER, helpers=(TOPIC,), asks=[(TOPIC, [collab.QUEUED, collab.TAKEN], {})])
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        line = self.items("turn")[f"{TOPIC}:collab-{cid}"]
        self.assertEqual((line["role"], line["status"], line["collab"], line["topic_key"]), ("event", "running", {"key": cid, "role": "helper"}, TOPIC))
        self.assertIn("协助 T8", line["body"])
        topic = self.items("topic")[TOPIC]
        self.assertLessEqual(line["seq"], topic["last_seq"])
        self.assertEqual({key: topic[key] for key in ("unseen", "last_seq", "state", "round", "tone")},
                         {key: before[key] for key in ("unseen", "last_seq", "state", "round", "tone")})
        reply = {"text": "好了", "at": T0, "job_id": 7, "evidence": {"level": collab.FORKED, "job_id": 7}}
        collab_records.change(self.state, cid, lambda record: record.update(asks=[
            collab_records.move_ask(dict(record["asks"][0]), collab.REPLIED, T0, reply=reply)]) or record)
        self.sync.push_once(T0 + 2)
        done = self.items("turn")[f"{TOPIC}:collab-{cid}"]
        self.assertEqual((done["status"], done["body"]), ("done", "已协助 T8（看这次协作）"))
        self.assertLessEqual(done["seq"], self.items("topic")[TOPIC]["last_seq"])

    def test_a_helper_that_did_not_take_the_ask_has_no_line(self):
        self.collaboration(integrator=OTHER, helpers=(TOPIC,), asks=[(TOPIC, [collab.NOT_TAKEN], {"reason": "x"})])
        self.sync.push_once(T0)
        self.assertEqual([key for key in self.items("turn") if "collab" in key], [])

    def test_without_the_switch_nothing_is_read_pushed_or_marked(self):
        cid = self.helped()
        message = collab.reply_message(cid, 1)
        append_history(self.state / "topics" / OTHER / "history.jsonl", "user", "长提示", {"message_id": message, "chat_id": CHAT})
        write_job(self.state, "k-reply", topic_id=OTHER, chat_id=CHAT, job_id=31, status="done", message_id=message, model="opus")
        with mock.patch.dict(os.environ, {turn_source.SWITCH: "0"}), \
                mock.patch.object(app_collect.collab_records, "read", side_effect=AssertionError("collab/ was read")), \
                mock.patch.object(app_collect.collab_records, "records", side_effect=AssertionError("collab/ was read")):
            self.sync.push_once(T0)
        self.assertEqual(self.web.items("collab"), [])
        self.assertFalse([item for item in self.web.items("turn") if "collab" in item["key"] or item["data"].get("collab")])
        owners = [item["data"] for item in self.web.items("turn") if item["data"]["topic_key"] == OTHER and item["data"]["role"] == "owner"]
        self.assertEqual([turn["body"] for turn in owners], ["你好", "长提示"])
        self.assertFalse([turn for turn in owners if "display" in turn])


if __name__ == "__main__":
    unittest.main()
