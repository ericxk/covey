"""网页工作台的设置页 (docs/web.md, COVEY_WEB=1; data contract v7 ``settings``): the 设置 card's tabs as one item the page
draws (``snapshot``), and the few changes the page may make (``change``, app/web_ops.py's ``settings``), both through the code the card
itself draws and writes with. Nothing here is a second copy of a rule.

- What is pushed: the tabs ``hub_tabs`` shows (app/menu_panel.py): 配置 as the card has it (v22 ``config``: its sections 新话题,
  管家 unless COVEY_BUTLER=0, 专项负责人 with COVEY_PROJECTS=1 (v36 ``lead``), and 更多, all open: the
  page has no card's size), 常用路径 (v43 ``repos``, the card's 仓库: the home, the repository map, the worktrees; v46 a plain directory's
  ``kind`` and a device's path, the devices a path may be added on; the old 工作区 page ``workspace`` is still pushed, for a page that
  draws it, but is no tab), 群聊, 设备 with COVEY_NODE_LISTEN, with devices on or with a node (v46 ``devices_page``: devices.json
  and the nodes as one row a machine, app/web_panels.py); not 记忆, 能做什么 and 诊断, the card's alone: CARD_ONLY, each with the rows the card draws (cards/butler_panel.py ``topic_settings``, ``butler_setting_list``, ``mode_setting``, ``subagent_setting``: label, kind, the chosen option and every option offered), the workspaces with their tags and 用途
  (cards/workspace_manager.py ``list_entries``, ``tag_words``) on the old 工作区 page, the groups with their state and 免@
  (cards/group_manager.py ``state_words``, ``free_words``; a group is named by a hash of its chat id) and the nodes' lines (app/node_panel.py
  ``facts``). ``editable`` says which rows the page may change (EDITABLE); every other row says 去飞书改 (``feishu``: the bot's applink).
- What the page may change: every row of 新话题; 管家's 供应商, 模型, 思考预算 and
  压缩阈值 (its 私聊顶层的消息 is read only); a workspace's 用途; a group's 停用 / 启用 (its 免@ is read only).
  Data contract v46: a device of devices.json added (checked first), changed, removed or checked (``device_add``, ``device_set``,
  ``device_remove``, ``device_check``: app/web_panels.py), and a path added on a device (``repos_add``'s ``device``).
  Data contract v43: a repository's 用途, 状态 and 写法 (``repos_set``), one added by its path (``repos_add``) or taken out of the map
  (``repos_remove``; the directory stays), and the worktrees the owner named removed (``worktrees_prune``: no --force, only those). The
  old ``workspace_add``, ``workspace_remove`` and ``workspace_prune`` still change the workspace list and are mirrored into the map
  (a directory with ``.git`` is added to it). The map is repos.json (core/repomap.py); before it exists the page shows the old workspace
  list as a read-only view, and the first change makes the file (``edit_repos``). The 新话题 and 管家 rows have no 工作区 or 运行方式
  row any more (cards/butler_panel.py OFFLINE_KINDS).
  Data contract v15: the butler's 私聊顶层的消息 (每条直接开一个话题 with
  ``confirm``), 免@ set to 关闭 or 只有你 (``free``), a workspace added by its path, removed, the missing ones removed, and the 设备 and 代答
  pages (app/web_panels.py); 本周用量 as the card counts it (``usage``, the item key USAGE_KEY).
  敏感词脱敏 is retired: the page no longer draws it, and a change of its two switches (``redaction``) or its terms (``terms``) from a
  page drawn before is refused, REDACTION_RETIRED.
  A row's change is the card's own (app/butler_panel.py ButlerPanel ``changes`` then core/butler_settings.py ``update``), so both write the one
  main-thread.json and the card and the page show the same; the page's receipt is the card's (``SAVED``). Only the owner: app/access.py
  ``is_owner`` of the owner the Bridge knows (the page's own check is its login).
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from covey.app import butler_harness
from covey.app import butler_panel as panel_rules
from covey.app.menu_panel import hub_tabs
from covey.cards import butler_panel as cards
from covey.cards.group_manager import FREE_CHOICES, KIND_WORDS, PROXY_BLOCKED, free_limit, free_mode, free_words, name_of, ordered, state_words
from covey.cards.settings_hub import (ABOUT, CONFIG, DOCTOR, GROUPS, LABELS, MEMORY, NODES, PROXY, REPOS, TOPIC,
                                      WORKSPACE)
from covey.cards.workspace_manager import list_entries, tag_words, tail
from covey.app import quota_watch, self_upgrade, web_launch, web_panels
from covey.app.access import free_talk_on
from covey.app.card_actions import REVIEW_KINDS, REVIEW_RETIRED
from covey.app.workspace_manager import derived_repos, repo_entries
from covey.core import assist as assist_records
from covey.core import butler, butler_settings, projects, repomap, web_sync
from covey.core.jobs import running_job_records, topic_job_records
from covey.core.state import Unreadable, epoch_seconds, read_object
from covey.core.statuses import ENDED
from covey.core.workspaces import add_workspace, load_config, missing_workspaces, remove_workspaces, worktree_root

LIMIT = 60  # The most options a row lists (the card cuts to fit its size; the page need not).
CARD_ONLY = (MEMORY, ABOUT, DOCTOR)  # The 设置 card's tabs the page's data contract has no page for: not pushed.
COARSE_SECONDS = 60  # WebSettings.stamps: a file that changes as the bot works moves its stamp once this long.
NAMES_SECONDS = 1800  # A group's name, as Feishu gave it, is asked again after this long.
NOTE_LENGTH = cards.NOTE_LENGTH
EDITABLE = frozenset({cards.TOPIC_HARNESS, cards.TOPIC_PROVIDER, cards.TOPIC_MODEL, cards.TOPIC_EFFORT,
                      cards.SUBAGENTS,
                      cards.HARNESS, cards.PROVIDER, cards.MODEL, cards.EFFORT, cards.COMPACT,
                      cards.MODE, cards.LEAD_HARNESS, cards.LEAD_PROVIDER, cards.LEAD_MODEL, cards.LEAD_EFFORT, cards.ASSIST,
                      cards.ASSIST_PROVIDER, cards.ASSIST_MODEL, cards.ASSIST_EFFORT})
BUTLER_KINDS = frozenset({cards.HARNESS, cards.PROVIDER, cards.MODEL, cards.EFFORT, cards.COMPACT})
CHANGES = ("row", "untrust", "note", "redaction", "terms", "group", "free", "workspace_add", "workspace_remove", "workspace_prune",
           "node_add", "node_remove", "refresh", "proxy", "repos_set", "repos_add", "repos_remove",
           "worktrees_prune", "device_add", "device_set", "device_remove", "device_check")  # payload["what"].
PAGE_LABELS = {REPOS: "常用路径"}  # The page's own word for a tab where it is not the card's (the card's 仓库 tab is read only).
PROXY_TAB, PROXY_LABEL = "proxy", "代答"  # The bot menu's own 代答 card, a tab of the page's 设置 (not of the card's).
FREE_ON_PAGE = ("", "owner")  # 免@ the page may set: only ever tighter; 你和选中的人 and 群里所有人 stay on the card.
MODE_TOPICS = "topics"  # cards/butler_panel.py MODE_CHOICES: 每条直接开一个话题, saved only with ``confirm`` (the card's 确定改).
APPLINK = "https://applink.feishu.cn/client/bot/open?appId="
OWNER_ONLY = "只有机器人主人能改设置"
GONE = "这个选项已不可用，页面会按最新设置重画"
READ_ONLY = "这一项网页上只读，请到飞书设置卡上改"
# 敏感词脱敏 is retired: what a page drawn before it was taken out asks for is refused with this (the rows' kinds as the cards had them).
REDACTION_RETIRED = "敏感词脱敏已下线，这次没有改任何设置"
REDACTION_KINDS = frozenset({"bridge_main_redaction", "bridge_main_topic_redaction"})


def devices_page(state_dir=None) -> bool:
    """Whether the page has a 设备 tab without the card having one (COVEY_NODE_LISTEN unset): devices on (COVEY_DEVICES=1, so the
    first one can be added) or a node in the registry besides the hub's own."""
    from covey.core import devices, nodes
    if devices.enabled():
        return True
    try:
        return bool(state_dir) and any(alias != nodes.LOCAL for alias in nodes.active(state_dir))
    except OSError:
        return False


def web_tabs(state_dir=None) -> list:
    """The 设置 card's tabs (hub_tabs) as the page has them: the card's own tabs (CARD_ONLY) left out, 设备 also when ``devices_page``."""
    tabs = [tab for tab in hub_tabs() if tab not in CARD_ONLY]
    if NODES not in tabs and devices_page(state_dir):
        tabs = [tab for tab in LABELS if tab in tabs or tab == NODES]
    return tabs


def group_key(chat_id) -> str:
    """How the page names a group: no chat id leaves the machine."""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def row(setting, editable=False, follow="") -> dict:
    """A cards/launch_config.py Setting as the page reads it."""
    options = [{"value": str(value), "text": str(text)} for value, text in setting.options]
    return {"key": setting.key, "kind": setting.kind, "label": setting.label, "current": str(setting.current),
            "text": setting.text, "options": options, "editable": bool(editable and setting.kind in EDITABLE)}


def rows_of(settings_) -> list:
    """``settings_`` as the page's rows, in the order of the card's lines, each with its ``line`` (1, 2, ...: the rows the
    card draws side by side, cards/butler_panel.py ``lines``): the page may lay a line out as it likes."""
    return [{**row(each, True), "line": number} for number, line in enumerate(cards.lines(settings_), 1) for each in line]


def page_ops() -> list:
    """The page's operations this Covey does (``ops``, v34): core/web_sync.py KINDS, ``upgrade`` (v39) only with an upgrader set up
    (app/self_upgrade.py config), so a page draws 版本与更新 only then; ``quota_refresh`` (v42) only with a quota source to read
    (app/quota_watch.py ``wanted``), so a page draws 账号与额度 only then."""
    return [kind for kind in web_sync.KINDS if (kind != "upgrade" or self_upgrade.config() is not None)
            and (kind != "quota_refresh" or quota_watch.wanted())]


# -- 仓库 (data contract v43 ``repos``): the repository map and the worktrees ---------------------------------------------------

NOTE_MAX, NAME_MAX, PRUNE_MAX = 200, 64, 50  # core/web_sync.py's check of the payloads (the server's reposSettingsError) is the same.
PATH_SIZE = 80  # Characters of a path the page gets: its tail (cards/workspace_manager.py ``tail``).
WORKTREE_SCAN = 200  # The most worktree directories the page counts (two levels of worktree_root(), no deeper).
WORKTREE_SECONDS = 4.0  # The scan's time: what it found by then is what the page gets.
WORKTREE_CACHE = 30.0  # Seconds the scan is kept (the settings are pushed more often than worktrees come and go).
PRUNABLE_AFTER = 86400  # A worktree is offered for cleaning once its topic ended this long ago.
GIT_SECONDS = 20  # ``git worktree remove``'s time.
DIR_SECONDS = 5  # The check of a directory added as a repository.
NOT_CHANGED = "没有改动"
NEW_LAYOUT, TOPIC_DIR = re.compile(r"T(\d+)-([0-9a-f]{8})"), re.compile(r"(?:.*-)?([0-9a-f]{8})")


def clean_line(text, limit) -> str:
    """``text`` on one line (white space collapsed), at most ``limit`` characters."""
    return " ".join(str(text).split())[:limit]


def _update_settings(state_dir, changes) -> None:
    """core/butler_settings.py ``update`` of ``changes``; ValueError (the page's receipt) when the settings file is there
    and cannot be read now: nothing written, never the defaults over it (W1-F)."""
    try:
        butler_settings.update(state_dir, **changes)
    except Unreadable:
        raise ValueError(butler_settings.UNREADABLE) from None


def edit_repos(state_dir, home, change) -> str:
    """One change of the repository map: ``change(repos)`` returns (the repos now, the receipt) or raises ValueError. It is
    core/repomap.py ``update``'s, the map's one writer (its file lock, the keys and entries a newer release wrote kept as they
    were); a deployment without repos.json starts from the old workspace list (``derived_repos``, with the butler's 用途) and
    this is where the file is made; one that cannot be read is never written over. Then ``repos.md`` (``render``), so the next
    turn reads the map as it is now."""
    receipt = []

    def apply(repos):
        repos, said = change(repos)
        receipt.append(said)
        return repos
    try:
        repomap.update(state_dir, apply, start=lambda: derived_repos(state_dir, home))
    except repomap.Unreadable as exc:
        raise ValueError(("常用路径（repos.json）的格式不对，" if str(exc) == "format" else "常用路径（repos.json）读不出来，")
                         + NOT_CHANGED) from None
    except OSError:
        raise ValueError("常用路径（repos.json）写不进去，" + NOT_CHANGED) from None
    repomap.render(state_dir, home)  # None when it cannot be written: the map is changed, the next turn's render tries again.
    return receipt[0]


def checked_directory(text, timeout=DIR_SECONDS):
    """(the directory as resolved, whether it holds ``.git``) of the absolute path ``text``, asked for at most ``timeout`` seconds so a
    slow disk holds up this call alone; ValueError says why not (no absolute path, no such directory, no answer in time). A directory
    without ``.git`` is one too: the 常用路径 page adds it as a 目录 (``kind`` dir)."""
    path = Path(text.strip())
    if not path.is_absolute():
        raise ValueError("请填写目录的绝对路径，例如 /home/me/project")
    found = {}

    def check():
        try:
            resolved = path.resolve()
            found["path"] = resolved if resolved.is_dir() else None
            found["git"] = found["path"] is not None and (resolved / ".git").exists()
        except OSError:
            found["path"], found["git"] = None, False

    worker = threading.Thread(target=check, name="repo-add", daemon=True)
    worker.start()
    worker.join(timeout)
    if "git" not in found:
        raise ValueError(f"目录 {timeout:g} 秒内没有答复，请稍后重试")
    if found["path"] is None:
        raise ValueError(f"目录不存在：{path}")
    return found["path"], found["git"]


README_RULES = ("README.md",)  # A 目录's rules when it has no AGENTS.md or CLAUDE.md but a README.md (e.g. a scripts folder).


def dir_rules(directory) -> tuple:
    """The ``rules`` a 目录 added here gets: README.md when that is its only guide, else the default (AGENTS.md, then CLAUDE.md)."""
    try:
        if not any((Path(directory) / name).is_file() for name in ("AGENTS.md", "CLAUDE.md")) and (Path(directory) / "README.md").is_file():
            return README_RULES
    except OSError:
        pass
    return repomap.Repo.__dataclass_fields__["rules"].default


def device_target(text, device=""):
    """(alias, the absolute path on it) of what the page asked to add on a device: ``device`` with an absolute path, or a path typed
    ``<别名>:/abs``; ('', '') for a path of this machine. ValueError: a device's path that is not absolute, an alias not listed."""
    from covey.core import devices
    text = str(text or "").strip()
    if device:
        if not devices.ALIAS.match(str(device)):
            raise ValueError("设备名不对")
        if not text.startswith("/"):
            raise ValueError(f"请填写设备 {device} 上的绝对路径，例如 /home/me/project")
        return str(device), devices.normalize_remote(text)
    alias, where = repomap.device_path(text)
    if not alias and devices.device_shaped(text):
        raise ValueError("设备上的路径要写成 设备名:/绝对路径")
    return alias, where


def add_repo(state_dir, home, text, name="", note="", device="") -> str:
    """repos_add: the directory ``text`` into the map as ``name`` (the directory's name by default, then ``<名字>-<设备>``, -2, -3 on a
    clash; a ``name`` given that is taken is refused), with ``note``. On this machine an existing absolute directory: a 仓库 (``kind``
    git) when it holds ``.git``, else a 目录 (``dir``, written in place, its rules README.md when that is its only guide). On a device
    (``device``, or the path typed ``<别名>:/abs``) the device is asked (core/devices.py ``inspect_dir``, DIR_SECONDS) and the entry
    is ``<别名>:/abs``, written in place. One already in the map by its path changes nothing. The receipt."""
    from covey.core import devices
    alias, where = device_target(text, device)
    if alias:
        if not devices.enabled():
            raise ValueError("多设备没开（COVEY_DEVICES=1），加不了设备上的路径")
        state, kind, why = devices.inspect_dir(state_dir, alias, where, timeout=DIR_SECONDS)
        if state != "ok":
            raise ValueError(why or f"设备 {alias} 上查不了 {where}")
        path, base, rules = f"{alias}:{where}", (where.rstrip("/").rpartition("/")[2] or "dir"), None
    else:
        resolved, git = checked_directory(text)  # Before the lock: a slow disk waits here.
        path, base, kind = str(resolved), resolved.name or "repo", "git" if git else "dir"
        rules = None if git else dir_rules(resolved)
    name, note = clean_line(name, NAME_MAX), clean_line(note, NOTE_MAX)
    if "/" in name:
        raise ValueError("名字里不能有 /")
    word = repomap.KIND_WORDS.get(kind, "仓库")

    def change(repos):
        same = next((repo for repo in repos if repo.path.rstrip("/") == path.rstrip("/")), None)
        if same is not None:
            return repos, f"这个路径已在常用路径里：{same.name}"
        taken = {repo.name for repo in repos}
        if name:
            if name in taken:
                raise ValueError(f"已经有叫 {name} 的常用路径")
            chosen = name
        else:
            stem = base[:NAME_MAX - 3]
            chosen, number = stem, 2
            if alias and chosen in taken:
                chosen = f"{stem}-{alias}"[:NAME_MAX - 3]
                stem = chosen
            while chosen in taken:
                chosen, number = f"{stem}-{number}", number + 1
        mode = "worktree" if not alias and kind == "git" and any(repo.write_mode == "worktree" for repo in repos) and not any(
            repo.write_mode == "inplace" and not repo.device and repo.kind == "git" for repo in repos) else "inplace"
        added = repomap.Repo(name=chosen, path=path, note=note, write_mode=mode, added_by="owner", kind=kind, device=alias,
                             **({"rules": rules} if rules else {}))
        return [*repos, added], f"已加入常用路径：{chosen}（{('设备 ' + alias + ' 上的') if alias else ''}{word}）"

    return edit_repos(state_dir, home, change)


def set_repo(state_dir, home, name, *, note=None, status=None, write_mode=None) -> str:
    """repos_set: ``note``, ``status`` (repomap.STATUSES) and ``write_mode`` (repomap.WRITE_MODES) of the repository ``name``, those given. The receipt."""
    if status is not None and status not in repomap.STATUSES:
        raise ValueError("状态要是 active、readonly 或 frozen")
    if write_mode is not None and write_mode not in repomap.WRITE_MODES:
        raise ValueError("写法要是 worktree 或 inplace")
    if note is None and status is None and write_mode is None:
        raise ValueError("没有要改的项")

    def change(repos):
        index = next((i for i, repo in enumerate(repos) if repo.name == name), None)
        if index is None:
            raise ValueError(f"没有叫 {name} 的常用路径")
        if write_mode == "worktree" and (repos[index].kind == "dir" or repos[index].device):
            raise ValueError("目录和设备上的路径只能原地改")
        fields = {key: value for key, value in (("note", None if note is None else clean_line(note, NOTE_MAX)),
                                                ("status", status), ("write_mode", write_mode)) if value is not None}
        # What the owner sets here is the value from now on, not one of a newer release's this one could not read (repomap.to_json).
        repos[index] = dataclasses.replace(repos[index], **fields, extra={k: v for k, v in repos[index].extra.items() if k not in fields})
        words = {"note": "用途", "status": "状态", "write_mode": "写法"}
        return repos, f"已改 {name}：" + "、".join(words[key] for key in fields)

    return edit_repos(state_dir, home, change)


def remove_repo(state_dir, home, name) -> str:
    """repos_remove: the repository ``name`` out of the map; the directory stays. The receipt."""
    def change(repos):
        if not any(repo.name == name for repo in repos):
            raise ValueError(f"没有叫 {name} 的常用路径")
        return [repo for repo in repos if repo.name != name], f"已从常用路径去掉 {name}，目录没有动"

    return edit_repos(state_dir, home, change)


def forget_repos(state_dir, home, pairs) -> None:
    """The old 移除 of a workspace, mirrored: the repositories of the map that are one of ``pairs`` ((name, path) of what was removed)
    out of it. Only when repos.json exists (the old calls make no file); a failure is the caller's to say."""
    if not repomap.path(state_dir).exists() or not pairs:
        return

    def change(repos):
        kept = [repo for repo in repos if (repo.name, repo.path) not in pairs]
        return kept, ""

    edit_repos(state_dir, home, change)


def live_counts(state_dir, repos) -> dict:
    """{repository name: topics running with a workdir in it}, from the running jobs' records (core/jobs.py ``running_job_records``:
    only those are read, no lock); 0 for all when they cannot be read."""
    try:
        records = running_job_records(Path(state_dir))[:500]
    except Exception:  # noqa: BLE001 - a count the page can do without
        return {}
    pairs = {(str(record.get("workdir") or ""), str(record.get("topic_id") or record.get("key") or "")) for record in records}
    return {repo.name: len({topic for workdir, topic in pairs if workdir == repo.path.rstrip("/")
                            or workdir.startswith(repo.path.rstrip("/") + "/")}) for repo in repos}


def worktree_branch(path) -> str:
    """The branch a worktree has checked out, from its ``.git`` file and the HEAD that names: two small reads, no git."""
    try:
        text = (path / ".git").read_text(encoding="utf-8")[:500]
        if not text.startswith("gitdir:"):
            return ""
        head = (Path(text[len("gitdir:"):].strip()) / "HEAD").read_text(encoding="utf-8")[:300]
    except (OSError, ValueError):
        return ""
    return head[len("ref: refs/heads/"):].strip() if head.startswith("ref: refs/heads/") else ""


def is_worktree(path) -> bool:
    """Whether ``path`` is a git worktree's directory: a real directory (no link) whose ``.git`` is a file."""
    try:
        return path.is_dir() and not path.is_symlink() and (path / ".git").is_file() and not (path / ".git").is_symlink()
    except OSError:
        return False


def scan_worktrees(root, found) -> None:
    """The worktrees under ``root``, two levels at most (the new layout ``T<号>-<id8>/<仓库>/``, the old ``<仓库>-<时间>-<id8>``), appended
    to ``found`` as {rel (the path under root), repo, topic8, number}: no deeper, no git, at most WORKTREE_SCAN."""
    try:
        top = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return
    for first in top:
        if len(found) >= WORKTREE_SCAN:
            return
        path = Path(first.path)
        if first.name.startswith(".") or not is_dir_here(first):
            continue
        if is_worktree(path):
            found.append({"rel": first.name, "repo": first.name, "topic8": (TOPIC_DIR.fullmatch(first.name) or [None, ""])[1],
                          "number": None, "path": path})
            continue
        match = NEW_LAYOUT.fullmatch(first.name)
        if not match:
            continue
        try:
            second = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in second:
            if len(found) >= WORKTREE_SCAN:
                return
            if is_dir_here(entry) and is_worktree(Path(entry.path)):
                found.append({"rel": f"{first.name}/{entry.name}", "repo": entry.name, "topic8": match[2],
                              "number": int(match[1]), "path": Path(entry.path)})


def is_dir_here(entry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def topic_ended(state_dir, topics, id8, now):
    """(topic id, its number or None, when it ended) of the topic whose id starts with ``id8`` (``topics``: the topic directories' names)
    when every job of it is over and the last was more than PRUNABLE_AFTER ago; (None, None, None) when that cannot be told: no or several
    topics by that name, no job, one still not ended."""
    ids = [name for name in topics if name.startswith(id8)] if id8 else []
    if len(ids) != 1:
        return None, None, None
    try:
        records = topic_job_records(Path(state_dir), ids[0])
    except Exception:  # noqa: BLE001 - not told
        return None, None, None
    if not records or any(str(record.get("status") or "") not in ENDED for record in records):
        return None, None, None
    stamps = [epoch_seconds(record.get("updated_at") or record.get("created_at")) for record in records]
    if any(stamp is None for stamp in stamps) or now - max(stamps) <= PRUNABLE_AFTER:
        return None, None, None
    number = read_object(Path(state_dir) / "topics" / ids[0] / "topic.json").get("topic_number")
    return ids[0], number if type(number) is int else None, max(stamps)


def worktrees_page(state_dir, now) -> dict:
    """``worktrees`` of the repos page: {total, prunable: [{repo, branch, tail, topic_number?, ended_at?}]}: the worktrees under
    core/workspaces.py ``worktree_root()`` (``scan_worktrees``, WORKTREE_SECONDS at most), those whose topic ended over a day ago
    offered for cleaning (``topic_ended``; one that cannot be told is not), PRUNE_MAX at most. ``tail`` is the path under that root,
    which ``prune_worktrees`` takes as it is."""
    found = []
    worker = threading.Thread(target=scan_worktrees, args=(worktree_root(), found), name="worktree-scan", daemon=True)
    worker.start()
    worker.join(WORKTREE_SECONDS)
    found = list(found)
    topics = []
    if found:
        try:
            topics = os.listdir(Path(state_dir) / "topics")
        except OSError:
            topics = []
    prunable = []
    for each in found:
        topic, number, ended = topic_ended(state_dir, topics, each["topic8"], now)
        if ended is None:
            continue
        prunable.append({"repo": each["repo"], "branch": worktree_branch(each["path"]), "tail": each["rel"],
                         **({"topic_number": each["number"] or number} if each["number"] or number else {}), "ended_at": ended})
        if len(prunable) >= PRUNE_MAX:
            break
    return {"total": len(found), "prunable": prunable}


def worktree_target(root, text):
    """The worktree directory ``text`` names under ``root`` (an absolute path, or the path under it that the page's ``tail`` is), as a
    Path; ValueError says why it is not one to remove: outside the root, deeper or shallower than a worktree is, a link, not a
    directory whose ``.git`` is a file."""
    real = Path(os.path.realpath(root))
    path = Path(text)
    if ".." in path.parts or not str(text).strip():
        raise ValueError("路径不在 worktree 目录下")
    if path.is_absolute():
        base = next((r for r in (root, real) if path == r or r in path.parents), None)
        if base is None:
            raise ValueError("路径不在 worktree 目录下")
        path = path.relative_to(base)
    if not 1 <= len(path.parts) <= 2:
        raise ValueError("路径不在 worktree 目录下")
    target = real / path
    if any((real / Path(*path.parts[:n])).is_symlink() for n in range(1, len(path.parts) + 1)):
        raise ValueError("路径是个链接，不删")
    if not is_worktree(target):
        raise ValueError("不是 git worktree（.git 不是文件），不删")
    return target


def remove_worktree(target) -> str:
    """``git worktree remove`` (no --force) of the worktree ``target``, run from its main repository, GIT_SECONDS at most; '' when it is
    gone, else why it was not removed (a worktree with changes, a locked one, a main repository that is not there)."""
    try:
        gitdir = Path((target / ".git").read_text(encoding="utf-8")[:500].partition("gitdir:")[2].strip())
    except (OSError, ValueError):
        return "读不出它的主仓库"
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git" or not gitdir.parent.parent.parent.is_dir():
        return "找不到它的主仓库"
    main = gitdir.parent.parent.parent
    try:
        proc = subprocess.run(["git", "-C", str(main), "worktree", "remove", str(target)], capture_output=True, text=True,
                              timeout=GIT_SECONDS, check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return f"git 超过 {GIT_SECONDS} 秒没有答复"
    except OSError as exc:
        return f"git 没跑起来（{type(exc).__name__}）"
    if proc.returncode == 0:
        return ""
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    text = detail[-1][:200] if detail else f"git 退出码 {proc.returncode}"
    return "有未提交的改动或被锁住，没删（" + text + "）" if "force" in text or "modified" in text or "untracked" in text or "locked" in text \
        else text


def prune_worktrees(paths) -> str:
    """worktrees_prune: exactly the worktrees ``paths`` name (absolute, or under worktree_root() as the page's ``tail``), one by one
    through ``remove_worktree``; nothing else is touched, not a directory around one, not a branch. The receipt lists them and each
    one's result; with none removed it is a refusal (ValueError)."""
    root = worktree_root()
    lines, removed = [], 0
    for text in dict.fromkeys(paths):
        try:
            target = worktree_target(root, text)
        except ValueError as exc:
            lines.append(f"- {tail(str(text), PATH_SIZE)}：{exc}")
            continue
        why = remove_worktree(target)
        removed += not why
        lines.append(f"- {tail(str(target), PATH_SIZE)}：" + ("已删除" if not why else "没删，" + why))
    receipt = f"要删 {len(lines)} 个 worktree，删掉 {removed} 个：\n" + "\n".join(lines)
    if not removed:
        raise ValueError(receipt)
    return receipt


class WebSettings:
    """The 设置 card's tabs for the page (``snapshot``) and the page's changes (``change``). ``env``: app/butler_env.py ButlerEnv, whose
    ``settings`` is the 设置 card (app/settings_hub.py SettingsHub: its panels draw and write)."""

    def __init__(self, env) -> None:
        self.env = env
        self.names = {}  # chat id -> (name, when asked)
        self.node_aliases = None  # ((state dir, registry mtime), aliases) of node_files.
        self.worktrees = None  # (when, state dir, the page) of the last worktree scan (WORKTREE_CACHE).

    @property
    def hub(self):
        return getattr(self.env, "settings", None)

    def apps(self) -> dict:
        """The Bridge's Harness apps by name ({} without them)."""
        return getattr(getattr(self.env, "topics", None), "apps", {}) or {}

    def context(self):
        """The Bridge's default Harness's context (state_dir, profile, default_workdir), as a panel's click gets one."""
        topics = self.env.topics
        apps = getattr(topics, "apps", {}) or {}
        app = apps.get(getattr(topics, "default", "")) or next(iter(apps.values()), None)
        base = getattr(getattr(app, "harness", None), "context", None)
        return SimpleNamespace(state_dir=Path(self.env.state_dir), profile=getattr(base, "profile", None) or self.env.profile,
                               default_workdir=getattr(base, "default_workdir", None))

    def sources(self, ctx) -> list:
        """Every path the page's settings read their state from, all tabs together (``tab_sources``), each once, in the tabs' order:
        what ``stamps`` stats each second. A directory stands for the records in it (a group, a 代答 contact, a launch form: every
        write there is a rename, which moves the directory's mtime), so a tab never lists a folder's files one by one."""
        found = []
        for paths in self.tab_sources(ctx).values():
            found.extend(path for path in paths if path not in found)
        return found

    def tab_sources(self, ctx) -> dict:
        """{tab: [path]}: the state a tab of ``snapshot`` reads (``CONFIG`` also the 新话题 choices the page's chat box takes, ``launch``),
        fixed paths only: nothing is listed or read here, so ``stamps`` stays a few stats. 设备's per-node files come from the
        registry (read again only when its mtime moved)."""
        from covey.app.web_panels import checks_path, joins_path
        from covey.core import devices, nodes, proxy as proxy_records
        from covey.core.workspaces import config_path
        state = Path(ctx.state_dir)
        return {
            # 管家 and 新话题 rows, 辅助模型 (its record), the model choices (the launch card's forms, last launches, the model menu's
            # cache, the recent selections) and what the 工作区 names list reads.
            CONFIG: [butler_settings.path(state), assist_records.path(state), config_path(state), state / "launch-defaults.json",
                     state / "launch-defaults", state / "launch-models", state / "model-settings", state / "model-usage.json"],
            REPOS: [repomap.path(state), config_path(state), state / "jobs" / ".index",  # The topics that run in a repository.
                    devices.config_path()],  # The devices a path may be added on.
            GROUPS: [state / "groups"],
            NODES: [nodes.nodes_dir(state) / "registry.json", joins_path(state), *self.node_files(state),
                    devices.config_path(), checks_path(state), *self.device_files(state)],
            PROXY_TAB: [proxy_records.proxy_dir(state) / "settings.json", proxy_records.proxy_dir(state) / "contacts"],
        }

    def node_files(self, state) -> list:
        """The link and latest sample of each node of the registry (the 设备 lines): the aliases are read from the registry only when
        its mtime moved, kept in between (``node_aliases``)."""
        from covey.core import nodes
        registry = nodes.nodes_dir(state) / "registry.json"
        try:
            stamp = registry.stat().st_mtime_ns
        except OSError:
            stamp = 0
        kept = self.node_aliases
        if kept is None or kept[0] != (str(state), stamp):
            kept = self.node_aliases = ((str(state), stamp), [alias for alias in nodes.active(state) if nodes.ALIAS.match(alias)])  # Never raises: read_object.
        return [nodes.node_dir(state, alias) / name for alias in kept[1] for name in ("link.json", "latest.json")]

    @staticmethod
    def device_files(state) -> list:
        """The last look and the last 体检 of each device of devices.json (the 设备 rows): its aliases from core/devices.py ``current``,
        which reads the file again only when it changed; none with devices off or a file that cannot be read."""
        from covey.core import devices
        try:
            config = devices.current()
        except devices.DeviceConfigError:
            return []
        return [devices.device_dir(state, alias) / name for alias in sorted({*config.devices, *config.watched})
                for name in ("health.json", "profile.json")]

    def stamps(self) -> tuple:
        """What a change of the settings shows in, read each second (the pass thread, app/web_sync.py settings_items): one stat of each
        path ``sources`` lists, for every tab, so a change of a group, a node, the 代答 or the 新话题 choices moves it as the butler's
        own settings do. A path's stamp is (mtime_ns, size), 0 for one that is not there; never raises. Three files change all the
        time while the bot works, and one stamp each per COARSE_SECONDS keeps them from pushing the item every second: the jobs'
        index (a repository's running topics) and a node's latest sample (``coarse``)."""
        out = []
        for path in self.sources(SimpleNamespace(state_dir=Path(self.env.state_dir))):
            try:
                stat = path.stat()
            except OSError:
                out.append(0)
                continue
            out.append(stat.st_mtime_ns // (COARSE_SECONDS * 10 ** 9) if self.coarse(path) else (stat.st_mtime_ns, stat.st_size))
        return tuple(out)

    @staticmethod
    def coarse(path) -> bool:
        return path.name == "latest.json" or path.parts[-2:] == ("jobs", ".index")

    # -- the snapshot ------------------------------------------------------------------------------------------------------------

    def snapshot(self, chat, now=None) -> dict:
        """The item's data: {tabs: [{key, label}], feishu, <tab key>: what it shows}."""
        now = time.time() if now is None else now
        ctx = self.context()
        tabs = web_tabs(ctx.state_dir)
        settings = butler_harness.drawn(butler_settings.load(ctx.state_dir), self.apps())  # Never saved.
        choices = self.choices(ctx)
        labels = {**LABELS, **PAGE_LABELS, PROXY_TAB: PROXY_LABEL}
        tabs = [*tabs, *([PROXY_TAB] if self.proxy_panel() is not None else [])]
        # ``ops``: the page's operations this Covey does (core/web_sync.py KINDS, data contract v34), so a page shows a button only
        # for what is done (收尾、停止 of a project, 不对 on a memory line) and an older Covey's page stays as it was.
        data = {"tabs": [{"key": tab, "label": labels[tab]} for tab in tabs], "feishu": self.feishu(), "ops": page_ops()}
        build = {CONFIG: lambda: self.config(ctx, settings, choices, chat),
                 REPOS: lambda: self.repos(ctx, now),
                 GROUPS: lambda: self.groups(ctx, now), NODES: lambda: self.nodes(ctx),
                 PROXY_TAB: lambda: web_panels.proxy_tab(ctx, self.proxy_panel(), row, LIMIT)}
        try:  # 开新话题's choices for the page's chat box (data contract v17), not a tab.
            data["launch"] = web_launch.catalog(self.env, ctx, chat)
        except Exception as exc:  # noqa: BLE001 - the rest of the settings go on
            self.env.runtime.log(f"web settings launch unreadable: {type(exc).__name__}")
        for tab in tabs:
            try:
                data[tab] = build[tab]()
            except Exception as exc:  # noqa: BLE001 - one tab that cannot be read leaves the others
                self.env.runtime.log(f"web settings tab {tab} unreadable: {type(exc).__name__}")
                data[tab] = {"error": "这一页现在读不出来，请到飞书设置卡上看"}
        try:  # The old 工作区 page, for a page that still draws it (v43: ``repos`` is the tab now).
            data[WORKSPACE] = self.workspaces(ctx, settings)
        except Exception as exc:  # noqa: BLE001
            self.env.runtime.log(f"web settings tab {WORKSPACE} unreadable: {type(exc).__name__}")
            data[WORKSPACE] = {"error": "这一页现在读不出来，请到飞书设置卡上看"}
        return data

    def feishu(self) -> str:
        app_id = str(getattr(self.env.runtime.access, "app_id", "") or "")
        return APPLINK + app_id if app_id else ""

    def choices(self, ctx):
        from covey.app.launch_models import available_models
        return available_models(self.env.topics, ctx)

    def panel(self):
        return self.hub.panel(TOPIC)

    def proxy_panel(self):
        """The 代答 card's panel (app/proxy_panel.py) when this deployment has a sender, else None."""
        from covey.app.proxy_send import available
        if not available():
            return None
        try:
            return self.hub.panel(PROXY)
        except KeyError:
            return None

    def usage(self, now=None) -> dict:
        """本周用量 as the bot menu's card counts it (app/weekly_usage.py ``summarize`` over the job records of the window), for the
        page's own 本周用量 (the page summed its pushed turns, 8.41B against the card's 9.43B); ``history`` (v26): the same count by
        local day and the page's ranges, for its 用量 page (weekly_usage.py ``history``)."""
        from covey.app import weekly_usage
        from covey.core.jobs import select_job_records
        now = time.time() if now is None else now
        since = now - weekly_usage.WINDOW_DAYS * 86400
        titles = {name: harness.spec.title for name, harness in (getattr(self.env.topics, "harnesses", {}) or {}).items()}
        records = select_job_records(Path(self.env.state_dir), lambda r: (weekly_usage.created_at(r) or 0) >= since)
        found = weekly_usage.summarize(records, now, titles)
        return {**found, "outcomes": dict(found["outcomes"]), "ended": dict(found["ended"]), "at": now,
                "history": weekly_usage.history(Path(self.env.state_dir), now, titles)}

    def config(self, ctx, settings, choices, chat) -> dict:
        """配置 (v22), as the card's page (cards/butler_panel.py config_page) with every section open: ``topic`` (新话题), ``main``
        (管家, unless COVEY_BUTLER=0), ``lead`` (专项负责人, with COVEY_PROJECTS=1, v36; no ``review`` since 独立复核 was taken out),
        ``assist`` (辅助模型, always: ``assist``; an older page leaves it out), each with its ``title``; under 更多 (``more``) 子
        Agent; ``summary`` the card's title bar."""
        on = butler.available() and settings.enabled
        found = {"summary": cards.config_subtitle(settings, choices, on, butler.available(), projects.enabled()),
                 "topic": {"title": cards.SECTIONS[cards.TOPIC_PART], **self.topic(ctx, settings, choices, chat)}}
        if butler.available():
            found["main"] = {"title": cards.SECTIONS[cards.BUTLER_PART], **self.butler(ctx, settings, choices)}
        if projects.enabled():
            found["lead"] = {"title": cards.SECTIONS[cards.LEAD_PART], "note": cards.LEAD_NOTE,
                             "rows": rows_of(cards.lead_setting_list(settings, choices, LIMIT))}
        try:  # 辅助模型: whether the butler is on or not (the background calls run without it).
            found["assist"] = self.assist(ctx, settings, choices)
        except Exception as exc:  # noqa: BLE001 - the rest of 配置 goes on
            self.env.runtime.log(f"web settings assist unreadable: {type(exc).__name__}")
        return {**found, "more": rows_of([cards.subagent_setting(settings)]), "more_note": cards.SUBAGENT_NOTE,
                "locked": cards.LOCKED if on else ""}

    def assist(self, ctx, settings, choices=None) -> dict:
        """配置's 辅助模型 (app/assist.py): its rows as the card's section has them (Harness: 自动, each Harness that answers one
        question by itself, 不用模型; with a Harness named its 供应商, 模型 and 思考预算; the page may change them with the butler
        off too), ``note`` the card's line under them, ``words`` what it runs on now, and ``recent`` the
        background calls' latest outcome, a row a feature (app/assist.py recent: who, ok or the reason's code and words, ms, at;
        never a word the owner or a model said)."""
        from covey.app import assist
        found = self.panel().assist_row(ctx, settings)
        return {"title": cards.SECTIONS[cards.ASSIST_PART], "note": cards.ASSIST_NOTE, "words": found["words"],
                "rows": rows_of(cards.assist_setting_list(settings, self.choices(ctx) if choices is None else choices,
                                                              LIMIT, found)),
                "recent": assist.recent(ctx.state_dir)}

    def topic(self, ctx, settings, choices, chat) -> dict:
        """配置's 新话题: its rows (every one the page may change), in the card's two lines."""
        panel, defaults = self.panel(), settings.topic_defaults
        on = butler.available() and settings.enabled
        found = cards.topic_settings(defaults, choices, panel_rules.workspace_names(ctx.state_dir), LIMIT,
                                     panel.follows(ctx, defaults, choices, chat), LIMIT, panel.fallback(ctx, defaults, choices, chat),
                                     butler=on)
        return {"note": cards.TOPIC_NOTE, "rows": rows_of(found)}

    def butler(self, ctx, settings, choices) -> dict:
        """配置's 管家, its rows as the card's section has them: off (纯话题), the mode's row alone; else its own (Harness first,
        压缩阈值 where it counts) and the mode last. The page may change them all; 每条直接开一个话题 only after it asked
        ``mode_confirm`` (the card's 确定改). ``note``: MODE_OFF when it is off."""
        mode = cards.mode_setting(settings)
        confirm = lambda rows: [{**each, "confirm": {MODE_TOPICS: cards.MODE_CONFIRM}} if each["key"] == mode.key else each
                                for each in rows]
        if not settings.enabled:
            return {"on": False, "rows": confirm(rows_of([mode])), "note": cards.MODE_OFF}
        own = cards.butler_setting_list(settings, choices, LIMIT, panel_rules.workspace_names(ctx.state_dir))
        return {"on": True, "rows": confirm(rows_of(own + [mode]))}

    def nodes(self, ctx) -> dict:
        """设备: app/web_panels.py ``nodes_tab`` (the card's panel only with COVEY_NODE_LISTEN: adding a node needs its gateway)."""
        return web_panels.nodes_tab(ctx.state_dir, (getattr(self.hub, "panels", None) or {}).get(NODES))

    def workspaces(self, ctx, settings) -> dict:
        """工作区: the list in the card's order, each row's tags, path's tail, last use and 用途 (with the butler)."""
        config = load_config(ctx.state_dir, ctx.default_workdir)
        status = self.hub.panel(WORKSPACE).status(config)
        notes = butler.available()
        items = [{"name": entry["name"], "path": tail(entry["path"], 80),
                  "tags": [{"text": text, "tone": colour} for text, colour in tag_words(entry, config["default"])],
                  "used": entry.get("used") or None, "removable": entry["name"] != config["default"],
                  **({"note": settings.note(entry["name"])} if notes else {})}
                 for entry in list_entries(config, status)]
        return {"summary": f"{len(items)} 个工作区", "items": items, "notes": notes,
                "note": cards.WORKSPACE_NOTE if notes else "", "note_length": NOTE_LENGTH,
                "missing": len(missing_workspaces(config, status))}

    def repos(self, ctx, now) -> dict:
        """仓库 (v43 ``repos``, SettingsReposPage): ``home`` (the tail of the home), the map's repositories (repo_entries: repos.json,
        or before it exists the old workspace list read only) each with how many topics run in it (``live_counts``), and the worktrees
        (``worktrees_page``, kept WORKTREE_CACHE seconds). A path is its tail (PATH_SIZE)."""
        repos, _ = repo_entries(ctx.state_dir, ctx.default_workdir)
        repos = repos[:200]
        live = live_counts(ctx.state_dir, [repo for repo in repos if not repo.device])
        items = [{"name": repo.name, "tail": tail(repomap.device_path(repo.path)[1] if repo.device else repo.path, PATH_SIZE),
                  "note": repo.note, "status": repo.status, "write_mode": repo.write_mode, "branch": repo.branch, "rules": list(repo.rules),
                  "kind": repo.kind, "device": repo.device,
                  **({"last_used": int(repo.last_used)} if repo.last_used else {}), "live_topics": live.get(repo.name, 0)}
                 for repo in repos]
        kept = self.worktrees
        if kept is None or kept[1] != str(ctx.state_dir) or not 0 <= now - kept[0] < WORKTREE_CACHE:
            kept = self.worktrees = (now, str(ctx.state_dir), worktrees_page(ctx.state_dir, now))
        return {"home": {"tail": tail(str(ctx.default_workdir or ""), PATH_SIZE)}, "items": items, "worktrees": kept[2],
                "devices": self.path_devices()}

    @staticmethod
    def path_devices() -> list:
        """The devices a path may be added on (v46 ``devices``): devices.json's with ssh, {alias, title, access}; [] with devices off or a
        devices.json that cannot be read (the page then adds paths of this machine only)."""
        from covey.core import devices
        if not devices.enabled():
            return []
        try:
            found = devices.current().devices
        except devices.DeviceConfigError:
            return []
        return [{"alias": alias, "title": device.title, "access": device.access} for alias, device in sorted(found.items())]

    def groups(self, ctx, now) -> dict:
        """群聊: every group the card lists, active first, its state and 免@; 停用 / 启用 the page may do."""
        manager = self.hub.panel(GROUPS)
        records, kinds = manager.listed()
        names = self.group_names(ctx, manager, records, now)
        active = sum(1 for g in records if g.get("active"))
        items = []
        for record in ordered(records, names):
            kind = (kinds or {}).get(record["chat_id"]) or ""
            personal = kind == "personal"
            text, tone = state_words(record)
            # The card's buttons: none on a personal group or an external one, else 停用 or 启用.
            switch = "" if personal or record.get("external") is True else "off" if record.get("active") is True else "on"
            free = free_talk_on() and not personal
            # 免@ the page may set: 关闭 and 只有你, where the card offers them (never in the proxy's group).
            blocked = free and free_limit(record, self.env.runtime.access.proxy_chat(record["chat_id"])) == PROXY_BLOCKED
            items.append({"key": group_key(record["chat_id"]), "name": name_of(record, names), "space": KIND_WORDS.get(kind, ""),
                          "state": None if personal else {"text": text, "tone": tone},
                          "free": free_words(record) if free else "", "switch": switch,
                          "free_mode": free_mode(record) if free else None,
                          "free_choices": [{"value": mode, "text": FREE_CHOICES[mode][0]} for mode in FREE_ON_PAGE]
                          if free and not blocked else []})
        return {"summary": f"{len(records)} 个群 · 已启用 {active}" if records else "还没有群", "items": items}

    def group_names(self, ctx, manager, records, now) -> dict:
        """{chat id: name}: asked of Feishu (the card's own lookup) for a group not asked in NAMES_SECONDS, kept between looks."""
        stale = [g for g in records if now - self.names.get(g["chat_id"], ("", 0.0))[1] >= NAMES_SECONDS]
        if stale:
            found = manager.names(ctx, stale)
            for g in stale:
                self.names[g["chat_id"]] = (found.get(g["chat_id"]) or self.names.get(g["chat_id"], ("", 0.0))[0], now)
        return {chat: name for chat, (name, _) in self.names.items() if name}

    # -- a change from the page ------------------------------------------------------------------------------------------------

    def change(self, payload, owner) -> str:
        """One change the page asked for (``payload["what"]``, CHANGES); the card's receipt. ValueError says why it was not made."""
        if not owner:
            raise ValueError(OWNER_ONLY)
        if self.hub is None:
            raise ValueError("设置卡不可用")
        what = str(payload.get("what") or "")
        if what not in CHANGES:
            raise ValueError("不认识的设置改动")
        ctx = self.context()
        return getattr(self, "change_" + what)(ctx, payload)

    def apply(self, ctx, kind, value) -> str:
        """``value`` (a card button's value) through the card's ``changes``, written by ``update``; the card's receipt."""
        changes = self.panel().changes(ctx, {}, {"kind": kind, **value})
        if changes is None:
            raise ValueError(GONE)
        if not changes:
            return "没有变化"
        _update_settings(ctx.state_dir, changes)
        return panel_rules.MODE_NOTICES.get(changes.get("enabled")) or panel_rules.saved_notice(kind)

    def change_row(self, ctx, payload) -> str:
        kind, option = str(payload.get("kind") or ""), payload.get("option")
        if kind in cards.OFFLINE_KINDS:
            raise ValueError(cards.OFFLINE_NOTICE)
        if kind in REDACTION_KINDS:  # 敏感词脱敏's rows on a page drawn before it was taken out.
            raise ValueError(REDACTION_RETIRED)
        if kind in REVIEW_KINDS:  # 独立复核's rows on a page drawn before it was taken out (app/card_actions.py).
            raise ValueError(REVIEW_RETIRED)
        if kind not in EDITABLE:
            raise ValueError(READ_ONLY)
        if not isinstance(option, str):
            raise ValueError(GONE)
        settings = butler_harness.drawn(butler_settings.load(ctx.state_dir), self.apps())  # Never saved.
        on = butler.available() and settings.enabled
        if kind in BUTLER_KINDS and not on:
            raise ValueError("管家没开，这一项现在不起作用")
        if kind == cards.MODE:
            if not butler.available():
                raise ValueError(READ_ONLY)
            if option == MODE_TOPICS and settings.enabled and payload.get("confirm") is not True:
                raise ValueError("改成每条直接开一个话题要先确认")
        return self.apply(ctx, kind, {"option": option})

    def change_untrust(self, ctx, payload) -> str:
        raise ValueError("记住的工作区已下线：管家开的话题一律直接开工")

    def change_note(self, ctx, payload) -> str:
        """A workspace's 用途, by the card's 保存用途 (ButlerPanel ``notes``): empty removes it."""
        name, text = str(payload.get("workspace") or ""), payload.get("note")
        if not butler.available():
            raise ValueError("管家没开，没有工作区用途")
        if name not in panel_rules.workspace_names(ctx.state_dir) or not isinstance(text, str):
            raise ValueError("没有这个工作区")
        changes = panel_rules.ButlerPanel.notes(butler_settings.load(ctx.state_dir), {"workspaces": [name]}, {"note_0": text})
        _update_settings(ctx.state_dir, changes)
        return panel_rules.SAVED[WORKSPACE]

    def change_redaction(self, ctx, payload) -> str:
        """敏感词脱敏's two switches, from a page drawn before it was retired: refused."""
        raise ValueError(REDACTION_RETIRED)

    def change_terms(self, ctx, payload) -> str:
        """敏感词脱敏's terms, from a page drawn before it was retired: refused."""
        raise ValueError(REDACTION_RETIRED)

    def change_group(self, ctx, payload) -> str:
        """停用 / 启用 of one group (GroupManager ``switch``, the card's)."""
        key, on = str(payload.get("group") or ""), payload.get("on")
        if not isinstance(on, bool):
            raise ValueError("没说停用还是启用")
        manager = self.hub.panel(GROUPS)
        records, _ = manager.listed()
        chat = next((g["chat_id"] for g in records if group_key(g["chat_id"]) == key), "")
        if not chat:
            raise ValueError("没有这个群的记录了")
        words = manager.switch(ctx, chat, on, {c: n for c, (n, _) in self.names.items() if n})
        if words.startswith("⚠️"):
            raise ValueError(words.removeprefix("⚠️").strip())
        return words.removeprefix("✓").strip()

    def change_free(self, ctx, payload) -> str:
        """A group's 免@ set to 关闭 or 只有你 (FREE_ON_PAGE), by the card's own (GroupManager ``free``: its limits, its words)."""
        mode = payload.get("mode")
        if mode not in FREE_ON_PAGE:
            raise ValueError("网页上只能把免@ 改成关闭或只有你；“你和选中的人”“群里所有人”请到飞书设置卡上改")
        if not free_talk_on():
            raise ValueError("这个部署没有打开免@")
        chat = self.group_chat(payload)
        notice = self.hub.panel(GROUPS).free(ctx, chat, mode, {c: n for c, (n, _) in self.names.items() if n}, {}).get("notice") or ""
        if notice.startswith("⚠️"):
            raise ValueError(notice.removeprefix("⚠️").strip())
        return notice.removeprefix("✓").strip() or "已保存。"

    def group_chat(self, payload) -> str:
        key = str(payload.get("group") or "")
        records, _ = self.hub.panel(GROUPS).listed()
        chat = next((g["chat_id"] for g in records if group_key(g["chat_id"]) == key), "")
        if not chat:
            raise ValueError("没有这个群的记录了")
        return chat

    def change_workspace_add(self, ctx, payload) -> str:
        """添加 by the path typed (or ``设备别名:路径``), the card's ``add_workspace``: the directory checked for at most 5 seconds. A
        directory with ``.git`` goes into the repository map as well (v43)."""
        text = payload.get("path")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("请填写目录的绝对路径")
        name, added = add_workspace(ctx.state_dir, ctx.default_workdir, text.strip()[:1000])
        words = f"已添加工作区 {name}" if added else f"该目录已在列表中：{name}"
        value = str(load_config(ctx.state_dir, ctx.default_workdir)["workspaces"].get(name) or "")
        if value.startswith("/") and Path(value).joinpath(".git").exists():
            try:
                self.mirror_repo(ctx, value, name)
                words += "，也加进了常用路径" if added else ""
            except ValueError as exc:  # The workspace list is changed; the map says why it is not.
                words += f"（常用路径没有加：{exc}）"
        return words

    def mirror_repo(self, ctx, path, name) -> str:
        """A workspace's directory into the repository map, as ``name`` when it is free (repos_add); its receipt."""
        shown = clean_line(name, NAME_MAX)
        taken = {repo.name for repo in repo_entries(ctx.state_dir, ctx.default_workdir)[0]}
        return add_repo(ctx.state_dir, ctx.default_workdir, path, name=shown if shown not in taken else "")

    def mirrored_removal(self, ctx, names, config) -> str:
        """The repositories that were these workspaces (the same name and path) out of the map too, when it exists; '' or a line."""
        pairs = {(name, str(config["workspaces"].get(name) or "")) for name in names}
        try:
            forget_repos(ctx.state_dir, ctx.default_workdir, pairs)
        except ValueError as exc:
            return f"（常用路径没有改：{exc}）"
        return ""

    def change_workspace_remove(self, ctx, payload) -> str:
        """移除 of one entry (confirmed on the page first): the list only, the directory and the topics on it stay; the map's repository of
        the same name and path goes too."""
        name = str(payload.get("workspace") or "")
        config = load_config(ctx.state_dir, ctx.default_workdir)
        removed = remove_workspaces(ctx.state_dir, ctx.default_workdir, [name])
        if not removed:
            raise ValueError("该工作区已不在列表中")
        return f"已从列表移除 {name}，磁盘目录未删除" + self.mirrored_removal(ctx, removed, config)

    def change_workspace_prune(self, ctx, payload) -> str:
        """移除不存在的: every entry whose directory is confirmed gone (never the default, never one that did not answer)."""
        config = load_config(ctx.state_dir, ctx.default_workdir)
        removed = remove_workspaces(ctx.state_dir, ctx.default_workdir, missing_workspaces(config))
        if not removed:
            return "没有目录不存在的工作区"
        return f"已从列表移除 {len(removed)} 项，磁盘目录未删除" + self.mirrored_removal(ctx, removed, config)

    @staticmethod
    def text_of(payload, key, limit, *, need=False):
        """The text ``payload[key]`` (a string of at most ``limit`` characters), None when it is not given; ValueError when it is not text,
        too long, or ``need`` and empty."""
        value = payload.get(key)
        if value is None and not need:
            return None
        if not isinstance(value, str) or len(value) > limit or need and not value.strip():
            raise ValueError(f"{key} 要是不超过 {limit} 个字的文字" if not need else f"要说明 {key}（最多 {limit} 字）")
        return value

    def change_repos_set(self, ctx, payload) -> str:
        """repos_set: a repository's 用途 (``note``), ``status`` and ``write_mode``, those given."""
        name = self.text_of(payload, "name", NAME_MAX, need=True)
        note = self.text_of(payload, "note", NOTE_MAX)
        status, mode = payload.get("status"), payload.get("write_mode")
        if status is not None and not isinstance(status, str) or mode is not None and not isinstance(mode, str):
            raise ValueError("status 和 write_mode 要是文字")
        return set_repo(ctx.state_dir, ctx.default_workdir, name, note=note, status=status, write_mode=mode)

    def change_repos_add(self, ctx, payload) -> str:
        """repos_add: an existing absolute directory into the map, a 仓库 with ``.git`` and a 目录 without; v46 one on a device (``device``,
        or the path typed ``<别名>:/abs``)."""
        path = self.text_of(payload, "path", 500, need=True)
        return add_repo(ctx.state_dir, ctx.default_workdir, path, name=self.text_of(payload, "name", NAME_MAX) or "",
                        note=self.text_of(payload, "note", NOTE_MAX) or "", device=self.text_of(payload, "device", 24) or "")

    def change_repos_remove(self, ctx, payload) -> str:
        """repos_remove: a repository out of the map; its directory stays."""
        return remove_repo(ctx.state_dir, ctx.default_workdir, self.text_of(payload, "name", NAME_MAX, need=True))

    def change_worktrees_prune(self, ctx, payload) -> str:
        """worktrees_prune: the worktrees the owner named (``paths``, at most PRUNE_MAX), removed without --force: the receipt lists
        each and its result; the ones not named are not touched."""
        paths = payload.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and 0 < len(p) <= 500 for p in paths):
            raise ValueError("要点名要删的 worktree（paths）")
        if len(paths) > PRUNE_MAX:
            raise ValueError(f"一次最多删 {PRUNE_MAX} 个")
        self.worktrees = None  # The next page is scanned again.
        return prune_worktrees(paths)

    def nodes_panel(self):
        from covey.core import nodes
        if not nodes.enabled():
            raise ValueError("这个部署没有开设备（COVEY_NODE_LISTEN）")
        return self.hub.panel(NODES)

    def change_node_add(self, ctx, payload) -> str:
        return web_panels.node_add(ctx, self.nodes_panel(), payload)

    def change_node_remove(self, ctx, payload) -> str:
        return web_panels.node_remove(ctx, self.nodes_panel(), payload)

    def change_device_add(self, ctx, payload) -> str:
        return web_panels.device_add(ctx, payload)

    def change_device_set(self, ctx, payload) -> str:
        return web_panels.device_set(ctx, payload)

    def change_device_remove(self, ctx, payload) -> str:
        return web_panels.device_remove(ctx, (getattr(self.hub, "panels", None) or {}).get(NODES), payload)

    def change_device_check(self, ctx, payload) -> str:
        return web_panels.device_check(ctx, payload)

    def change_refresh(self, ctx, payload) -> str:
        """刷新: nothing changes; the settings are pushed again now (app/web_sync.py makes them due after any settings op)."""
        return "已刷新"

    def change_proxy(self, ctx, payload) -> str:
        panel = self.proxy_panel()
        if panel is None:
            raise ValueError("这个部署没有代答")
        return web_panels.proxy_change(ctx, panel, payload)
