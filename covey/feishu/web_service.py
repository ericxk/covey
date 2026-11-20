"""Feishu web's browser service, for the acceptance page alone: one long-lived process (``covey_feishu_web.py serve
--home DIR``, the Playwright interpreter) that alone opens the login DIR/profile (for acceptance a copy of the
owner's). 代答 does not send through it (app/proxy_send.py runs the sender tool), and it has no send, login or
status command.
Commands come one JSON line each over the Unix socket DIR/web.sock (0600, DIR 0700); the answer is a last line
with ``"done": true``.

    ping                        answered at once, also while a command runs: version, busy, timing
    stop                        the service ends after answering
    accept_open|post|text|click|fill|scroll|shot|close   the acceptance page (below)

Every command but ``ping`` runs on the main thread, one at a time, in the order they came (Playwright's sync API
is single-threaded). ``accept_close`` parks the page: swapped for a new tab on the chat list, which opens no chat
(an open chat marks its messages read for the owner; Feishu web reopens the chat last open in a tab, kept in its
sessionStorage, when that tab loads it again). The tab loads the list twice while the service waits (``settle``):
a click on a tab's first load mostly leaves the chat pane blank (10 of 13 clicks in a test), on its second it
opens in 0.5-2.6 s; ``accept_open`` while it loads waits for it (``ready``). A load that shows no list in
STUCK_SECONDS is reset once (web_client.reset) and loaded again; a parked tab that opens a chat by itself or
stays without a list goes to PARK (about:blank). Idle IDLE_SECONDS (no command done), the service ends.

The acceptance page is for tests run as the owner (T5's library): it opens a chat by title, posts (with an @ when
asked), reads what the chat shows, clicks a button by its text on the newest card (in a group, where the launch and
task cards reply in a thread, the lowest one in the open thread panel right of the messages), fills an input,
scrolls (the thread panel when one is open, else the messages: a tall card seen part by part), screenshots.
Opening, posting, clicking, filling and scrolling first check that the open chat's title is one of ``accept_chats()``
(COVEY_ACCEPT_CHATS, comma-separated chat titles), else refuse; with none set, every one of them is refused.

A second ``serve`` on the same DIR finds the lock DIR/service.lock held and ends. ``code_version`` is a hash of
the sources the service runs. Standard library and Playwright (through web_client) only.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from covey.core import env as cenv
from covey.feishu import web_client
from covey.core.file_lock import open_lock

SOCKET = "web.sock"
LOCK = "service.lock"
LOG = "service.log"
IDLE_SECONDS = 1800
PARK = "about:blank"  # Where a parked tab goes when it opens a chat by itself or its list does not show.
STUCK_SECONDS = 30  # A parked tab's chat list shows in 4-25 s a load; not in this long, it is stuck.
WARM_SECONDS = 60  # How long a command waits for a parked tab still loading.
PARKED = f"""() => [document.querySelectorAll('[data-feed-id]').length > 0,
  !!document.querySelector('{web_client.CHAT_NAME}')]"""  # [the chat list shown, a chat open]
ACCEPT_CHATS = ()  # The deployment names its acceptance chats in COVEY_ACCEPT_CHATS; none here.
MENTION_MS = 1500  # Acceptance @: how long the mention list may take to show the name typed.
LIST = ".messageList .list_items"  # The open chat's messages (the feed list and the filters are .list_items too).
ROWS = LIST + " > *"
MESSAGES = f"""() => Array.from(document.querySelectorAll('{ROWS}'))
  .map(row => (row.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean).join('\\n')).filter(Boolean)"""
NEWEST_ROWS = 15  # Acceptance click and fill: the newest card among this many last rows.
PANEL_MIN = 200  # Pixels right of the messages from which a thread panel is taken to be open (it is ~350 wide).
SCROLL_MS = 600  # After a scroll, for the list to load what came into view.
SOURCES = ("feishu/web_service.py", "feishu/web_client.py")
ENTRY = Path(__file__).resolve().parents[2] / "bin" / "covey_feishu_web.py"


class Unreachable(OSError):
    """No service took the request: nothing was asked of it."""


class Refused(Exception):
    """A command the service will not run (an acceptance chat off the allowlist, an unknown command)."""


@functools.lru_cache(maxsize=1)
def code_version():
    """A hash of the service's sources, the same in the Bridge and in a service started from the same code."""
    root, digest = Path(__file__).resolve().parents[1], hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()[:12]


def accept_chats():
    configured = [name.strip() for name in cenv.get("COVEY_ACCEPT_CHATS", "").split(",") if name.strip()]
    return tuple(configured or ACCEPT_CHATS)


@contextlib.contextmanager
def short(path):
    """``path`` as a socket address: AF_UNIX takes ~108 bytes, so a longer one goes through its folder's fd."""
    path = Path(path)
    if len(os.fsencode(str(path))) < 100:
        yield str(path)
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        yield f"/proc/self/fd/{fd}/{path.name}"
    finally:
        os.close(fd)


# The client ------------------------------------------------------------------------------------


def call(home, request, *, timeout=60, on_line=None):
    """``request`` to the service on ``home``; its last line (``done`` dropped), the lines before it to
    ``on_line``. Unreachable when it was not asked; another OSError (a timeout) once it was; {} when the
    connection ended without a last line (the service died on the way)."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(5)
        with short(Path(home) / SOCKET) as name:
            client.connect(name)
        client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
    except OSError as exc:
        client.close()
        raise Unreachable(f"{type(exc).__name__}: {exc}") from None
    with client, client.makefile("r", encoding="utf-8") as reader:
        client.settimeout(timeout)
        for line in reader:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.pop("done", False):
                return payload
            if on_line is not None:
                on_line(payload)
    return {}


def ping(home, timeout=5):
    return call(home, {"cmd": "ping"}, timeout=timeout)


def start(python, home, *, idle=IDLE_SECONDS, env=None):
    """``serve`` on ``home`` started in its own session (a Bridge restart leaves it running); its output to
    DIR/service.log. The Popen."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    with open(home / LOG, "ab") as log:
        return subprocess.Popen([python, "-B", str(ENTRY), "serve", "--home", str(home), "--idle", str(idle)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)


def accept(home, action, *, python=None, timeout=180, **fields):
    """An acceptance command (``accept_<action>``) to the service on ``home``; with ``python``, one is started
    when none answers. Its result."""
    request = {"cmd": "accept_" + action, **{k: v for k, v in fields.items() if v is not None}}
    try:
        return call(home, request, timeout=timeout)
    except Unreachable:
        if not python:
            raise
    start(python, home)
    deadline = time.time() + 60
    while True:
        try:
            ping(home)
            break
        except Unreachable:
            if time.time() > deadline:
                raise
            time.sleep(0.5)
    return call(home, request, timeout=timeout)


# The service -----------------------------------------------------------------------------------


class Job:
    def __init__(self, request, conn):
        self.request, self.conn, self.done = request, conn, threading.Event()

    def say(self, payload):
        """A line on the way. Raises when the client has gone: ``send`` then presses no Enter."""
        self.conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())

    def finish(self, result):
        try:
            self.say({**result, "done": True})
        except OSError:
            pass  # The client left; the service goes on.
        self.done.set()


class Service:
    """The commands on ``context`` (a Playwright persistent context), run by ``step`` on one thread."""

    def __init__(self, home, context, *, idle=IDLE_SECONDS, chats=None, clock=time.monotonic):
        self.home, self.context, self.idle = Path(home), context, idle
        self.chats = tuple(chats or accept_chats())
        self.clock = clock
        self.jobs = queue.Queue()
        self.running = "launch"  # The command under way, for ping; launching until the browser is up.
        self.used = clock()
        self.stopping = threading.Event()
        self.pages = {}
        self.parked = {}  # Name: {"at", "since": clock, "state": "loading" | "reloading" | "messenger" | "blank"}.
        self.timing = {}
        self.handlers = {"stop": self.stop, "accept_open": self.accept_open, "accept_post": self.accept_post,
                         "accept_text": self.accept_text, "accept_click": self.accept_click,
                         "accept_fill": self.accept_fill, "accept_scroll": self.accept_scroll,
                         "accept_shot": self.accept_shot, "accept_close": self.accept_close}

    def log(self, text):
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", file=sys.stderr, flush=True)

    # Connections (their own threads) -------------------------------------------------------

    def accept(self, server):
        while not self.stopping.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return  # Closed: the service ends.
            threading.Thread(target=self.answer, args=(conn,), daemon=True).start()

    def answer(self, conn):
        with conn:
            try:
                with conn.makefile("r", encoding="utf-8") as reader:
                    request = json.loads(reader.readline() or "null")
            except (OSError, ValueError):
                return
            if not isinstance(request, dict):
                return
            job = Job(request, conn)
            if request.get("cmd") == "ping":
                job.finish(self.ping())
                return
            self.jobs.put(job)
            job.done.wait()

    def ping(self):
        return {"ok": True, "version": code_version(), "pid": os.getpid(), "busy": self.running,
                "queued": self.jobs.qsize(), "idle": round(self.clock() - self.used), "timing": dict(self.timing),
                "parked": {name: park["state"] for name, park in self.parked.items()}}

    # The main thread -----------------------------------------------------------------------

    def step(self, wait=1.0):
        """Run the next command, or move the parked tabs on, or past ``idle`` stop."""
        try:
            job = self.jobs.get(timeout=wait)
        except queue.Empty:
            self.settle()
            if self.clock() - self.used >= self.idle:
                self.log(f"idle {self.idle}s: stopping")
                self.stopping.set()
            return
        self.run(job)

    def run(self, job):
        cmd = str(job.request.get("cmd") or "")
        self.running, started = cmd, time.time()
        try:
            handler = self.handlers.get(cmd)
            if handler is None:
                raise Refused(f"没有这个命令：{cmd}")
            result = handler(job.request, job.say)
        except Refused as exc:
            result = {"ok": False, "refused": True, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - said, and the service goes on
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
        finally:
            self.running, self.used = None, self.clock()
        result.setdefault("ms", round((time.time() - started) * 1000))
        self.log(f"{cmd} ok={bool(result.get('ok'))} ms={result['ms']}"
                 + (f" error={' '.join(str(result['error']).split())[:200]}" if result.get("error") else ""))
        job.finish(result)

    def page(self, name, *, keep=False):
        """The page ``name``, no longer parked unless ``keep`` (a command that only reads it)."""
        if not keep:
            self.parked.pop(name, None)
        page = self.pages.get(name)
        if page is None or (hasattr(page, "is_closed") and page.is_closed()):
            reuse = self.context.pages and self.context.pages[0] not in self.pages.values()
            page = self.pages[name] = self.context.pages[0] if reuse else self.context.new_page()
        return page

    def park(self, name):
        """The page swapped for a new tab loading the chat list (a new tab has no chat to reopen), the old one
        closed; a page that was never opened, or is parked and untouched since, stays so."""
        old = self.pages.get(name)
        if old is None or self.parked.get(name, {}).get("state") in ("loading", "reloading", "messenger"):
            return
        try:
            page = self.context.new_page()
        except Exception as exc:  # noqa: BLE001 - the old tab off any chat instead
            self.log(f"park {name} failed: {type(exc).__name__}: {str(exc)[:200]}")
            self.blank(name, old)
            return
        self.pages[name], self.parked[name] = page, {"at": self.clock(), "since": self.clock(), "state": "loading"}
        with contextlib.suppress(Exception):
            old.close()
        self.load(name, page)

    def load(self, name, page, *, reset=False):
        """The chat list asked for (the wait is ``settle``'s), after web_client.reset with ``reset``."""
        try:
            if reset:
                web_client.reset(page)
            page.goto(web_client.MESSENGER, wait_until="commit", timeout=30000)
        except Exception as exc:  # noqa: BLE001 - the next command opens the chat list again anyway
            self.log(f"park {name} failed: {type(exc).__name__}: {str(exc)[:200]}")
            self.blank(name, page)

    def blank(self, name, page):
        self.parked[name] = {"at": self.clock(), "since": self.clock(), "state": "blank"}
        try:
            page.goto(PARK)
        except Exception as exc:  # noqa: BLE001 - the next command opens the chat list again anyway
            self.log(f"park {name} failed: {type(exc).__name__}: {str(exc)[:200]}")

    def settle(self):
        """Each parked tab a step on: its chat list shown once, then loaded again and shown, no chat open; else
        to PARK."""
        for name, park in list(self.parked.items()):
            page = self.pages.get(name)
            if park["state"] == "blank" or page is None:
                continue
            try:
                listed, chat = page.evaluate(PARKED)
            except Exception:  # noqa: BLE001 - a page between loads
                listed, chat = False, False
            if chat:
                self.log(f"parked {name} page opened a chat by itself: to {PARK}")
                self.blank(name, page)
            elif listed and park["state"] == "loading":
                park.update(state="reloading", since=self.clock())
                self.load(name, page)
            elif listed:
                if park["state"] == "reloading":
                    self.timing[f"park_{name}_ms"] = round((self.clock() - park["at"]) * 1000)
                park["state"] = "messenger"
            elif park["state"] == "messenger" or (park.get("reset") and self.clock() - park["since"] >= STUCK_SECONDS):
                self.log(f"parked {name} page shows no chat list: to {PARK}")
                self.blank(name, page)
            elif self.clock() - park["since"] >= STUCK_SECONDS:
                self.log(f"parked {name} page shows no chat list in {STUCK_SECONDS}s: reset and loaded again")
                park.update(state="loading", since=self.clock(), reset=True)
                self.load(name, page, reset=True)

    def ready(self, name, wait=0.2):
        """A parked tab still loading, waited for (up to WARM_SECONDS) so that the command gets it warm."""
        deadline = time.monotonic() + WARM_SECONDS
        while self.parked.get(name, {}).get("state") in ("loading", "reloading") and time.monotonic() < deadline:
            self.settle()
            time.sleep(wait)

    def stop(self, request, say):
        self.stopping.set()
        return {"ok": True, "stopping": True}

    # The acceptance page -------------------------------------------------------------------

    def guard(self, page):
        """The open chat's title when it is on the allowlist; else Refused."""
        title = web_client.header(page)
        if title not in self.chats:
            raise self.outside(f"打开的会话「{title or '没有打开会话'}」", "操作")
        return title

    def outside(self, what, verb):
        """The Refused for a chat off the allowlist, or for any chat while COVEY_ACCEPT_CHATS names none."""
        if not self.chats:
            return Refused(f"没有设置验收白名单（COVEY_ACCEPT_CHATS），{what}不{verb}")
        return Refused(f"{what}不在验收白名单（{'、'.join(self.chats)}）里，不{verb}")

    def accept_open(self, request, say):
        title = str(request.get("title") or "")
        if title not in self.chats:
            raise self.outside(f"「{title}」", "打开")
        started = time.time()
        self.ready("accept")
        page = self.page("accept")
        web_client.open_messenger(page, reuse=True)
        listed = time.time()
        title = web_client.find_chat(page, title, lambda: self.guard(page), web_client.openers(page, title))
        return {"ok": True, "title": title, "timing": {"list_ms": round((listed - started) * 1000),
                                                      "chat_ms": round((time.time() - listed) * 1000)}}

    def accept_post(self, request, say):
        page = self.page("accept")
        title = self.guard(page)
        text = str(request.get("text") or "")
        web_client.clear(page)
        if request.get("at"):
            page.keyboard.type("@")
            page.keyboard.type(str(request["at"]), delay=50)
            page.wait_for_timeout(MENTION_MS)
            page.keyboard.press("Enter")  # The mention list's first: the name typed.
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.insert_text(line)
        self.guard(page)
        page.keyboard.press("Enter")
        return {"ok": True, "title": title}

    def accept_text(self, request, say):
        """The open chat's messages shown, oldest first, each with its cards' text; ``text`` their end, joined."""
        page = self.page("accept", keep=True)
        messages = page.evaluate(MESSAGES)
        return {"ok": True, "title": web_client.header(page), "messages": messages,
                "text": "\n\n".join(messages)[-int(request.get("max") or 4000):]}

    def newest(self, page, find):
        """In the newest of the last NEWEST_ROWS rows that has one, the locator ``find(row)`` gives."""
        rows = page.locator(ROWS)
        count = rows.count()
        for index in range(count - 1, max(count - NEWEST_ROWS, 0) - 1, -1):
            found = find(rows.nth(index))
            if found.count():
                return found.first
        return None

    def in_thread_panel(self, page, text):
        """The lowest element showing exactly ``text`` right of the chat's messages: in the thread (话题) panel a
        group's cards reply in; None when the panel is closed or has none."""
        box = page.locator(".messageList").first.bounding_box()
        if box is None:
            return None
        found, lowest = page.get_by_text(text, exact=True), None
        for index in range(found.count()):
            item = found.nth(index)
            place = item.bounding_box()
            if place and place["x"] >= box["x"] + box["width"] and (lowest is None or place["y"] >= lowest[0]):
                lowest = (place["y"], item)
        return lowest[1] if lowest else None

    def in_thread_panel_field(self, page, selector):
        """The lowest input matching ``selector`` right of the chat's messages (a form in the thread panel)."""
        box = page.locator(".messageList").first.bounding_box()
        if box is None:
            return None
        found, lowest = page.locator(selector), None
        for index in range(found.count()):
            item = found.nth(index)
            place = item.bounding_box()
            if place and place["x"] >= box["x"] + box["width"] and (lowest is None or place["y"] >= lowest[0]):
                lowest = (place["y"], item)
        return lowest[1] if lowest else None

    def accept_click(self, request, say):
        page = self.page("accept")
        title = self.guard(page)
        text = str(request.get("text") or "")
        button = (self.newest(page, lambda row: row.get_by_text(text, exact=True))
                  or self.in_thread_panel(page, text))
        if button is None:
            raise web_client.Failure(f"最新的卡片上没有“{text}”")
        button.click(timeout=15000)
        return {"ok": True, "title": title}

    def accept_fill(self, request, say):
        page = self.page("accept")
        title = self.guard(page)
        selector = f'[placeholder="{request["placeholder"]}"]' if request.get("placeholder") else "input, textarea"
        field = self.newest(page, lambda row: row.locator(selector)) or self.in_thread_panel_field(page, selector)
        if field is None:
            raise web_client.Failure("最新的卡片上没有输入框")
        field.fill(str(request.get("text") or ""), timeout=15000)
        return {"ok": True, "title": title}

    def accept_scroll(self, request, say):
        """The wheel turned ``dy`` pixels (negative: up) over the open thread panel, or the messages when none is open."""
        page = self.page("accept")
        title = self.guard(page)
        box = page.locator(".messageList").first.bounding_box()
        if box is None:
            raise web_client.Failure("没有打开的会话")
        right, width = box["x"] + box["width"], (page.viewport_size or {}).get("width") or 0
        x = (right + width) / 2 if width - right >= PANEL_MIN else box["x"] + box["width"] / 2
        page.mouse.move(x, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, int(request.get("dy") or 0))
        page.wait_for_timeout(SCROLL_MS)
        return {"ok": True, "title": title, "panel": width - right >= PANEL_MIN}

    def accept_shot(self, request, say):
        page = self.page("accept", keep=True)
        path = Path(request.get("out") or self.home / "shots" / f"accept-{time.strftime('%Y%m%d-%H%M%S')}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=bool(request.get("full")))
        return {"ok": True, "path": str(path), "title": web_client.header(page)}

    def accept_close(self, request, say):
        self.park("accept")
        return {"ok": True}


@contextlib.contextmanager
def listening(home):
    path = Path(home) / SOCKET
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with short(path) as name:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)  # Left by a service that died; the lock says none runs.
        mask = os.umask(0o177)
        try:
            server.bind(name)
        finally:
            os.umask(mask)
        os.chmod(name, 0o600)
    server.listen(8)
    try:
        yield server
    finally:
        server.close()
        path.unlink(missing_ok=True)


def locked(home, wait=20):
    """The single-instance lock on ``home`` (its open file), None when another service keeps it."""
    lock = os.fdopen(open_lock(Path(home) / LOCK), "a+")
    deadline = time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if time.time() > deadline:
                    lock.close()
                    return None
                time.sleep(0.5)  # One that was told to stop may still be closing its browser.
    except BaseException:
        lock.close()
        raise


def serve(home, *, idle=IDLE_SECONDS):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    lock = locked(home)
    if lock is None:
        print(f"another service holds {home / LOCK}", file=sys.stderr, flush=True)
        return 1
    with lock, listening(home) as server:
        service = Service(home, None, idle=idle)
        threading.Thread(target=service.accept, args=(server,), name="accept", daemon=True).start()
        service.log(f"serving pid={os.getpid()} version={code_version()} idle={idle}s")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            started = time.time()
            service.context = web_client.launch(playwright, home)
            service.timing["launch_ms"] = round((time.time() - started) * 1000)
            service.running = None
            try:
                while not service.stopping.is_set():
                    service.step()
            finally:
                server.close()
                service.context.close()
    return 0
