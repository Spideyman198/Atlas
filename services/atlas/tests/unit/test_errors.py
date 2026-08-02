"""Tests for the error taxonomy and its HTTP mapping."""

from __future__ import annotations

import pytest

from atlas.domain.errors import (
    AtlasError,
    AuthorizationError,
    NotFoundError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    ValidationError,
)
from atlas.interfaces.http.errors import status_for

pytestmark = pytest.mark.unit


def test_context_is_copied_so_callers_cannot_mutate_it_afterwards() -> None:
    context = {"model": "sale.order"}
    error = AtlasError("boom", context=context)

    context["model"] = "mutated"

    assert error.context == {"model": "sale.order"}


def test_provider_is_folded_into_context_for_structured_logging() -> None:
    error = ProviderError("upstream refused", provider="anthropic")

    assert error.provider == "anthropic"
    assert error.context["provider"] == "anthropic"


def test_rate_limit_records_the_providers_own_retry_hint() -> None:
    error = RateLimitedError("slow down", provider="openai", retry_after_seconds=2.5)

    assert error.retry_after_seconds == 2.5
    assert error.context == {"provider": "openai", "retry_after_seconds": 2.5}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValidationError("bad input"), 422),
        (NotFoundError("no such record"), 404),
        (AuthorizationError("denied"), 403),
        (RateLimitedError("slow down"), 429),
        (ProviderTimeoutError("timed out"), 504),
        (ProviderError("upstream refused"), 502),
    ],
)
def test_errors_map_to_their_http_status(error: AtlasError, expected: int) -> None:
    assert status_for(error) == expected


def test_an_unmapped_subclass_inherits_the_status_of_its_base() -> None:
    """A new error type gets a sensible status without editing the mapping table."""

    class VendorSpecificProviderError(ProviderError):
        code = "vendor_specific"

    assert status_for(VendorSpecificProviderError("boom")) == 502


def test_an_unmapped_error_defaults_to_500() -> None:
    class SomethingNewError(AtlasError):
        code = "something_new"

    assert status_for(SomethingNewError("boom")) == 500


def test_codes_are_unique_across_the_taxonomy() -> None:
    """Codes are a public contract, so a duplicate would be an ambiguous response."""

    def descendants(klass: type[AtlasError]) -> list[type[AtlasError]]:
        found = [klass]
        for child in klass.__subclasses__():
            found.extend(descendants(child))
        return found

    # Subclasses defined inside other tests may be present; dedupe by class.
    codes = [k.code for k in dict.fromkeys(descendants(AtlasError))]

    assert len(codes) == len(set(codes)), f"duplicate error codes: {codes}"
