"""Sensitive-term redaction: the owner's term list, which topics use it and what each turn replaced.

A topic chooses redaction on its launch card and keeps the choice for its whole life
(``topic.json``'s ``redaction``): turning it on midway would rewrite history the vendor has
already seen, turning it off would send the terms out. Its Claude Code turns then reach the
model vendor through a loopback relay that replaces each listed term with its stand-in before a
request leaves the machine, and turns the stand-ins in replies and tool calls back into the
terms (backends/claude/redaction.py; the matching is core/redactor.py). Adapted from
claude-partner's redaction gateway (suyue.rd/claude-partner, feat/redaction-gateway).

The list lives in <state>/redaction/config.json (0600). The 设置 card's 脱敏 tab edits it one change at a time
(app/redaction_panel.py, ``update_config``) and the optional web dashboard as a whole (app/dashboard.py); every
turn reads it when it starts, so an edit applies from the next turn::

    {"default_on": false,   # the launch card's initial choice before 2026-11-19: the 新话题 page's 脱敏 row now
                            # (core/migrate.py redaction-default); kept for an older release
     "ignore_case": true,   # a term matches in any case; each spelling gets a stand-in of its own
     "restore": true,       # stand-ins in replies and tool calls turn back into the terms
     "upstream": "",        # where the Claude account's requests go; "" finds it each turn
     "terms": [{"term": "…", "stand_in": "…", "anywhere": false}]}

An empty ``stand_in`` is an automatic one (``auto_stand_in``). A stand-in shared by several terms
only replaces: a reply's copy of it cannot say which term it stood for. ``anywhere``: a term that
starts or ends with an ASCII letter or digit matches only where no ASCII letter or digit adjoins
it there, unless this is set.

A card never shows a term (docs/usage.md 敏感词脱敏): what is in the owner's chats a model may read, and send on to
its vendor. Cards name a term by ``masked`` and say why one was refused with ``Refused``, whose words name none.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from covey.core.state import read_json, utc_now_iso, write_json
from covey.core.logs import append_private_text, rotate_live

TOPIC_KEY = "redaction"  # topic.json: True once the launch card turned it on; absent otherwise.
DEFAULTS = {"default_on": False, "ignore_case": True, "restore": True, "upstream": ""}
MAX_TERMS = 1000
MAX_LENGTH = 200
AUTO_PREFIX = "RX"  # auto_stand_in: RX and 8 hex digits, a word in code, paths and prose alike.
STATS_LIMIT = 5 * 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UPSTREAM = re.compile(r"https?://[^\s/?#@]+(/[^\s?#]*)?")
UPSTREAM_WORDS = "上游地址必须是 http:// 或 https:// 开头的地址，不能带账号、查询参数或空格"
EDIT_LOCK = threading.Lock()  # update_config: one change of the saved list at a time.


class Refused(ValueError):
    """Why a change of the list was not made, in words that name no term: a card may show them."""


def masked(term: str) -> str:
    """How a card names ``term``: its first character and its length, never more (内··（4 字）)."""
    return f"{term[:1]}··（{len(term)} 字）"


def upstream_problem(value: str) -> str:
    """Why ``value`` (stripped) cannot be the upstream address; '' when it can (empty: found each turn)."""
    return "" if not value or _UPSTREAM.fullmatch(value) else UPSTREAM_WORDS


def config_path(state_dir) -> Path:
    return Path(state_dir) / "redaction" / "config.json"


def stats_path(state_dir) -> Path:
    return Path(state_dir) / "redaction" / "turns.jsonl"


def auto_stand_in(spelling: str) -> str:
    """The automatic stand-in of one exact spelling: stable, and never a word of its own."""
    return AUTO_PREFIX + hashlib.sha1(spelling.encode("utf-8")).hexdigest()[:8].upper()


def _text(value, what, *, required):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{what}必须是文字")
    value = value.strip()
    if required and len(value) < 2:
        raise ValueError(f"{what}至少要 2 个字")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"{what}不能超过 {MAX_LENGTH} 个字：{value[:20]}…")
    if _CONTROL.search(value):
        raise ValueError(f"{what}不能含换行或控制字符：{value[:20]}")
    return value


def normalize(raw) -> dict:
    """The config with every key, checked; ValueError (in words for the dashboard) when it is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("敏感词配置必须是一个对象")
    config = dict(DEFAULTS)
    for key in ("default_on", "ignore_case", "restore"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"{key} 必须是 true 或 false")
            config[key] = raw[key]
    upstream = raw.get("upstream") or ""
    if not isinstance(upstream, str) or upstream_problem(upstream.strip()):
        raise ValueError(UPSTREAM_WORDS)
    config["upstream"] = upstream.strip().rstrip("/")
    rows = raw.get("terms") or []
    if not isinstance(rows, list):
        raise ValueError("词表必须是列表")
    if len(rows) > MAX_TERMS:
        raise ValueError(f"词表最多 {MAX_TERMS} 个词")
    fold = str.casefold if config["ignore_case"] else (lambda s: s)
    terms, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("词表的每一项必须有 term 和 stand_in")
        term = _text(row.get("term"), "原词", required=True)
        stand_in = _text(row.get("stand_in"), "替身词", required=False)
        anywhere = row.get("anywhere", False)
        if not isinstance(anywhere, bool):
            raise ValueError("anywhere 必须是 true 或 false")
        if fold(term) in seen:
            raise ValueError(f"词表里「{term}」重复了")
        seen.add(fold(term))
        terms.append({"term": term, "stand_in": stand_in, "anywhere": anywhere})
    # A stand-in goes out as it is: one holding a term would send that term.
    for row in terms:
        for other in terms:
            if row["stand_in"] and fold(other["term"]) in fold(row["stand_in"]):
                raise ValueError(f"替身词「{row['stand_in']}」含有词表里的「{other['term']}」，会把它发出去")
    config["terms"] = terms
    return config


def load_config(state_dir) -> dict:
    """The saved config, or the defaults when none was saved. ValueError when it cannot be read or
    is wrong: a redacted topic's turn then fails rather than go out unredacted."""
    try:
        raw = read_json(config_path(state_dir))
    except FileNotFoundError:
        return normalize({})
    except (OSError, ValueError) as exc:
        raise ValueError(f"敏感词配置读取失败（{type(exc).__name__}）") from None
    return normalize(raw)


def save_config(state_dir, raw) -> dict:
    config = normalize(raw)
    write_json(config_path(state_dir), {**config, "updated_at": utc_now_iso()}, ensure_ascii=False, indent=2)
    return config


CUSTOM_REFUSED = "替身词由系统自动生成，不能指定"


def custom(config) -> dict:
    """The rows of ``config`` that carry a stand-in or ``anywhere`` of their own, by term: what an older version let
    the owner set and a saved list may still hold (they keep working; only a new one is refused, ``update_config``)."""
    return {row["term"]: (row["stand_in"], row["anywhere"]) for row in config["terms"]
            if row["stand_in"] or row["anywhere"]}


def update_config(state_dir, change, *, allow=()):
    """The saved config read, changed by ``change(config)`` (a copy, edited in place) and saved, under EDIT_LOCK;
    what ``change`` returns. Refused, from ``change`` (nothing saved) or from the save; ValueError, which may name a
    term, when the saved list cannot be read; OSError when it cannot be written. The stand-ins are the automatic
    ones (2026-11-14, the owner's decision): a change that gives a term a stand-in or ``anywhere`` it did not have is
    Refused, except a row of ``allow`` (a deleted row put back whole)."""
    with EDIT_LOCK:
        config = load_config(state_dir)
        config["terms"] = [dict(row) for row in config["terms"]]
        permitted = {**custom(config), **{row["term"]: (row["stand_in"], row["anywhere"]) for row in allow}}
        result = change(config)
        if any(permitted.get(term) != own for term, own in custom(config).items()):
            raise Refused(CUSTOM_REFUSED)
        try:
            save_config(state_dir, config)
        except ValueError:
            raise Refused("没有保存：词表没通过检查") from None
    return result


def term_lines(text) -> list:
    """The 加入词表 box: [(line number, line)], a stripped non-empty line each, a line a term."""
    return [(number, line.strip()) for number, line in enumerate(str(text or "").splitlines(), 1) if line.strip()]


def row_problem(terms, term, stand_in, *, ignore_case, skip=None) -> str:
    """Why ``term`` with ``stand_in`` (both stripped) cannot be a row of the list ``terms`` (``skip``: the index of
    the row it replaces), as ``normalize`` would refuse it, in words that name no term; '' when it can."""
    fold = str.casefold if ignore_case else (lambda s: s)
    others = [row for i, row in enumerate(terms) if i != skip]
    if len(term) < 2:
        return "原词至少要 2 个字"
    if len(term) > MAX_LENGTH or len(stand_in) > MAX_LENGTH:
        return f"原词和替身词都不能超过 {MAX_LENGTH} 个字"
    if _CONTROL.search(term) or _CONTROL.search(stand_in):
        return "不能含控制字符"
    if skip is None and len(others) >= MAX_TERMS:
        return f"词表最多 {MAX_TERMS} 个词"
    if any(fold(row["term"]) == fold(term) for row in others):
        return "词表里已经有这个词"
    if stand_in and any(fold(t) in fold(stand_in) for t in [term, *(row["term"] for row in others)]):
        return "替身词里含有词表里的词，会把它发出去"
    if any(row["stand_in"] and fold(term) in fold(row["stand_in"]) for row in others):
        return "它在别的词的替身词里，替身会把它发出去"
    return ""


def default_on(state_dir) -> bool:
    """The launch card's initial choice before 2026-11-19 (an older release still reads it); off when the config cannot
    be read. Only core/migrate.py redaction-default reads it now: the 新话题 page's 脱敏 row took its place."""
    try:
        return load_config(state_dir)["default_on"]
    except ValueError:
        return False


def topic_redaction(state_dir, topic_id) -> bool:
    """Whether the topic chose redaction. A job outside a topic, or a topic not yet written, did not;
    a topic file that cannot be read raises ValueError, so the turn is not sent."""
    if not topic_id:
        return False
    try:
        data = read_json(Path(state_dir) / "topics" / topic_id / "topic.json")
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise ValueError("本话题的设置读取失败，无法确认敏感词脱敏，本轮未发送") from None
    return isinstance(data, dict) and data.get(TOPIC_KEY) is True


def record_turn(state_dir, entry: dict) -> None:
    """One turn's counts, appended to <state>/redaction/turns.jsonl (0600); best effort."""
    path = stats_path(state_dir)
    try:
        rotate_live(path, max_bytes=STATS_LIMIT, keep=2)
        append_private_text(path, json.dumps({"ts": round(time.time(), 3), **entry}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_turns(state_dir, limit=500) -> list:
    """The latest ``limit`` turns recorded, oldest first; lines that do not parse are skipped."""
    try:
        lines = stats_path(state_dir).read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
