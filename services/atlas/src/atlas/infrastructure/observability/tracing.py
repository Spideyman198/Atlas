"""OpenTelemetry tracing, off unless an endpoint is configured.

A trace answers the question a metric cannot: *for this one bad answer, where
did the time go and what did each stage see?* Metrics say retrieval is slow;
a trace says retrieval was slow on this request because authorization made four
round-trips to an Odoo that was itself waiting on a lock.

Two decisions.

**Off by default.** With no endpoint configured this installs no provider and
every span becomes a no-op from the SDK's own non-recording implementation. An
engine that cannot start because a collector is missing would be a poor trade
for observability.

**The trace id is Atlas's, not OpenTelemetry's.** Spans carry ``atlas.trace_id``
as an attribute, because that is the id the addon logged, the engine logged, and
Odoo's access log recorded. A second identity that only the tracing backend
knows about would mean correlating two id spaces by timestamp.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, StatusCode

logger = logging.getLogger(__name__)

#: The attribute every span carries, so a trace can be found by the same id that
#: appears in the logs and in Odoo's access log.
TRACE_ID_ATTRIBUTE: Final = "atlas.trace_id"

_tracer = trace.get_tracer("atlas")


def configure_tracing(
    *,
    endpoint: str | None,
    service_name: str = "atlas-api",
    environment: str = "development",
) -> bool:
    """Install a tracer provider exporting to ``endpoint``.

    Args:
        endpoint: OTLP collector, for example ``http://collector:4317``. When
            absent, tracing stays off and this returns ``False``.
        service_name: Reported as ``service.name`` on every span.
        environment: Reported as ``deployment.environment``, so traces from a
            staging deployment are distinguishable from production ones.

    Returns:
        Whether tracing was switched on.
    """
    if not endpoint:
        logger.debug("no OTLP endpoint configured; tracing stays off")
        return False

    try:
        # Imported here because the exporter pulls in grpc, and a deployment
        # that never traces should not pay for loading it.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
    except ImportError:
        logger.warning(
            "tracing is configured but the OTLP exporter is not installed; "
            "install atlas[otlp] or unset the endpoint",
        )
        return False

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": environment}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    logger.info("tracing enabled", extra={"endpoint": endpoint, "service": service_name})
    return True


@contextmanager
def span(
    name: str,
    *,
    trace_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Record one stage of a request.

    An exception is recorded on the span and re-raised. Swallowing it would make
    a trace that says the request succeeded while the caller saw it fail, which
    is worse than no trace at all.
    """
    with _tracer.start_as_current_span(name) as current:
        if trace_id:
            current.set_attribute(TRACE_ID_ATTRIBUTE, trace_id)
        for key, value in (attributes or {}).items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as error:
            current.set_status(StatusCode.ERROR, str(error))
            current.record_exception(error)
            raise
