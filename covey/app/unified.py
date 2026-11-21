"""The Bridge: one Feishu ingress, immutable topic routes, one app per Harness.

UnifiedBridgeApp is the composition root. It builds the process-wide Runtime and
Messenger, one Harness and one BridgeApp for each roster entry, and the card
controllers; it routes every message, click and job to the app of its topic's
Harness (app/card_routing.py CardRouting, app/job_dispatch.py JobDispatch, both parts of
UnifiedBridgeApp) and runs the process. No Harness app has a mutable backend selector.
Topics recorded by the retired single-harness Codex bot stay readable in place.
A task runner (app/runner.py) builds it with ``task_runner=True``: the controllers, features and bot menu that only
the Bridge uses (``PARTS``) are built, and their modules imported, on first use, since a runner starts for every turn.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from covey.core import env as cenv
from covey.app.app import BridgeApp
from covey.app import auto_retry, policy_retry
from covey.app.availability import watch as watch_availability
from covey.app.card_lanes import CardLanes
from covey.app.card_routing import INBOUND_DIR, CardRouting
from covey.app.job_dispatch import JobDispatch
from covey.app.butler import Butler
from covey.app.harnesses import Harness, components, enabled_names, models_first
from covey.app.outbox import run as run_outbox
from covey.app.pins import configure_boards
from covey.app.policy_guard import run as run_policy_guard
from covey.app.questions import fold_older_questions, recover_question_delivery
from covey.app.runtime import Runtime
from covey.app.topics import DEFAULT, Topics
from covey.core import extensions
from covey.core.harness import BridgeContext, format_timeout
from covey.core import gateways
from covey.core import spaces
from covey.core.locks import configure_card_locks
from covey.core.loops import supervised
from covey.core.process import instance_lock, log_to, service_key, stop_processes
from covey.core.scheduler import WorkspaceScheduler
from covey.feishu import ws_intake
from covey.feishu.events import (CARD_ACTION_EVENT_KEY, MENU_EVENT_KEY, MESSAGE_EVENT_KEY, SUBSCRIPTIONS,
                                          consume, consumer)
from covey.feishu.messenger import Messenger
from covey.feishu.transport import CliFailed, cli_json, configure_receipts, lark_argv
from covey.core.jobs import max_job_id
from covey.core.roster import NAMES
from covey.core.state import write_private_text, read_json, write_json
from covey.core import background, butler_settings, timing
from covey.core.store import save_indexes

# A task runner (``task_runner=True``) imports what the Bridge alone uses only when it first needs it: the
# Bridge's controllers (_build_parts), and below, the startup and maintenance parts of main().

CARD_ACTION_RETRY_SECONDS = 300


def run_json(argv: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        return cli_json(argv, timeout=timeout, runner=subprocess.run)
    except CliFailed as exc:
        raise RuntimeError(f"{argv[0]} failed: {exc.proc.stderr or exc.proc.stdout}") from None


def remove_pid(path):
    """The Bridge's pid file, unless another process has written its own there since."""
    try:
        if path.read_text(encoding="utf-8", errors="replace").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def save_record_indexes(gateway, ctx):
    """An outbox-loop step: the record directories' .index, for the task runners this Bridge starts."""
    save_indexes(ctx.state_dir)


def fold_questions(gateway, ctx):
    """An outbox-loop step: a question card a task runner showed folds its topic's older ones within a poll."""
    fold_older_questions(ctx, [app.questions for app in gateway.apps.values()])


# What the outbox loop does each round after resuming saved deliveries (app/outbox.py run).
OUTBOX_STEPS = (auto_retry.tick, policy_retry.tick, fold_questions, save_record_indexes)


class UnifiedBridgeApp(CardRouting, JobDispatch):
    """The Bridge process. Public entry points: main, handle_event, handle_card_action_event,
    process_reply_job (the scheduler's worker), and the parser/context builders a task runner
    reuses. Everything shared lives on ``runtime`` and ``messenger``; ``topics`` holds the
    Harnesses and their apps by name (``harnesses`` and ``apps`` are the same dicts) and the
    topic routes. The card controllers receive ``topics``."""

    # The card controllers a click meets before topic routing, in order (_handle_card_action_event), by
    # attribute: each claim(ctx, event, raw, owner) returns the click's outcome, or None when it is not
    # its own. EARLY match the event (a form button's name or the value's kind); PANELS, the value
    # once it passed the closed set of kinds (app/card_actions.py ACTIONS).
    EARLY = ('workspace_manager', 'launch_model_picker', 'directory_picker', 'harness_switch', 'rewind')
    PANELS = ('proxy.panel', 'proxy.resend', 'proxy.owner_send', 'group_manager', 'redaction_panel', 'nodes.panel',
              'butler_panel', 'settings_hub', 'overview', 'digest', 'app_setup', 'welcome', 'group_menu', 'project_tidy',
              'standing_answers')

    # What only the Bridge uses, built by _build_parts: at once in the Bridge (the default), on first use in a
    # task runner (``task_runner=True``), which rarely meets any of it.
    PARTS = ('directory_picker', 'launch_model_picker', 'workspace_manager', 'harness_switch', 'rewind', 'proxy',
             'weekly_usage', 'group_manager', 'redaction_panel', 'butler_panel',
             'settings_hub', 'overview', 'butler_env', 'butler_tools', 'butler_search', 'memguard_alerts', 'digest',
             'topic_seen', 'app_setup', 'dispatch_cards', 'project_cards', 'projects', 'project_tidy',
             'project_loop', 'patrol', 'devices', 'nodes', 'web_sync', 'source_watch', 'extension_features', 'features', 'menu', 'group_menu', 'group_boards',
             'standing_answers', 'welcome', 'revisits')

    def __init__(self, *, script_path, component_factory=components, task_runner=False):
        self.runtime = Runtime(script_path=script_path)
        self.messenger = Messenger(self.runtime.log)
        self.harnesses = {name: Harness(*component_factory(name)) for name in NAMES}
        # The service command line stays Codex-compatible even when a bot enables only another Harness.
        self.argument_harness = self.harnesses[DEFAULT]
        self.topics = Topics(self.harnesses, runtime=self.runtime, messenger=self.messenger)
        self.apps = self.topics.apps
        for name, harness in self.harnesses.items():
            self.apps[name] = BridgeApp(harness, runtime=self.runtime, messenger=self.messenger, topics=self.topics)
        self.voice = self.topics.voice
        self.butler = Butler(self.topics)  # 管家 (docs/butler.md 私聊模式).
        self.owner = ''
        self.expected_app_id = ''
        self.pairing_file = None
        self.event_procs = {}  # Each event key's running lark-cli consumer (feishu/events.py SUBSCRIPTIONS).
        self.card_lanes = None
        # As the Harnesses were when the Bridge was built: build_context drops the ones a bot does not enable.
        self.titles = {name: h.spec.title for name, h in self.harnesses.items()}
        self.claude = self.harnesses['claude']
        self.butler_paths = None  # (state directory, profile) for the butler's tools, set by build_context
        self.parts_lock = threading.RLock()
        if task_runner:
            # The enabled extensions' feature objects take what the Bridge hands them (runtime, messenger, voice),
            # and a broken one fails here, as it did before.
            self.extension_features = extensions.features(self)
        else:
            self._build_parts()

    def __getattr__(self, name):
        """A part not built yet (``PARTS``): built now, with all the others."""
        if name in self.PARTS and 'parts_lock' in self.__dict__:
            self._build_parts()
            try:
                return self.__dict__[name]
            except KeyError:
                pass
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def _build_parts(self):
        """The Bridge's controllers, the optional features and the bot menu, in the order they depend on each
        other. Built once."""
        from covey.app.app_setup import AppSetup
        from covey.app.bot_menu import BotMenu, Panels, known_chats
        from covey.app.digest import Digest
        from covey.app.directory_picker import DirectoryPicker
        from covey.app.dispatch_cards import DispatchCards
        from covey.app.project_cards import ProjectCards
        from covey.app.project_clicks import stop_tasks
        from covey.app.butler_projects import TOOLS as PROJECT_BUTLER_TOOLS
        from covey.app.projects import Projects
        from covey.app.project_loop import ProjectLoop
        from covey.app.project_tidy import ProjectTidy
        from covey.app.patrol import Patrol
        from covey.app.devices import Devices
        from covey.app.nodes import Nodes
        from covey.app.web_sync import WebSync
        from covey.app.group_boards import GroupBoards
        from covey.app.group_manager import GroupManager
        from covey.app.group_menu import GroupMenu
        from covey.app.harness_switch import HarnessSwitch
        from covey.app import butler_knowledge, butler_search, butler_panel, butler_tools, butler_topics
        from covey.app.butler_env import ButlerEnv
        from covey.app.butler_panel import ButlerPanel
        from covey.app.butler_tools import ButlerTools
        from covey.app.memguard_alerts import MemguardAlerts
        from covey.app.model_picker import LaunchModelPicker
        from covey.app.overview import Overview
        from covey.app.proxy import Proxy
        from covey.app.redaction_panel import RedactionPanel, detect_upstream
        from covey.app.rewind import RewindCards
        from covey.app.standing_answers import StandingAnswers
        from covey.app.settings_hub import SettingsHub
        from covey.app.source_watch import SourceWatch
        from covey.app.topic_seen import TopicSeen
        from covey.app.weekly_usage import WeeklyUsage
        from covey.app.welcome import Welcome
        from covey.app.revisits import Revisits
        from covey.app.workspace_manager import WorkspaceManager
        with self.parts_lock:
            if 'features' in self.__dict__:
                return
            self.directory_picker = DirectoryPicker(self.topics)
            self.launch_model_picker = LaunchModelPicker(self.topics)
            self.workspace_manager = WorkspaceManager(self.topics, self.directory_picker.cache)
            self.harness_switch = HarnessSwitch(self.topics)
            self.rewind = RewindCards(self.topics)
            # 以后这类按推荐走 under a question card (app/standing_answers.py): a principle of the workspace, then the answer.
            self.standing_answers = StandingAnswers(self.topics)
            self.proxy = Proxy(self.topics)
            self.weekly_usage = WeeklyUsage(self.runtime, self.messenger, self.voice, self.titles)
            self.group_manager = GroupManager(self.runtime, self.messenger, self.voice)
            # The 设置 card's 脱敏 tab (app/redaction_panel.py): the term list on the card; 探测 asks the Claude Harness.
            self.redaction_panel = RedactionPanel(self.runtime, self.messenger, self.voice,
                                                  lambda harness=self.claude: detect_upstream(harness))
            # The butler's settings card (docs/butler.md 设置卡): the 设置 card's 管家 tab.
            self.butler_panel = ButlerPanel(self.topics)
            # The bot menu's 设置 (app/settings_hub.py): 管家, 工作区, 脱敏, 代答 and 群聊 on one card, a tab row on top.
            # 节点 (app/nodes.py): the gateway the nodes dial, the hub's own node, their alerts and the 设置 card's 设备
            # tab; no thread and no tab unless COVEY_NODE_LISTEN is set.
            self.nodes = Nodes(self.runtime, self.messenger, self.voice)
            self.settings_hub = SettingsHub(self.runtime, self.messenger, self.voice, self.butler_panel,
                                            self.proxy.panel, self.group_manager, self.workspace_manager,
                                            self.redaction_panel, nodes=self.nodes.panel)
            # 手上的事 (app/overview.py): the card pinned in the owner's private chat, when COVEY_PINNED_OVERVIEW=1.
            self.overview = Overview(self.runtime, self.messenger, self.voice, self.titles)
            # The butler's tools (app/butler_tools.py), unless COVEY_BUTLER=0: the state directory and
            # profile are filled in by build_context, before any thread runs.
            self.butler_env = ButlerEnv(self.runtime, self.topics, self.messenger, self.voice, state_dir=None, profile='',
                                    settings=self.settings_hub)
            if self.butler_paths is not None:
                self.butler_env.state_dir, self.butler_env.profile = self.butler_paths
            # Every tool core/butler_tool_specs.py names, by the module that runs it.
            self.butler_tools = ButlerTools(self.butler_env, handlers={**butler_tools.TOOLS, **butler_search.TOOLS,
                                                                 **butler_topics.TOOLS, **butler_panel.TOOLS,
                                                                 **butler_knowledge.TOOLS, **PROJECT_BUTLER_TOOLS})
            self.butler_search = butler_search.ButlerSearch(self.butler_env)  # Reads the checkpoints topics_search looks into.
            # The memory watchdog's alerts (service/memguard.py), to a task's topic or the owner's private chat.
            self.memguard_alerts = MemguardAlerts(self.runtime, self.messenger, self.voice)
            # 早晚汇总 (app/digest.py), when COVEY_DIGEST_TIMES is set: a card in the owner's private chat.
            self.digest = Digest(self.runtime, self.messenger, self.voice)
            # Its line of old questions has 手上的事's 这些都不用回答了: the click is 手上的事's, then the digest card's.
            self.overview.handed[self.digest.origin] = self.digest.dismissed
            # Which topics the owner has seen (app/topic_seen.py): read receipts, when COVEY_PINNED_TOPICS=1.
            self.topic_seen = TopicSeen(self.runtime, lambda ctx, key, handle: self.consume_events(ctx, key, handle))
            # The Feishu app kept up with the Bridge (app/app_setup.py): what it lacks offered in the owner's private
            # chat, added by the developer console after a QR login.
            self.app_setup = AppSetup(self.runtime, self.messenger, self.voice)
            # The welcome card (app/welcome.py): once per new deployment, at the top of the owner's private chat; its
            # examples are taken as the owner's own messages, through handle_event.
            self.welcome = Welcome(self.runtime, self.messenger, self.voice, self.topics, self.handle_event)
            # The 派发卡 (app/dispatch_cards.py), when COVEY_DISPATCH_CARD=1: the thread that keeps the cards of
            # the work the butler handed to topics up to date; without the switch it has no thread.
            self.dispatch_cards = DispatchCards(self.runtime, self.messenger, self.voice, self.titles)
            self.project_cards = ProjectCards(self.runtime, self.messenger, self.voice, self.titles)  # 专项卡 (COVEY_PROJECTS=1)
            self.projects = Projects(self.butler_env)  # 专项的升级、直接开、降级 (COVEY_PROJECTS=1): the thread that finishes a crashed upgrade
            self.butler_env.projects = self.projects  # What topic_start's ``project`` and the 派发卡's upgrade call (app/butler_topics.py).
            # 整理专项 (app/project_tidy.py), COVEY_PROJECT_TIDY: 话题要点, the real-time and scheduled 整理, its cards' clicks.
            self.project_tidy = ProjectTidy(self.runtime, self.messenger, self.voice, self.butler_env)
            self.butler_env.tidy = self.project_tidy  # The butler's projects_tidy and the web page's ``tidy``.
            self.project_tidy.handed["board"] = self.digest.board_refresh  # A proposal decided on the 看板: it is redrawn.
            # Wakes a project's lead when receipts wait and applies the guards (app/project_loop.py), COVEY_PROJECTS=1.
            self.project_loop = ProjectLoop(self.runtime, self.butler_env, stop_tasks)
            # What the butler would have told the owner, only recorded (app/patrol.py), COVEY_PATROL=shadow; the digest lists it.
            self.patrol = Patrol(self.runtime, self.butler_env)
            # The optional features: each says itself which worker threads (``threads()``: (name, target), each run as
            # ``target(ctx)``) and bot menu items (``menu_items()``: event key -> action) it adds; one off by default adds
            # none of its threads. The order is the threads' (worker_targets) and the menu's. The enabled extensions'
            # (core/extensions.py) come last.
            # The enabled extensions' feature objects also take a top-level message and a card click before the core's
            # (card_routing.py: handle_event, claim) and are closed when the Bridge stops (close).
            if 'extension_features' not in self.__dict__:
                self.extension_features = extensions.features(self)
            # The devices thread (app/devices.py): probes, replays the pending reaps, gc; none while COVEY_DEVICES is off.
            self.devices = Devices(self.runtime)
            # The web workbench's sync (app/web_sync.py): two threads with COVEY_WEB=1, none without.
            self.web_sync = WebSync(self.runtime, self.butler_env, self.web_message)
            # The owner's recalled or edited messages (app/source_watch.py): one thread unless COVEY_SOURCE_WATCH=0.
            self.source_watch = SourceWatch(self.runtime, self.topics, self.messenger, self.voice)
            # 到点回来看 (app/revisits.py): the thread that queues a topic's turn at the time it asked, COVEY_REVISIT=1.
            self.revisits = Revisits(self.runtime, self.topics)
            features = (self.weekly_usage, self.workspace_manager, self.memguard_alerts, self.proxy,
                        self.group_manager, self.overview, self.topic_seen, self.butler_tools,
                        self.butler_search, self.settings_hub, self.butler, self.digest, self.app_setup, self.welcome,
                        self.dispatch_cards, self.project_cards, self.projects, self.project_tidy,
                        self.project_loop, self.patrol, self.devices, self.nodes, self.web_sync, self.source_watch,
                        self.revisits, *self.extension_features)
            # The bot menu's items (app/bot_menu.py): each answers in the owner's private chat, its card
            # replacing the one it sent before, or folding and unfolding it while it is the newest (``menu_folds()``).
            self.menu = BotMenu(self.runtime, INBOUND_DIR,
                                {key: action for feature in features
                                 for key, action in getattr(feature, 'menu_items', dict)().items()},
                                Panels(self.runtime, self.messenger), hints=lambda ctx: known_chats(self.proxy.panel, ctx),
                                folds={key: fold for feature in features
                                       for key, fold in getattr(feature, 'menu_folds', dict)().items()})
            # The menu card pinned in each personal group (app/group_menu.py, core/spaces.py personal_groups).
            self.group_menu = GroupMenu(self.runtime, self.messenger, self.voice, self.menu)
            # 手上的事, the pinned board and 早晚汇总 in each of those groups too (app/group_boards.py).
            self.group_boards = GroupBoards(self.runtime, self.messenger, self.voice, self.titles, self.overview,
                                            digest=self.digest)
            self.features = features  # Last: 'features' in the instance says the build is done.

    def web_message(self, event) -> str:
        """The owner's words from the web page (app/web_ops.py tell_butler) as the message of a Feishu event at the top level of the
        private chat: the same ``handle_event`` (access, the duplicate check on its id, the extensions, the butler). '' when it went in,
        else why not: the butler cannot take it, so it must not become a topic of its own."""
        ctx = self.topics.apps[self.topics.default].harness.context
        if self.butler.app_for(ctx, event, self.runtime.access.is_owner(str(event.get('sender_id') or ''))) is None:
            return '管家现在没有开（私聊是纯话题模式，或管家的 Harness 不可用），话没有发出'
        self.handle_event(event, ctx)
        return ''

    def main(self, argv=None):
        # Task runners rebuild this Bridge from the same arguments.
        self.runtime.cli_argv = list(sys.argv[1:] if argv is None else argv)
        args = self.build_parser().parse_args(argv)
        ctx = self.build_context(args)
        instance = instance_lock("state-" + service_key(ctx.state_dir))
        if instance is None:
            raise SystemExit("another bridge already owns this state directory")
        if cenv.get("COVEY_MANAGED") == "1":
            # Its own log, kept by size: the Manager's file (any release's rotates at each start) keeps only what came
            # before this (repo review E3).
            from covey.core.logs import keep_log
            log = ctx.state_dir / "bridge.log"
            log_to(log)
            threading.Thread(target=keep_log, args=(log, self.runtime.shutdown), name="bridge-log", daemon=True).start()
        self.log_start(ctx)
        # A new release takes over by a restart (the Manager's or a deploy's); there is no in-place reload.
        signal.signal(signal.SIGTERM, self.shutdown_handler)
        signal.signal(signal.SIGINT, self.shutdown_handler)

        from covey.app.health import bridge_snapshot
        from covey.service.boot import serve
        pid_path = ctx.state_dir / "bridge.pid"
        write_private_text(pid_path, str(os.getpid()))
        workers = {}
        threading.Thread(target=serve, args=(instance, lambda: bridge_snapshot(self.runtime, workers),
                                             self.runtime.shutdown),
                         name='bridge-health', daemon=True).start()
        previous_receipts = configure_receipts(ctx.state_dir)
        # Its task runners lock the same cards (repo review E4).
        previous_card_locks = configure_card_locks(ctx.state_dir)
        previous_timing = timing.configure(self.runtime.log)  # Millisecond timing lines (core/timing.py).
        scheduler = None  # Joined only once every worker has started.

        def stop():
            # How a start that got this far stops, finished or failed (target/auth, backend hook, ...), in this
            # order; a step that raises ends the stop there, leaving the ones after it undone.
            # The pid write is undone here, so a failed startup cannot leave a stale pid file behind.
            for step in (
                    self.runtime.shutdown.set,
                    # 提交回答 said its answer is being sent: its follow-up queues the task (saved, so the next
                    # start runs it) before this process goes, rather than leaving that card wrong.
                    lambda: self.card_lanes is not None and self.card_lanes.drain(queues=self.runtime.card_queues),
                    lambda: self.runtime.jobs.cancel_all(),
                    lambda: self.stop_children(),
                    lambda: scheduler is not None and scheduler.join(timeout=15),
                    lambda: remove_pid(pid_path),
                    instance.close,
                    lambda: configure_receipts(previous_receipts),
                    lambda: configure_card_locks(previous_card_locks),
                    lambda: timing.flush(),
                    lambda: timing.configure(previous_timing)):
                step()
        with contextlib.ExitStack() as stopping:
            stopping.callback(stop)
            # Old records migrated before anything reads them (core/migrate.py); once done, one file read.
            from covey.core.migrate import migrate
            migrate(ctx.state_dir, log=self.runtime.log)
            self.butler.settle(ctx, self.runtime.log)  # The private chat mode, once (docs/butler.md 私聊模式).
            self.welcome.settle(ctx)  # Whether this deployment gets its welcome card, once (app/welcome.py).
            if butler_settings.desk_topics_left(ctx.state_dir):  # Retired (docs/butler.md 会话).
                self.runtime.log(butler_settings.DESK_TOPICS_RETIRED)
            from covey.feishu.sdk import enabled, lark_cli_direct
            lark_cli_direct(ctx.profile, self.runtime.log)  # Before any child starts: they inherit it (S48).
            if enabled():
                try:
                    from covey.feishu.sdk import client
                    # Import and credentials here, before card clicks are taken (a task runner builds it on a thread
                    # instead: feishu/sdk.py warm_sdk); its connection and token beside (S48).
                    threading.Thread(target=client(ctx.profile).warm, daemon=True, name=background.WARMUP).start()
                except (OSError, ValueError, ImportError):
                    self.runtime.log('CardKit SDK warmup unavailable; CLI remains available')
            self.init_target(ctx)
            self.harnesses[self.topics.default].ensure_started(ctx)  # The first job does not check again.
            self.log_startup(ctx)
            self.runtime.jobs.counter = max_job_id(ctx.state_dir)
            recover_question_delivery(ctx, [app.questions for app in self.apps.values()], self.runtime.log)
            try:  # Questions an earlier run held for a project's lead, with the question triage off now: the owner's (app/triage.py).
                from covey.app import triage as triage_flow
                triage_flow.settle_closed(self.butler_env)
            except Exception as exc:  # noqa: BLE001 - the start does not depend on it
                self.runtime.log(f"held questions not settled at start: {type(exc).__name__}")
            self.notify_stale_jobs(ctx)

            for name, target in self.worker_targets():
                workers[name] = supervised(self.runtime, name, target, ctx)
            scheduler = workers['scheduler']
            self.consume_loop(ctx)
        self.runtime.log("bridge stopped")
        return 0

    def build_parser(self):
        parser = self.argument_harness.build_parser()
        parser.add_argument('--harness-config', type=Path)
        parser.add_argument('--owner', default='', help='owner open_id in this exact Feishu application')
        parser.add_argument('--app-id', default='', help='pin the expected Feishu application identity')
        parser.add_argument('--pairing-file', type=Path, help='one-time owner pairing record issued by covey_pairing.py')
        return parser

    def build_context(self, args):
        config = read_json(args.harness_config) if args.harness_config else {}
        names = enabled_names(config)
        gateways.configure(config.get('gateways'))  # Before any Harness reads its gateways (core/gateways.py).
        ctx = self.argument_harness.context = self.argument_harness.build_context(args)
        self.owner, self.expected_app_id = args.owner, args.app_id
        self.pairing_file = args.pairing_file
        self.runtime.state_dir = ctx.state_dir
        self.runtime.access.spaces, notes = spaces.parse(config)  # core/spaces.py: the ``spaces`` section.
        for note in notes:
            self.runtime.log(note)
        configure_boards(self.runtime.access.board_groups())  # Each keeps a board record of its own (app/pins.py).
        self.butler_paths = ctx.state_dir, ctx.profile or ''
        if 'butler_env' in self.__dict__:  # Built already (the Bridge's); a task runner's is built later, with these.
            self.butler_env.state_dir, self.butler_env.profile = self.butler_paths
        self.butler.configure(ctx)  # Here, so a task runner's replies stay where the Bridge's do.
        ignored = sorted(set(config.get('harnesses', {})) - set(NAMES))
        if ignored:
            self.runtime.log('harness config ignores Harnesses not in the roster: ' + ', '.join(ignored))
        for name in NAMES[1:]:
            if name not in names:
                continue
            harness = self.harnesses[name]
            options = config.get('harnesses', {}).get(name, {}).get('args', [])
            if not isinstance(options, list) or not all(isinstance(x, str) for x in options):
                raise ValueError('Harness args must be an argv list: ' + name)
            parsed = harness.build_parser().parse_args(options + ['--state-dir', str(ctx.state_dir),
                     '--workdir', str(ctx.default_workdir), '--profile', ctx.profile or ''])
            harness.context = harness.build_context(parsed)
        for name in names:
            first = models_first(config, name)
            if first:
                self.harnesses[name].context.options['models_first'] = first
        # Controllers share these dictionaries: pruning them also restricts launch/switch choices,
        # native catalog probes, old card routes and task-runner reconstruction.
        for name in set(self.harnesses) - set(names):
            del self.harnesses[name]
            del self.apps[name]
        self.topics.default = names[0]
        return self.harnesses[self.topics.default].context

    def init_target(self, ctx):
        status = run_json(lark_argv(ctx.profile, 'auth', 'status', '--verify', '--json'))
        actual = status.get('appId', '')
        if not actual or (self.expected_app_id and actual != self.expected_app_id):
            raise ValueError('profile application identity does not match --app-id')
        path = ctx.state_dir / 'bot-identity.json'
        persisted = read_json(path) if path.exists() else {}
        if persisted and persisted.get('app_id') != actual:
            raise ValueError('state belongs to another Feishu application; use a separate state directory')
        user = status.get('identities', {}).get('user', {})
        owner = self.owner or persisted.get('owner') or (user.get('openId') if status.get('verified') and user.get('status') == 'ready' else '')
        if persisted.get('owner') and owner != persisted['owner']:
            raise ValueError('explicit owner conflicts with the existing bot binding')
        if not owner and not self.pairing_file:
            raise ValueError('owner is not verified; authenticate this profile or supply its explicit --owner')
        if not owner:
            pair = read_json(self.pairing_file)
            if pair.get('app_id') != actual or pair.get('expires_at', 0) <= time.time():
                raise ValueError('pairing record is expired or belongs to another application')
        self.runtime.owner_open_id = owner
        bot_open_id = self.bot_open_id(ctx) or persisted.get('bot_open_id') or ''
        self.runtime.access.identify(app_id=actual, bot_open_id=bot_open_id)
        # The owner's name as his login here gives it, else as an earlier start kept it: app/strangers.py's line.
        named = user.get('userName') if user.get('openId') == owner else None
        name = next((n.strip() for n in (named, persisted.get('owner_name')) if isinstance(n, str) and n.strip()), '')
        save = {'app_id': actual, 'owner': owner, 'profile': ctx.profile, 'schema_version': 1,
                **({'bot_open_id': bot_open_id} if bot_open_id else {}), **({'owner_name': name} if name else {})}
        write_json(path, save)

    def bot_open_id(self, ctx):
        """This bot's open_id from Feishu: group messages count only when they @ it. '' when unread
        (the saved one is used; without one, group messages are ignored until a restart reads it)."""
        try:
            info = run_json(lark_argv(ctx.profile, 'api', 'GET', '/open-apis/bot/v3/info', '--as', 'bot'), timeout=20)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.runtime.log(f'bot identity unavailable: {type(exc).__name__}')
            return ''
        bot = (info.get('data') or info.get('bot')) if isinstance(info, dict) else None
        open_id = bot.get('open_id') if isinstance(bot, dict) else ''
        return open_id if isinstance(open_id, str) and open_id.startswith('ou_') else ''

    def worker_targets(self):
        """The Bridge's worker threads: (name, target), each target run as ``target(ctx)``."""
        return [('scheduler', self.reply_worker_loop), ('cards', self.card_action_loop), ('menu', self.menu_loop),
                ('maintenance', self.maintenance_target), ('connections', self.connections_target),
                ('availability', lambda ctx: watch_availability(self, ctx)),
                ('outbox', lambda ctx: run_outbox(self, ctx, *OUTBOX_STEPS)),
                ('policy', lambda ctx: run_policy_guard(self, ctx)),
                *(thread for feature in self.features for thread in getattr(feature, 'threads', tuple)()),
                *self.group_menu.threads(), *self.group_boards.threads()]

    def maintenance_target(self, ctx):
        """The maintenance thread. Its modules are imported when the thread starts, not when the targets are listed:
        a task runner never lists them, and a test that patches the loop after listing still reaches it."""
        from covey.app.stale import close_stale_records
        from covey.core.maintenance import maintenance_loop
        return maintenance_loop(self.runtime, ctx, close_stale=lambda plan: close_stale_records(self, ctx, plan),
                                prune_web=lambda: self.proxy.web_login.prune_web_caches(ctx))

    def connections_target(self, ctx):
        """The event bus watchdog thread, imported when it starts (as ``maintenance_target``)."""
        from covey.feishu.connections import watch
        return watch(self.runtime, ctx)

    def log_start(self, ctx):
        """The first line of a start: the process, its Manager generation and its code. A start in the same
        generation as the one before is the Manager replacing a Bridge that exited or stopped answering; a
        new one is a deploy or restart (repo review E3: only "received signal 15" was left of it)."""
        from covey.service import manager
        try:
            generation = manager.generation(ctx.state_dir)
        except (OSError, ValueError, KeyError, TypeError):
            generation = "-"  # Not run by a Manager.
        self.runtime.log(f"bridge started pid={os.getpid()} generation={generation} "
                         f"release={self.runtime.script_path.resolve().parents[1].name}")

    def log_startup(self, ctx):
        default = self.apps[self.topics.default]
        self.runtime.log(f"profile={ctx.profile or 'default'} active_workspace={ctx.workspace_name} workdir={ctx.workdir}")
        self.runtime.log("conversation_mode=topic (one Feishu topic per Harness session)")
        from covey.core.ignored_env import ignored_env
        from covey.app.launch_models import start_catalog_refresh
        from covey.app import model_usage
        for line in ignored_env():  # Only here: a task runner builds its context without logging this.
            self.runtime.log(line)
        for line in extensions.unused_env():  # Variables of a feature that was taken out.
            self.runtime.log(line)
        # Which extensions this Bridge runs with, so a deploy check can see them (none: the open core alone).
        self.runtime.log("extensions loaded=" + (",".join(ext.package for ext in extensions.manifests()) or "none"))
        self.runtime.log(
            f"{default.spec.name}_timeout={format_timeout(ctx.timeout)} async_replies=true "
            f"streaming_card={default.cards.streaming}"
        )
        for line in default.backend.startup_lines(ctx):
            self.runtime.log(line)
        self.runtime.log(f"sessions_dir={ctx.sessions_dir} (exists={ctx.sessions_dir.exists()})")
        self.runtime.log(f"target open_id={self.runtime.owner_open_id or '(will learn from first message)'}")
        self.runtime.log(f"history={ctx.history_path}")
        self.runtime.log(f"session_path={ctx.session_path}")
        self.runtime.log(f"state_dir={ctx.state_dir}")
        note = self.butler.harness_note(ctx.state_dir)
        if note:
            self.runtime.log(note)
        start_catalog_refresh(self.topics)
        model_usage.start(ctx.state_dir, self.runtime.log)  # The model selects' order (app/model_usage.py).

    def reply_worker_loop(self, ctx):
        WorkspaceScheduler(self.runtime.shutdown, self.runtime.jobs.queue, lambda job: self.process_reply_job(job, ctx),
                           self.runtime.log).run()

    def consume_events(self, ctx: BridgeContext, key: str, handle) -> None:
        if ws_intake.enabled():  # COVEY_EVENT_TRANSPORT=sdk: one SDK WebSocket for all four keys, no lark-cli consumer.
            return ws_intake.consume(ctx, key, handle=handle, shutdown=self.runtime.shutdown, log=self.runtime.log,
                                     retry_seconds=CARD_ACTION_RETRY_SECONDS)
        consume(ctx, key, handle=handle, set_process=lambda proc: self.event_procs.__setitem__(key, proc),
                shutdown=self.runtime.shutdown, log=self.runtime.log, retry_seconds=CARD_ACTION_RETRY_SECONDS)

    def consume_loop(self, ctx: BridgeContext) -> None:
        self.consume_events(ctx, MESSAGE_EVENT_KEY, self.handle_event)

    def card_action_loop(self, ctx: BridgeContext) -> None:
        if self.card_lanes is None:
            health = consumer(CARD_ACTION_EVENT_KEY)
            health.inline_durations = False  # The lanes report each click's own time.
            self.card_lanes = CardLanes(self.handle_card_action_event, self.card_lane_key, self.runtime.log,
                                        self.runtime.shutdown, health)
        self.consume_events(ctx, CARD_ACTION_EVENT_KEY, self.card_lanes.submit)

    def menu_loop(self, ctx: BridgeContext) -> None:
        self.consume_events(ctx, MENU_EVENT_KEY, self.handle_menu_event)

    def stop_children(self) -> None:
        for feature in (self.butler, *self.extension_features):
            if hasattr(feature, 'close'):
                feature.close()
        stop_processes([(sub.stop_label, self.event_procs.get(key)) for key, sub in SUBSCRIPTIONS.items()],
                       self.runtime.log)
        ws_intake.stop()  # The SDK connection of COVEY_EVENT_TRANSPORT=sdk; nothing when this process opened none.

    def shutdown_handler(self, signum: int, _frame: Any) -> None:
        self.runtime.log(f"received signal {signum}; shutting down")
        self.runtime.shutdown.set()
        self.stop_children()
