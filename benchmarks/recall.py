"""Recall of the HNSW index against exact search, across the parameter grid.

The question is narrow and mechanical: **does the approximate index return the
same rows a sequential scan would?** That is the only recall an index parameter
can be tuned against, because it holds relevance judgement out of it entirely.
The other recall — did retrieval find the documents a person labelled relevant —
is `make eval`, and answers a different question.

Ground truth comes from a **second connection** with index scans disabled for
the session, not from toggling `SET LOCAL` around each query on one connection.
Toggling worked in isolation and stopped working across a transaction boundary
mid-sweep, producing a table where recall alternated between 1.000 at 105 ms —
both queries sequentially scanning — and 0.000. Two connections cannot drift
into each other's state.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from pgvector import Vector
from psycopg import sql

from benchmarks import config, environment, results
from benchmarks.generate_dataset import build_index, connect, generate, probe_vector


@dataclass(frozen=True, slots=True)
class Point:
    """One `(m, ef_construction, ef_search)` measurement."""

    m: int
    ef_construction: int
    ef_search: int
    recall: float
    p50_ms: float
    p95_ms: float
    build_seconds: float
    index_mb: float


async def measure_point(  # noqa: PLR0913 - two connections and four dials
    approximate: psycopg.AsyncConnection[Any],
    exact: psycopg.AsyncConnection[Any],
    *,
    table: str,
    ef_search: int,
    queries: int,
    top_k: int,
) -> tuple[float, list[float]]:
    """Recall and per-query latencies for one `ef_search`."""
    nearest = sql.SQL("SELECT id FROM {} ORDER BY embedding <=> %s LIMIT %s").format(
        sql.Identifier(table)
    )
    await approximate.execute(sql.SQL("SET hnsw.ef_search = {}").format(sql.Literal(ef_search)))

    hits = 0
    wanted = 0
    latencies: list[float] = []

    for index in range(queries):
        probe = Vector(probe_vector(index))

        cursor = await exact.execute(nearest, (probe, top_k))
        truth = {row[0] for row in await cursor.fetchall()}

        started = time.perf_counter()
        cursor = await approximate.execute(nearest, (probe, top_k))
        found = {row[0] for row in await cursor.fetchall()}
        latencies.append((time.perf_counter() - started) * 1000)

        hits += len(truth & found)
        wanted += len(truth)

    return (hits / wanted if wanted else 0.0), latencies


async def sweep(
    *, rows: int, queries: int, table: str, top_k: int
) -> tuple[list[Point], dict[str, Any]]:
    """Generate a corpus, then build and measure every parameter combination."""
    async with await connect() as approximate, await connect() as exact:
        # Ground truth, for the whole session. Nothing turns it back on.
        await exact.execute("SET enable_indexscan = off")
        await exact.execute("SET enable_bitmapscan = off")
        # Force the index on the measured connection. At these row counts the
        # planner sometimes prefers a sequential scan, and a sweep that timed
        # the scan for two builds and the index for the third reported `m=32` as
        # 200x faster than `m=16`. Whether the planner would *choose* the index
        # is `latency.py`'s question, and a different one.
        await approximate.execute("SET enable_seqscan = off")

        captured = await environment.capture(approximate)
        print(environment.describe(captured))
        print(f"\ngenerating {rows:,} rows…", flush=True)
        seconds = await generate(approximate, table=table, rows=rows)
        print(f"generated in {seconds:.1f}s\n")

        points: list[Point] = []
        for m, ef_construction in config.BUILD_PARAMETERS:
            build_seconds, index_mb = await build_index(
                approximate, table=table, m=m, ef_construction=ef_construction
            )
            print(
                f"m={m} ef_construction={ef_construction}: "
                f"built in {build_seconds:.1f}s, {index_mb:.1f} MB"
            )
            for ef_search in config.SEARCH_PARAMETERS:
                recall, latencies = await measure_point(
                    approximate,
                    exact,
                    table=table,
                    ef_search=ef_search,
                    queries=queries,
                    top_k=top_k,
                )
                points.append(
                    Point(
                        m=m,
                        ef_construction=ef_construction,
                        ef_search=ef_search,
                        recall=round(recall, 4),
                        p50_ms=round(statistics.median(latencies), 2),
                        p95_ms=round(results.percentile(latencies, 95), 2),
                        build_seconds=round(build_seconds, 1),
                        index_mb=round(index_mb, 1),
                    )
                )
                print(
                    f"  ef_search={ef_search:<4} recall={points[-1].recall:.3f} "
                    f"p50={points[-1].p50_ms:.2f}ms p95={points[-1].p95_ms:.2f}ms"
                )

        await approximate.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
        return points, captured


def render(points: list[Point], *, rows: int, top_k: int) -> str:
    """The table published in `docs/performance.md`."""
    lines = [
        "",
        f"HNSW recall against exact search — {rows:,} chunks, {config.DIMENSIONS}-d, top-{top_k}",
        "=" * 74,
        (
            f"{'m':>3} {'ef_c':>5} {'ef_s':>5} {'recall':>8} {'p50 ms':>8} "
            f"{'p95 ms':>8} {'build s':>8} {'index MB':>9}"
        ),
        "-" * 74,
    ]
    lines.extend(
        f"{point.m:>3} {point.ef_construction:>5} {point.ef_search:>5} "
        f"{point.recall:>8.3f} {point.p50_ms:>8.2f} {point.p95_ms:>8.2f} "
        f"{point.build_seconds:>8.1f} {point.index_mb:>9.1f}"
        for point in points
    )
    lines.append("")
    return "\n".join(lines)


async def _main(arguments: argparse.Namespace) -> int:
    points, captured = await sweep(
        rows=arguments.rows,
        queries=arguments.queries,
        table=arguments.table,
        top_k=arguments.top_k,
    )
    print(render(points, rows=arguments.rows, top_k=arguments.top_k))

    written = results.write(
        "recall",
        rows=[asdict(point) for point in points],
        environment=captured,
        parameters={
            "rows": arguments.rows,
            "queries": arguments.queries,
            "top_k": arguments.top_k,
            "dimensions": config.DIMENSIONS,
            "clusters": config.CLUSTERS,
            "command": f"python -m benchmarks.recall --rows {arguments.rows} "
            f"--queries {arguments.queries} --top-k {arguments.top_k}",
        },
    )
    print(f"wrote {written['json']}\n      {written['csv']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sweep HNSW parameters for recall.")
    parser.add_argument("--rows", type=int, default=config.DEFAULT_ROWS)
    parser.add_argument("--queries", type=int, default=config.DEFAULT_QUERIES)
    parser.add_argument("--top-k", type=int, default=config.TOP_K)
    parser.add_argument("--table", default=config.TABLE)
    sys.exit(asyncio.run(_main(parser.parse_args())))
