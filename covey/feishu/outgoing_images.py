"""The local images an answer's Markdown links to, uploaded to Feishu and linked by their image key instead
(prepare_outgoing_markdown_images)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from covey.core import env as cenv
from covey.core.paths import warn_legacy
from covey.feishu.transport import CliFailed, cli_json, data_of, lark_argv


MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+(?:\s+\"[^\"]*\")?)\)")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


def parse_markdown_image_target(raw_target: str) -> str:
    target = raw_target.strip()
    if " " in target and not Path(target).exists():
        target = target.split(" ", 1)[0]
    return target.strip("<>")


def extract_image_key(payload: dict[str, Any]) -> str | None:
    data = data_of(payload)
    for key in ("image_key", "imageKey"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value:
            return value
    image = data.get("image") if isinstance(data, dict) else None
    if isinstance(image, dict):
        for key in ("image_key", "imageKey"):
            value = image.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def upload_image(path: Path, profile: str | None = None) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    argv = lark_argv(
        profile,
        "im",
        "images",
        "create",
        "--as",
        "bot",
        "--data",
        '{"image_type":"message"}',
        "--file",
        f"./{path.name}",
        "--format",
        "json",
    )
    try:
        payload = cli_json(argv, timeout=60, runner=subprocess.run, cwd=str(path.parent))
    except (OSError, subprocess.TimeoutExpired, CliFailed, json.JSONDecodeError):
        # Called while a result is delivered: the link stays as written rather than the result failing.
        return None
    return extract_image_key(payload)


def resolve_local_image_path(target: str, base_dirs: list[Path]) -> Path | None:
    if target.startswith(("http://", "https://", "img_")):
        return None
    candidate = Path(target).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [base / candidate for base in base_dirs]
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_file() and resolved.suffix.lower() in IMAGE_SUFFIXES:
            return resolved
    return None


def prepare_outgoing_markdown_images(text: str, profile: str | None = None, base_dirs: list[Path] | None = None) -> str:
    """Upload local Markdown image links and rewrite them to Feishu image keys."""
    if "![" not in text:
        return text
    search_dirs = [Path.cwd()]
    if base_dirs:
        search_dirs = [*base_dirs, *search_dirs]
    outbox_dirs: list[Path] = []
    for base in search_dirs:
        outbox_dirs.append(base / ".local-state" / "outgoing_media")
        outbox_dirs.append(base / ".local-state" / "generated")
    env_dirs = [item for item in cenv.get("COVEY_OUTGOING_MEDIA_DIRS", "").split(os.pathsep) if item]
    search_dirs = [*search_dirs, *outbox_dirs, *(Path(item).expanduser() for item in env_dirs)]
    cache: dict[Path, str | None] = {}

    def replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw_target = match.group(2)
        target = parse_markdown_image_target(raw_target)
        if target.startswith(("http://", "https://", "img_")):
            return match.group(0)
        local_path = resolve_local_image_path(target, search_dirs)
        if not local_path:
            return match.group(0)
        if any(local_path.is_relative_to(folder.resolve()) for folder in outbox_dirs if folder.is_dir()):
            warn_legacy(local_path)
        if local_path not in cache:
            cache[local_path] = upload_image(local_path, profile)
        image_key = cache.get(local_path)
        if not image_key:
            return match.group(0)
        return f"![{alt}]({image_key})"

    return MARKDOWN_IMAGE_RE.sub(replace, text)
