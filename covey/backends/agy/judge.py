"""One question to Antigravity and nothing else (AntigravityBackend.ask_once, core/harness.py HarnessCapabilities.ask_once):
the background model's questions (app/assist.py, 辅助模型) when they go to Antigravity.

``agy --input-format stream-json --output-format stream-json`` in a HOME of its own (core/agy_home.py ScratchHome: the
login and the CLI's own files linked; the conversation, its log and its summaries stay there and go with it; no MCP server,
hook, rule, plugin or setting of the owner's), without --dangerously-skip-permissions (a step that asks is skipped) and with a
lead's deny rules on top (backends/agy/bridge.py READ_ONLY_SETTINGS: no file written, no command, no URL read), slash
commands off, in an empty directory of its own. The question (its system prompt, then its JSON) is one user line on
standard input, never in argv; the answer is the result's response, and the process group is killed then or at the limit
(core/one_shot.py). Without a sign-in (core/agy_home.py signed_in) nothing starts: agy would wait a minute for a browser.

Whose login (``home_of``): with several accounts (backends/agy/accounts.py, agy-accounts.json) the one a turn of the model's
group would start on (``accounts.pick``: 只用这个号, else the group's active one unless it is held out, else the next that is
not), read from the accounts' records alone: a spare account is never asked anything (Google may link accounts seen taking
turns from one machine), the records are not changed. Every account held out (spent, paused, signed out): nothing starts,
``quota_exhausted``. With one login, a spent quota an earlier turn said resets later (``accounts.holding``) is
``quota_exhausted`` too, before anything starts.

The model (``choose``): every question of the 辅助模型 alike (T924: no light model picked for some of them), the model the
setting names or follows (``follow``, app/assist.py) when the saved catalog (backends/agy/bridge.py model_catalog, the
launch card's) lists it or there is none, at the setting's budget (``effort``: the butler's xhigh is asked at xhigh) when
the catalog does not say the model lacks it, else at agy's own budget for it; with no model there, COVEY_JUDGE_AGY_MODEL
(an id ``agy models`` lists, its budget in it: gemini-3.8-flash-low); else agy's own default model and budget.
``model_of``: the model a question would run on, without starting anything ('' for agy's own default: which that is only
agy knows).

A failed result's reason: what the Harness reads of its error (``classify``: backends/agy/bridge.py error_code, a login,
a refusal, a model the account cannot use, a spent quota, a limit, a fault), else core/assist.py code_of_text, else
``model_error``; its detail the error's own words as a card may show them (core/failure.py said_of). A non-zero exit is
read from its standard error the same way (core/one_shot.py). Seen on agy 1.5.14: 4 s to start, a few more for the model.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from covey.backends.agy import accounts
from covey.core import env as cenv
from covey.core.agy_home import ScratchHome, signed_in
from covey.core.assist import code_of_text
from covey.core.failure import said_of
from covey.core.harness import AskFailed
from covey.core.one_shot import run_lines
from covey.core.process_env import child_process_env
from covey.core.state import ensure_private_dir

MODEL_ENV = "COVEY_JUDGE_AGY_MODEL"


def choose(catalog, *, environ=None, follow: str = "", effort: str = "") -> tuple:
    """(model, effort) of a question from ``catalog`` ({model: {display_name, supported_reasoning_levels}}): '' for agy's
    own (see the module docstring). ``follow``, ``effort``: the model and budget of the setting."""
    if follow and (not catalog or follow in catalog):
        row = (catalog or {}).get(follow)
        levels = None if row is None else [level.get("effort") for level in row.get("supported_reasoning_levels") or ()
                                           if isinstance(level, dict)]
        # Unknown to the catalog: agy checks the budget itself. Listed without budgets: agy refuses one.
        return follow, (effort if effort and (levels is None or effort in levels) else "")
    wanted = str(cenv.get(MODEL_ENV, "", environ) or "").strip()
    return (wanted, "") if wanted else ("", "")


def argv(ctx, model: str, effort: str, settings) -> list:
    """The ``agy`` command line of one question (its words come on standard input)."""
    return [*ctx.backend_cmd, "--output-format", "stream-json", "--input-format", "stream-json",
            "--disable-slash-commands", *(["--model", model] if model else []), *(["--effort", effort] if effort else []),
            "--settings", json.dumps(settings, separators=(",", ":"))]


def code_of(error: str, classify=None) -> str:
    """The reason of an error's words (see the module docstring)."""
    return (classify(error) if classify is not None else "") or code_of_text(error) or "model_error"


def answer_of(rows, classify=None) -> str:
    """The response of a successful result; AskFailed otherwise (``code_of`` its error, its words the detail)."""
    result = next((row.get("result") for row in rows if row.get("event") == "result"), None)
    if not isinstance(result, dict):
        raise AskFailed("bad_output")
    if result.get("status") != "SUCCESS":
        error = str(result.get("error") or "")
        raise AskFailed(code_of(error, classify) if error else "model_error",
                        said_of(error) or said_of(str(result.get("status") or ""))[:40])
    text = result.get("response")
    if not isinstance(text, str) or not text.strip():
        raise AskFailed("bad_output")
    return text


def home_of(ctx, env, model, environ=None) -> dict:
    """``env`` with HOME the login a question on ``model`` runs with (see the module docstring); AskFailed
    quota_exhausted when none may take it now. The accounts' records are read, never written."""
    try:
        listed = accounts.load(environ)
    except ValueError:
        listed = []
    now = time.time()
    if not listed:
        found = accounts.holding(ctx.state_dir, model, now)
        if found is not None:
            raise AskFailed("quota_exhausted", said_of(
                f"Antigravity 的 {accounts.label_of(accounts.group_of(ctx.state_dir, model))} 额度用完了，"
                f"{accounts.when(float(found['reset_at']), now)} 恢复"))
        return env
    state = accounts.Records(ctx.state_dir).read()
    group = str((state.get("groups") or {}).get(model or "") or accounts.group_of(ctx.state_dir, model))
    real = Path(env.get("HOME") or Path.home())
    account = accounts.pick(state, listed, group, now, real)
    if accounts.held(state, account, group, now, real):
        back = accounts.back_at(state, listed, group, now, real)
        raise AskFailed("quota_exhausted", said_of(
            f"Antigravity 的 {len(listed)} 个号的 {accounts.label_of(group)} 额度都用完了、暂停了或要重新登录"
            + (f"，最早 {accounts.when(back, now)} 恢复" if back else "")))
    return env if account.home is None else {**env, "HOME": str(account.home)}


def ask_once(backend, ctx, system: str, data: str, *, limit: float, cwd=None, settings,
             follow: str = "", effort: str = "", classify=None, environ=None) -> str:
    """The answer's text to ``data`` under ``system``, within ``limit`` seconds; AskFailed with the reason otherwise.
    ``settings``: the deny rules (backends/agy/bridge.py READ_ONLY_SETTINGS); ``cwd``: the empty directory it runs in
    (made; default <state>/agy-judge); ``follow``, ``effort``: the model and budget of the setting (``choose``);
    ``classify``: the Harness's reading of an error's words (backends/agy/bridge.py error_code)."""
    folder = Path(cwd) if cwd is not None else Path(ctx.state_dir) / "agy-judge"
    ensure_private_dir(folder)
    model, effort = choose(backend.model_catalog(ctx), environ=environ, follow=follow, effort=effort)
    env = home_of(ctx, child_process_env(folder), model, environ)
    if not signed_in(env):
        raise AskFailed("not_signed_in")
    line = json.dumps({"event": "user", "message": {"content": f"{system}\n\n{data}"}}, ensure_ascii=False) + "\n"
    with ScratchHome(Path(ctx.state_dir) / "tmp", env) as home:
        rows = run_lines(argv(ctx, model, effort, settings), line, env={**env, "HOME": str(home)}, cwd=str(folder),
                         limit=limit, done=lambda row: row.get("event") == "result", skip=("oauth2",), classify=classify)
    return answer_of(rows, classify)


def model_of(backend, ctx, *, follow: str = "", environ=None) -> str:
    """The model id a question (``ask_once``) would run on; '' for agy's own default. Nothing is started."""
    return choose(backend.model_catalog(ctx), environ=environ, follow=follow)[0]
