# Contributing to Odoo Atlas

Read this before opening your first pull request.

## Ground rules

1. Significant decisions need an ADR. If your change alters deployment topology, the
   security model, the database schema, a hard-to-remove dependency or a public
   contract, it ships with a new record in [`docs/adr/`](docs/adr/README.md).
   [ADR-0001](docs/adr/0001-record-architecture-decisions.md) defines what counts.
2. The layering is enforced by CI, not by review. `domain` imports nothing from the
   rest of `atlas`. `application` depends on ports, never on adapters. Nothing in
   `services/atlas` imports `odoo`. Nothing outside
   `atlas.infrastructure.llamaindex` imports `llama_index`. `import-linter` fails the
   build if you break any of these.
3. `sudo()` is prohibited in the Atlas request path. Every read performed on behalf
   of a user runs as that user, so Odoo's record rules apply. If you need `sudo()`,
   open an issue — it points at a design problem.
4. Tests are part of the change. Bug fixes come with a regression test. Features come
   with unit tests, and integration tests if they cross a boundary.

## Getting set up

See [docs/installation.md](docs/installation.md) for full instructions. In short:

```bash
git clone https://github.com/Spideyman198/Atlas.git
cd Atlas
make init
make up
make check
```

On Windows, use `.\make.ps1 <target>`.

The engine targets Python 3.12, the version in the runtime image. Newer interpreters
may lack wheels for parts of the stack. `make check` and `make test` run in a
container, so no local interpreter is required; if you do work outside one, use a
3.12 environment rather than loosening a version pin.

## Workflow

```
main ──┬── feat/M7-ingestion-pipeline
       ├── fix/citation-ordering
       └── docs/adr-0008-write-tools
```

- Branch from `main`. Name branches `<type>/<short-description>`, optionally prefixed
  with the milestone (`feat/M7-...`).
- Keep pull requests reviewable. A PR that touches forty files across three layers
  will be asked to split.
- Rebase rather than merge `main` into your branch.

### Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/). This is not
ceremony — M14's release automation derives the changelog and the version bump from
these prefixes.

```
<type>(<scope>): <imperative summary, no trailing period>
```

| Type | Use for |
| --- | --- |
| `feat` | New capability |
| `fix` | Bug fix |
| `refactor` | Behaviour-preserving restructuring |
| `perf` | Performance work, with numbers in the body |
| `test` | Tests only |
| `docs` | Documentation, including ADRs |
| `build` | Docker, dependencies, packaging |
| `ci` | Workflows and automation |
| `chore` | Housekeeping with no production impact |

Scopes track the architecture: `addon`, `core`, `rag`, `retrieval`, `providers`,
`store`, `ingest`, `ui`, `docs`, `ci`.

Breaking changes carry a `!` after the scope and a `BREAKING CHANGE:` footer.

Examples:

```
feat(retrieval): add reciprocal rank fusion over dense and lexical results
fix(addon): scope conversation record rule to the owning user
perf(store): batch authorization lookups per model, 41ms -> 12ms p95
docs(adr): record decision to run the engine as a sidecar service
```

### Pull request checklist

- [ ] `make lint` and `make type` are clean (`ruff`, `mypy --strict`, `import-linter`)
- [ ] `make test` passes; new code is covered
- [ ] An ADR is included if the change is architecturally significant
- [ ] Public functions and all modules have docstrings explaining *why*, not *what*
- [ ] No secrets, no API keys, no customer data — including in test fixtures
- [ ] `CHANGELOG.md` updated under `[Unreleased]` for user-visible changes
- [ ] Odoo XML changes are validated against the correct Odoo 19 view schema

## Code standards

### Python

- Formatted and linted by `ruff`; configuration in the root `pyproject.toml` is
  authoritative. Do not hand-format.
- `mypy --strict`. `Any` requires a comment justifying it. `# type: ignore` requires
  a specific error code and a reason.
- Full type annotations on every public signature.
- Docstrings explain intent and trade-offs. A docstring restating the function name
  is noise; delete it.
- Errors are typed exceptions from our taxonomy, never bare `Exception`.
- No I/O in `domain`. None. Not even logging.

### Odoo addon

- Follow the [Odoo coding guidelines](https://www.odoo.com/documentation/19.0/contributing/development/coding_guidelines.html).
- Model fields declared in a consistent order: fields, compute methods, constraints,
  CRUD overrides, action methods, business methods.
- Every model gets an `ir.model.access.csv` entry and a record rule where row-level
  scoping applies. A model with no access rule is a review blocker.
- XML view ids follow `<model_snake_case>_view_<type>`; actions
  `<model_snake_case>_action`.
- Never `sudo()` in the Atlas request path (see ground rule 3).

### Testing

| Kind | Location | Speed | Rule |
| --- | --- | --- | --- |
| Unit | `services/atlas/tests/unit` | milliseconds | No network, no database, no API key |
| Contract | `services/atlas/tests/contract` | milliseconds | Every port adapter passes the same suite |
| Integration | `services/atlas/tests/integration` | seconds | Throwaway PostgreSQL container |
| Odoo | `addons/odoo_atlas/tests` | seconds | `TransactionCase` / `HttpCase` |
| Evaluation | `evaluation/` | minutes | Retrieval quality metrics, gated in CI |

Security-relevant behaviour is tested from the **negative** direction: assert that a
restricted user *cannot* see a restricted record. A test that only proves the happy
path proves nothing about authorization.

## Reporting bugs

Open an issue with: what you expected, what happened, the `trace_id` from the failing
message if you have one, your Odoo and Atlas versions, and the smallest reproduction
you can manage. Redact customer data.

## Security issues

**Do not open a public issue.** Report privately via the repository's security
advisory page. Include reproduction steps and impact assessment; you will get an
acknowledgement within 72 hours.

## Licensing of contributions

Contributions are accepted under **LGPL-3.0-or-later**, the project's licence —
inbound equals outbound. There is no CLA. By opening a pull request you confirm you
have the right to license the code that way. See
[ADR-0007](docs/adr/0007-licensing.md).
