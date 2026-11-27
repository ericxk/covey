"""话题转交管家 (docs/architecture.md 话题转交管家, docs/projects.md 在话题里说开专项, COVEY_PROJECTS=1 with the butler on):
what the owner asks of a topic that only the butler can do (open a project, put this topic into one, open another topic,
hand something to another topic) goes from the topic to the butler, so he does not say it again at the top level. The
butler sees every project and topic (its snapshot, projects_list), so it, not the topic, judges whether a project for it is
there already.

- The rule (``RULE``, ``offered``; app/runs.py puts it after the checkpoint file): in the owner's own turn (``direct``: his
  Feishu message, his words from the web page or his answer to the topic's question (``owner_answer``), his by rule 11's
  ``owner_turn``; not a hand-on of the butler's or a lead's, an answer the butler or a lead gave for him, a wake-up, a
  revisit, an automatic retry, a 协作 turn) of an ordinary topic or a project's
  topic in a chat with a butler; not the butler's family (its leads too), not a 代答 topic. The topic writes
  ``ask_butler`` ({ask, why}, ``wanted``) into its checkpoint and says in its reply that it is handed on.
- Handed on (``collect``, the Bridge once the turn's runner is gone: app/detached.py after_job, never in a task runner,
  which queues no job): a turn that ended well (done or waiting_user, its reply authorized: rule 5) with ``ask_butler``
  is handed on once (its record <state>/butler-asks/<key>.json is written before anything is sent, rule 1; the same
  words of the same topic within SAME_DAYS days are not handed again, a later turn copying its checkpoint): a text
  message of the bot's at the top level of the chat (``message_text``: "↪ T12 转给管家", the owner's words in the topic,
  the topic's reading of them; the butler's model also reads his words whole, ``butler_prompt``: a forward's messages, the
  message he quoted, W0-3), then a turn of the butler's on that message, queued (never a 补充 of a running one) with
  the turn source ``handover`` (core/turn_source.py: the owner asserted it, the Bridge recorded it, its origin the topic
  and the owner's message). The butler answers it as any of his turns (its rule: app/prompts.py ASKED_RULE), its answer
  under that message.
- Not handed on: no butler can take it (the butler off, its Harness not available), the message did not go out (a send
  that may have gone is never sent again, rule 1) or the butler's turn could not be queued: a line in the topic's thread
  says so (``NOT_HANDED``) and the owner says it to the butler himself. A step that raises counts as what it left (the
  message not sent, perhaps sent, or the turn not queued): never ``sending`` or ``sent`` with nothing said. An automatic
  retry's ask is not handed on either, and its thread says so (the rule is not in such a turn; a copied checkpoint would
  else be taken as handed on).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from covey.app.butler_handoff import asked
from covey.core import butler, projects, triage, turn_source
from covey.core.attachments import message_words
from covey.core.checkpoint import read_checkpoint
from covey.core.history_storage import append_history, history_lines
from covey.core.jobs import context_for_job, find_job_record, job_field
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json
from covey.core.text import cut
from covey.core.topics import load_topic, remember_topic_message, topic_file, topic_label
from covey.core.web_sync import PREFIX as WEB_PREFIX

FIELD = "ask_butler"
FOLDER = "butler-asks"
ASK_CHARS, WHY_CHARS, WORDS_CHARS = 400, 120, 200
ENDED_WELL = ("done", "waiting_user")
DIRECT = ("om_", WEB_PREFIX)  # The message ids of the owner's own words the Bridge took in: Feishu's, the web page's.
ANSWER = "answer-"  # An answer's continuation: "answer-<question token>" (app/questions.py).
NOT_HIS = ("main-", "lead-")  # The answer events of the butler's question_answer and a lead's project_answer.
SAME_DAYS = 7
# Put after the checkpoint file in the owner's own turns of a topic (app/runs.py ``_ask_note``), only where ``offered``.
RULE = ("要管家办的事：主人这一轮要的事只有管家能办（开专项、把这个话题收进某个专项、另开一个话题、交给别的话题）时，"
        "不要请他再去对管家说一遍：在检查点写 ask_butler：{\"ask\":\"要管家办什么，用主人的说法写清楚：专项名、目标、"
        "要收进的话题（这个话题算一个）\",\"why\":\"一句：为什么要管家办\"}，回复里说这件事交给管家了。"
        "本轮结束后 Bridge 把它连同主人这一轮的原话交给管家；管家看得到全部专项和话题，"
        "开新专项还是归进已有的由它判断，你不用猜有哪些专项。主人只是问要不要开、或这件事你自己能做，就不写。")
HEAD = "↪ {label} 转给管家"  # The message's first line, as the butler's rule names it.
NOT_HANDED = {
    "no_butler": "⚠️ 这件事没能交给管家：管家现在没有开（或它的 Harness 不可用）。请直接在私聊里对管家说。",
    "failed": "⚠️ 这件事没能交给管家：消息没发出去。请直接在私聊里对管家说。",
    "uncertain": "⚠️ 交给管家的消息可能没发出去：私聊里没看到“↪ {label} 转给管家”的话，请直接对管家说。",
    "not_queued": "⚠️ “↪ {label} 转给管家”那条消息发出去了，但管家的这一轮没排上。请引用那条消息对管家再说一遍。",
    "retry": "⚠️ 这一轮是自动重试，要管家办的事没有转过去（只有你亲自发的那一轮才转）。请直接在私聊里对管家说。",
}


def enabled() -> bool:
    """Whether a topic may hand work to the butler: COVEY_PROJECTS=1 and the butler on (core/butler.py enabled)."""
    return projects.enabled() and butler.enabled()


def retried(job) -> bool:
    """Whether ``job`` (a job or its record) is an automatic retry: of a limit or a fault (app/auto_retry.py counts it), or
    after a safety refusal (app/policy_retry.py)."""
    from covey.app.auto_retry import COUNTS

    return bool(job_field(job, "policy_retry")) or any(int(job_field(job, name) or 0) > 0 for name in COUNTS)


def owner_answer(state_dir, message_id) -> bool:
    """Whether ``message_id`` is the continuation of an answer the owner gave himself (on the question card, from the web
    page, or a message of the topic taken as one): "answer-<token>" of a question on record that neither the butler
    (question_answer, "main-…") nor a lead (project_answer, "lead-…", its 分流 answered) answered for him. Never raises."""
    token = message_id.removeprefix(ANSWER) if message_id.startswith(ANSWER) else ""
    if not state_dir or not token or "/" in token or token.startswith("."):
        return False
    try:
        question = read_object(Path(state_dir) / "questions" / (token + ".json"))
    except (OSError, ValueError):
        return False
    return (bool(question) and triage.state(question) != triage.ANSWERED
            and not str(question.get("answer_event") or "").startswith(NOT_HIS))


def direct(job, access, state_dir="") -> bool:
    """Whether ``job`` (a job or its record) is the owner's own words to the topic: started by his Feishu message, his
    words from the web page or his answer to the topic's question (``owner_answer``, read under ``state_dir``; not the
    butler's or a lead's hand-on or answer, a wake-up, a revisit), of a source whose words are his as before (no 协作
    turn), not an automatic retry, and his (app/access.py owner_turn)."""
    message = str(job_field(job, "message_id") or "")
    return ((message.startswith(DIRECT) or owner_answer(state_dir, message))
            and turn_source.kind(job) in (turn_source.LEGACY, turn_source.OWNER) and not retried(job)
            and bool(access.owner_turn(str(job_field(job, "chat_id") or ""), str(job_field(job, "sender_id") or ""))))


def topic_ok(state_dir, topic_id, chat_id, access) -> bool:
    """Whether ``topic_id`` may hand on: an ordinary topic or a project's topic of a chat with a butler; not the butler's
    family (a lead is of it), not a 代答 topic."""
    if not topic_id or butler.is_butler_topic(topic_id, chat_id) or not access.agent_chat(chat_id):
        return False
    try:
        return not load_topic(Path(state_dir), topic_id).get("proxy")
    except (OSError, ValueError):
        return False


def offered(access, state_dir, job, topic_id="") -> bool:
    """Whether ``job``'s turn message carries ``RULE``: on (``enabled``), the owner's own turn, a topic that may hand on
    (``topic_id``, else the job's)."""
    topic_id = str(topic_id or job_field(job, "topic_id") or "")
    return (enabled() and direct(job, access, state_dir)
            and topic_ok(state_dir, topic_id, str(job_field(job, "chat_id") or ""), access))


def wanted(cp) -> dict | None:
    """The checkpoint's ``ask_butler`` as {ask, why} (a text alone is the ask), cut to what is shown; None without one."""
    raw = cp.get(FIELD) if isinstance(cp, dict) else None
    if isinstance(raw, str):
        raw = {"ask": raw}
    if not isinstance(raw, dict):
        return None
    ask = " ".join(str(raw.get("ask") or "").split())
    if not ask:
        return None
    return {"ask": cut(ask, ASK_CHARS), "why": cut(" ".join(str(raw.get("why") or "").split()), WHY_CHARS)}


def record_path(state_dir, key) -> Path:
    return Path(state_dir) / FOLDER / (hashlib.sha256(str(key).encode()).hexdigest()[:32] + ".json")


def same_words(text) -> str:
    return "".join(str(text or "").split())


HANDED = ("sending", "sent", "uncertain", "not_queued")  # Words that reached the butler, or may have: not handed again.


def handed_before(state_dir, topic_id, ask, now) -> bool:
    """Whether ``topic_id`` handed the same words on in the last SAME_DAYS days (a later turn that copied its checkpoint);
    words that certainly did not reach the butler (no butler, a send that failed) may go with a later turn."""
    try:
        paths = list((Path(state_dir) / FOLDER).glob("*.json"))
    except OSError:
        return False
    words = same_words(ask)
    for path in paths:
        record = read_object(path)
        if (record.get("topic_id") == topic_id and same_words(record.get("ask")) == words and record.get("state") in HANDED
                and now - float(record.get("at") or 0) < SAME_DAYS * 86400):
            return True
    return False


HISTORY_LOOK = 400  # Lines of the topic's history read back, newest first, for the owner's message.


def owner_words(state_dir, topic_id, message_id, key) -> str:
    """The owner's words the turn began with, as the Bridge took them in: the user line of ``message_id`` in the topic's
    history (core/attachments.py message_words: without the blocks the Bridge added), else the turn's record
    (app/butler_handoff.py asked)."""
    path = topic_file(Path(state_dir), topic_id).parent / "history.jsonl"
    try:
        for number, line in enumerate(history_lines(path, newest_first=True)):
            if number >= HISTORY_LOOK:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("role") == "user" and row.get("message_id") == message_id:
                words = message_words(str(row.get("display_content") or row.get("content") or ""))
                if words:
                    return words
    except OSError:
        pass
    return asked(state_dir, key).words


def message_text(label, ask, words) -> str:
    """The message the butler's turn reads, as the owner sees it at the top level: the owner's words first (what the butler
    goes by), then the topic's reading of them (a note)."""
    said = cut(" ".join(str(words or "").split()), WORDS_CHARS)
    return "\n".join((HEAD.format(label=label), *((f"你在 {label} 说：{said}",) if said else ()), f"{label} 的理解：{ask}"))


WHOLE_HEAD = "主人在 {label} 的原话全文（含合并转发和引用的消息，上面只是开头）："


def whole_words(record, words) -> str:
    """The owner's message the topic's turn began with (his Feishu or web message: ``record``, its job record), whole, as the
    topic's model read it (its ``prompt_text``, app/butler_handoff.py model_words: a merged forward's messages, the message he
    quoted), for the butler's model; '' when it says no more than ``words`` or is not his message (W0-3: the butler got 200
    characters of it, a forward as "[合并转发 3 条]")."""
    from covey.app.butler_handoff import model_words
    from covey.app.project_lead import OWNER_PREFIXES

    record = record or {}
    if not str(record.get("message_id") or "").startswith(OWNER_PREFIXES):
        return ""  # An answer's turn, a continuation: its prompt is the Bridge's, not his message.
    whole = model_words(str(record.get("prompt_text") or ""))
    return whole if whole and " ".join(whole.split()) != " ".join(str(words or "").split()) else ""


def butler_prompt(text, label, whole) -> str:
    """What the butler's model reads of the handed-on message ``text``: the message itself, then the owner's words whole when
    there is more of them (``whole_words``); the card and the history show ``text`` alone."""
    if not whole:
        return text
    return text + "\n\n" + WHOLE_HEAD.format(label=label) + "\n" + "\n".join("> " + line for line in whole.split("\n"))


def collect(gateway, job, ctx) -> None:
    """After a task's runner exited: its ``ask_butler`` handed on, as the module says. Never raises. Nothing in a task
    runner (app/detached.py after_job calls none there): a runner queues no job; its Bridge hands it on once it exits."""
    if getattr(gateway.runtime, "in_runner", False):
        return
    try:
        if not enabled():
            return
        record = find_job_record(ctx.state_dir, str(getattr(job, "message_id", "") or ""))
        path = str((record or {}).get("checkpoint_path") or "")
        ask = wanted(read_checkpoint(Path(path))) if path else None
        if not ask:
            return
        hand_on(gateway, job, ctx, record, ask)
    except Exception as exc:  # noqa: BLE001 - the turn's ending goes on; the checkpoint keeps the ask
        gateway.runtime.log(f"butler ask collect failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}: {str(exc)[:200]}")


def hand_on(gateway, job, ctx, record, ask) -> None:
    state, access = Path(ctx.state_dir), gateway.runtime.access
    topic, chat, key = str(record.get("topic_id") or ""), str(record.get("chat_id") or ""), str(record.get("key") or "")
    reason = ("not-finished" if record.get("status") not in ENDED_WELL
              else "not-authorized" if not record.get("verified_reply_authorized")
              else "retry" if retried(record) or retried(job)
              else "not-the-owners" if not direct(record, access, state)
              else "topic" if not topic_ok(state, topic, chat, access) else "")
    if reason or not key:
        gateway.runtime.log(f"butler ask ignored reason={reason or 'no-key'} job=#{record.get('job_id')}")
        if reason == "retry" and topic_ok(state, topic, chat, access):
            tell(gateway, job, ctx, record, "retry", topic_label(state, topic) or topic[:8])
        return
    path, now = record_path(state, key), time.time()
    label = topic_label(state, topic) or topic[:8]
    with resource_lock("butler-ask", str(path)):  # The Bridge's own lock: collect runs in the Bridge alone.
        if read_object(path):
            return  # Handed on (or tried) once already: a turn read twice hands on once.
        if handed_before(state, topic, ask["ask"], now):
            gateway.runtime.log(f"butler ask skipped job=#{record.get('job_id')}: the same words were handed on")
            return
        words = owner_words(state, topic, str(record.get("message_id") or ""), key)
        text = message_text(label, ask["ask"], words)
        prompt = butler_prompt(text, label, whole_words(record, words))
        group = chat in getattr(getattr(access, "spaces", None), "personal_groups", ())
        mine = {"job_key": key, "job_id": record.get("job_id"), "topic_id": topic, "chat_id": chat, "ask": ask["ask"],
                "why": ask["why"], "text": text, "group": group, "state": "sending", "at": now}
        write_json(path, mine, ensure_ascii=False, indent=2)
    base = gateway.topics.apps[gateway.topics.default].harness.context
    owner = gateway.runtime.owner_open_id or ""
    event = {"chat_id": chat, "chat_type": "group" if group else "p2p", "sender_id": owner, "sender_type": "user",
             "message_type": "text"}
    from covey.feishu.transport import last_failure

    # What it settles as when a step raises: the message not sent, perhaps sent (never sent again, rule 1), sent with no
    # turn of the butler's; never left "sending" or "sent" with nothing to come of it and nothing said (T688).
    outcome, stage, queued = "", "failed", None
    try:
        app = gateway.butler.app_for(base, event, True)
        if app is None:
            outcome = "no_butler"
        else:
            stage = "uncertain"
            message = gateway.messenger.send_text(base.profile, chat, text,
                                                  gateway.voice.safe_key(f"butler-ask-{path.stem[:24]}"))
            if not message:
                failure = last_failure()
                outcome = "failed" if failure is not None and not failure.ambiguous else "uncertain"
            else:
                stage, mine = "not_queued", {**mine, "message_id": message, "sent_at": time.time()}
                write_json(path, {**mine, "state": "sent"}, ensure_ascii=False, indent=2)
                queued = queue_turn(gateway, base, app, chat, message, text, owner, topic,
                                    str(record.get("message_id") or ""), prompt=prompt)
                outcome = "" if queued is not None else "not_queued"
    except Exception as exc:  # noqa: BLE001 - said in the topic as what became of it
        gateway.runtime.log(f"butler ask raised job=#{record.get('job_id')} as {stage}: "
                            f"{type(exc).__name__}: {str(exc)[:200]}")
        outcome = stage
    if outcome:
        return settle(gateway, job, ctx, record, path, mine, outcome, label)
    write_json(path, {**mine, "state": "sent", "butler_job": queued}, ensure_ascii=False, indent=2)
    gateway.runtime.log(f"butler ask handed on job=#{record.get('job_id')} topic={topic[-8:]} "
                        f"message={mine['message_id'][-8:]}")


def queue_turn(gateway, base, app, chat, message, text, owner, topic, said, *, prompt="") -> str | None:
    """The butler's turn on ``message`` (the bot's message at the top level), queued behind whatever of the butler's runs:
    its topic.json as the settings say (app/butler.py keep_topic), the message the butler's, its history line, and the job
    with the turn source ``handover``; its model reads ``prompt`` (``butler_prompt``: the owner's words whole), its card and
    history ``text``. The job's message id, None when it was not queued."""
    app = gateway.topics.apps.get(gateway.butler.keep_topic(base, app, chat)) or app
    topic_id = butler.butler_topic_id(chat)
    remember_topic_message(base.state_dir, topic_id, chat, message)
    target = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
    if target.topic_id != topic_id:
        return None
    source = turn_source.make(turn_source.HANDOVER, asserted_by=turn_source.PARTY_OWNER,
                              origin={"topic": topic, "message_id": said, "chat": chat,
                                      **({"question": said.removeprefix(ANSWER)} if said.startswith(ANSWER) else {})})
    append_history(target.history_path, "user", text,
                   {"message_id": message, "chat_id": chat, **({"sender_id": owner} if owner else {})})
    job = app.submission.enqueue_chat_job(target, text, chat, message, prompt_content=prompt or text, sender_id=owner, source=source)
    return None if job is None else str(getattr(job, "message_id", "") or message)


def settle(gateway, job, ctx, record, path, mine, outcome, label) -> None:
    """Not handed on: the record says why, and a line in the topic's thread tells the owner to say it himself."""
    write_json(path, {**mine, "state": outcome}, ensure_ascii=False, indent=2)
    gateway.runtime.log(f"butler ask not handed on job=#{record.get('job_id')}: {outcome}")
    tell(gateway, job, ctx, record, outcome, label)


def tell(gateway, job, ctx, record, outcome, label) -> None:
    jctx = context_for_job(ctx, job)
    gateway.messenger.send_message(jctx.profile, str(record.get("chat_id") or ""), NOT_HANDED[outcome].format(label=label),
                                   gateway.voice.key("butler-ask-" + outcome, record.get("key") or ""), voice=gateway.voice,
                                   reply_to_message_id=jctx.reply_to_message_id)
