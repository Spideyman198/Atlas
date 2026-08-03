"""Ingestion value objects.

The vocabulary of the cold path: what a source is, what one item of it looks
like once rendered to text, and what a unit of queued work is.

Nothing here does I/O and nothing here knows about Odoo, LlamaIndex or
PostgreSQL. That is what lets the sync use case be tested end to end with fakes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from atlas.domain.corpus import Visibility


class JobKind(StrEnum):
    """Why a job was queued.

    ``REINDEX`` differs from ``FULL_SYNC`` in one way that matters: it ignores
    the content hash, so it re-embeds text that has not changed. That is what to
    run after switching embedding model, and it is the expensive one.
    """

    FULL_SYNC = "full_sync"
    INCREMENTAL = "incremental"
    SINGLE = "single"
    REINDEX = "reindex"


class JobStatus(StrEnum):
    """Where a job is in its life.

    ``DEAD`` is a terminal state a human has to look at: the job failed more
    times than the policy allows. It is deliberately not ``FAILED``, so that
    "retrying" and "gave up" are distinguishable in a query.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One Odoo record, as ingestion received it.

    ``values`` is whatever the source template asked for, plus any child records
    it declared. Untyped on purpose: this is the boundary where an ERP's field
    soup becomes our text, and typing it would mean modelling every Odoo model.
    """

    res_model: str
    res_id: int
    values: Mapping[str, Any]
    write_date: datetime | None = None
    company_id: int | None = None


@dataclass(frozen=True, slots=True)
class RawDocument:
    """A source item rendered to retrievable text, before chunking.

    Attributes:
        text: The rendered content. This is what gets hashed, chunked and
            embedded — nothing downstream sees the original record.
        visibility: A coarse pre-filter tier, never an authorization decision
            (ADR-0006).
    """

    source_key: str
    title: str
    text: str
    res_model: str | None = None
    res_id: int | None = None
    external_ref: str | None = None
    company_id: int | None = None
    visibility: Visibility = Visibility.INTERNAL
    record_write_date: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    content_fingerprint: str | None = None

    def source_hash(self) -> str:
        """A stable fingerprint of this document's identity *and* its content.

        The identity is in the hash, not just the content, for two reasons. The
        column is unique, so two records that happen to render identically — two
        contacts with the same name and nothing else filled in — would otherwise
        collide and silently overwrite one another. And a hash that moves when
        the record moves is what makes ``documents_source_hash_key`` a working
        idempotency key rather than a content-addressed store.

        ``content_fingerprint`` stands in for the text when the source can tell
        us whether the content changed without handing it over. Attachments do:
        Odoo keeps a checksum, so a 40 MB contract that has not moved is skipped
        without ever being downloaded, and one that has changed is re-read.

        Otherwise the text is used, with whitespace normalised first — so
        reformatting a template does not invalidate a corpus that says exactly
        the same thing.
        """
        identity = f"{self.source_key}|{self.res_model or ''}|{self.res_id or 0}"
        content = self.content_fingerprint or " ".join(self.text.split())
        return hashlib.sha256(f"{identity}|{content}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class IngestJob:
    """A claimed unit of work.

    Attributes:
        attempts: Incremented when the job is claimed, not when it fails, so a
            worker that dies mid-job still burns an attempt. Otherwise a job that
            reliably kills its worker is retried forever.
    """

    id: int
    source_key: str
    kind: JobKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    attempts: int = 0
    status: JobStatus = JobStatus.RUNNING


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync did, in the terms the acceptance criteria are written in.

    ``embedding_calls`` is here because "re-running a sync with no data changes
    performs zero embedding calls" is a property the test suite asserts, and a
    property nobody can assert is a property nobody keeps.
    """

    source_key: str
    examined: int = 0
    unchanged: int = 0
    ingested: int = 0
    chunks_written: int = 0
    embedding_calls: int = 0
    cached_segments: int = 0
    deleted: int = 0
    failures: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether this sync altered the corpus at all."""
        return bool(self.ingested or self.deleted)

    def merged_with(self, other: SyncReport) -> SyncReport:
        """Combine two reports over the same source, for batched runs."""
        return SyncReport(
            source_key=self.source_key,
            examined=self.examined + other.examined,
            unchanged=self.unchanged + other.unchanged,
            ingested=self.ingested + other.ingested,
            chunks_written=self.chunks_written + other.chunks_written,
            embedding_calls=self.embedding_calls + other.embedding_calls,
            cached_segments=self.cached_segments + other.cached_segments,
            deleted=self.deleted + other.deleted,
            failures=self.failures + other.failures,
        )


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """What the queue decided after a job failed."""

    attempts: int
    dead: bool
    run_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecordBatch:
    """A page of records from one source, with the watermark it reached."""

    records: Sequence[SourceRecord]
    watermark: datetime | None = None
    more: bool = False
