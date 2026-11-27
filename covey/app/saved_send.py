"""A turn's saved reply, paused question or canonical text sent the same way on its first send (app/worker.py
TurnDelivery, its answer card app/answer_delivery.py AnswerParts) and on each redelivery (app/redelivery.py: 检查并继续,
the outbox's retries): one send key, one card, only the saved words (rule 5: the model does not run).

- ``Outgoing``: what a saved reply goes out as: its send key (core/saved_results.py saved_delivery_key), at the top
  level or where its context replies (the placement table, core/placement.py ``answer`` TOP), a message or a file,
  the answer card (None for a file), the cards of an answer too large for one (``pages``, cards/pages.py: each with
  the card's row, the first one ``answer``) and the small card after a file or after cards that could not hold the
  answer's 产物 and 输入/输出 too (``tail``), whether they show 产物 and at which upload status. ``plan`` makes it
  from the parts the first send draws (``tail_parts``, ``status_head``, ``owner_headed``; the "你问" block
  app/answer_cards.py asked); ``rebuilt`` draws the same parts from the job record when no first send got as far as
  planning it (a task runner that died before its delivery), as Markdown again (``paged`` False) when a release before
  ``pages`` sent it so.
- ``keep`` and ``kept``: the first send writes its Outgoing into the turn's answer-cards record (app/answer_cards.py,
  its ``outgoing``) before it sends, the record's lock let go first (rule 1); a redelivery under the same key sends
  that again byte for byte: a key is never sent with other content (feishu/transport.py fails it closed as
  ``idempotency_conflict``), so its 产物 are brought up to the upload after it is out (app/answer_cards.py refresh).
  The record has no integrity of its own: an Outgoing carries the ``digest`` of itself with the authorized text it
  was drawn with (``digest_of``), and ``kept`` gives back only one whose digest the reply authorized now gives (rule
  5); another, or one of a release before the digest, is drawn again (``rebuilt``).
- ``send_saved`` sends an Outgoing; ``send_paused`` a paused question's words; ``restore_original`` the canonical
  text once a question form failed and the shown text left the question out."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from covey.app import answer_cards, review
from covey.app.access import Access
from covey.app.artifacts import answer_files
from covey.cards.answer_card import tail as answer_tail, topic_head
from covey.cards.answer_card import tail_card as answer_tail_card
from covey.cards.pages import fits
from covey.cards.ui import banded
from covey.core import placement
from covey.core.jobs import job_key
from covey.core.locks import resource_lock
from covey.core.saved_results import FILE_BYTES
from covey.core.state import read_object, write_json
from covey.core.topics import topic_label, topic_title
from covey.core.trace_store import load_trace

OUTGOING = "outgoing"  # The answer-cards record's field ``keep`` writes.
TAIL_LEAD = "上面是这一轮的回答，产物和详情在这里"
TOP_FILE_LEAD = "回答较长，作为文件发在上面；产物和详情在这里"


@dataclasses.dataclass(frozen=True)
class Outgoing:
    key: str
    top: bool = False
    file: bool = False
    answer: dict | None = None
    tail: dict | None = None
    files: bool = False
    status: str | None = None
    digest: str = ""  # digest_of this Outgoing and the authorized text it carries; "" from an older release.
    # An answer too large for one card: each card it goes on, {"card", "text"} (its part, the Markdown sent when the card
    # is refused), ``answer`` the first's; () for one card (and from a release before them: their digest is the same).
    pages: tuple = ()


def digest_of(outgoing, text) -> str:
    """The sha256 of ``outgoing`` (its ``digest`` left out) with ``text``, the authorized reply as it is delivered
    (core/saved_results.py display_saved_reply): the same once its record went to JSON and back."""
    fields = {name: value for name, value in dataclasses.asdict(outgoing).items()
              if name != "digest" and not (name == "pages" and not value)}  # One card: as a release before pages.
    body = json.dumps({**fields, "text": text}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def signed(outgoing, text) -> Outgoing:
    return dataclasses.replace(outgoing, digest=digest_of(outgoing, text))


def label_of(ctx, job_id) -> str:
    return topic_label(ctx.state_dir, ctx.topic_id) or f"#{job_id}"


def plan(ctx, job_id, status, reply, text, *, key, file, top, tail, head, bare, files, upload_status, chat_id="",
         log=None, paged=True) -> Outgoing:
    """The Outgoing of the saved ``reply`` (``text`` as it is delivered) of task #job_id ended ``status``: a message
    with its answer card (cards/review.py answer_card via app/answer_cards.py saved_answer_card, ``tail`` under the
    answer and ``head`` atop it), or a file with a small card after it carrying ``head`` (the owner's words that open
    too: the file has no row) and ``tail`` when there is a ``tail``. An answer too large for one card goes on several
    (cards/pages.py answer_pages: ``head``'s row on each), ``tail`` under the last or on the small card after them; never
    as Markdown with no row, never without ``tail``. The cards' T numbers of the topics of ``chat_id`` are links to them
    (``linked``), each card measured with them in; the text is as it was authorized. ``paged`` False: a release before
    pages sent this reply as Markdown (app/redelivery.py), so again (the same keys never go with other content, rule 6),
    its ``tail`` on the small card after it."""
    head, tail = list(head), list(tail)
    label = label_of(ctx, job_id)
    if file:
        small = (banded(answer_tail_card([*head, *tail], f"{label} · 产物与详情", TOP_FILE_LEAD if top else TAIL_LEAD,
                                         words=True)) if tail else None)
        return signed(Outgoing(key, top, True, None, small, files, upload_status), text)
    built = answer_cards.saved_answer_card(ctx, job_id, status, reply, text, tail, head, bare=bare)
    built = linked(ctx, chat_id, built)
    if fits(built):
        return signed(Outgoing(key, top, False, built, None, files, upload_status), text)
    if not paged:
        small = banded(answer_tail_card([*head, *tail], f"{label} · 产物与详情", TAIL_LEAD, words=True)) if tail else None
        return signed(Outgoing(key, top, False, None, small, files, upload_status), text)
    cut = answer_cards.saved_answer_pages(ctx, job_id, status, reply, text, tail, head, bare=bare,
                                          link=lambda words: link_text(ctx, chat_id, words),
                                          finish=lambda page: linked(ctx, chat_id, page))
    small = banded(answer_tail_card([*head, *tail], f"{label} · 产物与详情", TAIL_LEAD)) if cut.alone and tail else None
    first = cut.pages[0].card
    if len(cut.pages) == 1:  # The answer fits one card without what follows it: that on the small card.
        return signed(Outgoing(key, top, False, first, small, files, upload_status), text)
    pages = tuple({"card": page.card, "text": page.text} for page in cut.pages)
    return signed(Outgoing(key, top, False, first, small, files, upload_status, pages=pages), text)


def linked(ctx, chat_id, built):
    """``built`` with each T number in its Markdown a link to that topic (app/relay_send.py linked, app/topic_links.py:
    the owner taps "T44" to open it), its own topic's left plain; as it was when no link can be told (the owner,
    2026-11-22: 说话提到 T638 的时候能直接点进去). Never raises; it reads messages, never under a record's lock."""
    if not isinstance(built, dict) or not chat_id:
        return built
    try:
        from covey.app.relay_send import linked as link_card

        own = str(getattr(ctx, "topic_id", "") or "")
        return link_card(ctx.state_dir, chat_id, built, "", profile=getattr(ctx, "profile", None), skip=(own,) if own else ())[0]
    except Exception:  # noqa: BLE001 - the card as it is
        return built


def link_text(ctx, chat_id, text):
    """``text`` (card Markdown) with its T numbers links, as ``linked`` makes them in a card; as it was when none can be
    told. Never raises."""
    if not chat_id:
        return text
    try:
        from covey.app.relay_send import linked as link_card

        own = str(getattr(ctx, "topic_id", "") or "")
        return link_card(ctx.state_dir, chat_id, None, text, profile=getattr(ctx, "profile", None), skip=(own,) if own else ())[1]
    except Exception:  # noqa: BLE001 - the text as it is
        return text


def keep(state_dir, key, outgoing, log) -> None:
    """``outgoing`` as the job ``key``'s answer-cards record's ``outgoing`` (no request under its lock). A failure is a
    log line: the reply still goes, a redelivery then draws it again (``rebuilt``)."""
    path = answer_cards.record_path(state_dir, key)
    try:
        with resource_lock("answer-cards", str(path)):
            saved = read_object(path)
            write_json(path, {"schema_version": answer_cards.SCHEMA, "job_key": key, "cards": [], **saved,
                              OUTGOING: dataclasses.asdict(outgoing)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - the reply goes all the same
        log(f"answer card plan not kept: {type(exc).__name__}")


def kept(state_dir, key, send_key, text) -> Outgoing | None:
    """The Outgoing the first send of the job ``key`` kept for the send key ``send_key`` and ``text``, the reply
    authorized now as it is delivered; None without one, or when its digest is not theirs (changed since, or kept
    by a release before the digest)."""
    try:
        found = read_object(answer_cards.record_path(state_dir, key)).get(OUTGOING)
    except Exception:  # noqa: BLE001 - drawn again
        return None
    if not isinstance(found, dict) or found.get("key") != send_key:
        return None
    names = {field.name for field in dataclasses.fields(Outgoing)}
    try:
        outgoing = Outgoing(**{name: value for name, value in found.items() if name in names})
    except TypeError:
        return None
    if (outgoing.answer is None) != outgoing.file or not all(card is None or isinstance(card, dict)
                                                           for card in (outgoing.answer, outgoing.tail)):
        return None
    pages = outgoing.pages
    if pages and (not isinstance(pages, (list, tuple)) or not all(
            isinstance(page, dict) and isinstance(page.get("card"), dict) and isinstance(page.get("text"), str)
            for page in pages) or pages[0]["card"] != outgoing.answer):
        return None
    if not isinstance(outgoing.digest, str) or not outgoing.digest or outgoing.digest != digest_of(outgoing, text):
        return None
    return outgoing


def send_saved(ctx, job, text, outgoing, *, send, artifacts, log):
    """Send ``outgoing`` with ``text``, the saved reply as it is delivered, for ``job`` in ``ctx`` (the top level when
    ``outgoing.top``). ``send(ctx, chat_id, text, key, card=None, on_card=None)`` sends a message (Messenger.reply in
    the Harness's voice). The cards showing 产物 are listed for the upload (app/answer_cards.py register); of an answer
    on several cards (``pages``), the one carrying what follows it, for its memory lines too (put_memory).
    (delivered, the answer card's message id or '': the first of several.)"""
    where = dataclasses.replace(ctx, reply_to_message_id="") if outgoing.top else ctx
    key = job_key(job.message_id, job.job_id)
    message = ""
    # The card that carries what follows the answer is listed: for the upload when it shows 产物, and for the memory
    # lines whenever the answer went on more than one card (app/answer_cards.py put_memory then takes no other).
    apart = outgoing.tail is not None and not outgoing.file  # Cards that could not hold what follows the answer too.
    listed = outgoing.files or apart or bool(outgoing.pages)
    if outgoing.file:
        delivered = artifacts.send_result_file(where, job, text)
    elif outgoing.pages:
        delivered, sent = send_pages(where, job.chat_id, outgoing, send)
        message = sent[0] if sent else ""
        if delivered and not apart and sent and sent[-1]:
            answer_cards.register(ctx.state_dir, key, "answer", sent[-1], outgoing.pages[-1]["card"], outgoing.status)
    else:
        sent = []
        delivered = send(where, job.chat_id, text, outgoing.key, card=outgoing.answer, on_card=sent.append)
        message = sent[0] if sent else ""
        if sent and outgoing.files and not apart:
            answer_cards.register(ctx.state_dir, key, "answer", sent[0], outgoing.answer, outgoing.status)
    if delivered and outgoing.tail is not None:
        # A file, or cards that could not hold them too: its 产物 and 输入/输出 follow on a small card.
        label = label_of(where, job.job_id)
        sent = []
        try:
            send(where, job.chat_id, f"{label} 这一轮的产物和详情", f"{outgoing.key}-tail", card=outgoing.tail,
                 **({"on_card": sent.append} if listed else {}))
        except Exception as exc:  # noqa: BLE001 - the answer is out; these are extras
            log(f"answer tail card failed: {type(exc).__name__}")
        if sent and listed:
            answer_cards.register(ctx.state_dir, key, "tail", sent[0], outgoing.tail, outgoing.status)
    return delivered, message


def send_pages(where, chat_id, outgoing, send):
    """The cards of an answer too large for one (``outgoing.pages``) in order, each a message of its own: the first under
    the Outgoing's key (as one card would go), the n-th under that key -p<n>, each its part of the answer as Markdown when
    its card is refused (feishu/delivery.py). (whether every one went, the message id of each card: '' for a part
    that went as Markdown.)"""
    ids = []
    for n, page in enumerate(outgoing.pages, 1):
        got = []
        if not send(where, chat_id, page["text"], outgoing.key if n == 1 else f"{outgoing.key}-p{n}",
                    card=page["card"], on_card=got.append):
            return False, ids
        ids.append(got[0] if got else "")
    return True, ids


def tail_parts(state_dir, record, topic_id, voice, job, harness, snapshot, ctx):
    """(elements, whether they show 产物, the upload's status they show) under an answer sent on its own
    (cards/answer_card.py tail): the 产物 of the job ``record`` as the upload stands now and the 输入/输出 box of
    ``job`` run by ``harness`` (its process ``snapshot``)."""
    panel = answer_files(state_dir, record, topic_id, voice) if record else None
    ledger = answer_cards.ledger_of(state_dir, record, voice) if panel else None
    elements = answer_tail(panel, answer_cards.inputs(job, harness, snapshot, ctx),
                           answer_cards.outputs(job, snapshot), change=answer_cards.change(job, state_dir),
                           notes=answer_cards.notes(state_dir, job), buttons=review.buttons(job))
    return elements, panel is not None, (ledger or {}).get("status")


def owner_headed(row, runtime, chat_id) -> bool:
    """Whether a turn's answer card in its thread leads with its status line (``row.answer_head``, D3): only in the
    owner's private chat (app/access.py owner_chat), and in the other chats with a butler (Access.agent_chat:
    the personal groups, each owner account's private chat: the board's Pin shows answers without it); as before
    when that chat cannot be told."""
    if not row.answer_head:
        return False
    chat_id = str(chat_id or "")
    try:
        chat = runtime.access.owner_chat()
        agent = isinstance(runtime.access, Access) and bool(chat_id) and runtime.access.agent_chat(chat_id)
    except Exception as exc:  # noqa: BLE001 - the cards as before
        runtime.log(f"owner chat unavailable: {type(exc).__name__}")
        return False
    return bool(agent) or isinstance(chat, str) and bool(chat) and chat == chat_id


def status_head(state_dir, topic_id, status):
    """What leads an answer card in its thread: the topic's row, as its relay has it (docs/ui.md 5.2 聊天结果卡,
    cards/answer_card.py topic_head: "[T43] **改看板** [有结果]"), no conclusion band over the answer any more (it said the
    reply's first line again, large: the owner, 2026-11-22: 话题里的卡片和私聊区对齐)."""
    return topic_head(topic_label(state_dir, topic_id), topic_title(state_dir, topic_id), status)


def heads(top, headed, ask, status):
    """The blocks atop an answer card: the "你问" block (``ask()``) at the top level, the status line (``status()``)
    in a headed thread, else none; a part that cannot be made is left out."""
    block = ask() if top else status() if headed else None
    return [block] if block else []


def record_job(record, saved_job, *, harness, cards, ctx, log):
    """``saved_job`` (core/jobs.py saved_job) with what its answer card's 输入/输出 read, from the job ``record``: as
    app/cards.py card_facts set them on the job that ran."""
    for name in ("model", "effort", "diff_json_path", "checkpoint_path", "user_text"):
        if record.get(name) and not getattr(saved_job, name, ""):
            setattr(saved_job, name, record[name])
    if isinstance(record.get("timings"), dict):
        saved_job.timings = dict(record["timings"])
    saved_job.workspace_label = str(record.get("workspace_name") or "")
    try:
        saved_job.model_label = cards.model_label(ctx, saved_job.model) if saved_job.model else ""
    except Exception as exc:  # noqa: BLE001 - named from the model itself
        log(f"model label unavailable: {type(exc).__name__}")
    try:
        from covey.core.harness import backend_capabilities
        from covey.core.redaction import topic_redaction

        redacts = backend_capabilities(getattr(harness, "backend", None), ctx).redaction
        saved_job.redaction = topic_redaction(ctx.state_dir, saved_job.topic_id) if redacts else None
    except Exception as exc:  # noqa: BLE001 - 脱敏 left out
        log(f"redaction unavailable: {type(exc).__name__}")
    return saved_job


def rebuilt(ctx, record, saved_job, reply, text, *, key, file, bare, harness, cards, runtime, voice,
            paged=True) -> Outgoing:
    """The Outgoing of the job ``record``'s saved reply when its first send kept none: its parts drawn from the record
    as the first send draws them from the job (``saved_job`` filled by ``record_job``; the 输出 line from the saved
    process record, core/trace_store.py). ``paged`` as ``plan`` takes it."""
    state_dir, log = ctx.state_dir, runtime.log
    row = placement.plan(state_dir, record).row
    top = row.answer == placement.TOP
    job = record_job(record, saved_job, harness=harness, cards=cards, ctx=ctx, log=log)
    try:
        snapshot = load_trace(Path(state_dir), record)
        tail, files, status = tail_parts(state_dir, record, ctx.topic_id, voice, job, harness, snapshot, ctx)
    except Exception as exc:  # noqa: BLE001 - the answer goes all the same
        log(f"answer card tail unavailable: {type(exc).__name__}: {exc}")
        tail, files, status = [], False, None

    def ask():
        try:
            return answer_cards.asked(state_dir, record, profile=ctx.profile, log=log)
        except Exception as exc:  # noqa: BLE001 - the answer goes without it
            log(f"answer card 你问 unavailable: {type(exc).__name__}")
            return None

    def status_line():
        try:
            return status_head(state_dir, ctx.topic_id, record.get("execution_status"))
        except Exception as exc:  # noqa: BLE001 - the answer goes without it
            log(f"answer card status line unavailable: {type(exc).__name__}")
            return None
    headed = not top and owner_headed(row, runtime, record.get("chat_id"))
    head = heads(top, headed, ask, status_line)
    return plan(ctx, record["job_id"], record.get("execution_status"), reply, text, key=key, file=file, top=top,
                tail=tail, head=head, bare=bare, files=files, upload_status=status, chat_id=str(record.get("chat_id") or ""),
                log=log, paged=paged)


def send_paused(ctx, where, job, text, key, *, send, artifacts):
    """A paused question's words for ``job``: a file in ``ctx`` when longer than FILE_BYTES, else a message in
    ``where`` under ``key`` (core/saved_results.py paused_question_delivery_key). Whether it was sent."""
    if len(text.encode("utf-8")) > FILE_BYTES:
        return artifacts.send_result_file(ctx, job, text)
    return send(where, job.chat_id, text, key)


def restore_original(ctx, job, text, file_text, *, key, send, artifacts, log, paint=None) -> bool:
    """A question form failed and the shown text left out its question: ``text``, the canonical reply, again, on the
    task card (``paint()``, when it has one, whether it took it), else as a message under ``key``, else as a file
    (``file_text``). Whether it is out; never raises."""
    try:
        restored = bool(paint()) if paint is not None else False
        if not restored:
            restored = bool(send(ctx, job.chat_id, text, key))
        if not restored:
            restored = bool(artifacts.send_result_file(ctx, job, file_text))
        return restored
    except Exception as exc:  # noqa: BLE001 - the caller records it as not restored
        log(f"original result fallback failed: {type(exc).__name__}: {exc}")
        return False
