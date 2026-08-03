"""HTTP entrypoint for the Atlas engine.

The application layer arrives with the milestones that need it; this module holds
the wiring — lifespan, middleware, exception handlers — and the operational probes.

The two probes answer different questions:

``/healthz`` — liveness. Is this process alive? It touches nothing external. A
    failing liveness probe makes an orchestrator restart the container, so wiring
    it to PostgreSQL would turn a brief database outage into a rolling restart
    across every replica.

``/readyz`` — readiness. Should this process receive traffic? It verifies the Atlas
    database is reachable and that pgvector meets the minimum version required by
    ``docs/adr/0004-vector-store-and-index-strategy.md``. A failing readiness probe
    removes the instance from rotation without killing it, so it can recover.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from atlas import __version__
from atlas.config.container import Container
from atlas.config.logging import configure_logging
from atlas.config.settings import get_settings
from atlas.domain.errors import AtlasError
from atlas.infrastructure.observability import configure_tracing, metrics
from atlas.interfaces.http.chat import router as chat_router
from atlas.interfaces.http.errors import register_exception_handlers
from atlas.interfaces.http.ingest import router as ingest_router
from atlas.interfaces.http.middleware import TraceIdMiddleware

logger = logging.getLogger(__name__)

#: Reported by the liveness probe. A constant rather than a settings lookup:
#: settings validation can fail, and liveness must not depend on it.
SERVICE_NAME: Final = "atlas-api"

#: ADR-0004 relies on `hnsw.iterative_scan`, introduced in pgvector 0.8, to keep
#: recall intact when retrieval is pre-filtered by company and visibility.
MINIMUM_PGVECTOR_VERSION: Final[tuple[int, int]] = (0, 8)

_PGVECTOR_VERSION_QUERY: Final = "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
_READINESS_TIMEOUT_SECONDS: Final = 3.0

router = APIRouter(tags=["operations"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the composition root and hold it for the process lifetime."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    configure_tracing(
        endpoint=settings.observability.otlp_endpoint,
        service_name=settings.service_name,
        environment=settings.env,
    )

    container = await Container.create(settings)
    app.state.container = container

    logger.info("atlas engine started", extra={"version": __version__, "env": settings.env})
    try:
        yield
    finally:
        await container.aclose()
        logger.info("atlas engine stopped")


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=Response,
    include_in_schema=False,
)
async def prometheus_metrics(request: Request) -> Response:
    """Counters and histograms, in Prometheus' exposition format.

    Deliberately outside the OpenAPI schema: it is scraped by a collector, not
    called by a client, and listing it as an API invites somebody to build on
    the format.

    Nothing here is per-user or per-question. The series are labelled by
    outcome, intent, tool and provider only, so scraping is not a way to learn
    what anybody asked.
    """
    container: Container | None = getattr(request.app.state, "container", None)
    if container is not None and not container.settings.observability.metrics_enabled:
        return Response(status_code=404)
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Report that the process is alive. Checks no dependencies, by design."""
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request) -> JSONResponse:
    """Report whether the engine can serve traffic.

    Returns ``200`` when every check passes and ``503`` otherwise, with a
    per-check breakdown so an operator can see which dependency failed without
    reading logs.
    """
    checks: dict[str, str] = {}
    container: Container | None = getattr(request.app.state, "container", None)

    if container is None:
        checks["database"] = "unavailable: container not initialised"
    else:
        try:
            async with (
                container.pool.connection(timeout=_READINESS_TIMEOUT_SECONDS) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(_PGVECTOR_VERSION_QUERY)
                row = await cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - a readiness probe must never raise
            logger.warning("readiness check failed", extra={"error": type(exc).__name__})
            checks["database"] = f"unavailable: {type(exc).__name__}"
        else:
            checks["database"] = "ok"
            checks["pgvector"] = _describe_pgvector(row[0] if row else None)
            checks["schema"] = await _describe_schema(container)

        checks["odoo"] = await _describe_odoo(container)

    ready = all(
        checks.get(name, "").startswith("ok") for name in ("database", "pgvector", "schema", "odoo")
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


async def _describe_schema(container: Container) -> str:
    """Compare the migrated vector width against the configured embedding model.

    A mismatch means the corpus and the model disagree, which produces silently
    wrong retrieval rather than an error. Reporting it as not-ready keeps the
    instance out of rotation until a re-index has run (ADR-0005).
    """
    configured = container.settings.embedding.dimensions
    try:
        actual = await container.vector_store.embedding_dimensions()
    except Exception as exc:  # noqa: BLE001 - a readiness probe must never raise
        return f"unavailable: {type(exc).__name__}"

    if actual != configured:
        return f"mismatch: schema is {actual}-d, embedding model is {configured}-d"
    return f"ok ({actual}-d)"


async def _describe_odoo(container: Container | None) -> str:
    """Check that Odoo is reachable, has the addon, and accepts our token.

    Gating, not informational. Odoo is the authorization authority (ADR-0006):
    with it unreachable the engine can retrieve candidates and clear none of
    them, so every answer it could give would be a refusal. Reporting not-ready
    keeps the instance out of rotation until that stops being true.
    """
    if container is None:
        return "unavailable: container not initialised"
    try:
        status = await container.odoo.status()
    except AtlasError as exc:
        return f"unavailable: {exc.code}"
    except Exception as exc:  # noqa: BLE001 - a readiness probe must never raise
        return f"unavailable: {type(exc).__name__}"
    return f"ok ({status.get('database', 'unknown database')})"


def _describe_pgvector(extension_version: str | None) -> str:
    """Summarise the installed pgvector version against the required minimum."""
    if extension_version is None:
        return "missing: extension 'vector' is not installed in the Atlas database"

    minimum = ".".join(str(part) for part in MINIMUM_PGVECTOR_VERSION)
    if _parse_version(extension_version) < MINIMUM_PGVECTOR_VERSION:
        return f"outdated: {extension_version} installed, >= {minimum} required"
    return f"ok ({extension_version})"


def _parse_version(raw: str) -> tuple[int, ...]:
    """Parse a dotted version into comparable integers, ignoring any suffix.

    PostgreSQL extension versions are free-form strings, so ``0.8.1`` and
    ``0.8.0-beta`` must both compare sensibly against ``(0, 8)``.
    """
    return tuple(
        int(digits) if (digits := "".join(itertools.takewhile(str.isdigit, part))) else 0
        for part in raw.split(".")
    )


def create_app() -> FastAPI:
    """Build the FastAPI application.

    A factory rather than a module-level singleton, so tests can construct isolated
    instances and a future entrypoint can inject a pre-built container.
    """
    app = FastAPI(
        title="Odoo Atlas Engine",
        description=(
            "Retrieval, orchestration and generation engine for the Odoo Atlas "
            "assistant. Internal service — do not expose this API publicly."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)
    app.include_router(router)
    app.include_router(ingest_router)
    app.include_router(chat_router)
    return app


app = create_app()
