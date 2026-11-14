"""Outbound Feishu doubles: they record what would have been sent and answer
with a configurable success.

``FakeCardKit`` is the Feishu side of the Messenger: every CardKit request and card
message, recorded and answered per call. ``install_card_transport`` routes them through
one of the transports below; tests put ``send_text`` and ``send_file`` in place of the
app Messenger's ``send_message`` and the app's ``send_result_file``."""
from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json
from unittest import mock

from covey.feishu import cardkit

OK = {"code": 0}
# Taken before any test replaces them: each fake copies their signatures.
CARDKIT_API, SEND_CARD_MESSAGE = cardkit.cardkit_api, cardkit.send_card_message
SIGNATURE = inspect.signature(CARDKIT_API)


@dataclasses.dataclass(frozen=True)
class CardCall:
    """One CardKit request as the Messenger made it (its log callback left out)."""

    profile: str
    method: str
    path: str
    data: dict
    options: dict

    @property
    def card(self) -> dict:
        """The card JSON of a whole replace (``put_card``); KeyError for any other request."""
        return json.loads(self.data["card"]["data"])

    @property
    def created(self) -> dict:
        """The card JSON of a create (``create_card``); KeyError for any other request."""
        return json.loads(self.data["data"])


class FakeCardKit:
    """The Feishu side of the Messenger while installed (``with`` or ``start(test)``).

    ``api`` and ``send`` stand in for ``cardkit.cardkit_api`` and ``cardkit.send_card_message``,
    the two functions the Messenger looks up at each call, with their signatures. They are
    Mocks: set ``return_value`` or ``side_effect`` (a list answers call by call; None is a
    failed write) and read ``call_args_list`` as for any Mock; ``calls`` reads the CardKit
    requests back by field. A request that reaches CardKit's transport while the fake is
    installed went around it; that fails the test when the fake is removed."""

    def __init__(self, return_value=OK, *, side_effect=None, sent=("om_card", False), sent_effect=None):
        return_value = dict(OK) if return_value is OK else return_value
        self.api = mock.create_autospec(CARDKIT_API, return_value=return_value, side_effect=side_effect)
        self.send = mock.create_autospec(SEND_CARD_MESSAGE, return_value=sent, side_effect=sent_effect)
        self.escaped = []
        self._stack = None

    def __enter__(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cardkit, "cardkit_api", self.api))
        stack.enter_context(mock.patch.object(cardkit, "send_card_message", self.send))
        stack.enter_context(mock.patch.object(cardkit, "call", self._escape))
        self._stack = stack
        return self

    def __exit__(self, kind, *_):
        self._stack.close()
        if self.escaped and kind is None:
            raise AssertionError(f"CardKit requests went around FakeCardKit: {self.escaped}")
        return False

    def start(self, test):
        """Install for the rest of ``test``; its cleanups remove it."""
        self.__enter__()
        test.addCleanup(self.__exit__, None)
        return self

    def _escape(self, argv, **_):
        self.escaped.append(" ".join(str(arg) for arg in list(argv)[:6]))
        return None

    @property
    def calls(self) -> list[CardCall]:
        def read(call):
            fields = SIGNATURE.bind(*call.args, **call.kwargs).arguments
            options = {name: fields.pop(name) for name in list(fields) if name not in ("profile", "method", "path", "data")}
            options.pop("log", None)
            return CardCall(options=options, **fields)
        return [read(call) for call in self.api.call_args_list]

    @property
    def last(self) -> CardCall:
        return self.calls[-1]


class MemoryTransport:
    """Keeps the result card's elements in memory so a test can read the card back.

    Events are tuples ``(kind, payload, ok)``.
    """

    def __init__(self, *, card_ok=True, text_ok=True, file_ok=True, question_ok=True, file_error=False):
        self.card_ok, self.text_ok, self.file_ok, self.question_ok = card_ok, text_ok, file_ok, question_ok
        self.events = []
        self.cards = {"result-card": [{"tag": "markdown", "element_id": "answer", "content": "placeholder"}]}
        self.file_error = file_error

    def api(self, profile, method, path, data, **kwargs):
        if method == "PATCH" and path.endswith("/elements/answer"):
            text = json.loads(data["partial_element"])["content"]
            self.events.append(("body_card", text, self.card_ok))
            if not self.card_ok:
                return None
            self.cards["result-card"][0]["content"] = text
            return {"ok": True}
        if method == "POST" and path.endswith("/elements"):
            elements = json.loads(data["elements"])
            if any(e.get("tag") == "form" for e in elements):
                self.events.append(("question_insert", elements, self.question_ok))
                if not self.question_ok:
                    return None
                assert data["target_element_id"] == "answer"
                self.cards["result-card"][1:1] = elements
            return {"ok": True}
        if method == "POST" and path == "/open-apis/cardkit/v1/cards":
            card = json.loads(data["data"])
            cid = "standalone-" + str(len(self.cards))
            self.cards[cid] = card["body"]["elements"]
            return {"data": {"card_id": cid}}
        return {"ok": True}

    def send_card(self, profile, chat, cid, key, **kwargs):
        self.events.append(("question_message", self.cards[cid], self.question_ok))
        return ("om-question", "") if self.question_ok else ("", "")

    def send_text(self, profile, chat, text, key, **kwargs):
        self.events.append(("body_text", text, self.text_ok))
        return self.text_ok

    def send_file(self, ctx, job, text):
        self.events.append(("body_file", text, self.file_ok and not self.file_error))
        if self.file_error:
            raise OSError("synthetic file failure")
        return self.file_ok


class OutboundTransport:
    """One switch for every outbound call; events are dicts with ``kind`` and ``ok``."""

    def __init__(self, ok=False):
        self.ok = ok
        self.events = []

    def send_text(self, profile, chat, text, key, **kwargs):
        self.events.append({"kind": "text", "profile": profile, "chat": chat, "text": text,
            "key": key, "reply_to": kwargs.get("reply_to_message_id"), "card": kwargs.get("card"), "ok": self.ok})
        return self.ok

    def send_file(self, ctx, job, text):
        self.events.append({"kind": "file", "chat": job.chat_id, "text": text, "ok": self.ok})
        return self.ok

    def api(self, *args, **kwargs):
        self.events.append({"kind": "card_api", "ok": self.ok})
        return {"ok": True} if self.ok else None

    def send_card(self, *args, **kwargs):
        self.events.append({"kind": "card_message", "ok": self.ok})
        return ("synthetic-card", "") if self.ok else ("", "synthetic failure")


def install_card_transport(stack, tx):
    """Route every CardKit call and card message of the worker through ``tx``."""
    return stack.enter_context(FakeCardKit(side_effect=tx.api, sent_effect=tx.send_card))


def route_launch_receipts(api, receipt):
    """A ``side_effect`` for ``FakeCardKit`` that hands launch receipts
    (idempotency keys ``launch-receipt-…``) to ``receipt`` and every other CardKit call to ``api``."""
    def route(profile, method, path, data, **kwargs):
        target = receipt if str(data.get("uuid") or "").startswith("launch-receipt-") else api
        return target(profile, method, path, data, **kwargs)
    return route
