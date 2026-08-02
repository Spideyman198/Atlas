"""Embedding value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atlas.domain.usage import TokenUsage

#: A single embedding. A tuple rather than a list so the containing value objects
#: stay hashable and cannot be mutated after the provider returns them.
Vector = tuple[float, ...]


class EmbeddingPurpose(StrEnum):
    """What an embedding will be used for.

    Some providers embed a passage and a query differently — the same text can
    produce different vectors depending on which side of the search it sits on,
    and using the wrong one measurably degrades recall. Providers that make no
    distinction ignore this.
    """

    DOCUMENT = "document"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Vectors for one batch of texts.

    ``vectors`` is positionally aligned with the input sequence. Adapters must
    preserve order even when the vendor returns results out of order.
    """

    vectors: tuple[Vector, ...]
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __len__(self) -> int:
        return len(self.vectors)
