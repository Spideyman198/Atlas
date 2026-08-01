# ADR-0005: Provider-agnostic model layer with split chat/embedding vendors

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team

## Context

Atlas needs two distinct model capabilities, and they are *not* the same decision:

- **Chat completion with tool calling** — synthesises answers, drives the structured
  query tools in M9. Quality-sensitive, latency-sensitive, and the dominant cost.
- **Text embedding** — turns chunks and queries into vectors. Throughput-sensitive,
  cheap per call, and **schema-coupled**: the vector dimension is baked into a
  PostgreSQL column type, so changing the model means a migration and a full
  re-index of the corpus.

Three constraints shape the design:

1. **Deployments differ.** One company mandates Azure OpenAI for compliance; another
   is an Anthropic shop; a third is air-gapped and can use no hosted API at all.
   Hard-coding a vendor makes the product undeployable for two of the three.
2. **Anthropic does not offer an embedding API.** A "Claude-powered" assistant still
   needs embeddings from somewhere. This is a fact of the ecosystem that forces the
   two capabilities apart.
3. **Cost must be observable.** M12 reports per-conversation cost; that requires
   token accounting at the provider boundary, uniformly across vendors.

## Decision

### Two ports, not one

`atlas.domain.ports` defines two independent protocols:

```python
class ChatProvider(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResponse: ...
    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]: ...


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[Vector]: ...
```

They are configured separately. **Anthropic Claude for chat + OpenAI or Voyage for
embeddings is a first-class, documented configuration**, not a workaround.

### Normalised at the boundary

Adapters translate vendor payloads into our own `ChatRequest`/`ChatResponse` domain
types — including tool definitions, tool results, stop reasons, and a uniform
`TokenUsage` (prompt / completion / cached) with a cost estimate from a pricing
table. Everything above `infrastructure` speaks only our types. Vendor-specific
concepts that do not generalise (Anthropic's system-prompt-as-parameter vs OpenAI's
system-message-in-the-array, differing tool-call schemas) are absorbed by the
adapter and never leak upward.

### Cross-cutting concerns in a decorator, not in each adapter

Retry with exponential backoff and jitter, timeouts, circuit breaking, rate-limit
handling, redaction, and usage logging are implemented **once** as decorators
wrapping any `ChatProvider`. Adapters stay small and boring — the only way to keep
"add a provider" a genuinely cheap operation.

### Planned adapters

| Capability | Adapter | Status |
| --- | --- | --- |
| Chat | Anthropic (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`) | M3 |
| Chat | OpenAI (+ Azure OpenAI via base-URL override) | M3 |
| Chat | `FakeChatProvider` — scripted, deterministic | M3, tests only |
| Embedding | OpenAI (`text-embedding-3-small`, 1536-d) | M3 |
| Embedding | Voyage (`voyage-3`-class) | M3 |
| Embedding | `HashEmbeddingProvider` — deterministic, no network | M3, tests only |
| Embedding | Local sentence-transformers (`bge-m3`) | Post-1.0, air-gapped deployments |

### Default embedding model: `text-embedding-3-small` (1536 dimensions)

Justified on four axes:

- **Cost/throughput.** Ingesting a full ERP corpus is embedding-dominated. The
  `-small` tier is roughly an order of magnitude cheaper than `-large` per token,
  and ingestion cost is what makes or breaks adoption.
- **Storage and index size.** 1536 × 4 bytes = 6 KB per chunk before index overhead.
  `-large` at 3072 doubles both the table and the HNSW graph, for a retrieval-quality
  gain that is real but small on short, factual ERP chunks.
- **Quality at our chunk size.** Our chunks are 200–800 tokens of semi-structured
  record text. Retrieval quality on that shape is dominated by chunking and hybrid
  fusion, not by the last few points of MTEB score.
- **Reversibility.** `text-embedding-3-*` supports dimension truncation, and our
  schema stores the model id and dimension per document, so a deployment can opt up
  without code changes.

**Voyage as the documented alternative** for quality-first deployments — it is
Anthropic's recommended embedding partner, so a Claude-only shop can source both
capabilities from a coherent vendor pair.

### The dimension is a migration, and we treat it as one

The `chunks.embedding` column is `vector(N)`. Changing embedding model changes `N`.
Therefore:

- `documents` records the `embedding_model` and `embedding_dimensions` used.
- Changing the configured model is refused at startup with an explicit error unless
  a re-index has been run.
- M7 ships a `reindex` command that re-embeds the corpus into a new column and swaps
  it. This is the single most under-appreciated operational hazard in RAG systems,
  so we design for it rather than discovering it.

## Consequences

**Easier**

- Deployments choose their vendor without a fork. Air-gapped installs get a path.
- Adding a provider is one class plus a contract-test registration.
- Unit tests run with fakes: no network, no API key, no cost, deterministic output —
  which is what makes the M12 evaluation harness reproducible.
- Cost and token accounting are uniform and free at the call site.

**Harder**

- **We maintain a translation layer**, and vendor features that do not generalise
  (extended thinking, prompt caching, structured outputs) need deliberate exposure
  through the port rather than pass-through. Handled case by case; capability flags
  on the provider let the application degrade gracefully.
- **The lowest common denominator risk** — the port could flatten providers into
  mediocrity. Mitigated by designing the port from *our* use cases (M9/M10) rather
  than from the intersection of two SDKs.
- **Two SDK dependencies** to keep current.
- Contract tests must run against real APIs periodically to catch vendor drift.
  Scheduled as a nightly, key-gated CI job in M14 — never on PRs.

## Alternatives considered

**Use one vendor's SDK directly, everywhere.** Simplest and fastest to build.
Rejected: it makes the product undeployable in the compliance scenarios above, and
it fails the brief's explicit requirement for OpenAI *and* Claude support.

**LiteLLM as the abstraction layer.** Genuinely tempting — it already normalises
~100 providers with a unified interface and cost tracking, which is most of what our
port does. Rejected for the core because it is a broad runtime dependency solving a
two-provider problem, its normalisation is OpenAI-shaped (tool-calling and
system-prompt semantics get coerced), and our decorator stack plus cost table is
~200 lines we fully control. It remains a legitimate future adapter *behind* the
port for deployments that want exotic providers.

**LangChain's chat model abstraction.** Rejected together with the framework itself;
see [ADR-0003](0003-rag-framework-selection.md).

**`text-embedding-3-large` (3072-d) as default.** Rejected as default, supported as
config: doubles storage, index memory, and embedding cost for a modest gain on our
chunk profile. Deployments that measure a retrieval-quality shortfall in M12's
harness can switch and re-index.

**Local embeddings (`bge-m3`) as default.** Rejected for 1.0: it drags PyTorch into
the service image (multi-GB), needs a GPU for acceptable ingestion throughput, and
makes the quickstart heavy. Correct for air-gapped deployments, so it stays on the
roadmap behind the existing port.
