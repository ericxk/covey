"""下载请求与结果: a button at the bottom of an ended task card that packs the turn's model requests and the
model's responses, as the Harness recorded them, for the owner to debug with.

The click (Bridge only: ``bot_only``) marks the job record ``turn_export: building`` and greys the button;
a thread of the Bridge asks the backend for the turn (``export_turn``, capability ``turn_export``: Claude
Code's session records, backends/claude/export.py; Codex's rollouts,
backends/codex/export.py), adds the task's own facts, writes one JSON file compressed with xz (requests share
most of their messages, which xz folds: a 117.5 MB session became 0.17 MB, gzip 35 MB), uploads it to the
bot's cloud space (today's folder under the 产物 root of app/artifacts.py, Artifacts.in_day_folder) and records
``done`` with the link, or ``failed`` with the reason. Each step redraws the element, whose button
then opens the file. The JSON is written as it is encoded, so a large turn does not sit in memory twice.
"""
from __future__ import annotations

import json
import lzma
import threading
import time

from covey.app.artifacts import drive_upload, private_folder
from covey.app.card_actions import action_value
from covey.cards.turn_export import export_panel
from covey.core.harness import backend_capabilities
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.topics import topic_label

PRESET = 6  # xz's 8 MiB window holds a request or two: the next one's repeated messages fold into it.
CHUNK = 1 << 20


def write_xz(data, path):
    """``data`` as compact UTF-8 JSON in an xz file at ``path``; the JSON's size in bytes."""
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    size, parts, pending = 0, [], 0
    with lzma.open(path, "wb", preset=PRESET) as out:
        for piece in encoder.iterencode(data):
            parts.append(piece)
            pending += len(piece)
            if pending >= CHUNK:
                block = "".join(parts).encode()
                out.write(block)
                size += len(block)
                parts, pending = [], 0
        block = "".join(parts).encode()
        out.write(block)
        size += len(block)
    return size


def turn_window(record):
    """The turn's (start, end) in epoch seconds: from its backend request to its end (now while unknown)."""
    timings = record.get("timings") if isinstance(record.get("timings"), dict) else {}
    start = timings.get("backend_requested") or timings.get("backend_started") or record.get("created_at")
    end = timings.get("execution_finished") or time.time()
    if not isinstance(start, (int, float)):
        raise ValueError("这一轮没有开始执行的时间，无法确定导出范围")
    return float(start), float(end)


class TurnExport:
    """One Harness's 下载请求与结果.

    - ``harness``: its backend (``export_turn``) and job records.
    - ``runtime``: the log.
    - ``cards``: the element on the task card.
    - ``artifacts``: the cloud space folder.
    """

    def __init__(self, harness, *, runtime, cards, artifacts) -> None:
        self.harness = harness
        self.runtime = runtime
        self.cards = cards
        self.artifacts = artifacts
        self.running = set()  # Job keys this process is exporting; a 'building' left by another is stale.
        self.lock = threading.Lock()

    def supported(self, ctx):
        return backend_capabilities(self.harness.backend, ctx).turn_export

    def attach_export_controls(self, ctx, job):
        if (not job.progress or job.export_controls_attached or not ctx.topic_id or not job.session_id
                or not self.supported(ctx)):
            return
        job.export_controls_attached = bool(self.cards.set_task_element(
            ctx, job, export_panel(ctx.topic_id, job.job_id), at_bottom=True))

    def export_action(self, ctx, event):
        raw = action_value(event)
        if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
            return True
        chat = str(event.get("chat_id") or "")
        record = self.harness.topic_record(ctx, chat, int(raw.get("job_id") or 0))
        if not record:
            raise ValueError("当前话题找不到这一轮任务")
        if not record.get("session_id") or not self.supported(ctx):
            raise ValueError("这一轮没有可导出的模型请求")
        key = record["key"]
        with self.lock:
            busy = key in self.running
            if not busy:
                self.running.add(key)
        if busy:
            self.redraw(ctx, chat, record, event)
            return True
        record = upsert_job_record(ctx.state_dir, key, turn_export={"status": "building", "at": time.time()})
        try:
            self.redraw(ctx, chat, record, event)
        except ValueError as exc:  # The finished element still shows: the line only says it is under way.
            self.runtime.log(f"turn export card not updated: {exc}")
        finally:
            threading.Thread(target=self.build, args=(ctx, chat, key), daemon=True, name="turn-export").start()
        return True

    def build(self, ctx, chat, key):
        """Pack, upload and record the turn; never raises."""
        try:
            record = find_job_record(ctx.state_dir, key) or {}
            try:
                state = self.package(ctx, record)
            except Exception as exc:  # noqa: BLE001 - every failure goes on the card
                self.runtime.log(f"turn export failed job=#{record.get('job_id')}: {type(exc).__name__}")
                reason = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}"
                state = {"status": "failed", "at": time.time(), "error": reason}
            record = upsert_job_record(ctx.state_dir, key, turn_export=state)
            try:
                self.redraw(ctx, chat, record, {"message_id": record.get("card_message_id", "")})
            except ValueError as exc:
                self.runtime.log(f"turn export card not updated: {exc}")
        finally:
            with self.lock:
                self.running.discard(key)

    def package(self, ctx, record):
        since, until = turn_window(record)
        data = self.harness.backend.export_turn(ctx, record["session_id"], since, until)
        label = topic_label(ctx.state_dir, ctx.topic_id) or "话题"
        data["task"] = {key: record.get(key) for key in (
            "job_id", "key", "topic_id", "harness_id", "model", "effort", "status", "execution_status",
            "session_id", "workdir", "timings", "input_tokens", "output_tokens", "context_tokens", "context_window")}
        data["task"]["topic"] = label
        name = f"{label}-#{record['job_id']}-请求与结果-{time.strftime('%Y%m%d-%H%M%S')}.json.xz"
        with private_folder(ctx.state_dir, "turn-exports") as folder:
            path = folder / name
            raw_size = write_xz(data, path)
            size = path.stat().st_size
            uploaded = self.artifacts.in_day_folder(
                ctx, lambda into: drive_upload(ctx.profile, path, into, log=self.runtime.log))
        main = data.get("main") or {}
        requests = len(main.get("requests") or []) + sum(len(s.get("requests") or []) for s in data.get("subagents") or [])
        verified = sum(1 for part in [main, *(data.get("subagents") or [])] for r in part.get("requests") or []
                       if r.get("digest_verified"))
        self.runtime.log(f"turn export uploaded job=#{record['job_id']} requests={requests} verified={verified} "
                         f"bytes={size} raw={raw_size}")
        return {"status": "done", "at": time.time(), "name": uploaded.get("file_name") or name, "url": uploaded["url"],
                "token": uploaded.get("file_token", ""), "size": size, "raw_size": raw_size, "requests": requests,
                "verified": verified, "subagents": len(data.get("subagents") or [])}

    def redraw(self, ctx, chat, record, event):
        panel = export_panel(ctx.topic_id, record["job_id"], record.get("turn_export"))
        token = f"export-{record['key']}-{(record.get('turn_export') or {}).get('status', '')}-{time.time_ns()}"
        self.cards.update_record_panel(ctx, chat, token, event, record, panel)

