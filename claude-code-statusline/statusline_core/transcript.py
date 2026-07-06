"""Transcript JSONL parsing — reads Claude Code session files into metrics."""

import json
import os
from datetime import datetime, timezone
from typing import TypedDict

from .constants import DEFAULT_CONTEXT_LIMIT, get_context_limit
from .debug import debug_log


class InputData(TypedDict):
    model: str
    cwd: str
    transcript_path: str
    session_id: str
    context_tokens: int
    context_limit: int
    used_percentage: float | None
    cost_usd: float
    duration_ms: int
    usage: dict | None


def _build_usage_from_rate_limits(rate_limits: dict) -> dict | None:
    """Map Claude Code's stdin `rate_limits` into the usage-bar shape.

    Claude Code provides `used_percentage` (0-100) and `resets_at`
    (Unix epoch seconds); the bar formatters want `utilization` and an
    ISO `resets_at`. Only present for Pro/Max after the first API response.
    """
    if not isinstance(rate_limits, dict):
        return None
    out = {}
    for key in ("five_hour", "seven_day"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("used_percentage")
        if pct is None:
            continue
        resets_iso = ""
        resets_at = window.get("resets_at")
        if isinstance(resets_at, (int, float)):
            resets_iso = datetime.fromtimestamp(resets_at, tz=timezone.utc).isoformat()
        out[key] = {"utilization": pct, "resets_at": resets_iso}
    return out or None


def parse_input_data(data: dict) -> InputData:
    ctx = data.get("context_window", {}) or {}
    cost = data.get("cost", {}) or {}
    model = data.get("model", {}) or {}
    return {
        "model": model.get("display_name", "unknown"),
        "cwd": os.path.basename(data.get("workspace", {}).get("current_dir", "")),
        "transcript_path": data.get("transcript_path", ""),
        "session_id": data.get("session_id", "")[:8],
        # context_window.total_input_tokens already = input + cache_creation +
        # cache_read (output excluded), matching used_percentage's formula.
        "context_tokens": ctx.get("total_input_tokens") or 0,
        # Prefer Claude Code's own window size; when it's absent (early session,
        # or a model this Claude Code build doesn't report a size for) resolve
        # from the model ID so 1M models don't fall back to the 200k default.
        "context_limit": ctx.get("context_window_size") or get_context_limit(model.get("id", "")),
        # Claude's own input-only percentage; None early in a session and
        # right after /compact, so callers fall back to tokens/limit.
        "used_percentage": ctx.get("used_percentage"),
        "cost_usd": cost.get("total_cost_usd") or 0.0,
        "duration_ms": cost.get("total_duration_ms") or 0,
        "usage": _build_usage_from_rate_limits(data.get("rate_limits", {})),
    }


def _new_metrics():
    return {
        "input_tokens_total": 0,
        "cache_read_tokens_total": 0,
        "cache_creation_tokens_total": 0,
        "output_tokens_total": 0,
        "compact_count": 0,
        "files_touched": set(),
        "tool_calls": 0,
        "tool_errors": 0,
        "subagent_count": 0,
        "turn_count": 0,
        "thinking_count": 0,
        "context_history": [],
        "context_per_turn": [],
        "tokens_wasted": 0,
        "total_context_built": 0,
        "recent_tools": [],
        "current_turn_file_edits": {},
        "turns_since_compact": 0,
        "context_at_last_compact": 0,
        "_pre_compact_ctx": 0,
    }


def _is_compaction_entry(obj):
    return obj.get("type") == "summary" or (
        obj.get("type") == "system" and obj.get("subtype") == "compact_boundary"
    )


def _iter_json_objects(lines):
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _update_turn_state(obj, result):
    if obj.get("type") != "user" or "message" not in obj:
        return
    content = obj["message"].get("content", [])
    has_text = False
    if isinstance(content, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    elif isinstance(content, str) and content.strip():
        has_text = True
    if has_text:
        result["turn_count"] += 1
        result["turns_since_compact"] += 1
        result["recent_tools"] = []
        result["current_turn_file_edits"] = {}


def _update_usage_metrics(obj, result, context_limit):
    if obj.get("type") != "assistant" or "message" not in obj or "usage" not in obj["message"]:
        return
    usage = obj["message"]["usage"]
    result["input_tokens_total"] += usage.get("input_tokens", 0)
    result["cache_read_tokens_total"] += usage.get("cache_read_input_tokens", 0)
    result["cache_creation_tokens_total"] += usage.get("cache_creation_input_tokens", 0)
    result["output_tokens_total"] += usage.get("output_tokens", 0)
    keys_ctx = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"]
    ctx_snapshot = sum(usage.get(k, 0) for k in keys_ctx)
    result["_pre_compact_ctx"] = ctx_snapshot
    if result["context_at_last_compact"] == -1:
        result["context_at_last_compact"] = ctx_snapshot
        if result["compact_count"] > 0 and result.get("_ctx_before_compact", 0) > 0:
            pre = result["_ctx_before_compact"]
            cache_r = usage.get("cache_read_input_tokens", 0)
            headroom = max(0, context_limit - pre)
            summary = max(0, ctx_snapshot - cache_r)
            result["tokens_wasted"] += headroom + summary
    turn = result["turn_count"]
    if result["context_per_turn"] and result["context_per_turn"][-1][0] == turn:
        result["context_per_turn"][-1] = (turn, ctx_snapshot)
    else:
        result["context_per_turn"].append((turn, ctx_snapshot))
    out_tok = usage.get("output_tokens", 0)
    if out_tok > 0:
        result["context_history"].append(out_tok)


def _apply_compaction_state(obj, result, context_limit):
    if not _is_compaction_entry(obj):
        return
    result["compact_count"] += 1
    result["context_history"].append(None)
    result["_ctx_before_compact"] = result["_pre_compact_ctx"]
    result["total_context_built"] += context_limit
    result["turns_since_compact"] = 0
    result["context_at_last_compact"] = -1
    result["context_per_turn"] = []


def _extract_file_path(inp):
    """Extract the first string file path from tool input."""
    for key in ("file_path", "path"):
        val = inp.get(key)
        if isinstance(val, str):
            return val
    return None


def _record_tool_use(block, result, active_subagents):
    result["tool_calls"] += 1
    inp = block.get("input", {})
    tool_name = block.get("name", "")
    file_path = _extract_file_path(inp)

    if file_path:
        result["recent_tools"].append(f"{tool_name} {os.path.basename(file_path)}")
        result["files_touched"].add(file_path)
        if tool_name in ("Edit", "Write", "MultiEdit"):
            fname = os.path.basename(file_path)
            result["current_turn_file_edits"][fname] = result["current_turn_file_edits"].get(fname, 0) + 1
    else:
        cmd = inp.get("command", "")
        short = cmd.split()[0] if cmd else ""
        result["recent_tools"].append(f"{tool_name} {short}".strip())

    if tool_name in ("Task", "Agent"):
        task_id = block.get("id", "")
        if task_id:
            active_subagents.add(task_id)


def _update_tool_activity(obj, result, active_subagents):
    if obj.get("type") != "assistant" or "message" not in obj:
        return
    content = obj["message"].get("content", [])
    has_thinking = False
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                has_thinking = True
            if block.get("type") == "tool_use":
                _record_tool_use(block, result, active_subagents)
    if has_thinking:
        result["thinking_count"] += 1


def _update_tool_errors(obj, result):
    if obj.get("type") != "user" or "message" not in obj:
        return
    content = obj["message"].get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                result["tool_errors"] += 1


def parse_transcript(transcript_path, context_limit=None):
    if context_limit is None:
        context_limit = DEFAULT_CONTEXT_LIMIT
    result = _new_metrics()
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        debug_log(f"parse_transcript could not read: {transcript_path}")
        return result

    active_subagents = set()
    for obj in _iter_json_objects(lines):
        _update_turn_state(obj, result)
        _update_usage_metrics(obj, result, context_limit)
        _apply_compaction_state(obj, result, context_limit)
        _update_tool_activity(obj, result, active_subagents)
        _update_tool_errors(obj, result)

    result["subagent_count"] = len(active_subagents)
    return result
