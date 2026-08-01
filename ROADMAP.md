# Roadmap

Odoo Atlas is built in small, independently reviewable milestones. Each one leaves
the repository in a working, committable state — no milestone depends on a later one
to make sense.

**Legend:** ✅ complete · 🚧 in progress · ⬜ planned

---

## Phase I — Foundations (M0–M5)

*Goal: a repository that boots, a chassis that scales, and an Odoo addon that stands
on its own before any AI exists.*

### ✅ M0 — Project Planning & Architecture

Decisions, recorded, before code.

- Seven ADRs covering topology, framework selection, vector storage, model
  providers, authorization, and licensing
- Architecture overview with C4 diagrams and layer contracts
- Data architecture with ER diagrams, DDL and indexing strategy
- Request lifecycle with sequence diagrams for query, ingestion, and failure paths
- Repository governance: license, contribution guide, changelog, line-ending policy

**Acceptance:** a senior engineer can read `docs/` and correctly predict the shape of
the implementation.

### ✅ M1 — Development Environment & Toolchain

- `docker-compose.yml`: Odoo 19 CE (pinned `19.0-20260723`), PostgreSQL 17 +
  pgvector 0.8.6, `atlas-api`. The `atlas-worker` service joins in M7, when its
  entrypoint exists.
- Multi-stage engine Dockerfile (`builder` / `dev` / `runtime`), non-root runtime
  user, liveness-only container health check
- Odoo bootstrap wrapper: first `up` initialises the database with demo data, so
  the stack is usable in one command
- `Makefile` plus `make.ps1` for Windows — identical targets, no local Python
  required for any of them
- Root `pyproject.toml`: ruff, mypy `--strict`, pytest, coverage; `.pre-commit-config.yaml`;
  `.env.example`
- GitHub Actions: lint, type-check, unit tests with coverage, compose validation,
  image build and a liveness smoke test
- `/healthz` and `/readyz` probes; `/readyz` asserts pgvector ≥ 0.8
- `docs/installation.md`

**Acceptance:** `make init && make up` yields a reachable Odoo and a healthy
`atlas-api` on a clean machine; `make check` is green.

### ⬜ M2 — Atlas Core Foundations

- `services/atlas` package with `domain` / `application` / `infrastructure` /
  `interfaces` / `config`
- Typed settings (`pydantic-settings`), fail-fast validation at startup
- Structured JSON logging with `trace_id` propagation via `contextvars`
- Exception taxonomy mapped to HTTP responses
- Composition root — the only module that wires adapters to ports
- `import-linter` contracts: `domain` imports nothing; nothing imports `odoo`;
  nothing outside `infrastructure.llamaindex` imports `llama_index`
- `/healthz`, `/readyz`; first unit tests

**Acceptance:** `mypy --strict` and `lint-imports` pass; the architectural rules are
enforced by CI rather than by convention.

### ⬜ M3 — Provider Abstraction Layer

- `ChatProvider` and `EmbeddingProvider` ports
- Adapters: Anthropic, OpenAI (+ Azure base-URL override), Voyage
- Decorator stack: retry with jittered backoff, timeout, circuit breaker,
  rate-limit handling, token & cost accounting
- `FakeChatProvider`, `HashEmbeddingProvider` for offline tests
- A shared **contract test suite** every adapter must pass (Liskov, enforced)

**Acceptance:** swapping provider by environment variable changes no application
code; the full suite runs with no network and no API key.

### ⬜ M4 — Vector Store & Persistence

- `atlas` database schema; Alembic migrations with working `downgrade`
- `documents`, `chunks`, `ingest_jobs`, `embedding_cache`
- HNSW + GIN + supporting indexes per
  [`02-data-architecture.md`](docs/architecture/02-data-architecture.md)
- `VectorStore` port; `PgVectorStore` adapter; hybrid search SQL
- Integration tests against a throwaway PostgreSQL container

**Acceptance:** round-trip write → dense search → lexical search → filtered search
passes against real PostgreSQL, with recall verified on a seeded fixture.

### ⬜ M5 — Odoo Addon Skeleton

- `__manifest__.py`, module structure, `LGPL-3` declaration
- Models: `atlas.conversation`, `atlas.message`, `atlas.message.citation`
- Security: two groups (User, Manager), `ir.model.access.csv`, record rules
  (own-conversations-only, multi-company)
- Views: list, form, search; menus; window actions
- `res.config.settings` extension for the settings page
- Odoo `TransactionCase` tests, including negative access-path tests

**Acceptance:** the addon installs on a clean Odoo 19 database, a non-manager user
cannot read another user's conversation, and Odoo's own test runner is green.

---

## Phase II — The Product (M6–M11)

*Goal: the assistant actually works, safely.*

### ⬜ M6 — Odoo Gateway & Authorization

- Addon REST controllers: `/atlas/api/authorize`, `/atlas/api/tool/execute`,
  `/atlas/api/records`
- Service-token auth (constant-time) + signed short-lived user context tokens
- `OdooGateway` port and HTTP adapter; batched authorization by model
- `atlas.access.log` audit model with views
- CI rule prohibiting `sudo()` in the Atlas request path
- Graceful degradation when the engine is unreachable
- `docs/api.md`

**Acceptance:** an integration test proves a restricted user's query cannot retrieve
a restricted record, and that an unreachable gateway fails **closed**.

### ⬜ M7 — Ingestion Pipeline

- Source registry: products, partners, CRM leads, sale orders, purchase orders,
  stock, invoices, `ir.attachment` PDFs, manual uploads
- Per-source rendering templates (record → retrievable text)
- Chunking via LlamaIndex node parsers behind the `DocumentLoader` port;
  `llama-index-readers-file` for PDF/DOCX
- Batch embedding with `embedding_cache` and `source_hash` short-circuit
- Postgres job queue with `FOR UPDATE SKIP LOCKED`, backoff, dead-letter state
- Incremental sync by `write_date` watermark; `ir.cron` trigger; `atlas reindex` CLI
- Ingest-source configuration wizard in Odoo

**Acceptance:** re-running a sync with no data changes performs zero embedding calls;
changing one record updates exactly its chunks, transactionally.

### ⬜ M8 — Retrieval Engine

- `Retriever` port; LlamaIndex `QueryFusionRetriever` adapter (dense + lexical,
  reciprocal rank fusion), MMR diversity, optional cross-encoder reranking
- `AtlasLlamaVectorStore` bridge so LlamaIndex retrievers query **our** schema
- Authorization post-filter as a mandatory `application` stage —
  `CandidateChunk → AuthorizedChunk` is the only path to a prompt
- Token-budgeted context assembly; citation construction from actual prompt content

**Acceptance:** hybrid retrieval measurably beats dense-only on the M12 golden set;
passing a `CandidateChunk` to the prompt assembler is a `mypy --strict` error; and
`lint-imports` confirms `llama_index` appears in exactly one package.

### ⬜ M9 — Structured Query Tools

- `find_records`, `aggregate`, `stock_levels`, `overdue_invoices`, `customer_360`
- Structured filter objects → validated Odoo domains (allow-listed fields,
  operators, types)
- Result caps and token budgeting; read-only enforcement
- Property-based tests asserting no filter input can produce an invalid or
  unauthorized domain

**Acceptance:** every worked example in the README returns a correct answer against
the Odoo demo database.

### ⬜ M10 — Orchestration & Answer Synthesis

- Intent router: `STRUCTURED` / `SEMANTIC` / `HYBRID` / `REFUSE`
- Versioned Jinja2 prompt registry
- Grounded synthesis with inline citation markers
- Explicit uncertainty handling — "I don't have information on that" is a success
- SSE streaming; conversation memory with summarisation
- Guardrails: prompt-injection resistance for ingested content

**Acceptance:** questions with no supporting context produce a refusal, not a
fabrication — asserted in tests.

### ⬜ M11 — Odoo Chat UI

- OWL 2 component: message list, composer, streaming renderer
- Conversation history sidebar with search
- Suggested prompts, seeded per installed Odoo module
- Loading/typing indicators, error and retry states
- Citation chips that open the referenced Odoo record
- Responsive, light/dark, keyboard accessible

**Acceptance:** a first-time user completes a question-to-cited-answer round trip
without instructions. Screenshots land here.

---

## Phase III — Credibility (M12–M15)

*Goal: the difference between "it demos" and "we can run this".*

### ⬜ M12 — Evaluation & Observability

- Golden question set with labelled relevant documents
- Retrieval metrics: recall@k, MRR, nDCG; regression gate in CI
- Answer faithfulness and citation-coverage judge
- OpenTelemetry tracing end to end; Prometheus metrics
- Per-conversation cost reporting surfaced in Odoo

**Acceptance:** `make eval` prints a metrics table; a retrieval regression fails CI.

### ⬜ M13 — Security Hardening & Performance

- PII detection and redaction before context enters a prompt
- Prompt-injection defenses for ingested content; output validation
- Per-user rate limiting; secrets handling review
- HNSW parameter sweep (`ef_search`, `m`) with recall/latency curves
- Query benchmarks with `EXPLAIN (ANALYZE, BUFFERS)`; caching layer
- Threat model document

**Acceptance:** documented p50/p95 latency and recall numbers replace the estimates
currently in the docs.

### ⬜ M14 — CI/CD, Release Engineering & Documentation

- Full pipeline: ruff, mypy, unit, integration, Odoo tests, coverage gate
- Security: bandit, pip-audit, Trivy, gitleaks, licence compliance
- Multi-arch image build and publish; semantic-release
- Architecture diagram exports, screenshots, demo GIF
- `docs/deployment.md`, `docs/developer-guide.md`, complete API reference

**Acceptance:** a green pipeline on every PR; a reviewer can install from the docs
alone.

### ⬜ M15 — v1.0.0

- Version freeze, CHANGELOG finalisation, tagged release, published images
- Upgrade notes and support policy

---

## Beyond 1.0

Deliberately out of scope for v1, and recorded here so the boundary is explicit:

- **Write actions** (create a lead, confirm an order) with human-in-the-loop
  confirmation and full audit
- **Local/air-gapped models** — `bge-m3` embeddings and a self-hosted chat provider
  behind the existing ports
- **Connector catalogue** — Confluence, SharePoint, Drive via the `DocumentLoader`
  port, using LlamaIndex readers
  ([ADR-0003](docs/adr/0003-rag-framework-selection.md))
- **Proactive insights** — scheduled anomaly digests rather than reactive Q&A
- **Multilingual prompts and evaluation**
- **Odoo 20 support**
