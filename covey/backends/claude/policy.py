"""Explicit upstream safety signals only; never classify prose or model-name differences.

Read live Claude stream-json messages, including child agents. The first signal is
latched by the run, before another tool permission or completion can be processed.
"""
from __future__ import annotations

import re
import json


def http_refusal(row, payload):
    """Only an API error envelope, never normal assistant/tool text quoting an error."""
    error = payload.get('error') if payload.get('type') == 'error' else None
    if row.get('isApiErrorMessage') is True:
        blocks = payload.get('content')
        text = ''.join(b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text') if isinstance(blocks, list) else ''
        match = re.fullmatch(r'API Error: 400\s+(\{.*\})', text.strip(), re.S)
        if match:
            try:
                obj = json.loads(match[1])
                error = obj.get('error') if obj.get('type') == 'error' else None
            except (ValueError, AttributeError):
                pass
    if not isinstance(error, dict):
        return None
    details = error.get('stop_details')
    if isinstance(details, dict) and details.get('type') == 'refusal':
        return details
    if error.get('code') in {'content_filter', 'policy_refusal', 'cyber_policy', 'frontier_llm_policy'}:
        return {'type': 'refusal', 'category': error.get('category') or error['code'].removesuffix('_policy')}
    return None


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else ""


def model(value):
    return value if isinstance(value, str) and re.fullmatch(r"claude-[a-z0-9-]{1,80}", value) else ""


def policy_signal(row):
    """Allow-listed metadata from an official refusal/fallback envelope; no recursive text scan."""
    if row.get("isReplay") or row.get("is_replay"):
        return None
    kind = row.get("type")
    payload = row.get("event") if kind == "stream_event" else row.get("message") if kind == "assistant" else row
    if kind not in {"stream_event", "assistant", "result"} or not isinstance(payload, dict):
        return None
    delta = payload.get("delta") if kind == "stream_event" and payload.get("type") == "message_delta" else payload
    if not isinstance(delta, dict):
        return None
    details = delta.get("stop_details")
    details = details if isinstance(details, dict) else {}
    api_refusal = http_refusal(row, payload)
    if api_refusal:
        details = api_refusal
    refused = delta.get("stop_reason") == "refusal" or details.get("type") == "refusal"
    blocks = (payload.get("content") or []) if kind == "assistant" else []
    if kind == "stream_event" and payload.get("type") == "content_block_start":
        blocks = [payload.get("content_block")]
    fallback = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "fallback"), None) if isinstance(blocks, list) else None
    usage = payload.get("usage")
    iterations = usage.get("iterations") if isinstance(usage, dict) else None
    inherited = (isinstance(iterations, list) and any(isinstance(i, dict) and i.get("type") == "fallback_message" for i in iterations))
    if fallback is None and not refused and not inherited:
        return None
    trigger = fallback.get("trigger") if fallback else details
    trigger = trigger if isinstance(trigger, dict) else {}
    category = trigger.get("category")
    category = category if isinstance(category, str) and category in {"cyber", "frontier_llm", "bio", "reasoning_extraction", "general_harms"} else "unspecified"
    source = fallback.get("from") if fallback else None
    target = fallback.get("to") if fallback else None
    return {
        "kind": "fallback" if fallback is not None or inherited else "refusal",
        "category": category,
        "source_model": model(source.get("model")) if isinstance(source, dict) else "",
        "target_model": model(target.get("model")) if isinstance(target, dict) else "",
        "scope": "subagent" if row.get("parent_tool_use_id") is not None else "main",
        "child_id": identifier(row.get("parent_tool_use_id")),
        "response_id": identifier(payload.get("id")),
        "evidence": "fallback_block" if fallback is not None else "fallback_iteration" if inherited else "http_refusal" if api_refusal else "refusal_stop",
    }
