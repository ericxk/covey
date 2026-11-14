"""Text helpers the process panel is drawn with (cards/trace_view.py): code in a note shown as written, and an escaped
text cut without breaking an entity. The trace itself is core/trace.py."""
import html
import re


# Fenced blocks, then inline code spans (a run of backticks closed by the same run, within a paragraph).
CODE = re.compile(r"(?ms)^[ \t]*(```+|~~~+)[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)"
                  r"|(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\2(?!`)")


def literal_code(text):
    """A note as ``safe_text(markdown=True)`` escaped it, its code shown as written: Feishu does not
    decode entities inside code (T182 #1111 read ``&lt;state&gt;`` and ``&quot;T182&quot;``), so code
    gets its characters back. A ``<`` keeps a zero-width space after it, so no tag can start there even
    where a chunk break leaves half a code span outside its backticks. Drawing time, not storage:
    snapshots saved escaped, by this release or an earlier one, read the same."""
    return CODE.sub(lambda match: html.unescape(match.group(0)).replace("<", "<\u200b"), text)


def cut_escaped(text, limit):
    """``text`` as safe_text escaped it, cut to ``limit`` with "…" but never inside an entity
    (``&amp;#x27;`` shown half) or right after an escaping backslash."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    amp = head.rfind("&")
    if amp != -1 and ";" not in head[amp:] and limit - amp <= 10:
        head = head[:amp]
    if (len(head) - len(head.rstrip("\\"))) % 2:
        head = head[:-1]
    return head + "…"
