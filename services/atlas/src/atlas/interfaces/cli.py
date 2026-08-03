"""The ``atlas`` command line.

Four things an operator needs that a web API is the wrong shape for: see which
sources exist, queue a sync, force a re-index after changing embedding model,
and run a worker.

Everything here goes through the same container, the same use cases and the same
queue as the scheduled path. There is no second implementation of ingestion that
only the CLI uses, which is why "it works from the command line" means something.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from typing import TextIO

from atlas.config.container import Container
from atlas.config.logging import configure_logging
from atlas.config.settings import get_settings
from atlas.domain.errors import AtlasError
from atlas.domain.ingestion import JobKind
from atlas.domain.sources import REGISTRY, source_keys
from atlas.interfaces.worker import IngestWorker

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Separate from :func:`main` so tests can read it."""
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="Operate the Atlas ingestion pipeline.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("sources", help="List the sources Atlas knows how to index")

    sync = commands.add_parser("sync", help="Queue an incremental sync")
    _add_source_argument(sync)
    sync.add_argument(
        "--full",
        action="store_true",
        help="Read every record rather than only what changed. Unchanged content is still skipped.",
    )
    sync.add_argument(
        "--now",
        action="store_true",
        help="Run inline instead of queueing, and print what it did.",
    )

    reindex = commands.add_parser(
        "reindex",
        help="Re-embed a source from scratch. Use after changing embedding model.",
    )
    _add_source_argument(reindex)
    reindex.add_argument(
        "--now",
        action="store_true",
        help="Run inline instead of queueing.",
    )

    worker = commands.add_parser("worker", help="Run an ingestion worker")
    worker.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one job and exit. Useful in tests and cron.",
    )
    return parser


def _add_source_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="KEY",
        help="Source key; repeatable. Defaults to every registered source.",
    )


async def _run(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if arguments.command == "sources":
        return _print_sources()

    async with await Container.create(settings) as container:
        if arguments.command == "worker":
            worker = IngestWorker(container)
            if arguments.once:
                did_work = await worker.run_once()
                _print("did one job" if did_work else "queue empty")
                return 0
            await worker.run_forever()
            return 0

        kind = _kind_for(arguments)
        targets = _targets(arguments.sources)
        if arguments.now:
            return await _run_inline(container, targets, kind)
        return await _enqueue(container, targets, kind)


def _kind_for(arguments: argparse.Namespace) -> JobKind:
    if arguments.command == "reindex":
        return JobKind.REINDEX
    return JobKind.FULL_SYNC if arguments.full else JobKind.INCREMENTAL


def _targets(requested: Sequence[str] | None) -> list[str]:
    if not requested:
        return list(source_keys())
    unknown = [key for key in requested if key not in REGISTRY]
    if unknown:
        known = ", ".join(source_keys())
        message = f"unknown source(s) {', '.join(unknown)}; known sources are {known}"
        raise SystemExit(message)
    return list(requested)


async def _enqueue(container: Container, targets: Sequence[str], kind: JobKind) -> int:
    for source_key in targets:
        job_id = await container.job_queue.enqueue(source_key, kind)
        _print(f"queued {kind} for {source_key} as job {job_id}")
    return 0


async def _run_inline(container: Container, targets: Sequence[str], kind: JobKind) -> int:
    """Run the sync in this process and report what it cost.

    ``--now`` exists because "did anything change, and did it call the provider"
    is the question an operator actually has, and waiting for a worker to pick
    the job up makes that harder to answer.
    """
    failed = False
    sync = container.sync_source()
    for source_key in targets:
        try:
            report = await sync.run(source_key, kind=kind)
        except AtlasError as exc:
            failed = True
            _print(f"{source_key}: FAILED {exc.code}: {exc.message}", stream=sys.stderr)
            continue
        _print(
            f"{source_key}: examined {report.examined}, unchanged {report.unchanged}, "
            f"ingested {report.ingested}, chunks {report.chunks_written}, "
            f"embedding calls {report.embedding_calls}, cached segments {report.cached_segments}"
        )
        for failure in report.failures:
            _print(f"  ! {failure}", stream=sys.stderr)
    return 1 if failed else 0


def _print_sources() -> int:
    for key in source_keys():
        template = REGISTRY[key]
        module = template.requires_module or "base"
        _print(f"{key:28} {template.res_model:20} needs {module}")
    return 0


def _print(message: str, *, stream: TextIO | None = None) -> None:
    """Write a line for a human.

    The engine logs structurally; a command line answers a person. Going through
    the logger here would bury the answer in JSON.
    """
    print(message, file=stream or sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``atlas`` console script."""
    arguments = build_parser().parse_args(argv)
    return asyncio.run(_run(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
