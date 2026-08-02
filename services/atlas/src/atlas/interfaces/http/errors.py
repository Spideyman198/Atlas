"""Translation from domain errors to HTTP responses.

Responses follow RFC 9457 problem details, served as ``application/problem+json``.
Two additions to the standard members: ``code``, the stable machine-readable
identifier from the error class, and ``trace_id``, so a user can quote one value
that locates the failure in the logs.

The status mapping lives here rather than on the error classes so the domain stays
transport-agnostic — the same errors are raised by the CLI and by the ingestion
worker, neither of which speaks HTTP.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from atlas.config.logging import get_trace_id
from atlas.domain.errors import (
    AtlasError,
    AuthorizationError,
    ConfigurationError,
    DependencyUnavailableError,
    NotFoundError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    StorageError,
    ValidationError,
)
from atlas.interfaces.http.middleware import TRACE_ID_SCOPE_KEY

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

# Most specific first is not required — resolution walks the MRO — but keeping the
# table ordered from specific to general makes it readable.
_STATUS_BY_ERROR: Final[dict[type[AtlasError], int]] = {
    ValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    AuthorizationError: status.HTTP_403_FORBIDDEN,
    RateLimitedError: status.HTTP_429_TOO_MANY_REQUESTS,
    ProviderTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
    ProviderError: status.HTTP_502_BAD_GATEWAY,
    DependencyUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    StorageError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ConfigurationError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def trace_id_for(request: Request) -> str | None:
    """Return the trace id for a request.

    Prefers the ASGI scope over the ContextVar. The 500 handler runs inside
    ``ServerErrorMiddleware``, which is installed outside our middleware, so by
    then the ContextVar has already been reset — see ``middleware.py``.
    """
    scoped = request.scope.get(TRACE_ID_SCOPE_KEY)
    if isinstance(scoped, str):
        return scoped
    return get_trace_id()


def status_for(error: AtlasError) -> int:
    """Return the HTTP status for an error, inheriting through its base classes.

    A new subclass therefore gets a sensible status without touching this table.
    """
    for klass in type(error).__mro__:
        if klass in _STATUS_BY_ERROR:
            return _STATUS_BY_ERROR[klass]
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def problem_response(
    error: AtlasError, http_status: int, trace_id: str | None = None
) -> JSONResponse:
    """Build an RFC 9457 problem document for an error."""
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": type(error).__name__,
        "status": http_status,
        "detail": error.message,
        "code": error.code,
    }

    if trace_id is not None:
        body["trace_id"] = trace_id

    if isinstance(error, RateLimitedError) and error.retry_after_seconds is not None:
        body["retry_after_seconds"] = error.retry_after_seconds

    return JSONResponse(status_code=http_status, content=body, media_type=PROBLEM_CONTENT_TYPE)


async def handle_atlas_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a deliberate failure.

    Server-side faults are logged at error level with a stack trace; client
    mistakes are logged at info level without one, so a misbehaving client cannot
    fill the error budget of an on-call dashboard.
    """
    assert isinstance(exc, AtlasError)  # noqa: S101 - registered for this type only
    http_status = status_for(exc)

    detail = {"code": exc.code, "path": request.url.path, **exc.context}
    if http_status >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.error("request failed: %s", exc.message, exc_info=exc, extra=detail)
    else:
        logger.info("request rejected: %s", exc.message, extra=detail)

    return problem_response(exc, http_status, trace_id_for(request))


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an error that is not part of the taxonomy.

    Reaching this handler means a bug. The message is not echoed to the client —
    it can contain internal detail — so the client gets the trace id and the log
    gets everything.
    """
    logger.exception(
        "unhandled exception",
        extra={"path": request.url.path, "exception_type": type(exc).__name__},
    )

    body: dict[str, Any] = {
        "type": "about:blank",
        "title": "InternalServerError",
        "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "detail": "An unexpected error occurred.",
        "code": "internal_error",
    }
    trace_id = trace_id_for(request)
    if trace_id is not None:
        body["trace_id"] = trace_id

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers above to an application."""
    app.add_exception_handler(AtlasError, handle_atlas_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
