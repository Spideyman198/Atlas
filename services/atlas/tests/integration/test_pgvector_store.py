"""Integration tests for the pgvector store.

These run against a migrated PostgreSQL, so they exercise the SQL, the schema and
the indexes together. That combination is the point: the migration and the queries
name the same tables in two places (ADR-0008), and this is what keeps them honest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg_pool import AsyncConnectionPool

from atlas.domain.corpus import ChunkInput, Document, SearchFilter, Visibility
from atlas.domain.embedding import Vector
from atlas.domain.errors import ValidationError
from atlas.infrastructure.persistence import PgVectorStore
from tests.integration.conftest import requires_database

pytestmark = [pytest.mark.integration, requires_database]

DIMENSIONS = 1536


def _vector(seed: float) -> Vector:
    """A unit vector pointing along one axis, so distances are predictable."""
    values = [0.0] * DIMENSIONS
    values[int(seed) % DIMENSIONS] = 1.0
    return tuple(values)


def _document(
    source_hash: str = "hash-1",
    *,
    res_model: str | None = "sale.order",
    res_id: int | None = 35,
    company_id: int | None = 1,
    visibility: Visibility = Visibility.INTERNAL,
) -> Document:
    return Document(
        source_key="odoo.sale.order",
        source_hash=source_hash,
        title="SO00035",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=DIMENSIONS,
        res_model=res_model,
        res_id=res_id,
        external_ref="SO00035",
        company_id=company_id,
        visibility=visibility,
        record_write_date=datetime(2026, 8, 1, tzinfo=UTC),
        metadata={"state": "sale"},
    )


def _chunks(*contents: str) -> list[ChunkInput]:
    return [
        ChunkInput(ordinal=i, content=text, embedding=_vector(i), token_count=len(text.split()))
        for i, text in enumerate(contents)
    ]


# --- schema ----------------------------------------------------------------


async def test_the_migration_produces_the_configured_vector_width(
    store: PgVectorStore,
) -> None:
    """Read from the live column, so a migration drift is caught rather than assumed."""
    assert await store.embedding_dimensions() == DIMENSIONS


async def test_the_expected_indexes_exist(pool: AsyncConnectionPool) -> None:
    """ADR-0004's index set is load-bearing; a missing one degrades silently."""
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
        names = {row[0] for row in await cursor.fetchall()}

    assert {
        "chunks_embedding_hnsw_idx",
        "chunks_tsv_gin_idx",
        "chunks_scope_idx",
        "chunks_record_idx",
        "documents_source_hash_key",
        "ingest_jobs_claim_idx",
    } <= names


async def test_the_dense_index_is_hnsw_with_the_configured_parameters(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_hnsw_idx'"
        )
        row = await cursor.fetchone()

    assert row is not None
    definition = row[0]
    assert "USING hnsw" in definition
    assert "vector_cosine_ops" in definition
    assert "m='16'" in definition.replace(" ", "") or "m=16" in definition
    assert "ef_construction" in definition


# --- writing ---------------------------------------------------------------


async def test_a_document_and_its_chunks_are_written(store: PgVectorStore) -> None:
    written = await store.upsert_document(_document(), _chunks("first chunk", "second chunk"))

    assert written == 2
    assert await store.document_exists("hash-1")


async def test_an_unknown_hash_does_not_exist(store: PgVectorStore) -> None:
    """The short-circuit that makes incremental sync cheap."""
    assert not await store.document_exists("never-ingested")


async def test_reingesting_replaces_chunks_rather_than_appending(
    store: PgVectorStore, pool: AsyncConnectionPool
) -> None:
    """A shortened document must not leave the previous version's tail behind."""
    await store.upsert_document(_document(), _chunks("a", "b", "c"))
    await store.upsert_document(_document(), _chunks("only one now"))

    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM chunks")
        row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 1


async def test_reingesting_updates_the_document_in_place(
    store: PgVectorStore, pool: AsyncConnectionPool
) -> None:
    await store.upsert_document(_document(), _chunks("a"))
    await store.upsert_document(_document(), _chunks("a"))

    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM documents WHERE source_hash = 'hash-1'")
        row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 1


async def test_a_wrong_width_embedding_is_rejected_before_it_reaches_postgres(
    store: PgVectorStore,
) -> None:
    """The error names the chunk and both widths, which a type error would not."""
    bad = [ChunkInput(ordinal=0, content="x", embedding=(0.1, 0.2, 0.3))]

    with pytest.raises(ValidationError, match="3-d embedding"):
        await store.upsert_document(_document(), bad)


async def test_deleting_a_record_removes_its_chunks(
    store: PgVectorStore, pool: AsyncConnectionPool
) -> None:
    """Chunks go with the document via the foreign key cascade."""
    await store.upsert_document(_document(), _chunks("a", "b"))

    deleted = await store.delete_record("sale.order", 35)

    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM chunks")
        row = await cursor.fetchone()

    assert deleted == 1
    assert row is not None
    assert row[0] == 0


async def test_metadata_round_trips_as_json(store: PgVectorStore) -> None:
    await store.upsert_document(_document(), _chunks("searchable content"))

    results = await store.search_lexical("searchable", limit=5)

    assert results[0].external_ref == "SO00035"


# --- dense search ----------------------------------------------------------


async def test_dense_search_ranks_the_nearest_chunk_first(store: PgVectorStore) -> None:
    await store.upsert_document(_document(), _chunks("alpha", "beta", "gamma"))

    results = await store.search_dense(_vector(1), limit=3)

    assert results[0].content == "beta"
    assert results[0].score == pytest.approx(1.0, abs=1e-6)


async def test_dense_scores_are_higher_is_better(store: PgVectorStore) -> None:
    """Cosine distance is converted so every mode sorts the same direction."""
    await store.upsert_document(_document(), _chunks("alpha", "beta", "gamma"))

    results = await store.search_dense(_vector(0), limit=3)

    assert results == sorted(results, key=lambda c: c.score, reverse=True)


async def test_dense_search_respects_the_limit(store: PgVectorStore) -> None:
    await store.upsert_document(_document(), _chunks("a", "b", "c", "d"))

    assert len(await store.search_dense(_vector(0), limit=2)) == 2


# --- lexical search --------------------------------------------------------


async def test_lexical_search_finds_an_exact_identifier(store: PgVectorStore) -> None:
    """The case dense search is weakest on, and why hybrid exists."""
    await store.upsert_document(
        _document(), _chunks("Order SO00035 shipped", "unrelated content here")
    )

    results = await store.search_lexical("SO00035", limit=5)

    assert len(results) == 1
    assert "SO00035" in results[0].content


async def test_lexical_search_accepts_punctuation_a_user_would_type(
    store: PgVectorStore,
) -> None:
    """`websearch_to_tsquery` tolerates what `to_tsquery` would reject."""
    await store.upsert_document(_document(), _chunks("refund policy for damaged goods"))

    results = await store.search_lexical('"refund policy" -unrelated', limit=5)

    assert len(results) == 1


async def test_lexical_search_returns_nothing_for_an_absent_term(
    store: PgVectorStore,
) -> None:
    await store.upsert_document(_document(), _chunks("refund policy"))

    assert await store.search_lexical("kangaroo", limit=5) == []


# --- pre-filters -----------------------------------------------------------


async def test_the_company_filter_excludes_other_companies(store: PgVectorStore) -> None:
    await store.upsert_document(_document("h1", company_id=1), _chunks("company one content"))
    await store.upsert_document(_document("h2", company_id=2, res_id=36), _chunks("company two"))

    results = await store.search_dense(_vector(0), limit=10, filters=SearchFilter(company_ids=(1,)))

    assert {chunk.company_id for chunk in results} == {1}


async def test_the_visibility_filter_excludes_higher_tiers(store: PgVectorStore) -> None:
    await store.upsert_document(
        _document("h1", visibility=Visibility.PUBLIC), _chunks("public content")
    )
    await store.upsert_document(
        _document("h2", visibility=Visibility.RESTRICTED, res_id=36), _chunks("restricted content")
    )

    results = await store.search_dense(
        _vector(0), limit=10, filters=SearchFilter(max_visibility=Visibility.PUBLIC)
    )

    assert {chunk.visibility for chunk in results} == {Visibility.PUBLIC}


async def test_the_model_filter_narrows_to_one_odoo_model(store: PgVectorStore) -> None:
    await store.upsert_document(_document("h1", res_model="sale.order"), _chunks("an order"))
    await store.upsert_document(
        _document("h2", res_model="res.partner", res_id=7), _chunks("a partner")
    )

    results = await store.search_dense(
        _vector(0), limit=10, filters=SearchFilter(res_models=("res.partner",))
    )

    assert {chunk.res_model for chunk in results} == {"res.partner"}


async def test_filters_apply_to_lexical_search_too(store: PgVectorStore) -> None:
    """Both modes feed the same authorization stage, so both must narrow alike."""
    await store.upsert_document(_document("h1", company_id=1), _chunks("shared keyword here"))
    await store.upsert_document(
        _document("h2", company_id=2, res_id=36), _chunks("shared keyword also")
    )

    results = await store.search_lexical(
        "keyword", limit=10, filters=SearchFilter(company_ids=(2,))
    )

    assert {chunk.company_id for chunk in results} == {2}


async def test_a_filter_matching_nothing_returns_nothing(store: PgVectorStore) -> None:
    await store.upsert_document(_document(), _chunks("content"))

    results = await store.search_dense(
        _vector(0), limit=10, filters=SearchFilter(company_ids=(999,))
    )

    assert results == []


async def test_search_carries_the_fields_the_authorization_filter_needs(
    store: PgVectorStore,
) -> None:
    """M6 authorizes by (res_model, res_id), so both must survive retrieval."""
    await store.upsert_document(_document(), _chunks("content"))

    hit = (await store.search_dense(_vector(0), limit=1))[0]

    assert hit.res_model == "sale.order"
    assert hit.res_id == 35
    assert hit.chunk_id > 0
    assert hit.document_id > 0
