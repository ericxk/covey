"""Nodes (节点): the machines that keep a connection to this hub, the hub itself included, and what they report.

On only when ``COVEY_NODE_LISTEN`` says where the hub listens for them (``[::]:<port>``, an IPv4 ``host:port`` or a
port; ``$NAME`` is the environment's NAME): off, nothing here reads or writes a file and no thread starts
(docs/architecture.md "节点", docs/configuration.md "节点与机器状况").

Everything is under ``<state>/nodes/``:
- ``tls/``: the hub's self-signed certificate and key (``ensure_tls``, made once with ``openssl``); a node pins the
  SHA-256 of the certificate's DER (``Tls.pin``), ``curl`` the SPKI hash (``Tls.spki``).
- ``registry.json``: each node by alias, ``{title, key_sha256, joined_at, via, info, revoked_at}``; only the hash of a
  node's key is kept. ``joins.json``: the open join tokens by their hash, ``{alias, title, expires, via}``. Both are
  changed under ``registry.lock``.
- ``<alias>/``: ``latest.json`` (the last sample with the hub's receipt time), ``link.json`` (connected or not, since,
  peer, version, host info), ``raw/<YYYYMMDD>.jsonl`` (every sample as it came, RAW_DAYS kept), ``minutes/<YYYYMMDD>.jsonl``
  (one row a minute: ``minute_row``), ``alerts.json`` (each rule's state, core/node_rules.py) and ``alerts.jsonl``.
- ``events/``: alert events for the Bridge to send (app/node_alerts.py), one file each.

Standard library plus core/state, core/file_lock and core/env.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import covey.core.env as cenv
from covey.core.file_lock import file_lock
from covey.core.state import ensure_private_dir, read_object, write_json

LISTEN_ENV = "COVEY_NODE_LISTEN"
ADDRESS_ENV = "COVEY_NODE_ADDRESS"  # host:port the nodes dial, when the address the hub works out is not it.
AGENT_FILE = Path(__file__).with_name("node_agent.py")
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
LOCAL = "local"  # The hub's own node.
JOIN_TTL = 1800.0
RAW_DAYS = 3
MINUTE_DAYS = 30
CERT_DAYS = 3650
ONLINE_SECONDS = 45.0  # A node heard from within this is online.


def listen_spec(environ=None):
    return str(cenv.get(LISTEN_ENV, "", environ) or "").strip()


def enabled(environ=None):
    """Whether COVEY_NODE_LISTEN is set (read on every call)."""
    return bool(listen_spec(environ))


def nodes_dir(state_dir) -> Path:
    return Path(state_dir) / "nodes"


def node_dir(state_dir, alias) -> Path:
    if not ALIAS.match(alias or ""):
        raise ValueError("bad node alias %r" % (alias,))
    return nodes_dir(state_dir) / alias


def agent_bytes():
    return AGENT_FILE.read_bytes()


def agent_version():
    return hashlib.sha256(agent_bytes()).hexdigest()[:16]


def key_hash(key):
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- TLS

@dataclass(frozen=True)
class Tls:
    cert: Path
    key: Path
    pin: str  # SHA-256 of the certificate's DER, hex: what node_agent.py pins.
    spki: str  # base64 SHA-256 of the public key's DER: curl's --pinnedpubkey sha256//<spki>.


def ensure_tls(state_dir, *, openssl="openssl"):
    """The hub's certificate, made on first use (EC P-256, CERT_DAYS)."""
    folder = nodes_dir(state_dir) / "tls"
    ensure_private_dir(folder)
    cert, key = folder / "hub.crt", folder / "hub.key"
    with file_lock(folder / "tls.lock"):
        if not (cert.exists() and key.exists()):
            subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", str(CERT_DAYS), "-subj", "/CN=covey-hub"],
                           check=True, capture_output=True, timeout=60)
            os.chmod(key, 0o600)
        der = subprocess.run([openssl, "x509", "-in", str(cert), "-outform", "der"], check=True, capture_output=True,
                             timeout=30).stdout
        pub = subprocess.run([openssl, "x509", "-in", str(cert), "-noout", "-pubkey"], check=True, capture_output=True,
                             timeout=30).stdout
        spki_der = subprocess.run([openssl, "pkey", "-pubin", "-outform", "der"], input=pub, check=True,
                                  capture_output=True, timeout=30).stdout
    return Tls(cert, key, hashlib.sha256(der).hexdigest(), base64.b64encode(hashlib.sha256(spki_der).digest()).decode())


# ---------------------------------------------------------------- registry and joins

def _lock(state_dir):
    folder = nodes_dir(state_dir)
    ensure_private_dir(folder)
    return file_lock(folder / "registry.lock")


def registry(state_dir) -> dict:
    """alias -> entry of every node, revoked ones included."""
    value = read_object(nodes_dir(state_dir) / "registry.json").get("nodes")
    return value if isinstance(value, dict) else {}


def active(state_dir) -> dict:
    return {alias: entry for alias, entry in registry(state_dir).items() if not entry.get("revoked_at")}


def _save_registry(state_dir, nodes):
    write_json(nodes_dir(state_dir) / "registry.json", {"schema": 1, "nodes": nodes}, ensure_ascii=False, indent=1)


def open_join(state_dir, alias, title, *, via="paste", ttl=JOIN_TTL, now=None):
    """A one-time token that makes the next node presenting it ``alias``; ValueError for a bad or taken alias."""
    if not ALIAS.match(alias or "") or alias == LOCAL:
        raise ValueError("名字要以小写字母开头，只用小写字母、数字和 -，最多 24 个字符，且不能叫 local")
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(24)
    with _lock(state_dir):
        if alias in active(state_dir):
            raise ValueError("已经有一台叫 %s 的设备" % alias)
        path = nodes_dir(state_dir) / "joins.json"
        joins = {k: v for k, v in read_object(path).items() if isinstance(v, dict) and v.get("expires", 0) > now}
        joins[key_hash(token)] = {"alias": alias, "title": title or alias, "expires": now + ttl, "via": via}
        write_json(path, joins, ensure_ascii=False)
    return token


def claim(state_dir, token, info, *, now=None):
    """(alias, key) for a node presenting ``token``; the token is used up. None when it is unknown or expired."""
    now = time.time() if now is None else now
    with _lock(state_dir):
        path = nodes_dir(state_dir) / "joins.json"
        joins = read_object(path)
        entry = joins.pop(key_hash(token), None)
        write_json(path, joins, ensure_ascii=False)
        if not isinstance(entry, dict) or entry.get("expires", 0) <= now:
            return None
        nodes = registry(state_dir)
        alias = entry["alias"]
        if alias in nodes and not nodes[alias].get("revoked_at"):
            return None
        key = secrets.token_urlsafe(32)
        nodes[alias] = {"title": entry.get("title") or alias, "key_sha256": key_hash(key), "joined_at": now,
                        "via": entry.get("via", "paste"), "info": info if isinstance(info, dict) else {}}
        _save_registry(state_dir, nodes)
    return alias, key


def ensure_local(state_dir, title="本机", *, now=None):
    """The hub's own node's key: made and registered on first use, kept in ``tls/local.key`` (0600)."""
    path = nodes_dir(state_dir) / "tls" / "local.key"
    ensure_private_dir(path.parent)
    with _lock(state_dir):
        key = path.read_text().strip() if path.exists() else ""
        nodes = registry(state_dir)
        entry = nodes.get(LOCAL) or {}
        if not key or entry.get("key_sha256") != key_hash(key) or entry.get("revoked_at"):
            key = secrets.token_urlsafe(32)
            path.write_text(key)
            os.chmod(path, 0o600)
            nodes[LOCAL] = {"title": title, "key_sha256": key_hash(key), "joined_at": time.time() if now is None else now,
                            "via": "local", "info": {}}
            _save_registry(state_dir, nodes)
    return key


def verify(state_dir, alias, key):
    """The registry entry of ``alias`` when ``key`` is its key; "revoked" for a revoked node; None otherwise."""
    entry = registry(state_dir).get(alias) if isinstance(alias, str) else None
    if not isinstance(entry, dict) or not isinstance(key, str):
        return None
    if not secrets.compare_digest(entry.get("key_sha256", ""), key_hash(key)):
        return None
    return "revoked" if entry.get("revoked_at") else entry


def revoke(state_dir, alias, *, now=None):
    """Marks ``alias`` revoked (its next hello is answered ``revoke``); False when there is no such node."""
    with _lock(state_dir):
        nodes = registry(state_dir)
        if alias not in nodes or nodes[alias].get("revoked_at"):
            return False
        nodes[alias]["revoked_at"] = time.time() if now is None else now
        _save_registry(state_dir, nodes)
    return True


def forget(state_dir, alias):
    """Drops a revoked node's entry once it has been told (or never came back)."""
    with _lock(state_dir):
        nodes = registry(state_dir)
        if nodes.get(alias, {}).get("revoked_at"):
            nodes.pop(alias)
            _save_registry(state_dir, nodes)


def update_info(state_dir, alias, info):
    with _lock(state_dir):
        nodes = registry(state_dir)
        if alias in nodes and isinstance(info, dict) and nodes[alias].get("info") != info:
            nodes[alias]["info"] = info
            _save_registry(state_dir, nodes)


# ---------------------------------------------------------------- what nodes report

def day(epoch):
    return dt.datetime.fromtimestamp(epoch).strftime("%Y%m%d")


def append_line(path: Path, row):
    ensure_private_dir(path.parent)
    with open(path, "a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def record_sample(state_dir, alias, sample, *, now=None):
    """A sample as it arrived: ``raw/``, and ``latest.json`` when it is the newest."""
    now = time.time() if now is None else now
    folder = node_dir(state_dir, alias)
    row = dict(sample, got=round(now, 3))
    append_line(folder / "raw" / ("%s.jsonl" % day(sample.get("t") or now)), row)
    latest = read_object(folder / "latest.json")
    if float(sample.get("t") or 0) >= float(latest.get("t") or 0):
        write_json(folder / "latest.json", row, ensure_ascii=False)


def last_seq(state_dir, alias):
    return int(read_object(node_dir(state_dir, alias) / "link.json").get("seq") or 0)


def write_link(state_dir, alias, **fields):
    path = node_dir(state_dir, alias) / "link.json"
    ensure_private_dir(path.parent)
    link = read_object(path)
    link.update(fields)
    write_json(path, link, ensure_ascii=False)
    return link


def read_link(state_dir, alias):
    return read_object(node_dir(state_dir, alias) / "link.json")


def read_latest(state_dir, alias):
    return read_object(node_dir(state_dir, alias) / "latest.json")


def online(link, now=None):
    now = time.time() if now is None else now
    return bool(link.get("connected")) and now - float(link.get("heard") or 0) <= ONLINE_SECONDS


class Minutes:
    """Folds samples into one row a minute per node (``minute_row``) and appends the row when the minute ends."""

    def __init__(self):
        self.open = {}  # alias -> (minute, [samples])

    def add(self, state_dir, alias, sample):
        minute = int((sample.get("t") or time.time()) // 60)
        current = self.open.get(alias)
        if current and current[0] != minute:
            self.flush(state_dir, alias)
            current = None
        if current is None:
            current = self.open[alias] = (minute, [])
        current[1].append(sample)

    def flush(self, state_dir, alias):
        current = self.open.pop(alias, None)
        if current and current[1]:
            row = minute_row(current[0] * 60, current[1])
            append_line(node_dir(state_dir, alias) / "minutes" / ("%s.jsonl" % day(row["t"])), row)


def _avg_max(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return (round(sum(values) / len(values), 1), round(max(values), 1)) if values else (None, None)


def minute_row(start, samples):
    """One minute: CPU and memory average and peak, each disk's fullest, GPU utilisation, the top processes of the
    minute's last sample, and whether a burst ran."""
    cpu = _avg_max([(s.get("cpu") or {}).get("pct") for s in samples])
    mem = _avg_max([(s.get("mem") or {}).get("pct") for s in samples])
    last = samples[-1]
    disks = {}
    for s in samples:
        for d in s.get("disk") or []:
            if isinstance(d, dict) and d.get("pct") is not None:
                disks[d["path"]] = max(disks.get(d["path"], 0), d["pct"])
    row = {"t": start, "n": len(samples), "cpu": cpu[0], "cpu_max": cpu[1], "mem": mem[0], "mem_max": mem[1],
           "mem_used": (last.get("mem") or {}).get("used"), "mem_limit": (last.get("mem") or {}).get("limit"),
           "disk": disks, "burst": any(s.get("burst") for s in samples),
           "top": [{k: p.get(k) for k in ("comm", "rss", "cpu", "task") if p.get(k) is not None}
                   for p in (last.get("procs") or [])[:5]]}
    gpu = [g for s in samples for g in (s.get("gpu") or [])]
    if gpu:
        row["gpu"] = _avg_max([g.get("util") for g in gpu])[0]
    return row


def read_minutes(state_dir, alias, since, *, now=None):
    """The minute rows from ``since`` (epoch) on, oldest first."""
    now = time.time() if now is None else now
    folder = node_dir(state_dir, alias) / "minutes"
    rows = []
    d = dt.datetime.fromtimestamp(since).date()
    end = dt.datetime.fromtimestamp(now).date()
    while d <= end:
        try:
            with open(folder / ("%s.jsonl" % d.strftime("%Y%m%d"))) as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("t", 0) >= since:
                        rows.append(row)
        except OSError:
            pass
        d += dt.timedelta(days=1)
    return rows


def prune(state_dir, *, now=None):
    """Removes raw files older than RAW_DAYS and minute files older than MINUTE_DAYS, of every node."""
    now = time.time() if now is None else now
    removed = 0
    root = nodes_dir(state_dir)
    if not root.is_dir():
        return 0
    for folder in root.iterdir():
        if not folder.is_dir() or not ALIAS.match(folder.name):
            continue
        for sub, days in (("raw", RAW_DAYS), ("minutes", MINUTE_DAYS)):
            cutoff = day(now - days * 86400)
            try:
                names = os.listdir(folder / sub)
            except OSError:
                continue
            for name in names:
                if name.endswith(".jsonl") and name[:8].isdigit() and name[:8] < cutoff:
                    try:
                        os.unlink(folder / sub / name)
                        removed += 1
                    except OSError:
                        pass
    return removed
