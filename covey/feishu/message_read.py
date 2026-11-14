"""Reading from Feishu through lark-cli: a message's detail (fetch_message_detail, message_item,
apply_message_metadata), a thread's newest messages (list_thread_messages), and the images and files a message
carries (download_resource)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from covey.feishu import message_request
from covey.feishu.transport import CliFailed, cli_json, data_of, lark_argv, run_cli


@dataclass(frozen=True)
class IncomingMedia:
    key: str
    kind: str
    local_path: Path | None = None
    error: str | None = None


def safe_slug(value: str, fallback: str = "media") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip(".-")
    return (slug or fallback)[:120]


def download_resource(
    *,
    message_id: str,
    file_key: str,
    kind: str,
    media_dir: Path,
    profile: str | None = None,
    index: int = 1,
    name: str = "",
) -> IncomingMedia:
    media_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".png" if kind == "image" else ".bin"
    named = Path(name).suffix.lower() if name else ""
    if re.fullmatch(r"\.[a-z0-9]{1,8}", named):
        suffix = named  # Keep the type visible to the model reading the file.
    output_name = f"{safe_slug(message_id)}-{index}-{safe_slug(file_key)}{suffix}"
    argv = lark_argv(
        profile,
        "im",
        "+messages-resources-download",
        "--as",
        "bot",
        "--message-id",
        message_id,
        "--file-key",
        file_key,
        "--type",
        kind,
        "--output",
        f"./{output_name}",
        "--format",
        "json",
    )
    try:
        proc = run_cli(argv, runner=subprocess.run, cwd=str(media_dir), text=True, capture_output=True, timeout=60,
                       check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return IncomingMedia(key=file_key, kind=kind, error=_process_error(exc))
    output_path = media_dir / output_name
    if proc.returncode == 0 and output_path.exists():
        return IncomingMedia(key=file_key, kind=kind, local_path=output_path)
    detail = (proc.stderr or proc.stdout or f"download failed rc={proc.returncode}")[-500:]
    return IncomingMedia(key=file_key, kind=kind, error=detail.strip())


def _process_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout:g}s"
    return str(exc)[-500:]


def fetch_detail_or_error(fetcher: Any, message_id: str, profile: str | None) -> dict[str, Any]:
    """A slow or missing lark-cli leaves the message without its detail; it never drops it."""
    try:
        return fetcher(message_id, profile)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": _process_error(exc)}


def fetch_message_detail(message_id: str, profile: str | None = None) -> dict[str, Any]:
    """Fetch a Feishu message detail payload through the raw OpenAPI escape hatch."""
    if not message_id:
        return {"ok": False, "error": "missing message_id"}
    params = {"user_id_type": "open_id", "card_msg_content_type": "user_card_content"}
    argv = lark_argv(profile, "api", "GET", f"/open-apis/im/v1/messages/{message_id}", "--as", "bot",
                     "--params", json.dumps(params, separators=(",", ":")), "--format", "json")
    try:
        payload = cli_json(argv, timeout=30, runner=message_request.read_runner(profile, message_id, params) or subprocess.run)
    except CliFailed as exc:
        proc = exc.proc
        return {"ok": False, "error": (proc.stderr or proc.stdout or f"rc={proc.returncode}")[-1000:]}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"non-json response: {exc.doc[-1000:]}"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "unexpected response shape"}


def list_thread_messages(thread_id: str, profile: str | None = None, page_size: int = 50) -> list[dict[str, Any]]:
    """A thread's newest ``page_size`` messages, oldest first; [] when they cannot be read."""
    params = {"container_id_type": "thread", "container_id": thread_id, "sort_type": "ByCreateTimeDesc",
              "page_size": page_size}
    argv = lark_argv(profile, "api", "GET", "/open-apis/im/v1/messages", "--as", "bot",
                     "--params", json.dumps(params, separators=(",", ":")), "--format", "json")
    try:
        payload = cli_json(argv, timeout=30)
    except (CliFailed, json.JSONDecodeError, OSError, subprocess.SubprocessError):
        return []
    return list(reversed(message_items(payload))) if isinstance(payload, dict) else []


def message_items(detail: dict[str, Any]) -> list[dict[str, Any]]:
    data = data_of(detail)
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    item = data.get("item") or data.get("message")
    if isinstance(item, dict):
        return [item]
    return []


def message_item(detail: dict[str, Any], message_id: str = "") -> dict[str, Any] | None:
    for item in message_items(detail):
        item_id = str(item.get("message_id") or item.get("id") or "")
        if message_id and item_id == message_id:
            return item
    items = message_items(detail)
    return items[0] if items else None


def item_type(item: dict[str, Any]) -> str:
    return str(item.get("msg_type") or item.get("message_type") or "").strip()


def apply_message_metadata(event: dict[str, Any], item: dict[str, Any]) -> None:
    for key in ("message_id", "root_id", "parent_id", "thread_id", "chat_id", "create_time", "update_time", "upper_message_id"):
        value = item.get(key)
        if value not in (None, ""):
            event[key] = value
    msg_type = item_type(item)
    if msg_type:
        event["message_type"] = msg_type
    mentions = item.get("mentions")
    if isinstance(mentions, list) and mentions:
        event["mentions"] = mentions
    sender = item.get("sender")
    if isinstance(sender, dict) and sender:
        event["sender"] = sender
        sender_type = sender.get("sender_type") or sender.get("type")
        if sender_type:
            event["sender_type"] = sender_type


def fetch_resources(specs: list[tuple[str, str, str]], owners, *, media_dir: Path, profile: str | None, downloader: Any,
                    start: int = 0):
    """Each (kind, key, name) of ``specs`` downloaded, numbered on from ``start``: (kind, key, name, its
    IncomingMedia) from the first of ``owners`` (message IDs) that has it, else the last one's failure."""
    for index, (kind, key, name) in enumerate(specs, start + 1):
        for owner in owners:
            downloaded = downloader(message_id=owner, file_key=key, kind=kind, media_dir=media_dir, profile=profile,
                                    index=index, name=name)
            if downloaded.local_path:
                break
        yield kind, key, name, downloaded
