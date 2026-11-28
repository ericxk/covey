"""Structure tests: they read the package source and the setup script, or start the entry
scripts in bin/ as real processes. scripts/test_map.py runs this module for every change.

- Every card action kind the code emits is in the one accepted table, and each one a topic's click
  can carry names one BridgeApp component method that takes it; every kind and form-button name
  prefix has exactly one taker (a click controller of app/unified.py or that method).
- Every CardKit write takes its sequence from card_sequence.reserve.
- Services, boot.json, setup scripts, Codex (workspace reader) and the isolated MASO
  interpreter start bin/ files by path, so unit imports alone cannot show that a moved
  module still runs.
- The backend table matches what is registered and what setup probes.
- Every repo path the docs name in backticks or link to exists (names the code lacks are only printed).
- Every environment variable and command-line option the code reads has its row in docs/configuration.md.
"""

import ast
import importlib
import importlib.util
import inspect
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from operator import attrgetter
from pathlib import Path

from support import REPO
from support.source_imports import imported_modules, imports, load_time_imports, module_name
from support.world import UnifiedCase

from covey.app import app as bridge_app
from covey.app.card_actions import ACTIONS, MOVED_CARDS, card_object
from covey.app.harness_switch import KINDS as SWITCH_KINDS
from covey.app.harnesses import components
from covey.cards.redaction_panel import ROTATE as ROTATE_KIND
from covey.cards.directory import DIRECTORY_FORM_PREFIX
from covey.cards.group_manager import KINDS as GROUP_KINDS
from covey.app.group_menu import KIND as GROUP_MENU_KIND
from covey.cards.butler_panel import KINDS as BUTLER_PANEL_KINDS
from covey.cards.redaction_panel import KINDS as REDACTION_KINDS
from covey.cards.node_panel import KINDS as NODE_PANEL_KINDS
from covey.cards.memory_panel import KINDS as MEMORY_PANEL_KINDS
from covey.cards.digest import MEMORY_KIND
from covey.cards.project_proposal import KINDS as TIDY_KINDS
from covey.cards.settings_hub import KINDS as SETTINGS_KINDS
from covey.cards.overview import DISMISS as OVERVIEW_DISMISS, UNDO as OVERVIEW_UNDO
from covey.cards.board import KINDS as BOARD_KINDS
from covey.cards.app_setup import SKIP as APP_SETUP_SKIP, START as APP_SETUP_START
from covey.cards.welcome import KIND as WELCOME_KIND
from covey.cards.batch_switch import KINDS as BATCH_SWITCH_KINDS
from covey.cards.web_login import KINDS as WEB_LOGIN_KINDS
from covey.cards import launch_config
from covey.cards.rewind import KINDS as REWIND_KINDS
from covey.cards.workspace_manager import MANAGER_PREFIX
from covey.cards.held_note import NOTE_KIND
from covey.cards.proxy_panel import KINDS as PROXY_PANEL_KINDS
from covey.cards.proxy_resend import RESEND as PROXY_RESEND, SUGGEST as PROXY_SUGGEST
from covey.cards.owner_send import (ASK as OWNER_SEND_ASK, DROP as OWNER_SEND_DROP, PICK as OWNER_SEND_PICK,
                                    SEND as OWNER_SEND)
from covey.cards.steer_receipts import CHECK_KIND, REQUEUE_KIND, WITHDRAW_KIND
from covey.core import extensions
from covey.core.harness import HARNESS_REGISTRY
from covey.core.jobs import JOB_STOP_ACTION_KIND
from covey.app.review import KIND as REVIEW_KIND
from covey.cards.collab_card import KIND as COLLAB_STOP
from covey.cards.review import FOLD_CLOSE_KIND
from covey.core.paths import BIN_DIR, entry_script, tool_root
from covey.core.roster import CORE_HARNESSES, HARNESSES, LIVE_QUESTIONS, NAMES, PAUSED_QUESTIONS, RETIRED


PACKAGE = REPO / "covey"
BIN = REPO / "bin"
# Not task-card buttons: the directory picker matches its own clicks first; bridge_restarted is a runner inbox message.
OTHER_ROUTES = {"bridge_directory", "bridge_restarted"}


class CardActionTableTest(unittest.TestCase):
    def test_every_emitted_kind_is_accepted(self):
        literal = re.compile(r"""["']kind["']\s*:\s*["'](bridge_[a-z_]+)["']""")
        emitted = {kind for path in PACKAGE.rglob("*.py") for kind in literal.findall(path.read_text())}
        constants = {JOB_STOP_ACTION_KIND, NOTE_KIND, CHECK_KIND, REQUEUE_KIND, WITHDRAW_KIND,
                     *PROXY_PANEL_KINDS, *BUTLER_PANEL_KINDS}
        self.assertEqual((emitted | constants) - OTHER_ROUTES - set(ACTIONS), set())

    def test_every_topic_kind_names_one_component_method(self):
        # The reverse: BridgeApp hands a topic's click to the method its kind names (card_handler), which takes
        # (ctx, event). The owner's cards the Bridge takes before topic routing name none (app/unified.py).
        before_routing = PROXY_PANEL_KINDS | GROUP_KINDS | BUTLER_PANEL_KINDS | SETTINGS_KINDS | {PROXY_RESEND, PROXY_SUGGEST, ROTATE_KIND}
        before_routing |= set(extensions.card_kinds())  # An enabled extension's cards (its features claim them).
        before_routing |= {OVERVIEW_DISMISS, OVERVIEW_UNDO}  # 手上的事's 这些都不用回答了 and its 撤销 (app/overview.py).
        before_routing |= BOARD_KINDS  # The 看板's 刷新 and 记为看过 (app/digest.py).
        before_routing |= {MEMORY_KIND}  # 整理记忆's 撤销 on the evening digest (app/digest.py).
        before_routing |= {"bridge_digest_remember"}  # An older evening card's 记下 / 不用, answered in words (app/digest.py).
        before_routing |= {APP_SETUP_START, APP_SETUP_SKIP}  # 飞书应用待补 (app/app_setup.py).
        before_routing |= WEB_LOGIN_KINDS  # 网页登录确认 (app/web_login.py).
        before_routing |= {WELCOME_KIND}  # 发这句 on the welcome card (app/welcome.py).
        before_routing |= {GROUP_MENU_KIND}  # A personal group's menu card (app/group_menu.py).
        before_routing |= REDACTION_KINDS  # The 设置 card's 脱敏 tab (app/redaction_panel.py).
        before_routing |= NODE_PANEL_KINDS  # The 设置 card's 设备 tab (app/node_panel.py).
        before_routing |= MEMORY_PANEL_KINDS  # The 设置 card's 记忆 tab (app/memory_panel.py).
        before_routing |= set(TIDY_KINDS)  # 整理专项's cards (app/project_tidy.py).
        before_routing |= {REVIEW_KIND}  # 独立复核 on an answer card (app/review.py).
        before_routing |= {COLLAB_STOP}  # 停止这次协作 on the 协作卡 (app/collab_cards.py).
        before_routing |= {FOLD_CLOSE_KIND}  # A click on the text of 这一轮中途的汇报 under a relay (app/fold_close.py).
        before_routing |= {OWNER_SEND, OWNER_SEND_DROP, OWNER_SEND_PICK,
                           OWNER_SEND_ASK}  # The 代发草稿 card (app/owner_send.py).
        before_routing |= BATCH_SWITCH_KINDS  # The 批量换配置 card (app/batch_switch.py).
        init = next(node for node in ast.walk(ast.parse((PACKAGE / "app" / "app.py").read_text()))
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        components = {node.targets[0].attr: getattr(bridge_app, node.value.func.id) for node in ast.walk(init)
                      if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
                      and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)}
        for kind, action in ACTIONS.items():
            with self.subTest(kind=kind):
                if kind in before_routing:
                    self.assertEqual(action.handler, "")
                    continue
                component, method = action.handler.split(".")
                handler = getattr(components[component], method)
                self.assertEqual(list(inspect.signature(handler).parameters), ["self", "ctx", "event"])


# The kinds the launch card's choices carry (app/model_picker.py), matched before the closed set.
LAUNCH_KINDS = {launch_config.HARNESS_ACTION, launch_config.PROVIDER_ACTION, launch_config.MODEL_ACTION,
                launch_config.BUDGET_ACTION, launch_config.WORK_MODE_ACTION, launch_config.REDACTION_ACTION,
                launch_config.WORKSPACE_ACTION, launch_config.MODE_ACTION, launch_config.PAGE_ACTION,
                launch_config.DEVICE_ACTION}
# Form buttons carry no kind: their name's prefix says whose they are. 开始任务 goes on to its topic's
# BridgeApp (submit_launch_form in app/launch_submit.py); the others to a controller.
FORM_PREFIXES = {MANAGER_PREFIX: None, launch_config.PICKER_PREFIX: None, DIRECTORY_FORM_PREFIX: None,
                 launch_config.SUBMIT_PREFIX: "launch.submit_launch_form"}


class ClickControllerTest(UnifiedCase):
    """Rule 8: a click the Bridge knows has exactly one taker. app/unified.py walks EARLY (by the
    event), then the closed set of kinds, then PANELS (by the value); what none claims goes on to its
    topic's BridgeApp, which takes it by the method ACTIONS names (or a launch form's 开始任务)."""

    def takers(self, event, form_handler=None):
        raw = card_object(event, "action_value")
        action = ACTIONS.get(raw.get("kind"))
        return ([name for name in self.app.EARLY if attrgetter(name)(self.app).matches(event)]
                + [type(f).__name__ for f in self.app.extension_features if hasattr(f, "matches") and f.matches(event)]
                + [name for name in self.app.PANELS if attrgetter(name)(self.app).matches(raw)]
                + [taker for taker in (action and action.handler, form_handler) if taker])

    def test_every_kind_has_one_taker(self):
        kinds = set(ACTIONS) | LAUNCH_KINDS | SWITCH_KINDS | REWIND_KINDS | OTHER_ROUTES - {"bridge_restarted"}
        for kind in sorted(kinds):
            with self.subTest(kind=kind):
                self.assertEqual(len(self.takers({"action_value": {"kind": kind}})), 1)

    def test_every_form_button_prefix_has_one_taker(self):
        for prefix, handler in FORM_PREFIXES.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(len(self.takers({"action_name": prefix + "0" * 24}, handler)), 1)

    def test_every_emitted_name_prefix_is_listed(self):
        # What the test above walks is typed by hand: a prefix the code starts emitting has to be in it. Whole
        # literals and an f-string's head, as a *_PREFIX constant or inline. Other names ("submit", "find")
        # carry a kind in their value, which test_every_emitted_kind_is_accepted reads.
        emitted = set()
        for path in PACKAGE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                head = node.values[0] if isinstance(node, ast.JoinedStr) and node.values else node
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    emitted.add(head.value)
        moved = {prefix for _, prefix in MOVED_CARDS}  # card_actions.py: what a feature that was taken out once emitted.
        self.assertEqual({text for text in emitted if re.fullmatch(r"bridge_[a-z_]+_", text)} - moved, set(FORM_PREFIXES))

    def test_the_managers_choice_on_the_launch_card_is_the_managers(self):
        # 管理工作区 is an option of the launch card's workspace choice: the earlier controller takes it.
        event = {"action_value": {"kind": launch_config.WORKSPACE_ACTION}, "option": launch_config.MANAGE_OPTION}
        self.assertEqual(self.takers(event), ["workspace_manager", "launch_model_picker"])

    def test_every_table_entry_claims_with_the_same_arguments(self):
        for name in self.app.EARLY + self.app.PANELS:
            with self.subTest(controller=name):
                claim = attrgetter(name)(self.app).claim
                self.assertEqual(list(inspect.signature(claim).parameters), ["ctx", "event", "raw", "owner"])


class CardSequenceScanTest(unittest.TestCase):
    def test_every_cardkit_write_takes_its_sequence_from_reserve(self):
        computed = re.compile(r"sequence\s*\+=|sequence['\"]?\]?\s*=\s*max\(|max\(int\(time\.time\(\)\)")
        found = [f'{path.relative_to(PACKAGE)}:{n}' for path in PACKAGE.rglob('*.py')
                 if 'backends' not in path.parts and path.name != 'card_sequence.py'
                 for n, line in enumerate(path.read_text().splitlines(), 1) if computed.search(line)]
        self.assertEqual(found, [])


ENTRIES = {
    "covey_app_setup.py": "covey.service.app_check",
    "covey_doctor.py": "covey.service.doctor",
    "covey_butler_mcp.py": "covey.core.butler_mcp",
    "covey_memguard.py": "covey.service.memguard",
    "covey_pairing.py": "covey.service.pairing",
    "covey_service.py": "covey.service.manager",
    "covey_service_install.py": "covey.service.install",
    "covey_install.py": "covey.service.installer",
    "covey_install_browser.py": "covey.feishu.install_browser",
    "covey_maintenance.py": "covey.core.maintenance",
    "covey_paths.py": "covey.core.paths",
    "covey_app_console.py": "covey.feishu.app_console",
    "covey_workspaces.py": "covey.core.workspaces",
    "covey_memory.py": "covey.core.memory_search",
    "covey_repo.py": "covey.core.repomap",
    "agy_as.py": "covey.backends.agy.account_homes",
    "covey_ledger.py": "covey.app.owner_ledger",
    "covey_inbound.py": "covey.core.inbound_ledger",
    "covey_devices.py": "covey.service.device_check",
    "covey_plugins.py": "covey.service.plugins",
    "covey_feishu_web.py": "covey.feishu.web_client",
    "covey_maso_runtime.py": "covey_internal.maso.runtime",  # compat shim for the company extension (extensions/covey_internal/)
    "covey_workspace_reader.py": "covey.backends.codex.workspace_reader",
    "covey_tool_guard.py": "covey.backends.claude.tool_guard",
    "covey_look_write.py": "covey.core.look_write",
}


# The names before the rename (2026-11-14; covey_main_mcp.py 2026-11-16): forwarders, what service manifests, boot hooks, crontabs and scripts call.
OLD_ENTRIES = {
    "bridge_app_setup.py": "covey_app_setup.py", "bridge_doctor.py": "covey_doctor.py",
    "bridge_main_mcp.py": "covey_butler_mcp.py", "covey_main_mcp.py": "covey_butler_mcp.py", "bridge_memguard.py": "covey_memguard.py",
    "bridge_pairing.py": "covey_pairing.py", "bridge_service.py": "covey_service.py",
    "bridge_service_install.py": "covey_service_install.py", "lark_install_browser.py": "covey_install_browser.py",
    "lark_bridge_maintenance.py": "covey_maintenance.py", "lark_bridge_paths.py": "covey_paths.py",
    "lark_app_console.py": "covey_app_console.py", "lark_bridge_workspaces.py": "covey_workspaces.py",
    "lark_feishu_web.py": "covey_feishu_web.py", "lark_maso_runtime.py": "covey_maso_runtime.py",
    "lark_workspace_reader.py": "covey_workspace_reader.py",
    "lark_harness_bridge.py": "covey_bridge.py", "lark_task_runner.py": "covey_runner.py",
}


# Entries that only run with the company extension (the public export leaves them out).
EXTENSION_ENTRIES = {"covey_maso_runtime.py", "lark_maso_runtime.py"}



def present(table):
    """The entries this checkout has: the public export has no extensions/ and leaves the extension's entries out."""
    return {k: v for k, v in table.items() if k not in EXTENSION_ENTRIES or (REPO / "extensions").exists()}


# Packages a layer of covey/ must not import (repo review R3; service since round 2 B1).
LAYERS = {"backends": {"app", "feishu", "cards", "service"}, "cards": {"app", "feishu", "service"},
          "core": {"app", "cards", "feishu", "service"}, "feishu": {"app", "cards"}, "service": {"app", "cards"}}

def foreign_backends(source, package):
    """What a module of backends/<X> (``package``) imports from covey besides backends/<X>,
    backends/gateway and core."""
    own = package.split(".")[2]
    return [(module, line) for module, line in imported_modules(source, package)
            if module.split(".")[0] == "covey" and not (module.split(".")[1:2] == ["core"] or (
                module.split(".")[1:2] == ["backends"] and module.split(".")[2:3] in ([own], ["gateway"])))]


def above_its_layer(source, package):
    """What a module of ``package`` imports from a layer LAYERS bars its own layer from."""
    barred = LAYERS.get("".join(package.split(".")[1:2]), ())
    return [(module, line) for module, line in imported_modules(source, package)
            if module.split(".")[0] == "covey" and "".join(module.split(".")[1:2]) in barred]


def run_entry(name, *args, python=sys.executable, env=None):
    # A foreign cwd and no PYTHONPATH: only the script's own directory may make
    # the package importable, exactly as for services and backend launchers.
    with tempfile.TemporaryDirectory() as cwd:
        return subprocess.run([python, "-B", str(BIN / name), *args], cwd=cwd, capture_output=True, text=True,
                              timeout=60, env=env or {"PATH": os.environ.get("PATH", ""), "HOME": cwd})


class EntrypointTests(unittest.TestCase):
    def test_every_entry_runs_its_module_as_main(self):
        shims = {}
        for path in BIN.glob("*.py"):
            calls = [n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Call)
                     and getattr(n.func, "attr", "") == "run_module"]
            if calls:
                self.assertEqual([kw.arg for kw in calls[0].keywords], ["run_name", "alter_sys"], path.name)
                shims[path.name] = calls[0].args[0].value
        # The MASO runtime's entries are the extension's: here a shell that forwards to it, in a release the extension's own file.
        for name in EXTENSION_ENTRIES & shims.keys():
            self.assertEqual(shims.pop(name), ENTRIES[name], name)
        self.assertEqual(shims, {k: v for k, v in present(ENTRIES).items() if k not in EXTENSION_ENTRIES})
        # The bridge starts task runners itself; neither is a module shim.
        # The old entry names stay as forwarders to the new ones (manifests, boot hooks and crontabs name them).
        forwards = {}
        for name in present(OLD_ENTRIES):
            tree = ast.parse((BIN / name).read_text())
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and hasattr(n.func, "attr")]
            self.assertEqual([n.func.attr for n in calls if n.func.attr == "run_path"], ["run_path"], name)
            forwards[name] = [n.args[0].value for n in calls if n.func.attr == "with_name"][0]
        self.assertEqual(forwards, present(OLD_ENTRIES))
        self.assertEqual({p.name for p in BIN.glob("*.py")},
                         {*present(ENTRIES), *present(OLD_ENTRIES), "covey_bridge.py", "covey_runner.py"})
        for module in present(ENTRIES).values():
            if module.split(".")[0] != "covey" and not extensions.enabled():
                continue  # An extension's compat shim; the extension is off.
            self.assertIsNotNone(importlib.util.find_spec(module), module)

    def test_importing_an_entry_does_not_run_it(self):
        # Deployment helpers used to import names such as covey_service.register by module name.
        modules = "covey_service, covey_maso_runtime" if (BIN / "covey_maso_runtime.py").exists() else "covey_service"
        result = subprocess.run([sys.executable, "-B", "-c", f"import {modules}; print('ok')"],
                                cwd=BIN, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (0, "ok\n"), result.stderr[-2000:])

    def test_entries_start_from_their_historical_paths(self):
        for name in sorted(ENTRIES):
            if name in EXTENSION_ENTRIES and not (BIN / name).exists():
                continue  # The extension's entry: not in the public export (a release gets it from the extension).
            with self.subTest(name):
                result = run_entry(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertTrue(result.stdout.startswith("usage: " + name), result.stdout[:200])
        result = run_entry("covey_bridge.py", "--harness", "unified", "--help")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("--harness-config", result.stdout)
        for old, new in sorted(OLD_ENTRIES.items()):  # The old names reach the same entries.
            args = ("--harness", "unified", "--help") if old == "lark_harness_bridge.py" else ("--help",)
            if old == "lark_task_runner.py":
                args = ("--help",)
            if old in EXTENSION_ENTRIES and not (BIN / old).exists():
                continue
            with self.subTest(old):
                result = run_entry(old, *args)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn("usage:", result.stdout)

    def test_the_maso_entry_forwards_to_the_extension_and_without_it_says_so(self):
        source = BIN / "covey_maso_runtime.py"
        if not source.exists() or "init_globals" not in source.read_text():
            self.skipTest("no forwarding shell here (no extension, or a release with the extension's own entry)")
        with tempfile.TemporaryDirectory() as empty:
            result = run_entry("covey_maso_runtime.py", "--help",
                               env={"PATH": os.environ.get("PATH", ""), "HOME": empty, "COVEY_EXTENSION_PATH": empty})
        self.assertEqual(result.returncode, 3, result.stderr[-500:])
        self.assertIn("not installed", result.stderr)

    def test_entries_import_their_own_package_not_the_cwd_one(self):
        # The service runs with PYTHONPATH=":..." (an empty entry is the cwd) and its
        # cwd is a checkout that has its own covey.
        with tempfile.TemporaryDirectory() as cwd:
            decoy = Path(cwd) / "covey"
            decoy.mkdir()
            (decoy / "__init__.py").write_text("raise SystemExit('imported the cwd package')\n")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": cwd, "PYTHONPATH": ":"}
            for name, args in (("covey_paths.py", ["--help"]),
                               ("covey_bridge.py", ["--harness", "unified", "--help"])):
                result = subprocess.run([sys.executable, "-B", str(BIN / name), *args], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, name + ": " + result.stderr[-500:])

    def test_launchers_resolve_entry_paths(self):
        self.assertEqual(BIN_DIR, BIN.resolve())
        self.assertEqual(tool_root(), REPO.resolve())
        for name in (*present({"covey_maso_runtime.py": 0}), "covey_workspace_reader.py", "covey_service.py"):
            self.assertTrue(entry_script(name).is_file(), name)
        for bad in ("covey/core/paths.py", "../bin/covey_service.py", "covey_service"):
            with self.assertRaises(ValueError):
                entry_script(bad)

    def test_package_inits_stay_empty(self):
        # The MASO interpreter imports a handful of modules; an __init__ must not pull in the rest.
        for init in (REPO / "covey").rglob("__init__.py"):
            self.assertEqual(init.read_text(), "", init)

    def test_layers_import_only_downwards(self):
        # Not even inside a function: the Manager and the maintenance command load core alone, the
        # backends run in task runners (rule 7), cards/ only renders. Nor under TYPE_CHECKING (round 2 D):
        # those do not run, but a type a layer names from above it is a dependency all the same.
        found = [f"{path.relative_to(PACKAGE)}:{line} {module}" for path in sorted(PACKAGE.rglob("*.py"))
                 for module, line in above_its_layer(path.read_text(), module_name(path.parent))]
        self.assertEqual(found, [])

    def test_above_its_layer_sees_every_import_form(self):
        source = "\n".join(("import covey", "from covey import app, core", "import json",
                            "from covey.app import runtime", "from covey.feishu.sdk import call",
                            "import covey.service.doctor", "from ..app import task_card",
                            "from ..core import state", "from . import elements",
                            "def f():\n    from covey import feishu",
                            "if TYPE_CHECKING:\n    from covey.app.runtime import Runtime"))
        self.assertCountEqual([module for module, _ in above_its_layer(source, "covey.cards")], [
            "covey.app", "covey.app.runtime", "covey.feishu.sdk",
            "covey.service.doctor", "covey.app.task_card", "covey.feishu",
            "covey.app.runtime"])

    def test_a_backend_imports_only_its_own_modules_the_gateway_and_core(self):
        # One Harness's code does not reach into another's (round 2 B took a backend off Codex's gateway catalog and
        # UUID pattern); what they share is in backends/gateway (the model gateway) or core. Relative imports,
        # imports inside functions and under TYPE_CHECKING count too.
        found = [f"{path.relative_to(PACKAGE)}:{line} {module}" for path in sorted((PACKAGE / "backends").rglob("*.py"))
                 if path.parent != PACKAGE / "backends"
                 for module, line in foreign_backends(path.read_text(), module_name(path.parent))]
        self.assertEqual(found, [])

    def test_foreign_backends_sees_every_import_form(self):
        source = "\n".join(("import covey.backends.codex.gateway", "from covey.backends import codex",
                            "from covey.backends.codex.sessions import UUID_RE", "from ..claude import models",
                            "from . import home", "from covey.backends import gateway, dsh",
                            "from covey.core import harness", "import covey.core.state",
                            "def f():\n    from covey.app import detached",
                            "if TYPE_CHECKING:\n    from covey.cards.streaming import CardRuntime",
                            "from ...core import roster", "from ...app.unified import x", "import json"))
        self.assertCountEqual([module for module, _ in foreign_backends(source, "covey.backends.dsh")], [
            "covey.backends.codex.gateway", "covey.backends.codex",
            "covey.backends.codex.sessions", "covey.backends.claude", "covey.app.detached",
            "covey.cards.streaming", "covey.app.unified"])

    @staticmethod
    def import_cycles(prefix=""):
        """The groups of modules that import each other, ``prefix`` limiting the graph to a package; inside
        functions too."""
        graph = {}
        for path in PACKAGE.rglob("*.py"):
            graph[module_name(path)] = {
                f"{module}.{name}" if name and (PACKAGE.parent / module.replace(".", "/") / f"{name}.py").exists()
                else module for module, name, _ in imports(path) if module.startswith("covey.")}
        graph = {name: {dep for dep in deps if dep.startswith(prefix)} for name, deps in graph.items()
                 if name.startswith(prefix)}
        reach = {}
        for start in graph:
            seen, todo = set(), [start]
            while todo:
                todo += [dep for dep in graph.get(todo.pop(), ()) if dep not in seen and not seen.add(dep)]
            reach[start] = seen
        return sorted({tuple(sorted(other for other in reach[name] if name in reach.get(other, ())))
                       for name in graph if name in reach[name]})

    def test_the_only_import_cycles_are_the_known_ones(self):
        # Inside functions too. Round 2 B1 broke app.detached <-> app.policy_guard; a new cycle fails here.
        self.assertEqual(self.import_cycles(), [
            ("covey.core.state", "covey.core.store"),
            ("covey.feishu.web_client", "covey.feishu.web_service")])

    def test_app_has_no_import_cycle(self):
        # The app's modules, function-level imports counted: every strongly connected group is one module.
        self.assertEqual(self.import_cycles("covey.app."), [])

    def test_no_module_imports_another_ones_private_names(self):
        # Repo review R3: app/ took feishu.media's and launch's underscored helpers; they are public now.
        found = [f"{path.relative_to(PACKAGE)}:{line} {module}.{name}" for path in sorted(PACKAGE.rglob("*.py"))
                 for module, name, line in imports(path) if name and name.startswith("_")
                 and module.startswith("covey.") and module != module_name(path)]
        self.assertEqual(found, [])

    def test_app_builds_no_card_json(self):
        # Repo review R8: card JSON is drawn in cards/, so a diff outside it does not change a card
        # (rule 9). Reading one (e.get("tag")) is fine.
        found = [f"{path.relative_to(PACKAGE)}:{n}" for path in sorted((PACKAGE / "app").glob("*.py"))
                 for n, line in enumerate(path.read_text().splitlines(), 1) if re.search(r"""["']tag["']\s*:""", line)]
        self.assertEqual(found, [])

    def test_no_object_calls_a_private_method_of_the_questions_facade(self):
        # Repo review R5: its parts call each other (questions.native, .checkpoints, .display) and its
        # public methods; the private forwarding methods are gone.
        found = [f"{path.relative_to(PACKAGE)}:{n}" for path in sorted(PACKAGE.rglob("*.py"))
                 for n, line in enumerate(path.read_text().splitlines(), 1) if re.search(r"\bquestions\._[a-z]", line)]
        self.assertEqual(found, [])

    def test_the_proxy_parts_import_only_downwards_and_the_web_side_takes_no_record_lock(self):
        # Repo review R9: Proxy holds its parts; the web side (sending as the owner, the tool's login) imports
        # none of the others but the send side, the commands and the take-in only its reads, the topics none,
        # and the web side runs the tool outside Proxy.lock (rule 1).
        app = PACKAGE / "app"
        parts = {f"covey.app.{name}" for name in ("proxy", "proxy_commands", "proxy_send", "proxy_panel",
                                                           "proxy_resend", "proxy_intake", "proxy_topics")}
        send = {"covey.app.proxy_send"}
        allowed = {"proxy_send": set(), "proxy_commands": send, "proxy_intake": send,
                   "proxy_topics": set()}
        for name, may in allowed.items():
            found = {module for module, _, _ in imports(app / f"{name}.py") if module in parts}
            self.assertEqual(found, may, name)
        sender_files = [d / name for d in extensions.package_dirs() for name in ("proxy_sender.py", "proxy_login.py")
                        if (d / name).exists()]
        for path in (app / "proxy_send.py", *sender_files):  # No extension, no sender files (the public export).
            self.assertNotRegex(path.read_text(), r"\bself\.proxy\.lock\b", path.name)


class RosterTest(unittest.TestCase):
    def test_every_backend_module_defines_its_spec_store_and_registration(self):
        for name, harness in HARNESSES.items():
            module = importlib.import_module(harness.module)
            spec = getattr(module, name.upper().replace("-", "_") + "_SPEC")
            self.assertEqual(spec.name, name)
            self.assertTrue(callable(getattr(importlib.import_module(harness.store_module or harness.module),
                                             harness.store)))
            HARNESS_REGISTRY.create(name)  # Raises for a backend that never registered.

    def test_the_harnesses_with_uuid_sessions_share_core_s_pattern(self):
        # Round 2 D: claude/sessions.py compiled a copy of it (the same object only through re's cache).
        # Round 3: the backends import it from core/harness.py, no alias in between. The expression is written once.
        from covey.backends.claude import completion
        from covey.core.harness import UUID_RE
        uuids = {name for name, harness in HARNESSES.items() if getattr(importlib.import_module(harness.module),
                 name.upper().replace("-", "_") + "_SPEC").session_pattern is UUID_RE}
        self.assertEqual(uuids, {"claude", "codex", "agy"})
        self.assertIs(completion.UUID_RE, UUID_RE)
        self.assertEqual([str(path.relative_to(PACKAGE)) for path in sorted(PACKAGE.rglob("*.py"))
                          if UUID_RE.pattern in path.read_text()], ["core/harness.py"])

    def test_question_modes_do_not_overlap(self):
        self.assertEqual(LIVE_QUESTIONS & PAUSED_QUESTIONS, set())
        self.assertEqual(NAMES[0], "codex")

    def test_retired_harnesses_are_out_of_the_roster(self):
        self.assertEqual(set(RETIRED) & set(HARNESSES), set())
        # Their old topics and records keep the name they showed.
        self.assertEqual(RETIRED, {"neeko": "Neeko", "codex-w": "Codex-W", "claude-w": "Claude-W", "neeko-next": "Neeko Next",
                                   "doubao": "豆包", "kiro": "Kiro", "pi": "Pi"})
        self.assertFalse((BIN / "acme_bridge_exec.sh").exists())  # The Acme wrappers' launcher is gone.
        self.assertFalse((PACKAGE / "backends" / "neeko_next").exists())  # 2026-11-02.
        self.assertFalse((PACKAGE / "backends" / "pi").exists())  # T766: the owner, 2026-11-25: Pi 没啥用了.
        # TRAE and 豆包 went for good (the owner, 2026-11-05: 后续没有这个供应商了), and with them what only they used.
        for gone in ("backends/traex", "backends/doubao", "backends/messages", "backends/claude/traex.py",
                     "backends/claude/doubao.py", "backends/claude/gateways.py"):
            self.assertFalse((PACKAGE / gone).exists(), gone)
        self.assertFalse(list(BIN.glob("lark_doubao_*")) + list(BIN.glob("lark_trae_*")))

    def test_setup_probes_every_cli_backend(self):
        script = (REPO / "scripts/setup_harness_bridge.sh").read_text()
        probed = re.search(r"for cli in ([a-z\- ]+); do", script).group(1).split()
        # The others are an enabled extension's, which prepares its own runtime.
        self.assertEqual(set(probed) | {name for e in extensions.manifests() for name in e.harnesses}, set(NAMES))

    def test_providers_are_not_harnesses_and_each_runs_in_one(self):
        # The owner (2026-11-03): Acme is where models come from, never a Harness of its own.
        from covey.core.providers import PROVIDERS, RETIRED_PREFIXES, providers
        gateway_harnesses = {p.harness for p in providers()} - {p.harness for p in PROVIDERS}
        self.assertLessEqual(gateway_harnesses, {"codex", "dsh"})  # A gateway's providers (core/gateways.py).
        self.assertLessEqual({p.harness for p in providers()}, set(NAMES))
        PROVIDERS = providers()
        # A removed provider's prefix is no live provider's.
        self.assertFalse([p for p in PROVIDERS if p.prefix and p.prefix in RETIRED_PREFIXES])
        for harness in set(NAMES) - {"dsh"}:  # One own provider a Harness, its models without a prefix.
            self.assertEqual(len([p for p in PROVIDERS if p.harness == harness and not p.prefix]), 1, harness)


# 环境变量与选项一览 (docs/configuration.md): what the code reads from the environment and its command lines.
INVENTORY_DOC, INVENTORY_HEADING = "docs/configuration.md", "## 环境变量与选项一览"
# The system's own variables, read only to pass them on or to find a home: not listed.
SYSTEM_ENV = {"HOME", "PATH", "PYTHONPATH", "XDG_CONFIG_HOME", "NO_PROXY", "no_proxy"}
# Reads whose name is not in the code: (file, the name's expression).
DYNAMIC_ENV = {
    ("covey/core/listen.py", "name"),  # COVEY_NODE_LISTEN's $NAME: a variable the owner names.
    ("covey/app/runner.py", "spelling"),  # Pops both spellings of RUNNER_PYTHONPATH, read just above.
    ("covey/core/app_manifest.py", "name"),  # switch(env, name): its callers' literals.
    ("covey/core/process_env.py", "name"),  # env_flag's own read; its callers are collected.
    ("covey/feishu/sdk.py", "n"), ("covey/feishu/sdk.py", "name"),  # no_proxy and NO_PROXY.
    ("extensions/covey_internal/maso/runtime.py", "key"),  # The MASO SDK's variables it removes.
    ("covey/core/extensions.py", "name"),  # unused_env: RETIRED_ENV's, read only to name them.
    ("covey/core/ignored_env.py", "name"),  # ignored_env: IGNORED_ENV's, read only to name them.
    ("covey/core/switches.py", "spelling"),  # shown: the spellings of KNOWN's names (each has its row), read from a manifest's environment.
    ("covey/service/installer.py", "key"),  # default_switches: INSTALL_SWITCHES' literals, each with its own row.
    # A model gateway's variables are named by its configuration (core/gateways.py), not by the code.
    ("covey/core/gateways.py", "self.base_url_env"), ("covey/core/gateways.py", "variable"),
    ("covey/core/gateways.py", "self.key_file_env"),
    ("covey/backends/gateway/connection.py", "gateway.key_env"),
    ("covey/core/same_account.py", "gateway.key_env"), ("covey/core/same_account.py", "gateway.base_url_env"),
    ("covey/core/same_account.py", "variable"),  # Codex's config.toml names its key's variable (env_key).
    ("covey/core/dropped_config.py", "name"),  # dropped: IGNORED_ENV's, read only to name them.
}


def module_constants(tree):
    """{name: value} of the module-level string constants in ``tree``."""
    return {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for target in node.targets if isinstance(target, ast.Name)}


def read_inventory():
    """({variable: {file}}, {option: {file}}, [unresolved reads]) of covey/ and bin/.

    Variables: os.environ.get/pop/setdefault/[...]/in, os.getenv, env_flag, cenv.get/has (core/env.py), and .get/.setdefault/[...] of a
    copy named ``env`` or ``environ``; the name a literal, a constant of the module or a unique one of another,
    ``HarnessSpec.env(SUFFIX)`` or Voice's f"{env_prefix}_SUFFIX" (each Harness's prefix). Options: every
    ``--name`` given to add_argument."""
    trees = {path: ast.parse(path.read_text()) for path in sorted([*PACKAGE.rglob("*.py"), *BIN.glob("*.py")])
             if path != PACKAGE / "core" / "env.py" and path.name not in EXTENSION_ENTRIES}  # An extension's entry (a release's bin/) is its README's.  # core/env.py is the reader itself: it names no variable.
    everywhere = {}
    for tree in trees.values():
        for name, value in module_constants(tree).items():
            everywhere.setdefault(name, set()).add(value)
    unique = {name: values.pop() for name, values in everywhere.items() if len(values) == 1}
    prefixes = sorted(components(name)[0].env_prefix for name in CORE_HARNESSES)  # An extension's variables are its README's.
    variables, options, unresolved = {}, {}, []

    def environ(node, copies=False):
        if isinstance(node, ast.IfExp):
            return environ(node.body, copies) or environ(node.orelse, copies)
        if isinstance(node, ast.Attribute):
            return node.attr == "environ"
        return isinstance(node, ast.Name) and (node.id == "environ" or copies and node.id == "env")

    for path, tree in trees.items():
        rel, local = str(path.relative_to(REPO)), module_constants(tree)

        def names(arg):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return [arg.value]
            key = arg.id if isinstance(arg, ast.Name) else arg.attr if isinstance(arg, ast.Attribute) else None
            if key in local or key in unique:
                return [local.get(key, unique.get(key))]
            if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "env"
                    and len(arg.args) == 1 and isinstance(arg.args[0], ast.Constant)):
                return [f"{prefix}_{arg.args[0].value}" for prefix in prefixes]
            if (isinstance(arg, ast.JoinedStr) and len(arg.values) == 2 and isinstance(arg.values[1], ast.Constant)
                    and isinstance(arg.values[0], ast.FormattedValue) and ast.unparse(arg.values[0].value) == "self.env_prefix"):
                return [prefix + arg.values[1].value for prefix in prefixes]
            return None

        for node in ast.walk(tree):
            arg, strict = None, True
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "add_argument":
                    for each in node.args:
                        if isinstance(each, ast.Constant) and str(each.value).startswith("--"):
                            options.setdefault(each.value, set()).add(rel)
                    continue
                if isinstance(func, ast.Attribute) and func.attr in ("get", "pop", "setdefault") and environ(func.value, True):
                    arg, strict = node.args[0], environ(func.value)
                elif isinstance(func, ast.Attribute) and func.attr == "getenv" or getattr(func, "id", "") == "env_flag":
                    arg = node.args[0]
                elif (isinstance(func, ast.Attribute) and func.attr in ("get", "has") and isinstance(func.value, ast.Name)
                      and func.value.id == "cenv"):  # core/env.py: COVEY_<X>, else the old LARK_BRIDGE_<X>.
                    arg = node.args[0]
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and environ(node.value, True):
                arg, strict = node.slice, environ(node.value)
            elif (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                  and environ(node.comparators[0])):
                arg = node.left
            if arg is None:
                continue
            found = names(arg)
            if found is None:
                if strict and (rel, ast.unparse(arg)) not in DYNAMIC_ENV:
                    unresolved.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
                continue
            for name in found:
                variables.setdefault(name, set()).add(rel)
    return variables, options, unresolved


def inventory_rows():
    """{name in a first cell's backticks: the text of its rows} of the table under INVENTORY_HEADING."""
    lines = (REPO / INVENTORY_DOC).read_text().splitlines()
    rows = {}
    for line in lines[lines.index(INVENTORY_HEADING) + 1:]:
        if line.startswith("#"):
            break
        if line.startswith("| ") and not line.startswith("| ---"):
            for name in re.findall(r"`([^`]+)`", line.split(" | ")[0]):
                rows[name] = rows.get(name, "") + line
    return rows


# Core modules the MCP server (bin/covey_butler_mcp.py) loads, with what they import: rule 7, only the standard library
# and core modules that are themselves only that. The project's memory and library are not loaded there yet; they are kept to
# the same rule so a lead's tool can be (core/project_memory.py, core/project_library.py).
MCP_CORE = ("covey.core.butler_mcp", "covey.core.butler", "covey.core.projects", "covey.core.project_memory",
            "covey.core.project_library")


class McpCoreTest(unittest.TestCase):
    def test_what_the_mcp_server_imports_is_the_standard_library_and_core(self):
        seen, todo, found = set(), list(MCP_CORE), []
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            for module, imported, line in imports(PACKAGE.parent / (name.replace(".", "/") + ".py")):
                if module in ("covey", "covey.core") and imported:
                    module = f"{module}.{imported}"
                if module.startswith("covey."):
                    if module.startswith("covey.core."):
                        todo.append(module)
                    else:
                        found.append(f"{name}:{line} {module}")
                elif module.split(".")[0] not in sys.stdlib_module_names:
                    found.append(f"{name}:{line} {module}")
        self.assertEqual(found, [])
        self.assertLessEqual(set(MCP_CORE), seen)
        self.assertIn("covey.core.projects", seen)


class InventoryTest(unittest.TestCase):
    def test_every_variable_and_option_the_code_reads_has_its_row_naming_who_reads_it(self):
        variables, options, unresolved = read_inventory()
        self.assertEqual(unresolved, [], "a read whose name the inventory cannot tell: name it or add it to DYNAMIC_ENV")
        rows = inventory_rows()
        missing = []
        for name, files in sorted({**{k: v for k, v in variables.items() if k not in SYSTEM_ENV}, **options}.items()):
            row = rows.get(name)
            if row is None:
                missing.append(f"{name}: no row")
                continue
            missing += [f"{name}: its row does not name {f}" for f in sorted(files)
                        if f.removeprefix("covey/") not in row]
        self.assertEqual(missing, [])

    def test_every_name_in_the_table_is_read(self):
        variables, options, _ = read_inventory()
        self.assertEqual(sorted(set(inventory_rows()) - set(variables) - set(options)), [])

    def test_settings_are_read_through_core_env_so_the_old_names_work_too(self):
        """T778: core/memory.py read COVEY_MEMORY from os.environ alone, and a service whose Manager starts it with the
        manifest's names (LARK_BRIDGE_MEMORY) had no memory at all. A setting is read through core/env.py get, which knows
        both spellings; these are not a manifest's (set by the node's own hook, or by Covey for the Harness process) or read both names themselves."""
        direct = re.compile(r"""(?:(?:os\.environ|environ)\)?(?:\.get\(|\[)|os\.getenv\()\s*(?:["']COVEY_|[A-Z_]*ENV\b)""")
        allowed = {("covey/core/node_agent.py", "COVEY_NODE_AUTOSTART"), ("covey/service/browser_setup.py", "COVEY_BROWSER_EXECUTABLE"),
                   ("covey/backends/claude/tool_guard.py", "COVEY_HOME")}  # Set by core/home.py env for the process itself.
        found = []
        for path in sorted(PACKAGE.rglob("*.py")):
            name = str(path.relative_to(REPO))
            if name == "covey/core/env.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if direct.search(line) and not any(name == where and word in line for where, word in allowed):
                    found.append(f"{name}:{number}: {line.strip()}")
        self.assertEqual(found, [], "read it with core/env.py get (cenv.get(NAME, default, environ))")

# What the docs name in backticks (AGENTS.md, README.md, docs/*.md; not docs/history/, which records the past).
DOC_FILES = ["AGENTS.md", "README.md", *sorted(str(p.relative_to(REPO)) for p in (REPO / "docs").glob("*.md"))]
# A backticked path whose first part is one of these is the repo's; others (`proxy/web/profile`, `jobs/`,
# `harnesses.json`) are under a deployer's data root.
DOC_ROOTS = {"covey", "bin", "docs", "scripts", "tests", "integrations", "skills"}
# AGENTS.md's code map names paths under covey/ and covey/backends/ without the prefix; these
# also start data-root paths (`cards/.locks`) and model IDs (`claude/claude-opus-5`), so only a file name with a
# code suffix or a directory with its trailing / counts.
DOC_SHORT_ROOTS = {"app", "core", "cards", "feishu", "service", "backends", "claude", "codex", "maso", "dsh", "gateway"}
CODE_SUFFIXES = (".py", ".sh", ".json", ".js", ".html", ".css", ".md")
# Paths the docs name on purpose that are not in this repo.
DOC_UNTRACKED_PATHS = {
    "lark_bridge_boot_hook.py",  # Written into the user's site-packages by the service install.
}
# Backticked names from outside this repo (Feishu, CardKit, Claude Code, the environment) or defined in the docs.
DOC_EXTERNAL_NAMES = {"SERVICE_ROOT", "partial_update_setting", "stream_idle_timeout_ms", "DEEPSEEK_API_KEY",
                      "offline_access", "OnP2CardActionTrigger", "card_content", "LARK_SEND_AS_USER_CHAT_NAME"}
FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"\]\(([^)\s]+)\)")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")


def doc_lines(name):
    """(line number, text) of a doc's lines outside fenced code blocks."""
    fenced = False
    for number, line in enumerate((REPO / name).read_text().splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, line


def doc_path(token):
    """The repo path a backticked word names, or None: a word with a / under DOC_ROOTS or DOC_SHORT_ROOTS, or
    a bare *.py or *.sh; without its trailing /, #anchor or :line, and never a template (<…>, *, $…) or an
    absolute path."""
    token = re.sub(r":\d+(-\d+)?$", "", token.strip("\"'()[],;:。，、").split("#")[0])
    if not token or re.search(r"[<>*$~{}…|=]|://", token) or token.startswith(("/", ".", "-")):
        return None
    first = token.split("/")[0]
    if "/." in token:  # A hidden directory (`cards/.locks/`) is a data root's.
        return None
    if "/" not in token:
        return token if token.endswith((".py", ".sh")) else None
    if first in DOC_ROOTS or first in DOC_SHORT_ROOTS and (token.endswith("/") or token.endswith(CODE_SUFFIXES)):
        return token.rstrip("/")
    return None


DOC_EXPORT_PACKED = {"tests/golden/cards.json"}


def doc_path_exists(doc, path, basenames):
    if path in DOC_UNTRACKED_PATHS:
        return True
    if any((base / path).exists() for base in (REPO, PACKAGE, PACKAGE / "backends", (REPO / doc).parent)):
        return True
    if path in DOC_EXPORT_PACKED and (REPO / (path + ".xz")).exists():
        return True  # The public export packs the 1 MB golden file.
    if path == "docs/history" or path.startswith("docs/history/"):
        return (REPO / "LICENSE").exists()  # The public export (it has LICENSE) leaves out the dated design records.
    return "/" not in path and path in basenames  # A code-map file named under its package's line.


def heading_anchor(heading):
    """GitHub's anchor of a Markdown heading: lower case, punctuation dropped, spaces as hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def doc_anchors(path):
    anchors, seen = set(), {}
    for _, line in doc_lines(str(path.relative_to(REPO))):
        if heading := re.match(r"#{1,6}\s+(.*)", line):
            anchor = heading_anchor(heading.group(1))
            anchors.add(f"{anchor}-{seen[anchor]}" if anchor in seen else anchor)
            seen[anchor] = seen.get(anchor, 0) + 1
    return anchors


class DocReferenceTest(unittest.TestCase):
    """A file the docs name in backticks or link to exists, and a linked heading too, so a moved or deleted
    module fails here instead of leaving AGENTS.md's code map stale. A backticked name the code does not have
    is only printed: about half of such hits are words from outside (DOC_EXTERNAL_NAMES)."""

    def test_every_path_the_docs_name_exists(self):
        tracked = [p for sub in ("covey", "bin", "scripts", "tests") for p in (REPO / sub).rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts]
        basenames = {p.name for p in tracked}
        missing, checked = [], 0
        for doc in DOC_FILES:
            for number, line in doc_lines(doc):
                for span in CODE_SPAN.finditer(line):
                    for word in span.group(2).split():
                        path = doc_path(word)
                        checked += bool(path)
                        if path and not doc_path_exists(doc, path, basenames):
                            missing.append(f"{doc}:{number} {path}")
                for target in LINK.findall(line):
                    if "://" in target or target.startswith("mailto:"):
                        continue
                    checked += 1
                    file, _, anchor = target.partition("#")
                    linked = ((REPO / doc).parent / file) if file else REPO / doc
                    if not linked.exists():
                        missing.append(f"{doc}:{number} {target}")
                    elif anchor and linked.suffix == ".md" and anchor not in doc_anchors(linked.resolve()):
                        missing.append(f"{doc}:{number} {target} (no such heading)")
        self.assertEqual(missing, [], "\n".join(missing))
        self.assertGreater(checked, 500)  # The docs' paths were found at all (2026-11-05: 872).

    def test_names_the_docs_give_are_reported_when_the_code_lacks_them(self):
        tracked = [p for sub in ("covey", "bin", "scripts", "tests") for p in (REPO / sub).rglob("*")
                   if p.is_file() and p.suffix in (".py", ".sh", ".json", ".js", ".html", ".css", "")
                   and "__pycache__" not in p.parts]
        words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", "\n".join(p.read_text(errors="replace") for p in tracked)))

        def checked(part):  # snake_case of five or more characters, or CamelCase: an identifier, not prose.
            return ("_" in part and len(part) >= 5) or re.match(r"[A-Z][a-z]+[A-Z]", part)

        unknown = []
        for doc in DOC_FILES:
            for number, line in doc_lines(doc):
                for span in CODE_SPAN.finditer(line):
                    name = span.group(2).strip()
                    if not IDENTIFIER.fullmatch(name):
                        continue
                    parts = name.removesuffix("()").split(".")
                    lacking = [p for p in parts if checked(p) and p not in DOC_EXTERNAL_NAMES and p not in words
                               and not (p.endswith("_") and any(w.startswith(p) for w in words))]
                    if lacking:
                        unknown.append(f"{doc}:{number} {name}")
        if unknown:
            print("\nnames in the docs the code does not have (not a failure):\n  " + "\n  ".join(unknown))


class CodeMapTest(unittest.TestCase):
    """AGENTS.md's module table is what scripts/code_map.py generates from the modules' docstrings, so every
    covey module has its row and a moved or deleted one leaves none behind."""

    def test_the_module_table_matches_the_docstrings(self):
        spec = importlib.util.spec_from_file_location("code_map", REPO / "scripts/code_map.py")
        code_map = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(code_map)
        text = (REPO / "AGENTS.md").read_text()
        self.assertIn(code_map.BEGIN, text)
        self.assertEqual(code_map.current(text), code_map.generate(),
                         "AGENTS.md's code map is stale: python3 -B scripts/code_map.py --write")
        listed = set(re.findall(r"^\| `([^`]+\.py)` \|", code_map.generate(), re.M))
        modules = {str(p.relative_to(PACKAGE)) for p in PACKAGE.rglob("*.py")
                   if p.name != "__init__.py" and "__pycache__" not in p.parts}
        self.assertEqual(sorted(modules - listed), [])



def load_concepts():
    spec = importlib.util.spec_from_file_location("concepts", REPO / "scripts/concepts.py")
    concepts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(concepts)
    return concepts


class ConceptFreezeTest(unittest.TestCase):
    """AGENTS.md 概念清单: every setting, card button kind, Bridge tool, Harness, provider and glossary word of the core is
    on tests/golden/concepts.json (scripts/concepts.py), so a new concept is added in sight, and in a 收敛期 (the list's
    ``freeze``, none now) only with who allowed it."""

    def test_the_core_has_the_concepts_the_list_keeps_and_no_other(self):
        concepts = load_concepts()
        listed = concepts.load()
        text = concepts.report(listed, concepts.compare(listed, concepts.current()), concepts.frozen(listed))
        self.assertEqual(text, "", "\n" + text)

    def test_every_approved_concept_says_who_allowed_it_and_why(self):
        concepts = load_concepts()
        for entry in concepts.load()["approved"]:
            self.assertIn(entry.get("kind"), concepts.KINDS, entry)
            for field in ("name", "by", "why"):
                self.assertTrue(str(entry.get(field) or "").strip(), (field, entry))

    def test_in_the_freeze_write_refuses_a_new_concept_and_after_it_takes_it(self):
        concepts = load_concepts()
        listed = {"freeze": {"since": "2026-11-25", "until": "2026-12-09"},
                  "concepts": {kind: ["old"] for kind in concepts.KINDS},
                  "approved": [{"kind": "tools", "name": "allowed", "by": "T1", "why": "w"}]}
        now = {kind: ["old"] for kind in concepts.KINDS} | {"tools": ["allowed", "new", "old"], "terms": []}
        self.assertTrue(concepts.frozen(listed, concepts.datetime.date(2026, 12, 9)))
        self.assertFalse(concepts.frozen(listed, concepts.datetime.date(2026, 12, 10)))
        diff = concepts.compare(listed, now)
        self.assertEqual(diff["added"], {"tools": ["new"]})
        self.assertEqual(diff["removed"], {"terms": ["old"]})
        self.assertIn("先问主人", concepts.report(listed, diff, True))
        out, refused = concepts.written(listed, now, True)
        self.assertEqual(refused, ["tools:new"])
        self.assertEqual((out["concepts"]["tools"], out["concepts"]["terms"]), (["old"], []))
        self.assertEqual([entry["name"] for entry in out["approved"]], ["allowed"])
        out, refused = concepts.written(listed, now, False)
        self.assertEqual((refused, out["concepts"]["tools"], out["approved"]), ([], ["allowed", "new", "old"], []))

class StandardLibraryTest(unittest.TestCase):
    """The core needs nothing but Python (pyproject.toml lists no dependencies) and CI installs nothing else: what a
    module of the core, its scripts or its tests imports as it loads is the standard library or this tree's own.
    A test module that imported Pillow at its top failed to import on CI, and its one stand-in case made every
    full run refuse a "stale map" (4754 cases there, 4769 here)."""

    OWN = {"covey", "harness_bridge", "support", "__future__"}

    def test_a_package_from_elsewhere_is_imported_where_its_absence_is_handled(self):
        found = []
        for path in sorted(p for root in (PACKAGE, BIN, REPO / "scripts", REPO / "tests") for p in root.rglob("*.py")):
            gated = path.parent == REPO / "tests" and re.search(r"^require_extension\(", path.read_text(), re.M)
            for module, line in load_time_imports(path):
                top = module.split(".")[0]
                if top in self.OWN or top in sys.stdlib_module_names or top.startswith("test_") or gated:
                    continue  # A module that skips whole without the extension imports it after require_extension().
                found.append(f"{path.relative_to(REPO)}:{line} {module}")
        self.assertEqual(found, [], "import it in a try that catches ImportError (and skip the tests that need it: "
                                    "unittest.skipUnless), or inside the function that uses it")

    def test_an_import_that_handles_its_absence_is_not_named(self):
        source = textwrap.dedent("""\
            import json
            import PIL
            from lark_oapi import Client
            try:
                from PIL import Image
            except ImportError:
                Image = None
            if TYPE_CHECKING:
                import yaml
            def draw():
                import playwright
            """)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.py"
            path.write_text(source)
            self.assertEqual(load_time_imports(path), [("json", 1), ("PIL", 2), ("lark_oapi", 3)])


class OldestPythonTest(unittest.TestCase):
    """pyproject.toml asks for Python 3.10; the tests run on a newer one, which parses more."""

    def test_no_f_string_reuses_an_enclosing_quote(self):
        # T444 A1: f"…{name or "本" + noun}…" parses only from Python 3.12 (PEP 701); on 3.10 and 3.11 importing
        # app/butler_topics.py failed and with it every butler tool.
        import io
        import tokenize
        if not hasattr(tokenize, "FSTRING_START"):
            self.skipTest("this Python's own parser rejects them")
        prefix = "rRbBuUfF"
        found = []
        for path in sorted(p for root in (PACKAGE, BIN, REPO / "scripts", REPO / "tests") for p in root.rglob("*.py")):
            enclosing = []
            for token in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
                if token.type == tokenize.FSTRING_END:
                    enclosing.pop()
                    continue
                if token.type not in (tokenize.STRING, tokenize.FSTRING_START):
                    continue
                body = token.string.lstrip(prefix)
                quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
                if any(quote[0] == outer[0] and (len(outer) == 1 or quote == outer) for outer in enclosing):
                    found.append(f"{path.relative_to(REPO)}:{token.start[0]}")
                if token.type == tokenize.FSTRING_START:
                    enclosing.append(quote)
        self.assertEqual(found, [])


class TidyPartsTest(unittest.TestCase):
    """ProjectTidy (app/project_tidy.py) is made of parts it builds in ``__init__`` (docs: app/project_tidy.py): none inherits from
    another, and each part imports only the parts below it, so reading one never needs the ones above."""

    ORDER = ("tidy_facts", "tidy_state", "tidy_words", "tidy_apply", "tidy_proposals", "tidy_run", "tidy_clicks",
             "tidy_web", "project_tidy")  # Bottom to top.
    BELOW = {"tidy_facts": (), "tidy_state": (), "tidy_words": ("tidy_facts",), "tidy_apply": ("tidy_facts",),
             "tidy_proposals": ("tidy_facts", "tidy_words", "tidy_apply"),
             "tidy_run": ("tidy_facts", "tidy_state", "tidy_words", "tidy_apply", "tidy_proposals"),
             "tidy_clicks": ("tidy_facts", "tidy_state", "tidy_words", "tidy_apply", "tidy_proposals", "tidy_run"),
             "tidy_web": ("tidy_facts", "tidy_state", "tidy_words", "tidy_clicks"),
             "project_tidy": ("tidy_facts", "tidy_state", "tidy_words", "tidy_apply", "tidy_proposals",
                              "tidy_run", "tidy_clicks", "tidy_web")}

    def tidy_imports(self, name):
        found = set()
        for module, imported, _ in imports(PACKAGE / "app" / f"{name}.py"):
            for candidate in (module, f"{module}.{imported}" if imported else module):
                parts = candidate.split(".")
                if parts[:2] == ["covey", "app"] and len(parts) > 2 and parts[2] in self.ORDER:
                    found.add(parts[2])
        return found

    def test_the_parts_import_only_the_parts_below_them(self):
        self.assertEqual(set(self.BELOW), set(self.ORDER))
        for name, below in self.BELOW.items():
            self.assertEqual(self.tidy_imports(name) - set(below), set(), name)

    def test_no_part_inherits_from_another_and_none_is_a_mixin(self):
        import ast
        for name in self.ORDER:
            tree = ast.parse((PACKAGE / "app" / f"{name}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self.assertEqual([ast.unparse(base) for base in node.bases if ast.unparse(base) != "Exception"], [],
                                     f"{name}.{node.name}")


class AssistExitTest(unittest.TestCase):
    """辅助模型 (app/assist.py): the Bridge's background model calls have one way out. No other module picks a Harness for
    them or starts one: none calls app/track_judge.py's ``tools`` (gone), ``ask``, ``judge`` or its process helpers (it
    may import its ``Failed``), and nothing but app/track_judge.py (and a backend wrapping its own one-question module)
    calls a backend's ``ask_once``. So 不用模型 and a named Harness hold everywhere. The standby of 分流
    (app/track_standby.py) is gone (T898): no module may bring it back under that name."""

    EXIT = {"app/assist.py", "app/track_judge.py"}
    JUDGE = {"tools", "available", "ask", "ask_native", "judge", "warm", "run", "claude_once", "standby_target"}

    def sources(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            rel = path.relative_to(PACKAGE).as_posix()
            if rel in self.EXIT:
                continue
            yield rel, ast.parse(path.read_text(), filename=str(path))

    def test_no_module_but_the_exit_calls_the_judge_or_the_standby(self):
        found = []
        for rel, tree in self.sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id == "track_judge" and node.attr in self.JUDGE:
                        found.append(f"{rel}:{node.lineno} track_judge.{node.attr}")
                    if node.value.id == "track_standby" and node.attr != "Failed":
                        found.append(f"{rel}:{node.lineno} track_standby.{node.attr}")
                if isinstance(node, ast.ImportFrom) and node.module in ("covey.app.track_judge", "covey.app.track_standby"):
                    for alias in node.names:
                        if alias.name != "Failed" and (node.module.endswith("standby") or alias.name in self.JUDGE):
                            found.append(f"{rel}:{node.lineno} from {node.module} import {alias.name}")
                if isinstance(node, ast.ImportFrom) and node.module == "covey.app":
                    for alias in node.names:
                        if alias.name == "track_standby":
                            found.append(f"{rel}:{node.lineno} imports track_standby")
        self.assertEqual(found, [], "后台模型调用只能经 app/assist.py（docs/configuration.md 辅助模型）")

    def test_only_the_judge_and_a_backend_s_own_wrapper_call_ask_once(self):
        found = []
        for rel, tree in self.sources():
            if rel.startswith("backends/"):
                continue  # A backend's ask_once method hands the question to its own judge module.
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "ask_once":
                    found.append(f"{rel}:{node.lineno}")
        self.assertEqual(found, [])

    def test_the_features_taken_out_stay_out(self):
        """T898: the standby, the real-time merge and the patrol are gone; 看看 and 整理记忆 ask no model. T914: the evening
        candidates, the nightly harvest and the Harnesses' memory import are gone."""
        from covey.app import assist
        for rel in ("app/track_standby.py", "app/tidy_realtime.py", "app/patrol.py", "app/patrol_judge.py",
                    "app/patrol_rules.py", "core/patrol.py", "app/memory_candidates.py", "app/memory_harvest.py",
                    "core/harness_memory.py"):
            self.assertFalse((PACKAGE / rel).exists(), rel)
        self.assertEqual(set(assist.FEATURES), {"track", "tidy", "backfill", "learn"})
        self.assertEqual(assist.SHOWN, ("track", "tidy", "learn"))
        for rel in ("app/stuck_check.py", "app/memory_tidy.py"):
            with self.subTest(rel=rel):
                self.assertNotIn("assist.ask(", (PACKAGE / rel).read_text())

    def test_the_exit_checks_the_harness_again_before_it_starts_one(self):
        source = (PACKAGE / "app" / "assist.py").read_text()
        self.assertIn("assist refused harness=", source)
        self.assertIn("def allowed(", source)

    def test_every_feature_is_asked_through_the_exit(self):
        from covey.app import assist
        users = {"track": "app/butler_tracks.py", "tidy": "app/tidy_run.py", "backfill": "app/project_sorter.py",
                 "learn": "app/memory_learn.py"}
        self.assertEqual(set(users), set(assist.FEATURES))
        for feature, rel in users.items():
            with self.subTest(feature=feature):
                self.assertIn("assist", (PACKAGE / rel).read_text())

    # WAVE12 W2-D 包 D: every question says whether it carries a redacting topic's words (``redacted``, a keyword without a
    # default), and one that does is refused at the exit. Read as the AST of the package and of the extensions a release
    # carries (``gated_sources``), through any alias of the module or of the function.
    ASK = {"covey.app.assist": {"ask"}}

    @classmethod
    def unsaid_redacted(cls, sources):
        return [f"{rel}:{node.lineno}" for rel, source in sources for _, _, node in module_calls(source, cls.ASK)
                if not any(keyword.arg == "redacted" for keyword in node.keywords)]

    def test_every_question_says_whether_it_is_a_redacting_topics_and_the_exit_needs_it(self):
        from covey.app import assist
        self.assertEqual(self.unsaid_redacted(gated_sources()), [],
                         "assist.ask 要显式带 redacted=（脱敏话题的内容不外发，WAVE12 W2-D 包 D）")
        callers = {rel for rel, source in gated_sources() if module_calls(source, self.ASK)}
        self.assertLessEqual({"covey/app/project_sorter.py", "covey/app/memory_learn.py"}, callers)  # The scan sees them.
        parameter = inspect.signature(assist.ask).parameters["redacted"]
        self.assertEqual((parameter.kind, parameter.default), (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.empty))
        from covey.core import assist as records
        self.assertIn("redacted", records.NOT_ASKED)
        self.assertIn("redacted", records.WORDS)

    def test_a_question_without_redacted_is_caught_through_any_alias(self):
        sources = [("covey/app/a.py", textwrap.dedent('''
            from covey.app import assist
            from covey.app import assist as helper
            from covey.app.assist import ask as put
            import covey.app.assist as exit_

            def f(kw):
                assist.ask("tidy", "s", "d", topics=1, limit=1, cwd=1, redacted=False)
                assist.ask("tidy", "s", "d", topics=1, limit=1, cwd=1, **kw)
                helper.ask("tidy", "s", "d", topics=1, limit=1, cwd=1)
                put("tidy", "s", "d", topics=1, limit=1, cwd=1)
                exit_.ask("tidy", "s", "d", topics=1, limit=1, cwd=1, redacted=True)
                other.ask("tidy")
        '''))]
        self.assertEqual(self.unsaid_redacted(sources), ["covey/app/a.py:9", "covey/app/a.py:10", "covey/app/a.py:11"])


# WAVE12 C11 gates G6 and G9 (W1-0 sets them up with every caller of today listed; W1-D and W1-B take the lists down). A
# source of the package, and of the extensions a release carries beside it (extensions/), is read as its AST: an alias of
# a module (``from covey.core import memstore as ms``) or of a function (``from covey.core.memstore import add as put``)
# is still found. A listed caller that is gone fails too, so a list only shrinks.
def gated_sources():
    roots = [PACKAGE] + ([REPO / "extensions"] if (REPO / "extensions").is_dir() else [])
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            yield path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8")


class _Calls(ast.NodeVisitor):
    """The calls of one module to the functions of ``modules`` ({dotted module: {function, …}}) and to the names of
    ``classes`` ({dotted module: {class, …}}), each as (qualified name of the enclosing def or class, "module.function",
    the Call node), through whatever alias the module gave them."""

    def __init__(self, modules, classes=None):
        self.targets = {**{m: set(f) for m, f in modules.items()}}
        for module, names in (classes or {}).items():
            self.targets.setdefault(module, set()).update(names)
        self.module_alias = {}  # Local name → dotted module.
        self.name_alias = {}  # Local name → (dotted module, function).
        self.stack = []
        self.found = []

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in self.targets and alias.asname:
                self.module_alias[alias.asname] = alias.name

    def visit_ImportFrom(self, node):
        for alias in node.names:
            dotted = f"{node.module}.{alias.name}"
            if dotted in self.targets:
                self.module_alias[alias.asname or alias.name] = dotted
            elif node.module in self.targets and alias.name in self.targets[node.module]:
                self.name_alias[alias.asname or alias.name] = (node.module, alias.name)

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Name) and node.value.id in self.module_alias:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.module_alias[target.id] = self.module_alias[node.value.id]
        self.generic_visit(node)

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _target(self, func):
        if isinstance(func, ast.Name) and func.id in self.name_alias:
            return self.name_alias[func.id]
        if isinstance(func, ast.Attribute):
            owner = func.value
            dotted = self.module_alias.get(owner.id) if isinstance(owner, ast.Name) else None
            if dotted is None and isinstance(owner, (ast.Attribute, ast.Name)):
                dotted = ast.unparse(owner) if ast.unparse(owner) in self.targets else None
            if dotted and func.attr in self.targets[dotted]:
                return dotted, func.attr
        return None

    def visit_Call(self, node):
        target = self._target(node.func)
        if target:
            self.found.append((".".join(self.stack) or "<module>", f"{target[0].rsplit('.', 1)[-1]}.{target[1]}", node))
        self.generic_visit(node)


def module_calls(source, modules, classes=None):
    visitor = _Calls(modules, classes)
    visitor.visit(ast.parse(source))
    return visitor.found


def counted(keys):
    found = {}
    for key in keys:
        found[key] = found.get(key, 0) + 1
    return found


def against(found, listed):
    """(new callers, listed callers that are gone or fewer): both empty when the list says what the code does."""
    new = {key: count for key, count in found.items() if count > listed.get(key, 0)}
    stale = {key: count for key, count in listed.items() if found.get(key, 0) < count}
    return new, stale


class MemoryWriteTest(unittest.TestCase):
    """G6 (WAVE12 C11): core/memstore.py's writes and principles.save are called only by core/memory_gate.py and the store's
    own modules; core/memory_gate.py's Proven is made only by app/memory_proof.py. Today's callers are listed (``WRITES``)
    until W1-D moves them behind core/memory_gate.py write and revise."""

    STORE = {"covey.core.memstore": {"add", "add_decision", "supersede", "archive", "settle", "set_status"},
             "covey.core.principles": {"save"}}
    PROOF = {"covey.core.memory_gate": {"Proven"}}
    OWN = {"covey/core/memory_gate.py", "covey/core/memstore.py", "covey/core/memory_review.py", "covey/core/migrate.py"}
    MAKER = "covey/app/memory_proof.py"
    WRITES = {  # "<file>::<enclosing def>::<module>.<function>": calls.
        "covey/app/butler_knowledge.py::memory_revise::memstore.add": 1,
        "covey/app/butler_knowledge.py::memory_revise::memstore.add_decision": 1,
        "covey/app/butler_knowledge.py::memory_revise::memstore.supersede": 1,
        "covey/app/butler_knowledge.py::memory_save::memstore.add": 1,
        "covey/app/butler_knowledge.py::memory_save::memstore.add_decision": 1,
        "covey/app/butler_tools.py::principle_save::principles.save": 1,
        "covey/app/knowledge_collect.py::_remember::memstore.add": 1,
        "covey/app/memory_collect.py::_take::memstore.add": 1,
        "covey/app/memory_collect.py::promote::principles.save": 1,
        "covey/app/memory_panel.py::MemoryPanel.put_away::memstore.archive": 1,
        "covey/app/project_context.py::remember_answer::memstore.add_decision": 1,
        "covey/app/project_tools.py::remember_new::memstore.add": 1,
        "covey/app/project_tools.py::revise_decision::memstore.settle": 1,
        "covey/app/project_tools.py::revise_decision::memstore.supersede": 1,
        "covey/app/question_dedup.py::settle::memstore.add": 1,
        "covey/app/web_memory.py::_add_one::principles.save": 1,
        "covey/app/web_memory.py::_baseline_decide::memstore.archive": 1,
        "covey/app/web_memory.py::_write_item::memstore.add": 1,
        "covey/app/web_memory.py::_write_item::memstore.add_decision": 1,
        "covey/app/web_memory.py::edit_v43::memstore.add": 1,
        "covey/app/web_memory.py::edit_v43::memstore.add_decision": 1,
    }

    @classmethod
    def writes(cls, sources):
        keys = []
        for rel, source in sources:
            if rel not in cls.OWN:
                keys += [f"{rel}::{scope}::{name}" for scope, name, _ in module_calls(source, cls.STORE)]
        return counted(keys)

    @classmethod
    def proofs(cls, sources):
        return [f"{rel}:{node.lineno}" for rel, source in sources if rel != cls.MAKER
                for _, _, node in module_calls(source, {}, cls.PROOF)]

    def test_only_the_gate_and_the_store_write_memory_and_the_callers_of_today_are_listed(self):
        new, stale = against(self.writes(gated_sources()), self.WRITES)
        self.assertEqual(new, {}, "记忆写入走 core/memory_gate.py write / revise（WAVE12 C8），不要直接调 core/memstore.py")
        self.assertEqual(stale, {}, "这几处已经不直接写了：把它们从 WRITES 里删掉")

    def test_only_memory_proof_makes_a_proven(self):
        self.assertEqual(self.proofs(gated_sources()), [], "Proven 只由 app/memory_proof.py 核对后构造")

    def test_an_alias_a_new_caller_and_a_stale_listing_are_caught(self):
        sources = [("covey/app/a.py", "from covey.core import memstore as ms\ndef f():\n    ms.add(1)\n    ms.find(1)\n"),
                   ("covey/app/b.py", "from covey.core.memstore import settle as done\ndone(1)\n"),
                   ("covey/app/c.py", "import covey.core.principles as p\nclass K:\n    def g(self):\n        p.save(1)\n"),
                   ("covey/app/d.py", "from covey.core import memstore\nstore = memstore\nstore.archive(1)\n"),
                   ("covey/core/memstore.py", "def add():\n    pass\nadd()\n"),
                   ("covey/app/e.py", "from covey.core.memory_gate import Proven as P\nP(1, 'owner')\n"),
                   ("covey/app/memory_proof.py", "from covey.core import memory_gate\nmemory_gate.Proven(1, 'owner')\n")]
        found = self.writes(sources)
        self.assertEqual(found, {"covey/app/a.py::f::memstore.add": 1, "covey/app/b.py::<module>::memstore.settle": 1,
                                 "covey/app/c.py::K.g::principles.save": 1, "covey/app/d.py::<module>::memstore.archive": 1})
        new, stale = against(found, {"covey/app/a.py::f::memstore.add": 1, "covey/app/gone.py::f::memstore.add": 1})
        self.assertEqual(set(new), {"covey/app/b.py::<module>::memstore.settle", "covey/app/c.py::K.g::principles.save",
                                    "covey/app/d.py::<module>::memstore.archive"})
        self.assertEqual(stale, {"covey/app/gone.py::f::memstore.add": 1})
        self.assertEqual(self.proofs(sources), ["covey/app/e.py:2"])


class AnswerSourceTest(unittest.TestCase):
    """G9 (WAVE12 C11): every call of ``submit_answer`` or ``submit_question_feedback`` says who answered (keyword ``by``:
    an app/answers.py AnswerBy, or as before an open_id), and a core/memory_gate.py write of a decision carries its
    ``proof``. Today's calls without ``by`` are listed (``UNSAID``) until W1-B gives each its AnswerBy."""

    METHODS = {"submit_answer", "submit_question_feedback"}
    GATE = {"covey.core.memory_gate": {"write"}}
    UNSAID = {}  # "<file>::<enclosing def>::<method>": calls without ``by`` (W1-B gave each its AnswerBy: none left).

    @classmethod
    def unsaid(cls, sources):
        keys = []
        for rel, source in sources:
            stack = []

            def walk(node):
                scoped = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                if scoped:
                    stack.append(node.name)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in cls.METHODS
                        and not any(keyword.arg == "by" for keyword in node.keywords)):
                    keys.append(f"{rel}::{'.'.join(stack) or '<module>'}::{node.func.attr}")
                for child in ast.iter_child_nodes(node):
                    walk(child)
                if scoped:
                    stack.pop()

            walk(ast.parse(source))
        return counted(keys)

    @classmethod
    def unproven(cls, sources):
        return [f"{rel}:{node.lineno}" for rel, source in sources for _, _, node in module_calls(source, cls.GATE)
                if any(k.arg == "decision" for k in node.keywords) and not any(k.arg == "proof" for k in node.keywords)]

    def test_every_answer_says_who_gave_it_and_the_calls_of_today_are_listed(self):
        new, stale = against(self.unsaid(gated_sources()), self.UNSAID)
        self.assertEqual(new, {}, "submit_answer / submit_question_feedback 要带 by=（app/answers.py AnswerBy，WAVE12 C2）")
        self.assertEqual(stale, {}, "这几处已经带上 by 了：把它们从 UNSAID 里删掉")

    def test_a_decision_written_through_the_gate_carries_its_proof(self):
        self.assertEqual(self.unproven(gated_sources()), [], "带 decision= 的 memory_gate.write 要带 proof=")

    def test_a_call_without_by_a_written_decision_without_proof_and_a_stale_listing_are_caught(self):
        sources = [("covey/app/a.py", "class Q:\n    def f(self):\n        self.q.submit_answer(1, by=None)\n"
                                      "        self.q.submit_answer(1, **extra)\n        self.q.submit_question_feedback(1)\n"),
                   ("covey/app/b.py", "from covey.core import memory_gate as gate\ngate.write(1, decision={}, by='lead')\n"
                                      "gate.write(1, decision={}, proof=None)\ngate.write(1, by='lead')\n")]
        found = self.unsaid(sources)
        self.assertEqual(found, {"covey/app/a.py::Q.f::submit_answer": 1, "covey/app/a.py::Q.f::submit_question_feedback": 1})
        new, stale = against(found, {"covey/app/a.py::Q.f::submit_answer": 1, "covey/app/gone.py::f::submit_answer": 1})
        self.assertEqual(set(new), {"covey/app/a.py::Q.f::submit_question_feedback"})
        self.assertEqual(stale, {"covey/app/gone.py::f::submit_answer": 1})
        self.assertEqual(self.unproven(sources), ["covey/app/b.py:2"])


class TurnSourceStampTest(unittest.TestCase):
    """WAVE12 W1-0 read the five new kinds of core/turn_source.py and stamped none; W1-B stamps ``relay``, ``lead``, ``answer``
    and ``answer_for`` (W1-A ``wake``): each place that makes a field of one (``make`` or a ``stamp`` with it) is listed
    (``STAMPED``), so a new one is seen."""

    NEW = {"relay", "lead", "wake", "answer", "answer_for"}
    NAMES = {"RELAY", "LEAD", "WAKE", "ANSWER", "ANSWER_FOR"}
    STAMPED = {  # "<file>::<enclosing def>::<kind>": calls (W1-B, W1-A).
        # W1-B: the continuations of answers (app/questions.py _queue_continuation queues them so).
        "covey/app/answers.py::continuation_source::answer": 1,
        "covey/app/answers.py::continuation_source::answer_for": 1,
        # W1-B: the butler's hand-on to a project's lead (project_send's work, ``_send`` since W2-B's once-a-turn), and to a
        # topic (topic_start, topic_send, topic_fork; a lead's too).
        "covey/app/butler_projects.py::_send::relay": 2,
        "covey/app/butler_topics.py::source_of::lead": 1,
        "covey/app/butler_topics.py::source_of::relay": 2,
        # W1-A: the Bridge's wake-up of a project's lead (ProjectLoop.enqueue).
        "covey/app/project_loop.py::ProjectLoop.enqueue::wake": 1,
    }

    @classmethod
    def stamps(cls, sources):
        keys = []
        for rel, source in sources:
            if rel == "covey/core/turn_source.py":
                continue
            for scope, name, node in module_calls(source, {"covey.core.turn_source": {"make", "stamp"}}):
                first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "kind_"), None)
                kind = (first.value if isinstance(first, ast.Constant) else
                        first.attr if isinstance(first, ast.Attribute) else first.id if isinstance(first, ast.Name) else "")
                kind = {name.lower(): name.lower() for name in cls.NAMES}.get(str(kind).lower(), str(kind))
                if name.endswith(".stamp") or kind in cls.NEW:
                    keys.append(f"{rel}::{scope}::{kind}")
        return counted(keys)

    def test_each_stamp_of_a_new_kind_is_listed(self):
        new, stale = against(self.stamps(gated_sources()), self.STAMPED)
        self.assertEqual(new, {}, "新 kind 的盖章归 W1-B / W1-A：盖的那个包在 STAMPED 里登记")
        self.assertEqual(stale, {})

    def test_a_stamp_by_constant_by_name_or_by_stamp_is_caught(self):
        sources = [("covey/app/a.py", "from covey.core import turn_source\nturn_source.make(turn_source.RELAY, asserted_by='butler')\n"
                                      "turn_source.make('answer_for', asserted_by='lead')\nturn_source.make(turn_source.ASK, asserted_by='topic')\n"
                                      "def f():\n    turn_source.stamp('owner')\n")]
        self.assertEqual(self.stamps(sources), {"covey/app/a.py::<module>::relay": 1, "covey/app/a.py::<module>::answer_for": 1,
                                                "covey/app/a.py::f::owner": 1})


# G7 (WAVE12 C11, W1-A): the rules a turn reads agree with what the Bridge gives it. The short codes they show as examples are
# codes (core/memstore.py CODE_RE); a Bridge tool a role's rules or tool descriptions name is on that role's list; a value they
# tell the model to write for an enumerated argument is one of that argument's values; a list of the five kinds the owner is
# always asked is core/ask_kinds.py's (SHORT, LONG), written nowhere else.
PROMPT_ON = {"COVEY_PROJECTS": "1", "COVEY_PROJECT_TRIAGE": "1", "COVEY_COLLAB": "1", "COVEY_PROJECT_AUTOWAKE": "1",
             "COVEY_BUTLER_RULES": "1", "COVEY_HISTORY_CMD": "/absolute/history", "COVEY_PROJECT_TIDY": "1"}
PROMPT_OFF = {"COVEY_PROJECTS": "1", "COVEY_PROJECT_TRIAGE": "0", "COVEY_COLLAB": "0", "COVEY_PROJECT_AUTOWAKE": "0",
              "COVEY_BUTLER_RULES": "0", "COVEY_HISTORY_CMD": "", "COVEY_PROJECT_TIDY": "0"}
EXAMPLE_CODE = re.compile(r"(?<![0-9A-Za-z_])[RDN][0-9][0-9A-Z]{2,6}(?![0-9A-Za-z_])")  # A code as a prompt shows one (R7K2M).
TOLD = r"\s*(?:写|填|为|是|=|：|:)\s*([a-z][a-z_]*)"  # "kind 写 ask", "scope：global".


def string_constants(source):
    """(line, text) of each string constant of ``source`` but its docstrings (an f-string's literal parts count)."""
    tree = ast.parse(source)
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
            and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs]


def schema_texts(node):
    """Every description in a tool (its own and its arguments', at any depth)."""
    if isinstance(node, dict):
        return [text for key, value in node.items()
                for text in ([value] if key == "description" and isinstance(value, str) else schema_texts(value))]
    return [text for value in node for text in schema_texts(value)] if isinstance(node, (list, tuple)) else []


def schema_enums(node, found=None):
    """{argument name: its values} of every enumerated argument in ``node`` (a tool, a schema, a list of them), at any depth;
    an array argument's items count as the argument."""
    found = {} if found is None else found
    if isinstance(node, dict):
        for name, spec in (node.get("properties") or {}).items():
            for side in (spec, spec.get("items") if isinstance(spec, dict) else None):
                if isinstance(side, dict) and isinstance(side.get("enum"), list):
                    found.setdefault(name, set()).update(side["enum"])
        for value in node.values():
            schema_enums(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            schema_enums(value, found)
    return found


def tool_names_in(text, names):
    return {name for name in names if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)}


def told_values(text, enums):
    """(argument, value) for each "<argument> 写 <value>" in ``text`` of an argument in ``enums`` (true/false are not values)."""
    if not enums:
        return []
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, sorted(enums, key=len, reverse=True))) + r")" + TOLD)
    return [(m.group(1), m.group(2)) for m in pattern.finditer(text) if m.group(2) not in ("true", "false", "null")]


def five_kinds_lists(sources):
    """{"<file>": count} of the string constants outside core/ask_kinds.py that write a list of the five kinds by hand: two of
    their short names joined by 、, or the long form of one (core/ask_kinds.py LONG's parts)."""
    from covey.core import ask_kinds

    shorts = "|".join(map(re.escape, (kind.short for kind in ask_kinds.KINDS)))
    hand = [re.compile(rf"(?:{shorts})、(?:{shorts})"),
            *(re.compile(re.escape(kind.long)) for kind in ask_kinds.KINDS if kind.long != kind.short)]
    keys = [rel for rel, source in sources if rel != "covey/core/ask_kinds.py"
            for _, text in string_constants(source) for pattern in hand if pattern.search(text)]
    return counted(keys)


class PromptSchemaTest(unittest.TestCase):
    """G7 (WAVE12 C11): what the butler's and a lead's rules and tool descriptions say agrees with their tools' schemas, the
    example codes are codes, and the five kinds are written in one place. Today's hand-written lists of the five are listed
    (``FIVE_LISTED``, none left) and the list only shrinks."""

    FIVE_LISTED = {}  # "<file>": string constants writing the five kinds by hand (W1-A 主会话清零; none found at W1-A).

    @staticmethod
    def roles(environ):
        """(role, its tools, its rules and tool descriptions) for the butler and a lead under ``environ``. With everything on, a
        lead's also every string of app/project_lead.py (the lines of its state pack, which name tools too); with it off, only
        the rules as they are put together (the parts for 协作 and the question triage are left out then)."""
        from unittest import mock

        from covey.app import project_lead, project_loop, prompts
        from covey.core import butler_tool_specs

        with mock.patch.dict(os.environ, environ):
            lead = butler_tool_specs.lead_tools(environ)
            constants = [value for value in vars(project_lead).values() if isinstance(value, str)]
            constants += [text for value in vars(project_lead).values() if isinstance(value, (tuple, list))
                          for text in value if isinstance(text, str)]
            constants = constants if environ is PROMPT_ON else []
            lead_text = "\n".join([project_lead.lead_rules(), project_loop.WAKE_TEXT, *constants, *schema_texts(lead)])
            butler = butler_tool_specs.listed(environ)
            butler_text = "\n".join([prompts.butler_rules(), *schema_texts(butler)])
        return (("负责人", lead, lead_text), ("管家", butler, butler_text))

    @staticmethod
    def known_tools():
        from covey.core import butler_tool_specs as specs

        return set(specs.ALL_NAMES) | set(specs.BUTLER_PROJECT_NAMES) | set(specs.PROJECT_NAMES) | set(specs.RENAMED_TOOLS)

    def test_every_example_code_is_a_code(self):
        from covey.core import memstore

        found = {(rel, line, code) for rel, source in gated_sources() for line, text in string_constants(source)
                 for code in EXAMPLE_CODE.findall(text)}
        self.assertTrue(any(code == "R7K2M" for _, _, code in found))
        self.assertEqual({f"{rel}:{line} {code}" for rel, line, code in found if not memstore.CODE_RE.match(code)}, set(),
                         "示例短码要过 core/memstore.py 的 CODE_RE（用 R7K2M 这类）")

    def test_a_tool_the_rules_name_is_on_the_roles_list(self):
        known = self.known_tools()
        for environ in (PROMPT_ON, PROMPT_OFF):
            for role, tools, text in self.roles(environ):
                with self.subTest(role=role, on=environ is PROMPT_ON):
                    listed = {tool["name"] for tool in tools}
                    self.assertEqual(tool_names_in(text, known) - listed, set(),
                                     f"{role}的规则或工具说明点名了它没有的工具（改规则，或把工具加进{role}的清单）")

    def test_a_value_the_rules_tell_is_one_of_the_arguments_values(self):
        for environ in (PROMPT_ON, PROMPT_OFF):
            for role, tools, text in self.roles(environ):
                enums = schema_enums(tools)
                with self.subTest(role=role, on=environ is PROMPT_ON):
                    self.assertEqual([f"{arg}={value}" for arg, value in told_values(text, enums) if value not in enums[arg]],
                                     [], f"{role}的规则让模型写的值不在工具 schema 的 enum 里")
                    for tool in tools:  # What follows a tool's name in the same sentence is told of that tool.
                        own = schema_enums(tool)
                        for match in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(tool['name'])}(?![A-Za-z0-9_])[^。；\n]{{0,60}}",
                                                 text):
                            wrong = [f"{tool['name']}.{arg}={value}" for arg, value in told_values(match.group(0), own)
                                     if value not in own[arg]]
                            self.assertEqual(wrong, [], match.group(0))

    def test_a_checkpoint_questions_kind_is_one_of_the_question_kinds(self):
        from covey.core import ask_kinds, butler_tool_specs, checkpoint

        listed = re.search(r"（([a-z_、]+)）", checkpoint.KIND_RULE)
        self.assertEqual(tuple(listed.group(1).split("、")), ask_kinds.QUESTION_KINDS)
        self.assertIn(checkpoint.KIND_RULE, checkpoint.CHECKPOINT_RULES)
        tools = (*butler_tool_specs.TOOLS, *butler_tool_specs.PROJECT_TOOLS, *butler_tool_specs.TRIAGE_TOOLS,
                 *butler_tool_specs.CHECKLIST_TOOLS, *butler_tool_specs.BUTLER_PROJECT_TOOLS)
        for tool in tools:  # A tool that asks the owner questions (a lead's question card) takes the same kinds.
            questions = tool["input_schema"].get("properties", {}).get("questions")
            kind = ((questions or {}).get("items") or {}).get("properties", {}).get("kind") if isinstance(questions, dict) else None
            if isinstance(kind, dict) and "enum" in kind:
                with self.subTest(tool=tool["name"]):
                    self.assertEqual(tuple(kind["enum"]), ask_kinds.QUESTION_KINDS)

    def test_the_five_kinds_are_written_only_in_ask_kinds(self):
        new, stale = against(five_kinds_lists(gated_sources()), self.FIVE_LISTED)
        self.assertEqual(new, {}, "五类（部署、删除…）的列表只写在 core/ask_kinds.py：引用 checkpoint.ASK_SHORT / ASK_KINDS")
        self.assertEqual(stale, {}, "这些文件已经不手写五类了：把它们从 FIVE_LISTED 里删掉")

    # What only a checkpoint contract says (core/checkpoint.py CHECKPOINT_RULES and checkpoint_note): a lead's turn has none.
    CONTRACT = ("requires_user", "本任务检查点文件", "next_step", "external_tasks", '"phase"')

    def test_lead_turn_has_no_checkpoint_contract(self):
        """WAVE12 C4 / G7: a lead's turn (held to LEAD, or in its topic) gets app/runs.py LEAD_RULE in place of the checkpoint
        contract, and neither its rules, its wake-up text nor its tools' descriptions write one, under either switch setting."""
        from types import SimpleNamespace
        from unittest import mock

        from covey.app.runs import LEAD_RULE, Runs
        from covey.core import checkpoint
        from covey.core.harness import LEAD, TOOL_LIMIT

        self.assertTrue(all(word in checkpoint.CHECKPOINT_RULES for word in self.CONTRACT[:1]))  # The markers are the contract's.
        runs = Runs.__new__(Runs)
        held = Runs.checkpoint_rules(runs, SimpleNamespace(options={TOOL_LIMIT: LEAD}, topic_id="t"), None)
        with mock.patch("covey.app.runs.is_lead", return_value=True):
            topic = Runs.checkpoint_rules(runs, SimpleNamespace(options={}, topic_id="t"),
                                          SimpleNamespace(topic_id="t", chat_id="oc"))
        self.assertEqual((held, topic), (LEAD_RULE, LEAD_RULE))
        for environ in (PROMPT_ON, PROMPT_OFF):
            lead = self.roles(environ)[0][2]
            for text in (LEAD_RULE, lead):
                with self.subTest(on=environ is PROMPT_ON):
                    self.assertEqual([word for word in self.CONTRACT if word in text], [])

    def test_the_checks_catch_a_bad_code_an_unlisted_tool_a_wrong_value_and_a_hand_written_list(self):
        from covey.core import memstore

        self.assertFalse(memstore.CODE_RE.match(EXAMPLE_CODE.findall("比如 R1IO0 这条")[0]))
        self.assertEqual(tool_names_in("用 topic_start 开，再 project_note。project_notes 不是", {"topic_start", "project_note"}),
                         {"topic_start", "project_note"})
        self.assertEqual(told_values("project_note（kind 写 ask）；report 为 true；scope：global", {"kind": {"note"}, "scope": {"x"}}),
                         [("kind", "ask"), ("scope", "global")])
        self.assertEqual(schema_enums({"properties": {"a": {"enum": ["x"]}, "b": {"type": "array", "items": {"enum": ["y"]}}}}),
                         {"a": {"x"}, "b": {"y"}})
        source = ('"""部署、删除 in a docstring."""\nRULE = "要问：部署、删除、花钱"\nTAIL = f"{x}占机器（长时间占 GPU 或集群、起长期运行的服务、大量下载或写盘）"\n'
                  'OK = "不部署、不删东西"\n')
        self.assertEqual(five_kinds_lists([("covey/app/a.py", source), ("covey/core/ask_kinds.py", source)]), {"covey/app/a.py": 2})


# G1 (W1-F, C11): a read for a write. read_object hands back {} for a file that is there but cannot be read for a moment (a
# network disk's passing error, a torn file); a function that reads a record so and writes it back writes over all of it with
# only what it changed (settings reset to their defaults, a ledger emptied). Such a read is state.read_for_write (or
# state.update_json), which refuses: core/state.py Unreadable.
STATE_READS = {"read_object"}
STATE_WRITES = {"write_json", "write_private_text", "write_durable_json", "atomic_write_bytes"}
STATE_WRITES_FILE = REPO / "tests" / "golden" / "state_writes.json"


def _called(func):
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _record_read(call):
    """read_object(...), or read_json(path, default): a read that is {} (the default) when the file cannot be read."""
    name = _called(call.func)
    return name in STATE_READS or (name == "read_json" and (len(call.args) >= 2 or any(k.arg == "default" for k in call.keywords)))


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def state_writes(source, rel):
    """[(``rel:function``, line)] of the functions of ``source`` (methods as ``Class.method``; nested functions count as
    their outer one's) that read a record for a write: before a write in the same function, a record read
    (``_record_read``) of the path it writes, a call of a reader of this module (a top-level function with a record read
    and no write) on the names that path is made of, or ``{**read_object(...)}``. A writer of this module (a top-level
    function that writes the path one of its arguments gives, ``save(target, record)``) counts as a write of that path."""
    tree = ast.parse(source)
    writers = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [a.arg for a in node.args.args]
            for c in ast.walk(node):
                if isinstance(c, ast.Call) and _called(c.func) in STATE_WRITES and c.args \
                        and isinstance(c.args[0], ast.Name) and c.args[0].id in params:
                    writers[node.name] = params.index(c.args[0].id)
    readers = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            if any(_record_read(c) for c in calls) and not any(_called(c.func) in STATE_WRITES for c in calls):
                readers.add(node.name)
    found = []

    def check(func, name):
        calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)]
        direct = [c for c in calls if _called(c.func) in STATE_WRITES and c.args]
        wrapped = [c for c in calls if isinstance(c.func, ast.Name) and c.func.id in writers and c.func.id != func.name
                   and len(c.args) > writers[c.func.id]]
        written = direct + wrapped
        if not written:
            return
        writes = [c.args[0] for c in direct] + [c.args[writers[c.func.id]] for c in wrapped]
        last = max(c.end_lineno for c in written)  # A read after every write (a fallback, a reply) writes nothing back.
        calls = [c for c in calls if c.lineno <= last]
        local = {}
        for n in ast.walk(func):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                local.setdefault(n.targets[0].id, n.value)
        paths = {ast.dump(target) for target in writes}
        made_of = []
        for target in writes:
            names = _names_in(target)
            made_of.append(names.union(*(_names_in(local[n]) for n in names if n in local)))
        for c in calls:
            if _record_read(c) and c.args and ast.dump(c.args[0]) in paths:
                found.append((f"{rel}:{name}", c.lineno))
                return
            if isinstance(c.func, ast.Name) and c.func.id in readers and c.func.id != func.name and c.args:
                given = set().union(*(_names_in(a) for a in c.args))
                if given and any(given <= names for names in made_of):
                    found.append((f"{rel}:{name}", c.lineno))
                    return
        for d in ast.walk(func):
            if isinstance(d, ast.Dict) and any(k is None and isinstance(v, ast.Call) and _called(v.func) in STATE_READS
                                               for k, v in zip(d.keys, d.values)):
                found.append((f"{rel}:{name}", d.lineno))
                return

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                check(node, prefix + node.name)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
    visit(tree.body, "")
    return found


class CardWriteTest(unittest.TestCase):
    """G3 (WAVE12 C11, W2-D 包 C): a card write's outcome is never thrown away. A call of ``.patch_card(`` or ``.put_card(``
    whose result is a bare expression or is given to ``_`` leaves the card at its old state with nothing said: use
    feishu/messenger.py ``patch_or_tell`` (it says so under the card once), or look at what came back. Those that rightly
    do not care (a card that cuts itself and is written again at once) are listed (``LEFT``), and a listed one that is gone
    fails too. The package and the extensions a release carries are read (``gated_sources``)."""

    METHODS = {"patch_card", "put_card"}
    LEFT: dict = {}  # "<file>::<enclosing def>::<method>": calls whose outcome is rightly not looked at.

    @classmethod
    def dropped(cls, sources):
        keys = []
        for rel, source in sources:
            stack = []

            def walk(node, parent=None):
                scoped = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                if scoped:
                    stack.append(node.name)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in cls.METHODS
                        and (isinstance(parent, ast.Expr) or (isinstance(parent, ast.Assign) and all(
                            isinstance(target, ast.Name) and target.id == "_" for target in parent.targets)))):
                    keys.append(f"{rel}::{'.'.join(stack) or '<module>'}::{node.func.attr}:{node.lineno}")
                for child in ast.iter_child_nodes(node):
                    walk(child, node)
                if scoped:
                    stack.pop()

            walk(ast.parse(source))
        return counted(key.rsplit(":", 1)[0] for key in keys), keys

    def test_no_card_write_throws_its_outcome_away(self):
        found, lines = self.dropped(gated_sources())
        new, stale = against(found, self.LEFT)
        self.assertEqual(new, {}, "改卡的结果不能丢：用 Messenger.patch_or_tell，或看返回值（WAVE12 G3）\n" + "\n".join(lines))
        self.assertEqual(stale, {}, "这几处已经不丢结果了：把它们从 LEFT 里删掉")

    def test_a_bare_call_one_given_to_underscore_and_a_stale_listing_are_caught(self):
        sources = [("covey/app/a.py", textwrap.dedent('''
            class C:
                def paint(self, m):
                    m.patch_card(1, 2, {})
                    _ = m.put_card(1, 2, {}, sequence=1, key="k")
                    ok = m.patch_card(1, 2, {})
                    if not m.put_card(1, 2, {}, sequence=1, key="k"):
                        pass
                    m.patch_or_tell(1, 2, {}, voice=None)
                    return m.patch_card(1, 2, {})
        '''))]
        found, lines = self.dropped(sources)
        self.assertEqual(found, {"covey/app/a.py::C.paint::patch_card": 1, "covey/app/a.py::C.paint::put_card": 1})
        self.assertEqual(lines, ["covey/app/a.py::C.paint::patch_card:4", "covey/app/a.py::C.paint::put_card:5"])
        new, stale = against(found, {"covey/app/a.py::C.paint::patch_card": 1, "covey/app/gone.py::f::put_card": 1})
        self.assertEqual(set(new), {"covey/app/a.py::C.paint::put_card"})
        self.assertEqual(stale, {"covey/app/gone.py::f::put_card": 1})


class StateWriteTest(unittest.TestCase):
    """G1 (W1-F): no new read for a write through read_object; the functions found today are listed in
    tests/golden/state_writes.json, which only shrinks."""

    def allowed(self):
        import json
        return set(json.loads(STATE_WRITES_FILE.read_text())["allowed"])

    def found(self):
        rows = []
        for path in sorted(PACKAGE.rglob("*.py")):
            rows += state_writes(path.read_text(), path.relative_to(PACKAGE).as_posix())
        return rows

    def test_a_record_written_back_is_read_with_read_for_write(self):
        allowed = self.allowed()
        found = self.found()
        new = [f"{name} (line {line})" for name, line in found if name not in allowed]
        self.assertEqual(new, [], "写回同一个文件前的读改用 core/state.py read_for_write（或 update_json），读不出就不写"
                                  "（tests/golden/state_writes.json 只减不增）")
        gone = sorted(allowed - {name for name, _ in found})
        self.assertEqual(gone, [], "这些函数已经不再这样读了：从 tests/golden/state_writes.json 去掉")

    def test_the_modules_this_package_fixed_stay_off_the_list(self):
        """W1-F step 3: the settings, the owner's ledger, the 专项 records and the 辅助模型's record."""
        fixed = ("core/butler_settings.py:", "core/owner_ledger.py:", "core/projects.py:", "core/assist.py:")
        self.assertEqual(sorted(name for name in self.allowed() if name.startswith(fixed)), [])

    def test_the_scan_finds_each_kind_of_read_for_a_write(self):
        bad = textwrap.dedent('''
            from covey.core.state import read_object, read_json, write_json
            def load(state_dir):
                return read_object(path(state_dir))
            def plain(state_dir):
                with lock():
                    data = read_object(path(state_dir))
                    write_json(path(state_dir), {**data, "a": 1})
            def by_name(state_dir):
                target = path(state_dir)
                data = read_json(target, {})
                write_json(target, data)
            def by_reader(state_dir):
                target = path(state_dir)
                data = load(state_dir)
                write_json(target, data)
            def spread(state_dir, other):
                write_json(other, {**read_object(somewhere()), "b": 2})
            class Panel:
                def save(self, path):
                    found = read_object(path)
                    def change():
                        write_json(path, found)
                    change()
            def store(target, record):
                write_json(target, record, indent=2)
            def by_writer(target):
                record = read_object(target)
                store(target, {**record, "c": 3})
        ''')
        self.assertEqual([name for name, _ in state_writes(bad, "x.py")],
                         ["x.py:plain", "x.py:by_name", "x.py:by_reader", "x.py:spread", "x.py:Panel.save", "x.py:by_writer"])
        good = textwrap.dedent('''
            from covey.core.state import read_for_write, read_json, read_object, update_json, write_json
            def plain(state_dir):
                with lock():
                    data = read_for_write(path(state_dir))
                    write_json(path(state_dir), {**data, "a": 1})
            def updated(state_dir):
                update_json(path(state_dir), lambda data: data.update(a=1))
            def raises(state_dir):
                write_json(path(state_dir), read_json(path(state_dir)))
            def reads_only(state_dir):
                return {**read_object(path(state_dir)), "b": 2}
            def other_file(state_dir):
                write_json(path(state_dir, "b"), read_object(path(state_dir, "a")))
            def load(state_dir):
                return read_object(path(state_dir))
            def falls_back(state_dir):
                try:
                    write_json(path(state_dir), read_for_write(path(state_dir)))
                except OSError:
                    return load(state_dir)
        ''')
        self.assertEqual(state_writes(good, "y.py"), [])

    def test_a_stale_or_new_entry_fails(self):
        allowed = self.allowed()
        found = {name for name, _ in self.found()}
        self.assertTrue(allowed, "G1 的白名单不该是空的：真清空了就把这条改掉")
        self.assertLessEqual(allowed, found)
        self.assertNotIn("x.py:plain", allowed)


# G4 (WAVE12 C11, T904): a mid-state a module writes, found in its source. MID_STATES are the states a restart can leave a record
# in; a write is one as the value (a branch of a conditional too) of key "state" or "status" in a dict, of keyword state= or status=
# of a call (not a read: MID_READS), or assigned to x["state"] / x["status"].
MID_STATES = frozenset({"sending", "creating", "running", "submitting", "uncertain"})
MID_KEYS = frozenset({"state", "status"})
MID_READS = frozenset({"query", "reply"})  # store queries by state; a tool's reply fields.
MIDSTATES = REPO / "tests" / "golden" / "midstates.json"
RESTART_SURFACES = ("飞书", "网页")


def mid_states_written(tree) -> set:
    """The MID_STATES ``tree`` (a module's AST) writes as a record's state or status."""
    def values(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.IfExp):
            yield from values(node.body)
            yield from values(node.orelse)

    def called(func):
        return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""

    found = set()
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Dict):
            pairs = [(key.value, value) for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant)]
        elif isinstance(node, ast.Call) and called(node.func) not in MID_READS:
            pairs = [(keyword.arg, keyword.value) for keyword in node.keywords]
        elif isinstance(node, ast.Assign):
            pairs = [(target.slice.value, node.value) for target in node.targets
                     if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)]
        for key, value in pairs:
            if key in MID_KEYS:
                found |= set(values(value)) & MID_STATES
    return found


def mid_state_problems(found, listed, cells) -> list:
    """What is wrong between ``found`` ({module: states its source writes}), ``listed`` (midstates.json's modules) and ``cells``
    (test_matrix.py RESTART_CELLS' modules -> their surfaces): each a line saying what to do."""
    problems = []
    for module, states in sorted(found.items()):
        entry = listed.get(module)
        if entry is None:
            problems.append(f"{module} 写中间态 {sorted(states)}：登记进 tests/golden/midstates.json，写明重启后怎么收尾")
        elif set(entry.get("states") or ()) != states:
            problems.append(f"{module} 写的中间态是 {sorted(states)}，登记的是 {sorted(entry.get('states') or ())}：改登记")
    for module, entry in sorted(listed.items()):
        if module not in found:
            problems.append(f"{module} 已不写中间态：从 tests/golden/midstates.json 去掉")
        if not str(entry.get("restart") or "").strip():
            problems.append(f"{module} 没写重启后怎么收尾（restart）")
        surfaces = cells.get(module)
        if surfaces is None:
            problems.append(f"{module} 在 tests/test_matrix.py RESTART_CELLS 没有「重启后再点一次」格")
        elif sorted(surfaces) != sorted(RESTART_SURFACES):
            problems.append(f"{module} 的「重启后再点一次」格要有 {'、'.join(RESTART_SURFACES)} 两格，现在是 {sorted(surfaces)}")
    for module in sorted(set(cells) - set(listed)):
        problems.append(f"{module} 在 RESTART_CELLS 里但没登记中间态：从 RESTART_CELLS 去掉")
    return problems


def restart_cells() -> dict:
    """tests/test_matrix.py RESTART_CELLS as {module: [its surfaces]}, read from its source (test_matrix builds a whole Bridge)."""
    tree = ast.parse((REPO / "tests" / "test_matrix.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RESTART_CELLS" for t in node.targets):
            return {ast.literal_eval(key): [ast.literal_eval(surface) for surface in value.keys]
                    for key, value in zip(node.value.keys, node.value.values) if isinstance(value, ast.Dict)}
    return {}


class MidStateTest(unittest.TestCase):
    """G4 (WAVE12 C11, T904): a module that writes sending, creating, running, submitting or uncertain is in
    tests/golden/midstates.json with what a restart leaves of it, and has its 重启后再点一次 cells in tests/test_matrix.py."""

    def found(self) -> dict:
        found = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            states = mid_states_written(ast.parse(path.read_text(encoding="utf-8")))
            if states:
                found[path.relative_to(REPO).as_posix()] = states
        return found

    def test_every_module_writing_a_mid_state_is_listed_with_its_restart_cells(self):
        import json
        listed = json.loads(MIDSTATES.read_text(encoding="utf-8"))["modules"]
        self.assertEqual(mid_state_problems(self.found(), listed, restart_cells()), [])

    def test_the_check_finds_writes_and_leaves_reads_and_says_what_is_stale(self):
        source = textwrap.dedent('''
            record = {"state": "sending", "other": "running"}
            save(path, {**record, "status": "done" if ok else "uncertain"})
            row["status"] = "creating"
            update(path, state="submitting")
            store.query(status="running")
            reply(False, "x", status="uncertain")
            if record.get("state") == "running":
                pass
        ''')
        self.assertEqual(mid_states_written(ast.parse(source)), {"sending", "uncertain", "creating", "submitting"})
        cells = {"covey/a.py": ["飞书", "网页"], "covey/gone.py": ["飞书", "网页"], "covey/half.py": ["飞书"]}
        listed = {"covey/a.py": {"states": ["sending"], "restart": "x"}, "covey/gone.py": {"states": ["running"], "restart": "y"},
                  "covey/half.py": {"states": ["running"], "restart": ""}}
        problems = mid_state_problems({"covey/a.py": {"sending", "uncertain"}, "covey/new.py": {"running"},
                                       "covey/half.py": {"running"}}, listed, {**cells, "covey/extra.py": ["飞书", "网页"]})
        self.assertEqual(len(problems), 6, problems)
        for words in ("covey/new.py 写中间态", "covey/a.py 写的中间态是", "covey/gone.py 已不写中间态", "covey/half.py 没写重启后",
                      "covey/half.py 的「重启后再点一次」格", "covey/extra.py 在 RESTART_CELLS 里但没登记"):
            self.assertTrue(any(line.startswith(words) for line in problems), words)
        self.assertEqual(mid_state_problems({}, {}, {}), [])
