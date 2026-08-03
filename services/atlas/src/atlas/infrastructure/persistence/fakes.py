"""In-memory persistence, for developing and testing without a database.

Same reasoning as the provider and gateway fakes: the pipeline above these is
the code that runs in production, and only the storage is different. They are
configuration rather than stubs — give them documents and they behave the way
PostgreSQL would, including the parts that matter, like replacing a record's
chunks atomically instead of accumulating them.

Never wired up outside tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from atlas.domain.corpus import CandidateChunk, ChunkInput, Document, SearchFilter
from atlas.domain.embedding import Vector


class InMemoryVectorStore:
    """A :class:`VectorStore` backed by dictionaries.

    Search is exact rather than approximate — cosine over every chunk — which is
    the right trade for a fake: it makes results deterministic, and recall is
    what the real index is for.
    """

    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}
        self.chunks: dict[str, list[ChunkInput]] = {}
        #: Every upsert, so a test can assert what was written and when.
        self.writes: list[str] = []
        # Ids are handed out the way PostgreSQL hands them out: unique across
        # the whole store, never per document. Reusing an ordinal here made
        # every chunk look like chunk 0, and LlamaIndex — which identifies
        # nodes by id — silently collapsed a result set of three into one.
        self._chunk_ids: dict[str, list[int]] = {}
        self._document_ids: dict[str, int] = {}
        self._next_id = 1

    async def document_exists(self, source_hash: str) -> bool:
        return source_hash in self.documents

    async def upsert_document(self, document: Document, chunks: Sequence[ChunkInput]) -> int:
        # The real store deletes any other document for the same record inside
        # the same transaction. Reproduced here, or a test would pass against
        # this and leave duplicate chunks in production.
        if document.res_model and document.res_id:
            stale = [
                source_hash
                for source_hash, existing in self.documents.items()
                if existing.res_model == document.res_model
                and existing.res_id == document.res_id
                and source_hash != document.source_hash
            ]
            for source_hash in stale:
                del self.documents[source_hash]
                self.chunks.pop(source_hash, None)
                self._chunk_ids.pop(source_hash, None)
                self._document_ids.pop(source_hash, None)

        self.documents[document.source_hash] = document
        self.chunks[document.source_hash] = list(chunks)
        self._document_ids.setdefault(document.source_hash, self._take_id())
        self._chunk_ids[document.source_hash] = [self._take_id() for _ in chunks]
        self.writes.append(document.source_hash)
        return len(chunks)

    def _take_id(self) -> int:
        identifier = self._next_id
        self._next_id += 1
        return identifier

    async def delete_record(self, res_model: str, res_id: int) -> int:
        doomed = [
            source_hash
            for source_hash, document in self.documents.items()
            if document.res_model == res_model and document.res_id == res_id
        ]
        for source_hash in doomed:
            del self.documents[source_hash]
            self.chunks.pop(source_hash, None)
            self._chunk_ids.pop(source_hash, None)
            self._document_ids.pop(source_hash, None)
        return len(doomed)

    async def search_dense(
        self,
        embedding: Vector,
        *,
        limit: int,
        filters: SearchFilter | None = None,
    ) -> list[CandidateChunk]:
        scored = [
            (_cosine(embedding, chunk.embedding), source_hash, ordinal, chunk)
            for source_hash, chunks in self.chunks.items()
            for ordinal, chunk in enumerate(chunks)
            if _passes(self.documents[source_hash], filters)
        ]
        scored.sort(key=lambda row: row[0], reverse=True)
        return [
            self._candidate(source_hash, chunk, ordinal, score)
            for score, source_hash, ordinal, chunk in scored[:limit]
        ]

    async def search_lexical(
        self,
        query: str,
        *,
        limit: int,
        filters: SearchFilter | None = None,
    ) -> list[CandidateChunk]:
        terms = {term.lower() for term in query.split() if term}
        scored = []
        for source_hash, chunks in self.chunks.items():
            if not _passes(self.documents[source_hash], filters):
                continue
            for ordinal, chunk in enumerate(chunks):
                words = {word.lower().strip(".,:;()") for word in chunk.content.split()}
                overlap = len(terms & words)
                if overlap:
                    scored.append((overlap / len(terms), source_hash, ordinal, chunk))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [
            self._candidate(source_hash, chunk, ordinal, score)
            for score, source_hash, ordinal, chunk in scored[:limit]
        ]

    async def embedding_dimensions(self) -> int:
        for chunks in self.chunks.values():
            if chunks:
                return len(chunks[0].embedding)
        return 0

    def _candidate(
        self, source_hash: str, chunk: ChunkInput, ordinal: int, score: float
    ) -> CandidateChunk:
        """Build a candidate carrying the ids this store handed out."""
        chunk_ids = self._chunk_ids.get(source_hash, [])
        return _candidate_for(
            self.documents[source_hash],
            chunk,
            chunk_id=chunk_ids[ordinal] if ordinal < len(chunk_ids) else 0,
            document_id=self._document_ids.get(source_hash, 0),
            score=score,
        )

    def chunk_count(self, res_model: str, res_id: int) -> int:
        """How many chunks one record currently has. For assertions."""
        return sum(
            len(chunks)
            for source_hash, chunks in self.chunks.items()
            if self.documents[source_hash].res_model == res_model
            and self.documents[source_hash].res_id == res_id
        )


class InMemoryEmbeddingCache:
    """An :class:`EmbeddingCache` backed by a dictionary."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], Vector] = {}

    async def get_many(self, content_hashes: Sequence[str], model: str) -> dict[str, Vector]:
        return {
            content_hash: self.entries[(content_hash, model)]
            for content_hash in content_hashes
            if (content_hash, model) in self.entries
        }

    async def put_many(self, entries: Mapping[str, Vector], model: str) -> int:
        for content_hash, vector in entries.items():
            self.entries.setdefault((content_hash, model), vector)
        return len(entries)


class InMemorySourceState:
    """A :class:`SourceState` backed by a dictionary."""

    def __init__(self) -> None:
        self.watermarks: dict[str, datetime] = {}
        self.registered: dict[str, dict[str, Any]] = {}

    async def register(
        self,
        source_key: str,
        kind: str,
        *,
        config: Mapping[str, Any] | None = None,
        active: bool = True,
    ) -> None:
        self.registered[source_key] = {
            "kind": kind,
            "config": dict(config or {}),
            "active": active,
        }

    async def watermark(self, source_key: str) -> datetime | None:
        return self.watermarks.get(source_key)

    async def advance(self, source_key: str, watermark: datetime) -> None:
        current = self.watermarks.get(source_key)
        if current is None or watermark > current:
            self.watermarks[source_key] = watermark

    async def reset(self, source_key: str) -> None:
        self.watermarks.pop(source_key, None)

    async def active_keys(self) -> list[str]:
        return sorted(key for key, row in self.registered.items() if row["active"])


def _passes(document: Document, filters: SearchFilter | None) -> bool:
    if filters is None:
        return True
    if filters.company_ids and document.company_id not in filters.company_ids:
        return False
    if document.visibility > filters.max_visibility:
        return False
    return not (filters.res_models and document.res_model not in filters.res_models)


def _candidate_for(
    document: Document,
    chunk: ChunkInput,
    *,
    chunk_id: int,
    document_id: int,
    score: float,
) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=chunk.content,
        score=score,
        res_model=document.res_model,
        res_id=document.res_id,
        company_id=document.company_id,
        visibility=document.visibility,
        external_ref=document.external_ref,
        metadata=chunk.metadata,
    )


def _cosine(left: Vector, right: Vector) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return float(dot / (left_norm * right_norm))
