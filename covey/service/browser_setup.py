"""./install 准备无头浏览器的全部细节（service/installer.py 调用）：用机器上已有的 Chrome/Chromium，没有就下载 Playwright 的
Chromium（显示进度、卡住换镜像、总超时）；起不来时补系统库：有 root 用 ``playwright install-deps``，没有 root 在 Debian/Ubuntu 上
``apt-get download`` 需要的包、解到数据根下，只给浏览器进程设 LD_LIBRARY_PATH（结果记在 ``<数据根>/browsers/launch.json``，
core/browser_env.py 读它）。别的发行版或失败时返回原因，由安装器退回手机扫码。只用标准库。

下载来源的顺序按实测定（2026-11-14，测试机直连）：官方 CDN 约 35 MB/s、npmmirror 约 10 MB/s、经公司代理只有约 0.1 MB/s，
所以先直连官方，再直连镜像，最后才带代理变量重试。"""
from __future__ import annotations

import glob
import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from covey.core import env as cenv
from covey.core import browser_env

PLAYWRIGHT_VERSION = "1.56.0"
MIRROR = "https://cdn.npmmirror.com/binaries/playwright"  # builds/chromium/<rev>/…zip 已核实可下
STALL_SECONDS = 60
SLOW_CHECK_SECONDS = 25  # 一个文件开始下载这么久之后，按已下载字节估算剩余时间
SLOW_ETA_SECONDS = 180  # 预计剩余超过这么久就换下一个源（最后一个源不换，只显示预计时间）
TOTAL_SECONDS = 1500
PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy",
              "npm_config_https_proxy", "npm_config_http_proxy", "npm_config_proxy",
              "NPM_CONFIG_HTTPS_PROXY", "NPM_CONFIG_HTTP_PROXY", "NPM_CONFIG_PROXY")  # Playwright 下载器也认 npm 的这几个
UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2, "gb": 1000 ** 3, "gib": 1024 ** 3}
PROGRESS = re.compile(r"(\d+)% of ([\d.]+ ?\w+)")
DOWNLOADING = re.compile(r"^Downloading (.+?)(?: [\d.]+)? \(?playwright build")
SYSTEM_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
MAC_BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium")
# Playwright's list of Chromium's system packages on Debian/Ubuntu (a t64 name is tried second: Ubuntu 24.04, Debian 13).
CHROMIUM_DEBS = ("libasound2", "libatk-bridge2.0-0", "libatk1.0-0", "libatspi2.0-0", "libcairo2", "libcups2", "libdbus-1-3",
                 "libdrm2", "libgbm1", "libglib2.0-0", "libnspr4", "libnss3", "libpango-1.0-0", "libx11-6", "libxcb1",
                 "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3", "libxkbcommon0", "libxrandr2")
# Packages that would replace a library the system itself loads (a second libsystemd, say).
UNSAFE_DEBS = ("libelogind0", "libsystemd0", "libc6", "libgcc-s1", "libstdc++6")
# Runs alone under the Playwright interpreter (-I -c): it imports nothing from covey, so no cenv here.
CHECK_SCRIPT = """
import os, sys
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        exe = os.environ.get("COVEY_BROWSER_EXECUTABLE") or os.environ.get("LARK_BRIDGE_BROWSER_EXECUTABLE") or None
        b = p.chromium.launch(headless=True, executable_path=exe)
        b.new_page().goto("about:blank")
        b.close()
except Exception as exc:
    print(" ".join(str(exc).split())[:500], file=sys.stderr)
    sys.exit(1)
"""


@dataclass
class Prepared:
    python: str
    executable: str = ""
    libs: list = field(default_factory=list)


class Unavailable(Exception):
    """The browser cannot be had here; the message says why (the installer goes on by phone)."""


def direct(env):
    return {k: v for k, v in env.items() if k not in PROXY_KEYS}


def has_proxy(env):
    return any(env.get(k) for k in PROXY_KEYS)


def size_bytes(text):
    """'160.5 MiB' -> bytes; 0 when unreadable."""
    found = re.match(r"([\d.]+) ?(\w+)", text.strip())
    try:
        return int(float(found.group(1)) * UNITS.get(found.group(2).lower(), 0)) if found else 0
    except ValueError:
        return 0


def speed_eta(percent, total, elapsed):
    """(bytes per second, seconds left) of one file from its percent, size and the seconds since it began; None when unknown."""
    done = total * percent / 100
    if elapsed <= 0 or done <= 0:
        return None
    rate = done / elapsed
    return rate, (total - done) / rate


def speed_text(rate, eta):
    left = f"{eta / 60:.1f} 分钟" if eta >= 90 else f"{eta:.0f} 秒"
    return f"{rate / 1e6:.1f} MB/s，预计还要 {left}"


def tree_size(*paths):
    total = 0
    for top in paths:
        for root, _dirs, files in os.walk(top):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    return total


def pump(proc):
    lines = queue.Queue()

    def read():
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    return lines


def stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# 浏览器 ---------------------------------------------------------------------------------------------------------------

def system_browser(env):
    """A Chrome or Chromium this machine already has: the variable, a name on PATH, or macOS's app; '' when none."""
    named = cenv.get(browser_env.EXECUTABLE, "", env)
    if named and os.access(named, os.X_OK):
        return named
    for name in SYSTEM_BROWSERS:
        found = shutil.which(name, path=env.get("PATH", ""))
        if found:
            return found
    for path in MAC_BROWSERS if platform.system() == "Darwin" else ():
        if os.access(path, os.X_OK):
            return path
    return ""


def browser_starts(python, env, executable="", timeout=90):
    """(ok, one line) of one headless start of the browser under ``python`` (which has Playwright)."""
    env = dict(env)
    for spelling in cenv.spellings(browser_env.EXECUTABLE):
        env.pop(spelling, None)
    if executable:
        cenv.put(env, browser_env.EXECUTABLE, executable)
    try:
        done = subprocess.run([python, "-I", "-c", CHECK_SCRIPT], env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "启动超时"
    return done.returncode == 0, " ".join((done.stderr or done.stdout).split())[-400:]


def remember(folder, executable, libs):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / browser_env.LAUNCH_FILE
    if executable or libs:
        path.write_text(json.dumps({"executable": executable, "libs": [str(x) for x in libs]}))
    else:
        path.unlink(missing_ok=True)


def chromium_installed(folder):
    return bool(glob.glob(str(Path(folder) / "chromium_headless_shell-*" / "INSTALLATION_COMPLETE")))


def run_install(python, env, say, live, *, stall, deadline, tmp_dirs, source="", slow_eta=None):
    """One ``playwright install chromium``; (ok, reason). Progress on one refreshed line (``live``) or, without one, a line
    per 10%, each with the source and the speed; no progress (output or growing files) for ``stall`` seconds kills it, and so
    does a file that, after SLOW_CHECK_SECONDS, would still need more than ``slow_eta`` seconds (None: only show the estimate)."""
    env = {**env, "PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS": "1"}  # The libraries are our own step.
    proc = subprocess.Popen([python, "-m", "playwright", "install", "chromium"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    lines, seen = pump(proc), []
    last, size, percent, title, began = time.monotonic(), -1, -1, "", 0.0
    tag = f"[{source}] " if source else ""
    while True:
        try:
            line = lines.get(timeout=2)
        except queue.Empty:
            line = ""
            now = tree_size(*tmp_dirs)
            if now != size:
                size, last = now, time.monotonic()
        else:
            if line is None:
                break
            last = time.monotonic()
        if line:
            seen.append(line)
            found = DOWNLOADING.search(line)
            if found:
                title, percent, began = found.group(1), -1, time.monotonic()
                if live:
                    live(None)
                say(f"    下载 {title}")
            progress = PROGRESS.search(line)
            if progress and title:
                now_percent = int(progress.group(1))
                total = size_bytes(progress.group(2))
                estimate = speed_eta(now_percent, total, time.monotonic() - began) if now_percent < 100 else None
                extra = f"  {speed_text(*estimate)}" if estimate and time.monotonic() - began >= 3 else ""
                if live:
                    live(f"    {tag}{now_percent:3d}% of {progress.group(2)}{extra}")
                elif now_percent // 10 > percent // 10 or now_percent == 100 and percent < 100:
                    say(f"    {tag}{now_percent}% of {progress.group(2)}{extra}")
                percent = now_percent
                if (estimate and slow_eta and time.monotonic() - began >= SLOW_CHECK_SECONDS and estimate[1] > slow_eta):
                    stop(proc)
                    return False, f"太慢（{speed_text(*estimate)}）"
        if time.monotonic() - last > stall:
            stop(proc)
            return False, f"{stall} 秒没有进展"
        if time.monotonic() > deadline:
            stop(proc)
            return False, "总时间超了"
    if live:
        live(None)
    code = proc.wait()
    return code == 0, "" if code == 0 else " | ".join(seen[-3:])[-300:]


def download_chromium(python, env, say, folder, tmp_dir, *, live=None, stall=STALL_SECONDS, total=TOTAL_SECONDS,
                      mirror=MIRROR, runner=run_install):
    """Chromium into ``folder``: official CDN direct, the mirror direct, then (when a proxy is set) both through it.
    Raises Unavailable with the last reason."""
    deadline = time.monotonic() + total
    plan = [("官方源", "", False), ("镜像 npmmirror", mirror, False)]
    if has_proxy(env):
        plan += [("官方源（走代理）", "", True), ("镜像（走代理）", mirror, True)]
    reason = ""
    for number, (name, host, proxied) in enumerate(plan, 1):
        e = dict(env if proxied else direct(env))
        e.pop("PLAYWRIGHT_DOWNLOAD_HOST", None)
        if host:
            e["PLAYWRIGHT_DOWNLOAD_HOST"] = host
        last_source = number == len(plan)
        say(f"    浏览器下载：{name}（{stall} 秒没进展或预计超过 {SLOW_ETA_SECONDS // 60} 分钟会自动换源）" if not last_source
            else f"    浏览器下载：{name}（最后一个源，不再换，只显示预计时间）")
        ok, reason = runner(python, e, say, live, stall=stall, deadline=deadline, tmp_dirs=[folder, tmp_dir], source=name,
                            slow_eta=None if last_source else SLOW_ETA_SECONDS)
        if ok:
            return
        say(f"    {name}没成功：{reason}")
        if time.monotonic() > deadline:
            break
    raise Unavailable(f"Chromium 下载失败：{reason}")


# 系统库 ---------------------------------------------------------------------------------------------------------------

def browser_binaries(folder):
    pats = ("chromium_headless_shell-*/*/headless_shell", "chromium-*/chrome-linux*/chrome")
    return [p for pat in pats for p in glob.glob(str(Path(folder) / pat))]


def missing_libs(binaries, ld_dirs=(), env=None):
    """The shared libraries ``ldd`` cannot find for the browser binaries (sorted, unique)."""
    env = dict(os.environ if env is None else env)
    if ld_dirs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join([*map(str, ld_dirs), env.get("LD_LIBRARY_PATH", "")])
    if not shutil.which("ldd", path=env.get("PATH")):
        return []
    found = set()
    for binary in binaries:
        done = subprocess.run(["ldd", binary], env=env, capture_output=True, text=True, timeout=60)
        found.update(m.group(1) for m in re.finditer(r"^\s*(\S+) => not found", done.stdout, re.M))
    return sorted(found)


def debian_like():
    try:
        text = Path("/etc/os-release").read_text().lower()
    except OSError:
        return False
    ids = " ".join(m for m in re.findall(r'^id(?:_like)?="?([^"\n]*)', text, re.M))
    return bool(shutil.which("apt-get")) and ("debian" in ids or "ubuntu" in ids)


def can_root(env):
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=15).returncode == 0:
        return ["sudo", "-n"]
    return None


def deb_names(run=subprocess.run):
    """The packages (Chromium's list, plus what they need that is not installed) to fetch, by name."""
    def out(argv):
        done = run(argv, capture_output=True, text=True, timeout=120)
        return done.stdout if done.returncode == 0 else ""

    base = []
    for name in CHROMIUM_DEBS:
        for candidate in (name, name + "t64"):
            if out(["apt-cache", "show", "--no-all-versions", candidate]).strip():
                base.append(candidate)
                break
    deps = out(["apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests", "--no-conflicts", "--no-breaks",
                "--no-replaces", "--no-enhances", *base])
    names = [x.strip() for x in deps.splitlines() if x and not x[0].isspace() and not x.startswith("<")]
    names = list(dict.fromkeys([*base, *names]))
    status = out(["dpkg-query", "-W", "-f", "${Package} ${db:Status-Abbrev}\n"])
    installed = {line.split()[0] for line in status.splitlines() if line.endswith("ii ") or line.split()[-1:] == ["ii"]}
    keep = [n for n in names if n.split(":")[0] not in installed and n not in UNSAFE_DEBS
            and (n.startswith("lib") or n.endswith(("-data", "-common")))]  # libraries and their data, no fonts or installers
    return keep[:80]


def fetch_libs(folder, env, say, run=subprocess.run):
    """apt-get download the missing packages and dpkg -x them under ``folder``; the library directories."""
    work = Path(folder) / "syslibs"
    debs, root = work / "debs", work / "root"
    debs.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    names = deb_names(run)
    if not names:
        raise Unavailable("apt 里找不到 Chromium 需要的系统库包（apt 列表可能没更新，需要 root 执行 apt-get update）")
    say(f"    没有 root：从 apt 下载 {len(names)} 个系统库包，解到 {work}（只给浏览器进程用）")
    for e, label in ((direct(env), "直连"), (env, "走代理")):
        if label == "走代理" and not has_proxy(env):
            break
        done = run(["apt-get", "download", *names], cwd=str(debs), env=e, capture_output=True, text=True, timeout=600)
        if done.returncode == 0 or list(debs.glob("*.deb")):
            break
    files = sorted(debs.glob("*.deb"))
    if not files:
        raise Unavailable("apt-get download 没下到包：" + " ".join((done.stderr or "").split())[-200:])
    for deb in files:
        run(["dpkg", "-x", str(deb), str(root)], check=True, capture_output=True, timeout=120)
    return [str(p) for pat in ("usr/lib/*-linux-gnu", "lib/*-linux-gnu", "usr/lib") for p in sorted(root.glob(pat)) if p.is_dir()]


def ensure_libs(python, env, say, folder):
    """Make the libraries the browser binaries lack available; the directories for LD_LIBRARY_PATH (maybe none).
    Raises Unavailable when it cannot."""
    binaries = browser_binaries(folder)
    lacking = missing_libs(binaries, env=env)
    if not lacking:
        return []
    say(f"    浏览器缺 {len(lacking)} 个系统库：{', '.join(lacking[:6])}{' …' if len(lacking) > 6 else ''}")
    prefix = can_root(env)
    if prefix is not None:
        say("    用 root 装：playwright install-deps chromium")
        done = subprocess.run([*prefix, python, "-m", "playwright", "install-deps", "chromium"], env=env,
                              capture_output=True, text=True, timeout=900)
        if done.returncode == 0 and not missing_libs(binaries, env=env):
            return []
        say("    install-deps 没成功，改用不需要 root 的办法")
    if not debian_like():
        raise Unavailable("这个系统不是 Debian/Ubuntu，缺的系统库请用系统的包管理器装（Playwright 的列表：python -m playwright "
                          "install-deps --dry-run chromium）")
    dirs = fetch_libs(folder, env, say)
    still = missing_libs(binaries, dirs, env)
    if still:
        raise Unavailable("系统库补不齐，还缺：" + ", ".join(still[:6]))
    return dirs


# 总入口 ---------------------------------------------------------------------------------------------------------------

def make_venv(venv, env, say):
    python = Path(venv) / "bin" / "python"
    if not python.exists():
        Path(venv).parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise Unavailable("建不了 Python 虚拟环境（Debian/Ubuntu 需要 python3-venv 包）：" + " ".join(done.stderr.split())[-150:])
    if subprocess.run([str(python), "-c", "import playwright"], capture_output=True).returncode != 0:
        say("    安装 Playwright（pip）")
        cache = str(Path(venv).parent / "pip-cache")
        reason = ""
        for e in (direct(env), env) if has_proxy(env) else (env,):
            done = subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                   "--default-timeout", "30", f"playwright=={PLAYWRIGHT_VERSION}"],
                                  env={**e, "PIP_CACHE_DIR": cache}, capture_output=True, text=True, timeout=600)
            if done.returncode == 0:
                break
            reason = " ".join(done.stderr.split())[-200:]
        else:
            raise Unavailable("pip 装不了 Playwright：" + reason)
    return str(python)


def prepare(data, env, say, *, live=None, runner=run_install, **options):
    """The Python (with Playwright) and what its browser needs; a Prepared, or Unavailable with the reason."""
    folder = Path(data) / "browsers"
    folder.mkdir(parents=True, exist_ok=True)
    python = make_venv(Path(data) / "proxy-web" / "venv", env, say)
    env = {**env, "PLAYWRIGHT_BROWSERS_PATH": str(folder)}
    own = system_browser(env)
    if own:
        ok, why = browser_starts(python, env, own)
        if ok:
            say(f"    用机器上已有的浏览器：{own}")
            remember(folder, own, [])
            return Prepared(python, own)
        say(f"    已有的浏览器 {own} 起不来（{why[:120]}），改用 Playwright 的 Chromium")
    if not chromium_installed(folder):
        say("    下载 Chromium（第一次约 200 MB，装在数据根下；觉得慢可以 Ctrl-C 后加 --no-browser 改用手机扫码）")
        download_chromium(python, env, say, folder, env.get("TMPDIR") or str(Path(data) / "tmp"), live=live, runner=runner, **options)
    ok, why = browser_starts(python, env)
    libs = []
    if not ok and platform.system() == "Linux":
        libs = ensure_libs(python, env, say, folder)
        ok, why = browser_starts(python, {**env, "LD_LIBRARY_PATH": os.pathsep.join([*libs, env.get("LD_LIBRARY_PATH", "")])})
    if not ok:
        raise Unavailable("Chromium 起不来：" + why[-200:])
    remember(folder, "", libs)
    return Prepared(python, "", libs)
