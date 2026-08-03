-- Query plans for the two searches retrieval issues.
--
-- Run against a corpus built by `generate_dataset.py`:
--
--     make bench-explain
--
-- or by hand:
--
--     psql "$ATLAS_BENCH_DATABASE_URL" -v ON_ERROR_STOP=1 -f benchmarks/explain.sql
--
-- The probe vector is taken from the table rather than generated, because psql
-- cannot easily build a 1536-component literal. That makes the *distances*
-- unrepresentative — a point in the index is trivially near itself — but the
-- plan shape and buffer counts, which are what this file is for, are the same
-- either way. `latency.py` measures with unseen probes.

\set ON_ERROR_STOP on
\set QUIET on

\echo
\echo == Session settings ==
SELECT current_setting('server_version') AS postgresql,
       (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector;

\echo
\echo == 1. Dense search, planner free ==
\echo -- What Atlas issued before M13. The (company_id, visibility) index wins the
\echo -- cost comparison and the vector index goes unused.
BEGIN;
SET LOCAL hnsw.ef_search = 40;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT id
FROM bench_chunks
WHERE company_id = 1 AND visibility >= 1
ORDER BY embedding <=> (SELECT embedding FROM bench_chunks ORDER BY id LIMIT 1)
LIMIT 8;
ROLLBACK;

\echo
\echo == 2. Dense search, forced index, no iterative scan ==
\echo -- Fast, and returns fewer rows than asked for: the HNSW walk exhausts its
\echo -- candidate list on rows the filter rejects and stops.
BEGIN;
SET LOCAL hnsw.ef_search = 40;
SET LOCAL hnsw.iterative_scan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_seqscan = off;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT id
FROM bench_chunks
WHERE company_id = 1 AND visibility >= 1
ORDER BY embedding <=> (SELECT embedding FROM bench_chunks ORDER BY id LIMIT 1)
LIMIT 8;
ROLLBACK;

\echo
\echo == 3. Dense search, forced index + relaxed_order  (what ships) ==
BEGIN;
SET LOCAL hnsw.ef_search = 40;
SET LOCAL hnsw.iterative_scan = relaxed_order;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_seqscan = off;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT id
FROM bench_chunks
WHERE company_id = 1 AND visibility >= 1
ORDER BY embedding <=> (SELECT embedding FROM bench_chunks ORDER BY id LIMIT 1)
LIMIT 8;
ROLLBACK;

\echo
\echo == 4. Lexical search (GIN) ==
\echo -- Wants the bitmap scan the dense search disables, which is why those
\echo -- settings are SET LOCAL and scoped to the dense query alone.
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT id, ts_rank_cd(content_tsv, query) AS rank
FROM bench_chunks, plainto_tsquery('english', 'order S00042 customer') AS query
WHERE content_tsv @@ query AND company_id = 1
ORDER BY rank DESC
LIMIT 8;
ROLLBACK;

\echo
\echo == Index sizes ==
SELECT indexrelname AS index,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE relname = 'bench_chunks'
ORDER BY pg_relation_size(indexrelid) DESC;
