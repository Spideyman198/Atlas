"""Prometheus metrics.

What is counted is chosen from the questions somebody actually asks when the
assistant is behaving badly, rather than from what is easy to instrument:

- *Is it answering, refusing, or failing?* — ``atlas_answers_total`` by outcome.
- *Is retrieval finding anything, and how much is authorization removing?* —
  the denial rate is the number that says whether over-fetch is set right.
- *Is it slow because of the model, Odoo, or the database?* — one histogram per
  stage rather than one for the request, because a single end-to-end number
  cannot be acted on.
- *What is it costing?* — tokens by provider and model.

Labels are deliberately low-cardinality. Nothing here is labelled by user, by
conversation or by question: those multiply series without bound, and the one
thing worse than no metrics is a metrics backend that fell over.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: Its own registry rather than the process-global default. A shared default
#: makes two test cases that both import this module fail on a duplicate
#: registration, and leaves no way to reset between them.
REGISTRY: Final = CollectorRegistry()

#: Seconds. Buckets chosen for what these operations actually cost: retrieval in
#: tens of milliseconds, a model call in seconds, and a long tail that matters
#: because a 30-second answer is a user who has already given up.
_FAST_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_SLOW_BUCKETS: Final = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0)

answers = Counter(
    "atlas_answers_total",
    "Answers produced, by how they turned out.",
    ["outcome", "intent"],
    registry=REGISTRY,
)

answer_duration = Histogram(
    "atlas_answer_duration_seconds",
    "End-to-end time to produce an answer.",
    ["intent"],
    buckets=_SLOW_BUCKETS,
    registry=REGISTRY,
)

retrieval_duration = Histogram(
    "atlas_retrieval_duration_seconds",
    "Time spent in retrieval, before authorization.",
    buckets=_FAST_BUCKETS,
    registry=REGISTRY,
)

chunks = Counter(
    "atlas_chunks_total",
    "Chunks by what happened to them: retrieved, authorized, denied, used.",
    ["stage"],
    registry=REGISTRY,
)

authorization_duration = Histogram(
    "atlas_authorization_duration_seconds",
    "Time spent asking Odoo what the acting user may read.",
    buckets=_FAST_BUCKETS,
    registry=REGISTRY,
)

tool_calls = Counter(
    "atlas_tool_calls_total",
    "Tool calls, by tool and outcome.",
    ["tool", "outcome"],
    registry=REGISTRY,
)

tool_duration = Histogram(
    "atlas_tool_duration_seconds",
    "Time spent executing a tool in Odoo.",
    ["tool"],
    buckets=_FAST_BUCKETS,
    registry=REGISTRY,
)

provider_calls = Counter(
    "atlas_provider_calls_total",
    "Model provider calls, by provider and outcome.",
    ["provider", "outcome"],
    registry=REGISTRY,
)

tokens = Counter(
    "atlas_tokens_total",
    "Tokens billed, by provider, model and kind.",
    ["provider", "model", "kind"],
    registry=REGISTRY,
)

ingested = Counter(
    "atlas_documents_ingested_total",
    "Documents written to the corpus, by source and outcome.",
    ["source", "outcome"],
    registry=REGISTRY,
)


def render() -> bytes:
    """The current values, in Prometheus' exposition format."""
    return generate_latest(REGISTRY)
