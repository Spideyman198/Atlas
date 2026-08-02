"""Structured logging and request correlation.

Log records are emitted as one JSON object per line, so an aggregator can ingest
them without a grok pattern. Every record carries the trace id of the request that
produced it, propagated through a :class:`~contextvars.ContextVar` so no call site
has to thread it through by hand.

The trace id is the join key for everything that follows: the `atlas.message` row
in Odoo, the authorization audit log (M6), and the retrieval and cost telemetry
(M12). One id takes a user-reported problem back to the exact prompt.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

#: Request header carrying an inbound trace id. When a caller supplies one — the
#: Odoo addon does — we adopt it instead of minting a new one, so a single id spans
#: both processes.
TRACE_ID_HEADER: Final = "X-Request-ID"

_trace_id: ContextVar[str | None] = ContextVar("atlas_trace_id", default=None)

_HUMAN_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Attributes every LogRecord carries. Anything outside this set was supplied by a
# caller through `extra=` and is merged into the JSON payload.
_STANDARD_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def new_trace_id() -> str:
    """Return a fresh trace id."""
    return uuid4().hex


def get_trace_id() -> str | None:
    """Return the trace id bound to the current context, if any."""
    return _trace_id.get()


def bind_trace_id(trace_id: str) -> Token[str | None]:
    """Bind a trace id to the current context.

    Returns:
        A token the caller must pass to :func:`reset_trace_id` when the scope ends.
        Skipping the reset leaks the id into whatever task reuses the context.
    """
    return _trace_id.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    """Restore the trace id that was bound before :func:`bind_trace_id`."""
    _trace_id.reset(token)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a JSON object on one line."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        trace_id = get_trace_id()
        if trace_id is not None:
            payload["trace_id"] = trace_id

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        payload.update(extras)

        # `default=str` keeps a non-serialisable value in a log call from raising
        # inside the logging machinery, which would lose the record entirely.
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(*, level: str, json_output: bool = True) -> None:
    """Install the root log handler.

    Called once, from the application lifespan. Uvicorn installs its own handlers
    on import, so we clear them and let those loggers propagate to ours; otherwise
    access logs bypass the JSON formatter and the output is half structured.

    Args:
        level: Minimum level, as a name such as ``INFO``.
        json_output: JSON lines when true, a human-readable format when false.
            Local development often prefers the latter.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter(_HUMAN_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.propagate = True
