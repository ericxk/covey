"""节点 at the Bridge (docs/architecture.md "节点"): the gateway the nodes dial, the hub's own node, the alerts.

Off unless COVEY_NODE_LISTEN is set (core/nodes.py): then there is no thread and nothing here runs. On:
- ``nodes-gateway`` (app/node_gateway.py): the TLS server.
- the 设置 card's 设备 tab (``panel``, app/node_panel.py): the nodes, 添加 and 移除.
- ``nodes-alerts`` (app/node_alerts.py): the alert cards.
- ``nodes-local`` (``keep_local``, every LOCAL_EVERY seconds): the hub's own node, so the hub is watched like any
  device: ``~/.covey/node/`` gets core/node_agent.py and a config that dials the gateway on the loopback address,
  then ``node.py ensure`` starts it unless it runs. It runs on its own (a new session), so a Bridge restart or a
  hung network disk does not stop it; it writes its samples to the local disk first. It also prunes the nodes' old
  files (core/nodes.py ``prune``) once an hour.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from covey.app.dashboard_entry import parse_bind
from covey.app.node_alerts import NodeAlerts
from covey.app.node_gateway import Gateway, local_config
from covey.app.node_panel import NodePanel
from covey.core import nodes
from covey.core.loops import every
from covey.core.state import ensure_private_dir, read_object, write_json

LOCAL_EVERY = 60
PRUNE_EVERY = 3600


def local_root():
    return Path(os.path.expanduser("~")) / ".covey" / "node"


class Nodes:
    def __init__(self, runtime, messenger, voice, *, environ=None):
        self.runtime, self.environ = runtime, environ
        self.alerts = NodeAlerts(runtime, messenger, voice, environ=environ)
        self.gateway = Gateway(runtime, environ=environ, events=lambda: None)
        self.panel = NodePanel(runtime, messenger, voice, self.gateway, environ=environ)  # The 设置 card's 设备 tab.
        self.pruned = 0.0

    def threads(self):
        if not nodes.enabled(self.environ):
            return ()
        return (*self.gateway.threads(), *self.alerts.threads(), ("nodes-local", self.run_local))

    def run_local(self, ctx):
        every(self.runtime, LOCAL_EVERY, self.keep_local, args=(ctx,), isolate="nodes local")

    def keep_local(self, ctx):
        if self.gateway.tls is None:
            return  # The gateway has not started (yet): nothing to dial.
        bind = parse_bind(nodes.listen_spec(self.environ), self.environ)
        root = local_root()
        ensure_private_dir(root)
        target = root / "node.py"
        if not target.exists():  # A node that runs fetches a newer file itself (welcome.version).
            tmp = root / "node.py.new"
            tmp.write_bytes(nodes.agent_bytes())
            os.replace(tmp, target)
        home = str(Path(ctx.state_dir).parent)
        config = local_config(ctx.state_dir, self.gateway.tls, bind, paths=[os.path.expanduser("~"), "/", home],
                              probe=[home])
        if read_object(root / "config.json") != config:
            write_json(root / "config.json", config)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("COVEY_TASK", "LARK_BRIDGE_TASK", "CLAUDECODE"))}
        result = subprocess.run([sys.executable, str(target), "ensure"], capture_output=True, text=True, timeout=30,
                                env=env, stdin=subprocess.DEVNULL)
        try:
            outcome = json.loads(result.stdout.strip().splitlines()[-1]).get("outcome")
        except (ValueError, IndexError, AttributeError):
            outcome = f"rc={result.returncode}"
        if outcome != "running":
            self.runtime.log(f"local node: {outcome}")
        if time.time() - self.pruned >= PRUNE_EVERY:
            self.pruned = time.time()
            removed = nodes.prune(ctx.state_dir)
            if removed:
                self.runtime.log(f"nodes pruned {removed} old files")
