"""轮次来源 (core/turn_source.py, docs/architecture.md 多参与者协作): a job without a source runs and is read as before; a job
whose source this release does not run is never run as the owner's message, on either side of the handoff; and the
consumers that act on the owner's words skip a turn whose words are not his."""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.world import UnifiedCase
from covey.app import knowledge_collect, memory_collect, owner_send, worker
from covey.app.app import BridgeApp
from covey.app.runner_spawn import dump_job, load_job
from covey.core import turn_source
from covey.core.harness import HarnessCapabilities
from covey.core.jobs import BridgeReplyJob, find_job_record, record_bridge_job_state, upsert_job_record
from covey.core.turn_source import ASK, HANDOVER, LEGACY, MALFORMED, OWNER, REPLY

REPO = Path(__file__).resolve().parent.parent
FULL = HarnessCapabilities(read_only=True, collab=True)
NEW_KINDS = frozenset({"relay", "lead", "wake", "answer", "answer_for"})  # WAVE12 C1: read and run from W1-0, stamped later.
ALL_KINDS = frozenset({OWNER, ASK, REPLY, HANDOVER, *NEW_KINDS})


def asked(**extra):
    return turn_source.make(ASK, asserted_by="topic", origin={"topic": "t3"}, collab="c1", ask="c1-a-1", **extra)


class KindsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {turn_source.SWITCH: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_job_without_a_source_is_legacy_and_does_everything_as_before(self):
        for item in (BridgeReplyJob(job_id=1, user_text="", chat_id="c", message_id="om_1", timeout=0), {}, {"turn_source": {}},
                     {"turn_source": None}, None):
            with self.subTest(item=item):
                self.assertEqual(turn_source.kind(item), LEGACY)
                self.assertEqual(turn_source.behavior(item), turn_source.AS_BEFORE)
                self.assertEqual(turn_source.refusal(item), "")

    def test_this_release_runs_the_owner_a_handover_and_a_collaboration_only_with_the_switch_and_the_harness(self):
        owner = {"turn_source": turn_source.make(OWNER, asserted_by="owner", origin={"message_id": "om_1"})}
        self.assertEqual(turn_source.refusal(owner), "")
        # 话题转交管家 (app/butler_asks.py) runs: no switch, no capability.
        handover = turn_source.make(HANDOVER, asserted_by="owner", origin={"message_id": "om_9", "topic": "t3"})
        self.assertEqual(turn_source.refusal({"turn_source": handover}), "")
        # 协作 (app/collab_flow.py): off by default whatever the Harness, on with COVEY_COLLAB=1 where it declares NEEDS.
        for made in (asked(), turn_source.make(REPLY, asserted_by="bridge", collab="c1")):
            with self.subTest(kind=made["kind"]):
                self.assertEqual(turn_source.refusal({"turn_source": made}, capabilities=FULL), turn_source.SWITCH_OFF)
                with mock.patch.dict("os.environ", {turn_source.SWITCH: "1"}):
                    self.assertEqual(turn_source.refusal({"turn_source": made}, capabilities=FULL), "")
                    self.assertIn("没有声明", turn_source.refusal({"turn_source": made}, capabilities=HarnessCapabilities()))

    def test_a_kind_a_version_or_a_shape_it_does_not_know_never_runs_and_may_do_nothing(self):
        for value in ({"v": 1, "kind": "lead_wake"}, {"v": 2, "kind": OWNER}, {"v": "1", "kind": OWNER}, {"kind": 3},
                      ["owner"], "owner"):
            with self.subTest(value=value):
                item = {"turn_source": value}
                self.assertNotEqual(turn_source.refusal(item, capabilities=FULL, runs=ALL_KINDS), "")
                self.assertEqual(turn_source.behavior(item), turn_source.NOTHING)
        self.assertEqual(turn_source.kind({"turn_source": ["owner"]}), MALFORMED)
        self.assertEqual(turn_source.kind({"turn_source": {"kind": "lead_wake"}}), "lead_wake")

    def test_once_a_package_runs_them_the_switch_and_the_harness_still_decide(self):
        item = {"turn_source": asked()}
        self.assertEqual(turn_source.refusal(item, capabilities=FULL, runs=ALL_KINDS), turn_source.SWITCH_OFF)
        with mock.patch.dict("os.environ", {turn_source.SWITCH: "1"}):
            self.assertEqual(turn_source.refusal(item, capabilities=FULL, runs=ALL_KINDS), "")
            for caps, missing in ((HarnessCapabilities(read_only=True), "collab"), (HarnessCapabilities(collab=True), "read_only"),
                                  (None, "collab、read_only")):
                with self.subTest(missing=missing):
                    self.assertEqual(turn_source.refusal(item, capabilities=caps, runs=ALL_KINDS),
                                     turn_source.NOT_DECLARED.format(names=missing))
            reply = {"turn_source": turn_source.make(REPLY, asserted_by="bridge", collab="c1")}
            self.assertEqual(turn_source.refusal(reply, capabilities=HarnessCapabilities(collab=True), runs=ALL_KINDS), "")
        handover = {"turn_source": turn_source.make(HANDOVER, asserted_by="owner", origin={"message_id": "om_9"})}
        self.assertEqual(turn_source.refusal(handover, runs=ALL_KINDS), "")  # Not 协作: no switch, no capability.

    def test_the_behavior_table_keeps_a_collaborations_turns_off_the_owners_things(self):
        ask, reply = turn_source.BEHAVIOR[ASK], turn_source.BEHAVIOR[REPLY]
        for row in (ask, reply):
            self.assertFalse(row.owner_words)  # No memory, no 记一下, no lead's owner turn.
            self.assertFalse(row.answers_questions)
            self.assertFalse(row.steers)
        self.assertTrue(ask.read_only)
        self.assertFalse(ask.own_delivery)  # The receiver's latest delivery stays its own.
        self.assertFalse(ask.owner_send)
        self.assertTrue(reply.own_delivery)  # The integrator's turn is its delivery.
        handover = turn_source.BEHAVIOR[HANDOVER]
        self.assertTrue(handover.owner_words)  # The owner's words, by the message they came in with.
        self.assertFalse(handover.answers_questions)
        self.assertEqual(set(turn_source.BEHAVIOR), {LEGACY, *ALL_KINDS})
        self.assertEqual(turn_source.KINDS, ALL_KINDS)
        self.assertEqual(turn_source.RUNS, ALL_KINDS)

    def test_the_first_item_with_a_source_decides(self):
        job = BridgeReplyJob(job_id=1, user_text="", chat_id="c", message_id="om_1", timeout=0)
        record = {"turn_source": asked()}
        self.assertEqual(turn_source.kind(job, record), ASK)  # The job has none: its record's.
        job.turn_source = turn_source.make(OWNER, asserted_by="owner", origin={"message_id": "om_1"})
        self.assertEqual(turn_source.kind(job, record), OWNER)


class ProvenanceTest(unittest.TestCase):
    def test_only_the_owners_words_with_an_origin_the_bridge_took_in_are_the_owners(self):
        level = lambda *args: turn_source.provenance(*args)["level"]  # noqa: E731
        self.assertEqual(level("owner", "bridge", {"message_id": "om_1"}), turn_source.LEVEL_OWNER)
        self.assertEqual(level("owner", "bridge", {"question": "q_1"}), turn_source.LEVEL_OWNER)
        self.assertEqual(level("owner", "lead", None), turn_source.LEVEL_AGENT)  # "主人同意了" with nothing to show.
        self.assertEqual(level("owner", "lead", {"topic": "t3"}), turn_source.LEVEL_AGENT)
        self.assertEqual(level("lead", "lead", {"message_id": "om_1"}), turn_source.LEVEL_AGENT)
        made = turn_source.provenance("owner", "lead", {"message_id": "om_1", "extra": "x", "topic": "t3"})
        self.assertEqual(made, {"asserted_by": "owner", "recorded_by": "lead", "origin": {"message_id": "om_1", "topic": "t3"},
                                "level": "owner"})
        for parties in (("主人", "bridge"), ("owner", "model")):
            with self.subTest(parties), self.assertRaises(ValueError):
                turn_source.provenance(*parties)

    def test_the_bridge_writes_the_field_and_names_itself_its_recorder(self):
        made = asked()
        self.assertEqual((made["v"], made["kind"], made["recorded_by"], made["asserted_by"], made["level"], made["collab"]),
                         (1, ASK, "bridge", "topic", "agent", "c1"))
        with self.assertRaises(ValueError):
            turn_source.make("lead_wake", asserted_by="bridge")


# WAVE12 C1: the five kinds of the hands-on and continuations, cell by cell (owner_words, answers_questions, steers,
# read_only, own_delivery, owner_send; joinable since T957).
TABLE = {
    "relay": (False, False, True, False, True, True, True),
    "lead": (False, False, True, False, True, True, True),
    "wake": (False, False, False, False, True, False, False),
    "answer": (True, True, False, False, True, True, True),
    "answer_for": (False, True, False, False, True, False, False),
}
FIELDS = ("owner_words", "answers_questions", "steers", "read_only", "own_delivery", "owner_send", "joinable")


def hand_on(kind_):
    """The field of a hand-on or a continuation as the release that stamps them writes it (core/turn_source.py make)."""
    return {"v": 1, "kind": kind_, "asserted_by": "owner", "recorded_by": "bridge", "origin": {"message_id": "om_9"},
            "level": "owner", "via": "butler"}


def stamped(kind_, asserted_by, origin=None, **extra):
    return {"turn_source": turn_source.make(kind_, asserted_by=asserted_by, origin=origin, **extra)}


class NewKindsTest(unittest.TestCase):
    """W1-0, readers first: this release runs the five new kinds as their rows say, before any release stamps them."""

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {turn_source.SWITCH: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_new_kind_has_its_row_cell_by_cell(self):
        self.assertEqual(set(TABLE), NEW_KINDS)
        self.assertEqual(dataclasses_fields(turn_source.Behavior), FIELDS)  # The six fields as they were, and joinable.
        for kind_, cells in TABLE.items():
            row = turn_source.BEHAVIOR[kind_]
            for field, cell in zip(FIELDS, cells):
                with self.subTest(kind=kind_, field=field):
                    self.assertIs(getattr(row, field), cell)
            self.assertEqual(turn_source.behavior(stamped(kind_, "bridge")), row)
        self.assertEqual(turn_source.VERSION, 1)
        self.assertEqual((turn_source.RELAY, turn_source.LEAD, turn_source.WAKE, turn_source.ANSWER, turn_source.ANSWER_FOR),
                         ("relay", "lead", "wake", "answer", "answer_for"))

    def test_each_runs_with_no_switch_and_no_capability_and_an_unknown_kind_still_never_runs(self):
        for kind_ in sorted(NEW_KINDS):
            with self.subTest(kind=kind_):
                self.assertEqual(turn_source.refusal(stamped(kind_, "bridge")), "")
                self.assertEqual(turn_source.refusal(stamped(kind_, "bridge"), capabilities=HarnessCapabilities()), "")
        for value in ({"v": 1, "kind": "lead_wake"}, {"v": 2, "kind": "relay"}, {"v": 1, "kind": "Relay"}):
            with self.subTest(value=value):
                self.assertIn("这个版本不执行", turn_source.refusal({"turn_source": value}))
                self.assertEqual(turn_source.behavior({"turn_source": value}), turn_source.NOTHING)

    def test_via_is_an_optional_top_level_key(self):
        made = turn_source.make("relay", asserted_by="owner", origin={"message_id": "om_1"}, via="butler")
        self.assertEqual(made["via"], "butler")
        self.assertNotIn("via", turn_source.make("relay", asserted_by="butler"))
        with self.assertRaises(ValueError):
            turn_source.make("relay", asserted_by="butler", via="email")
        self.assertEqual(turn_source.VIAS, {"butler", "lead", "web", "card", "message"})
        self.assertEqual(turn_source.via({"turn_source": made}), "butler")
        for item in ({"turn_source": {**made, "via": "email"}}, {"turn_source": {**made, "via": 3}}, {"message_id": "om_1"},
                     {"turn_source": {**made, "v": 2}}, {"turn_source": {**made, "kind": "lead_wake"}}):
            with self.subTest(item=item):
                self.assertEqual(turn_source.via(item), "")

    def test_the_level_of_a_legacy_job_is_read_of_its_message_id(self):
        from covey.core import web_sync

        self.assertEqual(turn_source.DIRECT, ("om_", web_sync.PREFIX))
        for message_id, level in (("om_1", "owner"), ("web-1-ab", "owner"), ("answer-0123", "agent"), ("main-3", "agent"),
                                  ("lead-4", "agent"), ("collab-c1-1", "agent"), ("", "agent")):
            with self.subTest(message_id=message_id):
                self.assertEqual(turn_source.level({"message_id": message_id}), level)
                job = BridgeReplyJob(job_id=1, user_text="", chat_id="c", message_id=message_id, timeout=0)
                self.assertEqual(turn_source.level(job), level)

    def test_the_level_of_a_stamped_job_is_its_provenance_and_unread_fields_are_an_agents(self):
        cases = (
            (stamped("relay", "owner", {"message_id": "om_9"}), "owner"),  # The owner's words carried in the hand-on.
            (stamped("relay", "butler", {"message_id": "om_9"}), "agent"),
            (stamped("relay", "owner", {"topic": "t3"}), "agent"),  # "主人说了" with nothing the Bridge took in.
            (stamped("lead", "lead", {"topic": "t1"}), "agent"),
            (stamped("wake", "bridge"), "agent"),
            (stamped("answer", "owner", {"question": "0123456789abcdef"}), "owner"),
            (stamped("answer_for", "lead", {"question": "0123456789abcdef"}), "agent"),
            ({"message_id": "om_1", "turn_source": {"v": 2, "kind": "relay", "asserted_by": "owner",
                                                     "recorded_by": "bridge", "origin": {"message_id": "om_1"}}}, "agent"),
            ({"message_id": "om_1", "turn_source": {"v": 1, "kind": "lead_wake", "asserted_by": "owner",
                                                     "recorded_by": "bridge", "origin": {"message_id": "om_1"}}}, "agent"),
            ({"message_id": "om_1", "turn_source": ["owner"]}, "agent"),
            ({"message_id": "om_1", "turn_source": {"v": 1, "kind": "relay", "asserted_by": "主人",
                                                     "recorded_by": "bridge", "origin": {"message_id": "om_1"}}}, "agent"),
        )
        for item, level in cases:
            with self.subTest(item=item):
                self.assertEqual(turn_source.level(item), level)

    def test_origin_reads_the_kept_parts_of_a_known_field(self):
        item = stamped("relay", "owner", {"message_id": "om_9", "topic": "t3", "extra": "x"})
        self.assertEqual(turn_source.origin(item), {"message_id": "om_9", "topic": "t3"})
        self.assertEqual(turn_source.origin({"message_id": "om_1"}), {})
        self.assertEqual(turn_source.origin({"turn_source": {**item["turn_source"], "v": 2}}), {})
        self.assertEqual(turn_source.origin({"turn_source": {"v": 1, "kind": "relay", "origin": "om_9"}}), {})

    def test_downstream_finds_the_running_jobs_one_message_led_to(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            relay = turn_source.make("relay", asserted_by="owner", origin={"message_id": "om_9"}, via="butler")
            rows = (("relay-1", "running", relay), ("relay-2", "queued", relay), ("relay-3", "done", relay),
                    ("other", "running", turn_source.make("relay", asserted_by="owner", origin={"message_id": "om_8"})),
                    ("om_9", "running", None))  # The owner's own message, unstamped: not one it led to.
            for number, (key, status, source) in enumerate(rows, 1):
                upsert_job_record(state, key, job_id=number, topic_id="t1", chat_id="oc_1", status=status,
                                  updated_at=f"2026-11-27T10:00:0{number}Z", **({"turn_source": source} if source else {}))
            self.assertEqual([record["key"] for record in turn_source.downstream(state, "om_9")], ["relay-2", "relay-1"])
            self.assertEqual(turn_source.downstream(state, "om_7"), [])
            self.assertEqual(turn_source.downstream(state, ""), [])


def dataclasses_fields(cls):
    import dataclasses

    return tuple(field.name for field in dataclasses.fields(cls))


class RecordsAndHandoffTest(unittest.TestCase):
    """runs/ and the job record are as before for a job without a source; one with a source carries it to every reader."""

    def job(self, **fields):
        job = BridgeReplyJob(job_id=7, user_text="核对第 3 题", chat_id="oc_1", message_id="collab-c1-1", timeout=60,
                             topic_id="t5")
        for name, value in fields.items():
            setattr(job, name, value)
        return job

    def test_a_handoff_of_a_job_without_a_source_has_no_new_key(self):
        dumped = dump_job(self.job())
        self.assertNotIn("turn_source", dumped["extras"])
        self.assertNotIn("turn_source", dumped["fields"])
        self.assertEqual(load_job(dumped).turn_source, {})

    def test_a_source_goes_through_the_handoff_and_into_the_record(self):
        dumped = dump_job(self.job(turn_source=asked()))
        self.assertEqual(dumped["extras"]["turn_source"]["kind"], ASK)
        self.assertEqual(turn_source.kind(load_job(dumped)), ASK)
        with mock.patch("covey.core.jobs.upsert_job_record") as upsert:
            record_bridge_job_state(state_dir=Path("/absolute/state"), backend="claude", job=self.job(), status="queued")
            self.assertNotIn("turn_source", upsert.call_args.kwargs)
            record_bridge_job_state(state_dir=Path("/absolute/state"), backend="claude", job=self.job(turn_source=asked()),
                                    status="queued")
            self.assertEqual(upsert.call_args.kwargs["turn_source"]["kind"], ASK)

    def test_only_the_harnesses_that_passed_the_real_write_refusal_check_declare_a_collaboration(self):
        # C8 (tests/test_collab_c8.py on the installed CLI): Claude Code and Codex held to CONSULT refused every write.
        # Antigravity and dsh have no read-only limit at all; an extension's Harness declares nothing of it.
        self.assertFalse(HarnessCapabilities().collab)
        declared = sorted(path.relative_to(REPO).as_posix() for path in (REPO / "covey" / "backends").rglob("*.py")
                          if "collab=True" in path.read_text())
        self.assertEqual(declared, ["covey/backends/claude/bridge.py", "covey/backends/codex/bridge.py"])


class BridgeGateTest(UnifiedCase):
    """app/job_dispatch.py refuse_source: the Bridge's side of the handoff."""

    def queued(self, mid):
        self.receive(mid, "initial")
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, "chat", "claude")
        app, ctx = self.route(mid)
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        return app, self.app.runtime.jobs.queue.get_nowait()

    def test_a_job_of_a_collaboration_is_recorded_cancelled_and_never_runs_as_the_owners_message(self):
        # With COVEY_COLLAB off (a rollback's state too, docs/architecture.md 开关、兼容与回退), and a newer release's kind.
        for mid, source, words in (("asked", asked(), "协作没有打开"),
                                    ("newer", {**asked(), "v": 2}, "这个版本不执行")):
            with self.subTest(mid):
                self.refused(mid, source, words)

    def refused(self, mid, source, words):
        app, job = self.queued(mid)
        job.turn_source = source
        said = len(self.said())
        with mock.patch("covey.app.job_dispatch.check_or_raise") as check, \
             mock.patch.object(BridgeApp, "process_reply_job", autospec=True) as process, \
             mock.patch.object(app.controls, "show_recovery_card") as recovery:
            self.app.process_reply_job(job, self.ctx)
        process.assert_not_called()
        check.assert_not_called()
        recovery.assert_not_called()
        record = find_job_record(self.ctx.state_dir, mid)
        self.assertEqual((record["status"], record["never_started"]), ("cancelled", True))
        self.assertIn(words, record["status_message"])
        self.assertEqual(record["turn_source"]["kind"], ASK)
        self.assertNotIn(job.job_id, self.app.runtime.jobs.by_id)
        self.assertEqual(self.said()[said:], [])
        self.assertTrue(any("turn source refused" in line and "kind=collab_ask" in line for line in self.logs))

    def test_a_job_without_a_source_and_an_owners_stamped_one_run_as_before(self):
        for mid, source in (("plain", None), ("stamped", turn_source.make(OWNER, asserted_by="owner",
                                                                           origin={"message_id": "stamped"}))):
            with self.subTest(mid):
                _, job = self.queued(mid)
                if source:
                    job.turn_source = source
                with mock.patch("covey.app.job_dispatch.check_or_raise"), \
                     mock.patch.object(BridgeApp, "process_reply_job", autospec=True) as process:
                    self.app.process_reply_job(job, self.ctx)
                self.assertEqual(process.call_count, 1)

    def test_a_hand_on_of_the_butler_runs_as_its_row_says(self):
        # W1-0 runs it before any release stamps it (an older release cancels it: refusal NOT_RUN).
        _, job = self.queued("relayed")
        job.turn_source = hand_on("relay")  # As the next release writes it: an older release reads only the dict.
        with mock.patch("covey.app.job_dispatch.check_or_raise"), \
             mock.patch.object(BridgeApp, "process_reply_job", autospec=True) as process:
            self.app.process_reply_job(job, self.ctx)
        self.assertEqual(process.call_count, 1)
        self.assertFalse(any("turn source refused" in line for line in self.logs))

    def test_enqueue_starts_no_lead_owner_turn_and_no_supplement_for_words_that_are_not_the_owners(self):
        app, ctx = self.route("lead-like")
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        with mock.patch("covey.app.project_loop.owner_turn") as owner_turn, \
             mock.patch.object(app.submission, "submit_topic_job", return_value={}) as submit:
            made = app.submission.enqueue_chat_job(ctx, "请核对", "chat", "collab-c1-1", source=asked(), steer=True)
            self.assertEqual(turn_source.kind(made), ASK)
            owner_turn.assert_not_called()
            self.assertFalse(submit.call_args.kwargs["steer"])
            app.submission.enqueue_chat_job(ctx, "主人的话", "chat", "om_owner", steer=True)
            owner_turn.assert_called_once()
            self.assertTrue(submit.call_args.kwargs["steer"])

    def test_a_record_rebuilt_into_a_job_keeps_its_source(self):
        app, ctx = self.route("rebuilt")
        record = {"job_id": 3, "user_text": "核对", "chat_id": "chat", "message_id": "collab-c1-2", "turn_source": asked()}
        self.assertEqual(turn_source.kind(app.submission.job_from_record(ctx, record)), ASK)
        self.assertEqual(app.submission.job_from_record(ctx, {**record, "turn_source": None}).turn_source, {})


class RunnerGateTest(unittest.TestCase):
    """app/worker.py: the task runner's own release checks what it runs."""

    def run_job(self, source):
        job = SimpleNamespace(topic_id="t5", chat_id="oc_1", job_id=8, message_id="collab-c1-1", kind="chat",
                              cancelled_before_start=False, execution_lock=threading.Lock(), execution_started=False,
                              turn_source=source)
        record, logs = mock.Mock(), []
        with mock.patch.object(worker, "context_for_job", side_effect=lambda ctx, job: ctx), \
             mock.patch.object(worker, "_start_turn", return_value=(0, None)) as start, \
             mock.patch.object(worker, "_run_turn", return_value=(SimpleNamespace(status="done"), None)), \
             mock.patch.object(worker, "_deliver_turn"), \
             mock.patch.object(worker.butler_reactions, "begin"), \
             mock.patch.object(worker.butler_tracks, "release_pointer"):
            worker.process_bridge_reply_job(job, SimpleNamespace(state_dir=Path("/absolute/state"), profile=""),
                                            harness=SimpleNamespace(backend=None), runtime=SimpleNamespace(log=logs.append),
                                            messenger=None, cards=None, sessions=None, runs=None, artifacts=None,
                                            questions=None, steering=None, record_job_state=record)
        return job, record, start, logs

    def test_a_collaborations_job_is_cancelled_before_its_turn_starts(self):
        # Switch off (the runner's own process reads it), then on with a Harness that declares nothing of 协作.
        for on, words in (("", "协作没有打开"), ("1", "没有声明")):
            with self.subTest(switch=on), mock.patch.dict("os.environ", {turn_source.SWITCH: on}):
                job, record, start, logs = self.run_job(asked())
                start.assert_not_called()
                self.assertEqual(record.call_args.args[2], "cancelled")
                self.assertIn(words, record.call_args.args[3])
                self.assertTrue(job.cancelled_before_start)
                self.assertEqual(logs, ["turn source refused job=#8 kind=collab_ask"])

    def test_each_new_kind_starts_its_turn(self):
        for kind_ in sorted(NEW_KINDS):
            with self.subTest(kind=kind_):
                job, record, start, logs = self.run_job(hand_on(kind_))
                start.assert_called_once()
                self.assertTrue(job.execution_started)
                self.assertFalse(any("turn source refused" in line for line in logs))

    def test_a_job_without_a_source_starts_its_turn(self):
        job, record, start, _ = self.run_job({})
        start.assert_called_once()
        self.assertTrue(job.execution_started)


class ConsumersTest(unittest.TestCase):
    """The owner's things taken from a turn's words skip a turn whose words are not his."""

    RECORD = {"status": "done", "chat_id": "oc_dm", "topic_id": "t5", "user_text": "以后都要带基线，记一下", "message_id": "x"}

    def runtime(self):
        return SimpleNamespace(access=SimpleNamespace(owner_turn=lambda chat, sender: True), log=lambda *_: None)

    def test_memory_and_remember_take_nothing_from_a_collaborations_turn(self):
        job = SimpleNamespace(topic_id="t5", chat_id="oc_dm", sender_id="", user_text=self.RECORD["user_text"], message_id="x",
                              prompt_text=None, turn_source={})
        ctx = SimpleNamespace(state_dir=Path("/absolute/state"))
        for module in (memory_collect, knowledge_collect):
            with self.subTest(module.__name__):
                job.turn_source = {}  # The record's source decides.
                self.assertEqual(module.reason_to_ignore(self.runtime(), ctx, job, {**self.RECORD, "turn_source": asked()}),
                                 "not-owner-words")
                job.turn_source = {"v": 1, "kind": "reply_of_the_future"}  # One it does not know: nothing.
                self.assertEqual(module.reason_to_ignore(self.runtime(), ctx, job, self.RECORD), "not-owner-words")

    def test_a_collaborations_ask_offers_no_owner_send_drafts(self):
        proxy = mock.Mock()
        gateway = SimpleNamespace(proxy=proxy)
        owner_send.collect(gateway, SimpleNamespace(turn_source=asked()), SimpleNamespace())
        proxy.owner_send.collect.assert_not_called()
        owner_send.collect(gateway, SimpleNamespace(turn_source={}), SimpleNamespace())
        proxy.owner_send.collect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
