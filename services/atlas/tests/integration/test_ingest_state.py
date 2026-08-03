"""The embedding cache and the sync watermark, against real PostgreSQL.

Both round-trip through PostgreSQL types that a fake would not exercise: a
``vector`` column and a ``timestamptz``. The first is why an embedding comes
back as something numpy-shaped, and the second is why a watermark comparison is
timezone-aware or wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg_pool import AsyncConnectionPool

from atlas.infrastructure.persistence.ingest_state import PgEmbeddingCache, PgSourceState

pytestmark = pytest.mark.integration

MODEL = "hash-embedding-v1"
OTHER_MODEL = "text-embedding-3-small"


@pytest.fixture
async def cache(pool: AsyncConnectionPool) -> PgEmbeddingCache:
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("TRUNCATE embedding_cache")
    return PgEmbeddingCache(pool)


@pytest.fixture
async def state(pool: AsyncConnectionPool) -> PgSourceState:
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("TRUNCATE ingest_sources")
    return PgSourceState(pool)


def vector(seed: float, width: int = 1536) -> tuple[float, ...]:
    return tuple(seed + index * 0.001 for index in range(width))


async def test_a_stored_vector_comes_back_as_it_went_in(cache: PgEmbeddingCache) -> None:
    stored = vector(0.5)

    await cache.put_many({"hash-a": stored}, MODEL)
    found = await cache.get_many(["hash-a"], MODEL)

    assert set(found) == {"hash-a"}
    assert isinstance(found["hash-a"], tuple)
    assert found["hash-a"] == pytest.approx(stored)


async def test_a_miss_is_simply_absent(cache: PgEmbeddingCache) -> None:
    await cache.put_many({"hash-a": vector(0.1)}, MODEL)

    found = await cache.get_many(["hash-a", "hash-b"], MODEL)

    assert set(found) == {"hash-a"}


async def test_the_model_is_part_of_the_key(cache: PgEmbeddingCache) -> None:
    """Two models produce incompatible vector spaces for the same text.

    Sharing a cache between them would poison a corpus in a way no error message
    would ever mention.
    """
    await cache.put_many({"hash-a": vector(0.1)}, MODEL)

    assert await cache.get_many(["hash-a"], OTHER_MODEL) == {}


async def test_storing_the_same_hash_twice_keeps_the_first(cache: PgEmbeddingCache) -> None:
    first = vector(0.25)
    await cache.put_many({"hash-a": first}, MODEL)

    await cache.put_many({"hash-a": vector(0.75)}, MODEL)
    found = await cache.get_many(["hash-a"], MODEL)

    assert found["hash-a"] == pytest.approx(first)


async def test_an_empty_lookup_is_not_a_round_trip(cache: PgEmbeddingCache) -> None:
    assert await cache.get_many([], MODEL) == {}
    assert await cache.put_many({}, MODEL) == 0


async def test_a_watermark_round_trips_with_its_timezone(state: PgSourceState) -> None:
    await state.register("odoo.res.partner", "odoo_model")
    mark = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    await state.advance("odoo.res.partner", mark)

    assert await state.watermark("odoo.res.partner") == mark


async def test_a_watermark_is_kept_for_a_source_nobody_registered(
    state: PgSourceState,
) -> None:
    """The sync advances the watermark; nothing else guarantees the row exists.

    An `UPDATE` alone silently matched nothing, so every "incremental" run
    re-read the whole model — invisible except on the bill. Caught by running a
    real sync twice, not by a unit test: the in-memory double stored the value
    happily either way.
    """
    mark = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    await state.advance("odoo.never.registered", mark)

    assert await state.watermark("odoo.never.registered") == mark


async def test_a_watermark_never_moves_backward(state: PgSourceState) -> None:
    """A full sync finishing on older records must not undo an incremental run."""
    await state.register("odoo.res.partner", "odoo_model")
    later = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    await state.advance("odoo.res.partner", later)

    await state.advance("odoo.res.partner", later - timedelta(hours=1))

    assert await state.watermark("odoo.res.partner") == later


async def test_registering_again_does_not_rewind_progress(state: PgSourceState) -> None:
    """Registration happens on every start-up. It must be free of consequence."""
    await state.register("odoo.res.partner", "odoo_model")
    mark = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    await state.advance("odoo.res.partner", mark)

    await state.register("odoo.res.partner", "odoo_model", config={"changed": True})

    assert await state.watermark("odoo.res.partner") == mark


async def test_resetting_makes_the_next_sync_read_everything(state: PgSourceState) -> None:
    await state.register("odoo.res.partner", "odoo_model")
    await state.advance("odoo.res.partner", datetime(2026, 8, 1, tzinfo=UTC))

    await state.reset("odoo.res.partner")

    assert await state.watermark("odoo.res.partner") is None


async def test_only_active_sources_are_listed(state: PgSourceState) -> None:
    await state.register("odoo.res.partner", "odoo_model", active=True)
    await state.register("odoo.sale.order", "odoo_model", active=False)

    assert await state.active_keys() == ["odoo.res.partner"]


async def test_an_unknown_source_has_no_watermark(state: PgSourceState) -> None:
    assert await state.watermark("odoo.never.registered") is None
