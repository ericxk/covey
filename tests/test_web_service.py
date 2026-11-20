"""feishu/web_service.py without a browser: the commands one at a time on the main thread, ping answered while
one runs, no send, login or status any more, the acceptance page parked on close, the idle exit, its allowlist,
and the client's contract (not asked, asked, no last line). Pages are stand-ins; the socket is a real one in a
temporary folder, never the live state's."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covey.feishu import web_client, web_service  # noqa: E402


class Page:
    """A page that records where it went and what was pressed; ``shows``: [the chat list shown, a chat open]."""

    def __init__(self, title="", shows=(True, False), messages=()):
        self.title, self.did, self.url, self.closed = title, [], "about:blank", False
        self.shows, self.messages = list(shows), list(messages)
        self.keyboard = SimpleNamespace(press=lambda key: self.did.append(("press", key)),
                                        type=lambda text, delay=None: self.did.append(("type", text)),
                                        insert_text=lambda text: self.did.append(("insert", text)))

    def goto(self, url, **kwargs):
        self.url = url
        self.did.append(("goto", url))

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        if script == web_service.PARKED:
            return self.shows
        if script == web_service.MESSAGES:
            return self.messages
        raise AssertionError(script)

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


def context(*pages):
    """Its pages[0] and new_page: the pages given, then new ones (the parked tabs), all in ``tabs``."""
    made, tabs = list(pages[1:]), list(pages)

    def new_page():
        page = made.pop(0) if made else Page()
        tabs.append(page) if page not in tabs else None
        return page
    return SimpleNamespace(pages=[pages[0]], new_page=new_page, tabs=tabs)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ServiceCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.accept_page = Page()
        self.clock = Clock()
        self.service = web_service.Service(self.home, context(self.accept_page), idle=1800,
                                           chats=("验收测试群", "代答测试群"), clock=self.clock)
        self.service.running = None
        patcher = mock.patch.object(self.service, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_parked(self, name, old):
        """``old`` closed, ``name`` a new tab loading the chat list."""
        page = self.service.pages[name]
        self.assertTrue(old.closed)
        self.assertIsNot(page, old)
        self.assertEqual(page.did, [("goto", web_client.MESSENGER)])
        return page

    def run_job(self, request):
        said = []
        job = web_service.Job(request, None)
        job.say = said.append
        job.finish = lambda result: said.append({**result, "done": True})
        self.service.run(job)
        return said


class DispatchTest(ServiceCase):
    def test_send_login_and_status_are_refused_and_a_failing_command_is_said(self):
        # 代答 sends with the sender tool (2026-10-28); the service's send had no allowlist (repo review 2).
        for cmd in ("send", "login", "status", "rm -rf"):
            with self.subTest(cmd=cmd):
                [answer] = self.run_job({"cmd": cmd, "chat_name": "张三", "text": "好的"})
                self.assertTrue(answer["refused"])
                self.assertEqual(self.service.pages, {})  # No page opened, none parked.
        self.assertNotIn("logged_in", self.service.ping())
        with mock.patch.object(web_client, "header", side_effect=RuntimeError("boom")):
            [answer] = self.run_job({"cmd": "accept_text"})
        self.assertEqual((answer["ok"], answer["error"]), (False, "RuntimeError: boom"))
        self.assertIsNone(self.service.running)


class IdleTest(ServiceCase):
    def test_no_login_is_checked_and_idle_it_stops(self):
        self.service.step(wait=0)
        self.assertEqual((self.service.pages, self.accept_page.did), ({}, []))  # No 代答 page loaded to check a login.
        # A command done puts the idle exit off; 1800 s after the last one the service stops.
        self.clock.now += 700
        self.run_job({"cmd": "accept_close"})
        self.clock.now += 1799
        self.service.step(wait=0)
        self.assertFalse(self.service.stopping.is_set())
        self.clock.now += 1
        self.service.step(wait=0)
        self.assertTrue(self.service.stopping.is_set())


class ParkTest(ServiceCase):
    def test_a_parked_tab_is_kept_while_it_shows_the_list_and_no_chat_else_it_goes_blank(self):
        with mock.patch.object(web_client, "header", return_value="验收测试群"):
            self.run_job({"cmd": "accept_text"})
        self.run_job({"cmd": "accept_close"})
        tab = self.assert_parked("accept", self.accept_page)
        tab.shows = [False, False]
        self.clock.now += 5
        self.service.settle()
        self.assertEqual(self.service.ping()["parked"], {"accept": "loading"})
        tab.shows = [True, False]
        self.clock.now += 7
        self.service.settle()  # Shown once: loaded again (a click on a tab's first load leaves the chat blank).
        self.assertEqual((tab.did, self.service.ping()["parked"]),
                         ([("goto", web_client.MESSENGER)] * 2, {"accept": "reloading"}))
        self.run_job({"cmd": "rm -rf"})  # A command that does not use the tab: not swapped mid-load.
        self.assertIs(self.service.pages["accept"], tab)
        self.clock.now += 6
        self.service.settle()
        self.assertEqual((self.service.ping()["parked"], self.service.timing["park_accept_ms"]), ({"accept": "messenger"}, 18000))
        # A chat opened by itself: the tab to about:blank at once.
        tab.shows = [True, True]
        self.service.settle()
        self.assertEqual((tab.did[-1], self.service.ping()["parked"]), (("goto", web_service.PARK), {"accept": "blank"}))
        # The next close swaps it again; a list not shown in STUCK_SECONDS: reset and loaded again, once; then
        # to about:blank.
        self.run_job({"cmd": "accept_close"})
        tab = self.assert_parked("accept", tab)
        tab.shows = [False, False]
        self.clock.now += web_service.STUCK_SECONDS - 1
        self.service.settle()
        self.assertEqual(tab.did, [("goto", web_client.MESSENGER)])
        self.clock.now += 1
        with mock.patch.object(web_client, "reset", side_effect=lambda page: page.goto("about:blank")):
            self.service.settle()
        self.assertEqual(tab.did[1:], [("goto", "about:blank"), ("goto", web_client.MESSENGER)])
        self.assertEqual(self.service.ping()["parked"], {"accept": "loading"})
        self.clock.now += web_service.STUCK_SECONDS
        self.service.settle()
        self.assertEqual((tab.did[-1], self.service.ping()["parked"]), (("goto", web_service.PARK), {"accept": "blank"}))

    def test_an_open_finds_the_parked_list_there(self):
        with mock.patch.object(web_client, "header", return_value="验收测试群"):
            self.run_job({"cmd": "accept_text"})
        self.run_job({"cmd": "accept_close"})
        tab = self.service.pages["accept"]
        with mock.patch.object(web_client, "open_messenger", side_effect=RuntimeError("stop")) as messenger:
            self.run_job({"cmd": "accept_open", "title": "验收测试群"})
        messenger.assert_called_once_with(tab, reuse=True)
        # Opened while the parked tab still loads: it waits for both loads, then gets the tab.
        self.assertEqual(tab.did, [("goto", web_client.MESSENGER)] * 2)


class AcceptTest(ServiceCase):
    def test_input_and_clicks_are_refused_outside_the_allowlisted_chats(self):
        with mock.patch.object(web_client, "open_messenger") as messenger:
            [answer] = self.run_job({"cmd": "accept_open", "title": "张三"})
        self.assertTrue(answer["refused"])
        messenger.assert_not_called()  # Not even the chat list opened.
        for cmd in ("accept_post", "accept_click", "accept_fill"):
            with self.subTest(cmd=cmd), mock.patch.object(web_client, "header", return_value="张三"), \
                    mock.patch.object(web_client, "clear") as cleared:
                [answer] = self.run_job({"cmd": cmd, "text": "hi"})
                self.assertTrue(answer["refused"])
                self.assertIn("不在验收白名单", answer["error"])
                cleared.assert_not_called()
        self.assertEqual(self.accept_page.did, [])
        # In 验收测试群: posted with the @, then Enter.
        with mock.patch.object(web_client, "header", return_value="验收测试群"), mock.patch.object(web_client, "clear"):
            [answer] = self.run_job({"cmd": "accept_post", "text": "看一下", "at": "测试机器人"})
        self.assertEqual((answer["ok"], answer["title"]), (True, "验收测试群"))
        self.assertEqual(self.accept_page.did, [("type", "@"), ("type", "测试机器人"), ("press", "Enter"), ("insert", "看一下"),
                                                ("press", "Enter")])

    def test_a_click_finds_a_card_in_the_thread_panel_when_the_messages_have_none(self):
        # A group's launch card replies in a thread (T254, 2026-10-30): it is right of the messages, not in them.
        def element(x, y):
            return SimpleNamespace(bounding_box=lambda: {"x": x, "y": y, "width": 80, "height": 30},
                                   click=mock.Mock())
        main_card, older, newest = element(700, 600), element(1290, 300), element(1290, 605)
        matches = {"开始任务": [main_card, older, newest]}
        page = self.accept_page
        page.locator = lambda selector: (SimpleNamespace(count=lambda: 0) if selector == web_service.ROWS else
                                         SimpleNamespace(first=SimpleNamespace(bounding_box=lambda: {
                                             "x": 610, "y": 70, "width": 470, "height": 750})))
        page.get_by_text = lambda text, exact: SimpleNamespace(count=lambda: len(matches.get(text, [])),
                                                               nth=lambda i: matches[text][i])
        with mock.patch.object(web_client, "header", return_value="验收测试群"):
            [answer] = self.run_job({"cmd": "accept_click", "text": "开始任务"})
            self.assertTrue(answer["ok"])
            newest.click.assert_called_once()
            older.click.assert_not_called()
            main_card.click.assert_not_called()  # Left of the panel's edge: not in the thread.
            [answer] = self.run_job({"cmd": "accept_click", "text": "提交回答"})
        self.assertIn("没有“提交回答”", answer["error"])

    def test_a_scroll_turns_the_wheel_over_the_thread_panel_or_else_the_messages(self):
        page, wheel = self.accept_page, []
        page.mouse = SimpleNamespace(move=lambda x, y: wheel.append(("move", x, y)),
                                     wheel=lambda dx, dy: wheel.append(("wheel", dx, dy)))
        page.viewport_size = {"width": 1440, "height": 900}
        box = {"x": 610, "y": 70, "width": 470, "height": 750}
        page.locator = lambda selector: SimpleNamespace(first=SimpleNamespace(bounding_box=lambda: box))
        with mock.patch.object(web_client, "header", return_value="验收测试群"):
            [answer] = self.run_job({"cmd": "accept_scroll", "dy": -400})
            self.assertEqual((answer["ok"], answer["panel"]), (True, True))
            self.assertEqual(wheel, [("move", (1080 + 1440) / 2, 445), ("wheel", 0, -400)])
            box["width"], wheel[:] = 800, []  # The panel closed: the messages reach the right edge.
            [answer] = self.run_job({"cmd": "accept_scroll", "dy": 300})
            self.assertEqual((answer["panel"], wheel[0]), (False, ("move", 1010, 445)))
        with mock.patch.object(web_client, "header", return_value="张三"):
            [answer] = self.run_job({"cmd": "accept_scroll", "dy": 300})
        self.assertTrue(answer["refused"])

    def test_text_is_the_open_chats_messages_newest_last_and_close_parks_the_page(self):
        self.accept_page.messages = ["主人\n@测试机器人 看一下", "测试机器人\n开始任务 · 模型、思考预算、工作区、运行方式"]
        with mock.patch.object(web_client, "header", return_value="验收测试群"):
            [answer] = self.run_job({"cmd": "accept_text", "max": 30})
        self.assertEqual(answer["messages"], self.accept_page.messages)
        self.assertEqual(answer["text"], "\n\n".join(self.accept_page.messages)[-30:])
        self.assertTrue(answer["text"].endswith("运行方式"))
        self.assertIn(".messageList .list_items > *", web_service.MESSAGES)  # Not the filters' or the feed's list.
        self.run_job({"cmd": "accept_close"})
        self.assert_parked("accept", self.accept_page)

    def test_a_chat_is_opened_only_when_its_header_is_on_the_allowlist(self):
        def find_chat(page, name, check, ways):
            return check()
        with mock.patch.object(web_client, "open_messenger"), mock.patch.object(web_client, "openers", return_value=[]), \
             mock.patch.object(web_client, "find_chat", side_effect=find_chat), \
             mock.patch.object(web_client, "header", return_value="代答测试群"):
            [answer] = self.run_job({"cmd": "accept_open", "title": "代答测试群"})
        self.assertEqual(answer["title"], "代答测试群")
        self.assertEqual(sorted(answer["timing"]), ["chat_ms", "list_ms"])

    def test_with_no_acceptance_chats_set_every_chat_is_refused(self):
        """Nothing in COVEY_ACCEPT_CHATS: no chat is on the allowlist, and the refusal names the variable."""
        with mock.patch.dict(os.environ, {"COVEY_ACCEPT_CHATS": "", "LARK_BRIDGE_ACCEPT_CHATS": ""}):
            self.assertEqual(web_service.accept_chats(), ())
            service = web_service.Service(self.home, context(Page()), idle=1800, clock=self.clock)
        self.assertEqual(service.chats, ())
        with mock.patch.object(web_client, "open_messenger") as messenger:
            with self.assertRaises(web_service.Refused) as refused:
                service.accept_open({"title": "验收测试群"}, lambda *_: None)
        self.assertIn("COVEY_ACCEPT_CHATS", str(refused.exception))
        messenger.assert_not_called()
        with mock.patch.object(web_client, "header", return_value="验收测试群"):
            self.assertRaises(web_service.Refused, service.guard, None)
        with mock.patch.dict(os.environ, {"COVEY_ACCEPT_CHATS": "验收测试群, 代答测试群"}):
            self.assertEqual(web_service.accept_chats(), ("验收测试群", "代答测试群"))


class SocketTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)

    def test_commands_from_many_clients_run_one_at_a_time_and_ping_answers_meanwhile(self):
        page = Page()
        service = web_service.Service(self.home, context(page), clock=time.monotonic)
        service.running = None
        running, overlaps, release = [], [], threading.Event()

        def accept_text(request, say):
            text = request["text"]
            running.append(text)
            if len(running) > 1:
                overlaps.append(list(running))
            release.wait(5)
            running.remove(text)
            return {"ok": True, "text": text}
        service.handlers["accept_text"] = accept_text
        with mock.patch.object(service, "log"), web_service.listening(self.home) as server:
            self.assertEqual(oct((self.home / web_service.SOCKET).stat().st_mode & 0o777), "0o600")
            threading.Thread(target=service.accept, args=(server,), daemon=True).start()
            loop = threading.Thread(target=lambda: [service.step(0.05) for _ in range(100) if not service.stopping.is_set()],
                                    daemon=True)
            loop.start()
            results = {}

            def client(text):
                results[text] = web_service.call(self.home, {"cmd": "accept_text", "text": text}, timeout=10)
            clients = [threading.Thread(target=client, args=(t,)) for t in ("一", "二", "三")]
            for thread in clients:
                thread.start()
            deadline = time.monotonic() + 5
            while not running and time.monotonic() < deadline:
                time.sleep(0.01)
            busy = web_service.ping(self.home)
            self.assertEqual((busy["busy"], busy["version"]), ("accept_text", web_service.code_version()))
            release.set()
            for thread in clients:
                thread.join(10)
            self.assertEqual({t: r["text"] for t, r in results.items()}, {"一": "一", "二": "二", "三": "三"})
            self.assertEqual(overlaps, [])
            self.assertEqual(web_service.call(self.home, {"cmd": "stop"}), {"ok": True, "stopping": True,
                                                                            "ms": mock.ANY})
            loop.join(5)
        self.assertFalse((self.home / web_service.SOCKET).exists())

    def test_the_client_tells_not_asked_from_asked_and_no_last_line(self):
        with self.assertRaises(web_service.Unreachable):
            web_service.call(self.home, {"cmd": "ping"})
        with web_service.listening(self.home) as server:
            def died():
                conn, _ = server.accept()
                with conn, conn.makefile() as reader:
                    reader.readline()
                    conn.sendall(b'{"ok": false, "submitted": true}\n')  # Then gone, no last line.
            threading.Thread(target=died, daemon=True).start()
            lines = []
            self.assertEqual(web_service.call(self.home, {"cmd": "send"}, on_line=lines.append), {})
            self.assertEqual(lines, [{"ok": False, "submitted": True}])

    def test_a_long_socket_path_goes_through_its_folder(self):
        deep = self.home / ("d" * 60) / ("e" * 60)
        deep.mkdir(parents=True)
        with web_service.listening(deep) as server:
            def answer():
                conn, _ = server.accept()
                with conn, conn.makefile("rb") as request:
                    request.readline()  # Closed before the client wrote, its send broke the pipe.
                    conn.sendall(json.dumps({"done": True, "ok": 1}).encode() + b"\n")
            threading.Thread(target=answer, daemon=True).start()
            self.assertEqual(web_service.call(deep, {"cmd": "ping"}), {"ok": 1})

    def test_a_second_service_on_the_same_folder_finds_the_lock_held(self):
        first = web_service.locked(self.home, wait=0)
        self.addCleanup(first.close)
        self.assertIsNotNone(first)
        self.assertIsNone(web_service.locked(self.home, wait=0))


if __name__ == "__main__":
    unittest.main()
