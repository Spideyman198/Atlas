"""Syncing a source into the corpus.

The cold path, and the one with the money in it. Every design choice here is
about not paying twice:

**The hash check happens before the embedding call**, not after. That ordering is
the difference between a cron job that costs cents a day and one that costs
dollars an hour, and it is what the acceptance criterion measures — re-running a
sync with nothing changed must make zero embedding calls.

**The embedding cache is keyed by segment, not by document.** Boilerplate is
everywhere in an ERP: the same delivery paragraph on every order, the same
description on every variant of a product. A changed order re-embeds the line
that changed, not the eleven that did not.

**A document's chunks are replaced atomically.** One record's update touches
exactly that record's chunks, in one transaction, so retrieval never sees a
half-rewritten document or two versions of the same order.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import replace

from atlas.domain.corpus import ChunkInput, Document
from atlas.domain.embedding import EmbeddingPurpose, Vector
from atlas.domain.errors import AtlasError
from atlas.domain.ingestion import JobKind, RawDocument, SourceRecord, SyncReport
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.domain.ports.ingestion import DocumentLoader, EmbeddingCache, SourceReader, SourceState
from atlas.domain.ports.vector_store import VectorStore
from atlas.domain.sources import SourceTemplate, template_for

logger = logging.getLogger(__name__)

#: Records read from Odoo per round-trip. Large enough that a full sync is not
#: dominated by HTTP, small enough that one page fits comfortably in memory
#: alongside its embeddings.
DEFAULT_PAGE_SIZE = 100

#: Stops a runaway or mis-filtered source from looping forever. A source with
#: more than this many pages of changes wants a full re-index, not a cron run.
MAX_PAGES = 10_000

#: Rough characters per token, used only to record an approximate size on each
#: chunk for M10's context budgeting. Deliberately not a tokeniser call: it would
#: cost more than it informs, and nothing correctness-bearing reads it.
CHARS_PER_TOKEN = 4


class SyncSource:
    """Bring one source's corpus up to date."""

    def __init__(  # noqa: PLR0913 - keyword-only collaborators, injected once
        self,
        *,
        reader: SourceReader,
        loader: DocumentLoader,
        embedder: EmbeddingProvider,
        store: VectorStore,
        cache: EmbeddingCache,
        state: SourceState,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._reader = reader
        self._loader = loader
        self._embedder = embedder
        self._store = store
        self._cache = cache
        self._state = state
        self._page_size = page_size

    async def run(
        self,
        source_key: str,
        *,
        kind: JobKind = JobKind.INCREMENTAL,
        record_ids: Sequence[int] | None = None,
    ) -> SyncReport:
        """Sync a source and report what it cost.

        Args:
            source_key: Which source to sync.
            kind: ``INCREMENTAL`` reads what has changed since the watermark.
                ``FULL_SYNC`` reads everything but still skips unchanged
                content. ``REINDEX`` ignores the hash and re-embeds — which is
                what to run after switching embedding model, and the only mode
                that is expensive on purpose.
            record_ids: Sync exactly these records, whatever the watermark says.
        """
        template = template_for(source_key)
        reads_everything = kind in (JobKind.FULL_SYNC, JobKind.REINDEX)
        since = None if reads_everything else await self._state.watermark(source_key)
        report = SyncReport(source_key=source_key)
        high_water = since

        offset = 0
        for _page in range(MAX_PAGES):
            batch = await self._reader.read_records(
                source_key,
                since=since,
                limit=self._page_size,
                offset=offset,
                record_ids=record_ids,
            )
            if not batch.records:
                break

            for record in batch.records:
                page_report = await self._ingest(template, record, kind=kind)
                report = report.merged_with(page_report)
                if record.write_date and (high_water is None or record.write_date > high_water):
                    high_water = record.write_date

            if not batch.more or record_ids is not None:
                break
            # The reader pages by offset within a stable ordering. Advancing the
            # watermark mid-run instead would skip records sharing a timestamp.
            offset += len(batch.records)

        if high_water is not None and high_water != since:
            await self._state.advance(source_key, high_water)

        logger.info(
            "source synced",
            extra={
                "source_key": source_key,
                "kind": str(kind),
                "examined": report.examined,
                "unchanged": report.unchanged,
                "ingested": report.ingested,
                "chunks": report.chunks_written,
                "embedding_calls": report.embedding_calls,
                "cached_segments": report.cached_segments,
                "failures": len(report.failures),
            },
        )
        return report

    async def delete_records(self, source_key: str, record_ids: Sequence[int]) -> SyncReport:
        """Remove records that no longer exist in Odoo.

        Driven by Odoo telling us, rather than by diffing id sets: an ERP has
        more records than we want to enumerate on a schedule, and a delete we
        missed is a citation that resolves to nothing rather than a leak.
        """
        template = template_for(source_key)
        deleted = 0
        for record_id in record_ids:
            deleted += await self._store.delete_record(template.res_model, record_id)
        return SyncReport(source_key=source_key, deleted=deleted)

    async def _ingest(
        self,
        template: SourceTemplate,
        record: SourceRecord,
        *,
        kind: JobKind,
    ) -> SyncReport:
        """Ingest one record, or explain why it was skipped."""
        key = template.key
        try:
            document = await self._render(template, record)
        except AtlasError as exc:
            logger.warning(
                "could not render record",
                extra={"source_key": key, "res_id": record.res_id, "error": exc.code},
            )
            return SyncReport(
                source_key=key,
                examined=1,
                failures=(f"{template.res_model}:{record.res_id}: {exc.message}",),
            )

        source_hash = document.source_hash()

        # The short-circuit. Nothing below this line runs for content that has
        # not moved, which is why an idle sync costs one query per record and no
        # provider call at all.
        if kind is not JobKind.REINDEX and await self._store.document_exists(source_hash):
            return SyncReport(source_key=key, examined=1, unchanged=1)

        segments = await self._segments(template, document, record)
        if not segments:
            return SyncReport(source_key=key, examined=1, unchanged=1)

        vectors, calls, cached = await self._embed(segments)

        stored = Document(
            source_key=key,
            source_hash=source_hash,
            title=document.title,
            embedding_model=self._embedder.model_id,
            embedding_dimensions=self._embedder.dimensions,
            res_model=document.res_model,
            res_id=document.res_id,
            external_ref=document.external_ref,
            company_id=document.company_id,
            visibility=document.visibility,
            record_write_date=document.record_write_date,
            metadata=document.metadata,
        )
        chunks = [
            ChunkInput(
                ordinal=ordinal,
                content=segment,
                embedding=vector,
                token_count=len(segment) // CHARS_PER_TOKEN,
                metadata={"title": document.title},
            )
            for ordinal, (segment, vector) in enumerate(zip(segments, vectors, strict=True))
        ]
        written = await self._store.upsert_document(stored, chunks)

        return SyncReport(
            source_key=key,
            examined=1,
            ingested=1,
            chunks_written=written,
            embedding_calls=calls,
            cached_segments=cached,
        )

    async def _render(self, template: SourceTemplate, record: SourceRecord) -> RawDocument:
        """Turn a record into a document, without downloading anything yet.

        For an attachment the checksum Odoo already holds stands in for the
        content, so an unchanged 40 MB contract is skipped before it is fetched.
        """
        document = template.render(record)
        if template.binary_key and (checksum := str(record.values.get("checksum") or "")):
            return replace(document, content_fingerprint=checksum)
        return document

    async def _segments(
        self,
        template: SourceTemplate,
        document: RawDocument,
        record: SourceRecord,
    ) -> list[str]:
        """Cut the document into the pieces that will be embedded."""
        if not template.binary_key:
            return self._loader.split_text(document.text)

        content = await self._reader.read_binary(template.key, record.res_id)
        mimetype = str(record.values.get("mimetype") or "")
        body = self._loader.load_file(document.title, content, mimetype)
        if not body:
            # No text layer, or a type nobody taught us. The header alone is
            # still worth indexing: somebody searching for the file by name
            # should find it.
            return self._loader.split_text(document.text)
        return self._loader.split_text(document.text) + body

    async def _embed(self, segments: Sequence[str]) -> tuple[list[Vector], int, int]:
        """Embed segments, paying only for the ones nobody has embedded before.

        Returns:
            The vectors in input order, how many provider calls it took, and how
            many segments came from the cache.
        """
        model = self._embedder.model_id
        hashes = [_content_hash(segment) for segment in segments]
        cached = await self._cache.get_many(hashes, model)

        # Deduplicated within the batch too: a document that repeats a paragraph
        # should not pay for it twice in the same call.
        outstanding: dict[str, str] = {
            digest: segment
            for digest, segment in zip(hashes, segments, strict=True)
            if digest not in cached
        }

        calls = 0
        fresh: dict[str, Vector] = {}
        pending = list(outstanding.items())
        batch_size = max(self._embedder.max_batch_size, 1)
        for start in range(0, len(pending), batch_size):
            window = pending[start : start + batch_size]
            result = await self._embedder.embed(
                [segment for _digest, segment in window],
                EmbeddingPurpose.DOCUMENT,
            )
            calls += 1
            fresh.update(
                {
                    digest: vector
                    for (digest, _segment), vector in zip(window, result.vectors, strict=True)
                }
            )

        if fresh:
            await self._cache.put_many(fresh, model)

        lookup = {**cached, **fresh}
        return [lookup[digest] for digest in hashes], calls, len(segments) - len(outstanding)


def _content_hash(segment: str) -> str:
    """The cache key for one segment: exactly the text that will be embedded."""
    return hashlib.sha256(segment.encode()).hexdigest()
