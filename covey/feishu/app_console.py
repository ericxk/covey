"""The Feishu developer console, driven by a headless Chromium logged in as the owner: what adds the scopes, events,
callback and bot menu items core/app_manifest.py plans, then publishes a version. Run by app/app_setup.py (and
bin/covey_app_setup.py) as its own process under an interpreter that has Playwright:

    covey_app_console.py --app-id ID --home DIR --plan PLAN.json [--login-from PROFILE] [--console URL]

It prints one JSON line per event on stdout: ``qr`` (``path``: the login QR as a PNG in DIR, again when it
changes; ``matrix``: its modules, rows of 1 and 0, for a terminal), ``login_page`` (``text``: a page between the
QR and the console, such as a device check), ``logged_in``, ``step`` (``name``, ``state`` doing/done/skipped),
``failed`` (``step``, ``detail``, ``shot``: the page then, a PNG in DIR) and ``done`` (``version``, ``status``
published, reviewing or unchanged, ``changed``). Exit 0 after ``done``, 1 after ``failed``.

The login: DIR/profile, a copy of ``--login-from`` (the owner's 代答 web login, <state>/proxy/web/profile) when
given, else empty; a page that asks for the QR shows it to the owner. The caller
removes DIR afterwards: no login is kept.

The console's own JSON calls (POST /developers/v1/…, as its pages make them, with the page's CSRF token):

    manifest/get, manifest/upsert   scopes, events, callbacks, the bot (schema 0.0.2); add only, as the console's
                                    own manifest merge does. It drops uploaded menu icons and names, so the menu
                                    goes after it, through
    robot/<app>, robot/update_changed/<app>   the bot menu as the console's menu editor saves it: the items kept
                                    as they were before (icons too), the Bridge's missing ones added with a
                                    built-in icon, never past the menu's limit
    app_version/list|detail|create, publish/commit   a version past the highest one, the online version's
                                    visibility and default abilities, 更新说明 from the plan; published, or
                                    reviewing when the tenant's admin has to approve it

A version the console holds unpublished (not this tool's) stops the run before any change: it is the owner's.
Standard library and Playwright only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from covey.core import browser_env

CONSOLE = "https://open.feishu.cn"
SCHEMA = "0.0.2"
LOGIN_SECONDS = 300
PUBLISH_SECONDS = 120
REMARK_PREFIX = "Harness Bridge 自动补齐"
CONDITIONAL_ACCESS_CODE = "conditional_access"
CONDITIONAL_ACCESS_URLS = ("/conditionalaccess/deny", "/conditionalaccess/")
CONDITIONAL_ACCESS_WORDS = ("设备信息授权", "条件访问", "未登记的设备", "受信任的设备")
CONDITIONAL_ACCESS_DETAIL = ("租户条件访问拦下了这台机器的网页登录（需在受信任设备的客户端完成设备信息授权）；"
                             "请改用手机扫码（不加 --browser 或使用 ./install --no-browser）")
# Not copied from the login: caches, and the lock files of a browser that has it open.
SKIPPED = ("Cache", "Code Cache", "Service Worker", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
           "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "IndexedDB", "Singleton*")
ONLINE, DRAFT, REVIEWING = 2, 0, 1  # app_version versionStatus.
QR = ".newLogin_scan-QR-code"  # The login page's QR block; any big canvas otherwise.
STEPS = {"login": "扫码登录", "read": "读取后台配置", "manifest": "写入权限与事件", "menu": "写入菜单",
         "version": "发布版本"}

CALL = """async ([path, body]) => {
  const r = await fetch(path, {method: 'POST', credentials: 'include', headers: {'content-type': 'application/json',
    'x-csrf-token': window.csrfToken || '', 'x-timezone-offset': String(new Date().getTimezoneOffset())},
    body: JSON.stringify(body)});
  return [r.status, await r.text()];
}"""
QR_DATA = f"""() => {{
  const block = document.querySelector('{QR}');
  const canvas = (block && block.querySelector('canvas')) || Array.from(document.querySelectorAll('canvas'))
    .find(c => c.width >= 120 && c.height >= 120);
  try {{ return canvas ? canvas.toDataURL() : ''; }} catch (e) {{ return ''; }}
}}"""
# The QR's modules, one string of 1 (dark) and 0 per row, for a terminal to draw (bin/covey_app_setup.py): the
# dark pixels' box, the module size from the top-left finder's first row (7 modules), each module's centre.
QR_MATRIX = f"""() => {{
  const block = document.querySelector('{QR}');
  const c = (block && block.querySelector('canvas')) || Array.from(document.querySelectorAll('canvas'))
    .find(x => x.width >= 120 && x.height >= 120);
  if (!c) return [];
  let d; try {{ d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; }} catch (e) {{ return []; }}
  const w = c.width, h = c.height;
  const dark = (x, y) => {{ const i = (y * w + x) * 4; return d[i + 3] > 128 && d[i] + d[i + 1] + d[i + 2] < 384; }};
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (dark(x, y)) {{
    if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y; }}
  if (x1 < 0) return [];
  let run = 0; while (x0 + run <= x1 && dark(x0 + run, y0)) run++;
  const m = run / 7, n = Math.round((x1 - x0 + 1) / m);
  if (!(m > 0) || n < 21 || n > 177) return [];
  const rows = [];
  for (let r = 0; r < n; r++) {{ let s = '';
    for (let k = 0; k < n; k++) s += dark(Math.floor(x0 + (k + 0.5) * m), Math.floor(y0 + (r + 0.5) * m)) ? '1' : '0';
    rows.push(s); }}
  return rows;
}}"""
VISIBLE_TEXT = ("Array.from(document.querySelectorAll('body *'))"
                ".filter(e => e.children.length === 0 && e.offsetParent !== null)"
                ".map(e => (e.innerText || '').trim()).filter(Boolean).join(' ').slice(0, 300)")


def conditional_access(url: str, text: str = "") -> bool:
    """Whether the browser landed on a Feishu conditional-access block page (e.g. DevBox lacking trusted-device auth)."""
    low = (url or "").lower()
    if any(part in low for part in CONDITIONAL_ACCESS_URLS):
        return True
    return bool(text) and any(word in text for word in CONDITIONAL_ACCESS_WORDS)


class Stop(Exception):
    def __init__(self, step, detail, *, code=""):
        self.step, self.detail, self.code = step, detail, code
        super().__init__(f"{step}: {detail}")


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def copy_login(source, profile):
    """The owner's web login copied into ``profile`` (0700), without caches; False when there is none."""
    source = Path(source)
    if not (source / "Default").is_dir():
        return False
    shutil.copytree(source, profile, ignore=shutil.ignore_patterns(*SKIPPED), symlinks=False)
    for path in (profile, *profile.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, 0o700)
    return True


def version_after(versions):
    """The number past every version the app has: 1.0.11 -> 1.0.12; 1.0.0 when there is none."""
    best = (1, 0, -1)
    for item in versions:
        parts = str(item.get("appVersion") or "").split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            best = max(best, tuple(int(part) for part in parts))
    return f"{best[0]}.{best[1]}.{best[2] + 1}"


def ids(group):
    """The console's visibility block (a version's ``visibleRange``) as the create call takes it."""
    group = group if isinstance(group, dict) else {}
    pick = lambda key: [str(x.get("id")) for x in group.get(key) or [] if isinstance(x, dict) and x.get("id")]
    return {"isAll": int(group.get("isAll") or 0), "departments": pick("departments"), "members": pick("members"),
            "groups": pick("groups")}


def merge(manifest, plan):
    """``manifest`` (the console's, schema 0.0.2) with ``plan``'s additions; nothing is taken out."""
    merged = json.loads(json.dumps(manifest))
    scopes = merged.setdefault("scopes", {})
    tenant = scopes.setdefault("tenant", [])
    tenant += [scope for scope in plan.get("scopes", []) if scope not in tenant]
    events = merged.setdefault("events", {})
    items = events.setdefault("items", {})
    tenant_events = items.setdefault("tenant", [])
    tenant_events += [event for event in plan.get("events", []) if event not in tenant_events]
    if plan.get("events"):
        events["subscription_type"] = events.get("subscription_type") or "websocket"
    callbacks = merged.setdefault("callbacks", {})
    callback_items = callbacks.setdefault("items", [])
    callback_items += [name for name in plan.get("callbacks", []) if name not in callback_items]
    if plan.get("callbacks"):
        callbacks["subscription_type"] = callbacks.get("subscription_type") or "websocket"
    if plan.get("bot"):
        merged.setdefault("bot", {})["enable"] = True
    return merged


def menu_item(item):
    """One menu item as the console's menu editor saves it (its own transform before robot/update_changed)."""
    kind = int(item.get("menuContentType") or 0)
    names = item.get("i18nBotMenuName") or {}
    out = {"i18nBotMenuName": names, "defaultName": names.get("zh_cn") or item.get("defaultName") or "",
           "menuContentType": kind}
    if item.get("botMenuID"):
        out["botMenuID"] = item["botMenuID"]
    if kind == 2 and item.get("eventKey"):
        out["eventKey"] = item["eventKey"]
    if kind == 1 and item.get("redirectLink"):
        out["redirectLink"] = item["redirectLink"]
    if (item.get("udIcon") or {}).get("token"):
        out["udIcon"] = item["udIcon"]
    if item.get("iconImageUrl"):
        out["iconImageUrl"] = item["iconImageUrl"]
    out["childNodes"] = [menu_item(child) for child in item.get("childNodes") or []]
    return out


def item_name(item):
    return item.get("eventKey") or (item.get("i18nBotMenuName") or {}).get("zh_cn") or item.get("defaultName") or ""


def desired_menu(before, after, plan):
    """The menu to save: ``before``'s items (the robot config before the manifest write, icons and all) under the
    IDs ``after`` gives them, then the plan's missing items while there is room; enabled, 悬浮 unless set. An item
    the console has no ID for gets a small number, as its editor numbers new items (getNewMenu: "" + ++counter);
    the save gives it a real one (without any, it answers 9499 Bad Request)."""
    ids_now = {item_name(item): item.get("botMenuID") for item in after.get("botMenuConfig") or []}
    fresh = iter(str(number) for number in range(1, 100))
    items = []
    gone = set(plan.get("menu_remove") or ())
    for item in before.get("botMenuConfig") or []:
        if item.get("eventKey") in gone:  # A stale item core/app_manifest.py STALE_MENU_KEYS names.
            continue
        items.append(menu_item({**item, "botMenuID": ids_now.get(item_name(item)) or next(fresh)}))
    limit = int(before.get("maxFloatingMenuCount") or 5)
    have = {item.get("eventKey") for item in items}
    for want in plan.get("menu", []):
        if want["key"] in have or len(items) >= limit:
            continue
        items.append({"botMenuID": next(fresh), "i18nBotMenuName": {"zh_cn": want["label"]},
                      "defaultName": want["label"], "menuContentType": 2, "eventKey": want["key"],
                      "udIcon": {"token": want["icon"]}, "childNodes": []})
    enabled = bool(before.get("botMenuEnable"))
    return {"botMenuEnable": enabled or bool(plan.get("menu_enable") or plan.get("menu")),
            "botMenuDisplayStrategy": int(before.get("botMenuDisplayStrategy") or 0) if enabled
            else int(plan.get("menu_display") or 3),
            "botMenuConfig": items}


def menu_shape(config):
    """What two menus are compared by: on, display, and each item's key or name, shown name and icon."""
    def shape(item):
        return (item_name(item), (item.get("i18nBotMenuName") or {}).get("zh_cn") or "", item.get("defaultName") or "",
                (item.get("udIcon") or {}).get("token") or "", item.get("iconImageUrl") or "",
                tuple(shape(child) for child in item.get("childNodes") or []))
    return (bool(config.get("botMenuEnable")), int(config.get("botMenuDisplayStrategy") or 0),
            tuple(shape(item) for item in config.get("botMenuConfig") or []))


def pending_version(versions):
    """The newest version neither online nor done with (a draft or one in review), or None."""
    for item in versions:
        if int(item.get("versionStatus", -1)) in (DRAFT, REVIEWING):
            return item
        if int(item.get("versionStatus", -1)) == ONLINE:
            return None
    return None


class Console:
    def __init__(self, page, base, app_id, home):
        self.page, self.base, self.app_id, self.home = page, base.rstrip("/"), app_id, Path(home)
        self.changed = []

    # The login -------------------------------------------------------------------------------

    def on_console(self):
        host = self.page.url.split("/")[2] if self.page.url.count("/") >= 2 else ""
        return host.startswith("open.") and "/accounts/" not in self.page.url

    def has_token(self):
        """The console page's CSRF token, which its own calls send, is there (it is set once the page loaded)."""
        try:
            return bool(self.page.evaluate("() => Boolean(window.csrfToken)"))
        except Exception:  # noqa: BLE001 - a page between loads
            return False

    def logged_in(self):
        try:
            status, text = self.page.evaluate(CALL, ["/napi/check/login", {}])
            payload = json.loads(text)
        except Exception:  # noqa: BLE001 - a page between loads
            return False
        return status == 200 and payload.get("code") == 0 and bool((payload.get("data") or {}).get("id"))

    def login(self, seconds=LOGIN_SECONDS, start=None, ready=None):
        """Scan-login: ``start`` is the page to open (default the app's console page), ``ready()`` says when the page
        wanted is there (default: the console is logged in); the QR is shown while a login page is in the way."""
        self.page.goto(start or f"{self.base}/app/{self.app_id}", wait_until="domcontentloaded", timeout=60000)
        deadline, shown, said, number = time.monotonic() + seconds, "", "", 0
        while time.monotonic() < deadline:
            if ready() if ready else (self.on_console() and self.has_token() and self.logged_in()):
                emit("logged_in")
                return
            if not self.on_console():
                url = str(getattr(self.page, "url", "") or "")
                if conditional_access(url):
                    text = self.visible_text()
                    emit("login_page", text=text or "条件访问拦截", reason=CONDITIONAL_ACCESS_CODE)
                    raise Stop("login", CONDITIONAL_ACCESS_DETAIL, code=CONDITIONAL_ACCESS_CODE)
            data = self.qr_data() if not self.on_console() else ""
            if data and data != shown:
                number += 1
                path = self.home / f"qr-{number}.png"
                self.qr_shot(path)
                shown = data
                try:
                    matrix = self.page.evaluate(QR_MATRIX)
                except Exception:  # noqa: BLE001
                    matrix = []
                emit("qr", path=str(path), number=number, matrix=matrix)
            elif not data and not self.on_console():
                text = self.visible_text()
                if conditional_access(str(getattr(self.page, "url", "") or ""), text):
                    emit("login_page", text=text or "条件访问拦截", reason=CONDITIONAL_ACCESS_CODE)
                    raise Stop("login", CONDITIONAL_ACCESS_DETAIL, code=CONDITIONAL_ACCESS_CODE)
                if text and text != said:
                    said = text
                    emit("login_page", text=text)
                self.refresh_qr()
            self.page.wait_for_timeout(1500)
        raise Stop("login", f"{seconds // 60} 分钟内没有完成扫码登录")

    def qr_data(self):
        try:
            data = self.page.evaluate(QR_DATA)
        except Exception:  # noqa: BLE001
            return ""
        return hashlib.sha256(data.encode()).hexdigest() if data else ""

    def qr_shot(self, path):
        block = self.page.locator(QR)
        target = block.first if block.count() else self.page.locator("canvas").first
        target.screenshot(path=str(path), timeout=15000)
        os.chmod(path, 0o600)

    def refresh_qr(self):
        """An expired QR shows a 刷新 link over it; click it."""
        for words in ("刷新", "重新获取"):
            link = self.page.get_by_text(words, exact=True)
            try:
                if link.count() and link.first.is_visible():
                    link.first.click(timeout=5000)
                    return
            except Exception:  # noqa: BLE001
                return

    def visible_text(self):
        try:
            return " ".join(str(self.page.evaluate(VISIBLE_TEXT)).split())
        except Exception:  # noqa: BLE001
            return ""

    # The console's calls ---------------------------------------------------------------------

    def call(self, step, path, body):
        """One console call's ``data``; Stop naming the call (its path without the app and version IDs)."""
        name = "/".join(part for part in path.split("/")[3:] if not part.startswith("cli_") and not part.isdigit())
        status, text = self.page.evaluate(CALL, [path, body])
        try:
            payload = json.loads(text)
        except ValueError:
            raise Stop(step, f"后台接口 {name or path} 返回 HTTP {status}") from None
        if payload.get("code") != 0:
            raise Stop(step, f"后台接口 {name or path} 报错 {payload.get('code')} {payload.get('msg') or ''}".strip())
        return payload.get("data") or {}

    def manifest(self, step):
        data = self.call(step, f"/developers/v1/manifest/get/{self.app_id}",
                         {"clientID": self.app_id, "schemaVersion": SCHEMA, "HTTPHead": {}})
        try:
            return json.loads(data.get("appManifest") or "")
        except ValueError:
            raise Stop(step, "后台没有返回应用清单") from None

    def robot(self, step):
        return self.call(step, f"/developers/v1/robot/{self.app_id}", {})

    def versions(self, step):
        return self.call(step, f"/developers/v1/app_version/list/{self.app_id}", {}).get("versions") or []

    # The work --------------------------------------------------------------------------------

    def apply(self, plan):
        emit("step", name="read", state="doing")
        before_robot, manifest, versions = self.robot("read"), self.manifest("read"), self.versions("read")
        me = str(self.call("read", "/napi/check/login", {}).get("id") or "")
        pending = pending_version(versions)
        if pending and not str(pending.get("updateRemark") or "").startswith(REMARK_PREFIX):
            raise Stop("read", f"后台已有未发布的版本 {pending.get('appVersion')}，它不是 Bridge 建的；"
                               "请先在开发者后台处理它")
        emit("step", name="read", state="done")

        emit("step", name="manifest", state="doing")
        merged = merge(manifest, plan)
        if merged != manifest:
            self.call("manifest", "/developers/v1/manifest/upsert",
                      {"clientID": self.app_id, "appManifest": json.dumps(merged, ensure_ascii=False), "HTTPHead": {}})
            now = self.manifest("manifest")
            lost = [s for s in plan.get("scopes", []) if s not in (now.get("scopes") or {}).get("tenant", [])]
            lost += [e for e in plan.get("events", [])
                     if e not in ((now.get("events") or {}).get("items") or {}).get("tenant", [])]
            lost += [c for c in plan.get("callbacks", []) if c not in (now.get("callbacks") or {}).get("items", [])]
            if lost:
                raise Stop("manifest", "写入后后台仍然没有：" + "、".join(lost[:6]))
            self.changed.append("manifest")
        emit("step", name="manifest", state="done" if merged != manifest else "skipped")

        emit("step", name="menu", state="doing")
        after_robot = self.robot("menu")
        want = desired_menu(before_robot, after_robot, plan)
        if menu_shape(want) != menu_shape(after_robot):
            self.call("menu", f"/developers/v1/robot/update_changed/{self.app_id}", {"clientId": self.app_id, "menu": want})
            if menu_shape(self.robot("menu")) != menu_shape(want):
                raise Stop("menu", "保存后后台的菜单和要写的不一样")
            self.changed.append("menu")
        emit("step", name="menu", state="done" if "menu" in self.changed else "skipped")

        # The plan holds only what the online version lacks: it needs a version even when an earlier run already
        # wrote it to the console (nothing changed here).
        emit("step", name="version", state="doing")
        wanted = any(plan.get(key) for key in ("scopes", "events", "callbacks", "bot", "menu", "menu_remove"))
        if not self.changed and not wanted and not pending:
            emit("step", name="version", state="skipped")
            emit("done", version="", status="unchanged", changed=[])
            return
        status = int(pending.get("versionStatus", -1)) if pending else -1
        if status == REVIEWING:
            version_id, number = str(pending.get("versionId")), str(pending.get("appVersion"))
            if self.changed:
                raise Stop("version", f"Bridge 上次提交的版本 {number} 还在审核中，这次的改动要等它审完再发布")
        else:
            if status == DRAFT:  # An earlier run's draft: this run's version holds its changes too.
                self.call("version", f"/developers/v1/app_version/delete/{self.app_id}/{pending.get('versionId')}", {})
                versions = self.versions("version")
            number = version_after(versions)
            version_id = self.create_version(versions, number, plan.get("remark") or REMARK_PREFIX, me)
            self.call("version", f"/developers/v1/publish/commit/{self.app_id}/{version_id}", {})
        deadline = time.monotonic() + PUBLISH_SECONDS
        while time.monotonic() < deadline:
            status = int(self.call("version", f"/developers/v1/app_version/detail/{self.app_id}/{version_id}",
                                   {}).get("versionStatus", -1))
            if status == ONLINE:
                break
            self.page.wait_for_timeout(5000)
        emit("step", name="version", state="done")
        emit("done", version=number, status="published" if status == ONLINE else "reviewing", changed=self.changed)

    def create_version(self, versions, number, remark, me):
        online = next((v for v in versions if int(v.get("versionStatus", -1)) == ONLINE), None)
        detail = self.call("version", f"/developers/v1/app_version/detail/{self.app_id}/{online['versionId']}",
                           {}) if online else {}
        visible = ids(detail.get("visibleRange")) if detail else {"isAll": 0, "departments": [], "members": [me],
                                                                    "groups": []}
        hidden = ids((detail.get("visibleRange") or {}).get("blackList")) if detail else ids({})
        body = {"appVersion": number, "changeLog": remark[:500], "visibleSuggest": visible,
                "blackVisibleSuggest": hidden, "pcDefaultAbility": detail.get("pcDefaultAbility") or "bot",
                "mobileDefaultAbility": detail.get("mobileDefaultAbility") or "bot"}
        version_id = str(self.call("version", f"/developers/v1/app_version/create/{self.app_id}", body).get("versionId") or "")
        if not version_id:
            raise Stop("version", "后台没有返回新版本的 ID")
        return version_id


def run(args, playwright):
    from covey.feishu.web_client import launch

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    plan = json.loads(Path(args.plan).read_text())
    if args.login_from and not (home / "profile").exists():
        emit("login_copy", copied=copy_login(args.login_from, home / "profile"))
    context = launch(playwright, home)
    page = context.pages[0] if context.pages else context.new_page()
    console = Console(page, args.console, args.app_id, home)
    step = "login"
    try:
        console.login(args.login_seconds)
        step = "read"
        console.apply(plan)
        return 0
    except Stop as stop:
        shot = home / f"failed-{stop.step}.png"
        try:
            page.screenshot(path=str(shot))
            os.chmod(shot, 0o600)
        except Exception:  # noqa: BLE001
            shot = None
        emit("failed", step=stop.step, words=STEPS.get(stop.step, stop.step), detail=stop.detail,
             shot=str(shot) if shot else "", changed=console.changed, **({"code": stop.code} if stop.code else {}))
        return 1
    except Exception as exc:  # noqa: BLE001 - reported as the step it happened in
        emit("failed", step=step, words=STEPS.get(step, step), detail=f"{type(exc).__name__}: {str(exc)[:200]}",
             shot="", changed=console.changed)
        return 1
    finally:
        context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_app_console.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--login-from", default="")
    parser.add_argument("--console", default=CONSOLE)
    parser.add_argument("--login-seconds", type=int, default=LOGIN_SECONDS)
    args = parser.parse_args(argv)
    browser_env.apply()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return run(args, playwright)


if __name__ == "__main__":
    sys.exit(main())
