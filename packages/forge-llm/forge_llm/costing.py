"""Per-model USD cost estimation for LLM generations.

Prices are USD per **1M tokens** (input, output). ``estimate_cost`` matches the
model name by substring so dated API model IDs (e.g.
``claude-sonnet-4-5-20250929``) resolve to the right row. Unknown models cost
``0.0`` — callers should treat that as "unpriced", not "free".
"""

from __future__ import annotations

# (input USD/1M tokens, output USD/1M tokens)
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-1": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
}

# Match order matters: more specific keys first ("gpt-4o-mini" before "gpt-4o",
# "opus" before "sonnet" before "haiku").
_MATCH_ORDER: tuple[str, ...] = (
    "claude-opus-4-1",
    "claude-sonnet-4-5",
    "claude-haiku",
    "gpt-4o-mini",
    "gpt-4o",
)


def _match_price_key(model: str) -> str | None:
    lowered = model.lower()
    for key in _MATCH_ORDER:
        if key in lowered:
            return key
    return None


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate USD cost for a generation.

    Returns ``0.0`` for unknown/unpriced models (including ``"stub"``).
    """
    key = _match_price_key(model)
    if key is None:
        return 0.0
    price_in, price_out = PRICES_USD_PER_MTOK[key]
    return tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out
