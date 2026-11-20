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
- Every environment variable and command-line option the code reads has its row in docs/operations.md.
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
import unittest
from operator import attrgetter
from pathlib import Path

from support import REPO
from support.source_imports import imported_modules, imports, module_name
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
from covey.cards.project_proposal import KINDS as TIDY_KINDS
from covey.cards.settings_hub import KINDS as SETTINGS_KINDS
from covey.cards.overview import DISMISS as OVERVIEW_DISMISS
from covey.cards.board import KINDS as BOARD_KINDS
from covey.cards.app_setup import SKIP as APP_SETUP_SKIP, START as APP_SETUP_START
from covey.cards import launch_config
from covey.cards.rewind import KINDS as REWIND_KINDS
from covey.cards.workspace_manager import MANAGER_PREFIX
from covey.cards.held_note import NOTE_KIND
from covey.cards.proxy_panel import KINDS as PROXY_PANEL_KINDS
from covey.cards.proxy_resend import RESEND as PROXY_RESEND, SUGGEST as PROXY_SUGGEST
from covey.cards.owner_send import DROP as OWNER_SEND_DROP, PICK as OWNER_SEND_PICK, SEND as OWNER_SEND
from covey.cards.steer_receipts import CHECK_KIND, REQUEUE_KIND, WITHDRAW_KIND
from covey.core import extensions
from covey.core.harness import HARNESS_REGISTRY
from covey.core.jobs import JOB_STOP_ACTION_KIND
from covey.app.standing_answers import KINDS as STANDING_KINDS
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
        before_routing |= {OVERVIEW_DISMISS}  # 手上的事's 这些都不用回答了 (app/overview.py).
        before_routing |= BOARD_KINDS  # The 看板's 刷新 and 记为看过 (app/digest.py).
        before_routing |= {APP_SETUP_START, APP_SETUP_SKIP}  # 飞书应用待补 (app/app_setup.py).
        before_routing |= {GROUP_MENU_KIND}  # A personal group's menu card (app/group_menu.py).
        before_routing |= REDACTION_KINDS  # The 设置 card's 脱敏 tab (app/redaction_panel.py).
        before_routing |= NODE_PANEL_KINDS  # The 设置 card's 设备 tab (app/node_panel.py).
        before_routing |= set(TIDY_KINDS)  # 整理专项's cards (app/project_tidy.py).
        before_routing |= STANDING_KINDS  # 以后这类按推荐走 and its card (app/standing_answers.py).
        before_routing |= {OWNER_SEND, OWNER_SEND_DROP, OWNER_SEND_PICK}  # The 代发草稿 card (app/owner_send.py).
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
                launch_config.WORKSPACE_ACTION, launch_config.MODE_ACTION, launch_config.PAGE_ACTION}
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
    "covey_devices.py": "covey.service.device_check",
    "covey_plugins.py": "covey.service.plugins",
    "covey_feishu_web.py": "covey.feishu.web_client",
    "covey_maso_runtime.py": "covey_internal.maso.runtime",  # compat shim for the company extension (extensions/covey_internal/)
    "covey_workspace_reader.py": "covey.backends.codex.workspace_reader",
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
        # One Harness's code does not reach into another's (round 2 B took Pi off Codex's gateway catalog and
        # UUID pattern); what they share is in backends/gateway (the model gateway) or core. Relative imports,
        # imports inside functions and under TYPE_CHECKING count too.
        found = [f"{path.relative_to(PACKAGE)}:{line} {module}" for path in sorted((PACKAGE / "backends").rglob("*.py"))
                 if path.parent != PACKAGE / "backends"
                 for module, line in foreign_backends(path.read_text(), module_name(path.parent))]
        self.assertEqual(found, [])

    def test_foreign_backends_sees_every_import_form(self):
        source = "\n".join(("import covey.backends.codex.gateway", "from covey.backends import codex",
                            "from covey.backends.codex.sessions import UUID_RE", "from ..claude import models",
                            "from . import home", "from covey.backends import gateway, pi",
                            "from covey.core import harness", "import covey.core.state",
                            "def f():\n    from covey.app import detached",
                            "if TYPE_CHECKING:\n    from covey.cards.streaming import CardRuntime",
                            "from ...core import roster", "from ...app.unified import x", "import json"))
        self.assertCountEqual([module for module, _ in foreign_backends(source, "covey.backends.pi")], [
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
        self.assertEqual(uuids, {"claude", "codex", "pi", "agy"})
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
                                   "doubao": "豆包", "kiro": "Kiro"})
        self.assertFalse((BIN / "acme_bridge_exec.sh").exists())  # The Acme wrappers' launcher is gone.
        self.assertFalse((PACKAGE / "backends" / "neeko_next").exists())  # 2026-11-02.
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
        self.assertLessEqual(gateway_harnesses, {"codex", "pi", "dsh"})  # A gateway's providers (core/gateways.py).
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
    ("covey/service/installer.py", "key"),  # default_switches: INSTALL_SWITCHES' literals, each with its own row.
    # A model gateway's variables are named by its configuration (core/gateways.py), not by the code.
    ("covey/core/gateways.py", "self.base_url_env"), ("covey/core/gateways.py", "variable"),
    ("covey/core/gateways.py", "self.key_file_env"),
    ("covey/backends/gateway/connection.py", "gateway.key_env"),
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

# What the docs name in backticks (AGENTS.md, README.md, docs/*.md; not docs/history/, which records the past).
DOC_FILES = ["AGENTS.md", "README.md", *sorted(str(p.relative_to(REPO)) for p in (REPO / "docs").glob("*.md"))]
# A backticked path whose first part is one of these is the repo's; others (`proxy/web/profile`, `jobs/`,
# `harnesses.json`) are under a deployer's data root.
DOC_ROOTS = {"covey", "bin", "docs", "scripts", "tests", "integrations", "skills"}
# AGENTS.md's code map names paths under covey/ and covey/backends/ without the prefix; these
# also start data-root paths (`cards/.locks`) and model IDs (`claude/claude-opus-5`), so only a file name with a
# code suffix or a directory with its trailing / counts.
DOC_SHORT_ROOTS = {"app", "core", "cards", "feishu", "service", "backends", "claude", "codex", "maso", "dsh", "pi",
                   "gateway"}
CODE_SUFFIXES = (".py", ".sh", ".json", ".js", ".html", ".css", ".md")
# Paths the docs name on purpose that are not in this repo.
DOC_UNTRACKED_PATHS = {
    "skills/lark-send-as-user",  # In the lark-send-as-user repo, which that script copies from.  # privacy: allow 外部发送工具仓库里的路径
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
    def test_no_method_is_defined_in_two_parts_of_project_tidy(self):
        # ProjectTidy is its parts put together (app/project_tidy.py): a second copy of a method in another part is dead
        # code the method resolution order hides, and an edit to it would do nothing.
        from covey.app.project_tidy import ProjectTidy
        seen, twice = {}, []
        for part in ProjectTidy.__mro__[1:-1]:
            for name, value in vars(part).items():
                if callable(value) and not name.startswith("__"):
                    if name in seen:
                        twice.append(f"{name}: {seen[name]} and {part.__name__}")
                    seen.setdefault(name, part.__name__)
        self.assertEqual(twice, [])
