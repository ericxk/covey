"""T numbers on the butler's cards link to their topics (app/topic_links.py)."""
import json
import os
import time
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from covey.app import topic_links
from covey.app.relay_send import linked
from covey.core import butler
from covey.core.jobs import upsert_job_record

CHAT = "oc_chat"


def detail(message_id, thread="", position="7", thread_position=None):
    item = {"message_id": message_id, "thread_id": thread, "message_position": position}
    if thread_position is not None:
        item["thread_message_position"] = thread_position
    return {"data": {"items": [item]}}


THREAD = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_chat&open_thread_id=omt_1"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.calls = []
        topic_links._index_cache.clear()
        self.addCleanup(self.tmp.cleanup)
        previous = butler.configure_state(self.state)
        self.addCleanup(butler.configure_state, previous)

    def topic(self, topic_id, number, root="", chat=CHAT):
        folder = self.state / "topics" / topic_id
        folder.mkdir(parents=True)
        (folder / "topic.json").write_text(json.dumps({"topic_id": topic_id, "chat_id": chat, "topic_number": number,
                                                       "root_id": root}))

    def fetch(self, replies):
        def fetch(message_id, profile=None):
            self.calls.append(message_id)
            reply = replies.get(message_id)
            if isinstance(reply, Exception):
                raise reply
            return reply or {}
        return fetch


class LinkTest(Case):
    def test_a_threaded_topic_opens_its_thread_and_is_read_once(self):
        self.topic("a" * 32, 404, root="om_root")
        fetch = self.fetch({"om_root": detail("om_root", thread="omt_1", position="715")})
        link = topic_links.link_for(self.state, "a" * 32, fetch=fetch)
        self.assertEqual("https://applink.feishu.cn/client/thread/open?open_chat_id=oc_chat&open_thread_id=omt_1", link)
        self.assertEqual(link, topic_links.link_for(self.state, "a" * 32, fetch=fetch))
        self.assertEqual(["om_root"], self.calls)

    def test_a_threaded_topic_opens_at_its_newest_card(self):
        # The owner, 2026-11-08: 点 T439 只能到话题上，到不了那张答案卡。
        self.topic("a" * 32, 439, root="om_root")
        upsert_job_record(self.state, "om_ask1", topic_id="a" * 32, chat_id=CHAT, job_id=7, card_message_id="om_card7")
        upsert_job_record(self.state, "om_ask2", topic_id="a" * 32, chat_id=CHAT, job_id=9, card_message_id="om_card9")
        fetch = self.fetch({"om_card9": detail("om_card9", thread="omt_1", position="-3", thread_position="5"),
                            "om_card7": detail("om_card7", thread="omt_1", position="-3", thread_position=0)})
        self.assertEqual(THREAD + "&thread_position=5", topic_links.link_for(self.state, "a" * 32, fetch=fetch))
        self.assertEqual(THREAD + "&thread_position=5", topic_links.link_for(self.state, "a" * 32, fetch=fetch))
        self.assertEqual(THREAD + "&thread_position=0",
                         topic_links.link_for(self.state, "a" * 32, card="om_card7", fetch=fetch))
        self.assertEqual(["om_card9", "om_card7"], self.calls)

    def test_a_card_that_cannot_be_read_opens_the_thread(self):
        self.topic("a" * 32, 439, root="om_root")
        upsert_job_record(self.state, "om_ask1", topic_id="a" * 32, chat_id=CHAT, job_id=7, card_message_id="om_card7")
        fetch = self.fetch({"om_card7": OSError("x"), "om_root": detail("om_root", thread="omt_1", position="715")})
        self.assertEqual(THREAD, topic_links.link_for(self.state, "a" * 32, fetch=fetch))

    def test_an_entry_from_before_cards_still_answers_and_reads_are_capped(self):
        self.topic("a" * 32, 404, root="om_root")
        (self.state / topic_links.LINKS).write_text(json.dumps(
            {"a" * 32: {"message_id": "om_root", "thread_id": "omt_1", "position": "715", "at": 1}}))
        self.assertEqual(THREAD, topic_links.link_for(self.state, "a" * 32, fetch=self.fetch({})))
        self.assertEqual([], self.calls)
        replies = {f"om_c{n}": detail(f"om_c{n}", thread="omt_1", thread_position=str(n)) for n in range(12)}
        for n in range(12):
            topic_links.link_for(self.state, "a" * 32, card=f"om_c{n}", fetch=self.fetch(replies))
        kept = json.loads((self.state / topic_links.LINKS).read_text())["a" * 32]
        self.assertEqual(topic_links.KEPT, len(kept["messages"]))
        self.assertIn("om_c11", kept["messages"])
        self.assertEqual("om_root", kept["message_id"])

    def test_no_link_when_the_message_cannot_be_read(self):
        self.topic("c" * 32, 403, root="om_gone")
        self.assertEqual("", topic_links.link_for(self.state, "c" * 32, fetch=self.fetch({"om_gone": OSError("x")})))
        self.assertEqual("", topic_links.link_for(self.state, "c" * 32, fetch=self.fetch({})))


class LinkifyTest(Case):
    def setUp(self):
        super().setUp()
        self.topic("a" * 32, 404, root="om_root")
        self.topic("d" * 32, 405, root="om_other", chat="oc_elsewhere")
        self.fetch_root = self.fetch({"om_root": detail("om_root", thread="omt_1")})
        self.url = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_chat&open_thread_id=omt_1"

    def link(self, text, **kwargs):
        return topic_links.linkify(text, self.state, CHAT, fetch=self.fetch_root, **kwargs)

    def test_bold_and_plain_t_numbers_become_links(self):
        self.assertEqual(f"↪ 转给 [**T404**]({self.url})「迁移」", self.link("↪ 转给 **T404**「迁移」"))
        self.assertEqual(f"已转给 [T404]({self.url})。", self.link("已转给 T404。"))
        self.assertEqual(f"**[T404]({self.url}) 需要你回答**", self.link("**T404 需要你回答**"))

    def test_a_topic_idle_for_weeks_does_not_link(self):
        self.topic("e" * 32, 13, root="om_old")
        old = time.time() - topic_links.RECENT - 60
        os.utime(self.state / "topics" / ("e" * 32) / "topic.json", (old, old))
        topic_links._index_cache.clear()
        self.assertEqual("T13 · #115", self.link("T13 · #115"))

    def test_code_links_other_chats_unknown_numbers_and_skipped_topics_stay(self):
        for text in ("`T404`", "```\nT404\n```", "[T404](https://x)", "T405 在别的聊天", "T999 没有", "AT404X"):
            self.assertEqual(text, self.link(text))
        self.assertEqual("T404 本身", self.link("T404 本身", skip=("a" * 32,)))
        self.assertEqual([], [c for c in self.calls if c != "om_root"])

    def test_only_a_t_number_on_its_own_links(self):
        """ACCEPT-330 #2: a relay's "T444·330 验收结束" opened T444. A T number continued by a dot or dash and a
        letter or digit, or inside a longer word or number, is not one; the usual ways of writing it still are."""
        self.topic("b" * 32, 4040, root="om_far")  # A topic T4040 that is no link here: its message is not read.
        link = f"[T404]({self.url})"
        for text, want in (("T404", link), ("T404「迁移」", link + "「迁移」"), ("（T404）", f"（{link}）"),
                           ("T404。", link + "。"), ("T404.", link + "."), ("T404 · 01:27", link + " · 01:27"),
                           ("T404·验收", link + "·验收"), ("**T404**·验收", f"[**T404**]({self.url})·验收"),
                           ("T404、T404", f"{link}、{link}"), ("T404：做完了", link + "：做完了")):
            with self.subTest(text=text):
                self.assertEqual(want, self.link(text))
        for text in ("T404·330 验收结束", "T404・330", "**T404**·330", "T404a", "T404A 版", "T404.md", "T404.5",
                     "T404-2", "T40400", "aT404", "#T404", "T4040"):
            with self.subTest(text=text):
                self.assertEqual(text, self.link(text))

    def test_a_relay_opens_its_own_turn_and_other_topics_their_newest_card(self):
        self.topic("f" * 32, 406, root="om_root6")
        for topic, job, card in (("a" * 32, 3, "om_old"), ("a" * 32, 4, "om_new"), ("f" * 32, 5, "om_six")):
            upsert_job_record(self.state, f"om_ask{job}", topic_id=topic, chat_id=CHAT, job_id=job,
                              card_message_id=card)
        fetch = self.fetch({"om_old": detail("om_old", thread="omt_1", thread_position="2"),
                            "om_six": detail("om_six", thread="omt_6", thread_position="0")})
        text = topic_links.linkify("T404 · 01:27，详情见 T406", self.state, CHAT, fetch=fetch,
                                   cards={"a" * 32: "om_old"})
        self.assertEqual(f"[T404]({THREAD}&thread_position=2) · 01:27，详情见 [T406]("
                         "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_chat&open_thread_id=omt_6"
                         "&thread_position=0)", text)
        self.assertNotIn("om_new", self.calls)

    def test_a_relay_card_links_its_label(self):
        card = {"body": {"elements": [{"tag": "markdown", "content": "**T404 需要你回答**"},
                                      {"tag": "markdown", "content": "<font color='grey'>在 T404 的问题卡里答</font>"}]}}
        with mock.patch.object(topic_links, "link_for", return_value=self.url):
            built, text = linked(self.state, CHAT, card, "**T404 需要你回答**")
        self.assertEqual(f"**[T404]({self.url}) 需要你回答**", built["body"]["elements"][0]["content"])
        self.assertIn(f"[T404]({self.url})", built["body"]["elements"][1]["content"])
        self.assertEqual(f"**[T404]({self.url}) 需要你回答**", text)

if __name__ == "__main__":
    unittest.main()
