# ADR-0004: pgvector in a dedicated database, HNSW + GIN hybrid indexes

- **Status:** Accepted; the persistence toolkit is superseded by
  [ADR-0008](0008-hand-written-migrations-and-explicit-sql.md)
- **Date:** 2026-08-02
- **Deciders:** Core team

> The storage decisions here — pgvector, a separate `atlas` database, HNSW plus
> GIN, the index set — all stand. Only the "SQLAlchemy 2.0 Core" part of the
> toolkit was reversed: migrations are hand-written Alembic revisions and runtime
> queries are explicit SQL over psycopg. See ADR-0008.

## Context

Atlas must store embeddings for tens to hundreds of thousands of chunks derived
from Odoo records and uploaded documents, and search them with sub-200 ms latency
while filtering by company and by document class. Three sub-decisions are tangled
together and each is expensive to reverse:

1. **Which vector engine** — a dedicated vector database, or PostgreSQL?
2. **Where it lives** — inside Odoo's database, or its own?
3. **Which index** — IVFFlat or HNSW, and how do we handle filtered search?

Constraints worth stating up front: the deployment target is a self-hosted Odoo CE
instance that already runs PostgreSQL. Every additional stateful service is an
additional backup policy, upgrade path, and on-call surface for whoever deploys
this.

## Decision

### 1. PostgreSQL + `pgvector`

We will use **`pgvector`** in the PostgreSQL cluster Odoo already requires. No
separate vector database.

The decisive argument is **operational**, not benchmark-driven: at our corpus size
a dedicated vector DB buys latency we do not need, and costs a second stateful
system in a deployment whose whole value proposition is "drop it next to your
existing Odoo". A secondary but real benefit is transactional consistency — chunk
rows, their metadata, and the ingestion job that produced them commit or roll back
together, which makes idempotent re-ingestion (M7) straightforward.

### 2. A dedicated `atlas` database in the same cluster

Vectors live in a **separate logical database** (`atlas`) in the **same PostgreSQL
cluster** as Odoo's database.

| Option | Verdict |
| --- | --- |
| Tables inside Odoo's DB, managed by the Odoo ORM | **Rejected.** The ORM has no `vector` column type; we would be fighting it with raw DDL in `init` hooks, and Odoo's `-u` module upgrade path would have opinions about tables it half-manages. It also couples the AI schema to Odoo's migration cycle. |
| Tables inside Odoo's DB, in an `atlas` schema, managed by Alembic | Workable, and cheaper on connections. Rejected because it puts non-Odoo migrations inside the database Odoo's own upgrade scripts operate on, and it muddies backup/restore ("can I restore Odoo without the vectors?" — with one DB, no). |
| **A separate `atlas` database, same cluster** | **Chosen.** Clean ownership boundary, independent migration history, independent backup and retention policy (embeddings are *derived* data — they can be rebuilt, Odoo's data cannot), one cluster to operate. |
| A separate cluster | Rejected as premature. It is a config change later if the workload warrants it. |

We give up cross-database joins. We never needed them: authorization goes through
Odoo's API rather than SQL ([ADR-0006](0006-data-access-and-authorization.md)), and
chunk rows carry `res_model` / `res_id` as a soft reference.

### 3. HNSW for dense search, GIN for lexical, fused at query time

- **Dense:** `hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)`
- **Lexical:** a generated `tsvector` column with `gin` — full-text search over the
  same chunk text
- **Fusion:** Reciprocal Rank Fusion over the two result lists (implemented in M8)

**HNSW over IVFFlat.** IVFFlat requires a populated table before you can build a
useful index (its lists are learned from the data), which is hostile to a system
whose corpus grows continuously via incremental sync — you would rebuild
periodically to keep recall. HNSW is built incrementally, has better
recall-at-latency, and needs no training step. The costs we accept: slower index
build, and a larger memory footprint. Both are acceptable at our scale, and build
time is a background-worker concern, not a request-path one.

**Cosine distance** because both candidate embedding families (OpenAI
`text-embedding-3-*`, Voyage) are trained for cosine similarity and are
L2-normalised, making cosine and inner product equivalent; cosine is the
conventional, least-surprising choice.

**Filtered search.** Every query filters by `company_id` and often by
`res_model`/`visibility`. Naive `WHERE ... ORDER BY embedding <=> $1 LIMIT k` against
an HNSW index degrades recall, because the index walk may exhaust its candidate
list on rows the filter rejects. Our mitigations, in order of preference:

1. **`hnsw.iterative_scan = relaxed_order`** (pgvector ≥ 0.8) — the index keeps
   scanning until enough rows survive the filter. This is the default strategy and
   the reason we require pgvector 0.8+.
2. **Partial indexes per company** when a deployment is genuinely multi-company and
   one company dominates the corpus.
3. **Over-fetch and post-filter** (`LIMIT k * 4`, filter in SQL) as a fallback for
   very selective filters.

M13 benchmarks these against a seeded corpus rather than trusting the reasoning
here.

**Supporting indexes** (detailed in [`docs/architecture/02-data-architecture.md`](../architecture/02-data-architecture.md)):

| Index | Purpose |
| --- | --- |
| `btree (source_hash)` unique on `documents` | Idempotent ingestion — re-ingesting unchanged content is a no-op |
| `btree (res_model, res_id)` on `chunks` | Delete-and-replace on record update; citation lookups |
| `btree (company_id, visibility)` on `chunks` | Pre-filter selectivity |
| `btree (status, run_after)` on `ingest_jobs` | `FOR UPDATE SKIP LOCKED` queue polling |

## Consequences

### Benefits

- One stateful service for the whole product. `docker compose up` gives a reviewer a
  working system.
- Chunks, metadata, and job state are transactionally consistent.
- Embeddings are rebuildable derived data, isolated in their own database with their
  own backup policy.
- SQL is the query language, so retrieval is debuggable with `psql` and `EXPLAIN
  ANALYZE` — which is exactly what M13's performance work needs.

### Costs

- **We must run a pgvector-enabled PostgreSQL image.** The stock `postgres` image
  does not include the extension. Handled in M1 with `pgvector/pgvector:pg17`.
- **Filtered ANN needs care.** Addressed above; verified with numbers in M13.
- **HNSW index builds are slow and memory-hungry** on large batches. Mitigated by
  building the index once and inserting incrementally, and by `maintenance_work_mem`
  tuning documented in the deployment guide.
- **No cross-database joins to Odoo tables.** By design; see ADR-0006.
- Scaling past a few million chunks would need partitioning or a move to a dedicated
  engine. Recorded as a known limit in the README rather than pre-solved.

## Alternatives considered

**Qdrant.** The strongest dedicated option: excellent filtered-search support
(payload indexes make our `company_id` filter a first-class concern rather than a
workaround), fast, and easy to run in Docker. Rejected because it adds a second
stateful service purely to solve a problem we do not have at this scale, and it
splits the transactional boundary — a chunk row and its vector would commit
separately, so crash recovery has to reconcile them.

**Weaviate / Milvus.** Rejected: heavier operationally than Qdrant with no offsetting
benefit for a single-tenant, on-prem ERP deployment.

**Pinecone or any hosted vector service.** Rejected outright. Sending ERP data —
customers, pricing, invoices — to a third-party index is a non-starter for the
target user, and it makes local development require network and an account.

**SQLite + `sqlite-vec` for local dev, pgvector in production.** Rejected: two storage
implementations means the tests that pass locally exercise different SQL from
production. We use throwaway PostgreSQL containers in tests instead (M4).

**FAISS in-process.** Rejected: no persistence story, no filtering, no concurrency
across our API and worker processes.
