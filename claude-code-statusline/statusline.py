#!/usr/bin/env python3
"""Claude Code Statusline entrypoint (orchestration only)."""

import json
import os
import sys

from statusline_core.api_clients import (
    fetch_api_status,
    format_api_status,
)
from statusline_core.calculations import (
    calculate_cache_ratio,
    calculate_compaction_prediction,
    calculate_context_metrics,
    calculate_cost_per_turn,
    calculate_efficiency,
    format_cache_part,
    format_duration_ms,
)
from statusline_core.display_state import DisplayState
from statusline_core.git_info import format_git_branch, get_git_branch, get_git_diff_stat
from statusline_core.layout import calculate_bar_widths
from statusline_core.output import render_compact, render_full
from statusline_core.render import (
    build_compact_line,
    build_line1_parts,
    build_line2_parts,
    build_line3_parts,
)
from claude_tui_components.utils import get_terminal_cols
from claude_tui_components.widgets import build_progress_bar, build_sparkline
from statusline_core.settings import get_setting, is_visible
from statusline_core.transcript import (
    parse_input_data,
    parse_transcript,
)
from claude_tui_components.utils import format_tokens


def main():
    compact_mode = "--compact" in sys.argv
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("statusline: no data")
        return

    basic = parse_input_data(data)
    # Context size, cost, duration, and rate-limit usage come straight from
    # the JSON Claude Code pipes in. The transcript is parsed only for the
    # analytics it doesn't expose (history, compaction, tool/turn counts).
    context_limit = basic["context_limit"]
    metrics = parse_transcript(basic["transcript_path"], context_limit=context_limit)

    ctx_used = basic["context_tokens"]
    ctx_metrics = calculate_context_metrics(
        ctx_used, context_limit, basic["used_percentage"]
    )
    ratio = ctx_metrics["ratio"]

    term_cols_padded = get_terminal_cols() - get_setting("custom", "buffer", default=30)
    bar_length, spark_width = calculate_bar_widths(term_cols_padded)
    cost = basic["cost_usd"]
    cache_pct, cache_color = calculate_cache_ratio(metrics)

    branch_part = ""
    branch = get_git_branch()
    if branch:
        branch_part = format_git_branch(branch, get_git_diff_stat())

    usage = basic["usage"]

    ds = DisplayState(
        model=basic["model"],
        session_id=basic["session_id"],
        cwd=basic["cwd"],
        bar=build_progress_bar(
            ratio, length=bar_length, threshold=ctx_metrics["compact_ratio"], pct_label="C"
        ),
        tokens_str=format_tokens(int(ctx_used)),
        limit_str=format_tokens(context_limit),
        metrics=metrics,
        usage=usage,
        compact_prediction=calculate_compaction_prediction(
            ctx_used, context_limit, metrics["turns_since_compact"],
            metrics, ratio, detailed=term_cols_padded >= 140,
        ),
        sparkline_part=build_sparkline(metrics["context_history"], width=spark_width),
        cost_str=f"${cost:.2f}" if cost >= 0.01 else "<$0.01",
        duration_str=format_duration_ms(basic["duration_ms"]),
        efficiency_part=calculate_efficiency(metrics, ctx_used),
        branch_part=branch_part,
        cache_part=format_cache_part(cache_pct, cache_color),
        cache_pct=cache_pct,
        cost_per_turn=calculate_cost_per_turn(cost, metrics["turn_count"]),
        bar_length=bar_length,
    )

    if compact_mode:
        render_compact(build_compact_line(ds))
        return

    line1_parts = build_line1_parts(ds)
    line2_parts = build_line2_parts(ds)
    if is_visible("line2", "api_status"):
        api_status_str = format_api_status(fetch_api_status(background=False))
        if api_status_str:
            line2_parts.append(api_status_str)
    line3_lines = build_line3_parts(ds)

    render_full(
        line1_parts, line2_parts, line3_lines,
        ratio, metrics, term_cols_padded,
        os.path.dirname(os.path.abspath(__file__)),
    )


if __name__ == "__main__":
    main()
