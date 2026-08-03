"""The ingestion queue, against real PostgreSQL.

``FOR UPDATE SKIP LOCKED`` is the whole design, and it cannot be tested against
a fake: the property being relied on is that two transactions polling the same
table at the same instant get different rows. Only a real database has that.
"""

from __future__ import annotations

import asyncio

import pytest
from psycopg_pool import AsyncConnectionPool

from atlas.domain.ingestion import JobKind
from atlas.infrastructure.persistence.job_queue import PgJobQueue

pytestmark = pytest.mark.integration


@pytest.fixture
async def queue(pool: AsyncConnectionPool) -> PgJobQueue:
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("TRUNCATE ingest_jobs")
    return PgJobQueue(pool, max_attempts=3, backoff_seconds=1.0)


async def test_a_queued_job_comes_back_with_what_it_was_queued_with(queue: PgJobQueue) -> None:
    job_id = await queue.enqueue("odoo.res.partner", JobKind.FULL_SYNC, payload={"ids": [1, 2]})

    claimed = await queue.claim("worker-1")

    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.source_key == "odoo.res.partner"
    assert claimed.kind is JobKind.FULL_SYNC
    assert claimed.payload == {"ids": [1, 2]}
    assert claimed.attempts == 1


async def test_an_empty_queue_gives_nothing_rather_than_waiting(queue: PgJobQueue) -> None:
    assert await queue.claim("worker-1") is None


async def test_two_workers_never_get_the_same_job(queue: PgJobQueue) -> None:
    """The property the whole queue rests on."""
    for index in range(6):
        await queue.enqueue(f"source-{index}", JobKind.INCREMENTAL)

    claims = await asyncio.gather(*(queue.claim(f"worker-{n}") for n in range(6)))

    ids = [job.id for job in claims if job is not None]
    assert len(ids) == len(set(ids))
    assert len(ids) == 6


async def test_a_claimed_job_is_not_offered_again(queue: PgJobQueue) -> None:
    await queue.enqueue("odoo.res.partner", JobKind.INCREMENTAL)

    first = await queue.claim("worker-1")
    second = await queue.claim("worker-2")

    assert first is not None
    assert second is None


async def test_a_failure_schedules_a_retry_in_the_future(queue: PgJobQueue) -> None:
    await queue.enqueue("odoo.res.partner", JobKind.INCREMENTAL)
    job = await queue.claim("worker-1")
    assert job is not None

    outcome = await queue.fail(job.id, "Odoo said no")

    assert outcome.dead is False
    assert outcome.attempts == 1
    assert outcome.run_after is not None
    # Backed off, so it is not immediately claimable again.
    assert await queue.claim("worker-1") is None


async def test_a_job_that_keeps_failing_eventually_gives_up(queue: PgJobQueue) -> None:
    """``dead`` is deliberately not ``failed``: one is still trying, one is not."""
    job_id = await queue.enqueue("odoo.res.partner", JobKind.INCREMENTAL)

    outcome = None
    for _attempt in range(3):
        await _make_due(queue, job_id)
        job = await queue.claim("worker-1")
        assert job is not None
        outcome = await queue.fail(job.id, "still broken")

    assert outcome is not None
    assert outcome.dead is True
    assert outcome.attempts == 3
    await _make_due(queue, job_id)
    assert await queue.claim("worker-1") is None


async def test_a_succeeded_job_leaves_the_queue(queue: PgJobQueue) -> None:
    await queue.enqueue("odoo.res.partner", JobKind.INCREMENTAL)
    job = await queue.claim("worker-1")
    assert job is not None

    await queue.succeed(job.id)

    assert await queue.claim("worker-1") is None


async def test_a_job_whose_worker_died_is_returned_to_the_queue(
    queue: PgJobQueue, pool: AsyncConnectionPool
) -> None:
    await queue.enqueue("odoo.res.partner", JobKind.INCREMENTAL)
    job = await queue.claim("worker-1")
    assert job is not None
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE ingest_jobs SET locked_at = now() - interval '1 hour' WHERE id = %s",
            (job.id,),
        )

    released = await queue.release_stale(older_than_seconds=60)

    assert released == 1
    recovered = await queue.claim("worker-2")
    assert recovered is not None
    # The burned attempt is not refunded: a job that reliably kills its worker
    # must still reach `dead` rather than crash-looping the whole pool.
    assert recovered.attempts == 2


async def test_a_stale_job_that_has_run_out_of_attempts_is_declared_dead(
    queue: PgJobQueue, pool: AsyncConnectionPool
) -> None:
    job_id = await queue.enqueue("odoo.res.partner", JobKind.INCREMENTAL)
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            """
            UPDATE ingest_jobs
            SET status = 'running', attempts = 3, locked_at = now() - interval '1 hour'
            WHERE id = %s
            """,
            (job_id,),
        )

    await queue.release_stale(older_than_seconds=60)

    assert await queue.claim("worker-1") is None


async def test_jobs_are_claimed_oldest_first(queue: PgJobQueue) -> None:
    first = await queue.enqueue("source-a", JobKind.INCREMENTAL)
    second = await queue.enqueue("source-b", JobKind.INCREMENTAL)

    claimed_first = await queue.claim("worker-1")
    claimed_second = await queue.claim("worker-1")

    assert claimed_first is not None
    assert claimed_second is not None
    assert [claimed_first.id, claimed_second.id] == [first, second]


async def _make_due(queue: PgJobQueue, job_id: int) -> None:
    """Bring a backed-off job forward, so a test need not wait for its retry."""
    async with queue._pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE ingest_jobs SET run_after = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
