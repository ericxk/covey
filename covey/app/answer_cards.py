"""The cards a finished turn's answer went out on (cards/answer_card.py): what they say under the answer and keeping
their 产物 up to date.

- ``inputs`` and ``outputs``: the 输入 and 输出 lines of a job (app/answer_delivery.py ``AnswerParts``, app/butler_relay.py).
- The 产物 panel itself is app/artifacts.py answer_files (drive_artifact_panel by ARTIFACTS_ID, folded unless
  something failed).
- The record <state>/answer-cards/<job key>.json lists each card the turn's answer went out on that shows 产物: the
  answer card in the topic (or the small card after an answer sent as Markdown or a file) and its relay at the top
  level: {schema_version, job_key, cards: [{where, message_id, card, status}]}, ``card`` as sent, ``status`` the
  ledger's when its panel was drawn. A 话题区 question's top-level copy showing the asking turn's 产物 is listed
  too (``register_copy``: where COPY, its question's ``token``, no ``card``): it is a CardKit card whose form
  changes, so it is repainted whole from its question record (app/question_display.py paint_mirror), never patched
  here. Removed by the daily maintenance after TEMPORARY_DAYS untouched
  (core/maintenance_rules.py Temporary). The task card then leaves 产物 out (the owner, 2026-11-08: 答案单独发时任务卡不留).
  Its ``outgoing`` is what a saved reply goes out as (app/saved_send.py keep: its key, place and cards before they
  are sent), so a redelivery under that key sends the same card; written for every saved reply sent on its own.
- ``refresh``: once the upload ends (app/artifacts.py), or the owner's 重新上传 on one of them, each card whose panel
  says otherwise is replaced whole (Messenger.patch_card; outside the record's lock, rule 1; last write wins, so
  no sequence). A failed write is a log line: the card keeps 上传中 and its 重新上传."""
from __future__ import annotations

import time
from pathlib import Path

from covey.cards.answer_card import ARTIFACTS_ID, input_parts, output_parts
from covey.cards.elements import replace_element
from covey.cards.review import answer_card, answer_lead, answer_summary, bare_answer_summary
from covey.core.checkpoint import checkpoint_delivery, read_checkpoint, shows_change
from covey.core.locks import resource_lock
from covey.core.metrics import job_took
from covey.core.state import read_json, read_object, write_json
from covey.core.topics import topic_label
from covey.feishu.message_request import optimize_markdown

FOLDER = "answer-cards"
SCHEMA = 1
COPY = "question"  # ``where`` of a 话题区 question's top-level copy (app/question_display.py mirror).


def record_path(state_dir, key) -> Path:
    return Path(state_dir) / FOLDER / f"{key}.json"


def inputs(job, harness, snapshot=None, ctx=None) -> list:
    """输入 of ``job`` run by the Harness ``harness`` (its spec: name, title), always its six parts (V9,
    2026-11-08): the Harness, the provider as the 换配置 card names it, the model's name, the reasoning budget, 脱敏
    and the workspace. No model chosen (the butler runs the Harness's default): the one the turn ran as its
    process ``snapshot`` names it (Claude's, app/launch_models.py shown_model), else 默认模型; no budget chosen, the
    one its context ``ctx`` (core/jobs.py context_for_job) ran with, else 默认."""
    from covey.app.launch_models import shown_model
    from covey.core.providers import menu_name, provider_of

    spec = getattr(harness, "spec", None)
    title = str(getattr(spec, "title", "") or "")
    model = str(getattr(job, "model", "") or "")
    label = str(getattr(job, "model_label", "") or "")
    ran = (snapshot or {}).get("model") if isinstance(snapshot, dict) else None
    state_dir = getattr(ctx, "state_dir", None)
    name = (label.rsplit(" · ", 1)[-1] if label else model.rsplit("/", 1)[-1] if model
            else (shown_model(state_dir, getattr(spec, "name", ""), ran) if ran and state_dir else str(ran or ""))
            or "默认模型")
    provider = provider_of(str(getattr(spec, "name", "") or ""), model)
    source = menu_name(provider, title) if provider is not None else ""
    effort = str(getattr(job, "effort", "") or "") or str(getattr(ctx, "effort", "") or "") or named_effort(model)
    # The card's workspace (app/cards.py card_facts), else the one its context ran in.
    workspace = str(getattr(job, "workspace_label", "") or "") or str(getattr(ctx, "workspace_name", "") or "")
    return input_parts(title, source, name, effort, getattr(job, "redaction", None), workspace)


def named_effort(model) -> str:
    """The budget a preset runs by its name alone (a budget preset run as it is, effort '', app/launch_models.py
    catalog_view: gemini-4-argon_reasoning_high is high), '' for any other model."""
    from covey.core.model_names import PRESET_RE

    named = PRESET_RE.fullmatch(str(model or "").rsplit("/", 1)[-1])
    return str(named["effort"] or "") if named else ""


def took(job, snapshot=None):
    """The run's 用时 (core/metrics.py job_took with no gate: the task card's, the status card's)."""
    return job_took(getattr(job, "timings", None), snapshot)


def outputs(job, snapshot) -> list:
    return output_parts(snapshot, took(job, snapshot))


def _field(job, name) -> str:
    return str(getattr(job, name, "") or (job.get(name) if isinstance(job, dict) else "") or "")


def files_changed(job) -> bool:
    """Whether the Bridge saw ``job``'s turn change files in its workspace: core/diff.py finalize_job_diff writes the
    turn's diff (``diff_json_path``) only when it found a file changed."""
    path = _field(job, "diff_json_path")
    return bool(path) and Path(path).is_file()


def change(job) -> dict:
    """The 改动 of ``job`` for its answer card's fold (cards/answer_card.py change_element): its checkpoint's
    ``delivery`` (core/checkpoint.py checkpoint_delivery, as written) when the turn changed code (``shows_change``:
    a commit, or files changed and a delivery), else {}."""
    path = _field(job, "checkpoint_path")
    found = checkpoint_delivery(read_checkpoint(Path(path))) if path else {}
    return found if found and shows_change(found, files_changed(job)) else {}


ANSWER_HEADING = "用户补充回答"  # How an answer form's turn begins (app/answers.py): 你答, not 你问.


def ask_link(state_dir, chat, topic_id, message_id, profile=None, fetch=None, chat_place=False) -> str:
    """The applink that opens the owner's message ``message_id``: its thread at it (its root when it is one), else
    its place in the chat (app/topic_links.py, the message read once and kept under ``topic_id``); '' when it
    cannot be read. ``chat_place``: its place in the chat first (the butler's own turns, ``ask_url``)."""
    from covey.app.topic_links import message_link, place_of, thread_link

    if not str(message_id or "").startswith("om_"):
        return ""
    if fetch is None:
        from covey.feishu.message_read import fetch_message_detail as fetch
    where = place_of(state_dir, topic_id, message_id, profile, fetch)
    if chat_place and where.get("position"):
        return message_link(chat, where["position"])
    if where.get("thread_id"):
        return thread_link(chat, where["thread_id"], where.get("thread_position") or "-1")
    return message_link(chat, where["position"]) if where.get("position") else ""


def _get(record, name):
    return record.get(name) if isinstance(record, dict) else getattr(record, name, None)


def turn_messages(record) -> list:
    """The messages of ``record``'s turn in its topic's thread, its task card first, then its answer card."""
    progress = _get(record, "progress")
    return [m for m in (_get(record, "card_message_id") or getattr(progress, "message_id", ""),
                        _get(record, "answer_message_id")) if str(m or "").startswith("om_")]


def owner_message(state_dir, record) -> str:
    """The owner's message ``record``'s turn answers: the one it reacts on or started from; for an answer's turn
    ("answer-<token>", no message of its own) the message of the turn that asked, else the question card's
    top-level copy or the card itself. '' when there is none."""
    mid = str(_get(record, "reaction_message") or _get(record, "message_id") or "")
    if mid.startswith("om_"):
        return mid
    token = mid.removeprefix("answer-") if mid.startswith("answer-") else ""
    if not token or "/" in token or token.startswith("."):
        return ""
    from covey.core.jobs import find_job_record

    question = read_object(Path(state_dir) / "questions" / (token + ".json"))
    asking = find_job_record(Path(state_dir), str(question.get("job_key") or "")) if question.get("job_key") else None
    for found in (str((asking or {}).get("reaction_message") or (asking or {}).get("message_id") or ""),
                  str(question.get("mirror_message_id") or ""), str(question.get("question_message_id") or "")):
        if found.startswith("om_"):
            return found
    return ""


def ask_url(state_dir, record, *, topic_id="", at=(), profile=None, fetch=None, log=None) -> str:
    """Where the "你问"/"你答" block of ``record``'s turn opens (the owner, 2026-11-10 16:55: 都能跳转到话题上，除非
    这个任务不是话题任务，是主Agent任务). A topic's turn (``topic_id``: the topic the block speaks for, a relay's or a
    question copy's, else the record's own): that topic's thread, at ``at`` or the turn's own card or answer when they
    are in it (app/topic_links.py topic_thread_link), else the root's place in the chat. The butler's own turn:
    the owner's message (``owner_message``) at its place in the chat. A fallback and a missing link are each one log
    line (``log``); '' only when nothing could be read."""
    from covey.app.topic_links import topic_thread_link
    from covey.core.butler import is_butler_topic

    chat = str(_get(record, "chat_id") or "")
    topic = str(topic_id or _get(record, "topic_id") or "")
    say = log or (lambda line: None)
    job = f"#{_get(record, 'job_id') or '?'}"
    if topic and not is_butler_topic(topic, chat):
        url, how = topic_thread_link(state_dir, topic, at=[*at, *turn_messages(record)], profile=profile, fetch=fetch)
        if how == "chat":
            say(f"ask link fell back to the chat job={job} topic={topic[:12]}: its root's thread could not be read")
        elif not url:
            say(f"ask link missing job={job} topic={topic[:12]}: neither its thread nor its root could be read")
        return url
    message = owner_message(state_dir, record)
    url = ask_link(state_dir, chat, topic, message, profile, fetch, chat_place=True) if message else ""
    if not url:
        say(f"ask link missing job={job} main thread message={message[-10:] or '-'}: its place could not be read")
    return url


def asked(state_dir, record, *, topic_id="", at=(), profile=None, fetch=None, log=None):
    """The "你问" block (cards/answer_card.py ask_block) for the job ``record`` (a job record or a job): the owner's
    words that started it (``asked_text`` first: the words he added to a turn that waited for them, app/steering.py
    merge_before_start), when, and the link ``ask_url`` gives (``topic_id``, ``at``, ``log`` as it takes them). An
    answer's turn says "你答" and "question → answer" as its task card's title, the same block with the same link;
    with no link at all it folds out the whole answer instead (answer_block, the question record's words split it,
    ``asked_questions``), so the words are still there. None without words."""
    from covey.core.turn_words import request_line
    from covey.cards.answer_card import ask_block
    from covey.core.state import epoch_seconds

    def get(name):
        return _get(record, name)
    from covey.core import projects

    if projects.own_message(str(get("message_id") or "")):
        return None  # A project's lead woken by the Bridge or handed the butler's words: the owner asked nothing here.
    text = str(get("user_text") or "")
    answer = text.lstrip().startswith(ANSWER_HEADING)
    words = " ".join(str(get("asked_text") or "").split())
    if answer and not words:
        # What was asked and answered, as its task card's title (app/answers.py answer_title, the job's ``title``):
        # the first line is the question's (ACCEPT-330 read "你答 / 验收：确认结束吗？" without its 确认).
        from covey.app.answers import shown_answer_title

        words = " ".join(str(get("title") or "").split()) or shown_answer_title(text)
    words = words or request_line(text)
    at_time = epoch_seconds(get("created_at")) or 0
    if not at_time:  # A job, not its record: when it was taken in.
        timings = get("timings") or {}
        at_time = next((timings[stage] for stage in ("received", "queued", "dispatched", "backend_started")
                        if isinstance(timings.get(stage), (int, float))), 0)
    when = time.strftime("%H:%M", time.localtime(at_time)) if at_time else ""
    try:
        url = ask_url(state_dir, record, topic_id=topic_id, at=at, profile=profile, fetch=fetch, log=log)
    except Exception as exc:  # noqa: BLE001 - the block without its link, said
        if log is not None:
            log(f"ask link failed job=#{get('job_id') or '?'}: {type(exc).__name__}")
        url = ""
    if answer and not url:  # Nowhere to open: the block folds out the whole answer (answer_block).
        from covey.app.answers import answer_parts
        from covey.cards.answer_card import answer_block

        parts = answer_parts(text, asked_questions(state_dir, str(get("message_id") or "")))
        added = str(get("asked_text") or "").strip()
        return answer_block([*parts, *([("heading", "另外说的"), ("note", added)] if added else [])], words, at=when)
    return ask_block(words, verb="你答" if answer else "你问", at=when, url=url)


def asked_questions(state_dir, message_id) -> list:
    """The questions' words of the question record an answer's turn ``message_id`` ("answer-<token>",
    app/questions.py _queue_continuation) answered, in order; [] when it cannot be read."""
    from covey.core.state import read_object

    token = message_id.removeprefix("answer-") if message_id.startswith("answer-") else ""
    if not token or "/" in token or token.startswith("."):
        return []
    try:
        questions = read_object(Path(state_dir) / "questions" / (token + ".json")).get("questions")
    except Exception:  # noqa: BLE001 - read back from the words alone
        return []
    return [str(q.get("question") or "") for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []


def ledger_of(state_dir, record, voice) -> dict | None:
    path = Path(state_dir) / "artifact-drive" / (voice.safe_key(record["key"]) + ".json")
    return read_json(path) if path.exists() else None


def register(state_dir, key, where, message_id, built, status=None):
    """A card ``built`` (with a 产物 panel drawn at the ledger's ``status``) sent as ``message_id``."""
    if not message_id or not built:
        return
    path = record_path(state_dir, key)
    with resource_lock("answer-cards", str(path)):
        saved = read_object(path)
        cards = [entry for entry in saved.get("cards") or [] if entry.get("message_id") != message_id]
        cards.append({"where": where, "message_id": message_id, "card": built, "status": status})
        write_json(path, {**saved, "schema_version": SCHEMA, "job_key": key, "cards": cards}, ensure_ascii=False)


def register_copy(state_dir, key, message_id, token, status=None):
    """The top-level copy ``message_id`` of the question ``token``, drawn with the job ``key``'s 产物 at the
    ledger's ``status``: a 重新上传 there takes the answer cards' path (``clicked``), and ``copies`` finds it."""
    if not message_id or not token:
        return
    path = record_path(state_dir, key)
    with resource_lock("answer-cards", str(path)):
        saved = read_object(path)
        listed = {"where": COPY, "message_id": message_id, "token": token, "status": status}
        if listed in (saved.get("cards") or []):
            return  # Listed as it is (every repaint of the copy comes here): nothing to write.
        cards = [entry for entry in saved.get("cards") or [] if entry.get("message_id") != message_id]
        cards.append(listed)
        write_json(path, {**saved, "schema_version": SCHEMA, "job_key": key, "cards": cards}, ensure_ascii=False)


def copies(state_dir, key, status, *, force=False) -> list:
    """The question tokens of the job ``key``'s top-level copies whose 产物 say other than ``status`` (``force``:
    every one)."""
    return [str(entry["token"]) for entry in read_object(record_path(state_dir, key)).get("cards") or ()
            if entry.get("where") == COPY and entry.get("token") and (force or entry.get("status") != status)]


def holds_files(state_dir, key) -> bool:
    """Whether an answer card of the job ``key`` shows its 产物 (the task card then does not)."""
    return any(entry.get("where") in ("answer", "tail") for entry in read_object(record_path(state_dir, key)).get("cards") or ())


def refresh(state_dir, profile, messenger, key, panel, status, log, *, force=False):
    """Put ``panel``, the 产物 panel of the job ``key`` as its upload's ledger has it (``status``), on each of its
    answer cards that shows another (``force``: on every one, after a 重新上传 click); the cards written."""
    path = record_path(state_dir, key)
    if panel is None:
        return 0
    with resource_lock("answer-cards", str(path)):
        entries = [dict(entry) for entry in read_object(path).get("cards") or []]
    written = 0
    for entry in entries:
        if entry.get("where") == COPY or (not force and entry.get("status") == status):
            continue  # A question's copy is repainted by its question (``copies``).
        built = entry.get("card")
        if not isinstance(built, dict) or not replace_element(built, ARTIFACTS_ID, panel):
            continue
        if messenger.patch_card(profile, entry["message_id"], built):
            written += 1
            with resource_lock("answer-cards", str(path)):
                saved = read_object(path)
                for kept in saved.get("cards") or []:
                    if kept.get("message_id") == entry["message_id"]:
                        kept.update(card=built, status=status)
                write_json(path, saved, ensure_ascii=False)
        else:
            log(f"answer card 产物 not updated job={key[-10:]} where={entry.get('where')}")
    return written


def clicked(state_dir, key, message_id) -> bool:
    """Whether ``message_id`` is one of the job ``key``'s answer cards or a question's top-level copy showing its
    产物 (a 重新上传 there redraws them, not the task card)."""
    return any(entry.get("message_id") == message_id for entry in read_object(record_path(state_dir, key)).get("cards") or ())


def saved_answer_card(ctx, job_id, status, reply, text, tail=(), head=(), bare=False):
    """The card a saved reply goes out as (Messenger.send_message's ``card``): ``text``, the reply as it is
    delivered, in lark-cli's Markdown style but with its headings' levels kept (``answer_card`` bolds the
    small ones), and the chat list's preview "✅ T304 · lead" (``answer_summary``; the task card's names the time). Made
    of the saved reply, the topic and the turn's status alone, so each retry sends the same card under the
    same key. ``bare``: a butler turn that sent no card (app/quiet_delivery.py QuietDelivery.bare), previewed without its task number
    (cards/review.py bare_answer_summary)."""
    lead = answer_lead(reply.text)
    label = topic_label(ctx.state_dir, ctx.topic_id) or f"#{job_id}"
    return answer_card(optimize_markdown(text, step_headings=False),
                       bare_answer_summary(status, lead) if bare else answer_summary(label, status, lead), tail, head)
