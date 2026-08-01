# Changelog

All notable changes to Odoo Atlas are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until `1.0.0` the public surface — REST contracts, Odoo model fields, configuration
keys — may change between milestones. Breaking changes are called out explicitly.

## [Unreleased]

### Changed

- **ADR-0003 revised at M0 review.** The original proposal — own the RAG
  orchestration with no general-purpose framework — was rejected. Atlas now uses
  **LlamaIndex** as an infrastructure-layer implementation of domain-owned ports
  (`Retriever`, `VectorStore`, `EmbeddingProvider`, `DocumentLoader`,
  `ChatProvider`), confined to `atlas.infrastructure.llamaindex` and enforced by an
  `import-linter` contract. Bridge adapters make LlamaIndex delegate to our provider
  and persistence layers, so there remains exactly one path to each model vendor and
  exactly one database schema. Authorization stays in the application layer and is
  enforced by the type system.
  See [ADR-0003](docs/adr/0003-rag-framework-selection.md).

### Added

- **M1 — Development Environment & Toolchain.**
  - `docker-compose.yml` bringing up PostgreSQL 17 + pgvector 0.8.6, Odoo 19 CE
    (pinned to `19.0-20260723`) and the Atlas engine, with health-gated startup
    ordering and loopback-only port publishing for internal services.
  - Multi-stage engine image (`builder` / `dev` / `runtime`) running as a
    non-root user, with a liveness-only container health check.
  - Odoo bootstrap wrapper that initialises the database with demo data on first
    boot, so the stack is usable after a single command.
  - PostgreSQL init script creating the dedicated `atlas` database, enabling
    `pgvector`, and failing loudly if the extension is older than 0.8.
  - `Makefile` and an equivalent `make.ps1` for Windows. Lint, type-check and
    test targets run inside the `dev` image, so no local Python is required.
  - Repository-wide tooling configuration in the root `pyproject.toml`: ruff
    (broad rule set, Odoo-aware per-file ignores), mypy `--strict`, pytest with
    markers, and coverage with a ratcheting threshold.
  - `.pre-commit-config.yaml` (formatting, hygiene, secret scanning),
    `.env.example`, `.dockerignore`.
  - GitHub Actions CI: lint, strict type-check, unit tests with coverage
    artefacts, compose validation, engine image build, and a liveness smoke test
    against the built image.
  - `/healthz` (liveness) and `/readyz` (readiness) probes on the engine, the
    latter asserting that pgvector meets the version ADR-0004 depends on.
  - [Installation guide](docs/installation.md).

- **M0 — Project Planning & Architecture.**
  - Architecture Decision Records ([`docs/adr/`](docs/adr/README.md)) covering the
    ADR process, sidecar deployment topology, RAG framework selection, vector store
    and index strategy, model provider strategy, the data access and authorization
    model, and licensing.
  - [Architecture overview](docs/architecture/01-overview.md): C4 context and
    container diagrams, layer contracts, target repository layout, SOLID mapping,
    cross-cutting concern placement, and stated 1.0 limits.
  - [Data architecture](docs/architecture/02-data-architecture.md): two-database
    design, ER diagrams for the Atlas and Odoo schemas, index strategy with
    rationale, performance design, and migration policy.
  - [Request lifecycle](docs/architecture/03-request-lifecycle.md): sequence diagrams
    for the query hot path, incremental ingestion, and failure/degradation
    behaviour.
  - Project [README](README.md) and sixteen-milestone [roadmap](ROADMAP.md).
  - Contribution guide with commit conventions, layering rules, and testing policy.
  - Repository governance: LGPL-3.0-or-later licence texts, `.gitignore`,
    `.gitattributes` line-ending normalisation for Windows/Linux parity, and
    `.editorconfig`.

[Unreleased]: https://github.com/<your-account>/odoo-atlas/commits/main
