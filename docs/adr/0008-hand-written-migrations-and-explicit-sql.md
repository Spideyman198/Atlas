# ADR-0008: Hand-written Alembic migrations and explicit SQL, without SQLAlchemy Core

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team
- **Supersedes:** the persistence-toolkit part of
  [ADR-0004](0004-vector-store-and-index-strategy.md) and the corresponding row in
  [ADR-0003](0003-rag-framework-selection.md). Every other decision in ADR-0004 —
  pgvector, the separate `atlas` database, HNSW plus GIN — stands unchanged.

## Context

ADR-0004 specified "SQLAlchemy 2.0 Core + Alembic" on the reasoning that Alembic
is the standard migration tool, Core keeps SQL explicit, and pgvector ships a
SQLAlchemy integration. Implementing the schema in M4 showed the Core half of that
pairing earning nothing.

Almost every object in the schema is PostgreSQL- or pgvector-specific, and Core
models none of them:

| Schema feature | In SQLAlchemy Core |
| --- | --- |
| `vector(1536)` column | Third-party type, or `op.execute` |
| `USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)` | Not expressible — raw SQL |
| `content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED` | Not expressible — raw SQL |
| Partial index `WHERE status IN ('pending', 'running')` | Expressible, awkwardly |
| `SELECT ... FOR UPDATE SKIP LOCKED` (M7) | Expressible, awkwardly |

A migration written in Core for this schema is a thin wrapper around a sequence of
`op.execute()` calls. The abstraction is paid for and not used.

The second force is the request path. The engine already owns a `psycopg` pool
built in the composition root, and pgvector's psycopg integration registers the
`vector` type on it directly. Introducing SQLAlchemy would mean two connection
abstractions, two pools to size, and two places a query can be written.

The third is where the performance work lands. ADR-0004 commits M13 to
`EXPLAIN (ANALYZE, BUFFERS)` and an HNSW parameter sweep. That work is done
against SQL text. Retrieval SQL we can read directly is the artefact being tuned.

## Decision

- **Migrations are hand-written Alembic revisions** using `op.execute()` with
  explicit DDL. Alembic keeps the version graph, the ordering and the `downgrade`
  path; it does not generate the SQL.
- **Runtime queries are explicit SQL executed through `psycopg`**, using the pool
  the composition root already owns.
- **`pgvector.psycopg` is registered on every pooled connection**, so vectors bind
  as parameters rather than being formatted into query text.
- **SQLAlchemy is not used to define schema or to query.** It is still installed:
  Alembic requires it, and Alembic's own machinery opens the migration connection
  through a SQLAlchemy engine. What we avoid is a second abstraction in our code —
  no Core table metadata, no SQLAlchemy in the request path, no second pool. The
  dependency exists; the coupling does not.

Autogenerate is lost. That is the point of the trade: autogenerate works by
diffing Core metadata against the database, and metadata that cannot express
HNSW, generated columns or operator classes would produce migrations that silently
drop them.

## Consequences

### Benefits

- One database library, one pool, one place a query lives.
- The DDL in a migration is the DDL that runs. Reviewing a migration means reading
  the statement PostgreSQL will execute, not inferring it.
- Index tuning in M13 edits the SQL under test rather than a builder that emits it.
- Smaller dependency tree and image.

### Costs

- **No autogenerate.** Every migration is written by hand, and a schema change that
  the author forgets to write simply does not happen. Mitigated by the M4
  integration tests, which run against a migrated database and fail when a column
  or index is missing.
- **No compile-time checking of SQL.** A typo surfaces at test time rather than at
  type-check time. Mitigated by integration coverage of every query.
- **Table and column names are strings in two places** — the migration and the
  queries. Mitigated by keeping all SQL in one module.
- SQLAlchemy is widely recognised, and its absence is a small unfamiliarity cost
  for contributors. The queries are ordinary SQL, which is more widely recognised
  still.

## Alternatives considered

**Keep SQLAlchemy Core as ADR-0004 specified.** Rejected: for this schema it is a
wrapper around `op.execute`, and it adds a second connection abstraction to a
service that already pools psycopg.

**SQLAlchemy Core for migrations only, psycopg at runtime.** The narrower version
of the same idea, and the one ADR-0004 implicitly described. Rejected because the
migration is exactly where Core's expressiveness runs out — the vector column, the
HNSW index and the generated column all fall back to raw SQL, so Core would be
present to declare the handful of ordinary columns around them.

**Plain numbered SQL files with a small runner.** Genuinely simpler than Alembic
and enough for forward-only deployment. Rejected: Alembic gives a branch-aware
version graph, `downgrade`, and an `alembic current` that reports what a live
database is actually running — worth more than the runner it replaces.

**An ORM (SQLAlchemy ORM, SQLModel, Tortoise).** Rejected outright: the retrieval
path is a hand-tuned query with a vector operator, a rank fusion and a
selectivity-sensitive filter. An ORM is the wrong tool for the one query whose
plan we care about.
