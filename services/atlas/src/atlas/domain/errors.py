"""The error taxonomy.

Every failure Atlas raises on purpose is an :class:`AtlasError`. Anything else
reaching a request handler is a bug, and is reported as such rather than being
translated into a tidy client response.

These types carry no HTTP status codes. Mapping an error to a transport lives in
``atlas.interfaces.http.errors``, so the domain stays usable from the CLI and from
the ingestion worker, neither of which speaks HTTP.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AtlasError(Exception):
    """Base class for deliberate failures.

    Args:
        message: Human-readable description, safe to log.
        context: Structured detail for logs and telemetry. Keep secrets and
            personal data out of it: this is emitted in log records.
    """

    #: Stable machine-readable identifier, exposed to API clients. Overridden by
    #: subclasses. Treated as a public contract — renaming one is a breaking change.
    code: str = "atlas_error"

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, Any] = dict(context or {})

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigurationError(AtlasError):
    """The deployment is misconfigured. Raised at startup, never per request."""

    code = "configuration_error"


class ValidationError(AtlasError):
    """Input failed validation before any work was attempted."""

    code = "validation_error"


class NotFoundError(AtlasError):
    """A requested resource does not exist, or the caller may not know that it does."""

    code = "not_found"


class AuthorizationError(AtlasError):
    """The acting user may not access a resource.

    Raised by the authorization filter when Odoo declines a record, and whenever
    the authorization gateway itself is unusable. Both cases fail closed: an
    unreachable gateway must never degrade into unfiltered retrieval.
    """

    code = "authorization_error"


class StorageError(AtlasError):
    """The Atlas database rejected an operation or is unreachable."""

    code = "storage_error"


class DependencyUnavailableError(AtlasError):
    """A required downstream is unreachable and the request cannot proceed."""

    code = "dependency_unavailable"


class ProviderError(AtlasError):
    """A model provider returned an error.

    Args:
        message: Description, safe to log.
        provider: Provider identifier, for example ``anthropic``.
        context: Structured detail. Never put the API key in it.
    """

    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = dict(context or {})
        if provider is not None:
            merged["provider"] = provider
        super().__init__(message, context=merged)
        self.provider = provider


class ProviderTimeoutError(ProviderError):
    """A model provider did not respond within the configured budget."""

    code = "provider_timeout"


class RateLimitedError(ProviderError):
    """A model provider applied rate limiting.

    Args:
        retry_after_seconds: Value advertised by the provider, when it supplies
            one. The retry decorator in M3 prefers it over its own backoff.
    """

    code = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = dict(context or {})
        if retry_after_seconds is not None:
            merged["retry_after_seconds"] = retry_after_seconds
        super().__init__(message, provider=provider, context=merged)
        self.retry_after_seconds = retry_after_seconds
