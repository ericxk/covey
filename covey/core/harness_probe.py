"""Harness 能不能真的调通：跑一次最便宜的真实调用（claude -p、codex exec），把失败分成几类。

只查二进制在不在、``auth status`` 通过，不等于模型能答：没登录、出口需要代理、直连 403，都是装完才发现。
安装器、doctor 和（开了 ``COVEY_HARNESS_PROBE=1`` 的）Bridge 共用这里。不导入 app/、feishu/、cards/，
只用标准库和 core/state。

``probe(name, ...)`` 返回 ``{'ok': bool, 'reason': 'ok'|'not_installed'|'not_logged_in'|'network'|'timeout'|'other',
'detail': str}``；``detail`` 是截短、抹掉密钥样子字符串的一两行，可以直接给用户看。没有探测方式的 Harness
（dsh、pi、扩展带来的）只查可执行文件：在就 ``ok``（``probed`` 为 False），不在 ``not_installed``。
``cached_probe`` 把结果存在 ``<目录>/<名字>.json``（带时间），成功和失败各有存活期，换了命令或代理设置就作废。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from covey.core.paths import data_home
from covey.core.state import read_json, write_json

REASONS = ('ok', 'not_installed', 'not_logged_in', 'network', 'timeout', 'other')
PROMPT = 'Reply with the single word: ok'
DEFAULT_TIMEOUT = 60
# 通过的结果用久一点（探测要花钱），失败的短一点（用户登录或设了代理后很快要看到）。
OK_TTL = 6 * 3600
FAIL_TTL = 600

# 先看没登录再看网络：没登录的报错里常带 "authentication" 一类词，网络错误里也可能有 "login"。
_NOT_LOGGED_IN = re.compile(
    r'not logged in|please run /login|run `?(?:claude|codex) (?:auth )?login|invalid api key|'
    r'authentication[_ ]error|authentication_failed|no (?:auth|credentials)|missing (?:bearer|credentials)|'
    r'unauthorized|\b401\b|logged out|sign in again|re-?authenticate|please log ?in', re.I)
_NETWORK = re.compile(
    r'econn(?:refused|reset|aborted)|etimedout|enotfound|eai_again|getaddrinfo|could not resolve|'
    r'name or service not known|network|connection (?:refused|reset|closed|error|timed out)|'
    r'unable to connect|failed to connect|proxy|tls|ssl|certificate|handshake|socket|offline|'
    r'error sending request|stream disconnected|\b(?:403|407|502|503|504)\b|forbidden|unexpected status', re.I)
_SECRET = re.compile(r'(?:sk|ak|ghp|xox[a-z])[-_][A-Za-z0-9_\-]{12,}|(?i:bearer)\s+[A-Za-z0-9._\-]{12,}'
                     r'|(?i:(?:token|key|secret|password)=)\S+')
_PROXY_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
               'no_proxy')


def probe_directory():
    """数据根下放探测缓存的目录（登录和出口是整台机器的事，不分 Bridge 实例）。"""
    return data_home() / 'harness-probe'


def _argv(name, executable):
    if name == 'claude':
        return [executable, '-p', PROMPT, '--output-format', 'text', '--no-session-persistence']
    if name == 'codex':
        return [executable, 'exec', '--skip-git-repo-check', '--ephemeral', '-s', 'read-only', PROMPT]
    return None


def command_for(name, executable):
    """The argv of ``name``'s minimal request with ``executable`` (also what a device runs: core/devices.py
    ``probe_harness``), or None for a Harness without one."""
    return _argv(name, executable)


def _result(ok, reason, detail=''):
    return {'ok': ok, 'reason': reason, 'detail': _clean(detail)}


def _clean(text, limit=240):
    """最后几行有内容的输出，抹掉像密钥的串，截到 limit 个字符。"""
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    text = ' | '.join(lines[-3:])
    text = _SECRET.sub('***', text)
    return text if len(text) <= limit else text[:limit - 1] + '…'


def classify(output, returncode=0):
    """从退出码和输出判断类别。退出码 0 且有输出算通过。"""
    if returncode == 0 and str(output or '').strip():
        return _result(True, 'ok')
    if _NOT_LOGGED_IN.search(output or ''):
        return _result(False, 'not_logged_in', output)
    if _NETWORK.search(output or ''):
        return _result(False, 'network', output)
    return _result(False, 'other', output or f'exit {returncode}')


def probe(name, *, command=None, env=None, timeout=DEFAULT_TIMEOUT, cwd=None):
    """跑一次探测。``command`` 覆盖可执行文件（默认 ``name``，可带路径）；``env`` 是子进程的完整环境
    （默认当前环境，传代理时在拷贝上加）；``timeout`` 秒后算 ``timeout``。"""
    executable = command[0] if isinstance(command, (list, tuple)) else (command or name)
    resolved = shutil.which(executable, path=(env or os.environ).get('PATH'))
    if not resolved:
        return _result(False, 'not_installed', f'{executable} not found on PATH')
    argv = _argv(name, resolved)
    if argv is None:
        return {**_result(True, 'ok', 'executable found; no live probe for this Harness'), 'probed': False}
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return _result(False, 'timeout', f'no answer within {timeout} s')
    except OSError as exc:
        return _result(False, 'other', f'{type(exc).__name__}: {exc}')
    return {**classify((proc.stderr or '') + '\n' + (proc.stdout or ''), proc.returncode), 'probed': True}


def _fingerprint(name, command, env):
    source = os.environ if env is None else env
    parts = [name, json.dumps(command), *(f'{k}={source[k]}' for k in _PROXY_KEYS if source.get(k))]
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:16]


def read_cache(directory, name, *, command=None, env=None, now=None):
    """缓存里还有效的结果（带 ``checked_at``、``cached``），没有或过期或换了命令/代理返回 None。"""
    row = read_json(Path(directory) / f'{name}.json', None)
    now = time.time() if now is None else now
    if not isinstance(row, dict) or row.get('fingerprint') != _fingerprint(name, command, env):
        return None
    ttl = OK_TTL if row.get('ok') else FAIL_TTL
    if not isinstance(row.get('checked_at'), (int, float)) or not 0 <= now - row['checked_at'] < ttl:
        return None
    return {**row, 'cached': True}


def cached_probe(directory, name, *, command=None, env=None, timeout=DEFAULT_TIMEOUT, cwd=None, refresh=False):
    """``probe`` 加缓存：有效缓存直接返回，否则探测一次并写回（写不了不报错）。"""
    if not refresh:
        hit = read_cache(directory, name, command=command, env=env)
        if hit is not None:
            return hit
    result = probe(name, command=command, env=env, timeout=timeout, cwd=cwd)
    row = {**result, 'checked_at': time.time(), 'fingerprint': _fingerprint(name, command, env)}
    try:
        write_json(Path(directory) / f'{name}.json', row)
    except OSError:
        pass
    return {**row, 'cached': False}


# 给卡片和提示用的一句话；能判断是网络问题时说清楚。
ADVICE = {
    'not_installed': '没有找到程序，请先安装',
    'not_logged_in': '尚未登录，请先在这台机器上登录',
    'network': '连不上模型服务（网络或代理问题）：检查出口，必要时设置 HTTPS_PROXY 后重新注册服务',
    'timeout': '模型服务没有及时回应，可能是网络或代理问题',
    'other': '调用失败',
}


def advice(reason):
    return ADVICE.get(reason, '')
