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
from covey.app.main_thread import MainThread
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
from covey.feishu.events import (CARD_ACTION_EVENT_KEY, MENU_EVENT_KEY, MESSAGE_EVENT_KEY, SUBSCRIPTIONS,
                                          consume, consumer)
from covey.feishu.messenger import Messenger
from covey.feishu.transport import CliFailed, cli_json, configure_receipts, lark_argv
from covey.core.jobs import max_job_id
from covey.core.roster import NAMES
from covey.core.state import write_private_text, read_json, write_json
from covey.core import background, main_thread_settings, timing
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


class DashboardRotate:
    """PANELS' controller for 换口令 on a 脱敏 link's card (app/dashboard.py matches it); its outcome is
    the Bridge's rotate_dashboard, which also needs the bot menu."""

    def __init__(self, bridge):
        self.bridge = bridge

    def matches(self, raw):
        return self.bridge.dashboard.matches(raw)

    def claim(self, ctx, event, raw, owner):
        return self.bridge.rotate_dashboard(ctx, event, owner, raw) if self.matches(raw) else None


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
    PANELS = ('proxy.panel', 'proxy.resend', 'group_manager', 'dashboard_rotate', 'redaction_panel',
              'main_thread_panel', 'settings_hub', 'overview', 'app_setup', 'group_menu')

    # What only the Bridge uses, built by _build_parts: at once in the Bridge (the default), on first use in a
    # task runner (``task_runner=True``), which rarely meets any of it.
    PARTS = ('directory_picker', 'launch_model_picker', 'workspace_manager', 'harness_switch', 'rewind', 'proxy',
             'dashboard', 'weekly_usage', 'group_manager', 'dashboard_rotate', 'redaction_panel', 'main_thread_panel',
             'settings_hub', 'overview', 'main_env', 'main_tools', 'main_search', 'memguard_alerts', 'digest',
             'topic_seen', 'app_setup', 'dispatch_cards', 'project_cards', 'devices', 'extension_features', 'features', 'menu', 'group_menu', 'group_boards')

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
        self.main_thread = MainThread(self.topics)  # Personal Agent (docs/main-thread.md 私聊模式).
        self.owner = ''
        self.expected_app_id = ''
        self.pairing_file = None
        self.event_procs = {}  # Each event key's running lark-cli consumer (feishu/events.py SUBSCRIPTIONS).
        self.card_lanes = None
        # As the Harnesses were when the Bridge was built: build_context drops the ones a bot does not enable.
        self.titles = {name: h.spec.title for name, h in self.harnesses.items()}
        self.claude = self.harnesses['claude']
        self.main_paths = None  # (state directory, profile) for the main thread's tools, set by build_context
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
        from covey.app.dashboard import Dashboard, detect_upstream
        from covey.app.digest import Digest
        from covey.app.directory_picker import DirectoryPicker
        from covey.app.dispatch_cards import DispatchCards
        from covey.app.project_cards import ProjectCards
        from covey.app.devices import Devices
        from covey.app.group_boards import GroupBoards
        from covey.app.group_manager import GroupManager
        from covey.app.group_menu import GroupMenu
        from covey.app.harness_switch import HarnessSwitch
        from covey.app import main_knowledge, main_search, main_thread_panel, main_tools, main_topics
        from covey.app.main_env import MainEnv
        from covey.app.main_thread_panel import MainThreadPanel
        from covey.app.main_tools import MainTools
        from covey.app.memguard_alerts import MemguardAlerts
        from covey.app.model_picker import LaunchModelPicker
        from covey.app.overview import Overview
        from covey.app.proxy import Proxy
        from covey.app.redaction_panel import RedactionPanel
        from covey.app.rewind import RewindCards
        from covey.app.settings_hub import SettingsHub
        from covey.app.topic_seen import TopicSeen
        from covey.app.weekly_usage import WeeklyUsage
        from covey.app.workspace_manager import WorkspaceManager
        with self.parts_lock:
            if 'features' in self.__dict__:
                return
            self.directory_picker = DirectoryPicker(self.topics)
            self.launch_model_picker = LaunchModelPicker(self.topics)
            self.workspace_manager = WorkspaceManager(self.topics, self.directory_picker.cache)
            self.harness_switch = HarnessSwitch(self.topics)
            self.rewind = RewindCards(self.topics)
            self.proxy = Proxy(self.topics)
            self.dashboard = Dashboard(self.runtime, self.messenger, self.voice,
                                       detect=lambda harness=self.claude: detect_upstream(harness))
            self.weekly_usage = WeeklyUsage(self.runtime, self.messenger, self.voice, self.titles)
            self.group_manager = GroupManager(self.runtime, self.messenger, self.voice)
            self.dashboard_rotate = DashboardRotate(self)
            # The 设置 card's 脱敏 tab (app/redaction_panel.py): the term list on the card, the web dashboard's link last.
            self.redaction_panel = RedactionPanel(self.runtime, self.messenger, self.voice, self.dashboard)
            # The main thread's settings card (docs/main-thread.md 设置卡): the 设置 card's 主线程 tab.
            self.main_thread_panel = MainThreadPanel(self.topics)
            # The bot menu's 设置 (app/settings_hub.py): 主线程, 工作区, 脱敏, 代答 and 群聊 on one card, a tab row on top.
            self.settings_hub = SettingsHub(self.runtime, self.messenger, self.voice, self.main_thread_panel,
                                            self.proxy.panel, self.group_manager, self.workspace_manager,
                                            self.redaction_panel)
            # 手上的事 (app/overview.py): the card pinned in the owner's private chat, when COVEY_PINNED_OVERVIEW=1.
            self.overview = Overview(self.runtime, self.messenger, self.voice, self.titles)
            # The main thread's tools (app/main_tools.py), unless COVEY_MAIN_THREAD=0: the state directory and
            # profile are filled in by build_context, before any thread runs.
            self.main_env = MainEnv(self.runtime, self.topics, self.messenger, self.voice, state_dir=None, profile='',
                                    settings=self.settings_hub)
            if self.main_paths is not None:
                self.main_env.state_dir, self.main_env.profile = self.main_paths
            # Every tool core/main_tool_specs.py names, by the module that runs it.
            self.main_tools = MainTools(self.main_env, handlers={**main_tools.TOOLS, **main_search.TOOLS,
                                                                 **main_topics.TOOLS, **main_thread_panel.TOOLS,
                                                                 **main_knowledge.TOOLS})
            self.main_search = main_search.MainSearch(self.main_env)  # Reads the checkpoints topics_search looks into.
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
            # The 派发活卡片 (app/dispatch_cards.py), when COVEY_MAIN_DISPATCH_CARD=1: the thread that keeps the cards of
            # the work the main thread handed to topics up to date; without the switch it has no thread.
            self.dispatch_cards = DispatchCards(self.runtime, self.messenger, self.voice, self.titles)
            self.project_cards = ProjectCards(self.runtime, self.messenger, self.voice, self.titles)  # 专项卡 (COVEY_PROJECTS=1)
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
            features = (self.weekly_usage, self.workspace_manager, self.memguard_alerts, self.proxy, self.dashboard,
                        self.group_manager, self.overview, self.topic_seen, self.main_tools,
                        self.main_search, self.settings_hub, self.main_thread, self.digest, self.app_setup,
                        self.dispatch_cards, self.project_cards, self.devices,
                        *self.extension_features)
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
            self.group_boards = GroupBoards(self.runtime, self.messenger, self.voice, self.titles, self.overview)
            self.features = features  # Last: 'features' in the instance says the build is done.

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
            self.main_thread.settle(ctx, self.runtime.log)  # The private chat mode, once (docs/main-thread.md 私聊模式).
            if main_thread_settings.desk_topics_left(ctx.state_dir):  # Retired 2026-11-09 (docs/main-thread.md 前台话题).
                self.runtime.log(main_thread_settings.DESK_TOPICS_RETIRED)
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
        self.main_paths = ctx.state_dir, ctx.profile or ''
        if 'main_env' in self.__dict__:  # Built already (the Bridge's); a task runner's is built later, with these.
            self.main_env.state_dir, self.main_env.profile = self.main_paths
        self.main_thread.configure(ctx)  # Here, so a task runner's replies stay where the Bridge's do.
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
        save = {'app_id': actual, 'owner': owner, 'profile': ctx.profile, 'schema_version': 1,
                **({'bot_open_id': bot_open_id} if bot_open_id else {})}
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
        for line in extensions.unused_env():  # Variables of a feature that moved into an extension that is not enabled.
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
        note = self.main_thread.harness_note()
        if note:
            self.runtime.log(note)
        start_catalog_refresh(self.topics)
        model_usage.start(ctx.state_dir, self.runtime.log)  # The model selects' order (app/model_usage.py).

    def reply_worker_loop(self, ctx):
        WorkspaceScheduler(self.runtime.shutdown, self.runtime.jobs.queue, lambda job: self.process_reply_job(job, ctx),
                           self.runtime.log).run()

    def consume_events(self, ctx: BridgeContext, key: str, handle) -> None:
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
        for feature in (self.main_thread, *self.extension_features):
            if hasattr(feature, 'close'):
                feature.close()
        stop_processes([(sub.stop_label, self.event_procs.get(key)) for key, sub in SUBSCRIPTIONS.items()],
                       self.runtime.log)

    def shutdown_handler(self, signum: int, _frame: Any) -> None:
        self.runtime.log(f"received signal {signum}; shutting down")
        self.runtime.shutdown.set()
        self.stop_children()
