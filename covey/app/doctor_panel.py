"""The 设置 card's 诊断 tab (cards/doctor_card.py): what service/doctor.py finds of this deployment, in a few lines.

The tab's record is the 设置 card's (app/settings_hub.py, app/menu_panel.py). The report is read when the card turns to the
tab or unfolds on it (``tab_fields``, after the click is acknowledged: it takes a few seconds, lark-cli's event status and
the hosting's own status command among them) and kept in the record as ``doctor`` (``summarize``: plain values, no command
output). The card shows that report and when it was read; another click on 诊断 folds the card, the next reads anew. The
owner's only, as every tab (a member's click is refused on the card). No buttons: nothing is changed and no model is called
(the Harness lines are the last probe's, as covey_doctor.py prints them without --probe-harnesses).
"""
from __future__ import annotations

import time

from covey.app.menu_panel import MenuPanel, hub_top
from covey.cards.doctor_card import doctor_card
from covey.cards.settings_hub import DOCTOR
from covey.core.process import service_key

OWNER_ONLY = '⚠️ 仅机器人主人可以看诊断。'
PERMISSION_LINES = 3


def summarize(report, key, now) -> dict:
    """What the tab keeps of ``report`` (service/doctor.py diagnose): the Manager, the service whose key is ``key`` (this
    Bridge's state directory), start at boot, the Harness probes, and how many other services the root has."""
    services = [row for row in report.get('services') or () if isinstance(row, dict)]
    mine = next((row for row in services if row.get('id') == key), None)
    manager = report.get('manager') if isinstance(report.get('manager'), dict) else None
    lag = manager.get('lag') if manager else None
    lag = round(lag, 1) if isinstance(lag, (int, float)) and not isinstance(lag, bool) else None
    boot = report.get('boot') if isinstance(report.get('boot'), dict) else {}
    probes = report.get('harness_probe') if isinstance(report.get('harness_probe'), dict) else {}
    data = {'checked_at': now, 'status': report.get('status'), 'runtime': report.get('runtime_status'),
            'manager': {'ok': lag is not None and lag < 120, 'lag': lag},
            'boot': {name: boot.get(name) for name in ('status', 'mode', 'autostart')},
            'harnesses': {name: {field: row.get(field) for field in ('ok', 'reason', 'advice', 'checked_at')}
                          for name, row in probes.items() if isinstance(row, dict)},
            'others': len(services) - (1 if mine else 0)}
    if mine is not None:
        bridge = mine.get('bridge') if isinstance(mine.get('bridge'), dict) else {}
        connection = bridge.get('remote_connection') if isinstance(bridge.get('remote_connection'), dict) else {}
        events = mine.get('events') if isinstance(mine.get('events'), dict) else {}
        data['service'] = {'status': mine.get('status'), 'desired': mine.get('desired_state'), 'alive': mine.get('alive'),
                           'generation': mine.get('generation'), 'bridge': bridge.get('status'),
                           'connection': connection.get('status'), 'connected': events.get('owned_required_consumers'),
                           'dropped': int(events.get('dropped') or 0),
                           'permission_env': [str(line) for line in mine.get('permission_env') or ()][:PERMISSION_LINES]}
    return data


def diagnose():
    """service/doctor.py's report of this machine's service root, events checked, no model called."""
    from covey.core.paths import service_root
    from covey.service.doctor import diagnose as run
    return run(service_root(), check_events=True, probe=False)


class DoctorPanel:
    def __init__(self, runtime, messenger, voice, *, report=diagnose, clock=time.time):
        """``report()``: service/doctor.py's diagnose (tests replace it)."""
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.report, self.clock = report, clock
        self.menu = MenuPanel(self, 'doctor-panel', 'doctor-cards', 'doctor-card', tab=DOCTOR)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True}

    def tab_fields(self, ctx):
        """The report now, as the record keeps it (``doctor``); read before the record's lock."""
        started = time.monotonic()
        try:
            data = summarize(self.report(), service_key(ctx.state_dir), self.clock())
        except Exception as exc:  # noqa: BLE001 - a report that cannot be read is said on the card
            data = {'checked_at': self.clock(), 'error': type(exc).__name__}
        self.runtime.log(f"doctor tab read status={data.get('status') or data.get('error')} "
                         f"ms={round((time.monotonic() - started) * 1000)}")
        return {'doctor': data}

    def tab_card(self, ctx, token, record):
        return self.draw(token, record['view'], record)

    def tab_shown(self, ctx, token):
        self.runtime.log(f'doctor tab shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.draw(path.stem, view, record))

    @staticmethod
    def draw(token, view, record):
        return doctor_card(token, view, record.get('doctor') or {}, top=hub_top(DOCTOR, token, view))
