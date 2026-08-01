# ADR-0002: Run the AI engine as a sidecar service, not inside Odoo

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team

## Context

Odoo Atlas needs to run retrieval-augmented generation against company data held in
Odoo. The obvious first instinct is to put the AI code inside the Odoo addon and
call it from a model method. Four forces push back hard.

**1. Dependency conflict.** Odoo pins its Python dependencies tightly and ships
them as a distribution package (`requirements.txt` in the Odoo source tree, plus
Debian packages in the official image). The AI stack — provider SDKs, tokenizers,
PDF parsers, optional local embedding models — moves fast and pulls a wide
transitive tree. Installing both into one interpreter means every Odoo minor
upgrade risks breaking the AI stack and vice versa. This is not hypothetical: it is
the single most common failure mode of "AI inside Odoo" modules.

**2. The worker model.** Odoo serves HTTP from a fixed pool of pre-forked,
**synchronous** workers (`workers = N` in `odoo.conf`). An LLM call takes 2–30
seconds and is almost entirely network wait. Blocking an Odoo worker for that long
is catastrophic: with the common default of 4–8 workers, a handful of concurrent
chat sessions starves the ERP itself. Odoo also enforces `limit_time_real`, which
will simply kill a long request.

**3. Independent scaling.** Chat traffic and ERP traffic have different shapes.
Ingestion in particular is a CPU- and network-bound batch workload that we want to
scale to zero most of the day and burst on a schedule. Coupling it to Odoo's worker
pool makes that impossible.

**4. Testability.** We want the RAG engine covered by fast unit tests. Importing
`odoo` requires a database, a registry, and a loaded module graph — a multi-second
fixture on every test run. Code that cannot be tested quickly does not get tested.

## Decision

We will split Odoo Atlas into **three deployable units** plus one shared database
cluster:

```
┌──────────────────────┐        HTTP/JSON        ┌────────────────────────┐
│  odoo                │  ────────────────────▶  │  atlas-api             │
│  Odoo 19 CE          │  ◀────────────────────  │  FastAPI + atlas core  │
│  + odoo_atlas addon  │   callback: ORM reads   │  (async)               │
└──────────┬───────────┘                         └───────────┬────────────┘
           │                                                 │
           │ Odoo ORM                                        │ SQLAlchemy Core
           ▼                                                 ▼
    ┌─────────────┐                                  ┌───────────────┐
    │ DB: odoo    │                                  │ DB: atlas     │
    └─────────────┘                                  │ (+ pgvector)  │
                                                     └───────────────┘
                          ┌────────────────────┐
                          │  atlas-worker      │  same image as atlas-api,
                          │  ingestion jobs    │  different entrypoint
                          └────────────────────┘
```

- **`odoo_atlas` (Odoo addon)** — a *thin adapter*. It owns conversation/message
  models, views, menus, security groups, the settings page, and the OWL chat
  widget. It contains **no AI logic, no prompts, and no vendor SDK imports**. Its
  only outbound dependency is an HTTP client pointed at `atlas-api`.
- **`atlas` (Python package)** — the engine. A framework-agnostic library laid out
  hexagonally (`domain` / `application` / `infrastructure` / `interfaces`). It
  **never imports `odoo`**; this is enforced in CI by an import-linter rule.
- **`atlas-api` / `atlas-worker`** — the same image, two entrypoints. The API serves
  synchronous query traffic; the worker drains the ingestion queue.

The addon and the service authenticate to each other with a shared service token,
and the addon propagates the *acting Odoo user* on every request so the service can
call back for authorization decisions (see [ADR-0006](0006-data-access-and-authorization.md)).

## Consequences

### Benefits

- The AI stack upgrades on its own cadence. An Odoo 19 → 20 migration touches the
  addon only.
- `atlas` is testable with plain `pytest` in milliseconds — no Odoo registry, no
  database for unit tests.
- Ingestion scales horizontally by adding worker replicas; the API scales
  independently by concurrency.
- Async I/O throughout the service means one process handles many in-flight LLM
  calls, which a pre-forked Odoo worker never could.
- Streaming responses (SSE) are natural in FastAPI and awkward in Odoo's WSGI stack.

### Costs

- **Two services to operate.** Mitigated by shipping a single `docker-compose.yml`
  that brings the whole system up with one command (M1), and by a `/healthz`
  endpoint the addon surfaces on the settings page.
- **A network boundary to secure.** The service must never be exposed publicly. It
  binds to the compose network only; the token is required on every call; the
  service treats every request as untrusted. Addressed in M6 and M13.
- **Latency budget.** One extra hop, ~1–3 ms on a container network. Negligible
  against a multi-second LLM call.
- **Distributed failure modes.** The addon must degrade gracefully when the service
  is down — the chat UI shows a clear "assistant unavailable" state rather than a
  traceback. Explicit acceptance criterion in M6.
- **Two dependency manifests** to keep audited. Handled by CI dependency scanning
  in M14.

## Alternatives considered

**In-process inside the Odoo addon.** Rejected for the four forces above. It is
simpler to deploy and that is its only advantage. The dependency-conflict and
worker-starvation problems are not solvable within it.

**In-process, but offloaded to `queue_job` (OCA).** A real improvement over naive
in-process — it moves LLM calls off the HTTP workers onto a job runner. Rejected
anyway because it solves only force #2. Dependency conflicts, testability, and
independent scaling remain, and it adds a hard dependency on an OCA module that
lags each Odoo release. We do borrow its *idea* (a durable job queue) in M7,
implemented on our own side of the boundary.

**A fully separate product with its own UI.** Rejected: the brief is an assistant
*inside* Odoo. Making users leave the ERP defeats the purpose, and it would forfeit
Odoo's authentication, record rules, and navigation.

**Serverless functions for the AI layer.** Rejected for this stage: cold starts hurt
an interactive assistant, local development becomes vendor-specific, and the
ingestion workload wants long-lived processes with warm connection pools. The
sidecar can be redeployed to Cloud Run/Fargate later without changing the code —
that is the point of keeping `interfaces` thin.
