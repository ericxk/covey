from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from covey.core.attachments import ATTACHMENTS_HEADER  # noqa: E402
from covey.feishu.forward import MERGE_FORWARD_TYPE, render_merge_forward_detail  # noqa: E402
from covey.feishu.media import enrich_incoming_event  # noqa: E402
from covey.feishu.message_parts import collect_message_parts  # noqa: E402
from covey.feishu.message_read import IncomingMedia  # noqa: E402
from covey.feishu.events import REPLY_CONTEXT  # noqa: E402


def merge_item(
    msg_type: str,
    content: dict[str, str] | str,
    *,
    message_id: str,
    sender_id: str = "ou_test_sender",
    sender_type: str = "user",
    create_time: str = "1700000000",
) -> dict:
    body_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {
        "message_id": message_id,
        "msg_type": msg_type,
        "body": {"content": body_content},
        "sender": {"sender_type": sender_type, "sender_id": {"open_id": sender_id}},
        "create_time": create_time,
    }


def message_detail(*items: dict) -> dict:
    return {"data": {"items": list(items)}}


class MediaKeyExtractionTest(unittest.TestCase):
    """Regression: img/file key regexes must not swallow ordinary English words."""

    def test_common_english_words_in_rich_text_are_not_media_keys(self) -> None:
        post_content = {"content": [[{"tag": "text", "text": "please check file lark_bridge_media.py and the image loading bug"}]]}
        texts, image_keys, file_keys = collect_message_parts(post_content)
        self.assertEqual(texts, ["please check file lark_bridge_media.py and the image loading bug"])
        self.assertEqual(image_keys, [])
        self.assertEqual(file_keys, [])

    def test_img_and_file_prefixed_words_are_not_media_keys(self) -> None:
        texts, image_keys, file_keys = collect_message_parts(
            {"tag": "text", "text": "my filename is a.txt, imgs look wrong, see file_path and file_system"}
        )
        self.assertEqual(texts, ["my filename is a.txt, imgs look wrong, see file_path and file_system"])
        self.assertEqual(image_keys, [])
        self.assertEqual(file_keys, [])

    def test_versioned_key_shapes_still_parse(self) -> None:
        self.assertEqual(collect_message_parts("[Image: img_v3_smoke]"), ([], ["img_v3_smoke"], []))
        self.assertEqual(
            collect_message_parts({"tag": "text", "text": "看图 [Image: img_v3_text]"}),
            (["看图"], ["img_v3_text"], []),
        )
        self.assertEqual(collect_message_parts("file_v2_abc123")[2], ["file_v2_abc123"])

    def test_uuid_style_key_shapes_still_parse(self) -> None:
        v2_key = "img_v2_041b28e3-5680-48c2-9af2-497ace79333g"
        legacy_key = "img_7ea74629-9191-4176-998c-2e603c9c5e8g"
        self.assertEqual(collect_message_parts(v2_key)[1], [v2_key])
        self.assertEqual(collect_message_parts(legacy_key)[1], [legacy_key])

    def test_structured_key_fields_bypass_the_text_regex(self) -> None:
        self.assertEqual(collect_message_parts({"image_key": "img_v3_from_dict"})[1], ["img_v3_from_dict"])
        self.assertEqual(
            collect_message_parts({"zh_cn": {"content": [[{"tag": "text", "text": "hello"}, {"tag": "img", "image_key": "img_v3_json"}]]}}),
            (["hello"], ["img_v3_json"], []),
        )


class MergeForwardRenderingTest(unittest.TestCase):
    def test_merge_forward_detail_renders_text_card_and_downloaded_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp)
            calls: list[tuple[str, str, str]] = []

            def fake_download(**kwargs):
                calls.append((kwargs["message_id"], kwargs["kind"], kwargs["file_key"]))
                return IncomingMedia(
                    key=kwargs["file_key"],
                    kind=kwargs["kind"],
                    local_path=media_dir / f"{kwargs['file_key']}.png",
                )

            parent_id = "om_parent"
            detail = {
                "ok": True,
                "data": {
                    "items": [
                        merge_item(MERGE_FORWARD_TYPE, '"Merged and Forwarded Message"', message_id=parent_id),
                        merge_item("text", {"text": "我把任务重跑了，看下指标正常吗？"}, message_id="om_child_1"),
                        merge_item("image", {"image_key": "img_v3_child"}, message_id="om_child_2"),
                        merge_item(
                            "interactive",
                            {
                                "elements": [
                                    [
                                        {"tag": "text", "text": "卡片正文"},
                                        {"tag": "img", "image_key": "img_v3_card"},
                                    ]
                                ]
                            },
                            message_id="om_child_3",
                            sender_type="app",
                        ),
                    ]
                },
            }

            rendered = render_merge_forward_detail(
                detail,
                parent_message_id=parent_id,
                media_dir=media_dir,
                downloader=fake_download,
            )

            self.assertIn("合并转发消息，共 3 条记录", rendered)
            self.assertIn("我把任务重跑了，看下指标正常吗？", rendered)
            self.assertIn("卡片正文", rendered)
            self.assertIn("[图片:", rendered)
            # Each resource is downloaded through the forwarded message that holds it.
            self.assertEqual(
                calls,
                [
                    ("om_child_2", "image", "img_v3_child"),
                    ("om_child_3", "image", "img_v3_card"),
                ],
            )

    def test_merge_forward_detail_truncates_long_threads(self) -> None:
        detail = {
            "ok": True,
            "data": {
                "items": [
                    merge_item("text", {"text": f"msg {idx}"}, message_id=f"om_child_{idx}")
                    for idx in range(5)
                ]
            },
        }

        rendered = render_merge_forward_detail(
            detail,
            parent_message_id="om_parent",
            media_dir=Path("/tmp"),
            max_items=2,
            downloader=lambda **_kwargs: IncomingMedia(key="unused", kind="image", error="unused"),
        )

        self.assertIn("合并转发消息，共 5 条记录", rendered)
        self.assertIn("仅解析前 2 条", rendered)
        self.assertIn("msg 0", rendered)
        self.assertIn("msg 1", rendered)
        self.assertNotIn("msg 2", rendered)

    def test_forwarded_card_keeps_its_header_columns_panels_and_divs(self) -> None:
        # The detail API gives the sender's card JSON (user_card_content), not the flattened form.
        def column_set(text):
            return {"tag": "column_set", "columns": [{"tag": "column", "elements": [{"tag": "markdown", "content": text}]}]}
        card = {
            "header": {"title": {"tag": "plain_text", "content": "Oncall工单信息"}},
            "elements": [
                {"tag": "markdown", "content": "基础信息"},
                column_set("【问题描述】：提交时报没有权限"),
                {"tag": "div", "text": {"tag": "lark_md", "content": "logId 2026"},
                 "fields": [{"is_short": True, "text": {"tag": "plain_text", "content": "级别 P2"}}]},
                {"tag": "collapsible_panel", "header": {"title": {"tag": "lark_md", "content": "详情信息"}},
                 "elements": [column_set("【工单详情】：链接")]},
                {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "认领"}}]},
            ],
        }
        rendered = render_merge_forward_detail(
            message_detail(merge_item("interactive", card, message_id="om_card", sender_type="app")),
            parent_message_id="om_parent",
            media_dir=Path("/tmp"),
            downloader=lambda **_kwargs: IncomingMedia(key="unused", kind="image", error="unused"),
        )

        texts = ["Oncall工单信息", "基础信息", "【问题描述】：提交时报没有权限", "logId 2026", "级别 P2", "详情信息", "【工单详情】：链接"]
        positions = [rendered.index(text) for text in texts]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("认领", rendered)
        # A post's title is not a header: it stays out of the text a command is read from.
        self.assertEqual(collect_message_parts({"title": "标题", "content": [[{"tag": "text", "text": "/pair 1"}]]})[0],
                         ["/pair 1"])

    def test_enrich_merge_forward_uses_injected_detail_and_reports_api_failure(self) -> None:
        event = {
            "message_id": "om_parent",
            "message_type": MERGE_FORWARD_TYPE,
            "content": '"Merged and Forwarded Message"',
        }

        rendered = enrich_incoming_event(
            event,
            Path("/tmp"),
            message_detail={"ok": False, "error": {"message": "no permission"}},
        ).text

        self.assertIn("合并转发消息", rendered)
        self.assertIn("no permission", rendered)

    def test_an_answer_sent_as_a_merge_forward_carries_the_forwarded_messages(self) -> None:
        detail = {"ok": True, "data": {"items": [merge_item(MERGE_FORWARD_TYPE, {}, message_id="om_parent"),
                                                 merge_item("text", {"text": "转发的正文"}, message_id="om_inner")]}}
        enriched = enrich_incoming_event({"message_id": "om_parent", "message_type": MERGE_FORWARD_TYPE,
                                          "content": '"Merged and Forwarded Message"'}, Path("/tmp"),
                                         message_detail=detail)
        self.assertTrue(enriched.display_text.startswith("[合并转发 "))
        self.assertNotIn("转发的正文", enriched.display_text)
        self.assertIn("转发的正文", enriched.attachments)  # The answer's or held note's model text.
        self.assertEqual(enriched.attachments, enriched.text)


class NestedForwardTest(unittest.TestCase):
    def detail(self, *items):
        return {"ok":True,"data":{"items":list(items)}}

    def test_nested_text_image_and_cycle(self):
        root = self.detail(merge_item(MERGE_FORWARD_TYPE, {}, message_id="nested"))
        nested = self.detail(
            merge_item(MERGE_FORWARD_TYPE, {}, message_id="nested"),
            merge_item("text", {"text":"内层正文"}, message_id="text"),
            merge_item("image", {"image_key":"img_v3_nested"}, message_id="image"),
            merge_item(MERGE_FORWARD_TYPE, {}, message_id="root"))
        fetches, downloads = [], []
        def fetch(mid, profile):
            fetches.append(mid)
            return nested
        def download(**kw):
            downloads.append(kw)
            return IncomingMedia(key=kw['file_key'],kind=kw['kind'],local_path=Path('/tmp/image.png'))
        text = render_merge_forward_detail(root,parent_message_id="root",media_dir=Path('/tmp'),message_detail_fetcher=fetch,downloader=download)
        self.assertIn("内层正文",text)
        self.assertIn("/tmp/image.png",text)
        self.assertIn("循环引用",text)
        self.assertEqual(fetches,["nested"])
        self.assertEqual(downloads[0]['message_id'],'image')

    def test_shared_budget_depth_and_fetch_failure(self):
        root = self.detail(merge_item(MERGE_FORWARD_TYPE,{},message_id='nested'))
        calls=[]
        def fetch(mid, profile):
            calls.append(mid)
            return self.detail(merge_item('text',{'text':'first'},message_id='one'),merge_item('text',{'text':'second'},message_id='two'))
        text=render_merge_forward_detail(root,parent_message_id='root',media_dir=Path('/tmp'),max_items=2,message_detail_fetcher=fetch)
        self.assertIn('first',text)
        self.assertNotIn('second',text)
        self.assertIn('累计达到',text)
        calls.clear()
        text=render_merge_forward_detail(root,parent_message_id='root',media_dir=Path('/tmp'),max_depth=1,message_detail_fetcher=fetch)
        self.assertIn('层限制',text)
        self.assertEqual(calls,[])
        text=render_merge_forward_detail(root,parent_message_id='root',media_dir=Path('/tmp'),message_detail_fetcher=lambda *args:{'ok':False,'error':'不可访问'})
        self.assertIn('不可访问',text)

    def test_reply_to_forward_expands_nested_contents(self):
        parent=self.detail(merge_item(MERGE_FORWARD_TYPE,{},message_id='parent'),merge_item(MERGE_FORWARD_TYPE,{},message_id='nested'))
        nested=self.detail(merge_item('text',{'text':'被回复内层正文'},message_id='text'))
        from covey.feishu.forward import render_replied_message_context
        text=render_replied_message_context({'parent_id':'parent'},parent,media_dir=Path('/tmp'),message_detail_fetcher=lambda *args:nested)
        self.assertIn('被回复内层正文',text)


class ReplyContextRenderingTest(unittest.TestCase):
    # A reply in a thread as lark-cli 1.0.98 delivers it (MESSAGE_EVENT_JQ) and the consumer marks
    # it, and the message details Feishu returns for it and its parent (shapes as recorded in live
    # history, 2026-10-26).
    EVENT = {"message_id": "om_reply", "message_type": "text", "chat_id": "oc_chat", "create_time": "1700000000",
             "content": json.dumps({"text": "我回复的是这条"}, ensure_ascii=False),
             "root_id": "om_parent", "reply_to": "om_parent", "thread_id": "omt_reply",
             "mentions": [{"id": "ou_codex", "key": "@_user_1", "name": "Codex"}], REPLY_CONTEXT: True}
    # The same message from a lark-cli before 1.0.69: no reply context, and no marker.
    OLD_CLI = ("reply_to", "root_id", "thread_id", "mentions", REPLY_CONTEXT)

    def enrich(self, drop=(), again=None, **fields):
        """The enriched EVENT without ``drop``, with ``fields``; and the message IDs fetched. A list
        ``again`` gets the same event enriched a second time, over the same media directory."""
        reply_item = merge_item("text", {"text": "你怎么看？"}, message_id="om_reply")
        reply_item.update(root_id="om_parent", parent_id="om_parent", thread_id="omt_reply", chat_id="oc_chat",
                          mentions=[{"id": "ou_codex", "id_type": "open_id", "key": "@_user_1", "name": "Codex",
                                     "tenant_key": "t"}])
        parent_item = merge_item("interactive", {"elements": [[{"tag": "text", "text": "已开启全新 Codex 会话。"}]]},
                                 message_id="om_parent", sender_type="app")
        details = {"om_reply": message_detail(reply_item), "om_parent": message_detail(parent_item)}
        calls: list[str] = []

        def fake_fetch(message_id: str, _profile=None) -> dict:
            calls.append(message_id)
            return details[message_id]
        event = {**{k: v for k, v in self.EVENT.items() if k not in drop}, **fields}
        with tempfile.TemporaryDirectory() as tmp:
            enriched = enrich_incoming_event(event, Path(tmp), fetch_message_details=True,
                                             message_detail_fetcher=fake_fetch)
            if again:
                again.append(enrich_incoming_event(event, Path(tmp), fetch_message_details=True,
                                                   message_detail_fetcher=fake_fetch))
        return enriched, calls

    def test_a_reply_fetches_only_its_parent(self) -> None:
        from covey.feishu.events import MESSAGE_EVENT_JQ
        self.assertIn("reply_to", MESSAGE_EVENT_JQ)  # lark-cli's name for the parent.
        enriched, calls = self.enrich()
        self.assertEqual(calls, ["om_parent"])
        self.assertEqual(enriched.event["parent_id"], "om_parent")
        self.assertEqual(enriched.event["root_id"], "om_parent")
        self.assertEqual(enriched.event["thread_id"], "omt_reply")
        self.assertIn("我回复的是这条", enriched.text)
        self.assertIn("提及信息", enriched.text)
        self.assertIn("@_user_1", enriched.text)
        self.assertIn("话题起始消息（背景参考）", enriched.text)
        self.assertIn("不代表本条消息要补充话题的第一个任务", enriched.text)
        self.assertNotIn("被回复消息：", enriched.text)
        self.assertIn("parent_id: `om_parent`", enriched.text)
        self.assertIn("已开启全新 Codex 会话", enriched.text)

    def test_a_topic_s_first_message_is_read_from_feishu_once(self) -> None:
        # S37: every reply in a topic read it again (msg_get 0.43 s on the way to its queued card).
        again = [None]
        enriched, calls = self.enrich(again=again)
        self.assertEqual(calls, ["om_parent"])  # Not read a second time.
        self.assertEqual(again[1].text, enriched.text)  # The model gets the same text.
        _, calls = self.enrich(again=[None], root_id="om_root")  # A quoted message, not the first: read each time.
        self.assertEqual(calls, ["om_parent", "om_parent"])

    def test_the_event_gives_what_the_messages_own_detail_gave(self) -> None:
        # The own detail is still read first for an event of an older lark-cli, which names no
        # parent, and for a marked one with a root but no reply_to: the prompt, the card text and
        # the reply fields come out as from the event alone. (Another bot's mention shows its
        # open_id from the event where the detail gave its app ID.)
        enriched, _ = self.enrich()
        for drop in (self.OLD_CLI, ("reply_to",)):
            fetched, calls = self.enrich(drop=drop)
            self.assertEqual(calls, ["om_reply", "om_parent"], drop)
            self.assertEqual((enriched.text, enriched.display_text, enriched.attachments),
                             (fetched.text, fetched.display_text, fetched.attachments), drop)
            for key in ("message_id", "message_type", "root_id", "parent_id", "thread_id", "chat_id", "create_time"):
                self.assertEqual(enriched.event.get(key), fetched.event.get(key), (drop, key))

    def test_a_message_that_replies_to_nothing_fetches_nothing(self) -> None:
        enriched, calls = self.enrich(drop=("root_id", "reply_to", "thread_id"))
        self.assertEqual(calls, [])
        self.assertNotIn("parent_id", enriched.text)
        self.assertIn("我回复的是这条", enriched.text)
        self.assertIn("@_user_1", enriched.text)
        topic_root, calls = self.enrich(drop=("root_id", "reply_to"))  # A topic group's root post.
        self.assertEqual((calls, topic_root.text), ([], enriched.text))
        _, calls = self.enrich(drop=("root_id", "reply_to", "thread_id", REPLY_CONTEXT))
        self.assertEqual(calls, ["om_reply", "om_parent"])  # Unmarked: it may be a reply all the same.

    def test_a_message_without_its_type_still_fetches_its_detail(self) -> None:
        enriched, calls = self.enrich(drop=("message_type", "root_id", "reply_to", "thread_id"))
        self.assertEqual(calls, ["om_reply", "om_parent"])  # The detail names the parent.
        self.assertEqual(enriched.event["message_type"], "text")

    def test_explicit_reply_to_later_message_preserves_that_quote(self):
        from covey.feishu.forward import render_replied_message_context
        text = render_replied_message_context(
            {"parent_id": "om_second", "root_id": "om_first", "thread_id": "omt_thread"},
            message_detail(merge_item("text", {"text": "第二轮的具体要求"}, message_id="om_second")),
            media_dir=Path("/unused"))
        self.assertIn("回复上下文", text)
        self.assertIn("被回复消息：", text)
        self.assertIn("第二轮的具体要求", text)
        self.assertNotIn("话题起始消息", text)

    def test_enrich_reply_of_a_slash_message_keeps_its_reply_context(self) -> None:
        # 2026-10-26: slash commands are no longer recognised; "/help" is text like any other.
        enriched, calls = self.enrich(content=json.dumps({"text": "/help"}, ensure_ascii=False))
        self.assertEqual(calls, ["om_parent"])
        self.assertTrue(enriched.text.startswith("/help"))
        self.assertIn("om_parent", enriched.text)  # Its reply context is kept like any message's.
        self.assertEqual(enriched.event["parent_id"], "om_parent")

    def test_an_answer_gets_the_quoted_message_but_shows_only_what_was_written(self) -> None:
        enriched, _ = self.enrich(root_id="om_root")
        self.assertEqual(enriched.display_text, "我回复的是这条")
        self.assertIn("被回复消息：", enriched.attachments)  # An answer or held note sends this to the model.
        self.assertIn("已开启全新 Codex 会话", enriched.attachments)
        self.assertNotIn("我回复的是这条", enriched.attachments)

    def test_an_answer_in_a_topic_does_not_carry_the_topic_root_or_the_mentions(self):
        enriched, _ = self.enrich()  # parent_id == root_id in a thread: the topic's first message.
        self.assertIn("话题起始消息（背景参考）", enriched.text)
        self.assertEqual(enriched.attachments, "")


class ReplyContextMarkerTest(unittest.TestCase):
    """The consumer marks message events only when its lark-cli's schema names the reply context."""

    @staticmethod
    def cli(stdout, code=0, asked=None):
        def argv(profile, *args):
            if asked is not None:
                asked.append((profile, *args))
            return [sys.executable, "-c", f"import sys; sys.stdout.write({stdout!r}); sys.exit({code})"]
        return argv

    def test_only_a_schema_with_the_reply_context_counts(self) -> None:
        from covey.feishu.events import names_reply_context
        schema = lambda *fields: json.dumps({"resolved_output_schema": {"properties": dict.fromkeys(fields, {})}})
        asked = []
        self.assertTrue(names_reply_context(self.cli(schema("reply_to", "root_id", "mentions", "content"), asked=asked), "p"))
        self.assertEqual(asked, [("p", "event", "schema", "im.message.receive_v1", "--json")])
        for argv in (self.cli(schema("root_id", "mentions", "content")),  # Before 1.0.69.
                     self.cli(schema("reply_to", "root_id", "mentions"), code=1),
                     self.cli("unknown command \"schema\""), self.cli("[]"),
                     lambda profile, *args: [str(Path(__file__).parent / "no-such-lark-cli")]):
            self.assertFalse(names_reply_context(argv, "p"))

    def test_the_consumer_marks_each_event_when_its_lark_cli_names_the_context(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        from covey.feishu import events
        for names, expected in ((True, {"message_id": "om_1", REPLY_CONTEXT: True}), (False, {"message_id": "om_1"})):
            handled, logs = [], []

            def stream(**kwargs):
                kwargs["handle_event"]({"message_id": "om_1"})
            with mock.patch.object(events, "names_reply_context", lambda argv, profile: names), \
                    mock.patch.object(events, "consume_event_stream", stream):
                events.consume(SimpleNamespace(profile="p"), events.MESSAGE_EVENT_KEY, shutdown=None, set_process=None,
                               handle=lambda event, ctx: handled.append(event), log=logs.append, retry_seconds=300)
            self.assertEqual(handled, [expected])
            self.assertEqual(len(logs), 0 if names else 1)


if __name__ == "__main__":
    unittest.main()


class AttachmentReceiptTest(unittest.TestCase):
    """What the request panel shows and what the model is told about attachments."""

    POST = ('![Image](img_v3_0216k_a7948a8b-3519-4579-8110-4b4719d852bg)\n'
            '还有两个问题\n<file key="file_v3_0016k_1869e4e0-a592-41b0-a868-a2153e1fa1ag" name="report.html"/>')

    def enrich(self, content, downloads):
        from unittest import mock
        from covey.feishu import message_read

        def fake(**kw):
            ok = downloads.get(kw["file_key"], True)
            path = Path("/tmp/media") / (kw["file_key"] + (Path(kw.get("name") or "x.png").suffix or ".bin"))
            return IncomingMedia(key=kw["file_key"], kind=kw["kind"], local_path=path if ok else None,
                                 error=None if ok else "HTTP 403 no permission")
        with mock.patch.object(message_read, "download_resource", side_effect=fake):
            return enrich_incoming_event({"message_id": "om_x", "message_type": "post", "content": content}, Path("/tmp/media"))

    def test_rendered_attachments_become_a_summary_not_broken_markup(self):
        enriched = self.enrich(self.POST, {})
        for text in (enriched.text, enriched.display_text):
            self.assertNotIn("!(", text)
            self.assertNotIn("![", text)
            self.assertNotIn("<file", text)
        self.assertNotIn("img_v3_", enriched.display_text)
        self.assertEqual(enriched.display_text, "还有两个问题\n附件：图片 1 张、文件 1 个（report.html）")
        self.assertIn("文件 1（report.html）: `/tmp/media/file_v3_0016k_1869e4e0-a592-41b0-a868-a2153e1fa1ag.html`",
                      enriched.text)
        self.assertIn("已下载到本地", enriched.text)

    def test_the_attachment_lines_are_kept_apart_for_answers(self):
        enriched = self.enrich(self.POST, {"file_v3_0016k_1869e4e0-a592-41b0-a868-a2153e1fa1ag": False})
        self.assertTrue(enriched.attachments.startswith(ATTACHMENTS_HEADER))
        self.assertIn("`/tmp/media/img_v3_0216k_a7948a8b-3519-4579-8110-4b4719d852bg.png`", enriched.attachments)
        self.assertIn("文件 1（report.html）: HTTP 403 no permission", enriched.attachments)
        self.assertNotIn("还有两个问题", enriched.attachments)
        plain = enrich_incoming_event({"message_id": "om_y", "message_type": "text",
                                       "content": '{"text": "只有文字"}'}, Path("/tmp/media"))
        self.assertEqual(plain.attachments, "")

    def test_a_failed_download_is_counted_and_named(self):
        enriched = self.enrich(self.POST, {"file_v3_0016k_1869e4e0-a592-41b0-a868-a2153e1fa1ag": False})
        self.assertIn("1 个下载失败", enriched.display_text)
        self.assertIn("附件下载失败（内容未传给模型）", enriched.text)
        self.assertIn("文件 1（report.html）: HTTP 403 no permission", enriched.text)

    def test_quoted_and_forwarded_links_keep_their_address(self):
        post = {"zh_cn": {"content": [[{"tag": "text", "text": "看这个 "},
                                      {"tag": "a", "text": "周报", "href": "https://example.feishu.cn/docx/AbC123"}]]}}
        texts, _, _ = collect_message_parts(post)
        self.assertEqual(texts, ["看这个", "[周报](https://example.feishu.cn/docx/AbC123)"])
        child = {"message_id": "c1", "msg_type": "post", "body": {"content": json.dumps(post)}}
        text = render_merge_forward_detail({"data": {"items": [child]}}, parent_message_id="root", media_dir=Path("/tmp"),
                                           downloader=lambda **_kw: None)
        self.assertIn("https://example.feishu.cn/docx/AbC123", text)

    def test_document_links_say_the_document_was_not_read(self):
        enriched = self.enrich("看下 [周报](https://example.feishu.cn/wiki/AbC123)", {})
        self.assertIn("[周报](https://example.feishu.cn/wiki/AbC123)", enriched.text)
        self.assertIn("没有读取文档正文", enriched.text)
        self.assertNotIn("没有读取文档正文", self.enrich("普通消息 https://example.com/a", {}).text)

    def test_forward_summary_counts_content_and_names_what_was_left_out(self):
        items = [{"message_id": "c%d" % i, "msg_type": "text", "body": {"content": json.dumps({"text": "m%d" % i})}}
                 for i in range(3)]
        items.append({"message_id": "img", "msg_type": "image", "body": {"content": json.dumps({"image_key": "img_v3_a"})}})
        items.append({"message_id": "file", "msg_type": "file",
                      "body": {"content": json.dumps({"file_key": "file_v3_b", "file_name": "日志.txt"})}})
        from unittest import mock
        from covey.feishu import message_read
        outputs = []

        def run(argv, cwd, **_kw):
            out = argv[argv.index("--output") + 1]
            outputs.append(out)
            if argv[argv.index("--type") + 1] == "image":
                (Path(cwd) / out).write_text("x")
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="denied")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(message_read.subprocess, "run", side_effect=run):
            enriched = enrich_incoming_event({"message_id": "om_f", "message_type": MERGE_FORWARD_TYPE, "content": "x"},
                                             Path(tmp), message_detail={"data": {"items": items}})
        self.assertEqual(enriched.display_text, "[合并转发 5 条，图片 1 张，1 个下载失败]")
        self.assertIn("[文件下载失败（日志.txt）", enriched.text)
        self.assertTrue(outputs[-1].endswith(".txt"))  # The forwarded file name reached the download.
        failed = enrich_incoming_event({"message_id": "om_g", "message_type": MERGE_FORWARD_TYPE, "content": "x"},
                                       Path("/tmp"), message_detail={"ok": False, "error": "forbidden"})
        self.assertEqual(failed.display_text, "[合并转发 0 条；未完整展开：展开失败]")
        self.assertIn("没有传给模型", failed.text)

    def test_slow_lark_cli_marks_the_attachment_failed_instead_of_dropping_the_message(self):
        import subprocess
        from unittest import mock
        from covey.feishu import message_read
        def run(argv, **_kw):
            raise subprocess.TimeoutExpired(argv, 60)
        event = {"message_id": "om_slow", "message_type": "image", "parent_id": "om_parent",
                 "content": json.dumps({"image_key": "img_v3_k"})}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(message_read.subprocess, "run", side_effect=run):
            enriched = enrich_incoming_event(event, Path(tmp), fetch_message_details=True)
        self.assertIn("timed out after 60s", enriched.text)
        self.assertNotIn("lark-cli", enriched.text)

    def test_a_slow_or_missing_lark_cli_leaves_an_outgoing_image_link_as_written(self):
        import subprocess
        from unittest import mock
        from covey.feishu import outgoing_images
        for error in (subprocess.TimeoutExpired(["lark-cli"], 60), FileNotFoundError("lark-cli")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "plot.png").write_bytes(b"\x89PNG")
                text = "结果见图：![曲线](plot.png)"
                with mock.patch.object(outgoing_images.subprocess, "run", side_effect=error):
                    shown = outgoing_images.prepare_outgoing_markdown_images(text, base_dirs=[Path(tmp)])
                self.assertEqual(shown, text)

    def test_an_image_found_in_a_legacy_local_state_outbox_logs_a_warning(self):
        import contextlib
        import io
        from unittest import mock
        from covey.feishu import outgoing_images
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / ".local-state" / "outgoing_media"
            outbox.mkdir(parents=True)
            (outbox / "old.png").write_bytes(b"\x89PNG")
            (Path(tmp) / "new.png").write_bytes(b"\x89PNG")
            for name, warned in (("new.png", False), ("old.png", True)):
                with self.subTest(name=name), contextlib.redirect_stderr(io.StringIO()) as err, \
                        mock.patch.object(outgoing_images, "upload_image", return_value="img_v3_fake"):
                    shown = outgoing_images.prepare_outgoing_markdown_images(f"![图]({name})", base_dirs=[Path(tmp)])
                self.assertEqual(shown, "![图](img_v3_fake)")
                self.assertEqual(err.getvalue().count("legacy .local-state path resolved"), int(warned))

    def test_downloaded_file_keeps_its_type(self):
        from unittest import mock
        from covey.feishu import message_read
        with tempfile.TemporaryDirectory() as tmp:
            def run(argv, cwd, **_kw):
                out = argv[argv.index("--output") + 1]
                (Path(cwd) / out).write_text("x")
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(message_read.subprocess, "run", side_effect=run):
                got = message_read.download_resource(message_id="om_x", file_key="file_v3_k", kind="file",
                                              media_dir=Path(tmp), name="验收报告.HTML")
        self.assertEqual(got.local_path.suffix, ".html")


class ForwardTreeTest(unittest.TestCase):
    """Shapes taken from a real 2026-10-19 forward (detail of one merge_forward message)."""

    def item(self, mid, msg_type, content, upper=None):
        row = merge_item(msg_type, content, message_id=mid)
        if upper:
            row["upper_message_id"] = upper
        return row

    def test_one_response_holds_the_whole_tree_and_is_rendered_once(self):
        detail = message_detail(
            self.item("root", MERGE_FORWARD_TYPE, '"Merged and Forwarded Message"'),
            self.item("mid", MERGE_FORWARD_TYPE, '"Merged and Forwarded Message"', "root"),
            self.item("deep", MERGE_FORWARD_TYPE, '"Merged and Forwarded Message"', "mid"),
            self.item("hidden", "text", {"text": "[转发层数过多，内容已被隐藏]"}, "deep"),
            self.item("mid-note", "text", {"text": "中间层正文"}, "mid"),
            self.item("top-note", "text", {"text": "外层正文"}, "root"))
        enriched = enrich_incoming_event({"message_id": "root", "message_type": MERGE_FORWARD_TYPE, "content": "x"},
                                         Path("/tmp"), message_detail=detail,
                                         message_detail_fetcher=lambda *a: self.fail("already in the response"))
        self.assertEqual(enriched.text.count("中间层正文"), 1)
        self.assertEqual(enriched.text.count("外层正文"), 1)
        self.assertEqual(enriched.display_text, "[合并转发 5 条；未完整展开：飞书隐藏了过深的转发层]")

    def test_post_files_array_and_child_message_download_with_fallback(self):
        post = {"title": "", "content": [[{"tag": "text", "text": "看附件"}]],
                "files": [{"file_key": "file_v3_csv", "file_name": "data.csv", "is_folder": False}]}
        detail = message_detail(
            self.item("root", MERGE_FORWARD_TYPE, '"Merged and Forwarded Message"'),
            self.item("om_post", "post", post, "root"),
            self.item("om_img", "post", {"content": [[{"tag": "img", "image_key": "img_v3_x"}]]}, "root"))
        calls = []

        def download(**kw):
            calls.append((kw["message_id"], kw["file_key"], kw.get("name")))
            ok = kw["message_id"] != "om_img"  # This child refuses; the forward itself is tried next.
            return IncomingMedia(key=kw["file_key"], kind=kw["kind"], local_path=Path("/tmp/x") if ok else None,
                                 error=None if ok else "File not in msg.")
        text = render_merge_forward_detail(detail, parent_message_id="root", media_dir=Path("/tmp"), downloader=download)
        self.assertEqual(calls, [("om_post", "file_v3_csv", "data.csv"), ("om_img", "img_v3_x", ""), ("root", "img_v3_x", "")])
        self.assertIn("看附件", text)
        self.assertIn("（data.csv）", text)

    def test_note_sent_with_a_forward_loses_its_paragraph_markup(self):
        enriched = enrich_incoming_event({"message_id": "om_n", "message_type": "text",
                                          "content": "<p>验收测试 F: 看 a &amp; b</p>"}, Path("/tmp"))
        self.assertEqual(enriched.display_text, "验收测试 F: 看 a & b")
        child = merge_item("text", {"text": "<p>留言</p><p>第二段</p>"}, message_id="c")
        text = render_merge_forward_detail(message_detail(child), parent_message_id="root", media_dir=Path("/tmp"))
        self.assertIn("留言\n第二段", text)
        self.assertNotIn("<p>", text)
        self.assertEqual(collect_message_parts("a <p>literal</p>")[0], ["a <p>literal</p>"])
