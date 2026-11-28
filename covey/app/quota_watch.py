"""账号与额度 at the Bridge (docs/quota.md): one thread that keeps the quota snapshots (core/quota.py) current, unless
COVEY_QUOTA=0. Every TICK seconds the sources that are due are read (core/quota_sources.py ``refresh``: the extensions'
sources, the commands and addresses of quota-sources.json, each at its own interval) and the agy
snapshots are written from the accounts' state (backends/agy/accounts.py ``snapshots``: no account is asked). The page and the
failure cards read the files; the one thing sent from here is the owner's notice when a source has no account left to use or an
account needs a sign-in again (app/quota_notice.py), after each look. The page's 刷新 reads at once (app/web_quota.py Refresher).

A Bridge with nothing to read (no extension's source, no quota-sources.json, fewer than two agy accounts) starts no thread
(``wanted``, asked when the threads are listed: a source added later is read after the next restart). A
quota-sources.json that may not be used (another user's, or others may write it) is written to the log once a change.
"""
from __future__ import annotations

import time

from covey.app import pins, quota_notice
from covey.backends.agy import accounts
from covey.core import quota, quota_sources

TICK = 60  # Seconds between looks.
START_WAIT = 20  # Seconds after the Bridge starts before the first.


def wanted(environ=None) -> bool:
    """Whether there is anything to read (see the module docstring)."""
    if not quota_sources.enabled(environ):
        return False
    found, problem = quota_sources.sources(environ)
    return bool(found or problem or accounts.configured(environ))


class QuotaWatch:
    """The feature object (app/unified.py): its thread, none when ``wanted`` says there is nothing to read."""
    name = "quota"

    def __init__(self, runtime, messenger=None, voice=None) -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.problem = ""
        self.notice = None
        self.failing = {}  # Source id -> why its last read failed, for the log line when that changes.

    def threads(self):
        return ((self.name, self.run),) if wanted() else ()

    def run(self, ctx) -> None:
        from covey.core.loops import every

        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def tick(self, ctx) -> list:
        """One look: the due sources read and the agy snapshots written; the source ids written."""
        if not quota_sources.enabled():
            return []
        found, problem = quota_sources.sources()
        if problem != self.problem:
            self.problem = problem
            if problem:
                self.runtime.log(f"quota: {quota_sources.CONFIG_FILE} not used: {problem}")
        quota_sources.forget_gone(ctx.state_dir, found)
        done = quota_sources.refresh(ctx.state_dir, found)
        self.note(ctx.state_dir, done)
        done += accounts.snapshots(ctx.state_dir)
        self.tell(ctx)
        return done

    def note(self, state_dir, ids) -> None:
        """One log line when a source's reads begin to fail (and why) and one when it reads again: what the page shows as
        上次读取没成功 can be looked up afterwards."""
        store = quota.Store(state_dir)
        for source in ids:
            why = str(store.read(source).get("error") or "")
            if why != self.failing.get(source, ""):
                self.runtime.log(f"quota source {source}: " + (f"read failed: {why}" if why else "read again"))
                self.failing[source] = why

    def tell(self, ctx) -> str:
        """The owner's notice of what turned true since the last look (app/quota_notice.py); never raises. The text sent."""
        if self.messenger is None or self.voice is None:
            return ""
        if self.notice is None:
            self.notice = quota_notice.Notice(ctx.state_dir, lambda text: self.send(ctx, text), self.runtime.log)
        try:
            return self.notice.step()
        except Exception as exc:  # noqa: BLE001 - the next look tries again
            self.runtime.log(f"quota notice failed: {type(exc).__name__}")
            return ""

    def send(self, ctx, text):
        """``text`` to the notify chat at its top level (a reply to it is the butler's): True when it went, False when it failed,
        None with no chat."""
        chat = self.runtime.access.notify_chat()
        if not chat:
            return None
        key = self.voice.safe_key(f"quota-notice-{int(time.time())}")
        return self.messenger.send_message(ctx.profile, chat, text, key, voice=self.voice,
                                           on_sent=lambda message: pins.remember_notice(ctx.state_dir, chat, message))
