"""LLM cost accounting.

Prices are list prices per million tokens (input, output) in USD. On a free tier the bill is $0,
but budgets still use list prices: they bound how much work a job may do, and they show what the
same workload would cost on a paid plan. Override with LLM_PRICE_INPUT_PER_MTOK /
LLM_PRICE_OUTPUT_PER_MTOK when the provider's prices change (check the provider's pricing page).
"""

from __future__ import annotations

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
}
# Unknown models are priced conservatively so a budget still bounds them.
FALLBACK_PER_MTOK = (1.00, 3.00)


def price_for(model: str, override: tuple[float | None, float | None] = (None, None)) -> tuple[float, float]:
    base = PRICES_PER_MTOK.get(model, FALLBACK_PER_MTOK)
    return (
        override[0] if override[0] is not None else base[0],
        override[1] if override[1] is not None else base[1],
    )


def cost_usd(tokens_in: int, tokens_out: int, prices: tuple[float, float]) -> float:
    return (tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000
