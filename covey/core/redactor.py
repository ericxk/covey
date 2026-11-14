"""Matching for sensitive-term redaction: terms to stand-ins in requests, stand-ins back in replies.

One ``Redactor`` serves one turn's relay (backends/claude/redaction.py) and the dashboard's preview,
built from ``core.redaction.load_config``. Standard library only.

Forward: one regular expression over the listed terms, longest first. A term that starts (ends) with an
ASCII letter or digit does not match where one adjoins it there, unless the term is ``anywhere``; with
``ignore_case`` any case matches. Each spelling has a stand-in of its own: the listed spelling its
``stand_in`` (or ``auto_stand_in`` of it), every other spelling ``auto_stand_in`` of that spelling, kept
in the spelling table so a reply's copy turns back into exactly that spelling (an Edit's old_string, a
path). Requests are redacted whole every time: Claude Code holds the restored history, so the same
history becomes the same request again and the vendor's prompt cache still matches.

Back: a stand-in used by one term turns back into it; an automatic one (``RX`` and 8 hex digits) in any
case, by the spelling table; one shared by several terms stays, as nothing tells which term it was; with
``restore`` off nothing turns back.

Bodies are Anthropic Messages JSON. Structural keys (``STRUCTURAL``) stay as they are, except the
``data`` of a document's text source; thinking blocks are signed and stay whole; every string of a tool
call's ``input`` is free text. ``<system-reminder>`` is redacted too: CLAUDE.md and AGENTS.md are in it.
"""
from __future__ import annotations

import collections
import json
import re
import threading

from covey.core.redaction import AUTO_PREFIX, auto_stand_in

STRUCTURAL = frozenset({"type", "id", "tool_use_id", "name", "model", "signature", "media_type", "cache_control",
                        "input_schema", "stop_reason", "role", "data",
                        # The vendor's opaque web search results and citations: changed, they no longer decode.
                        "encrypted_content", "encrypted_index"})
UNTOUCHED = frozenset({"thinking", "redacted_thinking"})  # Signed by the vendor: never changed.
_ALNUM = "A-Za-z0-9"
_AUTO = re.compile(re.escape(AUTO_PREFIX) + "[0-9A-F]{8}", re.IGNORECASE)  # No boundaries: anywhere terms.
# The tail of a streamed text that may still grow into an automatic stand-in.
_AUTO_TAIL = re.compile("R(?:X[0-9A-F]{0,7})?$", re.IGNORECASE)


def _guarded(text, anywhere):
    """``text`` as a pattern, kept from matching inside a longer ASCII word unless ``anywhere``."""
    out = re.escape(text)
    if not anywhere and re.match(f"[{_ALNUM}]", text[:1]):
        out = f"(?<![{_ALNUM}])" + out
    if not anywhere and re.match(f"[{_ALNUM}]", text[-1:]):
        out += f"(?![{_ALNUM}])"
    return out


def _json_inner(text):
    return json.dumps(text, ensure_ascii=False)[1:-1]


class Redactor:
    def __init__(self, config):
        self.ignore_case = bool(config.get("ignore_case", True))
        self.restore = bool(config.get("restore", True))
        self.terms = [dict(row) for row in config.get("terms") or []]
        self.hits = collections.Counter()  # Listed term -> replacements so far.
        self._lock = threading.Lock()
        self._spellings = {}  # Automatic stand-in (upper case) -> the exact spelling it replaced.
        self._forward = None
        if self.terms:
            # Longest first, so a term inside a longer one never wins; one group per row tells which.
            order = sorted(range(len(self.terms)), key=lambda i: len(self.terms[i]["term"]), reverse=True)
            self._forward = re.compile(
                "|".join(f"(?P<t{i}>{_guarded(self.terms[i]['term'], self.terms[i]['anywhere'])})" for i in order),
                re.IGNORECASE if self.ignore_case else 0)
        uses = collections.Counter(row["stand_in"] for row in self.terms if row["stand_in"])
        self._listed = {}  # Unique listed stand-in -> its term's row.
        for row in self.terms:
            if not row["stand_in"]:
                self._spellings[auto_stand_in(row["term"]).upper()] = row["term"]
            elif uses[row["stand_in"]] == 1:
                self._listed[row["stand_in"]] = row
        self._back = self._back_json = None
        self._tokens = []  # The listed stand-ins that turn back, longest first.
        if self.restore and self.terms:
            self._tokens = sorted(self._listed, key=len, reverse=True)
            auto = "(?i:" + _AUTO.pattern + ")"  # Listed stand-ins match in their own case only.
            self._back = re.compile("|".join([_guarded(s, self._listed[s]["anywhere"]) for s in self._tokens]
                                             + [auto]))
            self._back_json = re.compile("|".join([re.escape(_json_inner(s)) for s in self._tokens] + [auto]))

    @property
    def active(self):
        """Whether there is anything to replace."""
        return self._forward is not None

    def effective_stand_in(self, term):
        """The stand-in the listed spelling of ``term`` goes out as; None for a term not listed."""
        for row in self.terms:
            if row["term"] == term:
                return row["stand_in"] or auto_stand_in(term)
        return None

    def restorable(self, term):
        """Whether ``term``'s stand-in turns back into it in replies."""
        for row in self.terms:
            if row["term"] == term:
                return self.restore and (not row["stand_in"] or row["stand_in"] in self._listed)
        return False

    # Strings.

    def redact_text(self, text, tally=None):
        """``text`` with every listed term replaced; ``tally`` (a Counter), if given, counts them per term too."""
        if self._forward is None or not isinstance(text, str) or not text:
            return text

        def replace(match):
            row = self.terms[int(match.lastgroup[1:])]
            spelling = match.group(0)
            with self._lock:
                self.hits[row["term"]] += 1
                if spelling == row["term"]:
                    stand_in = row["stand_in"] or auto_stand_in(spelling)
                else:
                    stand_in = auto_stand_in(spelling)
                    self._spellings[stand_in.upper()] = spelling
            if tally is not None:
                tally[row["term"]] += 1
            return stand_in

        return self._forward.sub(replace, text)

    def _back_one(self, match, encode=None):
        token = match.group(0)
        if token in self._listed:
            term = self._listed[token]["term"]
            return encode(term) if encode else term
        with self._lock:
            spelling = self._spellings.get(token.upper())
        if spelling is None:
            return token
        return encode(spelling) if encode else spelling

    def restore_text(self, text, start=0):
        """``text`` with the stand-ins turned back; ``text[:start]`` is only context and stays as it is."""
        if self._back is None or not isinstance(text, str) or not text:
            return text
        parts, pos = [text[:start]], start
        for match in self._back.finditer(text, start):
            parts += [text[pos:match.start()], self._back_one(match)]
            pos = match.end()
        parts.append(text[pos:])
        return "".join(parts)

    def restore_json_text(self, text):
        """Turn stand-ins back inside JSON-encoded text (a tool call's input that did not parse)."""
        if self._back_json is None or not text:
            return text
        return self._back_json.sub(lambda m: self._back_one(m, _json_inner), text)

    def held_tail(self, text):
        """The length of ``text``'s tail that may still grow into a stand-in: held back from a stream."""
        if self._back is None:
            return 0
        auto = _AUTO_TAIL.search(text)
        best = len(auto.group(0)) if auto else 0
        for token in self._tokens:
            # A whole stand-in at the end waits too: the next character decides its boundary.
            for n in range(min(len(token), len(text)), best, -1):
                if text.endswith(token[:n]):
                    best = n
                    break
        return best

    # Anthropic Messages bodies.

    def _walk(self, value, change, structured, text_source=False):
        if isinstance(value, str):
            return change(value)
        if isinstance(value, list):
            return [self._walk(item, change, structured) for item in value]
        if not isinstance(value, dict):
            return value
        if structured and value.get("type") in UNTOUCHED:
            return value
        document_text = (structured and value.get("type") == "document" and isinstance(value.get("source"), dict)
                         and value["source"].get("type") == "text")
        out = {}
        for key, item in value.items():
            if structured and key in STRUCTURAL and not (key == "data" and text_source):
                out[key] = item
            elif structured and key == "input":
                out[key] = self._walk(item, change, False)  # A tool call's arguments: every string is text.
            else:
                out[key] = self._walk(item, change, structured, document_text and key == "source")
        return out

    def redact_request(self, body, tally=None):
        """A Messages request with every term replaced (a new object; ``body`` is left as it is)."""
        if self._forward is None:
            return body
        return self._walk(body, lambda text: self.redact_text(text, tally), True)

    def restore_response(self, body):
        """A non-streamed Messages response with the stand-ins turned back."""
        if self._back is None:
            return body
        return self._walk(body, self.restore_text, True)

    def restore_value(self, value):
        """Every string in ``value`` turned back (a tool call's parsed input)."""
        if self._back is None:
            return value
        return self._walk(value, self.restore_text, False)

    def stream(self):
        """A restorer for one SSE response: ``feed(bytes) -> bytes``, then ``close() -> bytes``."""
        return StreamRestorer(self)


def _sse(name, data):
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class StreamRestorer:
    """Turn stand-ins back in one Anthropic SSE stream, event by event.

    A text block holds back the tail that may still grow into a stand-in and lets it go with the next
    delta or before its ``content_block_stop``. A tool call's ``input_json_delta`` fragments are gathered
    per block and go out before its stop as one delta: parsed, turned back and written again (as text
    when they do not parse). Every other event (thinking, signatures, citations, usage, errors) passes
    as it came.
    """

    def __init__(self, redactor):
        self._r = redactor
        self._buffer = b""
        self._text = {}  # Block index -> (held tail, the last character already sent).
        self._json = {}  # Block index -> gathered partial_json.

    def feed(self, chunk):
        if self._r._back is None:
            return chunk
        self._buffer += chunk.replace(b"\r\n", b"\n")
        out = []
        while True:
            end = self._buffer.find(b"\n\n")
            if end < 0:
                break
            raw, self._buffer = self._buffer[:end], self._buffer[end + 2:]
            out += self._event(raw)
        return b"".join(out)

    def close(self):
        """The rest: an unfinished event as it came, and whatever a block still held."""
        if self._r._back is None:
            return b""
        tail, self._buffer = self._buffer, b""
        out = self._event(tail) if tail.strip() else []
        for index in list(self._text) + list(self._json):
            out += self._flush(index)
        return b"".join(out)

    def _event(self, raw):
        lines = raw.decode("utf-8", errors="replace").split("\n")
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        data_text = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        try:
            data = json.loads(data_text) if data_text else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return [raw + b"\n\n"]
        kind = data.get("type")
        index = data.get("index", 0)
        if kind == "content_block_start":
            block = data.get("content_block")
            if isinstance(block, dict) and block.get("type") not in UNTOUCHED:
                data["content_block"] = self._r.restore_response(block)
            return [_sse(name or kind, data)]
        if kind == "content_block_stop":
            return self._flush(index) + [raw + b"\n\n"]
        if kind != "content_block_delta" or not isinstance(data.get("delta"), dict):
            return [raw + b"\n\n"]
        delta = data["delta"]
        if delta.get("type") == "input_json_delta":
            self._json[index] = self._json.get(index, "") + str(delta.get("partial_json") or "")
            return []
        if delta.get("type") != "text_delta":
            return [raw + b"\n\n"]
        held, before = self._text.get(index, ("", ""))
        text = held + str(delta.get("text") or "")
        keep = self._r.held_tail(text)
        ready, held = text[:len(text) - keep], text[len(text) - keep:]
        self._text[index] = held, (ready[-1:] or before)
        if not ready:
            return []
        delta["text"] = self._r.restore_text(before + ready, len(before))[len(before):]
        return [_sse(name or kind, data)]

    def _flush(self, index):
        out = []
        held, before = self._text.pop(index, ("", ""))
        if held:
            text = self._r.restore_text(before + held, len(before))[len(before):]
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                    "delta": {"type": "text_delta", "text": text}}))
        gathered = self._json.pop(index, None)
        if gathered is not None:
            try:
                partial = json.dumps(self._r.restore_value(json.loads(gathered)), ensure_ascii=False) if gathered else ""
            except ValueError:
                partial = self._r.restore_json_text(gathered)
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                    "delta": {"type": "input_json_delta", "partial_json": partial}}))
        return out
