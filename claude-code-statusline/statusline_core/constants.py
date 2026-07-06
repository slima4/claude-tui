"""Shared constants for statusline modules."""

# Base paths / protocol constants
CLAUDE_DIR = ".claude"
APPLICATION_JSON = "application/json"
UTC_OFFSET = "+00:00"

# Model metadata from core
from claude_tui_core.models import (
    MODEL_CONTEXT_WINDOW,
    DEFAULT_CONTEXT_LIMIT,
    COMPACT_BUFFER,
    MODEL_PRICING,
    get_context_limit,
)

# ANSI colors
from claude_tui_components.colors import (
    RESET, BOLD, GREEN, YELLOW, ORANGE, RED, CYAN, MAGENTA, WHITE, GRAY
)
