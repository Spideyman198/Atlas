# ADR-0003: Use LlamaIndex as an infrastructure adapter behind framework-agnostic ports

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team

Revision note: the first draft of this ADR proposed owning the retrieval
orchestration outright, with no general-purpose framework in the codebase. It was
rejected at M0 review. This document records the decision that was accepted; the
original proposal is kept below under "Alternatives considered", because one of its
objections shaped the design that replaced it (see "The inversion").

Per [ADR-0001](0001-record-architecture-decisions.md), a rejected proposal is
amended and an accepted decision is superseded. From M1 onward, changes to accepted
ADRs get a new numbered record.

## Context

Atlas needs the standard RAG toolkit: document loading, chunking, embedding,
vector search, hybrid retrieval with rank fusion, reranking, and prompt assembly.
Two forces pull in opposite directions.

**Toward a framework.** These are solved problems with mature implementations.
LlamaIndex ships node parsers, a reader ecosystem, an ingestion pipeline with
content-hash deduplication, `QueryFusionRetriever` (reciprocal rank fusion built
in), auto-merging and sentence-window retrieval, and reranker integrations. Writing
all of that ourselves is real work with real bugs, and the result would be a private
reimplementation that no new contributor recognises.

**Away from a framework.** Atlas has one requirement that no RAG framework
accommodates: **per-request authorization** ([ADR-0006](0006-data-access-and-authorization.md)).
Every retrieved chunk must be re-checked against Odoo's record rules, as the asking
user, before it can enter a prompt. In a framework's native design this logic wants
to live *inside* the retriever or query engine, reached by subclassing internals.
We also need to know exactly which chunks entered which prompt at what token cost,
for the M12 evaluation harness and the M13 audit trail.

The resolution is not to pick a side. It is to decide **where the framework is
allowed to live**.

## Decision

We will use **LlamaIndex as one infrastructure-layer implementation of ports the
domain owns.** The framework is a replaceable detail, not a foundation.

### 1. The ports (owned by `atlas.domain.ports`)

Five protocols form the framework boundary. They are defined in terms of *our*
domain types and contain no LlamaIndex vocabulary:

| Port | Responsibility | Returns |
| --- | --- | --- |
| `DocumentLoader` | Turn a source reference into raw documents | `Iterable[RawDocument]` |
| `EmbeddingProvider` | Text → vectors; declares `model_id` and `dimensions` | `list[Vector]` |
| `VectorStore` | Persist and search chunks; metadata pre-filters | `list[CandidateChunk]` |
| `Retriever` | Query → ranked candidates (fusion, MMR, rerank) | `list[CandidateChunk]` |
| `ChatProvider` | Completion and streaming, with tool calling | `ChatResponse` / `AsyncIterator[ChatChunk]` |

`ChatProvider` and `EmbeddingProvider` are separate ports for the reasons in
[ADR-0005](0005-model-provider-strategy.md). Chunking is **not** a port: it is a
retrieval strategy, configured inside the loader/ingestion adapter and exposed
through settings, not a concept the domain reasons about.

### 2. Where LlamaIndex lives

```
services/atlas/src/atlas/
├── domain/ports/            ← protocols. No llama_index. Ever.
├── application/             ← use cases. No llama_index. Ever.
└── infrastructure/
    ├── llamaindex/          ← THE ONLY package permitted to import llama_index
    │   ├── bridges.py       ←   AtlasLlamaLLM, AtlasLlamaEmbedding, AtlasLlamaVectorStore
    │   ├── loaders.py       ←   DocumentLoader impl (readers + node parsers)
    │   └── retriever.py     ←   Retriever impl (QueryFusionRetriever, rerankers)
    ├── persistence/         ←   PgVectorStore — our schema, SQLAlchemy Core
    └── providers/           ←   Anthropic / OpenAI / Voyage SDK adapters
```

This is enforced, not requested. An `import-linter` **forbidden contract** fails the
build if `llama_index` is imported anywhere outside `atlas.infrastructure.llamaindex`
(M2).

### 3. The inversion: LlamaIndex plugs into our infrastructure, not the reverse

The strongest objection to adopting a framework was that it becomes a *second* path
to the model vendors — two retry policies, two cost meters, two sets of telemetry.
That objection is real, and it is answered by inverting the dependency inside the
adapter.

LlamaIndex components need an LLM, an embedding model, and a vector store. We give
them **bridges that delegate back to our own ports**:

```
application ──▶ ChatProvider (port) ──▶ AnthropicChatProvider ──▶ SDK
                       ▲
                       │ delegates to
              AtlasLlamaLLM(CustomLLM)  ──▶ used by LlamaIndex internals
```

- `AtlasLlamaLLM(llama_index.core.llms.CustomLLM)` wraps our `ChatProvider`.
- `AtlasLlamaEmbedding(BaseEmbedding)` wraps our `EmbeddingProvider`.
- `AtlasLlamaVectorStore(BasePydanticVectorStore)` wraps our `PgVectorStore`.

The composition root binds `Settings.llm` and `Settings.embed_model` to these
bridges. Consequences: **exactly one path to every vendor**, so retry, backoff,
circuit breaking, token accounting and cost telemetry stay in one decorator stack
([ADR-0005](0005-model-provider-strategy.md)); and **exactly one database schema**,
so the indexes, ACL pre-filter columns and generated `tsvector` designed in
[ADR-0004](0004-vector-store-and-index-strategy.md) survive intact.

We use LlamaIndex for **algorithms** — parsing, chunking, fusion, reranking,
ingestion orchestration — and never for **transport or storage**.

### 4. Authorization stays outside the framework, structurally

The `Retriever` port returns `CandidateChunk`. The prompt assembler accepts only
`AuthorizedChunk`. The **only** thing in the system that converts one to the other
is `application.AuthorizationFilter`, which calls the `OdooGateway`:

```python
# atlas/application/retrieval_pipeline.py
candidates: list[CandidateChunk] = await self._retriever.retrieve(query)
authorized: list[AuthorizedChunk] = await self._authorization.filter(candidates, actor)
context: PromptContext = self._assembler.assemble(authorized)  # ← won't type-check otherwise
```

Under `mypy --strict`, passing a `CandidateChunk` into the assembler is a **type
error**. Bypassing authorization is not a discipline problem or a review problem —
it does not compile.

This is strictly better than the rejected proposal achieved. Owning the retriever
would have made the filter *conventional*; putting it behind a port made it
*structural*, because the boundary had to be made explicit anyway.

### 5. Dependency policy

- Depend on **`llama-index-core`**, never the `llama-index` meta-package, which
  pulls dozens of integrations we do not use.
- Add narrowly scoped integration packages only when a milestone needs one
  (`llama-index-readers-file` in M7).
- Pin exact versions; upgrades are deliberate, reviewed, and covered by the
  contract test suite.

### 6. The exit test

The abstraction is only real if we can prove it. Two falsifiable checks, both in CI:

1. **`lint-imports` passes** — no `llama_index` symbol outside one package.
2. **The entire unit and contract suite runs green with fakes.** If LlamaIndex were
   deleted, `domain` and `application` tests would still pass; only
   `infrastructure/llamaindex` tests would fail.

If either check stops holding, the framework has escaped containment and the ADR has
been violated.

## Consequences

### Benefits

- **Less code to write and own.** Node parsers, fusion retrieval, reranking and
  ingestion orchestration come from a maintained library instead of ~800–1,200 lines
  of ours. M7 and M8 shrink materially.
- **Better algorithms sooner.** Sentence-window and auto-merging retrieval become
  configuration rather than projects.
- **Ecosystem access through the `DocumentLoader` port** — the reader catalogue
  (Confluence, SharePoint, Drive) is now a dependency addition, not a milestone.
- **Recognisable to reviewers and contributors.** A named, industry-standard
  framework lowers the onboarding cost and is legible on a CV.
- **Upstream improvements arrive for free**, including retrieval-quality work we
  would not have prioritised.

### Costs

- **The bridge tax.** Three adapter classes (`AtlasLlamaLLM`, `AtlasLlamaEmbedding`,
  `AtlasLlamaVectorStore`) totalling roughly 200–300 lines that a
  framework-native implementation would not need. This is the explicit price of
  keeping one vendor path and one schema. We consider it well spent; it is the
  single largest cost of this decision and it should be stated plainly.
- **Version churn.** LlamaIndex moves faster than an ERP support window. Mitigated
  by exact pins, isolation to one package, and contract tests that catch behavioural
  drift on upgrade — but upgrades remain real work.
- **Larger dependency surface.** A bigger image and more CVE noise in M14's
  `pip-audit`/Trivy gates. Mitigated by `llama-index-core` only.
- **Deeper stack traces.** Failures now travel through framework internals.
  Mitigated by our own structured logging and tracing at the port boundary, where
  `trace_id`, latency and token cost are recorded regardless of what happens inside.
- **Two mental models.** Contributors must hold both our domain types and
  LlamaIndex's `Document` / `TextNode` / `NodeWithScore`. Mitigated by confining
  translation to the adapter and documenting the mapping:

  | Atlas domain type | LlamaIndex type | Translated in |
  | --- | --- | --- |
  | `RawDocument` | `Document` | `llamaindex/loaders.py` |
  | `Chunk` | `TextNode` | `llamaindex/bridges.py` |
  | `CandidateChunk` | `NodeWithScore` | `llamaindex/retriever.py` |
  | `ChatRequest` / `ChatResponse` | `ChatMessage` / `CompletionResponse` | `llamaindex/bridges.py` |

- **Leakage.** Importing `NodeWithScore` into a use case is the failure mode that
  undoes this design. `import-linter` fails the build when it happens.
- **Two instrumentation systems.** LlamaIndex has its own callback and
  instrumentation stack. Our port-level decorators are authoritative for metrics,
  cost and tracing; LlamaIndex instrumentation is enabled only for local debugging.

## Alternatives considered

**Own the orchestration entirely; no framework (the original proposal, rejected).**
The argument was that items 1–5 of the RAG toolkit are ~600 lines of well-understood
code; that per-request authorization and full prompt observability fight any
framework's abstractions; and that a smaller dependency tree suits an ERP support
window. Rejected at M0 review: it trades a recognised, maintained implementation for
a private one, delays M7/M8 substantially, and forfeits the reader ecosystem — while
the authorization concern turned out to be better solved by an explicit port
boundary than by ownership (see *The inversion* and §4, which are direct
descendants of this proposal's objections). The reasoning is retained because the
day we need to remove LlamaIndex, this is the design we fall back to, and the ports
make that fallback a contained change.

**LangChain / LangGraph.** Rejected. Its value is breadth of integrations, which we
barely use — two model vendors, one vector store — while its cost is a heavier
abstraction stack (Runnables, LCEL, callback handlers) and historically rapid API
churn. LangGraph is premature: the M10 router is a classifier plus a tool loop, not
a stateful multi-agent graph. LlamaIndex is the better fit for a RAG-first,
retrieval-heavy system.

**Haystack.** Arguably the cleanest pipeline abstraction of the three for production
RAG. Rejected on ecosystem gravity: a smaller community and lower recognition, with
no offsetting technical advantage for our use case.

**LlamaIndex's `PGVectorStore` as the storage layer.** Genuinely tempting — it
supports hybrid search out of the box and would delete our `AtlasLlamaVectorStore`
bridge. Rejected because it owns its own schema (`data_*` tables with a `metadata_`
JSONB column), which would push `company_id`, `visibility`, `res_model` and
`res_id` into JSONB. Our ACL pre-filter and ingestion-idempotency indexes
([ADR-0004](0004-vector-store-and-index-strategy.md)) would degrade into JSONB
expression indexes, and the generated `content_tsv` column would be lost. The
authorization pre-filter is load-bearing; we keep our schema and pay the bridge.

**LlamaIndex's LLM and embedding abstractions as the primary provider layer.**
Rejected: it would duplicate the provider layer in [ADR-0005](0005-model-provider-strategy.md),
producing two retry policies and two cost meters. The bridge inverts this so there
is exactly one.
