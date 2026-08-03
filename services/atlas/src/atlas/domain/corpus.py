"""Corpus value objects: what gets stored, and what comes back from a search.

A **document** is the unit of ingestion and invalidation — one sales order, one
PDF. A **chunk** is the unit of retrieval. Re-ingesting a document replaces its
chunks atomically, which is what makes incremental sync correct.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from atlas.domain.embedding import Vector


class Visibility(IntEnum):
    """Coarse sensitivity tier, used as a retrieval pre-filter.

    Ordered so a query can ask for "at most this tier" with a single comparison.

    This is **not** an authorization decision. It narrows a candidate set cheaply;
    whether the acting user may see a record is settled by asking Odoo, per
    ADR-0006. Treating it as authoritative is the failure mode that ADR rejects.
    """

    PUBLIC = 0
    INTERNAL = 1
    RESTRICTED = 2


@dataclass(frozen=True, slots=True)
class Document:
    """An ingested source item, before chunking.

    Attributes:
        source_key: Which ingest source produced this, for example
            ``odoo.sale.order``.
        source_hash: SHA-256 of the normalised rendered content. Unique. If it is
            unchanged, ingestion skips the document entirely — no embedding call,
            no cost. This is what makes a 15-minute cron cheap.
        res_model: Odoo model name, for records. ``None`` for uploads.
        res_id: Odoo record id. A soft reference: there is no foreign key across
            the database boundary (ADR-0004).
        external_ref: Human-facing key such as ``SO00035``.
        company_id: Denormalised onto chunks for the pre-filter.
        visibility: Denormalised onto chunks for the pre-filter.
        embedding_model: Model that produced the vectors, stored so a model change
            is detectable rather than silently mixing vector spaces.
        embedding_dimensions: Width of those vectors.
        record_write_date: Source record's last modification, for the incremental
            sync watermark.
    """

    source_key: str
    source_hash: str
    title: str
    embedding_model: str
    embedding_dimensions: int
    res_model: str | None = None
    res_id: int | None = None
    external_ref: str | None = None
    company_id: int | None = None
    visibility: Visibility = Visibility.INTERNAL
    record_write_date: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChunkInput:
    """A chunk about to be written, with its embedding already computed."""

    ordinal: int
    content: str
    embedding: Vector
    token_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateChunk:
    """A retrieval hit, before authorization.

    Named for what it is not: authorized. The prompt assembler accepts only
    ``AuthorizedChunk`` (M6), so a candidate cannot reach a prompt without passing
    through the filter that asks Odoo. Under ``mypy --strict`` the shortcut does
    not type-check.

    Attributes:
        score: Higher is better, whatever the search mode. Dense search converts
            cosine distance so callers never have to remember which direction a
            given mode sorts in.
    """

    chunk_id: int
    document_id: int
    content: str
    score: float
    res_model: str | None = None
    res_id: int | None = None
    company_id: int | None = None
    visibility: Visibility = Visibility.INTERNAL
    external_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthorizedChunk:
    """A chunk Odoo has confirmed the acting user may read, in this request.

    The counterpart to :class:`CandidateChunk`, and the only kind of chunk the
    prompt assembler accepts. The two types are the mechanism: there is no way
    to put retrieval output into a prompt without going through the filter that
    asks Odoo, because the shortcut does not type-check.

    Nothing outside ``atlas.application.authorization`` should construct one.
    Doing so asserts something no code here has checked.
    """

    chunk_id: int
    document_id: int
    content: str
    score: float
    res_model: str | None = None
    res_id: int | None = None
    external_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """The cheap, non-authoritative narrowing applied inside the query.

    Empty collections mean "no restriction on this axis".
    """

    company_ids: tuple[int, ...] = ()
    max_visibility: Visibility = Visibility.RESTRICTED
    res_models: tuple[str, ...] = ()
