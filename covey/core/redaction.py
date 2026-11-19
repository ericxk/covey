"""Sensitive-term redaction: the owner's term list, which topics use it and what each turn replaced.

A topic chooses redaction on its launch card and keeps the choice for its whole life
(``topic.json``'s ``redaction``): turning it on midway would rewrite history the vendor has
already seen, turning it off would send the terms out. Its Claude Code turns then reach the
model vendor through a loopback relay that replaces each listed term with its stand-in before a
request leaves the machine, and turns the stand-ins in replies and tool calls back into the
terms (backends/claude/redaction.py; the matching is core/redactor.py). Adapted from
claude-partner's redaction gateway (suyue.rd/claude-partner, feat/redaction-gateway).

The list lives in <state>/redaction/config.json (0600). The 设置 card's 脱敏 tab and the web page's 设置 change its terms
many at a time (``edit_terms``, ``put_back``) and its switches one at a time (``update_config``), the optional web
dashboard writes it whole (app/dashboard.py); every turn reads it when it starts, so an edit applies from the next
turn::

    {"default_on": false,   # the launch card's initial choice before 2026-11-19: the 新话题 page's 脱敏 row now
                            # (core/migrate.py redaction-default); kept for an older release
     "ignore_case": true,   # a term matches in any case; each spelling gets a stand-in of its own
     "restore": true,       # stand-ins in replies and tool calls turn back into the terms
     "upstream": "",        # where the Claude account's requests go; "" finds it each turn
     "terms": [{"term": "…"}]}

Every term goes out as its automatic stand-in (``auto_stand_in``, RX and 8 hex digits of its hash): the owner names
none (2026-11-14 on the card; 2026-11-19 T566 everywhere). A row's ``stand_in``, which an older version let the owner
set, is ignored when the list is read and left out when it is written (core/migrate.py redaction-stand-in rewrote the
lists that had one). A term that starts or ends with an ASCII letter or digit matches only where no ASCII letter or
digit adjoins it there, unless its row has ``"anywhere": true``: an older version's flag, kept (dropping it would let
the term out where it sits inside a word) and never set anew (``update_config``).

A card never shows a term (docs/usage.md 敏感词脱敏): what is in the owner's chats a model may read, and send on to
its vendor. Cards name a term by ``masked`` and say why one was refused with ``Refused``, whose words name none.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
            raise ValueError("词表的每一项必须有 term")
        term = _text(row.get("term"), "原词", required=True)
        anywhere = row.get("anywhere", False)
        if not isinstance(anywhere, bool):
            raise ValueError("anywhere 必须是 true 或 false")
        if fold(term) in seen:
            raise ValueError(f"词表里「{term}」重复了")
        seen.add(fold(term))
        terms.append({"term": term, "anywhere": anywhere})
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


def saved_rows(config) -> list:
    """The rows of ``config`` as config.json holds them: the term, and ``anywhere`` only where an older version set it."""
    return [{"term": row["term"], **({"anywhere": True} if row.get("anywhere") else {})} for row in config["terms"]]


def save_config(state_dir, raw) -> dict:
    config = normalize(raw)
    write_json(config_path(state_dir), {**config, "terms": saved_rows(config), "updated_at": utc_now_iso()},
               ensure_ascii=False, indent=2)
    return config


CUSTOM_REFUSED = "任意位置匹配是旧版本的设置，不能新设"


def custom(config) -> set:
    """The terms of ``config`` whose row has ``anywhere``: what an older version let the owner set and a saved list
    may still hold (they keep working; only a new one is refused, ``update_config``)."""
    return {row["term"] for row in config["terms"] if row.get("anywhere")}


def update_config(state_dir, change, *, allow=()):
    """The saved config read, changed by ``change(config)`` (a copy, edited in place) and saved, under EDIT_LOCK;
    what ``change`` returns. Refused, from ``change`` (nothing saved) or from the save; ValueError, which may name a
    term, when the saved list cannot be read; OSError when it cannot be written. A change that gives a term
    ``anywhere`` it did not have is Refused, except a row of ``allow`` (a deleted row put back whole)."""
    with EDIT_LOCK:
        config = load_config(state_dir)
        config["terms"] = [dict(row) for row in config["terms"]]
        permitted = custom(config) | {row["term"] for row in allow if row.get("anywhere")}
        result = change(config)
        if not custom(config) <= permitted:
            raise Refused(CUSTOM_REFUSED)
        try:
            save_config(state_dir, config)
        except ValueError:
            raise Refused("没有保存：词表没通过检查") from None
    return result


def term_lines(text) -> list:
    """A box of terms (the card's, a pasted list): [(line number, line)], a stripped non-empty line each, a line a term
    (a term may hold spaces). A list or tuple is taken as its lines already split."""
    lines = text if isinstance(text, (list, tuple)) else str(text or "").splitlines()
    return [(number, str(line).strip()) for number, line in enumerate(lines, 1) if str(line).strip()]


ALREADY = "词表里已经有这个词"
ARROW = "替身词自动生成，不用写 =>"


def term_problem(terms, term, *, ignore_case) -> str:
    """Why ``term`` (stripped) cannot be added to the list ``terms``, as ``normalize`` would refuse it, in words that name
    no term (ALREADY when it is in the list in another case or this one); '' when it can."""
    fold = str.casefold if ignore_case else (lambda s: s)
    if len(term) < 2:
        return "原词至少要 2 个字"
    if len(term) > MAX_LENGTH:
        return f"原词不能超过 {MAX_LENGTH} 个字"
    if _CONTROL.search(term):
        return "不能含控制字符"
    if any(fold(row["term"]) == fold(term) for row in terms):
        return ALREADY
    if len(terms) >= MAX_TERMS:
        return f"词表最多 {MAX_TERMS} 个词"
    return ""


@dataclass(frozen=True)
class TermEdit:
    """What ``edit_terms`` did. A number is a term's line in its box (``term_lines``: a list's position from 1).

    ``added``: the terms added, in their order. ``removed``: (row, index) of each row taken out, ``index`` its place in
    the list before the edit, ascending (``put_back`` takes them as they are). ``already``: numbers of ``add`` already in
    the list. ``missing``: numbers of ``remove`` not in it. ``skipped``: (number, why) of ``add`` the list cannot take,
    ``why`` naming no term (``term_problem``, ARROW). ``changed``: whether the list was saved."""
    added: tuple = ()
    removed: tuple = ()
    already: tuple = ()
    missing: tuple = ()
    skipped: tuple = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


class _Unchanged(Exception):
    def __init__(self, edit):
        super().__init__()
        self.edit = edit


def edit_terms(state_dir, *, add=(), remove=(), whole=False) -> TermEdit:
    """The terms ``remove`` taken out of the saved list and ``add`` put in, in one read and save under EDIT_LOCK; each a
    box's text (a line a term) or a list of terms as written. Removes go first, by the whole term (in any case with
    ``ignore_case``), so a term in both is written anew; an added term goes to the end with its automatic stand-in. A
    term ``add`` names twice is ALREADY the second time. ``whole``: Refused, nothing saved, when an added term is
    ``skipped``. Nothing is saved when nothing changed. Raises as ``update_config``."""
    adds, removes = term_lines(add), term_lines(remove)

    def change(config):
        fold = str.casefold if config["ignore_case"] else (lambda s: s)
        wanted = {}
        for number, term in removes:
            wanted.setdefault(fold(term), []).append(number)
        removed = tuple((row, index) for index, row in enumerate(config["terms"]) if fold(row["term"]) in wanted)
        found = {fold(row["term"]) for row, _ in removed}
        config["terms"] = [row for row in config["terms"] if fold(row["term"]) not in found]
        missing = tuple(sorted(n for key, numbers in wanted.items() if key not in found for n in numbers))
        added, already, skipped = [], [], []
        for number, term in adds:
            why = ARROW if "=>" in term else term_problem(config["terms"], term, ignore_case=config["ignore_case"])
            if why == ALREADY:
                already.append(number)
            elif why:
                skipped.append((number, why))
            else:
                config["terms"].append({"term": term, "anywhere": False})
                added.append(term)
        if whole and skipped:
            raise Refused("没有保存：" + said_lines(skipped))
        edit = TermEdit(tuple(added), removed, tuple(already), missing, tuple(skipped))
        if not edit.changed:
            raise _Unchanged(edit)
        return edit

    try:
        return update_config(state_dir, change)
    except _Unchanged as unchanged:
        return unchanged.edit


def put_back(state_dir, removed) -> tuple:
    """The rows ``removed`` (``TermEdit.removed``) back where they were, ``anywhere`` and all: (rows put back, [(row, why)]
    of those the list cannot take now, ``why`` naming no term). Raises as ``update_config``; nothing saved when none
    goes back."""
    rows = [(dict(row), index) for row, index in sorted(removed, key=lambda pair: pair[1])]

    def change(config):
        back, left = [], []
        for row, index in rows:
            why = term_problem(config["terms"], row["term"], ignore_case=config["ignore_case"])
            if why:
                left.append((row, why))
                continue
            config["terms"].insert(min(index, len(config["terms"])), row)
            back.append(row)
        if not back:
            raise _Unchanged((back, left))
        return back, left

    try:
        return update_config(state_dir, change, allow=[row for row, _ in rows])
    except _Unchanged as unchanged:
        return unchanged.edit


def said_lines(skipped) -> str:
    """(number, why) pairs by reason, in words that name no term: 第 2、5 行（原词至少要 2 个字）."""
    lines = {}
    for number, why in skipped:
        lines.setdefault(why, []).append(number)
    return "；".join(f"第 {'、'.join(map(str, numbers[:6]))}{' 等' if len(numbers) > 6 else ''} 行（{why}）"
                    for why, numbers in lines.items())


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
