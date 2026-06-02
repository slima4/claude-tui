"""Shared terminal utilities."""

import os
import re

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

def visible_len(s):
    """Length of string after stripping ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))

def truncate(s, max_cols):
    """Truncate an ANSI-colored string to max_cols characters."""
    visible = 0
    i = 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            i = m.end()
            continue
        visible += 1
        if visible > max_cols:
            return s[:i] + "\033[0m"
        i += 1
    return s

def visual_rows(lines, term_width):
    """Count actual terminal rows, accounting for line wrapping."""
    rows = 0
    for line in lines:
        vlen = visible_len(line)
        rows += max(1, -(-vlen // term_width))  # ceil division, min 1
    return rows

def format_tokens(n):
    """Format token count as compact string (e.g. 150k, 1.2M)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def get_terminal_cols():
    """Get terminal width from the COLUMNS env var Claude Code sets.

    Claude Code captures the status line's stdout, so tput/ioctl/shutil
    width detection from inside the script cannot see the real terminal.
    Claude Code exports COLUMNS (and LINES) to the script instead.
    """
    try:
        cols = int(os.environ.get("COLUMNS", ""))
        if cols > 0:
            return cols
    except (TypeError, ValueError):
        pass
    return 80
