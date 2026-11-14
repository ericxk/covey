"""./install 用的浏览器会话：一个无头 Chromium 扫一次码，从头到尾替主人点完建应用、授权和开发者后台补配置。
在装了 Playwright 的 Python 下作为独立进程运行（service/installer.py 起它，bin/covey_install_browser.py 是入口）：

    covey_install_browser.py --home DIR [--shots DIR]

标准输入每行一条 JSON 命令，标准输出每行一个 JSON 事件（和 feishu/app_console.py 同一套 ``qr``、``login_page``、
``logged_in``、``step``）；每条命令以一个 ``result`` 事件结束（``ok``、``detail``）：

    {"cmd": "create_app", "url": ..., "name": ..., "seconds": 600}   lark-cli config init --new 给的链接：登录（画二维码）、
                                                                      填名称、点「创建」
    {"cmd": "authorize", "url": ..., "seconds": 300}                 lark-cli auth login 给的链接：点「开通并授权」
    {"cmd": "console", "app_id": ..., "plan": 计划文件, "seconds": 300}  开发者后台补回调、事件、菜单、权限并发布
                                                                      （feishu/app_console.py 的 Console.apply）
    {"cmd": "quit"}

浏览器登录态在 DIR/profile（0700），由调用方在安装结束时整个删掉，不保留主人的网页登录。只用标准库和 Playwright。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


from covey.core import browser_env
from covey.feishu.app_console import CONSOLE, Console, Stop, emit

CREATE_WORDS = "创建"
AUTHORIZE_WORDS = "开通并授权"
NAME_INPUT = "input[type=text], input:not([type])"


def visible_button(page, words):
    button = page.get_by_role("button", name=words, exact=True)
    try:
        return button.count() > 0 and button.first.is_visible()
    except Exception:  # noqa: BLE001 - a page between loads
        return False


def create_app(console, command):
    """Login when asked to, then fill the name and click 创建 on lark-cli's app page."""
    page = console.page
    console.login(int(command.get("seconds") or 600), start=command["url"],
                  ready=lambda: visible_button(page, CREATE_WORDS))
    field = page.locator(NAME_INPUT).first
    field.fill(str(command.get("name") or "Covey"), timeout=15000)
    page.get_by_role("button", name=CREATE_WORDS, exact=True).first.click(timeout=15000)
    page.wait_for_timeout(3000)
    return "已点创建"


def authorize(console, command):
    """lark-cli's device page: 开通并授权."""
    page = console.page
    console.login(int(command.get("seconds") or 300), start=command["url"],
                  ready=lambda: visible_button(page, AUTHORIZE_WORDS))
    buttons = page.get_by_role("button", name=AUTHORIZE_WORDS, exact=True)
    buttons.first.click(timeout=15000)
    # A confirmation dialog follows (确认开通并授权以下权限) with a second button of the same name: the last one is its.
    for _ in range(10):
        page.wait_for_timeout(1000)
        if buttons.count() >= 2:
            buttons.last.click(timeout=15000)
            break
    page.wait_for_timeout(3000)
    return "已点开通并授权"


def configure(console, command):
    console.app_id = str(command["app_id"])
    console.login(int(command.get("seconds") or 300))
    console.apply(json.loads(Path(command["plan"]).read_text()))
    return "done"


COMMANDS = {"create_app": create_app, "authorize": authorize, "console": configure}


def shot(console, shots, name):
    """The page as it was when a step failed (a few hundred KB; no login in it), so a failure can be seen, not guessed."""
    if not shots:
        return
    try:
        Path(shots).mkdir(parents=True, exist_ok=True)
        console.page.screenshot(path=str(Path(shots) / f"fail-{name}.png"))
        emit("step", name=f"失败时的页面 {console.page.url.split('?')[0]}", state="shot")
    except Exception:  # noqa: BLE001 - best effort
        pass


def serve(console, lines, shots=None):
    for line in lines:
        try:
            command = json.loads(line)
            name = command["cmd"]
        except (ValueError, KeyError, TypeError):
            continue
        if name == "quit":
            return 0
        action = COMMANDS.get(name)
        if action is None:
            emit("result", cmd=name, ok=False, detail="未知命令")
            continue
        try:
            emit("result", cmd=name, ok=True, detail=action(console, command))
        except Stop as stop:
            shot(console, shots, name)
            emit("result", cmd=name, ok=False, detail=f"{stop.step}: {stop.detail}")
        except Exception as exc:  # noqa: BLE001 - said in one line, the installer decides
            shot(console, shots, name)
            emit("result", cmd=name, ok=False, detail=f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_install_browser.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--home", required=True)
    parser.add_argument("--console", default=CONSOLE)
    parser.add_argument("--shots", help="失败时把页面截图存到这个目录")
    args = parser.parse_args(argv)
    browser_env.apply()
    from playwright.sync_api import sync_playwright

    from covey.feishu.web_client import launch

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    with sync_playwright() as playwright:
        context = launch(playwright, home)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            emit("ready")
            return serve(Console(page, args.console, "", home), sys.stdin, args.shots)
        finally:
            context.close()



if __name__ == "__main__":
    sys.exit(main())
