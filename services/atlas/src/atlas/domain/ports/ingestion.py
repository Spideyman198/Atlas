"""Ports the ingestion pipeline depends on.

Three of them, and the split is the same one everywhere else in this codebase:
where does the work happen, and what does the application layer need to say
about it without knowing.

``SourceReader``    reads records out of Odoo, as the integration user
``DocumentLoader``  turns text or a file into the segments that get embedded
``JobQueue``        durable work, claimed by one worker at a time
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from atlas.domain.embedding import Vector
from atlas.domain.ingestion import IngestJob, JobKind, RecordBatch, RetryOutcome


@runtime_checkable
class SourceReader(Protocol):
    """Reads Odoo records for indexing.

    Deliberately **not** the same path as the query-time gateway. Ingestion runs
    as a dedicated integration user that can see everything worth indexing, which
    is broader than any one person's view — and precisely why query-time
    authorization cannot be skipped (ADR-0006). Two different jobs, two different
    doors, so neither can be mistaken for the other.
    """

    async def read_records(
        self,
        source_key: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        record_ids: Sequence[int] | None = None,
    ) -> RecordBatch:
        """Read one page of a source, oldest change first.

        Args:
            source_key: Which source to read.
            limit: Page size.
            offset: How far into the ordering to start.
            since: Only records modified after this. The incremental watermark.
            record_ids: Read exactly these, ignoring ``since``. Used when Odoo
                tells us a specific record changed.

        Raises:
            NotFoundError: The source's Odoo model is not installed.
            DependencyUnavailableError: Odoo could not be reached.
        """
        ...

    async def read_binary(self, source_key: str, record_id: int) -> bytes:
        """Fetch the file behind an attachment record.

        Separate from :meth:`read_records` because attachment payloads are large
        and most of them turn out to be unchanged; the hash is checked on the
        metadata first, so most files are never fetched at all.
        """
        ...

    async def available_sources(self) -> Mapping[str, bool]:
        """Which known sources this Odoo can actually serve.

        A source whose module is not installed is reported here rather than
        discovered as a failure halfway through a sync.
        """
        ...


@runtime_checkable
class DocumentLoader(Protocol):
    """Turns content into the segments that get embedded.

    Chunking is not a port of its own: ADR-0003 puts it inside the loader, as a
    retrieval strategy exposed through settings rather than a concept the domain
    reasons about. So the port speaks of segments, and how they are cut is the
    adapter's business.
    """

    def split_text(self, text: str) -> list[str]:
        """Cut rendered text into overlapping segments, in order."""
        ...

    def load_file(self, filename: str, content: bytes, mimetype: str) -> list[str]:
        """Extract text from a file and cut it into segments.

        Returns an empty list for a file it cannot read — a scanned PDF with no
        text layer, an unsupported type. That is not an error: it is a document
        with nothing to index, and failing the sync over it would let one bad
        attachment block a corpus.
        """
        ...


@runtime_checkable
class EmbeddingCache(Protocol):
    """Vectors already paid for, keyed by content and model.

    The model is part of the key because two models produce incompatible vector
    spaces for the same text, and sharing a cache between them would poison a
    corpus in a way no error message would ever mention.
    """

    async def get_many(self, content_hashes: Sequence[str], model: str) -> dict[str, Vector]:
        """Return whatever is cached, keyed by content hash. Misses are absent."""
        ...

    async def put_many(self, entries: Mapping[str, Vector], model: str) -> int:
        """Store vectors, ignoring any another worker stored first."""
        ...


@runtime_checkable
class SourceState(Protocol):
    """Per-source registration and the incremental watermark."""

    async def register(
        self,
        source_key: str,
        kind: str,
        *,
        config: Mapping[str, Any] | None = None,
        active: bool = True,
    ) -> None:
        """Record that a source exists, without disturbing its watermark."""
        ...

    async def watermark(self, source_key: str) -> datetime | None:
        """The newest ``write_date`` already indexed for this source."""
        ...

    async def advance(self, source_key: str, watermark: datetime) -> None:
        """Move the watermark forward. Never backward."""
        ...

    async def reset(self, source_key: str) -> None:
        """Forget the watermark, so the next sync reads everything again."""
        ...

    async def active_keys(self) -> list[str]:
        """Every source that is switched on."""
        ...


@runtime_checkable
class JobQueue(Protocol):
    """Durable ingestion work, in PostgreSQL rather than in another service.

    ``SELECT ... FOR UPDATE SKIP LOCKED`` gives a transactional queue that
    several workers can drain safely, with no Redis, no broker and no extra
    container to operate. It is the single best argument for already having
    PostgreSQL.
    """

    async def enqueue(
        self,
        source_key: str,
        kind: JobKind,
        *,
        payload: Mapping[str, Any] | None = None,
        run_after: datetime | None = None,
    ) -> int:
        """Queue a job and return its id."""
        ...

    async def claim(self, worker: str) -> IngestJob | None:
        """Take the next due job, or ``None`` if there is nothing to do.

        Claiming is atomic: two workers polling at the same moment get different
        jobs, or one gets nothing. Never the same job twice.
        """
        ...

    async def succeed(self, job_id: int) -> None:
        """Mark a job done."""
        ...

    async def fail(self, job_id: int, error: str) -> RetryOutcome:
        """Record a failure and decide whether to retry.

        Returns:
            Whether the job will run again, and when. A job that has exhausted
            its attempts becomes ``dead`` — a terminal state a person has to look
            at, deliberately distinct from ``failed`` so "still trying" and "gave
            up" are different queries.
        """
        ...

    async def release_stale(self, older_than_seconds: float) -> int:
        """Return jobs whose worker died back to the queue.

        A crashed worker leaves its job ``running`` forever otherwise. The
        attempt it burned is not refunded, so a job that reliably kills workers
        still reaches ``dead``.
        """
        ...
