"""网页工作台的图片 (app/web_images.py, data contract v19): the images the owner sent and the ones a task showed him go up to the page once
each (a thumbnail and a large one, POST /sync/image) and the turn carries their ids; no local path is ever in what is pushed (the attachment
block becomes its "附件：…" line); only received files of this state directory are read; an image of a message older than KEEP_DAYS is
``gone`` and not sent; a page that does not take images holds them back and the items go on."""
from __future__ import annotations

import base64
import io
import json
from unittest import mock

from PIL import Image

from covey.app import web_images
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
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
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
    def test_an_image_the_owner_sent_goes_up_once_and_its_turn_carries_its_id_not_its_path(self):
        path = png(self.media / "om_1-1-img_v3.png")
        self.said_with(path, display=False)  # An answer's line has no display text: its words were pushed whole, path and all.
        self.sync.push_once(T0)
        self.assertNotIn("images", self.owner_turn())  # Not on the page yet: sent after the items of this step.
        ident = web_images.file_id(path)
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


class ShownImageTest(ImagesCase):
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
