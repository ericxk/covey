"""代发 (app/owner_send.py, core/owner_send.py, cards/owner_send.py): a topic's send_as_owner draft becomes a card in
its thread, only the owner's click sends it as the owner through the 代答 sender, with the text the card's box holds,
and what came of it is kept, drawn and read by the topic's next turn."""
import dataclasses
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import require_extension  # noqa: E402
require_extension()  # The sender is the company's (core/extensions.py hook proxy_sender).
from support.proxy import ProxyCase, lark, user  # noqa: E402
from covey.app import owner_send as module  # noqa: E402
from covey.cards.owner_send import DROP, PICK, SEND, TEXT_FIELD  # noqa: E402
from covey.core import owner_send as records  # noqa: E402
from covey.core.jobs import upsert_job_record  # noqa: E402

TOPIC = "t" * 64
KEY = "om_job1"
ZHANG = user("ou_zs", "张三", email="zhangsan@example.com", department="算法", p2p_chat_id="oc_zs")


class DraftParseTests(unittest.TestCase):
    def test_text_recipient_and_limits(self):
        found = records.drafts({records.FIELD: [
            {"id": "a", "to": {"email": "x@example.com"}, "text": " 你好 ", "why": "通知"},
            {"id": "b", "to": "ou_x", "text": ""},
            {"id": "c", "text": "没有收件人"},
            "不是对象",
            {"id": "d", "to": "张三", "text": "x" * (records.TEXT_LIMIT + 1)},
            {"id": "e", "to": "oc_1", "text": "第四条不要"},
        ]})
        self.assertEqual([d["ref"] for d in found], ["a", "b", "c"])
        self.assertEqual((found[0]["to"], found[0]["text"], found[0]["problem"]), ({"email": "x@example.com"}, "你好", ""))
        self.assertEqual(found[1]["to"], {"open_id": "ou_x"})
        self.assertEqual(found[1]["problem"], "没有正文")
        self.assertEqual(found[2]["problem"], "没有收件人")

    def test_text_file(self):
        with self.subTest("absolute file"), mock.patch.object(Path, "read_text", return_value="AK 在这里\n"), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_size=10)):
            found = records.drafts({records.FIELD: [{"to": {"name": "张三"}, "text_file": "/abs/msg.txt"}]})
            self.assertEqual((found[0]["text"], found[0]["problem"]), ("AK 在这里", ""))
        found = records.drafts({records.FIELD: [{"to": {"name": "张三"}, "text_file": "rel/msg.txt"}]})
        self.assertIn("绝对路径", found[0]["problem"])

    def test_transitions(self):
        self.assertEqual(records.TRANSITIONS["uncertain"], ())
        self.assertNotIn("sending", records.TRANSITIONS["sent"])


class OwnerSendCase(ProxyCase):
    def setUp(self):
        super().setUp()
        self.owner = self.proxy.owner_send
        self.app.messenger.send_message = mock.Mock(return_value="om_line")
        self.jctx = dataclasses.replace(self.ctx, topic_id=TOPIC, reply_to_message_id="om_thread")
        self.enterContext(mock.patch.object(module, "context_for_job", return_value=self.jctx))
        self.enterContext(mock.patch.object(module, "topic_label", return_value="T9「通知」"))
        self.enterContext(mock.patch("covey.app.topic_links.topic_thread_link", return_value=("", "")))
        self.cp = Path(self.root) / "cp.json"
        self.job = SimpleNamespace(message_id=KEY, job_id=9, topic_id=TOPIC, chat_id="oc_owner")
        self.lark.send.return_value = ("om_draft", False)

    def finished(self, drafts, key=KEY, **fields):
        self.cp.write_text(json.dumps({records.FIELD: drafts}, ensure_ascii=False))
        upsert_job_record(self.state, key, **{"status": "done", "verified_reply_authorized": True, "job_id": 9,
                                              "topic_id": TOPIC, "chat_id": "oc_owner", "checkpoint_path": str(self.cp),
                                              **fields})

    def draft(self, to=None, people=(ZHANG,)):
        self.finished([{"id": "zs", "to": to or {"email": "zhangsan@example.com"}, "text": "AK 开好了", "why": "交付"}])
        with mock.patch("covey.app.proxy_commands.user_cli", lark(list(people))):
            self.owner.collect(self.job, self.ctx)
        return records.read(self.state, records.record_id(KEY, "zs"))

    def click(self, record, kind, *, owner=True, box=None, **more):
        value = {"kind": kind, "draft": record["id"], **more}
        event = {"message_id": record["message_id"], "operator_id": "ou_me", "action_value": value,
                 **({"form_value": {TEXT_FIELD: box}} if box is not None else {})}
        return self.owner.click(self.ctx, event, value, owner)


class CollectTests(OwnerSendCase):
    def test_card_in_the_topic_thread_and_a_top_line(self):
        record = self.draft()
        self.assertEqual(record["status"], "drafted")
        self.assertEqual(record["recipient"]["open_id"], "ou_zs")
        self.assertEqual(record["recipient"]["chat_id"], "oc_zs")
        self.assertEqual(record["message_id"], "om_draft")
        self.assertEqual(self.lark.send.call_args.kwargs.get("reply_to_message_id")
                         or self.lark.send.call_args.args[-1], "om_thread")
        top = self.app.messenger.send_message.call_args
        self.assertIn("等你确认", top.args[2])
        self.assertFalse(top.kwargs.get("reply_to_message_id"))

    def test_read_twice_drafts_once(self):
        self.draft()
        with mock.patch("covey.app.proxy_commands.user_cli", lark([ZHANG])):
            self.owner.collect(self.job, self.ctx)
        self.assertEqual(self.lark.send.call_count, 1)

    def test_not_authorized_or_not_finished_is_ignored(self):
        for key, fields in (("om_a", {"verified_reply_authorized": False}), ("om_b", {"status": "failed"})):
            with self.subTest(fields):
                self.finished([{"to": "x@example.com", "text": "hi"}], key=key, **fields)
                self.owner.collect(SimpleNamespace(message_id=key, job_id=9, topic_id=TOPIC, chat_id="oc_owner"), self.ctx)
                self.assertEqual(records.topic_records(self.state, TOPIC), [])

    def test_several_people_of_a_name_are_offered(self):
        other = user("ou_zs2", "张三", email="zhangsan2@example.com", department="生活服务")
        record = self.draft(to={"name": "张三"}, people=(ZHANG, other))
        self.assertEqual(record["view"]["state"], "choose")
        self.assertEqual(len(record["candidates"]), 2)
        self.assertEqual(self.click(record, PICK, pick=1), "owner_send_pick")
        record = records.read(self.state, record["id"])
        self.assertEqual(record["recipient"]["open_id"], "ou_zs2")
        self.assertEqual(record["view"]["state"], "drafted")

    def test_nobody_found(self):
        record = self.draft(people=())
        self.assertEqual(record["status"], "declined")
        self.assertEqual(record["view"]["state"], "unresolved")
        self.assertIn("没查到", record["reason"])

    def test_off_without_switch(self):
        self.finished([{"to": "x@example.com", "text": "hi"}])
        with mock.patch.dict("os.environ", {"COVEY_OWNER_SEND": "0"}):
            self.owner.collect(self.job, self.ctx)
            self.assertEqual(module.turn_note(self.jctx, self.job), "")
        self.assertEqual(records.topic_records(self.state, TOPIC), [])
        self.assertEqual(self.app.messenger.send_message.call_args.args[2], module.OFF)


class ClickTests(OwnerSendCase):
    def sent_with(self, result):
        web_send = mock.Mock(return_value=result)
        self.enterContext(mock.patch.object(self.proxy.web, "web_send", web_send))
        return web_send

    def test_only_the_owner_sends(self):
        web_send = self.sent_with({"ok": True, "submitted": True, "message_id": "om_x"})
        record = self.draft()
        self.assertEqual(self.click(record, SEND, owner=False, box="hi"), "owner_only")
        self.assertEqual(records.read(self.state, record["id"])["status"], "drafted")
        web_send.assert_not_called()

    def test_send_the_edited_text(self):
        web_send = self.sent_with({"ok": True, "submitted": True, "message_id": "om_x"})
        record = self.draft()
        self.assertEqual(self.click(record, SEND, box=" AK 开好了，改过 ", text=records.digest("AK 开好了")), "owner_send")
        _, _, contact, batch, text = web_send.call_args.args
        self.assertEqual(text, "AK 开好了，改过")
        self.assertEqual((contact["target"], contact["chat_id"], batch["id"]), ("ou_zs", "oc_zs", record["id"]))
        self.assertFalse(self.proxy.web_lock.locked())
        record = records.read(self.state, record["id"])
        self.assertEqual((record["status"], record["message_id"], record["edited"]), ("sent", "om_x", True))
        self.assertIn("已以你的身份发给张三", self.app.messenger.send_message.call_args.args[2])
        lines = records.turn_lines(self.state, TOPIC)
        self.assertIn("已发出", lines[0])
        self.assertIn("主人改过正文", lines[0])
        self.assertIn(records.RULE, module.turn_note(self.jctx, self.job))

    def test_a_draft_with_a_blank_left_is_not_sent_until_the_box_is_filled(self):
        # T604 #41: the topic's text and the owner's edit are both checked; the card stays open and says what is left.
        web_send = self.sent_with({"ok": True, "submitted": True, "message_id": "om_x"})
        self.finished([{"id": "zs", "to": {"email": "zhangsan@example.com"}, "text": "会议改到【日期】，请确认", "why": "交付"}])
        with mock.patch("covey.app.proxy_commands.user_cli", lark([ZHANG])):
            self.owner.collect(self.job, self.ctx)
        record = records.read(self.state, records.record_id(KEY, "zs"))
        self.assertIn("没填的占位符", json.dumps(module.owner_send_card(record, {"state": "drafted"}), ensure_ascii=False))
        for box in ("会议改到【日期】，请确认", "会议改到 {{date}}", "会议改到 TODO", "会议改到 xx月xx日"):
            self.assertEqual(self.click(record, SEND, box=box, text=records.digest(record["text"])), "owner_send_refused", box)
        web_send.assert_not_called()
        self.assertEqual(records.read(self.state, record["id"])["status"], "drafted")
        self.assertEqual(self.click(record, SEND, box="会议改到周五下午三点", text=records.digest(record["text"])), "owner_send")
        self.assertEqual(web_send.call_args.args[-1], "会议改到周五下午三点")

    def test_changed_card_is_refused(self):
        web_send = self.sent_with({"ok": True, "submitted": True})
        record = self.draft()
        self.assertEqual(self.click(record, SEND, box="hi", text="000000000000"), "owner_send_stale")
        web_send.assert_not_called()

    def test_value_text_digest_kept_apart_from_form(self):
        # The button's value names the digest under "text"; the form's box is form_value["text"].
        self.sent_with({"ok": True, "submitted": True, "message_id": "om_x"})
        record = self.draft()
        self.assertEqual(self.click(record, SEND, box="", text=records.digest("AK 开好了")), "owner_send_refused")
        self.assertEqual(records.read(self.state, record["id"])["status"], "drafted")

    def test_not_sent_then_sent_again(self):
        web_send = self.sent_with({"ok": False, "submitted": False, "type": "auth", "error": "登录已失效"})
        record = self.draft()
        with mock.patch.object(self.proxy.web_login, "login_gone") as gone:
            self.click(record, SEND, box="AK 开好了", text=records.digest("AK 开好了"))
        gone.assert_called_once()
        record = records.read(self.state, record["id"])
        self.assertEqual((record["status"], record["reason"]), ("unsent", "登录已失效"))
        self.assertEqual(record["view"]["state"], "unsent")
        web_send.return_value = {"ok": True, "submitted": True, "message_id": "om_y"}
        self.assertEqual(self.click(record, SEND, box="AK 开好了", text=records.digest("AK 开好了")), "owner_send")
        self.assertEqual(records.read(self.state, record["id"])["status"], "sent")
        self.assertEqual(web_send.call_count, 2)

    def test_maybe_sent_is_never_sent_again(self):
        web_send = self.sent_with({"ok": False, "submitted": True, "type": "timeout", "error": "300 秒没有结果"})
        record = self.draft()
        with mock.patch.object(self.proxy.web, "read_back", return_value={}):
            self.click(record, SEND, box="AK 开好了", text=records.digest("AK 开好了"))
        record = records.read(self.state, record["id"])
        self.assertEqual(record["status"], "uncertain")
        self.assertEqual(self.click(record, SEND, box="AK 开好了", text=records.digest("AK 开好了")), "owner_send_stale")
        self.assertEqual(web_send.call_count, 1)

    def test_drop(self):
        web_send = self.sent_with({"ok": True, "submitted": True})
        record = self.draft()
        self.assertEqual(self.click(record, DROP), "owner_send_drop")
        self.assertEqual(records.read(self.state, record["id"])["status"], "declined")
        web_send.assert_not_called()
        self.assertIn("没发（主人点了不发）", records.turn_lines(self.state, TOPIC)[0])

    def test_busy_tool_is_not_sent(self):
        web_send = self.sent_with({"ok": True, "submitted": True})
        record = self.draft()
        self.proxy.web_lock.acquire()
        try:
            with mock.patch.object(module, "WEB_WAIT", 0.01):
                self.click(record, SEND, box="AK 开好了", text=records.digest("AK 开好了"))
        finally:
            self.proxy.web_lock.release()
        web_send.assert_not_called()
        self.assertEqual(records.read(self.state, record["id"])["status"], "unsent")


class CardTests(unittest.TestCase):
    def test_masked_and_read_only(self):
        from covey.cards.owner_send import EDIT_LIMIT, editable, masked, owner_send_card
        self.assertEqual(masked("找 zhangsan.zs@example.com 或 13812345678"), "找 zhangsan.zs@… 或 138****5678")
        self.assertTrue(editable("短的正文"))
        self.assertFalse(editable("x" * (EDIT_LIMIT + 1)))
        self.assertFalse(editable("邮箱 a@example.com"))
        record = {"id": "0" * 32, "status": "drafted", "text": "邮箱 a@example.com",
                  "recipient": {"name": "张三", "email": "zhangsan.zs@example.com"}}
        card = json.dumps(owner_send_card(record, {"state": "drafted"}), ensure_ascii=False)
        self.assertNotIn("@example.com", card)
        # No box for the text (it would send the mask); only 先问一句's line, which sends no text (T604).
        self.assertNotIn('"name": "text"', card)
        self.assertEqual(card.count('"tag": "input"'), 1)
        self.assertIn('"name": "ask"', card)
        hidden = json.dumps(owner_send_card({**record, "text": "正文"}, {"state": "drafted", "hide": True}), ensure_ascii=False)
        self.assertNotIn("正文\"", hidden.replace("正文飞书", ""))


class PlaceholderTests(unittest.TestCase):
    def test_what_counts_as_an_unfilled_blank(self):
        for text in ("发给【姓名】", "{{name}} 你好", "密码是 ${PASSWORD}", "TODO 补上", "待填", "xx月xx日开会", "<此处填写>", "[待补充]"):
            self.assertTrue(records.placeholders(text), text)
        for text in ("会议改到周五下午三点", "用 todo 列表跟进", "价格 100元", "x 轴", "xx"):
            self.assertEqual(records.placeholders(text), [], text)
        self.assertEqual(records.placeholders("【a】和【a】和{{b}}"), ["【a】", "{{b}}"])


class ReadOnlyTests(OwnerSendCase):
    def test_a_read_only_text_goes_as_the_topic_wrote_it(self):
        web_send = mock.Mock(return_value={"ok": True, "submitted": True, "message_id": "om_x"})
        self.enterContext(mock.patch.object(self.proxy.web, "web_send", web_send))
        text = "联系 a@example.com\n" + "x" * 1200
        self.finished([{"id": "zs", "to": {"email": "zhangsan@example.com"}, "text": text}])
        with mock.patch("covey.app.proxy_commands.user_cli", lark([ZHANG])):
            self.owner.collect(self.job, self.ctx)
        record = records.read(self.state, records.record_id(KEY, "zs"))
        self.assertEqual(self.click(record, SEND, box="被改的", text=records.digest(text)), "owner_send")
        self.assertEqual(web_send.call_args.args[-1], text)

    def test_card_refused_is_sent_again_without_the_text(self):
        self.lark.api.side_effect = [None, {"code": 0, "data": {"card_id": "rcid"}}]
        record = self.draft()
        self.assertTrue(record["view"].get("hide"))
        self.assertEqual(record["message_id"], "om_draft")


class TurnNoteTests(OwnerSendCase):
    def test_old_outcomes_are_left_out(self):
        records.write(self.state, {"id": "a" * 32, "ref": "old", "topic_id": TOPIC, "status": "sent", "created_at": 1,
                                   "ended_at": time.time() - 2 * 24 * 3600, "to_label": "张三", "history": []})
        self.assertEqual(records.turn_lines(self.state, TOPIC), [])


if __name__ == "__main__":
    unittest.main()
