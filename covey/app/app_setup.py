"""The Feishu app's configuration kept up with the Bridge (docs/operations.md 自动补齐飞书应用):
使用者只扫码，其余由 Bridge 用浏览器完成.

A worker checks the online app FIRST_CHECK seconds after the start and every CHECK_EVERY after
(feishu/app_config.py, the official API as the bot; core/app_manifest.py says what is missing). When something
the owner can add is missing, the owner's private chat gets one 飞书应用待补 card (cards/app_setup.py), once per
set of missing things (``notified``; 这些先不补 sets ``skipped``). Missing only a reviewed scope sends no card.

扫码自动补齐 (the owner's click, app/unified.py PANELS) runs feishu/app_console.py under the Playwright interpreter
(COVEY_PLAYWRIGHT_PYTHON, else <data home>/proxy-web/venv/bin/python): a copy of the owner's 代答 web
login when there is one (core/maintenance.py web_profile), else its login QR replied under the card; the
console adds what the plan lists and publishes a version. The card shows each step, then the official read-back:
what took effect and what is still missing, where it stopped and why. The run's folder (the login copy, the QR,
screenshots) is removed when the run ends, whatever the outcome, and at the next start if the Bridge stopped mid-run.

Off: COVEY_APP_CHECK=0. Records: <state>/app-setup/state.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from covey.core import env as cenv
from covey.cards import app_setup as cards
from covey.core import app_manifest
from covey.core.locks import resource_lock
from covey.core.web_profile import web_profile
from covey.core.process import stop_group
from covey.core.state import claim_once, read_object, utc_now_iso, write_json
from covey.feishu.app_config import PYTHON_ENV, playwright_python, remove, survey
from covey.feishu.transport import lark_argv, run_cli

ENV = "COVEY_APP_CHECK"
CONSOLE_ENV = "COVEY_CONSOLE_URL"
FOLDER = "app-setup"
RECORD = "state.json"
FIRST_CHECK = 90  # After the start: the event consumers are up and a deploy has settled.
CHECK_EVERY = 24 * 3600
CHAT_POLL = 15  # A fresh install has no private chat yet: look for the owner's first message this often.
RUN_SECONDS = 15 * 60  # One run, QR wait included; its process group is stopped past it.
INBOUND = "inbound-messages"


def enabled(env=None):
    return str(cenv.get(ENV, "", env)).strip() != "0"


def need_dict(need):
    return {"id": need.id, "kind": need.kind, "name": need.name, "words": need.words, "review": need.review,
            "label": need.label, "icon": need.icon}


def as_need(entry):
    return app_manifest.Need(entry["kind"], entry["name"], entry["words"], review=bool(entry.get("review")),
                             label=entry.get("label", ""), icon=entry.get("icon", ""))


class AppSetup:
    def __init__(self, runtime, messenger, voice) -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.busy = threading.Lock()  # One console run at a time.
        self.no_chat = False  # The last check had something to offer but nobody to offer it to.

    # Records -------------------------------------------------------------------------------------

    def path(self, ctx):
        return ctx.state_dir / FOLDER / RECORD

    def record(self, ctx):
        return read_object(self.path(ctx))

    def save(self, ctx, **fields):
        path = self.path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        with resource_lock("app-setup", str(path)):
            write_json(path, {**read_object(path), **fields}, ensure_ascii=False)

    def app_id(self, ctx):
        return str(read_object(ctx.state_dir / "bot-identity.json").get("app_id") or "")

    # The check -----------------------------------------------------------------------------------

    def threads(self):
        """Its worker, unless COVEY_APP_CHECK=0 (app/unified.py worker_targets)."""
        return (("app-setup", self.run),) if enabled() else ()

    def run(self, ctx) -> None:
        self.clear_leftovers(ctx)
        wait = FIRST_CHECK
        while not self.runtime.shutdown.wait(wait):
            try:
                self.check(ctx)
            except Exception as exc:  # noqa: BLE001 - the next check tries again
                self.runtime.log(f"app setup check failed: {type(exc).__name__}: {str(exc)[:200]}")
            wait = CHECK_EVERY
            if self.no_chat:
                # Not a day later: the card is promised for the owner's first message (install output).
                while not self.runtime.shutdown.wait(CHAT_POLL):
                    if self.runtime.access.notify_chat():
                        wait = 2
                        break

    def clear_leftovers(self, ctx):
        """Run folders a Bridge that stopped mid-run left (a login copy among them): no run is going at a start."""
        runs = ctx.state_dir / FOLDER / "runs"
        try:
            folders = [path for path in runs.iterdir() if path.is_dir()]
        except OSError:
            return
        for folder in folders:
            remove(folder, self.runtime.log)
        if folders:
            self.runtime.log(f"app setup removed {len(folders)} run folder(s) left by an earlier process")

    def check(self, ctx):
        """Read the online app; offer what is missing once per set. Returns the view it built (None: nothing)."""
        self.no_chat = False
        app_id = self.app_id(ctx)
        if not app_id:
            return None
        online, lacking, full = survey(ctx.profile, app_id)
        offer = app_manifest.offered(lacking, full)
        review = [n for n in lacking if n.review]
        self.save(ctx, checked_at=utc_now_iso(), version=online.get("version", ""), error=online.get("error", ""),
                  lacking=[n.id for n in lacking], offered=[n.id for n in offer])
        self.runtime.log(f"app setup check version={online.get('version') or '-'} error={online.get('error') or '-'} "
                         f"lacking={len(lacking)} offered={len(offer)} review={len(review)} full={len(full)}")
        if not offer:
            return None
        digest = app_manifest.digest(offer)
        record = self.record(ctx)
        if digest in (record.get("notified"), record.get("skipped")):
            return None
        chat = self.runtime.access.notify_chat()
        self.no_chat = not chat
        if not chat:
            self.runtime.log("app setup card not sent: no private chat with the owner yet")
            return None
        view = {"offer": [need_dict(n) for n in offer], "review": [need_dict(n) for n in review],
                "full": [need_dict(n) for n in full], "version": online.get("version", ""),
                "blind": online.get("error") == "no_self_manage", "digest": digest, "chat_id": chat}
        sent = []
        key = self.voice.safe_key(f"app-setup-{digest}-{int(time.time())}")
        if self.messenger.send_message(ctx.profile, chat, cards.offer_text(view), key, voice=self.voice,
                                       card=cards.offer_card(view), on_card=sent.append):
            self.save(ctx, notified=digest, card={"message_id": sent[0] if sent else "", "view": view})
            self.runtime.log(f"app setup card sent offered={len(offer)} card={'yes' if sent else 'no'}")
        return view

    # The clicks ----------------------------------------------------------------------------------

    def matches(self, raw):
        return isinstance(raw, dict) and raw.get("kind") in (cards.START, cards.SKIP)

    def claim(self, ctx, event, raw, owner):
        """A 飞书应用待补 card's click (app/unified.py PANELS), else None."""
        if not self.matches(raw):
            return None
        if not owner:
            return "owner_only"
        event_id = str(event.get("event_id") or "")
        if not event_id or not claim_once(ctx.state_dir / INBOUND, "app-setup-" + event_id):
            return "duplicate"
        message_id = str(event.get("message_id") or "")
        card = self.record(ctx).get("card") or {}
        view = card.get("view") if card.get("message_id") == message_id else None
        if not isinstance(view, dict):
            return "app_setup_stale"
        if raw["kind"] == cards.SKIP:
            self.save(ctx, skipped=view.get("digest"))
            self.messenger.patch_card(ctx.profile, message_id, cards.skipped_card(view))
            self.runtime.log("app setup skipped by the owner")
            return "app_setup_skip"
        if not self.busy.acquire(blocking=False):
            return "app_setup_busy"
        threading.Thread(target=self.apply, args=(ctx, message_id, view, bool(raw.get("review"))),
                         name="app-setup-run", daemon=True).start()
        return "app_setup_start"

    # The run -------------------------------------------------------------------------------------

    def apply(self, ctx, message_id, view, review):
        """One console run for the card ``message_id``; holds ``busy`` (taken by claim) until it ends."""
        folder = None
        try:
            folder = self.prepare(ctx)
            self.run_console(ctx, message_id, view, review, folder)
        except Exception as exc:  # noqa: BLE001 - said on the card
            self.runtime.log(f"app setup run failed: {type(exc).__name__}: {str(exc)[:200]}")
            self.messenger.patch_card(ctx.profile, message_id, cards.result_card({
                **view, "failed": {"words": "准备", "detail": f"{type(exc).__name__}"}, "steps": {},
                "still": [n["id"] for n in view["offer"]]}))
        finally:
            if folder is not None:
                remove(folder, self.runtime.log)  # The login copy, the QR, the screenshots.
            self.busy.release()

    def prepare(self, ctx):
        runs = ctx.state_dir / FOLDER / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        os.chmod(runs, 0o700)
        folder = runs / time.strftime("%Y%m%d-%H%M%S")
        folder.mkdir(mode=0o700)
        return folder

    def run_console(self, ctx, message_id, view, review, folder):
        app_id = self.app_id(ctx)
        online, lacking, full = survey(ctx.profile, app_id)
        offer = app_manifest.offered(lacking, full, review=review)
        if not offer:
            self.messenger.patch_card(ctx.profile, message_id, cards.result_card({**view, "offer": view["offer"],
                                                                                   "still": [], "steps": {}}))
            self.runtime.log("app setup run: nothing is missing any more")
            return
        view = {**view, "offer": [need_dict(n) for n in offer], "version": online.get("version", ""),
                "with_review": review}
        python = playwright_python()
        if python is None:
            self.messenger.patch_card(ctx.profile, message_id, cards.unavailable_card(
                view, f"没有装了 Playwright 的 Python（{PYTHON_ENV}）"))
            self.runtime.log("app setup run: no Playwright interpreter")
            return
        plan = app_manifest.plan(offer, remark=app_manifest.remark(offer))
        (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
        login = web_profile(ctx.state_dir)
        argv = [python, "-I", str(self.runtime.script_path.parent / "covey_app_console.py"), "--app-id", app_id,
                "--home", str(folder / "home"), "--plan", str(folder / "plan.json")]
        if (login / "Default").is_dir():
            argv += ["--login-from", str(login)]
        if cenv.get(CONSOLE_ENV, "").strip():
            argv += ["--console", cenv.get(CONSOLE_ENV).strip()]
        steps, state = {}, {"qr": False, "page": ""}
        progress = lambda: self.messenger.patch_card(ctx.profile, message_id, cards.progress_card(
            {**view, "steps": steps, **state}))
        progress()
        outcome = self.follow(ctx, message_id, argv, folder, steps, state, progress)
        steps["check"] = "doing"
        after, lacking_now, _ = survey(ctx.profile, app_id)
        still = [n["id"] for n in view["offer"] if n["id"] in {need.id for need in lacking_now}]
        steps["check"] = "failed" if still or after.get("error") else "done"
        result = {**view, "steps": steps, "still": still, "new_version": outcome.get("version", ""),
                  "reviewing": outcome.get("status") == "reviewing",
                  "version": after.get("version") or view.get("version", "")}
        if outcome.get("event") == "failed":
            result["failed"] = {"words": outcome.get("words") or outcome.get("step"), "detail": outcome.get("detail", ""),
                                "changed": outcome.get("changed") or []}
            if outcome.get("shot"):
                self.reply_image(ctx, message_id, Path(outcome["shot"]), folder)
        elif outcome.get("event") != "done":
            result["failed"] = {"words": "浏览器", "detail": outcome.get("detail") or "没有结果", "changed": []}
        self.messenger.patch_card(ctx.profile, message_id, cards.result_card(result))
        self.save(ctx, last_run={"at": utc_now_iso(), "outcome": outcome.get("event", ""), "still": still,
                                 "version": outcome.get("version", ""), "step": outcome.get("step", "")})
        self.runtime.log(f"app setup run outcome={outcome.get('event') or '-'} step={outcome.get('step') or '-'} "
                         f"version={outcome.get('version') or '-'} still={len(still)}")

    def follow(self, ctx, message_id, argv, folder, steps, state, progress):
        """Run the console process and follow its lines: the QR replied under the card, the steps on it. Its last
        ``done`` or ``failed`` line (``detail`` when it ended without one)."""
        env = {**os.environ, "TMPDIR": str(folder)}
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                text=True, cwd=str(folder), env=env, start_new_session=True)
        timer = threading.Timer(RUN_SECONDS, lambda: stop_group(proc, 10))
        timer.daemon = True
        timer.start()
        outcome = {}
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("event")
                if kind == "qr":
                    steps["login"] = "doing"
                    state["qr"] = True
                    self.reply_image(ctx, message_id, Path(str(event.get("path") or "")), folder)
                    progress()
                elif kind == "login_page":
                    state["page"] = str(event.get("text") or "")
                    progress()
                elif kind == "logged_in":
                    steps["login"], state["qr"], state["page"] = "done", False, ""
                    progress()
                elif kind == "step":
                    steps[str(event.get("name"))] = str(event.get("state"))
                    progress()
                elif kind in ("done", "failed"):
                    outcome = event
                    if kind == "failed":
                        steps[str(event.get("step"))] = "failed"
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            stop_group(proc, 5)
        finally:
            timer.cancel()
        if not outcome:
            outcome = {"event": "", "detail": f"浏览器进程结束（退出码 {proc.returncode}），没有结果"
                       if proc.returncode is not None else "超过 15 分钟没有结果"}
        return outcome

    def reply_image(self, ctx, message_id, path, folder):
        """``path`` (a PNG in the run's folder) as an image reply under the card; never one from elsewhere."""
        try:
            if folder.resolve() not in path.resolve().parents or not path.is_file():
                return
            run_cli(lark_argv(ctx.profile, "im", "+messages-reply", "--as", "bot", "--message-id", message_id,
                              "--image", path.name), runner=subprocess.run, cwd=str(path.parent),
                    capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.runtime.log(f"app setup image not sent: {type(exc).__name__}")
