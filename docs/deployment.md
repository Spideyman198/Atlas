# Deployment

Installing Atlas next to a real Odoo. For a laptop stack that starts in one
command, see [installation.md](installation.md); this is the version that
survives other people using it.

## What you are deploying

Three processes and two databases:

```
   users ─▶ Odoo ─────────────▶ atlas-api ─────▶ model provider
              │      HTTP  ◀────    │
              │                     │
        Odoo's PostgreSQL     Atlas's PostgreSQL + pgvector
                                    ▲
                              atlas-worker (ingestion)
```

`atlas-api` answers questions. `atlas-worker` fills the index. They share an
image and a database and differ only in the command they run.

**The two databases can share a cluster but not a database.** Atlas runs
Alembic migrations against its own; pointing it at Odoo's would put `chunks` and
`documents` beside `res_partner`, and Odoo's own upgrade tooling makes no
allowance for tables it does not own.

## Requirements

| | Minimum | Notes |
| --- | --- | --- |
| Odoo | 19.0 Community | The addon declares `19.0` in its manifest |
| PostgreSQL | 17 | 16 works; 17 is what is tested |
| pgvector | 0.8.0 | `readyz` refuses to start below this |
| Memory, engine | 1 GB | Plus the HNSW index, which is memory-resident in practice |
| Memory, PostgreSQL | 4 GB | ≥ 1 GB of it as `maintenance_work_mem` during index builds |
| Disk | ~8 GB per 10⁶ chunks | 1536-d vectors are 6 KB each before the index |

`/dev/shm` on the PostgreSQL container must be at least 2 GB. A parallel HNSW
build asks for about a gigabyte of shared memory, and Docker's 64 MB default
fails with `could not resize shared memory segment ... No space left on device`
— which reads like a disk problem and is not one.

## 1. Databases

```bash
createdb -U postgres atlas
psql -U postgres -d atlas -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

Atlas needs `CREATE` on its own database and nothing on Odoo's. It never
connects to Odoo's database — every read goes through Odoo's HTTP API so that
record rules apply ([ADR-0006](adr/0006-data-access-and-authorization.md)).

## 2. Secrets

Four, all from the environment. None belongs in `ir.config_parameter`: reading
one needs system rights, and the code that calls the engine runs as whichever
user asked the question, so a parameter would force a `sudo()` onto the request
path.

```bash
# Proves a caller is the engine. Same value on both sides.
ATLAS_SERVICE_TOKEN=$(openssl rand -hex 32)

# Signs the short-lived tokens naming the acting user. Odoo side only —
# the engine cannot mint one, which is what stops a compromised engine
# promoting itself to an arbitrary user.
ATLAS_CONTEXT_SECRET=$(openssl rand -hex 32)
```

Plus the model provider key (`ATLAS_CHAT__API_KEY`), the embedding key
(`ATLAS_EMBEDDING__API_KEY`), and the database URL.

Rotating `ATLAS_CONTEXT_SECRET` invalidates every token in flight. Tokens live
15 minutes by default, so a rotation costs at most one round of failed
questions; it is not a reason to avoid rotating it.

## 3. The engine

```bash
docker run -d --name atlas-api \
  -e ATLAS_DATABASE__URL='postgresql://atlas:...@postgres:5432/atlas' \
  -e ATLAS_CHAT__VENDOR=anthropic \
  -e ATLAS_CHAT__MODEL=claude-opus-5 \
  -e ATLAS_CHAT__API_KEY="$ANTHROPIC_API_KEY" \
  -e ATLAS_EMBEDDING__VENDOR=openai \
  -e ATLAS_EMBEDDING__MODEL=text-embedding-3-small \
  -e ATLAS_EMBEDDING__API_KEY="$OPENAI_API_KEY" \
  -e ATLAS_ODOO__BASE_URL=http://odoo:8069 \
  -e ATLAS_ODOO__DATABASE=production \
  -e ATLAS_ODOO__SERVICE_TOKEN="$ATLAS_SERVICE_TOKEN" \
  ghcr.io/spideyman198/atlas/atlas:latest
```

The composition root builds every provider at startup and **refuses to start
without a key**. That is deliberate: a missing key that surfaced on the first
user question would look like a model outage.

Migrations run separately, so a rollout can apply them once rather than once per
replica:

```bash
docker run --rm -e ATLAS_DATABASE__URL='...' \
  ghcr.io/spideyman198/atlas/atlas:latest alembic upgrade head
```

## 4. The worker

Same image, different command. One is enough; the job queue uses
`SELECT ... FOR UPDATE SKIP LOCKED`, so more than one is safe.

```bash
docker run -d --name atlas-worker \
  -e ATLAS_DATABASE__URL='...' \
  ...same provider and Odoo variables... \
  ghcr.io/spideyman198/atlas/atlas:latest atlas worker
```

`atlas` is the packaged command line; `atlas --help` lists the rest
(`sources`, `sync`, `reindex`).

## 5. The addon

Copy `addons/odoo_atlas` onto the Odoo addons path, or mount it, then:

```bash
odoo -d production -i odoo_atlas --stop-after-init
```

Odoo caches the module registry, so **restart Odoo after installing**. Without
it the new models return 404 while the server still answers for the old
registry — a failure that looks like a broken deployment and is a stale cache.

Set on the Odoo process:

```bash
ATLAS_ENGINE_URL=http://atlas-api:8000
ATLAS_SERVICE_TOKEN=...        # the same value the engine has
ATLAS_CONTEXT_SECRET=...       # Odoo only
ATLAS_CONTEXT_TOKEN_TTL=900
```

Then grant people the **Atlas / User** group. Nobody sees the panel without it.

## 6. Verify

In order, because each depends on the last:

```bash
curl -fsS http://atlas-api:8000/healthz     # the process is alive
curl -fsS http://atlas-api:8000/readyz      # database, pgvector, schema, Odoo
```

`readyz` reports each dependency separately:

```json
{"status":"ready","checks":{"database":"ok","pgvector":"ok (0.8.6)",
 "schema":"ok (1536-d)","odoo":"ok (production)"}}
```

`"odoo":"ok"` means the engine reached Odoo *and* the service tokens match. If
it says otherwise, the two sides disagree about `ATLAS_SERVICE_TOKEN`.

Then index something and ask a question:

```bash
curl -fsS -XPOST http://atlas-api:8000/v1/ingest/sync \
  -H 'Content-Type: application/json' -d '{"sources":["odoo.res.partner"]}'
```

and open **Atlas → Ask Atlas** in Odoo. A first question that returns "I don't
have information on that" with an empty index is correct behaviour, not a fault
— see [orchestration.md](orchestration.md).

## Operating it

### Probes

| Probe | Answers | On failure |
| --- | --- | --- |
| `/healthz` | Is the process alive? | Restart it |
| `/readyz` | Should it receive traffic? | Take it out of rotation; do **not** restart |

Wiring liveness to the database would turn a brief database outage into a
rolling restart across every replica.

### Metrics and traces

`/metrics` serves Prometheus format, on by default. Labels are held to a fixed
low-cardinality set — nothing per user, per conversation or per question — so
scraping is not a way to learn what anybody asked.

Traces are off unless `ATLAS_OBSERVABILITY__OTLP_ENDPOINT` is set, and the
exporter is an optional extra (`pip install atlas[otlp]`). See
[evaluation.md](evaluation.md).

### Ingestion

A cron in the addon queues an incremental sync. It reads what changed since a
watermark and skips unchanged content by hash, so the steady-state cost is close
to zero. A full re-index is:

```bash
curl -XPOST http://atlas-api:8000/v1/ingest/sync -d '{"kind":"reindex"}'
```

`reindex` re-embeds everything and costs real money. Run it after changing the
embedding model, and not otherwise.

### Backups

Both databases. Odoo's is the source of truth; **Atlas's is not a cache you can
throw away** — rebuilding it means re-embedding the whole corpus at the
provider's per-token rate.

The vector database holds ERP content stripped of Odoo's record rules. Protect
it exactly like Odoo's own ([security.md](security.md)).

### The runtime image has no pip

Deliberate. The virtualenv arrives complete from the build stage, so nothing
installs anything at run time, and pip's vendored copies of `msgpack` and
`setuptools` were the only HIGH findings in an otherwise clean image. A
container without a package installer is also one an attacker cannot
`pip install` into.

`alembic` is installed in the virtualenv and unaffected — migrations still run
from the image.

## Upgrading

1. Read the [changelog](../CHANGELOG.md). Before 1.0 the REST contracts, model
   fields and configuration keys may change between milestones.
2. Apply migrations: `alembic upgrade head`.
3. Update the addon: `odoo -d production -u odoo_atlas --stop-after-init`, then
   restart Odoo.
4. Roll the engine and worker.

Migrations are hand-written and forward-only in practice
([ADR-0008](adr/0008-migrations.md)). Downgrades exist and are tested, but
restoring a backup is the safer rollback.

## Sizing

Measured figures are in [performance.md](performance.md). The short version, at
50,000 chunks on a 12-core container:

| | |
| --- | --- |
| Dense search | ~2 ms p50 |
| Lexical search | <1 ms |
| HNSW index | ~390 MB |
| Index build | 15–45 s |

An answer is dominated by the model call — seconds — not by retrieval. Scale the
engine for concurrent model calls, not for search.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `readyz` reports `odoo: unreachable` | Wrong `ATLAS_ODOO__BASE_URL`, or the addon is not installed |
| `readyz` reports `odoo` refused | The two `ATLAS_SERVICE_TOKEN` values differ |
| Every answer refuses | Nothing indexed yet; run a sync |
| 404 from `/atlas/api/*` after install | Odoo was not restarted; the registry is stale |
| `could not resize shared memory segment` | `/dev/shm` under 2 GB on the PostgreSQL container |
| Answers cite nothing | The user lacks read access to the records retrieved — working as intended |
| Chat panel missing | The user is not in the **Atlas / User** group |
