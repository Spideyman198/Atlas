"""Voyage embedding adapter.

The alternative to OpenAI embeddings for quality-first deployments, and the one
that lets a Claude-only shop source both capabilities from a coherent vendor pair
(ADR-0005).

Unlike OpenAI, Voyage embeds a document and a query differently. That is why
:class:`~atlas.domain.embedding.EmbeddingPurpose` is on the port at all — using
the wrong side of the pair measurably degrades recall, and the choice has to be
expressible before the adapter that needs it exists.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

import voyageai

from atlas.domain.embedding import EmbeddingPurpose, EmbeddingResult, Vector
from atlas.domain.errors import ProviderError, ProviderTimeoutError, ValidationError
from atlas.domain.usage import TokenUsage

logger = logging.getLogger(__name__)

DEFAULT_MODEL: Final = "voyage-3"
DEFAULT_DIMENSIONS: Final = 1024

_INPUT_TYPES: Final[dict[EmbeddingPurpose, str]] = {
    EmbeddingPurpose.DOCUMENT: "document",
    EmbeddingPurpose.QUERY: "query",
}


class VoyageEmbeddingProvider:
    """An :class:`~atlas.domain.ports.embedding.EmbeddingProvider` over Voyage.

    Args:
        client: A ``voyageai.AsyncClient`` instance, injected so translation is
            testable offline.
        model: Model identifier.
        dimensions: Expected vector width. Verified on every response — a silent
            width change would corrupt a pgvector column rather than fail.
        max_batch_size: Largest batch accepted in one call.
    """

    def __init__(
        self,
        client: voyageai.AsyncClient,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        max_batch_size: int = 128,
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._max_batch_size = max_batch_size

    @property
    def name(self) -> str:
        return "voyage"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    async def embed(
        self,
        texts: Sequence[str],
        purpose: EmbeddingPurpose = EmbeddingPurpose.DOCUMENT,
    ) -> EmbeddingResult:
        if not texts:
            msg = "texts must not be empty"
            raise ValidationError(msg)
        if len(texts) > self._max_batch_size:
            msg = f"batch of {len(texts)} exceeds max_batch_size {self._max_batch_size}"
            raise ValidationError(msg, context={"batch_size": len(texts)})

        try:
            response = await self._client.embed(
                list(texts),
                model=self._model,
                input_type=_INPUT_TYPES[purpose],
                # Requested, not merely checked: the width is baked into a
                # pgvector column, so it must be the value we intend.
                output_dimension=self._dimensions,
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError(str(exc), provider="voyage") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(str(exc), provider="voyage") from exc

        return self._to_result(response)

    def _to_result(self, response: Any) -> EmbeddingResult:
        vectors: list[Vector] = []
        for embedding in response.embeddings:
            vector = tuple(float(value) for value in embedding)
            if len(vector) != self._dimensions:
                msg = f"provider returned {len(vector)}-d vector, expected {self._dimensions}"
                raise ProviderError(msg, provider="voyage", context={"model": self._model})
            vectors.append(vector)

        return EmbeddingResult(
            vectors=tuple(vectors),
            model=self._model,
            usage=TokenUsage(input_tokens=getattr(response, "total_tokens", 0) or 0),
        )


__all__ = ["DEFAULT_DIMENSIONS", "DEFAULT_MODEL", "VoyageEmbeddingProvider"]
