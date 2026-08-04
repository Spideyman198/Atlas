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

## The authorization boundary

The addon's `controllers/atlas_api.py` is where the engine asks Odoo what the
person who asked a question may see. The protocol is documented in
[api.md](api.md); what matters here is the shape of the rule it enforces.

**No `sudo()`, anywhere in the addon.** Not on the request path, not in the
models, not for configuration. There is no allow-list, because the moment one
exists the rule becomes a judgement call and the reason to trust it goes away.
`addons/odoo_atlas/tests/test_no_sudo.py` scans the source and fails the build,
so this is a property rather than a convention.

That rule is why Atlas configuration comes from the environment. Reading
`ir.config_parameter` needs system rights, and the code that reads it runs as
whichever user asked a question — so a config parameter would have forced the
exception the rule exists to avoid. The secrets are better off out of the
database anyway.

**Two secrets, and the split is load-bearing.** `ATLAS_SERVICE_TOKEN` proves a
call came from the engine. `ATLAS_CONTEXT_SECRET` signs the short-lived tokens
naming the acting user, and is never given to the engine. If the engine held it,
it could mint a token for any user it liked and Odoo would believe it, which
would make the whole authorization story decorative.

On the engine's side, the property is held up by types. `CandidateChunk` comes
out of retrieval; `AuthorizedChunk` is what the prompt assembler will accept; and
the only thing that converts one to the other is
`atlas.application.authorization.AuthorizationFilter`. Skipping stage 2 is not a
step somebody could forget — under `mypy --strict` it does not compile.

The filter fails closed on everything, including exception types that do not
exist yet:

```python
except AtlasError as exc:
    raise AuthorizationError("could not confirm access with Odoo") from exc
except Exception as exc:      # deliberately broad
    raise AuthorizationError("could not confirm access with Odoo") from exc
```

An unreachable Odoo, a slow one, or one returning nonsense all produce a refused
answer. None of them produces an unfiltered one. `/readyz` gates on Odoo for the
same reason: with it down the engine can clear no candidates, so every answer it
could give would be a refusal.

## Ingestion

The cold path, documented in full in [ingestion.md](ingestion.md). Three things
are worth knowing before reading the code.

**The hash check comes before the embedding call.** That ordering is the whole
economics of the feature, and `test_sync_source.py` asserts it directly: a sync
with nothing changed makes zero provider calls. Moving that check would pass
every other test in the suite.

**LlamaIndex lives in exactly one package.** `atlas.infrastructure.llamaindex`
supplies the sentence splitter and the file readers, and an `import-linter`
contract fails the build if it is imported anywhere else. The containment is
falsifiable, not asserted: delete the dependency and `test_document_loader.py` is
the only thing that fails.

**Ingestion reads as a different user from queries.** `SourceReader` goes to
`/atlas/api/ingest/*` as the integration user; `OdooGateway` goes to
`/atlas/api/*` as the person who asked. Two doors, so neither can be mistaken for
the other — and the index being wider than any answer is exactly why the
query-time check cannot be skipped.

## Retrieval

Documented in full in [retrieval.md](retrieval.md). Three things before reading
the code.

**The pipeline's order is the security model.** `RetrievalPipeline.run` is
retrieve, authorize, assemble. The middle stage takes no configuration that
could switch it off, and `test_authorization_is_structural.py` runs `mypy` over
a fixture that tries to skip it — so the guarantee is falsifiable, and stays so
if somebody later widens the assembler's signature to be helpful.

**Retrieval returns `CandidateChunk` on purpose.** The port's return type is
what makes authorization impossible to forget rather than merely easy to
remember. Nothing in `infrastructure/llamaindex` knows who is asking.

**`AtlasLlamaLLM` is not decoration.** Constructing LlamaIndex's fusion
retriever without an explicit LLM makes it resolve `Settings.llm` and reach for
its OpenAI integration — a second path to a vendor with its own retry policy
and cost meter, which is the failure ADR-0003 inverts the dependency to prevent.
Retrieval asks no model anything, so the bridge it gets refuses every call.

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

The chat panel is tested through a real browser. Google Chrome and
`websocket-client` provide it — Ubuntu's `chromium` package is a stub that
installs a snap and produces a binary that cannot start in a container.

Both are load-bearing in a way worth knowing about: **Odoo skips a browser test
when either is missing instead of failing it**. A suite full of tours reports
green having run none of them, which is exactly what happened the first time
these were written. `TestTheBrowserTestsCanRun` asserts both are present, so
that situation is a red build rather than a quiet one.

They live in a separate image target. `docker/odoo/Dockerfile` has two:

| Target | Contains | Built by | Architectures |
| --- | --- | --- | --- |
| `runtime` | Odoo and the bootstrap wrapper | the release workflow | amd64, arm64 |
| `test` | `runtime` plus Chrome and `websocket-client` | `docker-compose.yml` | amd64 |

Everything local goes through compose, so `make up`, `make test-odoo` and CI all
get `test` and all run the tours. Only the release workflow builds `runtime`,
and it names the target explicitly — the last stage in the file is `test`, so an
omitted target would publish the browser rather than leave it out.

The split is not a preference. Google ships Chrome for linux/amd64 and no other
Linux architecture, so installing it unconditionally made the arm64 half of the
published manifest unbuildable. Keeping a browser out of a deployment image is
worth doing regardless.

**Browser tours need amd64.** On arm64 hardware the `test` target cannot be
built, because no usable containerised browser exists for it on Ubuntu Noble.
The rest of the suite — `make check`, the engine tests, the addon's non-browser
tests — is unaffected.

### Troubleshooting: the addon suite reports zero tests

On Windows, run `make test-odoo` from **PowerShell** — `.\make.ps1 test-odoo` —
and not from Git Bash, MSYS or an MSYS-backed shell.

Git Bash rewrites arguments that look like absolute POSIX paths into Windows
paths before the process ever sees them. The test selector is `/odoo_atlas`,
which looks exactly like one, so Odoo receives:

```
--test-tags C:/Program Files/Git/odoo_atlas
```

Odoo does not treat an unusable tag as an error. It logs `Invalid tag`, matches
nothing, and exits **zero**:

```
ERROR odoo.tests.tag_selector: Invalid tag C:/Program Files/Git/odoo_atlas
WARNING odoo.tests.result: 0 failed, 0 error(s) of 0 tests
```

A green exit code, an installed module, and not one test executed. The same run
from PowerShell reports `0 failed, 0 error(s) of 174 tests`.

Check the count, not the exit code. `of 0 tests` is a failed run whatever the
shell told you. If Git Bash is the only shell available, `MSYS_NO_PATHCONV=1`
disables the rewriting for one command.

This is the third form of the same hazard on this page — a skipped browser test,
a stub too polite to fail, and now a mangled selector. Nothing here treats "the
command exited zero" as evidence that anything ran.

## Adding a port and an adapter

1. Define the `Protocol` in `atlas/domain/ports/`, using domain types only.
2. Write the use case in `atlas/application/`, taking the port as a constructor
   argument.
3. Implement the adapter in `atlas/infrastructure/<area>/`.
4. Bind it in the composition root, `atlas/config/container.py`.
5. Add the adapter to the shared contract test suite for that port.

If step 4 is the only place the concrete class is named, the layering is right.

### Test doubles must be faithful to the wire, not to the happy path

A stub stands in for an SDK, and it is only worth what it reproduces. If it hands
the adapter a finished object, it cannot show whether the adapter assembles one —
it tests the assertion, not the code.

This is not hypothetical. The OpenAI adapter's `stream()` never emitted tool
calls at all: streamed answers to any question needing a tool came back empty,
on OpenAI, on Azure and on every compatible endpoint. The suite was green
throughout, because `_openai_stream` replayed a stream containing no tool calls
to reassemble. The bug reached a stable release and was found by asking a live
provider a question, not by a test. Fixed in
[1.0.1](../CHANGELOG.md); the stub now fragments calls the way the wire does.

So, for any adapter over a network protocol:

- **Reproduce the delivery, not just the payload.** Streamed data arrives in
  pieces, in an order the protocol chooses, sometimes split mid-token. Replay it
  that way. OpenAI splits one tool call across many chunks and keys the parts by
  `index`; a double that skips the splitting proves nothing about reassembly.
- **Cover the dialects, not only the reference implementation.** One adapter
  serves several hosts. Fields the reference vendor always populates are optional
  elsewhere: Google's compatibility endpoint omits `index` on both streamed tool
  calls and embedding items, and each omission was a distinct crash or silent
  drop. Where a field is optional in practice, type it optional in the double and
  test both.
- **Exercise every branch the port exposes.** A provider that streams *and*
  returns tool calls needs a test for the two together, not one each.
- **Verify against the real service before release.** A recorded response or a
  scripted call is enough. The two 1.0.1 defects were both visible in the first
  live request and in no test.

New provider adapters are held to this before they ship, and the regression tests
in `tests/unit/test_openai_provider.py` stay as the worked example.

## Releasing

Releases are automatic. Push a `fix:` or `feat:` commit to `main`; CI runs, and
on success the Release workflow determines the version from the commit prefixes,
writes it, tags it, publishes both images and creates the GitHub release.

Two things are written by hand and two by the automation, and mixing them up
breaks the release:

| | Owner |
| --- | --- |
| `CHANGELOG.md` `[Unreleased]` section | you, before the release |
| `services/atlas/pyproject.toml` version | semantic-release |
| `addons/odoo_atlas/__manifest__.py` version | `scripts/check_versions.py --write` |
| Tag, GitHub release, images | the workflow |

Write the entry under `## [Unreleased]`. That heading is what the release job
lifts into the GitHub release notes, so an entry filed under a version number
publishes a release whose body reads "Nothing yet." Rename the heading to the
version after the tag exists.

**Never bump the version by hand.** The release job decides whether it ran by
checking `git diff --quiet` after semantic-release writes the version. A version
already committed at the value semantic-release computes produces no diff, so
`released=false`, and the tag, the images and the release are all silently
skipped.

### Traps this pipeline has already hit

Each of these produced a green or absent build rather than an obvious failure.

- **A revert of a release commit stops CI.** semantic-release commits
  `chore(release): X.Y.Z [skip ci]`, and `git revert` copies that subject into
  its own message verbatim. GitHub reads `[skip ci]` on the pushed head commit
  and creates no runs for any workflow — CI never starts, so the Release
  workflow never gets its `workflow_run` event. Amend the message before pushing.
- **Detached HEAD stops semantic-release.** It matches the current branch name
  against `[tool.semantic_release.branches.main]`, and checking out a SHA leaves
  no name to match. The workflow re-attaches the validated commit to a local
  `main` before running it.
- **`github.repository` is not a valid image path.** It keeps the account's
  capitalisation, and OCI repository names must be lowercase. The workflow
  lowercases it into a step output.
- **The Odoo image's last stage is `test`.** An omitted build target publishes
  the browser image and fails on arm64. The workflow names `runtime`.
