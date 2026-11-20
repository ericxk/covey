"""The bot's cloud space (Drive) through lark-cli: one call, one upload, and a file uploaded for a chat message.

A failed call raises DriveError with the reason the task card shows; the folder layout and what is uploaded
are app/artifacts.py's. The private folders files are copied into first are core/state.py's.
"""

from __future__ import annotations

import json
import subprocess

from covey.core.text import safe_text
from covey.feishu.transport import cli_result, lark_argv, set_failure

# Feishu holds at most 1500 children in a folder and answers this code when one more is created in a full one.
FOLDER_LIMIT = 1500
FOLDER_FULL = 1062507


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


def upload_chat_file(profile, name, staged):
    """The file at ``staged``, uploaded as ``name`` for the bot to send in a chat (``im files create``, lark-cli run in
    its folder): the file_key Feishu gives it, which a message then names. A failure is recorded for the caller
    (transport.set_failure) and raises ValueError with the reason to show."""
    payload, failure = cli_result(
        lark_argv(profile, "im", "files", "create", "--as", "bot", "--data",
                  json.dumps({"file_name": name, "file_type": "stream"}),
                  "--file", f"file={staged.name}", "--format", "json"),
        timeout=60, cwd=staged.parent, runner=subprocess.run)
    if failure:
        set_failure(failure)
        raise ValueError("文件上传失败，请稍后重试")
    data = payload.get("data", payload)
    file_key = data.get("file_key")
    if not file_key:
        raise ValueError("文件上传未返回文件标识，请重试")
    return file_key
