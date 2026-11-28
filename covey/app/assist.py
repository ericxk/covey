"""辅助模型 (assist, docs/configuration.md 辅助模型): the one way the Bridge's background model calls go out. 分流 (the track
judge), 整理专项 (its judge and, inside a run, the 话题要点 backfill) and 记忆自己长 all ask here (FEATURES); none of them
picks a Harness or calls app/track_judge.py's ``tools``, ``ask`` or ``judge`` or a backend's ``ask_once`` itself
(tests/test_structure.py AssistExitTest). 看看, the evening candidates and 整理记忆 work by their rules alone, the
real-time merge and the patrol were taken out (T898).

Which Harness, model and budget (``route``), from the 设置 card's 辅助模型 (core/butler_settings.py AssistChoice). One for
every feature (T924, the owner: 这块不用分开): no light model is picked for 分流 and 记忆自己长 and another for 整理专项.

- 不用模型 (``off``): no call at all; each feature has its own way without one (分流: a new track; 整理专项: no scheduled
  run, a run the owner starts says why; 记忆自己长: the checkpoint's 以后/别再 rule records the owner's corrections).
- A Harness named: that one only, on the model and budget named with it ('' each: the Harness's own). Not enabled, unable to answer one question by itself, unusable now (the availability
  watch's last preflight and the latest task outcomes the Bridge holds in memory, app/availability.py), not signed in
  (Antigravity), broken for the background (core/assist.py ``breakers``), cooling for this feature or gated for the day:
  no call, and the reason says so. Never another Harness.
- 自动（跟随管家）: the butler's Harness while the butler is on (core/butler.py ``available`` and the mode 管家), then the
  Harness of 新话题默认 and 专项负责人, each where the settings name one (none named: the Bridge's default
  Harness), each with that setting's model and budget (``follow``, ``effort``: the butler's gemini-4-neon at xhigh is asked
  at xhigh, never a lower one put in its place; an orchestration mode at its budget alone, core/defaults.py budget_of; a
  gateway's Codex model is asked through that gateway; a setting with no model leaves both to the Harness). The first that
  passes the checks above answers. One that fails on its environment (core/assist.py BREAKS) has
  the next one take the call (at most MAX_TRIES Harnesses a call), but for 分流, which asks once and falls back to a new
  track on any failure: it is on the path of the owner's message. A timeout or a refusal never goes to another Harness.

What a call leaves (core/assist.py ``settle``), for its lane (core/assist.py Lane: the Harness, the model it ran on, the
model gateway it went through; one lane's failures never stop another): its ``recent`` row; an environment failure breaks
that lane for the automatic calls for 15 minutes; an official refusal of a batch feature (BATCH) cools that feature on that
lane for 6 hours, and every refusal counts toward the lane's gate of the day. A call the owner asked for (``manual``) goes
past a break, a cooling or the gate, once a lane a minute (core/assist.py TRY_SECONDS), and its answer clears the break.
A change of the setting clears the breaks and the coolings.

Before a process starts the call is checked again against the settings read now (``allowed``): a Harness the owner no
longer allows is refused with a log line ``assist refused harness=… feature=…``. No process is kept between calls: each
question starts its own (app/track_judge.py).
"""
from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from typing import Callable

from covey.app import track_judge
from covey.app.track_judge import Failed
from covey.core import assist as records
from covey.core import butler, butler_settings, gateways
from covey.core import project_proposals as proposals
from covey.core.defaults import budget_of

CLAUDE = track_judge.CLAUDE
AUTO, NONE = "auto", "none"  # The setting's two choices that are not a Harness (the cards' and the page's values).
MAX_TRIES = 2
SETTINGS_SECONDS = 5.0  # How long a read of main-thread.json is trusted (a write here forgets it at once).
READY_SECONDS = 60.0  # How long Antigravity's sign-in check is trusted.


@dataclasses.dataclass(frozen=True)
class Feature:
    label: str
    batch: bool = False  # A refusal cools the feature on that Harness (BATCH); else only the item it was about.
    row: str = ""  # The feature whose 后台活最近一次 row its calls are kept under ('' its own).


TRACK, TIDY, BACKFILL, LEARN = "track", "tidy", "backfill", "learn"
FEATURES = {
    TRACK: Feature("分流"),
    TIDY: Feature("整理专项", batch=True),
    BACKFILL: Feature("补话题要点", row=TIDY),  # Only inside a 整理 run (app/tidy_run.py), before it judges.
    LEARN: Feature("记忆自己长"),
}
# The web page's 后台活最近一次 lists these, in this order.
SHOWN = (TRACK, TIDY, LEARN)
NAMES = {"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity"}


@dataclasses.dataclass(frozen=True)
class Choice:
    """One Harness a call may go through: its name, backend and context, and the model id of the setting it goes by
    (``follow``: named with the Harness or followed; its provider is the call's; '' the Harness's own). ``lane``: its core/assist.py Lane when the check worked it out (None:
    worked out when the call is kept, ``lane_for``); ``bypass``: the reason (broken, cooling, refusal_gate) a call the owner
    asked for goes past (core/assist.py ``take_try`` before it starts), '' none. ``effort``: the budget of that setting,
    with ``follow`` ('' none: the Harness's own). ``named``: the setting names this Harness (not 自动)."""
    name: str
    backend: object = None
    ctx: object = None
    follow: str = ""
    lane: object = None
    bypass: str = ""
    effort: str = ""
    named: bool = False


@dataclasses.dataclass(frozen=True)
class Route:
    """Where a call goes: ``first`` (None: nowhere, ``why`` says why: core/assist.py NOT_ASKED or a reason of the Harness
    ``about``), ``rest`` the further Harnesses of 自动, ``mode`` auto, named or none."""
    first: Choice | None = None
    rest: tuple = ()
    mode: str = AUTO
    why: str = ""
    about: str = ""
    detail: str = ""

    @property
    def harness(self) -> str:
        return self.first.name if self.first is not None else ""


def name_of(harness) -> str:
    return NAMES.get(harness, harness)


# What the settings say, read with a short memory -------------------------------------------------------------------

_lock = threading.Lock()
_settings: dict = {}  # state directory -> (monotonic time, Settings)
_ready: dict = {}  # (state directory, harness) -> (monotonic time, reason)


def settings_of(state, *, fresh=False):
    key = str(state)
    now = time.monotonic()
    with _lock:
        held = _settings.get(key)
        if held is not None and not fresh and now - held[0] < SETTINGS_SECONDS:
            return held[1]
    value = butler_settings.load(state)
    with _lock:
        _settings[key] = (time.monotonic(), value)
    return value


def forget() -> None:
    """This process's memory of the settings and the sign-in checks gone (a settings change, tests)."""
    with _lock:
        _settings.clear()
        _ready.clear()
    records.forget()


def state_of(topics, state=None):
    if state is not None:
        return Path(state)
    found = getattr(getattr(topics, "runtime", None), "state_dir", None)
    if found is not None:
        return Path(found)
    for app in (getattr(topics, "apps", None) or {}).values():
        ctx = getattr(getattr(app, "harness", None), "context", None)
        if getattr(ctx, "state_dir", None) is not None:
            return Path(ctx.state_dir)
    return None


# The checks of one Harness -----------------------------------------------------------------------------------------

def tools_of(apps, name):
    """(backend, context) of the Harness ``name`` when it answers one question: Claude Code (app/track_judge.py's
    one-shot) or one whose own CLI does (core/harness.py ask_once); (None, None) otherwise."""
    harness = getattr((apps or {}).get(name), "harness", None)
    backend, ctx = getattr(harness, "backend", None), getattr(harness, "context", None)
    if backend is None or ctx is None:
        return None, None
    if name == CLAUDE or track_judge.native(backend, ctx):
        return backend, ctx
    return None, None


def unusable(runtime, name, now=None) -> str:
    """Why the Harness ``name`` cannot take a call now as far as the Bridge's memory says ('' it can): the availability
    watch's last preflight (runtime.availability_health, refreshed each minute) and the latest task outcome it saw
    (app/availability.py NOT_NOW, NOT_NOW_SECONDS). No file is read: the 分流 of every butler message asks this."""
    from covey.app.availability import NOT_NOW, NOT_NOW_SECONDS
    health = getattr(runtime, "availability_health", None) or {}
    preflight = (health.get("preflight") or {}).get(name) or {}
    if preflight.get("status") == "unavailable":
        return "authentication_error" if preflight.get("reason") == "authentication" else "spawn_failed"
    now = time.time() if now is None else now
    rows = [row for row in health.get("observations") or () if isinstance(row, dict) and row.get("backend") == name
            and isinstance(row.get("checked_at"), (int, float))]
    latest = max(rows, key=lambda row: row["checked_at"], default=None)
    if latest and latest.get("status") == "failed" and latest.get("error") in NOT_NOW \
            and now - latest["checked_at"] < NOT_NOW_SECONDS:
        return str(latest["error"])
    return ""


def ready(state, name, ctx) -> str:
    """Antigravity's sign-in before it is chosen ('' signed in, or a Harness this does not concern): this machine's login,
    or with its accounts (backends/agy/accounts.py, two or more) one of them not held out of the Gemini group. Read at
    most once a READY_SECONDS."""
    if name != "agy" or ctx is None:
        return ""
    key = (str(state), name)
    now = time.monotonic()
    with _lock:
        held = _ready.get(key)
        if held is not None and now - held[0] < READY_SECONDS:
            return held[1]
    reason = _agy_ready(state, ctx)
    with _lock:
        _ready[key] = (time.monotonic(), reason)
    return reason


def _agy_ready(state, ctx) -> str:
    try:
        from covey.backends.agy import accounts
        from covey.core.agy_home import signed_in
        from covey.core.process_env import child_process_env
        if accounts.configured():
            listed = accounts.load()
            data = accounts.Records(state).read()
            if all(accounts.held(data, account, "gemini", time.time()) for account in listed):
                return "quota_exhausted"
            return ""
        if accounts.holding(state, ""):
            return "quota_exhausted"
        return "" if signed_in(child_process_env(Path(state))) else "not_signed_in"
    except Exception:  # noqa: BLE001 - not known: the call says it
        return ""


def lane_for(choice, feature):
    """The core/assist.py Lane a call of ``feature`` through ``choice`` goes by: the Harness, the model it runs on
    (``shown_model``) and the model gateway of that model (core/gateways.py ``for_model``; '' none)."""
    if choice.lane is not None:
        return choice.lane
    model = shown_model(choice)
    try:
        gateway = gateways.for_model(choice.name, model or choice.follow)
    except Exception:  # noqa: BLE001 - a gateway list that cannot be read names none
        gateway = None
    return records.Lane(choice.name, model, getattr(gateway, "name", "") or "")


def marked(data, harness) -> bool:
    """Whether the record holds anything for ``harness`` (a lane of it, its old break, cooling or count): only then is its
    lane worked out on the way (``check``: the 分流 of every butler message routes)."""
    lanes = any(isinstance(row, dict) and row.get("harness") == harness for row in (data.get("lanes") or {}).values())
    return (lanes or harness in (data.get("breakers") or {})
            or any(key.split(":", 1)[0] == harness for key in (data.get("cooling") or {}))
            or harness in ((data.get("refusals") or {}).get("counts") or {}))


def check(state, runtime, apps, name, feature, *, manual, data, now=None, follow="") -> tuple:
    """(reason, detail, lane, bypass): why the Harness ``name`` may not take a call of ``feature`` ('' it may); its lane when
    the record named it (``marked``, else None); the reason a call the owner asked for (``manual``) goes past (a break, a
    cooling, the gate: once a lane a minute, core/assist.py ``may_try``), '' none."""
    backend, ctx = tools_of(apps, name)
    if (apps or {}).get(name) is None:
        return "not_enabled", "", None, ""
    if backend is None:
        return "not_one_shot", "", None, ""
    bad = unusable(runtime, name, now)
    if bad:
        return "unusable", bad, None, ""
    bad = ready(state, name, ctx)
    if bad:
        return "unusable", bad, None, ""
    if not marked(data, name):
        return "", "", None, ""
    lane = lane_for(Choice(name, backend, ctx, follow), feature)
    why, detail = "", ""
    row = records.broken(data, lane, now)
    if row is not None:
        why, detail = "broken", str(row.get("code") or "")
    elif FEATURES[feature].batch and records.cooling(data, lane, feature, now):
        why = "cooling"
    elif records.gated(data, lane, now):
        why = "refusal_gate"
    if why and manual and records.may_try(data, lane, now):
        return "", "", lane, why
    return why, detail, lane, ""


# Where a call goes -------------------------------------------------------------------------------------------------

def followed(settings, apps, default) -> list:
    """[(Harness, model id, budget)] of 自动 in its order (the module docstring), each Harness once: the budget the setting
    runs its model with ('' with no model named: the Harness's own)."""
    from covey.app import butler_harness
    out = []
    if butler.available() and settings.enabled:
        name = butler_harness.name_of(settings, apps)
        out.append((name, *butler_harness.model_of(settings, name)))
    for row in (settings.topic_defaults, settings.lead):
        if row.harness:
            out.append((row.harness, row.model, row.effort))
    if not out and default:
        out.append((default, "", ""))
    seen, kept = set(), []
    for name, model, effort in out:
        if name not in seen:
            seen.add(name)
            kept.append((name, model, budget_of(effort) if model else ""))
    return kept


def default_of(topics) -> str:
    """The Bridge's default Harness (app/topics.py ``default``), else the first one enabled."""
    return getattr(topics, "default", "") or next(iter(getattr(topics, "apps", None) or {}), "")


def route(topics, feature=TRACK, *, manual=False, state=None, now=None) -> Route:
    """Where a call of ``feature`` goes now (the module docstring). Never raises."""
    state = state_of(topics, state)
    apps = getattr(topics, "apps", None) or {}
    runtime = getattr(topics, "runtime", None)
    if state is None:
        return Route(why="no_backend")
    settings = settings_of(state)
    choice = settings.assist
    if choice.off:
        return Route(mode=NONE, why="none")
    data = records.read(state)
    if choice.harness:
        why, detail, lane, bypass = check(state, runtime, apps, choice.harness, feature, manual=manual, data=data, now=now,
                                          follow=choice.model)
        if why:
            return Route(mode="named", why=why, about=choice.harness, detail=detail)
        backend, ctx = tools_of(apps, choice.harness)
        return Route(Choice(choice.harness, backend, ctx, choice.model, lane, bypass,
                            budget_of(choice.effort) if choice.model else "", named=True), mode="named")
    passed, first_bad = [], None
    for name, model, effort in followed(settings, apps, default_of(topics)):
        why, detail, lane, bypass = check(state, runtime, apps, name, feature, manual=manual, data=data, now=now, follow=model)
        if why:
            first_bad = first_bad or (why, name, detail)
            continue
        backend, ctx = tools_of(apps, name)
        passed.append(Choice(name, backend, ctx, model, lane, bypass, effort))
    if passed:
        return Route(passed[0], tuple(passed[1:]))
    if first_bad:
        return Route(why=first_bad[0], about=first_bad[1], detail=first_bad[2])
    return Route(why="no_backend")


def allowed(topics, feature, harness, *, state=None) -> bool:
    """Whether ``harness`` is one the settings, read now, let a call of ``feature`` go to: the named one, or under 自动
    one of the Harnesses it follows; never with 不用模型. The last check before a process starts."""
    state = state_of(topics, state)
    if state is None:
        return False
    choice = settings_of(state, fresh=True).assist
    if choice.off:
        return False
    if choice.harness:
        return harness == choice.harness
    settings = settings_of(state)
    return harness in {row[0] for row in followed(settings, getattr(topics, "apps", None) or {}, default_of(topics))}


# The calls ---------------------------------------------------------------------------------------------------------

def claude_model(choice) -> str:
    """The Claude Code model of a call through ``choice``: the setting's (``follow``), else COVEY_PROJECT_TIDY_MODEL, else
    '' (Claude Code's own default: no ``--model``)."""
    return choice.follow or proposals.model()


def effort_of(choice, model) -> str:
    """The budget a call through ``choice`` is asked at: the setting's, when it runs on that setting's model ('' otherwise:
    the Harness's own). A native Harness's own CLI checks it again against its catalog (``choose``)."""
    if not choice.effort:
        return ""
    return choice.effort if choice.name != CLAUDE or model == choice.follow else ""


def failed_of(route_) -> Failed:
    exc = Failed(route_.why or "no_backend", route_.detail)
    exc.via, exc.model = route_.about, ""
    return exc


def ask(feature, system, data, *, topics, limit, cwd, manual=False, state=None,
        log: Callable[[str], None] | None = None) -> str:
    """One question of ``feature`` (FEATURES), its raw answer as Claude Code's JSON envelope (app/track_judge.py
    ``answer_text`` reads it); Failed with the reason otherwise (``via``: the Harness it concerns, ``model``). ``manual``: the owner asked for it (✨ 整理, the butler's projects_tidy): a break, a cooling or the
    day's gate does not stop it, once a lane a minute. Asked again each call: a loop over topics never keeps a backend
    across calls."""
    if feature not in FEATURES:
        raise ValueError(f"unknown assist feature {feature!r}")
    state = state_of(topics, state)
    log = log or (getattr(getattr(topics, "runtime", None), "log", None) or print)
    kept = FEATURES[feature].row or feature  # The recent row (and a batch feature's cooling) it is kept under.
    found = route(topics, feature, manual=manual, state=state)
    if found.first is None:
        if state is not None:
            records.settle(state, feature=kept, harness=found.about, code=found.why, manual=manual,
                           batch=FEATURES[feature].batch)
        raise failed_of(found)
    tries = [found.first, *found.rest][:MAX_TRIES]
    for number, choice in enumerate(tries):
        if not allowed(topics, feature, choice.name, state=state):
            log(f"assist refused harness={choice.name} feature={feature}")
            exc = Failed("not_allowed")
            exc.via, exc.model = choice.name, ""
            raise exc
        lane = lane_for(choice, feature)
        if choice.bypass:
            if not records.take_try(state, lane):  # Another of his calls went past it within the minute.
                exc = Failed(choice.bypass, "tried")
                exc.via, exc.model = choice.name, lane.model
                raise exc
            log(f"assist past {choice.bypass} harness={choice.name} feature={feature}: the owner's call, once a minute")
        started = time.monotonic()
        try:
            # The setting's model and budget, every feature alike (``claude_model``; a native Harness's own CLI checks the
            # model and budget against its catalog, backends/agy/judge.py, backends/codex/judge.py ``choose``).
            asked = claude_model(choice)
            raw = track_judge.ask(system, data, backend=choice.backend, ctx=choice.ctx, limit=limit, cwd=cwd,
                                  model=asked, follow=choice.follow, effort=effort_of(choice, asked))
            track_judge.answer_text(raw, CLAUDE)  # An error envelope is a failure of the call, with its reason.
        except Failed as exc:
            ms = round((time.monotonic() - started) * 1000)
            code = exc.reason
            records.settle(state, feature=kept, harness=choice.name, model=lane.model, gateway=lane.gateway, code=code, ms=ms,
                           manual=manual, batch=FEATURES[feature].batch)
            log(f"assist failed feature={feature} harness={choice.name} reason={code} ms={ms}"
                + (f" detail={exc.detail[:160]}" if exc.detail else ""))
            exc.via, exc.model = choice.name, lane.model
            if records.kind_of(code) == "break" and number + 1 < len(tries):
                log(f"assist next feature={feature} from={choice.name} to={tries[number + 1].name}")
                continue
            raise
        ms = round((time.monotonic() - started) * 1000)
        records.settle(state, feature=kept, harness=choice.name, model=lane.model, gateway=lane.gateway, ms=ms, manual=manual,
                       batch=FEATURES[feature].batch)
        return raw
    raise failed_of(found)  # Not reached: the last try raises.


def judge(message, *, topics, question="", log=print, environ=None, state=None):
    """分流's verdict on ``message`` (app/track_judge.py ``judge``) through the route of TRACK: one Harness, one try (it is on
    the owner's message's path); with none (不用模型, the named one unusable) a ``new`` Verdict whose ``why`` says why.
    COVEY_TRACK_JUDGE=off or cmd: is the deployment's and comes first, as before."""
    state = state_of(topics, state)
    mode = track_judge.mode(environ)
    if mode != CLAUDE:
        return track_judge.judge(message, question=question, log=log, environ=environ)
    found = route(topics, TRACK, state=state)
    if found.first is None:
        verdict = track_judge.judge(message, question=question, log=log, environ=environ,
                                    unrouted=found.why)
        if verdict.why == found.why and state is not None:
            records.settle(state, feature=TRACK, harness=found.about, code=found.why)
        return verdict
    choice = found.first
    if not allowed(topics, TRACK, choice.name, state=state):
        log(f"assist refused harness={choice.name} feature={TRACK}")
        return track_judge.judge(message, question=question, log=log, environ=environ,
                                 unrouted="not_allowed")
    model = claude_model(choice)
    verdict = track_judge.judge(message, question=question, backend=choice.backend, ctx=choice.ctx,
                                log=log, environ=environ, follow=choice.follow, model=model,
                                effort=effort_of(choice, model))
    if verdict.why not in track_judge.NOT_CALLED and state is not None:
        code = "" if verdict.why in ("ok", "low_confidence") else verdict.why
        lane = lane_for(choice, TRACK)
        records.settle(state, feature=TRACK, harness=choice.name, model=lane.model, gateway=lane.gateway, code=code,
                       ms=verdict.ms)
    return verdict


# The settings changed ------------------------------------------------------------------------------------------------

def on_settings_changed(state_dir, before, after) -> None:
    """main-thread.json written (core/butler_settings.py ``listen``): the settings read again from now on; a change of the
    辅助模型 clears its breaks and coolings."""
    forget()
    try:
        if before is not None and after is not None and before.assist != after.assist:
            records.clear(state_dir)
    except Exception:  # noqa: BLE001 - the write itself is done
        pass


butler_settings.listen(on_settings_changed)


# What the settings card and the web page say -------------------------------------------------------------------------

def shown_model(choice) -> str:
    """The model id a call through ``choice`` runs on ('' not known: the Harness's own default): Claude Code's
    ``claude_model``; Codex's and Antigravity's as their backend says (``ask_model``)."""
    if choice is None:
        return ""
    if choice.name == CLAUDE:
        return claude_model(choice)
    pick = getattr(choice.backend, "ask_model", None)
    if pick is None:
        return ""
    try:
        return str(pick(choice.ctx, follow=choice.follow) or "")
    except Exception:  # noqa: BLE001
        return ""


def describe(topics, *, state=None) -> dict:
    """What the 辅助模型 section says under its rows: {mode, harness, model, effort, why, about, words}; ``words``: 现在：
    （跟随管家）Antigravity · gemini-4-neon · xhigh, every background call on it."""
    found = route(topics, TRACK, manual=True, state=state)
    setting = settings_of(state_of(topics, state)).assist if state_of(topics, state) is not None else None
    mode = NONE if setting is not None and setting.off else ("named" if setting is not None and setting.harness else AUTO)
    if found.first is None:
        about = name_of(found.about) if found.about else ""
        reason = records.words(found.detail or found.why) if found.why == "unusable" else records.words(found.why)
        words = "后台活不调模型，只按规则做。" if found.why == "none" else \
            f"现在用不了：{about + ' ' if about else ''}{reason}。后台活先只按规则做。"
        return {"mode": mode, "harness": found.about, "model": "", "effort": "", "why": found.why, "about": found.about,
                "words": words}
    model = shown_model(found.first)
    effort = effort_of(found.first, model) if found.first.name == CLAUDE else found.first.effort
    # Named as U5 asks (docs/ui.md 13): a model the Harness picks itself is its login's, a budget left to it is not said.
    shown = " · ".join([name_of(found.first.name), model or "当前登录账号的模型", *([effort] if effort else [])])
    words = f"现在：{'跟随管家，' if mode == AUTO else ''}{shown}，分流、整理专项、记忆自己长都用它。"
    return {"mode": mode, "harness": found.first.name, "model": model, "effort": effort, "why": "", "about": "",
            "words": words}


def offered(topics, *, state=None) -> list:
    """The 辅助模型 row's choices [(value, words)]: 自动, each Harness enabled here that answers one question by itself
    (the roster's order), 不用模型; a Harness the settings name that is not among them is kept, so the row says it."""
    from covey.core.roster import NAMES as ROSTER
    apps = getattr(topics, "apps", None) or {}
    names = [name for name in (*ROSTER, *apps) if name in apps and tools_of(apps, name)[0] is not None]
    names = list(dict.fromkeys(names))
    state = state_of(topics, state)
    named = settings_of(state).assist.harness if state is not None else ""
    if named and named not in names:
        names.append(named)
    from covey.cards.butler_panel import ASSIST_AUTO, ASSIST_NONE, ASSIST_WORDS
    return [(ASSIST_AUTO, ASSIST_WORDS[ASSIST_AUTO]), *((name, name_of(name)) for name in names),
            (ASSIST_NONE, ASSIST_WORDS[ASSIST_NONE])]


def current(settings) -> str:
    """The row's value of ``settings`` (core/butler_settings.py Settings): auto, none or the Harness named."""
    from covey.cards.butler_panel import ASSIST_AUTO, ASSIST_NONE
    choice = settings.assist
    return ASSIST_NONE if choice.off else choice.harness or ASSIST_AUTO


def chosen(option, offered_) -> object:
    """The AssistChoice a click on ``option`` saves, None when it is not offered."""
    from covey.cards.butler_panel import ASSIST_AUTO, ASSIST_NONE
    if option not in {value for value, _ in offered_}:
        return None
    if option == ASSIST_AUTO:
        return butler_settings.AssistChoice()
    if option == ASSIST_NONE:
        return butler_settings.AssistChoice(off=True)
    return butler_settings.AssistChoice(harness=option)


def available(topics, *, state=None) -> tuple:
    """(whether the background calls have a model now, the words when not): for 设置's 能做什么."""
    found = route(topics, TIDY, manual=True, state=state)
    if found.first is not None:
        return True, ""
    if found.why == "none":
        return False, "辅助模型选了不用模型：不做定时整理专项，手动整理会说明原因；分流一律新开一件"
    about = name_of(found.about) if found.about else ""
    reason = records.words(found.detail or found.why) if found.why == "unusable" else records.words(found.why)
    return False, f"辅助模型现在用不了：{about + ' ' if about else ''}{reason}"


def recent(state) -> list:
    """The web page's 后台活最近一次: one row a feature that has called (SHOWN's order), {feature, label, harness, name,
    model, ok, code, words, ms, at, manual}."""
    data = records.read(state)
    rows = []
    for feature in SHOWN:
        row = (data.get("recent") or {}).get(feature)
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "")
        rows.append({"feature": feature, "label": FEATURES[feature].label, "harness": str(row.get("harness") or ""),
                     "name": name_of(str(row.get("harness") or "")), "model": str(row.get("model") or ""),
                     "ok": bool(row.get("ok")), "code": code, "words": records.words(code) if code else "",
                     "ms": int(row.get("ms") or 0), "at": int(row.get("at") or 0), "manual": bool(row.get("manual"))})
    return rows
