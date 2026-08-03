# Roadmap

Atlas is built in milestones. Each one leaves the repository in a working state and
can be reviewed on its own; none depends on a later milestone to make sense.

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Project planning and architecture | Done |
| M1 | Development environment and toolchain | Done |
| M2 | Engine foundations | Done |
| M3a | Provider ports, fakes and resilience | Done |
| M3b | Vendor adapters (Anthropic, OpenAI, Voyage) | Done |
| M4 | Vector store and persistence | Done |
| M5 | Odoo addon skeleton | Done |
| M6 | Odoo gateway and authorization | Done |
| M7 | Ingestion pipeline | Done |
| M8 | Retrieval engine | Planned |
| M9 | Structured query tools | Planned |
| M10 | Orchestration and answer synthesis | Planned |
| M11 | Odoo chat UI | Planned |
| M12 | Evaluation and observability | Planned |
| M13 | Security hardening and performance | Planned |
| M14 | CI/CD, release engineering and documentation | Planned |
| M15 | 1.0.0 | Planned |

## M0 — Project planning and architecture

Status: done.

- Seven decision records covering topology, framework selection, vector storage,
  model providers, the authorization model and licensing
- Architecture overview with component diagrams and layer contracts
- Data architecture with entity diagrams, DDL and indexing strategy
- Request lifecycle covering query, ingestion and failure paths
- Licence, contribution guide, changelog, line-ending policy

Acceptance: a reader can work through `docs/` and predict the shape of the
implementation.

## M1 — Development environment and toolchain

Status: done.

- Compose stack: Odoo 19 CE (pinned `19.0-20260723`), PostgreSQL 17 with pgvector
  0.8.6, and the engine. The ingestion worker joins in M7 when its entrypoint exists.
- Multi-stage engine image (`builder`, `dev`, `runtime`), non-root runtime user,
  liveness-only container health check
- Odoo bootstrap wrapper so the first `up` initialises the database with demo data
- `Makefile` and `make.ps1` with identical targets; neither needs local Python
- Root `pyproject.toml` for ruff, mypy `--strict`, pytest and coverage;
  `.pre-commit-config.yaml`; `.env.example`
- CI: lint, type-check, unit tests with coverage, compose validation, image build,
  liveness smoke test
- `/healthz` and `/readyz`, the latter asserting pgvector 0.8 or newer

Acceptance: `make init && make up` gives a reachable Odoo and a healthy engine on a
clean machine, and `make check` passes.

## M2 — Engine foundations

Status: done.

- Package skeleton: `domain`, `application`, `infrastructure`, `interfaces`, `config`
- Typed settings with fail-fast validation, grouped by concern
  (`ATLAS_DATABASE__URL`)
- Structured JSON logging with `trace_id` propagation, including uvicorn access logs
- Error taxonomy in the domain, mapped to RFC 9457 problem documents at the HTTP
  boundary
- Composition root owning process-wide resources
- Four `import-linter` contracts: domain independence, application depends on ports
  only, the engine never imports `odoo`, and `llama_index` stays in its adapter
  package

Acceptance: `mypy --strict` and `lint-imports` pass, and a deliberate violation is
shown to break the relevant contract rather than pass silently.

## M3a — Provider ports, fakes and resilience

Status: done.

- `ChatProvider` and `EmbeddingProvider` ports, plus the domain vocabulary they
  speak: messages, tool definitions and calls, stop reasons, effort, token usage
- Shared contract test suites — subclass, supply a provider fixture, and the whole
  suite runs against that adapter
- `FakeChatProvider` (scripted, records requests) and `HashEmbeddingProvider`
  (deterministic, L2-normalised) for offline tests
- Retry decorator with jittered exponential backoff, provider-supplied
  `retry-after`, a backoff cap, and injectable sleep for testing
- Accounting decorator recording latency, token usage and estimated cost
- Pricing table with Decimal arithmetic; an unpriced model raises rather than
  reporting zero

Acceptance: the suite runs with no network and no API key; decorators compose and
preserve provider identity.

## M3b — Vendor adapters

Status: done.

- Anthropic chat adapter: adaptive thinking, effort mapped to `output_config`,
  tool calling, streaming, refusal as a stop reason
- OpenAI chat and embedding adapters, Azure reachable via base-URL override
- Voyage embedding adapter, honouring the document/query distinction
- All five registered against the M3a contract suites, driven by stub SDK clients
- Provider settings and composition-root wiring, validated at startup
- Live contract suite marked `live`, key-gated, excluded from pull requests

Acceptance: switching provider by environment variable changes no application
code; every adapter passes the same contract suite; a missing key, unpriced model
or dimension mismatch stops the engine at boot.

Carried into M14: verify the OpenAI and Voyage pricing entries against live
accounts, and schedule the `live` suite nightly with repository secrets.

## M4 — Vector store and persistence

Status: done.

- `atlas` schema as five tables — `ingest_sources`, `documents`, `chunks`,
  `ingest_jobs`, `embedding_cache` — with a hand-written Alembic migration and a
  working downgrade ([ADR-0008](docs/adr/0008-hand-written-migrations-and-explicit-sql.md))
- HNSW, GIN and supporting indexes per
  [docs/architecture/02-data-architecture.md](docs/architecture/02-data-architecture.md),
  plus a generated `content_tsv` so the dense and lexical sides cannot drift
- `VectorStore` port and `PgVectorStore` adapter: idempotent upsert, atomic
  delete-and-replace of chunks, dense and lexical search, company/visibility/model
  pre-filters
- `CandidateChunk` — retrieval results are unauthorized by construction, ready for
  the M6 filter
- Migrations ship in the runtime image; `/readyz` compares the migrated vector
  width against the configured embedding model
- Integration tests against real PostgreSQL, wired into CI as a service container

Acceptance: write, dense search, lexical search and filtered search round-trip
against real PostgreSQL; `alembic upgrade`/`downgrade`/`upgrade` is verified end to
end and readiness follows the schema state.

## M5 — Odoo addon skeleton

Status: done.

- `odoo_atlas`: manifest, module structure, `LGPL-3`, depending on `base` and
  `web` only — the business modules Atlas indexes are read by name at M7, which
  needs no dependency on them
- Models: `atlas.conversation`, `atlas.message`, `atlas.message.citation`. A
  conversation is titled from its first question, leaves `draft` on it, and keeps
  a stored message count and provider cost
- Citations resolve to the live record through a computed `Reference`, and keep
  the name the record had when it was cited so they survive its deletion
- Security: two groups, `ir.model.access.csv`, and three record rules per model —
  ownership, administrator, and a group-less multi-company rule that binds
  administrators too. `user_id` and `company_id` are stored on messages and
  citations so each rule is an indexed comparison, not a join
- A conversation cannot change owner, administrator included: its answers were
  computed under one user's access rights
- List, form and search views, menus, window actions, and a `res.config.settings`
  page for the engine URL, service token and timeout
- 40 `TransactionCase` tests, including the negative access paths, run by Odoo's
  own runner via `make test-odoo` and gating CI in a new `addon` job

Acceptance: the addon installs on a clean Odoo 19 database, a non-manager cannot
read another user's conversation, and Odoo's test runner passes.

## M6 — Odoo gateway and authorization

Status: done.

- Addon controllers: `/atlas/api/authorize`, `/atlas/api/records`,
  `/atlas/api/tool/execute`, and `/atlas/api/status` for the readiness probe.
  Every read runs as the acting user; archived records count as readable, since
  archiving is not a permission
- Constant-time service-token auth plus signed short-lived context tokens. Two
  separate secrets: the engine holds the shared one and not the signing key, so
  it can replay a context Odoo issued but cannot mint one. Both come from the
  environment, never from `ir.config_parameter`
- Verification re-reads the user, so revoking access takes effect on the next
  call, and intersects the token's companies with what the user still has
- `OdooGateway` port, HTTP adapter and in-memory fake, held together by a shared
  contract suite; authorization batched by model
- `AuthorizedChunk`: the filter is the only way to obtain one, so bypassing
  stage 2 is a type error rather than a policy
- `atlas.access.log`, append-only, written as the acting user, with views
- The `sudo()` prohibition enforced by a test that scans the addon — no
  allow-list and no exceptions
- `/readyz` gains a gating `odoo` check; the addon's engine client has a hard
  timeout and reports failure as a value
- [`docs/api.md`](docs/api.md)

Acceptance: an integration test shows a restricted user cannot retrieve a restricted
record, and that an unreachable gateway fails closed.

## M7 — Ingestion pipeline

Status: done. See [docs/ingestion.md](docs/ingestion.md).

- Eight sources — partners, products, attachments, CRM leads, sale and purchase
  orders, invoices, stock — declared as data rather than eight renderers. A
  source whose module is not installed reports itself unavailable instead of
  failing a sync halfway through
- Templates render labelled prose, with selection labels rather than keys and
  order lines indexed alongside their order, because that is what makes a record
  findable
- LlamaIndex arrives, confined to `atlas.infrastructure.llamaindex`: sentence
  splitting and the PDF and DOCX readers. Deleting it fails exactly one test file
- `SourceReader`, `DocumentLoader`, `JobQueue`, `EmbeddingCache` and
  `SourceState` ports, with in-memory doubles the M8 work can develop against
- Content hash carrying record identity as well as text, so two records that
  render identically cannot overwrite one another; attachments compared by the
  checksum Odoo already holds, so an unchanged contract is never downloaded
- Segment-level embedding cache keyed by `(content_hash, model)`
- `ingest_jobs` claimed with `FOR UPDATE SKIP LOCKED`, exponential backoff, a
  dead-letter state distinct from `failed`, and a stale sweep that does not
  refund the attempt a crashed worker burned
- `write_date` watermark that only moves forward; `ir.cron` trigger; the
  `atlas` CLI (`sources`, `sync`, `reindex`, `worker`); the `atlas-worker`
  service; an indexing wizard in Odoo
- Ingestion reads as a dedicated integration user with its own group, so what it
  may index is decided by Odoo's access rules and still no `sudo()`

Acceptance: re-running a sync with no data changes performs zero embedding calls,
and changing one record updates exactly its chunks, transactionally.

## M8 — Retrieval engine

- `Retriever` port and a LlamaIndex `QueryFusionRetriever` adapter combining dense
  and lexical results with reciprocal rank fusion, MMR diversity and optional
  cross-encoder reranking
- `AtlasLlamaVectorStore` bridge so LlamaIndex retrievers query our schema
- Authorization filter as a mandatory application stage;
  `CandidateChunk` to `AuthorizedChunk` is the only route into a prompt
- Token-budgeted context assembly and citations built from the assembled context

Acceptance: hybrid retrieval beats dense-only on the M12 golden set; passing a
`CandidateChunk` to the prompt assembler is a `mypy --strict` error; `lint-imports`
confirms `llama_index` appears in one package.

## M9 — Structured query tools

- `find_records`, `aggregate`, `stock_levels`, `overdue_invoices`, `customer_360`
- Structured filter objects compiled to Odoo domains, validated against per-model
  allow-lists for fields, operators and types
- Result caps, token budgeting, read-only enforcement
- Property-based tests showing no filter input produces an invalid or unauthorized
  domain

Acceptance: every example in the README returns a correct answer against the Odoo
demo database.

## M10 — Orchestration and answer synthesis

- Intent router: structured, semantic, hybrid, refuse
- Versioned Jinja2 prompt registry
- Grounded synthesis with inline citation markers
- Explicit uncertainty handling; "I don't have information on that" is a valid answer
- SSE streaming; conversation memory with summarisation
- Prompt-injection resistance for ingested content

Acceptance: questions with no supporting context produce a refusal rather than a
fabrication, asserted in tests.

## M11 — Odoo chat UI

- OWL component: message list, composer, streaming renderer
- Conversation history sidebar with search
- Suggested prompts seeded from installed Odoo modules
- Loading, error and retry states
- Citation chips that open the referenced record
- Responsive, light and dark, keyboard accessible

Acceptance: a first-time user completes a question-to-cited-answer round trip
without instructions.

## M12 — Evaluation and observability

- Golden question set with labelled relevant documents
- Retrieval metrics: recall@k, MRR, nDCG, with a regression gate in CI
- Answer faithfulness and citation coverage checks
- OpenTelemetry tracing and Prometheus metrics
- Per-conversation cost reporting in Odoo

Acceptance: `make eval` prints a metrics table and a retrieval regression fails CI.

## M13 — Security hardening and performance

- PII detection and redaction before context enters a prompt
- Prompt-injection defences for ingested content; output validation
- Per-user rate limiting; secrets handling review
- HNSW parameter sweep with recall and latency curves
- Query benchmarks with `EXPLAIN (ANALYZE, BUFFERS)`; caching layer
- Threat model

Acceptance: measured p50 and p95 latency and recall numbers replace the estimates
currently in the docs.

## M14 — CI/CD, release engineering and documentation

- Full pipeline: ruff, mypy, unit, integration and Odoo tests, coverage gate
- Security: bandit, pip-audit, Trivy, gitleaks, licence compliance
- Multi-arch image build and publish; semantic-release
- Diagram exports, screenshots, demo recording
- `docs/deployment.md`, `docs/developer-guide.md`, complete API reference

Acceptance: a green pipeline on every pull request, and a reviewer can install from
the documentation alone.

## M15 — 1.0.0

Version freeze, changelog finalisation, tagged release, published images, upgrade
notes and support policy.

## Out of scope for 1.0

Recorded here so the boundary is explicit:

- Write actions such as creating a lead or confirming an order, with human
  confirmation and audit
- Local and air-gapped models — `bge-m3` embeddings and a self-hosted chat provider
  behind the existing ports
- Connector catalogue for Confluence, SharePoint and Drive via the `DocumentLoader`
  port, using LlamaIndex readers
- Scheduled anomaly digests rather than reactive question answering
- Multilingual prompts and evaluation
- Odoo 20 support
