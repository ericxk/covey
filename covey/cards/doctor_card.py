"""The 设置 card's 诊断 tab (app/doctor_panel.py): what service/doctor.py found of this deployment, a line a part, in words.

``doctor_rows`` turns the panel's summary (app/doctor_panel.py ``summarize``) into rows (ok, what, detail): the Manager,
this bot's service (its generation, the connections that bring messages and clicks, dropped events, a permission variable
it should not keep), the optional switches it has set (INFO, ``switch_row``), how long Claude Code keeps session transcripts (``cleanup_row``, only with the Claude Harness), start at boot, and each Harness's last probe; ``ok`` None is something to know rather than a fault
(no hosting chosen, never probed), INFO only said (other bots in the root). The title bar says how many need a look and when the report was read. No buttons:
the tab only reads (another click on 诊断 folds it, the next one reads again)."""
from __future__ import annotations

import time

from covey.cards import ui
from covey.cards.elements import md, note, panel_header, styled_panel

TITLE = "诊断"
ICON = "setting_outlined"
NOTE = ("只读：不改任何东西，也不调模型。完整报告在服务器上运行 python3 -B bin/covey_doctor.py。"
        "再点一下「诊断」收起，展开时重新检查。")
HARNESS_NAMES = {"claude": "Claude Code", "codex": "Codex"}
BOOT_MODES = {"systemd-user": "systemd 用户服务", "launchd": "launchd", "cron": "crontab 的 @reboot"}
LOST = {"stalled": "卡住了", "disconnected": "断开了", "unresponsive": "没有回应"}
# The row's dot (core/look.py STATES): green fine, red a fault, orange to know, grey (INFO) only said.
INFO = "info"
MARKS = {True: "result", False: "stopped", None: "waiting", INFO: "idle"}


def clock(stamp) -> str:
    """HH:MM today, MM-DD HH:MM before (docs/ui.md 7: 今天的只写时刻); '' without a time."""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return ""
    today = time.localtime(stamp)[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if today else "%m-%d %H:%M", time.localtime(stamp))


def manager_row(manager) -> tuple:
    if manager.get("ok"):
        lag = manager.get("lag")
        return True, "服务管理器在运行", (f"巡检延迟 {lag} 秒；" if lag is not None else "") + "机器人退出了会被它拉起来"
    return False, "服务管理器没有回应", "机器人退出了没人拉起；在服务器上运行 covey_service.py start 拉起它"


def service_row(service) -> tuple:
    if not service:
        return None, "没找到这个机器人的服务清单", "它不是由服务管理器托管的，挂了不会被拉起，也不会私聊告诉你"
    if service.get("desired") == "stopped":
        return None, "机器人已停用", "covey_service.py start 重新启用"
    problems = []
    if not service.get("alive"):
        problems.append("进程不在")
    if service.get("connection") in LOST:
        problems.append(f"和飞书的连接{LOST[service['connection']]}")
    if service.get("connected") is False:
        problems.append("收消息或卡片点击的连接没就绪")
    if service.get("dropped"):
        problems.append(f"丢了 {service['dropped']} 个事件")
    if service.get("permission_env"):
        problems.append("清单里留着已不生效的权限变量，要重新注册")
    if service.get("bridge") == "degraded" and not problems:
        problems.append("有部件不正常，看状态目录里的 bridge.log")
    generation = service.get("generation")
    lead = f"第 {generation} 代" if generation is not None else ""
    if service.get("status") == "ready" and not problems:
        return True, "机器人正常", "｜".join(filter(None, [lead, "收消息和卡片点击都连着"]))
    return False, "机器人有问题", "｜".join(filter(None, [lead, *problems])) or "看状态目录里的 bridge.log"


def switch_row(service) -> tuple:
    """INFO row: the optional switches this service has set (core/switches.py), names and values as they read there; the tab
    shows nothing more of the environment."""
    pairs = [pair for pair in service.get("switches") or () if isinstance(pair, (list, tuple)) and len(pair) == 2]
    if not pairs:
        return INFO, "没设可选开关", "功能都按默认；开关一览见 docs/configuration.md「开关速查」"
    return INFO, f"设了 {len(pairs)} 个开关", "｜".join(f"{name}={value}" for name, value in pairs)


def cleanup_row(found) -> tuple:
    """Claude Code's keeping of session transcripts (core/claude_retention.py, ``claude_cleanup`` of the summary): to know
    (orange) with the fix when it keeps few days; fine from 90 days; INFO when its settings cannot be read."""
    state, days = found.get("state"), found.get("days")
    if state in ("low", "missing"):
        return None, f"Claude Code 只保留最近 {days} 天的会话", found.get("hint") or ""
    if state == "ok":
        return True, f"Claude Code 会话保留 {days} 天", "更早的话题仍能接着做、能回退一步"
    return INFO, "没读出 Claude Code 保留会话几天", f"{found.get('path') or 'settings.json'} 读不了或不是 JSON；Claude Code 默认只留 30 天"


LEDGER_WORDS = (("waiting", "等你"), ("stopped", "停住了"), ("stuck", "可能卡住"), ("result", "有结果"))


def ledger_row(found) -> tuple:
    """等主人的账 (``owner_ledger`` of the summary, app/owner_ledger.py): what it counted at the last 对账, and the cards it
    sent again; to know (orange) while a card is missing from the chat or a try did not go out."""
    counts = found.get("counts") if isinstance(found.get("counts"), dict) else {}
    said = "、".join(f"{words} {int(counts.get(key) or 0)}" for key, words in LEDGER_WORDS if counts.get(key)) or "没有等你的事"
    tries, failed, missing = int(found.get("tries") or 0), int(found.get("failed") or 0), int(found.get("missing") or 0)
    detail = "｜".join(filter(None, [said, f"{missing} 张卡没到聊天里，对账会补发" if missing else "",
                                     f"补发过 {tries} 次，发出 {int(found.get('ok') or 0)} 次" if tries else "",
                                     f"{clock(found.get('checked_at'))} 对的账" if clock(found.get("checked_at")) else ""]))
    return (None if missing or failed else True), "等你的事都有卡" if not missing else "有卡没到聊天里", detail


def boot_row(boot) -> tuple:
    status, mode = boot.get("status"), boot.get("mode")
    if status == "ready" and boot.get("autostart") is False:
        return None, "开机不会自动拉起", "这台机器没有可用的托管方式；重启机器后要重新运行 ./install"
    if status == "ready":
        return True, "开机会自动拉起", BOOT_MODES.get(mode, "扩展提供的托管")
    if status == "degraded":
        return False, "开机自启没装好", "重新运行 ./install 装好它（docs/operations.md 服务托管）"
    return None, "开机自启没核实", "没找到托管的回执；covey_doctor.py 的 boot 一段说明为什么"


def harness_rows(harnesses) -> list:
    rows = []
    for name, probe in sorted((harnesses or {}).items()):
        shown = HARNESS_NAMES.get(name, name)
        when = clock(probe.get("checked_at"))
        if probe.get("ok"):
            rows.append((True, f"{shown} 能调通", f"{when} 测的" if when else ""))
        else:
            advice = probe.get("advice") or "调用失败"
            rows.append((False, f"{shown} 调不通", "｜".join(filter(None, [advice, f"{when} 测的" if when else ""]))))
    if not rows:
        rows.append((None, "还没测过模型能不能调通", "在服务器上运行 covey_doctor.py --probe-harnesses"))
    return rows


def doctor_rows(data) -> list:
    """(ok, what, detail) of each part of ``data`` (app/doctor_panel.py ``summarize``), in the card's order."""
    if not data:
        return [(None, "还没检查", "再点一下「诊断」")]
    if data.get("error"):
        return [(False, "这次没查成", "读服务配置时出错；在服务器上运行 covey_doctor.py 看完整报告")]
    rows = [manager_row(data.get("manager") or {}), service_row(data.get("service")),
            *([switch_row(data["service"])] if data.get("service") else []),
            *([cleanup_row(data["claude_cleanup"])] if data.get("claude_cleanup") else []),
            *([ledger_row(data["owner_ledger"])] if data.get("owner_ledger") else []), boot_row(data.get("boot") or {}),
            *harness_rows(data.get("harnesses"))]
    if data.get("others"):
        rows.append((INFO, f"同一服务根下还有 {data['others']} 个机器人", "它们的情况见 covey_doctor.py"))
    return rows


def doctor_card(token, view, data, *, top=(), below=()):
    """The tab for ``view`` (its notice) over ``data``; ``top`` is the tab row, ``below`` more elements under the report
    (the 能做什么 part, cards/about_panel.py ``about_elements``)."""
    elements = [*top]
    if view.get("notice"):
        elements.append(ui.receipt(view["notice"]))
    rows = doctor_rows(data)
    for ok, what, detail in rows:
        elements.append(md(f"{ui.dot(MARKS[ok])} **{what}**"))
        if detail:
            elements.append(note(detail))
    elements.append(note(NOTE))
    elements += below
    faults = sum(1 for ok, _, _ in rows if ok is False)
    when = clock((data or {}).get("checked_at"))
    subtitle = "｜".join(filter(None, ["都正常" if not faults else f"{faults} 项要看", f"{when} 查的" if when else ""]))
    return styled_panel(panel_header(ICON, TITLE, subtitle), elements, TITLE)
