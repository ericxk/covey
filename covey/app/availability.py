"""Separate local backend readiness from recently observed model access."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

from covey.core.loops import every
from covey.core.state import read_json, write_json
from covey.core.roster import HARNESSES
from covey.core.process import device_of
from covey.core.process_env import child_process_env, env_flag, process_device
from covey.core import harness_probe
from covey.core import gateways
from covey.core.result import ERROR_CODES

# `claude auth status` takes 1.0-2.0 s before every Claude task (S33). A passing one is reused this long:
# a logout then shows when a task runs, not before it starts. The
# availability watch checks every minute and refreshes or clears it.
LOGIN_REUSE = 300
# COVEY_HARNESS_PROBE=1: besides the local preflight, the availability watch runs one real call per Claude
# and Codex (core/harness_probe.py: claude -p, codex exec) when its cached answer expires (6 h when it passed,
# 10 min when not), keeps the answer under <data>/harness-probe/ and shows it in the health snapshot. A cached
# "not logged in" makes the Harness unavailable; a network, timeout or other failure only shows in health, since
# it may pass again before a task runs. Off by default: a live deployment makes no extra model calls.
PROBE_ENV = 'COVEY_HARNESS_PROBE'
PROBED = ('claude', 'codex')
_logins: dict = {}  # executable -> monotonic time of its last passing login check


def _executable(ctx):
    command = ctx.backend_cmd
    return command[0] if isinstance(command, (list, tuple)) else command


def probe_enabled():
    return env_flag(PROBE_ENV, False)


def probe_row(result):
    return {k: result[k] for k in ('ok', 'reason', 'detail', 'checked_at', 'cached') if k in result}


def device_check(name, ctx):
    """A Harness's readiness on the device ``ctx`` works on, from what the device last said (its cached ``hello`` and the
    environment profile of its last self-check, core/devices.py): never a command looked for on this machine. No
    ``hello`` younger than ``HELLO_TTL`` (nothing has talked to the device lately) is 'unknown', which does not stop a
    task: the turn's own connection (and the 5-second check before it) says if the device is gone."""
    from covey.core import devices
    alias = device_of(ctx)
    hello = devices.read_hello(ctx.state_dir, alias)
    if hello is None:
        return {'status': 'unknown', 'reason': 'device_not_checked', 'scope': 'device_preflight'}
    tool = (hello.get('tools') or {}).get(name)
    if not isinstance(tool, dict) or not tool.get('found'):
        return {'status': 'unavailable', 'reason': 'executable_missing', 'scope': 'device_preflight'}
    if tool.get('login') == 'no':
        return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'device_preflight'}
    # The last minimal request (self-check) said it is not signed in: the same answer as a cached local probe.
    probe = (devices.read_profile(ctx.state_dir, alias).get('probe') or {}).get(name) or {}
    if probe.get('reason') == 'not_logged_in':
        return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'device_probe'}
    return {'status': 'configured', 'scope': 'device_preflight; model access not probed'}


def local_check(name, ctx):
    if device_of(ctx):
        return device_check(name, ctx)
    executable = _executable(ctx)
    if not executable or not shutil.which(executable):
        return {'status': 'unavailable', 'reason': 'executable_missing', 'scope': 'local_preflight'}
    if name == 'claude':
        # The last pass stays reusable while this runs: cleared up front, a task starting during the
        # watch's check ran its own (2 s).
        try:
            proc = subprocess.run([executable, 'auth', 'status', '--json'], cwd=ctx.workdir,
                                  text=True, capture_output=True, timeout=8, check=False)
            data = json.loads(proc.stdout)
            if data.get('loggedIn') is False:
                _logins.pop(executable, None)
                return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'local_preflight'}
        except (OSError, ValueError, subprocess.SubprocessError):
            _logins.pop(executable, None)
            return {'status': 'unknown', 'reason': 'auth_check_unavailable', 'scope': 'local_preflight'}
        _logins[executable] = time.monotonic()
    if name in PROBED and probe_enabled():
        # Only the cached answer: a task's preflight never waits for a model call (the watch fills the cache).
        known = harness_probe.read_cache(harness_probe.probe_directory(), name, command=_command(ctx),
                                         env=child_process_env(ctx.workdir, device=process_device(ctx)))
        if known and known.get('reason') == 'not_logged_in':
            return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'live_probe'}
    gateway = gateways.for_model(name, ctx.model) if name == 'pi' else gateways.dsh_gateway() if name == 'dsh' else None
    # Only for a Pi or dsh check: their modules (urllib and the rest) stay out of a task runner's start.
    if name == 'pi' and gateway is None:
        from covey.backends.pi import home as pi_home
        try:
            # Only that Codex has a login: an expiring token fails the turn with its own words (backends/pi/home.py).
            pi_home.access_token(margin=None)
        except ValueError:
            return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'local_preflight'}
    elif name in ('dsh', 'pi'):
        from covey.backends.gateway.connection import api_key
        try:
            if gateway is None or not api_key(gateway):
                return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'local_preflight'}
        except ValueError:
            return {'status': 'unavailable', 'reason': 'authentication', 'scope': 'local_preflight'}
    return {'status': 'configured', 'scope': 'local_preflight; model access not probed'}


def _command(ctx):
    command = ctx.backend_cmd
    return list(command) if isinstance(command, (list, tuple)) else command


def observe(ctx, name, model, *, status, error=''):
    # Only canonical backend outcome codes, never exception text or user content.
    if error not in ERROR_CODES:
        error = 'process_exit'
    key = name + '-' + hashlib.sha256(model.encode()).hexdigest()[:16]
    write_json(ctx.state_dir / 'backend-health' / (key + '.json'), {
        'backend': name, 'model': model, 'checked_at': time.time(), 'status': status,
        'error': error, 'scope': 'last_task_observation; not an active availability probe'})


def snapshot(state):
    values = []
    for path in (Path(state) / 'backend-health').glob('*.json'):
        try:
            row = read_json(path)
            if row.get('backend') not in HARNESSES:
                continue  # Left by a Harness no longer in the roster.
            row['age_seconds'] = max(0, round(time.time() - row['checked_at']))
            row['stale'] = row['age_seconds'] > 900
            values.append(row)
        except (OSError, ValueError, KeyError):
            continue
    return values


def check_or_raise(name, ctx):
    executable = _executable(ctx)
    passed = _logins.get(executable) if name == 'claude' and not device_of(ctx) else None
    if passed is not None and time.monotonic() - passed < LOGIN_REUSE and shutil.which(executable):
        return {'status': 'configured', 'scope': 'local_preflight; login check reused'}
    check = local_check(name, ctx)
    if check['status'] == 'unavailable':
        raise ValueError('模型后端尚未登录' if check['reason'] == 'authentication' else '模型后端程序不可用')
    return check


def watch(gateway, ctx):
    every(gateway.runtime, 60, sample, args=(gateway, ctx))


def sample(gateway, ctx):
    """Each Harness's local preflight and the model-access observations, for the health endpoint."""
    contexts = {name: harness.context for name, harness in gateway.harnesses.items()}
    checks = {}
    for name, target in contexts.items():
        try:
            checks[name] = {**local_check(name, target), 'checked_at': time.time()}
            check = checks[name]
            if name in PROBED and probe_enabled() and (check['status'] != 'unavailable' or check.get('scope') == 'live_probe'):
                check['probe'] = probe_row(harness_probe.cached_probe(
                    harness_probe.probe_directory(), name, command=_command(target),
                    env=child_process_env(target.workdir, device=process_device(target))))
        except (OSError, ValueError, TypeError, AttributeError):
            checks[name] = {'status': 'unknown', 'scope': 'local_preflight'}
    gateway.runtime.availability_health = {'preflight': checks, 'observations': snapshot(ctx.state_dir)}
