"""The embedding port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from atlas.domain.embedding import EmbeddingPurpose, EmbeddingResult


@runtime_checkable
class EmbeddingProvider(Protocol):
    """A model that turns text into vectors.

    ``model_id`` and ``dimensions`` are part of the contract because they are
    persisted alongside every stored vector. The dimension is baked into a
    PostgreSQL column type, so changing the configured model is a re-index rather
    than a configuration change — the engine refuses to start when the two
    disagree (ADR-0005).
    """

    @property
    def name(self) -> str:
        """Stable identifier for logs and metrics, for example ``openai``."""
        ...

    @property
    def model_id(self) -> str:
        """The model this instance is configured to call.

        Stored on every document so a model change is detectable rather than
        producing a corpus of silently incompatible vector spaces.
        """
        ...

    @property
    def dimensions(self) -> int:
        """Length of every vector this provider returns."""
        ...

    @property
    def max_batch_size(self) -> int:
        """Largest batch accepted in one call.

        Ingestion is embedding-dominated, so batching is the difference between a
        sync that takes minutes and one that takes hours. The caller chunks to
        this size; the provider does not silently truncate.
        """
        ...

    async def embed(
        self,
        texts: Sequence[str],
        purpose: EmbeddingPurpose = EmbeddingPurpose.DOCUMENT,
    ) -> EmbeddingResult:
        """Embed a batch of texts.

        Returns vectors positionally aligned with ``texts``.

        Raises:
            ValidationError: ``texts`` is empty or exceeds ``max_batch_size``.
            ProviderTimeoutError: The provider did not answer within its budget.
            RateLimitedError: The provider applied rate limiting.
            ProviderError: Any other provider-side failure.
        """
        ...
