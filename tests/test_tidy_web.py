"""整理专项 on the web page (app/tidy_web.py, T884): how the latest 整理 went is in the panel's view (``last_run``: its progress
while it reads, its reason in the card's words and the Harness it asked once it failed, never a CLI's own words), and the
panel is pushed again as the reading goes on (``stamps``). A 整理 started on the page says the panel follows it."""
from __future__ import annotations

from unittest import mock

from test_dispatch_cards import CHAT
from test_project_tidy import TidyCase
from covey.app import project_sorter as sorter
from covey.app.track_judge import Failed
from covey.app.web_ops import Ops
from covey.core import project_proposals as proposals
from covey.core.state import write_json


SECRET = "sk" + "-fake-" + "0123456789abcdefghij"  # Made at run time: no secret-shaped literal in the source.


def refused_login(*args, **kwargs):
    exc = Failed("authentication_error", "401 Unauthorized: token " + SECRET)
    exc.via, exc.model = "codex", "gpt-6-astra"
    raise exc


class LastRunTest(TidyCase):
    def test_a_failed_tidy_shows_on_the_page_with_its_reason_and_the_harness_never_the_clis_words(self):
        topics = self.filler()
        self.see(lambda: topics)
        # Every topic has its gist already: the judge itself is asked, and refused.
        with mock.patch.object(sorter, "ask", side_effect=refused_login), mock.patch.object(sorter, "needs_gist", return_value=False):
            record = self.tidy.runs.whole(self.ctx, CHAT, "20261127170000-aaaaaa", "web", "")
        self.assertEqual((record["view"]["state"], record["view"]["why_code"]), ("failed", "authentication_error"))
        last = self.tidy.web_view()["last_run"]
        self.assertEqual({key: last[key] for key in ("run", "state", "why_code", "via", "model", "retry", "origin")},
                         {"run": "20261127170000-aaaaaa", "state": "failed", "why_code": "authentication_error", "via": "codex",
                          "model": "gpt-6-astra", "retry": False, "origin": "web"})
        self.assertIn("Codex", last["why"])
        self.assertNotIn("detail", last)
        self.assertNotIn(SECRET, str(self.tidy.web_view()))
        self.assertEqual(self.tidy.web_view()["proposals"], [])  # No suggestion: the page shows the failure all the same.

    def test_no_run_no_last_run_and_a_run_ended_without_saying_how_is_failed(self):
        self.assertNotIn("last_run", self.tidy.web_view())
        folder = proposals.folder(self.state) / "runs"
        import time
        now = time.time()
        write_json(folder / "20261127170100-bbbbbb.json", {"run": "20261127170100-bbbbbb", "origin": "scheduled",
                                                            "started_at": now - 60, "finished_at": now,
                                                            "view": {"state": "running", "progress": {"done": 2, "total": 5}}})
        last = self.tidy.web_view()["last_run"]
        self.assertEqual((last["state"], last["progress"]), ("failed", {"done": 2, "total": 5, "current": ""}))
        write_json(folder / "20261127170200-cccccc.json", {"run": "20261127170200-cccccc", "origin": "web", "started_at": now,
                                                            "view": {"state": "running",
                                                                     "progress": {"done": 1, "total": 4, "current": "T101 杂事"}}})
        last = self.tidy.web_view()["last_run"]
        self.assertEqual((last["run"], last["state"], last["progress"]["current"]), ("20261127170200-cccccc", "running", "T101 杂事"))

    def test_the_stamps_follow_the_reading_and_a_new_run_record(self):
        before = self.tidy.web_stamps()
        self.tidy.shared.progress = {"done": 3, "total": 9}
        moved = self.tidy.web_stamps()
        self.assertNotEqual(moved, before)
        write_json(proposals.folder(self.state) / "runs" / "20261127170300-dddddd.json", {"run": "20261127170300-dddddd"})
        self.assertNotEqual(self.tidy.web_stamps(), moved)

    def test_a_tidy_asked_on_the_page_says_the_panel_follows_it(self):
        self.filler()
        with mock.patch.object(self.tidy.runs, "guarded"):  # Not run here: the receipt is what the page shows.
            self.assertEqual(Ops(self.env).run({"kind": "tidy", "op_id": "o1", "payload": {"scope": ""}}),
                             ("done", proposals.WEB_STARTED_WORDS))
        self.tidy.shared.busy.release()
