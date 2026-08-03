# Performance

Measured, not estimated. Every number below was produced by the scripts in
[`benchmarks/`](../benchmarks/README.md), and the raw output — including the
environment that produced it — is committed under `benchmarks/results/`.

## Measurement environment

Every table on this page was produced under these conditions unless it says
otherwise.

| | |
| --- | --- |
| Dataset | 50,000 chunks, synthetic, clustered (`benchmarks/generate_dataset.py`) |
| Embedding dimensions | 1536 |
| Neighbours requested | 8 (`top_k`), matching `ATLAS_RETRIEVAL__LIMIT` |
| Queries per point | 40 |
| PostgreSQL | 17.10 (Debian 17.10-1.pgdg12+1) |
| pgvector | 0.8.6 |
| Host | AMD Ryzen 7 7445HS, 12 cores, 7.4 GB available to the container |
| Platform | Linux 6.18 WSL2, containerised (Docker Desktop on Windows 11) |
| `shared_buffers` | 128 MB |
| `work_mem` | 4 MB |
| `maintenance_work_mem` | 64 MB session default, raised to 1 GB for index builds |
| Commit | recorded in each results file under `environment.git.commit` |

Reproduce with:

```bash
make bench
```

Absolute timings depend on the host. What transfers is the relative cost of one
query shape against another; what does not is the absolute recall figure, and
the ranking it appears to imply. See Benchmark limitations.

## Performance findings

### Filtered dense search returns incomplete results without `iterative_scan`

Atlas's dense search filters by company and visibility, then orders by vector
distance. Measured at 50,000 chunks, `LIMIT 8`, `ef_search = 40`:

| Configuration | p50 | p95 | Rows returned |
| --- | ---: | ---: | ---: |
| Unfiltered, planner free | 1.91 ms | 5.44 ms | 8.0 |
| Filtered, planner free | 1.30 ms | 1.88 ms | **4.3** |
| Filtered, forced index, no iterative scan | 1.01 ms | 2.08 ms | **4.3** |
| Filtered, forced index + `relaxed_order` | 2.12 ms | 8.62 ms | 8.0 |

Source: `benchmarks/results/20260803T190633Z-latency.json`.
Command: `python -m benchmarks.latency --rows 50000 --queries 40 --top-k 8`.

The HNSW walk exhausts its candidate list on rows the filter rejects and stops.
It returns what it has — four rows where eight were asked for — with no error.
The two fastest configurations in that table are fast because they are doing
less work than the query requires. Repeated runs put the under-return between
3.2 and 4.3 rows of 8; it is never complete.

`EXPLAIN` on a probe whose neighbours are mostly outside the filter shows the
same effect at its limit:

```
== Dense search, planner free ==
Limit (actual time=1.861..1.862 rows=0 loops=1)
  ->  Index Scan using bench_chunks_hnsw_idx (actual time=1.859..1.859 rows=0)
Execution Time: 1.976 ms

== Dense search, forced index + relaxed_order ==
Limit (actual time=2.471..3.843 rows=8 loops=1)
  ->  Index Scan using bench_chunks_hnsw_idx (actual time=2.470..3.840 rows=8)
Execution Time: 3.868 ms
```

Zero rows against eight, from the same query. Reproduce with `make bench-explain`.

### The planner's choice of scan is not stable

On an earlier corpus shape the same filtered query produced a different plan: a
bitmap scan over `(company_id, visibility)` followed by a sort of 16,666 rows,
at **126.95 ms p50** — complete results, 32× slower. `hnsw.iterative_scan` was
configured then and did nothing, because it governs an index scan the planner
had not chosen.

The committed fixture does not reproduce that plan; it reproduces the
under-return above. Both are failure modes of the same unforced configuration,
and which one appears depends on statistics the corpus happens to have. This is
recorded rather than dropped because it is the reason the fix disables the
alternatives rather than only setting `iterative_scan`.

### Configuration that ships

```sql
SET LOCAL hnsw.iterative_scan = relaxed_order;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_seqscan = off;
```

`SET LOCAL`, scoped to the dense search's own transaction. The lexical search
*wants* a bitmap scan — that is how a GIN index is read — so these must not leak
onto it. Applied in `PgVectorStore._search`.

## HNSW parameter sweep

50,000 chunks, top-8, recall against exact search.

| m | ef_construction | ef_search | recall | p50 | p95 | build | index |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 64 | 10 | 0.031 | 1.86 ms | 3.29 ms | 19.4 s | 391 MB |
| 16 | 64 | 40 | 0.084 | 2.20 ms | 4.58 ms | 19.4 s | 391 MB |
| 16 | 64 | 200 | 0.472 | 4.85 ms | 6.15 ms | 19.4 s | 391 MB |
| 16 | 128 | 10 | 0.106 | 1.48 ms | 2.37 ms | 32.7 s | 391 MB |
| 16 | 128 | 40 | 0.250 | 2.33 ms | 4.26 ms | 32.7 s | 391 MB |
| 16 | 128 | 200 | 0.466 | 6.15 ms | 8.20 ms | 32.7 s | 391 MB |
| 32 | 128 | 10 | 0.087 | 2.89 ms | 4.97 ms | 45.3 s | 391 MB |
| 32 | 128 | 40 | 0.131 | 3.84 ms | 8.07 ms | 45.3 s | 391 MB |
| 32 | 128 | 200 | 0.509 | 10.78 ms | 18.22 ms | 45.3 s | 391 MB |

Full grid in `benchmarks/results/20260803T190136Z-recall.csv`.

Two things are stable across runs and one is not.

**Stable:** recall rises monotonically with `ef_search`, and latency rises with
it. Build time rises with `m` and with `ef_construction`. Index size does not
move across the grid at this row count.

**Not stable: the ranking between build configurations.** An earlier run of the
same sweep on the same host measured `m=16, ef_construction=64` at 0.163 for
`ef_search = 40`, against 0.084 here — a factor of two — and put
`m=32, ef_construction=128` ahead of both, where this run puts it last. The
fixture's residual near-ties (see Benchmark limitations) move enough between
generations to reorder the configurations.

**Configurations cannot be ranked by these recall figures.** Only the
within-run `ef_search` trend survives repetition.

### Chosen parameters

`m = 16`, `ef_construction = 64`, `ef_search = 40`.

Chosen on **build cost and defaults, not on measured recall**, because the recall
measurements do not support a ranking. `m = 16, ef_construction = 64` is the
cheapest to build — 19.4 s against 45.3 s for `m = 32` at this row count, on
every rebuild — and `ef_search = 40` is pgvector's default, sitting where the
latency curve is still flat.

`ef_search` is the dial to turn first if production recall proves short, because
it needs no rebuild. Raising it to 200 roughly doubles p50.

**Open item:** whether `m` or `ef_construction` should rise cannot be answered by
this fixture. It needs `make eval --live` against a real corpus.

## Query plans

Dense search, with the pre-filter and the shipped settings:

```
Index Scan using chunks_embedding_hnsw_idx on chunks
  (actual time=2.470..3.840 rows=8 loops=1)
Execution Time: 3.868 ms
```

Lexical search:

```
Bitmap Heap Scan on chunks (actual time=0.478..0.530 rows=4 loops=1)
  ->  Bitmap Index Scan on chunks_tsv_gin_idx (actual time=0.449..0.449 rows=10)
  Buffers: shared hit=23 read=24
Execution Time: 0.663 ms
```

The lexical half is roughly an order of magnitude cheaper than the dense half.
Fusing them costs little beyond the dense query.

## No query cache

Retrieval is not the expensive part of an answer. A cache is easy to build, and
the measurements say it would save single-digit milliseconds from a request
dominated by seconds:

| Stage | Cost | Source |
| --- | --- | --- |
| Dense search | ~2.1 ms p50 | measured above |
| Lexical search | <1 ms | measured above |
| Authorization round-trip to Odoo | one HTTP call per model | batched, M6 |
| Model call | seconds | provider |

The cache that pays for itself already exists: the embedding cache and
content-hash short-circuit from M7, which turn a re-sync into a no-op and cost
nothing per query. A second cache in front of a 2 ms query would add an
invalidation problem to save a fraction of a percent.

Before measuring, a retrieval cache looked like an obvious win. This entry is
kept as the worked example for the rule in `benchmarks/README.md`: measure
first, implement second.

## Benchmark limitations

**Absolute recall figures do not transfer to production.** They are recall
against exact search on a synthetic corpus, and the corpus is the weak part.
Getting one that behaves like real embeddings took five attempts; the first four
each produced a table that measured nothing — scan order, equidistant points,
probes drawn from the index itself, and 170-way ties. The fifth produces the
monotone curve above, and residual near-ties still depress the absolute values.

What this means in practice: use the *ordering* in the sweep, not the numbers.
A configuration scoring 0.256 here is better than one scoring 0.163 here; neither
figure predicts what either would score on real embeddings.

**Latency, build time and index size do transfer**, subject to hardware. The
index does not know what the vectors mean.

**Recall varies substantially between runs.** The same sweep on the same host
measured `m=16, ef_construction=64` at `ef_search=40` as 0.163 on one run and
0.084 on the next, and reordered the build configurations between them. The
corpus is regenerated per run, and its residual near-ties are enough to move the
result. This is why the parameter choice above rests on build cost rather than
on the recall column.

**Single host, few runs.** Every table is one run on one machine. No confidence
intervals, no cross-machine comparison. Percentiles are nearest-rank over 40
samples, so p95 is the 38th value rather than an interpolation.

**The corpus is uniform in a way real ones are not.** Every chunk is the same
length and every cluster the same size. Real corpora have long documents,
sparse topics and duplicate boilerplate.

**Production recall is unmeasured.** It requires a real corpus and a real
embedding model: `make eval --live`. That is a deployment activity, not a CI one,
and it has not been run against a production dataset.

## Infrastructure notes

**`shm_size` on the PostgreSQL container.** Docker gives a container 64 MB of
`/dev/shm`, and a parallel HNSW build asks for around a gigabyte. It fails with:

```
could not resize shared memory segment ... : No space left on device
```

which reads like a disk problem and is not one. The compose file sets `2gb`.

**`maintenance_work_mem`** must be ≥ 1 GB during an index build. HNSW builds in
memory or spills catastrophically. The benchmark raises it per build; a
deployment should set it in `postgresql.conf`.

**Corpus generation is the slow step**, roughly 250 s for 50,000 rows, because
each vector is 1536 trigonometric expressions evaluated in SQL. Index builds are
15–40 s by comparison.
