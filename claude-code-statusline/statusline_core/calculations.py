"""Metric calculations and display formatting derived from transcript data."""

import os
from typing import TypedDict

from .constants import (
    COMPACT_BUFFER,
    GRAY,
    GREEN,
    ORANGE,
    RED,
    RESET,
    YELLOW,
)
from .settings import is_visible

from claude_tui_components.utils import format_tokens


class ContextMetrics(TypedDict):
    ratio: float
    compact_ratio: float


def format_cost(cost):
    return "<$0.01" if cost < 0.01 else f"${cost:.2f}"


def format_duration_ms(duration_ms):
    """Format Claude Code's cost.total_duration_ms into a compact label."""
    try:
        total_minutes = max(0, int(duration_ms)) // 60000
    except (TypeError, ValueError):
        return "0m"
    if total_minutes < 60:
        return f"{total_minutes}m"
    return f"{total_minutes // 60}h {total_minutes % 60:02d}m"


def calculate_context_metrics(
    ctx_used: int, context_limit: int, used_percentage: float | None = None
) -> ContextMetrics:
    # Prefer Claude's pre-calculated input-only percentage so the bar matches
    # /context; fall back to tokens/limit when it's absent (early session).
    if used_percentage is not None:
        ratio = used_percentage / 100
    elif context_limit > 0:
        ratio = ctx_used / context_limit
    else:
        ratio = 0
    compact_ratio = (context_limit - COMPACT_BUFFER) / context_limit if context_limit > 0 else 0.83
    return {"ratio": ratio, "compact_ratio": compact_ratio}


def calculate_cache_ratio(metrics: dict):
    total_input = metrics["input_tokens_total"] + metrics["cache_read_tokens_total"]
    if total_input <= 0:
        return 0, GRAY
    cache_pct = int(metrics["cache_read_tokens_total"] / total_input * 100)
    if cache_pct >= 70:
        return cache_pct, GREEN
    if cache_pct >= 40:
        return cache_pct, YELLOW
    return cache_pct, ORANGE


def format_cache_part(cache_pct, cache_color):
    return f"{cache_color}{cache_pct}%{RESET} cache" if cache_pct > 0 else f"{GRAY}0%{RESET} cache"


def calculate_cost_per_turn(cost: float, turn_count: int) -> str:
    if turn_count > 0:
        return f"{GRAY}~{format_cost(cost / turn_count)}/turn{RESET}"
    return ""


def format_context_trend(metrics: dict) -> str:
    """Readable context growth trend (tokens per turn)."""
    points = [ctx for _, ctx in metrics.get("context_per_turn", [])]
    if len(points) < 2:
        return ""
    deltas = [points[i] - points[i - 1] for i in range(1, len(points))]
    recent = deltas[-4:]
    if not recent:
        return ""
    avg_delta = sum(recent) / len(recent)
    magnitude = format_tokens(abs(int(avg_delta)))
    if avg_delta > 1200:
        color = ORANGE if avg_delta > 5000 else YELLOW
        return f"{color}CTX ↑{magnitude}{RESET}{GRAY}/t{RESET}"
    if avg_delta < -1200:
        return f"{GREEN}CTX ↓{magnitude}{RESET}{GRAY}/t{RESET}"
    return f"{GRAY}CTX →{magnitude}{RESET}{GRAY}/t{RESET}"


def _get_compact_ceiling(context_limit: int) -> float:
    compact_ceiling = context_limit - COMPACT_BUFFER
    env_pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "")
    if env_pct.isdigit() and 1 <= int(env_pct) <= 100:
        compact_ceiling = min(compact_ceiling, context_limit * int(env_pct) / 100)
    return compact_ceiling


def _estimate_context_growth_per_turn(turn_contexts: list[int], ctx_used: int, turns_since: int, baseline: int) -> float:
    if len(turn_contexts) >= 3:
        deltas = [
            turn_contexts[i] - turn_contexts[i - 1]
            for i in range(1, len(turn_contexts))
            if turn_contexts[i] > turn_contexts[i - 1]
        ]
        if not deltas:
            return 0
        alpha = 2 / (min(len(deltas), 5) + 1)
        ema = deltas[0]
        for d in deltas[1:]:
            ema = alpha * d + (1 - alpha) * ema
        return ema
    growth_since = ctx_used - baseline if baseline > 0 else ctx_used
    return growth_since / max(turns_since, 1)


def _prediction_color(turns_left: int) -> str:
    if turns_left <= 5:
        return RED
    if turns_left <= 15:
        return ORANGE
    if turns_left <= 30:
        return YELLOW
    return GREEN


def _format_turns_left(turns_left: int) -> str:
    """Compact turns-left formatter for cleaner statusline output."""
    if turns_left >= 1_000_000:
        return f"{turns_left / 1_000_000:.1f}M"
    if turns_left >= 1_000:
        return f"{turns_left / 1_000:.1f}k"
    return str(turns_left)


def calculate_compaction_prediction(
    ctx_used: int,
    context_limit: int,
    turns_since: int,
    metrics: dict,
    ratio: float,
    detailed: bool = True,
) -> str:
    if turns_since < 2 or ratio <= 0 or ratio >= 1.0:
        return ""
    remaining_tokens = _get_compact_ceiling(context_limit) - ctx_used
    growth_per_turn = _estimate_context_growth_per_turn(
        [ctx for _, ctx in metrics["context_per_turn"]],
        ctx_used,
        turns_since,
        metrics["context_at_last_compact"],
    )
    if growth_per_turn <= 0 or remaining_tokens <= 0:
        return ""
    turns_left = int(remaining_tokens / growth_per_turn)
    turns_str = _format_turns_left(turns_left)
    if detailed:
        return f"{_prediction_color(turns_left)}ETA {turns_str}{RESET} {GRAY}turns{RESET}"
    return f"{_prediction_color(turns_left)}ETA {turns_str}{RESET}"


def calculate_efficiency(metrics: dict, ctx_used: int) -> str:
    total_built = metrics["total_context_built"] + ctx_used
    if total_built <= 0 or not is_visible("line1", "efficiency"):
        return ""
    wasted = metrics["tokens_wasted"]
    eff_pct = int((max(0, 1 - wasted / total_built) if wasted > 0 else 1.0) * 100)
    if eff_pct >= 90:
        color = GREEN
    elif eff_pct >= 70:
        color = YELLOW
    elif eff_pct >= 50:
        color = ORANGE
    else:
        color = RED
    return f"{color}{eff_pct}%{RESET} {GRAY}eff{RESET}"
