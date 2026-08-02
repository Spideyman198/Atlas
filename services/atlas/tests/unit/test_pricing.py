"""Tests for cost estimation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from atlas.domain.errors import ConfigurationError
from atlas.domain.usage import TokenUsage
from atlas.infrastructure.providers.pricing import (
    ModelPrice,
    estimate_cost,
    known_models,
    price_for,
)

pytestmark = pytest.mark.unit


def test_cost_is_rated_per_million_tokens() -> None:
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)

    # claude-opus-5 is $5 in / $25 out per million.
    assert estimate_cost("claude-opus-5", usage) == Decimal(30)


def test_cache_reads_are_cheaper_than_uncached_input() -> None:
    cached = TokenUsage(cache_read_input_tokens=1_000_000)
    uncached = TokenUsage(input_tokens=1_000_000)

    assert estimate_cost("claude-opus-5", cached) < estimate_cost("claude-opus-5", uncached)


def test_cache_writes_cost_a_premium_over_uncached_input() -> None:
    written = TokenUsage(cache_write_input_tokens=1_000_000)
    uncached = TokenUsage(input_tokens=1_000_000)

    assert estimate_cost("claude-opus-5", written) > estimate_cost("claude-opus-5", uncached)


def test_anthropic_cache_multipliers_are_derived_from_the_input_rate() -> None:
    price = price_for("claude-sonnet-5")

    assert price.cache_read_per_mtok == price.input_per_mtok * Decimal("0.1")
    assert price.cache_write_per_mtok == price.input_per_mtok * Decimal("1.25")


def test_zero_usage_costs_nothing() -> None:
    assert estimate_cost("claude-opus-5", TokenUsage()) == Decimal(0)


def test_an_unpriced_model_raises_rather_than_reporting_zero() -> None:
    """A silent zero would make the cost dashboard quietly wrong."""
    with pytest.raises(ConfigurationError, match="no pricing configured"):
        estimate_cost("some-unconfigured-model", TokenUsage(input_tokens=1000))


def test_test_doubles_are_priced_at_zero() -> None:
    """So a fake never contributes to a cost assertion."""
    assert estimate_cost("fake-model", TokenUsage(input_tokens=10_000)) == Decimal(0)


@pytest.mark.parametrize("model", sorted(known_models()))
def test_every_configured_price_is_non_negative(model: str) -> None:
    price = price_for(model)

    assert price.input_per_mtok >= 0
    assert price.output_per_mtok >= 0
    assert price.cache_read_per_mtok >= 0
    assert price.cache_write_per_mtok >= 0


def test_costs_use_decimal_arithmetic() -> None:
    """Float accumulation drifts visibly across a day of traffic."""
    price = ModelPrice(Decimal("0.10"), Decimal("0.20"))

    assert isinstance(price.input_per_mtok, Decimal)
    assert isinstance(estimate_cost("claude-opus-5", TokenUsage(input_tokens=3)), Decimal)
