"""The ingestion API.

One endpoint, called by Odoo's cron and by an operator with ``curl``. It queues
work and returns; it does not sync inline. A full sync of a real ERP takes
minutes and would hold an Odoo cron thread open for all of them, which is the
kind of coupling ADR-0002 exists to avoid.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from atlas.config.container import Container
from atlas.domain.errors import ValidationError
from atlas.domain.ingestion import JobKind
from atlas.domain.sources import REGISTRY, source_keys

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingest", tags=["ingestion"])


class SyncRequest(BaseModel):
    """What to sync, and how hard."""

    model_config = {"extra": "forbid"}

    sources: list[str] = Field(
        default_factory=list,
        description="Source keys. Empty means every source Atlas knows.",
    )
    kind: JobKind = Field(
        default=JobKind.INCREMENTAL,
        description=(
            "`incremental` reads what changed since the watermark. `full_sync` "
            "reads everything but still skips unchanged content. `reindex` "
            "re-embeds regardless, which is what to run after changing model."
        ),
    )
    record_ids: list[int] = Field(
        default_factory=list,
        description="Sync exactly these records. Only meaningful with one source.",
    )
    deleted_ids: list[int] = Field(
        default_factory=list,
        description="Records that no longer exist and should leave the corpus.",
    )


class SyncResponse(BaseModel):
    """The jobs that were queued."""

    queued: dict[str, int] = Field(description="Job id per source key.")


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED, summary="Queue an ingestion run")
async def sync(request: Request, body: SyncRequest) -> SyncResponse:
    """Queue a sync and return immediately.

    Returns ``202``: the work is accepted, not done. A caller that needs to know
    the outcome watches the job, which is the honest shape for something that
    takes minutes.
    """
    container: Container = request.app.state.container
    targets = body.sources or list(source_keys())

    unknown = [key for key in targets if key not in REGISTRY]
    if unknown:
        message = f"unknown source(s): {', '.join(sorted(unknown))}"
        raise ValidationError(message, context={"known": list(source_keys())})

    if (body.record_ids or body.deleted_ids) and len(targets) != 1:
        message = "record_ids and deleted_ids apply to exactly one source"
        raise ValidationError(message)

    payload: dict[str, list[int]] = {}
    if body.record_ids:
        payload["ids"] = body.record_ids
    if body.deleted_ids:
        payload["deleted"] = body.deleted_ids

    queued: dict[str, int] = {}
    for source_key in targets:
        queued[source_key] = await container.job_queue.enqueue(
            source_key, body.kind, payload=payload
        )

    logger.info(
        "ingestion queued",
        extra={"sources": len(queued), "kind": str(body.kind), "jobs": list(queued.values())},
    )
    return SyncResponse(queued=queued)


@router.get("/sources", summary="List the sources Atlas can index")
async def sources(request: Request) -> dict[str, object]:
    """Report the registry, and which of it this Odoo can actually serve."""
    container: Container = request.app.state.container
    try:
        available = await container.source_reader.available_sources()
    except Exception as exc:  # noqa: BLE001 - a listing must not 500 because Odoo blinked
        logger.warning(
            "could not ask Odoo which sources exist",
            extra={"error": type(exc).__name__},
        )
        available = {}

    return {
        "sources": [
            {
                "key": key,
                "model": template.res_model,
                "requires_module": template.requires_module,
                "available": available.get(key),
            }
            for key, template in sorted(REGISTRY.items())
        ]
    }
