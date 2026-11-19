"""Millisecond timing (covey/core/timing.py) and its reader (scripts/bridge_timing.py).

The Bridge's live flows write their lines with the phases each names: a new message to its
launch card, 开始任务 to its receipt and task card, a bot menu click, a follow-up to its
queued card, a click forwarded to a task runner and that runner's handling of it, a job's
hand-off and a runner's start. The lines never hold a body, a token or user text; nothing
is written while timing is off; calls outside every flow are summarized. The reader turns a
recorded sample into its per-flow tables. Feishu is a fake lark-cli at subprocess.run, as
in the replay bench; nothing here waits on a real one."""

import contextlib
import importlib.util
import io
import itertools
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO, quiet
from support.processes import Leftovers
from support.world import stop_background, wait_for
from support.records import run_files

from covey.app import runner_ipc
from covey.app.card_lanes import CardLanes
from covey.app.runtime import print_log
from covey.core import run_dir as rd, timing
from covey.core.state import write_json, write_private_text
from covey.feishu import transport
from covey.feishu.events import REPLY_CONTEXT

_spec = importlib.util.spec_from_file_location("bridge_timing_script", REPO / "scripts" / "bridge_timing.py")
reader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reader)

REAL_RUN = subprocess.run
TASK_TEXT = "请把报表里的密钥 sk-用户文本 整理一下"  # User text: never in a timing line.
CLICK_TOKEN = "c-0123456789abcdef-token"  # A card callback's token: never in a timing line.
# The steps of a message's look for an answer or a held note (app/interaction.py), before "command".
COMMAND = ["cmd_reconcile", "cmd_questions", "cmd_undelivered", "cmd_capabilities", "cmd_pending"]
# The steps of a queued job's records (app/submission.py), before "recorded": the topic's task waiting for
# an answer looked up (none here), the card's alias, the job's record.
RECORDED = ["ack_query", "alias", "state_write", "recorded"]


class FakeLark:
    """lark-cli at subprocess.run, answering each call at once as Feishu would; anything else runs."""

    def __init__(self):
        self.calls = []
        self.ids = itertools.count(1)

    def __call__(self, argv, *args, **kwargs):
        if not argv or argv[0] != "lark-cli":
            return REAL_RUN(argv, *args, **kwargs)
        argv = list(argv)
        self.calls.append(argv)
        n = next(self.ids)
        if "api" in argv and argv[argv.index("api") + 1] == "GET" and "/messages/" in argv[argv.index("api") + 2]:
            mid = argv[argv.index("api") + 2].rsplit("/", 1)[1]
            out = {"code": 0, "data": {"items": [{
                "message_id": mid, "msg_type": "text", "chat_id": "chat",
                "body": {"content": json.dumps({"text": "引用的内容"})},
                "sender": {"id": "owner", "sender_type": "user", "id_type": "open_id"},
                "create_time": str(int(time.time() * 1000))}]}}
        elif "api" in argv and argv[argv.index("api") + 1:argv.index("api") + 3] == ["POST", "/open-apis/cardkit/v1/cards"]:
            out = {"code": 0, "data": {"card_id": f"card-{n}"}}
        elif "+messages-reply" in argv or "+messages-send" in argv:
            out = {"ok": True, "data": {"message_id": f"om_out_{n}"}}
        else:
            out = {"code": 0, "data": {}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")


def fields(line):
    return dict(item.split("=", 1) for item in line.split() if "=" in item)


def phases(line):
    return [(name, int(ms)) for name, _, ms in (p.rpartition(":") for p in fields(line)["phases"].split(",") if p)]


def phase_names(line):
    return [name for name, _ in phases(line)]


class Sink:
    """Timing on, its lines collected, for one test."""

    def __init__(self, test):
        self.lines = []
        test.addCleanup(timing.configure, timing.configure(self.lines.append))
        test.addCleanup(timing.flush)

    def flows(self, name=None):
        return [line for line in self.lines if line.startswith("timing ")
                and (name is None or fields(line)["flow"] == name)]

    def calls(self):
        return [line for line in self.lines if line.startswith("feishu call ")]


class LiveFlowsTest(unittest.TestCase):
    """The Bridge's flows, from the event to what the user sees, against the fake lark-cli."""

    def setUp(self):
        from covey.app.unified import UnifiedBridgeApp

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "project").mkdir()
        self.app = UnifiedBridgeApp(script_path=REPO / "bin/covey_bridge.py")
        self.addCleanup(stop_background, self, lambda: [self.app], Leftovers())
        args = self.app.build_parser().parse_args(["--workdir", str(root / "project"), "--state-dir", str(root / "state"),
                                                   "--profile", "test-bot"])
        self.ctx = self.app.build_context(args)
        self.logs = quiet(self.app)
        self.app.runtime.owner_open_id = "owner"
        self.app.runtime.access.identify(app_id="cli_test", bot_open_id="ou_bot")
        self.addCleanup(transport.configure_receipts, transport.configure_receipts(self.ctx.state_dir))
        for name, app in self.app.apps.items():
            write_private_text(self.ctx.state_dir / "launch-models" / (name + ".json"), json.dumps({
                "updated_at": time.time(), "models": {name + "-model": {
                    "display_name": name + " model", "levels": ["low", "high"], "default": "high"}}}))
            app.harness.started = True
            app.models.prepare_submission_context = lambda ctx: ctx
        # The last launch picked Claude (MASO's entries need a budget chosen first).
        write_json(self.ctx.state_dir / "launch-defaults.json", {"model_choice": "claude:claude-model", "effort": "high"})
        self.lark = FakeLark()
        patcher = mock.patch("covey.feishu.transport.subprocess.run", side_effect=self.lark)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sink = Sink(self)
        self.lanes = CardLanes(self.app.handle_card_action_event, self.app.card_lane_key, self.app.runtime.log,
                               self.app.runtime.shutdown)

    def message(self, mid, text, root=None, *, sent_ms_ago=800):
        now = int(time.time() * 1000)
        return {"chat_id": "chat", "chat_type": "p2p", "sender_id": "owner", "sender_type": "user",
                "message_type": "text", "content": text, "message_id": mid, "id": mid, "root_id": root,
                "parent_id": None, "reply_to": root, "thread_id": "omt_1" if root else None,
                "create_time": str(now - sent_ms_ago), "timestamp": str(now - sent_ms_ago + 100),
                "event_id": "ev-" + mid, REPLY_CONTEXT: True}

    def receive(self, event):
        timing.arrived()  # As the consumer does when it reads the event.
        self.app.handle_event(event, self.ctx)

    def click(self, event):
        timing.arrived()
        self.lanes.submit(event, self.ctx)

    def flow_line(self, name, **match):
        def found():
            return [line for line in self.sink.flows(name) if all(fields(line).get(k) == v for k, v in match.items())]
        wait_for(self, lambda: found())
        return found()[-1]

    def launch(self):
        """A new message's launch card, then 开始任务 on it: the started topic's first job."""
        from covey.cards.launch_config import SUBMIT_PREFIX

        self.receive(self.message("om_task", TASK_TEXT))
        path = next((self.ctx.state_dir / "launch-forms").glob("*.json"))
        record = json.loads(path.read_text())
        self.click({"operator_id": "owner", "chat_id": "chat", "message_id": record["message_id"],
                    "event_id": "ev-submit", "action_tag": "button", "action_name": SUBMIT_PREFIX + path.stem,
                    "form_value": {"path": ""}, "token": CLICK_TOKEN,
                    "timestamp": str(int(time.time() * 1_000_000) - 600_000)})  # A click's is in microseconds.
        return self.flow_line("click", kind="bridge_launch_submit")

    def test_a_new_message_reads_off_one_line_to_its_launch_card(self):
        self.receive(self.message("om_task", TASK_TEXT))
        [line] = self.sink.flows("message")
        values = fields(line)
        self.assertEqual((values["outcome"], values["chat"], values["message"], values["calls"]),
                         ("launch_card", "p2p", "om_task", "2"))
        self.assertGreaterEqual(int(values["feishu_ms"]), 800)  # From the message's create_time to its arrival.
        self.assertGreaterEqual(int(values["event_ms"]), 700)
        self.assertRegex(values["scan"], r"\bjobs:\d+")  # Each record directory rescan the message paid for.
        self.assertEqual(phase_names(line), ["access", "dedup", "route", "enrich", "context", *COMMAND, "command",
                                             "history", "prepare", "records", "state_write", "reserve", "build",
                                             "card_create", "msg_reply", "saved"])
        offsets = [ms for _, ms in phases(line)]
        self.assertEqual(offsets, sorted(offsets))
        self.assertLessEqual(offsets[-1], int(values["total_ms"]))
        # Each Feishu call inside it has its own line, named as its phase.
        self.assertEqual([fields(c)["op"] for c in self.sink.calls()], ["card_create", "msg_reply"])
        create, reply = (fields(c) for c in self.sink.calls())
        self.assertEqual((create["api"], create["via"], create["result"], create["attempts"], create["flow"]),
                         ("POST:/open-apis/cardkit/v1/cards", "cli", "ok", "1", "message:om_task"))
        self.assertEqual(reply["api"], "im+messages-reply")
        self.assertIn("thread", reply)
        self.assertEqual(len(self.lark.calls), 2)

    def test_start_task_reads_off_one_line_to_its_task_card_and_names_the_job(self):
        line = self.launch()
        values = fields(line)
        self.assertEqual((values["outcome"], values["event"], values["calls"]), ("handled", "ev-submit", "3"))
        self.assertGreaterEqual(int(values["feishu_ms"]), 600)  # From the click's own timestamp.
        self.assertLess(int(values["feishu_ms"]), 60_000)
        job = self.app.runtime.jobs.by_id[int(values["jobs"].lstrip("#"))]
        self.assertEqual(job.message_id, "om_task")
        # Acknowledged once bound; then the task card in its follow-up and the receipt beside it (S36),
        # both still on this line.
        names = phase_names(line)
        self.assertEqual(names[:7], ["lane", "access", "route", "job_route", "checked", "bound", "ack"])
        receipt = {"beside", "card_put"}
        self.assertEqual([n for n in names[7:] if n not in receipt],
                         ["followup", "card_create", "msg_reply", *RECORDED, "queued"])
        # The receipt, then its retry, which finds it written.
        self.assertEqual([n for n in names[7:] if n in receipt], ["beside", "card_put", "beside"])

    def test_a_bot_menu_click_reads_off_one_line(self):
        self.receive(self.message("om_dm", TASK_TEXT))  # The owner's private chat is known from here.
        timing.arrived()
        self.app.handle_menu_event({"event_id": "ev-menu", "event_key": "weekly_usage", "operator_open_id": "owner"},
                                   self.ctx)
        line = self.flow_line("menu", key="weekly_usage")
        self.assertEqual(fields(line)["outcome"], "weekly_usage")
        self.assertEqual(phase_names(line)[:2], ["dedup", "chat"])

    def test_a_follow_up_message_reads_off_one_line_to_its_queued_card(self):
        self.launch()
        self.receive(self.message("om_more", "再补一句：" + TASK_TEXT, root="om_task"))
        line = self.flow_line("message", message="om_more")
        values = fields(line)
        self.assertEqual((values["outcome"], values["parent"]), ("queued_card", "fetched"))
        self.assertEqual(phase_names(line), ["access", "dedup", "route", "msg_get", "enrich", "context", *COMMAND,
                                             "command", "history", "prepare", "records", "reserve", "card_create", "msg_reply",
                                             *RECORDED, "queued"])
        self.assertEqual(self.app.runtime.jobs.by_id[int(values["jobs"].lstrip("#"))].message_id, "om_more")

    def test_a_click_on_a_running_job_is_forwarded_and_its_runner_line_joins_it(self):
        job = self.app.runtime.jobs.by_id[int(fields(self.launch())["jobs"].lstrip("#"))]
        run = run_files(self.ctx.state_dir, "om_task", folders=(rd.INBOX, rd.ACKS))
        stop = {"operator_id": "owner", "chat_id": "chat", "message_id": job.progress.message_id, "event_id": "ev-stop",
                "action_tag": "button", "token": CLICK_TOKEN,
                "action_value": {"kind": "bridge_job_stop", "job_id": str(job.job_id), "message_id": job.message_id}}
        with mock.patch.object(runner_ipc, "runner_for", return_value=run):
            self.click(stop)
            bridge = self.flow_line("click", event="ev-stop")
        self.assertEqual(fields(bridge)["outcome"], "forwarded")
        # A job this Bridge runs: its runner found without reading the jobs records (runner_ipc._forward_to_live).
        self.assertEqual(phase_names(bridge)[-6:], ["job_route", "fwd_live", "inbox", "wake", "forward", "ack"])
        # The runner takes it from its inbox: its own line says how long it waited there.
        [path] = (run / rd.INBOX).glob("*.json")
        payload = json.loads(path.read_text())
        written = int(payload["id"].split("-")[0])
        aged = run / rd.INBOX / f"{written - 300_000_000}-{payload['id'].split('-')[1]}.json"
        path.rename(aged)
        aged.write_text(json.dumps({**payload, "id": aged.stem}))
        handled = []

        def handle(event, ctx):
            handled.append(event["event_id"])
            transport.call(transport.lark_argv("test-bot", "api", "PATCH", "/open-apis/cardkit/v1/cards/c1/settings",
                                               "--data", json.dumps({"settings": TASK_TEXT})))
            timing.note(outcome="handled")
        gateway = SimpleNamespace(handle_card_action_event=handle, runtime=self.app.runtime)
        app = SimpleNamespace(harness=SimpleNamespace(context=self.ctx))
        with mock.patch.object(runner_ipc, "context_for_job", return_value=self.ctx):
            runner_ipc.take_inbox(gateway, app, SimpleNamespace(job_id=job.job_id), self.ctx, run)
        self.assertEqual(handled, ["ev-stop"])
        line = self.sink.flows("click")[-1]
        values = fields(line)
        self.assertEqual((values["kind"], values["outcome"], values["event"], values["runner"]),
                         ("bridge_job_stop", "handled", "ev-stop", f"#{job.job_id}"))
        self.assertGreaterEqual(int(values["inbox_ms"]), 300)
        self.assertEqual(phase_names(line), ["card_settings", "ack"])

    def test_no_line_holds_user_text_a_body_or_a_token(self):
        self.launch()
        self.receive(self.message("om_more", "再补一句：" + TASK_TEXT, root="om_task"))
        self.flow_line("message", message="om_more")
        self.assertTrue(self.sink.flows() and self.sink.calls())
        # The calls carried the user's text (the queued card shows it) and card JSON in their arguments.
        self.assertTrue(any(TASK_TEXT in arg for argv in self.lark.calls for arg in argv))
        self.assertTrue(any("card_json" in arg for argv in self.lark.calls for arg in argv))
        for line in self.sink.lines:
            for secret in (TASK_TEXT, "密钥", "引用", CLICK_TOKEN, "test-bot", "{", '"', "card_json", "--"):
                self.assertNotIn(secret, line)
            self.assertRegex(line, r"^(timing|feishu calls?) [\w#:.+/@=, -]+$")


class TimingTest(unittest.TestCase):
    def call(self, argv, runner=None, **kwargs):
        return transport.call(argv, runner=runner or (lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, json.dumps({"code": 0, "data": {"ok": 1}}), "")), **kwargs)

    def test_off_by_default_nothing_is_written_and_calls_behave_alike(self):
        self.assertFalse(timing.enabled())
        self.assertIsNone(timing.begin("message"))
        with timing.flow("click") as flow:
            self.assertIsNone(flow)
            timing.mark("access")
        argv = transport.lark_argv("p", "api", "PUT", "/open-apis/cardkit/v1/cards/c1", "--data", "{}")
        off = self.call(argv)
        lines = Sink(self).lines
        with timing.flow("click"):
            on = self.call(argv)
        self.assertEqual(off, on)
        self.assertEqual(len(lines), 2)

    def test_feishu_delay_reads_a_clicks_microseconds_and_a_messages_milliseconds(self):
        # S32 开始任务: Feishu's click timestamp in microseconds, 477 ms before the consumer read it.
        wall = 1793019273.729
        self.assertEqual(timing.delay_ms("1793019273252000", wall), 477)
        self.assertEqual(timing.delay_ms("1793019273252", wall), 477)  # A message's create_time.
        self.assertEqual(timing.delay_ms(1793019273252, wall), 477)
        self.assertIsNone(timing.delay_ms(None, wall))
        self.assertIsNone(timing.delay_ms("", wall))

    def test_a_call_line_names_the_operation_never_its_arguments(self):
        sink = Sink(self)
        failing = lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", json.dumps(
            {"ok": False, "error": {"type": "api", "code": 230001, "message": "bad " + TASK_TEXT}}))
        sdk = lambda argv, **_: subprocess.CompletedProcess(argv, 0, json.dumps({"code": 0, "data": {}}), "")
        sdk.via = "sdk"
        with timing.flow("click", event="ev-1"):
            self.call(transport.lark_argv("prof", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1",
                                          "--markdown", TASK_TEXT, "--idempotency-key", "k"), runner=failing)
            self.call(transport.lark_argv("prof", "api", "PATCH", "/open-apis/cardkit/v1/cards/7700/elements/status_line",
                                          "--data", json.dumps({"token": CLICK_TOKEN})), runner=sdk)
            with mock.patch("covey.feishu.transport.subprocess.run",
                            return_value=subprocess.CompletedProcess([], 3, "", TASK_TEXT)):
                transport.run_cli(["lark-cli", "--profile", "prof", "event", "schema", "im.message.receive_v1", "--json"],
                                  capture_output=True)
        send, patch, schema = (fields(line) for line in sink.calls())
        self.assertEqual((send["op"], send["api"], send["result"], send["via"]),
                         ("msg_send", "im+messages-send", "api_rejected:230001", "cli"))
        self.assertEqual((patch["op"], patch["api"], patch["via"], patch["result"]),
                         ("card_patch", "PATCH:/open-apis/cardkit/v1/cards/:id/elements/:id", "sdk", "ok"))
        self.assertEqual((schema["op"], schema["result"]), ("event_schema", "rc:3"))
        [flow] = sink.flows()
        self.assertEqual(phase_names(flow), ["msg_send", "card_patch", "event_schema"])
        for line in sink.lines:
            for secret in (TASK_TEXT, CLICK_TOKEN, "prof", "oc_1", "7700", "status_line"):
                self.assertNotIn(secret, line)

    def test_a_call_line_carries_feishus_own_time_and_the_body_size(self):
        sink = Sink(self)

        def sdk(argv, **_):
            done = subprocess.CompletedProcess(argv, 0, json.dumps({"code": 0, "data": {}}), "")
            done.served = timing.served_ms({"server-timing": "inner; dur=640.4, cdn-cache;desc=MISS,edge;dur=0,origin;dur=700"})
            return done
        sdk.via = "sdk"
        body = json.dumps({"card": TASK_TEXT}, ensure_ascii=False)
        argv = transport.lark_argv("p", "api", "PUT", "/open-apis/cardkit/v1/cards/c1", "--data", body)
        with timing.flow("click", event="ev-1"):
            self.call(argv, runner=sdk)
            self.call(argv)  # lark-cli: no Server-Timing.
        own, cli = (fields(line) for line in sink.calls())
        self.assertEqual((own["inner_ms"], own["origin_ms"], own["bytes"]), ("640", "700", str(len(body.encode()))))
        self.assertNotIn("inner_ms", cli)
        self.assertEqual(cli["bytes"], own["bytes"])
        with mock.patch.object(timing, "FLUSH_SECONDS", 0):
            self.call(argv, runner=sdk)  # Outside every flow: its minute's summary has them too.
        summary = fields(sink.lines[-1])
        self.assertEqual((summary["op"], summary["inner_p50"], summary["origin_p50"]), ("card_put", "640", "700"))
        self.assertEqual(timing.served_ms({"Server-Timing": "inner;dur=7"}), {"inner": 7})
        for headers in ({}, {"Server-Timing": "cdn-cache;desc=MISS,edge;dur=0"}, {"server-timing": "inner; dur=x"}, None):
            self.assertEqual(timing.served_ms(headers), {})

    def test_describe_names_every_kind_of_call(self):
        cases = {
            ("api", "POST", "/open-apis/cardkit/v1/cards"): ("card_create", "POST:/open-apis/cardkit/v1/cards"),
            ("api", "PUT", "/open-apis/cardkit/v1/cards/1/elements/e/content"): (
                "card_text", "PUT:/open-apis/cardkit/v1/cards/:id/elements/:id/content"),
            ("api", "POST", "/open-apis/cardkit/v1/cards/1/batch_update"): (
                "card_batch", "POST:/open-apis/cardkit/v1/cards/:id/batch_update"),
            ("api", "GET", "/open-apis/im/v1/messages/om_1"): ("msg_get", "GET:/open-apis/im/v1/messages/:id"),
            ("api", "GET", "/open-apis/im/v1/chats/oc_1"): ("chat_get", "GET:/open-apis/im/v1/chats/:id"),
            ("api", "GET", "/open-apis/drive/v1/files/f1/statistics"): ("get_drive_v1_files_statistics",
                                                                        "GET:/open-apis/drive/v1/files/:id/statistics"),
            ("im", "+messages-reply", "--message-id", "om_1"): ("msg_reply", "im+messages-reply"),
            ("im", "+messages-resources-download", "--file-key", "k"): ("messages_resources_download",
                                                                        "im+messages-resources-download"),
            ("im", "images", "create", "--data", "{}"): ("im_images", "im_images"),
            ("auth", "status", "--verify", "--json"): ("auth_status", "auth_status"),
            ("event", "status", "--current"): ("event_status", "event_status"),
        }
        for args, expected in cases.items():
            for profile in ("harness", None):
                with self.subTest(args=args, profile=profile):
                    self.assertEqual(timing.describe(transport.lark_argv(profile, *args)), expected)

    def test_calls_outside_every_flow_are_summarized_a_minute_at_a_time(self):
        sink = Sink(self)
        failing = lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "{}")
        argv = transport.lark_argv("p", "api", "PUT", "/open-apis/cardkit/v1/cards/c1/elements/e1/content", "--data", "{}")
        for runner in (None, None, failing):
            self.call(argv, runner=runner)
        self.assertEqual(sink.lines, [])  # Streaming updates and other calls outside a flow: counted only.
        with mock.patch.object(timing, "FLUSH_SECONDS", 0):
            self.call(argv)
        [line] = sink.lines
        values = fields(line)
        self.assertTrue(line.startswith("feishu calls op=card_text "))
        self.assertEqual((values["n"], values["errors"]), ("4", "1"))
        self.assertIn("ms_p50", values)
        self.assertIn("proc_p50", values)
        timing.flush()
        self.assertEqual(len(sink.lines), 1)  # Nothing left to write.

    def test_a_flow_waits_for_the_work_it_carried_to_another_thread(self):
        sink = Sink(self)
        go, done = threading.Event(), []
        with timing.flow("message", started=timing.at(time.time() - 0.25), message="om_1") as flow:
            timing.mark("access")
            work = timing.carry(lambda: (go.wait(5), timing.mark("card"), done.append(1)), "worker")
            thread = threading.Thread(target=work)
            thread.start()
            dropped = timing.carry(lambda: None)
            timing.drop(dropped)  # One that will never run does not keep the line back.
        self.assertEqual(sink.flows(), [])
        go.set()
        thread.join(5)
        [line] = sink.flows()
        self.assertEqual(phase_names(line), ["access", "worker", "card"])
        self.assertGreaterEqual(int(fields(line)["total_ms"]), 250)
        self.assertTrue(flow.closed)
        # A thread already in a flow joins it rather than opening another.
        with timing.flow("click") as outer:
            self.assertIsNone(timing.begin("message"))
            timing.mark("inner")
        self.assertEqual(phase_names(sink.flows("click")[0]), ["inner"])
        self.assertIsNotNone(outer)

    def test_a_runner_start_ends_at_its_first_call_on_any_thread(self):
        sink = Sink(self)
        started = timing.begin("runner", started=timing.at(time.time() - 1.5), process=True, until_call=True,
                               job="#9", harness="claude")
        timing.mark("built")
        # The job's own hand-off flow joins it, as process_reply_job does inside a runner.
        self.assertIsNone(timing.begin("job_start", job="#9"))
        timing.mark("check")
        thread = threading.Thread(target=self.call, args=(transport.lark_argv("p", "api", "PATCH",
                                                                                "/open-apis/cardkit/v1/cards/c/settings"),))
        thread.start()
        thread.join(5)
        [line] = sink.flows()
        self.assertEqual(phase_names(line), ["built", "check", "card_settings"])
        self.assertGreaterEqual(int(fields(line)["total_ms"]), 1500)
        self.assertEqual(fields(sink.calls()[0])["flow"], "runner:#9")
        timing.mark("later")  # Ended: later phases and calls are not its.
        self.call(transport.lark_argv("p", "api", "PATCH", "/open-apis/cardkit/v1/cards/c/settings"))
        timing.end(started)
        self.assertEqual(len(sink.flows()), 1)
        self.assertIsNone(timing.current())
        # The Bridge's hand-off ends once the runner is spawned, not when the job does.
        with timing.flow("job_start", job="#10"):
            timing.mark("check")
            timing.mark("spawned")
            timing.end_current("job_start")
            timing.mark("job ran")
        self.assertEqual(phase_names(sink.flows("job_start")[0]), ["check", "spawned"])

    def test_the_log_prefix_has_milliseconds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_log("task runner started job=#1")
        self.assertRegex(out.getvalue(), r"^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\] task runner started")

    def test_an_ignored_message_writes_no_timing_line(self):
        sink = Sink(self)
        with timing.flow("message", message="om_1"):
            timing.outcome("ignored")
        with timing.flow("message", message="om_2"):
            timing.outcome("launch_card")
            timing.outcome("answer")  # What it called named the outcome first.
        [line] = sink.flows()
        self.assertEqual(fields(line)["outcome"], "launch_card")


class ReaderTest(unittest.TestCase):
    SAMPLE = REPO / "tests" / "fixtures" / "timing"

    def report(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reader.main(list(args))
        return code, out.getvalue()

    def test_a_recorded_sample_reads_into_per_flow_tables(self):
        code, text = self.report("--state", str(self.SAMPLE / "state"), "--since", "19:30")
        self.assertEqual(code, 0)
        self.assertIn("Flows (ms, p50/max)", text)
        row = next(line for line in text.splitlines() if line.startswith("message:launch_card "))
        # n, then p50/max of: Feishu's delay, total, Feishu API, Bridge local, calls.
        self.assertEqual(row.split()[1:], ["2", "640/910", "690/740", "1330/1650", "2115/2410", "640/700", "1475/1710", "2/2"])
        phases = text.split("Phases of message:launch_card")[1].split("\n\n")[0]
        self.assertRegex(phases, r"command\s+2\s+260/262")
        self.assertRegex(phases, r"msg_reply\s+2\s+410/430")
        forwarded = text.split("Forwarded clicks")[1].split("\n\n")[0]
        # Runner took it 330 ms after the Bridge's arrival (inbox 290); first card write 330+180, done 330+260.
        self.assertRegex(forwarded, r"bridge_job_stop\s+1\s+330/330\s+290/290\s+510/510\s+590/590")
        launch = text.split("开始任务 to its task runner")[1].split("\n\n")[0]
        self.assertRegex(launch, r"bridge_launch_submit\s+1\s+3420/3420\s+7050/7050")
        api = text.split("Feishu calls inside flows")[1].split("\n\n")[0]
        self.assertRegex(api, r"card_create\s+sdk\s+3\s+")
        self.assertRegex(api, r"msg_reply\s+cli\s+3\s+")
        self.assertIn("Feishu calls outside flows", text)
        self.assertNotIn("用户", text)  # A non-timing line's text never reaches the report.

    def test_a_click_logged_before_the_microsecond_fix_still_reads_its_delay(self):
        # Two S32 lines (ids replaced): their feishu_ms took the click's microseconds for milliseconds.
        lines = ["[2026-10-26T20:54:38.109+08:00] timing flow=click outcome=handled kind=bridge_launch_submit "
                 "event=ev1 t0=1793019273729 total_ms=4381 api_ms=1786 calls=3 feishu_ms=-1791226253978466 "
                 "phases=lane:1\n",
                 "[2026-10-26T21:04:35.381+08:00] timing flow=click outcome=launch_option kind=bridge_launch_model "
                 "event=ev2 t0=1793019874409 total_ms=972 api_ms=428 calls=1 feishu_ms=-1791226854142854 "
                 "phases=lane:1\n"]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bridge.log"
            log.write_text("".join(lines))
            code, text = self.report(str(log))
        self.assertEqual(code, 0)
        submit = next(line for line in text.splitlines() if line.startswith("click:bridge_launch_submit:handled "))
        model = next(line for line in text.splitlines() if line.startswith("click:bridge_launch_model:launch_option "))
        self.assertEqual((submit.split()[2], model.split()[2]), ("477/477", "392/392"))

    def test_feishus_own_time_reads_beside_each_call(self):
        lines = ["[2026-10-27T15:47:31.380+08:00] feishu call op=msg_reply api=im+messages-reply ms=811 proc_ms=788 "
                 "inner_ms=640 origin_ms=700 bytes=120 via=sdk attempts=1 result=ok thread=lane flow=click:ev1\n",
                 "[2026-10-27T15:47:32.380+08:00] feishu call op=msg_reply api=im+messages-reply ms=900 proc_ms=850 "
                 "via=cli attempts=1 result=ok thread=lane flow=click:ev2\n",  # Before S48, or lark-cli: none.
                 "[2026-10-27T15:48:00.000+08:00] feishu calls op=card_patch n=40 errors=0 ms_p50=484 ms_max=900 "
                 "proc_p50=470 inner_p50=380 origin_p50=420 window_s=60\n"]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bridge.log"
            log.write_text("".join(lines))
            code, text = self.report(str(log))
        self.assertEqual(code, 0)
        inside = text.split("Feishu calls inside flows")[1].split("\n\n")[0]
        self.assertRegex(inside, r"msg_reply\s+sdk\s+1\s+811/811\s+788/788\s+640/640\s+700/700\s+120/120\s+0")
        self.assertRegex(inside, r"msg_reply\s+cli\s+1\s+900/900\s+850/850\s+-\s+-\s+-\s+0")
        outside = text.split("Feishu calls outside flows")[1]
        self.assertRegex(outside, r"card_patch\s+40\s+0\s+484\s+900\s+380\s+420")

    def test_since_and_until_bound_the_lines(self):
        code, text = self.report(str(self.SAMPLE / "state" / "bridge.log"), "--since", "19:40", "--until", "19:41")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"\nmessage:queued_card\s+1\s")
        self.assertNotIn("message:launch_card", text)
        code, text = self.report(str(self.SAMPLE / "state" / "bridge.log"), "--since", "23:59")
        self.assertEqual((code, text.strip()), (1, "no timing lines in range"))


if __name__ == "__main__":
    unittest.main()
