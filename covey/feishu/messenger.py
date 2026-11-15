"""The Bridge's outbound Feishu boundary: text messages, card messages, published cards and
CardKit writes. One Messenger per process; each call names the Voice it speaks with."""
from __future__ import annotations

import dataclasses
import json
import subprocess

from covey.core import env as cenv
from covey.core.card_sequence import file_card, reserve
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json
from covey.core.topics import remember_topic_message
from covey.feishu import cardkit
from covey.feishu.delivery import resolve_message_format, safe_idempotency_key, send_message_with_fallback
from covey.feishu.message_request import message_argv
from covey.feishu.transport import call, lark_argv, message_id_of, run_cli

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


def card_json(card) -> dict:
    """A card's JSON as CardKit takes it when a card is created or replaced whole."""
    return {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)}


class Messenger:
    """Sends for every Harness of the Bridge. CardKit and card messages go through
    ``covey.feishu.cardkit``, looked up at each call.

    Card writes: ``create_card`` / ``create_and_send_card`` for a new card, ``put_card`` to
    replace one whole, ``write_card`` for one part of it (elements, settings, batch_update).
    Callers reserve each write's ``sequence`` and name its idempotency ``key``; both go out
    exactly as given."""

    def __init__(self, log) -> None:
        self.log = log

    def send_message(self, profile, chat_id, text, key, *, reply_to_message_id="", voice: Voice, card=None,
                     on_sent=None, on_card=None) -> bool:
        """``text`` as Markdown (``voice``'s format); ``card``: the same text as a card, tried first. ``on_sent``
        learns the ID of each message sent, ``on_card`` the card's when the card went (not the Markdown)."""
        return send_message_with_fallback(
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
        )

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

    def recall(self, profile, message_id) -> bool:
        """Recall (撤回) a message the bot sent; False when Feishu refused (past the tenant's recall
        window, already gone) or the call failed."""
        return self._bot(profile, "im", "messages", "delete", "--message-id", message_id, yes=True) is not None

    def patch_card(self, profile, message_id, card) -> bool:
        """Replace the card of a card message the bot sent (an ``update_multi`` card, within 14 days, at most
        30 KB) whole; False when Feishu refused or the call failed. Last write wins: no sequence."""
        data = {"content": json.dumps(card, ensure_ascii=False)}
        return self._bot(profile, "im", "messages", "patch", "--params", json.dumps({"message_id": message_id}),
                         "--data", json.dumps(data, ensure_ascii=False)) is not None

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
        result = self._bot(profile, "im", "reactions", "create", "--params", json.dumps({"message_id": message_id}),
                           "--data", json.dumps({"reaction_type": {"emoji_type": emoji}}))
        data = result.get("data", result) if isinstance(result, dict) else {}
        return str(data.get("reaction_id") or "") if isinstance(data, dict) else ""

    def unreact(self, profile, message_id, reaction_id) -> bool:
        """Remove the bot's reaction ``reaction_id`` from a message (one removed already: succeeds too); False when
        the call failed."""
        params = json.dumps({"message_id": message_id, "reaction_id": reaction_id})
        return self._bot(profile, "im", "reactions", "delete", "--params", params) is not None

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
        return call(message_argv(profile, user_id=open_id, flag="--text", value=text, key=key),
                    timeout=30, log=self.log, uuid=key)

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
        return cardkit.send_card_message(profile, chat_id, card_id, voice.safe_key(key), log=self.log, **kwargs)

    def create_card(self, profile, card):
        """Register ``card`` with CardKit; CardKit's response, None when the call failed."""
        return self.cardkit(profile, "POST", CARDS, card_json(card))

    def create_and_send_card(self, profile, chat_id, card, key, *, voice: Voice,
                             card_id="", on_created=None, **kwargs) -> tuple[str, str]:
        """Create ``card`` and send it as a card message (``kwargs`` as send_card takes them):
        (card_id, message_id), each '' when that step did not happen."""
        if not card_id:
            card_id = cardkit.extract_card_id(self.create_card(profile, card) or {})
            if card_id and on_created:
                on_created(card_id)  # Persist before sending so retries retain the same message content.
        if not card_id:
            return "", ""
        message_id, _ = self.send_card(profile, chat_id, card_id, key, voice=voice, **kwargs)
        return card_id, message_id

    def put_card(self, profile, card_id, card, *, sequence, key, **kwargs):
        """Replace a card's whole JSON; CardKit's response, None when unconfirmed."""
        return self.cardkit(profile, "PUT", f"{CARDS}/{card_id}",
                            {"card": card_json(card), "sequence": sequence, "uuid": key}, **kwargs)

    def write_card(self, profile, method, card_id, part, body, *, sequence, key, **kwargs):
        """One write to ``part`` of a card (``elements``, ``elements/<id>``, ``settings``,
        ``batch_update``): ``body`` plus the write's sequence and key."""
        return self.cardkit(profile, method, f"{CARDS}/{card_id}/{part}",
                            {**body, "sequence": sequence, "uuid": key}, **kwargs)

    def publish_card(self, ctx, chat_id, event_id, card, source_message="", *, voice: Voice, update_only=False,
                     on_sent=None, card_id="", on_created=None):
        """Show ``card``: replace the card ``source_message`` showed in this topic, or send a new one
        (``update_only``: replace or raise). browse-cards/ records each sent card for later updates.
        ``card_id`` / ``on_created`` let a caller persist the prepared card across delivery retries."""
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
                    return True
                if update_only:
                    raise ValueError("卡片更新失败，未创建新卡；请稍后重试")
        if update_only:
            raise ValueError("来源卡片已过期或不属于本话题，请使用本话题最新的任务卡")
        cid, mid = self.create_and_send_card(ctx.profile, chat_id, card, voice.key("browse", event_id), voice=voice,
                                             reply_to_message_id=ctx.reply_to_message_id,
                                             **({"card_id": card_id, "on_created": on_created}
                                                if card_id or on_created else {}))
        if not cid:
            self.reply(ctx, chat_id, "卡片暂不可用，请稍后重试。", voice.key("browse-error", event_id), voice=voice)
            return False
        if mid:
            self.remember(ctx, chat_id, mid)
            write_json(folder / (voice.safe_key(mid) + ".json"),
                       {"card_id": cid, "topic_id": ctx.topic_id, "chat_id": chat_id, "sequence": 0})
        if not mid:
            self.reply(ctx, chat_id, "卡片发送失败，请稍后重试。", voice.key("browse-send-error", event_id), voice=voice)
        if mid and on_sent:
            on_sent(mid)
        return bool(mid)
