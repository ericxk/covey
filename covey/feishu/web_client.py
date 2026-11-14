"""Feishu web, driven by a headless Chromium logged in as the owner, for the acceptance page of the browser
service (feishu/web_service.py): what opens the chat list and a chat by its title. 代答 no longer sends
through it (2026-10-28: app/proxy_send.py runs the sender tool; its send, check, status and login here went
2026-10-29, repo review 2). Started by path as bin/covey_feishu_web.py under an interpreter that has Playwright:

    serve --home DIR [--idle S]                     the browser service on DIR (feishu/web_service.py)
    accept ACTION --home DIR [--title …] [--text …] [--at NAME] [--placeholder P] [--out PNG] [--start PYTHON]
                                                    an acceptance command to that service (open, post, text,
                                                    click, fill, shot, close), its result on stdout

The profile (the owner's login, copied for acceptance) is DIR/profile, mode 0700. Feishu web opens no chat by
its chat_id (2026-10-27: a message's data-id is an internal number, a chat's data-feed-id too), so a chat is
found by name in the chat list, else through search (``find_chat``, CANDIDATES chats of one name at most).
Standard library and Playwright only.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from covey.core import env as cenv
from covey.core import browser_env, extensions

HOST_ENV = "COVEY_FEISHU_WEB_HOST"  # The tenant's host name; else an extension's, else open.feishu.cn (it redirects after login).
HOST = cenv.get(HOST_ENV) or extensions.setting("web_tenant_domain", "open.feishu.cn")
BASE = "https://" + HOST
TENANT_SUFFIXES = (".feishu.cn", ".larkoffice.com", ".larksuite.com")  # privacy: allow 飞书公开的租户域名，不是内部名
MESSENGER = BASE + "/messenger/"  # Redirects to /next/messenger/, which opens chats more reliably.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36")
COMPOSER = ".chatEditorContainer [contenteditable=true]"
CHAT_NAME = ".chatWindow_chatName"

FEED_ITEMS = """() => Array.from(document.querySelectorAll('[data-feed-id]')).map(item => {
  const lines = (item.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
  return [item.getAttribute('data-feed-id'), lines.find(s => !/^\\d+$/.test(s)) || ''];
})"""
SEARCH_ROWS = """() => Array.from(document.querySelectorAll('.user-result-card, .group-chat-card')).map((row, i) => {
  const title = row.querySelector('.user-chatter-info-name, .chat-text-title');
  return [i, (title ? title.innerText : '').trim()];
})"""


class Failure(Exception):
    """A step that failed, as the result says it; ``seen``: what the chats compared showed last, for the
    batch record (``same_chat``), else None."""

    def __init__(self, message="", seen=None):
        super().__init__(message)
        self.seen = seen


def bare_title(text):
    """A search row's or header's chat name: a group row ends with its member count, '(12)'."""
    text = " ".join(str(text or "").split())
    if text.endswith(")") and "(" in text:
        head, _, count = text.rpartition("(")
        if count[:-1].isdigit():
            return head.strip()
    return text


def launch(playwright, home):
    home = Path(home)
    profile = home / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    for path in (home, profile):
        os.chmod(path, 0o700)
    executable = cenv.get(browser_env.EXECUTABLE, "")  # A Chrome or Chromium the machine has (./install found it).
    return playwright.chromium.launch_persistent_context(
        str(profile), headless=True, **({"executable_path": executable} if executable else {}), viewport={"width": 1440, "height": 900}, locale="zh-CN",
        timezone_id="Asia/Shanghai", user_agent=USER_AGENT, args=["--disable-blink-features=AutomationControlled"])


def logged_out(url):
    return "accounts." in url or "/login" in url or "/accounts/" in url


VISIBLE_TEXT = ("Array.from(document.querySelectorAll('body *'))"
                ".filter(e => e.children.length === 0 && e.offsetParent !== null)"
                ".map(e => (e.innerText || '').trim()).filter(Boolean).join(' ').slice(0, 800)")
DEVICE_BLOCK = "设备信息授权"
UNAUTHORIZED = "网页端未登录：飞书要求先在客户端授权这台设备"


def blocked(page):
    """Whether Feishu web shows its page that asks for this device to be authorized in the desktop client
    instead of the chat list (2026-10-28: a copied login, signed in all the same; the list never loaded)."""
    try:
        return DEVICE_BLOCK in page.evaluate(VISIBLE_TEXT)
    except Exception:  # noqa: BLE001 - a page between loads
        return False


UNREGISTER_WORKERS = """async () => {
  const workers = await navigator.serviceWorker.getRegistrations();
  for (const worker of workers) await worker.unregister();
}"""


def reset(page):
    """What unsticks the web client when a clicked chat stays blank or the list stays a skeleton:
    drop Feishu's service worker (it registers itself again), then load from about:blank."""
    try:
        page.evaluate(UNREGISTER_WORKERS)
    except Exception:  # noqa: BLE001 - a page without service workers
        pass
    page.goto("about:blank")


def on_messenger(url):
    parts = urlparse(url)
    host = parts.hostname or ""
    return "/messenger" in parts.path and (host == HOST or host.endswith(TENANT_SUFFIXES))


def open_messenger(page, budget=90, reuse=False):
    """The chat list, logged in. With ``reuse``, a page already there (the browser service's parked tab, loaded
    or still loading) is not loaded again: a load takes 10-20 s. A list stuck on its skeleton is reset once."""
    for attempt in range(2):
        if attempt:
            reset(page)
        if attempt or not (reuse and on_messenger(page.url)):
            page.goto(MESSENGER, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + budget / 2
        while time.time() < deadline:
            if logged_out(page.url):
                raise Failure("网页端未登录")
            if page.locator("[data-feed-id]").count():
                return
            if blocked(page):
                raise Failure(UNAUTHORIZED)
            page.wait_for_timeout(1000)
    raise Failure("网页端会话列表未加载")


def header(page):
    node = page.locator(CHAT_NAME).first
    return bare_title(node.inner_text(timeout=5000)) if node.count() else ""


class Mismatch(Failure):
    """The chat opened is not the one: another chat may have the same name."""


CANDIDATES = 4  # Chats of one name tried, at most, before the send gives up.


def open_from_feed(page, name, skip=0):
    """Click the chat list's chat titled ``name`` after ``skip`` others of that name; False when none."""
    found = [feed_id for feed_id, title in page.evaluate(FEED_ITEMS) if bare_title(title) == name]
    if len(found) <= skip:
        return False
    page.locator(f'[data-feed-id="{found[skip]}"]').first.click(timeout=15000)
    return True


def open_from_search(page, name, skip=0):
    """Search for ``name`` and click the result of that name after ``skip`` others; False when none."""
    page.keyboard.press("Escape")
    page.keyboard.press("Control+k")
    box = page.locator(f"[contenteditable=true]:not({COMPOSER})").first
    box.wait_for(state="visible", timeout=20000)
    box.click()
    page.keyboard.insert_text(name)
    deadline, shown = time.time() + 20, None
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        rows = [i for i, title in page.evaluate(SEARCH_ROWS) if bare_title(title) == name]
        if len(rows) > skip:
            page.locator(".user-result-card, .group-chat-card").nth(rows[skip]).click(timeout=15000)
            return True
        shown = shown or (time.time() if rows else None)
        if shown and time.time() - shown > 3:
            break  # The results are in, with fewer of that name.
    page.keyboard.press("Escape")
    return False


def openers(page, name):
    """The ways to open a chat titled ``name``, in order: each one in the chat list, then each search
    result (it may be one of those again, or the one the list did not show)."""
    return ([functools.partial(open_from_feed, page, name, skip) for skip in range(CANDIDATES)]
            + [functools.partial(open_from_search, page, name, skip) for skip in range(CANDIDATES)])


def open_chat(page, name, opener):
    """Open a chat titled ``name`` with ``opener``; False when it has none to open. A click that leaves
    the chat pane blank resets the page once."""
    for attempt in range(2):
        if attempt:
            reset(page)
            open_messenger(page)
        if not opener():
            return False
        deadline = time.time() + 15
        while time.time() < deadline:
            if header(page) == name:
                return True
            page.wait_for_timeout(500)
        if header(page):
            break
    raise Mismatch(f"打开的会话不是 {name}（{header(page) or '会话没有打开'}）")


def find_chat(page, name, check, ways, opened=open_chat):
    """The result of ``check()`` in the first chat titled ``name`` it passes in, trying the ``ways`` one
    by one (CANDIDATES chats at most): a same-named chat that fails it is not the end."""
    errors, seen = [], []
    for way in ways:
        if len(errors) >= CANDIDATES:
            break
        try:
            if not opened(page, name, way):
                continue
            return check()
        except Mismatch as exc:
            errors.append(str(exc))
            seen += [exc.seen] if exc.seen else []
    if not errors:
        # The 重新发送 card says it as it is (core/proxy.py unsent): "没发出：网页端找不到「…」会话（试了 3 次）…".
        raise Failure(f"网页端找不到「{name}」会话")
    raise Failure(errors[0] if len(errors) == 1 else f"{len(errors)} 个名为 {name} 的会话都对不上：" + "；".join(errors),
                  seen or None)


def clear(page):
    """Empty the composer once it shows. Feishu restores a chat's draft a moment after it opens: by now
    ``check`` has read the chat for a second or more (a fixed 1.5 s wait before), and ``compose`` types
    again over a draft that comes back all the same."""
    box = page.locator(COMPOSER).first
    box.wait_for(state="visible", timeout=15000)
    box.click()
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_feishu_web.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_args = sub.add_parser("serve")
    serve_args.add_argument("--home", required=True, help="state dir; the login is DIR/profile, the socket DIR/web.sock")
    serve_args.add_argument("--idle", type=float, default=None, help="seconds without a command before it ends")
    accept_args = sub.add_parser("accept")
    accept_args.add_argument("action", choices=("open", "post", "text", "click", "fill", "scroll", "shot", "close"))
    accept_args.add_argument("--home", required=True)
    for flag in ("--title", "--text", "--at", "--placeholder", "--out"):
        accept_args.add_argument(flag)
    accept_args.add_argument("--max", type=int)
    accept_args.add_argument("--dy", type=int, help="scroll: pixels down (negative: up)")
    accept_args.add_argument("--start", metavar="PYTHON", help="start the service with this interpreter if none answers")
    args = parser.parse_args(argv)
    browser_env.apply()
    from covey.feishu import web_service

    if args.cmd == "serve":
        return web_service.serve(args.home, **({"idle": args.idle} if args.idle else {}))
    try:
        outcome = web_service.accept(args.home, args.action, python=args.start, title=args.title, text=args.text,
                                     at=args.at, placeholder=args.placeholder, out=args.out, max=args.max,
                                     dy=args.dy)
    except OSError as exc:
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    emit(outcome)
    return 0 if outcome.get("ok") else 1

if __name__ == "__main__":
    sys.exit(main())
