# Developer Guide

How the engine and the addon are put together, and how to work on them. For
setup, see [installation.md](installation.md). For why the system is shaped this
way, see [the decision records](adr/README.md).

## Layers

```
src/atlas/
├── domain/          entities, value objects, ports, errors. No I/O.
├── application/     use cases. Depends on ports, never on adapters.
├── infrastructure/  adapters: providers, persistence, odoo, llamaindex
├── interfaces/      FastAPI routers, CLI entrypoints
└── config/          settings, logging, composition root
```

`config` is the only module that knows which concrete adapter satisfies which port.
Everything else receives its collaborators by injection, which is what lets the
whole suite run with fakes and no network.

## The rules, and how they are enforced

Four `import-linter` contracts, defined in the root `pyproject.toml` and run by
`make imports`:

| Contract | Effect |
| --- | --- |
| Domain is independent | `atlas.domain` imports nothing else from `atlas` |
| Application depends on ports | `atlas.application` cannot import `infrastructure`, `interfaces` or `config` |
| The engine never imports odoo | ADR-0002: the engine reaches Odoo over HTTP |
| LlamaIndex is confined | ADR-0003: only `atlas.infrastructure.llamaindex` may import `llama_index` |

These are checked, not assumed. To convince yourself, add
`from atlas.config.settings import Settings` to any module under `atlas/domain/`
and run `make imports` — the first contract reports `BROKEN`.

`make check` runs ruff, mypy `--strict`, the contracts, and the tests — both the
engine's and the addon's. It is what CI runs.

## The addon

```
addons/odoo_atlas/
├── __manifest__.py   depends on base and web only
├── models/           atlas.conversation, atlas.message, atlas.message.citation,
│                     and the res.config.settings extension
├── security/         the two groups, ir.model.access.csv, the record rules
├── views/            list, form and search views, menus, window actions
├── tests/            TransactionCase suites, run by Odoo, not pytest
└── text.py           helpers with no Odoo import
```

The addon is a thin adapter and holds no AI code
([ADR-0002](adr/0002-sidecar-service-topology.md)). It is subject to Odoo's
conventions rather than the engine's, so the root `pyproject.toml` relaxes
several lint rules under `addons/**`; `mypy` does not cover it at all, because
the ORM's metaclass machinery makes strict typing there more noise than signal.

Two groups: **Atlas / User: Own Conversations** and **Atlas / Administrator**,
the second implying the first. Neither is implied by `base.group_user` — a
question costs money to answer, so access is granted per user rather than to
every employee.

Three record rules per model, and the split matters:

| Rule | Groups | Domain |
| --- | --- | --- |
| Ownership | Atlas user | `[('user_id', '=', user.id)]` |
| Administrator | Atlas administrator | `[(1, '=', 1)]` |
| Multi-company | *none — global* | `[('company_id', 'in', company_ids)]` |

Rules attached to different groups are ORed, so an administrator is not narrowed
by the ownership rule. A rule with no group is global and is ANDed with the rest,
so the company boundary binds administrators too.

`atlas.message` and `atlas.message.citation` carry their own stored `user_id` and
`company_id`, copied from the conversation. That is a denormalisation for the
benefit of the record rules: every one of them is then a comparison against an
indexed column rather than a join back to `atlas_conversation`. The engine makes
the same trade for the same reason — `chunks` carries a copy of its document's
company and visibility so the retrieval pre-filter stays a single index scan
([data architecture](architecture/02-data-architecture.md)).

A conversation cannot change owner, not even for an administrator. Its answers
were computed under one user's access rights
([ADR-0006](adr/0006-data-access-and-authorization.md)), so handing it to a
second user would show them results assembled from records they may not read.
The record rules stop a user reaching into someone else's conversation;
`atlas.conversation.write` stops the reverse.

## Configuration

Settings come from `ATLAS_*` environment variables and are validated once at
startup. Nested groups use a double underscore:

```
ATLAS_LOG_LEVEL=DEBUG
ATLAS_LOG_JSON=false
ATLAS_DATABASE__URL=postgresql://atlas:atlas@postgres:5432/atlas
ATLAS_DATABASE__POOL_MAX_SIZE=32
```

Validation is fail-fast: a missing or malformed value stops the process at boot
rather than surfacing later as a confusing runtime error. Add new settings to
`atlas/config/settings.py`, grouped by concern rather than flattened.

`get_settings()` is cached per process. Tests that need different values call
`get_settings.cache_clear()`.

## Errors

Every deliberate failure is an `AtlasError` from `atlas/domain/errors.py`. Anything
else reaching a handler is a bug and is reported as one.

Domain errors carry no HTTP status. `atlas/interfaces/http/errors.py` maps them,
walking the class hierarchy, so a new subclass inherits a sensible status without
touching the table. Responses are RFC 9457 problem documents:

```json
{
  "type": "about:blank",
  "title": "AuthorizationError",
  "status": 403,
  "detail": "not your record",
  "code": "authorization_error",
  "trace_id": "9f2c..."
}
```

`code` is a public contract. Renaming one is a breaking change.

Unexpected exceptions return a generic 500 with the trace id and nothing else — the
message can contain internal detail, so it stays in the logs.

## Logging and trace ids

Logs are JSON, one object per line, on stdout. Every record carries the trace id of
the request that produced it.

```python
logger.info("ingested source", extra={"source": key, "chunks": len(chunks)})
```

Anything passed through `extra` is merged into the JSON payload. Keep secrets and
personal data out of it.

`TraceIdMiddleware` adopts an inbound `X-Request-ID` when the caller supplies one,
so a single id spans the Odoo addon and the engine, and mints one otherwise. It is
bound to a `ContextVar` and also stored on the ASGI scope.

Both are needed. Starlette installs `ServerErrorMiddleware` *outside* user
middleware, so when an unhandled exception propagates the `ContextVar` has already
been reset by the time the 500 handler runs. Error handlers therefore read the
scope, via `trace_id_for(request)`.

Uvicorn installs its own log handlers on import. `configure_logging` clears them and
lets those loggers propagate, so access logs are structured too.

## Tests

| Marker | Location | Rule |
| --- | --- | --- |
| `unit` | `tests/unit` | No network, no database, no API key |
| `contract` | `tests/contract` | Every adapter of a port passes the same suite |
| `integration` | `tests/integration` | Throwaway PostgreSQL container |

```bash
make test                                        # unit tests with coverage
docker compose --profile tools run --rm --no-deps atlas-tools pytest -k logging
```

Security-relevant behaviour is tested from the negative direction: assert that a
restricted user *cannot* see a restricted record. A test that only exercises the
happy path proves nothing about access control.

Coverage has a floor that rises each milestone. Lowering it is a reviewable
decision.

### The addon's tests

The addon is not tested by pytest. Odoo models only exist inside a loaded
registry, so its tests are `TransactionCase` classes run by Odoo's own runner
against a database with the module installed:

```bash
make test-odoo
```

That target drops `odoo_atlas_test`, installs the addon into a database created
from nothing, and runs the suite. Starting from an empty database is the point:
a pass means the addon installs cleanly *and* its tests are green, never that it
still works on a database somebody has been editing by hand. Odoo exits non-zero
when a test fails, so it gates CI like any other job.

Coverage is not measured here. Odoo's runner has no coverage integration worth
the wiring, and the number would not be comparable to the engine's.

## Adding a port and an adapter

1. Define the `Protocol` in `atlas/domain/ports/`, using domain types only.
2. Write the use case in `atlas/application/`, taking the port as a constructor
   argument.
3. Implement the adapter in `atlas/infrastructure/<area>/`.
4. Bind it in the composition root, `atlas/config/container.py`.
5. Add the adapter to the shared contract test suite for that port.

If step 4 is the only place the concrete class is named, the layering is right.
