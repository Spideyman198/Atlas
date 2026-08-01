# `atlas` — the Odoo Atlas engine

The retrieval, orchestration and generation engine. Runs as a sidecar beside
Odoo ([ADR-0002](../../docs/adr/0002-sidecar-service-topology.md)) and **never
imports `odoo`** — it reaches the ERP only over HTTP.

## Layout

```
src/atlas/
├── domain/          entities, value objects, ports. Zero I/O. Imports nothing else from atlas.
├── application/     use cases. Depends on ports, never on adapters.
├── infrastructure/  adapters
│   ├── llamaindex/    the ONLY package permitted to import llama_index (ADR-0003)
│   ├── persistence/   PgVectorStore — our schema, SQLAlchemy Core
│   ├── providers/     Anthropic / OpenAI / Voyage SDK adapters
│   └── odoo/          OdooGateway HTTP adapter
├── interfaces/      FastAPI routers, CLI entrypoints
├── config/          typed settings and the composition root
└── prompts/         versioned Jinja2 templates
```

Only `config` knows which concrete adapter satisfies which port. Everything else
receives its collaborators by injection — which is what makes the test suite
runnable with no network and no API key.

Three rules are enforced by `import-linter` in CI (from M2), not by convention:

1. `domain` imports nothing else from `atlas`.
2. Nothing in `atlas` imports `odoo`.
3. Nothing outside `atlas.infrastructure.llamaindex` imports `llama_index`.

## Current state (M1)

Only `interfaces/http` and `config` exist, carrying the liveness and readiness
probes that prove the deployment topology works. The layers above arrive in M2.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Target interpreter is **Python 3.12** — the version in the runtime image. See
[CONTRIBUTING.md](../../CONTRIBUTING.md).
