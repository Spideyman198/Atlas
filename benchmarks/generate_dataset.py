"""The corpus every other script measures against.

Synthetic, deterministic, generated in the database. A real ERP export would be
a better benchmark and a worse fixture: it cannot be committed, it moves under
you, and a number that changed because somebody confirmed an order says nothing
about an index.

## Getting the distribution right took five attempts

Each of the first four produced a table that measured nothing. They are recorded
because the failure mode is not obvious and the next person to touch this will
otherwise repeat one of them.

1. **Uniform `random()` components.** Every vector lands in the positive orthant.
   All pairs sit at roughly 0.75 cosine similarity, differing in the fourth
   decimal, so "the nearest eight" is decided by tie-breaking in the scan order.
   Recall measured the scan order.

2. **Isotropic Gaussian components.** Directions now spread over the sphere, but
   in 1536 dimensions every point is about as far from every other. Ground truth
   is again arbitrary. Recall came out 1.000 at every `ef_search`: a flat line,
   and not the one being looked for.

3. **Probing with vectors drawn from the table.** The graph entry point lands on
   the probe itself and its true neighbours are its own edges, so the search is
   trivially easy. Recall 1.000 again. A real query is a question's embedding —
   a point the index has never seen — which is what `probe_vector` now returns.

4. **Tight clusters, one radius.** Every member of a cluster sits the same
   distance from its centroid, so a query near that centroid sees roughly 170
   near-exact ties and the "true top 8" is decided by rounding.

5. **Clusters with a per-point radius.** What this file does. Produces a
   monotone recall curve that responds to both `ef_construction` and
   `ef_search`.

Residual near-ties still depress the absolute recall figures, which is why
`docs/performance.md` publishes them as a shape rather than as a prediction.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from typing import Any

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg import sql

from benchmarks import config


async def generate(
    connection: psycopg.AsyncConnection[Any],
    *,
    table: str = config.TABLE,
    rows: int = config.DEFAULT_ROWS,
) -> float:
    """Create ``table`` and fill it with ``rows`` chunks. Returns seconds taken.

    Vectors are built in the database rather than sent over the wire: a
    thousand rows is a million and a half floats, and the only requirement on
    them is that they be the same every run.
    """
    started = time.perf_counter()

    await connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
    await connection.execute(
        sql.SQL("""
        CREATE TABLE {table} (
            id bigserial PRIMARY KEY,
            company_id integer NOT NULL,
            visibility smallint NOT NULL,
            content text NOT NULL,
            content_tsv tsvector,
            embedding vector({dimensions}) NOT NULL
        )
        """).format(table=sql.Identifier(table), dimensions=sql.Literal(config.DIMENSIONS))
    )

    # Makes `random()` reproducible for this session, so two runs compare.
    await connection.execute("SELECT setseed(0.42)")
    await connection.execute(
        sql.SQL("""
        INSERT INTO {table} (company_id, visibility, content, embedding)
        SELECT
            1 + (i % 3),
            1,
            'chunk ' || i || ' order S' || lpad((i % 5000)::text, 5, '0')
                     || ' customer ' || (i % 400),
            (
                SELECT array_agg(
                    sqrt(-2 * ln(greatest(random(), 1e-12)))
                        * cos(2 * pi() * random())
                        -- Per-point radius. One fixed radius puts every member
                        -- of a cluster the same distance from its centroid, and
                        -- recall then measures tie-breaking (attempt 4 above).
                        * (0.02 + 0.30 * ((hashtext('r' || i::text) / 2147483648.0 + 1.0) / 2.0))
                    + centroid.component
                )::vector
                FROM generate_series(1, {dimensions}) AS d
                CROSS JOIN LATERAL (
                    -- Deterministic per (cluster, dimension): the same centroid
                    -- for every member, without storing one.
                    SELECT (
                        sqrt(-2 * ln(greatest(
                            (hashtext((i % {clusters})::text || ':' || d::text) / 2147483648.0
                             + 1.0) / 2.0, 1e-12)))
                        * cos(pi() * (hashtext((i % {clusters})::text || ':x' || d::text)
                                      / 2147483648.0 + 1.0))
                    ) AS component
                ) AS centroid
            )
        FROM generate_series(1, {rows}) AS i
        """).format(
            table=sql.Identifier(table),
            dimensions=sql.Literal(config.DIMENSIONS),
            rows=sql.Literal(rows),
            clusters=sql.Literal(config.CLUSTERS),
        )
    )

    await connection.execute(
        sql.SQL("UPDATE {} SET content_tsv = to_tsvector('english', content)").format(
            sql.Identifier(table)
        )
    )
    await connection.execute(
        sql.SQL("CREATE INDEX ON {} USING gin (content_tsv)").format(sql.Identifier(table))
    )
    # The pre-filter index. Its existence is what makes the planner prefer a
    # bitmap scan over the vector index — see `latency.py`.
    await connection.execute(
        sql.SQL("CREATE INDEX ON {} (company_id, visibility)").format(sql.Identifier(table))
    )
    await connection.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))

    return time.perf_counter() - started


async def build_index(
    connection: psycopg.AsyncConnection[Any],
    *,
    table: str = config.TABLE,
    m: int,
    ef_construction: int,
) -> tuple[float, float]:
    """Build the HNSW index. Returns ``(seconds, megabytes)``."""
    index = f"{table}_hnsw_idx"
    await connection.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(index)))
    # HNSW builds in memory or spills catastrophically.
    await connection.execute("SET maintenance_work_mem = '1GB'")

    started = time.perf_counter()
    await connection.execute(
        sql.SQL("""
        CREATE INDEX {index} ON {table}
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = {m}, ef_construction = {ef_construction})
        """).format(
            index=sql.Identifier(index),
            table=sql.Identifier(table),
            m=sql.Literal(m),
            ef_construction=sql.Literal(ef_construction),
        )
    )
    elapsed = time.perf_counter() - started

    cursor = await connection.execute(
        "SELECT pg_relation_size(%s::regclass) / (1024.0 * 1024.0)", (index,)
    )
    row = await cursor.fetchone()
    return elapsed, float(row[0]) if row else 0.0


def probe_vector(seed: int) -> list[float]:
    """A query point the index has never seen, deterministic from ``seed``.

    Placed near a centroid, the way a question about something the corpus covers
    would be. A probe drawn from nowhere is equidistant from everything, which
    is the degenerate case this module exists to avoid.
    """
    centroid = random.Random(seed % config.CLUSTERS)  # noqa: S311 - a fixture, not a secret
    noise = random.Random(10_000 + seed)  # noqa: S311 - a fixture, not a secret
    return [centroid.gauss(0.0, 1.0) + noise.gauss(0.0, 0.1) for _ in range(config.DIMENSIONS)]


async def connect(*, autocommit: bool = True) -> psycopg.AsyncConnection[Any]:
    """A connection with the `vector` type registered."""
    connection = await psycopg.AsyncConnection.connect(config.dsn(), autocommit=autocommit)
    await register_vector_async(connection)
    return connection


async def _main(rows: int, table: str) -> int:
    async with await connect() as connection:
        seconds = await generate(connection, table=table, rows=rows)
        cursor = await connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        )
        row = await cursor.fetchone()
    print(f"generated {row[0] if row else 0:,} rows in {table} in {seconds:.1f}s")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the benchmark corpus.")
    parser.add_argument("--rows", type=int, default=config.DEFAULT_ROWS)
    parser.add_argument("--table", default=config.TABLE)
    arguments = parser.parse_args()
    sys.exit(asyncio.run(_main(arguments.rows, arguments.table)))
