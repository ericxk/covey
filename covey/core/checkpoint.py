"""Durable task checkpoints written by the agent and read by the bridge, and the artifacts a task lists in one."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

from covey.core.paths import expand_user
from covey.core.state import read_object, utc_now_iso, write_private_text

UPLOADS = "artifact-drive"  # <state>/artifact-drive/: each job's ledger of its artifacts' upload to the cloud space (app/artifacts.py).


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


def artifact_paths(record):
    """The files ``record``'s task registered as its artifacts: the checkpoint's ``artifacts``, its result file and its
    diff file, each once. What may be published of them is app/artifact_policy.py."""
    cp = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    raw = cp.get("artifacts", [])
    paths = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    if record.get("result_file_path"):
        paths.append(record["result_file_path"])
    if record.get("diff_artifact_path"):
        paths.append(record["diff_artifact_path"])
    return list(dict.fromkeys(paths))


def artifact_key(path):
    """The key a button and the upload ledger name an artifact by: a digest of its recorded path."""
    return hashlib.sha256(path.encode()).hexdigest()[:24]


def upload_ledger(state, key) -> dict:
    """The ledger of the cloud-space upload of job ``key``'s artifacts, as app/artifacts.py writes it ({} if none): ``files``
    {artifact_key: {name, size, sha256, token, url}}, ``errors`` {artifact_key: why}, ``status`` (uploading, done or failed) and
    ``folder_url``. Read without the Harness's voice: the file is named by Voice.safe_key (feishu/delivery.py safe_idempotency_key),
    a key of more than 50 characters by its digest after the Harness's prefix."""
    name = re.sub(r"[^A-Za-z0-9_-]", "-", str(key))
    folder = Path(state) / UPLOADS
    paths = [folder / (name + ".json")] if len(name) <= 50 else \
        sorted(folder.glob("*-" + hashlib.sha1(str(key).encode()).hexdigest()[:24] + ".json"))
    return next((ledger for ledger in map(read_object, paths) if ledger), {})


def release_user_wait(path: Path, *, drop_questions: bool = False) -> None:
    """The checkpoint at ``path`` no longer waits for the user; ``drop_questions`` also takes its form away."""
    data = read_checkpoint(path)
    data.update(requires_user=False, question="")
    if drop_questions:
        data.pop("questions", None)
    write_private_text(path, json.dumps(data, ensure_ascii=False))


# When a turn asks the owner at all (ASK_RULE): only what he must decide, ASK_SHORT, is asked; what can be undone and has a
# clear best choice is done that way and said in the conclusion with how to change it back; a request for a plan gets the
# whole plan first (PLAN_RULE): a question whose 推荐 option is all but always picked only stops the turn until the owner
# clicks it, and a plan asked about piece by piece before it is given leaves him choosing what he does not know yet. The
# butler's and a lead's handoffs name ASK_SHORT (core/butler_tool_specs.py ASK_NOTE, app/butler_handoff.py lead_note), so
# they need not write "never ask" into a task themselves. Every list of the five kinds a prompt gives is ASK_SHORT (or
# ASK_KINDS, its long form; tests/test_ask_rule.py). Which memory lets a turn go on without asking (ASK_GRANT, the owner's
# choice of T892 section 7 item 1): a rule the owner said himself, for the one kind of thing it names; never a deletion or
# anything else that cannot be taken back; never a rule the lead wrote or the Bridge learned (marked 负责人记的); and what a
# rule let spend money or take machines is said afterwards.
ASK_SHORT = "部署、删除、花钱、对外发送、占机器"
ASK_KINDS = ("部署上线和发布、删除和其他撤不回的操作（覆盖别人的改动、改线上数据、强推）、花钱、"
             "对外发送（以主人身份发消息、发到别人看得到的地方）、占机器（长时间占 GPU 或集群、起长期运行的服务、大量下载或写盘）")
ASK_GRANT = ("记忆里主人亲口立的“不用再问”的规矩（没标“负责人记的”），只放行它写明的那一类事（如“改完直接部署到本机”只放行部署到本机、"
             "“流量涨了直接扩容”只放行扩容），别的照常问；删除和其他撤不回的操作永远照常问；标“负责人记的”的规矩"
             f"（负责人整理的、自动学到的）不能放行{ASK_SHORT}这几类事；按规矩没问就花了钱、占了机器的，在结论里报一句做了什么。")
ASK_RULE = (
    f"- 什么时候问主人（问题卡、AskUserQuestion、request_user_input 都算）：{ASK_KINDS}照常问，主人这一轮明说要他自己定的也问。"
    + ASK_GRANT +
    "能自己查清的事先查，不问。其余能撤回、你有明确推荐的不问（实现细节、命名、默认值、先后顺序、几种做法挑一种、"
    "需求有几种理解时挑最可能的一种），按推荐直接做，在结论里写一行“按 X 做了；要改成 Y，回一句就行”，"
    "有更直接的改回办法（开关、配置项、要 revert 的提交号）就写那个。"
    "要问的留到最后：不等答案也能做的先做完，再把要问的合成一次问，不在半路停下来等。")
PLAN_RULE = (
    "- 主人要的是方案、设计或提案（“出个方案”“怎么做好”“先讨论一下”）时，这一轮先交完整方案，不先发问题卡问细节："
    "推荐的做法写全（做什么、怎么做、为什么、代价和风险、备选为什么不选），缺的信息按最合理的假设写并标明是假设；"
    "长的写成文档登记进 artifacts，回复里写要点。待定项列在方案后面，每项写你的推荐和不选它的代价，主人看完会在话题里接着说；"
    "待定项里有照常要问的那几类，才写进 questions。")
# What every turn carries (5.9 row 7): when to write, the fields, when and how to ask the user, recovery and files. It names
# no file: each turn's message gives the checkpoint file. The JSON is the same as before the split; only the words moved.
CHECKPOINT_RULES = "\n".join((
    "这是执行约定（检查点）：",
    "- 何时写：多阶段或长时间的任务，在开始执行、完成阶段、提交外部任务和等待用户时，把完整 JSON 写入本轮消息里"
    "“本任务检查点文件”给出的文件。原生工具支持时优先用原子替换；用户禁止 CLI 或工具不支持原子替换时，用原生文件工具"
    "完整覆盖并尽量回读核验，不要只为原子性请用户放开工具权限。检查点由桥接器读取、不在工作区内，写它不算修改工作区文件；"
    "用户要求不改文件时也照常写检查点。",
    "- 字段：phase（当前阶段，用用户的语言写成一句短语，任务卡状态行会显示它，不要写 waiting_for_x 这类英文标识）、"
    "completed（已完成步骤列表；子 Agent 返回后记一句它的结论，长报告写成文件、登记进 artifacts）、artifacts（产物绝对路径列表）、external_tasks（外部任务 ID/查询方式列表）、"
    "next_step（下一步）、requires_user（布尔值）、question（待用户回答的问题摘要）、updated_at（ISO 时间）。"
    "不要把密钥写入检查点。",
    ASK_RULE,
    "- 要用户决策时：保存 requires_user=true 和非空的 question 摘要。有多个待回答项时，另写 questions 数组，每题单独一个对象："
    '{"id":"稳定且唯一的标识","header":"短标题","question":"完整问题","options":[{"label":"选项","description":"说明"}]}；'
    '可以多选的题另加 "multiple":true，问题卡才给多选下拉框，否则只能单选；自由回答可省略 options；'
    "不要把多个独立决策合并进单个 question 输入框。"
    '题目不带背景看不懂时，另加 "context":"一句背景：这题在改什么、为什么现在问"，问题卡把它写在题目下面。'
    "单选题里你有把握哪个更稳妥时，把它排第一，在 label 末尾加“（推荐）”（一题只标一个），"
    "并在它的 description 里用一句话写不选它的代价。卡住了才来问的，在 question 里写清缺什么、已经试过什么、各是什么结果。",
    "- 用户怎么答：正常答题时，全部问题得到明确回答后再继续。只有一个待回答问题时，可告知用户直接回复本话题；"
    "有多个问题时，正常回答需在问题卡片中逐题作答并点击“提交回答”。用户也可以不逐题回答、只发补充说明（反馈原问题或更改需求）："
    "收到这种回答时应重新评估，不能把未回答部分视为同意或授权，也不能将补充说明仅当作漏答而坚持要求填完原表单。",
    PLAN_RULE,
    "- 最终回复先交代已形成的方案、依据和必要的决策背景，再列出待确认问题；没有形成方案或说明时应明确说明缺少什么，"
    "不得声称已有方案或编造内容。",
    "- 不要在后端进程中无限等待输入；等待长命令期间也应保留查询外部任务的方法。恢复或重试时（没有检查点也一样）先核查"
    "工作目录、已有产物与外部任务状态，再决定继续哪些步骤；不得仅凭旧步骤描述就假定成功，不得重复提交已存在的训练/部署等外部任务。",
    "- 用户要求交付文件时，完成文件并在 artifacts 中登记绝对路径；桥接任务卡提供发送文件入口，后端不需要单独的飞书附件工具，"
    "不要因此设置 requires_user=true 或请求开启消息工具。登记成功只说明文件已准备好，不代表已经发送，实际发送由任务卡处理。"
    "不要自己用 lark-cli 或别的工具往飞书发消息、图片或文件：你拿不到本话题的正确位置，会发到别处，用户看不到。",
))
# What only some turns use (``checkpoint_rules``): ``steps`` and ``delivery`` a turn that may run long or change code (a
# topic's), ``show_images`` one that may have an image to show at once (a topic's; the butler's when its message is about
# one, app/runs.py). The Bridge reads each the same whether its rule was given or not (``checkpoint_steps``,
# ``checkpoint_delivery``, app/turn_images.py).
STEPS_RULE = '- 长任务另写 steps 有序数组，每项 {"title":"短语","state":"done|doing|todo"}，同时只有一个 doing，开始和每完成一步都更新。'
DELIVERY_RULE = (
    '- 只有这一轮改了代码、有提交时才写 delivery：{"commit":"完整提交号（多个用空格分开）","deployed":"部署到哪里/第几代",'
    '"verified":"怎么验收的"}，没做的项留空；调研、报告、问答的轮次不写 delivery。'
    "verified 只写实际跑过的检查和结果，没验证就留空，别把推断写成已测；deployed 只写已生效的环境，只提了合并请求的留空。")
# 改了什么 / 去哪看 / 怎么验 on the answer card of a UI change or a turn the owner asked to accept (#21,
# ``checkpoint_acceptance``): carried with DELIVERY_RULE, as a turn that changes something writes both.
ACCEPTANCE_RULE = ('改了界面或主人说要验收时，另写 acceptance：{"what":"改了什么（30 字内，主人能看到的变化）",'
                   '"where":"去哪看：回复里给过的可点链接原样抄，没有不填","how":"怎么验：点哪里、做什么、该看到什么；'
                   '回复里没说的别编"}。')
# The delivery part as a turn's rules carry it (``checkpoint_rules``): DELIVERY_RULE, then ACCEPTANCE_RULE.
DELIVERY_PART = DELIVERY_RULE + "\n- " + ACCEPTANCE_RULE
IMAGES_RULE = (
    '- 要用户马上看的图片（登录二维码、要他确认的截图）写进 show_images：[{"path":"图片绝对路径","note":"一句说明","urgent":true}]，'
    "桥接器读到检查点就把图发到本话题，urgent 的同时发到私聊聊天区；同一路径的文件改写后会再发一次。"
    "这只在本轮还在执行时生效：等用户扫码或确认时让本轮保持运行。")


def checkpoint_rules(*, steps=True, delivery=True, images=True) -> str:
    """CHECKPOINT_RULES and, as asked, the parts only some turns use (STEPS_RULE, DELIVERY_RULE with ACCEPTANCE_RULE,
    IMAGES_RULE), in that order: the checkpoint contract a turn's rules carry. All of it by default, as a topic's turn
    has it."""
    return "\n".join((CHECKPOINT_RULES, *((STEPS_RULE,) if steps else ()),
                      *((DELIVERY_PART,) if delivery else ()), *((IMAGES_RULE,) if images else ())))


# An owner's message about an image to show at once (a login to scan, a screenshot to act on): a turn whose rules lack
# IMAGES_RULE is given it in its message (``images_note``).
IMAGE_WORDS = re.compile(r"二维码|扫码|扫一下|登录|登陆|截图|截屏|截个图|图片|screenshot|qr ?code", re.IGNORECASE)


def images_note(text) -> str:
    """IMAGES_RULE as a note of a turn's message when ``text`` (the owner's words) is about an image (IMAGE_WORDS); ''."""
    return IMAGES_RULE.removeprefix("- ") if IMAGE_WORDS.search(str(text or "")) else ""

# 话题要点 (COVEY_PROJECT_TIDY, core/topic_gist.py): put in each turn's message of an ordinary topic or a project's topic, after
# the checkpoint file (app/runs.py), never into the rules above, so a topic's rules stay the same with the switch on or off.
# Written when the topic has none or its goal or scope moved (#31): a follow-up question no longer costs a checkpoint write.
# A turn that writes none leaves the last one as it is (app/project_tidy.py saw keeps only what a turn wrote); a topic that
# never wrote one is read whole once by 整理专项 (app/project_sorter.py backfill).
TOPIC_GIST_RULE = (
    "话题要点：这个话题还没有要点，或这一轮让它的目标或涉及的范围变了，才在检查点里写 topic_gist（检查点里别的字段照常保留）；"
    "没变就不用写，Bridge 沿用上一份。写这个话题本质上在做什么："
    '{"essence":"本质，对象 + 要达成的事，一句话","goal":"现在的目标：要做成什么，不写进度和状态；做完了照抄原来的目标","involves":"涉及的仓库、系统、环节",'
    '"related":["相关话题的 T 号"],"changed":"和上次比变了什么，第一次写就写“第一次”"}。'
    "本质要读了整段对话再写，不写标题里第一件具体的事，也不写这件事现在所在的环节；开头聊的是一件事、后来改成了另一件，就写现在这件。"
)
GIST_BEFORE = "下面是上次的话题要点：目标和范围都没变就不写；变了就整份重写，本质变了在 changed 里写清从什么变成什么。"
GIST_NONE = "（这个话题还没有话题要点，这一轮第一次写。）"


def gist_note(previous_lines: str) -> str:
    """TOPIC_GIST_RULE with the topic's current gist (core/topic_gist.py lines) after GIST_BEFORE, or GIST_NONE for a topic
    that has none."""
    return TOPIC_GIST_RULE + "\n" + (GIST_BEFORE + "\n" + previous_lines if previous_lines else GIST_NONE)


def checkpoint_gist(cp):
    """The checkpoint's ``topic_gist`` as written (an object or text; None without one): core/topic_gist.py parse reads it."""
    raw = cp.get("topic_gist") if isinstance(cp, dict) else None
    return raw if isinstance(raw, (dict, str)) and raw else None


# The optional ``steps`` and ``delivery`` of CHECKPOINT_RULES, as the Bridge reads them (``checkpoint_steps``,
# ``checkpoint_delivery``): a model's slip (a wrong type, an unknown state, a second doing, a long title) is cut to
# what can be shown, never an error.
STEP_STATES = ("done", "doing", "todo")
STEP_TITLE_CHARS = 30
MAX_STEPS = 20
DELIVERY_FIELDS = ("commit", "deployed", "verified")
# Each field read as written up to this (the answer card's 改动 fold shows it all, not
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


ACCEPTANCE_FIELDS = ("what", "where", "how")
ACCEPTANCE_CHARS = {"what": 60, "where": 300, "how": 300}
LINK = re.compile(r"(?:https?://|applink\.feishu\.cn/|lark://)[^\s<>()（）\"'`，。；、]+", re.IGNORECASE)
LOCAL_HOST = re.compile(r"^(?:https?://)?(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\])(?:[:/]|$)", re.IGNORECASE)


def checkpoint_acceptance(cp) -> dict:
    """The checkpoint's ``acceptance`` (ACCEPTANCE_RULE) as {what, where, how} strings, each cut to its
    ACCEPTANCE_CHARS; {} without a what or a how to show."""
    raw = cp.get("acceptance") if isinstance(cp, dict) else None
    if not isinstance(raw, dict):
        return {}
    found = {name: _words(raw.get(name), ACCEPTANCE_CHARS[name]) for name in ACCEPTANCE_FIELDS}
    return found if found["what"] or found["how"] else {}


def acceptance_link(where) -> str:
    """The first link in ``where`` the owner can click (https, a Feishu applink), '' for none: a local address
    (localhost, 127.0.0.1) is not one."""
    for match in LINK.finditer(str(where or "")):
        url = match.group().rstrip(".,;:!?）)")
        if not LOCAL_HOST.match(url):
            return url if "://" in url else "https://" + url
    return ""


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
    """Whether a turn's answer card shows its 改动 fold: its
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


# ``verified`` is what the model says it checked, nobody's acceptance: the fold says 已自检, not 已验收 (which a
# reader took for the owner's or the Bridge's check). Words that say it was not checked ("尚未在手机上验证") are 未自检.
UNCHECKED = re.compile(r"^\s*(?:(?:尚未|暂未|还未|还没有?|没有?|未)[^，。；、,.;]{0,10}?(?:验证|验收|测试|自检|测过|跑过)"
                       r"|待(?:验证|验收|测试|自检)|untested\b|not (?:yet )?(?:verified|tested|checked)\b"
                       r"|(?:无|none|n/?a)\s*$)", re.IGNORECASE)


def checked(delivery) -> bool:
    """Whether ``delivery`` says something was checked: ``verified`` has words and they do not open by saying it was
    not (``UNCHECKED``)."""
    words = str((delivery or {}).get("verified") or "")
    return bool(words.strip()) and not UNCHECKED.search(words)


def change_summary(delivery, width=CHANGE_LINE_WIDTH) -> str:
    """``delivery`` in one line, the 改动 fold's title after "改动 · ": "951327a · 第 331 代 · 已自检" (the first
    commit short, "+1" for one more when the line has room; the deployment's "第 N 代" when it names one, else its
    first words, as many as leave room for 自检; 已自检 or 未自检, ``checked``), at most ``width`` wide (``_width``: a topic's side panel shows "改动 · " and 30, a
    sample wrapped its last word at 36) and cut alone when wider; '' without a delivery."""
    if not delivery:
        return ""
    commits = delivery_commits(delivery)
    first = _fit(short_commit(commits[0]), COMMIT_WIDTH) if commits else "未提交"
    deployed = str(delivery.get("deployed") or "")
    generation = GENERATION.search(deployed)
    verified = "已自检" if checked(delivery) else "未自检"
    room = width - _width(first) - _width(verified) - 2 * _width(" · ")  # 自检 stays: the deployment's words are cut.
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
    """Where ``steps`` are, in one short phrase: "第 3/5 步：<title>", "做完 2/5 步", "5/5 步都做完了" (docs/ui.md 3: 做完, not
    已完成); '' without."""
    if not steps:
        return ""
    total, doing = len(steps), current_step(steps)
    if doing is not None:
        return f"第 {doing[0]}/{total} 步：{doing[1]['title']}"
    done = sum(1 for step in steps if step["state"] == "done")
    return f"{total}/{total} 步都做完了" if done == total else f"做完 {done}/{total} 步"


def checkpoint_note(checkpoint: Path, previous: dict | None = None, *, shown: str = "") -> str:
    """This turn's checkpoint file and, when continuing, the previous status to verify. ``shown``: the path to name when
    the file the model writes is not ``checkpoint`` (a task on a device writes its copy; the hub's is ``checkpoint``)."""
    if previous:
        # The answers are in the message already, and "delivery":"unconfirmed" there reads as
        # though they never arrived.
        previous = {k: v for k, v in previous.items() if k != "saved_user_answers"}
        if isinstance(previous.get("checkpoint"), dict):
            previous["checkpoint"] = {k: v for k, v in previous["checkpoint"].items() if k != "saved_user_answers"}
    # What the dead turn's subagents delivered is a block of its own (``subagents_note``), not JSON in the status.
    delivered = (previous or {}).get("subagents")
    previous = {k: v for k, v in previous.items() if k != "subagents"} if previous else previous
    return f"本任务检查点文件：{shown or checkpoint}" + (
        "\n上次任务状态（仅供核查，不能直接信任为已完成）：\n" + json.dumps(previous, ensure_ascii=False)
        if previous
        else ""
    ) + subagents_note(delivered)


END_WORDS = {"finished": "做完", "failed": "出错", "unfinished": "没做完"}


def subagents_note(delivered) -> str:
    """The block of what the previous turn's subagents delivered (``delivered``: ``{"items": [{"description", "end",
    "text"}], "more": count, "files": [path]}`` as app/subagent_recovery.py writes it); '' for none or anything else."""
    if not isinstance(delivered, dict) or not isinstance(delivered.get("items"), list):
        return ""
    lines = []
    for item in delivered["items"]:
        if isinstance(item, dict) and isinstance(item.get("description"), str):
            end = END_WORDS.get(item.get("end"), "没做完")
            text = item.get("text") if isinstance(item.get("text"), str) and item.get("text") else "（没有文字）"
            lines.append(f"- {item['description']}（{end}）：{text}")
    if not lines:
        return ""
    files = [f for f in delivered.get("files") or [] if isinstance(f, str)]
    more = delivered.get("more") if type(delivered.get("more")) is int and delivered["more"] > 0 else 0
    tail = [f"另有 {more} 个子 Agent 的结果写在：" + "、".join(files)] if more and files else (
        ["更完整的结果写在：" + "、".join(files)] if files else [])
    return ("\n\n上一轮的这些子 Agent 已经交付：直接用它们的结果，不要重做；标“没做完”的才需要接着做，先核对再动手。\n"
            + "\n".join(lines + tail))


def audit_recovery(previous: dict, directory: Path, *, stat_paths=None) -> dict:
    """Check local artifact presence without executing actions from a checkpoint. ``stat_paths`` (a topic on a device,
    ``core/devices.py stat_paths``): ``directory`` is then the device's directory as text and the paths are looked at
    there, in one call, by the device: a list of ``{path, exists, size, escapes}`` per path, or an exception when it
    could not be asked (every path is then listed as not inspected, never as missing)."""
    cp = previous.get("checkpoint", previous)
    if not isinstance(cp, dict):
        cp = {}
    checks = []
    artifacts = cp.get("artifacts", [])
    if stat_paths is not None:
        return _audit_remote(cp, str(directory), [raw for raw in (artifacts[:100] if isinstance(artifacts, list) else [])
                                                  if isinstance(raw, str)], stat_paths)
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


def _audit_remote(cp: dict, directory: str, raws: list, stat_paths) -> dict:
    try:
        found = list(stat_paths(raws)) if raws else []
    except Exception:  # noqa: BLE001 - the device did not answer: what it holds is not known
        found = None
    checks = []
    for index, raw in enumerate(raws):
        item = found[index] if found is not None and index < len(found) else None
        if item is None:
            checks.append({"path": raw, "state": "device_not_inspected"})
        elif item.get("escapes"):
            checks.append({"path": raw, "state": "outside_workspace_not_inspected"})
        elif item.get("exists"):
            checks.append({"path": item.get("path", raw), "exists": True, "bytes": item.get("size", 0)})
        else:
            checks.append({"path": item.get("path", raw), "exists": False})
    return {
        "checked_at": utc_now_iso(),
        "workdir": directory,
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
