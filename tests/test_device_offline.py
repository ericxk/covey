"""A turn on a device when the device goes away (A-SPEC 5.5): what the card says, how the turn is settled, the pending
reaps and where they are registered and replayed, the devices thread and the device's gc.

The device is a temporary HOME running the real shim (tests/support/fake_device.py), the Agent a scripted local program;
faults are injected on the link (``faults.fail``, ``kill_attaches``). Every test ends by closing its processes and failing
on anything of the device left running."""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app import availability, detached
from covey.app import devices as device_turns
from covey.app import policy_guard, worker
from covey.app.app import BridgeApp
from covey.app.recovery_cards import recovery_card, recovery_text
from covey.core import device_link as dl
from covey.core import device_transport as dt
from covey.core import device_probe, device_reap, devices, turn_words
from covey.core import recovery as core_recovery
from covey.core.jobs import DEVICE_LOST_STATUS, find_job_record, upsert_job_record
from covey.core.longtask import TaskMonitor
from covey.core.state import read_object, write_json
from covey.core.status_line import Facts, status_summary, status_text
from covey.core.trace import ToolTrace
from support import exec_tempdir, make_job
from support.fake_device import FakeDevice
from test_device_process import DeviceCase, Lines, wait_for


def facts(**device):
    """A tick's Facts for a turn on a device whose link says ``device`` (a ``details()``)."""
    base = {"alias": "gpu1", "state": "up", "down_seconds": 0.0, "lost": False, "holding": False,
            "checkpoint_over": False}
    return Facts(elapsed=10.0, quiet=1.0, alive=True, finished=False, phase="", last_event="尚无阶段进展", checked="11-16 10:00:00",
                 question="", waiting=False, native_question_active=False, activity=None, traced=False, wall=time.time(),
                 stirred=False, retry=None, epoch=time.time(), long_mode=False, frame=0, device={**base, **device})


class WordsTest(unittest.TestCase):
    def test_the_status_line_says_what_the_link_is_doing(self):
        down = facts(state="down", down_seconds=130.4)
        self.assertEqual(status_summary(down), "gpu1 连接中断，重连中（设备上照常在跑）· 已断 2 分 10 秒")
        self.assertEqual(status_text(down), status_summary(down))  # A short task shows it too, not its ellipsis.
        waiting = facts(state="connecting", holding=True, down_seconds=3)
        self.assertEqual(status_summary(waiting), "gpu1 连接中断。任务停在等你回答，连上后你的回答会送过去。")
        up = facts()
        self.assertNotIn("连接中断", status_summary(up))
        self.assertEqual(status_text(up), "正在处理本轮消息 ·")  # A device that is fine changes nothing on the card.
        self.assertEqual(turn_words.span_words(5), "5 秒")
        self.assertEqual(turn_words.span_words(3725), "1 小时 2 分")

    def test_a_checkpoint_past_one_mib_is_marked_on_the_card(self):
        line = status_summary(facts(checkpoint_over=True))
        self.assertIn("gpu1 上的检查点超过 1 MiB，没有同步到本机", line)
        self.assertTrue(line.splitlines()[-1].startswith("gpu1 上的检查点"))
        self.assertNotIn("超过 1 MiB", status_summary(facts()))
        self.assertEqual(status_text(facts(checkpoint_over=True)), line)

    def test_the_words_of_the_table(self):
        self.assertEqual(turn_words.device_unreachable("gpu1", "ssh 连不上"), "gpu1 连不上（ssh 连不上）。这一轮没有开始，也不会改到本机跑。")
        self.assertEqual(turn_words.device_lost_notice("gpu1", 900), "gpu1 失联超过 15 分钟，这一轮按中断处理；设备上的进程连上后会停掉。")
        self.assertIn("失联超过 6 小时", turn_words.device_lost_notice("gpu1", 6 * 3600))
        self.assertEqual(turn_words.device_stop_pending("gpu1"), "已请求停止，gpu1 连上后生效")
        self.assertEqual(turn_words.device_restarted("gpu1"), "gpu1 重启过，这一轮按失败处理；会话还在设备上，检查并继续会接着做。")
        self.assertEqual(turn_words.device_hub_restarted("gpu1"),
                         "上一轮的任务进程中断过（机器人重启或任务进程退出），gpu1 上这一轮可能还在跑；点检查并继续会先停掉它再接着做。")
        self.assertEqual(turn_words.device_reconnected({"alias": "gpu1"}), "gpu1 已重新连上")

    def test_the_recovery_card_of_an_interrupted_device_turn_leads_with_where_the_agent_is(self):
        base = {"device": "gpu1", "status": "interrupted", "job_id": 5, "topic_id": "t", "status_message": "x"}
        lost = {**base, "interrupted_by": "device_lost", "device_state": {"limit_seconds": 900}}
        restarted = {**base, "interrupted_by": "restart"}
        local = {"status": "interrupted", "job_id": 5, "interrupted_by": "restart"}
        for record, words in ((lost, "gpu1 失联超过 15 分钟，这一轮按中断处理"), (restarted, "上一轮的任务进程中断过（机器人重启或任务进程退出），gpu1 上这一轮可能还在跑")):
            decided = core_recovery.classify(record)
            self.assertEqual((decided.card, decided.label), (core_recovery.INTERRUPTED, "检查并继续"))
            text = recovery_text(record, decided.card, decided.label, {}, continued=False, state_dir=None)
            self.assertTrue(text.startswith('停在：'), text)  # Where the Agent is leads the band instead (recovery_card).
            self.assertIn(words, json.dumps(recovery_card(record), ensure_ascii=False))
        decided = core_recovery.classify(local)
        self.assertNotIn("gpu1", recovery_text(local, decided.card, decided.label, {}, continued=False, state_dir=None))

    def test_an_old_record_without_the_new_fields_reads_as_before(self):
        self.assertEqual(turn_words.device_interrupted({"status": "interrupted", "job_id": 1}), "")
        self.assertEqual(turn_words.device_interrupted({"device": "gpu1", "status": "interrupted", "interrupted_by": "device_lost"}),
                         "gpu1 失联超过设定的耐心值，这一轮按中断处理；设备上的进程连上后会停掉。")
        self.assertEqual(device_turns.lost_notice({}), "")
        self.assertEqual(make_job().device_state, {})
        self.assertFalse(device_reap.needs_reap({}))


class PatienceConfigTest(unittest.TestCase):
    TEXT = {"schema": 1, "defaults": {"patience_minutes": 15, "waiting_patience_hours": 6},
            "devices": {"gpu1": {"ssh": {"host": "gpu1-host"}, "patience_minutes": 45, "waiting_patience_hours": 12},
                        "gpu2": {"ssh": {"host": "gpu2-host"}}}}

    def test_a_device_can_have_its_own_patience(self):
        config = devices.parse(json.dumps(self.TEXT))
        self.assertEqual((config.device("gpu1").patience_minutes, config.device("gpu1").waiting_patience_hours), (45, 12))
        self.assertEqual((config.device("gpu2").patience_minutes, config.device("gpu2").waiting_patience_hours), (None, None))
        for alias, short, long in (("gpu1", 45 * 60.0, 12 * 3600.0), ("gpu2", 15 * 60.0, 6 * 3600.0)):
            with mock.patch.object(devices, "current", return_value=config), mock.patch.object(dl, "transport_for"):
                link = dl.resolve(SimpleNamespace(device=alias, state_dir="/nowhere"))
            self.assertEqual((link.patience, link.waiting_patience), (short, long))
        bad = json.loads(json.dumps(self.TEXT))
        bad["devices"]["gpu1"]["patience_minutes"] = 0
        with self.assertRaisesRegex(devices.DeviceConfigError, r"gpu1\.patience_minutes"):
            devices.parse(json.dumps(bad))


class MonitorTest(unittest.TestCase):
    class Link:
        """What a backend's process is: it has its own ``details`` (a DeviceProcess's)."""
        state = {"alias": "gpu1", "state": "down", "down_seconds": 65.0, "lost": False, "holding": False,
                 "reconnects": 0, "checkpoint_over": False}

        def poll(self):
            return None

        def details(self):
            return dict(self.state)

    def test_the_monitor_puts_the_link_on_the_card_the_job_and_the_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            link, updates, records, now = self.Link(), [], [], [0.0]
            job = make_job(backend_process=link, tool_trace=ToolTrace())
            monitor = TaskMonitor(job, Path(directory) / "cp.json", updates.append, lambda **r: records.append(r),
                                  lambda *a: True, lambda _: None, lambda: None, clock=lambda: now[0])
            monitor.tick()
            self.assertEqual(updates[-1], "gpu1 连接中断，重连中（设备上照常在跑）· 已断 1 分 5 秒")
            self.assertEqual(job.device_state["state"], "down")
            self.assertEqual(records[-1]["device_state"]["alias"], "gpu1")
            link.state = {**link.state, "state": "up", "reconnects": 1}
            now[0] = 5.0
            monitor.tick()
            now[0] = 10.0
            monitor.tick()  # Once only.
            notes = [step for step in job.tool_trace.steps.values() if step.get("title") == "设备连接"]
            self.assertEqual([step["detail"] for step in notes], ["gpu1 已重新连上"])
            self.assertNotIn("连接中断", updates[-1])

    def test_a_process_here_has_no_device_state(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []
            job = make_job(backend_process=SimpleNamespace(poll=lambda: None))
            TaskMonitor(job, Path(directory) / "cp.json", lambda _: None, lambda **r: records.append(r), lambda *a: True,
                        lambda _: None, lambda: None, clock=lambda: 0.0).tick()
            self.assertEqual(job.device_state, {})
            self.assertNotIn("device_state", records[-1])


class LinkCase(DeviceCase):
    """A topic's turn on the fake device; the backoff is short and the patience a second and a half."""
    patience, waiting = 1.5, 6.0

    def topic_ctx(self, topic="topic-1"):
        return self.ctx(topic_id=topic)

    def lose(self, process):
        self.device.faults.fail(100000)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: process.state() in ("down", "lost")))

    def back(self):
        self.device.faults.fail_count = 0

    def job_of(self, process):
        return make_job(backend_process=process)

    def pending(self):
        return device_reap.entries(self.state, "gpu1")


class OfflineTest(LinkCase):
    def test_a_running_turn_goes_on_over_there_and_the_card_says_how_long_it_has_been_away(self):
        process = self.agent("--count", "400", "--delay", "0.05", ctx=self.topic_ctx())
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(3))
        self.lose(process)
        details = process.details()
        self.assertIn(details["state"], ("down", "lost"))
        job = self.job_of(process)
        monitor = TaskMonitor(job, self.root / "cp.json", lambda _: None, lambda **r: None, lambda *a: True, lambda _: None,
                              lambda: None, clock=lambda: 0.0)
        if process.state() == "down":
            self.assertIn("gpu1 连接中断，重连中（设备上照常在跑）· 已断", status_text(monitor.facts(0.0)[0]))
        self.back()
        self.assertTrue(wait_for(lambda: process.state() == "up" or process.lost))
        if not process.lost:
            self.assertEqual(process.details()["reconnects"], 1)
            count = len(lines.items)
            self.assertTrue(wait_for(lambda: len(lines.items) > count + 5))  # The journal is read on: nothing was missed.
            self.assertEqual(lines.ns(), list(range(len(lines.ns()))))

    def test_a_turn_that_waits_for_an_answer_is_given_the_longer_patience(self):
        waiting = threading.Event()
        waiting.set()
        process = self.agent("--count", "400", "--delay", "0.05", ctx=self.topic_ctx())
        process.set_hold(waiting.is_set)
        self.lose(process)
        time.sleep(self.patience + 1.0)  # Past the 1.5 s of an ordinary turn.
        self.assertFalse(process.lost)
        self.assertTrue(process.details()["holding"])
        text = status_summary(TaskMonitor(self.job_of(process), self.root / "cp.json", lambda _: None, lambda **r: None,
                                          lambda *a: True, lambda _: None, lambda: None).facts(0.0)[0])
        self.assertEqual(text, "gpu1 连接中断。任务停在等你回答，连上后你的回答会送过去。")
        self.back()

    def test_past_the_patience_the_turn_is_interrupted_and_the_reap_waits_for_the_device(self):
        process = self.agent("--count", "4000", "--delay", "0.05", ctx=self.topic_ctx())
        pgid = process.pgid
        self.lose(process)
        self.assertTrue(wait_for(lambda: process.lost, timeout=15))
        job = self.job_of(process)
        self.assertEqual(device_turns.end_of(job), "lost")
        with self.assertRaises(device_turns.DeviceLost):
            device_turns.verdict(job)
        self.assertEqual(worker._turn_from_exception(device_turns.DeviceLost("gpu1"), job, False), {"status": "interrupted"})
        records = []
        worker.leave_interrupted(SimpleNamespace(device="gpu1"), job, harness=SimpleNamespace(spec=SimpleNamespace(name="claude")),
                                 runtime=SimpleNamespace(log=lambda line: None),
                                 record_job_state=lambda ctx, job, status, message, **fields: records.append((status, message, fields)))
        self.assertEqual(records[0][:2], ("running", DEVICE_LOST_STATUS))  # Its runner ends; the Bridge marks it interrupted.
        self.assertEqual(records[0][2]["device_state"]["lost"], True)
        self.assertFalse(process.close())  # The device is not confirmed clean.
        entry = self.pending()[process.task]
        self.assertEqual((entry["reason"], entry["pgid"], entry["topic_id"], entry["alias"]),
                         ("device_lost", pgid, "topic-1", "gpu1"))
        self.assertTrue(entry["shim"])
        record = dl.read_record(self.root / "run")
        self.assertEqual((record["lost"], record["limit_seconds"]), (True, self.patience))
        self.assertTrue(device_reap.needs_reap(record))
        # The device is back: the devices thread replays the entry, and the Agent is gone for real.
        self.back()
        done, kept = device_reap.replay(self.state, "gpu1", self.device.transport)
        self.assertEqual((done, kept), ([process.task], []))
        self.assertEqual(self.pending(), {})
        self.assertEqual(self.device.transport.call("status", task=process.task)["state"], "exited")
        self.assertEqual(device_reap.previous_task(self.state, "gpu1", "topic-1"), {})

    def test_the_longer_patience_runs_out_too_and_is_the_one_the_card_names(self):
        self.link.waiting_patience = 2.5
        process = self.agent("--count", "4000", "--delay", "0.05", ctx=self.topic_ctx())
        process.set_hold(lambda: True)
        self.lose(process)
        self.assertTrue(wait_for(lambda: process.lost, timeout=15))
        self.assertEqual(process.details()["limit_seconds"], 2.5)
        record = {"device": "gpu1", "status": "interrupted", "interrupted_by": "device_lost",
                  "device_state": process.details()}
        self.assertIn("失联超过 2 秒", turn_words.device_interrupted(record))
        process.close()
        self.back()

    def test_a_stop_while_the_device_is_away_is_registered_and_said(self):
        process = self.agent("--count", "4000", "--delay", "0.05", ctx=self.topic_ctx())
        self.lose(process)
        process.signal_group(signal.SIGTERM)  # The user's stop: queued, the device cannot be told.
        job = self.job_of(process)
        self.assertEqual(device_turns.stop_note(job), "已请求停止，gpu1 连上后生效")
        self.assertEqual(worker._stop_note(job), "\n\n已请求停止，gpu1 连上后生效")
        process.close()
        self.assertEqual(self.pending()[process.task]["reason"], "stop")
        self.back()
        self.assertEqual(device_reap.replay(self.state, "gpu1", self.device.transport)[0], [process.task])
        self.assertEqual(self.device.transport.call("status", task=process.task)["state"], "exited")

    def test_a_stop_that_the_device_confirms_says_nothing_more(self):
        process = self.agent(ctx=self.topic_ctx())
        process.signal_group(signal.SIGTERM)
        process.wait(15)
        self.assertTrue(process.close())
        self.assertEqual(device_turns.stop_note(self.job_of(process)), "")
        self.assertEqual(self.pending(), {})

    def test_a_device_that_lost_the_task_fails_the_turn_with_its_own_words(self):
        process = self.agent("--count", "4000", "--delay", "0.05", ctx=self.topic_ctx())
        self.addCleanup(lambda: os.killpg(process.pgid, signal.SIGKILL))  # Its task directory is gone: reap cannot find it.
        self.device.faults.fail(3)
        self.device.kill_attaches()
        shutil_rmtree(self.device.transport.task_dir(process.task))  # What a restart leaves of the task.
        self.assertTrue(wait_for(lambda: process.state() == "gone", timeout=15))
        job = self.job_of(process)
        self.assertEqual(device_turns.end_of(job), "gone")
        with self.assertRaises(device_turns.DeviceGone):
            device_turns.verdict(job)
        turn = worker._turn_from_exception(device_turns.DeviceGone("gpu1"), job, False)
        self.assertEqual(turn["status"], "failed")
        self.assertIn("gpu1 重启过，这一轮按失败处理；会话还在设备上，检查并继续会接着做。", turn["final_text"])
        self.assertTrue(core_recovery.classify({"status": "failed"}).label)  # 检查并继续 is offered.

    def test_a_stop_by_the_user_is_not_a_lost_device(self):
        process = self.agent("--count", "4000", "--delay", "0.05", ctx=self.topic_ctx())
        self.lose(process)
        self.assertTrue(wait_for(lambda: process.lost, timeout=15))
        job = self.job_of(process)
        job.cancel_event.set()
        device_turns.verdict(job)  # Raises nothing: the stop is the turn's end.
        process.close()
        self.back()
        device_reap.replay(self.state, "gpu1", self.device.transport)


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class ReapBeforeTheNextTurnTest(LinkCase):
    def test_a_topics_last_task_is_reaped_before_its_next_turn_opens_the_session(self):
        first = self.agent(ctx=self.topic_ctx())
        old = first.task
        # The runner was killed or the machine restarted: nothing closed the first link, the Agent runs on.
        self.assertEqual(self.device.transport.call("status", task=old)["state"], "running")
        self.assertEqual(device_reap.previous_task(self.state, "gpu1", "topic-1")["task"], old)
        first._stop.set()
        self.device.transport.log.clear()
        second = self.agent(ctx=self.topic_ctx(), run=self.root / "run2")
        order = [(cmd, fields.get("task")) for cmd, fields in self.device.transport.log if cmd in ("reap", "spawn")]
        self.assertEqual(order, [("reap", old), ("spawn", second.task)])
        self.assertEqual(self.device.transport.call("status", task=old)["state"], "exited")
        self.assertEqual(device_reap.previous_task(self.state, "gpu1", "topic-1")["task"], second.task)

    def test_another_topics_task_is_left_alone(self):
        first = self.agent(ctx=self.topic_ctx("topic-1"))
        self.agent(ctx=self.topic_ctx("topic-2"), run=self.root / "run2")
        self.assertEqual(self.device.transport.call("status", task=first.task)["state"], "running")

    def test_a_device_that_will_not_confirm_stops_the_new_turn_from_starting(self):
        first = self.agent(ctx=self.topic_ctx())
        first._stop.set()
        with mock.patch.object(device_reap, "reap_one", return_value=False):
            with self.assertRaisesRegex(dl.DeviceSpawnError, "could not be stopped yet"):
                self.agent(ctx=self.topic_ctx(), run=self.root / "run2")
        self.assertEqual(self.device.transport.call("status", task=first.task)["state"], "running")

    def test_a_turn_that_ended_cleanly_leaves_nothing_to_reap(self):
        first = self.agent(ctx=self.topic_ctx())
        first.stdin.write(json.dumps({"exit": 0}) + "\n")
        first.stdin.flush()
        first.wait(15)
        self.assertTrue(first.close())
        self.assertEqual(device_reap.previous_task(self.state, "gpu1", "topic-1"), {})
        self.device.transport.log.clear()
        self.agent(ctx=self.topic_ctx(), run=self.root / "run2")
        self.assertNotIn("reap", [cmd for cmd, _ in self.device.transport.log])


class CheckpointTest(LinkCase):
    def test_a_checkpoint_past_one_mib_keeps_the_hubs_copy_and_is_marked(self):
        hub = self.root / "hub-checkpoint.json"
        hub.write_text(json.dumps({"phase": "start"}))
        remote = self.device.home / "cp.json"
        remote.write_text(json.dumps({"phase": "start"}))
        process = self.agent("--count", "4000", "--delay", "0.05", ctx=self.topic_ctx(),
                             checkpoint={"path": str(remote), "hub_path": str(hub), "poll": 0.2})
        remote.write_text(json.dumps({"phase": "big", "pad": "x" * (1 << 20 + 1)}))
        self.assertTrue(wait_for(lambda: process.details()["checkpoint_over"], timeout=15))
        self.assertEqual(json.loads(hub.read_text()), {"phase": "start"})  # Never truncated, never replaced.
        self.assertTrue(dl.read_record(self.root / "run").get("checkpoint_over") or wait_for(
            lambda: dl.read_record(self.root / "run").get("checkpoint_over"), timeout=5))
        text = status_summary(TaskMonitor(self.job_of(process), self.root / "cp.json", lambda _: None, lambda **r: None,
                                          lambda *a: True, lambda _: None, lambda: None).facts(0.0)[0])
        self.assertIn("gpu1 上的检查点超过 1 MiB，没有同步到本机", text)
        remote.write_text(json.dumps({"phase": "small"}))  # Small again: the hub's copy follows.
        self.assertTrue(wait_for(lambda: json.loads(hub.read_text()).get("phase") == "small", timeout=15))


class HooksTest(unittest.TestCase):
    """The three places a turn is given up on: the runner's end, a Bridge restart, an official stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.logs = []
        self.key = "dev-job-1"
        self.directory = self.state / "runs" / self.key
        self.directory.mkdir(parents=True)

    def device_json(self, **fields):
        record = {"schema": 1, "device": "gpu1", "hub": "hub1", "task": "dev-job-1-ab12", "shim": "~/.covey/shim/x/shim.py",
                  "task_dir": "/home/me/.covey/tasks/hub1/dev-job-1-ab12", "pgid": 4242, "state": "up", "lost": False,
                  **fields}
        write_json(self.directory / "device.json", record)

    def job_record(self, **fields):
        return upsert_job_record(self.state, self.key, job_id=1, topic_id="topic-1", chat_id="chat", message_id=self.key,
                                 status="running", backend="claude", kind="chat", device="gpu1", **fields)

    def entries(self):
        return device_reap.entries(self.state, "gpu1")

    def test_a_runner_that_ended_with_the_agent_still_there_registers_a_reap(self):
        self.device_json()
        self.job_record()
        gateway = SimpleNamespace(runtime=SimpleNamespace(log=self.logs.append, unregister_reply_job=lambda job: None))
        controls = mock.Mock()
        app = SimpleNamespace(runtime=gateway.runtime, controls=controls, key=lambda *a: "k", steering=mock.Mock())
        job = make_job(message_id=self.key, topic_id="topic-1")
        ctx = SimpleNamespace(state_dir=self.state, topic_id="topic-1")
        with mock.patch.object(detached, "context_for_job", return_value=ctx), \
                mock.patch.object(detached, "close_forms_sent_after_handoff"), \
                mock.patch.object(detached.knowledge_collect, "collect"), mock.patch.object(detached.butler_relay, "after_runner"), \
                mock.patch.object(detached, "_compact_when_idle"):
            detached.finish_detached(gateway, app, job, ctx, self.directory)
        entry = self.entries()["dev-job-1-ab12"]
        self.assertEqual((entry["reason"], entry["pgid"], entry["topic_id"], entry["job_key"]),
                         ("runner_lost", 4242, "topic-1", self.key))
        self.assertEqual(find_job_record(self.state, self.key)["status"], "interrupted")
        notice = controls.report_unfinished.call_args.args[2]
        self.assertTrue(notice.startswith("上一轮的任务进程中断过（机器人重启或任务进程退出），gpu1 上这一轮可能还在跑"), notice)
        self.assertTrue(any("device reap registered" in line for line in self.logs))

    def test_a_turn_the_device_lost_is_interrupted_by_device_lost_and_not_continued_by_itself(self):
        self.device_json(state="lost", lost=True, limit_seconds=900.0)
        self.job_record(status_message=DEVICE_LOST_STATUS, device_state={"alias": "gpu1", "limit_seconds": 900.0, "lost": True})
        gateway = SimpleNamespace(runtime=SimpleNamespace(log=self.logs.append, unregister_reply_job=lambda job: None))
        controls = mock.Mock()
        app = SimpleNamespace(runtime=gateway.runtime, controls=controls, key=lambda *a: "k", steering=mock.Mock())
        job = make_job(message_id=self.key, topic_id="topic-1")
        ctx = SimpleNamespace(state_dir=self.state, topic_id="topic-1")
        with mock.patch.object(detached, "context_for_job", return_value=ctx), \
                mock.patch.object(detached, "close_forms_sent_after_handoff"), \
                mock.patch.object(detached.knowledge_collect, "collect"), mock.patch.object(detached.butler_relay, "after_runner"), \
                mock.patch.object(detached, "_compact_when_idle"):
            detached.finish_detached(gateway, app, job, ctx, self.directory)
        record = find_job_record(self.state, self.key)
        self.assertEqual((record["status"], record["interrupted_by"]), ("interrupted", "device_lost"))
        self.assertEqual(controls.report_unfinished.call_args.kwargs["restart"], False)
        self.assertEqual(controls.report_unfinished.call_args.args[2],
                         "gpu1 失联超过 15 分钟，这一轮按中断处理；设备上的进程连上后会停掉。")
        self.assertEqual(self.entries()["dev-job-1-ab12"]["reason"], "device_lost")

    def test_a_task_that_ended_cleanly_registers_nothing(self):
        self.device_json(state="exited", exit={"code": 0}, cleanup_confirmed=True)
        self.assertIsNone(device_turns.register_run(self.state, self.directory, {"device": "gpu1"}, "runner_lost"))
        self.device_json(state="exited", exit={"code": 0, "left": [123]})
        self.assertIsNotNone(device_turns.register_run(self.state, self.directory, {"device": "gpu1"}, "runner_lost"))
        self.assertIsNone(device_turns.register_run(self.state, self.directory, {}, "runner_lost"))  # A turn here.

    def test_a_bridge_restart_registers_the_reap_of_every_device_turn_it_finds_unfinished(self):
        self.device_json()
        self.job_record()
        local = upsert_job_record(self.state, "local-job", job_id=2, topic_id="t2", chat_id="c", message_id="local-job",
                                  status="running", backend="claude", kind="chat")
        fake = SimpleNamespace(runtime=SimpleNamespace(adopted_keys=frozenset(), log=self.logs.append), spec=SimpleNamespace(name="claude"),
                               controls=mock.Mock(), key=lambda *a: "k")
        BridgeApp.notify_stale_jobs(fake, SimpleNamespace(state_dir=self.state))
        self.assertEqual(list(self.entries()), ["dev-job-1-ab12"])
        self.assertEqual(self.entries()["dev-job-1-ab12"]["reason"], "restart")
        notices = {call.args[1]["key"]: call.args[2] for call in fake.controls.report_unfinished.call_args_list}
        self.assertTrue(notices[self.key].startswith("上一轮的任务进程中断过（机器人重启或任务进程退出），gpu1"))
        self.assertIn("已中断", notices[local["key"]])
        # Registering twice leaves one entry with the first reason.
        BridgeApp.notify_stale_jobs(fake, SimpleNamespace(state_dir=self.state))
        self.assertEqual(len(self.entries()), 1)

    def stopped_by_policy(self, confirmed_by_device):
        self.device_json()
        self.job_record(policy_stop={"kind": "x"})
        intent = self.state / "policy-intent.json"
        write_json(intent, {"key": self.key, "job_id": 1, "phase": "stop_requested", "runner": {}, "observation": {"event": {}}})

        class Stop(Exception):
            pass
        with mock.patch.object(policy_guard, "stop_tree", return_value={"confirmed": True, "processes": []}), \
                mock.patch.object(device_turns, "reap_run", return_value=confirmed_by_device) as reap, \
                mock.patch.object(policy_guard.policy_retry, "stop_text", side_effect=Stop), \
                mock.patch.object(policy_guard, "policy_summary", return_value=""):
            with self.assertRaises(Stop):
                policy_guard.finish(None, SimpleNamespace(state_dir=self.state), intent)
        return read_object(intent), reap

    def test_an_official_stop_is_not_confirmed_until_the_device_says_the_agent_is_gone(self):
        intent, reap = self.stopped_by_policy(False)
        self.assertEqual((intent["phase"], intent["cleanup"]["confirmed"], intent["cleanup"]["reason"]),
                         ("cleanup_unconfirmed", False, "device_reap_pending"))
        self.assertEqual(reap.call_args.args[3], "policy_stop")
        # The device answers on a later pass: the stop is confirmed then.
        path = self.state / "policy-intent.json"
        with mock.patch.object(device_turns, "reap_run", return_value=True), \
                mock.patch.object(policy_guard.policy_retry, "stop_text", side_effect=KeyError), \
                mock.patch.object(policy_guard, "policy_summary", return_value=""):
            with self.assertRaises(KeyError):
                policy_guard.finish(None, SimpleNamespace(state_dir=self.state), path)
        again = read_object(path)
        self.assertEqual((again["phase"], again["cleanup"]["confirmed"]), ("stopped", True))

    def test_an_official_stop_of_a_turn_that_ran_here_is_unchanged(self):
        intent, _ = self.stopped_by_policy(True)
        self.assertEqual((intent["phase"], intent["cleanup"]["confirmed"]), ("stopped", True))

    def test_reap_run_registers_first_and_forgets_what_the_device_confirms(self):
        self.device_json()
        record = {"device": "gpu1", "topic_id": "topic-1", "key": self.key}
        device = FakeDevice()
        self.addCleanup(device.cleanup)
        done = device.spawn("dev-job-1-ab12", device.harness("--count", "100000", "--delay", "0.5"))
        write_json(self.directory / "device.json", {**read_object(self.directory / "device.json"), "pgid": done["pgid"],
                                                    "shim": device.transport.shim_path})
        config = devices.Config({"gpu1": object()}, Path(self.tmp.name))
        with mock.patch.object(devices, "current", return_value=config), \
                mock.patch.object(dl, "transport_for", return_value=device.transport):
            self.assertTrue(device_turns.reap_run(self.state, self.directory, record, "policy_stop", grace=1.0))
        self.assertEqual(self.entries(), {})
        self.assertEqual(device.transport.call("status", task="dev-job-1-ab12")["state"], "exited")
        # An unreachable device: not confirmed, and the entry stays for the thread.
        device.faults.fail(100)
        with mock.patch.object(devices, "current", return_value=config), \
                mock.patch.object(dl, "transport_for", return_value=device.transport):
            self.assertFalse(device_turns.reap_run(self.state, self.directory, record, "policy_stop", grace=1.0))
        self.assertEqual(list(self.entries()), ["dev-job-1-ab12"])


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(self.device.cleanup(), []))
        self.logs = []
        self.runtime = SimpleNamespace(shutdown=threading.Event(), log=self.logs.append)
        self.ctx = SimpleNamespace(state_dir=self.state)
        document = {"schema": 1, "devices": {"gpu1": {"ssh": {"host": "gpu1.example"}, "harnesses": ["claude", "codex"]}}}
        self.config = dataclasses.replace(devices.parse(json.dumps(document)), control_dir=Path(self.tmp.name),
                                          connect_seconds=2)
        self.patches = [mock.patch.object(devices, "current", return_value=self.config),
                        mock.patch.object(dl, "transport_for", return_value=self.device.transport)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_no_thread_while_the_switch_is_off(self):
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "", "LARK_BRIDGE_DEVICES": ""}):
            self.assertEqual(device_turns.Devices(self.runtime).threads(), ())
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "1"}):
            [(name, target)] = device_turns.Devices(self.runtime).threads()
        self.assertEqual(name, "devices")
        self.assertEqual(device_turns.PERIOD, 60)

    def test_a_round_probes_replays_the_pending_reaps_and_collects(self):
        task = "dev-old-1"
        done = self.device.spawn(task, self.device.harness("--count", "100000", "--delay", "0.5"))
        device_reap.register(self.state, "gpu1", task, reason="runner_lost", shim=self.device.transport.shim_path,
                             pgid=done["pgid"], topic_id="t")
        thread = device_turns.Devices(self.runtime)
        thread.round(self.ctx)
        health = read_object(devices.device_dir(self.state, "gpu1") / "health.json")
        self.assertTrue(health["ok"])
        self.assertIsNotNone(devices.read_hello(self.state, "gpu1"))
        self.assertEqual(device_reap.entries(self.state, "gpu1"), {})
        self.assertEqual(self.device.transport.call("status", task=task)["state"], "exited")
        self.assertIn("gc", [cmd for cmd, _ in self.device.transport.log])
        self.device.transport.log.clear()
        thread.round(self.ctx)  # gc runs once an hour, not every minute.
        self.assertNotIn("gc", [cmd for cmd, _ in self.device.transport.log])

    def with_tools(self, *names):
        """The named tools installed on the device (a script that says its version and that it is logged in)."""
        bin_dir = exec_tempdir()
        self.addCleanup(bin_dir.cleanup)
        for name in names:
            tool = Path(bin_dir.name) / name
            tool.write_text("#!/bin/sh\necho '%s 9.9.9 (fake)'\n" % name)
            tool.chmod(0o755)
            if not os.access(tool, os.X_OK):
                self.skipTest("no directory here where a script may be executed (run through scripts/test_map.py)")
        # Only these: the developer machine's own claude and codex must not count as the device's.
        self.device.transport.env["PATH"] = "%s:/usr/bin:/bin" % bin_dir.name

    def turn_context(self):
        from support import make_context
        return make_context(Path(self.tmp.name), device="gpu1", remote_workdir="/srv/p", backend_cmd=["claude"])

    def test_a_round_does_not_wipe_the_tools_the_self_check_found(self):
        """A-10: the thread's hello asked for no tools, its answer replaced the self-check's, and every turn on the
        device was then refused with 'executable missing'."""
        self.with_tools("claude", "codex")
        result = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertTrue(result.ok, result.lines())
        ctx = self.turn_context()
        self.assertEqual(availability.check_or_raise("claude", ctx)["status"], "configured")
        device_turns.Devices(self.runtime).round(self.ctx)
        self.assertTrue(read_object(devices.device_dir(self.state, "gpu1") / "health.json")["ok"])
        self.assertEqual(sorted(devices.read_hello(self.state, "gpu1")["tools"]), ["claude", "codex"])
        self.assertEqual(availability.check_or_raise("claude", ctx)["status"], "configured")
        self.assertEqual(availability.check_or_raise("codex", ctx)["status"], "configured")
        probe_hello = [fields for cmd, fields in self.device.transport.log if cmd == "hello"][-1]
        self.assertEqual((probe_hello["want"], probe_hello["proto"]), (["claude", "codex"], dt.PROTO))

    def test_a_tool_the_device_really_lacks_is_still_refused_after_a_round(self):
        self.with_tools("codex")  # No claude on this device.
        result = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertFalse(result.ok)
        ctx = self.turn_context()
        self.assertEqual(availability.local_check("claude", ctx)["reason"], "executable_missing")
        device_turns.Devices(self.runtime).round(self.ctx)
        self.assertEqual(availability.local_check("claude", ctx)["reason"], "executable_missing")
        self.assertEqual(availability.local_check("codex", ctx)["status"], "configured")
        with self.assertRaisesRegex(ValueError, "不可用"):
            availability.check_or_raise("claude", ctx)

    def test_a_hello_without_tools_keeps_the_tools_already_known(self):
        found = {"claude": {"found": True, "login": "ok"}}
        devices.write_hello(self.state, "gpu1", {"shim": "a", "tools": found}, now=1.0)
        devices.refresh_hello(self.state, "gpu1", {"shim": "b", "tools": {}})
        kept = devices.read_hello(self.state, "gpu1")
        self.assertEqual((kept["shim"], kept["tools"]), ("b", found))
        devices.refresh_hello(self.state, "gpu1", {"shim": "c", "tools": {"claude": {"found": False}}})
        self.assertEqual(devices.read_hello(self.state, "gpu1")["tools"], {"claude": {"found": False}})
        devices.refresh_hello(self.state, "gpu2", {"shim": "d"})  # Nothing known before: written as it is.
        self.assertEqual(devices.read_hello(self.state, "gpu2"), {"shim": "d"})

    def test_an_unreachable_device_keeps_its_entries_and_is_skipped(self):
        device_reap.register(self.state, "gpu1", "dev-old-2", reason="device_lost", shim="")
        self.device.faults.fail(100)
        device_turns.Devices(self.runtime).round(self.ctx)
        health = read_object(devices.device_dir(self.state, "gpu1") / "health.json")
        self.assertEqual((health["ok"], health["error"]), (False, "Unreachable"))
        self.assertEqual(list(device_reap.entries(self.state, "gpu1")), ["dev-old-2"])
        self.assertNotIn("reap", [cmd for cmd, _ in self.device.transport.log])


class GcTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(self.device.cleanup(), []))

    def test_gc_clears_old_checkpoint_mirrors_but_not_a_running_tasks(self):
        folder = self.device.home / ".covey" / "checkpoints" / "hub1"
        folder.mkdir(parents=True)
        month = time.time() - 30 * 86400
        names = {"old.json": month, "fresh.json": time.time(), "live.json": month}
        for name, stamp in names.items():
            (folder / name).write_text("{}")
            os.utime(folder / name, (stamp, stamp))
        (folder / "dir.json").mkdir()
        self.device.spawn("alive", self.device.harness("--count", "100000", "--delay", "0.5"),
                          checkpoint={"path": str(folder / "live.json"), "poll": 5.0})
        other = self.device.home / ".covey" / "checkpoints" / "hub-empty"
        other.mkdir()
        reply = self.device.transport.call("gc", keep_days=7)
        self.assertEqual(reply["removed_checkpoints"], ["hub1/old.json"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["dir.json", "fresh.json", "live.json"])
        self.assertFalse(other.exists(), "a hub directory left empty goes")
        self.assertEqual(self.device.transport.call("gc", keep_days=7)["removed_checkpoints"], [])

    def test_a_device_without_checkpoints_has_nothing_to_clear(self):
        reply = self.device.transport.call("gc", keep_days=7)
        self.assertEqual((reply["removed_checkpoints"], reply["removed_incoming"]), ([], []))

    def test_gc_clears_old_attachment_directories_but_not_a_running_turns(self):
        incoming = self.device.home / ".covey" / "incoming" / "hub1"
        month = time.time() - 30 * 86400
        for name, old in (("job-old", True), ("job-fresh", False), ("job-live", True), ("job-touched", True)):
            folder = incoming / name
            folder.mkdir(parents=True)
            (folder / "photo.png").write_bytes(b"x")
            stamp = month if old else time.time()
            for path in (folder / "photo.png", folder):
                os.utime(path, (stamp, stamp))
        fresh_file = incoming / "job-touched" / "new.png"  # An old directory that got a new file is not old.
        fresh_file.write_bytes(b"y")
        (incoming / "stray.txt").write_text("a file, not a turn's directory")
        os.utime(incoming / "stray.txt", (month, month))
        # A turn still running: its task key is the job key and a suffix (core/device_link.py task_key).
        self.device.spawn("job-live-ab12cd34", self.device.harness("--count", "100000", "--delay", "0.5"))
        reply = self.device.transport.call("gc", keep_days=7)
        self.assertEqual(reply["removed_incoming"], ["hub1/job-old"])
        self.assertEqual(sorted(p.name for p in incoming.iterdir()), ["job-fresh", "job-live", "job-touched", "stray.txt"])
        self.assertEqual(reply["removed_checkpoints"], [])

    def test_the_compat_check_knows_every_directory_the_hub_writes(self):
        import importlib.util
        from support import REPO
        spec = importlib.util.spec_from_file_location("check_shim_compat", REPO / "scripts" / "check_shim_compat.py")
        compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compat)
        shim = "def cmd_gc(req):\n    return ['tasks', 'shim']\n"
        hub = {"a.py": "x = '~/.covey/tasks/...' + '~/.covey/shim/x' + '~/.covey/checkpoints/h' + '~/.covey/incoming/h'"}
        self.assertEqual(compat.check_gc(shim, hub),
                         ["gc: the hub side writes ~/.covey/%s on the device, but the shim's gc never looks at it" % name
                          for name in ("checkpoints", "incoming")])
        self.assertEqual(compat.check_gc(shim.replace("'shim']", "'shim', 'checkpoints', 'incoming']"), hub), [])
        real = (REPO / "covey" / "core" / "device_shim.py").read_text()
        sources = {name: (REPO / "covey" / "core" / name).read_text() for name in compat.HUB_SIDE}
        self.assertEqual(compat.check_gc(real, sources), [])


if __name__ == "__main__":
    unittest.main()
