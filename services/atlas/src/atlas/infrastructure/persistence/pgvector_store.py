"""PostgreSQL + pgvector implementation of the vector store.

Explicit SQL over the psycopg pool the composition root owns (ADR-0008). Every
statement here is one a reader can paste into ``psql`` and ``EXPLAIN ANALYZE``,
which is the point — M13's index tuning works on this text.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Final

import psycopg
from pgvector import Vector as PgVector
from pgvector.psycopg import register_vector_async
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from atlas.domain.corpus import (
    CandidateChunk,
    ChunkInput,
    Document,
    SearchFilter,
    Visibility,
)
from atlas.domain.embedding import Vector
from atlas.domain.errors import StorageError, ValidationError

logger = logging.getLogger(__name__)

#: What a filtered dense search needs, and why each half is needed.
#:
#: `iterative_scan` asks the HNSW walk to keep going until enough rows survive
#: the WHERE clause. Without it a filtered ANN query exhausts its candidate list
#: on rows the filter rejects and silently returns too few (ADR-0004) — measured
#: at 5.1 rows for a LIMIT of 8.
#:
#: The scan settings are the half that was missing. With a company filter
#: matching a third of the table, the planner costs a bitmap scan over the
#: `(company_id, visibility)` index below an HNSW walk and takes it — then sorts
#: sixteen thousand rows by distance. `iterative_scan` never applies, because it
#: only governs an index scan that was never chosen. Measured at 50k chunks:
#:
#:     planner free                126.95 ms p50   8.0 rows
#:     forced index, no iterative    2.82 ms p50   5.1 rows   ← incomplete
#:     forced index + iterative      3.94 ms p50   8.0 rows
#:
#: Scoped with SET LOCAL to the dense search's own transaction. The lexical
#: search *wants* a bitmap scan — that is how a GIN index is read — so this must
#: never leak onto it.
_DENSE_SCAN_SETTINGS: Final = (
    "SET LOCAL hnsw.iterative_scan = relaxed_order",
    "SET LOCAL enable_bitmapscan = off",
    "SET LOCAL enable_seqscan = off",
)

_CHUNK_COLUMNS: Final = """
    c.id, c.document_id, c.content, c.res_model, c.res_id,
    c.company_id, c.visibility, c.metadata, d.external_ref
"""

#: Matches the width in a rendered `vector(1536)` type.
_VECTOR_WIDTH: Final = re.compile(r"vector\((\d+)\)")


async def register_vector(connection: psycopg.AsyncConnection[Any]) -> None:
    """Teach a connection the ``vector`` type.

    Used as the pool's ``configure`` hook so every connection can bind a vector as
    a parameter. Formatting vectors into query text instead would defeat the
    prepared-statement cache and invite injection on a hot path.
    """
    await register_vector_async(connection)


class PgVectorStore:
    """A :class:`~atlas.domain.ports.vector_store.VectorStore` over pgvector."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def document_exists(self, source_hash: str) -> bool:
        row = await self._fetchone("SELECT 1 FROM documents WHERE source_hash = %s", (source_hash,))
        return row is not None

    async def upsert_document(self, document: Document, chunks: Sequence[ChunkInput]) -> int:
        _validate(document, chunks)

        try:
            # One transaction: a reader sees the old chunk set or the new one,
            # never a half-replaced document.
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
                connection.cursor() as cursor,
            ):
                await cursor.execute(
                    """
                        INSERT INTO documents (
                            source_key, source_hash, title, res_model, res_id,
                            external_ref, company_id, visibility, embedding_model,
                            embedding_dimensions, metadata, record_write_date, indexed_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (source_hash) DO UPDATE SET
                            source_key           = EXCLUDED.source_key,
                            title                = EXCLUDED.title,
                            res_model            = EXCLUDED.res_model,
                            res_id               = EXCLUDED.res_id,
                            external_ref         = EXCLUDED.external_ref,
                            company_id           = EXCLUDED.company_id,
                            visibility           = EXCLUDED.visibility,
                            embedding_model      = EXCLUDED.embedding_model,
                            embedding_dimensions = EXCLUDED.embedding_dimensions,
                            metadata             = EXCLUDED.metadata,
                            record_write_date    = EXCLUDED.record_write_date,
                            indexed_at           = now()
                        RETURNING id
                        """,
                    (
                        document.source_key,
                        document.source_hash,
                        document.title,
                        document.res_model,
                        document.res_id,
                        document.external_ref,
                        document.company_id,
                        int(document.visibility),
                        document.embedding_model,
                        document.embedding_dimensions,
                        json.dumps(dict(document.metadata)),
                        document.record_write_date,
                    ),
                )
                row = await cursor.fetchone()
                document_id = int(row[0])  # type: ignore[index]

                # An edited record renders differently, so it hashes differently,
                # so the INSERT above created a *new* row rather than updating
                # the old one. Without this, the previous version's chunks would
                # linger and the record would be retrievable twice — once as it
                # is and once as it was. Same transaction as the insert, so a
                # reader sees one version or the other and never both.
                if document.res_model and document.res_id:
                    await cursor.execute(
                        """
                        DELETE FROM documents
                        WHERE res_model = %s AND res_id = %s AND id <> %s
                        """,
                        (document.res_model, document.res_id, document_id),
                    )

                # Replace rather than merge: an edit that shortens a document
                # must not leave the tail of the previous version behind.
                await cursor.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))

                for chunk in chunks:
                    await cursor.execute(
                        """
                            INSERT INTO chunks (
                                document_id, ordinal, content, token_count, embedding,
                                res_model, res_id, company_id, visibility, metadata
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                        (
                            document_id,
                            chunk.ordinal,
                            chunk.content,
                            chunk.token_count,
                            PgVector(list(chunk.embedding)),
                            document.res_model,
                            document.res_id,
                            document.company_id,
                            int(document.visibility),
                            json.dumps(dict(chunk.metadata)),
                        ),
                    )
        except psycopg.Error as exc:
            error = _storage_error("upsert_document", exc)
            raise error from exc

        return len(chunks)

    async def delete_record(self, res_model: str, res_id: int) -> int:
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                # Chunks go with the document: the foreign key cascades.
                await cursor.execute(
                    "DELETE FROM documents WHERE res_model = %s AND res_id = %s",
                    (res_model, res_id),
                )
                return cursor.rowcount
        except psycopg.Error as exc:
            error = _storage_error("delete_record", exc)
            raise error from exc

    async def search_dense(
        self,
        embedding: Vector,
        *,
        limit: int,
        filters: SearchFilter | None = None,
    ) -> list[CandidateChunk]:
        clauses, params = _filter_clauses(filters)

        # `<=>` is cosine distance, so smaller is closer. Converting to a score
        # here means every search mode returns "higher is better" and no caller
        # has to remember which direction a given operator sorts in.
        statement = sql.SQL(
            """
            SELECT {columns}, 1 - (c.embedding <=> %(embedding)s) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            {where}
            ORDER BY c.embedding <=> %(embedding)s
            LIMIT %(limit)s
            """
        ).format(columns=sql.SQL(_CHUNK_COLUMNS), where=_where(clauses))

        # Bound as a pgvector value, not a Python list: a list adapts to
        # `double precision[]`, and `<=>` has no operator for that.
        return await self._search(
            statement,
            {**params, "embedding": PgVector(list(embedding)), "limit": limit},
            iterative=True,
        )

    async def search_lexical(
        self,
        query: str,
        *,
        limit: int,
        filters: SearchFilter | None = None,
    ) -> list[CandidateChunk]:
        clauses, params = _filter_clauses(filters)
        # The match itself is a filter clause, so the WHERE is built once and
        # there is no "is the clause list empty" special case.
        clauses.append(sql.SQL("c.content_tsv @@ q.query"))

        # `websearch_to_tsquery` accepts what a person actually types — quoted
        # phrases, OR, leading minus — instead of raising on the punctuation that
        # `to_tsquery` rejects.
        statement = sql.SQL(
            """
            SELECT {columns}, ts_rank_cd(c.content_tsv, q.query) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            CROSS JOIN websearch_to_tsquery('english', %(query)s) AS q(query)
            {where}
            ORDER BY score DESC
            LIMIT %(limit)s
            """
        ).format(columns=sql.SQL(_CHUNK_COLUMNS), where=_where(clauses))

        return await self._search(statement, {**params, "query": query, "limit": limit})

    async def embedding_dimensions(self) -> int:
        # `format_type` renders the declared type as `vector(1536)`. Reading the
        # rendered type avoids depending on how pgvector encodes atttypmod, which
        # is an implementation detail of the extension.
        row = await self._fetchone(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class t ON t.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE t.relname = 'chunks'
              AND a.attname = 'embedding'
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND n.nspname = current_schema()
            """
        )
        if row is None:
            msg = "the chunks table has no embedding column; has the migration run?"
            raise StorageError(msg)

        rendered = str(row[0])
        match = _VECTOR_WIDTH.search(rendered)
        if match is None:
            msg = f"unexpected embedding column type {rendered!r}"
            raise StorageError(msg)
        return int(match.group(1))

    async def _search(
        self,
        statement: sql.Composed,
        params: dict[str, Any],
        *,
        iterative: bool = False,
    ) -> list[CandidateChunk]:
        try:
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
                connection.cursor(row_factory=dict_row) as cur,
            ):
                if iterative:
                    # SET LOCAL needs a transaction; the block above provides one.
                    for setting in _DENSE_SCAN_SETTINGS:
                        await cur.execute(setting)
                await cur.execute(statement, params)
                rows = await cur.fetchall()
        except psycopg.Error as exc:
            error = _storage_error("search", exc)
            raise error from exc

        return [_to_candidate(row) for row in rows]

    async def _fetchone(
        self, statement: str, params: tuple[Any, ...] = ()
    ) -> tuple[Any, ...] | None:
        try:
            async with self._pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute(statement, params)
                return await cursor.fetchone()
        except psycopg.Error as exc:
            error = _storage_error("query", exc)
            raise error from exc


def _validate(document: Document, chunks: Sequence[ChunkInput]) -> None:
    """Reject a write whose vectors do not match the declared width.

    PostgreSQL would reject it too, but with a message about column types. Failing
    here names the chunk and both widths, which is what an operator needs.
    """
    for chunk in chunks:
        if len(chunk.embedding) != document.embedding_dimensions:
            msg = (
                f"chunk {chunk.ordinal} has a {len(chunk.embedding)}-d embedding, "
                f"but the document declares {document.embedding_dimensions}"
            )
            raise ValidationError(msg, context={"source_hash": document.source_hash})


def _filter_clauses(
    filters: SearchFilter | None,
) -> tuple[list[sql.Composable], dict[str, Any]]:
    """Build the pre-filter clauses.

    Non-authoritative by design: this narrows a candidate set cheaply, and Odoo
    settles whether the acting user may see any of it (ADR-0006).
    """
    clauses: list[sql.Composable] = []
    params: dict[str, Any] = {}

    if filters is None:
        return clauses, params

    if filters.company_ids:
        clauses.append(sql.SQL("c.company_id = ANY(%(company_ids)s)"))
        params["company_ids"] = list(filters.company_ids)

    if filters.max_visibility != Visibility.RESTRICTED:
        clauses.append(sql.SQL("c.visibility <= %(max_visibility)s"))
        params["max_visibility"] = int(filters.max_visibility)

    if filters.res_models:
        clauses.append(sql.SQL("c.res_model = ANY(%(res_models)s)"))
        params["res_models"] = list(filters.res_models)

    return clauses, params


def _where(clauses: list[sql.Composable]) -> sql.Composable:
    """Render clauses as a WHERE, or nothing at all."""
    if not clauses:
        return sql.SQL("")
    return sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(clauses))


def _to_candidate(row: dict[str, Any]) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=row["id"],
        document_id=row["document_id"],
        content=row["content"],
        score=float(row["score"]),
        res_model=row["res_model"],
        res_id=row["res_id"],
        company_id=row["company_id"],
        visibility=Visibility(row["visibility"]),
        external_ref=row["external_ref"],
        metadata=row["metadata"] or {},
    )


def _storage_error(operation: str, exc: psycopg.Error) -> StorageError:
    logger.warning("database operation failed", extra={"operation": operation})
    return StorageError(str(exc), context={"operation": operation})
