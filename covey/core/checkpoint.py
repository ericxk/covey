"""Durable task checkpoints written by the agent and read by the bridge."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from covey.core.paths import expand_user
from covey.core.state import utc_now_iso, write_private_text


def read_checkpoint(path: Path) -> dict:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                text = stream.read()
        else:
            text = path.read_text()
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def release_user_wait(path: Path, *, drop_questions: bool = False) -> None:
    """The checkpoint at ``path`` no longer waits for the user; ``drop_questions`` also takes its form away."""
    data = read_checkpoint(path)
    data.update(requires_user=False, question="")
    if drop_questions:
        data.pop("questions", None)
    write_private_text(path, json.dumps(data, ensure_ascii=False))


# The same for every turn: it names no file, each turn's message gives the checkpoint file.
CHECKPOINT_RULES = (
    "这是执行约定：对于多阶段或长时间任务，在开始执行、完成阶段、提交外部任务及等待用户时，"
    "将完整 JSON 写入本轮消息中“本任务检查点文件”给出的文件；原生工具支持时优先使用原子替换。"
    "检查点由桥接器读取、不在工作区内，写它不算修改工作区文件；用户要求不改文件时也照常写检查点。"
    "若用户禁止 CLI 或工具不支持原子替换，可用原生文件工具完整覆盖，尽可能回读核验；不要仅为原子性额外请求用户放开工具权限。"
    "字段：phase（当前阶段，用用户的语言写成一句短语，任务卡状态行会显示它，不要写 waiting_for_x 这类英文标识），completed（已完成步骤列表），"
    "artifacts（产物绝对路径列表），external_tasks（外部任务 ID/查询方式列表），"
    "next_step（下一步），requires_user（布尔值），question（待用户回答的问题摘要），updated_at（ISO 时间）。"
    '有多个待回答项时，另写 questions 数组，每题单独一个对象：'
    '{"id":"稳定且唯一的标识","header":"短标题","question":"完整问题","options":[{"label":"选项","description":"说明"}]}。'
    '可以多选的题另加 "multiple":true，问题卡才给多选下拉框，否则只能单选。'
    "自由回答可省略 options；不要把多个独立决策合并进单个 question 输入框。"
    "必须保留非空 question 摘要。正常答题时，全部问题得到明确回答后再继续。"
    "只有一个待回答问题时，可告知用户直接回复本话题；有多个问题时，正常回答需在问题卡片中逐题作答并点击“提交回答”。"
    "用户也可以不逐题回答、只发补充说明（反馈原问题或更改需求）；收到这种回答时应重新评估，"
    "不能把未回答部分视为同意或授权，也不能将补充说明仅当作漏答而坚持要求填完原表单。"
    "在等待长命令期间也应保留查询外部任务的方法。需要用户决策时保存 requires_user=true 和 question。最终回复先交代已形成的方案、依据和必要的决策背景，再列出待确认问题；没有形成方案或说明时应明确说明缺少什么，不得声称已有方案或编造内容。"
    "不要在后端进程中无限等待输入。恢复或重试时先检查工作目录、已有产物与外部任务状态；"
    "不得仅凭旧步骤描述就假定成功，不得重复提交已存在的训练/部署等外部任务。"
    "没有检查点时也必须先核查现有状态，再决定继续哪些步骤。不要把密钥写入检查点。"
    "用户要求交付文件时，完成文件并在 artifacts 中登记绝对路径。桥接任务卡提供发送文件入口；"
    "后端不需要单独的飞书附件工具，不要因此设置 requires_user=true 或请求开启消息工具。"
    "登记成功只能说明文件已准备好，不代表附件已经发送；实际发送由任务卡处理。"
    '要用户马上看的图片（登录二维码、要他确认的截图）写进 show_images：[{"path":"图片绝对路径","note":"一句说明","urgent":true}]，'
    "桥接器读到检查点就把图发到本话题，urgent 的同时发到私聊聊天区；同一路径的文件改写后会再发一次。"
    "这只在本轮还在执行时生效：等用户扫码或确认时让本轮保持运行。"
    "不要自己用 lark-cli 或别的工具往飞书发消息、图片或文件：你拿不到本话题的正确位置，会发到别处，用户看不到。"
    '长任务另写 steps 有序数组，每项 {"title":"短语","state":"done|doing|todo"}，同时只有一个 doing，开始和每完成一步都更新。'
    '只有这一轮改了代码、有提交时才写 delivery：{"commit":"完整提交号（多个用空格分开）","deployed":"部署到哪里/第几代","verified":"怎么验收的"}，'
    "没做的项留空；调研、报告、问答的轮次不写 delivery。"
)

# The optional ``steps`` and ``delivery`` of CHECKPOINT_RULES, as the Bridge reads them (``checkpoint_steps``,
# ``checkpoint_delivery``): a model's slip (a wrong type, an unknown state, a second doing, a long title) is cut to
# what can be shown, never an error.
STEP_STATES = ("done", "doing", "todo")
STEP_TITLE_CHARS = 30
MAX_STEPS = 20
DELIVERY_FIELDS = ("commit", "deployed", "verified")
# Each field read as written up to this (the answer card's 改动 fold shows it all, the owner 2026-11-09: no more
# "…10:1…"); the cap keeps the fold within a card's budget (cards/elements.py CARD_BYTES).
DELIVERY_CHARS = 2000
CHANGE_LINE_WIDTH = 30  # The 改动 fold's one line after "改动 · " (``change_summary``), cut alone when wider.
COMMIT_SHOWN = 7  # A full hex commit id on that line.
COMMIT_WIDTH = 12  # A commit not a hex id on that line: its first words.


def _words(value, limit):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def checkpoint_steps(cp) -> list:
    """The checkpoint's ``steps`` as [{title, state}], at most MAX_STEPS, each title at most STEP_TITLE_CHARS; an item
    that is not an object with a title is left out, an unknown state reads todo, a doing after the first reads todo."""
    raw = cp.get("steps") if isinstance(cp, dict) else None
    steps, doing = [], False
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        title = _words(item.get("title"), STEP_TITLE_CHARS)
        if not title:
            continue
        state = str(item.get("state") or "").strip().lower()
        state = state if state in STEP_STATES else "todo"
        if state == "doing":
            state, doing = ("todo", doing) if doing else ("doing", True)
        steps.append({"title": title, "state": state})
        if len(steps) == MAX_STEPS:
            break
    return steps


def checkpoint_delivery(cp) -> dict:
    """The checkpoint's ``delivery`` as {commit, deployed, verified} strings of at most DELIVERY_CHARS, {} when it
    has none of them. A list of commits reads as one string of them, space separated."""
    raw = cp.get("delivery") if isinstance(cp, dict) else None
    if not isinstance(raw, dict):
        return {}
    commit = raw.get("commit")
    if isinstance(commit, list):
        raw = {**raw, "commit": " ".join(_words(item, DELIVERY_CHARS) for item in commit if _words(item, DELIVERY_CHARS))}
    found = {name: _words(raw.get(name), DELIVERY_CHARS) for name in DELIVERY_FIELDS}
    return found if any(found.values()) else {}


COMMIT_SPLIT = re.compile(r"[\s,，、;；]+")
GENERATION = re.compile(r"第\s*(\d+)\s*代")


HEX = re.compile(r"[0-9a-fA-F]{7,40}")


def delivery_commits(delivery) -> list:
    """The commits ``delivery`` (``checkpoint_delivery``) names, in its order, each once: "a6bc148 9d3aecd" or
    "a6bc148、9d3aecd" both read as two. Words not all commit ids ("bridge/x 分支") are one, as written."""
    text = str((delivery or {}).get("commit") or "").strip()
    words = [word for word in COMMIT_SPLIT.split(text) if word]
    if not all(HEX.fullmatch(word) for word in words):
        return [text] if text else []
    return list(dict.fromkeys(words))


def shows_change(delivery, files_changed=False) -> bool:
    """Whether a turn's answer card shows its 改动 fold (the owner, 2026-11-09: 只在改了代码的轮次显示): its
    checkpoint's ``delivery`` names a commit; or, without one, the Bridge saw the turn change files in its workspace
    (``files_changed``: core/diff.py wrote the turn's diff, the job's ``diff_json_path``) and the delivery says
    something. A research, report or Q&A turn shows none, even with a delivery written."""
    if not delivery:
        return False
    return bool(delivery_commits(delivery)) or bool(files_changed and any(delivery.get(name) for name in DELIVERY_FIELDS))


def short_commit(commit) -> str:
    """A full hex commit id cut to COMMIT_SHOWN characters; anything else as written."""
    commit = str(commit or "")
    if len(commit) > COMMIT_SHOWN + 1 and all(char in "0123456789abcdefABCDEF" for char in commit):
        return commit[:COMMIT_SHOWN]
    return commit


def _width(text) -> int:
    """``text``'s width in a card, an ASCII character 1, any other 2."""
    return sum(1 if ord(char) < 0x2E80 else 2 for char in text)


def _fit(text, width) -> str:
    """``text`` cut to ``width`` (``_width``), its end "…" when cut."""
    if _width(text) <= width:
        return text
    while text and _width(text) > width - 1:
        text = text[:-1]
    return text + "…"


def change_summary(delivery, width=CHANGE_LINE_WIDTH) -> str:
    """``delivery`` in one line, the 改动 fold's title after "改动 · ": "951327a · 第 331 代 · 已验收" (the first
    commit short, "+1" for one more when the line has room; the deployment's "第 N 代" when it names one, else its
    first words, as many as leave room for 验收; 已验收 or 未验收), at most ``width`` wide (``_width``: a topic's side panel shows "改动 · " and 30, the
    owner's sample 2026-11-09 wrapped "已验收" at 36) and cut alone when wider; '' without a delivery."""
    if not delivery:
        return ""
    commits = delivery_commits(delivery)
    first = _fit(short_commit(commits[0]), COMMIT_WIDTH) if commits else "未提交"
    deployed = str(delivery.get("deployed") or "")
    generation = GENERATION.search(deployed)
    verified = "已验收" if delivery.get("verified") else "未验收"
    room = width - _width(first) - _width(verified) - 2 * _width(" · ")  # 验收 stays: the deployment's words are cut.
    deployed = f"第 {generation.group(1)} 代" if generation else _fit(deployed, room) if deployed else "未部署"
    more = f"{first} +{len(commits) - 1}" if len(commits) > 1 else first
    line = " · ".join((more, deployed, verified))
    if _width(line) > width:
        line = " · ".join((first, deployed, verified))
    return _fit(line, width)


def clock_steps(steps, times, now) -> dict:
    """{title: {started, ended}} (epoch seconds) after seeing ``steps`` at ``now``, from the ``times`` seen before: a
    step's first doing starts it, its first done ends it, doing again (a step taken back) drops its end, todo drops
    both; the same state again changes nothing. A step done without a doing seen has its end alone. Steps no longer
    listed are dropped."""
    times = times if isinstance(times, dict) else {}
    found = {}
    for step in steps:
        old = times.get(step["title"])
        seen = {key: value for key, value in (old.items() if isinstance(old, dict) else ())
                if key in ("started", "ended") and isinstance(value, (int, float)) and not isinstance(value, bool)}
        if step["state"] == "doing":
            seen.setdefault("started", now)
            seen.pop("ended", None)
        elif step["state"] == "done":
            seen.setdefault("ended", now)
        else:
            seen = {}
        if seen:
            found[step["title"]] = seen
    return found


def current_step(steps):
    """(its number from 1, the step) of the doing step, else None."""
    return next(((index, step) for index, step in enumerate(steps, 1) if step["state"] == "doing"), None)


def step_words(steps) -> str:
    """Where ``steps`` are, in one short phrase: "第 3/5 步：<title>", "已完成 2/5 步", "5/5 步已完成"; '' without."""
    if not steps:
        return ""
    total, doing = len(steps), current_step(steps)
    if doing is not None:
        return f"第 {doing[0]}/{total} 步：{doing[1]['title']}"
    done = sum(1 for step in steps if step["state"] == "done")
    return f"{total}/{total} 步已完成" if done == total else f"已完成 {done}/{total} 步"


def checkpoint_note(checkpoint: Path, previous: dict | None = None) -> str:
    """This turn's checkpoint file and, when continuing, the previous status to verify."""
    if previous:
        # The answers are in the message already, and "delivery":"unconfirmed" there reads as
        # though they never arrived.
        previous = {k: v for k, v in previous.items() if k != "saved_user_answers"}
        if isinstance(previous.get("checkpoint"), dict):
            previous["checkpoint"] = {k: v for k, v in previous["checkpoint"].items() if k != "saved_user_answers"}
    return f"本任务检查点文件：{checkpoint}" + (
        "\n上次任务状态（仅供核查，不能直接信任为已完成）：\n" + json.dumps(previous, ensure_ascii=False)
        if previous
        else ""
    )


def audit_recovery(previous: dict, directory: Path) -> dict:
    """Check local artifact presence without executing actions from a checkpoint."""
    cp = previous.get("checkpoint", previous)
    if not isinstance(cp, dict):
        cp = {}
    checks = []
    artifacts = cp.get("artifacts", [])
    for raw in artifacts[:100] if isinstance(artifacts, list) else []:
        if not isinstance(raw, str):
            continue
        path = expand_user(raw)
        if path is None:  # "~name" of no user: no file to look at.
            checks.append({"path": raw, "exists": False})
            continue
        if not path.is_absolute():
            path = directory / path
        resolved = path.resolve()
        if not resolved.is_relative_to(directory.resolve()):
            checks.append({"path": raw, "state": "outside_workspace_not_inspected"})
            continue
        try:
            info = resolved.stat()
            checks.append({"path": str(resolved), "exists": True, "bytes": info.st_size})
        except OSError:
            checks.append({"path": str(resolved), "exists": False})
    return {
        "checked_at": utc_now_iso(),
        "workdir": str(directory.resolve()),
        "artifacts": checks,
        "external_tasks_require_verification": cp.get("external_tasks", []),
        "instruction": "外部任务尚未核验，先查询任务状态；禁止直接重复提交。已有文件也必须核验内容是否完整。",
    }


QUESTION_ONLY_REPLY = "本轮只返回了问题，未提供方案或说明。"

def checkpoint_reply_display(reply: str, question: str, deferred: bool) -> str:
    # Exact comparison only. Do not infer missing plans from length or wording.
    if deferred and (not reply.strip() or reply.strip() == question.strip()):
        return QUESTION_ONLY_REPLY
    if deferred and question.strip():
        body, separator, tail = reply.rstrip().rpartition("\n\n")
        if separator and body.strip() and tail.strip() == question.strip():
            return body.rstrip()
    return reply
