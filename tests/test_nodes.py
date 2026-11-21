"""节点 (docs/architecture.md "节点"): the node program (covey/core/node_agent.py), the hub's registry and records
(covey/core/nodes.py), the alert rules (covey/core/node_rules.py), the gateway (covey/app/node_gateway.py) with a real
node joining over TLS on the loopback address under a temporary HOME, and the alert sender (covey/app/node_alerts.py).
Nothing here touches the real ~/.covey; the end-to-end test fails on a node process it left running."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, make_context

from covey.app import node_alerts, node_gateway, pins
from covey.core.jobs import upsert_job_record
from covey.cards import node_cards
from covey.core import node_agent, node_rules, nodes

AGENT = REPO / "covey" / "core" / "node_agent.py"


def sample(t, pct=10.0, oom=0, boot="b1", disk=None, procs=None, seq=1):
    return {"t": t, "seq": seq, "boot": boot, "mem": {"pct": pct, "used": int(pct * 10), "limit": 1000, "oom_kill": oom},
            "cpu": {"pct": 5.0}, "disk": disk or [], "procs": procs or [{"pid": 1, "comm": "x", "rss": 5, "task": "k1"}]}


class RulesTest(unittest.TestCase):
    def test_memory_raises_after_the_hold_and_clears_below_the_lower_line(self):
        state, events = node_rules.evaluate({}, sample(0, 95), 0)
        self.assertEqual(events, [])
        state, events = node_rules.evaluate(state, sample(30, 96), 30)
        self.assertEqual(events, [])
        state, events = node_rules.evaluate(state, sample(61, 97), 61)
        self.assertEqual([(e["rule"], e["kind"]) for e in events], [("mem", "raise")])
        self.assertEqual(events[0]["top"][0]["task"], "k1")
        state, events = node_rules.evaluate(state, sample(70, 88), 70)  # Between the lines: still raised.
        self.assertEqual(events, [])
        state, events = node_rules.evaluate(state, sample(80, 80), 80)
        self.assertEqual([(e["rule"], e["kind"]) for e in events], [("mem", "clear")])

    def test_a_dip_restarts_the_hold(self):
        state, _ = node_rules.evaluate({}, sample(0, 95), 0)
        state, _ = node_rules.evaluate(state, sample(40, 50), 40)
        state, events = node_rules.evaluate(state, sample(70, 95), 70)
        self.assertEqual(events, [])

    def test_an_oom_kill_is_one_event_and_a_reboot_is_none(self):
        state, events = node_rules.evaluate({}, sample(0, oom=2), 0)
        self.assertEqual(events, [])
        state, events = node_rules.evaluate(state, sample(10, oom=3), 10)
        self.assertEqual([(e["rule"], e["kind"], e["count"]) for e in events], [("oom", "once", 1)])
        state, events = node_rules.evaluate(state, sample(20, oom=5, boot="b2"), 20)
        self.assertEqual(events, [])

    def test_disks_and_write_probes(self):
        disk = [{"path": "/d", "pct": 95.0, "free": 1, "write": {"ok": False, "error": "Disk quota exceeded"}}]
        state, events = node_rules.evaluate({}, sample(0, disk=disk), 0)
        self.assertEqual(sorted((e["rule"], e["kind"]) for e in events), [("disk:/d", "raise"), ("write:/d", "raise")])
        disk = [{"path": "/d", "pct": 50.0, "write": {"ok": True}}]
        state, events = node_rules.evaluate(state, sample(10, disk=disk), 10)
        self.assertEqual(sorted((e["rule"], e["kind"]) for e in events), [("disk:/d", "clear"), ("write:/d", "clear")])

    def test_a_disk_alert_names_who_fills_the_disk_not_who_holds_memory(self):
        dirs = {"path": "/", "items": [{"path": "/tmp", "bytes": 9 << 30, "files": 3, "shape": "a-*.tar"}], "at": 5}
        disk = [{"path": "/d", "pct": 92.0, "free": 1 << 30, "growth": [{"secs": 3600, "bytes": 2 << 30}], "dirs": dirs}]
        got = dict(sample(0, disk=disk), writers=[{"pid": 7, "comm": "pytest", "bytes": 6 << 30, "secs": 60, "task": "k2"}])
        _, events = node_rules.evaluate({}, got, 0)
        self.assertNotIn("top", events[0])
        self.assertEqual(events[0]["writers"][0]["task"], "k2")
        self.assertEqual((events[0]["dirs"], events[0]["growth"][0]["bytes"]), (dirs, 2 << 30))
        _, events = node_rules.evaluate({}, sample(0, disk=[{"path": "/d", "pct": 92.0, "free": 1}]), 0)
        self.assertEqual(sorted(events[0]), ["at", "free", "kind", "path", "pct", "rule"])  # Nothing to say, no keys.


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, True)

    def test_a_token_joins_once_and_the_key_is_kept_as_a_hash(self):
        token = nodes.open_join(self.state, "box", "盒子")
        alias, key = nodes.claim(self.state, token, {"hostname": "h"})
        self.assertEqual(alias, "box")
        self.assertIsNone(nodes.claim(self.state, token, {}))
        self.assertNotIn(key, (self.state / "nodes" / "registry.json").read_text())
        self.assertEqual(nodes.verify(self.state, "box", key)["title"], "盒子")
        self.assertIsNone(nodes.verify(self.state, "box", key + "x"))
        with self.assertRaises(ValueError):
            nodes.open_join(self.state, "box", "again")

    def test_an_expired_token_and_bad_aliases(self):
        token = nodes.open_join(self.state, "box", "", now=time.time() - 2 * nodes.JOIN_TTL)
        self.assertIsNone(nodes.claim(self.state, token, {}))
        for bad in ("Box", "1box", "local", "a" * 25, ""):
            with self.assertRaises(ValueError):
                nodes.open_join(self.state, bad, "")

    def test_revoke_and_forget(self):
        alias, key = nodes.claim(self.state, nodes.open_join(self.state, "box", ""), {})
        self.assertTrue(nodes.revoke(self.state, alias))
        self.assertEqual(nodes.verify(self.state, alias, key), "revoked")
        self.assertNotIn(alias, nodes.active(self.state))
        nodes.forget(self.state, alias)
        self.assertNotIn(alias, nodes.registry(self.state))
        nodes.open_join(self.state, "box", "")  # The name is free again.

    def test_the_hubs_own_key_is_stable(self):
        first = nodes.ensure_local(self.state)
        self.assertEqual(nodes.ensure_local(self.state), first)
        self.assertEqual(nodes.verify(self.state, nodes.LOCAL, first)["via"], "local")
        self.assertEqual(oct((self.state / "nodes" / "tls" / "local.key").stat().st_mode & 0o777), "0o600")


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, True)

    def test_minutes_fold_and_read_back(self):
        minutes = nodes.Minutes()
        base = (int(time.time()) // 60) * 60 - 120
        for i, pct in enumerate((10, 30, 20)):
            minutes.add(self.state, "box", sample(base + i * 10, pct))
        minutes.add(self.state, "box", sample(base + 60, 50))  # The next minute: the first is written.
        rows = nodes.read_minutes(self.state, "box", base - 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["n"], rows[0]["mem"], rows[0]["mem_max"]), (3, 20.0, 30.0))
        minutes.flush(self.state, "box")
        self.assertEqual(len(nodes.read_minutes(self.state, "box", base - 1)), 2)

    def test_latest_keeps_the_newest_and_prune_drops_old_days(self):
        nodes.record_sample(self.state, "box", sample(200, 10, seq=2))
        nodes.record_sample(self.state, "box", sample(100, 20, seq=1))
        self.assertEqual(nodes.read_latest(self.state, "box")["seq"], 2)
        old = self.state / "nodes" / "box" / "raw" / "20000101.jsonl"
        old.write_text("{}\n")
        self.assertEqual(nodes.prune(self.state), 2)  # The old raw day and the one of t=200 (1970).
        self.assertFalse(old.exists())


class AgentTest(unittest.TestCase):
    def test_ring_reads_back_from_memory_and_from_files(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        ring = node_agent.Ring(folder, segment=400, total=4000)
        for i in range(30):
            ring.append({"t": i, "pad": "x" * 200})
        self.assertEqual([r["seq"] for r in ring.since(27)], [28, 29, 30])
        again = node_agent.Ring(folder, segment=400, total=4000)  # A restart: the sequence goes on.
        self.assertEqual(again.seq, 30)
        rows = again.since(0)
        self.assertEqual(rows[-1]["seq"], 30)
        self.assertGreater(rows[0]["seq"], 1)  # The oldest segments were trimmed to the total.
        self.assertLessEqual(sum(os.path.getsize(os.path.join(folder, n)) for n in os.listdir(folder)), 4000 + 400)

    def test_burst_on_a_tight_or_fast_growing_memory(self):
        clock = [0.0]
        sampler = node_agent.Sampler(clock=lambda: clock[0])
        sampler.update_burst(0, {"used": 100, "limit": 1000, "oom_kill": 0})
        self.assertEqual(sampler.interval(), node_agent.INTERVAL)
        sampler.update_burst(10, {"used": 900, "limit": 1000, "oom_kill": 0})
        clock[0] = 10
        self.assertEqual(sampler.interval(), node_agent.BURST)
        clock[0] = 10 + node_agent.BURST_HOLD + 1
        self.assertEqual(sampler.interval(), node_agent.INTERVAL)

    def test_a_real_sample_has_the_parts(self):
        got = node_agent.Sampler(["/"]).sample()
        self.assertIn("pct", got["mem"])
        self.assertTrue(got["procs"])
        self.assertEqual(got["disk"][0]["path"], "/")

    def test_biggest_places_loose_files_and_mounts(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for i in range(4):
            (root / "tmp" / f"datalab-artifacts-{i:032x}.tar").parent.mkdir(parents=True, exist_ok=True)
            (root / "tmp" / f"datalab-artifacts-{i:032x}.tar").write_bytes(b"x" * 300_000)
        (root / "tmp" / "run1" / "deep").mkdir(parents=True)
        (root / "tmp" / "run1" / "deep" / "ckpt").write_bytes(b"x" * 500_000)
        (root / "home" / "me" / "cache").mkdir(parents=True)
        (root / "home" / "me" / "cache" / "blob").write_bytes(b"x" * 2_000_000)
        (root / "nas" / "big").mkdir(parents=True)
        (root / "nas" / "big" / "data").write_bytes(b"x" * 4_000_000)
        got = node_agent.biggest(str(root), mounts={str(root / "nas")})
        rows = {r["path"]: r for r in got["items"]}
        self.assertFalse(got["partial"])
        self.assertNotIn(str(root / "nas" / "big"), rows)  # Another mount: never entered.
        self.assertIn(str(root / "home" / "me" / "cache"), rows)  # Over a quarter of all: one level deeper.
        self.assertIn(str(root / "tmp" / "run1"), rows)
        loose = rows[str(root / "tmp")]
        self.assertEqual((loose["files"], loose["shape"]), (4, "datalab-artifacts-*.tar"))
        ticks = iter(range(0, 10 ** 6, 1000))
        many = root / "many"
        many.mkdir()
        for i in range(2100):
            (many / str(i)).touch()
        self.assertTrue(node_agent.biggest(str(root), budget=0.5, mounts=set(), clock=lambda: next(ticks))["partial"])

    def test_writers_and_disk_growth(self):
        disk = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, disk, True)
        sampler = node_agent.Sampler([disk])
        sampler.linux = True
        written = {11: 100, 12: 100}  # 12 writes a little: under WRITERS_MIN, not named.
        rows = [{"pid": 11, "start": 1, "comm": "pytest"}, {"pid": 12, "start": 1, "comm": "idle"}]
        with mock.patch.object(node_agent, "read_text", lambda name, limit=0: "write_bytes: %d\n" % written[int(name.split("/")[2])]), \
                mock.patch.object(node_agent, "read_environ_marker", lambda pid, timeout=0.5: "k5" if pid == 11 else None):
            sampler.count_writes(0, rows)
            self.assertEqual(sampler.writers, [])  # The first read is the baseline.
            written[11], written[12] = 100 + (3 << 30), 100 + (1 << 20)
            sampler.count_writes(60, rows)
        self.assertEqual(sampler.writers, [{"pid": 11, "comm": "pytest", "bytes": 3 << 30, "secs": 60, "task": "k5"}])
        total = 100 << 30
        counted = threading.Event()
        with mock.patch.object(node_agent, "biggest", lambda root: counted.set() or {"path": root, "items": []}):
            for minute in range(0, 70, 10):
                used = (80 << 30) + minute * (1 << 28)
                rows = [{"path": disk, "total": total, "free": total - used, "pct": round(100.0 * used / total, 1)}]
                sampler.disk_extras(minute * 60, rows)
            self.assertTrue(counted.wait(5))
        self.assertEqual(rows[0]["growth"], [{"secs": 3600, "bytes": 60 * (1 << 28)}])
        for _ in range(50):
            if not sampler.dirs_busy:
                break
            time.sleep(0.05)
        sampler.disk_extras(4300, rows)
        self.assertEqual(rows[0]["dirs"]["items"], [])
        quiet = [{"path": disk, "total": total, "free": total // 2, "pct": 50.0}]
        sampler.disk_extras(4400, quiet)
        self.assertNotIn("growth", quiet[0])  # Under DIRS_AT: the sample stays as it was.

    def test_hub_addresses_and_the_login_hook(self):
        self.assertEqual(node_agent.split_hub("[::1]:9"), ("::1", 9))
        self.assertEqual(node_agent.split_hub("h.example:10"), ("h.example", 10))
        text = "a\n" + node_agent.hook_text() + "b\n"
        self.assertEqual(node_agent.strip_hook(text), "a\nb\n")

    def test_python_39(self):
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "check_shim_compat.py"), str(AGENT)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


def free_port():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        probe.bind(("::1", 0))
        return probe.getsockname()[1]


class Runtime:
    def __init__(self):
        self.shutdown = threading.Event()
        self.lines = []

    def log(self, line):
        self.lines.append(line)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class GatewayTest(unittest.TestCase):
    """A gateway on the loopback address and a real node under a temporary HOME."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"  # make_context(self.root)'s state_dir.
        self.port = free_port()
        self.environ = {"COVEY_NODE_LISTEN": f"[::1]:{self.port}", "COVEY_NODE_ADDRESS": f"[::1]:{self.port}"}
        self.runtime = Runtime()
        self.gateway = node_gateway.Gateway(self.runtime, environ=self.environ)
        thread = threading.Thread(target=self.gateway.run, args=(make_context(self.root),), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.runtime.shutdown.set)
        for _ in range(200):
            if any("listening" in line for line in self.runtime.lines):
                break
            time.sleep(0.05)
        self.addCleanup(self.stop_node)

    def node(self, *args, timeout=30):
        env = dict(os.environ, HOME=str(self.home), COVEY_NODE_AUTOSTART="hook")
        return subprocess.run([sys.executable, str(self.home / ".covey" / "node" / "node.py"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def stop_node(self):
        state = self.home / ".covey" / "node" / "state.json"
        if state.exists():
            pid = json.loads(state.read_text()).get("pid")
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, TypeError):
                pass
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except (OSError, TypeError):
                    break
                time.sleep(0.1)
            else:
                self.fail(f"node {pid} still running")

    def get(self, path):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection(("::1", self.port), timeout=10) as raw, ctx.wrap_socket(raw) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return data.split(b"\r\n\r\n", 1)

    def wait(self, check, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.2)
        return False

    def test_a_node_joins_reports_and_is_removed(self):
        token = nodes.open_join(self.state, "box", "盒子")
        head, body = self.get(f"/join/{token}")
        self.assertIn(b"200 OK", head)
        self.assertIn(token.encode(), body)
        head, body = self.get("/node.py")
        self.assertEqual(body, AGENT.read_bytes())
        # The bootstrap as a device runs it: python3 reads it from stdin. It must not install a login hook in the
        # test: the HOME is a temporary one, so the hook goes there.
        env = dict(os.environ, HOME=str(self.home), COVEY_NODE_AUTOSTART="hook")  # No systemd unit from a test.
        out = subprocess.run([sys.executable, "-"], input=self.get(f"/join/{token}")[1], env=env, capture_output=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        answer = json.loads(out.stdout.decode().strip().splitlines()[-1])
        self.assertEqual((answer["alias"], answer["outcome"]), ("box", "started"))
        self.assertIn(node_agent.HOOK_BEGIN, (self.home / ".bashrc").read_text())
        self.assertTrue(self.wait(lambda: nodes.read_latest(self.state, "box").get("seq")))
        self.assertTrue(nodes.online(nodes.read_link(self.state, "box")))
        self.assertIn("pct", nodes.read_latest(self.state, "box")["mem"])
        # The used token shows the expired script.
        self.assertIn("已经用过".encode(), self.get(f"/join/{token}")[1])
        # A second ensure does not start a second node.
        self.assertEqual(json.loads(self.node("ensure").stdout)["outcome"], "running")
        # Removed: the node is told, takes its hook out and goes.
        nodes.revoke(self.state, "box")
        self.gateway.revoke_now("box")
        self.assertTrue(self.wait(lambda: not (self.home / ".covey" / "node").exists()))
        self.assertNotIn(node_agent.HOOK_BEGIN, (self.home / ".bashrc").read_text())

    def test_a_wrong_key_is_refused(self):
        alias, key = nodes.claim(self.state, nodes.open_join(self.state, "box", ""), {})
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection(("::1", self.port), timeout=10) as raw, ctx.wrap_socket(raw) as sock:
            sock.sendall(json.dumps({"k": "hello", "alias": alias, "key": key + "x"}).encode() + b"\n")
            self.assertEqual(json.loads(sock.makefile().readline())["err"], "auth")


class FakeMessenger:
    def __init__(self, ok=True):
        self.ok, self.sent, self.patched = ok, [], []

    def send_message(self, profile, chat, text, key, *, reply_to_message_id="", voice=None, card=None, on_card=None,
                     on_sent=None):
        self.sent.append((chat, reply_to_message_id, text, card))
        if self.ok and on_card:
            on_card("om_card")
        if self.ok and on_sent:
            on_sent("om_card")
        return self.ok

    def patch_card(self, profile, message_id, card):
        self.patched.append((message_id, card))
        return True


class Voice:
    @staticmethod
    def safe_key(value):
        return value


class Access:
    @staticmethod
    def notify_chat():
        return "oc_owner"


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ctx = make_context(self.root / "state")
        self.runtime = Runtime()
        self.runtime.access = Access()
        self.messenger = FakeMessenger()
        self.alerts = node_alerts.NodeAlerts(self.runtime, self.messenger, Voice(), environ={"COVEY_NODE_LISTEN": "1"})
        nodes.claim(self.ctx.state_dir, nodes.open_join(self.ctx.state_dir, "box", "盒子"), {})

    def event(self, kind, rule="mem", **more):
        folder = nodes.nodes_dir(self.ctx.state_dir) / "events"
        folder.mkdir(parents=True, exist_ok=True)
        event = {"alias": "box", "rule": rule, "kind": kind, "at": time.time(), "id": os.urandom(3).hex(),
                 "pct": 95, "used": 950, "limit": 1000, "top": [{"comm": "rg", "rss": 9 << 30}], **more}
        (folder / f"{time.time_ns()}.json").write_text(json.dumps(event))

    def test_one_card_then_redrawn_in_place(self):
        self.event("raise")
        self.alerts.round(self.ctx)
        self.assertEqual(len(self.messenger.sent), 1)
        chat, reply_to, text, card = self.messenger.sent[0]
        self.assertEqual((chat, reply_to), ("oc_owner", ""))
        self.assertIn("盒子 内存紧张", text)
        self.event("raise")  # Raised already: nothing more.
        self.event("clear")
        self.alerts.round(self.ctx)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(self.messenger.patched[0][0], "om_card")
        self.assertIn("已恢复", json.dumps(self.messenger.patched[0][1], ensure_ascii=False))

    def test_an_alert_at_the_top_level_is_the_bridges_own_one_in_a_topic_is_not(self):
        """2026-11-21 T622: a reply to a disk alert at the top level opened a topic on it, to wait for 开始. The alert sent there
        is kept as one of the Bridge's own cards (app/pins.py remember_notice): a reply to it is the butler's. One sent into a
        task's topic is that topic's, as before."""
        self.event("raise")
        self.alerts.round(self.ctx)
        self.assertTrue(pins.panel_message(self.ctx.state_dir, "oc_owner", "om_card"))
        upsert_job_record(self.ctx.state_dir, "k9", job_id=9, chat_id="oc_topic", reply_to_message_id="om_root", status="running")
        self.event("raise", rule="disk", top=[{"comm": "pytest", "rss": 9 << 30, "task": "k9"}])
        (self.ctx.state_dir / pins.NOTICES).unlink()
        self.alerts.round(self.ctx)
        self.assertEqual(self.messenger.sent[-1][:2], ("oc_topic", "om_root"))
        self.assertFalse(pins.panel_message(self.ctx.state_dir, "oc_topic", "om_card"))

    def test_cleared_before_it_went_is_never_sent(self):
        self.runtime.access = type("A", (), {"notify_chat": staticmethod(lambda: "")})()
        self.event("raise")
        self.alerts.round(self.ctx)  # No chat yet: kept pending.
        self.event("clear")
        self.runtime.access = Access()
        self.alerts.round(self.ctx)
        self.assertEqual(self.messenger.sent, [])

    def test_a_silent_node_raises_and_clears(self):
        now = time.time()
        nodes.write_link(self.ctx.state_dir, "box", connected=True, heard=now - node_alerts.OFFLINE_SECONDS - 5)
        self.alerts.round(self.ctx)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertIn("失联", self.messenger.sent[0][2])
        nodes.write_link(self.ctx.state_dir, "box", connected=True, heard=time.time())
        self.alerts.round(self.ctx)
        self.alerts.round(self.ctx)
        self.assertEqual(len(self.messenger.patched), 1)

    def test_the_hourly_limit(self):
        for i in range(node_alerts.PER_HOUR + 2):
            self.event("once", rule=f"oom{i}", count=1)
        self.alerts.round(self.ctx)
        self.assertEqual(len(self.messenger.sent), node_alerts.PER_HOUR)


class FakeGateway:
    address = "[::1]:9"
    tls = nodes.Tls(Path("/c"), Path("/k"), "ab" * 32, "AAAA")

    def __init__(self):
        self.revoked = []

    def revoke_now(self, alias):
        self.revoked.append(alias)


class PanelTest(unittest.TestCase):
    def setUp(self):
        from covey.app import node_panel
        self.node_panel = node_panel
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ctx = make_context(self.root)
        self.gateway = FakeGateway()
        self.started = []
        self.panel = node_panel.NodePanel(Runtime(), FakeMessenger(), Voice(), self.gateway, start=self.started.append)

    def click(self, kind, **value):
        return self.panel.act(self.ctx, {"kind": kind, **value}, {"form_value": value.pop("form", {})}, {})

    def test_add_without_ssh_shows_the_command(self):
        with mock.patch.object(self.node_panel, "form_fields", return_value={"node_name": "GPU1", "node_title": "训练机"}):
            changes, later = self.panel.act(self.ctx, {"kind": "bridge_nodes_add"}, {}, {})
        self.assertIsNone(later)
        self.assertEqual(changes["adding"]["alias"], "gpu1")
        self.assertIn("--pinnedpubkey 'sha256//AAAA' 'https://[::1]:9/join/", changes["adding"]["command"])
        self.assertEqual(nodes.active(self.ctx.state_dir), {})  # Only a join is open until the device comes.

    def test_add_over_ssh_runs_off_the_click(self):
        with mock.patch.object(self.node_panel, "form_fields",
                               return_value={"node_name": "gpu1", "node_ssh": "me@host -p 2222"}):
            changes, later = self.panel.act(self.ctx, {"kind": "bridge_nodes_add"}, {}, {})
        self.assertIn("正在经 ssh", changes["notice"])
        self.assertIsNotNone(later)
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok": true, "alias": "gpu1"}\n', "")
            notice = self.panel.ssh_join("gpu1", ["me@host", "-p", "2222"], "curl x | python3 -")
        self.assertIn("已装好", notice)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"])
        self.assertEqual(argv[-1], "curl x | python3 -")

    def test_ssh_addresses_that_would_run_something_here_are_refused(self):
        for bad in ("-oProxyCommand=sh host", "host -o LocalCommand=x", "'unclosed", "-p 22", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.node_panel.ssh_args(bad)
        self.assertEqual(self.node_panel.ssh_args("me@host -p 22 -i ~/.ssh/k"), ["me@host", "-p", "22", "-i", "~/.ssh/k"])

    def test_remove_asks_first_and_never_the_hub(self):
        nodes.claim(self.ctx.state_dir, nodes.open_join(self.ctx.state_dir, "box", ""), {})
        self.assertEqual(self.panel.act(self.ctx, {"kind": "bridge_nodes_remove", "alias": "box", "op": "ask"}, {}, {})[0],
                         {"confirm": "box"})
        changes, _ = self.panel.act(self.ctx, {"kind": "bridge_nodes_remove", "alias": "box", "op": "confirm"}, {}, {})
        self.assertIn("已移除", changes["notice"])
        self.assertEqual(self.gateway.revoked, ["box"])
        self.assertNotIn("box", nodes.active(self.ctx.state_dir))
        self.assertEqual(self.panel.act(self.ctx, {"kind": "bridge_nodes_remove", "alias": "local", "op": "confirm"}, {},
                                        {}), ({}, None))

    def test_facts_put_the_hub_first(self):
        nodes.ensure_local(self.ctx.state_dir)
        nodes.claim(self.ctx.state_dir, nodes.open_join(self.ctx.state_dir, "abox", "A 盒"), {})
        rows = self.node_panel.facts(self.ctx.state_dir, "addr")["rows"]
        self.assertEqual([(r["alias"], r["title"], r["local"]) for r in rows],
                         [("local", "本机", True), ("abox", "A 盒（abox）", False)])


class CardsTest(unittest.TestCase):
    def test_machine_lines(self):
        latest = {"cpu": {"pct": 12.0, "cores": 56.0}, "mem": {"pct": 90.0, "used": 9 << 30, "limit": 10 << 30},
                  "disk": [{"path": "/", "pct": 87.0}], "gpu": [{"util": 90}, {"util": 0}]}
        line, grey = node_cards.machine_lines("gpu1", {"connected": True, "heard": time.time()}, latest, online=True)
        self.assertEqual(line, "🟠 gpu1 · CPU 12% · 内存 90% · 盘 87% ⚠ · GPU 1/2 忙")
        self.assertIn("56 核", grey)
        line, _ = node_cards.machine_lines("gpu1", {"heard": time.time() - 3600}, latest, online=False)
        self.assertTrue(line.startswith("🔴 gpu1 · 失联 1 小时"))
        self.assertTrue(node_cards.machine_lines("x", {}, {}, online=False)[0].startswith("⚪"))

    def test_a_disk_card_shows_growth_writers_and_places(self):
        event = {"rule": "disk:/", "kind": "raise", "at": 0, "path": "/", "pct": 90.0, "free": 49 << 30,
                 "growth": [{"secs": 3600, "bytes": 2 << 30}, {"secs": 86400, "bytes": 45 << 30}],
                 "writers": [{"comm": "pytest", "bytes": 6 << 30, "secs": 60, "task": "k2"}],
                 "dirs": {"at": 0, "partial": True, "items": [
                     {"path": "/tmp", "bytes": 207 << 30, "files": 39, "shape": "datalab-artifacts-*.tar"},
                     {"path": "/tmp/claude-1000", "bytes": 43 << 30}]}}
        card = json.dumps(node_cards.alert_card(event, "本机", {"k2": "T488 · 任务 #4894"}), ensure_ascii=False)
        text = node_cards.alert_text(event, "本机", {"k2": "T488 · 任务 #4894"})
        for got in (card, text):
            self.assertIn("近 1 小时 +2.0 GiB，近 24 小时 +45.0 GiB", got)
            self.assertIn("按近 1 小时的速度约 24 小时 30 分钟写满", got)
            self.assertIn("pytest 写了 6.0 GiB · T488 · 任务 #4894", got)
            self.assertIn("/tmp 下散落的 39 个文件 207.0 GiB（多为 datalab-artifacts-", got)
            self.assertIn("没统计完", got)
            self.assertNotIn("占用最多", got)
        self.assertIn("（多为 datalab-artifacts-\\*.tar）", text)  # Markdown: the star is kept as a star.
        mem = {"rule": "mem", "kind": "raise", "at": 0, "pct": 95, "top": [{"comm": "rg", "rss": 9 << 30}]}
        self.assertIn("内存占用最多", node_cards.alert_text(mem, "本机", {}))


if __name__ == "__main__":
    unittest.main()
