"""The recorder that actually writes to Prometheus.

Every method swallows its own failures. A metrics backend is not worth a failed
answer, and a malformed label — which is a programming mistake, not a runtime
condition — should show up as a missing series and a log line rather than as a
500 on somebody's question.
"""

from __future__ import annotations

import logging

from atlas.infrastructure.observability import metrics

logger = logging.getLogger(__name__)


class PrometheusRecorder:
    """Implements :class:`~atlas.domain.observability.Recorder`."""

    def answer_finished(self, *, outcome: str, intent: str, seconds: float) -> None:
        """Count the answer and record how long it took."""
        try:
            metrics.answers.labels(outcome=outcome, intent=intent).inc()
            metrics.answer_duration.labels(intent=intent).observe(seconds)
        except Exception:  # noqa: BLE001 - a metric is never worth a failed answer
            _swallow("answer_finished")

    def retrieval_finished(
        self, *, candidates: int, authorized: int, used: int, seconds: float
    ) -> None:
        """Record what survived each stage, and how long retrieval took."""
        try:
            metrics.retrieval_duration.observe(seconds)
            metrics.chunks.labels(stage="retrieved").inc(candidates)
            metrics.chunks.labels(stage="authorized").inc(authorized)
            metrics.chunks.labels(stage="denied").inc(max(candidates - authorized, 0))
            metrics.chunks.labels(stage="used").inc(used)
        except Exception:  # noqa: BLE001 - a metric is never worth a failed answer
            _swallow("retrieval_finished")

    def tool_finished(self, *, tool: str, outcome: str, seconds: float) -> None:
        """Count the tool call and record how long Odoo took over it."""
        try:
            metrics.tool_calls.labels(tool=tool, outcome=outcome).inc()
            metrics.tool_duration.labels(tool=tool).observe(seconds)
        except Exception:  # noqa: BLE001 - a metric is never worth a failed answer
            _swallow("tool_finished")

    def provider_finished(
        self,
        *,
        provider: str,
        model: str,
        outcome: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Count the call and the tokens it billed."""
        try:
            metrics.provider_calls.labels(provider=provider, outcome=outcome).inc()
            if input_tokens:
                metrics.tokens.labels(provider=provider, model=model, kind="input").inc(
                    input_tokens
                )
            if output_tokens:
                metrics.tokens.labels(provider=provider, model=model, kind="output").inc(
                    output_tokens
                )
        except Exception:  # noqa: BLE001 - a metric is never worth a failed answer
            _swallow("provider_finished")


def _swallow(event: str) -> None:
    """Log a metrics failure at debug and carry on.

    Debug rather than warning: if the backend is broken this fires on every
    request, and a log full of "could not record a metric" is how the line that
    mattered gets lost.
    """
    logger.debug("could not record %s", event, exc_info=True)
