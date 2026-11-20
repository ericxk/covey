"""Read-only, bounded local health snapshots; silence is not a connection failure."""
from __future__ import annotations

import os

from covey.feishu import ws_intake
from covey.feishu.events import consumers_snapshot
from covey.core.process import process_identity


def bridge_snapshot(runtime, workers):

    consumers = consumers_snapshot()
    threads = {name: thread.is_alive() for name, thread in tuple(workers.items())}
    required = ('im.message.receive_v1', 'card.action.trigger')
    healthy = (all(consumers.get(key, {}).get('ready') and consumers[key]['handling_seconds'] < 60
                   for key in required) and all(threads.values()))
    connection = runtime.connection_health
    if connection.get('status') in {'stalled', 'disconnected', 'unresponsive'}:
        healthy = False
    availability = runtime.availability_health
    transport = {'event_transport': 'sdk'} if ws_intake.enabled() else {}  # Only then: the default snapshot is unchanged.
    return {**transport, 'schema_version': 1, 'process': process_identity(os.getpid()),
            'status': 'ready' if healthy and not runtime.shutdown.is_set() else 'degraded',
            'consumers': consumers, 'workers': threads,
            'queued_jobs': runtime.jobs.queue.qsize(),
            'remote_connection': connection or {'status': 'starting'},
            'model_access': availability or 'not_probed'}
