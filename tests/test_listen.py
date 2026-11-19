"""Where a local server listens and the host others reach it at (core/listen.py), as the nodes' gateway uses them."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from covey.core import listen
from covey.core.listen import global_ipv6, link_host, parse_bind


class BindAndLinkTests(unittest.TestCase):
    def test_bind_specs(self):
        self.assertIsNone(parse_bind(""))
        self.assertEqual(parse_bind("10601"), ("::", 10601))
        self.assertEqual(parse_bind("[::]:10601"), ("::", 10601))
        self.assertEqual(parse_bind("0.0.0.0:10601"), ("0.0.0.0", 10601))
        self.assertEqual(parse_bind("[fdbd::1]:80"), ("fdbd::1", 80))
        for wrong in ("::1:80", "[::]", "host:", "host:0", "host:70000", ":80", "abc"):
            self.assertRaises(ValueError, parse_bind, wrong)
        with self.assertRaisesRegex(ValueError, "^COVEY_NODE_LISTEN 要写成"):  # The error names the setting it read.
            parse_bind("abc", setting="COVEY_NODE_LISTEN")

    def test_a_bind_spec_reads_the_reserved_port_from_the_environment(self):
        environ = {"ARNOLD_RESERVED_4_CURRENT_PORT": "10601", "HOST": "0.0.0.0"}
        self.assertEqual(parse_bind("[::]:$ARNOLD_RESERVED_4_CURRENT_PORT", environ), ("::", 10601))
        self.assertEqual(parse_bind("${HOST}:${ARNOLD_RESERVED_4_CURRENT_PORT}", environ), ("0.0.0.0", 10601))
        with self.assertRaisesRegex(ValueError, "ARNOLD_RESERVED_3_CURRENT_PORT 没有设置"):
            parse_bind("[::]:$ARNOLD_RESERVED_3_CURRENT_PORT", environ)
        with mock.patch.dict(os.environ, {"ARNOLD_RESERVED_4_CURRENT_PORT": "10777"}):
            self.assertEqual(parse_bind("$ARNOLD_RESERVED_4_CURRENT_PORT"), ("::", 10777))

    def test_link_hosts(self):
        # The IPv6 address before the name, which a browser may look up for IPv4 alone.
        with mock.patch.object(socket, "getfqdn", return_value="box.example.org"), \
                mock.patch.object(listen, "resolves", return_value=True), \
                mock.patch.object(listen, "global_ipv6", return_value="fdbd:dc01:28:37::14"):
            self.assertEqual(link_host("::"), "[fdbd:dc01:28:37::14]")
            self.assertEqual(link_host("0.0.0.0"), "box.example.org")  # It listens on IPv4 only.
        with mock.patch.object(socket, "getfqdn", return_value="box.example.org"), \
                mock.patch.object(listen, "global_ipv6", return_value=""):
            with mock.patch.object(listen, "resolves", return_value=True):
                self.assertEqual(link_host("::"), "box.example.org")
            with mock.patch.object(listen, "resolves", return_value=False):
                self.assertEqual((link_host("::"), link_host("0.0.0.0")), ("localhost", "localhost"))
        self.assertEqual(link_host("192.0.2.5"), "192.0.2.5")
        self.assertEqual(link_host("fdbd::2"), "[fdbd::2]")

    def test_the_ipv6_address_is_the_default_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "if_inet6"
            # The interfaces list eth2's address before eth0's; the default route leaves from eth0's.
            table.write_text("00000000000000000000000000000001 01 80 10 80 lo\n"
                             "fe800000000000000e42a1fffe57cf10 02 40 20 80 eth0\n"
                             "fdbddc01002800380000000000000068 03 40 00 80 eth2\n"
                             "fdbddc01002800370000000000000014 02 40 00 80 eth0\n")
            with mock.patch.object(listen, "IF_INET6", table):
                with mock.patch.object(socket, "socket", return_value=Probe("fdbd:dc01:28:37::14")):
                    self.assertEqual(global_ipv6(), "fdbd:dc01:28:37::14")
                # No IPv6 route, or one from a link-local address: the first global address of an interface.
                for probe in (Probe(OSError(101, "Network is unreachable")), Probe("fe80::1%eth0")):
                    with mock.patch.object(socket, "socket", return_value=probe):
                        self.assertEqual(global_ipv6(), "fdbd:dc01:28:38::68")
                table.write_text("00000000000000000000000000000001 01 80 10 80 lo\n")
                with mock.patch.object(socket, "socket", return_value=Probe(OSError(101, "Network is unreachable"))):
                    self.assertEqual(global_ipv6(), "")


class Probe:
    """The UDP socket global_ipv6 connects: it leaves from ``source``, or raises it (an OSError)."""

    def __init__(self, source):
        self.source = source

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, address):
        if isinstance(self.source, OSError):
            raise self.source

    def getsockname(self):
        return self.source, 0, 0, 0


if __name__ == "__main__":
    unittest.main()
