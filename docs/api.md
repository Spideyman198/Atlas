# The Odoo callback API

Four endpoints, served by the `odoo_atlas` addon, called by the `atlas-api`
engine. They are how the engine asks Odoo what the person who asked a question is
allowed to see.

This is an **internal** API. It is not for browsers, it is not for end users, and
it must not be reachable from outside the network the two services share.

## Why it exists

The vector index is deliberately broader than any single user's view — ingestion
reads as an integration user so it can see everything worth indexing. What stops
that index leaking is that nothing reaches a prompt until Odoo has confirmed, in
this request and as this user, that they may read it
([ADR-0006](adr/0006-data-access-and-authorization.md)).

So the engine does not decide anything. It asks.

```
engine                                    Odoo (odoo_atlas)
  │
  │  POST /atlas/api/authorize
  │  Authorization: Bearer <service token>
  │  X-Odoo-Database: <database>
  │  { context_token, trace_id, records }
  ├───────────────────────────────────────▶
  │                                        verify service token
  │                                        verify context token signature
  │                                        switch env to the acting user
  │                                        search([('id','in',ids)])  ← record rules
  │                                        write atlas.access.log
  ◀───────────────────────────────────────┤
  │  { granted: { model: [ids] } }
```

## Authentication

Two secrets, doing two different jobs. Both come from the **environment** of the
process that needs them, never from `ir.config_parameter`: a secret in the
database is readable by every system administrator and travels in every backup.

| | Held by | Purpose |
| --- | --- | --- |
| `ATLAS_SERVICE_TOKEN` | Odoo **and** the engine | Proves a call came from the engine |
| `ATLAS_CONTEXT_SECRET` | Odoo alone | Signs the tokens naming the acting user |

The split is the point. If the engine held the signing key it could mint a token
for any user it liked, and Odoo would believe it — which would make the whole
authorization story decorative. It holds the shared token and nothing else, so
the most it can do is replay a context Odoo itself issued.

**If either secret is unset, every call is refused.** Treating "no token
configured" as "no check needed" would turn a forgotten environment variable into
an open door onto the ERP.

### The service token

Sent as `Authorization: Bearer <token>`, compared in constant time. Checked
before anything else, so an unauthenticated caller cannot make Odoo do work — not
even signature verification — by sending rubbish.

### The user context token

Minted by the addon immediately before it calls the engine, and travelling only
on that call. Nothing exposes minting over RPC; no browser ever sees one.

```
v1.<base64url payload>.<hmac-sha256 hex>

payload: {"uid": 7, "cid": [1], "exp": 1767225600}
```

Signed, not encrypted, and deliberately readable: it carries a user id, a company
list and an expiry, and none of those is a secret. Its integrity is what matters.

Verification, in order:

1. Shape and version. A `v2` token is rejected rather than reinterpreted.
2. Signature, compared in constant time.
3. Expiry. Default lifetime 900 seconds (`ATLAS_CONTEXT_TOKEN_TTL`).
4. **The user, re-read from the database.** Archived, deleted, or no longer in the
   Atlas group means refused — so revoking someone's access takes effect on their
   next request, not whenever their token happens to lapse.
5. **Companies, intersected with what the user still has.** A token cannot widen
   its own scope after the fact. The result becomes `allowed_company_ids` for the
   request, which is what multi-company record rules read.

Every refusal — bad service token, forged signature, expired token, revoked user
— produces the same response. Telling a caller *which* part of their attempt was
nearly right is telling a forger where to aim; the detail goes to the log.

## Choosing a database

These calls carry no session, so Odoo cannot infer which database to use. The
engine names it in the `X-Odoo-Database` header (`ATLAS_ODOO__DATABASE`). A
server hosting exactly one database will resolve without it; one hosting several
will not.

## Endpoints

All four are `POST`, take and return `application/json`, and expect
`context_token` and an optional `trace_id` in the body — except `/status`, which
acts for nobody.

### `POST /atlas/api/status`

Confirms the addon is installed and the service token is right. The engine's
readiness probe calls this; it needs no context token, which is the only reason
the engine can run it at all.

```json
{"addon": "odoo_atlas", "version": "19.0", "database": "odoo", "tools": []}
```

### `POST /atlas/api/authorize`

Stage 2 of retrieval. Batched by model: a question can retrieve forty candidates
across four models, and forty round-trips would cost more than the retrieval did.

```json
{
  "context_token": "v1.…",
  "trace_id": "0f9c…",
  "records": {"sale.order": [12, 13, 14], "res.partner": [7]}
}
```

```json
{"granted": {"sale.order": [12, 14], "res.partner": []}}
```

Every model asked about appears in the answer, so a caller can tell *asked and
refused* from *never asked*. Ids that are absent are denied, and there is no way
to ask why.

Two kinds of refusal, both answered the same way. A record the user may not read
is filtered out by Odoo's record rules. A **model** the user cannot touch at all
raises inside the controller and yields an empty list — not an error, because the
engine asked a legitimate question and the answer is "none of them".

Archived records still count as readable. Archiving is not a permission, and
treating it as one would quietly drop citations to closed orders.

Limits: 32 models per call, 500 ids per model. Exceeding either is a `400`.

### `POST /atlas/api/records`

Reads named fields of the records the acting user may see — enough to put a name
beside a citation.

```json
{"context_token": "v1.…", "model": "sale.order", "ids": [12, 13], "fields": ["name"]}
```

```json
{"records": [{"id": 12, "name": "S00012"}]}
```

Records the user may not read are simply absent. `fields` defaults to
`display_name` alone: defaulting to everything would ship binary columns and
every private note a model happens to carry into the engine's memory, for a
caller that asked for none of it.

### `POST /atlas/api/tool/execute`

Runs one typed tool inside Odoo, as the acting user.

```json
{"context_token": "v1.…", "tool": "find_records", "arguments": {"model": "sale.order"}}
```

**The tool set arrives in M9.** What exists now is the boundary the tools will run
behind: the same authentication, the same acting-user environment, the same audit
row. Adding a tool then means adding an entry to a registry
(`addons/odoo_atlas/services/tools.py`), never adding another route — which is
what keeps the number of ways into Odoo at one. An unregistered name is a `404`.

## Status codes

| Code | Meaning |
| --- | --- |
| `200` | Answered. An empty grant is a `200`, not an error. |
| `400` | The body is malformed, or a limit was exceeded. |
| `401` | The service token is wrong, missing, or unconfigured. |
| `403` | The context token is invalid, expired, or names a user who may not use Atlas. |
| `404` | No such tool. |

The engine maps these onto its own error taxonomy. `401` and `403` become
`AuthorizationError` — Odoo understood and declined, and retrying would only be
declined again. Everything else that goes wrong, including an unreachable Odoo,
becomes `DependencyUnavailableError`.

## Failing closed

`atlas.application.authorization.AuthorizationFilter` collapses **every** failure
into `AuthorizationError` and no chunks. An Odoo that is down, slow, or returning
nonsense produces a refused answer, never an unfiltered one. The catch is
deliberately broad: an exception type nobody anticipated escaping uncaught would
be a leak waiting to happen.

The engine's `/readyz` reports `odoo` as a gating check for the same reason. With
Odoo unreachable the engine can retrieve candidates and clear none of them, so
every answer it could give would be a refusal — reporting not-ready keeps it out
of rotation until that stops being true.

## No `sudo()`

Every read these endpoints cause runs as the acting user. There is no `sudo()`
anywhere in the addon, no allow-list, and no exception — including for
configuration, which is why the secrets and the engine's address come from the
environment rather than from `ir.config_parameter`.

This is enforced by a test that scans the addon's source
(`addons/odoo_atlas/tests/test_no_sudo.py`), so adding one fails the build rather
than the review.

## The audit log

Every call writes an `atlas.access.log` row: who acted, which model, how many ids
were asked about, how many were granted, a sample of those refused, the
`trace_id`, and how long it took. Visible under **Atlas → Technical → Access
Log**.

The row is written **as the acting user**, and the user and company come from the
environment rather than from the request, so a caller cannot attribute its own
access to somebody else. Nothing is caught around that write: an access that
could not be audited is exactly what the log exists to make impossible, so
failing to record one fails the request that caused it.

Append-only through the ORM — no group has write access. Users see their own
entries; administrators see every entry in their allowed companies and may delete
old ones, which is how retention is handled until it is automated.

## Configuration

Odoo's side (all read from the server's environment):

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_SERVICE_TOKEN` | — | Required. Shared with the engine. |
| `ATLAS_CONTEXT_SECRET` | — | Required. Never given to the engine. |
| `ATLAS_ENGINE_URL` | `http://atlas-api:8000` | Where the engine is. |
| `ATLAS_ENGINE_TIMEOUT` | `60` | Seconds Odoo waits for it. |
| `ATLAS_CONTEXT_TOKEN_TTL` | `900` | Context token lifetime. |

The engine's side:

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_ODOO__SERVICE_TOKEN` | — | Required; the engine refuses to start without it. |
| `ATLAS_ODOO__BASE_URL` | `http://odoo:8069` | Odoo's origin. |
| `ATLAS_ODOO__DATABASE` | `odoo` | Sent as `X-Odoo-Database`. |
| `ATLAS_ODOO__TIMEOUT_SECONDS` | `10` | Hard ceiling on one call. |
| `ATLAS_ODOO__MAX_IDS_PER_CALL` | `500` | Matches the addon's own limit. |

**Settings → Atlas** shows all of it read-only, with a *Test Connection* button.
Nothing there is editable: a form that silently lost its value on the next
redeploy would be worse than no form.
