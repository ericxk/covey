"""Shared test support: fixtures, fakes and the slow tier.

Nothing here is named ``test_*.py``, so ``discover`` never collects it. Test
modules import from here and never from each other.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
FIXTURES = TESTS / "fixtures"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SLOW = os.environ.get("BRIDGE_TESTS_SLOW") == "1"

# A shell inside a running Bridge or task runner carries its settings (card transport,
# data home, required mount ...). Tests never inherit them. ``discover`` imports this
# package before any test module, so this runs before covey reads them.
# The extension switches stay: a run with LARK_BRIDGE_EXTENSIONS='' checks the core without extensions.
_KEPT = {"LARK_BRIDGE_EXTENSIONS", "LARK_BRIDGE_EXTENSION_PATH", "COVEY_EXTENSIONS", "COVEY_EXTENSION_PATH"}
for _name in [name for name in os.environ if (name.startswith("COVEY_") or name.startswith("LARK_") and "BRIDGE" in name)
              and name not in _KEPT]:
    del os.environ[_name]
# Nor do they reach Feishu: a Bridge's startup would try a direct connection and add the host to this
# process's no_proxy (feishu/sdk.py lark_cli_direct). The tests of direct turn it on themselves.
os.environ["COVEY_FEISHU_DIRECT"] = "0"

# The enabled extensions' own tests: their tests/support/ files join this package (``from support.x import ...``), and
# their tests/ directories are where scripts/test_map.py finds their test modules.
from covey.core import extensions as _extensions  # noqa: E402

EXTENSION_TESTS = [d / "tests" for d in _extensions.package_dirs() if (d / "tests").is_dir()]
__path__.extend(str(d / "support") for d in EXTENSION_TESTS if (d / "support").is_dir())
sys.path.extend(str(d) for d in EXTENSION_TESTS)
if _extensions.enabled():
    _extensions.prepare_path(_extensions.search_path())  # Test modules import the extension before any Bridge module loads it.

# Every form status change the tests make is one FORM_TRANSITIONS lists (support/form_transitions.py).
from . import form_transitions  # noqa: E402

form_transitions.install()


# The gateway the tests run on is this stand-in, configured the way a deployment would in harnesses.json
# (core/gateways.py). An enabled extension's own gateways are left out of the tests' view, so the core's tests
# run the same with or without an extension.
ACME_TEST_GATEWAY = {
    "display": "Acme", "prefix": "acme/", "base_url": "https://gateway.example.com",
    "base_url_env": "ACME_BASE_URL", "key_env": "ACME_API_KEY", "key_file_env": "ACME_API_KEY_FILE",
    "key_file": "harness-bridge/acme-api-key", "headers": {"x-source": "bridge-tests"},
    "header_envs": {"x-source": "ACME_SOURCE"}, "quota_pattern": r"quota exceeded|daily limits? exceeded",
    "busy_hint": "一直如此时到 Acme 个人仪表盘看一下当日额度", "access": "Acme 个人密钥"}


def ensure_acme_gateway() -> None:
    """Add the test gateway and keep it through the Bridge's own ``gateways.configure`` (app/unified.py reads
    harnesses.json's gateways, none in the tests); the enabled extensions' gateways are not merged in."""
    from dataclasses import replace
    from covey.core import gateways
    if getattr(gateways.configure, "keeps_acme", False):
        return
    original = gateways.configure
    gateways.manifests = lambda: tuple(replace(e, gateways={}) for e in _extensions.manifests())

    def configure(rows):
        original({"acme": ACME_TEST_GATEWAY, **(rows or {})} if rows is None or isinstance(rows, dict) else rows)
    configure.keeps_acme = True
    gateways.configure = configure
    configure(None)


def acme():
    """The test gateway named acme (core/gateways.py)."""
    from covey.core import gateways
    ensure_acme_gateway()
    return gateways.named("acme")


def extension_enabled(package: str = "covey_internal") -> bool:
    return package in [extension.package for extension in _extensions.manifests()]


def requires_extension(package: str = "covey_internal"):
    """Mark a test that needs the company extension: it is skipped when that extension is not enabled
    (``LARK_BRIDGE_EXTENSIONS=''`` runs the core without it)."""
    return unittest.skipUnless(extension_enabled(package), f"needs the {package} extension (LARK_BRIDGE_EXTENSIONS)")


def require_extension(package: str = "covey_internal") -> None:
    """For a test module that imports the extension at the top: skips the whole module when it is not enabled."""
    if not extension_enabled(package):
        raise unittest.SkipTest(f"needs the {package} extension (LARK_BRIDGE_EXTENSIONS)")


def slow(reason: str):
    """Mark a test that starts real processes or waits on the real clock.

    It runs only with ``BRIDGE_TESTS_SLOW=1``. Use it only where a default-tier
    test already proves the same invariant without the real boundary.
    """
    return unittest.skipUnless(SLOW, "slow tier, set BRIDGE_TESTS_SLOW=1: " + reason)


def exec_tempdir() -> tempfile.TemporaryDirectory:
    """A temporary directory whose files may be executed directly.

    ``scripts/test_map.py`` puts test temp files on /dev/shm, which is mounted noexec, and
    names the previous temp dir in ``BRIDGE_TEST_EXEC_TMPDIR``. Tests that copy scripts or
    write fake CLIs and run them directly take their directory from here.
    """
    return tempfile.TemporaryDirectory(dir=os.environ.get("BRIDGE_TEST_EXEC_TMPDIR") or None)


def make_context(root: Path, profile: str | None = "unit", **overrides):
    """A BridgeContext whose state, history and sessions all live under ``root``."""
    from covey.core.harness import BridgeContext

    fields = dict(
        profile=profile,
        workspace_name="ws",
        default_workdir=root,
        workdir=root,
        state_dir=root / "state",
        history_path=root / "history.jsonl",
        session_path=root / "session",
        sessions_dir=root / "sessions",
        model="m",
        effort="max",
        timeout=0,
        backend_cmd=["backend"],
        skip_permissions=True,
    )
    fields.update(overrides)
    return BridgeContext(**fields)


def make_job(**fields):
    """A BridgeReplyJob with placeholder identity; any field, declared state included, can be given."""
    from covey.core.jobs import BridgeReplyJob

    init = {f.name for f in dataclasses.fields(BridgeReplyJob) if f.init}
    base = dict(job_id=1, user_text="task", chat_id="oc_chat", message_id="om_test", timeout=0)
    base.update({k: v for k, v in fields.items() if k in init})
    job = BridgeReplyJob(**base)
    for name, value in fields.items():
        if name not in init:
            setattr(job, name, value)
    return job


def quiet(app, logs: list | None = None) -> list:
    """Collect the Bridge log lines of ``app`` (anything holding its Runtime) in a list
    instead of printing them; every Harness of a Bridge shares that one Runtime."""
    logs = [] if logs is None else logs
    app.runtime.log_sink = logs.append
    return logs


ensure_acme_gateway()  # Before any test reads the gateways (core/gateways.py).
