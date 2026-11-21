"""记忆合并 (core/track_ledger.py, app/track_digest.py, docs/butler.md 同时做几件事): a track's delivered answer is saved
to the ledger, the butler's next turn is given what it has not read, and the tracks' session files are
maintained away. The butler's compaction does not change what is given."""
from __future__ import annotations

import dataclasses
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.butler import CHAT, BUTLER, ButlerCase, switch_on
from support.records import state_dir
from covey.app import butler as session
from covey.app import track_digest
from covey.app.turn_delivery import TurnDelivery
from covey.core import butler_tracks
from covey.core import track_ledger as ledger
from covey.core.jobs import job_key, upsert_job_record
from covey.core.maintenance import maintain
from covey.core.state import read_object, write_json
from covey.core.topics import topic_file

SLOT1 = butler_tracks.track_topic_id(CHAT, 1)
SLOT2 = butler_tracks.track_topic_id(CHAT, 2)
NOW = 1_800_000_000.0
SESSION = "33333333-3333-4333-8333-333333333333"


def settled(statuses):
    return lambda key: statuses.get(key) in session.GOOD_ENDS


def entry(key, ended=NOW - 60, **fields):
    record = ledger.make(key, CHAT, 1, "om_" + key, f"请求 {key}", f"答案 {key}", "done", ended, "", ())
    return {**record, **fields}


class Switched(unittest.TestCase):
    tracks = "1"

    def setUp(self):
        switch_on(self, "1")
        patch = mock.patch.dict(os.environ, {butler_tracks.ENV: self.tracks})
        patch.start()
        self.addCleanup(patch.stop)
        self.state = state_dir(self)


class LedgerTest(Switched):
    def test_a_record_keeps_the_text_cut_to_what_a_record_keeps(self):
        record = ledger.make("k", CHAT, 2, "om_1", "问 " * 300, "答" * 900, "done", NOW, SESSION, ["T12"])
        self.assertEqual(set(record), {"key", "chat_id", "track", "owner_message", "request", "answer", "status",
                                       "ended_at", "session_id", "opened", "digested_by"})
        self.assertEqual((len(record["request"]), len(record["answer"])), (ledger.REQUEST_CHARS, ledger.ANSWER_CHARS))
        self.assertTrue(record["answer"].endswith("…"))
        self.assertEqual((record["digested_by"], record["opened"]), ("", ["T12"]))

    def test_a_cut_just_after_a_blank_leaves_no_blank_before_the_ellipsis(self):
        words = "a" * (ledger.REQUEST_CHARS - 2) + " " + "b" * 50
        record = ledger.make("k", CHAT, 2, "om_1", words, words, "done", NOW, SESSION, [])
        self.assertTrue(record["request"].endswith("a…"))
        self.assertNotIn(" …", ledger.render([{**record, "answer": "c" * (ledger.SHOW_CHARS - 2) + " " + "d" * 50}]))

    def test_the_rows_are_this_chats_within_twelve_hours_newest_eight_oldest_first(self):
        for number in range(10):
            ledger.write(self.state, entry(f"k{number}", ended=NOW - 100 + number))
        ledger.write(self.state, entry("old", ended=NOW - ledger.WINDOW - 5))
        ledger.write(self.state, entry("other", chat_id="oc_other"))
        ledger.write(self.state, entry("me"))
        rows, more = ledger.pending(self.state, CHAT, "me", NOW, settled({}))
        self.assertEqual([r["key"] for r in rows], [f"k{n}" for n in range(2, 10)])
        self.assertEqual(more, 2)

    def test_given_ones_are_left_out_until_the_turn_that_took_them_ended_badly(self):
        ledger.write(self.state, entry("a"))
        ledger.write(self.state, entry("b"))
        ledger.write(self.state, entry("c"))
        ledger.claim(self.state, [entry("a"), entry("b")], "t1", settled({}))
        ledger.claim(self.state, [entry("c")], "t2", settled({}))
        for statuses, expected in (({"t1": "done", "t2": "running"}, ["c"]),
                                   ({"t1": "failed", "t2": "done"}, ["a", "b"]),
                                   ({"t1": "waiting_user", "t2": "continued"}, [])):
            rows, _ = ledger.pending(self.state, CHAT, "t3", NOW, settled(statuses))
            self.assertEqual([r["key"] for r in rows], expected, statuses)

    def test_a_claim_leaves_alone_what_another_turn_took_and_ended_well(self):
        ledger.write(self.state, entry("a"))
        ledger.claim(self.state, [entry("a")], "t1", settled({}))
        self.assertEqual(ledger.claim(self.state, [entry("a")], "t2", settled({"t1": "done"})), 0)
        self.assertEqual(read_object(ledger.path(self.state, "a"))["digested_by"], "t1")
        self.assertEqual(ledger.claim(self.state, [entry("a")], "t2", settled({"t1": "failed"})), 1)
        self.assertEqual(read_object(ledger.path(self.state, "a"))["digested_by"], "t2")

    def test_the_block_is_one_line_a_row_request_answer_and_topics(self):
        rows = [entry("a", opened=["T12", "T13"], request="查一下上周的部署记录", answer="上周共部署 3 次。" + "细" * 400),
                entry("b", status="failed", answer=""), entry("c", status="cancelled")]
        text = ledger.render(rows, more=2)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith(ledger.HEADER))
        self.assertIn("请求「查一下上周的部署记录」→ 上周共部署 3 次。", lines[1])
        self.assertTrue(lines[1].endswith("… → T12、T13"))
        self.assertLessEqual(len(lines[1].split("→ ")[1]), ledger.SHOW_CHARS + 20)
        self.assertIn("（失败） 请求「请求 b」→ 没有送达的回答", lines[2])
        self.assertIn("（已停止）", lines[3])
        self.assertEqual(lines[4], "（更早的还有 2 条没列）")
        self.assertEqual(ledger.render([]), "")

    def test_a_concurrent_pair_of_claims_leaves_one_name_and_a_whole_file(self):
        ledger.write(self.state, entry("a"))
        threads = [threading.Thread(target=ledger.claim, args=(self.state, [entry("a")], name, settled({"x": "done"})))
                   for name in ("x", "y", "z") * 4]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIn(read_object(ledger.path(self.state, "a"))["digested_by"], {"x", "y", "z"})


class Delivered(Switched):
    """app/track_digest.py record: TurnDelivery.deliver's hook."""

    def job(self, topic=SLOT1, mid="om_t1", job_id=7, **fields):
        return make_job(job_id=job_id, chat_id=CHAT, message_id=mid, topic_id=topic, user_text="查一下部署", **fields)

    def delivery(self, job, status="done", reply="答案正文", delivered=True):
        turn = SimpleNamespace(status=status, status_message="", final_text=reply,
                               verified_delivery=SimpleNamespace(text=reply, fingerprint="f") if reply else None)
        return SimpleNamespace(job=job, turn=turn, ctx=SimpleNamespace(state_dir=self.state), runtime=SimpleNamespace(log=mock.Mock()))

    def saved(self, job):
        return read_object(ledger.path(self.state, job_key(job.message_id, job.job_id)))

    def test_a_track_turn_that_was_delivered_is_saved_with_its_answer_session_and_topics(self):
        topic = "cd" * 16
        write_json(topic_file(self.state, topic), {"topic_id": topic, "chat_id": CHAT, "topic_number": 21})
        key = job_key("om_t1", 7)
        write_json(self.state / "main-thread" / "opened" / "r1.json",
                   {"job_key": key, "chat_id": CHAT, "topic_id": topic, "plan": {"start": True},
                    "answer": {"ok": True, "data": {}}, "at": 1.0})
        write_json(self.state / "main-thread" / "opened" / "r2.json",
                   {"job_key": key, "chat_id": CHAT, "topic_id": "ef" * 16, "plan": {}, "answer": {"ok": False}, "at": 1.0})
        job = self.job(session_id=SESSION)
        track_digest.record(self.delivery(job), True, "done", now=NOW)
        record = self.saved(job)
        self.assertEqual({k: record[k] for k in ("key", "chat_id", "track", "owner_message", "request", "answer",
                                                 "status", "ended_at", "session_id", "opened", "digested_by")},
                         {"key": key, "chat_id": CHAT, "track": 1, "owner_message": "om_t1", "request": "查一下部署",
                          "answer": "答案正文", "status": "done", "ended_at": NOW, "session_id": SESSION,
                          "opened": ["T21"], "digested_by": ""})

    def test_an_answer_that_was_not_delivered_is_not_kept(self):
        job = self.job()
        track_digest.record(self.delivery(job), False, "done", now=NOW)
        self.assertEqual(self.saved(job)["answer"], "")

    def test_failed_and_stopped_turns_are_saved_a_question_waiting_and_the_butler_are_not(self):
        for status, expected in (("failed", True), ("cancelled", True), ("waiting_user", False), ("interrupted", False)):
            job = self.job(mid=f"om_{status}")
            track_digest.record(self.delivery(job, status=status), True, status, now=NOW)
            self.assertEqual(ledger.path(self.state, job_key(job.message_id, job.job_id)).exists(), expected, status)
        job = self.job(topic=BUTLER, mid="om_root")
        track_digest.record(self.delivery(job), True, "done", now=NOW)
        self.assertFalse(ledger.path(self.state, job_key("om_root", 7)).exists())

    def test_with_the_switch_off_nothing_is_written(self):
        job = self.job()
        with mock.patch.dict(os.environ, {butler_tracks.ENV: ""}):
            track_digest.record(self.delivery(job), True, "done", now=NOW)
        self.assertFalse((self.state / "main-thread" / "tracks").exists())

    def test_a_failure_to_write_is_a_log_line_not_an_error(self):
        job = self.job()
        delivery = self.delivery(job)
        with mock.patch.object(ledger, "write", side_effect=OSError("disk")):
            track_digest.record(delivery, True, "done", now=NOW)
        delivery.runtime.log.assert_called_once()
        self.assertIn("OSError", delivery.runtime.log.call_args.args[0])

    def test_turn_delivery_calls_the_hook_after_the_relay(self):
        job = self.job(progress=SimpleNamespace())
        calls = []
        turn = self.delivery(job).turn
        turn.verified_outcome = SimpleNamespace(status="completed", notice=lambda: "", error_code="")
        turn.outcome, turn.allow_resume = None, True
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: text
        cards.exceeds_stream_card_limit = lambda text: False
        cards.body_cut.return_value = False
        delivery = TurnDelivery(job, SimpleNamespace(state_dir=self.state, history_path=self.state / "h.jsonl"), turn,
                                prefix="p", cards=cards, runtime=mock.Mock(), artifacts=mock.Mock(),
                                questions=mock.Mock(), steering=mock.Mock(), record_job_state=mock.Mock(),
                                send_text=mock.Mock(return_value=True),
                                relay=lambda *args: calls.append(("relay", args[1:])),
                                ledger=lambda *args: calls.append(("ledger", args[1:])))
        with mock.patch.object(TurnDelivery, "texts", return_value=("答案正文", "答案正文", False)), \
                mock.patch.object(TurnDelivery, "send", return_value=(True, True)), \
                mock.patch("covey.app.turn_delivery.record_attempt"), mock.patch("covey.app.turn_delivery.close_streaming_reply_async"), \
                mock.patch("covey.app.turn_delivery.append_history"):
            delivery.deliver(None)
        self.assertEqual(calls, [("relay", (True, "done")), ("ledger", (True, "done"))])


class Handed(ButlerCase):
    """butler_turn: the block after the snapshot, for the butler's turn and a track's."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {butler_tracks.ENV: "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.logs = []

    def turn(self, topic=BUTLER, mid="om_m2", job_id=11, request=None, session_id=None):
        job = SimpleNamespace(topic_id=topic, chat_id=CHAT, message_id=mid, job_id=job_id, kind="chat",
                              prompt_text=None, user_text="新问题", cancel_event=threading.Event())
        compaction = SimpleNamespace(request=mock.Mock(return_value=request), supported=lambda ctx: True)
        ctx = dataclasses.replace(self.ctx, state_dir=self.state)
        harness = None if topic == BUTLER else SimpleNamespace(backend=None, spec=SimpleNamespace(session_filename="s"))
        with mock.patch.object(session.contract, "traits_of",
                               return_value=session.contract.BUTLER), \
                mock.patch.object(session.tracks_module, "fork_options", return_value=None), \
                mock.patch.object(session.tracks_module, "context_pack", return_value=[]):
            return session.butler_turn(ctx, job, "RAW", session_id, compaction, self.logs.append, harness)[1], compaction

    def given(self, key, status):
        upsert_job_record(self.state, key, status=status, topic_id=BUTLER, chat_id=CHAT, job_id=1)

    def test_the_butler_gets_what_the_tracks_answered_after_the_snapshot_and_once(self):
        ledger.write(self.state, ledger.make("k1", CHAT, 1, "om_a", "查失败原因", "有三类：超时、权限、配置。", "done",
                                             time.time() - 120, SESSION, ["T5"]))
        first, _ = self.turn(mid="om_m2", job_id=11)
        self.assertIn(ledger.HEADER, first)
        self.assertIn("请求「查失败原因」→ 有三类：超时、权限、配置。 → T5", first)
        self.assertLess(first.index("[快照"), first.index(ledger.HEADER))
        self.assertLess(first.index(ledger.HEADER), first.index("RAW"))
        self.assertEqual(read_object(ledger.path(self.state, "k1"))["digested_by"], job_key("om_m2", 11))
        self.given(job_key("om_m2", 11), "done")
        second, _ = self.turn(mid="om_m3", job_id=12)
        self.assertNotIn(ledger.HEADER, second)

    def test_a_turn_that_did_not_end_well_gives_them_again(self):
        ledger.write(self.state, ledger.make("k1", CHAT, 1, "om_a", "查失败原因", "有三类", "done", time.time() - 5, "", ()))
        self.turn(mid="om_m2", job_id=11)
        self.given(job_key("om_m2", 11), "failed")
        again, _ = self.turn(mid="om_m3", job_id=12)
        self.assertIn("请求「查失败原因」", again)
        self.assertEqual(read_object(ledger.path(self.state, "k1"))["digested_by"], job_key("om_m3", 12))

    def test_a_new_track_gets_the_block_with_the_note_and_marks_nothing(self):
        ledger.write(self.state, ledger.make("k1", CHAT, 2, "om_a", "查失败原因", "有三类", "done", time.time() - 5, "", ()))
        upsert_job_record(self.state, "om_run", status="running", topic_id=BUTLER, chat_id=CHAT, job_id=3,
                          user_text="正在做的事")
        prompt, _ = self.turn(topic=SLOT1, mid="om_t1", job_id=9)
        self.assertIn("此刻另外几件事在做", prompt)
        self.assertIn(ledger.HEADER, prompt)
        self.assertLess(prompt.index("此刻另外几件事在做"), prompt.index(ledger.HEADER))
        self.assertEqual(read_object(ledger.path(self.state, "k1"))["digested_by"], "")

    def test_the_tracks_own_record_is_not_given_back_to_it(self):
        ledger.write(self.state, ledger.make(job_key("om_t1", 9), CHAT, 1, "om_t1", "自己", "自己的答案", "done", time.time() - 5, "", ()))
        prompt, _ = self.turn(topic=SLOT1, mid="om_t1", job_id=9)
        self.assertNotIn(ledger.HEADER, prompt)

    def test_with_the_switch_off_the_message_is_what_it_was(self):
        ledger.write(self.state, ledger.make("k1", CHAT, 1, "om_a", "查失败原因", "有三类", "done", time.time() - 5, "", ()))
        with mock.patch.dict(os.environ, {butler_tracks.ENV: ""}):
            prompt, _ = self.turn()
        self.assertNotIn(ledger.HEADER, prompt)
        self.assertEqual(read_object(ledger.path(self.state, "k1"))["digested_by"], "")

    def test_an_unreadable_ledger_leaves_the_message_without_the_block(self):
        with mock.patch.object(ledger, "pending", side_effect=OSError("gone")):
            prompt, _ = self.turn()
        self.assertNotIn(ledger.HEADER, prompt)
        self.assertTrue(any("ledger block unavailable" in line for line in self.logs))

    def test_after_the_butlers_compaction_the_block_is_still_given(self):
        """Compaction is the butler's alone (a track never asks): a turn that compacts first reads everything
        again, the ledger's rows included; rows it was not given before stay open, and the summary the compaction
        writes is of its own session, which holds only what it was given."""
        ledger.write(self.state, ledger.make("k1", CHAT, 1, "om_a", "查失败原因", "有三类", "done", time.time() - 60, "", ()))
        first, _ = self.turn(mid="om_m2", job_id=11)
        self.given(job_key("om_m2", 11), "done")
        ledger.write(self.state, ledger.make("k2", CHAT, 2, "om_b", "查部署", "三次", "done", time.time() - 30, "", ()))
        request = SimpleNamespace(idle=False)
        with mock.patch.object(session, "left_for_idle", return_value=False):
            after, compaction = self.turn(mid="om_m3", job_id=12, request=request, session_id=SESSION)
        compaction.request.assert_called_once()
        self.assertIn("：全部]", after)  # The snapshot is whole again after the compaction.
        self.assertIn("请求「查部署」", after)
        self.assertNotIn("请求「查失败原因」", after)  # k1 was given by a turn that ended well, before it.
        self.assertIn("请求「查失败原因」", first)
        # The idle compaction (between turns) changes nothing either.
        self.given(job_key("om_m3", 12), "done")
        ledger.write(self.state, ledger.make("k3", CHAT, 1, "om_c", "查权限", "两处", "done", time.time() - 10, "", ()))
        with mock.patch.object(session.idle_compaction, "compacted_at", return_value=time.time() + 5):
            last, _ = self.turn(mid="om_m4", job_id=13, session_id=SESSION)
        self.assertIn("请求「查权限」", last)
        self.assertNotIn("请求「查部署」", last)

    def test_a_track_never_marks_even_after_the_butler_compacted(self):
        ledger.write(self.state, ledger.make("k1", CHAT, 2, "om_a", "查失败原因", "有三类", "done", time.time() - 5, "", ()))
        with mock.patch.object(session.idle_compaction, "compacted_at", return_value=time.time() + 5):
            prompt, compaction = self.turn(topic=SLOT1, mid="om_t1", job_id=9)
        compaction.request.assert_not_called()
        self.assertIn(ledger.HEADER, prompt)
        self.assertEqual(read_object(ledger.path(self.state, "k1"))["digested_by"], "")


class Maintained(Switched):
    """core/maintenance_rules.py: the ledger seven days, a track's session file three. The tree is written now and the
    run is given a later ``now`` (a file's change time cannot be set back)."""
    DAY = 86400

    def setUp(self):
        super().setUp()
        self.t0 = time.time()
        home = self.state / "claude-home"
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.project = home / "projects" / "-work-dir"
        self.project.mkdir(parents=True)

    def track(self, name, session_id, ended_days, used_days=0):
        """A ledger record of a turn that ended ``ended_days`` after t0 and its session file, used up to ``used_days``."""
        transcript = self.project / f"{session_id}.jsonl"
        transcript.write_text("{}\n")
        if used_days:
            stamp = self.t0 + used_days * self.DAY
            os.utime(transcript, (stamp, stamp))
        ledger.write(self.state, ledger.make(name, CHAT, 1, "om_" + name, "q", "a", "done",
                                             self.t0 + ended_days * self.DAY, session_id, ()))
        return transcript

    def test_a_tracks_session_file_goes_after_three_days_and_its_record_after_seven(self):
        old = self.track("old", "00000001-0000-4000-8000-000000000000", 0)
        new = self.track("new", "00000002-0000-4000-8000-000000000000", 2)
        stranger = self.project / "99999999-0000-4000-8000-000000000000.jsonl"  # No record names it: left alone.
        stranger.write_text("{}\n")
        plan = maintain(self.state, now=self.t0 + 4 * self.DAY)
        self.assertEqual(plan["remove_track_sessions"], [str(old)])
        self.assertEqual(plan["remove_temporary"], [])
        maintain(self.state, apply=True, now=self.t0 + 4 * self.DAY)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and stranger.exists())
        self.assertTrue(ledger.path(self.state, "old").exists())  # The record stays its seven days.

    def test_a_session_still_in_use_stays(self):
        self.track("old", "00000009-0000-4000-8000-000000000000", 0, used_days=3.5)
        self.assertEqual(maintain(self.state, now=self.t0 + 4 * self.DAY)["remove_track_sessions"], [])

    def test_the_ledger_goes_after_seven_days_and_a_session_name_is_no_path(self):
        ledger.write(self.state, ledger.make("old", CHAT, 1, "om_old", "q", "a", "done", self.t0, "../../x", ()))
        plan = maintain(self.state, now=self.t0 + 9 * self.DAY)
        self.assertIn(str(ledger.path(self.state, "old")), plan["remove_temporary"])  # With its lock folder.
        self.assertEqual(plan["remove_track_sessions"], [])
        maintain(self.state, apply=True, now=self.t0 + 9 * self.DAY)
        self.assertFalse(ledger.path(self.state, "old").exists())


if __name__ == "__main__":
    unittest.main()
