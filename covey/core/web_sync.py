"""网页工作台同步的数据 (docs/butler.md 网页工作台, COVEY_WEB=1): the config, the four kinds of items pushed to the web page, and the
ledger of the operations fetched from it. Pure functions and one small file; no request is made here (app/web_sync.py asks, app/web_ops.py
does what the page asked for).

- ``COVEY_WEB_CONFIG`` names a JSON {base_url, key_file, push_seconds, poll_seconds}: the key is read from ``key_file`` alone and never
  stored in the config object's repr, a log line or a record.
- Items: ``project``, ``topic``, ``turn``, ``question`` (the contract's fields, in Chinese words for states) and one ``settings`` (the 设置
  card's tabs, app/web_settings.py); ``rev`` is the hash of an item's data, so only what changed is pushed. A batch is at most ``BATCH_ITEMS`` items or ``BATCH_BYTES`` bytes.
- The ledger (``<state>/web/inbox.json``): an operation's op_id and seq are written before it is done, so a restart does not do it
  twice; an operation found "received" without a result is not done again (rule 6 of AGENTS.md: a write is not replayed).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from pathlib import Path

from covey.core import env as cenv
from covey.core.state import read_object, write_json

ENV = "COVEY_WEB"  # ``1``: on; anything else, unset too: off.
CONFIG_ENV = "COVEY_WEB_CONFIG"  # The JSON file of the config.
FOLDER = "web"
LEDGER = "inbox.json"
PUSH_SECONDS, POLL_SECONDS, FULL_SECONDS = 1.0, 1.0, 60.0
BATCH_ITEMS, BATCH_BYTES = 500, 1_000_000  # Bytes as sent (UTF-8): the page's body parser refuses a request over 1 MiB.
BODY_BYTES = 64 * 1024
CUT_NOTE = "\n\n（内容太长，已截断）"
LEDGER_KEPT = 2000
KINDS = ("say", "answer", "stop", "reopen", "seen", "forget", "tidy", "settings", "dismiss", "launch")
TYPES = ("project", "topic", "turn", "question", "settings")
SETTINGS_KEY = "settings"  # The one ``settings`` item's key (app/web_settings.py).
USAGE_KEY = "usage"  # The ``settings`` item of 本周用量 as the bot menu's card counts it (app/web_settings.py usage).
PREFIX = "web-"  # The message id of the owner's words from the page: ``web-<op_id>``. Feishu has no such message.


def is_web_message(message_id) -> bool:
    """Whether ``message_id`` is the id of words that came from the page (``PREFIX``): nothing is quoted, replied to or reacted on."""
    return isinstance(message_id, str) and message_id.startswith(PREFIX)


def enabled() -> bool:
    """Whether COVEY_WEB=1 (read on every call)."""
    return str(cenv.get(ENV, "")).strip() == "1"


@dataclasses.dataclass(frozen=True)
class Config:
    """What the config file says. ``key`` is the key file's text (not shown by repr)."""
    base_url: str
    key: str = dataclasses.field(repr=False)
    push_seconds: float = PUSH_SECONDS
    poll_seconds: float = POLL_SECONDS


def _seconds(value, default) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.2 <= value <= 3600 else default


def load_config(path=None) -> Config:
    """The config ``COVEY_WEB_CONFIG`` (or ``path``) names; ValueError says what is wrong (never the key itself)."""
    path = str(path or cenv.get(CONFIG_ENV, "") or "").strip()
    if not path:
        raise ValueError("COVEY_WEB_CONFIG 没有设置")
    data = read_object(Path(path))
    base = str(data.get("base_url") or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("配置里的 base_url 要以 https:// 开头")
    key_file = str(data.get("key_file") or "").strip()
    try:
        key = Path(key_file).read_text().strip() if key_file else ""
    except OSError as exc:
        raise ValueError(f"key_file 读不了：{type(exc).__name__}") from exc
    if not key:
        raise ValueError("key_file 是空的或没有写")
    return Config(base, key, _seconds(data.get("push_seconds"), PUSH_SECONDS), _seconds(data.get("poll_seconds"), POLL_SECONDS))


# -- items ----------------------------------------------------------------------------------------------------------------------

def rev(data) -> str:
    """The hash of an item's data."""
    return hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def item(kind, key, data) -> dict:
    return {"type": kind, "key": str(key), "rev": rev(data), "data": data}


def clip(text) -> str:
    """``text`` cut to BODY_BYTES (a note says so)."""
    text = str(text or "")
    raw = text.encode()
    if len(raw) <= BODY_BYTES:
        return text
    return raw[:BODY_BYTES - len(CUT_NOTE.encode())].decode(errors="ignore") + CUT_NOTE


def batches(items) -> list:
    """``items`` in batches of at most BATCH_ITEMS items and BATCH_BYTES bytes as sent, UTF-8 with the separator (a Chinese character
    is three; one item over the size is a batch of its own)."""
    out, current, size = [], [], 0
    for each in items:
        weight = len(json.dumps(each, ensure_ascii=False).encode()) + 2
        if current and (len(current) >= BATCH_ITEMS or size + weight > BATCH_BYTES):
            out.append(current)
            current, size = [], 0
        current.append(each)
        size += weight
    if current:
        out.append(current)
    return out


# -- the ledger -----------------------------------------------------------------------------------------------------------------

class Ledger:
    """<state>/web/inbox.json: {"ops": {op_id: {seq, state, message, acked}}}. One thread uses it (the inbox's); a lock keeps a test's
    second one honest. ``state``: received (taken, not done), done, failed."""

    def __init__(self, state_dir) -> None:
        self.path = Path(state_dir) / FOLDER / LEDGER
        self.lock = threading.Lock()
        data = read_object(self.path)
        self.ops = data.get("ops") if isinstance(data.get("ops"), dict) else {}

    def get(self, op_id) -> dict | None:
        with self.lock:
            return dict(self.ops[op_id]) if op_id in self.ops else None

    def take(self, op_id, seq) -> None:
        """Write the op down as received, before anything is done for it."""
        self._set(op_id, {"seq": seq, "state": "received", "message": "", "acked": False})

    def finish(self, op_id, state, message) -> None:
        with self.lock:
            row = dict(self.ops.get(op_id) or {})
        self._set(op_id, {**row, "state": state, "message": str(message)[:500]})

    def acked(self, op_ids) -> None:
        with self.lock:
            for op_id in op_ids:
                if op_id in self.ops:
                    self.ops[op_id] = {**self.ops[op_id], "acked": True}
            self._save()

    def after(self) -> int:
        """The seq to ask after: below the lowest one not acknowledged, else the highest seen."""
        with self.lock:
            rows = [row for row in self.ops.values() if isinstance(row.get("seq"), int)]
        pending = [row["seq"] for row in rows if not row.get("acked")]
        return (min(pending) - 1) if pending else max((row["seq"] for row in rows), default=0)

    def _set(self, op_id, row) -> None:
        with self.lock:
            self.ops[op_id] = row
            self._save()

    def _save(self) -> None:
        if len(self.ops) > LEDGER_KEPT:
            old = sorted((name for name, row in self.ops.items() if row.get("acked")), key=lambda name: self.ops[name].get("seq") or 0)
            for name in old[:len(self.ops) - LEDGER_KEPT]:
                self.ops.pop(name, None)
        write_json(self.path, {"ops": self.ops}, ensure_ascii=False)
