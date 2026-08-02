# Roadmap

Atlas is built in milestones. Each one leaves the repository in a working state and
can be reviewed on its own; none depends on a later milestone to make sense.

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Project planning and architecture | Done |
| M1 | Development environment and toolchain | Done |
| M2 | Engine foundations | Done |
| M3 | Provider abstraction layer | Planned |
| M4 | Vector store and persistence | Planned |
| M5 | Odoo addon skeleton | Planned |
| M6 | Odoo gateway and authorization | Planned |
| M7 | Ingestion pipeline | Planned |
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

## M3 — Provider abstraction layer

- `ChatProvider` and `EmbeddingProvider` ports
- Adapters for Anthropic, OpenAI (including Azure via base-URL override) and Voyage
- Decorators for retry with jittered backoff, timeout, circuit breaking, rate-limit
  handling, and token and cost accounting
- `FakeChatProvider` and `HashEmbeddingProvider` for offline tests
- A shared contract test suite every adapter must pass

Acceptance: switching provider by environment variable changes no application code,
and the full suite runs without network access or an API key.

## M4 — Vector store and persistence

- `atlas` database schema; Alembic migrations with working downgrades
- `documents`, `chunks`, `ingest_jobs`, `embedding_cache`
- HNSW, GIN and supporting indexes per
  [docs/architecture/02-data-architecture.md](docs/architecture/02-data-architecture.md)
- `VectorStore` port and `PgVectorStore` adapter; hybrid search SQL
- Integration tests against a throwaway PostgreSQL container

Acceptance: write, dense search, lexical search and filtered search round-trip
against real PostgreSQL, with recall verified on a seeded fixture.

## M5 — Odoo addon skeleton

- Manifest, module structure, `LGPL-3` declaration
- Models: `atlas.conversation`, `atlas.message`, `atlas.message.citation`
- Security: two groups, `ir.model.access.csv`, record rules for own-conversations
  and multi-company scoping
- List, form and search views; menus; window actions
- `res.config.settings` extension for the settings page
- `TransactionCase` tests including negative access paths

Acceptance: the addon installs on a clean Odoo 19 database, a non-manager cannot
read another user's conversation, and Odoo's test runner passes.

## M6 — Odoo gateway and authorization

- Addon controllers: `/atlas/api/authorize`, `/atlas/api/tool/execute`,
  `/atlas/api/records`
- Constant-time service-token auth plus signed short-lived user context tokens
- `OdooGateway` port and HTTP adapter with authorization batched by model
- `atlas.access.log` audit model and views
- CI rule prohibiting `sudo()` in the Atlas request path
- Graceful degradation when the engine is unreachable
- `docs/api.md`

Acceptance: an integration test shows a restricted user cannot retrieve a restricted
record, and that an unreachable gateway fails closed.

## M7 — Ingestion pipeline

- Source registry for products, partners, CRM leads, sale and purchase orders,
  stock, invoices, `ir.attachment` PDFs and manual uploads
- Per-source templates rendering records to retrievable text
- Chunking through LlamaIndex node parsers behind the `DocumentLoader` port;
  `llama-index-readers-file` for PDF and DOCX
- Batch embedding with `embedding_cache` and a `source_hash` short-circuit
- Postgres job queue using `FOR UPDATE SKIP LOCKED`, with backoff and a dead-letter
  state
- Incremental sync by `write_date` watermark, `ir.cron` trigger, `atlas reindex` CLI
- Ingest-source configuration wizard in Odoo

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
