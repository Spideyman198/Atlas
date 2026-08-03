"""Tests for the in-memory persistence doubles.

These are not incidental test helpers: M8 develops retrieval against them, and a
fake that behaves differently from PostgreSQL turns every test written on top of
it into a false negative. So the doubles get tested too — particularly the parts
where they have to imitate behaviour rather than merely store things.
"""

from __future__ import annotations

import pytest

from atlas.domain.corpus import ChunkInput, Document, SearchFilter, Visibility
from atlas.infrastructure.persistence.fakes import (
    InMemoryEmbeddingCache,
    InMemorySourceState,
    InMemoryVectorStore,
)

pytestmark = pytest.mark.unit


def document(source_hash: str, **overrides: object) -> Document:
    values: dict[str, object] = {
        "source_key": "odoo.sale.order",
        "source_hash": source_hash,
        "title": "S00001",
        "embedding_model": "hash-embedding-v1",
        "embedding_dimensions": 3,
        "res_model": "sale.order",
        "res_id": 1,
        "company_id": 1,
        "visibility": Visibility.INTERNAL,
    }
    values.update(overrides)
    return Document(**values)  # type: ignore[arg-type]


def chunk(ordinal: int, content: str, embedding: tuple[float, ...]) -> ChunkInput:
    return ChunkInput(ordinal=ordinal, content=content, embedding=embedding)


async def test_a_stored_document_is_found_by_its_hash() -> None:
    store = InMemoryVectorStore()

    await store.upsert_document(document("hash-a"), [chunk(0, "desks", (1.0, 0.0, 0.0))])

    assert await store.document_exists("hash-a")
    assert not await store.document_exists("hash-b")


async def test_rewriting_a_record_replaces_it_rather_than_adding_to_it() -> None:
    """The real store deletes the previous version in the same transaction.

    A fake that merely accumulated would let a test pass while production
    quietly returned two versions of the same order.
    """
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "old", (1.0, 0.0, 0.0))])

    await store.upsert_document(document("hash-b"), [chunk(0, "new", (1.0, 0.0, 0.0))])

    assert not await store.document_exists("hash-a")
    assert store.chunk_count("sale.order", 1) == 1


async def test_documents_for_different_records_coexist() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "one", (1.0, 0.0, 0.0))])

    await store.upsert_document(document("hash-b", res_id=2), [chunk(0, "two", (0.0, 1.0, 0.0))])

    assert len(store.documents) == 2


async def test_deleting_a_record_removes_its_chunks() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "one", (1.0, 0.0, 0.0))])

    assert await store.delete_record("sale.order", 1) == 1
    assert store.chunk_count("sale.order", 1) == 0


async def test_dense_search_ranks_by_similarity() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(
        document("hash-a"),
        [chunk(0, "close", (1.0, 0.0, 0.0)), chunk(1, "far", (0.0, 0.0, 1.0))],
    )

    results = await store.search_dense((1.0, 0.0, 0.0), limit=2)

    assert [result.content for result in results] == ["close", "far"]
    assert results[0].score > results[1].score


async def test_dense_search_respects_the_company_pre_filter() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "ours", (1.0, 0.0, 0.0))])
    await store.upsert_document(
        document("hash-b", res_id=2, company_id=2), [chunk(0, "theirs", (1.0, 0.0, 0.0))]
    )

    results = await store.search_dense(
        (1.0, 0.0, 0.0), limit=5, filters=SearchFilter(company_ids=(1,))
    )

    assert [result.content for result in results] == ["ours"]


async def test_dense_search_respects_the_visibility_ceiling() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(
        document("hash-a", visibility=Visibility.RESTRICTED),
        [chunk(0, "restricted", (1.0, 0.0, 0.0))],
    )

    results = await store.search_dense(
        (1.0, 0.0, 0.0), limit=5, filters=SearchFilter(max_visibility=Visibility.INTERNAL)
    )

    assert results == []


async def test_dense_search_respects_the_model_filter() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "order", (1.0, 0.0, 0.0))])

    results = await store.search_dense(
        (1.0, 0.0, 0.0), limit=5, filters=SearchFilter(res_models=("res.partner",))
    )

    assert results == []


async def test_lexical_search_finds_words_not_vectors() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(
        document("hash-a"),
        [
            chunk(0, "Desk Combination, oak.", (1.0, 0.0, 0.0)),
            chunk(1, "Office chair", (0.0, 1.0, 0.0)),
        ],
    )

    results = await store.search_lexical("desk", limit=5)

    assert [result.content for result in results] == ["Desk Combination, oak."]


async def test_lexical_search_returns_nothing_for_an_absent_term() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "desks", (1.0, 0.0, 0.0))])

    assert await store.search_lexical("helicopters", limit=5) == []


async def test_the_stores_dimensions_come_from_what_is_in_it() -> None:
    store = InMemoryVectorStore()
    assert await store.embedding_dimensions() == 0

    await store.upsert_document(document("hash-a"), [chunk(0, "x", (1.0, 0.0, 0.0))])

    assert await store.embedding_dimensions() == 3


async def test_a_mismatched_vector_scores_zero_rather_than_raising() -> None:
    store = InMemoryVectorStore()
    await store.upsert_document(document("hash-a"), [chunk(0, "x", (1.0, 0.0, 0.0))])

    results = await store.search_dense((1.0, 0.0), limit=5)

    assert results[0].score == 0.0


async def test_the_cache_keeps_the_first_vector_for_a_hash() -> None:
    cache = InMemoryEmbeddingCache()
    await cache.put_many({"a": (1.0,)}, "model-x")

    await cache.put_many({"a": (2.0,)}, "model-x")

    assert (await cache.get_many(["a"], "model-x"))["a"] == (1.0,)


async def test_the_cache_separates_models() -> None:
    cache = InMemoryEmbeddingCache()
    await cache.put_many({"a": (1.0,)}, "model-x")

    assert await cache.get_many(["a"], "model-y") == {}


async def test_source_state_lists_only_active_sources() -> None:
    state = InMemorySourceState()
    await state.register("odoo.res.partner", "odoo_model")
    await state.register("odoo.sale.order", "odoo_model", active=False)

    assert await state.active_keys() == ["odoo.res.partner"]


async def test_source_state_forgets_a_watermark_on_reset() -> None:
    from datetime import UTC, datetime

    state = InMemorySourceState()
    await state.advance("odoo.res.partner", datetime(2026, 8, 1, tzinfo=UTC))

    await state.reset("odoo.res.partner")

    assert await state.watermark("odoo.res.partner") is None
