# Installation

Bring the full Odoo Atlas stack up locally. Everything runs in Docker — you do
**not** need a local Python interpreter, PostgreSQL, or Odoo installation.

## Prerequisites

| Requirement | Minimum | Check |
| --- | --- | --- |
| Docker Engine | 24 | `docker --version` |
| Docker Compose | v2.20 | `docker compose version` |
| Git | any recent | `git --version` |
| Free disk | ~6 GB | images + demo database |
| Free RAM | ~4 GB | Odoo and PostgreSQL are the consumers |

On Windows, Docker Desktop with the WSL 2 backend is strongly preferred over
Hyper-V — bind-mount performance for the source tree is dramatically better.

GNU `make` is **optional**. Windows users run `.\make.ps1 <target>`, which mirrors
every Makefile target.

## Quick start

```bash
git clone https://github.com/<your-account>/odoo-atlas.git
cd odoo-atlas
make init      # Windows:  .\make.ps1 init
make up        # Windows:  .\make.ps1 up
```

`make init` copies `.env.example` to `.env`. `make up` builds the images and
starts the stack in the background.

**First boot takes several minutes.** Odoo initialises its database and loads demo
data. Follow it with `make logs` and wait for:

```
[atlas-bootstrap] initialisation of 'odoo' complete
```

Then:

| Service | URL | Credentials |
| --- | --- | --- |
| Odoo | <http://localhost:8069> | `admin` / `admin` |
| Atlas engine — API docs | <http://127.0.0.1:8000/docs> | none |
| Atlas engine — liveness | <http://127.0.0.1:8000/healthz> | none |
| Atlas engine — readiness | <http://127.0.0.1:8000/readyz> | none |
| PostgreSQL | `127.0.0.1:5432` | from your `.env` |

## Verifying the installation

```bash
docker compose ps
```

All three services should report `running`, and `postgres` and `odoo` should
report `healthy`.

```bash
curl http://127.0.0.1:8000/readyz
```

Expected:

```json
{ "status": "ready", "checks": { "database": "ok", "pgvector": "ok (0.8.6)" } }
```

A `503` here means the engine started but a dependency is not usable — the
`checks` object names which one. That distinction is deliberate: see
[`docs/architecture/03-request-lifecycle.md`](architecture/03-request-lifecycle.md).

Confirm pgvector directly:

```bash
make psql-atlas
```

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

## What gets created

**Containers**

| Name | Image | Role |
| --- | --- | --- |
| `atlas-postgres` | `pgvector/pgvector:0.8.6-pg17-bookworm` | Both databases |
| `atlas-odoo` | built from `docker/odoo/Dockerfile` | Odoo 19 CE |
| `atlas-api` | built from `docker/atlas/Dockerfile` | The engine |

**Databases** — one cluster, two logical databases ([ADR-0004](adr/0004-vector-store-and-index-strategy.md)):

- `odoo` — created by Odoo on first boot; the ERP system of record.
- `atlas` — created by `docker/postgres/initdb/`, with the `vector` extension
  enabled. Empty until M4 adds the schema.

**Volumes** — `odoo-atlas-postgres-data` and `odoo-atlas-odoo-filestore`. They
survive `make down`. Only `make clean` removes them.

## Everyday commands

| Command | Effect |
| --- | --- |
| `make up` | Build and start everything |
| `make down` | Stop, keeping all data |
| `make logs` | Follow logs from every service |
| `make ps` | Service status and health |
| `make shell-odoo` / `make shell-atlas` | A shell inside a container |
| `make psql-odoo` / `make psql-atlas` | `psql` against either database |
| `make lint` / `make type` / `make test` | Quality gates, in a container |
| `make check` | Everything CI runs |
| `make clean` | Stop and **delete all data** (prompts first) |

Run `make help` for the full list.

## Configuration

Every setting lives in `.env`; `.env.example` documents each one. The values you
are most likely to change:

| Variable | Default | Notes |
| --- | --- | --- |
| `ODOO_PORT` | `8069` | Change if 8069 is taken |
| `ATLAS_API_PORT` | `8000` | Bound to `127.0.0.1` only |
| `ODOO_LOAD_DEMO_DATA` | `true` | `false` for an empty database |
| `ODOO_INIT_MODULES` | `base` | Becomes `base,odoo_atlas` at M5 |
| `ATLAS_LOG_LEVEL` | `INFO` | `DEBUG` while developing |

Model provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `VOYAGE_API_KEY`) are
present but unused until M3.

> **`.env` is git-ignored and must stay that way.** It is the only file in the
> repository that will ever hold a secret.

## Troubleshooting

**`POSTGRES_USER is required — copy .env.example to .env`**
You skipped `make init`. Compose refuses to start rather than silently using an
empty password.

**Port already allocated**, or on Windows:
`bind: An attempt was made to access a socket in a way forbidden by its access permissions`
Something else holds 8069, 8000 or 5432 — a locally installed PostgreSQL is the
usual culprit for 5432. Change the corresponding variable in `.env` (for example
`POSTGRES_PORT=5433`) and run `make up` again. Only the *host* mapping changes;
services still reach each other on 5432 across the compose network, so nothing
else needs editing.

Find the offending process on Windows with:

```bash
powershell -Command "Get-NetTCPConnection -LocalPort 5432 -State Listen | Select-Object OwningProcess"
```

**`option addons_path, invalid addons directory '/mnt/extra-addons', skipped`**
Expected until M5. Odoo skips an addons directory that contains no modules, and
`addons/` holds only a placeholder until the `odoo_atlas` addon lands. The
warning disappears on its own.

**`bad interpreter: /bin/sh^M` in a container log**
A shell script was checked out with Windows line endings. `.gitattributes`
prevents this, but a checkout made before it existed can carry CRLF. Fix with:

```bash
git rm --cached -r . && git reset --hard
```

**Odoo shows the database manager instead of a login page**
The bootstrap step did not complete. Check `docker compose logs odoo` for the
`[atlas-bootstrap]` lines. The most common cause is a failed first boot leaving a
partially created database — `make clean` and start over.

**`/readyz` reports `pgvector: missing`**
The `atlas` database exists but the extension is not installed. The init script
in `docker/postgres/initdb/` runs **only** on first initialisation of an empty
data directory, so it is skipped if the volume already existed. Either
`make clean` (destroys data) or install it by hand:

```bash
make psql-atlas
```
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

**`make: command not found` on Windows**
Expected — use `.\make.ps1 <target>` instead.

**`.\make.ps1` blocked by execution policy**
Allow local scripts for the current session:

```bash
powershell -ExecutionPolicy Bypass -File .\make.ps1 up
```

**Slow file access on Windows**
Make sure the repository lives on the Linux side of WSL 2, or that Docker Desktop
is using the WSL 2 backend. Bind mounts across the Windows filesystem boundary are
an order of magnitude slower.

## Uninstalling

```bash
make clean                       # stops everything and deletes both databases
docker image rm odoo-atlas/odoo:dev odoo-atlas/atlas:dev odoo-atlas/atlas:tools
```
