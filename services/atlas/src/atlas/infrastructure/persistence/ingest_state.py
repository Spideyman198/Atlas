"""The two pieces of state that make an incremental sync incremental.

The **embedding cache** keyed by ``(content_hash, model)`` stops us paying twice
for identical text. Boilerplate terms, repeated product descriptions and the
delivery paragraph on every order are far more common than they look, and after
a crash a re-run costs almost nothing.

The **watermark** on each source is the high-water mark of `write_date` we have
already seen, so a 15-minute cron reads the handful of records that moved rather
than the whole model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import psycopg
from pgvector import Vector as PgVector
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from atlas.domain.embedding import Vector
from atlas.domain.errors import StorageError

logger = logging.getLogger(__name__)


class PgEmbeddingCache:
    """Vectors we have already paid for, keyed by content and model.

    The model is part of the key because two models produce incompatible vector
    spaces for the same text. Sharing a cache across them would silently poison
    a corpus in a way no error message would ever mention.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_many(self, content_hashes: Sequence[str], model: str) -> dict[str, Vector]:
        """Look up whatever is already cached, in one round-trip."""
        if not content_hashes:
            return {}
        try:
            async with (
                self._pool.connection() as connection,
                connection.cursor(row_factory=dict_row) as cursor,
            ):
                await cursor.execute(
                    """
                    SELECT content_hash, embedding
                    FROM embedding_cache
                    WHERE model = %s AND content_hash = ANY(%s)
                    """,
                    (model, list(dict.fromkeys(content_hashes))),
                )
                rows = await cursor.fetchall()
        except psycopg.Error as exc:
            error = _storage_error("embedding_cache.get_many", exc)
            raise error from exc

        return {row["content_hash"]: _to_vector(row["embedding"]) for row in rows}

    async def put_many(self, entries: Mapping[str, Vector], model: str) -> int:
        """Store vectors, ignoring any another worker stored first."""
        if not entries:
            return 0
        try:
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
                connection.cursor() as cursor,
            ):
                for content_hash, vector in entries.items():
                    await cursor.execute(
                        """
                        INSERT INTO embedding_cache (content_hash, model, embedding)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (content_hash, model) DO NOTHING
                        """,
                        (content_hash, model, PgVector(list(vector))),
                    )
        except psycopg.Error as exc:
            error = _storage_error("embedding_cache.put_many", exc)
            raise error from exc
        return len(entries)


class PgSourceState:
    """Per-source configuration and the incremental watermark."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def register(
        self,
        source_key: str,
        kind: str,
        *,
        config: Mapping[str, Any] | None = None,
        active: bool = True,
    ) -> None:
        """Record that a source exists, leaving any watermark it already has.

        Idempotent: registration happens on every start-up and must not rewind a
        sync that has already made progress.
        """
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO ingest_sources (key, kind, config, active)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        kind   = EXCLUDED.kind,
                        config = EXCLUDED.config,
                        active = EXCLUDED.active
                    """,
                    (source_key, kind, json.dumps(dict(config or {})), active),
                )
        except psycopg.Error as exc:
            error = _storage_error("source_state.register", exc)
            raise error from exc

    async def watermark(self, source_key: str) -> datetime | None:
        """The newest ``write_date`` this source has already indexed."""
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT watermark FROM ingest_sources WHERE key = %s", (source_key,)
                )
                row = await cursor.fetchone()
        except psycopg.Error as exc:
            error = _storage_error("source_state.watermark", exc)
            raise error from exc
        return row[0] if row else None

    async def advance(self, source_key: str, watermark: datetime) -> None:
        """Move the watermark forward, never backward.

        Inserts the source if it is not there yet. An ``UPDATE`` alone matched
        nothing until somebody had called :meth:`register`, so the watermark was
        silently discarded and every "incremental" sync re-read the whole model
        — an inefficiency that costs nothing visible and shows up only as a
        database bill. Making this self-sufficient removes the ordering
        dependency rather than documenting it.

        The ``GREATEST`` guard matters when a full sync and an incremental run
        overlap: the full sync finishes on older records and must not undo the
        incremental run's progress.
        """
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO ingest_sources (key, kind, watermark)
                    VALUES (%(key)s, 'odoo_model', %(mark)s)
                    ON CONFLICT (key) DO UPDATE SET
                        watermark = GREATEST(
                            COALESCE(ingest_sources.watermark, %(mark)s), %(mark)s
                        )
                    """,
                    {"key": source_key, "mark": watermark},
                )
        except psycopg.Error as exc:
            error = _storage_error("source_state.advance", exc)
            raise error from exc

    async def reset(self, source_key: str) -> None:
        """Forget the watermark, so the next sync reads everything again."""
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE ingest_sources SET watermark = NULL WHERE key = %s", (source_key,)
                )
        except psycopg.Error as exc:
            error = _storage_error("source_state.reset", exc)
            raise error from exc

    async def active_keys(self) -> list[str]:
        """Every source that is switched on, in a stable order."""
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute("SELECT key FROM ingest_sources WHERE active ORDER BY key")
                rows = await cursor.fetchall()
        except psycopg.Error as exc:
            error = _storage_error("source_state.active_keys", exc)
            raise error from exc
        return [str(row[0]) for row in rows]


def _to_vector(value: Any) -> Vector:
    """Normalise whatever the driver hands back into our tuple of floats.

    A ``vector`` column decodes to :class:`pgvector.Vector`, which is **not**
    iterable — it exposes ``to_list()``. Assuming otherwise raises a ``TypeError``
    on the first cache hit, which is a long way from where the assumption was
    made. The fallback covers a plain sequence, in case a future release changes
    the decoded type again.
    """
    if isinstance(value, PgVector):
        return tuple(float(component) for component in value.to_list())
    return tuple(float(component) for component in value)


def _storage_error(operation: str, exc: psycopg.Error) -> StorageError:
    logger.warning("database operation failed", extra={"operation": operation})
    return StorageError(str(exc), context={"operation": operation})
