#!/usr/bin/env python3
"""Install an explicit boot registration without changing platform init files: a systemd user unit or a launchd
user agent (service/launchd.py) here; other ways to host the Manager (the company PID-1 hook) come from the
extension hook ``service_hosting`` (core/extensions.py: ``MODES`` name -> ``install(root, args)``,
``add_arguments(parser)``, optional ``auto_mode()``). ``--mode auto`` picks one: the hook's ``auto_mode()`` when
it names a mode, else launchd on macOS, else a systemd user service when the user manager answers, else the
Manager alone (started by ``covey_service.py launch``, not at boot). ``--status`` and ``--uninstall`` act on
what ``--mode`` names. Where neither is there but ``crontab`` is (a container, a plain VM), ``auto`` adds one ``@reboot``
line per service root to the user's crontab, marked by a comment with the root's short hash so a second run for the
same root replaces it, other roots keep theirs, and ``--uninstall`` removes only its own
(``--no-autostart`` leaves the Manager alone, as before)."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from covey.core.state import write_durable_json as save
from covey.core import extensions
from covey.core.paths import entry_script, service_root
from covey.service import launchd


def systemd_unit(root):
    command = [sys.executable, '-B', str(entry_script('covey_service.py').resolve()), '--root', str(root), 'run']
    def quote(s):
        return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    return ('[Unit]\nDescription=Covey Manager\nAfter=network-online.target\n'
            'Wants=network-online.target\nStartLimitIntervalSec=0\n\n[Service]\nType=exec\n'
            'ExecStart=' + ' '.join(map(quote, command)) + '\nRestart=always\nRestartSec=10\n'
            # Only the Manager is stopped with the unit: the next Manager adopts the live Bridge, and
            # task runners finish on their own, as under a PID 1 hook.
            'UMask=0077\nKillMode=process\nTimeoutStopSec=30\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\n\n[Install]\nWantedBy=default.target\n')




UNIT = 'covey-manager.service'
OLD_UNIT = 'lark-bridge-manager.service'  # What earlier releases installed; a machine that has it keeps it (no stop and swap).


def unit_name():
    """The unit this machine's Manager uses: the old name where that unit file already exists, else UNIT."""
    return OLD_UNIT if (Path.home() / '.config/systemd/user' / OLD_UNIT).is_file() else UNIT

MANAGER_NOTE = ('no boot hosting available here (no launchd, no user systemd): the Manager runs as a plain background '
                'process and does not start again after a reboot; run the install again after one')


def _run(run, argv, timeout=15):
    return run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def user_systemd_usable(run=subprocess.run):
    """Whether ``systemctl --user`` can reach a user manager here (a container may have the binary and no manager)."""
    if not shutil.which('systemctl'):
        return False
    try:
        return _run(run, ['systemctl', '--user', 'show-environment'], 5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def linger_hint(run=subprocess.run, user=None):
    """None when the user's services survive logout (or it cannot be told), else the command that enables it."""
    user = user or getpass.getuser()
    if not shutil.which('loginctl'):
        return None
    try:
        proc = _run(run, ['loginctl', 'show-user', user, '-p', 'Linger', '--value'], 5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout.strip() == 'no':
        return f'sudo loginctl enable-linger {user}'
    return None


def choose_mode(hosting, *, platform=None, systemd_usable=None, cron_usable=None, autostart=True):
    """(mode, why) for ``--mode auto``; the rules are in the module docstring."""
    platform = platform or sys.platform
    picked = getattr(hosting, 'auto_mode', lambda: None)()
    if picked and picked in getattr(hosting, 'MODES', {}):
        return picked, 'the extension hook applies here'
    if platform == 'darwin' and shutil.which('launchctl'):
        return 'launchd', 'macOS'
    if platform.startswith('linux') and (user_systemd_usable() if systemd_usable is None else systemd_usable):
        return 'systemd', 'a user systemd manager answers'
    if autostart and platform.startswith('linux') and (cron_available() if cron_usable is None else cron_usable):
        return 'cron', 'no systemd user manager; a crontab @reboot line starts the Manager'
    return 'manager', MANAGER_NOTE if autostart else 'autostart was declined (--no-autostart): ' + MANAGER_NOTE


CRON_MARK = '# covey-bridge-autostart'


def cron_available():
    return bool(shutil.which('crontab'))


def root_tag(root):
    """A short stable name of a service root: each root has its own crontab line, so a second Bridge on the machine
    does not replace the first one's."""
    return hashlib.sha256(str(Path(root).expanduser().resolve()).encode()).hexdigest()[:10]


def cron_line(root):
    command = [sys.executable, '-B', str(entry_script('covey_service.py').resolve()), '--root', str(root), 'run']
    log = Path(root) / 'cron-manager.log'
    return (f"@reboot cd / && PATH={shlex.quote(os.environ.get('PATH', ''))} {shlex.join(command)} "
            f">> {shlex.quote(str(log))} 2>&1 {CRON_MARK} {root_tag(root)}")


def _crontab(run, text=None):
    if text is None:
        done = _run(run, ['crontab', '-l'])
        return done.stdout if done.returncode == 0 else ''  # no crontab yet exits 1
    done = run(['crontab', '-'], input=text, capture_output=True, text=True, timeout=15, check=False)
    if done.returncode != 0:
        raise ValueError('crontab refused the new table: ' + ' '.join((done.stderr or '').split())[:200])
    return text


def _mine(line, root):
    """Whether a crontab line is the autostart of ``root`` (any root when None). A line from before the per-root tag
    ends with the bare mark; it counts for the root named in its command."""
    if CRON_MARK not in line:
        return False
    if root is None:
        return True
    tag = line.rsplit(CRON_MARK, 1)[1].strip()
    if tag:
        return tag == root_tag(root)
    return any(f"--root {shlex.quote(str(x))} run" in line for x in (root, Path(root).expanduser().resolve()))


def _without_mark(table, root=None):
    return ''.join(x for x in table.splitlines(True) if not _mine(x, root))


def cron_status(run=subprocess.run, root=None):
    return {'mode': 'cron', 'registered': any(_mine(x, root) for x in _crontab(run).splitlines())}


def install_cron(root, run=subprocess.run):
    line = cron_line(root)
    table = _without_mark(_crontab(run), root)
    _crontab(run, table + ('' if not table or table.endswith('\n') else '\n') + line + '\n')
    return {'mode': 'cron', 'autostart': True, 'line': line, 'reboot_verified': False}


def uninstall_cron(run=subprocess.run, root=None):
    table = _crontab(run)
    existed = any(_mine(x, root) for x in table.splitlines())
    if existed:
        _crontab(run, _without_mark(table, root))
    return {'mode': 'cron', 'removed': existed}


def systemd_status(run=subprocess.run):
    row = {'mode': 'systemd-user', 'unit': unit_name(), 'registered': (Path.home() / '.config/systemd/user' / unit_name()).is_file()}
    if not row['registered'] or not user_systemd_usable(run):
        return row
    row['enabled'] = _run(run, ['systemctl', '--user', 'is-enabled', unit_name()]).stdout.strip() == 'enabled'
    row['active'] = _run(run, ['systemctl', '--user', 'is-active', unit_name()]).stdout.strip() == 'active'
    return row


def install_systemd(root, activate, run=subprocess.run):
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('systemd is not running on this host')
    folder = Path.home() / '.config/systemd/user'
    folder.mkdir(parents=True, exist_ok=True)
    unit = folder / unit_name()
    unit.write_text(systemd_unit(root))
    run(['systemctl', '--user', 'daemon-reload'], check=True)
    if activate:
        run(['systemctl', '--user', 'enable', '--now', unit.name], check=True)
    result = {'mode': 'systemd-user', 'unit': str(unit), 'reboot_verified': False,
              'requires_linger_verification': True}
    hint = linger_hint(run)
    if hint:
        result['linger'] = False
        result['hint'] = 'user services stop at logout and do not start at boot until: ' + hint
    return result


def uninstall_systemd(run=subprocess.run):
    unit = Path.home() / '.config/systemd/user' / unit_name()
    if user_systemd_usable(run):
        _run(run, ['systemctl', '--user', 'disable', '--now', unit.name], 30)
    existed = unit.exists()
    unit.unlink(missing_ok=True)
    if user_systemd_usable(run):
        _run(run, ['systemctl', '--user', 'daemon-reload'])
    return {'mode': 'systemd-user', 'unit': str(unit), 'removed': existed}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_service_install.py", description=__doc__)
    # The same default as the Manager and doctor, so all three agree under COVEY_SERVICE_ROOT.
    parser.add_argument('--root', type=Path, default=service_root())
    hosting = extensions.hook('service_hosting')
    modes = getattr(hosting, 'MODES', {})
    parser.add_argument('--mode', choices=[*modes, 'systemd', 'launchd', 'cron', 'manager', 'auto'], required=True)
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--label', default=launchd.DEFAULT_LABEL, help='launchd label (launchd mode)')
    parser.add_argument('--no-autostart', action='store_true', help="with --mode auto: do not add a crontab @reboot line")
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--status', action='store_true', help='report the registration instead of installing')
    action.add_argument('--uninstall', action='store_true', help='remove the systemd unit or launchd agent')
    if hosting is not None:
        hosting.add_arguments(parser)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    mode, why = args.mode, ''
    if mode == 'auto':
        mode, why = choose_mode(hosting, autostart=not args.no_autostart)
    if args.status or args.uninstall:
        if mode == 'manager' or mode in modes:
            raise ValueError(f'--status and --uninstall do not apply to mode {mode}')
        if mode == 'launchd':
            result = launchd.status(args.label) if args.status else launchd.uninstall(args.label)
        elif mode == 'cron':
            result = cron_status(root=root) if args.status else uninstall_cron(root=root)
        else:
            result = systemd_status() if args.status else uninstall_systemd()
        if args.uninstall:
            (root / 'boot-installation.json').unlink(missing_ok=True)
    else:
        if mode in modes:
            result = modes[mode](root, args)
        elif mode == 'launchd':
            result = launchd.install(root, args.label, activate=args.activate)
            save(root / 'boot-installation.json', result)
        elif mode == 'cron':
            result = install_cron(root)
            save(root / 'boot-installation.json', result)
        elif mode == 'manager':
            result = {'mode': 'manager', 'autostart': False, 'note': why or MANAGER_NOTE, 'reboot_verified': False}
            save(root / 'boot-installation.json', result)
        else:
            result = install_systemd(root, args.activate)
            save(root / 'boot-installation.json', result)
        if args.mode == 'auto':
            result = {**result, 'auto': True, 'auto_reason': why}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None
