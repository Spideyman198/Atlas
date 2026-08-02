"""Model pricing and cost estimation.

Pricing is vendor knowledge, so it lives here rather than on the domain's
:class:`~atlas.domain.usage.TokenUsage`. Costs are computed with
:class:`~decimal.Decimal` — per-token rates are small enough that binary floating
point accumulates visible error across a day of traffic.

An unpriced model raises rather than returning zero. A silent zero would make the
M12 cost dashboard quietly wrong, which is worse than a loud failure at the point
a new model is configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from atlas.domain.errors import ConfigurationError
from atlas.domain.usage import TokenUsage

_TOKENS_PER_MILLION: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Rates for one model, in US dollars per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal = Decimal(0)
    cache_write_per_mtok: Decimal = Decimal(0)

    @classmethod
    def anthropic(cls, input_rate: str, output_rate: str) -> ModelPrice:
        """Build Anthropic rates, deriving the cache multipliers.

        Anthropic prices a cache read at 0.1x the input rate and a five-minute
        cache write at 1.25x. Deriving them keeps the table to the two numbers
        that actually get published.
        """
        base = Decimal(input_rate)
        return cls(
            input_per_mtok=base,
            output_per_mtok=Decimal(output_rate),
            cache_read_per_mtok=base * Decimal("0.1"),
            cache_write_per_mtok=base * Decimal("1.25"),
        )


# Anthropic rates confirmed against the published pricing table.
#
# The OpenAI and Voyage entries are carried from documentation and are NOT yet
# verified against a live account; M3b confirms them when the adapters land. They
# are marked here rather than omitted so cost reporting has a value to use, and
# so the thing that needs checking is visible in the diff.
_PRICES: Final[dict[str, ModelPrice]] = {
    # --- Anthropic (verified) ---
    "claude-opus-5": ModelPrice.anthropic("5.00", "25.00"),
    "claude-opus-4-8": ModelPrice.anthropic("5.00", "25.00"),
    "claude-sonnet-5": ModelPrice.anthropic("3.00", "15.00"),
    "claude-haiku-4-5": ModelPrice.anthropic("1.00", "5.00"),
    # --- OpenAI (unverified against a live account; see the note above) ---
    "text-embedding-3-small": ModelPrice(Decimal("0.02"), Decimal(0)),
    "text-embedding-3-large": ModelPrice(Decimal("0.13"), Decimal(0)),
    "gpt-4o": ModelPrice(Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": ModelPrice(Decimal("0.15"), Decimal("0.60")),
    # --- Voyage (unverified against a live account) ---
    "voyage-3": ModelPrice(Decimal("0.06"), Decimal(0)),
    "voyage-3-lite": ModelPrice(Decimal("0.02"), Decimal(0)),
    # --- Test doubles: free, so a fake never contributes to a cost assertion ---
    "fake-model": ModelPrice(Decimal(0), Decimal(0)),
    "hash-embedding-v1": ModelPrice(Decimal(0), Decimal(0)),
}


def price_for(model: str) -> ModelPrice:
    """Return the rates for a model.

    Raises:
        ConfigurationError: The model has no entry. Adding a model to the
            configuration without adding its price is a deployment mistake, and
            failing here surfaces it at the first call rather than as a silently
            free line on a cost report.
    """
    try:
        return _PRICES[model]
    except KeyError:
        msg = f"no pricing configured for model {model!r}"
        raise ConfigurationError(msg, context={"model": model}) from None


def estimate_cost(model: str, usage: TokenUsage) -> Decimal:
    """Estimate the US-dollar cost of one call.

    An estimate, not an invoice: providers round and occasionally reprice, and
    fallback or retry traffic can be billed differently. Accurate enough to rank
    conversations by spend and to catch a runaway loop, which is what M12 needs.
    """
    price = price_for(model)
    return (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
        + Decimal(usage.cache_read_input_tokens) * price.cache_read_per_mtok
        + Decimal(usage.cache_write_input_tokens) * price.cache_write_per_mtok
    ) / _TOKENS_PER_MILLION


def known_models() -> frozenset[str]:
    """Every model with a configured price."""
    return frozenset(_PRICES)
