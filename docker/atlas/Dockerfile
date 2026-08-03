# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Odoo Atlas engine.
#
# Two stages. The builder resolves dependencies into a virtualenv; the runtime
# copies only that virtualenv. Compilers, build metadata and pip caches never
# reach the shipped image — smaller surface, fewer CVEs in the M14 scan gate.
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12

# --- builder ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# The venv is created with whatever pip the base image shipped, and an old pip
# is itself a vulnerability finding — `pip-audit` reports the toolchain
# alongside the dependencies. Upgrading here means the audit reports the
# project.
RUN pip install --no-cache-dir --upgrade pip setuptools

WORKDIR /build

# Dependency metadata is copied before the source so that editing a module does
# not invalidate the (expensive) dependency-resolution layer. README.md comes
# along because pyproject declares it as the package readme — hatchling refuses
# to generate metadata without it.
COPY services/atlas/pyproject.toml services/atlas/README.md ./
COPY services/atlas/src ./src

RUN pip install --no-cache-dir .

# --- dev -------------------------------------------------------------------
# The lint, type-check and test toolchain. Never shipped; used by `make test`,
# `make lint` and `make type` so contributors need no local Python at all —
# which matters, because the engine pins 3.12 while developer machines vary.
FROM builder AS dev

# git, for the tools that read the repository rather than the source: the
# benchmark harness records the commit a measurement came from, and
# semantic-release derives the next version from the commit history. Without it
# the benchmark recorded an empty commit — a results file that cannot be traced
# to code is not a measurement.
RUN apt-get update  && apt-get install -y --no-install-recommends git  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ".[dev]"

# Keep every tool cache off the bind-mounted workspace, so a container run never
# litters the host checkout with files it may not own.
ENV RUFF_CACHE_DIR=/tmp/.ruff_cache \
    MYPY_CACHE_DIR=/tmp/.mypy_cache \
    COVERAGE_FILE=/tmp/.coverage \
    PYTEST_ADDOPTS=-p\ no:cacheprovider

WORKDIR /workspace
CMD ["pytest"]

# --- runtime ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Odoo Atlas — Engine" \
      org.opencontainers.image.description="Retrieval, orchestration and generation engine for Odoo Atlas" \
      org.opencontainers.image.licenses="LGPL-3.0-or-later"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

# Run as an unprivileged user.
RUN groupadd --system --gid 1001 atlas \
 && useradd  --system --uid 1001 --gid atlas --create-home --shell /usr/sbin/nologin atlas

COPY --from=builder --chown=atlas:atlas /opt/venv /opt/venv

# Remove pip from the runtime. Nothing installs anything here — the virtualenv
# arrives complete from the builder — and pip vendors its own copies of
# `msgpack` and `setuptools`, which is where two HIGH findings came from in an
# image whose own dependencies were clean. Trivy attributes them to the image
# because they are on disk, and it is right to: vendored code is still code that
# ships.
#
# The secondary benefit is the one that matters longer: a runtime container with
# no package installer is one an attacker cannot `pip install` into.
RUN /opt/venv/bin/pip uninstall --yes pip  && rm -rf /usr/local/lib/python3.12/site-packages/pip            /usr/local/lib/python3.12/site-packages/pip-*.dist-info            /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

# Migrations ship with the image so a deployment can apply its own schema:
#   docker compose exec atlas-api alembic upgrade head
# Without these the image can run but never migrate, which is only discovered on
# the first deploy to an empty database.
COPY --chown=atlas:atlas services/atlas/alembic.ini /app/alembic.ini
COPY --chown=atlas:atlas services/atlas/migrations /app/migrations

WORKDIR /app
USER atlas

EXPOSE 8000

# Liveness only — deliberately does not touch PostgreSQL. A health check that
# probes a downstream dependency turns a database blip into a restart storm.
# Readiness is a separate concern, exposed at /readyz for the orchestrator.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "atlas.interfaces.http.app:app", "--host", "0.0.0.0", "--port", "8000"]
