# API reference

Every HTTP endpoint in Atlas, on both sides of the boundary.

Three groups, and which one an endpoint belongs to determines how it
authenticates:

| Group | Served by | Called by | Authenticates with |
| --- | --- | --- | --- |
| Engine API | `atlas-api` | Odoo, operators | Nothing, or a context token in the body |
| Callback API | Odoo addon | `atlas-api` only | Service token, plus a context token |
| Panel API | Odoo addon | The browser | Odoo session cookie and CSRF token |

The callback API is the interesting one and has its own document:
[api.md](api.md) explains why it exists and what each call guarantees. This page
is the reference.

A generated OpenAPI schema for the engine is at `/openapi.json`, with Swagger at
`/docs`. `/metrics` is deliberately absent from both: it is scraped by a
collector, not called by a client, and listing it as an API invites somebody to
build on the format.

---

## Engine API

Served by `atlas-api`. Nothing here trusts the caller's claim about who they
are — the context token is minted and signed by Odoo.

### `GET /healthz`

Liveness. Touches nothing external.

```json
{"status": "ok", "service": "atlas-api", "version": "0.1.0"}
```

Always `200` while the process is alive. Wiring this to the database would turn
a brief outage into a rolling restart across every replica.

### `GET /readyz`

Readiness. Verifies the database, the pgvector version, the schema's embedding
width, and that Odoo answers and accepts the service token.

```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "pgvector": "ok (0.8.6)",
    "schema": "ok (1536-d)",
    "odoo": "ok (production)"
  }
}
```

`200` when ready, `503` otherwise with the same shape — each check names its own
state, so a failure says which dependency is at fault rather than that
"something" is.

### `GET /metrics`

Prometheus exposition format. Returns `404` when
`ATLAS_OBSERVABILITY__METRICS_ENABLED` is false.

Series are labelled by outcome, intent, stage, tool, provider, model and kind
only. Nothing is labelled by user, conversation or question — those multiply
series without bound, and scraping must not be a way to learn what anybody
asked.

### `POST /v1/chat`

Answer a question, streamed.

```json
{
  "question": "which invoices are overdue?",
  "context_token": "v1....",
  "history": [{"question": "...", "answer": "..."}],
  "conversation_id": 12,
  "intent": "semantic"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `question` | yes | 1–4000 characters |
| `context_token` | yes | Minted by Odoo. Opaque here; the engine never inspects it |
| `history` | no | Oldest first. Summarised when it stops fitting |
| `conversation_id` | no | The `atlas.conversation` this belongs to |
| `intent` | no | `structured`, `semantic`, `hybrid` or `refuse`. Omit to let the router decide |

Responds `text/event-stream`:

| Event | Payload |
| --- | --- |
| `delta` | `{"text": "..."}` — text as it is generated |
| `done` | the whole answer, below |
| `error` | `{"message": "..."}` |

```json
{
  "text": "Three invoices are overdue. [1]",
  "refused": false,
  "intent": "structured",
  "tools_called": ["overdue_invoices"],
  "prompt_version": "system@84298b4212b1",
  "trace_id": "9fb006b4...",
  "model": "claude-opus-5",
  "cost_usd": 0.0042,
  "usage": {"input_tokens": 1840, "output_tokens": 96, "total_tokens": 1936},
  "citations": [
    {"sequence": 1, "res_model": "account.move", "res_id": 117,
     "record_name": "INV/2026/0117", "snippet": "..."}
  ]
}
```

**A `200` does not mean it worked.** Once the first byte is out the status line
is gone, so a failure arrives as an `error` event. A client must read events.

Rate limited per context token. A refused request still returns `200` with an
`error` event and a `Retry-After` header — the panel parses one shape, and a
proxy still sees the conventional signal.

### `POST /v1/ingest/sync`

Queue an ingestion run. Returns `202`: the work is accepted, not done.

```json
{"sources": ["odoo.res.partner"], "kind": "incremental",
 "record_ids": [], "deleted_ids": []}
```

| `kind` | Reads |
| --- | --- |
| `incremental` | What changed since the watermark |
| `full_sync` | Everything, still skipping unchanged content by hash |
| `reindex` | Everything, re-embedding regardless — costs real money |

```json
{"queued": {"odoo.res.partner": 41}}
```

`record_ids` and `deleted_ids` apply to exactly one source.

### `GET /v1/ingest/sources`

The source registry, and which entries this Odoo can serve. Never `500`s because
Odoo blinked — an unreachable Odoo yields the registry with availability
unknown.

---

## Callback API

Served by the Odoo addon, called by `atlas-api` and nothing else. Every request
carries `Authorization: Bearer <service token>`; every request that acts for a
user also carries a context token in its body.

A bad service token, an expired context token and a user who lost their Atlas
group all produce the same refusal. The detail goes to the log, not the caller.

See [api.md](api.md) for the reasoning.

### `POST /atlas/api/status`

Confirms the addon is installed and the service token matches. Acts for nobody,
so it needs no context token — a probe that had to name a user would require the
engine to hold one.

```json
{"addon": "odoo_atlas", "version": "19.0", "database": "production"}
```

### `POST /atlas/api/authorize`

**The endpoint the whole design rests on.** Given candidate record ids per
model, returns the subset the acting user may read.

```json
{"context_token": "v1....", "records": {"sale.order": [41, 42, 43]}}
```

```json
{"allowed": {"sale.order": [41, 43]}}
```

Batched by model, not by record: one `search` per model turns forty round-trips
into three. Runs as the acting user, so Odoo's record rules decide.

### `POST /atlas/api/records`

Reads fields from records the acting user may see. Used to label citations.

### `POST /atlas/api/tool/catalog`

The tools this database can offer this user. A tool whose module is not
installed, or whose models the user cannot read, is omitted — so the model is
never told about a call that could only fail.

### `POST /atlas/api/tool/execute`

Runs one typed tool as the acting user.

```json
{"context_token": "v1....", "tool": "find_records",
 "arguments": {"model": "sale.order", "filters": [
   {"field": "state", "operator": "=", "value": "sale"}]}}
```

A rejected argument returns a message written to be read by the model that
produced it. See [tools.md](tools.md).

### `POST /atlas/api/ingest/sources`, `/records`, `/binary`

Read source definitions, record batches and attachment bytes for indexing. These
run as a dedicated integration user in the **Atlas / Ingest** group, not as an
end user: ingestion builds an index that is deliberately broader than any one
person's view, and authorization happens at query time instead.

---

## Panel API

Served by the addon, called by the chat panel in the browser.

### `POST /atlas/chat/ask`

Form-encoded — `payload` (a JSON string) and `csrf_token` — because Odoo's CSRF
check reads form parameters, not JSON bodies.

```
payload={"question":"which invoices are overdue?","conversation_id":12}
```

Responds `text/event-stream`, relaying the engine's events with one addition:

| Event | Payload |
| --- | --- |
| `open` | `{"conversation_id": 12}` — a new conversation has no id until now |
| `delta`, `done`, `error` | as the engine sends them, plus stored cost |

**The browser never holds a context token.** It sends a session cookie; Odoo
mints the token and calls the engine. A token is a bearer credential, and a page
holding one hands it to every script in that page.

---

## Model methods

Called over Odoo's ORM RPC by the panel, not over HTTP directly.

| Method | Model | Returns |
| --- | --- | --- |
| `atlas_suggestions()` | `atlas.conversation` | Starter questions this database can answer for this user |
| `atlas_transcript()` | `atlas.conversation` | Messages and citations, in one call |

---

## Status codes

| Code | Meaning |
| --- | --- |
| `200` | Handled. For a stream, check the events |
| `202` | Queued, not done |
| `400` | Malformed request |
| `403` | Refused — token, expiry or access, deliberately indistinguishable |
| `404` | No such route, or `/metrics` disabled |
| `422` | Request body failed validation |
| `503` | Not ready; `readyz` names the dependency |

## Stability

Before 1.0 these contracts may change between milestones. Breaking changes are
called out in the [changelog](../CHANGELOG.md).
