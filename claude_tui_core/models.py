"""
Single source of truth for all Anthropic model domain data.
Owns: MODEL_PRICING, MODEL_CONTEXT_WINDOW, COMPACT_BUFFER, get_context_limit(), get_model_pricing()
"""

# Context window sizes by model family.
# Substring-matched against the model ID (first match wins), so order the more
# specific keys before broader ones. Models absent here fall back to
# DEFAULT_CONTEXT_LIMIT (200k) — that covers Haiku and older/legacy Sonnet.
MODEL_CONTEXT_WINDOW = {
    "claude-opus-4": 1_000_000,   # Opus 4.5–4.8
    "claude-fable-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-mythos": 1_000_000,   # Mythos preview / Mythos 5
}
DEFAULT_CONTEXT_LIMIT = 200_000
COMPACT_BUFFER = 33_000

# Pricing per million tokens (input, cache_read, cache_write, output).
# Convention: cache_read = 0.1x input, cache_write (5-min) = 1.25x input.
# Keys are substring-matched against the model ID (get_model_pricing); each key
# is a full, distinct model ID so match order is unambiguous.
# Note: Sonnet 5 has an introductory rate ($2/$10 through 2026-08-31); the
# durable standard rate ($3/$15) is used here so the figure stays correct after
# the intro window closes.
MODEL_PRICING = {
    "claude-fable-5": {
        "input": 10.0,
        "cache_read": 1.0,
        "cache_write": 12.5,
        "output": 50.0,
    },
    "claude-mythos": {   # Mythos preview / Mythos 5 — Fable-tier pricing
        "input": 10.0,
        "cache_read": 1.0,
        "cache_write": 12.5,
        "output": 50.0,
    },
    "claude-opus-4-8": {
        "input": 5.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
        "output": 25.0,
    },
    "claude-opus-4-7": {
        "input": 5.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
        "output": 25.0,
    },
    "claude-opus-4-6": {
        "input": 5.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
        "output": 25.0,
    },
    "claude-sonnet-5": {
        "input": 3.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
        "output": 15.0,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
        "output": 15.0,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "cache_read": 0.10,
        "cache_write": 1.25,
        "output": 5.0,
    },
    "claude-sonnet-3-5": {
        "input": 3.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
        "output": 15.0,
    },
    "claude-haiku-3-5": {
        "input": 0.80,
        "cache_read": 0.08,
        "cache_write": 1.0,
        "output": 4.0,
    },
}

DEFAULT_MODEL_PRICING_KEY = "claude-sonnet-4-6"

# Deterministic aliases for abbreviated model keys used by sniffer logs.
# Keys are normalized (lowercase alnum only).
FUZZY_PRICING_ALIASES = {
    "claudeopus46": "claude-opus-4-6",
    "claudeopus4": "claude-opus-4-6",
    "opus46": "claude-opus-4-6",
    "opus4": "claude-opus-4-6",
    "opus": "claude-opus-4-6",
    "claudesonnet46": "claude-sonnet-4-6",
    "claudesonnet4": "claude-sonnet-4-6",
    "sonnet46": "claude-sonnet-4-6",
    "sonnet4": "claude-sonnet-4-6",
    "claudesonnet35": "claude-sonnet-3-5",
    "claudesonnet3": "claude-sonnet-3-5",
    "sonnet35": "claude-sonnet-3-5",
    "sonnet3": "claude-sonnet-3-5",
    "sonnet": "claude-sonnet-4-6",
    "claudehaiku45": "claude-haiku-4-5",
    "claudehaiku4": "claude-haiku-4-5",
    "haiku45": "claude-haiku-4-5",
    "haiku4": "claude-haiku-4-5",
    "claudehaiku35": "claude-haiku-3-5",
    "claudehaiku3": "claude-haiku-3-5",
    "haiku35": "claude-haiku-3-5",
    "haiku3": "claude-haiku-3-5",
    "haiku": "claude-haiku-4-5",
}


def _normalize_model_id(model_id: str) -> str:
    """Normalize model IDs for robust fuzzy matching."""
    return "".join(ch for ch in model_id.lower() if ch.isalnum())


def get_context_limit(model_id: str) -> int:
    """Resolve context window for any model ID string."""
    for key, limit in MODEL_CONTEXT_WINDOW.items():
        if key in model_id:
            return limit
    return DEFAULT_CONTEXT_LIMIT


def get_model_pricing(model_id: str) -> dict:
    """Resolve pricing dictionary for a model ID. Falls back to Sonnet 4.6."""
    for key, pricing in MODEL_PRICING.items():
        if key in model_id:
            return pricing
    return MODEL_PRICING[DEFAULT_MODEL_PRICING_KEY]


def get_model_pricing_fuzzy(model_id: str) -> dict:
    """
    Fuzzy pricing lookup for abbreviated model keys (used by sniffer).
    Handles 'claude-sonnet-4' -> 'claude-sonnet-4-6', etc.
    """
    if not model_id:
        return MODEL_PRICING[DEFAULT_MODEL_PRICING_KEY]

    m = _normalize_model_id(model_id)
    if not m:
        return MODEL_PRICING[DEFAULT_MODEL_PRICING_KEY]

    # Canonical model key found within a longer model identifier.
    for key, pricing in MODEL_PRICING.items():
        if _normalize_model_id(key) in m:
            return pricing

    # Deterministic shorthand aliases (e.g. "opus", "claude-sonnet-3").
    for alias, canonical in FUZZY_PRICING_ALIASES.items():
        if alias in m:
            return MODEL_PRICING[canonical]

    return MODEL_PRICING[DEFAULT_MODEL_PRICING_KEY]
