# Performance

Measured, not estimated. Everything here comes from `make bench` against
PostgreSQL 17 with pgvector 0.8.6, in the compose stack, on the machine that ran
it. Reproduce with:

```bash
make bench
```

Numbers move with hardware. What does not move is the *shape*: which
configurations are faster than which, and by how much.

## The finding that mattered

Atlas's dense search filters by company and visibility, then orders by vector
distance. That is the natural way to write it and it was **32× slower than it
needed to be**.

At 50,000 chunks, `LIMIT 8`, `ef_search = 40`:

| Configuration | p50 | p95 | Rows returned |
| --- | ---: | ---: | ---: |
| Filter in `WHERE`, planner free | 126.95 ms | 156.50 ms | 8.0 |
| Forced index scan, no iterative scan | 2.82 ms | 148.47 ms | **5.1** |
| Forced index scan + `iterative_scan = relaxed_order` | **3.94 ms** | **11.43 ms** | 8.0 |

Two separate problems, and each fix is useless without the other.

**The planner does not choose the vector index.** With a company filter matching
a third of the table, PostgreSQL costs a bitmap scan over
`(company_id, visibility)` below an HNSW walk and takes it — then sorts sixteen
thousand rows by distance. `EXPLAIN` says so plainly:

```
->  Bitmap Heap Scan on chunks (actual time=0.512..103.520 rows=16666 loops=1)
      Recheck Cond: ((company_id = 1) AND (visibility >= 1))
```

`hnsw.iterative_scan` was already set and did nothing, because it governs an
index scan that was never chosen.

**Forcing the index alone returns incomplete results.** The HNSW walk exhausts
its candidate list on rows the filter rejects and stops — 5.1 rows for a `LIMIT`
of 8, silently. This is the filtered-ANN recall collapse
[ADR-0004](adr/0004-vector-store-and-index-strategy.md) names, and it is what
`iterative_scan` exists for.

Both are now set together, scoped with `SET LOCAL` to the dense search's own
transaction:

```sql
SET LOCAL hnsw.iterative_scan = relaxed_order;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_seqscan = off;
```

Scoping matters. The lexical search *wants* a bitmap scan — that is how a GIN
index is read — so this must never leak onto it.

## HNSW parameter sweep

50,000 chunks, 1536-dimensional vectors, top-8, 40 queries per point.

| m | ef_construction | ef_search | recall | p50 | p95 | build | index |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 64 | 10 | 0.078 | 1.72 ms | 2.64 ms | 13.0 s | 391 MB |
| 16 | 64 | 40 | 0.166 | 2.00 ms | 3.24 ms | 13.0 s | 391 MB |
| 16 | 64 | 200 | 0.394 | 3.98 ms | 5.08 ms | 13.0 s | 391 MB |
| 16 | 128 | 40 | 0.116 | 2.10 ms | 2.94 ms | 23.9 s | 391 MB |
| 16 | 128 | 200 | 0.506 | 4.89 ms | 7.47 ms | 23.9 s | 391 MB |
| 32 | 128 | 40 | 0.150 | 3.40 ms | 5.60 ms | 39.3 s | 391 MB |
| 32 | 128 | 200 | 0.503 | 8.40 ms | 13.17 ms | 39.3 s | 391 MB |

At 200,000 chunks the same sweep builds in 155–346 s and produces a 1.5–1.6 GB
index, with p50 in the 1.2–1.5 ms range.

### What these recall numbers are, and are not

**They are not a prediction of production recall.** They are recall against exact
search *on a synthetic corpus*, and the corpus is the weak part. Getting one that
behaves like real embeddings took four attempts, each of which produced a table
that measured nothing:

1. Uniform `random()` — every vector in the positive orthant, all pairs at ~0.75
   cosine similarity, "nearest" decided by tie-breaking.
2. Isotropic Gaussian — spread over the sphere, but every point equidistant from
   every other. Recall 1.000 at every setting: a flat line.
3. Probing with vectors drawn from the table — the graph entry lands on the probe
   itself and its true neighbours are its own edges. Recall 1.000 again.
4. Tight clusters — ~170 members per centroid at near-identical distance, so the
   "true top 8" is decided by rounding.

The fifth attempt — clustered with a per-point radius — produces the monotone
curve above. It is a curve, and the *ordering* it shows is the actionable part:
recall rises with `ef_search`, latency rises with it, and a higher
`ef_construction` buys recall at the same `ef_search`. The absolute values are
depressed by residual near-ties in the fixture and should not be read as what
Atlas will achieve on real embeddings.

**Latency, build time and index size do not carry that caveat.** The index does
not know what the vectors mean; timing 1536-dimensional distance computations
over a graph of 50,000 nodes measures the same work either way.

The honest way to get production recall is `make eval --live` against a real
corpus, which is a deployment activity rather than a CI one.

### Chosen parameters

`m = 16`, `ef_construction = 64`, `ef_search = 40`.

`m = 32` costs 3× the build time and is slower at query time for no measured
recall advantage. `ef_construction = 128` doubles build time for a small recall
gain that `ef_search` buys more cheaply — and `ef_search` can be changed without
a rebuild, which `ef_construction` cannot.

`ef_search = 40` is pgvector's default and sits where the latency curve is still
flat. Raising it to 200 roughly doubles p50. That is the dial to turn if
production recall proves insufficient.

## Query plans

Dense search, with the pre-filter and the settings above:

```
Index Scan using chunks_embedding_hnsw_idx on chunks
  (actual time=7.539..7.648 rows=8 loops=1)
  Buffers: shared hit=362 read=1860
Execution Time: 7.687 ms
```

Lexical search:

```
Bitmap Heap Scan on chunks (actual time=0.478..0.530 rows=4 loops=1)
  ->  Bitmap Index Scan on chunks_tsv_gin_idx
        (actual time=0.449..0.449 rows=10 loops=1)
  Buffers: shared hit=23 read=24
Execution Time: 0.663 ms
```

The lexical half is an order of magnitude cheaper than the dense half and reads
a fiftieth of the buffers. Fusing them costs almost nothing beyond the dense
query.

## Where the time actually goes

Retrieval is not the expensive part of an answer, which is why there is **no
query cache**. One is easy to build and the measurements say it would save
single-digit milliseconds off a request dominated by seconds:

| Stage | Cost | Source |
| --- | --- | --- |
| Dense search | ~4 ms p50 | measured above |
| Lexical search | <1 ms | measured above |
| Authorization round-trip to Odoo | one HTTP call per model | batched, M6 |
| Model call | seconds | provider |

The cache that pays for itself already exists: the embedding cache and
content-hash short-circuit from M7, which turn a re-sync into a no-op and cost
nothing per query. Adding a second cache in front of a 4 ms query would add an
invalidation problem to save 0.1% of a request.

This is a decision the numbers made. Before measuring, a retrieval cache looked
like an obvious win.

## Infrastructure notes

**`shm_size` on the PostgreSQL container.** Docker gives a container 64 MB of
`/dev/shm`, and a parallel HNSW build asks for around a gigabyte. It fails with:

```
could not resize shared memory segment ... : No space left on device
```

which reads like a disk problem and is not one. The compose file sets `2gb`.

**`maintenance_work_mem`** must be ≥ 1 GB during an index build. HNSW builds in
memory or spills catastrophically.

## Reproducing

```bash
make bench                       # 20k rows, the default sweep
make bench ROWS=200000           # scale check
```

`ATLAS_BENCH_DATABASE_URL` must point at a database the benchmark may create and
drop tables in. It is never the configured corpus — the harness would eat it.
