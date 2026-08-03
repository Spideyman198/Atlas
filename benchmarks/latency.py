"""Latency of the query shapes retrieval actually issues.

`recall.py` forces the index scan, because tuning an index parameter means
measuring the index. This script does the opposite: it leaves the planner alone
and measures **what production would get**, which turned out to be a different
thing entirely.

The comparison below is what changed `PgVectorStore`. Atlas's dense search
filters by company and visibility and then orders by distance, and with that
filter PostgreSQL costs a bitmap scan over `(company_id, visibility)` below an
HNSW walk and takes it — then sorts every matching row by distance. The
`hnsw.iterative_scan` setting was already configured and did nothing, because it
governs an index scan that was never chosen.
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

#: `m` and `ef_construction` for this script's index. The values `recall.py`
#: selected, so latency is measured on the index that ships.
M = 16
EF_CONSTRUCTION = 64
EF_SEARCH = 40


@dataclass(frozen=True, slots=True)
class Shape:
    """One query shape and session configuration."""

    name: str
    p50_ms: float
    p95_ms: float
    rows_returned: float
    plan_node: str


async def _time(
    connection: psycopg.AsyncConnection[Any],
    statement: sql.Composed,
    *,
    queries: int,
) -> tuple[list[float], float]:
    """Latencies in milliseconds, and the mean row count.

    The row count is not decoration. A filtered ANN search that stops early
    returns fewer rows than asked for, silently, and the latency alone would
    make that configuration look like the fastest one.
    """
    latencies: list[float] = []
    returned = 0
    for index in range(queries):
        probe = Vector(probe_vector(index))
        started = time.perf_counter()
        cursor = await connection.execute(statement, (probe,))
        rows = await cursor.fetchall()
        latencies.append((time.perf_counter() - started) * 1000)
        returned += len(rows)
    return latencies, returned / queries


async def _plan_node(connection: psycopg.AsyncConnection[Any], statement: sql.Composed) -> str:
    """The scan node the planner chose, which is the whole point of the table."""
    cursor = await connection.execute(
        sql.SQL("EXPLAIN (COSTS OFF) ") + statement, (Vector(probe_vector(0)),)
    )
    lines = [row[0].strip() for row in await cursor.fetchall()]
    return next((line for line in lines if "Scan" in line), lines[0] if lines else "unknown")


async def compare(
    *, rows: int, queries: int, table: str, top_k: int
) -> tuple[list[Shape], dict[str, Any]]:
    """Measure each query shape under each session configuration."""
    async with await connect() as connection:
        captured = await environment.capture(connection)
        print(environment.describe(captured))
        print(f"\ngenerating {rows:,} rows…", flush=True)
        await generate(connection, table=table, rows=rows)
        await build_index(connection, table=table, m=M, ef_construction=EF_CONSTRUCTION)
        await connection.execute(sql.SQL("SET hnsw.ef_search = {}").format(sql.Literal(EF_SEARCH)))
        print("index built\n")

        unfiltered = sql.SQL("SELECT id FROM {} ORDER BY embedding <=> %s LIMIT {k}").format(
            sql.Identifier(table), k=sql.Literal(top_k)
        )
        filtered = sql.SQL(
            "SELECT id FROM {} WHERE company_id = 1 AND visibility >= 1 "
            "ORDER BY embedding <=> %s LIMIT {k}"
        ).format(sql.Identifier(table), k=sql.Literal(top_k))

        # `planner free` is what Atlas issued before this was measured.
        cases: tuple[tuple[str, sql.Composed, tuple[str, ...]], ...] = (
            (
                "unfiltered, planner free",
                unfiltered,
                ("SET enable_bitmapscan = on", "SET enable_seqscan = on"),
            ),
            (
                "filtered, planner free",
                filtered,
                (
                    "SET enable_bitmapscan = on",
                    "SET enable_seqscan = on",
                    "SET hnsw.iterative_scan = off",
                ),
            ),
            (
                "filtered, forced index, no iterative scan",
                filtered,
                (
                    "SET enable_bitmapscan = off",
                    "SET enable_seqscan = off",
                    "SET hnsw.iterative_scan = off",
                ),
            ),
            (
                "filtered, forced index + relaxed_order",
                filtered,
                (
                    "SET enable_bitmapscan = off",
                    "SET enable_seqscan = off",
                    "SET hnsw.iterative_scan = relaxed_order",
                ),
            ),
        )

        shapes: list[Shape] = []
        for name, statement, settings in cases:
            for setting in settings:
                await connection.execute(setting)
            latencies, returned = await _time(connection, statement, queries=queries)
            shapes.append(
                Shape(
                    name=name,
                    p50_ms=round(statistics.median(latencies), 2),
                    p95_ms=round(results.percentile(latencies, 95), 2),
                    rows_returned=round(returned, 1),
                    plan_node=await _plan_node(connection, statement),
                )
            )
            print(
                f"{name:44} p50={shapes[-1].p50_ms:7.2f}ms "
                f"p95={shapes[-1].p95_ms:7.2f}ms rows={shapes[-1].rows_returned:4.1f}"
            )

        await connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
        return shapes, captured


def render(shapes: list[Shape], *, rows: int, top_k: int) -> str:
    """The table published in `docs/performance.md`."""
    lines = [
        "",
        (
            f"Query shape latency — {rows:,} chunks, {config.DIMENSIONS}-d, "
            f"top-{top_k}, ef_search={EF_SEARCH}"
        ),
        "=" * 96,
        f"{'configuration':<44}{'p50 ms':>9}{'p95 ms':>9}{'rows':>7}  plan",
        "-" * 96,
    ]
    lines.extend(
        f"{shape.name:<44}{shape.p50_ms:>9.2f}{shape.p95_ms:>9.2f}"
        f"{shape.rows_returned:>7.1f}  {shape.plan_node[:30]}"
        for shape in shapes
    )
    lines.append("")
    return "\n".join(lines)


async def _main(arguments: argparse.Namespace) -> int:
    shapes, captured = await compare(
        rows=arguments.rows,
        queries=arguments.queries,
        table=arguments.table,
        top_k=arguments.top_k,
    )
    print(render(shapes, rows=arguments.rows, top_k=arguments.top_k))

    written = results.write(
        "latency",
        rows=[asdict(shape) for shape in shapes],
        environment=captured,
        parameters={
            "rows": arguments.rows,
            "queries": arguments.queries,
            "top_k": arguments.top_k,
            "dimensions": config.DIMENSIONS,
            "m": M,
            "ef_construction": EF_CONSTRUCTION,
            "ef_search": EF_SEARCH,
            "command": f"python -m benchmarks.latency --rows {arguments.rows} "
            f"--queries {arguments.queries} --top-k {arguments.top_k}",
        },
    )
    print(f"wrote {written['json']}\n      {written['csv']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure query shape latency.")
    parser.add_argument("--rows", type=int, default=config.DEFAULT_ROWS)
    parser.add_argument("--queries", type=int, default=config.DEFAULT_QUERIES)
    parser.add_argument("--top-k", type=int, default=config.TOP_K)
    parser.add_argument("--table", default=config.TABLE)
    sys.exit(asyncio.run(_main(parser.parse_args())))
