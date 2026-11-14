"""The sensitive-term dashboard: a page on the intranet where the owner edits the redaction term list.

A Bridge worker (``Dashboard.run``) serves it only when COVEY_DASHBOARD names where to listen
(``[::]:10601``, ``0.0.0.0:10601`` or a bare port, meaning ``[::]``; ``$NAME`` is read from the environment,
so ``[::]:$ARNOLD_RESERVED_4_CURRENT_PORT`` follows the port the platform reserves). The page
(dashboard_static/) edits <state>/redaction/config.json through core/redaction.py, previews a text with
core/redactor.py, shows the counts the relays recorded (``recent_turns``) and asks
backends/claude/redaction.py where a Claude account's requests would go (探测, ``detect_upstream``).

Its parts: where it listens and the address links name, and the fixed entry (app/dashboard_entry.py); the
server, the requests and access with the token (app/dashboard_http.py, ``http``); what the API answers
(core/redaction_views.py). Here: the worker and the link.

The address moves with the dev machine's host. Once listening, the worker points the fixed entry
(COVEY_DASHBOARD_ENTRY) at it and, when it is not the address last given in the owner's private chat
(where the link was last shown), sends the new link there.

The list itself is on the 设置 card's 脱敏 tab since 2026-11-13 (app/redaction_panel.py), the owner's browser not
reaching the port; the page is optional, its link that tab's last part (``web_link``). 换口令 there (ROTATE_KIND)
rotates the token, old links and logged-in browsers stop working, and redraws that card with the new link
(SettingsHub.rotate). A link card sent before still acts; it has a record (<state>/LINK_FOLDER/<token>.json,
app/menu_panel.py), and its 换口令 sends a new one (``send_link``).
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from covey.core import env as cenv
from covey.app.menu_panel import MenuPanel
from covey.app.dashboard_entry import DASHBOARD_ENV, dashboard_address, parse_bind, read_record, update_record
from covey.core import extensions
from covey.app.dashboard_http import DashboardHttp, dashboard_token
from covey.cards.redaction_link import ROTATE_KIND, link_card
from covey.backends.claude.redaction import DEFAULT_UPSTREAM, checked_url, gateway_status, settings_upstream
from covey.core.serving import serve_thread

NOT_CONFIGURED = ("脱敏看板没有开启：在 Bridge 服务清单里设置 COVEY_DASHBOARD（例如 "
                  "[::]:$ARNOLD_RESERVED_4_CURRENT_PORT，用平台给的预留端口），重新 launch 后再点机器人菜单「设置」的「脱敏」。"
                  "见运维指南「看板」一节。")
ROTATED = "口令已换：之前的链接和已登录的浏览器都失效了，点“打开看板”用新口令登录。"  # The card's line after 换口令.
NOT_LISTENING = "注意：看板现在没有在监听（端口被占用或设置不对，见 bridge.log 的 dashboard 行）。"
PENDING_KEY_AGE = 3000  # An announcement cut short goes again under its key within Feishu's one-hour window.
LINK_FOLDER = "dashboard-links"  # The link cards' records (app/menu_panel.py).
MENU_KEY = "redaction_dashboard"  # The bot menu's 脱敏 before 2026-11-09: the 设置 card on its 脱敏 tab now.


def detect_upstream(harness):
    """Where a Claude account's requests would go with no 上游地址 set, and why. ``harness`` is the
    Bridge's claude Harness. Unavailable when the personal gateway's wrapper says it failed."""
    ctx = harness.context
    if ctx is None:
        raise ValueError("Claude Harness 还没有启动")
    env = harness.backend.child_env(ctx)
    if str(env.get("ANTHROPIC_BASE_URL") or "").strip():
        return {"upstream": checked_url(env["ANTHROPIC_BASE_URL"]), "source": "Bridge 环境里的 ANTHROPIC_BASE_URL"}
    try:
        (Path(ctx.state_dir) / "redaction" / "upstream.json").unlink()  # 探测 asks again.
    except OSError:
        pass
    endpoint = gateway_status(ctx, env)
    if endpoint:
        return {"upstream": endpoint, "source": "个人网关（claude --gateway-status）"}
    endpoint = settings_upstream(env)
    if endpoint:
        return {"upstream": endpoint, "source": "Claude Code 的 settings.json"}
    return {"upstream": DEFAULT_UPSTREAM, "source": "默认（Anthropic 官方地址）"}


class Dashboard:
    """The dashboard worker and its owner commands. ``detect`` () -> {upstream, source} is 探测 (``http``'s)."""

    def __init__(self, runtime, messenger=None, voice=None, *, detect=None):
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.menu = MenuPanel(self, "dashboard-command", LINK_FOLDER, "dashboard-link")
        self.http = DashboardHttp(runtime, detect)
        self.listening = False

    @staticmethod
    def configured():
        return bool(cenv.get(DASHBOARD_ENV, "").strip())

    def threads(self):
        """Its worker, only when configured (app/unified.py worker_targets)."""
        return (("dashboard", self.run),) if self.configured() else ()

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def rotate_token(ctx):
        """换口令 in the 设置 card's 脱敏 tab (app/redaction_panel.py, SettingsHub.rotate)."""
        dashboard_token(ctx.state_dir, rotate=True)

    # The worker.

    def start(self, state_dir, bind):
        """A server on ``bind`` serving in a daemon thread; OSError when it cannot listen."""
        server = self.http.server(state_dir, bind)
        serve_thread(server, "dashboard-http").start()
        return server

    def run(self, ctx):
        """Serve until the Bridge stops. A wrong setting or a busy port is logged once and waits for the
        stop: the supervise loop restarts a worker that returns or raises."""
        try:
            bind = parse_bind(cenv.get(DASHBOARD_ENV, ""))
        except ValueError as exc:
            self.runtime.log(f"dashboard not started: {exc}")
            bind = None
        if bind is None:
            self.runtime.shutdown.wait()
            return
        try:
            server = self.start(ctx.state_dir, bind)
        except OSError as exc:
            self.runtime.log(f"dashboard not started: cannot listen on {bind[0]} port {bind[1]} "
                             f"({type(exc).__name__}: {exc.strerror or exc})")
            self.runtime.shutdown.wait()
            return
        self.listening = True
        self.runtime.log(f"dashboard listening on {bind[0]} port {server.server_address[1]}")
        # Publishing may take minutes; a stop does not wait for it (the next start checks again).
        threading.Thread(target=self.settle, args=(ctx, (bind[0], server.server_address[1])),
                         name="dashboard-entry", daemon=True).start()
        try:
            self.runtime.shutdown.wait()
        finally:
            self.listening = False
            server.shutdown()
            server.server_close()

    def settle(self, ctx, bind):
        """Once listening: point the fixed entry here, then give the owner the link if the address moved."""
        try:
            address = dashboard_address(bind)
            self.point_entry(ctx.state_dir, address)
            self.announce(ctx, address)
        except Exception as exc:  # noqa: BLE001 - the dashboard serves on; the next start tries again.
            self.runtime.log(f"dashboard entry check failed ({type(exc).__name__})")

    def point_entry(self, state_dir, address):
        """Publish the fixed entry when it does not point at ``address`` (or is getting old)."""
        hook = extensions.hook("dashboard_entry")  # None: this deployment has no fixed entry.
        if hook is None:
            return
        try:
            alias = hook.entry_alias()
        except ValueError as exc:
            self.runtime.log(f"dashboard entry not published: {exc}")
            return
        if not alias:
            return
        entry = read_record(state_dir).get("entry")
        entry = entry if isinstance(entry, dict) else {}
        target = address + "/"
        try:
            fresh = time.time() - float(entry.get("at") or 0) < hook.REPUBLISH_AFTER
        except (TypeError, ValueError):
            fresh = False
        if entry.get("alias") == alias and entry.get("target") == target and fresh:
            return
        problem = hook.publish(state_dir, alias, target, owned=entry.get("alias") == alias)
        if problem:
            update_record(state_dir, entry_error=problem)
            self.runtime.log(f"dashboard entry not published: {problem}")
            return
        update_record(state_dir, entry={"alias": alias, "target": target, "at": time.time()}, entry_error=None)
        self.runtime.log(f"dashboard entry published at {hook.entry_url(alias)}")

    def announce(self, ctx, address):
        """Send the link to the owner's private chat when ``address`` is not the one last given there.
        Nothing before the owner has asked for the dashboard in a private chat."""
        record = read_record(ctx.state_dir)
        chat_id = str(record.get("chat_id") or "")
        if not chat_id or record.get("address") == address:
            return
        pending = record.get("announcing")
        pending = pending if isinstance(pending, dict) else {}
        try:
            recent = time.time() - float(pending.get("at") or 0) < PENDING_KEY_AGE
        except (TypeError, ValueError):
            recent = False
        if pending.get("address") == address and pending.get("key") and recent:
            key = str(pending["key"])
        else:
            key = self.voice.key("dashboard-address", secrets.token_hex(8))
            update_record(ctx.state_dir, announcing={"address": address, "key": key, "at": time.time()})
        lead = "脱敏看板换了地址（开发机重启后换了机器或端口），新链接：" if record.get("address") else "脱敏看板："
        text = self.link_text(ctx.state_dir, address, dashboard_token(ctx.state_dir), lead=lead)
        _, link, notes = self.link_lines(ctx.state_dir, address, dashboard_token(ctx.state_dir), lead=lead)
        card = link_card("看板换了地址（开发机重启后换了机器或端口），旧链接打不开了。" if record.get("address") else "",
                         link, notes)
        if self.messenger.send_message(ctx.profile, chat_id, text, key, voice=self.voice, card=card):
            update_record(ctx.state_dir, address=address, announcing=None)
            self.runtime.log("dashboard address announced in the owner's private chat")
        else:
            self.runtime.log("dashboard address not announced: the message was not sent")

    # The owner's menu item and button.

    @staticmethod
    def matches(value):
        return value.get("kind") == ROTATE_KIND

    def send_link(self, ctx, chat_id, source, *, rotate=False, shown=None):
        """The link (after a new token when ``rotate``), as a new message in ``chat_id``, the owner's private
        chat: the bot menu's 脱敏, or 换口令 on an earlier link's card. ``source`` keys the message and
        ``shown`` hears the card's message id; the text goes instead when the card does not."""
        text, card = self.link_reply(ctx, rotate=rotate, chat_id=chat_id) if self.configured() else (NOT_CONFIGURED, None)
        self.runtime.log(f"dashboard link sent rotate={rotate} event={source[-10:]}")
        if card is not None:
            self.menu.open(ctx, chat_id, source, lambda token, record: card, text, shown=shown)
            return
        self.messenger.send_message(ctx.profile, chat_id, text, self.voice.key("dashboard-command-text", source),
                                    voice=self.voice)

    def current(self, ctx, *, rotate=False, chat_id=""):
        """(address, token) of the link, after a new token when ``rotate``; ValueError, in words for the owner, when
        the setting or the token is wrong. ``chat_id`` (the owner's private chat) becomes where the link goes when the
        address moves, and this address the one given there."""
        try:
            bind = parse_bind(cenv.get(DASHBOARD_ENV, ""))
        except ValueError as exc:
            raise ValueError(f"脱敏看板的设置不对：{exc}") from None
        try:
            token = dashboard_token(ctx.state_dir, rotate=rotate)
        except OSError as exc:
            raise ValueError(f"脱敏看板的口令读写失败（{type(exc).__name__}）") from None
        address = dashboard_address(bind)
        if chat_id:
            try:
                update_record(ctx.state_dir, chat_id=chat_id, address=address)
            except OSError as exc:
                self.runtime.log(f"dashboard chat not recorded ({type(exc).__name__})")
        return address, token

    def link_reply(self, ctx, *, rotate, chat_id=""):
        """The link card's text and card (None for a problem, said in the text), as ``current`` gives it."""
        try:
            address, token = self.current(ctx, rotate=rotate, chat_id=chat_id)
        except ValueError as exc:
            return str(exc), None
        lead = "口令已换：之前的链接和已登录的浏览器都失效了。新链接：" if rotate else "脱敏看板："
        _, link, notes = self.link_lines(ctx.state_dir, address, token, lead=lead)
        if not self.listening:
            notes.append(NOT_LISTENING)
        card = link_card(ROTATED if rotate else "", link, notes, listening=self.listening)
        return "\n".join([lead + link, *notes]), card

    def web_link(self, ctx, chat_id=""):
        """The 设置 card's 脱敏 tab's last part (app/redaction_panel.py): {link, notes, listening}, or {error}; None when
        the deployment serves no web dashboard. ``chat_id`` as for ``current``."""
        if not self.configured():
            return None
        try:
            address, token = self.current(ctx, chat_id=chat_id)
        except ValueError as exc:
            return {"error": str(exc)}
        _, link, notes = self.link_lines(ctx.state_dir, address, token, lead="")
        return {"link": link, "notes": notes, "listening": self.listening}

    @staticmethod
    def link_lines(state_dir, address, token, *, lead):
        """(``lead``, the link, the lines under it): the warning that it carries the token, and the fixed
        entry's state."""
        lines = ["链接里带着口令，别转发；要换口令点“换口令”。"]
        hook = extensions.hook("dashboard_entry")
        try:
            alias = hook.entry_alias() if hook else ""
        except ValueError as exc:
            return lead, f"{address}/?t={token}", [*lines, f"固定入口没有开：{exc}"]
        if alias:
            record = read_record(state_dir)
            entry = record.get("entry") if isinstance(record.get("entry"), dict) else {}
            url = hook.entry_url(alias)
            if entry.get("alias") == alias and entry.get("target") == address + "/":
                lines.append(f"固定入口：{url}（可以存书签，它总跳到看板现在的地址；看板换了地址后，"
                             "浏览器要用私聊里的新链接重新登录一次）")
            elif record.get("entry_error"):
                lines.append(f"固定入口 {url} 还没指向这个地址：{record['entry_error']}")
            else:
                lines.append(f"固定入口 {url} 正在更新到这个地址，过几分钟再用。")
        return lead, f"{address}/?t={token}", lines

    @staticmethod
    def link_text(state_dir, address, token, *, lead):
        """``lead`` and the link, the warning that it carries the token, and the fixed entry's state."""
        lead, link, lines = Dashboard.link_lines(state_dir, address, token, lead=lead)
        return "\n".join([lead + link, *lines])
