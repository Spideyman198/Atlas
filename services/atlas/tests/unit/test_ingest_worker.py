"""Tests for the ingestion worker.

The worker's job is to survive. A queue that stops draining because one job
raised something nobody predicted is worse than a queue that never started, so
most of what is asserted here is about failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from atlas.config.container import Container
from atlas.config.settings import IngestionSettings
from atlas.domain.errors import DependencyUnavailableError
from atlas.domain.ingestion import IngestJob, JobKind, RetryOutcome, SyncReport
from atlas.interfaces.worker import IngestWorker

pytestmark = pytest.mark.unit


class RecordingQueue:
    """A queue that hands out a scripted list of jobs and records the outcomes."""

    def __init__(self, jobs: Sequence[IngestJob] = ()) -> None:
        self._jobs = list(jobs)
        self.succeeded: list[int] = []
        self.failed: list[tuple[int, str]] = []
        self.sweeps = 0

    async def claim(self, worker: str) -> IngestJob | None:
        return self._jobs.pop(0) if self._jobs else None

    async def succeed(self, job_id: int) -> None:
        self.succeeded.append(job_id)

    async def fail(self, job_id: int, error: str) -> RetryOutcome:
        self.failed.append((job_id, error))
        return RetryOutcome(attempts=1, dead=False, run_after=datetime.now(UTC))

    async def release_stale(self, older_than_seconds: float) -> int:
        self.sweeps += 1
        return 0


class RecordingSync:
    """Stands in for the sync use case, so the worker's dispatch is what is tested."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.runs: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    async def run(
        self,
        source_key: str,
        *,
        kind: JobKind = JobKind.INCREMENTAL,
        record_ids: Sequence[int] | None = None,
    ) -> SyncReport:
        if self.failure is not None:
            raise self.failure
        self.runs.append({"source_key": source_key, "kind": kind, "record_ids": record_ids})
        return SyncReport(source_key=source_key, ingested=1)

    async def delete_records(self, source_key: str, record_ids: Sequence[int]) -> SyncReport:
        self.deletes.append({"source_key": source_key, "record_ids": list(record_ids)})
        return SyncReport(source_key=source_key, deleted=len(record_ids))


def build_worker(
    jobs: Sequence[IngestJob] = (),
    *,
    failure: Exception | None = None,
) -> tuple[IngestWorker, RecordingQueue, RecordingSync]:
    queue = RecordingQueue(jobs)
    sync = RecordingSync(failure)

    class _Container:
        settings = type("_Settings", (), {"ingestion": IngestionSettings(worker_poll_seconds=0.01)})
        job_queue = queue

        def sync_source(self) -> RecordingSync:
            return sync

    worker = IngestWorker(cast(Container, _Container()), name="test-worker")
    return worker, queue, sync


def job(**overrides: Any) -> IngestJob:
    values: dict[str, Any] = {
        "id": 1,
        "source_key": "odoo.res.partner",
        "kind": JobKind.INCREMENTAL,
        "payload": {},
    }
    values.update(overrides)
    return IngestJob(**values)


async def test_an_empty_queue_is_reported_as_no_work() -> None:
    worker, _queue, sync = build_worker()

    assert await worker.run_once() is False
    assert sync.runs == []


async def test_a_claimed_job_runs_and_is_marked_done() -> None:
    worker, queue, sync = build_worker([job()])

    assert await worker.run_once() is True
    assert queue.succeeded == [1]
    assert sync.runs[0]["source_key"] == "odoo.res.partner"


async def test_the_job_kind_reaches_the_sync() -> None:
    worker, _queue, sync = build_worker([job(kind=JobKind.REINDEX)])

    await worker.run_once()

    assert sync.runs[0]["kind"] is JobKind.REINDEX


async def test_a_payload_of_record_ids_narrows_the_sync() -> None:
    worker, _queue, sync = build_worker([job(payload={"ids": [4, 5]})])

    await worker.run_once()

    assert sync.runs[0]["record_ids"] == [4, 5]


async def test_a_deletion_payload_removes_records_instead_of_syncing() -> None:
    worker, queue, sync = build_worker([job(payload={"deleted": [7]})])

    await worker.run_once()

    assert sync.deletes == [{"source_key": "odoo.res.partner", "record_ids": [7]}]
    assert sync.runs == []
    assert queue.succeeded == [1]


async def test_rubbish_in_a_payload_is_ignored_rather_than_crashing() -> None:
    worker, queue, sync = build_worker([job(payload={"ids": "all of them", "deleted": None})])

    await worker.run_once()

    assert sync.runs[0]["record_ids"] is None
    assert queue.succeeded == [1]


async def test_a_known_failure_is_reported_to_the_queue() -> None:
    worker, queue, _sync = build_worker([job()], failure=DependencyUnavailableError("Odoo is down"))

    assert await worker.run_once() is True
    assert queue.succeeded == []
    assert queue.failed[0][0] == 1
    assert "dependency_unavailable" in queue.failed[0][1]


async def test_an_unexpected_failure_does_not_kill_the_worker() -> None:
    """The broad catch is the point, not an oversight.

    A worker that dies on an exception nobody predicted takes the whole queue
    down with it, and the job it was holding needs the stale sweep to come back.
    """
    worker, queue, _sync = build_worker([job()], failure=RuntimeError("something new"))

    assert await worker.run_once() is True
    assert queue.failed[0][1].startswith("RuntimeError")


async def test_stale_jobs_are_swept_on_the_first_poll() -> None:
    worker, queue, _sync = build_worker()

    await worker.run_once()

    assert queue.sweeps == 1


async def test_the_sweep_does_not_run_on_every_poll() -> None:
    """An idle queue should not be written to several times a second."""
    worker, queue, _sync = build_worker()

    for _poll in range(5):
        await worker.run_once()

    assert queue.sweeps == 1


async def test_run_forever_stops_when_asked() -> None:
    worker, queue, _sync = build_worker([job(), job(id=2)])
    stop = asyncio.Event()

    async def run() -> None:
        await worker.run_forever(stop)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert queue.succeeded == [1, 2]


async def test_the_worker_names_itself_so_a_stuck_job_can_be_traced() -> None:
    worker, _queue, _sync = build_worker()

    assert worker.name == "test-worker"


def test_a_default_worker_name_identifies_host_and_process() -> None:
    queue = RecordingQueue()

    class _Container:
        settings = type("_Settings", (), {"ingestion": IngestionSettings()})
        job_queue = queue

        def sync_source(self) -> Mapping[str, Any]:  # pragma: no cover - never called
            return {}

    worker = IngestWorker(cast(Container, _Container()))

    assert ":" in worker.name
