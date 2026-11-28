"""网页工作台同步的数据 (docs/web.md, COVEY_WEB=1): the config, the four kinds of items pushed to the web page, and the
ledger of the operations fetched from it. Pure functions and one small file; no request is made here (app/web_sync.py asks, app/web_ops.py
does what the page asked for).

- ``COVEY_WEB_CONFIG`` names a JSON {base_url, key_file, push_seconds, poll_seconds, page_url}: the key is read from ``key_file`` alone and
  never stored in the config object's repr, a log line or a record. ``page_url`` (optional): the address the owner opens the page at,
  for the cards' 打开网页 (``page_url``); without it the cards are as they were.
- Items: ``project``, ``topic``, ``turn``, ``question``, ``collab`` (COVEY_COLLAB=1 only: one collaboration, app/web_collect.py) (the contract's fields, in Chinese words for states) and one ``settings`` (the 设置
  card's tabs, app/web_settings.py); ``rev`` is the hash of an item's data, so only what changed is pushed. A batch is at most ``BATCH_ITEMS`` items and,
  to a page that says nothing of its limits, ``BATCH_BYTES`` bytes; a page of data contract v44 says them (``limits`` in its answers, ``limits_of``)
  and gets larger batches and images (``batch_bytes``, ``image_bytes``).
- The ledger (``<state>/web/inbox.json``): an operation's op_id and seq are written before it is done, so a restart does not do it
  twice; an operation found "received" without a result is not done again (rule 6 of AGENTS.md: a write is not replayed).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from covey.core import env as cenv
from covey.core.state import read_object, write_json

ENV = "COVEY_WEB"  # ``1``: on; anything else, unset too: off.
CONFIG_ENV = "COVEY_WEB_CONFIG"  # The JSON file of the config.
FOLDER = "web"
LEDGER = "inbox.json"
PUSH_SECONDS, POLL_SECONDS, FULL_SECONDS = 1.0, 0.5, 60.0
BATCH_ITEMS, BATCH_BYTES = 500, 1_000_000  # Bytes as sent (UTF-8) to a page without ``limits`` (before v44): it refuses a body over 1 MiB.
# v44: a page whose ``limits.body_bytes`` (its BODY_SIZE_LIMIT) is BIG_BODY or more gets batches of BATCH_SHARE of it, BATCH_MOST at
# most (the hosting platform refuses a request over 12 MB; the page's default is 8 MB): fewer requests to a gateway that allows about 10 a second.
BIG_BODY, BATCH_SHARE, BATCH_MOST = 2_000_000, 0.6, 5_000_000
# One encoded image (POST /sync/image): IMAGE_PART to a page without limits (its base64 stays under 1 MiB); to one whose
# ``limits.image_bytes`` is over IMAGE_OLD (what a page before v44 takes, 760 KiB) that less IMAGE_MARGIN, IMAGE_MOST at most.
IMAGE_PART, IMAGE_OLD, IMAGE_MARGIN, IMAGE_MOST = 700_000, 760 * 1024, 65_536, 3_000_000
BODY_BYTES = 64 * 1024
CUT_NOTE = "\n\n（内容太长，已截断）"
LEDGER_KEPT = 2000
KINDS = ("say", "answer", "stop", "reopen", "seen", "forget", "tidy", "settings", "dismiss", "tidy_decide", "launch", "switch",
         "recover", "login", "project_finish", "project_stop", "memory_undo", "revisit_cancel", "memory_edit", "upgrade",
         "quota_refresh")
TYPES = ("project", "topic", "turn", "question", "settings", "collab")
SETTINGS_KEY = "settings"  # The one ``settings`` item's key (app/web_settings.py).
USAGE_KEY = "usage"  # The ``settings`` item of 本周用量 as the bot menu's card counts it (app/web_settings.py usage).
TIDY_KEY = "tidy"  # The ``settings`` item of 整理专项's suggestions (app/project_tidy.py web_view, data contract v16).
ASKS_KEY = "asks"  # The ``settings`` item of 要你处理 as every card counts it (core/board_read.py Asks.counts, data contract v34).
MEMORY_KEY = "memory"  # The ``settings`` item of the owner's main memory, the page's 记忆 tab (app/web_memory.py view, data contract v37).
UPGRADE_KEY = "upgrade"  # The ``settings`` item of 版本与更新, with an upgrader set up only (app/self_upgrade.py view, data contract v39).
QUOTA_KEY = "quota"  # The ``settings`` item of 账号与额度, with a quota source only (app/web_quota.py view, data contract v42).
SETTINGS_KEYS = (SETTINGS_KEY, USAGE_KEY, TIDY_KEY, ASKS_KEY, MEMORY_KEY, UPGRADE_KEY, QUOTA_KEY)  # web/shared/api.interface.ts SETTINGS_KEYS.
# Data contract v43: the memory center's keys (web/shared/api.interface.ts MEMORY_KEY_RE, tests/golden/web_memory_v43.json), pushed by
# app/web_sync.py push_memory_center (app/web_memory.py Center).
MEMORY_KEY_RE = re.compile(r"^memory:(index|global|previews|topics|p:[0-9a-z]{6,32}|import:\d{1,3})$")
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
    # ``direct``: true when the page is reached without the environment's https proxy (2026-11-27 T876: the dev machine's proxy
    # carried 0.1 MB/s up to the page, the direct route 4-7 MB/s). Without it the proxy is used as the environment says.
    direct: bool = False


def page_url_ok(url) -> bool:
    """Whether ``url`` may be the config's ``page_url``: http:// or https://, no whitespace."""
    url = str(url or "")
    return url.startswith(("https://", "http://")) and not any(ch.isspace() for ch in url)


def page_url(path=None) -> str:
    """The address the owner opens the web page at (the config's ``page_url``, https:// or http://), with COVEY_WEB=1; '' when
    the switch is off, the config does not say it or cannot be read (the cards then have no 打开网页). Reads no key; never raises."""
    if not enabled():
        return ""
    path = str(path or cenv.get(CONFIG_ENV, "") or "").strip()
    try:
        url = str(read_object(Path(path)).get("page_url") or "").strip() if path else ""
    except (OSError, ValueError, TypeError):
        return ""
    return url if page_url_ok(url) else ""


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")  # urlsplit(...).hostname has no brackets.


def base_url_ok(url) -> bool:
    """Whether ``url`` may be the config's base_url: https://, or http:// to this machine's loopback alone (a self-hosted page on the
    same machine, docs/web.md 自托管); the key never goes over plain http to anywhere else."""
    url = str(url or "")
    if url.startswith("https://"):
        return True
    if not url.startswith("http://") or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        return parts.hostname in LOOPBACK_HOSTS and parts.username is None and (parts.port is None or parts.port > 0)
    except ValueError:
        return False


def _seconds(value, default) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.2 <= value <= 3600 else default


def load_config(path=None) -> Config:
    """The config ``COVEY_WEB_CONFIG`` (or ``path``) names; ValueError says what is wrong (never the key itself)."""
    path = str(path or cenv.get(CONFIG_ENV, "") or "").strip()
    if not path:
        raise ValueError("COVEY_WEB_CONFIG 没有设置")
    data = read_object(Path(path))
    base = str(data.get("base_url") or "").strip().rstrip("/")
    if not base_url_ok(base):
        raise ValueError("配置里的 base_url 要以 https:// 开头（自托管在本机时可以是 http://127.0.0.1:端口）")
    key_file = str(data.get("key_file") or "").strip()
    try:
        key = Path(key_file).read_text().strip() if key_file else ""
    except OSError as exc:
        raise ValueError(f"key_file 读不了：{type(exc).__name__}") from exc
    if not key:
        raise ValueError("key_file 是空的或没有写")
    return Config(base, key, _seconds(data.get("push_seconds"), PUSH_SECONDS), _seconds(data.get("poll_seconds"), POLL_SECONDS),
                  data.get("direct") is True)


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


def batches(items, limit=BATCH_BYTES) -> list:
    """``items`` in batches of at most BATCH_ITEMS items and ``limit`` bytes as sent, UTF-8 with the separator (a Chinese character
    is three; one item over the size is a batch of its own)."""
    out, current, size = [], [], 0
    for each in items:
        weight = len(json.dumps(each, ensure_ascii=False).encode()) + 2
        if current and (len(current) >= BATCH_ITEMS or size + weight > limit):
            out.append(current)
            current, size = [], 0
        current.append(each)
        size += weight
    if current:
        out.append(current)
    return out


def _count(value) -> int:
    """``value`` as a count of bytes: a positive int (not a bool), else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def limits_of(answer):
    """The ``limits`` a page of data contract v44 put in an answer (``/sync/upsert``, ``/sync/images/have``): {body_bytes, image_bytes,
    ...} with a positive ``body_bytes``; None when there are none (an older page) or they are not of that shape."""
    limits = answer.get("limits") if isinstance(answer, dict) else None
    if not isinstance(limits, dict) or not _count(limits.get("body_bytes")):
        return None
    return dict(limits)


# What one request may carry is also bounded by how fast the way to the page carries it (``rate``, bytes a second as measured by
# app/web_push_client.py): a batch about BATCH_TARGET_SECONDS, an image about IMAGE_TARGET_SECONDS of it, never under the old sizes'
# share below. A 5 MB batch through a proxy that carries 0.1 MB/s took 52 s, and the quick look took it for stuck (2026-11-27 T876).
BATCH_TARGET_SECONDS, BATCH_FLOOR = 4.0, 250_000
IMAGE_TARGET_SECONDS = 8.0


def batch_bytes(limits, rate=None) -> int:
    """The bytes of one batch to a page that said ``limits`` (``limits_of``; None: BATCH_BYTES, as before v44), over a way that
    carries ``rate`` bytes a second (None: not measured yet, at most BATCH_BYTES)."""
    body = _count((limits or {}).get("body_bytes"))
    most = min(BATCH_MOST, int(body * BATCH_SHARE)) if body >= BIG_BODY else BATCH_BYTES
    if not rate:
        return min(most, BATCH_BYTES)
    return max(min(BATCH_FLOOR, most), min(most, int(rate * BATCH_TARGET_SECONDS)))


def image_bytes(limits, rate=None) -> int:
    """The bytes of one encoded image to a page that said ``limits`` (None: IMAGE_PART, as before v44), over a way that carries
    ``rate`` bytes a second (None: not measured yet, IMAGE_PART)."""
    image = _count((limits or {}).get("image_bytes"))
    most = min(IMAGE_MOST, image - IMAGE_MARGIN) if image > IMAGE_OLD else IMAGE_PART
    if not rate:
        return min(most, IMAGE_PART)
    return max(min(IMAGE_PART, most), min(most, int(rate * IMAGE_TARGET_SECONDS)))


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
