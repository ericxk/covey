"""The hub's side of the nodes' connections (docs/architecture.md "节点"): a TLS server in the Bridge, when
COVEY_NODE_LISTEN says where (core/nodes.py).

One port, two kinds of request after the TLS handshake:
- **HTTP GET** ``/join/<token>``: the bootstrap a device owner pipes into ``python3`` (``paste_command``): it writes
  core/node_agent.py to ``~/.covey/node/node.py`` and runs its ``join``. ``/node.py``: that file, for a node that
  upgrades itself. Nothing else.
- **the node protocol** (node_agent.py's docstring): ``join`` with a token answers the node's alias and key
  (core/nodes.py ``claim``); ``hello`` with a key answers ``welcome`` (the last sequence number the hub has of it and
  the version it should run) or ``revoke``; then each sample is recorded (``nodes.record_sample``, the minute rows),
  its rules evaluated (core/node_rules.py) and their events left in ``<state>/nodes/events/`` for app/node_alerts.py;
  an ``ack`` goes back every ACK_EVERY seconds.

A newer connection of a node replaces an older one. Nothing here holds a record lock of a job or a topic, and
nothing here talks to Feishu.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import socketserver
import ssl
import threading
import time
from pathlib import Path

from covey.app.dashboard_entry import link_host, parse_bind
from covey.core import nodes, node_rules
from covey.core import env as cenv
from covey.core.state import ensure_private_dir, read_object, write_json

ACK_EVERY = 30.0
READ_TIMEOUT = 150.0  # A node sends a sample at least every 10 s.
LINE_MAX = 4 << 20
HANDSHAKE_TIMEOUT = 15.0


def hub_address(bind, environ=None):
    """host:port the nodes dial: COVEY_NODE_ADDRESS, else this machine's address as the dashboard works it out."""
    given = str(cenv.get(nodes.ADDRESS_ENV, "", environ) or "").strip()
    return given or "%s:%d" % (link_host(bind[0]), bind[1])


def paste_command(address, spki, token):
    """What a device owner pastes: the bootstrap fetched over TLS pinned by the hub's public key, run by python3."""
    return "curl -fsSk --noproxy '*' --pinnedpubkey 'sha256//%s' 'https://%s/join/%s' | python3 -" % (spki, address, token)


def bootstrap(address, pin, token):
    """The script /join/<token> serves: node_agent.py written to ~/.covey/node/node.py, then its ``join``."""
    agent = base64.b64encode(nodes.agent_bytes()).decode()
    return ("import base64, os, subprocess, sys\n"
            "root = os.path.join(os.path.expanduser('~'), '.covey', 'node')\n"
            "os.makedirs(root, mode=0o700, exist_ok=True)\n"
            "target = os.path.join(root, 'node.py')\n"
            "open(target + '.new', 'wb').write(base64.b64decode(%r))\n"
            "os.replace(target + '.new', target)\n"
            "sys.exit(subprocess.call([sys.executable, target, 'join', %r, %r, %r]))\n" % (agent, address, pin, token))


def expired_script(message):
    return "print(%r)\nraise SystemExit(1)\n" % message


class Gateway:
    """The server. ``runtime`` logs and says when to stop; ``events`` is called after events are written (the
    sender's wake-up)."""

    def __init__(self, runtime, *, environ=None, events=None):
        self.runtime, self.environ, self.wake = runtime, environ, events or (lambda: None)
        self.lock = threading.Lock()
        self.live = {}  # alias -> (connection id, socket)
        self.minutes = nodes.Minutes()
        self.rules = {}  # alias -> rule state (core/node_rules.py), loaded from alerts.json on first use
        self.state_dir = None
        self.tls = None
        self.address = ""

    def threads(self):
        return (("nodes-gateway", self.run),) if nodes.enabled(self.environ) else ()

    def run(self, ctx):
        try:
            bind = parse_bind(nodes.listen_spec(self.environ), self.environ)
        except ValueError as exc:
            self.runtime.log(f"node gateway not started: {exc}")
            self.runtime.shutdown.wait()
            return
        self.state_dir = Path(ctx.state_dir)
        self.tls = nodes.ensure_tls(self.state_dir)
        self.address = hub_address(bind, self.environ)
        server = self.server(bind)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="nodes-accept",
                                  daemon=True)
        thread.start()
        self.runtime.log(f"node gateway listening on {bind[0]} port {server.server_address[1]} as {self.address}")
        try:
            self.runtime.shutdown.wait()
        finally:
            server.shutdown()
            server.server_close()
            for alias in list(self.live):
                self.minutes.flush(self.state_dir, alias)

    def server(self, bind):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.tls.cert), str(self.tls.key))
        gateway = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                gateway.serve(self.request, self.client_address, context)

        family = socket.AF_INET6 if ":" in bind[0] else socket.AF_INET

        class Server(socketserver.ThreadingTCPServer):
            address_family = family
            allow_reuse_address = True
            daemon_threads = True

            def server_bind(self):
                if family == socket.AF_INET6:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                super().server_bind()

        return Server(bind, Handler)

    # ------------------------------------------------------------ one connection

    def serve(self, raw, peer, context):
        raw.settimeout(HANDSHAKE_TIMEOUT)
        try:
            sock = context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            return
        reader = sock.makefile("rb")
        try:
            first = reader.readline(LINE_MAX)
            if first.startswith(b"GET "):
                self.http(sock, reader, first)
                return
            try:
                hello = json.loads(first.decode("utf-8"))
            except ValueError:
                return
            if not isinstance(hello, dict):
                return
            if hello.get("k") == "join":
                self.join(sock, hello, peer)
            elif hello.get("k") == "hello":
                self.session(sock, reader, hello, peer)
        except (OSError, ssl.SSLError, ValueError) as exc:
            self.runtime.log(f"node connection from {peer[0]} ended: {type(exc).__name__}")
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def send(self, sock, obj):
        sock.sendall((json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))

    def http(self, sock, reader, first):
        while reader.readline(8192).strip():
            pass
        target = first.split()[1].decode("ascii", "replace") if len(first.split()) > 1 else ""
        if target == "/node.py":
            body, kind = nodes.agent_bytes(), "text/x-python"
        elif target.startswith("/join/"):
            token = target[len("/join/"):]
            joins = read_object(nodes.nodes_dir(self.state_dir) / "joins.json")
            entry = joins.get(nodes.key_hash(token))
            ok = isinstance(entry, dict) and entry.get("expires", 0) > time.time()
            body = (bootstrap(self.address, self.tls.pin, token) if ok
                    else expired_script("这个加入口令已经用过或过期了，请在 Covey 的「设备」里重新添加。")).encode()
            kind = "text/x-python"
            self.runtime.log(f"node bootstrap served ok={ok}")
        else:
            sock.sendall(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        sock.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n" % (kind.encode(), len(body))
                     + body)

    def join(self, sock, hello, peer):
        claimed = nodes.claim(self.state_dir, str(hello.get("token") or ""), hello.get("info"))
        if claimed is None:
            self.send(sock, {"k": "err", "err": "token"})
            self.runtime.log(f"node join refused from {peer[0]}: unknown or expired token")
            return
        alias, key = claimed
        self.send(sock, {"k": "joined", "alias": alias, "key": key})
        self.runtime.log(f"node {alias} joined from {peer[0]}")
        self.wake()

    def session(self, sock, reader, hello, peer):
        alias, key = hello.get("alias"), hello.get("key")
        entry = nodes.verify(self.state_dir, alias, key)
        if entry == "revoked":
            self.send(sock, {"k": "revoke"})
            nodes.forget(self.state_dir, alias)
            self.runtime.log(f"node {alias} told it is removed")
            return
        if entry is None:
            self.send(sock, {"k": "err", "err": "auth"})
            self.runtime.log(f"node hello refused from {peer[0]}: unknown alias or key")
            return
        last = int(hello.get("last") or 0)
        have = nodes.last_seq(self.state_dir, alias)
        ack = have if have <= last else last  # A node whose ring was wiped starts again from where it is.
        conn = secrets.token_hex(4)
        with self.lock:
            old = self.live.get(alias)
            self.live[alias] = (conn, sock)
        if old:
            try:
                old[1].close()
            except OSError:
                pass
        info = hello.get("info") if isinstance(hello.get("info"), dict) else {}
        nodes.update_info(self.state_dir, alias, info)
        nodes.write_link(self.state_dir, alias, connected=True, since=time.time(), heard=time.time(), peer=peer[0],
                         version=hello.get("version"), info=info, seq=ack, conn=conn)
        self.send(sock, {"k": "welcome", "ack": ack, "version": nodes.agent_version()})
        self.runtime.log(f"node {alias} connected from {peer[0]} version {hello.get('version')} ack {ack}")
        sock.settimeout(READ_TIMEOUT)
        acked_at = time.time()
        seq = ack
        try:
            while True:
                line = reader.readline(LINE_MAX)
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("k") != "s":
                    continue
                number = int(msg.get("seq") or 0)
                if number <= seq:
                    continue  # Already recorded (sent again after a reconnect).
                seq = number
                self.sample(alias, msg)
                now = time.time()
                nodes.write_link(self.state_dir, alias, heard=now, seq=seq)
                if now - acked_at >= ACK_EVERY:
                    self.send(sock, {"k": "ack", "seq": seq})
                    acked_at = now
        finally:
            with self.lock:
                mine = self.live.get(alias, (None,))[0] == conn
                if mine:
                    self.live.pop(alias, None)
            if mine:
                nodes.write_link(self.state_dir, alias, connected=False, left=time.time(), seq=seq)
                self.minutes.flush(self.state_dir, alias)
                self.runtime.log(f"node {alias} disconnected")

    def sample(self, alias, sample):
        now = time.time()
        nodes.record_sample(self.state_dir, alias, sample, now=now)
        self.minutes.add(self.state_dir, alias, sample)
        folder = nodes.node_dir(self.state_dir, alias)
        if alias not in self.rules:
            self.rules[alias] = read_object(folder / "alerts.json")
        state, events = node_rules.evaluate(self.rules[alias], sample, now)
        if state != self.rules[alias]:
            self.rules[alias] = state
            write_json(folder / "alerts.json", state, ensure_ascii=False)
        for event in events:
            event = dict(event, alias=alias, id=secrets.token_hex(6))
            nodes.append_line(folder / "alerts.jsonl", event)
            outbox = nodes.nodes_dir(self.state_dir) / "events"
            ensure_private_dir(outbox)
            write_json(outbox / ("%d-%s.json" % (int(now * 1000), event["id"])), event, ensure_ascii=False)
        if events:
            self.runtime.log(f"node {alias} events " + ",".join(f"{e['rule']}:{e['kind']}" for e in events))
            self.wake()

    def revoke_now(self, alias):
        """Tells a connected node it is removed (its next hello is told too)."""
        with self.lock:
            live = self.live.get(alias)
        if live:
            try:
                self.send(live[1], {"k": "revoke"})
            except OSError:
                pass


def local_config(state_dir, tls, bind, *, paths=None, probe=None):
    """The config.json of the hub's own node: it dials the hub on the loopback address."""
    host = "::1" if ":" in bind[0] else "127.0.0.1"
    hub = "[%s]:%d" % (host, bind[1]) if ":" in host else "%s:%d" % (host, bind[1])
    return {"hub": hub, "pin": tls.pin, "alias": nodes.LOCAL, "key": nodes.ensure_local(state_dir),
            "paths": paths or [os.path.expanduser("~"), "/"], "probe": probe or []}
