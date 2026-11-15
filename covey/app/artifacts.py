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

from contextlib import contextmanager
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from covey.app import answer_cards, device_files
from covey.cards.answer_card import ARTIFACTS_ID
from covey.core import background, extensions
from covey.core.checkpoint import read_checkpoint
from covey.core.locks import resource_lock, shared_file_lock
from covey.feishu.message_request import reply_in_thread
from covey.feishu.transport import cli_result, lark_argv
from covey.core.state import write_private_text, read_json, write_json
from covey.app.card_actions import action_value
from covey.cards.elements import callback_button, markdown_panel, md
from covey.cards.review import compact_text
from covey.core.text import safe_text
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.feishu.transport import classify, set_failure

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_DRIVE_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_DIRECTORY_FILES = 5000
AGENT_DIRS = {".claude", *extensions.agent_dirs()}  # Where an agent keeps its worktrees (an extension adds its own).
SENSITIVE_NAMES = {".git", ".ssh", ".codex", "credentials.json", "auth.json", *AGENT_DIRS}
DRIVE_ROOT_FOLDER = "Harness Bridge 产物"
# Feishu holds at most 1500 children in a folder and answers this code when one more is created in a full one.
FOLDER_LIMIT = 1500
FOLDER_FULL = 1062507
DAYS_KEPT = 7  # Day folders root-folder.json remembers; older ones stay on Drive, only their entries go.
DIFF_PRUNED = "差异报告已过期清理"
# A 代答 contact's notes (core/proxy.py session_dir_name) and the run data the model keeps beside them:
# every 代答 turn recorded its README.md, and the card uploaded it again each time (UI review P8).
# Where they are kept is a deployment's (the extension's ``contact_notes`` setting, a regex); none by default.
CONTACT_NOTES = re.compile(extensions.setting("contact_notes", r"(?!)"))


def artifact_paths(record):
    cp = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    raw = cp.get("artifacts", [])
    paths = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    if record.get("result_file_path"):
        paths.append(record["result_file_path"])
    if record.get("diff_artifact_path"):
        paths.append(record["diff_artifact_path"])
    return list(dict.fromkeys(paths))


def artifact_key(path):
    return hashlib.sha256(path.encode()).hexdigest()[:24]


def _local_path(record, raw):
    """``raw`` resolved against the task's workdir; ValueError for one that cannot be resolved
    (a symlink loop, ``~nosuchuser``), which the card then shows as that file's error. A path of a device
    (app/device_files.py) is never resolved here, the hub's links and home are not the device's: it is folded
    as text, against the device's working directory."""
    if device_files.is_remote(record, raw):
        return device_files.remote_path(record, raw)
    try:
        root = Path(record["workdir"]).resolve()
        path = Path(raw).expanduser()
        return (path if path.is_absolute() else root / path).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("产物路径无法解析") from exc


def _entry_name(name):
    """A zip entry name for a file name that may hold bytes that are not UTF-8 (os.fsdecode keeps them
    as surrogates, which a zip cannot store): those bytes become U+FFFD."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _secret_name(name):
    return name in SENSITIVE_NAMES or name == ".env" or name.startswith(".env.")


def _judged_parts(path):
    """The parts the denylist judges. Below an agent worktree (``<agent dir>/worktrees/<name>/``, as
    ``.claude/worktrees/<name>/``) the path is the agent's working copy, so the agent dir
    and ``worktrees`` are left out; everything else, the worktree name included, is still judged."""
    parts = path.parts
    for i, part in enumerate(parts[:-3]):
        if part in AGENT_DIRS and parts[i + 1] == "worktrees":
            return parts[:i] + parts[i + 2:]
    return parts


def device_policy():
    """The denylist a device's ``stat`` is given and the hub's own rule over the real path it reports back
    (app/device_files.py): the names and directories this module never publishes."""
    def denied(path):
        parts = _judged_parts(path)
        return ".local-state" in parts or any(part in SENSITIVE_NAMES for part in parts) or _secret_name(path.name)

    return device_files.Policy(tuple(sorted({*SENSITIVE_NAMES, ".env", ".env.*", ".local-state"})),
                               tuple(sorted("~/" + name for name in {".ssh", ".codex", *AGENT_DIRS})), denied)


def config_path(record, raw):
    """A configuration or credential path: never published, and left off the card."""
    if raw in {record.get("diff_artifact_path"), record.get("result_file_path")}:
        return False
    try:
        path = _local_path(record, raw)  # Resolved first, so ../ and links are judged where they lead.
    except ValueError:
        return False  # Not judged here: resolve_artifact reports it on the card.
    parts = _judged_parts(path)
    # A device's paths are not the hub's: its artifacts never live in the checkpoint's artifacts/ here.
    state_artifact = (bool(record.get("checkpoint_path")) and not device_files.is_remote(record, raw)
                      and path.is_relative_to(Path(record["checkpoint_path"]).resolve().parent / "artifacts"))
    return ((".local-state" in parts and not state_artifact) or
            any(part in SENSITIVE_NAMES for part in parts) or _secret_name(path.name))


def proxy_task(record):
    """Whether ``record`` is a 代答 task: the topic.json beside its history names the contact it answers."""
    try:
        return bool(read_json(Path(record["history_path"]).parent / "topic.json").get("proxy"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def contact_notes(record, raw):
    """A file of a 代答 contact's notes or their run data, judged where it resolves to."""
    try:
        path = _local_path(record, raw)
    except ValueError:
        path = Path(raw)
    return bool(CONTACT_NOTES.search(path.as_posix()))


def drive_paths(record):
    """The artifacts the Drive upload and its panel deal with: configuration and credential paths are
    skipped quietly, and so are, for a 代答 task, its contact's notes."""
    notes = proxy_task(record)
    return [raw for raw in artifact_paths(record)
            if not config_path(record, raw) and not (notes and contact_notes(record, raw))]


def resolve_artifact(record, key, max_bytes=MAX_ARTIFACT_BYTES, directories=False, state_dir=None):
    """The hub file for the artifact ``key`` of ``record``. A device's artifact is copied here first, through the
    device program (app/device_files.py; ``state_dir`` is where its copy goes, ValueError without it): a directory
    of a device is not one yet."""
    raw = next((p for p in artifact_paths(record) if artifact_key(p) == key), None)
    if raw is None:
        raise ValueError("产物记录已变化，请重新打开产物列表")
    path = _local_path(record, raw)
    if config_path(record, raw):
        raise ValueError("该文件属于配置或凭据目录，不能作为产物发送")
    if device_files.is_remote(record, raw):
        if state_dir is None:
            raise ValueError("设备上的产物需要状态目录才能取回")
        return device_files.fetch_artifact(state_dir, record, raw, max_bytes, device_policy())
    if path.is_dir():
        if directories:
            return path
        raise ValueError("这是目录，暂不支持发送")
    if not path.exists():
        # Maintenance bounds job-diffs/; the copy uploaded with the task's artifacts stays in Drive.
        raise ValueError(DIFF_PRUNED if raw == record.get("diff_artifact_path") else "产物文件不存在")
    if not path.is_file():
        raise ValueError("该产物不是普通文件，暂不支持发送")
    if not 0 < path.stat().st_size <= max_bytes:
        raise ValueError(f"仅支持非空且不超过 {max_bytes // (1024 * 1024)} MB 的文件")
    return path


def zip_directory(source, dest, max_bytes=MAX_DRIVE_ARTIFACT_BYTES, max_files=MAX_DIRECTORY_FILES):
    """Pack a registered directory as ``dest`` and return its SHA-256.

    Configuration and credential entries, symbolic links and special files are left out. Both caps
    are checked before anything is written. Entries are sorted and carry a fixed time and mode, so an
    unchanged directory packs to the same bytes and is uploaded once.
    """
    too_big = f"目录超过 {max_bytes // (1024 * 1024)} MB，未打包"
    entries, total, pending = [], 0, [(source, _entry_name(source.name or "artifact"))]
    try:
        while pending:
            folder, prefix = pending.pop()
            with os.scandir(folder) as listing:
                for entry in listing:
                    if _secret_name(entry.name) or entry.name == ".local-state" or entry.is_symlink():
                        continue
                    name = f"{prefix}/{_entry_name(entry.name)}"
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((entry.path, name))
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        entries.append((name, entry.path, size))
                        total += size
                        if len(entries) > max_files:
                            raise ValueError(f"目录超过 {max_files} 个文件，未打包")
                        if total > max_bytes:
                            raise ValueError(too_big)
        if not entries:
            raise ValueError("目录里没有可打包的文件")
        written = 0
        with zipfile.ZipFile(dest, "w") as archive:
            for name, path, size in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type, info.create_system, info.external_attr = zipfile.ZIP_DEFLATED, 3, 0o100644 << 16
                info.file_size = size
                with open(path, "rb") as incoming, archive.open(info, "w") as out:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(too_big)
                        out.write(chunk)
        digest = hashlib.sha256()
        with dest.open("rb") as packed:
            for chunk in iter(lambda: packed.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ValueError("目录里有读不了的文件，未打包") from exc


def _size(n):
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def drive_artifact_panel(record, topic_id, ledger=None, *, element_id="task_artifacts", fold_uploading=False):
    """Drive links for a task's artifacts. Without a finished ledger it shows the upload in progress.
    Folded once every file is up, its title naming what is in it (产物 3 个 · REPORT.md 等): open, it
    was the tallest block of a finished card (T182 #1111). Open while uploading or when something
    failed, where 重新上传 is; ``fold_uploading`` (an answer card's, app/answer_cards.py, ``element_id``
    ARTIFACTS_ID): folded while uploading too, open only when something failed."""
    paths = drive_paths(record)
    ledger = ledger or {}
    status = ledger.get("status")
    files, errors = ledger.get("files", {}), ledger.get("errors", {})
    lines = []
    for raw in paths:
        key, name = artifact_key(raw), safe_text(Path(raw).name or raw, 120)
        if key in files:
            lines.append(f"- [{safe_text(files[key]['name'], 120)}]({files[key]['url']}) · {_size(files[key]['size'])}")
        elif key in errors:
            lines.append(f"- {name}：{safe_text(errors[key], 160)}")
        else:
            lines.append(f"- {name}")
    keys = [artifact_key(raw) for raw in paths]
    left = [k for k in keys if k not in files]
    failed = [k for k in left if k in errors]
    uploading = status is None or (fold_uploading and status == "uploading")
    if uploading:
        title = f"产物 {len(paths)} 个 · 上传中" if fold_uploading else f"产物 · {len(paths)} 个 · 正在上传到云空间"
        note = "上传完成后这里会换成可点击的下载链接；长时间没有变化可点“重新上传”。"
    elif status == "failed":
        title, note = f"产物 · {len(paths)} 个 · 云空间上传失败", "上传失败：" + safe_text(ledger.get("error") or "未知原因", 300)
    else:
        first = compact_text(Path(paths[0]).name or paths[0], 40) if paths else ""
        title = (f"产物 {len(paths)} 个" + (f" · {first}" if first else "") + (" 等" if len(paths) > 1 else "")
                 + (f" · {len(left)} 个未上传" if left else ""))
        note = f"[在云空间打开本任务文件夹]({ledger['folder_url']})" if ledger.get("folder_url") else ""
        if failed:
            note = "处理好未上传的文件后点“重新上传”；已上传的文件不会重复上传。" + ("\n" + note if note else "")
    panel = markdown_panel(element_id, title, "\n".join(lines), quiet=True)
    if note:
        # Its own element: a line right after the list would join the last list item.
        panel['elements'].append(md(note, size='normal_v2'))
    # An upload cut off by a restart never reports back, so the uploading state offers a retry too.
    # A file that could not be uploaded can be fixed and retried from the same card.
    retry = bool(status in (None, "failed") or left)
    panel['expanded'] = bool(status == "failed" or (left and not uploading)) if fold_uploading else retry
    if retry:
        panel['elements'].append(callback_button('重新上传', {'kind': 'bridge_artifact_upload', 'topic_id': topic_id,
                                                             'job_id': record['job_id']}))
    return panel


def answer_files(state_dir, record, topic_id, voice):
    """The 产物 panel of an answer card (app/answer_cards.py) for the job ``record`` as its upload stands now; None
    without artifacts."""
    if not record or not drive_paths(record):
        return None
    return drive_artifact_panel(record, topic_id, answer_cards.ledger_of(state_dir, record, voice),
                                element_id=ARTIFACTS_ID, fold_uploading=True)


class DriveError(ValueError):
    """A failed lark-cli drive call: its text is what the card shows; ``code``, ``detail`` (Feishu's message) and
    ``log_id`` as Feishu answered, ``code`` None when it gave none. A ValueError, so the callers' except clauses
    that show the reason still take it."""

    def __init__(self, text, *, code=None, detail="", log_id=""):
        super().__init__(text)
        self.code, self.detail, self.log_id = code, detail, log_id

    @property
    def full(self):
        """The folder created into holds as many children as Feishu allows (FOLDER_FULL)."""
        return self.code == FOLDER_FULL


def _error_code(error):
    try:
        return int(error.get("code"))
    except (TypeError, ValueError):
        return None


def drive_error(payload, category):
    """The DriveError of a failed call that printed ``payload`` (lark-cli's error JSON, or None)."""
    error = (payload or {}).get("error") or {}
    if not isinstance(error, dict):
        error = {}
    code, detail = _error_code(error), safe_text(str(error.get("message") or ""), 200)
    log_id = safe_text(str(error.get("log_id") or ""), 80)
    if error.get("subtype") == "app_scope_not_applied":
        text = "机器人应用缺少云空间权限：" + ", ".join(error.get("missing_scopes") or [])
    elif code == FOLDER_FULL:
        text = f"云空间文件夹已满（一个文件夹最多 {FOLDER_LIMIT} 个）"
    elif code is not None:
        text = f"云空间接口调用失败（错误码 {code}" + (f"：{detail}" if detail else "") + "）"
    else:
        text = f"云空间接口调用失败（{category}）"
    return DriveError(text, code=code, detail=detail, log_id=log_id)


def drive_call(profile, *args, cwd=None, timeout=120, log=None):
    """lark-cli drive ``args`` as the bot: its data. A failure raises DriveError, logged first with Feishu's code,
    message and log_id (``log``, when given)."""
    payload, failure = cli_result(lark_argv(profile, "drive", *args, "--as", "bot", "--format", "json"),
                                  timeout=timeout, cwd=cwd, runner=subprocess.run)
    if failure:
        exc = drive_error(payload, failure.category)
        if log is not None:
            log(f"drive call failed op={args[0] if args else '-'} code={exc.code if exc.code is not None else '-'} "
                f"category={failure.category} msg={exc.detail or '-'} log_id={exc.log_id or '-'}")
        raise exc
    return payload.get("data", payload)


def drive_upload(profile, path, folder_token, log=None):
    """``path`` uploaded into ``folder_token`` under its own name, lark-cli run in its folder: drive_call's data.
    The bigger the file, the longer it may take."""
    size = path.stat().st_size
    return drive_call(profile, "+upload", "--file", path.name, "--name", path.name, "--folder-token", folder_token,
                      cwd=path.parent, timeout=min(1800, 120 + size // (512 * 1024)), log=log)


def private_staging(state_dir, name):
    """<state>/<name>, made private (0700) when missing: where a file is copied before it goes to Feishu."""
    staging = state_dir / name
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    return staging


@contextmanager
def private_folder(state_dir, name):
    """A new folder in private_staging(state_dir, name); its files and the folder are removed afterwards."""
    folder = Path(tempfile.mkdtemp(dir=private_staging(state_dir, name)))
    try:
        yield folder
    finally:
        for item in folder.iterdir():
            item.unlink(missing_ok=True)
        folder.rmdir()


def artifact_panel(record, topic_id, page=0):
    files = artifact_paths(record)
    pages = max(1, (len(files) + 4) // 5)
    page = max(0, min(page, pages - 1))
    title = f"产物 · {len(files)} 个"
    if pages > 1:
        title += f" · 第 {page + 1}/{pages} 页"
    panel = markdown_panel("task_artifacts", title, "点击发送到本话题；同一版本不会重复发送。", quiet=True, expanded=True)
    for path in files[page*5:(page+1)*5]:
        panel['elements'].append(callback_button('发送 ' + Path(path).name[:80], {
            'kind': 'bridge_artifact_send', 'topic_id': topic_id, 'job_id': record['job_id'],
            'artifact_key': artifact_key(path)}, width='fill'))
    for label, target in [('上一页',page-1),('下一页',page+1)]:
        if 0 <= target < pages:
            panel['elements'].append(callback_button(label, {'kind': 'bridge_artifact_page', 'topic_id': topic_id,
                                                             'job_id': record['job_id'], 'page': target}))
    return panel


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
        its folded 详情: the owner, 2026-11-08, 产物得从过程中挪下来). A quiet main-thread turn that sent no task card
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
            ctx, job, drive_artifact_panel(record, ctx.topic_id), under=True))
        if job.artifact_controls_attached:
            self.in_background(self.publish_artifacts, ctx, job, record)

    @staticmethod
    def in_background(target, *args):
        background.start(background.UPLOAD, target, *args)

    def publish_artifacts(self, ctx, job, record, on_task_card=True):
        ledger = self.upload_artifacts(ctx, record)
        if on_task_card:
            self.cards.set_task_element(ctx, job, drive_artifact_panel(record, ctx.topic_id, ledger), under=True)
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
        to none (a quiet main-thread turn's answer at the top level, app/worker.py top_ctx, D1), to the chat
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
                self.cards.update_record_panel(ctx, chat, eid, event, record, drive_artifact_panel(record, ctx.topic_id))

                def retry():
                    ledger = self.upload_artifacts(ctx, record)
                    # A second update under the click's key would be rejected as a replay.
                    try:
                        self.cards.update_record_panel(ctx, chat, eid + "-uploaded", event, record,
                                                 drive_artifact_panel(record, ctx.topic_id, ledger))
                    except ValueError as exc:
                        self.runtime.log(f"artifact panel not refreshed after upload: {exc}")
                        self.messenger.reply(ctx, chat, "产物已处理，但原卡片没能刷新；可再点一次“重新上传”刷新，"
                                             "已上传的文件不会重复上传。", self.voice.key("artifact-refresh", eid), voice=self.voice)
                self.in_background(retry)
                return True
            if raw.get("kind") == "bridge_artifact_page":
                panel = artifact_panel(record, ctx.topic_id, int(raw.get("page", 0)))
                panel['expanded'] = True
                self.cards.update_record_panel(ctx, chat, eid, event, record, panel)
                return True
            text = self.send_artifact(ctx, record, str(raw.get("artifact_key") or ""))
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            text = "文件发送暂未完成，请稍后重试。" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        if text:
            self.messenger.reply(ctx, chat, text, self.voice.key("artifact-result", eid), voice=self.voice)
        return True
