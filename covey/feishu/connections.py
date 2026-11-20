"""Linux event-bus transport watchdog, independent of user message activity.

CLI does not expose WebSocket pongs. Inspect TCP data-receive age for the
verified bus PID instead. This detects silent transport stalls, not application
level acknowledgement. Never replace the bridge or interrupt model processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from covey.core.process import descendant, is_owned, process_identity
from covey.core.logs import rotate_live
from covey.feishu.events import SUBSCRIPTIONS
from covey.feishu.transport import lark_argv, run_cli


class BusUnresponsive(RuntimeError):
    def __init__(self, identity=None):
        self.identity = identity


class EventStatusUnavailable(ValueError):
    def __init__(self, returncode):
        super().__init__('event status unavailable')
        self.returncode = returncode


def event_status_apps(profile):
    """This profile's apps from `event status --current`. A failed or malformed reply is a ValueError;
    a timeout is subprocess.TimeoutExpired."""
    proc = run_cli(lark_argv(profile, 'event', 'status', '--current', '--json'), runner=subprocess.run,
                   capture_output=True, text=True, timeout=8, check=False)
    if proc.returncode:
        raise EventStatusUnavailable(proc.returncode)
    payload = json.loads(proc.stdout)
    apps = payload.get('apps', []) if isinstance(payload, dict) else None
    if not isinstance(apps, list) or not all(
            isinstance(app, dict) and isinstance(app.get('consumers', []), list)
            and all(isinstance(c, dict) for c in app.get('consumers', [])) for app in apps):
        raise ValueError('event status malformed')
    return apps


def tcp_receive_age(output, pid):
    ages = []
    lines = output.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.startswith('ESTAB') and re.search(r'\bpid=' + str(pid) + r',', line):
            match = re.search(r'\blastrcv:(\d+)', lines[index + 1])
            if match:
                ages.append(int(match[1]) / 1000)
    return min(ages) if ages else None


def bus_sample(profile, owner_pid):
    try:
        apps = event_status_apps(profile)
    except subprocess.TimeoutExpired:
        raise BusUnresponsive() from None
    if len(apps) == 1 and apps[0].get('status') == 'orphan':
        identity = process_identity(apps[0].get('pid', 0))
        if identity:
            # CLI 1.0.98 returns exit 0/status=orphan after its own 5s IPC
            # timeout, even though the daemon PID still exists.
            raise BusUnresponsive(identity)
    if len(apps) != 1 or not apps[0].get('running'):
        return {'status': 'disconnected', 'exclusive': False}
    app = apps[0]
    identity = process_identity(app['pid'])
    if not identity:
        return {'status': 'disconnected', 'exclusive': False}
    consumers = app.get('consumers', [])
    allowed = set(SUBSCRIPTIONS)
    exclusive = bool(consumers) and all(c.get('event_key') in allowed and descendant(c.get('pid'), owner_pid)
                                      for c in consumers)
    proc = subprocess.run(['ss', '-Htinp'], capture_output=True, text=True, timeout=8, check=False)
    if proc.returncode:
        raise ValueError('TCP diagnostics unavailable')
    age = tcp_receive_age(proc.stdout, identity['pid'])
    # App ID comes from this explicit profile's local status response.
    app_id = app.get('app_id', '')
    if re.fullmatch(r'cli_[a-zA-Z0-9]+', app_id):
        rotate_live(Path.home() / '.lark-cli' / 'events' / app_id / 'bus.log')
    return {'status': 'receiving' if age is not None and age < 600 else 'stalled',
            'tcp_receive_age_seconds': age, 'process': identity, 'exclusive': exclusive,
            'scope': 'TCP receive activity; WebSocket pong not exposed by CLI'}


def stop_bus(identity):
    if not is_owned(identity):
        return
    os.kill(identity['pid'], signal.SIGTERM)
    until = time.monotonic() + 5
    while is_owned(identity) and time.monotonic() < until:
        time.sleep(.1)
    if is_owned(identity):
        # This is the verified shared bus daemon, never an individual consumer.
        os.kill(identity['pid'], signal.SIGKILL)


def watch(runtime, ctx):
    from covey.feishu import ws_intake
    if ws_intake.enabled():  # COVEY_EVENT_TRANSPORT=sdk: no lark-cli bus to look at; the SDK client's own state.
        return ws_intake.watch_status(runtime)
    failures = 0
    identity = None
    last_restart = float('-inf')  # not 0: time.monotonic() is the host's uptime, small just after a boot
    last_verified = None
    while not runtime.shutdown.is_set():
        try:
            sample = bus_sample(ctx.profile, os.getpid())
            last_verified = sample
            if sample.get('process') != identity:
                failures = 0
                identity = sample.get('process')
            failures = failures + 1 if sample['status'] in {'stalled', 'disconnected'} else 0
            if (failures >= 3 and sample.get('exclusive') and identity
                    and time.monotonic() - last_restart > 600):
                # Recheck ownership immediately before signalling a single PID.
                fresh = bus_sample(ctx.profile, os.getpid())
                if (fresh.get('process') == identity and fresh.get('exclusive')
                        and fresh['status'] == 'stalled' and is_owned(identity)):
                    stop_bus(identity)
                    last_restart = time.monotonic()
                    sample['recovery'] = 'bus_sigterm; consumers reconnect; model tasks untouched'
                    runtime.log('event bus TCP transport stalled; requested graceful reconnect')
            sample['consecutive_failures'] = failures
        except BusUnresponsive as exc:
            if exc.identity is not None and exc.identity != identity:
                last_verified, identity, failures = None, exc.identity, 0
            failures += 1
            sample = {'status': 'unresponsive', 'scope': 'local event bus IPC timed out',
                      'consecutive_failures': failures}
            # A previously verified bus may stop answering IPC altogether.
            # Without a prior exclusive ownership observation we only report it.
            if (failures >= 3 and last_verified and last_verified.get('exclusive')
                    and identity and is_owned(identity) and time.monotonic() - last_restart > 600):
                stop_bus(identity)
                last_restart = time.monotonic()
                sample['recovery'] = 'verified_bus_replaced'
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            sample = {'status': 'unknown', 'scope': 'TCP diagnostics unavailable; no restart inferred'}
            failures = 0
        runtime.connection_health = {**sample, 'checked_at': time.time()}
        runtime.shutdown.wait(30)
