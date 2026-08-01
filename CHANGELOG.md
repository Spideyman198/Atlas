# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before 1.0.0 the public surface — REST contracts, Odoo model fields, configuration
keys — may change between milestones. Breaking changes are called out explicitly.

## [Unreleased]

### Added

Development environment (M1):

- Compose stack: PostgreSQL 17 with pgvector 0.8.6, Odoo 19 CE pinned to
  `19.0-20260723`, and the Atlas engine, with health-gated startup ordering.
  Internal services publish on the loopback interface only.
- Multi-stage engine image (`builder`, `dev`, `runtime`) running as a non-root user,
  with a liveness-only container health check.
- Odoo bootstrap wrapper that initialises the database on first boot, so the stack
  is usable after a single command.
- PostgreSQL init script creating the `atlas` database, enabling `pgvector`, and
  failing if the extension is older than 0.8.
- `Makefile` and `make.ps1` with matching targets. Lint, type-check and test run in
  the `dev` image, so no local Python is required.
- Tooling configuration in the root `pyproject.toml`: ruff, mypy `--strict`, pytest
  with markers, coverage with a threshold that rises per milestone.
- `.pre-commit-config.yaml`, `.env.example`, `.dockerignore`.
- CI: lint, type-check, unit tests with coverage artefacts, compose validation,
  engine image build, and a liveness smoke test against the built image.
- `/healthz` and `/readyz` on the engine. Readiness asserts pgvector meets the
  minimum version ADR-0004 depends on.
- [Installation guide](docs/installation.md).

Planning and architecture (M0):

- Seven decision records in [`docs/adr/`](docs/adr/README.md) covering the ADR
  process, deployment topology, retrieval framework selection, vector storage and
  indexing, model providers, the data access and authorization model, and licensing.
- [Architecture overview](docs/architecture/01-overview.md): component diagrams,
  layer contracts, repository layout, and stated limits for 1.0.
- [Data architecture](docs/architecture/02-data-architecture.md): two-database
  design, entity diagrams, indexing strategy, performance notes, migration policy.
- [Request lifecycle](docs/architecture/03-request-lifecycle.md): query, ingestion
  and failure paths.
- README, [roadmap](ROADMAP.md), [contribution guide](CONTRIBUTING.md).
- LGPL-3.0-or-later licence texts, `.gitignore`, `.gitattributes` line-ending
  normalisation, `.editorconfig`.

### Changed

- ADR-0003 was revised during M0 review. The original proposal — own the retrieval
  orchestration with no general-purpose framework — was rejected. Atlas now uses
  LlamaIndex as an infrastructure-layer implementation of domain-owned ports,
  confined to `atlas.infrastructure.llamaindex` and enforced by an `import-linter`
  contract. Bridge adapters make LlamaIndex delegate to our provider and persistence
  layers, so there is one path to each model vendor and one database schema.
  Authorization stays in the application layer.
  See [ADR-0003](docs/adr/0003-rag-framework-selection.md).

[Unreleased]: https://github.com/Spideyman198/Atlas/commits/main
