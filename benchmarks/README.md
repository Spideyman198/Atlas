# Benchmarks

Every performance number in `docs/performance.md` comes from here. This document
says how to reproduce them and what each one is worth.

## Running

```bash
make bench
```

Creates the `atlas_bench` database if absent, generates a corpus, runs the recall
sweep and the latency comparison, and writes a timestamped JSON and CSV file per
run to `results/`.

```bash
make bench ROWS=200000        # scale check
make bench-explain            # query plans, as psql prints them
```

Or directly, with `ATLAS_BENCH_DATABASE_URL` set:

```bash
python -m benchmarks.generate_dataset --rows 50000
python -m benchmarks.recall  --rows 50000 --queries 40
python -m benchmarks.latency --rows 50000 --queries 40
python -m benchmarks.run_all --rows 50000
```

`ATLAS_BENCH_DATABASE_URL` must name a database these scripts may **create and
drop tables in**. It is deliberately not `ATLAS_DATABASE__URL`: pointing them at
a live corpus would destroy it.

## Files

| File | What it does |
| --- | --- |
| `config.py` | Every constant a published number depends on |
| `environment.py` | Commit, database versions, host, PostgreSQL settings |
| `generate_dataset.py` | The synthetic corpus, and why it is shaped as it is |
| `recall.py` | HNSW parameter sweep: recall against exact search |
| `latency.py` | Query-shape comparison: what the planner actually does |
| `explain.sql` | `EXPLAIN (ANALYZE, BUFFERS)` for the four plans |
| `results.py` | Timestamped JSON and CSV output |
| `run_all.py` | Sequences recall then latency |

## What each script measures

**`recall.py`** asks whether the approximate index returns the same rows a
sequential scan would. Ground truth comes from a second connection with index
scans disabled for the session — not from toggling `SET LOCAL` on one
connection, which worked in isolation and silently stopped working across a
transaction boundary, producing a table where recall alternated between 1.000 at
105 ms and 0.000.

It **forces the index scan** on the measured connection. Tuning an index
parameter means measuring the index; whether the planner would choose it is a
different question, and it is `latency.py`'s.

**`latency.py`** leaves the planner alone and measures what production gets. It
records the row count alongside the latency, because a filtered approximate
search that stops early returns fewer rows than asked for — silently — and
latency alone would make that configuration look like the fastest one.

**`explain.sql`** shows the plan and buffer counts for the same four
configurations. Its probe is a row from the table, because psql cannot easily
build a 1536-component literal; that makes the distances unrepresentative but
not the plan shape.

## The corpus, and four ways of getting it wrong

Synthetic, deterministic, generated in the database. A real ERP export would be
a better benchmark and a worse fixture: it cannot be committed, it moves, and a
number that changed because somebody confirmed an order says nothing about an
index.

Getting the distribution right took five attempts. The first four each produced
a table that measured nothing, and the failure mode is not obvious:

| Attempt | Result | Why |
| --- | --- | --- |
| Uniform `random()` | Recall measured scan order | Every vector in the positive orthant, all pairs ~0.75 similar |
| Isotropic Gaussian | Recall 1.000 everywhere | In 1536 dimensions every point is equidistant from every other |
| Probe drawn from the table | Recall 1.000 everywhere | The graph entry lands on the probe; its neighbours are its own edges |
| Tight clusters, one radius | Recall measured rounding | ~170 members per centroid at identical distance |
| Clusters, per-point radius | A monotone curve | What ships |

Residual near-ties still depress the absolute recall figures. `docs/performance.md`
publishes them as a shape, not as a prediction.

Generation is the slow part: about 250 s for 50,000 rows, because each vector is
1536 trigonometric expressions evaluated in SQL. Index builds are 15–40 s.

## Results files

`results/<UTC timestamp>-<kind>.json` and `.csv`, sortable by filename.

The JSON carries the environment; the CSV carries only the rows, because a
spreadsheet is where a table gets compared against the one in the documentation
and nested objects do not survive that.

```json
{
  "kind": "recall",
  "parameters": { "rows": 50000, "command": "python -m benchmarks.recall ..." },
  "environment": {
    "git": { "commit": "…", "dirty": false, "note": "…" },
    "database": { "postgresql": "PostgreSQL 17.10", "pgvector": "0.8.6", "settings": {} },
    "host": { "cpu_model": "…", "cpu_count": 12, "memory_gb": 7.4 }
  },
  "rows": []
}
```

The `dirty` flag is load-bearing. A run against uncommitted changes cannot be
reproduced by checking out the recorded commit, and a table that does not say so
invites somebody to try. `make bench` passes the commit in from the host,
because the container has no git binary — the first run of this recorded an
empty commit, which is exactly the gap the field exists to close.

## Interpreting the numbers

**Latency, build time and index size transfer.** The index does not know what
the vectors mean; timing distance computations over a graph of 50,000 nodes
measures the same work whatever produced the vectors.

**Absolute recall does not.** It is recall against exact search on a synthetic
corpus whose distance distribution is not that of a real embedding model. The
*ordering* — more `ef_search` buys recall and costs latency, higher `m` buys
recall and costs build time — is the actionable part.

Production recall comes from `make eval --live` against a real corpus, which is
a deployment activity rather than a CI one.

## Adding a benchmark

Measure before implementing. If a change is meant to make something faster, add
the measurement here first, record the number it produces, then make the change
and record it again. `docs/performance.md` has one entry that reversed a
decision that had looked obvious.
