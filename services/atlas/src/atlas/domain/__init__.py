"""Domain layer: entities, value objects, ports and errors.

This package performs no I/O and imports nothing else from ``atlas``. Both rules
are enforced by an ``import-linter`` contract rather than by review, so a violation
fails the build instead of surviving a code review.

Entities and ports arrive with the milestones that need them: ``ChatProvider`` and
``EmbeddingProvider`` in M3, ``VectorStore`` in M4, ``Retriever`` and
``DocumentLoader`` in M7 and M8.
"""

from atlas.domain.errors import (
    AtlasError,
    AuthorizationError,
    ConfigurationError,
    DependencyUnavailableError,
    NotFoundError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    StorageError,
    ValidationError,
)

__all__ = [
    "AtlasError",
    "AuthorizationError",
    "ConfigurationError",
    "DependencyUnavailableError",
    "NotFoundError",
    "ProviderError",
    "ProviderTimeoutError",
    "RateLimitedError",
    "StorageError",
    "ValidationError",
]
