"""Traces and metrics.

Metrics answer "is it healthy"; traces answer "what happened to this one
request". Both are adapters: the application layer records through them without
learning what a Prometheus registry or an OTLP exporter is.
"""

from atlas.infrastructure.observability import metrics
from atlas.infrastructure.observability.tracing import (
    TRACE_ID_ATTRIBUTE,
    configure_tracing,
    span,
)

__all__ = [
    "TRACE_ID_ATTRIBUTE",
    "configure_tracing",
    "metrics",
    "span",
]
