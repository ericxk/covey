"""feishu/web_client.py without a browser: how the acceptance page gets the chat list logged in and finds a
chat by its title (the chat list, then search, past others of its name). Web is Feishu web in memory,
Playwright a stand-in module, the clock fake (no test waits). No lark-cli, no network. Its send, check,
status and login went 2026-10-29 (代答 sends with the sender tool)."""
import functools
import re
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.clock import FakeClock  # noqa: E402
from covey.feishu import web_client  # noqa: E402

FEED = "[data-feed-id]"
SEARCH_BOX = f"[contenteditable=true]:not({web_client.COMPOSER})"
RESULTS = ".user-result-card, .group-chat-card"
REPLY = " .toolbar-item.reply"
LOGIN = "https://accounts.feishu.cn/accounts/page/login"


class Clock(FakeClock):
    strftime = staticmethod(time.strftime)  # shot() names its file by it.


class Chat:
    """A chat as the page shows it once open: its header (a group's ends with its member count), the
    positions of its messages and their texts, a draft Feishu restores into the editor, ``blank``
    clicks that leave the pane blank first, whether a message has a reply button."""

    def __init__(self, header, positions=(), texts=None, *, draft="", blank=0, replyable=True):
        self.header, self.positions, self.texts = header, list(positions), dict(texts or {})
        self.draft, self.blank, self.replyable = draft, blank, replyable


class Web:
    """Feishu web in memory, for one page: the chat list ``feed`` and the search ``results`` as (title, Chat),
    the open chat, the search box and the editor (``shows``: what the editor makes of the text typed);
    ``skeleton`` loads of the chat list that stay a skeleton. What the code did is in ``did``; each wait
    moves the clock and runs the next of ``later``."""

    def __init__(self, clock, feed=(), results=(), *, logged_in=True, skeleton=0, shows=None):
        self.clock, self.feed, self.results = clock, list(feed), list(results)
        self.logged_in, self.skeleton, self.shows = logged_in, skeleton, shows or (lambda text: text)
        self.url, self.listed, self.chat, self.searching = "about:blank", False, None, False
        self.focus, self.query, self.composer, self.selected = None, "", "", False
        self.did, self.later, self.visible = [], [], ""
        self.keyboard = SimpleNamespace(press=self.press, insert_text=self.insert_text)

    def goto(self, url, wait_until=None, timeout=None):
        self.did.append(("goto", url))
        self.chat = None
        if url == "about:blank":
            self.url, self.listed = url, False
            return
        self.url = web_client.MESSENGER if self.logged_in else LOGIN
        self.listed = self.logged_in and self.skeleton <= 0
        self.skeleton -= 1

    def wait_for_timeout(self, ms):
        self.clock.advance(ms / 1000)
        if self.later:
            self.later.pop(0)()

    def evaluate(self, script, *args):
        if script == web_client.FEED_ITEMS:
            return [[f"feed-{i}", title] for i, (title, _) in enumerate(self.feed)] if self.listed else []
        if script == web_client.SEARCH_ROWS:
            return [[i, title] for i, (title, _) in enumerate(self.results)] if self.searching and self.query else []
        if script == web_client.UNREGISTER_WORKERS:
            self.did.append(("reset",))
            return None
        if script == web_client.VISIBLE_TEXT:
            return self.visible
        return True  # SCROLL_LIST: the list scrolls.

    def locator(self, selector):
        return Node(self, selector)

    def screenshot(self, path):
        Path(path).write_bytes(b"png")

    def open(self, chat):
        if chat.blank:
            chat.blank -= 1
            self.did.append(("blank",))
            self.chat = None
        else:
            self.chat, self.composer = chat, chat.draft

    def press(self, key):
        self.did.append(("press", key))
        if key == "Control+k":
            self.searching, self.focus, self.query = True, None, ""
        elif key == "Escape":
            self.searching = False
        elif key == "Control+a":
            self.selected = True
        elif key == "Backspace" and self.selected:
            self.selected = False
            if self.focus == "composer":
                self.composer = ""
        elif key == "Shift+Enter" and self.focus == "composer":
            self.composer += "\n"
        elif key == "Enter" and self.focus == "composer":
            self.did.append(("sent", self.composer))

    def insert_text(self, text):
        self.did.append(("type", text))
        if self.focus == "search":
            self.query += text
        elif self.focus == "composer":
            self.composer += text


class Node:
    """page.locator(selector), and its .first, .nth() and .locator(): what the code calls on it."""

    def __init__(self, page, selector, index=0):
        self.page, self.selector, self.index = page, selector, index
        self.first = self

    def nth(self, index):
        return Node(self.page, self.selector, index)

    def locator(self, selector):
        return Node(self.page, self.selector + " " + selector)

    def position(self):
        found = re.search(r'data-position="(-?\d+)"', self.selector)
        return int(found.group(1)) if found else None

    def count(self):
        page, chat = self.page, self.page.chat
        if self.selector == FEED:
            return len(page.feed) if page.listed else 0
        if self.selector == web_client.CHAT_NAME:
            return int(chat is not None)
        if self.selector == SEARCH_BOX:
            return int(page.searching)
        if self.selector.endswith(REPLY):
            return int(bool(chat and chat.replyable))
        if self.position() is not None:
            return int(bool(chat) and self.position() in chat.texts)
        return 1

    def click(self, timeout=None):
        page = self.page
        page.did.append(("click", f"reply {self.position()}" if self.selector.endswith(REPLY) else self.selector))
        if feed := re.fullmatch(r'\[data-feed-id="feed-(\d+)"\]', self.selector):
            page.open(page.feed[int(feed.group(1))][1])
        elif self.selector == RESULTS:
            page.searching = False
            page.open(page.results[self.index][1])
        elif self.selector == SEARCH_BOX:
            page.focus = "search"
        elif self.selector == web_client.COMPOSER:
            page.focus = "composer"

    def wait_for(self, state=None, timeout=None):
        if not self.count():
            raise TimeoutError(f"Locator.wait_for: Timeout {timeout}ms exceeded.")

    def inner_text(self, timeout=None):
        page = self.page
        if self.selector == web_client.CHAT_NAME:
            return page.chat.header
        if self.selector == web_client.COMPOSER:
            return page.shows(page.composer)
        return page.chat.texts[self.position()]

    def bounding_box(self):
        return {"x": 0, "y": 0}

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def hover(self):
        pass


def listing(*positions):
    """What listed_messages gives for a chat whose main list ends with ``positions``."""
    return [{"position": p, "type": "text", "recalled": False, "sender": "张三", "text": f"消息 {p}"} for p in positions]


@contextmanager
def playwright(page, launch=None):
    """``from playwright.sync_api import sync_playwright`` gives a Chromium whose context has ``page`` (its
    ``closed``, the handlers it got with ``on``); ``launch``: what starting it raises."""
    context = SimpleNamespace(pages=[page], closed=False, handlers={})
    context.close = lambda: setattr(context, "closed", True)
    context.on = lambda event, handler: context.handlers.setdefault(event, handler)
    page.on = lambda event, handler: context.handlers.setdefault(event, handler)

    def start(profile, **options):
        if launch:
            raise launch
        return context

    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=start))

        def __exit__(self, *exc):
            return False
    sync_api = SimpleNamespace(sync_playwright=Manager)
    with mock.patch.dict(sys.modules, {"playwright": SimpleNamespace(), "playwright.sync_api": sync_api}):
        yield context


class WebCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch.object(web_client, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def web(self, *feed, **options):
        return Web(self.clock, feed, **options)


class ChatListTest(WebCase):
    def test_a_page_already_on_the_chat_list_is_not_loaded_again_when_asked_to_reuse_it(self):
        page = self.web(("张三", Chat("张三")))
        web_client.open_messenger(page)
        page.did.clear()
        web_client.open_messenger(page, reuse=True)  # The browser service's parked tab: the list is there.
        self.assertEqual(page.did, [])
        web_client.open_messenger(page)  # Not asked to (the login check): loaded again.
        self.assertEqual(page.did, [("goto", web_client.MESSENGER)])
        blank = self.web(("张三", Chat("张三")))
        web_client.open_messenger(blank, reuse=True)  # Not on it: loaded.
        self.assertEqual(blank.did, [("goto", web_client.MESSENGER)])
        stuck = self.web(("张三", Chat("张三")))
        web_client.open_messenger(stuck)
        stuck.listed, stuck.did = False, []
        web_client.open_messenger(stuck, reuse=True)  # On it, the list never shown: reset and loaded once.
        self.assertEqual(stuck.did, [("reset",), ("goto", "about:blank"), ("goto", web_client.MESSENGER)])

    def test_the_chat_list_loads_logged_in_resets_a_skeleton_once_or_says_why_not(self):
        page = self.web(("张三", Chat("张三")))
        web_client.open_messenger(page)
        self.assertEqual(page.did, [("goto", web_client.MESSENGER)])
        page = self.web(("张三", Chat("张三")), skeleton=1)
        web_client.open_messenger(page)  # Stuck once: the service worker dropped, loaded from about:blank.
        self.assertEqual(page.did, [("goto", web_client.MESSENGER), ("reset",), ("goto", "about:blank"),
                                    ("goto", web_client.MESSENGER)])
        with self.assertRaisesRegex(web_client.Failure, "^网页端会话列表未加载$"):
            web_client.open_messenger(self.web(skeleton=2))
        with self.assertRaisesRegex(web_client.Failure, "^网页端未登录$"):
            web_client.open_messenger(self.web(logged_in=False))
        # Signed in but the device not authorized (a copied login, 2026-10-28): said at once, as logged out.
        device = self.web(skeleton=2)
        device.visible = "无法访问 根据企业安全策略，继续访问飞书网页版前，需先在客户端完成设备信息授权。 前往飞书客户端授权"
        before = self.clock.now
        with self.assertRaisesRegex(web_client.Failure, "^网页端未登录：飞书要求先在客户端授权这台设备$"):
            web_client.open_messenger(device)
        self.assertLess(self.clock.now - before, 5)  # Not the list's 90 s.

    def test_a_chat_is_opened_from_the_list_then_from_search_past_others_of_its_name(self):
        first, second, found, group = Chat("张三"), Chat("张三"), Chat("张三"), Chat("Covey 验收测试(3)")
        page = self.web(("李四", Chat("李四")), ("张三", first), ("张三", second),
                        results=[("张三", found), ("张三丰", Chat("张三丰")), ("Covey 验收测试(3)", group)])
        web_client.open_messenger(page)
        self.assertTrue(web_client.open_from_feed(page, "张三"))
        self.assertIs(page.chat, first)
        self.assertTrue(web_client.open_from_feed(page, "张三", skip=1))
        self.assertIs(page.chat, second)
        page.did.clear()
        self.assertFalse(web_client.open_from_feed(page, "张三", skip=2))
        self.assertEqual(page.did, [])  # Nothing clicked.
        # Search: the name goes into the search box, never the editor; a group's title ends with its count.
        self.assertTrue(web_client.open_from_search(page, "张三"))
        self.assertIs(page.chat, found)
        self.assertEqual(page.did[:4], [("press", "Escape"), ("press", "Control+k"), ("click", SEARCH_BOX),
                                        ("type", "张三")])
        self.assertEqual(page.composer, "")
        self.assertTrue(web_client.open_from_search(page, "Covey 验收测试"))
        self.assertIs(page.chat, group)
        # The results are in with fewer of that name: given up after 3 s, not at the 20 s deadline.
        start = self.clock.time()
        self.assertFalse(web_client.open_from_search(page, "张三", skip=1))
        self.assertLess(self.clock.time() - start, 6)
        self.assertEqual(page.did[-1], ("press", "Escape"))
        start, page.results = self.clock.time(), []
        self.assertFalse(web_client.open_from_search(page, "张三"))  # No result at all: the whole 20 s.
        self.assertGreaterEqual(self.clock.time() - start, 20)
        # Each chat of the list, then each search result, CANDIDATES of each.
        ways = web_client.openers(page, "张三")
        self.assertEqual([(way.func, way.args[2]) for way in ways],
                         [(web_client.open_from_feed, n) for n in range(web_client.CANDIDATES)]
                         + [(web_client.open_from_search, n) for n in range(web_client.CANDIDATES)])

    def test_the_open_chat_is_told_by_its_header_and_a_blank_pane_reset_once(self):
        def opened(chat, name="张三"):
            page = self.web((name, chat))
            web_client.open_messenger(page)
            return page, web_client.open_chat(page, name, functools.partial(web_client.open_from_feed, page, name))
        self.assertTrue(opened(Chat("张三"))[1])
        self.assertTrue(opened(Chat("Covey 验收测试(3)"), "Covey 验收测试")[1])
        page, found = opened(Chat("张三", blank=1))
        self.assertTrue(found)
        self.assertEqual(page.did[1:6], [("click", '[data-feed-id="feed-0"]'), ("blank",), ("reset",),
                                         ("goto", "about:blank"), ("goto", web_client.MESSENGER)])
        with self.assertRaisesRegex(web_client.Mismatch, "^打开的会话不是 张三（李四）$"):
            opened(Chat("李四"))
        with self.assertRaisesRegex(web_client.Mismatch, "^打开的会话不是 张三（会话没有打开）$"):
            opened(Chat("张三", blank=2))
        page = self.web()
        self.assertFalse(web_client.open_chat(page, "张三", lambda: False))
        self.assertEqual(page.did, [])


if __name__ == "__main__":
    unittest.main()
