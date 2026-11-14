"""macOS 的 launchd 托管：``~/Library/LaunchAgents/<label>.plist`` 让 Manager 登录后自动跑、挂了自动拉起。

只托管 Manager（和 systemd 单元、公司的 PID-1 钩子一样）：Bridge 的环境变量在服务清单里，Manager 启动它时带上，
所以 plist 里只放 Manager 自己要的几项（PATH、数据根、服务根）。日志写到服务根下的 ``launchd.log``。
launchctl 的调用都经 ``run``（默认 subprocess.run），测试传假的。
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from covey.core.paths import entry_script

DEFAULT_LABEL = 'com.covey.manager'
# 传给 Manager 进程的环境：它自己要找数据根和服务根；PATH 取安装时的，launchd 给的 PATH 只有 /usr/bin:/bin。
PASS_ENV = ('PATH', 'HOME', 'LANG', 'COVEY_DATA_HOME', 'COVEY_SERVICE_ROOT', 'COVEY_EXTENSIONS',
            'LARK_BRIDGE_DATA_HOME', 'LARK_BRIDGE_SERVICE_ROOT', 'LARK_BRIDGE_EXTENSIONS')


def plist_path(label, home=None):
    return Path(home or Path.home()) / 'Library/LaunchAgents' / f'{label}.plist'


def domain(uid=None):
    return f'gui/{os.getuid() if uid is None else uid}'


def plist_data(root, label=DEFAULT_LABEL, *, python=None, environ=None):
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if k in PASS_ENV and v}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    log = str(Path(root) / 'launchd.log')
    return {
        'Label': label,
        'ProgramArguments': [python or sys.executable, '-B', str(entry_script('covey_service.py').resolve()),
                             '--root', str(root), 'run'],
        'EnvironmentVariables': env,
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'ProcessType': 'Background',
        # 停掉 Manager 不连带结束它的子进程：下一个 Manager 接管还在跑的 Bridge，任务进程自己跑完。
        'AbandonProcessGroup': True,
        'Umask': 0o077,
        'StandardOutPath': log,
        'StandardErrorPath': log,
    }


def _launchctl(run, *args, check=False):
    return run(['launchctl', *args], capture_output=True, text=True, timeout=30, check=check)


def install(root, label=DEFAULT_LABEL, *, activate=False, run=subprocess.run, home=None, python=None, environ=None):
    """写 plist；``activate`` 时先 bootout 旧的（没有也行）再 bootstrap，所以重复运行安全。"""
    root = Path(root)
    path = plist_path(label, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    data = plistlib.dumps(plist_data(root, label, python=python, environ=environ))
    path.write_bytes(data)
    path.chmod(0o644)  # launchd 要求 plist 不能被组或其他人写
    started = False
    if activate:
        _launchctl(run, 'bootout', f'{domain()}/{label}')
        _launchctl(run, 'bootstrap', domain(), str(path), check=True)
        _launchctl(run, 'enable', f'{domain()}/{label}')
        started = True
    return {'mode': 'launchd', 'label': label, 'plist': str(path), 'log': str(root / 'launchd.log'),
            'started': started, 'root': str(root), 'reboot_verified': False,
            'autostart': 'at login (launchd user agent; the Mac must be logged in)'}


def uninstall(label=DEFAULT_LABEL, *, run=subprocess.run, home=None):
    path = plist_path(label, home)
    _launchctl(run, 'bootout', f'{domain()}/{label}')
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {'mode': 'launchd', 'label': label, 'plist': str(path), 'removed': existed}


def status(label=DEFAULT_LABEL, *, run=subprocess.run, home=None):
    path = plist_path(label, home)
    row = {'mode': 'launchd', 'label': label, 'plist': str(path), 'registered': path.is_file(), 'loaded': False}
    if not row['registered']:
        return row
    try:
        proc = _launchctl(run, 'print', f'{domain()}/{label}')
    except (OSError, subprocess.SubprocessError):
        return row
    row['loaded'] = proc.returncode == 0
    row['running'] = proc.returncode == 0 and 'state = running' in (proc.stdout or '')
    return row
