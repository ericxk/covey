"""Where the sensitive-term dashboard is: where it listens (COVEY_DASHBOARD, ``parse_bind``), the address
links name (COVEY_DASHBOARD_URL, else this machine's IPv6 address, or its name without one:
``dashboard_address``), and its fixed entry: a Goofy quick preview page that sends the browser on to where the
dashboard is now.

A dev machine can land on another host when it restarts, and the dashboard's address carries the host's address.
A deployment can have a fixed entry that sends the browser on to where the dashboard is now: the extension hook
``dashboard_entry`` (core/extensions.py; the company's is a Goofy quick preview page, extensions/covey_internal/
dashboard_entry.py). Without the hook there is no fixed entry and nothing is published; the dashboard and the link
to it work as before.

<state>/redaction/dashboard-entry.json, changed only under ENTRY_LOCK and never while a call is out:
- ``chat_id``: the owner's private chat where the link was last given (the 设置 card's 脱敏 tab); announcements go there.
- ``address``: the dashboard address last given there (the command's reply or an announcement).
- ``announcing``: {address, key, at} of an announcement being sent; one cut short goes again under its key.
- ``entry``: {alias, target, at} of the page last published; ``entry_error``: why the last try failed.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
from pathlib import Path

from covey.core import env as cenv
from covey.core.state import read_object, write_json

ENTRY_LOCK = threading.Lock()
DASHBOARD_ENV = "COVEY_DASHBOARD"  # Where to listen; unset: no dashboard.
URL_ENV = "COVEY_DASHBOARD_URL"  # The base URL links use, when the machine's own address is not it.
_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
ROUTE_PROBE = ("2001:4860:4860::8888", 53)  # Any global address: connecting a UDP socket picks a route, sends nothing.
IF_INET6 = Path("/proc/net/if_inet6")


def record_path(state_dir) -> Path:
    return Path(state_dir) / "redaction" / "dashboard-entry.json"


def read_record(state_dir):
    return read_object(record_path(state_dir))


def update_record(state_dir, **changes):
    """The record with ``changes`` applied (None removes a key), written under ENTRY_LOCK."""
    with ENTRY_LOCK:
        record = read_record(state_dir)
        for key, value in changes.items():
            if value is None:
                record.pop(key, None)
            else:
                record[key] = value
        write_json(record_path(state_dir), record, ensure_ascii=False)
        return record


def parse_bind(spec, environ=None):
    """(host, port) to listen on from COVEY_DASHBOARD; None when it is empty. ValueError when wrong.
    ``$NAME`` and ``${NAME}`` are the environment's NAME, which must be set."""
    environ = os.environ if environ is None else environ

    def value(match):
        name = match.group(1) or match.group(2)
        if not str(environ.get(name) or "").strip():
            raise ValueError(f"{DASHBOARD_ENV} 用到的环境变量 {name} 没有设置")
        return str(environ[name]).strip()

    spec = _REFERENCE.sub(value, (spec or "").strip())
    if not spec:
        return None
    if spec.isdigit():
        host, port = "::", spec
    elif spec.startswith("["):
        host, sep, port = spec[1:].partition("]:")
        if not sep:
            raise ValueError(f"{DASHBOARD_ENV} 要写成 [IPv6]:端口、IPv4:端口或端口")
    else:
        host, sep, port = spec.rpartition(":")
        if not sep or ":" in host:
            raise ValueError(f"{DASHBOARD_ENV} 要写成 [IPv6]:端口、IPv4:端口或端口")
    if not port.isdigit() or not 0 < int(port) < 65536 or not host:
        raise ValueError(f"{DASHBOARD_ENV} 的端口要在 1–65535")
    return host, int(port)


def global_ipv6():
    """This machine's IPv6 address others reach it at, or '': the source address of its default route (the one
    a dev machine registers for its reserved ports), else the first
    global address of an interface, which may be another network's (eth2 before eth0 on 2026-11-13)."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            probe.connect(ROUTE_PROBE)
            address = ipaddress.IPv6Address(probe.getsockname()[0].partition("%")[0])
        if not (address.is_link_local or address.is_loopback or address.is_unspecified or address.is_multicast):
            return str(address)
    except (OSError, ValueError):
        pass  # No IPv6 route.
    try:
        lines = IF_INET6.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        # address, interface index, prefix length, scope (00: global), flags, name.
        if len(fields) >= 6 and fields[3] == "00" and fields[5] != "lo":
            try:
                return str(ipaddress.IPv6Address(int(fields[0], 16)))
            except ValueError:
                continue
    return ""


def resolves(name):
    try:
        return bool(socket.getaddrinfo(name, None))
    except (OSError, UnicodeError):
        return False


def link_host(bind_host):
    """The host links name: a specific bind address as it is; else this machine's IPv6 address (in brackets),
    unless it listens on IPv4 only (0.0.0.0); else its name when it resolves; else localhost.

    The address before the name: a browser may look the name up for IPv4 alone (Chrome does when it has no
    route to the IPv6 internet) while a dev machine is reached over IPv6. On 2026-11-13 the owner's browser
    never reached http://<the machine's name>:<port>; the platform registers the IPv6 address for the port."""
    if bind_host not in ("::", "0.0.0.0", ""):
        return f"[{bind_host}]" if ":" in bind_host else bind_host
    address = global_ipv6() if bind_host != "0.0.0.0" else ""
    if address:
        return f"[{address}]"
    name = socket.getfqdn()
    if "." in name and not name.startswith("localhost") and resolves(name):
        return name
    return "localhost"


def dashboard_address(bind, environ=None):
    """Where the dashboard is reached: COVEY_DASHBOARD_URL, else http://<link_host>:<port>."""
    environ = os.environ if environ is None else environ
    return str(cenv.get(URL_ENV, "", environ) or "").strip().rstrip("/") or f"http://{link_host(bind[0])}:{bind[1]}"
