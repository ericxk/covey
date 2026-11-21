"""独立复核: a read-only topic that reviews one finished turn's changes, opened only when the owner clicks the button on
that turn's answer card (cards/answer_card.py review_element, KIND; COVEY_REVIEW=1), never by itself (#20).

The review gets only the owner's own words in the reviewed topic (``owner_words``: its history's owner rows up to that
turn), the topic's completion criteria (core/criteria.py, copied into the new topic, whose every turn is then told them)
and the turn's changes: the commits its checkpoint names and the diff the Bridge kept of it (core/diff.py's diff.md,
carried into the new topic's incoming_media). Not the reviewed topic's session, its replies or the model's own account of
what it checked: an implementer's reasoning leads its reviewer along.

The new topic runs where the reviewed one ran, its workspace and directory as 原目录 (an uncommitted change is there to
see), read-only as the butler's read-only topics are (``origin.readonly``; its message says what it may and may not do),
on a model of another vendor than the turn's that is available now (``pick``: core/model_names.py vendor_of,
app/availability.py usable), else another model, else the same one in a session of its own; a topic that redacts keeps
redacting, on a model that can. It is opened as the web page opens one (app/web_launch.py): app/butler_topics.py's topic
card at the top level of the chat, its topic.json bound (``origin.kind`` "review", ``review_of``), its first turn queued.
One review a turn: <state>/reviews/<token>.json keeps each step, so a second click, or one after a restart, opens nothing
more and says which topic it is. The click is the owner's alone (app/access.py), taken before topic routing
(app/unified.py PANELS) on a worker after its acknowledgement; nothing here holds a lock across a Feishu call.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from covey.app.card_actions import action_value
from covey.app.card_queue import CardActionQueue
from covey.cards.answer_card import REVIEW_KIND, review_element
from covey.core import criteria
from covey.core.process_env import env_flag
from covey.core.state import keyed_json_path, read_object, write_json

ENV = "COVEY_REVIEW"
KIND = REVIEW_KIND
FOLDER = "reviews"
TITLE_CHARS = 40
OWNER_ROWS = 6  # The owner's latest messages in the reviewed topic the review quotes.
OWNER_CHARS = 1500  # Of each.
ENDED = frozenset({"done", "continued", "waiting_user"})


def enabled() -> bool:
    """Whether answer cards offer 独立复核 (COVEY_REVIEW=1; off unset)."""
    return env_flag(ENV, False)


def buttons(job) -> list:
    """The buttons under an answer's 输入/输出 box (cards/answer_card.py tail's ``buttons``): 独立复核 (review_element) for
    a turn of a topic on this machine, not the butler's own, that changed code (app/answer_cards.py changed_code), with
    the switch on (``enabled``); [] otherwise."""
    from covey.app import answer_cards

    from covey.core.butler import is_butler_topic

    def field(name):
        return job.get(name) if isinstance(job, dict) else getattr(job, name, None)
    topic, job_id = str(field("topic_id") or ""), field("job_id")
    if (not enabled() or not topic or not job_id or field("device") or is_butler_topic(topic, field("chat_id"))
            or not answer_cards.changed_code(job)):
        return []  # The butler's own turns (its family: a lead too) review nothing of a topic's.
    return [review_element(topic, job_id)]


def token_of(topic_id, job_id) -> str:
    return hashlib.sha256(f"{topic_id}\0{job_id}".encode()).hexdigest()[:24]


def record_path(state_dir, token) -> Path:
    return keyed_json_path(Path(state_dir), FOLDER, token)


def owner_words(history_path, message_id, rows=OWNER_ROWS, chars=OWNER_CHARS) -> list:
    """[(HH:MM or '', words)] of the owner's latest ``rows`` messages in a topic's history (its user rows, as its cards
    show them) up to the one ``message_id`` names, oldest first; all of them up to now when it names none there."""
    from covey.core.history_storage import history_lines

    found, seen = [], not message_id
    try:
        for line in history_lines(Path(history_path), newest_first=True):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("role") != "user":
                continue
            seen = seen or row.get("message_id") == message_id
            if not seen:
                continue
            words = str(row.get("display_content") or row.get("content") or "").strip()
            if words:
                at = str(row.get("time") or "")
                found.append((at[11:16] if len(at) >= 16 else "", words if len(words) <= chars else words[:chars - 1] + "…"))
            if len(found) >= rows:
                break
    except OSError:
        return []
    if not found and message_id:
        return owner_words(history_path, "", rows, chars)
    return list(reversed(found))


def pick(topics, runtime, state_dir, chat, topic, redacting):
    """The menu key (app/launch_models.py available_models) the review of a turn of ``topic`` (its topic.json) runs on,
    '' when none can: the chat's most used first (app/model_usage.py ranked), only Harnesses usable now
    (app/availability.py usable) and, for a topic that redacts, models that can; of those one of another vendor than
    the turn's model (core/model_names.py vendor_of), else another model, else the topic's own."""
    from covey.app import availability, model_usage
    from covey.app.launch_models import available_models
    from covey.core.menu_choice import harness_of
    from covey.core.model_names import vendor_of

    harness = str(topic.get("harness_id") or "")
    app = topics.apps.get(harness) or topics.apps.get(topics.default) or next(iter(topics.apps.values()), None)
    if app is None:
        return ""
    choices = available_models(topics, app.models.configured_context(app.harness.context))
    ranked = model_usage.ranked(choices, model_usage.recent(state_dir, chat))
    turn = vendor_of(harness, str(topic.get("model") or ""))
    usable = {name: availability.usable(runtime, state_dir, name) for name in topics.apps}
    keys = [key for key, entry in ranked.items()
            if usable.get(harness_of(key, entry), False) and (entry.get("redaction") or not redacting)]
    own = f"{harness}:{topic.get('model') or ''}"
    other = [key for key in keys
             if vendor_of(harness_of(key, ranked[key]), ranked[key].get("model") or key.partition(":")[2],
                          ranked[key].get("display_name")) != turn]
    return next(iter(other or [key for key in keys if key != own] or keys), "")


def review_prompt(*, label, title, source, job_id, workdir, words, commits, absent, diff_items) -> str:
    """What the review topic's model gets first (see the module's docstring): the Bridge's note, the owner's words, the
    turn's changes, how to answer; the diff's attachment block after it."""
    from covey.core.attachments import attachment_block

    named = f"{label}「{title}」" if label else f"「{title}」"
    parts = [
        f"[Bridge 说明] 你在话题 {named}。主人在 {source} 的答案卡上点了「独立复核」：你来独立复核 {source} 任务 #{job_id} "
        "这一轮的改动。你只拿到主人的原话、完成标准和这一轮的改动，没有原话题的过程和它对自己的评价，这是有意的："
        "按要求和改动本身判断，不要猜它为什么这么做。",
        "这是只读复核：可以读文件、看 git 记录（git show、git diff、git log）、跑不改文件的检查；不要改文件、提交、推送、"
        f"部署，不要停止或重启任何任务和服务。工作区：{workdir}（和 {source} 同一个目录，没提交的改动也在这里）。",
    ]
    if words:
        quoted = [f"主人的原话（{source}，按时间）："]
        for at, text in words:
            quoted.append(f"（{at}）" if at else "（时间不详）")
            quoted += ["> " + line for line in text.split("\n")]
        parts.append("\n".join(quoted))
    changes = ["这一轮的改动："]
    for commit in commits:
        changes.append(f"- 提交 {commit}" + ("（工作区的仓库里没有这个提交，可能写错了）" if commit in absent else "（用 git show 看）"))
    if diff_items:
        changes.append("- 差异报告：见下方“收到的附件”里的 diff.md（Bridge 在这一轮结束时记下的工作区差异）")
    if len(changes) == 1:
        changes.append("- Bridge 没有记下提交号和差异报告：用 git status、git diff、git log 看工作区里这一轮留下的改动")
    parts.append("\n".join(changes))
    parts.append("回复先写结论（能不能用、要不要返工），再按严重程度列问题：阻断（必须改）、应改、可选；每条写文件和行号、"
                 "问题、为什么、建议怎么改；有完成标准时逐条写满足 / 没满足 / 没查。最后一行写“阻断 N · 应改 N · 可选 N”。")
    if diff_items:
        parts.append("\n".join(attachment_block(diff_items)))
    return "\n\n".join(parts)


class Review(CardActionQueue):
    """The 独立复核 click (app/unified.py PANELS), on a worker after its acknowledgement. ``env``: what
    app/butler_topics.py's topic card and binding take (app/butler_env.py ButlerEnv)."""

    def __init__(self, topics, env):
        super().__init__(topics, name="review", workers=1, label="review")
        self.env = env

    @staticmethod
    def matches(raw):
        return (raw or {}).get("kind") == KIND

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is KIND on a card of a topic that still routes, else None: a retired Harness's
        card goes on to topic routing, which says so."""
        if not self.matches(raw):
            return None
        raw = action_value(event)
        chat = str(event.get("chat_id") or "")
        if self.topics.target(ctx.state_dir, raw.get("topic_id"), chat) is None:
            return None
        if not owner:
            return "owner_only"  # A review starts a task of the owner's.
        self.submit("review not opened", self.open, ctx, event, raw)
        return "review"

    def tell(self, app, target, chat, text, name, click):
        try:
            app.messenger.reply(target, chat, text, app.key(name, click), voice=app.voice)
        except Exception as exc:  # noqa: BLE001 - the record says what was done
            self.topics.runtime.log(f"review notice failed: {type(exc).__name__}")

    def open(self, ctx, event, raw):
        """Open the review of the clicked turn once (see the module's docstring); every step is kept in its record."""
        chat, click = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        found = self.topics.target(ctx.state_dir, raw.get("topic_id"), chat)
        if found is None:
            return
        app, target, topic = found
        try:
            job_id = int(raw.get("job_id") or 0)
        except (TypeError, ValueError):
            return
        state = Path(ctx.state_dir)
        token = token_of(target.topic_id, job_id)
        path = record_path(state, token)
        saved = read_object(path)
        if saved.get("answer"):
            self.tell(app, target, chat, saved["answer"]["again"], "review-again", click)
            return
        if not saved.get("plan"):
            record = app.harness.topic_record(target, chat, job_id) or {}
            plan = self.plan(state, chat, target, topic, record)
            if isinstance(plan, str):
                self.tell(app, target, chat, f"没开独立复核：{plan}", "review-refused", click)
                return
            from covey.core.topics import topic_label, topic_title

            source = topic_label(state, target.topic_id) or "原话题"
            # "topic_id" is the review topic's, as app/butler_topics.py bind_topic writes it.
            saved = {"token": token, "chat_id": chat, "source_topic_id": target.topic_id, "job_id": job_id, "source": source,
                     "title": f"独立复核：{source} {topic_title(state, target.topic_id)}".strip()[:TITLE_CHARS],
                     "why": f"独立复核 {source} 任务 #{job_id}", "plan": plan, "at": time.time(), "sequence": 0,
                     "message_id": str(record.get("message_id") or ""), "checkpoint_path": record.get("checkpoint_path") or "",
                     "diff_artifact_path": record.get("diff_artifact_path") or "", "criteria": criteria.of_topic(topic),
                     "history_path": str(target.history_path)}
            write_json(path, saved, ensure_ascii=False, indent=1)
        answer = self.launch(path, saved, chat, target)
        if answer is None:
            self.tell(app, target, chat, "独立复核的话题卡没有发出去，没有开话题；可以稍后再点。", "review-unsent", click)
            return
        self.tell(app, target, chat, answer["first"], "review-opened", click)

    def plan(self, state, chat, target, topic, record):
        """The review topic's plan (app/butler_topics.py's keys), or a str saying why there is none."""
        from covey.app.butler_model_choice import resolve
        from covey.core import butler_settings
        from covey.core.redaction import TOPIC_KEY

        if record.get("status") not in ENDED:
            return "这一轮还没结束，结束后再点。"
        if record.get("device") or topic.get("device"):
            return "话题在设备上，独立复核还不能开在设备上。"
        workdir = str(topic.get("workdir") or record.get("workdir") or "")
        if not workdir or not Path(workdir).is_dir():
            return "这个话题的工作目录不在了。"
        redacting = topic.get(TOPIC_KEY) is True
        key = pick(self.topics, self.topics.runtime, state, chat, topic, redacting)
        if not key:
            return "现在没有能用的模型（开着脱敏的话题只能用能脱敏的模型）。"
        harness, _, model = key.partition(":")
        found = resolve(self.topics, state, chat, butler_settings.load(state).topic_defaults,
                        {"harness": harness, "model": model})
        if isinstance(found, str):
            return found
        return {"harness": found["harness"], "model": found["model"], "model_key": found["model_key"],
                "effort": found["effort"], "work_mode": "", "start": True, "run_mode": "direct", "redaction": redacting,
                "workspace": str(topic.get("workspace_name") or record.get("workspace_name") or Path(workdir).name),
                "workdir": workdir}

    def launch(self, path, saved, chat, target):
        """The review topic's card, binding and first turn, each once; its answer ({first, again}), None when its card
        did not go out."""
        from covey.app import butler_topics
        from covey.core.history_storage import append_history
        from covey.core.jobs import find_job_record
        from covey.core.locks import LOCK
        from covey.core.topics import load_topic

        env, plan, token = self.env, saved["plan"], saved["token"]
        app = self.topics.apps.get(plan["harness"])
        if app is None:
            return None
        config = butler_topics.config_line(env, plan)
        if not butler_topics.send_root(env, path, saved, chat, saved["title"], config, "review-" + token):
            return None
        root = saved["root_message_id"]
        event = {"chat_id": chat, "message_id": root, "root_id": root}
        request = SimpleNamespace(id="review-" + token, job_key="")
        label = butler_topics.bind_topic(env, app, request, path, saved, {"readonly": True, "criteria": saved["criteria"]},
                                         event, config, kind="review")
        mid = "review-" + token
        new_ctx = app.submission.event_context(app.harness.context, event)
        if not (new_ctx.history_path.parent / "topic.json").is_file():
            return None
        with LOCK:
            data = load_topic(Path(env.state_dir), new_ctx.topic_id)
            if not (data.get("origin") or {}).get("review_of"):
                data.setdefault("origin", {})["review_of"] = {"topic_id": saved["source_topic_id"], "job_id": saved["job_id"]}
                write_json(new_ctx.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
        if not (find_job_record(Path(env.state_dir), mid) or app.runtime.jobs.settling(mid)):
            prompt = self.prompt(saved, label, new_ctx)
            content = f"独立复核 {saved['source']} 任务 #{saved['job_id']} 的改动"
            append_history(new_ctx.history_path, "user", content, {"message_id": mid, "chat_id": chat})
            app.submission.enqueue_chat_job(new_ctx, content, chat, mid, prompt_content=prompt, title=saved["title"])
        model = app.cards.model_label(app.harness.context, plan["model"]) if plan["model"] else ""
        named = f"{label}「{saved['title']}」" if label else f"「{saved['title']}」"
        how = f"{model or app.spec.title}，只读" + ("，开着脱敏" if plan.get("redaction") else "")
        answer = {"first": f"已开 {named}（{how}）：它只看你的原话、完成标准和这一轮的改动，结论在它的话题里。",
                  "again": f"这一轮已经开过独立复核：{named}，结论在它的话题里。"}
        saved["answer"] = answer
        write_json(path, saved, ensure_ascii=False, indent=1)
        return answer

    def prompt(self, saved, label, new_ctx):
        """The first turn's message (``review_prompt``) from what the record kept of the reviewed turn; its diff carried
        into the new topic's incoming_media."""
        from covey.app import butler_handoff
        from covey.core.attachments import ListedAttachment
        from covey.core.checkpoint import checkpoint_delivery, delivery_commits, read_checkpoint
        from covey.core.diff import COMMIT_ID, missing_commits

        state = Path(self.env.state_dir)
        delivery = checkpoint_delivery(read_checkpoint(Path(saved["checkpoint_path"]))) if saved["checkpoint_path"] else {}
        commits = [commit for commit in delivery_commits(delivery) if COMMIT_ID.fullmatch(commit)]
        absent = set(missing_commits(saved["plan"]["workdir"], commits) or ()) if commits else set()
        diff = str(saved.get("diff_artifact_path") or "")
        items = []
        if diff and Path(diff).is_file():
            items = butler_handoff.carry([ListedAttachment("file", "diff.md", diff)],
                                         new_ctx.history_path.parent / "incoming_media", (state / "job-diffs",))
        words = owner_words(saved.get("history_path") or "", saved.get("message_id") or "")
        return review_prompt(label=label, title=saved["title"], source=saved["source"], job_id=saved["job_id"],
                             workdir=saved["plan"]["workdir"], words=words, commits=commits, absent=absent,
                             diff_items=items)

