# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before 1.0.0 the public surface — REST contracts, Odoo model fields, configuration
keys — may change between milestones. Breaking changes are called out explicitly.

## [Unreleased]

### Added

Vector store and persistence (M4):

- The `atlas` schema: `ingest_sources`, `documents`, `chunks`, `ingest_jobs`,
  `embedding_cache`, in a hand-written Alembic migration with a working
  downgrade. `chunks.content_tsv` is a generated column, so the lexical and dense
  sides always describe the same text.
- HNSW (`vector_cosine_ops`, `m=16`, `ef_construction=64`), GIN over the tsvector,
  and the supporting btree indexes from ADR-0004, including the partial index the
  M7 job queue will poll.
- `VectorStore` port and `PgVectorStore` adapter: content-hash existence check,
  idempotent upsert that replaces a document's chunks atomically, record deletion
  via the foreign-key cascade, and dense and lexical search with company,
  visibility and model pre-filters.
- Dense search sets `hnsw.iterative_scan = relaxed_order` so a filtered ANN query
  cannot silently return too few rows, and converts cosine distance to a score so
  every search mode sorts the same direction.
- `CandidateChunk`: retrieval results are unauthorized by construction, so the
  M6 authorization filter has a type to convert from.
- Migrations ship inside the runtime image, so a deployment can apply its own
  schema. `/readyz` compares the migrated vector width against the configured
  embedding model and reports not-ready on a mismatch.
- Integration tests against real PostgreSQL, run in CI against a pgvector service
  container. They create and migrate their own database, so the migration is
  verified against an empty cluster on every run.

Vendor adapters (M3b):

- Anthropic chat adapter. Sends `output_config.effort` and adaptive thinking,
  and sends no sampling parameters — `temperature`, `top_p` and `top_k` are
  rejected with a 400 by current models, as is `thinking.budget_tokens`. Folds
  `system`-role messages into the system parameter, renders tool results as
  user-role content blocks, drops thinking blocks from the answer, and maps a
  safety refusal onto a stop reason rather than an exception.
- OpenAI chat and embedding adapters over the Chat Completions API. Azure is
  reached through the same client with a base-URL override. Tool arguments are
  parsed from JSON strings, cached tokens are subtracted from `prompt_tokens` so
  the usage fields stay disjoint, and streamed responses request usage
  explicitly.
- Voyage embedding adapter, sending `input_type` so the document/query
  distinction reaches the vendor, and requesting `output_dimension` rather than
  assuming it.
- All five adapters registered against the M3a contract suites, driven by stub
  SDK clients — substitutability is checked at build time, not asserted.
- `atlas.config.providers`: the only module naming a concrete adapter. Wraps
  every chat provider as `Accounting(Retrying(adapter))` and disables the vendor
  SDKs' own retrying so there is one backoff policy rather than two.
- Provider settings (`ATLAS_CHAT__*`, `ATLAS_EMBEDDING__*`) including the
  offline `fake`/`hash` vendors, so the stack starts with no account.
- Providers are built in the composition root, so a missing API key, an unpriced
  model, or a dimension that disagrees with the provider stops the engine at
  startup.
- Live contract suite marked `live` and key-gated, excluded from pull requests.

Provider ports and resilience (M3a):

- `ChatProvider` and `EmbeddingProvider` ports, with the domain vocabulary they
  speak: `Message`, `ToolDefinition`, `ToolCall`, `ToolResult`, `ChatRequest`,
  `ChatResponse`, `ChatChunk`, `StopReason`, `Effort`, `TokenUsage`.
- Shared contract test suites. An adapter subclasses the contract, supplies a
  provider fixture, and inherits every assertion — substitutability is enforced
  rather than assumed.
- `FakeChatProvider` (scripted responses and errors, records requests) and
  `HashEmbeddingProvider` (deterministic, L2-normalised vectors), so the suite
  runs with no network, no API key and no cost.
- Retry decorator: jittered exponential backoff, provider-supplied `retry-after`
  taking precedence, a 30-second cap, and injectable sleep so tests exercise the
  schedule without waiting. Non-retryable provider errors fail immediately.
- Accounting decorator recording latency, token usage and estimated cost against
  the request's `trace_id`.
- Pricing table using `Decimal`. Anthropic cache multipliers are derived from the
  input rate; an unpriced model raises `ConfigurationError` rather than reporting
  a silent zero.

Engine foundations (M2):

- Layered package skeleton: `domain`, `application`, `infrastructure`,
  `interfaces`, `config`.
- Error taxonomy in `atlas.domain.errors`. Errors carry a stable `code` and
  structured context but no HTTP status; the transport mapping lives at the HTTP
  boundary so the same errors are usable from the CLI and the ingestion worker.
- RFC 9457 problem documents for error responses, served as
  `application/problem+json` and carrying `code` and `trace_id`. Unexpected
  exceptions return a generic 500 without leaking the message.
- Structured JSON logging with request correlation. `TraceIdMiddleware` adopts an
  inbound `X-Request-ID` or mints one, binds it for the request, and echoes it on
  the response. Uvicorn's own loggers are rerouted so access logs are structured
  and correlated too.
- Composition root in `atlas.config.container`, owning the lifetime of
  process-wide resources.
- Settings grouped by concern with a nested env delimiter
  (`ATLAS_DATABASE__URL`, `ATLAS_DATABASE__POOL_MAX_SIZE`), plus pool sizing and
  connect timeout.
- Four `import-linter` contracts enforcing the layering, wired into `make check`
  and CI.
- [Developer guide](docs/developer-guide.md).

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

- CI and `make test` now run `-m "unit or contract"` rather than `-m unit`. Both
  tiers are offline, and excluding `contract` meant the suite that enforces
  adapter substitutability never gated a pull request.
- Coverage floor raised from 70% to 85% (M3a). Actual coverage is 90%.
- The engine's database setting moved from `ATLAS_DATABASE_URL` to
  `ATLAS_DATABASE__URL` when settings were grouped by concern (M2). Deployments
  setting it directly must rename the variable; `docker-compose.yml` and CI are
  already updated.
- ADR-0003 was revised during M0 review. The original proposal — own the retrieval
  orchestration with no general-purpose framework — was rejected. Atlas now uses
  LlamaIndex as an infrastructure-layer implementation of domain-owned ports,
  confined to `atlas.infrastructure.llamaindex` and enforced by an `import-linter`
  contract. Bridge adapters make LlamaIndex delegate to our provider and persistence
  layers, so there is one path to each model vendor and one database schema.
  Authorization stays in the application layer.
  See [ADR-0003](docs/adr/0003-rag-framework-selection.md).

[Unreleased]: https://github.com/Spideyman198/Atlas/commits/main
