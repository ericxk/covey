"""The app's online configuration, read as the bot through Feishu's official API (no login): what
core/app_manifest.py compares with what the Bridge needs, before and after feishu/app_console.py changes it.

    GET /open-apis/application/v6/applications/<app>                      scopes in effect, callbacks, online version
    GET /open-apis/application/v6/applications/<app>/app_versions/<ver>   that version's bot, menu and events

Both need the app's own scope application:application:self_manage; without it ``online_config`` says so
(``error`` no_self_manage) and only a console run (which adds it) can tell more.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from covey.core import env as cenv
from covey.core import app_manifest
from covey.core.paths import data_home
from covey.feishu.transport import CliFailed, cli_json, lark_argv, last_json

TIMEOUT = 30
SCOPE_CODES = {99991672, 99991679}  # Feishu: the app lacks the scope this API needs.
PYTHON_ENV = "COVEY_PLAYWRIGHT_PYTHON"  # The interpreter feishu/app_console.py runs under.


def api_get(profile, path, params, *, runner=None):
    """The ``data`` of one official GET as the bot; CliFailed / ValueError / OSError when it failed."""
    argv = lark_argv(profile, "api", "GET", path, "--as", "bot", "--params",
                     json.dumps(params, separators=(",", ":")), "--format", "json")
    try:
        payload = cli_json(argv, timeout=TIMEOUT, runner=runner or subprocess.run, expect=dict)
    except CliFailed as exc:
        payload = last_json(exc.proc.stdout)
        if not payload:
            raise
    if payload.get("ok") is False or payload.get("code") not in (None, 0):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        raise ApiError(error.get("code"), str(error.get("message") or error.get("msg") or ""))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApiError(None, "no data")
    return data


class ApiError(RuntimeError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(f"{code} {message}".strip())

    @property
    def scope(self):
        return self.code in SCOPE_CODES or "scope" in self.message.lower() or "权限" in self.message


def online_config(profile, app_id, *, runner=None):
    """The online app as core/app_manifest.py reads it: ``scopes``, ``events``, ``callbacks`` (sets), ``bot``,
    ``menu_enable`` (bools), ``menu_keys`` (the menu's event keys, in order), ``menus`` ((key, name) pairs),
    ``version`` (its number) and ``version_id``; or ``{"error": …, "detail": …}``: no_self_manage, no_version,
    failed."""
    base = f"/open-apis/application/v6/applications/{app_id}"
    try:
        app = api_get(profile, base, {"lang": "zh_cn"}, runner=runner).get("app") or {}
        version_id = str(app.get("online_version_id") or "")
        if not version_id:
            return {"error": "no_version", "detail": "应用还没有发布过版本"}
        version = api_get(profile, f"{base}/app_versions/{version_id}", {"lang": "zh_cn", "user_id_type": "open_id"},
                          runner=runner).get("app_version") or {}
    except ApiError as exc:
        return {"error": "no_self_manage" if exc.scope else "failed", "detail": str(exc)[:200]}
    except (CliFailed, ValueError, OSError, subprocess.SubprocessError) as exc:
        detail = exc.reason() if isinstance(exc, CliFailed) else type(exc).__name__
        return {"error": "failed", "detail": detail[:200]}
    bot = ((version.get("ability") or {}).get("bot")) or {}
    menus = [(str(item.get("event_key") or ""), str(item.get("default_name") or (item.get("i18n_name") or {}).get("zh_cn") or ""))
             for item in bot.get("bot_menus") or [] if isinstance(item, dict)]
    callbacks = (app.get("callback_info") or {}).get("subscribed_callbacks") or []
    return {"scopes": {str(s.get("scope")) for s in app.get("scopes") or [] if isinstance(s, dict)},
            "events": {str(e.get("event_type")) for e in version.get("event_infos") or [] if isinstance(e, dict)},
            "callbacks": {str(c) for c in callbacks},
            "bot": "bot" in (version.get("ability") or {}) or bool(bot),
            "menu_enable": bool(bot.get("bot_menu_enable")),
            "menu_keys": [key for key, _ in menus if key],
            "menus": menus,
            "version": str(version.get("version") or ""),
            "version_id": version_id}


def survey(profile, app_id, env=None, *, read=None):
    """(online, lacking, full) now; ``online`` carries ``error`` when the app could not be read. Without its own
    application:application:self_manage the app cannot be read: that scope alone is lacking (``blind``)."""
    online = (read or online_config)(profile, app_id)
    if online.get("error") == "no_self_manage":
        need = app_manifest.Need("scope", "application:application:self_manage", "检查应用配置")
        return online, [need], []
    if online.get("error"):
        return online, [], []
    lacking, full = app_manifest.missing(os.environ if env is None else env, online)
    return online, lacking, full


def playwright_python(env=None):
    """The interpreter that has Playwright, or None."""
    env = os.environ if env is None else env
    path = str(cenv.get(PYTHON_ENV, "", env)).strip() or str(data_home() / "proxy-web" / "venv" / "bin" / "python")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def remove(folder, log, tries=5):
    """``folder`` and all in it: again a few times, the browser's last writes may land while it is removed."""
    for _ in range(tries):
        shutil.rmtree(folder, ignore_errors=True)
        if not os.path.lexists(folder):
            return True
        time.sleep(1)
    log(f"app setup run folder not removed: {folder}")
    return False
