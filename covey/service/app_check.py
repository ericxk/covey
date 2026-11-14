#!/usr/bin/env python3
"""The Feishu app check and the console run from a terminal (bin/covey_app_setup.py), for the install and deploy
scripts and by hand; the Bridge does the same from the owner's private chat (app/app_setup.py).

    covey_app_setup.py check --profile P [--app-id ID] [--json]
        what the online app lacks (core/app_manifest.py, this shell's switches); exit 0 nothing to add,
        3 something to add, 1 the app could not be read
    covey_app_setup.py apply --profile P [--app-id ID] [--review] [--python PY] [--login-from DIR] [--work DIR]
        the console run (feishu/app_console.py): its login QR drawn here, then the official read-back;
        exit 0 when nothing is missing after it

The switches are read from this shell's environment, as the service will run with them (COVEY_*).
Standard library only; ``apply`` needs an interpreter with Playwright (``--python``, COVEY_PLAYWRIGHT_PYTHON,
else <data home>/proxy-web/venv/bin/python; scripts/install_console_browser.sh installs one).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from covey.core import app_manifest
from covey.core.paths import data_home
from covey.feishu.app_config import playwright_python, remove, survey
from covey.feishu.transport import cli_json, lark_argv

NOTHING, SOMETHING, UNREADABLE = 0, 3, 1


def profile_app_id(profile):
    """The appId of a lark-cli profile, '' when it has none."""
    try:
        payload = cli_json(lark_argv(profile, "config", "show"), timeout=20)
    except Exception:  # noqa: BLE001 - asked for by --app-id then
        return ""
    return str(payload.get("appId") or payload.get("app_id") or "") if isinstance(payload, dict) else ""


def profile_app_name(profile):
    """The name the app really has now (the phone's create page proposes its own and the user may keep it), read from
    the bot's info; '' when it cannot be read."""
    try:
        info = cli_json(lark_argv(profile, "api", "GET", "/open-apis/bot/v3/info", "--as", "bot"), timeout=30)
    except Exception:  # noqa: BLE001 - the closing line then does not name it
        return ""
    bot = (info.get("data") or info.get("bot") or {}) if isinstance(info, dict) else {}
    bot = bot.get("bot") if isinstance(bot, dict) and isinstance(bot.get("bot"), dict) else bot
    name = bot.get("app_name") if isinstance(bot, dict) else ""
    return name.strip() if isinstance(name, str) else ""


def draw(matrix, out=sys.stdout):
    """A QR's modules as half blocks, two rows a line: light is a block (dark terminals), two modules of margin."""
    if not matrix:
        return
    size = len(matrix[0])
    rows = ["0" * (size + 4)] * 2 + [f"00{row}00" for row in matrix]
    rows += ["0" * (size + 4)] * (2 + len(rows) % 2)  # An even count: two rows a line.
    for top, bottom in zip(rows[0::2], rows[1::2]):
        out.write("".join({("0", "0"): "█", ("0", "1"): "▀", ("1", "0"): "▄", ("1", "1"): " "}[(a, b)]
                          for a, b in zip(top, bottom)) + "\n")
    out.flush()


def check(args):
    online, lacking, full = survey(args.profile, args.app_id)
    offer = app_manifest.offered(lacking, full, review=getattr(args, "review", False))
    report = {"version": online.get("version", ""), "error": online.get("error", ""),
              "detail": online.get("detail", ""), "offer": [n.id for n in offer],
              "review": [n.id for n in lacking if n.review], "full": [n.id for n in full]}
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False))
    else:
        if online.get("error") and online.get("error") != "no_self_manage":
            print(f"读不到飞书应用的线上配置：{online.get('detail') or online['error']}")
        elif not offer:
            print(f"飞书应用（线上版本 {online.get('version') or '-'}）不缺 Bridge 要用的配置。")
        else:
            print(f"飞书应用（线上版本 {online.get('version') or '-'}）缺 {len(offer)} 项：")
            for need in offer:
                print(f"  - {need.words}（{need.name}）")
        for need in [n for n in lacking if n.review]:
            print(f"  需要管理员审核、默认不申请：{need.words}（{need.name}），加 --review 才申请")
        for need in full:
            print(f"  菜单已满放不下：{need.words}")
    if online.get("error") and online.get("error") != "no_self_manage":
        return UNREADABLE
    return SOMETHING if offer else NOTHING


def apply(args):
    online, lacking, full = survey(args.profile, args.app_id)
    if online.get("error") and online.get("error") != "no_self_manage":
        print(f"读不到飞书应用的线上配置：{online.get('detail') or online['error']}")
        return UNREADABLE
    offer = app_manifest.offered(lacking, full, review=args.review)
    if not offer:
        print("没有要补的。")
        return NOTHING
    python = args.python or playwright_python()
    if not python:
        print("没有装了 Playwright 的 Python：先运行 scripts/install_console_browser.sh，或用 --python 指定。")
        return UNREADABLE
    work = Path(args.work) if args.work else data_home() / "app-setup-runs"
    work.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    try:
        plan = app_manifest.plan(offer, remark=app_manifest.remark(offer))
        (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
        argv = [python, "-I", str(Path(__file__).resolve().parents[2] / "bin" / "covey_app_console.py"),
                "--app-id", args.app_id, "--home", str(folder / "home"), "--plan", str(folder / "plan.json")]
        if args.login_from:
            argv += ["--login-from", args.login_from]
        print(f"补 {len(offer)} 项：" + app_manifest.joined(offer))
        outcome = {}
        with subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, cwd=str(folder)) as proc:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("event")
                if kind == "qr":
                    print("\n用飞书手机端扫码登录开发者后台（过期会换新的）：")
                    draw(event.get("matrix") or [])
                    print(f"扫不了时打开图片：{event.get('path')}")
                elif kind == "login_page":
                    print(f"登录页显示：{event.get('text')}")
                elif kind == "logged_in":
                    print("已登录开发者后台。")
                elif kind == "step" and event.get("state") in ("done", "skipped"):
                    print(f"  {event.get('name')}: {event.get('state')}")
                elif kind in ("done", "failed"):
                    outcome = event
        if outcome.get("event") == "failed":
            print(f"卡在「{outcome.get('words')}」：{outcome.get('detail')}")
        elif outcome.get("event") == "done":
            status = {"published": "已发布", "reviewing": "已提交，等管理员审核", "unchanged": "没有改动"}
            print(f"版本 {outcome.get('version') or '-'}：{status.get(outcome.get('status'), outcome.get('status'))}")
    finally:
        remove(folder, print)
    _, after, after_full = survey(args.profile, args.app_id)
    still = [n for n in app_manifest.offered(after, after_full, review=args.review) if n.id in {o.id for o in offer}]
    for need in still:
        print(f"线上版本仍然缺：{need.words}（{need.name}）")
    return SOMETHING if still else NOTHING


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_app_setup.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--app-id", default="")
        command.add_argument("--review", action="store_true", help="also the scopes the tenant admin reviews")
    sub.choices["check"].add_argument("--json", action="store_true")
    sub.choices["apply"].add_argument("--python", default="")
    sub.choices["apply"].add_argument("--login-from", default="", help="a Feishu web login (browser profile) to copy")
    sub.choices["apply"].add_argument("--work", default="")
    args = parser.parse_args(argv)
    args.app_id = args.app_id or profile_app_id(args.profile)
    if not args.app_id:
        print("找不到应用的 appId：用 --app-id 指定。")
        return UNREADABLE
    return check(args) if args.cmd == "check" else apply(args)


if __name__ == "__main__":
    sys.exit(main())
