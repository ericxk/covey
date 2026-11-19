"""Where a local server listens (``parse_bind``: ``[::]:10601``, ``0.0.0.0:10601`` or a bare port, meaning ``[::]``; ``$NAME``
is read from the environment, so ``[::]:$ARNOLD_RESERVED_4_CURRENT_PORT`` follows the port the platform reserves) and the
host others reach it at (``link_host``): this machine's IPv6 address, or its name without one. The nodes' gateway
(app/node_gateway.py) uses both. Standard library only.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path

_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
ROUTE_PROBE = ("2001:4860:4860::8888", 53)  # Any global address: connecting a UDP socket picks a route, sends nothing.
IF_INET6 = Path("/proc/net/if_inet6")


def parse_bind(spec, environ=None, *, setting="COVEY_NODE_LISTEN"):
    """(host, port) to listen on from ``spec``, the value of the variable ``setting``; None when it is empty. ValueError,
    naming ``setting``, when wrong. ``$NAME`` and ``${NAME}`` are the environment's NAME, which must be set."""
    environ = os.environ if environ is None else environ

    def value(match):
        name = match.group(1) or match.group(2)
        if not str(environ.get(name) or "").strip():
            raise ValueError(f"{setting} 用到的环境变量 {name} 没有设置")
        return str(environ[name]).strip()

    spec = _REFERENCE.sub(value, (spec or "").strip())
    if not spec:
        return None
    if spec.isdigit():
        host, port = "::", spec
    elif spec.startswith("["):
        host, sep, port = spec[1:].partition("]:")
        if not sep:
            raise ValueError(f"{setting} 要写成 [IPv6]:端口、IPv4:端口或端口")
    else:
        host, sep, port = spec.rpartition(":")
        if not sep or ":" in host:
            raise ValueError(f"{setting} 要写成 [IPv6]:端口、IPv4:端口或端口")
    if not port.isdigit() or not 0 < int(port) < 65536 or not host:
        raise ValueError(f"{setting} 的端口要在 1–65535")
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
