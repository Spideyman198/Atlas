# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before 1.0.0 the public surface — REST contracts, Odoo model fields, configuration
keys — may change between milestones. Breaking changes are called out explicitly.

## [Unreleased]

### Added

Ingestion pipeline (M7):

- Eight sources — partners, products, attachments, CRM leads, sale and purchase
  orders, invoices, stock — declared as data in `atlas.domain.sources`. One
  renderer walks all of them, so a field nobody thought to index is one line to
  add. Documented in [docs/ingestion.md](docs/ingestion.md).
- LlamaIndex arrives, confined to `atlas.infrastructure.llamaindex`: sentence
  splitting plus PDF and DOCX readers. The containment is falsifiable — deleting
  the dependency fails exactly one test file, and an `import-linter` contract
  fails the build if it is imported anywhere else (ADR-0003).
- `SourceReader`, `DocumentLoader`, `JobQueue`, `EmbeddingCache` and
  `SourceState` ports, with in-memory doubles so retrieval work can be developed
  and tested without PostgreSQL or an Odoo.
- Re-running a sync with nothing changed makes no embedding calls: the content
  hash is checked before the provider is called. Attachments are compared by the
  checksum Odoo already holds, so an unchanged contract is never downloaded.
- The hash carries record identity as well as content, so two records that
  render identically — two contacts with the same name and nothing else filled
  in — cannot collide on the unique index and overwrite one another.
- Segment-level embedding cache keyed by `(content_hash, model)`. A changed
  order re-embeds the line that changed, not the eleven that did not.
- `ingest_jobs` claimed with `SELECT ... FOR UPDATE SKIP LOCKED`: a durable queue
  several workers can drain with no broker. Exponential backoff, a `dead` state
  deliberately distinct from `failed`, and a stale sweep that returns a crashed
  worker's job without refunding the attempt it burned.
- Incremental sync by `write_date` watermark, which only ever moves forward.
- The `atlas` command line (`sources`, `sync`, `reindex`, `worker`), the
  `atlas-worker` compose service, `POST /v1/ingest/sync`, an `ir.cron` trigger
  and an indexing wizard in Odoo. Sources are off until somebody turns them on.
- Ingestion reads as a dedicated integration user (`ATLAS_INGEST_UID`, holding
  `odoo_atlas.group_atlas_ingest`) through its own endpoints. That account sees
  more than any one person does, which is precisely why the query-time check
  cannot be skipped — and it is still an ordinary Odoo account, with no `sudo()`.

### Fixed

- `PgVectorStore.upsert_document` now removes any other document for the same
  record inside the same transaction. An edited record renders differently and so
  hashes differently, which meant the previous version's chunks lingered and the
  record was retrievable twice — once as it is and once as it was.

Odoo gateway and authorization (M6):

- The callback API the engine asks Odoo through: `/atlas/api/authorize`,
  `/atlas/api/records`, `/atlas/api/tool/execute` and `/atlas/api/status`,
  documented in [docs/api.md](docs/api.md). Every read runs as the acting user.
- Two secrets doing two jobs. `ATLAS_SERVICE_TOKEN` proves a call came from the
  engine; `ATLAS_CONTEXT_SECRET` signs the short-lived tokens naming the acting
  user and is never given to the engine, so it can replay a context Odoo issued
  but cannot mint one. An unset secret refuses every call.
- Context tokens are re-checked against the database on every use, so
  deactivating an account or removing its Atlas group takes effect on the next
  call. The companies in a token are intersected with the ones the user still
  has, so it cannot widen its own scope.
- `OdooGateway` port, an httpx adapter and an in-memory fake, held to one shared
  contract suite. Authorization is batched by model.
- `AuthorizedChunk`, produced only by `AuthorizationFilter`. Skipping the
  authorization step is a type error rather than a policy that could be
  forgotten. Every failure — including exception types nobody anticipated —
  collapses into a refusal and no chunks.
- `atlas.access.log`: who acted, which model, how many ids were asked about,
  granted and refused, the trace id and the duration. Append-only through the
  ORM, written as the acting user, and failing to write one fails the request.
- The `sudo()` prohibition from ADR-0006 enforced by a test that scans the
  addon's source. No allow-list and no exceptions.
- `/readyz` gains a gating `odoo` check: with Odoo unreachable the engine can
  clear no candidates, so every answer it could give would be a refusal.
- A *Test Connection* button on the settings page, and a hard timeout on the
  addon's engine client, so an engine outage degrades the assistant and not Odoo.

### Changed

- Atlas configuration on the Odoo side moved from `ir.config_parameter` to the
  server's environment (`ATLAS_ENGINE_URL`, `ATLAS_ENGINE_TIMEOUT`,
  `ATLAS_CONTEXT_TOKEN_TTL`). Reading a config parameter needs system rights, and
  the code that reads it runs as whichever user asked a question — so a parameter
  would have forced a `sudo()` onto the request path. The settings page now
  reports the configuration read-only instead of offering to overrule it. The
  M5 `odoo_atlas.service_token` parameter is gone; set `ATLAS_SERVICE_TOKEN`
  instead.
- The engine refuses to start without `ATLAS_ODOO__SERVICE_TOKEN`. Odoo
  authorises every retrieval, so an engine without one could retrieve candidates
  and clear none of them — an outage that looks like an assistant which knows
  nothing rather than like a missing environment variable.

Odoo addon skeleton (M5):

- `odoo_atlas`, the Odoo half of Atlas: `atlas.conversation`, `atlas.message` and
  `atlas.message.citation`, with list, form and search views, menus and window
  actions. It depends on `base` and `web` only and contains no AI code.
- A conversation is titled from its first question, leaves `draft` when it gets
  one, and carries a stored message count and provider cost.
- Citations are rows, not a JSON blob: they resolve to the live record through a
  computed reference, and keep the name the record had when it was cited so they
  still read sensibly once it is deleted.
- Two groups — Atlas user and Atlas administrator — with `ir.model.access.csv`
  and three record rules per model: ownership, administrator, and a group-less
  multi-company rule, which is global and so binds administrators too. Neither
  group is implied by `base.group_user`; access is granted per user.
- Messages and citations store their own `user_id` and `company_id`, copied from
  the conversation, so every record rule is a comparison against an indexed
  column rather than a join back to `atlas_conversation`.
- A conversation cannot change owner, administrator included. Its answers were
  computed under one user's access rights, so reassigning it would show a second
  user results drawn from records they may not read.
- Settings page for the engine URL, the service token and the request timeout.
- 40 `TransactionCase` tests including the negative access paths, run by Odoo's
  own runner: `make test-odoo` installs the addon into a database created from
  nothing, so a pass means it installs cleanly as well as behaves. CI gains an
  `addon` job that does the same.

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
