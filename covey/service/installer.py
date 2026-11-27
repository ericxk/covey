"""./install 的安装器本体：一遍跑完，手机扫码，官方斜杠命令自动注册，最后发一句话就有回答（docs/install.md）。

步骤（每步一行标题，之后只有必要的提示）：
  1 检查环境和目录   Python、平台；数据根、工作区、临时目录、浏览器和 lark-cli 的安装位置都给默认值并自动建；在检出里运行、
                    没给 --workdir 时终端里问一次默认工作区（回车用 <数据根>/home，新建的空目录里放一份说明
                    core/home.py seed_home）；重跑沿用上次注册的工作区（install.json）
  2 准备 lark-cli   没有就装（npm 全局，不行装到数据根下 tools/）；``--skills`` 才同步 Skills
  3 检查 Harness    对 claude、codex、agy 各做一次真实调用（service/install_probe.py），分清没登录、网络不通、其他；PATH 里
                    没有的再看 ~/.local/bin、~/.claude/local（找到就把目录加进服务的 PATH）；没装的合成一句“不用每个都装”；
                    装了 dsh 且它的网关密钥在文件里就启用（不调用模型），没装的不提；
                    像网络问题时问一次代理地址；通过后把代理写进服务环境；扩展的安装钩子在这里跑；重跑时这次能用、上次写
                    harnesses.json 时不能用的加进去（install-harnesses.json 记每次的结果）。Claude Code 没调通时（docs/install.md 没有 Claude Code 时）新服务的管家写成
                    调通了的 Codex 或 Antigravity（core/butler_settings.py preset_harness，第一次启动前）；专项、整理专项这些
                    开关照常写（负责人和小模型判断都能跑在它们上面），--showcase 也一样
  4 飞书应用        默认手机扫码 3 步（建应用、授权、事件与回调一键订阅）+ Bot OpenAPI 自动注册斜杠命令（feishu/slash_commands.py），
                    无需浏览器和电脑端设备认证；显式加 ``--browser`` 时改用无头浏览器扫一次码（feishu/install_browser.py），
                    遇到浏览器起不来或条件访问拦截自动切回手机扫码
  5 注册并启动服务   scripts/bootstrap_harness_bridge.sh，先 bin/covey_service_install.py --mode auto --activate（托管），再 bootstrap（注册）；
                    先用将注册的 python3 给检出预编译 .pyc（scripts/precompile_release.py，失败只提示）；终端里有
                    Harness 可能靠它登录的变量（API key、网关地址）时，按服务自己的环境再探测一次（check_service_env）
  6 等待就绪        bin/covey_doctor.py 退出码 0（没就绪时打出它的中文摘要）；在终端里运行时接着等私聊里第一句话真的有了回答
                    （core/welcome.py first_answer，最多 ANSWER_SECONDS，每到一步一行；Ctrl-C 不等，超时不算失败，只说该查什么）

停下时（Fail，或命令超时、文件写不了）打印停在哪、下一步做什么和安装日志在哪；装好时最后几行写数据根、状态目录、默认工作区、
服务开着的功能和多开的命令（演示套餐、单个开关），再请主人去私聊发一句话。
幂等：已有的 lark-cli profile、授权、应用配置、服务都复用，只做缺的；``--reconfigure`` 才重来。
浏览器登录态放本次运行的临时目录，结束时删掉（``--keep-login`` 留下并说明路径）。
网页工作台（可选，默认不做）：``--web`` / ``--web-public ADDR``，或终端里在 Node 22+ 和 npm 都有、服务没开过它时问一次；要了就在
第 3 步之后后台构建（web/deploy/selfhost/build.sh，日志 install-logs/web-build.log），第 5 步注册前等它，成功才用
service/web_selfhost.py prepare 备好开关写进服务清单；Node 不够、构建失败只在“还差”里说，不挡服务。没选就什么都不做。
核心里没有任何公司的名词：扩展的钩子（extensions/<包>/scripts/setup_hook.sh）按它自己的约定在步骤 3 里被调用。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from covey.core import app_manifest, butler_settings, claude_retention, extensions, gateways, home, qr, switches, welcome
from covey.core import env as cenv
from covey.core.paths import REPO_ROOT, data_home
from covey.core.process import service_key
from covey.core.state import read_json, write_json
from covey.feishu import slash_commands
from covey.feishu.app_config import remove, survey
from covey.feishu.app_console import CONDITIONAL_ACCESS_CODE
from covey.service import browser_setup, install_probe, web_selfhost
from covey.service.app_check import draw, profile_app_id, profile_app_name
from covey.service.manager import (PROXY_ENV_KEYS, SECRET_NAME, change_settings, manifest_path, persistable,
                                   runtime_environment, service_environment, validate_environment)

# What ./install switches on in the service it registers (docs/install.md): the code's default stays off (AGENTS.md rule 4).
# One table with the 演示套餐 `--showcase` writes instead (core/switches.py SHOWCASE).
INSTALL_SWITCHES = switches.INSTALL
# Settings of a registered service a re-run does not carry over (keep_registered): dev-only names that must never be in a
# manifest (the deploy pre-check stops them), and what this run decides itself (PATH, LANG, the proxy).
NOT_KEPT = frozenset({"COVEY_EXTENSIONS", "COVEY_EXTENSION_PATH", "PATH", "LANG"}) | PROXY_ENV_KEYS

DEFAULT_PROFILE = "covey"
DEFAULT_APP_NAME = "Covey"
LARK_DOMAINS = "im,docs,drive,contact"
FEISHU_NO_PROXY = ".feishu.cn,.larksuite.com,.larkoffice.com,.feishucdn.com,localhost,127.0.0.1"  # privacy: allow 飞书自己的域名
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY")
PROBED = ("claude", "codex", "agy")  # The Harnesses one real call proves.
# Enabled without a model call where they are set up (set_up_harnesses): dsh installed and its gateway's key in a file
# the service reads. Not installed, nothing is said: no one has to install every Harness.
GATEWAY_HARNESSES = ("dsh",)
# Where a Harness CLI's own installer may have put it while this shell's PATH lacks it (Claude Code's native installer:
# ~/.local/bin; its older local install: ~/.claude/local, reached through a shell alias a child process never sees).
# Found there, its folder goes into this run's PATH, which the service is registered with (locate).
COMMON_DIRS = (".local/bin", ".claude/local")
STEPS = 6
# What the last run that wrote harnesses.json found usable, under the data root (last_harnesses): a re-run adds what is
# usable since, also after a run that stopped before the registration wrote install.json.
USABLE_RECORD = "install-harnesses.json"
URL = re.compile(r"https://\S+")
RELEASES = "https://nodejs.org/"  # Where a machine without npm gets Node.js.
ANSWER_SECONDS = 600  # How long a run in a terminal waits for the first answer in the private chat (docs/install.md).
MINUTES = ANSWER_SECONDS // 60
# What the wait says once the first message gets that far (core/welcome.py first_answer), each once; RECEIVED after LAUNCH
# is the launch card started.
ANSWER_WORDS = {welcome.LAUNCH: "收到了：话题里出了启动卡，点「开始任务」它才开工",
                welcome.RECEIVED: "收到了，在回答…",
                welcome.STOPPED: "这一轮停住了，卡上写了原因：点「检查并继续」，或再发一句",
                welcome.ANSWERED: "已回答，装好了。"}
STARTED_WORDS = "开工了，在回答…"
# What a wait that ran out says, by how far it got. Never a failure of the install.
TIMEOUT_WORDS = {
    welcome.NONE: f"{MINUTES} 分钟内没等到私聊里的消息（不影响安装，机器人照常在运行）。发了没反应的话，看 docs/troubleshooting.md "
                  "的「扫码后机器人没反应」。",
    welcome.LAUNCH: "启动卡还在等你点「开始任务」（不影响安装）。",
    welcome.RECEIVED: f"收到了，但 {MINUTES} 分钟内还没回答完（不影响安装）。看飞书里的任务卡；没动静时看 docs/troubleshooting.md "
                      "的「发消息后没有反应」。",
}
TIMEOUT_WORDS[welcome.STOPPED] = TIMEOUT_WORDS[welcome.RECEIVED]
NO_BROWSER = "./install --no-browser（不用浏览器，每一步画一个二维码用手机扫）"
AGAIN = "重新运行 ./install，做完的步骤会复用"
# What a Harness CLI reads for its account or endpoint, by name prefix (never a value is read out): a shell may have them,
# a service never gets them, since a manifest keeps no credential (service/manager.py persistable). check_service_env.
ACCOUNT_PREFIXES = {"claude": ("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "CLOUD_ML_", "GOOGLE_APPLICATION_CREDENTIALS"),
                    "codex": ("OPENAI_", "AZURE_OPENAI_", "CODEX_"),
                    "agy": ("GEMINI_", "GOOGLE_GEMINI_", "AGY_")}
# The words the closing lines name a service's switches by (docs/install.md 装好以后, docs/configuration.md 演示套餐); every
# switch of core/switches.py INSTALL and SHOWCASE has one (a test keeps them so), the ones on unset (DEFAULTS) are said on
# unless the manifest sets them off.
SWITCH_WORDS = {"COVEY_PROJECTS": "专项", "COVEY_PROJECT_AUTOWAKE": "叫醒负责人", "COVEY_PROJECT_ASK": "停住问你",
                "COVEY_PROJECT_MEMORY": "专项记忆", "COVEY_PROJECT_TRIAGE": "问题分流", "COVEY_PROJECT_TIDY": "整理专项",
                "COVEY_REVIEW": "独立复核", "COVEY_REVISIT": "到点回来看", "COVEY_MEMORY": "记忆",
                "COVEY_KNOWLEDGE": "知识条目", "COVEY_COLLAB": "协作（开发中）", "COVEY_PINNED_OVERVIEW": "置顶的「手上的事」",
                "COVEY_PINNED_TOPICS": "话题看板", "COVEY_DIGEST_TIMES": "早晚汇总", "COVEY_BUTLER_TRACKS": "管家同时做几件事",
                "COVEY_DISPATCH_CARD": "派发卡", "COVEY_EVENT_TRANSPORT": "飞书 SDK 收消息（收下先落盘，重启不丢）",
                "COVEY_CARD_TRANSPORT": "飞书 SDK 写卡片"}
# pyproject.toml's ``feishu-sdk``: what the service's Python needs for switches.SDK_SWITCHES (a test keeps the two the same).
SDK_REQUIREMENTS = ("lark-oapi==1.7.4", "cryptography>=43")
SDK_PROBE = ("import importlib.metadata as m, sys; import cryptography; "
             "sys.exit(0 if m.version('lark-oapi') == sys.argv[1] else 3)")
PROJECT_PARTS = ("COVEY_PROJECT_AUTOWAKE", "COVEY_PROJECT_ASK", "COVEY_PROJECT_MEMORY", "COVEY_PROJECT_TRIAGE")
PROJECT_SWITCHES = ("COVEY_PROJECTS", "COVEY_PROJECT_TIDY", *PROJECT_PARTS)  # What harness_lines turns on where 专项 is set off.
HARNESS_TITLES = {"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity", "dsh": "DeepSeek Harness"}
# Besides Claude Code, the probed Harnesses that can run the butler and a project's lead (core/harness.py bridge_tools), in
# the roster's order: where Claude Code did not answer, a new service's butler is preset to the first of them that did.
BUTLER_HARNESSES = ("codex", "agy")
WEB_NODE_MAJOR = 22  # web/deploy/selfhost/build.sh wants Node 22 or newer.
WEB_BUILD_SECONDS = 1200  # A build that takes longer than this is a failure (the page is left for later).
WEB_BUILD_SCRIPT = "web/deploy/selfhost/build.sh"
WEB_BUILD_MARK = ".covey-selfhost-build"  # What build.sh writes in a folder it built: its presence means a re-run upgrades.
YES = ("y", "yes", "是", "要", "好")
UPGRADER = "upgrade.json"  # Under the data root: app/self_upgrade.py's config, written by write_upgrader.


def butler_preset(working) -> str:
    """The Harness a new service's butler is preset to (D3, docs/install.md 没有 Claude Code 时): '' when Claude Code answered
    (its default stays) or none of BUTLER_HARNESSES did, else the first of them that did (``working``: the Harnesses that
    answered). Nothing else changes with it: 专项, 整理专项 and the small model's judgments run on it too."""
    if "claude" in working:
        return ""
    return next((name for name in BUTLER_HARNESSES if name in working), "")


def say_now(text):
    """print that reaches a pipe or a log at once (a killed run still shows what it said)."""
    print(text, flush=True)


class Fail(Exception):
    """A step that cannot go on; the message is what the user reads, ``next`` what to do about it (a command or a step),
    printed under it as 下一步."""

    def __init__(self, message, next=""):
        super().__init__(message)
        self.next = next


class ConditionalAccessFallback(Exception):
    """Browser login hit tenant conditional access: close Chromium and switch to phone QR."""


def parser():
    p = argparse.ArgumentParser(prog="covey_install.py", description=__doc__.split("\n\n")[0] + "\n\n" +
                                "前提：这台机器上已装好并登录了 Claude Code、Codex 或 Antigravity（见 docs/harnesses.md）。")
    p.add_argument("--reconfigure", action="store_true", help="重来：重新建应用、授权、补配置并重注册服务")
    p.add_argument("--skills", action="store_true", help="同时把 skills/ 同步到 Codex 和 Claude 的 Skills 目录（默认不做）")
    p.add_argument("--non-interactive", action="store_true", help="不提问（代理地址等）；缺什么就报什么退出")
    p.add_argument("--data-home", help="数据根（默认：COVEY_DATA_HOME，没设时是仓库同级的 <仓库目录名>-data）")
    p.add_argument("--workdir", help="默认工作区（默认：当前目录；在仓库里运行时用 <数据根>/home）")
    p.add_argument("--keep-login", action="store_true", help="结束后不删浏览器里主人的网页登录（默认删）")
    p.add_argument("--browser", action="store_true", help="改用无头浏览器扫一次码完成建应用和授权（默认不用浏览器、直接手机扫码，"
                   "避免无头 Linux 触发条件访问设备认证；若遇条件访问仍会自动切回手机扫码）")
    p.add_argument("--no-browser", action="store_true", help="不用无头浏览器：每个链接画成二维码，手机上依次打开（已是默认，保留以兼容）")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help=f"lark-cli profile 名（默认 {DEFAULT_PROFILE}）")
    p.add_argument("--app-name", default=DEFAULT_APP_NAME, help=f"新建飞书应用的名称（默认 {DEFAULT_APP_NAME}）")
    p.add_argument("--browser-login-dir", help=argparse.SUPPRESS)  # 验收用：浏览器从这份已登录的用户目录副本开始，跳过二维码
    p.add_argument("--no-autostart", action="store_true", help="没有 systemd/launchd 时不往 crontab 加开机自启的一行")
    p.add_argument("--no-start", action="store_true", help="只准备到飞书应用为止，不注册、不启动服务")
    p.add_argument("--showcase", action="store_true", help="演示套餐：新装默认开的之外，再开协作（开发中）、置顶的手上的事和"
                   "话题看板、早晚汇总、管家同时做几件事、派发卡（docs/configuration.md 演示套餐）")
    p.add_argument("--web", action="store_true", help="在这台机器上自托管网页工作台（要 Node 22+ 和 npm，约 1 GB、几分钟；"
                   "docs/install.md 网页工作台）")
    p.add_argument("--web-public", metavar="ADDR", help="网页工作台也开公网 HTTPS 直连（隐含 --web）：IP 或域名，逗号分隔；"
                   "auto 用本机出网网卡的公网地址")
    return p


def stream_lines(proc):
    """A queue of the lines of ``proc``'s output, None at its end (read in a thread so waits can time out)."""
    lines = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        proc.stdout.close()
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return lines


def boot_words(path):
    """One plain line about the hosting registration the installer printed as JSON (its last line)."""
    try:
        row = json.loads(path.read_text(errors="replace").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return "已注册"
    if row.get("autostart") is False:
        return "没有 systemd/launchd，Manager 以后台进程运行；重启机器后要再运行 ./install"
    if row.get("mode") == "cron":
        return "crontab 的 @reboot 一行（重启机器后自动起；不想要加 --no-autostart）"
    return {"systemd-user": "systemd 用户服务", "launchd": "launchd 用户代理"}.get(row.get("mode"), str(row.get("mode") or "已注册"))


def meaningful(lines):
    """The lines that say something: not blank, not only braces and commas of a JSON dump."""
    return [x.strip() for x in lines if x.strip(" \t{}[],")]


def flow_name(argv):
    return "flow-auth-login" if "auth" in argv else "flow-config-init"


class BrowserSession:
    """The headless browser (feishu/install_browser.py) as a process: JSON commands in, events out."""

    def __init__(self, python, home, say, env):
        self.say, self.last = say, {}
        self.proc = subprocess.Popen(
            [python, "-I", str(REPO_ROOT / "bin" / "covey_install_browser.py"), "--home", str(home), "--shots", str(Path(home).parent.parent / "install-logs")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(home), env=env)
        self.lines = stream_lines(self.proc)
        self.wait_for("ready", 90)

    def wait_for(self, kind, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                raise Fail("浏览器进程意外退出", next=f"{AGAIN}；还不行就 {NO_BROWSER}")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if self.handle(event) == kind:
                return event
        raise Fail("浏览器没有响应", next=f"{AGAIN}；还不行就 {NO_BROWSER}")

    def handle(self, event):
        kind = event.get("event")
        if kind == "qr":
            self.say("\n用飞书手机端扫码（只扫这一次；过期会自动换新的）：")
            draw(event.get("matrix") or [])
            self.say(f"扫不了时打开图片：{event.get('path')}")
            if platform.system() == "Darwin":
                with contextlib.suppress(OSError):
                    subprocess.Popen(["open", str(event.get("path"))])
        elif kind == "login_page":
            self.say(f"登录页显示：{event.get('text')}")
        elif kind == "step" and event.get("state") in ("done", "skipped", "shot"):
            self.say(f"    {event.get('name')}：{event.get('state')}")
        elif kind in ("done", "failed"):
            self.last = event
        return kind

    def request(self, command, seconds):
        self.proc.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return self.wait_for("result", seconds)

    def create_app(self, url, name):
        return self.request({"cmd": "create_app", "url": url, "name": name, "seconds": 600}, 700)

    def authorize(self, url):
        return self.request({"cmd": "authorize", "url": url, "seconds": 120}, 200)

    def configure(self, app_id, plan):
        self.last = {}
        return self.request({"cmd": "console", "app_id": app_id, "plan": str(plan), "seconds": 300}, 600)

    def close(self):
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.write('{"cmd": "quit"}\n')
            self.proc.stdin.flush()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


STARTUP_ENV = ("BASH_ENV", "ENV")  # Files a non-interactive bash (or sh) runs before its script.


class Installer:
    """One run of ./install. ``say``, ``ask`` and the callables below are replaceable so tests need no network."""

    def __init__(self, args, *, env=None, say=say_now, ask=input, interactive=None, probe=install_probe.probe,
                 browser_factory=None, survey_app=survey, app_id_of=profile_app_id, app_name_of=profile_app_name, prepare_browser=browser_setup.prepare,
                 web_prepare=None, web_suggest=None, sync_slash=slash_commands.sync, feishu_sdk=None):
        self.args, self.say, self.ask = args, say, ask
        self.feishu_sdk = feishu_sdk or self.ensure_feishu_sdk  # () -> whether the service's Python has the Feishu SDK.
        # The old spelling inside: what is saved, fingerprinted and passed on is what an older release reads too (core/env.py).
        self.env = cenv.canonical(dict(os.environ if env is None else env))
        # Not BASH_ENV or sh's ENV: every bash it starts (bootstrap_harness_bridge.sh, the hosting hooks) would run the file
        # they name first (some ssh setups point BASH_ENV at .bashrc), and the PATH that file sets would be registered
        # as the service's, which a later upgrade's pre-check then finds differs from a clean shell's (T818).
        for name in STARTUP_ENV:
            self.env.pop(name, None)
        self.interactive = (not args.non_interactive and sys.stdin.isatty()) if interactive is None else interactive
        self.probe, self.survey_app, self.app_id_of, self.app_name_of = probe, survey_app, app_id_of, app_name_of
        self.sync_slash = sync_slash
        self.ready_seconds = self.answer_seconds = None  # tests
        self.ready = False  # The doctor said the service is ready (wait_ready).
        self.waiting_steps = []  # Phone steps (their numbers) whose scan is not seen in the app's online version yet
        self.browser_factory, self.browser, self.browser_home = browser_factory, None, None
        self.data = Path(args.data_home).expanduser().resolve() if args.data_home else (
            Path(self.env["LARK_BRIDGE_DATA_HOME"]).expanduser().resolve() if self.env.get("LARK_BRIDGE_DATA_HOME")
            else data_home())
        self.workdir, self.phone_step, self.poll_seconds = None, 0, 5
        self.browser_python, self.live_open, self.prepare_browser = None, False, prepare_browser
        self.working, self.extra, self.reasons, self.leftovers = [], [], {}, []
        self.set_up = []  # Of GATEWAY_HARNESSES, the ones set up here (set_up_harnesses)
        self.switches_settled = False  # settle_switches ran
        self.fresh = False  # settle_switches found no service registered for this app: a new one (D3 presets its butler)
        self.app_id = ""
        self.web_build = None  # The page's build in the background (start_web_build): proc, folder, started, log, applied
        self.web_names, self.web_result = [], None  # The public addresses asked for; what web_selfhost.prepare returned
        self.web_decided = False  # choose_web ran
        self.web_prepare, self.web_suggest = web_prepare or web_selfhost.prepare, web_suggest or (lambda: web_selfhost.suggest_address())
        self.web_seconds, self.web_poll = WEB_BUILD_SECONDS, 2  # tests

    # Output ----------------------------------------------------------------------------------

    def step(self, number, title):
        self.say(f"[{number}/{STEPS}] {title}")

    def note(self, text):
        self.say(f"    {text}")

    def log(self, name):
        folder = self.data / "install-logs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{name}.log"

    def run_logged(self, name, argv, **kwargs):
        """Run ``argv`` with its output in install-logs/<name>.log; (returncode, the log's last lines)."""
        path = self.log(name)
        with open(path, "w") as sink:
            done = subprocess.run(argv, env=self.env, stdout=sink, stderr=subprocess.STDOUT, **kwargs)
        tail = " | ".join(path.read_text(errors="replace").strip().splitlines()[-3:])
        return done.returncode, tail

    # 1 -----------------------------------------------------------------------------------------

    def check_environment(self):
        self.step(1, "检查环境和目录")
        if sys.version_info < (3, 10):
            raise Fail(f"需要 Python 3.10 或更新（现在是 {platform.python_version()}）",
                       next="装好 Python 3.10+ 后指定它运行：PYTHON=python3.12 ./install")
        if platform.system() not in ("Linux", "Darwin"):
            raise Fail("目前只支持 Linux 和 macOS", next="换一台 Linux 或 macOS 机器（Windows 上可以在 WSL 里装）")
        try:
            for sub in ("", "tmp", "browsers", "tools"):
                (self.data / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Fail(f"数据根建不了：{self.data}（{exc.strerror or type(exc).__name__}）",
                       next="换一个自己能写的目录：./install --data-home ~/covey-data") from None
        if not os.access(self.data, os.W_OK):
            raise Fail(f"数据根不可写：{self.data}", next="换一个自己能写的目录：./install --data-home ~/covey-data")
        self.env["LARK_BRIDGE_DATA_HOME"] = str(self.data)
        self.env.setdefault("TMPDIR", str(self.data / "tmp"))
        self.env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.data / "browsers")
        self.workdir, made = self.choose_workdir()
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Fail(f"默认工作区建不了：{self.workdir}（{exc.strerror or type(exc).__name__}）",
                       next="换一个能写的目录：./install --workdir <你的项目目录>") from None
        self.note(f"数据根 {self.data}；默认工作区 {self.workdir}")
        if made and home.seed_home(self.workdir):
            self.note("主目录是新建的空目录，里面放了一份说明（AGENTS.md）。要它在你的项目里干活：飞书里「设置」→「工作区」"
                      "添加项目目录，或 ./install --workdir <项目目录>")

    def recorded_workdir(self):
        """The default workspace this data root's last run registered (install.json, the fingerprint), when it is still a
        directory; else None."""
        try:
            recorded = json.loads((self.data / "install.json").read_text()).get("workdir")
        except (OSError, ValueError, AttributeError):
            return None
        return Path(recorded) if isinstance(recorded, str) and recorded and Path(recorded).is_dir() else None

    def choose_workdir(self):
        """(the default workspace, whether it is the <data>/home this run makes): ``--workdir``; else the one the last
        run registered (a re-run keeps what was chosen); else the current directory, or in the checkout <data>/home,
        which a run in a terminal offers to change once (docs/install.md 它做了什么)."""
        if self.args.workdir:
            return Path(self.args.workdir).expanduser().resolve(), False
        recorded = self.recorded_workdir()
        if recorded is not None:
            return recorded, False
        here = Path.cwd().resolve()
        if here != REPO_ROOT.resolve():
            return here, False
        default = self.data / "home"
        if not self.interactive:
            return default, True
        for _ in range(2):
            answer = self.ask(f"    新话题默认在哪个目录干活？输入你的项目目录（如 ~/code/myapp），直接回车用 {default}"
                              "（新建的空目录，之后在飞书「设置」→「工作区」也能加）：").strip()
            if not answer:
                break
            chosen = Path(answer).expanduser().resolve()
            if chosen.is_dir():
                return chosen, False
            self.note(f"没有这个目录：{chosen}")
        return default, True

    # 2 -----------------------------------------------------------------------------------------

    def which(self, name):
        return shutil.which(name, path=self.env.get("PATH", ""))

    def ensure_lark_cli(self):
        self.step(2, "准备 lark-cli")
        if self.which("lark-cli"):
            self.note("已有 lark-cli")
        else:
            npm = self.which("npm")
            if not npm:
                raise Fail("没有 lark-cli，也没有 npm 可以装它",
                           next=f"先装 Node.js（{RELEASES}，装好后 npm -v 能打出版本号），再运行 ./install")
            self.note("安装 lark-cli（npm）")
            code, tail = self.run_logged("lark-cli-install", [npm, "install", "-g", "@larksuite/cli"], timeout=600)
            if code != 0 or not self.which("lark-cli"):
                prefix = self.data / "tools"
                code, tail = self.run_logged("lark-cli-install", [npm, "install", "--prefix", str(prefix),
                                                                  "@larksuite/cli"], timeout=600)
                self.env["PATH"] = f"{prefix / 'node_modules' / '.bin'}{os.pathsep}{self.env.get('PATH', '')}"
                if code != 0 or not self.which("lark-cli"):
                    raise Fail(f"lark-cli 装不上：{tail or '见 ' + str(self.log('lark-cli-install'))}",
                               next=f"完整输出在 {self.log('lark-cli-install')}。多半是网络：设好代理（export HTTPS_PROXY="
                                    "http://主机:端口）或 npm 镜像再运行 ./install；也可以自己 npm install -g @larksuite/cli "
                                    "装好再来")
        if self.args.skills:
            code, tail = self.run_logged("skills", [str(REPO_ROOT / "scripts" / "install_skills.sh")], timeout=600)
            self.note("Skills 已同步" if code == 0 else f"Skills 同步失败（不影响安装）：{tail}")

    # 3 -----------------------------------------------------------------------------------------

    def hook_dirs(self):
        try:
            dirs = extensions.package_dirs(self.env)
        except Exception:  # noqa: BLE001 - no extension is the usual case
            return []
        return [d for d in dirs if (d / "scripts" / "setup_hook.sh").is_file()]

    def run_hooks(self, *phases):
        """The enabled extensions' install hooks (scripts/setup_hook.sh), each phase a function it may define.
        Returns the Harness IDs ``ext_detect`` adds. They see the variables and helpers the old setup script gave them."""
        dirs = self.hook_dirs()
        if not dirs:
            return []
        sources = "\n".join(f'. "{d}/scripts/setup_hook.sh"' for d in dirs)
        calls = "\n".join(f"if declare -F {p} >/dev/null; then {p}; fi" for p in phases)
        script = (f'ROOT="{REPO_ROOT}"\nhave() {{ command -v "$1" >/dev/null 2>&1; }}\ninfo() {{ :; }}\n'
                  'warn() { echo "$*" >&2; }\nSKIP_EXT_TOOLS=0\nSKIP_LOGIN=0\n'
                  f'NON_INTERACTIVE={0 if self.interactive else 1}\nAVAILABLE_HARNESSES=()\n{sources}\n{calls}\n'
                  'for h in "${AVAILABLE_HARNESSES[@]}"; do echo "HARNESS:$h"; done\n')
        code, tail = self.run_logged("extension-hook", ["bash", "-c", script], timeout=900)
        found = [line[8:] for line in self.log("extension-hook").read_text().splitlines() if line.startswith("HARNESS:")]
        if code != 0:
            self.note(f"扩展的安装钩子没跑完（不影响核心）：{tail}")
        return found

    def proxied(self):
        return any(self.env.get(k) or self.env.get(k.lower()) for k in PROXY_VARIABLES)

    def use_proxy(self, url):
        self.env["https_proxy"] = self.env["http_proxy"] = url

    def finish_proxy(self):
        """When any proxy is set, its non-secret form goes into the service's environment (the launch saves it) and the
        Feishu domains stay direct; a proxy URL with credentials is refused, never printed. The HTTPS proxy is saved
        under its lowercase name: scripts/deploy_bridge.sh unsets HTTPS_PROXY, so a manifest that kept the uppercase
        one failed every later upgrade's pre-check (env_missing)."""
        if not self.proxied():
            return
        upper = self.env.pop("HTTPS_PROXY", "")
        if upper:
            self.env.setdefault("https_proxy", upper)
        keep = [h for h in self.env.get("NO_PROXY", self.env.get("no_proxy", "")).split(",") if h]
        self.env["NO_PROXY"] = ",".join(dict.fromkeys(keep + FEISHU_NO_PROXY.split(",")))
        self.env.pop("no_proxy", None)
        try:
            validate_environment({k: v for k, v in self.env.items() if k in PROXY_ENV_KEYS})
        except ValueError:
            raise Fail("代理地址带了用户名或密码，不能写进服务", next=(
                "换一个不带凭据的代理地址（比如本机起一个不用登录的本地转发），export HTTPS_PROXY=http://127.0.0.1:端口 再运行 "
                "./install")) from None
        self.note("代理设置会一起写进服务（飞书域名直连）")

    def ask_proxy(self, name):
        words = f"{name} 像是网络不通。"
        if not self.interactive:
            self.note(words + "需要代理时先 export HTTPS_PROXY=http://主机:端口 再运行 ./install")
            return ""
        answer = self.ask(f"    {words}要走代理的话输入代理地址（如 http://127.0.0.1:7890），不用就直接回车：").strip()
        return answer

    def locate(self, name):
        """Whether Harness ``name``'s CLI is on this run's PATH, or in one of COMMON_DIRS under HOME: then that folder is
        put at the front of the PATH (the service is registered with this run's PATH) and said once."""
        if self.which(name):
            return True
        home = Path(self.env.get("HOME") or Path.home())
        for relative in COMMON_DIRS:
            folder = home / relative
            path = folder / name
            if path.is_file() and os.access(path, os.X_OK):
                self.env["PATH"] = f"{folder}{os.pathsep}{self.env.get('PATH', '')}"
                self.note(f"{name}：在 ~/{relative} 找到（这个终端的 PATH 里没有它），服务的 PATH 会加上这个目录")
                return True
        return False

    def harness_config(self):
        """<data>/harnesses.json as it is now, {} when it is not there or not an object."""
        try:
            config = json.loads((self.data / "harnesses.json").read_text())
        except (OSError, ValueError):
            return {}
        return config if isinstance(config, dict) else {}

    def set_up_harnesses(self):
        """GATEWAY_HARNESSES installed here, each enabled when its gateway's key is in a file the service reads (a key only
        in this shell's environment never reaches it: a manifest keeps no credential) and said in one line either way; one
        not installed is not said. dsh's command is harnesses.json's --dsh-cmd, else ``dsh``; its gateway the first that
        declares it (core/gateways.py: an extension's or harnesses.json's)."""
        config = self.harness_config()
        args = ((config.get("harnesses") or {}).get("dsh") or {}).get("args") or []
        command = args[args.index("--dsh-cmd") + 1] if "--dsh-cmd" in args[:-1] else "dsh"
        if not self.which(command):
            return
        try:
            gateways.configure(config.get("gateways"))
            gateway = gateways.dsh_gateway()
        except (ValueError, extensions.ExtensionError):
            gateway = None
        why = "没有给它用的模型网关（docs/harnesses.md「dsh」）" if gateway is None else self.key_problem(gateway)
        if why:
            self.note(f"dsh：装了，{why}，先不启用")
            return
        self.set_up.append("dsh")
        self.note(f"dsh：装了，{gateway.display} 的密钥也在，启用")

    def key_problem(self, gateway):
        """'' when ``gateway``'s key file is one the service can read (owner-only, not empty), else what to do."""
        path = gateway.key_path()
        try:
            info = path.stat()
        except OSError:
            info = None
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_size and not info.st_mode & 0o077:
            return ""
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_size:
            return f"{gateway.display} 的密钥文件 {path} 要改成只有自己能读（chmod 600）"
        if self.env.get(gateway.key_env):
            return f"{gateway.display} 的密钥只在这个终端的 {gateway.key_env} 里，服务拿不到：写进 {path}（权限 600）"
        return f"没有 {gateway.display} 的密钥：{gateway.missing_key}"

    def check_harnesses(self):
        self.step(3, "检查 Harness（各调用一次模型）")
        installed = [n for n in PROBED if self.locate(n)]
        extra = self.run_hooks("ext_ensure", "ext_detect")
        for name in installed:
            result = self.probe(name, self.env, cwd=str(self.data / "tmp"), directory=self.data / "harness-probe")
            if result.kind == install_probe.NETWORK and not self.proxied():
                url = self.ask_proxy(name)
                if url:
                    self.use_proxy(url)
                    result = self.probe(name, self.env, cwd=str(self.data / "tmp"), directory=self.data / "harness-probe")
            if result.ok:
                self.working.append(name)
                self.note(f"{name}：能调通")
            else:
                self.reasons[name] = {install_probe.NOT_LOGGED_IN: "没登录，先在终端里登录它",
                                      install_probe.NETWORK: "网络不通，需要代理时设 HTTPS_PROXY 再运行"
                                      }.get(result.kind, "调用失败") + (f"（{result.detail}）" if result.detail else "")
                self.note(f"{name}：{self.reasons[name]}；不启用")
        for name in PROBED:
            if name not in installed:
                self.reasons[name] = f"没装（PATH 里没有 {name} 命令）"
        self.set_up_harnesses()
        self.extra = [h for h in extra if h not in self.working]
        for h in self.extra:
            self.note(f"{h}：扩展提供，沿用它自己的检查")
        missing = [HARNESS_TITLES[n] for n in PROBED if n not in installed]
        if missing and (self.working or self.extra or self.set_up):
            self.note(f"没装 {'、'.join(missing)}：不用每个都装，有一个能调通就能用；以后装了哪个，重新运行 ./install 会把它加进来")
        if not self.working and not self.extra and not self.set_up:
            raise Fail("没有能调通的 Harness", next=(
                "按下面的原因修好其中一个：没装的照 docs/harnesses.md「安装和登录 Harness」装；没登录的在终端里运行 claude "
                "按提示登录，或 codex login，或运行 agy 用 Google 登录；网络不通的先 export HTTPS_PROXY=http://主机:端口。确认 "
                "claude -p \"ok\"、codex exec --skip-git-repo-check \"ok\" 或 agy -p \"ok\" 有回答，再运行 ./install"))
        self.finish_proxy()

    def retention_hint(self):
        """One line when Claude Code works here and its settings keep few days of sessions (core/claude_retention.py); the
        installer only says it, never touches the user's Claude config."""
        if "claude" in self.working and (line := claude_retention.hint(claude_retention.check(self.env))):
            self.note("提示：" + line)

    def last_harnesses(self):
        """The Harnesses usable at the last run that wrote harnesses.json (USABLE_RECORD), else at the last registration
        (install.json, the fingerprint, of a data root an older release installed); None without either."""
        for name, key in ((USABLE_RECORD, "usable"), ("install.json", "harnesses")):
            try:
                found = json.loads((self.data / name).read_text()).get(key)
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(found, list):
                return found
        return None

    def write_harness_config(self):
        """<data>/harnesses.json: the usable Harnesses (the probed ones that answered, the GATEWAY_HARNESSES set up here and
        the extensions') enabled, the default (the roster's first) first; what else the file holds stays, and so do the
        enabled ones that are not probed here. On a re-run one usable now and not at the last run that wrote the file
        (last_harnesses: Claude Code installed after an install with only Codex, D3; dsh set up since) is added, even when
        that run stopped before the registration; one the owner took out of the file after a run had it usable stays out.
        What this run found usable is recorded in USABLE_RECORD."""
        from covey.core.roster import NAMES

        path = self.data / "harnesses.json"
        try:
            config = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        usable = self.working + self.set_up
        old = config.get("enabled_harnesses") if isinstance(config.get("enabled_harnesses"), list) else []
        if old:  # Upgrade: what the owner enabled stays; a failed probe is only said (the launch card shows it unavailable).
            for n in old:
                if n in PROBED and n not in self.working:
                    self.leftovers.append(f"{n} {self.reasons.get(n, '探测没通过')}，启动卡上会显示不可用（仍保留在已启用里）")
            last = self.last_harnesses()
            newly = [n for n in (*PROBED, *GATEWAY_HARNESSES)
                     if n in usable and n not in old and last is not None and n not in last]
            for n in newly:
                self.note(f"{n}：这次能用了，加进启用的 Harness")
            enabled = [n for n in NAMES if n in old or n in self.extra or n in newly]
        else:
            enabled = [n for n in NAMES if n in usable or n in self.extra]
        config["enabled_harnesses"] = enabled
        write_json(path, config)
        write_json(self.data / USABLE_RECORD, {"usable": sorted(usable + self.extra)})
        return path

    # 4 -----------------------------------------------------------------------------------------

    def app_ready(self):
        """The app id of the profile when it exists and its login verifies; '' otherwise."""
        done = subprocess.run(["lark-cli", "--profile", self.args.profile, "auth", "status", "--verify", "--json"],
                              env=self.env, capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            return ""
        try:  # A created app with nobody authorized (the owner's user login) still needs the authorization step.
            user = (json.loads(done.stdout).get("identities") or {}).get("user") or {}
        except (ValueError, AttributeError):
            user = {}
        return self.app_id_of(self.args.profile) if user.get("available", True) is not False and user.get("status") != "missing" else ""

    def profile_exists(self):
        return subprocess.run(["lark-cli", "--profile", self.args.profile, "config", "show"], env=self.env,
                              capture_output=True, timeout=30).returncode == 0

    def live(self, text):
        """The refreshed progress line (a terminal only); None ends it. Without a terminal the caller prints lines."""
        if text is None:
            if self.live_open:
                sys.stdout.write("\n")
                self.live_open = False
            return
        sys.stdout.write("\r" + text.ljust(40))
        sys.stdout.flush()
        self.live_open = True

    def use_browser(self):
        """Whether this run opts into headless browser automation (default False: phone QR + Bot OpenAPI slash commands)."""
        return (bool(getattr(self.args, "browser", False)) or bool(self.args.browser_login_dir)) and not self.args.no_browser

    def close_browser(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def ensure_browser(self):
        """The Python that has Playwright and a browser that starts, prepared under the data root (service/browser_setup.py:
        a Chrome the machine has, else Chromium downloaded with mirrors, plus system libraries when they are missing);
        None (and why) when it cannot be had or when browser mode is not requested."""
        if self.browser_python is not None:
            return self.browser_python or None
        self.browser_python = ""
        if not self.use_browser():
            return None
        env_python = self.env.get("LARK_BRIDGE_PLAYWRIGHT_PYTHON", "")
        if env_python and os.access(env_python, os.X_OK):
            self.browser_python = env_python
            return env_python
        if platform.system() not in ("Linux", "Darwin"):
            return None
        self.note("准备浏览器（只用来点飞书网页，装在数据根下）")
        tty = self.say is say_now and sys.stdout.isatty()
        try:
            prepared = self.prepare_browser(self.data, self.env, self.note, live=self.live if tty else None)
        except browser_setup.Unavailable as why:
            self.note(f"浏览器用不了：{why}")
            self.note("改用手机扫码：下面每个链接画成二维码，依次扫（以后想直接这样，不用加 --browser）")
            return None
        self.browser_python = prepared.python
        return prepared.python

    def open_browser(self, python):
        if self.browser is None:
            self.browser_home = Path(tempfile.mkdtemp(prefix="login-", dir=self.data / "tmp"))
            os.chmod(self.browser_home, 0o700)
            if self.args.browser_login_dir:  # 验收用：从已登录的副本开始，不画二维码
                source = Path(self.args.browser_login_dir).expanduser()
                source = source / "profile" if (source / "profile").is_dir() else source
                shutil.copytree(source, self.browser_home / "profile", symlinks=False)
                self.note("浏览器用给定的登录副本（--browser-login-dir）")
            factory = self.browser_factory or BrowserSession
            self.browser = factory(python, self.browser_home, self.say, self.env)
        return self.browser

    def run_flow(self, argv, on_url, wait=600):
        """Run a lark-cli command that prints a link and then waits for a person; ``on_url(link)`` is what takes the
        link to the person (the browser clicks it, or its QR is drawn). Fail with its last lines when it ends badly."""
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env)
        lines, seen, sent = stream_lines(proc), [], False
        deadline = time.monotonic() + wait
        try:
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                seen.append(line)
                found = URL.search(line)
                if found and not sent:
                    sent = True
                    on_url(found.group(0))
                    deadline = time.monotonic() + wait
            else:
                raise Fail("等太久了：lark-cli 还没有结束（二维码过期或这一步没做完）", next=f"{AGAIN}，已建好的应用不用重建")
            if proc.wait(timeout=30) != 0:
                raise Fail("lark-cli 没有成功：" + " | ".join(meaningful(seen)[-3:]) + f"（完整输出：{self.log(flow_name(argv))}）",
                           next=f"{AGAIN}；还不行就 ./install --reconfigure 从头建应用")
        finally:
            self.log(flow_name(argv)).write_text("\n".join(seen) + "\n")
            if proc.poll() is None:
                proc.kill()

    def by_phone(self, title, link, after="做完这一步后这里会自动继续"):
        """One step for the phone: the title, the link, its QR on the terminal and as a PNG (named by the step)."""
        self.phone_step += 1
        self.say(f"\n手机第 {self.phone_step} 步：{title}\n    用飞书手机端扫下面的码，或在手机浏览器里打开：{link}")
        with contextlib.suppress(ValueError):
            draw(qr.matrix(link))
            path = self.data / "tmp" / f"link-qr-{self.phone_step}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(qr.png(qr.matrix(link)))
            os.chmod(path, 0o600)
            self.say(f"    扫不了时打开图片：{path}")
            if platform.system() == "Darwin":
                with contextlib.suppress(OSError):
                    subprocess.Popen(["open", str(path)])
        self.say(f"    {after}")

    def feishu_app(self):
        self.step(4, "飞书应用（扫一次码）" if self.use_browser() else "飞书应用（手机扫码）")
        profile = self.args.profile
        python = None
        try:
            if self.args.reconfigure and self.profile_exists():
                subprocess.run(["lark-cli", "profile", "remove", profile], env=self.env, capture_output=True, timeout=60)
            have_profile = self.profile_exists()
            self.app_id = self.app_ready() if have_profile else ""
            reused = bool(self.app_id)
            if self.app_id:
                self.note(f"复用已有应用 {self.app_id}（profile {profile}）")
            else:
                python = self.ensure_browser()
                self.create_and_authorize(python, have_profile)
            self.settle_switches()  # Before the app's needs are read: they follow the switches (slash /board, Pin scopes).
            self.sync_slash_commands()
            lacking = self.lacking()
            if lacking and reused and not self.interactive and not self.args.browser_login_dir:
                # An upgrade with nobody to scan (no login copy either): list what is missing, go on with the service; the Bridge's 飞书应用待补 card finishes it.
                self.leftovers.append(f"飞书应用还缺 {app_manifest.joined(lacking)}。非交互不等扫码，服务照常换成新代码；"
                                      "给机器人发一句话后会收到「飞书应用待补」卡，点「扫码自动补齐」（或交互运行 ./install）")
            elif lacking:
                self.configure_app(python if self.browser_python else self.ensure_browser(), lacking)
        finally:
            self.close_browser()
            if self.browser_home is not None:
                if self.args.keep_login:
                    self.note(f"浏览器登录留在 {self.browser_home}（--keep-login）；用完请删掉这个目录")
                else:
                    remove(self.browser_home, self.note)

    def sync_slash_commands(self):
        """Register Covey's official Feishu slash commands (/usage, /board, /proxy, /settings) via Bot OpenAPI."""
        result = self.sync_slash(self.args.profile, self.env)
        wanted = result.get("wanted") or []
        if result.get("ok"):
            if wanted:
                cmds = "、".join(f"/{name}" for name in wanted)
                self.note(f"斜杠命令已就绪：{cmds}（私聊输入框打 / 呼出，新注册约 5 分钟内生效）")
            return
        why = {"no_scope": "应用缺少 application:app_slash_command 权限",
               "unsupported": "当前 lark-cli 版本不支持斜杠命令注册（请升级 @larksuite/cli）"}.get(
            result.get("error"), result.get("detail") or "接口报错")
        self.leftovers.append(f"斜杠命令没注册到输入框面板（{why}）；不影响使用，私聊直接发 /设置、/看板、/用量 也能打开菜单卡")

    def create_and_authorize(self, python, have_profile):
        profile = self.args.profile
        init = ["lark-cli", "config", "init", "--new", "--name", profile, "--brand", "feishu", "--lang", "zh"]
        login = ["lark-cli", "--profile", profile, "auth", "login", "--domain", LARK_DOMAINS]
        if python:
            session = self.open_browser(python)
            try:
                if not have_profile:
                    self.note("在浏览器里建应用")
                    self.run_flow(init, lambda link: self.expect(session.create_app(link, self.args.app_name), fallback=True))
                self.note("在浏览器里授权")
                self.run_flow(login, lambda link: self.expect(session.authorize(link), fallback=True))
            except ConditionalAccessFallback:
                self.close_browser()
                self.browser_python = ""
                self.note("飞书网页登录被租户条件访问拦下（需要受信任设备授权），自动改用手机扫码")
                self.create_and_authorize(None, self.profile_exists())
                return
        else:
            self.note("手机上共 3 步：1 建应用、2 授权、3 事件与回调订阅（一个链接）；菜单用官方斜杠命令自动注册，不需要浏览器或电脑端认证"
                      + ("" if self.interactive else "。非交互时第 3 步不等待：列出链接，扫完后重新运行 ./install 或等几分钟"))
            if not have_profile:
                self.run_flow(init, lambda link: self.by_phone(f"建应用（建议把名称改成「{self.args.app_name}」，点「创建」）", link))
            self.run_flow(login, lambda link: self.by_phone("授权（点「开通并授权」，弹窗里再点一次）", link))
        self.app_id = self.app_id_of(profile)
        if not self.app_id:
            raise Fail("读不到应用的 appId：lark-cli 的 profile 没建好", next="./install --reconfigure 从头建应用")
        self.note(f"应用 {self.app_id} 已建好并授权")

    @staticmethod
    def expect(result, *, fallback=False):
        if not result.get("ok"):
            if fallback and result.get("code") == CONDITIONAL_ACCESS_CODE:
                raise ConditionalAccessFallback()
            raise Fail(f"浏览器这一步没做成：{result.get('detail')}", next=f"{AGAIN}；还不行就 {NO_BROWSER}")

    def lacking(self):
        online, lacking, full = self.survey_app(self.args.profile, self.app_id, self.env)
        if online.get("error") and online.get("error") != "no_self_manage":
            self.leftovers.append(f"读不到应用的线上配置（{online.get('detail') or online['error']}）")
            return []
        return app_manifest.offered(lacking, full)

    def configure_app(self, python, offer):
        words = app_manifest.joined(offer)
        if not python:
            self.configure_by_phone(offer)
            return
        self.note(f"开发者后台补 {len(offer)} 项并发布：{words}")
        session = self.open_browser(python)
        plan = self.browser_home / "plan.json"
        plan.write_text(json.dumps(app_manifest.plan(offer, remark=app_manifest.remark(offer)), ensure_ascii=False))
        result = session.configure(self.app_id, plan)
        if not result.get("ok"):
            if result.get("code") == CONDITIONAL_ACCESS_CODE:
                self.close_browser()
                self.browser_python = ""
                self.note("开发者后台登录被租户条件访问拦下，自动改用手机扫码补齐事件与回调")
                self.configure_by_phone(offer)
                return
            self.leftovers.append(f"开发者后台没补完（{result.get('detail')}）；重新运行 ./install 再试，"
                                  "或点机器人发来的「飞书应用待补」卡")
            return
        outcome = getattr(session, "last", {}) or {}
        status = {"published": "已发布", "reviewing": "已提交，等管理员审核", "unchanged": "没有改动"}
        self.note(f"版本 {outcome.get('version') or '-'}：{status.get(outcome.get('status'), outcome.get('status') or '完成')}")
        still = [app_manifest.joined(self.lacking_after(offer))]
        if still[0]:
            self.leftovers.append("线上版本仍然缺：" + still[0])

    def wait_scanned(self, ids, seconds=300):
        """Poll the app's online version until ``ids`` (need ids) are in it; False after ``seconds`` or on Ctrl-C."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            if not {n.id for n in self.lacking()} & ids:
                return True
        return False

    def configure_by_phone(self, offer):
        """Without a browser: the events and callbacks get ONE one-tap link (its page lists both; one confirm adds both),
        the same lark-cli itself uses; what has no link (menu, scopes) is left to the Bridge's 飞书应用待补 card, which
        is said plainly."""
        ids = {n.id for n in offer if n.kind in ("event", "callback")}
        for what, url in app_manifest.launcher_links(self.app_id, offer):
            self.by_phone(what, url, "扫完这里会自动继续" if self.interactive else "非交互：不等待，扫完后重新运行 ./install 会接着检查")
            number = self.phone_step
            if self.interactive and not self.wait_scanned(ids):
                self.waiting_steps.append(number)
                self.leftovers.append(f"手机第 {number} 步（{what}）还没看到生效；扫完后重新运行 ./install 检查")
            elif not self.interactive:
                self.waiting_steps.append(number)
                self.leftovers.append(f"手机第 {number} 步（{what}）要在手机上扫上面的码；扫完后重新运行 ./install，或等几分钟（机器人"
                                      "会自己重试订阅）")
        rest = [n for n in offer if n.kind not in ("event", "callback")]
        if rest:
            self.leftovers.append(f"飞书应用还缺 {app_manifest.joined(rest)}。装完不影响收发消息；你给机器人发第一句话后会收到"
                                  "「飞书应用待补」卡，点「扫码自动补齐」就行（或按 docs/feishu-app.md 的「在开发者后台手动配置」做）")

    def lacking_after(self, offer):
        ids = {n.id for n in offer}
        return [n for n in self.lacking() if n.id in ids]

    # The web page (optional) -------------------------------------------------------------------------------------

    def web_prerequisite(self):
        """Why the page cannot be built here ('' when it can): Node 22+ and npm on the PATH the build will run with."""
        node, npm = self.which("node"), self.which("npm")
        if not node:
            return "没有 node"
        if not npm:
            return "没有 npm"
        try:
            done = subprocess.run([node, "-v"], env=self.env, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return "node 运行不了"
        found = re.match(r"v?(\d+)", done.stdout.strip())
        if done.returncode != 0 or not found:
            return "node 运行不了"
        return "" if int(found.group(1)) >= WEB_NODE_MAJOR else f"Node 版本太低（{done.stdout.strip()}，要 {WEB_NODE_MAJOR} 以上）"

    def web_registered(self):
        """Whether the service registered for this data root's app has the self-hosted page on (its manifest)."""
        app_id = self.app_id
        if not app_id:
            try:
                app_id = self.app_id_of(self.args.profile)
            except (OSError, ValueError, subprocess.SubprocessError):
                app_id = ""
        registered = self.registered_environment(app_id)
        return bool(registered) and web_selfhost.enabled(registered)

    def web_command(self, folder):
        """The commands that build the page and prepare it by hand (what the 还差 line names when a step here failed)."""
        build = f"bash {self.path_of(WEB_BUILD_SCRIPT)} {folder}"
        prepare = f"python3 -B -m covey.service.web_selfhost --data-root {self.data} --dir {folder / 'dist-selfhost'}"
        return f"{build} && {prepare}"

    def web_suggestion(self):
        try:
            return self.web_suggest() or ""
        except Exception:  # noqa: BLE001 - a suggestion that cannot be had is none
            return ""

    @staticmethod
    def addresses(text):
        return [a.strip() for a in text.split(",") if a.strip()]

    def ask_web_address(self, wanted, question):
        """The public addresses (a list) the page is asked to open to: ``wanted`` is the ``--web-public`` text ('auto': the
        address of this machine's outgoing interface, web_selfhost.suggest_address; confirmed or replaced in a terminal, and
        without one only said); with none, ``question`` (the run chose the page by answering) asks once in a terminal, and
        only where there is a public address to suggest. The default is no."""
        text = wanted.strip()
        if text and text != "auto":
            return self.addresses(text)
        suggested = self.web_suggestion()
        if text:
            if self.interactive:
                where = suggested or "没找到这台机器的公网地址，这里输入"
                text = self.ask(f"    公网地址用 {where}？回车确认{'' if suggested else '（不开公网就直接回车）'}，或输入别的 IP 或域名：").strip() or suggested
            else:
                text = suggested
            if not text:
                self.leftovers.append("--web-public auto 没找到这台机器的公网地址，网页只开本机地址；知道地址时用 ./install --web-public <IP或域名>")
            return self.addresses(text)
        if not (question and self.interactive and suggested):
            return []
        answer = self.ask(f"    要让人在外面直接打开网页吗？这台机器的出网地址 {suggested} 像是公网地址（自签证书，浏览器第一次会提示不安全，核对指纹后"
                          "继续）。要就输入 y 用它，或输入别的 IP 或域名；不要直接回车：").strip()
        if answer.lower() in YES:
            return [suggested]
        return [] if answer.lower() in ("", "n", "no", "否", "不要") else self.addresses(answer)

    def choose_web(self):
        """Settle once, before the scan, whether the run builds the page: ``--web``/``--web-public``; else in a terminal, when the
        service has it off and Node 22+ and npm are here, one question (default no). Nothing is detected, built or written
        for a run that does not choose it. A page the registered service has on is kept as it is (no question, no build)."""
        if self.web_decided:
            return
        self.web_decided = True
        if self.args.no_start:
            return
        explicit = bool(getattr(self.args, "web", False) or getattr(self.args, "web_public", None))
        folder = self.data / "web" / "app"
        wanted = getattr(self.args, "web_public", None) or ""
        if explicit:
            why = self.web_prerequisite()
            if why:
                self.leftovers.append(f"网页工作台没装：{why}。装好 Node.js {WEB_NODE_MAJOR} 以上和 npm 后重新运行 ./install --web，"
                                      f"或手动：{self.web_command(folder)}")
                return
        else:
            if not self.interactive or self.web_registered() or self.web_prerequisite():
                return
            answer = self.ask("    要不要在这台机器上跑网页工作台？在浏览器里看专项和话题，要装约 1 GB、几分钟（后台构建，不耽误后面的步骤）。"
                              "要输入 y，默认不要：").strip().lower()
            if answer not in YES:
                return
        self.web_names = self.ask_web_address(wanted, question=not explicit)
        self.start_web_build(folder)

    def spawn_web_build(self, argv, log):
        """The build as a process of its own (a process group: a stop reaches npm and what it started), its output in ``log``."""
        with open(log, "w") as sink:
            return subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                    start_new_session=True)

    def signal_group(self, proc, sig):
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, sig)

    def start_web_build(self, folder):
        argv = ["bash", str(REPO_ROOT / WEB_BUILD_SCRIPT), str(folder)]
        if (folder / WEB_BUILD_MARK).exists():
            argv.append("--restart")  # A build is there: an upgrade, and the running page restarts on the new one.
        log = self.log("web-build")
        try:
            proc = self.spawn_web_build(argv, log)
        except OSError as exc:
            self.leftovers.append(f"网页工作台没能开始构建（{exc.strerror or type(exc).__name__}）；手动：{self.web_command(folder)}")
            return
        self.web_build = {"proc": proc, "folder": folder, "started": time.monotonic(), "log": log, "applied": False}
        self.note(f"网页工作台在后台构建（日志 {log}），第 5 步注册服务前等它")

    def stop_web_build(self):
        """Stop a build still running, as a group (Ctrl-C, a step that failed): TERM, then KILL after 10 seconds."""
        build = self.web_build
        if not build or build["proc"].poll() is not None:
            return
        self.signal_group(build["proc"], signal.SIGTERM)
        try:
            build["proc"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.signal_group(build["proc"], signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                build["proc"].wait(timeout=5)

    def apply_switches(self, lines):
        """``NAME=VALUE`` lines into this run's environment (the registration saves them), over what a registered manifest
        or the shell had under any spelling of the name; the old spelling is what is kept (core/env.py)."""
        for line in lines:
            name, _, value = line.partition("=")
            names = cenv.spellings(name)
            for spelling in names:
                self.env.pop(spelling, None)
            self.env[names[-1]] = value

    def finish_web(self):
        """Before the registration: wait for the build (a line each minute; WEB_BUILD_SECONDS at most), then prepare the page
        and take its switches into the environment. A build that failed or ran out, a prepare that failed: only said in
        the 还差 lines, with the commands to do it by hand; the service is registered all the same."""
        build = self.web_build
        if not build or build["applied"]:
            return
        build["applied"] = True
        proc, folder, log = build["proc"], build["folder"], build["log"]
        deadline, said = build["started"] + self.web_seconds, build["started"] + 60
        while (code := proc.poll()) is None:
            now = time.monotonic()
            if now >= deadline:
                self.stop_web_build()
                self.leftovers.append(f"网页工作台构建超过 {self.web_seconds // 60} 分钟，已停掉（日志 {log}）；服务照常注册。"
                                      f"网络慢时设好代理或 npm 镜像后手动：{self.web_command(folder)}")
                return
            if now >= said:
                self.note(f"网页工作台还在构建（已 {int((now - build['started']) // 60)} 分钟）…")
                said += 60
            time.sleep(self.web_poll)
        if code != 0:
            tail = " | ".join(Path(log).read_text(errors="replace").strip().splitlines()[-3:]) if Path(log).exists() else ""
            self.leftovers.append(f"网页工作台没构建成功{('：' + tail) if tail else ''}（日志 {log}）；服务照常注册。改好后手动：{self.web_command(folder)}")
            return
        extra = {"public": self.web_names} if self.web_names else {}
        try:
            # The port this run's environment names (COVEY_WEB_SELFHOST_PORT) is the one the page config says: the Manager starts
            # the page on it, and web.json's base_url must reach it.
            result = self.web_prepare(self.data, folder=str(folder / "dist-selfhost"), port=web_selfhost.port_of(self.env), **extra)
            switch_lines = [str(line) for line in result["switches"]]
        except Exception as exc:  # noqa: BLE001 - whatever prepare says is only said
            self.leftovers.append(f"网页工作台构建好了，但没能备好配置（{exc}）；服务照常注册。手动：{self.web_command(folder)}")
            return
        self.apply_switches(switch_lines)
        self.web_result = result
        self.note("网页工作台构建好了，随服务一起启动")

    def web_lines(self, environment):
        """The 网页工作台 lines of the closing lines: where to open it and, when it is open from outside, the port to let
        through, the certificate's fingerprint and how the first visit goes. [] where the page is not on."""
        if not web_selfhost.enabled(environment):
            return []
        result = self.web_result or {}
        page = result.get("page_url")
        if not page:
            kept = read_json(self.data / "web" / "web.json", {})
            page = str(kept.get("page_url") or "") if isinstance(kept, dict) else ""
        lines = [f"网页工作台：{page or f'http://127.0.0.1:{web_selfhost.port_of(environment)}/'}（用法见 docs/web.md；第一次打开在飞书私聊机器人收到确认卡，点「允许」）"]
        public = result.get("public")
        if public:
            ports = "、".join(str(port) for port in result.get("open_ports") or [])
            if ports:
                lines.append(f"    公网访问要在防火墙或云安全组放行 TCP 端口 {ports}")
            if result.get("fingerprint"):
                lines.append(f"    证书是自签的，浏览器第一次会提示不安全：核对指纹 {result['fingerprint']} 一致后选「继续访问」")
            else:
                lines.append("    证书是自签的，浏览器第一次会提示不安全：核对网页进程日志里的证书指纹后选「继续访问」")
        return lines

    # 5, 6 --------------------------------------------------------------------------------------

    def service_state(self):
        return self.data / f"harness-{self.app_id}"

    def fingerprint(self, config_path):
        proxy = {k: self.env[k] for k in sorted(PROXY_ENV_KEYS) if self.env.get(k)}
        # The code (REPO_ROOT) and the LARK_BRIDGE_* settings are part of it: an upgrade into another clone, or with
        # other switches, has to re-register instead of "nothing changed".
        settings = {k: v for k, v in sorted(self.env.items()) if k.startswith("LARK_BRIDGE_") and persistable(k)}
        return {"profile": self.args.profile, "app_id": self.app_id, "workdir": str(self.workdir),
                "harnesses": sorted(self.working + self.set_up + self.extra), "proxy": proxy, "config": str(config_path),
                "repo": str(REPO_ROOT), "settings": settings}

    def preset(self):
        """The Harness a new service's butler is preset to (``butler_preset`` of the Harnesses that worked), '' for none."""
        return butler_preset(getattr(self, "working", ()))

    def default_switches(self):
        """The switches a new service starts with on (INSTALL_SWITCHES, core/switches.py INSTALL: 装好就开; with
        ``--showcase`` the 演示套餐 SHOWCASE), whichever Harnesses work: put into this shell's environment, which the launch
        saves in the manifest; one the environment already sets stays, under either spelling (``self.env`` holds the old
        one: ``COVEY_PROJECTS=0`` is ``LARK_BRIDGE_PROJECTS`` there)."""
        showcase = getattr(getattr(self, "args", None), "showcase", False)
        table = dict(switches.SHOWCASE if showcase else INSTALL_SWITCHES)
        wanted = [key for key in switches.SDK_SWITCHES if key in table and not str(cenv.get(key, "", self.env)).strip()]
        if wanted and not self.feishu_sdk():
            for key in wanted:
                table.pop(key)
        for key, value in table.items():
            if not str(cenv.get(key, "", self.env)).strip():
                self.env[key] = value

    def service_python(self):
        """The python3 the bootstrap registers the service with (the first on this run's PATH)."""
        return self.which("python3") or sys.executable

    def ensure_feishu_sdk(self):
        """Whether the service's Python has the Feishu SDK (SDK_REQUIREMENTS), installed with pip when it has not (into the
        user's site, or the virtualenv the Python is); False (said, the events and cards then stay on lark-cli) when that did
        not work. Asked once a run."""
        if getattr(self, "_feishu_sdk", None) is not None:
            return self._feishu_sdk
        python, pinned = self.service_python(), SDK_REQUIREMENTS[0].split("==", 1)[1]

        def has():
            try:
                return subprocess.run([python, "-c", SDK_PROBE, pinned], env=self.env, capture_output=True,
                                      timeout=60).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False
        ok = has()
        if not ok:
            self.note("安装飞书 SDK（lark-oapi，收消息和写卡片用；pip）")
            try:
                venv = subprocess.run([python, "-c", "import sys; print(sys.prefix != sys.base_prefix)"], env=self.env,
                                      capture_output=True, text=True, timeout=30).stdout.strip() == "True"
                code, tail = self.run_logged("feishu-sdk", [python, "-m", "pip", "install", "--quiet",
                                                            *([] if venv else ["--user"]), *SDK_REQUIREMENTS], timeout=600)
            except (OSError, subprocess.TimeoutExpired) as exc:
                code, tail = 1, type(exc).__name__
            ok = code == 0 and has()
            if not ok:
                self.note(f"飞书 SDK 没装上（{tail or '见 ' + str(self.log('feishu-sdk'))}）：收消息和写卡片照旧用 lark-cli，不影响使用。"
                          f"装好后（{python} -m pip install {' '.join(SDK_REQUIREMENTS)}）再打开：python3 -B bin/covey_service.py set "
                          "--state-dir <状态目录> COVEY_EVENT_TRANSPORT=sdk COVEY_CARD_TRANSPORT=sdk")
        self._feishu_sdk = ok
        return ok

    def service_up(self):
        root = self.data / "bridge-services"
        code = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "bin" / "covey_doctor.py"), "--root", str(root),
                               "--runtime-only"], env=self.env, capture_output=True, timeout=120).returncode
        return code == 0

    def precompile(self):
        """Write the checkout's .pyc files with the python3 bootstrap will register. The service runs every process
        with -B and so never writes them: without this each start compiles Covey from source (scripts/precompile_release.py).
        Skipped files are the up-to-date ones, so a re-run is quick; a failure is only said."""
        python = shutil.which("python3", path=self.env.get("PATH")) or sys.executable
        try:
            done = subprocess.run([python, "-B", "-I", str(REPO_ROOT / "scripts" / "precompile_release.py"), str(REPO_ROOT)],
                                  env=self.env, capture_output=True, text=True, timeout=300)
            ok = done.returncode == 0 and json.loads(done.stdout.strip().splitlines()[-1]).get("ok") is True
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            ok = False
        if not ok:
            self.note("没能预编译字节码（只是每个进程启动慢一点，不影响使用）")

    def registered_environment(self, app_id=""):
        """The environment the service registered for this app runs with (its manifest under <data>/bridge-services), None
        when there is none yet: a new service. ``app_id`` asks about another app than this run's (before the scan has one)."""
        app_id = app_id or self.app_id
        if not app_id:
            return None
        record = read_json(manifest_path(self.data / "bridge-services", service_key(self.data / f"harness-{app_id}")), None)
        environment = record.get("environment") if isinstance(record, dict) else None
        return dict(environment) if isinstance(environment, dict) else None

    def keep_registered(self, registered):
        """Put into this run's environment the settings ``registered`` has that it sets under no spelling, so registering
        again (another clone, other flags, a service that was down) keeps what `covey_service.py set` changed. What this
        shell or this run sets wins; NOT_KEPT and what a manifest cannot keep stay out."""
        for name, value in registered.items():
            if name in NOT_KEPT or not persistable(name) or not isinstance(value, str):
                continue
            if not any(spelling in self.env for spelling in cenv.spellings(name)):
                self.env[name] = value

    def settle_switches(self):
        """The switches the service will run with, settled once the app is known and before anything reads them (the
        app's needs in feishu_app, the registration in start_service). A new service: INSTALL_SWITCHES, with ``--showcase``
        the 演示套餐 (default_switches). A service registered for this app already: its manifest's settings this run does
        not set are kept (keep_registered) and no new default is added (a re-run is safe); ``--showcase`` turns on what
        the 演示套餐 has that the manifest lacks or has off (core/switches.py preset_changes) unless this shell sets it."""
        if self.switches_settled:
            return
        self.switches_settled = True
        registered = self.registered_environment()
        self.fresh = registered is None
        if registered is None:
            self.default_switches()
            return
        shell = set(self.env)
        self.keep_registered(registered)
        if getattr(self.args, "showcase", False):
            preset = switches.preset_changes("showcase", registered)
            for name, value in preset.items():
                spellings = cenv.spellings(name)
                if not shell & set(spellings):
                    for spelling in spellings:
                        self.env.pop(spelling, None)
                    self.env[spellings[-1]] = value

    def start_service(self, config_path):
        self.step(5, "注册并启动服务")
        self.precompile()
        self.settle_switches()
        self.finish_web()  # Its switches join this run's environment: the registration below writes them into the manifest.
        record = self.data / "install.json"
        now = self.fingerprint(config_path)
        try:
            before = json.loads(record.read_text())
        except (OSError, ValueError):
            before = None
        if before == now and not self.args.reconfigure and self.service_up():
            if getattr(self.args, "showcase", False):
                self.showcase_running()
            else:
                self.note("服务已在运行，配置没变")
            self.check_service_env()
            return
        # Hosting first (the unit's Manager takes the single-instance lock), then the registration; without systemd or
        # launchd ("manager" mode) nothing is hosted here and the bootstrap's own launch starts the Manager.
        code, tail = self.run_logged("service-install", [
            sys.executable, "-B", str(REPO_ROOT / "bin" / "covey_service_install.py"), "--root", str(self.data / "bridge-services"),
            "--mode", "auto", "--activate", *(["--no-autostart"] if self.args.no_autostart else [])], timeout=300)
        hosted = code == 0
        if hosted:
            self.note(f"开机自启：{boot_words(self.log('service-install'))}")
        if self.fresh and self.preset():
            self.preset_butler(self.preset())
        code, tail_boot = self.run_logged("bootstrap", [
            str(REPO_ROOT / "scripts" / "bootstrap_harness_bridge.sh"), "--workdir", str(self.workdir),
            "--profile", self.args.profile, "--app-id", self.app_id, "--harness-config", str(config_path)], timeout=300)
        if code != 0:
            raise Fail(f"服务没注册成功：{tail_boot}", next=f"看 {self.log('bootstrap')} 里的报错，改好后{AGAIN}")
        write_json(record, now)
        self.write_upgrader()
        if not hosted:
            self.leftovers.append("开机自启没注册上（服务已在运行，重启这台机器后要再运行 ./install）：" + tail)
        self.check_service_env()

    def write_upgrader(self):
        """<data>/upgrade.json, how the page's 版本与更新 and the daily COVEY_AUTO_UPGRADE check and upgrade this service
        (app/self_upgrade.py, app/auto_upgrade.py): this checkout's scripts/upgrade.sh (``--status --json``, and a run) for this
        app's state directory. Only when this runs from a git checkout (a release is upgraded by whoever made it: an extension's
        installer writes its own) and never over one another installer wrote; a failure is only said."""
        if not (REPO_ROOT / ".git").exists():
            return
        path = self.data / UPGRADER
        found = read_json(path, None)
        if isinstance(found, dict) and found.get("by") not in (None, "", "covey"):
            return
        script = str(REPO_ROOT / "scripts" / "upgrade.sh")
        where = ["--state-dir", str(self.service_state())]
        try:
            write_json(path, {"by": "covey", "status": [script, "--status", "--json", *where], "run": [script, *where]})
        except OSError as exc:
            self.note(f"没写成 {path}（{exc.strerror or type(exc).__name__}）：网页上不能点一下升级、定时自动升级不会跑；"
                      "终端里的 scripts/upgrade.sh 照常能用")

    def showcase_running(self):
        """`--showcase` for a service running unchanged: the 演示套餐 goes into its manifest the way `covey_service.py set
        --preset showcase` puts it (only the switches it lacks or has off, everything else as it is), never by registering
        again from this shell, which would drop what was set with `covey_service.py set`. A refusal is only said."""
        state = self.service_state()
        registered = self.registered_environment() or {}
        try:
            changed = change_settings(self.data / "bridge-services", state, {}, preset="showcase")["changed"]
        except (OSError, ValueError) as exc:
            self.leftovers.append(f"演示套餐没打开（{exc}）；稍后运行 {self.showcase_command(state, registered)}")
            return
        self.note(f"服务已在运行：演示套餐打开了 {len(changed)} 个开关" if changed else "服务已在运行，演示套餐的开关都开着")

    def preset_butler(self, name):
        """D3: a new service where Claude Code did not answer gets its butler on ``name``, the first of BUTLER_HARNESSES
        that did (core/butler_settings.py preset_harness: the settings file under its state directory, written before its
        first start, only while it names no Harness), so the private chat has a butler from the first message and keeps
        it when Claude Code is installed later. A file that cannot be written is only said (the butler then follows what is
        enabled: app/butler_harness.py)."""
        title = HARNESS_TITLES.get(name, name)
        try:
            written = butler_settings.preset_harness(self.service_state(), name)
        except OSError as exc:
            self.note(f"没能把管家设成 {title}（{exc.strerror or str(exc) or type(exc).__name__}）：装好后在飞书「设置」→「配置」的管家一段换")
            return
        self.note((f"Claude Code 没调通：管家用 {title}" if written else "Claude Code 没调通：管家的 Harness 选过了，照旧")
                  + f"；专项、整理专项照常开，负责人和小模型判断也用 {title}")

    def butler_harness(self):
        """The Harness the butler of this app's service runs on: the one its settings name, else Claude Code when it works
        here, else the first of BUTLER_HARNESSES that works (as app/butler_harness.py picks among the enabled ones)."""
        named = butler_settings.load(self.service_state()).harness
        if named:
            return named
        working = [*getattr(self, "working", ()), *getattr(self, "extra", ())]
        return "claude" if "claude" in working else next((h for h in BUTLER_HARNESSES if h in working), "claude")

    def showcase_command(self, state, environment):
        """The command that turns on the 演示套餐 for the service of ``state``, '' when ``environment`` (its manifest's) has it
        all: `--preset showcase`."""
        changes = switches.preset_changes("showcase", environment)
        head = f"{self.entry('covey_service.py')} set --state-dir {state}"
        return f"{head} --preset showcase" if changes else ""

    def harness_lines(self, state, environment):
        """D3, in the closing lines: what Claude Code not working means for this service (docs/install.md 没有 Claude Code
        时: the butler, a project's lead and the small model's judgments run on the Harness that works) and what to do once
        it works; with Claude Code working, a line while the butler's settings name Codex or Antigravity (a preset's or the
        owner's choice). Where 专项
        is set off on such a service (an older release installed with Codex only wrote it off), the command that turns it
        and 整理专项 on, which need Claude Code no more."""
        set_now = dict(switches.shown(environment))
        projects = switches.is_on("COVEY_PROJECTS", set_now)
        butler = self.butler_harness()
        title = HARNESS_TITLES.get(butler, butler)
        turn_on = f"{self.entry('covey_service.py')} set --state-dir {state} " + " ".join(
            f"{key}={switches.DEFAULTS[key]}" for key in PROJECT_SWITCHES)
        more = "" if projects else f"；专项和整理专项没开，要开：{turn_on}"
        if "claude" in self.working:
            if butler in BUTLER_HARNESSES:
                return [f"Claude Code 能用了：管家用的是 {title}，想换在飞书「设置」→「配置」的管家一段改" + more]
            return []
        if butler not in BUTLER_HARNESSES or butler not in self.working:
            return []
        first = f"Claude Code 没调通：管家用 {title}，专项的负责人和整理专项、同时做几件事的小模型判断也用它" + more
        then = ("装好并登录 Claude Code 以后：重新运行 ./install（它会把 Claude Code 加进启用的 Harness）；"
                "想让管家用它，在「设置」→「配置」的管家一段换成 Claude Code")
        why = self.reasons.get("claude")  # What step 3 found (T766: a colleague saw only "没调通" and not why).
        return [first, *([f"    Claude Code 这次：{why}"] if why else []), then]

    # The service's own environment (D4) ---------------------------------------------------------------------------

    def account_names(self, name):
        """The variables this shell sets that Harness ``name`` may take its account or endpoint from and the service will
        not have: its own (ACCOUNT_PREFIXES), else any credential-like one (a custom gateway's key; the names a manifest
        refuses, service/manager.py SECRET_NAME). Names only, sorted."""
        prefixes = ACCOUNT_PREFIXES.get(name, ())
        kept = [k for k, v in self.env.items() if v and not persistable(k)]
        return sorted(k for k in kept if k.startswith(prefixes)) or sorted(k for k in kept if SECRET_NAME.search(k))

    def hosting_environment(self):
        """What the Manager starts with besides the manifest when systemd hosts it (its user manager's environment:
        ``systemctl --user show-environment``, which ~/.config/environment.d adds to); {} for the other hostings, whose
        Manager gets only the account's basics after a reboot (launchd's plist and cron's line pass few)."""
        try:
            receipt = read_json(self.data / "bridge-services" / "boot-installation.json", {})
        except (OSError, ValueError):
            return {}
        if not isinstance(receipt, dict) or receipt.get("mode") != "systemd-user":
            return {}
        try:
            done = subprocess.run(["systemctl", "--user", "show-environment"], env=self.env, capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return {}
        return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line) if done.returncode == 0 else {}

    def service_probe_env(self):
        """The environment the registered service's Bridge runs with, as the Manager builds it (service/manager.py
        service_environment): the manifest's settings over the account's basics and the hosting's own environment;
        never this shell's other variables (a credential among them)."""
        base = {k: self.env[k] for k in ("HOME", "USER", "LOGNAME", "SHELL") if self.env.get(k)}
        base.update(self.hosting_environment())
        saved = self.registered_environment()
        if saved is None:
            try:
                saved = runtime_environment(self.env)
            except ValueError:
                saved = {}
        env = service_environment(base, saved)
        env.update(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.data / "tmp"))
        return env

    def check_service_env(self):
        """D4: a Harness that answered from this shell may lean on a variable only the shell has (an API key, a gateway's
        address): the service never gets one, so its first task would fail. For each Harness that worked and has such a
        variable here, one more probe in the service's own environment (service_probe_env, a cache of its own); when it
        fails, the names (never a value) and the fix go into the closing lines. Nothing is done without such a variable."""
        suspects = {name: found for name in self.working if (found := self.account_names(name))}
        if not suspects:
            return
        env = self.service_probe_env()
        for name, found in suspects.items():
            result = self.probe(name, env, cwd=str(self.data / "tmp"), directory=self.data / "harness-probe" / "service",
                                refresh=True)
            if result.ok:
                self.note(f"{name}：服务的环境里也能调通")
                continue
            why = {install_probe.NOT_LOGGED_IN: "没登录", install_probe.NETWORK: "网络不通"}.get(result.kind, "调用失败")
            names = "、".join(found)
            self.note(f"{name}：这个终端里能调通，服务的环境里调不通（{why}）")
            if name == "claude":
                settings = Path(self.env.get("CLAUDE_CONFIG_DIR") or "~/.claude") / "settings.json"
                fix = (f"把它们写进 {settings} 的 \"env\"（Claude Code 每次启动都读），或在终端里运行 claude 用 /login 登录"
                       "（登录存在它自己的目录里，服务读得到）")
            elif name == "agy":
                fix = "在终端里运行一次 agy 用 Google 登录（登录存在它自己的目录里，服务读得到）"
            else:
                fix = ("用 codex login 把登录存进 Codex 自己的目录（API key：printenv OPENAI_API_KEY | codex login --with-api-key），"
                       "服务读得到")
            self.leftovers.append(f"{name} 在服务里调不通（{why}）：它可能靠这个终端里的 {names}，服务清单不存凭据，服务里没有它们。"
                                  f"{fix}；改完不用重装，重新运行 ./install 会再查一次")

    # The closing lines (D5) ------------------------------------------------------------------------------------------

    @staticmethod
    def path_of(relative):
        """A file of the checkout as the closing lines name it: relative in the checkout (where ./install runs), absolute
        elsewhere."""
        return relative if Path.cwd().resolve() == REPO_ROOT.resolve() else str(REPO_ROOT / relative)

    def entry(self, name):
        """The command line of a bin/ script, as path_of names it."""
        return f"python3 -B {self.path_of('bin/' + name)}"

    def doctor_lines(self):
        """The doctor's summary of the service root (covey_doctor.py --summary), its lines; [] when it cannot be had."""
        try:
            done = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "bin" / "covey_doctor.py"), "--root",
                                   str(self.data / "bridge-services"), "--runtime-only", "--summary"], env=self.env,
                                  capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in done.stdout.splitlines() if line.strip()]

    def feature_words(self, environment):
        """What the service runs with, in the words of docs/install.md 装好以后: the butler (its Harness working,
        COVEY_BUTLER not off; one on another Harness than Claude Code says which) and the switches of SWITCH_WORDS it has on
        (core/switches.py shown: its manifest's, either spelling; one of DEFAULTS it does not set is on)."""
        set_now = dict(switches.shown(environment))

        def on(key):
            return switches.is_on(key, set_now)

        words = []
        butler = self.butler_harness()
        if butler in self.working + self.extra and str(set_now.get("COVEY_BUTLER", "1")).strip().lower() not in switches.OFF_VALUES:
            words.append("管家" if butler == "claude" else f"管家（{HARNESS_TITLES.get(butler, butler)}）")
        for key, word in SWITCH_WORDS.items():
            if key in PROJECT_PARTS or not on(key) or (key == "COVEY_PROJECT_TIDY" and not on("COVEY_PROJECTS")):
                continue
            if key == "COVEY_PROJECTS":
                parts = [SWITCH_WORDS[part] for part in PROJECT_PARTS if on(part)]
                word += f"（{'、'.join(parts)}）" if parts else ""
            elif key == "COVEY_PROJECT_TIDY" and switches.holds(key, set_now) == "suggest":
                word += "（只提议）"
            elif key == "COVEY_DIGEST_TIMES" and switches.holds(key, set_now) != switches.HIDDEN:
                word += f"（{switches.holds(key, set_now).replace(',', '、')}）"
            words.append(word)
        if web_selfhost.enabled(environment):
            words.append("网页工作台")
        return words

    def closing_lines(self):
        """Where the install put things, what the service runs with and the commands for more (docs/install.md 装好以后):
        printed before the last line once a service is registered."""
        state = self.service_state()
        environment = self.registered_environment() or {}
        service = self.entry("covey_service.py")
        lines = ["", "装在这里：", f"    数据根      {self.data}", f"    状态目录    {state}（日志 {state / 'bridge.log'}）"]
        if self.workdir:
            lines.append(f"    默认工作区  {self.workdir}")
        words = self.feature_words(environment)
        if words:
            lines.append("已经开着：" + "、".join(words))
        lines += self.web_lines(environment)
        lines += self.harness_lines(state, environment)
        lines.append("想多开：")
        showcase = self.showcase_command(state, environment)
        if showcase:
            lines.append(f"    演示套餐  {showcase}")
        if not web_selfhost.enabled(environment):
            lines.append("    网页工作台  ./install --web（在这台机器上自托管，约 1 GB、几分钟；要公网直连加 --web-public <IP或域名>）")
        lines += [f"    单个开关  {service} set --state-dir {state} --dry-run COVEY_DIGEST_TIMES=09:30,21:30"
                  "（先看会改什么，去掉 --dry-run 才改）",
                  "    开关一览  docs/configuration.md「开关速查」",
                  f"出问题时：{self.entry('covey_doctor.py')}（说哪里不对、下一步做什么）；升级：{self.path_of('scripts/upgrade.sh')}"
                  "（docs/deploy.md「日常升级」）"]
        return lines

    def wait_ready(self):
        self.step(6, "等待就绪")
        # Waiting on a scan the run does not wait for: look briefly, the Bridge picks the scan up on its own afterwards.
        seconds = self.ready_seconds if self.ready_seconds is not None else (30 if self.waiting_steps and not self.interactive else 150)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.service_up():
                self.note("机器人已就绪")
                self.ready = True
                return
            time.sleep(3)
        if self.waiting_steps:
            steps = "、".join(str(n) for n in self.waiting_steps)
            self.leftovers.append(f"机器人还没就绪：在等你扫完手机第 {steps} 步（订阅没生效前消息收不到）。扫完后约半分钟内自动就绪，"
                                  "也可以重新运行 ./install 确认")
            return
        command = f"{self.entry('covey_doctor.py')} --root {self.data / 'bridge-services'} --runtime-only"
        found = self.doctor_lines()
        if found:
            self.leftovers.append("服务起来了但还没就绪，诊断这么说：\n" + "\n".join(f"    {line}" for line in found))
        else:
            self.leftovers.append(f"服务起来了但还没就绪；诊断：{command}；日志：{self.service_state() / 'bridge.log'}")

    def waits_for_answer(self):
        """Whether the run waits for the first answer (``wait_answer``): in a terminal, once the service is ready, for a
        deployment whose private chat has had no answer yet; not for one that was in use before (core/welcome.py
        SKIPPED), nor when the records cannot be read."""
        if not (self.interactive and self.ready and self.app_id):
            return False
        state = self.service_state()
        try:
            return welcome.read(state).get("state") != welcome.SKIPPED and welcome.first_answer(state) != welcome.ANSWERED
        except (OSError, ValueError):
            return False

    def wait_answer(self):
        """Until the owner's first private message has been answered (core/welcome.py first_answer), at most
        ANSWER_SECONDS: one line each time it gets further (ANSWER_WORDS), and one once the welcome card is there.
        Ctrl-C ends the wait; running out only says what to look at (TIMEOUT_WORDS)."""
        state = self.service_state()
        deadline = time.monotonic() + (self.answer_seconds if self.answer_seconds is not None else ANSWER_SECONDS)
        said, stage = set(), welcome.NONE
        try:
            while True:
                try:
                    if "card" not in said and welcome.read(state).get("state") == welcome.SENT:
                        said.add("card")
                        self.note("私聊里有一张欢迎卡，点上面的示例也行")
                    stage = welcome.first_answer(state)
                except (OSError, ValueError):
                    pass  # Read again at the next look.
                if stage in ANSWER_WORDS and stage not in said:
                    self.note(STARTED_WORDS if stage == welcome.RECEIVED and welcome.LAUNCH in said else ANSWER_WORDS[stage])
                    said.add(stage)
                if stage == welcome.ANSWERED:
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self.say("    不等了；机器人照常在运行。")
            return
        self.say(TIMEOUT_WORDS[stage])

    # Run ---------------------------------------------------------------------------------------

    def stopped(self, why, next_step=""):
        """What a run that cannot go on prints (D7): where it stopped, each Harness's reason, the next step, the logs."""
        self.say(f"\n安装停在这里：{why}")
        for name, reason in self.reasons.items():
            self.say(f"    {name}：{reason}")
        if next_step:
            self.say(f"下一步：{next_step}")
        logs = self.data / "install-logs"
        if logs.is_dir():
            self.say(f"安装日志在 {logs}（每步一个文件）。改好后重新运行 ./install，做完的步骤会复用。")

    def run(self):
        try:
            self.check_environment()
            self.ensure_lark_cli()
            self.check_harnesses()
            self.retention_hint()
            self.choose_web()
            self.feishu_app()
            if self.interactive:
                self.run_hooks("ext_login")
            config_path = self.write_harness_config()
            home.ensure_shim(self.data, force=True)  # The ``covey`` command every Harness process finds first on PATH.
            if self.args.no_start:
                self.say("已准备到飞书应用为止（--no-start）。")
            else:
                self.start_service(config_path)
                self.wait_ready()
        except Fail as fail:
            self.stopped(str(fail), fail.next)
            return 1
        except (OSError, subprocess.SubprocessError) as exc:  # A command that hung or a file that would not be written.
            self.stopped(f"{type(exc).__name__}: {exc}", f"多半是网络慢或权限不够：{AGAIN}；一直这样时把这几行和安装日志一起"
                         "发给帮你装的人")
            return 1
        except KeyboardInterrupt:
            self.say("\n已取消；重新运行 ./install 会接着做缺的。")
            return 130
        finally:
            self.stop_web_build()
        for line in self.leftovers:
            self.say(f"还差：{line}")
        if not self.args.no_start and self.app_id:
            for line in self.closing_lines():
                self.say(line)
        name = self.app_name_of(self.args.profile) if self.app_id else ""
        bot = f"机器人「{name}」" if name else "刚建的机器人"
        if not self.waits_for_answer():
            self.say(f"\n完成。去飞书私聊{bot}发一句话。")
            return 0
        self.say(f"\n完成。去飞书私聊{bot}发一句话，这里等它回答（最多 {MINUTES} 分钟，Ctrl-C 不等）。")
        self.wait_answer()
        return 0


def main(argv=None):
    args = parser().parse_args(argv)
    return Installer(args).run()


if __name__ == "__main__":
    sys.exit(main())
