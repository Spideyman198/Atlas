# ADR-0006: Odoo is the authorization authority; tool-calling over text-to-SQL

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team

## Context

This is the most consequential decision in the project. Everything else is
engineering taste; this one is the difference between a deployable product and a
data breach.

Odoo enforces access control in three layers:

1. **Groups** (`res.groups`) — what a user is.
2. **Model access rights** (`ir.model.access`) — CRUD per model per group.
3. **Record rules** (`ir.rule`) — row-level domains, evaluated per user. This is
   where multi-company isolation lives, where "a salesperson sees only their own
   opportunities" lives, and where the portal user's window onto the database is
   defined.

Record rules are **dynamic domains evaluated at query time against the acting
user's context** (`user.company_ids`, `user.id`, arbitrary related fields). They are
not a static ACL you can snapshot.

An AI assistant that reads company data therefore faces a hard question: *when the
model retrieves a chunk about Sales Order SO00035, was the person asking allowed to
see SO00035?* Get this wrong and a warehouse intern can ask "summarise our largest
customer's pricing" and receive it.

There is a second, related hazard: **the vector index is a lossy copy of the
database**. A chunk embedded on Monday reflects Monday's record. If the record's
ownership changes on Tuesday, any authorization baked into the index at ingestion
time is now wrong.

## Decision

### 1. Odoo is the single source of truth for authorization. Always. At query time.

Atlas never decides what a user may see. It **asks Odoo**, as that user, on every
request.

Retrieval is a three-stage pipeline:

```
       user question
            │
            ▼
  ┌───────────────────────┐
  │ 1. PRE-FILTER         │  cheap, index-assisted, best-effort
  │  vector + lexical     │  WHERE company_id = ANY(:allowed)
  │  search in pgvector   │    AND visibility <= :max_visibility
  └──────────┬────────────┘  over-fetch: k * 4 candidates
             │  candidate (res_model, res_id, chunk_id)[]
             ▼
  ┌───────────────────────┐
  │ 2. POST-FILTER        │  AUTHORITATIVE
  │  ask Odoo, as the     │  per model: search([('id','in',ids)])
  │  acting user, which   │  executed in the acting user's env
  │  ids survive          │  → record rules applied by Odoo itself
  └──────────┬────────────┘
             │  authorized chunks only
             ▼
  ┌───────────────────────┐
  │ 3. ASSEMBLE           │  rank, deduplicate, budget tokens,
  │  context + citations  │  attach citations
  └───────────────────────┘
             │
             ▼
      prompt → LLM
```

**Stage 2 is non-negotiable and cannot be disabled by configuration.** Nothing
reaches the prompt without Odoo having confirmed, in this request, that this user
can read that record. The pre-filter exists purely for efficiency; if it were
removed entirely the system would be slower but equally secure. That property —
security independent of the index's freshness — is the whole design.

Chunks derived from non-record sources (uploaded policy PDFs, manuals) carry an
explicit `visibility` tier and an owning group; stage 2 checks group membership for
those instead of record ids.

### 2. Structured questions use typed tool-calling, never generated SQL

"Which invoices are overdue?" is not a retrieval problem — it is an aggregation over
live data, and a vector index cannot answer it correctly. The model therefore gets a
**closed set of typed tools** that compile to Odoo ORM calls:

| Tool | Compiles to |
| --- | --- |
| `find_records(model, filters, fields, limit)` | `search_read` with a **server-validated** domain |
| `aggregate(model, group_by, measures, filters)` | `read_group` |
| `stock_levels(product_query, location?)` | `stock.quant` aggregation |
| `overdue_invoices(partner?, as_of)` | `account.move` with a fixed domain shape |
| `customer_360(partner_id)` | Composite read across partner, SO, invoice, CRM |

Rules enforced on our side of the boundary:

- The model **never emits SQL and never emits a raw Odoo domain**. It emits
  structured filter objects (`{"field": "amount_total", "op": ">=", "value": 1000}`)
  which our code compiles into a domain after validating field names, operators, and
  types against a per-model allow-list.
- Every tool executes **as the acting user**, so record rules apply. Same authority,
  same guarantee as stage 2.
- Every tool is read-only in 1.0. Write operations (create a lead, confirm an order)
  are a post-1.0 milestone with explicit human confirmation, and are called out in
  the roadmap rather than smuggled in.
- Result sets are capped and token-budgeted before entering the prompt.

### 3. The transport: an addon-owned REST controller

`atlas-api` calls back into Odoo over HTTP endpoints exposed by the `odoo_atlas`
addon, not over raw XML-RPC and not over direct SQL.

- Service-to-service authentication: a shared secret (`ATLAS_SERVICE_TOKEN`),
  constant-time compared, plus network isolation to the compose network.
- The **acting user** is propagated as a signed, short-lived context token minted by
  the addon when the user opens a conversation. The service cannot impersonate an
  arbitrary user; it can only replay a token Odoo itself issued.
- The controller executes reads in `request.env` under **that user's** id — never
  `sudo()`. `sudo()` in the Atlas request path is a CI-checked prohibition.
- Every call is written to an audit log (`atlas.access.log`): who asked, which
  models, which record ids, which were denied.

## Consequences

**Easier**

- The security story is one sentence a CISO accepts: *"the assistant can never
  surface a record the user could not open in the Odoo UI."*
- Record-rule changes take effect immediately — no re-index, no cache invalidation.
- Multi-company isolation comes free, because it is an Odoo record rule.
- Structured answers are *correct* (live aggregation) rather than *plausible*
  (embedded snapshot).
- The audit log makes the system defensible under review and satisfies GDPR-style
  "what did it access" questions.

**Harder**

- **Latency.** Stage 2 adds an Odoo round-trip per query, grouped by model.
  Budgeted at 20–60 ms against a multi-second LLM call. Mitigated by batching ids
  per model into a single `search`, and by a short-TTL per-request cache. Measured
  in M13.
- **Over-fetching.** The pre-filter must return more candidates than we need,
  because we do not know the denial rate in advance. We over-fetch `k * 4` and
  degrade gracefully; adaptive over-fetch based on observed denial rate is an M13
  refinement.
- **The index still contains data the user cannot see.** Anyone with database access
  bypasses this design entirely. Accepted and documented: `atlas.chunks` must be
  treated with the same sensitivity as the Odoo database itself, and the deployment
  guide says so. Chunk *text* redaction for PII is a separate concern handled in M13.
- **Tool coverage is finite.** Questions outside the tool set fall back to semantic
  retrieval, which may answer vaguely. Better than answering confidently and wrongly;
  the router (M10) is required to say when it cannot answer.
- **Two authorization paths to test** (retrieval post-filter, tool execution). Both
  get dedicated negative-path integration tests in M6 and M9 — the test suite
  explicitly asserts that a restricted user *cannot* retrieve a restricted record.

## Alternatives considered

**Query the Odoo PostgreSQL tables directly with SQL.** By far the fastest option,
and the reason so many "Odoo AI" projects do it. **Rejected as a security defect.**
Record rules exist only in the ORM layer; raw SQL sees every row. It also breaks on
computed and related fields, on translated values (`jsonb` in modern Odoo), and on
the many-to-many join tables whose semantics live in Python. It would make the
assistant a privilege-escalation tool.

**Bake permissions into the index at ingestion time** (store allowed group/user ids
per chunk, filter on them). Rejected: it is a snapshot of a dynamic evaluation.
Every change to a record rule, a user's groups, a salesperson assignment, or a
company assignment silently invalidates it, and the failure mode is silent
over-disclosure. It is also unable to express rules that reference arbitrary related
fields. We keep a *coarse* version of this idea (`company_id`, `visibility`) as a
performance pre-filter only, explicitly labelled non-authoritative.

**Index per user / per role.** Rejected: storage and ingestion cost multiply by the
number of distinct permission sets, which in a real Odoo instance approaches the
number of users.

**Text-to-SQL with a read-only role and row-level security.** Rejected: RLS would
have to duplicate Odoo's record rules in PostgreSQL, which is a second
implementation of the security model that must be kept in sync forever — the exact
failure mode we are avoiding. Generated SQL is also unbounded in cost and hard to
validate; a mis-generated join can table-scan a production ERP.

**XML-RPC with the end user's own credentials.** Genuinely secure and it was a close
second. Rejected because it requires the service to hold user passwords or API keys,
which is a far worse secret-management problem than one service token, and because
XML-RPC gives us no place to put audit logging, batching, or payload shaping. Our
controller approach keeps the same security property without holding user
credentials.
