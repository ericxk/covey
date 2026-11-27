"""网页登录确认 (docs/web.md, COVEY_WEB=1 with a self-hosted page): the page's ``login`` operation becomes a card in the owner's private
chat (cards/web_login.py), and the owner's click on it is told to the page.

- ``send`` (app/web_ops.py ``login``): the operation's payload ``{request_id, code, browser, os, ip, at}`` (``at`` in seconds; ``ip`` where
  the request came from, which an older page leaves out) is written down
  first (<state>/web/logins.json, by request id), then the 网页登录确认 card goes out. The same request id again sends nothing (the card is
  already there; one Feishu refused is sent again). A request older than ``VALID_SECONDS`` (its ``at``) gets no card.
- ``claim`` (app/unified.py PANELS): 允许 / 不是我, the owner's only (``owner`` as app/access.py decides it). A click is taken once per
  event; what goes over the network runs after the click is acknowledged (app/card_lanes.py ``after_ack``: Feishu wants the answer
  within 3 seconds): ``POST /sync/upsert`` with one ``login`` item ``{decision: approved | denied, decided_at}``, no record lock held
  (AGENTS.md rule 1). When the page took it (``upserted``) the card is replaced whole by the result (已允许 · time, 已拒绝); when the page
  answered but took nothing (the request is over there: expired, decided, or a page without self-hosted login) the card says the
  login is void; when the request failed, the card says why and keeps its buttons, so it can be clicked again. A request past
  ``VALID_SECONDS`` is shown as expired and sends nothing; once decided, a later click (either button) only redraws the result: the
  page is told once.

Records: <state>/web/logins.json {request_id: {view, message_id, state (sending, failed, pending, approved, denied, void, expired),
decided_at}}, the newest ``KEPT``.
"""
from __future__ import annotations

import functools
import re
import threading
import time
from pathlib import Path

from covey.app.card_lanes import after_ack
from covey.cards import web_login as cards
from covey.core import web_sync
from covey.core.state import claim_once, read_object, write_json

FOLDER = web_sync.FOLDER
RECORD = "logins.json"
INBOUND = "inbound-messages"
KEPT = 100
REQUEST_ID = re.compile(r"[0-9A-Za-z_-]{6,64}")
DECIDED = ("approved", "denied", "void")  # The page was told (or answered that it takes nothing more): a click only redraws.


class WebLogin:
    """The card and its clicks. ``request(method, path, body)``: the page's /openapi (app/web_push_client.py Client.request), looked
    up at each call; ``clock``: the time in seconds (a test's)."""

    def __init__(self, runtime, messenger, voice, request, clock=time.time) -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.request, self.clock = request, clock
        self.lock = threading.Lock()  # The record's read-modify-write; never held across a request or a send.
        self.busy = set()  # Request ids whose decision is on its way to the page.

    # Records -------------------------------------------------------------------------------------

    @staticmethod
    def path(state_dir) -> Path:
        return Path(state_dir) / FOLDER / RECORD

    def get(self, state_dir, request_id) -> dict | None:
        with self.lock:
            row = read_object(self.path(state_dir)).get(request_id)
            return dict(row) if isinstance(row, dict) else None

    def put(self, state_dir, request_id, **fields) -> None:
        with self.lock:
            path = self.path(state_dir)
            rows = read_object(path)
            rows[request_id] = {**(rows.get(request_id) if isinstance(rows.get(request_id), dict) else {}), **fields,
                                "touched": self.clock()}
            if len(rows) > KEPT:
                for name in sorted(rows, key=lambda each: rows[each].get("touched") or 0)[:len(rows) - KEPT]:
                    rows.pop(name, None)
            write_json(path, rows, ensure_ascii=False)

    # The page's operation ------------------------------------------------------------------------

    def send(self, state_dir, chat, profile, payload) -> str:
        """The card for one ``login`` operation; ValueError says why there is none."""
        request_id = str(payload.get("request_id") or "")
        at = payload.get("at")
        if not REQUEST_ID.fullmatch(request_id) or isinstance(at, bool) or not isinstance(at, (int, float)):
            raise ValueError("登录请求的内容不对：要有 request_id 和 at")
        # A clock ahead of this one does not make a request live longer than five minutes from now.
        view = {"request_id": request_id, "code": cards.clean(payload.get("code"), 8), "browser": cards.clean(payload.get("browser")),
                "os": cards.clean(payload.get("os")), "ip": cards.clean(payload.get("ip"), 64) if payload.get("ip") else "",
                "at": min(float(at), self.clock())}
        if (self.get(state_dir, request_id) or {}).get("state") not in (None, "failed"):
            return "确认卡已经发过了，没有重复发"
        if self.clock() - view["at"] > cards.VALID_SECONDS:
            raise ValueError("这次登录请求已超过 5 分钟，没有发确认卡；请在网页上重新点“用飞书登录”")
        self.put(state_dir, request_id, view=view, state="sending", message_id="")  # Written before the card goes (rule 1, 6).
        sent = []
        key = self.voice.safe_key(f"web-login-{request_id}")
        if not self.messenger.send_message(profile, chat, cards.text(view), key, voice=self.voice, card=cards.pending_card(view),
                                           on_card=sent.append) or not sent:
            self.put(state_dir, request_id, state="failed")
            raise ValueError("确认卡没有发出去（飞书拒绝了卡片），请在网页上重试")
        self.put(state_dir, request_id, state="pending", message_id=str(sent[0]))
        self.runtime.log(f"web login card sent request={request_id[:8]}")
        return f"已在飞书私聊发出网页登录确认卡（对照码 {view['code']}）"

    # The clicks ----------------------------------------------------------------------------------

    @staticmethod
    def matches(raw) -> bool:
        return isinstance(raw, dict) and raw.get("kind") in cards.KINDS

    def claim(self, ctx, event, raw, owner):
        """A 网页登录确认 card's click (app/unified.py PANELS), else None."""
        if not self.matches(raw):
            return None
        if not owner:
            return "owner_only"
        event_id = str(event.get("event_id") or "")
        if not event_id or not claim_once(Path(ctx.state_dir) / INBOUND, "web-login-" + event_id):
            return "duplicate"
        request_id, message_id = str(raw.get("request_id") or ""), str(event.get("message_id") or "")
        row = self.get(ctx.state_dir, request_id) if REQUEST_ID.fullmatch(request_id) else None
        if not row or not message_id or row.get("message_id") != message_id:
            return "web_login_stale"
        view, approved = row["view"], raw["kind"] == cards.ALLOW
        if row.get("state") in DECIDED:  # The page is not told twice.
            after_ack(functools.partial(self.messenger.patch_card, ctx.profile, message_id, self.decided_card(row)))
            return "web_login_done"
        if row.get("state") == "expired" or self.clock() - view["at"] > cards.VALID_SECONDS:
            self.put(ctx.state_dir, request_id, state="expired")
            after_ack(functools.partial(self.messenger.patch_card, ctx.profile, message_id, cards.expired_card(view)))
            return "web_login_expired"
        with self.lock:
            if request_id in self.busy:
                return "web_login_busy"
            self.busy.add(request_id)
        after_ack(functools.partial(self.decide, ctx, message_id, request_id, view, approved))
        return "web_login_allow" if approved else "web_login_deny"

    @staticmethod
    def decided_card(row):
        view = row["view"]
        if row.get("state") == "void":
            return cards.void_card(view, bool(row.get("approved")))
        return cards.result_card(view, row.get("state") == "approved", row.get("decided_at") or 0)

    def decide(self, ctx, message_id, request_id, view, approved) -> str:
        """Tell the page (no lock held) and redraw the card; the outcome: 'sent', 'void' (the page took nothing), 'failed' (said on the
        card, buttons kept) or 'expired'."""
        try:
            now = self.clock()
            if now - view["at"] > cards.VALID_SECONDS:
                self.put(ctx.state_dir, request_id, state="expired")
                self.messenger.patch_or_tell(ctx.profile, message_id, cards.expired_card(view), voice=self.voice, what=f"「{cards.TITLE}」卡")
                return "expired"
            decided = int(now)
            item = {"type": "login", "key": request_id, "rev": f"{request_id}-{int(now * 1000)}",
                    "data": {"decision": "approved" if approved else "denied", "decided_at": decided}}
            try:
                answer = self.request("POST", "/sync/upsert", {"items": [item], "deleted": []})
            except Exception as exc:  # noqa: BLE001 - said on the card
                failed = str(exc) if exc.args else type(exc).__name__
                self.runtime.log(f"web login decision not taken request={request_id[:8]}: {type(exc).__name__}")
                self.messenger.patch_or_tell(ctx.profile, message_id, cards.pending_card(view, failed or type(exc).__name__),
                                             voice=self.voice, what=f"「{cards.TITLE}」卡")
                return "failed"
            took = answer.get("upserted") if isinstance(answer, dict) else None
            if isinstance(took, int) and not isinstance(took, bool) and took < 1:
                # The page answered and changed nothing: its request is no longer waiting (expired or decided over there, or the
                # page is not a self-hosted one). Nothing is to be retried: the card says the login is void.
                self.put(ctx.state_dir, request_id, state="void", approved=approved, decided_at=decided)
                self.messenger.patch_or_tell(ctx.profile, message_id, cards.void_card(view, approved), voice=self.voice, what=f"「{cards.TITLE}」卡")
                self.runtime.log(f"web login void request={request_id[:8]}")
                return "void"
            self.put(ctx.state_dir, request_id, state="approved" if approved else "denied", decided_at=decided)
            self.messenger.patch_or_tell(ctx.profile, message_id, cards.result_card(view, approved, decided), voice=self.voice,
                                         what=f"「{cards.TITLE}」卡")
            self.runtime.log(f"web login {'approved' if approved else 'denied'} request={request_id[:8]}")
            return "sent"
        finally:
            with self.lock:
                self.busy.discard(request_id)
