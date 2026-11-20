"""Publish only explicitly recorded, bounded artifacts of a task.

New task cards list Drive links: the files are uploaded with the bot identity
into a per-task folder in a folder of the day under a root folder the owner can manage (a new root, and a day's
next folder, when one is full: Feishu holds 1500 children in a folder); a recorded
directory goes up as one zip, and configuration and credential paths are left
off the card. Older cards
keep their per-file "send to topic" buttons, and an over-long reply is still
sent to the topic as a Markdown file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from covey.app import answer_cards
from covey.app.artifact_policy import (MAX_ARTIFACT_BYTES, MAX_DRIVE_ARTIFACT_BYTES, artifact_key, artifact_paths,
                                       config_path, drive_paths, resolve_artifact, zip_directory)
from covey.app.card_actions import action_value
from covey.cards.answer_card import ARTIFACTS_ID
from covey.cards.artifact_panels import artifact_panel, drive_artifact_panel
from covey.core import background
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.locks import resource_lock, shared_file_lock
from covey.core.state import read_json, write_json, write_private_text
from covey.feishu.drive import DriveError, drive_call, drive_upload, private_folder, private_staging
from covey.feishu.message_request import reply_in_thread
from covey.feishu.transport import classify, cli_result, lark_argv, set_failure

DRIVE_ROOT_FOLDER = "Harness Bridge 产物"
DAYS_KEPT = 7  # Day folders root-folder.json remembers; older ones stay on Drive, only their entries go.


def answer_files(state_dir, record, topic_id, voice):
    """The 产物 panel of an answer card (app/answer_cards.py) for the job ``record`` as its upload stands now; None
    without artifacts."""
    paths = drive_paths(record) if record else None
    if not paths:
        return None
    return drive_artifact_panel(paths, record.get("job_id"), topic_id, answer_cards.ledger_of(state_dir, record, voice),
                                element_id=ARTIFACTS_ID, fold_uploading=True)


class Artifacts:
    """One Harness's task artifacts: the 产物 controls on a finished task card, their Drive
    upload, the older per-file sends, and an over-long reply sent as a Markdown file.

    - ``harness``: the voice of the sent files and messages, and its topic's job records.
    - ``runtime``: the owner (Drive permission) and the log.
    - ``messenger``: files and messages sent to the topic.
    - ``cards``: the controls on the task card and its redrawn panel.
    - ``copies``: repaints a 话题区 question's top-level copy showing a turn's 产物 (``repaint_copy`` of the
      Questions facade, app/question_display.py); None: no copies to repaint.
    """

    def __init__(self, harness, *, runtime, messenger, cards, copies=None) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.copies = copies

    def attach_artifact_controls(self, ctx, job):
        """A finished turn's 产物 go up to Drive: on the answer card when it shows them (app/answer_cards.py; the
        task card then leaves them out), else on the task card right under its body (a slim card's too, not in
        its folded 详情: the owner, 2026-11-08, 产物得从过程中挪下来). A quiet butler turn that sent no task card
        (core/placement.py Row.quiet, job.progress None) still has its answer card's 产物 uploaded and redrawn."""
        if job.artifact_controls_attached:
            return
        record = find_job_record(ctx.state_dir, job.message_id)
        if not record or not drive_paths(record):
            if record:
                self.log_skipped(record)
            return
        if answer_cards.holds_files(ctx.state_dir, record["key"]):
            job.artifact_controls_attached = True
            self.in_background(self.publish_artifacts, ctx, job, record, False)
            return
        if not job.progress:
            return
        job.artifact_controls_attached = bool(self.cards.set_task_element(
            ctx, job, drive_artifact_panel(drive_paths(record), record.get("job_id"), ctx.topic_id), under=True))
        if job.artifact_controls_attached:
            self.in_background(self.publish_artifacts, ctx, job, record)

    @staticmethod
    def in_background(target, *args):
        background.start(background.UPLOAD, target, *args)

    def publish_artifacts(self, ctx, job, record, on_task_card=True):
        ledger = self.upload_artifacts(ctx, record)
        if on_task_card:
            self.cards.set_task_element(ctx, job, drive_artifact_panel(drive_paths(record), record.get("job_id"), ctx.topic_id, ledger), under=True)
        self.refresh_answer_cards(ctx, record)

    def refresh_answer_cards(self, ctx, record, force=False):
        """The answer cards' 产物 brought up to the upload (app/answer_cards.py refresh), and the top-level copies of
        the turn's questions showing them repainted whole (``copies``, through CardKit: their forms change); never
        raises."""
        try:
            panel = answer_files(ctx.state_dir, record, ctx.topic_id, self.voice)
            status = (answer_cards.ledger_of(ctx.state_dir, record, self.voice) or {}).get("status")
            answer_cards.refresh(ctx.state_dir, ctx.profile, self.messenger, record["key"], panel, status,
                                 self.runtime.log, force=force)
            if self.copies is not None and panel is not None:
                for token in answer_cards.copies(ctx.state_dir, record["key"], status, force=force):
                    self.copies.repaint_copy(ctx, token)
        except Exception as exc:  # noqa: BLE001 - the files are up; a card that says otherwise has 重新上传
            self.runtime.log(f"answer card 产物 refresh failed: {type(exc).__name__}")

    @staticmethod
    def drive_record(ctx):
        return ctx.state_dir / "artifact-drive" / "root-folder.json"

    def drive_root_folder(self, ctx):
        """The current root folder: one per bot state until it is full, then "Harness Bridge 产物 2" and on
        (day_folder); the owner manages it and inherits every file in it."""
        path = self.drive_record(ctx)
        with shared_file_lock(path):
            saved = read_json(path) if path.exists() else {}
            return self._root(ctx, path, saved)

    def _root(self, ctx, path, saved):
        """``saved``'s root folder, made when missing and shared with the owner; ``saved`` written as it changes.
        root-folder.json: ``folder_token``, ``url``, ``owner`` of the current root (all an older release reads);
        ``roots``, the full ones before it; ``days``, day_folder's folders of the last DAYS_KEPT days. Under its
        lock, held across processes: task runners upload too, and two writers of the record could each make a
        day folder or a root, the later write dropping the other."""
        log = self.runtime.log
        if not saved.get("folder_token"):
            data = drive_call(ctx.profile, "+create-folder", "--name", DRIVE_ROOT_FOLDER, log=log)
            saved.update(folder_token=data["folder_token"], url=data.get("url", ""))
            saved.pop("owner", None)
            write_json(path, saved, ensure_ascii=False)
        owner = self.runtime.owner_open_id or ""
        if owner and saved.get("owner") != owner:
            drive_call(ctx.profile, "+member-add", "--token", saved["folder_token"], "--type", "folder",
                       "--member-type", "openid", "--member-id", owner, "--perm", "full_access", "--yes", log=log)
            saved["owner"] = owner
            write_json(path, saved, ensure_ascii=False)
        return saved["folder_token"]

    def _next_root(self, ctx, path, saved):
        """A new root in the bot's space after the current one filled up ("Harness Bridge 产物 2", 3, ...); the
        full one stays as it is, listed in ``roots``."""
        roots = [r for r in saved.get("roots") or [] if isinstance(r, dict)]
        roots.append({"folder_token": saved["folder_token"], "url": saved.get("url", ""),
                      "name": saved.get("name") or DRIVE_ROOT_FOLDER})
        name = f"{DRIVE_ROOT_FOLDER} {len(roots) + 1}"
        data = drive_call(ctx.profile, "+create-folder", "--name", name, log=self.runtime.log)
        saved.update(folder_token=data["folder_token"], url=data.get("url", ""), name=name, roots=roots)
        saved.pop("owner", None)
        write_json(path, saved, ensure_ascii=False)
        self.runtime.log(f"artifact drive root full: new root {name} roots={len(roots) + 1}")
        return self._root(ctx, path, saved)

    def day_folder(self, ctx, full=None):
        """Today's folder (local date "YYYY-MM-DD") in the current root, made when missing; ``full``: that folder
        is full, so the day moves on to "YYYY-MM-DD (2)" (then 3, ...) unless another upload already moved it.
        A root too full for the day's folder gives way to a new root (_next_root)."""
        import time
        today = time.strftime("%Y-%m-%d", time.localtime())
        path = self.drive_record(ctx)
        with shared_file_lock(path):
            saved = read_json(path) if path.exists() else {}
            root = self._root(ctx, path, saved)
            days = saved.get("days") if isinstance(saved.get("days"), dict) else {}
            day = days.get(today) if isinstance(days.get(today), dict) else {}
            if day.get("token") and day["token"] != full:
                return day["token"]
            number = int(day.get("number") or 1) + 1 if day.get("token") else 1
            name = today if number == 1 else f"{today} ({number})"
            try:
                data = drive_call(ctx.profile, "+create-folder", "--name", name, "--folder-token", root,
                                  log=self.runtime.log)
            except DriveError as exc:
                if not exc.full:
                    raise
                root = self._next_root(ctx, path, saved)
                data = drive_call(ctx.profile, "+create-folder", "--name", name, "--folder-token", root,
                                  log=self.runtime.log)
            since = time.strftime("%Y-%m-%d", time.localtime(time.time() - DAYS_KEPT * 86400))
            days = {d: v for d, v in days.items() if d >= since and d != today}
            days[today] = {"token": data["folder_token"], "url": data.get("url", ""), "name": name,
                           "number": number, "root": root}
            saved["days"] = days
            write_json(path, saved, ensure_ascii=False)
            return days[today]["token"]

    def in_day_folder(self, ctx, put):
        """``put(folder_token)`` into today's folder (day_folder); once more into the day's next folder when that
        one is full. ``put``'s result."""
        folder = self.day_folder(ctx)
        try:
            return put(folder)
        except DriveError as exc:
            if not exc.full:
                raise
            return put(self.day_folder(ctx, full=folder))

    def upload_artifacts(self, ctx, record):
        """Upload a task's recorded artifacts once per file content; never raises."""
        import time
        path = ctx.state_dir / "artifact-drive" / (self.voice.safe_key(record["key"]) + ".json")
        with resource_lock("artifact-drive", str(path)):
            ledger = read_json(path) if path.exists() else {}
            ledger.setdefault("files", {})
            ledger.update(errors={}, status="uploading", error="")
            self.log_skipped(record)
            try:
                if not ledger.get("folder_token"):
                    name = time.strftime("%Y-%m-%d %H:%M ", time.localtime()) + f"任务 #{record['job_id']}"
                    data = self.in_day_folder(ctx, lambda day: drive_call(
                        ctx.profile, "+create-folder", "--name", name, "--folder-token", day, log=self.runtime.log))
                    ledger.update(folder_token=data["folder_token"], folder_url=data.get("url", ""))
                    write_json(path, ledger, ensure_ascii=False)
                for raw in drive_paths(record):
                    key = artifact_key(raw)
                    try:
                        source = resolve_artifact(record, key, MAX_DRIVE_ARTIFACT_BYTES, directories=True, state_dir=ctx.state_dir)
                    except ValueError as exc:
                        ledger["errors"][key] = str(exc)
                        continue
                    self._upload_one(ctx, ledger, key, source)
                    write_json(path, ledger, ensure_ascii=False)
                ledger["status"] = "done"
            except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
                ledger.update(status="failed", error="上传超时" if isinstance(exc, subprocess.TimeoutExpired) else str(exc))
                self.runtime.log(f"artifact upload failed: {type(exc).__name__}")
            write_json(path, ledger, ensure_ascii=False)
            return ledger

    def log_skipped(self, record):
        skipped = sum(1 for raw in artifact_paths(record) if config_path(record, raw))
        if skipped:
            self.runtime.log(f"artifact upload skipped config paths job=#{record.get('job_id')} count={skipped}")

    def _upload_one(self, ctx, ledger, key, source):
        # Upload a private snapshot so the recorded digest is exactly what Drive received.
        # A directory's snapshot is its zip, written here and never in the workspace.
        with private_folder(ctx.state_dir, "artifact-upload") as folder:
            directory = source.is_dir()
            name = (source.name or "artifact") + ".zip" if directory else source.name
            copy = folder / name
            if directory:
                try:
                    digest = zip_directory(source, copy)
                except ValueError as exc:
                    ledger["errors"][key] = str(exc)
                    return
            else:
                digest = hashlib.sha256()
                with source.open("rb") as incoming, copy.open("wb") as out:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        digest.update(chunk)
                        out.write(chunk)
                digest = digest.hexdigest()
            size = copy.stat().st_size
            if ledger["files"].get(key, {}).get("sha256") == digest:
                return
            data = drive_upload(ctx.profile, copy, ledger["folder_token"], log=self.runtime.log)
            ledger["files"][key] = {"name": data.get("file_name") or name, "size": size, "sha256": digest,
                                    "token": data["file_token"], "url": data["url"]}

    def send_result_file(self, ctx, job, text):
        """``text``, a result too long for a card, as a file in reply to ``job``'s message, or, when ``ctx`` replies
        to none (a quiet butler turn's answer at the top level, app/worker.py top_ctx, D1), to the chat
        itself; whether it went."""
        key = job_key(job.message_id, job.job_id)
        path = ctx.state_dir / "result-files" / (key + ".md")
        write_private_text(path, text)
        upsert_job_record(ctx.state_dir, key, result_file_path=str(path))
        record = {
            "key": key,
            "workdir": str(ctx.workdir),
            "result_file_path": str(path),
            "reply_to_message_id": job.reply_to_message_id if ctx.reply_to_message_id else "",
            "chat_id": job.chat_id,
        }
        try:
            self.send_artifact(ctx, record, artifact_key(str(path)))
            return True
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, (OSError, subprocess.TimeoutExpired)):
                set_failure(classify(exception=exc))
            self.runtime.log(f"result file delivery failed: {type(exc).__name__}")
            return False

    def send_artifact(self, ctx, record, key):
        source = resolve_artifact(record, key, state_dir=ctx.state_dir)
        staging = private_staging(ctx.state_dir, "artifact-upload")
        fd, name = tempfile.mkstemp(dir=staging, suffix=source.suffix)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as incoming:
                content = incoming.read(MAX_ARTIFACT_BYTES + 1)
                if not 0 < len(content) <= MAX_ARTIFACT_BYTES:
                    raise ValueError("文件大小已变化，无法发送")
                out.write(content)
            digest = hashlib.sha256(content).hexdigest()
            ledger = (
                ctx.state_dir
                / "artifact-deliveries"
                / (self.voice.safe_key(f"{record['key']}-{key}-{digest}") + ".json")
            )
            # One send per version of this file; uploads of other files go on in parallel.
            with resource_lock("artifact-delivery", str(ledger)):
                saved = read_json(ledger) if ledger.exists() else {}
                if saved.get("sent"):
                    return "该版本文件已发送到本话题，无需重复发送。"
                if not saved.get("file_key"):
                    payload, failure = cli_result(
                        lark_argv(ctx.profile, "im", "files", "create", "--as", "bot", "--data",
                                  json.dumps({"file_name": source.name, "file_type": "stream"}),
                                  "--file", f"file={temp.name}", "--format", "json"),
                        timeout=60, cwd=staging, runner=subprocess.run)
                    if failure:
                        set_failure(failure)
                        raise ValueError("文件上传失败，请稍后重试")
                    data = payload.get("data", payload)
                    file_key = data.get("file_key")
                    if not file_key:
                        raise ValueError("文件上传未返回文件标识，请重试")
                    saved = {"file_key": file_key, "sha256": digest, "source": str(source)}
                    write_json(ledger, saved)
                reply_to = str(record.get("reply_to_message_id") or "")
                content = {"msg_type": "file", "content": json.dumps({"file_key": saved["file_key"]}),
                           "uuid": self.voice.safe_key("artifact-" + ledger.stem)}
                if reply_to:
                    result = self.messenger.cardkit(
                        ctx.profile, "POST", f"/open-apis/im/v1/messages/{reply_to}/reply",
                        {**content, **({"reply_in_thread": True}
                                       if reply_in_thread(str(record.get("chat_id") or ""), reply_to) else {})})
                else:  # A new message in the chat, quoting nothing (``send_result_file``).
                    result = self.messenger.cardkit(
                        ctx.profile, "POST", "/open-apis/im/v1/messages",
                        {**content, "receive_id": str(record.get("chat_id") or "")},
                        params={"receive_id_type": "chat_id"})
                if result is None:
                    raise ValueError("文件已上传但未能发送，请重试；不会重新执行原任务")
                saved["sent"] = True
                write_json(ledger, saved)
            return ""
        finally:
            temp.unlink(missing_ok=True)

    def artifact_action(self, ctx, event):
        raw = action_value(event)
        if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
            return True
        chat = str(event.get("chat_id") or "")
        eid = str(event.get("event_id") or event.get("message_id") or "")
        try:
            record = next(
                (r for r in self.harness.topic_records(ctx, chat) if str(r["job_id"]) == str(raw.get("job_id"))), None
            )
            if not record:
                raise ValueError("当前话题找不到该任务的产物")
            if raw.get("kind") == "bridge_artifact_upload" and answer_cards.clicked(
                    ctx.state_dir, record["key"], str(event.get("message_id") or "")):
                # On an answer card (in the topic or at the top level) or a question's top-level copy showing the
                # turn's 产物 (answer_cards.register_copy): every card of the answer and every such copy is redrawn.
                def again():
                    self.upload_artifacts(ctx, record)
                    self.refresh_answer_cards(ctx, record, force=True)
                self.in_background(again)
                return True
            if raw.get("kind") == "bridge_artifact_upload":
                self.cards.update_record_panel(ctx, chat, eid, event, record, drive_artifact_panel(drive_paths(record), record.get("job_id"), ctx.topic_id))

                def retry():
                    ledger = self.upload_artifacts(ctx, record)
                    # A second update under the click's key would be rejected as a replay.
                    try:
                        self.cards.update_record_panel(ctx, chat, eid + "-uploaded", event, record,
                                                 drive_artifact_panel(drive_paths(record), record.get("job_id"), ctx.topic_id, ledger))
                    except ValueError as exc:
                        self.runtime.log(f"artifact panel not refreshed after upload: {exc}")
                        self.messenger.reply(ctx, chat, "产物已处理，但原卡片没能刷新；可再点一次“重新上传”刷新，"
                                             "已上传的文件不会重复上传。", self.voice.key("artifact-refresh", eid), voice=self.voice)
                self.in_background(retry)
                return True
            if raw.get("kind") == "bridge_artifact_page":
                panel = artifact_panel(artifact_paths(record), record.get("job_id"), ctx.topic_id, int(raw.get("page", 0)))
                panel['expanded'] = True
                self.cards.update_record_panel(ctx, chat, eid, event, record, panel)
                return True
            text = self.send_artifact(ctx, record, str(raw.get("artifact_key") or ""))
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            text = "文件发送暂未完成，请稍后重试。" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        if text:
            self.messenger.reply(ctx, chat, text, self.voice.key("artifact-result", eid), voice=self.voice)
        return True
