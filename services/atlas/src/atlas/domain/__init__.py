"""Domain layer: entities, value objects, ports and errors.

This package performs no I/O and imports nothing else from ``atlas``. Both rules
are enforced by an ``import-linter`` contract rather than by review, so a violation
fails the build instead of surviving a code review.

Entities and ports arrive with the milestones that need them: ``ChatProvider`` and
``EmbeddingProvider`` in M3, ``VectorStore`` in M4, ``Retriever`` and
``DocumentLoader`` in M7 and M8.
"""

from atlas.domain.chat import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    Effort,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from atlas.domain.embedding import EmbeddingPurpose, EmbeddingResult, Vector
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
from atlas.domain.ports import ChatProvider, EmbeddingProvider
from atlas.domain.usage import TokenUsage

__all__ = [
    "AtlasError",
    "AuthorizationError",
    "ChatChunk",
    "ChatProvider",
    "ChatRequest",
    "ChatResponse",
    "ConfigurationError",
    "DependencyUnavailableError",
    "Effort",
    "EmbeddingProvider",
    "EmbeddingPurpose",
    "EmbeddingResult",
    "Message",
    "NotFoundError",
    "ProviderError",
    "ProviderTimeoutError",
    "RateLimitedError",
    "Role",
    "StopReason",
    "StorageError",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "ValidationError",
    "Vector",
]
