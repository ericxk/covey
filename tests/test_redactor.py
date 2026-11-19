"""Terms go out as stand-ins and come back as the exact spellings they replaced (core/redactor.py)."""
import json
import threading
import unittest

from covey.core.redaction import auto_stand_in, normalize
from covey.core.redactor import Redactor


def redactor(*terms, **config):
    return Redactor(normalize({**config, "terms": [t if isinstance(t, dict) else {"term": t} for t in terms]}))


def sse(data, name=None):
    return f"event: {name or data['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def text_delta(index, text):
    return sse({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}})


def events(raw):
    return [json.loads(line[6:]) for line in raw.decode().split("\n") if line.startswith("data: ")]


class MatchingTests(unittest.TestCase):
    def test_each_spelling_has_its_own_stand_in_and_comes_back_exactly(self):
        r = redactor("Contoso")
        text = "Contoso, CONTOSO and contoso/src"
        out = r.redact_text(text)
        for spelling in ("Contoso", "CONTOSO", "contoso"):
            self.assertIn(auto_stand_in(spelling), out)
        self.assertNotIn("ontoso", out.lower())
        self.assertEqual(r.restore_text(out), text)
        # An automatic stand-in comes back in any case.
        self.assertEqual(r.restore_text(auto_stand_in("CONTOSO").lower()), "CONTOSO")
        self.assertEqual(r.hits, {"Contoso": 3})

    def test_a_stand_in_an_older_version_let_the_owner_name_is_not_used(self):
        # The owner names none (T566): the term goes out as its automatic stand-in, never as "Widget" (which could hold
        # another term) and comes back from it.
        r = redactor({"term": "Acme", "stand_in": "Widget"})
        self.assertEqual(r.redact_text("Acme ACME"), auto_stand_in("Acme") + " " + auto_stand_in("ACME"))
        self.assertEqual(r.restore_text(f"Widget {auto_stand_in('Acme')}"), "Widget Acme")
        self.assertEqual(r.effective_stand_in("Acme"), auto_stand_in("Acme"))
        self.assertTrue(r.restorable("Acme"))

    def test_ascii_ends_match_only_as_words_unless_anywhere(self):
        r = redactor("foo", {"term": "bar", "anywhere": True}, "鹰眼")
        out = r.redact_text("foo foobar xfoo foo_1 sidebar 鹰眼计划")
        self.assertEqual(out.split()[:3], [auto_stand_in("foo"), "foo" + auto_stand_in("bar"), "xfoo"])
        self.assertIn(auto_stand_in("foo") + "_1", out)  # Only ASCII letters and digits are word characters.
        self.assertIn("side" + auto_stand_in("bar"), out)
        self.assertIn(auto_stand_in("鹰眼") + "计划", out)
        self.assertEqual(r.restore_text(out), "foo foobar xfoo foo_1 sidebar 鹰眼计划")

    def test_the_longest_term_wins_and_case_matters_when_asked(self):
        r = redactor("Alpha", "Alpha Beta", ignore_case=False)
        self.assertEqual(r.redact_text("Alpha Beta alpha"), auto_stand_in("Alpha Beta") + " alpha")

    def test_terms_once_sharing_a_stand_in_each_come_back_and_restore_off_restores_nothing(self):
        r = redactor({"term": "a1", "stand_in": "Zed"}, {"term": "b2", "stand_in": "Zed"}, "c3")
        self.assertEqual(r.redact_text("a1 b2"), f"{auto_stand_in('a1')} {auto_stand_in('b2')}")
        self.assertEqual(r.restore_text(r.redact_text("a1 b2 Zed")), "a1 b2 Zed")
        self.assertTrue(r.restorable("a1"))
        off = redactor("c3", restore=False)
        out = off.redact_text("c3")
        self.assertEqual(off.restore_text(out), out)
        self.assertFalse(off.restorable("c3"))
        self.assertFalse(redactor().active)
        self.assertEqual(redactor().redact_text("anything"), "anything")

    def test_the_spelling_table_is_safe_across_threads(self):
        r = redactor("Contoso")
        spellings = ["".join(c.upper() if (n >> i) & 1 else c for i, c in enumerate("contoso")) for n in range(128)]
        threads = [threading.Thread(target=r.redact_text, args=(" ".join(spellings),)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(r.hits["Contoso"], 8 * 128)
        self.assertEqual(r.restore_text(" ".join(auto_stand_in(s) for s in spellings)), " ".join(spellings))


class BodyTests(unittest.TestCase):
    def test_a_request_keeps_structure_and_thinking_and_redacts_everything_else(self):
        r = redactor("Contoso")
        body = {
            "model": "Contoso", "system": [{"type": "text", "text": "Contoso rules", "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "Contoso", "description": "Contoso tool", "input_schema": {"description": "Contoso"}}],
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "<system-reminder>CLAUDE.md says Contoso</system-reminder> hi Contoso"},
                    {"type": "tool_result", "tool_use_id": "Contoso", "content": "read /Contoso/a"},
                    {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "Contoso doc"}},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "Contoso"}}]},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "Contoso", "signature": "sig"},
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"type": "Contoso", "old": ["Contoso"]}},
                    {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                        {"type": "web_search_result", "title": "Contoso", "encrypted_content": "Contoso"}]}]}]}
        original = json.dumps(body)
        out = r.redact_request(body)
        self.assertEqual(json.dumps(body), original)  # The caller's body is untouched.
        stand_in = auto_stand_in("Contoso")
        self.assertEqual((out["model"], out["tools"][0]["name"], out["tools"][0]["input_schema"]),
                         ("Contoso", "Contoso", {"description": "Contoso"}))
        self.assertEqual(out["system"][0]["text"], stand_in + " rules")
        self.assertEqual(out["tools"][0]["description"], stand_in + " tool")
        user, assistant = out["messages"][0]["content"], out["messages"][1]["content"]
        self.assertIn("says " + stand_in + "</system-reminder> hi " + stand_in, user[0]["text"])
        self.assertEqual((user[1]["tool_use_id"], user[1]["content"]), ("Contoso", f"read /{stand_in}/a"))
        self.assertEqual(user[2]["source"]["data"], stand_in + " doc")
        self.assertEqual(user[3]["source"]["data"], "Contoso")
        self.assertEqual(assistant[0], body["messages"][1]["content"][0])
        self.assertEqual(assistant[1]["input"], {"type": stand_in, "old": [stand_in]})
        # The vendor's opaque search result stays decodable; its title is text.
        self.assertEqual(assistant[2]["content"][0], {"type": "web_search_result", "title": stand_in,
                                                      "encrypted_content": "Contoso"})
        self.assertEqual(r.hits["Contoso"], 9)

    def test_a_response_turns_text_and_tool_input_back_but_not_thinking(self):
        r = redactor("Contoso")
        s = auto_stand_in("Contoso")
        body = {"id": "msg", "type": "message", "role": "assistant", "content": [
            {"type": "thinking", "thinking": s, "signature": "x"}, {"type": "text", "text": f"in {s}"},
            {"type": "tool_use", "id": s, "name": "Read", "input": {"file_path": f"/{s}/a"}}]}
        out = r.restore_response(body)
        self.assertEqual(out["content"][0]["thinking"], s)
        self.assertEqual(out["content"][1]["text"], "in Contoso")
        self.assertEqual(out["content"][2], {"type": "tool_use", "id": s, "name": "Read",
                                             "input": {"file_path": "/Contoso/a"}})


class StreamTests(unittest.TestCase):
    def test_stand_ins_split_across_chunks_and_deltas_come_back(self):
        r = redactor("Contoso", {"term": "Acme", "stand_in": "Widget"})
        spoken = r.redact_text("Ask CONTOSO about Acme, not Widgets.")
        raw = sse({"type": "message_start", "message": {"content": []}})
        raw += sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        raw += b"".join(text_delta(0, spoken[i:i + 3]) for i in range(0, len(spoken), 3))
        raw += sse({"type": "content_block_stop", "index": 0})
        tool_input = json.dumps({"file_path": "/x/" + r.redact_text("contoso") + "/f", "n": 1})
        raw += sse({"type": "content_block_start", "index": 1,
                    "content_block": {"type": "tool_use", "id": "t", "name": "Read", "input": {}}})
        raw += b"".join(sse({"type": "content_block_delta", "index": 1, "delta": {
            "type": "input_json_delta", "partial_json": tool_input[i:i + 4]}}) for i in range(0, len(tool_input), 4))
        raw += sse({"type": "content_block_stop", "index": 1})
        raw += sse({"type": "message_stop"})
        stream = r.stream()
        out = b"".join(stream.feed(raw[i:i + 7]) for i in range(0, len(raw), 7)) + stream.close()
        rows = events(out)
        text = "".join(e["delta"]["text"] for e in rows if e.get("delta", {}).get("type") == "text_delta")
        self.assertEqual(text, "Ask CONTOSO about Acme, not Widgets.")
        json_deltas = [e for e in rows if e.get("delta", {}).get("type") == "input_json_delta"]
        self.assertEqual(len(json_deltas), 1)  # Gathered, then sent once before the block's stop.
        self.assertEqual(json.loads(json_deltas[0]["delta"]["partial_json"]), {"file_path": "/x/contoso/f", "n": 1})
        stop = next(i for i, e in enumerate(rows) if e["type"] == "content_block_stop" and e["index"] == 1)
        self.assertEqual(rows[stop - 1], json_deltas[0])
        self.assertEqual(rows[-1]["type"], "message_stop")

    def test_thinking_and_unparsable_tool_input_and_other_events_pass(self):
        r = redactor("Contoso")
        s = auto_stand_in("Contoso")
        thinking = sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": s}})
        broken = sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                                                                             "partial_json": '{"p": "' + s}})
        ping = b"event: ping\ndata: not json\n\n"
        stream = r.stream()
        out = stream.feed(thinking + ping + broken + sse({"type": "content_block_stop", "index": 1}))
        self.assertTrue(out.startswith(thinking + ping))
        self.assertIn('"partial_json": "{\\"p\\": \\"Contoso"', out.decode())

    def test_without_terms_the_stream_is_untouched(self):
        stream = redactor().stream()
        raw = text_delta(0, "RX12345678 R")
        self.assertEqual(stream.feed(raw) + stream.close(), raw)


if __name__ == "__main__":
    unittest.main()
