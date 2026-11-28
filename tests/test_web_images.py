"""网页工作台的图片 (app/web_images.py, data contract v19): the images the owner sent and the ones a task showed him go up to the page once
each (a thumbnail and a large one, POST /sync/image) and the turn carries their ids; no local path is ever in what is pushed (the attachment
block becomes its "附件：…" line); only received files of this state directory are read; an image of a message older than KEEP_DAYS is
``gone`` and not sent; a page that does not take images holds them back and the items go on."""
from __future__ import annotations

import base64
import io
import json
import struct
import threading
import unittest
import zlib
from unittest import mock

try:  # Pillow is optional (app/web_images.py): without it the tests of what it makes skip, the rest still run.
    from PIL import Image
except ImportError:  # pragma: no cover - depends on the machine
    Image = None

needs_pillow = unittest.skipUnless(Image, "needs Pillow (optional): the thumbnail and the large image are made by it")

from covey.app import web_images
from covey.app.source_watch import EDIT_HEAD
from covey.core.attachments import ATTACHMENTS_HEADER, ATTACHMENTS_HINT, FAILED_HEADER, attachment_line
from covey.core.history_storage import append_history
from covey.core.state import write_json
from support.records import write_job, write_topic

from test_project_lead import CHAT
from test_web_sync import KEY, T0, TOPIC, Web, WebCase


class ImageWeb(Web):
    """The fake page, which also takes images: ``images`` (id, v) -> the request; ``have`` the ids it says it has; ``refuse``
    a status for the image paths alone."""

    def __init__(self, test):
        self.images, self.have, self.refuse, self.refuse_image = {}, set(), 0, 0
        super().__init__(test)

    def handle(self, handler, body):
        if handler.path.startswith("/sync/image") and handler.headers.get("Authorization") == "Bearer " + KEY:
            self.seen.append((handler.command, handler.path, None))
            request = json.loads(body)
            status, answer = 200, {"ok": True}
            if self.refuse or (self.refuse_image and handler.path == "/sync/image"):
                status, answer = self.refuse or self.refuse_image, {"ok": False}
            elif handler.path.startswith("/sync/images/have"):
                answer = {"have": [each for each in request["ids"] if each in self.have]}
                if self.limits is not None:  # Web.limits: a page of data contract v44 says what it takes here too.
                    answer["limits"] = self.limits
            else:
                self.images[(request["id"], request["v"])] = request
            payload = json.dumps(answer).encode()
            handler.send_response(status)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return
        super().handle(handler, body)


def png(path, size=(1200, 800), color=(30, 120, 200)):
    """A one-colour RGB PNG at ``path``, written with the standard library so a machine without Pillow has it too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = size

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\0" + bytes(color) * width
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))
    return path


class ImagesCase(WebCase):
    def setUp(self):
        super().setUp()
        self.web = ImageWeb(self)
        write_json(self.state / "web.json", {"base_url": self.web.server.base_url, "key_file": str(self.state / "key.txt")})
        self.media = self.state / "topics" / TOPIC / "incoming_media"

    def said_with(self, *paths, words="看这张图", display=True, time=None):
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, origin={"title": "截图"},
                    workspace_name="ws1")
        write_job(self.state, "k-img", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="done", model="opus", message_id="om_1")
        block = [ATTACHMENTS_HEADER, *(attachment_line("image", n, path) for n, path in enumerate(paths, 1)), ATTACHMENTS_HINT]
        content = words + "\n\n" + "\n\n".join(block)
        event = {"message_id": "om_1", **({"display_content": f"{words}\n附件：图片 {len(paths)} 张"} if display else {})}
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", content, event)
        if time is not None:  # An older message: its line's time.
            rows = history.read_text().splitlines()
            row = json.loads(rows[-1])
            row["time"] = time
            history.write_text("\n".join([*rows[:-1], json.dumps(row, ensure_ascii=False)]) + "\n")
        append_history(history, "assistant", "看到了", {"message_id": "om_a"})

    def owner_turn(self):
        return [item for item in self.web.items("turn") if item["data"]["role"] == "owner"][-1]["data"]

    def pushed_text(self):
        return json.dumps([batch for batch in self.web.upserts], ensure_ascii=False)


class ScrubTest(ImagesCase):
    def test_the_attachment_block_becomes_its_line_and_other_paths_a_word(self):
        path = "/x/state/topics/ab/incoming_media/om_1-1-img.png"
        block = "\n".join([ATTACHMENTS_HEADER, "", attachment_line("image", 1, path), attachment_line("file", 1, "/x/a.pdf", "a.pdf"),
                           "", ATTACHMENTS_HINT])
        self.assertEqual(web_images.scrub("看看\n\n" + block), "看看\n\n附件：图片 1 张、文件 1 个")
        self.assertEqual(web_images.scrub("看看\n附件：图片 1 张\n" + block), "看看\n附件：图片 1 张")  # A display text has the line already.
        self.assertEqual(web_images.scrub(f"截图: {path}\n再看 `{path}`"), "截图: （图片）\n再看 （图片）")
        self.assertEqual(web_images.scrub("好\n" + FAILED_HEADER + "\n- 图片 1: timeout"), "好\n附件：1 个下载失败")
        self.assertEqual(web_images.scrub("没有附件的话"), "没有附件的话")


class SentImageTest(ImagesCase):
    @needs_pillow
    def test_an_image_the_owner_sent_goes_up_once_and_its_turn_carries_its_id_not_its_path(self):
        path = png(self.media / "om_1-1-img_v3.png")
        self.said_with(path, display=False)  # An answer's line has no display text: its words were pushed whole, path and all.
        self.sync.push_once(T0)
        # The pass's step read it and sent the image; the quick look after it built the topic again, now with it (T673: before, the
        # turn went out without it and got it a step later).
        ident = web_images.file_id(path)
        self.assertEqual(self.owner_turn()["images"], [{"id": ident, "kind": "sent", "w": 1200, "h": 800}])
        self.assertEqual(len([item for item in self.web.items("turn") if item["key"] == f"{TOPIC}:1"]), 1)  # Only that read of it.
        self.assertEqual(sorted(self.web.images), [(ident, "full"), (ident, "thumb")])
        thumb = self.web.images[(ident, "thumb")]
        self.assertEqual((thumb["mime"], thumb["w"], thumb["h"]), ("image/webp", 1200, 800))
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(thumb["data"]))).size, (480, 320))
        self.sync.push_once(T0 + 1)  # Its topic is built again, now with it.
        turn = self.owner_turn()
        self.assertEqual(turn["images"], [{"id": ident, "kind": "sent", "w": 1200, "h": 800}])
        self.assertEqual(turn["body"], "看这张图\n\n附件：图片 1 张")
        self.assertNotIn(str(self.state), self.pushed_text())
        self.assertNotIn("incoming_media", self.pushed_text())
        sent = len(self.web.images)
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 2)
        self.assertEqual(len(self.web.images), sent)  # Never twice.
        again = web_images.WebImages(self.state)  # A restart: the ledger knows it is on the page.
        self.assertEqual(again.sent(f"`{path}`", TOPIC, T0, T0)[0]["id"], ident)
        self.assertEqual(again.pending, {})

    def test_an_image_the_page_has_already_is_not_sent_again(self):
        path = png(self.media / "om_1-1-img.png")
        self.web.have.add(web_images.file_id(path))
        self.said_with(path)
        self.sync.push_once(T0)
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.images, {})
        self.assertEqual(self.owner_turn()["images"][0]["id"], web_images.file_id(path))

    def test_only_received_files_of_this_state_directory_are_read(self):
        outside = png(self.state / "secret.png")
        elsewhere = png(self.state / "other" / "incoming_media" / "x.png")
        self.said_with(outside, elsewhere)
        self.sync.push_once(T0)
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.images, {})
        self.assertNotIn("images", self.owner_turn())
        self.assertNotIn(str(self.state), self.pushed_text())

    def test_the_image_of_a_message_older_than_the_page_keeps_is_gone_and_not_sent(self):
        path = png(self.media / "om_1-1-img.png")
        self.said_with(path, time="2020-01-01T00:00:00+08:00")
        self.sync.push_once(T0)
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.images, {})
        self.assertEqual(self.owner_turn()["images"], [{"id": "", "kind": "sent", "gone": True}])

    def test_a_page_without_images_holds_them_back_and_the_items_go_on(self):
        path = png(self.media / "om_1-1-img.png")
        self.said_with(path)
        self.web.refuse = 404
        self.assertGreater(self.sync.push_once(T0), 0)
        self.assertEqual(len([line for line in self.logs if "web sync images failed" in line]), 1)
        self.web.refuse = 0
        self.sync.push_once(T0 + 10)  # Held back IMAGES_WAIT seconds.
        self.assertEqual(self.web.images, {})
        self.sync.push_once(T0 + 400)
        self.assertEqual(len(self.web.images), 2)

    def test_an_image_the_page_refuses_is_not_sent_again(self):
        path = png(self.media / "om_1-1-img.png")
        self.said_with(path)
        self.web.refuse_image = 413
        self.sync.push_once(T0)
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        self.sync.push_once(T0 + 2)
        self.assertEqual(sum(1 for each in self.web.seen if each[1] == "/sync/image"), 1)
        self.assertNotIn("images", self.owner_turn())
        self.assertEqual([line for line in self.logs if "web sync images failed" in line], [])  # Not the page's failure: one image's.

    def test_without_pillow_a_small_image_goes_as_it_is_and_a_large_one_not_at_all(self):
        small = png(self.media / "om_1-1-small.png", size=(40, 30))
        large = self.media / "om_1-2-large.png"
        large.write_bytes(b"\x89PNG" + b"0" * (web_images.MAX_PART + 10))
        self.said_with(small, large)
        with mock.patch.object(web_images, "Image", None):
            self.sync.push_once(T0)
            self.sync.push_once(T0 + 1)
        ident = web_images.file_id(small)
        self.assertEqual(sorted(self.web.images), [(ident, "full"), (ident, "thumb")])
        self.assertEqual(self.web.images[(ident, "full")]["mime"], "image/png")
        self.assertEqual([each["id"] for each in self.owner_turn()["images"]], [ident])

    def test_a_page_that_says_it_takes_larger_images_gets_them(self):
        """Data contract v44: the page's ``limits.image_bytes`` (here 4 MiB) lifts MAX_PART; without Pillow an image of 800 KB, which
        an older page would not get, goes up as it is."""
        large = self.media / "om_1-1-large.png"
        large.parent.mkdir(parents=True, exist_ok=True)
        large.write_bytes(b"\x89PNG" + b"0" * 800_000)
        self.said_with(large)
        self.web.limits = {"body_bytes": 8 * 1024 * 1024, "image_bytes": 4 * 1024 * 1024, "store": "file"}
        self.sync.client.rate = 50_000_000  # A fast way to the page (T876: a slow one keeps images smaller).
        with mock.patch.object(web_images, "Image", None):
            self.sync.push_once(T0)
            self.sync.push_once(T0 + 1)
        ident = web_images.file_id(large)
        self.assertEqual(self.sync.client.image_bytes(), 3_000_000)
        self.assertEqual(sorted(self.web.images), [(ident, "full"), (ident, "thumb")])
        self.assertEqual(len(base64.b64decode(self.web.images[(ident, "full")]["data"])), 800_004)


class ShownImageTest(ImagesCase):
    @needs_pillow
    def test_the_images_a_task_showed_go_under_its_answer_with_their_notes(self):
        shot = png(self.state / "runs-out" / "shot.png", size=(800, 1000))
        checkpoint = self.state / "checkpoints" / "answer-1.json"
        write_json(checkpoint, {"show_images": [{"path": str(shot), "note": "线上深色"}, {"path": "/no/such.png"}]})
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, origin={"title": "截图"},
                    workspace_name="ws1")
        write_job(self.state, "k-shown", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="done", model="opus", message_id="om_1",
                  checkpoint_path=str(checkpoint))
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", "截个图", {"message_id": "om_1"})
        append_history(history, "assistant", "截好了", {"message_id": "om_a", "job_id": 7})
        self.sync.push_once(T0)
        self.sync.push_once(T0 + 1)
        agent = [item for item in self.web.items("turn") if item["data"]["role"] == "agent"][-1]["data"]
        self.assertEqual(agent["images"], [{"id": web_images.file_id(shot), "kind": "shown", "note": "线上深色", "w": 800, "h": 1000}])
        self.assertNotIn(str(shot), self.pushed_text())

    def test_a_task_on_a_device_shows_none_on_the_page(self):
        shot = png(self.state / "runs-out" / "shot.png")
        checkpoint = self.state / "checkpoints" / "answer-2.json"
        write_json(checkpoint, {"show_images": [str(shot)]})
        images = web_images.WebImages(self.state)
        self.assertEqual(images.shown({"checkpoint_path": str(checkpoint), "device": "gpu1"}, TOPIC, T0, T0), [])
        self.assertEqual(images.shown({"checkpoint_path": str(checkpoint)}, TOPIC, T0, T0), [])  # Not on the page yet: pending.
        self.assertEqual(list(images.pending), [str(shot)])


class BackfillTest(ImagesCase):
    def test_the_first_pass_looks_at_every_topic_once_and_the_next_does_not_delete_the_old_ones(self):
        self.topic()  # Its task was written now: 40 days on it is not one a full look takes any more.
        later = T0 + 40 * 86400
        self.sync.push_once(later)
        self.assertIn(TOPIC, [item["key"] for item in self.web.items("topic")])
        self.assertTrue(self.sync.images.backfilled)
        self.sync.push_once(later + 61)  # The next full pass: recent topics only, and none deleted.
        self.assertEqual([batch["deleted"] for batch in self.web.upserts if batch["deleted"]], [])
        pushed = len(self.web.items("topic"))
        fresh = type(self.sync)(self.runtime, self.env)  # A restart: the ledger says it was done, so no pass over every topic again.
        fresh.client.opener = self.sync.client.opener
        fresh.push_once(later + 200)
        self.assertEqual(len(self.web.items("topic")), pushed)


class TouchImageTest(ImagesCase):
    """2026-11-28 T932: the owner opened a topic on the page with a screenshot; 20 s on, his line there still said 附件：图片 1 张 and
    showed none. Only the pass's thread sent images, a step of it after reading 25 topics on the network disk (images.json had it
    40 s after his words). The touch, which pushes what the page just acted on, sends that topic's images itself and pushes it again."""

    def touched(self, now):
        self.sync.load_sent()
        self.sync.touch_soon({TOPIC})
        return self.sync.touch_once(now)

    def test_an_image_sent_on_the_page_goes_up_with_the_touch_and_its_turn_is_pushed_again_with_it(self):
        path = png(self.media / "web-0123456789abcdef-shot.png")
        self.said_with(path)
        self.assertGreater(self.touched(T0), 0)
        self.assertEqual(self.owner_turn()["body"], "看这张图\n附件：图片 1 张")  # His words at once, before the image is up.
        ident = web_images.file_id(path)
        self.assertEqual(sorted(self.web.images), [(ident, "full"), (ident, "thumb")])  # No pass step ran.
        self.assertTrue(self.sync.touch_event.is_set())  # Built and pushed again by the next touch.
        self.assertGreater(self.sync.touch_once(T0 + 1), 0)
        self.assertEqual([image["id"] for image in self.owner_turn()["images"]], [ident])
        self.assertEqual(self.sync.images.pending, {})
        self.assertFalse(self.sync.touch_event.is_set())  # Nothing more to send: no touch after it.

    def test_an_image_said_on_the_page_goes_up_before_the_touch_builds_so_its_first_push_carries_it(self):
        """2026-11-28 T933 acceptance: the touch built the topic, pushed the words bare, sent the image, built it again: 14 s for one
        screenshot on a long topic. The say's images wait to go up as soon as it is done, and the touch sends them first."""
        self.topic()
        data = png(self.state / "src" / "shot.png").read_bytes()
        ident = "e" * 32
        self.web.uploads[ident] = ({"name": "shot.png", "mime": "image/png", "size": len(data), "done": True}, [data])
        self.web.op("say", 1, "op-img", TOPIC, text="看这张", files=[{"id": ident, "name": "shot.png", "mime": "image/png", "size": len(data)}])
        self.sync.load_sent()
        self.sync.inbox_once()
        self.assertEqual([(r["op_id"], r["state"]) for r in self.web.acks], [("op-img", "done")])
        [saved] = sorted(self.media.glob(f"web-{ident[:16]}-*"))
        self.assertIn(str(saved), self.sync.images.pending)  # Waiting before any build asked for it.
        [(_, message, content)] = self.app.queued
        append_history(self.state / "topics" / TOPIC / "history.jsonl", "user", content, {"message_id": message})  # As the app does.
        self.assertTrue(self.sync.touch_event.is_set())
        self.assertGreater(self.sync.touch_once(T0), 0)
        owner = [item["data"] for item in self.web.items("turn") if item["data"]["role"] == "owner" and "看这张" in item["data"]["body"]]
        self.assertEqual([image["id"] for image in owner[0]["images"]], [web_images.file_id(saved)])  # The first push has it.
        self.assertEqual(self.sync.images.pending, {})
        self.assertFalse(self.sync.touch_event.is_set())  # Not built a second time for it.

    def test_an_operation_without_image_files_waits_for_no_image(self):
        self.topic()
        self.sync.expect_images({"payload": {"files": [{"id": "f" * 32, "name": "a.md", "mime": "text/markdown"}]}}, [TOPIC])
        self.sync.expect_images({"payload": {"text": "只有字"}}, [TOPIC])
        self.sync.expect_images({"payload": {"files": "坏的"}}, [TOPIC])
        self.assertEqual(self.sync.images.pending, {})

    def test_the_touch_sends_only_the_images_of_the_topics_the_page_acted_on(self):
        other = png(self.state / "topics" / "other-topic" / "incoming_media" / "om_9-1-img.png", color=(200, 30, 30))
        self.sync.images.sent(f"`{other}`", "other-topic", T0, T0)
        path = png(self.media / "web-0123456789abcdef-shot.png")
        self.said_with(path)
        self.touched(T0)
        self.assertEqual({ident for ident, _ in self.web.images}, {web_images.file_id(path)})
        self.assertIn(str(other), self.sync.images.pending)  # Left to the pass's step.

    def test_while_the_pass_sends_images_the_touch_leaves_them_to_it(self):
        path = png(self.media / "web-0123456789abcdef-shot.png")
        self.said_with(path)
        held, done = threading.Event(), threading.Event()

        def pass_uploading():
            with self.sync.images.lock:
                held.set()
                done.wait(5)
        thread = threading.Thread(target=pass_uploading, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(5))
        self.assertGreater(self.touched(T0), 0)
        self.assertEqual(self.web.images, {})
        self.assertIn(str(path), self.sync.images.pending)  # The pass sends it, then the quick look builds the topic again.
        done.set()
        thread.join(5)
        self.sync.push_images(self.sync.passer, T0 + 1)
        self.assertEqual(len(self.web.images), 2)
        self.assertIn(TOPIC, self.sync.collector.dirty)


class LateStateTest(ImagesCase):
    def test_images_go_up_when_the_state_directory_is_known_only_after_the_sync_was_made(self):
        # The unified Bridge makes WebSync before build_context fills the butler env's state directory in.
        self.env.state_dir = None
        late = type(self.sync)(self.runtime, self.env)
        late.client.opener = self.sync.client.opener
        self.env.state_dir = self.state
        path = png(self.media / "om_1-1-img.png")
        self.said_with(path)
        late.push_once(T0)
        late.push_once(T0 + 1)
        self.assertEqual(len(self.web.images), 2)
        self.assertEqual(self.owner_turn()["images"][0]["id"], web_images.file_id(path))


class HandOnTest(ImagesCase):
    """T584, 2026-11-20: the line the butler hands on keeps the owner's words and their "附件：图片 1 张"; the images are in the
    prompt of the task it started, and the page showed none. An edit of the message carried in the same line showed both texts."""

    def handed(self, content, prompt):
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, origin={"title": "截图"},
                    workspace_name="ws1")
        write_job(self.state, "k-hand", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="done", model="opus", message_id="main-abc",
                  prompt_text=prompt, user_text=content)
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", content, {"message_id": "main-abc"})
        append_history(history, "assistant", "看到了", {"message_id": "om_a", "job_id": 7})

    def test_a_handed_on_line_shows_the_images_its_task_was_given(self):
        path = png(self.media / "om_1-1-img.png")
        prompt = "\n".join(["[Bridge 说明] 管家转来", "附件：图片 1 张（见下方“收到的附件”）", "", ATTACHMENTS_HEADER,
                            attachment_line("image", 1, path), ATTACHMENTS_HINT])
        self.handed("帮我看看这张图\n附件：图片 1 张", prompt)
        self.sync.push_once(T0)
        self.sync.push_once(T0 + 1)
        turn = self.owner_turn()
        self.assertEqual([each["id"] for each in turn["images"]], [web_images.file_id(path)])
        self.assertEqual(turn["body"], "帮我看看这张图\n附件：图片 1 张")
        self.assertNotIn(str(self.state), self.pushed_text())

    def test_a_line_with_no_attachment_line_takes_no_image_from_its_prompt(self):
        path = png(self.media / "om_1-1-img.png")
        self.handed("没有图的话", "\n".join([ATTACHMENTS_HEADER, attachment_line("image", 1, path), ATTACHMENTS_HINT]))
        self.sync.push_once(T0)
        self.sync.push_once(T0 + 1)
        self.assertNotIn("images", self.owner_turn())

    def test_a_line_that_carries_the_message_and_its_edit_shows_the_edited_text_alone(self):
        self.handed("原来的话\n" + EDIT_HEAD + "\n\n改过的话\n附件：图片 1 张", "")
        self.sync.push_once(T0)
        turn = self.owner_turn()
        self.assertEqual((turn["display"], turn["edited"]), ("改过的话\n附件：图片 1 张", True))
        self.assertTrue(turn["body"].startswith("原来的话"))  # What the task was sent stays as it was.

    def test_an_edit_sent_on_its_own_line_goes_onto_the_line_it_edits(self):
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, origin={"title": "截图"},
                    workspace_name="ws1")
        write_job(self.state, "k-edit", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="done", model="opus", message_id="om_x1")
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", "原来的话", {"message_id": "om_x1"})
        append_history(history, "user", EDIT_HEAD + "\n\n改过的话", {"message_id": "edit-om_x1-1795150000"})
        append_history(history, "assistant", "好", {"message_id": "om_a", "job_id": 7})
        self.sync.push_once(T0)
        first, edit = [item["data"] for item in self.web.items("turn") if item["data"]["role"] == "owner"]
        self.assertEqual((first["body"], first["display"], first["edited"]), ("原来的话", "改过的话", True))
        self.assertTrue(edit["merged"])
