"""A device's self-check: ``self_check`` connects, pushes the shim (and the Codex reader), says hello, imports the
environment profile and (with ``probe_harness``, which ``check_device`` and ``bin/covey_devices.py check`` pass) runs
one minimal real request per declared Harness on the device, with the profile's environment. Its outcome, in words
(``CheckResult.explain``), is kept in ``profile.json`` (core/devices.py ``build_profile``) and read back by
``status_text`` (the workspace card's device line, ``service/doctor.py``). The command line is service/device_check.py.

Standard library plus core/devices and what it imports."""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.core import devices, harness_probe
from covey.core.device_transport import LinkError, ShimError
from covey.core.state import read_object, write_json


LOGIN_ENV_SCRIPT = ["sh", "-c", 'exec "${SHELL:-sh}" -lc env']
READER_FILE = Path(__file__).resolve().parents[1] / "backends" / "codex" / "workspace_reader.py"
PROBE_SECONDS = 60
PROBE_OUTPUT = 1 << 16
LOGIN_HELP = {"claude": "在设备上运行 claude，用 /login 登录", "codex": "在设备上运行 codex login"}


@dataclasses.dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False


def proxy_names(env):
    """The proxy variables (names only) a mapping of environment variables sets."""
    return [name for name in devices.PROXY_URLS if (env or {}).get(name)]


def explain_probe(alias, harness, answer, env):
    """What a failed minimal request means for the one who sets the device up, in words. ``answer`` is a
    ``harness_probe`` result, ``env`` the environment the request ran with (what ``devices.effective_env`` gave)."""
    reason, detail = answer.get("reason", "other"), (answer.get("detail") or "").strip()
    if reason == "not_installed":
        return "%s 上没有 %s（PATH 和常见安装目录里都找不到）。" % (alias, harness)
    if reason == "not_logged_in":
        return "%s 上的 %s 没有登录：%s，再自检。" % (alias, harness, LOGIN_HELP.get(harness, "先在设备上登录"))
    if reason == "timeout":
        return "%s 上的 %s 在 %d 秒内没有答复（出网慢，或被代理卡住）。" % (alias, harness, PROBE_SECONDS)
    if reason == "network":
        proxies = proxy_names(env)
        if not proxies:
            return ("%s 上的 %s 连不上模型服务，而且环境档案里没有代理（HTTPS_PROXY）。设备出网要代理的话，在设备的 "
                    "~/.claude/settings.json 的 env 里设 HTTPS_PROXY，或写进 devices.json 里这台设备的 env，再自检。"
                    % (alias, harness))
        return ("%s 上的 %s 连不上模型服务；已带代理（%s）仍然不通：确认这个代理从 %s 上访问得到，或在 devices.json "
                "的 env 里改。" % (alias, harness, "、".join(proxies), alias))
    return "%s 上的 %s 最小请求失败：%s" % (alias, harness, detail or "没有输出")


def probe_harness(transport, device, name, profile, *, timeout=PROBE_SECONDS):
    """The one real minimal request of a Harness on a device (``self_check``'s ``probe``): the same call as
    ``core/harness_probe.py`` makes here (``claude -p``, ``codex exec``), run by the shim's ``exec`` on the device with
    the profile's environment (``devices.effective_env``). ``{"ok", "reason", "detail", "seconds"}``; ``reason`` is one of
    ``harness_probe.REASONS``."""
    info = (profile.get("tools") or {}).get(name) or {}
    if not info.get("found") or not info.get("path"):
        return {"ok": False, "reason": "not_installed", "detail": "%s not found on the device" % name, "seconds": 0.0}
    argv = harness_probe.command_for(name, info["path"])
    started = time.monotonic()
    answer = transport.call("exec", argv=argv, env=devices.effective_env(device, profile), cwd="~", timeout=timeout,
                            max_out=PROBE_OUTPUT, patience=timeout + 30.0)
    spent = round(time.monotonic() - started, 1)
    if answer.get("timed_out"):
        return {**harness_probe._result(False, "timeout", "no answer within %d s" % timeout), "seconds": spent}
    output = (answer.get("stderr") or "") + "\n" + (answer.get("out") or "")
    return {**harness_probe.classify(output, answer.get("code", 1)), "seconds": spent}


@dataclasses.dataclass
class CheckResult:
    alias: str
    steps: list
    hello: dict | None = None
    profile: dict | None = None
    env: dict = dataclasses.field(default_factory=dict)  # What the minimal requests ran with: for the words.

    @property
    def ok(self):
        return all(step.ok or step.skipped for step in self.steps)

    @property
    def failed(self):
        return next((step for step in self.steps if not (step.ok or step.skipped)), None)

    def lines(self):
        """One line per step, for ``bin/covey_devices.py check``."""
        return ["%s %-9s %5.1fs  %s" % ("ok " if step.ok else "-- " if step.skipped else "FAIL", step.name,
                                        step.seconds, step.detail) for step in self.steps]

    def explain(self):
        """The outcome in words: one line when it passed (what was found, and that the minimal requests passed), else
        what failed first and what to do about it, a line each for every failed step."""
        if self.ok:
            tools = ((self.profile or {}).get("tools") or {})
            found = " · ".join("%s %s" % (name, short_version(name, info)) for name, info in tools.items()
                               if info.get("found")) or "没有声明 Harness"
            probed = [step.name.split(":", 1)[1] for step in self.steps if step.name.startswith("probe:") and step.ok]
            return "%s 自检通过：%s%s" % (self.alias, found,
                                        "；%s 的最小请求都通过了" % "、".join(probed) if probed else "（没有真跑最小请求）")
        return "\n".join(self._failure(step) for step in self.steps if not (step.ok or step.skipped))

    def _failure(self, step):
        name, detail, alias = step.name, step.detail, self.alias
        if name == "connect":
            return ("连不上 %s：%s。自检用 ssh BatchMode（从不问密码）：设备要能免密登录，known_hosts 里要有它的主机密钥"
                    "（先在 Bridge 所在机器上手动 ssh 一次）。" % (alias, detail))
        if name == "shim":
            return "%s 上放不下设备端程序：%s" % (alias, detail)
        if name == "hello":
            return "%s 上的设备端程序没有正常答复：%s。设备上要有 Python 3.9 以上（devices.json 的 ssh.python）。" % (alias, detail)
        if name == "profile":
            return "%s 的环境档案没取成：%s" % (alias, detail)
        if name == "tools":
            return "%s 上没装 %s。" % (alias, detail.rpartition(":")[2].strip())
        harness = name.split(":", 1)[1]
        answer = (self.profile or {}).get("probe", {}).get(harness)
        return explain_probe(alias, harness, answer, self.env) if answer else "%s 上的 %s 最小请求没做成：%s" % (
            alias, harness, detail)


def short_version(name, info):
    """A tool's version as a short word: the first number-led part of what ``--version`` printed."""
    text = str(info.get("version") or "")
    return next((word for word in text.replace(",", " ").split() if word[:1].isdigit()), text[:20] or "?")


def self_check(config, alias, *, state_dir=None, transport=None, probe=None, now=time.time, data=None):
    """Check a device end to end and record what was learned.

    Steps, stopping at the first that fails: ``connect`` (a trivial command over ssh, BatchMode), ``shim`` (push
    ``core/device_shim.py``, and ``backends/codex/workspace_reader.py`` when Codex is declared, unless the device has
    them), ``hello`` (the device answers with the shim we pushed), ``profile`` (tools, logins and the environment that
    passed the whitelist; written with ``hello`` to ``<state>/devices/<alias>/`` when ``state_dir`` is given), ``tools``
    (every Harness the device declares is installed) and ``probe:<harness>`` (``probe(transport, device, harness,
    profile)`` -> ``{"ok", "reason", "detail"}``, the one real minimal request: ``probe_harness``; without a ``probe``
    these are skipped, not failed). ``profile.json`` also gets ``check`` (when, whether it passed, ``explain()``) -- a
    check that fails before the profile step only updates the ``check`` of a profile that is already there.
    ``transport`` defaults to ``devices.open_transport`` (needs ``state_dir``)."""
    device = config.device(alias)
    data = devices.shim_bytes() if data is None else data
    result = CheckResult(alias, [])
    if transport is None:
        if state_dir is None:
            raise ValueError("self_check needs a state_dir or a transport")
        transport = devices.open_transport(config, alias, state_dir)
    try:
        _run_check(config, device, transport, data, probe, now, state_dir, result)
    finally:
        _record_check(state_dir, alias, result, now)
    return result


def _run_check(config, device, transport, data, probe, now, state_dir, result):
    alias = device.alias

    def step(name, action):
        started = time.monotonic()
        try:
            detail = action() or ""
            result.steps.append(Step(name, True, detail, time.monotonic() - started))
            return True
        except (LinkError, ShimError, OSError) as exc:
            result.steps.append(Step(name, False, str(exc)[:300], time.monotonic() - started))
            return False

    def connect():
        code, out, err = transport.shell("echo covey-connected", patience=config.connect_seconds + 5)
        if "covey-connected" not in out:
            raise LinkError("the device did not answer (exit %s): %s" % (code, (err or out).strip()[-200:]))
        return "ssh BatchMode"

    def push():
        state, path = devices.push_file(transport, data)
        if "codex" in device.harnesses:
            reader, _ = devices.push_file(transport, READER_FILE.read_bytes(), "workspace_reader.py")
            return "%s %s · reader %s" % (state, path, reader)
        return "%s %s" % (state, path)

    hello = {}

    def say_hello():
        hello.update(transport.hello(device.harnesses))
        if hello.get("shim") != devices.shim_id(data):
            raise LinkError("the shim that answered (%s) is not the one pushed (%s)" % (hello.get("shim"), devices.shim_id(data)))
        return "%s %s, python %s" % (hello.get("os"), hello.get("machine"), ".".join(map(str, hello.get("python") or [])))

    def make_profile():
        login = {}
        if device.login_shell:
            answer = transport.call("exec", argv=LOGIN_ENV_SCRIPT, timeout=20, max_out=1 << 18, patience=40.0)
            login = devices.parse_env_lines(answer.get("out") or "")
        result.profile = devices.build_profile(device, hello, login_env=login, now=now())
        result.env = devices.effective_env(device, result.profile)
        if state_dir is not None:
            devices.write_hello(state_dir, alias, hello, now=now())
            write_json(devices.device_dir(state_dir, alias) / "profile.json", result.profile, ensure_ascii=False)
        rejected = result.profile["env_rejected"]
        return "%d variable(s)%s" % (len(result.profile["env"]), ", %d refused" % len(rejected) if rejected else "")

    def tools():
        missing = [name for name in device.harnesses if not (result.profile["tools"].get(name) or {}).get("found")]
        if missing:
            raise LinkError("not installed on the device: %s" % ", ".join(missing))
        return ", ".join("%s %s" % (name, result.profile["tools"][name].get("version", "")) for name in device.harnesses)

    if not step("connect", connect) or not step("shim", push) or not step("hello", say_hello):
        return
    result.hello = dict(hello)
    if not step("profile", make_profile):
        return
    step("tools", tools)
    for name in device.harnesses:
        if probe is None:
            result.steps.append(Step("probe:" + name, False, "no probe supplied", 0.0, skipped=True))
            continue

        def run_probe(name=name):
            answer = probe(transport, device, name, result.profile) or {}
            result.profile["probe"][name] = answer
            if state_dir is not None:
                write_json(devices.device_dir(state_dir, alias) / "profile.json", result.profile, ensure_ascii=False)
            if not answer.get("ok"):
                raise LinkError("%s: %s" % (answer.get("reason", "other"), answer.get("detail", "")))
            return "ok"

        step("probe:" + name, run_probe)


def _record_check(state_dir, alias, result, now):
    """The outcome of ``result`` in ``profile.json`` (``check``): on the profile this check made, else on the one that
    is already there (no record at all when there is neither)."""
    if state_dir is None:
        return
    path = devices.device_dir(state_dir, alias) / "profile.json"
    profile = result.profile if result.profile is not None else read_object(path)
    if not profile:
        return
    failed = result.failed
    profile["check"] = {"at": now(), "ok": result.ok, "failed": failed.name if failed else "", "text": result.explain()}
    write_json(path, profile, ensure_ascii=False)


def check_device(config, alias, *, state_dir, transport=None, now=time.time):
    """``self_check`` with the real minimal request (``probe_harness``): what ``bin/covey_devices.py check`` runs."""
    return self_check(config, alias, state_dir=state_dir, transport=transport, probe=probe_harness, now=now)


def age_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "刚刚"
    if seconds < 5400:
        return "%d 分钟前" % round(seconds / 60)
    if seconds < 129600:
        return "%d 小时前" % round(seconds / 3600)
    return "%d 天前" % round(seconds / 86400)


def status_text(state_dir, alias, *, now=None):
    """One line on a device's last self-check, from ``profile.json`` (read here, never asked of the device): the
    workspace card's device line and ``service/doctor.py`` print it."""
    profile = devices.read_profile(state_dir, alias)
    check = profile.get("check") if isinstance(profile.get("check"), dict) else None
    if not check:
        return "还没自检：在 Bridge 所在机器上运行 covey_devices.py check %s" % alias
    when = age_text((now if now is not None else time.time()) - float(check.get("at") or 0))
    if check.get("ok"):
        tools = (profile.get("tools") or {})
        found = " · ".join("%s %s" % (name, short_version(name, info)) for name, info in tools.items() if info.get("found"))
        return "自检通过（%s）%s" % (when, " · " + found if found else "")
    return "自检未通过（%s）：%s" % (when, str(check.get("text") or "").replace("\n", " ")[:160])


# ---------------------------------------------------------------- browsing and models on the device
