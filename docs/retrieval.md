# Retrieval

How a question becomes the context an answer is grounded on.

Three stages, and the order of them is the security model:

```
                    ┌──────────────────────────────┐
   question ───────▶│ 1. RETRIEVE                  │  dense + lexical, fused,
                    │    over-fetch k × 4          │  then diversified
                    └──────────────┬───────────────┘
                                   │  CandidateChunk[]  ← unauthorized
                    ┌──────────────▼───────────────┐
                    │ 2. AUTHORIZE                 │  ask Odoo, as this user
                    │    ADR-0006. Not optional.   │
                    └──────────────┬───────────────┘
                                   │  AuthorizedChunk[]
                    ┌──────────────▼───────────────┐
                    │ 3. ASSEMBLE                  │  token budget, citations
                    └──────────────┬───────────────┘
                                   ▼
                            PromptContext
```

There is no path from the index to a prompt that skips the middle stage — not
by convention, but because `PromptContext` can only be assembled from
`AuthorizedChunk`, and the only thing that produces one is the authorization
filter. Handing the assembler a candidate is a `mypy --strict` error, and
`test_authorization_is_structural.py` runs the type checker to prove it.

## Why hybrid

Two searches run over the same chunks and fail in opposite directions.

**Dense search** embeds the question and finds neighbours. It answers "which
customers are slow to pay" from a chunk that never uses those words.

**Lexical search** is PostgreSQL full-text search over the generated
`content_tsv`. It finds `S00035` — an identifier no embedding model has a
useful opinion about, and which dense search ranks no better than chance.

Their scores are not comparable. Cosine similarity and `ts_rank_cd` do not share
a scale, and normalising them would mean inventing one. **Reciprocal rank
fusion** sidesteps that by using only the positions:

```
score(chunk) = Σ  1 / (60 + rank in that list)
              lists
```

A chunk found by both lists beats one found by either. That is the whole
mechanism, and it is why a question can be phrased as an identifier or as a
sentence without anybody deciding in advance which it was.

The fusion implementation is LlamaIndex's `QueryFusionRetriever`, per
[ADR-0003](adr/0003-rag-framework-selection.md): algorithms come from the
library, storage and transport stay ours.

## Diversity

Rank by relevance alone over an ERP corpus and the top eight results are
frequently the same fact eight times — shared order headers, a product
description copied across every variant, the same delivery paragraph on fifty
quotations. That wastes a context window that could have held eight different
facts.

Maximal marginal relevance picks greedily, trading relevance against repetition:

```
score = lambda * relevance - (1 - lambda) * max similarity to anything chosen
```

`ATLAS_RETRIEVAL__MMR_LAMBDA` defaults to `0.7`, which keeps relevance firmly in
charge while breaking up runs of near-duplicates. `1.0` disables it.

**Similarity is over words, not vectors.** The textbook formulation uses the
embedding space, which would mean carrying a 1536-float vector back from the
database for every candidate — real bandwidth on the hot path — and would say
nothing about the lexical half of the result set, which has no vector at all.
Token overlap costs nothing and detects exactly the failure being fixed: two
chunks that are largely the same words. Whether embedding-space MMR earns its
bandwidth is an M13 question; the function's signature does not change if the
answer is yes.

## Over-fetching

Authorization discards an unknown fraction of what retrieval finds, and the
denial rate is not knowable in advance. So retrieval asks for
`limit × over_fetch` candidates and lets the filter trim
([ADR-0006](adr/0006-data-access-and-authorization.md)).

The surplus is for the filter's benefit, not the prompt's: what survives is cut
back to `limit` before the token budget gets involved. `denied` is reported on
every result — not as an error count, but because watching it is how M13 decides
whether four is the right multiplier.

## Reranking

There is a `Reranker` port and the default does nothing.

A cross-encoder reads the query and a chunk together and is markedly better at
ordering the top few than a bi-encoder, which scores them independently. The
usual implementation pulls in `sentence-transformers` and `torch`: roughly
2.5 GB in the image, a much larger CVE surface for M14's scans, and per-query
latency nobody has measured.

ADR-0003's dependency policy is to add an integration package when a milestone
needs one. The milestone that needs this is M13, where there will be a golden
set (M12) to say whether reranking improves anything and a latency budget to say
what it costs. Until then the seam exists, the port is fixed, and the default is
honest about doing nothing.

## Assembly and citations

Chunks arrive ranked, so the budget is filled greedily from the front. A chunk
that does not fit is **skipped, not truncated** — half a sales order is a good
way to make a model state half a fact with confidence — and a smaller one
further down can still fit.

Each block is numbered so an answer can refer to it:

```
[1] S00035
Sales Order: S00035
Customer: Deco Addict
Total: 4,500.00

[2] Deco Addict
Contact: Deco Addict
City: Brussels
```

**Citations are built from the assembled context, never by the model**, so a
citation cannot be hallucinated: it names something that was demonstrably in
front of the model. One citation per *record* rather than per chunk — three
chunks of the same order are one thing to go and look at.

Token counts are estimated at three characters per token. Deliberately
pessimistic: overflowing a context window truncates an answer mid-sentence,
while under-filling one merely leaves room unused.

## Where LlamaIndex is, and what stops it spreading

`atlas.infrastructure.llamaindex`, and nowhere else. Three bridges make the
inversion in ADR-0003 §3 real:

| Bridge | Wraps | So that |
| --- | --- | --- |
| `AtlasLlamaVectorStore` | our `VectorStore` | LlamaIndex queries our schema, and never writes to it |
| `AtlasLlamaEmbedding` | our `EmbeddingProvider` | one embedding model, one retry policy, one cost meter |
| `AtlasLlamaLLM` | our `ChatProvider` | LlamaIndex cannot reach a vendor of its own |

The last one is not decoration. Constructing a `QueryFusionRetriever` without an
explicit LLM makes LlamaIndex resolve `Settings.llm`, which tries to import its
OpenAI integration — the exact second-vendor-path failure the ADR inverts the
dependency to prevent. Retrieval asks no language model anything, so the bridge
it gets refuses every call, which makes that a guarantee rather than a comment.

Both store bridges are **read-only**: `add` and `delete` raise. Ingestion owns
writes, so there is one schema and one migration history.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_RETRIEVAL__LIMIT` | `8` | Chunks offered to the prompt, after authorization |
| `ATLAS_RETRIEVAL__OVER_FETCH` | `4` | Candidates fetched per result wanted |
| `ATLAS_RETRIEVAL__MMR_LAMBDA` | `0.7` | Relevance weight in the diversity pass |
| `ATLAS_RETRIEVAL__TOKEN_BUDGET` | `4000` | Tokens of context one answer may use |

## Known limits

- **Retrieval quality is unmeasured.** The tests prove the mechanisms work —
  a lexical-only hit is ranked first, a semantic question survives having no
  words in common — but "hybrid beats dense-only" as a *number* needs M12's
  golden set. Nothing here should be read as a quality claim.
- **No reranking ships.** See above.
- **MMR similarity is lexical**, not embedding-space.
- **Chunks with no Odoo record behind them are dropped** by the authorization
  filter until the visibility-and-group check for uploads exists.
- **Latency is unmeasured.** The figures in
  [03-request-lifecycle.md](architecture/03-request-lifecycle.md) remain
  estimates until M13.
