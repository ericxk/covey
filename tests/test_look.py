"""docs/ui.md's shared look: core/look.py's table and tone, the orb's image, cards/ui.py's parts and who stopped a job."""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from covey.cards import board, ui
from covey.cards.elements import walk
from covey.core import jobs, look, orb_image
from covey.feishu import orb_images

REPO = Path(__file__).resolve().parents[1]


class TableTest(unittest.TestCase):
    def test_the_board_states_read_as_the_table(self):
        """The 看板's five states (cards/board.py LOOKS) are the table's words and colours, so it moves here unchanged."""
        for key, (word, color, tint) in board.LOOKS.items():
            self.assertEqual((look.STATES[key].word, look.STATES[key].color, look.STATES[key].tint), (word, color, tint), key)

    def test_retired_words_are_not_the_words(self):
        words = {found.word for found in look.STATES.values()}
        for key, old in look.RETIRED.items():
            self.assertIn(key, look.STATES)
            self.assertFalse(words & set(old), key)

    def test_orb_order_is_states_with_an_orb_image(self):
        self.assertEqual(set(look.ORB_ORDER), set(orb_image.COLORS))
        for key in look.ORB_ORDER:
            self.assertEqual("#%02X%02X%02X" % orb_image.COLORS[key], look.STATES[key].web)

    def test_export_is_json_of_the_table(self):
        out = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "export_look.py")], capture_output=True,
                             text=True, check=True, timeout=60)
        data = json.loads(out.stdout)
        self.assertEqual(data, json.loads(json.dumps(look.export())))
        self.assertEqual(data["states"]["result"]["heading"], "有结果没看")
        self.assertEqual(data["states"]["stopped"]["word"], "停住了")


class ToneTest(unittest.TestCase):
    def test_statuses(self):
        cases = [("waiting_user", {}, "waiting"), ("waiting_workspace", {}, "waiting"), ("queued", {}, "queued"),
                 ("running", {}, "running"), ("running", {"stuck": True}, "stuck"), ("cancel_requested", {}, "running"),
                 ("done", {}, "result"), ("done", {"seen": True}, "done"), ("continued", {}, "result"),
                 ("failed", {}, "stopped"), ("interrupted", {}, "stopped"), ("cancelled", {}, "stopped"),
                 ("cancelled", {"by_owner": False}, "stopped"), ("cancelled", {"by_owner": True}, "cancelled"),
                 ("strange", {}, "idle"), ("", {}, "idle")]
        for status, kw, want in cases:
            self.assertEqual(look.tone(status, **kw), want, (status, kw))

    def test_who_stopped(self):
        """The owner's own stops are 已停止; the lead's, the guard's and 代答's are 停住了, as an unknown one is."""
        for by, want in (("card", "cancelled"), ("word", "cancelled"), ("web", "cancelled"), ("project", "cancelled"),
                         (jobs.RECALLED, "cancelled"),
                         ("lead", "stopped"), ("guard", "stopped"), ("proxy", "stopped"), ("", "stopped")):
            record = {"status": "cancelled", "cancel_requested_by": by}
            self.assertEqual(look.record_tone(record), want, by)
        self.assertLessEqual(look.OWNER_STOPS, jobs.STOP_SOURCES)
        self.assertEqual(look.record_tone({"status": "cancelled", "never_started": True}), "cancelled")
        self.assertEqual(look.record_tone({"status": "cancelled", "merged_into_job_id": 3}), "cancelled")
        self.assertEqual(look.record_tone({"status": "cancelled", "never_started": True, "cancel_requested_by": "lead"}),
                         "stopped")

    def test_orb(self):
        self.assertEqual(look.orb({"running": 3, "result": 1, "stopped": 1}), "stopped")
        self.assertEqual(look.orb(["running", "waiting"]), "waiting")
        self.assertEqual(look.orb({"done": 4, "queued": 1}), None)
        self.assertEqual(look.orb({}), None)


class StopSourceTest(unittest.TestCase):
    def test_the_click_value_keeps_a_known_source_only(self):
        value = {"kind": jobs.JOB_STOP_ACTION_KIND, "job_id": "7", "message_id": "om_1"}
        self.assertNotIn("by", jobs.parse_job_stop_action_value(value))
        self.assertEqual(jobs.parse_job_stop_action_value({**value, "by": "web"})["by"], "web")
        self.assertNotIn("by", jobs.parse_job_stop_action_value({**value, "by": "somebody"}))

    def test_each_stop_names_its_source(self):
        """Each way a job is stopped other than its card's button says who: core/look.py tells the owner's apart."""
        sources = {"app/butler_tracks.py": '"by": "word"', "app/web_ops.py": '"by": "web"', "app/proxy.py": '"by": "proxy"',
                   "app/project_tools.py": 'by="lead"', "app/project_loop.py": 'by="guard"',
                   "app/project_cards.py": 'by="project"', "app/task_controls.py": 'action.get("by") in STOP_SOURCES',
                   "app/source_watch.py": "by=RECALLED"}
        for path, needle in sources.items():
            self.assertIn(needle, (REPO / "covey" / path).read_text(encoding="utf-8"), path)


def pixels(data):
    """(width, height, RGBA rows) of an 8-bit RGBA PNG with unfiltered rows, as core/orb_image.py writes it."""
    width, height = struct.unpack(">II", data[16:24])
    start = data.index(b"IDAT") + 4
    size = struct.unpack(">I", data[start - 8:start - 4])[0]
    raw = zlib.decompress(data[start:start + size])
    stride = 1 + width * 4
    return width, height, [raw[y * stride + 1:(y + 1) * stride] for y in range(height)]


class OrbImageTest(unittest.TestCase):
    def test_a_ball_on_a_clear_ground(self):
        data = orb_image.png("waiting")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height, rows = pixels(data)
        self.assertEqual((width, height), (orb_image.SIZE, orb_image.SIZE))
        self.assertEqual(rows[0][3], 0)  # A corner is clear.
        middle = rows[height // 2][width // 2 * 4:width // 2 * 4 + 4]
        self.assertEqual(tuple(middle), (255, 255, 255, 255))  # The core is white.

    def test_same_bytes_every_time(self):
        self.assertEqual(orb_image.png("result"), orb_image.png("result"))
        self.assertNotEqual(orb_image.digest("result"), orb_image.digest("stopped"))


class OrbKeyTest(unittest.TestCase):
    def test_uploaded_once_per_profile_and_drawing(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload = mock.Mock(side_effect=["img_a", "img_b"])
            self.assertEqual(orb_images.orb_key(tmp, "bot", "waiting", upload=upload), "img_a")
            self.assertEqual(orb_images.orb_key(tmp, "bot", "waiting", upload=upload), "img_a")
            self.assertEqual(orb_images.orb_key(tmp, "other", "waiting", upload=upload), "img_b")
            self.assertEqual(upload.call_count, 2)
            sent = upload.call_args_list[0].args[0]
            self.assertEqual(sent.read_bytes(), orb_image.png("waiting"))

    def test_a_failed_upload_is_no_orb_and_is_tried_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload = mock.Mock(side_effect=[None, "img_a"])
            self.assertEqual(orb_images.orb_key(tmp, "bot", "stopped", upload=upload), "")
            self.assertEqual(orb_images.orb_key(tmp, "bot", "stopped", upload=upload), "img_a")
            self.assertEqual(orb_images.orb_key(tmp, "bot", "done", upload=upload), "")  # No orb for 做完.
            self.assertEqual(upload.call_count, 2)


class PartsTest(unittest.TestCase):
    def test_band_rings_and_orb(self):
        band = ui.conclusion_band("要你处理", ui.number(2, "件事"), "T531 等你选风格", below=ui.seg_bar({"waiting": 1}),
                                  orb_key="img_x")
        self.assertEqual(band["background_style"], look.BRAND["halo"][0])
        ring = band["elements"][0]
        self.assertEqual(ring["background_style"], look.BRAND["halo"][1])
        inner = ring["elements"][0]
        self.assertEqual(inner["background_style"], look.BRAND["band"])
        images = [element for _, _, element in walk([band]) if element.get("tag") == "img"]
        self.assertEqual([image["img_key"] for image in images], ["img_x"])
        self.assertIs(images[0]["preview"], False)
        # Feishu takes a px size only with crop_center or crop_top (a card with fit_horizontal was refused, 230099).
        self.assertIn(images[0]["scale_type"], {"crop_center", "crop_top"})

    def test_no_orb_without_its_key(self):
        band = ui.conclusion_band("要你处理", "都在掌握中")
        self.assertFalse([element for _, _, element in walk([band]) if element.get("tag") == "img"])

    def test_seg_bar_is_as_long_as_its_counts(self):
        bar, legend = ui.seg_bar({"waiting": 1, "stopped": 1, "result": 1, "running": 3})
        self.assertEqual(bar["content"].count(ui.SEG_CHAR), ui.SEG_WIDTH)
        self.assertEqual(bar["content"].count(ui.SEG_CHAR * 12), 1)  # 3 of 6: half of it.
        self.assertIn("1 停住了", legend["content"])
        self.assertEqual(ui.seg_bar({"waiting": 0}), [])

    def test_row_escapes_what_it_is_given(self):
        row = ui.topic_row("stopped", "T526 a*b", "测试没过 <x>", ["23:10 停住"], url="https://x/y")
        text = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("a*b", text)
        self.assertNotIn("<x>", text)
        self.assertIn("停住了", text)
        self.assertEqual(row["behaviors"][0]["default_url"], "https://x/y")


if __name__ == "__main__":
    unittest.main()
