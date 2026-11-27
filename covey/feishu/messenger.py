"""The Bridge's outbound Feishu boundary: text messages, card messages, published cards and
CardKit writes. One Messenger per process; each call names the Voice it speaks with."""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import threading
import time
from typing import Any

from covey.core import card_limits
from covey.core import env as cenv
from covey.core.card_sequence import file_card, reserve
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json
from covey.core.topics import remember_topic_message, topic_label, topic_title
from covey.feishu import cardkit, sdk
from covey.feishu.delivery import resolve_message_format, safe_idempotency_key, send_message_with_fallback
from covey.feishu.message_request import message_argv, runner as message_runner
from covey.feishu.transport import (NOT_SENT, SENT, UNCERTAIN, Failure, call, data_of, last_failure, lark_argv, message_id_of,
                                    outcome, reason, run_cli, set_failure)

_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]+")
_PATH_ID = re.compile(r"[A-Za-z0-9_-]+")  # An id put in an API path (a reaction id) as it is.

DEFAULT_MESSAGE_CHUNK_LIMIT = 12000
FALLBACK_MESSAGE_CHUNK_LIMIT = 3000
DEFAULT_MESSAGE_FORMAT = "markdown"
MESSAGE_FORMATS = {"markdown", "text"}


@dataclasses.dataclass(frozen=True)
class Voice:
    """How one Harness's messages look on Feishu: the prefix of every idempotency key it
    sends and the environment that tunes its text (``<env_prefix>_MESSAGE_FORMAT`` and
    ``<env_prefix>_MESSAGE_CHUNK_LIMIT``, read at each send)."""

    key_prefix: str
    env_prefix: str

    def key(self, tag: str, message_id: str) -> str:
        return f"{self.key_prefix}-{tag}-{message_id}"

    def safe_key(self, value: str) -> str:
        return safe_idempotency_key(value, fallback=self.key_prefix)

    def message_format(self) -> str:
        raw = cenv.get(f"{self.env_prefix}_MESSAGE_FORMAT", DEFAULT_MESSAGE_FORMAT)
        return resolve_message_format(raw, MESSAGE_FORMATS)

    def chunk_limit(self) -> int:
        raw = cenv.get(f"{self.env_prefix}_MESSAGE_CHUNK_LIMIT", "").strip()
        try:
            return int(raw) if raw else DEFAULT_MESSAGE_CHUNK_LIMIT
        except ValueError:
            return DEFAULT_MESSAGE_CHUNK_LIMIT


CARDS = "/open-apis/cardkit/v1/cards"


@dataclasses.dataclass(frozen=True)
class CardAbout:
    """What a card is, for the line ``publish_card`` sends in its place when it could not be shown: ``what`` it is (问题卡),
    whose (``topic``, "T12「标题」"; '' reads ctx's topic, ``whose``), where the owner goes on from there (``where``, or a
    function that says it, called only when the line goes; '' says to try again); ``quiet``: no line, the caller says it
    itself (its own words, or a card that follows)."""

    what: str = "卡片"
    topic: str = ""
    where: Any = ""
    quiet: bool = False


TITLE_CHARS = 30  # Of a topic's title in the line.


def whose(ctx) -> str:
    """ctx's topic as the line names it: "T12「标题」", and what of it is not known when part is ("话题「标题」（T 号未知）",
    "话题（T 号和标题都没读到）"); '' for a card of no topic (the bot menu's). Never raises."""
    state, topic = getattr(ctx, "state_dir", None), str(getattr(ctx, "topic_id", "") or "")
    if not topic or state is None:
        return ""
    try:
        label, title = topic_label(state, topic), topic_title(state, topic)
    except (OSError, ValueError, TypeError):
        label, title = "", ""
    title = title[:TITLE_CHARS] + ("…" if len(title) > TITLE_CHARS else "")
    if label:
        return label + (f"「{title}」" if title else "")
    return f"话题「{title}」（T 号未知）" if title else "话题（T 号和标题都没读到）"


def not_shown_text(about, topic, failure, created) -> str:
    """The line that stands for a card that could not be shown: whose card, what went wrong (``created``: CardKit made
    it, the message did not go; transport.py ``reason``) and where to go on (CardAbout)."""
    what = about.what or "卡片"
    head = f"{topic}的{what}" if topic else f"这张{what}"
    went = "建好了但没发出去" if created else "没建成"
    try:
        where = about.where() if callable(about.where) else about.where
    except Exception:  # noqa: BLE001 - the line goes all the same, without where
        where = ""
    where = str(where or "").rstrip("。") or ("稍后再试；急的话直接在话题里说" if topic else "稍后再试")
    return f"⚠️ {head}{went}：{reason(failure)}。{where}。"


def not_updated_text(what, failure, how, where="") -> str:
    """The line ``patch_or_tell`` sends under a card it could not update: which card (``what``), that it surely did not change
    (``how`` NOT_SENT) or may not have, why (transport.py ``reason``) and where to go on (``where``, else to try again)."""
    went = "没更新" if how == NOT_SENT else "可能没更新"
    where = str(where or "").rstrip("。") or "卡上的状态可能是旧的，稍后再点一次或直接在话题里说"
    return f"⚠️ 这张{what or '卡片'}{went}：{reason(failure)}。{where}。"


CHECK_LOG_SECONDS = 60.0  # A card over a target alone is logged at most once a minute for each kind of write.


def unsent_outcome(messenger) -> str:
    """For a send through ``messenger`` that was not confirmed: NOT_SENT when its ``last_outcome`` says it surely did not go
    (the same key may go again), else UNCERTAIN (a stand-in that cannot say may have sent it: never sent again)."""
    told = getattr(messenger, "last_outcome", None)
    return NOT_SENT if callable(told) and told() == NOT_SENT else UNCERTAIN


def card_json(card) -> dict:
    """A card's JSON as CardKit takes it when a card is created or replaced whole."""
    return {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)}


class Messenger:
    """Sends for every Harness of the Bridge. CardKit and card messages go through
    ``covey.feishu.cardkit``, looked up at each call.

    Card writes: ``create_card`` / ``create_and_send_card`` for a new card, ``put_card`` to
    replace one whole, ``write_card`` for one part of it (elements, settings, batch_update).
    Callers reserve each write's ``sequence`` and name its idempotency ``key``; both go out
    exactly as given.

    What became of a send (``send_message``, ``reply``, ``send_card``, ``create_and_send_card``, ``publish_card``,
    ``send_text``, ``send_to_user``, ``send_card_to_user``) is ``last_outcome`` on the thread that sent it, the return values
    staying as they are."""

    def __init__(self, log) -> None:
        self.log = log

    def _outcomes(self):
        return self.__dict__.setdefault("_outcome_local", threading.local())

    def _set_outcome(self, value) -> None:
        self._outcomes().value = value

    def _noted(self, ok):
        """``ok`` as it came, its outcome (feishu/transport.py ``outcome`` of it and the last failure) kept for
        ``last_outcome``."""
        self._set_outcome(outcome(bool(ok), last_failure()))
        return ok

    def last_outcome(self) -> str:
        """What became of this thread's last send through this Messenger: feishu/transport.py SENT, NOT_SENT (surely not
        sent: the same key may go again) or UNCERTAIN (may have gone: never sent again by itself, AGENTS.md rule 1);
        UNCERTAIN before any, and for one that raised."""
        return getattr(self._outcomes(), "value", UNCERTAIN)

    def send_message(self, profile, chat_id, text, key, *, reply_to_message_id="", voice: Voice, card=None,
                     on_sent=None, on_card=None) -> bool:
        """``text`` as Markdown (``voice``'s format); ``card``: the same text as a card, tried first. ``on_sent``
        learns the ID of each message sent, ``on_card`` the card's when the card went (not the Markdown)."""
        self._set_outcome(UNCERTAIN)
        return self._noted(send_message_with_fallback(
            profile=profile,
            chat_id=chat_id,
            text=text,
            idempotency_key=key,
            message_format=voice.message_format(),
            chunk_limit=voice.chunk_limit(),
            fallback_chunk_limit=FALLBACK_MESSAGE_CHUNK_LIMIT,
            log=self.log,
            fallback_key_prefix=voice.key_prefix,
            reply_to_message_id=reply_to_message_id,
            card=card,
            on_sent=on_sent,
            on_card=on_card,
        ))

    def reply(self, ctx, chat_id, text, key, *, voice: Voice, card=None, on_card=None) -> bool:
        """A text message in ctx's topic (the main chat when ctx has no topic); ``card`` and ``on_card`` as
        send_message takes them. Each message sent is learned as the topic's, as its cards are (``remember``)."""
        return self.send_message(ctx.profile, chat_id, text, key, reply_to_message_id=ctx.reply_to_message_id,
                                 voice=voice, on_sent=lambda mid: self.remember(ctx, chat_id, mid),
                                 **({"card": card} if card is not None else {}),
                                 **({"on_card": on_card} if on_card is not None else {}))

    def remember(self, ctx, chat_id, message_id) -> None:
        """``message_id``, sent in ctx's topic, as one of the topic's messages (core/topics.py), so a reply to it or
        the owner's read receipt (app/topic_seen.py) finds the topic. The message is out already: a failure here
        is logged, never the send's."""
        try:
            remember_topic_message(ctx.state_dir, getattr(ctx, "topic_id", ""), chat_id, message_id)
        except (OSError, TypeError, AttributeError) as exc:
            self.log(f"sent message not remembered for its topic ({type(exc).__name__})")

    def _bot(self, profile, *args, yes=False):
        """lark-cli ``args`` as the bot (``yes``: with --yes), JSON out, 30 seconds: its parsed answer, None when
        Feishu refused or the call failed. The argv is the one each caller always sent (the timing log sums by it)."""
        return call(lark_argv(profile, *args, "--as", "bot", *(["--yes"] if yes else []), "--format", "json"),
                    timeout=30, log=self.log)

    def _bot_api(self, profile, request, *args, ids=(), yes=False):
        """``_bot(profile, *args)``, sent in process when the SDK transport is on (feishu/sdk.py) as ``request``
        (method, path, body, params) says: the same answer and failures as lark-cli's, its subprocess (~0.7 s) saved.
        ``ids``: the message id, then any other id, in ``path``; one that does not look like one is lark-cli's to
        refuse."""
        method, path, body, params = request
        argv = lark_argv(profile, *args, "--as", "bot", *(["--yes"] if yes else []), "--format", "json")
        execute = None
        if (sdk.enabled() and ids and _MESSAGE_ID.fullmatch(str(ids[0]))
                and all(_PATH_ID.fullmatch(str(part)) for part in ids[1:])):
            execute = sdk.runner_or_none(profile, method, path, body, params, log=self.log, label="Message",
                                         cli_errors=True)
        return call(argv, timeout=30, log=self.log, runner=execute)

    def recall(self, profile, message_id) -> bool:
        """Recall (撤回) a message the bot sent; False when Feishu refused (past the tenant's recall
        window, already gone) or the call failed."""
        return self._bot_api(profile, ("DELETE", f"/open-apis/im/v1/messages/{message_id}", None, None),
                             "im", "messages", "delete", "--message-id", message_id, ids=(message_id,),
                             yes=True) is not None

    def refused(self, card, *, step, message=False, whole=True) -> bool:
        """Whether a card write must not go (WAVE12 W2-D 包 C): ``card`` (a whole card, or the part a CardKit write carries)
        over what Feishu takes (core/card_limits.py ``check``: more than 200 tagged nodes; a card message over 30 KB). Then
        nothing is sent, the failure is the one Feishu would answer (transport.py ``Failure`` with its code, so ``reason``
        and a caller that reads the code see what they would have) and a log line says by how much. A card within them but
        over a target (195 nodes, 29 KB, a ``whole`` CardKit card's rendered size) is only logged, at most once a minute a
        ``step``. Never raises: a card that cannot be measured goes as before."""
        over = card_limits.check(card, message=message)
        if over is None:
            near = card_limits.over_target(card, message=message, cost=whole)
            if near and self._may_log(step):
                self.log(f"card over its target step={step} {near}")
            return False
        set_failure(Failure("api_rejected", code=over.code, detail=over.words()))
        self.log(f"card refused before sending step={step} code={over.code} nodes={over.nodes}"
                 + (f" bytes={over.bytes}" if over.bytes else "") + f" limit={over.limit}")
        return True

    def _may_log(self, step) -> bool:
        times = self.__dict__.setdefault("_check_logged", {})
        now = time.monotonic()
        if now - times.get(step, -CHECK_LOG_SECONDS) < CHECK_LOG_SECONDS:
            return False
        times[step] = now
        return True

    def patch_card(self, profile, message_id, card) -> bool:
        """Replace the card of a card message the bot sent (an ``update_multi`` card, within 14 days, at most
        30 KB) whole; False when Feishu refused or the call failed, or the card is over its limits (``refused``: not
        sent). Last write wins: no sequence. What became of it is ``last_outcome``."""
        self._set_outcome(UNCERTAIN)
        if self.refused(card, step="patch", message=True):
            self._set_outcome(NOT_SENT)
            return False
        data = {"content": json.dumps(card, ensure_ascii=False)}
        return self._noted(self._bot(profile, "im", "messages", "patch", "--params", json.dumps({"message_id": message_id}),
                                     "--data", json.dumps(data, ensure_ascii=False)) is not None)

    def patch_or_tell(self, profile, message_id, card, *, voice: Voice | None, chat_id="", what="卡片", where="") -> bool:
        """``patch_card``, and when the card did not take: one short line under it (a reply to ``message_id``) saying which
        card did not update and why (``not_updated_text``), so the owner is not left looking at its old state. At most one
        line a card (in this process, and under one key a card, so a retry of it says nothing new), never from inside such
        a line; ``voice`` None: the log line only. Whether the card was replaced; ``last_outcome`` is the card's."""
        if self.patch_card(profile, message_id, card):
            return True
        how, failure = self.last_outcome(), last_failure()
        self.log(f"card not updated what={what} outcome={how} category={getattr(failure, 'category', '-') or '-'} "
                 f"code={getattr(failure, 'code', '') or '-'}")
        local = self._outcomes()
        told = self.__dict__.setdefault("_told", set())
        if voice is None or not message_id or getattr(local, "telling", False) or message_id in told:
            return False
        told.add(message_id)
        local.telling = True
        try:
            self.send_message(profile, chat_id, not_updated_text(what, failure, how, where),
                              voice.safe_key(voice.key("card-not-updated", message_id)), reply_to_message_id=message_id,
                              voice=voice)
        except Exception as exc:  # noqa: BLE001 - the card's outcome is what the caller asks about
            self.log(f"card not updated line not sent ({type(exc).__name__})")
        finally:
            local.telling = False
        self._set_outcome(how)
        set_failure(failure)
        return False

    def pin(self, profile, message_id) -> bool:
        """Pin (Pin 消息) a message in its chat as the bot; pinning one already pinned succeeds too. False when
        Feishu refused or the call failed."""
        return self.pin_info(profile, message_id) is not None

    def pin_info(self, profile, message_id):
        """Pin as ``pin`` does: the pin Feishu answered with (its ``create_time``, epoch milliseconds as a string,
        is the original one when the message was pinned already, a new one when it was not), {} when the answer
        named none; None when Feishu refused or the call failed."""
        result = self._bot(profile, "im", "pins", "create", "--data", json.dumps({"message_id": message_id}))
        if result is None:
            return None
        data = result.get("data", result) if isinstance(result, dict) else {}
        pin = data.get("pin") if isinstance(data, dict) else None
        return pin if isinstance(pin, dict) else {}

    def unpin(self, profile, message_id) -> bool:
        """Take a message off its chat's pins as the bot (one not pinned: succeeds too); False as for pin."""
        return self._bot(profile, "im", "pins", "delete", "--message-id", message_id, yes=True) is not None

    def react(self, profile, message_id, emoji) -> str:
        """Add the reaction ``emoji`` (an emoji_type: OnIt, DONE, Get, CrossMark) to a message as the bot; adding one
        it has already is the same reaction again. Its reaction_id, '' when Feishu refused or the call failed."""
        body = {"reaction_type": {"emoji_type": emoji}}
        result = self._bot_api(profile, ("POST", f"/open-apis/im/v1/messages/{message_id}/reactions", body, None),
                               "im", "reactions", "create", "--params", json.dumps({"message_id": message_id}),
                               "--data", json.dumps(body), ids=(message_id,))
        data = result.get("data", result) if isinstance(result, dict) else {}
        return str(data.get("reaction_id") or "") if isinstance(data, dict) else ""

    def unreact(self, profile, message_id, reaction_id) -> bool:
        """Remove the bot's reaction ``reaction_id`` from a message (one removed already: succeeds too); False when
        the call failed."""
        params = json.dumps({"message_id": message_id, "reaction_id": reaction_id})
        return self._bot_api(profile, ("DELETE", f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
                                       None, None), "im", "reactions", "delete", "--params", params,
                             ids=(message_id, reaction_id)) is not None

    def reaction_ids(self, profile, message_id, emoji) -> list | None:
        """The ids of the reactions ``emoji`` the bot has put on a message (an app's, not a person's); None when the call
        failed (an empty list: there are none). One page of up to 50, which is all one bot's one emoji can be."""
        result = self._bot(profile, "im", "reactions", "list", "--params",
                           json.dumps({"message_id": message_id, "reaction_type": emoji, "page_size": 50}))
        if not isinstance(result, dict):
            return None
        data = result.get("data", result)
        items = data.get("items") if isinstance(data, dict) else None
        if items is None:
            return [] if isinstance(data, dict) else None
        return [str(item.get("reaction_id")) for item in items if isinstance(item, dict) and item.get("reaction_id")
                and (item.get("operator") or {}).get("operator_type") == "app"]

    def send_to_user(self, profile, open_id, text, key):
        """``text`` as a plain text message to the user ``open_id`` under ``key`` (already safe): the owner's first
        private message (app/bot_menu.py). Feishu's answer, None when it was not sent."""
        self._set_outcome(UNCERTAIN)
        payload = call(message_argv(profile, user_id=open_id, flag="--text", value=text, key=key),
                       timeout=30, log=self.log, uuid=key)
        self._noted(payload is not None)
        return payload

    def send_card_to_user(self, profile, open_id, card_id, key):
        """The card ``card_id`` as a message to the user ``open_id`` under ``key`` (already safe), which opens the bot's
        private chat with them if there is none yet (the welcome card, app/welcome.py): (its message ID, the chat's ID
        when Feishu's answer names it), both '' when it was not sent (``last_failure`` says whether it may have)."""
        content = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
        self._set_outcome(UNCERTAIN)
        payload = call(message_argv(profile, user_id=open_id, flag="--content", value=content, key=key),
                       timeout=30, log=self.log, uuid=key)
        data, message_id = data_of(payload), message_id_of(payload)
        self._noted(message_id)
        chat_id = str(data.get("chat_id") or "") if message_id and isinstance(data, dict) else ""
        return message_id, chat_id

    def send_text(self, profile, chat_id, text, key) -> str:
        """``text`` as one plain text message (no Markdown, so it reads back as written) at the top level of ``chat_id``
        under ``key`` (already safe): its message ID, '' when it was not sent (``last_failure`` says whether it may
        have). A welcome card's example (app/welcome.py); the line a person who is not the owner gets in private
        (app/strangers.py)."""
        if text.startswith("@") or text == "-":
            text = "\u200b" + text  # lark-cli reads "@path" as a file and "-" as stdin (feishu/delivery.py).
        argv = message_argv(profile, chat_id=chat_id, flag="--text", value=text, key=key)
        self._set_outcome(UNCERTAIN)
        return self._noted(message_id_of(call(argv, timeout=30, log=self.log, uuid=key,
                                              runner=message_runner(argv, self.log))))

    def reply_image(self, profile, message_id, path):
        """The image file ``path`` as a reply to ``message_id``, run in its folder (lark-cli takes the image by a
        relative path); what it ran. Raises OSError or SubprocessError when it could not run."""
        return run_cli(lark_argv(profile, "im", "+messages-reply", "--as", "bot", "--message-id", message_id,
                                 "--image", path.name), runner=subprocess.run, cwd=str(path.parent),
                       capture_output=True, text=True, timeout=60, check=False)

    def send_image(self, profile, chat_id, path, key, *, voice: Voice, reply_to_message_id="") -> str:
        """The image file ``path`` as an image message under ``voice``'s safe ``key``: in reply to
        ``reply_to_message_id`` in its thread as any reply (feishu/message_request.py message_argv), else to
        ``chat_id``. Run in its folder (lark-cli takes the image by a relative path). Its message ID, '' when it was
        not sent (logged)."""
        argv = message_argv(profile, chat_id=chat_id, reply_to=reply_to_message_id, key=voice.safe_key(key),
                            flag="--image", value=path.name)
        try:
            proc = run_cli(argv, runner=subprocess.run, cwd=str(path.parent), capture_output=True, text=True,
                           timeout=60, check=False)
            payload = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.log(f"image not sent ({type(exc).__name__})")
            return ""
        message_id = message_id_of(payload) if isinstance(payload, dict) else ""
        if not message_id:
            self.log(f"image not sent rc={proc.returncode} {(proc.stderr or '')[-200:].strip()}")
        return message_id

    def cardkit(self, profile, method, path, data, **kwargs):
        return cardkit.cardkit_api(profile, method, path, data, log=self.log, **kwargs)

    def send_card(self, profile, chat_id, card_id, key, *, voice: Voice, **kwargs):
        """A card message under ``voice``'s safe ``key``; ``reply_to_message_id`` as send_card_message takes it."""
        self._set_outcome(UNCERTAIN)
        sent = cardkit.send_card_message(profile, chat_id, card_id, voice.safe_key(key), log=self.log, **kwargs)
        self._noted(sent[0])
        return sent

    def create_card(self, profile, card):
        """Register ``card`` with CardKit; CardKit's response, None when the call failed or the card is over its limits
        (``refused``: not sent)."""
        if self.refused(card, step="create"):
            return None
        return self.cardkit(profile, "POST", CARDS, card_json(card))

    def create_and_send_card(self, profile, chat_id, card, key, *, voice: Voice,
                             card_id="", on_created=None, **kwargs) -> tuple[str, str]:
        """Create ``card`` and send it as a card message (``kwargs`` as send_card takes them):
        (card_id, message_id), each '' when that step did not happen. No card made is NOT_SENT (``last_outcome``): nothing
        can have gone."""
        self._set_outcome(UNCERTAIN)
        if not card_id:
            card_id = cardkit.extract_card_id(self.create_card(profile, card) or {})
            if card_id and on_created:
                on_created(card_id)  # Persist before sending so retries retain the same message content.
        if not card_id:
            self._set_outcome(NOT_SENT)
            return "", ""
        message_id, _ = self.send_card(profile, chat_id, card_id, key, voice=voice, **kwargs)
        return card_id, message_id

    def put_card(self, profile, card_id, card, *, sequence, key, **kwargs):
        """Replace a card's whole JSON; CardKit's response, None when unconfirmed or the card is over its limits
        (``refused``: not sent; its ``sequence`` is left unused, which CardKit takes)."""
        if self.refused(card, step="put"):
            return None
        return self.cardkit(profile, "PUT", f"{CARDS}/{card_id}",
                            {"card": card_json(card), "sequence": sequence, "uuid": key}, **kwargs)

    def write_card(self, profile, method, card_id, part, body, *, sequence, key, **kwargs):
        """One write to ``part`` of a card (``elements``, ``elements/<id>``, ``settings``,
        ``batch_update``): ``body`` plus the write's sequence and key. None, not sent, when the part alone is over a
        card's limits (``refused``)."""
        if self.refused(body, step="write", whole=False):
            return None
        return self.cardkit(profile, method, f"{CARDS}/{card_id}/{part}",
                            {**body, "sequence": sequence, "uuid": key}, **kwargs)

    def publish_card(self, ctx, chat_id, event_id, card, source_message="", *, voice: Voice, update_only=False,
                     on_sent=None, card_id="", on_created=None, about=None):
        """Show ``card``: replace the card ``source_message`` showed in this topic, or send a new one
        (``update_only``: replace or raise). browse-cards/ records each sent card for later updates.
        ``card_id`` / ``on_created`` let a caller persist the prepared card across delivery retries. A new card that
        could not be created or sent gets one line in its place (``not_shown``; ``about``: what the card is)."""
        folder = ctx.state_dir / "browse-cards"
        key = voice.safe_key(source_message or event_id)
        path = folder / (key + ".json")
        if source_message and path.exists():
            record = read_json(path)
            if update_only and (not isinstance(record, dict)
                    or not all(key in record for key in ("topic_id", "chat_id", "card_id", "sequence"))
                    or not isinstance(record["sequence"], int) or not isinstance(record["card_id"], str)
                    or not record["card_id"]):
                raise ValueError("来源卡片记录损坏，请使用本话题最新的任务卡")
            if record["topic_id"] == ctx.topic_id and record["chat_id"] == chat_id:
                with resource_lock("card", record["card_id"]):
                    record = read_json(path)  # Another writer of this card may have moved it on.
                    seq = reserve(file_card(path, record))
                    result = self.put_card(ctx.profile, record["card_id"], card, sequence=seq,
                                           key=voice.key("browse", event_id))
                if result is not None:
                    self._set_outcome(SENT)
                    return True
                if update_only:
                    raise ValueError("卡片更新失败，未创建新卡；请稍后重试")
        if update_only:
            raise ValueError("来源卡片已过期或不属于本话题，请使用本话题最新的任务卡")
        cid, mid = self.create_and_send_card(ctx.profile, chat_id, card, voice.key("browse", event_id), voice=voice,
                                             reply_to_message_id=ctx.reply_to_message_id,
                                             **({"card_id": card_id, "on_created": on_created}
                                                if card_id or on_created else {}))
        how = self.last_outcome()  # The card's: the line ``not_shown`` sends in its place is not what the caller asks about.
        if not cid:
            self.not_shown(ctx, chat_id, event_id, about, created=False, voice=voice)
            self._set_outcome(how)
            return False
        if mid:
            self.remember(ctx, chat_id, mid)
            write_json(folder / (voice.safe_key(mid) + ".json"),
                       {"card_id": cid, "topic_id": ctx.topic_id, "chat_id": chat_id, "sequence": 0})
        if not mid:
            self.not_shown(ctx, chat_id, event_id, about, created=True, voice=voice)
            self._set_outcome(how)
        if mid and on_sent:
            on_sent(mid)
        return bool(mid)

    def not_shown(self, ctx, chat_id, event_id, about, *, created, voice: Voice):
        """A new card that ``publish_card`` could not create (``created`` False) or send: a log line of what it was and
        why, and, unless ``about`` is quiet, one line in its place in the same thread, under the key the fixed line had
        ("browse-error" / "browse-send-error" and ``event_id``), so a later try of the same card says it once."""
        about = about or CardAbout()
        failure = last_failure()  # The failed call's: nothing has called Feishu since.
        self.log(f"card not shown what={about.what} step={'send' if created else 'create'} "
                 f"category={getattr(failure, 'category', '-') or '-'} code={getattr(failure, 'code', '') or '-'}")
        if about.quiet:
            return
        text = not_shown_text(about, about.topic or whose(ctx), failure, created)
        self.reply(ctx, chat_id, text, voice.key("browse-send-error" if created else "browse-error", event_id), voice=voice)
