"""The vector store port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from atlas.domain.corpus import CandidateChunk, ChunkInput, Document, SearchFilter
from atlas.domain.embedding import Vector


@runtime_checkable
class VectorStore(Protocol):
    """Persistence and search for the retrieval corpus.

    Dense and lexical search are separate operations rather than one call with a
    mode flag. Fusing their results is a retrieval decision — reciprocal rank
    fusion, then MMR (M8) — and belongs in the application layer, not in storage.
    """

    async def document_exists(self, source_hash: str) -> bool:
        """Whether a document with this content hash is already stored.

        The short-circuit that makes incremental sync cheap: unchanged content
        skips embedding entirely.
        """
        ...

    async def upsert_document(self, document: Document, chunks: Sequence[ChunkInput]) -> int:
        """Write a document and replace its chunks, atomically.

        Delete-and-replace in one transaction, so a reader sees either the old
        chunk set or the new one and never a half-updated document.

        Returns:
            The number of chunks written.

        Raises:
            ValidationError: A chunk's embedding width disagrees with the
                document's declared dimensions.
            StorageError: The database rejected the write.
        """
        ...

    async def delete_record(self, res_model: str, res_id: int) -> int:
        """Remove everything derived from one Odoo record.

        Returns:
            The number of documents deleted.
        """
        ...

    async def search_dense(
        self,
        embedding: Vector,
        *,
        limit: int,
        filters: SearchFilter | None = None,
    ) -> list[CandidateChunk]:
        """Approximate nearest-neighbour search, highest score first.

        Callers over-fetch: the authorization filter will discard some results,
        and how many is not known in advance (ADR-0006).
        """
        ...

    async def search_lexical(
        self,
        query: str,
        *,
        limit: int,
        filters: SearchFilter | None = None,
    ) -> list[CandidateChunk]:
        """Full-text search over the same chunks, highest score first.

        Catches the exact identifiers — ``SO00035``, a SKU, a part number — that
        dense search is weakest on.
        """
        ...

    async def embedding_dimensions(self) -> int:
        """The vector width the schema was migrated with.

        Compared against the configured embedding model at startup: a mismatch is
        a re-index, not a configuration change (ADR-0005).
        """
        ...
