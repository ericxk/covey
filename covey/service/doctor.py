#!/usr/bin/env python3
"""Read-only layered diagnostics. No messages or state repair; model requests only with --probe-harnesses."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from covey.core.process import descendant, is_owned, query, service_key
from covey.core.state import read_json
from covey.service.boot import boot_health, init_identity, manager_health
from covey.feishu.connections import EventStatusUnavailable, event_status_apps
from covey.core.paths import service_root
from covey.core import claude_retention
from covey.core.ignored_env import loosened
from covey.core.switches import shown as switches_set


def hosting_report(receipt, run=subprocess.run, root=None):
    """What the core's own hosting modes (service/install.py, service/launchd.py) look like now; None for any
    other receipt (the PID-1 hook's has a ``config``)."""
    mode = receipt.get('mode')
    if mode == 'manager':
        # Chosen on purpose where nothing can host it: the Manager runs, a reboot stops it.
        return {'status': 'ready', 'mode': mode, 'autostart': False, 'reason': receipt.get('note')}
    if mode == 'cron':
        from covey.service.install import cron_status
        try:
            ok = bool(cron_status(run, root=root).get('registered'))
        except (OSError, subprocess.SubprocessError):
            ok = False
        report = {'status': 'ready' if ok else 'degraded', 'mode': mode, 'registered': ok, 'autostart': 'cron @reboot',
                  'reboot_verified': receipt.get('reboot_verified', False)}
        if not ok:
            report['reason'] = 'the crontab @reboot line is gone; run ./install again (docs/operations.md 服务托管)'
        return report
    if mode == 'launchd':
        from covey.service import launchd
        now = launchd.status(receipt.get('label') or launchd.DEFAULT_LABEL, run=run)
        ok = bool(now.get('registered') and now.get('loaded'))
    elif mode == 'systemd-user':
        from covey.service.install import systemd_status
        now = systemd_status(run)
        ok = bool(now.get('registered') and now.get('enabled') and now.get('active'))
    else:
        return None
    report = {'status': 'ready' if ok else 'degraded', 'mode': mode, 'registered': bool(now.get('registered')),
              'reboot_verified': receipt.get('reboot_verified', False), **{
                  k: v for k, v in now.items() if k in ('loaded', 'running', 'enabled', 'active')}}
    if receipt.get('hint'):
        report['hint'] = receipt['hint']
    if not ok:
        report['reason'] = f'{mode} registration is not loaded or not enabled; install it again (docs/operations.md 服务托管)'
    return report


def boot_report(root):
    receipt = read_json(root / 'boot-installation.json', {})
    path = receipt.get('config')
    if not path:
        hosted = hosting_report(receipt, root=root)
        return hosted or {'status': 'unverified', 'mode': receipt.get('mode', 'not_installed')}
    config = read_json(Path(path), {})
    health = boot_health(path)
    fresh = bool(health and health.get('lag', 999) < config.get('watchdog_seconds', 120))
    active = fresh and health.get('mode') == 'pid1' and health.get('init_identity') == init_identity()
    # A host whose PID 1 loads no hook (the extension's rh2 case) starts the standalone watchdog from a login file
    # the receipt names; a running one is then the hosting itself, not a stopgap until the next restart.
    login = receipt.get('login_hook')
    try:
        by_login = bool(login) and str(path) in Path(login).read_text(errors='replace')
    except OSError:
        by_login = False
    hosted = active or (fresh and by_login and health.get('mode') == 'standalone')
    return {'status': 'ready' if hosted else 'degraded',
            'registered': config.get('enabled') is True and Path(receipt.get('hook', '')).is_file(),
            'active_pid1_hook': active, 'login_hook': login if by_login else None, 'watchdog': health,
            'new_init_since_install': bool(config.get('installed_init_identity')
                                          and config['installed_init_identity'] != init_identity()),
            'reboot_verified': receipt.get('reboot_verified', False),
            'reason': None if hosted else 'PID-1 hook takes effect at the next platform restart; standalone watchdog is temporary'}


def event_report(profile):
    try:
        apps = event_status_apps(profile)
    except EventStatusUnavailable as exc:
        return {'status': 'unavailable', 'exit_code': exc.returncode}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'status': 'unavailable'}
    # Explicit fields only: never echo CLI output, credentials or identities.
    consumers = [c for app in apps for c in app.get('consumers', [])]
    return {'status': 'ready' if len(apps) == 1 and apps[0].get('running') else 'degraded',
            'scope': 'local_event_bus', 'consumers': [
                {k: c.get(k) for k in ('pid', 'event_key', 'received', 'dropped')} for c in consumers],
            'websocket_heartbeat': 'not_exposed_by_cli'}


def probe_report(names=('claude', 'codex'), *, run=False):
    """What the last live probe of each Harness said (core/harness_probe.py), read from its cache; ``run`` makes
    the call now (a real, short model request per Harness that is installed) and refreshes the cache."""
    from covey.core import harness_probe
    probe_directory = harness_probe.probe_directory
    rows = {}
    for name in names:
        if run:
            result = harness_probe.cached_probe(probe_directory(), name, refresh=True)
        else:
            result = harness_probe.read_cache(probe_directory(), name)
        if result:
            rows[name] = {k: result.get(k) for k in ('ok', 'reason', 'detail', 'checked_at')}
            if not result.get('ok'):
                rows[name]['advice'] = harness_probe.advice(result.get('reason'))
    return rows


def devices_row(state_dir):
    """Each device's last self-check (``covey_devices.py check``), read from the service's state directory; asks no
    device. None when devices are off or none is listed, so the report is the same as before then."""
    from covey.service.device_check import devices_report
    report = devices_report(Path(state_dir))
    if not report:
        return None
    if 'error' in report:
        return {'status': 'degraded', 'reason': report['error']}
    rows = {alias: {'title': row['title'], 'status': row['status'], 'check': row['check']} for alias, row in report.items()}
    return {'status': 'ready' if all((row['check'] or {}).get('ok') for row in rows.values()) else 'unverified',
            'devices': rows, 'advice': 'a device that is not passed: covey_devices.py --state-dir <state> check <alias>'}


def claude_enabled(manifest):
    """Whether the Claude Harness is enabled for the service ``manifest``: its harnesses.json (the ``--harness-config`` of its
    command, else the data root's) has no ``enabled_harnesses`` or lists it; a file that cannot be read counts as enabled."""
    command = [str(part) for part in manifest.get('command') or ()]
    if '--harness-config' in command[:-1]:
        path = Path(command[command.index('--harness-config') + 1])
    else:
        from covey.core.paths import data_home
        path = data_home() / 'harnesses.json'
    config = read_json(path, {})
    names = config.get('enabled_harnesses') if isinstance(config, dict) else None
    return not isinstance(names, list) or 'claude' in names


def retention_row(manifest):
    """How long Claude Code keeps session transcripts for the service ``manifest`` (core/claude_retention.py: its settings,
    as the service's CLAUDE_CONFIG_DIR says), None without the Claude Harness. Read only: the setting is the user's."""
    try:
        if not claude_enabled(manifest):
            return None
    except (OSError, ValueError, TypeError):
        return None
    found = claude_retention.check({**os.environ, **(manifest.get('environment') or {})})
    row = {key: found[key] for key in ('state', 'days', 'path')}
    if claude_retention.needs_hint(found):
        row['hint'] = claude_retention.hint(found)
    return row


def diagnose(root, *, check_events=True, probe=False):
    manager = manager_health(str(root))
    manager_ok = bool(manager and is_owned(manager.get('process')) and manager.get('lag', 999) < 120)
    services = []
    for path in sorted((root / 'services').glob('*.json')):
        manifest = read_json(path, None)
        row = {'id': manifest['id'], 'desired_state': manifest['desired_state']}
        # A permission variable the backend no longer reads, kept in the manifest: the service runs looser
        # than it was registered to (core/ignored_env.py). A running service fails until it is registered again
        # without it; a stopped one only once started: until then it fails the full report, not the runtime's
        # (--runtime-only, which scripts/deploy_bridge.sh checks after a deploy, as its pre-check does).
        loose = loosened(manifest.get('environment') or {})
        if loose:
            row['permission_env'] = loose
        # The optional switches this service has set (core/switches.py: known names only, no credential-like name or value).
        row['switches'] = [list(pair) for pair in switches_set(manifest.get('environment') or {})]
        if (retention := retention_row(manifest)) is not None:
            row['claude_cleanup'] = retention  # Said on the 诊断 tab; never makes the service degraded.
        if manifest['desired_state'] != 'running':
            row['status'] = 'stopped'
            services.append(row)
            continue
        status = read_json(root / 'status' / path.name, {})
        identity = status.get('process')
        health = query('state-' + service_key(Path(manifest['state_dir']))) if is_owned(identity) else None
        ready = bool(health and health.get('process') == identity and health.get('status') == 'ready'
                     and status.get('generation') == manifest['generation'])
        row.update(status='ready' if ready else 'degraded', alive=is_owned(identity), bridge=health,
                   generation=manifest['generation'])
        # Devices (COVEY_DEVICES=1) are reported but never make the service degraded: a device that is off is its
        # own topics' trouble.
        if (found := devices_row(manifest['state_dir'])) is not None:
            row['devices'] = found
        if check_events and (health or {}).get('event_transport') == 'sdk':
            # COVEY_EVENT_TRANSPORT=sdk: there is no lark-cli bus; the Bridge's own consumers say whether they are up.
            consumers = (health or {}).get('consumers') or {}
            connected = all((consumers.get(key) or {}).get('ready') for key in ('im.message.receive_v1', 'card.action.trigger'))
            row['events'] = {'status': 'ready' if connected else 'degraded', 'scope': 'sdk_websocket',
                             'owned_required_consumers': connected, 'dropped': 0}
            if not connected:
                row['status'] = 'degraded'
        elif check_events:
            events = event_report(manifest['profile'])
            owned = [c for c in events.get('consumers', [])
                     if identity and descendant(c.get('pid'), identity['pid'])]
            keys = {c['event_key'] for c in owned}
            connected = events['status'] == 'ready' and {'im.message.receive_v1', 'card.action.trigger'} <= keys
            events['owned_required_consumers'] = connected
            events['dropped'] = sum(c.get('dropped') or 0 for c in owned)
            row['events'] = events
            if not connected or events['dropped']:
                row['status'] = 'degraded'
        if loose:
            row['status'] = 'degraded'
        services.append(row)
    runtime_ok = manager_ok and bool(services) and all(s['status'] in {'ready', 'stopped'} for s in services)
    stopped_loose = any(s['status'] == 'stopped' and s.get('permission_env') for s in services)
    boot = boot_report(root)
    return {'schema_version': 1,
            'status': 'ready' if runtime_ok and not stopped_loose and boot['status'] == 'ready' else 'degraded',
            'runtime_status': 'ready' if runtime_ok else 'degraded',
            'boot': boot, 'manager': manager, 'services': services,
            'model_access': 'probed' if probe else 'not_probed', 'harness_probe': probe_report(run=probe),
            'events_checked': check_events}


def main():
    parser = argparse.ArgumentParser(prog="covey_doctor.py", description=__doc__)
    parser.add_argument('--root', type=Path, default=service_root())
    parser.add_argument('--skip-events', action='store_true', help='local process diagnostics only')
    parser.add_argument('--probe-harnesses', action='store_true',
                        help='also make one short real call per Harness (claude -p, codex exec) and report why one fails')
    parser.add_argument('--runtime-only', action='store_true', help='exit code ignores boot readiness; report still includes it')
    args = parser.parse_args()
    try:
        report = diagnose(args.root.expanduser().resolve(), check_events=not args.skip_events, probe=args.probe_harnesses)
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'unavailable', 'reason': 'invalid or unreadable service configuration'}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for service in report['services']:
        for line in service.get('permission_env', ()):
            print(f"service {service['id']}: {line}, then register it again (docs/troubleshooting.md)", file=sys.stderr)
    return 0 if report['runtime_status' if args.runtime_only else 'status'] == 'ready' else 2


if __name__ == '__main__':
    sys.exit(main())
