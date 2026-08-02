"""Application layer: use cases.

Orchestration only. This package depends on the ports declared in
``atlas.domain`` and never on the adapters that implement them, so a use case can
be tested against fakes with no network, no database and no API key.

Use cases arrive with the milestones that need them: ``AnswerQuestion`` in M10,
``IngestSource`` and ``SyncCorpus`` in M7, ``AuthorizationFilter`` in M6.
"""
