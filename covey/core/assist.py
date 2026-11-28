"""辅助模型 (assist, docs/configuration.md 辅助模型): what the background model calls (分流, 整理专项, 看看, 记忆) share across
processes, in <state>/assist-state.json, and the words of their reasons. app/assist.py decides and calls; this module only
keeps the record. Standard library and core/ only: a task runner (app/switch_handoff.py) reads it too.

The record, written under its lock (``change``), never with the owner's words or a model's answer. What a failure leaves is
kept for its lane (``Lane``: the Harness, the model the call ran on and the model gateway it went through, WAVE12 W2-D 包 D),
so one lane's trouble never stops another's (Claude Code's Haiku and its 整理 model, Codex on its own login and through a
gateway):

- ``lanes``: {"harness|model|gateway": {harness, model, gateway, broken, cooling, refusals, tried}}:
  - ``broken`` {code, model, at, until}: a background call failed on the environment (BREAKS: a login, a spent quota, a CLI
    that did not start, a lost connection, a model the account cannot use); the automatic calls leave that lane alone until
    ``until`` (BREAK_SECONDS). A timeout never breaks; a success clears it.
  - ``cooling`` {feature: {at, until}}: an official safety refusal (REFUSED) of a batch feature's call (整理判断): that
    feature does not send to that lane for COOL_SECONDS. A refusal of one item (回填 a topic, 记忆自己长) marks that item
    alone.
  - ``refusals`` {day, count}: the lane's refusals of the day; at DAILY_REFUSALS it takes no more background calls that day
    (an account refused again and again may be blocked).
  - ``tried``: when a call the owner asked for (✨ 整理, the butler's projects_tidy) last went past its break, cooling or
    gate: one such try a lane each TRY_SECONDS (``may_try``, ``take_try``), so his clicks never hammer an account that
    is refused.
- ``breakers`` {harness: …}, ``cooling`` {"harness:feature": …}, ``refusals`` {day, counts: {harness: n}}: the same, kept
  for a whole Harness by the releases before the lanes. Read still (each holds every lane of its Harness until it runs
  out), never written again; a success of a lane clears its Harness's old break.
- ``recent``: {feature: {harness, model, gateway, ok, code, ms, at, manual}}: each feature's latest call, for the web
  page's 后台活最近一次.

A change of the 辅助模型 setting clears the breaks and the coolings (``clear``): the owner chose again.
"""
from __future__ import annotations

import copy
import dataclasses
import re
import threading
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import read_for_write, read_object, write_json

FILE = "assist-state.json"
BREAK_SECONDS = 900  # The same window as app/availability.py NOT_NOW_SECONDS.
COOL_SECONDS = 6 * 3600
DAILY_REFUSALS = 3
CACHE_SECONDS = 5.0  # How long a process trusts its own copy (``read``) before it reads the file again.
RECENT_GAP = 60.0  # A feature's ``recent`` row is written again within this only when its outcome changed.
TRY_SECONDS = 60  # A call the owner asked for goes past a lane's break, cooling or gate at most once this long (``tried``).

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


@dataclasses.dataclass(frozen=True)
class Lane:
    """Where a background call goes: the Harness, the model id it runs on ('' its own default) and the model gateway it goes
    through ('' none: the Harness's own login)."""
    harness: str
    model: str = ""
    gateway: str = ""

    @property
    def key(self) -> str:
        return f"{self.harness}|{self.model}|{self.gateway}"


def lane_of(value) -> Lane:
    """``value`` as a Lane: one already, or a Harness's name (the lane of its own default model and login)."""
    return value if isinstance(value, Lane) else Lane(str(value or ""))


def empty() -> dict:
    return {"version": 2, "lanes": {}, "breakers": {}, "cooling": {}, "refusals": {"day": "", "counts": {}}, "recent": {}}


def _shaped(data) -> dict:
    out = empty()
    if isinstance(data, dict):
        for key in ("lanes", "breakers", "cooling", "recent"):
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
    it returns None; the record after. Never raises: a record that cannot be written stays as it was, as does one that is
    there and cannot be read now (core/state.py Unreadable: its breakers and counts are not started over)."""
    try:
        with resource_lock("assist-state", str(path(state_dir))):
            data = _shaped(read_for_write(path(state_dir)))
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


def _until(row, now) -> bool:
    try:
        return isinstance(row, dict) and float(row.get("until") or 0) > now
    except (TypeError, ValueError):
        return False


def _lanes(data, harness) -> list:
    """The rows of ``data``'s lanes of ``harness``."""
    return [row for row in (data.get("lanes") or {}).values() if isinstance(row, dict) and row.get("harness") == harness]


def _lane(data, lane) -> dict:
    row = (data.get("lanes") or {}).get(lane.key)
    return row if isinstance(row, dict) else {}


def breaks(data, harness, now=None) -> list:
    """Every break of ``harness`` still on at ``now`` in ``data`` (a ``read``): its lanes' ({code, model, at, until}, model
    the lane's) and its old Harness-wide one."""
    now = time.time() if now is None else now
    rows = [{**row["broken"], "model": row["broken"].get("model") or row.get("model") or ""}
            for row in _lanes(data, harness) if _until(row.get("broken"), now)]
    old = (data.get("breakers") or {}).get(harness)
    return rows + ([old] if _until(old, now) else [])


def broken(data, lane, now=None) -> dict | None:
    """The break still on at ``now`` in ``data`` of ``lane``: a Lane's own, else its Harness's old one; a Harness's name: any
    of that Harness (``breaks``'s first). None when there is none."""
    now = time.time() if now is None else now
    if not isinstance(lane, Lane):
        rows = breaks(data, str(lane or ""), now)
        return rows[0] if rows else None
    row = _lane(data, lane).get("broken")
    if _until(row, now):
        return {**row, "model": row.get("model") or lane.model}
    old = (data.get("breakers") or {}).get(lane.harness)
    return old if _until(old, now) else None


def cooling(data, lane, feature, now=None) -> dict | None:
    """The cooling of ``feature`` still on at ``now`` on ``lane`` (a Lane's own, else its Harness's old one; a Harness's name:
    any lane of it), or None."""
    now = time.time() if now is None else now
    harness = lane.harness if isinstance(lane, Lane) else str(lane or "")
    rows = [_lane(data, lane)] if isinstance(lane, Lane) else _lanes(data, harness)
    for row in rows:
        own = (row.get("cooling") or {}).get(feature)
        if _until(own, now):
            return own
    old = (data.get("cooling") or {}).get(f"{harness}:{feature}")
    return old if _until(old, now) else None


def refusals_today(data, lane, now=None) -> int:
    """The background refusals today of ``lane`` (a Lane, or a Harness's name: all its lanes), with its Harness's old count
    of the day."""
    now = time.time() if now is None else now
    day = day_of(now)
    harness = lane.harness if isinstance(lane, Lane) else str(lane or "")
    count = 0
    for row in [_lane(data, lane)] if isinstance(lane, Lane) else _lanes(data, harness):
        own = row.get("refusals") or {}
        if isinstance(own, dict) and own.get("day") == day and type(own.get("count")) is int:
            count += own["count"]
    refusals = data.get("refusals") or {}
    if refusals.get("day") == day:
        count += int((refusals.get("counts") or {}).get(harness) or 0)
    return count


def gated(data, lane, now=None) -> bool:
    """Whether ``lane`` has had DAILY_REFUSALS background refusals today: no more background calls to it today."""
    return refusals_today(data, lane, now) >= DAILY_REFUSALS


def may_try(data, lane, now=None) -> bool:
    """Whether a call the owner asked for may go past ``lane``'s break, cooling or gate now: none did in TRY_SECONDS."""
    now = time.time() if now is None else now
    try:
        return now - float(_lane(data, lane_of(lane)).get("tried") or 0) >= TRY_SECONDS
    except (TypeError, ValueError):
        return True


def take_try(state_dir, lane, now=None) -> bool:
    """``may_try`` and its mark (``tried``) at once, under the record's lock: whether this call is the one. Never raises."""
    now = time.time() if now is None else now
    lane, took = lane_of(lane), []

    def edit(data):
        if not may_try(data, lane, now):
            return False
        _own(data, lane)["tried"] = round(now)
        took.append(True)
        return True

    change(state_dir, edit)
    return bool(took)


def _own(data, lane) -> dict:
    """``lane``'s row in ``data``, made when it has none."""
    row = data["lanes"].get(lane.key)
    if not isinstance(row, dict):
        row = data["lanes"][lane.key] = {"harness": lane.harness, "model": lane.model, "gateway": lane.gateway}
    return row


def _prune(data, now) -> None:
    """Lanes with nothing on any more (no break, no cooling, no refusal today, no try in TRY_SECONDS) taken out."""
    day = day_of(now)
    for key, row in list(data["lanes"].items()):
        if not _live(row, now, day):
            data["lanes"].pop(key, None)


def _live(row, now, day) -> bool:
    """Whether a lane's row still holds anything: a break, a cooling, a refusal of ``day``, a try in TRY_SECONDS."""
    cool, counts = row.get("cooling"), row.get("refusals")
    try:
        tried = now - float(row.get("tried") or 0) < TRY_SECONDS
    except (TypeError, ValueError):
        tried = False
    return (_until(row.get("broken"), now) or (isinstance(cool, dict) and any(_until(each, now) for each in cool.values()))
            or (isinstance(counts, dict) and counts.get("day") == day) or tried)


def settle(state_dir, *, feature, harness, model="", gateway="", code="", ms=0, manual=False, batch=False, now=None) -> dict:
    """One call's outcome kept (``code`` '' for an answer) for its lane (``harness``, ``model``, ``gateway``): its ``recent``
    row, a break for an environment failure (BREAKS), a cooling and the day's count for a refusal (a batch feature's
    cooling; an item's refusal only counts), a success clearing its lane's break (and its Harness's old one). The record
    after."""
    now = time.time() if now is None else now
    kind = kind_of(code)
    lane = Lane(str(harness or ""), str(model or ""), str(gateway or ""))

    def edit(data):
        changed = False
        row = {"harness": harness, "model": model, "gateway": gateway, "ok": not code, "code": code, "ms": int(ms),
               "at": round(now), "manual": bool(manual)}
        old = data["recent"].get(feature) or {}
        if any(old.get(k) != row[k] for k in ("harness", "model", "gateway", "ok", "code", "manual")) \
                or now - float(old.get("at") or 0) >= RECENT_GAP:
            data["recent"][feature] = row
            changed = True
        if harness and not code:
            if _lane(data, lane).get("broken"):
                _lane(data, lane).pop("broken", None)
                changed = True
            if harness in data["breakers"]:
                data["breakers"].pop(harness, None)
                changed = True
        if harness and kind == "break":
            _own(data, lane)["broken"] = {"code": code, "model": model, "at": round(now), "until": round(now + BREAK_SECONDS)}
            changed = True
        if harness and kind == "refused":
            own = _own(data, lane)
            if batch:
                own.setdefault("cooling", {})[feature] = {"at": round(now), "until": round(now + COOL_SECONDS)}
            day = day_of(now)
            counts = own.get("refusals") if isinstance(own.get("refusals"), dict) else {}
            own["refusals"] = {"day": day, "count": (int(counts.get("count") or 0) if counts.get("day") == day else 0) + 1}
            changed = True
        if changed:
            _prune(data, now)
        return changed

    return change(state_dir, edit)


def clear(state_dir) -> dict:
    """The coolings and the breaks gone, every lane's and the old Harness-wide ones (the owner changed the 辅助模型 setting);
    the day's refusal counts and the owner's last tries stay."""
    def edit(data):
        lanes = [row for row in data["lanes"].values() if row.get("broken") or row.get("cooling")]
        if not data["cooling"] and not data["breakers"] and not lanes:
            return False
        data["cooling"], data["breakers"] = {}, {}
        for row in lanes:
            row.pop("broken", None)
            row.pop("cooling", None)
        _prune(data, time.time())
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
