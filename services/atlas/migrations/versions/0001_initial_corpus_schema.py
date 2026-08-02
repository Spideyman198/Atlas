"""Initial corpus schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-02

Implements the design in docs/architecture/02-data-architecture.md. Written as
explicit DDL per ADR-0008 — every interesting object here (the vector column, the
HNSW index, the generated tsvector, the partial index) is PostgreSQL-specific and
would be raw SQL under any query builder anyway.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# The vector width is fixed at migration time because it is part of the column
# type. Changing embedding model is therefore a migration plus a re-index, not a
# configuration change (ADR-0005). The engine compares this against the configured
# model at startup and refuses to run if they disagree.
EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE ingest_sources (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key         text        NOT NULL UNIQUE,
            kind        text        NOT NULL,
            config      jsonb       NOT NULL DEFAULT '{}'::jsonb,
            watermark   timestamptz,
            active      boolean     NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE documents (
            id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_key            text        NOT NULL,
            source_hash           text        NOT NULL,
            title                 text        NOT NULL DEFAULT '',
            res_model             text,
            res_id                bigint,
            external_ref          text,
            company_id            integer,
            visibility            smallint    NOT NULL DEFAULT 1,
            embedding_model       text        NOT NULL,
            embedding_dimensions  integer     NOT NULL DEFAULT {EMBEDDING_DIMENSIONS},
            metadata              jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            record_write_date     timestamptz,
            indexed_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT documents_visibility_range CHECK (visibility BETWEEN 0 AND 2)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE chunks (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id  bigint   NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
            ordinal      integer  NOT NULL,
            content      text     NOT NULL,
            token_count  integer  NOT NULL DEFAULT 0,
            embedding    vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            -- Generated, so the lexical and dense sides always describe the same
            -- text. A column maintained by application code could drift.
            content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
            -- Denormalised from documents so the pre-filter is a single index scan
            -- on chunks. A join would defeat the filtered HNSW path.
            res_model    text,
            res_id       bigint,
            company_id   integer,
            visibility   smallint NOT NULL DEFAULT 1,
            metadata     jsonb    NOT NULL DEFAULT '{{}}'::jsonb,
            CONSTRAINT chunks_document_ordinal_key UNIQUE (document_id, ordinal)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ingest_jobs (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_key  text        NOT NULL,
            kind        text        NOT NULL,
            status      text        NOT NULL DEFAULT 'pending',
            payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
            run_after   timestamptz NOT NULL DEFAULT now(),
            attempts    integer     NOT NULL DEFAULT 0,
            last_error  text,
            locked_at   timestamptz,
            locked_by   text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ingest_jobs_status_values
                CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead'))
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE embedding_cache (
            content_hash text        NOT NULL,
            model        text        NOT NULL,
            embedding    vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (content_hash, model)
        )
        """
    )

    # Idempotent ingestion: unchanged content is skipped before any API call.
    op.execute("CREATE UNIQUE INDEX documents_source_hash_key ON documents (source_hash)")
    # Incremental sync watermark scans.
    op.execute(
        "CREATE INDEX documents_source_written_idx ON documents (source_key, record_write_date)"
    )
    # Delete-and-replace on record update, and citation resolution.
    op.execute("CREATE INDEX documents_record_idx ON documents (res_model, res_id)")

    # Dense retrieval. HNSW builds incrementally and needs no training step, unlike
    # IVFFlat whose lists are learned from the data (ADR-0004).
    op.execute(
        """
        CREATE INDEX chunks_embedding_hnsw_idx ON chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )
    # Lexical retrieval, fused with the dense results at query time (M8).
    op.execute("CREATE INDEX chunks_tsv_gin_idx ON chunks USING gin (content_tsv)")
    # Pre-filter selectivity. company_id leads: it is the high-cardinality
    # discriminator and is present in every predicate.
    op.execute("CREATE INDEX chunks_scope_idx ON chunks (company_id, visibility)")
    op.execute("CREATE INDEX chunks_record_idx ON chunks (res_model, res_id)")
    op.execute("CREATE INDEX chunks_document_idx ON chunks (document_id)")

    # Queue polling with SELECT ... FOR UPDATE SKIP LOCKED (M7). Partial, because
    # finished jobs accumulate and are never claimed.
    op.execute(
        """
        CREATE INDEX ingest_jobs_claim_idx ON ingest_jobs (status, run_after)
            WHERE status IN ('pending', 'running')
        """
    )


def downgrade() -> None:
    # Reverse creation order. chunks first: it references documents.
    op.execute("DROP TABLE IF EXISTS embedding_cache")
    op.execute("DROP TABLE IF EXISTS ingest_jobs")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS ingest_sources")
    # The extension is left in place: another database object may depend on it,
    # and dropping it is not this migration's to undo.
