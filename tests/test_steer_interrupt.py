"""A supplement the model has not read: how long it waited, and stopping the command that holds it back (T538: a
"二维码过期，马上重发" sent during a long blocking command was read 4 minutes later)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.processes import leftover_check
from support.world import TopicFixture, wait_for

from covey.app import steer_interrupt
from covey.backends.claude import command_stop
from covey.backends.claude.control import ClaudeControl
from covey.backends.claude.events import ClaudeEventAdapter
from covey.cards.steer_receipts import INTERRUPT_KIND, receipt_card, stage_line
from covey.core.deliveries import INTERRUPT, delivery_path, offers, read_delivery
from covey.core.harness import HarnessCapabilities, for_device
from covey.core.steer import is_urgent, keeps_running

setUpModule, tearDownModule = leftover_check()


class UrgentWordsTest(unittest.TestCase):
    def test_words_that_ask_for_it_now_are_urgent(self):
        for text in ("二维码过期，马上重发", "先停一下", "立刻看这个", "别等了，直接发", "STOP", "赶紧改回来"):
            self.assertTrue(is_urgent(text), text)
        for text in ("顺便看看 docs/ui.md", "另外把测试也跑一下", "", None):
            self.assertFalse(is_urgent(text), text)

    def test_a_push_a_deploy_or_an_install_keeps_running(self):
        for command in ("git push origin main", "git -C covey push", "scripts/deploy_bridge.sh --gen 410",
                        "pip install -r requirements.txt", "kubectl apply -f x.yaml", "rsync -a a b:/c",
                        "curl -s -X POST https://x/api", "lark-cli im +messages-send"):
            self.assertTrue(keeps_running(command), command)
        for command in ("sleep 600", "python3 -m pytest", "while ! test -f done; do sleep 5; done",
                        "curl -s https://x/status", "rg -n foo covey", "pip download x"):
            self.assertFalse(keeps_running(command), command)


class CommandStopTest(unittest.TestCase):
    """The shells are found among a process's children by their command line, as Claude Code runs them."""

    def spawn(self, command):
        line = "true && eval " + command_stop.quoted(command) + "' < /dev/null && true"
        process = subprocess.Popen(["bash", "-c", line], start_new_session=True)
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()

    def test_the_foreground_command_is_found_and_stopped_with_what_it_started(self):
        command = "sleep 30 # t538 'quoted' \"x\"\nsleep 30"
        waited_on, background = self.spawn(command), self.spawn("sleep 31 # in the background")
        wait_for(self, lambda: command_stop.shells(os.getpid(), [command]))
        found = command_stop.shells(os.getpid(), [command])
        self.assertEqual([pid for pid, _ in found], [waited_on.pid])
        started = time.monotonic()
        self.assertEqual(command_stop.stop(found), 1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(waited_on.wait(5))
        self.assertIsNone(background.poll())  # Not the command waited on: it goes on.
        with self.assertRaises(ProcessLookupError):
            os.killpg(waited_on.pid, 0)  # Its sleep, in its group, went with it.

    def test_a_command_with_no_shell_finds_nothing(self):
        self.assertEqual(command_stop.shells(os.getpid(), ["sleep 99 # never started"]), [])
        self.assertEqual(command_stop.stop([(2 ** 22 - 3, 1)]), 0)  # A pid that names nothing is not signalled.


class ForegroundCommandsTest(unittest.TestCase):
    def test_only_the_main_tasks_running_foreground_bash_calls(self):
        adapter = ClaudeEventAdapter(lambda *_: None)
        start = lambda key, inputs, child=None, name="Bash": adapter.tool_started(  # noqa: E731
            {"id": key, "name": name, "input": inputs}, {"parent_tool_use_id": child}, child)
        start("a", {"command": "sleep 600"})
        start("b", {"command": "sleep 700", "run_in_background": True})
        start("c", {"command": "sleep 800"}, child="agent-1")
        start("d", {"file_path": "/absolute/x"}, name="Read")
        start("e", {"command": "make test"})
        self.assertEqual(adapter.foreground_commands(), ["sleep 600", "make test"])
        adapter.tool_finished({"tool_use_id": "a"}, {})
        adapter.command_task("e", {"subtype": "task_started", "is_backgrounded": True}, "running")  # Moved there.
        self.assertEqual(adapter.foreground_commands(), [])
        self.assertNotIn("a", adapter.shells)


class StopCommandTest(unittest.TestCase):
    def control(self, commands):
        sent = []
        process = SimpleNamespace(send=sent.append, process=SimpleNamespace(pid=4242))
        control = ClaudeControl(process, SimpleNamespace(submit=lambda _: None, session_id="s"), lambda *_: None)
        control.foreground = lambda: list(commands)
        return control, sent

    def test_the_note_goes_in_before_the_command_is_stopped(self):
        control, sent = self.control(["sleep 600"])
        order = []
        with mock.patch.object(command_stop, "shells", return_value=[(7, 1)]) as shells, \
                mock.patch.object(command_stop, "stop", side_effect=lambda found: order.append(("stop", len(sent)))):
            outcome = control.stop_command("为什么停")
        self.assertEqual(outcome, {"stopped": ["sleep 600"]})
        shells.assert_called_once_with(4242, ["sleep 600"])
        self.assertEqual(order, [("stop", 1)])
        self.assertIn("为什么停", sent[0]["message"]["content"])

    def test_nothing_is_stopped_for_a_push_no_command_or_a_question(self):
        with mock.patch.object(command_stop, "stop") as stop:
            control, sent = self.control(["sleep 5", "git push origin main"])
            self.assertEqual(control.stop_command("n"), {"kept": "git push origin main"})
            control, sent = self.control([])
            self.assertIn("reason", control.stop_command("n"))
            control, _ = self.control(["sleep 600"])
            control.user_requests.add("q")
            self.assertIn("回答", control.stop_command("n")["reason"])
            with mock.patch.object(command_stop, "shells", return_value=[]):
                control, sent = self.control(["sleep 600"])
                self.assertIn("没找到", control.stop_command("n")["reason"])
        stop.assert_not_called()
        self.assertEqual(sent, [])

    def test_a_device_task_has_no_command_stop(self):
        caps = HarnessCapabilities(command_stop=True)
        self.assertFalse(for_device(caps, SimpleNamespace(device="testbox", options={})).command_stop)


class ReceiptTest(unittest.TestCase):
    RECORD = {"status": "sent", "reads": True, "stops": True, "job_id": 9, "sent_at": 100.0, "text": "马上重发",
              "token": "c" * 64, "topic_id": "t"}

    def test_unread_it_says_how_long_and_offers_the_interrupt_once(self):
        self.assertIn(INTERRUPT, offers(self.RECORD))
        with mock.patch("time.time", return_value=100.0 + 200):
            self.assertEqual(stage_line(self.RECORD), "✓ 已送达 · 还没读到 · 已等 3 分 20 秒")
            card = receipt_card(self.RECORD)
        self.assertIn(INTERRUPT_KIND, str(card))
        tried = {**self.RECORD, "interrupt_at": 120.0, "interrupt": "已停掉正在跑的命令，模型马上读到"}
        self.assertNotIn(INTERRUPT, offers(tried))
        self.assertIn("已停掉", stage_line(tried))
        for done in ({"status": "read"}, {"target_ended": True}, {"merged": True}, {"stops": False}, {"reads": False}):
            self.assertNotIn(INTERRUPT, offers({**self.RECORD, **done}), done)


class WatchTest(TopicFixture, unittest.TestCase):
    """The watch in the process that holds the task's backend: an urgent one stops the command once, any other
    repaints its receipt while it is unread."""

    def setUp(self):
        super().setUp()
        self.backend.declare(steering=True, steer_reads=True, command_stop=True)
        self.app.steering.show_steer_receipt = mock.Mock(return_value=True)
        self.app.steering.note_latest_request = mock.Mock()
        self.stops = []
        self.stopped = threading.Event()
        self.control = SimpleNamespace(supports_steer=True, steer=lambda text, mid: None,
                                       stop_command=self.stop_command)
        self.receive("om_run", "task")
        self.job = self.app.runtime.jobs.queue.get_nowait()
        self.job.execution_started = True
        self.job.backend_control = self.control
        self.app.runtime.jobs.register(self.job)
        self.ctx = self.topic("om_run")  # The task's topic.
        self.assertEqual(self.ctx.topic_id, self.job.topic_id)
        self.enterContext(mock.patch.dict(os.environ, {steer_interrupt.SWITCH: "0.3"}))
        self.enterContext(mock.patch.object(steer_interrupt, "REPAINT_AT", (0.6,)))

    def stop_command(self, note):
        self.stops.append(note)
        self.stopped.set()
        return {"stopped": ["sleep 600"]}

    def send(self, text, mid="steer-1", **fields):
        with mock.patch.object(self.app.steering, "steer_stage", return_value="running"):
            self.app.steering.deliver_steer(self.ctx, self.job, text, mid,
                                            receipt={"kind": "browse", "source": "form"}, **fields)
        return delivery_path(self.ctx.state_dir, self.job.topic_id, mid)

    def test_an_urgent_supplement_still_unread_has_the_command_stopped_once(self):
        path = self.send("二维码过期，马上重发")
        self.assertTrue(read_delivery(path)["stops"])
        self.assertTrue(self.stopped.wait(5))
        wait_for(self, lambda: read_delivery(path).get("interrupted") == "sleep 600")
        record = read_delivery(path)
        self.assertEqual((record["interrupt_by"], record["status"]), ("auto", "sent"))
        self.assertNotIn(INTERRUPT, offers(record))
        self.assertIn("它不是自己失败的", self.stops[0])
        time.sleep(0.8)
        self.assertEqual(len(self.stops), 1)
        self.app.steering.note_latest_request.assert_called_with(self.ctx, self.job, "二维码过期，马上重发", "steer-1",
                                                                 after="已停掉正在跑的命令，马上读到")

    def test_another_one_is_only_said_unread_on_its_receipt(self):
        path = self.send("顺便把文档也看一下")
        wait_for(self, lambda: self.app.steering.show_steer_receipt.call_count >= 2)
        self.assertEqual(self.stops, [])
        self.assertIn(INTERRUPT, offers(read_delivery(path)))

    def test_one_read_in_time_is_left_alone(self):
        path = self.send("马上看")
        self.app.steering.mark_steer_read(self.ctx, self.job, "steer-1")
        time.sleep(0.9)
        self.assertEqual(self.stops, [])
        self.assertEqual(read_delivery(path)["status"], "read")

    def test_zero_turns_the_automatic_stop_off(self):
        with mock.patch.dict(os.environ, {steer_interrupt.SWITCH: "0"}), \
                mock.patch.object(steer_interrupt, "URGENT_SECONDS", 0.3):
            self.send("马上重发")
            wait_for(self, lambda: self.app.steering.show_steer_receipt.called)
        self.assertEqual(self.stops, [])

    def test_the_button_stops_it_and_nothing_to_stop_lets_it_be_tried_again(self):
        path = self.send("顺便看看")
        outcomes = [{"reason": "模型现在没有在等命令"}, {"stopped": ["make test"]}]
        self.control.stop_command = lambda note: outcomes.pop(0)
        press = lambda: self.app.interaction.interaction_action(self.ctx, {  # noqa: E731
            "chat_id": self.job.chat_id, "event_id": f"e-{len(outcomes)}", "action_value": {
                "kind": INTERRUPT_KIND, "topic_id": self.ctx.topic_id, "token": path.stem, "job_id": self.job.job_id}})
        press()
        wait_for(self, lambda: "没有打断" in str(read_delivery(path).get("interrupt")))
        self.assertIn(INTERRUPT, offers(read_delivery(path)))  # Nothing was stopped: the button stays.
        press()
        wait_for(self, lambda: read_delivery(path).get("interrupted") == "make test")
        self.assertEqual(read_delivery(path)["interrupt_by"], "click")
        self.assertNotIn(INTERRUPT, offers(read_delivery(path)))

    def test_the_button_on_a_task_no_longer_here_says_so(self):
        path = self.send("顺便看看")
        self.app.runtime.unregister_reply_job(self.job)
        with self.assertRaisesRegex(ValueError, "打断不了"):
            self.app.steering.steer_receipt_action(self.ctx, self.job.chat_id, INTERRUPT_KIND,
                                                   {"token": path.stem, "job_id": self.job.job_id}, "e-1")


if __name__ == "__main__":
    unittest.main()
