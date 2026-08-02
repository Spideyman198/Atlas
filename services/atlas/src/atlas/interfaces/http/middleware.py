"""ASGI middleware.

Written against the raw ASGI interface rather than Starlette's
``BaseHTTPMiddleware``. That base class wraps each request in an anonymous task,
which breaks ``ContextVar`` propagation in exactly the way this middleware depends
on, and it interferes with streaming responses — which M10 needs for SSE.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atlas.config.logging import (
    TRACE_ID_HEADER,
    bind_trace_id,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
)

#: ASGI scope key holding the trace id for the current request.
#:
#: The ContextVar alone is not enough. Starlette installs ``ServerErrorMiddleware``
#: *outside* user middleware, so when an unhandled exception propagates out of this
#: middleware the ``finally`` below has already reset the ContextVar by the time the
#: 500 handler runs. The scope survives the unwind, so error responses read from it.
TRACE_ID_SCOPE_KEY: Final = "atlas.trace_id"

_HEADER_BYTES = TRACE_ID_HEADER.lower().encode("latin-1")

# The inbound value is echoed into a response header and into every log line for
# the request, so an unbounded client-supplied string is not acceptable.
_MAX_TRACE_ID_LENGTH = 128


class TraceIdMiddleware:
    """Bind a trace id for the duration of each HTTP request.

    Adopts an inbound ``X-Request-ID`` when the caller supplies one, so a single id
    spans the Odoo addon and the engine. Otherwise it mints one. The id is bound to
    the request context, so every log record emitted while handling the request
    carries it, and is echoed back on the response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI event stream."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = _inbound_trace_id(scope) or new_trace_id()
        scope[TRACE_ID_SCOPE_KEY] = trace_id
        token = bind_trace_id(trace_id)

        async def send_with_trace_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((_HEADER_BYTES, trace_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_id)
        finally:
            # Resetting matters: without it the id leaks into whichever task next
            # reuses this context.
            reset_trace_id(token)


def _inbound_trace_id(scope: MutableMapping[str, Any]) -> str | None:
    """Return a usable trace id from the request headers, if one is present."""
    # `Scope` is a MutableMapping[str, Any], so annotate to keep the loop typed.
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name.lower() == _HEADER_BYTES:
            candidate = value.decode("latin-1").strip()
            if candidate and len(candidate) <= _MAX_TRACE_ID_LENGTH and candidate.isprintable():
                return candidate
            return None
    return None


__all__ = ["TraceIdMiddleware", "get_trace_id"]
