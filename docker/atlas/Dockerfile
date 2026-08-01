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

WORKDIR /app
USER atlas

EXPOSE 8000

# Liveness only — deliberately does not touch PostgreSQL. A health check that
# probes a downstream dependency turns a database blip into a restart storm.
# Readiness is a separate concern, exposed at /readyz for the orchestrator.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "atlas.interfaces.http.app:app", "--host", "0.0.0.0", "--port", "8000"]
