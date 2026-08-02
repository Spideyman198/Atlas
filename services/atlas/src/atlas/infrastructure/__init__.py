"""Infrastructure layer: adapters.

Concrete implementations of the ports in ``atlas.domain``. Everything that talks to
a network, a database or a third-party SDK lives here, and nothing above this layer
imports it directly — the composition root in ``atlas.config`` performs the binding.

Sub-packages arrive with the milestones that need them:

``providers``
    Anthropic, OpenAI and Voyage SDK adapters (M3).
``persistence``
    ``PgVectorStore`` over the schema in ADR-0004 (M4).
``odoo``
    ``OdooGateway`` HTTP adapter (M6).
``llamaindex``
    The only package permitted to import ``llama_index`` (M7, M8). An
    ``import-linter`` contract fails the build if the import appears elsewhere.
"""
