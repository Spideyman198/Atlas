"""The ingestion worker.

Claims one job at a time from PostgreSQL, runs it, and records the outcome. No
broker, no scheduler, no second datastore — the queue is a table and the claim is
a row lock (:mod:`atlas.infrastructure.persistence.job_queue`).

Running several of these is safe and is the way to go faster: ``FOR UPDATE SKIP
LOCKED`` means two workers polling at the same instant get different jobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from typing import Final

from atlas.config.container import Container
from atlas.domain.errors import AtlasError
from atlas.domain.ingestion import IngestJob, SyncReport

logger = logging.getLogger(__name__)

#: How often to sweep for jobs whose worker died, as a multiple of the poll
#: interval. Sweeping on every poll would be a needless write on an idle queue.
_SWEEP_EVERY: Final = 20


class IngestWorker:
    """Drains the ingestion queue until told to stop."""

    def __init__(self, container: Container, *, name: str | None = None) -> None:
        self._container = container
        self._settings = container.settings.ingestion
        # Identifies the holder of a claim, so a stuck job can be traced to the
        # process that took it.
        self._name = name or f"{socket.gethostname()}:{os.getpid()}"
        self._polls = 0

    @property
    def name(self) -> str:
        """This worker's identity, as recorded on the jobs it claims."""
        return self._name

    async def run_once(self) -> bool:
        """Claim and run one job.

        Returns:
            Whether there was anything to do. Callers use this to decide whether
            to sleep or to come straight back for more.
        """
        self._polls += 1
        if self._polls % _SWEEP_EVERY == 1:
            await self._container.job_queue.release_stale(self._settings.stale_job_seconds)

        job = await self._container.job_queue.claim(self._name)
        if job is None:
            return False

        logger.info(
            "ingest job claimed",
            extra={
                "job_id": job.id,
                "source_key": job.source_key,
                "kind": str(job.kind),
                "attempt": job.attempts,
                "worker": self._name,
            },
        )
        try:
            report = await self._run(job)
        except AtlasError as exc:
            await self._give_up_or_retry(job, f"{exc.code}: {exc.message}")
        except Exception as exc:
            # Deliberately broad. A worker that dies on an unexpected exception
            # takes the whole queue down with it, and the job it was holding
            # would need the stale sweep to come back.
            logger.exception("ingest job raised", extra={"job_id": job.id})
            await self._give_up_or_retry(job, f"{type(exc).__name__}: {exc}")
        else:
            await self._container.job_queue.succeed(job.id)
            logger.info(
                "ingest job finished",
                extra={
                    "job_id": job.id,
                    "source_key": job.source_key,
                    "ingested": report.ingested,
                    "unchanged": report.unchanged,
                    "deleted": report.deleted,
                    "embedding_calls": report.embedding_calls,
                },
            )
        return True

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Poll until ``stop`` is set, sleeping only when there is nothing to do."""
        stop = stop or asyncio.Event()
        logger.info("ingest worker started", extra={"worker": self._name})
        while not stop.is_set():
            try:
                did_work = await self.run_once()
            except Exception:
                logger.exception("ingest worker poll failed", extra={"worker": self._name})
                did_work = False

            if did_work:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.worker_poll_seconds)
        logger.info("ingest worker stopped", extra={"worker": self._name})

    async def _run(self, job: IngestJob) -> SyncReport:
        """Dispatch one job to the work it names."""
        sync = self._container.sync_source()
        payload = job.payload

        if deleted := _ids(payload.get("deleted")):
            return await sync.delete_records(job.source_key, deleted)

        return await sync.run(
            job.source_key,
            kind=job.kind,
            record_ids=_ids(payload.get("ids")) or None,
        )

    async def _give_up_or_retry(self, job: IngestJob, error: str) -> None:
        outcome = await self._container.job_queue.fail(job.id, error)
        logger.warning(
            "ingest job failed",
            extra={
                "job_id": job.id,
                "source_key": job.source_key,
                "attempts": outcome.attempts,
                "dead": outcome.dead,
                "retry_at": outcome.run_after.isoformat() if outcome.run_after else None,
            },
        )


def _ids(value: object) -> list[int]:
    """Read a list of record ids out of a job payload, ignoring anything else."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
