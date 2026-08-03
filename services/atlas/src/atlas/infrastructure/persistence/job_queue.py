"""The ingestion job queue, in PostgreSQL.

``SELECT ... FOR UPDATE SKIP LOCKED`` is the whole trick. It gives a
transactional queue several workers can drain concurrently, with no broker and
no extra container: the row lock *is* the claim, and it is released by the same
commit that records the outcome. There is no window in which a job is claimed
but not recorded as claimed.

Explicit SQL over the shared pool, per ADR-0008.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from atlas.domain.errors import StorageError
from atlas.domain.ingestion import IngestJob, JobKind, JobStatus, RetryOutcome

logger = logging.getLogger(__name__)

#: Attempts before a job is declared dead. Three is enough to ride out a restart
#: or a rate limit, and few enough that a genuinely broken job surfaces the same
#: working day rather than retrying all week.
DEFAULT_MAX_ATTEMPTS: Final = 5

#: Backoff base in seconds; the wait is `base * 2 ** (attempts - 1)`, capped.
DEFAULT_BACKOFF_SECONDS: Final = 30.0
MAX_BACKOFF_SECONDS: Final = 3600.0


class PgJobQueue:
    """A :class:`~atlas.domain.ports.ingestion.JobQueue` over PostgreSQL."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._pool = pool
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    async def enqueue(
        self,
        source_key: str,
        kind: JobKind,
        *,
        payload: Mapping[str, Any] | None = None,
        run_after: datetime | None = None,
    ) -> int:
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO ingest_jobs (source_key, kind, payload, run_after)
                    VALUES (%s, %s, %s, COALESCE(%s, now()))
                    RETURNING id
                    """,
                    (source_key, str(kind), json.dumps(dict(payload or {})), run_after),
                )
                row = await cursor.fetchone()
                return int(row[0])  # type: ignore[index]
        except psycopg.Error as exc:
            error = _storage_error("enqueue", exc)
            raise error from exc

    async def claim(self, worker: str) -> IngestJob | None:
        """Take the next due job.

        The subquery locks exactly one row and skips any another worker already
        holds, so concurrent pollers never collide and never wait on each other.
        """
        try:
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
                connection.cursor(row_factory=dict_row) as cursor,
            ):
                await cursor.execute(
                    """
                    UPDATE ingest_jobs
                    SET status    = 'running',
                        locked_at = now(),
                        locked_by = %s,
                        attempts  = attempts + 1
                    WHERE id = (
                        SELECT id
                        FROM ingest_jobs
                        WHERE status = 'pending' AND run_after <= now()
                        ORDER BY run_after, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING id, source_key, kind, payload, attempts
                    """,
                    (worker,),
                )
                row = await cursor.fetchone()
        except psycopg.Error as exc:
            error = _storage_error("claim", exc)
            raise error from exc

        if row is None:
            return None
        return IngestJob(
            id=int(row["id"]),
            source_key=row["source_key"],
            kind=JobKind(row["kind"]),
            payload=row["payload"] or {},
            attempts=int(row["attempts"]),
            status=JobStatus.RUNNING,
        )

    async def succeed(self, job_id: int) -> None:
        await self._finish(
            job_id,
            "UPDATE ingest_jobs SET status = 'succeeded', locked_at = NULL,"
            " locked_by = NULL, last_error = NULL WHERE id = %s",
        )

    async def fail(self, job_id: int, error: str) -> RetryOutcome:
        """Record a failure, and either schedule a retry or give up.

        The decision is made in SQL against the row's own attempt count rather
        than from anything the caller passes, so two workers reporting the same
        job cannot disagree about how many tries it has had.
        """
        try:
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
                connection.cursor(row_factory=dict_row) as cursor,
            ):
                await cursor.execute(
                    """
                    UPDATE ingest_jobs
                    SET status = CASE WHEN attempts >= %(max_attempts)s
                                      THEN 'dead' ELSE 'pending' END,
                        run_after = CASE WHEN attempts >= %(max_attempts)s
                                         THEN run_after
                                         ELSE now() + make_interval(
                                             secs => LEAST(
                                                 %(backoff)s * power(2, attempts - 1),
                                                 %(max_backoff)s
                                             )
                                         ) END,
                        last_error = %(error)s,
                        locked_at  = NULL,
                        locked_by  = NULL
                    WHERE id = %(job_id)s
                    RETURNING attempts, status, run_after
                    """,
                    {
                        "job_id": job_id,
                        # Truncated: a driver traceback can be enormous, and the
                        # useful part is at the front.
                        "error": error[:2000],
                        "max_attempts": self._max_attempts,
                        "backoff": self._backoff_seconds,
                        "max_backoff": MAX_BACKOFF_SECONDS,
                    },
                )
                row = await cursor.fetchone()
        except psycopg.Error as exc:
            # Not named `error`: that is this method's own parameter, and
            # shadowing it here would read as if the message were being reused.
            failure = _storage_error("fail", exc)
            raise failure from exc

        if row is None:
            message = f"ingest job {job_id} vanished while being failed"
            raise StorageError(message, context={"job_id": job_id})

        dead = row["status"] == JobStatus.DEAD
        if dead:
            logger.error(
                "ingest job gave up",
                extra={"job_id": job_id, "attempts": row["attempts"]},
            )
        return RetryOutcome(
            attempts=int(row["attempts"]),
            dead=dead,
            run_after=None if dead else row["run_after"],
        )

    async def release_stale(self, older_than_seconds: float) -> int:
        """Return jobs whose worker died back to the queue.

        The burned attempt is not refunded. A job that reliably kills its worker
        must still reach ``dead``, or a poisoned payload becomes an infinite
        crash loop across the whole worker pool.
        """
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE ingest_jobs
                    SET status = CASE WHEN attempts >= %(max_attempts)s
                                      THEN 'dead' ELSE 'pending' END,
                        locked_at = NULL,
                        locked_by = NULL,
                        last_error = COALESCE(last_error, 'worker vanished while holding this job')
                    WHERE status = 'running'
                      AND locked_at < now() - make_interval(secs => %(age)s)
                    """,
                    {"age": older_than_seconds, "max_attempts": self._max_attempts},
                )
                released = cursor.rowcount
        except psycopg.Error as exc:
            error = _storage_error("release_stale", exc)
            raise error from exc

        if released:
            logger.warning("released stale ingest jobs", extra={"count": released})
        return released

    async def _finish(self, job_id: int, statement: str) -> None:
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(statement, (job_id,))
        except psycopg.Error as exc:
            error = _storage_error("finish", exc)
            raise error from exc


def _storage_error(operation: str, exc: psycopg.Error) -> StorageError:
    logger.warning("job queue operation failed", extra={"operation": operation})
    return StorageError(str(exc), context={"operation": operation})
