"""``make bench``: measure what retrieval actually costs, and what it finds.

Two numbers matter and they trade against each other.

**Recall** here is not the golden-set recall that ``make eval`` reports. That one
asks "did retrieval find the documents a person labelled relevant". This one asks
a narrower and more mechanical question: **did the approximate index return the
same rows an exact scan would**. It is the only recall an index parameter can be
tuned against, because it isolates the index from every judgement about
relevance.

**Latency** is measured per query, reported as p50 and p95, from inside the
database session. Client-side timing would fold in the round-trip and the driver,
which are real costs but not the ones an index parameter moves.

The corpus is generated rather than borrowed. Vectors are deterministic from a
seed, so a sweep is reproducible, and the row count is a flag so the same command
answers "what does this cost at 10k chunks" and "at 200k". Real ERP text would
make the lexical numbers more honest and the vector numbers no different — the
index does not know what the vectors mean.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any, TextIO

import psycopg
from pgvector import Vector as PgVector
from psycopg import sql

from atlas.config.logging import configure_logging
from atlas.infrastructure.persistence import register_vector

logger = logging.getLogger(__name__)

#: Where the benchmark writes. Never the configured database: this creates and
#: drops tables and would otherwise eat somebody's corpus.
DEFAULT_TABLE = "bench_chunks"

#: Vector width, matching the default embedding model.
DIMENSIONS = 1536

#: Neighbours asked for. Eight is what retrieval serves.
TOP_K = 8

#: Topics in the synthetic corpus. Real embeddings cluster by subject, and that
#: structure is the thing an approximate index navigates.
CLUSTERS = 1200

#: Queries per measurement. Enough for a p95 to mean something without making a
#: sweep take longer than anyone will wait for.
QUERIES = 50

#: The sweep. `m` is edges per node — build cost and memory. `ef_construction`
#: is how hard the builder looks for good edges. `ef_search` is the query-time
#: dial, and the only one that can be changed without a rebuild.
BUILD_PARAMETERS = ((16, 64), (16, 128), (32, 128))
SEARCH_PARAMETERS = (10, 20, 40, 60, 100, 200)


@dataclass(frozen=True, slots=True)
class Measurement:
    """One (m, ef_construction, ef_search) point on the curve."""

    m: int
    ef_construction: int
    ef_search: int
    recall: float
    p50_ms: float
    p95_ms: float
    build_seconds: float
    index_mb: float

    def as_dict(self) -> dict[str, float | int]:
        """The row, for `--json`."""
        return {
            "m": self.m,
            "ef_construction": self.ef_construction,
            "ef_search": self.ef_search,
            "recall_at_k": round(self.recall, 4),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "build_seconds": round(self.build_seconds, 1),
            "index_mb": round(self.index_mb, 1),
        }


async def seed(connection: psycopg.AsyncConnection[Any], table: str, rows: int) -> None:
    """Build a corpus of ``rows`` deterministic vectors.

    Generated in the database rather than sent over the wire. A million floats
    per thousand rows is a lot of protocol for data whose only requirement is
    that it be the same every time.
    """
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
        """).format(table=sql.Identifier(table), dimensions=sql.Literal(DIMENSIONS))
    )
    # `setseed` makes `random()` reproducible for the session, so two runs of
    # this benchmark compare like with like.
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
                -- A point near one of {clusters} centroids, not a point drawn
                -- from nowhere.
                --
                -- Two earlier attempts produced tables that measured nothing.
                -- Uniform `random()` puts every vector in the positive orthant,
                -- where all pairs sit at ~0.75 cosine similarity. Isotropic
                -- Gaussian spreads over the sphere but leaves every point
                -- equidistant from every other, so "the true top 8" is decided
                -- by noise and recall came out 1.000 at every ef_search — a
                -- flat line, and not the one that was being looked for.
                --
                -- Real embeddings cluster: documents about the same thing land
                -- near each other. That structure is what an approximate index
                -- navigates, and without it there is nothing to be approximate
                -- about. Box-Muller for the centroid offset, a tenth of the
                -- spread for the within-cluster noise.
                SELECT array_agg(
                    sqrt(-2 * ln(greatest(random(), 1e-12)))
                        * cos(2 * pi() * random())
                        -- Per-point radius. With one fixed radius every member
                        -- of a cluster sits the same distance from its centroid,
                        -- so a query near that centroid sees ~170 near-exact
                        -- ties and "the true top 8" is decided by rounding.
                        -- Recall then measures tie-breaking, not the index.
                        * (0.02 + 0.30 * ((hashtext('r' || i::text) / 2147483648.0 + 1.0) / 2.0))
                    + centroid.component
                )::vector
                FROM generate_series(1, {dimensions}) AS d
                CROSS JOIN LATERAL (
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
            dimensions=sql.Literal(DIMENSIONS),
            rows=sql.Literal(rows),
            clusters=sql.Literal(CLUSTERS),
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
    await connection.execute(
        sql.SQL("CREATE INDEX ON {} (company_id, visibility)").format(sql.Identifier(table))
    )
    await connection.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))


async def build_index(
    connection: psycopg.AsyncConnection[Any], table: str, m: int, ef_construction: int
) -> tuple[float, float]:
    """Build the HNSW index, returning build seconds and its size in MB."""
    index_name = f"{table}_hnsw_idx"
    await connection.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(index_name)))
    # HNSW builds in memory or spills catastrophically.
    await connection.execute("SET maintenance_work_mem = '1GB'")

    started = time.perf_counter()
    await connection.execute(
        sql.SQL("""
        CREATE INDEX {index} ON {table}
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = {m}, ef_construction = {ef_construction})
        """).format(
            index=sql.Identifier(index_name),
            table=sql.Identifier(table),
            m=sql.Literal(m),
            ef_construction=sql.Literal(ef_construction),
        )
    )
    elapsed = time.perf_counter() - started

    cursor = await connection.execute(
        "SELECT pg_relation_size(%s::regclass) / (1024.0 * 1024.0)", (index_name,)
    )
    row = await cursor.fetchone()
    return elapsed, float(row[0]) if row else 0.0


async def measure(  # noqa: PLR0913 - two connections and three dials
    connection: psycopg.AsyncConnection[Any],
    exact_connection: psycopg.AsyncConnection[Any],
    table: str,
    *,
    ef_search: int,
    queries: int,
    top_k: int,
) -> tuple[float, list[float]]:
    """Recall against exact search, and per-query latencies in milliseconds.

    Ground truth comes from a **second connection** with index scans disabled
    for the whole session, rather than from toggling ``SET LOCAL`` around each
    query on one connection. Toggling worked in isolation and silently stopped
    working across a transaction boundary mid-sweep, producing a table where
    recall alternated between 1.000 at 105 ms — both queries sequentially
    scanning — and 0.000. Two connections cannot drift into each other's state.
    """
    hits = 0
    wanted = 0
    latencies: list[float] = []

    nearest = sql.SQL("SELECT id FROM {} ORDER BY embedding <=> %s LIMIT %s").format(
        sql.Identifier(table)
    )
    await connection.execute(sql.SQL("SET hnsw.ef_search = {}").format(sql.Literal(ef_search)))

    for index in range(queries):
        # A fresh point, not a row from the table. Probing with an indexed
        # vector makes the search trivially easy: the graph entry lands on the
        # probe itself and its true neighbours are its own edges. A real query
        # is a question's embedding — a point the index has never seen.
        probe = PgVector(_probe_vector(index))

        cursor = await exact_connection.execute(nearest, (probe, top_k))
        exact = {record[0] for record in await cursor.fetchall()}

        started = time.perf_counter()
        cursor = await connection.execute(nearest, (probe, top_k))
        approximate = {record[0] for record in await cursor.fetchall()}
        latencies.append((time.perf_counter() - started) * 1000)

        hits += len(exact & approximate)
        wanted += len(exact)

    return (hits / wanted if wanted else 0.0), latencies


async def explain(connection: psycopg.AsyncConnection[Any], table: str) -> str:
    """`EXPLAIN (ANALYZE, BUFFERS)` for the two queries retrieval actually runs."""
    cursor = await connection.execute(
        sql.SQL("SELECT embedding FROM {} LIMIT 1").format(sql.Identifier(table))
    )
    row = await cursor.fetchone()
    probe = row[0] if row else None

    await connection.execute("SET LOCAL hnsw.ef_search = 40")
    plans = []

    cursor = await connection.execute(
        sql.SQL("""
        EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
        SELECT id FROM {table}
        WHERE company_id = 1 AND visibility >= 1
        ORDER BY embedding <=> %s LIMIT {top_k}
        """).format(table=sql.Identifier(table), top_k=sql.Literal(TOP_K)),
        (probe,),
    )
    plans.append("Dense search (HNSW, with the company pre-filter)")
    plans.extend("  " + _trim(record[0]) for record in await cursor.fetchall())

    cursor = await connection.execute(
        sql.SQL("""
        EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
        SELECT id, ts_rank_cd(content_tsv, query) AS rank
        FROM {table}, plainto_tsquery('english', 'order S00042 customer') AS query
        WHERE content_tsv @@ query AND company_id = 1
        ORDER BY rank DESC LIMIT {top_k}
        """).format(table=sql.Identifier(table), top_k=sql.Literal(TOP_K))
    )
    plans.append("")
    plans.append("Lexical search (GIN)")
    plans.extend("  " + _trim(record[0]) for record in await cursor.fetchall())

    return "\n".join(plans)


async def sweep(
    dsn: str, *, rows: int, queries: int, table: str, top_k: int = TOP_K
) -> list[Measurement]:
    """Seed once, then build and measure each parameter combination."""
    async with (
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection,
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as exact_connection,
    ):
        await register_vector(connection)
        await register_vector(exact_connection)
        # Force the index on the timed connection. At 20k rows the planner
        # sometimes prefers a sequential scan, and a sweep that silently timed
        # the scan for two builds and the index for the third produced a table
        # where `m=32` looked 200x faster than `m=16`. This measures the index;
        # whether the planner would *choose* it is what the EXPLAIN section
        # below reports.
        await connection.execute("SET enable_seqscan = off")
        # Ground truth, for the whole session. Nothing here ever turns it back
        # on, which is the point.
        await exact_connection.execute("SET enable_indexscan = off")
        await exact_connection.execute("SET enable_bitmapscan = off")

        _write(f"seeding {rows:,} rows…")
        started = time.perf_counter()
        await seed(connection, table, rows)
        _write(f"seeded in {time.perf_counter() - started:.1f}s\n")

        results = []
        for m, ef_construction in BUILD_PARAMETERS:
            _write(f"building m={m} ef_construction={ef_construction}…")
            build_seconds, index_mb = await build_index(connection, table, m, ef_construction)
            _write(f"  built in {build_seconds:.1f}s, {index_mb:.1f} MB")

            for ef_search in SEARCH_PARAMETERS:
                recall, latencies = await measure(
                    connection,
                    exact_connection,
                    table,
                    ef_search=ef_search,
                    queries=queries,
                    top_k=top_k,
                )
                results.append(
                    Measurement(
                        m=m,
                        ef_construction=ef_construction,
                        ef_search=ef_search,
                        recall=recall,
                        p50_ms=statistics.median(latencies),
                        p95_ms=_percentile(latencies, 95),
                        build_seconds=build_seconds,
                        index_mb=index_mb,
                    )
                )
                _write(
                    f"    ef_search={ef_search:<4} recall={recall:.3f} "
                    f"p50={results[-1].p50_ms:.2f}ms p95={results[-1].p95_ms:.2f}ms"
                )

        _write("\n" + await explain(connection, table))
        await connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
        await connection.commit()
        return results


def render(results: list[Measurement], *, rows: int, top_k: int = TOP_K) -> str:
    """The table that goes into the docs."""
    lines = [
        "",
        f"HNSW sweep — {rows:,} chunks, {DIMENSIONS}-d vectors, top-{top_k}",
        "=" * 74,
        (
            f"{'m':>3} {'ef_c':>5} {'ef_s':>5} {'recall':>8} {'p50 ms':>8} "
            f"{'p95 ms':>8} {'build s':>8} {'index MB':>9}"
        ),
        "-" * 74,
    ]
    lines.extend(
        f"{r.m:>3} {r.ef_construction:>5} {r.ef_search:>5} {r.recall:>8.3f} "
        f"{r.p50_ms:>8.2f} {r.p95_ms:>8.2f} {r.build_seconds:>8.1f} {r.index_mb:>9.1f}"
        for r in results
    )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The command line, kept separate so a test can parse without running."""
    parser = argparse.ArgumentParser(
        prog="atlas-bench",
        description="Sweep HNSW parameters and report recall against exact search.",
    )
    parser.add_argument("--rows", type=int, default=20_000, help="Corpus size.")
    parser.add_argument("--queries", type=int, default=QUERIES, help="Queries per point.")
    parser.add_argument(
        "--top-k",
        type=int,
        default=TOP_K,
        help=(
            f"Neighbours to ask for. Retrieval serves {TOP_K}; a larger k gives an "
            "approximate index more room to miss, which is where the recall curve "
            "becomes visible."
        ),
    )
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Scratch table name.")
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="Print JSON, not a table."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the sweep. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    configure_logging(level="WARNING", json_output=False)

    dsn = os.environ.get("ATLAS_BENCH_DATABASE_URL") or os.environ.get("ATLAS_DATABASE__URL")
    if not dsn:
        _write(
            "Set ATLAS_BENCH_DATABASE_URL to a database this may create and drop "
            "tables in. It is never the configured corpus.",
            stream=sys.stderr,
        )
        return 2

    results = asyncio.run(
        sweep(dsn, rows=args.rows, queries=args.queries, table=args.table, top_k=args.top_k)
    )
    if args.as_json:
        _write(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        _write(render(results, rows=args.rows, top_k=args.top_k))
    return 0


def _trim(line: str, limit: int = 120) -> str:
    """Shorten a plan line.

    A sort key on a vector column prints all 1536 components, which buries the
    plan in six pages of floats.
    """
    return line if len(line) <= limit else line[:limit] + " …"


def _probe_vector(seed: int) -> list[float]:
    """A query point the index has never seen, deterministic from ``seed``.

    Normally distributed, matching how the corpus was generated, so probes sit
    in the same space as the data rather than off in a corner of it where every
    neighbour is equally far away.
    """
    # Near a centroid, like a question about something the corpus covers. A
    # probe drawn from nowhere is equidistant from everything, which is the
    # degenerate case this whole generator exists to avoid.
    centroid = random.Random(seed % CLUSTERS)  # noqa: S311 - a fixture, not a secret
    noise = random.Random(10_000 + seed)  # noqa: S311 - a fixture, not a secret
    return [centroid.gauss(0.0, 1.0) + noise.gauss(0.0, 0.1) for _ in range(DIMENSIONS)]


def _percentile(values: list[float], percentile: int) -> float:
    """The nth percentile, nearest-rank.

    Nearest-rank rather than interpolated: with 50 samples an interpolated p95
    invents a number between two measurements, and the honest answer is one of
    the measurements.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * percentile / 100), len(ordered) - 1)
    return ordered[index]


def _write(message: str, *, stream: TextIO | None = None) -> None:
    """Write a line for a human, as the sweep progresses."""
    print(message, file=stream or sys.stdout, flush=True)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
