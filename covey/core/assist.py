"""辅助模型 (assist, docs/configuration.md 辅助模型): what the background model calls (分流, 整理专项, 看看, 记忆) share across
processes, in <state>/assist-state.json, and the words of their reasons. app/assist.py decides and calls; this module only
keeps the record. Standard library and core/ only: a task runner (app/switch_handoff.py) reads it too.

The record, written under its lock (``change``), never with the owner's words or a model's answer:

- ``breakers``: {harness: {code, model, at, until}}: a background call failed on the environment (BREAKS: a login, a spent
  quota, a CLI that did not start, a lost connection, a model the account cannot use); the automatic calls leave that Harness
  alone until ``until`` (BREAK_SECONDS). A call the owner asked for (✨ 整理, 看看, the butler's projects_tidy) still tries
  once, and its success clears it. A timeout never breaks.
- ``cooling``: {"harness:feature": {at, until}}: an official safety refusal (REFUSED) of a batch feature's call (整理判断,
  整理记忆, 晚间候选): that feature does not send to that Harness for COOL_SECONDS. A refusal of one item (回填 a topic, a
  real-time merge, 记忆自己长) marks that item alone.
- ``refusals``: {day, counts: {harness: n}}: the background's refusals of the day; at DAILY_REFUSALS a Harness takes no
  more background calls that day (an account refused again and again may be blocked).
- ``recent``: {feature: {harness, model, ok, code, ms, at, manual}}: each feature's latest call, for the web page's
  后台活最近一次.

A change of the 辅助模型 setting clears ``cooling`` and ``breakers`` (``clear``): the owner chose again.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

FILE = "assist-state.json"
BREAK_SECONDS = 900  # The same window as app/availability.py NOT_NOW_SECONDS.
COOL_SECONDS = 6 * 3600
DAILY_REFUSALS = 3
CACHE_SECONDS = 5.0  # How long a process trusts its own copy (``read``) before it reads the file again.
RECENT_GAP = 60.0  # A feature's ``recent`` row is written again within this only when its outcome changed.

# The reasons of one background call (core/harness.py AskFailed's, app/track_judge.py Failed's), by what they mean.
# The environment of the Harness: the next call would fail the same; the automatic calls leave it for BREAK_SECONDS.
BREAKS = frozenset({"authentication_error", "not_signed_in", "quota_exhausted", "spawn_failed", "transport_error",
                    "model_unavailable", "model_blocked"})
# Passing: a later try may well pass (过一会儿再试). Never a break.
PASSING = frozenset({"timeout", "rate_limited", "server_fault", "request_hung"})
# An official safety refusal of the content ("refused" is the word older records have).
REFUSED = frozenset({"policy_refusal", "refused"})
# Of this call alone: the content, or what the model answered.
ONE_OFF = frozenset({"too_long", "bad_output", "out_of_range", "model_error", "exit_nonzero", "root_failed", "error",
                     "low_confidence"})
# No model was asked: the setting says none, the one it names cannot take it now, or no Harness can.
NOT_ASKED = frozenset({"none", "not_allowed", "no_backend", "broken", "cooling", "refusal_gate", "not_one_shot",
                       "unusable", "not_enabled"})

# What each reason is, in the words of a card or the web page (the Harness's name goes before them where there is one).
WORDS = {
    "authentication_error": "登录失效或没有权限", "not_signed_in": "没登录", "quota_exhausted": "额度用完了",
    "spawn_failed": "程序没起来", "transport_error": "连不上模型服务（网关断开）", "model_unavailable": "所选模型在账号里用不了",
    "model_blocked": "账号被模型服务暂停使用这个模型", "timeout": "超时了", "rate_limited": "模型服务限流",
    "server_fault": "模型服务临时故障", "request_hung": "请求一直没有回应", "policy_refusal": "被官方内容安全拦了",
    "refused": "被官方内容安全拦了", "too_long": "内容太长，一次读不完", "bad_output": "回答看不懂",
    "out_of_range": "回答看不懂", "model_error": "模型返回了错误", "exit_nonzero": "进程出错退出了", "root_failed": "模型返回了错误",
    "error": "出错了", "low_confidence": "模型拿不准", "none": "设置里选了不用模型", "not_allowed": "设置里选的用不了",
    "no_backend": "没有能用的模型（Claude Code、Codex、Antigravity 都没启用）", "broken": "刚失败过，15 分钟内后台先不用它",
    "cooling": "这项后台活刚被它的官方内容安全拦过，6 小时内不再送给它", "refusal_gate": "今天后台被官方内容安全拦了好几次，今天后台先不用它",
    "not_one_shot": "不能一问一答", "unusable": "现在用不了", "not_enabled": "这台机器上没启用",
}


def kind_of(code: str) -> str:
    """'break', 'passing', 'refused', 'one_off' or 'not_asked' (and '' for ok or a reason nobody knows)."""
    for kind, codes in (("break", BREAKS), ("passing", PASSING), ("refused", REFUSED), ("one_off", ONE_OFF),
                        ("not_asked", NOT_ASKED)):
        if code in codes:
            return kind
    return ""


def words(code: str) -> str:
    return WORDS.get(code, WORDS["error"])


# A CLI's own words of a failure, for the few it says only in text (a non-zero exit with no structured error): the
# environment's first, then the content's. Matched on the words as they came, before they are cut and hidden.
_TEXT_CODES = (
    ("policy_refusal", re.compile(r"usage policy|content (?:management )?policy|safety (?:system|filter|review)|"
                                  r"(?<!connection )(?<!econn)\brefus(?:al|ed)\b|flagged as potentially violating|invalid prompt|cyber ?policy|"
                                  r"bio ?policy|stop_reason=refusal", re.I)),
    ("authentication_error", re.compile(r"\b401\b|unauthori[sz]ed|invalid api key|authentication|not logged in|"
                                        r"please (?:run )?/?login|oauth token|credential", re.I)),
    ("quota_exhausted", re.compile(r"\b402\b|quota exceeded|insufficient_quota|billing|credit balance|usage limit "
                                   r"reached|out of credits|resource_exhausted", re.I)),
    ("rate_limited", re.compile(r"\b429\b|rate.?limit|too many requests", re.I)),
    ("model_unavailable", re.compile(r"model[_ ]not[_ ]found|unknown model|model .{0,40}(?:not (?:available|exist|"
                                     r"supported)|does not exist)|no access to model", re.I)),
    ("too_long", re.compile(r"too long|context length|context window|too many tokens|maximum context", re.I)),
    ("transport_error", re.compile(r"connection (?:refused|reset|error|closed)|econnrefused|econnreset|"
                                   r"failed to connect|network error|could not resolve|socket hang up|"
                                   r"unable to connect|proxy error", re.I)),
    ("server_fault", re.compile(r"\b(?:500|502|503|504|529)\b|overloaded|internal server error|bad gateway|"
                                r"service unavailable", re.I)),
)


def code_of_text(text) -> str:
    """The reason ``text`` (a CLI's stderr, an error's message) says, or '' when it says none of _TEXT_CODES."""
    text = str(text or "")
    return next((code for code, pattern in _TEXT_CODES if pattern.search(text)), "")


# The record -----------------------------------------------------------------------------------------------------------

def path(state_dir) -> Path:
    return Path(state_dir) / FILE


def empty() -> dict:
    return {"version": 1, "breakers": {}, "cooling": {}, "refusals": {"day": "", "counts": {}}, "recent": {}}


def _shaped(data) -> dict:
    out = empty()
    if isinstance(data, dict):
        for key in ("breakers", "cooling", "recent"):
            if isinstance(data.get(key), dict):
                out[key] = {str(k): v for k, v in data[key].items() if isinstance(v, dict)}
        refusals = data.get("refusals")
        if isinstance(refusals, dict) and isinstance(refusals.get("counts"), dict):
            out["refusals"] = {"day": str(refusals.get("day") or ""),
                               "counts": {str(k): v for k, v in refusals["counts"].items() if type(v) is int}}
    return out


_cache_lock = threading.Lock()
_cache: dict = {}  # state directory -> (monotonic time read, data)


def read(state_dir, *, fresh=False) -> dict:
    """The record (a copy); this process's own copy when it read or wrote the file less than CACHE_SECONDS ago (``fresh``:
    the file now). Never raises: a missing or broken file is an empty record."""
    key = str(state_dir)
    now = time.monotonic()
    with _cache_lock:
        held = _cache.get(key)
        if held is not None and not fresh and now - held[0] < CACHE_SECONDS:
            return copy.deepcopy(held[1])
    data = _shaped(read_object(path(state_dir)))
    with _cache_lock:
        _cache[key] = (time.monotonic(), data)
    return copy.deepcopy(data)


def forget() -> None:
    """Drop this process's copies (tests)."""
    with _cache_lock:
        _cache.clear()


def change(state_dir, edit) -> dict:
    """``edit(data)`` on the file's record under its lock, written when it says so (returns something true) or always when
    it returns None; the record after. Never raises: a record that cannot be written stays as it was."""
    try:
        with resource_lock("assist-state", str(path(state_dir))):
            data = _shaped(read_object(path(state_dir)))
            wrote = edit(data)
            if wrote is None or wrote:
                write_json(path(state_dir), data, ensure_ascii=False, indent=1)
        with _cache_lock:
            _cache[str(state_dir)] = (time.monotonic(), data)
        return copy.deepcopy(data)
    except (OSError, ValueError, TypeError):
        return read(state_dir)


def day_of(now) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def broken(data, harness, now=None) -> dict | None:
    """The break of ``harness`` still on at ``now`` in ``data`` (a ``read``), or None."""
    now = time.time() if now is None else now
    row = (data.get("breakers") or {}).get(harness)
    return row if isinstance(row, dict) and float(row.get("until") or 0) > now else None


def cooling(data, harness, feature, now=None) -> dict | None:
    now = time.time() if now is None else now
    row = (data.get("cooling") or {}).get(f"{harness}:{feature}")
    return row if isinstance(row, dict) and float(row.get("until") or 0) > now else None


def refusals_today(data, harness, now=None) -> int:
    now = time.time() if now is None else now
    refusals = data.get("refusals") or {}
    if refusals.get("day") != day_of(now):
        return 0
    return int((refusals.get("counts") or {}).get(harness) or 0)


def gated(data, harness, now=None) -> bool:
    """Whether ``harness`` has had DAILY_REFUSALS background refusals today: no more background calls to it today."""
    return refusals_today(data, harness, now) >= DAILY_REFUSALS


def settle(state_dir, *, feature, harness, model="", code="", ms=0, manual=False, batch=False, now=None) -> dict:
    """One call's outcome kept (``code`` '' for an answer): its ``recent`` row, a break for an environment failure (BREAKS),
    a cooling and the day's count for a refusal (a batch feature's cooling; an item's refusal only counts), a success
    clearing its Harness's break. The record after."""
    now = time.time() if now is None else now
    kind = kind_of(code)

    def edit(data):
        changed = False
        row = {"harness": harness, "model": model, "ok": not code, "code": code, "ms": int(ms), "at": round(now),
               "manual": bool(manual)}
        old = data["recent"].get(feature) or {}
        if any(old.get(k) != row[k] for k in ("harness", "model", "ok", "code", "manual")) \
                or now - float(old.get("at") or 0) >= RECENT_GAP:
            data["recent"][feature] = row
            changed = True
        if harness and not code and harness in data["breakers"]:
            data["breakers"].pop(harness, None)
            changed = True
        if harness and kind == "break":
            data["breakers"][harness] = {"code": code, "model": model, "at": round(now), "until": round(now + BREAK_SECONDS)}
            changed = True
        if harness and kind == "refused":
            if batch:
                data["cooling"][f"{harness}:{feature}"] = {"at": round(now), "until": round(now + COOL_SECONDS)}
            day = day_of(now)
            if data["refusals"].get("day") != day:
                data["refusals"] = {"day": day, "counts": {}}
            counts = data["refusals"]["counts"]
            counts[harness] = int(counts.get(harness) or 0) + 1
            changed = True
        return changed

    return change(state_dir, edit)


def clear(state_dir) -> dict:
    """The coolings and the breaks gone (the owner changed the 辅助模型 setting); the day's refusal counts stay."""
    def edit(data):
        if not data["cooling"] and not data["breakers"]:
            return False
        data["cooling"], data["breakers"] = {}, {}
        return True
    return change(state_dir, edit)


def stamp(state_dir):
    """(mtime_ns, size) of the record, None without one: the web page's settings are pushed again when it changes
    (app/web_settings.py stamps)."""
    try:
        stat = path(state_dir).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size
