"""The Manager tells the owner when his bot is down (COVEY_DOWN_NOTICE in the service's manifest, on unless 0): one plain
message through the bot's own lark-cli profile (no other bot), and one more once it is back.

- Down: the Manager failed to start the Bridge CRASHES times in a row (its ``failures``), the same pass error kept it from
  starting for ERROR_AFTER seconds (a missing workdir or interpreter, a live unmanaged PID), or the Bridge runs but has not
  been receiving Feishu messages for OFFLINE_AFTER seconds (its health snapshot: the connection lost, or the message
  consumer not ready).
- Back: once the owner was told, the Bridge running with a ready health snapshot for BACK_AFTER seconds.
- At most one down message every COOLDOWN seconds for a service: one that falls due sooner waits, and is sent then only
  when it is still down. A service stopped on purpose says nothing more; with the switch off the Manager does not call
  this at all (nothing read, written or sent: as before).
- The message goes to the owner of ``<state>/bot-identity.json`` (written by the Bridge at its start; nothing is sent
  before there is one), as ``im +messages-send --user-id … --as bot`` in the service's own environment (its PATH and
  proxy), under an idempotency key of its own: a try that may have landed is tried again with the same key, which
  Feishu sends once within the hour, and no more after RETRY_WINDOW. The intent is saved before each try
  (<root>/notices/<key>.json), so a Manager that restarts finishes what its predecessor began, never twice.
- The send runs in a thread of its own: a pass of the Manager never waits for lark-cli (the boot watchdog replaces a
  Manager whose loop stalls). Nothing here stops what the Manager does: every failure is a line of manager.log.

Standard library and core only, like the Manager (it never loads the app stack).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time

from covey.core.process_env import env_flag
from covey.core.state import read_json, write_durable_json

SWITCH = "COVEY_DOWN_NOTICE"
CRASH, ERROR, OFFLINE = "crash", "error", "offline"
CRASHES = 3  # Failed starts in a row (the Manager's ``failures``).
ERROR_AFTER = 120  # Seconds a pass error kept the Bridge from starting.
OFFLINE_AFTER = 600  # Seconds the Bridge runs without receiving messages.
BACK_AFTER = 300  # Seconds it must run fine before it counts as back (the Manager's own stable run).
COOLDOWN = 3600  # Seconds between two down messages for one service.
RETRY_AFTER = 120  # Seconds between two tries of one message.
RETRY_WINDOW = 3000  # Seconds a message is tried for: within Feishu's one-hour idempotency window.
SEND_SECONDS = 30  # The lark-cli call's limit.
FOLDER = "notices"
LOST = frozenset({"stalled", "disconnected", "unresponsive"})  # feishu/connections.py, feishu/ws_intake.py.
RECEIVE = "im.message.receive_v1"
OWNER = re.compile(r"ou_[A-Za-z0-9_-]+")
ERROR_CHARS = 160


def enabled(manifest) -> bool:
    """Whether the service's manifest leaves the notice on (COVEY_DOWN_NOTICE unset, or anything but 0/false/no/off)."""
    environment = manifest.get("environment")
    return env_flag(SWITCH, True, environment if isinstance(environment, dict) else {})


def offline(health) -> bool:
    """Whether a Bridge's health snapshot (app/health.py) says Feishu's messages cannot reach it: the connection is lost,
    or the message consumer is not ready. A snapshot that did not come (``unavailable``) says nothing: a Bridge that does
    not answer is replaced by the Manager's health timeout."""
    if not isinstance(health, dict) or health.get("status") not in ("ready", "degraded"):
        return False
    connection = health.get("remote_connection")
    consumers = health.get("consumers")
    receiving = consumers.get(RECEIVE) if isinstance(consumers, dict) else None
    return ((isinstance(connection, dict) and connection.get("status") in LOST)
            or (isinstance(receiving, dict) and not receiving.get("ready")))


def problem(status, failed, alive) -> str | None:
    """What keeps the service down in this pass, or None. A Bridge whose process is ``alive`` is down only when it is
    OFFLINE (running, and its snapshot says ``offline``): a pass that failed to write its status (a full disk) has not
    stopped it. Else ERROR (``failed``, the pass's error), or CRASH (CRASHES failed starts in a row)."""
    if alive:
        return OFFLINE if status.get("status") == "running" and offline(status.get("health")) else None
    if failed:
        return ERROR
    return CRASH if int(status.get("failures") or 0) >= CRASHES else None


def fine(status) -> bool:
    """The Bridge runs and its snapshot is ready."""
    health = status.get("health")
    return (status.get("status") == "running" and isinstance(health, dict) and health.get("status") == "ready"
            and not offline(health))


def minutes(seconds) -> int:
    return max(1, round(seconds / 60))


def down_text(kind, status, failed, lasted) -> str:
    """The down message: what is wrong, what the owner sees, what happens next, where to look."""
    if kind == CRASH:
        code = status.get("returncode")
        return (f"⚠️ 机器人没起来：服务管理器已经连续 {int(status.get('failures') or 0)} 次启动它都失败了"
                + (f"（最近一次退出码 {code}）" if code is not None else "") + "，你现在发的消息没人接。"
                "它会继续自动重试，好了再告诉你。排查：在服务器上运行 covey_doctor.py，或看状态目录里的 "
                "bridge-console.log 和 bridge.log。")
    if kind == ERROR:
        said = " ".join(str(failed).split())
        said = said if len(said) <= ERROR_CHARS else said[:ERROR_CHARS - 1] + "…"
        return (f"⚠️ 服务管理器已经 {minutes(lasted)} 分钟启动不了机器人（{said}）。要是没有别的进程在运行它，"
                "你现在发的消息没人接。它会继续重试，好了再告诉你。排查：在服务器上运行 covey_doctor.py，"
                "或看服务根里的 manager.log。")
    return (f"⚠️ 机器人已经 {minutes(lasted)} 分钟收不到飞书消息了（和飞书的连接断开或卡住），你现在发的消息可能没人接。"
            "它会自己重连，好了再告诉你。一直不好就在服务器上运行 covey_doctor.py。")


def back_text(kind, lasted) -> str:
    what = "又收得到消息了" if kind == OFFLINE else "又在运行了"
    return (f"✅ 机器人恢复了：{what}（前后约 {minutes(lasted)} 分钟）。这段时间发的消息要是没人回，请再发一次。")


def idempotency_key(key, what, since) -> str:
    """The message's key (lark-cli takes at most 50 characters): one per service, message and episode."""
    return "covey-down-" + hashlib.sha256(f"{key}:{what}:{since}".encode()).hexdigest()[:32]


def delivered(proc) -> tuple[bool, str]:
    """(whether lark-cli says the message went out, what to log otherwise: its exit code and Feishu's error type and
    code, never the message or the owner)."""
    try:
        payload = json.loads(proc.stdout or "")
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return False, f"rc={proc.returncode}"
    if proc.returncode == 0 and payload.get("ok") is not False and payload.get("code", 0) in (0, "0"):
        return True, ""
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    return False, " ".join(f"{name}={str(error[name])[:40]}" for name in ("type", "code") if error.get(name) not in (None, ""))\
        or f"rc={proc.returncode}"


class DownNotice:
    """The notices of one Manager. ``say`` writes a line of manager.log; ``environment(manifest)`` is the environment
    the service runs with (its PATH finds lark-cli, its proxy reaches Feishu); ``clock`` is time.time."""

    def __init__(self, root, say, environment, clock=time.time):
        self.root, self.say, self.environment, self.clock = Path(root), say, environment, clock
        self.watch = {}  # service key -> {"kind", "since", "fine_since"}: what this pass and the ones before saw.
        self.states = {}  # service key -> its record (``load``), kept here once read and written at each change.
        self.flight = {}  # service key -> the thread sending its message.
        self.outcomes = {}  # service key -> (delivered, what to log), from a finished thread.
        self.lock = threading.Lock()
        self.reported = {}  # service key -> the last line said about it that is not news when it repeats.

    def path(self, key) -> Path:
        return self.root / FOLDER / (key + ".json")

    def load(self, key) -> dict:
        """Service ``key``'s record: ``told`` (the down message the owner got, until its back message), ``last_down``
        (when the last down message went out) and ``sending`` (a message being tried); read once, then kept."""
        if key not in self.states:
            state = read_json(self.path(key), {})
            self.states[key] = state if isinstance(state, dict) else {}
        return self.states[key]

    def save(self, key, state) -> None:
        """Write ``state`` as service ``key``'s record. A disk that takes no writes (a full quota, when a Bridge is the
        likeliest to fail) never holds a message back: the record stays in memory and the failure is one line."""
        self.states[key] = state
        try:
            path = self.path(key)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_durable_json(path, state)
        except OSError as exc:
            self.once(key, f"notice record not written: {exc}")

    def once(self, key, line) -> None:
        """``line`` about service ``key``, unless it was the last one said about it."""
        if self.reported.get(key) != line:
            self.reported[key] = line
            self.say(f"service {key[:8]}: {line}")

    def check(self, manifest, status, failed=None, alive=False) -> None:
        """One pass over the service of ``manifest``: its ``status`` as the Manager saved it, ``failed`` the error its
        pass ended with (None when it did not), ``alive`` whether the process the status names runs."""
        key, now = manifest["id"], self.clock()
        if key in self.flight and not self.flight[key].is_alive():
            self.flight.pop(key).join()
        if key in self.flight:
            return  # A message is on its way: its outcome first.
        state = self.load(key)
        with self.lock:
            outcome = self.outcomes.pop(key, None)
        if outcome is not None:
            state = self.settle(key, state, outcome, now)
        if manifest.get("desired_state") != "running":
            self.watch.pop(key, None)
            if state.get("told") or state.get("sending"):
                state.pop("told", None)
                state.pop("sending", None)
                self.save(key, state)  # Stopped on purpose: no word of it coming back.
            return
        watch = self.watch.setdefault(key, {})
        kind = problem(status, failed, alive)
        if kind:
            if watch.get("kind") != kind:
                watch.update(kind=kind, since=now)
            watch["fine_since"] = None
        else:
            watch.pop("kind", None)
            watch.pop("since", None)
            watch["fine_since"] = (watch.get("fine_since") or now) if fine(status) else None
        sending, told = state.get("sending"), state.get("told")
        if sending:
            if now - float(sending.get("first") or 0) > RETRY_WINDOW:
                self.give_up(key, state, sending, now)
            elif now - float(sending.get("last") or 0) >= RETRY_AFTER:
                self.start(key, manifest, state, sending, now)
            return
        if told:
            if watch.get("fine_since") is not None and now - watch["fine_since"] >= BACK_AFTER:
                since = float(told.get("since") or now)
                self.begin(key, manifest, state, "back", told.get("kind") or CRASH, since,
                           back_text(told.get("kind"), watch["fine_since"] - since), now)
            return
        wait = {CRASH: 0, ERROR: ERROR_AFTER, OFFLINE: OFFLINE_AFTER}
        if kind and now - watch["since"] >= wait[kind] and now - float(state.get("last_down") or 0) >= COOLDOWN:
            self.begin(key, manifest, state, "down", kind, watch["since"],
                       down_text(kind, status, failed, now - watch["since"]), now)

    def owner(self, manifest) -> str:
        """The owner's open_id from the Bridge's record of its bot (app/unified.py init_target); '' before there is one."""
        found = read_json(Path(manifest["state_dir"]) / "bot-identity.json", {})
        owner = found.get("owner") if isinstance(found, dict) else None
        return owner if isinstance(owner, str) and OWNER.fullmatch(owner) else ""

    def begin(self, key, manifest, state, what, kind, since, text, now) -> None:
        owner = self.owner(manifest)
        if not owner:
            self.once(key, f"the owner is not told it is {'back' if what == 'back' else 'down'}: "
                           "no owner in bot-identity.json yet")
            return
        sending = {"what": what, "kind": kind, "since": since, "key": idempotency_key(key, what, since),
                   "owner": owner, "text": text, "first": now, "last": now, "tries": 0}
        self.start(key, manifest, state, sending, now)

    def start(self, key, manifest, state, sending, now) -> None:
        """One try of ``sending``: the intent saved first, then lark-cli in a thread of its own."""
        sending = {**sending, "last": now, "tries": int(sending.get("tries") or 0) + 1}
        state["sending"] = sending
        self.save(key, state)
        profile = str(manifest.get("profile") or "")
        argv = ["lark-cli", *(["--profile", profile] if profile else []), "im", "+messages-send",
                "--user-id", sending["owner"], "--text", sending["text"], "--as", "bot",
                "--idempotency-key", sending["key"], "--format", "json"]
        thread = threading.Thread(target=self.send, args=(key, argv, self.environment(manifest), manifest["state_dir"]),
                                  name=f"down-notice-{key[:8]}", daemon=True)
        self.flight[key] = thread
        thread.start()

    def send(self, key, argv, env, cwd) -> None:
        try:
            proc = subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=SEND_SECONDS, check=False)
            outcome = delivered(proc)
        except subprocess.TimeoutExpired:
            outcome = (False, "timeout")
        except (OSError, ValueError) as exc:
            outcome = (False, type(exc).__name__)
        with self.lock:
            self.outcomes[key] = outcome

    def settle(self, key, state, outcome, now) -> dict:
        """``state`` once the try of its ``sending`` came out as ``outcome``."""
        sending = state.get("sending")
        if not sending:
            return state
        ok, detail = outcome
        if not ok:
            self.say(f"service {key[:8]}: the {sending['what']} message to the owner did not go out "
                     f"(try {sending.get('tries')}: {detail}); trying again in {RETRY_AFTER}s")
            return state
        state.pop("sending")
        if sending["what"] == "down":
            state["told"] = {"kind": sending["kind"], "since": sending["since"], "at": now}
            state["last_down"] = now
            self.say(f"service {key[:8]}: told the owner it is down ({sending['kind']})")
        else:
            state.pop("told", None)
            self.say(f"service {key[:8]}: told the owner it is back ({sending['kind']})")
        self.save(key, state)
        return state

    def give_up(self, key, state, sending, now) -> None:
        """No more tries of ``sending``: a down message that never went out tells nothing to take back (and the cooldown
        holds the next one); a back message closes its episode all the same."""
        state.pop("sending", None)
        if sending.get("what") == "down":
            state["last_down"] = now
        else:
            state.pop("told", None)
        self.save(key, state)
        self.say(f"service {key[:8]}: the {sending.get('what')} message to the owner was not sent after "
                 f"{sending.get('tries')} tries; given up")

    def wait(self, timeout=None) -> None:
        """Until the messages on their way have their outcome (tests)."""
        for thread in list(self.flight.values()):
            thread.join(timeout)
