"""./install 的安装器本体：一遍跑完，扫一次码，最后发一句话就有回答（docs/install.md）。

步骤（每步一行标题，之后只有必要的提示）：
  1 检查环境和目录   Python、平台；数据根、工作区、临时目录、浏览器和 lark-cli 的安装位置都给默认值并自动建
  2 准备 lark-cli   没有就装（npm 全局，不行装到数据根下 tools/）；``--skills`` 才同步 Skills
  3 检查 Harness    对 claude、codex 各做一次真实调用（service/install_probe.py），分清没登录、网络不通、其他；
                    像网络问题时问一次代理地址；通过后把代理写进服务环境；扩展的安装钩子在这里跑
  4 飞书应用        一个无头浏览器扫一次码：登录、建应用、授权、开发者后台补回调/事件/菜单/权限并发布、读回核对
                    （feishu/install_browser.py）；浏览器装不上或 ``--no-browser`` 时退回手机多次扫码
  5 注册并启动服务   scripts/bootstrap_harness_bridge.sh，先 bin/covey_service_install.py --mode auto --activate（托管），再 bootstrap（注册）
  6 等待就绪        bin/covey_doctor.py 退出码 0

幂等：已有的 lark-cli profile、授权、应用配置、服务都复用，只做缺的；``--reconfigure`` 才重来。
浏览器登录态放本次运行的临时目录，结束时删掉（``--keep-login`` 留下并说明路径）。
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
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from covey.core import app_manifest, extensions, qr
from covey.core import env as cenv
from covey.core.paths import REPO_ROOT, data_home
from covey.core.state import write_json
from covey.feishu.app_config import remove, survey
from covey.service import browser_setup, install_probe
from covey.service.app_check import draw, profile_app_id, profile_app_name
from covey.service.manager import PROXY_ENV_KEYS, persistable, validate_environment

DEFAULT_PROFILE = "covey"
DEFAULT_APP_NAME = "Covey"
LARK_DOMAINS = "im,docs,drive,contact"
FEISHU_NO_PROXY = ".feishu.cn,.larksuite.com,.larkoffice.com,.feishucdn.com,localhost,127.0.0.1"  # privacy: allow 飞书自己的域名
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY")
PROBED = ("claude", "codex")  # The Harnesses one real call proves; dsh, Pi and an extension's need set-up of their own.
STEPS = 6
URL = re.compile(r"https://\S+")
RELEASES = "https://nodejs.org/"  # Where a machine without npm gets Node.js.


def say_now(text):
    """print that reaches a pipe or a log at once (a killed run still shows what it said)."""
    print(text, flush=True)


class Fail(Exception):
    """A step that cannot go on; the message is what the user reads."""


def parser():
    p = argparse.ArgumentParser(prog="covey_install.py", description=__doc__.split("\n\n")[0] + "\n\n" +
                                "前提：这台机器上已装好并登录了 Claude Code 或 Codex（见 docs/harnesses.md）。")
    p.add_argument("--reconfigure", action="store_true", help="重来：重新建应用、授权、补配置并重注册服务")
    p.add_argument("--skills", action="store_true", help="同时把 skills/ 同步到 Codex 和 Claude 的 Skills 目录（默认不做）")
    p.add_argument("--non-interactive", action="store_true", help="不提问（代理地址等）；缺什么就报什么退出")
    p.add_argument("--data-home", help="数据根（默认：仓库同级的 <仓库目录名>-data，或 LARK_BRIDGE_DATA_HOME）")
    p.add_argument("--workdir", help="默认工作区（默认：当前目录；在仓库里运行时用 <数据根>/workspace）")
    p.add_argument("--keep-login", action="store_true", help="结束后不删浏览器里主人的网页登录（默认删）")
    p.add_argument("--no-browser", action="store_true", help="不用无头浏览器：每个链接画成二维码，手机上依次打开")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help=f"lark-cli profile 名（默认 {DEFAULT_PROFILE}）")
    p.add_argument("--app-name", default=DEFAULT_APP_NAME, help=f"新建飞书应用的名称（默认 {DEFAULT_APP_NAME}）")
    p.add_argument("--browser-login-dir", help=argparse.SUPPRESS)  # 验收用：浏览器从这份已登录的用户目录副本开始，跳过二维码
    p.add_argument("--no-autostart", action="store_true", help="没有 systemd/launchd 时不往 crontab 加开机自启的一行")
    p.add_argument("--no-start", action="store_true", help="只准备到飞书应用为止，不注册、不启动服务")
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
                raise Fail("浏览器进程意外退出")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if self.handle(event) == kind:
                return event
        raise Fail("浏览器没有响应")

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


class Installer:
    """One run of ./install. ``say``, ``ask`` and the callables below are replaceable so tests need no network."""

    def __init__(self, args, *, env=None, say=say_now, ask=input, interactive=None, probe=install_probe.probe,
                 browser_factory=None, survey_app=survey, app_id_of=profile_app_id, app_name_of=profile_app_name, prepare_browser=browser_setup.prepare):
        self.args, self.say, self.ask = args, say, ask
        # The old spelling inside: what is saved, fingerprinted and passed on is what an older release reads too (core/env.py).
        self.env = cenv.canonical(dict(os.environ if env is None else env))
        self.interactive = (not args.non_interactive and sys.stdin.isatty()) if interactive is None else interactive
        self.probe, self.survey_app, self.app_id_of, self.app_name_of = probe, survey_app, app_id_of, app_name_of
        self.ready_seconds = None  # tests
        self.waiting_steps = []  # Phone steps (their numbers) whose scan is not seen in the app's online version yet
        self.browser_factory, self.browser, self.browser_home = browser_factory, None, None
        self.data = Path(args.data_home).expanduser().resolve() if args.data_home else (
            Path(self.env["LARK_BRIDGE_DATA_HOME"]).expanduser().resolve() if self.env.get("LARK_BRIDGE_DATA_HOME")
            else data_home())
        self.workdir, self.phone_step, self.poll_seconds = None, 0, 5
        self.browser_python, self.live_open, self.prepare_browser = None, False, prepare_browser
        self.working, self.extra, self.reasons, self.leftovers = [], [], {}, []
        self.app_id = ""

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
            raise Fail("需要 Python 3.10 或更新")
        if platform.system() not in ("Linux", "Darwin"):
            raise Fail("目前只支持 Linux 和 macOS")
        for sub in ("", "tmp", "browsers", "tools"):
            (self.data / sub).mkdir(parents=True, exist_ok=True)
        if not os.access(self.data, os.W_OK):
            raise Fail(f"数据根不可写：{self.data}")
        self.env["LARK_BRIDGE_DATA_HOME"] = str(self.data)
        self.env.setdefault("TMPDIR", str(self.data / "tmp"))
        self.env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.data / "browsers")
        if self.args.workdir:
            self.workdir = Path(self.args.workdir).expanduser().resolve()
        else:
            here = Path.cwd().resolve()
            self.workdir = self.data / "workspace" if here == REPO_ROOT.resolve() else here
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.note(f"数据根 {self.data}；默认工作区 {self.workdir}")

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
                raise Fail(f"没有 lark-cli，也没有 npm 可以装它。先装 Node.js（{RELEASES}），再重新运行 ./install")
            self.note("安装 lark-cli（npm）")
            code, tail = self.run_logged("lark-cli-install", [npm, "install", "-g", "@larksuite/cli"], timeout=600)
            if code != 0 or not self.which("lark-cli"):
                prefix = self.data / "tools"
                code, tail = self.run_logged("lark-cli-install", [npm, "install", "--prefix", str(prefix),
                                                                  "@larksuite/cli"], timeout=600)
                self.env["PATH"] = f"{prefix / 'node_modules' / '.bin'}{os.pathsep}{self.env.get('PATH', '')}"
                if code != 0 or not self.which("lark-cli"):
                    raise Fail(f"lark-cli 装不上：{tail or '见 ' + str(self.log('lark-cli-install'))}")
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
                  'warn() { echo "$*" >&2; }\nSKIP_BYTEDCLI=0\nSKIP_LOGIN=0\n'  # privacy: allow 扩展钩子约定的变量名
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
        self.env["HTTPS_PROXY"] = self.env["HTTP_PROXY"] = url

    def finish_proxy(self):
        """When any proxy is set, its non-secret form goes into the service's environment (the launch saves it) and the
        Feishu domains stay direct; a proxy URL with credentials is refused, never printed."""
        if not self.proxied():
            return
        keep = [h for h in self.env.get("NO_PROXY", self.env.get("no_proxy", "")).split(",") if h]
        self.env["NO_PROXY"] = ",".join(dict.fromkeys(keep + FEISHU_NO_PROXY.split(",")))
        self.env.pop("no_proxy", None)
        try:
            validate_environment({k: v for k, v in self.env.items() if k in PROXY_ENV_KEYS})
        except ValueError:
            raise Fail("代理地址带了用户名或密码，不能写进服务；换一个不带凭据的代理地址再运行") from None
        self.note("代理设置会一起写进服务（飞书域名直连）")

    def ask_proxy(self, name):
        words = f"{name} 像是网络不通。"
        if not self.interactive:
            self.note(words + "需要代理时先 export HTTPS_PROXY=http://主机:端口 再运行 ./install")
            return ""
        answer = self.ask(f"    {words}要走代理的话输入代理地址（如 http://127.0.0.1:7890），不用就直接回车：").strip()
        return answer

    def check_harnesses(self):
        self.step(3, "检查 Harness（各调用一次模型）")
        installed = [n for n in PROBED if self.which(n)]
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
                self.reasons[name] = "没装"
        self.extra = [h for h in extra if h not in self.working]
        for h in self.extra:
            self.note(f"{h}：扩展提供，沿用它自己的检查")
        if not self.working and not self.extra:
            raise Fail("没有能调通的 Harness。先装好并登录 Claude Code 或 Codex（官方链接见 docs/harnesses.md），再重新运行 ./install")
        self.finish_proxy()

    def write_harness_config(self):
        """<data>/harnesses.json: the working Harnesses enabled, the default (the roster's first) first; what else the
        file holds stays, and so do the enabled ones that are not probed here."""
        from covey.core.roster import NAMES

        path = self.data / "harnesses.json"
        try:
            config = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        old = config.get("enabled_harnesses") if isinstance(config.get("enabled_harnesses"), list) else []
        if old:  # Upgrade: what the owner enabled stays; a failed probe is only said (the launch card shows it unavailable).
            for n in old:
                if n in PROBED and n not in self.working:
                    self.leftovers.append(f"{n} {self.reasons.get(n, '探测没通过')}，启动卡上会显示不可用（仍保留在已启用里）")
            enabled = [n for n in NAMES if n in old or n in self.extra]
        else:
            enabled = [n for n in NAMES if n in self.working or n in self.extra]
        config["enabled_harnesses"] = enabled
        write_json(path, config)
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

    def ensure_browser(self):
        """The Python that has Playwright and a browser that starts, prepared under the data root (service/browser_setup.py:
        a Chrome the machine has, else Chromium downloaded with mirrors, plus system libraries when they are missing);
        None (and why) when it cannot be had."""
        if self.browser_python is not None:
            return self.browser_python or None
        self.browser_python = ""
        if self.args.no_browser:
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
            self.note("改用手机扫码：下面每个链接画成二维码，依次扫（以后想直接这样，加 --no-browser）")
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
                raise Fail("等太久了：lark-cli 还没有结束")
            if proc.wait(timeout=30) != 0:
                raise Fail("lark-cli 没有成功：" + " | ".join(meaningful(seen)[-3:]) + f"（完整输出：{self.log(flow_name(argv))}）")
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
        self.step(4, "飞书应用（扫一次码）")
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
            lacking = self.lacking()
            if lacking and reused and not self.interactive and not self.args.browser_login_dir:
                # An upgrade with nobody to scan (no login copy either): list what is missing, go on with the service; the Bridge's 飞书应用待补 card finishes it.
                self.leftovers.append(f"飞书应用还缺 {app_manifest.joined(lacking)}。非交互不等扫码，服务照常换成新代码；"
                                      "给机器人发一句话后会收到「飞书应用待补」卡，点「扫码自动补齐」（或交互运行 ./install）")
            elif lacking:
                self.configure_app(python or self.ensure_browser(), lacking)
        finally:
            if self.browser is not None:
                self.browser.close()
                self.browser = None
            if self.browser_home is not None:
                if self.args.keep_login:
                    self.note(f"浏览器登录留在 {self.browser_home}（--keep-login）；用完请删掉这个目录")
                else:
                    remove(self.browser_home, self.note)

    def create_and_authorize(self, python, have_profile):
        profile = self.args.profile
        init = ["lark-cli", "config", "init", "--new", "--name", profile, "--brand", "feishu", "--lang", "zh"]
        login = ["lark-cli", "--profile", profile, "auth", "login", "--domain", LARK_DOMAINS]
        if python:
            session = self.open_browser(python)
            if not have_profile:
                self.note("在浏览器里建应用")
                self.run_flow(init, lambda link: self.expect(session.create_app(link, self.args.app_name)))
            self.note("在浏览器里授权")
            self.run_flow(login, lambda link: self.expect(session.authorize(link)))
        else:
            self.note("手机上共 3 步：1 建应用、2 授权、3 事件与回调订阅（一个链接）；后面的链接要等前一步做完才有"
                      + ("" if self.interactive else "。非交互时第 3 步不等待：列出链接，扫完后重新运行 ./install 或等几分钟"))
            if not have_profile:
                self.run_flow(init, lambda link: self.by_phone(f"建应用（建议把名称改成「{self.args.app_name}」，点「创建」）", link))
            self.run_flow(login, lambda link: self.by_phone("授权（点「开通并授权」，弹窗里再点一次）", link))
        self.app_id = self.app_id_of(profile)
        if not self.app_id:
            raise Fail("读不到应用的 appId：lark-cli 的 profile 没建好，重新运行 ./install --reconfigure")
        self.note(f"应用 {self.app_id} 已建好并授权")

    @staticmethod
    def expect(result):
        if not result.get("ok"):
            raise Fail(f"浏览器这一步没做成：{result.get('detail')}")

    def lacking(self):
        online, lacking, full = self.survey_app(self.args.profile, self.app_id)
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
                                  "「飞书应用待补」卡，点「扫码自动补齐」就行（或按 docs/operations.md 的「手动配置飞书应用」做）")

    def lacking_after(self, offer):
        ids = {n.id for n in offer}
        return [n for n in self.lacking() if n.id in ids]

    # 5, 6 --------------------------------------------------------------------------------------

    def service_state(self):
        return self.data / f"harness-{self.app_id}"

    def fingerprint(self, config_path):
        proxy = {k: self.env[k] for k in sorted(PROXY_ENV_KEYS) if self.env.get(k)}
        # The code (REPO_ROOT) and the LARK_BRIDGE_* settings are part of it: an upgrade into another clone, or with
        # other switches, has to re-register instead of "nothing changed".
        settings = {k: v for k, v in sorted(self.env.items()) if k.startswith("LARK_BRIDGE_") and persistable(k)}
        return {"profile": self.args.profile, "app_id": self.app_id, "workdir": str(self.workdir),
                "harnesses": sorted(self.working + self.extra), "proxy": proxy, "config": str(config_path),
                "repo": str(REPO_ROOT), "settings": settings}

    def service_up(self):
        root = self.data / "bridge-services"
        code = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "bin" / "covey_doctor.py"), "--root", str(root),
                               "--runtime-only"], env=self.env, capture_output=True, timeout=120).returncode
        return code == 0

    def start_service(self, config_path):
        self.step(5, "注册并启动服务")
        record = self.data / "install.json"
        now = self.fingerprint(config_path)
        try:
            before = json.loads(record.read_text())
        except (OSError, ValueError):
            before = None
        if before == now and not self.args.reconfigure and self.service_up():
            self.note("服务已在运行，配置没变")
            return
        # Hosting first (the unit's Manager takes the single-instance lock), then the registration; without systemd or
        # launchd ("manager" mode) nothing is hosted here and the bootstrap's own launch starts the Manager.
        code, tail = self.run_logged("service-install", [
            sys.executable, "-B", str(REPO_ROOT / "bin" / "covey_service_install.py"), "--root", str(self.data / "bridge-services"),
            "--mode", "auto", "--activate", *(["--no-autostart"] if self.args.no_autostart else [])], timeout=300)
        hosted = code == 0
        if hosted:
            self.note(f"开机自启：{boot_words(self.log('service-install'))}")
        code, tail_boot = self.run_logged("bootstrap", [
            str(REPO_ROOT / "scripts" / "bootstrap_harness_bridge.sh"), "--workdir", str(self.workdir),
            "--profile", self.args.profile, "--app-id", self.app_id, "--harness-config", str(config_path)], timeout=300)
        if code != 0:
            raise Fail(f"服务没注册成功：{tail_boot}")
        write_json(record, now)
        if not hosted:
            self.leftovers.append("开机自启没注册上（服务已在运行，重启这台机器后要再运行 ./install）：" + tail)

    def wait_ready(self):
        self.step(6, "等待就绪")
        # Waiting on a scan the run does not wait for: look briefly, the Bridge picks the scan up on its own afterwards.
        seconds = self.ready_seconds if self.ready_seconds is not None else (30 if self.waiting_steps and not self.interactive else 150)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.service_up():
                self.note("机器人已就绪")
                return
            time.sleep(3)
        if self.waiting_steps:
            steps = "、".join(str(n) for n in self.waiting_steps)
            self.leftovers.append(f"机器人还没就绪：在等你扫完手机第 {steps} 步（订阅没生效前消息收不到）。扫完后约半分钟内自动就绪，"
                                  "也可以重新运行 ./install 确认")
            return
        self.leftovers.append(f"服务起来了但还没就绪；诊断：python3 -B bin/covey_doctor.py --root "
                              f"{self.data / 'bridge-services'} --runtime-only")

    # Run ---------------------------------------------------------------------------------------

    def run(self):
        try:
            self.check_environment()
            self.ensure_lark_cli()
            self.check_harnesses()
            self.feishu_app()
            if self.interactive:
                self.run_hooks("ext_login")
            config_path = self.write_harness_config()
            if self.args.no_start:
                self.say("已准备到飞书应用为止（--no-start）。")
            else:
                self.start_service(config_path)
                self.wait_ready()
        except Fail as fail:
            self.say(f"\n安装停在这里：{fail}")
            for name, why in self.reasons.items():
                self.say(f"    {name}：{why}")
            return 1
        except KeyboardInterrupt:
            self.say("\n已取消；重新运行 ./install 会接着做缺的。")
            return 130
        for line in self.leftovers:
            self.say(f"还差：{line}")
        name = self.app_name_of(self.args.profile) if self.app_id else ""
        self.say(f"\n完成。去飞书私聊机器人「{name}」发一句话。" if name else "\n完成。去飞书私聊刚建的机器人发一句话。")
        return 0


def main(argv=None):
    args = parser().parse_args(argv)
    return Installer(args).run()


if __name__ == "__main__":
    sys.exit(main())
