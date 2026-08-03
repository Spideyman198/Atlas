"""Shared settings for the benchmark scripts.

Everything a published number depends on is a named constant here, so a reader
can check what a table was produced with rather than inferring it from the code
that produced it.
"""

from __future__ import annotations

import os
from typing import Final

#: Vector width. Matches the default embedding model (`text-embedding-3-small`),
#: because index size and distance cost both scale with it.
DIMENSIONS: Final = 1536

#: Neighbours requested. Retrieval serves eight (`ATLAS_RETRIEVAL__LIMIT`).
TOP_K: Final = 8

#: Topics in the generated corpus. Real embeddings cluster by subject; see
#: `generate_dataset.py` for why a corpus without that structure measures
#: nothing.
CLUSTERS: Final = 1200

#: Rows, unless overridden. Large enough for the HNSW graph to have structure,
#: small enough that a full sweep finishes while somebody waits.
DEFAULT_ROWS: Final = 50_000

#: Queries per measurement point. A p95 over fewer than about thirty samples is
#: one sample wearing a percentile's name.
DEFAULT_QUERIES: Final = 40

#: `(m, ef_construction)`. `m` is edges per node — build cost and memory.
#: `ef_construction` is how hard the builder looks for good edges. Neither can
#: be changed without rebuilding the index.
BUILD_PARAMETERS: Final = ((16, 64), (16, 128), (32, 128))

#: The query-time dial, and the only one that needs no rebuild.
SEARCH_PARAMETERS: Final = (10, 20, 40, 60, 100, 200)

#: Scratch table. The benchmark creates and drops it.
TABLE: Final = "bench_chunks"

#: Where results land, relative to this file.
RESULTS_DIRECTORY: Final = "results"

#: PostgreSQL settings a filtered dense search needs. Measured in
#: `latency.py`; see `docs/performance.md` for the numbers behind each.
DENSE_SCAN_SETTINGS: Final = (
    "SET LOCAL hnsw.iterative_scan = relaxed_order",
    "SET LOCAL enable_bitmapscan = off",
    "SET LOCAL enable_seqscan = off",
)


def dsn() -> str:
    """The database to benchmark against.

    Deliberately its own variable rather than `ATLAS_DATABASE__URL`. These
    scripts create and drop tables, and pointing them at a live corpus would
    destroy it.

    Raises:
        SystemExit: The variable is unset, with instructions rather than a
            traceback.
    """
    url = os.environ.get("ATLAS_BENCH_DATABASE_URL")
    if not url:
        message = (
            "Set ATLAS_BENCH_DATABASE_URL to a database these scripts may create "
            "and drop tables in.\n"
            "It must not be the configured corpus — the harness would destroy it.\n\n"
            "  make bench\n\n"
            "sets it for you."
        )
        raise SystemExit(message)
    return url
