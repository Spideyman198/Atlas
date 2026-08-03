"""Run every benchmark and write a results file for each.

The order matters: `recall.py` selects the index parameters, and `latency.py`
measures the query shapes on the index those parameters produce.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from benchmarks import config, latency, recall


async def _main(arguments: argparse.Namespace) -> int:
    # Each script owns its own reporting and results file; this only sequences
    # them, so the private entry points are the right thing to call.
    await recall._main(arguments)
    await latency._main(arguments)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the full benchmark suite.")
    parser.add_argument("--rows", type=int, default=config.DEFAULT_ROWS)
    parser.add_argument("--queries", type=int, default=config.DEFAULT_QUERIES)
    parser.add_argument("--top-k", type=int, default=config.TOP_K)
    parser.add_argument("--table", default=config.TABLE)
    sys.exit(asyncio.run(_main(parser.parse_args())))
