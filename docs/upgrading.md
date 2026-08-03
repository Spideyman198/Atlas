# Upgrading

What 1.0 promises, what it does not, and how to move between versions.

## The compatibility promise

From 1.0.0, these are covered by semantic versioning. A breaking change to any
of them requires a major version.

| Surface | Covered |
| --- | --- |
| Engine HTTP API | Request and response shapes, status codes, event names |
| Callback API | The endpoints the engine calls on Odoo |
| Configuration | `ATLAS_*` variable names and their meanings |
| Odoo models | `atlas.conversation`, `atlas.message`, `atlas.message.citation` field names and types |
| Security groups | `group_atlas_user`, `group_atlas_manager`, `group_atlas_ingest` |
| Database schema | Through migrations, not directly |

**Not covered**, and deliberately so:

- **Python module paths inside `atlas.*`.** The engine is deployed as a
  container, not imported as a library. Nothing is published to PyPI precisely
  so that this is not an interface promise.
- **Prompt wording.** Prompts are versioned by content hash and change whenever
  a measurement says they should. The *behaviour* they produce — grounding,
  citing, refusing — is covered; the sentences are not.
- **Retrieval ranking.** A better ranking is not a breaking change. Answers to
  the same question may differ between versions.
- **Metric names and labels.** Covered by best effort. A renamed series will be
  called out in the changelog, but it will not force a major version.
- **Log formats.** Structured, and their fields may change.
- **Anything documented as internal**, meaning a leading underscore or a
  docstring that says so.

## Between patch versions

`1.0.0` to `1.0.1`. Security and bug fixes only.

```bash
alembic upgrade head
odoo -d production -u odoo_atlas --stop-after-init   # then restart Odoo
# roll the engine and worker
```

No configuration changes, no re-indexing.

## Between minor versions

`1.0.x` to `1.1.0`. New capability, nothing removed.

Same procedure, plus: read the changelog for new configuration variables. New
ones always have defaults that preserve existing behaviour — a minor release
that changes what an existing deployment does without being asked is a breaking
release wearing the wrong number.

## Between major versions

`1.x` to `2.0`. Read the release notes first; they will carry a migration
section rather than a bare list.

Two things that make a major upgrade harder than it looks, both worth planning
for:

**A changed embedding model means a full re-index.** Vectors from two models do
not share a space, and mixing them produces retrieval that is subtly wrong
rather than obviously broken. `readyz` refuses to start when the schema's
declared width does not match the configured model, which catches the change in
dimensions but not a same-width model swap. If the notes say the model changed:

```bash
curl -XPOST http://atlas-api:8000/v1/ingest/sync -d '{"kind":"reindex"}'
```

That re-embeds the whole corpus at the provider's per-token rate. Budget for it.

**Migrations are forward-only in practice.** Downgrades exist and are tested
([ADR-0008](adr/0008-migrations.md)), but restoring a backup is the safer
rollback for anything beyond a patch.

## Deprecation policy

A covered surface is never removed without notice.

1. It is marked deprecated in a minor release, with the replacement named in the
   changelog and a warning logged at runtime where that is possible.
2. It keeps working for at least one further minor release.
3. It is removed in the next major.

The minimum is one minor release of overlap. In practice a widely used surface
gets longer, and the changelog says which.

## Rolling back

Downgrading the engine and the addon is straightforward; downgrading the
*schema* is where it goes wrong.

For a patch or minor version, if no migration ran:

```bash
# roll the engine and worker back to the previous tag
odoo -d production -u odoo_atlas --stop-after-init   # with the previous addon
```

If a migration ran, restore the Atlas database from backup and roll back
together. Odoo's database is untouched by an Atlas rollback — Atlas never writes
to it.

The vector index is not a cache. Rebuilding it means re-embedding the corpus and
paying for it again, so it belongs in the backup schedule alongside Odoo's own
database.

## Checking a version

```bash
curl -fsS http://atlas-api:8000/healthz
```

```json
{"status": "ok", "service": "atlas-api", "version": "1.0.0"}
```

The Odoo addon carries the same version with the series in front —
`19.0.1.0.0` — and CI fails when the two disagree. If a deployment reports
mismatched versions, the engine and the addon came from different releases and
the callback contract between them is not guaranteed.
