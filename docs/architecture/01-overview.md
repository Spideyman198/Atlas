# Architecture Overview

Read [`docs/adr/`](../adr/README.md) for why the system is shaped this way. This
document describes what it is.

## 1. System context

```mermaid
flowchart LR
    user[Business user] --> odoo
    admin[Odoo administrator] --> odoo
    odoo[Odoo 19 CE and odoo_atlas addon] -->|queries and ingest commands| api
    api[Atlas engine] -->|authorized reads as the acting user| odoo
    api -->|chat completion and tool calling| llm[LLM provider]
    api -->|batch embeddings| emb[Embedding provider]
```

Two properties of this picture carry most of the design:

1. The engine has no direct read path to ERP data. Every read goes back through
   Odoo, executed as the acting user
   ([ADR-0006](../adr/0006-data-access-and-authorization.md)).
2. Users only ever talk to Odoo. The engine is not publicly exposed.

## 2. Container view

```mermaid
flowchart TB
    owl["OWL chat component"] -->|JSON-RPC| addon
    cron["ir.cron sync trigger"] --> addon
    addon["odoo_atlas addon"] -->|HTTP and service token| iface
    iface["interfaces - routers and CLI"] --> app
    app["application - use cases"] --> dom
    app --> infra
    dom["domain - entities and ports"]
    infra["infrastructure - adapters"] -.->|implements ports| dom
    infra -->|authorized reads| addon
    addon --> dbodoo["Odoo database"]
    infra --> dbatlas["Atlas database with pgvector"]
    infra --> llm["LLM provider"]
    infra --> emb["Embedding provider"]
```

The dashed arrow is the **Dependency Inversion Principle** drawn literally:
`application` depends on the abstractions in `domain`, and `infrastructure` supplies
implementations.

Three rules make that diagram true, and all three are enforced in CI by
`import-linter` contracts (M2) rather than by convention:

1. `domain` imports nothing else from `atlas`, and performs no I/O.
2. Nothing anywhere in `atlas` imports `odoo` — the engine reaches Odoo only over
   HTTP ([ADR-0002](../adr/0002-sidecar-service-topology.md)).
3. Nothing outside `atlas.infrastructure.llamaindex` imports `llama_index` — the
   retrieval framework is a swappable detail ([ADR-0003](../adr/0003-rag-framework-selection.md)).

## 3. Target repository layout

Paths marked *(Mx)* are not built yet and arrive at that milestone; everything else
exists today. See [ROADMAP.md](../../ROADMAP.md).

```
odoo-atlas/
├── .github/workflows/            #   lint, type, test, security, release
├── addons/
│   └── odoo_atlas/               #   the Odoo addon — a thin adapter
│       ├── __manifest__.py
│       ├── models/               #   atlas.conversation, atlas.message, settings
│       ├── controllers/          #   REST endpoints the engine calls back into
│       ├── services/             #   context tokens, secrets, engine client
│       ├── wizards/              #   ingest-source configuration wizard
│       ├── views/                #   XML: forms, lists, menus, actions, settings
│       ├── security/             #   groups, ir.model.access.csv, record rules
│       ├── data/                 #   ir.cron, suggested prompts
│       ├── static/src/           # (M11) OWL components, SCSS, XML templates
│       └── tests/                #   Odoo TransactionCase / HttpCase
├── services/
│   └── atlas/                    #   the engine — framework-agnostic library
│       ├── pyproject.toml
│       ├── src/atlas/
│       │   ├── domain/           # (M2) entities, value objects, ports. Zero I/O.
│       │   ├── application/      # (M2) use cases. Orchestration only.
│       │   ├── infrastructure/   # (M3) adapters
│       │   │   ├── llamaindex/   #     ONLY package that may import llama_index
│       │   │   ├── persistence/  #     PgVectorStore — our schema, explicit SQL
│       │   │   ├── providers/    #     Anthropic / OpenAI / Voyage SDK adapters
│       │   │   └── odoo/         #     OdooGateway HTTP adapter and fake
│       │   ├── interfaces/       #   FastAPI routers, CLI entrypoints
│       │   ├── config/           #   typed settings, composition root
│       │   └── prompts/          #   versioned Jinja2 templates
│       ├── migrations/           #   Alembic
│       └── tests/{unit,integration,contract}
├── evaluation/                   # (M12) golden question set + metrics harness
├── docker/                       #   Dockerfiles per image
├── docs/
│   ├── adr/
│   ├── architecture/
│   ├── assets/                   # (M14) diagrams and screenshots
│   ├── installation.md
│   ├── developer-guide.md
│   ├── api.md                    #   the Odoo callback API
│   ├── ingestion.md              #   sources, cost, the job queue
│   ├── retrieval.md              #   hybrid search, fusion, assembly
│   └── deployment.md             # (M14)
├── scripts/                      # (M14) seed helpers; `atlas` CLI covers reindex
├── docker-compose.yml
├── Makefile
├── pyproject.toml                #   workspace-level tooling config
├── README.md
├── ROADMAP.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE / COPYING
└── .gitignore / .gitattributes / .editorconfig
```

## 4. The layers, and what belongs in each

| Layer | May import | Must never import | Contains |
| --- | --- | --- | --- |
| `domain` | stdlib, `pydantic` | anything else in `atlas` | `Document`, `Chunk`, `Citation`, `Conversation`, `RetrievalResult`, `ChatRequest`; the `Protocol` definitions for every port |
| `application` | `domain` | `infrastructure`, `interfaces`, any SDK | Use cases: `AnswerQuestion`, `IngestSource`, `SyncCorpus`, `EvaluateRetrieval`. Pure orchestration, no I/O primitives |
| `infrastructure` | `domain`, SDKs, drivers, LlamaIndex | `application`, `interfaces` | `PgVectorStore`, `AnthropicChatProvider`, `OpenAIEmbeddingProvider`, `OdooHttpGateway`, retry/metrics decorators, and `infrastructure/llamaindex/` — the **only** package permitted to import `llama_index` ([ADR-0003](../adr/0003-rag-framework-selection.md)) |
| `interfaces` | `application`, `domain`, `config` | `infrastructure` directly | FastAPI routers, request/response schemas, CLI commands |
| `config` | everything | — | Typed settings and the **composition root** — the one place adapters are wired to ports |

The composition root is deliberate: it is the *only* module allowed to know which
concrete adapter satisfies which port. Everything else receives its collaborators
by injection. That is what makes the fakes in M3 possible and the unit tests fast.

## 5. How this maps to SOLID

Each principle is doing a specific job here.

- **Single Responsibility.** A `Chunker` chunks. A `Retriever` retrieves. An
  `AnswerQuestion` use case orchestrates them and does neither. When "add citation
  formatting" arrives, exactly one class changes.
- **Open/Closed.** Adding Voyage embeddings, a Gemini chat provider, or a Confluence
  loader means adding a class in `infrastructure` and one line in the composition
  root. No existing file is edited.
- **Liskov Substitution.** Every adapter for a port passes the same
  **contract test suite** (M3). `FakeChatProvider` and `AnthropicChatProvider` are
  interchangeable by construction, which is what lets the whole test suite run
  offline.
- **Interface Segregation.** `ChatProvider` and `EmbeddingProvider` are separate
  ports precisely because Anthropic implements one and not the other
  ([ADR-0005](../adr/0005-model-provider-strategy.md)). A single fat `AIProvider`
  would force every adapter to raise `NotImplementedError` — the classic smell.
- **Dependency Inversion.** Drawn in the container diagram above.

## 6. Cross-cutting concerns

Each is implemented **once**, at a boundary, never sprinkled through use cases.

| Concern | Where it lives | Milestone |
| --- | --- | --- |
| Retry, backoff, timeout, circuit breaking | Decorator over any provider port | M3 |
| Token & cost accounting | Same decorator stack | M3 |
| Structured logging with correlation id | Middleware + `contextvars` | M2 |
| Tracing (OpenTelemetry) and metrics | Middleware + port decorators | M12 |
| Authorization post-filter | A pipeline stage in `application`, not optional | M6 |
| Audit logging | Odoo-side model, written by the callback controller | M6 |
| Rate limiting per user | FastAPI dependency | M13 |
| PII redaction | A `Redactor` applied before context enters a prompt | M13 |

## 7. Known limits at 1.0

Stated up front, because a README that claims no limits is not credible.

- **Corpus scale.** Designed and benchmarked for ~10⁵–10⁶ chunks on a single
  PostgreSQL instance. Beyond that, partition `chunks` by company or move dense
  search to a dedicated engine.
- **Read-only.** The assistant answers; it does not create or modify Odoo records.
  Write tools with human-in-the-loop confirmation are post-1.0.
- **Language.** Ingestion and prompts are tuned for English. Multilingual embeddings
  are a config change (`bge-m3`, `voyage-multilingual`); prompt localisation is not
  done.
- **Index sensitivity.** `atlas.chunks` contains ERP content and must be protected
  like the Odoo database itself. Authorization protects the *assistant*, not the
  database ([ADR-0006](../adr/0006-data-access-and-authorization.md), Consequences).
- **Freshness.** Semantic answers reflect the last incremental sync (default: 15
  minutes). Structured answers via tool calling are always live.
