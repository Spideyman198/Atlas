# Data Architecture

> Design intent for M4 (Atlas schema) and M5 (Odoo models). DDL shown here is the
> target; the authoritative definitions are the Alembic migrations and the Odoo
> model classes once those milestones land.

## 1. Two databases, one cluster

```mermaid
flowchart LR
    subgraph cluster["PostgreSQL 17 cluster"]
        direction LR
        subgraph odoo_db["db: odoo — owned by the Odoo ORM"]
            o1["res_partner, product_template,<br/>sale_order, account_move, …"]
            o2["atlas_conversation<br/>atlas_message<br/>atlas_message_citation<br/>atlas_ingest_source<br/>atlas_access_log"]
        end
        subgraph atlas_db["db: atlas — owned by Alembic"]
            a1["documents<br/>chunks<br/>ingest_jobs<br/>embedding_cache"]
            ext(["EXTENSION vector 0.8"])
        end
    end
```

**Why the split** is argued in [ADR-0004](../adr/0004-vector-store-and-index-strategy.md).
The short version: embeddings are *derived* data with a different lifecycle, a
different owner, and a different backup policy from the ERP's system of record.

**What goes where, and the rule that decides it:**

> If a user needs to *see* it in Odoo — with a view, a menu, an access right, a
> record rule, or a chatter — it belongs in the **odoo** database as an ORM model.
> If it is machine-generated derived data serving the retrieval pipeline, it belongs
> in **atlas**.

That is why conversations live in Odoo (they need views, security groups, and record
rules so a user sees only their own threads) while chunks live in Atlas (nobody
browses a chunk table).

**Cost of the split:** no foreign keys across the boundary. `chunks.res_model` /
`chunks.res_id` is a *soft* reference, exactly like Odoo's own `ir.attachment`.
Referential integrity is maintained by the ingestion pipeline's delete-and-replace
semantics, and dangling chunks are harmless because stage-2 authorization
([ADR-0006](../adr/0006-data-access-and-authorization.md)) drops any chunk whose
record no longer resolves.

## 2. Entity–relationship model

### 2.1 Atlas database

```mermaid
erDiagram
    DOCUMENTS ||--o{ CHUNKS : "produces"
    INGEST_SOURCES ||--o{ DOCUMENTS : "yields"
    INGEST_JOBS }o--|| INGEST_SOURCES : "processes"

    INGEST_SOURCES {
        bigint   id PK
        text     key           "e.g. odoo.sale.order"
        text     kind          "odoo_model | attachment | upload"
        jsonb    config        "domain, fields, template"
        timestamptz watermark  "last synced write_date"
        boolean  active
    }

    DOCUMENTS {
        bigint   id PK
        bigint   source_id FK
        text     res_model     "soft ref to Odoo model"
        bigint   res_id        "soft ref to Odoo record id"
        text     external_ref  "human key, e.g. SO00035"
        text     title
        text     source_hash   "sha256 of normalised content"
        int      company_id    "soft ref, pre-filter only"
        smallint visibility    "0 public 1 internal 2 restricted"
        text     embedding_model
        int      embedding_dimensions
        jsonb    metadata
        timestamptz record_write_date
        timestamptz indexed_at
    }

    CHUNKS {
        bigint   id PK
        bigint   document_id FK
        int      ordinal       "position within document"
        text     content
        int      token_count
        vector   embedding     "vector(1536)"
        tsvector content_tsv   "GENERATED, for lexical search"
        text     res_model     "denormalised from document"
        bigint   res_id        "denormalised from document"
        int      company_id    "denormalised from document"
        smallint visibility    "denormalised from document"
        jsonb    metadata
    }

    INGEST_JOBS {
        bigint   id PK
        bigint   source_id FK
        text     kind          "full_sync | incremental | single | reindex"
        text     status        "pending running succeeded failed"
        jsonb    payload
        timestamptz run_after
        int      attempts
        text     last_error
        timestamptz locked_at
        text     locked_by     "worker identity"
    }

    EMBEDDING_CACHE {
        text     content_hash PK
        text     model PK
        vector   embedding
        timestamptz created_at
    }
```

Notes on the design:

- **`documents` vs `chunks` is a real distinction, not bookkeeping.** A document is
  the unit of *ingestion and invalidation* (one sales order, one PDF). A chunk is the
  unit of *retrieval*. Re-ingesting a document deletes and replaces its chunks
  atomically, which is what makes incremental sync correct.
- **Denormalising `company_id`, `visibility`, `res_model`, `res_id` onto `chunks`**
  is deliberate. The retrieval pre-filter must be evaluable in a single index scan
  on `chunks`; a join to `documents` would defeat the HNSW filtered-scan path. The
  cost is a write-time consistency obligation, satisfied because chunks are only
  ever written by the ingestion pipeline as part of a document replacement.
- **`source_hash`** is a SHA-256 of the normalised rendered content. If it is
  unchanged, ingestion skips the document entirely — no embedding call, no cost.
  This is what makes a 15-minute cron cheap.
- **`embedding_cache`** keyed by `(content_hash, model)` prevents re-paying for
  identical text across documents (boilerplate terms, repeated product descriptions)
  and makes re-index runs after a crash nearly free.
- **`embedding_model` / `embedding_dimensions` on `documents`** exist so the system
  can *detect* a model change rather than silently mixing incompatible vector spaces
  — the failure mode described in [ADR-0005](../adr/0005-model-provider-strategy.md).
- **`content_tsv` is a generated column**, so lexical and dense search always describe
  the same text. It cannot drift.
- **`visibility` as a smallint tier**, not a boolean: `0 = public` (product
  descriptions, published docs), `1 = internal` (most ERP records), `2 = restricted`
  (HR, finance). It is a coarse pre-filter, never the authority.

### 2.2 Odoo database — addon models

```mermaid
erDiagram
    RES_USERS ||--o{ ATLAS_CONVERSATION : "owns"
    ATLAS_CONVERSATION ||--o{ ATLAS_MESSAGE : "contains"
    ATLAS_MESSAGE ||--o{ ATLAS_MESSAGE_CITATION : "cites"
    RES_COMPANY ||--o{ ATLAS_CONVERSATION : "scopes"

    ATLAS_CONVERSATION {
        int      id PK
        char     name          "auto-titled from first message"
        int      user_id FK
        int      company_id FK
        selection state        "draft active archived"
        int      message_count "computed, stored"
        float    total_cost    "computed from messages"
        datetime last_activity
    }

    ATLAS_MESSAGE {
        int      id PK
        int      conversation_id FK
        selection role          "user assistant system tool"
        text     content
        jsonb    tool_calls
        int      prompt_tokens
        int      completion_tokens
        float    cost
        int      latency_ms
        char     model_used
        char     trace_id       "correlates to engine logs"
        selection status        "pending streaming done error"
    }

    ATLAS_MESSAGE_CITATION {
        int      id PK
        int      message_id FK
        char     res_model
        int      res_id
        char     display_name
        char     snippet
        float    score
        int      sequence
    }

    ATLAS_ACCESS_LOG {
        int      id PK
        int      user_id FK
        char     trace_id
        char     res_model
        int      requested_count
        int      granted_count
        text     denied_ids
        datetime create_date
    }
```

**Why citations are a model, not a JSON blob on the message.** They need to be
clickable — a citation resolves to `res_model` + `res_id`, and the UI renders a link
that opens the actual Odoo record. Making them rows gives us `search`, `read_group`
("which records are cited most?"), and a clean many2one-style resolution without
parsing JSON in the view layer. It also lets M12 measure citation coverage with SQL.

**Why `atlas_access_log` is separate from `atlas_message`.** One message may trigger
several authorization checks across several models, and the log must survive message
deletion for audit purposes.

## 3. Indexing strategy

```sql
-- Dense retrieval. HNSW: incremental builds, no training step, best recall/latency.
-- See ADR-0004 for why not IVFFlat.
CREATE INDEX chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Lexical retrieval, fused with dense results via RRF at query time (M8).
CREATE INDEX chunks_tsv_gin_idx ON chunks USING gin (content_tsv);

-- Pre-filter selectivity. Column order matters: company_id is the high-cardinality
-- discriminator in multi-company deployments and is always present in the predicate.
CREATE INDEX chunks_scope_idx ON chunks (company_id, visibility);

-- Delete-and-replace on record update, and citation resolution.
CREATE INDEX chunks_record_idx ON chunks (res_model, res_id);

-- Idempotent ingestion: unchanged content is skipped before any API call.
CREATE UNIQUE INDEX documents_source_hash_key ON documents (source_hash);

-- Incremental sync watermark scans.
CREATE INDEX documents_source_written_idx ON documents (source_id, record_write_date);

-- Queue polling with SELECT ... FOR UPDATE SKIP LOCKED (M7).
CREATE INDEX ingest_jobs_claim_idx ON ingest_jobs (status, run_after)
    WHERE status IN ('pending', 'running');
```

### Query-time parameters

| Parameter | Value | Why |
| --- | --- | --- |
| `hnsw.ef_search` | 40–100, tuned in M13 | Recall/latency dial. Higher = better recall, more work. |
| `hnsw.iterative_scan` | `relaxed_order` | Keeps scanning until enough rows survive the `WHERE` filter. The fix for filtered-ANN recall collapse. |
| `maintenance_work_mem` | ≥ 1 GB during index build | HNSW builds in memory or spills catastrophically. |
| Over-fetch factor | `k * 4` | Headroom for the authorization post-filter's denial rate. |

## 4. Performance design

Ordered by impact, which is roughly the reverse of the order people usually attack.

1. **Don't embed what hasn't changed.** `source_hash` + `embedding_cache` turn a
   full re-sync into a cheap no-op. This dominates ingestion cost.
2. **Batch embeddings.** One API call per ~96 chunks rather than per chunk. Cuts
   ingestion wall-clock by roughly an order of magnitude.
3. **`COPY` for bulk chunk inserts** during initial ingestion; `INSERT` for
   incremental updates.
4. **Build the HNSW index once, then insert incrementally.** Never drop and rebuild
   during normal operation.
5. **Batch the authorization post-filter** — one `search` per *model*, not per
   record id. Turns 40 round-trips into 3.
6. **Pool connections** (`psycopg_pool`) in the engine; Odoo manages its own pool.
7. **Cap the context budget in tokens, not chunk count.** Prompt cost is the largest
   per-query expense.
8. **`EXPLAIN (ANALYZE, BUFFERS)` in the benchmark suite (M13)**, so performance
   claims in the README come with numbers.

## 5. Migration policy

- **Atlas DB:** Alembic. Every schema change is a reviewed migration with a working
  `downgrade`. Migrations run on container start via an init job, never implicitly
  from application code.
- **Odoo DB:** the standard Odoo module upgrade path (`-u odoo_atlas`), with
  `migrations/<version>/` scripts when a data migration is required.
- **Embedding dimension changes** are a re-index, not a migration. M7 ships an
  explicit `atlas reindex` command; the service refuses to start if the configured
  model disagrees with the corpus.
- **Backups:** the `odoo` database is the system of record and must be backed up.
  The `atlas` database is derived and *may* be backed up for restore speed — it can
  always be rebuilt from Odoo plus the source documents.
