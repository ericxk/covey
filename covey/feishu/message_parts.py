"""The content of a Feishu message as lark-cli and the message API give it: its text, image keys, file keys and the
file names given with them (collect_message_parts_named). Pure parsing, nothing is read from Feishu."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# Real Feishu keys look like img_v3_xxxx / file_v2_xxxx or img_<uuid-ish>.
# Require that shape so ordinary English words in rich-text messages
# ("file", "filename", "imgs", "file_path", ...) are never treated as keys.
IMAGE_KEY_RE = re.compile(r"\bimg_(?:v\d_[A-Za-z0-9_-]+|[A-Za-z0-9-]{16,})\b")
FILE_KEY_RE = re.compile(r"\bfile_(?:v\d_[A-Za-z0-9_-]+|[A-Za-z0-9-]{16,})\b")
# lark-cli renders attachments inside rich text as ![Image](img_key) and <file key="..." name="..."/>.
RENDERED_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(img_[A-Za-z0-9_-]+)\s*\)")
RENDERED_FILE_RE = re.compile(r'<file\s+key="([^"]*)"(?:\s+name="([^"]*)")?\s*/?>')
PARAGRAPH_RE = re.compile(r"\s*<p>.*</p>\s*", re.DOTALL)


def json_object(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def compact_whitespace(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def strip_paragraph_markup(text: str) -> str:
    """Some text messages (a note sent with a forward) arrive as <p>…</p> HTML."""
    if not isinstance(text, str) or not PARAGRAPH_RE.fullmatch(text):
        return text
    import html
    text = re.sub(r"<br\s*/?>|</p>\s*<p>", "\n", text.strip())
    text = re.sub(r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>', lambda m: f"[{m[2]}]({m[1]})" if m[2] else m[1], text)
    text = re.sub(r"</?(?:p|b|strong|i|em|u|s|del|code|span)(?:\s[^>]*)?>", "", text)
    return html.unescape(text).strip()


def collect_message_parts(value: Any) -> tuple[list[str], list[str], list[str]]:
    """Return text snippets, image keys, and file keys from Lark message content."""
    return collect_message_parts_named(value)[:3]


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


@dataclass
class _MessageParts:
    """What collect_message_parts_named finds while it walks message content."""
    texts: list[str] = field(default_factory=list)
    image_keys: list[str] = field(default_factory=list)
    file_keys: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)

    def visit(self, node: Any) -> None:
        node = json_object(node)
        if isinstance(node, str):
            self._visit_text(node)
            return
        if isinstance(node, list):
            for item in node:
                self.visit(item)
            return
        if not isinstance(node, dict):
            return

        text = self._tag_parts(node, str(node.get("tag") or node.get("type") or "").lower())
        if text is not None:
            self.visit(text)
        self._key_parts(node)

        # A card's or a collapsible panel's header, before its elements. Only under "header":
        # a post's "title" would come before a command and hide it.
        header = node.get("header")
        if isinstance(header, dict):
            self.visit([header.get("title"), header.get("subtitle")])
        # "columns" are a column set's; a table's column definitions there carry no text.
        for child_key in ("content", "elements", "body", "columns", "zh_cn", "en_us", "items", "files"):
            child = node.get(child_key)
            if child is not None:
                self.visit(child)

    def _visit_text(self, node: str) -> None:
        node = strip_paragraph_markup(node)
        # Remove the whole rendered shell, not just its key: "![Image]()" and
        # '<file key="" .../>' left behind read as broken content.
        for key, name in RENDERED_FILE_RE.findall(node):
            if key:
                self.file_keys.append(key)
                self.names.setdefault(key, name)
        node = RENDERED_FILE_RE.sub("", node)
        self.image_keys.extend(RENDERED_IMAGE_RE.findall(node))
        node = RENDERED_IMAGE_RE.sub("", node)
        found_images = IMAGE_KEY_RE.findall(node)
        found_files = FILE_KEY_RE.findall(node)
        if found_images or found_files:
            self.image_keys.extend(found_images)
            self.file_keys.extend(found_files)
            display_text = re.sub(
                r"\[(?:Image|File|Media)\s*:?\s*(?:img[_a-zA-Z0-9-]*|file[_a-zA-Z0-9-]*)?\]",
                "",
                node,
                flags=re.IGNORECASE,
            )
            display_text = IMAGE_KEY_RE.sub("", display_text)
            display_text = FILE_KEY_RE.sub("", display_text)
            if display_text.strip():
                self.texts.append(display_text.strip())
        elif node.strip():
            self.texts.append(node.strip())

    def _tag_parts(self, node: dict[str, Any], tag: str) -> Any:
        """Record a link, image or file tag; return the text of a text-like tag, or a card
        div's text objects, for visit to walk."""
        if tag == "a":
            # A forwarded or quoted document link is only useful with its address.
            text, href = node.get("text"), node.get("href")
            text = text.strip() if isinstance(text, str) else ""
            href = href.strip() if isinstance(href, str) else ""
            link = f"[{text}]({href})" if text and href and text != href else (href or text)
            if link:
                self.texts.append(link)
        elif tag in {"text", "md", "markdown", "at"}:
            text = node.get("text") or node.get("content") or node.get("user_name")
            if isinstance(text, str) and text.strip():
                return text
        elif tag == "div":
            fields = node.get("fields") if isinstance(node.get("fields"), list) else []
            return [node.get("text"), *(f.get("text") for f in fields if isinstance(f, dict))]
        elif tag in {"img", "image"}:
            for key_name in ("image_key", "img_key", "key"):
                key = node.get(key_name)
                if isinstance(key, str) and key:
                    self.image_keys.append(key)
        elif tag in {"file", "media"}:
            for key_name in ("file_key", "key"):
                key = node.get(key_name)
                if isinstance(key, str) and key:
                    self.file_keys.append(key)
        return None

    def _key_parts(self, node: dict[str, Any]) -> None:
        """Keys any node may carry, whatever its tag, and the file name given with a file key."""
        for key_name in ("image_key", "img_key"):
            key = node.get(key_name)
            if isinstance(key, str) and key:
                self.image_keys.append(key)
        file_key = node.get("file_key")
        if isinstance(file_key, str) and file_key:
            self.file_keys.append(file_key)
            if isinstance(node.get("file_name"), str) and node["file_name"]:
                self.names.setdefault(file_key, node["file_name"])


def collect_message_parts_named(value: Any) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """collect_message_parts plus the file names the message gives for its file keys."""
    value = json_object(value)
    parts = _MessageParts()
    parts.visit(value)
    cleaned = [re.sub(r"\n\s*\n(\s*\n)+", "\n\n", compact_whitespace(t)).strip() for t in parts.texts]
    return _unique([t for t in cleaned if t]), _unique(parts.image_keys), _unique(parts.file_keys), parts.names


def content_field(raw_content: str, field: str) -> str | None:
    parsed = json_object(raw_content)
    if isinstance(parsed, dict):
        value = parsed.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resource_specs(image_keys: list[str], file_keys: list[str], names: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    names = names or {}
    return [*(("image", key, "") for key in image_keys), *(("file", key, names.get(key, "")) for key in file_keys)]


def text_message_texts(raw: str, *, plain: bool = False) -> list[str]:
    """A text message's text when collect_message_parts_named found none in it: its ``text`` field (some
    arrive as <p>…</p> HTML), or, with ``plain``, content that is a string rather than a JSON object."""
    parsed = json_object(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
        return [strip_paragraph_markup(parsed["text"]).strip()]
    if plain and isinstance(parsed, str):
        return [strip_paragraph_markup(parsed)]
    return []
